"""Historical image evidence uses authenticated bytes, never server-local source paths."""
import hashlib
import io
import json
from datetime import timedelta
from pathlib import Path
from zipfile import ZipFile, ZIP_STORED

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlmodel import Session, select

from glance_agent.image_preparation import prepare_image
from glance_agent.workspace import prepare_workspace
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, TaskStatus, TaskType, utc_now
from glance_core.task_service import stable_hash
from test.business.test_content_http import content_app
from test.business.test_discovery_http import ADMIN, make_runtime


def sha(data):
    return hashlib.sha256(data).hexdigest()


def archive(manifest, receipt, original, *, extra=None):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_STORED) as zipped:
        zipped.writestr("manifest.json", json.dumps(manifest).encode())
        zipped.writestr("preparation.json", json.dumps(receipt).encode())
        zipped.writestr("original.bin", original)
        if extra:
            for name, body in extra:
                zipped.writestr(name, body)
    return buffer.getvalue()


@pytest.fixture
def evidence_fixture(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    now = utc_now()
    inputs = {"requests": [{"segment_index": 0, "asset_request": {"information": "AI concept", "min_width": 1080, "min_height": 1920, "aspect_ratio": "9:16"}}]}
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        candidate, _ = flow.ingest_candidate(source_type="agent", source_id="prep", source_url="https://example.org/prep", original_title="prep")
        root = runtime.settings.content_dir / "evidence"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="evidence", content_dir=str(root), business_status=ContentStatus.PACKAGED, production_mode=ProductionMode.IMAGE_ONLY)
        session.add(content)
        session.commit()
        task = AgentTask(content_id=content.id, task_type=TaskType.ASSET_GENERATE_IMAGE, prompt_name="fixture", instructions="fixture", input_data=inputs, input_hash=stable_hash(inputs), status=TaskStatus.COMPLETED, attempt=1, assigned_agent_id="historical-image", completed_at=now + timedelta(seconds=4))
        session.add(task)
        session.commit()
        task_id, content_id = task.id, content.id
        directory = prepare_workspace(tmp_path / "sdk", lease_token="fixture-only", api_url="http://127.0.0.1:9", agent_id="historical-image", bundle={"task": {"task_id": task_id, "task_type": "asset_generate_image"}, "input": inputs, "result_schema": {}, "instructions": "fixture"})
        source = tmp_path / "native.png"
        Image.new("RGB", (940, 1672), "red").save(source)
        receipt = prepare_image(directory, task_id, "historical-image", source, 0)
        prepared = Path(receipt["path"]).read_bytes()
        image_path = root / "agent-assets" / task_id / (sha(prepared) + ".png")
        image_path.parent.mkdir(parents=True)
        image_path.write_bytes(prepared)
        image = Artifact(content_id=content_id, artifact_type="agent_generated_image", relative_path=image_path.relative_to(root).as_posix(), sha256=sha(prepared), size_bytes=len(prepared), mime_type="image/png", producer_stage=task_id, created_at=now + timedelta(seconds=2))
        session.add(image)
        session.commit()
        image_id = image.id
        result = {"images": [{"artifact_id": image_id, "segment_index": 0, "tool_name": "native_image", "prompt_summary": "AI concept", "width": 1080, "height": 1920, "contains_real_person": False, "contains_brand": False, "contains_text": False, "contains_watermark": False}]}
        task.result_data = result
        session.add(task)
        raw = runtime.settings.agent_submission_dir / task_id / "1" / "result.json"
        raw.parent.mkdir(parents=True)
        raw.write_bytes(json.dumps(result).encode())
        session.add(AgentSubmission(task_id=task_id, agent_id="historical-image", attempt=1, raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED", created_at=now + timedelta(seconds=3)))
        run_id = "run_" + "1" * 32
        request_id = "image-request"
        worker = {"task_id": task_id, "task_type": "asset_generate_image", "content_id": content_id, "input_hash": task.input_hash, "agent_id": "historical-image", "run_id": run_id, "run_generation": 7, "host_epoch": 1}
        def event(entity_type, entity_id, action, metadata, actor_type="system", actor_id="mpt-content-host", offset=1):
            row = AuditEvent(entity_type=entity_type, entity_id=entity_id, action=action, metadata_json=metadata, actor_type=actor_type, actor_id=actor_id, created_at=now + timedelta(seconds=offset))
            session.add(row)
            return row
        permission = event("content_run", run_id, "mpt_content_run_image_adaptation_permission_recorded", {"run_id": run_id, "generation": 7, "content_id": content_id, "allow_local_image_adaptation": True, "scope": "new_ai_concept_images_frozen_minimum_size_preserve_original_and_processing_record_independent_qc"}, "admin", "mpt-admin-api", -1)
        event("scheduler_offer", request_id, "mpt_content_run_offer", {"worker": worker, "worker_hash": stable_hash(worker)})
        event("agent_task", task_id, "mpt_content_run_claim", {"run_id": run_id, "generation": 7, "host_epoch": 1, "task_id": task_id, "request_id": request_id, "offer_hash": stable_hash(worker), "agent_id": "historical-image", "claim_id": "claim-fixture"})
        event("agent_task", task_id, "mpt_task_owned", {"input_hash": task.input_hash})
        event("agent_task", task_id, "completed", {"result_hash": stable_hash(result)}, "agent", "historical-image", 4)
        event("agent_task", task_id, "mpt_agent_consumed", {"result_hash": stable_hash(result)}, offset=5)
        event("artifact", image_id, "generated_artifact_uploaded", {"task_id": task_id, "mime_type": "image/png"}, "agent", "historical-image", 2)
        session.commit()
        manifest = {"version": 1, "task_id": task_id, "attempt": 1, "input_sha256": task.input_hash, "result_sha256": stable_hash(result), "accepted_raw_sha256": sha(raw.read_bytes()), "prepared_artifact_id": image_id, "prepared_sha256": image.sha256, "segment_index": 0, "run_id": run_id, "run_generation": 7, "permission_event_id": permission.id}
    return runtime, content_id, manifest, receipt, source.read_bytes(), root


def post(http, content_id, image_id, body, *, headers=ADMIN):
    return http.post(f"/api/v1/contents/{content_id}/image-preparations/{image_id}", headers={**headers, "Content-Type": "application/zip"}, content=body)


def test_import_registers_truthful_immutable_bytes_without_reopening_task(evidence_fixture):
    runtime, content_id, manifest, receipt, original, root = evidence_fixture
    body = archive(manifest, receipt, original)
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], body, headers={}).status_code == 401
        with Session(runtime.engine) as session:
            before = stable_hash(session.get(AgentTask, manifest["task_id"]).model_dump(mode="json"))
        first = post(http, content_id, manifest["prepared_artifact_id"], body)
        assert first.status_code == 200, first.text
        second = post(http, content_id, manifest["prepared_artifact_id"], body)
        assert second.status_code == 200 and second.json() == first.json()
        with Session(runtime.engine) as session:
            assert stable_hash(session.get(AgentTask, manifest["task_id"]).model_dump(mode="json")) == before
            originals = session.exec(select(Artifact).where(Artifact.content_id == content_id, Artifact.artifact_type == "image_preparation_original")).all()
            records = session.exec(select(Artifact).where(Artifact.content_id == content_id, Artifact.artifact_type == "image_preparation_receipt")).all()
            assert len(originals) == len(records) == 1
            assert (root / originals[0].relative_path).read_bytes() == original
            record = json.loads((root / records[0].relative_path).read_bytes())
            assert record["prepared_artifact_id"] == manifest["prepared_artifact_id"]
            assert record["source_width"] == 940 and record["source_height"] == 1672
            assert record["replay_verified"] is True and record["raw_input_bytes_verified"] is False
            assert record["source_receipt_sha256"] == sha(json.dumps(receipt).encode())
            assert "source_path" not in record and "path" not in record
            assert "license_verified" in record and record["license_verified"] is False
            assert session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all() == []


@pytest.mark.parametrize("field,value", [("input_sha256", "0" * 64), ("result_sha256", "0" * 64), ("accepted_raw_sha256", "0" * 64), ("prepared_sha256", "0" * 64), ("segment_index", 1), ("attempt", 2), ("prepared_artifact_id", "asset_" + "0" * 32), ("permission_event_id", "aud_" + "0" * 32), ("run_generation", 8), ("run_id", "run_" + "2" * 32), ("unexpected", True)])
def test_import_rejects_spoofed_manifest_without_registering(evidence_fixture, field, value):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    damaged = {**manifest, field: value}
    with TestClient(content_app(runtime)) as http:
        response = post(http, content_id, manifest["prepared_artifact_id"], archive(damaged, receipt, original))
        assert response.status_code == 409
        with Session(runtime.engine) as session:
            assert session.exec(select(Artifact).where(Artifact.artifact_type.in_(["image_preparation_original", "image_preparation_receipt"]))).all() == []


@pytest.mark.parametrize("field,value", [("source_sha256", "0" * 64), ("source_width", 941), ("width", 720), ("upscaled", False), ("method", "invented resize"), ("sha256", "0" * 64)])
def test_import_replays_instead_of_trusting_receipt_claims(evidence_fixture, field, value):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, {**receipt, field: value}, original)).status_code == 409


@pytest.mark.parametrize("damage", ["missing_permission", "permission_false", "permission_numeric", "wrong_permission_actor", "fake_accepted", "raw_changed", "consumed_actor", "prepared_changed"])
def test_import_revalidates_historical_authority_and_actual_bytes(evidence_fixture, damage):
    runtime, content_id, manifest, receipt, original, root = evidence_fixture
    with Session(runtime.engine) as session:
        permission = session.get(AuditEvent, manifest["permission_event_id"])
        if damage == "missing_permission":
            session.delete(permission)
        elif damage == "permission_false":
            permission.metadata_json = {**permission.metadata_json, "allow_local_image_adaptation": False}
            session.add(permission)
        elif damage == "permission_numeric":
            from sqlalchemy import update
            # ORM JSON equality treats True == 1; write the actual corrupt stored type explicitly.
            session.execute(update(AuditEvent).where(AuditEvent.id == permission.id).values(
                metadata_json={**permission.metadata_json, "allow_local_image_adaptation": 1}))
        elif damage == "wrong_permission_actor":
            permission.actor_type = "agent"
            session.add(permission)
        elif damage == "fake_accepted":
            accepted = session.exec(select(AgentSubmission)).one()
            accepted.result_hash = "0" * 64
            session.add(accepted)
        elif damage == "raw_changed":
            accepted = session.exec(select(AgentSubmission)).one()
            Path(accepted.raw_result_path).write_text("{}")
        elif damage == "consumed_actor":
            consumed = session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_agent_consumed")).one()
            consumed.actor_type = "agent"
            session.add(consumed)
        else:
            image = session.get(Artifact, manifest["prepared_artifact_id"])
            (root / image.relative_path).write_bytes(b"tampered")
        session.commit()
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, receipt, original)).status_code == 409


def test_system_cancelled_accepted_image_remains_valid_historical_origin(evidence_fixture):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, manifest["task_id"])
        task.status = TaskStatus.CANCELLED
        session.add(task)
        session.add(AuditEvent(actor_type="system", actor_id="fixture-system", entity_type="agent_task", entity_id=task.id, action="cancelled", created_at=utc_now() + timedelta(seconds=10)))
        session.commit()
    with TestClient(content_app(runtime)) as http:
        response = post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, receipt, original))
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, manifest["task_id"]).status == TaskStatus.CANCELLED


@pytest.mark.parametrize("extra", [[("../escape", b"x")], [("manifest.json", b"{}")], [("MANIFEST.JSON", b"{}")], [("nested/original.bin", b"x")]])
def test_import_refuses_extra_duplicate_or_traversal_members(evidence_fixture, extra):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, receipt, original, extra=extra)).status_code == 409


def test_import_rejects_zip_symlink_and_compression_and_invalid_json(evidence_fixture):
    import stat
    from zipfile import ZIP_DEFLATED, ZipInfo
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    with TestClient(content_app(runtime)) as http:
        for damage in ("symlink", "compression", "invalid_json", "duplicate_json"):
            body = io.BytesIO()
            with ZipFile(body, "w", compression=ZIP_DEFLATED if damage == "compression" else ZIP_STORED) as zipped:
                raw_manifest = json.dumps(manifest).encode()
                if damage == "invalid_json":
                    raw_manifest = b"{"
                elif damage == "duplicate_json":
                    raw_manifest = b'{"version":1,"version":1}'
                zipped.writestr("manifest.json", raw_manifest)
                zipped.writestr("preparation.json", json.dumps(receipt).encode())
                info = ZipInfo("original.bin")
                if damage == "symlink":
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                zipped.writestr(info, original)
            assert post(http, content_id, manifest["prepared_artifact_id"], body.getvalue()).status_code == 409


def test_upload_limit_counts_streamed_bytes_even_with_false_content_length(evidence_fixture, monkeypatch):
    from app.business import image_preparation_evidence as module
    runtime, content_id, manifest, _, _, _ = evidence_fixture
    assert module.MAX_BODY == 26 * 1024 * 1024
    monkeypatch.setattr(module, "MAX_BODY", 1024)
    with TestClient(content_app(runtime)) as http:
        response = http.post(f"/api/v1/contents/{content_id}/image-preparations/{manifest['prepared_artifact_id']}", headers={**ADMIN, "Content-Type": "application/zip", "Content-Length": "1"}, content=(b"x" * 512 for _ in range(3)))
        assert response.status_code == 413


def test_source_path_strings_are_not_resolved_or_read(evidence_fixture):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    receipt = {**receipt, "path": "Z:/definitely-absent/prepared.png", "source_path": "Z:/never-read/private.png"}
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, receipt, original)).status_code == 200


def test_immutable_registration_rejects_reparse_and_tampering(evidence_fixture, tmp_path):
    runtime, content_id, manifest, receipt, original, root = evidence_fixture
    external = tmp_path / "external-evidence"
    external.mkdir()
    target = root / "image-preparations"
    try:
        target.symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    with TestClient(content_app(runtime)) as http:
        body = archive(manifest, receipt, original)
        assert post(http, content_id, manifest["prepared_artifact_id"], body).status_code == 409
        assert list(external.iterdir()) == []
        target.unlink()
        first = post(http, content_id, manifest["prepared_artifact_id"], body)
        assert first.status_code == 200, first.text
        with Session(runtime.engine) as session:
            record = session.get(Artifact, first.json()["receipt_artifact_id"])
            (root / record.relative_path).write_bytes(b"tampered")
        assert post(http, content_id, manifest["prepared_artifact_id"], body).status_code == 409


def test_eight_imports_refresh_one_normal_review_and_real_sdk_context(evidence_fixture, tmp_path):
    from glance_agent.cli import _download_context, _manifest_gaps
    from glance_core.content_acceptance import generated_image_material
    from app.business import pipeline
    from test.business.test_content_http import client_for

    runtime, content_id, manifest, receipt, original, root = evidence_fixture
    runtime.settings.archive_enabled = False
    imports = []
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        def accepted_task(kind, inputs, result):
            task = AgentTask(content_id=content_id, task_type=kind, prompt_name="fixture", instructions="fixture", input_data=inputs, input_hash=stable_hash(inputs), result_data=result, status=TaskStatus.COMPLETED, attempt=1, assigned_agent_id="fixture", completed_at=utc_now())
            session.add(task)
            session.commit()
            seal(task)
            return task
        def seal(task):
            raw = runtime.settings.agent_submission_dir / task.id / "1/result.json"
            raw.parent.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(json.dumps(task.result_data).encode())
            session.add(AgentSubmission(task_id=task.id, agent_id=task.assigned_agent_id, attempt=1, raw_result_path=str(raw), result_hash=stable_hash(task.result_data), validation_status="ACCEPTED"))
            for action, data, actor in [("mpt_task_owned", {"input_hash": task.input_hash}, "system"), ("mpt_agent_consumed", {"result_hash": stable_hash(task.result_data)}, "system"), ("completed", {"result_hash": stable_hash(task.result_data)}, "agent")]:
                session.add(AuditEvent(actor_type=actor, actor_id=task.assigned_agent_id if actor == "agent" else "fixture-system", entity_type="agent_task", entity_id=task.id, action=action, metadata_json=data))
            session.commit()
        research = accepted_task(TaskType.CONTENT_RESEARCH, {}, {"primary_sources": [], "secondary_sources": []})
        script = accepted_task(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}}, {"cover_title": "共享专注工作舱", "full_narration": "test"})
        accepted_task(TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}}, {"passed": True})
        plan = AgentTask(content_id=content_id, task_type=TaskType.VISUAL_PLAN, prompt_name="fixture", instructions="fixture", input_data={"upstream_task_ids": {"script": script.id}}, input_hash="", result_data={}, status=TaskStatus.COMPLETED, attempt=1, assigned_agent_id="fixture", completed_at=utc_now())
        plan.input_hash = stable_hash(plan.input_data)
        session.add(plan)
        session.commit()
        images = []
        for index in range(8):
            source_task = session.get(AgentTask, manifest["task_id"])
            task_input = {"requests": source_task.input_data["requests"], **({"upstream_task_ids": {"visual_plan": plan.id}} if index < 7 else {"asset_purpose": "cover"})}
            task = AgentTask(content_id=content_id, task_type=TaskType.ASSET_GENERATE_IMAGE, prompt_name="fixture", instructions="fixture", input_data=task_input, input_hash=stable_hash(task_input), result_data={}, status=TaskStatus.COMPLETED, attempt=1, assigned_agent_id="historical-image", completed_at=source_task.completed_at)
            session.add(task)
            session.commit()
            prepared_source = session.get(Artifact, manifest["prepared_artifact_id"])
            image_path = root / "accepted-images" / str(index) / "image.png"
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes((root / prepared_source.relative_path).read_bytes())
            image = Artifact(content_id=content_id, artifact_type="agent_generated_image", relative_path=image_path.relative_to(root).as_posix(), sha256=prepared_source.sha256, size_bytes=prepared_source.size_bytes, mime_type="image/png", producer_stage=task.id, created_at=prepared_source.created_at)
            session.add(image)
            session.commit()
            result = {"images": [{**source_task.result_data["images"][0], "artifact_id": image.id}]}
            task.result_data = result
            session.add(task)
            session.commit()
            seal(task)
            accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
            # Keep the historical claim/upload ordering used by the actual import contract.
            accepted.created_at = source_task.completed_at - timedelta(seconds=1)
            session.add(accepted)
            request_id = f"import-{index}"
            worker = {"task_id": task.id, "task_type": "asset_generate_image", "content_id": content_id, "input_hash": task.input_hash, "agent_id": "historical-image", "run_id": manifest["run_id"], "run_generation": manifest["run_generation"], "host_epoch": 1}
            for entity_type, entity_id, action, data, actor_type, actor_id in [
                ("scheduler_offer", request_id, "mpt_content_run_offer", {"worker": worker, "worker_hash": stable_hash(worker)}, "system", "mpt-content-host"),
                ("agent_task", task.id, "mpt_content_run_claim", {"run_id": manifest["run_id"], "generation": manifest["run_generation"], "host_epoch": 1, "task_id": task.id, "agent_id": "historical-image", "request_id": request_id, "offer_hash": stable_hash(worker)}, "system", "mpt-content-host"),
                ("artifact", image.id, "generated_artifact_uploaded", {"task_id": task.id, "mime_type": "image/png"}, "agent", "historical-image"),
            ]:
                session.add(AuditEvent(actor_type=actor_type, actor_id=actor_id, entity_type=entity_type, entity_id=entity_id, action=action, metadata_json=data, created_at=prepared_source.created_at - timedelta(seconds=1)))
            session.commit()
            raw = Path(accepted.raw_result_path).read_bytes()
            bound = {**manifest, "task_id": task.id, "input_sha256": task.input_hash, "result_sha256": stable_hash(result), "accepted_raw_sha256": sha(raw), "prepared_artifact_id": image.id, "prepared_sha256": image.sha256}
            imports.append((bound, {**receipt, "task_id": task.id}, original))
            images.append(image)
        plan.result_data = {"segments": [{"asset_id": image.id, "layout": "image_card"} for image in images[:7]]}
        session.add(plan)
        session.commit()
        seal(plan)
        accepted_task(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id, "script": script.id}}, {"passed": True})
        content = session.get(Content, content_id)
        assets = [{"segment_index": i, **generated_image_material(session, content, image.id)} for i, image in enumerate(images[:7])]
        accepted_task(TaskType.ASSET_QC, {"upstream_task_ids": {"visual_plan": plan.id}, "full_generated_assets": True, "assets": assets}, {"passed": True})
        final_path = root / "render/current/final.mp4"
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes(b"frozen final")
        for kind, relative in [("final_video", "render/current/final.mp4"), ("publish_video", "publish/final.mp4")]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"frozen final")
            artifact = Artifact(content_id=content_id, artifact_type=kind, relative_path=relative, sha256=sha(b"frozen final"), size_bytes=len(b"frozen final"), mime_type="video/mp4", producer_stage="render")
            session.add(artifact)
            session.commit()
            if kind == "final_video":
                final_id = artifact.id
        accepted_task(TaskType.CONTENT_QC, {"video_artifact_id": final_id, "upstream_task_ids": {"script": script.id, "visual_plan": plan.id}}, {"passed": True})
        cover = pipeline._build_and_register_cover(session, runtime.settings, content, base_image=root / images[-1].relative_path, base_artifact_id=images[-1].id, action="cover_ai_upgraded")
        pipeline._queue_post_package_tasks(session, flow, content, runtime.settings, script.result_data, research.result_data, cover_artifact_id=cover.id)
        prepare = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_PREPARE)).one()
        prepare.status, prepare.result_data = TaskStatus.COMPLETED, {"description_zh": "fixture"}
        session.add(prepare)
        session.commit()
        prepare_id = prepare.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, prepare_id)
    with Session(runtime.engine) as session:
        old = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).one()
        assert old.input_data["production_evidence"]["local_adaptation"]["complete"] is False
        old.status, old.result_data = TaskStatus.COMPLETED, {"verdict": "BLOCKED"}
        session.add(old)
        session.commit()
        session.refresh(old)
        old_id, old_hash = old.id, stable_hash(old.model_dump(mode="json"))
    with TestClient(content_app(runtime), base_url="http://127.0.0.1:8010") as http:
        for index, (bound, claimed, source) in enumerate(imports):
            response = post(http, content_id, bound["prepared_artifact_id"], archive(bound, claimed, source))
            assert response.status_code == 200, response.text
            with Session(runtime.engine) as session:
                adaptation = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)["production_evidence"]["local_adaptation"]
                assert adaptation["complete"] is (index == 7)
                assert len(adaptation["records"]) == index + 1
                assert len(session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all()) == 1
        for _ in range(2):
            response = http.post(f"/api/v1/contents/{content_id}/rebuild-cover", headers=ADMIN, json={})
            assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            reviews = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all()
            assert len(reviews) == 2
            fresh = next(task for task in reviews if task.id != old_id)
            assert fresh.input_data["production_evidence"]["local_adaptation"]["complete"] is True
            assert stable_hash(session.get(AgentTask, old_id).model_dump(mode="json")) == old_hash
            fresh_id = fresh.id
        assistant = client_for(http, "assistant")
        claim = assistant.dispatch(wait=0, task_id=fresh_id)
        assert claim["should_run"]
        assistant.confirm_claim(fresh_id, claim["lease_token"], assistant.settings.agent_id)
        bundle = assistant.bundle(claim["bundle_url"], claim["lease_token"])
        workspace = tmp_path / "review-sdk"
        (workspace / "context").mkdir(parents=True)
        _download_context(assistant, bundle, claim["lease_token"], workspace)
        assert _manifest_gaps(bundle, workspace) == []
        names = [item["name"] for item in bundle["context_files"]]
        assert len(names) == len({name.casefold() for name in names})
        assert sum(name.startswith("preparation-") for name in names) == 8
        assert any(name.startswith("original-") for name in names)


def test_registered_audit_actor_tampering_is_not_idempotent_success(evidence_fixture):
    runtime, content_id, manifest, receipt, original, _ = evidence_fixture
    body = archive(manifest, receipt, original)
    with TestClient(content_app(runtime)) as http:
        assert post(http, content_id, manifest["prepared_artifact_id"], body).status_code == 200
        with Session(runtime.engine) as session:
            event = session.exec(select(AuditEvent).where(AuditEvent.action == "image_preparation_registered")).one()
            event.actor_type = "agent"
            session.add(event)
            session.commit()
        assert post(http, content_id, manifest["prepared_artifact_id"], body).status_code == 409
