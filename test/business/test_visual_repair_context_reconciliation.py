"""A narrow compensation transaction preserves accepted history and the Recovery budget."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus,
    PublicationMode, PublicationRecord, SchedulerOffer, TaskStatus, TaskType, utc_now,
)
from glance_core.task_schemas import RecoveryPlan
from glance_core.task_service import TaskConflictError, stable_hash
from sqlmodel import Session, select
from sqlalchemy import event

from app.business.content_run import ContentRun, load_run, save_run
from app.business.runtime import AGENT_CONSUMED, OWNED, VISUAL_REPAIR_CONTEXT
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN
from test.business.test_visual_plan_materialization import (
    failed_image_contract, image_contract,  # noqa: F401 -- pytest fixture dependencies
    record_accepted_completion,
)


def consumed(session, task):
    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                           entity_id=task.id, action=AGENT_CONSUMED,
                           metadata_json={"result_hash": stable_hash(task.result_data)}))


@pytest.fixture
def reconciliation(failed_image_contract, tmp_path):  # noqa: F811 -- imported pytest fixture
    runtime, content_id, previous_id, generation_id, source_id, issue, image_ids = failed_image_contract
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        previous, source = session.get(AgentTask, previous_id), session.get(AgentTask, source_id)
        old_qc = flow.create_agent_task(content_id, TaskType.CONTENT_QC, {}, allow_multiple=True)
        old_issues = [{"severity": "error", "field": "audio", "message": "old audio review"}]
        record_accepted_completion(session, runtime, old_qc, {"passed": False, "issues": old_issues})
        consumed(session, old_qc)
        previous.input_data = {**previous.input_data, "previous_qc_task_id": old_qc.id,
                               "qc_issues": old_issues, "voice_artifact_id": "frozen-voice",
                               "visual_duration_sec": 21.0}
        previous.input_hash = stable_hash(previous.input_data)
        owned = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == previous_id, AuditEvent.action == OWNED,
        )).one()
        owned.metadata_json = {"input_hash": previous.input_hash}
        consumed(session, source)
        signature = flow._root_cause_signature("BUSINESS_QC", "ASSET_QC_FAILED")
        prior = flow.create_agent_task(content_id, TaskType.RECOVERY, {
            "failure": {"source_task_id": old_qc.id, "failure_class": "BUSINESS_QC", "code": "ASSET_QC_FAILED"},
            "lineage": {"round": 1, "root_cause_signature": signature},
        }, allow_multiple=True)
        record_accepted_completion(session, runtime, prior, {
            "failure_class": "BUSINESS_QC", "action": "escalate_to_human", "reason": "old fixture recovery",
        })
        prior.created_at = utc_now() - timedelta(minutes=1)
        consumed(session, prior)
        session.add_all([previous, owned])
        session.commit()
        recovery = flow.create_qc_recovery_task(source, previous)
        assert recovery is not None
        result = {"failure_class": "BUSINESS_QC", "action": "create_repair_task",
                  "target_task_type": "visual_plan", "reason": "repair indexed continuity",
                  "must_preserve": ["accepted script and voice"], "must_not_do": ["publish"]}
        record_accepted_completion(session, runtime, recovery, result)
        consumed(session, recovery)
        session.commit()
        assert recovery.input_data["lineage"]["round"] == 2
        correct = flow.visual_repair_input(previous, recovery, RecoveryPlan.model_validate(result), source=source)
        legacy = deepcopy(correct)
        legacy.update(reusable_generated_assets=[], original_reuse=False,
                      reusable_assets_evidence_gaps=["reuse: failed segment has no exact index"],
                      excluded_generated_asset_ids=[], allowed_asset_ids=[], context_files=[])
        legacy.update(previous_qc_task_id=old_qc.id, qc_issues=old_issues)
        flow.invalidate_visual_plan(content_id, include_review_tasks=True, reason="recovery-visual-repair")
        bad = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, legacy, allow_multiple=True)
        record_accepted_completion(session, runtime, bad, deepcopy(previous.result_data))
        consumed(session, bad)
        qc = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN_QC, {
            "upstream_task_ids": {"visual_plan": bad.id, "script": previous.input_data["upstream_task_ids"]["script"]},
            "scenes": bad.input_data.get("scenes", []),
            "voice_duration_sec": bad.input_data.get("voice_duration_sec"),
            "allowed_layouts": bad.input_data.get("allowed_layouts", []),
            "allowed_asset_ids": bad.input_data.get("allowed_asset_ids", []),
            "context_files": bad.input_data.get("context_files", []),
        }, priority=55, allow_multiple=True)
        content = session.get(Content, content_id)
        content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "visual_plan_qc"
        now = utc_now()
        run = ContentRun(run_id="run_" + "a" * 32, content_id=content_id, generation=26,
                         state="stopped", stop_requested=True, stop_reason="operator_stop",
                         created_at=(now - timedelta(minutes=10)).isoformat(),
                         expires_at=(now + timedelta(minutes=110)).isoformat(),
                         host_epoch=1, parent_thread_id="fixture-parent", process_identity={
                             "pid": 1234, "started_at": (now - timedelta(hours=1)).isoformat(),
                             "instance_id": "fixture-host", "binary_path": str(Path(runtime.settings.data_dir) / "host.exe"),
                             "binary_sha256": "a" * 64, "version": "fixture",
                         }, host_observed_at=(now - timedelta(minutes=5)).isoformat(),
                         intents={"old-child": {"state": "TERMINAL", "host_epoch": 1,
                                                "offer_hash": "accepted-offer", "child": None, "claim_id": None}})
        save_run(session, run, "mpt_content_run_drained")
        session.add(content)
        session.commit()
        expected = {"run": {key: getattr(run, key) for key in (
            "run_id", "generation", "host_epoch", "parent_thread_id", "process_identity",
        )}}
        for name, task in (("recovery", recovery), ("plan", bad), ("qc", qc)):
            expected[name] = {"task_id": task.id, "input_hash": task.input_hash}
            if name != "qc":
                expected[name]["result_hash"] = stable_hash(task.result_data)
        ids = {"previous": previous_id, "generation": generation_id, "source": source_id,
               "recovery": recovery.id, "bad": bad.id, "qc": qc.id, "prior": prior.id}
    with TestClient(app_for(runtime, tmp_path)) as http:
        yield runtime, http, content_id, expected, ids, image_ids


def frozen_history(runtime):
    with Session(runtime.engine) as session:
        tasks = {task.id: deepcopy((task.input_data, task.input_hash, task.result_data, task.attempt,
                                    task.max_attempts, task.completed_at))
                 for task in session.exec(select(AgentTask)).all()}
        submissions = {row.id: row.model_dump() for row in session.exec(select(AgentSubmission)).all()}
        raw = {row.raw_result_path: Path(row.raw_result_path).read_bytes() if Path(row.raw_result_path).exists() else None
               for row in session.exec(select(AgentSubmission)).all()}
        receipts = {row.id: row.model_dump() for row in session.exec(select(AuditEvent).where(
            AuditEvent.action.in_(["completed", AGENT_CONSUMED, OWNED, "recovery_task_created"]),
        )).all()}
    return tasks, submissions, raw, receipts


def assert_frozen(runtime, before):
    after = frozen_history(runtime)
    for old, new in zip(before, after, strict=True):
        assert all(new[key] == value for key, value in old.items())


def endpoint(content_id):
    return f"/api/v1/contents/{content_id}/reconcile-visual-repair-context"


def test_reconciliation_supersedes_only_unexecuted_branch_and_replays(reconciliation):
    runtime, http, content_id, expected, ids, image_ids = reconciliation
    before = frozen_history(runtime)
    assert http.post(endpoint(content_id), json=expected).status_code == 401
    response = http.post(endpoint(content_id), headers=ADMIN, json=expected)
    assert response.status_code == 200, response.text
    new_id = response.json()["visual_plan_task_id"]
    with Session(runtime.engine) as session:
        new = session.get(AgentTask, new_id)
        assert new.status == TaskStatus.PENDING and new.attempt == 0
        assert new.input_data["recovery_task_id"] == ids["recovery"]
        assert new.input_data["previous_plan_task_id"] == ids["previous"]
        assert new.input_data["previous_qc_task_id"] == ids["source"]
        assert new.input_data["supersedes_task_id"] == ids["bad"]
        assert new.input_data["visual_repair_context_version"] == 1
        assert new.input_data["voice_artifact_id"] == "frozen-voice"
        assert new.input_data["visual_duration_sec"] == 21.0
        assert [row["generation_segment_index"] for row in new.input_data["reusable_generated_assets"]] == [0, 1, 3, 4, 5, 6]
        assert set(new.input_data["allowed_asset_ids"]) == set(image_ids) - {image_ids[2]}
        assert new.input_data["excluded_generated_asset_ids"] == [image_ids[2]]
        assert new.input_data["reusable_assets_evidence_gaps"] == []
        assert session.get(AgentTask, ids["qc"]).status == TaskStatus.CANCELLED
        assert session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == ids["qc"], AuditEvent.action == OWNED,
        )).all() == []
        assert session.get(AgentTask, ids["bad"]).status == TaskStatus.CANCELLED
        assert session.get(Content, content_id).current_stage == "visual_plan"
        recovery = session.get(AgentTask, ids["recovery"])
        assert recovery.input_data["lineage"]["round"] == 2
        assert recovery.input_data["lineage"]["max_rounds"] == 3
        flow = runtime.workflow_service(session)
        assert flow._create_visual_repair_from_recovery(
            recovery, RecoveryPlan.model_validate(recovery.result_data), target_task=session.get(AgentTask, ids["previous"]),
        ).id == new_id
        # Replay remains valid after ordinary execution starts or the host resumes.
        new.status, new.attempt = TaskStatus.LEASED, 1
        session.add(new)
        run = load_run(session)
        run.run_id, run.generation = "run_" + "b" * 32, run.generation + 1
        run.state, run.stop_requested = "running", False
        run.host_observed_at = utc_now().isoformat()
        save_run(session, run, "mpt_content_run_started")
        session.commit()
    assert_frozen(runtime, before)
    writes = []

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE", "REPLACE"}:
            writes.append(statement)

    event.listen(runtime.engine, "before_cursor_execute", record_write)
    try:
        replay = http.post(endpoint(content_id), headers=ADMIN, json=expected)
    finally:
        event.remove(runtime.engine, "before_cursor_execute", record_write)
    assert writes == []
    assert replay.status_code == 200, replay.text
    assert replay.json()["visual_plan_task_id"] == new_id and replay.json()["replayed"]
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == VISUAL_REPAIR_CONTEXT)).all()) == 1
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.RECOVERY)).all()) == 2


@pytest.mark.parametrize("damage", [
    "run_binding", "draining", "host_live", "child_unknown", "active_offer", "leased", "submitted",
    "qc_attempt", "qc_assignment", "qc_submission", "qc_hash", "descendant_generation", "descendant_render",
    "publish_record", "publish_intent", "source_raw", "source_submission", "source_consumed", "source_owned",
    "source_human_cancel", "previous_raw", "generation_raw", "artifact_bytes", "artifact_origin",
    "recovery_raw", "recovery_consumed", "recovery_origin", "recovery_budget", "source_issues",
    "bad_raw", "bad_consumed", "bad_input", "later_recovery",
])
def test_reconciliation_rejects_changed_authority_without_partial_mutation(reconciliation, damage):
    runtime, http, content_id, expected, ids, image_ids = reconciliation
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        qc = session.get(AgentTask, ids["qc"])
        if damage == "run_binding":
            expected["run"]["generation"] += 1
        elif damage in {"draining", "host_live", "child_unknown"}:
            run = load_run(session)
            if damage == "draining":
                run.state = "draining"
            elif damage == "host_live":
                run.host_observed_at = utc_now().isoformat()
            else:
                run.intents["old-child"]["state"] = "UNKNOWN"
            save_run(session, run, "fixture-damage")
        elif damage == "active_offer":
            session.add(SchedulerOffer(request_id="active", task_id=qc.id, expires_at=utc_now() + timedelta(minutes=1)))
        elif damage in {"leased", "submitted", "qc_attempt", "qc_assignment", "qc_submission", "qc_hash"}:
            if damage in {"leased", "submitted"}:
                qc.status = TaskStatus.LEASED if damage == "leased" else TaskStatus.SUBMITTED
            elif damage == "qc_attempt":
                qc.attempt = 1
            elif damage == "qc_assignment":
                qc.assigned_agent_id = "already-claimed"
            elif damage == "qc_submission":
                session.add(AgentSubmission(task_id=qc.id, attempt=0, agent_id="intruder", raw_result_path="missing",
                                            result_hash="a" * 64, validation_status="REJECTED"))
            else:
                qc.input_data = {**qc.input_data, "changed": True}
            session.add(qc)
        elif damage.startswith("descendant_"):
            kind = TaskType.ASSET_GENERATE_IMAGE if damage.endswith("generation") else TaskType.CONTENT_QC
            flow.create_agent_task(content_id, kind, {"upstream_task_ids": {"visual_plan": ids["bad"]}}, allow_multiple=True)
        elif damage == "publish_record":
            session.add(PublicationRecord(content_id=content_id, platform="douyin", mode=PublicationMode.EXECUTOR))
        elif damage == "publish_intent":
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent",
                                   entity_id="attempted", action="publish_intent_state", metadata_json={"content_id": content_id}))
        elif damage.endswith("_raw"):
            name = damage.removesuffix("_raw")
            task = session.get(AgentTask, ids[name])
            row = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
            Path(row.raw_result_path).write_text('{"changed": true}', encoding="utf-8")
        elif damage in {"source_submission", "source_consumed", "source_owned", "bad_consumed", "recovery_consumed"}:
            name = damage.split("_")[0]
            if damage == "source_submission":
                row = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == ids[name])).one()
            else:
                action = OWNED if damage.endswith("owned") else AGENT_CONSUMED
                row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == ids[name], AuditEvent.action == action)).one()
            session.delete(row)
        elif damage == "source_human_cancel":
            row = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == ids["source"], AuditEvent.action == "cancelled",
            )).one()
            row.actor_type = "human"
            session.add(row)
        elif damage == "recovery_origin":
            row = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == ids["recovery"], AuditEvent.action == "recovery_task_created",
            )).one()
            row.metadata_json = {**row.metadata_json, "round": 3}
            session.add(row)
        elif damage in {"recovery_budget", "source_issues", "bad_input"}:
            name = {"recovery_budget": "recovery", "source_issues": "source", "bad_input": "bad"}[damage]
            task = session.get(AgentTask, ids[name])
            if damage == "source_issues":
                task.result_data = {"passed": False, "issues": []}
            else:
                task.input_data = {**task.input_data, "unexpected": True}
            session.add(task)
        elif damage == "later_recovery":
            flow.create_agent_task(content_id, TaskType.RECOVERY, {"newer": True}, allow_multiple=True)
        else:
            artifact = session.get(Artifact, image_ids[0])
            if damage == "artifact_bytes":
                content = session.get(Content, content_id)
                (Path(content.content_dir) / artifact.relative_path).write_bytes(b"changed")
            else:
                artifact.producer_stage = ids["bad"]
                session.add(artifact)
        session.commit()
    before = frozen_history(runtime)
    with Session(runtime.engine) as session:
        statuses = {task.id: task.status for task in session.exec(select(AgentTask)).all()}
    response = http.post(endpoint(content_id), headers=ADMIN, json=expected)
    assert response.status_code == 409, response.text
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert {task.id: task.status for task in session.exec(select(AgentTask)).all()} == statuses
        assert session.exec(select(AuditEvent).where(AuditEvent.action == VISUAL_REPAIR_CONTEXT)).all() == []


def test_reconciliation_rolls_back_creation_and_cancellation(reconciliation, monkeypatch):
    runtime, http, content_id, expected, ids, _ = reconciliation
    before = frozen_history(runtime)
    original = Session.add

    def fail_receipt(session, row, *args, **kwargs):
        if isinstance(row, AuditEvent) and row.action == VISUAL_REPAIR_CONTEXT:
            raise OSError("synthetic reconciliation receipt failure")
        return original(session, row, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Session, "add", fail_receipt)
        assert http.post(endpoint(content_id), headers=ADMIN, json=expected).status_code == 409
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, ids["qc"]).status == TaskStatus.PENDING
        assert session.get(AgentTask, ids["bad"]).status == TaskStatus.COMPLETED
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN)).all()) == 2
    assert http.post(endpoint(content_id), headers=ADMIN, json=expected).status_code == 200


def test_reconciliation_concurrent_replay_creates_one_plan(reconciliation):
    runtime, _, content_id, expected, _, _ = reconciliation
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.reconcile_visual_repair_context(content_id, expected), range(2)))
    assert results[0]["visual_plan_task_id"] == results[1]["visual_plan_task_id"]
    assert sorted(row["replayed"] for row in results) == [False, True]


def test_reconciliation_replay_rejects_conflicting_binding_and_new_input_tamper(reconciliation):
    runtime, http, content_id, expected, _, _ = reconciliation
    result = http.post(endpoint(content_id), headers=ADMIN, json=expected).json()
    changed = deepcopy(expected)
    changed["run"]["generation"] += 1
    assert http.post(endpoint(content_id), headers=ADMIN, json=changed).status_code == 409
    with Session(runtime.engine) as session:
        new = session.get(AgentTask, result["visual_plan_task_id"])
        new.input_data = {**new.input_data, "changed": True}
        session.add(new)
        session.commit()
    with pytest.raises(TaskConflictError):
        runtime.reconcile_visual_repair_context(content_id, expected)


def test_reconciliation_request_accepts_only_expected_bindings(reconciliation):
    _, http, content_id, expected, _, _ = reconciliation
    for payload in ({**expected, "new_input": {}}, {**expected, "run": {**expected["run"], "generation": True}},
                    {**expected, "qc": {**expected["qc"], "input_hash": "bad"}}):
        assert http.post(endpoint(content_id), headers=ADMIN, json=payload).status_code == 422


def test_reconciliation_allows_existing_exact_qc_ownership(reconciliation):
    runtime, http, content_id, expected, ids, _ = reconciliation
    with Session(runtime.engine) as session:
        session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                               entity_id=ids["qc"], action=OWNED,
                               metadata_json={"input_hash": expected["qc"]["input_hash"]}))
        session.commit()
    assert http.post(endpoint(content_id), headers=ADMIN, json=expected).status_code == 200
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == ids["qc"], AuditEvent.action == OWNED,
        )).all()) == 1


@pytest.mark.parametrize("damage", [
    "missing_created", "duplicate_created", "human_created", "created_metadata", "execution_event",
    "wrong_owned_hash", "derived_input", "prepared_input",
])
def test_reconciliation_requires_unexecuted_exact_pending_qc_provenance(reconciliation, damage):
    runtime, http, content_id, expected, ids, _ = reconciliation
    with Session(runtime.engine) as session:
        created = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == ids["qc"], AuditEvent.action == "created",
        )).one()
        if damage == "missing_created":
            session.delete(created)
        elif damage == "duplicate_created":
            session.add(AuditEvent(actor_type="system", actor_id="task-service", entity_type="agent_task",
                                   entity_id=ids["qc"], action="created"))
        elif damage in {"human_created", "created_metadata"}:
            if damage == "human_created":
                created.actor_type = "human"
            else:
                created.metadata_json = {"forged": True}
            session.add(created)
        elif damage == "execution_event":
            session.add(AuditEvent(actor_type="agent", actor_id="claimed-worker", entity_type="agent_task",
                                   entity_id=ids["qc"], action="claimed"))
        elif damage == "wrong_owned_hash":
            session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                   entity_id=ids["qc"], action=OWNED, metadata_json={"input_hash": "a" * 64}))
        else:
            qc = session.get(AgentTask, ids["qc"])
            qc.input_data = {**qc.input_data, **(
                {"allowed_asset_ids": ["undeclared-asset"]} if damage == "derived_input"
                else {"prepared_visual_plan": {"segments": []}}
            )}
            qc.input_hash = stable_hash(qc.input_data)
            expected["qc"]["input_hash"] = qc.input_hash
            session.add(qc)
        session.commit()
    before = frozen_history(runtime)
    response = http.post(endpoint(content_id), headers=ADMIN, json=expected)
    assert response.status_code == 409, response.text
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, ids["qc"]).status == TaskStatus.PENDING
        assert session.exec(select(AuditEvent).where(AuditEvent.action == VISUAL_REPAIR_CONTEXT)).all() == []


def refreeze_fixture(session, task):
    """Keep generic receipts valid so negative tests exercise the domain checks."""
    task.input_hash = stable_hash(task.input_data)
    for row in session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).all():
        row.result_hash = stable_hash(task.result_data)
        Path(row.raw_result_path).write_text(json.dumps(task.result_data), encoding="utf-8")
        session.add(row)
    for row in session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == task.id, AuditEvent.action.in_([OWNED, "completed", AGENT_CONSUMED]),
    )).all():
        row.metadata_json = {"input_hash": task.input_hash} if row.action == OWNED else {
            "result_hash": stable_hash(task.result_data),
        }
        session.add(row)
    session.add(task)


@pytest.mark.parametrize("damage", [
    "round", "max_rounds", "allowed_action", "root", "retry_budget", "failure_issues",
    "unindexed", "bool_index", "unknown_field", "source_segment", "unrelated_context", "current_qc",
])
def test_reconciliation_rejects_semantic_drift_even_with_matching_receipts(reconciliation, damage):
    runtime, http, content_id, expected, ids, _ = reconciliation
    with Session(runtime.engine) as session:
        recovery = session.get(AgentTask, ids["recovery"])
        source = session.get(AgentTask, ids["source"])
        bad = session.get(AgentTask, ids["bad"])
        recovery.input_data = deepcopy(recovery.input_data)
        if damage == "round":
            recovery.input_data["lineage"]["round"] = 3
        elif damage == "max_rounds":
            recovery.input_data["policy"]["max_rounds"] = 1
        elif damage == "allowed_action":
            recovery.input_data["allowed_actions"] = []
        elif damage == "root":
            recovery.input_data["lineage"]["root_task_id"] = ids["prior"]
        elif damage == "retry_budget":
            recovery.input_data["lineage"]["same_task_retries"] = 1
        elif damage == "failure_issues":
            recovery.input_data["failure"]["validation_errors"][0]["segment_index"] = 3
        elif damage in {"unindexed", "bool_index", "unknown_field"}:
            issue = recovery.input_data["failure"]["validation_errors"][0]
            if damage == "unindexed":
                issue.pop("segment_index")
            elif damage == "bool_index":
                issue["segment_index"] = True
            else:
                issue["field"] = "unknown"
            source.result_data = {"passed": False, "issues": [deepcopy(issue)]}
            refreeze_fixture(session, source)
        elif damage == "source_segment":
            source.input_data = deepcopy(source.input_data)
            source.input_data["assets"][0]["segment_index"] = 6
            refreeze_fixture(session, source)
            # Update the fingerprint too; refusal must come from material identity.
            recovery.input_data["lineage"]["repair_fingerprint"] = runtime.workflow_service(session)._repair_fingerprint(
                source, failure_class="BUSINESS_QC", code="ASSET_QC_FAILED",
            )
        else:
            bad.input_data = {**bad.input_data, **(
                {"reviewer_note": "unrelated new business instruction"} if damage == "unrelated_context"
                else {"previous_qc_task_id": source.id}
            )}
            refreeze_fixture(session, bad)
            expected["plan"]["input_hash"] = bad.input_hash
        refreeze_fixture(session, recovery)
        expected["recovery"]["input_hash"] = recovery.input_hash
        session.commit()
    before = frozen_history(runtime)
    response = http.post(endpoint(content_id), headers=ADMIN, json=expected)
    assert response.status_code == 409, response.text
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, ids["qc"]).status == TaskStatus.PENDING
        assert session.get(AgentTask, ids["bad"]).status == TaskStatus.COMPLETED
