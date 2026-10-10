"""枚举与领域术语的中文标签：管理台唯一的翻译出口。

页面不得内联中文枚举名；新增枚举值时在此补一行，忘记补会回退成原值而不是报错。
"""

from __future__ import annotations

from glance_core.models import (
    CandidateStatus,
    ContentStatus,
    ProductionMode,
    ProviderKind,
    ProviderStatus,
    PublicationMode,
    RightsStatus,
    SourceType,
    StageStatus,
    TaskStatus,
    TaskType,
)

#: Agent 任务类型。括号内保留英文原值，便于对照 Skill 文档与日志。
TASK_TYPE_LABELS: dict[str, str] = {
    TaskType.AGENT_SELFTEST.value: "Agent 接入自检",
    TaskType.SOURCE_DISCOVERY.value: "来源发现",
    TaskType.CANDIDATE_ENRICH.value: "候选评估",
    TaskType.CANDIDATE_ASSIST.value: "选题辅助审核",
    TaskType.CONTENT_RESEARCH.value: "事实研究",
    TaskType.SCENE_ANALYZE.value: "场景分析",
    TaskType.SHORT_SCRIPT.value: "脚本撰写",
    TaskType.SCRIPT_QC.value: "脚本质检",
    TaskType.VISUAL_PLAN.value: "画面计划",
    TaskType.VISUAL_PLAN_QC.value: "画面计划质检",
    TaskType.CONTENT_QC.value: "成片质检",
    TaskType.ASSET_GENERATE_IMAGE.value: "AI 图片生成",
    TaskType.ASSET_QC.value: "生成素材质检",
    TaskType.ASSET_GENERATE_VIDEO.value: "AI 视频生成",
    TaskType.PUBLISH_PREPARE.value: "发布包准备",
    TaskType.DOUYIN_PUBLISH.value: "抖音直发布",
    TaskType.SCRIPT_REVIEW_ASSIST.value: "脚本辅助审核",
    TaskType.FINAL_REVIEW_ASSIST.value: "成片辅助审核",
    TaskType.PUBLISH_REVIEW_ASSIST.value: "发布辅助检查",
    TaskType.MEDIA_SOURCE_ASSIST.value: "视频源补录建议",
    TaskType.RECOVERY.value: "智能恢复协调",
}

#: 任务类型的一句话职责说明（Agent 接入引导与任务详情用）。
TASK_TYPE_DESCRIPTIONS: dict[str, str] = {
    TaskType.AGENT_SELFTEST.value: "验证 Agent 的领取、工作区、校验与提交链路，不进入生产流程。",
    TaskType.SOURCE_DISCOVERY.value: "按服务端下发的范围浏览真实来源，回传结构化候选。",
    TaskType.CANDIDATE_ENRICH.value: "为一批候选产出中文标题、摘要、评分与是否值得制作。",
    TaskType.CANDIDATE_ASSIST.value: "对待人工确认的候选排序，建议入选、淘汰或人工判断。",
    TaskType.CONTENT_RESEARCH.value: "核实主题事实，区分已验证结论、厂商宣传与不确定说法。",
    TaskType.SCENE_ANALYZE.value: "看缩略图描述每个场景，判断画面价值与信息价值。",
    TaskType.SHORT_SCRIPT.value: "根据研究与场景分析写中文口播脚本，只引用已验证结论。",
    TaskType.SCRIPT_QC.value: "检查脚本的事实、时长、表达与结论，列出全部问题。",
    TaskType.VISUAL_PLAN.value: "按配音时长排布画面，只能引用已登记的场景与白名单布局。",
    TaskType.VISUAL_PLAN_QC.value: "检查画面计划与旁白是否匹配、有无重复与遗漏。",
    TaskType.CONTENT_QC.value: "对成片抽帧与字幕做语义质检，识别误导风险。",
    TaskType.ASSET_GENERATE_IMAGE.value: "按视觉规范生成并上传受控图片素材。",
    TaskType.ASSET_QC.value: "逐张检查生成素材是否符合画幅、主体、连续性与禁用元素要求。",
    TaskType.ASSET_GENERATE_VIDEO.value: "按镜头规范生成并上传受控 AI 视频素材。",
    TaskType.PUBLISH_PREPARE.value: "生成标题、简介、标签和封面建议，不执行发布。",
    TaskType.DOUYIN_PUBLISH.value: (
        "由本机发布 Agent 使用已登录浏览器分步核验并完成抖音发布，回传证据。"
    ),
    TaskType.SCRIPT_REVIEW_ASSIST.value: "对照研究和质检结果，为人工脚本审批提供建议。",
    TaskType.FINAL_REVIEW_ASSIST.value: "检查成片的画面、声音、字幕和脚本一致性并标注时间点。",
    TaskType.PUBLISH_REVIEW_ASSIST.value: "检查发布包和必填信息是否齐全。",
    TaskType.MEDIA_SOURCE_ASSIST.value: "为下载失败的内容寻找可用的替代视频源，供人工一键采纳。",
    TaskType.RECOVERY.value: "诊断 Agent 任务失败，按白名单创建重试或修复子任务。",
}

TASK_STATUS_LABELS: dict[str, str] = {
    TaskStatus.PENDING.value: "待领取",
    TaskStatus.LEASED.value: "执行中",
    TaskStatus.SUBMITTED.value: "已提交",
    TaskStatus.COMPLETED.value: "已完成",
    TaskStatus.REJECTED.value: "校验未通过",
    TaskStatus.FAILED.value: "失败",
    TaskStatus.CANCELLED.value: "已取消",
}

#: Agent 提交结果的校验结论（AgentSubmission.validation_status）。
VALIDATION_STATUS_LABELS: dict[str, str] = {
    "ACCEPTED": "校验通过",
    "REJECTED": "校验未通过",
}

CONTENT_STATUS_LABELS: dict[str, str] = {
    ContentStatus.PREPARING_CONTEXT.value: "素材准备中",
    ContentStatus.WAITING_AGENT.value: "等待 Agent",
    ContentStatus.SCRIPT_REVIEW.value: "待审脚本",
    ContentStatus.PRODUCING.value: "渲染中",
    ContentStatus.FINAL_REVIEW.value: "待审成片",
    ContentStatus.APPROVED.value: "已批准",
    ContentStatus.PACKAGED.value: "待发布",
    ContentStatus.PUBLISHED.value: "已发布",
    ContentStatus.REJECTED.value: "已否决",
    ContentStatus.FAILED.value: "失败",
    ContentStatus.ARCHIVED.value: "已归档",
}

PRODUCTION_MODE_LABELS: dict[str, str] = {
    ProductionMode.FOOTAGE.value: "原片剪辑",
    ProductionMode.IMAGE_ONLY.value: "图文模式",
}

PUBLICATION_MODE_LABELS: dict[str, str] = {
    PublicationMode.MANUAL.value: "人工上传",
    PublicationMode.EXECUTOR.value: "发布 Agent",
}

CANDIDATE_STATUS_LABELS: dict[str, str] = {
    CandidateStatus.NEW.value: "待评估",
    CandidateStatus.ENRICHED.value: "已评估",
    CandidateStatus.SELECTED.value: "已选题",
    CandidateStatus.IGNORED.value: "已忽略",
}

STAGE_STATUS_LABELS: dict[str, str] = {
    StageStatus.PENDING.value: "待执行",
    StageStatus.RUNNING.value: "执行中",
    StageStatus.SUCCESS.value: "成功",
    StageStatus.FAILED.value: "失败",
}

SOURCE_TYPE_LABELS: dict[str, str] = {
    SourceType.RSS.value: "RSS 订阅",
    SourceType.YOUTUBE.value: "YouTube 频道",
    SourceType.RSSHUB.value: "RSSHub",
    SourceType.REDDIT.value: "Reddit 版块",
    SourceType.PRODUCT_SITE.value: "产品站",
    SourceType.TRENDRADAR.value: "TrendRadar 推送",
    SourceType.AGENT.value: "Agent 主动发现",
}

RIGHTS_STATUS_LABELS: dict[str, str] = {
    RightsStatus.SELF_OWNED.value: "自有素材",
    RightsStatus.EXPLICIT_LICENSE.value: "已获授权",
    RightsStatus.OFFICIAL_REFERENCE.value: "官方引用",
    RightsStatus.RESEARCH_ONLY.value: "仅供研究",
    RightsStatus.REJECTED.value: "不可使用",
}

#: 管线阶段（StageRun.stage）与内容 current_stage。
STAGE_LABELS: dict[str, str] = {
    "research": "事实研究",
    "acquire": "素材获取",
    "acquire_images": "网页图片获取",
    "transcribe": "字幕转写",
    "scene_detect": "场景切分",
    "scene_analyze": "场景分析",
    "agent_context": "等待 Agent",
    "content_research": "事实研究",
    "short_script": "脚本撰写",
    "script_qc": "脚本质检",
    "script_qc_failed": "脚本质检未通过",
    "voice": "配音合成",
    "voice_gate": "配音时长校验",
    "voice_pending": "等待配音",
    "script_review": "脚本审核",
    "script_rewrite": "脚本重写",
    "visual_plan": "画面计划",
    "visual_plan_qc": "画面计划质检",
    "visual_plan_retry": "画面计划重试",
    "asset_generate": "素材生成",
    "asset_qc": "素材质检",
    "asset_qc_failed": "素材质检未通过",
    "asset_regenerate": "素材重新生成",
    "render_ready": "准备渲染",
    "render": "渲染",
    "render_pending": "等待渲染",
    "technical_qc": "技术质检",
    "content_qc": "成片质检",
    "content_qc_failed": "成片质检未通过",
    "content_qc_remediation": "成片质检整改",
    "recovery": "智能恢复",
    "content_qc_frames": "质检抽帧",
    "final_review": "成片审核",
    "handsfree_script_approved": "Handsfree 等待配音",
    "handsfree_final_approved": "Handsfree 等待打包",
    "package": "打包",
    "package_pending": "等待打包",
    "published": "已发布",
    "archived": "已归档",
}

PROVIDER_KIND_LABELS: dict[str, str] = {
    ProviderKind.EXECUTOR.value: "代码执行器",
    ProviderKind.AGENT.value: "Agent",
}

PROVIDER_STATUS_LABELS: dict[str, str] = {
    ProviderStatus.UNKNOWN.value: "未知",
    ProviderStatus.HEALTHY.value: "正常",
    ProviderStatus.DEGRADED.value: "降级",
    ProviderStatus.OFFLINE.value: "离线",
}

#: 审计事件动作。
AUDIT_ACTION_LABELS: dict[str, str] = {
    "created": "创建",
    "claimed": "领取",
    "claim_confirmed": "预约确认",
    "completed": "完成",
    "failed": "失败",
    "cancelled": "取消",
    "requeued": "重派",
    "lease_expired": "租约过期回收",
    "duplicate_suppressed": "拦截重复任务",
    "validation_rejected": "结果校验未通过",
    "archived": "归档",
    "content_archived": "内容归档",
    "content_deleted": "内容彻底删除",
    "advance_failed": "管线推进失败",
    "advance_recovered": "管线推进已补跑",
    "enrich_unknown_candidates": "回写了本批之外的候选",
    "enrich_title_not_translated": "中文标题未翻译被拒",
    "enrich_summary_not_translated": "中文摘要缺失或未翻译被拒",
    "restored_selected_status": "恢复已选题状态",
    "settings_updated": "修改系统设置",
    "prompt_updated": "修改任务提示词",
    "prompt_reset": "提示词恢复默认",
    "lease_policy_updated": "修改任务锁时长",
    "claim_at_capacity": "Agent 达到在途上限",
    "provider_policy_updated": "修改 Provider 路由策略",
    "generated_artifact_uploaded": "上传生成式素材",
    "similarity_checked": "实时查询相似项目",
    "archive_failed": "归档失败",
    "source_deleted": "删除来源",
    "candidate_ignored": "忽略候选",
    "candidate_restored": "恢复候选",
    "script_approved": "批准脚本",
    "script_rejected": "驳回脚本",
    "final_approved": "批准成片",
    "publication_recorded": "回填发布结果",
    "rerender_requested": "请求重新渲染",
    "dependency_missing": "系统依赖缺失",
    "candidate_auto_rejected": "助手自动淘汰候选",
    "handsfree_candidate_selected": "Handsfree 自动选题",
    "handsfree_candidate_blocked": "Handsfree 选题暂停",
    "handsfree_prepare_queued": "Handsfree 已排素材准备",
    "handsfree_script_approved": "Handsfree 自动通过脚本",
    "handsfree_script_queued": "Handsfree 已排脚本配音",
    "handsfree_final_approved": "Handsfree 自动通过成片",
    "handsfree_final_queued": "Handsfree 已排发布打包",
    "handsfree_publish_queued": "Handsfree 已排抖音发布",
    "agent_selfcheck": "Agent 接入自检",
    "agent_id_device_conflict": "Agent ID 设备冲突",
    "execution_deadline_exceeded": "执行超时回收",
}

#: 审计事件的实体类型（AuditEvent.entity_type）。
ENTITY_TYPE_LABELS: dict[str, str] = {
    "source": "采集来源",
    "candidate": "候选",
    "content": "内容",
    "agent_task": "Agent 任务",
    "agent_worker": "Agent 实例",
    "runtime_setting": "系统设置",
    "task_lease_policy": "任务锁策略",
    "publication_record": "发布记录",
    "system": "系统",
}

#: 审计事件的操作者类型（AuditEvent.actor_type）。
ACTOR_TYPE_LABELS: dict[str, str] = {
    "human": "人工",
    "system": "系统",
    "agent": "Agent",
}

#: 审计事件 metadata_json 的常见键。查不到的键原样显示，便于排查新字段。
AUDIT_META_LABELS: dict[str, str] = {
    "unknown_candidate_ids": "越界回写的候选 ID",
    "candidate_ids": "涉及的候选 ID",
    "content_id": "内容 ID",
    "missing": "缺失项",
    "tools": "工具状态",
    "task_type": "任务类型",
    "result_hash": "结果哈希",
    "attempt": "第几次尝试",
    "max_attempts": "最多尝试次数",
    "retryable": "是否可重试",
    "next_status": "变更后状态",
    "error": "错误详情",
    "error_code": "错误码",
    "commit": "归档提交",
    "path": "文件路径",
    "count": "数量",
    "status": "任务状态",
    "dedup_key": "幂等指纹",
    "direction_key": "搜索方向",
    "match_count": "相似结果数",
    "query_hash": "查询哈希",
    "changed": "变更项",
    "name": "名称",
    "note": "处理意见",
    "reason": "原因",
    "script_version": "脚本版本",
    "platform": "发布平台",
    "external_url": "外部链接",
    "external_id": "平台内容 ID",
    "mode": "发布方式",
}

#: 管线产物类型（Artifact.artifact_type）。
ARTIFACT_TYPE_LABELS: dict[str, str] = {
    "source_video": "源视频",
    "source_image": "源图片",
    "transcript": "字幕转写",
    "scenes": "场景清单",
    "voice": "配音音频",
    "agent_generated_image": "生成图片",
    "agent_generated_video": "生成视频",
    "final_video": "成片",
    "preview_video": "预览片",
    "publish_video": "发布视频",
    "cover": "发布封面",
    "publish_metadata": "发布信息",
}

#: 质检问题的严重程度（task_schemas.QCIssue.severity）。
QC_SEVERITY_LABELS: dict[str, str] = {
    "error": "严重",
    "warning": "提醒",
}

#: 候选分类：正文由 Agent 自由填写，这里只登记系统写入的默认值。
CATEGORY_LABELS: dict[str, str] = {
    "unknown": "未分类",
}

#: 失败码：前三个由管线自己写入，其余是 Agent 上报的标准码（见 Skill 的 errors.md）。
ERROR_CODE_LABELS: dict[str, str] = {
    "VOICE_DURATION": "配音时长超出边界",
    "TECHNICAL_QC": "技术质检未通过",
    "ADVANCE_FAILED": "管线推进失败",
    "PREPARE_FAILED": "素材准备失败",
    "RENDER_FAILED": "渲染失败",
    "VOICE_FAILED": "配音合成失败",
    "PACKAGE_FAILED": "打包失败",
    "VISUAL_PLAN_QC_FAILED": "画面计划质检未通过",
    "CONTENT_QC_FAILED": "成片语义质检未通过",
    "SCRIPT_QC_FAILED": "脚本质检未通过",
    "ASSET_QC_FAILED": "素材质检未通过",
    "SCENE_BATCH_INCOMPLETE": "场景批次不完整",
    "AGENT_TASK_DEPENDENCY_MISSING": "Agent 任务依赖缺失",
    "LEASE_EXPIRED": "任务租约已过期",
    "DIRECTION_BUSY": "搜索方向执行中",
    "NO_VIDEO_SOURCE": "缺可下载视频源",
    "SOURCE_UNAVAILABLE": "来源暂时不可访问",
    "INSUFFICIENT_EVIDENCE": "证据不足，无法核实",
    "INVALID_INPUT": "任务输入不合法",
    "UNSUPPORTED_TASK": "Agent 不支持该任务类型",
    "TOOL_UNAVAILABLE": "Agent 所需工具不可用",
    "SAFETY_BLOCKED": "触及安全边界，已终止",
}


def _lookup(table: dict[str, str], value: object, *, with_original: bool = False) -> str:
    """查表；未登记的值原样回显，避免页面出现空白。

    SQL 原生查询取回的枚举可能是 name 形式（如 SOURCE_DISCOVERY）而表 key 是 value
    形式（source_discovery），此处做大小写归一容错，一处修复所有漏翻。
    """
    raw = getattr(value, "value", value)
    if raw is None or raw == "":
        return "—"
    text = str(raw)
    label = table.get(text)
    if label is None:
        normalized = next(
            (form for form in (text.lower(), text.upper()) if form in table),
            None,
        )
        if normalized is not None:
            text = normalized
            label = table[normalized]
    if label is None:
        return text
    return f"{label}（{text}）" if with_original else label


def task_type_label(value: object, *, with_original: bool = False) -> str:
    return _lookup(TASK_TYPE_LABELS, value, with_original=with_original)


def task_type_description(value: object) -> str:
    return TASK_TYPE_DESCRIPTIONS.get(str(getattr(value, "value", value)), "")


def task_status_label(value: object) -> str:
    return _lookup(TASK_STATUS_LABELS, value)


def validation_status_label(value: object) -> str:
    return _lookup(VALIDATION_STATUS_LABELS, value)


def content_status_label(value: object) -> str:
    return _lookup(CONTENT_STATUS_LABELS, value)


def publication_mode_label(value: object) -> str:
    return _lookup(PUBLICATION_MODE_LABELS, value)


def candidate_status_label(value: object) -> str:
    return _lookup(CANDIDATE_STATUS_LABELS, value)


def stage_status_label(value: object) -> str:
    return _lookup(STAGE_STATUS_LABELS, value)


def source_type_label(value: object) -> str:
    return _lookup(SOURCE_TYPE_LABELS, value)


def rights_status_label(value: object) -> str:
    return _lookup(RIGHTS_STATUS_LABELS, value)


def stage_label(value: object) -> str:
    return _lookup(STAGE_LABELS, value)


def audit_action_label(value: object) -> str:
    return _lookup(AUDIT_ACTION_LABELS, value)


def error_code_label(value: object, *, with_original: bool = False) -> str:
    return _lookup(ERROR_CODE_LABELS, value, with_original=with_original)


def entity_type_label(value: object) -> str:
    return _lookup(ENTITY_TYPE_LABELS, value)


def actor_type_label(value: object) -> str:
    return _lookup(ACTOR_TYPE_LABELS, value)


def audit_meta_label(value: object, *, with_original: bool = False) -> str:
    """审计 metadata_json 的键说明；未登记的键原样显示。"""
    return _lookup(AUDIT_META_LABELS, value, with_original=with_original)


def artifact_type_label(value: object, *, with_original: bool = False) -> str:
    return _lookup(ARTIFACT_TYPE_LABELS, value, with_original=with_original)


def qc_severity_label(value: object) -> str:
    return _lookup(QC_SEVERITY_LABELS, value)


def category_label(value: object) -> str:
    return _lookup(CATEGORY_LABELS, value)
