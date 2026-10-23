"""Extracted Glance bundle contract; keep lineage and file evidence checks intact."""

import hashlib
import json
import logging
from math import isfinite
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from glance_core.content_acceptance import (
    _owned_visual_chain,
    _unchanged_material_task,
    _visual_parent_chain,
    generated_image_material,
    source_context,
    visual_review_source_context,
    resolved_visual_plan,
    verify_visual_joint_evidence,
)
from glance_core.models import (
    AgentTask,
    AgentSubmission,
    Content,
    TaskStatus,
    TaskType,
    ProductionMode,
    AuditEvent,
    Artifact,
)
from glance_core.prompts import resolve_instructions
from glance_core.task_schemas import schema_for
from glance_core.task_service import AgentTaskService, TaskConflictError, script_source_media_context, stable_hash
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

logger = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


_VIDEO_CONTEXT_SUFFIXES = {".mp4", ".webm", ".mov", ".mkv", ".m4v"}
_VIDEO_CONTEXT_TASKS = frozenset(
    {
        TaskType.DOUYIN_PUBLISH,
        TaskType.PUBLISH_PREPARE,
        TaskType.PUBLISH_REVIEW_ASSIST,
        TaskType.ASSET_GENERATE_VIDEO,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
    }
)


def _merge_upstream_results(session: Session, task_ids: list[str]) -> dict | None:
    """把多个上游子任务的结果合并为一份（REQ-9 分批场景分析）。

    列表字段按递交顺序拼接，标量字段取第一份；任一批次未完成则返回 None。
    """
    merged: dict[str, Any] = {}
    for task_id in task_ids:
        upstream = session.get(AgentTask, task_id)
        if upstream is None or upstream.status != TaskStatus.COMPLETED:
            return None
        for key, value in (upstream.result_data or {}).items():
            if isinstance(value, list):
                merged.setdefault(key, [])
                merged[key].extend(value)
            else:
                merged.setdefault(key, value)
    return merged or None


def _compact_scene_analysis(value: Any) -> dict:
    """Keep script/visual planning inputs bounded without dropping scene identity.

    Scene analysis is a schema-bounded list, but an Agent may return very verbose
    descriptions. Passing those verbatim into every downstream script/visual task
    multiplies input tokens and was the source of the 90k-character rewrite task.
    Preserve every scene and score while bounding only free-form prose.
    """
    if not isinstance(value, dict):
        return {"scenes": []}
    scenes = value.get("scenes")
    if not isinstance(scenes, list):
        return {"scenes": []}
    compact: list[dict[str, Any]] = []
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        compact.append(
            {
                "scene_id": str(scene.get("scene_id") or ""),
                "description": str(scene.get("description") or "")[:360],
                "visual_quality": scene.get("visual_quality", 0),
                "information_value": scene.get("information_value", 0),
                "recommended_usage": str(scene.get("recommended_usage") or "")[:240],
            }
        )
    return {"scenes": compact}


def _context_manifest(
    root: Path, relative_paths: list[str], scenes: list[dict]
) -> list[dict]:
    """任务包素材清单：文件名、字节数、内容哈希与 input 字段的对应关系。

    CLI 下载完毕后逐项预检，缺图不得静默降级（BUG-9）。
    """
    scene_by_thumbnail = {
        str(scene.get("thumbnail") or "").replace("\\", "/"): str(
            scene.get("scene_id") or ""
        )
        for scene in scenes
        if scene.get("thumbnail")
    }
    manifest: list[dict] = []
    for index, relative_path in enumerate(relative_paths):
        target = (root / str(relative_path)).resolve()
        entry: dict[str, Any] = {
            "index": index,
            "name": Path(str(relative_path)).name,
            "input_ref": scene_by_thumbnail.get(
                str(relative_path).replace("\\", "/"), ""
            ),
        }
        if root in target.parents and target.is_file():
            entry["size_bytes"] = target.stat().st_size
            entry["sha256"] = sha256_file(target)
        else:
            entry["size_bytes"] = 0
            entry["sha256"] = ""
            entry["missing"] = True
        manifest.append(entry)
    return manifest


def _filter_context_files(task_type: TaskType, relative_paths: list[str]) -> list[str]:
    """Review/publish must receive the actual video; planning tasks use frames."""
    if task_type in _VIDEO_CONTEXT_TASKS:
        return relative_paths
    return [
        path
        for path in relative_paths
        if Path(path).suffix.casefold() not in _VIDEO_CONTEXT_SUFFIXES
    ]


def _scene_context_files(scenes: list[dict[str, Any]]) -> list[str]:
    """从场景清单恢复视觉任务需要下载到 workspace/context 的缩略图。"""
    paths: list[str] = []
    seen: set[str] = set()
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        relative_path = str(scene.get("thumbnail") or "").replace("\\", "/").strip()
        if relative_path and relative_path not in seen:
            seen.add(relative_path)
            paths.append(relative_path)
    return paths


def _has_bundle_value(value: Any) -> bool:
    return value not in (None, "", [], {})


def _bundle_contract_errors(
    session: Session,
    task: AgentTask,
    bundle_input: dict[str, Any],
    context_files: list[str],
    context_manifest: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate the final materialized bundle before an Agent spends a turn on it."""
    errors: list[dict[str, Any]] = []
    content = session.get(Content, task.content_id) if task.content_id else None
    if task.task_type == TaskType.CONTENT_QC and task.schema_version == "3":
        try:
            evidence = task.input_data["visual_evidence"]
            root = Path(content.content_dir).resolve()
            plan = session.get(AgentTask, task.input_data["upstream_task_ids"]["visual_plan"])
            verify_visual_joint_evidence(root, evidence, None, task.input_data["audio_evidence"],
                                         resolved_visual_plan(session, plan), passed=False)
            pack_root = (root / evidence["manifest_path"]).parent
            required = {evidence["manifest_path"], *[
                (pack_root / item["path"]).relative_to(root).as_posix()
                for item in [*evidence["manifest"]["pages"], *evidence["manifest"]["frames"],
                             evidence["manifest"]["subtitles"]]]}
            if not required.issubset(set(context_files)):
                raise ValueError("VISUAL_EVIDENCE_CONTEXT_INCOMPLETE")
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            errors.append({"path": ["visual_evidence"], "message": str(exc)})
    declared_upstream = task.input_data.get("upstream_task_ids") or {}
    # Some pre-contract tasks were intentionally minimal bundle-materialization records.
    # Enforce the full script contract when its dependency/mode fields are declared, while
    # still allowing those legacy bundles to be served for context migration.
    legacy_scene_only_script = (
        task.task_type == TaskType.SHORT_SCRIPT
        and set(declared_upstream) == {"scene_analysis"}
        and "research" not in task.input_data
    )
    short_script_contract_declared = task.task_type != TaskType.SHORT_SCRIPT or not (
        legacy_scene_only_script
    )

    def require(field: str, message: str) -> None:
        if not _has_bundle_value(bundle_input.get(field)):
            errors.append({"path": [field], "message": message})

    if task.task_type in {
        TaskType.CONTENT_RESEARCH,
        TaskType.VISUAL_PLAN,
    } or (task.task_type == TaskType.SHORT_SCRIPT and short_script_contract_declared):
        mode = str(bundle_input.get("production_mode") or "")
        if mode not in {ProductionMode.FOOTAGE.value, ProductionMode.IMAGE_ONLY.value}:
            errors.append(
                {
                    "path": ["production_mode"],
                    "message": "production_mode 必须是 footage 或 image_only",
                }
            )
        elif content is not None and mode != content.production_mode.value:
            errors.append(
                {
                    "path": ["production_mode"],
                    "message": (
                        f"production_mode 与内容控制面不一致，期望 {content.production_mode.value}"
                    ),
                }
            )

    if task.task_type == TaskType.CONTENT_RESEARCH:
        require("candidate", "事实研究缺少候选来源")
    elif task.task_type == TaskType.SCENE_ANALYZE:
        require("scenes", "场景分析缺少 scenes 清单")
        if bundle_input.get("scenes") and not context_files:
            errors.append(
                {"path": ["context_files"], "message": "场景分析缺少场景缩略图上下文"}
            )
    elif task.task_type == TaskType.SHORT_SCRIPT:
        if short_script_contract_declared:
            require("research", "短脚本缺少已完成的事实研究")
        if (
            short_script_contract_declared
            and bundle_input.get("production_mode") == ProductionMode.FOOTAGE.value
        ) or "scene_analysis" in declared_upstream:
            require("scene_analysis", "短脚本声明的场景分析不可用")
    elif task.task_type == TaskType.SCRIPT_QC:
        require("script", "脚本质检缺少脚本结果")
        require("research", "脚本质检缺少事实研究")
    elif task.task_type == TaskType.SCRIPT_REVIEW_ASSIST:
        require("script", "脚本审核辅助缺少脚本结果")
        require("script_qc", "脚本审核辅助缺少脚本质检结果")
        require("research", "脚本审核辅助缺少事实研究")
    elif task.task_type == TaskType.VISUAL_PLAN:
        require("script", "画面计划缺少脚本结果")
        if bundle_input.get("production_mode") == ProductionMode.FOOTAGE.value:
            require("scene_analysis", "原片模式画面计划缺少场景分析")
    elif task.task_type == TaskType.VISUAL_PLAN_QC and (
        "visual_plan" in task.input_data or "visual_plan" in declared_upstream
    ):
        require("visual_plan", "画面计划质检缺少画面计划结果")

    for entry in context_manifest:
        if entry.get("missing"):
            errors.append(
                {
                    "path": ["context_files", entry.get("index", 0)],
                    "message": f"任务包素材不存在：{entry.get('name') or 'unknown'}",
                }
            )
    return errors


def _is_handsfree_publish_policy(policy: object) -> bool:
    return (
        isinstance(policy, dict)
        and policy.get("initiated_by") == "handsfree"
        and policy.get("user_clicked") is False
        and policy.get("explicit_user_opt_in") is True
    )


def _publish_instruction_mode(task: AgentTask, session: Session) -> bool | None:
    if task.task_type == TaskType.DOUYIN_PUBLISH:
        return _is_handsfree_publish_policy(task.input_data.get("publish_policy"))
    if task.task_type == TaskType.PUBLISH_REVIEW_ASSIST:
        from glance_core.content_acceptance import is_handsfree_content
        from glance_core.workflow import handsfree_review_policy

        return bool(stable_hash(task.input_data) == task.input_hash
            and _is_handsfree_publish_policy(task.input_data.get("publish_policy"))
            and stable_hash(task.input_data.get("publish_policy")) == stable_hash(handsfree_review_policy())
            and task.content_id and is_handsfree_content(session, task.content_id))
    return None


def scene_evidence_context(
    task: AgentTask, session: Session, flow: WorkflowService
) -> list[tuple[str, bytes]]:
    # Reuse the leased context download contract, not a task-ID read API.
    if stable_hash(task.input_data) != task.input_hash:
        return []
    owner = None
    if task.task_type == TaskType.RECOVERY:
        failure = task.input_data.get("failure") or {}
        ref = failure.get("source_task_id")
        owner = session.get(AgentTask, ref) if isinstance(ref, str) else None
        if owner and (
            failure.get("source_task_type", owner.task_type.value)
            != owner.task_type.value
            or not (
                owner.status == TaskStatus.FAILED
                or (
                    owner.status == TaskStatus.COMPLETED
                    and (owner.result_data or {}).get("passed") is False
                )
            )
        ):
            owner = None
    elif task.task_type in {TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST}:
        ref = (task.input_data.get("upstream_task_ids") or {}).get("script")
        owner = session.get(AgentTask, ref) if isinstance(ref, str) else None
        if owner and (
            owner.task_type != TaskType.SHORT_SCRIPT
            or owner.status != TaskStatus.COMPLETED
        ):
            owner = None
    if (
        owner is None
        or owner.content_id != task.content_id
        or owner.task_type == TaskType.RECOVERY
        or stable_hash(owner.input_data) != owner.input_hash
        or owner.input_data.get("production_mode") == "image_only"
    ):
        return []
    context = flow._recovery_current_input(owner, full_scenes=True)
    scenes = context.get("scene_analysis", {}).get("scenes", [])
    if not scenes:
        return []
    payload = {
        "scene_analysis_sources": context["scene_analysis_sources"],
        "scene_analysis": {"scenes": scenes},
        "evidence_gaps": [
            gap for gap in context.get("evidence_gaps", []) if gap.startswith("scene")
        ],
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return [(f"scene-evidence-{hashlib.sha256(data).hexdigest()[:12]}.json", data)]


def fail_bundle_preflight(
    service: AgentTaskService,
    task: AgentTask,
    lease_token: str,
    errors: list[dict[str, Any]],
    flow: WorkflowService,
    *,
    failure_recovery,
) -> None:
    """Stop a malformed leased task and start its Recovery chain server-side."""
    if not callable(failure_recovery):
        raise ValueError("MPT_FAILURE_RECOVERY_HANDLER_REQUIRED")
    message = "；".join(
        f"{'.'.join(str(part) for part in error.get('path', []))}: {error.get('message', '')}"
        for error in errors[:30]
    )[:4000]
    agent_id = task.assigned_agent_id
    if not agent_id:
        raise HTTPException(
            status_code=409, detail="任务租约缺少 Agent 身份，无法安全预检"
        )
    try:
        failed = service.fail(
            task_id=task.id,
            lease_token=lease_token,
            agent_id=agent_id,
            retryable=False,
            code=(
                "ENVIRONMENT_MISSING_CONTEXT"
                if any("context_files" in error.get("path", []) for error in errors)
                else "ENVIRONMENT_MISSING_INPUT"
            ),
            message=message,
            attempt=task.attempt,
        )
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    try:
        recovery_id = failure_recovery(failed.id, validation_errors=errors)["recovery_task_id"]
    except Exception:
        # The terminal failure is already committed; never undo it on admission failure.
        logger.exception("Bundle preflight failure pending recovery: %s", failed.id)
        recovery_id = None
    raise HTTPException(
        status_code=409,
        detail={
            "code": failed.error_code,
            "message": message,
            "recovery_task_id": recovery_id,
        },
    )


def build_bundle(
    task_id: str,
    token: str,
    service: AgentTaskService,
    session: Session,
    flow: WorkflowService,
    *,
    failure_recovery,
) -> dict:
    if not callable(failure_recovery):
        raise ValueError("MPT_FAILURE_RECOVERY_HANDLER_REQUIRED")
    try:
        task = service.require_lease(task_id, token)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    current_instructions = resolve_instructions(
        session,
        task.task_type,
        handsfree=_publish_instruction_mode(task, session),
        schema_version=task.schema_version,
        base_settings=flow.base_settings,
        opening_identity_overlay=flow.opening_identity_overlay,
    )
    bundle_input = dict(task.input_data)
    overlay = flow.opening_identity_input(task.content_id or "", task.task_type, task.input_data)
    if "opening_identity_overlay" in task.input_data:
        if task.input_data["opening_identity_overlay"] != overlay:
            raise HTTPException(status_code=409, detail="Opening identity execution evidence changed")
    elif overlay:
        bundle_input["opening_identity_overlay_read_only"] = overlay
    if task.task_type == TaskType.RECOVERY:
        failure = task.input_data.get("failure") or {}
        source_id = failure.get("source_task_id") if isinstance(failure, dict) else None
        source = (
            session.get(AgentTask, source_id) if isinstance(source_id, str) else None
        )
        bundle_input["current_input"] = {}
        if (
            source is not None
            and source.content_id == task.content_id
            and source.task_type != TaskType.RECOVERY
            and failure.get("source_task_type", source.task_type.value)
            == source.task_type.value
        ):
            context = flow._recovery_current_input(source)
            context["current_contract"] = {
                "source_task_id": source.id,
                "source_task_type": source.task_type.value,
                "instructions": resolve_instructions(
                    session,
                    source.task_type,
                    schema_version=source.schema_version,
                    base_settings=flow.base_settings,
                    opening_identity_overlay=flow.opening_identity_overlay,
                    handsfree=_publish_instruction_mode(source, session),
                ),
            }
            bundle_input["current_input"] = context
    for field, upstream_id in bundle_input.pop("upstream_task_ids", {}).items():
        if field == "scene_analysis" and task.task_type == TaskType.SHORT_SCRIPT:
            ids = upstream_id if isinstance(upstream_id, list) else [upstream_id]
            rows = [
                session.get(AgentTask, ref) if isinstance(ref, str) else None
                for ref in ids
            ]
            bundle_input.pop("scene_analysis", None)
            bundle_input.pop("visual_evidence_gaps", None)
            if not rows or any(
                row is None
                or row.content_id != task.content_id
                or row.task_type != TaskType.SCENE_ANALYZE
                or row.status != TaskStatus.COMPLETED
                for row in rows
            ):
                bundle_input["visual_evidence_gaps"] = [
                    "scene_analysis: exact completed scene evidence unavailable"
                ]
                continue
            scenes = []
            missing_timings = 0
            content = session.get(Content, task.content_id) if task.content_id else None
            image_only = (
                bundle_input.get("production_mode")
                or (content.production_mode.value if content else None)
            ) == ProductionMode.IMAGE_ONLY.value
            for row in rows:
                timings = {
                    scene.get("scene_id"): scene
                    for scene in row.input_data.get("scenes", [])
                    if isinstance(scene, dict)
                    and isinstance(scene.get("scene_id"), str)
                }
                for scene in _compact_scene_analysis(row.result_data)["scenes"]:
                    source = timings.get(scene["scene_id"], {})
                    timing = {
                        key: source.get(key)
                        for key in ("start_sec", "end_sec", "duration_sec")
                    }
                    valid = all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and isfinite(value)
                        and value >= 0
                        for value in timing.values()
                    )
                    # The executor rounds all three fields to milliseconds.
                    if (
                        not image_only
                        and valid
                        and (
                            timing["start_sec"] < timing["end_sec"]
                            and timing["duration_sec"] > 0
                            and abs(
                                round(
                                    timing["duration_sec"]
                                    - (timing["end_sec"] - timing["start_sec"]),
                                    3,
                                )
                            )
                            <= 0.001
                        )
                    ):
                        scene.update(timing)
                    elif not image_only:
                        missing_timings += 1
                    scenes.append(scene)
            bundle_input["scene_analysis"] = {"scenes": scenes}
            if not image_only and (missing_timings or not scenes):
                bundle_input["visual_evidence_gaps"] = [
                    f"scene_analysis: {missing_timings} scenes lack complete source timing "
                    "evidence; do not invent timestamps or assume opening executability"
                ]
            continue
        if isinstance(upstream_id, list):
            # 分批上游（如百场景级 scene_analyze）：所有批次完成后合并为一份结果。
            merged = _merge_upstream_results(
                session, [str(item) for item in upstream_id]
            )
            if merged is not None:
                if field == "scene_analysis" and task.task_type in {
                    TaskType.SHORT_SCRIPT,
                    TaskType.VISUAL_PLAN,
                }:
                    merged = _compact_scene_analysis(merged)
                bundle_input[field] = merged
            continue
        upstream = session.get(AgentTask, upstream_id)
        if upstream is not None and upstream.status == TaskStatus.COMPLETED:
            upstream_result = upstream.result_data
            if field == "scene_analysis" and task.task_type in {
                TaskType.SHORT_SCRIPT,
                TaskType.VISUAL_PLAN,
            }:
                upstream_result = _compact_scene_analysis(upstream_result)
            bundle_input[field] = upstream_result
    if task.task_type == TaskType.SHORT_SCRIPT:
        source = _script_source_context(task, session)
        if source is not None:
            bundle_input.update(source)
            bundle_input["research"] = {**bundle_input["research"], "source_images": source["source_images"]}
            bundle_input["script_source_media_context_read_only"] = source
    if task.task_type == TaskType.VISUAL_PLAN_QC and "prepared_visual_plan" in task.input_data:
        if (stable_hash(task.input_data) != task.input_hash
                or task.input_data.get("visual_plan_result_sha256") != stable_hash(bundle_input.get("visual_plan"))):
            raise HTTPException(status_code=409, detail="Prepared visual plan source changed")
        bundle_input["visual_plan"] = task.input_data["prepared_visual_plan"]
    materialized = bundle_input.pop("materialized_context", {})
    if materialized and task.content_id:
        content = session.get(Content, task.content_id)
        if content is not None:
            root = Path(content.content_dir).resolve()
            for field, relative_path in materialized.items():
                target = (root / str(relative_path)).resolve()
                if root in target.parents and target.is_file():
                    value = json.loads(target.read_text(encoding="utf-8"))
                    if field == "transcript":
                        encoded = json.dumps(value, ensure_ascii=False)
                        if len(encoded) > 8000:
                            value = {
                                "text": str(value.get("text") or "")[:7500],
                                "segments": [],
                                "truncated": True,
                            }
                    bundle_input[field] = value
    if task.task_type in {TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST}:

        def exact_completed(
            owner: AgentTask, field: str, kind: TaskType
        ) -> AgentTask | None:
            refs = owner.input_data.get("upstream_task_ids")
            ref = refs.get(field) if isinstance(refs, dict) else None
            if isinstance(ref, list):
                ref = ref[0] if len(ref) == 1 else None
            row = session.get(AgentTask, ref) if isinstance(ref, str) else None
            return (
                row
                if (
                    row is not None
                    and row.content_id == task.content_id
                    and row.task_type == kind
                    and row.status == TaskStatus.COMPLETED
                )
                else None
            )

        for field in (
            "research",
            "scene_analysis",
            "source_images",
            "asset_image_enabled",
            "image_only_gen_budget",
            "visual_evidence_gaps",
        ):
            bundle_input.pop(field, None)
        script = exact_completed(task, "script", TaskType.SHORT_SCRIPT)
        gaps = []
        if script is None:
            gaps.append(
                "script: exact completed lineage unavailable; visual evidence unavailable"
            )
            if "script" in (task.input_data.get("upstream_task_ids") or {}):
                bundle_input.pop("script", None)
        else:
            content = session.get(Content, task.content_id) if task.content_id else None
            mode = script.input_data.get("production_mode") or (
                content.production_mode.value if content else "footage"
            )
            bundle_input["production_mode"] = mode
            research = exact_completed(script, "research", TaskType.CONTENT_RESEARCH)
            declared = (task.input_data.get("upstream_task_ids") or {}).get("research")
            if declared is not None and (
                research is None or declared not in (research.id, [research.id])
            ):
                research = None
            if research is not None:
                bundle_input["research"] = research.result_data
            if mode == "image_only":
                if research is not None:
                    source = _script_source_context(script, session)
                    bundle_input["source_images"] = source["source_images"] if source is not None else [
                        {
                            key: image[key]
                            for key in ("url", "caption", "source_page")
                            if isinstance(image.get(key), str)
                            and (
                                key == "caption"
                                or image[key].startswith(("https://", "http://"))
                            )
                        }
                        for image in (research.result_data or {}).get(
                            "source_images", []
                        )[:30]
                        if isinstance(image, dict)
                        and isinstance(image.get("url"), str)
                        and image["url"].startswith(("https://", "http://"))
                    ]
                    if source is not None:
                        bundle_input["research"] = {**research.result_data, "source_images": source["source_images"]}
                        bundle_input["script_source_media_context_read_only"] = source
                enabled = script.input_data.get("asset_image_enabled")
                budget = script.input_data.get("image_only_gen_budget")
                if isinstance(enabled, bool):
                    bundle_input["asset_image_enabled"] = enabled
                if type(budget) is int and budget >= 0:
                    bundle_input["image_only_gen_budget"] = budget
                if not bundle_input.get("source_images") and not (
                    enabled is True and type(budget) is int and budget > 0
                ):
                    gaps.append(
                        "source_images: exact research image evidence unavailable"
                    )
            else:
                evidence = flow._recovery_current_input(script)
                if evidence.get("scene_analysis", {}).get("scenes"):
                    bundle_input["scene_analysis"] = evidence["scene_analysis"]
                else:
                    gaps.append(
                        "scene_analysis: exact completed scene evidence unavailable"
                    )
                gaps.extend(
                    gap
                    for gap in evidence.get("evidence_gaps", [])
                    if gap.startswith("scene")
                )
        if gaps:
            bundle_input["visual_evidence_gaps"] = gaps
    if task.content_id and task.task_type in {
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.VISUAL_PLAN_QC,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
    }:
        refs = dict(task.input_data.get("upstream_task_ids") or {})
        if task.task_type == TaskType.VISUAL_PLAN_QC:
            plan_ref = refs.get("visual_plan")
            plan = (
                session.get(AgentTask, plan_ref) if isinstance(plan_ref, str) else None
            )
            if (
                plan is not None
                and plan.content_id == task.content_id
                and plan.task_type == TaskType.VISUAL_PLAN
                and plan.status == TaskStatus.COMPLETED
                and stable_hash(plan.input_data) == plan.input_hash
            ):
                refs["script"] = plan.input_data.get("upstream_task_ids", {}).get(
                    "script"
                )
        # A new frozen context stays untouched. Legacy tasks receive explicitly
        # read-only supplementary evidence, never an upgraded subject/snapshot.
        review_fields = {"visual_review_context_version", "visual_review_source_context",
                         "visual_review_source_context_sha256"}
        frozen_review = bool(review_fields.intersection(task.input_data))
        if task.task_type == TaskType.VISUAL_PLAN_QC and frozen_review and "source_context_version" in task.input_data:
            raise HTTPException(status_code=409, detail="Pre-review and final source context versions conflict")
        if "source_context_version" not in task.input_data:
            if task.task_type == TaskType.VISUAL_PLAN_QC and (frozen_review or (
                    plan is not None and plan.input_data.get("production_mode") == ProductionMode.IMAGE_ONLY.value
                    and _owned_visual_chain(session, plan))):
                try:
                    context = visual_review_source_context(
                        session, task.content_id, refs, review_task=task, include_references=True
                    )
                    if frozen_review:
                        frozen = task.input_data.get("visual_review_source_context")
                        version = task.input_data.get("visual_review_context_version")
                        if (type(version) is not int or version != 1 or not isinstance(frozen, dict)
                                or stable_hash(frozen) != task.input_data.get("visual_review_source_context_sha256")
                                or frozen != context):
                            raise ValueError("Pre-review frozen source context changed")
                    bundle_input["source_context_read_only"] = context
                except ValueError as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from None
            else:
                bundle_input["source_context_read_only"] = source_context(session, task.content_id, refs)
    live_schema = schema_for(task.task_type, task.schema_version)
    if task.task_type == TaskType.SHORT_SCRIPT:
        duration = live_schema.get("properties", {}).get("estimated_duration_sec", {})
        duration["minimum"] = float(bundle_input.get("min_video_seconds") or 20)
        duration["maximum"] = float(bundle_input.get("max_video_seconds") or 90)
    context_manifest: list[dict] = []
    context_files = context_files_for(task, session)
    # Artifact IDs stay signed; transport URLs are scoped to the current task lease.
    # The old server's /media/artifacts route is not mounted in MPT.
    for url_field, id_field in (
        ("video_url", "video_artifact_id"),
        ("publish_video_url", "publish_artifact_id"),
        ("cover_image_url", "cover_artifact_id"),
    ):
        artifact_id = task.input_data.get(id_field)
        artifact = session.get(Artifact, artifact_id) if artifact_id else None
        if artifact is not None and artifact.relative_path in context_files:
            index = context_files.index(artifact.relative_path)
            bundle_input[url_field] = f"/api/v1/agent/tasks/{task.id}/context/{index}"
    # The task record keeps content-relative paths for server-side hashing and file
    # serving.  The Agent workspace, however, receives every file directly under
    # context/; make input.json use the same basename contract as context_files.
    if context_files:
        bundle_input["context_files"] = [
            Path(relative_path).name for relative_path in context_files
        ]
    if context_files and task.content_id:
        content = session.get(Content, task.content_id)
        if content is not None:
            context_manifest = _context_manifest(
                Path(content.content_dir).resolve(),
                context_files,
                list(task.input_data.get("scenes", []) or []),
            )
            for artifact in _visual_image_artifacts(task, session):
                context_manifest[context_files.index(artifact.relative_path)]["input_ref"] = artifact.id
            source = _script_source_context(task, session)
            if source is not None:
                for image in source["source_images"]:
                    context_manifest[context_files.index(image["relative_path"])]["input_ref"] = image["artifact_id"]
    bundle_errors = _bundle_contract_errors(
        session,
        task,
        bundle_input,
        context_files,
        context_manifest,
    )
    if bundle_errors:
        fail_bundle_preflight(service, task, token, bundle_errors, flow, failure_recovery=failure_recovery)
    extra_context = scene_evidence_context(task, session, flow)
    extra_offset = len(context_files)
    for index, (name, data) in enumerate(extra_context, extra_offset):
        context_manifest.append(
            {
                "index": index,
                "name": name,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "input_ref": "scene_analysis",
            }
        )
    if extra_context:
        bundle_input["scene_analysis_context_files"] = [
            name for name, _ in extra_context
        ]
    session.add(
        AuditEvent(
            actor_type="agent",
            actor_id=task.assigned_agent_id or "unknown-agent",
            entity_type="agent_task",
            entity_id=task.id,
            action="bundle_served",
            metadata_json={
                "served_prompt_hash": stable_hash(current_instructions),
                "schema_version": task.schema_version,
            },
        )
    )
    session.commit()
    return {
        "task": {
            "task_id": task.id,
            "task_type": task.task_type,
            "content_id": task.content_id,
            "attempt": task.attempt,
            "prompt_name": task.prompt_name,
            "prompt_version": f"live-{stable_hash(current_instructions)[:12]}",
            "prompt_snapshot_version": task.prompt_version,
            "schema_version": task.schema_version,
            "lease_expires_at": task.lease_expires_at,
        },
        "instructions": current_instructions,
        "input": bundle_input,
        "result_schema": live_schema,
        "previous_failures": [
            {
                "attempt": submission.attempt,
                "validation_errors": submission.validation_errors,
            }
            for submission in session.exec(
                select(AgentSubmission)
                .where(
                    AgentSubmission.task_id == task.id,
                    AgentSubmission.validation_status == "REJECTED",
                )
                .order_by(AgentSubmission.created_at.desc())
                .limit(3)
            ).all()
        ],
        "context_files": [
            {
                "name": Path(relative_path).name,
                "url": f"/api/v1/agent/tasks/{task.id}/context/{index}",
            }
            for index, relative_path in enumerate(context_files)
        ]
        + [
            {"name": name, "url": f"/api/v1/agent/tasks/{task.id}/context/{index}"}
            for index, (name, _) in enumerate(extra_context, extra_offset)
        ],
        # 素材清单：CLI 下载后逐项校对字节数与哈希，不一致拒绝开工。
        "context_manifest": context_manifest,
    }


def _visual_image_artifacts(task: AgentTask, session: Session) -> list[Artifact]:
    """Transport only signed image references, with the existing material provenance checks."""
    if task.task_type not in {TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC}:
        return []
    try:
        if stable_hash(task.input_data) != task.input_hash:
            raise ValueError("Visual context task input changed")
        plan = task
        ids = task.input_data.get("allowed_asset_ids", [])
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("Visual context image whitelist is invalid")
        if task.task_type == TaskType.VISUAL_PLAN_QC:
            ref = task.input_data.get("upstream_task_ids", {}).get("visual_plan")
            if not ref:
                if ids or "prepared_visual_plan" in task.input_data or "visual_plan" in task.input_data:
                    raise ValueError("Visual context plan lineage unavailable")
                return []
            plan = session.get(AgentTask, ref) if isinstance(ref, str) else None
            if (plan is None or plan.content_id != task.content_id
                    or plan.task_type != TaskType.VISUAL_PLAN or plan.status != TaskStatus.COMPLETED):
                raise ValueError("Visual context plan lineage unavailable")
            current = session.exec(select(AgentTask).where(
                AgentTask.content_id == task.content_id, AgentTask.task_type == TaskType.VISUAL_PLAN,
                AgentTask.status != TaskStatus.CANCELLED,
            ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
            if current is None or current.id != plan.id:
                raise ValueError("Visual context plan is stale")
            owned = _owned_visual_chain(session, plan)
            _unchanged_material_task(session, plan, owned=owned)
            if owned:
                _visual_parent_chain(session, plan, owned=True)
            selected = plan.result_data or {}
            if "prepared_visual_plan" in task.input_data:
                if task.input_data.get("visual_plan_result_sha256") != stable_hash(selected):
                    raise ValueError("Prepared visual plan source changed")
                selected = task.input_data["prepared_visual_plan"]
            if not isinstance(selected, dict) or not isinstance(selected.get("segments", []), list):
                raise ValueError("Visual context plan is invalid")
            ids = []
            for segment in selected.get("segments", []):
                if not isinstance(segment, dict) or not isinstance(segment.get("asset_request") or {}, dict):
                    raise ValueError("Visual context segment is invalid")
                if segment.get("asset_id"):
                    asset_id = segment["asset_id"]
                    if (asset_id not in plan.input_data.get("allowed_asset_ids", [])
                            or ("allowed_asset_ids" in task.input_data
                                and asset_id not in task.input_data["allowed_asset_ids"])):
                        raise ValueError("Visual context image is not declared by plan/QC")
                    ids.append(asset_id)
                request = segment.get("asset_request") or {}
                if request.get("reference_artifact_id"):
                    reference_id = request["reference_artifact_id"]
                    if reference_id not in plan.input_data.get("allowed_reference_artifact_ids", []):
                        raise ValueError("Visual context reference is not declared by plan")
                    ids.append(reference_id)
        if not ids:
            return []
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError("Visual context image reference is invalid")
        content = session.get(Content, task.content_id) if task.content_id else None
        if content is None:
            raise ValueError("Visual context content unavailable")
        root = Path(content.content_dir).resolve()
        artifacts = []
        for artifact_id in dict.fromkeys(ids):
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.content_id != content.id:
                raise ValueError("Visual context image missing/foreign")
            if (task.task_type == TaskType.VISUAL_PLAN
                    and artifact.artifact_type == "agent_generated_video"
                    and artifact.mime_type.startswith("video/")):
                continue
            if (artifact.artifact_type not in {"source_image", "agent_generated_image"}
                    or artifact.mime_type not in {"image/png", "image/jpeg", "image/webp"}
                    or Path(artifact.relative_path).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}):
                raise ValueError("Visual context Artifact is not a supported image")
            if artifact.artifact_type == "agent_generated_image":
                generated_image_material(session, content, artifact.id)
            else:
                target = (root / artifact.relative_path).resolve()
                if (root not in target.parents or not target.is_file()
                        or target.stat().st_size != artifact.size_bytes or sha256_file(target) != artifact.sha256):
                    raise ValueError("Visual context image bytes unavailable/changed")
            artifacts.append(artifact)
        return artifacts
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def _script_source_context(task: AgentTask, session: Session) -> dict | None:
    if task.task_type not in {TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST}:
        return None
    try:
        if stable_hash(task.input_data) != task.input_hash or not task.content_id:
            raise TaskConflictError("SOURCE_MEDIA_SCRIPT_INPUT_CHANGED")
        content = session.get(Content, task.content_id)
        if content is None or task.revision_id != content.current_revision_id:
            raise TaskConflictError("SOURCE_MEDIA_SCRIPT_REVISION_CHANGED")
        if task.task_type != TaskType.SHORT_SCRIPT:
            refs = task.input_data.get("upstream_task_ids") or {}
            script_id = refs.get("script") if isinstance(refs, dict) else None
            if isinstance(script_id, list):
                script_id = script_id[0] if len(script_id) == 1 else None
            if not script_id:
                return None
            script = session.get(AgentTask, script_id) if isinstance(script_id, str) else None
            if (script is None or script.content_id != task.content_id or script.revision_id != task.revision_id
                    or script.task_type != TaskType.SHORT_SCRIPT or script.status != TaskStatus.COMPLETED):
                raise TaskConflictError("SOURCE_MEDIA_REVIEW_SCRIPT_CHANGED")
            declared = refs.get("research")
            research_id = (script.input_data.get("upstream_task_ids") or {}).get("research")
            if declared is not None and declared not in (research_id, [research_id]):
                raise TaskConflictError("SOURCE_MEDIA_REVIEW_RESEARCH_CHANGED")
            return _script_source_context(script, session)
        if task.input_data.get("production_mode") != "image_only":
            return None
        context = script_source_media_context(session, task.content_id, task.input_data, verify_files=True)
        if context is not None:
            acquisition = context["source_media_acquisition"]
            consumed = session.exec(select(AuditEvent).where(
                AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == acquisition["research_task_id"],
                AuditEvent.action == "mpt_agent_consumed",
            )).all()
            if (len(consumed) != 1 or consumed[0].actor_type != "system"
                    or consumed[0].metadata_json.get("result_hash") != acquisition["research_result_hash"]):
                raise TaskConflictError("SOURCE_MEDIA_SCRIPT_RESEARCH_NOT_CONSUMED")
        return context
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def context_files_for(task: AgentTask, session: Session | None = None) -> list[str]:
    # Bundle and download must share indices after video filtering; never mutate the leased hash.
    files = _filter_context_files(
        task.task_type, [str(item) for item in task.input_data.get("context_files", [])]
    )
    if not files and task.task_type in {
        TaskType.SCENE_ANALYZE,
        TaskType.VISUAL_PLAN,
        TaskType.VISUAL_PLAN_QC,
    }:
        files = _scene_context_files(list(task.input_data.get("scenes", []) or []))
    if session is not None:
        source = _script_source_context(task, session)
        if source is not None:
            for relative_path in source["context_files"]:
                if relative_path not in files:
                    files.append(relative_path)
        for artifact in _visual_image_artifacts(task, session):
            if artifact.relative_path not in files:
                files.append(artifact.relative_path)
        if task.task_type in {TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC, TaskType.SHORT_SCRIPT,
                              TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST}:
            names = [Path(path).name.casefold() for path in files]
            if len(names) != len(set(names)):
                raise HTTPException(status_code=409, detail="Visual context filenames collide")
    if session is not None and task.task_type in _VIDEO_CONTEXT_TASKS:
        for field in ("video_artifact_id", "publish_artifact_id", "cover_artifact_id"):
            artifact_id = task.input_data.get(field)
            if not artifact_id:
                continue
            artifact = session.get(Artifact, artifact_id)
            if artifact is None or artifact.content_id != task.content_id:
                raise HTTPException(
                    status_code=409, detail="Task Artifact ownership mismatch"
                )
            if artifact.relative_path not in files:
                files.append(artifact.relative_path)
    return files
