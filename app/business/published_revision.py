"""Append an explicitly requested published revision without rewriting its history."""

from glance_core.models import (
    AgentTask, Artifact, AuditEvent, Content, ContentRevision, ContentStatus,
    PublicationRecord, SchedulerOffer, StageRun, StageStatus, TaskStatus, utc_now,
)
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.content_revision import require_reconciled_publish_intents
from sqlmodel import select


def _require_idle(session, content_id):
    from app.business.conductor_launch import _load
    from app.business.content_host import child_work_unresolved, host_live
    from app.business.content_run import load_run

    run = load_run(session)
    execution = _load(session, content_id)
    if (run is not None and run.content_id == content_id and (
            run.state not in {"stopped", "expired"} or host_live(run) or child_work_unresolved(run))):
        raise TaskConflictError("PUBLISHED_REVISION_WORK_IN_FLIGHT")
    if execution is not None and execution.state != "FINISHED":
        raise TaskConflictError("PUBLISHED_REVISION_WORK_IN_FLIGHT")
    tasks = select(AgentTask.id).where(AgentTask.content_id == content_id)
    if (session.exec(select(AgentTask.id).where(
            AgentTask.content_id == content_id,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(StageRun.id).where(
            StageRun.content_id == content_id, StageRun.status == StageStatus.RUNNING,
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(tasks), SchedulerOffer.expires_at > utc_now(),
        )).first()):
        raise TaskConflictError("PUBLISHED_REVISION_WORK_IN_FLIGHT")


def start_published_revision(session, content_id, payload):
    """Caller owns the content lock and BEGIN IMMEDIATE; this function never commits."""
    from app.business.quarantine import assert_allowed

    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    request = payload.model_dump()
    previous = session.exec(select(ContentRevision).where(
        ContentRevision.content_id == content_id, ContentRevision.request_key == payload.request_key,
    )).first()
    if previous:
        if previous.feedback_json != request:
            raise TaskConflictError("PUBLISHED_REVISION_REQUEST_KEY_CHANGED")
        return {"content_id": content_id, "revision_id": previous.id,
                "revision_no": previous.revision_no, "replayed": True}
    if content.current_revision_id != payload.expected_revision_id:
        raise TaskConflictError("PUBLISHED_REVISION_HEAD_CHANGED")
    publication = session.get(PublicationRecord, payload.publication_id)
    if (content.business_status != ContentStatus.PUBLISHED or publication is None
            or publication.content_id != content_id or publication.revision_id != content.current_revision_id
            or publication.status not in {"PUBLISHED", "RECORDED"}
            or not (publication.external_id.strip() or publication.external_url.strip())):
        raise TaskConflictError("PUBLISHED_REVISION_REAL_PUBLICATION_REQUIRED")
    _require_idle(session, content_id)
    require_reconciled_publish_intents(session, content_id)
    revisions = session.exec(select(ContentRevision).where(ContentRevision.content_id == content_id)).all()
    head = content.current_revision_id
    task_rows = session.exec(select(AgentTask).where(
        AgentTask.content_id == content_id, AgentTask.revision_id == head,
    )).all()
    artifact_rows = session.exec(select(Artifact).where(
        Artifact.content_id == content_id, Artifact.revision_id == head,
    )).all()
    publications = session.exec(select(PublicationRecord).where(
        PublicationRecord.content_id == content_id, PublicationRecord.revision_id == head,
    )).all()
    snapshot = {
        "content": content.model_dump(mode="json"),
        "publication_id": publication.id,
        "publications": [row.model_dump(mode="json") for row in publications],
        "tasks": [{"id": row.id, "task_type": row.task_type.value, "input_hash": row.input_hash,
                   "result_hash": stable_hash(row.result_data), "row_sha256": stable_hash(row.model_dump(mode="json"))}
                  for row in task_rows],
        "artifacts": [row.model_dump(mode="json") for row in artifact_rows],
    }
    revision = ContentRevision(
        content_id=content_id, revision_no=max((row.revision_no for row in revisions), default=0) + 1,
        request_key=payload.request_key, revision_type="PUBLISHED_REWORK",
        subject_sha256=stable_hash(snapshot), parent_revision_id=head,
        snapshot_json=snapshot, feedback_json=request, actor_type="admin", actor_id="mpt-admin-api",
    )
    session.add(revision)
    content.current_revision_id = revision.id
    content.business_status, content.current_stage = ContentStatus.PREPARING_CONTEXT, "research"
    content.error_code = content.error_message = None
    content.updated_at = utc_now()
    session.add(content)
    session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="content",
                           entity_id=content_id, action="published_revision_started",
                           metadata_json={"revision_id": revision.id, "parent_revision_id": head,
                                          "publication_id": publication.id, "request_key": payload.request_key,
                                          "snapshot_sha256": revision.subject_sha256}))
    session.flush()
    return {"content_id": content_id, "revision_id": revision.id,
            "revision_no": revision.revision_no, "replayed": False}
