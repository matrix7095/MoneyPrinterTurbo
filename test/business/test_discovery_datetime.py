from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from glance_collector.service import CollectionService
from glance_core.models import (
    AgentSubmission,
    AgentTask,
    AuditEvent,
    Candidate,
    CandidateSignal,
    CollectionRun,
    TaskStatus,
)
from glance_core.task_service import stable_hash
from sqlmodel import Session, select

from app.business.runtime import CONSUMED
from test.business.test_discovery_http import (
    agent_client,
    issue_and_claim,
    make_app,
    make_runtime,
)


def frozen_submission(runtime, task_id):
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, task_id)
        submission = session.exec(
            select(AgentSubmission).where(AgentSubmission.task_id == task_id)
        ).one()
        assert task.status == TaskStatus.COMPLETED
        assert submission.validation_status == "ACCEPTED"
        assert submission.result_hash == stable_hash(task.result_data)
        return (
            task.input_data,
            task.input_hash,
            task.result_data,
            submission.result_hash,
            Path(submission.raw_result_path).read_bytes(),
        )


@pytest.mark.parametrize(
    "published_at,expected",
    [
        ("2026-10-09", datetime(2026, 10, 9, tzinfo=UTC)),
        ("2026-10-09T12:34:56", datetime(2026, 10, 9, 12, 34, 56, tzinfo=UTC)),
        ("2026-10-09T12:34:56+08:00", datetime(2026, 10, 9, 4, 34, 56, tzinfo=UTC)),
        (None, None),
    ],
)
@pytest.mark.parametrize("interrupted", [False, True])
def test_discovery_dates_consume_and_recover_without_changing_submission(
    tmp_path, monkeypatch, published_at, expected, interrupted
):
    runtime = make_runtime(tmp_path)
    original_ingest = CollectionService._ingest_item

    def interrupt_after_candidate(self, *args, **kwargs):
        original_ingest(self, *args, **kwargs)
        raise RuntimeError("Injected interruption after candidate flush")

    with TestClient(make_app(runtime)) as http:
        agent = agent_client(http)
        claimed = issue_and_claim(http, agent)
        task_id = claimed["task_id"]
        result = {
            "candidates": [
                {
                    "external_id": "dated-project",
                    "url": "https://example.org/dated-project",
                    "title": "Dated project",
                    "published_at": published_at,
                }
            ]
        }
        if interrupted:
            monkeypatch.setattr(
                CollectionService, "_ingest_item", interrupt_after_candidate
            )
        completed = agent.complete(
            task_id, claimed["lease_token"], "http-worker", result
        )
        monkeypatch.setattr(CollectionService, "_ingest_item", original_ingest)
        assert completed["advanced"] == ("PENDING" if interrupted else "CONSUMED")
        frozen = frozen_submission(runtime, task_id)
        if interrupted:
            with Session(runtime.engine) as session:
                assert session.exec(select(Candidate)).all() == []
                assert session.exec(select(CandidateSignal)).all() == []
                assert (
                    session.exec(
                        select(AuditEvent).where(AuditEvent.action == CONSUMED)
                    ).all()
                    == []
                )

    restarted = make_runtime(tmp_path)
    try:
        restarted.initialize()
        restarted.recover_discovery()
        assert restarted.consume_discovery(task_id) is False
        assert frozen_submission(restarted, task_id) == frozen
        with Session(restarted.engine) as session:
            candidate = session.exec(select(Candidate)).one()
            assert candidate.published_at == expected
            if expected is not None:
                assert candidate.published_at.tzinfo == UTC
            assert (
                session.exec(select(CandidateSignal)).one().candidate_id == candidate.id
            )
            run = session.exec(select(CollectionRun)).one()
            assert (run.status, run.created_count, run.duplicate_count) == (
                "SUCCESS",
                1,
                0,
            )
            receipt = session.exec(
                select(AuditEvent).where(AuditEvent.action == CONSUMED)
            ).one()
            assert receipt.entity_id == task_id
            assert receipt.metadata_json["result_hash"] == frozen[3]
    finally:
        restarted.close()


@pytest.mark.parametrize(
    "published_at,expected",
    [
        (datetime(2026, 10, 9, 12), datetime(2026, 10, 9, 12, tzinfo=UTC)),
        (
            datetime(2026, 10, 9, 12, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 10, 9, 4, tzinfo=UTC),
        ),
    ],
)
def test_shared_candidate_ingest_normalizes_datetime(tmp_path, published_at, expected):
    runtime = make_runtime(tmp_path)
    try:
        runtime.initialize()
        with Session(runtime.engine) as session:
            candidate, created = runtime.workflow_service(session).ingest_candidate(
                source_type="agent",
                source_id="datetime-project",
                source_url="https://example.org/datetime-project",
                original_title="Datetime project",
                published_at=published_at,
            )
            assert created
            assert candidate.published_at == expected
            assert candidate.published_at.tzinfo == UTC
    finally:
        runtime.close()


@pytest.mark.parametrize("published_at", ["not-a-date", "2026-02-30"])
def test_invalid_discovery_date_remains_validation_rejected(tmp_path, published_at):
    runtime = make_runtime(tmp_path)
    with TestClient(make_app(runtime)) as http:
        claimed = issue_and_claim(http, agent_client(http))
        response = http.post(
            f"/api/v1/agent/tasks/{claimed['task_id']}/complete",
            headers={"Authorization": f"Bearer {claimed['lease_token']}"},
            json={
                "agent_id": "http-worker",
                "result": {
                    "candidates": [
                        {
                            "external_id": "invalid-date",
                            "url": "https://example.org/invalid-date",
                            "title": "Invalid date",
                            "published_at": published_at,
                        }
                    ]
                },
            },
        )
        assert response.status_code == 422
        runtime.recover_discovery()
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claimed["task_id"])
            assert task.status == TaskStatus.LEASED
            assert task.result_data is None
            assert (
                session.exec(select(AgentSubmission)).one().validation_status
                == "REJECTED"
            )
            assert session.exec(select(Candidate)).all() == []
            assert (
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == CONSUMED)
                ).all()
                == []
            )
