"""Registered originals must reach script writers without rewriting leased inputs."""

from copy import deepcopy
from datetime import timedelta
import hashlib
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from glance_core.agent_roles import primary_role_for
from glance_core.models import AgentTask, Artifact, AuditEvent, ContentRevision, TaskStatus, TaskType, as_utc
from glance_core.task_schemas import schema_for
from glance_core.task_service import AgentTaskService, TaskValidationError, stable_hash
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

from app.business import bundles
from app.business import pipeline
from app.business.runtime import AGENT_CONSUMED
from test.business.test_content_http import RESEARCH, client_for, content_app, select_and_claim
from test.business.test_discovery_http import make_runtime
from test.business.test_semantic_http import SCRIPT
from test.business.test_source_media_priority import accepted_research, issue, subject as subject


@pytest.fixture
def original_context(subject):
    session, settings, content, assets = subject
    revision = ContentRevision(content_id=content.id, subject_sha256="b" * 64)
    session.add(revision)
    session.commit()
    content.current_revision_id = revision.id
    session.add(content)
    session.commit()
    asset = assets[0]
    asset.revision_id = revision.id
    path = Path(content.content_dir) / asset.relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"registered original one")
    asset.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    asset.size_bytes = path.stat().st_size
    session.add(asset)
    session.commit()
    research = accepted_research(subject)
    # Two source URLs, deliberately reverse the acquisition receipt order.
    second = Artifact(content_id=content.id, revision_id=revision.id, artifact_type="source_image",
                      relative_path="second.png", sha256=asset.sha256, size_bytes=asset.size_bytes,
                      mime_type="image/png", producer_stage="fixture")
    (path.parent / second.relative_path).write_bytes(path.read_bytes())
    session.add(second)
    session.commit()
    research.result_data = {**research.result_data, "source_images": [*research.result_data["source_images"],
                            {"url": "https://example.org/second.png", "caption": "Second view",
                             "source_page": "https://example.org/product"}]}
    from glance_core.models import AgentSubmission
    accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == research.id)).one()
    accepted.result_hash = stable_hash(research.result_data)
    receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                   AuditEvent.action == "source_media_images_acquired")).one()
    receipt.metadata_json = {"result_hash": accepted.result_hash, "images": [
        {"artifact_id": second.id, "url": "https://example.org/second.png", "relative_path": second.relative_path, "sha256": second.sha256},
        receipt.metadata_json["images"][0]], "unavailable_image_urls": []}
    session.add_all([research, accepted, receipt])
    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                           entity_id=research.id, action="mpt_agent_consumed",
                           metadata_json={"result_hash": accepted.result_hash}))
    session.commit()
    return subject, research, [asset, second]


def script_bundle(original_context, *, legacy):
    subject, research, _ = original_context
    session, settings, content, _ = subject
    if legacy:
        inputs = {"production_mode": "image_only", "upstream_task_ids": {"research": research.id}}
        script = AgentTask(content_id=content.id, revision_id=content.current_revision_id,
                           task_type=TaskType.SHORT_SCRIPT, prompt_name="short_script", instructions="Frozen legacy",
                           input_data=inputs, input_hash=stable_hash(inputs), result_schema=schema_for(TaskType.SHORT_SCRIPT))
        session.add(script)
        session.commit()
        service = AgentTaskService(session, settings.agent_submission_dir)
        script, token = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.SHORT_SCRIPT),
                                       capabilities={TaskType.SHORT_SCRIPT}, task_id=script.id)
    else:
        service, script, token = issue(subject, TaskType.SHORT_SCRIPT,
                                       {"upstream_task_ids": {"research": research.id}})
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    frozen = deepcopy(script.input_data), script.input_hash, script.attempt
    result = bundles.build_bundle(script.id, token, service, session, flow, failure_recovery=lambda *a, **k: {})
    assert (script.input_data, script.input_hash, script.attempt) == frozen
    return script, result


@pytest.mark.parametrize("legacy", [False, True])
def test_two_registered_originals_reach_script_and_manifest_by_exact_url(original_context, legacy):
    _, research, assets = original_context
    script, bundle = script_bundle(original_context, legacy=legacy)
    images = bundle["input"]["research"]["source_images"]
    assert {row["url"]: row["artifact_id"] for row in images} == {
        research.result_data["source_images"][index]["url"]: asset.id for index, asset in enumerate(assets)}
    assert bundle["input"]["source_images"] == images
    assert set(bundle["input"]["allowed_asset_ids"]) == {asset.id for asset in assets}
    assert {row["input_ref"]: row["sha256"] for row in bundle["context_manifest"]} == {
        asset.id: asset.sha256 for asset in assets}
    assert len(bundle["context_files"]) == 2
    assert research.result_data["source_images"][0].get("artifact_id") is None
    assert "script_source_media_context_version" not in script.input_data
    assert bundle["input"]["script_source_media_context_read_only"]["source_media_acquisition"]["research_task_id"] == research.id


@pytest.mark.parametrize("damage", ["old_revision", "wrong_url", "sha", "file_bytes", "duplicate_url", "research_hash", "consumed_hash"])
def test_script_context_rejects_changed_source_evidence(original_context, damage):
    subject, research, assets = original_context
    session, _, content, _ = subject
    receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                   AuditEvent.action == "source_media_images_acquired")).one()
    data = deepcopy(receipt.metadata_json)
    if damage == "old_revision":
        assets[0].revision_id = None
        session.add(assets[0])
    elif damage == "wrong_url":
        data["images"][0]["url"] = "https://example.org/unrelated.png"
    elif damage == "duplicate_url":
        data["images"][0]["url"] = data["images"][1]["url"]
    elif damage == "sha":
        data["images"][0]["sha256"] = "0" * 64
    elif damage == "file_bytes":
        (Path(content.content_dir) / assets[0].relative_path).write_bytes(b"changed original")
    elif damage == "research_hash":
        research.result_data = {**research.result_data, "recommended_angle": "changed"}
        session.add(research)
    else:
        consumed = session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                        AuditEvent.action == AGENT_CONSUMED)).one()
        consumed.metadata_json = {"result_hash": "changed"}
        session.add(consumed)
    receipt.metadata_json = data
    session.add(receipt)
    session.commit()
    with pytest.raises((HTTPException, RuntimeError), match="SOURCE_MEDIA"):
        script_bundle(original_context, legacy=True)


@pytest.mark.parametrize("preferred_source", ["https://example.org/product.png", "asset_previous_revision", "source"])
def test_script_submission_cannot_replace_registered_ids_with_urls_or_old_assets(original_context, preferred_source):
    subject, research, _ = original_context
    service, script, token = issue(subject, TaskType.SHORT_SCRIPT,
                                   {"upstream_task_ids": {"research": research.id}})
    result = deepcopy(SCRIPT)
    result["visual_requirements"][0]["preferred_source"] = preferred_source
    with pytest.raises(TaskValidationError) as failure:
        service.complete(task_id=script.id, lease_token=token, agent_id="fixture-worker", result=result)
    assert any(row["path"] == ["visual_requirements", 0, "preferred_source"] for row in failure.value.errors)


def test_context_missing_pending_script_uses_normal_claim_download_submit_and_consume(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    research_result = deepcopy(RESEARCH)
    research_result["source_images"] = [
        {"url": f"https://example.org/view-{index}.png", "caption": f"Product view {index}",
         "source_page": "https://example.org/product"} for index in range(2)]

    def acquire(executor, images):
        from PIL import Image
        output = []
        for index, image in enumerate(reversed(images)):
            path = executor.root / f"original-{index}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1080, 1920), (index * 100, 60, 120)).save(path)
            output.append({**image, "path": path, "width": 1080, "height": 1920})
        return output

    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", acquire)
    with TestClient(content_app(runtime)) as http:
        content_id, researcher, claim = select_and_claim(http, runtime)
        assert researcher.complete(claim["task_id"], claim["lease_token"], "research-worker", research_result)["advanced"] == "CONSUMED"
        agent = client_for(http, "script")
        first = agent.claim()
        agent.fail(first["task_id"], first["lease_token"], "script-worker", retryable=True,
                   error_code="CONTEXT_MISSING", message="research.source_images has URLs but no artifact_id")
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, first["task_id"])
            assert task.status == TaskStatus.PENDING and task.attempt == 1
            assert task.error_code == "CONTEXT_MISSING"
            assert "source_images" not in task.input_data and "allowed_asset_ids" not in task.input_data
            frozen = deepcopy(task.input_data), task.input_hash
            # Advance the test clock past retry backoff; do not reset the pending task or attempt.
            after_backoff = as_utc(task.not_before) + timedelta(seconds=1)
        monkeypatch.setattr("glance_core.task_service.utc_now", lambda: after_backoff)
        retry = agent.claim()
        assert retry["task_id"] == first["task_id"]
        bundle = agent.bundle(retry["bundle_url"], retry["lease_token"])
        images = bundle["input"]["research"]["source_images"]
        assert len(images) == len(bundle["context_manifest"]) == 2
        for entry in bundle["context_manifest"]:
            download = http.get(bundle["context_files"][entry["index"]]["url"],
                                headers={"Authorization": f"Bearer {retry['lease_token']}"})
            assert download.status_code == 200
            assert hashlib.sha256(download.content).hexdigest() == entry["sha256"]
        result = deepcopy(SCRIPT)
        for row, image in zip(result["visual_requirements"], images, strict=True):
            row["preferred_source"] = image["artifact_id"]
        # The worker's real schema validator runs before the HTTP submission.
        import json
        from glance_agent.validator import validate_result
        workspace = tmp_path / "script-workspace"
        workspace.mkdir()
        (workspace / "result.json").write_text(json.dumps(result), encoding="utf-8")
        (workspace / "result.schema.json").write_text(json.dumps(bundle["result_schema"]), encoding="utf-8")
        _, errors = validate_result(workspace)
        assert errors == []
        completed = agent.complete(retry["task_id"], retry["lease_token"], "script-worker", result)
        assert completed["advanced"] == "CONSUMED"
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, retry["task_id"])
            assert task.status == TaskStatus.COMPLETED and task.attempt == 2
            assert (task.input_data, task.input_hash) == frozen
            assert len(session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id,
                                                            AuditEvent.action == "failed")).all()) == 1
            assert len(session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id,
                                                            AuditEvent.action == AGENT_CONSUMED)).all()) == 1
        qc_claim = agent.claim()
        assert qc_claim["task_type"] == TaskType.SCRIPT_QC.value
        qc_bundle = agent.bundle(qc_claim["bundle_url"], qc_claim["lease_token"])
        assert qc_bundle["input"]["source_images"] == images
        assert qc_bundle["input"]["research"]["source_images"] == images
        assert len(qc_bundle["context_manifest"]) == 2
        assert {row["input_ref"] for row in qc_bundle["context_manifest"]} == {row["artifact_id"] for row in images}
        for entry in qc_bundle["context_manifest"]:
            download = http.get(qc_bundle["context_files"][entry["index"]]["url"],
                                headers={"Authorization": f"Bearer {qc_claim['lease_token']}"})
            assert download.status_code == 200
            assert hashlib.sha256(download.content).hexdigest() == entry["sha256"]
        assert agent.complete(qc_claim["task_id"], qc_claim["lease_token"], "script-worker",
                               {"passed": True, "issues": []})["advanced"] == "CONSUMED"
        assistant = client_for(http, "assistant")
        review = assistant.claim()
        assert review["task_type"] == TaskType.SCRIPT_REVIEW_ASSIST.value
        review_bundle = assistant.bundle(review["bundle_url"], review["lease_token"])
        assert review_bundle["input"]["source_images"] == images
        assert len(review_bundle["context_manifest"]) == 2
        assert {row["input_ref"] for row in review_bundle["context_manifest"]} == {row["artifact_id"] for row in images}
        for entry in review_bundle["context_manifest"]:
            download = http.get(review_bundle["context_files"][entry["index"]]["url"],
                                headers={"Authorization": f"Bearer {review['lease_token']}"})
            assert download.status_code == 200
            assert hashlib.sha256(download.content).hexdigest() == entry["sha256"]
