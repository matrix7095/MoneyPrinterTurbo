"""Terminal failures enter durable Recovery through current MPT authority only."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_core.agent_roles import role_spec
from glance_core.models import AgentTask, AuditEvent, Content, ContentStatus, ProductionMode, RuntimeSetting, TaskStatus, TaskType, utc_now
from glance_core.security import issue_role_token
from glance_core.task_service import stable_hash
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

from app.business.quarantine import CONTENT_QUARANTINE, TASK_QUARANTINE
from app.business.runtime import AGENT_CONSUMED, OWNED, FAILURE_RECOVERY
from test.business.test_content_http import seed_candidate
from test.business.test_deployment_http import app_for, stage
from test.business.test_discovery_http import ADMIN, make_runtime


def registered_client(http, runtime, role):
    payload = stage(runtime, role)
    spec = role_spec(role)
    client = AgentClient(AgentSettings(
        token=issue_role_token("test-root", role), agent_id=payload["agent_id"], role_id=role,
        skill_name=spec.skill_name, skill_version=spec.skill_version,
    ))
    client.client.close()
    client.client = http
    client.whoami()
    assert client.deployment(payload)["status"] == "VERIFIED"
    return client


def claim_source(http, runtime, *, image=False):
    response = http.post(f"/api/v1/candidates/{seed_candidate(runtime)}/select?production_mode=image_only", headers=ADMIN)
    assert response.status_code == 200
    content_id = response.json()["id"]
    if image:
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)

            def completed(kind, inputs, result):
                task = flow.create_agent_task(content_id, kind, inputs, allow_multiple=True)
                task.status, task.result_data, task.completed_at = TaskStatus.COMPLETED, result, utc_now()
                session.add(task)
                for action, metadata in ((OWNED, {"input_hash": task.input_hash}),
                                         (AGENT_CONSUMED, {"result_hash": stable_hash(result)})):
                    session.add(AuditEvent(actor_type="system", actor_id="fixture",
                                           entity_type="agent_task", entity_id=task.id,
                                           action=action, metadata_json=metadata))
                session.commit()
                return task

            script = completed(TaskType.SHORT_SCRIPT, {"production_mode": "image_only"}, {"full_script": "家具概念示意"})
            plan = completed(TaskType.VISUAL_PLAN, {
                "production_mode": "image_only", "upstream_task_ids": {"script": script.id},
                "script": script.result_data, "voice_artifact_id": "preserved-voice",
            }, {"segments": [{"asset_request": {"prompt": "generic stackable stool illustration"}}]})
            source = flow.create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {
                "requests": [{"segment_index": 0, "asset_request": plan.result_data["segments"][0]["asset_request"]}],
                "script": script.result_data, "visual_plan": plan.result_data,
            }, allow_multiple=True)
            content = session.get(Content, content_id)
            content.current_stage = "asset_generate"
            session.add(content)
            session.commit()
            source_id = source.id
        client = registered_client(http, runtime, "image")
    else:
        client = registered_client(http, runtime, "research")
        source_id = None
    claim = client.claim()
    assert claim["status"] == "CLAIMED", claim
    if source_id:
        assert claim["task_id"] == source_id
    return client, claim, content_id


def fail_before_hook(runtime, client, claim, *, code="UNKNOWN_NATIVE_FAILURE", retryable=False):
    with Session(runtime.engine) as session:
        task = runtime.task_service(session).fail(
            task_id=claim["task_id"], lease_token=claim["lease_token"], agent_id=client.settings.agent_id,
            retryable=retryable, code=code, message="native capability could not satisfy the frozen request",
        )
        return task.id


def recovery_rows(runtime):
    with Session(runtime.engine) as session:
        return session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.RECOVERY)).all()


def test_fail_hook_commits_truthful_failure_and_creates_owned_recovery(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        response = client.fail(claim["task_id"], claim["lease_token"], client.settings.agent_id,
                               retryable=False, error_code="UNKNOWN_NATIVE_FAILURE", message="native failure")
        assert response["status"] == "FAILED"
        assert response["recovery_status"] == "PENDING"
        recovery = recovery_rows(runtime)[0]
        assert response["recovery_task_id"] == recovery.id
        assert recovery.input_data["failure"]["failure_class"] == "UNKNOWN"
        with Session(runtime.engine) as session:
            source = session.get(AgentTask, claim["task_id"])
            assert source.status == TaskStatus.FAILED and not source.result_data and source.lease_token_hash is None
            assert session.exec(select(AuditEvent).where(AuditEvent.action == OWNED, AuditEvent.entity_id == recovery.id)).one().metadata_json["input_hash"] == recovery.input_hash
            assert session.exec(select(AuditEvent).where(AuditEvent.action == FAILURE_RECOVERY)).one().metadata_json["source_task_id"] == source.id
        assert http.post(f"/api/v1/agent/tasks/{source.id}/retry-recovery", headers=ADMIN).json()["recovery_task_id"] == recovery.id
        assert len(recovery_rows(runtime)) == 1


def test_retryable_fail_uses_original_backoff_without_recovery(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        response = client.fail(claim["task_id"], claim["lease_token"], client.settings.agent_id,
                               retryable=True, error_code="NETWORK_TIMEOUT", message="temporary network failure")
        assert response["status"] == "PENDING" and response["recovery_status"] == "NOT_REQUIRED"
        assert recovery_rows(runtime) == []
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, claim["task_id"]).not_before is not None


def test_recovery_error_does_not_undo_failure_and_admin_can_resume(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        with monkeypatch.context() as patch:
            def unavailable(task_id):
                raise OSError("synthetic unavailable recovery storage")
            patch.setattr(runtime, "schedule_failure_recovery", unavailable)
            response = client.fail(claim["task_id"], claim["lease_token"], client.settings.agent_id,
                                   retryable=False, error_code="UNKNOWN_NATIVE_FAILURE", message="native failure")
        assert response == {"status": "FAILED", "task_id": claim["task_id"], "recovery_status": "PENDING", "recovery_task_id": None}
        assert http.post(f"/api/v1/agent/tasks/{claim['task_id']}/retry-recovery", headers=ADMIN).status_code == 200
        assert len(recovery_rows(runtime)) == 1


def test_image_failure_targets_verified_current_plan_and_preserves_frozen_inputs(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, content_id = claim_source(http, runtime, image=True)
        source_id = fail_before_hook(runtime, client, claim, code="GENERATED_IMAGE_CONSTRAINT_MISMATCH")
        with Session(runtime.engine) as session:
            source = session.get(AgentTask, source_id)
            frozen = deepcopy(source.input_data)
            plan = runtime.workflow_service(session).completed_task_for(content_id, TaskType.VISUAL_PLAN)
            parent_input = deepcopy(plan.input_data)
        response = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
        assert response.status_code == 200, response.text
        recovery = recovery_rows(runtime)[0]
        assert recovery.input_data["failure"]["failure_class"] == "BUSINESS_QC"
        assert recovery.input_data["lineage"]["repair_target_task_id"] == plan.id
        assert recovery.input_data["current_result"] == frozen["visual_plan"]
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source_id).input_data == frozen
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
            assert session.get(AgentTask, plan.id).input_data == parent_input


@pytest.mark.parametrize("damage", ["unowned", "input", "owner_hash", "quarantine_task", "quarantine_content",
                                   "unverified", "manifest", "late_failure", "failure_message", "leased", "submitted", "archived"])
def test_admin_recovery_rejects_unverified_or_noncurrent_source(tmp_path, damage):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, content_id = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        with Session(runtime.engine) as session:
            source = session.get(AgentTask, source_id)
            owner = session.exec(select(AuditEvent).where(AuditEvent.action == OWNED, AuditEvent.entity_id == source_id)).one()
            if damage == "unowned":
                session.delete(owner)
            elif damage == "owner_hash":
                owner.metadata_json = {"input_hash": "changed"}
                session.add(owner)
            elif damage == "input":
                source.input_data = {**source.input_data, "unexpected": "changed"}
            elif damage.startswith("quarantine"):
                session.add(AuditEvent(actor_type="system", actor_id="migration", entity_type="content" if damage.endswith("content") else "agent_task",
                                       entity_id=content_id if damage.endswith("content") else source_id,
                                       action=CONTENT_QUARANTINE if damage.endswith("content") else TASK_QUARANTINE))
            elif damage in {"unverified", "manifest"}:
                proof = session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_deployment_verified")).one()
                if damage == "unverified":
                    session.delete(proof)
                else:
                    from pathlib import Path
                    Path(proof.metadata_json["skill_path"]).parents[1].joinpath(f"{client.settings.agent_id}.json").write_text("{}", encoding="utf-8")
            elif damage == "late_failure":
                event = session.exec(select(AuditEvent).where(AuditEvent.action == "failed")).one()
                event.metadata_json = {**event.metadata_json, "late_report": True}
                session.add(event)
            elif damage == "failure_message":
                source.error_message = "changed after failure"
            elif damage in {"leased", "submitted"}:
                source.status = TaskStatus(damage.upper())
            else:
                content = session.get(Content, content_id)
                content.business_status = ContentStatus.ARCHIVED
                session.add(content)
            session.add(source)
            session.commit()
        assert http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN).status_code == 409
        assert recovery_rows(runtime) == []


@pytest.mark.parametrize("damage", ["plan_result", "plan_input", "script_result", "plan_owner", "plan_receipt", "new_plan"])
def test_image_recovery_rejects_stale_or_changed_upstream(tmp_path, damage):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, content_id = claim_source(http, runtime, image=True)
        source_id = fail_before_hook(runtime, client, claim, code="GENERATED_IMAGE_CONSTRAINT_MISMATCH")
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            if damage.endswith("result"):
                target = script if damage.startswith("script") else plan
                target.result_data = {"changed": "result"}
                session.add(target)
            elif damage == "plan_input":
                plan.input_data = {**plan.input_data, "voice_artifact_id": "changed"}
                session.add(plan)
            elif damage in {"plan_owner", "plan_receipt"}:
                action = OWNED if damage == "plan_owner" else AGENT_CONSUMED
                session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan.id, AuditEvent.action == action)).one())
            else:
                newer = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, {**plan.input_data, "revision": 2}, allow_multiple=True)
                newer.status, newer.result_data = TaskStatus.COMPLETED, plan.result_data
                session.add(newer)
            session.commit()
        assert http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN).status_code == 409
        assert recovery_rows(runtime) == []


def test_admin_recovery_auth_and_concurrent_retries_are_idempotent(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        url = f"/api/v1/agent/tasks/{source_id}/retry-recovery"
        assert http.post(url).status_code == 401
        assert http.post(url, headers={"Authorization": f"Bearer {client.settings.token.get_secret_value()}"}).status_code == 401
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: http.post(url, headers=ADMIN), range(2)))
        assert all(response.status_code == 200 for response in responses)
        assert len({response.json()["recovery_task_id"] for response in responses}) == 1
        assert len(recovery_rows(runtime)) == 1


def test_effective_disabled_recovery_is_blocked_without_rewriting_failure(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        with Session(runtime.engine) as session:
            session.add(RuntimeSetting(key="recovery_enabled", value="false"))
            session.commit()
        response = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
        assert response.status_code == 200
        assert response.json() == {"recovery_status": "BLOCKED", "recovery_task_id": None}
        assert recovery_rows(runtime) == []
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED


def test_recovery_creation_exception_rolls_back_new_work_only(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        original = WorkflowService.create_recovery_task

        def crash_after_creation(self, *args, **kwargs):
            original(self, *args, **kwargs)
            raise OSError("synthetic crash after portable internal commit")

        with monkeypatch.context() as patch:
            patch.setattr(WorkflowService, "create_recovery_task", crash_after_creation)
            with pytest.raises(OSError):
                runtime.schedule_failure_recovery(source_id)
        assert recovery_rows(runtime) == []
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
        assert runtime.schedule_failure_recovery(source_id)["recovery_task_id"]


REPAIR_PLAN = {
    "failure_class": "BUSINESS_QC", "action": "create_repair_task", "target_task_type": "visual_plan",
    "reason": "Two native generations could not preserve stool geometry; revise the illustration plan",
    "must_change": ["Use a schematic viewpoint with independently visible matching stool geometry"],
    "must_preserve": ["Accepted script", "Existing voice"], "must_not_do": ["Publish", "Invent uploaded assets"],
    "acceptance_checks": ["Two matching stool geometries are independently legible"],
}


def image_recovery(http, runtime):
    client, claim, content_id = claim_source(http, runtime, image=True)
    source_id = fail_before_hook(runtime, client, claim, code="GENERATED_IMAGE_CONSTRAINT_MISMATCH")
    response = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
    assert response.status_code == 200, response.text
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assert claim["status"] == "CLAIMED" and claim["task_id"] == response.json()["recovery_task_id"]
    return assistant, claim, source_id, content_id


def record_intent(session, content_id, status="UNKNOWN", *, malformed=False):
    target = {"platform": "douyin", "account_id": "fixture-account", "content_id": content_id,
              "video_artifact_id": "fixture-video", "video_sha256": "a" * 64}
    key = stable_hash(target)
    data = {**target, "intent_key": key, "intent_sequence": 1, "status": status}
    if malformed:
        data.pop("content_id")
    session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent",
                           entity_id=key, action="publish_intent_state", metadata_json=data))


def test_native_assistant_recovery_creates_one_visual_revision_and_preserves_script_voice(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        assistant, claim, source_id, content_id = image_recovery(http, runtime)
        with Session(runtime.engine) as session:
            original_script = runtime.workflow_service(session).completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            script_hash = stable_hash(original_script.result_data)
        result = assistant.complete(claim["task_id"], claim["lease_token"], assistant.settings.agent_id, REPAIR_PLAN)
        assert result["advanced"] == "CONSUMED", result
        assert assistant.complete(claim["task_id"], claim["lease_token"], assistant.settings.agent_id, REPAIR_PLAN)["advanced"] == "CONSUMED"
        with Session(runtime.engine) as session:
            repairs = session.exec(select(AgentTask).where(AgentTask.input_data["recovery_task_id"].as_string() == claim["task_id"])).all()
            assert len(repairs) == 1 and repairs[0].task_type == TaskType.VISUAL_PLAN
            assert repairs[0].status == TaskStatus.PENDING
            assert repairs[0].input_data["voice_artifact_id"] == "preserved-voice"
            assert repairs[0].input_data["upstream_task_ids"]["script"] == original_script.id
            assert repairs[0].input_data["reusable_generated_assets"] == []
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
            assert stable_hash(session.get(AgentTask, original_script.id).result_data) == script_hash
            assert session.get(Content, content_id).current_stage == "visual_plan"


@pytest.mark.parametrize("damage", ["plan_input", "plan_result", "script_input", "script_result", "source_input",
                                   "source_error", "envelope", "receipt", "plan_owner", "script_receipt",
                                   "source_deployment", "assistant_deployment", "pending_plan", "pending_script",
                                   "pending_source", "active_task", "unknown_publish", "malformed_intent"])
def test_queued_recovery_revalidates_authority_before_consuming(tmp_path, damage):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        assistant, claim, source_id, content_id = image_recovery(http, runtime)
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            source = session.get(AgentTask, source_id)
            recovery = session.get(AgentTask, claim["task_id"])
            if damage in {"plan_input", "script_input", "source_input", "envelope"}:
                target = {"plan_input": plan, "script_input": script, "source_input": source, "envelope": recovery}[damage]
                target.input_data = {**target.input_data, "changed": True}
                session.add(target)
            elif damage in {"plan_result", "script_result"}:
                target = script if damage == "script_result" else plan
                target.result_data = {"changed": True}
                session.add(target)
            elif damage == "source_error":
                source.error_message = "different failure"
                session.add(source)
            elif damage in {"receipt", "plan_owner", "script_receipt"}:
                action, target = {"receipt": (FAILURE_RECOVERY, recovery), "plan_owner": (OWNED, plan),
                                  "script_receipt": (AGENT_CONSUMED, script)}[damage]
                session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == target.id, AuditEvent.action == action)).one())
            elif damage.endswith("deployment"):
                identity = assistant.settings.agent_id if damage.startswith("assistant") else source.assigned_agent_id
                session.delete(session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_deployment_verified", AuditEvent.entity_id == identity)).one())
            elif damage in {"pending_plan", "pending_script", "pending_source", "active_task"}:
                target = {"pending_plan": plan, "pending_script": script, "pending_source": source, "active_task": script}[damage]
                newer = flow.create_agent_task(content_id, target.task_type, {**target.input_data, "revision": 2}, allow_multiple=True)
                if damage == "active_task":
                    newer.status = TaskStatus.LEASED
                    session.add(newer)
            else:
                record_intent(session, content_id, malformed=damage == "malformed_intent")
            session.commit()
        response = http.post(f"/api/v1/agent/tasks/{claim['task_id']}/complete",
                             headers={"Authorization": f"Bearer {claim['lease_token']}"},
                             json={"agent_id": assistant.settings.agent_id, "result": REPAIR_PLAN})
        if damage == "assistant_deployment":
            assert response.status_code == 409  # Shared native lease guard rejects before acceptance.
        else:
            assert response.status_code == 200, response.text
            result = response.json()
            assert result["status"] == "COMPLETED" and result["advanced"] == "PENDING", result
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
            assert session.exec(select(AgentTask.id).where(AgentTask.input_data["recovery_task_id"].as_string() == claim["task_id"])).first() is None
            assert session.exec(select(AuditEvent.id).where(AuditEvent.action == AGENT_CONSUMED, AuditEvent.entity_id == claim["task_id"])).first() is None


@pytest.mark.parametrize("damage", ["pending_revision", "unknown_publish", "authorized_publish", "malformed_intent", "corrupt_target", "published_intent"])
def test_recovery_creation_rejects_superseded_source_and_unreconciled_content(tmp_path, damage):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, content_id = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        with Session(runtime.engine) as session:
            if damage == "pending_revision":
                source = session.get(AgentTask, source_id)
                runtime.workflow_service(session).create_agent_task(content_id, source.task_type, {**source.input_data, "revision": 2}, allow_multiple=True)
            else:
                record_intent(session, content_id, status={"authorized_publish": "AUTHORIZED", "published_intent": "PUBLISHED"}.get(damage, "UNKNOWN"), malformed=damage == "malformed_intent")
                session.flush()
                if damage == "corrupt_target":
                    intent = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "publish_intent")).one()
                    intent.metadata_json = {**intent.metadata_json, "status": "FAILED", "video_sha256": "changed"}
                    session.add(intent)
            session.commit()
        assert http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN).status_code == 409
        assert recovery_rows(runtime) == []


@pytest.mark.parametrize("setting", ["recovery_max_rounds", "recovery_unknown_attempts"])
def test_recovery_honors_core_round_and_unknown_budgets(tmp_path, setting):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        with Session(runtime.engine) as session:
            session.add(RuntimeSetting(key=setting, value="0"))
            session.commit()
        result = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
        assert result.status_code == 200 and result.json()["recovery_status"] == "BLOCKED"
        assert recovery_rows(runtime) == []


def test_capacity_deferral_is_durable_and_can_be_retried_without_duplicate(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        source_id = fail_before_hook(runtime, client, claim)
        with Session(runtime.engine) as session:
            blocker = runtime.workflow_service(session).create_agent_task(None, TaskType.RECOVERY, {"capacity_fixture": True})
            session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=blocker.id,
                                   action=OWNED, metadata_json={"input_hash": blocker.input_hash}))
            session.commit()
            blocker_id = blocker.id
        for _ in range(2):
            assert runtime.schedule_failure_recovery(source_id) == {"recovery_status": "DEFERRED", "recovery_task_id": None}
        with Session(runtime.engine) as session:
            assert len(session.exec(select(AuditEvent).where(AuditEvent.action == "recovery_capacity_deferred")).all()) == 1
            runtime.task_service(session).cancel(blocker_id, actor_id="fixture-admin")
        first = runtime.schedule_failure_recovery(source_id)
        assert first["recovery_status"] == "PENDING" and first["recovery_task_id"]
        assert runtime.schedule_failure_recovery(source_id) == first


def test_invalid_failure_lease_cannot_create_recovery(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        client, claim, _ = claim_source(http, runtime)
        result = http.post(f"/api/v1/agent/tasks/{claim['task_id']}/fail", headers={"Authorization": "Bearer invalid-lease"},
                           json={"agent_id": client.settings.agent_id, "retryable": False, "error_code": "UNKNOWN", "message": "invalid claim"})
        assert result.status_code == 409
        assert recovery_rows(runtime) == []


@pytest.mark.parametrize("newer_same_batch", [False, True])
def test_footage_script_failure_validates_each_current_scene_batch(tmp_path, newer_same_batch):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        image, image_claim, content_id = claim_source(http, runtime, image=True)
        fail_before_hook(runtime, image, image_claim)
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            content = session.get(Content, content_id)
            content.production_mode = ProductionMode.FOOTAGE
            session.add(content)
            session.commit()
            research = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            scenes = []
            for index in (1, 2):
                scene = flow.create_agent_task(content_id, TaskType.SCENE_ANALYZE, {"batch_index": index, "batch_count": 2}, allow_multiple=True)
                scene.status, scene.result_data = TaskStatus.COMPLETED, {"scenes": []}
                session.add(scene)
                for action, metadata in ((OWNED, {"input_hash": scene.input_hash}), (AGENT_CONSUMED, {"result_hash": stable_hash(scene.result_data)})):
                    session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task", entity_id=scene.id,
                                           action=action, metadata_json=metadata))
                scenes.append(scene)
            source = flow.create_agent_task(content_id, TaskType.SHORT_SCRIPT, {
                "upstream_task_ids": {"scene_analysis": [scene.id for scene in scenes], "video_artifact_id": "artifact-reference"},
                "previous_script": research.result_data, "production_mode": "footage",
            }, allow_multiple=True)
            source_id = source.id
            session.commit()
        script = registered_client(http, runtime, "script")
        claim = script.claim()
        assert claim["task_id"] == source_id
        fail_before_hook(runtime, script, claim)
        if newer_same_batch:
            with Session(runtime.engine) as session:
                runtime.workflow_service(session).create_agent_task(content_id, TaskType.SCENE_ANALYZE,
                                                                    {"batch_index": 1, "batch_count": 2, "revision": 2}, allow_multiple=True)
        response = http.post(f"/api/v1/agent/tasks/{source_id}/retry-recovery", headers=ADMIN)
        assert response.status_code == (409 if newer_same_batch else 200), response.text


def test_completed_qc_with_prior_retryable_failure_retains_portable_recovery(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        image, image_claim, content_id = claim_source(http, runtime, image=True)
        fail_before_hook(runtime, image, image_claim)
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            plan_id = plan.id
            qc = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN_QC, {
                "upstream_task_ids": {"visual_plan": plan.id, "script": script.id}, "plan": plan.result_data,
            }, allow_multiple=True)
            qc_id = qc.id
        visual = registered_client(http, runtime, "visual")
        claim = visual.claim()
        assert claim["task_id"] == qc_id
        result = visual.fail(qc_id, claim["lease_token"], visual.settings.agent_id, retryable=True,
                             error_code="NETWORK_TIMEOUT", message="transient fixture failure")
        assert result["status"] == "PENDING"
        future = utc_now() + timedelta(minutes=6)
        monkeypatch.setattr("glance_core.task_service.utc_now", lambda: future)
        claim = visual.claim()
        assert claim["task_id"] == qc_id
        result = visual.complete(qc_id, claim["lease_token"], visual.settings.agent_id, {
            "passed": False, "issues": [{"severity": "error", "field": "segments", "message": "geometry needs repair"}],
        })
        assert result["advanced"] == "CONSUMED", result
        with Session(runtime.engine) as session:
            recovery = runtime.workflow_service(session).create_recovery_task(
                session.get(AgentTask, qc_id), code="VISUAL_PLAN_QC_FAILED", message="geometry needs repair",
                failure_class="BUSINESS_QC", repair_target_task_id=plan_id,
            )
            assert recovery is not None
        recovery = recovery_rows(runtime)[0]
        with Session(runtime.engine) as session:
            assert session.exec(select(AuditEvent.id).where(AuditEvent.action == FAILURE_RECOVERY, AuditEvent.entity_id == recovery.id)).first() is None
            assert session.get(AgentTask, qc_id).status == TaskStatus.COMPLETED
        assistant = registered_client(http, runtime, "assistant")
        claim = assistant.claim()
        assert claim["task_id"] == recovery.id
        result = assistant.complete(recovery.id, claim["lease_token"], assistant.settings.agent_id,
                                    {"failure_class": "BUSINESS_QC", "action": "escalate_to_human", "reason": "review fixture"})
        assert result["advanced"] == "CONSUMED", result


def test_visual_repair_marker_failure_replays_without_duplicate_revision(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        assistant, claim, source_id, _ = image_recovery(http, runtime)
        original = Session.commit

        def missing_marker(self):
            if any(isinstance(row, AuditEvent) and row.action == AGENT_CONSUMED and row.entity_id == claim["task_id"] for row in self.new):
                raise OSError("synthetic marker storage failure")
            return original(self)

        with monkeypatch.context() as patch:
            patch.setattr(Session, "commit", missing_marker)
            assert assistant.complete(claim["task_id"], claim["lease_token"], assistant.settings.agent_id, REPAIR_PLAN)["advanced"] == "PENDING"
        assert runtime.consume_agent(claim["task_id"])
        assert not runtime.consume_agent(claim["task_id"])
        with Session(runtime.engine) as session:
            assert len(session.exec(select(AgentTask).where(AgentTask.input_data["recovery_task_id"].as_string() == claim["task_id"])).all()) == 1
            assert session.get(AgentTask, source_id).status == TaskStatus.FAILED
