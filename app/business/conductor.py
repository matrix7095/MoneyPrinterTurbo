"""One on-demand native host; the business service owns offers and acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from glance_agent.native_desktop import (
    POWERSHELL_EXECUTABLE,
    NativeDesktopBinary,
    NativeDesktopResolutionError,
    resolve_native_desktop_binary,
)
from glance_agent.scheduler_handoff import offer_path
from glance_core.task_service import stable_hash

from app.business.consumption import content_operation_lock
from app.business.content_run import FROZEN_FIELDS

PARENT_MODEL = "gpt-6.1-sol"
PARENT_EFFORT = "high"
POLL_SECONDS = 5
HEARTBEAT_SECONDS = 30
MAX_ROUNDS = 16
REQUEST_ERROR_CLASSES = {
    "ConnectError",
    "ConnectTimeout",
    "ReadError",
    "ReadTimeout",
    "WriteError",
    "WriteTimeout",
    "PoolTimeout",
    "CloseError",
    "LocalProtocolError",
    "RemoteProtocolError",
    "ProxyError",
    "UnsupportedProtocol",
    "TooManyRedirects",
    "DecodingError",
}
BACKEND_ENDPOINTS = (
    (r"/api/v1/contents/[^/?#]+/run", "/api/v1/contents/{content_id}/run"),
    (r"/api/v1/contents/[^/?#]+/run/stop", "/api/v1/contents/{content_id}/run/stop"),
    (
        r"/api/v1/content-runs/[^/?#]+/host/attach",
        "/api/v1/content-runs/{run_id}/host/attach",
    ),
    (
        r"/api/v1/content-runs/[^/?#]+/intents/[^/?#]+",
        "/api/v1/content-runs/{run_id}/intents/{request_id}",
    ),
    (
        r"/api/v1/content-runs/[^/?#]+/intents/[^/?#]+/observation",
        "/api/v1/content-runs/{run_id}/intents/{request_id}/observation",
    ),
)


class ConductorError(RuntimeError):
    """Only safe, fixed error codes cross the CLI boundary."""


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def require_fresh_run(view, run_id):
    if (
        view.get("run_id") != run_id
        or type(view.get("generation")) is not int
        or view["generation"] < 1
        or view.get("intents")
        or type(view.get("host_epoch")) is not int
        or view["host_epoch"] != 0
        or view.get("parent_thread_id") is not None
        or view.get("process_identity") is not None
        or view.get("state") != "awaiting_host"
    ):
        raise ConductorError("CONDUCTOR_RUN_NOT_FRESH")


def event_summary(message: dict) -> dict:
    """Keep identity/lifecycle, never prompt, DOM, tool output or credentials."""
    result = {key: message[key] for key in ("id", "method") if key in message}
    params = message.get("params") or {}
    raw_result = message.get("result") or {}
    if not isinstance(params, dict) or not isinstance(raw_result, dict):
        raise ConductorError("CONDUCTOR_NATIVE_INVALID_EVENT")
    result.update(
        {key: params[key] for key in ("threadId", "turnId", "itemId") if key in params}
    )
    item = params.get("item") or {}
    result["item"] = {
        key: item[key]
        for key in (
            "id",
            "type",
            "kind",
            "status",
            "agentThreadId",
            "agentPath",
            "tool",
            "server",
        )
        if key in item
    }
    thread = raw_result.get("thread")
    if isinstance(thread, dict):
        result["thread"] = {
            key: thread[key]
            for key in (
                "id",
                "parentThreadId",
                "agentRole",
                "model",
                "reasoningEffort",
                "ephemeral",
            )
            if key in thread
        }
    turn = params.get("turn") or raw_result.get("turn") or {}
    if turn:
        result["turn"] = {key: turn[key] for key in ("id", "status") if key in turn}
    result["payload_sha256"] = stable_hash(message)
    result["payload_bytes"] = len(
        json.dumps(message, ensure_ascii=False).encode("utf-8")
    )
    return result


class Journal:
    def __init__(self, directory: Path, run_id: str, content_id: str):
        self.directory = directory
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory != directory.resolve():
            raise ConductorError("CONDUCTOR_STATE_REDIRECTED")
        self.path = directory / "state.json"
        if self.path.exists():
            # Ephemeral host history is not recoverable from a local cursor.
            raise ConductorError("CONDUCTOR_RECOVERY_REQUIRED")
        self.state = {
            "version": 1,
            "run_id": run_id,
            "content_id": content_id,
            "phase": "NEW",
            "intents": {},
        }
        self.save()
        self.sequence = 0
        self.log = (directory / "events.jsonl").open("x", encoding="utf-8")
        self.log_path = directory / "events.jsonl"

    def save(self):
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def record(self, kind: str, data: dict):
        self.sequence += 1
        json.dump(
            {"seq": self.sequence, "kind": kind, **data}, self.log, ensure_ascii=False
        )
        self.log.write("\n")
        self.log.flush()
        os.fsync(self.log.fileno())

    def close(self):
        self.log.close()


class HttpBackend:
    def __init__(self, api_url: str, runtime_private: Path, workspace: Path):
        if (
            not re.fullmatch(r"http://127\.0\.0\.1:[1-9][0-9]{0,4}", api_url)
            or int(api_url.rsplit(":", 1)[1]) > 65535
        ):
            raise ConductorError("CONDUCTOR_API_URL_INVALID")
        if (
            not runtime_private.is_absolute()
            or runtime_private != runtime_private.resolve()
            or not runtime_private.is_relative_to(workspace / "storage")
            or runtime_private.parent.name != "private"
        ):
            raise ConductorError("CONDUCTOR_PRIVATE_CONFIG_INVALID")
        data = json.loads(runtime_private.read_text(encoding="utf-8"))
        token = data.get("admin_token")
        if (
            not isinstance(token, str)
            or not token
            or token == "change-me-before-network-use"
        ):
            raise ConductorError("CONDUCTOR_ADMIN_TOKEN_INVALID")
        self.client = httpx.Client(
            base_url=api_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
            trust_env=False,
            follow_redirects=False,
            # Polling and the server both idle for five seconds. Avoid reusing
            # a socket exactly as the server closes it; never retry a request.
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=0),
        )
        self.last_request_failure = None

    def request(self, method: str, path: str, body: dict | None = None):
        self.last_request_failure = None
        started = time.monotonic()
        try:
            response = self.client.request(
                method, path, **({"json": body} if body is not None else {})
            )
        except httpx.RequestError as exc:
            # Only fixed labels and elapsed time survive; exception/request
            # contents and dynamic path segments can contain credentials.
            endpoint = "unknown"
            for pattern, label in BACKEND_ENDPOINTS:
                if re.fullmatch(pattern, path.split("?", 1)[0].split("#", 1)[0]):
                    endpoint = label
                    break
            name = type(exc).__name__
            self.last_request_failure = {
                "error_class": name
                if name in REQUEST_ERROR_CLASSES
                else "RequestError",
                "method": method if method in {"GET", "POST", "PUT"} else "OTHER",
                "endpoint": endpoint,
                "elapsed_seconds": round(max(0, time.monotonic() - started), 6),
            }
            raise ConductorError("CONDUCTOR_BACKEND_UNREACHABLE") from None
        if response.status_code >= 300:
            raise ConductorError(f"CONDUCTOR_BACKEND_HTTP_{response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ConductorError("CONDUCTOR_BACKEND_INVALID_JSON") from None
        if not isinstance(data, dict):
            raise ConductorError("CONDUCTOR_BACKEND_INVALID_SHAPE")
        return data

    def view(self, content_id):
        return self.request("GET", f"/api/v1/contents/{content_id}/run")

    def attach(self, run_id, body):
        return self.request("POST", f"/api/v1/content-runs/{run_id}/host/attach", body)

    def intent(self, run_id, request_id, body):
        return self.request(
            "PUT", f"/api/v1/content-runs/{run_id}/intents/{request_id}", body
        )

    def observe(self, run_id, request_id, body):
        return self.request(
            "POST",
            f"/api/v1/content-runs/{run_id}/intents/{request_id}/observation",
            body,
        )

    def stop(self, content_id, expected_binding):
        return self.request(
            "POST",
            f"/api/v1/contents/{content_id}/run/stop",
            {"expected_binding": expected_binding},
        )

    def close(self):
        self.client.close()


class SchedulerPlanner:
    def __init__(self, workspace: Path, launcher: Path, scheduler_id: str):
        root = workspace / "storage" / "agent-launcher-staging"
        if (
            launcher != launcher.resolve()
            or not launcher.is_relative_to(root)
            or launcher.name != f"{scheduler_id}.ps1"
            or not launcher.is_file()
        ):
            raise ConductorError("CONDUCTOR_SCHEDULER_LAUNCHER_INVALID")
        manifest = json.loads(launcher.with_suffix(".json").read_text(encoding="utf-8"))
        if (
            manifest.get("workspace") != str(workspace)
            or manifest.get("scheduler_id") != scheduler_id
            or manifest.get("role_id") != "scheduler"
            or manifest.get("agent_id") != scheduler_id
        ):
            raise ConductorError("CONDUCTOR_SCHEDULER_MANIFEST_INVALID")
        self.workspace, self.launcher, self.scheduler_id = (
            workspace,
            launcher,
            scheduler_id,
        )

    def __call__(self, run_id):
        try:
            result = subprocess.run(
                [
                    str(POWERSHELL_EXECUTABLE),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(self.launcher),
                    "scheduler-plan",
                    "--scheduler-id",
                    self.scheduler_id,
                    "--contract-version",
                    "2",
                    "--run-id",
                    run_id,
                ],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=30,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ConductorError("CONDUCTOR_PLAN_UNAVAILABLE") from None
        if result.returncode:
            raise ConductorError("CONDUCTOR_PLAN_HANDOFF_ERROR")
        try:
            plan = json.loads(result.stdout.lstrip("\ufeff"))
        except ValueError:
            raise ConductorError("CONDUCTOR_PLAN_INVALID_JSON") from None
        if not isinstance(plan, dict) or not isinstance(
            plan.get("worker_requests"), list
        ):
            raise ConductorError("CONDUCTOR_PLAN_INVALID")
        for worker in plan["worker_requests"]:
            worker_launcher = Path(worker["launcher"])
            if (
                worker_launcher != worker_launcher.resolve()
                or worker_launcher.parent != self.launcher.parent
                or worker_launcher.name
                != f"{self.scheduler_id}-{worker['role_id']}.ps1"
            ):
                raise ConductorError("CONDUCTOR_OFFER_LAUNCHER_INVALID")
            envelope = offer_path(worker_launcher, worker["request_id"]).with_suffix(
                ".json"
            )
            if envelope != envelope.resolve():
                raise ConductorError("CONDUCTOR_OFFER_ENVELOPE_INVALID")
            data = json.loads(envelope.read_text(encoding="utf-8"))
            if any(data.get(field) != worker.get(field) for field in FROZEN_FIELDS):
                raise ConductorError("CONDUCTOR_OFFER_ENVELOPE_CHANGED")
            if (
                type(data.get("deadline")) not in {int, float}
                or not time.time() < data["deadline"] <= time.time() + 600
            ):
                raise ConductorError("CONDUCTOR_OFFER_EXPIRED")
            worker["_offer_deadline"] = data["deadline"]
        return plan


class NativeTransport:
    def __init__(self, binary: NativeDesktopBinary, workspace: Path):
        self.binary = binary
        self.workspace = workspace
        self.process = subprocess.Popen(
            [
                str(binary.path),
                "app-server",
                "--stdio",
                "-c",
                "features.multi_agent=true",
            ],
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.queue = queue.Queue()
        self.eof = False
        self.counter = 0
        for stream, channel in (
            (self.process.stdout, "stdout"),
            (self.process.stderr, "stderr"),
        ):
            threading.Thread(
                target=self._reader, args=(stream, channel), daemon=True
            ).start()
        self.identity = {
            "pid": self.process.pid,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "instance_id": uuid.uuid4().hex,
            "binary_path": str(binary.path),
            "binary_sha256": binary.sha256,
            "version": binary.version,
        }

    def _reader(self, stream, channel):
        for line in stream:
            if channel == "stderr":
                self.queue.put(
                    {
                        "method": "conductor/stderr",
                        "params": {"sha256": prompt_sha256(line)},
                    }
                )
            else:
                try:
                    data = json.loads(line)
                except ValueError:
                    data = {"method": "conductor/invalidJson"}
                self.queue.put(data)
        if channel == "stdout":
            self.queue.put(None)

    def send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError):
            raise ConductorError("CONDUCTOR_NATIVE_SEND_UNCERTAIN") from None

    def request(self, method, params):
        self.counter += 1
        identifier = f"rpc_{self.counter}"
        self.send(
            {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        )
        return identifier

    def receive(self, timeout=POLL_SECONDS):
        try:
            data = self.queue.get(timeout=min(timeout, 30))
        except queue.Empty:
            if self.process.poll() is not None:
                self.eof = True
                raise ConductorError("CONDUCTOR_NATIVE_OBSERVATION_LOST") from None
            return None
        if data is None:
            self.eof = True
            raise ConductorError("CONDUCTOR_NATIVE_OBSERVATION_LOST")
        return data

    def alive(self):
        return self.process.poll() is None and not self.eof

    def process_alive(self):
        return self.process.poll() is None

    def close(self):
        # Only this Popen handle is touched; no PID scans or existing app shutdown.
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)


def validate_worker(worker, view, run_id, content_id):
    if (
        not isinstance(worker, dict)
        or worker.get("run_id") != run_id
        or worker.get("content_id") != content_id
        or worker.get("run_generation") != view["generation"]
        or worker.get("host_epoch") != view["host_epoch"]
    ):
        raise ConductorError("CONDUCTOR_OFFER_SCOPE_CHANGED")
    if (
        type(worker.get("run_generation")) is not int
        or type(worker.get("host_epoch")) is not int
    ):
        raise ConductorError("CONDUCTOR_OFFER_SCOPE_CHANGED")
    if (
        worker.get("native_agent_type") != "default"
        or worker.get("fork_turns") != "none"
        or worker.get("model") not in {"gpt-6.1-sol", "gpt-6-luna"}
        or worker.get("reasoning_effort") not in {"medium", "high", "xhigh", "max"}
    ):
        raise ConductorError("CONDUCTOR_OFFER_ROUTE_INVALID")
    if (
        not re.fullmatch(r"[a-z0-9_]{1,200}", str(worker.get("title", "")))
        or not isinstance(worker.get("prompt"), str)
        or not worker["prompt"]
    ):
        raise ConductorError("CONDUCTOR_OFFER_PROMPT_INVALID")
    try:
        frozen_hash = stable_hash({field: worker[field] for field in FROZEN_FIELDS})
    except KeyError:
        raise ConductorError("CONDUCTOR_OFFER_INCOMPLETE") from None
    if frozen_hash != worker.get("offer_hash"):
        raise ConductorError("CONDUCTOR_OFFER_HASH_CHANGED")


class Conductor:
    """Conservative serial parent turns; no parallelism is claimed or simulated."""

    def __init__(
        self,
        backend,
        planner,
        native,
        journal,
        run_id,
        content_id,
        *,
        clock=time.time,
        monotonic=time.monotonic,
    ):
        self.backend, self.planner, self.native, self.journal = (
            backend,
            planner,
            native,
            journal,
        )
        self.run_id, self.content_id, self.clock, self.monotonic = (
            run_id,
            content_id,
            clock,
            monotonic,
        )
        self.parent = None
        self.parent_turn = None
        self.parent_idle = True
        self.generation = None
        self.epoch = None
        self.current = None
        self.inbox = deque()
        self.responses = {}
        self.metadata = {}
        self.children = {}
        self.stop_reason = None
        self.last_heartbeat = monotonic()

    def _observe(self, outcome, request_id="host", child=None):
        body = {
            "generation": self.generation,
            "host_epoch": self.epoch,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "outcome": outcome,
            "child": child,
        }
        self.journal.record(
            "observation_send",
            {
                "request_id": request_id,
                "outcome": outcome,
                "event_id": body["event_id"],
            },
        )
        response = self.backend.observe(self.run_id, request_id, body)
        if (
            self.current
            and self.current["worker"]["request_id"] == request_id
            and outcome in {"REJECTED", "UNKNOWN", "OBSERVATION_LOST", "TERMINAL"}
        ):
            self.current["admitted"] = False
        self.journal.record(
            "observation_committed",
            {
                "request_id": request_id,
                "outcome": outcome,
                "event_id": body["event_id"],
            },
        )
        if request_id in self.journal.state["intents"]:
            self.journal.state["intents"][request_id]["last_observation"] = outcome
            self.journal.save()
        return response

    def _stop(self, reason):
        if self.stop_reason:
            return
        self.stop_reason = reason
        self.journal.state["phase"] = reason
        self.journal.save()
        self.backend.stop(self.content_id, self._own_binding())

    def _own_binding(self, view=None):
        epoch = self.epoch if self.epoch is not None else (view or {}).get("host_epoch")
        if not self.parent or type(epoch) is not int or epoch < 1:
            raise ConductorError("CONDUCTOR_HOST_BINDING_UNKNOWN")
        binding = {
            "run_id": self.run_id,
            "generation": self.generation,
            "host_epoch": epoch,
            "parent_thread_id": self.parent,
            "process_identity": dict(self.native.identity),
        }
        if view is not None and any(
            view.get(key) != value for key, value in binding.items()
        ):
            raise ConductorError("CONDUCTOR_HOST_BINDING_CHANGED")
        return binding

    def _heartbeat(self):
        if (
            self.epoch is None
            or self.monotonic() - self.last_heartbeat < HEARTBEAT_SECONDS
            or not self.native.alive()
        ):
            return
        if (
            self.current
            and self.current.get("admitted")
            and not self.children[self.current["child"]["child_id"]]["terminal"]
        ):
            self._observe(
                "HEARTBEAT", self.current["worker"]["request_id"], self.current["child"]
            )
        else:
            self._observe("HEARTBEAT")
        self.last_heartbeat = self.monotonic()

    def _rpc(self, method, params, timeout=30):
        identifier = self.native.request(method, params)
        deadline = self.monotonic() + timeout
        while identifier not in self.responses:
            if self.monotonic() >= deadline:
                raise ConductorError("CONDUCTOR_NATIVE_RESPONSE_UNCERTAIN")
            self._pump(min(POLL_SECONDS, max(0.1, deadline - self.monotonic())))
        response = self.responses.pop(identifier)
        if "error" in response:
            raise ConductorError("CONDUCTOR_NATIVE_RPC_REJECTED")
        return response.get("result") or {}

    def _pump(self, timeout=POLL_SECONDS):
        self._heartbeat()
        message = self.native.receive(min(timeout, 30))
        if message is None:
            return
        if not isinstance(message, dict):
            raise ConductorError("CONDUCTOR_NATIVE_INVALID_EVENT")
        self.journal.record("native", event_summary(message))
        if "method" in message and "id" in message:
            self.native.send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Unexpected host callback requires operator review",
                    },
                }
            )
            if self.current:
                self._observe(
                    "REJECTED",
                    self.current["worker"]["request_id"],
                    self.current.get("child"),
                )
            self._stop("HOST_APPROVAL_REQUIRED")
            return
        if "method" not in message:
            identifier = message.get("id")
            if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
                raise ConductorError("CONDUCTOR_NATIVE_INVALID_RPC_ID")
            if identifier in self.metadata:
                started = self.metadata.pop(identifier)
                self._verify_child(identifier, started, message)
            else:
                self.responses[identifier] = message
            return
        method, params = message["method"], message.get("params") or {}
        if method == "conductor/invalidJson":
            raise ConductorError("CONDUCTOR_NATIVE_INVALID_JSON")
        if method == "turn/started" and params.get("threadId") == self.parent:
            self.parent_turn = params["turn"]["id"]
            self.parent_idle = False
        if method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            if (
                item.get("type") == "subAgentActivity"
                and item.get("kind") == "started"
                and method == "item/started"
            ):
                self._child_started(params, item)
        if method == "turn/completed":
            thread_id = params.get("threadId")
            if thread_id == self.parent:
                self.parent_idle = True
                if self.current and not self.current.get("started"):
                    self._observe("UNKNOWN", self.current["worker"]["request_id"])
                    self._stop("CONDUCTOR_SPAWN_NOT_OBSERVED")
            elif thread_id in self.children:
                self.children[thread_id]["terminal"] = True
                if (
                    self.current
                    and self.current.get("child", {}).get("child_id") == thread_id
                ):
                    self._observe(
                        "TERMINAL",
                        self.current["worker"]["request_id"],
                        self.current["child"],
                    )

    def _child_started(self, params, item):
        child_id = item.get("agentThreadId")
        if child_id in self.children:
            return
        self.children[child_id] = {"terminal": False}
        if not isinstance(child_id, str) or not child_id:
            raise ConductorError("CONDUCTOR_NATIVE_INVALID_CHILD")
        if (
            not self.current
            or params.get("threadId") != self.parent
            or params.get("turnId") != self.parent_turn
            or item.get("agentPath", "").rsplit("/", 1)[-1]
            != self.current["worker"]["title"]
            or self.current.get("child")
            or self.metadata
        ):
            if self.current:
                self._observe(
                    "REJECTED",
                    self.current["worker"]["request_id"],
                    self.current.get("child"),
                )
            self._stop("CONDUCTOR_UNEXPECTED_CHILD")
            return
        started = {
            "parent_thread_id": params["threadId"],
            "parent_turn_id": params["turnId"],
            "item_id": item["id"],
            "agent_path": item["agentPath"],
            "child_id": child_id,
        }
        self.current["started"] = started
        identifier = self.native.request(
            "thread/read", {"threadId": child_id, "includeTurns": False}
        )
        self.metadata[identifier] = started

    def _verify_child(self, rpc_id, started, response):
        worker = self.current["worker"]
        thread = (response.get("result") or {}).get("thread") or {}
        expected = {
            "id": started["child_id"],
            "parentThreadId": self.parent,
            "agentRole": worker["native_agent_type"],
            "model": worker["model"],
            "reasoningEffort": worker["reasoning_effort"],
            "ephemeral": True,
        }
        if "error" in response or any(
            thread.get(key) != value for key, value in expected.items()
        ):
            self._observe("REJECTED", worker["request_id"])
            self._stop("CONDUCTOR_CHILD_METADATA_MISMATCH")
            return
        child = {
            "child_id": thread["id"],
            "parent_thread_id": thread["parentThreadId"],
            "native_agent_type": thread["agentRole"],
            "model": thread["model"],
            "reasoning_effort": thread["reasoningEffort"],
            "ephemeral": thread["ephemeral"],
            "started": started,
            "metadata_rpc_id": rpc_id,
            "issued_prompt_sha256": prompt_sha256(worker["prompt"]),
        }
        self.current["child"] = child
        self.journal.state["intents"][worker["request_id"]]["child"] = child
        self.journal.save()
        if self.stop_reason:
            self._observe("REJECTED", worker["request_id"], child)
        else:
            self._observe("VERIFIED", worker["request_id"], child)
            self.current["admitted"] = True
        if self.children[child["child_id"]]["terminal"]:
            self._observe("TERMINAL", worker["request_id"], child)

    def _spawn(self, worker, view):
        validate_worker(worker, view, self.run_id, self.content_id)
        if worker.get("_offer_deadline", self.clock() + 1) <= self.clock():
            self._stop("CONDUCTOR_OFFER_EXPIRED")
            return
        request_id = worker["request_id"]
        if request_id in self.journal.state["intents"]:
            raise ConductorError("CONDUCTOR_INTENT_ALREADY_SENT")
        self.current = {"worker": worker}
        safe_worker = {
            key: worker[key]
            for key in (
                "request_id",
                "task_id",
                "title",
                "model",
                "reasoning_effort",
                "offer_hash",
                "host_epoch",
                "round_id",
            )
        }
        safe_worker.update(
            state="INTENT_SEND", issued_prompt_sha256=prompt_sha256(worker["prompt"])
        )
        self.journal.state["intents"][request_id] = safe_worker
        self.journal.save()
        body = {
            "generation": self.generation,
            "host_epoch": self.epoch,
            "event_id": f"evt_{uuid.uuid4().hex}",
            "offer_hash": worker["offer_hash"],
        }
        committed = self.backend.intent(self.run_id, request_id, body)
        if (
            committed.get("request_id") != request_id
            or committed.get("state") != "SPAWN_INTENT"
            or committed.get("offer_hash") != worker["offer_hash"]
            or committed.get("host_epoch") != self.epoch
        ):
            raise ConductorError("CONDUCTOR_INTENT_COMMIT_UNCERTAIN")
        safe_worker["state"] = "SPAWN_SEND_INTENT"
        self.journal.save()
        values = {
            "task_name": worker["title"],
            "agent_type": worker["native_agent_type"],
            "model": worker["model"],
            "reasoning_effort": worker["reasoning_effort"],
            "fork_turns": worker["fork_turns"],
            "message": worker["prompt"],
        }
        prompt = (
            "Call the host-native spawn API exactly once with this JSON's values verbatim. Do not execute the worker task yourself, alter the message, change model/effort, substitute a chat, or retry. Use fork_turns=none. After the real spawn returns, end this parent turn; do not wait on or close the child.\n"
            + json.dumps(values, ensure_ascii=False)
        )
        self.parent_idle = False
        result = self._rpc(
            "turn/start",
            {
                "threadId": self.parent,
                "model": PARENT_MODEL,
                "effort": PARENT_EFFORT,
                "input": [{"type": "text", "text": prompt}],
            },
        )
        self.parent_turn = result.get("turn", {}).get("id", self.parent_turn)

    def _business_resolved(self, view):
        if not self.current:
            return True
        child = self.current.get("child")
        if (
            not child
            or not self.children[child["child_id"]]["terminal"]
            or not self.parent_idle
        ):
            return False
        summary = (
            view.get("intents", {})
            .get(self.current["worker"]["request_id"], {})
            .get("claimbinding_summary")
            or {}
        )
        if summary.get("task_id") != self.current["worker"][
            "task_id"
        ] or not summary.get("claim_id"):
            return False
        if self.current.get("last_business_summary") != summary:
            self.journal.record(
                "business_observation",
                {
                    "request_id": self.current["worker"]["request_id"],
                    **{
                        key: summary.get(key)
                        for key in (
                            "claim_id",
                            "task_id",
                            "current_attempt",
                            "task_status",
                            "accepted",
                            "accepted_result_hash",
                            "consumed",
                            "consumed_result_hash",
                        )
                    },
                },
            )
            safe_summary = {
                key: summary.get(key)
                for key in (
                    "claim_id",
                    "task_id",
                    "current_attempt",
                    "task_status",
                    "accepted",
                    "accepted_result_hash",
                    "consumed",
                    "consumed_result_hash",
                )
            }
            self.current["last_business_summary"] = dict(summary)
            self.journal.state["intents"][self.current["worker"]["request_id"]][
                "business"
            ] = safe_summary
            self.journal.save()
        if summary.get("accepted") is True:
            return bool(
                summary.get("consumed") is True
                and summary.get("accepted_result_hash")
                and summary.get("accepted_result_hash")
                == summary.get("consumed_result_hash")
            )
        return bool(
            summary.get("claim_id")
            and summary.get("task_status") in {"FAILED", "CANCELLED", "PENDING"}
        )

    def _plan(self, view):
        plan = self.planner(self.run_id)
        if (
            not isinstance(plan, dict)
            or plan.get("contract_version") != 2
            or plan.get("run_id") != self.run_id
            or plan.get("run_generation") != self.generation
            or plan.get("content_id") != self.content_id
        ):
            raise ConductorError("CONDUCTOR_PLAN_SCOPE_INVALID")
        status = plan.get("status")
        workers = plan.get("worker_requests")
        if status == "READY" and isinstance(workers, list) and 1 <= len(workers) <= 16:
            for worker in workers:
                validate_worker(worker, view, self.run_id, self.content_id)
                if (
                    worker["request_id"] in {item["request_id"] for item in self.inbox}
                    or worker["request_id"] in self.journal.state["intents"]
                ):
                    raise ConductorError("CONDUCTOR_DUPLICATE_OFFER")
                self.inbox.append(worker)
        elif status in {"NO_TASK", "WAITING_UPSTREAM"} and workers == []:
            if (
                status == "NO_TASK"
                and not self.current
                and not self.inbox
                and not view.get("active_task_ids")
            ):
                self._stop("CONDUCTOR_NO_TASK")
        elif status in {"BLOCKED", "DISABLED", "HANDOFF_ERROR"}:
            self._stop(f"CONDUCTOR_PLAN_{status}")
        else:
            raise ConductorError("CONDUCTOR_PLAN_INVALID")

    def run(self):
        try:
            view = self.backend.view(self.content_id)
            require_fresh_run(view, self.run_id)
            self.generation = view["generation"]
            self._rpc(
                "initialize",
                {
                    "clientInfo": {"name": "mpt_content_conductor", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.native.send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            result = self._rpc(
                "thread/start",
                {
                    "model": PARENT_MODEL,
                    "cwd": str(self.native.workspace),
                    "ephemeral": True,
                    "developerInstructions": "This parent is a spawn-only MPT Conductor. Only call native spawn once per issued turn with exact supplied parameters and fork_turns=none; do not inspect or execute business work, continue other goals, or create substitute chats.",
                },
            )
            self.parent = result["thread"]["id"]
            if result["thread"].get("ephemeral") is not True:
                raise ConductorError("CONDUCTOR_PARENT_NOT_EPHEMERAL")
            self.journal.state.update(
                phase="ATTACH_SEND",
                parent_thread_id=self.parent,
                process_identity=self.native.identity,
                generation=self.generation,
            )
            self.journal.save()
            attached = self.backend.attach(
                self.run_id,
                {
                    "generation": self.generation,
                    "expected_host_epoch": view.get("host_epoch", 0),
                    "parent_thread_id": self.parent,
                    "process_identity": self.native.identity,
                },
            )
            if (
                attached.get("run_id") != self.run_id
                or attached.get("generation") != self.generation
                or attached.get("parent_thread_id") != self.parent
                or type(attached.get("host_epoch")) is not int
                or attached["host_epoch"] <= view.get("host_epoch", 0)
            ):
                raise ConductorError("CONDUCTOR_HOST_ATTACH_UNCERTAIN")
            self.epoch = attached["host_epoch"]
            self.journal.state.update(phase="RUNNING", host_epoch=self.epoch)
            self.journal.save()
            next_plan = self.monotonic()
            while True:
                view = self.backend.view(self.content_id)
                if (
                    view.get("run_id") != self.run_id
                    or view.get("generation") != self.generation
                    or view.get("host_epoch") != self.epoch
                ):
                    raise ConductorError("CONDUCTOR_RUN_GENERATION_CHANGED")
                if view.get("stop_requested") or view.get("state") in {
                    "draining",
                    "stopped",
                    "expired",
                }:
                    self.stop_reason = self.stop_reason or "CONDUCTOR_STOP_REQUESTED"
                expires = datetime.fromisoformat(view["expires_at"]).timestamp()
                if self.clock() >= expires and not self.stop_reason:
                    self._stop("CONDUCTOR_RUN_EXPIRED")
                all_terminal = all(
                    child["terminal"] for child in self.children.values()
                )
                business_resolved = self._business_resolved(view)
                if (
                    view.get("content_status")
                    in {"PUBLISHED", "ARCHIVED", "REJECTED", "FAILED"}
                    and not self.stop_reason
                ):
                    self._stop("CONDUCTOR_CONTENT_TERMINAL")
                if (
                    self.stop_reason
                    and all_terminal
                    and self.parent_idle
                    and not view.get("active_task_ids")
                ):
                    break
                if not self.stop_reason:
                    if (
                        self.current
                        and self.current.get("child")
                        and all_terminal
                        and self.parent_idle
                    ):
                        intent = view.get("intents", {}).get(
                            self.current["worker"]["request_id"], {}
                        )
                        if intent.get("state") == "TERMINAL" and not (
                            intent.get("claimbinding_summary") or {}
                        ).get("claim_id"):
                            self._stop("CONDUCTOR_HANDOFF_NOT_CLAIMED")
                            continue
                    if business_resolved:
                        self.current = None
                    if self.current is None and self.parent_idle and self.inbox:
                        self._spawn(self.inbox.popleft(), view)
                    elif self.monotonic() >= next_plan and (
                        self.current is None
                        or (
                            self.current.get("child")
                            and all_terminal
                            and self.parent_idle
                        )
                    ):
                        if not self.inbox and view.get("ready_rounds", 0) >= MAX_ROUNDS:
                            self._stop("CONDUCTOR_ROUND_LIMIT")
                        else:
                            self._plan(view)
                        next_plan = self.monotonic() + 30
                self._pump()
            self.journal.state["phase"] = self.stop_reason
            self.journal.save()
            return {
                "status": self.stop_reason,
                "run_id": self.run_id,
                "parent_thread_id": self.parent,
            }
        except (ConductorError, OSError, ValueError, KeyError) as exc:
            code = (
                str(exc)
                if isinstance(exc, ConductorError)
                else "CONDUCTOR_PROTOCOL_OR_IO_ERROR"
            )
            if (
                code == "CONDUCTOR_BACKEND_UNREACHABLE"
                and isinstance(self.backend, HttpBackend)
                and self.backend.last_request_failure is not None
            ):
                self.journal.record(
                    "backend_request_failure", self.backend.last_request_failure
                )
            if self.epoch is not None:
                try:
                    request_id = (
                        self.current["worker"]["request_id"] if self.current else "host"
                    )
                    outcome = "OBSERVATION_LOST" if self.native.eof else "UNKNOWN"
                    self._observe(
                        outcome,
                        request_id,
                        self.current.get("child") if self.current else None,
                    )
                except (ConductorError, OSError, ValueError):
                    pass
            if self.generation is not None:
                try:
                    reconciled = self.backend.view(self.content_id)
                    self.journal.record(
                        "backend_reconcile",
                        {
                            "same_run": reconciled.get("run_id") == self.run_id,
                            "run_state": reconciled.get("state"),
                            "host_epoch": reconciled.get("host_epoch"),
                        },
                    )
                    self.backend.stop(self.content_id, self._own_binding(reconciled))
                except (ConductorError, OSError, ValueError):
                    pass
            self.journal.state.update(phase=code, unresolved=True)
            self.journal.save()
            raise ConductorError(code) from None
        finally:
            known_drained = (
                all(child["terminal"] for child in self.children.values())
                and self.parent_idle
                and (not self.current or bool(self.current.get("started")))
            )
            if known_drained or not self.native.process_alive():
                self.native.close()
            else:
                self.journal.record(
                    "cleanup_deferred",
                    {
                        "pid": self.native.identity["pid"],
                        "last_observed_process_alive": True,
                        "actively_terminated": False,
                        "reason": "UNOBSERVED_OR_LIVE_CHILD",
                    },
                )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace", type=Path, default=Path(__file__).resolve().parents[2]
    )
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--runtime-private", type=Path, required=True)
    parser.add_argument("--scheduler-launcher", type=Path, required=True)
    parser.add_argument("--scheduler-id", default="mpt-codex")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--content-id", required=True)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"run_[0-9a-f]{32}", args.run_id) or not re.fullmatch(
        r"content_[0-9a-f]{32}", args.content_id
    ):
        parser.error("Invalid run/content identity")
    workspace = args.workspace.resolve()
    state_dir = args.runtime_private.parent / "content-runs" / args.run_id
    journal = backend = None
    try:
        with content_operation_lock(
            SimpleNamespace(data_dir=args.runtime_private.parent),
            f"conductor:{args.run_id}",
            timeout_seconds=0,
        ):
            backend = HttpBackend(args.api_url, args.runtime_private, workspace)
            require_fresh_run(backend.view(args.content_id), args.run_id)
            planner = SchedulerPlanner(
                workspace, args.scheduler_launcher, args.scheduler_id
            )
            journal = Journal(state_dir, args.run_id, args.content_id)
            binary = resolve_native_desktop_binary()
            native = NativeTransport(binary, workspace)
            native.workspace = workspace
            result = Conductor(
                backend, planner, native, journal, args.run_id, args.content_id
            ).run()
            print(json.dumps(result))
            return 0
    except (
        ConductorError,
        NativeDesktopResolutionError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": str(exc)
                    if isinstance(exc, ConductorError)
                    else "CONDUCTOR_ALREADY_RUNNING"
                    if isinstance(exc, TimeoutError)
                    else "CONDUCTOR_STARTUP_FAILED",
                    "state_dir": str(state_dir),
                }
            )
        )
        return 20
    finally:
        if journal:
            journal.close()
        if backend:
            backend.close()


if __name__ == "__main__":
    raise SystemExit(main())
