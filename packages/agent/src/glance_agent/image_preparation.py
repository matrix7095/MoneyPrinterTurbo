"""Local, explicit dimension preparation; upload and visual QC remain separate."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import warnings
from fractions import Fraction
from pathlib import Path

from glance_agent.workspace import file_sha256, load_json, write_json_atomic


def _plain_path(path: Path) -> None:
    for entry in (path, *path.parents):
        try:
            attributes = entry.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attributes.st_mode) or (
            getattr(attributes, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise ValueError(f"Image preparation refuses redirected paths: {entry}")


def _dimensions(task_input: dict, segment_index: int) -> tuple[int, int]:
    if not isinstance(task_input, dict):
        raise ValueError("Frozen image input must be an object")
    requests = task_input.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("A frozen requests[] image contract is required")
    indexes = [entry.get("segment_index") for entry in requests if isinstance(entry, dict)]
    if len(indexes) != len(requests) or any(type(index) is not int or index < 0 for index in indexes):
        raise ValueError("Invalid frozen image segment indexes")
    if len(set(indexes)) != len(indexes) or segment_index not in indexes:
        raise ValueError("Image segment is missing or duplicated in frozen requests[]")
    request = requests[indexes.index(segment_index)].get("asset_request")
    if not isinstance(request, dict):
        raise ValueError("A frozen asset_request is required")
    minimum_width, minimum_height = request.get("min_width"), request.get("min_height")
    if any(type(value) is not int or not 0 < value <= 16384 for value in (minimum_width, minimum_height)):
        raise ValueError("Frozen image minimum dimensions must be integers in 1..16384")
    try:
        declared_ratio = request["aspect_ratio"]
        if not isinstance(declared_ratio, str) or len(declared_ratio) > 64 or not re.fullmatch(r"\d+(?:\.\d+)?:\d+(?:\.\d+)?", declared_ratio):
            raise ValueError
        left, right = declared_ratio.split(":")
        ratio = Fraction(left) / Fraction(right)
        if ratio <= 0:
            raise ValueError
    except (KeyError, AttributeError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("Frozen image aspect_ratio must contain positive numbers") from exc
    scale = max(math.ceil(minimum_width / ratio.numerator), math.ceil(minimum_height / ratio.denominator))
    width, height = ratio.numerator * scale, ratio.denominator * scale
    if max(width, height) > 16384 or width * height > 64_000_000:
        raise ValueError("Exact frozen aspect ratio exceeds safe image preparation dimensions")
    return width, height


def prepare_image(directory: Path, task_id: str, agent_id: str, source: Path, segment_index: int) -> dict:
    """Keep immutable image evidence outside the disposable leased task workspace."""
    directory = directory.expanduser().absolute()
    _plain_path(directory)
    if not re.fullmatch(r"agt_[A-Za-z0-9_-]+", task_id) or directory.name != task_id:
        raise ValueError("Image preparation requires this exact task workspace")
    for name in ("task.json", "input.json", ".lease.json"):
        _plain_path(directory / name)
    task, lease = load_json(directory / "task.json"), load_json(directory / ".lease.json")
    if not isinstance(task, dict) or task.get("task_id") != task_id or task.get("task_type") != "asset_generate_image":
        raise ValueError("Image preparation requires this exact asset_generate_image task")
    if not isinstance(lease, dict) or lease.get("task_id") != task_id or lease.get("agent_id") != agent_id or not lease.get("lease_token"):
        raise ValueError("Image preparation requires this agent's local task lease")
    if type(segment_index) is not int or segment_index < 0:
        raise ValueError("Image segment index must be a non-negative integer")
    frozen_input = (directory / "input.json").read_bytes()
    width, height = _dimensions(json.loads(frozen_input), segment_index)
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Pillow is unavailable; run prepare-image in the installed MoneyPrinterTurbo environment") from exc

    source = source.expanduser().absolute()
    _plain_path(source)
    if not source.is_file() or source.stat().st_size > 25 * 1024 * 1024:
        raise ValueError("Image source must be a regular file no larger than 25 MiB")
    original = source.read_bytes()
    if len(original) > 25 * 1024 * 1024:
        raise ValueError("Image source exceeds the 25 MiB upload limit")
    source_hash = hashlib.sha256(original).hexdigest()
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(original)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1:
                    raise ValueError("Image source must be a single-frame PNG, JPEG or WebP")
                original_suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
                if image.width * image.height > 64_000_000:
                    raise ValueError("Image source exceeds safe decoding dimensions")
                image.load()
                oriented = ImageOps.exif_transpose(image)
                original_size = image.size
                oriented_size = oriented.size
                if abs((oriented.width / oriented.height) / (width / height) - 1) > 0.02:
                    raise ValueError("Source aspect ratio differs by more than 2%; regenerate rather than crop or stretch")
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                resized = ImageOps.pad(
                    oriented.convert("RGBA" if alpha else "RGB"), (width, height),
                    method=Image.Resampling.LANCZOS, color=(0, 0, 0, 0) if alpha else "white",
                )
                encoded = io.BytesIO()
                resized.save(encoded, format="PNG")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError("Image source exceeds safe decoding dimensions") from exc
        except OSError as exc:
            raise ValueError("Image source could not be decoded") from exc
    prepared_bytes = encoded.getvalue()
    if len(prepared_bytes) > 25 * 1024 * 1024:
        raise ValueError("Prepared PNG exceeds the 25 MiB upload limit")
    input_hash = hashlib.sha256(frozen_input).hexdigest()
    key = hashlib.sha256(f"{source_hash}:{input_hash}:{segment_index}".encode()).hexdigest()
    # submit/fail/GC delete the entire leased workspace, including its credentials.
    workspace_root = directory.parent
    root = workspace_root.with_name(f"{workspace_root.name}-image-preparation-evidence") / task_id
    _plain_path(root)
    root.mkdir(parents=True, exist_ok=True)
    _plain_path(root)
    destination = root / key
    _plain_path(destination)
    target, receipt = destination / "image.png", destination / "preparation.json"
    retained_source = destination / f"original{original_suffix}"
    metadata = {
        "status": "PREPARED", "task_id": task_id, "segment_index": segment_index,
        "path": str(target), "width": width, "height": height,
        "source_sha256": source_hash, "source_width": original_size[0], "source_height": original_size[1],
        "source_path": str(retained_source),
        "input_sha256": input_hash, "sha256": hashlib.sha256(prepared_bytes).hexdigest(),
        "method": "Pillow EXIF orientation, LANCZOS proportional resize with centered padding; no added detail or visual QC",
        "upscaled": width > oriented_size[0] or height > oriented_size[1],
    }
    try:
        destination.mkdir()
    except FileExistsError:
        for path in (target, receipt, retained_source):
            _plain_path(path)
        if not target.is_file() or not receipt.is_file() or not retained_source.is_file() or load_json(receipt) != metadata or file_sha256(target) != metadata["sha256"] or file_sha256(retained_source) != source_hash:
            raise ValueError("Existing prepared image is incomplete or changed; refusing to overwrite") from None
    else:
        with retained_source.open("xb") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        with target.open("xb") as handle:
            handle.write(prepared_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        write_json_atomic(receipt, metadata)
    with Image.open(target) as actual:
        actual.load()
        if actual.size != (width, height) or actual.format != "PNG":
            raise ValueError("Prepared image does not match its actual decoded dimensions")
    return metadata
