"""Real leases, origin validation and SDK downloads reproduce the historical whitelist defect."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import zipfile

import httpx
import pytest
from fastapi.testclient import TestClient
from glance_agent import cli
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, PublicationMode, PublicationRecord, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_service import stable_hash
from sqlmodel import Session, select

from app.business import pipeline
from app.business.conductor_launch import LaunchRecord, _save
from app.business.content_run import ContentRun, save_run
from app.business.visual_plan_context import ORIGIN_FAILURE, RECONCILED
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN
from test.business.test_kokoro_voice_rebuild import contract as contract, prove_completed, sha
from test.business.test_packaged_script_revision import NOTE


def complete_fixture(session, runtime, task, result, agent_id=None):
    task.status, task.completed_at, task.result_data = TaskStatus.COMPLETED, utc_now(), result
    prove_completed(session, runtime, task)
    if agent_id:
        task.assigned_agent_id = agent_id
        submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
        submission.agent_id = agent_id
        event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id, AuditEvent.action == "completed")).one()
        event.actor_id = agent_id
        session.add_all([task, submission, event])
        session.commit()


@pytest.fixture
def approved_context(contract, monkeypatch, tmp_path):
    runtime, _, ids, calls, _, bundle = contract
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    with TestClient(app_for(runtime, tmp_path)) as http:
        script_client = registered_client(http, runtime, "script")
        visual = registered_client(http, runtime, "visual")
        assistant = registered_client(http, runtime, "assistant")
        assert http.post(f"/api/v1/contents/{ids['content']}/reject-script", json={"note": NOTE}, headers=ADMIN).status_code == 200
        with Session(runtime.engine) as session:
            script = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                         AgentTask.status == TaskStatus.PENDING)).one()
            complete_fixture(session, runtime, script, {"full_narration": "主打共享专注工作舱。它的占地面积是2.5平方米。",
                                                        "estimated_duration_sec": 30}, script_client.settings.agent_id)
            script_id = script.id
        pipeline.advance_after_agent(runtime.engine, runtime.settings, script_id)
        with Session(runtime.engine) as session:
            qc = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCRIPT_QC, AgentTask.status == TaskStatus.PENDING)).one()
            complete_fixture(session, runtime, qc, {"passed": True})
            qc_id = qc.id
        pipeline.advance_after_agent(runtime.engine, runtime.settings, qc_id)
        with Session(runtime.engine) as session:
            assist = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCRIPT_REVIEW_ASSIST,
                                                         AgentTask.status == TaskStatus.PENDING)).one()
            complete_fixture(session, runtime, assist, {"verdict": "APPROVE", "confidence": "high", "summary": "Reviewed",
                                                        "reasons": ["Facts unchanged"], "issues": [], "checklist": []})
            content = session.get(Content, ids["content"])
            invalid_ids = []
            for index in range(26):
                path = Path(content.content_dir) / f"images/unaccepted-{index}.png"
                path.write_bytes(f"unaccepted historical upload {index}".encode())
                artifact = Artifact(content_id=content.id, artifact_type="agent_generated_image", relative_path=path.relative_to(content.content_dir).as_posix(),
                                    sha256=sha(path.read_bytes()), size_bytes=path.stat().st_size, mime_type="image/png", producer_stage="agt_unaccepted_history")
                session.add(artifact)
                invalid_ids.append(artifact.id)
            session.commit()
        response = http.post(f"/api/v1/contents/{ids['content']}/approve-script", headers=ADMIN)
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            plan = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN, AgentTask.status == TaskStatus.PENDING)).one()
            plan_id = plan.id
        yield runtime, http, ids, calls, visual, assistant, plan_id, invalid_ids


def assert_downloaded_context(client, claim, bundle, ids, workspace):
    assert bundle["input"]["allowed_asset_ids"] == ids["images"]
    assert len(bundle["context_files"]) == 8
    assert {row["input_ref"] for row in bundle["context_manifest"] if row.get("input_ref") in ids["images"]} == set(ids["images"])
    (workspace / "context").mkdir(parents=True)
    cli._download_context(client, bundle, claim["lease_token"], workspace)
    assert cli._manifest_gaps(bundle, workspace) == []
    receipt = workspace / "context/tts_generation_receipt.json"
    assert json.loads(receipt.read_bytes())["provider"] == "kokoro"
    assert hashlib.sha256(receipt.read_bytes()).hexdigest() == bundle["input"]["voice_generation"]["receipt_artifact"]["sha256"]
    archive = workspace / "context.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for row in bundle["context_files"]:
            output.write(workspace / "context" / row["name"], row["name"])
    with zipfile.ZipFile(archive) as output:
        assert len(output.namelist()) == 8
        assert output.read("tts_generation_receipt.json") == receipt.read_bytes()


def test_normal_revision_indexes_only_previous_seven_and_signed_voice_receipt(approved_context, tmp_path):
    _, _, ids, calls, visual, _, plan_id, _ = approved_context
    claim = visual.claim()
    assert claim["task_id"] == plan_id
    bundle = visual.bundle(claim["bundle_url"], claim["lease_token"])
    assert_downloaded_context(visual, claim, bundle, ids, tmp_path / "future-bundle")
    assert len(calls) == 1


@pytest.fixture
def failed_context(approved_context):
    runtime, http, ids, calls, visual, assistant, plan_id, invalid_ids = approved_context
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        valid = session.get(AgentTask, plan_id)
        legacy_input = {**deepcopy(valid.input_data), "allowed_asset_ids": [*invalid_ids, *ids["images"]],
                        "context_files": [valid.input_data["voice_generation"]["receipt_artifact"]["relative_path"]]}
        flow.tasks.cancel(valid.id, actor_id="reproduce-legacy-producer", actor_type="system")
        bad = flow.create_agent_task(ids["content"], TaskType.VISUAL_PLAN, legacy_input, allow_multiple=True)
        bad_id = bad.id
    claim = visual.claim()
    assert claim["task_id"] == bad_id
    visual.confirm_claim(bad_id, claim["lease_token"], visual.settings.agent_id)
    with pytest.raises(httpx.HTTPStatusError) as failure:
        visual.bundle(claim["bundle_url"], claim["lease_token"])
    assert failure.value.response.status_code == 409
    assert failure.value.response.json()["detail"] == "generated image origin unavailable"
    failed = visual.fail(bad_id, claim["lease_token"], visual.settings.agent_id, retryable=False,
                         error_code="BUNDLE_HANDOFF_ERROR", message=ORIGIN_FAILURE)
    recovery_id = failed["recovery_task_id"]
    claimed = assistant.claim()
    assert claimed["task_id"] == recovery_id
    assistant.confirm_claim(recovery_id, claimed["lease_token"], assistant.settings.agent_id)
    response = assistant.complete(recovery_id, claimed["lease_token"], assistant.settings.agent_id, {
        "failure_class": "CONTEXT_MISSING", "action": "repair_task_context", "target_task_type": "visual_plan",
        "reason": "Historical uploads lack accepted origins; rebuild exact resolved image context.",
        "must_preserve": ["current approved script and signed voice"], "must_not_do": ["retry identical input or publish"],
    })
    assert response["status"] == "COMPLETED"
    runtime.consume_agent(recovery_id)
    with Session(runtime.engine) as session:
        assert (session.get(Content, ids["content"]).business_status,
                session.get(Content, ids["content"]).current_stage) == (ContentStatus.FAILED, "recovery_blocked")
        now = utc_now()
        process = {"pid": 1234, "started_at": (now - timedelta(hours=1)).isoformat(), "instance_id": "ended-host",
                   "binary_path": "C:/fixture/host.exe", "binary_sha256": "b" * 64, "version": "fixture"}
        run = ContentRun(run_id="run_" + "a" * 32, content_id=ids["content"], generation=36, state="stopped", stop_requested=True,
                         created_at=(now - timedelta(minutes=10)).isoformat(), expires_at=(now + timedelta(minutes=110)).isoformat(),
                         host_epoch=1, parent_thread_id="ended-parent", process_identity=process,
                         host_observed_at=(now - timedelta(minutes=5)).isoformat(),
                         intents={"ended": {"state": "TERMINAL", "host_epoch": 1, "offer_hash": "proof", "child": None, "claim_id": None}})
        save_run(session, run, "mpt_content_run_drained")
        execution = LaunchRecord(launch_id="ended-launch", content_id=ids["content"], run_id=run.run_id,
                                 generation=run.generation, state="FINISHED", created_at=run.created_at, exited_at=now.isoformat(), exit_code=0)
        _save(session, execution)
        expected = {"run": {field: getattr(run, field) for field in ("run_id", "generation", "host_epoch", "parent_thread_id", "process_identity")},
                    "execution_sha256": stable_hash(execution.model_dump())}
        for field, task_id in (("failed_plan", bad_id), ("recovery", recovery_id)):
            task = session.get(AgentTask, task_id)
            expected[field] = {"task_id": task.id, "input_hash": task.input_hash, "result_hash": stable_hash(task.result_data), "attempt": task.attempt}
            if field == "recovery":
                submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
                expected[field]["raw_file_sha256"] = sha(Path(submission.raw_result_path).read_bytes())
        session.commit()
    return runtime, http, ids, calls, visual, expected


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/reconcile-packaged-visual-context"


def frozen_history(runtime):
    with Session(runtime.engine) as session:
        return {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()}


def test_failed_context_reconciliation_preserves_all_history_and_sdk_downloads_exact_eight(failed_context, tmp_path):
    runtime, http, ids, calls, visual, expected = failed_context
    before = frozen_history(runtime)
    assert http.post(endpoint(ids), json=expected).status_code == 401
    response = http.post(endpoint(ids), json=expected, headers=ADMIN)
    assert response.status_code == 200, response.text
    new_id = response.json()["visual_plan_task_id"]
    after = frozen_history(runtime)
    assert all(after[key] == value for key, value in before.items()) and len(calls) == 1
    claim = visual.claim()
    assert claim["task_id"] == new_id
    bundle = visual.bundle(claim["bundle_url"], claim["lease_token"])
    assert_downloaded_context(visual, claim, bundle, ids, tmp_path / "repaired-bundle")
    with Session(runtime.engine) as session:
        old = session.get(AgentTask, expected["failed_plan"]["task_id"])
        new = session.get(AgentTask, new_id)
        for field in ("voice_artifact", "voice_generation", "voice_duration_sec", "visual_timing_evidence", "previous_plan", "script"):
            assert new.input_data.get(field) == old.input_data.get(field)
        assert new.input_data["supersedes_task_id"] == old.id
    replay = http.post(endpoint(ids), json=expected, headers=ADMIN)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["visual_plan_task_id"] == new_id and len(calls) == 1


@pytest.mark.parametrize("damage", ["binding", "execution", "active", "offer", "voice", "image", "raw", "approval", "recovery-action",
                                    "run", "child", "host-live", "execution-failed", "publication", "intent", "image-origin", "empty-result"])
def test_reconciliation_rejects_changed_subject_without_writes(failed_context, damage):
    runtime, http, ids, calls, _, expected = failed_context
    with Session(runtime.engine) as session:
        if damage == "binding":
            expected["failed_plan"]["input_hash"] = "f" * 64
        elif damage == "execution":
            expected["execution_sha256"] = "f" * 64
        elif damage == "empty-result":
            bad = session.get(AgentTask, expected["failed_plan"]["task_id"])
            bad.result_data = {}
            expected["failed_plan"]["result_hash"] = stable_hash({})
            session.add(bad)
        elif damage == "active":
            session.add(AgentTask(content_id=ids["content"], task_type=TaskType.RECOVERY, prompt_name="busy", instructions="busy",
                                  input_hash="busy", status=TaskStatus.LEASED))
        elif damage == "offer":
            session.add(SchedulerOffer(request_id="live", task_id=expected["failed_plan"]["task_id"], expires_at=utc_now() + timedelta(minutes=5)))
        elif damage in {"run", "child", "host-live"}:
            from app.business.content_run import load_run

            run = load_run(session)
            if damage == "run":
                run.state = "draining"
            elif damage == "child":
                run.intents = {"unresolved": {"state": "UNKNOWN", "host_epoch": 1, "offer_hash": "proof", "child": None, "claim_id": None}}
            else:
                run.host_observed_at = utc_now().isoformat()
            save_run(session, run, "fixture_changed_run")
        elif damage == "execution-failed":
            from app.business.conductor_launch import _load

            execution = _load(session, ids["content"])
            execution.state, execution.exit_code = "FAILED", 1
            _save(session, execution)
            expected["execution_sha256"] = stable_hash(execution.model_dump())
        elif damage == "publication":
            session.add(PublicationRecord(content_id=ids["content"], platform="douyin", mode=PublicationMode.EXECUTOR))
        elif damage == "intent":
            session.add(AuditEvent(actor_type="agent", actor_id="fixture", entity_type="publish_intent", entity_id="intent",
                                   action="publish_intent_registered", metadata_json={"content_id": ids["content"]}))
        elif damage == "image-origin":
            image = session.get(Artifact, ids["images"][0])
            task = session.get(AgentTask, image.producer_stage)
            task.status = TaskStatus.FAILED
            session.add(task)
        elif damage in {"voice", "image"}:
            bad = session.get(AgentTask, expected["failed_plan"]["task_id"])
            artifact_id = bad.input_data["voice_artifact"]["id"] if damage == "voice" else ids["images"][0]
            artifact = session.get(Artifact, artifact_id)
            (Path(session.get(Content, ids["content"]).content_dir) / artifact.relative_path).write_bytes(b"tampered")
        elif damage == "raw":
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == expected["recovery"]["task_id"])).one()
            Path(submission.raw_result_path).write_bytes(b"{}")
        elif damage == "approval":
            for event in session.exec(select(AuditEvent).where(AuditEvent.action == "manual_script_approved")).all():
                session.delete(event)
        else:
            task = session.get(AgentTask, expected["recovery"]["task_id"])
            task.result_data = {**task.result_data, "action": "retry_same_task"}
            session.add(task)
        session.commit()
    before = frozen_history(runtime)
    response = http.post(endpoint(ids), json=expected, headers=ADMIN)
    assert response.status_code == 409, response.text
    if damage == "empty-result":
        assert response.json()["detail"] == "PACKAGED_VISUAL_FAILURE_NOT_RECOGNIZED"
    assert frozen_history(runtime) == before and len(calls) == 1


def test_reconciliation_concurrent_response_loss_creates_one_plan(failed_context):
    runtime, _, ids, calls, _, expected = failed_context
    runtime.run_content_stage(ids["content"], lambda *args: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: runtime.reconcile_packaged_visual_context(ids["content"], expected), range(2)))
    assert len({row["visual_plan_task_id"] for row in responses}) == 1
    assert sorted(row["replayed"] for row in responses) == [False, True]
    assert len(calls) == 1
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == RECONCILED)).all()) == 1


def test_reconciliation_receipt_failure_rolls_back_replacement_and_transition(failed_context, monkeypatch):
    from app.business.runtime import _ConsumptionSession

    runtime, http, ids, calls, _, expected = failed_context
    original = _ConsumptionSession.add

    def fail_receipt(self, instance, *args, **kwargs):
        if isinstance(instance, AuditEvent) and instance.action == RECONCILED:
            raise OSError("receipt interrupted")
        return original(self, instance, *args, **kwargs)

    monkeypatch.setattr(_ConsumptionSession, "add", fail_receipt)
    before = frozen_history(runtime)
    response = http.post(endpoint(ids), json=expected, headers=ADMIN)
    assert response.status_code == 409, response.text
    assert frozen_history(runtime) == before and len(calls) == 1
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.FAILED
        assert not session.exec(select(AuditEvent).where(AuditEvent.action == RECONCILED)).all()
