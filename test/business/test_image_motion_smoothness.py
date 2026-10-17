import shutil
import subprocess

import numpy as np
import pytest
from PIL import Image

from glance_media.executor import layout_filter


def test_image_card_hold_does_not_upsample():
    assert "scale=4320:7680" not in layout_filter("image_card", motion="hold")


@pytest.mark.parametrize(
    "motion",
    ["pan_left", "pan_right", "ken_burns_in", "ken_burns_out", "focus_product"],
)
def test_image_card_motion_has_smooth_subpixel_steps(tmp_path, motion):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the image motion regression")

    image = Image.new("RGB", (1080, 1920), "white")
    image.paste("black", (200, 0, 400, 1920))
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
            "-vf",
            f"{layout_filter('image_card', motion=motion)},crop=1080:2:0:960,format=gray",
            "-frames:v",
            "60",
            "-threads",
            "1",
            "-filter_threads",
            "1",
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

    rows = (
        np.frombuffer(completed.stdout, dtype=np.uint8)
        .reshape(60, 2, 1080)
        .mean(axis=1)
    )
    dark = np.clip(120 - rows, 0, None)
    assert np.all(dark.sum(axis=1) > 0)
    centers = dark @ np.arange(1080) / dark.sum(axis=1)
    max_acceleration = np.abs(np.diff(centers, n=2)).max()
    assert max_acceleration <= 0.75, f"{motion} reached {max_acceleration:.3f}px/frame²"
