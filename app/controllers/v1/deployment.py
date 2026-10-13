from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from glance_core.agent_roles import AgentRole
from glance_core.runtime_config import RuntimeSettingsError, effective_settings
from glance_core.scheduler_profiles import load_scheduler_config, save_scheduler_config
from glance_core.security import verify_scheduler_token
from glance_core.task_service import TaskConflictError
from sqlmodel import Session

from app.business.auth import bearer_token, require_admin, require_agent
from app.business.requests import DeploymentReportRequest
from app.business.runtime import get_runtime, get_session
from app.business.scheduler import register_deployment, scheduler_plan

router = APIRouter(prefix="/api/v1", tags=["Deployment"])


@router.post("/agent/deployment")
def deployment(
    *,
    payload: DeploymentReportRequest,
    request: Request,
    authorized_role: Annotated[AgentRole | None, Depends(require_agent)],
    session: Annotated[Session, Depends(get_session)],
):
    if authorized_role is None or authorized_role != payload.role_id:
        raise HTTPException(
            status_code=403, detail="Deployment requires the exact role token"
        )
    return register_deployment(get_runtime(request), session, payload)


def require_scheduler(
    request: Request,
    scheduler_id: str,
    authorization: str | None = Header(default=None),
):
    token = bearer_token(authorization)
    root = get_runtime(request).settings.bootstrap_agent_token.get_secret_value()
    if not token.isascii() or not verify_scheduler_token(root, token, scheduler_id):
        raise HTTPException(status_code=401, detail="Invalid scoped scheduler token")


@router.get("/agent/scheduler/plan")
def plan(
    *,
    request: Request,
    scheduler_id: str,
    contract_version: int = Query(...),
    run_id: str | None = Query(default=None, pattern=r"^run_[0-9a-f]{32}$"),
    authorized: Annotated[None, Depends(require_scheduler)],
    session: Annotated[Session, Depends(get_session)],
):
    if contract_version != 2:
        raise HTTPException(status_code=422, detail="MPT requires scheduler contract 2")
    runtime = get_runtime(request)
    try:
        effective_settings(session, runtime.settings)
        if run_id is None:
            runtime.maintain_dispatch()
        else:
            runtime.maintain_content_dispatch(run_id)
        return scheduler_plan(runtime, session, scheduler_id, run_id=run_id)
    except RuntimeSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    except KeyError:
        raise HTTPException(
            status_code=404, detail="Scheduler not configured"
        ) from None
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/agent/scheduler/config", dependencies=[Depends(require_admin)])
def config(*, session: Annotated[Session, Depends(get_session)]):
    return load_scheduler_config(session)


@router.put("/agent/scheduler/config", dependencies=[Depends(require_admin)])
def update_config(*, payload: dict, session: Annotated[Session, Depends(get_session)]):
    try:
        return {
            "changed": save_scheduler_config(session, payload, actor_id="mpt-admin-api")
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
