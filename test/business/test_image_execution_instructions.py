"""Image dispatch includes honest local preparation even with a saved legacy prompt."""

import json

import pytest
from sqlmodel import Session

from glance_core.models import TaskPrompt, TaskType
from glance_core.prompts import resolve_instructions
from test.business.test_discovery_http import make_runtime


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("handsfree", [False, True])
def test_image_instructions_preserve_frozen_preparation_and_independent_qc(tmp_path, custom, handsfree):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            if custom:
                session.add(TaskPrompt(task_type=TaskType.ASSET_GENERATE_IMAGE.value,
                                       instructions="旧尺寸模板：生成后直接上传。"))
                session.commit()
            text = resolve_instructions(session, TaskType.ASSET_GENERATE_IMAGE, handsfree=handsfree)
            assert "prepare-image <task_id>" in text
            assert "租约工作区之外的持久证据目录" in text
            assert "正常提交、失败上报或工作区清理后仍保留" in text
            assert "在任务工作区保留原图" not in text
            assert "宿主已明确授权" in text
            assert "不覆盖原图或修改冻结输入" in text
            assert "尺寸放大不增加原始细节" in text
            assert "独立素材 QC" in text
            assert "style_notes" in text and "哈希和准备方法" in text
            if custom:
                assert "固定图片执行边界" in text
    finally:
        runtime.close()


def test_image_skill_and_server_role_share_durable_evidence_patch_version():
    from glance_agent.cli import bundled_role_skill_path
    from glance_core.agent_roles import role_spec

    _, directory = bundled_role_skill_path("image")
    metadata = json.loads((directory / "role.json").read_text(encoding="utf-8"))
    text = (directory / "SKILL.md").read_text(encoding="utf-8")
    assert metadata["skill_version"] == role_spec("image").skill_version == "1.2.1"
    assert "版本 `1.2.1`" in text
    assert "租约工作区之外的持久证据目录" in text
    assert "正常提交、失败上报或工作区清理后仍保留" in text
