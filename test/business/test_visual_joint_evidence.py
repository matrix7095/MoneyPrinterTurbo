"""A complete evidence pack is a binding, never a semantic QC decision."""

import hashlib
import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from fastapi.testclient import TestClient
from glance_core.content_acceptance import JOINT_VISUAL_ACTIVE_SECONDS, acceptance_subject, joint_visual_plan_supported, resolved_visual_plan, source_context, verify_acceptance, verify_visual_joint_evidence
from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS, result_model_for
from glance_core.models import AgentTask, Artifact, AuditEvent, Candidate, CandidateStatus, Content, ContentStatus, ProductionMode, RightsStatus, TaskStatus, TaskType
from glance_media.frame_evidence import generate_frame_evidence
from sqlmodel import Session

from app.business import pipeline
from test.business.test_content_http import client_for, content_app
from test.business.test_discovery_http import make_runtime
from test.business.test_frame_evidence import synthetic_clip as synthetic_clip
from test.business.test_content_qc_recovery import failed_qc as failed_qc, accept, recovery_rows
from test.business.test_visual_plan_materialization import image_contract as image_contract


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def joint_pack(tmp_path):
    pack = tmp_path / "render" / "joint"
    pack.mkdir(parents=True)
    frames = []
    for index in range(8):
        path = pack / f"frame-{index}.png"
        Image.new("RGB", (32, 64), (index * 20, 60, 120)).save(path)
        frames.append({"index": index, "pts_seconds": str(index / 4),
                       "display_duration_sec": .25, "display_end_sec": (index + 1) / 4,
                       "path": path.name, "sha256": digest(path)})
    pages = []
    for index in range(2):
        path = pack / f"page-{index}.png"
        Image.new("RGB", (64, 128), (index * 40, 60, 120)).save(path)
        pages.append({"page_index": index, "frame_indices": list(range(index * 4, index * 4 + 4)),
                      "path": path.name, "sha256": digest(path)})
    plan = {"segments": [
        {"asset_id": "first", "layout": "image_card", "narration_start_sec": 0,
         "narration_end_sec": 1, "motion": "pan_right", "transition": "short_fade"},
        {"asset_id": "second", "layout": "image_card", "narration_start_sec": 1,
         "narration_end_sec": 2, "motion": "ken_burns_in", "transition": "short_fade"},
    ]}
    subtitle = pack / "narration.ass"
    subtitle.write_text("frozen rendered subtitle bytes", encoding="utf-8")
    manifest = {"version": 2, "method": "full_decoded_frame_sequence",
                "input_video": {key: "a" * 64 for key in
                                ("expected_sha256", "sha256_before_processing", "sha256_after_processing")},
                "review": {"observed_by_reviewer": False, "audio_review_performed": False},
                "coverage": {"frame_count": 8, "mapped_frame_count": 8, "mapping_complete": True},
                "frames": frames, "pages": pages, "video_end_sec": 2,
                "visual_plan": plan,
                "subtitles": {"path": subtitle.name, "sha256": digest(subtitle), "events": [
                    {"index": 0, "start_sec": 0, "end_sec": 1, "text": "产品"},
                    {"index": 1, "start_sec": 1, "end_sec": 2, "text": "作用"}]}}
    manifest_path = pack / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    evidence = {"manifest_path": "render/joint/manifest.json", "manifest_sha256": digest(manifest_path),
                "manifest": manifest}
    review = {"method": "native_contact_pages_asr_pcm", "video_sha256": "a" * 64,
              "manifest_sha256": evidence["manifest_sha256"], "realtime_motion_observed": False,
              "realtime_motion_required": False,
              "limitations": "No real-time playback or native listening; pages and independent ASR/PCM only.",
              "pages": [{"page_index": p["page_index"], "sha256": p["sha256"],
                         "frame_indices": p["frame_indices"], "observed": True,
                         "observation": "实际查看该页四帧产品、字幕和逐步移动。"} for p in pages],
              "segments": [{"segment_index": i, "frame_index": i * 4, "pts_sec": i,
                            "motion_continuous": True,
                            "observation": "该段产品、旁白、字幕一致，位移逐帧连续。"} for i in range(2)],
              "boundaries": [{"boundary_index": 0, "left_frame_index": 3, "right_frame_index": 4,
                              "observation": "0.75至1.00秒切换对应产品，没有错误主体。"}],
              "subtitle_alignment": [{"event_index": i, "word_indices": [i], "observed_sync": True,
                                      "observation": "烧录字幕与独立ASR词时码匹配。"} for i in range(2)]}
    audio = {"video_sha256": "a" * 64, "method": "local_asr_and_pcm", "asr_status": "available",
             "signal_status": "available", "signal": {"duration_sec": 2, "sample_rate_hz": 16000,
                 "channels": 1, "decoded_pcm_sha256": "c" * 64, "window_sec": .1,
                 "rms_dbfs": [-20] * 20, "peak_amplitude": .5, "full_scale_sample_fraction": 0}, "segments": [
                 {"start_sec": i, "end_sec": i + 1, "text": text,
                  "words": [{"word": text, "start_sec": i, "end_sec": i + 1, "probability": .99}]}
                 for i, text in enumerate(("产品", "作用"))]}
    return tmp_path, evidence, review, audio, plan


def verify(pack, *, passed=True, audio_review_policy_version=1):
    root, evidence, review, audio, plan = pack
    verify_visual_joint_evidence(root, evidence, review, audio, plan, passed=passed,
                                 audio_review_policy_version=audio_review_policy_version)


def test_low_probability_is_a_review_locator_only_for_prospective_policy(joint_pack):
    joint_pack[3]["segments"][0]["words"][0]["probability"] = .69
    joint_pack[2]["subtitle_alignment"][0]["observation"] = (
        "词索引0，原0–1秒，产品身份词；独立ASR及同成片词区间PCM与画面来源对应。"
        "此为合成契约测试，不声称实际听回；没有未消歧的数字、单位或关系。"
    )
    with pytest.raises(ValueError, match="SUBTITLE_ASR_MISMATCH"):
        verify(joint_pack)
    verify(joint_pack, audio_review_policy_version=2)


@pytest.mark.parametrize("policy", [True, False, "2", 2.0, None, 0, 3])
@pytest.mark.parametrize("passed", [True, False])
def test_unknown_audio_policy_is_never_accepted(joint_pack, policy, passed):
    with pytest.raises(ValueError, match="AUDIO_REVIEW_POLICY_VERSION_UNSUPPORTED"):
        verify(joint_pack, passed=passed, audio_review_policy_version=policy)


@pytest.mark.parametrize("probability", [None, True, "0.9", -0.1, 1.1, float("nan"), float("inf")])
def test_prospective_policy_preserves_raw_probability_validation(joint_pack, probability):
    joint_pack[3]["segments"][0]["words"][0]["probability"] = probability
    with pytest.raises(ValueError, match="ASR_PROBABILITY_INVALID"):
        verify(joint_pack, audio_review_policy_version=2)


@pytest.mark.parametrize("fault", ["time", "silence", "missing_word", "no_observation"])
def test_prospective_low_confidence_review_does_not_waive_physical_gates(joint_pack, fault):
    word = joint_pack[3]["segments"][0]["words"][0]
    word["probability"] = .69
    if fault == "time":
        word.update(start_sec=.8, end_sec=1.8)
    elif fault == "silence":
        joint_pack[3]["signal"]["rms_dbfs"][:10] = [-100] * 10
    elif fault == "missing_word":
        joint_pack[3]["segments"][1]["words"].append(
            {"word": "未覆盖", "start_sec": 1.1, "end_sec": 1.9, "probability": .9}
        )
    else:
        joint_pack[2]["subtitle_alignment"][0]["observation"] = ""
    with pytest.raises(ValueError):
        verify(joint_pack, audio_review_policy_version=2)


def test_silent_midpoint_does_not_mean_the_whole_word_is_silent(joint_pack):
    joint_pack[3]["signal"]["rms_dbfs"][4:7] = [-100] * 3
    verify(joint_pack, audio_review_policy_version=2)


def test_complete_observed_pack_can_support_but_does_not_decide_qc(joint_pack):
    verify(joint_pack)
    assert joint_pack[1]["manifest"]["review"]["observed_by_reviewer"] is False


@pytest.mark.parametrize("fault", ["missing_page", "page_sha", "foreign_video", "nonmonotonic_pts",
                                   "wrong_end", "subtitle_shift", "unobserved", "requires_realtime",
                                   "missing_boundary", "missing_segment", "missing_subtitle"])
def test_joint_proof_rejects_incomplete_or_mismatched_observations(joint_pack, fault):
    root, evidence, review, audio, _ = joint_pack
    if fault == "missing_page":
        review["pages"].pop()
    elif fault == "page_sha":
        (root / "render/joint/page-0.png").write_bytes(b"replaced")
    elif fault == "foreign_video":
        audio["video_sha256"] = "b" * 64
    elif fault in {"nonmonotonic_pts", "wrong_end"}:
        if fault == "nonmonotonic_pts":
            evidence["manifest"]["frames"][2]["pts_seconds"] = "0.1"
        else:
            evidence["manifest"]["frames"][-1]["display_end_sec"] = 2.2
        path = root / evidence["manifest_path"]
        path.write_text(json.dumps(evidence["manifest"]), encoding="utf-8")
        evidence["manifest_sha256"] = review["manifest_sha256"] = digest(path)
    elif fault == "subtitle_shift":
        audio["segments"][0]["words"][0].update(start_sec=.8, end_sec=1.8)
    elif fault == "unobserved":
        review["pages"][0]["observed"] = False
    elif fault == "requires_realtime":
        review["realtime_motion_required"] = True
    else:
        review[{"missing_boundary": "boundaries", "missing_segment": "segments",
                "missing_subtitle": "subtitle_alignment"}[fault]].pop()
    with pytest.raises(ValueError):
        verify(joint_pack)


def test_exact_repeated_moving_frames_cannot_pass(joint_pack):
    root, evidence, review, _, _ = joint_pack
    frames = evidence["manifest"]["frames"]
    for frame in frames[1:4]:
        path = root / "render/joint" / frame["path"]
        path.write_bytes((root / "render/joint" / frames[0]["path"]).read_bytes())
        frame["sha256"] = digest(path)
    path = root / evidence["manifest_path"]
    path.write_text(json.dumps(evidence["manifest"]), encoding="utf-8")
    evidence["manifest_sha256"] = review["manifest_sha256"] = digest(path)
    with pytest.raises(ValueError, match="REPEATED"):
        verify(joint_pack)


@pytest.mark.parametrize("check", CONTENT_ACCEPTANCE_CHECKS)
def test_v3_keeps_every_acceptance_hard_gate(joint_pack, check):
    result = {"passed": True, "issues": [], "contract_version": 2, "confidence": "high",
              "subject": {"video_sha256": "a" * 64}, "visual_review": deepcopy(joint_pack[2]),
              "checks": {key: {"passed": key != check, "evidence": "实际证据"}
                         for key in CONTENT_ACCEPTANCE_CHECKS}}
    with pytest.raises(ValueError, match="every hard check"):
        result_model_for(TaskType.CONTENT_QC, "3").model_validate(result)


def test_v2_schema_does_not_accept_joint_review(joint_pack):
    schema = result_model_for(TaskType.CONTENT_QC, "2").model_json_schema()
    assert "visual_review" not in schema["properties"]
    old = {"passed": False, "issues": [], "contract_version": 2, "confidence": "high",
           "subject": {"video_sha256": "a" * 64},
           "checks": {key: {"passed": False, "evidence": "Original v2 proof"}
                      for key in CONTENT_ACCEPTANCE_CHECKS}}
    assert result_model_for(TaskType.CONTENT_QC, "2").model_validate(old).model_dump(mode="json") == old


def test_real_decoding_freezes_each_display_interval_and_last_end(synthetic_clip, tmp_path):
    path = generate_frame_evidence(synthetic_clip, digest(synthetic_clip), tmp_path / "timed", timed=True)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert manifest["version"] == 2
    assert len(manifest["frames"]) == 7
    assert manifest["video_end_sec"] == pytest.approx(1, abs=.001)
    for frame in manifest["frames"]:
        assert frame["display_duration_sec"] == pytest.approx(1 / 7, abs=.001)
        assert frame["display_end_sec"] > float(frame["pts_seconds"])
    assert manifest["frames"][-1]["display_end_sec"] == manifest["video_end_sec"]


def test_timed_generation_rejects_nonmonotonic_decoded_pts(synthetic_clip, tmp_path, monkeypatch):
    from glance_media import frame_evidence

    original = frame_evidence._read_pts

    def bad_pts(*args):
        values = original(*args)
        values[2] = values[1]
        return values

    monkeypatch.setattr(frame_evidence, "_read_pts", bad_pts)
    with pytest.raises(frame_evidence.FrameEvidenceError, match="PTS"):
        generate_frame_evidence(synthetic_clip, digest(synthetic_clip), tmp_path / "bad-pts", timed=True)
    assert not (tmp_path / "bad-pts/manifest.json").exists()


def seed_joint(runtime, joint_pack):
    """Synthetic business contract data; no assertion of real native review or publication."""
    _, evidence, review, audio, plan_data = deepcopy(joint_pack)
    with Session(runtime.engine) as session:
        candidate = Candidate(source_type="agent", source_id="joint-test", source_url="https://example.org/product",
                              original_title="Product", rights_status=RightsStatus.SELF_OWNED, status=CandidateStatus.SELECTED)
        session.add(candidate)
        session.flush()
        content = Content(candidate_id=candidate.id, title="Product", production_mode=ProductionMode.IMAGE_ONLY,
                          content_dir="", business_status=ContentStatus.PRODUCING, current_stage="render")
        content.content_dir = str(runtime.settings.content_dir / content.id)
        root = Path(content.content_dir)
        shutil.copytree(joint_pack[0] / "render" / "joint", root / "render" / "joint")
        session.add(content)
        session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content", entity_id=content.id,
                               action="manual_candidate_selected"))
        flow = pipeline.workflow(session, runtime.settings)

        def completed(kind, refs, result):
            task = flow.create_agent_task(content.id, kind, {"upstream_task_ids": refs})
            task.status, task.result_data = TaskStatus.COMPLETED, result
            session.add(task)
            session.commit()
            return task

        research = completed(TaskType.CONTENT_RESEARCH, {}, {"verified_claims": []})
        script = completed(TaskType.SHORT_SCRIPT, {"research": research.id}, {"full_narration": "产品作用"})
        completed(TaskType.SCRIPT_QC, {"script": script.id, "research": research.id}, {"passed": True})
        plan = completed(TaskType.VISUAL_PLAN, {"script": script.id}, plan_data)
        completed(TaskType.VISUAL_PLAN_QC, {"visual_plan": plan.id, "script": script.id}, {"passed": True})
        for segment in plan_data["segments"]:
            source = root / "render" / "joint" / f"frame-{0 if segment['asset_id'] == 'first' else 4}.png"
            session.add(Artifact(id=segment["asset_id"], content_id=content.id, artifact_type="source_image",
                                 relative_path=source.relative_to(root).as_posix(), sha256=digest(source),
                                 size_bytes=source.stat().st_size, mime_type="image/png", producer_stage="test"))
        final = root / "render" / "final.mp4"
        final.write_bytes(b"synthetic business binding; not production media")
        artifact = Artifact(content_id=content.id, artifact_type="final_video", relative_path="render/final.mp4",
                            sha256=digest(final), size_bytes=final.stat().st_size, mime_type="video/mp4", producer_stage="render")
        session.add(artifact)
        session.commit()
        for key in evidence["manifest"]["input_video"]:
            evidence["manifest"]["input_video"][key] = artifact.sha256
        audio["video_sha256"] = review["video_sha256"] = artifact.sha256
        manifest_path = root / evidence["manifest_path"]
        manifest_path.write_text(json.dumps(evidence["manifest"]), encoding="utf-8")
        evidence["manifest_sha256"] = review["manifest_sha256"] = digest(manifest_path)
        refs = {"script": script.id, "visual_plan": plan.id, "video_artifact_id": artifact.id}
        review_input = {"script": script.result_data, "research": research.result_data,
                        "upstream_task_ids": {"script": script.id, "visual_plan": plan.id},
                        "video_artifact_id": artifact.id, "acceptance_contract_version": 2,
                        "acceptance_origin": "manual", "source_context_version": 1,
                        "source_context": source_context(session, content.id, refs),
                        "acceptance_subject": acceptance_subject(session, content.id, refs, source_context_version=1),
                        "context_files": [], "technical_qc": {"passed": True}}
        assert not review_input["source_context"]["evidence_gaps"]
        return content.id, evidence, review, audio, review_input


def test_new_v3_issuance_http_pages_heartbeat_submission_and_publish_binding(tmp_path, joint_pack, monkeypatch):
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path / "http")
    with TestClient(content_app(runtime)) as http:
        content_id, evidence, review, audio, review_input = seed_joint(runtime, joint_pack)
        audio["segments"][0]["words"][0]["probability"] = .69
        review["subtitle_alignment"][0]["observation"] = (
            "索引0，原0–1秒产品词，低置信定位已核对独立ASR与同final PCM；"
            "合成契约证据，不宣称原生听回，不包含未消歧的关键关系。"
        )
        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", lambda *args, **kwargs: deepcopy(audio))
        monkeypatch.setattr(pipeline, "_collect_visual_evidence", lambda *args: deepcopy(evidence))
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            pipeline._issue_acceptance(session, runtime.settings, content, review_input)
        client = client_for(http, "script")
        claimed = client.claim()
        assert claimed["status"] == "CLAIMED"
        bundle = client.bundle(claimed["bundle_url"], claimed["lease_token"])
        assert bundle["task"]["schema_version"] == "3"
        assert bundle["input"]["audio_review_policy_version"] == 2
        assert "受限 image_card 联合证据验收" in bundle["instructions"]
        pages = [row for row in bundle["context_manifest"] if row["name"].startswith("page-")]
        assert len(pages) == 2
        for page in pages:
            url = bundle["context_files"][page["index"]]["url"]
            response = http.get(url, headers={"Authorization": f"Bearer {claimed['lease_token']}"})
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == page["sha256"]
        raw = next(row for row in bundle["context_manifest"] if row["name"] == "frame-0.png")
        original = http.get(bundle["context_files"][raw["index"]]["url"],
                            headers={"Authorization": f"Bearer {claimed['lease_token']}"})
        assert original.status_code == 200 and hashlib.sha256(original.content).hexdigest() == raw["sha256"]
        assert http.post(f"/api/v1/agent/tasks/{claimed['task_id']}/heartbeat",
                         headers={"Authorization": f"Bearer {claimed['lease_token']}"}, json={}).status_code == 200
        result = {"passed": True, "issues": [], "contract_version": 2, "confidence": "high",
                  "subject": bundle["input"]["acceptance_subject"], "visual_review": review,
                  "checks": {key: {"passed": True, "evidence": "Synthetic contract observation"}
                             for key in CONTENT_ACCEPTANCE_CHECKS}}
        submitted = client.complete(claimed["task_id"], claimed["lease_token"], "script-worker", result)
        assert submitted["status"] == "COMPLETED"
        assert client.complete(claimed["task_id"], claimed["lease_token"], "script-worker", result) == submitted
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claimed["task_id"])
            assert task.schema_version == "3"
            assert task.result_data == result  # No new result fields or normalization defaults.
            verify_acceptance(session, task, task.result_data)
            frozen_input = task.input_data
            task.input_data = {**frozen_input, "audio_review_policy_version": 1}
            with pytest.raises(ValueError, match="snapshot changed"):
                verify_acceptance(session, task, task.result_data)
            task.input_data = frozen_input
            path = pipeline.verify_publish_ready(session, runtime.settings, content_id)
            path.write_bytes(b"changed final")
            with pytest.raises(ValueError, match="video file|video hash"):
                pipeline.verify_publish_ready(session, runtime.settings, content_id)


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize("original_policy", [None, 1, 2, True, "2", 3])
def test_reissued_qc_inherits_first_policy_regardless_of_prior_status(
    tmp_path, joint_pack, monkeypatch, status, original_policy
):
    from app.business.media_executor import MptMediaExecutor
    from glance_core.task_service import stable_hash
    from sqlmodel import select

    runtime = make_runtime(tmp_path / "history")
    with TestClient(content_app(runtime)):
        content_id, evidence, _, audio, review_input = seed_joint(runtime, joint_pack)
        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", lambda *args, **kwargs: deepcopy(audio))
        monkeypatch.setattr(pipeline, "_collect_visual_evidence", lambda *args: deepcopy(evidence))
        with Session(runtime.engine) as session:
            inputs = deepcopy(review_input)
            if original_policy is not None:
                inputs["audio_review_policy_version"] = original_policy
            old = pipeline.workflow(session, runtime.settings).create_agent_task(content_id, TaskType.CONTENT_QC, inputs)
            old.status = status
            session.add(old)
            session.commit()
            session.refresh(old)
            snapshot = old.model_dump(mode="json")
            # A caller's requested policy can never override the first frozen QC.
            if original_policy is not None and (type(original_policy) is not int or original_policy not in {1, 2}):
                with pytest.raises(ValueError, match="AUDIO_REVIEW_POLICY_VERSION_UNSUPPORTED"):
                    pipeline._issue_acceptance(session, runtime.settings, session.get(Content, content_id), review_input)
                assert session.get(AgentTask, old.id).model_dump(mode="json") == snapshot
                return
            pipeline._issue_acceptance(session, runtime.settings, session.get(Content, content_id),
                                       {**review_input, "audio_review_policy_version": 2})
            issued = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                  AgentTask.task_type == TaskType.CONTENT_QC, AgentTask.id != old.id)).one()
            assert issued.input_data["audio_review_policy_version"] == (original_policy or 1)
            assert stable_hash(issued.input_data) == issued.input_hash
            receipt = session.get(Artifact, issued.input_data["acceptance_receipt_artifact_id"])
            path = Path(session.get(Content, content_id).content_dir) / receipt.relative_path
            assert json.loads(path.read_text(encoding="utf-8")) == issued.input_data
            assert session.get(AgentTask, old.id).model_dump(mode="json") == snapshot


@pytest.mark.parametrize("policy", ["absent", 1, 2, True, "2", None, 3])
def test_recovery_evidence_projects_only_verified_explicit_policy(tmp_path, joint_pack, monkeypatch, policy):
    from app.business.media_executor import MptMediaExecutor
    from glance_core.task_service import stable_hash
    from sqlmodel import select

    runtime = make_runtime(tmp_path / "projection")
    with TestClient(content_app(runtime)):
        content_id, evidence, _, audio, review_input = seed_joint(runtime, joint_pack)
        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", lambda *args, **kwargs: deepcopy(audio))
        monkeypatch.setattr(pipeline, "_collect_visual_evidence", lambda *args: deepcopy(evidence))
        with Session(runtime.engine) as session:
            pipeline._issue_acceptance(session, runtime.settings, session.get(Content, content_id), review_input)
            task = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                AgentTask.task_type == TaskType.CONTENT_QC)).one()
            inputs = deepcopy(task.input_data)
            inputs.pop("audio_review_policy_version")
            if policy != "absent":
                inputs["audio_review_policy_version"] = policy
            task.input_data, task.input_hash = inputs, stable_hash(inputs)
            projected = pipeline.workflow(session, runtime.settings)._recovery_current_input(task)
            binding = projected["acceptance_evidence"]["visual_evidence_binding"]
            if type(policy) is int and policy in {1, 2}:
                assert binding["audio_review_policy_version"] == policy
            else:
                assert "audio_review_policy_version" not in binding
                if policy != "absent":
                    assert any("audio_review_policy_version" in gap for gap in projected["evidence_gaps"])
            # Even a valid-looking marker is unavailable when its frozen hash changed.
            task.input_hash = "bad"
            unverified = pipeline.workflow(session, runtime.settings)._recovery_current_input(task)
            assert "acceptance_evidence" not in unverified


@pytest.mark.parametrize("policy", [None, 2, True, 3])
def test_manual_stalled_qc_rebuild_keeps_source_policy(tmp_path, joint_pack, monkeypatch, policy):
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path / "manual-rebuild")
    with TestClient(content_app(runtime)):
        content_id, evidence, _, audio, bare = seed_joint(runtime, joint_pack)
        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", lambda *args, **kwargs: deepcopy(audio))
        monkeypatch.setattr(pipeline, "_collect_visual_evidence", lambda *args: deepcopy(evidence))
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            root = Path(content.content_dir)
            pack = (root / evidence["manifest_path"]).parent
            frozen = {**bare, "audio_evidence_version": 1, "audio_evidence": audio,
                      "visual_evidence_version": 1, "visual_evidence": evidence,
                      "context_files": [evidence["manifest_path"], *[
                          (pack / item["path"]).relative_to(root).as_posix()
                          for item in [*evidence["manifest"]["frames"], *evidence["manifest"]["pages"],
                                       evidence["manifest"]["subtitles"]]]]}
            if policy is not None:
                frozen["audio_review_policy_version"] = policy
            refs = {**bare["upstream_task_ids"], "video_artifact_id": bare["video_artifact_id"]}
            frozen["acceptance_subject"] = acceptance_subject(session, content_id, refs, source_context_version=1,
                                                             audio_evidence=audio, visual_evidence=evidence)
            pipeline._write_acceptance_receipt(session, content, pipeline.MediaExecutor(root), frozen)
            source = pipeline.workflow(session, runtime.settings).create_agent_task(
                content_id, TaskType.CONTENT_QC, frozen, schema_version="3")
            source.status = TaskStatus.FAILED
            content.business_status, content.current_stage = ContentStatus.FAILED, "content_qc_failed"
            session.add(source)
            session.add(content)
            session.commit()
            session.refresh(source)
            snapshot = source.model_dump(mode="json")
        if type(policy) is bool or policy == 3:
            with pytest.raises(ValueError, match="AUDIO_REVIEW_POLICY_VERSION_UNSUPPORTED"):
                pipeline.repair_stalled_content_qc(runtime.engine, runtime.settings, content_id)
        else:
            _, child = pipeline.repair_stalled_content_qc(runtime.engine, runtime.settings, content_id)
            assert child.input_data["audio_review_policy_version"] == (policy or 1)
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source.id).model_dump(mode="json") == snapshot


@pytest.mark.parametrize("fault", ["missing_page", "foreign_video", "missing_subtitle", "hard_check"])
def test_http_completion_cannot_bypass_v3_evidence(tmp_path, joint_pack, monkeypatch, fault):
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path / "http")
    with TestClient(content_app(runtime)) as http:
        content_id, evidence, review, audio, review_input = seed_joint(runtime, joint_pack)
        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", lambda *args, **kwargs: deepcopy(audio))
        monkeypatch.setattr(pipeline, "_collect_visual_evidence", lambda *args: deepcopy(evidence))
        with Session(runtime.engine) as session:
            pipeline._issue_acceptance(session, runtime.settings, session.get(Content, content_id), review_input)
        worker = client_for(http, "script")
        claimed = worker.claim()
        bundle = worker.bundle(claimed["bundle_url"], claimed["lease_token"])
        result = {"passed": True, "issues": [], "contract_version": 2, "confidence": "high",
                  "subject": bundle["input"]["acceptance_subject"], "visual_review": review,
                  "checks": {key: {"passed": True, "evidence": "Synthetic contract observation"}
                             for key in CONTENT_ACCEPTANCE_CHECKS}}
        if fault == "missing_page":
            result["visual_review"]["pages"].pop()
        elif fault == "foreign_video":
            result["visual_review"]["video_sha256"] = "b" * 64
        elif fault == "missing_subtitle":
            result["visual_review"]["subtitle_alignment"].pop()
        else:
            result["checks"]["facts"]["passed"] = False
        response = http.post(f"/api/v1/agent/tasks/{claimed['task_id']}/complete",
                             headers={"Authorization": f"Bearer {claimed['lease_token']}"},
                             json={"agent_id": "script-worker", "result": result})
        assert response.status_code == 422, response.text
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, claimed["task_id"]).status != TaskStatus.COMPLETED
            assert session.get(Content, content_id).business_status != ContentStatus.FINAL_REVIEW


def test_new_v3_failure_uses_existing_native_recovery_and_preserves_old_v2(failed_qc, joint_pack):
    runtime, _, content_id, old_qc_id, plan_id, root = failed_qc
    with Session(runtime.engine) as session:
        old = session.get(AgentTask, old_qc_id)
        frozen_old = old.model_dump(mode="json")
        evidence, review = deepcopy(joint_pack[1:3])
        pack_root = root / "render" / "joint"
        shutil.copytree(joint_pack[0] / "render" / "joint", pack_root)
        plan = resolved_visual_plan(session, session.get(AgentTask, plan_id))
        assert len(plan["segments"]) == 1
        end = plan["segments"][-1]["narration_end_sec"]
        evidence["manifest"].update(visual_plan=plan, video_end_sec=end)
        for frame in evidence["manifest"]["frames"]:
            frame.update(pts_seconds=str(frame["index"] * end / 8), display_duration_sec=end / 8,
                         display_end_sec=(frame["index"] + 1) * end / 8)
        evidence["manifest"]["subtitles"]["events"] = [{"index": 0, "start_sec": 0, "end_sec": end, "text": "产品"}]
        sha = old.input_data["acceptance_subject"]["video_sha256"]
        for key in evidence["manifest"]["input_video"]:
            evidence["manifest"]["input_video"][key] = sha
        path = pack_root / "manifest.json"
        path.write_text(json.dumps(evidence["manifest"]), encoding="utf-8")
        evidence["manifest_sha256"] = review["manifest_sha256"] = digest(path)
        review.update(video_sha256=sha, pages=[], segments=[], boundaries=[], subtitle_alignment=[])
        frozen = deepcopy(old.input_data)
        frozen.update(visual_evidence_version=1, visual_evidence=evidence,
                      context_files=[evidence["manifest_path"], *[
                          (pack_root / page["path"]).relative_to(root).as_posix() for page in evidence["manifest"]["pages"]]])
        refs = {**frozen["upstream_task_ids"], "video_artifact_id": frozen["video_artifact_id"]}
        frozen["acceptance_subject"] = acceptance_subject(session, content_id, refs, source_context_version=1,
                                                         audio_evidence=frozen["audio_evidence"], visual_evidence=evidence)
        content = session.get(Content, content_id)
        pipeline._write_acceptance_receipt(session, content, pipeline.MediaExecutor(root), frozen)
        task = runtime.workflow_service(session).create_agent_task(content_id, TaskType.CONTENT_QC, frozen,
                                                                  allow_multiple=True, schema_version="3")
        result = deepcopy(old.result_data)
        result.update(subject=frozen["acceptance_subject"], visual_review=review)
        accept(session, runtime, task, result, old.assigned_agent_id)
        new_id = task.id
    assert runtime.consume_agent(new_id) is True
    recovery = recovery_rows(runtime)[0]
    assert recovery.input_data["failure"]["source_task_id"] == new_id
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, old_qc_id).model_dump(mode="json") == frozen_old
        assert session.get(AgentTask, new_id).result_data["passed"] is False
        assert session.get(Content, content_id).business_status != ContentStatus.FINAL_REVIEW


def test_old_task_cannot_adopt_new_method_and_pages(tmp_path, joint_pack):
    runtime = make_runtime(tmp_path / "http")
    with TestClient(content_app(runtime)):
        content_id, evidence, _, audio, bare = seed_joint(runtime, joint_pack)
        with Session(runtime.engine) as session:
            old = pipeline.workflow(session, runtime.settings).create_agent_task(
                content_id, TaskType.CONTENT_QC, {**bare, "audio_evidence_version": 1, "audio_evidence": audio,
                                                "visual_evidence_version": 1, "visual_evidence": evidence},
                schema_version="2")
            with pytest.raises(ValueError, match="LEGACY_QC_CANNOT_USE"):
                verify_acceptance(session, old, {"passed": False, "subject": bare["acceptance_subject"]})


@pytest.mark.parametrize(("native_sampling", "fault"), [
    (False, None), (True, None), (True, "missing_sidecar"), (True, "missing_sidecar30"),
    (True, "wrong_version"), (True, "wrong_revision"), (True, "wrong_actual_fps"),
    (True, "wrong_spec"), (True, "wrong_digest"), (True, "wrong_video_crf"), (True, "wrong_spec_crf"),
    (True, "wrong_budget"), (True, "wrong_spec_budget"),
])
def test_real_visual_collection_creates_new_full_packs_without_claiming_observation(
        tmp_path, joint_pack, synthetic_clip, native_sampling, fault):
    import pysubs2
    from glance_media.executor import _with_transition, layout_filter

    runtime = make_runtime(tmp_path / "http")
    with TestClient(content_app(runtime)):
        content_id, _, _, audio, bare = seed_joint(runtime, joint_pack)
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            root = Path(content.content_dir)
            artifact = session.get(Artifact, bare["video_artifact_id"])
            final = root / artifact.relative_path
            fps = 60 if native_sampling and fault != "missing_sidecar30" else 30
            if native_sampling:
                final = root / "render/revisions/new-60/final.mp4"
                final.parent.mkdir(parents=True)
                artifact.relative_path = final.relative_to(root).as_posix()
            # Evidence plumbing only; genuine intermediate motion is tested in the media regressions.
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(synthetic_clip), "-vf", f"fps={fps}",
                            "-c:v", "libx264", str(final)], check=True, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            artifact.sha256, artifact.size_bytes = digest(final), final.stat().st_size
            if native_sampling:
                parameters = {"renderer_version": "local-ffmpeg-v5-global-vbv",
                              "revision_id": final.parent.name, "fps": fps,
                              "spatial_sampling": 2 if fps == 60 else 1, "video_crf": 10 if fps == 60 else 23,
                              "video_maxrate_kbps": 20000 if fps == 60 else 0,
                              "video_bufsize_kbits": 40000 if fps == 60 else 0}
                if fault == "wrong_version":
                    parameters["renderer_version"] = "BOGUS"
                if fault == "wrong_revision":
                    parameters["revision_id"] = "different"
                if fault == "wrong_actual_fps":
                    parameters.update(fps=30, spatial_sampling=1)
                if fault == "wrong_video_crf":
                    parameters["video_crf"] = 18
                if fault == "wrong_budget":
                    parameters.update(video_maxrate_kbps=18000, video_bufsize_kbits=36000)
                sidecar = final.with_name("render-parameters.json")
                sidecar.write_text(json.dumps(parameters), encoding="utf-8")
                output_hashes = {"final.mp4": artifact.sha256, sidecar.name: digest(sidecar)}
                if fault == "wrong_digest":
                    output_hashes[sidecar.name] = "c" * 64
                final.with_name("outputs.json").write_text(json.dumps(output_hashes), encoding="utf-8")
                spec = root / "render/mpt-project/revisions" / final.parent.name / "spec.json"
                spec.parent.mkdir(parents=True)
                spec.write_text(json.dumps({"revision_id": final.parent.name,
                    "settings": {"fps": 30 if fault in {"wrong_spec", "wrong_actual_fps"} else fps,
                                 "video_crf": 18 if fault == "wrong_spec_crf" else parameters["video_crf"],
                                 "video_maxrate_kbps": 18000 if fault == "wrong_spec_budget" else parameters["video_maxrate_kbps"],
                                 "video_bufsize_kbits": parameters["video_bufsize_kbits"]}}), encoding="utf-8")
                if fault in {"missing_sidecar", "missing_sidecar30"}:
                    sidecar.unlink()
            plan = session.get(AgentTask, bare["upstream_task_ids"]["visual_plan"])
            plan.result_data = deepcopy(plan.result_data)
            for index, segment in enumerate(plan.result_data["segments"]):
                segment.update(narration_start_sec=index * .5, narration_end_sec=(index + 1) * .5)
            session.add(plan)
            session.add(artifact)
            session.commit()
            subs = pysubs2.SSAFile()
            subs.events = [pysubs2.SSAEvent(start=0, end=500, text="产品"),
                           pysubs2.SSAEvent(start=500, end=1000, text="作用")]
            (root / "transcript").mkdir()
            subs.save(str(root / "transcript" / "narration.ass"))
            refs = {**bare["upstream_task_ids"], "video_artifact_id": artifact.id}
            if fault:
                with pytest.raises(ValueError, match="VISUAL_EVIDENCE_RENDER_PARAMETERS_INVALID"):
                    pipeline._collect_visual_evidence(session, content, refs)
                assert not list((root / "render").glob("visual-evidence-*"))
                return
            first = pipeline._collect_visual_evidence(session, content, refs)
            frozen = (root / first["manifest_path"]).read_bytes()
            # A later project's mutable current receipt must not change the registered revision's template.
            (root / "render" / "mpt-render-request.json").write_text(
                json.dumps({"request": {"settings": {"fps": 30}}}), encoding="utf-8")
            second = pipeline._collect_visual_evidence(session, content, refs)
            assert first["manifest_path"] != second["manifest_path"]
            assert (root / first["manifest_path"]).read_bytes() == frozen
            assert len(first["manifest"]["frames"]) == (60 if native_sampling else 30)
            assert len(first["manifest"]["pages"]) == (15 if native_sampling else 8)
            assert first["manifest"]["subtitles"]["events"][1]["start_sec"] == .5
            assert len(first["manifest"]["render_templates"]) == 2
            expected = [_with_transition(layout_filter(
                "image_card", motion=segment["motion"], fps=60 if native_sampling else 30,
                spatial_sampling=2 if native_sampling else 1), segment["transition"])
                for segment in resolved_visual_plan(session, plan)["segments"]]
            assert first["manifest"]["render_templates"] == second["manifest"]["render_templates"] == expected
            if native_sampling:
                assert first["manifest"]["render_parameters"]["receipt_sha256"] == digest(sidecar)
                assert first["manifest"]["render_parameters"]["revision_id"] == final.parent.name
                assert first["manifest"]["render_parameters"]["video_crf"] == 10
                assert first["manifest"]["render_parameters"]["video_maxrate_kbps"] == 20000
                assert first["manifest"]["render_parameters"]["video_bufsize_kbits"] == 40000
            else:
                assert first["manifest"]["render_parameters"]["source"] == "legacy_30fps_video"
            assert first["manifest"]["review"]["observed_by_reviewer"] is False
            audio["video_sha256"] = artifact.sha256
            verify_visual_joint_evidence(root, first, None, audio, resolved_visual_plan(session, plan), passed=False)


@pytest.fixture(scope="module", params=["pan_left", "pan_right", "focus_product", "ken_burns_in", "ken_burns_out"])
def real_capped_motion(request, tmp_path_factory):
    """Real visual render/decode only; synthetic audio/review stand-ins do not assert semantic QC."""
    from glance_media.executor import _with_transition, layout_filter

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg/ffprobe required for real cap regression")
    motion = request.param
    root = tmp_path_factory.mktemp(f"motion-cap-{motion}")
    image = Image.new("RGB", (1080, 1920), (245, 245, 235))
    draw = ImageDraw.Draw(image)
    for x in range(0, 1080, 90):
        draw.line((x, 0, x, 1919), fill=(150, 165, 170), width=3)
    for y in range(0, 1920, 120):
        draw.line((0, y, 1079, y), fill=(150, 165, 170), width=3)
    draw.rectangle((220, 450, 850, 1480), fill=(40, 110, 180), outline="black", width=12)
    draw.ellipse((350, 700, 700, 1050), fill=(230, 180, 30), outline="white", width=8)
    asset = root / "source.png"
    image.save(asset)
    frames_count = 270 if JOINT_VISUAL_ACTIVE_SECONDS[motion] == 8 else 300
    duration = frames_count / 30
    template = _with_transition(layout_filter("image_card", motion=motion), "hard_cut")
    normal_video = root / "normal.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(asset),
                    "-vf", template, "-frames:v", str(frames_count), "-threads", "1", "-c:v", "libx264",
                    "-preset", "ultrafast", "-qp", "0", "-pix_fmt", "yuv420p", str(normal_video)],
                   check=True, capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    early_video = root / "early.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(normal_video), "-vf",
                    "trim=end_frame=30,setpts=PTS-STARTPTS,tpad=stop_mode=clone:stop_duration=12",
                    "-frames:v", str(frames_count), "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast",
                    "-qp", "0", "-pix_fmt", "yuv420p", str(early_video)],
                   check=True, capture_output=True, timeout=120,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    plan = {"segments": [{"asset_id": "motion-source", "layout": "image_card", "motion": motion,
                           "transition": "hard_cut", "narration_start_sec": 0, "narration_end_sec": duration}]}
    packs = {}
    for name, video in (("normal", normal_video), ("early", early_video)):
        manifest_path = generate_frame_evidence(video, digest(video), root / name, timed=True)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        subtitle = manifest_path.parent / "narration.ass"
        subtitle.write_text("Synthetic cap validation subtitle binding", encoding="utf-8")
        manifest.update(visual_plan=plan, render_templates=[template], subtitles={"path": subtitle.name,
                        "sha256": digest(subtitle), "events": [{"index": 0, "start_sec": 0,
                                                                "end_sec": duration, "text": "产品"}]})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        evidence = {"manifest_path": manifest_path.relative_to(root).as_posix(), "manifest_sha256": digest(manifest_path),
                    "manifest": manifest}
        review = {"method": "native_contact_pages_asr_pcm", "video_sha256": digest(video),
                  "manifest_sha256": evidence["manifest_sha256"], "realtime_motion_observed": False,
                  "realtime_motion_required": False, "limitations": "Synthetic validator test; no native semantic acceptance.",
                  "pages": [{"page_index": page["page_index"], "sha256": page["sha256"],
                             "frame_indices": page["frame_indices"], "observed": True,
                             "observation": "Contract observation stand-in only."} for page in manifest["pages"]],
                  "segments": [{"segment_index": 0, "frame_index": 0, "pts_sec": 0, "motion_continuous": True,
                                "observation": "Expected cap then hold; this tests active-window binding only."}],
                  "boundaries": [], "subtitle_alignment": [{"event_index": 0, "word_indices": [0], "observed_sync": True,
                                                              "observation": "Synthetic alignment stand-in only."}]}
        audio = {"video_sha256": digest(video), "method": "local_asr_and_pcm", "asr_status": "available",
                 "signal_status": "available", "signal": {"duration_sec": duration, "sample_rate_hz": 16000,
                     "channels": 1, "decoded_pcm_sha256": "c" * 64, "window_sec": .1,
                     "rms_dbfs": [-20] * round(duration * 10), "peak_amplitude": .5, "full_scale_sample_fraction": 0},
                 "segments": [{"start_sec": 0, "end_sec": duration, "words": [
                     {"word": "产品", "start_sec": 0, "end_sec": duration, "probability": .99}]}]}
        packs[name] = (root, evidence, review, audio, plan)
    return motion, packs


def test_real_renderer_cap_hold_is_expected_and_early_freeze_is_rejected(real_capped_motion):
    motion, packs = real_capped_motion
    assert joint_visual_plan_supported(packs["normal"][-1])
    frames = packs["normal"][1]["manifest"]["frames"]
    assert frames[-1]["sha256"] == frames[-10]["sha256"]
    # Chroma-aligned crop steps can repeat several frames while movement continues.
    assert len({frame["sha256"] for frame in frames[:30]}) > 3
    assert float(frames[-10]["pts_seconds"]) > JOINT_VISUAL_ACTIVE_SECONDS[motion]
    verify(packs["normal"])
    with pytest.raises(ValueError, match="REPEATED_MOVING_FRAMES"):
        verify(packs["early"])
    assert packs["normal"][1]["manifest"]["review"]["observed_by_reviewer"] is False


def test_expected_cap_hold_never_waives_native_pacing_hard_gate(joint_pack):
    result = {"passed": True, "issues": [], "contract_version": 2, "confidence": "high",
              "subject": {"video_sha256": "a" * 64}, "visual_review": deepcopy(joint_pack[2]),
              "checks": {key: {"passed": key != "pacing", "evidence": "Expected hold still has unacceptable pacing"}
                         for key in CONTENT_ACCEPTANCE_CHECKS}}
    with pytest.raises(ValueError, match="every hard check"):
        result_model_for(TaskType.CONTENT_QC, "3").model_validate(result)
