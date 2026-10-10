from __future__ import annotations

import math
import re
from datetime import datetime
from itertools import pairwise
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from glance_core.models import RightsStatus, TaskType

DISCOVERY_URL_BLACKLIST = (
    "google.com/search",
    "bing.com/search",
    "duckduckgo.com/?q=",
    "/redirect",
    "t.co/",
)


def is_blocked_discovery_url(url: str) -> bool:
    lowered = url.lower()
    return any(pattern in lowered for pattern in DISCOVERY_URL_BLACKLIST)


def normalize_blank_optional(value: object) -> object:
    """指令写「查不到就留空」的可选字段：空串/纯空白/null/none 归一为 None。"""
    if isinstance(value, str) and value.strip().lower() in {"", "null", "none"}:
        return None
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScoreSet(StrictModel):
    visual: int = Field(ge=0, le=25)
    understandability: int = Field(ge=0, le=20)
    relevance: int = Field(ge=0, le=15)
    freshness: int = Field(ge=0, le=15)
    verdict_potential: int = Field(ge=0, le=10)
    source_quality: int = Field(ge=0, le=10)
    positioning: int = Field(ge=0, le=5)
    total: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def total_matches_parts(self) -> ScoreSet:
        expected = (
            self.visual
            + self.understandability
            + self.relevance
            + self.freshness
            + self.verdict_potential
            + self.source_quality
            + self.positioning
        )
        if self.total != expected:
            raise ValueError(f"total 必须等于各分项之和 {expected}")
        return self


class DiscoveredCandidate(StrictModel):
    external_id: str = Field(min_length=1)
    url: str = Field(max_length=2000, pattern=r"^https?://")
    video_url: str = Field(default="", max_length=2000, pattern=r"^(?:https?://.+)?$")
    thumbnail_url: str = Field(default="", max_length=2000, pattern=r"^(?:https?://.+)?$")
    title: str = Field(min_length=1, max_length=300)
    source_account: str = ""
    published_at: datetime | None = None
    rights_status: RightsStatus = RightsStatus.OFFICIAL_REFERENCE
    rights_note: str = ""
    heat_score: float = Field(default=0, ge=0, le=100)
    summary: str = Field(default="", max_length=500)

    @field_validator("published_at", mode="before")
    @classmethod
    def blank_published_at_is_none(cls, value: object) -> object:
        # 指令允许「查不到就留空」：空串不得拒收整批候选；无效日期仍报错。
        return normalize_blank_optional(value)


class SourceDiscoveryResult(StrictModel):
    candidates: list[DiscoveredCandidate] = Field(max_length=50)
    notes: str = Field(default="", max_length=2000)


class CandidateEnrichItem(StrictModel):
    candidate_id: str = Field(min_length=1)
    #: 中文标题：来源多为英文，运营需要在候选列表里直接读懂是什么。
    title_zh: str = Field(min_length=1)
    category: str
    summary: str = Field(min_length=1)
    scores: ScoreSet
    suggested_hook: str
    recommended: bool
    #: 新闻媒体文章页补录的嵌入视频原平台链接；查不到留空。
    video_url: str = Field(default="", max_length=2000, pattern=r"^(?:https?://.+)?$")

    @field_validator("title_zh")
    @classmethod
    def title_must_contain_chinese(cls, value: str) -> str:
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("title_zh 必须包含中文")
        return value


class AgentSelftestHostReport(StrictModel):
    app_name: str
    model_name: str


class AgentSelftestResult(StrictModel):
    echo_nonce: str
    translated: str
    host_report: AgentSelftestHostReport

    @field_validator("translated")
    @classmethod
    def translated_must_contain_chinese(cls, value: str) -> str:
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("translated 必须包含中文")
        return value


class CandidateEnrichBatchResult(StrictModel):
    """schema_version=2：一个任务对一批候选逐项回写。"""

    items: list[CandidateEnrichItem] = Field(min_length=1, max_length=20)
    notes: str = ""


class CandidateAssistItem(StrictModel):
    candidate_id: str = Field(min_length=1)
    verdict: Literal["SELECT", "REJECT", "HUMAN"]
    confidence: Literal["high", "medium", "low"]
    priority: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=1, max_length=5)
    risks: list[str] = Field(default_factory=list, max_length=5)


class CandidateAssistResult(StrictModel):
    items: list[CandidateAssistItem] = Field(min_length=1, max_length=20)
    summary: str = ""


class AssistantIssue(StrictModel):
    severity: Literal["blocker", "warning", "info"]
    message: str = Field(min_length=1)
    timecode_sec: float | None = Field(default=None, ge=0)


class ManualAssistResult(StrictModel):
    verdict: Literal["APPROVE", "REVISE", "HUMAN", "READY", "BLOCKED"]
    confidence: Literal["high", "medium", "low"]
    summary: str = Field(min_length=1)
    reasons: list[str] = Field(min_length=1, max_length=8)
    issues: list[AssistantIssue] = Field(default_factory=list, max_length=30)
    checklist: list[str] = Field(default_factory=list, max_length=20)


class RecoveryFailureEnvelope(StrictModel):
    """The bounded, server-built incident context supplied to a recovery Agent."""

    failure_class: Literal[
        "BUSINESS_QC",
        "TASK_CONTRACT",
        "CONTEXT_MISSING",
        "TRANSIENT_EXECUTOR",
        "RIGHTS_OR_PUBLISHING",
        "UNKNOWN",
    ]
    code: str = Field(min_length=1, max_length=120)
    message: str = Field(default="", max_length=4000)
    source_task_id: str = Field(min_length=1, max_length=100)
    source_task_type: str = Field(min_length=1, max_length=80)
    source_attempt: int = Field(default=0, ge=0)
    retryable: bool = False
    validation_errors: list[dict[str, object]] = Field(default_factory=list, max_length=30)


class RecoveryPlan(StrictModel):
    """Only safe recovery decisions may leave the assistant task."""

    failure_class: Literal[
        "BUSINESS_QC",
        "TASK_CONTRACT",
        "CONTEXT_MISSING",
        "TRANSIENT_EXECUTOR",
        "RIGHTS_OR_PUBLISHING",
        "UNKNOWN",
    ]
    action: Literal[
        "retry_same_task",
        "reroute_source_discovery",
        "create_repair_task",
        "repair_task_context",
        "rebuild_bundle",
        "reroute_to_role",
        "mark_recovery_blocked",
        "escalate_to_human",
    ]
    target_task_type: (
        Literal[
            "content_research",
            "source_discovery",
            "scene_analyze",
            "short_script",
            "script_qc",
            "visual_plan",
            "visual_plan_qc",
            "asset_generate_image",
            "asset_generate_video",
            "asset_qc",
            "content_qc",
            "publish_prepare",
        ]
        | None
    ) = None
    reason: str = Field(min_length=1, max_length=2000)
    must_change: list[str] = Field(default_factory=list, max_length=20)
    must_preserve: list[str] = Field(default_factory=list, max_length=20)
    must_not_do: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    acceptance_checks: list[str] = Field(default_factory=list, max_length=20)
    reviewer_note: str = Field(default="", max_length=4000)


class SourceRef(StrictModel):
    url: str
    title: str
    source_type: Literal["official", "primary", "secondary"]


class VerifiedClaim(StrictModel):
    id: str
    claim: str
    source_urls: list[str] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]


class SourceImage(StrictModel):
    """网页原图线索：Agent 只交 URL，下载与登记由确定性执行器完成。"""

    url: str = Field(max_length=2000, pattern=r"^https?://")
    caption: str = Field(min_length=1, max_length=200)
    source_page: str = Field(default="", max_length=2000)


class ContentResearchResult(StrictModel):
    subject: str
    primary_sources: list[SourceRef]
    secondary_sources: list[SourceRef]
    verified_claims: list[VerifiedClaim]
    marketing_claims: list[str]
    uncertain_claims: list[str]
    limitations: list[str]
    recommended_angle: str
    recommended_verdict: str
    #: 图文模式的画面素材来源；与 verified_claims 相关的配图，可为空。
    source_images: list[SourceImage] = Field(default_factory=list, max_length=30)


class MediaSourceAssistResult(StrictModel):
    """下载失败内容的视频源补录建议；不改任何业务状态。

    `rights_risk` / `rights_note` 仅为旧任务结果保留的兼容字段；新任务不再要求
    Agent 判断或返回这类信息，平台也不会据此做任何决定。
    """

    verdict: Literal["found", "not_found", "rights_risk"]
    suggested_video_url: str | None = Field(default=None, max_length=2000)
    #: Agent 浏览器会话中实际提取到的临时媒体地址；平台只用于本次立即取源。
    direct_video_url: str | None = Field(default=None, max_length=2000)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    rights_note: str = Field(default="", max_length=1000)

    @field_validator("suggested_video_url", mode="before")
    @classmethod
    def blank_url_is_none(cls, value: object) -> object:
        return normalize_blank_optional(value)

    @field_validator("direct_video_url", mode="before")
    @classmethod
    def blank_direct_url_is_none(cls, value: object) -> object:
        return normalize_blank_optional(value)

    @model_validator(mode="after")
    def found_requires_url(self) -> MediaSourceAssistResult:
        if self.verdict == "found" and not (self.suggested_video_url or "").startswith(
            ("http://", "https://")
        ):
            raise ValueError("verdict 为 found 时必需给出 http/https 的 suggested_video_url")
        if self.direct_video_url:
            parsed = urlparse(self.direct_video_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("direct_video_url 必须是 http/https 地址")
            if not parsed.path.casefold().endswith((".mp4", ".m3u8")):
                raise ValueError("direct_video_url 只能填写真实的 .mp4 或 .m3u8 直链")
        if self.verdict == "not_found" and (self.suggested_video_url or self.direct_video_url):
            raise ValueError("verdict 为 not_found 时不得填写视频地址")
        return self


class SceneAnalysis(StrictModel):
    scene_id: str
    description: str
    visual_quality: int = Field(ge=0, le=10)
    information_value: int = Field(ge=0, le=10)
    recommended_usage: str


class SceneAnalyzeResult(StrictModel):
    scenes: list[SceneAnalysis]


class VisualRequirement(StrictModel):
    purpose: str
    keywords: list[str]
    preferred_source: str


_SCRIPT_STAGE_QUALIFIER = re.compile(
    r"(?:众筹中(?:的)?(?:计划)?|计划中的|尚未(?:量产|交付)的)(?:产品|设备|方案|项目)"
)

_SCRIPT_COPY_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "项目阶段或可得性",
        re.compile(
            r"众筹|预售|预购|预订|未上市|尚未上市|未发售|尚未发售|交付|发货|现货|缺货|"
            r"尚未量产|计划中的(?:产品|设备|方案|项目)|"
            r"(?:预计|计划).{0,12}(?:上市|发售|交付|发货)"
        ),
    ),
    (
        "认证、平台状态或兼容限制",
        re.compile(
            r"认证|Google\s*Play|应用商店|商店可用|运营商|"
            r"(?:频段).{0,8}(?:核对|兼容|适配)|"
            r"(?:仍在|还在|尚未|未完成|申请中|进行中).{0,8}(?:测试|申请)|"
            r"(?:测试|申请).{0,8}(?:仍在|还在|尚未|未完成|进行中)"
        ),
    ),
    (
        "价格、限制或风险提示",
        re.compile(r"价格|售价|限制|局限|风险提示|项目方宣称|官方表示"),
    ),
    (
        "未证实或不确定表达",
        re.compile(
            r"有待(?:证实|确认|验证)|待(?:证实|确认|验证)|未证实|"
            r"(?:不|不能|无法|难以|尚不).{0,5}(?:确定|证实|确认|下定论|当定论)|"
            r"据称|据说|传闻|可能|或许|似乎|有望"
        ),
    ),
    (
        "购买或观望建议",
        re.compile(
            r"(?:建议|推荐|可以|值得).{0,8}(?:观望|等待|关注|等到|下单|购买)|"
            r"(?:先别急着|先别|不要急着).{0,8}(?:下单|购买|等待)|追求现货"
        ),
    ),
)


def _script_copy_forbidden_field(value: object) -> tuple[str, list[str]] | None:
    def find(item: object, path: str) -> tuple[str, list[str]] | None:
        if isinstance(item, str):
            categories = [
                label for label, pattern in _SCRIPT_COPY_FORBIDDEN_PATTERNS if pattern.search(item)
            ]
            if categories:
                return path, categories
        elif isinstance(item, dict):
            for key, nested in item.items():
                if match := find(nested, f"{path}.{key}" if path else str(key)):
                    return match
        elif isinstance(item, (list, tuple)):
            for index, nested in enumerate(item):
                if match := find(nested, f"{path}[{index}]"):
                    return match
        return None

    return find(value, "")


class OpeningSpec(StrictModel):
    """结构化开场信息；默认值保持旧任务结果可读，新任务由提示词要求填写。

    `product_identity` 是来源中的正式产品名/品牌/型号，不是泛化类别；
    `special_value` 与 `payoff_line` 负责承载功能和价值表达。
    """

    product_identity: str = ""
    special_value: str = ""
    hook_line: str = ""
    payoff_line: str = ""
    scene: str = ""


class RetentionBeat(StrictModel):
    at_sec: float = Field(ge=0)
    beat: str = Field(min_length=1, max_length=200)
    kind: Literal["HOOK", "PAYOFF", "BENEFIT", "PROOF", "FACT", "CAVEAT", "CTA", "INFO"] = "INFO"


class ShortScriptResult(StrictModel):
    # Complete/validate responses stringify these errors; do not echo submitted copy or secrets.
    model_config = ConfigDict(hide_input_in_errors=True)

    title_options: list[str] = Field(min_length=3, max_length=3)
    hook_type: Literal["number_shock", "contrast", "open_loop", "conflict", "identity"]
    bgm_mood: Literal["upbeat_product", "tech", "warm", "hype", "none"]
    opening: OpeningSpec = Field(default_factory=OpeningSpec)
    retention_beats: list[RetentionBeat] = Field(min_length=2, max_length=30)
    cta: str = Field(min_length=1, max_length=100)
    cover_title: str = Field(min_length=5, max_length=9)
    cover_visual_brief: str = Field(min_length=1, max_length=60)
    body: str
    verdict: str
    full_narration: str
    # 业务边界由任务输入中的运行时设置决定；这里仅保留绝对安全边界。
    estimated_duration_sec: float = Field(gt=0, le=600)
    claims_used: list[str] = Field(min_length=1, max_length=3)
    visual_requirements: list[VisualRequirement]

    @field_validator("full_narration")
    @classmethod
    def reject_catalog_conjunctions(cls, value: str) -> str:
        tokens = ("还能", "还带", "还支持", "同时", "此外")
        if sum(value.count(token) for token in tokens) >= 4:
            raise ValueError(
                "面向观众的脚本不得写成说明书腔：还能/还带/还支持/同时/此外合计不得超过 3 次"
            )
        return value

    @model_validator(mode="after")
    def validate_retention_contract(self) -> ShortScriptResult:
        ordered = sorted(self.retention_beats, key=lambda item: item.at_sec)
        if ordered != self.retention_beats:
            raise ValueError("retention_beats 必须按 at_sec 升序")
        if ordered[0].at_sec > 3 or ordered[1].at_sec > 8:
            raise ValueError("retention_beats 必须覆盖 0-3 秒开场与 3-8 秒钩子兑现")
        if any(right.at_sec - left.at_sec > 8 for left, right in pairwise(ordered)):
            raise ValueError("retention_beats 间隔不得超过 8 秒")
        if self.estimated_duration_sec - ordered[-1].at_sec > 8:
            raise ValueError("最后一个 retention_beat 距结束不得超过 8 秒")
        narration_end = self.full_narration.rstrip("。！？!? ")
        cta_end = self.cta.rstrip("。！？!? ")
        if not narration_end.endswith(cta_end):
            raise ValueError("full_narration 末尾必须包含 cta")
        identity = self.opening.product_identity.strip()
        if (
            identity
            and identity not in self.full_narration
            and not any(identity in title for title in self.title_options)
        ):
            raise ValueError(
                "opening.product_identity 必须在 full_narration 或 title_options 中出现"
            )
        return self

    @model_validator(mode="after")
    def reject_forbidden_copy(self) -> ShortScriptResult:
        payload = self.model_dump()
        qualifiers = list(_SCRIPT_STAGE_QUALIFIER.finditer(self.full_narration))
        if qualifiers:
            qualifier = qualifiers[0]
            sentences = [
                span
                for span in re.finditer(r"[^。！？!?]+[。！？!?]?", self.full_narration)
                if span.group().rstrip("。！？!?").strip()
            ]
            sentence = next(
                span for span in sentences if span.start() <= qualifier.start() < span.end()
            )
            if (
                len(qualifiers) != 1
                or sentence is sentences[0]
                or len(sentence.group().strip("。！？!? ")) > 28
            ):
                raise ValueError(
                    "full_narration：阶段事实限定仅允许开场之后的短介绍句中出现一次"
                    "（句长不超过28字）；仅修正该字段中的违规限定"
                )
            # Syntax only: source support and factual accuracy still require Agent QC.
            payload["full_narration"] = (
                self.full_narration[: qualifier.start()] + self.full_narration[qualifier.end() :]
            )
        forbidden = _script_copy_forbidden_field(payload)
        if forbidden:
            path, categories = forbidden
            raise ValueError(
                f"{path}：面向观众的脚本不得出现项目状态、可得性、限制、"
                "未证实/不确定或购买观望表达："
                + "、".join(categories)
                + "；仅修正被标字段，保留 full_narration 中合法的单次阶段事实限定"
            )
        return self


class QCIssue(StrictModel):
    severity: Literal["error", "warning"]
    field: str
    message: str
    segment_index: int | None = Field(default=None, ge=0)


class ScriptQCResult(StrictModel):
    passed: bool
    issues: list[QCIssue]
    suggested_revision: dict[str, str] | None = None


#: 服务端强制注入的负面清单基础项（三组）；Agent 只能追加，不能删减。
#: 表述为自行组织的中文，不复制任何 GPL 项目的 negative_prompt 字符串。
IMAGE_BASE_FORBIDDEN_ELEMENTS: tuple[str, ...] = (
    # 低质感
    "廉价 CGI 质感",
    "游戏引擎实时画面感",
    "喷绘广告质感",
    # 文字与标记
    "任何文字、数字与字母",
    "水印、置标与 logo 标记",
    "UI 图标与界面元素",
    # 项目专有项
    "霓虹光与发光描边",
    "悬浮无支撑的物体",
    "镜面反射地面",
    "紫蓝类渐变背景",
    "镜头光晕与体积光",
)

#: style_direction 中命中即拒的项目禁用效果关键词。
STYLE_DIRECTION_BANNED_WORDS: tuple[str, ...] = ("发光", "霓虹", "光晕")


class AssetRequest(StrictModel):
    """生图请求：意图声明由 visual_plan 填写，平台硬约束由服务端注入或强校验。"""

    # 意图声明（visual_plan 填写）
    information: str = Field(min_length=1, max_length=300)
    style_direction: str = Field(min_length=1, max_length=200)
    subject: str = Field(min_length=1, max_length=500)
    #: 图文模式的参考重绘：指向已下载的网页原图 artifact；纯生图留空。
    reference_artifact_id: str | None = Field(default=None, pattern=r"^asset_[a-f0-9]{32}$")
    # 平台硬约束
    text_rendering_enabled: bool = False
    forbidden_elements: list[str] = Field(default_factory=list, max_length=60)
    aspect_ratio: str = Field(pattern=r"^\d+(?:\.\d+)?:\d+(?:\.\d+)?$")
    min_width: int = Field(default=1080, gt=0, le=16384)
    min_height: int = Field(default=1920, gt=0, le=16384)
    #: 仅支持 seed 的工具填写；gpt-image 系列留空，风格一致性用参考图机制。
    seed: int | None = Field(default=None, ge=0)

    @field_validator("seed", mode="before")
    @classmethod
    def blank_seed_is_none(cls, value: object) -> object:
        return normalize_blank_optional(value)

    @field_validator("reference_artifact_id", mode="before")
    @classmethod
    def blank_reference_is_none(cls, value: object) -> object:
        return normalize_blank_optional(value)

    @field_validator("text_rendering_enabled")
    @classmethod
    def text_rendering_must_stay_off(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "text_rendering_enabled 必须为 false：图片一律不含文字、数字与单位，"
                "参数与数据由字幕与旁白表达"
            )
        return value

    @field_validator("style_direction")
    @classmethod
    def style_direction_bans_project_effects(cls, value: str) -> str:
        hits = [word for word in STYLE_DIRECTION_BANNED_WORDS if word in value]
        if hits:
            raise ValueError(f"style_direction 不得包含项目禁用效果：{'、'.join(hits)}")
        return value

    @model_validator(mode="after")
    def inject_base_forbidden_elements(self) -> AssetRequest:
        # 服务端基础项无法删减：Agent 提交的列表只作追加项合并。
        merged = list(IMAGE_BASE_FORBIDDEN_ELEMENTS)
        merged.extend(item for item in self.forbidden_elements if item not in merged)
        self.forbidden_elements = merged
        return self


class SourceRedaction(StrictModel):
    """A normalized rectangle to cover on the unmodified source media."""

    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)
    width: float = Field(gt=0, le=1)
    height: float = Field(gt=0, le=1)
    mode: Literal["black"] = "black"

    @model_validator(mode="after")
    def validate_bounds(self) -> SourceRedaction:
        values = (self.x, self.y, self.width, self.height)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("source_redactions 坐标和尺寸必须是有限值")
        if self.x + self.width > 1 + 1e-9 or self.y + self.height > 1 + 1e-9:
            raise ValueError("source_redactions 矩形必须完全落在 0..1 范围内")
        return self


class VisualSegment(StrictModel):
    narration_start_sec: float = Field(ge=0)
    narration_end_sec: float = Field(gt=0)
    scene_id: str | None = None
    asset_id: str | None = None
    asset_request: AssetRequest | None = None
    source_start_sec: float | None = Field(default=None, ge=0)
    source_end_sec: float | None = Field(default=None, ge=0)
    source_redactions: list[SourceRedaction] = Field(default_factory=list)
    layout: Literal["center_crop", "blur_background", "fit", "image_card", "screen_capture"]
    purpose: str
    motion: Literal[
        "ken_burns_in",
        "ken_burns_out",
        "pan_left",
        "pan_right",
        "focus_product",
        "hold",
    ] = "ken_burns_in"
    transition: Literal["hard_cut", "short_fade"] = "hard_cut"
    focus_x: float = Field(default=0.5, ge=0, le=1)
    focus_y: float = Field(default=0.5, ge=0, le=1)

    @model_validator(mode="after")
    def validate_reference(self) -> VisualSegment:
        references = [
            self.scene_id is not None,
            self.asset_id is not None,
            self.asset_request is not None,
        ]
        if sum(references) != 1:
            raise ValueError("scene_id、asset_id、asset_request 必须且只能填写一个")
        if self.asset_request and self.layout != "image_card":
            raise ValueError("asset_request 只能使用 image_card 布局")
        return self


#: 单条内容生图段的 Schema 硬上限；业务上限（footage 2 段 / image_only 预算）由服务端校验。
MAX_GENERATED_IMAGE_SEGMENTS = 8


class VisualPlanResult(StrictModel):
    segments: list[VisualSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def limit_generated_assets(self) -> VisualPlanResult:
        requests = [
            segment.asset_request for segment in self.segments if segment.asset_request is not None
        ]
        if len(requests) > MAX_GENERATED_IMAGE_SEGMENTS:
            raise ValueError(f"全片 asset_request 不得超过 {MAX_GENERATED_IMAGE_SEGMENTS} 段")
        directions = {request.style_direction for request in requests}
        if len(directions) > 1:
            raise ValueError("同一条内容的全部 asset_request 必须声明同一 style_direction")
        references = [
            request.reference_artifact_id for request in requests if request.reference_artifact_id
        ]
        if len(references) != len(set(references)):
            raise ValueError("同一张原图至多重绘一次：reference_artifact_id 不得重复")
        return self


class GenericQCResult(StrictModel):
    passed: bool
    issues: list[QCIssue]


CONTENT_ACCEPTANCE_CHECKS = (
    "facts",
    "rights",
    "core_demonstration",
    "visual_narration",
    "subtitles",
    "audio",
    "pacing",
    "identity_and_payoff",
    "technical",
)


class AcceptanceCheck(StrictModel):
    passed: bool
    evidence: str = Field(min_length=1, max_length=2000)

    @field_validator("evidence")
    @classmethod
    def evidence_not_blank(cls, value):
        if not value.strip():
            raise ValueError("acceptance evidence cannot be blank")
        return value


class ContentAcceptanceResult(GenericQCResult):
    contract_version: Literal[2]
    confidence: Literal["high", "medium", "low"]
    subject: dict[str, str]
    checks: dict[str, AcceptanceCheck]

    @model_validator(mode="after")
    def complete_acceptance(self):
        if set(self.checks) != set(CONTENT_ACCEPTANCE_CHECKS):
            raise ValueError("all content acceptance checks must be evidenced exactly once")
        if self.passed and (
            self.confidence != "high"
            or not all(item.passed for item in self.checks.values())
            or any(item.severity == "error" for item in self.issues)
        ):
            raise ValueError("acceptance requires high confidence and every hard check passed")
        return self


class GeneratedImageResult(StrictModel):
    artifact_id: str = Field(pattern=r"^asset_[a-f0-9]{32}$")
    tool_name: str
    prompt_summary: str
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    contains_real_person: bool = False
    contains_brand: bool = False
    contains_text: bool = False
    contains_watermark: bool = False


class GeneratedImageItem(GeneratedImageResult):
    """单图结果加段落对应关系：与下发的 requests[].segment_index 一一对应。"""

    segment_index: int = Field(ge=0)


class GeneratedImageSetResult(StrictModel):
    """schema_version=1（V8）：一条内容的全部插图由单个任务一次产出。"""

    images: list[GeneratedImageItem] = Field(min_length=1, max_length=MAX_GENERATED_IMAGE_SEGMENTS)
    style_notes: str = ""

    @model_validator(mode="after")
    def segment_indexes_must_be_unique(self) -> GeneratedImageSetResult:
        indexes = [image.segment_index for image in self.images]
        if len(indexes) != len(set(indexes)):
            raise ValueError("images[].segment_index 不得重复")
        return self


class GeneratedVideoResult(StrictModel):
    artifact_id: str = Field(pattern=r"^asset_[a-f0-9]{32}$")
    tool_name: str
    prompt_summary: str
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    duration_sec: float = Field(gt=0, le=300)
    contains_real_person: bool = False
    contains_brand: bool = False
    contains_text: bool = False
    contains_watermark: bool = False


class PublishPrepareResult(StrictModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(max_length=30)
    cover_artifact_id: str | None = None
    compliance_notes: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def title_must_contain_chinese(cls, value: str) -> str:
        # 发布面向中文短视频平台；产品名等专有名词可保留原文，但标题主体必须为中文。
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("title 必须为面向中文短视频平台的中文标题")
        return value


class PublishResult(StrictModel):
    """结果由发布 Agent 回传；外部发布必须带平台证据。"""

    status: Literal["PUBLISHED", "FAILED", "UNKNOWN", "NEEDS_USER"]
    platform: str = Field(min_length=1, max_length=40)
    title: str = Field(default="", max_length=120)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    platform_fields: dict[str, object] = Field(default_factory=dict)
    external_id: str = Field(default="", max_length=300)
    external_url: str = Field(default="", max_length=2000)
    published_at: datetime | None = None
    evidence_artifact_id: str = Field(default="", max_length=100)
    message: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def published_requires_evidence(self) -> PublishResult:
        if self.status == "PUBLISHED" and not (
            self.external_id.strip() or self.external_url.strip()
        ):
            raise ValueError("PUBLISHED 必须返回 external_id 或 external_url")
        if self.status == "PUBLISHED" and not self.evidence_artifact_id.strip():
            raise ValueError("PUBLISHED 必须返回 evidence_artifact_id")
        return self


RESULT_MODELS: dict[TaskType, type[StrictModel]] = {
    TaskType.AGENT_SELFTEST: AgentSelftestResult,
    TaskType.SOURCE_DISCOVERY: SourceDiscoveryResult,
    TaskType.CANDIDATE_ENRICH: CandidateEnrichBatchResult,
    TaskType.CANDIDATE_ASSIST: CandidateAssistResult,
    TaskType.CONTENT_RESEARCH: ContentResearchResult,
    TaskType.SCENE_ANALYZE: SceneAnalyzeResult,
    TaskType.SHORT_SCRIPT: ShortScriptResult,
    TaskType.SCRIPT_QC: ScriptQCResult,
    TaskType.VISUAL_PLAN: VisualPlanResult,
    TaskType.VISUAL_PLAN_QC: GenericQCResult,
    TaskType.CONTENT_QC: GenericQCResult,
    TaskType.ASSET_GENERATE_IMAGE: GeneratedImageSetResult,
    TaskType.ASSET_QC: GenericQCResult,
    TaskType.ASSET_GENERATE_VIDEO: GeneratedVideoResult,
    TaskType.PUBLISH_PREPARE: PublishPrepareResult,
    TaskType.DOUYIN_PUBLISH: PublishResult,
    TaskType.SCRIPT_REVIEW_ASSIST: ManualAssistResult,
    TaskType.FINAL_REVIEW_ASSIST: ManualAssistResult,
    TaskType.PUBLISH_REVIEW_ASSIST: ManualAssistResult,
    TaskType.MEDIA_SOURCE_ASSIST: MediaSourceAssistResult,
    TaskType.RECOVERY: RecoveryPlan,
}


def result_model_for(task_type: TaskType, schema_version: str = "1") -> type[StrictModel]:
    if task_type == TaskType.CONTENT_QC and schema_version == "2":
        return ContentAcceptanceResult
    return RESULT_MODELS[task_type]


def schema_for(task_type: TaskType, schema_version: str = "1") -> dict:
    return result_model_for(task_type, schema_version).model_json_schema()


#: 各任务类型派发时实际组装的 input 键（含上游结果物化进来的字段）。
#: 提示词 lint 的输入侧契约源（BUG-6）：模板引用输入字段不应被当作笔误告警。
INPUT_FIELDS: dict[TaskType, frozenset[str]] = {
    TaskType.AGENT_SELFTEST: frozenset({"nonce", "phrase", "target_role"}),
    TaskType.SOURCE_DISCOVERY: frozenset(
        {
            "source_id",
            "collection_run_id",
            "direction_key",
            "name",
            "url",
            "scope",
            "max_candidates",
            "history",
            "direction_candidates",
            "already_produced",
            "recent_searches",
        }
    ),
    TaskType.CANDIDATE_ENRICH: frozenset(
        {
            "candidate_id",
            "title",
            "source_url",
            "source_account",
            "source_summary",
            "heat_score",
        }
    ),
    TaskType.CANDIDATE_ASSIST: frozenset(
        {
            "candidate_id",
            "title",
            "summary",
            "score",
            "suggested_hook",
            "source_account",
            "source_url",
            "heat_score",
        }
    ),
    TaskType.CONTENT_RESEARCH: frozenset(
        {
            "content_id",
            "candidate",
            "transcript",
            "production_mode",
            "source_url",
            "image_only",
            "footage",
        }
    ),
    TaskType.SCENE_ANALYZE: frozenset(
        {
            "content_id",
            "candidate",
            "scenes",
            "context_files",
            "batch_index",
            "batch_count",
            "transcript",
            "scene_id",
            "start_sec",
            "end_sec",
            "duration_sec",
            "thumbnail",
        }
    ),
    TaskType.SHORT_SCRIPT: frozenset(
        {
            "research",
            "scene_analysis",
            "transcript",
            "target_duration_sec",
            "style",
            "reviewer_note",
            "previous_script",
            "allowed_claim_ids",
            "min_video_seconds",
            "max_video_seconds",
            "production_mode",
            "verified_claims",
            "marketing_claims",
            "uncertain_claims",
            "recommended_verdict",
            "scene_id",
            "source_images",
            "artifact_id",
            "ai_generate",
            "image_only",
            "footage",
            "suggested_hook",
            "recommended_angle",
        }
    ),
    TaskType.SCRIPT_QC: frozenset(
        {
            "script",
            "research",
            "min_video_seconds",
            "max_video_seconds",
            "verified_claims",
            "claims_used",
            "hook_type",
            "bgm_mood",
            "estimated_duration_sec",
            "full_narration",
            "cover_title",
            "opening",
            "scene",
        }
    ),
    TaskType.VISUAL_PLAN: frozenset(
        {
            "script",
            "scene_analysis",
            "scenes",
            "voice_duration_sec",
            "allowed_layouts",
            "allowed_asset_ids",
            "asset_image_enabled",
            "qc_issues",
            "previous_plan",
            "production_mode",
            "image_only_gen_budget",
            "source_images",
            "allowed_reference_artifact_ids",
            "context_files",
            "artifact_id",
            "artifact_url",
            "start_sec",
            "end_sec",
            "visual_quality",
            "information_value",
            "image_only",
            "footage",
            "opening",
            "hook_line",
            "scene",
            "fit",
        }
    ),
    TaskType.VISUAL_PLAN_QC: frozenset(
        {
            "visual_plan",
            "script",
            "scenes",
            "voice_duration_sec",
            "allowed_layouts",
            "allowed_asset_ids",
            "context_files",
            "qc_issues",
            "previous_plan",
            "segments",
            "scene_id",
            "asset_id",
            "asset_request",
            "source_redactions",
            "layout",
            "start_sec",
            "end_sec",
            "source_start_sec",
            "source_end_sec",
            "narration_start_sec",
            "narration_end_sec",
            "image_card",
            "opening",
            "hook_line",
            "scene",
            "fit",
        }
    ),
    TaskType.CONTENT_QC: frozenset(
        {
            "script",
            "research",
            "subtitles",
            "technical_qc",
            "video_url",
            "video_artifact_id",
            "context_files",
            "verified_claims",
        }
    ),
    TaskType.ASSET_GENERATE_IMAGE: frozenset(
        {
            "requests",
            "segment_index",
            "asset_request",
            "asset_purpose",
            "script",
            "previous_artifact_ids",
            "qc_issues",
            "reference_artifact_url",
            "reference_artifact_id",
            "context_files",
            "information",
            "style_direction",
            "subject",
            "aspect_ratio",
            "min_width",
            "min_height",
            "forbidden_elements",
            "artifact_id",
        }
    ),
    TaskType.ASSET_QC: frozenset(
        {
            "assets",
            "segment_index",
            "artifact_id",
            "artifact_url",
            "request",
            "generation",
            "script",
            "context_files",
            "previous_issues",
            "forbidden_elements",
        }
    ),
    TaskType.ASSET_GENERATE_VIDEO: frozenset(
        {"script", "segment_index", "asset_request", "artifact_id", "forbidden_elements"}
    ),
    TaskType.PUBLISH_PREPARE: frozenset({"script", "research", "cover_artifact_id"}),
    TaskType.DOUYIN_PUBLISH: frozenset(
        {
            "platform",
            "account",
            "account_id",
            "account_name",
            "publish_attempt",
            "retry_of",
            "video_artifact_id",
            "cover_artifact_id",
            "video_sha256",
            "content",
            "candidate",
            "script",
            "research",
            "publish_prepare",
            "artifacts",
            "context_files",
            "publish_policy",
        }
    ),
    TaskType.SCRIPT_REVIEW_ASSIST: frozenset({"script", "script_qc", "research", "timecode_sec"}),
    TaskType.FINAL_REVIEW_ASSIST: frozenset(
        {
            "script",
            "video_url",
            "video_artifact_id",
            "technical_qc",
            "content_qc",
            "timecode_sec",
        }
    ),
    TaskType.PUBLISH_REVIEW_ASSIST: frozenset(
        {
            "script",
            "research",
            "publish_prepare",
            "publish_policy",
            "publish_artifact_id",
            "publish_video_url",
            "cover_artifact_id",
            "cover_image_url",
            "context_files",
        }
    ),
    TaskType.MEDIA_SOURCE_ASSIST: frozenset(
        {
            "content_id",
            "title",
            "original_title",
            "source_url",
            "video_url",
            "source_account",
            "failure_error_code",
            "failure_message",
        }
    ),
    TaskType.RECOVERY: frozenset(
        {
            "failure",
            "lineage",
            "current_input",
            "current_result",
            "repair_history",
            "policy",
            "allowed_actions",
            "source_context",
        }
    ),
}


def declared_schema_names(schema: dict) -> set[str]:
    """递归收集输出 Schema 的属性名与枚举取值（含 `$defs` / `items` / `anyOf`）。"""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                names.update(str(key) for key in properties)
            enum_values = node.get("enum")
            if isinstance(enum_values, list):
                names.update(str(value) for value in enum_values if isinstance(value, str))
            constant = node.get("const")
            if isinstance(constant, str):
                names.add(constant)
            for key, value in node.items():
                if key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
                    for child in value.values():
                        walk(child)
                elif key in {"items", "anyOf", "oneOf", "allOf", "prefixItems"}:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def declared_field_names(task_type: TaskType, schema_version: str = "1") -> set[str]:
    """任务契约内已声明的全部名字：输出属性 + 枚举值 + 输入字段。"""
    return declared_schema_names(schema_for(task_type, schema_version)) | set(
        INPUT_FIELDS.get(task_type, frozenset())
    )
