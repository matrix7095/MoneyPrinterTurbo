"""Trusted host admission races and recovery via real HTTP/SQLite fixtures."""

import copy
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from glance_core.models import (
    AgentTask,
    AuditEvent,
    RuntimeSetting,
    SchedulerOffer,
    TaskStatus,
    utc_now,
    as_utc,
    TaskType,
)
from glance_core.task_service import TaskConflictError
from sqlmodel import Session, select

from app.business.content_host import CLAIM, accepted_binding, authorize_publish
from app.business.content_run import RUN_KEY, load_run
from app.business.quarantine import IsolatedTaskService
from test.business.test_content_http import RESEARCH, client_for
from test.business.test_content_run_http import (
    system as shared_system,
    selected,
    start,
    attach_fixture_host,
    fixture_child,
    verify_fixture_worker,
    terminal_fixture_worker,
)
from test.business.test_deployment_http import scheduler
from test.business.test_discovery_http import ADMIN

system = shared_system


@pytest.fixture
def host_offer(system):
    runtime, http = system
    content_id, task_id = selected(http, runtime, "host-admission")
    run = start(http, content_id)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    return runtime, http, content_id, task_id, run, worker


def offer_path(worker):
    return f"/api/v1/content-runs/{worker['run_id']}/intents/{worker['request_id']}"


def put_intent(http, worker, **overrides):
    return http.put(
        offer_path(worker),
        headers=ADMIN,
        json={
            "generation": worker["run_generation"],
            "host_epoch": worker["host_epoch"],
            "event_id": "spawn-" + worker["request_id"],
            "offer_hash": worker["offer_hash"],
            **overrides,
        },
    )


def observation(http, worker, outcome, *, child=None, event_id=None, **overrides):
    return http.post(
        offer_path(worker) + "/observation",
        headers=ADMIN,
        json={
            "generation": worker["run_generation"],
            "host_epoch": worker["host_epoch"],
            "event_id": event_id or str(uuid4()),
            "outcome": outcome,
            "child": child,
            **overrides,
        },
    )


def admit(http, worker, role="research"):
    return http.get(
        f"/api/v1/agent/content-run-offers/{worker['request_id']}/admission",
        headers=client_for(http, role)._headers(),
        params={
            "agent_id": worker["agent_id"],
            "run_id": worker["run_id"],
            "generation": worker["run_generation"],
        },
    )


def raw_dispatch(http, worker):
    return http.post(
        "/api/v1/agent/dispatch?wait=0",
        headers=client_for(http, "research")._headers(),
        json={
            "role_id": worker["role_id"],
            "agent_id": worker["agent_id"],
            "task_type": worker["task_type"],
            "task_id": worker["task_id"],
            "run_id": worker["run_id"],
            "run_generation": worker["run_generation"],
            "request_id": worker["request_id"],
            "runtime": {
                "model_name": worker["model"],
                "reasoning_effort": worker["reasoning_effort"],
            },
        },
    )


def assert_unclaimed(runtime, task_id):
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert task.status == TaskStatus.PENDING and task.attempt == 0
        assert not task.assigned_agent_id
        assert (
            session.exec(select(AuditEvent).where(AuditEvent.action == CLAIM)).all()
            == []
        )


def test_unattached_host_cannot_offer_and_attach_requires_admin(system):
    runtime, http = system
    content_id, task_id = selected(http, runtime, "unattached")
    run = start(http, content_id, attach=False)
    response = http.get(
        "/api/v1/agent/scheduler/plan",
        headers=scheduler(http)._headers(),
        params={
            "scheduler_id": "mpt-codex",
            "contract_version": 2,
            "run_id": run["run_id"],
        },
    )
    assert response.status_code == 409 and "CONTENT_HOST_NOT_LIVE" in response.text
    body = {
        "generation": 1,
        "expected_host_epoch": 0,
        "parent_thread_id": "fixture-parent",
        "process_identity": {
            "pid": 1,
            "started_at": "2026-10-01T00:00:00+00:00",
            "instance_id": "test",
            "binary_path": "C:/test.exe",
            "binary_sha256": "a" * 64,
            "version": "test",
        },
    }
    assert (
        http.post(
            f"/api/v1/content-runs/{run['run_id']}/host/attach",
            headers=client_for(http, "research")._headers(),
            json=body,
        ).status_code
        == 401
    )
    assert (
        http.post(
            f"/api/v1/content-runs/{run['run_id']}/host/attach",
            headers=ADMIN,
            json={**body, "untrusted": True},
        ).status_code
        == 422
    )
    assert_unclaimed(runtime, task_id)


def test_child_racing_binding_waits_and_frozen_fields_are_server_issued(host_offer):
    runtime, http, _, task_id, run, worker = host_offer
    assert worker["native_agent_type"] == "default" and worker["fork_turns"] == "none"
    assert worker["host_epoch"] == run["host_epoch"] == 1
    assert (
        worker["title"]
        and worker["prompt"]
        and worker["offer_hash"]
        and worker["round_id"]
    )
    assert admit(http, worker).json()["status"] == "WAITING"
    assert raw_dispatch(http, worker).status_code == 409
    assert put_intent(http, worker).status_code == 200
    assert admit(http, worker).json()["status"] == "WAITING"
    assert raw_dispatch(http, worker).status_code == 409
    assert_unclaimed(runtime, task_id)
    assert (
        observation(http, worker, "VERIFIED", child=fixture_child(worker)).status_code
        == 200
    )
    assert admit(http, worker).json()["status"] == "READY"
    assert admit(http, worker, "visual").status_code == 409


@pytest.mark.parametrize(
    "field,value",
    [
        ("model", "gpt-6-luna"),
        ("reasoning_effort", "max"),
        ("parent_thread_id", "other-parent"),
        ("native_agent_type", "researcher"),
        ("ephemeral", False),
        ("issued_prompt_sha256", "0" * 64),
    ],
)
def test_wrong_child_configuration_never_leases(host_offer, field, value):
    runtime, http, _, task_id, _, worker = host_offer
    assert put_intent(http, worker).status_code == 200
    child = fixture_child(worker)
    child[field] = value
    assert observation(http, worker, "VERIFIED", child=child).status_code == 409
    assert raw_dispatch(http, worker).status_code == 409
    assert_unclaimed(runtime, task_id)


def test_binding_response_loss_replays_without_rebinding_or_releasing_new_offer(
    host_offer,
):
    runtime, http, _, task_id, _, worker = host_offer
    put_intent(http, worker)
    child = fixture_child(worker)
    response = observation(
        http, worker, "VERIFIED", child=child, event_id="lost-bind-response"
    )
    assert response.status_code == 200
    claim = raw_dispatch(http, worker)
    assert claim.status_code == 200 and claim.json()["should_run"]
    repeated = observation(
        http, worker, "VERIFIED", child=child, event_id="lost-bind-response"
    )
    assert repeated.json() == response.json()
    changed = observation(
        http, worker, "HEARTBEAT", child=child, event_id="lost-bind-response"
    )
    assert changed.status_code == 409
    with Session(runtime.engine) as session:
        run = load_run(session)
        assert run.intents[worker["request_id"]]["state"] == "CLAIMED"
        assert (
            len(
                session.exec(select(AuditEvent).where(AuditEvent.action == CLAIM)).all()
            )
            == 1
        )
        assert session.get(AgentTask, task_id).attempt == 0


def test_claim_response_loss_is_one_lease_and_completion_can_drain_idempotently(
    host_offer,
):
    runtime, http, content_id, task_id, run, worker = host_offer
    assert verify_fixture_worker(http, worker).status_code == 200
    claim = raw_dispatch(http, worker).json()
    assert claim["should_run"]
    assert raw_dispatch(http, worker).status_code == 409
    stopped = http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN).json()
    assert stopped["state"] == "draining"
    agent = client_for(http, "research")
    agent.settings.agent_id = worker["agent_id"]
    agent.confirm_claim(task_id, claim["lease_token"], worker["agent_id"])
    first = agent.complete(task_id, claim["lease_token"], worker["agent_id"], RESEARCH)
    assert first["advanced"] == "CONSUMED"
    second = agent.complete(task_id, claim["lease_token"], worker["agent_id"], RESEARCH)
    assert second["status"] == first["status"]
    view = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    summary = view["intents"][worker["request_id"]]["claimbinding_summary"]
    assert (
        summary["accepted"] and summary["consumed"] and summary["current_attempt"] == 1
    )
    assert summary["accepted_result_hash"] == summary["consumed_result_hash"]
    terminal_fixture_worker(http, worker)
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "stopped"
    )
    with Session(runtime.engine) as session:
        assert (
            len(
                session.exec(select(AuditEvent).where(AuditEvent.action == CLAIM)).all()
            )
            == 1
        )
        assert session.get(AgentTask, task_id).attempt == 1


@pytest.mark.parametrize("outcome", ["UNKNOWN", "OBSERVATION_LOST", "REJECTED"])
def test_unknown_spawn_is_not_reissued_or_assumed_terminal(host_offer, outcome):
    runtime, http, content_id, task_id, run, worker = host_offer
    put_intent(http, worker)
    assert observation(http, worker, outcome).status_code == 200
    assert admit(http, worker).json()["status"] == "BLOCKED"
    response = http.get(
        "/api/v1/agent/scheduler/plan",
        headers=scheduler(http)._headers(),
        params={
            "scheduler_id": "mpt-codex",
            "contract_version": 2,
            "run_id": run["run_id"],
        },
    )
    assert response.status_code == 409 and "OUTCOME_UNRESOLVED" in response.text
    assert (
        http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN).json()[
            "state"
        ]
        == "draining"
    )
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "draining"
    )
    assert_unclaimed(runtime, task_id)


def test_ephemeral_host_observation_expiry_blocks_but_same_handle_heartbeat_refreshes(
    host_offer,
):
    runtime, http, content_id, _, _, worker = host_offer
    verify_fixture_worker(http, worker)
    with Session(runtime.engine) as session:
        row = session.get(RuntimeSetting, RUN_KEY)
        value = json.loads(row.value)
        value["host_observed_at"] = (utc_now() - timedelta(seconds=181)).isoformat()
        row.value = json.dumps(value)
        session.add(row)
        session.commit()
    assert admit(http, worker).json()["status"] == "BLOCKED"
    view = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    assert (
        not view["host_live"]
        and view["intents"][worker["request_id"]]["state"] == "VERIFIED"
    )
    child = fixture_child(worker)
    child["metadata_rpc_id"] = "new-same-handle-observation"
    assert observation(http, worker, "HEARTBEAT", child=child).status_code == 200
    assert admit(http, worker).json()["status"] == "READY"
    http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN)
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "draining"
    )


def test_attach_same_identity_is_idempotent_and_takeover_is_cas_blocked(host_offer):
    runtime, http, _, _, run, worker = host_offer
    assert attach_fixture_host(http, run)["host_epoch"] == 1
    put_intent(http, worker)
    with Session(runtime.engine) as session:
        record = load_run(session)
        identity = copy.deepcopy(record.process_identity)
        identity["instance_id"] = "different-process"
    response = http.post(
        f"/api/v1/content-runs/{run['run_id']}/host/attach",
        headers=ADMIN,
        json={
            "generation": run["generation"],
            "expected_host_epoch": 1,
            "parent_thread_id": "new-parent",
            "process_identity": identity,
        },
    )
    assert response.status_code == 409 and "TAKEOVER_BLOCKED" in response.text


def test_post_claim_hook_failure_rolls_back_entire_reservation(host_offer, monkeypatch):
    runtime, http, _, task_id, _, worker = host_offer
    verify_fixture_worker(http, worker)

    def refused(*args, **kwargs):
        raise TaskConflictError("fixture post-claim rejected")

    monkeypatch.setattr(IsolatedTaskService, "_after_claim", refused)
    assert raw_dispatch(http, worker).status_code == 409
    assert_unclaimed(runtime, task_id)
    with Session(runtime.engine) as session:
        assert session.get(SchedulerOffer, worker["request_id"]) is not None
        assert load_run(session).intents[worker["request_id"]]["state"] == "VERIFIED"


def test_publish_requires_live_host_and_unknown_outcome_never_reauthorizes(host_offer):
    runtime, http, _, task_id, _, worker = host_offer
    verify_fixture_worker(http, worker)
    assert raw_dispatch(http, worker).json()["should_run"]
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        assert accepted_binding(session, task, worker["agent_id"]) is not None
        authorize_publish(session, task)
    assert observation(http, worker, "UNKNOWN").status_code == 200
    with Session(runtime.engine) as session:
        with pytest.raises(TaskConflictError, match="OUTCOME_UNRESOLVED"):
            authorize_publish(session, session.get(AgentTask, task_id))


def test_only_nonempty_ready_rounds_charge_server_round_budget(host_offer):
    _, http, content_id, _, run, worker = host_offer
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    assert plan["worker_requests"] == []
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()[
            "ready_rounds"
        ]
        == 1
    )
    for index in range(1, 17):
        assert put_intent(http, worker).status_code == 200
        assert (
            observation(
                http, worker, "TERMINAL", child=fixture_child(worker)
            ).status_code
            == 200
        )
        next_plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
        if index < 16:
            assert next_plan["ready_rounds"] == index + 1
            worker = next_plan["worker_requests"][0]
        else:
            assert (
                next_plan["status"] == "BLOCKED" and next_plan["worker_requests"] == []
            )
            assert next_plan["blocked"] == [{"reason": "CONTENT_RUN_ROUND_LIMIT"}]


def test_scoped_launcher_waits_without_consuming_before_verified_admission(
    host_offer, monkeypatch
):
    import hashlib
    import time
    from pathlib import Path
    from glance_agent import cli
    from glance_agent.client import AgentClient
    from glance_agent.scheduler_handoff import (
        materialize_offer,
        offer_path as local_offer_path,
    )
    from typer.testing import CliRunner

    _, http, _, task_id, _, worker = host_offer
    assert put_intent(http, worker).status_code == 200
    launcher = Path(worker["launcher"])
    materialize_offer(worker, "mpt-codex", launcher, time.time() + 540)
    envelope = local_offer_path(launcher, worker["request_id"]).with_suffix(".json")
    config = client_for(http, "research").settings
    config.agent_id = "mpt-codex-research"
    config.workspace_dir = launcher.parent / "workspaces"
    calls = []

    def make_client(settings):
        instance = AgentClient(settings)
        instance.client.close()
        instance.client = http
        original = instance.offer_admission

        def checked(worker_request):
            assert not envelope.with_suffix(".consumed").exists()
            if calls:
                assert (
                    observation(
                        http, worker, "VERIFIED", child=fixture_child(worker)
                    ).status_code
                    == 200
                )
            result = original(worker_request)
            calls.append(result["status"])
            return result

        instance.offer_admission = checked
        return instance

    monkeypatch.setattr(cli, "settings", lambda: config)
    monkeypatch.setattr(cli, "AgentClient", make_client)
    monkeypatch.setattr(cli.time, "sleep", lambda *_: None)
    result = CliRunner().invoke(
        cli.app,
        [
            "dispatch-offer",
            "--offer",
            str(envelope),
            "--digest",
            hashlib.sha256(envelope.read_bytes()).hexdigest(),
        ],
    )
    assert result.exit_code == 0, result.stdout + str(result.exception)
    assert calls == ["WAITING", "READY"]
    assert envelope.with_suffix(".consumed").is_file()
    assert task_id in result.stdout


def test_stopping_spawn_before_binding_can_record_real_terminal_without_granting_lease(
    host_offer,
):
    runtime, http, content_id, task_id, _, worker = host_offer
    put_intent(http, worker)
    assert (
        http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN).json()[
            "state"
        ]
        == "draining"
    )
    assert (
        observation(http, worker, "VERIFIED", child=fixture_child(worker)).status_code
        == 409
    )
    terminal_fixture_worker(http, worker)
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "stopped"
    )
    assert_unclaimed(runtime, task_id)


@pytest.mark.parametrize("field", ["metadata_rpc_id", "ephemeral"])
def test_native_metadata_boolean_is_not_coerced_to_rpc_id_or_ephemeral(
    host_offer, field
):
    _, http, _, _, _, worker = host_offer
    put_intent(http, worker)
    child = fixture_child(worker)
    child[field] = True if field == "metadata_rpc_id" else 1
    assert observation(http, worker, "VERIFIED", child=child).status_code == 422


@pytest.mark.parametrize("blocked", ["unknown", "stopped", "host-expired"])
def test_actual_http_publish_intent_refuses_unsafe_scoped_host(system, blocked):
    from pathlib import Path
    from glance_core.agent_roles import role_spec
    from test.business.test_deployment_http import stage, deploy
    from test.business.test_publish_policy import seed_publish

    runtime, http = system
    spec = role_spec("publish")
    deployment = stage(runtime, "publish")
    manifest_path = Path(deployment["launcher_path"]).with_suffix(".json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = list(spec.required_host_capabilities)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    deploy(http, deployment)
    content_id, task_id = seed_publish(runtime, automatic=False)
    run = start(http, content_id)
    worker = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])[
        "worker_requests"
    ][0]
    assert worker["task_id"] == task_id
    assert verify_fixture_worker(http, worker).status_code == 200
    client = client_for(http, "publish")
    report = {
        **client.settings.runtime_report(),
        "model_name": worker["model"],
        "reasoning_effort": worker["reasoning_effort"],
        "skill_name": spec.skill_name,
        "skill_version": spec.skill_version,
        "capabilities": list(spec.required_host_capabilities),
    }
    claim_response = http.post(
        "/api/v1/agent/dispatch?wait=0",
        headers=client._headers(),
        json={
            "role_id": "publish",
            "agent_id": worker["agent_id"],
            "task_id": task_id,
            "run_id": worker["run_id"],
            "run_generation": worker["run_generation"],
            "request_id": worker["request_id"],
            "runtime": report,
        },
    )
    assert claim_response.status_code == 200, claim_response.text
    claim = claim_response.json()
    assert claim["should_run"]
    client.confirm_claim(task_id, claim["lease_token"], worker["agent_id"])
    if blocked == "unknown":
        assert observation(http, worker, "UNKNOWN").status_code == 200
    elif blocked == "stopped":
        http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN)
    else:
        with Session(runtime.engine) as session:
            row = session.get(RuntimeSetting, RUN_KEY)
            record = json.loads(row.value)
            record["host_observed_at"] = (
                utc_now() - timedelta(seconds=181)
            ).isoformat()
            row.value = json.dumps(record)
            session.add(row)
            session.commit()
    response = http.post(
        f"/api/v1/agent/tasks/{task_id}/publish-intent",
        headers={"Authorization": f"Bearer {claim['lease_token']}"},
        json={"agent_id": worker["agent_id"]},
    )
    assert response.status_code == 409 and "CONTENT_" in response.text
    with Session(runtime.engine) as session:
        assert (
            session.exec(
                select(AuditEvent).where(AuditEvent.entity_type == "publish_intent")
            ).all()
            == []
        )


def test_cli_blocked_admission_leaves_offer_unconsumed(host_offer, monkeypatch):
    import hashlib
    import time
    from pathlib import Path
    from glance_agent import cli
    from glance_agent.client import AgentClient
    from glance_agent.scheduler_handoff import (
        materialize_offer,
        offer_path as local_offer_path,
    )
    from typer.testing import CliRunner

    _, http, content_id, _, _, worker = host_offer
    launcher = Path(worker["launcher"])
    materialize_offer(worker, "mpt-codex", launcher, time.time() + 540)
    envelope = local_offer_path(launcher, worker["request_id"]).with_suffix(".json")
    http.post(f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN)
    config = client_for(http, "research").settings
    config.agent_id = "mpt-codex-research"
    config.workspace_dir = launcher.parent / "workspaces"

    def make_client(settings):
        instance = AgentClient(settings)
        instance.client.close()
        instance.client = http
        return instance

    monkeypatch.setattr(cli, "settings", lambda: config)
    monkeypatch.setattr(cli, "AgentClient", make_client)
    result = CliRunner().invoke(
        cli.app,
        [
            "dispatch-offer",
            "--offer",
            str(envelope),
            "--digest",
            hashlib.sha256(envelope.read_bytes()).hexdigest(),
        ],
    )
    assert result.exit_code == 20 and "HANDOFF_ERROR" in result.stdout
    assert not envelope.with_suffix(".consumed").exists()


@pytest.mark.parametrize(
    "phase",
    ["verified-offer-expired", "claimed-lease-expired", "confirmed-lease-expired"],
)
def test_live_native_child_retains_reservation_after_offer_or_lease_expiry(
    host_offer, phase
):
    runtime, http, content_id, task_id, run, worker = host_offer
    assert verify_fixture_worker(http, worker).status_code == 200
    if phase in {"claimed-lease-expired", "confirmed-lease-expired"}:
        claim = raw_dispatch(http, worker).json()
        assert claim["should_run"]
        if phase == "confirmed-lease-expired":
            client_for(http, "research").confirm_claim(
                task_id, claim["lease_token"], worker["agent_id"]
            )
    with Session(runtime.engine) as session:
        if phase == "verified-offer-expired":
            row = session.get(SchedulerOffer, worker["request_id"])
            row.expires_at = utc_now() - timedelta(seconds=1)
        else:
            row = session.get(AgentTask, task_id)
            row.lease_expires_at = utc_now() - timedelta(seconds=1)
        session.add(row)
        session.commit()
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    assert plan["worker_requests"] == []
    ordinary = client_for(http, "research")
    assert ordinary.claim()["status"] == "NO_TASK"
    response = http.post(
        "/api/v1/agent/dispatch?wait=0",
        headers=ordinary._headers(),
        json={
            "agent_id": ordinary.settings.agent_id,
            "role_id": "research",
            "task_id": task_id,
        },
    )
    assert response.status_code == 409
    terminal_fixture_worker(http, worker)
    released = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    if phase == "confirmed-lease-expired":
        assert released["worker_requests"] == []
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert task.attempt == 1 and task.not_before is not None
            assert as_utc(task.not_before) > utc_now()
            assert load_run(session).ready_rounds == 1
        return
    assert (
        released["status"] == "READY"
        and released["worker_requests"][0]["task_id"] == task_id
    )
    assert released["worker_requests"][0]["agent_id"] != worker["agent_id"]
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, task_id).attempt == 0
        assert load_run(session).ready_rounds == 2


@pytest.mark.parametrize("expired", [False, True])
def test_native_reservation_counts_global_type_host_capacity_once(host_offer, expired):
    runtime, http, content_id, task_id, run, worker = host_offer
    assert verify_fixture_worker(http, worker).status_code == 200
    with Session(runtime.engine) as session:
        for index in range(2):
            runtime.task_service(session).create_task(
                task_type=TaskType.CONTENT_RESEARCH,
                content_id=content_id,
                instructions="capacity-only fixture",
                input_data={"variant": index},
            )
        if expired:
            offer = session.get(SchedulerOffer, worker["request_id"])
            offer.expires_at = utc_now() - timedelta(seconds=1)
            session.add(offer)
            session.commit()
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    assert len(plan["worker_requests"]) == 1
    assert plan["worker_requests"][0]["task_id"] != task_id


def machine_binding(view):
    return {
        key: copy.deepcopy(view[key])
        for key in (
            "run_id",
            "generation",
            "host_epoch",
            "parent_thread_id",
            "process_identity",
        )
    }


@pytest.mark.parametrize(
    "field",
    [
        "run_id",
        "generation",
        "host_epoch",
        "parent_thread_id",
        "pid",
        "started_at",
        "instance_id",
        "binary_path",
        "binary_sha256",
        "version",
    ],
)
def test_foreign_machine_stop_binding_has_no_state_offer_or_audit_side_effects(
    host_offer, field
):
    runtime, http, content_id, _, _, _ = host_offer
    view = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    binding = machine_binding(view)
    if field == "run_id":
        binding[field] = "run_" + "0" * 32
    elif field in {"generation", "host_epoch"}:
        binding[field] += 1
    elif field == "parent_thread_id":
        binding[field] = "foreign-parent"
    else:
        identity = binding["process_identity"]
        identity[field] = {
            "pid": identity["pid"] + 1,
            "started_at": "2026-10-02T00:00:00+00:00",
            "instance_id": "foreign-instance",
            "binary_path": "C:/foreign/codex.exe",
            "binary_sha256": "0" * 64,
            "version": "foreign-version",
        }[field]
    with Session(runtime.engine) as session:
        before = session.get(RuntimeSetting, RUN_KEY).value
        offers = [
            row.model_dump(mode="json")
            for row in session.exec(select(SchedulerOffer)).all()
        ]
        audits = session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
    response = http.post(
        f"/api/v1/contents/{content_id}/run/stop",
        headers=ADMIN,
        json={"expected_binding": binding},
    )
    assert response.status_code == 409 and "STOP_BINDING_CHANGED" in response.text
    with Session(runtime.engine) as session:
        assert session.get(RuntimeSetting, RUN_KEY).value == before
        assert [
            row.model_dump(mode="json")
            for row in session.exec(select(SchedulerOffer)).all()
        ] == offers
        assert (
            session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all() == audits
        )


def test_view_then_host_takeover_rejects_old_machine_stop_atomically(host_offer):
    runtime, http, content_id, _, _, _ = host_offer
    old = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    with Session(runtime.engine) as session:
        row = session.get(RuntimeSetting, RUN_KEY)
        value = json.loads(row.value)
        value["host_observed_at"] = (utc_now() - timedelta(seconds=181)).isoformat()
        row.value = json.dumps(value)
        session.add(row)
        session.commit()
    identity = {
        **old["process_identity"],
        "instance_id": "new-own-process",
        "pid": old["process_identity"]["pid"] + 1,
    }
    attached = http.post(
        f"/api/v1/content-runs/{old['run_id']}/host/attach",
        headers=ADMIN,
        json={
            "generation": old["generation"],
            "expected_host_epoch": old["host_epoch"],
            "parent_thread_id": "new-parent",
            "process_identity": identity,
        },
    )
    assert attached.status_code == 200 and attached.json()["host_epoch"] == 2
    with Session(runtime.engine) as session:
        before = session.get(RuntimeSetting, RUN_KEY).value
        audits = session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all()
    response = http.post(
        f"/api/v1/contents/{content_id}/run/stop",
        headers=ADMIN,
        json={"expected_binding": machine_binding(old)},
    )
    assert response.status_code == 409
    with Session(runtime.engine) as session:
        assert session.get(RuntimeSetting, RUN_KEY).value == before
        assert (
            session.exec(select(AuditEvent.id).order_by(AuditEvent.id)).all() == audits
        )
        assert (
            load_run(session).state == "running"
            and not load_run(session).stop_requested
        )


def test_lost_attach_response_get_own_binding_allows_machine_stop_and_valid_drain(
    host_offer,
):
    runtime, http, content_id, task_id, _, worker = host_offer
    assert verify_fixture_worker(http, worker).status_code == 200
    claim = raw_dispatch(http, worker).json()
    assert claim["should_run"]
    own = http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    response = http.post(
        f"/api/v1/contents/{content_id}/run/stop",
        headers=ADMIN,
        json={"expected_binding": machine_binding(own)},
    )
    assert response.status_code == 200 and response.json()["state"] == "draining"
    agent = client_for(http, "research")
    agent.settings.agent_id = worker["agent_id"]
    agent.confirm_claim(task_id, claim["lease_token"], worker["agent_id"])
    assert (
        agent.complete(task_id, claim["lease_token"], worker["agent_id"], RESEARCH)[
            "advanced"
        ]
        == "CONSUMED"
    )
    terminal_fixture_worker(http, worker)
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "stopped"
    )
    with Session(runtime.engine) as session:
        event = session.exec(
            select(AuditEvent).where(
                AuditEvent.action == "mpt_content_run_machine_stop_requested"
            )
        ).one()
        assert event.actor_type == "system"


def test_partial_machine_stop_body_does_not_fall_back_to_human_stop(host_offer):
    _, http, content_id, _, _, _ = host_offer
    response = http.post(
        f"/api/v1/contents/{content_id}/run/stop", headers=ADMIN, json={}
    )
    assert response.status_code == 422
    assert (
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()["state"]
        == "running"
    )


def test_rejected_machine_stop_releases_python_session_write_lock(host_offer):
    from app.business.content_run import get_run
    from app.business.requests import ExpectedHostBinding
    from glance_core.database import begin_immediate
    from sqlalchemy import text

    runtime, http, content_id, _, _, _ = host_offer
    binding = machine_binding(
        http.get(f"/api/v1/contents/{content_id}/run", headers=ADMIN).json()
    )
    binding["parent_thread_id"] = "foreign-parent"
    with Session(runtime.engine) as rejected:
        with pytest.raises(TaskConflictError, match="STOP_BINDING_CHANGED"):
            get_run(
                rejected,
                content_id,
                stop=True,
                expected_binding=ExpectedHostBinding.model_validate(binding),
            )
        assert not rejected.in_transaction()
        with Session(runtime.engine) as writer:
            writer.exec(text("PRAGMA busy_timeout=100"))
            begin_immediate(writer)
            assert load_run(writer).state == "running"
            writer.rollback()
