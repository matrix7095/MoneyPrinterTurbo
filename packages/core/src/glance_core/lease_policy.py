from __future__ import annotations

from sqlmodel import Session

from glance_core.models import AgentTask, AuditEvent, TaskLeasePolicy, TaskType, utc_now

DEFAULT_LEASE_MINUTES: dict[TaskType, int] = {
    TaskType.SOURCE_DISCOVERY: 60,
    TaskType.CANDIDATE_ENRICH: 45,
    TaskType.CANDIDATE_ASSIST: 45,
    TaskType.CONTENT_RESEARCH: 90,
    TaskType.SCENE_ANALYZE: 45,
    TaskType.SHORT_SCRIPT: 60,
    TaskType.SCRIPT_QC: 30,
    TaskType.VISUAL_PLAN: 45,
    TaskType.VISUAL_PLAN_QC: 30,
    TaskType.CONTENT_QC: 30,
    TaskType.ASSET_GENERATE_IMAGE: 60,
    TaskType.ASSET_QC: 45,
    TaskType.ASSET_GENERATE_VIDEO: 180,
    TaskType.PUBLISH_PREPARE: 30,
    TaskType.DOUYIN_PUBLISH: 30,
    TaskType.SCRIPT_REVIEW_ASSIST: 45,
    TaskType.FINAL_REVIEW_ASSIST: 60,
    TaskType.PUBLISH_REVIEW_ASSIST: 30,
    TaskType.MEDIA_SOURCE_ASSIST: 45,
}


def lease_minutes_for(session: Session, task_type: TaskType, fallback: int = 45) -> int:
    row = session.get(TaskLeasePolicy, task_type.value)
    if row is not None:
        return row.lease_minutes
    return DEFAULT_LEASE_MINUTES.get(task_type, fallback)


#: 场景分析的规模加成：每 50 个场景额外 +15 分钟（V10 REQ-7）。
SCENES_PER_LEASE_BONUS = 50
LEASE_BONUS_MINUTES = 15


def lease_minutes_for_task(session: Session, task: AgentTask, fallback: int = 45) -> int:
    """按任务规模动态租期：基础值仍由 task_lease_policy 决定。

    百场景级的 scene_analyze 单任务在基础租期内做不完，按场景数追加租期。
    """
    minutes = lease_minutes_for(session, task.task_type, fallback)
    if task.task_type == TaskType.SCENE_ANALYZE:
        scenes = len(task.input_data.get("scenes") or [])
        minutes += LEASE_BONUS_MINUTES * (scenes // SCENES_PER_LEASE_BONUS)
    return minutes


def lease_policy_values(session: Session, fallback: int = 45) -> dict[TaskType, int]:
    return {task_type: lease_minutes_for(session, task_type, fallback) for task_type in TaskType}


def save_lease_policies(
    session: Session,
    values: dict[TaskType, int],
    *,
    actor_id: str = "admin-ui",
) -> int:
    changed: dict[str, int] = {}
    for task_type, minutes in values.items():
        if not 1 <= minutes <= 1440:
            raise ValueError(f"{task_type.value} lease must be between 1 and 1440 minutes")
        existing = session.get(TaskLeasePolicy, task_type.value)
        if existing is not None and existing.lease_minutes == minutes:
            continue
        row = existing or TaskLeasePolicy(task_type=task_type.value, lease_minutes=minutes)
        row.lease_minutes = minutes
        row.updated_at = utc_now()
        session.add(row)
        changed[task_type.value] = minutes
    if changed:
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="task_lease_policy",
                entity_id="batch",
                action="lease_policy_updated",
                metadata_json={"changed": changed},
            )
        )
        session.commit()
    return len(changed)
