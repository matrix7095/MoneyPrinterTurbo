"""Consumed native terminal Recovery releases WIP without weakening QC."""

from datetime import timedelta

import pytest
from sqlmodel import Session, select
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Content,
    Candidate,
    ContentStatus,
    PublicationRecord,
    PublicationMode,
    RuntimeSetting,
    SchedulerOffer,
    TaskType,
    utc_now,
)
from test.business import test_content_qc_recovery as qc_recovery

from test.business.test_agent_failure_recovery import (
    registered_client,
    recovery_rows,
    record_intent,
)
from test.business.test_maintenance import enable
from app.business.topic_policy import _handsfree_wip_count
from app.business.maintenance import INTERVAL, TERMINAL_SCAN_CURSOR, Maintenance


failed_qc = qc_recovery.failed_qc
image_contract = qc_recovery.image_contract
REPAIR = qc_recovery.REPAIR


@pytest.fixture
def terminal(failed_qc):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    client = registered_client(http, runtime, "assistant")
    claim = client.claim()
    assert claim["task_id"] == recovery.id
    client.confirm_claim(recovery.id, claim["lease_token"], client.settings.agent_id)
    result = {**REPAIR, "action": "mark_recovery_blocked", "target_task_type": None}
    assert (
        client.complete(
            recovery.id, claim["lease_token"], client.settings.agent_id, result
        )["advanced"]
        == "CONSUMED"
    )
    enable(runtime, handsfree_auto_topic_enabled=False)
    with Session(runtime.engine) as session:
        # Model coordinator selection instead of the fixture's manual HTTP selection.
        for audit in session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_id == content_id,
                AuditEvent.action == "manual_candidate_selected",
            )
        ).all():
            session.delete(audit)
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="handsfree-coordinator",
                entity_type="content",
                entity_id=content_id,
                action="handsfree_candidate_selected",
                metadata_json={
                    "candidate_id": session.get(Content, content_id).candidate_id
                },
            )
        )
        session.commit()
    return runtime, content_id, recovery.id, qc_id


def test_real_maintenance_archives_consumed_terminal_once_and_preserves_history(
    terminal,
):
    runtime, content_id, recovery_id, qc_id = terminal
    with Session(runtime.engine) as session:
        before = {
            key: session.get(AgentTask, key).model_dump(mode="json")
            for key in (recovery_id, qc_id)
        }
        assert _handsfree_wip_count(session) == 1
    now = utc_now() + INTERVAL
    assert runtime.maintenance.run(now=now)["skipped"] == 1
    assert runtime.maintenance.run(now=now + INTERVAL)["skipped"] == 0
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        assert (content.business_status, content.current_stage) == (
            ContentStatus.ARCHIVED,
            "archived",
        )
        assert content.error_code == "RECOVERY_BLOCKED"
        assert _handsfree_wip_count(session) == 0
        assert {
            key: session.get(AgentTask, key).model_dump(mode="json") for key in before
        } == before
        audit = session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_id == content_id,
                AuditEvent.action == "handsfree_content_skipped",
            )
        ).one()
        assert audit.metadata_json["recovery_task_id"] == recovery_id


@pytest.mark.parametrize(
    "damage",
    [
        "manual",
        "active",
        "offer",
        "input",
        "result",
        "disabled",
        "quarantine",
        "UNKNOWN",
        "AUTHORIZED",
        "PUBLISHED",
        "malformed_intent",
        "publication",
        "missing_receipt",
        "raw_result",
        "block_audit",
    ],
)
def test_terminal_maintenance_fails_closed(terminal, damage):
    runtime, content_id, recovery_id, _ = terminal
    with Session(runtime.engine) as session:
        if damage in {"manual", "quarantine"}:
            session.add(
                AuditEvent(
                    actor_type="human",
                    actor_id="admin",
                    entity_type="content",
                    entity_id=content_id,
                    action="manual_candidate_selected"
                    if damage == "manual"
                    else "mpt_migration_quarantined_content",
                )
            )
        elif damage == "active":
            runtime.task_service(session).create_task(
                content_id=content_id,
                task_type=TaskType.SHORT_SCRIPT,
                instructions="active sibling fixture",
                input_data={"fixture": True},
            )
        elif damage == "offer":
            session.add(
                SchedulerOffer(
                    request_id="live-offer",
                    task_id=recovery_id,
                    expires_at=utc_now() + timedelta(hours=1),
                )
            )
        elif damage in {"UNKNOWN", "AUTHORIZED", "PUBLISHED", "malformed_intent"}:
            record_intent(
                session, content_id, damage, malformed=damage == "malformed_intent"
            )
        elif damage == "publication":
            session.add(
                PublicationRecord(
                    content_id=content_id,
                    mode=list(PublicationMode)[0],
                    platform="douyin",
                )
            )
        elif damage in {"missing_receipt", "block_audit"}:
            action = (
                "mpt_agent_consumed"
                if damage == "missing_receipt"
                else "recovery_blocked"
            )
            for audit in session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_id == recovery_id, AuditEvent.action == action
                )
            ).all():
                session.delete(audit)
        elif damage == "raw_result":
            (
                runtime.settings.agent_submission_dir
                / recovery_id
                / "1"
                / "result.json"
            ).write_text("{}")
        elif damage in {"input", "result"}:
            task = session.get(AgentTask, recovery_id)
            if damage == "input":
                task.input_data = {**task.input_data, "changed": True}
            else:
                task.result_data = {**task.result_data, "reason": "tampered"}
            session.add(task)
        if damage == "disabled":
            setting = session.get(RuntimeSetting, "handsfree_enabled")
            setting.value = "false"
            session.add(setting)
        session.commit()
    assert runtime.maintenance.run(now=utc_now() + INTERVAL).get("skipped", 0) == 0
    with Session(runtime.engine) as session:
        assert (
            session.get(Content, content_id).business_status != ContentStatus.ARCHIVED
        )


def test_parallel_terminal_maintenance_commits_one_skip(terminal):
    from concurrent.futures import ThreadPoolExecutor

    runtime, content_id, _, _ = terminal
    now = utc_now() + INTERVAL
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: runtime.maintenance.run(now=now), range(3)))
    assert sum(result.get("skipped", 0) for result in results) == 1
    with Session(runtime.engine) as session:
        assert (
            len(
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == content_id,
                        AuditEvent.action == "handsfree_content_skipped",
                    )
                ).all()
            )
            == 1
        )


def test_terminal_archive_and_receipt_rollback_together(terminal, monkeypatch):
    runtime, content_id, _, _ = terminal
    original = Session.commit

    def fail_archive(session):
        if any(
            isinstance(row, Content) and row.business_status == ContentStatus.ARCHIVED
            for row in session.identity_map.values()
        ):
            raise RuntimeError("injected archive commit failure")
        return original(session)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_archive)
        with pytest.raises(RuntimeError, match="injected archive commit failure"):
            runtime.maintenance.run(now=utc_now() + INTERVAL)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery_blocked"
        assert not session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_id == content_id,
                AuditEvent.action == "handsfree_content_skipped",
            )
        ).first()
    assert runtime.maintenance.run(now=utc_now() + 2 * INTERVAL)["skipped"] == 1


def test_rejected_old_batch_cannot_starve_later_valid_terminal_and_wraps(terminal):
    runtime, content_id, _, _ = terminal
    with Session(runtime.engine) as session:
        good = session.get(Content, content_id)
        before = {}
        for index in range(20):
            data = session.get(Candidate, good.candidate_id).model_dump()
            data.update(
                id=f"bad_candidate_{index}",
                source_url=f"https://example.org/bad/{index}",
                normalized_url=f"https://example.org/bad/{index}",
            )
            bad = Candidate(**data)
            session.add(bad)
            item = Content(
                id=f"a_terminal_{index:02d}",
                candidate_id=bad.id,
                title="Unowned terminal fixture",
                content_dir=good.content_dir,
                business_status=ContentStatus.FAILED,
                current_stage="recovery_blocked",
                error_code="RECOVERY_BLOCKED",
            )
            session.add(item)
            before[item.id] = item.updated_at
        session.commit()
    now = utc_now() + INTERVAL
    assert runtime.maintenance.run(now=now)["skipped"] == 0
    assert Maintenance(runtime).run(now=now + INTERVAL)["skipped"] == 1
    assert Maintenance(runtime).run(now=now + 2 * INTERVAL)["skipped"] == 0
    with Session(runtime.engine) as session:
        assert (
            session.get(RuntimeSetting, TERMINAL_SCAN_CURSOR).value == "a_terminal_19"
        )
        for key, stamp in before.items():
            assert (
                session.get(Content, key).updated_at.replace(tzinfo=stamp.tzinfo)
                == stamp
            )


def test_cursor_transaction_failure_leaves_content_and_cursor_unchanged(
    terminal, monkeypatch
):
    runtime, content_id, _, _ = terminal
    original = Session.commit

    def fail_cursor(session):
        if any(
            isinstance(row, RuntimeSetting) and row.key == TERMINAL_SCAN_CURSOR
            for row in list(session.new) + list(session.dirty)
        ):
            raise RuntimeError("injected cursor commit failure")
        return original(session)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "commit", fail_cursor)
        with pytest.raises(RuntimeError, match="injected cursor commit failure"):
            runtime.maintenance.run(now=utc_now() + INTERVAL)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery_blocked"
        assert session.get(RuntimeSetting, TERMINAL_SCAN_CURSOR) is None


def test_rejected_native_evidence_records_concrete_reason(terminal, caplog):
    runtime, _, recovery_id, _ = terminal
    with Session(runtime.engine) as session:
        recovery = session.get(AgentTask, recovery_id)
        recovery.input_data = {**recovery.input_data, "changed": True}
        session.add(recovery)
        session.commit()
    assert runtime.maintenance.run(now=utc_now() + INTERVAL)["skipped"] == 0
    assert "TaskConflictError" in caplog.text
    assert "RECOVERY_SOURCE_CHANGED_OR_UNOWNED" in caplog.text
