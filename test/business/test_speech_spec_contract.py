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
HISTORICAL_AUDIO_NOTE = (
    "旧自定义音频规则：ASR低置信、静音/削波或硬项判断不足不能通过；格式化符号低置信直接判错。"
)
ASR_FORMATTING_PRIORITY = (
    "本节的ASR格式化/低置信判定仅在这类判定上优先于基础、自定义及历史提示中的冲突表述"
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


@pytest.mark.parametrize("handsfree", [False, True])
@pytest.mark.parametrize("custom", [False, True])
def test_content_qc_v3_distinguishes_asr_formatting_uncertainty_from_unresolved_semantics(
    tmp_path, handsfree, custom
):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            if custom:
                session.add(
                    TaskPrompt(
                        task_type=TaskType.CONTENT_QC.value,
                        instructions=HISTORICAL_AUDIO_NOTE,
                    )
                )
                session.commit()
            text = resolve_instructions(
                session,
                TaskType.CONTENT_QC,
                handsfree=handsfree,
                schema_version="3",
            )
            if custom:
                assert text.startswith(HISTORICAL_AUDIO_NOTE)
                assert session.get(TaskPrompt, TaskType.CONTENT_QC.value).instructions == HISTORICAL_AUDIO_NOTE
                assert text.index(HISTORICAL_AUDIO_NOTE) < text.index(ASR_FORMATTING_PRIORITY)
    finally:
        runtime.close()

    assert "关键读音低置信且无法凭独立证据消歧" in text
    assert "audio 必须 false 并定位缺口" in text
    assert ASR_FORMATTING_PRIORITY in text
    assert "其他硬项要求不变，九项硬项仍须逐项满足" in text
    assert "ASR低置信只用于定位需核验的关键语义时段，不能直接判为错读" in text
    assert "“×”“-”等格式化符号token的低置信不能单独代表实际读音错误" in text
    assert "同系列Whisper相互一致可作为补充线索" in text
    assert "本身不能证明相应连接词" in text
    assert "已被听清或全部音频硬项通过" in text
    assert "不得把" in text
    assert "字幕或TTS原文当作实际读音证据或回填低置信ASR词" in text
    assert "关键关系低置信且无法凭独立证据消歧时，audio必须false并定位时段" in text
    assert "任何关键关系缺乏充分证据时也必须false" in text
    assert "静音/削波、" in text
    assert "其他音频硬项或任一九项硬项" in text
    assert "证据不足/失败仍必须false" in text
    assert "九硬项仍全部需要" in text
    if not custom:
        assert "ASR低置信、静音/削波或硬项判断不足不能通过。" not in text


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
