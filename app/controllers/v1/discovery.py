from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from glance_collector.service import CollectionService
from glance_core.models import (
    AgentTask,
    Candidate,
    CollectionRun,
    Source,
    SourceType,
    TaskStatus,
    TaskType,
)
from glance_core.task_service import TaskConflictError, stable_hash
from sqlmodel import Session, select

from app.business.auth import require_admin
from app.business.requests import SearchTaskRequest
from app.business.quarantine import assert_allowed, quarantine_records, task_projection
from app.business.runtime import get_runtime, get_session

router = APIRouter(
    prefix="/api/v1", tags=["Discovery"], dependencies=[Depends(require_admin)]
)


@router.post("/discovery/search-tasks")
def create_search(
    *,
    payload: SearchTaskRequest,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    runtime = get_runtime(request)
    # A durable source is required by the upstream consumer; one-off tasks with source_id=''
    # are accepted upstream but cannot reach CollectionService.ingest_agent_result.
    source_id = f"src_mpt_search_{stable_hash(payload.direction)[:24]}"
    previous = session.exec(
        select(AgentTask)
        .where(
            AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
            AgentTask.input_data["source_id"].as_string() == source_id,
        )
        .order_by(AgentTask.created_at.desc())
    ).first()
    if previous is not None:
        try:
            assert_allowed(session, task_id=previous.id)
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    source = session.get(Source, source_id)
    if source is None:
        source = Source(
            id=source_id, name=payload.direction, source_type=SourceType.AGENT, url=""
        )
    source.enabled = True
    source.config_json = {
        "direction_key": payload.direction,
        "max_candidates": payload.max_candidates,
        "scope": "\n".join(
            part for part in (payload.purpose, payload.query) if part.strip()
        ),
    }
    session.add(source)
    session.commit()
    collection = CollectionService(session, runtime.settings)
    run = collection.collect_source(source.id)
    if run.status == "FAILED":
        raise HTTPException(status_code=409, detail=run.error_message)
    if run.status == "SKIPPED" and run.error_code != "DIRECTION_BUSY":
        raise HTTPException(status_code=409, detail=run.error_code)
    task = session.exec(
        select(AgentTask)
        .where(
            AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
            AgentTask.input_data["source_id"].as_string() == source.id,
        )
        .order_by(AgentTask.created_at.desc())
    ).first()
    if task is None:
        raise HTTPException(
            status_code=409, detail=run.error_code or "Discovery was not scheduled"
        )
    return {
        "status": task.status.value,
        "task_id": task.id,
        "task_type": task.task_type.value,
        "source_id": source.id,
        "collection_run_id": task.input_data.get("collection_run_id"),
    }


@router.get("/candidates")
def list_candidates(*, session: Annotated[Session, Depends(get_session)]):
    return [
        row.model_dump(mode="json")
        for row in session.exec(
            select(Candidate).order_by(Candidate.created_at.desc())
        ).all()
    ]


@router.get("/collection-runs")
def list_collection_runs(*, session: Annotated[Session, Depends(get_session)]):
    return [
        row.model_dump(mode="json")
        for row in session.exec(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(100)
        ).all()
    ]


@router.get("/discovery/tasks/{task_id}")
def get_task(*, task_id: str, session: Annotated[Session, Depends(get_session)]):
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        **task_projection(session, task),
        "migration_quarantine": quarantine_records(session, task_id=task.id),
    }


@router.post("/discovery/tasks/{task_id}/consume")
def retry_consumption(
    *, task_id: str, request: Request, session: Annotated[Session, Depends(get_session)]
):
    task = session.get(AgentTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.task_type != TaskType.SOURCE_DISCOVERY:
        raise HTTPException(status_code=409, detail="Task is not discovery")
    if task.status != TaskStatus.COMPLETED:
        raise HTTPException(
            status_code=409, detail="Discovery result is not accepted yet"
        )
    try:
        consumed = get_runtime(request).consume_discovery(task_id)
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"task_id": task.id, "consumed": consumed}
