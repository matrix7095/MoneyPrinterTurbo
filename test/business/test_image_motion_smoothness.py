import json
import shutil
import subprocess
import wave

import cv2
import numpy as np
import pytest
from PIL import Image

from glance_media.executor import layout_filter
from app.business.media_executor import MptMediaExecutor


def test_image_card_hold_does_not_upsample():
    assert "scale=4320:7680" not in layout_filter("image_card", motion="hold")


@pytest.mark.parametrize(
    ("motion", "velocity"),
    [
        ("ken_burns_in", ((299.5 - 540) * 0.0005, (399.5 - 921.6) * 0.0005)),
        ("ken_burns_out", ((540 - 299.5) * 0.0005, (921.6 - 399.5) * 0.0005)),
        ("focus_product", ((299.5 - 540) * 0.00075, (399.5 - 921.6) * 0.00075)),
        ("pan_left", (-0.45, 0)),
        ("pan_right", (0.45, 0)),
        ("hold", (0, 0)),
    ],
)
def test_image_card_motion_has_smooth_subpixel_steps(tmp_path, motion, velocity):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the image motion regression")

    image = Image.new("RGB", (1080, 1920), "white")
    image.paste("black", (200, 0, 400, 1920))
    image.paste("black", (0, 300, 1080, 500))
    source = tmp_path / "vertical-bar.png"
    image.save(source)

    completed = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-loop",
            "1",
            "-i",
            str(source),
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            _strip_graph(layout_filter("image_card", motion=motion, focus_y=0.48)),
            "-map",
            "[out]",
            "-frames:v",
            "180",
            "-threads",
            "1",
            "-fps_mode",
            "passthrough",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    centers = _centers(completed.stdout, 180)
    zoom = (
        1.14 if motion == "ken_burns_out" else 1.1 if motion.startswith("pan_") else 1
    )
    x_focus = 0 if motion == "pan_left" else 1 if motion == "pan_right" else 0.5
    first_expected = (
        299.5 + (zoom - 1) * (299.5 * 1080 / 1079 - x_focus * 1080),
        399.5 + (zoom - 1) * (399.5 * 1920 / 1919 - 0.48 * 1920),
    )
    # perspective's output-frame counter starts at 1; the camera starts at 0.
    assert np.allclose([trace[0] for trace in centers], first_expected, atol=0.05)
    _assert_continuous_steps(centers, velocity)


def _strip_graph(template):
    return (
        template + ",format=gray,split=2[a][b];[a]crop=1080:2:0:960[ha];"
        "[b]crop=2:1920:900:0,transpose=1[va];[ha][va]hstack=inputs=2[out]"
    )


def _centers(raw, frames):
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(frames, 2, 3000).mean(axis=1)
    result = []
    for axis, row in enumerate((rows[:, :1080], rows[:, 1080:])):
        dark = np.clip(row.max(axis=1, keepdims=True) - row, 0, None)
        assert np.all(dark.sum(axis=1) > 0)
        center = dark @ np.arange(dark.shape[1]) / dark.sum(axis=1)
        result.append(center if axis == 0 else 1919 - center)
    return result


def _assert_continuous_steps(centers, velocity):
    for axis, (trace, expected) in enumerate(zip(centers, velocity)):
        steps = np.diff(trace)
        if expected == 0:
            assert np.max(np.abs(steps)) < 0.035, (axis, steps)
            continue
        relative = steps / expected
        # The old <.75px/frame² bound missed reversals, dwells and hops during
        # slow motion; compare each step with the known continuous trajectory.
        assert np.all(relative > 0.25), (axis, relative.min())
        assert np.all(relative < 1.75), (axis, relative.max())
        error = np.sqrt(np.mean((steps - expected) ** 2))
        assert error < 0.04, (axis, error)
        assert abs(np.polyfit(np.arange(len(trace)), trace, 1)[0] - expected) < 0.002


def _ffmpeg(*args):
    executable = shutil.which("ffmpeg")
    if not executable:
        pytest.skip("ffmpeg is required for the image motion regression")
    return subprocess.run(
        [executable, "-nostdin", "-hide_banner", "-v", "error", *args],
        check=True,
        capture_output=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
        shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _grid_image(path):
    image = Image.new("RGB", (1080, 1920), (236, 228, 214))
    for x in range(180, 920, 40):
        for y in range(460, 1340, 40):
            color = (65, 85, 105) if (x + y) % 80 else (155, 125, 95)
            image.paste(color, (x, y, x + 25, y + 25))
    image.save(path)


def _camera_geometry(points, motion, frame_index):
    if motion == "ken_burns_out":
        zoom = max(1, 1.14 - frame_index * 0.0005)
    elif motion in {"pan_left", "pan_right"}:
        zoom = 1.1
    elif motion == "focus_product":
        zoom = min(1.18, 1 + frame_index * 0.00075)
    else:
        zoom = min(1.14, 1 + frame_index * 0.0005)
    focus_x = (
        min(1, frame_index / 240)
        if motion == "pan_left"
        else max(0, 1 - frame_index / 240)
        if motion == "pan_right"
        else 0.5
    )
    scale = 1 + (zoom - 1) * np.array([1080 / 1079, 1920 / 1919])
    shift = -(zoom - 1) * np.array([focus_x * 1080, 0.48 * 1920])
    return points * scale + shift


def _encoded_geometric_steps(video, motion):
    """Track the same source corners; aggregate only within fixed quadrants."""
    probe = shutil.which("ffprobe")
    assert probe
    metadata = json.loads(
        subprocess.check_output(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time:stream=width,height,r_frame_rate",
                "-of",
                "json",
                str(video),
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )
    assert metadata["streams"][0] == {
        "width": 1080,
        "height": 1920,
        "r_frame_rate": "30/1",
    }
    assert np.allclose(
        [float(frame["best_effort_timestamp_time"]) for frame in metadata["frames"]],
        np.arange(180) / 30,
        atol=0.000001,
    )
    _ffmpeg("-xerror", "-i", str(video), "-map", "0:v:0", "-an", "-f", "null", "-")
    cv2.setNumThreads(1)
    capture = cv2.VideoCapture(str(video))
    ok, first = capture.read()
    assert ok
    previous = cv2.cvtColor(first, cv2.COLOR_BGR2GRAY)[400:1350, 100:980]
    mask = np.zeros_like(previous)
    mask[100:850, 100:780] = 255
    tracked = cv2.goodFeaturesToTrack(previous, 160, 0.02, 15, mask=mask)
    assert tracked is not None and len(tracked) >= 100
    initial = tracked.reshape(-1, 2).astype(np.float64) + [100, 400]
    zero = _camera_geometry(np.zeros((1, 2)), motion, 0)[0]
    scale = _camera_geometry(np.ones((1, 2)), motion, 0)[0] - zero
    source = (initial - zero) / scale
    quadrants = (source[:, 0] > 540).astype(int) + 2 * (source[:, 1] > 921.6)
    measured_steps, ideal_steps = [[] for _ in range(4)], [[] for _ in range(4)]
    feature_error, counts = [], []
    for index in range(1, 180):
        ok, frame = capture.read()
        assert ok
        current = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[400:1350, 100:980]
        next_points, status, _ = cv2.calcOpticalFlowPyrLK(
            previous, current, tracked, None
        )
        assert next_points is not None and status is not None
        good = status.ravel().astype(bool) & np.isfinite(next_points).all(axis=(1, 2))
        actual = next_points[good].reshape(-1, 2) - tracked[good].reshape(-1, 2)
        source, quadrants = source[good], quadrants[good]
        ideal = _camera_geometry(source, motion, index) - _camera_geometry(
            source, motion, index - 1
        )
        feature_error.extend(np.linalg.norm(actual - ideal, axis=1).tolist())
        counts.append(len(source))
        assert len(source) >= 100
        for quadrant in range(4):
            selected = quadrants == quadrant
            assert selected.sum() >= 10
            measured_steps[quadrant].append(actual[selected].mean(axis=0))
            ideal_steps[quadrant].append(ideal[selected].mean(axis=0))
        previous, tracked = current, next_points[good]
    ok, _ = capture.read()
    assert not ok
    capture.release()
    actual, ideal = np.array(measured_steps), np.array(ideal_steps)
    eligible = np.abs(ideal) >= 0.05
    ratios = actual[eligible] / ideal[eligible]
    all_ratios = np.divide(
        actual, ideal, out=np.full_like(actual, np.nan), where=eligible
    )
    deviations = [
        {
            "quadrant": int(q),
            "pair_end_frame": int(index + 1),
            "axis": "xy"[axis],
            "ideal_step_px": float(ideal[q, index, axis]),
            "measured_step_px": float(actual[q, index, axis]),
            "ratio": float(all_ratios[q, index, axis]),
        }
        for q, index, axis in np.argwhere(
            eligible & ((all_ratios < 0.25) | (all_ratios > 1.75))
        )
    ]
    return {
        "minimum_tracked_corners": min(counts),
        "quadrant_pair_count": actual.shape[0] * actual.shape[1],
        "eligible_step_components": int(eligible.sum()),
        "reverse": int((ratios < 0).sum()),
        "dwell": int((ratios < 0.25).sum()),
        "hop": int((ratios > 1.75).sum()),
        "step_error_rmse": float(np.sqrt(np.mean((actual - ideal) ** 2))),
        "ratio_min_max": [float(ratios.min()), float(ratios.max())],
        "deviation_steps": deviations,
        "individual_feature_error_p50_p95_max": np.percentile(
            feature_error, [50, 95, 100]
        ).tolist(),
    }


@pytest.mark.parametrize(
    "motion",
    ["ken_burns_in", "ken_burns_out", "focus_product", "pan_left", "pan_right"],
)
def test_encoded_image_motion_retains_continuous_steps(tmp_path, motion):
    source = tmp_path / "known-grid.png"
    _grid_image(source)
    voice = tmp_path / "voice.wav"
    with wave.open(str(voice), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(48000)
        audio.writeframes(b"\0\0" * (48000 * 6))
    video, _ = MptMediaExecutor(tmp_path).render(
        video_path=tmp_path / "unused.mp4",
        voice_path=voice,
        narration="测试。",
        plan_data={
            "segments": [
                {
                    "asset_id": "analytic-bars",
                    "layout": "image_card",
                    "motion": motion,
                    "focus_x": 0.5,
                    "focus_y": 0.48,
                    "narration_start_sec": 0,
                    "narration_end_sec": 6,
                    "purpose": "Synthetic motion regression",
                }
            ]
        },
        scenes=[],
        assets={"analytic-bars": source},
    )
    measured = _encoded_geometric_steps(video, motion)
    assert measured["eligible_step_components"] >= 700
    assert measured["reverse"] == measured["dwell"] == measured["hop"] == 0, measured
    assert measured["step_error_rmse"] < 0.04, measured


@pytest.mark.parametrize(
    "motion",
    ["hold", "ken_burns_in", "ken_burns_out", "focus_product", "pan_left", "pan_right"],
)
@pytest.mark.parametrize("focus", [-2, 2])
def test_image_motion_keeps_edges_covered(tmp_path, motion, focus):
    source = tmp_path / "solid.png"
    Image.new("RGB", (1080, 1920), (180, 120, 80)).save(source)
    template = layout_filter("image_card", motion=motion, focus_x=focus, focus_y=focus)
    clamped = layout_filter(
        "image_card",
        motion=motion,
        focus_x=max(0, min(1, focus)),
        focus_y=max(0, min(1, focus)),
    )
    assert template == clamped
    # Exercise both camera caps; only retain five tiny full-image samples.
    raw = _ffmpeg(
        "-threads",
        "1",
        "-filter_complex_threads",
        "1",
        "-loop",
        "1",
        "-i",
        str(source),
        "-filter_complex",
        template
        + ",select='eq(n,0)+eq(n,179)+eq(n,240)+eq(n,280)+eq(n,299)',scale=18:32,format=rgb24[out]",
        "-map",
        "[out]",
        "-frames:v",
        "5",
        "-fps_mode",
        "passthrough",
        "-f",
        "rawvideo",
        "pipe:1",
    )
    pixels = np.frombuffer(raw, dtype=np.uint8).reshape(5, 32, 18, 3)
    assert np.max(np.abs(pixels.astype(int) - np.array((180, 120, 80)))) <= 3


def test_default_png_input_has_thirty_fps_timing(tmp_path):
    source = tmp_path / "solid.png"
    Image.new("RGB", (1080, 1920), "white").save(source)
    video = tmp_path / "short.mp4"
    _ffmpeg(
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-loop",
        "1",
        "-i",
        str(source),
        "-t",
        "0.367",
        "-vf",
        layout_filter("image_card", motion="ken_burns_in"),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        str(video),
    )
    probe = shutil.which("ffprobe")
    assert probe
    frames = json.loads(
        subprocess.check_output(
            [
                probe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "frame=best_effort_timestamp_time:stream=width,height,r_frame_rate",
                "-of",
                "json",
                str(video),
            ],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )
    assert frames["streams"][0] == {
        "width": 1080,
        "height": 1920,
        "r_frame_rate": "30/1",
    }
    timestamps = [
        float(frame["best_effort_timestamp_time"]) for frame in frames["frames"]
    ]
    assert len(timestamps) == 11
    assert np.allclose(timestamps, np.arange(11) / 30, atol=0.000001)
