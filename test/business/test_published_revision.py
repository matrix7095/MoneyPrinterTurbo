"""Published rework appends a new head; legacy bytes and public evidence stay frozen."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from io import BytesIO
from pathlib import Path
import shutil
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlmodel import Session, select

from glance_core.content_revision import current_revision_clause, revision_work_root
from glance_core.database import create_db
from glance_core.models import (
    AgentTask, Artifact, AuditEvent, Content, ContentRevision, ContentStatus,
    PublicationMode, PublicationRecord, SchedulerOffer, StageRun, StageStatus,
    TaskStatus, TaskType, RightsStatus, utc_now,
)
from glance_core.task_service import TaskConflictError, publish_target_for_task, stable_hash
from app.business import pipeline
from app.business.media_executor import MptMediaExecutor
from test.business.test_content_http import content_app
from test.business.test_discovery_http import ADMIN, make_runtime


@pytest.fixture
def published(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            candidate, _ = flow.ingest_candidate(source_type="agent", source_id="published-revision",
                source_url="https://example.org/revision", original_title="Revision product",
                rights_status=RightsStatus.EXPLICIT_LICENSE)
            root = runtime.settings.content_dir / "published-revision"
            root.mkdir(parents=True)
            content = Content(candidate_id=candidate.id, title="Revision product", content_dir=str(root),
                              business_status=ContentStatus.PUBLISHED, current_stage="published", script_version=7)
            session.add(content)
            session.commit()
            tasks = []
            for kind in (TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC,
                         TaskType.VISUAL_PLAN, TaskType.CONTENT_QC):
                task = flow.create_agent_task(content.id, kind, {"fixture": "immutable old input"})
                task.status, task.result_data = TaskStatus.COMPLETED, {"passed": True, "old": True}
                session.add(task)
                session.commit()
                tasks.append(task.id)
            for kind, relative in (("source_image", "source_images/source-001.png"),
                                   ("voice", "audio/old.mp3"), ("final_video", "render/old.mp4")):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"immutable {kind}".encode())
                pipeline.register_artifact(session, content=content, executor=MptMediaExecutor(root), path=path,
                                           artifact_type=kind, stage="fixture")
            publication = PublicationRecord(content_id=content.id, platform="douyin", mode=PublicationMode.EXECUTOR,
                                             status="PUBLISHED", external_id="old-public-id",
                                             external_url="https://www.douyin.com/video/old-public-id")
            session.add(publication)
            session.commit()
            ids = {"content": content.id, "publication": publication.id, "tasks": tasks}
        yield runtime, http, ids


def payload(ids, key="request-one", head=None):
    return {"request_key": key, "expected_revision_id": head, "publication_id": ids["publication"],
            "feedback": "优先产品官方素材，AI仅补缺口；配音+50%，更自然；运动60fps且连续。"}


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/published-revisions"


def snapshot(runtime, cid):
    with Session(runtime.engine) as session:
        rows = {}
        for model in (AgentTask, Artifact, PublicationRecord):
            for row in session.exec(select(model).where(model.content_id == cid)).all():
                rows[(model.__tablename__, row.id)] = row.model_dump(mode="json")
        return rows


def test_revision_cas_replay_and_new_head_do_not_change_old_rows(published):
    runtime, http, ids = published
    before = snapshot(runtime, ids["content"])
    assert http.post(endpoint(ids), json=payload(ids)).status_code == 401
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 200, response.text
    revision_id = response.json()["revision_id"]
    assert response.json()["revision_no"] == 1
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["replayed"]
    assert http.post(endpoint(ids), headers=ADMIN, json=payload(ids, "different")).status_code == 409
    changed = {**payload(ids), "feedback": "changed request using same key"}
    assert http.post(endpoint(ids), headers=ADMIN, json=changed).status_code == 409
    assert snapshot(runtime, ids["content"]) == before
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        revision = session.get(ContentRevision, revision_id)
        assert (content.business_status, content.current_stage, content.script_version) == (
            ContentStatus.PREPARING_CONTEXT, "research", 7)
        assert content.content_dir == revision.snapshot_json["content"]["content_dir"]
        assert revision.subject_sha256 == stable_hash(revision.snapshot_json)
        assert revision.snapshot_json["publications"][0]["external_id"] == "old-public-id"
        flow = runtime.workflow_service(session)
        assert all(flow.completed_task_for(content.id, kind) is None for kind in (
            TaskType.CONTENT_RESEARCH, TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC,
            TaskType.VISUAL_PLAN, TaskType.CONTENT_QC))
        new = flow.create_agent_task(content.id, TaskType.CONTENT_RESEARCH, {"fixture": "immutable old input"})
        assert new.id not in ids["tasks"] and new.revision_id == revision_id
        assert new.input_data["content_revision_id"] == revision_id
        assert new.input_data["content_revision"]["feedback"] == payload(ids)["feedback"]
        assert stable_hash(new.input_data) == new.input_hash
        with pytest.raises(TaskConflictError, match="UPSTREAM"):
            flow.create_agent_task(content.id, TaskType.SHORT_SCRIPT,
                                   {"upstream_task_ids": {"research": ids["tasks"][0]}})
        with pytest.raises(ValueError, match="CONTENT_REVISION_STALE"):
            pipeline._unchanged_material_task(session, session.get(AgentTask, ids["tasks"][1]))
        with pytest.raises(Exception, match="Current script_qc"):
            pipeline._require_current_qc(session, content.id, TaskType.SCRIPT_QC, {})
        assert session.exec(select(Artifact).where(current_revision_clause(Artifact),
                                                   Artifact.content_id == content.id)).all() == []


def test_concurrent_different_requests_admit_one_head(published):
    runtime, http, ids = published
    runtime.run_content_stage(ids["content"], lambda *args: None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda key: http.post(endpoint(ids), headers=ADMIN, json=payload(ids, key)),
                                ["one", "two"]))
    assert sorted(row.status_code for row in results) == [200, 409]
    with Session(runtime.engine) as session:
        assert len(session.exec(select(ContentRevision)).all()) == 1


def test_concurrent_same_request_replays_one_head_and_stale_task_cannot_advance(published):
    runtime, http, ids = published
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: http.post(endpoint(ids), headers=ADMIN, json=payload(ids)), range(2)))
    assert [row.status_code for row in responses] == [200, 200]
    assert len({row.json()["revision_id"] for row in responses}) == 1
    before = snapshot(runtime, ids["content"])
    with pytest.raises(Exception, match="CONTENT_REVISION_STALE"):
        pipeline.advance_after_agent(runtime.engine, runtime.settings, ids["tasks"][0])
    assert snapshot(runtime, ids["content"]) == before
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PREPARING_CONTEXT


@pytest.mark.parametrize("kind", [TaskType.CONTENT_RESEARCH, TaskType.DOUYIN_PUBLISH])
def test_expired_late_failure_cannot_rewrite_previous_revision_or_downgrade_intent(published, kind):
    runtime, http, ids = published
    with Session(runtime.engine) as session:
        if kind == TaskType.CONTENT_RESEARCH:
            task = session.get(AgentTask, ids["tasks"][0])
        else:
            task = runtime.workflow_service(session).create_agent_task(ids["content"], kind,
                {"platform": "douyin", "account": {"account_id": "local-default"},
                 "video_artifact_id": "old-video", "video_sha256": "a" * 64})
            task.status, task.result_data = TaskStatus.COMPLETED, {"status": "PUBLISHED", "platform": "douyin"}
        task.assigned_agent_id, task.attempt = "old-worker", 1
        session.add(task)
        if kind == TaskType.DOUYIN_PUBLISH:
            target = publish_target_for_task(task)
            session.add(AuditEvent(actor_type="agent", actor_id="old-worker", entity_type="publish_intent",
                entity_id=target["intent_key"], action="publish_intent_state",
                metadata_json={**target, "status": "PUBLISHED", "task_id": task.id, "intent_sequence": 1}))
        session.commit()
        task_id = task.id
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 200, response.text
    before = snapshot(runtime, ids["content"])
    with Session(runtime.engine) as session:
        service = runtime.task_service(session)
        audits = [row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()]
        with pytest.raises(TaskConflictError, match="CONTENT_REVISION_STALE"):
            service.fail(task_id=task_id, lease_token="expired-invalid-token", agent_id="old-worker",
                         attempt=1, retryable=False, code="LEASE_EXPIRED", message="late stale worker report")
        if kind == TaskType.DOUYIN_PUBLISH:
            assert service._record_publish_intent_state(session.get(AgentTask, task_id), status="INTERRUPTED",
                                                        actor_id="lease-reporter", reason="late") is None
        assert [row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()] == audits
    assert snapshot(runtime, ids["content"]) == before


def test_immutable_revision_and_unreviewed_new_video_cannot_release(published):
    from sqlalchemy.exc import IntegrityError

    runtime, http, ids = published
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    with Session(runtime.engine) as session:
        revision = session.get(ContentRevision, head)
        frozen = revision.model_dump(mode="json")
        revision.feedback_json = {"feedback": "rewrite immutable evidence"}
        session.add(revision)
        with pytest.raises(IntegrityError, match="immutable"):
            session.commit()
        session.rollback()
        assert session.get(ContentRevision, head).model_dump(mode="json") == frozen
        content = session.get(Content, ids["content"])
        path = revision_work_root(content) / "render/unreviewed.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"unreviewed new render")
        pipeline.register_artifact(session, content=content, executor=MptMediaExecutor(Path(content.content_dir)),
                                   path=path, artifact_type="final_video", stage="render")
        with pytest.raises(Exception, match="Current script_qc"):
            pipeline.verify_publish_ready(session, runtime.settings, content.id)
    assert http.post(f"/api/v1/contents/{ids['content']}/approve-final", headers=ADMIN).status_code == 409
    assert http.post(f"/api/v1/contents/{ids['content']}/publications/douyin/publish", headers=ADMIN).status_code == 409


@pytest.mark.parametrize("active", ["task", "stage", "offer", "intent"])
def test_cross_revision_inflight_or_uncertain_click_blocks_revision(published, active):
    runtime, http, ids = published
    with Session(runtime.engine) as session:
        if active == "task":
            task = session.get(AgentTask, ids["tasks"][0])
            task.status = TaskStatus.SUBMITTED
            session.add(task)
        elif active == "stage":
            session.add(StageRun(content_id=ids["content"], stage="render", input_hash="fixture", status=StageStatus.RUNNING))
        elif active == "offer":
            session.add(SchedulerOffer(request_id="live-old-offer", task_id=ids["tasks"][0],
                                       expires_at=utc_now() + timedelta(minutes=5)))
        else:
            target = {"platform": "douyin", "account_id": "local-default", "content_id": ids["content"],
                      "video_artifact_id": "old-video", "video_sha256": "a" * 64}
            key = stable_hash(target)
            session.add(AuditEvent(actor_type="agent", actor_id="old-publisher", entity_type="publish_intent",
                entity_id=key, action="publish_intent_registered",
                metadata_json={**target, "intent_key": key, "intent_sequence": 0, "status": "UNKNOWN"}))
        session.commit()
    before = snapshot(runtime, ids["content"])
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 409, response.text
    assert snapshot(runtime, ids["content"]) == before
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).current_revision_id is None
        assert session.exec(select(ContentRevision)).all() == []


def test_new_publication_appends_and_stale_result_cannot_publish_head(published, monkeypatch):
    runtime, http, ids = published
    before = snapshot(runtime, ids["content"])
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        content = session.get(Content, ids["content"])
        root = Path(content.content_dir)
        path = revision_work_root(content) / "render/final.mp4"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"new immutable video")
        video = pipeline.register_artifact(session, content=content, executor=MptMediaExecutor(root),
                                           path=path, artifact_type="final_video", stage="render")
        content.business_status, content.current_stage = ContentStatus.PACKAGED, "packaged"
        session.add(content)
        session.commit()
        runtime.settings.cover_ai_enabled = False
        queued = flow.queue_douyin_publish(content.id, settings=runtime.settings)
        task = session.get(AgentTask, queued["task_id"])
        target = publish_target_for_task(task)
        assert task.revision_id == head and target["content_revision_id"] == head
        assert task.input_data["video_artifact_id"] == video.id
        legacy_target = {key: value for key, value in target.items() if key not in {"intent_key", "content_revision_id"}}
        assert stable_hash(legacy_target) != target["intent_key"]
        task.status = TaskStatus.COMPLETED
        task.result_data = {"platform": "douyin", "status": "PUBLISHED", "external_id": "new-public-id",
                            "external_url": "https://www.douyin.com/video/new-public-id"}
        session.add(task)
        session.commit()
        # The external click/evidence is simulated; row identity and all writes are real SQLite.
        monkeypatch.setattr(flow.tasks, "validate_published_publish_result", lambda *a, **kw: target)
        monkeypatch.setattr(flow.tasks, "reconcile_publish_intent", lambda *a, **kw: target)
        result = flow.record_agent_publication_result(task)
        assert result.revision_id == head and result.id != ids["publication"]
        assert flow.record_agent_publication_result(task).id == result.id
        assert flow.queue_douyin_publish(content.id, settings=runtime.settings)["status"] == "ALREADY_COMPLETED"
        records = session.exec(select(PublicationRecord)).all()
        assert len(records) == 2 and {row.external_id for row in records} == {"old-public-id", "new-public-id"}
        assert session.get(Content, content.id).business_status == ContentStatus.PUBLISHED
        content.business_status = ContentStatus.PACKAGED
        session.add(content)
        session.commit()
        old_task = AgentTask(content_id=content.id, task_type=TaskType.DOUYIN_PUBLISH, prompt_name="old publish",
            instructions="old", input_data={"platform": "douyin"}, input_hash="old", status=TaskStatus.COMPLETED,
            result_data={"platform": "douyin", "status": "PUBLISHED", "external_id": "old-public-id",
                         "external_url": "https://www.douyin.com/video/old-public-id"})
        session.add(old_task)
        session.commit()
        assert flow.record_agent_publication_result(old_task).id == ids["publication"]
        session.refresh(content)
        assert content.business_status == ContentStatus.PACKAGED
        after = snapshot(runtime, ids["content"])
        assert all(after[key] == value for key, value in before.items())


def test_old_uncertain_intent_after_head_start_still_blocks_new_queue_and_legacy_key_is_unchanged(published):
    runtime, http, ids = published
    http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    with Session(runtime.engine) as session:
        legacy_input = {"platform": "douyin", "account": {"account_id": "local-default"},
                        "video_artifact_id": "old-video", "video_sha256": "a" * 64}
        legacy = AgentTask(content_id=ids["content"], task_type=TaskType.DOUYIN_PUBLISH,
                           prompt_name="old", instructions="old", input_data=legacy_input,
                           input_hash=stable_hash(legacy_input))
        target = publish_target_for_task(legacy)
        assert "content_revision_id" not in target
        assert target["intent_key"] == stable_hash({"platform": "douyin", "account_id": "local-default",
            "content_id": ids["content"], "video_artifact_id": "old-video", "video_sha256": "a" * 64})
        session.add(AuditEvent(actor_type="agent", actor_id="old-worker", entity_type="publish_intent",
            entity_id=target["intent_key"], action="publish_intent_state",
            metadata_json={**target, "intent_sequence": 2, "status": "UNKNOWN"}))
        content = session.get(Content, ids["content"])
        content.business_status = ContentStatus.PACKAGED
        session.add(content)
        session.commit()
        with pytest.raises(TaskConflictError, match="RECONCILIATION"):
            runtime.workflow_service(session).queue_douyin_publish(content.id, settings=runtime.settings)
        assert session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.DOUYIN_PUBLISH)).all() == []


@pytest.mark.parametrize("collision", ["id", "normalized_url", "modal_url", "url_only_tracking"])
def test_old_external_identity_is_rejected_before_publish_completion_or_manual_append(published, collision):
    from glance_core.task_service import TaskValidationError
    from test.business.test_publish_policy import publisher

    runtime, http, ids = published
    if collision == "url_only_tracking":
        with Session(runtime.engine) as session:
            old = session.get(PublicationRecord, ids["publication"])
            old.external_id, old.external_url = "7698443917590629670", "https://www.douyin.com/video/7698443917590629670"
            session.add(old)
            session.commit()
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.business_status = ContentStatus.PACKAGED
        session.add(content)
        video = Artifact(content_id=content.id, revision_id=head, artifact_type="publish_video",
                         relative_path=f"revisions/{head}/publish/new-video.mp4", sha256="b" * 64,
                         size_bytes=1, mime_type="video/mp4", producer_stage="package")
        session.add(video)
        session.commit()
        task = runtime.workflow_service(session).create_agent_task(content.id, TaskType.DOUYIN_PUBLISH,
            {"platform": "douyin", "account": {"account_id": "local-default"},
             "video_artifact_id": video.id, "video_sha256": video.sha256})
        # No old artifact is offered: this tests identity validation independently.
        session.commit()
        task_id = task.id
    client = publisher(http)
    claim = client.dispatch(wait=0, task_id=task_id)
    client.confirm_claim(task_id, claim["lease_token"], "publish-worker")
    assert client.register_publish_intent(task_id, claim["lease_token"], "publish-worker")["click_authorized"]
    with Session(runtime.engine) as session:
        evidence = Artifact(content_id=ids["content"], revision_id=head, artifact_type="publish_evidence",
                            relative_path="agent-assets/new-evidence.png", sha256="c" * 64,
                            size_bytes=1, mime_type="image/png", producer_stage=task_id)
        session.add(evidence)
        session.commit()
        evidence_id = evidence.id
    external_id = "" if collision == "url_only_tracking" else "old-public-id" if collision == "id" else "different-new-id"
    external_url = {"id": "https://www.douyin.com/video/new-id",
        "normalized_url": "http://douyin.com/video/old-public-id/?utm_source=share#ignored",
        "modal_url": "https://www.douyin.com/user/self?modal_id=old-public-id&from=share",
        "url_only_tracking": "https://www.douyin.com/video/7698443917590629670?previous_page=app_code_link#ignored"}[collision]
    with Session(runtime.engine) as session:
        service = runtime.task_service(session)
        with pytest.raises(TaskValidationError) as rejection:
            service.complete(task_id=task_id, lease_token=claim["lease_token"], agent_id="publish-worker",
                result={"platform": "douyin", "status": "PUBLISHED", "external_id": external_id,
                        "external_url": external_url, "evidence_artifact_id": evidence_id})
        assert "EXTERNAL_IDENTITY_ALREADY_USED" in str(rejection.value.errors)
        task = session.get(AgentTask, task_id)
        assert task.status != TaskStatus.COMPLETED and task.result_data is None
        assert service.publish_intent_for_task(task)["intent_status"] == "AUTHORIZED"
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED
        assert len(session.exec(select(PublicationRecord)).all()) == 1
        with pytest.raises(TaskConflictError, match="EXTERNAL_IDENTITY_ALREADY_USED"):
            runtime.workflow_service(session).record_publication(ids["content"], platform="douyin",
                external_id=external_id, external_url=external_url)
        assert session.get(PublicationRecord, ids["publication"]).external_id == (
            "7698443917590629670" if collision == "url_only_tracking" else "old-public-id")


@pytest.mark.parametrize("old_id,old_url,new_id,new_url,conflict", [
    ("7698443917590629670", "", "", "https://www.douyin.com/video/7698443917590629670?arbitrary_tracking=1", True),
    ("", "https://www.douyin.com/video/7698443917590629670", "7698443917590629670", "", True),
    ("7698443917590629670", "", "new-id", "https://www.douyin.com/video/%37%36%39%38%34%34%33%39%31%37%35%39%30%36%32%39%36%37%30", True),
    ("7698443917590629670", "", "", "https://www.douyin.com/user/self?modal_id=7698443917590629670&previous_page=app_code_link", True),
    ("7698443917590629670", "", "7698443917590629671", "https://www.douyin.com/video/7698443917590629671?previous_page=app_code_link", False),
    ("7698443917590629670", "", "", "https://www.douyin.com/video/7698443917590629671?tracking_anything=1", False),
])
def test_douyin_stable_work_identity_crosses_id_and_url_fields(published, old_id, old_url, new_id, new_url, conflict):
    from glance_core.content_revision import require_distinct_publication_identity

    runtime, http, ids = published
    with Session(runtime.engine) as session:
        old = session.get(PublicationRecord, ids["publication"])
        old.external_id, old.external_url = old_id, old_url
        session.add(old)
        session.commit()
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    with Session(runtime.engine) as session:
        if conflict:
            with pytest.raises(TaskConflictError, match="EXTERNAL_IDENTITY_ALREADY_USED"):
                require_distinct_publication_identity(session, ids["content"], head, "douyin", new_id, new_url)
        else:
            require_distinct_publication_identity(session, ids["content"], head, "douyin", new_id, new_url)


def test_unresolved_douyin_short_url_is_not_a_new_identity_but_recorded_same_head_replays(published):
    from glance_core.content_revision import require_distinct_publication_identity

    runtime, http, ids = published
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    with Session(runtime.engine) as session:
        url = "https://v.douyin.com/unresolved-short-link/"
        with pytest.raises(TaskConflictError, match="IDENTITY_UNRESOLVED"):
            require_distinct_publication_identity(session, ids["content"], head, "douyin", "", url)
        session.add(PublicationRecord(content_id=ids["content"], revision_id=head, platform="douyin",
            mode=PublicationMode.EXECUTOR, status="PUBLISHED", external_url=url))
        session.commit()
        require_distinct_publication_identity(session, ids["content"], head, "douyin", "", url)


def test_revision_source_cache_does_not_read_or_overwrite_old_files(published, monkeypatch):
    from glance_media import executor as portable

    runtime, http, ids = published
    http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        root = Path(content.content_dir)
        old = root / "source_images/source-001.png"
        old_bytes = old.read_bytes()
        def acquire(executor, *args, **kwargs):
            assert executor.root == revision_work_root(content)
            destination = executor.root / "source/original.mp4"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"new source URL content")
            return destination
        def images(executor, *args, **kwargs):
            destination = executor.root / "source_images/source-001.png"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"new downloaded image")
            return [{"path": destination}]
        monkeypatch.setattr(portable.MediaExecutor, "acquire", acquire)
        monkeypatch.setattr(portable.MediaExecutor, "acquire_images", images)
        executor = pipeline._media_executor(content)
        assert executor.acquire("https://example.org/new", "agent").is_relative_to(revision_work_root(content))
        item = executor.acquire_images([])[0]
        artifact = pipeline.register_artifact(session, content=content, executor=executor, path=item["path"],
                                              artifact_type="source_image", stage="acquire_images")
        assert artifact.revision_id == content.current_revision_id
        assert artifact.relative_path.startswith(f"revisions/{content.current_revision_id}/")
        assert old.read_bytes() == old_bytes
        assert (root / artifact.relative_path).read_bytes() == b"new downloaded image"


def test_new_research_acquisitions_have_distinct_paths_inside_the_same_revision(published, monkeypatch):
    from glance_media import executor as portable

    runtime, http, ids = published
    http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    def acquire(executor, images):
        destination = executor.root / "source_images/source-001.png"
        destination.parent.mkdir(parents=True)
        destination.write_bytes(images[0]["url"].encode())
        return [{**images[0], "path": destination, "width": 1080, "height": 1920}]
    monkeypatch.setattr(portable.MediaExecutor, "acquire_images", acquire)
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        paths = []
        for version in range(2):
            result = {"source_images": [{"url": f"https://example.org/new-{version}.png",
                       "caption": "official product", "source_page": "https://example.org/product"}],
                      "source_media_search": {"fixture": version}}
            task = runtime.workflow_service(session).create_agent_task(content.id, TaskType.CONTENT_RESEARCH,
                                                                       {"version": version}, allow_multiple=True)
            entries = pipeline._acquire_source_images(session, content, result, task)
            paths.append(entries[0]["relative_path"])
            assert f"/research/{task.id}/" in paths[-1]
        assert paths[0] != paths[1]
        assert (Path(content.content_dir) / paths[0]).read_bytes() == b"https://example.org/new-0.png"
        assert (Path(content.content_dir) / paths[1]).read_bytes() == b"https://example.org/new-1.png"


def test_database_migration_is_idempotent_without_rehashing_legacy_rows(published):
    runtime, _, ids = published
    before = snapshot(runtime, ids["content"])
    create_db(runtime.engine)
    create_db(runtime.engine)
    assert snapshot(runtime, ids["content"]) == before
    with Session(runtime.engine) as session:
        rows = session.exec(text("PRAGMA index_list(publication_record)")).all()
        names = {row[1] for row in rows}
        assert "ux_publication_record_content_platform" not in names
        assert {"ux_publication_record_legacy_content_platform", "ux_publication_record_revision_platform"} <= names
        assert all(row.revision_id is None for row in session.exec(select(AgentTask)).all())
        assert all("content_revision_id" not in row.input_data for row in session.exec(select(AgentTask)).all())


def test_real_legacy_schema_adds_nullable_columns_without_changing_shared_values(published):
    runtime, _, ids = published
    with runtime.engine.begin() as connection:
        for name in ("ix_content_current_revision_id", "ix_agent_task_revision_id", "ix_artifact_revision_id",
                     "ix_stage_run_revision_id", "ix_publication_record_revision_id", "ix_content_revision_request_key",
                     "ux_publication_record_revision_platform", "ux_publication_record_legacy_content_platform",
                     "ux_content_revision_request"):
            connection.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")
        for table, column in (("content", "current_revision_id"), ("agent_task", "revision_id"),
                              ("artifact", "revision_id"), ("stage_run", "revision_id"),
                              ("publication_record", "revision_id"), ("content_revision", "request_key")):
            connection.exec_driver_sql(f"ALTER TABLE {table} DROP COLUMN {column}")
        connection.exec_driver_sql("CREATE UNIQUE INDEX ux_publication_record_content_platform ON publication_record(content_id, platform)")
        before = connection.exec_driver_sql("SELECT id,input_data,input_hash,result_data,dedup_key,attempt,max_attempts FROM agent_task ORDER BY id").all()
        publications = connection.exec_driver_sql("SELECT * FROM publication_record ORDER BY id").all()
    create_db(runtime.engine)
    create_db(runtime.engine)
    with runtime.engine.begin() as connection:
        assert connection.exec_driver_sql("SELECT id,input_data,input_hash,result_data,dedup_key,attempt,max_attempts FROM agent_task ORDER BY id").all() == before
        assert connection.exec_driver_sql("SELECT revision_id FROM agent_task").all() == [(None,)] * len(before)
        old_columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(publication_record)").all() if row[1] != "revision_id"]
        assert connection.exec_driver_sql(f"SELECT {','.join(old_columns)} FROM publication_record ORDER BY id").all() == publications


def test_migration_restart_repairs_index_after_column_add_was_committed(published):
    from sqlalchemy import event, inspect

    runtime, _, ids = published
    before = snapshot(runtime, ids["content"])
    with runtime.engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_agent_task_revision_id")
        connection.exec_driver_sql("ALTER TABLE agent_task DROP COLUMN revision_id")
    def interrupt_index(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("CREATE INDEX IF NOT EXISTS ix_agent_task_revision_id"):
            raise RuntimeError("injected interruption after ALTER ADD")
    event.listen(runtime.engine, "before_cursor_execute", interrupt_index)
    try:
        with pytest.raises(RuntimeError, match="injected interruption"):
            create_db(runtime.engine)
    finally:
        event.remove(runtime.engine, "before_cursor_execute", interrupt_index)
    assert "revision_id" in {row["name"] for row in inspect(runtime.engine).get_columns("agent_task")}
    assert "ix_agent_task_revision_id" not in {row["name"] for row in inspect(runtime.engine).get_indexes("agent_task")}
    create_db(runtime.engine)
    create_db(runtime.engine)
    assert "ix_agent_task_revision_id" in {row["name"] for row in inspect(runtime.engine).get_indexes("agent_task")}
    assert snapshot(runtime, ids["content"]) == before


def test_old_run_cannot_cross_head_and_new_run_owns_its_own_budget(published):
    from app.business.content_run import ContentRun, require_run, save_run, start_run

    runtime, http, ids = published
    with Session(runtime.engine) as session:
        now = utc_now()
        old = ContentRun(run_id="run_" + "1" * 32, content_id=ids["content"], generation=4,
                         state="stopped", stop_requested=True, ready_rounds=12,
                         created_at=now.isoformat(), expires_at=(now + timedelta(minutes=90)).isoformat())
        save_run(session, old, "mpt_content_run_stop_requested")
        session.commit()
        old_event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == old.run_id)).one()
        frozen = old_event.model_dump(mode="json")
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(ids))
    assert response.status_code == 200, response.text
    with Session(runtime.engine) as session:
        new = start_run(session, ids["content"])
        assert new["revision_id"] == response.json()["revision_id"]
        assert new["run_id"] != old.run_id and new["generation"] == 5 and new["ready_rounds"] == 0
        assert new["intents"] == {}
        assert session.get(AuditEvent, frozen["id"]).model_dump(mode="json") == frozen
        assert require_run(session, new["run_id"]).revision_id == response.json()["revision_id"]
        with pytest.raises(TaskConflictError):
            require_run(session, old.run_id, old.generation)


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_real_namespace_render_preserves_legacy_caches_and_content_root_assets(tmp_path, monkeypatch):
    from PIL import Image
    from glance_media.executor import probe, run_command, sha256_file

    root = tmp_path / "content"
    executor = MptMediaExecutor(root, revision_id="rev_" + "3" * 32)
    frozen = {}
    for name in ("source/original.mp4", "source_images/source-001.png", "scenes/scenes.json",
                 "transcript/transcript.json", "transcript/narration.ass", "render/mpt-render-request.json",
                 "render/qc.json", "audio/bgm.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"retained original {name}".encode())
        frozen[name] = path.read_bytes()
    voice = root / "audio/immutable-new-voice/narration.wav"
    voice.parent.mkdir(parents=True)
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=root)
    voice.with_name("word_boundaries.json").write_text("[]")
    timing = executor.visual_timing_evidence(voice, 1.2)
    monkeypatch.setattr(executor, "_local_asr", lambda *a, **kw: ([{
        "text": "产品测试。", "start_sec": .05, "end_sec": 1.0,
        "words": [{"text": "产品", "start_sec": .05, "end_sec": .5},
                  {"text": "测试。", "start_sec": .5, "end_sec": 1.0}],
    }], ""))
    alignment = executor.align_subtitles(voice, "产品测试。", asr_model="isolated-fixture")
    image = root / "agent-assets/new-task/image.png"
    image.parent.mkdir(parents=True)
    Image.new("RGB", (800, 800), "green").save(image)
    final, preview = executor.render(video_path=executor.work_root / "source/unused.mp4", voice_path=voice,
        narration="产品测试。", scenes=[], assets={"new-image": image}, visual_timing_evidence=timing,
        subtitle_alignment=alignment, plan_data={"segments": [{"asset_id": "new-image", "layout": "image_card",
            "narration_start_sec": 0, "narration_end_sec": 1.2, "purpose": "isolated namespace render", "motion": "pan_right"}]})
    assert final.is_relative_to(executor.work_root / "render/revisions")
    assert executor.artifact_metadata(final, "final_video", "render")["relative_path"].startswith("revisions/rev_")
    assert (executor.work_root / "transcript/narration.ass").is_file()
    assert (executor.work_root / "render/mpt-render-request.json").is_file()
    stream = next(row for row in probe(final)["streams"] if row["codec_type"] == "video")
    assert (stream["width"], stream["height"], stream["avg_frame_rate"]) == (1080, 1920, "60/1")
    for path in (final, preview):
        run_command(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], cwd=root)
    assert all((root / name).read_bytes() == data for name, data in frozen.items())
    print(json.dumps({"namespace_render": {"final": str(final), "final_sha256": sha256_file(final),
        "preview_sha256": sha256_file(preview), "legacy_cache_sha256": {
            name: sha256_file(root / name) for name in frozen}, "legacy_caches_unchanged": True}}))


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
@pytest.mark.parametrize("failed_qc_only", [False, True])
def test_simulated_full_revision_research_to_second_publication_preserves_history(published, monkeypatch, failed_qc_only):
    """Real HTTP/SQLite/QC/package/intent; simulated agent decisions, TTS and render."""
    from PIL import Image
    from glance_core.models import Candidate, CandidateStatus, RightsStatus
    from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS
    from glance_media.executor import probe, run_command
    from test.business.test_content_http import RESEARCH, client_for
    from test.business.test_kokoro_voice_rebuild import prove_completed
    from test.business.test_publish_policy import publisher

    runtime, http, ids = published
    runtime.settings.cover_ai_enabled = runtime.settings.publish_prepare_enabled = runtime.settings.archive_enabled = False
    runtime.settings.handsfree_enabled = False
    runtime.settings.asr_model = "isolated-ASR-fixture"
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        candidate = session.get(Candidate, content.candidate_id)
        candidate.status, candidate.rights_status = CandidateStatus.SELECTED, RightsStatus.SELF_OWNED
        session.add(candidate)
        session.add(AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                               entity_id=content.id, action="manual_candidate_selected"))
        session.commit()
    before = snapshot(runtime, ids["content"])
    head = http.post(endpoint(ids), headers=ADMIN, json=payload(ids)).json()["revision_id"]
    narration = "完整的新研究和脚本，新的三十秒配音，必须重新审核后才能发布。"
    def acquire(executor, *args, **kwargs):
        path = executor.work_root / "source/original.mp4"
        path.parent.mkdir(parents=True)
        run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=green:s=64x64:r=30:d=30",
                     "-f", "lavfi", "-i", "sine=frequency=440:duration=30", "-c:v", "libx264",
                     "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(path)], cwd=executor.root)
        return path
    def transcript(executor, **kwargs):
        path = executor.work_root / "transcript/transcript.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"segments": []}', encoding="utf-8")
        return path
    scene = {"scene_id": "scene-001", "start_sec": 0.0, "end_sec": 30.0, "duration_sec": 30.0}
    def scenes(executor, source):
        directory = executor.work_root / "scenes"
        directory.mkdir(parents=True)
        thumbnail = directory / "scene-001.jpg"
        Image.new("RGB", (64, 64), "green").save(thumbnail)
        rows = [{**scene, "thumbnail": thumbnail.relative_to(executor.root).as_posix()}]
        path = directory / "scenes.json"
        path.write_text(json.dumps({"scenes": rows}), encoding="utf-8")
        return path, rows
    def voice(executor, text, **kwargs):
        path = executor.work_root / "audio/voice-fixture/narration.wav"
        path.parent.mkdir(parents=True)
        run_command(["ffmpeg", "-y", "-i", str(executor.work_root / "source/original.mp4"),
                     "-vn", "-c:a", "pcm_s16le", str(path)], cwd=executor.root)
        path.with_name("word_boundaries.json").write_text("[]")
        return SimpleNamespace(audio_path=path, duration_sec=30.0)
    def render(executor, **kwargs):
        assert kwargs["voice_path"].is_relative_to(executor.work_root)
        directory = executor.work_root / "render/revisions/simulated-render"
        directory.mkdir(parents=True)
        paths = [directory / name for name in ("final.mp4", "preview.mp4")]
        for path in paths:
            shutil.copy2(executor.work_root / "source/original.mp4", path)
        (directory / "outputs.json").write_text(json.dumps({path.name: pipeline.sha256_file(path) for path in paths}))
        assert float(probe(paths[0])["format"]["duration"]) >= runtime.settings.min_video_seconds
        return tuple(paths)
    def asr(executor, *args, **kwargs):
        return [{"start_sec": 0.1, "end_sec": 29.9, "text": narration}], ""
    monkeypatch.setattr(MptMediaExecutor, "acquire", acquire)
    monkeypatch.setattr(MptMediaExecutor, "ensure_transcript", transcript)
    monkeypatch.setattr(MptMediaExecutor, "analyze_scenes", scenes)
    monkeypatch.setattr(MptMediaExecutor, "synthesize_voice", voice)
    monkeypatch.setattr(MptMediaExecutor, "align_subtitles", lambda *a, **kw: None)
    monkeypatch.setattr(MptMediaExecutor, "render", render)
    monkeypatch.setattr(MptMediaExecutor, "qc", lambda *a, **kw: {"passed": True, "errors": [], "duration_sec": 30})
    monkeypatch.setattr(MptMediaExecutor, "_local_asr", asr)
    assert http.post(f"/api/v1/contents/{ids['content']}/prepare", headers=ADMIN).status_code == 200

    def complete(kind, result, *, advance=True):
        with Session(runtime.engine) as session:
            task = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"],
                AgentTask.revision_id == head, AgentTask.task_type == kind, AgentTask.status == TaskStatus.PENDING)).one()
            task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
            prove_completed(session, runtime, task)
            task_id = task.id
        if advance:
            pipeline.advance_after_agent(runtime.engine, runtime.settings, task_id)
        return task_id

    complete(TaskType.CONTENT_RESEARCH, RESEARCH)
    complete(TaskType.SCENE_ANALYZE, {"scenes": [scene]})
    complete(TaskType.SHORT_SCRIPT, {"full_narration": narration, "estimated_duration_sec": 30,
                                   "title_options": ["新修订产品测试"], "cover_title": "新修订测试"})
    complete(TaskType.SCRIPT_QC, {"passed": True, "issues": []})
    complete(TaskType.SCRIPT_REVIEW_ASSIST, {"verdict": "APPROVE", "confidence": "high", "issues": []}, advance=False)
    approved = http.post(f"/api/v1/contents/{ids['content']}/approve-script", headers=ADMIN)
    assert approved.status_code == 200, approved.text
    complete(TaskType.VISUAL_PLAN, {"segments": [{"scene_id": "scene-001", "layout": "fit",
        "source_start_sec": 0, "source_end_sec": 30, "narration_start_sec": 0,
        "narration_end_sec": 30, "purpose": "new official product demonstration"}]})
    complete(TaskType.VISUAL_PLAN_QC, {"passed": True, "issues": []})
    rendered = http.post(f"/api/v1/contents/{ids['content']}/render", headers=ADMIN)
    assert rendered.status_code == 200, rendered.text
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.content_id == ids["content"], AgentTask.revision_id == head,
                                                AgentTask.task_type == TaskType.CONTENT_QC)).one()
        assert qc.schema_version == "2" and qc.input_data["audio_evidence"]["asr_status"] == "available"
        assert qc.input_data["source_context"]["evidence_gaps"] == []
        qc_id, qc_subject = qc.id, qc.input_data["acceptance_subject"]
        assert qc_subject["content_revision_id"] == head
    decision = {"contract_version": 2, "passed": not failed_qc_only,
        "issues": [] if not failed_qc_only else [{"severity": "error", "field": "audio", "message": "Unclear test narration"}],
        "confidence": "high", "subject": qc_subject,
        "checks": {key: {"passed": not failed_qc_only or key != "audio",
                          "evidence": "Simulated complete revision protocol review"} for key in CONTENT_ACCEPTANCE_CHECKS}}
    if failed_qc_only:
        complete(TaskType.CONTENT_QC, decision)
        # Deployment enrollment is independently covered by recovery tests. Keep
        # exact current QC/voice/media/receipt verification real in this scope test.
        monkeypatch.setattr(runtime, "_require_accepted_native_task", lambda *a, **kw: None)
        with Session(runtime.engine) as session:
            qc, script = runtime._content_qc_source(session, qc_id)
            assert qc.revision_id == script.revision_id == head
            assert session.get(PublicationRecord, ids["publication"]).external_id == "old-public-id"
        after = snapshot(runtime, ids["content"])
        assert all(after[key] == value for key, value in before.items())
        return
    script = client_for(http, "script")
    claim = script.dispatch(wait=0, task_id=qc_id)
    script.confirm_claim(qc_id, claim["lease_token"], "script-worker")
    completed_qc = script.complete(qc_id, claim["lease_token"], "script-worker", decision)
    assert completed_qc["advanced"] == "CONSUMED", completed_qc
    packaged = http.post(f"/api/v1/contents/{ids['content']}/approve-final", headers=ADMIN)
    assert packaged.status_code == 200, packaged.text
    queued = http.post(f"/api/v1/contents/{ids['content']}/publications/douyin/publish", headers=ADMIN)
    assert queued.status_code == 200, queued.text
    publish_id = queued.json()["task_id"]
    pub_client = publisher(http)
    claim = pub_client.dispatch(wait=0, task_id=publish_id)
    pub_client.confirm_claim(publish_id, claim["lease_token"], "publish-worker")
    intent = pub_client.register_publish_intent(publish_id, claim["lease_token"], "publish-worker")
    assert intent["click_authorized"]
    png = BytesIO()
    Image.new("RGB", (32, 32), "green").save(png, format="PNG")
    evidence = http.post(f"/api/v1/agent/tasks/{publish_id}/artifacts",
        headers={"Authorization": f"Bearer {claim['lease_token']}", "Content-Type": "image/png"}, content=png.getvalue())
    assert evidence.status_code == 200, evidence.text
    result = {"status": "PUBLISHED", "platform": "douyin", "external_id": "simulated-second-external-id",
              "external_url": "https://www.douyin.com/video/simulated-second-external-id",
              "evidence_artifact_id": evidence.json()["artifact_id"], "message": "Simulated external click result"}
    pub_client.complete(publish_id, claim["lease_token"], "publish-worker", result)
    pub_client.complete(publish_id, claim["lease_token"], "publish-worker", result)
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PUBLISHED
        assert len(session.exec(select(PublicationRecord)).all()) == 2
        assert session.get(PublicationRecord, ids["publication"]).external_id == "old-public-id"
        assert session.exec(select(PublicationRecord).where(PublicationRecord.revision_id == head)).one().external_id == "simulated-second-external-id"
    after = snapshot(runtime, ids["content"])
    assert all(after[key] == value for key, value in before.items())
