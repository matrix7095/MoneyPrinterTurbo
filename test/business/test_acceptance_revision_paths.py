"""Acceptance snapshots and packaging follow the current registered render revision."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient
from glance_core.content_acceptance import acceptance_subject, source_context
from glance_core.models import AgentTask, Artifact, Content, ContentStatus, TaskStatus, TaskType, utc_now
from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS
from glance_media.executor import NonRetryableMediaError, sha256_file
from PIL import Image
from sqlmodel import Session, select

from app.business import pipeline
from app.business.media_executor import MptMediaExecutor
from test.business.test_content_http import acceptance_fixture, content_app, patched_asr
from test.business.test_discovery_http import make_runtime


@pytest.fixture
def waiting_acceptance(tmp_path, monkeypatch):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe required for real media fixture")
    runtime = make_runtime(tmp_path)
    runtime.settings.archive_repo_dir = tmp_path / "isolated-archive"
    runtime.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=False)
    with TestClient(content_app(runtime)) as http:
        identifiers = acceptance_fixture(http, runtime)
        yield runtime, identifiers
    runtime.close()


def write_current(runtime, content_id):
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        old = root / "render" / "acceptance-input.json"
        inputs = json.loads(old.read_text(encoding="utf-8"))
        inputs["revision_note"] = "New independent receipt"
        artifact = pipeline._write_acceptance_receipt(session, content, MptMediaExecutor(root), inputs)
        artifact.created_at = utc_now() + timedelta(seconds=1)
        session.add(artifact)
        session.commit()
        return root, artifact.id, deepcopy(inputs), old.read_bytes()


def test_two_snapshots_preserve_old_bytes_and_resume_latest_registered_input(waiting_acceptance, monkeypatch):
    runtime, (content_id, _, _, _, old_receipt_id) = waiting_acceptance
    with Session(runtime.engine) as session:
        old_hash = session.get(Artifact, old_receipt_id).sha256
    root, new_id, current, original = write_current(runtime, content_id)
    patched_asr(monkeypatch, available=True)
    monkeypatch.setattr(MptMediaExecutor, "render", lambda *args, **kwargs: pytest.fail("Resume must not render"))
    resumed = runtime.run_content_stage(content_id, pipeline.resume_content_acceptance)
    assert resumed.current_stage == "content_qc"
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC)).one()
        assert qc.input_data["acceptance_receipt_artifact_id"] == new_id
        assert qc.input_data["revision_note"] == current["revision_note"]
        assert session.get(Artifact, old_receipt_id).sha256 == old_hash
    assert (root / "render" / "acceptance-input.json").read_bytes() == original


@pytest.mark.parametrize("damage", ["bytes", "missing", "outside", "duplicate", "foreign_id", "stale_subject"])
def test_bad_latest_receipt_closes_instead_of_falling_back_to_valid_old(waiting_acceptance, monkeypatch, damage):
    runtime, (content_id, _, _, _, _) = waiting_acceptance
    root, current_id, current, original = write_current(runtime, content_id)
    with Session(runtime.engine) as session:
        artifact = session.get(Artifact, current_id)
        path = root / artifact.relative_path
        if damage == "bytes":
            path.write_bytes(b"changed")
        elif damage == "missing":
            path.unlink()
        elif damage == "outside":
            artifact.relative_path = "../outside.json"
            session.add(artifact)
        elif damage == "duplicate":
            session.add(Artifact(content_id=content_id, artifact_type="acceptance_input", relative_path=artifact.relative_path,
                                 sha256=artifact.sha256, size_bytes=artifact.size_bytes, mime_type="application/json",
                                 producer_stage="render", created_at=utc_now() + timedelta(seconds=2)))
        else:
            if damage == "foreign_id":
                current["acceptance_receipt_artifact_id"] = "asset_" + "f" * 32
            else:
                current["acceptance_subject"]["script_sha256"] = "changed"
            path.write_text(json.dumps(current), encoding="utf-8")
            artifact.sha256, artifact.size_bytes = sha256_file(path), path.stat().st_size
            session.add(artifact)
        session.commit()
    patched_asr(monkeypatch, available=True)
    result = runtime.run_content_stage(content_id, pipeline.resume_content_acceptance)
    assert result.current_stage == "audio_evidence_closed"
    assert result.business_status == ContentStatus.FAILED
    with Session(runtime.engine) as session:
        assert session.exec(select(AgentTask.id).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC)).first() is None
    assert (root / "render" / "acceptance-input.json").read_bytes() == original


def test_package_uses_current_passed_artifact_not_historical_final_alias(waiting_acceptance, monkeypatch):
    runtime, (content_id, old_video_id, _, script_id, _) = waiting_acceptance
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        old_video = session.get(Artifact, old_video_id)
        alias = root / old_video.relative_path
        before = alias.read_bytes()
        current_path = root / "render" / "revisions" / "current.mp4"
        current_path.parent.mkdir()
        shutil.copyfile(alias, current_path)
        current = Artifact(content_id=content_id, artifact_type="final_video", relative_path=current_path.relative_to(root).as_posix(),
                           sha256=sha256_file(current_path), size_bytes=current_path.stat().st_size,
                           mime_type="video/mp4", producer_stage="render", created_at=utc_now() + timedelta(seconds=1))
        session.add(current)
        script = session.get(AgentTask, script_id)
        script.result_data = {**script.result_data, "title_options": ["Current product"], "cover_title": "Product"}
        session.add(script)
        session.commit()
        plan = runtime.workflow_service(session).completed_task_for(content_id, TaskType.VISUAL_PLAN)
        image_path = root / "source-image.png"
        Image.new("RGB", (64, 64), "black").save(image_path)
        image = Artifact(content_id=content_id, artifact_type="source_image", relative_path=image_path.name,
                         sha256=sha256_file(image_path), size_bytes=image_path.stat().st_size,
                         mime_type="image/png", producer_stage="prepare")
        session.add(image)
        plan.result_data = {"segments": [{"asset_id": image.id}]}
        session.add(plan)
        session.commit()
        refs = {"script": script_id, "visual_plan": plan.id, "video_artifact_id": current.id}
        inputs = {"script": script.result_data, "research": runtime.workflow_service(session).result_for(content_id, TaskType.CONTENT_RESEARCH),
                  "upstream_task_ids": {"script": script_id, "visual_plan": plan.id}, "video_artifact_id": current.id,
                  "acceptance_contract_version": 2, "acceptance_origin": "manual", "source_context_version": 1,
                  "source_context": source_context(session, content_id, refs),
                  "acceptance_subject": acceptance_subject(session, content_id, refs, source_context_version=1)}
        pipeline._write_acceptance_receipt(session, content, MptMediaExecutor(root), inputs)
        session.commit()
    patched_asr(monkeypatch, available=True)
    runtime.run_content_stage(content_id, pipeline.resume_content_acceptance)
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.CONTENT_QC)).one()
        qc.status = TaskStatus.COMPLETED
        qc.result_data = {"contract_version": 2, "confidence": "high", "passed": True, "issues": [],
                          "subject": qc.input_data["acceptance_subject"],
                          "checks": {key: {"passed": True, "evidence": "Isolated actual media contract"} for key in CONTENT_ACCEPTANCE_CHECKS}}
        session.add(qc)
        session.commit()
    runtime.settings.cover_ai_enabled = False
    seen = []
    package = MptMediaExecutor.package

    def actual_package(executor, metadata, *, final_path):
        seen.append(final_path)
        return package(executor, metadata, final_path=final_path)

    monkeypatch.setattr(MptMediaExecutor, "package", actual_package)
    result = runtime.run_content_stage(content_id, pipeline.approve_final)
    assert result.business_status == ContentStatus.PACKAGED
    assert seen == [current_path.resolve()]
    assert (root / "publish" / "final.mp4").read_bytes() == current_path.read_bytes()
    assert alias.read_bytes() == before


def test_cover_frame_uses_current_registered_video_and_rejects_changed_bytes(waiting_acceptance, monkeypatch):
    from glance_media import executor as media

    runtime, (content_id, old_video_id, _, _, _) = waiting_acceptance
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        old = session.get(Artifact, old_video_id)
        path = root / "render" / "current.mp4"
        shutil.copyfile(root / old.relative_path, path)
        latest = Artifact(content_id=content_id, artifact_type="final_video", relative_path="render/current.mp4",
                          sha256=sha256_file(path), size_bytes=path.stat().st_size, mime_type="video/mp4", producer_stage="render",
                          created_at=utc_now() + timedelta(seconds=1))
        session.add(latest)
        session.commit()
        observed = []
        monkeypatch.setattr(media, "probe", lambda actual: {"format": {"duration": 10}})

        def extract(self, actual, timestamp, target):
            observed.append((actual, timestamp))
            return target

        monkeypatch.setattr(MptMediaExecutor, "extract_cover_frame", extract)
        assert pipeline._fallback_cover_base(session, content, MptMediaExecutor(root))[1] == "final_30_percent"
        assert observed == [(path.resolve(), 3.0)]
        path.write_bytes(b"changed")
        with pytest.raises(NonRetryableMediaError, match="integrity"):
            pipeline._fallback_cover_base(session, content, MptMediaExecutor(root))
