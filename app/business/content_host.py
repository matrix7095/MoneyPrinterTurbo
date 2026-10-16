"""Trusted local host admission and durable spawn/claim observations."""

import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from glance_core.database import begin_immediate
from glance_core.models import (
    AgentTask,
    AuditEvent,
    SchedulerOffer,
    TaskStatus,
    as_utc,
    utc_now,
)
from glance_core.task_service import TaskConflictError, stable_hash
from sqlmodel import select

from app.business.content_run import (
    OFFER_ACTION,
    load_run,
    require_run,
    save_run,
)

HOST_TTL = timedelta(seconds=180)
UNRESOLVED = {
    "SPAWN_INTENT",
    "VERIFIED",
    "CLAIMED",
    "UNKNOWN",
    "OBSERVATION_LOST",
    "REJECTED",
}
BLOCKED = {"UNKNOWN", "OBSERVATION_LOST", "REJECTED"}
OBSERVATION = "mpt_content_host_observation"
CLAIM = "mpt_content_run_claim"
LEASE_RENEWED = "mpt_content_host_lease_renewed"
LEASE_DEADLINE = "mpt_content_host_lease_deadline"


def host_live(run):
    return bool(
        run.host_epoch
        and run.host_observed_at
        and (
            as_utc(datetime.fromisoformat(run.host_observed_at)) + HOST_TTL > utc_now()
        )
    )


def require_host(session, run_id, generation=None, epoch=None, *, active=True):
    run = require_run(session, run_id, generation) if active else load_run(session)
    if (
        run is None
        or run.run_id != run_id
        or (generation is not None and run.generation != generation)
    ):
        raise TaskConflictError("CONTENT_HOST_GENERATION_CHANGED")
    if not run.parent_thread_id or not run.process_identity or not host_live(run):
        raise TaskConflictError("CONTENT_HOST_NOT_LIVE")
    if epoch is not None and epoch != run.host_epoch:
        raise TaskConflictError("CONTENT_HOST_EPOCH_CHANGED")
    if active and any(intent["state"] in BLOCKED for intent in run.intents.values()):
        raise TaskConflictError("CONTENT_HOST_OUTCOME_UNRESOLVED")
    return run


def child_work_unresolved(run):
    # Observation expiry is uncertainty, never evidence that a child ended.
    return any(intent["state"] in UNRESOLVED for intent in run.intents.values())


def reserved_tasks(session):
    run = load_run(session)
    if run is None:
        return {}
    tasks = {}
    for request_id, intent in run.intents.items():
        if intent["state"] not in UNRESOLVED:
            continue
        worker, _ = offer_receipt(session, request_id)
        if worker["task_id"] in tasks:
            raise TaskConflictError("CONTENT_HOST_TASK_MULTIPLE_LIVE_HANDLES")
        # Read the frozen receipt, not filtered pending-task ORM rows. A live
        # child remains a resource reservation even after business lease expiry.
        tasks[worker["task_id"]] = worker
    return tasks


def host_view(run):
    return {
        "run_id": run.run_id,
        "generation": run.generation,
        "host_epoch": run.host_epoch,
        "state": run.state,
        "parent_thread_id": run.parent_thread_id,
        "run_state": run.state,
        "host_observation_expires_at": (
            as_utc(datetime.fromisoformat(run.host_observed_at)) + HOST_TTL
        ).isoformat()
        if run.host_observed_at
        else None,
    }


def attach_host(session, run_id, payload):
    begin_immediate(session)
    run = require_run(session, run_id, payload.generation)
    identity = payload.process_identity.model_dump()
    try:
        started = datetime.fromisoformat(identity["started_at"])
        if (
            started.tzinfo is None
            or as_utc(started) > utc_now()
            or not Path(identity["binary_path"]).is_absolute()
        ):
            raise ValueError("Invalid process identity")
    except ValueError:
        raise TaskConflictError("CONTENT_HOST_PROCESS_INVALID") from None
    same = (
        run.parent_thread_id == payload.parent_thread_id
        and run.process_identity == identity
    )
    if run.host_epoch and same:
        if payload.expected_host_epoch not in {run.host_epoch, run.host_epoch - 1}:
            raise TaskConflictError("CONTENT_HOST_EPOCH_CHANGED")
        session.commit()
        return host_view(run)
    if payload.expected_host_epoch != run.host_epoch:
        raise TaskConflictError("CONTENT_HOST_EPOCH_CHANGED")
    if child_work_unresolved(run) or (run.host_epoch and host_live(run)):
        raise TaskConflictError("CONTENT_HOST_TAKEOVER_BLOCKED")
    run.host_epoch += 1
    run.parent_thread_id, run.process_identity = payload.parent_thread_id, identity
    run.host_observed_at = utc_now().isoformat()
    run.state = "running"
    save_run(session, run, "mpt_content_host_attached")
    session.commit()
    return host_view(run)


def offer_receipt(session, request_id):
    receipt = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_type == "scheduler_offer",
            AuditEvent.entity_id == request_id,
            AuditEvent.action == OFFER_ACTION,
        )
    ).one_or_none()
    if receipt is None:
        raise TaskConflictError("CONTENT_HOST_OFFER_MISSING")
    worker = receipt.metadata_json["worker"]
    if stable_hash(worker) != receipt.metadata_json.get("worker_hash"):
        raise TaskConflictError("CONTENT_HOST_OFFER_CHANGED")
    return worker, receipt.metadata_json["worker_hash"]


def check_offer(session, run, request_id, offer_hash):
    worker, frozen_hash = offer_receipt(session, request_id)
    offer = session.get(SchedulerOffer, request_id)
    if (
        frozen_hash != offer_hash
        or worker.get("run_id") != run.run_id
        or worker.get("run_generation") != run.generation
        or worker.get("host_epoch") != run.host_epoch
        or offer is None
        or as_utc(offer.expires_at) <= utc_now()
        or offer.task_id != worker["task_id"]
        or offer.agent_id != worker["agent_id"]
    ):
        raise TaskConflictError("CONTENT_HOST_OFFER_INVALID")
    return worker


def event_repeat(session, run_id, request_id, payload):
    event = session.exec(
        select(AuditEvent).where(
            AuditEvent.action == OBSERVATION,
            AuditEvent.entity_type == "content_run",
            AuditEvent.entity_id == run_id,
            AuditEvent.metadata_json["event_id"].as_string() == payload.event_id,
        )
    ).one_or_none()
    if event:
        if event.metadata_json["request_id"] != request_id or event.metadata_json[
            "payload_hash"
        ] != stable_hash(payload.model_dump()):
            raise TaskConflictError("CONTENT_HOST_EVENT_REUSED")
        return event.metadata_json["response"]
    return None


def record_event(session, run, request_id, payload, response):
    session.add(
        AuditEvent(
            actor_type="human",
            actor_id="mpt-conductor",
            entity_type="content_run",
            entity_id=run.run_id,
            action=OBSERVATION,
            metadata_json={
                "event_id": payload.event_id,
                "request_id": request_id,
                "payload_hash": stable_hash(payload.model_dump()),
                "response": response,
            },
        )
    )


def spawn_intent(session, run_id, request_id, payload):
    begin_immediate(session)
    repeat = event_repeat(session, run_id, request_id, payload)
    if repeat:
        session.commit()
        return repeat
    run = require_host(session, run_id, payload.generation, payload.host_epoch)
    check_offer(session, run, request_id, payload.offer_hash)
    intent = run.intents.get(request_id)
    if intent is not None:
        raise TaskConflictError("CONTENT_HOST_INTENT_ALREADY_EXISTS")
    intent = {
        "state": "SPAWN_INTENT",
        "host_epoch": run.host_epoch,
        "offer_hash": payload.offer_hash,
        "child": None,
        "claim_id": None,
    }
    run.intents[request_id] = intent
    save_run(session, run, "mpt_content_spawn_intent")
    response = {**host_view(run), "request_id": request_id, **intent}
    record_event(session, run, request_id, payload, response)
    session.commit()
    return response


def validate_child(run, worker, child, *, terminal=False):
    if child is None:
        raise TaskConflictError("CONTENT_HOST_CHILD_MISSING")
    issued_hash = hashlib.sha256(worker["prompt"].encode()).hexdigest()
    if (
        child.parent_thread_id != run.parent_thread_id
        or child.ephemeral is not True
        or child.started.parent_thread_id != run.parent_thread_id
        or child.started.child_id != child.child_id
        or child.started.agent_path.rsplit("/", 1)[-1] != worker["title"]
        or (not terminal and child.native_agent_type != worker["native_agent_type"])
        or (
            not terminal
            and (
                child.model != worker["model"]
                or child.reasoning_effort != worker["reasoning_effort"]
            )
        )
        or child.issued_prompt_sha256.lower() != issued_hash
    ):
        raise TaskConflictError("CONTENT_HOST_CHILD_MISMATCH")


def _renew_child_lease(session, run, request_id, intent, worker, payload, tasks):
    """A fresh native observation may renew only its exactly proven current lease."""
    if intent["state"] != "CLAIMED" or run.state not in {"running", "draining"}:
        return
    task = session.get(AgentTask, worker["task_id"])
    now = utc_now()
    if (
        task is None
        or task.status != TaskStatus.LEASED
        or task.lease_expires_at is None
        or as_utc(task.lease_expires_at) <= now
        or not task.lease_token_hash
        or task.assigned_agent_id != worker["agent_id"]
        or task.attempt_started_at is None
        or task.started_at is None
        or not 1 <= task.attempt <= task.max_attempts
    ):
        return
    from app.business.runtime import _require_owned_input

    _require_owned_input(session, task)
    _, frozen_hash = offer_receipt(session, request_id)
    binding = accepted_binding(session, task, task.assigned_agent_id)
    expected = {
        "claim_id": intent["claim_id"],
        "run_id": run.run_id,
        "generation": run.generation,
        "host_epoch": run.host_epoch,
        "request_id": request_id,
        "offer_hash": frozen_hash,
        "task_id": task.id,
        "agent_id": task.assigned_agent_id,
        "lease_token_hash": task.lease_token_hash,
        "child_id": intent["child"]["child_id"],
        "parent_thread_id": run.parent_thread_id,
        "started_at": as_utc(task.started_at).isoformat(),
    }
    if (
        binding is None
        or any(binding.get(key) != value for key, value in expected.items())
        or intent["host_epoch"] != run.host_epoch
        or intent["offer_hash"] != frozen_hash
        or run.offer_children.get(request_id) != expected["child_id"]
        or worker["request_id"] != request_id
        or worker["content_id"] != run.content_id
        or task.content_id != run.content_id
        or worker["input_hash"] != task.input_hash
        or worker["task_type"] != task.task_type.value
        or not as_utc(task.started_at) <= as_utc(task.attempt_started_at) <= now
    ):
        return
    claims = session.exec(select(AuditEvent).where(
        AuditEvent.action == CLAIM,
        AuditEvent.metadata_json["claim_id"].as_string() == intent["claim_id"],
    )).all()
    if (
        len(claims) != 1
        or claims[0].actor_type != "system"
        or claims[0].actor_id != "mpt-content-host"
        or claims[0].entity_type != "agent_task"
        or claims[0].entity_id != task.id
        or claims[0].metadata_json != binding
    ):
        return
    # The claim's lease hash distinguishes attempts. Older confirmation receipts
    # lack exact start/hash proof and remain observable, never retroactively renewed.
    confirmations = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task",
        AuditEvent.entity_id == task.id,
        AuditEvent.action == "claim_confirmed",
        AuditEvent.actor_type == "agent",
        AuditEvent.actor_id == task.assigned_agent_id,
        AuditEvent.metadata_json["lease_token_hash"].as_string() == task.lease_token_hash,
    )).all()
    if (
        len(confirmations) != 1
        or confirmations[0].metadata_json.get("attempt") != task.attempt
        or confirmations[0].metadata_json.get("attempt_started_at")
        != as_utc(task.attempt_started_at).isoformat()
    ):
        return
    # Also reject ambiguous live handles rather than renewing either owner.
    reserved_tasks(session)
    previous_expiry = as_utc(task.lease_expires_at).isoformat()
    renewed = tasks._apply_lease_heartbeat(
        task, now=now, touch_worker=False, deadline_actor_id="lease-host",
    )
    session.add(AuditEvent(
        actor_type="system", actor_id="mpt-content-host", entity_type="agent_task",
        entity_id=task.id, action=LEASE_RENEWED if renewed else LEASE_DEADLINE,
        metadata_json={
            **expected, "input_hash": task.input_hash, "event_id": payload.event_id,
            "attempt": task.attempt,
            "attempt_started_at": as_utc(task.attempt_started_at).isoformat(),
            "previous_lease_expires_at": previous_expiry,
            "lease_expires_at": as_utc(task.lease_expires_at).isoformat() if renewed else None,
        },
    ))


def observe_host(session, run_id, request_id, payload, *, tasks):
    begin_immediate(session)
    repeat = event_repeat(session, run_id, request_id, payload)
    if repeat:
        session.commit()
        return repeat
    run = load_run(session)
    if (
        run is None
        or run.run_id != run_id
        or run.generation != payload.generation
        or run.host_epoch != payload.host_epoch
    ):
        raise TaskConflictError("CONTENT_HOST_EPOCH_CHANGED")
    if not run.parent_thread_id or not run.process_identity:
        raise TaskConflictError("CONTENT_HOST_NOT_ATTACHED")
    intent = run.intents.get(request_id)
    if intent is None:
        # Reserved name for host-idle same-process observations, never a spawn result.
        if (
            request_id != "host"
            or payload.outcome != "HEARTBEAT"
            or payload.child is not None
        ):
            raise TaskConflictError("CONTENT_HOST_INTENT_MISSING")
    else:
        worker, _ = offer_receipt(session, request_id)
        if payload.child is not None:
            validate_child(
                run, worker, payload.child, terminal=payload.outcome == "TERMINAL"
            )
            child = payload.child.model_dump()
            if intent["child"] is not None and (
                {
                    key: value
                    for key, value in intent["child"].items()
                    if key != "metadata_rpc_id"
                }
                != {
                    key: value
                    for key, value in child.items()
                    if key != "metadata_rpc_id"
                }
            ):
                raise TaskConflictError("CONTENT_HOST_CHILD_CHANGED")
        elif payload.outcome in {"VERIFIED", "TERMINAL", "HEARTBEAT"}:
            raise TaskConflictError("CONTENT_HOST_CHILD_MISSING")
        if payload.outcome == "VERIFIED":
            if intent["state"] not in {"SPAWN_INTENT", "UNKNOWN"} or run.stop_requested:
                raise TaskConflictError("CONTENT_HOST_INTENT_NOT_BINDABLE")
            if any(
                other.get("child", {}).get("child_id") == payload.child.child_id
                for other in run.intents.values()
                if other.get("child")
            ):
                raise TaskConflictError("CONTENT_HOST_CHILD_ALREADY_BOUND")
            intent.update(state="VERIFIED", child=child)
            run.offer_children[request_id] = payload.child.child_id
        elif payload.outcome == "HEARTBEAT":
            if (
                intent["state"] not in {"VERIFIED", "CLAIMED", "OBSERVATION_LOST"}
                or intent["child"] is None
            ):
                raise TaskConflictError("CONTENT_HOST_INTENT_NOT_OBSERVABLE")
            _renew_child_lease(session, run, request_id, intent, worker, payload, tasks)
            if intent["state"] == "OBSERVATION_LOST":
                intent["state"] = "CLAIMED" if intent["claim_id"] else "VERIFIED"
        elif payload.outcome == "TERMINAL":
            if intent["child"] is None:
                intent["child"] = child
                run.offer_children[request_id] = payload.child.child_id
            intent["state"] = "TERMINAL"
            offer = session.get(SchedulerOffer, request_id)
            if offer is not None:
                offer.expires_at = utc_now()
                session.add(offer)
        else:
            if intent["state"] == "TERMINAL":
                raise TaskConflictError("CONTENT_HOST_INTENT_TERMINAL")
            intent["state"] = payload.outcome
    run.host_observed_at = utc_now().isoformat()
    if payload.outcome != "HEARTBEAT":
        run.last_event_cursor = payload.event_id
    save_run(session, run, "mpt_content_host_observed")
    response = {
        **host_view(run),
        "request_id": request_id,
        "intent_state": intent["state"] if intent else None,
    }
    record_event(session, run, request_id, payload, response)
    session.commit()
    return response


def require_admission(session, worker, offer_hash):
    run = require_host(
        session, worker["run_id"], worker["run_generation"], worker.get("host_epoch")
    )
    intent = run.intents.get(worker["request_id"])
    if intent is None or intent["state"] == "SPAWN_INTENT":
        raise TaskConflictError("CONTENT_HOST_BINDING_PENDING")
    if (
        intent["state"] != "VERIFIED"
        or intent["host_epoch"] != run.host_epoch
        or intent["offer_hash"] != offer_hash
    ):
        raise TaskConflictError("CONTENT_HOST_NOT_ADMITTED")
    check_offer(session, run, worker["request_id"], offer_hash)
    return run, intent


def admission(session, request_id, *, role_id, agent_id, run_id, generation):
    worker, frozen_hash = offer_receipt(session, request_id)
    if (
        worker["role_id"] != str(role_id)
        or worker["agent_id"] != agent_id
        or worker["run_id"] != run_id
        or worker["run_generation"] != generation
    ):
        raise TaskConflictError("CONTENT_HOST_ADMISSION_IDENTITY_MISMATCH")
    try:
        require_admission(session, worker, frozen_hash)
        return {"status": "READY", "offer_hash": frozen_hash, "retry_after_sec": 0}
    except TaskConflictError as exc:
        return {
            "status": "WAITING"
            if str(exc) == "CONTENT_HOST_BINDING_PENDING"
            else "BLOCKED",
            "reason": str(exc),
            "retry_after_sec": 1,
        }


def record_claim(session, task, scope):
    if scope is None:
        return
    run_id, generation, request_id = scope
    worker, frozen_hash = offer_receipt(session, request_id)
    run = require_host(session, run_id, generation, worker["host_epoch"])
    intent = run.intents[request_id]
    if intent["state"] != "VERIFIED" or intent["claim_id"] is not None:
        raise TaskConflictError("CONTENT_HOST_ALREADY_CLAIMED")
    claim_id = "claim_" + secrets.token_hex(16)
    binding = {
        "claim_id": claim_id,
        "run_id": run_id,
        "generation": generation,
        "host_epoch": run.host_epoch,
        "request_id": request_id,
        "offer_hash": frozen_hash,
        "task_id": task.id,
        "agent_id": task.assigned_agent_id,
        "lease_token_hash": task.lease_token_hash,
        "child_id": intent["child"]["child_id"],
        "parent_thread_id": run.parent_thread_id,
        "started_at": as_utc(task.started_at).isoformat(),
    }
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="mpt-content-host",
            entity_type="agent_task",
            entity_id=task.id,
            action=CLAIM,
            metadata_json=binding,
        )
    )
    intent.update(state="CLAIMED", claim_id=claim_id)
    save_run(session, run, "mpt_content_host_claimed")


def accepted_binding(session, task, agent_id):
    scoped = session.exec(
        select(AuditEvent.id).where(
            AuditEvent.action == OFFER_ACTION,
            AuditEvent.entity_type == "scheduler_offer",
            AuditEvent.metadata_json["worker"]["agent_id"].as_string() == agent_id,
        )
    ).first()
    if scoped is None:
        return None
    bindings = session.exec(
        select(AuditEvent).where(
            AuditEvent.action == CLAIM,
            AuditEvent.entity_type == "agent_task",
            AuditEvent.entity_id == task.id,
            AuditEvent.metadata_json["agent_id"].as_string() == agent_id,
            AuditEvent.metadata_json["lease_token_hash"].as_string()
            == task.lease_token_hash,
        )
    ).all()
    if len(bindings) != 1:
        raise TaskConflictError("CONTENT_HOST_CLAIM_ASSOCIATION_MISSING")
    data = bindings[0].metadata_json
    worker, frozen_hash = offer_receipt(session, data["request_id"])
    if (
        data["offer_hash"] != frozen_hash
        or worker["task_id"] != task.id
        or worker["agent_id"] != agent_id
        or worker["run_id"] != data["run_id"]
        or worker["run_generation"] != data["generation"]
        or worker["host_epoch"] != data["host_epoch"]
    ):
        raise TaskConflictError("CONTENT_HOST_CLAIM_ASSOCIATION_CHANGED")
    return data


def authorize_publish(session, task):
    data = accepted_binding(session, task, task.assigned_agent_id)
    if data is None:
        return
    run = require_host(session, data["run_id"], data["generation"], data["host_epoch"])
    intent = run.intents.get(data["request_id"])
    if (
        intent is None
        or intent["state"] != "CLAIMED"
        or intent["claim_id"] != data["claim_id"]
    ):
        raise TaskConflictError("CONTENT_HOST_PUBLISH_NOT_ADMITTED")


def claim_summary(session, intent):
    from glance_core.models import AgentSubmission
    from app.business.runtime import AGENT_CONSUMED

    if not intent.get("claim_id"):
        return None
    receipts = session.exec(
        select(AuditEvent).where(
            AuditEvent.action == CLAIM,
            AuditEvent.metadata_json["claim_id"].as_string() == intent["claim_id"],
        )
    ).all()
    if len(receipts) != 1:
        raise TaskConflictError("CONTENT_HOST_CLAIM_ASSOCIATION_MISSING")
    data = receipts[0].metadata_json
    task = session.get(AgentTask, data["task_id"])
    if task is None:
        raise TaskConflictError("CONTENT_HOST_CLAIM_TASK_MISSING")
    associated = (
        task.assigned_agent_id == data["agent_id"]
        and task.lease_token_hash == data["lease_token_hash"]
    )
    submission = session.exec(
        select(AgentSubmission)
        .where(
            AgentSubmission.task_id == task.id,
            AgentSubmission.agent_id == data["agent_id"],
            AgentSubmission.attempt == task.attempt,
            AgentSubmission.validation_status == "ACCEPTED",
        )
        .order_by(AgentSubmission.created_at.desc())
    ).first()
    accepted = (
        associated
        and task.status == TaskStatus.COMPLETED
        and submission is not None
        and submission.result_hash == stable_hash(task.result_data)
    )
    consumed = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task",
            AuditEvent.entity_id == task.id,
            AuditEvent.action == AGENT_CONSUMED,
        )
    ).all()
    valid_consumed = bool(
        accepted
        and consumed
        and all(
            event.metadata_json.get("result_hash") == submission.result_hash
            for event in consumed
        )
    )
    return {
        "claim_id": data["claim_id"],
        "task_id": task.id,
        "current_attempt": task.attempt,
        "task_status": task.status.value,
        "accepted": bool(accepted),
        "accepted_result_hash": submission.result_hash if accepted else None,
        "consumed": valid_consumed,
        "consumed_result_hash": submission.result_hash if valid_consumed else None,
    }
