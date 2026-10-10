"""Saved publish prompts cannot remove the staged pre-click authorization boundary."""

import json
from pathlib import Path

import pytest
from sqlmodel import Session
from pydantic import SecretStr
from typer.testing import CliRunner

from glance_agent import cli
from glance_agent.settings import AgentSettings, PUBLISH_REQUIRED_CAPABILITIES
from glance_core.agent_roles import AgentRole, role_spec
from glance_core.models import TaskPrompt, TaskType
from glance_core.prompts import resolve_instructions
from test.business.test_discovery_http import make_runtime


@pytest.mark.parametrize("handsfree", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_publish_bundle_preserves_browser_checks_and_unique_intent(tmp_path, handsfree, custom):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            if custom:
                session.add(TaskPrompt(task_type=TaskType.DOUYIN_PUBLISH.value,
                                       instructions="旧模板：直接运行一键上传，不检查正文。"))
                session.commit()
            instructions = resolve_instructions(session, TaskType.DOUYIN_PUBLISH, handsfree=handsfree)
            assert "禁止运行一键发布 CLI" in instructions
            assert "完整正文" in instructions and "AI 内容声明" in instructions
            assert "实际预览" in instructions and "缺字" in instructions
            assert "register-publish-intent <task_id>" in instructions
            assert "click_authorized=true" in instructions
            assert instructions.index("先保存这些检查的截图证据") < instructions.index("register-publish-intent")
            assert "响应丢失" in instructions and "禁止再次上传或点击" in instructions
            if custom:
                assert "以下规则覆盖自定义提示词中冲突的执行方式" in instructions
    finally:
        runtime.close()


def test_publish_skill_and_registered_role_have_matching_execution_contract():
    root = Path(__file__).resolve().parents[2]
    skill = root / "packages/agent/skill/glance-publish-agent"
    spec = role_spec(AgentRole.PUBLISH)
    metadata = json.loads((skill / "role.json").read_text(encoding="utf-8"))
    assert metadata["skill_version"] == spec.skill_version == "1.3.0"
    assert "social-auto-upload" not in spec.required_host_capabilities
    assert tuple(PUBLISH_REQUIRED_CAPABILITIES) == spec.required_host_capabilities
    for filename in ("SKILL.md", "references/protocol.md"):
        instructions = (skill / filename).read_text(encoding="utf-8")
        assert "完整正文" in instructions
        assert "register-publish-intent" in instructions
        assert "禁止一键发布 CLI" in instructions


def test_browser_only_publish_capabilities_create_actual_launcher(tmp_path, monkeypatch):
    config = AgentSettings(token=SecretStr("isolated-token"), agent_id="isolated-publish",
                           role_id="publish", capabilities=list(role_spec(AgentRole.PUBLISH).required_host_capabilities))
    monkeypatch.setattr(cli, "settings", lambda: config)
    target = tmp_path / "publish.ps1"
    result = CliRunner().invoke(cli.app, ["create-launcher", "--output", str(target)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["status"] == "CREATED"
    assert target.is_file()
