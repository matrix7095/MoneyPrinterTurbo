"""Portable Glance contracts, isolated from its server/worker test fixtures."""

import importlib
import json
import pkgutil
from datetime import timedelta

import pytest
from glance_agent.validator import validate_result
from glance_collector.service import CollectionService
from glance_core.agent_roles import AgentRole, role_spec
from glance_core.database import build_engine, create_db
from glance_core.models import (
    AgentSubmission,
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    CandidateSignal,
    Content,
    ContentStatus,
    RightsStatus,
    Source,
    SourceType,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.settings import Settings
from glance_core.task_service import (
    AgentTaskService,
    TaskConflictError,
    TaskValidationError,
)
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select


@pytest.fixture
def portable_settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}",
        handsfree_enabled=False,
        archive_repo_dir=tmp_path / "archive",
    )


@pytest.fixture
def portable_session(portable_settings):
    engine = build_engine(portable_settings)
    create_db(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def discovery(portable_session, portable_settings):
    collector = CollectionService(portable_session, portable_settings)
    source = collector.create_source(
        Source(
            name="Portable discovery",
            source_type=SourceType.AGENT,
            url="",
            config_json={"scope": "Useful new products", "require_video": False},
        )
    )
    run = collector.collect_source(source.id)
    assert run.status == "SUCCESS", run.error_message
    task = portable_session.exec(
        select(AgentTask).where(AgentTask.task_type == TaskType.SOURCE_DISCOVERY)
    ).one()
    tasks = AgentTaskService(portable_session, portable_settings.agent_submission_dir)
    return collector, source, task, tasks


def discovered_result():
    return {
        "candidates": [
            {
                "external_id": "portable-project",
                "url": "https://example.com/project?utm_source=discovery",
                "title": "Useful new product",
                "rights_status": "EXPLICIT_LICENSE",
                "rights_note": "Publisher: example; CC BY 4.0; https://example.com/license",
            }
        ]
    }


def claim_discovery(tasks):
    claimed = tasks.claim(
        agent_id="portable-agent",
        role_id=AgentRole.DISCOVERY,
        capabilities={TaskType.SOURCE_DISCOVERY},
    )
    assert claimed is not None
    return claimed


def test_all_portable_modules_import_and_role_resources_match():
    # Import every module to catch delayed cross-package dependencies before HTTP startup.
    for name in ("glance_core", "glance_agent", "glance_collector"):
        package = importlib.import_module(name)
        for module in pkgutil.walk_packages(package.__path__, prefix=f"{name}."):
            importlib.import_module(module.name)
    from glance_agent.cli import bundled_role_skill_path

    for role in AgentRole:
        _, directory = bundled_role_skill_path(role.value)
        metadata = json.loads((directory / "role.json").read_text(encoding="utf-8"))
        assert metadata["skill_version"] == role_spec(role).skill_version
        assert (directory / "SKILL.md").is_file()


def test_lease_heartbeat_and_result_survive_engine_restart(discovery, portable_settings):
    collector, _, task, tasks = discovery
    leased, token = claim_discovery(tasks)
    heartbeat = tasks.heartbeat(task.id, token)
    assert heartbeat.attempt == 1
    assert heartbeat.lease_token_hash != token
    assert as_utc(heartbeat.lease_expires_at) > utc_now()
    engine = build_engine(portable_settings)
    with Session(engine) as restarted_session:
        persisted = restarted_session.get(AgentTask, task.id)
        assert persisted.status == TaskStatus.LEASED
        assert persisted.assigned_agent_id == "portable-agent"
        restarted = AgentTaskService(restarted_session, portable_settings.agent_submission_dir)
        completed = restarted.complete(
            task_id=leased.id,
            lease_token=token,
            agent_id="portable-agent",
            result=discovered_result(),
        )
        assert completed.status == TaskStatus.COMPLETED
        submission = restarted_session.exec(select(AgentSubmission)).one()
        assert submission.validation_status == "ACCEPTED"
        assert submission.attempt == 1
        assert (portable_settings.agent_submission_dir / submission.raw_result_path).is_file()
    engine.dispose()
    collector.session.expire_all()
    assert collector.ingest_agent_result(collector.session.get(AgentTask, task.id)) == 1


@pytest.mark.parametrize("role", ["", AgentRole.SCRIPT])
def test_wrong_role_cannot_claim_discovery(discovery, role):
    _, _, task, tasks = discovery
    assert tasks.claim(
        agent_id="wrong-role", role_id=role, capabilities={TaskType.SOURCE_DISCOVERY}
    ) is None
    assert task.status == TaskStatus.PENDING
    assert task.attempt == 0


def test_wrong_worker_and_token_cannot_submit(discovery):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    for agent_id, lease_token in (("another-agent", token), ("portable-agent", "invalid-token")):
        with pytest.raises(TaskConflictError):
            tasks.complete(
                task_id=task.id,
                lease_token=lease_token,
                agent_id=agent_id,
                result=discovered_result(),
            )
    assert task.status == TaskStatus.LEASED


@pytest.mark.parametrize("result", [{"candidates": [{}]}, {"candidates": [], "extra": True}])
def test_invalid_schema_records_rejection_and_keeps_correction_lease(discovery, result):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    with pytest.raises(TaskValidationError):
        tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result=result)
    assert task.status == TaskStatus.LEASED
    assert task.result_data is None
    assert tasks.session.exec(select(AgentSubmission)).one().validation_status == "REJECTED"


def test_search_result_url_is_rejected(discovery):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    result = discovered_result()
    result["candidates"][0]["url"] = "https://google.com/search?q=product"
    with pytest.raises(TaskValidationError) as rejection:
        tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result=result)
    assert rejection.value.errors[0]["path"] == ["candidates", 0, "url"]


@pytest.mark.parametrize("exhausted", [False, True])
def test_expired_lease_rejects_result_and_reaps_with_bounded_attempts(discovery, exhausted):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    task.lease_expires_at = utc_now() - timedelta(seconds=1)
    if exhausted:
        task.max_attempts = task.attempt
    tasks.session.add(task)
    tasks.session.commit()
    with pytest.raises(TaskConflictError):
        tasks.complete(
            task_id=task.id,
            lease_token=token,
            agent_id="portable-agent",
            result=discovered_result(),
        )
    assert tasks.reap_expired_leases() == 1
    assert task.status == (TaskStatus.FAILED if exhausted else TaskStatus.PENDING)
    assert task.lease_token_hash is None
    assert task.attempt == 1
    if not exhausted:
        assert as_utc(task.not_before) > utc_now()
    assert tasks.session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == "lease_expired"
        )
    ).one()


def test_repeated_schema_rejections_release_lease(discovery):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    for _ in range(4):
        with pytest.raises(TaskValidationError):
            tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result={})
    assert task.status == TaskStatus.PENDING
    assert task.lease_token_hash is None
    assert task.attempt == 1
    assert len(tasks.session.exec(select(AgentSubmission)).all()) == 4


def test_active_source_poll_does_not_duplicate_tasks(discovery):
    collector, source, task, tasks = discovery
    second_run = collector.collect_source(source.id)
    assert second_run.error_code == "DIRECTION_BUSY"
    assert tasks.session.exec(select(AgentTask)).one().id == task.id


def test_repeated_result_and_candidate_ingest_preserve_rights(discovery):
    # Adapted from Glance test_collect_filters.py and test_dedup.py.
    collector, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    for _ in range(2):
        tasks.complete(
            task_id=task.id,
            lease_token=token,
            agent_id="portable-agent",
            result=discovered_result(),
        )
    assert len(tasks.session.exec(select(AgentSubmission)).all()) == 1
    assert collector.ingest_agent_result(task) == 1
    candidate = tasks.session.exec(select(Candidate)).one()
    assert candidate.rights_status == RightsStatus.EXPLICIT_LICENSE
    candidate.rights_status = RightsStatus.REJECTED
    candidate.rights_note = "User registration: not usable."
    tasks.session.add(candidate)
    tasks.session.commit()
    assert collector.ingest_agent_result(task) == 0
    tasks.session.refresh(candidate)
    assert candidate.rights_status == RightsStatus.REJECTED
    assert candidate.rights_note == "User registration: not usable."
    assert len(tasks.session.exec(select(CandidateSignal)).all()) == 1


def test_partial_ingest_replay_does_not_duplicate_candidates(discovery, monkeypatch):
    collector, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    result = discovered_result()
    result["candidates"].append(
        {"external_id": "second-product", "url": "https://example.com/second", "title": "Second"}
    )
    tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result=result)
    ingest = collector._ingest_item
    calls = 0

    def fail_after_first_commit(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated consumer interruption")
        return ingest(*args, **kwargs)

    monkeypatch.setattr(collector, "_ingest_item", fail_after_first_commit)
    with pytest.raises(RuntimeError, match="consumer interruption"):
        collector.ingest_agent_result(task)
    assert len(tasks.session.exec(select(Candidate)).all()) == 1
    monkeypatch.setattr(collector, "_ingest_item", ingest)
    assert collector.ingest_agent_result(task) == 1
    assert collector.ingest_agent_result(task) == 0
    assert len(tasks.session.exec(select(Candidate)).all()) == 2
    assert len(tasks.session.exec(select(CandidateSignal)).all()) == 2


def test_agent_local_result_validation_uses_server_schema(discovery, tmp_path):
    _, _, task, _ = discovery
    (tmp_path / "result.schema.json").write_text(json.dumps(task.result_schema), encoding="utf-8")
    (tmp_path / "result.json").write_text(json.dumps(discovered_result()), encoding="utf-8")
    assert validate_result(tmp_path)[1] == []
    (tmp_path / "result.json").write_text(json.dumps({"candidates": [{}]}), encoding="utf-8")
    assert validate_result(tmp_path)[1]


def test_publish_task_uses_configured_sau_account_without_changing_intent_identity(
    portable_session, portable_settings
):
    candidate = Candidate(
        source_type="agent", source_id="account-contract", source_url="https://example.com/source",
        original_title="Account contract", rights_status=RightsStatus.SELF_OWNED,
    )
    portable_session.add(candidate)
    portable_session.commit()
    content = Content(
        candidate_id=candidate.id, title="Account contract", business_status=ContentStatus.PACKAGED,
        content_dir=str(portable_settings.content_dir / "account-contract"),
    )
    portable_session.add(content)
    portable_session.commit()
    artifact = Artifact(
        content_id=content.id, artifact_type="publish_video", relative_path="publish/final.mp4",
        sha256="a" * 64, size_bytes=1, mime_type="video/mp4", producer_stage="package",
    )
    portable_session.add(artifact)
    portable_session.commit()
    settings = portable_settings.model_copy(update={"cover_ai_enabled": False})
    flow = WorkflowService(
        portable_session, data_dir=settings.data_dir,
        submission_root=settings.agent_submission_dir, lease_minutes=settings.agent_lease_minutes,
    )
    queued = flow.queue_douyin_publish(content.id, settings=settings)
    task = portable_session.get(AgentTask, queued["task_id"])
    assert task.input_data["account"]["account_name"] == "local-default"
    assert task.input_data["account"]["account_id"] == "local-default"
    assert flow.tasks.publish_intent_for_task(task) is None
    assert flow.queue_douyin_publish(content.id, settings=settings)["task_id"] == task.id


def test_accepted_retry_requires_original_attempt_token(discovery):
    _, _, task, tasks = discovery
    _, token = claim_discovery(tasks)
    result = discovered_result()
    tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result=result)
    with pytest.raises(TaskConflictError, match="accepted-attempt"):
        tasks.complete(task_id=task.id, lease_token="invalid", agent_id="portable-agent", result=result)
    assert tasks.complete(task_id=task.id, lease_token=token, agent_id="portable-agent", result=result).id == task.id
    assert len(tasks.session.exec(select(AgentSubmission)).all()) == 1
