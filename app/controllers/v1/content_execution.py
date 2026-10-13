"""Authenticated server-side launch and cooperative stop; no command inputs."""

from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from glance_core.task_service import TaskConflictError
from sqlmodel import Session

from app.business.auth import require_admin
from app.business.conductor_launch import execute_content, execution_view, stop_execution
from app.business.runtime import get_runtime, get_session

router = APIRouter(prefix="/api/v1", tags=["Content"], dependencies=[Depends(require_admin)])


def _empty_request(payload):
    if payload is not None and payload != {}:
        raise HTTPException(422, "CONTENT_EXECUTION_INPUT_FORBIDDEN")


def _execute_permission(payload):
    if payload is None:
        return False
    if not isinstance(payload, dict) or set(payload) - {"allow_local_image_adaptation"}:
        raise HTTPException(422, "CONTENT_EXECUTION_INPUT_FORBIDDEN")
    permission = payload.get("allow_local_image_adaptation", False)
    if type(permission) is not bool:
        raise HTTPException(422, "CONTENT_EXECUTION_PERMISSION_INVALID")
    return permission


def _operation(operation, request, session, content_id, **options):
    try:
        return operation(get_runtime(request), session, content_id, **options)
    except KeyError:
        raise HTTPException(404, "Content execution not found") from None
    except TaskConflictError as exc:
        raise HTTPException(409, str(exc)) from None
    except TimeoutError:
        raise HTTPException(409, "CONTENT_EXECUTION_BUSY") from None


@router.post("/contents/{content_id}/run/execute")
def execute(*, content_id: str, request: Request, session: Annotated[Session, Depends(get_session)], payload: Annotated[object | None, Body()] = None):
    permission = _execute_permission(payload)
    return _operation(execute_content, request, session, content_id, allow_local_image_adaptation=permission)


@router.get("/contents/{content_id}/execution")
def execution(*, content_id: str, request: Request, session: Annotated[Session, Depends(get_session)]):
    return _operation(execution_view, request, session, content_id)


@router.post("/contents/{content_id}/execution/stop")
def stop(*, content_id: str, request: Request, session: Annotated[Session, Depends(get_session)], payload: Annotated[object | None, Body()] = None):
    _empty_request(payload)
    return _operation(stop_execution, request, session, content_id)
