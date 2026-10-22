"""A real HTTP/SQLite revoice creates new lineage and preserves completed history."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from glance_core.content_acceptance import acceptance_subject, generated_image_material, resolved_visual_plan
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, CandidateStatus, Content, ContentStatus, ProductionMode,
    PublicationMode, PublicationRecord, RightsStatus, SchedulerOffer, StageRun, StageStatus, TaskStatus, TaskType, utc_now,
)
from glance_core.task_service import stable_hash
from glance_core.settings import resolve_kokoro_voice
from glance_core.voice_generation import GENERATION_ACTION, artifact_binding, verify_voice_generation
from glance_core.workflow import WorkflowService
from glance_media import executor as portable
from glance_media.tts import TtsResult
from sqlmodel import Session, select

from app.business import pipeline, voice_rebuild
from test.business.test_content_http import RESEARCH, content_app
from test.business.test_discovery_http import ADMIN, make_runtime


def sha(data):
    return hashlib.sha256(data).hexdigest()


def receipt_bundle():
    text = "Retained original license\r\nSecond line\r\n"
    metadata = {"path": "licenses/LICENSE.txt", "sha256": sha(text.encode()), "size_bytes": len(text.encode()), "role": "license"}
    runtime = {"distribution": "sherpa-onnx", "version": "1.13.8", "core_version": "1.13.8", "python": "cp311",
               "platform": "win_amd64", "provider": "cpu", "bundled_onnxruntime_version": "1.28.2"}
    return {"model_id": "kokoro-v1.1-zh", "manifest_sha256": "a" * 64, "runtime": runtime, "files": [metadata],
            "licenses": [{"path": metadata["path"], "sha256": metadata["sha256"], "spdx_id": "Apache-2.0",
                          "scope": "fixture", "source_url": "https://example.org/license", "text": text}],
            "supporting_license_documents": [], "rights_summary": "License text evidence does not determine legal authorization.",
            "provenance_limits": ["No independent speaker-rights audit."]}


@pytest.fixture
def contract(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    runtime.settings = runtime.settings.model_copy(update={
        "tts_provider": "kokoro", "tts_voice": "kokoro:58", "tts_rate": "+0%", "kokoro_model_dir": str(tmp_path / "model"),
        "asset_image_enabled": True, "min_video_seconds": 20, "max_video_seconds": 90,
    })
    runtime.initialize()
    calls, fail = [], {"provider": False, "during": None, "voice_override": None}
    bundle = receipt_bundle()
    monkeypatch.setattr(voice_rebuild, "verify_kokoro_bundle", lambda _: deepcopy(bundle))

    class Provider:
        def synthesize(self, narration, output, **options):
            calls.append((narration, options))
            output.write_bytes(f"local speech {len(calls)}".encode())
            if fail["during"]:
                fail["during"]()
            if fail["provider"]:
                raise RuntimeError("synthetic provider failure")
            name, sid = resolve_kokoro_voice(options["voice"])
            receipt = {"schema_version": 1, "provider": "kokoro", "generated_at": utc_now().isoformat(),
                       "bundle": deepcopy(bundle), "runtime": {**bundle["runtime"], "num_threads": 2},
                       "voice": fail["voice_override"] or {"id": name, "sid": sid}, "narration_sha256": sha(narration.encode()),
                       "parameters": {"rate": options["rate"], "speed": 1.0, "volume": "+0%", "gain": 1.0},
                       "audio": {"sample_rate": 24000, "sample_count": 720000, "pcm_duration_sec": 30.0,
                                 "encoded_duration_sec": 30.0, "peak_amplitude": .5, "pcm16_sha256": "b" * 64},
                       "output_sha256": sha(output.read_bytes()), "output_size_bytes": output.stat().st_size}
            return TtsResult(output, 30.0, [], "kokoro", provenance=receipt)

    monkeypatch.setattr(portable, "get_tts_provider", lambda *args, **kwargs: Provider())
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        candidate, _ = flow.ingest_candidate(source_type="agent", source_id="kokoro-rebuild", source_url="https://example.org/rebuild", original_title="rebuild")
        candidate.status, candidate.rights_status = CandidateStatus.SELECTED, RightsStatus.SELF_OWNED
        session.add(candidate)
        root = runtime.settings.content_dir / "rebuild"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="rebuild", content_dir=str(root), business_status=ContentStatus.PACKAGED,
                          current_stage="package", production_mode=ProductionMode.IMAGE_ONLY)
        session.add(content)
        session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content", entity_id=content.id,
                               action="manual_candidate_selected"))
        session.commit()

        def completed(kind, inputs, result, schema="1"):
            task = flow.create_agent_task(content.id, kind, inputs, allow_multiple=True, schema_version=schema)
            task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
            session.add(task)
            session.commit()
            return task

        def artifact(kind, relative, data, producer="fixture"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            item = Artifact(content_id=content.id, artifact_type=kind, relative_path=relative, sha256=sha(data), size_bytes=len(data),
                            mime_type="image/png" if "image" in kind else "audio/mpeg", producer_stage=producer)
            session.add(item)
            session.commit()
            return item

        research = completed(TaskType.CONTENT_RESEARCH, {}, {**deepcopy(RESEARCH), "verified_claims": []})
        prove_completed(session, runtime, research)
        script = completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}},
                           {"full_narration": "已批准的原文，面积为2.5平方米。", "estimated_duration_sec": 30})
        completed(TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}}, {"passed": True})
        old_voice = artifact("voice", "audio/old/narration.mp3", b"historical Edge speech")
        (root / old_voice.relative_path).with_name("word_boundaries.json").write_text("[]")
        plan = completed(TaskType.VISUAL_PLAN, {"upstream_task_ids": {"script": script.id}, "voice_artifact": artifact_binding(old_voice)}, {"segments": []})
        requests = [{"segment_index": i, "asset_request": {"prompt": f"image {i}"}} for i in range(7)]
        generation = completed(TaskType.ASSET_GENERATE_IMAGE, {"upstream_task_ids": {"visual_plan": plan.id}, "requests": requests}, {})
        images = [artifact("agent_generated_image", f"images/image-{i}.png", f"image-{i}".encode(), generation.id) for i in range(7)]
        generation.result_data = {"images": [{"segment_index": i, "artifact_id": item.id} for i, item in enumerate(images)]}
        plan.result_data = {"segments": [{"asset_id": item.id, "layout": "image_card", "narration_start_sec": i * 30 / 7,
                                          "narration_end_sec": (i + 1) * 30 / 7, "purpose": f"image {i}"} for i, item in enumerate(images)]}
        session.add(generation)
        session.add(plan)
        session.commit()
        completed(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id, "script": script.id}}, {"passed": True})
        completed(TaskType.ASSET_QC, {"upstream_task_ids": {"visual_plan": plan.id}, "full_generated_assets": True,
                                     "assets": [{"segment_index": i, **generated_image_material(session, content, item.id)} for i, item in enumerate(images)]}, {"passed": True})
        final = artifact("final_video", "render/old-final.mp4", b"old final video")
        refs = {"script": script.id, "visual_plan": plan.id, "video_artifact_id": final.id}
        subject = acceptance_subject(session, content.id, refs)
        completed(TaskType.CONTENT_QC, {"upstream_task_ids": {"script": script.id, "visual_plan": plan.id},
                                       "video_artifact_id": final.id, "acceptance_contract_version": 2,
                                       "acceptance_origin": "manual", "acceptance_subject": subject},
                  {"passed": True, "subject": subject}, "2")
        negative = completed(TaskType.PUBLISH_REVIEW_ASSIST, {"fixture": "old negative"}, {"verdict": "BLOCKED", "reasons": ["historical missing provider proof"]})
        recovery = completed(TaskType.RECOVERY, {"fixture": "budget", "lineage": {"round": 4, "same_task_retries": 3}}, {"historical": True})
        ids = {"content": content.id, "script": script.id, "plan": plan.id, "voice": old_voice.id,
               "images": [item.id for item in images], "negative": negative.id, "recovery": recovery.id}
        for task in session.exec(select(AgentTask).where(AgentTask.content_id == content.id)).all():
            if task.id != research.id:
                prove_completed(session, runtime, task)
    with TestClient(content_app(runtime)) as http:
        yield runtime, http, ids, calls, fail, bundle


def old_snapshot(runtime, content_id):
    with Session(runtime.engine) as session:
        return {task.id: stable_hash(task.model_dump(mode="json")) for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()}


def prove_completed(session, runtime, task):
    task.attempt, task.assigned_agent_id = 1, "fixture-worker"
    raw = runtime.settings.agent_submission_dir / task.id / "1/result.json"
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(task.result_data), encoding="utf-8")
    session.add(task)
    session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=task.assigned_agent_id, raw_result_path=str(raw),
                               result_hash=stable_hash(task.result_data), validation_status="ACCEPTED"))
    for action, metadata in (("completed", {"result_hash": stable_hash(task.result_data)}),
                             ("mpt_task_owned", {"input_hash": task.input_hash}),
                             ("mpt_agent_consumed", {"result_hash": stable_hash(task.result_data)})):
        session.add(AuditEvent(actor_type="agent" if action == "completed" else "system", actor_id=task.assigned_agent_id,
                               entity_type="agent_task", entity_id=task.id, action=action, metadata_json=metadata))
    session.commit()


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/rebuild-voice"


def test_rebuild_creates_exact_new_lineage_and_preserves_history(contract):
    runtime, http, ids, calls, _, _ = contract
    before = old_snapshot(runtime, ids["content"])
    assert http.post(endpoint(ids)).status_code == 401
    response = http.post(endpoint(ids), headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["business_status"] == "WAITING_AGENT" and len(calls) == 1
    assert "2.5" in calls[0][0] and calls[0][1]["rate"] == "+0%"
    after = old_snapshot(runtime, ids["content"])
    assert all(after[key] == value for key, value in before.items())
    with Session(runtime.engine) as session:
        task = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        assert task.id != ids["plan"] and task.input_data["allowed_asset_ids"] == ids["images"]
        assert [row["asset_id"] for row in task.input_data["previous_plan"]["segments"]] == ids["images"]
        generation = task.input_data["voice_generation"]
        content, script = session.get(Content, ids["content"]), session.get(AgentTask, ids["script"])
        summary, receipt = verify_voice_generation(session, runtime.settings, content, task.input_data["voice_artifact"], generation, script)
        assert summary == generation and summary["actual_provider_verified"] is True
        assert "text" not in summary["licenses"][0] and len(json.dumps(summary)) < 12000
        assert receipt.relative_path in task.input_data["context_files"]
        assert pipeline._has_manual_script_approval(session, runtime.settings, content.id)
        assert session.get(AgentTask, ids["negative"]).result_data["verdict"] == "BLOCKED"
        assert session.get(Artifact, ids["voice"]).sha256 == sha(b"historical Edge speech")
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 409 and len(calls) == 1


def test_concurrent_rebuild_has_one_speech_generation(contract):
    runtime, http, ids, calls, _, _ = contract
    runtime.run_content_stage(ids["content"], lambda *args: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: http.post(endpoint(ids), headers=ADMIN).status_code, range(2)))
    assert sorted(statuses) == [200, 409] and len(calls) == 1


def test_female_voice_three_is_preserved_in_signed_receipt(contract):
    runtime, http, ids, calls, _, _ = contract
    runtime.settings.tts_voice = "kokoro:3"
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 200
    assert calls[0][1]["voice"] == "kokoro:3"
    with Session(runtime.engine) as session:
        plan = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        assert plan.input_data["voice_generation"]["voice"] == {"id": "zf_001", "sid": 3}


@pytest.mark.parametrize("actual", [{"id": "zf_001", "sid": 58}, {"id": "zm_009", "sid": 3}, {"id": "zf_001", "sid": 3}])
def test_mismatched_or_unrequested_voice_cannot_receive_server_binding(contract, actual):
    runtime, http, ids, _, fail, _ = contract
    fail["voice_override"] = actual
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 409
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert not session.exec(select(AuditEvent).where(AuditEvent.action == GENERATION_ACTION)).all()


def test_reused_seven_images_enter_fresh_plan_and_full_asset_qc(contract):
    runtime, http, ids, calls, _, _ = contract
    before = old_snapshot(runtime, ids["content"])
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        plan = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        plan.status, plan.result_data, plan.completed_at = TaskStatus.COMPLETED, deepcopy(plan.input_data["previous_plan"]), utc_now()
        prove_completed(session, runtime, plan)
        plan_id = plan.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, plan_id)
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
                                                AgentTask.status == TaskStatus.PENDING)).one()
        assert qc.input_data["upstream_task_ids"]["visual_plan"] == plan_id
        qc.status, qc.result_data, qc.completed_at = TaskStatus.COMPLETED, {"passed": True}, utc_now()
        prove_completed(session, runtime, qc)
        qc_id = qc.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, qc_id)
    with Session(runtime.engine) as session:
        full = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.ASSET_QC,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        assert full.input_data["full_generated_assets"] is True
        assert [row["artifact_id"] for row in full.input_data["assets"]] == ids["images"]
        assert full.input_data["upstream_task_ids"]["visual_plan"] == plan_id
        full.status, full.result_data, full.completed_at = TaskStatus.COMPLETED, {"passed": True}, utc_now()
        prove_completed(session, runtime, full)
        full_id = full.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, full_id)
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).current_stage == "render_ready"
        resolved = resolved_visual_plan(session, session.get(AgentTask, plan_id))
        assert [segment["asset_id"] for segment in resolved["segments"]] == ids["images"]
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == GENERATION_ACTION)).all()) == 1
    after = old_snapshot(runtime, ids["content"])
    assert all(after[key] == value for key, value in before.items()) and len(calls) == 1


def test_publish_review_includes_compact_verified_generation_and_full_receipt_context(contract):
    runtime, http, ids, _, _, _ = contract
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        plan = session.exec(select(AgentTask).where(AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        plan.status, plan.result_data, plan.completed_at = TaskStatus.COMPLETED, deepcopy(plan.input_data["previous_plan"]), utc_now()
        prove_completed(session, runtime, plan)
        # Exercise the production-evidence builder directly; unrelated package gaps remain explicit.
        publish = session.exec(select(Artifact).where(Artifact.content_id == content.id, Artifact.artifact_type == "final_video")).one()
        cover_path = Path(content.content_dir) / "cover.jpg"
        cover_path.write_bytes(b"cover")
        cover = Artifact(content_id=content.id, artifact_type="cover", relative_path="cover.jpg", sha256=sha(b"cover"), size_bytes=5,
                         mime_type="image/jpeg", producer_stage="fixture")
        session.add(cover)
        session.commit()
        contexts = []
        evidence = runtime.workflow_service(session)._publish_production_evidence(content, publish, cover, contexts, runtime.settings)
        voice = evidence["voice"]
        assert voice["provider"] == "kokoro" and voice["generation"]["actual_provider_verified"] is True
        assert voice["generation_receipt"]["bytes_verified"] is True
        assert voice["generation"]["receipt_artifact"]["relative_path"] in contexts
        assert "text" not in voice["generation"]["licenses"][0]
        receipt = session.get(Artifact, voice["generation"]["receipt_artifact"]["id"])
        (Path(content.content_dir) / receipt.relative_path).write_bytes(b"{}")
        contexts.clear()
        tampered = runtime.workflow_service(session)._publish_production_evidence(content, publish, cover, contexts, runtime.settings)
        assert tampered["voice"]["provider"] == "unknown"
        assert any("generation receipt failed" in gap for gap in tampered["evidence_gaps"])


def test_old_normalized_license_receipt_cannot_be_registered(contract):
    runtime, http, ids, _, _, bundle = contract
    bundle["licenses"][0]["text"] = bundle["licenses"][0]["text"].replace("\r\n", "\n")
    response = http.post(endpoint(ids), headers=ADMIN)
    assert response.status_code == 409 and "license bytes changed" in response.text
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert not session.exec(select(AuditEvent).where(AuditEvent.action == GENERATION_ACTION)).all()
        assert len(session.exec(select(Artifact).where(Artifact.content_id == ids["content"], Artifact.artifact_type == "voice")).all()) == 1


@pytest.mark.parametrize("blocker", ["status", "rights", "script-qc", "content-qc", "content-run", "pending", "leased", "submitted", "running-stage", "publication", "intent", "model"])
def test_preflight_blockers_never_generate_or_mutate_old_tasks(contract, monkeypatch, blocker):
    runtime, http, ids, calls, _, _ = contract
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        if blocker == "status":
            content.business_status = ContentStatus.FINAL_REVIEW
            session.add(content)
        elif blocker == "rights":
            from glance_core.models import Candidate

            candidate = session.get(Candidate, content.candidate_id)
            candidate.rights_status = RightsStatus.REJECTED
            session.add(candidate)
        elif blocker in {"pending", "leased", "submitted"}:
            session.add(AgentTask(content_id=content.id, task_type=TaskType.RECOVERY, prompt_name="busy", instructions="busy",
                                  input_hash="busy", status=TaskStatus(blocker.upper())))
        elif blocker in {"script-qc", "content-qc"}:
            kind = TaskType.SCRIPT_QC if blocker == "script-qc" else TaskType.CONTENT_QC
            qc = session.exec(select(AgentTask).where(AgentTask.content_id == content.id, AgentTask.task_type == kind)).one()
            qc.result_data = {**qc.result_data, "passed": False}
            session.add(qc)
        elif blocker == "content-run":
            from app.business.content_run import ContentRun, save_run

            now = utc_now()
            run = ContentRun(run_id="run_" + "1" * 32, content_id=content.id, generation=1, state="running",
                             created_at=now.isoformat(), expires_at=(now + timedelta(minutes=60)).isoformat())
            save_run(session, run, "mpt_content_run_started")
        elif blocker == "running-stage":
            session.add(StageRun(content_id=content.id, stage="render", status=StageStatus.RUNNING, input_hash="busy"))
        elif blocker == "publication":
            session.add(PublicationRecord(content_id=content.id, platform="douyin", mode=PublicationMode.EXECUTOR))
        elif blocker == "intent":
            session.add(AuditEvent(actor_type="agent", actor_id="fixture", entity_type="publish_intent", entity_id="intent",
                                   action="publish_intent_registered", metadata_json={"content_id": content.id}))
        elif blocker == "model":
            def bad_model(_):
                raise ValueError("tampered model")

            monkeypatch.setattr(voice_rebuild, "verify_kokoro_bundle", bad_model)
        session.commit()
    before = old_snapshot(runtime, ids["content"])
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 409
    assert calls == [] and old_snapshot(runtime, ids["content"]) == before


def test_active_scheduler_offer_blocks_before_generation(contract):
    runtime, http, ids, calls, _, _ = contract
    with Session(runtime.engine) as session:
        session.add(SchedulerOffer(request_id="live-offer", scheduler_id="fixture", agent_id="fixture", task_id=ids["plan"],
                                   expires_at=utc_now() + timedelta(minutes=1)))
        session.commit()
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 409 and calls == []


def test_provider_failure_preserves_packaged_subject(contract):
    runtime, http, ids, calls, fail, _ = contract
    fail["provider"] = True
    before = old_snapshot(runtime, ids["content"])
    with pytest.raises(RuntimeError, match="synthetic provider failure"):
        http.post(endpoint(ids), headers=ADMIN)
    assert len(calls) == 1 and old_snapshot(runtime, ids["content"]) == before
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert len(session.exec(select(Artifact).where(Artifact.content_id == ids["content"], Artifact.artifact_type == "voice")).all()) == 1
        assert session.exec(select(StageRun).where(StageRun.content_id == ids["content"])).one().status == StageStatus.FAILED


def test_crash_after_committed_generation_reuses_exact_speech(contract, monkeypatch):
    runtime, http, ids, calls, _, _ = contract
    original = WorkflowService.create_agent_task

    def crash(self, content_id, task_type, inputs, **kwargs):
        if inputs.get("voice_rebuild_input_sha256"):
            raise RuntimeError("crash before plan commit")
        return original(self, content_id, task_type, inputs, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(WorkflowService, "create_agent_task", crash)
        with pytest.raises(RuntimeError, match="crash before plan commit"):
            http.post(endpoint(ids), headers=ADMIN)
    assert len(calls) == 1
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert session.exec(select(StageRun).where(StageRun.content_id == ids["content"])).one().status == StageStatus.SUCCESS
    response = http.post(endpoint(ids), headers=ADMIN)
    assert response.status_code == 200, response.text
    assert len(calls) == 1


def test_crash_after_plan_commit_cannot_generate_again(contract, monkeypatch):
    runtime, http, ids, calls, _, _ = contract
    original = WorkflowService.create_agent_task

    def crash(self, content_id, task_type, inputs, **kwargs):
        result = original(self, content_id, task_type, inputs, **kwargs)
        if inputs.get("voice_rebuild_input_sha256"):
            raise RuntimeError("crash after plan commit")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(WorkflowService, "create_agent_task", crash)
        with pytest.raises(RuntimeError, match="crash after plan commit"):
            http.post(endpoint(ids), headers=ADMIN)
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 409 and len(calls) == 1
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.WAITING_AGENT
        assert len(session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                       AgentTask.status == TaskStatus.PENDING)).all()) == 1


def test_normal_script_approval_passes_model_directory_and_registers_generation(contract, monkeypatch):
    runtime, http, ids, calls, _, bundle = contract
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
        session.add(content)
        session.commit()
    response = http.post(f"/api/v1/contents/{ids['content']}/approve-script", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    with Session(runtime.engine) as session:
        plan = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        generation = plan.input_data["voice_generation"]
        assert generation["model_manifest_sha256"] == bundle["manifest_sha256"]
        assert generation["receipt_artifact"]["relative_path"] in plan.input_data["context_files"]
        assert session.get(StageRun, generation["stage_id"]).input_hash == generation["voice_input_hash"]


@pytest.mark.parametrize("checkpoint", ["success-commit", "before-plan", "orphan-success", "orphan-running"])
def test_normal_approval_crash_never_repeats_committed_or_unproven_generation(contract, monkeypatch, checkpoint):
    runtime, http, ids, calls, _, bundle = contract
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
        session.add(content)
        session.commit()
    original_commit, original_invalidate = Session.commit, WorkflowService.invalidate_visual_plan
    armed = [True]

    def crash_commit(self):
        original_commit(self)
        if armed[0] and calls and self.exec(select(StageRun).where(StageRun.status == StageStatus.SUCCESS)).first():
            armed[0] = False
            assert self.exec(select(AuditEvent).where(AuditEvent.action == GENERATION_ACTION)).one()
            assert self.exec(select(Artifact).where(Artifact.artifact_type == "tts_generation_receipt")).one()
            raise ValueError("crash immediately after success commit")

    def crash_plan(self, *args, **kwargs):
        raise ValueError("crash before visual invalidation")

    if checkpoint == "success-commit":
        monkeypatch.setattr(Session, "commit", crash_commit)
    else:
        monkeypatch.setattr(WorkflowService, "invalidate_visual_plan", crash_plan)
    url = f"/api/v1/contents/{ids['content']}/approve-script"
    assert http.post(url, headers=ADMIN).status_code == 409
    assert len(calls) == 1
    monkeypatch.setattr(Session, "commit", original_commit)
    monkeypatch.setattr(WorkflowService, "invalidate_visual_plan", original_invalidate)
    with Session(runtime.engine) as session:
        assert len(session.exec(select(Artifact).where(Artifact.artifact_type == "voice")).all()) == 2
        if checkpoint.startswith("orphan-"):
            event = session.exec(select(AuditEvent).where(AuditEvent.action == GENERATION_ACTION)).one()
            session.delete(event)
            if checkpoint == "orphan-running":
                stage = session.exec(select(StageRun).where(StageRun.stage == "voice")).one()
                stage.status = StageStatus.RUNNING
                session.add(stage)
            session.commit()
    response = http.post(url, headers=ADMIN)
    assert response.status_code == (409 if checkpoint.startswith("orphan-") else 200), response.text
    assert len(calls) == 1
    with Session(runtime.engine) as session:
        assert len(session.exec(select(StageRun).where(StageRun.stage == "voice")).all()) == 1


def test_normal_approval_uses_persisted_kokoro_options_over_edge_base(contract, monkeypatch):
    from glance_core.models import RuntimeSetting

    runtime, http, ids, calls, _, bundle = contract
    selected = []
    factory = portable.get_tts_provider

    def capture(provider, *args, **kwargs):
        selected.append(provider)
        return factory(provider, *args, **kwargs)

    monkeypatch.setattr(portable, "get_tts_provider", capture)
    runtime.settings = runtime.settings.model_copy(update={"tts_provider": "edge", "tts_voice": "zh-CN-YunxiNeural", "tts_rate": "+20%"})
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    with Session(runtime.engine) as session:
        for key, value in {"tts_provider": "kokoro", "tts_voice": "kokoro:3", "tts_rate": "+0%"}.items():
            session.add(RuntimeSetting(key=key, value=value))
        content = session.get(Content, ids["content"])
        content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
        session.add(content)
        session.commit()
    response = http.post(f"/api/v1/contents/{ids['content']}/approve-script", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert len(calls) == 1
    assert selected == ["kokoro"] and calls[0][1]["voice"] == "kokoro:3" and calls[0][1]["rate"] == "+0%"
    with Session(runtime.engine) as session:
        plan = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                   AgentTask.status == TaskStatus.PENDING)).one()
        assert plan.input_data["voice_generation"]["voice"] == {"id": "zf_001", "sid": 3}


@pytest.mark.parametrize("complete_proof", [True, False])
def test_automatic_third_attempt_reuses_complete_success_without_extending_budget(contract, monkeypatch, complete_proof):
    runtime, _, ids, calls, fail, bundle = contract
    runtime.settings = runtime.settings.model_copy(update={"handsfree_enabled": True, "handsfree_auto_review_enabled": True})
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
        session.add(content)
        session.add(AuditEvent(actor_type="system", actor_id="handsfree-coordinator", entity_type="content",
                               entity_id=content.id, action="handsfree_candidate_selected"))
        flow = runtime.workflow_service(session)
        research = flow.completed_task_for(content.id, TaskType.CONTENT_RESEARCH)
        script = session.get(AgentTask, ids["script"])
        qc = flow.completed_task_for(content.id, TaskType.SCRIPT_QC)
        review = flow.create_agent_task(content.id, TaskType.SCRIPT_REVIEW_ASSIST, {
            "upstream_task_ids": {"script": script.id, "script_qc": qc.id, "research": research.id},
            "script": deepcopy(script.result_data), "script_qc": deepcopy(qc.result_data), "research": deepcopy(research.result_data),
        }, allow_multiple=True)
        review.status, review.completed_at = TaskStatus.COMPLETED, utc_now()
        review.result_data = {"verdict": "APPROVE", "confidence": "high", "summary": "Reviewed current script",
                              "reasons": ["Verified research and narration"], "issues": [], "checklist": []}
        prove_completed(session, runtime, review)
    fail["provider"] = True
    for _ in range(2):
        with pytest.raises(RuntimeError, match="synthetic provider failure"):
            pipeline.approve_script(runtime.engine, runtime.settings, ids["content"], automatic=True)
    if complete_proof:
        fail["provider"] = False
        with monkeypatch.context() as crashing:
            def crash(*args, **kwargs):
                raise ValueError("crash before plan after third success")

            crashing.setattr(WorkflowService, "invalidate_visual_plan", crash)
            with pytest.raises(ValueError, match="third success"):
                pipeline.approve_script(runtime.engine, runtime.settings, ids["content"], automatic=True)
        resumed = pipeline.approve_script(runtime.engine, runtime.settings, ids["content"], automatic=True)
        assert resumed.current_stage == "visual_plan"
    else:
        for _ in range(2):
            with pytest.raises(pipeline.VoiceRetryExhausted, match="VOICE_RETRY_EXHAUSTED"):
                pipeline.approve_script(runtime.engine, runtime.settings, ids["content"], automatic=True)
    assert len(calls) == 3
    with Session(runtime.engine) as session:
        assert len(session.exec(select(StageRun).where(StageRun.stage == "voice")).all()) == 3


def test_late_script_change_does_not_promote_voice(contract):
    runtime, http, ids, _, fail, _ = contract

    def change():
        with Session(runtime.engine) as session:
            script = session.get(AgentTask, ids["script"])
            script.result_data = {**script.result_data, "full_narration": "changed after approval"}
            session.add(script)
            session.commit()

    fail["during"] = change
    response = http.post(endpoint(ids), headers=ADMIN)
    assert response.status_code == 409
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert len(session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.task_type == TaskType.VISUAL_PLAN)).all()) == 1


@pytest.mark.parametrize("damage", ["voice", "receipt", "license-newlines", "script", "summary", "signature", "stage", "model"])
def test_signed_generation_verification_rejects_tampered_evidence(contract, damage):
    runtime, http, ids, _, _, _ = contract
    assert http.post(endpoint(ids), headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        task = session.exec(select(AgentTask).where(AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
        summary = deepcopy(task.input_data["voice_generation"])
        receipt = session.get(Artifact, summary["receipt_artifact"]["id"])
        voice = session.get(Artifact, task.input_data["voice_artifact"]["id"])
        script = session.get(AgentTask, ids["script"])
        event = session.get(AuditEvent, summary["binding_event_id"])
        if damage == "voice":
            (Path(content.content_dir) / voice.relative_path).write_bytes(b"tampered")
        elif damage == "receipt":
            (Path(content.content_dir) / receipt.relative_path).write_bytes(b"{}")
        elif damage in {"license-newlines", "model"}:
            data = json.loads((Path(content.content_dir) / receipt.relative_path).read_bytes())
            if damage == "license-newlines":
                data["bundle"]["licenses"][0]["text"] = data["bundle"]["licenses"][0]["text"].replace("\r\n", "\n")
            else:
                data["bundle"]["manifest_sha256"] = "c" * 64
            raw = json.dumps(data).encode()
            (Path(content.content_dir) / receipt.relative_path).write_bytes(raw)
            receipt.sha256, receipt.size_bytes = sha(raw), len(raw)
            session.add(receipt)
        elif damage == "script":
            script.result_data = {"full_narration": "different approved script"}
        elif damage == "summary":
            summary["model_manifest_sha256"] = "c" * 64
        elif damage == "signature":
            event.metadata_json = {**event.metadata_json, "signature": "0" * 64}
        elif damage == "stage":
            session.get(StageRun, summary["stage_id"]).status = StageStatus.FAILED
        session.commit()
        with pytest.raises(ValueError):
            verify_voice_generation(session, runtime.settings, content, task.input_data["voice_artifact"], summary, script)
