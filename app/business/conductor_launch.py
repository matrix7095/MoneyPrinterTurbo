"""Server-owned, at-most-once launch of one content conductor."""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from glance_core.models import AgentWorker, AuditEvent, Content, RuntimeSetting, utc_now
from glance_core.scheduler_profiles import load_scheduler_config
from glance_core.task_service import TaskConflictError
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.business.conductor import ConductorError, SchedulerPlanner
from app.business.consumption import content_operation_lock
from app.business.content_run import get_run, start_run
from app.business.scheduler import VERIFIED, tree_hash

PREFIX = "mpt_content_execution_v1:"
ACTIVE = {"STARTING", "RUNNING", "DRAINING", "UNKNOWN"}
# Handles are observation evidence only. Persistent records are launch authority.
_handles = {}


class LaunchRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    launch_id: str
    content_id: str
    run_id: str
    generation: int
    state: Literal["STARTING", "RUNNING", "DRAINING", "UNKNOWN", "FAILED", "FINISHED"]
    created_at: str
    pid: int | None = None
    process_started_at: str | None = None
    binary_sha256: str | None = None
    exited_at: str | None = None
    exit_code: int | None = None
    error_code: str = ""
    allow_local_image_adaptation: bool = False


class LaunchBinding(BaseModel):
    run_id: str
    generation: int
    host_epoch: int
    parent_thread_id: str | None
    process_identity: dict | None


def process_creation_time(process):
    """Read Windows creation identity from our open process handle, never a PID scan."""
    import ctypes
    from ctypes import wintypes

    creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
    function = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
    function.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    function.restype = wintypes.BOOL
    if not function(process._handle, *(ctypes.byref(item) for item in (creation, exited, kernel, user))):
        raise OSError("Process identity unavailable")
    ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    return datetime.fromtimestamp(ticks / 10_000_000 - 11644473600, timezone.utc).isoformat()


def _load(session, content_id):
    row = session.get(RuntimeSetting, PREFIX + content_id)
    if row is None:
        return None
    try:
        return LaunchRecord.model_validate_json(row.value)
    except ValueError:
        raise TaskConflictError("CONTENT_EXECUTION_RECORD_INVALID") from None


def _save(session, record):
    for key in (PREFIX + record.content_id, PREFIX + record.run_id):
        row = session.get(RuntimeSetting, key) or RuntimeSetting(key=key, value="")
        row.value = record.model_dump_json()
        row.updated_at = utc_now()
        session.add(row)
    session.commit()


def launch_command(runtime, session, run):
    """Select the installed workspace, private config and verified deployment only."""
    workspace = runtime.workspace_root
    private = runtime.settings.data_dir.resolve() / "private" / "runtime.json"
    python = workspace / ".venv" / "Scripts" / "python.exe"
    if (
        workspace != workspace.resolve()
        or not private.is_relative_to(workspace / "storage")
        or private != private.resolve()
        or python != python.resolve()
        or not python.is_file()
        or Path(sys.executable).resolve() != python
    ):
        raise ValueError("Invalid installed runtime")
    config = json.loads(private.read_text(encoding="utf-8"))
    token = config.get("admin_token")
    if not isinstance(token, str) or not hmac.compare_digest(
        token.encode(), runtime.settings.admin_token.get_secret_value().encode()
    ):
        raise ValueError("Private runtime mismatch")
    scheduler_id = load_scheduler_config(session)["scheduler_id"]
    workers = session.exec(
        select(AgentWorker).where(AgentWorker.deployment_verified_at.is_not(None))
    ).all()
    parents = {
        Path(worker.launcher_path).parent
        for worker in workers
        if worker.agent_id == f"{scheduler_id}-{worker.role_id}"
    }
    if len(parents) != 1:
        raise ValueError("Deployment missing or ambiguous")
    launcher = parents.pop() / f"{scheduler_id}.ps1"
    SchedulerPlanner(workspace, launcher, scheduler_id)
    manifest_path = launcher.with_suffix(".json")
    if manifest_path != manifest_path.resolve():
        raise ValueError("Redirected scheduler manifest")
    for worker in workers:
        if worker.agent_id != f"{scheduler_id}-{worker.role_id}":
            continue
        proof = session.exec(
            select(AuditEvent).where(
                AuditEvent.action == VERIFIED, AuditEvent.entity_id == worker.agent_id,
            ).order_by(AuditEvent.created_at.desc())
        ).first()
        if proof is None:
            raise ValueError("Deployment proof missing")
        worker_launcher = Path(worker.launcher_path)
        data = proof.metadata_json
        if (
            worker_launcher != worker_launcher.resolve()
            or worker_launcher.parent != launcher.parent
            or hashlib.sha256(worker_launcher.read_bytes()).hexdigest() != data["launcher_sha256"]
            or hashlib.sha256(worker_launcher.with_suffix(".json").read_bytes()).hexdigest() != data["manifest_sha256"]
            or tree_hash(Path(data["skill_path"])) != worker.skill_content_hash
            or not data["device_label"]
        ):
            raise ValueError("Deployment proof changed")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    api_url = f"http://127.0.0.1:{runtime.settings.port}"
    if manifest.get("runtime_config") != str(private) or manifest.get("api_url") != api_url:
        raise ValueError("Scheduler runtime mismatch")

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    expected = (
        "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)\n"
        "$ErrorActionPreference = 'Stop'\n"
        f"& {quote(python)} {quote(workspace / 'scripts' / 'mpt_agent_launchers.py')} run "
        f"--manifest {quote(manifest_path)} @CommandArgs\nexit $LASTEXITCODE\n"
    )
    if launcher.read_text(encoding="utf-8-sig") != expected:
        raise ValueError("Scheduler wrapper changed")
    journal = private.parent / "content-runs" / run["run_id"] / "state.json"
    if journal.exists():
        raise ValueError("Existing conductor journal")
    return [
        str(python), "-m", "app.business.conductor", "--workspace", str(workspace),
        "--api-url", api_url, "--runtime-private", str(private),
        "--scheduler-launcher", str(launcher), "--scheduler-id", scheduler_id,
        "--run-id", run["run_id"], "--content-id", run["content_id"],
    ]


def _own_binding(runtime, record, run):
    if run is None:
        return None
    if run["run_id"] != record.run_id or run["generation"] != record.generation:
        return None
    if not run["host_epoch"]:
        return LaunchBinding(
            run_id=record.run_id, generation=record.generation, host_epoch=0,
            parent_thread_id=None, process_identity=None,
        )
    path = runtime.settings.data_dir / "private" / "content-runs" / record.run_id / "state.json"
    try:
        journal = json.loads(path.read_text(encoding="utf-8"))
        fields = ("run_id", "generation", "host_epoch", "parent_thread_id", "process_identity")
        binding = {key: run[key] for key in fields}
        if path != path.resolve() or any(journal.get(key) != value for key, value in binding.items()):
            return None
        return LaunchBinding(**binding)
    except (OSError, ValueError, KeyError):
        return None


def _current_run(session, record):
    try:
        run = get_run(session, record.content_id)
    except KeyError:
        session.rollback()
        return None
    return run if run["run_id"] == record.run_id else None


def _view(runtime, session, record):
    run = _current_run(session, record)
    own = _own_binding(runtime, record, run)
    if record.state in ACTIVE:
        if record.launch_id not in _handles:
            record.state, record.error_code = "UNKNOWN", "CONTENT_EXECUTION_OBSERVATION_LOST"
        elif own is None:
            record.state, record.error_code = "UNKNOWN", "CONTENT_EXECUTION_HOST_BINDING_CHANGED"
        elif run["host_epoch"] and run["host_live"] and record.process_started_at:
            record.state = "DRAINING" if run["stop_requested"] else "RUNNING"
            record.error_code = ""
        elif run["host_epoch"]:
            record.state, record.error_code = "UNKNOWN", "CONTENT_EXECUTION_HOST_OBSERVATION_LOST"
        _save(session, record)
    content = session.get(Content, record.content_id)
    return {
        **record.model_dump(), "host_ready": bool(own and run["host_live"] and record.state in {"RUNNING", "DRAINING"}),
        "run_state": run["state"] if run else None,
        "content_status": content.business_status.value if content else None,
        "current_stage": content.current_stage if content else None,
        "active_task_count": len(run["active_task_ids"]) if run else 0,
        "can_stop": bool(own and record.state in ACTIVE and not run["stop_requested"]),
        "stop_requested": run["stop_requested"] if run else False,
    }


def execution_view(runtime, session, content_id):
    with content_operation_lock(runtime.settings, "execution:" + content_id):
        record = _load(session, content_id)
        if record is None:
            if session.get(Content, content_id) is None:
                raise KeyError(content_id)
            return {"state": "NOT_STARTED", "host_ready": False, "can_stop": False}
        return _view(runtime, session, record)


def _finished(runtime, content_id, launch_id, process):
    try:
        code = process.wait()
        with content_operation_lock(runtime.settings, "execution:" + content_id):
            with Session(runtime.engine) as session:
                record = _load(session, content_id)
                if record is None or record.launch_id != launch_id:
                    return
                record.exit_code, record.exited_at = code, utc_now().isoformat()
                run = _current_run(session, record)
                own = _own_binding(runtime, record, run)
                if code != 0 or not own or run["state"] not in {"stopped", "expired"}:
                    record.state = "FAILED"
                    record.error_code = "CONTENT_EXECUTION_EXIT_FAILED"
                else:
                    record.state, record.error_code = "FINISHED", ""
                _save(session, record)
                # Request cooperative drain only for this launch's unchanged binding.
                if own and record.state == "FAILED":
                    get_run(session, content_id, stop=True, expected_binding=own)
    finally:
        _handles.pop(launch_id, None)


def execute_content(runtime, session, content_id, *, allow_local_image_adaptation=False):
    with content_operation_lock(runtime.settings, "execution:" + content_id):
        existing = _load(session, content_id)
        if existing and existing.state in ACTIVE:
            if existing.allow_local_image_adaptation != allow_local_image_adaptation:
                raise TaskConflictError("CONTENT_RUN_PERMISSION_FROZEN")
            return _view(runtime, session, existing)
        run = start_run(session, content_id, allow_local_image_adaptation=allow_local_image_adaptation)
        if existing and existing.run_id == run["run_id"]:
            return _view(runtime, session, existing)
        if run["host_epoch"] or run["state"] != "awaiting_host":
            raise TaskConflictError("CONTENT_EXECUTION_HOST_ALREADY_BOUND")
        record = LaunchRecord(
            launch_id=secrets.token_hex(16), content_id=content_id, run_id=run["run_id"],
            generation=run["generation"], state="STARTING", created_at=utc_now().isoformat(),
            allow_local_image_adaptation=run["allow_local_image_adaptation"],
        )
        # Commit intent before spawning: a crash in the gap is uncertain, never retryable.
        _save(session, record)
        try:
            command = launch_command(runtime, session, run)
            record.binary_sha256 = hashlib.sha256(Path(command[0]).read_bytes()).hexdigest()
            environment = {
                key: value for key, value in os.environ.items()
                if not key.startswith(("MPT_", "GLANCE_"))
            }
            process = subprocess.Popen(
                command, cwd=runtime.workspace_root, env=environment, shell=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (ConductorError, OSError, ValueError, KeyError):
            record.state, record.error_code = "FAILED", "CONTENT_EXECUTION_START_FAILED"
            _save(session, record)
            own = _own_binding(runtime, record, get_run(session, content_id))
            if own:
                get_run(session, content_id, stop=True, expected_binding=own)
            return _view(runtime, session, record)
        # No PID lookup/kill is used; this handle belongs to exactly this launch.
        record.pid = process.pid
        _handles[record.launch_id] = process
        try:
            record.process_started_at = process_creation_time(process)
        except OSError:
            record.state, record.error_code = "UNKNOWN", "CONTENT_EXECUTION_PROCESS_IDENTITY_LOST"
        _save(session, record)
        threading.Thread(
            target=_finished, args=(runtime, content_id, record.launch_id, process), daemon=True,
        ).start()
        return _view(runtime, session, record)


def stop_execution(runtime, session, content_id):
    with content_operation_lock(runtime.settings, "execution:" + content_id):
        record = _load(session, content_id)
        if record is None:
            raise KeyError(content_id)
        run = get_run(session, content_id)
        binding = _own_binding(runtime, record, run)
        if binding is None:
            raise TaskConflictError("CONTENT_EXECUTION_HOST_BINDING_CHANGED")
        get_run(session, content_id, stop=True, expected_binding=binding)
        return _view(runtime, session, record)
