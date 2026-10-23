"""Request new source research before an unstarted script, preserving accepted history."""

from glance_core.content_revision import require_reconciled_publish_intents
from glance_core.database import begin_immediate
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus,
    ProductionMode, PublicationRecord, SchedulerOffer, StageRun, StageStatus,
    TaskStatus, TaskType, utc_now,
)
from glance_core.task_service import TaskConflictError, stable_hash
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import assert_allowed

REFRESH = "mpt_source_media_refresh_requested"
ACTOR = "source-media-refresh"


def _replay(session, content_id, payload):
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id,
        AuditEvent.action == REFRESH,
        AuditEvent.metadata_json["request"]["request_key"].as_string() == payload.request_key,
    )).all()
    if not receipts:
        return None
    if len(receipts) != 1 or receipts[0].metadata_json.get("request") != payload.model_dump():
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_REQUEST_KEY_CHANGED")
    receipt = receipts[0]
    data = receipt.metadata_json
    task = session.get(AgentTask, data.get("research_task_id"))
    if (receipt.actor_type != "admin" or receipt.actor_id != ACTOR
            or task is None or task.content_id != content_id
            or task.revision_id != payload.expected_revision_id
            or task.task_type != TaskType.CONTENT_RESEARCH
            or task.input_hash != data.get("research_input_hash")
            or stable_hash(task.input_data) != task.input_hash
            or task.input_data.get("source_media_refresh_of") != payload.research_task_id
            or task.input_data.get("source_media_refresh_request") != payload.model_dump()):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_RECEIPT_CHANGED")
    return {"content_id": content_id, "revision_id": task.revision_id,
            "research_task_id": task.id, "replayed": True}


def _require_idle(session, content, pending):
    from app.business.conductor_launch import _load
    from app.business.content_host import child_work_unresolved, host_live
    from app.business.content_run import load_run

    run = load_run(session)
    execution = _load(session, content.id)
    if ((run is not None and run.content_id == content.id and (
            run.state not in {"stopped", "expired"} or host_live(run) or child_work_unresolved(run)))
            or (execution is not None and execution.state != "FINISHED")):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_WORK_IN_FLIGHT")
    task_ids = select(AgentTask.id).where(AgentTask.content_id == content.id)
    if (session.exec(select(AgentTask.id).where(
            AgentTask.content_id == content.id,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
            AgentTask.id != pending.id,
        )).first() or session.exec(select(StageRun.id).where(
            StageRun.content_id == content.id,
            StageRun.status.in_([StageStatus.PENDING, StageStatus.RUNNING]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(task_ids), SchedulerOffer.expires_at > utc_now(),
        )).first()):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_WORK_IN_FLIGHT")
    require_reconciled_publish_intents(session, content.id)


def _refresh(runtime, session, content_id, payload):
    from app.business.runtime import OWNED, _require_current_revision, _require_owned_input
    from glance_core.task_service import _normalize_content_task_input

    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    replay = _replay(session, content_id, payload)
    if replay is not None:
        return replay
    if not content.current_revision_id:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_BUSINESS_REVISION_REQUIRED")
    if content.current_revision_id != payload.expected_revision_id:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_HEAD_CHANGED")
    if session.exec(select(AgentTask.id).where(
            AgentTask.content_id == content_id,
            AgentTask.input_data["source_media_refresh_request"]["request_key"].as_string() == payload.request_key,
        )).first():
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_RECEIPT_MISSING")
    if (content.production_mode != ProductionMode.IMAGE_ONLY
            or content.business_status != ContentStatus.WAITING_AGENT or content.current_stage != "short_script"):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_REQUIRES_UNSTARTED_SCRIPT")
    research = session.get(AgentTask, payload.research_task_id)
    if (research is None or research.content_id != content_id
            or research.revision_id != content.current_revision_id
            or research.task_type != TaskType.CONTENT_RESEARCH or research.status != TaskStatus.COMPLETED
            or not isinstance(research.result_data, dict)
            or not isinstance(research.result_data.get("source_media_search"), dict)):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_CURRENT_RESEARCH_REQUIRED")
    _require_current_revision(session, research)
    try:
        runtime._require_accepted_native_task(session, research, consumed=True)
    except (OSError, ValueError) as exc:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_RESEARCH_PROOF_CHANGED") from exc
    current = session.exec(select(AgentTask).where(
        AgentTask.content_id == content_id, AgentTask.revision_id == content.current_revision_id,
    )).all()
    downstream = [row for row in current if row.task_type != TaskType.CONTENT_RESEARCH
                  and row.status != TaskStatus.CANCELLED]
    if len(downstream) != 1:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_DOWNSTREAM_PRESENT")
    pending = downstream[0]
    refs = pending.input_data.get("upstream_task_ids")
    if (pending.task_type != TaskType.SHORT_SCRIPT or pending.status != TaskStatus.PENDING
            or pending.attempt != 0 or pending.assigned_agent_id or pending.lease_token_hash
            or pending.lease_expires_at or pending.started_at or pending.completed_at
            or pending.attempt_started_at or pending.last_heartbeat_at or pending.result_data is not None
            or pending.error_code or pending.error_message or pending.not_before
            or not isinstance(refs, dict) or refs != {"research": research.id}
            or session.exec(select(AgentSubmission.id).where(AgentSubmission.task_id == pending.id)).first()):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_SCRIPT_ALREADY_STARTED_OR_CHANGED")
    assert_allowed(session, task_id=pending.id)
    if stable_hash(pending.input_data) != pending.input_hash:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_SCRIPT_INPUT_CHANGED")
    canonical, _ = _normalize_content_task_input(session, TaskType.SHORT_SCRIPT, content_id, pending.input_data)
    if canonical != pending.input_data:
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_SCRIPT_INPUT_CHANGED")
    events = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == pending.id,
    )).all()
    created = [row for row in events if row.action == "created"]
    normalized = [row for row in events if row.action == "task_input_normalized"]
    expected_fields = ["content_revision_id", "content_revision"]
    if (len(created) != 1 or created[0].actor_type != "system"
            or created[0].actor_id != "task-service" or created[0].metadata_json != {}
            or len(normalized) != 1
            or any(row.actor_type != "system" or row.actor_id != "task-service"
                   or row.metadata_json != {"fields": expected_fields} for row in normalized)
            or any(row.action not in {"created", "task_input_normalized", OWNED} for row in events)):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_SCRIPT_HISTORY_CHANGED")
    if any(row.action == OWNED for row in events):
        _require_owned_input(session, pending)
    if (session.exec(select(Artifact.id).where(
            Artifact.content_id == content_id, Artifact.revision_id == content.current_revision_id,
            Artifact.artifact_type != "source_image",
        )).first() or session.exec(select(PublicationRecord.id).where(
            PublicationRecord.content_id == content_id,
            PublicationRecord.revision_id == content.current_revision_id,
        )).first()):
        raise TaskConflictError("SOURCE_MEDIA_REFRESH_DOWNSTREAM_PRESENT")
    _require_idle(session, content, pending)
    flow = runtime.workflow_service(session)
    inputs = flow._research_repair_input(content, research)
    inputs.update(source_media_refresh_of=research.id, source_media_refresh_request=payload.model_dump())
    # The feedback is a lead for independent verification, never a license or a QC approval.
    inputs["source_media_refresh_verification"] = (
        "核验反馈中的新素材及授权证据，重新检查同型号官方媒体；未知或缺失许可必须如实保留，"
        "不得因管理员要求补查而认定素材可公开发布。"
    )
    flow.tasks.cancel(pending.id, actor_id=ACTOR, actor_type="admin")
    replacement = flow.create_agent_task(content_id, TaskType.CONTENT_RESEARCH, inputs,
                                         priority=35, allow_multiple=True)
    session.add(AuditEvent(actor_type="system", actor_id=ACTOR, entity_type="agent_task",
                           entity_id=replacement.id, action=OWNED,
                           metadata_json={"input_hash": replacement.input_hash}))
    content.current_stage = "agent_context"
    content.error_code = content.error_message = None
    content.updated_at = utc_now()
    session.add(content)
    session.add(AuditEvent(actor_type="admin", actor_id=ACTOR, entity_type="content", entity_id=content_id,
                           action=REFRESH, metadata_json={"request": payload.model_dump(),
                               "research_task_id": replacement.id, "research_input_hash": replacement.input_hash,
                               "previous_research_result_hash": stable_hash(research.result_data),
                               "cancelled_script_task_id": pending.id}))
    session.flush()
    return {"content_id": content_id, "revision_id": replacement.revision_id,
            "research_task_id": replacement.id, "replayed": False}


def refresh_source_media(runtime, content_id, payload):
    from app.business.runtime import _ConsumptionSession

    with content_operation_lock(runtime.settings, content_id):
        with _ConsumptionSession(runtime.engine) as session:
            begin_immediate(session)
            try:
                response = _refresh(runtime, session, content_id, payload)
                Session.commit(session)
                return response
            finally:
                session.rollback()
