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
        if label == "final":
            assert b"vbv_maxrate=20000" in encoded and b"vbv_bufsize=40000" in encoded
        else:
            assert b"vbv_maxrate=" not in encoded
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
            "codec_name": "h264", "width": 1080, "height": 1920, "nb_frames": "25",
        }
        times = [float(frame["best_effort_timestamp_time"]) for frame in timeline["frames"]]
        assert len(times) == 25
        assert times == pytest.approx([index / 60 for index in range(25)], abs=1e-6)
    assert observed == {label: {"crf": [b"10.0"], "mbtree": [b"0"]} for label in observed}
    assert receipt["request"]["settings"] == {
        "fps": 60, "video_crf": 10, "video_maxrate_kbps": 20000, "video_bufsize_kbits": 40000,
    }
    assert json.loads(final.with_name("render-parameters.json").read_text("utf-8")) == {
        "renderer_version": video_project.RENDERER_VERSION, "revision_id": revision["revision_id"],
        "fps": 60, "spatial_sampling": 2, "video_crf": 10,
        "video_maxrate_kbps": 20000, "video_bufsize_kbits": 40000,
    }
    assert receipt["renderer_version"] == video_project.RENDERER_VERSION == "local-ffmpeg-v5-global-vbv"
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
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", "local-ffmpeg-v3-cfr-pcm-no-mbtree")
    old = executor.render(**arguments)
    frozen = {path: path.read_bytes() for path in old}
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", current)
    new = executor.render(**arguments)
    assert new[0].parent != old[0].parent
    assert {path: path.read_bytes() for path in old} == frozen
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text("utf-8"))
    assert receipt["renderer_version"] == current == "local-ffmpeg-v5-global-vbv"
    assert executor.render(**arguments) == new


def test_frozen_sampling_parameters_cannot_change_on_retry(render_fixture):
    executor, arguments = render_fixture
    final, _ = executor.render(**arguments)
    receipt = final.with_name("render-parameters.json")
    receipt.write_text(json.dumps({"fps": 30, "spatial_sampling": 1}), encoding="utf-8")
    frozen = final.read_bytes()
    with pytest.raises(media_executor.NonRetryableMediaError, match="Immutable render parameters"):
        executor.render(**arguments)
    assert final.read_bytes() == frozen


@pytest.mark.parametrize("kind", ["image", "mixed", "original"])
def test_image_profile_uses_cpu_while_video_preserves_requested_encoder(render_fixture, monkeypatch, kind):
    executor, arguments = render_fixture
    arguments["encoder"] = "h264_nvenc"
    if kind != "image":
        arguments["video_path"].write_bytes(b"fixture original footage")
        monkeypatch.setattr(media_executor, "probe", lambda _: {
            "format": {"duration": .4}, "streams": [{"codec_type": "video", "width": 320, "height": 480}]})
        scene = {"scene_id": "source", "source_start_sec": 0, "source_end_sec": .2,
                 "layout": "fit", "purpose": "encoder contract", "narration_start_sec": 0, "narration_end_sec": .4}
        arguments["scenes"] = [{"scene_id": "source", "start_sec": 0, "end_sec": .4}]
        if kind == "mixed":
            arguments["plan_data"]["segments"][0]["narration_end_sec"] = .2
            scene["narration_start_sec"] = .2
            arguments["plan_data"]["segments"].append(scene)
        else:
            arguments["plan_data"]["segments"] = [scene]
    commands = []
    actual = media_executor.run_command
    def capture(command, **kwargs):
        if "-frames:v" in command:
            commands.append(command)
        return actual(command, **kwargs)
    monkeypatch.setattr(media_executor, "run_command", capture)
    executor.render(**arguments)
    assert commands
    expected = "libx264" if kind == "image" else "h264_nvenc"
    assert all(command[command.index("-c:v") + 1] == expected for command in commands)
    if kind == "image":
        assert all(command[command.index("-crf") + 1] == "10" for command in commands)
    else:
        assert all("-crf" not in command for command in commands)


@pytest.mark.parametrize("kind", ["image", "mixed", "original", "original_image_layout"])
@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="native FFmpeg required")
def test_final_two_cut_clock_uses_sixty_only_for_images(tmp_path, kind):
    executor = MptMediaExecutor(tmp_path / "content")
    red, blue = executor.root / "red.png", executor.root / "blue.png"
    Image.new("RGB", (128, 128), "red").save(red)
    Image.new("RGB", (128, 128), "blue").save(blue)
    voice, source = executor.root / "voice.wav", executor.root / "source.mp4"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                 "-t", "0.41", "-c:a", "pcm_s16le", str(voice)], cwd=executor.root)
    if kind != "image":
        run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=red:s=128x128:r=30:d=1",
                     "-c:v", "libx264", str(source)], cwd=executor.root)
    segments = []
    for index, (start, end) in enumerate(((0, .2), (.2, .41))):
        segment = {"narration_start_sec": start, "narration_end_sec": end, "purpose": "cut clock"}
        if kind.startswith("original") or (kind == "mixed" and index == 0):
            segment.update(scene_id="source", source_start_sec=0, source_end_sec=end-start,
                           layout="image_card" if kind == "original_image_layout" else "fit")
        else:
            segment.update(asset_id="red" if index == 0 else "blue", layout="image_card", motion="hold")
        segments.append(segment)
    final, _ = executor.render(
        video_path=source, voice_path=voice, narration="测试。", plan_data={"segments": segments},
        scenes=[{"scene_id": "source", "start_sec": 0, "end_sec": 1}], assets={"red": red, "blue": blue})
    fps = 60 if kind == "image" else 30
    count = 25 if kind == "image" else 13
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams",
        "-show_frames", "-show_entries", "stream=r_frame_rate,duration:frame=best_effort_timestamp_time",
        "-of", "json", str(final)], capture_output=True, check=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(result.stdout)
    assert data["streams"][0]["r_frame_rate"] == f"{fps}/1"
    assert float(data["streams"][0]["duration"]) == pytest.approx(count / fps, abs=1e-6)
    assert [float(frame["best_effort_timestamp_time"]) for frame in data["frames"]] == pytest.approx(
        [index / fps for index in range(count)], abs=1e-6)
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text("utf-8"))
    assert receipt["request"]["settings"]["fps"] == fps
    parameters = json.loads(final.with_name("render-parameters.json").read_text("utf-8"))
    assert (parameters["video_maxrate_kbps"], parameters["video_bufsize_kbits"]) == (
        (20000, 40000) if kind == "image" else (0, 0))
    assert b"vbv_maxrate=20000" in final.read_bytes() if kind == "image" else b"vbv_maxrate=" not in final.read_bytes()
    if kind == "original_image_layout":
        assert receipt["request"]["settings"]["video_crf"] == 18
    assert [scene["frame_count"] for scene in receipt["request"]["scenes"]] == (
        [12, 13] if kind == "image" else [6, 7])
    pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", str(final), "-an", "-vf",
        "crop=2:2:540:960,scale=1:1,format=rgb24", "-f", "rawvideo", "pipe:1"],
        capture_output=True, check=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    assert len(pixels) == count * 3
    first_cut = 12 if kind == "image" else 6
    for index in range(count):
        red, _, blue = pixels[index * 3:index * 3 + 3]
        if kind.startswith("original") or index < first_cut:
            assert red > 200 and blue < 30, (index, red, blue)
        else:
            assert blue > 200 and red < 30, (index, red, blue)
