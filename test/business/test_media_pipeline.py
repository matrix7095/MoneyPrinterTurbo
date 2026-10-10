"""Real MPT render smoke plus migrated approval/replay contracts, using isolated data."""

import hashlib
import json
import shutil
import wave
from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import numpy as np
import pytest
from glance_core.content_acceptance import acceptance_origin, acceptance_subject, is_handsfree_content, verify_acceptance
from glance_core.database import build_engine, create_db
from glance_core.models import (
    AgentTask, Artifact, AuditEvent, Candidate, CandidateStatus, Content,
    ContentStatus, RightsStatus, TaskStatus, TaskType, utc_now,
)
from glance_core.settings import Settings
from glance_core.task_service import stable_hash
from glance_media.executor import MediaExecutor, NonRetryableMediaError, probe, run_command
from PIL import Image
from sqlmodel import Session, select

from app.business.media_executor import MptMediaExecutor
from app.business.pipeline import (
    _queue_post_package_tasks, _require_current_qc, advance_after_agent, approve_script,
    verify_publish_ready, workflow,
)
from app.services.video_project import VideoProject


@pytest.fixture
def media_contract(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data",
                        database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
                        handsfree_enabled=False)
    engine = build_engine(settings)
    create_db(engine)
    with Session(engine) as session:
        candidate = Candidate(source_type="agent", source_id="media-contract",
                              source_url="https://example.com/product", original_title="Product",
                              status=CandidateStatus.SELECTED, rights_status=RightsStatus.SELF_OWNED)
        session.add(candidate)
        session.commit()
        root = settings.content_dir / "media-contract"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="Product", content_dir=str(root),
                          business_status=ContentStatus.SCRIPT_REVIEW, current_stage="script_review")
        session.add(content)
        session.commit()
        flow = workflow(session, settings)

        def completed(kind, refs, result, **kwargs):
            task = flow.create_agent_task(content.id, kind, {"upstream_task_ids": refs}, **kwargs)
            task.status = TaskStatus.COMPLETED
            task.result_data = result
            task.completed_at = utc_now()
            session.add(task)
            session.commit()
            return task

        research = completed(TaskType.CONTENT_RESEARCH, {}, {"verified_claims": [
            {"id": "c1", "claim": "Product fact", "source_urls": [candidate.source_url]}]})
        script = completed(TaskType.SHORT_SCRIPT, {"research": research.id}, {"full_narration": "产品测试。"})
        script_qc = completed(TaskType.SCRIPT_QC, {"script": script.id, "research": research.id}, {"passed": True})
        plan = completed(TaskType.VISUAL_PLAN, {"script": script.id}, {"segments": [{"scene_id": "s1"}]})
        completed(TaskType.VISUAL_PLAN_QC, {"visual_plan": plan.id, "script": script.id}, {"passed": True})
        path = root / "final.mp4"
        path.write_bytes(b"contract-file-is-not-a-production-video")
        artifact = Artifact(content_id=content.id, artifact_type="final_video", relative_path="final.mp4",
                            sha256=hashlib.sha256(path.read_bytes()).hexdigest(), size_bytes=path.stat().st_size,
                            mime_type="video/mp4", producer_stage="render")
        session.add(artifact)
        session.commit()
        yield engine, session, settings, content, candidate, research, script, script_qc, plan, artifact, completed
    engine.dispose()


@pytest.mark.parametrize("failure", ["false", "lineage", "hash", "not_latest"])
def test_script_approval_rejects_failed_or_stale_qc_before_voice(media_contract, monkeypatch, failure):
    engine, session, settings, content, _, research, script, qc, _, _, _ = media_contract
    if failure == "false":
        qc.result_data = {"passed": False}
    elif failure == "lineage":
        qc.input_data = {"upstream_task_ids": {"script": "agt_old", "research": research.id}}
        qc.input_hash = stable_hash(qc.input_data)
    elif failure == "hash":
        qc.input_hash = "changed"
    else:
        fresh = AgentTask(task_type=TaskType.SCRIPT_QC, content_id=content.id, prompt_name="qc",
                          instructions="qc", input_hash="new", created_at=utc_now() + timedelta(seconds=1))
        session.add(fresh)
    session.add(qc)
    session.commit()

    def never_voice(*args, **kwargs):
        pytest.fail("Rejected QC must not reach paid/network TTS")

    monkeypatch.setattr(MptMediaExecutor, "synthesize_voice", never_voice)
    with pytest.raises(NonRetryableMediaError):
        _require_current_qc(session, content.id, TaskType.SCRIPT_QC, {"script": script, "research": research})
    with pytest.raises(NonRetryableMediaError):
        approve_script(engine, settings, content.id)


def test_publish_rejects_legacy_qc_even_for_manual_selection(media_contract):
    _, session, settings, content, _, _, script, _, plan, _, completed = media_contract
    completed(TaskType.CONTENT_QC, {"script": script.id, "visual_plan": plan.id}, {"passed": True})
    with pytest.raises(NonRetryableMediaError, match="schema-v2"):
        verify_publish_ready(session, settings, content.id)


def test_manual_acceptance_uses_explicit_authority_without_handsfree_marker(media_contract):
    _, session, _, content, _, _, script, _, plan, artifact, _ = media_contract
    refs = {"script": script.id, "visual_plan": plan.id, "video_artifact_id": artifact.id}
    with pytest.raises(ValueError, match="explicit production ownership"):
        acceptance_subject(session, content.id, refs)
    session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                           entity_id=content.id, action="manual_candidate_selected"))
    session.commit()
    assert acceptance_origin(session, content.id) == "manual"
    assert not is_handsfree_content(session, content.id)
    subject = acceptance_subject(session, content.id, refs)
    assert subject["video_sha256"] == artifact.sha256
    (Path(content.content_dir) / artifact.relative_path).write_bytes(b"replacement")
    with pytest.raises(ValueError, match="file missing/invalid|hash changed"):
        acceptance_subject(session, content.id, refs)


def test_manual_v2_acceptance_preserves_origin_and_exact_subject(media_contract):
    _, session, _, content, _, _, script, _, plan, artifact, completed = media_contract
    session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                           entity_id=content.id, action="manual_candidate_selected"))
    session.commit()
    refs = {"script": script.id, "visual_plan": plan.id, "video_artifact_id": artifact.id}
    subject = acceptance_subject(session, content.id, refs)
    task = completed(TaskType.CONTENT_QC, {"script": script.id, "visual_plan": plan.id},
                     {"passed": False, "subject": subject}, schema_version="2")
    task.input_data = {**task.input_data, "video_artifact_id": artifact.id, "acceptance_subject": subject,
                       "acceptance_contract_version": 2, "acceptance_origin": "manual"}
    task.input_hash = stable_hash(task.input_data)
    session.add(task)
    session.commit()
    # A rejected decision still has to bind its actual manual subject. No simulated pass is needed.
    verify_acceptance(session, task, task.result_data)
    task.input_data = {**task.input_data, "acceptance_origin": "handsfree"}
    task.input_hash = stable_hash(task.input_data)
    with pytest.raises(ValueError, match="origin"):
        verify_acceptance(session, task, task.result_data)


def test_packaged_publish_metadata_completion_still_schedules_review(media_contract, monkeypatch):
    engine, session, settings, content, _, _, _, _, _, _, completed = media_contract
    task = completed(TaskType.PUBLISH_PREPARE, {}, {})
    content.business_status = ContentStatus.PACKAGED
    content.current_stage = "package"
    session.add(content)
    session.commit()
    calls = []
    monkeypatch.setattr(type(workflow(session, settings)), "schedule_manual_assist_tasks",
                        lambda self, **kwargs: calls.append(kwargs))
    assert advance_after_agent(engine, settings, task.id) is None
    assert calls == [{"publish_policy": None, "settings": settings}]


@pytest.mark.parametrize("old_status", [TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.COMPLETED])
def test_new_script_qc_creates_current_review_without_reusing_old_revision(media_contract, old_status):
    engine, session, settings, content, _, research, script, qc, _, _, _ = media_contract
    flow = workflow(session, settings)
    old = flow.create_agent_task(content.id, TaskType.SCRIPT_REVIEW_ASSIST, {
        "script": {"full_narration": "上一版脚本。"}, "script_qc": {"passed": True},
        "research": research.result_data,
        "upstream_task_ids": {"script": "old-script", "script_qc": "old-qc", "research": research.id},
    })
    old.status = old_status
    old.result_data = {"verdict": "APPROVE"} if old_status == TaskStatus.COMPLETED else None
    session.add(old)
    content.current_stage = "script_qc"
    session.add(content)
    session.commit()
    session.refresh(old)
    original = deepcopy(old.model_dump())

    assert advance_after_agent(engine, settings, qc.id) is None
    session.expire_all()
    reviews = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.SCRIPT_REVIEW_ASSIST,
    )).all()
    assert len(reviews) == 2
    current = next(task for task in reviews if task.id != old.id)
    assert current.status == TaskStatus.PENDING
    assert current.input_data["script"] == script.result_data
    assert current.input_data["upstream_task_ids"] == {
        "script": script.id, "script_qc": qc.id, "research": research.id,
    }
    assert session.get(AgentTask, old.id).model_dump() == original
    # Same accepted QC replay and the existing repair scan both preserve one current review.
    assert advance_after_agent(engine, settings, qc.id) is None
    scanned = workflow(session, settings).schedule_manual_assist_tasks(settings=settings)
    assert all(task.id == current.id for task in scanned)
    assert len(session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.SCRIPT_REVIEW_ASSIST,
    )).all()) == 2


@pytest.mark.parametrize("prepare_enabled", [True, False])
@pytest.mark.parametrize("old_status", [TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.COMPLETED])
def test_new_package_creates_current_metadata_or_review_and_deduplicates_exact_input(
    media_contract, prepare_enabled, old_status,
):
    _, session, settings, content, _, research, script, _, _, _, _ = media_contract
    settings = settings.model_copy(update={"publish_prepare_enabled": prepare_enabled, "archive_enabled": False})
    flow = workflow(session, settings)
    kind = TaskType.PUBLISH_PREPARE if prepare_enabled else TaskType.PUBLISH_REVIEW_ASSIST
    old = flow.create_agent_task(content.id, kind, {"script": {"full_narration": "上一版脚本。"}})
    old.status = old_status
    old.result_data = {"title": "上一版标题"} if old_status == TaskStatus.COMPLETED else None
    session.add(old)
    session.commit()
    session.refresh(old)
    original = deepcopy(old.model_dump())

    for _ in range(2):
        _queue_post_package_tasks(session, flow, content, settings, script.result_data, research.result_data)
    tasks = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == kind,
    )).all()
    assert len(tasks) == 2
    current = next(task for task in tasks if task.id != old.id)
    assert current.status == TaskStatus.PENDING
    assert current.input_data["script"] == script.result_data
    assert current.input_data["research"] == research.result_data
    assert session.get(AgentTask, old.id).model_dump() == original


@pytest.mark.parametrize("stage,status", [("visual_plan", ContentStatus.WAITING_AGENT),
                                          ("content_qc", ContentStatus.WAITING_AGENT),
                                          ("cover_generate", ContentStatus.WAITING_AGENT),
                                          ("packaged", ContentStatus.PACKAGED)])
def test_replaying_old_script_qc_does_not_reset_later_media_stage(media_contract, stage, status):
    engine, session, settings, content, _, _, _, qc, _, _, _ = media_contract
    content.current_stage, content.business_status = stage, status
    session.add(content)
    session.commit()
    before = len(session.exec(select(AgentTask)).all())
    assert advance_after_agent(engine, settings, qc.id) is None
    session.refresh(content)
    assert content.current_stage == stage and content.business_status == status
    assert len(session.exec(select(AgentTask)).all()) == before


def test_handsfree_script_qc_waits_for_review_and_manual_does_not_auto_approve(media_contract):
    engine, session, settings, content, _, _, _, qc, _, _, _ = media_contract
    assert advance_after_agent(engine, settings, qc.id) is None
    session.add(AuditEvent(actor_type="system", actor_id="handsfree-coordinator", entity_type="content",
                           entity_id=content.id, action="handsfree_candidate_selected"))
    session.commit()
    enabled = settings.model_copy(update={"handsfree_enabled": True, "handsfree_auto_review_enabled": True})
    assert advance_after_agent(engine, enabled, qc.id) is None
    content.current_stage = "visual_plan"
    content.business_status = ContentStatus.WAITING_AGENT
    session.add(content)
    session.commit()
    assert advance_after_agent(engine, enabled, qc.id) is None


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
@pytest.mark.parametrize("native_boundaries", [True, False])
def test_real_mpt_render_preserves_approved_images_audio_and_revision(tmp_path, monkeypatch, native_boundaries):
    root = tmp_path / "render-contract"
    executor = MptMediaExecutor(root)
    voice = root / "audio" / "voice-frozen-fixture" / "voice.wav"
    voice.parent.mkdir(parents=True)
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=root)
    voice.with_name("word_boundaries.json").write_text(json.dumps([
        {"text": "产品", "start_sec": 0, "end_sec": 0.6},
        {"text": "测试。", "start_sec": 0.6, "end_sec": 1.2},
    ] if native_boundaries else []), encoding="utf-8")
    evidence = executor.visual_timing_evidence(voice, 1.2)
    # A changed legacy alias must not contaminate an approved immutable pair or its subtitles.
    (root / "audio" / "narration.mp3").write_bytes(b"wrong alias")
    (root / "audio" / "word_boundaries.json").write_text("[]", encoding="utf-8")
    images = {}
    segments = []
    for index, color in enumerate(["red", "blue"]):
        image = root / f"image-{index}.png"
        Image.new("RGB", (800, 800), color).save(image)
        images[f"image-{index}"] = image
        segments.append({"asset_id": f"image-{index}", "narration_start_sec": index * 0.6,
                         "narration_end_sec": (index + 1) * 0.6, "layout": "image_card",
                         "purpose": "Approved concept image", "motion": "hold"})

    def forbidden_legacy_render(*args, **kwargs):
        pytest.fail("MPT adapter must use the actual MPT renderer")

    monkeypatch.setattr(MediaExecutor, "render", forbidden_legacy_render)
    final, preview = executor.render(video_path=root / "unused.mp4", voice_path=voice,
                                     narration="产品测试。", plan_data={"segments": segments}, scenes=[],
                                     assets=images, generated_image_ids=set(images),
                                     visual_timing_evidence=evidence)
    assert final.is_file() and preview.is_file()
    metadata = probe(final)
    assert abs(float(metadata["format"]["duration"]) - 1.2) < 0.15
    video = next(stream for stream in metadata["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert any(stream["codec_type"] == "audio" for stream in metadata["streams"])
    project = VideoProject(root / "render" / "mpt-project")
    assert project.status()["state"] == "succeeded"
    assert len(project.revision()["scenes"]) == 2
    receipt = json.loads((root / "render" / "mpt-render-request.json").read_text(encoding="utf-8"))
    assert len(receipt["prepared_sha256"]) == 2
    assert "AI概念示意" in (root / "transcript" / "narration.ass").read_text(encoding="utf-8-sig")

    # Decode the actual outputs, rather than accepting only container metadata.
    for output in (final, preview):
        run_command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], cwd=root)
    for instant, channel in [(0.25, 0), (0.85, 2)]:
        frame = root / f"frame-{instant}.png"
        run_command(["ffmpeg", "-y", "-ss", str(instant), "-i", str(final),
                     "-frames:v", "1", str(frame)], cwd=root)
        with Image.open(frame) as decoded:
            pixel = decoded.convert("RGB").getpixel((540, 960))
        assert pixel[channel] > 150 and all(pixel[channel] > value * 2 for index, value in enumerate(pixel) if index != channel)
    decoded_audio = root / "decoded-final.wav"
    run_command(["ffmpeg", "-y", "-i", str(final), "-map", "0:a:0", "-ar", "48000", "-ac", "1",
                 "-c:a", "pcm_s16le", str(decoded_audio)], cwd=root)
    with wave.open(str(decoded_audio), "rb") as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(float) / 32768
        rate = audio.getframerate()
    assert np.sqrt(np.mean(samples ** 2)) > 0.03
    frequencies = np.fft.rfftfreq(len(samples), 1 / rate)
    assert abs(frequencies[np.argmax(abs(np.fft.rfft(samples))) ] - 440) < 3
    revision_id = project.revision()["revision_id"]
    executor.render(video_path=root / "unused.mp4", voice_path=voice, narration="产品测试。",
                    plan_data={"segments": segments}, scenes=[], assets=images, generated_image_ids=set(images),
                    visual_timing_evidence=evidence)
    assert project.revision()["revision_id"] == revision_id
    assert all(stage["reused"] for stage in project.status()["stages"].values())
