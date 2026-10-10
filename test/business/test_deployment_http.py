import hashlib
import json
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from fastapi.testclient import TestClient
from glance_agent import cli
from glance_agent.client import AgentClient
from glance_agent.scheduler_handoff import (
    consume_offer,
    materialize_offer,
    offer_path,
    validate_worker,
)
from glance_agent.settings import AgentSettings
from glance_core.agent_roles import role_spec
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Content,
    ContentStatus,
    ExecutorNode,
    SchedulerOffer,
    StageRun,
    StageStatus,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.scheduler_profiles import default_scheduler_config
from glance_core.security import issue_role_token, issue_scheduler_token
from glance_core.task_service import TaskConflictError
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.quarantine import CONTENT_QUARANTINE, TASK_QUARANTINE
from app.business.scheduler import tree_hash
from app.business.runtime import AGENT_CONSUMED, CONSUMED
from app.controllers.v1.deployment import router
from test.business.test_content_http import (
    RESEARCH,
    client_for,
    content_app,
    select_and_claim,
)
from test.business.test_discovery_http import ADMIN, RESULT, agent_client, make_runtime


def app_for(runtime, tmp_path):
    runtime.workspace_root = tmp_path
    app = content_app(runtime)
    app.include_router(router)
    return app


def scheduler(http, identity="mpt-codex"):
    client = AgentClient(
        AgentSettings(
            token=issue_scheduler_token("test-root", identity),
            agent_id=identity,
            role_id="scheduler",
        )
    )
    client.client.close()
    client.client = http
    return client


def enable(http, enabled=True):
    config = default_scheduler_config()
    config.update(scheduler_id="mpt-codex", enabled=enabled, max_parallel=2)
    for profile in config["task_profiles"]:
        if profile["task_type"] == "source_discovery":
            profile.update(model="gpt-6-luna", reasoning_effort="max")
    response = http.put("/api/v1/agent/scheduler/config", headers=ADMIN, json=config)
    assert response.status_code == 200, response.text
    return config


def stage(runtime, role="discovery"):
    spec = role_spec(role)
    root = runtime.workspace_root / "storage" / "agent-launcher-staging" / "fixture"
    root.mkdir(parents=True, exist_ok=True)
    skill = root / "skills" / spec.skill_name
    _, source = cli.bundled_role_skill_path(role)
    shutil.copytree(source, skill)
    credentials = runtime.workspace_root / "storage" / "private-fixture.json"
    credentials.write_text("{}", encoding="utf-8")
    identity = f"mpt-codex-{role}"
    manifest = {
        "workspace": str(runtime.workspace_root),
        "runtime_config": str(credentials),
        "scheduler_id": "mpt-codex",
        "agent_id": identity,
        "role_id": role,
        "skill_name": spec.skill_name,
        "skill_version": spec.skill_version,
        "skill_path": str(skill),
        "capabilities": [],
        "workspace_dir": str(root / "workspaces"),
    }
    manifest_path = root / f"{identity}.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    launcher = manifest_path.with_suffix(".ps1")

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    launcher.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"& {quote(sys.executable)} {quote(runtime.workspace_root / 'scripts' / 'mpt_agent_launchers.py')} run --manifest {quote(manifest_path)} @CommandArgs\nexit $LASTEXITCODE\n",
        encoding="utf-8-sig",
    )
    return {
        "agent_id": identity,
        "role_id": role,
        "launcher_path": str(launcher),
        "skill": {
            "name": spec.skill_name,
            "path": str(skill),
            "content_hash": tree_hash(skill),
        },
        "host": {
            "device_label": "isolated-tests",
            "max_concurrency": 2,
            "supported_models": ["gpt-6.1-sol", "gpt-6-luna"],
        },
        "model": {"name": "gpt-6-luna", "reasoning_effort": "max"},
        "protocol_version": "1",
        "request_selftest": False,
        "self_check": dict.fromkeys(
            ["cli", "connectivity", "skill_installed", "launcher_executable"], "pass"
        ),
    }


def deploy(http, payload):
    agent = client_for(http, payload["role_id"])
    agent.settings.agent_id = payload["agent_id"]
    agent.whoami()
    response = agent.deployment(payload)
    assert response["status"] == "VERIFIED", response
    return agent


def sign(http, direction):
    response = http.post(
        "/api/v1/discovery/search-tasks", headers=ADMIN, json={"direction": direction}
    )
    assert response.status_code == 200, response.text
    return response.json()["task_id"]


@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-sol"])
def test_deployment_and_contract2_offer_is_real_scoped_fresh_and_one_use(
    tmp_path, model
):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        plan = scheduler(http, "codex").scheduler_plan("codex", contract_version=2)
        assert plan["status"] == "DISABLED" and plan["contract_version"] == 2
        config = enable(http)
        for profile in config["task_profiles"]:
            if profile["task_type"] == "source_discovery":
                profile["model"] = model
        assert (
            http.put(
                "/api/v1/agent/scheduler/config", headers=ADMIN, json=config
            ).status_code
            == 200
        )
        sch = scheduler(http)
        assert (
            sch.scheduler_plan("mpt-codex", contract_version=2)["status"] == "NO_TASK"
        )
        task_id = sign(http, "fresh scheduler contract")
        assert (
            sch.scheduler_plan("mpt-codex", contract_version=2)["status"] == "BLOCKED"
        )
        payload = stage(runtime)
        agent = deploy(http, payload)
        ready = sch.scheduler_plan("mpt-codex", contract_version=2)
        assert ready["status"] == "READY" and ready["contract_version"] == 2
        worker = ready["worker_requests"][0]
        assert worker["task_id"] == task_id
        assert (worker["model"], worker["reasoning_effort"]) == (model, "max")
        assert re.fullmatch(r"[a-z0-9_]+", worker["title"])
        assert worker["title"] == (
            f"mpt_worker_source_discovery_{task_id}_"
            f"{worker['request_id'].rsplit(':', 1)[-1]}"
        )
        assert worker["agent_id"] != payload["agent_id"]
        roles = cli._handoff_roles()
        launcher = validate_worker(
            worker,
            "mpt-codex",
            __import__("pathlib").Path(payload["launcher_path"]).parent,
            roles,
        )
        materialize_offer(worker, "mpt-codex", launcher, time.time() + 540)
        path = offer_path(launcher, worker["request_id"]).with_suffix(".json")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        consumed = consume_offer(path, digest, agent.settings, roles)
        assert (
            consumed["model"] == worker["model"]
            and consumed["reasoning_effort"] == worker["reasoning_effort"]
        )
        with pytest.raises(FileExistsError):
            consume_offer(path, digest, agent.settings, roles)
        assert (
            sch.scheduler_plan("mpt-codex", contract_version=2)["worker_requests"] == []
        )
        agent.settings.agent_id = worker["agent_id"]
        claimed = agent.dispatch(
            wait=0, task_type=worker["task_type"], task_id=worker["task_id"]
        )
        assert claimed["should_run"]
        agent.confirm_claim(task_id, claimed["lease_token"], worker["agent_id"])
        assert (
            agent.complete(task_id, claimed["lease_token"], worker["agent_id"], RESULT)[
                "advanced"
            ]
            == "CONSUMED"
        )
        with Session(runtime.engine) as session:
            assert session.exec(select(ExecutorNode)).all() == []


@pytest.mark.parametrize(
    "bad",
    ["role", "protocol", "hash", "capacity", "model", "selftest", "path", "wrapper"],
)
def test_deployment_rejects_invalid_actual_contract(tmp_path, bad):
    from pathlib import Path

    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        payload = stage(runtime)
        agent = client_for(http, "discovery")
        agent.settings.agent_id = payload["agent_id"]
        agent.whoami()
        if bad == "role":
            payload["role_id"] = "research"
        elif bad == "protocol":
            payload["protocol_version"] = "0"
        elif bad == "hash":
            payload["skill"]["content_hash"] = "sha256:bad"
        elif bad == "capacity":
            payload["host"]["max_concurrency"] = 0
        elif bad == "model":
            payload["model"]["name"] = "unknown"
        elif bad == "selftest":
            payload["request_selftest"] = True
        elif bad == "path":
            payload["launcher_path"] = str(tmp_path / "outside.ps1")
        else:
            with Path(payload["launcher_path"]).open("a", encoding="utf-8") as file:
                file.write("Write-Output 'unexpected'\n")
        response = http.post(
            "/api/v1/agent/deployment", headers=agent._headers(), json=payload
        )
        assert response.status_code == (403 if bad == "role" else 200)
        if bad != "role":
            assert response.json()["status"] == "REJECTED"


def test_plan_authority_versions_capacity_and_expired_offers(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        config = enable(http)
        sch = scheduler(http)
        for token in (
            "test-admin",
            "test-root",
            issue_role_token("test-root", "discovery"),
            issue_scheduler_token("test-root", "codex"),
        ):
            assert (
                http.get(
                    "/api/v1/agent/scheduler/plan?scheduler_id=mpt-codex&contract_version=2",
                    headers={"Authorization": f"Bearer {token}"},
                ).status_code
                == 401
            )
        assert (
            http.get(
                "/api/v1/agent/scheduler/plan?scheduler_id=mpt-codex&contract_version=1",
                headers=sch._headers(),
            ).status_code
            == 422
        )
        assert (
            http.get(
                "/api/v1/agent/scheduler/plan?scheduler_id=mpt-codex",
                headers=sch._headers(),
            ).status_code
            == 422
        )
        config["task_profiles"][0]["model"] = "unknown"
        assert (
            http.put(
                "/api/v1/agent/scheduler/config", headers=ADMIN, json=config
            ).status_code
            == 422
        )
        deploy(http, stage(runtime))
        for index in range(4):
            sign(http, f"concurrent plan {index}")
        with ThreadPoolExecutor(max_workers=2) as pool:
            plans = list(
                pool.map(
                    lambda _: sch.scheduler_plan("mpt-codex", contract_version=2),
                    range(2),
                )
            )
        assert sum(len(plan["worker_requests"]) for plan in plans) == 2
        requests = [worker for plan in plans for worker in plan["worker_requests"]]
        assert len({worker["request_id"] for worker in requests}) == 2
        titles_by_task = {worker["task_id"]: worker["title"] for worker in requests}
        with Session(runtime.engine) as session:
            for offer in session.exec(select(SchedulerOffer)).all():
                offer.expires_at = utc_now() - timedelta(seconds=1)
                session.add(offer)
            session.commit()
        fresh = sch.scheduler_plan("mpt-codex", contract_version=2)
        assert fresh["status"] == "READY" and len(fresh["worker_requests"]) == 2
        assert not {worker["request_id"] for worker in fresh["worker_requests"]} & {
            worker["request_id"] for worker in requests
        }
        assert {
            worker["task_id"] for worker in fresh["worker_requests"]
        } <= titles_by_task.keys()
        for worker in fresh["worker_requests"]:
            assert worker["title"] != titles_by_task[worker["task_id"]]
            assert worker["title"] == (
                f"mpt_worker_{worker['task_type']}_{worker['task_id']}_"
                f"{worker['request_id'].rsplit(':', 1)[-1]}"
            )


def test_expired_owned_reservation_and_confirmed_lease_recovery(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        agent = agent_client(http)
        task_id = sign(http, "lease maintenance")
        reserved = agent.dispatch(wait=0, task_id=task_id)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(task)
            session.commit()
        again = agent.dispatch(wait=0, task_id=task_id)
        assert again["should_run"] and again["lease_token"] != reserved["lease_token"]
        agent.confirm_claim(task_id, again["lease_token"], "http-worker")
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert task.attempt == 1
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(task)
            session.commit()
        assert agent.dispatch(wait=0, task_id=task_id)["reason"] == "NO_TASK"
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert (
                task.status == TaskStatus.PENDING
                and task.attempt == 1
                and as_utc(task.not_before) > utc_now()
            )
            task.status = TaskStatus.LEASED
            task.attempt = task.max_attempts
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(task)
            session.commit()
        assert runtime.maintain_dispatch() == 1
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert (
                task.status == TaskStatus.FAILED and task.error_code == "LEASE_EXPIRED"
            )


def test_quarantine_blocks_service_mutation_claim_recovery_and_keeps_metadata(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        content_id, researcher, claim = select_and_claim(http, runtime)
        task_id, token = claim["task_id"], claim["lease_token"]
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            content.business_status = ContentStatus.PUBLISHED
            session.add(content)
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="migration-fixture",
                    entity_type="content",
                    entity_id=content_id,
                    action=CONTENT_QUARANTINE,
                    metadata_json={"verified": False, "historical": True},
                )
            )
            standalone = runtime.task_service(session).create_task(
                task_type=TaskType.SOURCE_DISCOVERY,
                instructions="historical",
                input_data={"source_id": "historical"},
            )
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="migration-fixture",
                    entity_type="agent_task",
                    entity_id=standalone.id,
                    action=TASK_QUARANTINE,
                    metadata_json={"verified": False, "historical": True},
                )
            )
            session.commit()
            standalone_id = standalone.id
        detail = http.get(f"/api/v1/contents/{content_id}", headers=ADMIN)
        assert (
            detail.status_code == 200
            and detail.json()["migration_quarantine"][0]["metadata_json"]["verified"]
            is False
        )
        for action in (
            "prepare",
            "approve-script",
            "reject-script",
            "render",
            "approve-final",
            "resume-acceptance",
            "publications/douyin/publish",
        ):
            response = http.post(
                f"/api/v1/contents/{content_id}/{action}",
                headers=ADMIN,
                json={"note": "no"} if action == "reject-script" else None,
            )
            assert response.status_code == 409, (action, response.text)
        for suffix, body in (
            ("complete", {"agent_id": "research-worker", "result": RESEARCH}),
            (
                "fail",
                {
                    "agent_id": "research-worker",
                    "retryable": False,
                    "error_code": "STOP",
                    "message": "no",
                },
            ),
            ("confirm-claim", {"agent_id": "research-worker"}),
            ("publish-intent", {"agent_id": "research-worker"}),
            ("heartbeat", {}),
        ):
            assert (
                http.post(
                    f"/api/v1/agent/tasks/{task_id}/{suffix}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=body,
                ).status_code
                == 409
            )
        assert (
            http.post(
                f"/api/v1/agent/tasks/{task_id}/artifacts",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "image/png",
                },
                content=b"not-read",
            ).status_code
            == 409
        )
        with pytest.raises(TaskConflictError):
            runtime.consume_agent(task_id)
        discovery = agent_client(http)
        assert (
            http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=discovery._headers(),
                json={
                    "agent_id": "http-worker",
                    "role_id": "discovery",
                    "task_id": standalone_id,
                },
            ).status_code
            == 409
        )
        fresh_id = sign(http, "new normal work")
        assert discovery.claim()["task_id"] == fresh_id
        with Session(runtime.engine) as session:
            historical = session.get(AgentTask, standalone_id)
            assert historical.status == TaskStatus.PENDING
            assert (
                session.get(Content, content_id).business_status
                == ContentStatus.PUBLISHED
            )


def test_waiting_upstream_requires_live_mpt_lock_and_stage_evidence(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http)
        content_id, _, claimed = select_and_claim(http, runtime)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claimed["task_id"])
            task.status = TaskStatus.CANCELLED
            content = session.get(Content, content_id)
            content.business_status = ContentStatus.PRODUCING
            session.add(task)
            session.add(content)
            session.add(
                StageRun(
                    content_id=content_id,
                    stage="render",
                    status=StageStatus.RUNNING,
                    input_hash="fixture",
                    started_at=utc_now(),
                )
            )
            session.commit()
        sch = scheduler(http)
        assert (
            sch.scheduler_plan("mpt-codex", contract_version=2)["status"] == "NO_TASK"
        )
        entered, release = Event(), Event()

        def live():
            with content_operation_lock(runtime.settings, content_id):
                entered.set()
                assert release.wait(5)

        with ThreadPoolExecutor(max_workers=1) as pool:
            active = pool.submit(live)
            assert entered.wait(5)
            plan = sch.scheduler_plan("mpt-codex", contract_version=2)
            assert (
                plan["status"] == "WAITING_UPSTREAM" and plan["retry_after_sec"] == 30
            )
            release.set()
            active.result()


def quarantine(session, *, task_id=None, content_id=None):
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="migration-fixture",
            action=TASK_QUARANTINE if task_id else CONTENT_QUARANTINE,
            entity_type="agent_task" if task_id else "content",
            entity_id=task_id or content_id,
            metadata_json={
                "verified": False,
                "historical": True,
                "frozen_payload": {"secret": "never-serialize"},
            },
        )
    )
    session.commit()


def test_quarantine_completed_owned_tasks_are_excluded_on_restart(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        content_id, researcher, claimed = select_and_claim(http, runtime)
        discovery = agent_client(http)
        direction = "historical completed discovery"
        discovery_id = sign(http, direction)
        found = discovery.claim()
        with Session(runtime.engine) as session:
            runtime.task_service(session).complete(
                task_id=claimed["task_id"],
                lease_token=claimed["lease_token"],
                agent_id="research-worker",
                result=RESEARCH,
            )
            runtime.task_service(session).complete(
                task_id=discovery_id,
                lease_token=found["lease_token"],
                agent_id="http-worker",
                result=RESULT,
            )
            quarantine(session, content_id=content_id)
            quarantine(session, task_id=discovery_id)
        detail = http.get(f"/api/v1/contents/{content_id}", headers=ADMIN)
        assert "never-serialize" not in detail.text
        assert http.get(
            f"/api/v1/discovery/tasks/{discovery_id}", headers=ADMIN
        ).json()["migration_quarantine"]
        assert (
            http.post(
                f"/api/v1/discovery/tasks/{discovery_id}/consume", headers=ADMIN
            ).status_code
            == 409
        )
        assert (
            http.post(
                "/api/v1/discovery/search-tasks",
                headers=ADMIN,
                json={"direction": direction},
            ).status_code
            == 409
        )
    restarted = make_runtime(tmp_path / "business")
    with TestClient(app_for(restarted, tmp_path)) as http:
        assert http.get("/api/v1/candidates", headers=ADMIN).status_code == 200
        with Session(restarted.engine) as session:
            assert (
                session.get(AgentTask, claimed["task_id"]).status
                == TaskStatus.COMPLETED
            )
            assert session.get(AgentTask, discovery_id).status == TaskStatus.COMPLETED
            assert (
                session.exec(
                    select(AgentTask).where(
                        AgentTask.task_type == TaskType.SHORT_SCRIPT
                    )
                ).all()
                == []
            )
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action.in_([AGENT_CONSUMED, CONSUMED])
                    )
                ).all()
                == []
            )


def test_dispatch_recovers_owned_accepted_result_before_next_claim(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        _, _, claimed = select_and_claim(http, runtime)
        with Session(runtime.engine) as session:
            runtime.task_service(session).complete(
                task_id=claimed["task_id"],
                lease_token=claimed["lease_token"],
                agent_id="research-worker",
                result=RESEARCH,
            )
        script = client_for(http, "script")
        claimed_script = script.dispatch(wait=0, task_type="short_script")
        assert claimed_script["should_run"]
        with Session(runtime.engine) as session:
            assert (
                session.get(AgentTask, claimed_script["task_id"]).task_type
                == TaskType.SHORT_SCRIPT
            )
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.action == AGENT_CONSUMED,
                            AuditEvent.entity_id == claimed["task_id"],
                        )
                    ).all()
                )
                == 1
            )


def test_reaper_excludes_quarantined_and_unowned_leases(tmp_path):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        discovery = agent_client(http)
        task_id = sign(http, "quarantined expired lease")
        discovery.claim()
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(task)
            quarantine(session, task_id=task_id)
            unowned = runtime.task_service(session).create_task(
                task_type=TaskType.SOURCE_DISCOVERY,
                instructions="imported lease",
                input_data={"source_id": "imported"},
            )
            unowned.status = TaskStatus.LEASED
            unowned.lease_expires_at = utc_now() - timedelta(seconds=1)
            session.add(unowned)
            session.commit()
            unowned_id = unowned.id
        assert runtime.maintain_dispatch() == 0
        assert discovery.dispatch(wait=0, task_id=unowned_id)["reason"] == "NO_TASK"
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, task_id).status == TaskStatus.LEASED
            assert session.get(AgentTask, unowned_id).status == TaskStatus.LEASED
            assert (
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == "lease_expired")
                ).all()
                == []
            )


def test_host_capacity_and_changed_staged_proof_block_new_offers(tmp_path):
    from pathlib import Path

    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http)
        payload = stage(runtime)
        payload["host"]["max_concurrency"] = 1
        deploy(http, payload)
        for index in range(3):
            sign(http, f"host capacity {index}")
        sch = scheduler(http)
        plan = sch.scheduler_plan("mpt-codex", contract_version=2)
        assert len(plan["worker_requests"]) == 1
        assert plan["blocked"][0]["reason"] == "HOST_AT_CAPACITY"
        with Session(runtime.engine) as session:
            for offer in session.exec(select(SchedulerOffer)).all():
                offer.expires_at = utc_now() - timedelta(seconds=1)
                session.add(offer)
            session.commit()
        Path(payload["skill"]["path"]).joinpath("SKILL.md").write_text(
            "changed", encoding="utf-8"
        )
        plan = sch.scheduler_plan("mpt-codex", contract_version=2)
        assert plan["status"] == "BLOCKED" and plan["worker_requests"] == []
        assert {row["reason"] for row in plan["blocked"]} == {"DEPLOYMENT_CHANGED"}


@pytest.mark.parametrize("ownership", ["missing", "wrong-hash"])
def test_explicit_claim_cached_historical_lease_cannot_bypass_ownership(
    tmp_path, ownership
):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine, expire_on_commit=False) as session:
            service = runtime.task_service(session)
            task = service.create_task(
                task_type=TaskType.SOURCE_DISCOVERY,
                instructions="historical cached lease",
                input_data={"source_id": "history"},
            )
            task.status = TaskStatus.LEASED
            task.lease_expires_at = utc_now() - timedelta(seconds=1)
            task.assigned_agent_id = "old-worker"
            session.add(task)
            if ownership == "wrong-hash":
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="fixture",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="mpt_task_owned",
                        metadata_json={"input_hash": "wrong"},
                    )
                )
            session.commit()
            assert session.get(AgentTask, task.id) is task
            assert (
                service.claim(
                    task_id=task.id,
                    agent_id="new-worker",
                    role_id="discovery",
                    capabilities={TaskType.SOURCE_DISCOVERY},
                )
                is None
            )
            session.refresh(task)
            assert (
                task.assigned_agent_id == "old-worker"
                and task.status == TaskStatus.LEASED
            )
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="fixture",
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="mpt_task_owned",
                    metadata_json={"input_hash": task.input_hash},
                )
            )
            session.commit()
            assert (
                service.claim(
                    task_id=task.id,
                    agent_id="new-worker",
                    role_id="discovery",
                    capabilities={TaskType.SOURCE_DISCOVERY},
                )
                is not None
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("active", ["new-offers", "existing-lease"])
def test_host_capacity_is_shared_by_discovery_and_research(tmp_path, active):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http)
        _, _, research_claim = select_and_claim(http, runtime)
        for role in ("discovery", "research"):
            payload = stage(runtime, role)
            payload["host"]["max_concurrency"] = 1
            deploy(http, payload)
        discovery_id = sign(http, "same host other role")
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, research_claim["task_id"])
            if active == "new-offers":
                task.status = TaskStatus.PENDING
                task.lease_expires_at = None
            else:
                task.assigned_agent_id = "mpt-codex-research-123456789abc"
            session.add(task)
            session.commit()
        plan = scheduler(http).scheduler_plan("mpt-codex", contract_version=2)
        if active == "new-offers":
            assert len(plan["worker_requests"]) == 1
            assert (
                len(plan["blocked"]) == 1
                and plan["blocked"][0]["reason"] == "HOST_AT_CAPACITY"
            )
            followup = scheduler(http).scheduler_plan("mpt-codex", contract_version=2)
            assert followup["worker_requests"] == [] and followup["status"] == "BLOCKED"
            assert followup["blocked"][0]["reason"] == "HOST_AT_CAPACITY"
        else:
            assert plan["worker_requests"] == [] and plan["status"] == "BLOCKED"
            assert plan["blocked"][0]["reason"] == "HOST_AT_CAPACITY"
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, discovery_id).status == TaskStatus.PENDING


@pytest.mark.parametrize("marker", ["content", "task"])
def test_quarantined_task_get_hides_frozen_input_and_result_without_deleting(
    tmp_path, marker
):
    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        content_id, _, claimed = select_and_claim(http, runtime)
        task_id = claimed["task_id"]
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            task.input_data = {"secret": "private-input-marker"}
            task.result_data = {"secret": "private-result-marker"}
            task.instructions = "private-instructions-marker"
            session.add(task)
            session.commit()
        normal = http.get(f"/api/v1/discovery/tasks/{task_id}", headers=ADMIN)
        assert (
            "private-input-marker" in normal.text
            and "private-result-marker" in normal.text
        )
        with Session(runtime.engine) as session:
            quarantine(
                session,
                **(
                    {"content_id": content_id}
                    if marker == "content"
                    else {"task_id": task_id}
                ),
            )
        for path in (
            f"/api/v1/contents/{content_id}",
            f"/api/v1/discovery/tasks/{task_id}",
        ):
            response = http.get(path, headers=ADMIN)
            assert response.status_code == 200
            assert all(
                marker not in response.text
                for marker in (
                    "private-input-marker",
                    "private-result-marker",
                    "private-instructions-marker",
                )
            )
            task_meta = (
                response.json()["tasks"][0] if "/contents/" in path else response.json()
            )
            assert task_meta["input_hash"] and task_meta["migration_quarantine"]
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert task.input_data["secret"] == "private-input-marker"
            assert task.result_data["secret"] == "private-result-marker"


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_media_lock_io_failure_is_diagnosed_not_no_task(tmp_path, monkeypatch, error):
    from app.business import scheduler as service

    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http)
        content_id, _, claimed = select_and_claim(http, runtime)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claimed["task_id"])
            task.status = TaskStatus.CANCELLED
            content = session.get(Content, content_id)
            content.business_status = ContentStatus.PRODUCING
            session.add(task)
            session.add(content)
            session.add(
                StageRun(
                    content_id=content_id,
                    stage="render",
                    status=StageStatus.RUNNING,
                    input_hash="fixture",
                    started_at=utc_now(),
                )
            )
            session.commit()

        def failed_lock(*args, **kwargs):
            raise error("private-path-must-not-leak")

        monkeypatch.setattr(service, "content_operation_lock", failed_lock)
        plan = scheduler(http).scheduler_plan("mpt-codex", contract_version=2)
        assert plan["status"] == "BLOCKED" and plan["worker_requests"] == []
        assert plan["blocked"][0]["reason"] == "MEDIA_LOCK_IO_ERROR"
        assert plan["blocked"][0]["error_type"] == error.__name__
        assert "private-path-must-not-leak" not in json.dumps(plan)


@pytest.mark.parametrize(
    "operation", ["dispatch", "heartbeat", "fresh-dispatch", "claim", "whoami"]
)
def test_verified_host_cannot_be_moved_by_runtime_device_label(tmp_path, operation):
    from glance_core.models import AgentWorker

    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        enable(http)
        _, _, claimed = select_and_claim(http, runtime)
        for role in ("discovery", "research"):
            payload = stage(runtime, role)
            payload["host"]["max_concurrency"] = 1
            deploy(http, payload)
        sign(http, "runtime label attack")
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claimed["task_id"])
            task.status = TaskStatus.PENDING
            task.lease_expires_at = None
            session.add(task)
            session.commit()
        researcher = client_for(http, "research")
        identity = "mpt-codex-research" + (
            "-123456789abc" if operation == "fresh-dispatch" else ""
        )
        body = {
            "agent_id": identity,
            "role_id": "research",
            "task_id": claimed["task_id"],
            "runtime": {"device_label": "spoofed-other-host"},
        }
        if operation == "heartbeat":
            legitimate = http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=researcher._headers(),
                json={**body, "runtime": {"device_label": "isolated-tests"}},
            ).json()
            response = http.post(
                f"/api/v1/agent/tasks/{claimed['task_id']}/heartbeat",
                headers={"Authorization": f"Bearer {legitimate['lease_token']}"},
                json={"runtime": {"device_label": "spoofed-other-host"}},
            )
        elif operation in {"claim", "whoami"}:
            response = http.post(
                "/api/v1/agent/"
                + ("tasks/claim?wait=0" if operation == "claim" else "whoami"),
                headers=researcher._headers(),
                json={key: value for key, value in body.items() if key != "task_id"},
            )
        else:
            response = http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=researcher._headers(),
                json=body,
            )
        assert response.status_code == 409
        with Session(runtime.engine) as session:
            worker = session.get(AgentWorker, "mpt-codex-research")
            assert (
                worker.device_label == "isolated-tests"
                and worker.deployment_verified_at is not None
            )
        if operation != "heartbeat":
            legitimate = http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=researcher._headers(),
                json={**body, "runtime": {"device_label": "isolated-tests"}},
            ).json()
            assert legitimate["should_run"]
        plan = scheduler(http).scheduler_plan("mpt-codex", contract_version=2)
        assert plan["status"] == "BLOCKED" and plan["worker_requests"] == []
        assert plan["blocked"][0]["reason"] == "HOST_AT_CAPACITY"
        # Simulate registry state left by an older unsafe _touch_worker. Neither
        # the mutable host label nor self-check capacity moves audited occupancy.
        with Session(runtime.engine) as session:
            for agent_id in ("mpt-codex-research", identity):
                worker = session.get(AgentWorker, agent_id)
                worker.device_label = "spoofed-other-host"
                worker.self_check = {"host_capacity": "100"}
                session.add(worker)
            session.commit()
        plan = scheduler(http).scheduler_plan("mpt-codex", contract_version=2)
        assert plan["status"] == "BLOCKED" and plan["worker_requests"] == []
        researcher.settings.agent_id = identity
        assert researcher.whoami()["known_device_label"] == "isolated-tests"


def test_unpinned_verification_requires_actual_redeployment_before_claim(tmp_path):
    from app.business.quarantine import deployment_proof

    runtime = make_runtime(tmp_path / "business")
    with TestClient(app_for(runtime, tmp_path)) as http:
        payload = stage(runtime)
        agent = deploy(http, payload)
        task_id = sign(http, "unpinned verification")
        with Session(runtime.engine) as session:
            proof = deployment_proof(session, payload["agent_id"])
            proof.metadata_json = {
                key: value
                for key, value in proof.metadata_json.items()
                if key != "device_label"
            }
            session.add(proof)
            session.commit()
        assert agent.whoami()["known_device_label"] == ""
        response = http.post(
            "/api/v1/agent/dispatch?wait=0",
            headers=agent._headers(),
            json={
                "role_id": "discovery",
                "agent_id": payload["agent_id"],
                "task_id": task_id,
            },
        )
        assert (
            response.status_code == 409 and "DEPLOYMENT_HOST_UNPINNED" in response.text
        )
        assert agent.deployment(payload)["status"] == "VERIFIED"
        assert agent.dispatch(wait=0, task_id=task_id)["should_run"]
