"""Signed image references reach leased visual workers without changing task materials."""

from copy import deepcopy
from io import BytesIO
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlmodel import Session, select

from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, Content, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from glance_core.security import issue_role_token

from app.business.runtime import AGENT_CONSUMED, OWNED
from app.controllers.v1 import agent as agent_controller
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_deployment_http import app_for
from test.business.test_visual_plan_materialization import image_contract as image_contract, record_accepted_completion


@pytest.fixture
def visual_images(image_contract):
    runtime, content_id, old_plan_id, generation_id, _, _, _ = image_contract
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        generation = session.get(AgentTask, generation_id)
        artifacts, bodies = [], {}
        for index in range(5):
            stream = BytesIO()
            Image.new("RGB", (16, 24), (index * 40, 20, 200)).save(stream, format="PNG")
            body = stream.getvalue()
            digest = hashlib.sha256(body).hexdigest()
            relative = f"agent-assets/{generation_id}/{digest}.png"
            path = Path(content.content_dir) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            artifact = Artifact(content_id=content_id, artifact_type="agent_generated_image",
                                relative_path=relative, mime_type="image/png", sha256=digest,
                                size_bytes=len(body), producer_stage=generation_id)
            session.add(artifact)
            artifacts.append(artifact)
            bodies[artifact.id] = body
        generation.input_data = {**generation.input_data, "requests": [
            {"segment_index": index, "asset_request": {"prompt": f"concept {index}"}}
            for index in range(5)
        ]}
        generation.input_hash = stable_hash(generation.input_data)
        generation.result_data = {"images": [
            {"segment_index": index, "artifact_id": artifact.id}
            for index, artifact in enumerate(artifacts)
        ]}
        session.add(generation)
        submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation_id)).one()
        submission.result_hash = stable_hash(generation.result_data)
        Path(submission.raw_result_path).write_text(json.dumps(generation.result_data), encoding="utf-8")
        session.add(submission)
        for action in (OWNED, "completed", AGENT_CONSUMED):
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation_id,
                                                         AuditEvent.action == action)).one()
            event.metadata_json = ({"input_hash": generation.input_hash} if action == OWNED
                                   else {"result_hash": submission.result_hash})
            session.add(event)
        old_plan = session.get(AgentTask, old_plan_id)
        inputs = {**old_plan.input_data, "voice_duration_sec": 30, "image_only_gen_budget": 8,
                  "asset_image_enabled": True, "allowed_asset_ids": list(bodies),
                  "allowed_layouts": ["image_card"], "context_files": []}
        plan = runtime.workflow_service(session).create_agent_task(content_id, TaskType.VISUAL_PLAN,
                                                                  inputs, allow_multiple=True)
        result = {"segments": [
            {"asset_id": artifact.id, "layout": "image_card", "narration_start_sec": index * 6,
             "narration_end_sec": (index + 1) * 6, "purpose": f"concept {index}"}
            for index, artifact in enumerate(artifacts)
        ]}
        record_accepted_completion(session, runtime, plan, result)
        content.current_stage = "visual_plan"
        session.add(content)
        session.commit()
        plan_id = plan.id
    assert runtime.consume_agent(plan_id)
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                 AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
                                                 AgentTask.status == TaskStatus.PENDING)).one()
        qc_id = qc.id
    yield runtime, content_id, plan_id, qc_id, generation_id, old_plan_id, bodies


@pytest.mark.parametrize("prepared", [False, True])
def test_visual_qc_agent_downloads_all_five_exact_images_without_rewriting_input(visual_images, tmp_path, prepared, monkeypatch):
    runtime, _, plan_id, qc_id, _, _, bodies = visual_images
    with Session(runtime.engine) as session:
        plan, qc = session.get(AgentTask, plan_id), session.get(AgentTask, qc_id)
        if prepared:
            selection = deepcopy(plan.result_data)
            selection["segments"] = list(reversed(selection["segments"]))
            qc.input_data = {**qc.input_data, "prepared_visual_plan": selection,
                             "visual_plan_result_sha256": stable_hash(plan.result_data)}
            qc.input_hash = stable_hash(qc.input_data)
            session.add(qc)
            session.commit()
        frozen = deepcopy(qc.input_data)
        signed_hash, original = qc.input_hash, deepcopy(plan.result_data)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == qc_id
        token = claim["lease_token"]
        assert http.post("/api/v1/agent/tasks/claim", headers={
            "Authorization": f"Bearer {issue_role_token('test-root', 'image')}"
        }, json={"agent_id": client.settings.agent_id, "role_id": "visual"}).status_code == 403
        resolved_tasks = []
        original_resolver = agent_controller.context_files_for

        def resolve_once(task, session):
            resolved_tasks.append(task.id)
            return original_resolver(task, session)

        monkeypatch.setattr(agent_controller, "context_files_for", resolve_once)
        bundle = client.bundle(claim["bundle_url"], token)
        assert resolved_tasks == [qc_id]
        assert len(bundle["context_files"]) == len(bundle["context_manifest"]) == 5
        selected = frozen.get("prepared_visual_plan", original)
        assert bundle["input"]["visual_plan"] == selected
        assert [entry["input_ref"] for entry in bundle["context_manifest"]] == [
            segment["asset_id"] for segment in selected["segments"]
        ]
        for entry, manifest in zip(bundle["context_files"], bundle["context_manifest"], strict=True):
            assert entry["name"] == Path(entry["name"]).name
            destination = tmp_path / "worker" / "context" / entry["name"]
            client.download_context(entry["url"], token, destination)
            assert destination.read_bytes() == bodies[manifest["input_ref"]]
            assert hashlib.sha256(destination.read_bytes()).hexdigest() == manifest["sha256"]
            assert destination.stat().st_size == manifest["size_bytes"]
        for headers, status in (({}, 401), ({"Authorization": "Bearer wrong-lease"}, 409),
                                ({"Authorization": f"Bearer {client.settings.token.get_secret_value()}"}, 409)):
            assert http.get(bundle["context_files"][0]["url"], headers=headers).status_code == status
        with Session(runtime.engine) as session:
            qc, plan = session.get(AgentTask, qc_id), session.get(AgentTask, plan_id)
            assert qc.input_data == frozen and qc.input_hash == signed_hash == stable_hash(frozen)
            assert plan.result_data == original and qc.result_data is None


@pytest.mark.parametrize("tamper", [
    "foreign", "missing_artifact", "missing_file", "bytes", "type", "root", "input",
    "plan_result", "plan_input", "plan_submission", "plan_consumed", "plan_stale",
    "generation_submission", "generation_consumed", "origin_plan_owned", "collision", "collision_casefold",
    "qc_whitelist", "prepared_binding", "missing_parent",
])
def test_visual_context_bundle_and_direct_download_fail_closed(visual_images, tmp_path, tamper):
    runtime, content_id, plan_id, qc_id, generation_id, old_plan_id, bodies = visual_images
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == qc_id
        token = claim["lease_token"]
        with Session(runtime.engine) as session:
            artifact = session.get(Artifact, next(iter(bodies)))
            content, plan, qc = session.get(Content, content_id), session.get(AgentTask, plan_id), session.get(AgentTask, qc_id)
            target = Path(content.content_dir) / artifact.relative_path
            if tamper == "foreign":
                candidate = Candidate(source_type="agent", source_id="foreign",
                                      source_url="https://example.org/other", original_title="Other")
                session.add(candidate)
                session.flush()
                foreign = Content(candidate_id=candidate.id, title="Other",
                                  content_dir=str(tmp_path / "other-content"))
                session.add(foreign)
                session.flush()
                artifact.content_id = foreign.id
                session.add(artifact)
            elif tamper == "missing_artifact":
                session.delete(artifact)
            elif tamper == "missing_file":
                target.unlink()
            elif tamper == "bytes":
                target.write_bytes(b"tampered")
            elif tamper == "type":
                artifact.artifact_type = "publish_evidence"
                session.add(artifact)
            elif tamper == "root":
                artifact.relative_path = "../outside.png"
                session.add(artifact)
            elif tamper == "input":
                qc.input_data = {**qc.input_data, "changed": True}
                session.add(qc)
            elif tamper == "plan_result":
                plan.result_data = {**plan.result_data, "changed": True}
                session.add(plan)
            elif tamper == "plan_input":
                plan.input_data = {**plan.input_data, "changed": True}
                session.add(plan)
            elif tamper in {"plan_submission", "generation_submission"}:
                owner = plan_id if tamper == "plan_submission" else generation_id
                row = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == owner)).one()
                session.delete(row)
            elif tamper in {"plan_consumed", "generation_consumed", "origin_plan_owned"}:
                owner = {"plan_consumed": plan_id, "generation_consumed": generation_id,
                         "origin_plan_owned": old_plan_id}[tamper]
                action = OWNED if tamper == "origin_plan_owned" else AGENT_CONSUMED
                row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == owner,
                                                            AuditEvent.action == action)).one()
                session.delete(row)
            elif tamper == "plan_stale":
                runtime.workflow_service(session).create_agent_task(content_id, TaskType.VISUAL_PLAN,
                    {**plan.input_data, "revision": 2}, allow_multiple=True)
            elif tamper in {"collision", "collision_casefold"}:
                second = session.get(Artifact, list(bodies)[1])
                basename = target.name.upper() if tamper == "collision_casefold" else target.name
                second.relative_path = f"another/{basename}"
                duplicate = Path(content.content_dir) / second.relative_path
                duplicate.parent.mkdir(parents=True)
                duplicate.write_bytes(bodies[second.id])
                session.add(second)
            elif tamper == "qc_whitelist":
                qc.input_data = {**qc.input_data, "allowed_asset_ids": []}
                qc.input_hash = stable_hash(qc.input_data)
                session.add(qc)
            elif tamper == "prepared_binding":
                qc.input_data = {**qc.input_data, "prepared_visual_plan": deepcopy(plan.result_data),
                                 "visual_plan_result_sha256": "wrong"}
                qc.input_hash = stable_hash(qc.input_data)
                session.add(qc)
            elif tamper == "missing_parent":
                qc.input_data = {**qc.input_data, "upstream_task_ids": {}}
                qc.input_hash = stable_hash(qc.input_data)
                session.add(qc)
            session.commit()
        headers = {"Authorization": f"Bearer {token}"}
        assert http.get(claim["bundle_url"], headers=headers).status_code == 409
        assert http.get(f"/api/v1/agent/tasks/{qc_id}/context/0", headers=headers).status_code == 409
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, qc_id).status == TaskStatus.LEASED
            assert session.get(AgentTask, qc_id).result_data is None


def test_visual_plan_receives_declared_images(visual_images, tmp_path):
    runtime, content_id, plan_id, qc_id, _, _, bodies = visual_images
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, qc_id)
        qc.status = TaskStatus.CANCELLED
        session.add(qc)
        plan = session.get(AgentTask, plan_id)
        pending = runtime.workflow_service(session).create_agent_task(content_id, TaskType.VISUAL_PLAN,
            {**plan.input_data, "revision": 2}, allow_multiple=True)
        pending_id, frozen = pending.id, deepcopy(pending.input_data)
        session.commit()
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == pending_id
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        assert {entry["input_ref"] for entry in bundle["context_manifest"]} == set(bodies)
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, pending_id).input_data == frozen


@pytest.mark.parametrize("selection", ["subset", "future_generation", "source_image"])
def test_qc_context_uses_only_selected_existing_images(visual_images, tmp_path, selection):
    runtime, _, plan_id, qc_id, _, _, bodies = visual_images
    with Session(runtime.engine) as session:
        plan, qc = session.get(AgentTask, plan_id), session.get(AgentTask, qc_id)
        prepared = deepcopy(plan.result_data)
        first_id = next(iter(bodies))
        if selection == "source_image":
            artifact = session.get(Artifact, first_id)
            artifact.artifact_type, artifact.producer_stage = "source_image", "acquire_images"
            session.add(artifact)
            expected_ids = list(bodies)
        else:
            for artifact_id in list(bodies)[1:]:
                session.delete(session.get(Artifact, artifact_id))
            if selection == "subset":
                prepared["segments"] = prepared["segments"][:1]
                expected_ids = [first_id]
            else:
                for segment in prepared["segments"]:
                    segment.pop("asset_id")
                    segment["asset_request"] = {"prompt": "new future concept"}
                expected_ids = []
        qc.input_data = {**qc.input_data, "prepared_visual_plan": prepared,
                         "visual_plan_result_sha256": stable_hash(plan.result_data)}
        qc.input_hash = stable_hash(qc.input_data)
        session.add(qc)
        session.commit()
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == qc_id
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        assert [entry["input_ref"] for entry in bundle["context_manifest"]] == expected_ids
        for entry, artifact_id in zip(bundle["context_files"], expected_ids, strict=True):
            destination = tmp_path / "worker" / "context" / entry["name"]
            client.download_context(entry["url"], claim["lease_token"], destination)
            assert destination.read_bytes() == bodies[artifact_id]
