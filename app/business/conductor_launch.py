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

from glance_core.database import begin_immediate
from glance_core.content_revision import current_revision_clause
from glance_core.models import (
    AgentSubmission, AgentTask, AgentWorker, AuditEvent, Content, PublicationRecord,
    RuntimeSetting, SchedulerOffer, TaskStatus, TaskType, as_utc, utc_now,
)
from glance_core.scheduler_profiles import load_scheduler_config
from glance_core.task_service import TaskConflictError, stable_hash
from pydantic import BaseModel, ConfigDict
from sqlmodel import Session, select

from app.business.conductor import ConductorError, SchedulerPlanner
from app.business.consumption import content_operation_lock
from app.business.content_run import OFFER_ACTION, get_run, invalidate_offers, load_run, save_run, start_run
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


def _save(session, record, *, commit=True):
    for key in (PREFIX + record.content_id, PREFIX + record.run_id):
        row = session.get(RuntimeSetting, key) or RuntimeSetting(key=key, value="")
        row.value = record.model_dump_json()
        row.updated_at = utc_now()
        session.add(row)
    if commit:
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
        run = start_run(session, content_id, allow_local_image_adaptation=allow_local_image_adaptation,
                        runtime=runtime, execution=existing)
        retry_start = (existing and existing.state == "FAILED"
                       and existing.error_code == "CONTENT_EXECUTION_START_FAILED"
                       and run["state"] == "awaiting_host" and not run["host_epoch"])
        if retry_start:
            from app.business.content_run import load_run
            from app.business.image_authorization_recovery import receipt_for_run

            retry_start = receipt_for_run(session, load_run(session)) is not None
        if existing and existing.run_id == run["run_id"] and not retry_start:
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


HOST_LOSS_RECONCILED = "mpt_content_execution_host_loss_reconciled"


def process_absence_proof(pid):
    """Only Windows' nonexistent-PID error proves absence; never inspect or kill it."""
    import ctypes
    from ctypes import wintypes

    if os.name != "nt" or type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise TaskConflictError("CONTENT_EXECUTION_PROCESS_PROOF_UNAVAILABLE")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        kernel.CloseHandle(handle)
        raise TaskConflictError("CONTENT_EXECUTION_PROCESS_STILL_EXISTS")
    error = ctypes.get_last_error()
    if error != 87:
        raise TaskConflictError("CONTENT_EXECUTION_PROCESS_PROOF_UNAVAILABLE")
    return {"pid": pid, "winerror": error}


def _reconcilable_intents(session, run, journal):
    from app.business.content_host import accepted_binding, claim_summary, offer_receipt, validate_child
    from app.business.requests import NativeChild

    pending = []
    if set(journal["intents"]) != set(run.intents):
        raise TaskConflictError("CONTENT_EXECUTION_JOURNAL_CHANGED")
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.action == OFFER_ACTION,
        AuditEvent.metadata_json["worker"]["run_id"].as_string() == run.run_id,
    )).all()
    if {row.entity_id for row in receipts} != set(run.intents):
        raise TaskConflictError("CONTENT_EXECUTION_UNRESOLVED_OFFERS")
    for request_id, intent in run.intents.items():
        worker, offer_hash = offer_receipt(session, request_id)
        task = session.get(AgentTask, worker["task_id"])
        local = journal["intents"][request_id]
        offer = session.get(SchedulerOffer, request_id)
        if (
            task is None or (offer is None and intent["state"] != "TERMINAL")
            or worker.get("run_id") != run.run_id or worker.get("run_generation") != run.generation
            or worker.get("content_id") != run.content_id or task.content_id != run.content_id
            or worker.get("request_id") != request_id or worker.get("host_epoch") != run.host_epoch
            or worker.get("task_type") != task.task_type.value or task.task_type == TaskType.DOUYIN_PUBLISH
            or worker.get("input_hash") != task.input_hash or stable_hash(task.input_data) != task.input_hash
            or (offer is not None and (
                offer.task_id != task.id or offer.agent_id != worker.get("agent_id") or offer.task_type != task.task_type.value
            ))
            or intent["offer_hash"] != offer_hash or intent["host_epoch"] != run.host_epoch
            or any(local.get(key) != value for key, value in {
                "request_id": request_id, "task_id": task.id, "host_epoch": run.host_epoch,
                "offer_hash": offer_hash,
                "issued_prompt_sha256": hashlib.sha256(worker["prompt"].encode()).hexdigest(),
            }.items())
        ):
            raise TaskConflictError("CONTENT_EXECUTION_TASK_BINDING_CHANGED")
        if intent["state"] == "TERMINAL":
            child = NativeChild.model_validate(intent["child"])
            validate_child(run, worker, child, terminal=True)
            summary = claim_summary(session, intent)
            binding = accepted_binding(session, task, task.assigned_agent_id)
            if (
                not summary or not summary["accepted"] or not summary["consumed"]
                or summary["task_id"] != task.id or summary["claim_id"] != intent["claim_id"]
                or summary["accepted_result_hash"] != summary["consumed_result_hash"]
                or local.get("child") != intent["child"] or local.get("last_observation") != "TERMINAL"
                or local.get("business") != summary
                or run.offer_children.get(request_id) != child.child_id
                or task.started_at is None
                or not binding or any(binding.get(key) != value for key, value in {
                    "claim_id": intent["claim_id"], "run_id": run.run_id, "generation": run.generation,
                    "host_epoch": run.host_epoch, "request_id": request_id, "offer_hash": offer_hash,
                    "task_id": task.id, "agent_id": worker["agent_id"], "child_id": child.child_id,
                    "parent_thread_id": run.parent_thread_id,
                    "lease_token_hash": task.lease_token_hash,
                    "started_at": as_utc(task.started_at).isoformat() if task.started_at else None,
                }.items())
            ):
                raise TaskConflictError("CONTENT_EXECUTION_PRIOR_WORK_UNPROVEN")
            continue
        events = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
        )).all()
        if (
            intent["state"] != "SPAWN_INTENT" or intent["child"] is not None or intent["claim_id"] is not None
            or request_id in run.offer_children or task.task_type != TaskType.RECOVERY
            or task.status != TaskStatus.PENDING or task.attempt != 0
            or any(getattr(task, key) is not None for key in (
                "assigned_agent_id", "lease_token_hash", "lease_expires_at", "started_at",
                "attempt_started_at", "last_heartbeat_at", "completed_at", "result_data",
                "error_code", "error_message",
            ))
            or local.get("state") != "SPAWN_SEND_INTENT"
            or any(local.get(key) is not None for key in ("child", "started", "business", "last_observation", "claim_id"))
            or any(event.action not in {"created", "recovery_task_created", "mpt_task_owned"} for event in events)
            or any(event.action == "mpt_task_owned" and event.metadata_json.get("input_hash") != task.input_hash for event in events)
            or session.exec(select(AgentSubmission.id).where(AgentSubmission.task_id == task.id)).first()
        ):
            raise TaskConflictError("CONTENT_EXECUTION_UNCLAIMED_RECOVERY_UNPROVEN")
        pending.append((request_id, task))
    if len(pending) != 1:
        raise TaskConflictError("CONTENT_EXECUTION_UNCLAIMED_RECOVERY_UNPROVEN")
    return pending[0]


def reconcile_host_loss(runtime, session, content_id, *, expected):
    """Explicitly release one unclaimed recovery only after both host PIDs are gone."""
    from app.business.content_host import host_live

    with content_operation_lock(runtime.settings, "execution:" + content_id):
        begin_immediate(session)
        try:
            repeated = session.exec(select(AuditEvent).where(
                AuditEvent.action == HOST_LOSS_RECONCILED, AuditEvent.entity_id == expected.run_id,
            )).one_or_none()
            if repeated:
                if repeated.metadata_json["expected"] != expected.model_dump() or repeated.metadata_json["content_id"] != content_id:
                    raise TaskConflictError("CONTENT_EXECUTION_RECONCILIATION_BINDING_CHANGED")
                session.rollback()
                return repeated.metadata_json["response"]
            record = _load(session, content_id)
            run = load_run(session)
            if (
                record is None or run is None or session.get(Content, content_id) is None
                or record.launch_id != expected.launch_id or record.run_id != expected.run_id
                or record.generation != expected.generation or run.run_id != expected.run_id
                or run.generation != expected.generation or run.content_id != content_id
                or record.launch_id in _handles or record.state != "UNKNOWN"
                or record.error_code != "CONTENT_EXECUTION_OBSERVATION_LOST"
                or run.state != "running" or run.stop_requested or not run.host_epoch
                or not run.host_observed_at or host_live(run) or not run.parent_thread_id or not run.process_identity
                or not record.pid or not record.process_started_at or not record.binary_sha256
                or record.exit_code is not None or record.exited_at is not None
            ):
                raise TaskConflictError("CONTENT_EXECUTION_RECONCILIATION_BINDING_CHANGED")
            archived = session.get(RuntimeSetting, PREFIX + record.run_id)
            if archived is None or LaunchRecord.model_validate_json(archived.value) != record:
                raise TaskConflictError("CONTENT_EXECUTION_RECORD_CHANGED")
            if (
                datetime.fromisoformat(record.process_started_at).tzinfo is None
                or datetime.fromisoformat(run.process_identity["started_at"]).tzinfo is None
                or len(record.binary_sha256) != 64
                or any(char not in "0123456789abcdef" for char in record.binary_sha256)
            ):
                raise TaskConflictError("CONTENT_EXECUTION_RECORD_CHANGED")
            path = runtime.settings.data_dir / "private" / "content-runs" / record.run_id / "state.json"
            if path != path.resolve():
                raise TaskConflictError("CONTENT_EXECUTION_JOURNAL_CHANGED")
            original = path.read_bytes()
            journal = json.loads(original)
            if journal.get("version") != 1 or any(journal.get(key) != getattr(run, key) for key in (
                "content_id", "run_id", "generation", "host_epoch", "parent_thread_id", "process_identity",
            )):
                raise TaskConflictError("CONTENT_EXECUTION_JOURNAL_CHANGED")
            if (
                session.exec(select(AgentTask.id).where(
                    current_revision_clause(AgentTask), AgentTask.content_id == content_id,
                    (AgentTask.task_type == TaskType.DOUYIN_PUBLISH) | AgentTask.status.in_([TaskStatus.LEASED, TaskStatus.SUBMITTED]),
                )).first()
                or session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord), PublicationRecord.content_id == content_id)).first()
            ):
                raise TaskConflictError("CONTENT_EXECUTION_BUSINESS_WORK_UNRESOLVED")
            request_id, task = _reconcilable_intents(session, run, journal)
            proofs = [process_absence_proof(record.pid), process_absence_proof(run.process_identity["pid"])]
            if path.read_bytes() != original:
                raise TaskConflictError("CONTENT_EXECUTION_JOURNAL_CHANGED")
            before = record.model_dump()
            binding = {key: getattr(run, key) for key in (
                "run_id", "generation", "host_epoch", "parent_thread_id", "process_identity",
            )}
            run.intents[request_id] = {**run.intents[request_id], "state": "ABANDONED_BEFORE_CLAIM"}
            run.state, run.stop_requested, run.stop_reason = "stopped", True, "host_process_exit"
            invalidate_offers(session, run)
            save_run(session, run, "mpt_content_run_host_exit_reconciled")
            record.state, record.error_code = "FAILED", "CONTENT_EXECUTION_HOST_EXIT_CONFIRMED"
            record.exited_at = utc_now().isoformat()
            _save(session, record, commit=False)
            response = {**record.model_dump(), "run_state": run.state, "stop_requested": True, "host_ready": False, "can_stop": False}
            session.add(AuditEvent(
                actor_type="admin", actor_id="mpt-admin-api", entity_type="content_run", entity_id=run.run_id,
                action=HOST_LOSS_RECONCILED, metadata_json={
                    "expected": expected.model_dump(), "content_id": content_id, "previous_launch": before,
                    "binding": binding, "process_absence_proofs": proofs,
                    "journal_sha256": hashlib.sha256(original).hexdigest(), "request_id": request_id,
                    "task_id": task.id, "task_input_hash": task.input_hash, "response": response,
                },
            ))
            session.commit()
            return response
        except (OSError, ValueError, KeyError, TypeError) as exc:
            session.rollback()
            raise TaskConflictError("CONTENT_EXECUTION_RECONCILIATION_UNPROVEN") from exc
        except Exception:
            session.rollback()
            raise
