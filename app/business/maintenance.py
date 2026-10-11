"""MPT-owned five-minute polling and Handsfree topic control plane."""

import logging
from contextlib import contextmanager
from datetime import timedelta
from threading import Event, Thread

from glance_collector.service import CollectionService
from glance_core.database import begin_immediate
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Candidate,
    CollectionRun,
    Content,
    ContentStatus,
    PublicationRecord,
    SchedulerOffer,
    RuntimeSetting,
    Source,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.runtime_config import effective_settings
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.task_schemas import RecoveryPlan
from app.business.topic_policy import (
    _auto_select,
    _handsfree_wip_count,
    _maybe_schedule_source_discovery,
)
from sqlalchemy import event
from sqlalchemy.orm import aliased, with_loader_criteria
from sqlalchemy.exc import MultipleResultsFound, NoResultFound
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import (
    CONTENT_QUARANTINE,
    IsolatedTaskService,
    assert_allowed,
    eligible_task_clause,
)

logger = logging.getLogger(__name__)
TICK = "mpt_maintenance_last_run"
INTERVAL = timedelta(minutes=5)
TERMINAL_SCAN_CURSOR = "mpt_terminal_recovery_scan_cursor"


class _Session(Session):
    def commit(self):
        # Portable services commit internally; keep policy/task/ownership atomic.
        self.flush()


class _Tasks(IsolatedTaskService):
    def create_task(self, **kwargs):
        task = super().create_task(**kwargs)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="mpt-maintenance",
                entity_type="agent_task",
                entity_id=task.id,
                action="mpt_task_owned",
                metadata_json={"input_hash": task.input_hash},
            )
        )
        self.session.commit()
        return task


@contextmanager
def _session(runtime):
    with _Session(runtime.engine) as session:
        begin_immediate(session)
        quarantined = session.exec(
            select(AuditEvent.entity_id).where(
                AuditEvent.action == CONTENT_QUARANTINE,
                AuditEvent.entity_type == "content",
            )
        ).all()
        candidates = session.exec(
            select(Content.candidate_id).where(Content.id.in_(quarantined))
        ).all()
        owned = select(AuditEvent.entity_id).where(
            AuditEvent.action == "mpt_task_owned",
            AuditEvent.entity_type == "agent_task",
            AuditEvent.metadata_json["input_hash"].as_string() == AgentTask.input_hash,
        )
        # Read columns before installing ORM filters; identity-map objects are
        # not evidence that frozen input and consumed result still match bytes.
        receipt_row = aliased(AuditEvent)
        receipts = session.exec(
            select(
                AgentTask.id,
                AgentTask.input_data,
                AgentTask.input_hash,
                AgentTask.result_data,
                receipt_row.metadata_json,
            )
            .join(receipt_row, receipt_row.entity_id == AgentTask.id)
            .where(
                AgentTask.status == TaskStatus.COMPLETED,
                eligible_task_clause(),
                AgentTask.id.in_(owned),
                (
                    (AgentTask.task_type == TaskType.SOURCE_DISCOVERY)
                    & (receipt_row.action == "mpt_discovery_consumed")
                )
                | (
                    (AgentTask.task_type != TaskType.SOURCE_DISCOVERY)
                    & (receipt_row.action == "mpt_agent_consumed")
                ),
                receipt_row.entity_type == "agent_task",
            )
        ).all()
        verified = {}
        for key, inputs, frozen_input, result, receipt in receipts:
            matches = stable_hash(inputs) == frozen_input and receipt.get(
                "result_hash"
            ) == stable_hash(result)
            verified[key] = verified.get(key, True) and matches
        consumed = [key for key, valid in verified.items() if valid]
        eligible = eligible_task_clause() & (
            (AgentTask.status != TaskStatus.COMPLETED) | AgentTask.id.in_(consumed)
        )

        def restrict(state):
            if state.is_select:
                state.statement = state.statement.options(
                    with_loader_criteria(AgentTask, eligible, include_aliases=True),
                    with_loader_criteria(
                        Candidate, Candidate.id.notin_(candidates), include_aliases=True
                    ),
                    with_loader_criteria(
                        Content, Content.id.notin_(quarantined), include_aliases=True
                    ),
                )

        event.listen(session, "do_orm_execute", restrict)
        try:
            yield session
            Session.commit(session)
        finally:
            event.remove(session, "do_orm_execute", restrict)
            session.rollback()


def _flow(runtime, session, settings):
    flow = runtime.workflow_service(session)
    flow.tasks = _Tasks(
        session,
        settings.agent_submission_dir,
        lease_minutes=settings.agent_lease_minutes,
        max_inflight=settings.agent_max_inflight,
    )
    return flow


def _collector(runtime, session, settings, flow):
    collector = CollectionService(session, settings)
    collector.workflow = flow
    return collector


def _skip_terminal_recoveries(runtime):
    # Use an unfiltered Session: quarantined/unowned siblings can still be live.
    from app.business.runtime import (
        _require_current_revision,
        _require_reconciled_content,
        QC_TARGET_REJECTION,
    )

    with Session(runtime.engine) as session:
        begin_immediate(session)
        if not effective_settings(session, runtime.settings).handsfree_enabled:
            return 0
        cursor = session.get(RuntimeSetting, TERMINAL_SCAN_CURSOR)
        query = (
            select(Content.id)
            .where(
                Content.current_stage == "recovery_blocked",
                Content.error_code == "RECOVERY_BLOCKED",
                Content.business_status.in_(
                    [ContentStatus.FAILED, ContentStatus.SCRIPT_REVIEW]
                ),
            )
            .order_by(Content.id)
        )
        keys = session.exec(
            query.where(Content.id > (cursor.value if cursor else "")).limit(20)
        ).all()
        if not keys and cursor is not None:
            keys = session.exec(query.limit(20)).all()
        if keys:
            # Private keyset progress prevents rejected old evidence starving later topics.
            cursor = cursor or RuntimeSetting(key=TERMINAL_SCAN_CURSOR, value="")
            cursor.value, cursor.updated_at = keys[-1], utc_now()
            session.add(cursor)
            session.commit()
    skipped = 0
    for key in keys:
        try:
            with content_operation_lock(runtime.settings, key, timeout_seconds=0):
                with Session(runtime.engine) as session:
                    begin_immediate(session)
                    if not effective_settings(
                        session, runtime.settings
                    ).handsfree_enabled:
                        continue
                    content = session.get(Content, key)
                    if (
                        content is None
                        or content.current_stage != "recovery_blocked"
                        or content.error_code != "RECOVERY_BLOCKED"
                        or content.business_status
                        not in {ContentStatus.FAILED, ContentStatus.SCRIPT_REVIEW}
                    ):
                        continue
                    assert_allowed(session, content_id=key)
                    selections = session.exec(
                        select(AuditEvent).where(
                            AuditEvent.entity_type == "content",
                            AuditEvent.entity_id == key,
                            AuditEvent.action == "handsfree_candidate_selected",
                        )
                    ).all()
                    if not selections or any(
                        row.actor_type != "system"
                        or row.actor_id != "handsfree-coordinator"
                        or row.metadata_json.get("candidate_id") != content.candidate_id
                        for row in selections
                    ):
                        continue
                    if session.exec(
                        select(AuditEvent.id).where(
                            AuditEvent.entity_type == "content",
                            AuditEvent.entity_id == key,
                            AuditEvent.action.in_(
                                [
                                    "manual_candidate_selected",
                                    "handsfree_content_skipped",
                                ]
                            ),
                        )
                    ).first():
                        continue
                    tasks = session.exec(
                        select(AgentTask)
                        .where(AgentTask.content_id == key)
                        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
                    ).all()
                    if not tasks or any(
                        row.status
                        in {
                            TaskStatus.PENDING,
                            TaskStatus.LEASED,
                            TaskStatus.SUBMITTED,
                        }
                        for row in tasks
                    ):
                        continue
                    recovery = tasks[0]
                    if (
                        recovery.task_type != TaskType.RECOVERY
                        or recovery.status != TaskStatus.COMPLETED
                    ):
                        continue
                    if any(
                        row.id != recovery.id
                        and as_utc(row.created_at) == as_utc(recovery.created_at)
                        for row in tasks
                    ):
                        continue
                    _require_current_revision(session, recovery)
                    runtime._require_accepted_native_task(
                        session, recovery, consumed=True
                    )
                    plan = RecoveryPlan.model_validate(recovery.result_data)
                    reason = plan.reason
                    if plan.action != "mark_recovery_blocked":
                        runtime._check_content_qc_rejection_receipt(session, recovery)
                        reason = QC_TARGET_REJECTION
                    blocks = session.exec(
                        select(AuditEvent).where(
                            AuditEvent.entity_type == "agent_task",
                            AuditEvent.entity_id == recovery.id,
                            AuditEvent.actor_type == "system",
                            AuditEvent.actor_id == "recovery-coordinator",
                            AuditEvent.action == "recovery_blocked",
                        )
                    ).all()
                    if (
                        len(blocks) != 1
                        or blocks[0].metadata_json.get("action") != "mark_recovery_blocked"
                        or blocks[0].metadata_json.get("reason")
                        != content.error_message
                        or (plan.action != "mark_recovery_blocked" and content.error_message != reason)
                        or not 0
                        <= (
                            as_utc(blocks[0].created_at) - as_utc(content.updated_at)
                        ).total_seconds()
                        <= 1
                    ):
                        continue
                    if session.exec(
                        select(SchedulerOffer.request_id).where(
                            SchedulerOffer.task_id.in_([row.id for row in tasks]),
                            SchedulerOffer.expires_at > utc_now(),
                        )
                    ).first():
                        continue
                    if session.exec(
                        select(PublicationRecord.id).where(
                            PublicationRecord.content_id == key
                        )
                    ).first():
                        continue
                    _require_reconciled_content(session, key)
                    previous = {
                        "business_status": content.business_status.value,
                        "current_stage": content.current_stage,
                        "error_code": content.error_code,
                    }
                    content.business_status = ContentStatus.ARCHIVED
                    content.current_stage = "archived"
                    content.updated_at = utc_now()
                    session.add(content)
                    session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id="handsfree-coordinator",
                            entity_type="content",
                            entity_id=key,
                            action="handsfree_content_skipped",
                            metadata_json={
                                "candidate_id": content.candidate_id,
                                "recovery_task_id": recovery.id,
                                "result_hash": stable_hash(recovery.result_data),
                                "previous": previous,
                                "reason": reason,
                                "policy": "consumed_terminal_recovery",
                            },
                        )
                    )
                    session.commit()
                    skipped += 1
        except TimeoutError:
            continue
        except (
            TaskConflictError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
            NoResultFound,
            MultipleResultsFound,
        ) as exc:
            logger.warning(
                "Terminal Recovery evidence rejected for content %s (%s): %s",
                key,
                type(exc).__name__,
                exc,
            )
    return skipped


class Maintenance:
    def __init__(self, runtime):
        self.runtime = runtime
        self.stop = Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = Thread(target=self._loop, name="mpt-maintenance", daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join(timeout=30)
            if self.thread.is_alive():
                raise RuntimeError("MPT maintenance is still using its database")

    def _loop(self):
        while not self.stop.is_set():
            try:
                self.runtime.maintain_dispatch()
            except Exception:
                logger.exception(
                    "MPT maintenance failed; next bounded check in 30 seconds"
                )
            self.stop.wait(30)

    def run(self, *, now=None):
        try:
            with content_operation_lock(
                self.runtime.settings, "mpt-maintenance", timeout_seconds=0
            ):
                return self._run(as_utc(now or utc_now()))
        except TimeoutError:
            return {"status": "BUSY"}

    def _run(self, now):
        runtime = self.runtime
        result = {
            "status": "RUNNING",
            "polled": 0,
            "selected": 0,
            "source_discovery_queued": 0,
            "blocked": 0,
            "prepared": 0,
            "skipped": 0,
        }
        with _session(runtime) as session:
            tick = session.get(RuntimeSetting, TICK)
            if tick is not None and as_utc(tick.updated_at) + INTERVAL > now:
                return {"status": "NOT_DUE"}
            tick = tick or RuntimeSetting(key=TICK, value="")
            tick.value, tick.updated_at = now.isoformat(), now
            session.add(tick)
            due = session.exec(
                select(Source.id)
                .where(
                    Source.enabled.is_(True),
                    Source.next_poll_at <= now,
                )
                .order_by(Source.next_poll_at, Source.id)
                .limit(20)
            ).all()
        for source_id in due:
            try:
                with _session(runtime) as session:
                    settings = effective_settings(session, runtime.settings)
                    source = session.get(Source, source_id)
                    if (
                        source is None
                        or not source.enabled
                        or as_utc(source.next_poll_at) > now
                    ):
                        continue
                    flow = _flow(runtime, session, settings)
                    _collector(runtime, session, settings, flow).collect_source(
                        source_id
                    )
                    result["polled"] += 1
            except Exception as exc:
                logger.exception("MPT source polling failed for %s", source_id)
                with _session(runtime) as session:
                    source = session.get(Source, source_id)
                    if source is not None:
                        retry_at = max(as_utc(source.next_poll_at), now + INTERVAL)
                        source.last_polled_at = now
                        source.next_poll_at = retry_at
                        source.last_error = f"MPT polling failed: {type(exc).__name__}"
                        source.error_count += 1
                        session.add(source)
                        session.add(
                            CollectionRun(
                                source_id=source_id,
                                status="FAILED",
                                error_code="MPT_POLL_FAILED",
                                error_message=source.last_error,
                                started_at=now,
                                completed_at=now,
                            )
                        )
                        session.add(
                            AuditEvent(
                                actor_type="system",
                                actor_id="mpt-maintenance",
                                entity_type="source",
                                entity_id=source_id,
                                action="mpt_source_poll_failed",
                                metadata_json={
                                    "error_type": type(exc).__name__,
                                    "retry_at": retry_at.isoformat(),
                                },
                            )
                        )
        result["skipped"] = _skip_terminal_recoveries(runtime)
        with _session(runtime) as session:
            settings = effective_settings(session, runtime.settings)
            if not settings.handsfree_enabled:
                result["status"] = "HANDSFREE_DISABLED"
                return result
            flow = _flow(runtime, session, settings)
            if _handsfree_wip_count(session) < 2:
                flow.schedule_enrich_batch(settings.enrich_batch_size)
                flow.schedule_candidate_assist_batch(
                    settings.enrich_batch_size, settings=settings
                )
            previous = set(
                session.exec(
                    select(AuditEvent.id).where(
                        AuditEvent.action == "handsfree_candidate_selected",
                    )
                ).all()
            )
            _auto_select(session, flow, settings, result)
            for audit in session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == "handsfree_candidate_selected",
                    AuditEvent.id.notin_(previous),
                )
            ).all():
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="mpt-maintenance",
                        entity_type="content",
                        entity_id=audit.entity_id,
                        action="mpt_maintenance_selected",
                        metadata_json={"selection_audit_id": audit.id},
                    )
                )
            session.flush()
            _maybe_schedule_source_discovery(
                session,
                settings,
                flow,
                result,
                now=now,
                collector=_collector(runtime, session, settings, flow),
            )
            selected = select(AuditEvent.entity_id).where(
                AuditEvent.action == "mpt_maintenance_selected",
                AuditEvent.actor_id == "mpt-maintenance",
            )
            pending = session.exec(
                select(Content.id)
                .where(
                    Content.id.in_(selected),
                    Content.business_status == ContentStatus.PREPARING_CONTEXT,
                    Content.current_stage.in_([""]) | Content.current_stage.is_(None),
                )
                .order_by(Content.created_at)
                .limit(1)
            ).all()
        for content_id in pending:
            if self.stop.is_set():
                break
            from app.business.pipeline import prepare_content

            with Session(runtime.engine) as session:
                assert_allowed(session, content_id=content_id)
            runtime.run_content_stage(
                content_id,
                lambda engine, base, key: prepare_content(engine, settings, key),
            )
            result["prepared"] += 1
        with _session(runtime) as session:
            handsfree = select(AuditEvent.entity_id).where(
                AuditEvent.action == "handsfree_candidate_selected",
                AuditEvent.entity_type == "content",
                AuditEvent.actor_type == "system",
                AuditEvent.actor_id == "handsfree-coordinator",
            )
            packaged = session.exec(
                select(Content.id)
                .where(
                    Content.business_status == ContentStatus.PACKAGED,
                    Content.id.in_(handsfree),
                )
                .order_by(Content.updated_at, Content.id)
                .limit(20)
            ).all()
        if packaged and not self.stop.is_set():
            from app.business.publish_policy import try_auto_publish

            for content_id in packaged:
                if self.stop.is_set():
                    break
                try_auto_publish(runtime.engine, runtime.settings, content_id, now=now)
        return result
