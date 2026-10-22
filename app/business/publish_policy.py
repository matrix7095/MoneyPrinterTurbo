"""Server-owned Handsfree budget and final click authorization.

Queue checks are advisory. The same checks run inside core's BEGIN IMMEDIATE
before its durable AUTHORIZED event, so two processes cannot spend one budget.
"""

from datetime import timedelta, timezone
from pathlib import Path

from glance_core.content_acceptance import is_handsfree_content
from glance_core.content_revision import current_revision_clause, is_current_revision, require_reconciled_publish_intents
from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Content,
    ContentStatus,
    PublicationRecord,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.runtime_config import RuntimeSettingsError, effective_settings
from glance_core.task_service import AgentTaskService, TaskConflictError, stable_hash
from glance_core.workflow import handsfree_review_policy
from glance_media.executor import sha256_file
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import assert_allowed

# Preserve the source host's actual local-day contract, independent of deployment
# machine timezone. A fixed +08:00 also needs no optional Windows tzdata package.
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def _waiting(reason, now, *, reconcile=False):
    return {
        "status": "RECONCILE_REQUIRED" if reconcile else "WAITING_POLICY",
        "reason": reason,
        "retry_after_sec": 300,
        "retry_at": (now + timedelta(seconds=300)).isoformat(),
    }


def publish_budget(session: Session, settings, *, now=None):
    """Include actual manual/history publications and unresolved click reservations."""
    now = as_utc(now or utc_now())
    if settings.handsfree_daily_publish_limit <= 0:
        return _waiting("daily_publish_limit_is_zero", now)
    records = session.exec(
        select(PublicationRecord).where(
            PublicationRecord.platform == "douyin",
            PublicationRecord.status == "PUBLISHED",
        )
    ).all()
    times = [as_utc(record.published_at) for record in records]
    recorded_targets = {(record.content_id, record.revision_id) for record in records}
    events = session.exec(
        select(AuditEvent).where(AuditEvent.entity_type == "publish_intent")
    ).all()
    latest = {}
    for event in events:
        previous = latest.get(event.entity_id)
        key = (
            AgentTaskService._intent_sequence(event),
            as_utc(event.created_at),
            event.id,
        )
        if previous is None or key > previous[0]:
            latest[event.entity_id] = (key, event)
    for _, event in latest.values():
        metadata = event.metadata_json
        if not isinstance(metadata, dict) or not metadata.get("platform"):
            return _waiting("publish_intent_metadata_invalid", now, reconcile=True)
        if metadata.get("platform") != "douyin":
            continue
        status = metadata.get("status")
        # No guessed expiry: a click may have happened before worker/process loss.
        # Manual outstanding clicks also consume the automatic account's safety budget.
        if status == "AUTHORIZED":
            return _waiting("publish_intent_outstanding", now)
        if status not in {"FAILED", "PUBLISHED"}:
            return _waiting(
                "publish_outcome_requires_reconciliation", now, reconcile=True
            )
        if (
            status == "PUBLISHED"
            and (metadata.get("content_id"), metadata.get("content_revision_id")) not in recorded_targets
        ):
            times.append(as_utc(event.created_at))
    today = now.astimezone(SHANGHAI).date()
    if (
        sum(instant.astimezone(SHANGHAI).date() == today for instant in times)
        >= settings.handsfree_daily_publish_limit
    ):
        return _waiting("daily_publish_limit_reached", now)
    if times and now - max(times) < timedelta(
        minutes=settings.handsfree_publish_interval_minutes
    ):
        return _waiting("publish_interval_not_elapsed", now)
    return {"status": "ALLOWED"}


def _current_review(session, settings, content_id):
    from app.business.pipeline import verify_publish_ready, workflow

    verify_publish_ready(session, settings, content_id, automatic=True)
    flow = workflow(session, settings)
    if settings.publish_prepare_enabled:
        prepare = session.exec(
            select(AgentTask)
            .where(
                current_revision_clause(AgentTask), AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.PUBLISH_PREPARE,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        ).first()
        if (
            prepare is None
            or prepare.status != TaskStatus.COMPLETED
            or stable_hash(prepare.input_data) != prepare.input_hash
        ):
            raise TaskConflictError("Current publish preparation is missing or stale")
        if prepare.input_data.get("script") != flow.result_for(
            content_id, TaskType.SHORT_SCRIPT
        ) or prepare.input_data.get("research") != flow.result_for(
            content_id, TaskType.CONTENT_RESEARCH
        ):
            raise TaskConflictError("Current publish preparation lineage changed")
    review = session.exec(
        select(AgentTask)
        .where(
            current_revision_clause(AgentTask), AgentTask.content_id == content_id,
            AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST,
        )
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    ).first()
    result = review.result_data or {} if review is not None else {}
    if (
        review is None
        or review.status != TaskStatus.COMPLETED
        or stable_hash(review.input_data) != review.input_hash
        or str(result.get("confidence", "")).lower() != "high"
        or str(result.get("verdict", "")).upper() not in {"APPROVE", "READY"}
        or any(
            issue.get("severity", "").lower() == "blocker"
            for issue in result.get("issues", [])
            if isinstance(issue, dict)
        )
    ):
        raise TaskConflictError(
            "Current publish review requires high-confidence approval"
        )
    expected = flow.build_publish_review_input(
        content_id, publish_policy=handsfree_review_policy(), settings=settings
    )
    if any(review.input_data.get(key) != value for key, value in expected.items()):
        raise TaskConflictError("Current publish review lineage changed")
    if (
        settings.publish_prepare_enabled
        and prepare.input_data.get("cover_artifact_id") != expected["cover_artifact_id"]
    ):
        raise TaskConflictError("Current publish preparation cover changed")
    for key in ("publish_artifact_id", "cover_artifact_id"):
        artifact = session.get(Artifact, expected[key]) if expected.get(key) else None
        if artifact is None:
            raise TaskConflictError("Publish review is missing registered video/cover")
        content = session.get(Content, content_id)
        root = Path(content.content_dir).resolve()
        path = (root / artifact.relative_path).resolve()
        if (
            artifact.content_id != content_id
            or artifact.revision_id != content.current_revision_id
            or not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size != artifact.size_bytes
            or sha256_file(path) != artifact.sha256
        ):
            raise TaskConflictError("Publish review artifact integrity changed")
    final = session.exec(
        select(Artifact)
        .where(
            current_revision_clause(Artifact), Artifact.content_id == content_id, Artifact.artifact_type == "final_video"
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ).first()
    packaged = session.get(Artifact, expected["publish_artifact_id"])
    if packaged.sha256 != final.sha256:
        raise TaskConflictError("Publish package does not match accepted final video")
    return expected


def enforce_auto_intent(session, task, base_settings, *, now=None):
    """Called under core's intent transaction; never commits or starts a transaction."""
    assert_allowed(session, task_id=task.id)
    if not is_current_revision(session, task):
        raise TaskConflictError("CONTENT_REVISION_STALE")
    require_reconciled_publish_intents(session, task.content_id)
    policy = (task.input_data or {}).get("publish_policy") or {}
    if stable_hash(task.input_data) != task.input_hash:
        raise TaskConflictError("Publish task input changed")
    if policy.get("initiated_by") != "handsfree":
        return  # Explicit operator publishing retains its independent manual contract.
    account = task.input_data.get("account") or {}
    if (
        task.input_data.get("platform") != "douyin"
        or account.get("account_id") != "local-default"
    ):
        raise TaskConflictError(
            "Automatic publish requires the single local Douyin account"
        )
    if not task.content_id or not is_handsfree_content(session, task.content_id):
        raise TaskConflictError(
            "Automatic publish lacks Handsfree production ownership"
        )
    if (
        policy.get("user_clicked") is not False
        or policy.get("explicit_user_opt_in") is not True
    ):
        raise TaskConflictError("Automatic publish authorization is missing")
    if base_settings is None:
        raise TaskConflictError("Automatic publish requires effective MPT settings")
    try:
        current = effective_settings(session, base_settings)
    except RuntimeSettingsError as exc:
        raise TaskConflictError(str(exc)) from None
    if (
        not current.handsfree_enabled
        or not current.handsfree_auto_publish_douyin_enabled
    ):
        raise TaskConflictError("Automatic publish is disabled")
    decision = publish_budget(session, current, now=now)
    if decision["status"] != "ALLOWED":
        raise TaskConflictError(decision["reason"])
    content = session.get(Content, task.content_id)
    if content is None or content.business_status != ContentStatus.PACKAGED:
        raise TaskConflictError("Automatic publish requires current PACKAGED content")
    try:
        expected = _current_review(session, current, content.id)
    except (ValueError, RuntimeError) as exc:
        raise TaskConflictError(str(exc)) from exc
    video = session.get(Artifact, expected["publish_artifact_id"])
    if (
        task.input_data.get("video_artifact_id") != video.id
        or task.input_data.get("video_sha256") != video.sha256
        or task.input_data.get("cover_artifact_id") != expected["cover_artifact_id"]
    ):
        raise TaskConflictError("Publish task video/cover revision changed")
    from app.business.pipeline import workflow

    flow = workflow(session, current)
    for key, kind in (
        ("script", TaskType.SHORT_SCRIPT),
        ("research", TaskType.CONTENT_RESEARCH),
        ("publish_prepare", TaskType.PUBLISH_PREPARE),
    ):
        if task.input_data.get(key, {}) != (flow.result_for(content.id, kind) or {}):
            raise TaskConflictError("Publish task source lineage changed")


def queue_auto_publish(session, settings, content_id, *, now=None):
    """Caller holds the content OS lock. Quota waits leave PACKAGED untouched."""
    from app.business.pipeline import workflow

    assert_allowed(session, content_id=content_id)
    now = as_utc(now or utc_now())
    content = session.get(Content, content_id)
    try:
        current = effective_settings(session, settings)
    except RuntimeSettingsError as exc:
        return {
            "status": "BLOCKED",
            "reason": "RUNTIME_SETTINGS_INVALID",
            "fields": list(exc.fields),
        }
    if (
        content is None
        or content.business_status != ContentStatus.PACKAGED
        or not is_handsfree_content(session, content_id)
        or not current.handsfree_enabled
        or not current.handsfree_auto_publish_douyin_enabled
    ):
        return {"status": "NOT_READY", "reason": "automatic_publish_not_eligible"}
    # Never manufacture another task after any uncertain/terminal publish attempt.
    previous = session.exec(
        select(AgentTask)
        .where(
            current_revision_clause(AgentTask), AgentTask.content_id == content_id,
            AgentTask.task_type == TaskType.DOUYIN_PUBLISH,
        )
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    ).first()
    if previous:
        return {
            "status": "ALREADY_QUEUED"
            if previous.status
            in {TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED}
            else "RECONCILE_REQUIRED",
            "task_id": previous.id,
        }
    decision = publish_budget(session, current, now=now)
    if decision["status"] != "ALLOWED":
        return decision
    try:
        _current_review(session, current, content_id)
    except (ValueError, RuntimeError) as exc:
        return {"status": "NOT_READY", "reason": str(exc)}
    return workflow(session, current).queue_douyin_publish(
        content_id,
        settings=current,
        initiated_by="handsfree",
        user_clicked=False,
        allow_retry=False,
    )


def try_auto_publish(engine, settings, content_id, *, now=None):
    with content_operation_lock(settings, content_id), Session(engine) as session:
        return queue_auto_publish(session, settings, content_id, now=now)
