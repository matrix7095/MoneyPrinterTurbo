"""Consumed native terminal Recovery releases WIP without weakening QC."""

from datetime import timedelta
from pathlib import Path

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
    TaskStatus,
    Source,
    SourceType,
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
from app.business.runtime import AGENT_CONSUMED, QC_PLAN_REJECTED
from glance_core.task_service import TaskConflictError
from glance_core.content_acceptance import acceptance_subject, verify_acceptance


failed_qc = qc_recovery.failed_qc
image_contract = qc_recovery.image_contract
REPAIR = qc_recovery.REPAIR
pytestmark = pytest.mark.parametrize("failed_qc", ["handsfree"], indirect=True)


@pytest.fixture(params=["blocked", "rejected"])
def terminal(failed_qc, request):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    client = registered_client(http, runtime, "assistant")
    claim = client.claim()
    assert claim["task_id"] == recovery.id
    client.confirm_claim(recovery.id, claim["lease_token"], client.settings.agent_id)
    result = {**REPAIR, "action": "mark_recovery_blocked", "target_task_type": None}
    if request.param == "rejected":
        result.update(action="repair_task_context", target_task_type="content_qc")
    assert (
        client.complete(
            recovery.id, claim["lease_token"], client.settings.agent_id, result
        )["advanced"]
        == "CONSUMED"
    )
    enable(runtime, handsfree_auto_topic_enabled=False)
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
        root = Path(session.get(Content, content_id).content_dir)
        media = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
        task_ids = set(session.exec(select(AgentTask.id).where(AgentTask.content_id == content_id)).all())
    assert runtime.consume_agent(recovery_id) is False
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
        assert set(session.exec(select(AgentTask.id).where(AgentTask.content_id == content_id)).all()) == task_ids
        assert not session.exec(select(PublicationRecord)).first()
    assert runtime.consume_agent(recovery_id) is False
    assert all(path.read_bytes() == value for path, value in media.items())


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
        "newer_recovery",
        "unowned_sibling",
        "leased_sibling",
        "submitted_sibling",
        "newer_completed_sibling",
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
        elif damage in {"newer_recovery", "unowned_sibling", "leased_sibling", "submitted_sibling", "newer_completed_sibling"}:
            latest = session.get(AgentTask, recovery_id)
            sibling = AgentTask(**{**latest.model_dump(), "id": "agt_sibling", "dedup_key": None,
                                  "task_type": TaskType.RECOVERY if damage == "newer_recovery" else TaskType.SHORT_SCRIPT,
                                  "created_at": latest.created_at + timedelta(seconds=1),
                                  "status": {"leased_sibling": TaskStatus.LEASED, "submitted_sibling": TaskStatus.SUBMITTED,
                                             "unowned_sibling": TaskStatus.PENDING}.get(damage, TaskStatus.COMPLETED)})
            session.add(sibling)
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


@pytest.mark.parametrize("damage", ["missing", "code", "action", "target_task_type", "recovery_input_hash", "recovery_result_hash",
                                     "source_task_id", "source_input_hash", "source_result_hash", "source_attempt",
                                     "target_task_id", "target_input_hash", "target_result_hash", "actor", "duplicate",
                                     "missing_and_origin_tampered"])
@pytest.mark.parametrize("terminal", ["rejected"], indirect=True)
def test_rejection_receipt_cannot_be_skipped_by_consumed_or_maintenance(terminal, damage):
    runtime, content_id, recovery_id, _ = terminal
    with Session(runtime.engine) as session:
        receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery_id,
                                                       AuditEvent.action == QC_PLAN_REJECTED)).one()
        if damage in {"missing", "missing_and_origin_tampered"}:
            session.delete(receipt)
            if damage == "missing_and_origin_tampered":
                task = session.get(AgentTask, recovery_id)
                task.input_data = {**task.input_data, "failure": {**task.input_data["failure"], "source_task_type": "visual_plan_qc"}}
                session.add(task)
        elif damage == "duplicate":
            session.add(AuditEvent(**{**receipt.model_dump(), "id": "audit_duplicate"}))
        elif damage == "actor":
            receipt.actor_id = "assistant"
            session.add(receipt)
        else:
            receipt.metadata_json = {**receipt.metadata_json, damage: "tampered"}
            session.add(receipt)
        session.commit()
    with pytest.raises(TaskConflictError, match="RECOVERY_QC_REJECTION_RECEIPT_CHANGED|RECOVERY_SOURCE_CHANGED_OR_UNOWNED"):
        runtime.consume_agent(recovery_id)
    assert runtime.maintenance.run(now=utc_now() + INTERVAL)["skipped"] == 0
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery_blocked"
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery_id,
                                                   AuditEvent.action == AGENT_CONSUMED)).one()


@pytest.mark.parametrize("damage", ["result", "input", "new_script", "new_qc", "parent_unconsumed", "video", "voice", "audio",
                                     "receipt", "deployment", "UNKNOWN", "AUTHORIZED", "publication"])
@pytest.mark.parametrize("terminal", ["rejected"], indirect=True)
def test_rejected_plan_maintenance_rechecks_frozen_source(terminal, damage):
    runtime, content_id, recovery_id, qc_id = terminal
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, qc_id)
        plan_id = qc.input_data["upstream_task_ids"]["visual_plan"]
        if damage in {"UNKNOWN", "AUTHORIZED"}:
            record_intent(session, content_id, damage)
        elif damage == "publication":
            session.add(PublicationRecord(content_id=content_id, mode=list(PublicationMode)[0], platform="douyin"))
        else:
            qc_recovery.damage_source(session, runtime, content_id, qc_id, plan_id,
                                      Path(session.get(Content, content_id).content_dir), damage)
        session.commit()
    with pytest.raises((TaskConflictError, ValueError)):
        runtime.consume_agent(recovery_id)
    assert runtime.maintenance.run(now=utc_now() + INTERVAL)["skipped"] == 0
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery_blocked"


@pytest.mark.parametrize("terminal", ["rejected"], indirect=True)
@pytest.mark.parametrize("damage", ["pending_sibling", "missing_archive", "archive_hash", "archive_actor", "source_result",
                                     "video", "voice", "audio", "receipt", "image", "rights", "candidate_title", "research", "source",
                                     "script_qc", "plan_qc", "asset_qc"])
def test_archived_rejection_idempotence_requires_unchanged_terminal_scope(terminal, damage):
    runtime, content_id, recovery_id, qc_id = terminal
    assert runtime.maintenance.run(now=utc_now() + INTERVAL)["skipped"] == 1
    assert runtime.consume_agent(recovery_id) is False
    with Session(runtime.engine) as session:
        if damage == "pending_sibling":
            runtime.task_service(session).create_task(content_id=content_id, task_type=TaskType.ASSET_GENERATE_IMAGE,
                                                       instructions="new unrelated pending sibling", input_data={"fixture": True})
        elif damage == "source_result":
            source = session.get(AgentTask, qc_id)
            source.result_data = {**source.result_data, "changed": True}
            session.add(source)
        elif damage in {"video", "voice", "audio", "receipt"}:
            source = session.get(AgentTask, qc_id)
            qc_recovery.damage_source(session, runtime, content_id, qc_id, source.input_data["upstream_task_ids"]["visual_plan"],
                                      Path(session.get(Content, content_id).content_dir), damage)
        elif damage == "image":
            (Path(session.get(Content, content_id).content_dir) / "image.png").write_bytes(b"changed")
        elif damage in {"rights", "candidate_title"}:
            candidate = session.get(Candidate, session.get(Content, content_id).candidate_id)
            if damage == "rights":
                candidate.rights_note = "changed rights registration"
            else:
                candidate.original_title = "changed original title"
            session.add(candidate)
        elif damage == "source":
            candidate = session.get(Candidate, session.get(Content, content_id).candidate_id)
            session.add(Source(id=candidate.source_id, name="new registration", source_type=list(SourceType)[0],
                               url="https://example.org/changed-source"))
        elif damage in {"research", "script_qc", "plan_qc", "asset_qc"}:
            kind = {"research": TaskType.CONTENT_RESEARCH, "script_qc": TaskType.SCRIPT_QC,
                    "plan_qc": TaskType.VISUAL_PLAN_QC, "asset_qc": TaskType.ASSET_QC}[damage]
            parent = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == kind)).one()
            parent.result_data = {**parent.result_data, "changed": True}
            session.add(parent)
        else:
            archive = session.exec(select(AuditEvent).where(AuditEvent.entity_id == content_id,
                                                            AuditEvent.action == "handsfree_content_skipped")).one()
            if damage == "missing_archive":
                session.delete(archive)
            elif damage == "archive_hash":
                archive.metadata_json = {**archive.metadata_json, "result_hash": "tampered"}
                session.add(archive)
            else:
                archive.actor_id = "assistant"
                session.add(archive)
        session.commit()
    with pytest.raises((TaskConflictError, ValueError)):
        runtime.consume_agent(recovery_id)


@pytest.mark.parametrize("terminal", ["rejected"], indirect=True)
def test_archived_receipt_does_not_change_default_acceptance_or_allow_published(terminal):
    runtime, content_id, recovery_id, qc_id = terminal
    assert runtime.maintenance.run(now=utc_now() + INTERVAL)["skipped"] == 1
    assert runtime.consume_agent(recovery_id) is False
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, qc_id)
        refs = {**source.input_data["upstream_task_ids"], "video_artifact_id": source.input_data["video_artifact_id"]}
        with pytest.raises(ValueError, match="acceptance content missing or terminal"):
            verify_acceptance(session, source, source.result_data)
        with pytest.raises(ValueError, match="acceptance content missing or terminal"):
            acceptance_subject(session, content_id, refs)
        content = session.get(Content, content_id)
        content.business_status = ContentStatus.PUBLISHED
        session.add(content)
        session.commit()
        with pytest.raises(ValueError, match="acceptance content missing or terminal"):
            verify_acceptance(session, source, source.result_data, _archived_revalidation=True)
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery_id)


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
