"""URL-only research starts normally; declared required files still fail closed."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from glance_agent import cli
from glance_core.agent_roles import role_spec
from glance_core.models import AgentTask, TaskStatus, TaskType, RightsStatus
from glance_core.task_instructions import render_dispatch_task_prompt
from glance_core.task_service import stable_hash
from sqlmodel import Session, select

from test.business.test_agent_failure_recovery import registered_client
from test.business.test_agent_launcher_staging import staged as staged
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN, make_runtime


OFFICIAL_URL = "https://www.raspberrypi.com/products/raspberry-pi-5/"


def select_official_research(http, runtime):
    with Session(runtime.engine) as session:
        candidate, _ = runtime.workflow_service(session).ingest_candidate(
            source_type="agent", source_id="raspberry-pi-official",
            source_url=OFFICIAL_URL, original_title="Raspberry Pi 5",
            rights_status=RightsStatus.RESEARCH_ONLY,
        )
        candidate_id = candidate.id
    response = http.post(f"/api/v1/candidates/{candidate_id}/select?production_mode=image_only", headers=ADMIN)
    assert response.status_code == 200, response.text
    content_id = response.json()["id"]
    with Session(runtime.engine) as session:
        task = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
                                                  AgentTask.task_type == TaskType.CONTENT_RESEARCH)).one()
        return content_id, task.id


def test_new_url_only_research_dispatch_bundle_and_sdk_require_no_undeclared_context(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        content_id, task_id = select_official_research(http, runtime)
        with Session(runtime.engine) as session:
            original = session.get(AgentTask, task_id).model_dump(mode="json")
        worker = registered_client(http, runtime, "research")
        dispatched = worker.dispatch(wait=0, task_id=task_id, task_type="content_research",
                                     launcher="C:/isolated/research.ps1")
        assert dispatched["should_run"] is True
        assert dispatched["task_id"] == task_id
        assert "未声明或空清单不要求 context/ 目录" in dispatched["prompt"]
        assert "无预置素材不属于 CONTEXT_MISSING" in dispatched["prompt"]
        worker.confirm_claim(task_id, dispatched["lease_token"], worker.settings.agent_id)
        bundle = worker.bundle(dispatched["bundle_url"], dispatched["lease_token"])
        assert bundle["input"]["candidate"]["source_url"] == OFFICIAL_URL
        assert bundle["input"]["production_mode"] == "image_only"
        assert not bundle["input"]["transcript"].get("segments")
        assert bundle["context_files"] == bundle["context_manifest"] == []
        assert "`source_images` 是本次联网研究应收集的输出" in bundle["instructions"]
        workspace = tmp_path / "worker" / task_id
        workspace.mkdir(parents=True)
        cli._download_context(worker, bundle, dispatched["lease_token"], workspace)
        assert cli._verify_context(worker, bundle, dispatched["lease_token"], workspace) == []
        assert not (workspace / "context").exists()
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, task_id)
            assert task.content_id == content_id and task.status == TaskStatus.LEASED
            assert task.input_data == original["input_data"] and task.input_hash == original["input_hash"]
            assert task.result_data == original["result_data"] is None


@pytest.mark.parametrize("kind", [TaskType.CONTENT_RESEARCH, TaskType.VISUAL_PLAN_QC, TaskType.CONTENT_QC])
def test_explicit_nonempty_required_context_still_rejects_missing_files(tmp_path, kind):
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        content_id, task_id = select_official_research(http, runtime)
        with Session(runtime.engine) as session:
            original = session.get(AgentTask, task_id)
            inputs = {**original.input_data, "context_files": ["required-real-frame.png"]}
            fresh = runtime.workflow_service(session).create_agent_task(
                content_id, kind, inputs, allow_multiple=True, priority=100)
            fresh_id, frozen_hash = fresh.id, fresh.input_hash
            assert frozen_hash == stable_hash(inputs)
        role = role_spec("research" if kind == TaskType.CONTENT_RESEARCH else
                         "visual" if kind == TaskType.VISUAL_PLAN_QC else "script")
        worker = registered_client(http, runtime, role.role.value)
        dispatched = worker.dispatch(wait=0, task_id=fresh_id, task_type=kind.value,
                                     launcher="C:/isolated/worker.ps1")
        worker.confirm_claim(fresh_id, dispatched["lease_token"], worker.settings.agent_id)
        response = http.get(dispatched["bundle_url"], headers={"Authorization": f"Bearer {dispatched['lease_token']}"})
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "ENVIRONMENT_MISSING_CONTEXT"
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, fresh_id)
            assert task.input_hash == frozen_hash and task.input_data == inputs
            assert task.status == TaskStatus.FAILED


def test_dispatch_keeps_declared_visual_and_content_qc_files_mandatory():
    for kind in (TaskType.CONTENT_RESEARCH, TaskType.VISUAL_PLAN_QC, TaskType.CONTENT_QC):
        prompt = render_dispatch_task_prompt(task_id="test", task_type=kind.value, skill_name="test-skill",
                                             workspace_dir="C:/isolated/task", launcher="C:/isolated/worker.ps1")
        assert "仅核验本任务明确声明的非空 context_files/context_manifest" in prompt
        assert "视觉/成片QC等已声明的必需素材缺文件或哈希不符仍须 fail" in prompt
        assert "缺少必需输入时只用 `fail`" in prompt


def test_actual_staged_research_skill_matches_role_version_and_declared_context_rule(staged):
    stage, _ = staged
    manifest = json.loads((stage / "mpt-codex-research.json").read_text(encoding="utf-8"))
    skill = Path(manifest["skill_path"])
    role = json.loads((skill / "role.json").read_text(encoding="utf-8"))
    assert manifest["skill_version"] == role["skill_version"] == role_spec("research").skill_version == "1.2.0"
    assert "不要求 `context/` 目录" in (skill / "SKILL.md").read_text(encoding="utf-8")
    protocol = (skill / "references/protocol.md").read_text(encoding="utf-8")
    assert "仅校验任务包明确声明的非空" in protocol
    assert "视觉/成片 QC 等任务已声明的必需素材不适用免检" in protocol
    assert "不能因此上报 `CONTEXT_MISSING`" in protocol
