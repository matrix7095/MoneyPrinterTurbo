"""The offline frame evidence pack preserves the complete decoded sequence."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from glance_media import frame_evidence


@pytest.fixture
def synthetic_clip(tmp_path: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for the synthetic clip")

    source_dir = tmp_path / "source-frames"
    source_dir.mkdir()
    for index in range(7):
        color = (20, 220, 20) if index == 3 else (245, 245, 245)
        Image.new("RGB", (320, 240), color).save(source_dir / f"source-{index:02d}.png")

    video = tmp_path / "synthetic-4x3.mp4"
    result = subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-v",
            "error",
            "-y",
            "-threads",
            "1",
            "-framerate",
            "7",
            "-i",
            str(source_dir / "source-%02d.png"),
            "-frames:v",
            "7",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    return video


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _probe_pts(video: Path) -> list[str]:
    ffprobe = shutil.which("ffprobe")
    assert ffprobe is not None
    result = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(video),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        shell=False,
        stdin=subprocess.DEVNULL,
    )
    return [
        item["best_effort_timestamp_time"]
        for item in json.loads(result.stdout)["frames"]
    ]


def test_generates_full_sequence_hashes_and_ordered_contact_pages(
    synthetic_clip: Path, tmp_path: Path
) -> None:
    expected_sha = _sha256(synthetic_clip)
    manifest_path = frame_evidence.generate_frame_evidence(
        synthetic_clip, expected_sha, tmp_path / "evidence"
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    frames = manifest["frames"]
    pages = manifest["pages"]
    assert manifest["version"] == 1
    assert manifest["method"] == "full_decoded_frame_sequence"
    assert manifest["input_video"]["sha256_before_processing"] == expected_sha
    assert manifest["input_video"]["sha256_after_processing"] == expected_sha
    assert manifest["review"] == {
        "observed_by_reviewer": False,
        "audio_review_performed": False,
    }
    assert manifest["coverage"]["kind"] == "generated_frame_to_page_mapping"
    assert manifest["coverage"]["mapping_complete"] is True
    assert "quality_passed" not in manifest

    expected_pts = _probe_pts(synthetic_clip)
    assert [frame["index"] for frame in frames] == list(range(7))
    assert [frame["pts_seconds"] for frame in frames] == expected_pts
    for frame in frames:
        assert (frame["native_width"], frame["native_height"]) == (320, 240)
        image_path = manifest_path.parent / frame["path"]
        with Image.open(image_path) as image:
            assert image.size == (320, 240)
        assert _sha256(image_path) == frame["sha256"]

    assert [page["frame_indices"] for page in pages] == [[0, 1, 2, 3], [4, 5, 6]]
    assert all(page["layout"] == "2x2" for page in pages)
    mapped = [index for page in pages for index in page["frame_indices"]]
    assert mapped == list(range(7))
    assert len(set(mapped)) == 7
    for page in pages:
        page_path = manifest_path.parent / page["path"]
        with Image.open(page_path) as image:
            assert image.size == (1080, 1920)
        assert _sha256(page_path) == page["sha256"]

    # The injected green anomaly remains present at its original sequence index.
    anomaly_path = manifest_path.parent / frames[3]["path"]
    with Image.open(anomaly_path) as image:
        red, green, blue = image.convert("RGB").getpixel((160, 120))
    assert green > red * 1.5 and green > blue * 1.3

    # A 4:3 source stays 4:3 in its 2x2 contact-page cell.
    with Image.open(manifest_path.parent / pages[0]["path"]) as page:
        assert page.getpixel((10, 200)) == (20, 20, 20)
        assert min(page.getpixel((10, 500))) > 220
        assert page.getpixel((10, 800)) == (20, 20, 20)


def test_wrong_expected_hash_is_rejected_before_tools_or_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not processed")
    output = tmp_path / "evidence"

    def forbidden_run(*args, **kwargs):
        pytest.fail("a hash mismatch must be rejected before invoking media tools")

    monkeypatch.setattr(frame_evidence.subprocess, "run", forbidden_run)
    with pytest.raises(frame_evidence.FrameEvidenceError, match="SHA-256 mismatch"):
        frame_evidence.generate_frame_evidence(video, "0" * 64, output)
    assert not output.exists()


def test_existing_output_directory_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "evidence"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    def forbidden_run(*args, **kwargs):
        pytest.fail(
            "an existing evidence directory must be rejected before media tools"
        )

    monkeypatch.setattr(frame_evidence.subprocess, "run", forbidden_run)
    with pytest.raises(frame_evidence.FrameEvidenceError, match="already exists"):
        frame_evidence.generate_frame_evidence(video, "0" * 64, output)
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("failing_tool", ["ffprobe", "ffmpeg"])
def test_tool_failure_never_writes_success_manifest(
    synthetic_clip: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_tool: str,
) -> None:
    actual_run = subprocess.run

    def fail_selected_tool(args, **kwargs):
        if Path(args[0]).stem.casefold() == failing_tool:
            return subprocess.CompletedProcess(
                args, 1, "", f"{failing_tool} injected failure"
            )
        return actual_run(args, **kwargs)

    monkeypatch.setattr(frame_evidence.subprocess, "run", fail_selected_tool)
    output = tmp_path / f"{failing_tool}-failure"
    with pytest.raises(
        frame_evidence.FrameEvidenceError, match="failed with exit code"
    ):
        frame_evidence.generate_frame_evidence(
            synthetic_clip, _sha256(synthetic_clip), output
        )
    assert output.is_dir()
    assert not (output / "manifest.json").exists()


def test_extracted_count_mismatch_never_writes_success_manifest(
    synthetic_clip: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_run = subprocess.run

    def drop_one_extracted_frame(args, **kwargs):
        result = actual_run(args, **kwargs)
        if Path(args[0]).stem.casefold() == "ffmpeg":
            last_frame = Path(args[-1].replace("%09d", "000000006"))
            last_frame.unlink()
        return result

    monkeypatch.setattr(frame_evidence.subprocess, "run", drop_one_extracted_frame)
    output = tmp_path / "count-mismatch"
    with pytest.raises(
        frame_evidence.FrameEvidenceError, match="frame count does not match"
    ):
        frame_evidence.generate_frame_evidence(
            synthetic_clip, _sha256(synthetic_clip), output
        )
    assert not (output / "manifest.json").exists()


def test_input_replacement_during_processing_is_rejected(
    synthetic_clip: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_run = subprocess.run

    def replace_input_after_decode(args, **kwargs):
        result = actual_run(args, **kwargs)
        if Path(args[0]).stem.casefold() == "ffmpeg":
            synthetic_clip.write_bytes(
                synthetic_clip.read_bytes() + b"changed during processing"
            )
        return result

    monkeypatch.setattr(frame_evidence.subprocess, "run", replace_input_after_decode)
    output = tmp_path / "changed-input"
    original_sha = _sha256(synthetic_clip)
    with pytest.raises(
        frame_evidence.FrameEvidenceError, match="changed during processing"
    ):
        frame_evidence.generate_frame_evidence(synthetic_clip, original_sha, output)
    assert not (output / "manifest.json").exists()
