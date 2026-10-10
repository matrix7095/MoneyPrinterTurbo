"""Legacy response projections of durable, content-bound semantic Agent results."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, Response
from glance_core.models import AgentTask, AuditEvent, Content, TaskStatus, TaskType
from glance_core.task_schemas import result_model_for
from glance_core.task_service import stable_hash
from pydantic import ValidationError
from sqlmodel import Session, select

from app.business.auth import require_admin
from app.business.runtime import AGENT_CONSUMED, get_runtime
from app.controllers import base
from app.controllers.v1.base import new_router
from app.models.exception import HttpException
from app.models.schema import (
    VideoScriptRequest,
    VideoScriptResponse,
    VideoSocialMetadataRequest,
    VideoSocialMetadataResponse,
    VideoTermsRequest,
    VideoTermsResponse,
)
from app.services import llm
from app.utils import utils


def authorize_semantic(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header(alias="x-api-key")] = None,
):
    if llm.is_upstream_compatibility_enabled():
        return base.verify_token(request, x_api_key)
    return require_admin(request, authorization)


router = new_router(dependencies=[Depends(authorize_semantic)])
SCRIPT_TASKS = (TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC)


def _conflict(code: str, message: str):
    raise HTTPException(status_code=409, detail={"code": code, "message": message})


def _accepted_result(session, task):
    if task is None or task.status != TaskStatus.COMPLETED:
        return None
    if stable_hash(task.input_data) != task.input_hash:
        _conflict("INPUT_CHANGED", "Accepted Agent task input hash changed")
    marker = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED
        )
    ).first()
    if marker is None:
        return None  # Accepted submission is still waiting for the pipeline consumer.
    if marker.metadata_json.get("result_hash") != stable_hash(task.result_data):
        _conflict("RESULT_CHANGED", "Consumed Agent result hash changed")
    try:
        return result_model_for(task.task_type, task.schema_version).model_validate(
            task.result_data
        ).model_dump(mode="json")
    except ValidationError:
        _conflict("INVALID_RESULT", "Persisted Agent result fails its schema")


def _assert_lineage(task, expected):
    declared = (task.input_data or {}).get("upstream_task_ids") or {}
    if any(declared.get(key) != value.id for key, value in expected.items()):
        _conflict("STALE_LINEAGE", "Result does not reference the current content tasks")


def _project_result(request, content_id, operation, amount=5):
    if not content_id or not content_id.strip():
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CONTENT_CONTEXT_REQUIRED",
                "message": "Select a discovery candidate and supply its content_id",
            },
        )
    runtime = get_runtime(request)

    def project(engine, settings, target_id):
        with Session(engine) as session:
            content = session.get(Content, target_id)
            if content is None:
                raise HTTPException(status_code=404, detail="Content not found")
            # Latest across ALL statuses: a replacement must hide an older completed draft.
            rows = session.exec(
                select(AgentTask)
                .where(AgentTask.content_id == target_id)
                .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            ).all()
            current = {}
            for task in rows:
                current.setdefault(task.task_type, task)
            required = (*SCRIPT_TASKS, TaskType.PUBLISH_PREPARE) if operation == "metadata" else SCRIPT_TASKS
            accepted = {kind: _accepted_result(session, current.get(kind)) for kind in required}
            task_ids = {kind.value: current[kind].id for kind in required if kind in current}
            if all(accepted.values()):
                research = current[TaskType.CONTENT_RESEARCH]
                script = current[TaskType.SHORT_SCRIPT]
                qc = current[TaskType.SCRIPT_QC]
                _assert_lineage(script, {"research": research})
                _assert_lineage(qc, {"research": research, "script": script})
                if not accepted[TaskType.SCRIPT_QC]["passed"]:
                    _conflict("SCRIPT_QC_FAILED", "Script quality review did not pass")
                if operation == "script":
                    result = {"video_script": accepted[TaskType.SHORT_SCRIPT]["full_narration"]}
                elif operation == "terms":
                    # Real Agent-authored script requirements; never infer words from purpose.
                    terms = [
                        keyword
                        for requirement in accepted[TaskType.SHORT_SCRIPT]["visual_requirements"]
                        for keyword in requirement["keywords"]
                        if keyword.strip()
                    ]
                    if not terms:
                        _conflict("TERMS_UNAVAILABLE", "Approved script declares no material keywords")
                    result = {"video_terms": terms[:amount]}
                else:
                    task = current[TaskType.PUBLISH_PREPARE]
                    if (task.input_data or {}).get("upstream_task_ids"):
                        _assert_lineage(task, {"script": script, "research": research})
                    elif any(
                        stable_hash(task.input_data.get(key)) != stable_hash(upstream.result_data)
                        for key, upstream in {"script": script, "research": research}.items()
                    ):
                        _conflict("STALE_LINEAGE", "Metadata snapshot does not match the current script and research")
                    metadata = accepted[TaskType.PUBLISH_PREPARE]
                    result = {"title": metadata["title"], "caption": metadata["description"], "hashtags": metadata["tags"]}
                return 200, {
                    **result, "content_id": target_id, "task_ids": task_ids,
                    "result_origin": "short_script.visual_requirements" if operation == "terms" else required[-1].value if operation == "metadata" else "short_script.full_narration",
                }
            # Failed QC is a concrete refusal even if another consumer is pending.
            qc = current.get(TaskType.SCRIPT_QC)
            qc_refs = (qc.input_data or {}).get("upstream_task_ids", {}) if qc else {}
            qc_is_current = all(kind in current and qc_refs.get(key) == current[kind].id for key, kind in (("script", TaskType.SHORT_SCRIPT), ("research", TaskType.CONTENT_RESEARCH)))
            if qc_is_current and accepted.get(TaskType.SCRIPT_QC) and not accepted[TaskType.SCRIPT_QC]["passed"]:
                _conflict("SCRIPT_QC_FAILED", "Script quality review did not pass")
            if content.business_status.value in {"FAILED", "REJECTED"}:
                _conflict("CONTENT_BLOCKED", "Review the content error and use its recovery action")
            if any(current[kind].status in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.REJECTED} for kind in required if kind in current):
                _conflict("TASK_BLOCKED", "Current semantic task needs review or recovery")
            if operation == "metadata" and TaskType.PUBLISH_PREPARE not in current and not settings.publish_prepare_enabled:
                _conflict("METADATA_UNAVAILABLE", "Publishing metadata Agent preparation is not enabled")
            next_actions = {
                "PREPARING_CONTEXT": "prepare",
                "SCRIPT_REVIEW": "approve-script after reviewing script and QC",
                "PRODUCING": "render after visual and asset QC",
                "FINAL_REVIEW": "approve-final after reviewing the actual video and QC",
            }
            return 202, {
                "content_id": target_id, "business_status": content.business_status.value,
                "stage": content.current_stage, "status": "PENDING", "task_ids": task_ids,
                "tasks": [{"task_id": row.id, "task_type": row.task_type.value, "status": row.status.value} for row in rows],
                "required_tasks": [kind.value for kind in required if not accepted[kind]],
                "poll_url": f"/api/v1/contents/{target_id}",
                "next_action": next_actions.get(content.business_status.value, "Wait for the current Agent tasks and consumption; repeat this request"),
            }

    # Same serialization boundary as pipeline consumers; a read cannot race a rewrite.
    try:
        return runtime.run_content_stage(content_id, project)
    except TimeoutError:
        raise HTTPException(status_code=409, detail="Content stage is busy; retry later") from None


def _business_response(request, response, content_id, operation, amount=5):
    status, data = _project_result(request, content_id, operation, amount)
    response.status_code = status
    return utils.get_response(status, data)


@router.post("/scripts", response_model=VideoScriptResponse, response_model_exclude_none=True, responses={202: {"model": VideoScriptResponse, "description": "Content Agent tasks or consumption pending"}})
def generate_video_script(request: Request, response: Response, body: VideoScriptRequest):
    if not llm.is_upstream_compatibility_enabled():
        return _business_response(request, response, body.content_id, "script")
    video_script = llm.generate_script(
        video_subject=body.video_subject, language=body.video_language,
        paragraph_number=body.paragraph_number, video_script_prompt=body.video_script_prompt,
        custom_system_prompt=body.custom_system_prompt,
    )
    if not video_script or video_script.startswith("Error: "):
        raise HttpException(task_id=base.get_task_id(request), status_code=503, message="script generation is unavailable")
    return utils.get_response(200, {"video_script": video_script})


@router.post("/terms", response_model=VideoTermsResponse, response_model_exclude_none=True, responses={202: {"model": VideoTermsResponse, "description": "Current script and QC pending"}})
def generate_video_terms(request: Request, response: Response, body: VideoTermsRequest):
    if not llm.is_upstream_compatibility_enabled():
        return _business_response(request, response, body.content_id, "terms", body.amount)
    video_terms = llm.generate_terms(
        video_subject=body.video_subject, video_script=body.video_script,
        amount=body.amount, match_script_order=body.match_materials_to_script,
    )
    if not video_terms:
        raise HttpException(task_id=base.get_task_id(request), status_code=503, message="search term generation is unavailable")
    return utils.get_response(200, {"video_terms": video_terms})


@router.post("/social-metadata", response_model=VideoSocialMetadataResponse, response_model_exclude_none=True, responses={202: {"model": VideoSocialMetadataResponse, "description": "Content publishing metadata pending"}})
def generate_video_social_metadata(request: Request, response: Response, body: VideoSocialMetadataRequest):
    if not llm.is_upstream_compatibility_enabled():
        return _business_response(request, response, body.content_id, "metadata")
    metadata = llm.generate_social_metadata(
        video_subject=body.video_subject, video_script=body.video_script,
        language=body.language, platform=body.platform,
    )
    return utils.get_response(200, metadata)
