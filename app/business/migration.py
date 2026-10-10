"""Offline, fail-closed business-data merge into a new isolated staging directory.

Never accepts a writable live database destination. Raw source backup stays in memory;
only an allowlisted business projection and referenced, verified files are persisted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from glance_core.database import build_engine, create_db
from glance_core.settings import Settings
from glance_core.task_service import stable_hash
from sqlalchemy.dialects.sqlite import dialect
from sqlalchemy.schema import CreateTable
from sqlmodel import SQLModel

TABLES = ("source", "collection_run", "candidate", "candidate_signal", "content",
          "agent_task", "stage_run", "artifact", "agent_submission", "publication_record",
          "content_issue", "content_revision", "publication_preview", "audit_event")
ACTIVE = {"PENDING", "LEASED", "SUBMITTED"}
SECRET_KEY = re.compile(r"(?:tokens?|cookies?|passwords?|secrets?|authorizations?|credentials?|api.?keys?)$", re.I)
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
HANDLED = "mpt_migration_handled_task"


class MigrationError(ValueError):
    pass


@dataclass(frozen=True)
class Dataset:
    database: Path
    project_root: Path
    content_roots: tuple[Path, ...]
    submission_roots: tuple[Path, ...]


def _hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _read(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _json(path: Path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _secret_fields(value, prefix=""):
    if isinstance(value, dict):
        for key, item in value.items():
            field = f"{prefix}.{key}"
            if SECRET_KEY.search(key) and key != "lease_token_hash" and item not in (None, ""):
                yield field
            yield from _secret_fields(item, field)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _secret_fields(item, f"{prefix}[{index}]")


def _snapshot(dataset: Dataset, output: Path):
    """SQLite backup observes a consistent WAL snapshot; excluded data never reaches disk."""
    raw = sqlite3.connect(":memory:")
    with _read(dataset.database) as source:
        source.backup(raw)
    raw.row_factory = sqlite3.Row
    projection = sqlite3.connect(":memory:")
    try:
        for table in TABLES:
            model = SQLModel.metadata.tables[table]
            actual = {row[1] for row in raw.execute(f'PRAGMA table_info("{table}")')}
            expected = {column.name for column in model.columns}
            if actual != expected:
                raise MigrationError(f"Unsupported schema columns: {table}; no implicit schema conversion")
            projection.execute(str(CreateTable(model).compile(dialect=dialect())))
            columns = [column.name for column in model.columns]
            names = ",".join(f'"{name}"' for name in columns)
            for row in raw.execute(f'SELECT {names} FROM "{table}"'):
                for column in model.columns:
                    if column.type.__class__.__name__ == "JSON" and row[column.name]:
                        violations = list(_secret_fields(json.loads(row[column.name]), column.name))
                        if violations:
                            # Report identity/field names only, never the sensitive value.
                            raise MigrationError(f"Sensitive business JSON: {table}/{row['id']}/{violations[0]}")
                projection.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})', tuple(row))
        projection.commit()
        with sqlite3.connect(output) as destination:
            projection.backup(destination)
    finally:
        projection.close()
        raw.close()


def _resolve(value: str, dataset: Dataset, roots: tuple[Path, ...]) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = dataset.project_root / path
    path = path.resolve()
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise MigrationError("Referenced path escapes explicitly allowed roots")
    return path


def _validate_paused_handoff(value, snapshot):
    """Authorize preservation of exact queued inputs, never source freeze or execution."""
    if (not isinstance(value, dict) or set(value) != {"version", "source_snapshot_sha256", "reason", "tasks"}
            or value["version"] != 1 or value["source_snapshot_sha256"] != _hash(snapshot)
            or not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 1000
            or not isinstance(value["tasks"], list) or not value["tasks"]):
        raise MigrationError("Invalid paused handoff manifest or snapshot pin")
    seen = set()
    with _read(snapshot) as db:
        for item in value["tasks"]:
            if (not isinstance(item, dict) or set(item) != {"task_id", "content_id", "input_hash"}
                    or not isinstance(item["task_id"], str) or item["task_id"] in seen):
                raise MigrationError("Invalid paused handoff task identity")
            seen.add(item["task_id"])
            row = db.execute("SELECT * FROM agent_task WHERE id=?", (item["task_id"],)).fetchone()
            if (row is None or row["status"] != "PENDING" or row["content_id"] != item["content_id"]
                    or row["input_hash"] != item["input_hash"]
                    or row["input_hash"] != stable_hash(json.loads(row["input_data"] or "null"))):
                raise MigrationError("Paused handoff requires exact PENDING task, content and frozen input hash")
    return value


def _plan(dataset: Dataset, snapshot: Path, staging: Path, *, source_history: bool, include_quarantined_history=False,
          paused_handoff=None):
    blockers, warnings, files, quarantine, publications = [], [], {}, [], []
    if paused_handoff is not None:
        if not source_history:
            raise MigrationError("Paused handoff applies only to source history")
        _validate_paused_handoff(paused_handoff, snapshot)
    paused = {item["task_id"]: item for item in (paused_handoff or {}).get("tasks", [])}
    paused_contents = {item["content_id"] for item in paused.values() if item["content_id"]}
    with _read(snapshot) as db:
        counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in TABLES}
        contents = {row["id"]: dict(row) for row in db.execute("SELECT * FROM content")}
        artifacts = {row["id"]: dict(row) for row in db.execute("SELECT * FROM artifact")}
        tasks = [dict(row) for row in db.execute("SELECT * FROM agent_task")]
        task_by_id = {task["id"]: task for task in tasks}

        def issue(identity, reason, *, content_id=None, task_id=None, historical_reference=False):
            terminal = (contents.get(content_id, {}).get("business_status") in {"ARCHIVED", "PUBLISHED", "REJECTED"}
                        if content_id else task_by_id.get(task_id, {}).get("status") in {"COMPLETED", "FAILED", "CANCELLED", "REJECTED"})
            if source_history and ((include_quarantined_history and terminal)
                                   or (historical_reference and content_id in paused_contents)):
                quarantine.append({"identity": identity, "content_id": content_id, "task_id": task_id, "reason": str(reason)})
            else:
                blockers.append(f"{identity}: {reason}")

        roots = {}
        for key, row in contents.items():
            if not SAFE_ID.fullmatch(key):
                blockers.append(f"content/{key}: unsafe identifier")
                continue
            try:
                roots[key] = _resolve(row["content_dir"], dataset, dataset.content_roots)
            except MigrationError as exc:
                issue(f"content/{key}", exc, content_id=key)

        facts_cache = {}

        def facts(path):
            # Cache physical facts only. A failed old expected hash cannot poison a current reference.
            if path not in facts_cache:
                try:
                    if not path.is_file():
                        raise MigrationError("referenced file missing")
                    if path.name.lower().startswith(".env") or "cookie" in path.name.lower():
                        raise MigrationError("credential file cannot be staged")
                    semantic = None
                    if path.suffix.lower() == ".json":
                        value = json.loads(path.read_text(encoding="utf-8"))
                        if list(_secret_fields(value)):
                            raise MigrationError("sensitive referenced JSON cannot be staged")
                        semantic = stable_hash(value)
                    facts_cache[path] = (path.stat().st_size, _hash(path), semantic)
                except (MigrationError, OSError, ValueError) as exc:
                    facts_cache[path] = exc
            value = facts_cache[path]
            if isinstance(value, Exception):
                raise MigrationError(str(value))
            return value

        artifact_paths, valid_artifacts = {}, {}
        for key, row in artifacts.items():
            root = roots.get(row["content_id"])
            if root is not None:
                try:
                    path = _resolve(str(root / row["relative_path"]), dataset, (root,))
                    artifact_paths[key] = path
                    size, sha, _ = facts(path)
                    if size == row["size_bytes"] and sha == row["sha256"]:
                        valid_artifacts.setdefault(path, []).append(row)
                except (MigrationError, OSError, ValueError):
                    pass
        declared_artifact_paths = set(artifact_paths.values())

        def timing_voices(timing, content_id):
            return [row for row in artifacts.values() if row["content_id"] == content_id
                    and row["artifact_type"] == "voice" and row["sha256"] == timing.get("voice_sha256")
                    and row["id"] in artifact_paths
                    and row in valid_artifacts.get(artifact_paths[row["id"]], [])]

        current_paused_timing = {}
        for task_id in paused:
            task = task_by_id[task_id]
            payload = json.loads(task["input_data"] or "null")
            timing = payload.get("visual_timing_evidence") if isinstance(payload, dict) else None
            if isinstance(timing, dict):
                for voice in timing_voices(timing, task["content_id"]):
                    try:
                        path = _resolve(str(artifact_paths[voice["id"]].parent / "word_boundaries.json"), dataset,
                                        (roots[task["content_id"]],))
                        if facts(path)[1] == timing.get("boundaries_sha256"):
                            current_paused_timing.setdefault(task["content_id"], []).append((task, voice))
                    except (MigrationError, OSError, ValueError):
                        pass

        def superseded_timing(task, timing):
            if task["status"] not in {"COMPLETED", "CANCELLED", "FAILED", "REJECTED"}:
                return False
            for current_task, current_voice in current_paused_timing.get(task["content_id"], []):
                if current_task["created_at"] <= task["created_at"]:
                    continue
                for old in artifacts.values():
                    if (old["content_id"] == task["content_id"] and old["artifact_type"] == "voice"
                            and old["sha256"] == timing.get("voice_sha256") and old["created_at"] < current_voice["created_at"]
                            and artifact_paths.get(old["id"]) == artifact_paths[current_voice["id"]]):
                        return True
            return False

        def register(path, relative, *, size=None, sha=None, semantic_hash=None, identity, content_id=None, task_id=None,
                     historical_reference=False, context=False):
            try:
                path = path.resolve()
                actual_size, actual_sha, actual_semantic = facts(path)
                if context and path in declared_artifact_paths and path not in valid_artifacts:
                    raise MigrationError("no verified Artifact supports context file")
                if size is not None and actual_size != size or sha is not None and actual_sha != sha:
                    raise MigrationError("file size/hash mismatch")
                if semantic_hash and actual_semantic != semantic_hash:
                    raise MigrationError("submission semantic hash mismatch")
                destination = staging / "data" / relative
                if not destination.resolve().is_relative_to((staging / "data").resolve()):
                    raise MigrationError("destination escapes staging")
                entry = {"source": str(path), "destination": str(destination), "size": actual_size, "sha256": actual_sha}
                previous = files.get(str(destination))
                if previous and previous["sha256"] != actual_sha:
                    raise MigrationError("different source files collide at destination")
                files[str(destination)] = entry
            except (MigrationError, OSError, ValueError) as exc:
                issue(identity, exc, content_id=content_id, task_id=task_id, historical_reference=historical_reference)

        for key, row in artifacts.items():
            root = roots.get(row["content_id"])
            if root is None:
                issue(f"artifact/{key}", "content root unavailable", content_id=row["content_id"])
                continue
            try:
                path = _resolve(str(root / row["relative_path"]), dataset, (root,))
                superseded = any(current["artifact_type"] == row["artifact_type"] and current["content_id"] == row["content_id"]
                                 and current["created_at"] > row["created_at"] for current in valid_artifacts.get(path, []))
                register(path, Path("content") / row["content_id"] / row["relative_path"],
                         size=row["size_bytes"], sha=row["sha256"], identity=f"artifact/{key}", content_id=row["content_id"],
                         historical_reference=superseded)
            except MigrationError as exc:
                issue(f"artifact/{key}", exc, content_id=row["content_id"])
        submissions = [dict(row) for row in db.execute("SELECT * FROM agent_submission")]
        valid_submissions = {}
        for row in submissions:
            try:
                path = _resolve(row["raw_result_path"], dataset, dataset.submission_roots)
                task = task_by_id.get(row["task_id"], {})
                if (row["validation_status"] == "ACCEPTED" and facts(path)[2] == row["result_hash"]
                        and task.get("status") == "COMPLETED"
                        and row["result_hash"] == stable_hash(json.loads(task["result_data"] or "null"))):
                    valid_submissions.setdefault(path, []).append(row)
            except (MigrationError, OSError, ValueError):
                pass
        for row in submissions:
            try:
                path = _resolve(row["raw_result_path"], dataset, dataset.submission_roots)
                allowed = next(root.resolve() for root in dataset.submission_roots if path.is_relative_to(root.resolve()))
                superseded = row["validation_status"] == "REJECTED" and any(
                    current["task_id"] == row["task_id"] and current["attempt"] == row["attempt"]
                    and current["created_at"] > row["created_at"] for current in valid_submissions.get(path, []))
                register(path, Path("agent-submissions") / path.relative_to(allowed),
                         semantic_hash=row["result_hash"], identity=f"submission/{row['id']}",
                         task_id=row["task_id"], content_id=task_by_id.get(row["task_id"], {}).get("content_id"),
                         historical_reference=superseded)
            except MigrationError as exc:
                issue(f"submission/{row['id']}", exc, task_id=row["task_id"],
                      content_id=task_by_id.get(row["task_id"], {}).get("content_id"))
        for task in tasks:
            if source_history and task["status"] in ACTIVE:
                if task["id"] in paused and task["status"] == "PENDING":
                    quarantine.append({"identity": f"paused-handoff/{task['id']}", "content_id": task["content_id"],
                                       "task_id": task["id"], "reason": "Explicit staging-only paused handoff: " + paused_handoff["reason"],
                                       "paused": True, "input_hash": task["input_hash"]})
                else:
                    blockers.append(f"task/{task['id']}: active {task['status']}; source freeze/manual handoff required")
            if task["schema_version"] not in {"1", "2"} or task["task_type"] == "ASSET_GENERATE_VIDEO":
                issue(f"task/{task['id']}", "unsupported legacy schema/video; manual handoff required",
                      content_id=task["content_id"], task_id=task["id"])
            payload = json.loads(task["input_data"]) if task["input_data"] is not None else None
            if task["input_hash"] != stable_hash(payload):
                issue(f"task/{task['id']}", "frozen input hash mismatch (stable_hash contract); no payload/hash repair",
                      content_id=task["content_id"], task_id=task["id"])
            if not isinstance(payload, dict):
                issue(f"task/{task['id']}", "unsupported frozen input JSON shape", content_id=task["content_id"], task_id=task["id"])
                payload = {}
            context = payload.get("context_files", [])
            materialized = payload.get("materialized_context", {})
            context = context + list(materialized.values()) if isinstance(context, list) and isinstance(materialized, dict) else []
            root = roots.get(task["content_id"])
            if root:
                for value in context:
                    if not isinstance(value, str) or value.startswith(("http://", "https://")):
                        continue
                    try:
                        if Path(value).is_absolute():
                            raise MigrationError("frozen absolute context requires explicit handoff; payload cannot be rewritten")
                        path = _resolve(str(root / value), dataset, (root,))
                        register(path, Path("content") / task["content_id"] / value, identity=f"task/{task['id']}/context",
                                 content_id=task["content_id"], task_id=task["id"], context=True)
                    except MigrationError as exc:
                        issue(f"task/{task['id']}/context", exc, content_id=task["content_id"], task_id=task["id"])
            timing = payload.get("visual_timing_evidence")
            if isinstance(timing, dict):
                # Both legacy aliases and immutable voice directories use the sidecar adjacent to the frozen voice Artifact.
                voices = timing_voices(timing, task["content_id"])
                if not voices or not isinstance(timing.get("boundaries_sha256"), str):
                    issue(f"task/{task['id']}/timing", "frozen voice/timing reference unavailable",
                          content_id=task["content_id"], task_id=task["id"], historical_reference=superseded_timing(task, timing))
                else:
                    for voice in voices:
                        try:
                            path = _resolve(str(artifact_paths[voice["id"]].parent / "word_boundaries.json"), dataset, (root,))
                            register(path, Path("content") / task["content_id"] / Path(voice["relative_path"]).parent / "word_boundaries.json",
                                     sha=timing["boundaries_sha256"], identity=f"task/{task['id']}/timing",
                                     content_id=task["content_id"], task_id=task["id"])
                        except MigrationError as exc:
                            issue(f"task/{task['id']}/timing", exc, content_id=task["content_id"], task_id=task["id"])
        for row in db.execute("SELECT * FROM publication_record"):
            if row["status"] != "PUBLISHED":
                issue(f"publication/{row['id']}", "unresolved publication state; manual reconciliation required; never requeue",
                      content_id=row["content_id"])
                publications.append({"record_id": row["id"], "content_id": row["content_id"], "status": row["status"],
                                     "external_id": row["external_id"], "external_url": row["external_url"],
                                     "evidence_artifact_ids": [], "record_binding_verified": False})
                continue
            matching = []
            for task in tasks:
                result = json.loads(task["result_data"] or "{}")
                if (task["content_id"] == row["content_id"] and task["task_type"] == "DOUYIN_PUBLISH"
                        and task["status"] == "COMPLETED" and result.get("status") == "PUBLISHED"
                        and result.get("external_id") == row["external_id"] and result.get("external_url") == row["external_url"]):
                    proof = artifacts.get(result.get("evidence_artifact_id"))
                    if proof and proof["content_id"] == row["content_id"] and proof["artifact_type"] == "publish_evidence":
                        matching.append(proof["id"])
            if not row["external_id"] or not row["external_url"] or not matching:
                issue(f"publication/{row['id']}", "incomplete external ID/URL/task/evidence proof; quarantine", content_id=row["content_id"])
            publications.append({"record_id": row["id"], "content_id": row["content_id"], "status": row["status"],
                                 "external_id": row["external_id"], "external_url": row["external_url"],
                                 "evidence_artifact_ids": sorted(set(matching)),
                                 "record_binding_verified": bool(row["external_id"] and row["external_url"] and matching)})
        if source_history and db.execute("SELECT 1 FROM audit_event WHERE action='mpt_task_owned' LIMIT 1").fetchone():
            blockers.append("source audit contains target-only ownership marker; explicit provenance review required")
        fk = list(db.execute("PRAGMA foreign_key_check"))
        blockers.extend(f"foreign-key violation: {tuple(row)}" for row in fk)
    required = sum(entry["size"] for entry in files.values())
    free = shutil.disk_usage(staging).free
    # Copy plus preserved baseline/snapshots and reserve. No best-effort partial migration.
    if required + snapshot.stat().st_size * 3 + 256 * 1024 * 1024 > free:
        blockers.append("insufficient disk space for referenced files, backup and reserve")
    return {"counts": counts, "blockers": sorted(set(blockers)), "warnings": sorted(set(warnings)), "quarantine": quarantine,
            "files": list(files.values()), "publication_bindings": publications,
            "required_file_bytes": required, "free_bytes": free,
            "snapshot_sha256": _hash(snapshot), "status": "BLOCKED" if blockers else "READY"}


def _contained(staging: Path, path: Path):
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(staging.resolve()):
        raise MigrationError("Staging path was replaced or redirected")


def _copy_files(plan, staging):
    for entry in plan["files"]:
        source, target = Path(entry["source"]), Path(entry["destination"])
        _contained(staging / "data", target)
        if _hash(source) != entry["sha256"] or source.stat().st_size != entry["size"]:
            raise MigrationError("Source file changed since immutable snapshot inventory")
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if _hash(target) != entry["sha256"]:
                raise MigrationError("Staged file collision; existing file preserved")
            continue
        temporary = target.with_name(target.name + ".migration.tmp")
        shutil.copyfile(source, temporary)
        if _hash(temporary) != entry["sha256"]:
            raise MigrationError("Copied file failed hash validation")
        temporary.replace(target)


def _rebased_rows(snapshot: Path, staging: Path, dataset: Dataset):
    with _read(snapshot) as db:
        for table in TABLES:
            for row in db.execute(f'SELECT * FROM "{table}"'):
                value = dict(row)
                if table == "content":
                    value["content_dir"] = str((staging / "data" / "content" / value["id"]).resolve())
                elif table == "agent_submission":
                    try:
                        path = _resolve(value["raw_result_path"], dataset, dataset.submission_roots)
                        root = next(root.resolve() for root in dataset.submission_roots if path.is_relative_to(root.resolve()))
                        relative = path.relative_to(root)
                    except MigrationError:
                        # Explicit terminal quarantine retains the row without pointing back into production.
                        relative = Path("quarantined") / (hashlib.sha256(value["id"].encode()).hexdigest() + ".unavailable.json")
                    value["raw_result_path"] = str((staging / "data" / "agent-submissions" / relative).resolve())
                yield table, value


def _audit_rows(snapshot: Path, quarantine, created_at):
    snapshot_sha256 = _hash(snapshot)
    with _read(snapshot) as source:
        for task in source.execute("SELECT id,task_type,result_data FROM agent_task WHERE status='COMPLETED'"):
            digest = stable_hash(json.loads(task["result_data"] or "null"))
            action = "mpt_discovery_consumed" if task["task_type"] == "SOURCE_DISCOVERY" else "mpt_agent_consumed"
            for kind in (HANDLED, action):
                marker_id = "aud_" + hashlib.sha256((snapshot_sha256 + task["id"] + kind).encode()).hexdigest()[:32]
                yield {"id": marker_id, "actor_type": "system", "actor_id": "offline-migration", "entity_type": "agent_task",
                       "entity_id": task["id"], "action": kind,
                       "metadata_json": json.dumps({"result_hash": digest, "historical": True, "snapshot_sha256": snapshot_sha256}),
                       "created_at": created_at}
    for item in quarantine:
        entity_type = "content" if item["content_id"] else "agent_task"
        entity_id = item["content_id"] or item["task_id"]
        action = "mpt_migration_quarantined_content" if item["content_id"] else "mpt_migration_quarantined_task"
        marker_id = "aud_" + stable_hash({"snapshot": snapshot_sha256, "item": item})[:32]
        metadata = {"verified": False, "historical": True, "snapshot_sha256": snapshot_sha256,
                    "identity": item["identity"], "reason": item["reason"]}
        if item.get("paused"):
            metadata.update(paused=True, task_id=item["task_id"], content_id=item["content_id"],
                            input_hash=item["input_hash"], cutover_authorized=False)
        event = {"id": marker_id, "actor_type": "system", "actor_id": "offline-migration", "entity_type": entity_type,
                 "entity_id": entity_id, "action": action, "metadata_json": json.dumps(metadata), "created_at": created_at}
        yield event
        if item.get("paused") and item["content_id"]:
            # Guard both the content lifecycle and the exact pending queue entry, without a fake ownership/consumption receipt.
            yield {**event, "id": "aud_" + stable_hash({"parent": marker_id, "task_id": item["task_id"]})[:32],
                   "entity_type": "agent_task", "entity_id": item["task_id"], "action": "mpt_migration_quarantined_task"}


def _merge(database: Path, snapshot: Path, staging: Path, dataset: Dataset, *, historical: bool, quarantine=(),
           created_at=None, before_hashes=None, after_hash=None, verify_integrity=None):
    inserted = {table: 0 for table in TABLES}
    _contained(staging, database)
    with sqlite3.connect(database) as target:
        target.row_factory = sqlite3.Row
        target.execute("PRAGMA foreign_keys=ON")
        target.execute("BEGIN IMMEDIATE")
        # Check under the same SQLite write lock as insertion; a stale receipt cannot accept external edits.
        if before_hashes is not None and stable_hash(_logical_state(target)) not in before_hashes:
            raise MigrationError("External target drift from prepared baseline/expected merge")
        for table, row in _rebased_rows(snapshot, staging, dataset):
            existing = target.execute(f'SELECT * FROM "{table}" WHERE id=?', (row["id"],)).fetchone()
            if existing is not None:
                if dict(existing) != row:
                    raise MigrationError(f"Conflicting existing identity: {table}/{row['id']}; no overwrite")
                continue
            names = ",".join(f'"{key}"' for key in row)
            try:
                target.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
            except sqlite3.IntegrityError as exc:
                raise MigrationError(f"Unique/relation collision: {table}/{row['id']}") from exc
            inserted[table] += 1
        if historical:
            for row in _audit_rows(snapshot, quarantine, created_at):
                names = ",".join(f'"{key}"' for key in row)
                target.execute(f'INSERT OR IGNORE INTO audit_event ({names}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        if list(target.execute("PRAGMA foreign_key_check")):
            raise MigrationError("Merged foreign-key validation failed")
        if after_hash is not None and stable_hash(_logical_state(target)) != after_hash:
            raise MigrationError("Merged logical rows differ from immutable expected lineage")
        if verify_integrity:
            verify_integrity()
    return inserted


def prepare(source: Dataset, staging: Path, *, baseline: Dataset | None = None, include_quarantined_history=False,
            paused_handoff: Path | None = None):
    staging = staging.resolve()
    if staging.exists():
        raise MigrationError("Prepare requires a new independent staging directory")
    for dataset in (source, baseline):
        if dataset and any(staging.is_relative_to(root.resolve()) or root.resolve().is_relative_to(staging)
                           for root in (*dataset.content_roots, *dataset.submission_roots, dataset.database.parent)):
            raise MigrationError("Staging overlaps a source/baseline database or allowed file root")
    staging.mkdir(parents=True)
    marker = {"version": 2, "stage_id": uuid4().hex, "prepared_at": datetime.now(UTC).isoformat(),
              "source": _dataset_json(source), "baseline": _dataset_json(baseline) if baseline else None}
    _json(staging / "stage.json", marker)
    snapshot = staging / "source-business.db"
    try:
        # Capture the target episode even when source active tasks later block the migration.
        baseline_plan = None
        if baseline:
            baseline_snapshot = staging / "baseline-business.db"
            _snapshot(baseline, baseline_snapshot)
            baseline_plan = _plan(baseline, baseline_snapshot, staging, source_history=False)
            _json(staging / "baseline-preflight.json", baseline_plan)
        _snapshot(source, snapshot)
        handoff = None
        if paused_handoff is not None:
            try:
                handoff = _validate_paused_handoff(json.loads(paused_handoff.read_text(encoding="utf-8")), snapshot)
            except (OSError, ValueError) as exc:
                raise MigrationError("Invalid paused handoff manifest: " + str(exc)) from exc
            _json(staging / "paused-handoff.json", handoff)
            marker["paused_handoff_sha256"] = _hash(staging / "paused-handoff.json")
        plan = _plan(source, snapshot, staging, source_history=True, include_quarantined_history=include_quarantined_history,
                     paused_handoff=handoff)
        if baseline_plan:
            plan["baseline_counts"] = baseline_plan["counts"]
            plan["baseline_snapshot_sha256"] = baseline_plan["snapshot_sha256"]
            plan["blockers"].extend("baseline: " + reason for reason in baseline_plan["blockers"])
            combined = {entry["destination"]: entry for entry in baseline_plan["files"]}
            for entry in plan["files"]:
                previous = combined.get(entry["destination"])
                if previous and previous["sha256"] != entry["sha256"]:
                    plan["blockers"].append("baseline/source referenced files collide; no overwrite")
                combined[entry["destination"]] = entry
            plan["combined_required_file_bytes"] = sum(entry["size"] for entry in combined.values())
            reserve = (snapshot.stat().st_size + baseline_snapshot.stat().st_size) * 3 + 256 * 1024 * 1024
            if plan["combined_required_file_bytes"] + reserve > plan["free_bytes"]:
                plan["blockers"].append("insufficient combined baseline/source disk space with backup reserve")
            plan["status"] = "BLOCKED" if plan["blockers"] else "READY"
        _json(staging / "preflight.json", plan)
        marker["source_snapshot_sha256"] = plan["snapshot_sha256"]
        marker["preflight_sha256"] = _hash(staging / "preflight.json")
        if baseline_plan:
            marker["baseline_snapshot_sha256"] = baseline_plan["snapshot_sha256"]
            marker["baseline_preflight_sha256"] = _hash(staging / "baseline-preflight.json")
        _json(staging / "stage.json", marker)
        if plan["blockers"]:
            return plan
        database = staging / "target.db"
        settings = Settings(_env_file=None, data_dir=staging / "data", database_url=f"sqlite:///{database.as_posix()}")
        engine = build_engine(settings)
        create_db(engine)
        engine.dispose()
        if baseline:
            _copy_files(baseline_plan, staging)
            _merge(database, staging / "baseline-business.db", staging, baseline, historical=False)
        with _read(database) as before, sqlite3.connect(staging / "rollback.db") as backup:
            before.backup(backup)
        marker["rollback_sha256"] = _hash(staging / "rollback.db")
        marker["initial_target_logical_hash"] = _logical_hash(staging / "rollback.db")
        _json(staging / "stage.json", marker)
        _copy_files(plan, staging)
        return plan
    except MigrationError as exc:
        _json(staging / "quarantine.json", {"status": "BLOCKED", "reason": str(exc)})
        raise


def _dataset_json(dataset):
    return {"database": str(dataset.database.resolve()), "project_root": str(dataset.project_root.resolve()),
            "content_roots": [str(path.resolve()) for path in dataset.content_roots],
            "submission_roots": [str(path.resolve()) for path in dataset.submission_roots]}


def _dataset(value):
    return Dataset(Path(value["database"]), Path(value["project_root"]),
                   tuple(Path(path) for path in value["content_roots"]), tuple(Path(path) for path in value["submission_roots"]))


def _verify_files(staging, inventory):
    for entry in inventory["files"]:
        path = Path(entry["destination"])
        _contained(staging / "data", path)
        if not path.is_file() or path.stat().st_size != entry["size"] or _hash(path) != entry["sha256"]:
            raise MigrationError("Staged source/baseline file integrity changed")


def _verified_baseline_inventory(staging, marker):
    if not marker["baseline"]:
        return None
    for name, key in (("baseline-business.db", "baseline_snapshot_sha256"),
                      ("baseline-preflight.json", "baseline_preflight_sha256")):
        path = staging / name
        _contained(staging, path)
        if not path.is_file() or _hash(path) != marker.get(key):
            raise MigrationError("Immutable baseline snapshot changed")
    inventory = json.loads((staging / "baseline-preflight.json").read_text(encoding="utf-8"))
    _verify_files(staging, inventory)
    return inventory


def _verify_stage(staging, marker, plan):
    for name, key in (("source-business.db", "source_snapshot_sha256"), ("preflight.json", "preflight_sha256"),
                      ("rollback.db", "rollback_sha256")):
        path = staging / name
        _contained(staging, path)
        if not path.is_file() or _hash(path) != marker.get(key):
            raise MigrationError("Immutable business snapshot changed or rollback backup changed")
    if marker.get("paused_handoff_sha256"):
        path = staging / "paused-handoff.json"
        _contained(staging, path)
        if not path.is_file() or _hash(path) != marker["paused_handoff_sha256"]:
            raise MigrationError("Pinned paused handoff changed")
        _validate_paused_handoff(json.loads(path.read_text(encoding="utf-8")), staging / "source-business.db")
    plans = [plan]
    _verify_files(staging, plan)
    baseline_inventory = _verified_baseline_inventory(staging, marker)
    if baseline_inventory:
        plans.append(baseline_inventory)
    if _logical_hash(staging / "rollback.db") != marker["initial_target_logical_hash"]:
        raise MigrationError("Prepared baseline logical hash drift")
    if marker["baseline"]:
        with _read(staging / "rollback.db") as baseline:
            for table, row in _rebased_rows(staging / "baseline-business.db", staging, _dataset(marker["baseline"])):
                actual = baseline.execute(f'SELECT * FROM "{table}" WHERE id=?', (row["id"],)).fetchone()
                if actual is None or dict(actual) != row:
                    raise MigrationError("Prepared baseline rows differ from immutable snapshot lineage")
    return plans


def _expected_merged_hash(staging, marker, plan, created_at):
    with _read(staging / "rollback.db") as baseline:
        expected = _logical_state(baseline)
    tables = expected["tables"]
    indexed = {table: {row["id"]: row for row in tables[table]} for table in TABLES}
    rows = _rebased_rows(staging / "source-business.db", staging, _dataset(marker["source"]))
    audits = (("audit_event", row) for row in _audit_rows(staging / "source-business.db", plan["quarantine"], created_at))
    from itertools import chain
    for table, row in chain(rows, audits):
        previous = indexed[table].get(row["id"])
        if previous is not None and previous != row:
            raise MigrationError(f"Conflicting existing identity: {table}/{row['id']}; no overwrite")
        indexed[table][row["id"]] = row
    for table, rows_by_id in indexed.items():
        tables[table] = sorted(rows_by_id.values(), key=stable_hash)
    return stable_hash(expected)


def apply(staging: Path):
    staging = staging.resolve()
    _contained(staging, staging / "target.db")
    _contained(staging, staging / "source-business.db")
    marker = json.loads((staging / "stage.json").read_text(encoding="utf-8"))
    plan = json.loads((staging / "preflight.json").read_text(encoding="utf-8"))
    if marker.get("version") != 2 or plan["status"] != "READY" or plan["blockers"]:
        raise MigrationError("Staging is quarantined; source freeze/evidence/manual handoff required")
    inventories = _verify_stage(staging, marker, plan)
    # A fixed audit time lets a committed import be recognized after a receipt-write crash,
    # while checking every expected row rather than trusting a mutable prior success report.
    created_at = datetime.fromisoformat(marker["prepared_at"]).replace(tzinfo=None).isoformat(sep=" ")
    expected_hash = _expected_merged_hash(staging, marker, plan, created_at)
    inserted = _merge(staging / "target.db", staging / "source-business.db", staging, _dataset(marker["source"]),
                      historical=True, quarantine=plan["quarantine"], created_at=created_at,
                      before_hashes={marker["initial_target_logical_hash"], expected_hash}, after_hash=expected_hash,
                      verify_integrity=lambda: _verify_stage(staging, marker, plan))
    with _read(staging / "target.db") as db:
        report = {"status": "QUARANTINED_STAGING" if plan["quarantine"] else "VERIFIED_STAGING_ONLY", "inserted": inserted,
                  "quarantine": plan["quarantine"], "cutover_authorized": False,
                  "counts": {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in TABLES},
                  "foreign_key_violations": [tuple(row) for row in db.execute("PRAGMA foreign_key_check")],
                  "publication_ids": [row[0] for row in db.execute("SELECT external_id FROM publication_record")],
                  "publication_bindings": plan["publication_bindings"],
                  "file_hashes_verified": len({entry["destination"] for inventory in inventories for entry in inventory["files"]}),
                  "logical_hash": expected_hash, "warnings": plan["warnings"]}
    _json(staging / "audit-report.json", report)
    return report


def restore_rehearsal(staging: Path):
    staging = staging.resolve()
    marker = json.loads((staging / "stage.json").read_text(encoding="utf-8"))
    if marker.get("version") != 2:
        raise MigrationError("Unsupported staging version; fresh preparation required")
    baseline = staging / "rollback.db"
    _contained(staging, baseline)
    if _hash(baseline) != marker["rollback_sha256"]:
        raise MigrationError("Rollback backup changed")
    _verified_baseline_inventory(staging, marker)
    restored = staging / "rollback-rehearsal.db"
    _contained(staging, restored)
    if restored.exists() or restored.is_symlink():
        raise MigrationError("Restore rehearsal destination already exists")
    with _read(baseline) as original, sqlite3.connect(restored) as destination:
        original.backup(destination)
    with _read(restored) as db:
        counts = {table: db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in TABLES}
        violations = list(db.execute("PRAGMA foreign_key_check"))
    if violations:
        raise MigrationError("Restored baseline violates foreign keys")
    if _logical_hash(baseline) != _logical_hash(restored):
        raise MigrationError("Restored baseline differs from pre-import rows")
    return {"status": "RESTORE_REHEARSAL_VERIFIED", "counts": counts,
            "logical_hash": _logical_hash(restored), "restored_database": str(restored)}


def _logical_state(db):
    schema = [dict(row) for row in db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]
    tables = {}
    for row in schema:
        if row["type"] == "table":
            name = row["name"].replace('"', '""')
            tables[row["name"]] = sorted((dict(item) for item in db.execute(f'SELECT * FROM "{name}"')), key=stable_hash)
    return {"schema": schema, "tables": tables}


def _logical_hash(database: Path):
    with _read(database) as db:
        return stable_hash(_logical_state(db))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply", "restore-rehearsal"])
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--include-quarantined-history", action="store_true")
    parser.add_argument("--paused-handoff", type=Path, help="Snapshot-pinned PENDING tasks preserved paused in staging only")
    parser.add_argument("--source-db", type=Path)
    parser.add_argument("--source-project", type=Path)
    parser.add_argument("--content-root", type=Path, action="append")
    parser.add_argument("--submission-root", type=Path, action="append")
    parser.add_argument("--baseline-db", type=Path)
    parser.add_argument("--baseline-project", type=Path)
    parser.add_argument("--baseline-content-root", type=Path, action="append")
    parser.add_argument("--baseline-submission-root", type=Path, action="append")
    arguments = parser.parse_args()
    if arguments.action == "prepare":
        if not all((arguments.source_db, arguments.source_project, arguments.content_root, arguments.submission_root)):
            parser.error("prepare requires explicit source database/project and exact allowed file roots")
        baseline_args = (arguments.baseline_db, arguments.baseline_project,
                         arguments.baseline_content_root, arguments.baseline_submission_root)
        if any(baseline_args) and not all(baseline_args):
            parser.error("baseline clone requires database/project and exact allowed file roots")
        baseline = Dataset(arguments.baseline_db, arguments.baseline_project,
                           tuple(arguments.baseline_content_root), tuple(arguments.baseline_submission_root)) if all(baseline_args) else None
        report = prepare(Dataset(arguments.source_db, arguments.source_project,
                                 tuple(arguments.content_root), tuple(arguments.submission_root)), arguments.staging,
                         baseline=baseline, include_quarantined_history=arguments.include_quarantined_history,
                         paused_handoff=arguments.paused_handoff)
    elif arguments.action == "apply":
        report = apply(arguments.staging)
    else:
        report = restore_rehearsal(arguments.staging)
    print(json.dumps({key: value for key, value in report.items() if key != "files"}, ensure_ascii=False, indent=2))
    return 0 if not report.get("blockers") else 2


if __name__ == "__main__":
    raise SystemExit(main())
