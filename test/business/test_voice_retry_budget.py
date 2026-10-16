"""A completed review cannot turn an unavailable voice provider into an infinite loop."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from glance_core.models import (
    AgentTask, AuditEvent, Candidate, Content, ContentStatus, ProductionMode, RightsStatus,
    RuntimeSetting, StageRun, StageStatus, TaskStatus, TaskType, utc_now,
)
from pydantic import SecretStr
from sqlmodel import Session, select

from app.business import pipeline
from app.business.content_run import RUN_KEY, get_run, load_run, save_run, start_run
from app.business.runtime import AGENT_CONSUMED, BusinessRuntime
from test.business.test_script_assist_gate import script_review_contract as script_review_contract


def _voice(runtime, content, monkeypatch, *, failures):
    calls = []
    voice = Path(content.content_dir) / "audio" / "retry-voice.wav"
    voice.parent.mkdir(exist_ok=True)
    voice.write_bytes(b"isolated successful voice")

    def synthesize(*args, **kwargs):
        calls.append("voice")
        if len(calls) <= failures:
            raise RuntimeError("voice provider HTTP 403")
        return SimpleNamespace(audio_path=voice, duration_sec=45)

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", synthesize)
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    return calls


def _run(runtime, content_id):
    with Session(runtime.engine) as session:
        return start_run(session, content_id)


def _consume_repeatedly(runtime, content_id, count=10):
    for _ in range(count):
        runtime.recover_agent_results(content_id=content_id)


def test_automatic_voice_failure_stops_after_three_without_consuming_review(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    frozen = deepcopy((task.input_data, task.result_data, task.attempt, task.max_attempts))
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id)
    session.expire_all()
    assert len(calls) == 3
    assert (session.get(Content, content.id).current_stage,
            session.get(Content, content.id).error_code) == ("voice_gate", "VOICE_FAILED")
    unchanged = session.get(AgentTask, task.id)
    assert unchanged.status == TaskStatus.COMPLETED
    assert (unchanged.input_data, unchanged.result_data, unchanged.attempt, unchanged.max_attempts) == frozen
    assert not session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
    )).all()
    assert not session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
    )).all()
    runs = session.exec(select(StageRun).where(StageRun.content_id == content.id, StageRun.stage == "voice")).all()
    assert len(runs) == 3 and len({run.input_hash for run in runs}) == 1 and runs[0].input_hash


def test_new_authorized_run_resumes_same_review_without_manual_approval(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    first = _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=3)
    _consume_repeatedly(runtime, content.id, 5)
    assert len(calls) == 3
    with Session(runtime.engine) as changed:
        get_run(changed, content.id, stop=True)
    second = _run(runtime, content.id)
    assert second["generation"] == first["generation"] + 1
    _consume_repeatedly(runtime, content.id, 5)
    assert len(calls) == 4
    session.expire_all()
    assert session.get(Content, content.id).current_stage == "visual_plan"
    assert len(session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
    )).all()) == 1
    assert not session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
    )).all()


@pytest.mark.parametrize("failures", [0, 1, 2])
def test_transient_voice_failure_recovers_within_original_run(
    script_review_contract, monkeypatch, failures,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    first = _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=failures)
    _consume_repeatedly(runtime, content.id)
    assert len(calls) == failures + 1
    session.expire_all()
    assert session.get(Content, content.id).current_stage == "visual_plan"
    assert load_run(session).run_id == first["run_id"]
    assert len(session.exec(select(AuditEvent).where(
        AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
    )).all()) == 1


def test_typed_exhaustion_preserves_original_failed_stage_and_does_no_further_io(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="provider HTTP 403"):
            runtime.consume_agent(task.id)
    for _ in range(5):
        with pytest.raises(pipeline.VoiceRetryExhausted, match="VOICE_RETRY_EXHAUSTED"):
            runtime.consume_agent(task.id)
    assert len(calls) == 3
    runs = session.exec(select(StageRun).where(StageRun.content_id == content.id)).all()
    assert len(runs) == 3
    assert all(run.status == StageStatus.FAILED and run.error_code == "RuntimeError"
               and run.error_message == "voice provider HTTP 403" for run in runs)


@pytest.mark.parametrize("change", ["stop", "expire", "another_content", "missing_slot"])
def test_terminal_or_replaced_run_and_runtime_restart_cannot_refresh_allowance(
    script_review_contract, monkeypatch, change,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id, 3)
    with Session(runtime.engine) as changed:
        if change == "expire":
            run = load_run(changed)
            run.created_at = (utc_now() - timedelta(hours=3)).isoformat()
            run.expires_at = (utc_now() - timedelta(hours=1)).isoformat()
            save_run(changed, run, "test_run_expiry")
            changed.commit()
            get_run(changed, content.id)
        elif change == "missing_slot":
            changed.delete(changed.get(RuntimeSetting, RUN_KEY))
            changed.commit()
        else:
            get_run(changed, content.id, stop=True)
            if change == "another_content":
                candidate = Candidate(source_type="agent", source_id="other", source_url="https://example.org/other",
                                      original_title="Other")
                changed.add(candidate)
                changed.commit()
                other = Content(candidate_id=candidate.id, title="Other",
                                content_dir=str(Path(content.content_dir).parent / "other"))
                changed.add(other)
                changed.commit()
                start_run(changed, other.id)
    restarted = BusinessRuntime(runtime.settings)
    try:
        _consume_repeatedly(restarted, content.id)
    finally:
        restarted.close()
    assert len(calls) == 3


def test_no_run_scope_is_persistent_and_does_not_misattribute_unbound_legacy_failures(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review()
    old = []
    for attempt in range(1, 11):
        stage = StageRun(content_id=content.id, stage="voice", input_hash="", attempt=attempt,
                         status=StageStatus.FAILED, error_code="Historical403", error_message="old")
        session.add(stage)
        old.append(stage)
    session.commit()
    frozen = [(row.id, row.input_hash, row.attempt, row.error_message) for row in old]
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id)
    restarted = BusinessRuntime(runtime.settings)
    try:
        _consume_repeatedly(restarted, content.id)
    finally:
        restarted.close()
    assert len(calls) == 3
    session.expire_all()
    assert [(session.get(StageRun, key).id, session.get(StageRun, key).input_hash,
             session.get(StageRun, key).attempt, session.get(StageRun, key).error_message)
            for key, *_ in frozen] == frozen


def test_crashed_running_voice_starts_consume_the_same_finite_allowance(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id, 1)
    first = session.exec(select(StageRun).where(StageRun.content_id == content.id)).one()
    for attempt in (2, 3):
        session.add(StageRun(content_id=content.id, stage="voice", input_hash=first.input_hash,
                             attempt=attempt, status=StageStatus.RUNNING, started_at=utc_now()))
    session.commit()
    _consume_repeatedly(runtime, content.id)
    assert len(calls) == 1
    session.expire_all()
    assert session.get(Content, content.id).error_code == "VOICE_FAILED"


def test_secret_rotation_does_not_refresh_same_subject_allowance_or_disclose_token(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id, 3)
    runtime.settings = runtime.settings.model_copy(update={"cosyvoice_token": SecretStr("private-rotated-token")})
    _consume_repeatedly(runtime, content.id)
    assert len(calls) == 3
    runs = session.exec(select(StageRun).where(StageRun.content_id == content.id)).all()
    assert all("private-rotated-token" not in str(row.model_dump()) for row in runs)


def test_changed_accepted_review_or_late_replacement_cannot_advance_voice(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    _consume_repeatedly(runtime, content.id, 1)
    task.result_data = {**task.result_data, "summary": "Changed accepted bytes"}
    session.add(task)
    session.commit()
    with pytest.raises(pipeline.NonRetryableMediaError):
        pipeline.approve_script(runtime.engine, runtime.settings, content.id, automatic=True)
    assert len(calls) == 1


def test_late_subject_change_during_failed_voice_does_not_overwrite_new_stage(
    script_review_contract, monkeypatch,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    _run(runtime, content.id)

    def fail_after_replacement(*args, **kwargs):
        with Session(runtime.engine) as changed:
            changed_task = changed.get(AgentTask, task.id)
            changed_task.status = TaskStatus.CANCELLED
            changed_content = changed.get(Content, content.id)
            changed_content.current_stage = "script_rewrite"
            changed_content.business_status = ContentStatus.WAITING_AGENT
            changed.add(changed_task)
            changed.add(changed_content)
            changed.commit()
        raise RuntimeError("provider failed after replacement")

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", fail_after_replacement)
    with pytest.raises(pipeline.NonRetryableMediaError, match="subject changed"):
        runtime.consume_agent(task.id)
    session.expire_all()
    assert session.get(Content, content.id).current_stage == "script_rewrite"


def test_concurrent_consumers_share_one_persisted_three_start_allowance(
    script_review_contract, monkeypatch,
):
    runtime, _, content, _, _, _, review, _ = script_review_contract
    task = review()
    _run(runtime, content.id)
    entered, release = Event(), Event()
    calls = []

    def failing_voice(*args, **kwargs):
        calls.append("voice")
        entered.set()
        assert release.wait(5)
        raise RuntimeError("provider failed")

    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", failing_voice)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.consume_agent, task.id)
        assert entered.wait(5)
        second = pool.submit(runtime.consume_agent, task.id, lock_timeout_seconds=0)
        with pytest.raises(TimeoutError):
            second.result(timeout=5)
        release.set()
        with pytest.raises(RuntimeError, match="provider failed"):
            first.result(timeout=5)
    _consume_repeatedly(runtime, content.id)
    assert len(calls) == 3


@pytest.mark.parametrize("crash_point", ["dispatch", "tts", "after_voice"])
def test_restart_after_voice_crash_does_not_consume_unfinished_review(
    script_review_contract, monkeypatch, crash_point,
):
    runtime, session, content, _, _, _, review, _ = script_review_contract
    task = review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=100)
    with pytest.raises(RuntimeError, match="provider HTTP 403"):
        runtime.consume_agent(task.id)

    class ProcessCrash(BaseException):
        pass

    def crash(*args, **kwargs):
        raise ProcessCrash("isolated process crash")

    with monkeypatch.context() as crashing:
        if crash_point == "dispatch":
            crashing.setattr(pipeline, "approve_script", crash)
        elif crash_point == "tts":
            def synthesize_then_crash(*args, **kwargs):
                calls.append("crashed voice")
                crash()

            crashing.setattr(pipeline.MediaExecutor, "synthesize_voice", synthesize_then_crash)
        else:
            def successful_voice(*args, **kwargs):
                calls.append("successful voice before crash")
                return SimpleNamespace(audio_path=Path(content.content_dir) / "audio" / "retry-voice.wav",
                                       duration_sec=45)

            crashing.setattr(pipeline.MediaExecutor, "synthesize_voice", successful_voice)
            crashing.setattr(pipeline.WorkflowService, "invalidate_visual_plan", crash)
        with pytest.raises(ProcessCrash):
            runtime.consume_agent(task.id)

    restarted = BusinessRuntime(runtime.settings)
    try:
        _consume_repeatedly(restarted, content.id)
        session.expire_all()
        assert not session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
        )).all()
        assert not session.exec(select(AgentTask).where(
            AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
        )).all()
        assert session.get(Content, content.id).current_stage == "voice_gate"
        assert session.get(Content, content.id).error_code == "VOICE_FAILED"
        assert len(calls) == 3
        assert len(session.exec(select(StageRun).where(StageRun.content_id == content.id)).all()) == 3
        with Session(runtime.engine) as changed:
            get_run(changed, content.id, stop=True)
        _run(restarted, content.id)
        repaired_calls = _voice(restarted, content, monkeypatch, failures=0)
        _consume_repeatedly(restarted, content.id)
        assert len(repaired_calls) == 1
        session.expire_all()
        assert session.get(Content, content.id).current_stage == "visual_plan"
        assert len(session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == task.id, AuditEvent.action == AGENT_CONSUMED,
        )).all()) == 1
        assert not session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == content.id, AuditEvent.action == "manual_script_approved",
        )).all()
    finally:
        restarted.close()


@pytest.mark.parametrize("mode", [ProductionMode.IMAGE_ONLY, ProductionMode.FOOTAGE])
def test_voice_failure_marker_clears_with_successful_stage_transition_only(
    script_review_contract, monkeypatch, mode,
):
    runtime, session, content, _, _, _, review, completed = script_review_contract
    content.production_mode = mode
    candidate = session.get(Candidate, content.candidate_id)
    candidate.rights_status = RightsStatus.EXPLICIT_LICENSE
    session.add(candidate)
    session.add(content)
    session.commit()
    if mode == ProductionMode.FOOTAGE:
        completed(TaskType.SCENE_ANALYZE, {}, {"scenes": []})
        scenes = Path(content.content_dir) / "scenes" / "scenes.json"
        scenes.parent.mkdir()
        scenes.write_text('{"scenes": []}', encoding="utf-8")
    task = review()
    _run(runtime, content.id)
    calls = _voice(runtime, content, monkeypatch, failures=1)
    with pytest.raises(RuntimeError, match="provider HTTP 403"):
        runtime.consume_agent(task.id)
    original = pipeline.WorkflowService.mark_status
    transitions = []

    def verify_committed_failure_before_transition(flow, content_id, status, stage=None):
        if stage == "visual_plan":
            with Session(runtime.engine) as observed:
                persisted = observed.get(Content, content_id)
                assert (persisted.current_stage, persisted.error_code) == ("voice_gate", "VOICE_FAILED")
            transitions.append(stage)
        return original(flow, content_id, status, stage)

    monkeypatch.setattr(pipeline.WorkflowService, "mark_status", verify_committed_failure_before_transition)
    assert runtime.consume_agent(task.id)
    assert transitions == ["visual_plan"] and len(calls) == 2
    session.expire_all()
    current = session.get(Content, content.id)
    assert (current.current_stage, current.error_code, current.error_message) == ("visual_plan", None, None)
