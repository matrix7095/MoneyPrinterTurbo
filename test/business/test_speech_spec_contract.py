"""Specification speech policy reaches new contracts without rewriting history."""

from copy import deepcopy

import pytest
from glance_core.models import RuntimeSetting, TaskPrompt, TaskType
from glance_core.prompts import resolve_instructions
from glance_core.task_instructions import _SCRIPT_FACT_FIDELITY_RULE
from glance_core.workflow import _script_rewrite_note
from sqlmodel import Session

from test.business import test_script_assist_gate
from test.business.test_discovery_http import make_runtime


script_review_contract = test_script_assist_gate.script_review_contract

HISTORICAL_NOTE = (
    "旧 must_change：full_narration 必须原样使用1920×1080、60–80英寸，禁止中文连接词。"
)
STAGES = [
    (TaskType.SHORT_SCRIPT, "1"),
    (TaskType.SCRIPT_QC, "1"),
    (TaskType.SCRIPT_REVIEW_ASSIST, "1"),
    (TaskType.CONTENT_QC, "1"),
    (TaskType.CONTENT_QC, "2"),
    (TaskType.CONTENT_QC, "3"),
    (TaskType.FINAL_REVIEW_ASSIST, "1"),
    (TaskType.RECOVERY, "1"),
]


def assert_bounded_speech_policy(text):
    for requirement in (
        "仅 `full_narration` 及明确重复同一句口播的节点",
        "1920×1080", "1920乘以1080", "60–80英寸", "60到80英寸",
        "阿拉伯数字、全部数值、顺序、关系和单位",
        "标称", "暗环境",
        "研究、结构化事实及技术展示字段仍须保留规范符号",
        "不得中文数字化、四舍五入、改单位、扩缩范围或猜测不确定规格",
        "48MP", "字幕随实际 `full_narration`",
        "历史 reviewer_note / must_change / must_preserve",
        "本规则优先", "不修改历史任务、输入或结果",
    ):
        assert requirement in text


@pytest.mark.parametrize("task_type,schema_version", STAGES)
@pytest.mark.parametrize("handsfree", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_resolved_spec_policy_is_consistent_across_modes_and_legacy_overrides(
    tmp_path, task_type, schema_version, handsfree, custom
):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            if custom:
                session.add(TaskPrompt(task_type=task_type.value, instructions=HISTORICAL_NOTE))
                session.commit()
            text = resolve_instructions(
                session, task_type, handsfree=handsfree, schema_version=schema_version
            )
            assert_bounded_speech_policy(text)
            assert text.count(_SCRIPT_FACT_FIDELITY_RULE.strip()) == 1
            if custom:
                assert text.startswith(HISTORICAL_NOTE)
                assert text.index(_SCRIPT_FACT_FIDELITY_RULE.strip()) > len(HISTORICAL_NOTE)
                assert session.get(TaskPrompt, task_type.value).instructions == HISTORICAL_NOTE
            assert "在所有脚本字段中必须保留研究中的原样写法" not in text
            assert "不得泛化忽略范围、乘号" not in text
    finally:
        runtime.close()


def test_rewrite_note_overrides_symbol_only_history_without_erasing_it():
    text = _script_rewrite_note(HISTORICAL_NOTE)
    assert_bounded_speech_policy(text)
    assert text.endswith(HISTORICAL_NOTE)
    assert "仅修改 issue 相关字段" in text
    assert "仍须完整重新 QC" in text


@pytest.mark.parametrize("handsfree", [False, True])
def test_real_rewrite_task_injects_policy_and_keeps_previous_subject_immutable(
    script_review_contract, handsfree
):
    runtime, session, content, research, script, _, _, _ = script_review_contract
    session.add(RuntimeSetting(key="handsfree_enabled", value=str(handsfree).lower()))
    session.commit()
    original = deepcopy((script.input_data, script.result_data, script.instructions))
    rewrite = runtime.workflow_service(session)._queue_script_rewrite(
        content, research_task=research, scene_tasks=[],
        previous_script=script.result_data, reviewer_note=HISTORICAL_NOTE,
        reason="spec-pronunciation", priority=100,
    )
    assert_bounded_speech_policy(rewrite.instructions)
    assert_bounded_speech_policy(rewrite.input_data["reviewer_note"])
    assert rewrite.input_data["reviewer_note"].endswith(HISTORICAL_NOTE)
    assert rewrite.input_data["previous_script"] == original[1]
    session.refresh(script)
    assert (script.input_data, script.result_data, script.instructions) == original
