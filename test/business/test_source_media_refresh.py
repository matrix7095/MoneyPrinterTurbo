"""A source supplement uses a new accepted lineage, never rewritten research or rights."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, Content, ContentStatus,
    ProductionMode, PublicationMode, PublicationRecord, RuntimeSetting, SchedulerOffer,
    StageRun, StageStatus, TaskStatus, TaskType, utc_now,
)
from glance_core.task_service import stable_hash
from app.business.runtime import AGENT_CONSUMED, OWNED
from app.business.source_media_refresh import REFRESH
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_content_http import RESEARCH
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN, make_runtime


@pytest.fixture
def subject(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        researcher = registered_client(http, runtime, "research")
        with Session(runtime.engine) as session:
            candidate, _ = runtime.workflow_service(session).ingest_candidate(
                source_type="agent", source_id="source-refresh", source_url="https://example.org/product",
                original_title="Product")
            content = Content(candidate_id=candidate.id, title="Product", content_dir=str(tmp_path / "content"),
                              production_mode=ProductionMode.IMAGE_ONLY,
                              business_status=ContentStatus.PUBLISHED, script_version=5)
            session.add(content)
            session.commit()
            old = runtime.workflow_service(session).create_agent_task(content.id, TaskType.CONTENT_RESEARCH,
                                                                      {"historical": True})
            old.status, old.result_data = TaskStatus.COMPLETED, {"historical": True}
            old_publication = PublicationRecord(content_id=content.id, platform="douyin", mode=PublicationMode.EXECUTOR,
                                                status="PUBLISHED", external_id="7698443917590629670",
                                                external_url="https://www.douyin.com/video/7698443917590629670")
            session.add(old)
            session.add(old_publication)
            session.commit()
            cid, old_id, pub_id = content.id, old.id, old_publication.id
        revision = http.post(f"/api/v1/contents/{cid}/published-revisions", headers=ADMIN,
            json={"request_key": "rework", "expected_revision_id": None, "publication_id": pub_id,
                  "feedback": "Use original media after verifying its publication permission."})
        assert revision.status_code == 200, revision.text
        assert http.post(f"/api/v1/contents/{cid}/prepare", headers=ADMIN).status_code == 200
        claim = researcher.claim()
        assert claim["status"] == "CLAIMED", claim
        response = researcher.complete(claim["task_id"], claim["lease_token"], researcher.settings.agent_id, RESEARCH)
        assert response["advanced"] == "CONSUMED", response
        with Session(runtime.engine) as session:
            pending = session.exec(select(AgentTask).where(AgentTask.content_id == cid,
                AgentTask.task_type == TaskType.SHORT_SCRIPT, AgentTask.status == TaskStatus.PENDING)).one()
            ids = {"content": cid, "head": revision.json()["revision_id"], "research": claim["task_id"],
                   "script": pending.id, "old": old_id, "publication": pub_id}
        yield runtime, http, researcher, ids


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/source-media-refresh"


def payload(ids, **updates):
    return {"request_key": "new-sources", "expected_revision_id": ids["head"],
            "research_task_id": ids["research"], "feedback": "New official media or permission may be available; verify it independently.",
            **updates}


def rows(runtime):
    with Session(runtime.engine) as session:
        return {(model.__tablename__, row.key if model is RuntimeSetting else row.id): row.model_dump(mode="json")
                for model in (AgentTask, Artifact, PublicationRecord, RuntimeSetting, Candidate)
                for row in session.exec(select(model)).all()}


def test_authenticated_refresh_preserves_history_and_uses_real_research_to_script_lineage(subject):
    runtime, http, researcher, ids = subject
    before = rows(runtime)
    assert http.post(endpoint(ids), json=payload(ids)).status_code == 401
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 200, response.text
    replacement_id = response.json()["research_task_id"]
    assert response.json() == {"content_id": ids["content"], "revision_id": ids["head"],
                               "research_task_id": replacement_id, "replayed": False}
    with Session(runtime.engine) as session:
        replacement = session.get(AgentTask, replacement_id)
        assert replacement.revision_id == ids["head"] and replacement.attempt == 0
        assert replacement.input_data["source_media_refresh_of"] == ids["research"]
        assert replacement.input_data["source_media_refresh_request"] == payload(ids)
        assert replacement.input_data["source_media_priority_version"] == 1
        assert "许可" in replacement.input_data["source_media_refresh_verification"]
        assert stable_hash(replacement.input_data) == replacement.input_hash
        assert session.get(AgentTask, ids["script"]).status == TaskStatus.CANCELLED
        assert session.get(AgentTask, ids["script"]).attempt == 0
        assert session.get(Content, ids["content"]).script_version == 5
        cancelled = session.exec(select(AuditEvent).where(AuditEvent.entity_id == ids["script"],
                                                         AuditEvent.action == "cancelled")).one()
        assert cancelled.actor_id == "source-media-refresh"
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == replacement_id,
                                                    AuditEvent.action == OWNED)).one().metadata_json == {
                                                        "input_hash": replacement.input_hash}
    after = rows(runtime)
    assert all(after[key] == value for key, value in before.items() if key != ("agent_task", ids["script"]))
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["replayed"] is True
    claim = researcher.claim()
    assert claim["task_id"] == replacement_id
    bundle = researcher.bundle(claim["bundle_url"], claim["lease_token"])
    assert bundle["input"]["source_media_refresh_request"]["feedback"] == payload(ids)["feedback"]
    assert researcher.complete(replacement_id, claim["lease_token"], researcher.settings.agent_id, RESEARCH)["advanced"] == "CONSUMED"
    with Session(runtime.engine) as session:
        scripts = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"],
                                                       AgentTask.task_type == TaskType.SHORT_SCRIPT)).all()
        assert len(scripts) == 2
        new = next(row for row in scripts if row.id != ids["script"])
        assert new.status == TaskStatus.PENDING and new.revision_id == ids["head"]
        assert new.input_data["upstream_task_ids"]["research"] == replacement_id
        assert session.get(AgentTask, ids["research"]).model_dump(mode="json") == before[("agent_task", ids["research"])]
        candidate = session.get(Candidate, session.get(Content, ids["content"]).candidate_id)
        assert candidate.rights_status.value == "OFFICIAL_REFERENCE"
        assert session.get(AgentTask, replacement_id).result_data["source_images"] == []
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["research_task_id"] == replacement_id


@pytest.mark.parametrize("field,value", [
    ("request_key", "   "), ("feedback", "\n"), ("expected_revision_id", ""),
    ("expected_revision_id", None), ("research_task_id", 123), ("path", "C:/arbitrary/file"),
    ("command", "anything"), ("revision_id", "fake"),
])
def test_strict_request_has_no_file_command_or_worker_revision_input(subject, field, value):
    runtime, http, _, ids = subject
    before = rows(runtime)
    assert http.post(endpoint(ids), headers=ADMIN, json={**payload(ids), field: value}).status_code == 422
    assert rows(runtime) == before


@pytest.mark.parametrize("changed", ["feedback", "research_task_id", "expected_revision_id"])
def test_idempotency_rejects_changed_payload_and_cannot_create_a_second_task(subject, changed):
    runtime, http, _, ids = subject
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).status_code == 200
    before = rows(runtime)
    response = http.post(endpoint(ids), headers=ADMIN, json={**payload(ids), changed: "different"})
    assert response.status_code == 409 and "REQUEST_KEY_CHANGED" in response.text
    assert rows(runtime) == before
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids, request_key="another")).status_code == 409


@pytest.mark.parametrize("defect", [
    "head", "old_research", "unconsumed", "raw_result", "input", "completed_receipt", "submission_hash",
    "script_attempt", "script_lease", "script_completed", "script_input", "script_submission", "script_bad_owned",
    "script_bad_created", "script_duplicate_created", "script_bad_normalization", "script_claim_history",
    "script_stage", "published", "footage", "offer", "active_stage", "artifact", "publication", "other_lease", "intent",
])
def test_unsafe_or_changed_state_is_rejected_atomically(subject, defect):
    runtime, http, _, ids = subject
    request = payload(ids)
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        research = session.get(AgentTask, ids["research"])
        script = session.get(AgentTask, ids["script"])
        if defect == "head":
            request["expected_revision_id"] = "stale"
        elif defect == "old_research":
            request["research_task_id"] = ids["old"]
        elif defect == "unconsumed":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                                  AuditEvent.action == AGENT_CONSUMED)).one())
        elif defect == "raw_result":
            raw = runtime.settings.agent_submission_dir / research.id / "1" / "result.json"
            raw.write_text("{}", encoding="utf-8")
        elif defect == "input":
            research.input_data = {**research.input_data, "tampered": True}
        elif defect == "completed_receipt":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                                  AuditEvent.action == "completed")).one())
        elif defect == "submission_hash":
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == research.id)).one()
            submission.result_hash = "bad"
            session.add(submission)
        elif defect == "script_attempt":
            script.attempt = 1
        elif defect == "script_lease":
            script.status, script.assigned_agent_id = TaskStatus.LEASED, "worker"
        elif defect == "script_completed":
            script.status, script.result_data = TaskStatus.COMPLETED, {"full_script": "completed"}
        elif defect == "script_input":
            script.input_data = {"upstream_task_ids": {"research": "other"}}
        elif defect == "script_submission":
            session.add(AgentSubmission(task_id=script.id, agent_id="someone", attempt=0, raw_result_path="unknown",
                                        result_hash="bad", validation_status="REJECTED"))
        elif defect == "script_bad_owned":
            session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task",
                                   entity_id=script.id, action=OWNED, metadata_json={"input_hash": "bad"}))
        elif defect == "script_bad_created":
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == script.id,
                                                          AuditEvent.action == "created")).one()
            event.actor_id = "fake"
            session.add(event)
        elif defect == "script_duplicate_created":
            session.add(AuditEvent(actor_type="system", actor_id="task-service", entity_type="agent_task",
                                   entity_id=script.id, action="created"))
        elif defect == "script_bad_normalization":
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == script.id,
                                                          AuditEvent.action == "task_input_normalized")).one()
            event.metadata_json = {"fields": []}
            session.add(event)
        elif defect == "script_claim_history":
            session.add(AuditEvent(actor_type="agent", actor_id="worker", entity_type="agent_task",
                                   entity_id=script.id, action="claimed"))
        elif defect == "script_stage":
            content.current_stage = "content_qc"
        elif defect == "published":
            content.business_status = ContentStatus.PUBLISHED
        elif defect == "footage":
            content.production_mode = ProductionMode.FOOTAGE
        elif defect == "offer":
            session.add(SchedulerOffer(request_id="active", task_id=script.id, expires_at=utc_now() + timedelta(minutes=3)))
        elif defect == "active_stage":
            session.add(StageRun(content_id=content.id, revision_id=ids["head"], stage="render",
                                 status=StageStatus.RUNNING, input_hash="x"))
        elif defect == "artifact":
            session.add(Artifact(content_id=content.id, revision_id=ids["head"], artifact_type="voice",
                                 relative_path="voice.mp3", sha256="a" * 64, size_bytes=1,
                                 mime_type="audio/mpeg", producer_stage="voice"))
        elif defect == "publication":
            session.add(PublicationRecord(content_id=content.id, revision_id=ids["head"], platform="douyin",
                                          mode=PublicationMode.EXECUTOR, status="PUBLISHED", external_id="new"))
        elif defect == "other_lease":
            task = runtime.workflow_service(session).create_agent_task(content.id, TaskType.CONTENT_RESEARCH,
                                                                       {"other": True}, allow_multiple=True)
            task.status = TaskStatus.LEASED
            session.add(task)
        else:
            session.add(AuditEvent(actor_type="agent", actor_id="worker", entity_type="publish_intent", entity_id="uncertain",
                                   action="publish_intent_state", metadata_json={"content_id": content.id, "status": "UNKNOWN"}))
        session.add(content)
        session.add(research)
        session.add(script)
        session.commit()
    before = rows(runtime)
    response = http.post(endpoint(ids), headers=ADMIN, json=request)
    assert response.status_code == 409, response.text
    assert rows(runtime) == before
    with Session(runtime.engine) as session:
        assert session.exec(select(AuditEvent).where(AuditEvent.action == REFRESH)).all() == []


def test_cancel_and_creation_rollback_together_when_new_task_creation_fails(subject, monkeypatch):
    from glance_core.workflow import WorkflowService

    runtime, http, _, ids = subject
    before = rows(runtime)
    def fail(*args, **kwargs):
        raise ValueError("injected task creation failure")
    monkeypatch.setattr(WorkflowService, "create_agent_task", fail)
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).status_code == 409
    assert rows(runtime) == before
    with Session(runtime.engine) as session:
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == ids["script"],
                                                    AuditEvent.action == "cancelled")).all() == []


def test_legacy_head_and_fixed_source_images_are_rejected_without_overwriting_bytes(subject):
    runtime, http, _, ids = subject
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.current_revision_id = None
        source = Path(content.content_dir) / "source_images" / "source-001.png"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"immutable legacy source image at its original fixed path")
        digest = sha256(source.read_bytes()).hexdigest()
        session.add(Artifact(content_id=content.id, artifact_type="source_image",
                             relative_path="source_images/source-001.png", sha256=digest,
                             size_bytes=source.stat().st_size, mime_type="image/png", producer_stage="legacy"))
        session.add(content)
        session.commit()
    before = rows(runtime)
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 409 and "BUSINESS_REVISION_REQUIRED" in response.text
    assert rows(runtime) == before
    assert sha256(source.read_bytes()).hexdigest() == digest
    with Session(runtime.engine) as session:
        assert session.exec(select(AuditEvent).where(AuditEvent.action == REFRESH)).all() == []


def test_genuine_pending_ownership_marker_is_validated_when_present(subject):
    from app.controllers.v1.agent import mark_owned

    runtime, http, _, ids = subject
    with Session(runtime.engine) as session:
        mark_owned(session, session.get(AgentTask, ids["script"]))
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).status_code == 200


def test_active_run_blocks_and_stopped_run_budget_is_preserved(subject):
    from app.business.content_run import load_run, save_run

    runtime, http, _, ids = subject
    assert http.post(f"/api/v1/contents/{ids['content']}/run", headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        run = load_run(session)
        run.ready_rounds = 3
        save_run(session, run, "fixture_budget_already_used")
        session.commit()
    before = rows(runtime)
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 409 and "WORK_IN_FLIGHT" in response.text
    assert rows(runtime) == before
    assert http.post(f"/api/v1/contents/{ids['content']}/run/stop", headers=ADMIN).status_code == 200
    stopped = rows(runtime)
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).status_code == 200
    after = rows(runtime)
    assert all(after[key] == value for key, value in stopped.items() if key[0] == "runtime_setting")
    with Session(runtime.engine) as session:
        assert load_run(session).ready_rounds == 3


def test_exact_replay_after_head_changed_returns_history_without_mutating_head(subject):
    runtime, http, _, ids = subject
    first = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert first.status_code == 200
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.current_revision_id = "a-later-head"
        session.add(content)
        session.commit()
    before = rows(runtime)
    replay = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert replay.json()["research_task_id"] == first.json()["research_task_id"]
    assert rows(runtime) == before
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).current_revision_id == "a-later-head"
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids, request_key="new")).status_code == 409


def test_replay_rejects_changed_receipt_or_orphan_research(subject):
    runtime, http, _, ids = subject
    first = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert first.status_code == 200
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, first.json()["research_task_id"])
        old_hash = task.input_hash
        task.input_hash = "tampered"
        session.add(task)
        session.commit()
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 409 and "RECEIPT_CHANGED" in response.text
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, first.json()["research_task_id"])
        task.input_hash = old_hash
        session.add(task)
        session.delete(session.exec(select(AuditEvent).where(AuditEvent.action == REFRESH)).one())
        content = session.get(Content, ids["content"])
        content.current_stage = "short_script"
        session.add(content)
        session.commit()
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 409 and "RECEIPT_MISSING" in response.text


def test_concurrent_same_key_returns_one_research_and_different_key_is_rejected(subject):
    runtime, http, _, ids = subject
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: http.post(endpoint(ids), headers=ADMIN, json=payload(ids)), range(2)))
    assert [result.status_code for result in results] == [200, 200]
    assert sorted(result.json()["replayed"] for result in results) == [False, True]
    assert len({result.json()["research_task_id"] for result in results}) == 1
    before = rows(runtime)
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids, request_key="different")).status_code == 409
    assert rows(runtime) == before


def test_concurrent_different_keys_admit_one_refresh(subject):
    runtime, http, _, ids = subject
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: http.post(endpoint(ids), headers=ADMIN, json=payload(ids, request_key=key)),
                                ["one", "two"]))
    assert sorted(result.status_code for result in results) == [200, 409]
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == REFRESH)).all()) == 1
