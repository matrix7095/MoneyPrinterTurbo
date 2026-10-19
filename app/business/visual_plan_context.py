"""Exact image context for a manual revision of an accepted packaged film."""

from copy import deepcopy
import hashlib
from pathlib import Path

from glance_core.content_acceptance import generated_image_material
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, TaskStatus, TaskType, utc_now
from glance_core.task_schemas import RecoveryFailureEnvelope, RecoveryPlan
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.voice_generation import artifact_binding, script_binding, verified_artifact_bytes, verify_voice_generation
from glance_media.executor import NonRetryableMediaError
from sqlmodel import select

RECONCILED = "mpt_packaged_visual_context_reconciled"
ORIGIN_FAILURE = '任务包交接 HTTP 409: {"detail":"generated image origin unavailable"}'


def packaged_visual_context(session, content, script, previous_plan, source_images):
    """Derive the whitelist from the frozen resolved plan, never artifact history."""
    if ((script.input_data or {}).get("packaged_script_revision") is not True
            or stable_hash(script.input_data) != script.input_hash
            or previous_plan != script.input_data.get("previous_plan")
            or not isinstance(previous_plan, dict)
            or not isinstance(previous_plan.get("segments"), list)
            or not previous_plan["segments"]):
        raise NonRetryableMediaError("Packaged revision resolved image plan changed")
    ids, paths = [], []
    for segment in previous_plan["segments"]:
        if not isinstance(segment, dict) or segment.get("asset_request") or not isinstance(segment.get("asset_id"), str):
            raise NonRetryableMediaError("Packaged revision requires resolved previous images")
        artifact_id = segment["asset_id"]
        if artifact_id in ids:
            continue
        artifact = session.get(Artifact, artifact_id)
        if (artifact is None or artifact.content_id != content.id
                or artifact.artifact_type not in {"source_image", "agent_generated_image"}):
            raise NonRetryableMediaError("Packaged revision previous image is unavailable")
        if artifact.artifact_type == "agent_generated_image":
            generated_image_material(session, content, artifact.id)
        else:
            verified_artifact_bytes(content, artifact)
        ids.append(artifact.id)
        paths.append(artifact.relative_path)
    if len({Path(path).name.casefold() for path in paths}) != len(paths):
        raise NonRetryableMediaError("Packaged revision image context filenames collide")
    sources = [deepcopy(row) for row in source_images if row.get("artifact_id") in ids]
    return {"allowed_asset_ids": ids, "context_files": paths, "source_images": sources,
            "allowed_reference_artifact_ids": [str(row["artifact_id"]) for row in sources]}


def _task(session, content_id, binding, kind):
    task = session.get(AgentTask, binding["task_id"])
    if (task is None or task.content_id != content_id or task.task_type != kind
            or task.attempt != binding["attempt"] or task.input_hash != binding["input_hash"]
            or stable_hash(task.input_data) != task.input_hash
            or stable_hash(task.result_data) != binding["result_hash"]):
        raise TaskConflictError("PACKAGED_VISUAL_EXPECTED_TASK_CHANGED")
    return task


def reconcile_packaged_visual_context(runtime, session, content_id, expected):
    """Replace only the proved historical-image whitelist defect; never regenerate speech."""
    from app.business import pipeline
    from app.business.conductor_launch import _load
    from app.business.content_host import host_live
    from app.business.content_run import load_run
    from app.business.quarantine import assert_allowed
    from app.business.runtime import FAILURE_RECOVERY, OWNED, _failure_hash, _require_current_revision
    from app.business.voice_rebuild import _idle
    from glance_core.runtime_config import effective_settings

    assert_allowed(session, content_id=content_id)
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    bad = _task(session, content_id, expected["failed_plan"], TaskType.VISUAL_PLAN)
    recovery = _task(session, content_id, expected["recovery"], TaskType.RECOVERY)
    key = stable_hash({"version": 1, "content_id": content_id, "failed_plan_id": bad.id})
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id, AuditEvent.action == RECONCILED,
    )).all()
    prior = [row for row in receipts if row.metadata_json.get("key") == key]
    if prior:
        if (len(prior) != 1 or prior[0].actor_type != "admin" or prior[0].actor_id != "mpt-admin-api"
                or prior[0].metadata_json.get("expected") != expected):
            raise TaskConflictError("PACKAGED_VISUAL_REPLAY_BINDING_CHANGED")
        proof = prior[0].metadata_json
        replacement = session.get(AgentTask, proof.get("replacement_task_id"))
        if (replacement is None or replacement.content_id != content_id or replacement.task_type != TaskType.VISUAL_PLAN
                or replacement.input_hash != proof.get("replacement_input_hash")
                or stable_hash(replacement.input_data) != replacement.input_hash
                or replacement.input_data.get("supersedes_task_id") != bad.id
                or replacement.input_data.get("recovery_task_id") != recovery.id
                or replacement.input_data.get("packaged_visual_context_version") != 1):
            raise TaskConflictError("PACKAGED_VISUAL_REPLAY_CHANGED")
        from app.business.runtime import _require_owned_input

        _require_owned_input(session, replacement)
        return {**proof["response"], "replayed": True}

    run = load_run(session)
    execution = _load(session, content_id)
    if (run is None or run.content_id != content_id or run.state != "stopped" or not run.stop_requested
            or {field: getattr(run, field) for field in expected["run"]} != expected["run"]
            or host_live(run) or any(intent.get("state") != "TERMINAL" for intent in run.intents.values())
            or execution is None or execution.state != "FINISHED" or execution.exit_code != 0
            or execution.error_code or not execution.exited_at
            or execution.run_id != run.run_id or execution.generation != run.generation
            or stable_hash(execution.model_dump()) != expected["execution_sha256"]):
        raise TaskConflictError("PACKAGED_VISUAL_RUN_NOT_DRAINED_OR_CHANGED")
    _idle(session, content_id)
    if (content.production_mode != ProductionMode.IMAGE_ONLY
            or (content.business_status, content.current_stage, content.error_code)
            != (ContentStatus.FAILED, "recovery_blocked", "RECOVERY_BLOCKED")):
        raise TaskConflictError("PACKAGED_VISUAL_CONTENT_NOT_CURRENT")
    if (bad.status != TaskStatus.FAILED or bad.result_data is not None or bad.error_code != "BUNDLE_HANDOFF_ERROR"
            or bad.error_message != ORIGIN_FAILURE or recovery.status != TaskStatus.COMPLETED):
        raise TaskConflictError("PACKAGED_VISUAL_FAILURE_NOT_RECOGNIZED")
    runtime._failure_source(session, bad.id)
    _require_current_revision(session, recovery)
    runtime._require_accepted_native_task(session, recovery, consumed=True)
    submission = session.exec(select(AgentSubmission).where(
        AgentSubmission.task_id == recovery.id, AgentSubmission.attempt == recovery.attempt,
        AgentSubmission.validation_status == "ACCEPTED",
    )).one()
    if hashlib.sha256(Path(submission.raw_result_path).read_bytes()).hexdigest() != expected["recovery"]["raw_file_sha256"]:
        raise TaskConflictError("PACKAGED_VISUAL_RECOVERY_RAW_CHANGED")
    failure = RecoveryFailureEnvelope.model_validate(recovery.input_data.get("failure"))
    plan = RecoveryPlan.model_validate(recovery.result_data)
    if (failure.failure_class != "CONTEXT_MISSING" or failure.code != bad.error_code
            or failure.message != bad.error_message or failure.source_task_id != bad.id
            or failure.source_task_type != "visual_plan" or failure.source_attempt != bad.attempt
            or failure.retryable or failure.validation_errors
            or plan.failure_class != "CONTEXT_MISSING" or plan.action != "repair_task_context"
            or plan.target_task_type != "visual_plan"):
        raise TaskConflictError("PACKAGED_VISUAL_RECOVERY_INTENT_CHANGED")
    current = effective_settings(session, runtime.settings)
    flow = runtime.workflow_service(session)
    lineage, policy = recovery.input_data.get("lineage") or {}, recovery.input_data.get("policy") or {}
    signature = flow._root_cause_signature("CONTEXT_MISSING", bad.error_code)
    limits = [lineage.get("max_rounds"), policy.get("max_rounds"), current.recovery_max_rounds]
    if (not current.recovery_enabled or policy.get("enabled") is not True
            or any(type(limit) is not int or limit < 1 for limit in limits)
            or type(lineage.get("round")) is not int or not 1 <= lineage["round"] <= min(limits)
            or lineage.get("root_task_id") != bad.id or lineage.get("parent_task_id") != bad.id
            or lineage.get("repair_target_task_id") != bad.id or lineage.get("content_id") != content_id
            or lineage.get("source_attempt") != bad.attempt or lineage.get("same_task_retries") != 0
            or lineage.get("target_task_type") != "visual_plan" or lineage.get("root_cause_signature") != signature
            or lineage.get("repair_fingerprint") != flow._repair_fingerprint(bad, failure_class="CONTEXT_MISSING", code=bad.error_code)
            or any("repair_task_context" not in actions for actions in (
                policy.get("allowed_actions", []), recovery.input_data.get("allowed_actions", [])))):
        raise TaskConflictError("PACKAGED_VISUAL_RECOVERY_AUTHORITY_CHANGED")
    origins = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "agent_task",
        AuditEvent.entity_id == recovery.id, AuditEvent.action == "recovery_task_created")).all()
    original = {"source_task_id": bad.id, "failure_class": "CONTEXT_MISSING", "code": bad.error_code,
                "root_cause_signature": signature, "round": lineage["round"], "target_task_id": bad.id}
    links = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "agent_task",
        AuditEvent.entity_id == recovery.id, AuditEvent.action == FAILURE_RECOVERY)).all()
    if (len(origins) != 1 or origins[0].actor_type != "system" or origins[0].metadata_json != original
            or len(links) != 1 or links[0].actor_type != "system" or links[0].actor_id != "mpt-agent-failure"
            or links[0].metadata_json != {"source_task_id": bad.id, "source_input_hash": bad.input_hash,
                "source_attempt": bad.attempt, "source_failure_hash": _failure_hash(bad),
                "recovery_input_hash": recovery.input_hash, "target_hashes": {}}):
        raise TaskConflictError("PACKAGED_VISUAL_RECOVERY_ORIGIN_CHANGED")
    script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
    research = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
    if (script is None or (script.input_data or {}).get("packaged_script_revision") is not True
            or bad.input_data.get("upstream_task_ids") != {"script": script.id}):
        raise TaskConflictError("PACKAGED_VISUAL_SCRIPT_CHANGED")
    _require_current_revision(session, script)
    runtime._require_accepted_native_task(session, script, consumed=True)
    pipeline._verify_rights(session, content)
    pipeline._require_current_qc(session, content_id, TaskType.SCRIPT_QC, {"script": script, "research": research})
    pipeline._require_script_assist(session, current, content_id, release=True, allow_manual_approval=True)
    if not pipeline._has_manual_script_approval(session, current, content_id):
        raise TaskConflictError("PACKAGED_VISUAL_MANUAL_APPROVAL_MISSING")
    generation = bad.input_data.get("voice_generation")
    summary, receipt = verify_voice_generation(session, current, content, bad.input_data.get("voice_artifact", {}), generation, script)
    if summary["audio"]["encoded_duration_sec"] != bad.input_data.get("voice_duration_sec"):
        raise TaskConflictError("PACKAGED_VISUAL_VOICE_DURATION_CHANGED")
    corrected = deepcopy(bad.input_data)
    context = packaged_visual_context(session, content, script, bad.input_data.get("previous_plan"), pipeline.source_image_index(content))
    # This repair is only for a strict superset of the legitimate frozen image set.
    if (not set(context["allowed_asset_ids"]) < set(bad.input_data.get("allowed_asset_ids", []))
            or any(session.get(Artifact, item) is None for item in bad.input_data["allowed_asset_ids"])):
        raise TaskConflictError("PACKAGED_VISUAL_WHITELIST_DEFECT_NOT_PROVEN")
    corrected.update(context)
    corrected["context_files"].append(receipt.relative_path)
    corrected.update(supersedes_task_id=bad.id, recovery_task_id=recovery.id, packaged_visual_context_version=1)
    replacement = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, corrected, priority=50, allow_multiple=True)
    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task", entity_id=replacement.id,
                           action=OWNED, metadata_json={"input_hash": replacement.input_hash}))
    content.error_code = content.error_message = None
    session.add(content)
    flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "visual_plan")
    response = {"reconciled": True, "replayed": False, "visual_plan_task_id": replacement.id,
                "failed_visual_plan_task_id": bad.id}
    session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="content", entity_id=content_id,
        action=RECONCILED, metadata_json={"key": key, "version": 1, "expected": deepcopy(expected),
            "replacement_task_id": replacement.id, "replacement_input_hash": replacement.input_hash,
            "script": script_binding(script), "voice_artifact": deepcopy(bad.input_data["voice_artifact"]),
            "receipt_artifact": artifact_binding(receipt), "allowed_asset_ids": context["allowed_asset_ids"],
            "response": response, "created_at": utc_now().isoformat()}))
    return response
