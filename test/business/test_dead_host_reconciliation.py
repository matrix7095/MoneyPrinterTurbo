"""Explicit recovery never converts uncertain children into completed work."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from glance_core.models import AgentSubmission, AgentTask, AuditEvent, PublicationRecord, RuntimeSetting, SchedulerOffer, TaskType, utc_now
from glance_core.task_service import TaskConflictError, stable_hash
from sqlmodel import Session, select

from app.business import conductor_launch as launch
from app.business.content_run import ContentRun, OFFER_ACTION, RUN_KEY, load_run, save_run
from app.controllers.v1.content_execution import router
from test.business.test_content_http import content_app
from test.business.test_content_run_http import selected
from test.business.test_discovery_http import ADMIN, make_runtime


@pytest.fixture
def dead_host(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    app = content_app(runtime)
    app.include_router(router)
    with TestClient(app) as http:
        cid, _ = selected(http, runtime, "dead-host")
        now = utc_now()
        run = ContentRun(
            run_id="run_" + "a" * 32, content_id=cid, generation=19, state="running",
            created_at=(now - timedelta(minutes=30)).isoformat(),
            expires_at=(now + timedelta(minutes=90)).isoformat(),
            host_epoch=1, host_observed_at=(now - timedelta(minutes=5)).isoformat(),
            parent_thread_id="old-native-parent", process_identity={
                "pid": 11600, "started_at": (now - timedelta(minutes=29)).isoformat(),
                "instance_id": "old-instance", "binary_path": str(tmp_path / "native.exe"),
                "binary_sha256": "b" * 64, "version": "old-native-version",
            },
        )
        task = AgentTask(content_id=cid, task_type=TaskType.RECOVERY, prompt_name="recovery",
                         instructions="frozen", input_data={"scope": "recovery"},
                         input_hash=stable_hash({"scope": "recovery"}))
        task_id = task.id
        request_id = "recovery:1:frozen"
        worker = {"request_id": request_id, "task_id": task_id, "task_type": "recovery",
                  "run_id": run.run_id, "run_generation": 19, "host_epoch": 1,
                  "content_id": cid, "agent_id": "recovery-agent", "input_hash": task.input_hash,
                  "prompt": "frozen prompt"}
        offer_hash = stable_hash(worker)
        run.intents[request_id] = {"state": "SPAWN_INTENT", "host_epoch": 1,
                                   "offer_hash": offer_hash, "child": None, "claim_id": None}
        record = launch.LaunchRecord(launch_id="c" * 32, content_id=cid, run_id=run.run_id,
                                     generation=19, state="UNKNOWN", created_at=run.created_at,
                                     pid=30464, process_started_at=run.created_at, binary_sha256="d" * 64,
                                     error_code="CONTENT_EXECUTION_OBSERVATION_LOST")
        with Session(runtime.engine) as session:
            session.add(task)
            session.add(SchedulerOffer(request_id=request_id, task_id=task.id, task_type="recovery",
                                       agent_id=worker["agent_id"], expires_at=now + timedelta(minutes=2)))
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="scheduler_offer",
                                   entity_id=request_id, action=OFFER_ACTION,
                                   metadata_json={"worker": worker, "worker_hash": offer_hash}))
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=task.id, action="mpt_task_owned",
                                   metadata_json={"input_hash": task.input_hash}))
            save_run(session, run, "fixture-run")
            launch._save(session, record)
        journal = tmp_path / "private" / "content-runs" / run.run_id / "state.json"
        journal.parent.mkdir(parents=True)
        data = {key: getattr(run, key) for key in ("run_id", "content_id", "generation", "host_epoch", "parent_thread_id", "process_identity")}
        data.update(version=1, phase="RUNNING", intents={request_id: {
            "state": "SPAWN_SEND_INTENT", "request_id": request_id, "task_id": task_id,
            "offer_hash": offer_hash, "host_epoch": 1,
            "issued_prompt_sha256": launch.hashlib.sha256(worker["prompt"].encode()).hexdigest(),
        }})
        journal.write_text(json.dumps(data), encoding="utf-8")
        probes = []
        monkeypatch.setattr(launch, "process_absence_proof", lambda pid: probes.append(pid) or {"pid": pid, "winerror": 87}, raising=False)
        yield http, runtime, cid, record, task_id, request_id, journal, probes


def post(fixture, **changes):
    http, _, cid, record, *_ = fixture
    return http.post(f"/api/v1/contents/{cid}/execution/reconcile-host-loss", headers=ADMIN,
                     json={"launch_id": record.launch_id, "run_id": record.run_id,
                           "generation": record.generation, **changes})


def test_dead_unclaimed_recovery_is_audited_without_touching_task_or_journal(dead_host):
    _, runtime, cid, record, task_id, rid, journal, probes = dead_host
    before = journal.read_bytes()
    response = post(dead_host)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "FAILED"
    assert response.json()["error_code"] == "CONTENT_EXECUTION_HOST_EXIT_CONFIRMED"
    assert response.json()["exit_code"] is None
    assert probes == [30464, 11600]
    assert journal.read_bytes() == before
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert task.status == "PENDING" and task.attempt == 0 and task.assigned_agent_id is None
        run = load_run(session)
        assert run.state == "stopped" and run.stop_reason == "host_process_exit"
        assert run.intents[rid]["state"] == "ABANDONED_BEFORE_CLAIM"
        assert session.get(SchedulerOffer, rid).expires_at.replace(tzinfo=utc_now().tzinfo) <= utc_now()
        events = session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_content_execution_host_loss_reconciled")).all()
        assert len(events) == 1
        assert events[0].metadata_json["task_input_hash"] == task.input_hash
    # Replay uses the frozen audit; a new generation is never touched or re-probed.
    assert post(dead_host).json() == response.json()
    assert probes == [30464, 11600]
    with Session(runtime.engine) as session:
        row = session.get(RuntimeSetting, RUN_KEY)
        data = json.loads(row.value)
        data.update(run_id="run_" + "e" * 32, generation=20, state="awaiting_host", intents={})
        row.value = json.dumps(data)
        session.add(row)
        session.commit()
    assert post(dead_host).json() == response.json()
    with Session(runtime.engine) as session:
        assert load_run(session).generation == 20
        assert launch._load(session, cid).launch_id == record.launch_id


@pytest.mark.parametrize("field,value", [
    ("attempt", 1), ("assigned_agent_id", "unknown"), ("lease_token_hash", "token"),
    ("started_at", utc_now()), ("last_heartbeat_at", utc_now()),
    ("lease_expires_at", utc_now()), ("attempt_started_at", utc_now()),
    ("completed_at", utc_now()), ("result_data", {}), ("status", "LEASED"),
    ("task_type", TaskType.DOUYIN_PUBLISH), ("input_hash", "changed"),
])
def test_any_task_claim_or_changed_input_blocks(dead_host, field, value):
    _, runtime, _, _, task_id, *_ = dead_host
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        setattr(task, field, value)
        session.add(task)
        session.commit()
    assert post(dead_host).status_code == 409
    with Session(runtime.engine) as session:
        assert load_run(session).state == "running"


def test_auth_strict_binding_and_no_observation_state_forgery(dead_host):
    from app.business.requests import HostObservationRequest
    from pydantic import ValidationError

    http, _, cid, record, *_ = dead_host
    url = f"/api/v1/contents/{cid}/execution/reconcile-host-loss"
    assert http.post(url, json={}).status_code == 401
    assert post(dead_host, generation=True).status_code == 422
    assert post(dead_host, surprise="secret").status_code == 422
    assert post(dead_host, launch_id="f" * 32).status_code == 409
    with pytest.raises(ValidationError):
        HostObservationRequest(generation=19, host_epoch=1, event_id="x", outcome="ABANDONED_BEFORE_CLAIM")
    assert record.launch_id not in launch._handles


def add_terminal(fixture):
    _, runtime, cid, _, _, _, path, _ = fixture
    data = json.loads(path.read_text())
    with Session(runtime.engine) as session:
        run = load_run(session)
        started = utc_now() - timedelta(minutes=20)
        task = AgentTask(content_id=cid, task_type=TaskType.ASSET_QC, prompt_name="asset_qc",
                         instructions="prior", input_data={"prior": True}, input_hash=stable_hash({"prior": True}),
                         status="COMPLETED", result_data={"passed": False}, attempt=1,
                         assigned_agent_id="prior-agent", lease_token_hash="prior-token", started_at=started)
        tid = task.id
        rid = "asset_qc:1:prior"
        worker = {"request_id": rid, "task_id": tid, "task_type": "asset_qc", "run_id": run.run_id,
                  "run_generation": 19, "host_epoch": 1, "content_id": cid, "agent_id": "prior-agent",
                  "input_hash": task.input_hash, "prompt": "prior frozen prompt", "title": "prior-worker"}
        offer_hash = stable_hash(worker)
        child = {"child_id": "prior-child", "parent_thread_id": run.parent_thread_id,
                 "native_agent_type": "default", "model": "gpt-6.1-sol", "reasoning_effort": "high",
                 "ephemeral": True, "metadata_rpc_id": "rpc-prior",
                 "issued_prompt_sha256": launch.hashlib.sha256(worker["prompt"].encode()).hexdigest(),
                 "started": {"child_id": "prior-child", "parent_thread_id": run.parent_thread_id,
                             "parent_turn_id": "prior-turn", "item_id": "prior-item", "agent_path": "/root/prior-worker"}}
        binding = {"claim_id": "prior-claim", "run_id": run.run_id, "generation": 19, "host_epoch": 1,
                   "request_id": rid, "offer_hash": offer_hash, "task_id": tid, "agent_id": "prior-agent",
                   "lease_token_hash": "prior-token", "child_id": "prior-child",
                   "parent_thread_id": run.parent_thread_id, "started_at": started.isoformat()}
        result_hash = stable_hash(task.result_data)
        summary = {"claim_id": "prior-claim", "task_id": tid, "current_attempt": 1, "task_status": "COMPLETED",
                   "accepted": True, "accepted_result_hash": result_hash, "consumed": True, "consumed_result_hash": result_hash}
        run.intents[rid] = {"state": "TERMINAL", "host_epoch": 1, "offer_hash": offer_hash,
                            "child": child, "claim_id": "prior-claim"}
        run.offer_children[rid] = "prior-child"
        session.add(task)
        session.flush()
        session.add(SchedulerOffer(request_id=rid, task_id=tid, task_type="asset_qc", agent_id="prior-agent", expires_at=started))
        session.add(AgentSubmission(task_id=tid, agent_id="prior-agent", attempt=1, raw_result_path="fixture",
                                    result_hash=result_hash, validation_status="ACCEPTED"))
        for action, entity, entity_id, metadata in [
            (OFFER_ACTION, "scheduler_offer", rid, {"worker": worker, "worker_hash": offer_hash}),
            ("mpt_content_run_claim", "agent_task", tid, binding),
            ("mpt_agent_consumed", "agent_task", tid, {"result_hash": result_hash}),
        ]:
            session.add(AuditEvent(actor_type="system", actor_id="mpt-content-host", entity_type=entity,
                                   entity_id=entity_id, action=action, metadata_json=metadata))
        save_run(session, run, "fixture-prior")
        session.commit()
        data["intents"][rid] = {"request_id": rid, "task_id": tid, "offer_hash": offer_hash, "host_epoch": 1,
                                "issued_prompt_sha256": child["issued_prompt_sha256"], "child": child,
                                "last_observation": "TERMINAL", "business": summary}
        path.write_text(json.dumps(data), encoding="utf-8")
    return tid, rid


def test_completed_native_and_negative_business_qc_are_preserved(dead_host):
    tid, rid = add_terminal(dead_host)
    assert post(dead_host).status_code == 200
    with Session(dead_host[1].engine) as session:
        assert load_run(session).intents[rid]["state"] == "TERMINAL"
        assert session.get(AgentTask, tid).result_data == {"passed": False}


def test_cleaned_terminal_offer_uses_durable_receipts(dead_host):
    _, rid = add_terminal(dead_host)
    with Session(dead_host[1].engine) as session:
        session.delete(session.get(SchedulerOffer, rid))
        session.commit()
    assert post(dead_host).status_code == 200


@pytest.mark.parametrize("damage", ["result", "consumption", "claim", "journal_child", "not_terminal"])
def test_prior_child_or_business_uncertainty_blocks(dead_host, damage):
    tid, rid = add_terminal(dead_host)
    with Session(dead_host[1].engine) as session:
        if damage == "result":
            task = session.get(AgentTask, tid)
            task.result_data = {"passed": True}
            session.add(task)
        elif damage in {"consumption", "claim"}:
            action = "mpt_agent_consumed" if damage == "consumption" else "mpt_content_run_claim"
            event = session.exec(select(AuditEvent).where(AuditEvent.action == action, AuditEvent.entity_id == tid)).one()
            event.metadata_json = {**event.metadata_json, "result_hash": "changed", "child_id": "foreign-child"}
            session.add(event)
        elif damage == "not_terminal":
            run = load_run(session)
            run.intents[rid]["state"] = "CLAIMED"
            save_run(session, run, "fixture-changed")
        session.commit()
    if damage == "journal_child":
        path = dead_host[6]
        data = json.loads(path.read_text())
        data["intents"][rid]["child"]["child_id"] = "foreign"
        path.write_text(json.dumps(data))
    assert post(dead_host).status_code == 409


@pytest.mark.parametrize("reason", ["live", "access_denied", "query_failed"])
def test_either_process_without_absence_proof_blocks(dead_host, monkeypatch, reason):
    calls = []

    def probe(pid):
        calls.append(pid)
        if pid == 11600:
            raise TaskConflictError("CONTENT_EXECUTION_PROCESS_" + reason)
        return {"pid": pid, "winerror": 87}

    monkeypatch.setattr(launch, "process_absence_proof", probe)
    assert post(dead_host).status_code == 409
    assert calls == [30464, 11600]
    with Session(dead_host[1].engine) as session:
        assert load_run(session).state == "running"


@pytest.mark.parametrize("damage", ["heartbeat", "handle", "child", "claim", "journal", "submission", "claim_audit", "publication", "publish_offer", "record", "unknown_intent"])
def test_uncertain_authority_or_external_work_blocks(dead_host, damage):
    _, runtime, cid, record, tid, rid, journal, _ = dead_host
    with Session(runtime.engine) as session:
        run = load_run(session)
        if damage == "heartbeat":
            run.host_observed_at = utc_now().isoformat()
        elif damage == "child":
            run.offer_children[rid] = "unknown-child"
        elif damage == "claim":
            run.intents[rid]["claim_id"] = "unknown-claim"
        elif damage == "unknown_intent":
            run.intents[rid]["state"] = "OBSERVATION_LOST"
        elif damage == "record":
            row = session.get(RuntimeSetting, launch.PREFIX + record.run_id)
            row.value = row.value.replace('"pid":30464', '"pid":999')
            session.add(row)
        elif damage == "submission":
            session.add(AgentSubmission(task_id=tid, agent_id="foreign", attempt=0,
                                        raw_result_path="fixture", result_hash="x", validation_status="REJECTED"))
        elif damage == "claim_audit":
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=tid, action="claim_confirmed"))
        elif damage == "publication":
            session.add(PublicationRecord(content_id=cid, mode="AGENT", platform="douyin", status="UNKNOWN"))
        elif damage == "publish_offer":
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="scheduler_offer",
                                   entity_id="publish-offer", action=OFFER_ACTION,
                                   metadata_json={"worker": {"run_id": run.run_id, "task_type": "douyin_publish"}}))
        save_run(session, run, "fixture-damage")
        session.commit()
    if damage == "journal":
        data = json.loads(journal.read_text())
        data["generation"] = 18
        journal.write_text(json.dumps(data))
    if damage == "handle":
        launch._handles[record.launch_id] = object()
    try:
        assert post(dead_host).status_code == 409
    finally:
        launch._handles.pop(record.launch_id, None)
    with Session(runtime.engine) as session:
        assert load_run(session).state == "running"


def test_failure_after_writes_rolls_back_run_offer_and_launch(dead_host, monkeypatch):
    original_save = launch._save

    def interrupted(session, record, **options):
        original_save(session, record, **options)
        session.flush()
        raise RuntimeError("fixture interrupted before commit")

    monkeypatch.setattr(launch, "_save", interrupted)
    with pytest.raises(RuntimeError, match="before commit"):
        post(dead_host)
    with Session(dead_host[1].engine) as session:
        assert load_run(session).state == "running"
        assert launch._load(session, dead_host[2]).state == "UNKNOWN"
        assert session.get(SchedulerOffer, dead_host[5]).expires_at.replace(tzinfo=utc_now().tzinfo) > utc_now()
        assert not session.exec(select(AuditEvent).where(AuditEvent.action == launch.HOST_LOSS_RECONCILED)).all()


@pytest.mark.skipif(launch.os.name != "nt", reason="Windows kernel proof")
def test_windows_probe_requires_nonexistent_pid_and_does_not_kill_live_process():
    assert launch.process_absence_proof(0xFFFFFFFE) == {"pid": 0xFFFFFFFE, "winerror": 87}
    with pytest.raises(TaskConflictError, match="PROCESS_STILL_EXISTS"):
        launch.process_absence_proof(launch.os.getpid())
    assert launch.os.getpid() > 0


@pytest.mark.parametrize("pid", [True, 0, -1, 0x100000000])
def test_invalid_pid_never_reaches_windows_api(pid):
    with pytest.raises(TaskConflictError, match="PROOF_UNAVAILABLE"):
        launch.process_absence_proof(pid)


@pytest.mark.skipif(launch.os.name != "nt", reason="Windows kernel proof")
@pytest.mark.parametrize("error", [0, 5, 6])
def test_windows_api_query_failure_is_never_absence(monkeypatch, error):
    import ctypes

    class Function:
        def __call__(self, *args):
            return None

    class Kernel:
        OpenProcess = Function()
        CloseHandle = Function()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: Kernel())
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error)
    with pytest.raises(TaskConflictError, match="PROOF_UNAVAILABLE"):
        launch.process_absence_proof(11600)


def test_concurrent_explicit_reconciliation_is_one_audit(dead_host):
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: post(dead_host), range(4)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert dead_host[7] == [30464, 11600]
    with Session(dead_host[1].engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == launch.HOST_LOSS_RECONCILED)).all()) == 1


def test_normal_execute_creates_new_generation_and_old_replay_cannot_change_it(dead_host, monkeypatch, tmp_path):
    http, runtime, cid, old_record, tid, *_ = dead_host
    assert post(dead_host).status_code == 200
    binary = tmp_path / "fixture-python.exe"
    binary.write_bytes(b"test binary")
    monkeypatch.setattr(launch, "launch_command", lambda *args: [str(binary)])

    def unavailable(*args, **options):
        raise OSError("fixture start unavailable")

    monkeypatch.setattr(launch.subprocess, "Popen", unavailable)
    response = http.post(f"/api/v1/contents/{cid}/run/execute", headers=ADMIN, json={})
    assert response.status_code == 200, response.text
    new = response.json()
    assert new["generation"] == 20 and new["launch_id"] != old_record.launch_id
    assert new["run_id"] != old_record.run_id
    assert post(dead_host).json()["launch_id"] == old_record.launch_id
    with Session(runtime.engine) as session:
        assert launch._load(session, cid).launch_id == new["launch_id"]
        assert load_run(session).generation == 20
        task = session.get(AgentTask, tid)
        assert task.status == "PENDING" and task.attempt == 0 and task.max_attempts == 3


def test_private_journal_failure_is_normalized_without_echo(dead_host, monkeypatch):
    from pathlib import Path

    read = Path.read_bytes

    def denied(path):
        if path == dead_host[6]:
            raise PermissionError("test-admin PRIVATE SECRET PATH")
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    response = post(dead_host)
    assert response.status_code == 409
    assert "SECRET" not in response.text and "test-admin" not in response.text
    with Session(dead_host[1].engine) as session:
        assert load_run(session).state == "running"
