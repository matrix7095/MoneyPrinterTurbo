"""One private, content-scoped execution permit; no host or Worker is implied."""

import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Literal

from glance_core.database import begin_immediate
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Content,
    ContentStatus,
    RuntimeSetting,
    SchedulerOffer,
    TaskStatus,
    as_utc,
    utc_now,
)
from glance_core.task_service import TaskConflictError, stable_hash
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from app.business.quarantine import assert_allowed

RUN_KEY = "mpt_content_run_v1"
OFFER_ACTION = "mpt_content_run_offer"
TTL = timedelta(minutes=120)
FROZEN_FIELDS = (
    "request_id",
    "task_id",
    "task_type",
    "role_id",
    "skill_name",
    "model",
    "reasoning_effort",
    "agent_id",
    "launcher",
    "scheduler_id",
    "run_id",
    "run_generation",
    "content_id",
    "input_hash",
    "host_epoch",
    "round_id",
    "title",
    "prompt",
    "native_agent_type",
    "fork_turns",
)


class ContentRun(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1] = 1
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    content_id: str
    generation: int = Field(ge=1)
    state: Literal["awaiting_host", "running", "draining", "stopped", "expired"]
    created_at: str
    expires_at: str
    stop_requested: bool = False
    stop_reason: str = ""
    allow_local_image_adaptation: bool = False
    parent_thread_id: str | None = None
    process_identity: dict | None = None
    last_event_cursor: str | None = None
    offer_children: dict[str, str] = Field(default_factory=dict)
    host_epoch: int = Field(default=0, ge=0)
    host_observed_at: str | None = None
    ready_rounds: int = Field(default=0, ge=0, le=16)
    intents: dict[str, dict] = Field(default_factory=dict)


def load_run(session):
    row = session.get(RuntimeSetting, RUN_KEY)
    if row is None:
        return None
    try:
        run = ContentRun.model_validate(json.loads(row.value))
        from app.business.requests import HostIntentRecord, HostProcess

        for intent in run.intents.values():
            HostIntentRecord.model_validate(intent)
        if run.process_identity is not None:
            HostProcess.model_validate(run.process_identity)
        if (
            run.host_observed_at is not None
            and datetime.fromisoformat(run.host_observed_at).tzinfo is None
        ):
            raise ValueError("Invalid host observation timestamp")
        dates = [
            datetime.fromisoformat(run.created_at),
            datetime.fromisoformat(run.expires_at),
        ]
        if (
            any(date.tzinfo is None for date in dates)
            or not timedelta(0) < dates[1] - dates[0] <= TTL
        ):
            raise ValueError("Invalid run lifetime")
        return run
    except (TypeError, ValueError):
        raise TaskConflictError("CONTENT_RUN_RECORD_INVALID") from None


def save_run(session, run, action):
    row = session.get(RuntimeSetting, RUN_KEY) or RuntimeSetting(key=RUN_KEY, value="")
    row.value = run.model_dump_json()
    row.updated_at = utc_now()
    session.add(row)
    admin = action in {"mpt_content_run_started", "mpt_content_run_stop_requested"}
    session.add(
        AuditEvent(
            actor_type="admin" if admin else "system",
            actor_id="mpt-admin-api" if admin else "mpt-content-run",
            entity_type="content_run",
            entity_id=run.run_id,
            action=action,
            metadata_json=run.model_dump(),
        )
    )


def invalidate_offers(session, run):
    receipts = select(AuditEvent.entity_id).where(
        AuditEvent.entity_type == "scheduler_offer",
        AuditEvent.action == OFFER_ACTION,
        AuditEvent.metadata_json["worker"]["run_id"].as_string() == run.run_id,
        AuditEvent.metadata_json["worker"]["run_generation"].as_integer()
        == run.generation,
    )
    for offer in session.exec(
        select(SchedulerOffer).where(
            SchedulerOffer.request_id.in_(receipts),
            SchedulerOffer.expires_at > utc_now(),
        )
    ).all():
        offer.expires_at = utc_now()
        session.add(offer)


def live_tasks(session, content_id):
    return session.exec(
        select(AgentTask.id).where(
            AgentTask.content_id == content_id,
            (AgentTask.status == TaskStatus.SUBMITTED)
            | (
                (AgentTask.status == TaskStatus.LEASED)
                & (
                    AgentTask.lease_expires_at.is_(None)
                    | (AgentTask.lease_expires_at > utc_now())
                )
            ),
        )
    ).all()


def refresh_run(session, run):
    from app.business.content_host import child_work_unresolved

    expired = as_utc(datetime.fromisoformat(run.expires_at)) <= utc_now()
    if run.state in {"awaiting_host", "running"} and expired:
        invalidate_offers(session, run)
        run.stop_requested = True
        run.stop_reason = "expired"
        run.state = (
            "draining"
            if live_tasks(session, run.content_id) or child_work_unresolved(run)
            else "expired"
        )
        save_run(session, run, "mpt_content_run_expired")
    elif (
        run.state == "draining"
        and not live_tasks(session, run.content_id)
        and not child_work_unresolved(run)
    ):
        run.state = "expired" if run.stop_reason == "expired" else "stopped"
        save_run(session, run, "mpt_content_run_drained")
    return run


def require_run(session, run_id, generation=None):
    run = load_run(session)
    if (
        run is None
        or run.run_id != run_id
        or (generation is not None and run.generation != generation)
        or run.state not in {"awaiting_host", "running"}
        or run.stop_requested
        or as_utc(datetime.fromisoformat(run.expires_at)) <= utc_now()
    ):
        raise TaskConflictError("CONTENT_RUN_NOT_ACTIVE")
    assert_allowed(session, content_id=run.content_id)
    content = session.get(Content, run.content_id)
    if content is None or content.business_status in {
        ContentStatus.ARCHIVED,
        ContentStatus.PUBLISHED,
        ContentStatus.REJECTED,
    }:
        raise TaskConflictError("CONTENT_RUN_CONTENT_NOT_CURRENT")
    return run


def run_view(session, run):
    from app.business.content_host import claim_summary, host_live

    content = session.get(Content, run.content_id)
    return {
        **run.model_dump(),
        "execution_mode": "native_host" if run.host_epoch else "contract_only",
        "current_stage": content.current_stage if content else None,
        "content_status": content.business_status.value if content else None,
        "blocking_reason": run.stop_reason
        or (content.error_code if content else "CONTENT_MISSING")
        or (
            "HOST_NOT_ATTACHED"
            if not run.host_epoch
            else "HOST_OBSERVATION_EXPIRED"
            if not host_live(run)
            else ""
        ),
        "active_task_ids": live_tasks(session, run.content_id),
        "host_live": host_live(run),
        "intents": {
            key: {**intent, "claimbinding_summary": claim_summary(session, intent)}
            for key, intent in run.intents.items()
        },
    }


def start_run(session, content_id, *, allow_local_image_adaptation=None):
    if allow_local_image_adaptation is not None and type(allow_local_image_adaptation) is not bool:
        raise TaskConflictError("CONTENT_RUN_PERMISSION_INVALID")
    begin_immediate(session)
    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    if content.business_status in {
        ContentStatus.ARCHIVED,
        ContentStatus.PUBLISHED,
        ContentStatus.REJECTED,
    }:
        raise TaskConflictError("CONTENT_RUN_CONTENT_NOT_CURRENT")
    existing = load_run(session)
    if existing:
        refresh_run(session, existing)
        if existing.state in {"awaiting_host", "running", "draining"}:
            if existing.content_id != content_id:
                raise TaskConflictError("CONTENT_RUN_ALREADY_ACTIVE")
            if allow_local_image_adaptation is not None and existing.allow_local_image_adaptation != allow_local_image_adaptation:
                raise TaskConflictError("CONTENT_RUN_PERMISSION_FROZEN")
            session.commit()
            return run_view(session, existing)
    now = utc_now()
    run = ContentRun(
        run_id="run_" + secrets.token_hex(16),
        content_id=content_id,
        generation=existing.generation + 1 if existing else 1,
        state="awaiting_host",
        created_at=now.isoformat(),
        expires_at=(now + TTL).isoformat(),
        allow_local_image_adaptation=allow_local_image_adaptation is True,
    )
    save_run(session, run, "mpt_content_run_started")
    if allow_local_image_adaptation is not None:
        session.add(AuditEvent(
            actor_type="admin", actor_id="mpt-admin-api", entity_type="content_run",
            entity_id=run.run_id, action="mpt_content_run_image_adaptation_permission_recorded",
            metadata_json={
                "run_id": run.run_id, "generation": run.generation, "content_id": content_id,
                "allow_local_image_adaptation": allow_local_image_adaptation,
                "scope": "new_ai_concept_images_frozen_minimum_size_preserve_original_and_processing_record_independent_qc",
            },
        ))
    session.commit()
    return run_view(session, run)


def get_run(session, content_id, *, stop=False, expected_binding=None):
    from app.business.content_host import child_work_unresolved

    begin_immediate(session)
    if session.get(Content, content_id) is None:
        raise KeyError(content_id)
    run = load_run(session)
    if run is None or run.content_id != content_id:
        raise KeyError("Content run not found")
    if expected_binding is not None:
        expected = expected_binding.model_dump()
        if {key: getattr(run, key) for key in expected} != expected:
            session.rollback()
            raise TaskConflictError("CONTENT_RUN_STOP_BINDING_CHANGED")
    refresh_run(session, run)
    if stop and run.state in {"awaiting_host", "running"}:
        invalidate_offers(session, run)
        run.stop_requested = True
        run.stop_reason = "operator_stop"
        run.state = (
            "draining"
            if live_tasks(session, content_id) or child_work_unresolved(run)
            else "stopped"
        )
        save_run(session, run, "mpt_content_run_machine_stop_requested" if expected_binding is not None else "mpt_content_run_stop_requested")
    session.commit()
    return run_view(session, run)


def scoped_offer_clause(session=None):
    # Only pending selection is isolated. Other content's leases still count.
    scoped = select(AuditEvent.entity_id).where(
        AuditEvent.entity_type == "scheduler_offer",
        AuditEvent.action == OFFER_ACTION,
    )
    offered = AgentTask.id.in_(
        select(SchedulerOffer.task_id).where(
            SchedulerOffer.request_id.in_(scoped),
            SchedulerOffer.expires_at > utc_now(),
        )
    )
    if session is None:
        return offered
    from app.business.content_host import reserved_tasks

    return offered | AgentTask.id.in_(list(reserved_tasks(session)))


def authorize_claim(session, *, agent_id, task_id, role_id, runtime_info, scope):
    """Called inside the core claim write transaction, before any lease mutation."""
    receipts = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_type == "scheduler_offer",
            AuditEvent.action == OFFER_ACTION,
            AuditEvent.metadata_json["worker"]["agent_id"].as_string() == agent_id,
        )
    ).all()
    if not scope:
        from app.business.content_host import reserved_tasks

        scoped = select(AuditEvent.entity_id).where(
            AuditEvent.entity_type == "scheduler_offer",
            AuditEvent.action == OFFER_ACTION,
        )
        if (
            receipts
            or task_id in reserved_tasks(session)
            or (
                task_id
                and session.exec(
                    select(SchedulerOffer.request_id).where(
                        SchedulerOffer.task_id == task_id,
                        SchedulerOffer.request_id.in_(scoped),
                        SchedulerOffer.expires_at > utc_now(),
                    )
                ).first()
            )
        ):
            raise TaskConflictError("CONTENT_RUN_OFFER_REQUIRED")
        return
    run_id, generation, request_id = scope
    run = require_run(session, run_id, generation)
    if not task_id or len(receipts) != 1:
        raise TaskConflictError("CONTENT_RUN_OFFER_INVALID")
    receipt = receipts[0]
    offer = session.get(SchedulerOffer, request_id)
    task = session.get(AgentTask, task_id)
    worker = receipt.metadata_json.get("worker", {})
    if (
        receipt.entity_id != request_id
        or offer is None
        or as_utc(offer.expires_at) <= utc_now()
        or offer.task_id != task_id
        or offer.agent_id != agent_id
        or task is None
        or task.status != TaskStatus.PENDING
        or task.content_id != run.content_id
        or worker.get("run_id") != run_id
        or worker.get("run_generation") != generation
        or worker.get("content_id") != run.content_id
        or worker.get("task_id") != task_id
        or worker.get("task_type") != task.task_type.value
        or worker.get("input_hash") != task.input_hash
        or stable_hash(task.input_data) != task.input_hash
        or worker.get("agent_id") != agent_id
        or worker.get("request_id") != request_id
        or worker.get("role_id") != str(role_id)
        or worker.get("scheduler_id") != offer.scheduler_id
        or stable_hash(worker) != receipt.metadata_json.get("worker_hash")
        or (runtime_info or {}).get("model_name") != worker.get("model")
        or (runtime_info or {}).get("reasoning_effort")
        != worker.get("reasoning_effort")
        or not re.fullmatch(r"[0-9a-f]{12}", agent_id.rsplit("-", 1)[-1])
    ):
        raise TaskConflictError("CONTENT_RUN_OFFER_INVALID")
    from app.business.content_host import require_admission

    require_admission(session, worker, receipt.metadata_json["worker_hash"])
