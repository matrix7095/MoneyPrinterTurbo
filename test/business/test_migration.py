"""Offline migration, collision rollback, history quarantine and WAL snapshot contracts."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from glance_core.database import build_engine, create_db
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, Candidate, CandidateStatus, Content, ContentStatus,
    PublicationMode, PublicationRecord, RightsStatus, RuntimeSetting, Source, SourceType, TaskStatus, TaskType,
)
from glance_core.settings import Settings
from glance_core.task_service import stable_hash
from PIL import Image
from sqlmodel import Session

from app.business.migration import Dataset, MigrationError, _hash, _logical_hash, _read, _snapshot, apply, prepare, restore_rehearsal


@pytest.fixture
def datasets(tmp_path):
    def create(name, *, live=False):
        project = tmp_path / name
        data = project / "data"
        db = data / "database" / "app.db"
        settings = Settings(_env_file=None, data_dir=data, database_url=f"sqlite:///{db.as_posix()}")
        engine = build_engine(settings)
        create_db(engine)
        with Session(engine) as session:
            source = Source(id=f"src_{name}", name=name, source_type=SourceType.AGENT, url="",
                            config_json={"scope": "product discovery"})
            session.add(source)
            candidate = Candidate(id=f"cand_{name}", source_type="agent", source_id=source.id,
                                  source_url=f"https://example.com/{name}", original_title=name,
                                  status=CandidateStatus.SELECTED, rights_status=RightsStatus.SELF_OWNED)
            session.add(candidate)
            session.commit()
            content_id = f"content_{name}"
            root = settings.content_dir / content_id
            root.mkdir(parents=True)
            content = Content(id=content_id, candidate_id=candidate.id, title=name,
                              business_status=ContentStatus.WAITING_AGENT if live else ContentStatus.PUBLISHED,
                              current_stage="content_research" if live else "published", content_dir=str(root))
            session.add(content)
            session.commit()
            picture = root / "publish-proof.png"
            Image.new("RGB", (100, 100), "blue").save(picture)
            artifact = Artifact(id=f"asset_{name}", content_id=content.id, artifact_type="publish_evidence",
                                relative_path=picture.name, sha256=hashlib.sha256(picture.read_bytes()).hexdigest(),
                                size_bytes=picture.stat().st_size, mime_type="image/png", producer_stage="agent")
            session.add(artifact)
            payload = {"upstream_task_ids": {}, "account": {"account_id": "local-default"},
                       "source_url": f"https://example.com/{name}", "context_files": [picture.name]}
            result = {"status": "PUBLISHED", "external_id": "published-123", "external_url": "https://www.douyin.com/video/123",
                      "evidence_artifact_id": artifact.id}
            task = AgentTask(id=f"agt_{name}", content_id=content.id,
                             task_type=TaskType.CONTENT_RESEARCH if live else TaskType.DOUYIN_PUBLISH,
                             status=TaskStatus.PENDING if live else TaskStatus.COMPLETED, prompt_name="fixture",
                             instructions="fixed historical prompt", input_data=payload, input_hash=stable_hash(payload),
                             result_data=None if live else result, lease_token_hash=None)
            session.add(task)
            session.commit()
            if not live:
                discovery_result = {"candidates": []}
                discovery = AgentTask(id="agt_source_discovery", task_type=TaskType.SOURCE_DISCOVERY,
                                      status=TaskStatus.COMPLETED, prompt_name="discovery", instructions="fixed",
                                      input_data={"source_id": source.id}, input_hash=stable_hash({"source_id": source.id}),
                                      result_data=discovery_result)
                session.add(discovery)
                session.commit()
                raw = settings.agent_submission_dir / task.id / "attempt-1" / "result.json"
                raw.parent.mkdir(parents=True)
                raw.write_text(json.dumps(result), encoding="utf-8")
                session.add(AgentSubmission(id="sub_source", task_id=task.id, agent_id="historical-worker", attempt=1,
                                            raw_result_path=str(raw.relative_to(project)), result_hash=stable_hash(result),
                                            validation_status="ACCEPTED", validation_errors=[]))
                session.add(PublicationRecord(id="pub_source", content_id=content.id, mode=PublicationMode.EXECUTOR,
                                             platform="douyin", external_id=result["external_id"],
                                             external_url=result["external_url"], status="PUBLISHED"))
            session.add(RuntimeSetting(key="excluded_runtime_secret", value="do-not-copy-runtime-secret"))
            session.commit()
        engine.dispose()
        return Dataset(db, project, (settings.content_dir,), (settings.agent_submission_dir,))
    return create("source"), create("baseline", live=True)


def test_merge_preserves_new_episode_history_hashes_and_idempotence_then_restores(datasets, tmp_path):
    source, baseline = datasets
    initial_source, initial_baseline = _logical_hash(source.database), _logical_hash(baseline.database)
    staging = tmp_path / "stage"
    plan = prepare(source, staging, baseline=baseline)
    assert plan["status"] == "READY" and not plan["blockers"]
    report = apply(staging)
    assert report["status"] == "VERIFIED_STAGING_ONLY" and not report["cutover_authorized"]
    assert report["publication_ids"] == ["published-123"]
    assert report["publication_bindings"][0]["record_binding_verified"]
    assert report["foreign_key_violations"] == []
    with _read(staging / "target.db") as db:
        assert db.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 2
        assert db.execute("SELECT status FROM agent_task WHERE id='agt_baseline'").fetchone()[0] == "PENDING"
        assert db.execute("SELECT lease_token_hash FROM agent_task WHERE id='agt_source'").fetchone()[0] is None
        content = db.execute("SELECT * FROM content WHERE id='content_source'").fetchone()
        assert Path(content["content_dir"]).is_relative_to(staging / "data")
        with _read(source.database) as original:
            old_input = original.execute("SELECT input_data FROM agent_task WHERE id='agt_source'").fetchone()[0]
        assert db.execute("SELECT input_data FROM agent_task WHERE id='agt_source'").fetchone()[0] == old_input
        assert db.execute("SELECT COUNT(*) FROM audit_event WHERE action='mpt_task_owned'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM audit_event WHERE action='mpt_migration_handled_task'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM audit_event WHERE action='mpt_discovery_consumed'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM audit_event WHERE action='mpt_agent_consumed'").fetchone()[0] == 1
        submission = db.execute("SELECT raw_result_path FROM agent_submission").fetchone()[0]
        assert Path(submission).is_relative_to(staging / "data" / "agent-submissions")
        assert stable_hash(json.loads(Path(submission).read_text(encoding="utf-8"))) == db.execute("SELECT result_hash FROM agent_submission").fetchone()[0]
    with _read(staging / "source-business.db") as snapshot:
        assert snapshot.execute("SELECT name FROM sqlite_master WHERE name='runtime_setting'").fetchone() is None
    merged = _logical_hash(staging / "target.db")
    repeated = apply(staging)
    assert all(count == 0 for count in repeated["inserted"].values())
    assert _logical_hash(staging / "target.db") == merged
    restored = restore_rehearsal(staging)
    assert restored["status"] == "RESTORE_REHEARSAL_VERIFIED"
    with _read(Path(restored["restored_database"])) as db:
        assert db.execute("SELECT id FROM content").fetchone()[0] == "content_baseline"
        assert db.execute("SELECT status FROM agent_task WHERE id='agt_baseline'").fetchone()[0] == "PENDING"
    assert _logical_hash(source.database) == initial_source
    assert _logical_hash(baseline.database) == initial_baseline


def test_conflicting_original_id_rolls_back_all_rows_and_preserves_new_episode(datasets, tmp_path):
    source, baseline = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE candidate SET id='cand_baseline' WHERE id='cand_source'")
        db.execute("UPDATE content SET candidate_id='cand_baseline' WHERE id='content_source'")
    staging = tmp_path / "stage"
    assert prepare(source, staging, baseline=baseline)["status"] == "READY"
    before = _logical_hash(staging / "target.db")
    with pytest.raises(MigrationError, match="Conflicting existing identity"):
        apply(staging)
    assert _logical_hash(staging / "target.db") == before


@pytest.mark.parametrize("status", ["PENDING", "LEASED", "SUBMITTED"])
def test_active_source_tasks_require_handoff_without_expiring_lease(datasets, tmp_path, status):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE agent_task SET status=?,lease_token_hash='historical-hash' WHERE id='agt_source'", (status,))
    before = _logical_hash(source.database)
    plan = prepare(source, tmp_path / "stage", include_quarantined_history=True)
    assert plan["status"] == "BLOCKED"
    assert any("source freeze/manual handoff" in reason for reason in plan["blockers"])
    with pytest.raises(MigrationError, match="quarantined"):
        apply(tmp_path / "stage")
    assert _logical_hash(source.database) == before


@pytest.mark.parametrize("fault", ["missing_media", "corrupt_media", "missing_publish_proof"])
def test_bad_terminal_history_is_preserved_only_with_explicit_quarantine(datasets, tmp_path, fault):
    source, baseline = datasets
    picture = source.content_roots[0] / "content_source" / "publish-proof.png"
    if fault == "missing_media":
        picture.unlink()
    elif fault == "corrupt_media":
        picture.write_bytes(b"different")
    else:
        with sqlite3.connect(source.database) as db:
            db.execute("UPDATE publication_record SET external_id='' WHERE id='pub_source'")
    strict = prepare(source, tmp_path / "strict", baseline=baseline)
    assert strict["status"] == "BLOCKED"
    staging = tmp_path / "quarantined"
    plan = prepare(source, staging, baseline=baseline, include_quarantined_history=True)
    assert plan["status"] == "READY" and plan["quarantine"]
    report = apply(staging)
    assert report["status"] == "QUARANTINED_STAGING" and not report["cutover_authorized"]
    if fault == "corrupt_media":
        assert not (staging / "data" / "content" / "content_source" / picture.name).exists()
    with _read(staging / "target.db") as db:
        assert db.execute("SELECT status FROM publication_record WHERE id='pub_source'").fetchone()[0] == "PUBLISHED"
        assert db.execute("SELECT business_status FROM content WHERE id='content_source'").fetchone()[0] == "PUBLISHED"
        audit = db.execute("SELECT metadata_json FROM audit_event WHERE action='mpt_migration_quarantined_content'").fetchone()
        assert json.loads(audit[0])["verified"] is False
        assert db.execute("SELECT COUNT(*) FROM audit_event WHERE action='mpt_task_owned'").fetchone()[0] == 0


def test_nonterminal_bad_media_cannot_use_history_quarantine(datasets, tmp_path):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE content SET business_status='WAITING_AGENT' WHERE id='content_source'")
    (source.content_roots[0] / "content_source" / "publish-proof.png").unlink()
    assert prepare(source, tmp_path / "stage", include_quarantined_history=True)["status"] == "BLOCKED"


def test_frozen_business_credentials_never_reach_snapshot(datasets, tmp_path):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE source SET config_json=? WHERE id='src_source'", (json.dumps({"authorization": "private-credential"}),))
    staging = tmp_path / "stage"
    with pytest.raises(MigrationError, match="Sensitive business JSON"):
        prepare(source, staging)
    assert not (staging / "source-business.db").exists()
    assert "private-credential" not in (staging / "quarantine.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("tamper", ["snapshot", "file"])
def test_immutable_snapshot_or_staged_file_changes_block_import(datasets, tmp_path, tamper):
    source, baseline = datasets
    staging = tmp_path / "stage"
    plan = prepare(source, staging, baseline=baseline)
    before = _logical_hash(staging / "target.db")
    if tamper == "snapshot":
        with sqlite3.connect(staging / "source-business.db") as db:
            db.execute("UPDATE candidate SET original_title='changed'")
    else:
        Path(plan["files"][0]["destination"]).write_bytes(b"changed")
    with pytest.raises(MigrationError, match="snapshot changed|integrity changed"):
        apply(staging)
    assert _logical_hash(staging / "target.db") == before


def test_wal_snapshot_excludes_uncommitted_source_rows(datasets, tmp_path):
    source, _ = datasets
    with sqlite3.connect(source.database) as writer:
        writer.row_factory = sqlite3.Row
        row = dict(writer.execute("SELECT * FROM candidate").fetchone())
        row["id"] = "cand_uncommitted"
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f'INSERT INTO candidate ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
        plan = prepare(source, tmp_path / "stage")
        assert plan["counts"]["candidate"] == 1
        writer.rollback()


def test_stage_cannot_overlap_live_source_roots(datasets):
    source, _ = datasets
    with pytest.raises(MigrationError, match="overlaps"):
        prepare(source, source.content_roots[0] / "staging")


def test_terminal_submission_outside_allowed_roots_remains_quarantined_and_rebased(datasets, tmp_path):
    source, baseline = datasets
    outside = source.project_root / "private-result.json"
    outside.write_text('{"secret":"never-read-or-copy"}', encoding="utf-8")
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE agent_submission SET raw_result_path=?", (str(outside),))
    staging = tmp_path / "stage"
    assert prepare(source, staging, baseline=baseline, include_quarantined_history=True)["status"] == "READY"
    assert apply(staging)["status"] == "QUARANTINED_STAGING"
    with _read(staging / "target.db") as db:
        path = Path(db.execute("SELECT raw_result_path FROM agent_submission").fetchone()[0])
        assert path.is_relative_to(staging / "data" / "agent-submissions" / "quarantined")
        assert not path.exists()


def test_context_credentials_are_quarantined_without_copying_values(datasets, tmp_path):
    source, _ = datasets
    context = source.content_roots[0] / "content_source" / "context.json"
    context.write_text('{"api_key":"do-not-stage-secret"}', encoding="utf-8")
    with sqlite3.connect(source.database) as db:
        payload = json.loads(db.execute("SELECT input_data FROM agent_task WHERE id='agt_source'").fetchone()[0])
        payload["context_files"].append(context.name)
        db.execute("UPDATE agent_task SET input_data=?,input_hash=? WHERE id='agt_source'", (json.dumps(payload), stable_hash(payload)))
    staging = tmp_path / "stage"
    plan = prepare(source, staging, include_quarantined_history=True)
    assert plan["status"] == "READY"
    assert any("sensitive referenced JSON" in item["reason"] for item in plan["quarantine"])
    assert not (staging / "data" / "content" / "content_source" / context.name).exists()
    assert "do-not-stage-secret" not in (staging / "preflight.json").read_text(encoding="utf-8")


def test_combined_baseline_and_source_capacity_is_checked(datasets, tmp_path, monkeypatch):
    source, baseline = datasets
    from collections import namedtuple
    import app.business.migration as migration

    usage = namedtuple("usage", "total used free")
    # Make each independent inventory fit while their two snapshots plus reserve do not.
    monkeypatch.setattr(migration.shutil, "disk_usage", lambda _: usage(10**9, 0, 269_000_000))
    plan = prepare(source, tmp_path / "stage", baseline=baseline)
    assert plan["status"] == "BLOCKED"
    assert any("combined baseline/source disk space" in reason for reason in plan["blockers"])
    assert plan["combined_required_file_bytes"] > plan["required_file_bytes"]


def test_unknown_publication_stays_unknown_and_requires_quarantine_not_requeue(datasets, tmp_path):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE content SET business_status='ARCHIVED' WHERE id='content_source'")
        db.execute("UPDATE publication_record SET status='UNKNOWN',external_id='',external_url='' WHERE id='pub_source'")
    assert prepare(source, tmp_path / "strict")["status"] == "BLOCKED"
    staging = tmp_path / "stage"
    assert prepare(source, staging, include_quarantined_history=True)["status"] == "READY"
    report = apply(staging)
    assert report["status"] == "QUARANTINED_STAGING"
    assert report["publication_bindings"][0]["record_binding_verified"] is False
    with _read(staging / "target.db") as db:
        assert db.execute("SELECT status FROM publication_record").fetchone()[0] == "UNKNOWN"
        assert db.execute("SELECT COUNT(*) FROM agent_task WHERE status='PENDING'").fetchone()[0] == 0
        assert db.execute("SELECT 1 FROM audit_event WHERE action='mpt_migration_quarantined_content'").fetchone()


@pytest.mark.parametrize("key", ["api_keys", "tokens", "credentials", "refresh_tokens", "Passwords", "clientSecrets", "cookies"])
def test_plural_nested_credentials_never_reach_business_snapshot(datasets, tmp_path, key):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE source SET config_json=?", (json.dumps({"nested": [{key: ["private-demo-secret"]}]}),))
    staging = tmp_path / "stage"
    with pytest.raises(MigrationError, match="Sensitive business JSON"):
        prepare(source, staging)
    assert not (staging / "source-business.db").exists()
    assert "private-demo-secret" not in (staging / "quarantine.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("fault", ["baseline_file", "baseline_snapshot", "baseline_content", "extra_target_row", "rollback"])
@pytest.mark.parametrize("repeated", [False, True])
def test_baseline_or_target_drift_rejects_first_and_repeated_import(datasets, tmp_path, fault, repeated):
    source, baseline = datasets
    staging = tmp_path / "stage"
    prepare(source, staging, baseline=baseline)
    if repeated:
        apply(staging)
    if fault == "baseline_file":
        (staging / "data" / "content" / "content_baseline" / "publish-proof.png").write_bytes(b"changed")
    elif fault in {"baseline_snapshot", "rollback"}:
        name = "baseline-business.db" if fault == "baseline_snapshot" else "rollback.db"
        with sqlite3.connect(staging / name) as db:
            db.execute("UPDATE content SET title='external-drift'")
    else:
        with sqlite3.connect(staging / "target.db") as db:
            if fault == "baseline_content":
                db.execute("UPDATE content SET title='external-drift' WHERE id='content_baseline'")
            else:
                db.execute("INSERT INTO audit_event (id,actor_type,actor_id,entity_type,entity_id,action,metadata_json,created_at) VALUES ('aud_external','system','external','content','content_baseline','external','{}','2026-10-10 00:00:00')")
    before = _logical_hash(staging / "target.db")
    with pytest.raises(MigrationError, match="snapshot changed|integrity changed|drift|backup changed"):
        apply(staging)
    assert _logical_hash(staging / "target.db") == before


@pytest.mark.parametrize("live", [False, True])
def test_frozen_input_hash_mismatch_is_quarantined_only_for_terminal_history(datasets, tmp_path, live):
    source, baseline = datasets
    dataset = baseline if live else source
    with sqlite3.connect(dataset.database) as db:
        db.execute("UPDATE agent_task SET input_hash='different-hash'")
    staging = tmp_path / "stage"
    plan = prepare(source, staging, baseline=baseline, include_quarantined_history=True)
    issues = plan["blockers"] if live else [item["reason"] for item in plan["quarantine"]]
    assert any("input hash mismatch" in item for item in issues)
    if live:
        assert plan["status"] == "BLOCKED"
    else:
        assert plan["status"] == "READY"
        assert apply(staging)["status"] == "QUARANTINED_STAGING"
        with _read(staging / "target.db") as db:
            assert db.execute("SELECT input_hash FROM agent_task WHERE id='agt_source'").fetchone()[0] == "different-hash"


def test_media_change_during_merge_is_detected_before_transaction_commit(datasets, tmp_path, monkeypatch):
    import app.business.migration as migration
    source, baseline = datasets
    staging = tmp_path / "stage"
    prepare(source, staging, baseline=baseline)
    before = _logical_hash(staging / "target.db")
    original = migration._audit_rows
    calls = 0

    def change_during_real_merge(*args):
        nonlocal calls
        calls += 1
        if calls == 2:  # First iterator constructs expected lineage; second runs inside the merge transaction.
            (staging / "data" / "content" / "content_baseline" / "publish-proof.png").write_bytes(b"changed-during-merge")
        yield from original(*args)

    monkeypatch.setattr(migration, "_audit_rows", change_during_real_merge)
    with pytest.raises(MigrationError, match="integrity changed"):
        apply(staging)
    assert _logical_hash(staging / "target.db") == before


def test_terminal_input_hash_mismatch_without_quarantine_still_blocks(datasets, tmp_path):
    source, _ = datasets
    with sqlite3.connect(source.database) as db:
        db.execute("UPDATE agent_task SET input_hash='invalid'")
    plan = prepare(source, tmp_path / "stage")
    assert plan["status"] == "BLOCKED"
    assert any("input hash mismatch" in reason for reason in plan["blockers"])


def test_restore_rehearsal_rejects_corrupted_baseline_media(datasets, tmp_path):
    source, baseline = datasets
    staging = tmp_path / "stage"
    prepare(source, staging, baseline=baseline)
    apply(staging)
    (staging / "data" / "content" / "content_baseline" / "publish-proof.png").write_bytes(b"changed")
    with pytest.raises(MigrationError, match="integrity changed"):
        restore_rehearsal(staging)
    assert not (staging / "rollback-rehearsal.db").exists()


@pytest.fixture
def paused_dataset(datasets):
    source, baseline = datasets
    root = source.content_roots[0] / "content_source"
    audio = root / "audio" / "narration.mp3"
    audio.parent.mkdir()
    audio.write_bytes(b"current-voice")
    boundaries = audio.parent / "word_boundaries.json"
    boundaries.write_text('[{"text":"current","start_sec":0,"end_sec":1}]', encoding="utf-8")
    evidence = {"voice_sha256": _hash(audio), "boundaries_sha256": _hash(boundaries), "duration_sec": 1.0,
                "boundaries": json.loads(boundaries.read_text(encoding="utf-8"))}
    engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{source.database.as_posix()}"))
    with Session(engine) as session:
        content = session.get(Content, "content_source")
        content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "visual_plan_qc"
        session.add(content)
        session.delete(session.get(PublicationRecord, "pub_source"))
        old = Artifact(id="asset_old_voice", content_id=content.id, artifact_type="voice", relative_path="audio/narration.mp3",
                       sha256=hashlib.sha256(b"old-voice").hexdigest(), size_bytes=len(b"old-voice"),
                       mime_type="audio/mpeg", producer_stage="voice", created_at=datetime(2026, 1, 1, tzinfo=UTC))
        current = Artifact(id="asset_current_voice", content_id=content.id, artifact_type="voice", relative_path=old.relative_path,
                           sha256=evidence["voice_sha256"], size_bytes=audio.stat().st_size,
                           mime_type="audio/mpeg", producer_stage="voice", created_at=datetime(2026, 1, 2, tzinfo=UTC))
        session.add(old)
        session.add(current)
        payload = {"upstream_task_ids": {"script": "agt_source"}, "visual_timing_evidence": evidence,
                   "context_files": ["audio/narration.mp3"]}
        session.add(AgentTask(id="agt_paused", content_id=content.id, task_type=TaskType.VISUAL_PLAN_QC,
                              status=TaskStatus.PENDING, prompt_name="fixture", instructions="frozen handoff",
                              input_data=payload, input_hash=stable_hash(payload)))
        accepted = session.get(AgentSubmission, "sub_source")
        accepted.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        session.add(accepted)
        session.add(AgentSubmission(id="sub_old_rejected", task_id="agt_source", agent_id="historical-worker", attempt=1,
                                    raw_result_path=accepted.raw_result_path, result_hash=stable_hash({"rejected": True}),
                                    validation_status="REJECTED", validation_errors=[], created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        session.commit()
    engine.dispose()
    return source, baseline


def handoff_manifest(source, tmp_path):
    pin = tmp_path / "pin.db"
    _snapshot(source, pin)
    with _read(pin) as db:
        row = db.execute("SELECT id,content_id,input_hash FROM agent_task WHERE id='agt_paused'").fetchone()
    value = {"version": 1, "source_snapshot_sha256": _hash(pin), "reason": "Preserve queued work paused in independent staging",
             "tasks": [{"task_id": row["id"], "content_id": row["content_id"], "input_hash": row["input_hash"]}]}
    manifest = tmp_path / "handoff.json"
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return manifest


@pytest.mark.parametrize("reverse", [False, True])
def test_paused_handoff_preserves_valid_shared_paths_and_old_metadata_order_independently(paused_dataset, tmp_path, reverse):
    source, baseline = paused_dataset
    if reverse:
        with sqlite3.connect(source.database) as db:
            db.row_factory = sqlite3.Row
            for table, condition in (("artifact", "artifact_type='voice'"), ("agent_submission", "task_id='agt_source'")):
                rows = [dict(r) for r in db.execute(f"SELECT * FROM {table} WHERE {condition}")][::-1]
                db.execute(f"DELETE FROM {table} WHERE {condition}")
                for row in rows:
                    db.execute(f'INSERT INTO {table} ({",".join(row)}) VALUES ({",".join("?" for _ in row)})', tuple(row.values()))
    manifest = handoff_manifest(source, tmp_path)
    before = _logical_hash(source.database)
    staging = tmp_path / "stage"
    plan = prepare(source, staging, baseline=baseline, paused_handoff=manifest)
    assert plan["status"] == "READY" and not plan["blockers"]
    assert any(i["identity"] == "artifact/asset_old_voice" for i in plan["quarantine"])
    assert any(i["identity"] == "submission/sub_old_rejected" for i in plan["quarantine"])
    assert not any(i["identity"] in {"artifact/asset_current_voice", "submission/sub_source"} for i in plan["quarantine"])
    assert _hash(staging / "data/content/content_source/audio/narration.mp3") == _hash(source.content_roots[0] / "content_source/audio/narration.mp3")
    assert _hash(staging / "data/content/content_source/audio/word_boundaries.json") == _hash(source.content_roots[0] / "content_source/audio/word_boundaries.json")
    report = apply(staging)  # Temporary fixture staging only, never real source/live.
    assert report["status"] == "QUARANTINED_STAGING" and not report["cutover_authorized"]
    with _read(staging / "target.db") as target, _read(source.database) as original:
        assert dict(target.execute("SELECT * FROM agent_task WHERE id='agt_paused'").fetchone()) == dict(original.execute("SELECT * FROM agent_task WHERE id='agt_paused'").fetchone())
        assert target.execute("SELECT 1 FROM artifact WHERE id='asset_old_voice'").fetchone()
        assert target.execute("SELECT 1 FROM agent_submission WHERE id='sub_old_rejected'").fetchone()
        assert target.execute("SELECT 1 FROM audit_event WHERE action='mpt_migration_quarantined_content' AND entity_id='content_source'").fetchone()
        paused_audit = target.execute("SELECT metadata_json FROM audit_event WHERE action='mpt_migration_quarantined_task' AND entity_id='agt_paused'").fetchone()
        assert json.loads(paused_audit[0])["paused"] is True
        assert json.loads(paused_audit[0])["input_hash"] == json.loads(manifest.read_text())["tasks"][0]["input_hash"]
        assert not target.execute("SELECT 1 FROM audit_event WHERE entity_id='agt_paused' AND action IN ('mpt_task_owned','mpt_agent_consumed','mpt_migration_handled_task')").fetchone()
    assert _logical_hash(source.database) == before
    assert all(v == 0 for v in apply(staging)["inserted"].values())


def test_without_manifest_pending_handoff_still_blocks(paused_dataset, tmp_path):
    source, _ = paused_dataset
    assert prepare(source, tmp_path / "stage", include_quarantined_history=True)["status"] == "BLOCKED"


@pytest.mark.parametrize("fault", ["snapshot", "input_hash", "content_id", "task_id", "duplicate", "LEASED", "SUBMITTED", "UNKNOWN"])
def test_paused_handoff_requires_exact_pinned_pending_tasks(paused_dataset, tmp_path, fault):
    source, _ = paused_dataset
    if fault in {"LEASED", "SUBMITTED", "UNKNOWN"}:
        with sqlite3.connect(source.database) as db:
            db.execute("UPDATE agent_task SET status=? WHERE id='agt_paused'", (fault,))
    manifest = handoff_manifest(source, tmp_path)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    if fault == "snapshot":
        value["source_snapshot_sha256"] = "0" * 64
    elif fault in {"input_hash", "content_id", "task_id"}:
        value["tasks"][0][fault] = "not-the-pinned-value"
    elif fault == "duplicate":
        value["tasks"].append(value["tasks"][0])
    manifest.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(MigrationError, match="handoff"):
        prepare(source, tmp_path / "stage", paused_handoff=manifest)


@pytest.mark.parametrize("fault", ["current_voice", "current_sidecar", "credential_context", "extra_pending", "unknown_publication"])
def test_manifest_does_not_swallow_other_nonterminal_blockers(paused_dataset, tmp_path, fault):
    source, _ = paused_dataset
    root = source.content_roots[0] / "content_source"
    if fault == "current_voice":
        (root / "audio/narration.mp3").write_bytes(b"bad-current-voice")
    elif fault == "current_sidecar":
        (root / "audio/word_boundaries.json").write_text("[]", encoding="utf-8")
    elif fault == "unknown_publication":
        engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{source.database.as_posix()}"))
        with Session(engine) as session:
            session.add(PublicationRecord(id="pub_unknown", content_id="content_source", platform="douyin",
                                          mode=PublicationMode.EXECUTOR, status="UNKNOWN"))
            session.commit()
        engine.dispose()
    else:
        with sqlite3.connect(source.database) as db:
            if fault == "credential_context":
                (root / "context.json").write_text('{"api_keys":["private-not-copied"]}', encoding="utf-8")
                payload = json.loads(db.execute("SELECT input_data FROM agent_task WHERE id='agt_paused'").fetchone()[0])
                payload["context_files"].append("context.json")
                db.execute("UPDATE agent_task SET input_data=?,input_hash=? WHERE id='agt_paused'", (json.dumps(payload), stable_hash(payload)))
            elif fault == "extra_pending":
                db.execute("UPDATE agent_task SET status='PENDING' WHERE id='agt_source'")
    manifest = handoff_manifest(source, tmp_path)
    plan = prepare(source, tmp_path / "stage", paused_handoff=manifest, include_quarantined_history=True)
    assert plan["status"] == "BLOCKED" and plan["blockers"]


def test_paired_sidecars_follow_frozen_voice_artifact_in_source_and_baseline(paused_dataset, tmp_path):
    source, baseline = paused_dataset
    for dataset, content_id, task_id in ((source, "content_source", "agt_paused"),
                                         (baseline, "content_baseline", "agt_baseline")):
        root = dataset.content_roots[0] / content_id
        voice = root / "audio/voice-unique/narration.mp3"
        voice.parent.mkdir(parents=True)
        if dataset == source:
            (root / "audio/narration.mp3").rename(voice)
            (root / "audio/word_boundaries.json").rename(voice.parent / "word_boundaries.json")
        else:
            voice.write_bytes(b"baseline-new-voice")
            (voice.parent / "word_boundaries.json").write_text("[]", encoding="utf-8")
        evidence = {"voice_sha256": _hash(voice), "boundaries_sha256": _hash(voice.parent / "word_boundaries.json"),
                    "duration_sec": 1.0, "boundaries": []}
        engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{dataset.database.as_posix()}"))
        with Session(engine) as session:
            task = session.get(AgentTask, task_id)
            task.input_data = {**task.input_data, "visual_timing_evidence": evidence,
                               "context_files": ["audio/voice-unique/narration.mp3"]}
            task.input_hash = stable_hash(task.input_data)
            session.add(task)
            if dataset == source:
                for artifact_id in ("asset_old_voice", "asset_current_voice"):
                    artifact = session.get(Artifact, artifact_id)
                    artifact.relative_path = "audio/voice-unique/narration.mp3"
                    session.add(artifact)
            else:
                session.add(Artifact(id="asset_baseline_voice", content_id=content_id, artifact_type="voice",
                                     relative_path="audio/voice-unique/narration.mp3", size_bytes=voice.stat().st_size,
                                     sha256=_hash(voice), mime_type="audio/mpeg", producer_stage="voice"))
            session.commit()
        engine.dispose()
    staging = tmp_path / "stage"
    assert prepare(source, staging, baseline=baseline, paused_handoff=handoff_manifest(source, tmp_path))["status"] == "READY"
    for dataset, content_id in ((source, "content_source"), (baseline, "content_baseline")):
        relative = Path(content_id) / "audio/voice-unique/word_boundaries.json"
        assert _hash(staging / "data/content" / relative) == _hash(dataset.content_roots[0] / relative)
        assert not (staging / "data/content" / content_id / "audio/word_boundaries.json").exists()
    assert apply(staging)["status"] == "QUARANTINED_STAGING"


def test_paused_manifest_tampering_after_prepare_is_rejected(paused_dataset, tmp_path):
    source, _ = paused_dataset
    staging = tmp_path / "stage"
    assert prepare(source, staging, paused_handoff=handoff_manifest(source, tmp_path))["status"] == "READY"
    (staging / "paused-handoff.json").write_text("{}", encoding="utf-8")
    with pytest.raises(MigrationError, match="handoff changed"):
        apply(staging)


@pytest.mark.parametrize("current_sidecar_valid", [True, False])
def test_cancelled_old_timing_requires_verified_newer_paused_voice(paused_dataset, tmp_path, current_sidecar_valid):
    source, _ = paused_dataset
    engine = build_engine(Settings(_env_file=None, database_url=f"sqlite:///{source.database.as_posix()}"))
    with Session(engine) as session:
        old_voice = session.get(Artifact, "asset_old_voice")
        payload = {"visual_timing_evidence": {"voice_sha256": old_voice.sha256,
                                             "boundaries_sha256": hashlib.sha256(b"lost-boundaries").hexdigest()}}
        session.add(AgentTask(id="agt_old_timing", content_id="content_source", task_type=TaskType.VISUAL_PLAN,
                              status=TaskStatus.CANCELLED, prompt_name="old", instructions="old frozen timing",
                              input_data=payload, input_hash=stable_hash(payload), created_at=datetime(2026, 1, 1, tzinfo=UTC)))
        session.commit()
    engine.dispose()
    if not current_sidecar_valid:
        (source.content_roots[0] / "content_source/audio/word_boundaries.json").write_text("[]", encoding="utf-8")
    plan = prepare(source, tmp_path / "stage", paused_handoff=handoff_manifest(source, tmp_path))
    if current_sidecar_valid:
        assert plan["status"] == "READY"
        assert any(item["identity"] == "task/agt_old_timing/timing" for item in plan["quarantine"])
    else:
        assert plan["status"] == "BLOCKED"
        assert any("agt_old_timing/timing" in reason for reason in plan["blockers"])
