from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_collector.service import CollectionService
from glance_core.models import (
    AgentSubmission,
    AgentWorker,
    AuditEvent,
    Candidate,
    CollectionRun,
    AgentTask,
    TaskStatus,
)
from glance_core.security import issue_role_token
from glance_core.settings import Settings
from sqlmodel import Session, select

from app.business.runtime import CONSUMED, OWNED, BusinessRuntime, settings_from_config
from app.controllers.v1.agent import router as agent_router
from app.controllers.v1.discovery import router as discovery_router

ADMIN = {"Authorization": "Bearer test-admin"}
RESULT = {
    "candidates": [
        {
            "external_id": "project-a",
            "url": "https://example.org/project-a",
            "title": "Project A",
        },
        {
            "external_id": "project-b",
            "url": "https://example.org/project-b",
            "title": "Project B",
        },
    ],
    "notes": "HTTP contract fixture",
}


def make_runtime(tmp_path):
    return BusinessRuntime(
        Settings(
            _env_file=None,
            _env_prefix="MPT_BUSINESS_",
            data_dir=tmp_path,
            database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
            admin_token="test-admin",
            bootstrap_agent_token="test-root",
        )
    )


def make_app(runtime):
    @asynccontextmanager
    async def lifespan(app):
        runtime.initialize()
        yield
        runtime.close()

    app = FastAPI(lifespan=lifespan)
    app.state.business_runtime = runtime
    app.include_router(agent_router)
    app.include_router(discovery_router)
    return app


def agent_client(http):
    agent = AgentClient(
        AgentSettings(
            token=issue_role_token("test-root", "discovery"),
            agent_id="http-worker",
            role_id="discovery",
            skill_version="1.1.1",
        )
    )
    agent.client.close()
    agent.client = http
    return agent


def issue_and_claim(http, agent):
    issued = http.post(
        "/api/v1/discovery/search-tasks",
        headers=ADMIN,
        json={"direction": "海外新项目", "max_candidates": 2},
    )
    assert issued.status_code == 200, issued.text
    claimed = agent.claim()
    assert claimed["status"] == "CLAIMED"
    assert claimed["task_id"] == issued.json()["task_id"]
    return claimed


def assert_consumed(runtime, task_id):
    with Session(runtime.engine) as session:
        assert len(session.exec(select(Candidate)).all()) == 2
        runs = session.exec(select(CollectionRun)).all()
        assert len(runs) == 1
        assert (runs[0].status, runs[0].created_count, runs[0].duplicate_count) == (
            "SUCCESS",
            2,
            0,
        )
        markers = session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_id == task_id, AuditEvent.action == CONSUMED
            )
        ).all()
        assert len(markers) == 1


def test_agent_http_discovery_persists_and_restarts(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(make_app(runtime)) as http:
        agent = agent_client(http)
        assert http.get("/api/v1/candidates").status_code == 401
        assert (
            http.get(
                "/api/v1/candidates", headers=[(b"authorization", b"Bearer \xff")]
            ).status_code
            == 401
        )
        assert (
            http.post(
                "/api/v1/agent/tasks/claim",
                headers=[(b"authorization", b"Bearer glance-role.discovery.\xff")],
                json={"agent_id": "invalid-header", "role_id": "discovery"},
            ).status_code
            == 401
        )
        assert agent.whoami()["role_id"] == "discovery"
        claimed = issue_and_claim(http, agent)
        task_id, token = claimed["task_id"], claimed["lease_token"]
        bundle = agent.bundle(claimed["bundle_url"], token)
        assert bundle["task"]["task_type"] == "source_discovery"
        assert bundle["input"]["source_id"]
        assert bundle["result_schema"]["properties"]["candidates"]
        assert agent.heartbeat(task_id, token)["status"] == "LEASED"
        assert agent.claim()["status"] == "AT_CAPACITY"
        assert http.get(claimed["bundle_url"], headers=ADMIN).status_code == 409
        wrong_role = {
            "Authorization": f"Bearer {issue_role_token('test-root', 'research')}"
        }
        assert (
            http.post(
                "/api/v1/agent/tasks/claim",
                headers=wrong_role,
                json={"agent_id": "other", "role_id": "discovery"},
            ).status_code
            == 403
        )
        assert (
            http.post(
                f"/api/v1/agent/tasks/{task_id}/artifacts",
                headers={"Authorization": f"Bearer {token}"},
                content=b"arbitrary",
            ).status_code
            == 409
        )
        invalid = http.post(
            f"/api/v1/agent/tasks/{task_id}/complete",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "agent_id": "http-worker",
                "result": {"candidates": [{"external_id": "bad"}]},
            },
        )
        assert invalid.status_code == 422
        assert agent.heartbeat(task_id, token)["status"] == "LEASED"
        assert (
            agent.similar(
                task_id=task_id,
                lease_token=token,
                agent_id="http-worker",
                query="project",
            )["status"]
            == "NO_SIMILAR"
        )
        assert (
            agent.complete(task_id, token, "http-worker", RESULT)["advanced"]
            == "CONSUMED"
        )
        consumed = []
        original_consume = runtime.consume_agent

        def counted_consume(target_id):
            consumed.append(target_id)
            return original_consume(target_id)

        monkeypatch.setattr(runtime, "consume_agent", counted_consume)
        assert (
            http.post(
                f"/api/v1/agent/tasks/{task_id}/complete",
                headers={"Authorization": "Bearer wrong-completed-lease"},
                json={"agent_id": "http-worker", "result": RESULT},
            ).status_code
            == 409
        )
        assert consumed == []
        assert (
            agent.complete(task_id, token, "http-worker", RESULT)["advanced"]
            == "CONSUMED"
        )
        assert consumed == [task_id]
        assert len(http.get("/api/v1/candidates", headers=ADMIN).json()) == 2
        with Session(runtime.engine) as session:
            accepted = session.exec(
                select(AgentSubmission).where(
                    AgentSubmission.validation_status == "ACCEPTED"
                )
            ).all()
            assert len(accepted) == 1
            assert Path(accepted[0].raw_result_path).is_relative_to(tmp_path)
            assert Path(accepted[0].raw_result_path).is_file()
            assert session.get(AgentWorker, "http-worker").tasks_completed == 1
        assert_consumed(runtime, task_id)
    restarted = make_runtime(tmp_path)
    with TestClient(make_app(restarted)) as http:
        assert len(http.get("/api/v1/candidates", headers=ADMIN).json()) == 2
        assert_consumed(restarted, task_id)


def test_partial_ingestion_rolls_back_then_restart_consumes(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    original = CollectionService._ingest_item

    def fail_after_real_candidate_commit(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("Injected crash after first candidate commit")

    with TestClient(make_app(runtime)) as http:
        agent = agent_client(http)
        claimed = issue_and_claim(http, agent)
        monkeypatch.setattr(
            CollectionService, "_ingest_item", fail_after_real_candidate_commit
        )
        assert (
            agent.complete(
                claimed["task_id"], claimed["lease_token"], "http-worker", RESULT
            )["advanced"]
            == "PENDING"
        )
        with Session(runtime.engine) as session:
            assert session.exec(select(Candidate)).all() == []
            assert session.exec(
                select(AgentSubmission).where(
                    AgentSubmission.validation_status == "ACCEPTED"
                )
            ).first()
            assert (
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == CONSUMED)
                ).all()
                == []
            )
    monkeypatch.setattr(CollectionService, "_ingest_item", original)
    restarted = make_runtime(tmp_path)
    with TestClient(make_app(restarted)):
        assert_consumed(restarted, claimed["task_id"])


def test_two_runtimes_serialize_consumption(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(make_app(runtime)) as http:
        agent = agent_client(http)
        claimed = issue_and_claim(http, agent)

        def defer(_task_id):
            raise RuntimeError("Defer consumption for concurrent workers")

        monkeypatch.setattr(runtime, "consume_discovery", defer)
        assert (
            agent.complete(
                claimed["task_id"], claimed["lease_token"], "http-worker", RESULT
            )["advanced"]
            == "PENDING"
        )
        first, second = make_runtime(tmp_path), make_runtime(tmp_path)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(
                    pool.map(
                        lambda consumer: consumer.consume_discovery(claimed["task_id"]),
                        [first, second],
                    )
                )
            assert sorted(results) == [False, True]
            assert_consumed(first, claimed["task_id"])
        finally:
            first.close()
            second.close()


def test_configuration_never_inherits_live_glance_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("GLANCE_DATABASE_URL", "sqlite:///live-production.db")
    monkeypatch.setenv("GLANCE_DATA_DIR", "live-production")
    monkeypatch.setenv("GLANCE_BOOTSTRAP_AGENT_TOKEN", "live-token")
    monkeypatch.setenv("MPT_BUSINESS_PUBLIC_BASE_URL", "http://127.0.0.1:8010")
    settings = settings_from_config(
        {
            "business": {
                "bootstrap_agent_token": "independent",
                "admin_token": "independent-admin",
            }
        },
        tmp_path,
    )
    assert settings.data_dir == tmp_path / "storage/business"
    assert "live-production" not in settings.database_url
    assert settings.bootstrap_agent_token.get_secret_value() == "independent"
    assert settings.public_base_url == "http://127.0.0.1:8010"
    with pytest.raises(ValueError, match="inside business data_dir"):
        BusinessRuntime(
            settings.model_copy(update={"database_url": "sqlite:///outside.db"})
        )


def test_real_dispatch_reserves_exact_task_then_confirms_without_double_claim(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(make_app(runtime)) as http:
        agent = agent_client(http)
        ids = []
        for direction in ("dispatch topic one", "dispatch topic two"):
            response = http.post(
                "/api/v1/discovery/search-tasks",
                headers=ADMIN,
                json={"direction": direction, "max_candidates": 2},
            )
            assert response.status_code == 200, response.text
            ids.append(response.json()["task_id"])
        assert (
            http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=agent._headers(),
                json={"agent_id": "http-worker", "role_id": "publish"},
            ).status_code
            == 403
        )
        assert (
            http.post(
                "/api/v1/agent/dispatch?wait=0",
                headers=agent._headers(),
                json={
                    "agent_id": "http-worker",
                    "role_id": "discovery",
                    "task_type": "agent_selftest",
                },
            ).status_code
            == 422
        )
        assert (
            agent.dispatch(wait=0, task_id="agt_not_a_signed_task")["reason"]
            == "NO_TASK"
        )
        reserved = agent.dispatch(
            wait=0,
            task_id=ids[1],
            task_type="source_discovery",
            launcher="mpt-discovery",
        )
        assert reserved["should_run"] is True
        assert reserved["task_id"] == ids[1]
        assert f"confirm-claim {ids[1]}" in reserved["prompt"]
        assert "glance-discovery-agent" in reserved["prompt"]
        assert "mpt-discovery" in reserved["prompt"]
        assert reserved["workspace_dir"].endswith(ids[1])
        capacity = agent.dispatch(wait=0, task_id=ids[0])
        assert capacity["should_run"] is False
        assert capacity["reason"] == "AT_CAPACITY"
        assert capacity["active_task_ids"] == [ids[1]]
        confirmed = agent.confirm_claim(ids[1], reserved["lease_token"], "http-worker")
        assert confirmed["status"] == "CONFIRMED"
        assert confirmed["lease_expires_at"] > reserved["lease_expires_at"]
        assert (
            agent.bundle(reserved["bundle_url"], reserved["lease_token"])["task"][
                "task_id"
            ]
            == ids[1]
        )
        assert (
            agent.complete(ids[1], reserved["lease_token"], "http-worker", RESULT)[
                "advanced"
            ]
            == "CONSUMED"
        )
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, ids[0]).status == TaskStatus.PENDING
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == ids[1],
                            AuditEvent.action == OWNED,
                        )
                    ).all()
                )
                == 1
            )
