"""Native failed v2 acceptance uses the existing bounded Recovery contract."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from glance_core.content_acceptance import acceptance_subject, source_context
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, RuntimeSetting, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_schemas import CONTENT_ACCEPTANCE_CHECKS, RecoveryPlan
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.workflow import WorkflowService
from glance_media.executor import MediaExecutor, sha256_file
from sqlmodel import Session, select

from app.business.runtime import AGENT_CONSUMED, OWNED, QC_RECOVERY, QC_PLAN_REJECTED, QC_TARGET_REJECTION, _require_current_revision
from test.business.test_agent_failure_recovery import registered_client, record_intent, recovery_rows
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN, make_runtime
from test.business import test_visual_plan_materialization as materialization

image_contract = materialization.image_contract

REPAIR = {
    "failure_class": "BUSINESS_QC", "action": "create_repair_task", "target_task_type": "short_script",
    "reason": "Final audio cannot independently resolve the spoken product name",
    "must_change": ["Use a clear Chinese product name in the narration"],
    "must_preserve": ["Verified product facts", "Original failed QC evidence"],
    "must_not_do": ["Modify original QC or voice", "Publish before new acceptance"],
    "acceptance_checks": ["New script QC and independent final audio QC pass"],
}


@pytest.fixture
def failed_qc(image_contract, tmp_path, request):
    runtime, content_id, plan_id, _, asset_qc_id, _, _ = image_contract
    runtime.consume_agent(asset_qc_id)
    with TestClient(app_for(runtime, tmp_path)) as http:
        worker = registered_client(http, runtime, "script")
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            origin = getattr(request, "param", "manual")
            if origin == "handsfree":
                for audit in session.exec(select(AuditEvent).where(
                    AuditEvent.entity_id == content_id, AuditEvent.action == "manual_candidate_selected",
                )).all():
                    session.delete(audit)
                session.add(AuditEvent(actor_type="system", actor_id="handsfree-coordinator", entity_type="content",
                                       entity_id=content_id, action="handsfree_candidate_selected",
                                       metadata_json={"candidate_id": content.candidate_id}))
            root = Path(content.content_dir)
            voice = root / "audio" / "narration.mp3"
            voice.parent.mkdir()
            voice.write_bytes(b"immutable-voice")
            session.add(Artifact(content_id=content_id, artifact_type="voice", relative_path="audio/narration.mp3",
                                 sha256=sha256_file(voice), size_bytes=voice.stat().st_size, mime_type="audio/mpeg", producer_stage="voice"))
            video = root / "final.mp4"
            video.write_bytes(b"immutable-final")
            artifact = Artifact(content_id=content_id, artifact_type="final_video", relative_path=video.name,
                                sha256=sha256_file(video), size_bytes=video.stat().st_size, mime_type="video/mp4", producer_stage="render")
            session.add(artifact)
            content.business_status, content.current_stage = ContentStatus.WAITING_AGENT, "content_qc"
            session.add(content)
            session.commit()
            plan = session.get(AgentTask, plan_id)
            refs = {"script": plan.input_data["upstream_task_ids"]["script"], "visual_plan": plan_id}
            full_refs = {**refs, "video_artifact_id": artifact.id}
            audio = {"version": 1, "video_sha256": artifact.sha256, "method": "local_asr_and_pcm",
                     "asr_status": "available", "segments": [{"text": "unclear name", "start_sec": 0, "end_sec": 3}],
                     "signal_status": "available", "signal": {"decoded_pcm_sha256": "fixture"}}
            bare = {"upstream_task_ids": refs, "video_artifact_id": artifact.id,
                    "acceptance_contract_version": 2, "acceptance_origin": origin, "source_context_version": 1,
                    "source_context": source_context(session, content_id, full_refs),
                    "acceptance_subject": acceptance_subject(session, content_id, full_refs, source_context_version=1)}
            receipt = root / "render" / "acceptance-input.json"
            receipt.parent.mkdir()
            receipt.write_text(json.dumps(bare), encoding="utf-8")
            session.add(Artifact(content_id=content_id, artifact_type="acceptance_input",
                                 relative_path="render/acceptance-input.json", sha256=sha256_file(receipt), size_bytes=receipt.stat().st_size, mime_type="application/json", producer_stage="render"))
            (receipt.parent / f"audio-evidence-{artifact.sha256}.json").write_text(json.dumps(audio), encoding="utf-8")
            frozen = {**bare, "audio_evidence_version": 1, "audio_evidence": audio,
                      "acceptance_subject": acceptance_subject(session, content_id, full_refs, source_context_version=1, audio_evidence=audio)}
            qc = runtime.workflow_service(session).create_agent_task(content_id, TaskType.CONTENT_QC, frozen,
                                                                      allow_multiple=True, schema_version="2")
            result = {"contract_version": 2, "confidence": "high", "passed": False, "subject": frozen["acceptance_subject"],
                      "checks": {key: {"passed": key != "audio", "evidence": "fixture independently observed"}
                                 for key in CONTENT_ACCEPTANCE_CHECKS},
                      "issues": [{"severity": "error", "field": "audio", "message": "Spoken name not independently resolved"}]}
            accept(session, runtime, qc, result, worker.settings.agent_id)
            yield runtime, http, content_id, qc.id, plan_id, root


def accept(session, runtime, task, result, agent_id, *, consumed=False):
    task.status, task.attempt, task.assigned_agent_id = TaskStatus.COMPLETED, 1, agent_id
    task.result_data, task.completed_at = result, utc_now()
    raw = runtime.settings.agent_submission_dir / task.id / "1" / "result.json"
    raw.parent.mkdir(parents=True)
    raw.write_text(json.dumps(result), encoding="utf-8")
    session.add(task)
    session.add(AgentSubmission(task_id=task.id, attempt=1, agent_id=agent_id,
                                raw_result_path=str(raw), result_hash=stable_hash(result), validation_status="ACCEPTED"))
    session.add(AuditEvent(actor_type="agent", actor_id=agent_id, entity_type="agent_task", entity_id=task.id,
                           action="completed", metadata_json={"result_hash": stable_hash(result)}))
    if not session.exec(select(AuditEvent.id).where(AuditEvent.entity_id == task.id, AuditEvent.action == OWNED)).first():
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=task.id,
                               action=OWNED, metadata_json={"input_hash": task.input_hash}))
    if consumed:
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=task.id,
                               action=AGENT_CONSUMED, metadata_json={"result_hash": stable_hash(result)}))
    session.commit()


def consumed(session, qc_id):
    qc = session.get(AgentTask, qc_id)
    session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=qc_id,
                           action=AGENT_CONSUMED, metadata_json={"result_hash": stable_hash(qc.result_data)}))
    session.commit()


def test_future_failed_qc_consumes_atomically_and_does_not_change_old_evidence(failed_qc):
    runtime, _, content_id, qc_id, _, root = failed_qc
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with Session(runtime.engine) as session:
        old = session.get(AgentTask, qc_id).model_dump(mode="json")
    assert runtime.consume_agent(qc_id)
    assert runtime.consume_agent(qc_id) is False
    recovery = recovery_rows(runtime)[0]
    assert recovery.input_data["failure"]["failure_class"] == "BUSINESS_QC"
    assert recovery.input_data["lineage"]["target_task_type"] == "short_script"
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, qc_id).model_dump(mode="json") == old
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc_id, AuditEvent.action == AGENT_CONSUMED)).one()
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == QC_RECOVERY)).one()
        assert session.get(Content, content_id).current_stage == "recovery"
    assert all(p.read_bytes() == data for p, data in before.items())


def test_admin_old_consumed_failure_is_authenticated_and_idempotent(failed_qc):
    runtime, http, _, qc_id, _, _ = failed_qc
    url = f"/api/v1/agent/tasks/{qc_id}/content-qc-recovery"
    assert http.post(url).status_code == 401
    assert http.post(url, headers=ADMIN).status_code == 409
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
    first = http.post(url, headers=ADMIN)
    assert first.status_code == 200, first.text
    assert first.json()["recovery_status"] == "PENDING"
    assert http.post(url, headers=ADMIN).json() == first.json()
    assert len(recovery_rows(runtime)) == 1
    assert http.post(f"/api/v1/agent/tasks/{qc_id}/retry-recovery", headers=ADMIN).status_code == 409


@pytest.mark.parametrize("storage, allowed", [
    ("identity", True), ("legacy", True), ("mismatched_legacy", False),
    ("corrupt_identity_valid_legacy", False), ("identity_valid_mismatched_legacy", True),
])
def test_qc_recovery_checks_exact_frozen_audio_at_identity_or_legacy_path(failed_qc, storage, allowed):
    runtime, http, _, qc_id, _, root = failed_qc
    runtime.settings.asr_model = "different-current-model"
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        audio = session.get(AgentTask, qc_id).input_data["audio_evidence"]
    legacy = root / "render" / f"audio-evidence-{audio['video_sha256']}.json"
    identity = MediaExecutor(root).final_audio_evidence_path(audio)
    if "identity" in storage:
        identity.write_text(json.dumps(audio), encoding="utf-8")
    if storage == "identity":
        legacy.unlink()
    elif "mismatched_legacy" in storage:
        legacy.write_text(json.dumps({**audio, "asr_model": "another-model"}), encoding="utf-8")
    elif storage == "corrupt_identity_valid_legacy":
        identity.write_text(json.dumps({**audio, "segments": []}), encoding="utf-8")
    before = {path: path.read_bytes() for path in (identity, legacy) if path.is_file()}

    response = http.post(f"/api/v1/agent/tasks/{qc_id}/content-qc-recovery", headers=ADMIN)

    assert response.status_code == (200 if allowed else 409), response.text
    assert len(recovery_rows(runtime)) == int(allowed)
    assert all(path.read_bytes() == data for path, data in before.items())


@pytest.mark.parametrize("explicit_null", [False, True])
def test_actual_complete_uses_frozen_script_target_when_optional_target_is_empty(failed_qc, explicit_null):
    runtime, http, _, qc_id, _, root = failed_qc
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assert claim["task_id"] == recovery.id
    assistant.confirm_claim(recovery.id, claim["lease_token"], assistant.settings.agent_id)
    result = dict(REPAIR)
    if explicit_null:
        result["target_task_type"] = None
    else:
        result.pop("target_task_type")
    response = assistant.complete(recovery.id, claim["lease_token"], assistant.settings.agent_id, result)
    assert response["advanced"] == "CONSUMED", response
    with Session(runtime.engine) as session:
        children = session.exec(select(AgentTask).where(
            AgentTask.input_data["recovery_task_id"].as_string() == recovery.id,
        )).all()
        assert len(children) == 1 and children[0].task_type == TaskType.SHORT_SCRIPT
        assert children[0].input_data["previous_qc_task_id"] == qc_id
        owner = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == children[0].id, AuditEvent.action == OWNED,
        )).one()
        assert owner.metadata_json["input_hash"] == stable_hash(children[0].input_data)
        assert session.get(AgentTask, qc_id).result_data["passed"] is False
    assert all(path.read_bytes() == value for path, value in before.items())


def damage_source(session, runtime, content_id, qc_id, plan_id, root, damage):
    qc = session.get(AgentTask, qc_id)
    plan = session.get(AgentTask, plan_id)
    script = session.get(AgentTask, plan.input_data["upstream_task_ids"]["script"])
    if damage in {"null_result", "list_result", "null_input"}:
        if damage == "null_input":
            qc.input_data = None
        else:
            qc.result_data = None if damage == "null_result" else []
        session.add(qc)
    elif damage in {"passed", "result"}:
        qc.result_data = {**qc.result_data, **({"passed": True} if damage == "passed" else {"changed": True})}
        session.add(qc)
    elif damage == "input":
        qc.input_data = {**qc.input_data, "changed": True}
        session.add(qc)
    elif damage in {"unowned", "completed", "consumed", "parent_unowned", "parent_unconsumed"}:
        target = script if damage.startswith("parent") else qc
        action = {"unowned": OWNED, "completed": "completed", "consumed": AGENT_CONSUMED,
                  "parent_unowned": OWNED, "parent_unconsumed": AGENT_CONSUMED}[damage]
        session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == target.id, AuditEvent.action == action)).one())
    elif damage == "accepted":
        session.delete(session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc_id)).one())
    elif damage == "raw":
        raw = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc_id)).one()
        Path(raw.raw_result_path).write_text("{}", encoding="utf-8")
    elif damage == "deployment":
        session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.assigned_agent_id,
                                                            AuditEvent.action == "mpt_deployment_verified")).one())
    elif damage in {"new_script", "new_qc", "leased", "submitted"}:
        target = script if damage == "new_script" else qc
        newer = runtime.workflow_service(session).create_agent_task(content_id, target.task_type,
                                                                    {**target.input_data, "revision": 2}, allow_multiple=True)
        newer.created_at = target.created_at + timedelta(microseconds=1)
        session.add(newer)
        if damage in {"leased", "submitted"}:
            newer.status = TaskStatus.LEASED if damage == "leased" else TaskStatus.SUBMITTED
            session.add(newer)
    elif damage == "offer":
        session.add(SchedulerOffer(request_id="active", agent_id="offered", role_id="script", task_id=qc_id,
                                    expires_at=utc_now() + timedelta(seconds=60)))
    elif damage in {"unknown", "malformed"}:
        record_intent(session, content_id, malformed=damage == "malformed")
    elif damage == "published":
        content = session.get(Content, content_id)
        content.business_status = ContentStatus.PUBLISHED
        session.add(content)
    else:
        path = {"video": root / "final.mp4", "voice": root / "audio" / "narration.mp3",
                "receipt": root / "render" / "acceptance-input.json"}.get(damage)
        if damage == "audio":
            path = next((root / "render").glob("audio-evidence-*.json"))
        path.write_bytes(b"changed")
    session.commit()


@pytest.mark.parametrize("damage", ["passed", "result", "input", "unowned", "completed", "consumed", "accepted", "raw",
                                     "null_result", "list_result", "null_input", "parent_unowned", "parent_unconsumed", "deployment", "new_script", "new_qc", "leased",
                                     "submitted", "offer", "unknown", "malformed", "published", "video", "voice", "receipt", "audio"])
def test_admin_rejects_noncurrent_changed_or_unproven_acceptance(failed_qc, damage):
    runtime, http, content_id, qc_id, plan_id, root = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        damage_source(session, runtime, content_id, qc_id, plan_id, root, damage)
    response = http.post(f"/api/v1/agent/tasks/{qc_id}/content-qc-recovery", headers=ADMIN)
    assert response.status_code == 409, response.text
    assert recovery_rows(runtime) == []


@pytest.mark.parametrize("sibling_id", ["agt_00000000000000000000000000000000", "agt_ffffffffffffffffffffffffffffffff"])
@pytest.mark.parametrize("sibling_status", [TaskStatus.PENDING, TaskStatus.COMPLETED])
def test_equal_timestamp_qc_revisions_reject_both_without_recovery_side_effects(failed_qc, sibling_id, sibling_status):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        qc = session.get(AgentTask, qc_id)
        sibling = AgentTask(**{**qc.model_dump(), "id": sibling_id, "status": sibling_status, "dedup_key": None})
        session.add(sibling)
        session.commit()
        if sibling_status == TaskStatus.COMPLETED:
            accept(session, runtime, sibling, qc.result_data, qc.assigned_agent_id, consumed=True)
        else:
            sibling.result_data, sibling.attempt, sibling.assigned_agent_id, sibling.completed_at = None, 0, None, None
            session.commit()
        assert sibling.created_at == qc.created_at
        assert (sibling.id < qc.id) == sibling_id.startswith("agt_0")
        # Recovery rounds and budgets are persisted in tasks and audit events.
        before = {model: [row.model_dump(mode="json") for row in session.exec(select(model)).all()]
                  for model in (AgentTask, AuditEvent, Content, RuntimeSetting)}
    for task_id in (qc_id, sibling_id):
        response = http.post(f"/api/v1/agent/tasks/{task_id}/content-qc-recovery", headers=ADMIN)
        assert response.status_code == 409, response.text
    with Session(runtime.engine) as session:
        for task_id in (qc_id, sibling_id):
            with pytest.raises(TaskConflictError, match="RECOVERY_SOURCE_SUPERSEDED"):
                _require_current_revision(session, session.get(AgentTask, task_id))
        assert {model: [row.model_dump(mode="json") for row in session.exec(select(model)).all()]
                for model in before} == before
        assert session.get(Content, content_id).current_stage == "content_qc"
    assert recovery_rows(runtime) == []


@pytest.mark.parametrize("case, rejected", [
    ("older", False), ("newer", True), ("equal", True), ("cancelled", False),
    ("different_content", False), ("different_type", False), ("different_scene_batch", False),
    ("same_scene_batch", True), ("default_scene_batch", True),
])
def test_current_revision_guard_preserves_revision_boundaries(tmp_path, case, rejected):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    with Session(runtime.engine) as session:
        kind = TaskType.SCENE_ANALYZE if "scene_batch" in case else TaskType.CONTENT_QC
        task = AgentTask(id="agt_f", content_id="content", task_type=kind, status=TaskStatus.FAILED,
                         prompt_name="fixture", instructions="fixture", input_hash="fixture",
                         input_data={"batch_index": 1} if kind == TaskType.SCENE_ANALYZE else {},
                         created_at=datetime(2026, 10, 10, tzinfo=timezone.utc))
        sibling = AgentTask(**{**task.model_dump(), "id": "agt_0", "status": TaskStatus.PENDING})
        if case in {"older", "newer"}:
            sibling.created_at += timedelta(microseconds=-1 if case == "older" else 1)
        elif case == "cancelled":
            sibling.status = TaskStatus.CANCELLED
        elif case == "different_content":
            sibling.content_id = "other-content"
        elif case == "different_type":
            sibling.task_type = TaskType.SHORT_SCRIPT
        elif case == "different_scene_batch":
            sibling.input_data = {"batch_index": 2}
        elif case == "default_scene_batch":
            sibling.input_data = {}
        session.add_all([task, sibling])
        session.commit()
        if rejected:
            with pytest.raises(TaskConflictError, match="RECOVERY_SOURCE_SUPERSEDED"):
                _require_current_revision(session, task)
        else:
            _require_current_revision(session, task)


def test_qc_recovery_creates_one_owned_new_script_with_original_false_immutable(failed_qc):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id), REPAIR, assistant.settings.agent_id)
    assert runtime.consume_agent(recovery.id)
    assert runtime.consume_agent(recovery.id) is False
    with Session(runtime.engine) as session:
        children = session.exec(select(AgentTask).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery.id)).all()
        assert len(children) == 1 and children[0].task_type == TaskType.SHORT_SCRIPT
        assert children[0].status == TaskStatus.PENDING
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == children[0].id, AuditEvent.action == OWNED)).one().metadata_json["input_hash"] == children[0].input_hash
        assert session.get(AgentTask, qc_id).status == TaskStatus.COMPLETED
        assert session.get(AgentTask, qc_id).result_data["passed"] is False
        assert session.get(Content, content_id).current_stage == "recovery_repair"


@pytest.mark.parametrize("damage", ["input", "result", "new_script", "new_qc", "parent_unconsumed", "unknown", "malformed",
                                     "video", "voice", "audio", "receipt", "deployment", "leased", "offer", "qc_receipt"])
def test_queued_recovery_rechecks_frozen_source_before_creating_script(failed_qc, damage):
    runtime, http, content_id, qc_id, plan_id, root = failed_qc
    runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id), REPAIR, assistant.settings.agent_id)
        if damage == "qc_receipt":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == QC_RECOVERY)).one())
            session.commit()
        else:
            damage_source(session, runtime, content_id, qc_id, plan_id, root, damage)
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.exec(select(AgentTask.id).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery.id)).first() is None
        assert session.exec(select(AuditEvent.id).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == AGENT_CONSUMED)).first() is None


@pytest.mark.parametrize("action", ["create_repair_task", "reroute_to_role", "repair_task_context"])
@pytest.mark.parametrize("target", ["visual_plan", "content_qc"])
def test_native_invalid_qc_target_is_consumed_as_server_rejection_without_new_work(failed_qc, action, target):
    runtime, http, content_id, qc_id, _, root = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    media = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with Session(runtime.engine) as session:
        old = {row.id: row.model_dump(mode="json") for row in session.exec(select(AgentTask)).all() if row.id != recovery.id}
        settings = [row.model_dump(mode="json") for row in session.exec(select(RuntimeSetting)).all()]
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assert claim["task_id"] == recovery.id
    assistant.confirm_claim(recovery.id, claim["lease_token"], assistant.settings.agent_id)
    result = {**REPAIR, "action": action, "target_task_type": target}
    result = RecoveryPlan.model_validate(result).model_dump(mode="json")
    assert assistant.complete(recovery.id, claim["lease_token"], assistant.settings.agent_id, result)["advanced"] == "CONSUMED"
    assert runtime.consume_agent(recovery.id) is False
    with Session(runtime.engine) as session:
        rejected = session.get(AgentTask, recovery.id)
        assert rejected.status == TaskStatus.COMPLETED
        assert rejected.input_data == recovery.input_data and rejected.result_data == result
        assert {row.id: row.model_dump(mode="json") for row in session.exec(select(AgentTask)).all() if row.id != recovery.id} == old
        assert [row.model_dump(mode="json") for row in session.exec(select(RuntimeSetting)).all()
                if row.key != "mpt_maintenance_last_run"] == settings
        assert session.get(AgentTask, qc_id).result_data["passed"] is False
        content = session.get(Content, content_id)
        assert (content.current_stage, content.error_code, content.error_message) == ("recovery_blocked", "RECOVERY_BLOCKED", QC_TARGET_REJECTION)
        receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == QC_PLAN_REJECTED)).one()
        assert receipt.metadata_json["action"] == action and receipt.metadata_json["target_task_type"] == target
        assert receipt.metadata_json["recovery_result_hash"] == stable_hash(result)
        assert receipt.metadata_json["source_task_id"] == qc_id
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == AGENT_CONSUMED)).one()
        assert not session.exec(select(AuditEvent).where(AuditEvent.action.in_(["recovery_repair_task_created", "recovery_same_task_retried"]))).first()
    runtime.recover_agent_results()
    assert all(path.read_bytes() == data for path, data in media.items())


@pytest.mark.parametrize("damage", ["result", "input", "new_script", "new_qc", "parent_unconsumed", "unknown", "malformed",
                                     "video", "voice", "audio", "receipt", "deployment", "leased", "offer", "qc_receipt"])
def test_invalid_qc_target_with_unproven_source_remains_conflict(failed_qc, damage):
    runtime, http, content_id, qc_id, plan_id, root = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id),
               {**REPAIR, "action": "repair_task_context", "target_task_type": "content_qc"}, assistant.settings.agent_id)
        if damage == "qc_receipt":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == QC_RECOVERY)).one())
            session.commit()
        else:
            damage_source(session, runtime, content_id, qc_id, plan_id, root, damage)
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery"
        assert not session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
                                                        AuditEvent.action.in_([QC_PLAN_REJECTED, AGENT_CONSUMED, "recovery_blocked"]))).first()


@pytest.mark.parametrize("failure_class", ["TASK_CONTRACT", "CONTEXT_MISSING", "TRANSIENT_EXECUTOR", "RIGHTS_OR_PUBLISHING", "UNKNOWN"])
def test_wrong_failure_class_is_not_a_trusted_qc_target_rejection(failed_qc, failure_class):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id),
               {**REPAIR, "failure_class": failure_class, "target_task_type": "content_qc"}, assistant.settings.agent_id)
    with pytest.raises(TaskConflictError, match="RECOVERY_QC_REJECTION_RECEIPT_CHANGED"):
        runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery"
        assert not session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
                                                        AuditEvent.action.in_([QC_PLAN_REJECTED, AGENT_CONSUMED, "recovery_blocked"]))).first()


@pytest.mark.parametrize("failure_point", ["blocked", "commit"])
def test_qc_target_rejection_receipt_block_and_consumption_rollback_together(failed_qc, monkeypatch, failure_point):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id),
               {**REPAIR, "target_task_type": "content_qc"}, assistant.settings.agent_id)
    original = WorkflowService._mark_recovery_blocked

    def fail_block(flow, *args, **kwargs):
        original(flow, *args, **kwargs)
        raise OSError("injected rejection failure")

    def fail_commit(session):
        raise OSError("injected rejection failure")

    with monkeypatch.context() as patch:
        if failure_point == "blocked":
            patch.setattr(WorkflowService, "_mark_recovery_blocked", fail_block)
        else:
            patch.setattr(Session, "commit", fail_commit)
        with pytest.raises(TaskConflictError, match="injected rejection failure"):
            runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "recovery"
        assert not session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
                                                        AuditEvent.action.in_([QC_PLAN_REJECTED, AGENT_CONSUMED, "recovery_blocked"]))).first()
    assert runtime.consume_agent(recovery.id)


def test_core_budget_and_capacity_stay_visible_and_durable(failed_qc):
    runtime, _, _, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        blocker = runtime.workflow_service(session).create_agent_task(None, TaskType.RECOVERY, {"blocker": True})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=blocker.id,
                               action=OWNED, metadata_json={"input_hash": blocker.input_hash}))
        session.commit()
        blocker_id = blocker.id
    for _ in range(2):
        assert runtime.schedule_content_qc_recovery(qc_id) == {"recovery_status": "DEFERRED", "recovery_task_id": None}
    with Session(runtime.engine) as session:
        runtime.task_service(session).cancel(blocker_id, actor_id="fixture")
        session.add(RuntimeSetting(key="recovery_max_rounds", value="0"))
        session.commit()
    assert runtime.schedule_content_qc_recovery(qc_id) == {"recovery_status": "BLOCKED", "recovery_task_id": None}
    assert len(recovery_rows(runtime)) == 1


def test_creation_failure_rolls_back_recovery_status_ownership_and_consumed(failed_qc, monkeypatch):
    runtime, _, content_id, qc_id, _, _ = failed_qc
    original = WorkflowService.create_qc_recovery_task

    def fail(flow, *args, **kwargs):
        original(flow, *args, **kwargs)
        raise OSError("after core commit")

    monkeypatch.setattr(WorkflowService, "create_qc_recovery_task", fail)
    with pytest.raises(TaskConflictError, match="after core commit"):
        runtime.consume_agent(qc_id)
    assert recovery_rows(runtime) == []
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "content_qc"
        assert session.exec(select(AuditEvent.id).where(AuditEvent.entity_id == qc_id, AuditEvent.action == AGENT_CONSUMED)).first() is None


def test_recovery_apply_failure_rolls_back_script_and_consumption(failed_qc, monkeypatch):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id), REPAIR, assistant.settings.agent_id)
    original = WorkflowService.apply_recovery_plan

    def fail(flow, *args, **kwargs):
        original(flow, *args, **kwargs)
        raise OSError("after repair commit")

    monkeypatch.setattr(WorkflowService, "apply_recovery_plan", fail)
    with pytest.raises(TaskConflictError, match="after repair commit"):
        runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.exec(select(AgentTask.id).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery.id)).first() is None
        assert session.exec(select(AuditEvent.id).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == AGENT_CONSUMED)).first() is None
        assert session.get(Content, content_id).current_stage == "recovery"


def test_admin_recovery_claim_race_serializes_on_sqlite_writer(failed_qc, monkeypatch):
    from glance_core.database import begin_immediate

    runtime, _, _, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
    entered, release, claimed = Event(), Event(), Event()
    original = runtime._content_qc_source

    def pause(*args, **kwargs):
        value = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return value

    def competing_writer():
        with Session(runtime.engine) as session:
            begin_immediate(session)
            claimed.set()
            session.rollback()

    monkeypatch.setattr(runtime, "_content_qc_source", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        repair = pool.submit(runtime.schedule_content_qc_recovery, qc_id)
        assert entered.wait(5)
        claim = pool.submit(competing_writer)
        with pytest.raises(TimeoutError):
            claim.result(timeout=0.1)
        assert not claimed.is_set()
        release.set()
        assert repair.result(timeout=5)["recovery_status"] == "PENDING"
        claim.result(timeout=5)


@pytest.mark.parametrize("first", ["claim", "recovery"])
def test_content_qc_recovery_serializes_with_actual_native_claim(failed_qc, monkeypatch, first):
    runtime, http, content_id, qc_id, _, _ = failed_qc
    image = registered_client(http, runtime, "image")
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        cover = runtime.workflow_service(session).create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE,
                                                                    {"asset_purpose": "cover"}, allow_multiple=True)
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=cover.id,
                               action=OWNED, metadata_json={"input_hash": cover.input_hash}))
        session.commit()
        cover_id = cover.id

    def claim():
        with Session(runtime.engine) as session:
            return runtime.task_service(session).claim(task_id=cover_id, agent_id=image.settings.agent_id,
                                                       role_id="image", capabilities={TaskType.ASSET_GENERATE_IMAGE},
                                                       runtime_info=image.settings.runtime_report())

    if first == "claim":
        assert claim() is not None
        with pytest.raises(TaskConflictError, match="IN_FLIGHT"):
            runtime.schedule_content_qc_recovery(qc_id)
        assert recovery_rows(runtime) == []
        return
    entered, release, claiming = Event(), Event(), Event()
    original = runtime._content_qc_source

    def pause(*args, **kwargs):
        result = original(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result

    def claim_now():
        claiming.set()
        return claim()

    monkeypatch.setattr(runtime, "_content_qc_source", pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        recovery = pool.submit(runtime.schedule_content_qc_recovery, qc_id)
        assert entered.wait(5)
        claimant = pool.submit(claim_now)
        assert claiming.wait(5)
        try:
            with pytest.raises(TimeoutError):
                claimant.result(timeout=0.1)
        finally:
            release.set()
        assert recovery.result(timeout=5)["recovery_status"] == "PENDING"
        assert claimant.result(timeout=5) is not None
    assert len(recovery_rows(runtime)) == 1


def test_expired_offer_does_not_block_qc_recovery(failed_qc):
    runtime, _, _, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        session.add(SchedulerOffer(request_id="expired", agent_id="worker", role_id="script", task_id=qc_id,
                                    expires_at=utc_now() - timedelta(seconds=1)))
        session.commit()
    assert runtime.schedule_content_qc_recovery(qc_id)["recovery_status"] == "PENDING"


def test_bound_restoration_receipt_keeps_exact_input_and_recovery_budget(failed_qc):
    runtime, _, _, qc_id, _, root = failed_qc
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, qc_id)
        receipt = Artifact(content_id=qc.content_id, artifact_type="acceptance_input", relative_path="render/restored.json",
                            mime_type="application/json", producer_stage="visual_plan_restoration", sha256="", size_bytes=0)
        frozen = {**qc.input_data, "acceptance_receipt_artifact_id": receipt.id}
        path = root / receipt.relative_path
        path.write_text(json.dumps(frozen), encoding="utf-8")
        receipt.sha256, receipt.size_bytes = sha256_file(path), path.stat().st_size
        qc.input_data, qc.input_hash = frozen, stable_hash(frozen)
        owner = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc_id, AuditEvent.action == OWNED)).one()
        owner.metadata_json = {"input_hash": qc.input_hash}
        session.add(owner)
        session.add(qc)
        session.add(receipt)
        consumed(session, qc_id)
    assert runtime.schedule_content_qc_recovery(qc_id)["recovery_status"] == "PENDING"


@pytest.mark.parametrize("setting", ["recovery_enabled", "recovery_max_rounds"])
def test_invalid_settings_fail_closed_without_recovery(failed_qc, setting):
    runtime, http, _, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        session.add(RuntimeSetting(key=setting, value="invalid"))
        session.commit()
    response = http.post(f"/api/v1/agent/tasks/{qc_id}/content-qc-recovery", headers=ADMIN)
    assert response.status_code == 503
    assert recovery_rows(runtime) == []


def test_disabled_recovery_respects_existing_core_boundary(failed_qc):
    runtime, _, _, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        session.add(RuntimeSetting(key="recovery_enabled", value="false"))
        session.commit()
    assert runtime.schedule_content_qc_recovery(qc_id)["recovery_status"] == "BLOCKED"
    assert recovery_rows(runtime) == []


@pytest.mark.parametrize("damage", ["raw", "accepted", "completed", "owned", "result", "input", "deployment"])
def test_recovery_executor_own_acceptance_cannot_change_before_apply(failed_qc, damage):
    runtime, http, _, qc_id, _, _ = failed_qc
    runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, recovery.id)
        accept(session, runtime, task, REPAIR, assistant.settings.agent_id)
        if damage in {"raw", "accepted"}:
            submitted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
            if damage == "raw":
                Path(submitted.raw_result_path).write_text("{}", encoding="utf-8")
            else:
                session.delete(submitted)
        elif damage in {"completed", "owned", "deployment"}:
            action, target = ("mpt_deployment_verified", assistant.settings.agent_id) if damage == "deployment" else (OWNED if damage == "owned" else "completed", task.id)
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == target, AuditEvent.action == action)).one())
        elif damage == "input":
            task.input_data = {**task.input_data, "changed": True}
            session.add(task)
        else:
            task.result_data = {**task.result_data, "reason": "changed"}
            session.add(task)
        session.commit()
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery.id)
    with Session(runtime.engine) as session:
        assert session.exec(select(AgentTask.id).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery.id)).first() is None


def test_new_server_receipt_without_audio_binds_full_failed_qc(failed_qc):
    from app.business import pipeline
    from app.business.media_executor import MptMediaExecutor

    runtime, _, content_id, qc_id, _, root = failed_qc
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, qc_id)
        review = json.loads((root / "render" / "acceptance-input.json").read_text(encoding="utf-8"))
        artifact = pipeline._write_acceptance_receipt(session, session.get(Content, content_id), MptMediaExecutor(root), review)
        qc.input_data = {**qc.input_data, "acceptance_receipt_artifact_id": artifact.id}
        qc.input_hash = stable_hash(qc.input_data)
        owner = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc_id, AuditEvent.action == OWNED)).one()
        owner.metadata_json = {"input_hash": qc.input_hash}
        session.add(qc)
        session.add(owner)
        session.commit()
    assert runtime.consume_agent(qc_id)
    assert len(recovery_rows(runtime)) == 1


@pytest.mark.parametrize("subject", ["task", "content"])
def test_quarantined_acceptance_never_creates_qc_recovery(failed_qc, subject):
    from app.business.quarantine import CONTENT_QUARANTINE, TASK_QUARANTINE

    runtime, http, content_id, qc_id, _, _ = failed_qc
    with Session(runtime.engine) as session:
        consumed(session, qc_id)
        session.add(AuditEvent(actor_type="system", actor_id="migration", entity_type="agent_task" if subject == "task" else "content",
                               entity_id=qc_id if subject == "task" else content_id,
                               action=TASK_QUARANTINE if subject == "task" else CONTENT_QUARANTINE))
        session.commit()
    assert http.post(f"/api/v1/agent/tasks/{qc_id}/content-qc-recovery", headers=ADMIN).status_code == 409
    assert recovery_rows(runtime) == []
