"""Codex scheduler profiles and read-only dispatch planning.

This module is the v1 authority for the Codex scheduler.  It deliberately does
not read or write Codex Desktop automation state: the only persisted value is
the validated JSON document in ``runtime_setting``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)
from sqlmodel import Session, select

from glance_core.agent_roles import AgentRole, primary_role_for
from glance_core.models import (
    AgentTask,
    AuditEvent,
    RuntimeSetting,
    SchedulerOffer,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)

SCHEDULER_CONFIG_KEY = "codex_scheduler_config_v1"
SCHEDULER_CONFIG_VERSION = 1
DEFAULT_SCHEDULER_ID = "codex"
DEFAULT_SCHEDULER_LABEL = "Codex App"
DEFAULT_MODEL = "gpt-6.1-sol"

SUPPORTED_MODELS = frozenset({"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna"})
SUPPORTED_REASONING_EFFORTS = frozenset({"medium", "high", "xhigh", "max"})

SCHEDULABLE_TASK_TYPES: tuple[TaskType, ...] = tuple(
    task_type for task_type in TaskType if task_type != TaskType.AGENT_SELFTEST
)
SCHEDULABLE_TASK_TYPE_VALUES = frozenset(task_type.value for task_type in SCHEDULABLE_TASK_TYPES)

_DEFAULT_EFFORT_BY_ROLE: dict[AgentRole, str] = {
    AgentRole.DISCOVERY: "xhigh",
    AgentRole.ASSISTANT: "medium",
    AgentRole.RESEARCH: "high",
    AgentRole.SCRIPT: "high",
    AgentRole.VISUAL: "high",
    AgentRole.IMAGE: "medium",
    AgentRole.VIDEO: "high",
    AgentRole.PUBLISH: "high",
}


class SchedulerTaskProfile(BaseModel):
    """Validated per-task-type model and concurrency settings."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_type: StrictStr
    enabled: StrictBool
    model: StrictStr
    reasoning_effort: StrictStr
    max_parallel: StrictInt = Field(ge=1, le=8)

    @field_validator("task_type")
    @classmethod
    def validate_task_type(cls, value: str) -> str:
        if value not in SCHEDULABLE_TASK_TYPE_VALUES:
            if value == TaskType.AGENT_SELFTEST.value:
                raise ValueError("agent_selftest cannot be configured in the Codex scheduler")
            raise ValueError(f"unknown scheduler task_type: {value}")
        return value

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        if value not in SUPPORTED_MODELS:
            allowed = ", ".join(sorted(SUPPORTED_MODELS))
            raise ValueError(f"unsupported scheduler model: {value}; allowed: {allowed}")
        return value

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str) -> str:
        if value not in SUPPORTED_REASONING_EFFORTS:
            allowed = ", ".join(sorted(SUPPORTED_REASONING_EFFORTS))
            raise ValueError(f"unsupported reasoning_effort: {value}; allowed: {allowed}")
        return value

    @model_validator(mode="after")
    def validate_publish_concurrency(self) -> SchedulerTaskProfile:
        if self.task_type == TaskType.DOUYIN_PUBLISH.value and self.max_parallel != 1:
            raise ValueError("douyin_publish requires max_parallel=1 for the single local account")
        return self


class SchedulerConfig(BaseModel):
    """Versioned Codex scheduler configuration stored as one JSON document."""

    model_config = ConfigDict(extra="forbid", strict=True)

    version: StrictInt
    scheduler_id: StrictStr
    label: StrictStr = Field(min_length=1, max_length=100)
    enabled: StrictBool
    max_parallel: StrictInt = Field(ge=1, le=16)
    task_profiles: list[SchedulerTaskProfile] = Field(
        min_length=len(SCHEDULABLE_TASK_TYPES), max_length=len(SCHEDULABLE_TASK_TYPES)
    )

    @field_validator("version")
    @classmethod
    def validate_version(cls, value: int) -> int:
        if value != SCHEDULER_CONFIG_VERSION:
            raise ValueError(f"unsupported scheduler config version: {value}")
        return value

    @field_validator("scheduler_id")
    @classmethod
    def validate_scheduler_id(cls, value: str) -> str:
        if value not in {DEFAULT_SCHEDULER_ID, "mpt-codex"}:
            raise ValueError(f"unsupported scheduler_id: {value}")
        return value

    @model_validator(mode="after")
    def validate_task_profile_set(self) -> SchedulerConfig:
        task_types = [profile.task_type for profile in self.task_profiles]
        if len(task_types) != len(set(task_types)):
            raise ValueError("task_profiles must not contain duplicate task_type values")
        expected = SCHEDULABLE_TASK_TYPE_VALUES
        actual = set(task_types)
        missing = sorted(expected - actual)
        if missing:
            raise ValueError(f"task_profiles missing task_type values: {', '.join(missing)}")
        if actual - expected:
            unexpected = sorted(actual - expected)
            raise ValueError(f"task_profiles contain unknown task_type values: {unexpected}")
        return self


class SchedulerPlanItem(BaseModel):
    """The only data a scheduler needs to create one new Agent execution."""

    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: StrictStr
    task_type: StrictStr
    model: StrictStr
    reasoning_effort: StrictStr


def _config_model(payload: SchedulerConfig | Mapping[str, Any]) -> SchedulerConfig:
    if isinstance(payload, SchedulerConfig):
        return payload
    return SchedulerConfig.model_validate(payload)


def _config_dict(config: SchedulerConfig) -> dict[str, Any]:
    return config.model_dump(mode="json")


def _default_reasoning_effort(task_type: TaskType) -> str:
    role = primary_role_for(task_type)
    return _DEFAULT_EFFORT_BY_ROLE[role]


def default_scheduler_config() -> dict[str, Any]:
    """Return the complete v1 configuration as a JSON-compatible dictionary."""
    return _config_dict(
        SchedulerConfig(
            version=SCHEDULER_CONFIG_VERSION,
            scheduler_id=DEFAULT_SCHEDULER_ID,
            label=DEFAULT_SCHEDULER_LABEL,
            enabled=True,
            max_parallel=2,
            task_profiles=[
                SchedulerTaskProfile(
                    task_type=task_type.value,
                    enabled=True,
                    model=DEFAULT_MODEL,
                    reasoning_effort=_default_reasoning_effort(task_type),
                    max_parallel=1 if task_type == TaskType.DOUYIN_PUBLISH else 2,
                )
                for task_type in SCHEDULABLE_TASK_TYPES
            ],
        )
    )


def _backfill_missing_profiles(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Add newly schedulable task types to a previously persisted v1 config.

    The scheduler document keeps version 1 while task types evolve.  A strict
    read must still reject malformed or duplicate profiles, but an older valid
    document should not make the whole console/API unavailable merely because
    it predates a newly added Agent role.
    """
    raw_profiles = payload.get("task_profiles")
    if not isinstance(raw_profiles, list):
        return dict(payload)
    existing_types = {item.get("task_type") for item in raw_profiles if isinstance(item, Mapping)}
    additions = [
        profile
        for profile in default_scheduler_config()["task_profiles"]
        if profile["task_type"] not in existing_types
    ]
    if not additions:
        return dict(payload)
    return {**payload, "task_profiles": [*raw_profiles, *additions]}


def _decode_row(row: RuntimeSetting) -> SchedulerConfig:
    try:
        raw = json.loads(row.value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {SCHEDULER_CONFIG_KEY}") from exc
    if not isinstance(raw, Mapping):
        return _config_model(raw)
    return _config_model(_backfill_missing_profiles(raw))


def load_scheduler_config(session: Session) -> dict[str, Any]:
    """Load and strictly validate the Codex scheduler configuration."""
    row = session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY)
    if row is None:
        return default_scheduler_config()
    return _config_dict(_decode_row(row))


def save_scheduler_config(
    session: Session,
    payload: SchedulerConfig | Mapping[str, Any],
    actor_id: str = "admin-ui",
) -> int:
    """Validate and persist the v1 configuration; return 1 only when changed."""
    config = _config_model(payload)
    normalized = _config_dict(config)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    existing = session.get(RuntimeSetting, SCHEDULER_CONFIG_KEY)

    if existing is not None:
        try:
            if _config_dict(_decode_row(existing)) == normalized:
                return 0
        except (TypeError, ValueError):
            # A corrupt row is replaced by the strictly validated payload below.
            pass

    if existing is None:
        session.add(RuntimeSetting(key=SCHEDULER_CONFIG_KEY, value=encoded))
    else:
        existing.value = encoded
        existing.updated_at = utc_now()
        session.add(existing)
    session.add(
        AuditEvent(
            actor_type="human",
            actor_id=actor_id,
            entity_type="runtime_setting",
            entity_id=SCHEDULER_CONFIG_KEY,
            action="scheduler_config_updated",
            metadata_json={"changed": normalized},
        )
    )
    session.commit()
    return 1


def _task_type_value(task: AgentTask) -> str | None:
    raw = task.task_type
    value = raw.value if isinstance(raw, TaskType) else raw
    if value == TaskType.AGENT_SELFTEST.value:
        return None
    if not isinstance(value, str) or value not in SCHEDULABLE_TASK_TYPE_VALUES:
        raise ValueError(f"unknown AgentTask task_type: {value}")
    return value


def _status_value(task: AgentTask) -> str:
    raw = task.status
    return raw.value if isinstance(raw, TaskStatus) else raw


def _task_sort_key(task: AgentTask) -> tuple[int, datetime, str]:
    return (-task.priority, as_utc(task.created_at), task.id)


def build_scheduler_plan(
    tasks: Iterable[AgentTask] | Session,
    payload: SchedulerConfig | Mapping[str, Any],
    *,
    handsfree: bool = False,
) -> list[dict[str, Any]]:
    """Build a read-only plan for pending tasks under the configured capacity.

    LEASED tasks consume both scheduler-wide and per-task-type capacity.  PENDING
    tasks are then considered in priority-descending, creation-ascending order;
    each selected task becomes one metadata-only dispatch item.  No task or
    session state is changed.
    """
    config = _config_model(payload)
    if not config.enabled:
        return []

    session = tasks if isinstance(tasks, Session) else None
    if session is not None:
        tasks = session.exec(
            select(AgentTask).where(AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]))
        ).all()

    profiles = {profile.task_type: profile for profile in config.task_profiles}
    now = utc_now()
    leased_total = 0
    leased_by_type: dict[str, int] = {}
    pending: list[tuple[AgentTask, str]] = []
    offered_ids: set[str] = set()
    if session is not None:
        live_offers = session.exec(
            select(SchedulerOffer).where(SchedulerOffer.expires_at > now)
        ).all()
        for offer in live_offers:
            offered_ids.add(offer.task_id)
            leased_total += 1
            if offer.task_type:
                leased_by_type[offer.task_type] = leased_by_type.get(offer.task_type, 0) + 1

    for task in tasks:
        status = _status_value(task)
        if status not in {TaskStatus.PENDING.value, TaskStatus.LEASED.value}:
            continue
        task_type = _task_type_value(task)
        if task_type is None:
            continue
        if status == TaskStatus.LEASED.value:
            leased_total += 1
            leased_by_type[task_type] = leased_by_type.get(task_type, 0) + 1
        elif task.id in offered_ids:
            continue
        elif task.not_before is None or as_utc(task.not_before) <= now:
            pending.append((task, task_type))

    global_remaining = max(0, config.max_parallel - leased_total)
    per_type_remaining = {
        task_type: max(0, profile.max_parallel - leased_by_type.get(task_type, 0))
        for task_type, profile in profiles.items()
    }
    plan: list[dict[str, Any]] = []

    def order(item):
        task, task_type = item
        lane = 0
        if handsfree:
            if task_type in {
                TaskType.DOUYIN_PUBLISH.value,
                TaskType.PUBLISH_REVIEW_ASSIST.value,
                TaskType.PUBLISH_PREPARE.value,
            }:
                lane = 0
            elif task_type in {
                TaskType.RECOVERY.value,
                TaskType.CONTENT_QC.value,
                TaskType.FINAL_REVIEW_ASSIST.value,
            }:
                lane = 1
            elif task_type in {
                TaskType.SOURCE_DISCOVERY.value,
                TaskType.CANDIDATE_ENRICH.value,
                TaskType.CANDIDATE_ASSIST.value,
            }:
                lane = 3
            else:
                lane = 2
        return (lane, *_task_sort_key(task))

    for task, task_type in sorted(pending, key=order):
        if global_remaining <= 0:
            break
        profile = profiles[task_type]
        if not profile.enabled or per_type_remaining[task_type] <= 0:
            continue
        plan.append(
            SchedulerPlanItem(
                task_id=task.id,
                task_type=task_type,
                model=profile.model,
                reasoning_effort=profile.reasoning_effort,
            ).model_dump(mode="json")
        )
        global_remaining -= 1
        per_type_remaining[task_type] -= 1
    return plan
