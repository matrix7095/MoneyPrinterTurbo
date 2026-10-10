"""Portable topic/refill policy extracted from Glance 369d4c1 handsfree.py.

Only control-plane predicates are retained; MPT owns sessions and media execution.
"""

from datetime import datetime, timedelta
from typing import Any
from glance_collector.service import CollectionService, _empty_result_backoff_active
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Candidate,
    CandidateStatus,
    Content,
    ContentStatus,
    ProductionMode,
    RightsStatus,
    Source,
    SourceType,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.settings import Settings
from glance_core.task_service import stable_hash
from glance_core.workflow import DuplicateTopicError, WorkflowService
from sqlmodel import Session, desc, select

from app.business.quarantine import CONTENT_QUARANTINE

_ACTIVE_TASK_STATUSES = {TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED}
_HANDSFREE_CANDIDATE_LOW_WATERMARK = 1
_LOW_WATER_SUPPLEMENT_META = "handsfree_low_water_supplement"
_LOW_WATER_SUPPLEMENT_COOLDOWN_MINUTES = 60
_LOW_WATER_SUPPLEMENT_DAILY_LIMIT = 3


def _latest_candidate_assist_items(
    session: Session,
) -> dict[str, tuple[dict[str, Any], AgentTask]]:
    tasks = session.exec(
        select(AgentTask)
        .where(
            AgentTask.task_type == TaskType.CANDIDATE_ASSIST,
            AgentTask.status == TaskStatus.COMPLETED,
        )
        .order_by(AgentTask.created_at.desc(), AgentTask.completed_at.desc())
        .limit(200)
    ).all()
    result: dict[str, tuple[dict[str, Any], AgentTask]] = {}
    for task in tasks:
        for item in (task.result_data or {}).get("items", []):
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("candidate_id") or "")
            if candidate_id and candidate_id not in result:
                result[candidate_id] = (item, task)
    return result


def _has_available_candidate(
    session: Session,
    flow: WorkflowService,
    settings: Settings,
) -> bool:
    """Use existing Agent verdicts and workflow gates to find unattended work."""
    # An archived content is an explicit retirement, not an active topic lock.
    # Keeping it here permanently starves the low-water supplement after a failed run.
    selected_ids = set(
        session.exec(
            select(Content.candidate_id).where(
                Content.business_status != ContentStatus.ARCHIVED
            )
        ).all()
    )
    blocked_ids = {
        row.entity_id
        for row in session.exec(
            select(AuditEvent).where(
                AuditEvent.action == "handsfree_candidate_blocked",
                AuditEvent.entity_type == "candidate",
            )
        ).all()
    }
    assist_items = _latest_candidate_assist_items(session)
    candidates = session.exec(
        select(Candidate).where(
            Candidate.status.in_([CandidateStatus.NEW, CandidateStatus.ENRICHED])
        )
    ).all()
    available = 0
    for candidate in candidates:
        if candidate.id in selected_ids or candidate.id in blocked_ids:
            continue
        if candidate.rights_status == RightsStatus.REJECTED:
            continue
        if candidate.status == CandidateStatus.NEW:
            available += 1
            continue
        if candidate.score < settings.handsfree_min_candidate_score:
            continue
        if flow.production_blockers(
            candidate,
            production_mode=flow.handsfree_candidate_mode(candidate, settings),
        ):
            continue
        item_and_task = assist_items.get(candidate.id)
        if item_and_task is None:
            if candidate.assist_advised_at is not None:
                continue
            available += 1
            continue
        item, assist_task = item_and_task
        if str(item.get("verdict") or "").upper() == "REJECT":
            continue
        if str(item.get("verdict") or "").upper() != "SELECT":
            continue
        if str(item.get("confidence") or "").lower() not in {"high", "medium"}:
            continue
        if _candidate_assist_mode(flow, candidate, settings, assist_task) is None:
            continue
        available += 1
    return available >= _HANDSFREE_CANDIDATE_LOW_WATERMARK


def _source_can_receive_low_water_supplement(source: Source, *, now: datetime) -> bool:
    """Allow normal supplements, then one persisted refill per bounded cooldown."""
    if not source.enabled or source.source_type != SourceType.AGENT:
        return False
    if _empty_result_backoff_active(source, now=now):
        return False
    state = (source.config_json or {}).get(_LOW_WATER_SUPPLEMENT_META)
    if not isinstance(state, dict):
        return True
    try:
        retry_at = datetime.fromisoformat(str(state.get("next_retry_at")))
    except (TypeError, ValueError):
        retry_at = None
    if retry_at is not None:
        retry_at = as_utc(retry_at)
        try:
            last_scheduled_at = datetime.fromisoformat(
                str(state.get("last_scheduled_at"))
            )
        except (TypeError, ValueError):
            last_scheduled_at = None
        if last_scheduled_at is not None:
            retry_at = min(
                retry_at,
                as_utc(last_scheduled_at)
                + timedelta(minutes=_LOW_WATER_SUPPLEMENT_COOLDOWN_MINUTES),
            )
        if retry_at > now:
            return False
    day = str(state.get("day") or "")
    if day != now.date().isoformat():
        return True
    try:
        count = max(0, int(state.get("count", 0) or 0))
    except (TypeError, ValueError):
        count = 0
    if count < _LOW_WATER_SUPPLEMENT_DAILY_LIMIT:
        return True
    # The daily burst cap remains; an expired persisted cooldown is the only
    # allowance after exhaustion so a queue cannot starve until day rollover.
    return retry_at is not None


def _record_low_water_supplement(source: Source, *, now: datetime) -> None:
    config = dict(source.config_json or {})
    previous = config.get(_LOW_WATER_SUPPLEMENT_META)
    previous = previous if isinstance(previous, dict) else {}
    day = now.date().isoformat()
    if str(previous.get("day") or "") != day:
        count = 0
    else:
        try:
            count = max(0, int(previous.get("count", 0) or 0))
        except (TypeError, ValueError):
            count = 0
    next_count = min(count + 1, _LOW_WATER_SUPPLEMENT_DAILY_LIMIT)
    cooldown_minutes = _LOW_WATER_SUPPLEMENT_COOLDOWN_MINUTES
    retry_at = now + timedelta(minutes=cooldown_minutes)
    config[_LOW_WATER_SUPPLEMENT_META] = {
        "status": (
            "EXHAUSTED_COOLDOWN"
            if next_count >= _LOW_WATER_SUPPLEMENT_DAILY_LIMIT
            else "COOLDOWN"
        ),
        "count": next_count,
        "daily_limit": _LOW_WATER_SUPPLEMENT_DAILY_LIMIT,
        "cooldown_minutes": cooldown_minutes,
        "day": day,
        "last_scheduled_at": now.isoformat(),
        "next_retry_at": retry_at.isoformat(),
    }
    source.config_json = config


def _handsfree_wip_count(session: Session) -> int:
    """Only eligible, coordinator-selected nonterminal topics consume the WIP budget."""
    selected = select(AuditEvent.entity_id).where(
        AuditEvent.action == "handsfree_candidate_selected",
        AuditEvent.entity_type == "content",
    )
    quarantined = select(AuditEvent.entity_id).where(
        AuditEvent.action == CONTENT_QUARANTINE,
        AuditEvent.entity_type == "content",
    )
    return len(
        session.exec(
            select(Content.id).where(
                Content.id.in_(selected),
                Content.id.notin_(quarantined),
                Content.business_status.notin_(
                    [ContentStatus.ARCHIVED, ContentStatus.PUBLISHED]
                ),
            )
        ).all()
    )


def _maybe_schedule_source_discovery(
    session: Session,
    settings: Settings,
    flow: WorkflowService,
    result: dict[str, int | str],
    *,
    now: datetime,
    collector: CollectionService | None = None,
) -> None:
    """Prefill a drained topic queue with a persisted bounded budget."""
    if not settings.handsfree_auto_topic_enabled or _handsfree_wip_count(session) >= 2:
        return
    if _has_available_candidate(session, flow, settings):
        return

    active_source_ids = {
        str((task.input_data or {}).get("source_id") or "")
        for task in session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
                AgentTask.status.in_(_ACTIVE_TASK_STATUSES),
            )
        ).all()
    }
    sources = session.exec(
        select(Source)
        .where(Source.enabled == True, Source.source_type == SourceType.AGENT)  # noqa: E712
        .order_by(Source.next_poll_at, Source.id)
    ).all()
    collector = collector or CollectionService(session, settings)
    for source in sources:
        if source.id in active_source_ids:
            continue
        if _empty_result_backoff_active(source, now=now):
            continue
        normal_poll_due = as_utc(source.next_poll_at) <= now
        if not normal_poll_due and not _source_can_receive_low_water_supplement(
            source, now=now
        ):
            continue
        run = collector.collect_source(source.id)
        if run.status == "SUCCESS":
            if not normal_poll_due:
                _record_low_water_supplement(source, now=now)
            session.add(source)
            session.commit()
            result["source_discovery_queued"] = (
                int(result.get("source_discovery_queued", 0)) + 1
            )
            return


def _has_audit(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any] | None = None,
) -> bool:
    rows = session.exec(
        select(AuditEvent)
        .where(
            AuditEvent.action == action,
            AuditEvent.entity_type == entity_type,
            AuditEvent.entity_id == entity_id,
        )
        .order_by(desc(AuditEvent.created_at))
        .limit(100)
    ).all()
    if not metadata:
        return bool(rows)
    return any(
        all(
            (row.metadata_json or {}).get(key) == value
            for key, value in metadata.items()
        )
        for row in rows
    )


def _add_audit(
    session: Session,
    *,
    action: str,
    entity_type: str,
    entity_id: str,
    metadata: dict[str, Any],
) -> None:
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="handsfree-coordinator",
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            metadata_json=metadata,
        )
    )


def _record_candidate_block(
    session: Session,
    candidate: Candidate,
    *,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> None:
    metadata = {"reason": reason, **(detail or {})}
    if _has_audit(
        session,
        action="handsfree_candidate_blocked",
        entity_type="candidate",
        entity_id=candidate.id,
        metadata={"reason": reason},
    ):
        return
    _add_audit(
        session,
        action="handsfree_candidate_blocked",
        entity_type="candidate",
        entity_id=candidate.id,
        metadata=metadata,
    )
    session.commit()


def _record_candidate_auto_skip(
    session: Session,
    candidate: Candidate,
    assist_task: AgentTask,
    *,
    verdict: str,
    confidence: str,
) -> bool:
    """Safely retire one non-selectable topic while keeping an auditable restore path."""
    if _has_audit(
        session,
        action="handsfree_candidate_auto_skipped",
        entity_type="candidate",
        entity_id=candidate.id,
        metadata={"assistant_task_id": assist_task.id},
    ):
        return False
    _add_audit(
        session,
        action="handsfree_candidate_auto_skipped",
        entity_type="candidate",
        entity_id=candidate.id,
        metadata={
            "assistant_task_id": assist_task.id,
            "previous_status": candidate.status.value,
            "verdict": verdict,
            "confidence": confidence,
            "reason": (
                "assistant_human_review"
                if verdict == "HUMAN"
                else "assistant_low_confidence"
                if verdict == "SELECT"
                else "assistant_verdict_not_selectable"
            ),
        },
    )
    candidate.status = CandidateStatus.IGNORED
    candidate.updated_at = utc_now()
    session.add(candidate)
    session.commit()
    return True


def _candidate_assist_mode(
    flow: WorkflowService,
    candidate: Candidate,
    settings: Settings,
    assist_task: AgentTask,
) -> ProductionMode | None:
    mode = flow.handsfree_candidate_mode(candidate, settings)
    declared = next(
        (
            entry
            for entry in assist_task.input_data.get("items", [])
            if isinstance(entry, dict) and entry.get("candidate_id") == candidate.id
        ),
        {},
    )
    if mode == ProductionMode.IMAGE_ONLY:
        if not (
            declared.get("production_mode") == mode.value
            and assist_task.input_data.get("asset_image_enabled") is True
            and type(assist_task.input_data.get("image_only_gen_budget")) is int
            and assist_task.input_data["image_only_gen_budget"] > 0
            and stable_hash(assist_task.input_data) == assist_task.input_hash
        ):
            return None
    elif declared.get("production_mode") == ProductionMode.IMAGE_ONLY.value:
        return None  # Do not silently switch the route the Agent evaluated.
    return mode


def _auto_select(
    session: Session,
    flow: WorkflowService,
    settings: Settings,
    result: dict[str, int | str],
) -> None:
    if not settings.handsfree_auto_topic_enabled:
        return
    if _handsfree_wip_count(session) >= 2:
        return
    assist_items = _latest_candidate_assist_items(session)
    candidates = session.exec(
        select(Candidate)
        .where(Candidate.status == CandidateStatus.ENRICHED)
        .order_by(Candidate.score.desc(), Candidate.updated_at.desc())
        .limit(100)
    ).all()
    for candidate in candidates:
        # An existing manual selection must never acquire Handsfree ownership.
        if session.exec(
            select(Content.id).where(Content.candidate_id == candidate.id)
        ).first():
            continue
        item_and_task = assist_items.get(candidate.id)
        if item_and_task is None:
            continue
        item, assist_task = item_and_task
        verdict = str(item.get("verdict") or "").upper()
        confidence = str(item.get("confidence") or "").lower()
        if verdict != "SELECT" or confidence not in {"high", "medium"}:
            if _record_candidate_auto_skip(
                session,
                candidate,
                assist_task,
                verdict=verdict,
                confidence=confidence,
            ):
                result["blocked"] = int(result["blocked"]) + 1
            continue
        if candidate.score < settings.handsfree_min_candidate_score:
            _record_candidate_block(
                session,
                candidate,
                reason="score_below_handsfree_threshold",
                detail={
                    "score": candidate.score,
                    "threshold": settings.handsfree_min_candidate_score,
                    "assistant_task_id": assist_task.id,
                },
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        if candidate.rights_status == RightsStatus.REJECTED:
            _record_candidate_block(
                session,
                candidate,
                reason="rights_rejected",
                detail={"assistant_task_id": assist_task.id},
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        mode = _candidate_assist_mode(flow, candidate, settings, assist_task)
        if mode is None:
            _record_candidate_block(
                session,
                candidate,
                reason="image_only_not_declared",
                detail={"assistant_task_id": assist_task.id},
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        blockers = flow.production_blockers(candidate, production_mode=mode)
        if blockers:
            _record_candidate_block(
                session,
                candidate,
                reason="production_blocked",
                detail={"blockers": blockers, "assistant_task_id": assist_task.id},
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        try:
            content = flow.select_candidate(candidate.id, production_mode=mode)
        except DuplicateTopicError as exc:
            _record_candidate_block(
                session,
                candidate,
                reason="duplicate_topic",
                detail={
                    "conflicts": exc.conflicts,
                    "assistant_task_id": assist_task.id,
                },
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        except ValueError as exc:
            _record_candidate_block(
                session,
                candidate,
                reason="selection_rejected",
                detail={"message": str(exc)[:500], "assistant_task_id": assist_task.id},
            )
            result["blocked"] = int(result["blocked"]) + 1
            continue
        _add_audit(
            session,
            action="handsfree_candidate_selected",
            entity_type="content",
            entity_id=content.id,
            metadata={
                "candidate_id": candidate.id,
                "assistant_task_id": assist_task.id,
                "score": candidate.score,
                "rights_status": candidate.rights_status.value,
                "production_mode": mode.value,
                "policy": "select_medium_or_high_confidence",
            },
        )
        session.commit()
        result["selected"] = int(result["selected"]) + 1
        break
