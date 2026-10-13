"""Real HTTP/SQLite launch ownership with bounded process fault injection."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.business import conductor_launch as launch
from app.business.content_run import load_run
from app.controllers.v1.content_execution import router
from test.business.test_content_http import content_app
from test.business.test_content_run_http import attach_fixture_host, selected
from test.business.test_discovery_http import ADMIN, make_runtime


class Process:
    pid = 501

    def __init__(self):
        self.done = Event()
        self.code = 20

    def wait(self):
        assert self.done.wait(10), "Fixture process never released"
        return self.code


@pytest.fixture
def execution(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    app = content_app(runtime)
    app.include_router(router)
    processes = []
    binary = tmp_path / "fixture.exe"
    binary.write_bytes(b"fixture")
    monkeypatch.setattr(launch, "launch_command", lambda *args: [str(binary), "fixed-command"])
    monkeypatch.setattr(launch, "process_creation_time", lambda process: "2026-10-13T00:00:00+00:00")

    def popen(command, **options):
        assert command == [str(binary), "fixed-command"]
        assert options["shell"] is False
        assert options["stdout"] == options["stderr"] == launch.subprocess.DEVNULL
        assert not any(key.startswith(("MPT_", "GLANCE_")) for key in options["env"])
        process = Process()
        processes.append(process)
        return process

    monkeypatch.setattr(launch.subprocess, "Popen", popen)
    with TestClient(app) as http:
        content_id, _ = selected(http, runtime, "execute-fixture")
        yield http, runtime, content_id, processes
        for process in processes:
            process.done.set()
        deadline = time.monotonic() + 5
        while launch._handles and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not launch._handles


def path(content_id, endpoint="run/execute"):
    return f"/api/v1/contents/{content_id}/{endpoint}"


def test_auth_and_fixed_launch_inputs(execution):
    http, _, content_id, processes = execution
    assert http.post(path(content_id)).status_code == 401
    response = http.post(path(content_id), headers=ADMIN, json={"command": "secret", "token": "private"})
    assert response.status_code == 422
    assert "secret" not in response.text and "private" not in response.text
    assert processes == []
    assert http.get(path(content_id, "execution"), headers=ADMIN).json()["state"] == "NOT_STARTED"


@pytest.mark.skipif(launch.os.name != "nt", reason="Windows owned process handle")
def test_windows_creation_identity_reads_the_owned_handle():
    from datetime import datetime, timezone, timedelta

    process = launch.subprocess.Popen(
        [launch.sys.executable, "-c", "pass"], stdin=launch.subprocess.DEVNULL,
        stdout=launch.subprocess.DEVNULL, stderr=launch.subprocess.DEVNULL,
        creationflags=launch.subprocess.CREATE_NO_WINDOW, shell=False,
    )
    try:
        created = datetime.fromisoformat(launch.process_creation_time(process))
        # Windows creation and wall clocks can differ by one clock tick.
        assert abs(datetime.now(timezone.utc) - created) < timedelta(seconds=10)
    finally:
        assert process.wait(timeout=10) == 0


def test_concurrent_start_is_one_launch_and_not_ready(execution):
    http, runtime, content_id, processes = execution
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: http.post(path(content_id), headers=ADMIN), range(4)))
    assert all(result.status_code == 200 for result in results)
    bodies = [result.json() for result in results]
    assert len({item["launch_id"] for item in bodies}) == len(processes) == 1
    assert all(item["state"] == "STARTING" and item["host_ready"] is False for item in bodies)
    with Session(runtime.engine) as session:
        record = launch._load(session, content_id)
        assert record.pid == 501 and record.process_started_at
        assert record.binary_sha256 and record.run_id == load_run(session).run_id
    assert "test-admin" not in json.dumps(bodies)


def own_host(http, runtime, content_id):
    run = http.get(path(content_id, "run"), headers=ADMIN).json()
    attach_fixture_host(http, run)
    run = http.get(path(content_id, "run"), headers=ADMIN).json()
    journal = runtime.settings.data_dir / "private" / "content-runs" / run["run_id"] / "state.json"
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({key: run[key] for key in ("run_id", "generation", "host_epoch", "parent_thread_id", "process_identity")}), encoding="utf-8")
    return run


def test_ready_requires_matching_owned_host_and_stop_is_cooperative(execution):
    http, runtime, content_id, processes = execution
    http.post(path(content_id), headers=ADMIN)
    own_host(http, runtime, content_id)
    view = http.get(path(content_id, "execution"), headers=ADMIN).json()
    assert view["state"] == "RUNNING" and view["host_ready"] and view["can_stop"]
    response = http.post(path(content_id, "execution/stop"), headers=ADMIN)
    assert response.status_code == 200 and response.json()["stop_requested"]
    assert not processes[0].done.is_set()
    processes[0].code = 0
    processes[0].done.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = http.get(path(content_id, "execution"), headers=ADMIN).json()
        if result["state"] == "FINISHED":
            break
        time.sleep(0.01)
    assert result["state"] == "FINISHED" and result["exit_code"] == 0
    assert not result["host_ready"]
    restarted = http.post(path(content_id), headers=ADMIN).json()
    assert len(processes) == 2 and restarted["run_id"] != result["run_id"]
    from glance_core.models import RuntimeSetting
    with Session(runtime.engine) as session:
        previous = session.get(RuntimeSetting, launch.PREFIX + result["run_id"])
        assert json.loads(previous.value)["state"] == "FINISHED"


def test_foreign_host_cannot_be_stopped_or_restarted(execution):
    http, _, content_id, processes = execution
    http.post(path(content_id), headers=ADMIN)
    attach_fixture_host(http, http.get(path(content_id, "run"), headers=ADMIN).json())
    result = http.get(path(content_id, "execution"), headers=ADMIN).json()
    assert result["state"] == "UNKNOWN" and not result["host_ready"] and not result["can_stop"]
    assert http.post(path(content_id, "execution/stop"), headers=ADMIN).status_code == 409
    http.post(path(content_id), headers=ADMIN)
    assert len(processes) == 1


def test_existing_bound_run_is_never_spawned(execution):
    http, _, content_id, processes = execution
    run = http.post(path(content_id, "run"), headers=ADMIN).json()
    attach_fixture_host(http, run)
    assert http.post(path(content_id), headers=ADMIN).status_code == 409
    assert processes == []


def test_popen_failure_is_persisted_without_secrets(execution, monkeypatch):
    http, _, content_id, _ = execution

    def fail(*args, **kwargs):
        raise OSError("test-admin PRIVATE TOKEN")

    monkeypatch.setattr(launch.subprocess, "Popen", fail)
    result = http.post(path(content_id), headers=ADMIN)
    assert result.status_code == 200
    assert result.json()["state"] == "FAILED"
    assert result.json()["run_state"] == "stopped"
    assert "test-admin" not in result.text and "PRIVATE" not in result.text
    assert http.get(path(content_id, "execution"), headers=ADMIN).json()["state"] == "FAILED"


def test_lost_process_observation_is_not_retried(execution):
    http, runtime, content_id, processes = execution
    result = http.post(path(content_id), headers=ADMIN).json()
    process = launch._handles.pop(result["launch_id"])
    try:
        response = http.post(path(content_id), headers=ADMIN).json()
        assert response["state"] == "UNKNOWN" and len(processes) == 1
        with Session(runtime.engine) as session:
            assert launch._load(session, content_id).error_code == "CONTENT_EXECUTION_OBSERVATION_LOST"
    finally:
        launch._handles[result["launch_id"]] = process


def test_process_creation_identity_failure_remains_unknown(execution, monkeypatch):
    http, _, content_id, processes = execution

    def unavailable(process):
        raise OSError("PRIVATE creation error")

    monkeypatch.setattr(launch, "process_creation_time", unavailable)
    response = http.post(path(content_id), headers=ADMIN)
    assert response.json()["state"] == "UNKNOWN"
    assert response.json()["error_code"] == "CONTENT_EXECUTION_PROCESS_IDENTITY_LOST"
    assert "PRIVATE" not in response.text
    http.post(path(content_id), headers=ADMIN)
    assert len(processes) == 1


def test_process_exit_failure_is_queryable(execution):
    http, _, content_id, processes = execution
    http.post(path(content_id), headers=ADMIN)
    processes[0].done.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = http.get(path(content_id, "execution"), headers=ADMIN).json()
        if result["state"] == "FAILED":
            break
        time.sleep(0.01)
    assert result["state"] == "FAILED" and result["exit_code"] == 20 and result["exited_at"]
    assert not result["host_ready"] and result["run_state"] == "stopped"


def test_exit_record_survives_new_content_permit_without_stopping_it(execution):
    http, runtime, content_id, processes = execution
    http.post(path(content_id), headers=ADMIN)
    http.post(path(content_id, "execution/stop"), headers=ADMIN)
    other, _ = selected(http, runtime, "other-content")
    new_run = http.post(path(other, "run"), headers=ADMIN).json()
    processes[0].done.set()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = http.get(path(content_id, "execution"), headers=ADMIN)
        assert response.status_code == 200
        result = response.json()
        if result["state"] == "FAILED":
            break
        time.sleep(0.01)
    assert result["exit_code"] == 20 and result["run_state"] is None
    unchanged = http.get(path(other, "run"), headers=ADMIN).json()
    assert unchanged["run_id"] == new_run["run_id"] and not unchanged["stop_requested"]


def test_fixed_configuration_checks_workspace_tokens_and_wrapper(tmp_path, monkeypatch):
    from pathlib import Path
    from glance_core.models import AgentWorker, AuditEvent, utc_now
    from glance_core.scheduler_profiles import save_scheduler_config, default_scheduler_config

    data = tmp_path / "storage" / "business-test"
    data.mkdir(parents=True)
    runtime = make_runtime(data)
    runtime.workspace_root = tmp_path
    runtime.initialize()
    python = tmp_path / ".venv" / "Scripts" / "python.exe"
    python.parent.mkdir(parents=True)
    python.write_bytes(b"fixture")
    monkeypatch.setattr(launch.sys, "executable", str(python))
    private = data / "private" / "runtime.json"
    private.parent.mkdir()
    private.write_text(json.dumps({"admin_token": "test-admin"}), encoding="utf-8")
    stage = tmp_path / "storage" / "agent-launcher-staging" / "fixed"
    stage.mkdir(parents=True)
    launcher = stage / "mpt-codex.ps1"
    manifest = launcher.with_suffix(".json")
    manifest.write_text(json.dumps({
        "workspace": str(tmp_path), "scheduler_id": "mpt-codex", "role_id": "scheduler",
        "agent_id": "mpt-codex", "runtime_config": str(private),
        "api_url": f"http://127.0.0.1:{runtime.settings.port}",
    }), encoding="utf-8")

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    launcher.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"& {quote(python)} {quote(tmp_path / 'scripts' / 'mpt_agent_launchers.py')} run "
        f"--manifest {quote(manifest)} @CommandArgs\nexit $LASTEXITCODE\n", encoding="utf-8",
    )
    run = {"run_id": "run_" + "a" * 32, "content_id": "content_" + "b" * 32}
    try:
        with Session(runtime.engine) as session:
            config = default_scheduler_config()
            config["scheduler_id"] = "mpt-codex"
            save_scheduler_config(session, config)
            worker_launcher = stage / "mpt-codex-research.ps1"
            worker_launcher.write_text("fixture worker", encoding="utf-8")
            worker_launcher.with_suffix(".json").write_text("{}", encoding="utf-8")
            skill = stage / "skills" / "fixture"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text("fixture", encoding="utf-8")
            session.add(AgentWorker(agent_id="mpt-codex-research", role_id="research", launcher_path=str(worker_launcher), skill_content_hash=launch.tree_hash(skill), deployment_verified_at=utc_now()))
            session.add(AuditEvent(actor_type="agent", actor_id="fixture", entity_type="agent_worker", entity_id="mpt-codex-research", action=launch.VERIFIED, metadata_json={
                "launcher_sha256": launch.hashlib.sha256(worker_launcher.read_bytes()).hexdigest(),
                "manifest_sha256": launch.hashlib.sha256(worker_launcher.with_suffix(".json").read_bytes()).hexdigest(),
                "skill_path": str(skill), "device_label": "fixture-host",
            }))
            session.commit()
            command = launch.launch_command(runtime, session, run)
            assert command[:3] == [str(python), "-m", "app.business.conductor"]
            assert "test-admin" not in " ".join(command)
            worker_launcher.write_text("changed worker", encoding="utf-8")
            with pytest.raises(ValueError, match="proof changed"):
                launch.launch_command(runtime, session, run)
            worker_launcher.write_text("fixture worker", encoding="utf-8")
            launcher.write_text("arbitrary command", encoding="utf-8")
            with pytest.raises(ValueError, match="wrapper"):
                launch.launch_command(runtime, session, run)
            private.write_text(json.dumps({"admin_token": "OTHER PRIVATE TOKEN"}), encoding="utf-8")
            with pytest.raises(ValueError, match="mismatch"):
                launch.launch_command(runtime, session, run)
            assert Path(command[0]) == python
    finally:
        runtime.close()
