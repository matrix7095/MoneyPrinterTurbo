"""Audio rewrites retain the approved, resolved image composition as context only."""

from copy import deepcopy
import json
from pathlib import Path

import pytest
from glance_core.content_acceptance import resolved_visual_plan
from glance_core.models import AgentTask, Artifact, AuditEvent, Content, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from glance_media import executor as portable
from glance_media.executor import NonRetryableMediaError, sha256_file
from glance_media.tts import TtsResult
from sqlmodel import Session, select

from app.business import pipeline
from app.business.bundles import context_files_for
from app.business.runtime import AGENT_CONSUMED
from test.business.test_visual_plan_materialization import (
    failed_image_contract as failed_image_contract,
    image_contract as image_contract,
    record_accepted_completion,
)


@pytest.fixture
def audio_rewrite_images(failed_image_contract, monkeypatch):
    runtime, content_id, origin_id, _, _, _, old_ids = failed_image_contract
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        content, origin = session.get(Content, content_id), session.get(AgentTask, origin_id)

        def completed(kind, inputs, result):
            task = flow.create_agent_task(content_id, kind, inputs, allow_multiple=True)
            record_accepted_completion(session, runtime, task, result)
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=task.id, action=AGENT_CONSUMED,
                                   metadata_json={"result_hash": stable_hash(result)}))
            session.commit()
            return task

        old_script = session.get(AgentTask, origin.input_data["upstream_task_ids"]["script"])
        research_id = old_script.input_data["upstream_task_ids"]["research"]
        flow.invalidate_visual_plan(content_id, reason="recovery-visual-repair")
        segments = deepcopy(origin.result_data["segments"])
        for segment in segments:
            segment.pop("asset_request", None)
        request = {"prompt": "repaired index two, desk at the rear wall"}
        segments[2].pop("asset_id")
        segments[2]["asset_request"] = request
        plan = completed(TaskType.VISUAL_PLAN, {
            **origin.input_data, "allowed_asset_ids": [key for key in old_ids if key != old_ids[2]],
        }, {"segments": segments})
        plan_qc = completed(TaskType.VISUAL_PLAN_QC, {
            "upstream_task_ids": {"visual_plan": plan.id, "script": old_script.id},
        }, {"passed": True})
        generation = flow.create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {
            "upstream_task_ids": {"visual_plan": plan.id},
            "requests": [{"segment_index": 2, "asset_request": request}],
        }, allow_multiple=True)
        image = Path(content.content_dir) / "repaired-two.png"
        image.write_bytes(b"repaired-image-two")
        artifact = Artifact(content_id=content_id, artifact_type="agent_generated_image",
                            relative_path=image.name, sha256=sha256_file(image), size_bytes=image.stat().st_size,
                            mime_type="image/png", producer_stage=generation.id)
        session.add(artifact)
        record_accepted_completion(session, runtime, generation, {
            "images": [{"segment_index": 2, "artifact_id": artifact.id}],
        })
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=generation.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(generation.result_data)}))
        session.commit()
        full_qc = completed(TaskType.ASSET_QC, pipeline._asset_qc_input(session, flow, generation, plan),
                            {"passed": True})
        resolved = resolved_visual_plan(session, plan)
        expected_ids = [segment["asset_id"] for segment in resolved["segments"]]
        assert expected_ids == old_ids[:2] + [artifact.id] + old_ids[3:]
        assert session.get(AgentTask, origin_id).status == TaskStatus.CANCELLED
        assert session.get(AgentTask, session.get(Artifact, old_ids[0]).producer_stage).status == TaskStatus.CANCELLED
        manifest = {"visual_plan": resolved}
        manifest_path = Path(content.content_dir) / "joint-manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence = {"manifest": manifest, "manifest_path": manifest_path.name,
                    "manifest_sha256": sha256_file(manifest_path)}
        subject = {"visual_evidence_sha256": stable_hash(evidence)}
        for key, task in (("script", old_script), ("visual_plan", plan), ("plan_qc", plan_qc)):
            subject.update({f"{key}_task_id": task.id, f"{key}_sha256": stable_hash(task.result_data)})
        failed_qc = completed(TaskType.CONTENT_QC, {
            "upstream_task_ids": {"script": old_script.id, "visual_plan": plan.id},
            "acceptance_subject": subject,
            "visual_evidence": evidence,
        }, {"passed": False, "issues": [{"field": "audio", "message": "ambiguous pronunciation"}],
            "subject": subject})
        failed_qc.schema_version = "3"
        session.add(failed_qc)
        new_script = completed(TaskType.SHORT_SCRIPT, {
            "upstream_task_ids": {"research": research_id}, "previous_qc_task_id": failed_qc.id,
        }, {"full_narration": "新脚本，保留这七张已经通过图片质检的图。"})
        completed(TaskType.SCRIPT_QC, {
            "upstream_task_ids": {"script": new_script.id, "research": research_id},
        }, {"passed": True})
        with pytest.raises(ValueError, match="script lineage missing/stale"):
            resolved_visual_plan(session, plan)
        frozen = {task.id: (deepcopy(task.input_data), task.input_hash, deepcopy(task.result_data))
                  for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()}
        ids = {"plan": plan.id, "full_qc": full_qc.id, "failed_qc": failed_qc.id,
               "plan_qc": plan_qc.id, "generation": generation.id, "image": artifact.id,
               "new_script": new_script.id, "bad_image": old_ids[2]}

    class Provider:
        def synthesize(self, narration, output, **kwargs):
            output.write_bytes(b"new approved voice")
            return TtsResult(output, 30.0, [], "stub")

    monkeypatch.setattr(portable, "get_tts_provider", lambda *args: Provider())
    return runtime, content_id, ids, expected_ids, frozen


def test_audio_rewrite_retains_all_seven_resolved_bindings_without_changing_old_materials(audio_rewrite_images):
    runtime, content_id, ids, expected_ids, frozen = audio_rewrite_images
    pipeline.approve_script(runtime.engine, runtime.settings, content_id)
    with Session(runtime.engine) as session:
        task = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                   AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                   AgentTask.status == TaskStatus.PENDING)).one()
        previous = task.input_data["previous_plan"]
        assert [segment.get("asset_id") for segment in previous["segments"]] == expected_ids
        assert not any(segment.get("asset_request") for segment in previous["segments"])
        assert ids["bad_image"] not in {segment.get("asset_id") for segment in previous["segments"]}
        assert task.input_data["upstream_task_ids"] == {"script": ids["new_script"]}
        files = context_files_for(task, session)
        assert {session.get(Artifact, key).relative_path for key in expected_ids}.issubset(files)
        for key, snapshot in frozen.items():
            old = session.get(AgentTask, key)
            assert (old.input_data, old.input_hash, old.result_data) == snapshot
        assert session.get(AgentTask, ids["failed_qc"]).result_data["passed"] is False


def test_legacy_qc_keeps_raw_previous_plan_without_claiming_resolved_approval(audio_rewrite_images):
    runtime, content_id, ids, _, frozen = audio_rewrite_images
    with Session(runtime.engine) as session:
        failed = session.get(AgentTask, ids["failed_qc"])
        failed.schema_version = "2"
        session.add(failed)
        session.commit()
    pipeline.approve_script(runtime.engine, runtime.settings, content_id)
    with Session(runtime.engine) as session:
        task = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                   AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                   AgentTask.status == TaskStatus.PENDING)).one()
        assert task.input_data["previous_plan"] == frozen[ids["plan"]][2]
        assert task.input_data["previous_plan"]["segments"][2].get("asset_request")
        assert task.input_data["previous_plan"]["segments"][2].get("asset_id") is None


@pytest.mark.parametrize("damage", ["manifest", "image", "plan_qc", "full_qc", "missing_full_qc",
                                    "generation_receipt", "failed_qc_receipt", "qc_input_hash", "subject",
                                    "coverage", "full_qc_script"])
def test_audio_rewrite_rejects_unproven_resolved_context_before_invalidating_history(audio_rewrite_images, damage):
    runtime, content_id, ids, _, _ = audio_rewrite_images
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        failed = session.get(AgentTask, ids["failed_qc"])
        if damage == "manifest":
            (Path(content.content_dir) / "joint-manifest.json").write_bytes(b"changed")
        elif damage == "image":
            artifact = session.get(Artifact, ids["image"])
            (Path(content.content_dir) / artifact.relative_path).write_bytes(b"changed")
        elif damage in {"plan_qc", "full_qc"}:
            task = session.get(AgentTask, ids[damage])
            task.result_data = {"passed": False}
            session.add(task)
        elif damage == "missing_full_qc":
            task = session.get(AgentTask, ids["full_qc"])
            task.status = TaskStatus.CANCELLED
            session.add(task)
        elif damage in {"generation_receipt", "failed_qc_receipt"}:
            target = "generation" if damage == "generation_receipt" else "failed_qc"
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == ids[target],
                                                         AuditEvent.action == AGENT_CONSUMED)).one()
            session.delete(event)
        elif damage == "qc_input_hash":
            failed.input_hash = "changed"
        elif damage == "subject":
            failed.input_data = {**failed.input_data, "acceptance_subject": {}}
            failed.input_hash = stable_hash(failed.input_data)
        else:
            full = session.get(AgentTask, ids["full_qc"])
            full.input_data = {**full.input_data, **(
                {"assets": full.input_data["assets"][:-1]} if damage == "coverage" else {"script": {}})}
            full.input_hash = stable_hash(full.input_data)
            owned = session.exec(select(AuditEvent).where(AuditEvent.entity_id == full.id,
                                                         AuditEvent.action == "mpt_task_owned")).one()
            owned.metadata_json = {"input_hash": full.input_hash}
            session.add(owned)
            session.add(full)
        session.add(failed)
        session.commit()
    with pytest.raises(NonRetryableMediaError, match="Previous visual context"):
        pipeline.approve_script(runtime.engine, runtime.settings, content_id)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, ids["plan"]).status == TaskStatus.COMPLETED
        assert not session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                       AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                       AgentTask.status == TaskStatus.PENDING)).all()
