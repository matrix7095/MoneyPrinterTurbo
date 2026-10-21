"""Explicitly retire an unoffered review before a changed renderer creates new media."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

from glance_core.content_acceptance import _unchanged_material_task, acceptance_subject
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus,
    PublicationRecord, SchedulerOffer, StageRun, StageStatus, TaskStatus, TaskType, utc_now,
)
from glance_core.runtime_config import effective_settings
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.voice_generation import script_binding, verified_artifact_bytes, verify_voice_generation
from glance_media.executor import sha256_file
from sqlmodel import select

REQUESTED = "mpt_pending_render_revision_requested"


def _run(session, content_id, expected):
    from app.business.conductor_launch import _load
    from app.business.content_host import host_live
    from app.business.content_run import load_run

    run, execution = load_run(session), _load(session, content_id)
    if (run is None or run.content_id != content_id or run.state != "stopped" or not run.stop_requested
            or {field: getattr(run, field) for field in expected["run"]} != expected["run"]
            or host_live(run) or any(item.get("state") != "TERMINAL" for item in run.intents.values())
            or execution is None or execution.state != "FINISHED" or execution.exit_code != 0
            or execution.error_code or not execution.exited_at
            or execution.run_id != run.run_id or execution.generation != run.generation
            or stable_hash(execution.model_dump()) != expected["execution_sha256"]):
        raise TaskConflictError("RERENDER_RUN_NOT_DRAINED_OR_CHANGED")


def _material(session, content, expected):
    artifact = session.get(Artifact, expected["final_artifact_id"])
    if (artifact is None or artifact.content_id != content.id or artifact.artifact_type != "final_video"
            or artifact.sha256 != expected["final_sha256"]):
        raise TaskConflictError("RERENDER_FINAL_CHANGED")
    verified_artifact_bytes(content, artifact)
    root = Path(content.content_dir).resolve()
    relative = Path(artifact.relative_path)
    if (len(relative.parts) != 4 or relative.parts[:2] != ("render", "revisions")
            or relative.parts[2] in {".", ".."} or relative.name != "final.mp4"):
        raise TaskConflictError("RERENDER_FINAL_NOT_IMMUTABLE")
    path = (root / relative).resolve()
    if (not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != artifact.size_bytes
            or sha256_file(path) != artifact.sha256):
        raise TaskConflictError("RERENDER_FINAL_BYTES_CHANGED")
    directory = path.parent
    outputs = directory / "outputs.json"
    frozen = json.loads(outputs.read_bytes())
    if (not isinstance(frozen, dict)
            or set(frozen) not in ({"final.mp4", "preview.mp4"},
                                   {"final.mp4", "preview.mp4", "render-parameters.json"})
            or frozen["final.mp4"] != artifact.sha256
            or any(not (directory / name).is_file()
                   or sha256_file(directory / name) != sha
                   for name, sha in frozen.items())):
        raise TaskConflictError("RERENDER_OUTPUTS_CHANGED")
    return artifact, relative.parts[2], hashlib.sha256(outputs.read_bytes()).hexdigest()


def request_rerender(runtime, session, content_id, expected):
    from app.business import media_executor, pipeline
    from app.business.quarantine import assert_allowed
    from app.business.runtime import _require_current_revision, _require_owned_input

    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    key = stable_hash({"version": 1, "content_id": content_id, "qc_task_id": expected["qc"]["task_id"]})
    prior = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id, AuditEvent.action == REQUESTED,
    )).all()
    prior = [row for row in prior if row.metadata_json.get("key") == key]
    _run(session, content_id, expected)
    artifact, revision, outputs_hash = _material(session, content, expected)
    task = session.get(AgentTask, expected["qc"]["task_id"])
    if prior:
        proof = prior[0].metadata_json
        if (len(prior) != 1 or prior[0].actor_type != "admin" or prior[0].actor_id != "mpt-admin-api"
                or proof.get("expected") != expected or task is None
                or task.status != TaskStatus.CANCELLED or stable_hash(task.model_dump(mode="json")) != proof.get("cancelled_task_sha256")
                or proof.get("outputs_sha256") != outputs_hash):
            raise TaskConflictError("RERENDER_REPLAY_CHANGED")
        receipt = session.get(Artifact, proof.get("acceptance_receipt_artifact_id"))
        cancellations = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id, AuditEvent.action == "cancelled",
        )).all()
        if (receipt is None or receipt.content_id != content_id or receipt.artifact_type != "acceptance_input"
                or json.loads(verified_artifact_bytes(content, receipt)) != task.input_data
                or len(cancellations) != 1 or cancellations[0].actor_type != "admin"
                or cancellations[0].actor_id != "mpt-admin-api" or cancellations[0].metadata_json != {}):
            raise TaskConflictError("RERENDER_REPLAY_MATERIAL_MISSING")
        # The global request is replaced by the later normal render; this is only a historical receipt.
        return {**proof["response"], "replayed": True}

    if (content.business_status != ContentStatus.WAITING_AGENT or content.current_stage != "content_qc"
            or content.error_code or content.error_message):
        raise TaskConflictError("RERENDER_CONTENT_NOT_PENDING_QC")
    latest = session.exec(select(AgentTask).where(
        AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    if (task is None or task != latest or task.content_id != content_id or task.task_type != TaskType.CONTENT_QC
            or task.schema_version != "3" or task.status != TaskStatus.PENDING or task.attempt != 0
            or task.input_hash != expected["qc"]["input_hash"] or stable_hash(task.input_data) != task.input_hash
            or task.result_data is not None or any(getattr(task, field) is not None for field in (
                "assigned_agent_id", "lease_token_hash", "lease_expires_at", "attempt_started_at",
                "last_heartbeat_at", "started_at", "completed_at", "error_code", "error_message", "not_before"))):
        raise TaskConflictError("RERENDER_QC_NOT_PRISTINE_CURRENT_PENDING")
    assert_allowed(session, task_id=task.id)
    _require_current_revision(session, task)
    history = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
    )).all()
    if any(row.action == "mpt_task_owned" for row in history):
        _require_owned_input(session, task)
    created = [row for row in history if row.action == "created"]
    if (len(created) != 1 or created[0].actor_type != "system" or created[0].actor_id != "task-service"
            or created[0].metadata_json != {}):
        raise TaskConflictError("RERENDER_QC_CREATION_CHANGED")
    offered = session.exec(select(AuditEvent.id).where(
        AuditEvent.entity_type == "scheduler_offer",
        AuditEvent.metadata_json["worker"]["task_id"].as_string() == task.id,
    )).first()
    if (any(row.action not in {"created", "task_input_normalized", "mpt_task_owned"} for row in history)
            or offered
            or session.exec(select(AgentSubmission.id).where(AgentSubmission.task_id == task.id)).first()):
        raise TaskConflictError("RERENDER_QC_PREVIOUSLY_OFFERED_OR_RUN")
    content_tasks = select(AgentTask.id).where(AgentTask.content_id == content_id)
    if (session.exec(select(AgentTask.id).where(
            AgentTask.content_id == content_id, AgentTask.id != task.id,
            AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
        )).first() or session.exec(select(SchedulerOffer.request_id).where(
            SchedulerOffer.task_id.in_(content_tasks),
            (SchedulerOffer.task_id == task.id) | (SchedulerOffer.expires_at > utc_now()),
        )).first()
            or session.exec(select(StageRun.id).where(StageRun.content_id == content_id, StageRun.status == StageStatus.RUNNING)).first()
            or session.exec(select(PublicationRecord.id).where(PublicationRecord.content_id == content_id)).first()
            or session.exec(select(AuditEvent.id).where(AuditEvent.entity_type == "publish_intent",
                AuditEvent.metadata_json["content_id"].as_string() == content_id)).first()):
        raise TaskConflictError("RERENDER_CONTENT_IN_FLIGHT_OR_PUBLICATION_EXISTS")
    current = effective_settings(session, runtime.settings)
    flow = runtime.workflow_service(session)
    script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
    plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
    research = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
    for upstream in (script, plan):
        if upstream is None:
            raise TaskConflictError("RERENDER_UPSTREAM_MISSING")
        _require_current_revision(session, upstream)
        _unchanged_material_task(session, upstream, owned=True, consumed=True)
    pipeline._verify_rights(session, content)
    pipeline._require_current_qc(session, content_id, TaskType.SCRIPT_QC, {"script": script, "research": research})
    pipeline._require_current_qc(session, content_id, TaskType.VISUAL_PLAN_QC, {"visual_plan": plan, "script": script})
    pipeline._require_script_assist(session, current, content_id, release=True, allow_manual_approval=True)
    verify_voice_generation(session, current, content, plan.input_data.get("voice_artifact", {}),
                            plan.input_data.get("voice_generation"), script)
    data = task.input_data
    refs = {"script": script.id, "visual_plan": plan.id}
    if (data.get("upstream_task_ids") != refs or data.get("video_artifact_id") != artifact.id
            or data.get("acceptance_contract_version") != 2 or data.get("source_context_version") != 1
            or data.get("audio_evidence_version") != 1 or data.get("visual_evidence_version") != 1
            or not isinstance(data.get("audio_evidence"), dict) or not isinstance(data.get("visual_evidence"), dict)
            or acceptance_subject(session, content_id, {**refs, "video_artifact_id": artifact.id},
                source_context_version=1, audio_evidence=data["audio_evidence"], visual_evidence=data["visual_evidence"])
            != data.get("acceptance_subject")):
        raise TaskConflictError("RERENDER_QC_SUBJECT_CHANGED")
    receipt = session.get(Artifact, data.get("acceptance_receipt_artifact_id"))
    if (receipt is None or receipt.content_id != content_id or receipt.artifact_type != "acceptance_input"
            or json.loads(verified_artifact_bytes(content, receipt)) != data):
        raise TaskConflictError("RERENDER_ACCEPTANCE_RECEIPT_CHANGED")
    raw = (Path(content.content_dir).resolve() / "render" / "mpt-render-request.json").read_bytes()
    request = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != expected["render_request_sha256"]
            or request.get("revision_id") != revision
            or type(request.get("renderer_version")) is not str
            or request["renderer_version"] != expected["previous_renderer_version"]
            or type(media_executor.RENDERER_VERSION) is not str
            or media_executor.RENDERER_VERSION != expected["renderer_version"]
            or expected["previous_renderer_version"] == expected["renderer_version"]
            or request.get("voice_sha256") != plan.input_data["voice_artifact"].get("sha256")):
        raise TaskConflictError("RERENDER_RENDERER_NOT_CHANGED_OR_RECEIPT_CHANGED")
    before = task.model_dump(mode="json")
    runtime.task_service(session).cancel(task.id, actor_type="admin", actor_id="mpt-admin-api")
    content.business_status, content.current_stage = ContentStatus.PRODUCING, "render_ready"
    content.updated_at = utc_now()
    session.add(content)
    response = {"requested": True, "replayed": False, "content_id": content_id,
                "cancelled_qc_task_id": task.id, "previous_final_artifact_id": artifact.id,
                "previous_revision_id": revision, "renderer_version": expected["renderer_version"]}
    session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="content", entity_id=content_id,
        action=REQUESTED, metadata_json={"version": 1, "key": key, "expected": deepcopy(expected),
            "previous_qc": before, "cancelled_task_sha256": stable_hash(task.model_dump(mode="json")),
            "script": script_binding(script), "visual_plan": script_binding(plan),
            "acceptance_receipt_artifact_id": receipt.id, "outputs_sha256": outputs_hash,
            "previous_render_request": request, "response": response}))
    return response
