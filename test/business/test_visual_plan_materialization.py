"""Asset approval binds rendering without rewriting the accepted visual result."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from fastapi.testclient import TestClient
from glance_core.database import create_db
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, CandidateStatus, Content, ContentStatus, ProductionMode, RightsStatus, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_service import stable_hash
from glance_core.workflow import _failed_segment_indexes
from glance_media.executor import sha256_file
from sqlmodel import Session, select

from app.business import pipeline
from app.business.runtime import AGENT_CONSUMED, OWNED
from test.business.test_discovery_http import make_runtime
from test.business.test_discovery_http import ADMIN
from test.business.test_deployment_http import app_for
from test.business.test_agent_failure_recovery import registered_client


@pytest.mark.parametrize(
    ("field", "segment_index", "segment_count", "expected"),
    [
        ("assets[7].request.subject", 7, 8, {7}),
        ("assets[7].request.seed", 7, 8, {7}),
        ("assets[7].request.subject", None, 8, None),
        ("assets[7].request.subject", 6, 8, None),
        ("assets[7].request.subject", True, 8, None),
        ("assets[7].request.subject", "7", 8, None),
        ("assets[8].request.subject", 8, 8, None),
        ("assets[7].request.unknown", 7, 8, None),
        ("assets[0].request.subject", 7, 8, None),
    ],
)
def test_failed_segment_indexes_accepts_only_exact_asset_request_indexes(
    field, segment_index, segment_count, expected
):
    issue = {"severity": "error", "field": field, "message": "invalid asset", "segment_index": segment_index}
    assert _failed_segment_indexes([issue], segment_count) == expected


def test_failed_segment_indexes_preserves_legacy_segment_and_plain_asset_fields():
    assert _failed_segment_indexes(
        [{"severity": "error", "field": "segments[1].purpose", "message": "invalid"}], 3
    ) == {1}
    assert _failed_segment_indexes(
        [{"severity": "error", "field": "assets", "message": "invalid", "segment_index": 1}], 3
    ) == {1}


def test_recovery_reuses_only_qc_good_generated_segments(image_contract):
    runtime, content_id, plan_id, generation_id, qc_id, first_image_id, _ = image_contract
    issue = {
        "severity": "error",
        "field": "assets[1].request.subject",
        "message": "second image subject is incorrect",
        "segment_index": 1,
    }
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        generation = session.get(AgentTask, generation_id)
        qc = session.get(AgentTask, qc_id)
        content = session.get(Content, content_id)
        flow = runtime.workflow_service(session)

        second_path = Path(content.content_dir) / "second-image.png"
        second_path.write_bytes(b"second-generated-image")
        second = Artifact(
            content_id=content_id,
            artifact_type="agent_generated_image",
            relative_path=second_path.name,
            sha256=sha256_file(second_path),
            size_bytes=second_path.stat().st_size,
            mime_type="image/png",
            producer_stage=generation_id,
        )
        session.add(second)
        session.flush()
        second_image_id = second.id

        request = deepcopy(generation.input_data["requests"][0]["asset_request"])
        generation.input_data = deepcopy(generation.input_data)
        generation.input_data["requests"].append({"segment_index": 1, "asset_request": request})
        generation.input_hash = stable_hash(generation.input_data)
        owned = session.exec(
            select(AuditEvent).where(AuditEvent.entity_id == generation_id, AuditEvent.action == OWNED)
        ).one()
        owned.metadata_json = {"input_hash": generation.input_hash}
        generation.result_data = {
            "images": [
                *generation.result_data["images"],
                {"segment_index": 1, "artifact_id": second.id},
            ]
        }

        first_segment = deepcopy(plan.result_data["segments"][0])
        first_segment["asset_id"] = first_image_id
        second_segment = {
            **deepcopy(first_segment),
            "asset_id": second.id,
            "narration_start_sec": 3.0,
            "narration_end_sec": 6.0,
        }
        plan.result_data = {"segments": [first_segment, second_segment]}

        def refresh_accepted_result(task):
            accepted = session.exec(
                select(AgentSubmission).where(AgentSubmission.task_id == task.id)
            ).one()
            accepted.result_hash = stable_hash(task.result_data)
            Path(accepted.raw_result_path).write_text(json.dumps(task.result_data), encoding="utf-8")
            completed = session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_id == task.id,
                    AuditEvent.action == "completed",
                )
            ).one()
            completed.metadata_json = {"result_hash": accepted.result_hash}
            session.add(accepted)
            session.add(completed)
            consumed = session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_id == task.id,
                    AuditEvent.action == AGENT_CONSUMED,
                )
            ).first()
            if consumed:
                consumed.metadata_json = {"result_hash": accepted.result_hash}
                session.add(consumed)

        refresh_accepted_result(generation)
        refresh_accepted_result(plan)
        session.flush()

        qc.input_data = pipeline._asset_qc_input(session, flow, generation, plan)
        qc.input_hash = stable_hash(qc.input_data)
        qc.result_data = {"passed": False, "issues": [issue]}
        qc_owned = session.exec(
            select(AuditEvent).where(AuditEvent.entity_id == qc_id, AuditEvent.action == OWNED)
        ).one()
        qc_owned.metadata_json = {"input_hash": qc.input_hash}
        refresh_accepted_result(qc)
        session.add_all([second, generation, plan, qc, owned, qc_owned])
        session.commit()

        reusable, gaps, excluded = flow._visual_repair_generated_assets(
            plan, {"validation_errors": [issue]}
        )

    assert [material["artifact_id"] for material in reusable] == [first_image_id]
    assert excluded == {second_image_id}
    assert gaps == []


def record_accepted_completion(session, runtime, task, result):
    task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
    task.attempt, task.assigned_agent_id = 1, "fixture-worker"
    raw = runtime.settings.agent_submission_dir / task.id / "1" / "result.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(result), encoding="utf-8")
    session.add(task)
    session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=task.assigned_agent_id,
                               raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED"))
    session.add(AuditEvent(actor_type="agent", actor_id=task.assigned_agent_id,
                           entity_type="agent_task", entity_id=task.id, action="completed",
                           metadata_json={"result_hash": stable_hash(result)}))
    session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                           entity_id=task.id, action=OWNED, metadata_json={"input_hash": task.input_hash}))


@pytest.fixture
def image_contract(tmp_path):
    runtime = make_runtime(tmp_path)
    create_db(runtime.engine)
    with Session(runtime.engine) as session:
        candidate = Candidate(source_type="agent", source_id="materialization",
                              source_url="https://example.org/product", original_title="Product",
                              status=CandidateStatus.SELECTED, rights_status=RightsStatus.RESEARCH_ONLY)
        session.add(candidate)
        session.commit()
        root = runtime.settings.content_dir / "materialization"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="Product", content_dir=str(root),
                          production_mode=ProductionMode.IMAGE_ONLY,
                          business_status=ContentStatus.WAITING_AGENT, current_stage="asset_qc")
        session.add(content)
        session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                               entity_id=content.id, action="manual_candidate_selected"))
        session.commit()
        flow = runtime.workflow_service(session)

        def completed(kind, inputs, result, *, consumed=True):
            task = flow.create_agent_task(content.id, kind, inputs, allow_multiple=True)
            task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
            task.attempt, task.assigned_agent_id = 1, "fixture-worker"
            raw = runtime.settings.agent_submission_dir / task.id / "1" / "result.json"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps(result), encoding="utf-8")
            session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=task.assigned_agent_id,
                                        raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED"))
            session.add(AuditEvent(actor_type="agent", actor_id=task.assigned_agent_id,
                                   entity_type="agent_task", entity_id=task.id, action="completed",
                                   metadata_json={"result_hash": stable_hash(result)}))
            session.add(task)
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=task.id, action=OWNED, metadata_json={"input_hash": task.input_hash}))
            if consumed:
                session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                       entity_id=task.id, action=AGENT_CONSUMED,
                                       metadata_json={"result_hash": stable_hash(result)}))
            session.commit()
            return task

        research = completed(TaskType.CONTENT_RESEARCH, {}, {"verified_claims": []})
        script = completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}},
                           {"full_narration": "产品测试。"})
        completed(TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}}, {"passed": True})
        request = {"prompt": "generic concept illustration"}
        plan = completed(TaskType.VISUAL_PLAN, {
            "production_mode": "image_only", "upstream_task_ids": {"script": script.id},
        }, {"segments": [{"asset_request": request, "asset_id": None, "layout": "image_card",
                           "narration_start_sec": 0.0, "narration_end_sec": 3.0}]})
        completed(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id, "script": script.id}}, {"passed": True})
        generation = completed(TaskType.ASSET_GENERATE_IMAGE, {
            "upstream_task_ids": {"visual_plan": plan.id},
            "requests": [{"segment_index": 0, "asset_request": request}],
        }, {})
        image = root / "image.png"
        image.write_bytes(b"image-fixture")
        artifact = Artifact(content_id=content.id, artifact_type="agent_generated_image",
                            relative_path=image.name, sha256=sha256_file(image), size_bytes=image.stat().st_size,
                            mime_type="image/png", producer_stage=generation.id)
        session.add(artifact)
        generation.result_data = {"images": [{"segment_index": 0, "artifact_id": artifact.id}]}
        receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                      AuditEvent.action == AGENT_CONSUMED)).one()
        receipt.metadata_json = {"result_hash": stable_hash(generation.result_data)}
        accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation.id)).one()
        accepted.result_hash = stable_hash(generation.result_data)
        Path(accepted.raw_result_path).write_text(json.dumps(generation.result_data), encoding="utf-8")
        event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                    AuditEvent.action == "completed")).one()
        event.metadata_json = {"result_hash": accepted.result_hash}
        session.add(accepted)
        session.add(event)
        session.add(receipt)
        session.add(generation)
        session.commit()
        qc = completed(TaskType.ASSET_QC, pipeline._asset_qc_input(session, flow, generation, plan),
                       {"passed": True}, consumed=False)
        yield runtime, content.id, plan.id, generation.id, qc.id, artifact.id, deepcopy(plan.result_data)
    runtime.close()


def test_asset_qc_consume_preserves_plan_and_maintenance_receipt(image_contract, caplog):
    runtime, content_id, plan_id, _, qc_id, _, original = image_contract
    assert runtime.consume_agent(qc_id)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, plan_id).result_data == original
        content = session.get(Content, content_id)
        assert (content.business_status, content.current_stage) == (ContentStatus.PRODUCING, "render_ready")
    assert runtime.consume_agent(plan_id) is False
    assert runtime.consume_agent(qc_id) is False
    runtime.recover_agent_results()
    assert "Consumed Agent result changed" not in caplog.text


def test_first_asset_qc_automatic_render_uses_only_current_accepted_consumption(image_contract, monkeypatch):
    from glance_core.content_acceptance import resolved_visual_plan

    runtime, content_id, plan_id, _, qc_id, image_id, original = image_contract
    runtime.settings = runtime.settings.model_copy(update={"handsfree_enabled": True, "handsfree_auto_review_enabled": True})
    with Session(runtime.engine) as session:
        session.add(AuditEvent(actor_type="system", actor_id="handsfree-coordinator", entity_type="content",
                               entity_id=content_id, action="handsfree_candidate_selected"))
        session.commit()
    calls = []

    def render(engine, settings, target_id, *, consuming_task_id, automatic):
        assert automatic is True
        with Session(engine) as session:
            assert session.exec(select(AuditEvent.id).where(AuditEvent.entity_id == qc_id,
                                                           AuditEvent.action == AGENT_CONSUMED)).first() is None
            plan = session.get(AgentTask, plan_id)
            with pytest.raises(ValueError, match="accepted/consumed proof missing"):
                resolved_visual_plan(session, plan)
            session.info["consuming_agent_task_id"] = consuming_task_id
            assert resolved_visual_plan(session, plan)["segments"][0]["asset_id"] == image_id
            assert plan.result_data == original
            calls.append(target_id)

    monkeypatch.setattr(pipeline, "render_content", render)
    assert runtime.consume_agent(qc_id)
    assert runtime.consume_agent(qc_id) is False
    assert calls == [content_id]


def test_sparse_preparation_keeps_agent_result_and_qc_bundle_uses_same_frozen_plan(image_contract, tmp_path):
    from glance_core.content_acceptance import prepared_visual_plan

    runtime, content_id, old_plan_id, _, _, image_id, _ = image_contract
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        with Session(runtime.engine) as session:
            old = session.get(AgentTask, old_plan_id)
            inputs = {**old.input_data, "voice_duration_sec": 30, "image_only_gen_budget": 8,
                      "asset_image_enabled": True, "allowed_asset_ids": [image_id], "allowed_layouts": ["image_card"]}
            plan = runtime.workflow_service(session).create_agent_task(content_id, TaskType.VISUAL_PLAN, inputs, allow_multiple=True)
            original = {"segments": [{"asset_id": image_id, "layout": "image_card",
                                      "narration_start_sec": 0, "narration_end_sec": 30, "purpose": "Product"}]}
            record_accepted_completion(session, runtime, plan, original)
            content = session.get(Content, content_id)
            content.current_stage = "visual_plan"
            session.add(content)
            session.commit()
            plan_id = plan.id
        assert runtime.consume_agent(plan_id)
        with Session(runtime.engine) as session:
            plan = session.get(AgentTask, plan_id)
            assert plan.result_data == original
            qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                     AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
                                                     AgentTask.status == TaskStatus.PENDING)).one()
            prepared = deepcopy(qc.input_data["prepared_visual_plan"])
            assert len(prepared["segments"]) == 4
            assert qc.input_data["visual_plan_result_sha256"] == stable_hash(original)
            qc_id = qc.id
        claim = client.claim()
        assert claim["status"] == "CLAIMED", claim
        assert claim["task_id"] == qc_id
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        assert bundle["input"]["visual_plan"] == prepared
        with Session(runtime.engine) as session:
            qc = session.get(AgentTask, qc_id)
            qc.status, qc.result_data = TaskStatus.COMPLETED, {"passed": True}
            session.add(qc)
            session.commit()
            assert prepared_visual_plan(session, session.get(AgentTask, plan_id)) == prepared
        assert runtime.consume_agent(plan_id) is False


def test_resolved_materials_bind_render_and_research_only_acceptance(image_contract, monkeypatch):
    from glance_core.content_acceptance import acceptance_subject, resolved_visual_plan, source_context

    runtime, content_id, plan_id, _, qc_id, image_id, original = image_contract
    runtime.consume_agent(qc_id)
    seen = []

    class Rendered(Exception):
        pass

    def render(self, **arguments):
        seen.append(arguments["plan_data"])
        raise Rendered

    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *names: None)
    monkeypatch.setattr(pipeline, "_render_voice_path", lambda *args: Path("fixture-voice.mp3"))
    monkeypatch.setattr(pipeline.MediaExecutor, "render", render)
    with pytest.raises(Rendered):
        pipeline.render_content(runtime.engine, runtime.settings, content_id)
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        resolved = resolved_visual_plan(session, plan)
        assert resolved["segments"][0]["asset_id"] == image_id
        assert "asset_request" not in resolved["segments"][0]
        assert plan.result_data == original
        assert seen == [pipeline._render_plan_data(resolved)]
        script_id = plan.input_data["upstream_task_ids"]["script"]
        video = Path(session.get(Content, content_id).content_dir) / "final.mp4"
        video.write_bytes(b"final-fixture")
        artifact = Artifact(content_id=content_id, artifact_type="final_video", relative_path=video.name,
                            sha256=sha256_file(video), size_bytes=video.stat().st_size, mime_type="video/mp4", producer_stage="render")
        session.add(artifact)
        session.commit()
        refs = {"script": script_id, "visual_plan": plan_id, "video_artifact_id": artifact.id}
        context = source_context(session, content_id, refs)
        assert not context["evidence_gaps"]
        assert context["original_media"]["segments"][0]["artifact_id"] == image_id
        subject = acceptance_subject(session, content_id, refs, source_context_version=1)
        assert subject["visual_plan_sha256"] == stable_hash(original)
        assert subject["source_context_sha256"] == stable_hash(context)


@pytest.mark.parametrize("tamper", ["plan", "generation", "qc_result", "qc_input", "bytes", "foreign", "unowned", "plan_unowned", "unaccepted", "unconsumed", "new_generation", "new_qc", "cover", "script_unconsumed", "script_qc_unaccepted", "new_script", "new_script_qc", "research_unowned"])
def test_resolver_rejects_unproven_current_materials(image_contract, tamper):
    from glance_core.content_acceptance import resolved_visual_plan

    runtime, content_id, plan_id, generation_id, qc_id, image_id, _ = image_contract
    runtime.consume_agent(qc_id)
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        generation = session.get(AgentTask, generation_id)
        qc = session.get(AgentTask, qc_id)
        artifact = session.get(Artifact, image_id)
        if tamper in {"plan", "generation", "qc_result"}:
            row = {"plan": plan, "generation": generation, "qc_result": qc}[tamper]
            row.result_data = {**row.result_data, "tampered": True}
            session.add(row)
        elif tamper == "qc_input":
            qc.input_data = {**qc.input_data, "assets": []}
            session.add(qc)
        elif tamper == "bytes":
            (Path(session.get(Content, content_id).content_dir) / artifact.relative_path).write_bytes(b"tampered")
        elif tamper == "foreign":
            other_candidate = Candidate(source_type="agent", source_id="other",
                                        source_url="https://example.org/other", original_title="Other")
            session.add(other_candidate)
            session.flush()
            other = Content(candidate_id=other_candidate.id,
                            title="Other", content_dir=str(runtime.settings.content_dir / "other"))
            session.add(other)
            session.flush()
            artifact.content_id = other.id
            session.add(artifact)
        elif tamper in {"unowned", "plan_unowned", "unconsumed"}:
            action = AGENT_CONSUMED if tamper == "unconsumed" else OWNED
            target_id = plan_id if tamper == "plan_unowned" else generation_id
            receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == target_id,
                                                          AuditEvent.action == action)).one()
            session.delete(receipt)
        elif tamper == "unaccepted":
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation_id)).one()
            session.delete(submission)
        elif tamper in {"script_unconsumed", "script_qc_unaccepted", "research_unowned"}:
            kind = {"script_unconsumed": TaskType.SHORT_SCRIPT, "script_qc_unaccepted": TaskType.SCRIPT_QC,
                    "research_unowned": TaskType.CONTENT_RESEARCH}[tamper]
            parent = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == kind)).one()
            if tamper == "script_qc_unaccepted":
                session.delete(session.exec(select(AgentSubmission).where(AgentSubmission.task_id == parent.id)).one())
            else:
                action = AGENT_CONSUMED if tamper == "script_unconsumed" else OWNED
                session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == parent.id, AuditEvent.action == action)).one())
        elif tamper == "cover":
            generation.input_data = {**generation.input_data, "asset_purpose": "cover"}
            session.add(generation)
        else:
            kind = {"new_generation": TaskType.ASSET_GENERATE_IMAGE, "new_qc": TaskType.ASSET_QC,
                    "new_script": TaskType.SHORT_SCRIPT, "new_script_qc": TaskType.SCRIPT_QC}[tamper]
            inputs = generation.input_data if tamper == "new_generation" else qc.input_data
            newer = runtime.workflow_service(session).create_agent_task(content_id, kind, {**inputs, "revision": 2}, allow_multiple=True)
            newer.status = TaskStatus.FAILED
            session.add(newer)
        session.commit()
        with pytest.raises(ValueError):
            resolved_visual_plan(session, plan)


@pytest.fixture
def restoration_contract(image_contract, tmp_path):
    from glance_core.content_acceptance import acceptance_subject, resolved_visual_plan, source_context

    runtime, content_id, plan_id, _, qc_id, _, original = image_contract
    runtime.consume_agent(qc_id)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        with Session(runtime.engine) as session:
            plan = session.get(AgentTask, plan_id)
            plan.assigned_agent_id = client.settings.agent_id
            accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == plan_id)).one()
            accepted.agent_id = plan.assigned_agent_id
            completed = session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan_id,
                                                            AuditEvent.action == "completed")).one()
            completed.actor_id = plan.assigned_agent_id
            session.add(plan)
            session.add(accepted)
            session.add(completed)
            content = session.get(Content, content_id)
            content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "content_qc"
            session.add(content)
            root = Path(content.content_dir)
            video = root / "final.mp4"
            video.write_bytes(b"historical-final-video")
            artifact = Artifact(content_id=content_id, artifact_type="final_video", relative_path=video.name,
                                sha256=sha256_file(video), size_bytes=video.stat().st_size, mime_type="video/mp4", producer_stage="render")
            session.add(artifact)
            session.commit()
            refs = {"script": plan.input_data["upstream_task_ids"]["script"], "visual_plan": plan_id}
            full_refs = {**refs, "video_artifact_id": artifact.id}
            audio = {"version": 1, "method": "local_asr_and_pcm", "video_sha256": artifact.sha256,
                     "asr_status": "available", "segments": [{"text": "test", "start_sec": 0, "end_sec": 3}],
                     "signal_status": "available", "signal": {"decoded_pcm_sha256": "fixture"}}
            subject = acceptance_subject(session, content_id, full_refs, source_context_version=1)
            context = source_context(session, content_id, full_refs)
            backfilled = resolved_visual_plan(session, plan)
            subject["visual_plan_sha256"] = stable_hash(backfilled)
            bare = {"upstream_task_ids": refs, "video_artifact_id": artifact.id,
                    "acceptance_contract_version": 2, "acceptance_origin": "manual",
                    "source_context_version": 1, "source_context": context, "acceptance_subject": subject,
                    "script": session.get(AgentTask, refs["script"]).result_data,
                    "technical_qc": {"passed": True}, "context_files": []}
            receipt = root / "render" / "acceptance-input.json"
            receipt.parent.mkdir()
            receipt.write_text(json.dumps(bare), encoding="utf-8")
            receipt_artifact = Artifact(content_id=content_id, artifact_type="acceptance_input",
                                        relative_path="render/acceptance-input.json", sha256=sha256_file(receipt),
                                        size_bytes=receipt.stat().st_size, mime_type="application/json", producer_stage="render")
            session.add(receipt_artifact)
            (root / "render" / f"audio-evidence-{artifact.sha256}.json").write_text(json.dumps(audio), encoding="utf-8")
            frozen = {**bare, "audio_evidence_version": 1, "audio_evidence": audio,
                      "acceptance_subject": {**subject, "audio_evidence_sha256": stable_hash(audio)}}
            offer = runtime.workflow_service(session).create_agent_task(content_id, TaskType.CONTENT_QC, frozen,
                                                                        allow_multiple=True, schema_version="2")
            plan.result_data = backfilled
            session.add(plan)
            session.commit()
            yield runtime, http, content_id, plan_id, offer.id, root, original, backfilled


def test_admin_restoration_preserves_receipts_video_and_supersedes_only_pending_qc(restoration_contract, caplog):
    from glance_core.content_acceptance import verify_acceptance

    runtime, http, content_id, plan_id, offer_id, root, original, _ = restoration_contract
    before_video = (root / "final.mp4").read_bytes()
    before_receipt = (root / "render" / "acceptance-input.json").read_bytes()
    with Session(runtime.engine) as session:
        old_offer = deepcopy(session.get(AgentTask, offer_id).input_data)
        consume = session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan_id,
                                                       AuditEvent.action == AGENT_CONSUMED)).one().model_dump(mode="json")
    assert http.post(f"/api/v1/contents/{content_id}/restore-visual-plan").status_code == 401
    response = http.post(f"/api/v1/contents/{content_id}/restore-visual-plan", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert response.json()["restored"]
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, plan_id).result_data == original
        previous = session.get(AgentTask, offer_id)
        assert previous.status == TaskStatus.CANCELLED and previous.input_data == old_offer
        assert previous.input_hash == stable_hash(old_offer)
        offers = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                    AgentTask.task_type == TaskType.CONTENT_QC,
                                                    AgentTask.status == TaskStatus.PENDING)).all()
        assert len(offers) == 1 and offers[0].id == response.json()["content_qc_task_id"]
        replacement = offers[0]
        assert replacement.input_data["acceptance_subject"]["visual_plan_sha256"] == stable_hash(original)
        assert replacement.input_data["audio_evidence"] == old_offer["audio_evidence"]
        verify_acceptance(session, replacement, {"passed": False, "subject": replacement.input_data["acceptance_subject"]})
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan_id,
                                                  AuditEvent.action == AGENT_CONSUMED)).one().model_dump(mode="json") == consume
        receipt = session.get(Artifact, response.json()["receipt_artifact_id"])
        assert receipt.relative_path != "render/acceptance-input.json"
        assert json.loads((root / receipt.relative_path).read_text(encoding="utf-8")) == replacement.input_data
    assert (root / "final.mp4").read_bytes() == before_video
    assert (root / "render" / "acceptance-input.json").read_bytes() == before_receipt
    assert runtime.consume_agent(plan_id) is False
    caplog.clear()
    runtime.recover_agent_results()
    assert "Consumed Agent result changed" not in caplog.text
    assert http.post(f"/api/v1/contents/{content_id}/restore-visual-plan", headers=ADMIN).json()["restored"] is False


@pytest.mark.parametrize("tamper", ["submission", "projection", "receipt", "audio", "offer_started", "intent", "unknown_intent", "native", "new_failed_script_qc", "new_pending_script", "missing_script_qc_submission", "missing_script_consumed", "active_cover", "submitted_cover", "scheduler_offer"])
def test_restoration_fails_closed_without_touching_plan_or_pending_offer(restoration_contract, tamper):
    runtime, http, content_id, plan_id, offer_id, root, _, backfilled = restoration_contract
    with Session(runtime.engine) as session:
        if tamper == "submission":
            row = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == plan_id)).one()
            Path(row.raw_result_path).write_text("{}", encoding="utf-8")
        elif tamper == "projection":
            row = session.get(AgentTask, plan_id)
            row.result_data = {**backfilled, "unknown": True}
            session.add(row)
        elif tamper == "receipt":
            (root / "render" / "acceptance-input.json").write_text("{}", encoding="utf-8")
        elif tamper == "audio":
            path = next((root / "render").glob("audio-evidence-*.json"))
            path.write_text("{}", encoding="utf-8")
        elif tamper == "offer_started":
            row = session.get(AgentTask, offer_id)
            row.attempt = 1
            session.add(row)
        elif tamper == "unknown_intent":
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent",
                                   entity_id="unknown", action="registered", metadata_json={"status": "UNKNOWN"}))
        elif tamper in {"new_failed_script_qc", "new_pending_script", "active_cover", "submitted_cover"}:
            kind = {"new_failed_script_qc": TaskType.SCRIPT_QC, "new_pending_script": TaskType.SHORT_SCRIPT,
                    "active_cover": TaskType.ASSET_GENERATE_IMAGE, "submitted_cover": TaskType.ASSET_GENERATE_IMAGE}[tamper]
            row = runtime.workflow_service(session).create_agent_task(content_id, kind, {"asset_purpose": "cover"}, allow_multiple=True)
            row.status = {"new_failed_script_qc": TaskStatus.FAILED, "new_pending_script": TaskStatus.PENDING,
                          "active_cover": TaskStatus.LEASED, "submitted_cover": TaskStatus.SUBMITTED}[tamper]
            session.add(row)
        elif tamper in {"missing_script_qc_submission", "missing_script_consumed"}:
            kind = TaskType.SCRIPT_QC if tamper == "missing_script_qc_submission" else TaskType.SHORT_SCRIPT
            parent = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == kind)).one()
            if tamper == "missing_script_qc_submission":
                session.delete(session.exec(select(AgentSubmission).where(AgentSubmission.task_id == parent.id)).one())
            else:
                session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == parent.id, AuditEvent.action == AGENT_CONSUMED)).one())
        elif tamper == "scheduler_offer":
            session.add(SchedulerOffer(request_id="frozen-offer", task_id=offer_id,
                                       expires_at=utc_now() + timedelta(minutes=1)))
        elif tamper == "intent":
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent",
                                   entity_id="intent", action="registered", metadata_json={"content_id": content_id}))
        else:
            row = session.get(AgentTask, plan_id)
            row.assigned_agent_id = "unknown-native"
            session.add(row)
        session.commit()
        before = deepcopy(session.get(AgentTask, plan_id).result_data)
    response = http.post(f"/api/v1/contents/{content_id}/restore-visual-plan", headers=ADMIN)
    assert response.status_code == 409, response.text
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, plan_id).result_data == before
        assert session.get(AgentTask, offer_id).status == TaskStatus.PENDING
        assert not session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_visual_plan_restored")).all()


def test_restoration_rolls_back_all_db_changes_on_final_commit_failure(restoration_contract, monkeypatch):
    from app.business.runtime import _ConsumptionSession

    runtime, _, content_id, plan_id, offer_id, root, _, backfilled = restoration_contract
    real_commit = Session.commit

    def fail_commit(self):
        if isinstance(self, _ConsumptionSession):
            raise OSError("fixture commit failure")
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(OSError, match="commit failure"):
        runtime.restore_consumed_visual_plan(content_id)
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, plan_id).result_data == backfilled
        assert session.get(AgentTask, offer_id).status == TaskStatus.PENDING
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC,
                                                      AgentTask.content_id == content_id)).all()) == 1
    monkeypatch.setattr(Session, "commit", real_commit)
    assert runtime.restore_consumed_visual_plan(content_id)["restored"]
    assert (root / "final.mp4").read_bytes() == b"historical-final-video"


def test_concurrent_restoration_keeps_one_current_pending_qc(restoration_contract):
    runtime, _, content_id, _, _, _, _, _ = restoration_contract
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runtime.restore_consumed_visual_plan(content_id), range(2)))
    assert sorted(row["restored"] for row in results) == [False, True]
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC,
                                                      AgentTask.content_id == content_id,
                                                      AgentTask.status == TaskStatus.PENDING)).all()) == 1


def test_restoration_allows_other_content_intent_and_completed_cover(restoration_contract):
    runtime, _, content_id, _, offer_id, _, _, _ = restoration_contract
    with Session(runtime.engine) as session:
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent",
                               entity_id="other-intent", action="registered",
                               metadata_json={"content_id": "different-content", "status": "UNKNOWN"}))
        cover = runtime.workflow_service(session).create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE,
                                                                   {"asset_purpose": "cover"}, allow_multiple=True)
        cover.status = TaskStatus.COMPLETED
        session.add(cover)
        session.add(SchedulerOffer(request_id="expired-offer", task_id=offer_id,
                                   expires_at=utc_now() - timedelta(minutes=1)))
        session.commit()
    assert runtime.restore_consumed_visual_plan(content_id)["restored"]


@pytest.mark.parametrize("tamper", [None, "origin_generation_submission", "origin_generation_consumed", "origin_plan_owned"])
def test_owned_reuse_proves_cancelled_accepted_origin_and_current_full_qc(image_contract, tamper):
    from glance_core.content_acceptance import resolved_visual_plan, source_context

    runtime, content_id, old_plan_id, generation_id, old_qc_id, image_id, _ = image_contract
    runtime.consume_agent(old_qc_id)
    with Session(runtime.engine) as session:
        old_plan = session.get(AgentTask, old_plan_id)
        projection = resolved_visual_plan(session, old_plan)
        flow = runtime.workflow_service(session)

        def accept(kind, inputs, result):
            task = flow.create_agent_task(content_id, kind, inputs, allow_multiple=True)
            task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
            task.attempt, task.assigned_agent_id = 1, "reuse-worker"
            raw = runtime.settings.agent_submission_dir / task.id / "1" / "result.json"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps(result), encoding="utf-8")
            session.add(task)
            session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=task.assigned_agent_id,
                                       raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED"))
            for action, actor, metadata in [("completed", "agent", {"result_hash": stable_hash(result)}),
                                             (OWNED, "system", {"input_hash": task.input_hash}),
                                             (AGENT_CONSUMED, "system", {"result_hash": stable_hash(result)})]:
                session.add(AuditEvent(actor_type=actor, actor_id=task.assigned_agent_id,
                                       entity_type="agent_task", entity_id=task.id, action=action, metadata_json=metadata))
            session.commit()
            return task

        plan = accept(TaskType.VISUAL_PLAN, {**old_plan.input_data, "original_reuse": True,
                                             "allowed_asset_ids": [image_id]}, projection)
        accept(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id}}, {"passed": True})
        full = pipeline._asset_qc_input(session, flow, None, plan)
        accept(TaskType.ASSET_QC, full, {"passed": True})
        # Match normal system invalidation of an already accepted origin.
        for origin_id in [old_plan_id, generation_id]:
            origin = session.get(AgentTask, origin_id)
            origin.status = TaskStatus.CANCELLED
            session.add(origin)
            session.add(AuditEvent(actor_type="system", actor_id="workflow", entity_type="agent_task",
                                   entity_id=origin_id, action="cancelled"))
        session.commit()
        assert resolved_visual_plan(session, plan) == projection
        refs = {"script": plan.input_data["upstream_task_ids"]["script"], "visual_plan": plan.id}
        assert not source_context(session, content_id, refs)["evidence_gaps"]
        if tamper:
            if tamper == "origin_generation_submission":
                row = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation_id)).one()
            else:
                target = old_plan_id if tamper == "origin_plan_owned" else generation_id
                action = OWNED if tamper == "origin_plan_owned" else AGENT_CONSUMED
                row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == target, AuditEvent.action == action)).one()
            session.delete(row)
            session.commit()
            with pytest.raises(ValueError):
                resolved_visual_plan(session, plan)
            assert source_context(session, content_id, refs)["evidence_gaps"]


@pytest.mark.parametrize("first", ["claim", "restore"])
def test_restoration_serializes_with_real_task_claim(restoration_contract, monkeypatch, first):
    import app.business.runtime as runtime_module

    runtime, http, content_id, plan_id, offer_id, _, _, backfilled = restoration_contract
    client = registered_client(http, runtime, "script")

    def claim():
        with Session(runtime.engine) as session:
            return runtime.task_service(session).claim(
                task_id=offer_id, agent_id=client.settings.agent_id, role_id="script",
                capabilities={TaskType.CONTENT_QC}, runtime_info=client.settings.runtime_report())

    if first == "claim":
        assert claim() is not None
        with pytest.raises(ValueError, match="active or offered"):
            runtime.restore_consumed_visual_plan(content_id)
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, plan_id).result_data == backfilled
            assert session.get(AgentTask, offer_id).status == TaskStatus.LEASED
        return

    entered, release, claiming = Event(), Event(), Event()
    real_check = runtime_module._require_reconciled_content

    def gate(session, target):
        real_check(session, target)
        entered.set()
        assert release.wait(5)

    def race_claim():
        claiming.set()
        return claim()

    monkeypatch.setattr(runtime_module, "_require_reconciled_content", gate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        restoration = pool.submit(runtime.restore_consumed_visual_plan, content_id)
        assert entered.wait(5)
        claimant = pool.submit(race_claim)
        assert claiming.wait(5)
        try:
            with pytest.raises(TimeoutError):
                claimant.result(timeout=0.1)
        finally:
            release.set()
        assert restoration.result(timeout=5)["restored"]
        assert claimant.result(timeout=5) is None
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, offer_id).status == TaskStatus.CANCELLED
        assert len(session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                      AgentTask.task_type == TaskType.CONTENT_QC,
                                                      AgentTask.status == TaskStatus.PENDING)).all()) == 1


def test_reuse_qc_input_during_exact_accepted_generation_consumption(image_contract):
    from glance_core.content_acceptance import generated_image_material

    runtime, content_id, _, generation_id, _, image_id, _ = image_contract
    with Session(runtime.engine) as session:
        marker = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation_id,
                                                      AuditEvent.action == AGENT_CONSUMED)).one()
        session.delete(marker)
        session.commit()
        content = session.get(Content, content_id)
        with pytest.raises(ValueError, match="proof missing"):
            generated_image_material(session, content, image_id)
        session.info["consuming_agent_task_id"] = generation_id
        assert generated_image_material(session, content, image_id)["generation_task_id"] == generation_id
        accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation_id)).one()
        session.delete(accepted)
        session.commit()
        with pytest.raises(ValueError, match="proof missing"):
            generated_image_material(session, content, image_id)


@pytest.fixture
def manual_reuse_contract(image_contract, request):
    runtime, content_id, old_plan_id, generation_id, old_qc_id, image_id, _ = image_contract
    assert runtime.consume_agent(old_qc_id)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        origin = session.get(AgentTask, generation_id)
        content = session.get(Content, content_id)
        image_ids = [image_id]
        for index in range(1, 6):
            generation = flow.create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE,
                                                {**origin.input_data, "fixture_image": index}, allow_multiple=True)
            path = Path(content.content_dir) / f"image-{index}.png"
            path.write_bytes(f"image-fixture-{index}".encode())
            artifact = Artifact(content_id=content_id, artifact_type="agent_generated_image",
                                relative_path=path.name, sha256=sha256_file(path), size_bytes=path.stat().st_size,
                                mime_type="image/png", producer_stage=generation.id)
            session.add(artifact)
            record_accepted_completion(session, runtime, generation,
                                       {"images": [{"segment_index": 0, "artifact_id": artifact.id}]})
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=generation.id, action=AGENT_CONSUMED,
                                   metadata_json={"result_hash": stable_hash(generation.result_data)}))
            image_ids.append(artifact.id)
        old_plan = session.get(AgentTask, old_plan_id)
        segments = [
            {"asset_id": asset_id, "layout": "image_card", "narration_start_sec": index * 3,
             "narration_end_sec": (index + 1) * 3}
            for index, asset_id in enumerate(image_ids[:5])
        ]
        shape = getattr(request, "param", "all_generated")
        allowed_ids = list(image_ids)
        if shape in {"source_only", "mixed_source"}:
            path = Path(content.content_dir) / "source.png"
            path.write_bytes(b"source-image")
            source = Artifact(content_id=content_id, artifact_type="source_image", relative_path=path.name,
                              sha256=sha256_file(path), size_bytes=path.stat().st_size, mime_type="image/png",
                              producer_stage="source_image_acquire")
            session.add(source)
            allowed_ids.append(source.id)
            for segment in segments if shape == "source_only" else segments[-1:]:
                segment["asset_id"] = source.id
        elif shape == "future_generation":
            segments[-1].pop("asset_id")
            segments[-1]["asset_request"] = {"prompt": "new current plan image"}
        inputs = {**old_plan.input_data, "allowed_asset_ids": allowed_ids}
        if shape == "reuse_false":
            inputs["original_reuse"] = False
        plan = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, inputs, allow_multiple=True)
        record_accepted_completion(session, runtime, plan, {"segments": segments})
        flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "visual_plan")
        session.commit()
        plan_id = plan.id
    assert runtime.consume_agent(plan_id)
    with Session(runtime.engine) as session:
        plan_qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                      AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
                                                      AgentTask.status == TaskStatus.PENDING)).one()
        record_accepted_completion(session, runtime, plan_qc, {"passed": True})
        session.commit()
        plan_qc_id = plan_qc.id
        frozen = {task.id: (deepcopy(task.input_data), task.input_hash, deepcopy(task.result_data))
                  for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()}
    return runtime, content_id, plan_id, plan_qc_id, image_ids, frozen


@pytest.mark.parametrize("passed", [False, True])
@pytest.mark.parametrize("manual_reuse_contract", ["all_generated", "reuse_false"], indirect=True)
def test_manual_reused_images_require_fresh_full_current_qc(manual_reuse_contract, monkeypatch, passed):
    from glance_core.content_acceptance import generated_image_material, resolved_visual_plan

    runtime, content_id, plan_id, plan_qc_id, image_ids, frozen = manual_reuse_contract
    monkeypatch.setattr(pipeline, "render_content", lambda *args, **kwargs: pytest.fail("Manual QC auto-rendered"))
    assert runtime.consume_agent(plan_qc_id)
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        assert plan.input_data.get("original_reuse") is not True
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                AgentTask.task_type == TaskType.ASSET_QC,
                                                AgentTask.status == TaskStatus.PENDING)).one()
        assert qc.input_data["upstream_task_ids"] == {"visual_plan": plan_id}
        assert qc.input_data["full_generated_assets"] is True
        content = session.get(Content, content_id)
        assert (content.business_status, content.current_stage) == (ContentStatus.WAITING_AGENT, "asset_qc")
        expected = [{"segment_index": index, **generated_image_material(session, content, asset_id)}
                    for index, asset_id in enumerate(image_ids[:5])]
        assert qc.input_data["assets"] == expected
        assert image_ids[5] not in {item["artifact_id"] for item in expected}
        script = session.get(AgentTask, plan.input_data["upstream_task_ids"]["script"])
        assert qc.input_data["script"] == script.result_data
        assert qc.input_data["context_files"] == [session.get(Artifact, asset_id).relative_path
                                                   for asset_id in image_ids[:5]]
        with pytest.raises(ValueError):
            resolved_visual_plan(session, plan)
        record_accepted_completion(session, runtime, qc, {"passed": passed})
        session.commit()
        qc_id = qc.id
    assert runtime.consume_agent(plan_qc_id) is False
    assert runtime.consume_agent(qc_id)
    assert runtime.consume_agent(qc_id) is False
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        plan = session.get(AgentTask, plan_id)
        if passed:
            assert (content.business_status, content.current_stage) == (ContentStatus.PRODUCING, "render_ready")
            assert resolved_visual_plan(session, plan) == frozen[plan_id][2]
        else:
            assert content.current_stage != "render_ready"
            with pytest.raises(ValueError, match="Full current asset QC"):
                resolved_visual_plan(session, plan)
        assert len(session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                       AgentTask.task_type == TaskType.ASSET_QC,
                                                       AgentTask.input_data["upstream_task_ids"]["visual_plan"].as_string() == plan_id)).all()) == 1
        for task_id, snapshot in frozen.items():
            task = session.get(AgentTask, task_id)
            assert (task.input_data, task.input_hash, task.result_data) == snapshot


@pytest.mark.parametrize("manual_reuse_contract", ["source_only", "mixed_source"], indirect=True)
def test_reuse_factory_preserves_source_only_and_rejects_mixed_coverage(manual_reuse_contract):
    runtime, content_id, plan_id, plan_qc_id, _, frozen = manual_reuse_contract
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        flow = runtime.workflow_service(session)
        if all(session.get(Artifact, segment["asset_id"]).artifact_type == "source_image"
               for segment in plan.result_data["segments"]):
            assert pipeline._asset_qc_input(session, flow, None, plan) is None
        else:
            # Core full coverage includes source asset IDs, which the generated-only factory cannot approve.
            with pytest.raises(ValueError, match="Current plan image generation is incomplete"):
                pipeline._asset_qc_input(session, flow, None, plan)
            with pytest.raises(ValueError, match="Current plan image generation is incomplete"):
                runtime.consume_agent(plan_qc_id)
            return
    assert runtime.consume_agent(plan_qc_id)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        assert (content.business_status, content.current_stage) == (ContentStatus.PRODUCING, "render_ready")
        assert not session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.ASSET_QC,
                                                      AgentTask.status == TaskStatus.PENDING)).all()
        assert session.get(AgentTask, plan_id).result_data == frozen[plan_id][2]


@pytest.mark.parametrize("tamper", ["missing", "foreign", "bytes", "generation_input", "plan_input", "undeclared"])
def test_manual_reuse_factory_rejects_unproven_images(manual_reuse_contract, tamper):
    runtime, content_id, plan_id, _, image_ids, _ = manual_reuse_contract
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        artifact = session.get(Artifact, image_ids[0])
        if tamper == "missing":
            for asset_id in image_ids[:5]:
                session.delete(session.get(Artifact, asset_id))
        elif tamper == "foreign":
            candidate = Candidate(source_type="agent", source_id="foreign", source_url="https://example.org/foreign",
                                  original_title="Other")
            session.add(candidate)
            session.flush()
            other = Content(candidate_id=candidate.id, title="Other",
                            content_dir=str(runtime.settings.content_dir / "foreign"))
            session.add(other)
            session.flush()
            artifact.content_id = other.id
            session.add(artifact)
        elif tamper == "bytes":
            (Path(session.get(Content, content_id).content_dir) / artifact.relative_path).write_bytes(b"tampered")
        elif tamper == "generation_input":
            generation = session.get(AgentTask, artifact.producer_stage)
            generation.input_data = {**generation.input_data, "tampered": True}
            session.add(generation)
        elif tamper == "plan_input":
            plan.input_data = {**plan.input_data, "tampered": True}
            session.add(plan)
        else:
            # An accepted but undeclared selection must not gain a reuse grant.
            result = deepcopy(plan.result_data)
            result["segments"][0]["asset_id"] = image_ids[5]
            replacement = runtime.workflow_service(session).create_agent_task(
                content_id, TaskType.VISUAL_PLAN, {**plan.input_data, "allowed_asset_ids": image_ids[:5]},
                allow_multiple=True)
            record_accepted_completion(session, runtime, replacement, result)
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=replacement.id, action=AGENT_CONSUMED,
                                   metadata_json={"result_hash": stable_hash(result)}))
            plan = replacement
        session.commit()
        with pytest.raises(ValueError):
            pipeline._asset_qc_input(session, runtime.workflow_service(session), None, plan)


@pytest.mark.parametrize("manual_reuse_contract", ["future_generation"], indirect=True)
def test_reuse_waits_for_current_generation_and_full_qc_covers_both(manual_reuse_contract):
    from glance_core.content_acceptance import generated_image_material, resolved_visual_plan

    runtime, content_id, plan_id, plan_qc_id, image_ids, frozen = manual_reuse_contract
    with Session(runtime.engine) as session:
        with pytest.raises(ValueError, match="Current plan image generation is incomplete"):
            pipeline._asset_qc_input(session, runtime.workflow_service(session), None,
                                     session.get(AgentTask, plan_id))
    assert runtime.consume_agent(plan_qc_id)
    with Session(runtime.engine) as session:
        generation = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                         AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                                                         AgentTask.status == TaskStatus.PENDING)).one()
        assert generation.input_data["upstream_task_ids"] == {"visual_plan": plan_id}
        assert generation.input_data["requests"] == [{"segment_index": 4,
                                                     "asset_request": {"prompt": "new current plan image"}}]
        content = session.get(Content, content_id)
        path = Path(content.content_dir) / "current-generation.png"
        path.write_bytes(b"new-current-image")
        artifact = Artifact(content_id=content_id, artifact_type="agent_generated_image", relative_path=path.name,
                            sha256=sha256_file(path), size_bytes=path.stat().st_size,
                            mime_type="image/png", producer_stage=generation.id)
        session.add(artifact)
        record_accepted_completion(session, runtime, generation,
                                   {"images": [{"segment_index": 4, "artifact_id": artifact.id}]})
        session.commit()
        generation_id, artifact_id = generation.id, artifact.id
    assert runtime.consume_agent(generation_id)
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                AgentTask.task_type == TaskType.ASSET_QC,
                                                AgentTask.status == TaskStatus.PENDING)).one()
        assert qc.input_data["upstream_task_ids"] == {"visual_plan": plan_id,
                                                   "asset_generate_image": generation_id}
        assert qc.input_data["full_generated_assets"] is True
        expected_ids = image_ids[:4] + [artifact_id]
        content = session.get(Content, content_id)
        assert qc.input_data["assets"] == [{"segment_index": index,
                                           **generated_image_material(session, content, asset_id)}
                                          for index, asset_id in enumerate(expected_ids)]
        record_accepted_completion(session, runtime, qc, {"passed": True})
        session.commit()
        qc_id = qc.id
    assert runtime.consume_agent(qc_id)
    with Session(runtime.engine) as session:
        plan = session.get(AgentTask, plan_id)
        assert [segment["asset_id"] for segment in resolved_visual_plan(session, plan)["segments"]] == expected_ids
        assert (plan.input_data, plan.input_hash, plan.result_data) == frozen[plan_id]


def test_current_generation_without_reuse_keeps_generation_scoped_qc(image_contract):
    runtime, _, plan_id, generation_id, qc_id, _, _ = image_contract
    with Session(runtime.engine) as session:
        inputs = pipeline._asset_qc_input(session, runtime.workflow_service(session),
                                          session.get(AgentTask, generation_id), session.get(AgentTask, plan_id))
        assert inputs == session.get(AgentTask, qc_id).input_data
        assert "full_generated_assets" not in inputs


def test_reuse_factory_rejects_old_generation_as_current(manual_reuse_contract):
    runtime, _, plan_id, _, image_ids, _ = manual_reuse_contract
    with Session(runtime.engine) as session:
        generation = session.get(AgentTask, session.get(Artifact, image_ids[0]).producer_stage)
        with pytest.raises(ValueError, match="Generated image is not from current plan/task"):
            pipeline._asset_qc_input(session, runtime.workflow_service(session), generation,
                                     session.get(AgentTask, plan_id))


def test_portable_unowned_reuse_keeps_legacy_no_qc_route(manual_reuse_contract):
    runtime, _, plan_id, plan_qc_id, _, _ = manual_reuse_contract
    with Session(runtime.engine) as session:
        for event in session.exec(select(AuditEvent).where(AuditEvent.entity_id.in_([plan_id, plan_qc_id]),
                                                         AuditEvent.action.in_([OWNED, AGENT_CONSUMED]))).all():
            session.delete(event)
        session.commit()
        assert pipeline._asset_qc_input(session, runtime.workflow_service(session), None,
                                        session.get(AgentTask, plan_id)) is None
