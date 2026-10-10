"""Real Agent HTTP submission/consumption followed by legacy result projections."""

import pytest
from fastapi.testclient import TestClient
from glance_core.models import AgentTask, AuditEvent, TaskType
from sqlmodel import Session, select

from app.business.runtime import AGENT_CONSUMED
from app.controllers.v1.llm import router as semantic_router
from test.business.test_content_http import (
    RESEARCH,
    client_for,
    content_app,
    select_and_claim,
)
from test.business.test_discovery_http import ADMIN, make_runtime

SCRIPT = {
    "title_options": ["Project A 的功能设计", "看看 Project A 的设计", "Project A 功能介绍"],
    "hook_type": "contrast", "bgm_mood": "none",
    "retention_beats": [{"at_sec": second, "beat": "展示产品功能"} for second in (0, 5, 12, 19, 26)],
    "cta": "你会怎么使用它？", "cover_title": "产品设计看这里",
    "cover_visual_brief": "展示产品主体", "body": "介绍产品设计。", "verdict": "功能清晰",
    "full_narration": "Project A 展示了产品的功能设计。你会怎么使用它？",
    "estimated_duration_sec": 30, "claims_used": ["claim-1"],
    "visual_requirements": [
        {"purpose": "功能展示", "keywords": ["product demo", "design"], "preferred_source": "source"},
        {"purpose": "产品主体", "keywords": ["product detail", "product demo"], "preferred_source": "source"},
    ],
}


def semantic_app(runtime):
    app = content_app(runtime)
    app.include_router(semantic_router)
    return app


def request(http, content_id, endpoint="scripts", **extra):
    return http.post(f"/api/v1/{endpoint}", headers=ADMIN, json={"content_id": content_id, **extra})


def complete_script(http, runtime, *, passed=True, script_result=None):
    content_id, researcher, research = select_and_claim(http, runtime)
    assert researcher.complete(research["task_id"], research["lease_token"], "research-worker", RESEARCH)["advanced"] == "CONSUMED"
    agent = client_for(http, "script")
    script = agent.claim()
    assert script["task_type"] == "short_script"
    assert agent.complete(script["task_id"], script["lease_token"], "script-worker", script_result or SCRIPT)["advanced"] == "CONSUMED"
    pending = request(http, content_id)
    assert pending.status_code == 202
    assert "script_qc" in pending.json()["data"]["required_tasks"]
    qc = agent.claim()
    assert qc["task_type"] == "script_qc"
    result = {"passed": passed, "issues": [] if passed else [{"severity": "error", "field": "facts", "message": "需要修正"}]}
    assert agent.complete(qc["task_id"], qc["lease_token"], "script-worker", result)["advanced"] == "CONSUMED"
    return content_id, research["task_id"], script["task_id"], qc["task_id"]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("MPT_UPSTREAM_COMPATIBILITY", raising=False)
    return make_runtime(tmp_path)


def test_independent_auth_and_explicit_content_context(runtime):
    with TestClient(semantic_app(runtime)) as http:
        for endpoint in ("scripts", "terms", "social-metadata"):
            assert "202" in http.get("/openapi.json").json()["paths"][f"/api/v1/{endpoint}"]["post"]["responses"]
            assert http.post(f"/api/v1/{endpoint}", json={}).status_code == 401
            assert http.post(f"/api/v1/{endpoint}", headers=ADMIN, json={}).status_code == 422
            assert request(http, "missing", endpoint).status_code == 404


def test_pending_returns_durable_ids_without_duplicate_tasks(runtime):
    with TestClient(semantic_app(runtime)) as http:
        content_id, _, research = select_and_claim(http, runtime)
        first = request(http, content_id)
        second = request(http, content_id)
        assert first.status_code == second.status_code == 202
        assert first.json() == second.json()
        assert first.json()["data"]["task_ids"] == {"content_research": research["task_id"]}
        with Session(runtime.engine) as session:
            assert len(session.exec(select(AgentTask)).all()) == 1


def test_real_consumed_script_terms_and_metadata_survive_restart(runtime, tmp_path):
    with TestClient(semantic_app(runtime)) as http:
        content_id, research_id, script_id, _ = complete_script(http, runtime)
        script = request(http, content_id)
        assert script.status_code == 200
        assert script.json()["data"]["video_script"] == SCRIPT["full_narration"]
        assert script.json()["data"]["task_ids"]["short_script"] == script_id
        terms = request(http, content_id, "terms", amount=3, match_materials_to_script=True)
        assert terms.status_code == 200
        assert terms.json()["data"]["video_terms"] == ["product demo", "design", "product detail"]
        assert terms.json()["data"]["result_origin"] == "short_script.visual_requirements"
        assert request(http, content_id, "social-metadata").status_code == 202
        # Exercise the existing typed task/consumer, not a fake synchronous LLM string.
        with Session(runtime.engine) as session:
            task = runtime.workflow_service(session).create_agent_task(content_id, TaskType.PUBLISH_PREPARE, {
                "script": session.get(AgentTask, script_id).result_data,
                "research": session.get(AgentTask, research_id).result_data,
            })
            metadata_id = task.id
        agent = client_for(http, "script")
        claimed = agent.claim()
        assert claimed["task_id"] == metadata_id
        metadata = {"title": "产品功能设计", "description": "介绍产品的功能设计", "tags": ["产品", "设计"]}
        assert agent.complete(metadata_id, claimed["lease_token"], "script-worker", metadata)["advanced"] == "CONSUMED"
        result = request(http, content_id, "social-metadata")
        assert result.status_code == 200
        assert result.json()["data"]["caption"] == metadata["description"]
        assert result.json()["data"]["hashtags"] == metadata["tags"]
    restarted = make_runtime(tmp_path)
    with TestClient(semantic_app(restarted)) as http:
        assert request(http, content_id).json()["data"]["video_script"] == SCRIPT["full_narration"]
        assert request(http, content_id, "social-metadata").json() == result.json()


def test_failed_qc_and_empty_keywords_have_concrete_refusals(runtime):
    with TestClient(semantic_app(runtime)) as http:
        content_id, *_ = complete_script(http, runtime, passed=False)
        assert request(http, content_id).json()["detail"]["code"] == "SCRIPT_QC_FAILED"
        assert request(http, content_id, "terms").status_code == 409


def test_completed_approved_script_without_keywords_is_not_pending(runtime):
    with TestClient(semantic_app(runtime)) as http:
        content_id, *_ = complete_script(http, runtime, script_result={**SCRIPT, "visual_requirements": []})
        result = request(http, content_id, "terms")
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == "TERMS_UNAVAILABLE"


def test_newer_pending_script_hides_old_accepted_result(runtime):
    with TestClient(semantic_app(runtime)) as http:
        content_id, research_id, *_ = complete_script(http, runtime)
        with Session(runtime.engine) as session:
            new = runtime.workflow_service(session).create_agent_task(content_id, TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research_id}}, allow_multiple=True)
            new_id = new.id
        result = request(http, content_id)
        assert result.status_code == 202
        assert result.json()["data"]["task_ids"]["short_script"] == new_id
        assert "video_script" not in result.json()["data"]


@pytest.mark.parametrize("fault,code", [("lineage", "INPUT_CHANGED"), ("input", "INPUT_CHANGED"), ("result", "RESULT_CHANGED")])
def test_stale_lineage_and_changed_consumed_result_fail_closed(runtime, fault, code):
    with TestClient(semantic_app(runtime)) as http:
        content_id, _, script_id, qc_id = complete_script(http, runtime)
        with Session(runtime.engine) as session:
            if fault == "lineage":
                task = session.get(AgentTask, qc_id)
                task.input_data = {"upstream_task_ids": {"script": "old", "research": "old"}}
            elif fault == "input":
                task = session.get(AgentTask, qc_id)
                task.input_data = {**task.input_data, "min_video_seconds": 999}
            else:
                task = session.get(AgentTask, script_id)
                task.result_data = {**task.result_data, "body": "changed"}
            session.add(task)
            session.commit()
        result = request(http, content_id)
        assert result.status_code == 409
        assert result.json()["detail"]["code"] == code


def test_accepted_without_consumption_marker_remains_pending(runtime):
    with TestClient(semantic_app(runtime)) as http:
        content_id, _, _, qc_id = complete_script(http, runtime)
        with Session(runtime.engine) as session:
            marker = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc_id, AuditEvent.action == AGENT_CONSUMED)).one()
            session.delete(marker)
            session.commit()
        assert request(http, content_id).status_code == 202
        runtime.consume_agent(qc_id)
        assert request(http, content_id).status_code == 200
