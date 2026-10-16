"""Protocol fault injection: no desktop, business service or media side effects."""

import copy
import json
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from glance_core.task_service import stable_hash

from app.business import conductor as c
from app.business.content_run import FROZEN_FIELDS

RUN = "run_" + "a" * 32
CONTENT = "content_" + "b" * 32


def worker(index=0):
    data = dict.fromkeys(FROZEN_FIELDS, "fixture")
    data.update(
        request_id=f"mpt-codex:{index}:abcdef{index}",
        task_id=f"task_{index}",
        run_id=RUN,
        content_id=CONTENT,
        run_generation=1,
        host_epoch=1,
        title=f"mpt_task_{index}",
        prompt="PRIVATE WORKER PROMPT 原样",
        native_agent_type="default",
        fork_turns="none",
        model="gpt-6-luna",
        reasoning_effort="max",
        round_id="round_fixture",
    )
    data["offer_hash"] = stable_hash({key: data[key] for key in FROZEN_FIELDS})
    data["_offer_deadline"] = 10**12
    return data


class FakeBackend:
    def __init__(self):
        self.events = []
        self.state = dict(
            run_id=RUN,
            generation=1,
            host_epoch=0,
            state="awaiting_host",
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=45)).isoformat(),
            content_status="WAITING_AGENT",
            intents={},
            ready_rounds=0,
            active_task_ids=[],
        )

    def view(self, content_id):
        assert content_id == CONTENT
        return copy.deepcopy(self.state)

    def attach(self, run_id, body):
        assert run_id == RUN and body["expected_host_epoch"] == 0
        self.events.append(("attach", body))
        self.state.update(
            host_epoch=1,
            state="running",
            parent_thread_id=body["parent_thread_id"],
            process_identity=copy.deepcopy(body["process_identity"]),
        )
        return dict(
            run_id=RUN,
            generation=1,
            parent_thread_id=body["parent_thread_id"],
            host_epoch=1,
        )

    def intent(self, run_id, request_id, body):
        self.events.append(("intent", request_id, body))
        self.state["intents"][request_id] = dict(state="SPAWN_INTENT")
        return dict(
            request_id=request_id,
            state="SPAWN_INTENT",
            offer_hash=body["offer_hash"],
            host_epoch=1,
        )

    def observe(self, run_id, request_id, body):
        assert body["generation"] == body["host_epoch"] == 1
        self.events.append((body["outcome"], request_id, copy.deepcopy(body)))
        return {}

    def stop(self, content_id, expected_binding):
        assert all(
            self.state.get(key) == value for key, value in expected_binding.items()
        )
        self.events.append(("stop", content_id, copy.deepcopy(expected_binding)))
        self.state.update(state="draining", stop_requested=True, stop_reason="operator_stop")
        return {}


class FakeNative:
    def __init__(self, backend, fault=None):
        self.backend, self.fault = backend, fault
        self.workspace = Path.cwd()
        self.identity = dict(
            pid=123,
            started_at="2026-10-13T00:00:00+00:00",
            instance_id="fixture",
            binary_path="fixture.exe",
            binary_sha256="a" * 64,
            version="0.162",
        )
        self.queue = deque()
        self.sent = []
        self.eof = self.closed = False
        self.counter = self.turn_count = 0
        self.after_empty = None

    def request(self, method, params):
        self.counter += 1
        rpc = f"rpc_{self.counter}"
        self.sent.append((method, params))
        if method == "initialize":
            self.queue.append(dict(id=rpc, result={}))
        elif method == "thread/start":
            assert params["ephemeral"] is True
            self.queue.append(
                dict(id=rpc, result=dict(thread=dict(id="parent", ephemeral=True)))
            )
        elif method == "turn/start":
            self.turn_count += 1
            values = json.loads(params["input"][0]["text"].split("\n", 1)[1])
            request_id = list(self.backend.state["intents"])[-1]
            assert any(
                e[0] == "intent" and e[1] == request_id for e in self.backend.events
            )
            turn_id = f"turn_{self.turn_count}"
            child_id = f"child_{self.turn_count}"
            self.queue.extend(
                [
                    dict(id=rpc, result=dict(turn=dict(id=turn_id))),
                    dict(
                        method="turn/started",
                        params=dict(threadId="parent", turn=dict(id=turn_id)),
                    ),
                ]
            )
            if self.fault == "missing_child":
                self.queue.append(
                    dict(method="turn/completed", params=dict(threadId="parent"))
                )
            elif self.fault == "eof":
                self.queue.append("EOF")
            elif self.fault == "callback":
                self.queue.append(
                    dict(
                        method="item/commandExecution/requestApproval",
                        id="approve",
                        params={},
                    )
                )
                self.queue.append(
                    dict(method="turn/completed", params=dict(threadId="parent"))
                )
            else:
                path = "/root/" + (
                    "wrong" if self.fault == "path" else values["task_name"]
                )
                self.queue.append(
                    dict(
                        method="item/started",
                        params=dict(
                            threadId="parent",
                            turnId=turn_id,
                            item=dict(
                                id="call_" + child_id,
                                type="subAgentActivity",
                                kind="started",
                                agentPath=path,
                                agentThreadId=child_id,
                            ),
                        ),
                    )
                )
                self.values = values
        elif method == "thread/read":
            child_id = params["threadId"]
            self.queue.append(
                dict(
                    id=rpc,
                    result=dict(
                        thread=dict(
                            id=child_id,
                            parentThreadId="parent",
                            agentRole="default",
                            model="wrong"
                            if self.fault == "model"
                            else self.values["model"],
                            reasoningEffort=self.values["reasoning_effort"],
                            ephemeral=True,
                        )
                    ),
                )
            )
            self.queue.extend(
                [
                    child_turn_event("turn_" + child_id, child_id=child_id),
                    child_turn_event("turn_" + child_id, "completed", child_id),
                    dict(method="turn/completed", params=dict(threadId="parent")),
                ]
            )
        return rpc

    def send(self, message):
        self.sent.append(("send", message))

    def receive(self, timeout):
        assert 0 < timeout <= 30
        if self.queue:
            value = self.queue.popleft()
            if value == "EOF":
                self.eof = True
                raise c.ConductorError("CONDUCTOR_NATIVE_OBSERVATION_LOST")
            return value
        if self.after_empty:
            self.after_empty()
        return None

    def alive(self):
        return not self.eof

    def process_alive(self):
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def setup(tmp_path):
    backend = FakeBackend()
    native = FakeNative(backend)
    journal = c.Journal(tmp_path / "state", RUN, CONTENT)
    tick = [0]

    def monotonic():
        tick[0] += 0.1
        return tick[0]

    plans = []

    def planner(run_id):
        assert run_id == RUN
        plans.append(run_id)
        return dict(
            contract_version=2,
            status="READY",
            run_id=RUN,
            run_generation=1,
            content_id=CONTENT,
            worker_requests=[worker()],
        )

    runner = c.Conductor(
        backend, planner, native, journal, RUN, CONTENT, monotonic=monotonic
    )
    yield runner, backend, native, journal, plans
    journal.close()


def finish(backend, runner):
    if (
        runner.current
        and runner.current.get("child")
        and runner.children[runner.current["child"]["child_id"]]["terminal"]
    ):
        w = runner.current["worker"]
        backend.state["intents"][w["request_id"]]["claimbinding_summary"] = dict(
            claim_id="claim",
            task_id=w["task_id"],
            task_status="COMPLETED",
            accepted=True,
            consumed=True,
            accepted_result_hash="a" * 64,
            consumed_result_hash="a" * 64,
        )
        backend.state["content_status"] = "PUBLISHED"


def test_real_event_binding_precedes_verified_and_exact_dispatch(setup):
    runner, backend, native, journal, plans = setup
    native.after_empty = lambda: finish(backend, runner)
    result = runner.run()
    assert result["status"] == "CONDUCTOR_CONTENT_TERMINAL"
    assert native.closed and native.turn_count == 1
    verified = next(e for e in backend.events if e[0] == "VERIFIED")
    child = verified[2]["child"]
    assert child["started"]["agent_path"] == "/root/mpt_task_0"
    assert child["metadata_rpc_id"].startswith("rpc_")
    assert child["model"] == "gpt-6-luna" and child["reasoning_effort"] == "max"
    assert child["issued_prompt_sha256"] == c.prompt_sha256(worker()["prompt"])
    assert native.values == dict(
        task_name="mpt_task_0",
        agent_type="default",
        model="gpt-6-luna",
        reasoning_effort="max",
        fork_turns="none",
        message=worker()["prompt"],
    )
    assert [e[0] for e in backend.events].index("intent") < [
        e[0] for e in backend.events
    ].index("VERIFIED")
    assert len(plans) == 1
    assert (
        "PRIVATE WORKER PROMPT"
        not in journal.path.read_text() + journal.log_path.read_text()
    )
    assert "VERIFIED" in journal.log_path.read_text()


@pytest.mark.parametrize(
    "fault,code",
    [
        ("model", "CONDUCTOR_CHILD_METADATA_MISMATCH"),
        ("missing_child", "CONDUCTOR_SPAWN_NOT_OBSERVED"),
        ("callback", "HOST_APPROVAL_REQUIRED"),
    ],
)
def test_rejection_closes_new_admissions_and_does_not_respawn(setup, fault, code):
    runner, backend, native, _, _ = setup
    native.fault = fault
    result = runner.run()
    assert result["status"] == code and native.turn_count == 1
    assert not any(e[0] == "VERIFIED" for e in backend.events)
    if fault == "callback":
        reply = next(
            p for method, p in native.sent if method == "send" and "error" in p
        )
        assert "result" not in reply and reply["id"] == "approve"


def test_eof_keeps_unknown_live_own_process_unterminated(setup):
    runner, backend, native, journal, _ = setup
    native.fault = "eof"
    with pytest.raises(c.ConductorError, match="OBSERVATION_LOST"):
        runner.run()
    assert native.turn_count == 1 and not native.closed
    assert any(e[0] == "OBSERVATION_LOST" for e in backend.events)
    assert journal.state["unresolved"] is True
    log = journal.log_path.read_text()
    assert '"actively_terminated": false' in log


def bind_fixture(runner, journal):
    w = worker()
    runner.generation = runner.epoch = 1
    runner.parent, runner.parent_turn = "parent", "turn_1"
    runner.backend.state.update(
        host_epoch=1,
        state="running",
        parent_thread_id="parent",
        process_identity=copy.deepcopy(runner.native.identity),
    )
    runner.current = dict(worker=w)
    journal.state["intents"][w["request_id"]] = {}
    return w


def child_turn_event(turn_id="child_turn", status="inProgress", child_id="child"):
    return dict(
        method="turn/started" if status == "inProgress" else "turn/completed",
        params=dict(threadId=child_id, turn=dict(id=turn_id, status=status, items=[])),
    )


@pytest.fixture
def interrupt_context(setup):
    runner, backend, native, journal, plans = setup
    w = bind_fixture(runner, journal)
    started = dict(parent_thread_id="parent", parent_turn_id="turn_1", item_id="spawn",
                   agent_path="/root/" + w["title"], child_id="child")
    child = dict(child_id="child", parent_thread_id="parent", native_agent_type="default",
                 model=w["model"], reasoning_effort=w["reasoning_effort"], ephemeral=True,
                 started=started, metadata_rpc_id="verified-rpc",
                 issued_prompt_sha256=c.prompt_sha256(w["prompt"]))
    runner.current.update(started=started, child=child, admitted=True)
    runner.stop_reason = "CONDUCTOR_STOP_REQUESTED"
    runner.children["child"] = dict(terminal=False)
    backend.state.update(
        stop_requested=True, stop_reason="operator_stop", state="draining",
        offer_children={w["request_id"]: "child"},
    )
    backend.state["intents"][w["request_id"]] = dict(
        state="CLAIMED", host_epoch=1, offer_hash=w["offer_hash"], child=copy.deepcopy(child),
        claim_id="original-claim",
        claimbinding_summary=dict(claim_id="original-claim", task_id=w["task_id"],
                                  task_status="PENDING", current_attempt=1,
                                  accepted=False, consumed=False),
    )
    native.queue.append(child_turn_event())
    runner._pump()
    return runner, backend, native, journal, plans


def test_child_turn_identity_is_captured_only_from_live_native_event(interrupt_context):
    runner, _, _, _, _ = interrupt_context
    assert runner.children["child"]["turn_id"] == "child_turn"
    assert not runner.children["child"]["terminal"]


def test_operator_stop_interrupts_verified_pending_original_claim_once(interrupt_context):
    runner, backend, native, journal, plans = interrupt_context
    for _ in range(3):
        runner._interrupt_pending_child(backend.view(CONTENT))
    assert [(method, params) for method, params in native.sent if method == "turn/interrupt"] == [
        ("turn/interrupt", dict(threadId="child", turnId="child_turn")),
    ]
    assert not native.closed and native.turn_count == 0 and plans == []
    assert not runner.children["child"]["terminal"]
    assert "interrupt_send_intent" in journal.log_path.read_text()


@pytest.mark.parametrize("origin", ["server-expired", "local-expired", "local-machine-stop"])
def test_expired_run_interrupts_pending_bound_child_once(interrupt_context, origin):
    runner, backend, native, journal, _ = interrupt_context
    deadline = datetime.now(timezone.utc)
    runner.clock = lambda: deadline.timestamp()
    backend.state.update(expires_at=deadline.isoformat(), stop_reason="expired")
    if origin != "server-expired":
        runner.stop_reason = "CONDUCTOR_RUN_EXPIRED"
    if origin == "local-machine-stop":
        backend.state["stop_reason"] = "operator_stop"
    for _ in range(2):
        runner._interrupt_pending_child(backend.view(CONTENT))
    # Changing the stop classification cannot buy another send for the same turn.
    runner.stop_reason = "CONDUCTOR_STOP_REQUESTED"
    backend.state["stop_reason"] = "operator_stop"
    runner._interrupt_pending_child(backend.view(CONTENT))
    assert [p for method, p in native.sent if method == "turn/interrupt"] == [
        dict(threadId="child", turnId="child_turn"),
    ]
    record = journal.state["intents"][worker()["request_id"]]["interrupts"]["child_turn"]
    assert record["reason"] == "RUN_EXPIRED_LOST_LEASE"
    assert record["run_expires_at"] == deadline.isoformat()
    assert record["expiry_observed_at"] >= deadline.timestamp()
    assert not runner.children["child"]["terminal"] and not native.closed


@pytest.mark.parametrize("origin", ["server-expired", "local-machine-stop"])
@pytest.mark.parametrize("weak", [
    "future", "naive", "invalid", "missing", "null", "date-only", "leased",
    "claim", "turn", "internal-stop", "unknown-local-stop", "unknown-server-stop",
])
def test_expiry_cancellation_requires_real_deadline_and_original_binding(
    interrupt_context, origin, weak,
):
    runner, backend, native, journal, _ = interrupt_context
    now = datetime.now(timezone.utc)
    runner.clock = lambda: now.timestamp()
    backend.state.update(expires_at=now.isoformat(), stop_reason="expired")
    if origin == "local-machine-stop":
        runner.stop_reason = "CONDUCTOR_RUN_EXPIRED"
        backend.state["stop_reason"] = "operator_stop"
    if weak == "future":
        backend.state["expires_at"] = (now + timedelta(microseconds=1)).isoformat()
    elif weak == "naive":
        backend.state["expires_at"] = (now - timedelta(seconds=1)).replace(tzinfo=None).isoformat()
    elif weak == "invalid":
        backend.state["expires_at"] = "not-a-deadline"
    elif weak == "missing":
        backend.state.pop("expires_at")
    elif weak == "null":
        backend.state["expires_at"] = None
    elif weak == "date-only":
        backend.state["expires_at"] = now.date().isoformat()
    elif weak in {"leased", "claim"}:
        summary = backend.state["intents"][worker()["request_id"]]["claimbinding_summary"]
        summary["task_status" if weak == "leased" else "claim_id"] = "LEASED" if weak == "leased" else "another-claim"
    elif weak == "turn":
        runner.children["child"].pop("turn_id")
    elif weak == "internal-stop":
        runner.stop_reason = "CONDUCTOR_CONTENT_TERMINAL"
    elif weak == "unknown-local-stop":
        runner.stop_reason = "UNKNOWN"
    elif weak == "unknown-server-stop":
        backend.state["stop_reason"] = "unknown"
    runner._interrupt_pending_child(backend.view(CONTENT))
    assert not any(method == "turn/interrupt" for method, _ in native.sent)
    assert not native.closed and not runner.children["child"]["terminal"]
    assert "interrupt_send_intent" not in journal.log_path.read_text()


def test_old_child_turn_completion_does_not_finish_new_active_turn(interrupt_context):
    runner, backend, native, _, _ = interrupt_context
    native.queue.append(child_turn_event("new_child_turn"))
    runner._pump()
    native.queue.append(child_turn_event(status="completed"))
    runner._pump()
    assert not runner.children["child"]["terminal"]
    assert not any(event[0] == "TERMINAL" for event in backend.events)


@pytest.mark.parametrize("status", ["completed", "interrupted", "failed"])
def test_interrupt_ack_waits_for_same_turn_real_terminal(interrupt_context, status):
    runner, backend, native, _, _ = interrupt_context
    runner._interrupt_pending_child(backend.view(CONTENT))
    rpc_id = next(iter(runner.interrupt_requests))
    native.queue.append(dict(id=rpc_id, result={}))
    runner._pump()
    assert not runner.children["child"]["terminal"] and not native.closed
    assert not any(event[0] == "TERMINAL" for event in backend.events)
    invalid = child_turn_event()
    invalid["method"] = "turn/completed"
    native.queue.extend([invalid, child_turn_event("parent_turn", "completed", "parent")])
    runner._pump()
    runner._pump()
    assert not runner.children["child"]["terminal"]
    runner.last_heartbeat = -100
    runner._pump()
    assert any(event[0] == "HEARTBEAT" for event in backend.events)
    native.queue.append(child_turn_event(status=status))
    runner._pump()
    assert runner.children["child"]["terminal"]
    assert len([event for event in backend.events if event[0] == "TERMINAL"]) == 1
    native.queue.extend([child_turn_event(status=status), child_turn_event()])
    runner._pump()
    runner._pump()
    assert runner.children["child"]["terminal"]
    assert len([event for event in backend.events if event[0] == "TERMINAL"]) == 1


def test_new_active_turn_is_not_finished_by_old_completion_or_ack(interrupt_context):
    runner, backend, native, _, _ = interrupt_context
    runner._interrupt_pending_child(backend.view(CONTENT))
    old_rpc = next(iter(runner.interrupt_requests))
    native.queue.append(child_turn_event("new_turn"))
    runner._pump()
    runner._interrupt_pending_child(backend.view(CONTENT))
    native.queue.extend([dict(id=old_rpc, result={}), child_turn_event(status="interrupted")])
    runner._pump()
    runner._pump()
    assert not runner.children["child"]["terminal"]
    assert runner.children["child"]["turn_id"] == "new_turn"
    assert [p["turnId"] for method, p in native.sent if method == "turn/interrupt"] == ["child_turn", "new_turn"]
    native.queue.append(child_turn_event("new_turn", "interrupted"))
    runner._pump()
    assert runner.children["child"]["terminal"]
    assert len([event for event in backend.events if event[0] == "TERMINAL"]) == 1


@pytest.mark.parametrize("weak", [
    "leased", "status-missing", "status-unknown", "not-stopped", "expired-stop",
    "not-draining", "unverified", "terminal", "missing-turn", "journal-only-turn",
    "run", "generation", "epoch", "parent", "process", "intent-missing",
    "not-claimed", "claim-missing", "claim-mismatch", "task", "offer", "intent-epoch",
    "child", "child-map", "accepted", "consumed", "native-dead", "started", "internal-stop",
])
def test_interrupt_predicate_fails_closed(interrupt_context, weak):
    runner, backend, native, journal, plans = interrupt_context
    view = backend.view(CONTENT)
    intent = view["intents"][worker()["request_id"]]
    summary = intent["claimbinding_summary"]
    if weak == "leased":
        summary["task_status"] = "LEASED"
    elif weak == "status-missing":
        summary.pop("task_status")
    elif weak == "status-unknown":
        summary["task_status"] = "UNKNOWN"
    elif weak == "not-stopped":
        view["stop_requested"] = False
    elif weak == "expired-stop":
        view["stop_reason"] = "expired"
    elif weak == "not-draining":
        view["state"] = "running"
    elif weak == "unverified":
        runner.current["admitted"] = False
    elif weak == "terminal":
        runner.children["child"]["terminal"] = True
    elif weak == "missing-turn":
        runner.children["child"].pop("turn_id")
    elif weak == "journal-only-turn":
        runner.children["child"] = dict(terminal=False, turn_id="child_turn")
        journal.state["intents"][worker()["request_id"]]["child_turn_id"] = "child_turn"
    elif weak in {"run", "generation", "epoch", "parent", "process"}:
        field = dict(run="run_id", generation="generation", epoch="host_epoch",
                     parent="parent_thread_id", process="process_identity")[weak]
        view[field] = 2 if weak in {"generation", "epoch"} else "changed"
    elif weak == "intent-missing":
        view["intents"] = {}
    elif weak == "not-claimed":
        intent["state"] = "VERIFIED"
    elif weak == "claim-missing":
        intent["claim_id"] = None
    elif weak == "claim-mismatch":
        summary["claim_id"] = "another-claim"
    elif weak == "task":
        summary["task_id"] = "another-task"
    elif weak == "offer":
        intent["offer_hash"] = "changed"
    elif weak == "intent-epoch":
        intent["host_epoch"] = 2
    elif weak == "child":
        intent["child"]["child_id"] = "another-child"
    elif weak == "child-map":
        view["offer_children"][worker()["request_id"]] = "another-child"
    elif weak in {"accepted", "consumed"}:
        summary[weak] = True
    elif weak == "native-dead":
        native.eof = True
    elif weak == "started":
        runner.current["started"] = {"child_id": "another-child"}
    elif weak == "internal-stop":
        runner.stop_reason = "CONDUCTOR_CONTENT_TERMINAL"
    runner._interrupt_pending_child(view)
    assert not any(method == "turn/interrupt" for method, _ in native.sent)
    assert not native.closed and plans == []
    assert "interrupt_send_intent" not in journal.log_path.read_text()


@pytest.mark.parametrize("fault", ["send-uncertain", "rpc-error", "missing-ack", "malformed-ack"])
def test_interrupt_uncertainty_never_retries_or_fakes_terminal(
    interrupt_context, monkeypatch, fault,
):
    runner, backend, native, journal, plans = interrupt_context
    original = native.request

    def request(method, params):
        rpc = original(method, params)
        if fault == "send-uncertain":
            raise c.ConductorError("CONDUCTOR_NATIVE_SEND_UNCERTAIN")
        if fault == "rpc-error":
            native.queue.append(dict(id=rpc, error=dict(code=-32603, message="PRIVATE_NATIVE_PAYLOAD")))
        elif fault == "malformed-ack":
            native.queue.append(dict(id=rpc, result=None))
        return rpc

    monkeypatch.setattr(native, "request", request)
    runner._interrupt_pending_child(backend.view(CONTENT))
    for _ in range(3):
        runner.last_heartbeat = -100
        runner._pump()
        runner._interrupt_pending_child(backend.view(CONTENT))
    assert len([method for method, _ in native.sent if method == "turn/interrupt"]) == 1
    assert not runner.children["child"]["terminal"] and not native.closed and plans == []
    assert any(event[0] == "HEARTBEAT" for event in backend.events)
    assert not any(event[0] == "TERMINAL" for event in backend.events)
    assert "PRIVATE_NATIVE_PAYLOAD" not in journal.log_path.read_text()


@pytest.mark.parametrize("phase", ["save-before-send", "log-before-send", "save-after-send"])
def test_interrupt_audit_failure_keeps_live_identity_without_extra_send(
    interrupt_context, monkeypatch, phase,
):
    runner, backend, native, journal, _ = interrupt_context
    save, record = journal.save, journal.record
    saves = []

    def fail_save():
        saves.append(True)
        if phase == "save-before-send" or len(saves) >= 2:
            raise OSError("injected audit save failure")
        save()

    def fail_record(kind, data):
        if kind == "interrupt_send_intent":
            raise OSError("injected audit log failure")
        record(kind, data)

    if phase == "log-before-send":
        monkeypatch.setattr(journal, "record", fail_record)
    else:
        monkeypatch.setattr(journal, "save", fail_save)
    for _ in range(2):
        runner._interrupt_pending_child(backend.view(CONTENT))
    assert len([method for method, _ in native.sent if method == "turn/interrupt"]) == (1 if phase == "save-after-send" else 0)
    assert not native.closed and not runner.children["child"]["terminal"]


def test_child_turn_seen_before_metadata_does_not_authorize_interrupt(interrupt_context):
    runner, backend, native, _, _ = interrupt_context
    runner.current["admitted"] = False
    runner._interrupt_pending_child(backend.view(CONTENT))
    assert not any(method == "turn/interrupt" for method, _ in native.sent)
    child = runner.current["child"]
    runner.stop_reason = None
    runner._verify_child(child["metadata_rpc_id"], child["started"], dict(result=dict(thread=dict(
        id="child", parentThreadId="parent", agentRole="default", ephemeral=True,
        model=worker()["model"], reasoningEffort=worker()["reasoning_effort"],
    ))))
    runner.stop_reason = "CONDUCTOR_STOP_REQUESTED"
    runner._interrupt_pending_child(backend.view(CONTENT))
    assert len([method for method, _ in native.sent if method == "turn/interrupt"]) == 1


@pytest.mark.parametrize("status", ["PENDING", "LEASED"])
@pytest.mark.parametrize("stop", [
    "operator", "server-expired", "local-expired", "server-invalid-expiry", "server-naive-expiry",
])
def test_run_loop_interrupts_pending_child_but_keeps_normal_leased_drain(setup, status, stop):
    runner, backend, native, journal, plans = setup
    real_request, real_observe = native.request, backend.observe
    weak_expiry = stop in {"server-invalid-expiry", "server-naive-expiry"}
    should_interrupt = status == "PENDING" and not weak_expiry

    def observe(run_id, request_id, body):
        response = real_observe(run_id, request_id, body)
        if body["outcome"] == "VERIFIED":
            w = runner.current["worker"]
            backend.state.update(
                stop_requested=stop != "local-expired",
                stop_reason="expired" if stop.startswith("server-") else "operator_stop",
                state="running" if stop == "local-expired" else "draining",
                offer_children={request_id: body["child"]["child_id"]},
                active_task_ids=[w["task_id"]] if status == "LEASED" else [],
            )
            backend.state["intents"][request_id].update(
                state="CLAIMED", child=copy.deepcopy(body["child"]), host_epoch=1,
                offer_hash=w["offer_hash"], claim_id="original-claim",
                claimbinding_summary=dict(claim_id="original-claim", task_id=w["task_id"],
                                          task_status=status, accepted=False, consumed=False),
            )
            if weak_expiry:
                backend.state["expires_at"] = (
                    "invalid-deadline" if stop == "server-invalid-expiry"
                    else datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
                )
            elif stop != "operator":
                deadline = datetime.fromisoformat(backend.state["expires_at"]).timestamp()
                runner.clock = lambda: deadline + 1
        elif body["outcome"] == "TERMINAL":
            backend.state["intents"][request_id]["state"] = "TERMINAL"
            backend.state.update(
                state="expired" if backend.state["stop_reason"] == "expired" else "stopped",
                active_task_ids=[],
            )
        return response

    def request(method, params):
        rpc = real_request(method, params)
        if method == "thread/read":
            parent_done = native.queue.pop()
            child_done = native.queue.pop()
            native.queue.append(parent_done)

            def terminal_later():
                assert not native.closed
                calls = [p for m, p in native.sent if m == "turn/interrupt"]
                assert len(calls) == (1 if should_interrupt else 0)
                assert not runner.children[params["threadId"]]["terminal"]
                if should_interrupt:
                    child_done["params"]["turn"]["status"] = "interrupted"
                elif status == "LEASED":
                    backend.state["active_task_ids"] = []
                    summary = next(iter(backend.state["intents"].values()))["claimbinding_summary"]
                    summary.update(task_status="COMPLETED", accepted=True, consumed=True,
                                   accepted_result_hash="a" * 64, consumed_result_hash="a" * 64)
                native.queue.append(child_done)
                native.after_empty = None

            native.after_empty = terminal_later
        elif method == "turn/interrupt":
            native.queue.append(dict(id=rpc, result={}))
        return rpc

    backend.observe, native.request = observe, request
    result = runner.run()
    assert result["status"] == ("CONDUCTOR_RUN_EXPIRED" if stop == "local-expired" else "CONDUCTOR_STOP_REQUESTED")
    assert native.closed and native.turn_count == 1 and len(plans) == 1
    assert backend.state["state"] == ("expired" if stop.startswith("server-") else "stopped")
    assert len([e for e in backend.events if e[0] == "TERMINAL"]) == 1
    summary = next(iter(backend.state["intents"].values()))["claimbinding_summary"]
    assert summary["task_status"] == ("COMPLETED" if status == "LEASED" else "PENDING")
    assert summary["accepted"] == (status == "LEASED")
    if should_interrupt:
        saved = json.loads(journal.path.read_text())
        record = next(iter(next(iter(saved["intents"].values()))["interrupts"].values()))
        assert record["child_id"] == "child_1" and record["turn_id"] == "turn_child_1"
        assert record["state"] == "ACKNOWLEDGED"
        assert record["reason"] == ("OPERATOR_STOP_LOST_LEASE" if stop == "operator" else "RUN_EXPIRED_LOST_LEASE")


def test_matching_completion_before_ack_still_requires_real_terminal(interrupt_context):
    runner, backend, native, _, _ = interrupt_context
    runner._interrupt_pending_child(backend.view(CONTENT))
    rpc = next(iter(runner.interrupt_requests))
    native.queue.extend([child_turn_event(status="interrupted"), dict(id=rpc, result={})])
    runner._pump()
    assert runner.children["child"]["terminal"]
    runner._pump()
    assert runner.children["child"]["terminal"]
    assert len([e for e in backend.events if e[0] == "TERMINAL"]) == 1


@pytest.mark.parametrize("invalid", ["missing-id", "empty-id", "old-id", "missing-status", "inProgress", "unknown-status"])
def test_invalid_or_old_turn_completion_never_ends_active_child(interrupt_context, invalid):
    runner, backend, native, _, _ = interrupt_context
    message = child_turn_event(status="completed")
    turn = message["params"]["turn"]
    if invalid == "missing-id":
        turn.pop("id")
    elif invalid == "empty-id":
        turn["id"] = ""
    elif invalid == "old-id":
        turn["id"] = "old-turn"
    elif invalid == "missing-status":
        turn.pop("status")
    else:
        turn["status"] = invalid
    native.queue.append(message)
    runner._pump()
    assert not runner.children["child"]["terminal"]
    assert not any(e[0] == "TERMINAL" for e in backend.events)


def test_child_turn_before_metadata_response_is_retained_until_verification(setup):
    runner, backend, native, _, _ = setup
    real_request = native.request

    def request(method, params):
        rpc = real_request(method, params)
        if method == "thread/read":
            messages = list(native.queue)
            start = next(event for event in messages if event.get("method") == "turn/started")
            native.queue.remove(start)
            native.queue.appendleft(start)
        return rpc

    native.request = request
    native.after_empty = lambda: finish(backend, runner)
    result = runner.run()
    assert result["status"] == "CONDUCTOR_CONTENT_TERMINAL"
    assert runner.children["child_1"]["turn_id"] == "turn_child_1"
    assert runner.children["child_1"]["terminal"]
    kinds = [event[0] for event in backend.events]
    assert kinds.index("VERIFIED") < kinds.index("TERMINAL")
    assert not any(method == "turn/interrupt" for method, _ in native.sent)


def test_stop_before_metadata_does_not_forge_terminal(setup):
    runner, backend, native, journal, _ = setup
    w = bind_fixture(runner, journal)
    runner.stop_reason = "CONDUCTOR_STOP_REQUESTED"
    started = dict(
        parent_thread_id="parent",
        parent_turn_id="turn_1",
        item_id="call",
        agent_path="/root/" + w["title"],
        child_id="child",
    )
    runner.children["child"] = dict(terminal=False)
    response = dict(
        result=dict(
            thread=dict(
                id="child",
                parentThreadId="parent",
                agentRole="default",
                model=w["model"],
                reasoningEffort=w["reasoning_effort"],
                ephemeral=True,
            )
        )
    )
    runner._verify_child("rpc_read", started, response)
    assert [e[0] for e in backend.events] == ["REJECTED"]
    assert not runner.children["child"]["terminal"]


def test_parent_turn_mismatch_and_extra_child_are_rejected(setup):
    runner, backend, _, journal, _ = setup
    w = bind_fixture(runner, journal)
    runner._child_started(
        dict(threadId="parent", turnId="wrong"),
        dict(id="call", agentThreadId="child", agentPath="/root/" + w["title"]),
    )
    assert runner.stop_reason == "CONDUCTOR_UNEXPECTED_CHILD"
    assert not runner.metadata and not any(e[0] == "VERIFIED" for e in backend.events)


def test_wait_timeout_does_not_mark_child_terminal_or_respawn(setup):
    runner, _, native, journal, _ = setup
    bind_fixture(runner, journal)
    runner.children["child"] = dict(terminal=False)
    for _ in range(20):
        runner._pump(0.01)
    assert runner.children["child"] == dict(terminal=False)
    assert native.turn_count == 0


def test_accepted_is_not_consumed_and_task_binding_must_match(setup):
    runner, _, _, journal, _ = setup
    w = bind_fixture(runner, journal)
    runner.current["child"] = dict(child_id="child")
    runner.children["child"] = dict(terminal=True)
    summary = dict(
        claim_id="claim",
        task_id=w["task_id"],
        accepted=True,
        consumed=False,
        accepted_result_hash="hash",
        consumed_result_hash="hash",
    )
    view = dict(intents={w["request_id"]: dict(claimbinding_summary=summary)})
    assert not runner._business_resolved(view)
    summary["consumed"] = True
    assert runner._business_resolved(view)
    summary.update(consumed=False, task_status="PENDING")
    assert not runner._business_resolved(view)
    summary.update(consumed=True, task_status="COMPLETED")
    summary["task_id"] = "other"
    assert not runner._business_resolved(view)
    summary.update(task_id=w["task_id"], consumed_result_hash="different")
    assert not runner._business_resolved(view)


def test_round_batch_retained_no_replan_or_duplicate_intent(setup):
    runner, backend, _, journal, _ = setup
    runner.generation = runner.epoch = 1
    runner.planner = lambda run_id: dict(
        contract_version=2,
        status="READY",
        run_id=RUN,
        run_generation=1,
        content_id=CONTENT,
        worker_requests=[worker(0), worker(1)],
    )
    runner._plan(dict(generation=1, host_epoch=1))
    assert [w["title"] for w in runner.inbox] == ["mpt_task_0", "mpt_task_1"]
    with pytest.raises(c.ConductorError, match="DUPLICATE_OFFER"):
        runner._plan(dict(generation=1, host_epoch=1))
    assert len(runner.inbox) == 2
    assert backend.events == [] and journal.state["intents"] == {}


def test_journal_restart_refuses_replay_and_redacts_raw_payload(tmp_path):
    path = tmp_path / "private"
    journal = c.Journal(path, RUN, CONTENT)
    message = dict(
        method="item/completed",
        params=dict(
            threadId="safe",
            item=dict(
                type="mcpToolCall",
                output="PRIVATE DOM phone 12345",
                arguments=dict(token="SECRET"),
            ),
        ),
    )
    journal.record("native", c.event_summary(message))
    journal.close()
    assert (
        "PRIVATE DOM" not in journal.log_path.read_text()
        and "SECRET" not in journal.log_path.read_text()
    )
    with pytest.raises(c.ConductorError, match="RECOVERY_REQUIRED"):
        c.Journal(path, RUN, CONTENT)


@pytest.mark.parametrize(
    "field,value",
    [
        ("prompt", "changed"),
        ("host_epoch", True),
        ("fork_turns", "all"),
        ("model", "gemini"),
        ("content_id", "other"),
    ],
)
def test_offer_tampering_fails_before_spawn(field, value):
    w = worker()
    w[field] = value
    with pytest.raises(c.ConductorError):
        c.validate_worker(w, dict(generation=1, host_epoch=1), RUN, CONTENT)


def test_http_exact_protocol_and_token_only_in_memory(tmp_path):
    private = tmp_path / "storage" / "private" / "runtime.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(dict(admin_token="secret-token")))
    backend = c.HttpBackend("http://127.0.0.1:8000", private, tmp_path)
    calls = []

    def respond(request):
        assert request.headers["Authorization"] == "Bearer secret-token"
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    backend.client.close()
    backend.client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer secret-token"},
        transport=httpx.MockTransport(respond),
    )
    backend.view(CONTENT)
    backend.attach(RUN, {})
    backend.intent(RUN, "request", {})
    backend.observe(RUN, "request", {})
    backend.stop(CONTENT, dict(run_id=RUN))
    assert calls == [
        ("GET", f"/api/v1/contents/{CONTENT}/run"),
        ("POST", f"/api/v1/content-runs/{RUN}/host/attach"),
        ("PUT", f"/api/v1/content-runs/{RUN}/intents/request"),
        ("POST", f"/api/v1/content-runs/{RUN}/intents/request/observation"),
        ("POST", f"/api/v1/contents/{CONTENT}/run/stop"),
    ]
    backend.close()
    assert list(private.parent.iterdir()) == [private]


@pytest.mark.parametrize(
    "url",
    [
        "http://evil:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1:8000/",
        "http://127.0.0.1:65536",
    ],
)
def test_non_loopback_or_noncanonical_api_rejected_before_credentials(tmp_path, url):
    with pytest.raises(c.ConductorError, match="API_URL_INVALID"):
        c.HttpBackend(url, tmp_path / "absent", tmp_path)


def test_scheduler_uses_ps7_argument_array_not_service_command_text(
    tmp_path, monkeypatch
):
    root = tmp_path / "storage" / "agent-launcher-staging"
    root.mkdir(parents=True)
    launcher = root / "mpt-codex.ps1"
    launcher.write_text("fixture")
    launcher.with_suffix(".json").write_text(
        json.dumps(
            dict(
                workspace=str(tmp_path),
                scheduler_id="mpt-codex",
                agent_id="mpt-codex",
                role_id="scheduler",
            )
        )
    )
    plan = dict(worker_requests=[], command="evil command")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(plan))

    monkeypatch.setattr(c.subprocess, "run", run)
    assert c.SchedulerPlanner(tmp_path, launcher, "mpt-codex")(RUN) == plan
    argv, kwargs = calls[0]
    assert argv[0] == str(c.POWERSHELL_EXECUTABLE) and argv[-2:] == ["--run-id", RUN]
    assert (
        "evil command" not in argv
        and kwargs["shell"] is False
        and kwargs["timeout"] == 30
    )


def test_native_transport_inherits_security_configuration(tmp_path, monkeypatch):
    import io

    calls = []
    process = SimpleNamespace(
        pid=111,
        stdin=io.StringIO(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        poll=lambda: 0,
    )

    def popen(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    monkeypatch.setattr(c.subprocess, "Popen", popen)
    transport = c.NativeTransport(
        c.NativeDesktopBinary(tmp_path / "codex.exe", "a" * 64, "0.162"), tmp_path
    )
    assert calls[0][0][1:] == [
        "app-server",
        "--stdio",
        "-c",
        "features.multi_agent=true",
    ]
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["creationflags"] == getattr(c.subprocess, "CREATE_NO_WINDOW", 0)
    assert not transport.process_alive()


def test_parent_response_lost_persists_uncertain_without_retry(setup):
    runner, backend, native, journal, _ = setup
    original = native.request

    def request(method, params):
        rpc = original(method, params)
        if method == "turn/start":
            native.queue.clear()
        return rpc

    native.request = request
    with pytest.raises(c.ConductorError, match="RESPONSE_UNCERTAIN"):
        runner.run()
    assert native.turn_count == 1 and not native.closed
    assert (
        journal.state["intents"][worker()["request_id"]]["state"] == "SPAWN_SEND_INTENT"
    )
    assert any(e[0] == "UNKNOWN" for e in backend.events)
    assert any(e[0] == "stop" for e in backend.events)


def test_intent_commit_failure_never_sends_native_turn(setup):
    runner, backend, native, _, _ = setup

    def intent(*args):
        raise c.ConductorError("CONDUCTOR_BACKEND_HTTP_409")

    backend.intent = intent
    with pytest.raises(c.ConductorError, match="HTTP_409"):
        runner.run()
    assert native.turn_count == 0
    assert any(e[0] == "stop" for e in backend.events)


def test_stop_drains_same_child_before_closing_process(setup):
    runner, backend, native, _, plans = setup
    original = native.request

    def request(method, params):
        rpc = original(method, params)
        if method == "thread/read":
            terminal_child = native.queue.pop()
            terminal_parent = native.queue.pop()
            backend.state.update(stop_requested=True, state="draining")

            def later():
                assert not native.closed
                assert runner.stop_reason == "CONDUCTOR_STOP_REQUESTED"
                assert not runner.children[params["threadId"]]["terminal"]
                native.queue.extend([terminal_parent, terminal_child])
                native.after_empty = None

            native.after_empty = later
        return rpc

    native.request = request
    result = runner.run()
    assert result["status"] == "CONDUCTOR_STOP_REQUESTED"
    assert native.closed and native.turn_count == 1 and len(plans) == 1
    assert not any(e[0] == "VERIFIED" for e in backend.events)
    assert any(e[0] == "TERMINAL" for e in backend.events)


def test_serial_runner_preserves_two_offers_in_one_ready_batch(setup):
    runner, backend, native, _, plans = setup

    def planner(run_id):
        plans.append(run_id)
        return dict(
            contract_version=2,
            status="READY",
            run_id=RUN,
            run_generation=1,
            content_id=CONTENT,
            worker_requests=[worker(0), worker(1)],
        )

    runner.planner = planner

    def consume():
        if not runner.current or not runner.current.get("child"):
            return
        finish(backend, runner)
        if native.turn_count < 2:
            backend.state["content_status"] = "WAITING_AGENT"

    native.after_empty = consume
    runner.run()
    assert native.turn_count == 2 and len(plans) == 1
    assert [e[1] for e in backend.events if e[0] == "intent"] == [
        worker(0)["request_id"],
        worker(1)["request_id"],
    ]


def test_sixteen_server_rounds_stops_new_offers(setup):
    runner, backend, native, _, plans = setup
    original = backend.attach

    def attach(*args):
        result = original(*args)
        backend.state["ready_rounds"] = 16
        return result

    backend.attach = attach
    result = runner.run()
    assert result["status"] == "CONDUCTOR_ROUND_LIMIT"
    assert native.turn_count == 0 and plans == [] and native.closed


def test_expired_run_stops_without_new_worker(setup):
    runner, backend, native, _, plans = setup
    backend.state["expires_at"] = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    ).isoformat()
    result = runner.run()
    assert (
        result["status"] == "CONDUCTOR_RUN_EXPIRED"
        and native.turn_count == 0
        and plans == []
    )


@pytest.mark.parametrize("status", ["PUBLISHED", "ARCHIVED", "REJECTED", "FAILED"])
def test_business_terminal_stops_permit_before_planning(setup, status):
    runner, backend, native, _, plans = setup
    backend.state["content_status"] = status
    assert runner.run()["status"] == "CONDUCTOR_CONTENT_TERMINAL"
    assert plans == [] and native.closed and any(e[0] == "stop" for e in backend.events)


def test_http_error_never_exposes_response_credentials(tmp_path):
    private = tmp_path / "storage" / "private" / "runtime.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(dict(admin_token="secret")))
    backend = c.HttpBackend("http://127.0.0.1:8000", private, tmp_path)
    backend.client.close()
    backend.client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(
            lambda req: httpx.Response(409, json=dict(detail="SECRET PRIVATE PAYLOAD"))
        ),
    )
    with pytest.raises(c.ConductorError) as raised:
        backend.view(CONTENT)
    assert str(raised.value) == "CONDUCTOR_BACKEND_HTTP_409"
    backend.close()


def test_http_backend_uses_fresh_connections_without_retry(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            requests.append((self.path, self.client_address[1]))
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    private = tmp_path / "storage" / "private" / "runtime.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(dict(admin_token="opaque-secret")))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = c.HttpBackend(f"http://127.0.0.1:{server.server_port}", private, tmp_path)
    try:
        for _ in range(3):
            assert backend.view(CONTENT) == {}
        assert len(requests) == 3
        assert len({port for _, port in requests}) == 3
        assert all(path == f"/api/v1/contents/{CONTENT}/run" for path, _ in requests)
        assert backend.client.timeout == httpx.Timeout(30)
    finally:
        backend.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize(
    "error_class", [httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError]
)
def test_request_failure_journals_only_safe_labels(
    setup, tmp_path, monkeypatch, error_class
):
    runner, _, native, journal, _ = setup
    opaque = "opaque-credential-9f14d23ce57a"
    private = tmp_path / "storage" / "private" / "runtime.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(dict(admin_token=opaque)))
    backend = c.HttpBackend("http://127.0.0.1:8000", private, tmp_path)
    backend.client.close()

    def fail(request):
        raise error_class(opaque, request=request)

    backend.client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": f"Bearer {opaque}"},
        transport=httpx.MockTransport(fail),
    )
    ticks = iter([10, 10.25])
    monkeypatch.setattr(c.time, "monotonic", lambda: next(ticks))
    runner.backend = backend
    with pytest.raises(c.ConductorError) as raised:
        runner.run()
    assert str(raised.value) == "CONDUCTOR_BACKEND_UNREACHABLE"
    entries = [json.loads(line) for line in journal.log_path.read_text().splitlines()]
    assert entries[0] == {
        "seq": 1,
        "kind": "backend_request_failure",
        "error_class": error_class.__name__,
        "method": "GET",
        "endpoint": "/api/v1/contents/{content_id}/run",
        "elapsed_seconds": 0.25,
    }
    assert opaque not in journal.log_path.read_text() + journal.path.read_text()
    assert native.closed and native.sent == []
    backend.close()


@pytest.mark.parametrize(
    ("method", "path", "endpoint", "safe_method"),
    [
        (
            "POST",
            "/api/v1/contents/opaque-secret/run/stop?token=opaque-secret",
            "/api/v1/contents/{content_id}/run/stop",
            "POST",
        ),
        (
            "PUT",
            "/api/v1/content-runs/opaque-secret/intents/opaque-secret#opaque-secret",
            "/api/v1/content-runs/{run_id}/intents/{request_id}",
            "PUT",
        ),
        (
            "POST",
            "/api/v1/content-runs/opaque-secret/intents/opaque-secret/observation",
            "/api/v1/content-runs/{run_id}/intents/{request_id}/observation",
            "POST",
        ),
        (
            "POST",
            "/api/v1/content-runs/opaque-secret/host/attach",
            "/api/v1/content-runs/{run_id}/host/attach",
            "POST",
        ),
        ("opaque-secret", "/opaque-secret?opaque-secret", "unknown", "OTHER"),
    ],
)
def test_request_failure_drops_unknown_class_paths_query_body_and_method(
    tmp_path, method, path, endpoint, safe_method
):
    private = tmp_path / "storage" / "private" / "runtime.json"
    private.parent.mkdir(parents=True)
    private.write_text(json.dumps(dict(admin_token="opaque-secret")))
    backend = c.HttpBackend("http://127.0.0.1:8000", private, tmp_path)
    backend.client.close()
    unknown_error = type("opaque-secret", (httpx.RequestError,), {})

    def fail(request):
        raise unknown_error("opaque-secret", request=request)

    backend.client = httpx.Client(
        base_url="http://127.0.0.1:8000", transport=httpx.MockTransport(fail)
    )
    with pytest.raises(c.ConductorError, match="CONDUCTOR_BACKEND_UNREACHABLE"):
        backend.request(method, path, {"credential": "opaque-secret"})
    diagnostic = backend.last_request_failure
    assert diagnostic["error_class"] == "RequestError"
    assert diagnostic["endpoint"] == endpoint
    assert diagnostic["method"] == safe_method
    assert diagnostic["elapsed_seconds"] >= 0
    assert "opaque-secret" not in json.dumps(diagnostic)
    backend.client.close()
    backend.client = httpx.Client(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    backend.view(CONTENT)
    assert backend.last_request_failure is None
    backend.close()


def test_finished_child_heartbeat_uses_host_idle_not_terminal_intent(setup):
    runner, backend, _, journal, _ = setup
    bind_fixture(runner, journal)
    runner.current.update(child=dict(child_id="child"), admitted=True)
    runner.children["child"] = dict(terminal=True)
    runner.last_heartbeat = -100
    runner._heartbeat()
    assert backend.events[-1][0:2] == ("HEARTBEAT", "host")
    assert backend.events[-1][2]["child"] is None


def test_parent_heartbeat_reports_same_live_child_with_fresh_event_ids(setup):
    runner, backend, native, journal, _ = setup
    w = bind_fixture(runner, journal)
    child = dict(child_id="child", metadata_rpc_id="verified-metadata")
    runner.current.update(child=child, admitted=True)
    runner.children["child"] = dict(terminal=False)
    for _ in range(2):
        runner.last_heartbeat = -100
        runner._heartbeat()
    first, second = backend.events[-2:]
    assert first[:2] == second[:2] == ("HEARTBEAT", w["request_id"])
    assert first[2]["child"] == second[2]["child"] == child
    assert first[2]["event_id"] != second[2]["event_id"]
    native.eof = True
    runner.last_heartbeat = -100
    runner._heartbeat()
    assert backend.events[-1] == second


def test_actual_child_terminal_without_claim_stops_incomplete_handoff(setup):
    runner, backend, native, _, _ = setup
    original = backend.observe

    def observe(run_id, request_id, body):
        response = original(run_id, request_id, body)
        if body["outcome"] == "TERMINAL":
            backend.state["intents"][request_id].update(
                state="TERMINAL", claimbinding_summary=dict(claim_id=None)
            )
        return response

    backend.observe = observe
    assert runner.run()["status"] == "CONDUCTOR_HANDOFF_NOT_CLAIMED"
    assert native.turn_count == 1 and native.closed


def test_attach_response_lost_reconciles_and_stops_without_spawn(setup):
    runner, backend, native, _, _ = setup
    original = backend.attach

    def attach(*args):
        original(*args)
        raise c.ConductorError("CONDUCTOR_BACKEND_UNREACHABLE")

    backend.attach = attach
    with pytest.raises(c.ConductorError, match="BACKEND_UNREACHABLE"):
        runner.run()
    assert native.turn_count == 0 and native.closed
    assert any(e[0] == "stop" for e in backend.events)


def test_verified_observation_body_matches_actual_strict_backend_schema(setup):
    from app.business.requests import (
        HostAttachRequest,
        HostObservationRequest,
        SpawnIntentRequest,
    )

    runner, backend, native, _, _ = setup
    native.after_empty = lambda: finish(backend, runner)
    runner.run()
    for event in backend.events:
        if event[0] == "attach":
            HostAttachRequest.model_validate(event[1])
        elif event[0] == "intent":
            SpawnIntentRequest.model_validate(event[2])
        elif event[0] in {"VERIFIED", "TERMINAL", "HEARTBEAT"}:
            HostObservationRequest.model_validate(event[2])


@pytest.mark.parametrize(
    "item",
    [
        dict(
            type="commandExecution",
            command="worker --lease-token opaque_lease_0123",
            aggregatedOutput="opaque_lease_0123",
        ),
        dict(
            type="agentMessage",
            text="opaque_lease_0123",
            message="private business prompt",
        ),
        dict(
            type="mcpToolCall",
            arguments=dict(lease="opaque_lease_0123"),
            result=dict(token="opaque_lease_0123"),
        ),
    ],
)
def test_opaque_lease_tokens_never_persist_even_without_secret_prefix(tmp_path, item):
    journal = c.Journal(tmp_path / "private", RUN, CONTENT)
    message = dict(
        method="item/completed",
        params=dict(threadId="parent", turnId="turn", item=dict(id="call", **item)),
    )
    summary = c.event_summary(message)
    journal.record("native", summary)
    journal.close()
    assert summary["payload_bytes"] > 0 and len(summary["payload_sha256"]) == 64
    assert (
        "opaque_lease_0123"
        not in journal.log_path.read_text() + journal.path.read_text()
    )
    assert "private business prompt" not in journal.log_path.read_text()


def test_no_task_is_final_only_without_pending_or_inflight_handles(setup):
    runner, backend, native, _, plans = setup

    def planner(run_id):
        plans.append(run_id)
        return dict(
            contract_version=2,
            status="NO_TASK",
            run_id=RUN,
            run_generation=1,
            content_id=CONTENT,
            worker_requests=[],
        )

    runner.planner = planner
    assert runner.run()["status"] == "CONDUCTOR_NO_TASK"
    assert native.closed and native.turn_count == 0 and len(plans) == 1


def test_accepted_pending_consumption_drives_controlled_plan_maintenance(setup):
    runner, backend, native, _, plans = setup
    first_planner = runner.planner

    def planner(run_id):
        if not backend.state["intents"]:
            return first_planner(run_id)
        plans.append(run_id)
        finish(backend, runner)
        return dict(
            contract_version=2,
            status="NO_TASK",
            run_id=RUN,
            run_generation=1,
            content_id=CONTENT,
            worker_requests=[],
        )

    runner.planner = planner

    def accepted():
        if runner.current and runner.current.get("child"):
            w = runner.current["worker"]
            backend.state["intents"][w["request_id"]]["claimbinding_summary"] = dict(
                claim_id="claim",
                task_id=w["task_id"],
                task_status="COMPLETED",
                accepted=True,
                consumed=False,
                accepted_result_hash="a" * 64,
                consumed_result_hash=None,
            )
            native.after_empty = None

    native.after_empty = accepted
    assert runner.run()["status"] == "CONDUCTOR_CONTENT_TERMINAL"
    assert len(plans) == 2 and native.turn_count == 1


@pytest.mark.parametrize(
    "foreign",
    [
        dict(host_epoch=2),
        dict(parent_thread_id="foreign"),
        dict(process_identity={"pid": 999}),
    ],
)
def test_existing_host_without_intents_rejected_before_initialize(setup, foreign):
    runner, backend, native, _, _ = setup
    backend.state.update(foreign)
    with pytest.raises(c.ConductorError, match="RUN_NOT_FRESH"):
        runner.run()
    assert native.sent == []
    assert backend.events == []


def test_main_rejects_healthy_host_before_constructing_new_process(
    tmp_path, monkeypatch
):
    backend = FakeBackend()
    backend.state.update(
        host_epoch=1,
        parent_thread_id="healthy",
        process_identity=dict(pid=999),
        state="running",
    )
    backend.close = lambda: None
    monkeypatch.setattr(c, "HttpBackend", lambda *args: backend)

    def forbidden(*args):
        pytest.fail("Existing host must be rejected before native discovery/Popen")

    monkeypatch.setattr(c, "resolve_native_desktop_binary", forbidden)
    monkeypatch.setattr(c, "NativeTransport", forbidden)
    assert (
        c.main(
            [
                "--workspace",
                str(tmp_path),
                "--api-url",
                "http://127.0.0.1:8000",
                "--runtime-private",
                str(tmp_path / "storage" / "private" / "runtime.json"),
                "--scheduler-launcher",
                str(tmp_path / "unused.ps1"),
                "--run-id",
                RUN,
                "--content-id",
                CONTENT,
            ]
        )
        == 20
    )
    assert backend.events == []


def test_foreign_epoch_on_reconcile_never_requests_stop(setup):
    runner, backend, native, _, _ = setup
    original = backend.attach

    def attach(*args):
        result = original(*args)
        backend.state.update(host_epoch=2)
        return result

    backend.attach = attach
    with pytest.raises(c.ConductorError, match="GENERATION_CHANGED"):
        runner.run()
    assert native.turn_count == 0
    assert not any(event[0] == "stop" for event in backend.events)


def test_lost_attach_foreign_process_not_stopped(setup):
    runner, backend, native, _, _ = setup
    original = backend.attach

    def attach(*args):
        original(*args)
        backend.state["process_identity"]["instance_id"] = "foreign"
        raise c.ConductorError("CONDUCTOR_BACKEND_UNREACHABLE")

    backend.attach = attach
    with pytest.raises(c.ConductorError, match="BACKEND_UNREACHABLE"):
        runner.run()
    assert native.turn_count == 0
    assert not any(event[0] == "stop" for event in backend.events)


def test_machine_stop_cas_rejects_takeover_after_view(setup):
    runner, backend, native, _, _ = setup
    backend.state["content_status"] = "PUBLISHED"
    attempts = []

    def stop(content_id, expected_binding):
        attempts.append(copy.deepcopy(expected_binding))
        backend.state.update(host_epoch=2, parent_thread_id="replacement")
        raise c.ConductorError("CONDUCTOR_BACKEND_HTTP_409")

    backend.stop = stop
    with pytest.raises(c.ConductorError, match="HTTP_409"):
        runner.run()
    assert len(attempts) == 1
    assert (
        attempts[0]["host_epoch"] == 1 and attempts[0]["parent_thread_id"] == "parent"
    )
    assert attempts[0]["process_identity"] == native.identity
    assert backend.state["state"] == "running" and not backend.state.get(
        "stop_requested"
    )


def test_all_machine_stops_have_exact_own_binding(setup):
    runner, backend, native, _, _ = setup
    backend.state["content_status"] = "PUBLISHED"
    runner.run()
    stops = [event for event in backend.events if event[0] == "stop"]
    assert len(stops) == 1
    assert stops[0][2] == dict(
        run_id=RUN,
        generation=1,
        host_epoch=1,
        parent_thread_id="parent",
        process_identity=native.identity,
    )
