"""运行时可改配置：管理台改完即时生效，无需重启。

分层：`.env` 提供启动默认值 → `runtime_setting` 表按键覆盖。
只有真正安全的字段可覆盖（见 EDITABLE_FIELDS）；数据库地址、监听端口这类
必须重启的字段不在其中，页面上只读展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from sqlmodel import Session, select

from glance_core.models import AuditEvent, RuntimeSetting, utc_now
from glance_core.settings import (
    SUPPORTED_TTS_PROVIDERS,
    Settings,
    normalize_tts_provider,
    resolve_kokoro_voice,
    validate_tts_provider_endpoint,
)

FieldKind = Literal["text", "number", "bool", "select"]


@dataclass(frozen=True)
class EditableField:
    key: str
    label: str
    group: str
    kind: FieldKind
    help: str = ""
    options: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


#: 允许在管理台修改并即时生效的字段。顺序即页面展示顺序。
EDITABLE_FIELDS: tuple[EditableField, ...] = (
    # ---- 配音 ----
    EditableField(
        "tts_provider",
        "配音 Provider",
        "配音",
        "select",
        "支持 edge、cosyvoice3 和本机 kokoro；不可用时不会静默切换。",
        SUPPORTED_TTS_PROVIDERS,
    ),
    EditableField(
        "tts_provider_endpoint",
        "配音 Provider 地址",
        "配音",
        "text",
        "cosyvoice3 只能填写本机 loopback 地址；edge 和 kokoro 留空。",
    ),
    EditableField(
        "tts_voice",
        "配音音色",
        "配音",
        "select",
        "Edge 中文音色；Kokoro 本机中文男声 kokoro:58，女声 kokoro:3。",
        (
            "zh-CN-YunxiNeural",
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-YunjianNeural",
            "zh-CN-XiaoyiNeural",
            "zh-CN-YunyangNeural",
            "zh-CN-liaoning-XiaobeiNeural",
            "kokoro:58",
            "kokoro:3",
        ),
    ),
    EditableField(
        "tts_rate",
        "语速",
        "配音",
        "text",
        "edge-tts 语法，例如 +20%、-10%。超过 +30% 会明显机械化。",
    ),
    EditableField("tts_volume", "音量", "配音", "text", "edge-tts 语法，例如 +0%、+10%。"),
    EditableField(
        "min_video_seconds",
        "成片最短时长（秒）",
        "配音",
        "number",
        "配音合成后立即校验，不达标不进入渲染。",
        minimum=5,
        maximum=600,
    ),
    EditableField(
        "max_video_seconds",
        "成片最长时长（秒）",
        "配音",
        "number",
        "超过即判定不合格。",
        minimum=10,
        maximum=1800,
    ),
    # ---- 渲染 ----
    EditableField(
        "video_encoder",
        "视频编码器",
        "渲染",
        "select",
        "h264_nvenc 走 NVIDIA 硬件编码；探测不到会自动回退 libx264。",
        ("libx264", "h264_nvenc"),
    ),
    EditableField(
        "bgm_enabled",
        "成片背景音乐",
        "渲染",
        "bool",
        "开启后把 data/bgm 曲库中的已授权铺底混进口播下方；曲库为空则仍只保留口播。",
    ),
    EditableField(
        "bgm_track_id",
        "指定曲目 ID",
        "渲染",
        "text",
        "对应 catalog.json 的 id 或文件名（不含扩展名）。留空则按内容 ID 稳定抽一首。",
    ),
    EditableField(
        "bgm_volume",
        "背景音乐音量",
        "渲染",
        "number",
        "相对口播的铺底音量，建议 0.10-0.20；说话时还会再压低。",
        minimum=0.05,
        maximum=0.4,
    ),
    EditableField(
        "cover_ai_enabled",
        "AI 封面底图",
        "渲染",
        "bool",
        "开启后必须先完成 AI 封面底图，内容才会进入 PACKAGED 和发布队列；"
        "关闭后才允许使用确定性兜底封面。",
    ),
    EditableField(
        "cover_style_preset",
        "封面视觉预设",
        "渲染",
        "text",
        "账号所有 AI 封面共用这一套 Brand Kit；封面大字始终由平台用统一字体确定性叠加。",
    ),
    # ---- 转写 ----
    EditableField(
        "asr_model",
        "本地转写模型",
        "转写",
        "text",
        "faster-whisper 模型名，如 distil-large-v3。留空则不做本地转写。",
    ),
    EditableField(
        "asr_device",
        "转写设备",
        "转写",
        "select",
        "auto 会优先使用 GPU。",
        ("auto", "cuda", "cpu"),
    ),
    EditableField(
        "asr_compute_type",
        "转写精度",
        "转写",
        "select",
        "RTX 50 系（Blackwell）固定用 float16，int8 有兼容问题。",
        ("float16", "int8", "float32"),
    ),
    # ---- 采集与调度 ----
    EditableField(
        "enrich_batch_size",
        "候选评估批量大小",
        "采集与调度",
        "number",
        "一个评估任务最多打包多少条候选（上限 20）。",
        minimum=1,
        maximum=20,
    ),
    EditableField(
        "enrich_task_ttl_hours",
        "评估任务过期小时",
        "采集与调度",
        "number",
        "超时仍无人领取的评估任务自动取消，候选留待下轮重新打包。",
        minimum=1,
        maximum=720,
    ),
    EditableField(
        "ytdlp_cookies_file",
        "YouTube Cookies 文件",
        "采集与调度",
        "text",
        "Netscape 格式 cookies.txt 绝对路径；用浏览器插件导出，留空则不携带。"
        "用于破解 YouTube 人机验证，建议用小号无痕窗口导出。",
    ),
    EditableField(
        "media_user_agent",
        "媒体请求浏览器 UA",
        "采集与调度",
        "text",
        "Kickstarter 来源页和视频直链请求使用；应与导出 cookies.txt 的浏览器保持一致。",
    ),
    EditableField(
        "video_platform_domains",
        "视频平台域名白名单",
        "采集与调度",
        "text",
        "逗号分隔，匹配域名后缀；候选未填 video_url 时只有命中的来源页才算有视频源。",
    ),
    EditableField(
        "assistant_auto_reject_enabled",
        "助手自动淘汰低价值候选",
        "助手 Agent",
        "bool",
        "仅对助手给出高置信度淘汰建议、且评分不高于阈值的候选生效；候选可恢复。",
    ),
    EditableField(
        "assistant_auto_reject_max_score",
        "自动淘汰最高评分",
        "助手 Agent",
        "number",
        "只有评分不高于该值且助手高置信度建议淘汰时才自动移出人工队列。",
        minimum=0,
        maximum=100,
    ),
    EditableField(
        "assistant_auto_approve_script_enabled",
        "脚本自动审核（兼容配置）",
        "助手 Agent",
        "bool",
        "当前产品分享流程始终保留人工脚本验收；该字段仅兼容旧配置，worker 不会据此自动"
        "批准、重写或推进配音。",
    ),
    # ---- 无人值守 ----
    EditableField(
        "handsfree_enabled",
        "Handsfree 无人值守总开关",
        "无人值守",
        "bool",
        "打开后服务端按下方子开关自动推进选题、审核和抖音发布；风控、登录和未知结果仍会暂停并要求人工处理。",
    ),
    EditableField(
        "handsfree_auto_topic_enabled",
        "自动选题",
        "无人值守",
        "bool",
        "接受候选助手 SELECT（medium/high）、达到最低评分、未重复且视频源可用的候选；"
        "低置信度或 HUMAN 会暂停，一次只自动进入一个新选题。",
    ),
    EditableField(
        "handsfree_auto_review_enabled",
        "自动审核与返工",
        "无人值守",
        "仅对高置信度通过执行脚本/成片推进；质检失败、REVISE 超过预算或 HUMAN 会停在人工队列。",
    ),
    EditableField(
        "handsfree_auto_publish_douyin_enabled",
        "自动发布到抖音",
        "无人值守",
        "只发布已完成成片且发布审核高置信度通过的内容；单本机账号并发 1，发布不确定时不重发。",
    ),
    EditableField(
        "handsfree_min_candidate_score",
        "自动选题最低评分",
        "无人值守",
        "候选评分低于此值不会自动进入制作；候选助手仍必须返回 SELECT。",
        minimum=0,
        maximum=100,
    ),
    EditableField(
        "handsfree_daily_publish_limit",
        "每日自动发布上限",
        "无人值守",
        "按本地日期统计已确认 PUBLISHED 的抖音记录；达到上限后暂停新发布。0 表示禁止自动发布。",
        minimum=0,
        maximum=20,
    ),
    EditableField(
        "handsfree_publish_interval_minutes",
        "自动发布最小间隔（分钟）",
        "无人值守",
        "距离最近一次已确认抖音发布不足此间隔时暂停新发布；发布结果 UNKNOWN 不会触发重试。",
        minimum=0,
        maximum=1440,
    ),
    # ---- 智能恢复 ----
    EditableField(
        "recovery_enabled",
        "智能恢复总开关",
        "智能恢复",
        "bool",
        "业务质检、任务契约和上下文失败可由助手 Agent 诊断并创建受控修复任务；"
        "发布和不确定事项仍升级人工。",
    ),
    EditableField(
        "recovery_max_rounds",
        "单条内容最大恢复轮次",
        "智能恢复",
        "number",
        "每轮都会创建新的恢复任务或修复子任务；达到上限后阻断，防止无限循环。",
        minimum=0,
        maximum=10,
    ),
    EditableField(
        "recovery_same_task_retries",
        "同任务暂态重试次数",
        "智能恢复",
        "number",
        "仅用于网络、执行器或租约等暂态错误；业务质检不会覆盖原任务重试。",
        minimum=0,
        maximum=3,
    ),
    EditableField(
        "recovery_unknown_attempts",
        "未知错误诊断次数",
        "智能恢复",
        "未知错误只允许有限次助手诊断，无法安全归类时升级人工。",
        minimum=0,
        maximum=2,
    ),
    EditableField(
        "recovery_max_parallel",
        "恢复协调最大并发",
        "智能恢复",
        "全局 Recovery Agent 同时处理的链路数；每条链路仍是短任务，完成即退出。",
        minimum=1,
        maximum=8,
    ),
    # ---- 任务租约 ----
    EditableField(
        "agent_lease_minutes",
        "任务正式租约时长（分钟）",
        "任务租约",
        "number",
        "超时未完成的任务将被自动回收。",
        minimum=5,
        maximum=180,
    ),
    EditableField(
        "agent_reserve_seconds",
        "预约租约时长（秒）",
        "任务租约",
        "number",
        "唤起成功前的短租约；须大于宿主从取得 prompt 到 Agent 发出首个请求的耗时。",
        minimum=10,
        maximum=600,
    ),
    # ---- 视觉分析 ----
    EditableField(
        "scene_batch_size",
        "场景分析分批阈值",
        "视觉分析",
        "number",
        "单条内容场景数超过此值时拆为多批分析，全部批次完成后服务端合并。",
        minimum=20,
        maximum=200,
    ),
    # ---- 管线能力 ----
    EditableField(
        "asset_image_enabled",
        "AI 生图补充画面",
        "管线能力",
        "bool",
        "允许视觉 Agent 提出生图请求；开启后需有生图 Agent 才能继续渲染。",
    ),
    EditableField(
        "image_only_gen_budget",
        "图文模式生图预算",
        "管线能力",
        "number",
        "图文模式单条内容的最大生图次数（0=全部原图直用，8=全部重绘）。",
        minimum=0,
        maximum=8,
    ),
    EditableField(
        "content_qc_enabled",
        "成片语义质检",
        "管线能力",
        "bool",
        "技术质检通过后先交给视觉 Agent 检查成片语义，再进入人工成片审核。",
    ),
    EditableField(
        "publish_prepare_enabled",
        "发布文案准备",
        "管线能力",
        "bool",
        "成片打包后由编导 Agent 生成标题、简介和标签建议，不阻断人工发布。",
    ),
    # ---- 归档 ----
    EditableField(
        "archive_enabled", "打包后自动归档", "归档", "bool", "向归档 git 仓库提交元数据快照。"
    ),
)

EDITABLE_KEYS: frozenset[str] = frozenset(field.key for field in EDITABLE_FIELDS)

#: 只读展示：改这些必须编辑 .env 并重启。
READONLY_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("host", "监听地址", "改动需重启服务端"),
    ("port", "监听端口", "改动需重启服务端"),
    ("data_dir", "数据根目录", "媒体产物与工作区的根路径，改动需重启"),
    ("database_url", "数据库地址", "状态与审计的事实源，改动需重启"),
    ("huey_database_path", "任务队列文件", "Huey 队列，改动需重启服务端与 Worker"),
    ("archive_repo_dir", "归档仓库目录", "改动需重启"),
    ("public_base_url", "对外基础地址", "仅用于发布包元数据"),
)


class RuntimeSettingsError(ValueError):
    """Persisted overrides cannot be applied; report keys without their values."""

    def __init__(self, fields):
        self.fields = tuple(sorted(set(fields)))
        super().__init__(f"RUNTIME_SETTINGS_INVALID ({', '.join(self.fields)})")


def _coerce(field: EditableField, raw: str) -> Any:
    if field.kind == "number":
        number = float(raw)
        return int(number) if number.is_integer() else number
    if field.kind == "bool":
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise ValueError("Invalid persisted boolean")
    return raw


def load_overrides(session: Session) -> dict[str, Any]:
    """Read recognized overrides; an invalid value must not erase an opt-out."""
    overrides: dict[str, Any] = {}
    by_key = {field.key: field for field in EDITABLE_FIELDS}
    for row in session.exec(select(RuntimeSetting)).all():
        field = by_key.get(row.key)
        if field is None:
            continue
        try:
            value = _coerce(field, row.value)
            if row.key == "tts_provider":
                value = normalize_tts_provider(value, default=None)
            overrides[row.key] = value
        except (TypeError, ValueError):
            raise RuntimeSettingsError([row.key]) from None
    return overrides


def effective_settings(session: Session, base: Settings) -> Settings:
    """把数据库覆盖合并进启动配置，得到本次执行真正使用的配置。"""
    overrides = load_overrides(session)
    if not overrides:
        return base
    merged = base.model_dump()
    merged.update(overrides)
    try:
        # Validate a complete explicit snapshot without BaseSettings.__init__:
        # that constructor would read global GLANCE env/.env sources again.
        current = Settings.__new__(Settings)
        Settings.__pydantic_validator__.validate_python(merged, self_instance=current)
        return current
    except ValidationError as exc:
        fields = {str(error["loc"][0]) for error in exc.errors(include_input=False)
                  if error["loc"] and error["loc"][0] in EDITABLE_KEYS}
        raise RuntimeSettingsError(fields or overrides.keys()) from None
    except (TypeError, ValueError):
        raise RuntimeSettingsError(overrides.keys()) from None


def save_overrides(session: Session, values: dict[str, Any], *, actor_id: str = "admin-ui") -> int:
    """写入覆盖；值与启动默认一致时删除该行，保持表内只有真正的差异。"""
    base = Settings()
    values = dict(values)
    if "tts_provider" in values:
        values["tts_provider"] = normalize_tts_provider(values["tts_provider"], default=None)
    if "tts_provider_endpoint" in values:
        endpoint = values["tts_provider_endpoint"]
        if not isinstance(endpoint, str):
            raise ValueError("TTS Provider 地址必须是字符串")
        values["tts_provider_endpoint"] = endpoint.strip().rstrip("/")

    current_overrides = load_overrides(session)
    validate_tts_provider_endpoint(
        values.get("tts_provider", current_overrides.get("tts_provider", base.tts_provider)),
        values.get(
            "tts_provider_endpoint",
            current_overrides.get("tts_provider_endpoint", base.tts_provider_endpoint),
        ),
        allow_unconfigured=True,
    )
    provider = values.get("tts_provider", current_overrides.get("tts_provider", base.tts_provider))
    voice = values.get("tts_voice", current_overrides.get("tts_voice", base.tts_voice))
    if provider == "kokoro":
        resolve_kokoro_voice(voice)
    by_key = {field.key: field for field in EDITABLE_FIELDS}
    changed: dict[str, str] = {}
    for key, value in values.items():
        field = by_key.get(key)
        if field is None:
            continue
        text = "true" if value is True else "false" if value is False else str(value).strip()
        default = getattr(base, key, None)
        default_text = "true" if default is True else "false" if default is False else str(default)
        existing = session.get(RuntimeSetting, key)
        if text == default_text:
            if existing is not None:
                session.delete(existing)
                changed[key] = f"恢复默认（{default_text}）"
            continue
        if existing is None:
            session.add(RuntimeSetting(key=key, value=text))
            changed[key] = text
        elif existing.value != text:
            existing.value = text
            existing.updated_at = utc_now()
            session.add(existing)
            changed[key] = text
    if changed:
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="runtime_setting",
                entity_id="batch",
                action="settings_updated",
                metadata_json={"changed": changed},
            )
        )
    session.commit()
    return len(changed)
