from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from glance_core.models import TaskType


class AgentRole(StrEnum):
    DISCOVERY = "discovery"
    ASSISTANT = "assistant"
    RESEARCH = "research"
    SCRIPT = "script"
    VISUAL = "visual"
    IMAGE = "image"
    VIDEO = "video"
    PUBLISH = "publish"


@dataclass(frozen=True)
class AgentRoleSpec:
    role: AgentRole
    label: str
    description: str
    task_types: frozenset[TaskType]
    recommended_model_tier: str
    recommended_reasoning_effort: str
    required_host_capabilities: tuple[str, ...]
    skill_name: str
    required: bool
    requirement_tags: tuple[str, ...]
    recommended_wake_minutes: int
    #: 期望的技能版本，与仓库源 role.json 的 skill_version 保持一致；
    #: 宿主上报版本不一致时管理台 Agent 页告警（技能单一源，安装副本禁止手编）。
    skill_version: str = "1.1.0"
    #: Agent runtime protocol required for task claim/dispatch.
    protocol_version: str = "1"


ROLE_SPECS: dict[AgentRole, AgentRoleSpec] = {
    AgentRole.DISCOVERY: AgentRoleSpec(
        role=AgentRole.DISCOVERY,
        label="搜索 Agent",
        description="搜索新来源，实时查重，并把候选内容整理成中文摘要。",
        task_types=frozenset({TaskType.SOURCE_DISCOVERY, TaskType.CANDIDATE_ENRICH}),
        recommended_model_tier="经济级",
        recommended_reasoning_effort="中等",
        required_host_capabilities=("联网搜索", "网页阅读", "中文摘要", "结构化 JSON"),
        skill_name="glance-discovery-agent",
        skill_version="1.1.1",
        required=True,
        requirement_tags=("经济",),
        recommended_wake_minutes=30,
    ),
    AgentRole.ASSISTANT: AgentRoleSpec(
        role=AgentRole.ASSISTANT,
        label="助手 Agent",
        description="处理 manual 或 Handsfree 选题、脚本、成片和发布建议，由服务端按对应流程消费。",
        task_types=frozenset(
            {
                TaskType.CANDIDATE_ASSIST,
                TaskType.SCRIPT_REVIEW_ASSIST,
                TaskType.FINAL_REVIEW_ASSIST,
                TaskType.PUBLISH_REVIEW_ASSIST,
                TaskType.MEDIA_SOURCE_ASSIST,
                TaskType.RECOVERY,
            }
        ),
        recommended_model_tier="旗舰级",
        recommended_reasoning_effort="高",
        required_host_capabilities=("文本理解", "图片理解", "视频与音频理解", "结构化 JSON"),
        skill_name="glance-assistant-agent",
        required=True,
        requirement_tags=("旗舰", "多模态"),
        recommended_wake_minutes=30,
        skill_version="1.3.1",
    ),
    AgentRole.RESEARCH: AgentRoleSpec(
        role=AgentRole.RESEARCH,
        label="研究 Agent",
        description="对入选主题进行深度研究、事实核查，并整理可追溯来源。",
        task_types=frozenset({TaskType.CONTENT_RESEARCH}),
        recommended_model_tier="旗舰级",
        recommended_reasoning_effort="高",
        required_host_capabilities=("联网搜索", "网页阅读", "长上下文", "引用来源"),
        skill_name="glance-research-agent",
        required=True,
        requirement_tags=("旗舰",),
        recommended_wake_minutes=30,
        skill_version="1.2.1",
    ),
    AgentRole.SCRIPT: AgentRoleSpec(
        role=AgentRole.SCRIPT,
        label="编导 Agent",
        description="完成翻译、短视频脚本创作、文本审校和发布文案准备。",
        task_types=frozenset(
            {
                TaskType.SHORT_SCRIPT,
                TaskType.SCRIPT_QC,
                TaskType.CONTENT_QC,
                TaskType.PUBLISH_PREPARE,
            }
        ),
        recommended_model_tier="普通级",
        recommended_reasoning_effort="高",
        required_host_capabilities=("中文写作", "长文本审校", "结构化 JSON"),
        skill_name="glance-script-agent",
        required=True,
        requirement_tags=("普通",),
        recommended_wake_minutes=15,
        skill_version="1.2.0",
    ),
    AgentRole.VISUAL: AgentRoleSpec(
        role=AgentRole.VISUAL,
        label="视觉 Agent",
        description="理解原始画面，选择可用镜头，规划分镜并检查画面与文案是否匹配。",
        task_types=frozenset(
            {
                TaskType.SCENE_ANALYZE,
                TaskType.VISUAL_PLAN,
                TaskType.VISUAL_PLAN_QC,
                TaskType.ASSET_QC,
            }
        ),
        recommended_model_tier="旗舰级",
        recommended_reasoning_effort="高",
        required_host_capabilities=("图片理解", "视频或关键帧理解", "结构化 JSON"),
        skill_name="glance-visual-agent",
        required=True,
        requirement_tags=("旗舰", "多模态"),
        recommended_wake_minutes=15,
        skill_version="1.1.1",
    ),
    AgentRole.IMAGE: AgentRoleSpec(
        role=AgentRole.IMAGE,
        label="生图 Agent",
        description="在视觉方案明确需要新图片时，按规范生成可用于视频的图片素材。",
        task_types=frozenset({TaskType.ASSET_GENERATE_IMAGE}),
        recommended_model_tier="普通级",
        recommended_reasoning_effort="中等；连续性任务使用高",
        required_host_capabilities=("原生生图", "图片理解", "文件上传"),
        skill_name="glance-image-agent",
        required=False,
        requirement_tags=("普通", "图片生成"),
        recommended_wake_minutes=30,
        skill_version="1.2.1",
    ),
    AgentRole.VIDEO: AgentRoleSpec(
        role=AgentRole.VIDEO,
        label="视频 Agent",
        description="在视觉方案明确需要生成式镜头时，制作文生视频或图生视频素材。",
        task_types=frozenset({TaskType.ASSET_GENERATE_VIDEO}),
        recommended_model_tier="普通级",
        recommended_reasoning_effort="高",
        required_host_capabilities=("原生视频生成", "进度查询", "文件上传", "长任务心跳"),
        skill_name="glance-video-agent",
        required=False,
        requirement_tags=("普通", "视频生成"),
        recommended_wake_minutes=0,
    ),
    AgentRole.PUBLISH: AgentRoleSpec(
        role=AgentRole.PUBLISH,
        label="发布 Agent",
        description=(
            "读取完整发布任务，在本机已登录浏览器分步核验并完成平台发布，回传证据。"
        ),
        task_types=frozenset({TaskType.DOUYIN_PUBLISH}),
        recommended_model_tier="普通级",
        recommended_reasoning_effort="高",
        required_host_capabilities=(
            "浏览器控制",
            "本机 Chrome 会话",
            "截图",
            "结构化 JSON",
        ),
        skill_name="glance-publish-agent",
        required=False,
        requirement_tags=("普通", "发布"),
        recommended_wake_minutes=0,
        skill_version="1.3.0",
    ),
}


def role_spec(role: AgentRole | str) -> AgentRoleSpec:
    return ROLE_SPECS[AgentRole(role)]


def primary_role_for(task_type: TaskType) -> AgentRole:
    for role in (
        AgentRole.DISCOVERY,
        AgentRole.ASSISTANT,
        AgentRole.RESEARCH,
        AgentRole.SCRIPT,
        AgentRole.VISUAL,
        AgentRole.IMAGE,
        AgentRole.VIDEO,
        AgentRole.PUBLISH,
    ):
        if task_type in ROLE_SPECS[role].task_types:
            return role
    raise ValueError(f"No Agent role configured for task type {task_type.value}")
