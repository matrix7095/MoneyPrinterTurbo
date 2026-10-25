"""MPT startup settings survive task creation, leased bundles and recovery context."""

import pytest
from fastapi.testclient import TestClient
from glance_core.models import AgentTask, AuditEvent, RuntimeSetting, TaskPrompt, TaskType
from glance_core.prompts import _HANDSFREE_STAGE_FACT_RULE
from glance_core.task_service import stable_hash
from sqlmodel import Session, select

from app.business.runtime import BusinessRuntime, settings_from_config
from test.business.test_content_http import RESEARCH, client_for, select_and_claim
from test.business.test_discovery_http import agent_client, issue_and_claim, make_app
from test.business.test_semantic_http import SCRIPT, semantic_app


@pytest.mark.parametrize("enabled,override", [(True, None), (True, False), (False, None), (False, True)])
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("passed", [False, True])
def test_startup_mode_reaches_live_script_and_recovery_contracts(
    tmp_path, monkeypatch, enabled, override, custom, passed
):
    monkeypatch.setenv("GLANCE_HANDSFREE_ENABLED", "false")
    settings = settings_from_config({"business": {
        "data_dir": str(tmp_path), "admin_token": "test-admin",
        "bootstrap_agent_token": "test-root", "handsfree_enabled": enabled,
    }}, tmp_path)
    runtime = BusinessRuntime(settings)
    effective = enabled if override is None else override
    with TestClient(semantic_app(runtime)) as http:
        with Session(runtime.engine) as session:
            assert session.get(RuntimeSetting, "handsfree_enabled") is None
            if override is not None:
                session.add(RuntimeSetting(key="handsfree_enabled", value=str(override).lower()))
            if custom:
                for kind in (TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC, TaskType.SCRIPT_REVIEW_ASSIST):
                    session.add(TaskPrompt(task_type=kind.value, instructions="旧合同：全部使用条件只能留后台。"))
            session.commit()
        content_id, researcher, claim = select_and_claim(http, runtime)
        research_bundle = researcher.bundle(claim["bundle_url"], claim["lease_token"])
        assert "私有许可证据：用户针对指定素材" in research_bundle["instructions"]
        assert researcher.complete(claim["task_id"], claim["lease_token"], "research-worker", RESEARCH)["advanced"] == "CONSUMED"
        script_agent = client_for(http, "script")
        task_ids = []
        for result in (SCRIPT, {"passed": passed, "issues": [] if passed else [
            {"severity": "error", "field": "facts", "message": "必要条件缺失"},
        ]}):
            claim = script_agent.claim()
            bundle = script_agent.bundle(claim["bundle_url"], claim["lease_token"])
            assert "私有许可证据：用户针对指定素材" in bundle["instructions"]
            assert (_HANDSFREE_STAGE_FACT_RULE in bundle["instructions"]) is effective
            task_ids.append(claim["task_id"])
            assert script_agent.complete(claim["task_id"], claim["lease_token"], "script-worker", result)["advanced"] == "CONSUMED"
        script_id, qc_id = task_ids
        with Session(runtime.engine) as session:
            for task_id in (script_id, qc_id):
                task = session.get(AgentTask, task_id)
                assert (_HANDSFREE_STAGE_FACT_RULE in task.instructions) is effective
                receipts = session.exec(select(AuditEvent).where(
                    AuditEvent.entity_id == task_id, AuditEvent.action == "bundle_served",
                )).all()
                assert receipts
                assert all(row.metadata_json["served_prompt_hash"] == stable_hash(task.instructions)
                           for row in receipts)
            kind = TaskType.SCRIPT_REVIEW_ASSIST if passed else TaskType.RECOVERY
            task = session.exec(select(AgentTask).where(
                AgentTask.content_id == content_id, AgentTask.task_type == kind,
            )).one()
            task_id, frozen_input, frozen_instructions = task.id, task.input_data, task.instructions
        assistant = client_for(http, "assistant")
        claim = assistant.claim()
        assert claim["task_id"] == task_id
        bundle = assistant.bundle(claim["bundle_url"], claim["lease_token"])
        assert bundle["instructions"] == frozen_instructions
        assert "私有许可证据：用户针对指定素材" in bundle["instructions"]
        if passed:
            assert (_HANDSFREE_STAGE_FACT_RULE in bundle["instructions"]) is effective
        else:
            contract = bundle["input"]["current_input"]["current_contract"]
            assert contract["source_task_id"] == qc_id
            assert contract["source_task_type"] == "script_qc"
            assert (_HANDSFREE_STAGE_FACT_RULE in contract["instructions"]) is effective
            with Session(runtime.engine) as session:
                assert contract["instructions"] == session.get(AgentTask, qc_id).instructions
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert task.instructions == frozen_instructions and task.input_data == frozen_input
            assert (session.get(RuntimeSetting, "handsfree_enabled") is None) is (override is None)


@pytest.mark.parametrize("enabled,override", [(True, None), (True, False), (False, None), (False, True)])
@pytest.mark.parametrize("custom", [False, True])
def test_collector_source_creation_matches_actual_leased_http_bundle(
    tmp_path, monkeypatch, enabled, override, custom
):
    monkeypatch.setenv("GLANCE_HANDSFREE_ENABLED", "false")
    settings = settings_from_config({"business": {
        "data_dir": str(tmp_path), "admin_token": "test-admin",
        "bootstrap_agent_token": "test-root", "handsfree_enabled": enabled,
    }}, tmp_path)
    runtime = BusinessRuntime(settings)
    effective = enabled if override is None else override
    with TestClient(make_app(runtime)) as http:
        with Session(runtime.engine) as session:
            assert session.get(RuntimeSetting, "handsfree_enabled") is None
            if override is not None:
                session.add(RuntimeSetting(key="handsfree_enabled", value=str(override).lower()))
            if custom:
                session.add(TaskPrompt(task_type=TaskType.SOURCE_DISCOVERY.value, instructions="旧来源合同。"))
            session.commit()
        agent = agent_client(http)
        claim = issue_and_claim(http, agent)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claim["task_id"])
            snapshot, frozen_input = task.instructions, task.input_data
        assert ("## Handsfree 无人值守来源规则" in snapshot) is effective
        bundle = agent.bundle(claim["bundle_url"], claim["lease_token"])
        assert bundle["instructions"] == snapshot
        assert bundle["task"]["prompt_version"] == f"live-{stable_hash(snapshot)[:12]}"
        assert ("allowed_production_modes" in bundle["input"]) is effective
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claim["task_id"])
            assert task.instructions == snapshot and task.input_data == frozen_input
            receipt = session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == task.id, AuditEvent.action == "bundle_served",
            )).one()
            assert receipt.metadata_json["served_prompt_hash"] == stable_hash(snapshot)
