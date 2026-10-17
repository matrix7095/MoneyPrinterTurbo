"""Explicit changed authority resumes the same image task with immutable history."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from glance_core.models import AgentTask, Artifact, AuditEvent, Content, ContentStatus, RuntimeSetting, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.runtime_config import effective_settings
from sqlmodel import Session, select

from app.business import conductor_launch as launch
from app.business.content_run import RUN_KEY, load_run, save_run, start_run
from app.business.image_authorization_recovery import ERROR, RESUMED
from app.business.pipeline import _record_manual_script_approval, _script_approval_snapshot
from app.controllers.v1.content_execution import router
from test.business.test_content_execution_http import Process
from test.business.test_content_qc_recovery import accept
from test.business.test_content_http import client_for
from test.business.test_content_run_http import attach_fixture_host, fixture_child, scoped_dispatch, selected, terminal_fixture_worker
from test.business.test_deployment_http import app_for, deploy, enable, scheduler, stage
from test.business.test_discovery_http import ADMIN, make_runtime


@pytest.fixture
def blocked(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    app = app_for(runtime, tmp_path)
    app.include_router(router)
    processes = []
    binary = tmp_path / "fixture.exe"
    binary.write_bytes(b"fixed-fixture")
    monkeypatch.setattr(launch, "launch_command", lambda *args: [str(binary)])
    monkeypatch.setattr(launch, "process_creation_time", lambda _: utc_now().isoformat())

    def popen(*args, **kwargs):
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    with TestClient(app) as http:
        enable(http, enabled=False)
        for role in ("image", "research", "script", "visual", "assistant"):
            deploy(http, stage(runtime, role))
        content_id, research_id = selected(http, runtime, "image-authorization-change")
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)

            def completed(kind, inputs, result, role):
                task = flow.create_agent_task(content_id, kind, inputs, allow_multiple=True)
                accept(session, runtime, task, result, f"mpt-codex-{role}", consumed=True)
                return task

            research = session.get(AgentTask, research_id)
            accept(session, runtime, research, {"verified": "furniture concept"}, "mpt-codex-research", consumed=True)
            script = completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}},
                               {"full_script": "家具概念示意"}, "script")
            completed(TaskType.SCRIPT_QC, {"upstream_task_ids": {"research": research.id, "script": script.id}}, {"passed": True}, "script")
            voice = Artifact(content_id=content_id, artifact_type="voice", relative_path="voice.mp3",
                             sha256=hashlib.sha256(b"voice").hexdigest(), size_bytes=5,
                             mime_type="audio/mpeg", producer_stage="voice")
            content = session.get(Content, content_id)
            from pathlib import Path
            root = Path(content.content_dir)
            root.mkdir(parents=True, exist_ok=True)
            (root / "voice.mp3").write_bytes(b"voice")
            session.add(voice)
            session.commit()
            voice_binding = {key: getattr(voice, key) for key in ("id", "relative_path", "sha256", "size_bytes")}
            request = {"prompt": "AI concept stool illustration", "aspect_ratio": "9:16", "min_width": 1080, "min_height": 1920}
            plan = completed(TaskType.VISUAL_PLAN, {"upstream_task_ids": {"script": script.id}, "voice_artifact": voice_binding,
                                                    "image_only_gen_budget": 8},
                             {"segments": [{"asset_request": request} for _ in range(7)]}, "visual")
            plan_qc = completed(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id}}, {"passed": True}, "visual")
            _record_manual_script_approval(session, runtime.settings, content_id, _script_approval_snapshot(session, content_id), voice_binding)
            session.commit()
            source = flow.create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {
                "upstream_task_ids": {"visual_plan": plan.id}, "script": script.result_data,
                "visual_plan": plan.result_data,
                "requests": [{"segment_index": index, "asset_request": request} for index in range(7)],
            }, allow_multiple=True)
            source_id, plan_id, plan_qc_id = source.id, plan.id, plan_qc.id
        old = http.post(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
        attach_fixture_host(http, old)
        worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=old["run_id"])["worker_requests"][0]
        assert worker["task_id"] == source_id
        response = scoped_dispatch(http, worker)
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            runtime.task_service(session).confirm_claim(task_id=source_id, lease_token=response.json()["lease_token"], agent_id=worker["agent_id"])
            source = session.get(AgentTask, source_id)
            assert source.attempt == 1 and source.attempt_started_at
            # Fail through the real task lifecycle; retain the confirmed native claim.
            claim_token = response.json()["lease_token"]
            runtime.task_service(session).fail(task_id=source_id, lease_token=claim_token,
                agent_id=worker["agent_id"], retryable=False, code=ERROR, message="Native output 941x1672 needs local size preparation")
        recovery_id = runtime.schedule_failure_recovery(source_id)["recovery_task_id"]
        with Session(runtime.engine) as session:
            recovery = session.get(AgentTask, recovery_id)
            accept(session, runtime, recovery, {"failure_class": "ENVIRONMENT", "action": "mark_recovery_blocked",
                "reason": "Frozen run has no local size adaptation authority", "must_change": [], "must_preserve": [],
                "must_not_do": [], "acceptance_checks": []}, "mpt-codex-assistant", consumed=True)
            runtime.workflow_service(session)._mark_recovery_blocked(recovery, reason="Authorization is missing", action="mark_recovery_blocked")
        terminal_fixture_worker(http, worker)
        http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN)
        with Session(runtime.engine) as session:
            old = load_run(session)
            old.host_observed_at = (utc_now() - timedelta(minutes=4)).isoformat()
            save_run(session, old, "fixture_host_expired")
            session.commit()
            record = launch.LaunchRecord(launch_id="original-launch", content_id=content_id, run_id=old.run_id,
                generation=old.generation, state="FINISHED", created_at=old.created_at, pid=123,
                process_started_at=old.created_at, exited_at=utc_now().isoformat(), exit_code=0)
            launch._save(session, record)
        directory = runtime.settings.data_dir / "private" / "content-runs" / old.run_id
        directory.mkdir(parents=True)
        journal = {key: getattr(old, key) for key in ("run_id", "content_id", "generation", "host_epoch", "parent_thread_id", "process_identity", "intents")}
        journal.update(version=1, phase="CONDUCTOR_STOP_REQUESTED")
        (directory / "state.json").write_text(json.dumps(journal), encoding="utf-8")
        events = [{"seq": index, "kind": "native", "method": method, "threadId": fixture_child(worker)["child_id"],
                   "turnId": "old-native-turn", "item": {"id": "old-image-call", "type": "imageGeneration", "status": status}}
                  for index, method, status in ((1, "item/started", "in_progress"), (2, "item/completed", "completed"))]
        (directory / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        yield runtime, http, content_id, source_id, recovery_id, old, processes, directory, plan_id, plan_qc_id
        for process in processes:
            process.done.set()
        deadline = time.monotonic() + 5
        while launch._handles and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not launch._handles


def snapshot(runtime):
    with Session(runtime.engine) as session:
        return {"tasks": {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()},
                "events": {row.id: row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()},
                "run": session.get(RuntimeSetting, RUN_KEY).value}


def execute(blocked, body=None):
    _, http, content_id, *_ = blocked
    return http.post(f"/api/v1/contents/{content_id}/run/execute", headers=ADMIN, json=body)


def test_changed_authority_resumes_same_task_atomically_and_claims_attempt_two(blocked):
    runtime, http, cid, source_id, recovery_id, old, processes, directory, *_ = blocked
    before, journal = snapshot(runtime), {path: path.read_bytes() for path in directory.iterdir()}
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["generation"] == old.generation + 1 and len(processes) == 1
    after = snapshot(runtime)
    assert after["tasks"][recovery_id] == before["tasks"][recovery_id]
    for key in ("input_data", "input_hash", "result_data", "attempt", "max_attempts", "instructions", "attempt_started_at"):
        assert after["tasks"][source_id][key] == before["tasks"][source_id][key]
    assert all(after["events"][key] == value for key, value in before["events"].items())
    assert all(path.read_bytes() == value for path, value in journal.items())
    assert after["tasks"][source_id]["status"] == "PENDING"
    with Session(runtime.engine) as session:
        receipt = session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).one()
        assert receipt.metadata_json["old_run"] == old.model_dump()
        assert receipt.metadata_json["max_new_native_generations"] == 7
        assert receipt.metadata_json["generation_evidence"]["native_generations"] == 1
        assert receipt.metadata_json["original_error_code"] == ERROR
        assert session.get(Content, cid).current_stage == "asset_generate"
    attach_fixture_host(http, http.get(f"/api/v1/contents/{cid}/run", headers=ADMIN).json())
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])["worker_requests"][0]
    assert worker["task_id"] == source_id and '"max_new_native_generations": 7' in worker["prompt"]
    assert "每个指定 segment" in worker["prompt"]
    claimed = scoped_dispatch(http, worker)
    assert claimed.status_code == 200, claimed.text
    with Session(runtime.engine) as session:
        runtime.task_service(session).confirm_claim(task_id=source_id, lease_token=claimed.json()["lease_token"], agent_id=worker["agent_id"])
        source = session.get(AgentTask, source_id)
        assert (source.attempt, source.max_attempts, source.status) == (2, 3, TaskStatus.LEASED)


@pytest.mark.parametrize("body", [None, {}, {"allow_local_image_adaptation": False}])
def test_missing_or_false_authority_has_no_side_effects(blocked, body):
    runtime, *_ = blocked
    before = snapshot(runtime)
    response = execute(blocked, body)
    assert response.status_code == 409, response.text
    assert snapshot(runtime) == before


@pytest.mark.parametrize("damage", ["old_true", "wrong_run", "wrong_content", "execution_active", "execution_exit", "host_live", "live_child", "pending", "leased", "submitted", "offer", "archived", "input", "claim", "confirmation", "failure", "recovery_input", "recovery_result", "recovery_receipt", "recovery_budget", "attempt_budget", "attempt_two", "image_budget", "configured_budget", "native_count", "journal", "script_qc", "plan_qc", "manual_signature", "raw_result"])
def test_changed_authority_rejects_unproven_or_inflight_state(blocked, damage):
    runtime, _, cid, source_id, recovery_id, _, _, directory, plan_id, plan_qc_id = blocked
    with Session(runtime.engine) as session:
        source, recovery, run = session.get(AgentTask, source_id), session.get(AgentTask, recovery_id), load_run(session)
        if damage == "old_true":
            run.allow_local_image_adaptation = True
        elif damage == "wrong_run":
            execution = launch._load(session, cid)
            execution.run_id = "run_" + "b" * 32
            launch._save(session, execution)
        elif damage == "wrong_content":
            run.content_id = "other-content"
        elif damage == "execution_active":
            execution = launch._load(session, cid)
            execution.state = "UNKNOWN"
            launch._save(session, execution)
        elif damage == "execution_exit":
            execution = launch._load(session, cid)
            execution.exit_code = 1
            launch._save(session, execution)
        elif damage == "host_live":
            run.host_observed_at = utc_now().isoformat()
        elif damage == "live_child":
            next(iter(run.intents.values()))["state"] = "OBSERVATION_LOST"
        elif damage in {"pending", "leased", "submitted"}:
            runtime.task_service(session).create_task(task_type=TaskType.SCENE_ANALYZE, content_id=cid, instructions="Other work", input_data={"extra": damage})
            task = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCENE_ANALYZE)).one()
            task.status = TaskStatus(damage.upper())
            session.add(task)
        elif damage == "offer":
            session.add(SchedulerOffer(request_id="live-offer", task_id=source_id, task_type=source.task_type.value, agent_id="fresh-agent", scheduler_id="mpt-codex", expires_at=utc_now() + timedelta(minutes=1)))
        elif damage == "archived":
            content = session.get(Content, cid)
            content.business_status = ContentStatus.ARCHIVED
            session.add(content)
        elif damage == "input":
            source.input_data = {**source.input_data, "tampered": True}
        elif damage in {"claim", "confirmation", "failure", "recovery_receipt", "manual_signature"}:
            action = {"claim": "mpt_content_run_claim", "confirmation": "claim_confirmed", "failure": "failed", "recovery_receipt": "mpt_agent_consumed", "manual_signature": "manual_script_approved"}[damage]
            entity = cid if damage == "manual_signature" else recovery_id if damage == "recovery_receipt" else source_id
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == entity, AuditEvent.action == action)).one()
            row.metadata_json = {**row.metadata_json, "agent_id": "wrong", "attempt": 99, "error_code": "wrong", "signature": "bad", "result_hash": "bad"}
            session.add(row)
        elif damage == "recovery_input":
            recovery.input_data = {**recovery.input_data, "tampered": True}
        elif damage == "recovery_result":
            recovery.result_data = {**recovery.result_data, "action": "retry_same_task"}
        elif damage == "recovery_budget":
            runtime.settings.recovery_max_rounds = 1
        elif damage == "attempt_budget":
            source.max_attempts = source.attempt
        elif damage == "attempt_two":
            source.attempt = 2
        elif damage == "configured_budget":
            runtime.settings.image_only_gen_budget = 7
        elif damage == "image_budget":
            plan = session.get(AgentTask, plan_id)
            plan.input_data = {**plan.input_data, "image_only_gen_budget": 7}
            session.add(plan)
        elif damage == "native_count":
            path = directory / "events.jsonl"
            raw = path.read_text(encoding="utf-8")
            extra = [dict(json.loads(line), seq=index + 3, item={**json.loads(line)["item"], "id": "second-call"}) for index, line in enumerate(raw.splitlines())]
            path.write_text(raw + "".join(json.dumps(event) + "\n" for event in extra), encoding="utf-8")
        elif damage == "journal":
            path = directory / "state.json"
            data = json.loads(path.read_text())
            data["generation"] += 1
            path.write_text(json.dumps(data))
        elif damage in {"script_qc", "plan_qc"}:
            task = session.get(AgentTask, plan_qc_id) if damage == "plan_qc" else session.exec(select(AgentTask).where(AgentTask.content_id == cid, AgentTask.task_type == TaskType.SCRIPT_QC)).one()
            task.result_data = {"passed": False}
            session.add(task)
        elif damage == "raw_result":
            (runtime.settings.agent_submission_dir / recovery_id / "1" / "result.json").write_text("{}")
        session.add_all([source, recovery])
        save_run(session, run, "fixture_damage")
        session.commit()
    before = snapshot(runtime)
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 409, response.text
    assert snapshot(runtime) == before


def test_concurrent_execute_consumes_one_receipt_and_launch(blocked):
    runtime, _, _, source_id, *_ = blocked
    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(lambda _: execute(blocked, {"allow_local_image_adaptation": True}), range(3)))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert len({response.json()["launch_id"] for response in responses}) == 1
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).all()) == 1
        assert session.get(AgentTask, source_id).attempt == 1


@pytest.mark.parametrize("point", ["resume", "commit"])
def test_atomic_resume_rolls_back_all_changes_on_fault(blocked, monkeypatch, point):
    from app.business import image_authorization_recovery as recovery

    runtime, _, cid, *_ = blocked
    before = snapshot(runtime)
    with monkeypatch.context() as patch:
        if point == "resume":
            original = recovery.apply_resume

            def fail(*args):
                original(*args)
                raise RuntimeError("injected resume failure")

            patch.setattr(recovery, "apply_resume", fail)
        else:
            patch.setattr(Session, "commit", lambda _: (_ for _ in ()).throw(RuntimeError("injected commit failure")))
        with pytest.raises(RuntimeError):
            with Session(runtime.engine) as session:
                launch.execute_content(runtime, session, cid, allow_local_image_adaptation=True)
    assert snapshot(runtime) == before


def test_launch_failure_retries_same_authorized_run_without_second_exception(blocked, monkeypatch):
    runtime, _, _, source_id, *_ = blocked
    with monkeypatch.context() as patch:
        patch.setattr(launch.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("pre-spawn failure")))
        first = execute(blocked, {"allow_local_image_adaptation": True})
    assert first.status_code == 200 and first.json()["error_code"] == "CONTENT_EXECUTION_START_FAILED"
    second = execute(blocked, {"allow_local_image_adaptation": True})
    assert second.status_code == 200 and second.json()["run_id"] == first.json()["run_id"]
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).all()) == 1
        assert session.get(AgentTask, source_id).attempt == 1


def test_plain_start_run_preserves_existing_behavior_without_resuming_task(blocked):
    runtime, _, cid, source_id, *_ = blocked
    with Session(runtime.engine) as session:
        old = load_run(session)
        new = start_run(session, cid)
        assert new["run_id"] != old.run_id
        assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
        assert session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).all() == []


@pytest.mark.parametrize("endpoint", ["run", "run/execute"])
def test_stopped_authorized_continuation_cannot_escape_receipt_or_budget(blocked, endpoint):
    runtime, http, cid, _, _, _, processes, *_ = blocked
    response = execute(blocked, {"allow_local_image_adaptation": True})
    http.post(f"/api/v1/contents/{cid}/execution/stop", headers=ADMIN)
    processes[0].code = 0
    processes[0].done.set()
    deadline = time.monotonic() + 5
    while launch._handles and time.monotonic() < deadline:
        time.sleep(0.01)
    before = snapshot(runtime)
    denied = http.post(f"/api/v1/contents/{cid}/{endpoint}", headers=ADMIN,
                       json={"allow_local_image_adaptation": True} if endpoint.endswith("execute") else None)
    assert response.status_code == 200 and denied.status_code == 409
    assert snapshot(runtime) == before
    assert len(processes) == 1


def test_crash_after_authority_commit_before_launch_intent_is_retryable_once(blocked, monkeypatch):
    runtime, _, cid, _, _, _, processes, *_ = blocked
    before = snapshot(runtime)
    with monkeypatch.context() as patch:
        patch.setattr(launch, "_save", lambda *args: (_ for _ in ()).throw(RuntimeError("intent storage unavailable")))
        with pytest.raises(RuntimeError):
            with Session(runtime.engine) as session:
                launch.execute_content(runtime, session, cid, allow_local_image_adaptation=True)
    after = snapshot(runtime)
    assert after["run"] != before["run"] and processes == []
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 200 and len(processes) == 1
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).all()) == 1


def test_uncertain_launch_intent_never_spawns_again(blocked, monkeypatch):
    runtime, _, cid, _, _, _, processes, *_ = blocked
    original = launch._save

    def uncertain(session, record):
        original(session, record)
        raise RuntimeError("crash after intent commit")

    with monkeypatch.context() as patch:
        patch.setattr(launch, "_save", uncertain)
        with pytest.raises(RuntimeError):
            with Session(runtime.engine) as session:
                launch.execute_content(runtime, session, cid, allow_local_image_adaptation=True)
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 200 and response.json()["state"] == "UNKNOWN"
    assert processes == []


def test_other_blocked_failure_preserves_ordinary_execute_behavior(blocked):
    runtime, _, _, source_id, *_ = blocked
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, source_id)
        task.error_code = "OTHER_ENVIRONMENT_FAILURE"
        session.add(task)
        session.commit()
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 200
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
        assert session.exec(select(AuditEvent).where(AuditEvent.action == RESUMED)).all() == []


def test_resumed_task_is_reserved_before_offer_and_cannot_be_claimed_unscoped(blocked):
    runtime, http, _, source_id, *_ = blocked
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 200
    image = client_for(http, "image")
    body = {"agent_id": "mpt-codex-image", "role_id": "image", "task_id": source_id,
            "task_type": TaskType.ASSET_GENERATE_IMAGE.value}
    unscoped = http.post("/api/v1/agent/dispatch?wait=0", headers=image._headers(), json=body)
    assert unscoped.status_code in {200, 409}
    if unscoped.status_code == 200:
        assert unscoped.json()["status"] != "CLAIMED"
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, source_id)
        assert source.status == TaskStatus.PENDING and source.attempt == 1


def test_previous_completed_image_for_same_plan_rejects_without_side_effects(blocked):
    runtime, _, cid, source_id, _, _, processes, *_ = blocked
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, source_id)
        previous = runtime.workflow_service(session).create_agent_task(
            cid, TaskType.ASSET_GENERATE_IMAGE,
            {**source.input_data, "requests": source.input_data["requests"][:1]}, allow_multiple=True,
        )
        previous.status = TaskStatus.COMPLETED
        previous.created_at = source.created_at - timedelta(minutes=1)
        previous.completed_at = source.created_at
        previous.result_data = {"images": []}
        session.add(previous)
        session.commit()
        assert previous.input_data["upstream_task_ids"] == source.input_data["upstream_task_ids"]
    before = snapshot(runtime)
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 409, response.text
    assert snapshot(runtime) == before
    assert processes == []


@pytest.mark.parametrize("key,value", [
    ("asset_image_enabled", "false"),
    ("image_only_gen_budget", "0"),
    ("image_only_gen_budget", "7"),
])
def test_persisted_image_configuration_opt_out_rejects_resume_without_side_effects(blocked, key, value):
    runtime, _, _, _, _, _, processes, *_ = blocked
    assert runtime.settings.asset_image_enabled is True
    assert runtime.settings.image_only_gen_budget == 8
    with Session(runtime.engine) as session:
        session.add(RuntimeSetting(key=key, value=value))
        session.commit()
        current = effective_settings(session, runtime.settings)
        assert getattr(current, key) == (False if key == "asset_image_enabled" else int(value))
        override_before = session.get(RuntimeSetting, key).model_dump(mode="json")
    before = snapshot(runtime)
    response = execute(blocked, {"allow_local_image_adaptation": True})
    assert response.status_code == 409, response.text
    assert snapshot(runtime) == before
    assert processes == []
    with Session(runtime.engine) as session:
        assert session.get(RuntimeSetting, key).model_dump(mode="json") == override_before
