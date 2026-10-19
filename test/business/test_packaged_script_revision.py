"""A packaged manual revision uses the existing script lifecycle and immutable input."""

from copy import deepcopy
from datetime import timedelta

import pytest
from glance_core.content_acceptance import resolved_visual_plan
from glance_core.models import (
    AgentTask, Artifact, AuditEvent, Content, ContentStatus, PublicationMode,
    PublicationRecord, SchedulerOffer, TaskStatus, TaskType, utc_now,
)
from glance_core.task_service import stable_hash
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

from app.business import pipeline
from test.business.test_discovery_http import ADMIN
from test.business.test_kokoro_voice_rebuild import contract as contract, old_snapshot, prove_completed


NOTE = (
    "仅将‘定位为共享专注工作舱’改为‘主打共享专注工作舱’；"
    "将‘它的占地面积为2.5平方米’改为‘它的占地面积是2.5平方米’。"
    "保留其他文字、事实、claim_id 和 2.5 的精度，独立核验新稿。"
)


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/reject-script"


def historical_materials(runtime, ids):
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        old_time = utc_now() - timedelta(days=1)
        cover = flow.create_agent_task(ids["content"], TaskType.ASSET_GENERATE_IMAGE,
                                       {"asset_purpose": "cover"}, allow_multiple=True)
        cover.status, cover.result_data = TaskStatus.COMPLETED, {"images": []}
        session.add(cover)
        negative = flow.create_agent_task(ids["content"], TaskType.ASSET_QC,
                                          {"historical": True}, allow_multiple=True)
        negative.status, negative.result_data = TaskStatus.COMPLETED, {"passed": False, "issues": [{"message": "old"}]}
        negative.created_at = negative.completed_at = old_time
        session.add(negative)
        failed = flow.create_agent_task(ids["content"], TaskType.CONTENT_QC,
                                        {"historical": True}, allow_multiple=True)
        failed.status, failed.result_data = TaskStatus.COMPLETED, {"passed": False, "issues": [{"message": "obsolete S4"}]}
        failed.created_at = failed.completed_at = old_time
        session.add(failed)
        session.commit()
        return cover.id, negative.id, failed.id


def test_packaged_revision_freezes_resolved_plan_and_preserves_negative_cover_and_media(contract):
    runtime, http, ids, calls, _, _ = contract
    protected = historical_materials(runtime, ids)
    before = old_snapshot(runtime, ids["content"])
    with Session(runtime.engine) as session:
        artifacts = [row.model_dump(mode="json") for row in session.exec(select(Artifact)).all()]
        version = session.get(Content, ids["content"]).script_version
    assert http.post(endpoint(ids), json={"note": NOTE}).status_code == 401
    response = http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN)
    assert response.status_code == 200, response.text
    assert calls == []
    with Session(runtime.engine) as session:
        rewrite = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                       AgentTask.status == TaskStatus.PENDING)).one()
        assert rewrite.input_hash == stable_hash(rewrite.input_data)
        assert rewrite.input_data["packaged_script_revision"] is True
        assert NOTE in rewrite.input_data["reviewer_note"]
        assert "数值精度" in rewrite.input_data["reviewer_note"]
        assert rewrite.input_data["previous_script"]["full_narration"] == "已批准的原文，面积为2.5平方米。"
        assert [row["asset_id"] for row in rewrite.input_data["previous_plan"]["segments"]] == ids["images"]
        assert all("asset_request" not in row for row in rewrite.input_data["previous_plan"]["segments"])
        content = session.get(Content, ids["content"])
        assert content.script_version == version + 1
        assert (content.business_status, content.current_stage) == (ContentStatus.WAITING_AGENT, "script_rewrite")
        assert [row.model_dump(mode="json") for row in session.exec(select(Artifact)).all()] == artifacts
        for task_id, old_hash in before.items():
            task = session.get(AgentTask, task_id)
            if task_id in {*protected, ids["negative"], ids["recovery"]} or task.task_type in {TaskType.CONTENT_RESEARCH, TaskType.CONTENT_QC}:
                assert stable_hash(task.model_dump(mode="json")) == old_hash
            else:
                assert task.status == TaskStatus.CANCELLED
                assert task.dedup_key is None
                assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id, AuditEvent.action == "cancelled")).first()
        assert session.get(AgentTask, ids["script"]).result_data["full_narration"] == "已批准的原文，面积为2.5平方米。"
    assert http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN).status_code == 409


@pytest.mark.parametrize("blocker", ["publication", "intent", "pending", "leased", "submitted", "offer"])
def test_packaged_revision_preflight_blocks_before_cancellation(contract, blocker):
    runtime, http, ids, calls, _, _ = contract
    with Session(runtime.engine) as session:
        if blocker == "publication":
            session.add(PublicationRecord(content_id=ids["content"], platform="douyin", mode=PublicationMode.EXECUTOR))
        elif blocker == "intent":
            session.add(AuditEvent(actor_type="agent", actor_id="fixture", entity_type="publish_intent", entity_id="intent",
                                   action="publish_intent_registered", metadata_json={"content_id": ids["content"]}))
        elif blocker == "offer":
            session.add(SchedulerOffer(request_id="live-offer", task_id=ids["script"], expires_at=utc_now() + timedelta(minutes=5)))
        else:
            session.add(AgentTask(content_id=ids["content"], task_type=TaskType.RECOVERY, prompt_name="busy", instructions="busy",
                                  input_hash="busy", status=TaskStatus(blocker.upper())))
        session.commit()
    before = old_snapshot(runtime, ids["content"])
    response = http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN)
    assert response.status_code == 409, response.text
    assert before == old_snapshot(runtime, ids["content"]) and calls == []


def test_packaged_revision_creation_failure_rolls_back_cancellation(contract, monkeypatch):
    runtime, http, ids, _, _, _ = contract
    original = WorkflowService.create_agent_task

    def fail(self, content_id, kind, inputs, **kwargs):
        if kind == TaskType.SHORT_SCRIPT:
            raise ValueError("creation interrupted")
        return original(self, content_id, kind, inputs, **kwargs)

    monkeypatch.setattr(WorkflowService, "create_agent_task", fail)
    before = old_snapshot(runtime, ids["content"])
    assert http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN).status_code == 409
    assert before == old_snapshot(runtime, ids["content"])
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).business_status == ContentStatus.PACKAGED


@pytest.mark.parametrize("verdict", ["REVISE", "HUMAN", "READY", "BLOCKED"])
def test_packaged_revision_preserves_nonapproving_assistant_history(contract, verdict):
    runtime, http, ids, _, _, _ = contract
    with Session(runtime.engine) as session:
        task = runtime.workflow_service(session).create_agent_task(
            ids["content"], TaskType.SCRIPT_REVIEW_ASSIST, {"historical": True}, allow_multiple=True,
        )
        task.status, task.completed_at = TaskStatus.COMPLETED, utc_now()
        task.result_data = {"verdict": verdict}
        session.add(task)
        session.commit()
        task_id, before = task.id, stable_hash(task.model_dump(mode="json"))
    response = http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN)
    assert response.status_code == 200, response.text
    with Session(runtime.engine) as session:
        assert stable_hash(session.get(AgentTask, task_id).model_dump(mode="json")) == before


def test_normal_script_review_rejection_retains_previous_behavior(contract):
    runtime, http, ids, _, _, _ = contract
    cover_id, negative_id, _ = historical_materials(runtime, ids)
    with Session(runtime.engine) as session:
        content = session.get(Content, ids["content"])
        content.business_status = ContentStatus.SCRIPT_REVIEW
        session.add(content)
        session.commit()
    response = http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN)
    assert response.status_code == 200, response.text
    with Session(runtime.engine) as session:
        rewrite = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                       AgentTask.status == TaskStatus.PENDING)).one()
        assert "packaged_script_revision" not in rewrite.input_data and "previous_plan" not in rewrite.input_data
        assert session.get(AgentTask, cover_id).status == TaskStatus.CANCELLED
        assert session.get(AgentTask, negative_id).status == TaskStatus.CANCELLED


def test_revised_script_normal_approval_preserves_cover_and_discards_obsolete_qc(contract, monkeypatch):
    runtime, http, ids, calls, _, bundle = contract
    cover_id, negative_id, failed_id = historical_materials(runtime, ids)
    monkeypatch.setattr("glance_media.tts.kokoro.verify_kokoro_bundle", lambda _: deepcopy(bundle))
    assert http.post(endpoint(ids), json={"note": NOTE}, headers=ADMIN).status_code == 200
    with Session(runtime.engine) as session:
        rewrite = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                       AgentTask.status == TaskStatus.PENDING)).one()
        rewrite.status, rewrite.completed_at = TaskStatus.COMPLETED, utc_now()
        rewrite.result_data = {"full_narration": "主打共享专注工作舱。它的占地面积是2.5平方米。", "estimated_duration_sec": 30}
        prove_completed(session, runtime, rewrite)
        rewrite_id = rewrite.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, rewrite_id)
    with Session(runtime.engine) as session:
        qc = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCRIPT_QC,
                                                 AgentTask.status == TaskStatus.PENDING)).one()
        assert qc.input_data["upstream_task_ids"]["script"] == rewrite_id
        qc.status, qc.result_data, qc.completed_at = TaskStatus.COMPLETED, {"passed": True}, utc_now()
        prove_completed(session, runtime, qc)
        qc_id = qc.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, qc_id)
    with Session(runtime.engine) as session:
        assist = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCRIPT_REVIEW_ASSIST,
                                                     AgentTask.status == TaskStatus.PENDING)).one()
        assert assist.input_data["upstream_task_ids"]["script"] == rewrite_id
        assist.status, assist.result_data, assist.completed_at = TaskStatus.COMPLETED, {"verdict": "PASS"}, utc_now()
        prove_completed(session, runtime, assist)
    response = http.post(f"/api/v1/contents/{ids['content']}/approve-script", headers=ADMIN)
    assert response.status_code == 200, response.text
    assert len(calls) == 1 and calls[0][0] == "主打共享专注工作舱。它的占地面积是2.5平方米。"
    with Session(runtime.engine) as session:
        plan = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN,
                                                   AgentTask.status == TaskStatus.PENDING)).one()
        assert plan.input_data["upstream_task_ids"]["script"] == rewrite_id
        assert [row["asset_id"] for row in plan.input_data["previous_plan"]["segments"]] == ids["images"]
        assert set(ids["images"]) <= set(plan.input_data["allowed_asset_ids"])
        assert "qc_issues" not in plan.input_data and "previous_qc_task_id" not in plan.input_data
        assert plan.input_data["voice_generation"]["narration_sha256"]
        for task_id in (cover_id, negative_id, failed_id, ids["negative"], ids["recovery"]):
            assert session.get(AgentTask, task_id).status == TaskStatus.COMPLETED
        plan.status, plan.result_data, plan.completed_at = TaskStatus.COMPLETED, deepcopy(plan.input_data["previous_plan"]), utc_now()
        prove_completed(session, runtime, plan)
        plan_id = plan.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, plan_id)
    for kind in (TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC):
        with Session(runtime.engine) as session:
            qc = session.exec(select(AgentTask).where(AgentTask.task_type == kind, AgentTask.status == TaskStatus.PENDING)).one()
            assert qc.input_data["upstream_task_ids"]["visual_plan"] == plan_id
            if kind == TaskType.ASSET_QC:
                assert qc.input_data["full_generated_assets"] is True
                assert [row["artifact_id"] for row in qc.input_data["assets"]] == ids["images"]
            qc.status, qc.result_data, qc.completed_at = TaskStatus.COMPLETED, {"passed": True}, utc_now()
            prove_completed(session, runtime, qc)
            qc_id = qc.id
        pipeline.advance_after_agent(runtime.engine, runtime.settings, qc_id)
    with Session(runtime.engine) as session:
        assert session.get(Content, ids["content"]).current_stage == "render_ready"
        assert [row["asset_id"] for row in resolved_visual_plan(session, session.get(AgentTask, plan_id))["segments"]] == ids["images"]
