"""MPT's existing identity layer reaches frozen plans and genuine leased contracts."""

from copy import deepcopy
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from glance_core.models import AgentTask, AuditEvent, ContentStatus, RuntimeSetting, TaskPrompt, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from sqlmodel import select

from app.business import pipeline
from app.business.runtime import AGENT_CONSUMED
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_deployment_http import app_for
from test.business.test_script_assist_gate import script_review_contract as script_review_contract
from test.business.test_visual_plan_materialization import record_accepted_completion


HEADING = "## MPT 开场身份叠加执行边界"
KEY = "opening_identity_overlay"
NARRATION = "自平衡外骨骼腾出双手。受训陪护须持续在场，伸手可及。"


def new_plan(contract, *, identity="Eve", duration=30, flow=None):
    runtime, session, content, research, _, _, _, completed = contract
    # The previous fixture revision must predate this one even on coarse Windows clocks.
    for old in contract[4:6]:
        old.created_at -= timedelta(seconds=1)
        session.add(old)
    session.commit()
    script = completed(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}, "revision": identity}, {
        "opening": {"product_identity": identity}, "full_narration": NARRATION,
        "title_options": ["Eve 自平衡外骨骼"],
    })
    qc = completed(TaskType.SCRIPT_QC, {
        "upstream_task_ids": {"script": script.id, "research": research.id},
    }, {"passed": True, "issues": []})
    completed(TaskType.SCRIPT_REVIEW_ASSIST, {
        "upstream_task_ids": {"script": script.id, "script_qc": qc.id, "research": research.id},
        "script": script.result_data, "script_qc": qc.result_data, "research": research.result_data,
    }, {"verdict": "APPROVE", "confidence": "high", "summary": "Verified identity",
        "reasons": ["Source-backed identity"], "issues": [], "checklist": []})
    flow = flow or runtime.workflow_service(session)
    plan = flow.create_agent_task(content.id, TaskType.VISUAL_PLAN, {
        "upstream_task_ids": {"script": script.id}, "voice_duration_sec": duration,
        "production_mode": "image_only", "allowed_layouts": ["image_card"],
        "asset_image_enabled": True, "image_only_gen_budget": 8,
        "allowed_asset_ids": [], "context_files": [], "scenes": [],
    }, allow_multiple=True)
    content.current_stage, content.business_status = "visual_plan", ContentStatus.WAITING_AGENT
    session.add(content)
    session.commit()
    return plan, script


def finish_plan(contract, plan):
    runtime, session, *_ = contract
    record_accepted_completion(session, runtime, plan, {"segments": [{
        "narration_start_sec": index * 6, "narration_end_sec": (index + 1) * 6,
        "layout": "image_card", "purpose": "原创概念说明", "motion": "ken_burns_in",
        "asset_request": {"information": "人物与近身陪护关系概念", "style_direction": "纸艺插画",
                          "subject": "Original paper cut full people, no text or real product likeness"},
    } for index in range(5)]})
    session.commit()
    assert runtime.consume_agent(plan.id)
    return session.exec(select(AgentTask).where(
        AgentTask.content_id == plan.content_id, AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
        AgentTask.status == TaskStatus.PENDING,
    )).one()


@pytest.mark.parametrize("phase", ["plan", "qc", "recovery"])
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_real_plan_qc_and_recovery_http_contracts(script_review_contract, tmp_path, phase, enabled, custom):
    runtime, session, content, *_ = script_review_contract
    session.add(RuntimeSetting(key="handsfree_enabled", value=str(enabled).lower()))
    if custom:
        for kind in (TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC, TaskType.RECOVERY):
            session.add(TaskPrompt(task_type=kind.value, instructions="旧合同只保证 Concept 标签。"))
    session.commit()
    plan, script = new_plan(script_review_contract)
    source = plan
    task = plan
    if phase != "plan":
        source = task = finish_plan(script_review_contract, plan)
    if phase == "recovery":
        record_accepted_completion(session, runtime, source, {"passed": False, "issues": [
            {"severity": "error", "field": "segments[0].purpose", "message": "身份执行证据缺口"},
        ]})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=source.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(source.result_data)}))
        session.commit()
        task = runtime.workflow_service(session).create_recovery_task(
            source, code="VISUAL_PLAN_QC_FAILED", message="身份执行证据缺口",
            failure_class="BUSINESS_QC", repair_target_task_id=plan.id,
        )
        assert task is not None
    frozen = deepcopy((task.input_data, task.input_hash, task.instructions, task.result_data))
    setting = session.get(RuntimeSetting, "handsfree_enabled")
    setting.value = str(not enabled).lower()
    session.add(setting)
    if custom:
        prompt = session.get(TaskPrompt, source.task_type.value)
        prompt.instructions = "新自定义合同仍未声明独立身份层。"
        session.add(prompt)
    session.commit()
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "assistant" if phase == "recovery" else "visual")
        claim = client.claim()
        assert claim["task_id"] == task.id
        response = http.get(claim["bundle_url"], headers={"Authorization": f"Bearer {claim['lease_token']}"})
        assert response.status_code == 200, response.text
        bundle = response.json()
        instructions = (bundle["input"]["current_input"]["current_contract"]["instructions"]
                        if phase == "recovery" else bundle["instructions"])
        assert HEADING in instructions
        context = bundle["input"]["current_input"] if phase == "recovery" else bundle["input"]
        overlay = context[KEY]
        assert overlay["script_task_id"] == script.id
        assert overlay["script_result_sha256"] == stable_hash(script.result_data)
        assert overlay["text"] == "Eve"
        assert (overlay["start_sec"], overlay["end_sec"]) == (0, 8)
        assert "不自动拼接中文类别或陪护条件" in instructions
        assert "最终成片必须实际核验" in instructions
        assert HEADING in task.instructions
    session.expire_all()
    task = session.get(AgentTask, task.id)
    assert (task.input_data, task.input_hash, task.instructions, task.result_data) == frozen
    assert KEY in session.get(AgentTask, plan.id).input_data
    assert pipeline.workflow(session, runtime.settings).opening_identity_overlay is True


@pytest.mark.parametrize("identity", ["", "  \t\n", "\x00\x01"])
def test_blank_identity_never_claims_visible_layer(script_review_contract, tmp_path, identity):
    runtime, session, *_ = script_review_contract
    plan, _ = new_plan(script_review_contract, identity=identity)
    assert KEY not in plan.input_data
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        assert KEY not in bundle["input"] and f"{KEY}_read_only" not in bundle["input"]
        assert "空白身份不生成身份层" in bundle["instructions"]


@pytest.mark.parametrize("duration,end", [(2.25, 2.25), (2.257, 2.25), (30, 8)])
def test_short_voice_interval_and_exact_identity_without_category(script_review_contract, duration, end):
    plan, script = new_plan(script_review_contract, duration=duration)
    assert plan.input_data[KEY] == {
        "renderer": "mpt", "source_field": "script.opening.product_identity",
        "script_task_id": script.id, "script_result_sha256": stable_hash(script.result_data),
        "text": "Eve", "start_sec": 0, "end_sec": end,
    }


def test_default_and_unknown_renderer_cannot_claim_capability(script_review_contract, monkeypatch):
    from glance_core.prompts import resolve_instructions
    from glance_core.workflow import WorkflowService
    from app.business import runtime as runtime_module

    runtime, session, *_ = script_review_contract
    settings = runtime.settings
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir,
                           lease_minutes=settings.agent_lease_minutes, base_settings=settings)
    plan, _ = new_plan(script_review_contract, flow=flow)
    assert KEY not in plan.input_data and HEADING not in plan.instructions
    assert HEADING not in resolve_instructions(session, TaskType.VISUAL_PLAN, base_settings=settings)
    monkeypatch.setattr(pipeline, "MediaExecutor", type("UnsupportedRenderer", (), {}))
    assert pipeline.workflow(session, settings).opening_identity_overlay is False
    monkeypatch.setattr(runtime_module, "MptMediaExecutor", pipeline.MediaExecutor)
    assert runtime.workflow_service(session).opening_identity_overlay is False


@pytest.mark.parametrize("phase", ["plan", "qc", "recovery"])
def test_legacy_tasks_get_only_read_only_execution_context(script_review_contract, tmp_path, phase):
    runtime, session, *_ = script_review_contract
    flow = runtime.workflow_service(session)
    flow.opening_identity_overlay = False
    plan, script = new_plan(script_review_contract, flow=flow)
    task = source = plan
    if phase != "plan":
        record_accepted_completion(session, runtime, plan, {"segments": []})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=plan.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(plan.result_data)}))
        session.commit()
        task = source = flow.create_agent_task(plan.content_id, TaskType.VISUAL_PLAN_QC, {
            "upstream_task_ids": {"visual_plan": plan.id, "script": script.id},
        }, allow_multiple=True)
    if phase == "recovery":
        record_accepted_completion(session, runtime, source, {"passed": False, "issues": [
            {"severity": "error", "field": "identity", "message": "Legacy mechanism absent"},
        ]})
        session.commit()
        task = flow.create_recovery_task(source, code="VISUAL_PLAN_QC_FAILED", message="Legacy contract",
                                         failure_class="BUSINESS_QC", repair_target_task_id=plan.id)
    assert KEY not in task.input_data
    frozen = deepcopy((task.input_data, task.input_hash, task.instructions, task.result_data))
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "assistant" if phase == "recovery" else "visual")
        claim = client.claim()
        assert claim["task_id"] == task.id
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        context = bundle["input"]["current_input"] if phase == "recovery" else bundle["input"]
        assert KEY not in context
        assert context[f"{KEY}_read_only"]["script_task_id"] == script.id
        assert context[f"{KEY}_read_only"]["text"] == "Eve"
    session.expire_all()
    task = session.get(AgentTask, task.id)
    assert (task.input_data, task.input_hash, task.instructions, task.result_data) == frozen


@pytest.mark.parametrize("mutation", ["script_ref", "plan_hash", "plan_content", "script_result"])
def test_visual_qc_never_inherits_unproven_identity(script_review_contract, mutation):
    runtime, session, _, _, _, _, _, completed = script_review_contract
    plan, script = new_plan(script_review_contract)
    record_accepted_completion(session, runtime, plan, {"segments": []})
    session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                           entity_id=plan.id, action=AGENT_CONSUMED,
                           metadata_json={"result_hash": stable_hash(plan.result_data)}))
    refs = {"visual_plan": plan.id, "script": script.id}
    if mutation == "script_ref":
        other = completed(TaskType.SHORT_SCRIPT, {"revision": "another"}, {
            "opening": {"product_identity": "Other Product"}, "full_narration": "另一款产品。",
        })
        refs["script"] = other.id
    elif mutation == "plan_hash":
        plan.input_data = {**plan.input_data, "voice_duration_sec": 31}
        session.add(plan)
    elif mutation == "plan_content":
        plan.content_id = "content_another"
        session.add(plan)
    else:
        script.result_data = {**script.result_data, "opening": {"product_identity": "Changed"}}
        session.add(script)
    session.commit()
    qc = runtime.workflow_service(session).create_agent_task(script.content_id, TaskType.VISUAL_PLAN_QC, {
        "upstream_task_ids": refs, KEY: {"text": "Invented identity"},
    }, allow_multiple=True)
    assert KEY not in qc.input_data


def test_changed_frozen_script_fails_actual_serve_without_replacing_identity(script_review_contract, tmp_path):
    runtime, session, *_ = script_review_contract
    plan, script = new_plan(script_review_contract)
    frozen = deepcopy((plan.input_data, plan.input_hash, plan.result_data))
    script.result_data = {**script.result_data, "opening": {"product_identity": "Other Product"}}
    session.add(script)
    session.commit()
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        response = http.get(claim["bundle_url"], headers={"Authorization": f"Bearer {claim['lease_token']}"})
        assert response.status_code == 409
        assert response.json()["detail"] == "Opening identity execution evidence changed"
    session.expire_all()
    plan = session.get(AgentTask, plan.id)
    assert (plan.input_data, plan.input_hash, plan.result_data) == frozen
