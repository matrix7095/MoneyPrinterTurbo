"""Audited new evidence for a drained manual rights-only script rejection."""

from copy import deepcopy
import hashlib
from pathlib import Path

from glance_core.content_acceptance import _unchanged_material_task, is_handsfree_content
from glance_core.database import begin_immediate
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, PublicationRecord, StageRun, TaskStatus, TaskType, utc_now
from glance_core.task_service import TaskConflictError, script_source_media_context, stable_hash
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import assert_allowed
from app.business.source_media_refresh import _require_idle

REQUESTED = "mpt_rights_evidence_requested"
ACTOR = "rights-evidence"
RIGHTS_FIELDS = {"rights", "source_context_read_only.registration.rights_status", "source_context_read_only.registration.rights_note",
                "source_context.registration.rights_status", "source_context.registration.rights_note"}


def _reject(reason):
    raise TaskConflictError("RIGHTS_EVIDENCE_" + reason)


def _binding(session, task):
    submissions = session.exec(select(AgentSubmission).where(
        AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
        AgentSubmission.validation_status == "ACCEPTED",
    )).all()
    if len(submissions) != 1:
        _reject("ACCEPTED_PROOF_CHANGED")
    submission = submissions[0]
    raw = Path(submission.raw_result_path).resolve()
    try:
        raw_hash = hashlib.sha256(raw.read_bytes()).hexdigest()
    except OSError as exc:
        raise TaskConflictError("RIGHTS_EVIDENCE_ACCEPTED_RAW_UNAVAILABLE") from exc
    return {"task_id": task.id, "input_hash": task.input_hash, "result_hash": stable_hash(task.result_data),
            "task_type": task.task_type.value, "revision_id": task.revision_id, "status": task.status.value,
            "attempt": task.attempt, "max_attempts": task.max_attempts,
            "raw_result_path": str(raw), "raw_file_sha256": raw_hash}


def _inspect(runtime, session, content_id, revision_id, research_id, qc_id, recovery_id):
    from app.business.runtime import _require_current_revision

    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    if not revision_id or content.current_revision_id != revision_id:
        _reject("HEAD_CHANGED")
    if (content.production_mode != ProductionMode.IMAGE_ONLY or is_handsfree_content(session, content.id)
            or (content.business_status, content.current_stage, content.error_code, content.error_message)
            != (ContentStatus.SCRIPT_REVIEW, "recovery_blocked", "RECOVERY_BLOCKED", "角色路由动作与失败分类不匹配")):
        _reject("REQUIRES_BLOCKED_MANUAL_SCRIPT")
    rows = {}
    for name, task_id, kind in (("research", research_id, TaskType.CONTENT_RESEARCH),
                                ("qc", qc_id, TaskType.SCRIPT_QC), ("recovery", recovery_id, TaskType.RECOVERY)):
        task = session.get(AgentTask, task_id)
        if (task is None or task.content_id != content_id or task.revision_id != revision_id
                or task.task_type != kind or task.status != TaskStatus.COMPLETED):
            _reject("SUBJECT_CHANGED")
        rows[name] = task
    research, qc, recovery = (rows[name] for name in ("research", "qc", "recovery"))
    refs = qc.input_data.get("upstream_task_ids") or {}
    script = session.get(AgentTask, refs.get("script")) if isinstance(refs.get("script"), str) else None
    if (script is None or script.content_id != content_id or script.revision_id != revision_id
            or script.task_type != TaskType.SHORT_SCRIPT or script.status != TaskStatus.COMPLETED
            or refs.get("research") != research.id
            or (script.input_data.get("upstream_task_ids") or {}).get("research") != research.id):
        _reject("SCRIPT_LINEAGE_CHANGED")
    rows["script"] = script
    for task in rows.values():
        assert_allowed(session, task_id=task.id)
        _require_current_revision(session, task)
        try:
            runtime._require_accepted_native_task(session, task, consumed=True)
        except (ValueError, OSError) as exc:
            raise TaskConflictError("RIGHTS_EVIDENCE_ACCEPTED_PROOF_CHANGED") from exc
    errors = [issue for issue in qc.result_data.get("issues", []) if issue.get("severity") == "error"]
    if qc.result_data.get("passed") is not False or not errors or any(issue.get("field") not in RIGHTS_FIELDS for issue in errors):
        _reject("REQUIRES_RIGHTS_ONLY_QC")
    flow = runtime.workflow_service(session)
    failure, lineage, policy = (recovery.input_data.get(key) or {} for key in ("failure", "lineage", "policy"))
    plan = recovery.result_data
    signature = flow._root_cause_signature("BUSINESS_QC", "SCRIPT_QC_FAILED")
    if (failure.get("source_task_id") != qc.id or failure.get("source_task_type") != TaskType.SCRIPT_QC.value
            or failure.get("source_attempt") != qc.attempt or failure.get("failure_class") != "BUSINESS_QC"
            or failure.get("code") != "SCRIPT_QC_FAILED" or failure.get("retryable") is not False
            or lineage.get("repair_target_task_id") != script.id or lineage.get("parent_task_id") != qc.id
            or lineage.get("source_attempt") != qc.attempt or recovery.input_data.get("failure_result") != qc.result_data
            or not isinstance(lineage.get("root_task_id"), str) or lineage.get("content_id") != content_id
            or lineage.get("root_cause_signature") != signature
            or lineage.get("repair_fingerprint") != flow._repair_fingerprint(qc, failure_class="BUSINESS_QC", code="SCRIPT_QC_FAILED")
            or plan.get("failure_class") != "BUSINESS_QC" or plan.get("action") != "reroute_to_role"
            or plan.get("target_task_type") != TaskType.CONTENT_RESEARCH.value):
        _reject("RECOVERY_LINEAGE_CHANGED")
    related = [item for item in flow._recovery_history(content_id) if item["root_cause_signature"] == signature]
    limits = [lineage.get("max_rounds"), policy.get("max_rounds"), flow.recovery_max_rounds]
    if (not flow.recovery_enabled or policy.get("enabled") is not True
            or any(type(limit) is not int or limit <= 0 for limit in limits)
            or type(lineage.get("round")) is not int or lineage["round"] != len(related)
            or not 0 < len(related) < min(limits)):
        _reject("RECOVERY_BUDGET_EXHAUSTED_OR_CHANGED")
    escalated = session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
        AuditEvent.entity_type == "agent_task", AuditEvent.action == "recovery_escalated")).all()
    if (len(escalated) != 1 or escalated[0].actor_type != "system" or escalated[0].actor_id != "recovery-coordinator"
            or escalated[0].metadata_json != {"reason": content.error_message, "action": plan["action"]}):
        _reject("BLOCKED_PROOF_CHANGED")
    allowed = {TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC, TaskType.RECOVERY}
    if (session.exec(select(AgentTask.id).where(AgentTask.content_id == content_id, AgentTask.revision_id == revision_id,
            AgentTask.task_type.not_in(allowed))).first()
            or session.exec(select(Artifact.id).where(Artifact.content_id == content_id, Artifact.revision_id == revision_id,
                Artifact.artifact_type != "source_image")).first()
            or session.exec(select(StageRun.id).where(StageRun.content_id == content_id, StageRun.revision_id == revision_id,
                StageRun.stage.not_in(["acquire", "acquire_images", "transcript", "scene_analyze"]))).first()
            or session.exec(select(PublicationRecord.id).where(PublicationRecord.content_id == content_id,
                PublicationRecord.revision_id == revision_id)).first()):
        _reject("DOWNSTREAM_PRESENT")
    _require_idle(session, content, qc)
    media = script_source_media_context(session, content_id, script.input_data, verify_files=True)
    if media is None:
        _reject("SOURCE_MEDIA_UNAVAILABLE")
    binding = {"tasks": {name: _binding(session, task) for name, task in rows.items()},
               "source_images": media["source_images"], "lineage": deepcopy(lineage),
               "recovery_history": deepcopy(related), "max_rounds": min(limits)}
    return content, rows, binding


def preflight(runtime, content_id, revision_id, research_id, qc_id, recovery_id):
    with Session(runtime.engine) as session:
        _, _, binding = _inspect(runtime, session, content_id, revision_id, research_id, qc_id, recovery_id)
        return {"content_id": content_id, "revision_id": revision_id, "binding": binding}


def _receipt(session, task):
    from app.business.runtime import _require_owned_input

    receipts = session.exec(select(AuditEvent).where(AuditEvent.action == REQUESTED,
        AuditEvent.entity_type == "content", AuditEvent.entity_id == task.content_id,
        AuditEvent.metadata_json["research_task_id"].as_string() == task.id)).all()
    if len(receipts) != 1:
        _reject("RECEIPT_MISSING_OR_AMBIGUOUS")
    _require_owned_input(session, task)
    receipt, inputs = receipts[0], task.input_data
    data = receipt.metadata_json
    if (not isinstance(data.get("request"), dict) or not isinstance(data.get("binding"), dict)
            or receipt.actor_type != "admin" or receipt.actor_id != ACTOR or stable_hash(inputs) != task.input_hash
            or data.get("research_input_hash") != task.input_hash
            or data.get("request") != inputs.get("rights_evidence_request")
            or data.get("binding") != inputs.get("rights_evidence_binding")
            or inputs.get("rights_evidence_text_sha256") != hashlib.sha256(data["request"]["evidence_text"].encode()).hexdigest()
            or inputs.get("lineage") != data["binding"]["lineage"]):
        _reject("RECEIPT_CHANGED")
    for expected in data["binding"]["tasks"].values():
        source = session.get(AgentTask, expected["task_id"])
        if (source is None or source.content_id != task.content_id or source.revision_id != task.revision_id
                or _binding(session, source) != expected):
            _reject("FROZEN_SOURCE_CHANGED")
        _unchanged_material_task(session, source, owned=True, consumed=True)
    root = Path(session.get(Content, task.content_id).content_dir).resolve()
    for expected in data["binding"]["source_images"]:
        artifact = session.get(Artifact, expected["artifact_id"])
        if (artifact is None or artifact.content_id != task.content_id or artifact.revision_id != task.revision_id
                or artifact.artifact_type != "source_image" or artifact.sha256 != expected["sha256"]
                or artifact.relative_path != expected["relative_path"]):
            _reject("FROZEN_IMAGE_CHANGED")
        path = (root / artifact.relative_path).resolve()
        if (root not in path.parents or not path.is_file() or path.stat().st_size != artifact.size_bytes
                or hashlib.sha256(path.read_bytes()).hexdigest() != artifact.sha256):
            _reject("FROZEN_IMAGE_BYTES_CHANGED")
    return receipt


def continuation_inputs(session, parent, *, recovery_max_rounds=None):
    """Only an intact administrator receipt can carry a recovery root through normal tasks."""
    def has_receipt(task_id):
        return session.exec(select(AuditEvent.id).where(AuditEvent.action == REQUESTED,
            AuditEvent.entity_type == "content", AuditEvent.entity_id == parent.content_id,
            AuditEvent.metadata_json["research_task_id"].as_string() == task_id)).first() is not None

    if parent.task_type == TaskType.CONTENT_RESEARCH:
        if not any(key.startswith("rights_evidence_") for key in parent.input_data) and not has_receipt(parent.id):
            return {}
        research = parent
    else:
        upstream = parent.input_data.get("upstream_task_ids", {}).get("research")
        research_id = parent.input_data.get("rights_evidence_research_task_id")
        if (not research_id and not any(key.startswith("rights_evidence_") for key in parent.input_data)
                and not (isinstance(upstream, str) and has_receipt(upstream))):
            return {}
        research = session.get(AgentTask, research_id) if isinstance(research_id, str) else None
        if (parent.task_type != TaskType.SHORT_SCRIPT or research is None
                or upstream != research.id
                or parent.content_id != research.content_id or parent.revision_id != research.revision_id
                or stable_hash(parent.input_data) != parent.input_hash):
            _reject("CONTINUATION_LINEAGE_CHANGED")
    content = session.get(Content, parent.content_id)
    if (content is None or research.task_type != TaskType.CONTENT_RESEARCH or research.status != TaskStatus.COMPLETED
            or research.revision_id != content.current_revision_id):
        _reject("CONTINUATION_RESEARCH_CHANGED")
    receipt = _receipt(session, research)
    if recovery_max_rounds is not None and recovery_max_rounds > receipt.metadata_json["binding"]["max_rounds"]:
        _reject("CONTINUATION_BUDGET_CHANGED")
    inputs = {"rights_evidence_research_task_id": research.id, "lineage": deepcopy(research.input_data["lineage"]),
              "recovery_task_id": research.input_data["recovery_task_id"], "recovery_round": research.input_data["recovery_round"]}
    if parent.task_type != TaskType.CONTENT_RESEARCH and any(parent.input_data.get(key) != value for key, value in inputs.items()):
        _reject("CONTINUATION_BINDING_CHANGED")
    return inputs


def submit(runtime, content_id, payload):
    from app.business.runtime import OWNED, _ConsumptionSession

    with content_operation_lock(runtime.settings, content_id):
        with _ConsumptionSession(runtime.engine) as session:
            begin_immediate(session)
            try:
                assert_allowed(session, content_id=content_id)
                request = payload.model_dump()
                receipts = session.exec(select(AuditEvent).where(AuditEvent.action == REQUESTED,
                    AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id)).all()
                for receipt in receipts:
                    previous = receipt.metadata_json.get("request", {})
                    if previous.get("request_key") == payload.request_key:
                        if previous != request:
                            _reject("REQUEST_KEY_CHANGED")
                        task = session.get(AgentTask, receipt.metadata_json.get("research_task_id"))
                        if task is None:
                            _reject("RECEIPT_CHANGED")
                        _receipt(session, task)
                        return {"content_id": content_id, "revision_id": task.revision_id, "research_task_id": task.id, "replayed": True}
                    if (previous.get("expected_revision_id") == payload.expected_revision_id
                            and (previous.get("qc_task_id") == payload.qc_task_id
                                 or previous.get("evidence_text") == payload.evidence_text)):
                        _reject("SOURCE_OR_EVIDENCE_ALREADY_REQUESTED")
                content, rows, binding = _inspect(runtime, session, content_id, payload.expected_revision_id,
                    payload.research_task_id, payload.qc_task_id, payload.recovery_task_id)
                if any(getattr(payload, name + "_result_hash") != binding["tasks"][name]["result_hash"]
                       for name in ("research", "qc", "recovery")):
                    _reject("EXPECTED_RESULT_CHANGED")
                if session.exec(select(AgentTask.id).where(AgentTask.content_id == content_id,
                    AgentTask.input_data["rights_evidence_request"]["request_key"].as_string() == payload.request_key)).first():
                    _reject("ORPHAN_REQUEST_PRESENT")
                flow = runtime.workflow_service(session)
                inputs = flow._research_repair_input(content, rows["research"])
                inputs.update(source_media_refresh_of=rows["research"].id, rights_evidence_request=request,
                    rights_evidence_text_sha256=hashlib.sha256(payload.evidence_text.encode()).hexdigest(),
                    rights_evidence_binding=binding, lineage=binding["lineage"],
                    recovery_task_id=rows["recovery"].id, recovery_round=binding["lineage"]["round"],
                    recovery_plan=deepcopy(rows["recovery"].result_data), previous_research=deepcopy(rows["research"].result_data),
                    previous_script=deepcopy(rows["script"].result_data), previous_script_qc=deepcopy(rows["qc"].result_data),
                    rights_evidence_verification="新增文本是待核验材料，不是许可或QC批准。独立核验授权方、确切图片、抖音公开传播、裁切运镜、字幕及署名条件；证据不足如实保留缺口，不能更改权益登记或跳过后续门禁。")
                task = flow.create_agent_task(content_id, TaskType.CONTENT_RESEARCH, inputs, priority=52, allow_multiple=True)
                session.add(AuditEvent(actor_type="system", actor_id=ACTOR, entity_type="agent_task", entity_id=task.id,
                    action=OWNED, metadata_json={"input_hash": task.input_hash}))
                session.add(AuditEvent(actor_type="admin", actor_id=ACTOR, entity_type="content", entity_id=content_id,
                    action=REQUESTED, metadata_json={"request": request, "binding": binding,
                        "research_task_id": task.id, "research_input_hash": task.input_hash}))
                content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "agent_context"
                content.error_code = content.error_message = None
                content.updated_at = utc_now()
                session.add(content)
                Session.commit(session)
                return {"content_id": content_id, "revision_id": task.revision_id, "research_task_id": task.id, "replayed": False}
            finally:
                session.rollback()
