"""Preflight failures keep native Recovery provenance and immutable historical evidence."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path

import pytest
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_core.agent_roles import role_spec
from glance_core.content_acceptance import acceptance_subject
from glance_core.models import AgentSubmission, AgentTask, AuditEvent, Candidate, Content, ContentStatus, RightsStatus, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_service import TaskConflictError, stable_hash
from glance_core.security import issue_role_token
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

from app.business import bundles, pipeline
from app.business.content_run import start_run
from app.business.runtime import AGENT_CONSUMED, FAILURE_RECOVERY, FAILURE_RECOVERY_RECONCILED, OWNED
from test.business.test_agent_failure_recovery import registered_client, record_intent, recovery_rows
from test.business import test_content_qc_recovery as qc_fixtures
from test.business.test_discovery_http import ADMIN

failed_qc = qc_fixtures.failed_qc
image_contract = qc_fixtures.image_contract


ERRORS = [{"path": ["visual_evidence"], "message": "VISUAL_EVIDENCE_PLAN_END"}]
PLAN = {
    "failure_class": "CONTEXT_MISSING", "action": "repair_task_context", "target_task_type": "content_qc",
    "reason": "Plan endpoint and final timeline do not agree; preserve the failed revision",
    "must_change": ["Issue a valid new immutable review subject"],
    "must_preserve": ["Original failure, video and evidence"],
    "must_not_do": ["Edit frozen evidence", "Bypass QC or publish"],
    "acceptance_checks": ["New final and independent complete QC pass"],
}


def qc_claim(failed_qc):
    runtime, http, content_id, old_id, _, root = failed_qc
    spec = role_spec("script")
    worker = AgentClient(AgentSettings(token=issue_role_token("test-root", "script"), agent_id="mpt-codex-script",
                                       role_id="script", skill_name=spec.skill_name, skill_version=spec.skill_version))
    worker.client.close()
    worker.client = http
    with Session(runtime.engine) as session:
        old = session.get(AgentTask, old_id)
        inputs = deepcopy(old.input_data)
        video_hash = inputs["acceptance_subject"]["video_sha256"]
        inputs["visual_evidence_version"] = 1
        inputs["visual_evidence"] = {
            "manifest_path": "review/manifest.json",
            "manifest": {"input_video": {"expected_sha256": video_hash}},
        }
        refs = {**inputs["upstream_task_ids"], "video_artifact_id": inputs["video_artifact_id"]}
        inputs["acceptance_subject"] = acceptance_subject(
            session, content_id, refs, source_context_version=1,
            audio_evidence=inputs["audio_evidence"], visual_evidence=inputs["visual_evidence"],
        )
        task = runtime.workflow_service(session).create_agent_task(
            content_id, TaskType.CONTENT_QC, inputs, schema_version="3", allow_multiple=True,
        )
        task_id = task.id
    claim = worker.claim()
    assert claim["task_id"] == task_id
    worker.confirm_claim(task_id, claim["lease_token"], worker.settings.agent_id)
    return runtime, http, content_id, root, worker, claim


def legacy_accepted_chain(failed_qc):
    runtime, http, content_id, root, worker, claim = qc_claim(failed_qc)
    # Reproduce the historical producer: a real lease failure plus the portable creation audit.
    with Session(runtime.engine) as session:
        service = runtime.task_service(session)
        task = session.get(AgentTask, claim["task_id"])
        message = "visual_evidence: VISUAL_EVIDENCE_PLAN_END"
        failed = service.fail(task_id=task.id, lease_token=claim["lease_token"], agent_id=worker.settings.agent_id,
                              retryable=False, code="ENVIRONMENT_MISSING_INPUT", message=message, attempt=task.attempt)
        runtime.workflow_service(session).create_recovery_task(
            failed, code=failed.error_code, message=message, retryable=False,
            validation_errors=ERRORS, actor_id="bundle-preflight",
        )
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    claimed = assistant.claim()
    assistant.confirm_claim(recovery.id, claimed["lease_token"], assistant.settings.agent_id)
    result = assistant.complete(recovery.id, claimed["lease_token"], assistant.settings.agent_id, PLAN)
    assert result["status"] == "COMPLETED" and result["advanced"] == "PENDING"
    return runtime, http, content_id, root, claim["task_id"], recovery.id


@pytest.mark.parametrize("method", ["build_bundle", "fail_bundle_preflight"])
@pytest.mark.parametrize("explicit_none", [False, True])
def test_missing_mpt_handler_is_rejected_before_lease_failure_or_receipt_writes(failed_qc, method, explicit_none):
    runtime, _, _, _, _, claim = qc_claim(failed_qc)
    with Session(runtime.engine) as session:
        service = runtime.task_service(session)
        task = session.get(AgentTask, claim["task_id"])
        before = task.model_dump(mode="json")
        audits = [row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()]
        flow = runtime.workflow_service(session)
        args = (task.id, claim["lease_token"], service, session, flow) if method == "build_bundle" else (
            service, task, claim["lease_token"], ERRORS, flow,
        )
        with pytest.raises(ValueError if explicit_none else TypeError):
            getattr(bundles, method)(*args, **({"failure_recovery": None} if explicit_none else {}))
        session.expire_all()
        assert session.get(AgentTask, task.id).model_dump(mode="json") == before
        assert [row.model_dump(mode="json") for row in session.exec(select(AuditEvent)).all()] == audits
    assert recovery_rows(runtime) == []


def test_real_bundle_preflight_creates_owned_failure_receipt_and_consumes_normally(failed_qc, monkeypatch):
    runtime, http, content_id, _, worker, claim = qc_claim(failed_qc)

    def invalid_endpoint(*args, **kwargs):
        raise ValueError("VISUAL_EVIDENCE_PLAN_END")

    monkeypatch.setattr(bundles, "verify_visual_joint_evidence", invalid_endpoint)
    response = http.get(claim["bundle_url"], headers={"Authorization": f"Bearer {claim['lease_token']}"})
    assert response.status_code == 409, response.text
    recovery_id = response.json()["detail"]["recovery_task_id"]
    assert recovery_id
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, claim["task_id"])
        recovery = session.get(AgentTask, recovery_id)
        assert source.error_message == "visual_evidence: VISUAL_EVIDENCE_PLAN_END"
        assert (source.attempt, source.max_attempts) == (1, 3)
        assert recovery.input_data["failure"]["validation_errors"] == ERRORS
        assert session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == FAILURE_RECOVERY,
        )).one().metadata_json["source_input_hash"] == source.input_hash
        assert session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == OWNED,
        )).one()
        original = source.model_dump(mode="json")
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assistant.confirm_claim(recovery_id, claim["lease_token"], assistant.settings.agent_id)
    assert assistant.complete(recovery_id, claim["lease_token"], assistant.settings.agent_id, PLAN)["advanced"] == "CONSUMED"
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        assert (content.business_status, content.current_stage) == (ContentStatus.FAILED, "recovery_blocked")
        assert session.get(AgentTask, original["id"]).model_dump(mode="json") == original
        assert session.exec(select(AgentTask.id).where(
            AgentTask.input_data["recovery_task_id"].as_string() == recovery_id,
        )).first() is None


def test_admin_reconciles_only_proven_old_bundle_chain_and_original_render_can_resume(failed_qc, monkeypatch):
    runtime, http, content_id, root, source_id, recovery_id = legacy_accepted_chain(failed_qc)
    url = f"/api/v1/agent/tasks/{source_id}/retry-recovery"
    assert http.post(url).status_code == 401
    before_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with Session(runtime.engine) as session:
        original = {key: session.get(AgentTask, key).model_dump(mode="json") for key in (source_id, recovery_id)}
    with pytest.raises(TaskConflictError, match="RECOVERY_FAILURE_RECEIPT_MISSING"):
        runtime.consume_agent(recovery_id)
    assert http.post(f"/api/v1/contents/{content_id}/render", headers=ADMIN).status_code == 409
    result = http.post(url, headers=ADMIN)
    assert result.status_code == 200, result.text
    assert result.json() == {"recovery_status": "COMPLETED", "recovery_task_id": recovery_id}
    assert http.post(url, headers=ADMIN).json() == result.json()
    with Session(runtime.engine) as session:
        receipt = session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == FAILURE_RECOVERY_RECONCILED,
        )).one()
        assert (receipt.actor_type, receipt.actor_id) == ("admin", "mpt-admin-api")
        assert receipt.metadata_json["accepted_result_hash"] == stable_hash(original[recovery_id]["result_data"])
        assert session.exec(select(AuditEvent.id).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == FAILURE_RECOVERY,
        )).first() is None
    assert runtime.consume_agent(recovery_id)
    assert runtime.consume_agent(recovery_id) is False
    with Session(runtime.engine) as session:
        assert {key: session.get(AgentTask, key).model_dump(mode="json") for key in original} == original
        assert session.get(Content, content_id).business_status == ContentStatus.FAILED
        assert session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == "recovery_escalated",
        )).one()
    assert all(path.read_bytes() == data for path, data in before_files.items())
    calls = []

    def render(engine, settings, target_id):
        # Only prove the existing HTTP status gate; media revision/QC tests own actual rendering.
        calls.append(target_id)
        with Session(engine) as session:
            return session.get(Content, target_id)

    monkeypatch.setattr(pipeline, "render_content", render)
    assert http.post(f"/api/v1/contents/{content_id}/render", headers=ADMIN).status_code == 200
    assert calls == [content_id]


@pytest.mark.parametrize("damage", ["origin_actor", "origin_metadata", "no_origin", "source_input", "source_failure",
                                   "source_owner", "source_deployment", "recovery_input", "recovery_owner",
                                   "assistant_deployment", "accepted_hash", "raw_result", "completed_hash",
                                   "new_source", "active", "offer", "publish", "archive", "final_bytes", "pending_recovery", "active_run"])
def test_old_bundle_reconciliation_fails_closed_without_rewriting_evidence(failed_qc, damage):
    runtime, http, content_id, root, source_id, recovery_id = legacy_accepted_chain(failed_qc)
    with Session(runtime.engine) as session:
        source, recovery = (session.get(AgentTask, key) for key in (source_id, recovery_id))
        if damage.startswith("origin") or damage == "no_origin":
            event = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == recovery_id, AuditEvent.action == "recovery_task_created",
            )).one()
            if damage == "no_origin":
                session.delete(event)
            else:
                if damage == "origin_actor":
                    event.actor_id = "imported-pipeline"
                else:
                    event.metadata_json = {**event.metadata_json, "target_task_id": "foreign"}
                session.add(event)
        elif damage in {"source_input", "recovery_input"}:
            task = source if damage == "source_input" else recovery
            task.input_data = {**task.input_data, "mutated": True}
            session.add(task)
        elif damage == "source_failure":
            source.error_message = "changed"
            session.add(source)
        elif damage == "pending_recovery":
            recovery.status = TaskStatus.PENDING
            session.add(recovery)
        elif damage == "active_run":
            start_run(session, content_id)
        elif damage.endswith("owner"):
            task = source if damage.startswith("source") else recovery
            session.delete(session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == task.id, AuditEvent.action == OWNED,
            )).one())
        elif damage.endswith("deployment"):
            task = source if damage.startswith("source") else recovery
            session.delete(session.exec(select(AuditEvent).where(
                AuditEvent.action == "mpt_deployment_verified", AuditEvent.entity_id == task.assigned_agent_id,
            )).one())
        elif damage in {"accepted_hash", "raw_result"}:
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == recovery_id)).one()
            if damage == "raw_result":
                Path(submission.raw_result_path).write_text("{}", encoding="utf-8")
            else:
                submission.result_hash = "changed"
                session.add(submission)
        elif damage == "completed_hash":
            event = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == recovery_id, AuditEvent.action == "completed",
            )).one()
            event.metadata_json = {"result_hash": "changed"}
            session.add(event)
        elif damage in {"active", "new_source"}:
            kind = TaskType.CONTENT_QC if damage == "new_source" else TaskType.FINAL_REVIEW_ASSIST
            runtime.workflow_service(session).create_agent_task(content_id, kind, {"revision": 2}, allow_multiple=True)
        elif damage == "offer":
            session.add(SchedulerOffer(request_id="live-offer", task_id=source_id, task_type=source.task_type,
                                       agent_id="reserved", scheduler_id="mpt-codex",
                                       expires_at=utc_now() + timedelta(minutes=5)))
        elif damage == "publish":
            record_intent(session, content_id)
        elif damage == "archive":
            content = session.get(Content, content_id)
            content.business_status = ContentStatus.ARCHIVED
            session.add(content)
        else:
            (root / "final.mp4").write_bytes(b"changed-final")
        session.commit()
        before = {key: session.get(AgentTask, key).model_dump(mode="json") for key in (source_id, recovery_id)}
    response = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
    assert response.status_code == 409, response.text
    with Session(runtime.engine) as session:
        assert {key: session.get(AgentTask, key).model_dump(mode="json") for key in before} == before
        assert session.exec(select(AuditEvent.id).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action.in_([FAILURE_RECOVERY, FAILURE_RECOVERY_RECONCILED, AGENT_CONSUMED]),
        )).first() is None


@pytest.mark.parametrize("damage", ["origin", "receipt", "accepted_result", "new_recovery", "active_run", "publish",
                                   "final_bytes", "rights", "rights_rejected", "source_context", "content_status", "content_stage"])
def test_reconciled_receipt_is_revalidated_before_consume(failed_qc, damage):
    runtime, http, content_id, root, source_id, recovery_id = legacy_accepted_chain(failed_qc)
    assert http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        if damage in {"origin", "receipt"}:
            action = "recovery_task_created" if damage == "origin" else FAILURE_RECOVERY_RECONCILED
            receipt = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == recovery_id, AuditEvent.action == action,
            )).one()
            receipt.metadata_json = {**receipt.metadata_json, "changed": True} if damage == "origin" else {
                **receipt.metadata_json, "origin_metadata_hash": "changed",
            }
            session.add(receipt)
        elif damage == "accepted_result":
            recovery = session.get(AgentTask, recovery_id)
            recovery.result_data = {**recovery.result_data, "reason": "changed after adoption"}
            session.add(recovery)
        elif damage == "new_recovery":
            runtime.workflow_service(session).create_agent_task(content_id, TaskType.RECOVERY, {"revision": 2}, allow_multiple=True)
        elif damage == "active_run":
            start_run(session, content_id)
        elif damage == "publish":
            record_intent(session, content_id)
        elif damage == "final_bytes":
            video = root / "final.mp4"
            replacement = b"changed---final"
            assert len(replacement) == video.stat().st_size
            video.write_bytes(replacement)
        elif damage in {"rights", "rights_rejected", "source_context"}:
            candidate = session.get(Candidate, session.get(Content, content_id).candidate_id)
            if damage == "source_context":
                candidate.source_account = "changed-after-adoption"
            else:
                candidate.rights_status = RightsStatus.EXPLICIT_LICENSE if damage == "rights" else RightsStatus.REJECTED
            session.add(candidate)
        else:
            content = session.get(Content, content_id)
            if damage == "content_status":
                content.business_status = ContentStatus.FINAL_REVIEW
            else:
                content.current_stage = "content_qc"
            session.add(content)
        session.commit()
        before_content = session.get(Content, content_id).model_dump(mode="json")
        before_tasks = {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()}
        before_audits = {event.id: event.model_dump(mode="json") for event in session.exec(select(AuditEvent)).all()}
    before_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises((TaskConflictError, ValueError)):
        runtime.consume_agent(recovery_id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).model_dump(mode="json") == before_content
        assert {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()} == before_tasks
        assert {event.id: event.model_dump(mode="json") for event in session.exec(select(AuditEvent)).all()} == before_audits
        assert session.exec(select(AuditEvent.id).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == AGENT_CONSUMED,
        )).first() is None
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files


@pytest.mark.parametrize("failure_point", ["marker_commit", "portable_apply"])
def test_reconciled_apply_and_consumed_marker_roll_back_together_and_retry_once(failed_qc, monkeypatch, failure_point):
    runtime, http, content_id, root, source_id, recovery_id = legacy_accepted_chain(failed_qc)
    assert http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        before_content = session.get(Content, content_id).model_dump(mode="json")
        before_tasks = {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()}
        before_audits = {event.id: event.model_dump(mode="json") for event in session.exec(select(AuditEvent)).all()}
    before_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    original_commit, original_apply = Session.commit, WorkflowService.apply_recovery_plan

    def fail_marker_commit(session):
        if any(isinstance(row, AuditEvent) and row.action == AGENT_CONSUMED
               and row.entity_id == recovery_id for row in session.new):
            raise OSError("synthetic consumed marker storage failure")
        return original_commit(session)

    def fail_after_portable_apply(flow, recovery):
        result = original_apply(flow, recovery)
        if recovery.id == recovery_id:
            raise OSError("synthetic failure after portable apply flush")
        return result

    with monkeypatch.context() as patch:
        if failure_point == "marker_commit":
            patch.setattr(Session, "commit", fail_marker_commit)
        else:
            patch.setattr(WorkflowService, "apply_recovery_plan", fail_after_portable_apply)
        with pytest.raises(TaskConflictError, match="synthetic"):
            runtime.consume_agent(recovery_id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).model_dump(mode="json") == before_content
        assert {task.id: task.model_dump(mode="json") for task in session.exec(select(AgentTask)).all()} == before_tasks
        assert {event.id: event.model_dump(mode="json") for event in session.exec(select(AuditEvent)).all()} == before_audits
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
    assert runtime.consume_agent(recovery_id)
    assert runtime.consume_agent(recovery_id) is False
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).business_status == ContentStatus.FAILED
        assert session.get(Content, content_id).current_stage == "recovery_blocked"
        assert session.get(AgentTask, source_id).model_dump(mode="json") == before_tasks[source_id]
        assert session.get(AgentTask, recovery_id).model_dump(mode="json") == before_tasks[recovery_id]
        assert len(session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == AGENT_CONSUMED,
        )).all()) == 1
        assert len(session.exec(select(AuditEvent).where(
            AuditEvent.entity_id == recovery_id, AuditEvent.action == "recovery_escalated",
        )).all()) == 1
        assert session.exec(select(AgentTask.id).where(
            AgentTask.input_data["recovery_task_id"].as_string() == recovery_id,
        )).first() is None
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before_files
