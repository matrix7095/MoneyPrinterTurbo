"""Exercise real staged PowerShell launchers without contacting a live server."""

import json
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_core.models import AgentTask, AgentWorker, AuditEvent, TaskType
from glance_core.security import verify_role_token, verify_scheduler_token
from pydantic import SecretStr
from sqlmodel import Session, select

from app.controllers.v1.deployment import router
from scripts.mpt_agent_launchers import WORKSPACE, controlled_stage, install, launcher_environment, main
from test.business.test_discovery_http import make_app, make_runtime


@pytest.fixture(scope="module")
def staged():
    root = WORKSPACE / "storage/agent-launcher-staging" / f"test-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    credentials = root / "runtime.json"
    credentials.write_text(json.dumps({"bootstrap_agent_token": "isolated-test-root"}), encoding="utf-8")
    stage = root / "launchers"
    result = install(stage, credentials, "mpt-codex", [])
    return stage, result


def test_actual_powershell_launchers_are_staged_without_deployment_claim(staged):
    stage, result = staged
    assert result["status"] == "STAGED"
    assert result["executable_selftest"] == "PASS"
    assert result["launcher_count"] == 9
    assert result["deployment_verified"] is False
    assert result["publish_capabilities"] == []
    assert len(list(stage.glob("*.ps1"))) == 9
    for file in stage.glob("*"):
        if file.is_file():
            assert "isolated-test-root" not in file.read_text(encoding="utf-8-sig")
    with pytest.raises(ValueError, match="already exists"):
        install(stage, stage.parent / "runtime.json", "mpt-codex", [])


def test_launchers_derive_scoped_credentials_and_drop_source_environment(staged, monkeypatch):
    stage, _ = staged
    monkeypatch.setenv("GLANCE_AGENT_TOKEN", "source-token-must-not-be-used")
    monkeypatch.setenv("GLANCE_AGENT_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("GLANCE_AGENT_TIMEOUT_SECONDS", "99999")
    monkeypatch.setenv("MPT_UPSTREAM_COMPATIBILITY", "1")
    for role in ("research", "scheduler", "publish"):
        identity = "mpt-codex" if role == "scheduler" else f"mpt-codex-{role}"
        manifest = json.loads((stage / f"{identity}.json").read_text(encoding="utf-8"))
        environment = launcher_environment(manifest)
        token = environment["GLANCE_AGENT_TOKEN"]
        if role == "scheduler":
            assert verify_scheduler_token("isolated-test-root", token, "mpt-codex")
            assert not verify_scheduler_token("isolated-test-root", token, "other")
        else:
            assert verify_role_token("isolated-test-root", token, {role}) == role
            assert verify_role_token("isolated-test-root", token, {"image"}) is None
        assert environment["GLANCE_AGENT_API_URL"] == "http://127.0.0.1:8010"
        assert "GLANCE_AGENT_TIMEOUT_SECONDS" not in environment
        assert "MPT_UPSTREAM_COMPATIBILITY" not in environment
        assert json.loads(environment["GLANCE_AGENT_CAPABILITIES"]) == []


def test_staging_never_targets_global_skills_or_redirected_directories(tmp_path):
    for path in (WORKSPACE, WORKSPACE / "storage/agent-launcher-staging", tmp_path):
        with pytest.raises(ValueError, match="fresh directory"):
            controlled_stage(path)


def test_invalid_runtime_file_fails_without_echoing_secrets(staged):
    stage, _ = staged
    invalid = stage.parent / "invalid.json"
    invalid.write_text('{"bootstrap_agent_token": "secret-unclosed', encoding="utf-8")
    result = subprocess.run(
        [str(Path(__import__("sys").executable)), str(WORKSPACE / "scripts/mpt_agent_launchers.py"),
         "install", "--staging-directory", str(stage.parent / "invalid-stage"),
         "--runtime-config", str(invalid)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 20
    assert "HANDOFF_ERROR" in result.stderr
    assert "secret-unclosed" not in result.stderr + result.stdout


def test_registration_cli_submits_host_contract_to_real_asgi(staged, tmp_path, capsys):
    stage, _ = staged
    runtime = make_runtime(tmp_path)
    runtime.workspace_root = WORKSPACE
    runtime.settings.bootstrap_agent_token = SecretStr("isolated-test-root")
    app = make_app(runtime)
    app.include_router(router)
    received = []

    @app.middleware("http")
    async def capture(request, call_next):
        if request.url.path == "/api/v1/agent/deployment":
            received.append(await request.json())
        assert "scheduler/plan" not in request.url.path
        return await call_next(request)

    with TestClient(app) as http:
        conflicting_host = False

        def client_factory(config):
            client = AgentClient(config)
            client.client.close()
            # Real ASGI transport; independent HTTP client lifetime for each role.
            client.client = httpx.Client(base_url=config.api_url, transport=http._transport)
            if conflicting_host:
                deployment = client.deployment

                def wrong_host(payload):
                    return deployment({**payload, "host": {**payload["host"], "device_label": "another-host"}})

                client.deployment = wrong_host
            return client

        code = main([
            "register", "--staging-directory", str(stage), "--role", "discovery",
            "--role", "research", "--max-concurrency", "2",
            "--supported-model", "gpt-6.1-sol", "--supported-model", "gpt-6-luna",
            "--model-name", "gpt-6.1-sol", "--reasoning-effort", "high",
        ], client_factory=client_factory)
        report = json.loads(capsys.readouterr().out)
        assert code == 0, report
        assert report["status"] == "REGISTERED"
        assert report["business_selftest"] == "NOT_REQUESTED"
        assert len(received) == 2
        for payload in received:
            assert payload["host"]["max_concurrency"] == 2
            assert payload["host"]["supported_models"] == ["gpt-6.1-sol", "gpt-6-luna"]
            assert payload["request_selftest"] is False
            assert payload["model"] == {"name": "gpt-6.1-sol", "reasoning_effort": "high"}
        with Session(runtime.engine) as session:
            worker = session.get(AgentWorker, "mpt-codex-research")
            assert worker.deployment_verified_at is not None
            assert worker.self_check["host_capacity"] == "2"
            proofs = session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_deployment_verified")).all()
            assert len(proofs) == 2
            assert all(event.metadata_json["supported_models"] == ["gpt-6.1-sol", "gpt-6-luna"] for event in proofs)
            assert not session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.AGENT_SELFTEST)).all()
            worker.device_label = "another-host"
            session.add(worker)
            session.commit()
        args = [
            "register", "--staging-directory", str(stage), "--role", "research",
            "--max-concurrency", "2", "--supported-model", "gpt-6.1-sol",
            "--model-name", "gpt-6.1-sol", "--reasoning-effort", "high",
        ]
        # Mutable registry corruption must not replace the verified host identity.
        assert main(args, client_factory=client_factory) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "REGISTERED"
        with Session(runtime.engine) as session:
            assert session.get(AgentWorker, "mpt-codex-research").device_label != "another-host"
        # A real HTTP 200 carrying REJECTED must still make the CLI fail.
        conflicting_host = True
        code = main(args, client_factory=client_factory)
        rejected = json.loads(capsys.readouterr().out)
        assert code == 20
        assert rejected["status"] == "REGISTRATION_INCOMPLETE"
        assert rejected["roles"][0]["status"] == "REJECTED"


@pytest.mark.parametrize("capacity,models,model,role", [
    (0, ["gpt-6.1-sol"], "gpt-6.1-sol", "discovery"),
    (2, ["unsupported-model"], "unsupported-model", "discovery"),
    (2, ["gpt-6-luna"], "gpt-6.1-sol", "discovery"),
    (2, ["gpt-6.1-sol"], "gpt-6.1-sol", "publish"),
])
def test_registration_rejects_missing_host_or_publish_proof_before_http(staged, capsys, capacity, models, model, role):
    stage, _ = staged

    def forbidden(config):
        raise AssertionError("Invalid registration must not contact a server")

    args = ["register", "--staging-directory", str(stage), "--role", role,
            "--max-concurrency", str(capacity), "--model-name", model, "--reasoning-effort", "high"]
    for supported in models:
        args.extend(["--supported-model", supported])
    assert main(args, client_factory=forbidden) == 20
    assert "HANDOFF_ERROR" in capsys.readouterr().err
