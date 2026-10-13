"""Generate complete, offline frame evidence for independent visual review."""

from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from PIL import Image, ImageDraw, ImageFont


class FrameEvidenceError(RuntimeError):
    """Raised when complete frame evidence cannot be generated safely."""


_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
_FRAME_PATTERN = "frame-%09d.png"
_FRAMES_PER_PAGE = 4
_CELL_SIZE = (540, 960)
_LABEL_HEIGHT = 30
_PAGE_SIZE = (1080, 1920)
_FFPROBE_TIMEOUT_SECONDS = 300
_FFMPEG_TIMEOUT_SECONDS = 1800


def _digest_file(path: Path, *, label: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise FrameEvidenceError(f"Could not hash {label}: {path}") from exc
    return digest.hexdigest()


def _resolve_input_path(video_path: Path) -> Path:
    try:
        resolved = Path(video_path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FrameEvidenceError(
            f"Input video path cannot be resolved: {video_path}"
        ) from exc
    if not resolved.is_file():
        raise FrameEvidenceError(f"Input video is not a regular file: {resolved}")
    return resolved


def _resolve_new_output_dir(output_dir: Path) -> Path:
    candidate = Path(output_dir).expanduser()
    if candidate.exists() or candidate.is_symlink():
        raise FrameEvidenceError(
            f"Output directory already exists; refusing to overwrite: {candidate}"
        )
    if candidate.name in {"", ".", ".."}:
        raise FrameEvidenceError(
            f"Output directory must name a new child directory: {candidate}"
        )
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise FrameEvidenceError(
            f"Output directory parent cannot be resolved: {candidate.parent}"
        ) from exc
    if not parent.is_dir():
        raise FrameEvidenceError(
            f"Output directory parent is not a directory: {parent}"
        )
    resolved = parent / candidate.name
    if resolved.exists() or resolved.is_symlink():
        raise FrameEvidenceError(
            f"Output directory already exists; refusing to overwrite: {resolved}"
        )
    return resolved


def _require_tool(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise FrameEvidenceError(
            f"Required media tool is not available on PATH: {name}"
        )
    return str(Path(executable).resolve())


def _run_tool(args: Sequence[str], *, timeout_seconds: int, purpose: str) -> str:
    tool = Path(args[0]).name
    try:
        completed = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise FrameEvidenceError(
            f"{tool} timed out after {timeout_seconds} seconds while {purpose}"
        ) from exc
    except OSError as exc:
        raise FrameEvidenceError(
            f"Could not run {tool} while {purpose}: {exc}"
        ) from exc
    if completed.returncode:
        details = (completed.stderr or "").strip()[-2000:]
        suffix = f": {details}" if details else ""
        raise FrameEvidenceError(
            f"{tool} failed with exit code {completed.returncode} while {purpose}{suffix}"
        )
    return completed.stdout


def _read_pts(video_path: Path, ffprobe: str) -> list[str]:
    args = [
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
        str(video_path),
    ]
    output = _run_tool(
        args,
        timeout_seconds=_FFPROBE_TIMEOUT_SECONDS,
        purpose="reading every decoded video-frame timestamp",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FrameEvidenceError(
            "ffprobe returned invalid JSON while reading frame timestamps"
        ) from exc
    frames = payload.get("frames")
    if not isinstance(frames, list) or not frames:
        raise FrameEvidenceError(
            "ffprobe returned no decoded frames for video stream v:0"
        )

    pts: list[str] = []
    for index, frame in enumerate(frames):
        value = (
            frame.get("best_effort_timestamp_time") if isinstance(frame, dict) else None
        )
        timestamp = str(value) if value is not None else ""
        try:
            seconds = float(timestamp)
        except ValueError as exc:
            raise FrameEvidenceError(
                f"ffprobe omitted a usable best_effort_timestamp_time for frame {index}"
            ) from exc
        if not timestamp.isascii() or not math.isfinite(seconds):
            raise FrameEvidenceError(
                f"ffprobe returned an invalid timestamp for frame {index}"
            )
        pts.append(timestamp)
    return pts


def _extract_frames(video_path: Path, frames_dir: Path, ffmpeg: str) -> list[Path]:
    output_pattern = str(frames_dir / _FRAME_PATTERN)
    args = [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-fps_mode",
        "passthrough",
        "-start_number",
        "0",
        "-c:v",
        "png",
        output_pattern,
    ]
    _run_tool(
        args,
        timeout_seconds=_FFMPEG_TIMEOUT_SECONDS,
        purpose="extracting every frame from video stream v:0 as PNG",
    )
    frames = sorted(frames_dir.glob("frame-*.png"))
    expected_names = [f"frame-{index:09d}.png" for index in range(len(frames))]
    if [path.name for path in frames] != expected_names:
        raise FrameEvidenceError(
            "FFmpeg frame outputs are not a contiguous zero-based sequence"
        )
    return frames


def _frame_metadata(
    frame_paths: list[Path], pts: list[str], output_dir: Path
) -> list[dict[str, Any]]:
    if len(frame_paths) != len(pts):
        raise FrameEvidenceError(
            f"Decoded frame count does not match ffprobe timestamps: "
            f"{len(frame_paths)} PNGs versus {len(pts)} timestamps"
        )

    frames: list[dict[str, Any]] = []
    for index, (path, timestamp) in enumerate(zip(frame_paths, pts, strict=True)):
        try:
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG":
                    raise FrameEvidenceError(
                        f"Extracted frame {index} is not a PNG: {path}"
                    )
                width, height = image.size
        except OSError as exc:
            raise FrameEvidenceError(
                f"Could not decode extracted frame {index}: {path}"
            ) from exc
        if width <= 0 or height <= 0:
            raise FrameEvidenceError(
                f"Extracted frame {index} has invalid native dimensions"
            )
        frames.append(
            {
                "index": index,
                "pts_seconds": timestamp,
                "native_width": width,
                "native_height": height,
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": _digest_file(path, label=f"frame {index}"),
            }
        )
    return frames


def _fit_contact_image(image: Image.Image) -> Image.Image:
    available_height = _CELL_SIZE[1] - _LABEL_HEIGHT
    scale = min(_CELL_SIZE[0] / image.width, available_height / image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def _write_contact_pages(
    frame_paths: list[Path], pts: list[str], output_dir: Path, pages_dir: Path
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    font = ImageFont.load_default(size=18)
    for page_index, start in enumerate(range(0, len(frame_paths), _FRAMES_PER_PAGE)):
        page = Image.new("RGB", _PAGE_SIZE, (20, 20, 20))
        draw = ImageDraw.Draw(page)
        indices = list(range(start, min(start + _FRAMES_PER_PAGE, len(frame_paths))))
        for slot, index in enumerate(indices):
            frame_path = frame_paths[index]
            left = (slot % 2) * _CELL_SIZE[0]
            top = (slot // 2) * _CELL_SIZE[1]
            with Image.open(frame_path) as source:
                tile_image = _fit_contact_image(source.convert("RGB"))
            image_area_top = top + _LABEL_HEIGHT
            paste_left = left + (_CELL_SIZE[0] - tile_image.width) // 2
            paste_top = (
                image_area_top
                + (_CELL_SIZE[1] - _LABEL_HEIGHT - tile_image.height) // 2
            )
            page.paste(tile_image, (paste_left, paste_top))
            draw.rectangle(
                (left, top, left + _CELL_SIZE[0] - 1, top + _LABEL_HEIGHT - 1),
                fill=(20, 20, 20),
            )
            draw.text(
                (left + 8, top + 5),
                f"frame={index:06d} pts={pts[index]}s",
                fill=(255, 255, 255),
                font=font,
            )

        path = pages_dir / f"page-{page_index:06d}.png"
        page.save(path, format="PNG", optimize=True)
        pages.append(
            {
                "page_index": page_index,
                "layout": "2x2",
                "frame_indices": indices,
                "path": path.relative_to(output_dir).as_posix(),
                "sha256": _digest_file(path, label=f"contact page {page_index}"),
            }
        )
    return pages


def generate_frame_evidence(
    video_path: Path,
    expected_sha256: str,
    output_dir: Path,
    *,
    timed: bool = False,
) -> Path:
    """Create a complete frame-by-frame evidence pack without making review judgments.

    ``output_dir`` must be a new directory whose parent already exists. On any
    failure, partial evidence may remain there, but no successful manifest is written.
    """
    if not isinstance(expected_sha256, str) or not _SHA256_RE.fullmatch(
        expected_sha256
    ):
        raise FrameEvidenceError(
            "expected_sha256 must be exactly 64 hexadecimal characters"
        )

    source = _resolve_input_path(video_path)
    destination = _resolve_new_output_dir(output_dir)
    expected_digest = expected_sha256.casefold()
    before_digest = _digest_file(source, label="input video before processing")
    if before_digest != expected_digest:
        raise FrameEvidenceError(
            f"Input video SHA-256 mismatch before processing: expected {expected_digest}, "
            f"got {before_digest}"
        )

    ffprobe = _require_tool("ffprobe")
    ffmpeg = _require_tool("ffmpeg")
    try:
        destination.mkdir(exist_ok=False)
        frames_dir = destination / "frames"
        pages_dir = destination / "pages"
        frames_dir.mkdir()
        pages_dir.mkdir()
    except OSError as exc:
        raise FrameEvidenceError(
            f"Could not create new evidence directory: {destination}"
        ) from exc

    pts = _read_pts(source, ffprobe)
    frame_paths = _extract_frames(source, frames_dir, ffmpeg)
    if len(frame_paths) != len(pts):
        raise FrameEvidenceError(
            f"Decoded frame count does not match ffprobe timestamps: "
            f"{len(frame_paths)} PNGs versus {len(pts)} timestamps"
        )
    frames = _frame_metadata(frame_paths, pts, destination)
    pages = _write_contact_pages(frame_paths, pts, destination, pages_dir)

    video_end = None
    if timed:
        output = _run_tool(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=start_time,duration", "-of", "json", str(source)],
            timeout_seconds=_FFPROBE_TIMEOUT_SECONDS, purpose="reading final video presentation end",
        )
        try:
            stream = json.loads(output)["streams"][0]
            video_end = float(stream.get("start_time", 0)) + float(stream["duration"])
            times = [float(value) for value in pts] + [video_end]
            if not math.isfinite(video_end) or any(right <= left for left, right in zip(times, times[1:])):
                raise ValueError("nonmonotonic presentation sequence")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise FrameEvidenceError("Video presentation duration or PTS is unavailable/invalid") from exc
        for frame, start, end in zip(frames, times, times[1:], strict=False):
            frame["display_duration_sec"] = end - start
            frame["display_end_sec"] = end

    after_digest = _digest_file(source, label="input video after processing")
    if after_digest != expected_digest:
        raise FrameEvidenceError(
            f"Input video SHA-256 changed during processing: expected {expected_digest}, "
            f"got {after_digest}"
        )

    manifest = {
        "version": 1,
        "method": "full_decoded_frame_sequence",
        "input_video": {
            "path": str(source),
            "expected_sha256": expected_digest,
            "sha256_before_processing": before_digest,
            "sha256_after_processing": after_digest,
        },
        "generation": {
            "video_stream": "v:0",
            "timestamp_field": "best_effort_timestamp_time",
            "ffmpeg_fps_mode": "passthrough",
            "frame_format": "PNG",
        },
        "review": {
            "observed_by_reviewer": False,
            "audio_review_performed": False,
        },
        "coverage": {
            "kind": "generated_frame_to_page_mapping",
            "description": (
                "Generation mapping only; this does not record semantic review, audio review, "
                "or QC."
            ),
            "frame_count": len(frames),
            "mapped_frame_count": sum(len(page["frame_indices"]) for page in pages),
            "mapping_complete": True,
        },
        "frames": frames,
        "pages": pages,
        "limitations": (
            "This pack does not replace independent frame-by-frame semantic review, "
            "audio review, or QC."
        ),
    }
    if timed:
        manifest.update(version=2, video_end_sec=video_end)
    manifest_path = destination / "manifest.json"
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, ensure_ascii=True, indent=2, allow_nan=False)
            stream.write("\n")
    except (OSError, TypeError, ValueError) as exc:
        raise FrameEvidenceError(
            f"Could not write evidence manifest: {manifest_path}"
        ) from exc
    return manifest_path
