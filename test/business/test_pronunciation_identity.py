"""Shared identity policy and genuine leased manual script/QC HTTP bundles."""

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from glance_core.models import (
    AgentTask,
    AuditEvent,
    RuntimeSetting,
    TaskPrompt,
    TaskType,
)
from glance_core.prompts import (
    _HANDSFREE_STAGE_FACT_RULE,
    _PRONUNCIATION_IDENTITY_RULE,
    resolve_instructions,
)
from glance_core.runtime_config import effective_settings
from glance_core.settings import Settings
from glance_core.task_instructions import _SCRIPT_FACT_FIDELITY_RULE
from glance_core.task_schemas import ShortScriptResult
from glance_core.task_service import stable_hash
from pydantic import ValidationError
from sqlmodel import Session, select

from app.business.quarantine import CONTENT_QUARANTINE, TASK_QUARANTINE
from app.business.runtime import OWNED
from test.business.test_content_http import RESEARCH, client_for, select_and_claim
from test.business.test_discovery_http import ADMIN, make_runtime
from test.business.test_semantic_http import SCRIPT, request, semantic_app


IDENTITY_STAGES = (
    TaskType.SHORT_SCRIPT,
    TaskType.SCRIPT_QC,
    TaskType.SCRIPT_REVIEW_ASSIST,
    TaskType.CONTENT_QC,
    TaskType.FINAL_REVIEW_ASSIST,
)
RULE_HEADING = "## 读音与元数据身份一致性"
CUSTOM_PROMPT = "正式产品名必须在 full_narration 实际说出、不能只放标题。"


def test_shared_identity_policy_preserves_semantic_hard_gates():
    for requirement in (
        "仅在研究未提供可靠读音依据，或正常易读口播修复需要时",
        "已核验产品类别与自然中文规格说明",
        "不得编造中文品牌名或中文正式名称、删除关键数字事实或改变规格含义",
        "opening.product_identity 仍保留来源中的正式身份",
        "至少一项 title_options\n必须准确完整包含该身份",
        "前8秒仍须明确产品身份并兑现核心价值/payoff",
        "元数据不能替代成片中缺失或不清的身份与价值",
        "仍列 error / blocker",
        "返回非通过结论，不得只记 warning",
        "后续声音、画面及全部事实 QC 保持不变",
        "不作字符串强换、拼接读音或向 ASR 注入预期答案",
    ):
        assert requirement in _PRONUNCIATION_IDENTITY_RULE


@pytest.mark.parametrize(
    ("task_type", "schema_version"),
    [(task, "1") for task in IDENTITY_STAGES] + [(TaskType.CONTENT_QC, "2")],
)
@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize(
    ("handsfree", "setting_enabled"),
    [(False, True), (True, False), (None, False), (None, True), (None, None)],
)
def test_identity_rule_reaches_all_modes_and_custom_prompts(
    tmp_path, monkeypatch, task_type, schema_version, custom, handsfree, setting_enabled
):
    monkeypatch.setenv("GLANCE_HANDSFREE_ENABLED", "false")
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            if setting_enabled is not None:
                session.add(
                    RuntimeSetting(
                        key="handsfree_enabled", value=str(setting_enabled).lower()
                    )
                )
            if custom:
                session.add(
                    TaskPrompt(task_type=task_type.value, instructions=CUSTOM_PROMPT)
                )
            session.commit()
            before = effective_settings(session, Settings()).handsfree_enabled
            text = resolve_instructions(
                session, task_type, handsfree=handsfree, schema_version=schema_version
            )
            assert text.count(RULE_HEADING) == 1
            assert _PRONUNCIATION_IDENTITY_RULE in text
            if custom:
                assert text.startswith(CUSTOM_PROMPT)
                assert text.index(RULE_HEADING) > text.index(CUSTOM_PROMPT)
            is_handsfree = before if handsfree is None else handsfree
            assert (_HANDSFREE_STAGE_FACT_RULE in text) is is_handsfree
            if not is_handsfree:
                assert "## Handsfree" not in text
                assert "不请求人工" not in text
                assert "自动跳过" not in text
            assert effective_settings(session, Settings()).handsfree_enabled is before
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "task_type", [task for task in TaskType if task not in IDENTITY_STAGES]
)
@pytest.mark.parametrize("handsfree", [False, True])
def test_unrelated_stages_do_not_receive_identity_rule(tmp_path, task_type, handsfree):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    try:
        with Session(runtime.engine) as session:
            session.add(
                TaskPrompt(
                    task_type=task_type.value, instructions="保留本角色授权边界。"
                )
            )
            session.commit()
            text = resolve_instructions(session, task_type, handsfree=handsfree)
            assert RULE_HEADING not in text
            if not handsfree and task_type == TaskType.RECOVERY:
                assert text == "保留本角色授权边界。\n" + _SCRIPT_FACT_FIDELITY_RULE
            elif not handsfree and task_type not in {TaskType.DOUYIN_PUBLISH, TaskType.ASSET_GENERATE_IMAGE}:
                assert text == "保留本角色授权边界。"
    finally:
        runtime.close()


def readable_script():
    script = deepcopy(SCRIPT)
    script.update(
        title_options=[
            "IKEA KOMPISHÄNG 的功能设计",
            "看看宜家的家具设计",
            "家具功能介绍",
        ],
        opening={
            "product_identity": "IKEA KOMPISHÄNG",
            "special_value": "家具功能设计",
            "hook_line": "看看宜家的家具设计。",
            "payoff_line": "宜家的家具展示了功能设计。",
            "scene": "家具概念说明",
        },
        full_narration="看看宜家的家具设计。宜家的家具展示了功能设计。你会怎么使用它？",
    )
    return script


def test_formal_identity_can_anchor_title_without_forcing_unreadable_narration():
    script = readable_script()
    parsed = ShortScriptResult.model_validate(script)
    assert parsed.opening.product_identity not in parsed.full_narration
    assert parsed.opening.product_identity in parsed.title_options[0]
    script["title_options"][0] = "家具功能设计"
    with pytest.raises(ValidationError, match="full_narration 或 title_options"):
        ShortScriptResult.model_validate(script)


@pytest.mark.parametrize("quarantine", [TASK_QUARANTINE, CONTENT_QUARANTINE])
def test_imported_work_cannot_use_a_genuine_live_lease_to_get_a_bundle(
    tmp_path, quarantine
):
    runtime = make_runtime(tmp_path)
    with TestClient(semantic_app(runtime)) as http:
        content_id, researcher, claim = select_and_claim(http, runtime)
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, claim["task_id"])
            frozen = deepcopy(task.model_dump())
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="test-import",
                    entity_type="agent_task"
                    if quarantine == TASK_QUARANTINE
                    else "content",
                    entity_id=task.id if quarantine == TASK_QUARANTINE else content_id,
                    action=quarantine,
                    metadata_json={"reason": "Unverified historical import"},
                )
            )
            session.commit()
        denied = researcher.client.get(
            claim["bundle_url"],
            headers={"Authorization": f"Bearer {claim['lease_token']}"},
        )
        assert denied.status_code == 409
        assert "MPT_MIGRATION_QUARANTINED" in denied.text
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, claim["task_id"]).model_dump() == frozen


def test_real_manual_http_script_and_qc_bundles_preserve_policy_and_lease_guards(
    tmp_path,
):
    runtime = make_runtime(tmp_path)
    with TestClient(semantic_app(runtime)) as http:
        with Session(runtime.engine) as session:
            session.add(RuntimeSetting(key="handsfree_enabled", value="false"))
            for task_type in (TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC):
                session.add(
                    TaskPrompt(task_type=task_type.value, instructions=CUSTOM_PROMPT)
                )
            session.commit()
        content_id, researcher, research_claim = select_and_claim(http, runtime)
        research = deepcopy(RESEARCH)
        research.update(
            subject="IKEA KOMPISHÄNG", recommended_angle="说明宜家的家具功能设计"
        )
        research["verified_claims"][0]["claim"] = (
            "IKEA（宜家）的 KOMPISHÄNG 是家具产品，展示功能设计。"
        )
        assert (
            researcher.complete(
                research_claim["task_id"],
                research_claim["lease_token"],
                "research-worker",
                research,
            )["advanced"]
            == "CONSUMED"
        )

        agent = client_for(http, "script")
        for task_type in (TaskType.SHORT_SCRIPT, TaskType.SCRIPT_QC):
            with Session(runtime.engine) as session:
                task = session.exec(
                    select(AgentTask).where(AgentTask.task_type == task_type)
                ).one()
                task_id = task.id
                snapshot = task.instructions
                frozen_input = deepcopy(task.input_data)
                assert snapshot.count(RULE_HEADING) == 1
                assert task.assigned_agent_id is None
            bundle_url = f"/api/v1/agent/tasks/{task_id}/bundle"
            assert http.get(bundle_url, headers=ADMIN).status_code == 409
            assert http.get(bundle_url, headers=agent._headers()).status_code == 409
            claim = agent.claim()
            assert claim["task_id"] == task_id
            bundle = agent.bundle(claim["bundle_url"], claim["lease_token"])
            assert bundle["instructions"] == snapshot
            assert bundle["instructions"].count(RULE_HEADING) == 1
            assert "## Handsfree" not in bundle["instructions"]
            assert (
                bundle["task"]["prompt_version"] == f"live-{stable_hash(snapshot)[:12]}"
            )
            with Session(runtime.engine) as session:
                task = session.get(AgentTask, task_id)
                assert task.assigned_agent_id == agent.settings.agent_id
                assert task.instructions == snapshot and task.input_data == frozen_input
                assert (
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == task_id, AuditEvent.action == OWNED
                        )
                    )
                    .one()
                    .metadata_json["input_hash"]
                    == task.input_hash
                )
                assert session.exec(
                    select(AuditEvent).where(
                        AuditEvent.entity_id == task_id,
                        AuditEvent.action == "bundle_served",
                    )
                ).one().metadata_json["served_prompt_hash"] == stable_hash(snapshot)

            if task_type == TaskType.SHORT_SCRIPT:
                assert (
                    agent.complete(
                        task_id,
                        claim["lease_token"],
                        "script-worker",
                        readable_script(),
                    )["advanced"]
                    == "CONSUMED"
                )
                assert request(http, content_id).status_code == 202
            else:
                result = {
                    "passed": False,
                    "issues": [
                        {
                            "severity": "error",
                            "field": "facts",
                            "message": "仍需核验实际事实证据",
                        }
                    ],
                }
                assert (
                    agent.complete(
                        task_id, claim["lease_token"], "script-worker", result
                    )["advanced"]
                    == "CONSUMED"
                )
                assert (
                    http.post(
                        f"/api/v1/contents/{content_id}/approve-script", headers=ADMIN
                    ).status_code
                    == 409
                )

        with Session(runtime.engine) as session:
            stored_result = deepcopy(session.get(AgentTask, task_id).result_data)
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="test-import",
                    entity_type="agent_task",
                    entity_id=task_id,
                    action=TASK_QUARANTINE,
                    metadata_json={"reason": "Unverified historical import"},
                )
            )
            session.commit()
        denied = http.get(
            claim["bundle_url"],
            headers={"Authorization": f"Bearer {claim['lease_token']}"},
        )
        assert denied.status_code == 409
        assert "MPT_MIGRATION_QUARANTINED" in denied.text
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, task_id).result_data == stored_result
            assert not effective_settings(session, runtime.settings).handsfree_enabled
