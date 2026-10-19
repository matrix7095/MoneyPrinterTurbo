"""Real H.264 encoding contract for image-card micro-motion and cache upgrades."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from glance_media.executor import run_command, sha256_file
from PIL import Image, ImageDraw

from app.business import media_executor
from app.business.media_executor import MptMediaExecutor
from app.services import video_project
from app.services.video_project import VideoProject, stage_graph
from test.business.test_render_media_immutability import render_fixture as render_fixture


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="native FFmpeg required")
def test_real_image_encoding_preserves_crf_and_cfr_with_mbtree_disabled(tmp_path, monkeypatch):
    executor = MptMediaExecutor(tmp_path / "content")
    image = executor.root / "synthetic.png"
    picture = Image.new("RGB", (320, 480), "#b4bcc4")
    draw = ImageDraw.Draw(picture)
    for index in range(12):
        draw.rectangle((index * 24, index * 31, index * 24 + 22, index * 31 + 80),
                       fill=(index * 19, 255 - index * 17, index * 13))
    picture.save(image)
    voice = executor.root / "voice.wav"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.41",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=executor.root)
    final, _ = executor.render(
        video_path=executor.root / "unused.mp4", voice_path=voice, narration="测试。", scenes=[],
        assets={"image": image}, generated_image_ids={"image"}, plan_data={"segments": [{
            "asset_id": "image", "layout": "image_card", "motion": "pan_right", "purpose": "synthetic fixture",
            "focus_x": .5, "focus_y": .51, "narration_start_sec": 0, "narration_end_sec": .41,
        }]},
    )
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text("utf-8"))
    project = VideoProject(executor.root / "render/mpt-project")
    revision = project.revision()
    stage = next(stage for stage in stage_graph(revision) if stage.kind == "scene")
    cached = project._cached(stage)
    assert cached is not None
    observed = {}
    for label, path in (("prepared", Path(receipt["request"]["scenes"][0]["footage"])),
                        ("mpt-scene", Path(cached["path"])), ("final", final)):
        encoded = path.read_bytes()
        observed[label] = {
            "crf": re.findall(rb"\bcrf=([0-9.]+)", encoded),
            "mbtree": re.findall(rb"\bmbtree=([0-9]+)", encoded),
        }
        result = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_frames",
            "-show_entries", "stream=codec_name,width,height,nb_frames:frame=best_effort_timestamp_time",
            "-of", "json", str(path),
        ], capture_output=True, check=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timeline = json.loads(result.stdout)
        stream = timeline["streams"][0]
        assert {key: stream[key] for key in ("codec_name", "width", "height", "nb_frames")} == {
            "codec_name": "h264", "width": 1080, "height": 1920, "nb_frames": "13",
        }
        times = [float(frame["best_effort_timestamp_time"]) for frame in timeline["frames"]]
        assert len(times) == 13
        assert times == pytest.approx([index / 30 for index in range(13)], abs=1e-6)
    assert observed == {label: {"crf": [b"18.0"], "mbtree": [b"0"]} for label in observed}
    assert receipt["renderer_version"] == video_project.RENDERER_VERSION == "local-ffmpeg-v3-cfr-pcm-no-mbtree"
    assert all(stage["action"] == "reuse" for stage in project.plan())
    frozen = sha256_file(final)
    new_fingerprints = {stage.key: stage.fingerprint for stage in stage_graph(revision)}
    monkeypatch.setattr(video_project, "RENDERER_VERSION", "local-ffmpeg-v2-cfr-pcm")
    assert all(stage["action"] == "build" for stage in project.plan())
    assert all(stage.fingerprint != new_fingerprints[stage.key] for stage in stage_graph(revision))
    assert sha256_file(final) == frozen


def test_motion_encoder_upgrade_keeps_old_revision_bytes_and_changes_receipt(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    current = media_executor.RENDERER_VERSION
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", "local-ffmpeg-v2-cfr-pcm")
    old = executor.render(**arguments)
    frozen = {path: path.read_bytes() for path in old}
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", current)
    new = executor.render(**arguments)
    assert new[0].parent != old[0].parent
    assert {path: path.read_bytes() for path in old} == frozen
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text("utf-8"))
    assert receipt["renderer_version"] == current == "local-ffmpeg-v3-cfr-pcm-no-mbtree"
    assert executor.render(**arguments) == new
