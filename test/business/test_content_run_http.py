"""Real HTTP/SQLite scope permits, without a conductor or business Worker."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from glance_agent import cli
from glance_agent.scheduler_handoff import consume_offer, materialize_offer, offer_path
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Content,
    RightsStatus,
    RuntimeSetting,
    SchedulerOffer,
    TaskStatus,
    TaskType,
    utc_now,
)
from glance_core.scheduler_profiles import SCHEDULER_CONFIG_KEY
from glance_core.runtime_config import load_overrides, save_overrides
from sqlmodel import Session, select

from app.business.content_run import RUN_KEY, OFFER_ACTION, load_run
from app.business.quarantine import IsolatedTaskService, CONTENT_QUARANTINE
from app.business.runtime import OWNED, AGENT_CONSUMED
from test.business.test_content_http import RESEARCH, client_for
from test.business.test_deployment_http import app_for, deploy, enable, scheduler, stage
from test.business.test_discovery_http import ADMIN, make_runtime


def selected(http, runtime, name):
    with Session(runtime.engine) as session:
        candidate, _ = runtime.workflow_service(session).ingest_candidate(
            source_type="agent",
            source_id=name,
            source_url=f"https://example.org/{name}",
            original_title=name,
            rights_status=RightsStatus.EXPLICIT_LICENSE,
        )
        candidate_id = candidate.id
    response = http.post(
        f"/api/v1/candidates/{candidate_id}/select?production_mode=image_only",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    content_id = response.json()["id"]
    with Session(runtime.engine) as session:
        task = session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.CONTENT_RESEARCH,
            )
        ).one()
        return content_id, task.id


def attach_fixture_host(http, run):
    import os
    import sys
    from pathlib import Path

    response = http.post(
        f"/api/v1/content-runs/{run['run_id']}/host/attach",
        headers=ADMIN,
        json={
            "generation": run["generation"],
            "expected_host_epoch": run.get("host_epoch", 0),
            "parent_thread_id": "fixture-parent",
            "process_identity": {
                "pid": os.getpid(),
                "started_at": "2026-10-01T00:00:00+00:00",
                "instance_id": "fixture-instance",
                "binary_path": sys.executable,
                "binary_sha256": hashlib.sha256(
                    Path(sys.executable).read_bytes()
                ).hexdigest(),
                "version": "test-host",
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def fixture_child(worker):
    child_id = "fixture-child-" + worker["request_id"].rsplit(":", 1)[-1]
    return {
        "child_id": child_id,
        "parent_thread_id": "fixture-parent",
        "native_agent_type": worker["native_agent_type"],
        "model": worker["model"],
        "reasoning_effort": worker["reasoning_effort"],
        "ephemeral": True,
        "started": {
            "parent_thread_id": "fixture-parent",
            "parent_turn_id": "fixture-turn",
            "item_id": "fixture-item",
            "agent_path": "/root/" + worker["title"],
            "child_id": child_id,
        },
        "metadata_rpc_id": 1,
        "issued_prompt_sha256": hashlib.sha256(worker["prompt"].encode()).hexdigest(),
    }


def verify_fixture_worker(http, worker):
    path = f"/api/v1/content-runs/{worker['run_id']}/intents/{worker['request_id']}"
    http.put(
        path,
        headers=ADMIN,
        json={
            "generation": worker["run_generation"],
            "host_epoch": worker["host_epoch"],
            "event_id": "spawn-" + worker["request_id"],
            "offer_hash": worker["offer_hash"],
        },
    )
    return http.post(
        path + "/observation",
        headers=ADMIN,
        json={
            "generation": worker["run_generation"],
            "host_epoch": worker["host_epoch"],
            "event_id": "verified-" + worker["request_id"],
            "outcome": "VERIFIED",
            "child": fixture_child(worker),
        },
    )


def terminal_fixture_worker(http, worker):
    response = http.post(
        f"/api/v1/content-runs/{worker['run_id']}/intents/{worker['request_id']}/observation",
        headers=ADMIN,
        json={
            "generation": worker["run_generation"],
            "host_epoch": worker["host_epoch"],
            "event_id": "terminal-" + worker["request_id"],
            "outcome": "TERMINAL",
            "child": fixture_child(worker),
        },
    )
    assert response.status_code == 200, response.text


def start(http, content_id, *, attach=True):
    response = http.post(f"/api/v1/contents/{content_id}/run", headers=ADMIN)
    assert response.status_code == 200, response.text
    run = response.json()
    if attach and run["state"] in {"awaiting_host", "running"}:
        attach_fixture_host(http, run)
        run = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    return run


def scoped_dispatch(http, worker, **overrides):
    verify_fixture_worker(http, worker)
    agent = client_for(http, worker["role_id"])
    body = {
        "agent_id": worker["agent_id"],
        "role_id": worker["role_id"],
        "task_id": worker["task_id"],
        "task_type": worker["task_type"],
        "run_id": worker["run_id"],
        "run_generation": worker["run_generation"],
        "request_id": worker["request_id"],
        "runtime": {
            "model_name": worker["model"],
            "reasoning_effort": worker["reasoning_effort"],
        },
        **overrides,
    }
    return http.post(
        "/api/v1/agent/dispatch?wait=0", headers=agent._headers(), json=body
    )


@pytest.fixture
def system(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http, enabled=False)
        deploy(http, stage(runtime, "research"))
        yield runtime, http


def test_admin_unique_idempotent_run_and_unchanged_private_config(system):
    runtime, http = system
    content_id, _ = selected(http, runtime, "idempotent")
    other, _ = selected(http, runtime, "other")
    before = http.get("/api/v1/agent/scheduler/config", headers=ADMIN).json()
    with Session(runtime.engine) as session:
        raw = session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY).value
    assert http.post(f"/api/v1/contents/{content_id}/run").status_code == 401
    assert (
        http.post(
            f"/api/v1/contents/{content_id}/run",
            headers=client_for(http, "research")._headers(),
        ).status_code
        == 401
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: start(http, content_id, attach=False), range(4))
        )
    assert len({item["run_id"] for item in results}) == 1
    assert all(
        item["state"] == "awaiting_host" and item["execution_mode"] == "contract_only"
        for item in results
    )
    assert all(
        item["parent_thread_id"] is None and item["process_identity"] is None
        for item in results
    )
    assert http.post(f"/api/v1/contents/{other}/run", headers=ADMIN).status_code == 409
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["run_id"]
        == results[0]["run_id"]
    )
    assert http.get("/api/v1/agent/scheduler/config", headers=ADMIN).json() == before
    with Session(runtime.engine) as session:
        assert session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY).value == raw
        assert (
            len(
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action == "mpt_content_run_started"
                    )
                ).all()
            )
            == 1
        )
        assert (
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == "handsfree_candidate_selected"
                )
            ).all()
            == []
        )
        assert load_overrides(session) == {}
        private_before = session.get(RuntimeSetting, RUN_KEY).value
        assert save_overrides(session, {RUN_KEY: "untrusted-user-config"}) == 0
        assert session.get(RuntimeSetting, RUN_KEY).value == private_before
    assert (
        http.get(
            "/api/v1/agent/scheduler/plan",
            headers=client_for(http, "research")._headers(),
            params={
                "scheduler_id": "mpt-codex",
                "contract_version": 2,
                "run_id": results[0]["run_id"],
            },
        ).status_code
        == 401
    )


def test_new_run_has_two_hour_deadline_and_enforces_exact_expiry(system, monkeypatch):
    from app.business import content_run
    from glance_core.task_service import TaskConflictError

    runtime, http = system
    content_id, _ = selected(http, runtime, "two-hour-boundary")
    run = start(http, content_id, attach=False)
    created, expires = (datetime.fromisoformat(run[key]) for key in ("created_at", "expires_at"))
    assert expires - created == timedelta(minutes=120)
    with Session(runtime.engine) as session:
        original = session.get(RuntimeSetting, RUN_KEY).value
    monkeypatch.setattr(content_run, "utc_now", lambda: expires - timedelta(microseconds=1))
    with Session(runtime.engine) as session:
        assert content_run.require_run(session, run["run_id"]).expires_at == run["expires_at"]
        assert session.get(RuntimeSetting, RUN_KEY).value == original
    monkeypatch.setattr(content_run, "utc_now", lambda: expires)
    with Session(runtime.engine) as session:
        with pytest.raises(TaskConflictError, match="CONTENT_RUN_NOT_ACTIVE"):
            content_run.require_run(session, run["run_id"])
        assert session.get(RuntimeSetting, RUN_KEY).value == original
    expired = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    assert expired["state"] == "expired" and expired["stop_reason"] == "expired"
    assert expired["run_id"] == run["run_id"] and expired["expires_at"] == run["expires_at"]


@pytest.mark.parametrize("lifetime,valid", [
    (timedelta(minutes=45), True), (timedelta(minutes=120), True),
    (timedelta(minutes=120, microseconds=1), False), (timedelta(0), False), (timedelta(seconds=-1), False),
], ids=["legacy-45-minutes", "bounded-120-minutes", "over-120-minutes", "zero", "negative"])
def test_persisted_run_lifetime_is_bounded_without_rewriting_legacy(system, lifetime, valid):
    from glance_core.task_service import TaskConflictError

    runtime, http = system
    content_id, _ = selected(http, runtime, "persisted-lifetime")
    run = start(http, content_id, attach=False)
    with Session(runtime.engine) as session:
        row = session.get(RuntimeSetting, RUN_KEY)
        data = json.loads(row.value)
        data["expires_at"] = (datetime.fromisoformat(run["created_at"]) + lifetime).isoformat()
        original = row.value = json.dumps(data)
        session.add(row)
        session.commit()
        if valid:
            loaded = load_run(session)
            assert loaded.run_id == run["run_id"] and loaded.expires_at == data["expires_at"]
        else:
            with pytest.raises(TaskConflictError, match="CONTENT_RUN_RECORD_INVALID"):
                load_run(session)
        assert session.get(RuntimeSetting, RUN_KEY).value == original


def test_repeat_execute_preserves_persisted_45_minute_run_deadline(system, monkeypatch):
    import sys

    from app.business import conductor_launch as launch, content_run
    from app.controllers.v1.content_execution import router
    from test.business.test_content_execution_http import Process

    runtime, http = system
    http.app.include_router(router)
    content_id, task_id = selected(http, runtime, "legacy-execute-deadline")
    run = start(http, content_id, attach=False)
    with Session(runtime.engine) as session:
        row = session.get(RuntimeSetting, RUN_KEY)
        data = json.loads(row.value)
        created = datetime.fromisoformat(data["created_at"])
        data["expires_at"] = (created + timedelta(minutes=45)).isoformat()
        original = row.value = json.dumps(data)
        session.add(row)
        session.commit()
        before_task = session.get(AgentTask, task_id).model_dump(mode="json")
    monkeypatch.setattr(content_run, "utc_now", lambda: created + timedelta(minutes=10))
    process, launches = Process(), []
    monkeypatch.setattr(launch, "launch_command", lambda *args: [sys.executable])
    monkeypatch.setattr(launch, "process_creation_time", lambda _: created.isoformat())

    def popen(*args, **kwargs):
        launches.append(process)
        return process

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    endpoint = f"/api/v1/contents/{content_id}/run/execute"
    try:
        first = http.post(endpoint, headers=ADMIN)
        assert first.status_code == 200, first.text
        monkeypatch.setattr(content_run, "utc_now", lambda: created + timedelta(minutes=20))
        repeated = http.post(endpoint, headers=ADMIN)
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["launch_id"] == first.json()["launch_id"]
        assert repeated.json()["run_id"] == run["run_id"] and launches == [process]
        permit = start(http, content_id, attach=False)
        assert permit["created_at"] == run["created_at"] and permit["expires_at"] == data["expires_at"]
        assert permit["generation"] == run["generation"] and permit["ready_rounds"] == 0
        with Session(runtime.engine) as session:
            assert load_run(session).expires_at == data["expires_at"]
            assert session.get(RuntimeSetting, RUN_KEY).value == original
            assert session.get(AgentTask, task_id).model_dump(mode="json") == before_task
    finally:
        process.done.set()
        deadline = time.monotonic() + 5
        while any(handle is process for handle in launch._handles.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(handle is process for handle in launch._handles.values())


def test_disabled_global_scope_selects_only_target_and_never_global_maintenance(
    system, monkeypatch
):
    runtime, http = system
    target, task_id = selected(http, runtime, "target")
    other, other_id = selected(http, runtime, "high-priority-other")
    with Session(runtime.engine) as session:
        other_task = session.get(AgentTask, other_id)
        other_task.priority = 1000
        session.add(other_task)
        session.commit()
        session.refresh(other_task)
        before = session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY).value
        task_before = other_task.model_dump(mode="json")
        content_before = session.get(Content, other).model_dump(mode="json")
    for method in ("maintain_dispatch", "recover_discovery"):
        monkeypatch.setattr(
            runtime, method, lambda: pytest.fail("global maintenance called")
        )
    unknown = http.get(
        "/api/v1/agent/scheduler/plan",
        headers=scheduler(http)._headers(),
        params={
            "scheduler_id": "mpt-codex",
            "contract_version": 2,
            "run_id": "run_" + "0" * 32,
        },
    )
    assert unknown.status_code == 409
    with Session(runtime.engine) as session:
        assert session.exec(select(SchedulerOffer)).all() == []
    run = start(http, target)
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    assert plan["status"] == "READY"
    assert [item["task_id"] for item in plan["worker_requests"]] == [task_id]
    worker = plan["worker_requests"][0]
    assert worker["run_id"] == run["run_id"] and worker["run_generation"] == 1
    assert worker["content_id"] == target
    assert scoped_dispatch(http, worker).status_code == 200
    with Session(runtime.engine) as session:
        assert session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY).value == before
        assert session.get(AgentTask, other_id).model_dump(mode="json") == task_before
        assert session.get(Content, other).model_dump(mode="json") == content_before
        assert (
            session.exec(select(AuditEvent).where(AuditEvent.action == OFFER_ACTION))
            .one()
            .metadata_json["worker"]["run_id"]
            == run["run_id"]
        )


@pytest.mark.parametrize("occupied", ["lease", "offer"])
def test_other_content_occupies_global_capacity(system, occupied):
    runtime, http = system
    target, _ = selected(http, runtime, "capacity-target")
    for index in range(2):
        _, task_id = selected(http, runtime, f"occupied-{index}")
        with Session(runtime.engine) as session:
            if occupied == "lease":
                task = session.get(AgentTask, task_id)
                task.status = TaskStatus.LEASED
                task.assigned_agent_id = "outside-worker"
                task.lease_expires_at = utc_now() + timedelta(minutes=5)
                session.add(task)
            else:
                session.add(
                    SchedulerOffer(
                        request_id=f"outside-{index}",
                        task_id=task_id,
                        task_type=TaskType.CONTENT_RESEARCH.value,
                        scheduler_id="mpt-codex",
                        agent_id=f"outside-{index}",
                        expires_at=utc_now() + timedelta(minutes=5),
                    )
                )
            session.commit()
    run = start(http, target)
    assert (
        scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
            "worker_requests"
        ]
        == []
    )


@pytest.mark.parametrize(
    "invalid",
    ["stop", "expiry", "generation", "offer-expiry", "model", "input", "no-scope"],
)
def test_invalid_scope_cannot_claim_or_charge_attempt(system, invalid):
    runtime, http = system
    target, task_id = selected(http, runtime, "invalid-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    assert verify_fixture_worker(http, worker).status_code == 200
    overrides = {}
    if invalid == "stop":
        stopped = http.post(f"/api/v1/contents/{target}/run/stop", headers=ADMIN).json()
        assert stopped["state"] == "draining"
        terminal_fixture_worker(http, worker)
        new = start(http, target)
        assert new["generation"] == 2 and new["run_id"] != run["run_id"]
        replacement = scheduler(http).scheduler_plan(
            "mpt-codex", 2, run_id=new["run_id"]
        )
        assert (
            replacement["status"] == "READY"
            and replacement["worker_requests"][0]["run_generation"] == 2
        )
    elif invalid in {"expiry", "offer-expiry", "input"}:
        with Session(runtime.engine) as session:
            if invalid == "expiry":
                row = session.get(RuntimeSetting, RUN_KEY)
                value = json.loads(row.value)
                value["created_at"] = (utc_now() - timedelta(minutes=46)).isoformat()
                value["expires_at"] = (utc_now() - timedelta(minutes=1)).isoformat()
                row.value = json.dumps(value)
            elif invalid == "offer-expiry":
                row = session.get(SchedulerOffer, worker["request_id"])
                row.expires_at = utc_now() - timedelta(seconds=1)
            else:
                row = session.get(AgentTask, task_id)
                row.input_data = {"tampered": True}
            session.add(row)
            session.commit()
    elif invalid == "generation":
        overrides["run_generation"] = 2
    elif invalid == "model":
        overrides["runtime"] = {"model_name": "gpt-6-luna", "reasoning_effort": "max"}
    elif invalid == "no-scope":
        overrides = dict.fromkeys(["run_id", "run_generation", "request_id"])
    with Session(runtime.engine) as session:
        audits_before = [
            row.model_dump(mode="json")
            for row in session.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
        ]
        offers_before = [
            row.model_dump(mode="json")
            for row in session.exec(
                select(SchedulerOffer).order_by(SchedulerOffer.request_id)
            ).all()
        ]
    response = scoped_dispatch(http, worker, **overrides)
    assert response.status_code == 409, response.text
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert task.status == TaskStatus.PENDING and task.attempt == 0
        assert not task.assigned_agent_id
        assert [
            row.model_dump(mode="json")
            for row in session.exec(select(AuditEvent).order_by(AuditEvent.id)).all()
        ] == audits_before
        assert [
            row.model_dump(mode="json")
            for row in session.exec(
                select(SchedulerOffer).order_by(SchedulerOffer.request_id)
            ).all()
        ] == offers_before


def test_scoped_offer_is_frozen_one_use_and_old_worker_can_drain(system):
    runtime, http = system
    target, task_id = selected(http, runtime, "drain-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    from pathlib import Path

    launcher = Path(worker["launcher"])
    materialize_offer(worker, "mpt-codex", launcher, time.time() + 540)
    envelope = offer_path(launcher, worker["request_id"]).with_suffix(".json")
    digest = hashlib.sha256(envelope.read_bytes()).hexdigest()
    agent = client_for(http, "research")
    agent.settings.agent_id = "mpt-codex-research"
    frozen = consume_offer(envelope, digest, agent.settings, cli._handoff_roles())
    assert frozen["run_id"] == run["run_id"] and frozen["run_generation"] == 1
    with pytest.raises(FileExistsError):
        consume_offer(envelope, digest, agent.settings, cli._handoff_roles())
    claim = scoped_dispatch(http, worker).json()
    assert claim["should_run"]
    stopped = http.post(f"/api/v1/contents/{target}/run/stop", headers=ADMIN).json()
    assert stopped["state"] == "draining" and stopped["active_task_ids"] == [task_id]
    agent.settings.agent_id = worker["agent_id"]
    agent.confirm_claim(task_id, claim["lease_token"], worker["agent_id"])
    assert agent.heartbeat(task_id, claim["lease_token"])["status"] == "LEASED"
    assert (
        agent.complete(task_id, claim["lease_token"], worker["agent_id"], RESEARCH)[
            "advanced"
        ]
        == "CONSUMED"
    )
    terminal_fixture_worker(http, worker)
    assert (
        http.get(f"/api/v1/contents/{target}/run", headers=ADMIN).json()["state"]
        == "stopped"
    )
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, task_id).attempt == 1
        assert (
            session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == "handsfree_candidate_selected"
                )
            ).all()
            == []
        )


def test_atomic_claim_stop_ordering(system, monkeypatch):
    runtime, http = system
    target, task_id = selected(http, runtime, "race-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    acquired, release = Event(), Event()
    original = IsolatedTaskService._authorize_claim

    def pause_inside_claim(self, **kwargs):
        original(self, **kwargs)
        acquired.set()
        assert release.wait(10)

    monkeypatch.setattr(IsolatedTaskService, "_authorize_claim", pause_inside_claim)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claim_job = pool.submit(scoped_dispatch, http, worker)
        assert acquired.wait(10)
        stop_job = pool.submit(
            http.post, f"/api/v1/contents/{target}/run/stop", headers=ADMIN
        )
        try:
            assert not stop_job.done()
        finally:
            release.set()
        claim = claim_job.result(timeout=15)
        stopped = stop_job.result(timeout=15)
    assert claim.status_code == 200 and claim.json()["should_run"]
    assert stopped.status_code == 200 and stopped.json()["state"] == "draining"
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert task.status == TaskStatus.LEASED and task.attempt == 0


def test_quarantine_blocks_admin_run_without_adopting_content(system):
    runtime, http = system
    target, _ = selected(http, runtime, "quarantined")
    with Session(runtime.engine) as session:
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="fixture",
                entity_type="content",
                entity_id=target,
                action=CONTENT_QUARANTINE,
            )
        )
        session.commit()
    assert http.post(f"/api/v1/contents/{target}/run", headers=ADMIN).status_code == 409
    with Session(runtime.engine) as session:
        assert load_run(session) is None


def test_scoped_recovery_consumes_only_target_accepted_result(system, monkeypatch):
    runtime, http = system
    target, task_id = selected(http, runtime, "completed-target")
    other, other_id = selected(http, runtime, "completed-other")
    agent = client_for(http, "research")
    original = runtime.consume_agent

    def unavailable(*args, **kwargs):
        raise RuntimeError("fixture delays committed result consumption")

    monkeypatch.setattr(runtime, "consume_agent", unavailable)
    for key in (task_id, other_id):
        claimed = agent.dispatch(wait=0, task_id=key)
        assert claimed["should_run"]
        agent.confirm_claim(key, claimed["lease_token"], agent.settings.agent_id)
        assert (
            agent.complete(
                key, claimed["lease_token"], agent.settings.agent_id, RESEARCH
            )["advanced"]
            == "PENDING"
        )
    monkeypatch.setattr(runtime, "consume_agent", original)
    with Session(runtime.engine) as session:
        other_before = session.get(Content, other).model_dump(mode="json")
    run = start(http, target)
    for method in ("maintain_dispatch", "recover_discovery"):
        monkeypatch.setattr(
            runtime, method, lambda: pytest.fail("global recovery called")
        )
    scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    with Session(runtime.engine) as session:
        consumed = session.exec(
            select(AuditEvent.entity_id).where(AuditEvent.action == AGENT_CONSUMED)
        ).all()
        assert task_id in consumed and other_id not in consumed
        assert session.get(Content, other).model_dump(mode="json") == other_before
        assert session.get(Content, target).current_stage == "short_script"


def test_scoped_reaping_never_changes_expired_other_content(system):
    runtime, http = system
    target, task_id = selected(http, runtime, "reap-target")
    _, other_id = selected(http, runtime, "reap-other")
    with Session(runtime.engine) as session:
        for key in (task_id, other_id):
            task = session.get(AgentTask, key)
            task.status = TaskStatus.LEASED
            task.assigned_agent_id = "expired-fixture"
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(task)
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="fixture",
                    entity_type="agent_task",
                    entity_id=key,
                    action=OWNED,
                    metadata_json={"input_hash": task.input_hash},
                )
            )
        session.commit()
        other_before = session.get(AgentTask, other_id).model_dump(mode="json")
    run = start(http, target)
    scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, other_id).model_dump(mode="json") == other_before
        assert session.get(AgentTask, task_id).status == TaskStatus.PENDING


def test_scoped_plan_shares_audited_host_capacity(system):
    runtime, http = system
    target, _ = selected(http, runtime, "host-target")
    _, other_id = selected(http, runtime, "host-other")
    with Session(runtime.engine) as session:
        proof = session.exec(
            select(AuditEvent).where(AuditEvent.action == "mpt_deployment_verified")
        ).one()
        proof.metadata_json = {**proof.metadata_json, "host_capacity": 1}
        session.add(proof)
        task = session.get(AgentTask, other_id)
        task.status = TaskStatus.LEASED
        task.assigned_agent_id = "mpt-codex-research-123456789abc"
        task.lease_expires_at = utc_now() + timedelta(minutes=5)
        session.add(task)
        session.commit()
    run = start(http, target)
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    assert plan["worker_requests"] == [] and plan["status"] == "BLOCKED"
    assert plan["blocked"][0]["reason"] == "HOST_AT_CAPACITY"


@pytest.mark.parametrize(
    "field", ["agent_id", "task_id", "request_id", "run_id", "run_generation"]
)
def test_role_token_cannot_invent_a_scoped_offer(system, field, monkeypatch):
    runtime, http = system
    target, _ = selected(http, runtime, "forgery-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    value = {
        "agent_id": "mpt-codex-research-111111111111",
        "task_id": "agt_" + "1" * 32,
        "request_id": "fake-request",
        "run_id": "run_" + "1" * 32,
        "run_generation": 2,
    }[field]
    monkeypatch.setattr(
        runtime,
        "maintain_content_dispatch",
        lambda *_: pytest.fail("invalid offer reached maintenance"),
    )
    assert scoped_dispatch(http, worker, **{field: value}).status_code == 409


def test_old_fresh_identity_without_scope_never_enters_global_maintenance(
    system, monkeypatch
):
    runtime, http = system
    target, _ = selected(http, runtime, "unscoped-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    http.post(f"/api/v1/contents/{target}/run/stop", headers=ADMIN)
    monkeypatch.setattr(
        runtime,
        "maintain_dispatch",
        lambda: pytest.fail("old scoped worker reached global maintenance"),
    )
    assert (
        scoped_dispatch(
            http, worker, **dict.fromkeys(["run_id", "run_generation", "request_id"])
        ).status_code
        == 409
    )
    agent = client_for(http, "research")
    response = http.post(
        "/api/v1/agent/tasks/claim?wait=0",
        headers=agent._headers(),
        json={"agent_id": worker["agent_id"], "role_id": "research"},
    )
    assert response.status_code == 409


def test_ordinary_claim_cannot_steal_live_scoped_offer(system):
    runtime, http = system
    target, task_id = selected(http, runtime, "reserved-target")
    run = start(http, target)
    scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    agent = client_for(http, "research")
    assert agent.claim()["status"] == "NO_TASK"
    response = http.post(
        "/api/v1/agent/dispatch?wait=0",
        headers=agent._headers(),
        json={
            "agent_id": agent.settings.agent_id,
            "role_id": "research",
            "task_id": task_id,
        },
    )
    assert response.status_code == 409
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, task_id).status == TaskStatus.PENDING
    from glance_core.task_service import TaskConflictError

    with Session(runtime.engine, expire_on_commit=False) as session:
        task = session.get(AgentTask, task_id)
        with pytest.raises(TaskConflictError, match="CONTENT_RUN_OFFER_REQUIRED"):
            runtime.task_service(session).claim(
                task_id=task.id,
                agent_id="direct-old-role",
                role_id="research",
                capabilities={TaskType.CONTENT_RESEARCH},
            )


def test_real_cli_v2_run_option_materializes_and_dispatches_frozen_scope(
    system, monkeypatch
):
    from pathlib import Path
    from glance_agent.client import AgentClient
    from typer.testing import CliRunner

    runtime, http = system
    target, task_id = selected(http, runtime, "cli-target")
    run = start(http, target)
    scheduler_config = scheduler(http).settings
    scheduler_config.workspace_dir = (
        runtime.workspace_root / "storage/agent-launcher-staging/fixture/workspaces"
    )

    def client(config):
        instance = AgentClient(config)
        instance.client.close()
        instance.client = http
        return instance

    monkeypatch.setattr(cli, "AgentClient", client)
    monkeypatch.setattr(cli, "settings", lambda: scheduler_config)
    result = CliRunner().invoke(
        cli.app,
        [
            "scheduler-plan",
            "--scheduler-id",
            "mpt-codex",
            "--contract-version",
            "2",
            "--run-id",
            run["run_id"],
        ],
    )
    assert result.exit_code == 0, result.stdout + str(result.exception)
    worker = json.loads(result.stdout)["worker_requests"][0]
    envelope = offer_path(Path(worker["launcher"]), worker["request_id"]).with_suffix(
        ".json"
    )
    frozen = json.loads(envelope.read_bytes())
    assert (
        frozen["run_id"] == run["run_id"]
        and frozen["content_id"] == target
        and frozen["run_generation"] == 1
    )
    agent_config = client_for(http, "research").settings
    agent_config.agent_id = "mpt-codex-research"
    agent_config.workspace_dir = scheduler_config.workspace_dir
    monkeypatch.setattr(cli, "settings", lambda: agent_config)
    assert verify_fixture_worker(http, worker).status_code == 200
    digest = hashlib.sha256(envelope.read_bytes()).hexdigest()
    dispatched = CliRunner().invoke(
        cli.app, ["dispatch-offer", "--offer", str(envelope), "--digest", digest]
    )
    assert dispatched.exit_code == 0, dispatched.stdout + str(dispatched.exception)
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert (
            task.status == TaskStatus.LEASED
            and task.assigned_agent_id == worker["agent_id"]
        )
    replay = CliRunner().invoke(
        cli.app, ["dispatch-offer", "--offer", str(envelope), "--digest", digest]
    )
    assert replay.exit_code == 20 and "HANDOFF_ERROR" in replay.stdout


def test_cli_legacy_plan_rejects_run_option_before_http(system, monkeypatch):
    from typer.testing import CliRunner

    _, http = system
    monkeypatch.setattr(cli, "settings", lambda: scheduler(http).settings)
    monkeypatch.setattr(
        cli, "AgentClient", lambda *_: pytest.fail("legacy run option reached HTTP")
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "scheduler-plan",
            "--scheduler-id",
            "mpt-codex",
            "--run-id",
            "run_" + "1" * 32,
        ],
    )
    assert result.exit_code != 0


@pytest.mark.parametrize("generation", [True, "1", 1.0])
def test_dispatch_generation_requires_json_integer(system, generation):
    runtime, http = system
    target, _ = selected(http, runtime, "generation-type")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    assert scoped_dispatch(http, worker, run_generation=generation).status_code == 422


def test_default_core_claim_hook_retains_unscoped_behavior(system):
    from glance_core.task_service import AgentTaskService

    runtime, _ = system
    with Session(runtime.engine) as session:
        service = AgentTaskService(session, runtime.settings.agent_submission_dir)
        task = service.create_task(
            task_type=TaskType.SOURCE_DISCOVERY,
            instructions="ordinary core contract",
            input_data={"source_id": "core-only"},
        )
        claimed, token = service.claim(
            agent_id="core-worker",
            role_id="discovery",
            capabilities={TaskType.SOURCE_DISCOVERY},
            task_id=task.id,
        )
        assert claimed.status == TaskStatus.LEASED and claimed.attempt == 1 and token


@pytest.mark.parametrize("permission", [False, True])
@pytest.mark.parametrize("task_kind", [TaskType.ASSET_GENERATE_IMAGE, TaskType.RECOVERY])
def test_image_permission_reaches_only_scoped_image_or_readonly_recovery_offer(system, monkeypatch, permission, task_kind):
    import sys
    from pathlib import Path

    from glance_core.task_service import stable_hash
    from glance_core.agent_roles import primary_role_for
    from app.business import conductor_launch as launch
    from app.business.content_run import FROZEN_FIELDS
    from app.controllers.v1.content_execution import router
    from test.business.test_content_execution_http import Process

    runtime, http = system
    http.app.include_router(router)
    content_id, _ = selected(http, runtime, "image-permission")
    deploy(http, stage(runtime, primary_role_for(task_kind).value))
    task_input = {"requests": [{
        "segment_index": 0, "prompt": "Conceptual AI illustration",
        "min_width": 1080, "min_height": 1920, "aspect_ratio": "9:16",
    }]} if task_kind == TaskType.ASSET_GENERATE_IMAGE else {
        "failure": {"failure_class": "ENVIRONMENT", "error_code": "LOCAL_IMAGE_AUTHORIZATION_MISSING"},
        "policy": {"allowed_actions": ["retry_same_task"]},
    }
    with Session(runtime.engine) as session:
        task = runtime.task_service(session).create_task(
            task_type=task_kind, content_id=content_id,
            instructions="Frozen permission scope fixture", input_data=task_input,
        )
        task_id, input_before, hash_before = task.id, task.input_data, task.input_hash
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=task.id, action=OWNED, metadata_json={"input_hash": task.input_hash}))
        session.commit()
    process = Process()
    monkeypatch.setattr(launch, "launch_command", lambda *args: [sys.executable])
    monkeypatch.setattr(launch, "process_creation_time", lambda _: utc_now().isoformat())
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *args, **kwargs: process)
    endpoint = f"/api/v1/contents/{content_id}/run/execute"
    try:
        response = http.post(endpoint, headers=ADMIN, json={"allow_local_image_adaptation": permission})
        assert response.status_code == 200, response.text
        run = response.json()
        attach_fixture_host(http, http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json())
        plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
        assert plan["status"] == "READY", plan
        targets = [worker for worker in plan["worker_requests"] if worker["task_type"] == task_kind.value]
        others = [worker for worker in plan["worker_requests"] if worker["task_type"] != task_kind.value]
        assert len(targets) == 1 and others
        worker = targets[0]
        assert worker["task_id"] == task_id and worker["input_hash"] == hash_before
        if task_kind == TaskType.ASSET_GENERATE_IMAGE:
            assert ("allow_local_image_adaptation" in worker["prompt"]) is permission
            if permission:
                assert run["run_id"] in worker["prompt"]
                assert '"allow_local_image_adaptation": true' in worker["prompt"]
                assert "保留原始图片" in worker["prompt"] and "独立质量质检" in worker["prompt"]
            else:
                assert "未获本机图片尺寸适配授权" in worker["prompt"]
                assert "LOCAL_IMAGE_AUTHORIZATION_MISSING" in worker["prompt"]
        else:
            assert run["run_id"] in worker["prompt"]
            assert f'"allow_local_image_adaptation": {str(permission).lower()}' in worker["prompt"]
            assert "只读" in worker["prompt"] and "不授予恢复角色本机处理权限" in worker["prompt"]
            assert "冻结恢复预算" in worker["prompt"] and "不得自己改图" in worker["prompt"]
        assert all("allow_local_image_adaptation" not in worker["prompt"] for worker in others)
        frozen = {field: worker[field] for field in FROZEN_FIELDS}
        assert stable_hash(frozen) == worker["offer_hash"]
        altered = {**frozen, "prompt": worker["prompt"] + " forged permission"}
        assert stable_hash(altered) != worker["offer_hash"]
        assert http.post(endpoint, headers=ADMIN, json={"allow_local_image_adaptation": not permission}).status_code == 409
        with Session(runtime.engine) as session:
            saved = session.get(AgentTask, task_id)
            assert saved.input_data == input_before and saved.input_hash == hash_before
            receipt = session.exec(select(AuditEvent).where(AuditEvent.action == OFFER_ACTION, AuditEvent.entity_id == worker["request_id"])).one()
            assert receipt.metadata_json["worker"] == frozen
            assert receipt.metadata_json["worker_hash"] == worker["offer_hash"]
            assert load_run(session).allow_local_image_adaptation is permission
            assert Path(worker["launcher"]).is_file()
    finally:
        process.done.set()
        deadline = time.monotonic() + 5
        while any(handle is process for handle in launch._handles.values()) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not any(handle is process for handle in launch._handles.values())


@pytest.mark.parametrize("lifetime_minutes", [45, 120])
def test_expired_run_with_live_lease_drains_without_extending_ttl(system, lifetime_minutes):
    runtime, http = system
    target, task_id = selected(http, runtime, "expired-drain")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    claim = scoped_dispatch(http, worker).json()
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        before_task = {field: getattr(task, field) for field in ("attempt", "max_attempts", "input_hash", "assigned_agent_id")}
        row = session.get(RuntimeSetting, RUN_KEY)
        value = json.loads(row.value)
        now = utc_now()
        value["created_at"] = (now - timedelta(minutes=lifetime_minutes + 1)).isoformat()
        value["expires_at"] = (now - timedelta(minutes=1)).isoformat()
        row.value = json.dumps(value)
        session.add(row)
        session.commit()
    draining = start(http, target)
    assert draining["state"] == "draining" and draining["run_id"] == run["run_id"]
    assert draining["expires_at"] == value["expires_at"]
    planned = http.get("/api/v1/agent/scheduler/plan", headers=scheduler(http)._headers(), params={
        "scheduler_id": "mpt-codex", "contract_version": 2, "run_id": run["run_id"],
    })
    assert planned.status_code == 409 and planned.json()["detail"] == "CONTENT_RUN_NOT_ACTIVE"
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert {field: getattr(task, field) for field in before_task} == before_task
    agent = client_for(http, "research")
    agent.settings.agent_id = worker["agent_id"]
    agent.confirm_claim(task_id, claim["lease_token"], worker["agent_id"])
    assert agent.heartbeat(task_id, claim["lease_token"])["status"] == "LEASED"
    assert (
        agent.complete(task_id, claim["lease_token"], worker["agent_id"], RESEARCH)[
            "advanced"
        ]
        == "CONSUMED"
    )
    terminal_fixture_worker(http, worker)
    assert (
        http.get(f"/api/v1/contents/{target}/run", headers=ADMIN).json()["state"]
        == "expired"
    )


@pytest.mark.parametrize(
    "scope",
    [
        {"run_id": "run_" + "1" * 32},
        {"run_generation": 1},
        {"request_id": "stale-request"},
        {"run_id": "run_" + "1" * 32, "run_generation": 1},
        {"run_id": "run_" + "1" * 32, "request_id": "stale-request"},
        {"run_generation": 1, "request_id": "stale-request"},
    ],
)
def test_client_partial_run_scope_never_falls_back_to_legacy(system, scope):
    runtime, http = system
    _, task_id = selected(http, runtime, "client-partial-scope")
    with Session(runtime.engine) as session:
        audit_ids = session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
    agent = client_for(http, "research")
    with pytest.raises(ValueError, match="complete frozen run offer"):
        agent.dispatch(wait=0, task_id=task_id, **scope)
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert task.status == TaskStatus.PENDING and task.attempt == 0
        assert not task.assigned_agent_id
        assert (
            session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
            == audit_ids
        )
    # Completely absent scope still executes the existing direct-role contract.
    assert agent.dispatch(wait=0, task_id=task_id)["should_run"]


@pytest.mark.parametrize(
    "scope_args",
    [
        ["--run-id", "run_" + "1" * 32],
        ["--run-generation", "1"],
        ["--request-id", "stale-request"],
        ["--run-id", "run_" + "1" * 32, "--run-generation", "1"],
        ["--run-id", "run_" + "1" * 32, "--request-id", "stale-request"],
        ["--run-generation", "1", "--request-id", "stale-request"],
    ],
)
def test_cli_partial_scope_rejects_before_config_client_or_workspace(
    monkeypatch, scope_args
):
    from typer.testing import CliRunner

    monkeypatch.setattr(
        cli, "settings", lambda: pytest.fail("partial scope read runtime config")
    )
    monkeypatch.setattr(
        cli, "AgentClient", lambda *_: pytest.fail("partial scope initialized client")
    )
    result = CliRunner().invoke(cli.app, ["dispatch", "--wait", "0", *scope_args])
    assert result.exit_code == 20 and "HANDOFF_ERROR" in result.stdout


def test_authorization_exception_rolls_back_before_reusing_session(system):
    from glance_core.database import begin_immediate
    from glance_core.task_service import TaskConflictError
    from sqlalchemy import text

    runtime, http = system
    target, task_id = selected(http, runtime, "rollback-target")
    run = start(http, target)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    with Session(runtime.engine) as rejected:
        before = rejected.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
        with pytest.raises(TaskConflictError, match="CONTENT_RUN_NOT_ACTIVE"):
            runtime.task_service(rejected).claim(
                task_id=task_id,
                agent_id=worker["agent_id"],
                role_id="research",
                capabilities={TaskType.CONTENT_RESEARCH},
                authorization_context=(
                    run["run_id"],
                    run["generation"] + 1,
                    worker["request_id"],
                ),
                runtime_info={
                    "model_name": worker["model"],
                    "reasoning_effort": worker["reasoning_effort"],
                },
            )
        assert not rejected.in_transaction()
        # Keep the rejected Session open: cleanup must not depend on HTTP teardown.
        with Session(runtime.engine) as writer:
            writer.exec(text("PRAGMA busy_timeout=100"))
            begin_immediate(writer)
            task = writer.get(AgentTask, task_id)
            assert task.status == TaskStatus.PENDING and task.attempt == 0
            assert not task.assigned_agent_id
            assert writer.get(SchedulerOffer, worker["request_id"]) is not None
            assert (
                writer.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
                == before
            )
            writer.add(RuntimeSetting(key="test_second_writer", value="lock released"))
            writer.commit()
        # The same Python Session remains reusable after the original exception.
        begin_immediate(rejected)
        assert (
            rejected.get(RuntimeSetting, "test_second_writer").value == "lock released"
        )
        rejected.rollback()
