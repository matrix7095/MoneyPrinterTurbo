from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from glance_core.models import (
    AgentTask,
    AuditEvent,
    Candidate,
    CandidateStatus,
    Content,
    ContentStatus,
    RuntimeSetting,
    Source,
    SourceType,
    TaskStatus,
    TaskType,
    utc_now,
)
from glance_core.settings import Settings
from glance_core.task_service import TaskConflictError, stable_hash
from app.business.topic_policy import (
    _handsfree_wip_count,
    _record_low_water_supplement,
    _source_can_receive_low_water_supplement,
)
from sqlmodel import Session, select

from app.business.maintenance import INTERVAL, TICK
from app.business.quarantine import CONTENT_QUARANTINE, TASK_QUARANTINE
from app.business.runtime import BusinessRuntime


@pytest.fixture
def runtime(tmp_path):
    item = BusinessRuntime(
        Settings(
            _env_file=None,
            _env_prefix="MPT_BUSINESS_",
            data_dir=tmp_path,
            database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
            admin_token="test-admin",
            bootstrap_agent_token="test-agent",
        )
    )
    item.initialize()
    yield item
    item.close()


def add_source(runtime, *, due=True, enabled=True, config=None):
    now = utc_now()
    source = Source(
        name="海外新产品",
        source_type=SourceType.AGENT,
        url="https://example.org",
        enabled=enabled,
        poll_interval_minutes=720,
        next_poll_at=now - timedelta(minutes=1) if due else now + timedelta(hours=12),
        config_json=config or {},
    )
    with Session(runtime.engine) as session:
        session.add(source)
        session.commit()
        session.refresh(source)
    return source


def enable(runtime, **values):
    with Session(runtime.engine) as session:
        for key, value in {"handsfree_enabled": True, **values}.items():
            import json

            session.add(RuntimeSetting(key=key, value=json.dumps(value)))
        session.commit()


def counts(runtime, cls):
    with Session(runtime.engine) as session:
        return session.exec(select(cls)).all()


def test_due_poll_independent_of_handsfree_and_repeated_check(runtime):
    source = add_source(runtime)
    add_source(runtime, enabled=False)
    now = utc_now()
    assert runtime.maintenance.run(now=now)["polled"] == 1
    assert runtime.maintenance.run(now=now)["status"] == "NOT_DUE"
    tasks = counts(runtime, AgentTask)
    assert len(tasks) == 1 and tasks[0].task_type == TaskType.SOURCE_DISCOVERY
    assert tasks[0].input_data["source_id"] == source.id
    assert tasks[0].status == TaskStatus.PENDING
    owned = [a for a in counts(runtime, AuditEvent) if a.action == "mpt_task_owned"]
    assert [(a.entity_id, a.metadata_json["input_hash"]) for a in owned] == [
        (tasks[0].id, tasks[0].input_hash)
    ]
    assert runtime.maintenance.run(now=now + INTERVAL)["polled"] == 0


def test_parallel_maintenance_has_one_persisted_tick_and_task(runtime):
    add_source(runtime)
    now = utc_now()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: runtime.maintenance.run(now=now), range(4)))
    assert sum(r.get("polled", 0) for r in results) == 1
    assert len(counts(runtime, AgentTask)) == 1
    assert len([r for r in counts(runtime, RuntimeSetting) if r.key == TICK]) == 1


def test_lowwater_preserves_cooldown_budget_and_active_idempotency(runtime):
    enable(runtime)
    source = add_source(runtime, due=False)
    now = utc_now()
    assert runtime.maintenance.run(now=now)["source_discovery_queued"] == 1
    assert runtime.maintenance.run(now=now + INTERVAL)["source_discovery_queued"] == 0
    with Session(runtime.engine) as session:
        current = session.get(Source, source.id)
        state = current.config_json["handsfree_low_water_supplement"]
        assert state["count"] == 1 and state["cooldown_minutes"] == 60
        assert not _source_can_receive_low_water_supplement(
            current, now=now + timedelta(minutes=59)
        )
        for index in range(1, 3):
            _record_low_water_supplement(current, now=now + timedelta(hours=index))
        state = current.config_json["handsfree_low_water_supplement"]
        assert state["count"] == 3 and state["status"] == "EXHAUSTED_COOLDOWN"
        assert not _source_can_receive_low_water_supplement(
            current, now=now + timedelta(hours=2, minutes=59)
        )
        assert _source_can_receive_low_water_supplement(
            current, now=now + timedelta(hours=3)
        )


def test_empty_result_backoff_blocks_due_and_lowwater(runtime):
    enable(runtime)
    now = utc_now()
    add_source(
        runtime,
        config={
            "empty_result_backoff": {
                "next_retry_at": (now + timedelta(hours=2)).isoformat(),
            }
        },
    )
    runtime.maintenance.run(now=now)
    assert not counts(runtime, AgentTask)


def candidate(runtime, **options):
    row = Candidate(
        source_type="agent",
        source_id="source",
        original_title="Portable desk",
        title_zh="便携书桌",
        summary="年轻人搬家的便携书桌概念",
        source_url="https://example.org/desk",
        score=90,
        status=CandidateStatus.ENRICHED,
        **options,
    )
    with Session(runtime.engine) as session:
        session.add(row)
        session.commit()
        session.refresh(row)
    return row


def assist(
    runtime, row, *, owned=True, consumed=True, quarantined=False, verdict="SELECT"
):
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        inputs = {
            "items": [{"candidate_id": row.id, "production_mode": "image_only"}],
            "asset_image_enabled": True,
            "image_only_gen_budget": 8,
        }
        task = flow.tasks.create_task(
            task_type=TaskType.CANDIDATE_ASSIST,
            instructions="fixture",
            input_data=inputs,
        )
        task.status = TaskStatus.COMPLETED
        task.result_data = {
            "items": [
                {"candidate_id": row.id, "verdict": verdict, "confidence": "high"}
            ]
        }
        session.add(task)
        for action, enabled in [("mpt_task_owned", owned)]:
            if enabled:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="test",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action=action,
                        metadata_json={"input_hash": stable_hash(inputs)},
                    )
                )
        session.commit()
        session.refresh(task)
        task_id = task.id
    if consumed:
        assert runtime.consume_agent(task_id)
    if quarantined:
        with Session(runtime.engine) as session:
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="test",
                    entity_type="agent_task",
                    entity_id=task_id,
                    action=TASK_QUARANTINE,
                    metadata_json={},
                )
            )
            session.commit()
    return task


def test_owned_consumed_assist_selects_and_real_prepare_once(runtime):
    enable(runtime)
    row = candidate(runtime)
    assist(runtime, row)
    now = utc_now()
    result = runtime.maintenance.run(now=now)
    assert result["selected"] == result["prepared"] == 1
    content = counts(runtime, Content)[0]
    assert (
        content.current_stage == "agent_context"
        and content.business_status == ContentStatus.WAITING_AGENT
    )
    research = [
        t
        for t in counts(runtime, AgentTask)
        if t.task_type == TaskType.CONTENT_RESEARCH
    ]
    assert len(research) == 1 and research[0].status == TaskStatus.PENDING
    assert runtime.maintenance.run(now=now + INTERVAL)["selected"] == 0
    assert len(counts(runtime, Content)) == 1


@pytest.mark.parametrize(
    "owned,consumed,quarantined",
    [(False, True, False), (True, False, False), (True, True, True)],
)
def test_historical_unconsumed_and_quarantined_assist_cannot_select(
    runtime, owned, consumed, quarantined
):
    enable(runtime)
    row = candidate(runtime)
    assist(runtime, row, owned=owned, consumed=consumed, quarantined=quarantined)
    assert runtime.maintenance.run()["selected"] == 0
    assert not counts(runtime, Content)


@pytest.mark.parametrize("quarantined", [False, True])
def test_manual_and_quarantined_contents_never_adopted_or_scheduled(
    runtime, quarantined
):
    enable(runtime)
    row = candidate(runtime)
    assist(runtime, row)
    with Session(runtime.engine) as session:
        content = runtime.workflow_service(session).select_candidate(
            row.id, production_mode="image_only"
        )
        row = session.get(Candidate, row.id)
        row.status = CandidateStatus.ENRICHED
        session.add(row)
        if quarantined:
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="import",
                    entity_type="content",
                    entity_id=content.id,
                    action=CONTENT_QUARANTINE,
                    metadata_json={},
                )
            )
        session.commit()
    assert runtime.maintenance.run()["selected"] == 0
    assert not [
        a
        for a in counts(runtime, AuditEvent)
        if a.action == "handsfree_candidate_selected"
    ]


def test_inherited_exhausted_quota_is_preserved_and_normal_due_poll_bypasses_it(
    runtime,
):
    enable(runtime)
    now = utc_now()
    state = {
        "status": "EXHAUSTED_COOLDOWN",
        "count": 3,
        "daily_limit": 3,
        "cooldown_minutes": 60,
        "day": now.date().isoformat(),
        "last_scheduled_at": now.isoformat(),
        "next_retry_at": (now + timedelta(hours=1)).isoformat(),
    }
    source = add_source(
        runtime, due=False, config={"handsfree_low_water_supplement": state}
    )
    assert runtime.maintenance.run(now=now)["source_discovery_queued"] == 0
    assert not counts(runtime, AgentTask)
    with Session(runtime.engine) as session:
        actual = session.get(Source, source.id)
        assert actual.config_json["handsfree_low_water_supplement"] == state
        actual.next_poll_at = now
        session.add(actual)
        session.commit()
    assert runtime.maintenance.run(now=now + INTERVAL)["polled"] == 1
    with Session(runtime.engine) as session:
        assert (
            session.get(Source, source.id).config_json["handsfree_low_water_supplement"]
            == state
        )


@pytest.mark.parametrize("quarantined", [False, True])
def test_auto_topic_disable_and_two_owned_wip_bound_selection(runtime, quarantined):
    enable(runtime, handsfree_auto_topic_enabled=False)
    row = candidate(runtime)
    assist(runtime, row)
    now = utc_now()
    assert runtime.maintenance.run(now=now)["selected"] == 0
    existing = [candidate(runtime) for _ in range(2)]
    historical = []
    with Session(runtime.engine) as session:
        session.get(RuntimeSetting, "handsfree_auto_topic_enabled").value = "true"
        for index in range(2):
            content = Content(
                candidate_id=existing[index].id,
                title="Existing",
                content_dir=str(runtime.settings.data_dir / f"old-{index}"),
                business_status=ContentStatus.WAITING_AGENT,
                current_stage="agent_context",
            )
            session.add(content)
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="handsfree-coordinator",
                    entity_type="content",
                    entity_id=content.id,
                    action="handsfree_candidate_selected",
                    metadata_json={},
                )
            )
            if quarantined:
                session.flush()
                task = runtime.task_service(session).create_task(
                    task_type=TaskType.CONTENT_RESEARCH,
                    instructions="frozen historical research",
                    input_data={"historical": True},
                    content_id=content.id,
                )
                historical.append((content.id, task.id, task.model_dump(mode="json")))
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="import",
                        entity_type="content",
                        entity_id=content.id,
                        action=CONTENT_QUARANTINE,
                        metadata_json={},
                    )
                )
        session.commit()
        assert _handsfree_wip_count(session) == (0 if quarantined else 2)
    result = runtime.maintenance.run(now=now + INTERVAL)
    assert result["selected"] == result["prepared"] == int(quarantined)
    with Session(runtime.engine) as session:
        for content_id, task_id, before in historical:
            assert (
                session.get(Content, content_id).business_status
                == ContentStatus.WAITING_AGENT
            )
            assert session.get(AgentTask, task_id).model_dump(mode="json") == before
            with pytest.raises(TaskConflictError, match="MPT_MIGRATION_QUARANTINED"):
                runtime.task_service(session).claim(
                    agent_id="research-fixture",
                    capabilities={TaskType.CONTENT_RESEARCH},
                    task_id=task_id,
                )
        if quarantined:
            new_content = session.exec(
                select(Content).where(Content.candidate_id == row.id)
            ).one()
            assert new_content.business_status == ContentStatus.WAITING_AGENT
            assert new_content.current_stage == "agent_context"
            research = session.exec(
                select(AgentTask).where(
                    AgentTask.content_id == new_content.id,
                    AgentTask.task_type == TaskType.CONTENT_RESEARCH,
                )
            ).one()
            assert research.status == TaskStatus.PENDING


@pytest.mark.parametrize("status", list(ContentStatus))
@pytest.mark.parametrize("quarantined", [False, True])
def test_handsfree_wip_count_preserves_status_and_selection_contract(
    runtime, status, quarantined
):
    rows = [candidate(runtime) for _ in range(2)]
    with Session(runtime.engine) as session:
        for index, row in enumerate(rows):
            content = Content(
                candidate_id=row.id,
                title="Existing",
                content_dir=str(runtime.settings.data_dir / f"old-{index}"),
                business_status=status,
            )
            session.add(content)
            if index == 0:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="handsfree-coordinator",
                        entity_type="content",
                        entity_id=content.id,
                        action="handsfree_candidate_selected",
                        metadata_json={},
                    )
                )
                if quarantined:
                    session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id="import",
                            entity_type="content",
                            entity_id=content.id,
                            action=CONTENT_QUARANTINE,
                            metadata_json={},
                        )
                    )
        session.commit()
        expected = int(
            not quarantined
            and status not in {ContentStatus.ARCHIVED, ContentStatus.PUBLISHED}
        )
        assert _handsfree_wip_count(session) == expected


def test_quarantined_paused_pending_task_is_frozen_and_not_reaped(runtime):
    add_source(runtime)
    row = candidate(runtime)
    with Session(runtime.engine) as session:
        content = Content(
            candidate_id=row.id,
            title="Paused",
            content_dir=str(runtime.settings.data_dir / "paused"),
        )
        session.add(content)
        session.commit()
        task = runtime.workflow_service(session).create_agent_task(
            content.id, TaskType.SCRIPT_QC, {"frozen": True}
        )
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="import",
                entity_type="content",
                entity_id=content.id,
                action=CONTENT_QUARANTINE,
                metadata_json={"paused": True},
            )
        )
        session.commit()
        session.refresh(task)
        task_id = task.id
        before = task.model_dump(mode="json")
    runtime.maintain_dispatch()
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, task_id).model_dump(mode="json") == before


def test_runtime_close_keeps_engine_when_thread_wont_stop(runtime, monkeypatch):
    class Stuck:
        def join(self, timeout):
            assert timeout == 30

        def is_alive(self):
            return True

    runtime.maintenance.thread = Stuck()
    disposed = []
    monkeypatch.setattr(runtime.engine, "dispose", lambda: disposed.append(True))
    with pytest.raises(RuntimeError, match="still using"):
        runtime.close()
    assert not disposed
    runtime.maintenance.thread = None
    assert not [
        t
        for t in counts(runtime, AgentTask)
        if t.task_type in {TaskType.CONTENT_RESEARCH, TaskType.CANDIDATE_ENRICH}
    ]


def test_loop_stop_is_bounded_and_runtime_does_not_dispose_active_thread(
    runtime, monkeypatch
):
    entered, release = Event(), Event()

    def blocked():
        entered.set()
        release.wait(5)

    monkeypatch.setattr(runtime, "maintain_dispatch", blocked)
    runtime.start_maintenance()
    assert entered.wait(2)
    release.set()
    runtime.close()
    assert not runtime.maintenance.thread.is_alive()


def test_one_unexpected_poll_failure_does_not_starve_other_sources(
    runtime, monkeypatch
):
    from glance_collector.service import CollectionService

    first = add_source(runtime)
    second = add_source(runtime)
    real = CollectionService.collect_source

    def fail_one(self, source_id):
        if source_id == first.id:
            raise RuntimeError("fixture source failure")
        return real(self, source_id)

    monkeypatch.setattr(CollectionService, "collect_source", fail_one)
    assert runtime.maintenance.run()["polled"] == 1
    assert [t.input_data["source_id"] for t in counts(runtime, AgentTask)] == [
        second.id
    ]


def test_periodic_publish_recheck_filters_before_limit_and_keeps_waiting_items(
    runtime, monkeypatch
):
    from app.business import publish_policy

    enable(runtime, handsfree_auto_topic_enabled=False)
    rows = [candidate(runtime) for _ in range(22)]
    valid = None
    with Session(runtime.engine) as session:
        for index, row in enumerate(rows):
            content = Content(
                candidate_id=row.id,
                title="Packaged",
                current_stage="packaged",
                content_dir=str(runtime.settings.data_dir / f"packed-{index}"),
                business_status=ContentStatus.PACKAGED,
            )
            session.add(content)
            if index >= 20:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="handsfree-coordinator",
                        entity_type="content",
                        entity_id=content.id,
                        action="handsfree_candidate_selected",
                        metadata_json={},
                    )
                )
            if index == 21:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="import",
                        entity_type="content",
                        entity_id=content.id,
                        action=CONTENT_QUARANTINE,
                        metadata_json={},
                    )
                )
            if index == 20:
                valid = content.id
        session.commit()
    calls = []

    def waiting(engine, settings, content_id, *, now=None):
        assert engine is runtime.engine and settings is runtime.settings
        calls.append((content_id, now))
        return {"status": "WAITING_POLICY", "retry_after_sec": 300}

    monkeypatch.setattr(publish_policy, "try_auto_publish", waiting)
    now = utc_now()
    runtime.maintenance.run(now=now)
    runtime.maintenance.run(now=now + INTERVAL)
    assert calls == [(valid, now), (valid, now + INTERVAL)]
    assert all(
        c.business_status == ContentStatus.PACKAGED for c in counts(runtime, Content)
    )
    assert not [
        t for t in counts(runtime, AgentTask) if t.task_type == TaskType.DOUYIN_PUBLISH
    ]


def test_consumed_human_result_tampering_cannot_turn_into_automatic_selection(runtime):
    enable(runtime)
    row = candidate(runtime)
    task = assist(runtime, row, verdict="HUMAN")
    task_id = task.id
    with Session(runtime.engine) as session:
        original = session.exec(
            select(AuditEvent).where(
                AuditEvent.action == "mpt_agent_consumed",
                AuditEvent.entity_id == task_id,
            )
        ).one()
        receipt_before = original.model_dump(mode="json")
        current = session.get(AgentTask, task_id)
        current.result_data = {
            "items": [
                {"candidate_id": row.id, "verdict": "SELECT", "confidence": "high"}
            ]
        }
        session.add(current)
        session.commit()
    with pytest.raises(ValueError, match="Consumed Agent result changed"):
        runtime.consume_agent(task_id)
    assert runtime.maintenance.run()["selected"] == 0
    assert not counts(runtime, Content)
    with Session(runtime.engine) as session:
        receipt = session.exec(
            select(AuditEvent).where(
                AuditEvent.action == "mpt_agent_consumed",
                AuditEvent.entity_id == task_id,
            )
        ).one()
        assert receipt.model_dump(mode="json") == receipt_before


def test_twenty_unexpected_poll_failures_cannot_starve_twenty_first_source(
    runtime, monkeypatch
):
    from glance_collector.service import CollectionService

    now = utc_now()
    sources = [add_source(runtime) for _ in range(21)]
    with Session(runtime.engine) as session:
        for index, source in enumerate(sources):
            current = session.get(Source, source.id)
            current.next_poll_at = now - timedelta(minutes=30 - index)
            session.add(current)
        session.commit()
    failed = {source.id for source in sources[:20]}
    original = CollectionService.collect_source

    def fail_first_twenty(self, source_id):
        if source_id in failed:
            raise RuntimeError("persistent source failure")
        return original(self, source_id)

    monkeypatch.setattr(CollectionService, "collect_source", fail_first_twenty)
    assert runtime.maintenance.run(now=now)["polled"] == 0
    runtime.maintenance.run(now=now + INTERVAL)
    assert [task.input_data["source_id"] for task in counts(runtime, AgentTask)] == [
        sources[20].id
    ]
    failures = [
        a for a in counts(runtime, AuditEvent) if a.action == "mpt_source_poll_failed"
    ]
    assert {a.entity_id for a in failures} == failed
    assert all(a.metadata_json["error_type"] == "RuntimeError" for a in failures)


@pytest.mark.parametrize(
    "mutation", ["input", "owned", "missing_receipt_hash", "conflicting_receipt"]
)
def test_frozen_input_ownership_and_all_receipt_hashes_are_required(runtime, mutation):
    enable(runtime)
    row = candidate(runtime)
    task = assist(runtime, row)
    task_id = task.id
    with Session(runtime.engine) as session:
        current = session.get(AgentTask, task_id)
        receipt = session.exec(
            select(AuditEvent).where(
                AuditEvent.action == "mpt_agent_consumed",
                AuditEvent.entity_id == task_id,
            )
        ).one()
        receipt_before = receipt.model_dump(mode="json")
        if mutation == "input":
            current.input_data = {
                **current.input_data,
                "scope": "changed after consumption",
            }
            session.add(current)
        elif mutation == "owned":
            ownership = session.exec(
                select(AuditEvent).where(
                    AuditEvent.action == "mpt_task_owned",
                    AuditEvent.entity_id == task_id,
                )
            ).one()
            ownership.metadata_json = {"input_hash": "wrong"}
            session.add(ownership)
        elif mutation == "missing_receipt_hash":
            receipt.metadata_json = {}
            session.add(receipt)
        else:
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="test",
                    entity_type="agent_task",
                    entity_id=task_id,
                    action="mpt_agent_consumed",
                    metadata_json={"result_hash": "wrong"},
                )
            )
        session.commit()
    assert runtime.maintenance.run()["selected"] == 0
    assert not counts(runtime, Content)
    with Session(runtime.engine) as session:
        receipt = session.get(AuditEvent, receipt_before["id"])
        assert receipt.metadata_json == (
            {}
            if mutation == "missing_receipt_hash"
            else receipt_before["metadata_json"]
        )


def test_source_history_uses_actual_discovery_receipt_and_rejects_changed_notes(
    runtime,
):
    from app.business.maintenance import _session
    from glance_core.research import research_history

    add_source(runtime)
    runtime.maintenance.run()
    task_id = counts(runtime, AgentTask)[0].id
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        direction = task.input_data["direction_key"]
        task.status = TaskStatus.COMPLETED
        task.completed_at = utc_now()
        task.result_data = {
            "candidates": [
                {
                    "external_id": "history-one",
                    "url": "https://example.org/history-one",
                    "title": "New project",
                }
            ],
            "notes": "actually consumed discovery",
        }
        session.add(task)
        session.commit()
    assert runtime.consume_discovery(task_id)
    with _session(runtime) as session:
        searches = research_history(session, direction_key=direction)["recent_searches"]
        assert [row["task_id"] for row in searches] == [task_id]
        assert searches[0]["notes"] == "actually consumed discovery"
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        task.result_data = {**task.result_data, "notes": "changed after receipt"}
        session.add(task)
        session.commit()
    with _session(runtime) as session:
        assert not research_history(session, direction_key=direction)["recent_searches"]
