"""Handsfree consumes the current script assistant review before any media side effects."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import timedelta
from pathlib import Path
import json
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from glance_core.database import create_db
from glance_core.models import (
    AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, CandidateStatus, Content,
    ContentStatus, ProductionMode, RightsStatus, RuntimeSetting, TaskStatus, TaskType, utc_now,
)
from glance_core.task_service import stable_hash
from glance_media.executor import sha256_file
from sqlmodel import Session, select
from pydantic import SecretStr

from app.business import pipeline
from app.business.runtime import AGENT_CONSUMED, OWNED
from test.business.test_discovery_http import make_runtime
from test.business.test_content_http import ADMIN, content_app
from test.business.test_visual_plan_materialization import record_accepted_completion


@pytest.fixture
def script_review_contract(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings = runtime.settings.model_copy(update={
        "handsfree_enabled": True, "handsfree_auto_review_enabled": True,
    })
    create_db(runtime.engine)
    with Session(runtime.engine) as session:
        candidate = Candidate(source_type="agent", source_id="review-gate",
                              source_url="https://example.org/product", original_title="Product",
                              status=CandidateStatus.SELECTED, rights_status=RightsStatus.RESEARCH_ONLY)
        session.add(candidate)
        session.commit()
        root = runtime.settings.content_dir / "review-gate"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="Product", content_dir=str(root),
                          production_mode=ProductionMode.IMAGE_ONLY,
                          business_status=ContentStatus.SCRIPT_REVIEW, current_stage="script_review")
        session.add(content)
        session.commit()
        session.add(AuditEvent(actor_type="system", actor_id="handsfree-coordinator",
                               entity_type="content", entity_id=content.id,
                               action="handsfree_candidate_selected"))
        session.commit()
        flow = runtime.workflow_service(session)

        def completed(kind, inputs, result, *, consumed=True):
            task = flow.create_agent_task(content.id, kind, inputs, allow_multiple=True)
            task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
            task.attempt, task.assigned_agent_id = 1, "fixture-worker"
            raw = runtime.settings.agent_submission_dir / task.id / "1" / "result.json"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps(result), encoding="utf-8")
            session.add(task)
            session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=task.assigned_agent_id,
                                       raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED"))
            session.add(AuditEvent(actor_type="agent", actor_id=task.assigned_agent_id,
                                   entity_type="agent_task", entity_id=task.id, action="completed",
                                   metadata_json={"result_hash": stable_hash(result)}))
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                   entity_id=task.id, action=OWNED, metadata_json={"input_hash": task.input_hash}))
            if consumed:
                session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                                       entity_id=task.id, action=AGENT_CONSUMED,
                                       metadata_json={"result_hash": stable_hash(result)}))
            session.commit()
            return task

        research = completed(TaskType.CONTENT_RESEARCH, {}, {"verified_claims": []})
        script = completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}},
                           {"full_narration": "产品测试。"})
        qc = completed(TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}},
                       {"passed": True})

        def review(verdict="APPROVE", *, confidence="high", issues=None):
            return completed(TaskType.SCRIPT_REVIEW_ASSIST, {
                "upstream_task_ids": {"script": script.id, "script_qc": qc.id, "research": research.id},
                "script": deepcopy(script.result_data), "script_qc": deepcopy(qc.result_data),
                "research": deepcopy(research.result_data),
            }, {"verdict": verdict, "confidence": confidence, "summary": "Independent review",
                "reasons": ["Verified the research and current script"], "issues": issues or [], "checklist": []},
                consumed=False)

        yield runtime, session, content, research, script, qc, review, completed
    runtime.close()


def test_passed_script_qc_waits_for_current_assistant_review(script_review_contract):
    runtime, session, content, _, _, qc, _, _ = script_review_contract
    content.current_stage = "script_qc"
    content.business_status = ContentStatus.WAITING_AGENT
    session.add(content)
    session.commit()
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, qc.id) is None
    session.refresh(content)
    assert content.current_stage == "script_review"
    assert len(session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.SCRIPT_REVIEW_ASSIST,
    )).all()) == 1


def test_current_high_confidence_approval_advances_to_voice(script_review_contract):
    runtime, _, _, _, _, _, review, _ = script_review_contract
    task = review()
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) == "voice"


@pytest.mark.parametrize("verdict", ["REVISE", "APPROVE"])
def test_blocker_uses_one_existing_bounded_rewrite(script_review_contract, verdict):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review(verdict, issues=[{"severity": "blocker", "message": "Opening does not answer the viewer"}])
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    session.expire_all()
    rewritten = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.SHORT_SCRIPT,
        AgentTask.status == TaskStatus.PENDING,
    )).one()
    assert rewritten.input_data["auto_assist_revision_attempt"] == 1
    assert "Opening does not answer the viewer" in rewritten.input_data["reviewer_note"]
    session.refresh(content)
    assert content.current_stage == "script_rewrite" and content.script_version == 1
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    session.refresh(content)
    assert content.script_version == 1


@pytest.mark.parametrize("boundary", ["voice", "render", "release"])
def test_unresolved_current_review_blocks_paid_and_release_boundaries(script_review_contract, monkeypatch, boundary):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review("REVISE", issues=[{"severity": "blocker", "message": "Opening is misleading"}])
    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *args: None)
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: pytest.fail("TTS ran before review"))
    monkeypatch.setattr(pipeline.MediaExecutor, "align_subtitles", lambda *args, **kwargs: pytest.fail("ASR ran before review"))
    if boundary != "voice":
        content.current_stage = "render_ready" if boundary == "render" else "package"
        content.business_status = ContentStatus.PRODUCING if boundary == "render" else ContentStatus.PACKAGED
        session.add(content)
        session.commit()
    with pytest.raises(pipeline.NonRetryableMediaError, match="script.*review|Script.*review"):
        if boundary == "voice":
            pipeline.approve_script(runtime.engine, runtime.settings, content.id)
        elif boundary == "render":
            pipeline.render_content(runtime.engine, runtime.settings, content.id)
        else:
            pipeline.verify_publish_ready(session, runtime.settings, content.id)


@pytest.mark.parametrize("verdict,confidence", [
    ("HUMAN", "high"), ("BLOCKED", "high"), ("READY", "high"),
    ("UNKNOWN", "high"), ("", "high"), ("APPROVE", ""),
    ("APPROVE", "medium"), ("APPROVE", "low"), ("REVISE", "low"),
])
def test_uncertain_review_remains_identifiably_blocked(script_review_contract, verdict, confidence):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review(verdict, confidence=confidence)
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    session.refresh(content)
    assert content.current_stage == "script_review" and content.script_version == 0
    assert content.error_code == "SCRIPT_ASSIST_REVIEW_BLOCKED"


def test_exhausted_existing_revision_budget_does_not_loop(script_review_contract):
    runtime, session, content, _, script, _, review, _ = script_review_contract
    script.input_data = {**script.input_data, "auto_assist_revision_attempt": 1}
    script.input_hash = stable_hash(script.input_data)
    owned = session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == script.id, AuditEvent.action == OWNED,
    )).one()
    owned.metadata_json = {"input_hash": script.input_hash}
    session.add(script)
    session.add(owned)
    session.commit()
    task = review("REVISE", issues=[{"severity": "blocker", "message": "Still misleading"}])
    for _ in range(2):
        assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    session.refresh(content)
    assert content.script_version == 0 and content.current_stage == "script_review"
    assert content.error_code == "SCRIPT_ASSIST_REVISION_LIMIT"


@pytest.mark.parametrize("failure", [
    "input_hash", "accepted_result", "owned", "completed", "upstream_consumption",
    "missing_refs", "snapshot", "pending_script", "pending_research", "pending_qc", "pending_review",
])
def test_changed_or_unproven_current_review_cannot_advance(script_review_contract, failure):
    runtime, session, content, research, script, qc, review, _ = script_review_contract
    task = review()
    if failure == "input_hash":
        task.input_hash = "changed"
    elif failure == "accepted_result":
        task.result_data = {**task.result_data, "summary": "Replaced result"}
    elif failure in {"owned", "completed", "upstream_consumption"}:
        action, target = {"owned": (OWNED, task), "completed": ("completed", task),
                          "upstream_consumption": (AGENT_CONSUMED, qc)}[failure]
        event = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == target.id, AuditEvent.action == action,
        )).one()
        session.delete(event)
    elif failure in {"missing_refs", "snapshot"}:
        task.input_data = deepcopy(task.input_data)
        if failure == "missing_refs":
            task.input_data["upstream_task_ids"] = {}
        else:
            task.input_data["script"] = {"full_narration": "Old text"}
        task.input_hash = stable_hash(task.input_data)
        owned = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == OWNED,
        )).one()
        owned.metadata_json = {"input_hash": task.input_hash}
        session.add(owned)
    else:
        kind = {"pending_script": script.task_type, "pending_research": research.task_type,
                "pending_qc": qc.task_type, "pending_review": task.task_type}[failure]
        runtime.workflow_service(session).create_agent_task(content.id, kind, {"new": True}, allow_multiple=True)
    session.add(task)
    session.commit()
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    with pytest.raises(pipeline.NonRetryableMediaError, match="script assistant review|script_qc"):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id)
    session.refresh(content)
    assert content.script_version == 0


def test_late_review_after_replacement_never_rewrites_or_approves(script_review_contract):
    runtime, session, content, research, _, _, review, completed = script_review_contract
    task = review("REVISE", issues=[{"severity": "blocker", "message": "Old issue"}])
    completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}, "revision": 2},
              {"full_narration": "A replaced script"})
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    session.refresh(content)
    assert content.current_stage == "script_review" and content.script_version == 0
    assert session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == task.id, AuditEvent.action == "stale_agent_result_ignored",
    )).first() is not None


@pytest.mark.parametrize("manual_override", ["handsfree_disabled", "auto_review_disabled", "manual_content"])
def test_manual_explicit_approval_preserves_existing_contract(script_review_contract, monkeypatch, manual_override):
    runtime, session, content, _, _, _, _, _ = script_review_contract
    if manual_override == "manual_content":
        marker = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == content.id, AuditEvent.action == "handsfree_candidate_selected",
        )).one()
        session.delete(marker)
    else:
        key = "handsfree_enabled" if manual_override == "handsfree_disabled" else "handsfree_auto_review_enabled"
        session.add(RuntimeSetting(key=key, value="false"))
    session.commit()

    def reached_tts(*args, **kwargs):
        raise RuntimeError("Explicit approval reached TTS")

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", reached_tts)
    with pytest.raises(RuntimeError, match="Explicit approval reached TTS"):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id)


def test_high_confidence_warnings_are_not_automatic_rewrite_requests(script_review_contract):
    runtime, _, _, _, _, _, review, _ = script_review_contract
    task = review(issues=[{"severity": "warning", "message": "Optional phrasing preference"}])
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) == "voice"


def test_real_stage_lock_and_late_followup_synthesize_voice_only_once(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    entered, release = Event(), Event()
    calls = []
    voice = Path(content.content_dir) / "audio" / "voice.wav"
    voice.parent.mkdir()
    voice.write_bytes(b"isolated fixture voice")

    def voice_once(*args, **kwargs):
        calls.append("voice")
        entered.set()
        assert release.wait(5)
        return SimpleNamespace(audio_path=voice, duration_sec=45)

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", voice_once)
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.run_content_stage, content.id, pipeline.approve_script)
        assert entered.wait(5)
        second = pool.submit(runtime.run_content_stage, content.id, pipeline.approve_script)
        with pytest.raises(TimeoutError):
            second.result(timeout=0.2)
        release.set()
        assert first.result(timeout=5).current_stage == "visual_plan"
        with pytest.raises(pipeline.NonRetryableMediaError, match="advanced past voice"):
            second.result(timeout=5)
    assert calls == ["voice"]
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None


@pytest.mark.parametrize("disabled_key", ["handsfree_enabled", "handsfree_auto_review_enabled"])
@pytest.mark.parametrize("replaced_review", [False, True])
@pytest.mark.parametrize("followup", ["voice", "render", "package"])
def test_late_automatic_followup_cannot_become_manual_when_configuration_changes(
    script_review_contract, monkeypatch, disabled_key, replaced_review, followup,
):
    runtime, _, content, _, _, _, review, _ = script_review_contract
    task = review()
    original = pipeline.advance_after_agent

    def disable_after_decision(engine, settings, task_id):
        action = original(engine, settings, task_id)
        assert action == "voice"
        with Session(engine) as changed:
            changed.add(RuntimeSetting(key=disabled_key, value="false"))
            if replaced_review:
                runtime.workflow_service(changed).create_agent_task(
                    content.id, TaskType.SCRIPT_REVIEW_ASSIST, {"replacement": True}, allow_multiple=True,
                )
            changed.commit()
        # Exercise all runtime follow-up routes against their own real pipeline entrypoint.
        return followup

    monkeypatch.setattr(pipeline, "advance_after_agent", disable_after_decision)
    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *args: None)
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice",
                        lambda *args, **kwargs: pytest.fail("Automatic follow-up used the manual bypass"))
    with pytest.raises(pipeline.NonRetryableMediaError, match="automatic.*disabled"):
        runtime.consume_agent(task.id)


def test_equal_creation_time_with_pending_review_is_ambiguous_and_closed(script_review_contract):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    pending = AgentTask(id="agt_00000000000000000000000000000000", content_id=content.id,
                        task_type=TaskType.SCRIPT_REVIEW_ASSIST, status=TaskStatus.PENDING,
                        prompt_name="fixture", instructions="fixture", created_at=task.created_at,
                        input_data={"replacement": True}, input_hash=stable_hash({"replacement": True}))
    session.add(pending)
    session.commit()
    assert pipeline.advance_after_agent(runtime.engine, runtime.settings, task.id) is None
    with pytest.raises(pipeline.NonRetryableMediaError, match="ambiguous"):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id)


def manual_approval_http(runtime, content, monkeypatch, *, during_tts=None):
    voice = Path(content.content_dir) / "audio" / "manual-voice.wav"
    voice.parent.mkdir(exist_ok=True)
    voice.write_bytes(b"isolated successful manual voice")

    def synthesize(*args, **kwargs):
        if during_tts:
            during_tts()
        return SimpleNamespace(audio_path=voice, duration_sec=45)

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", synthesize)
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    http = TestClient(content_app(runtime))
    try:
        return http.post(f"/api/v1/contents/{content.id}/approve-script", headers=ADMIN)
    finally:
        http.close()


def test_explicit_authenticated_manual_approval_can_override_assistant_without_faking_it(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review("REVISE", issues=[{"severity": "blocker", "message": "Operator-reviewed issue"}])
    original = deepcopy(task.result_data)
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    session.expire_all()
    assert session.get(AgentTask, task.id).result_data == original
    event = session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).one()
    assert event.actor_type == "human" and event.actor_id == "admin-api"
    assert "test-admin" not in json.dumps(event.metadata_json)
    assert pipeline._has_manual_script_approval(session, runtime.settings, content.id)
    session.add(RuntimeSetting(key="handsfree_auto_review_enabled", value="false"))
    session.commit()
    assert pipeline._has_manual_script_approval(session, runtime.settings, content.id)


@pytest.mark.parametrize("change", [
    "script_result", "script_input", "new_script", "research_result", "qc_result", "review_result",
    "review_status", "review_attempt", "new_review", "voice_file", "rotated_key", "forged_signature",
    "copied_event", "wrong_producer",
])
def test_manual_approval_receipt_rejects_changed_subject_or_forgery(script_review_contract, monkeypatch, change):
    runtime, session, content, research, script, qc, review, _ = script_review_contract
    task = review("REVISE")
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    session.expire_all()
    event = session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).one()
    target = {"script_result": script, "script_input": script, "research_result": research,
              "qc_result": qc, "review_result": task, "review_status": task, "review_attempt": task}.get(change)
    if change.endswith("_result"):
        target.result_data = {**target.result_data, "changed": True}
    elif change == "script_input":
        target.input_data = {**target.input_data, "changed": True}
        target.input_hash = stable_hash(target.input_data)
    elif change == "review_status":
        target.status = TaskStatus.FAILED
    elif change == "review_attempt":
        target.attempt += 1
    elif change in {"new_script", "new_review"}:
        runtime.workflow_service(session).create_agent_task(
            content.id, TaskType.SHORT_SCRIPT if change == "new_script" else TaskType.SCRIPT_REVIEW_ASSIST,
            {"replacement": True}, allow_multiple=True,
        )
    elif change == "voice_file":
        (Path(content.content_dir) / "audio" / "manual-voice.wav").write_bytes(b"changed voice")
    elif change == "rotated_key":
        runtime.settings = runtime.settings.model_copy(update={"admin_token": SecretStr("rotated-admin")})
    elif change == "copied_event":
        session.delete(event)
        session.flush()
        event = AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content",
                           entity_id=content.id, action="manual_script_approved",
                           metadata_json=deepcopy(event.metadata_json))
    else:
        event.metadata_json = {**event.metadata_json,
                               "signature" if change == "forged_signature" else "producer":
                               "0" * 64 if change == "forged_signature" else "forged"}
    if target is not None:
        session.add(target)
    session.add(event)
    session.commit()
    assert not pipeline._has_manual_script_approval(session, runtime.settings, content.id)


def test_review_arriving_during_tts_cannot_be_rebound_as_human_approval(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()

    def change_review():
        with Session(runtime.engine) as changed:
            row = changed.get(AgentTask, task.id)
            row.status = TaskStatus.FAILED
            changed.add(row)
            changed.commit()

    response = manual_approval_http(runtime, content, monkeypatch, during_tts=change_review)
    assert response.status_code == 409, response.text
    session.expire_all()
    assert session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).first() is None
    assert session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
    )).first() is None


def test_pending_review_completing_during_manual_tts_leaves_no_new_downstream(script_review_contract, monkeypatch):
    runtime, session, content, research, script, qc, _, _ = script_review_contract
    flow = runtime.workflow_service(session)
    pending = flow.create_agent_task(content.id, TaskType.SCRIPT_REVIEW_ASSIST, {
        "upstream_task_ids": {"script": script.id, "script_qc": qc.id, "research": research.id},
        "script": deepcopy(script.result_data), "script_qc": deepcopy(qc.result_data),
        "research": deepcopy(research.result_data),
    })
    old_plan = flow.create_agent_task(content.id, TaskType.VISUAL_PLAN, {"old_plan": True})

    def complete_review():
        with Session(runtime.engine) as changed:
            record_accepted_completion(changed, runtime, changed.get(AgentTask, pending.id), {
                "verdict": "REVISE", "confidence": "high", "summary": "Late independent review",
                "reasons": ["Opening is misleading"], "issues": [{"severity": "blocker", "message": "Opening"}],
                "checklist": [],
            })
            changed.commit()

    response = manual_approval_http(runtime, content, monkeypatch, during_tts=complete_review)
    assert response.status_code == 409, response.text
    session.expire_all()
    plans = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
    )).all()
    assert len(plans) == 1 and plans[0].id == old_plan.id and plans[0].status == TaskStatus.PENDING
    assert session.get(AgentTask, pending.id).result_data["verdict"] == "REVISE"
    assert session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).first() is None


def test_manual_receipt_missing_review_becomes_invalid_when_review_appears(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, _, _ = script_review_contract
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    session.expire_all()
    assert pipeline._has_manual_script_approval(session, runtime.settings, content.id)
    runtime.workflow_service(session).create_agent_task(content.id, TaskType.SCRIPT_REVIEW_ASSIST, {"late": True})
    assert not pipeline._has_manual_script_approval(session, runtime.settings, content.id)


@pytest.mark.parametrize("change", ["missing", "new_voice", "same_voice", "tampered_input", "ambiguous"])
def test_manual_receipt_requires_current_plan_to_use_approved_voice(script_review_contract, monkeypatch, change):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review("REVISE")
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    session.expire_all()
    plan = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
    )).one()
    if change == "missing":
        plan.status = TaskStatus.CANCELLED
    elif change == "tampered_input":
        plan.input_data = {**plan.input_data, "voice_artifact": {"id": "another-voice"}}
    else:
        binding = plan.input_data["voice_artifact"]
        if change == "new_voice":
            path = Path(content.content_dir) / "audio" / "replacement.wav"
            path.write_bytes(b"new successful voice with old voice retained")
            voice = Artifact(content_id=content.id, artifact_type="voice", relative_path="audio/replacement.wav",
                             sha256=sha256_file(path), size_bytes=path.stat().st_size, mime_type="audio/wav",
                             producer_stage="voice")
            session.add(voice)
            session.commit()
            binding = {key: getattr(voice, key) for key in ("id", "relative_path", "sha256", "size_bytes")}
        inputs = {**plan.input_data, "voice_artifact": binding, "visual_rework": True}
        replacement = runtime.workflow_service(session).create_agent_task(
            content.id, TaskType.VISUAL_PLAN, inputs, allow_multiple=True,
        )
        if change == "ambiguous":
            replacement.created_at = plan.created_at
        else:
            replacement.created_at = plan.created_at + timedelta(seconds=1)
        session.add(replacement)
    session.add(plan)
    session.commit()
    assert pipeline._has_manual_script_approval(session, runtime.settings, content.id) is (change == "same_voice")


def test_manual_receipt_cannot_be_replayed_on_another_content(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review("REVISE")
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    session.expire_all()
    candidate = Candidate(source_type="agent", source_id="replay-target", source_url="https://example.org/replay",
                          original_title="Replay target", status=CandidateStatus.SELECTED,
                          rights_status=RightsStatus.RESEARCH_ONLY)
    session.add(candidate)
    session.commit()
    other = Content(candidate_id=candidate.id, content_dir=content.content_dir,
                    production_mode=content.production_mode, title="Replay target")
    session.add(other)
    session.commit()
    # Replay the whole current graph and original event, preserving its ID and signature.
    for task in session.exec(select(AgentTask).where(AgentTask.content_id == content.id)).all():
        task.content_id = other.id
        session.add(task)
    for voice in session.exec(select(Artifact).where(Artifact.content_id == content.id)).all():
        voice.content_id = other.id
        session.add(voice)
    event = session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).one()
    event.entity_id = other.id
    session.add(event)
    session.commit()
    assert not pipeline._has_manual_script_approval(session, runtime.settings, other.id)


def test_unauthenticated_manual_http_cannot_mint_receipt(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, _, _ = script_review_contract
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: pytest.fail("TTS ran"))
    http = TestClient(content_app(runtime))
    try:
        response = http.post(f"/api/v1/contents/{content.id}/approve-script")
    finally:
        http.close()
    assert response.status_code in {401, 403}
    assert session.exec(select(AuditEvent).where(AuditEvent.action == "manual_script_approved")).first() is None


def test_automatic_source_cannot_claim_manual_approval(script_review_contract, monkeypatch):
    runtime, session, content, _, _, _, _, _ = script_review_contract
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: pytest.fail("TTS ran"))
    with pytest.raises(pipeline.NonRetryableMediaError, match="cannot mint"):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id, automatic=True, manual_approval=True)
    assert session.exec(select(AuditEvent).where(AuditEvent.action == "manual_script_approved")).first() is None


@pytest.mark.parametrize("automatic,change", [(True, "new_script"), (True, "disabled"),
                                               (False, "new_script"), (False, "script_input")])
def test_tts_rechecks_frozen_script_and_auto_switch_before_creating_plan(script_review_contract, monkeypatch, automatic, change):
    runtime, session, content, _, script, _, review, _ = script_review_contract
    review()
    if not automatic:
        session.add(RuntimeSetting(key="handsfree_auto_review_enabled", value="false"))
        session.commit()
    voice = Path(content.content_dir) / "automatic.wav"
    voice.write_bytes(b"automatic voice from frozen original script")

    def synthesize(*args, **kwargs):
        with Session(runtime.engine) as changed:
            if change == "new_script":
                runtime.workflow_service(changed).create_agent_task(content.id, TaskType.SHORT_SCRIPT, {"replacement": True},
                                                                  allow_multiple=True)
            elif change == "disabled":
                changed.add(RuntimeSetting(key="handsfree_auto_review_enabled", value="false"))
                changed.commit()
            else:
                row = changed.get(AgentTask, script.id)
                row.input_data = {**row.input_data, "changed_during_tts": True}
                row.input_hash = stable_hash(row.input_data)
                changed.add(row)
                changed.commit()
        return SimpleNamespace(audio_path=voice, duration_sec=45)

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", synthesize)
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    with pytest.raises(pipeline.NonRetryableMediaError):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id, automatic=automatic)
    session.expire_all()
    assert session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
    )).first() is None


@pytest.mark.parametrize("boundary", ["render", "package"])
@pytest.mark.parametrize("automatic,disabled,valid", [(True, False, True), (False, False, True),
                                                       (True, True, True), (False, True, True),
                                                       (False, True, False)])
def test_manual_override_authorizes_media_boundaries_without_bypassing_auto_switch(
    script_review_contract, monkeypatch, boundary, automatic, disabled, valid,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review("REVISE", issues=[{"severity": "blocker", "message": "Operator approved this issue"}])
    response = manual_approval_http(runtime, content, monkeypatch)
    assert response.status_code == 200, response.text
    if disabled:
        session.add(RuntimeSetting(key="handsfree_auto_review_enabled", value="false"))
    if not valid:
        (Path(content.content_dir) / "audio" / "manual-voice.wav").write_bytes(b"unapproved change")
    session.commit()
    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *args: None)
    original_qc = pipeline._require_current_qc
    original_verify = pipeline.verify_publish_ready

    def next_qc(session, content_id, kind, refs):
        if kind == TaskType.VISUAL_PLAN_QC:
            raise RuntimeError("Reached independent visual QC")
        return original_qc(session, content_id, kind, refs)

    monkeypatch.setattr(pipeline, "_require_current_qc", next_qc)
    monkeypatch.setattr(pipeline, "verify_publish_ready",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("Reached independent release QC")))
    operation = pipeline.render_content if boundary == "render" else pipeline.approve_final
    if automatic and disabled:
        with pytest.raises(pipeline.NonRetryableMediaError, match="disabled"):
            operation(runtime.engine, runtime.settings, content.id, automatic=automatic)
    elif not valid:
        if boundary == "render":
            with pytest.raises(pipeline.NonRetryableMediaError, match="assistant review"):
                operation(runtime.engine, runtime.settings, content.id, automatic=automatic)
        else:
            monkeypatch.setattr(pipeline, "verify_publish_ready", original_verify)
            with pytest.raises(pipeline.NonRetryableMediaError, match="assistant review"):
                operation(runtime.engine, runtime.settings, content.id, automatic=automatic)
    else:
        with pytest.raises(RuntimeError, match="Reached independent"):
            operation(runtime.engine, runtime.settings, content.id, automatic=automatic)


@pytest.mark.parametrize("automatic", [False, True])
def test_default_or_automatic_pipeline_cannot_mint_human_approval(script_review_contract, monkeypatch, automatic):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review()
    voice = Path(content.content_dir) / "voice.wav"
    voice.write_bytes(b"successful nonhuman voice")
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice",
                        lambda *args, **kwargs: SimpleNamespace(audio_path=voice, duration_sec=45))
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    pipeline.approve_script(runtime.engine, runtime.settings, content.id, automatic=automatic)
    session.expire_all()
    assert session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).first() is None


@pytest.mark.parametrize("unsafe_key", ["", "change-me-before-network-use"])
def test_manual_receipt_requires_safe_signing_secret_before_tts(script_review_contract, monkeypatch, unsafe_key):
    runtime, _, content, _, _, _, _, _ = script_review_contract
    unsafe = runtime.settings.model_copy(update={"admin_token": SecretStr(unsafe_key)})
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice",
                        lambda *args, **kwargs: pytest.fail("Unsafe signing key reached paid TTS"))
    with pytest.raises(pipeline.NonRetryableMediaError):
        pipeline.approve_script(runtime.engine, unsafe, content.id, manual_approval=True)
