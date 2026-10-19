"""Approved opening identity reaches both ASS render paths without changing narration."""

import json
import os
import shutil
from pathlib import Path

import numpy as np
import pysubs2
import pytest
from glance_core.models import AgentSubmission, AgentTask, AuditEvent
from glance_core.task_service import stable_hash
from glance_media.executor import FONT_FILES, MediaExecutor, NonRetryableMediaError, probe, run_command
from PIL import Image, ImageFont
from sqlmodel import Session, select

from app.business import pipeline
from app.business.media_executor import MptMediaExecutor
from app.business.runtime import AGENT_CONSUMED
from test.business import test_visual_plan_materialization


IDENTITY = "Jackery Explorer 1000 Plus v2"
image_contract = test_visual_plan_materialization.image_contract


@pytest.mark.parametrize("identity", [IDENTITY, None])
def test_pipeline_passes_approved_opening_identity(image_contract, monkeypatch, identity):
    runtime, content_id, plan_id, _, qc_id, _, _ = image_contract
    with Session(runtime.engine) as session:
        script_id = session.get(AgentTask, plan_id).input_data["upstream_task_ids"]["script"]
        script = session.get(AgentTask, script_id)
        if identity is not None:
            script.result_data = {**script.result_data, "opening": {"product_identity": identity}}
            digest = stable_hash(script.result_data)
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == script_id)).one()
            submission.result_hash = digest
            Path(submission.raw_result_path).write_text(json.dumps(script.result_data), encoding="utf-8")
            session.add(submission)
            for event in session.exec(select(AuditEvent).where(AuditEvent.entity_id == script_id)).all():
                if event.action in {"completed", AGENT_CONSUMED}:
                    event.metadata_json = {"result_hash": digest}
                    session.add(event)
            session.add(script)
            session.commit()
    assert runtime.consume_agent(qc_id)
    calls = []

    class RenderReached(Exception):
        pass

    def render(self, **arguments):
        calls.append(arguments)
        raise RenderReached

    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *names: None)
    monkeypatch.setattr(pipeline, "_render_voice_path", lambda *args: Path("fixture-voice.wav"))
    monkeypatch.setattr(pipeline.MediaExecutor, "align_subtitles", lambda *args, **kwargs: {"fixture": "pre-render ASR contract"})
    monkeypatch.setattr(pipeline.MediaExecutor, "render", render)
    with pytest.raises(RenderReached):
        pipeline.render_content(runtime.engine, runtime.settings, content_id)
    assert calls[0]["opening_identity"] == identity
    assert calls[0]["narration"] == "产品测试。"
    assert IDENTITY not in calls[0]["narration"]


@pytest.mark.parametrize("duration,end", [(12, 8000), (2.25, 2250), (2.257, 2250)])
def test_ass_identity_interval_safe_layout_and_existing_subtitles(tmp_path, duration, end):
    executor = MediaExecutor(tmp_path)
    before = pysubs2.load(str(executor._build_subtitles("产品测试。", duration, concept_intervals=[(0, duration)])))
    after = pysubs2.load(str(executor._build_subtitles(
        "产品测试。", duration, opening_identity=IDENTITY, concept_intervals=[(0, duration)]
    )))
    assert [event.as_dict() for event in after if event.style != "OpeningIdentity"] == [event.as_dict() for event in before]
    for name in ("Default", "Concept"):
        assert after.styles[name].as_dict() == before.styles[name].as_dict()
    caption, = [event for event in after if event.style == "OpeningIdentity"]
    assert (caption.start, caption.end) == (0, end)
    assert caption.text.replace(r"\N", "") == IDENTITY
    style = after.styles[caption.style]
    assert style.alignment == 8 and style.fontsize >= 48
    assert style.marginl >= 80 and style.marginr >= 240
    assert 250 <= style.marginv <= 400
    assert "OpeningIdentity" not in before.styles


def test_identity_text_cannot_inject_ass_and_long_names_are_not_truncated(tmp_path):
    executor = MediaExecutor(tmp_path)
    unsafe = "Jackery {\\pos(0,0)}\\NExplorer\n1000\r\nPlus\x00 v2"
    subtitles = pysubs2.load(str(executor._build_subtitles("产品测试。", 12, opening_identity=unsafe)))
    caption, = [event for event in subtitles if event.style == "OpeningIdentity"]
    assert "{" not in caption.text and "}" not in caption.text
    assert "\\" not in caption.text.replace(r"\N", "")
    assert "\n" not in caption.text and "\r" not in caption.text and "\x00" not in caption.text
    long_identity = "正式型号 " + "WideProductModel " * 5 + "1000 Plus v2"
    subtitles = pysubs2.load(str(executor._build_subtitles("产品测试。", 12, opening_identity=long_identity)))
    caption, = [event for event in subtitles if event.style == "OpeningIdentity"]
    assert caption.text.count(r"\N") >= 3
    assert caption.text.replace(r"\N", "") == long_identity
    assert caption.text.replace(r"\N", " ").split() == long_identity.split()
    with pytest.raises(NonRetryableMediaError, match="too long"):
        executor._build_subtitles("产品测试。", 12, opening_identity="型号" * 100)
    with pytest.raises(NonRetryableMediaError, match="too long"):
        executor._build_subtitles("产品测试。", 12, opening_identity="WideProductModel" * 5)


@pytest.mark.parametrize("identity", [
    "Model Alpha. v2", "Sony WF-1000XM5 Wireless", "IKEA KOMPISHÄNG", "DJI Osmo Action 5 Pro.",
    "Model  Alpha. v2",
])
def test_identity_wrap_preserves_punctuation_spaces_and_complete_tokens(tmp_path, identity):
    subtitles = pysubs2.load(str(MediaExecutor(tmp_path)._build_subtitles(
        "产品测试。", 12, opening_identity=identity,
    )))
    caption, = [event for event in subtitles if event.style == "OpeningIdentity"]
    assert caption.text.replace(r"\N", "") == identity
    assert caption.text.replace(r"\N", " ").split() == identity.split()


@pytest.mark.parametrize("identity,model_tokens", [
    ("科沃斯地宝扫拖机器人全能旗舰版", []),
    ("小米米家全能扫拖机器人旗舰款第二代", []),
    ("全能扫拖机器人家庭清洁旗舰款升级第二代", []),
    ("科沃斯全能扫拖机器人 Sony WF-1000XM5 Wireless", ["Sony", "WF-1000XM5", "Wireless"]),
    ("宜家家具功能设计 IKEA KOMPISHÄNG 升级第二代", ["IKEA", "KOMPISHÄNG"]),
    ("便携储能电源Jackery Explorer 1000 Plus v2第二代", ["Jackery", "Explorer", "1000", "Plus", "v2"]),
])
def test_identity_wrap_supports_cjk_without_splitting_model_tokens(tmp_path, identity, model_tokens):
    subtitles = pysubs2.load(str(MediaExecutor(tmp_path)._build_subtitles(
        "产品测试。", 12, opening_identity=identity,
    )))
    caption, = [event for event in subtitles if event.style == "OpeningIdentity"]
    assert caption.text.replace(r"\N", "") == identity
    assert r"\N" in caption.text
    for token in model_tokens:
        assert token in caption.text
    assert subtitles.styles[caption.style].fontsize == 48
    font = ImageFont.truetype(str(FONT_FILES[1]), 48)
    assert all(font.getlength(line) <= 692 for line in caption.text.split(r"\N"))
    if not model_tokens:
        assert len(caption.text.split(r"\N")) == 2


@pytest.mark.parametrize("identity", [None, "", " \n\t "])
def test_absent_identity_preserves_default_ass(tmp_path, identity):
    executor = MediaExecutor(tmp_path)
    expected = executor._build_subtitles("产品测试。", 2).read_bytes()
    actual = executor._build_subtitles("产品测试。", 2, opening_identity=identity).read_bytes()
    assert actual == expected


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
@pytest.mark.parametrize("executor_type", [MptMediaExecutor, MediaExecutor])
@pytest.mark.parametrize("identity_name,identity", [
    ("jackery", IDENTITY),
    ("cjk", "小米米家全能扫拖机器人旗舰款第二代"),
    ("mixed", "宜家家具功能设计 IKEA KOMPISHÄNG 升级第二代"),
])
def test_real_render_burns_identity_with_disclosure_and_narration(tmp_path, executor_type, identity_name, identity):
    # An explicit smoke directory preserves the actual renderer outputs for independent pixel QA.
    root = Path(os.environ.get("MPT_OPENING_SMOKE_DIR", str(tmp_path / "opening-smoke"))) / identity_name
    if executor_type is MediaExecutor:
        root = root / "shared-executor"
    executor = executor_type(root)
    voice = root / "voice.wav"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=8.4",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=root)
    image = root / "concept.png"
    Image.new("RGB", (1080, 1920), (32, 48, 64)).save(image)
    final, preview = executor.render(
        video_path=root / "unused.mp4", voice_path=voice, narration="产品测试。",
        opening_identity=identity, plan_data={"segments": [{
            "asset_id": "concept", "narration_start_sec": 0, "narration_end_sec": 8.4,
            "layout": "image_card", "purpose": "No identity inferred here", "motion": "hold",
        }]}, scenes=[], assets={"concept": image}, generated_image_ids={"concept"},
    )
    assert final.is_file() and preview.is_file()
    subtitles = pysubs2.load(str(root / "transcript" / "narration.ass"))
    caption, = [event for event in subtitles if event.style == "OpeningIdentity"]
    assert caption.text.replace(r"\N", "") == identity
    assert abs(float(probe(final)["format"]["duration"]) - 8.4) < 0.15
    for output in (final, preview):
        run_command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], cwd=root)
    frames = {}
    for instant in (0, 3, 7, 8.2):
        frame = root / f"frame-{instant}s.png"
        run_command(["ffmpeg", "-y", "-ss", str(instant), "-i", str(final), "-frames:v", "1", str(frame)], cwd=root)
        frames[instant] = np.asarray(Image.open(frame).convert("RGB"))
    opening_region = (slice(250, 900), slice(80, 840))
    for instant in (0, 3, 7):
        assert (frames[instant][opening_region].min(axis=2) > 180).sum() > 1000
    assert (frames[8.2][opening_region].min(axis=2) > 180).sum() == 0
    for instant in frames:
        assert (frames[instant][80:200, 40:600].min(axis=2) > 180).sum() > 100
        assert (frames[instant][1300:1640, 100:980].min(axis=2) > 180).sum() > 100
    (root / "evidence.json").write_text(json.dumps({
        "identity": identity, "narration": "产品测试。", "final": str(final), "preview": str(preview),
        "frames": [str(root / f"frame-{instant}s.png") for instant in frames],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
