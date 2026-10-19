"""Package readiness precedes the publisher's mandatory live-platform checks."""
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.business.bundles import _publish_instruction_mode
from glance_core.models import AgentTask, AuditEvent, RuntimeSetting, TaskPrompt, TaskStatus, TaskType
from glance_core.prompts import resolve_instructions
from glance_core.task_instructions import TASK_INSTRUCTIONS
from glance_core.task_service import stable_hash
from glance_core.workflow import WorkflowService, handsfree_review_policy
from test.business.test_content_http import client_for, content_app
from test.business.test_discovery_http import make_runtime
from test.business.test_image_preparation_evidence import evidence_fixture as evidence_fixture
from test.business.test_publish_package_evidence_repair import seed_package

HF_HEADING = "## Handsfree 无人值守发布包审核规则"
PHASE_HEADING = "## 发布包审核相位边界"


def mark_handsfree(session, content_id, *, actor="handsfree-coordinator"):
    session.add(AuditEvent(actor_type="system", actor_id=actor, entity_type="content", entity_id=content_id,
        action="handsfree_candidate_selected"))
    session.commit()


def leased_bundle(http, runtime, task_id):
    assistant = client_for(http, "assistant")
    claim = assistant.dispatch(wait=0, task_id=task_id)
    assert claim["should_run"] and claim["task_id"] == task_id
    assistant.confirm_claim(task_id, claim["lease_token"], assistant.settings.agent_id)
    return assistant.bundle(claim["bundle_url"], claim["lease_token"])


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("origin,policy_kind,expected", [
    ("manual", "none", False), ("manual", "full", False),
    ("fake_actor", "full", False), ("handsfree", "none", False),
    ("handsfree", "partial", False), ("handsfree", "numeric_optin", False),
    ("handsfree", "wrong_timing", False), ("handsfree", "full", True),
])
def test_actual_review_bundle_mode_uses_frozen_policy_and_real_origin(tmp_path, custom, origin, policy_kind, expected):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_auto_publish_douyin_enabled = True
    content_id, *_ = seed_package(runtime)
    with TestClient(content_app(runtime)) as http:
        with Session(runtime.engine) as session:
            if origin != "manual":
                mark_handsfree(session, content_id, actor="handsfree-coordinator" if origin == "handsfree" else "forged")
            if custom:
                session.add(TaskPrompt(task_type=TaskType.PUBLISH_REVIEW_ASSIST.value,
                    instructions="旧自定义要求：先登录并上传，取得实际平台裁切截图才能审核。"))
                session.commit()
            flow = runtime.workflow_service(session)
            payload = flow.build_publish_review_input(content_id, settings=runtime.settings,
                publish_policy=handsfree_review_policy() if expected else None)
            if policy_kind != "none":
                policy = handsfree_review_policy()
                if policy_kind == "partial":
                    policy.pop("timing")
                elif policy_kind == "numeric_optin":
                    policy["explicit_user_opt_in"] = 1
                elif policy_kind == "wrong_timing":
                    policy["timing"] = "unknown"
                # Simulate even a malformed frozen policy: a complete policy alone cannot forge ownership.
                payload["publish_policy"] = policy
            task = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, payload)
            assert (HF_HEADING in task.instructions) is expected
            task_id, frozen = task.id, deepcopy(task.model_dump(mode="json"))
            # Current global settings cannot convert this manual task or erase its frozen legitimate policy.
            session.add(RuntimeSetting(key="handsfree_enabled", value="false" if expected else "true"))
            session.commit()
        bundle = leased_bundle(http, runtime, task_id)
        instructions = bundle["instructions"]
        assert (HF_HEADING in instructions) is expected
        assert PHASE_HEADING in instructions
        assert "READY 表示发布包材料达到本阶段要求" in instructions
        assert "只有 AUTHORIZED 且 click_authorized=true 才能点击一次" in instructions
        assert "真实封面文件缺字、披露不清" in instructions
        if custom:
            assert "旧自定义要求" in instructions
            assert "以下规则覆盖自定义提示词中冲突的执行方式" in instructions
        with Session(runtime.engine) as session:
            current = session.get(AgentTask, task_id)
            # Lease fields change normally; the historical prompt and business input do not.
            assert current.input_data == frozen["input_data"]
            assert current.input_hash == frozen["input_hash"]
            assert current.instructions == frozen["instructions"]


@pytest.mark.parametrize("handsfree", [False, True])
def test_recovery_current_contract_keeps_same_review_phase_and_mode(tmp_path, handsfree):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_auto_publish_douyin_enabled = True
    content_id, *_ = seed_package(runtime)
    with TestClient(content_app(runtime)) as http:
        with Session(runtime.engine) as session:
            if handsfree:
                mark_handsfree(session, content_id)
            flow = runtime.workflow_service(session)
            payload = flow.build_publish_review_input(content_id, settings=runtime.settings,
                publish_policy=handsfree_review_policy() if handsfree else None)
            source = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, payload)
            source.status, source.error_code, source.attempt = TaskStatus.FAILED, "CONTRACT_FIXTURE", 1
            session.add(source)
            session.commit()
            source_id, frozen_source = source.id, deepcopy(source.model_dump(mode="json"))
            recovery = flow.create_recovery_task(source, failure_class="TASK_CONTRACT",
                code="CONTRACT_FIXTURE", message="Package phase contract fixture")
            assert recovery is not None
            recovery_id = recovery.id
        bundle = leased_bundle(http, runtime, recovery_id)
        current = bundle["input"]["current_input"]
        assert current["package_review_contract"] == payload["package_review_contract"]
        assert current.get("publish_policy") == payload.get("publish_policy")
        assert (HF_HEADING in current["current_contract"]["instructions"]) is handsfree
        assert PHASE_HEADING in current["current_contract"]["instructions"]
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, source_id).model_dump(mode="json") == frozen_source


@pytest.mark.parametrize("revision", [False, True])
def test_versioned_phase_marker_is_meaningful_and_legacy_payload_stays_unchanged(tmp_path, revision):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.settings.handsfree_auto_publish_douyin_enabled = True
    content_id, *_ = seed_package(runtime, revision=revision)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        payload = flow.build_publish_review_input(content_id, settings=runtime.settings)
        assert ("package_review_contract" in payload) is revision
        assert "publish_policy" not in payload
        if revision:
            contract = payload["package_review_contract"]
            assert contract["phase"] == "package_preflight" and contract["ready_means"] == "package_ready_only"
            assert contract["publication_path"] == "operator_requested_douyin_after_ready"
            assert contract["platform"] == "douyin" and contract["authorizes_publication"] is False
            assert "portrait_and_landscape_cover_crop" in contract["required_live_checks"]
            assert "user_clicked" not in contract and "explicit_user_opt_in" not in contract and "timing" not in contract
        else:
            assert set(payload) == {"script", "research", "publish_artifact_id", "publish_video_url", "publish_prepare",
                "cover_artifact_id", "cover_image_url", "context_files"}
        forged = flow.build_publish_review_input(content_id, settings=runtime.settings, publish_policy=handsfree_review_policy())
        assert "publish_policy" not in forged


def test_normal_rebuild_issues_one_new_phase_review_and_preserves_old_blocked(evidence_fixture, monkeypatch, tmp_path):
    from test.business import test_image_preparation_evidence as previous
    original_builder = WorkflowService.build_publish_review_input
    first = []
    def old_contract_once(self, *args, **kwargs):
        payload = original_builder(self, *args, **kwargs)
        if not first:
            payload.pop("package_review_contract")
            first.append(True)
        return payload
    monkeypatch.setattr(WorkflowService, "build_publish_review_input", old_contract_once)
    previous.test_eight_imports_refresh_one_normal_review_and_real_sdk_context(evidence_fixture, tmp_path)
    runtime, content_id, *_ = evidence_fixture
    with Session(runtime.engine) as session:
        tasks = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
            AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST).order_by(AgentTask.created_at)).all()
        assert len(tasks) == 2
        assert tasks[0].result_data["verdict"] == "BLOCKED"
        assert "package_review_contract" not in tasks[0].input_data
        assert tasks[1].input_data["package_review_contract"]["version"] == 1


def test_manual_prompt_default_cannot_inherit_global_handsfree_and_publisher_gates_are_unchanged(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.settings.handsfree_enabled = True
    runtime.initialize()
    with Session(runtime.engine) as session:
        assert HF_HEADING not in resolve_instructions(session, TaskType.PUBLISH_REVIEW_ASSIST, base_settings=runtime.settings)
        assert HF_HEADING in resolve_instructions(session, TaskType.PUBLISH_REVIEW_ASSIST, handsfree=True, base_settings=runtime.settings)
        for kind in (TaskType.CONTENT_QC, TaskType.FINAL_REVIEW_ASSIST):
            assert "Handsfree 机器音频证据与自动修复共同规则" in resolve_instructions(session, kind, base_settings=runtime.settings)
    # Existing publisher upload/crop/AI/intent/one-click instructions are byte-identical to base 5934c17.
    assert stable_hash(TASK_INSTRUCTIONS[TaskType.DOUYIN_PUBLISH]) == "0c4ae0fd1f7ca99328cf1cdbed22958923d86f61682e87f70df89710f4ce07fe"


def test_modified_frozen_review_input_does_not_grant_handsfree_mode(tmp_path):
    runtime = make_runtime(tmp_path)
    content_id, *_ = seed_package(runtime)
    with Session(runtime.engine) as session:
        mark_handsfree(session, content_id)
        task = AgentTask(content_id=content_id, task_type=TaskType.PUBLISH_REVIEW_ASSIST,
            input_data={"publish_policy": handsfree_review_policy()}, input_hash="0" * 64,
            prompt_name="fixture", instructions="fixture")
        assert _publish_instruction_mode(task, session) is False
