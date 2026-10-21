import hashlib
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


@pytest.mark.parametrize(("motion", "digest"), [
    ("hold", "a2fe4f85fc27ecc414f929d6232c5f000848ee54f151490f03e5d5a08c91fddb"),
    ("ken_burns_in", "c1b5c4457715b04b8b1cc822feec15dd87415f1985edd626351a9dd113f7b0e1"),
    ("ken_burns_out", "bb06a21a439ca6deeb2d6a57633225306977c9d7638ded5d857b641a8063a480"),
    ("pan_left", "25f04c5a2c0a8655a621461165036b7b0fe03b0463dd030a7f64d48e06a3bb68"),
    ("pan_right", "f3f0f15ff8b7e622bc633bb1fbea31bdaac4b15b8f6cb0d0e83b3ca3d59c7a22"),
    ("focus_product", "556f6db0a85e2c8d294b47ae9e7a5ef3533fd408bb4e4b4b335528f5effb61fb"),
])
def test_default_image_filter_is_byte_compatible(motion, digest):
    # Frozen from 61d7fb5, before adding native temporal/spatial sampling.
    assert hashlib.sha256(layout_filter("image_card", motion=motion).encode()).hexdigest() == digest


@pytest.mark.parametrize("fps", [True, 0, 25, 120, 30.0, "60"])
def test_layout_rejects_unsupported_sampling_fps(fps):
    with pytest.raises(ValueError, match="fps"):
        layout_filter("image_card", fps=fps)


@pytest.mark.parametrize("sampling", [True, 0, 3, 2.0, "2"])
def test_layout_rejects_unsupported_spatial_sampling(sampling):
    with pytest.raises(ValueError, match="spatial_sampling"):
        layout_filter("image_card", spatial_sampling=sampling)


def test_spatial_sampling_surrounds_only_the_dynamic_transform():
    assert layout_filter("image_card", motion="hold", fps=60, spatial_sampling=2) == layout_filter(
        "image_card", motion="hold", fps=60)
    filters = layout_filter("image_card", motion="pan_right", fps=60, spatial_sampling=2)
    assert filters.index("fps=60") < filters.index("scale=2160:3840") < filters.index("perspective=")
    assert filters.endswith("scale=1080:1920:flags=lanczos")


@pytest.mark.parametrize("motion", ["ken_burns_in", "ken_burns_out", "focus_product", "pan_left", "pan_right"])
def test_native_sixty_fps_has_new_half_steps_at_the_same_camera_speed(tmp_path, motion):
    source = tmp_path / "bars.png"
    image = Image.new("RGB", (1080, 1920), "white")
    image.paste("black", (200, 0, 400, 1920))
    image.paste("black", (0, 300, 1080, 500))
    image.save(source)
    rows = {}
    for fps in (30, 60):
        rows[fps] = _ffmpeg(
            "-threads", "1", "-filter_complex_threads", "1", "-loop", "1", "-i", str(source),
            "-filter_complex", _strip_graph(layout_filter("image_card", motion=motion, fps=fps, focus_y=.48)),
            "-map", "[out]", "-frames:v", str(fps + 1), "-threads", "1",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1")
    baseline, native = _centers(rows[30], 31), _centers(rows[60], 61)
    for old, new in zip(baseline, native):
        assert np.allclose(old, new[::2], atol=.001)  # Same real-time endpoints, including t=1s.
        old_step = np.polyfit(np.arange(31), old, 1)[0]
        new_step = np.polyfit(np.arange(61), new, 1)[0]
        assert abs(new_step - old_step / 2) < .001
    decoded = np.frombuffer(rows[60], dtype=np.uint8).reshape(61, 6000)
    assert all(not np.array_equal(decoded[index], decoded[index - 1]) for index in range(1, 61, 2))


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


def _encoded_geometric_steps(video, motion, fps=60):
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
        "r_frame_rate": f"{fps}/1",
    }
    assert np.allclose(
        [float(frame["best_effort_timestamp_time"]) for frame in metadata["frames"]],
        np.arange(6 * fps) / fps,
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
    for index in range(1, 6 * fps):
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
        ideal = _camera_geometry(source, motion, index * 30 / fps) - _camera_geometry(
            source, motion, (index - 1) * 30 / fps
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
    # Preserve the original minimum camera speed when the per-frame step is halved.
    eligible = np.abs(ideal) >= 0.05 * 30 / fps
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
