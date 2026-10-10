import logging
import hashlib
from pathlib import Path
import time
from typing import Annotated

from fastapi.responses import FileResponse, Response
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from glance_core.agent_roles import AgentRole, role_spec
from glance_core.models import (
    Artifact,
    Content,
    AgentTask,
    AgentWorker,
    AuditEvent,
    TaskStatus,
    TaskType,
    utc_now,
)
from glance_core.research import find_similar_projects
from glance_core.runtime_config import RuntimeSettingsError, effective_settings
from glance_core.task_instructions import (
    render_capacity_message,
    render_dispatch_task_prompt,
    render_no_task_message,
)
from glance_core.task_service import (
    AgentCapacityError,
    AgentTaskService,
    TaskCapabilityError,
    TaskConflictError,
    TaskValidationError,
    stable_hash,
)
from sqlmodel import Session, select

from app.business.bundles import (
    build_bundle,
    context_files_for,
    scene_evidence_context,
    sha256_file,
)
from app.business.auth import bearer_token, require_admin, require_agent
from app.business.requests import (
    ClaimRequest,
    DispatchRequest,
    ConfirmClaimRequest,
    PublishIntentRequest,
    CompleteRequest,
    FailRequest,
    HeartbeatRequest,
    SimilarityQueryRequest,
    WhoamiRequest,
)
from app.business.runtime import OWNED, get_runtime, get_session, task_service
from app.business.quarantine import checked_runtime_host, deployment_proof

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["Agent"])


@router.post("/agent/whoami")
def whoami(
    *,
    payload: WhoamiRequest,
    authorized_role: Annotated[AgentRole | None, Depends(require_agent)],
    session: Annotated[Session, Depends(get_session)],
):
    if authorized_role is not None and payload.role_id != authorized_role:
        raise HTTPException(status_code=403, detail="Role token does not match role_id")
    selected = authorized_role or payload.role_id
    worker = session.get(AgentWorker, payload.agent_id)
    proof = deployment_proof(session, payload.agent_id)
    if proof and proof.metadata_json.get("device_label"):
        try:
            checked_runtime_host(
                session,
                payload.agent_id,
                payload.runtime.model_dump() if payload.runtime else None,
            )
        except TaskConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    # An old unpinned verification may authenticate a diagnostic self-check
    # before redeployment; it grants no task lease or verified host identity.
    spec = role_spec(selected)
    pending = session.exec(
        select(AgentTask).where(
            AgentTask.status == TaskStatus.PENDING,
            AgentTask.task_type.in_(spec.task_types),
            (AgentTask.required_role == "")
            | (AgentTask.required_role == selected.value),
        )
    ).all()
    session.add(
        AuditEvent(
            actor_type="agent",
            actor_id=payload.agent_id,
            entity_type="agent_worker",
            entity_id=payload.agent_id,
            action="agent_selfcheck",
        )
    )
    session.commit()
    return {
        "role_id": selected.value,
        "allowed_task_types": sorted(task.value for task in spec.task_types),
        "pending_count": len(pending),
        "server_time": utc_now().isoformat(),
        "protocol_version": "1",
        "known_device_label": proof.metadata_json.get("device_label", "")
        if proof
        else worker.device_label
        if worker and not worker.deployment_verified_at
        else "",
    }


@router.post("/agent/tasks/claim")
def claim(
    *,
    payload: ClaimRequest,
    request: Request,
    wait: int = 0,
    authorized_role: Annotated[AgentRole | None, Depends(require_agent)],
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    if (
        authorized_role is not None
        and payload.role_id is not None
        and payload.role_id != authorized_role
    ):
        raise HTTPException(
            status_code=403, detail="Role token cannot claim another Agent role"
        )
    selected = authorized_role or payload.role_id
    if selected is None:
        raise HTTPException(
            status_code=422, detail="必须指定具体业务角色；平台不支持全能 Agent"
        )
    # Only tasks with installed consumers may leave this runtime; extend with each migrated stage.
    capabilities = (
        set(role_spec(selected).task_types) & get_runtime(request).supported_task_types
    )
    runtime = get_runtime(request)
    try:
        current = effective_settings(service.session, runtime.settings)
        runtime.maintain_dispatch()
    except RuntimeSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    deadline = time.monotonic() + max(0, min(wait, current.agent_claim_wait_cap))
    try:
        while True:
            claimed = service.claim(
                agent_id=payload.agent_id,
                capabilities=capabilities,
                role_id=selected,
                runtime_info=payload.runtime.model_dump() if payload.runtime else None,
            )
            if claimed is not None or time.monotonic() >= deadline:
                break
            time.sleep(1)
    except TaskCapabilityError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from None
    except AgentCapacityError as exc:
        return {
            "status": "AT_CAPACITY",
            "active_leases": exc.active_leases,
            "max_inflight": exc.max_inflight,
            "active_task_ids": [
                row.id
                for row in service.session.exec(
                    select(AgentTask).where(
                        AgentTask.assigned_agent_id == payload.agent_id,
                        AgentTask.status == TaskStatus.LEASED,
                    )
                ).all()
            ],
        }
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if claimed is None:
        return {"status": "NO_TASK"}
    task, token = claimed
    mark_owned(service.session, task)
    return {
        "status": "CLAIMED",
        "task_id": task.id,
        "task_type": task.task_type,
        "lease_token": token,
        "lease_expires_at": task.lease_expires_at,
        "bundle_url": f"/api/v1/agent/tasks/{task.id}/bundle",
    }


def mark_owned(session: Session, task: AgentTask):
    owned = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == OWNED
        )
    ).first()
    if owned is None:
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="mpt-business",
                entity_type="agent_task",
                entity_id=task.id,
                action=OWNED,
                metadata_json={"input_hash": task.input_hash},
            )
        )
        session.commit()


@router.post("/agent/dispatch")
def dispatch(
    *,
    payload: DispatchRequest,
    request: Request,
    authorized_role: Annotated[AgentRole | None, Depends(require_agent)],
    service: Annotated[AgentTaskService, Depends(task_service)],
    wait: int | None = None,
):
    if (
        authorized_role is not None
        and payload.role_id is not None
        and payload.role_id != authorized_role
    ):
        raise HTTPException(
            status_code=403, detail="Role token cannot claim another Agent role"
        )
    selected = authorized_role or payload.role_id
    if selected is None:
        raise HTTPException(
            status_code=422, detail="必须指定具体业务角色；平台不支持全能 Agent"
        )
    runtime = get_runtime(request)
    try:
        current = effective_settings(service.session, runtime.settings)
        runtime.maintain_dispatch()
    except RuntimeSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None
    capabilities = set(role_spec(selected).task_types) & runtime.supported_task_types
    if payload.task_type is not None:
        if payload.task_type not in capabilities:
            raise HTTPException(
                status_code=422,
                detail="Task type has no installed consumer for this role",
            )
        capabilities = {payload.task_type}
    effective_wait = current.agent_claim_wait_cap if wait is None else wait
    deadline = time.monotonic() + max(
        0, min(effective_wait, current.agent_claim_wait_cap)
    )
    try:
        while True:
            claimed = service.claim(
                agent_id=payload.agent_id,
                capabilities=capabilities,
                role_id=selected,
                runtime_info=payload.runtime.model_dump() if payload.runtime else None,
                reserve_seconds=current.agent_reserve_seconds,
                task_id=payload.task_id,
            )
            if claimed is not None or time.monotonic() >= deadline:
                break
            time.sleep(1)
    except TaskCapabilityError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from None
    except AgentCapacityError:
        active = [
            row.id
            for row in service.session.exec(
                select(AgentTask).where(
                    AgentTask.assigned_agent_id == payload.agent_id,
                    AgentTask.status == TaskStatus.LEASED,
                )
            ).all()
        ]
        return {
            "should_run": False,
            "reason": "AT_CAPACITY",
            "message": render_capacity_message(active),
            "active_task_ids": active,
        }
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if claimed is None:
        return {
            "should_run": False,
            "reason": "NO_TASK",
            "message": render_no_task_message(),
        }
    task, token = claimed
    mark_owned(service.session, task)
    workspace_root = payload.workspace_dir.rstrip("/\\") or "<workspace>"
    workspace_dir = f"{workspace_root}/{task.id}"
    return {
        "should_run": True,
        "prompt": render_dispatch_task_prompt(
            task_id=task.id,
            task_type=task.task_type.value,
            skill_name=role_spec(selected).skill_name,
            workspace_dir=workspace_dir,
            launcher=payload.launcher or "<启动器>",
        ),
        "task_id": task.id,
        "task_type": task.task_type,
        "lease_token": token,
        "lease_expires_at": task.lease_expires_at,
        "workspace_dir": workspace_dir,
        "bundle_url": f"/api/v1/agent/tasks/{task.id}/bundle",
    }


@router.get("/agent/tasks/{task_id}/bundle")
def bundle(
    *,
    task_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    session = service.session
    try:
        task = service.require_lease(task_id, bearer_token(authorization))
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if task is not None and task.content_id:
        content = session.get(Content, task.content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, get_runtime(request))
    result = build_bundle(
        task_id,
        bearer_token(authorization),
        service,
        session,
        get_runtime(request).workflow_service(session),
    )
    if task.content_id:
        for entry in result["context_manifest"]:
            files = context_files_for(task, session)
            if entry["index"] < len(files):
                verify_context_artifact(session, content, files[entry["index"]])
    return result


@router.post("/agent/tasks/{task_id}/heartbeat")
def heartbeat(
    *,
    task_id: str,
    payload: HeartbeatRequest | None = None,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.heartbeat(
            task_id,
            bearer_token(authorization),
            payload.runtime.model_dump() if payload and payload.runtime else None,
        )
    except (KeyError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {"status": task.status, "lease_expires_at": task.lease_expires_at}


@router.post("/agent/similar")
def similar(
    *,
    payload: SimilarityQueryRequest,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.require_lease(
            payload.task_id,
            bearer_token(authorization),
            renew=True,
            implicit_confirm=True,
            agent_id=payload.agent_id,
        )
    except (KeyError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    direction = payload.direction_key or str(task.input_data.get("direction_key") or "")
    matches = find_similar_projects(
        service.session,
        query=payload.query,
        url=payload.url,
        direction_key=direction,
        limit=payload.limit,
    )
    service.session.add(
        AuditEvent(
            actor_type="agent",
            actor_id=payload.agent_id,
            entity_type="agent_task",
            entity_id=task.id,
            action="similarity_checked",
            metadata_json={
                "query_hash": stable_hash(payload.query),
                "direction_key": direction,
                "match_count": len(matches),
            },
        )
    )
    service.session.commit()
    return {
        "status": "SIMILAR_FOUND" if matches else "NO_SIMILAR",
        "direction_key": direction,
        "matches": matches,
    }


@router.post("/agent/tasks/{task_id}/artifacts")
async def upload_generated_artifact(
    task_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()],
    service: Annotated[AgentTaskService, Depends(task_service)],
    session: Annotated[Session, Depends(get_session)],
) -> dict:
    try:
        task = service.require_lease(
            task_id, bearer_token(authorization), renew=True, implicit_confirm=True
        )
    except (KeyError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    rules = {
        TaskType.ASSET_GENERATE_IMAGE: (
            {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"},
            25 * 1024 * 1024,
            "agent_generated_image",
        ),
        TaskType.ASSET_GENERATE_VIDEO: (
            {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"},
            500 * 1024 * 1024,
            "agent_generated_video",
        ),
        TaskType.DOUYIN_PUBLISH: (
            {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"},
            10 * 1024 * 1024,
            "publish_evidence",
        ),
    }
    if task.task_type not in rules or not task.content_id:
        raise HTTPException(
            status_code=409, detail="Task does not accept Agent artifacts"
        )
    mime_map, max_bytes, artifact_type = rules[task.task_type]
    mime_type = (
        (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    )
    extension = mime_map.get(mime_type)
    if extension is None:
        raise HTTPException(status_code=415, detail="Unsupported Agent artifact type")
    declared_size = request.headers.get("content-length", "").lstrip("0") or "0"
    if (
        declared_size.isascii()
        and declared_size.isdigit()
        and (len(declared_size) > len(str(max_bytes)) or int(declared_size) > max_bytes)
    ):
        raise HTTPException(
            status_code=413, detail="Agent artifact is empty or too large"
        )
    # Check actual streamed bytes before retaining each chunk; Content-Length alone
    # cannot protect this boundary from a chunked or dishonest request.
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise HTTPException(
                status_code=413, detail="Agent artifact is empty or too large"
            )
        if chunk:
            chunks.append(chunk)
    if not size:
        raise HTTPException(
            status_code=413, detail="Agent artifact is empty or too large"
        )
    body = b"".join(chunks)
    if mime_type == "image/png" and not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise HTTPException(status_code=422, detail="Invalid PNG signature")
    if mime_type == "video/mp4" and b"ftyp" not in body[:32]:
        raise HTTPException(status_code=422, detail="Invalid MP4 signature")
    if mime_type == "image/jpeg" and not body.startswith(b"\xff\xd8\xff"):
        raise HTTPException(status_code=422, detail="Invalid JPEG signature")
    if mime_type == "image/webp" and not (
        body.startswith(b"RIFF") and body[8:12] == b"WEBP"
    ):
        raise HTTPException(status_code=422, detail="Invalid WebP signature")

    content = session.get(Content, task.content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    digest = hashlib.sha256(body).hexdigest()
    root = content_root(content, get_runtime(request))
    relative_path = Path("agent-assets") / task.id / f"{digest}{extension}"
    target = (root / relative_path).resolve()
    if not target.is_relative_to(root):
        raise HTTPException(status_code=400, detail="Invalid artifact path")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(body)
    elif sha256_file(target) != digest:
        raise HTTPException(status_code=409, detail="Stored artifact hash mismatch")
    existing = session.exec(
        select(Artifact).where(
            Artifact.content_id == task.content_id,
            Artifact.sha256 == digest,
            Artifact.producer_stage == task.id,
        )
    ).first()
    artifact = existing or Artifact(
        content_id=task.content_id,
        artifact_type=artifact_type,
        relative_path=relative_path.as_posix(),
        sha256=digest,
        size_bytes=len(body),
        mime_type=mime_type,
        producer_stage=task.id,
    )
    if existing is None:
        session.add(artifact)
        session.add(
            AuditEvent(
                actor_type="agent",
                actor_id=task.assigned_agent_id or "unknown-agent",
                entity_type="artifact",
                entity_id=artifact.id,
                action="generated_artifact_uploaded",
                metadata_json={"task_id": task.id, "mime_type": mime_type},
            )
        )
        session.commit()
        session.refresh(artifact)
    return {
        "status": "UPLOADED",
        "artifact_id": artifact.id,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "mime_type": artifact.mime_type,
    }


@router.post("/agent/tasks/{task_id}/complete")
def complete(
    *,
    task_id: str,
    payload: CompleteRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.complete(
            task_id=task_id,
            lease_token=bearer_token(authorization),
            result=payload.result,
            agent_id=payload.agent_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except TaskValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors) from None
    try:
        get_runtime(request).consume_agent(task.id)
    except Exception:
        logger.exception("Accepted Agent result is pending consumption: %s", task.id)
        return {"status": task.status, "task_id": task.id, "advanced": "PENDING"}
    return {"status": task.status, "task_id": task.id, "advanced": "CONSUMED"}


@router.post("/agent/tasks/{task_id}/fail")
def fail(
    *,
    task_id: str,
    payload: FailRequest,
    request: Request,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.fail(
            task_id=task_id,
            lease_token=bearer_token(authorization),
            agent_id=payload.agent_id,
            retryable=payload.retryable,
            code=payload.error_code,
            message=payload.message,
            attempt=payload.attempt,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    recovery = {"recovery_status": "NOT_REQUIRED", "recovery_task_id": None}
    if task.status == TaskStatus.FAILED:
        try:
            recovery = get_runtime(request).schedule_failure_recovery(task.id)
        except Exception:
            # The accepted failure remains committed if recovery cannot be admitted yet.
            logger.exception("Terminal Agent failure pending recovery: %s", task.id)
            recovery["recovery_status"] = "PENDING"
    return {"status": task.status, "task_id": task.id, **recovery}


@router.post("/agent/tasks/{task_id}/retry-recovery", dependencies=[Depends(require_admin)])
def retry_failure_recovery(*, task_id: str, request: Request):
    try:
        return get_runtime(request).schedule_failure_recovery(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except RuntimeSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@router.post("/agent/tasks/{task_id}/content-qc-recovery", dependencies=[Depends(require_admin)])
def retry_content_qc_recovery(*, task_id: str, request: Request):
    try:
        return get_runtime(request).schedule_content_qc_recovery(task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except RuntimeSettingsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


def content_root(content: Content, runtime) -> Path:
    root = Path(content.content_dir).resolve()
    if not root.is_relative_to(runtime.settings.content_dir.resolve()):
        raise HTTPException(
            status_code=409, detail="Content storage is outside business data_dir"
        )
    return root


def verify_context_artifact(session: Session, content: Content, relative_path: str):
    artifact = session.exec(
        select(Artifact)
        .where(
            Artifact.content_id == content.id,
            Artifact.relative_path == relative_path,
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ).first()
    if artifact is not None:
        target = (Path(content.content_dir).resolve() / relative_path).resolve()
        if (
            not target.is_file()
            or target.stat().st_size != artifact.size_bytes
            or sha256_file(target) != artifact.sha256
        ):
            raise HTTPException(
                status_code=409, detail="Context artifact hash mismatch"
            )


@router.get("/agent/tasks/{task_id}/context/{file_index}")
def context_file(
    *,
    task_id: str,
    file_index: int,
    request: Request,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.require_lease(task_id, bearer_token(authorization))
    except (KeyError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    session = service.session
    files = context_files_for(task, session)
    if file_index >= len(files):
        extra = scene_evidence_context(
            task, session, get_runtime(request).workflow_service(session)
        )
        index = file_index - len(files)
        if 0 <= index < len(extra):
            return Response(extra[index][1], media_type="application/json")
    if file_index < 0 or file_index >= len(files) or not task.content_id:
        raise HTTPException(status_code=404, detail="Context file not found")
    content = session.get(Content, task.content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    root = content_root(content, get_runtime(request))
    target = (root / files[file_index]).resolve()
    if root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="Context file not found")
    verify_context_artifact(session, content, files[file_index])
    return FileResponse(target, filename=target.name)


@router.post("/agent/tasks/{task_id}/confirm-claim")
def confirm_claim(
    *,
    task_id: str,
    payload: ConfirmClaimRequest,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        task = service.confirm_claim(
            task_id=task_id,
            lease_token=bearer_token(authorization),
            agent_id=payload.agent_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return {
        "status": "CONFIRMED",
        "task_id": task.id,
        "attempt": task.attempt,
        "lease_expires_at": task.lease_expires_at,
    }


@router.post("/agent/tasks/{task_id}/publish-intent")
def publish_intent(
    *,
    request: Request,
    task_id: str,
    payload: PublishIntentRequest,
    authorization: str | None = Header(default=None),
    service: Annotated[AgentTaskService, Depends(task_service)],
):
    try:
        return service.register_publish_intent(
            task_id=task_id,
            settings=get_runtime(request).settings,
            lease_token=bearer_token(authorization),
            agent_id=payload.agent_id,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Task not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
