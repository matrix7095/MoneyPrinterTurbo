"""Explicit local revoice of a packaged image film without rewriting its history."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from glance_core.content_acceptance import _unchanged_material_task, resolved_visual_plan, verify_acceptance
from glance_core.models import (
    AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, PublicationRecord,
    SchedulerOffer, StageRun, StageStatus, TaskStatus, TaskType, utc_now,
)
from glance_core.runtime_config import effective_settings
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.voice_generation import (
    GENERATION_ACTION, artifact_binding, bind_voice_generation, script_binding,
    verified_artifact_bytes, verify_voice_generation,
)
from glance_core.workflow import log_stage
from glance_media.executor import NonRetryableMediaError
from glance_media.tts.kokoro import kokoro_parameters, verify_kokoro_bundle
from sqlmodel import Session, select

from app.business.media_executor import MptMediaExecutor
from glance_core.content_revision import current_revision_clause


def _idle(session, content_id, *, own_stage_id=None):
    from app.business.content_run import load_run

    run = load_run(session)
    if run is not None and run.content_id == content_id and run.state not in {"stopped", "expired"}:
        raise TaskConflictError("VOICE_REBUILD_CONTENT_IN_FLIGHT")
    if session.exec(select(AgentTask.id).where(
        current_revision_clause(AgentTask), AgentTask.content_id == content_id,
        AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
    )).first() or session.exec(select(SchedulerOffer.request_id).where(
        SchedulerOffer.task_id.in_(select(AgentTask.id).where(current_revision_clause(AgentTask), AgentTask.content_id == content_id)),
        SchedulerOffer.expires_at > utc_now(),
    )).first():
        raise TaskConflictError("VOICE_REBUILD_CONTENT_IN_FLIGHT")
    active = session.exec(select(StageRun).where(
        current_revision_clause(StageRun), StageRun.content_id == content_id, StageRun.status == StageStatus.RUNNING,
    )).all()
    if any(stage.id != own_stage_id for stage in active):
        raise TaskConflictError("VOICE_REBUILD_STAGE_IN_FLIGHT")
    if session.exec(select(PublicationRecord.id).where(current_revision_clause(PublicationRecord),
                                                     PublicationRecord.content_id == content_id)).first():
        raise TaskConflictError("VOICE_REBUILD_PUBLICATION_EXISTS")
    if session.exec(select(AuditEvent.id).where(
        AuditEvent.entity_type == "publish_intent", AuditEvent.metadata_json["content_id"].as_string() == content_id,
    )).first():
        raise TaskConflictError("VOICE_REBUILD_PUBLISH_INTENT_EXISTS")


def register_generation(session, settings, content, executor, tts_result, voice, script, stage, *, operation, previous_plan=None):
    if tts_result.provider != "kokoro" or not tts_result.provenance:
        raise NonRetryableMediaError("Kokoro generation evidence is required")
    path = tts_result.audio_path.with_name("tts_generation_receipt.json")
    if json.loads(path.read_bytes()) != tts_result.provenance:
        raise NonRetryableMediaError("Kokoro generation receipt changed before registration")
    receipt = Artifact(content_id=content.id, revision_id=content.current_revision_id,
                       **executor.artifact_metadata(path, "tts_generation_receipt", "voice"))
    event, summary = bind_voice_generation(
        session, settings, content, voice, receipt, script, stage,
        operation=operation, previous_plan=previous_plan,
    )
    session.add(receipt)
    return event, summary


def _subject(session, settings, content_id):
    from app.business import pipeline

    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    if content.business_status != ContentStatus.PACKAGED or content.production_mode != ProductionMode.IMAGE_ONLY:
        raise NonRetryableMediaError("Voice rebuild requires a packaged image-only film")
    _idle(session, content_id)
    pipeline._verify_rights(session, content)
    flow = pipeline.workflow(session, settings)
    script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
    research = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
    pipeline._require_current_qc(session, content_id, TaskType.SCRIPT_QC, {"script": script, "research": research})
    pipeline._require_script_assist(session, settings, content_id, release=True, allow_manual_approval=True)
    previous = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
    if script is None or previous is None:
        raise NonRetryableMediaError("Voice rebuild requires the current script and visual plan")
    _unchanged_material_task(session, previous, consumed=False)
    qc = pipeline._require_current_qc(session, content_id, TaskType.CONTENT_QC, {"script": script, "visual_plan": previous})
    verify_acceptance(session, qc, qc.result_data or {})
    snapshot = pipeline._script_approval_snapshot(session, content_id)
    pipeline._manual_approval_key(settings)
    plan = resolved_visual_plan(session, previous)
    asset_ids = list(dict.fromkeys(str(segment.get("asset_id") or "") for segment in plan.get("segments", [])))
    if not asset_ids or any(not item for item in asset_ids):
        raise NonRetryableMediaError("Voice rebuild requires every previous segment to have a resolved image")
    for asset_id in asset_ids:
        artifact = session.get(Artifact, asset_id)
        if artifact is None or artifact.artifact_type not in {"source_image", "agent_generated_image"}:
            raise NonRetryableMediaError("Voice rebuild previous image identity is unavailable")
        verified_artifact_bytes(content, artifact)
    return content, flow, script, previous, snapshot, plan, asset_ids, script_binding(qc)


def revise_packaged_script(session, settings, content_id, note):
    """Use the normal rewrite lifecycle after the locked packaged-film preflight."""
    current = effective_settings(session, settings)
    _, flow, _, _, _, previous_plan, _, _ = _subject(session, current, content_id)
    return flow.reject_script(
        content_id,
        note + "\n除审核意见明确要求的措辞外，保留原稿其余已核验事实、数值精度和 claim_id。"
        "previous_plan 仅供复用原有图片；新稿仍须独立脚本质检、审核及重新配音和成片验收。",
        packaged_previous_plan=deepcopy(previous_plan),
    )


def approved_script_voice(session, settings, content, executor, script, requested, options, model_sha, revalidate, before_generate):
    """Commit a complete Kokoro generation once, then resume approval from its proof."""
    from glance_core.voice_generation import _summary

    bindings = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "content", AuditEvent.entity_id == content.id,
        AuditEvent.action == GENERATION_ACTION,
    )).all()
    bindings = [event for event in bindings if event.metadata_json.get("voice_input_hash") == requested]
    if len(bindings) > 1:
        raise NonRetryableMediaError("Script voice has ambiguous prior generation evidence")
    if bindings:
        event = bindings[0]
        voice = session.get(Artifact, event.metadata_json.get("voice_artifact", {}).get("id", ""))
        receipt = session.get(Artifact, event.metadata_json.get("receipt_artifact", {}).get("id", ""))
        if voice is None or receipt is None:
            raise NonRetryableMediaError("Script voice prior generation artifacts are unavailable")
        data = json.loads(verified_artifact_bytes(content, receipt, limit=8 * 1024 * 1024))
        generation = _summary(event, data, receipt)
        verify_voice_generation(session, settings, content, artifact_binding(voice), generation, script)
        revalidate()
        duration = data["audio"]["encoded_duration_sec"]
        timing = executor.visual_timing_evidence(Path(content.content_dir) / voice.relative_path, duration)
        if timing is None or not settings.min_video_seconds <= duration <= settings.max_video_seconds:
            raise NonRetryableMediaError("Script voice prior timing/duration evidence is unavailable")
        return voice, generation, duration, timing
    if session.exec(select(StageRun.id).where(
        current_revision_clause(StageRun), StageRun.content_id == content.id, StageRun.stage == "voice", StageRun.input_hash == requested,
        StageRun.status.in_([StageStatus.RUNNING, StageStatus.SUCCESS]),
    )).first():
        raise NonRetryableMediaError("Script voice prior attempt lacks generation evidence; reconcile before retry")

    def generate():
        stage = session.exec(select(StageRun).where(
            current_revision_clause(StageRun), StageRun.content_id == content.id, StageRun.stage == "voice", StageRun.input_hash == requested,
            StageRun.status == StageStatus.RUNNING,
        )).one()
        result = executor.synthesize_voice(script.result_data["full_narration"], **options)
        duration = result.duration_sec
        if not settings.min_video_seconds <= duration <= settings.max_video_seconds:
            raise NonRetryableMediaError("Generated voice duration is outside configured production bounds")
        if result.provenance.get("bundle", {}).get("manifest_sha256") != model_sha:
            raise NonRetryableMediaError("Generated Kokoro model identity changed")
        timing = executor.visual_timing_evidence(result.audio_path, duration)
        if timing is None:
            raise NonRetryableMediaError("Generated voice timing sidecar is missing")
        revalidate()
        voice = Artifact(content_id=content.id, revision_id=content.current_revision_id,
                         **executor.artifact_metadata(result.audio_path, "voice", "voice"))
        _, generation = register_generation(session, settings, content, executor, result, voice, script, stage,
                                            operation="script_approval")
        session.add(voice)
        return voice, generation, duration, timing

    before_generate()
    return log_stage(session, content.id, "voice", generate, input_hash=requested)


def rebuild_voice(engine, settings, content_id):
    """Called under the runtime's content OS lock; repeat promotion reuses committed speech."""
    from app.business import pipeline

    with Session(engine) as session:
        current = effective_settings(session, settings)
        content, flow, script, previous, snapshot, old_plan, asset_ids, old_qc = _subject(session, current, content_id)
        if current.tts_provider != "kokoro" or current.tts_provider_endpoint:
            raise NonRetryableMediaError("Voice rebuild requires configured offline Kokoro with no endpoint")
        kokoro_parameters(current.tts_voice, current.tts_rate, current.tts_volume)
        bundle = verify_kokoro_bundle(current.kokoro_model_dir)
        old_proof = script_binding(previous)
        narration = script.result_data["full_narration"]
        options = {"voice": current.tts_voice, "rate": current.tts_rate, "volume": current.tts_volume,
                   "provider": "kokoro", "provider_endpoint": "", "model_dir": current.kokoro_model_dir,
                   "target_duration_sec": float(script.result_data.get("estimated_duration_sec") or 0) or None}
        requested = stable_hash({"version": 2, "operation": "manual_kokoro_rebuild", "script": script_binding(script),
                                 "approval": snapshot, "previous_plan": old_proof,
                                 "previous_content_qc": old_qc,
                                 "model_manifest_sha256": bundle["manifest_sha256"],
                                 "voice_options": {key: value for key, value in options.items() if key != "model_dir"}})
        executor = MptMediaExecutor(Path(content.content_dir).resolve())

        def revalidate(*, own_stage_id=None):
            session.expire_all()
            pipeline._revalidate_script_approval(session, content_id, snapshot)
            latest = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            if (content.business_status != ContentStatus.PACKAGED or latest is None
                    or script_binding(latest) != old_proof
                    or resolved_visual_plan(session, latest) != old_plan):
                raise NonRetryableMediaError("Voice rebuild subject changed during generation")
            _idle(session, content_id, own_stage_id=own_stage_id)
            pipeline._verify_rights(session, content)
            qc = pipeline._require_current_qc(session, content_id, TaskType.CONTENT_QC, {"script": script, "visual_plan": latest})
            if script_binding(qc) != old_qc:
                raise NonRetryableMediaError("Voice rebuild prior acceptance changed during generation")
            verify_acceptance(session, qc, qc.result_data or {})

        bindings = session.exec(select(AuditEvent).where(
            AuditEvent.entity_type == "content", AuditEvent.entity_id == content_id,
            AuditEvent.action == GENERATION_ACTION,
        )).all()
        bindings = [event for event in bindings if event.metadata_json.get("voice_input_hash") == requested]
        if len(bindings) > 1:
            raise NonRetryableMediaError("Voice rebuild has ambiguous prior generation evidence")
        if bindings:
            event = bindings[0]
            voice = session.get(Artifact, event.metadata_json["voice_artifact"]["id"])
            receipt = session.get(Artifact, event.metadata_json["receipt_artifact"]["id"])
            if voice is None or receipt is None:
                raise NonRetryableMediaError("Voice rebuild prior generation artifacts are unavailable")
            data = json.loads(verified_artifact_bytes(content, receipt, limit=8 * 1024 * 1024))
            from glance_core.voice_generation import _summary

            generation = _summary(event, data, receipt)
            verify_voice_generation(session, current, content, artifact_binding(voice), generation, script)
            duration = data["audio"]["encoded_duration_sec"]
            timing = executor.visual_timing_evidence(Path(content.content_dir) / voice.relative_path, duration)
        else:
            if session.exec(select(StageRun.id).where(
                current_revision_clause(StageRun), StageRun.content_id == content_id, StageRun.stage == "voice", StageRun.input_hash == requested,
                StageRun.status == StageStatus.SUCCESS,
            )).first():
                raise NonRetryableMediaError("Voice rebuild prior success lacks generation evidence; reconcile before retry")

            def generate():
                stage = session.exec(select(StageRun).where(
                    current_revision_clause(StageRun), StageRun.content_id == content_id, StageRun.stage == "voice", StageRun.input_hash == requested,
                    StageRun.status == StageStatus.RUNNING,
                )).one()
                result = executor.synthesize_voice(narration, **options)
                duration = result.duration_sec
                if not current.min_video_seconds <= duration <= current.max_video_seconds:
                    raise NonRetryableMediaError("Rebuilt voice duration is outside configured production bounds")
                if result.provenance.get("bundle", {}).get("manifest_sha256") != bundle["manifest_sha256"]:
                    raise NonRetryableMediaError("Kokoro model identity changed during voice generation")
                timing = executor.visual_timing_evidence(result.audio_path, duration)
                if timing is None:
                    raise NonRetryableMediaError("Rebuilt voice timing sidecar is missing")
                revalidate(own_stage_id=stage.id)
                voice = Artifact(content_id=content_id, revision_id=content.current_revision_id, **executor.artifact_metadata(result.audio_path, "voice", "voice"))
                _, generation = register_generation(
                    session, current, content, executor, result, voice, script, stage,
                    operation="manual_kokoro_rebuild", previous_plan=old_proof,
                )
                session.add(voice)
                # log_stage commits speech artifacts, signed binding and SUCCESS together.
                return voice, generation, duration, timing

            voice, generation, duration, timing = log_stage(session, content_id, "voice", generate, input_hash=requested)

        revalidate()
        if not current.min_video_seconds <= duration <= current.max_video_seconds:
            raise NonRetryableMediaError("Rebuilt voice duration is outside configured production bounds")
        if timing is None:
            raise NonRetryableMediaError("Rebuilt voice timing evidence is unavailable")
        verify_voice_generation(session, current, content, artifact_binding(voice), generation, script)
        receipt_path = generation["receipt_artifact"]["relative_path"]
        source_images = [row for row in pipeline.source_image_index(content) if row.get("artifact_id") in asset_ids]
        source_ids = [row["artifact_id"] for row in source_images]
        plan_input = {
            "upstream_task_ids": {"script": script.id}, "voice_duration_sec": duration,
            "visual_timing_evidence": timing, "voice_artifact": artifact_binding(voice), "voice_generation": generation,
            "previous_plan": deepcopy(old_plan), "previous_visual_plan_task_id": previous.id,
            "voice_rebuild_input_sha256": requested, "scenes": [], "allowed_layouts": ["image_card"],
            "production_mode": ProductionMode.IMAGE_ONLY.value, "asset_image_enabled": current.asset_image_enabled,
            "image_only_gen_budget": current.image_only_gen_budget if current.asset_image_enabled else 0,
            "source_images": source_images, "allowed_asset_ids": asset_ids, "allowed_reference_artifact_ids": source_ids,
            "context_files": [receipt_path, *[session.get(Artifact, asset_id).relative_path for asset_id in asset_ids]],
            "revision_note": "保留已审批口播原文；按新配音时长重新编排 previous_plan 的全部现有图片，进入正常计划质检与完整素材质检。",
        }
        pipeline._record_manual_script_approval(session, current, content_id, snapshot, artifact_binding(voice))
        content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "visual_plan"
        content.error_code = content.error_message = None
        content.updated_at = utc_now()
        session.add(content)
        # No invalidation: completed plans, negative reviews, dedup keys and counters stay immutable.
        flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, plan_input, priority=50, allow_multiple=True)
        session.refresh(content)
        return content
