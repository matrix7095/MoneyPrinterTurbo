import hashlib
import json
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_core.agent_roles import AgentRole, role_spec
from glance_core.content_acceptance import acceptance_subject, source_context
from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    Content,
    ContentStatus,
    PublicationMode,
    PublicationRecord,
    RuntimeSetting,
    TaskStatus,
    TaskType,
)
from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS
from glance_core.runtime_config import RuntimeSettingsError, effective_settings
from glance_core.task_service import TaskConflictError
from glance_core.workflow import handsfree_review_policy
from PIL import Image
from sqlmodel import Session, select

from app.business.publish_policy import publish_budget, try_auto_publish
from app.business.media_executor import MptMediaExecutor
from test.business import test_content_http as content_tests
from test.business.test_content_http import (
    content_app,
    client_for,
    patched_asr,
    acceptance_fixture,
)
from test.business.test_discovery_http import ADMIN, make_runtime

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def unique_candidate(runtime):
    key = uuid4().hex
    with Session(runtime.engine) as session:
        candidate, _ = runtime.workflow_service(session).ingest_candidate(
            source_type="agent",
            source_id=key,
            source_url=f"https://example.org/{key}",
            original_title=key,
            rights_status=content_tests.RightsStatus.EXPLICIT_LICENSE,
        )
        return candidate.id


def seed_publish(runtime, *, automatic=True):
    candidate_id = unique_candidate(runtime)
    with Session(runtime.engine) as session:
        content = Content(
            candidate_id=candidate_id,
            title="Quota fixture",
            content_dir=str(runtime.settings.data_dir / candidate_id),
            business_status=ContentStatus.PACKAGED,
        )
        session.add(content)
        session.commit()
        if automatic:
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="handsfree-coordinator",
                    entity_type="content",
                    entity_id=content.id,
                    action="handsfree_candidate_selected",
                )
            )
            session.commit()
        task = runtime.workflow_service(session).create_agent_task(
            content.id,
            TaskType.DOUYIN_PUBLISH,
            {
                "platform": "douyin",
                "account": {"account_id": "local-default"},
                "video_artifact_id": "asset_" + "a" * 32,
                "video_sha256": "b" * 64,
                "publish_policy": {
                    "initiated_by": "handsfree" if automatic else "operator",
                    "user_clicked": not automatic,
                    "explicit_user_opt_in": automatic,
                },
            },
        )
        return content.id, task.id


def claim_publish(http, task_id, agent_id="publish-worker"):
    client = publisher(http, agent_id)
    claim = client.dispatch(wait=0, task_id=task_id)
    assert claim["should_run"], claim
    assert claim["task_id"] == task_id
    client.confirm_claim(task_id, claim["lease_token"], agent_id)
    return {"Authorization": f"Bearer {claim['lease_token']}"}


def publisher(http, agent_id="publish-worker"):
    spec = role_spec(AgentRole.PUBLISH)
    client = AgentClient(
        AgentSettings(
            token="test-root",
            agent_id=agent_id,
            role_id="publish",
            skill_name=spec.skill_name,
            skill_version=spec.skill_version,
            protocol_version=spec.protocol_version,
            capabilities=list(spec.required_host_capabilities),
        )
    )
    client.client.close()
    client.client = http
    return client


def test_zero_quota_blocks_real_http_publish_intent(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_daily_publish_limit = 0
    with TestClient(content_app(runtime)) as http:
        _, task_id = seed_publish(runtime)
        headers = claim_publish(http, task_id)
        response = http.post(
            f"/api/v1/agent/tasks/{task_id}/publish-intent",
            headers=headers,
            json={"agent_id": "publish-worker"},
        )
        assert response.status_code == 409, response.text
        assert "daily_publish_limit_is_zero" in response.text


@pytest.mark.parametrize(
    "published_at,limit,interval,expected",
    [
        (NOW, 1, 0, "daily_publish_limit_reached"),
        (NOW - timedelta(minutes=119), 2, 120, "publish_interval_not_elapsed"),
        (NOW - timedelta(minutes=120), 2, 120, "ALLOWED"),
        # Same UTC day, opposite Shanghai-local days (23:59 -> 00:00).
        (datetime(2026, 10, 10, 15, 59, tzinfo=timezone.utc), 1, 0, "ALLOWED"),
        (
            datetime(2026, 10, 10, 16, 0, tzinfo=timezone.utc),
            1,
            0,
            "daily_publish_limit_reached",
        ),
    ],
)
def test_budget_counts_manual_historical_records_in_shanghai_day(
    tmp_path, published_at, limit, interval, expected
):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_daily_publish_limit = limit
    runtime.settings.handsfree_publish_interval_minutes = interval
    with TestClient(content_app(runtime)):
        content_id, _ = seed_publish(runtime, automatic=False)
        with Session(runtime.engine) as session:
            session.add(
                PublicationRecord(
                    content_id=content_id,
                    mode=PublicationMode.MANUAL,
                    platform="douyin",
                    status="PUBLISHED",
                    published_at=published_at,
                )
            )
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="migration",
                    entity_type="content",
                    entity_id=content_id,
                    action="mpt_migration_quarantined_content",
                    metadata_json={"verified": False, "historical": True},
                )
            )
            session.commit()
            now = (
                datetime(2026, 10, 10, 16, 1, tzinfo=timezone.utc)
                if published_at.hour in {15, 16}
                else NOW
            )
            decision = publish_budget(session, runtime.settings, now=now)
            assert decision.get("reason", decision["status"]) == expected


def ready_content(http, runtime, monkeypatch, *, automatic=True):
    """Temporary contract fixture: real encoded audio/video and accepted QC-v2 HTTP result."""
    monkeypatch.setattr(content_tests, "seed_candidate", unique_candidate)
    runtime.settings.asr_model = "test-asr"
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_auto_review_enabled = False
    runtime.settings.publish_prepare_enabled = False
    runtime.settings.cover_ai_enabled = False
    runtime.settings.archive_enabled = False
    runtime.settings.handsfree_daily_publish_limit = (
        0  # Fixture review never queues a click task.
    )
    patched_asr(monkeypatch, available=False)
    def synthesize_voice(executor, *args, **kwargs):
        voice = executor.root / "audio" / "manual-voice.wav"
        voice.parent.mkdir(parents=True, exist_ok=True)
        voice.write_bytes(b"isolated successful manual voice fixture")
        return SimpleNamespace(audio_path=voice, duration_sec=45)

    with monkeypatch.context() as voice_patch:
        voice_patch.setattr(MptMediaExecutor, "synthesize_voice", synthesize_voice)
        voice_patch.setattr(MptMediaExecutor, "visual_timing_evidence", lambda *args: {})
        content_id, video_id, _, _, receipt_id = acceptance_fixture(http, runtime, manual_script_approval=True)
    with Session(runtime.engine) as session:
        if automatic:
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="handsfree-coordinator",
                    entity_type="content",
                    entity_id=content_id,
                    action="handsfree_candidate_selected",
                )
            )
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        source_image = root / "source-image.png"
        Image.new("RGB", (64, 64), "black").save(source_image)
        image = Artifact(
            content_id=content_id,
            artifact_type="source_image",
            relative_path=source_image.name,
            sha256=hashlib.sha256(source_image.read_bytes()).hexdigest(),
            size_bytes=source_image.stat().st_size,
            mime_type="image/png",
            producer_stage="prepare",
        )
        session.add(image)
        plan = session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.VISUAL_PLAN,
            )
        ).one()
        plan.result_data = {"segments": [{"asset_id": image.id}]}
        session.add(plan)
        session.commit()
        artifact = session.get(Artifact, receipt_id)
        receipt = root / artifact.relative_path
        frozen = json.loads(receipt.read_text(encoding="utf-8"))
        if automatic:
            frozen["acceptance_origin"] = "handsfree"
        refs = {**frozen["upstream_task_ids"], "video_artifact_id": video_id}
        frozen["source_context"] = source_context(session, content_id, refs)
        frozen["acceptance_subject"] = acceptance_subject(
            session, content_id, refs, source_context_version=1
        )
        assert not frozen["source_context"]["evidence_gaps"]
        receipt.write_text(json.dumps(frozen), encoding="utf-8")
        artifact.sha256 = hashlib.sha256(receipt.read_bytes()).hexdigest()
        artifact.size_bytes = receipt.stat().st_size
        session.add(artifact)
        session.commit()
    patched_asr(monkeypatch, available=True)
    response = http.post(
        f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
    )
    assert response.status_code == 200, response.text
    with Session(runtime.engine) as session:
        qc = session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.CONTENT_QC,
            )
        ).one()
        result = {
            "contract_version": 2,
            "passed": True,
            "issues": [],
            "confidence": "high",
            "subject": qc.input_data["acceptance_subject"],
            "checks": {
                key: {"passed": True, "evidence": "Isolated contract fixture review"}
                for key in CONTENT_ACCEPTANCE_CHECKS
            },
        }
        qc_id = qc.id
    script = client_for(http, "script")
    claimed = script.dispatch(wait=0, task_id=qc_id)
    script.confirm_claim(qc_id, claimed["lease_token"], "script-worker")
    try:
        script.complete(qc_id, claimed["lease_token"], "script-worker", result)
    except httpx.HTTPStatusError as exc:
        pytest.fail(exc.response.text)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        content.business_status, content.current_stage = (
            ContentStatus.PACKAGED,
            "packaged",
        )
        session.add(content)
        root = Path(content.content_dir)
        final = session.get(Artifact, video_id)
        package = root / "publish" / "final.mp4"
        package.parent.mkdir(exist_ok=True)
        shutil.copy2(root / final.relative_path, package)
        for kind, path, mime in (
            ("publish_video", package, "video/mp4"),
            ("cover", root / "publish" / "cover.png", "image/png"),
        ):
            if kind == "cover":
                Image.new("RGB", (64, 64), "green").save(path)
            session.add(
                Artifact(
                    content_id=content_id,
                    artifact_type=kind,
                    relative_path=path.relative_to(root).as_posix(),
                    sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    size_bytes=path.stat().st_size,
                    mime_type=mime,
                    producer_stage="package",
                )
            )
        session.commit()
        flow = runtime.workflow_service(session)
        review = flow.create_agent_task(
            content_id,
            TaskType.PUBLISH_REVIEW_ASSIST,
            flow.build_publish_review_input(
                content_id,
                settings=runtime.settings,
                publish_policy=handsfree_review_policy() if automatic else None,
            ),
        )
        review_id = review.id
    assistant = client_for(http, "assistant")
    claimed = assistant.dispatch(wait=0, task_id=review_id)
    assistant.confirm_claim(review_id, claimed["lease_token"], "assistant-worker")
    assistant.complete(
        review_id,
        claimed["lease_token"],
        "assistant-worker",
        {
            "verdict": "READY",
            "confidence": "high",
            "summary": "Contract fixture approved",
            "reasons": ["Current registered final artifacts reviewed"],
            "issues": [],
        },
    )
    return content_id, review_id


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required")
def test_old_packaged_revise_cannot_become_manual_by_disabling_auto_review(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, _ = ready_content(http, runtime, monkeypatch)
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            parents = {key: flow.completed_task_for(content_id, kind) for key, kind in (
                ("script", TaskType.SHORT_SCRIPT), ("script_qc", TaskType.SCRIPT_QC),
                ("research", TaskType.CONTENT_RESEARCH),
            )}
            task = flow.create_agent_task(content_id, TaskType.SCRIPT_REVIEW_ASSIST, {
                **{key: row.result_data for key, row in parents.items()},
                "upstream_task_ids": {key: row.id for key, row in parents.items()},
            }, allow_multiple=True)
            task_id = task.id
        assistant = client_for(http, "assistant")
        claim = assistant.dispatch(wait=0, task_id=task_id)
        assistant.confirm_claim(task_id, claim["lease_token"], "assistant-worker")
        assistant.complete(task_id, claim["lease_token"], "assistant-worker", {
            "verdict": "REVISE", "confidence": "high", "summary": "Unresolved script blocker",
            "reasons": ["Opening misleads"], "issues": [{"severity": "blocker", "message": "Opening misleads"}],
        })
        runtime.settings.handsfree_daily_publish_limit = 2
        runtime.settings.handsfree_publish_interval_minutes = 0
        decision = try_auto_publish(runtime.engine, runtime.settings, content_id, now=NOW)
        assert decision["status"] == "NOT_READY", decision
        assert "script assistant review" in decision["reason"]


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_ready_http_intent_uses_runtime_policy_and_durable_retry(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, _ = ready_content(http, runtime, monkeypatch)
        assert (
            try_auto_publish(runtime.engine, runtime.settings, content_id)["status"]
            == "WAITING_POLICY"
        )
        runtime.settings.handsfree_daily_publish_limit = 1
        queued = try_auto_publish(runtime.engine, runtime.settings, content_id)
        assert queued["status"] == "QUEUED", queued
        assert (
            try_auto_publish(runtime.engine, runtime.settings, content_id)["status"]
            == "ALREADY_QUEUED"
        )
        client = publisher(http)
        claim = client.dispatch(wait=0, task_id=queued["task_id"])
        client.confirm_claim(claim["task_id"], claim["lease_token"], "publish-worker")
        with Session(runtime.engine) as session:
            session.add(RuntimeSetting(key="handsfree_daily_publish_limit", value="0"))
            session.commit()
        blocked = http.post(
            f"/api/v1/agent/tasks/{claim['task_id']}/publish-intent",
            headers={"Authorization": f"Bearer {claim['lease_token']}"},
            json={"agent_id": "publish-worker"},
        )
        assert (
            blocked.status_code == 409 and "daily_publish_limit_is_zero" in blocked.text
        )
        with Session(runtime.engine) as session:
            setting = session.get(RuntimeSetting, "handsfree_daily_publish_limit")
            setting.value = "1"
            session.add(setting)
            session.commit()
        authorized = client.register_publish_intent(
            claim["task_id"], claim["lease_token"], "publish-worker"
        )
        assert authorized["click_authorized"] is True
        runtime.settings.handsfree_enabled = False
        retry = client.register_publish_intent(
            claim["task_id"], claim["lease_token"], "publish-worker"
        )
        assert (
            retry["click_authorized"] is False
            and retry["status"] == "RECONCILE_REQUIRED"
        )
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.action == "publish_intent_registered"
                        )
                    ).all()
                )
                == 1
            )
    restarted = make_runtime(tmp_path)
    with TestClient(content_app(restarted)) as http:
        retry = publisher(http).register_publish_intent(
            claim["task_id"], claim["lease_token"], "publish-worker"
        )
        assert retry["click_authorized"] is False


@pytest.mark.parametrize(
    "status,expected",
    [
        ("AUTHORIZED", "WAITING_POLICY"),
        ("UNKNOWN", "RECONCILE_REQUIRED"),
        ("INTERRUPTED", "RECONCILE_REQUIRED"),
        ("NEEDS_USER", "RECONCILE_REQUIRED"),
        ("FAILED", "ALLOWED"),
        ("PUBLISHED", "WAITING_POLICY"),
    ],
)
def test_persisted_reservation_never_guesses_unknown_outcome(
    tmp_path, status, expected
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)):
        with Session(runtime.engine) as session:
            # No PublicationRecord yet: a completed intent's actual outcome still counts.
            session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id="lost-worker",
                    entity_type="publish_intent",
                    entity_id="test-intent",
                    action="publish_intent_registered",
                    metadata_json={
                        "platform": "douyin",
                        "account_id": "local-default",
                        "status": "AUTHORIZED",
                        "content_id": "old",
                        "intent_sequence": 0,
                    },
                    created_at=NOW - timedelta(days=2),
                )
            )
            session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id="lost-worker",
                    entity_type="publish_intent",
                    entity_id="test-intent",
                    action="publish_intent_state",
                    metadata_json={
                        "platform": "douyin",
                        "account_id": "local-default",
                        "status": status,
                        "content_id": "old",
                        "intent_sequence": 1,
                    },
                    created_at=NOW,
                )
            )
            session.commit()
    restarted = make_runtime(tmp_path)
    with TestClient(content_app(restarted)):
        with Session(restarted.engine) as session:
            assert (
                publish_budget(session, restarted.settings, now=NOW)["status"]
                == expected
            )


def test_explicit_manual_publish_does_not_inherit_automatic_quota_or_origin(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_daily_publish_limit = 0
    with TestClient(content_app(runtime)) as http:
        content_id, task_id = seed_publish(runtime, automatic=False)
        headers = claim_publish(http, task_id)
        result = publisher(http).register_publish_intent(
            task_id, headers["Authorization"][7:], "publish-worker"
        )
        assert result["click_authorized"] is True
        with Session(runtime.engine) as session:
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == content_id,
                        AuditEvent.action == "handsfree_candidate_selected",
                    )
                ).all()
                == []
            )
            assert (
                session.get(AgentTask, task_id).input_data["publish_policy"][
                    "initiated_by"
                ]
                == "operator"
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_two_real_http_intents_cannot_spend_the_same_budget(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        contents = [ready_content(http, runtime, monkeypatch)[0] for _ in range(2)]
        runtime.settings.handsfree_daily_publish_limit = 1
        runtime.settings.handsfree_publish_interval_minutes = 0
        queued = [
            try_auto_publish(runtime.engine, runtime.settings, content_id)
            for content_id in contents
        ]
        assert all(item["status"] == "QUEUED" for item in queued), queued
        headers = [
            claim_publish(http, item["task_id"], f"publish-{index}")
            for index, item in enumerate(queued)
        ]
        barrier = Barrier(2)

        def authorize(index):
            barrier.wait(timeout=10)
            return http.post(
                f"/api/v1/agent/tasks/{queued[index]['task_id']}/publish-intent",
                headers=headers[index],
                json={"agent_id": f"publish-{index}"},
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(authorize, range(2)))
        assert sorted(response.status_code for response in responses) == [200, 409], [
            r.text for r in responses
        ]
        assert (
            sum(
                response.json().get("click_authorized", False) for response in responses
            )
            == 1
        )
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.action == "publish_intent_registered"
                        )
                    ).all()
                )
                == 1
            )
            assert all(
                session.get(AgentTask, item["task_id"]).status == TaskStatus.LEASED
                for item in queued
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
@pytest.mark.parametrize(
    "change", ["rights", "script", "final", "cover", "review", "qc", "disabled"]
)
def test_previously_queued_auto_task_rechecks_current_gates_before_click(
    tmp_path, monkeypatch, change
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, review_id = ready_content(http, runtime, monkeypatch)
        runtime.settings.handsfree_daily_publish_limit = 1
        queued = try_auto_publish(runtime.engine, runtime.settings, content_id)
        assert queued["status"] == "QUEUED", queued
        headers = claim_publish(http, queued["task_id"])
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            if change == "rights":
                candidate = session.get(Candidate, content.candidate_id)
                candidate.rights_status = content_tests.RightsStatus.REJECTED
                session.add(candidate)
            elif change in {"script", "qc"}:
                kind = (
                    TaskType.SHORT_SCRIPT if change == "script" else TaskType.CONTENT_QC
                )
                task = session.exec(
                    select(AgentTask).where(
                        AgentTask.content_id == content_id, AgentTask.task_type == kind
                    )
                ).one()
                task.result_data = {
                    **task.result_data,
                    **(
                        {"full_narration": "Changed"}
                        if change == "script"
                        else {"passed": False}
                    ),
                }
                session.add(task)
            elif change in {"final", "cover"}:
                kind = "final_video" if change == "final" else "cover"
                artifact = session.exec(
                    select(Artifact).where(
                        Artifact.content_id == content_id,
                        Artifact.artifact_type == kind,
                    )
                ).one()
                (Path(content.content_dir) / artifact.relative_path).write_bytes(
                    b"Changed file"
                )
            elif change == "review":
                review = session.get(AgentTask, review_id)
                review.result_data = {**review.result_data, "confidence": "low"}
                session.add(review)
            else:
                runtime.settings.handsfree_auto_publish_douyin_enabled = False
            session.commit()
        response = http.post(
            f"/api/v1/agent/tasks/{queued['task_id']}/publish-intent",
            headers=headers,
            json={"agent_id": "publish-worker"},
        )
        assert response.status_code == 409, response.text
        with Session(runtime.engine) as session:
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action == "publish_intent_registered"
                    )
                ).all()
                == []
            )


def test_auto_intent_direct_service_without_runtime_settings_fails_closed(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    with TestClient(content_app(runtime)) as http:
        _, task_id = seed_publish(runtime)
        headers = claim_publish(http, task_id)
        with Session(runtime.engine) as session:
            service = runtime.task_service(session)
            with pytest.raises(TaskConflictError, match="effective MPT settings"):
                service.register_publish_intent(
                    task_id=task_id,
                    lease_token=headers["Authorization"][7:],
                    agent_id="publish-worker",
                )
            assert service._mpt_publish_policy_context is None


def test_quota_wait_keeps_packaged_and_never_duplicates_tasks(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_daily_publish_limit = 0
    with TestClient(content_app(runtime)):
        content_id, _ = seed_publish(runtime)
        with Session(runtime.engine) as session:
            task = session.exec(
                select(AgentTask).where(AgentTask.content_id == content_id)
            ).one()
            session.delete(task)
            session.commit()
        for _ in range(3):
            decision = try_auto_publish(
                runtime.engine, runtime.settings, content_id, now=NOW
            )
            assert decision["status"] == "WAITING_POLICY"
            assert 0 < decision["retry_after_sec"] <= 300
        with Session(runtime.engine) as session:
            assert (
                session.get(Content, content_id).business_status
                == ContentStatus.PACKAGED
            )
            assert (
                session.exec(
                    select(AgentTask).where(AgentTask.content_id == content_id)
                ).all()
                == []
            )


def test_unknown_terminal_task_never_queues_automatic_retry(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    with TestClient(content_app(runtime)):
        content_id, task_id = seed_publish(runtime)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            task.status, task.result_data = TaskStatus.COMPLETED, {"status": "UNKNOWN"}
            session.add(task)
            session.commit()
        assert (
            try_auto_publish(runtime.engine, runtime.settings, content_id)["status"]
            == "RECONCILE_REQUIRED"
        )
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AgentTask).where(AgentTask.content_id == content_id)
                    ).all()
                )
                == 1
            )
            assert (
                session.get(Content, content_id).business_status
                == ContentStatus.PACKAGED
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_independent_process_http_intents_share_durable_reservation(
    tmp_path, monkeypatch
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        contents = [ready_content(http, runtime, monkeypatch)[0] for _ in range(2)]
        runtime.settings.handsfree_daily_publish_limit = 1
        queued = [
            try_auto_publish(runtime.engine, runtime.settings, content_id)
            for content_id in contents
        ]
        assert all(item["status"] == "QUEUED" for item in queued)
        headers = [
            claim_publish(http, item["task_id"], f"publish-{index}")
            for index, item in enumerate(queued)
        ]
        code = """
import json, sys
from pathlib import Path
from fastapi.testclient import TestClient
from test.business.test_content_http import content_app
from test.business.test_discovery_http import make_runtime
runtime = make_runtime(Path(sys.argv[1]))
runtime.settings.handsfree_enabled = True
runtime.settings.publish_prepare_enabled = False
runtime.settings.cover_ai_enabled = False
with TestClient(content_app(runtime)) as http:
    print('READY', flush=True)
    sys.stdin.readline()
    response = http.post('/api/v1/agent/tasks/' + sys.argv[2] + '/publish-intent',
        headers={'Authorization': sys.argv[3]}, json={'agent_id': sys.argv[4]})
    print(json.dumps({'code': response.status_code, 'body': response.json()}), flush=True)
"""
        children = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(tmp_path),
                    item["task_id"],
                    headers[index]["Authorization"],
                    f"publish-{index}",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            for index, item in enumerate(queued)
        ]
        try:
            for child in children:
                assert child.stdout.readline().strip() == "READY"
            for child in children:
                child.stdin.write("go\n")
                child.stdin.flush()
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                assert child.returncode == 0, stderr
                results.append(json.loads(stdout.strip()))
            assert sorted(item["code"] for item in results) == [200, 409], results
            assert (
                sum(item["body"].get("click_authorized", False) for item in results)
                == 1
            )
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=10)
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.action == "publish_intent_registered"
                        )
                    ).all()
                )
                == 1
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_real_manual_publish_prepare_completion_does_not_claim_handsfree_opt_in(
    tmp_path, monkeypatch
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, previous_review = ready_content(
            http, runtime, monkeypatch, automatic=False
        )
        runtime.settings.publish_prepare_enabled = True
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            cover = session.exec(
                select(Artifact).where(
                    Artifact.content_id == content_id, Artifact.artifact_type == "cover"
                )
            ).one()
            prepare = flow.create_agent_task(
                content_id,
                TaskType.PUBLISH_PREPARE,
                {
                    "script": flow.result_for(content_id, TaskType.SHORT_SCRIPT),
                    "research": flow.result_for(content_id, TaskType.CONTENT_RESEARCH),
                    "cover_artifact_id": cover.id,
                },
            )
            prepare_id = prepare.id
        client = client_for(http, "script")
        claimed = client.dispatch(wait=0, task_id=prepare_id)
        client.confirm_claim(prepare_id, claimed["lease_token"], "script-worker")
        accepted = client.complete(
            prepare_id,
            claimed["lease_token"],
            "script-worker",
            {
                "title": "真实人工项目审核",
                "description": "临时契约夹具的发布说明",
                "tags": [],
            },
        )
        assert accepted["advanced"] == "CONSUMED", accepted
        with Session(runtime.engine) as session:
            created = session.exec(
                select(AgentTask).where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST,
                    AgentTask.id != previous_review,
                )
            ).one()
            assert "publish_policy" not in created.input_data, created.input_data.get(
                "publish_policy"
            )
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == content_id,
                        AuditEvent.action == "handsfree_candidate_selected",
                    )
                ).all()
                == []
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
@pytest.mark.parametrize("auto_enabled", [True, False])
def test_mixed_packaged_publish_prepare_completion_uses_each_actual_owner_and_current_opt_in(
    tmp_path, monkeypatch, auto_enabled
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        manual_id, manual_review = ready_content(
            http, runtime, monkeypatch, automatic=False
        )
        handsfree_id, hf_review = ready_content(http, runtime, monkeypatch)
        runtime.settings.publish_prepare_enabled = True
        with Session(runtime.engine) as session:
            session.add(
                RuntimeSetting(
                    key="handsfree_auto_publish_douyin_enabled",
                    value=json.dumps(auto_enabled),
                )
            )
            session.commit()
            flow = runtime.workflow_service(session)
            tasks = []
            for content_id in (manual_id, handsfree_id):
                cover = session.exec(
                    select(Artifact).where(
                        Artifact.content_id == content_id,
                        Artifact.artifact_type == "cover",
                    )
                ).one()
                task = flow.create_agent_task(
                    content_id,
                    TaskType.PUBLISH_PREPARE,
                    {
                        "script": flow.result_for(content_id, TaskType.SHORT_SCRIPT),
                        "research": flow.result_for(
                            content_id, TaskType.CONTENT_RESEARCH
                        ),
                        "cover_artifact_id": cover.id,
                    },
                )
                tasks.append(task.id)
        client = client_for(http, "script")
        for task_id in tasks:
            claimed = client.dispatch(wait=0, task_id=task_id)
            client.confirm_claim(task_id, claimed["lease_token"], "script-worker")
            accepted = client.complete(
                task_id,
                claimed["lease_token"],
                "script-worker",
                {
                    "title": "按真实归属审核内容",
                    "description": "临时夹具审核说明",
                    "tags": [],
                },
            )
            assert accepted["advanced"] == "CONSUMED", accepted
        with Session(runtime.engine) as session:
            reviews = session.exec(
                select(AgentTask).where(
                    AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST,
                    AgentTask.id.not_in([manual_review, hf_review]),
                )
            ).all()
            assert len(reviews) == 2
            policies = {
                task.content_id: task.input_data.get("publish_policy")
                for task in reviews
            }
            assert policies[manual_id] is None
            assert policies[handsfree_id] == (
                handsfree_review_policy() if auto_enabled else None
            )
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == manual_id,
                        AuditEvent.action == "handsfree_candidate_selected",
                    )
                ).all()
                == []
            )
            assert (
                session.get(AgentTask, hf_review).input_data["publish_policy"]
                == handsfree_review_policy()
            )


def test_shared_publish_review_builder_missing_settings_never_reads_global_authorization(
    tmp_path, monkeypatch
):
    from glance_core import workflow as core_workflow

    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    with TestClient(content_app(runtime)):
        content_id, _ = seed_publish(runtime)
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            monkeypatch.setattr(
                core_workflow,
                "Settings",
                lambda *args, **kwargs: pytest.fail("Must not load global Settings"),
            )
            # Existing keyword-only signatures remain callable without a base;
            # the missing base preserves manual/None and cannot infer HF opt-in.
            assert "publish_policy" not in flow.build_publish_review_input(
                content_id, publish_policy=handsfree_review_policy()
            )
            created = flow.schedule_manual_assist_tasks(
                publish_policy=handsfree_review_policy()
            )
            assert len(created) == 1 and "publish_policy" not in created[0].input_data
            operator = {"initiated_by": "operator", "user_clicked": True}
            assert (
                flow.build_publish_review_input(content_id, publish_policy=operator)[
                    "publish_policy"
                ]
                == operator
            )


def test_invalid_combined_override_never_drops_opt_out_in_review_builder(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    with TestClient(content_app(runtime)):
        content_id, _ = seed_publish(runtime)
        with Session(runtime.engine) as session:
            session.add(
                RuntimeSetting(
                    key="handsfree_auto_publish_douyin_enabled", value="false"
                )
            )
            session.add(RuntimeSetting(key="handsfree_daily_publish_limit", value="21"))
            session.commit()
            with pytest.raises(ValueError, match="RUNTIME_SETTINGS_INVALID"):
                runtime.workflow_service(session).build_publish_review_input(
                    content_id,
                    publish_policy=handsfree_review_policy(),
                    settings=runtime.settings,
                )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_invalid_combined_override_never_drops_opt_out_before_actual_http_click(
    tmp_path, monkeypatch
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, _ = ready_content(http, runtime, monkeypatch)
        runtime.settings.handsfree_daily_publish_limit = 1
        queued = try_auto_publish(runtime.engine, runtime.settings, content_id)
        headers = claim_publish(http, queued["task_id"])
        with Session(runtime.engine) as session:
            session.add(
                RuntimeSetting(
                    key="handsfree_auto_publish_douyin_enabled", value="false"
                )
            )
            session.add(RuntimeSetting(key="handsfree_daily_publish_limit", value="21"))
            session.commit()
        response = http.post(
            f"/api/v1/agent/tasks/{queued['task_id']}/publish-intent",
            headers=headers,
            json={"agent_id": "publish-worker"},
        )
        assert response.status_code in {409, 503}, response.text
        assert "RUNTIME_SETTINGS_INVALID" in response.text
        assert '"21"' not in response.text
        with Session(runtime.engine) as session:
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action == "publish_intent_registered"
                    )
                ).all()
                == []
            )


@pytest.mark.parametrize(
    "key,value",
    [
        ("handsfree_daily_publish_limit", "21"),
        ("handsfree_daily_publish_limit", "private-invalid-value"),
        ("handsfree_auto_publish_douyin_enabled", "private-invalid-value"),
        ("tts_provider", "private-invalid-value"),
    ],
)
def test_invalid_override_diagnostics_are_key_only_and_never_fall_back(
    tmp_path, key, value
):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)):
        with Session(runtime.engine) as session:
            session.add(RuntimeSetting(key=key, value=value))
            session.commit()
            with pytest.raises(RuntimeSettingsError) as captured:
                effective_settings(session, runtime.settings)
            assert captured.value.fields == (key,)
            assert value not in str(captured.value)
            assert "RUNTIME_SETTINGS_INVALID" in str(captured.value)


def test_valid_persisted_overrides_use_explicit_base_without_settings_constructor_or_env(
    tmp_path, monkeypatch
):
    from glance_core.settings import Settings

    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    with TestClient(content_app(runtime)):
        with Session(runtime.engine) as session:
            for key, value in (
                ("handsfree_auto_publish_douyin_enabled", "false"),
                ("handsfree_daily_publish_limit", "2"),
                ("handsfree_publish_interval_minutes", "180"),
            ):
                session.add(RuntimeSetting(key=key, value=value))
            session.commit()
            monkeypatch.setenv("GLANCE_ADMIN_TOKEN", "global-credential-must-not-load")
            monkeypatch.setenv("GLANCE_HANDSFREE_AUTO_PUBLISH_DOUYIN_ENABLED", "true")
            monkeypatch.setattr(
                Settings,
                "__init__",
                lambda *args, **kwargs: pytest.fail(
                    "Must not read BaseSettings env sources"
                ),
            )
            current = effective_settings(session, runtime.settings)
            assert current.handsfree_enabled is True
            assert current.handsfree_auto_publish_douyin_enabled is False
            assert current.handsfree_daily_publish_limit == 2
            assert current.handsfree_publish_interval_minutes == 180
            assert current.admin_token == runtime.settings.admin_token
            assert current.data_dir == runtime.settings.data_dir
            assert runtime.settings.handsfree_auto_publish_douyin_enabled is True


def test_invalid_config_returns_blocked_and_authenticated_dispatch_diagnostic(tmp_path):
    from app.controllers.v1.deployment import router as deployment_router
    from glance_core.security import issue_scheduler_token

    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    app = content_app(runtime)
    app.include_router(deployment_router)
    with TestClient(app) as http:
        content_id, task_id = seed_publish(runtime)
        with Session(runtime.engine) as session:
            session.add(
                RuntimeSetting(
                    key="handsfree_daily_publish_limit", value="private-invalid-value"
                )
            )
            session.commit()
        blocked = try_auto_publish(runtime.engine, runtime.settings, content_id)
        assert blocked == {
            "status": "BLOCKED",
            "reason": "RUNTIME_SETTINGS_INVALID",
            "fields": ["handsfree_daily_publish_limit"],
        }
        response = http.post(
            "/api/v1/agent/dispatch",
            headers={"Authorization": "Bearer test-root"},
            json={
                "agent_id": "publish-worker",
                "role_id": "publish",
                "task_id": task_id,
            },
        )
        assert response.status_code == 503, response.text
        assert (
            "RUNTIME_SETTINGS_INVALID" in response.text
            and "private-invalid-value" not in response.text
        )
        claim = http.post(
            "/api/v1/agent/tasks/claim",
            headers={"Authorization": "Bearer test-root"},
            json={"agent_id": "publish-worker", "role_id": "publish"},
        )
        assert claim.status_code == 503 and "RUNTIME_SETTINGS_INVALID" in claim.text
        plan = http.get(
            "/api/v1/agent/scheduler/plan",
            params={"scheduler_id": "codex", "contract_version": 2},
            headers={
                "Authorization": f"Bearer {issue_scheduler_token('test-root', 'codex')}"
            },
        )
        assert plan.status_code == 503 and "RUNTIME_SETTINGS_INVALID" in plan.text
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, task_id).status == TaskStatus.PENDING
