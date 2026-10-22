from __future__ import annotations

from glance_core.content_revision import current_revision_clause, is_current_revision

import hashlib
import json
from datetime import timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from glance_core.agent_roles import AgentRole, primary_role_for, role_spec
from glance_core.database import begin_immediate
from glance_core.lease_policy import lease_minutes_for_task
from glance_core.models import (
    AgentSubmission,
    AgentTask,
    AgentWorker,
    Artifact,
    AuditEvent,
    Content,
    ContentRevision,
    SchedulerOffer,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.security import hash_secret, issue_token, verify_secret
from glance_core.task_schemas import ContentResearchResult, is_blocked_discovery_url, result_model_for, schema_for


class TaskConflictError(RuntimeError):
    pass


class TaskCapabilityError(TaskConflictError):
    """The worker runtime is not authorized to claim the requested role."""

    code = "CAPABILITY_REJECTED"

    def __init__(self, role_id: AgentRole | str, errors: list[dict[str, Any]]) -> None:
        self.role_id = str(role_id)
        self.errors = errors
        detail = "; ".join(str(error.get("message") or error) for error in errors)
        super().__init__(f"Agent runtime rejected for role {self.role_id!r}: {detail}")


class TaskLaneBusyError(TaskConflictError):
    def __init__(self, lane_key: str, task_id: str) -> None:
        super().__init__(f"Research direction {lane_key!r} is already handled by {task_id}")
        self.lane_key = lane_key
        self.task_id = task_id


class AgentCapacityError(TaskConflictError):
    def __init__(self, agent_id: str, active_leases: int, max_inflight: int) -> None:
        super().__init__(
            f"Agent {agent_id!r} already holds {active_leases}/{max_inflight} active leases"
        )
        self.agent_id = agent_id
        self.active_leases = active_leases
        self.max_inflight = max_inflight


class TaskValidationError(ValueError):
    def __init__(self, errors: list[dict[str, Any]]) -> None:
        super().__init__("Agent result validation failed")
        self.errors = errors


#: 同一 attempt 内允许的校验失败总次数（首次提交 + 3 次重提）；
#: 未超限时保留租约，剩余租约时间即修正窗口。
MAX_VALIDATION_FAILURES_PER_ATTEMPT = 4


def runtime_capability_errors(
    role_id: AgentRole | str | None,
    runtime_info: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return strict claim/dispatch mismatches for the publish runtime.

    Other roles retain their existing tolerant runtime reporting contract.  Publish
    is different: an omitted runtime is an old worker, and an old skill/protocol or
    incomplete host capability set must not receive a lease.
    """
    try:
        role = AgentRole(role_id) if role_id else None
    except (TypeError, ValueError):
        role = None
    if role != AgentRole.PUBLISH:
        return []

    spec = role_spec(role)
    runtime = runtime_info or {}
    errors: list[dict[str, Any]] = []
    for field, expected in (
        ("skill_name", spec.skill_name),
        ("skill_version", spec.skill_version),
        ("protocol_version", spec.protocol_version),
    ):
        actual = str(runtime.get(field) or "")
        if actual != expected:
            errors.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                    "message": f"runtime.{field} must equal {expected!r}",
                }
            )

    advertised = runtime.get("capabilities")
    if not isinstance(advertised, (list, tuple, set, frozenset)):
        advertised_values: set[str] = set()
    else:
        advertised_values = {str(value).strip() for value in advertised if str(value).strip()}
    missing = [
        capability
        for capability in spec.required_host_capabilities
        if capability not in advertised_values
    ]
    if missing:
        errors.append(
            {
                "field": "capabilities",
                "expected": list(spec.required_host_capabilities),
                "actual": sorted(advertised_values),
                "missing": missing,
                "message": f"runtime.capabilities missing required values: {', '.join(missing)}",
            }
        )
    return errors


def require_runtime_capability(
    role_id: AgentRole | str | None,
    runtime_info: dict[str, Any] | None,
) -> None:
    errors = runtime_capability_errors(role_id, runtime_info)
    if errors:
        raise TaskCapabilityError(role_id or "", errors)


def _generated_segment_errors(task: AgentTask, segments: list[dict[str, Any]]) -> list[dict]:
    """生图段的业务上限校验：按制作模式分支（V10 REQ-5）。

    footage 保持既有约束（至多 2 段、不得连续、无参考重绘）；
    image_only 改按管理台可调的生图预算限制，并校验参考原图属于本内容。
    """
    errors: list[dict] = []
    requests = [
        (index, segment.get("asset_request") or {})
        for index, segment in enumerate(segments)
        if segment.get("asset_request")
    ]
    if str(task.input_data.get("production_mode") or "footage") == "image_only":
        budget = int(task.input_data.get("image_only_gen_budget") or 0)
        if len(requests) > budget:
            errors.append(
                {
                    "path": ["segments"],
                    "message": (
                        f"图文模式生图段最多 {budget} 段（含参考重绘），实际 {len(requests)} 段；"
                        "其余段改用原图直用"
                    ),
                }
            )
        allowed_references = {
            str(item) for item in task.input_data.get("allowed_reference_artifact_ids", [])
        }
        for index, request in requests:
            reference = str(request.get("reference_artifact_id") or "")
            if reference and reference not in allowed_references:
                errors.append(
                    {
                        "path": ["segments", index, "asset_request", "reference_artifact_id"],
                        "message": "reference_artifact_id 必须指向本内容已下载的网页原图",
                    }
                )
        return errors
    if len(requests) > 2:
        errors.append({"path": ["segments"], "message": "全片 asset_request 不得超过 2 段"})
    positions = [index for index, _ in requests]
    if any(right == left + 1 for left, right in pairwise(positions)):
        errors.append({"path": ["segments"], "message": "asset_request 不得连续出现"})
    for index, request in requests:
        if request.get("reference_artifact_id"):
            errors.append(
                {
                    "path": ["segments", index, "asset_request", "reference_artifact_id"],
                    "message": "reference_artifact_id 仅图文模式（image_only）可用",
                }
            )
    return errors


def source_media_research(session: Session, content_id: str, inputs: dict) -> tuple[AgentTask, dict, list[dict]]:
    """Resolve the accepted research and its acquired images from server-owned lineage."""
    refs = inputs.get("upstream_task_ids") or {}
    if not isinstance(refs, dict):
        raise TaskConflictError("SOURCE_MEDIA_SCRIPT_LINEAGE_REQUIRED")
    research_id = refs.get("research")
    script_id = refs.get("script")
    if not isinstance(script_id, str) or not script_id.strip():
        raise TaskConflictError("SOURCE_MEDIA_SCRIPT_LINEAGE_REQUIRED")
    script = session.get(AgentTask, script_id)
    scripts = session.exec(select(AgentTask).where(
        current_revision_clause(AgentTask), AgentTask.content_id == content_id, AgentTask.task_type == TaskType.SHORT_SCRIPT,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc()).limit(2)).all()
    current_script = scripts[0] if scripts else None
    if (script is None or script.content_id != content_id or script.task_type != TaskType.SHORT_SCRIPT
            or current_script is None or current_script.id != script.id
            or (len(scripts) == 2 and scripts[0].created_at == scripts[1].created_at)
            or script.status != TaskStatus.COMPLETED or stable_hash(script.input_data) != script.input_hash):
        raise TaskConflictError("SOURCE_MEDIA_SCRIPT_LINEAGE_CHANGED")
    bound_id = (script.input_data.get("upstream_task_ids") or {}).get("research")
    if research_id and research_id != bound_id:
        raise TaskConflictError("SOURCE_MEDIA_RESEARCH_LINEAGE_CHANGED")
    research_id = bound_id
    research = session.get(AgentTask, research_id) if isinstance(research_id, str) else None
    researches = session.exec(select(AgentTask).where(
        current_revision_clause(AgentTask), AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_RESEARCH,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc()).limit(2)).all()
    current = researches[0] if researches else None
    if (research is None or current is None or research.id != current.id or research.content_id != content_id
            or (len(researches) == 2 and researches[0].created_at == researches[1].created_at)
            or research.task_type != TaskType.CONTENT_RESEARCH or research.status != TaskStatus.COMPLETED
            or stable_hash(research.input_data) != research.input_hash):
        raise TaskConflictError("SOURCE_MEDIA_RESEARCH_REQUIRED: 先完成当前产品素材盘点研究")
    accepted = session.exec(select(AgentSubmission).where(
        AgentSubmission.task_id == research.id, AgentSubmission.attempt == research.attempt,
        AgentSubmission.validation_status == "ACCEPTED",
    )).all()
    if (len(accepted) != 1 or accepted[0].agent_id != research.assigned_agent_id
            or accepted[0].result_hash != stable_hash(research.result_data)):
        raise TaskConflictError("SOURCE_MEDIA_ACCEPTED_RESEARCH_CHANGED")
    try:
        value = ContentResearchResult.model_validate(research.result_data).model_dump(mode="json")
        search = value.get("source_media_search")
        if search is None or (not value["source_images"] and not search["image_gap_reason"]):
            raise ValueError("素材盘点缺失")
    except ValueError as exc:
        raise TaskConflictError("SOURCE_MEDIA_AUDIT_REQUIRED: 先执行补充研究，不得沿用未盘点结果") from exc
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == research.id,
        AuditEvent.actor_type == "system", AuditEvent.actor_id == "pipeline",
        AuditEvent.action == "source_media_images_acquired",
    )).all()
    if not value["source_images"] and not receipts:
        return research, search, []
    if len(receipts) != 1 or receipts[0].metadata_json.get("result_hash") != accepted[0].result_hash:
        raise TaskConflictError("SOURCE_MEDIA_ACQUISITION_REQUIRED: 图片下载结果尚未登记")
    rows = receipts[0].metadata_json.get("images", [])
    urls = {item["url"] for item in value["source_images"]}
    for row in rows:
        artifact = session.get(Artifact, row.get("artifact_id"))
        if (row.get("url") not in urls or artifact is None or artifact.content_id != content_id
                or artifact.revision_id != research.revision_id
                or artifact.artifact_type != "source_image" or artifact.sha256 != row.get("sha256")
                or artifact.relative_path != row.get("relative_path")):
            raise TaskConflictError("SOURCE_MEDIA_ACQUIRED_IMAGE_CHANGED")
    if receipts[0].metadata_json.get("unavailable_image_urls") != [
        item["url"] for item in value["source_images"] if item["url"] not in {row["url"] for row in rows}
    ]:
        raise TaskConflictError("SOURCE_MEDIA_ACQUISITION_OUTCOME_CHANGED")
    return research, search, rows


def source_media_acquisition(research: AgentTask, rows: list[dict]) -> dict:
    requested = [item["url"] for item in research.result_data.get("source_images", [])]
    downloaded = [row["url"] for row in rows]
    return {"research_task_id": research.id, "research_result_hash": stable_hash(research.result_data),
            "requested_image_urls": requested, "downloaded_image_urls": downloaded,
            "unavailable_image_urls": [url for url in requested if url not in downloaded]}


def _source_media_priority_errors(session: Session, task: AgentTask, result: dict) -> list[dict]:
    inputs = task.input_data or {}
    if inputs.get("production_mode") != "image_only" or "source_media_priority_version" not in inputs:
        return []
    if type(inputs["source_media_priority_version"]) is not int or inputs["source_media_priority_version"] != 1:
        return [{"path": ["source_media_priority_version"], "message": "不支持的素材优先级合同版本"}]
    if task.task_type == TaskType.CONTENT_RESEARCH:
        search = result.get("source_media_search")
        if not isinstance(search, dict):
            return [{"path": ["source_media_search"], "message": "必须先盘点官方产品图片、同型号宣传视频及媒体库，不能把漏检当成素材不足"}]
        if not result.get("source_images") and not str(search.get("image_gap_reason") or "").strip():
            return [{"path": ["source_media_search", "image_gap_reason"], "message": "没有可用原图时必须说明已检查来源及具体缺口，不能静默改用全 AI 图"}]
    if task.task_type != TaskType.VISUAL_PLAN:
        return []
    try:
        research, search, registered = source_media_research(session, task.content_id, inputs)
        if (inputs.get("source_media_search") != search
                or inputs.get("source_media_acquisition") != source_media_acquisition(research, registered)):
            raise TaskConflictError("SOURCE_MEDIA_AUDIT_CHANGED: 计划盘点必须与已接受研究完全一致")
    except TaskConflictError as exc:
        return [{"path": ["source_media_search"], "message": str(exc)}]
    allowed = set(inputs.get("allowed_asset_ids") or [])
    source_ids = {row["artifact_id"] for row in registered}
    declared = {item.get("artifact_id") for item in inputs.get("source_images", []) if isinstance(item, dict)}
    errors = []
    if declared - source_ids or declared != source_ids.intersection(allowed) or (source_ids and not declared):
        errors.append({"path": ["source_images"], "message": "原图必须是本内容已登记且在可用白名单中的 source_image"})
    unrelated = set(session.exec(select(Artifact.id).where(
        current_revision_clause(Artifact), Artifact.content_id == task.content_id, Artifact.artifact_type == "source_image",
        Artifact.id.in_(allowed),
    )).all()) - source_ids
    if unrelated:
        errors.append({"path": ["allowed_asset_ids"], "message": "原图白名单包含不属于当前已接受研究取图收据的素材"})
    segments = result.get("segments", [])
    if source_ids and not any(segment.get("asset_id") in source_ids for segment in segments):
        errors.append({"path": ["segments"], "message": "已有相关产品原图，计划必须优先直用，不能整片由 AI 图替代"})
    for index, segment in enumerate(segments):
        request = segment.get("asset_request")
        if request and not str(request.get("source_gap_reason") or "").strip():
            errors.append({"path": ["segments", index, "asset_request", "source_gap_reason"],
                           "message": "AI 补图必须说明现有产品素材无法覆盖的具体画面缺口"})
    return errors


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


PUBLISH_INTENT_ENTITY = "publish_intent"
PUBLISH_INTENT_REGISTERED = "publish_intent_registered"
PUBLISH_INTENT_STATE = "publish_intent_state"
_PUBLISH_RECONCILE_STATUSES = frozenset({"PUBLISHED", "FAILED", "UNKNOWN", "NEEDS_USER"})
_PUBLISH_INTENT_STATUSES = frozenset(
    {
        "AUTHORIZED",
        "PUBLISHED",
        "FAILED",
        "UNKNOWN",
        "NEEDS_USER",
        "INTERRUPTED",
        "RECONCILE_REQUIRED",
    }
)
_PUBLISH_TARGET_FIELDS = (
    "platform",
    "account_id",
    "content_id",
    "video_artifact_id",
    "video_sha256",
    "intent_key",
)


def publish_target_for_task(task: AgentTask) -> dict[str, str]:
    """Return the immutable external target used for publish idempotency.

    The target deliberately contains both the artifact identity and its digest when
    available.  A retry/recovery task can therefore point at the same video without
    creating a second click authorization, while a genuinely new render gets a new
    target key.
    """
    if task.task_type != TaskType.DOUYIN_PUBLISH:
        raise TaskConflictError("Publish intent requires a douyin_publish task")
    input_data = task.input_data or {}
    account = input_data.get("account")
    account_id = (
        account.get("account_id") if isinstance(account, dict) else input_data.get("account_id")
    )
    if not account_id and isinstance(account, dict) and account.get("single_account"):
        account_id = "local-default"
    target = {
        "platform": str(input_data.get("platform") or "").strip().lower(),
        "account_id": str(account_id or "").strip(),
        "content_id": str(task.content_id or input_data.get("content_id") or "").strip(),
        "video_artifact_id": str(input_data.get("video_artifact_id") or "").strip(),
        "video_sha256": str(input_data.get("video_sha256") or "").strip().lower(),
    }
    if not target["platform"] or not target["account_id"] or not target["content_id"]:
        raise TaskConflictError("Publish intent is missing platform, account, or content")
    if not target["video_artifact_id"] and not target["video_sha256"]:
        raise TaskConflictError("Publish intent is missing video artifact identity")
    if task.revision_id:
        if input_data.get("content_revision_id") != task.revision_id:
            raise TaskConflictError("CONTENT_REVISION_INPUT_CHANGED")
        target["content_revision_id"] = task.revision_id
    target["intent_key"] = stable_hash({key: target[key] for key in target if key != "intent_key"})
    return target


_NON_RETRYABLE_TASK_ERROR_PREFIXES = (
    "ENVIRONMENT_",
    "TASK_CONTRACT",
    "CONTEXT_MISSING",
    "BUNDLE_",
    "DEPENDENCY_MISSING",
    "AGENT_TASK_DEPENDENCY_",
    "HANDOFF_ERROR",
)


def is_non_retryable_task_error(error_code: str | None) -> bool:
    """Return whether an Agent task error must go through Recovery, not requeue.

    A task package or dependency defect cannot be fixed by running the same immutable
    input again.  Keep this classifier deliberately code-based so the admin API and
    the console use the same safety rule without inspecting free-form messages.
    """
    normalized = str(error_code or "").strip().upper()
    return bool(normalized) and (
        normalized in {"CONTEXT_MISSING", "DEPENDENCY_MISSING", "BUNDLE_MISSING"}
        or normalized.startswith(_NON_RETRYABLE_TASK_ERROR_PREFIXES)
    )


def _normalize_content_task_input(
    session: Session,
    task_type: TaskType,
    content_id: str | None,
    input_data: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Fill deterministic task fields from the server-owned content record.

    AgentTask input is an immutable task contract once created.  Normalizing here keeps
    callers from creating a second, subtly different contract than the workflow builder,
    while leaving the caller's dictionary untouched.  We only repair fields whose value is
    authoritative in the control plane; semantic Agent input is never guessed here.
    """
    normalized = dict(input_data)
    changed: list[str] = []
    if not content_id:
        return normalized, changed
    content = session.get(Content, content_id)
    if content is None:
        return normalized, changed

    if "content_revision_id" in normalized and normalized["content_revision_id"] != content.current_revision_id:
        raise TaskConflictError("CONTENT_REVISION_INPUT_CHANGED")
    if content.current_revision_id:
        normalized["content_revision_id"] = content.current_revision_id
        changed.append("content_revision_id")
        revision = session.get(ContentRevision, content.current_revision_id)
        if revision is None or revision.content_id != content_id:
            raise TaskConflictError("CONTENT_REVISION_HEAD_MISSING")
        context = {"id": revision.id, "revision_no": revision.revision_no,
                   "parent_revision_id": revision.parent_revision_id,
                   "previous_snapshot_sha256": revision.subject_sha256,
                   "feedback": revision.feedback_json.get("feedback", "")}
        if "content_revision" in normalized and normalized["content_revision"] != context:
            raise TaskConflictError("CONTENT_REVISION_FEEDBACK_CHANGED")
        normalized["content_revision"] = context
        changed.append("content_revision")
        refs = normalized.get("upstream_task_ids") or {}
        for value in refs.values():
            for task_id in value if isinstance(value, list) else [value]:
                parent = session.get(AgentTask, task_id) if isinstance(task_id, str) else None
                if (parent is None or parent.content_id != content_id
                        or parent.revision_id != content.current_revision_id):
                    raise TaskConflictError("CONTENT_REVISION_UPSTREAM_CHANGED")
        asset_ids = set(normalized.get("allowed_asset_ids") or [])
        asset_ids.update(normalized.get("allowed_reference_artifact_ids") or [])
        for field in ("video_artifact_id", "cover_artifact_id"):
            if normalized.get(field):
                asset_ids.add(normalized[field])
        if isinstance(normalized.get("voice_artifact"), dict) and normalized["voice_artifact"].get("id"):
            asset_ids.add(normalized["voice_artifact"]["id"])
        for artifact_id in asset_ids:
            artifact = session.get(Artifact, artifact_id)
            if (artifact is None or artifact.content_id != content_id
                    or artifact.revision_id != content.current_revision_id):
                raise TaskConflictError("CONTENT_REVISION_ARTIFACT_CHANGED")

    if task_type in {
        TaskType.CONTENT_RESEARCH,
        TaskType.SHORT_SCRIPT,
        TaskType.VISUAL_PLAN,
    }:
        expected_mode = content.production_mode.value
        if normalized.get("production_mode") != expected_mode:
            normalized["production_mode"] = expected_mode
            changed.append("production_mode")
        if task_type in {TaskType.CONTENT_RESEARCH, TaskType.VISUAL_PLAN} and expected_mode == "image_only":
            if type(normalized.get("source_media_priority_version")) is not int or normalized.get("source_media_priority_version") != 1:
                normalized["source_media_priority_version"] = 1
                changed.append("source_media_priority_version")
            if task_type == TaskType.VISUAL_PLAN:
                research, search, rows = source_media_research(session, content_id, normalized)
                acquisition = source_media_acquisition(research, rows)
                for key, value in (("source_media_search", search), ("source_media_acquisition", acquisition)):
                    if key in normalized and normalized[key] != value:
                        raise TaskConflictError(f"SOURCE_MEDIA_AUDIT_CHANGED: {key}")
                    if key not in normalized:
                        normalized[key] = value
                        changed.append(key)
                source_ids = {row["artifact_id"] for row in rows}
                allowed = set(normalized.get("allowed_asset_ids") or [])
                declared = {row.get("artifact_id") for row in normalized.get("source_images", []) if isinstance(row, dict)}
                offered = set(session.exec(select(Artifact.id).where(
                    current_revision_clause(Artifact), Artifact.content_id == content_id, Artifact.artifact_type == "source_image", Artifact.id.in_(allowed),
                )).all())
                if (declared != source_ids.intersection(allowed) or offered - source_ids
                        or (source_ids and not declared)):
                    raise TaskConflictError("SOURCE_MEDIA_SOURCE_WHITELIST_CHANGED")

    if task_type in {
        TaskType.SCENE_ANALYZE,
        TaskType.VISUAL_PLAN,
        TaskType.VISUAL_PLAN_QC,
    }:
        scenes = normalized.get("scenes") or []
        if scenes and not normalized.get("context_files"):
            context_files = [
                str(scene.get("thumbnail") or "").replace("\\", "/")
                for scene in scenes
                if isinstance(scene, dict) and scene.get("thumbnail")
            ]
            if context_files:
                normalized["context_files"] = context_files
                changed.append("context_files")
    return normalized, changed


def task_dedup_key(
    *,
    task_type: TaskType,
    content_id: str | None,
    input_hash: str,
    instructions: str,
    prompt_version: str,
    schema_version: str,
) -> str:
    return stable_hash(
        {
            "task_type": task_type.value,
            "content_id": content_id,
            "input_hash": input_hash,
            "instructions_hash": stable_hash(instructions),
            "prompt_version": prompt_version,
            "schema_version": schema_version,
        }
    )


class AgentTaskService:
    def __init__(
        self,
        session: Session,
        submission_root: Path,
        lease_minutes: int = 45,
        max_inflight: int = 1,
    ) -> None:
        self.session = session
        self.submission_root = submission_root
        self.lease_minutes = lease_minutes
        self.max_inflight = max(1, max_inflight)

    def _latest_publish_intent(self, intent_key: str) -> AuditEvent | None:
        events = self.session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_type == PUBLISH_INTENT_ENTITY,
                AuditEvent.entity_id == intent_key,
            )
        ).all()
        if not events:
            return None
        return max(
            events,
            key=lambda event: (
                self._intent_sequence(event),
                as_utc(event.created_at).isoformat() if event.created_at else "",
                str(event.id or ""),
            ),
        )

    @staticmethod
    def _intent_metadata(event: AuditEvent) -> dict[str, Any] | None:
        metadata = event.metadata_json
        return metadata if isinstance(metadata, dict) else None

    @classmethod
    def _intent_sequence(cls, event: AuditEvent) -> int:
        metadata = cls._intent_metadata(event)
        value = metadata.get("intent_sequence") if metadata is not None else None
        if isinstance(value, bool):
            return -1
        try:
            return int(value)
        except (TypeError, ValueError):
            return -1

    @staticmethod
    def _intent_metadata_matches_target(
        metadata: dict[str, Any] | None, target: dict[str, str]
    ) -> bool:
        return metadata is not None and all(
            metadata.get(field) == target[field] for field in (*_PUBLISH_TARGET_FIELDS,
                *(("content_revision_id",) if "content_revision_id" in target else ()))
        )

    def _publish_intent_registered(self, target: dict[str, str]) -> bool:
        events = self.session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_type == PUBLISH_INTENT_ENTITY,
                AuditEvent.entity_id == target["intent_key"],
                AuditEvent.action == PUBLISH_INTENT_REGISTERED,
            )
        ).all()
        for event in events:
            metadata = self._intent_metadata(event)
            if (
                self._intent_metadata_matches_target(metadata, target)
                and metadata is not None
                and metadata.get("status") == "AUTHORIZED"
                and self._intent_sequence(event) == 0
                and isinstance(metadata.get("task_id"), str)
                and bool(metadata["task_id"])
                and isinstance(metadata.get("agent_id"), str)
                and bool(metadata["agent_id"])
                and isinstance(metadata.get("attempt"), int)
                and not isinstance(metadata["attempt"], bool)
                and metadata["attempt"] > 0
            ):
                return True
        return False

    @staticmethod
    def _publish_intent_payload(
        target: dict[str, str], event: AuditEvent | None, *, authorized: bool
    ) -> dict[str, Any]:
        metadata = AgentTaskService._intent_metadata(event) if event is not None else None
        metadata_is_valid = event is None or AgentTaskService._intent_metadata_matches_target(
            metadata, target
        )
        raw_status = metadata.get("status") if metadata_is_valid and metadata else None
        intent_status = (
            raw_status
            if isinstance(raw_status, str) and raw_status in _PUBLISH_INTENT_STATUSES
            else ("AUTHORIZED" if event is None else "CORRUPT")
        )
        task_id = (
            metadata.get("task_id")
            if metadata_is_valid and metadata and isinstance(metadata.get("task_id"), str)
            else ""
        )
        return {
            "status": "AUTHORIZED" if authorized else "RECONCILE_REQUIRED",
            "click_authorized": authorized,
            "intent_key": target["intent_key"],
            "intent_status": intent_status,
            "task_id": task_id,
            "target": {
                key: target[key]
                for key in (
                    "platform",
                    "account_id",
                    "content_id",
                    "video_artifact_id",
                    "video_sha256",
                )
            },
        }

    def publish_intent_for_target(self, target: dict[str, str]) -> dict[str, Any] | None:
        """Read the durable intent state without changing task or business state."""
        event = self._latest_publish_intent(target["intent_key"])
        if event is None:
            return None
        return self._publish_intent_payload(target, event, authorized=False)

    def publish_intent_for_task(self, task: AgentTask) -> dict[str, Any] | None:
        return self.publish_intent_for_target(publish_target_for_task(task))

    def _assert_clean_before_publish_intent(self) -> None:
        # begin_immediate() intentionally rolls back the Session.  Refuse to discard
        # caller-owned business writes rather than guessing whether they are safe.
        if self.session.new or self.session.dirty or self.session.deleted:
            raise TaskConflictError("Publish intent requires a clean committed session")

    def register_publish_intent(
        self,
        *,
        task_id: str,
        lease_token: str,
        agent_id: str,
    ) -> dict[str, Any]:
        """Atomically grant the one external publish click for a target.

        Existing intent is always returned as reconciliation-only, even when the
        caller presents an expired lease or a different task.  Only a clean session
        with a currently valid, confirmed lease can create the first intent.
        """
        self._assert_clean_before_publish_intent()
        begin_immediate(self.session)
        try:
            task = self.session.get(AgentTask, task_id)
            if task is None:
                raise KeyError(task_id)
            target = publish_target_for_task(task)
            existing = self._latest_publish_intent(target["intent_key"])
            if existing is not None:
                response = self._publish_intent_payload(target, existing, authorized=False)
                self.session.rollback()
                return response

            now = utc_now()
            if (
                task.status != TaskStatus.LEASED
                or task.assigned_agent_id != agent_id
                or task.lease_expires_at is None
                or as_utc(task.lease_expires_at) < now
                or task.lease_token_hash is None
                or not verify_secret(lease_token, task.lease_token_hash)
            ):
                raise TaskConflictError("Lease is missing, invalid, or expired")
            if task.attempt_started_at is None:
                raise TaskConflictError("Confirm claim before registering publish intent")

            from glance_core.content_revision import is_current_revision

            if not is_current_revision(self.session, task):
                raise TaskConflictError("CONTENT_REVISION_STALE")
            from glance_core.content_revision import require_reconciled_publish_intents

            require_reconciled_publish_intents(self.session, task.content_id)

            event = AuditEvent(
                actor_type="agent",
                actor_id=agent_id,
                entity_type=PUBLISH_INTENT_ENTITY,
                entity_id=target["intent_key"],
                action=PUBLISH_INTENT_REGISTERED,
                metadata_json={
                    **target,
                    "status": "AUTHORIZED",
                    "task_id": task.id,
                    "agent_id": agent_id,
                    "attempt": task.attempt,
                    "intent_sequence": 0,
                },
            )
            self.session.add(event)
            self.session.commit()
            return self._publish_intent_payload(target, event, authorized=True)
        except BaseException:
            self.session.rollback()
            raise

    def _record_publish_intent_state(
        self,
        task: AgentTask,
        *,
        status: str,
        actor_id: str,
        reason: str = "",
        external_id: str = "",
        external_url: str = "",
        evidence_artifact_id: str = "",
    ) -> AuditEvent | None:
        try:
            target = publish_target_for_task(task)
        except TaskConflictError:
            return None
        latest = self._latest_publish_intent(target["intent_key"])
        if latest is None:
            return None
        # Lease diagnostics cannot undo a reconciled external outcome. Explicit
        # reconciliation still accepts separately validated observed outcomes.
        previous_metadata = latest.metadata_json if isinstance(latest.metadata_json, dict) else {}
        if (previous_metadata.get("status") in {"PUBLISHED", "FAILED"}
                and status.strip().upper() in {"INTERRUPTED", "RECONCILE_REQUIRED", "AUTHORIZED"}):
            return None
        previous_events = self.session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_type == PUBLISH_INTENT_ENTITY,
                AuditEvent.entity_id == target["intent_key"],
            )
        ).all()
        next_sequence = (
            max(
                (self._intent_sequence(event) for event in previous_events),
                default=-1,
            )
            + 1
        )
        metadata: dict[str, Any] = {
            **target,
            "status": status.strip().upper(),
            "task_id": task.id,
            "intent_sequence": next_sequence,
        }
        for key, value in {
            "reason": reason,
            "external_id": external_id,
            "external_url": external_url,
            "evidence_artifact_id": evidence_artifact_id,
        }.items():
            if value:
                metadata[key] = value
        event = AuditEvent(
            actor_type="system" if actor_id.startswith(("lease-", "recovery-")) else "agent",
            actor_id=actor_id,
            entity_type=PUBLISH_INTENT_ENTITY,
            entity_id=target["intent_key"],
            action=PUBLISH_INTENT_STATE,
            metadata_json=metadata,
        )
        self.session.add(event)
        return event

    def reconcile_publish_intent(
        self,
        task: AgentTask,
        *,
        status: str,
        actor_id: str = "publish-agent",
        reason: str = "",
        external_id: str = "",
        external_url: str = "",
        evidence_artifact_id: str = "",
    ) -> dict[str, Any] | None:
        """Append an observed external outcome; never grants another click."""
        if not isinstance(status, str):
            raise TaskConflictError("Publish result status is invalid")
        normalized_status = status.strip().upper()
        if normalized_status not in _PUBLISH_RECONCILE_STATUSES:
            raise TaskConflictError(f"Unsupported publish result status: {normalized_status!r}")
        if normalized_status == "PUBLISHED":
            target = self.validate_published_publish_result(
                task,
                external_id=external_id,
                external_url=external_url,
                evidence_artifact_id=evidence_artifact_id,
            )
        else:
            target = None
        event = self._record_publish_intent_state(
            task,
            status=normalized_status,
            actor_id=actor_id,
            reason=reason,
            external_id=external_id,
            external_url=external_url,
            evidence_artifact_id=evidence_artifact_id,
        )
        if event is None:
            return None
        self.session.commit()
        target = target or publish_target_for_task(task)
        return self._publish_intent_payload(target, event, authorized=False)

    def validate_published_publish_result(
        self,
        task: AgentTask,
        *,
        external_id: str = "",
        external_url: str = "",
        evidence_artifact_id: str = "",
    ) -> dict[str, str]:
        """Validate the proof and the durable authorization without writing state."""
        external_id = str(external_id or "").strip()
        external_url = str(external_url or "").strip()
        evidence_artifact_id = str(evidence_artifact_id or "").strip()
        if not external_id and not external_url:
            raise TaskConflictError("PUBLISHED requires an external id or URL")
        if not evidence_artifact_id:
            raise TaskConflictError("PUBLISHED requires publish evidence")
        evidence = self.session.get(Artifact, evidence_artifact_id)
        if (
            evidence is None
            or evidence.content_id != task.content_id
            or evidence.revision_id != task.revision_id
            or evidence.artifact_type != "publish_evidence"
            or evidence.producer_stage != task.id
        ):
            raise TaskConflictError("PUBLISHED requires evidence uploaded by this task")
        target = publish_target_for_task(task)
        if not self._publish_intent_registered(target):
            raise TaskConflictError("PUBLISHED requires a registered publish submit intent")
        from glance_core.content_revision import require_distinct_publication_identity

        require_distinct_publication_identity(self.session, task.content_id, task.revision_id,
            target["platform"], external_id, external_url)
        return target

    def create_task(
        self,
        *,
        task_type: TaskType,
        instructions: str,
        input_data: dict[str, Any],
        content_id: str | None = None,
        priority: int = 0,
        prompt_version: str = "v1",
        schema_version: str = "1",
        lane_key: str | None = None,
        required_role: str | None = None,
    ) -> AgentTask:
        input_data, normalized_fields = _normalize_content_task_input(
            self.session, task_type, content_id, input_data
        )
        if prompt_version == "v1":
            prompt_version = f"builtin-{stable_hash(instructions)[:8]}"
        input_hash = stable_hash(input_data)
        dedup_key = task_dedup_key(
            task_type=task_type,
            content_id=content_id,
            input_hash=input_hash,
            instructions=instructions,
            prompt_version=prompt_version,
            schema_version=schema_version,
        )
        existing = self.session.exec(
            select(AgentTask).where(AgentTask.dedup_key == dedup_key)
        ).first()
        if existing is not None:
            self._record_duplicate(existing)
            return existing
        if lane_key:
            lane_task = self._active_lane_task(lane_key)
            if lane_task is not None:
                raise TaskLaneBusyError(lane_key, lane_task.id)
        task = AgentTask(
            task_type=task_type,
            content_id=content_id,
            revision_id=(self.session.get(Content, content_id).current_revision_id
                         if content_id and self.session.get(Content, content_id) else None),
            priority=priority,
            prompt_name=task_type.value,
            prompt_version=prompt_version,
            schema_version=schema_version,
            instructions=instructions,
            input_data=input_data,
            result_schema=schema_for(task_type, schema_version),
            input_hash=input_hash,
            dedup_key=dedup_key,
            lane_key=lane_key,
            required_role=required_role
            or ("" if task_type == TaskType.AGENT_SELFTEST else primary_role_for(task_type).value),
        )
        self.session.add(task)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="task-service",
                entity_type="agent_task",
                entity_id=task.id,
                action="created",
            )
        )
        if normalized_fields:
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="task-service",
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="task_input_normalized",
                    metadata_json={"fields": normalized_fields},
                )
            )
        try:
            self.session.commit()
        except IntegrityError:
            # 另一请求在本事务提交前创建了同一任务。唯一索引是最终裁决者。
            self.session.rollback()
            existing = self.session.exec(
                select(AgentTask).where(AgentTask.dedup_key == dedup_key)
            ).first()
            if existing is not None:
                self._record_duplicate(existing)
                return existing
            lane_task = self._active_lane_task(lane_key) if lane_key else None
            if lane_task is not None:
                raise TaskLaneBusyError(lane_key, lane_task.id) from None
            raise
        self.session.refresh(task)
        return task

    def _authorize_claim(self, *, agent_id, task_id, role_id, runtime_info, authorization_context):
        """Runtime-specific permits are checked inside the lease transaction."""

    def _after_claim(self, *, task, authorization_context):
        """Record runtime-specific claim associations before the lease commits."""

    def claim(
        self,
        *,
        agent_id: str,
        capabilities: set[TaskType],
        role_id: AgentRole | str = "",
        runtime_info: dict[str, Any] | None = None,
        reserve_seconds: int | None = None,
        task_id: str | None = None,
        authorization_context: tuple | None = None,
    ) -> tuple[AgentTask, str] | None:
        # Validate before BEGIN IMMEDIATE: an old publish worker must receive a
        # capability error and must never enter the lease/dispatch transaction.
        require_runtime_capability(role_id, runtime_info)
        now = utc_now()
        begin_immediate(self.session)
        try:
            self._authorize_claim(
                agent_id=agent_id, task_id=task_id, role_id=role_id,
                runtime_info=runtime_info, authorization_context=authorization_context,
            )
        except Exception:
            self.session.rollback()
            raise
        active_leases = len(
            self.session.exec(
                select(AgentTask).where(
                    AgentTask.assigned_agent_id == agent_id,
                    AgentTask.status == TaskStatus.LEASED,
                    AgentTask.lease_expires_at >= now,
                )
            ).all()
        )
        if active_leases >= self.max_inflight:
            self._touch_worker(
                agent_id,
                role_id=role_id,
                runtime_info=runtime_info,
                claim_status="AT_CAPACITY",
            )
            self.session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id=agent_id,
                    entity_type="agent_worker",
                    entity_id=agent_id,
                    action="claim_at_capacity",
                    metadata_json={
                        "active_leases": active_leases,
                        "max_inflight": self.max_inflight,
                    },
                )
            )
            self.session.commit()
            raise AgentCapacityError(agent_id, active_leases, self.max_inflight)
        # 角色隔离：无角色的 claim 只能领无角色约束的任务，不得退化为恒真过滤。
        role_filter = (
            AgentTask.required_role == ""
            if not role_id
            else or_(
                AgentTask.required_role == "",
                AgentTask.required_role == str(role_id),
            )
        )
        if task_id:
            candidate = self.session.get(AgentTask, task_id)
            eligible = (
                candidate is not None
                and (not candidate.content_id or is_current_revision(self.session, candidate))
                and candidate.task_type in capabilities
                and candidate.attempt < candidate.max_attempts
                and (candidate.not_before is None or as_utc(candidate.not_before) <= now)
                and (
                    candidate.status == TaskStatus.PENDING
                    or (
                        candidate.status == TaskStatus.LEASED
                        and candidate.lease_expires_at is not None
                        and as_utc(candidate.lease_expires_at) < now
                    )
                )
            )
            if eligible and role_id:
                eligible = candidate.required_role in {"", str(role_id)}
            elif eligible and not role_id:
                eligible = candidate.required_role == ""
            task = candidate if eligible else None
        else:
            query = (
                select(AgentTask)
                .where(
                    current_revision_clause(AgentTask),
                    AgentTask.task_type.in_(capabilities),
                    AgentTask.attempt < AgentTask.max_attempts,
                    role_filter,
                    or_(AgentTask.not_before.is_(None), AgentTask.not_before <= now),
                    (AgentTask.status == TaskStatus.PENDING)
                    | (
                        (AgentTask.status == TaskStatus.LEASED) & (AgentTask.lease_expires_at < now)
                    ),
                )
                .order_by(AgentTask.priority.desc(), AgentTask.created_at)
                .limit(1)
            )
            task = self.session.exec(query).first()
        if task is None:
            self._touch_worker(
                agent_id, role_id=role_id, runtime_info=runtime_info, claim_status="NO_TASK"
            )
            self.session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id=agent_id,
                    entity_type="agent_worker",
                    entity_id=agent_id,
                    action="claim_no_task",
                    metadata_json={"role_id": str(role_id or "")},
                )
            )
            self.session.commit()
            return None

        lease_token = issue_token()
        task.status = TaskStatus.LEASED
        task.assigned_agent_id = agent_id
        task.lease_token_hash = hash_secret(lease_token)
        if reserve_seconds is not None:
            # dispatch 预约路径：只给短预约租约，attempt 迁至 confirm_claim 时递增；
            # attempt_started_at 保持 None 作为「尚未确认」的标记。
            task.lease_expires_at = now + timedelta(seconds=max(1, reserve_seconds))
            task.attempt_started_at = None
        else:
            task.lease_expires_at = now + timedelta(
                minutes=lease_minutes_for_task(self.session, task, self.lease_minutes)
            )
            task.attempt_started_at = now
            task.attempt += 1
        task.started_at = task.started_at or now
        task.last_heartbeat_at = now
        task.not_before = None
        self.session.add(task)
        for offer in self.session.exec(
            select(SchedulerOffer).where(SchedulerOffer.task_id == task.id)
        ).all():
            self.session.delete(offer)
        self.session.add(
            AuditEvent(
                actor_type="agent",
                actor_id=agent_id,
                entity_type="agent_task",
                entity_id=task.id,
                action="claimed",
                metadata_json={"attempt": task.attempt, "reserved": reserve_seconds is not None},
            )
        )
        self._touch_worker(
            agent_id,
            task_id=task.id,
            role_id=role_id,
            runtime_info=runtime_info,
            claim_status="CLAIMED",
        )
        try:
            self._after_claim(task=task, authorization_context=authorization_context)
        except Exception:
            self.session.rollback()
            raise
        self.session.commit()
        self.session.refresh(task)
        return task, lease_token

    def require_lease(
        self,
        task_id: str,
        lease_token: str,
        *,
        renew: bool = False,
        implicit_confirm: bool = False,
        agent_id: str | None = None,
    ) -> AgentTask:
        """校验租约；可选自动续租与预约租约的隐式 confirm（V10 REQ-7）。

        - `renew`：已 confirm 的正式租约在任何持锁请求上顺带续至「现在 + 正式时长」，
          漏发 heartbeat 不再直接导致回收；
        - `implicit_confirm`：未 confirm 的预约租约收到工作请求时等同于显式 confirm-claim，
          预约租约不适用静默续租，防止 attempt 计数失真。
        bundle 与 context 下载属于预约阶段的准备动作，两项副作用均不开启。
        """
        task = self.session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        now = utc_now()
        if (
            task.status != TaskStatus.LEASED
            or task.lease_expires_at is None
            or as_utc(task.lease_expires_at) < now
            or task.lease_token_hash is None
            or not verify_secret(lease_token, task.lease_token_hash)
        ):
            raise TaskConflictError("Lease is missing, invalid, or expired")
        reserved = task.attempt_started_at is None
        if reserved and implicit_confirm:
            self.confirm_claim(
                task_id=task_id,
                lease_token=lease_token,
                agent_id=agent_id or task.assigned_agent_id or "",
                implicit=True,
            )
            return self.session.get(AgentTask, task_id)
        if renew and not reserved:
            task.lease_expires_at = now + timedelta(
                minutes=lease_minutes_for_task(self.session, task, self.lease_minutes)
            )
            self.session.add(task)
            self.session.commit()
            self.session.refresh(task)
        return task

    def confirm_claim(
        self,
        *,
        task_id: str,
        lease_token: str,
        agent_id: str,
        implicit: bool = False,
    ) -> AgentTask:
        """dispatch 预约确认：Agent 会话启动后把预约租约重算为正式时长。

        仅预约路径（attempt_started_at 为 None）递增 attempt；重复 confirm 幂等，
        只续期不叠加。传统 claim 路径的任务调用本方法同样只续期。
        `implicit=True` 表示由工作请求触发的隐式确认，后续行为与显式完全一致。
        """
        task = self.session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        now = utc_now()
        if (
            task.status != TaskStatus.LEASED
            or task.lease_expires_at is None
            or as_utc(task.lease_expires_at) < now
            or task.lease_token_hash is None
            or not verify_secret(lease_token, task.lease_token_hash)
        ):
            raise TaskConflictError("Lease is missing, invalid, or expired")
        if task.assigned_agent_id != agent_id:
            raise TaskConflictError("Task belongs to another agent")

        if task.content_id and task.task_type != TaskType.DOUYIN_PUBLISH:
            from glance_core.content_revision import is_current_revision

            if not is_current_revision(self.session, task):
                raise TaskConflictError("CONTENT_REVISION_STALE")
        confirmed = task.attempt_started_at is None
        if confirmed:
            task.attempt += 1
            task.attempt_started_at = now
        task.lease_expires_at = now + timedelta(
            minutes=lease_minutes_for_task(self.session, task, self.lease_minutes)
        )
        task.last_heartbeat_at = now
        self.session.add(task)
        if confirmed:
            self.session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id=agent_id,
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="claim_confirmed",
                    metadata_json={
                        "attempt": task.attempt,
                        "implicit": implicit,
                        "attempt_started_at": as_utc(task.attempt_started_at).isoformat(),
                        "lease_token_hash": task.lease_token_hash,
                    },
                )
            )
        self.session.commit()
        self.session.refresh(task)
        return task

    def heartbeat(
        self,
        task_id: str,
        lease_token: str,
        runtime_info: dict[str, Any] | None = None,
    ) -> AgentTask:
        task = self.require_lease(task_id, lease_token)
        renewed = self._apply_lease_heartbeat(task, now=utc_now(), runtime_info=runtime_info)
        self.session.commit()
        if not renewed:
            raise TaskConflictError("Execution deadline exceeded; lease was released")
        self.session.refresh(task)
        return task

    def _apply_lease_heartbeat(
        self, task: AgentTask, *, now, runtime_info=None,
        touch_worker: bool = True, deadline_actor_id: str = "lease-guard",
    ) -> bool:
        """Apply the shared lease policy after caller authorization, without committing.

        Trusted host callers must verify the current confirmed lease in their own
        write transaction. Only a worker-authenticated heartbeat touches its registry.
        False means the existing execution deadline released the lease.
        """
        lease_minutes = lease_minutes_for_task(self.session, task, self.lease_minutes)
        if task.attempt_started_at and now > as_utc(task.attempt_started_at) + timedelta(
            minutes=lease_minutes * 4
        ):
            task.status = (
                TaskStatus.PENDING if task.attempt < task.max_attempts else TaskStatus.FAILED
            )
            task.lease_token_hash = None
            task.lease_expires_at = None
            task.not_before = (
                now + timedelta(minutes=5) if task.status == TaskStatus.PENDING else None
            )
            self.session.add(task)
            self._record_publish_intent_state(
                task,
                status="INTERRUPTED",
                actor_id=deadline_actor_id,
                reason="EXECUTION_DEADLINE_EXCEEDED",
            )
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id=deadline_actor_id,
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="execution_deadline_exceeded",
                )
            )
            return False
        task.lease_expires_at = now + timedelta(minutes=lease_minutes)
        task.last_heartbeat_at = now
        self.session.add(task)
        if touch_worker and task.assigned_agent_id:
            self._touch_worker(
                task.assigned_agent_id,
                task_id=task.id,
                runtime_info=runtime_info,
            )
        return True

    def complete(
        self,
        *,
        task_id: str,
        lease_token: str,
        result: dict[str, Any],
        agent_id: str,
    ) -> AgentTask:
        # 幂等短路：提交成功但响应丢失后 Agent 会重试，同一结果直接返回成功，
        # 不重复写 submission/audit；不同结果仍走 require_lease 报 409。
        existing = self.session.get(AgentTask, task_id)
        if (
            existing is not None
            and existing.status == TaskStatus.COMPLETED
            and existing.assigned_agent_id == agent_id
        ):
            # Accepted retries retain proof of the original attempt, even after expiry.
            if existing.lease_token_hash is None or not verify_secret(lease_token, existing.lease_token_hash):
                raise TaskConflictError("Invalid accepted-attempt lease token")
            normalized = result
            try:
                model = result_model_for(existing.task_type, existing.schema_version)
                normalized = model.model_validate(result).model_dump(mode="json")
            except ValueError:
                pass
            accepted = self.session.exec(
                select(AgentSubmission)
                .where(
                    AgentSubmission.task_id == existing.id,
                    AgentSubmission.validation_status == "ACCEPTED",
                )
                .order_by(AgentSubmission.attempt.desc())
            ).first()
            if accepted is not None and accepted.result_hash == stable_hash(normalized):
                if (
                    existing.task_type == TaskType.DOUYIN_PUBLISH
                    and str(normalized.get("status") or "").upper() == "PUBLISHED"
                ):
                    self.validate_published_publish_result(
                        existing,
                        external_id=str(normalized.get("external_id") or ""),
                        external_url=str(normalized.get("external_url") or ""),
                        evidence_artifact_id=str(normalized.get("evidence_artifact_id") or ""),
                    )
                return existing
        task = self.require_lease(
            task_id, lease_token, renew=True, implicit_confirm=True, agent_id=agent_id
        )
        if task.assigned_agent_id != agent_id:
            raise TaskConflictError("Task belongs to another agent")
        if task.content_id and task.task_type != TaskType.DOUYIN_PUBLISH and not is_current_revision(self.session, task):
            raise TaskConflictError("CONTENT_REVISION_STALE")

        expected_subject = str((task.input_data or {}).get("_expected_subject_sha256") or "")
        if expected_subject and task.content_id:
            current_revision = self.session.exec(
                select(ContentRevision)
                .where(ContentRevision.content_id == task.content_id)
                .order_by(ContentRevision.created_at.desc())
            ).first()
            if current_revision is not None and current_revision.subject_sha256 != expected_subject:
                task.status = TaskStatus.CANCELLED
                task.error_code = "TASK_SNAPSHOT_STALE"
                task.error_message = "任务输入对应的内容版本已被人工返工替换"
                task.lease_token_hash = None
                task.lease_expires_at = None
                task.dedup_key = None
                self.session.add(task)
                self.session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="stale-result-guard",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="stale_result_rejected",
                        metadata_json={
                            "expected_subject_sha256": expected_subject,
                            "current_subject_sha256": current_revision.subject_sha256,
                        },
                    )
                )
                self.session.commit()
                raise TaskConflictError("TASK_SNAPSHOT_STALE")

        validator = Draft202012Validator(schema_for(task.task_type, task.schema_version))
        errors = [
            {"path": list(error.absolute_path), "message": error.message}
            for error in sorted(validator.iter_errors(result), key=lambda item: list(item.path))
        ]
        if not errors:
            try:
                model = result_model_for(task.task_type, task.schema_version)
                result = model.model_validate(result).model_dump(mode="json")
            except ValueError as exc:
                errors.append({"path": [], "message": str(exc)})
        if not errors:
            errors.extend(_source_media_priority_errors(self.session, task, result))
        if not errors and task.task_type == TaskType.SOURCE_DISCOVERY:
            for index, candidate in enumerate(result.get("candidates", [])):
                for field in ("url", "video_url"):
                    url = candidate.get(field, "")
                    if url and is_blocked_discovery_url(url):
                        errors.append(
                            {
                                "path": ["candidates", index, field],
                                "message": "必须返回最终内容页，不能返回搜索结果页或跳转链接",
                            }
                        )
        if not errors and task.task_type == TaskType.CONTENT_QC and task.schema_version in {"2", "3"}:
            from glance_core.content_acceptance import verify_acceptance

            try:
                verify_acceptance(self.session, task, result)
            except (ValueError, OSError, TypeError, KeyError) as exc:
                errors.append({"path": ["subject"], "message": str(exc)})
        if not errors and task.task_type == TaskType.ASSET_GENERATE_VIDEO:
            artifact = self.session.get(Artifact, result.get("artifact_id"))
            if (
                artifact is None
                or artifact.content_id != task.content_id
                or artifact.revision_id != task.revision_id
                or artifact.artifact_type != "agent_generated_video"
                or artifact.producer_stage != task.id
            ):
                errors.append(
                    {
                        "path": ["artifact_id"],
                        "message": "Artifact was not uploaded by this generation task",
                    }
                )
            if result.get("contains_text") or result.get("contains_watermark"):
                errors.append(
                    {
                        "path": ["artifact_id"],
                        "message": "生成视频含文字或水印，必须重新生成干净素材",
                    }
                )
        if not errors and task.task_type == TaskType.DOUYIN_PUBLISH:
            expected_platform = str(task.input_data.get("platform") or "").strip()
            if result.get("platform") != expected_platform:
                errors.append(
                    {
                        "path": ["platform"],
                        "message": f"发布平台必须与任务目标一致：{expected_platform}",
                    }
                )
            evidence_id = str(result.get("evidence_artifact_id") or "").strip()
            if evidence_id:
                evidence = self.session.get(Artifact, evidence_id)
                if (
                    evidence is None
                    or evidence.content_id != task.content_id
                    or evidence.revision_id != task.revision_id
                    or evidence.artifact_type != "publish_evidence"
                    or evidence.producer_stage != task.id
                ):
                    errors.append(
                        {
                            "path": ["evidence_artifact_id"],
                            "message": "发布证据必须由当前发布任务上传并登记",
                        }
                    )
            if result.get("status") == "PUBLISHED" and not evidence_id:
                errors.append(
                    {
                        "path": ["evidence_artifact_id"],
                        "message": "PUBLISHED 必须引用发布截图 Artifact",
                    }
                )
            if result.get("status") == "PUBLISHED":
                try:
                    self.validate_published_publish_result(task,
                        external_id=str(result.get("external_id") or ""),
                        external_url=str(result.get("external_url") or ""),
                        evidence_artifact_id=evidence_id)
                except TaskConflictError as exc:
                    errors.append({"path": ["status"], "message": str(exc)})
        if not errors and task.task_type == TaskType.ASSET_GENERATE_IMAGE:
            # V8：一任务多图。requests[] 与 images[] 按 segment_index 一一对应；
            # 旧式单 request 与手工任务降级为单段 requests 同法校验。
            requests_by_segment: dict[int, dict[str, Any]] = {
                int(entry.get("segment_index") or 0): dict(entry.get("asset_request") or {})
                for entry in task.input_data.get("requests", [])
            }
            if not requests_by_segment:
                legacy = task.input_data.get("request") or {
                    # Legacy/manual generation tasks used top-level dimensions.
                    "min_width": task.input_data.get("width", 1080),
                    "min_height": task.input_data.get("height", 1920),
                    "aspect_ratio": (
                        f"{task.input_data.get('width', 1080)}:"
                        f"{task.input_data.get('height', 1920)}"
                    ),
                }
                requests_by_segment = {int(task.input_data.get("segment_index") or 0): legacy}
            images = result.get("images", [])
            got_indexes = [int(image.get("segment_index") or 0) for image in images]
            if set(got_indexes) != set(requests_by_segment) or len(got_indexes) != len(
                set(got_indexes)
            ):
                errors.append(
                    {
                        "path": ["images"],
                        "message": (
                            "images[].segment_index 必须与下发的 requests[] 一一对应，"
                            f"期望 {sorted(requests_by_segment)}，实际 {sorted(got_indexes)}"
                        ),
                    }
                )
            for position, image in enumerate(images):
                artifact = self.session.get(Artifact, image.get("artifact_id"))
                if (
                    artifact is None
                    or artifact.content_id != task.content_id
                    or artifact.revision_id != task.revision_id
                    or artifact.artifact_type != "agent_generated_image"
                    or artifact.producer_stage != task.id
                ):
                    errors.append(
                        {
                            "path": ["images", position, "artifact_id"],
                            "message": "Artifact was not uploaded by this generation task",
                        }
                    )
                request = requests_by_segment.get(int(image.get("segment_index") or 0)) or {}
                if image.get("contains_text") or image.get("contains_watermark"):
                    errors.append(
                        {
                            "path": ["images", position, "artifact_id"],
                            "message": "生成图片含文字或水印，必须重新生成干净素材",
                        }
                    )
                width = int(image.get("width") or 0)
                height = int(image.get("height") or 0)
                minimum_width = int(request.get("min_width") or 1080)
                minimum_height = int(request.get("min_height") or 1920)
                if width < minimum_width or height < minimum_height:
                    errors.append(
                        {
                            "path": ["images", position, "width"],
                            "message": (
                                f"生成图尺寸至少为 {minimum_width} x {minimum_height}，"
                                f"实际为 {width} x {height}"
                            ),
                        }
                    )
                # 宽高比只要求方向一致：消费方 layout 对任意比例均能正确渲染，
                # 比值匹配无消费方；方向错误（横图交竖屏请求）仍拦下。
                ratio = str(request.get("aspect_ratio") or "")
                try:
                    ratio_width, ratio_height = (float(part) for part in ratio.split(":", 1))
                except (TypeError, ValueError):
                    ratio_width = ratio_height = 0
                if (
                    ratio_width > 0
                    and ratio_height > 0
                    and ratio_width != ratio_height
                    and width
                    and height
                    and (ratio_height > ratio_width) != (height > width)
                ):
                    errors.append(
                        {
                            "path": ["images", position, "width"],
                            "message": (
                                f"生成图方向必须与 {ratio} 一致"
                                f"（竖屏内容要求 height > width），"
                                f"实际为 {width} x {height}"
                            ),
                        }
                    )
        if (
            not errors
            and task.task_type == TaskType.AGENT_SELFTEST
            and result.get("echo_nonce") != task.input_data.get("nonce")
        ):
            errors.append({"path": ["echo_nonce"], "message": "必须原样回传 input.nonce"})
        if not errors and task.task_type == TaskType.SHORT_SCRIPT:
            allowed_claims = set(task.input_data.get("allowed_claim_ids", []))
            if not allowed_claims:
                research = task.input_data.get("research") or {}
                upstream_ids = task.input_data.get("upstream_task_ids") or {}
                upstream = upstream_ids.get("research") if isinstance(upstream_ids, dict) else None
                if isinstance(upstream, list):
                    upstream = upstream[0] if upstream else None
                upstream_task = self.session.get(AgentTask, upstream) if upstream else None
                if upstream_task is not None and upstream_task.result_data:
                    research = upstream_task.result_data
                if not isinstance(research, dict):
                    research = {}
                allowed_claims = {
                    str(item.get("id"))
                    for item in research.get("verified_claims", [])
                    if isinstance(item, dict) and item.get("id")
                }
            unknown = sorted(set(result.get("claims_used", [])) - allowed_claims)
            if unknown:
                errors.append(
                    {
                        "path": ["claims_used"],
                        "message": f"引用了未验证的 claim ID: {', '.join(unknown)}",
                    }
                )
            duration = float(result.get("estimated_duration_sec") or 0)
            minimum = float(task.input_data.get("min_video_seconds") or 20)
            maximum = float(task.input_data.get("max_video_seconds") or 90)
            if not minimum <= duration <= maximum:
                errors.append(
                    {
                        "path": ["estimated_duration_sec"],
                        "message": f"预估时长必须在 {minimum:g}-{maximum:g} 秒之间",
                    }
                )
        if not errors and task.task_type in {
            TaskType.SCRIPT_QC,
            TaskType.VISUAL_PLAN_QC,
            TaskType.ASSET_QC,
            TaskType.CONTENT_QC,
        }:
            error_issues = [
                issue for issue in result.get("issues", []) if issue.get("severity") == "error"
            ]
            if result.get("passed") and error_issues:
                errors.append(
                    {
                        "path": ["passed"],
                        "message": "存在 error 级问题时 passed 必须为 false",
                    }
                )
            if task.task_type == TaskType.ASSET_QC:
                indexes = {
                    int(item["segment_index"])
                    for item in task.input_data.get("assets", [])
                    if item.get("segment_index") is not None
                }
                for index, issue in enumerate(result.get("issues", [])):
                    segment_index = issue.get("segment_index")
                    if issue.get("severity") == "error" and segment_index is None:
                        errors.append(
                            {
                                "path": ["issues", index, "segment_index"],
                                "message": "素材错误必须指明 segment_index，才能定向重生",
                            }
                        )
                    elif segment_index is not None and int(segment_index) not in indexes:
                        errors.append(
                            {
                                "path": ["issues", index, "segment_index"],
                                "message": "segment_index 不在本次待审素材中",
                            }
                        )
        if not errors and task.task_type == TaskType.VISUAL_PLAN:
            segments = result.get("segments", [])
            asset_image_enabled = bool(task.input_data.get("asset_image_enabled"))
            if not asset_image_enabled and any(
                segment.get("asset_request") for segment in segments
            ):
                errors.append(
                    {
                        "path": ["segments"],
                        "message": "AI 生图管线当前关闭，不能提交 asset_request",
                    }
                )
            errors.extend(_generated_segment_errors(task, segments))
            duration = float(task.input_data.get("voice_duration_sec") or 0)
            scenes = {
                str(scene.get("scene_id")): scene for scene in task.input_data.get("scenes", [])
            }
            allowed_asset_ids = {
                str(asset_id) for asset_id in task.input_data.get("allowed_asset_ids", [])
            }
            layouts = set(task.input_data.get("allowed_layouts", []))
            cursor = 0.0
            for index, segment in enumerate(segments):
                start = float(segment["narration_start_sec"])
                end = float(segment["narration_end_sec"])
                if abs(start - cursor) > 0.25 or end <= start:
                    errors.append(
                        {
                            "path": ["segments", index],
                            "message": "画面时间轴必须首尾相接且结束时间大于开始时间",
                        }
                    )
                cursor = end
                scene_id = segment.get("scene_id")
                asset_id = segment.get("asset_id")
                if scene_id and scene_id not in scenes:
                    errors.append(
                        {"path": ["segments", index, "scene_id"], "message": "scene_id 不存在"}
                    )
                if asset_id and asset_id not in allowed_asset_ids:
                    errors.append(
                        {
                            "path": ["segments", index, "asset_id"],
                            "message": "asset_id 不在可用素材白名单",
                        }
                    )
                if segment.get("layout") not in layouts:
                    errors.append(
                        {"path": ["segments", index, "layout"], "message": "layout 不在白名单"}
                    )
                scene = scenes.get(scene_id)
                if scene and segment.get("source_start_sec") is not None:
                    source_start = float(segment["source_start_sec"])
                    source_end = float(segment.get("source_end_sec") or 0)
                    if (
                        source_end <= source_start
                        or source_start < float(scene.get("start_sec") or 0)
                        or source_end > float(scene.get("end_sec") or 0)
                    ):
                        errors.append(
                            {
                                "path": ["segments", index, "source_start_sec"],
                                "message": "来源区间必须落在所选场景内",
                            }
                        )
            if abs(cursor - duration) > 0.25:
                errors.append({"path": ["segments"], "message": "画面计划必须覆盖完整配音时长"})

        submission_dir = self.submission_root / task.id / str(task.attempt)
        submission_dir.mkdir(parents=True, exist_ok=True)
        raw_path = submission_dir / "result.json"
        raw_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        submission = AgentSubmission(
            task_id=task.id,
            agent_id=agent_id,
            attempt=task.attempt,
            raw_result_path=str(raw_path),
            result_hash=stable_hash(result),
            validation_status="REJECTED" if errors else "ACCEPTED",
            validation_errors=errors,
        )
        self.session.add(submission)

        if errors:
            # 同一 attempt 内未超限时保留租约：Agent 用原 token 在剩余租约期内直接重提；
            # 到期由 reap_expired_leases 回收，不新增配置项与字段。
            rejected_in_attempt = len(
                self.session.exec(
                    select(AgentSubmission).where(
                        AgentSubmission.task_id == task.id,
                        AgentSubmission.attempt == task.attempt,
                        AgentSubmission.validation_status == "REJECTED",
                    )
                ).all()
            )
            if rejected_in_attempt >= MAX_VALIDATION_FAILURES_PER_ATTEMPT:
                task.status = (
                    TaskStatus.PENDING if task.attempt < task.max_attempts else TaskStatus.REJECTED
                )
                task.lease_token_hash = None
                task.lease_expires_at = None
                task.not_before = (
                    utc_now() + timedelta(minutes=2) if task.status == TaskStatus.PENDING else None
                )
                if task.status == TaskStatus.REJECTED:
                    task.dedup_key = None
                    task.error_code = "TASK_CONTRACT_MISMATCH"
                    task.error_message = (
                        "结果多次未通过服务端 Schema 或业务契约校验，请进入 Recovery 修复"
                    )
            self.session.add(task)
            self.session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id=agent_id,
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="validation_rejected",
                    metadata_json={
                        "attempt": task.attempt,
                        "error_count": len(errors),
                        "failures_in_attempt": rejected_in_attempt,
                        "lease_kept": task.status == TaskStatus.LEASED,
                        "retryable": task.status != TaskStatus.REJECTED,
                    },
                )
            )
            self._touch_worker(agent_id, task_id=task.id, failed=True)
            self.session.commit()
            raise TaskValidationError(errors)

        task.result_data = result
        task.error_code = None
        task.error_message = None
        task.status = TaskStatus.COMPLETED
        task.completed_at = utc_now()
        # Retain only the hash for authenticated idempotent retries; COMPLETED is not leaseable.
        task.lease_expires_at = None
        self.session.add(task)
        self.session.add(
            AuditEvent(
                actor_type="agent",
                actor_id=agent_id,
                entity_type="agent_task",
                entity_id=task.id,
                action="completed",
                metadata_json={"result_hash": submission.result_hash},
            )
        )
        self._touch_worker(agent_id, task_id=task.id, completed=True)
        self.session.commit()
        self.session.refresh(task)
        return task

    def fail(
        self,
        *,
        task_id: str,
        lease_token: str,
        agent_id: str,
        retryable: bool,
        code: str,
        message: str,
        attempt: int | None = None,
    ) -> AgentTask:
        existing = self.session.get(AgentTask, task_id)
        if existing is not None and existing.content_id and not is_current_revision(self.session, existing):
            # Reject before both the lease path and its late-report fallback;
            # neither may rewrite a previous published revision's task or intent.
            raise TaskConflictError("CONTENT_REVISION_STALE")
        try:
            task = self.require_lease(
                task_id,
                lease_token,
                renew=code != "LEASE_EXPIRED",
                implicit_confirm=code != "LEASE_EXPIRED",
                agent_id=agent_id,
            )
        except TaskConflictError:
            # 租约已失效时放宽：assigned_agent_id 与 attempt 均匹配则只记录失败原因
            # 与审计，不改任务状态也不回 409；失败信息任何情况下都能上报。
            task = self.session.get(AgentTask, task_id)
            if (
                task is None
                or task.assigned_agent_id != agent_id
                or attempt is None
                or task.attempt != attempt
            ):
                raise
            if (
                code == "LEASE_EXPIRED"
                and task.status == TaskStatus.LEASED
                and (task.lease_expires_at is None or as_utc(task.lease_expires_at) >= utc_now())
            ):
                # Failed token validation must not turn a live lease into a late report.
                raise
            task.error_code = code
            task.error_message = message
            self.session.add(task)
            self._record_publish_intent_state(
                task,
                status="INTERRUPTED",
                actor_id="lease-reporter",
                reason=code,
            )
            self.session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id=agent_id,
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="failed",
                    metadata_json={
                        "error_code": code,
                        "retryable": retryable,
                        "next_status": task.status.value,
                        "message": message[:200],
                        "late_report": True,
                    },
                )
            )
            self._touch_worker(agent_id, task_id=task.id)
            self.session.commit()
            self.session.refresh(task)
            return task
        if task.assigned_agent_id != agent_id:
            raise TaskConflictError("Task belongs to another agent")
        if code == "LEASE_EXPIRED":
            raise TaskConflictError(
                "租约仍有效，拒绝 LEASE_EXPIRED 上报；"
                f"截止 {as_utc(task.lease_expires_at).isoformat()} UTC，"
                f"当前 {utc_now().isoformat()} UTC；请使用 heartbeat 或提交结果"
            )
        task.error_code = code
        task.error_message = message
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.status = (
            TaskStatus.PENDING
            if retryable and task.attempt < task.max_attempts
            else TaskStatus.FAILED
        )
        if task.status == TaskStatus.PENDING:
            delays = (5, 15, 45)
            task.not_before = utc_now() + timedelta(
                minutes=delays[min(max(task.attempt - 1, 0), len(delays) - 1)]
            )
        if task.status == TaskStatus.FAILED:
            task.dedup_key = None
        self.session.add(task)
        self._record_publish_intent_state(
            task,
            status="INTERRUPTED",
            actor_id=agent_id,
            reason=code,
        )
        self.session.add(
            AuditEvent(
                actor_type="agent",
                actor_id=agent_id,
                entity_type="agent_task",
                entity_id=task.id,
                action="failed",
                metadata_json={
                    "error_code": code,
                    "retryable": retryable,
                    "next_status": task.status.value,
                    "message": message[:200],
                },
            )
        )
        self._touch_worker(agent_id, task_id=task.id, failed=True)
        self.session.commit()
        self.session.refresh(task)
        return task

    def cancel(
        self, task_id: str, *, actor_id: str = "admin", actor_type: str = "human"
    ) -> AgentTask:
        task = self.session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status not in {TaskStatus.PENDING, TaskStatus.LEASED}:
            raise TaskConflictError(f"Cannot cancel a task in status {task.status.value}")
        task.status = TaskStatus.CANCELLED
        task.dedup_key = None
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.not_before = None
        self.session.add(task)
        self._record_publish_intent_state(
            task,
            status="INTERRUPTED",
            actor_id=actor_id,
            reason="cancelled",
        )
        self.session.add(
            AuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                entity_type="agent_task",
                entity_id=task.id,
                action="cancelled",
            )
        )
        self.session.commit()
        self.session.refresh(task)
        return task

    def check_requeue(self, task_id: str, *, allow_attempt_extension: bool = True) -> AgentTask:
        """Read-only eligibility shared by manual requeue and bounded Recovery."""
        task = self.session.get(AgentTask, task_id)
        if task is None:
            raise KeyError(task_id)
        if task.content_id and not is_current_revision(self.session, task):
            raise TaskConflictError("CONTENT_REVISION_STALE")
        if task.status not in {TaskStatus.FAILED, TaskStatus.REJECTED, TaskStatus.CANCELLED}:
            raise TaskConflictError(f"Cannot requeue a task in status {task.status.value}")
        if task.status == TaskStatus.REJECTED:
            raise TaskConflictError(
                "该任务多次未通过服务端契约校验，不能重派原始输入；请进入 Recovery 修复"
            )
        if is_non_retryable_task_error(task.error_code):
            raise TaskConflictError(
                "该任务属于环境、任务契约或依赖失败，不能重派原始输入；请进入 Recovery 修复"
            )
        latest_failure = self.session.exec(
            select(AuditEvent)
            .where(
                AuditEvent.entity_type == "agent_task",
                AuditEvent.entity_id == task.id,
                AuditEvent.action == "failed",
            )
            .order_by(AuditEvent.created_at.desc())
        ).first()
        if latest_failure is not None and latest_failure.metadata_json.get("retryable") is False:
            raise TaskConflictError(
                "该任务已被 Agent 标记为不可重试，不能复用原始输入；请进入 Recovery 修复"
            )
        if not allow_attempt_extension and task.attempt >= task.max_attempts:
            raise TaskConflictError("原任务尝试预算已耗尽，Recovery 不能扩展 max_attempts")
        return task

    def requeue(
        self, task_id: str, *, actor_id: str = "admin", allow_attempt_extension: bool = True
    ) -> AgentTask:
        task = self.check_requeue(task_id, allow_attempt_extension=allow_attempt_extension)
        task.status = TaskStatus.PENDING
        task.error_code = None
        task.error_message = None
        task.assigned_agent_id = None
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.dedup_key = task_dedup_key(
            task_type=task.task_type,
            content_id=task.content_id,
            input_hash=task.input_hash,
            instructions=task.instructions,
            prompt_version=task.prompt_version,
            schema_version=task.schema_version,
        )
        if task.attempt >= task.max_attempts:
            task.max_attempts = task.attempt + 3
        self.session.add(task)
        self._record_publish_intent_state(
            task,
            status="RECONCILE_REQUIRED",
            actor_id=actor_id,
            reason="requeued",
        )
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="agent_task",
                entity_id=task.id,
                action="requeued",
                metadata_json={"max_attempts": task.max_attempts},
            )
        )
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise TaskConflictError("相同输入的任务已经在队列中或已完成，不能重复派发") from exc
        self.session.refresh(task)
        return task

    def reap_expired_leases(self) -> int:
        now = utc_now()
        expired = self.session.exec(
            select(AgentTask).where(
                AgentTask.status == TaskStatus.LEASED,
                AgentTask.lease_expires_at < now,
            )
        ).all()
        for task in expired:
            reserved = task.attempt_started_at is None
            task.lease_token_hash = None
            task.lease_expires_at = None
            task.status = (
                TaskStatus.PENDING if task.attempt < task.max_attempts else TaskStatus.FAILED
            )
            if task.status == TaskStatus.PENDING:
                if reserved:
                    # dispatch 预约未确认：不烧重试预算也不退避，立即回到可领取。
                    task.not_before = None
                else:
                    delays = (5, 15, 45)
                    task.not_before = now + timedelta(
                        minutes=delays[min(max(task.attempt - 1, 0), len(delays) - 1)]
                    )
            if task.status == TaskStatus.FAILED:
                task.dedup_key = None
                task.error_code = task.error_code or "LEASE_EXPIRED"
                task.error_message = task.error_message or (
                    "Lease expired with no submission and no attempts left"
                )
            self.session.add(task)
            self._record_publish_intent_state(
                task,
                status="INTERRUPTED",
                actor_id="lease-reaper",
                reason="LEASE_EXPIRED",
            )
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="lease-reaper",
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="lease_expired",
                    metadata_json={"next_status": task.status.value},
                )
            )
        if expired:
            self.session.commit()
        return len(expired)

    def _touch_worker(
        self,
        agent_id: str,
        *,
        task_id: str | None = None,
        completed: bool = False,
        failed: bool = False,
        role_id: AgentRole | str | None = None,
        runtime_info: dict[str, Any] | None = None,
        claim_status: str | None = None,
    ) -> None:
        worker = self.session.get(AgentWorker, agent_id) or AgentWorker(agent_id=agent_id)
        worker.last_seen_at = utc_now()
        if role_id:
            worker.role_id = AgentRole(role_id).value
        if runtime_info:
            incoming_device = str(runtime_info.get("device_label") or "")
            if worker.device_label and incoming_device and worker.device_label != incoming_device:
                self.session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="agent-registry",
                        entity_type="agent_worker",
                        entity_id=agent_id,
                        action="agent_id_device_conflict",
                        metadata_json={
                            "known": worker.device_label,
                            "incoming": incoming_device,
                        },
                    )
                )
            for field in (
                "app_name",
                "app_version",
                "model_name",
                "model_tier",
                "reasoning_effort",
                "skill_name",
                "skill_version",
                "device_label",
                "protocol_version",
                "self_check",
            ):
                value = runtime_info.get(field)
                if value not in (None, "", [], {}):
                    setattr(worker, field, value)
        if task_id:
            worker.last_task_id = task_id
        if claim_status:
            worker.last_claim_status = claim_status
        worker.tasks_completed += int(completed)
        worker.tasks_failed += int(failed)
        self.session.add(worker)

    def _record_duplicate(self, task: AgentTask) -> None:
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="task-service",
                entity_type="agent_task",
                entity_id=task.id,
                action="duplicate_suppressed",
                metadata_json={"status": task.status.value, "dedup_key": task.dedup_key},
            )
        )
        self.session.commit()
        self.session.refresh(task)

    def _active_lane_task(self, lane_key: str) -> AgentTask | None:
        return self.session.exec(
            select(AgentTask).where(
                AgentTask.lane_key == lane_key,
                AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
            )
        ).first()
