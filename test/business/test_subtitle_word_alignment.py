"""Strict voice ASR timing is separate from frozen visual TTS cut timing."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pysubs2
import pytest

from glance_media import executor as portable
from glance_media.executor import MediaExecutor, NonRetryableMediaError
from test.business import test_visual_plan_materialization as materialization

image_contract = materialization.image_contract


def segments(words):
    return [{"start_sec": words[0][1], "end_sec": words[-1][2],
             "text": "".join(word[0] for word in words),
             "words": [{"text": text, "start_sec": start, "end_sec": end} for text, start, end in words]}]


@pytest.fixture
def aligner(tmp_path, monkeypatch):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"frozen synthetic voice")
    executor = MediaExecutor(tmp_path)
    state = {"segments": segments([("产品", 0.1, 0.4), ("测试", 0.4, 0.8)]), "note": "", "duration": 1.2}
    calls = []

    def recognize(model, device, compute, *, video_path):
        assert video_path == voice
        calls.append((model, device, compute))
        return deepcopy(state["segments"]), state["note"]

    monkeypatch.setattr(executor, "_local_asr", recognize)
    monkeypatch.setattr(portable, "probe", lambda _: {"format": {"duration": state["duration"]}})

    def align(narration="产品测试。"):
        return executor.align_subtitles(voice, narration, asr_model="fixture", asr_device="cpu", asr_compute_type="int8")

    return executor, voice, state, calls, align


def caption_events(executor, narration, alignment, duration):
    path = executor._build_subtitles(narration, duration, subtitle_alignment=alignment)
    return [event for event in pysubs2.load(str(path)) if event.style == "Default"]


def test_asr_word_is_never_split_across_timed_events(aligner):
    executor, _, state, _, align = aligner
    narration = "变化就在这里：共享的不只是一个工作空间。"
    tokens = ["变化", "就在", "这里", "共享", "的", "不", "只是", "一个", "工作", "空间"]
    state.update(duration=20, segments=segments([(text, 15 + i * 0.3, 15.3 + i * 0.3) for i, text in enumerate(tokens)]))
    alignment = align(narration)
    events = caption_events(executor, narration, alignment, 20)
    assert "".join(event.text.replace(r"\N", "") for event in events) == narration
    assert any("一个" in event.text.replace(r"\N", "") for event in events)
    assert not any(event.text.endswith("一") for event in events)
    assert all(left.end <= right.start for left, right in zip(events, events[1:]))


def test_last_caption_ends_at_last_asr_word_not_voice_tail(aligner):
    executor, _, state, _, align = aligner
    narration = "最想借它专注完成哪件事？"
    state.update(duration=26.85, segments=segments([("最想借它专注", 24, 25), ("完成哪件事", 25, 26.24)]))
    events = caption_events(executor, narration, align(narration), 26.85)
    assert events[-1].end == 26240


def test_small_cross_segment_overlap_is_clamped_only_for_events(aligner):
    executor, _, state, _, align = aligner
    state.update(duration=5, segments=[
        segments([("吗", 3.2, 3.48)])[0], segments([("这", 3.4, 3.7)])[0]])
    alignment = align("吗？这。")
    assert alignment["words"][1]["start_sec"] == 3.4
    events = caption_events(executor, "吗？这。", alignment, 5)
    assert [(event.start, event.end) for event in events] == [(3200, 3480), (3480, 3700)]


@pytest.mark.parametrize(("narration", "observed"), [
    ("2.5平方米。", "25平方米"), ("2.5平方米。", "二点五平方米"),
    ("借用。", "界用"), ("Product A。", "product A"), ("产品测试。", "产品"),
])
def test_text_mismatch_is_rejected_without_normalization(aligner, narration, observed):
    _, _, state, _, align = aligner
    state["segments"] = segments([(observed, 0.1, 0.8)])
    with pytest.raises(NonRetryableMediaError, match="match"):
        align(narration)


def test_only_punctuation_and_whitespace_are_ignored_and_original_text_is_preserved(aligner):
    _, _, state, _, align = aligner
    state["segments"] = segments([("2.5", 0.1, 0.4), ("平方米", 0.4, 0.8)])
    narration = " 2.5 平方米！ "
    alignment = align(narration)
    assert "".join(word["text"] for word in alignment["words"]) == narration


def test_decimal_token_spanning_asr_words_never_becomes_two_timed_captions(aligner):
    executor, _, state, _, align = aligner
    narration = "它的占地面积为2.5平方米。"
    state["segments"] = segments([("它的占地面积为", .0, .3), ("2.", .3, .5), ("5", .6, .7), ("平方米", .7, .9)])
    alignment = align(narration)
    events = caption_events(executor, narration, alignment, 1.2)
    assert any("2.5" in event.text.replace(r"\N", "") for event in events)
    assert alignment["words"][1]["text"] == "2."
    assert alignment["words"][2]["text"] == "5"


def test_aligned_caption_wrap_keeps_decimal_unit_together(aligner):
    executor, _, state, _, align = aligner
    narration = "这款单人工作舱，每个占地2.5平方米。"
    state["segments"] = segments([(narration, 0.1, 0.9)])

    events = caption_events(executor, narration, align(narration), 1.2)

    lines = [line for event in events for line in event.text.split(r"\N")]
    assert "".join(lines) == narration
    assert any("2.5平方米" in line for line in lines)
    assert all(len(line) <= portable._SUBTITLE_LINE_CHARS for line in lines)


@pytest.mark.parametrize(
    "measurement",
    ["2.5平方米", "2.5平方米面积", "2.5 平方米", "68厘米", "118厘米", "Model-X100"],
)
def test_aligned_caption_keeps_measurement_and_model_tokens_together(aligner, measurement):
    executor, _, state, _, align = aligner
    narration = "甲" * 11 + measurement + "后"
    state["segments"] = segments([(narration, 0.1, 0.9)])

    events = caption_events(executor, narration, align(narration), 1.2)

    lines = [line for event in events for line in event.text.split(r"\N")]
    assert "".join(lines) == narration
    assert any(measurement in line for line in lines)
    assert all(portable._subtitle_visible_length(line) <= portable._SUBTITLE_LINE_CHARS for line in lines)


@pytest.mark.parametrize("identifier", ["5G网络支持", "3D建模", "5mmWave", "1.2.3版本"])
def test_aligned_caption_keeps_numeric_leading_identifiers_together(aligner, identifier):
    executor, _, state, _, align = aligner
    narration = "甲" * 13 + identifier
    state["segments"] = segments([(narration, 0.1, 0.9)])

    events = caption_events(executor, narration, align(narration), 1.2)

    lines = [line for event in events for line in event.text.split(r"\N")]
    assert "".join(lines) == narration
    assert any(identifier in line for line in lines)
    assert all(portable._subtitle_visible_length(line) <= portable._SUBTITLE_LINE_CHARS for line in lines)


def test_aligned_caption_wraps_long_cjk_without_losing_text(aligner):
    executor, _, state, _, align = aligner
    narration = "这是一个比较长的中文字幕句子，需要正常换行。"
    state["segments"] = segments([(narration, 0.1, 0.9)])

    events = caption_events(executor, narration, align(narration), 1.2)

    lines = [line for event in events for line in event.text.split(r"\N")]
    assert len(lines) > 1
    assert "".join(lines) == narration
    assert all(len(line) <= portable._SUBTITLE_LINE_CHARS for line in lines)


def test_aligned_caption_packs_multiple_punctuation_breaks_into_two_lines(aligner):
    executor, _, state, _, align = aligner
    narration = "甲" * 6 + "，" + "乙" * 6 + "，" + "丙" * 6 + "。"
    state["segments"] = segments([(narration, 0.1, 0.9)])

    events = caption_events(executor, narration, align(narration), 1.2)

    lines = [line for event in events for line in event.text.split(r"\N")]
    assert len(events) == 1
    assert (events[0].start, events[0].end) == (100, 900)
    assert len(lines) == 2
    assert "".join(lines) == narration
    assert all(len(line) <= portable._SUBTITLE_LINE_CHARS for line in lines)


def test_aligned_caption_rejects_unbreakable_token_wider_than_line(aligner):
    executor, _, state, _, align = aligner
    narration = "abcdefghijklmnop"
    state["segments"] = segments([(narration, 0.1, 0.9)])

    with pytest.raises(NonRetryableMediaError, match="two lines"):
        caption_events(executor, narration, align(narration), 1.2)


@pytest.mark.parametrize(("start", "end"), [(-0.1, .5), (.1, .1), (.5, .1), (float("nan"), .5), (.1, float("inf")), (.1, 2), (True, 1)])
def test_invalid_word_times_fail_instead_of_estimation(aligner, start, end):
    _, _, state, _, align = aligner
    state["segments"] = segments([("产品测试", start, end)])
    with pytest.raises(NonRetryableMediaError, match="tim"):
        align()


@pytest.mark.parametrize("words", [[("产品", .1, .8), ("测试", .2, 1)], [("产品", .4, .8), ("测试", .3, 1)]])
def test_severe_overlap_or_reverse_order_fails(aligner, words):
    _, _, state, _, align = aligner
    state["segments"] = segments(words)
    with pytest.raises(NonRetryableMediaError, match="tim"):
        align()


@pytest.mark.parametrize("failure", ["disabled", "unavailable", "missing_words", "segment_disagrees"])
def test_unavailable_or_incomplete_asr_is_explicit_failure(aligner, failure):
    executor, voice, state, _, align = aligner
    if failure == "disabled":
        with pytest.raises(NonRetryableMediaError, match="ASR"):
            executor.align_subtitles(voice, "产品测试。", asr_model="")
        return
    if failure == "unavailable":
        state.update(segments=[], note="fixture unavailable")
    elif failure == "missing_words":
        state["segments"][0].pop("words")
    else:
        state["segments"][0]["text"] = "different segment text"
    with pytest.raises(NonRetryableMediaError, match="ASR|match"):
        align()


def test_long_atom_wraps_on_screen_without_splitting_its_time(aligner):
    executor, _, state, _, align = aligner
    narration = "甲" * 20
    state["segments"] = segments([(narration, .1, .8)])
    events = caption_events(executor, narration, align(narration), 1.2)
    assert len(events) == 1 and (events[0].start, events[0].end) == (100, 800)
    assert [len(line) for line in events[0].text.split(r"\N")] == [14, 6]


def test_atom_too_long_to_display_fails(aligner):
    _, _, state, _, align = aligner
    narration = "甲" * 29
    state["segments"] = segments([(narration, .1, .8)])
    with pytest.raises(NonRetryableMediaError, match="display"):
        align(narration)


def test_voice_asr_runs_fresh_and_identity_binds_approved_bytes(aligner):
    _, voice, _, calls, align = aligner
    first, second = align(), align()
    assert first == second and calls == [("fixture", "cpu", "int8")] * 2
    assert first["voice_sha256"] == hashlib.sha256(voice.read_bytes()).hexdigest()
    assert first["narration_sha256"] == hashlib.sha256("产品测试。".encode()).hexdigest()
    assert first["mapping_sha256"] == hashlib.sha256(json.dumps(first["words"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.mark.parametrize(("note", "code"), [
    ("faster-whisper is not installed; secret", "dependency_missing"),
    ("source video missing; secret", "source_missing"),
    ("local ASR failed (RuntimeError): secret", "runtime_error"),
    ("local ASR failed (ValueError): secret", "value_error"),
    ("local ASR failed (OSError): secret", "os_error"),
    ("unrecognized secret", "unavailable"), ("", "empty"),
])
def test_asr_failure_keeps_safe_category_without_raw_details(aligner, note, code):
    _, _, state, _, align = aligner
    state.update(segments=[], note=note)
    with pytest.raises(NonRetryableMediaError, match=code) as failure:
        align()
    assert "secret" not in str(failure.value)


@pytest.mark.parametrize("damage", ["voice", "narration", "segments", "mapping", "model"])
def test_supplied_alignment_identity_and_observations_are_revalidated(aligner, damage):
    executor, voice, _, _, align = aligner
    alignment = align()
    narration = "产品测试。"
    if damage == "voice":
        voice.write_bytes(b"replacement")
    elif damage == "narration":
        narration = "不同原稿。"
    elif damage == "segments":
        alignment["segments"][0]["words"][0]["end_sec"] = .3
    elif damage == "mapping":
        alignment["words"][0]["text"] = "different"
    else:
        alignment["asr_model"] = ""
    with pytest.raises(NonRetryableMediaError, match="alignment"):
        executor._checked_subtitle_alignment(alignment, narration, 1.2, voice_path=voice)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
def test_portable_render_uses_explicit_alignment_without_changing_visual_timing(tmp_path, monkeypatch):
    from PIL import Image
    executor = MediaExecutor(tmp_path)
    voice = tmp_path / "voice.wav"
    portable.run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                          "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=tmp_path)
    boundary = voice.with_name("word_boundaries.json")
    boundary.write_text(json.dumps([{"text": "产品测试。", "start_sec": 0, "end_sec": .4}]))
    frozen = boundary.read_bytes()
    evidence = executor.visual_timing_evidence(voice, .4)
    monkeypatch.setattr(executor, "_local_asr", lambda *args, **kwargs: (segments([("产品测试。", .05, .3)]), ""))
    alignment = executor.align_subtitles(voice, "产品测试。", asr_model="fixture", asr_device="cpu", asr_compute_type="int8")
    monkeypatch.setattr(executor, "_local_asr", lambda *args, **kwargs: pytest.fail("Direct renderer invoked ASR"))
    image = tmp_path / "image.png"
    Image.new("RGB", (1080, 1920), "navy").save(image)
    arguments = dict(video_path=tmp_path / "unused.mp4", voice_path=voice, narration="产品测试。",
                     scenes=[], assets={"image": image}, generated_image_ids={"image"},
                     visual_timing_evidence=evidence,
                     plan_data={"segments": [{"asset_id": "image", "narration_start_sec": 0,
                                               "narration_end_sec": .4, "layout": "image_card",
                                               "purpose": "concept image", "motion": "hold"}]})
    final, preview = executor.render(**arguments, subtitle_alignment=alignment)
    assert final.is_file() and preview.is_file()
    receipts = list((tmp_path / "render").glob("subtitle-alignment-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text()) == {"video_sha256": portable.sha256_file(final), "subtitle_alignment": alignment}
    assert caption_events(executor, "产品测试。", alignment, .4)[0].end == 300
    assert boundary.read_bytes() == frozen and executor.visual_timing_evidence(voice, .4) == evidence
    # Omitted alignment remains an ASR-free direct/synthetic compatibility path.
    executor.render(**arguments)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
def test_failed_content_normal_http_rerender_keeps_history_and_creates_new_evidence(image_contract, monkeypatch):
    from fastapi.testclient import TestClient
    from PIL import Image
    from sqlmodel import Session, select
    from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, StageRun, StageStatus, TaskStatus, TaskType
    from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS
    from glance_core.task_service import stable_hash
    from glance_media.executor import run_command, sha256_file
    from app.business import pipeline
    from app.business.runtime import AGENT_CONSUMED, OWNED
    from test.business.test_content_http import content_app
    from test.business.test_content_qc_recovery import accept
    from test.business.test_discovery_http import ADMIN

    runtime, content_id, plan_id, _, asset_qc_id, image_id, _ = image_contract
    runtime.settings.asr_model, runtime.settings.asr_device, runtime.settings.asr_compute_type = "fixture", "cpu", "int8"
    runtime.settings.bgm_enabled, runtime.settings.min_video_seconds = False, .1
    observations = {"last": 2.9, "calls": []}

    def recognize(self, model, device, compute, *, video_path):
        observations["calls"].append(Path(video_path))
        if observations.get("unavailable"):
            return [], "local ASR failed (RuntimeError): secret diagnostic must not escape"
        return segments([("产品", .1, 1.1), ("测试。", 1.2, observations["last"])]), ""

    monkeypatch.setattr(MediaExecutor, "_local_asr", recognize)
    http = TestClient(content_app(runtime))
    try:
        # Prepare real accepted fixture materials before freezing any failed output.
        with Session(runtime.engine) as session:
            root = Path(session.get(Content, content_id).content_dir)
            image = session.get(Artifact, image_id)
            Image.new("RGB", (1080, 1920), "navy").save(root / image.relative_path)
            image.sha256, image.size_bytes = sha256_file(root / image.relative_path), (root / image.relative_path).stat().st_size
            session.add(image)
            plan = session.get(AgentTask, plan_id)
            plan.result_data = deepcopy(plan.result_data)
            plan.result_data["segments"][0].update(purpose="approved concept image", motion="hold")
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == plan_id)).one()
            submission.result_hash = stable_hash(plan.result_data)
            Path(submission.raw_result_path).write_text(json.dumps(plan.result_data), encoding="utf-8")
            session.add(submission)
            for event in session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan_id)).all():
                if event.action in {"completed", AGENT_CONSUMED}:
                    event.metadata_json = {"result_hash": submission.result_hash}
                    session.add(event)
            session.add(plan)
            flow = runtime.workflow_service(session)
            asset_qc = session.get(AgentTask, asset_qc_id)
            generation = flow.completed_task_for(content_id, TaskType.ASSET_GENERATE_IMAGE)
            asset_qc.input_data = pipeline._asset_qc_input(session, flow, generation, plan)
            asset_qc.input_hash = stable_hash(asset_qc.input_data)
            session.add(asset_qc)
            for event in session.exec(select(AuditEvent).where(AuditEvent.entity_id == asset_qc_id, AuditEvent.action == OWNED)).all():
                event.metadata_json = {"input_hash": asset_qc.input_hash}
                session.add(event)
            voice = root / "audio/narration.mp3"
            voice.parent.mkdir()
            run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                         "-ar", "48000", "-ac", "2", str(voice)], cwd=root)
            session.add(Artifact(content_id=content_id, artifact_type="voice", relative_path="audio/narration.mp3",
                                 sha256=sha256_file(voice), size_bytes=voice.stat().st_size,
                                 mime_type="audio/mpeg", producer_stage="voice"))
            session.commit()
        assert runtime.consume_agent(asset_qc_id)
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            content.business_status = ContentStatus.FAILED
            session.add(content)
            session.commit()
        response = http.post(f"/api/v1/contents/{content_id}/render", headers=ADMIN)
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            old_qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC)).one()
            assert old_qc.status == TaskStatus.PENDING
            result = {"contract_version": 2, "confidence": "high", "passed": False,
                      "subject": old_qc.input_data["acceptance_subject"],
                      "checks": {key: {"passed": key != "subtitles", "evidence": "synthetic reviewer observation"}
                                 for key in CONTENT_ACCEPTANCE_CHECKS},
                      "issues": [{"severity": "error", "field": "subtitles", "message": "synthetic old failure"}]}
            accept(session, runtime, old_qc, result, "fixture-worker", consumed=True)
            signature = runtime.workflow_service(session)._root_cause_signature("BUSINESS_QC", "CONTENT_QC_FAILED")
            for number in range(1, 4):
                recovery = runtime.workflow_service(session).create_agent_task(content_id, TaskType.RECOVERY, {
                    "failure": {"source_task_id": old_qc.id, "failure_class": "BUSINESS_QC", "code": "CONTENT_QC_FAILED"},
                    "lineage": {"round": number, "max_rounds": 3, "root_task_id": old_qc.id,
                                "root_cause_signature": signature, "repair_fingerprint": f"historic-{number}"},
                }, allow_multiple=True)
                accept(session, runtime, recovery, {"action": "mark_recovery_blocked"}, "fixture-worker", consumed=True)
            content = session.get(Content, content_id)
            content.business_status, content.current_stage, content.script_version = ContentStatus.FAILED, "recovery_blocked", 4
            session.add(content)
            session.commit()
            preserved = {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()}
            raw_before = {row.raw_result_path: Path(row.raw_result_path).read_bytes() for row in session.exec(select(AgentSubmission)).all()}
            audits_before = {row.id: row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()}
            old_video = session.get(Artifact, old_qc.input_data["video_artifact_id"])
            old_path, old_hash = root / old_video.relative_path, old_video.sha256
            old_alignment = (old_path.parent / "subtitle-alignment.json").read_bytes()
            frozen_files = {str(root / item.relative_path): sha256_file(root / item.relative_path)
                            for item in session.exec(select(Artifact).where(Artifact.content_id == content_id)).all()}
            history = deepcopy(runtime.workflow_service(session)._recovery_history(content_id))
            old_id = old_qc.id
        observations["unavailable"] = True
        with monkeypatch.context() as failure_patch:
            failure_patch.setattr(pipeline.MediaExecutor, "render", lambda *args, **kwargs: pytest.fail("Renderer ran after failed alignment"))
            failed = http.post(f"/api/v1/contents/{content_id}/render", headers=ADMIN)
        assert failed.status_code == 409 and "runtime_error" in failed.text and "secret" not in failed.text
        with Session(runtime.engine) as session:
            assert {task.id for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()} == set(preserved)
            assert runtime.workflow_service(session)._recovery_history(content_id) == history
            assert {str(root / item.relative_path) for item in session.exec(select(Artifact).where(Artifact.content_id == content_id)).all()} == set(frozen_files)
            failures = session.exec(select(StageRun).where(StageRun.content_id == content_id,
                                                          StageRun.stage == "render", StageRun.status == StageStatus.FAILED)).all()
            assert failures and "runtime_error" in failures[-1].error_message
            assert "secret" not in failures[-1].error_message
        observations["unavailable"] = False
        observations["last"] = 2.3
        response = http.post(f"/api/v1/contents/{content_id}/render", headers=ADMIN)
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            new_qcs = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC, AgentTask.id != old_id)).all()
            assert len(new_qcs) == 1
            new_qc = new_qcs[0]
            assert new_qc.status == TaskStatus.PENDING and not new_qc.result_data
            new_video = session.get(Artifact, new_qc.input_data["video_artifact_id"])
            new_path = root / new_video.relative_path
            assert new_path.parent != old_path.parent and new_video.sha256 != old_hash
            assert json.loads((new_path.parent / "subtitle-alignment.json").read_text())["words"][-1]["end_sec"] == 2.3
            assert new_qc.input_data["audio_evidence"]["video_sha256"] == new_video.sha256
            assert observations["calls"].count(voice) == 3
            assert old_path in observations["calls"] and new_path in observations["calls"]
            for task_id, before in preserved.items():
                assert session.get(AgentTask, task_id).model_dump(mode="json") == before
            assert runtime.workflow_service(session)._recovery_history(content_id) == history
            assert session.get(Content, content_id).script_version == 4
            for audit_id, before in audits_before.items():
                assert session.get(AuditEvent, audit_id).model_dump(mode="json") == before
            assert all(Path(path).read_bytes() == before for path, before in raw_before.items())
            assert all(sha256_file(Path(path)) == before for path, before in frozen_files.items())
            assert (old_path.parent / "subtitle-alignment.json").read_bytes() == old_alignment
    finally:
        http.close()
