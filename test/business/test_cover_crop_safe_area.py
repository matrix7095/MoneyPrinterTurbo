"""Actual rendered cover copy survives the platform's centered 3:4 preview."""

import shutil

import pytest
from PIL import Image, ImageChops

from glance_media.executor import MediaExecutor


@pytest.mark.parametrize("title", ["凳子第二种用法", "一张凳子的十二种新鲜用法"])
def test_rendered_title_survives_centered_portrait_crop(tmp_path, title):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg required to inspect rendered cover text")
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1920), "black").save(source)
    output = MediaExecutor(tmp_path).build_cover(source, title)
    with Image.open(output) as cover:
        assert cover.size == (1080, 1920)
        # 1080 x 1440 is the centered 3:4 view of the frozen 9:16 cover.
        bounds = ImageChops.difference(cover.convert("RGB"), Image.new("RGB", cover.size)).getbbox()
        assert bounds is not None, "The actual cover must contain rendered title text"
        assert 240 < bounds[1] < bounds[3] < 1680, bounds
        cropped = cover.crop((0, 240, 1080, 1680))
        crop_bounds = ImageChops.difference(cropped.convert("RGB"), Image.new("RGB", cropped.size)).getbbox()
        assert crop_bounds == (bounds[0], bounds[1] - 240, bounds[2], bounds[3] - 240)
