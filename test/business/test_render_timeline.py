"""Real packet/PCM regression for the production eight-cut, 35.976-second timeline."""

import json
import math
import re
import shutil
import subprocess
import wave

import numpy as np
import pytest
from PIL import Image

from app.business import media_executor

from app.business.media_executor import MptMediaExecutor
from app.services import video_project
from app.services.video_project import ProjectError, VideoProject, stage_graph
from test.services.test_video_project import _media, native as native
from test.business.test_render_media_immutability import render_fixture as render_fixture

EDGES = [0, 2.89375, 8.552083, 13.291666, 17.531249, 21.062499,
         25.822916, 32.072915, 35.976]


def video_timeline(path):
    result = subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_streams", "-show_entries", "frame=best_effort_timestamp_time:stream=duration,nb_frames",
        "-of", "json", str(path),
    ], text=True)
    return json.loads(result)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="native FFmpeg required")
def test_eight_real_cuts_have_one_video_clock_and_complete_pcm(tmp_path, monkeypatch):
    footage = tmp_path / "footage.mp4"
    _media(["-f", "lavfi", "-i", "testsrc2=s=64x64:r=30:d=1", "-c:v", "libx264", str(footage)])
    request = {"project_id": "eight-cuts", "settings": {"width": 64, "height": 64, "fps": 30}, "scenes": []}
    expected_pcm = bytearray()
    previous_frame = 0
    for index, (start, end) in enumerate(zip(EDGES, EDGES[1:])):
        audio = tmp_path / f"voice-{index}.wav"
        # Exact independently decoded PCM samples, including the 6.249999s / 187.5-frame cut.
        count = round(end * 48000) - round(start * 48000)
        samples = (np.sin(np.arange(count) * (440 + index * 50) * 2 * np.pi / 48000) * 5000).astype("<i2")
        pcm = np.repeat(samples[:, None], 2, axis=1).tobytes()
        expected_pcm.extend(pcm)
        with wave.open(str(audio), "wb") as output:
            output.setparams((2, 2, 48000, 0, "NONE", "not compressed"))
            output.writeframes(pcm)
        end_frame = math.ceil(end * 30 - 1e-9)
        request["scenes"].append({"id": f"s{index}", "footage": str(footage), "audio": str(audio),
                                  "frame_count": end_frame - previous_frame})
        previous_frame = end_frame
    project = VideoProject(tmp_path / "project")
    revision = project.create_revision(request)
    rendered = project.render()
    timeline = video_timeline(rendered["export"]["path"])
    times = [float(frame["best_effort_timestamp_time"]) for frame in timeline["frames"]]
    assert len(times) == 1080 and times[0] == 0
    assert np.allclose(np.diff(times), 1 / 30, atol=1e-6, rtol=0)
    assert float(timeline["streams"][0]["duration"]) == 36
    assert [s["frame_count"] for s in revision["scenes"]] == [87, 170, 142, 127, 106, 143, 188, 117]
    assert all(abs(math.ceil(end * 30 - 1e-9) / 30 - end) < 1 / 30 for end in EDGES[1:])
    actual_pcm = bytearray()
    for scene in revision["scenes"]:
        audio_record = rendered["stages"][f"{scene['id']}:audio"]
        artifact = project._cached(next(s for s in stage_graph(revision) if s.key == f"{scene['id']}:audio"))
        assert audio_record["sha256"] == artifact["sha256"]
        scene_artifact = project._cached(next(s for s in stage_graph(revision) if s.key == f"{scene['id']}:scene"),
                                        {key: project._cached(next(s for s in stage_graph(revision) if s.key == key))
                                         for key in (f"{scene['id']}:audio", f"{scene['id']}:timing")})
        streams = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                                       scene_artifact["path"]], text=True))["streams"]
        assert [stream["codec_type"] for stream in streams] == ["video"]
        with wave.open(artifact["path"], "rb") as source:
            actual_pcm.extend(source.readframes(source.getnframes()))
    assert actual_pcm == expected_pcm and len(actual_pcm) == round(35.976 * 48000) * 4
    # A previous renderer's valid cache cannot satisfy the changed clock implementation.
    monkeypatch.setattr(video_project, "RENDERER_VERSION", "local-ffmpeg-v1")
    assert all(stage["action"] == "build" for stage in project.plan())


@pytest.mark.parametrize("count", [True, 0, -1, 1.5, 108001])
def test_explicit_frame_count_rejects_invalid_prepared_contract(tmp_path, monkeypatch, count):
    source = tmp_path / "footage.mp4"
    source.write_bytes(b"local fixture")
    monkeypatch.setattr(video_project, "_tool_version", lambda _: "fixture")
    with pytest.raises(ProjectError, match="frame_count"):
        VideoProject(tmp_path / "project").create_revision({
            "project_id": "invalid", "scenes": [{"id": "s", "footage": str(source), "frame_count": count}],
        })


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="native FFmpeg required")
def test_final_mux_preserves_noninteger_cfr_frames_and_bgm(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    voice, bgm = executor.root / "voice.wav", executor.root / "bgm.wav"
    for path, hz in ((voice, 440), (bgm, 1320)):
        _media(["-f", "lavfi", "-i", f"sine=frequency={hz}:sample_rate=48000:duration=0.41",
                "-ac", "2", "-c:a", "pcm_s16le", str(path)])
    image = executor.root / "image.png"
    Image.new("RGB", (100, 100), "red").save(image)
    arguments = dict(video_path=executor.root / "unused.mp4", voice_path=voice, narration="完整旁白。",
                     assets={"image": image}, generated_image_ids={"image"}, scenes=[],
                     plan_data={"segments": [{"asset_id": "image", "layout": "image_card", "motion": "hold",
                                               "purpose": "fixture", "narration_start_sec": 0, "narration_end_sec": .41}]})
    plain, _ = executor.render(**arguments)
    mixed, _ = executor.render(**arguments, bgm_path=bgm, bgm_volume=.25)
    for path in (plain, mixed):
        timeline = video_timeline(path)
        times = [float(frame["best_effort_timestamp_time"]) for frame in timeline["frames"]]
        assert len(times) == 13 and times[0] == 0
        assert np.allclose(np.diff(times), 1 / 30, atol=1e-6, rtol=0)
        assert abs(float(timeline["streams"][0]["duration"]) - 13 / 30) < 1e-6
    pcm = []
    for path in (plain, mixed):
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
                                       "-f", "s16le", "-ar", "48000", "-ac", "1", "-"])
        pcm.append(np.frombuffer(raw, dtype="<i2").astype(float))
    assert min(map(len, pcm)) >= round(.41 * 48000)
    assert np.corrcoef(pcm[0][:19680], pcm[1][:19680])[0, 1] > .97
    assert np.std(pcm[1][:19680] - pcm[0][:19680]) > 50
    assert "AI概念示意" in (executor.root / "transcript/narration.ass").read_text(encoding="utf-8-sig")


def test_default_scene_keeps_its_stereo_48khz_preview_audio(native):
    project, request, revision, root = native
    project.render()
    stage = next(s for s in stage_graph(revision) if s.key == "opening:scene")
    artifact = project._cached(stage)
    data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                              artifact["path"]], text=True))
    audio = next(stream for stream in data["streams"] if stream["codec_type"] == "audio")
    assert audio["sample_rate"] == "48000" and audio["channels"] == 2
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", artifact["path"], "-map", "0:a:0",
                                   "-f", "s16le", "-"])
    assert np.max(np.abs(np.frombuffer(raw, dtype="<i2").astype(float))) > 1000
    # Direct PCM inputs must participate in the cache receipt even if video bytes stay identical.
    verified = {}
    for current in stage_graph(revision):
        verified[current.key] = project._cached(current, verified)
    assembly = next(s for s in stage_graph(revision) if s.kind == "assembly")
    assert "opening:audio" in assembly.dependencies
    assert project._cached(assembly, verified) is not None
    verified["opening:audio"] = {**verified["opening:audio"], "sha256": "a" * 64}
    assert project._cached(assembly, verified) is None
    request["scenes"][0]["frame_count"] = 30
    project.create_revision(request, relative_to=root)
    with pytest.raises(ProjectError, match="does not match prepared audio"):
        project.render()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="native FFmpeg required")
def test_cumulative_ass_clock_uses_exact_integer_frame_526(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    monkeypatch.setattr(media_executor, "probe", lambda _: {"format": {"duration": 35.976}})
    arguments["plan_data"] = {"segments": [{"asset_id": "image", "layout": "image_card", "motion": "hold",
        "purpose": "clock fixture", "narration_start_sec": start, "narration_end_sec": end}
        for start, end in zip(EDGES, EDGES[1:])]}
    captured = []
    actual_command = media_executor.run_command
    def command(arguments, **kwargs):
        if "-vf" in arguments and "ass=" in arguments[arguments.index("-vf") + 1]:
            captured.append(arguments)
        return actual_command(arguments, **kwargs)
    monkeypatch.setattr(media_executor, "run_command", command)
    executor.render(**arguments)
    assert [int(c[c.index("-frames:v") + 1]) for c in captured] == [87, 170, 142, 127, 106, 143, 188, 117]
    filters = captured[4][captured[4].index("-vf") + 1]
    clock = filters[filters.index("settb=1/30"):].split(",ass=", 1)[0]
    actual = subprocess.run(["ffmpeg", "-hide_banner", "-f", "lavfi", "-i", "color=s=64x64:r=30:d=0.1",
        "-vf", clock + ",showinfo", "-frames:v", "1", "-fps_mode", "passthrough", "-f", "null", "-"],
        capture_output=True, text=True)
    assert actual.returncode == 0, actual.stderr
    assert re.search(r"n:\s*0\s+pts:\s*526\s+pts_time:17\.5333", actual.stderr)
