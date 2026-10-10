"""Exercise real staged PowerShell launchers without contacting a live server."""

import json
import os
import subprocess
import sys
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_core.models import AgentTask, AgentWorker, AuditEvent, TaskType
from glance_core.security import verify_role_token, verify_scheduler_token
from pydantic import SecretStr
from sqlmodel import Session, select

from app.controllers.v1.deployment import router
from scripts import mpt_agent_launchers
from scripts.mpt_agent_launchers import (
    DEFAULT_API_URL, WORKSPACE, controlled_stage, install, launcher_environment, main, run,
    validated_api_url,
)
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


@pytest.fixture(scope="module")
def powershell_staged8000():
    root = WORKSPACE / "storage/agent-launcher-staging" / f"test-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    credentials = root / "runtime.json"
    credentials.write_text(json.dumps({"bootstrap_agent_token": "isolated-test-root"}), encoding="utf-8")
    stage = root / "launchers"
    result = subprocess.run(
        ["C:/Program Files/PowerShell/7/pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
         str(WORKSPACE / "scripts/install-mpt-agent-launchers.ps1"),
         "-StagingDirectory", str(stage), "-RuntimeConfig", str(credentials),
         "-ApiUrl", "http://127.0.0.1:8000"],
        capture_output=True, text=True, timeout=180, check=False,
    )
    assert result.returncode == 0, result.stderr
    return stage, json.loads(result.stdout)


@pytest.fixture(scope="module")
def staged8000():
    root = WORKSPACE / "storage/agent-launcher-staging" / f"test-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    credentials = root / "runtime.json"
    credentials.write_text(json.dumps({"bootstrap_agent_token": "isolated-test-root"}), encoding="utf-8")
    stage = root / "launchers"
    # Deployment validates the launcher's exact server interpreter, not just its CLI output.
    return stage, install(stage, credentials, "mpt-codex", [], "http://127.0.0.1:8000")


def test_powershell_forwards_install_binding_to_all_launchers(powershell_staged8000, monkeypatch):
    stage, result = powershell_staged8000
    assert result["api_url"] == "http://127.0.0.1:8000"
    assert result["executable_selftest"] == "PASS"
    for path in stage.glob("*.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        assert manifest["api_url"] == "http://127.0.0.1:8000"
        assert launcher_environment(manifest)["GLANCE_AGENT_API_URL"] == manifest["api_url"]
    manifest_path = stage / "mpt-codex-research.json"
    calls = []
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(mpt_agent_launchers.cli, "app", lambda **kwargs: calls.append(dict(os.environ)))
    run(manifest_path, ["whoami"])
    assert calls[0]["GLANCE_AGENT_API_URL"] == "http://127.0.0.1:8000"


@pytest.fixture
def local_http_and_proxy(monkeypatch):
    received = {"direct": [], "proxy": []}
    servers = []
    threads = []

    for name in received:
        def handler_for(endpoint):
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    received[endpoint].append((self.path, self.headers.get("Authorization")))
                    response = json.dumps({
                        "role_id": body.get("role_id"), "status": "VERIFIED", "findings": [],
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response)))
                    self.end_headers()
                    self.wfile.write(response)

                def log_message(self, *args):
                    pass

            return Handler

        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(name))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        threads.append(thread)
    direct_url, proxy_url = [f"http://127.0.0.1:{server.server_port}" for server in servers]
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(key, proxy_url)
    for key in ("NO_PROXY", "no_proxy"):
        monkeypatch.setenv(key, "")
    try:
        yield direct_url, received
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_agent_client_trust_env_opt_out_preserves_legacy_proxy_contract(local_http_and_proxy):
    direct_url, received = local_http_and_proxy
    settings = AgentSettings(_env_file=None, api_url=direct_url, token="synthetic-proxy-test",
                             agent_id="test-proxy", role_id="research")
    for trust_env in (True, False):
        client = AgentClient(settings) if trust_env else AgentClient(settings, trust_env=False)
        try:
            assert client.whoami()["role_id"] == "research"
        finally:
            client.client.close()
    assert len(received["proxy"]) == len(received["direct"]) == 1
    assert received["proxy"][0][0] == f"{direct_url}/api/v1/agent/whoami"


@pytest.mark.parametrize("action", ["run", "register"])
def test_mpt_requests_bypass_inherited_proxy(staged, local_http_and_proxy, action, capsys):
    stage, _ = staged
    direct_url, received = local_http_and_proxy
    manifest_path = stage / "mpt-codex-research.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original)
    manifest["api_url"] = direct_url
    original_environment = dict(os.environ)
    try:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        if action == "run":
            result = subprocess.run(
                [sys.executable, str(WORKSPACE / "scripts/mpt_agent_launchers.py"), "run",
                 "--manifest", str(manifest_path), "selftest"],
                capture_output=True, text=True, timeout=30, check=False,
            )
            assert result.returncode == 0, result.stderr
        else:
            assert main([
                "register", "--staging-directory", str(stage), "--role", "research",
                "--max-concurrency", "2", "--supported-model", "gpt-6.1-sol",
                "--model-name", "gpt-6.1-sol", "--reasoning-effort", "high",
            ]) == 0
            assert json.loads(capsys.readouterr().out)["status"] == "REGISTERED"
        assert received["proxy"] == []
        assert [path for path, _ in received["direct"]] == (
            ["/api/v1/agent/whoami"] if action == "run"
            else ["/api/v1/agent/whoami", "/api/v1/agent/deployment"]
        )
        assert all(verify_role_token("isolated-test-root", auth.removeprefix("Bearer "), {"research"})
                   == "research" for _, auth in received["direct"])
        assert dict(os.environ) == original_environment
    finally:
        manifest_path.write_text(original, encoding="utf-8")


def test_launcher_environment_prevents_windows_registry_proxy_fallback(staged, local_http_and_proxy, monkeypatch):
    stage, _ = staged
    direct_url, received = local_http_and_proxy
    manifest = json.loads((stage / "mpt-codex-research.json").read_text(encoding="utf-8"))
    manifest["api_url"] = direct_url
    environment = launcher_environment(manifest)

    def forbidden():
        raise AssertionError("MPT must never consult Windows registry proxy settings")

    monkeypatch.setattr(urllib.request, "getproxies_registry", forbidden)
    monkeypatch.setattr(os, "environ", environment)
    client = AgentClient(AgentSettings())
    try:
        assert client.whoami()["role_id"] == "research"
    finally:
        client.client.close()
    assert len(received["direct"]) == 1
    assert received["proxy"] == []


def test_legacy_manifest_without_binding_keeps_8010(staged):
    stage, result = staged
    assert result["api_url"] == DEFAULT_API_URL
    manifest = json.loads((stage / "mpt-codex-research.json").read_text(encoding="utf-8"))
    assert manifest.pop("api_url") == DEFAULT_API_URL
    assert launcher_environment(manifest)["GLANCE_AGENT_API_URL"] == DEFAULT_API_URL


@pytest.mark.parametrize("port", [1, 8000, 8010, 65535])
def test_loopback_binding_accepts_valid_ports(port):
    assert validated_api_url(f"http://127.0.0.1:{port}") == f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("api_url", [
    None, 8000, "", "http://127.0.0.1", "http://127.0.0.1:0", "http://127.0.0.1:65536",
    "http://127.0.0.1:08000", "http://127.0.0.1:+8000", "http://127.0.0.1:8000/",
    "http://127.0.0.1:8000/api", "http://127.0.0.1:8000?x=1", "http://127.0.0.1:8000#x",
    "http://user@127.0.0.1:8000", "https://127.0.0.1:8000", "HTTP://127.0.0.1:8000",
    "http://localhost:8000", "http://127.1:8000", "http://2130706433:8000",
    "http://127.0.0.2:8000", "http://[::1]:8000", "http://10.10.10.1:8000",
    "http://example.com:8000", "http://127.0.0.1:8000\n", " http://127.0.0.1:8000",
    "http://127.0.0.1:８０００",
])
def test_invalid_binding_rejected_before_credentials_or_stage_creation(tmp_path, monkeypatch, api_url):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid binding must fail before credentials are read")

    monkeypatch.setattr(mpt_agent_launchers, "read_root_token", forbidden)
    with pytest.raises(ValueError, match="API URL"):
        install(tmp_path / "not-created", tmp_path / "missing-credentials.json", "mpt-codex", [], api_url)
    with pytest.raises(ValueError, match="API URL"):
        launcher_environment({"api_url": api_url})
    assert not (tmp_path / "not-created").exists()


def test_tampered_manifest_prevents_run_and_whole_batch_registration(staged, monkeypatch, capsys):
    stage, _ = staged
    manifest_path = stage / "mpt-codex-research.json"
    original = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original)
    manifest["api_url"] = "http://remote.example:8000"

    def forbidden(*args, **kwargs):
        raise AssertionError("Tampered binding must fail before credentials, local execution or HTTP")

    monkeypatch.setattr(mpt_agent_launchers, "read_root_token", forbidden)
    monkeypatch.setattr(mpt_agent_launchers, "check_launcher", forbidden)
    monkeypatch.setattr(mpt_agent_launchers.cli, "app", forbidden)
    try:
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(ValueError, match="API URL"):
            run(manifest_path, ["whoami"])
        assert main([
            "register", "--staging-directory", str(stage), "--role", "discovery", "--role", "research",
            "--max-concurrency", "2", "--supported-model", "gpt-6.1-sol",
            "--model-name", "gpt-6.1-sol", "--reasoning-effort", "high",
        ], client_factory=forbidden) == 20
        assert "HANDOFF_ERROR" in capsys.readouterr().err
    finally:
        manifest_path.write_text(original, encoding="utf-8")


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
    for proxy_key in ("HTTP_PROXY", "https_proxy", "All_Proxy", "no_proxy"):
        monkeypatch.setenv(proxy_key, "http://proxy.invalid:8888")
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
        assert not {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}.intersection(
            key.upper() for key in environment
        )
        assert environment["NO_PROXY"] == "*"
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


@pytest.mark.parametrize("fixture_name", ["staged", "staged8000"])
def test_registration_cli_submits_host_contract_to_real_asgi(request, fixture_name, tmp_path, capsys):
    stage, staged_report = request.getfixturevalue(fixture_name)
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
            assert config.api_url == staged_report["api_url"]
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
