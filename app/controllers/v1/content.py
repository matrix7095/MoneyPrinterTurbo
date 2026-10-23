"""Authenticated content actions; original pipeline owns each stage and its gates."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Content,
    ContentStatus,
    ProductionMode,
    PublicationRecord,
    TaskType,
)
from glance_core.task_service import TaskConflictError
from glance_core.workflow import DuplicateTopicError
from glance_media.executor import NonRetryableMediaError
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.business.auth import require_admin
from app.business.requests import RejectScriptRequest, HostAttachRequest, SpawnIntentRequest, HostObservationRequest, StopRunRequest, ExpectedHostBinding
from app.business.requests import PackagedVisualContextRequest, PendingRenderRevisionRequest, PublishedRevisionRequest, RightsEvidenceRequest, SourceMediaRefreshRequest
from app.business.runtime import get_runtime, get_session
from app.business.bundles import sha256_file
from app.business.quarantine import quarantine_records, task_projection
from app.controllers.v1.agent import content_root

router = APIRouter(
    prefix="/api/v1", tags=["Content"], dependencies=[Depends(require_admin)]
)


class VisualRepairInputBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: str = Field(min_length=1, max_length=100)
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisualRepairResultBinding(VisualRepairInputBinding):
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisualRepairContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    run: ExpectedHostBinding
    recovery: VisualRepairResultBinding
    plan: VisualRepairResultBinding
    qc: VisualRepairInputBinding


class VisualReviewAcceptedBinding(VisualRepairResultBinding):
    attempt: int = Field(ge=1, strict=True)
    raw_file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class VisualReviewDeploymentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    relative_path: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")


class VisualReviewContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    evidence_mode: Literal["accepted_failure_plus_deployed_derivation_v1"]
    run: ExpectedHostBinding
    plan: VisualReviewAcceptedBinding
    qc: VisualReviewAcceptedBinding
    recovery: VisualReviewAcceptedBinding
    corrected_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    historical_deployment: VisualReviewDeploymentEvidence


@router.post("/content-runs/{run_id}/host/attach")
def attach_content_host(*, run_id: str, payload: HostAttachRequest, session: Annotated[Session, Depends(get_session)]):
    from app.business.content_host import attach_host

    try:
        return attach_host(session, run_id, payload)
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.put("/content-runs/{run_id}/intents/{request_id}")
def content_spawn_intent(*, run_id: str, request_id: str, payload: SpawnIntentRequest, session: Annotated[Session, Depends(get_session)]):
    from app.business.content_host import spawn_intent

    try:
        return spawn_intent(session, run_id, request_id, payload)
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/content-runs/{run_id}/intents/{request_id}/observation")
def content_host_observation(*, request: Request, run_id: str, request_id: str, payload: HostObservationRequest, session: Annotated[Session, Depends(get_session)]):
    from app.business.content_host import observe_host

    try:
        return observe_host(
            session, run_id, request_id, payload,
            tasks=get_runtime(request).task_service(session),
        )
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/run")
def start_content_run(*, content_id: str, session: Annotated[Session, Depends(get_session)]):
    from app.business.content_run import start_run

    try:
        return start_run(session, content_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/contents/{content_id}/run")
def content_run(*, content_id: str, session: Annotated[Session, Depends(get_session)]):
    from app.business.content_run import get_run

    try:
        return get_run(session, content_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content run not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/run/stop")
def stop_content_run(*, content_id: str, session: Annotated[Session, Depends(get_session)], payload: StopRunRequest | None = None):
    from app.business.content_run import get_run

    try:
        return get_run(session, content_id, stop=True, expected_binding=payload.expected_binding if payload else None)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content run not found") from None
    except TaskConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/contents")
def list_contents(*, session: Annotated[Session, Depends(get_session)]):
    return [
        row.model_dump(mode="json")
        for row in session.exec(
            select(Content).order_by(Content.created_at.desc())
        ).all()
    ]


@router.get("/contents/{content_id}")
def content_detail(
    *, content_id: str, session: Annotated[Session, Depends(get_session)]
):
    content = session.get(Content, content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="Content not found")
    return {
        "content": content.model_dump(mode="json"),
        "migration_quarantine": quarantine_records(session, content_id=content_id),
        "tasks": [
            task_projection(session, row)
            for row in session.exec(
                select(AgentTask)
                .where(AgentTask.content_id == content_id)
                .order_by(AgentTask.created_at)
            ).all()
        ],
        "artifacts": [
            row.model_dump(mode="json")
            for row in session.exec(
                select(Artifact)
                .where(Artifact.content_id == content_id)
                .order_by(Artifact.created_at)
            ).all()
        ],
        "publications": [
            row.model_dump(mode="json")
            for row in session.exec(
                select(PublicationRecord).where(
                    PublicationRecord.content_id == content_id
                )
            ).all()
        ],
    }


@router.post("/candidates/{candidate_id}/select")
def select_candidate(
    *,
    candidate_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    production_mode: ProductionMode = ProductionMode.FOOTAGE,
):
    from app.business.pipeline import prepare_content

    runtime = get_runtime(request)
    flow = runtime.workflow_service(session)
    existing = session.exec(
        select(Content).where(Content.candidate_id == candidate_id)
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "ALREADY_SELECTED", "content_id": existing.id},
        )
    try:
        content = flow.select_candidate(candidate_id, production_mode=production_mode)
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id="admin-api",
                entity_type="content",
                entity_id=content.id,
                action="manual_candidate_selected",
                metadata_json={"candidate_id": candidate_id},
            )
        )
        session.commit()
        return runtime.run_content_stage(content.id, prepare_content).model_dump(
            mode="json"
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="Candidate not found") from None
    except DuplicateTopicError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "DUPLICATE_TOPIC", "conflicts": exc.conflicts},
        ) from None
    except (ValueError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


def run_stage(
    request: Request, content_id: str, operation, allowed: set[ContentStatus]
):
    runtime = get_runtime(request)

    def guarded(engine, settings, target_id):
        with Session(engine) as session:
            content = session.get(Content, target_id)
            if content is None:
                raise KeyError(target_id)
            if content.business_status not in allowed:
                raise ValueError(
                    f"Content is {content.business_status.value}; action is not available"
                )
        return operation(engine, settings, target_id)

    try:
        content = runtime.run_content_stage(content_id, guarded)
        return content.model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/prepare")
def prepare(*, content_id: str, request: Request):
    from app.business.pipeline import prepare_content

    return run_stage(
        request,
        content_id,
        prepare_content,
        {ContentStatus.PREPARING_CONTEXT, ContentStatus.FAILED},
    )


@router.post("/contents/{content_id}/published-revisions")
def start_published_revision(*, content_id: str, payload: PublishedRevisionRequest, request: Request):
    try:
        return get_runtime(request).start_published_revision(content_id, payload.model_dump())
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/source-media-refresh")
def refresh_source_media(*, content_id: str, payload: SourceMediaRefreshRequest, request: Request):
    from app.business.source_media_refresh import refresh_source_media as refresh

    try:
        return refresh(get_runtime(request), content_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/contents/{content_id}/rights-evidence/preflight")
def rights_evidence_preflight(*, content_id: str, expected_revision_id: str, research_task_id: str,
                             qc_task_id: str, recovery_task_id: str, request: Request):
    from app.business.rights_evidence import preflight

    try:
        return preflight(get_runtime(request), content_id, expected_revision_id, research_task_id, qc_task_id, recovery_task_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/rights-evidence")
def submit_rights_evidence(*, content_id: str, payload: RightsEvidenceRequest, request: Request):
    from app.business.rights_evidence import submit

    try:
        return submit(get_runtime(request), content_id, payload)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/approve-script")
def approve_script(*, content_id: str, request: Request):
    from app.business.pipeline import approve_script

    def approve(engine, settings, target_id):
        return approve_script(engine, settings, target_id, manual_approval=True)

    return run_stage(request, content_id, approve, {ContentStatus.SCRIPT_REVIEW})


@router.post("/contents/{content_id}/reject-script")
def reject_script(
    *,
    content_id: str,
    payload: RejectScriptRequest,
    request: Request,
):
    runtime = get_runtime(request)

    def reject(engine, settings, target_id):
        with Session(engine) as session:
            content = session.get(Content, target_id)
            if content is not None and content.business_status == ContentStatus.PACKAGED:
                from app.business.voice_rebuild import revise_packaged_script

                return revise_packaged_script(session, settings, target_id, payload.note)
            return runtime.workflow_service(session).reject_script(
                target_id, payload.note
            )

    try:
        return runtime.run_content_stage(content_id, reject).model_dump(mode="json")
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/rebuild-voice")
def rebuild_voice(*, content_id: str, request: Request):
    from app.business.voice_rebuild import rebuild_voice

    return run_stage(request, content_id, rebuild_voice, {ContentStatus.PACKAGED})


@router.post("/contents/{content_id}/render")
def render(*, content_id: str, request: Request):
    from app.business.pipeline import render_content

    return run_stage(
        request,
        content_id,
        render_content,
        {ContentStatus.PRODUCING, ContentStatus.FAILED, ContentStatus.FINAL_REVIEW},
    )


@router.post("/contents/{content_id}/request-rerender")
def request_rerender(*, content_id: str, payload: PendingRenderRevisionRequest, request: Request):
    runtime = get_runtime(request)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, runtime)
    try:
        return runtime.request_pending_render_revision(content_id, payload.model_dump())
    except (ValueError, OSError, KeyError, TypeError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/approve-final")
def approve_final(*, content_id: str, request: Request):
    from app.business.pipeline import approve_final

    return run_stage(request, content_id, approve_final, {ContentStatus.FINAL_REVIEW})


@router.post("/contents/{content_id}/image-preparations/{prepared_artifact_id}")
async def register_image_preparation(*, content_id: str, prepared_artifact_id: str, request: Request):
    from starlette.concurrency import run_in_threadpool
    from app.business.image_preparation_evidence import MAX_BODY, register_image_preparation

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/zip":
        raise HTTPException(status_code=415, detail="Image preparation import requires application/zip")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(status_code=413, detail="Image preparation archive exceeds 26 MiB")
        body.extend(chunk)
    runtime = get_runtime(request)

    def register(engine, settings, target_id):
        return register_image_preparation(engine, settings, target_id, prepared_artifact_id, bytes(body))

    try:
        return await run_in_threadpool(runtime.run_content_stage, content_id, register)
    except (ValueError, KeyError, OSError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail="Image preparation evidence verification failed") from exc


@router.post("/contents/{content_id}/image-use-evidence/{prepared_artifact_id}")
async def register_image_use_evidence(*, content_id: str, prepared_artifact_id: str, request: Request):
    from starlette.concurrency import run_in_threadpool
    from app.business.image_use_evidence import MAX_BODY, register_image_use

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="Image-use registration requires application/json")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise HTTPException(status_code=413, detail="Image-use registration exceeds 4 KiB")
        body.extend(chunk)
    runtime = get_runtime(request)

    def register(engine, settings, target_id):
        return register_image_use(engine, settings, target_id, prepared_artifact_id, bytes(body))

    try:
        return await run_in_threadpool(runtime.run_content_stage, content_id, register)
    except (ValueError, KeyError, OSError, TypeError, AttributeError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail="Image-use evidence verification failed") from exc


class RebuildCoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    title: str | None = Field(default=None, min_length=1, max_length=12)


@router.post("/contents/{content_id}/rebuild-cover")
def rebuild_cover(*, content_id: str, payload: RebuildCoverRequest, request: Request):
    from app.business.pipeline import rebuild_cover

    runtime = get_runtime(request)

    def rebuild(engine, settings, target_id):
        with Session(engine) as session:
            content = session.get(Content, target_id)
            if content is None:
                raise KeyError(target_id)
            content_root(content, runtime)
        return rebuild_cover(engine, settings, target_id, actor_id="mpt-admin-api", title=payload.title)

    return run_stage(request, content_id, rebuild, {ContentStatus.PACKAGED, ContentStatus.PUBLISHED})


@router.post("/contents/{content_id}/resume-acceptance")
def resume_acceptance(*, content_id: str, request: Request):
    from app.business.pipeline import resume_content_acceptance

    runtime = get_runtime(request)

    def resume(engine, settings, target_id):
        with Session(engine) as session:
            content = session.get(Content, target_id)
            if content is None:
                raise KeyError(target_id)
            content_root(content, runtime)
            if (content.business_status, content.current_stage) not in {
                (ContentStatus.WAITING_AGENT, "audio_evidence_waiting"),
                (ContentStatus.PRODUCING, "audio_evidence_running"),
            }:
                raise ValueError("Content is not waiting for acceptance audio evidence")
        return resume_content_acceptance(engine, settings, target_id)

    return run_stage(
        request,
        content_id,
        resume,
        {ContentStatus.WAITING_AGENT, ContentStatus.PRODUCING},
    )


@router.post("/contents/{content_id}/restore-visual-plan")
def restore_visual_plan(*, content_id: str, request: Request):
    runtime = get_runtime(request)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, runtime)
    try:
        return runtime.restore_consumed_visual_plan(content_id)
    except (ValueError, OSError, KeyError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/reconcile-visual-repair-context")
def reconcile_visual_repair_context(*, content_id: str, payload: VisualRepairContextRequest, request: Request):
    runtime = get_runtime(request)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, runtime)
    try:
        return runtime.reconcile_visual_repair_context(content_id, payload.model_dump())
    except (ValueError, OSError, KeyError, TypeError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/reconcile-packaged-visual-context")
def reconcile_packaged_visual_context(*, content_id: str, payload: PackagedVisualContextRequest, request: Request):
    runtime = get_runtime(request)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, runtime)
    try:
        return runtime.reconcile_packaged_visual_context(content_id, payload.model_dump())
    except (ValueError, OSError, KeyError, TypeError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/reconcile-visual-plan-review-context")
def reconcile_visual_plan_review_context(*, content_id: str, payload: VisualReviewContextRequest, request: Request):
    runtime = get_runtime(request)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise HTTPException(status_code=404, detail="Content not found")
        content_root(content, runtime)
    try:
        return runtime.reconcile_visual_plan_review_context(content_id, payload.model_dump())
    except (ValueError, OSError, KeyError, TypeError, TaskConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.post("/contents/{content_id}/publications/douyin/publish")
def publish(
    *,
    content_id: str,
    request: Request,
):
    from app.business.pipeline import verify_publish_ready

    runtime = get_runtime(request)
    if TaskType.DOUYIN_PUBLISH not in runtime.supported_task_types:
        raise HTTPException(status_code=503, detail="Publish consumer is not available")

    def queue(engine, settings, target_id):
        with Session(engine) as session:
            # Validate and queue under the same content lock as advance/admin actions.
            verify_publish_ready(session, settings, target_id)
            return runtime.workflow_service(session).queue_douyin_publish(
                target_id, settings=settings, initiated_by="operator", user_clicked=True
            )

    try:
        # PACKAGED alone is not proof: verify fresh v2 QC, rights, lineage and actual files.
        return runtime.run_content_stage(content_id, queue)
    except KeyError:
        raise HTTPException(status_code=404, detail="Content not found") from None
    except (ValueError, TaskConflictError, NonRetryableMediaError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/contents/{content_id}/artifacts/{artifact_id}")
def download_artifact(
    *,
    content_id: str,
    artifact_id: str,
    request: Request,
    session: Annotated[Session, Depends(get_session)],
):
    content = session.get(Content, content_id)
    artifact = session.get(Artifact, artifact_id)
    if content is None or artifact is None or artifact.content_id != content_id:
        raise HTTPException(status_code=404, detail="Artifact not found")
    root = content_root(content, get_runtime(request))
    target = (root / artifact.relative_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="Artifact not found")
    if (
        target.stat().st_size != artifact.size_bytes
        or sha256_file(target) != artifact.sha256
    ):
        raise HTTPException(status_code=409, detail="Artifact hash mismatch")
    return FileResponse(target, filename=target.name, media_type=artifact.mime_type)
