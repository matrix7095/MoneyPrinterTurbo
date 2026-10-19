from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

_REQUIRED_TASK_FILES = (
    ".lease.json",
    "task.json",
    "input.json",
    "result.schema.json",
    "instructions.md",
)

#: Finished workspaces without a live lease are deleted after this age.
WORKSPACE_MAX_AGE = timedelta(hours=24)
#: Hard cap on leftover task directories (active leased ones are never deleted).
WORKSPACE_MAX_COUNT = 50


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def write_text_atomic(path: Path, value: str) -> None:
    """Write a handoff text file without exposing a partial document."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def prepare_workspace(
    root: Path,
    *,
    bundle: dict[str, Any],
    lease_token: str,
    api_url: str,
    agent_id: str,
) -> Path:
    task_id = bundle["task"]["task_id"]
    directory = root / task_id
    directory.mkdir(parents=True, exist_ok=True)
    write_json_atomic(directory / "task.json", bundle["task"])
    write_json_atomic(directory / "input.json", bundle["input"])
    write_json_atomic(directory / "context_manifest.json", bundle.get("context_manifest", []) or [])
    write_json_atomic(directory / "result.schema.json", bundle["result_schema"])
    write_json_atomic(
        directory / ".lease.json",
        {
            "task_id": task_id,
            "lease_token": lease_token,
            "api_url": api_url,
            "agent_id": agent_id,
        },
    )
    write_text_atomic(directory / "instructions.md", bundle["instructions"])
    (directory / "context").mkdir(exist_ok=True)
    return directory


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_workspace(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def gc_workspaces(
    root: Path,
    *,
    keep_task_id: str | None = None,
    now: datetime | None = None,
    max_age: timedelta = WORKSPACE_MAX_AGE,
    max_count: int = WORKSPACE_MAX_COUNT,
) -> int:
    """Delete finished task directories. Never touch a live lease or ``keep_task_id``."""
    if not root.is_dir():
        return 0
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    inactive: list[tuple[float, Path]] = []
    active = 0
    for entry in root.iterdir():
        if not entry.is_dir() or not entry.name.startswith("agt_"):
            continue
        if keep_task_id and entry.name == keep_task_id:
            active += 1
            continue
        if (entry / ".lease.json").is_file():
            active += 1
            continue
        inactive.append((entry.stat().st_mtime, entry))
    inactive.sort(reverse=True)
    removed = 0
    kept: list[Path] = []
    for mtime, path in inactive:
        modified = datetime.fromtimestamp(mtime, tz=UTC)
        if current - modified > max_age:
            remove_workspace(path)
            removed += 1
            continue
        kept.append(path)
    overflow = active + len(kept) - max_count
    if overflow > 0:
        for path in reversed(kept):
            if overflow <= 0:
                break
            remove_workspace(path)
            removed += 1
            overflow -= 1
    return removed


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def task_directory(root: Path, task_id: str) -> Path:
    directory = (root / task_id).resolve()
    if root.resolve() not in directory.parents:
        raise ValueError("Task path escapes workspace root")
    if not directory.is_dir():
        raise FileNotFoundError(f"Unknown task workspace: {task_id}")
    missing = [name for name in _REQUIRED_TASK_FILES if not (directory / name).is_file()]
    if not (directory / "context").is_dir():
        missing.append("context/")
    if missing:
        raise FileNotFoundError(
            f"Unknown task workspace: {task_id}; missing handoff files: {', '.join(missing)}"
        )
    return directory


def _edit_distance_at_most(left: str, right: str, limit: int = 2) -> bool:
    """Bounded Levenshtein check used only for correcting a local task-id typo."""
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for row, left_char in enumerate(left, start=1):
        current = [row]
        row_minimum = row
        for column, right_char in enumerate(right, start=1):
            value = min(
                current[column - 1] + 1,
                previous[column] + 1,
                previous[column - 1] + (left_char != right_char),
            )
            current.append(value)
            row_minimum = min(row_minimum, value)
        if row_minimum > limit:
            return False
        previous = current
    return previous[-1] <= limit


def resolve_task_directory(root: Path, requested_task_id: str) -> tuple[Path, str]:
    """Resolve an exact workspace, or one unique nearby workspace for a host typo.

    Agent commands normally use the exact task ID. A failed exact lookup may also be
    caused by a single omitted/copied character in a long ID. We only correct when the
    candidate is a complete workspace, carries the same ``agt_`` namespace and there is
    exactly one match within a bounded edit distance. Ambiguous or malformed lookups
    retain the original failure and never guess a different task.
    """
    try:
        return task_directory(root, requested_task_id), requested_task_id
    except FileNotFoundError as original_error:
        root_path = root.expanduser().resolve()
        requested_path = (root_path / requested_task_id).resolve()
        if requested_path.is_dir() or not requested_task_id.startswith("agt_"):
            raise

        candidates: list[tuple[Path, str]] = []
        try:
            entries = tuple(root_path.iterdir())
        except OSError:
            raise original_error from None
        for entry in entries:
            if not entry.is_dir() or not entry.name.startswith("agt_"):
                continue
            try:
                metadata = load_json(entry / "task.json")
                actual_task_id = str(metadata.get("task_id") or entry.name)
                if actual_task_id != entry.name:
                    continue
                if (
                    len(actual_task_id) != len(requested_task_id)
                    and abs(len(actual_task_id) - len(requested_task_id)) > 2
                ):
                    continue
                if actual_task_id[:8] != requested_task_id[:8]:
                    continue
                if not _edit_distance_at_most(requested_task_id, actual_task_id):
                    continue
                complete_directory = task_directory(root_path, actual_task_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError, FileNotFoundError):
                continue
            candidates.append((complete_directory, actual_task_id))

        if len(candidates) == 1:
            return candidates[0]
        raise original_error
