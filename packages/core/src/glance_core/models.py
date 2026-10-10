from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, Text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class TaskType(StrEnum):
    AGENT_SELFTEST = "agent_selftest"
    SOURCE_DISCOVERY = "source_discovery"
    CANDIDATE_ENRICH = "candidate_enrich"
    CANDIDATE_ASSIST = "candidate_assist"
    CONTENT_RESEARCH = "content_research"
    SCENE_ANALYZE = "scene_analyze"
    SHORT_SCRIPT = "short_script"
    SCRIPT_QC = "script_qc"
    VISUAL_PLAN = "visual_plan"
    VISUAL_PLAN_QC = "visual_plan_qc"
    CONTENT_QC = "content_qc"
    ASSET_GENERATE_IMAGE = "asset_generate_image"
    ASSET_QC = "asset_qc"
    ASSET_GENERATE_VIDEO = "asset_generate_video"
    PUBLISH_PREPARE = "publish_prepare"
    DOUYIN_PUBLISH = "douyin_publish"
    SCRIPT_REVIEW_ASSIST = "script_review_assist"
    FINAL_REVIEW_ASSIST = "final_review_assist"
    PUBLISH_REVIEW_ASSIST = "publish_review_assist"
    MEDIA_SOURCE_ASSIST = "media_source_assist"
    RECOVERY = "recovery"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    LEASED = "LEASED"
    SUBMITTED = "SUBMITTED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CandidateStatus(StrEnum):
    NEW = "NEW"
    ENRICHED = "ENRICHED"
    SELECTED = "SELECTED"
    IGNORED = "IGNORED"


class ContentStatus(StrEnum):
    PREPARING_CONTEXT = "PREPARING_CONTEXT"
    WAITING_AGENT = "WAITING_AGENT"
    SCRIPT_REVIEW = "SCRIPT_REVIEW"
    PRODUCING = "PRODUCING"
    FINAL_REVIEW = "FINAL_REVIEW"
    APPROVED = "APPROVED"
    PACKAGED = "PACKAGED"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"


class ProductionMode(StrEnum):
    """内容制作模式：有原片走 footage，无原片用网页原图与生图混排走 image_only。"""

    FOOTAGE = "footage"
    IMAGE_ONLY = "image_only"


class StageStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"


class ProviderKind(StrEnum):
    EXECUTOR = "EXECUTOR"
    AGENT = "AGENT"


class ProviderStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class PublicationMode(StrEnum):
    MANUAL = "MANUAL"
    EXECUTOR = "EXECUTOR"


class IssueStatus(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class IssueClassification(StrEnum):
    INPUT = "INPUT"
    QUALITY = "QUALITY"
    POLICY = "POLICY"
    UNKNOWN = "UNKNOWN"


class IssueSourceType(StrEnum):
    AGENT_TASK = "AGENT_TASK"
    STAGE_RUN = "STAGE_RUN"
    PUBLICATION = "PUBLICATION"
    POLICY = "POLICY"


class RightsStatus(StrEnum):
    SELF_OWNED = "SELF_OWNED"
    EXPLICIT_LICENSE = "EXPLICIT_LICENSE"
    OFFICIAL_REFERENCE = "OFFICIAL_REFERENCE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"


class SourceType(StrEnum):
    RSS = "RSS"
    YOUTUBE = "YOUTUBE"
    RSSHUB = "RSSHUB"
    REDDIT = "REDDIT"
    PRODUCT_SITE = "PRODUCT_SITE"
    TRENDRADAR = "TRENDRADAR"
    AGENT = "AGENT"


class Source(SQLModel, table=True):
    __tablename__ = "source"

    id: str = Field(default_factory=lambda: new_id("src"), primary_key=True)
    name: str
    source_type: SourceType = Field(index=True)
    url: str
    enabled: bool = Field(default=True, index=True)
    poll_interval_minutes: int = 30
    default_rights_status: RightsStatus = RightsStatus.OFFICIAL_REFERENCE
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    next_poll_at: datetime = Field(default_factory=utc_now, index=True)
    last_polled_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    error_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CollectionRun(SQLModel, table=True):
    __tablename__ = "collection_run"

    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True)
    source_id: str = Field(foreign_key="source.id", index=True)
    status: str = Field(default="RUNNING", index=True)
    discovered_count: int = 0
    created_count: int = 0
    duplicate_count: int = 0
    filtered_count: int = 0
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime = Field(default_factory=utc_now, index=True)
    completed_at: datetime | None = None


class CandidateSignal(SQLModel, table=True):
    __tablename__ = "candidate_signal"

    id: str = Field(default_factory=lambda: new_id("signal"), primary_key=True)
    candidate_id: str = Field(foreign_key="candidate.id", index=True)
    source_id: str = Field(foreign_key="source.id", index=True)
    collection_run_id: str | None = Field(default=None, foreign_key="collection_run.id", index=True)
    heat_score: float = 0
    thumbnail_url: str = ""
    summary: str = Field(default="", sa_column=Column(Text))
    raw_metadata: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    discovered_at: datetime = Field(default_factory=utc_now, index=True)


class Candidate(SQLModel, table=True):
    __tablename__ = "candidate"

    id: str = Field(default_factory=lambda: new_id("cand"), primary_key=True)
    source_type: str = Field(index=True)
    source_id: str = Field(index=True)
    direction_key: str = Field(default="", index=True)
    source_url: str
    video_url: str = ""
    #: 视频直链的稳定来源页；直链本身可能是带签名、会过期的临时凭证。
    video_source_page_url: str = ""
    normalized_url: str = Field(default="", index=True)
    #: Agent 回写的中文标题（来源多为英文，运营列表按此显示）。
    title_zh: str = ""
    assist_advised_at: datetime | None = Field(default=None, index=True)
    source_account: str = ""
    original_title: str
    published_at: datetime | None = None
    category: str = "unknown"
    summary: str = Field(default="", sa_column=Column(Text))
    score: int = 0
    suggested_hook: str = ""
    rights_status: RightsStatus = Field(default=RightsStatus.OFFICIAL_REFERENCE, index=True)
    rights_note: str = Field(default="", sa_column=Column(Text))
    status: CandidateStatus = Field(default=CandidateStatus.NEW, index=True)
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class Content(SQLModel, table=True):
    __tablename__ = "content"

    id: str = Field(default_factory=lambda: new_id("content"), primary_key=True)
    candidate_id: str = Field(foreign_key="candidate.id", unique=True, index=True)
    title: str
    #: 中文标题（取自候选的 title_zh）；列表优先展示，为空时回退 title。
    title_zh: str = ""
    business_status: ContentStatus = Field(default=ContentStatus.PREPARING_CONTEXT, index=True)
    #: 制作模式：image_only 跳过原片获取、转写与场景分析，画面由原图与生图混排。
    production_mode: ProductionMode = Field(default=ProductionMode.FOOTAGE, index=True)
    current_stage: str | None = None
    script_version: int = 0
    template_version: str = "p0-v1"
    content_dir: str
    cover_title_override: str = ""
    error_code: str | None = None
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class StageRun(SQLModel, table=True):
    __tablename__ = "stage_run"

    id: str = Field(default_factory=lambda: new_id("stage"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    stage: str = Field(index=True)
    status: StageStatus = Field(default=StageStatus.PENDING, index=True)
    input_hash: str
    attempt: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = Field(default=None, sa_column=Column(Text))


class Artifact(SQLModel, table=True):
    __tablename__ = "artifact"

    id: str = Field(default_factory=lambda: new_id("asset"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    artifact_type: str = Field(index=True)
    relative_path: str
    sha256: str
    size_bytes: int
    mime_type: str
    producer_stage: str
    created_at: datetime = Field(default_factory=utc_now)


class AgentTask(SQLModel, table=True):
    __tablename__ = "agent_task"

    id: str = Field(default_factory=lambda: new_id("agt"), primary_key=True)
    content_id: str | None = Field(default=None, index=True)
    task_type: TaskType = Field(index=True)
    status: TaskStatus = Field(default=TaskStatus.PENDING, index=True)
    priority: int = Field(default=0, index=True)
    prompt_name: str
    prompt_version: str = "v1"
    schema_version: str = "1"
    instructions: str = Field(sa_column=Column(Text, nullable=False))
    input_data: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    result_data: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    input_hash: str = Field(index=True)
    # 非终止失败任务持有稳定指纹。数据库唯一索引保证多个服务线程/Agent 并发时，
    # 相同输入、指令与 Schema 的任务只会创建一次；取消/失败后释放，允许人工重建。
    dedup_key: str | None = Field(default=None, index=True)
    # 搜索任务使用方向锁；同一 lane 同时只能有一条 PENDING/LEASED 任务。
    lane_key: str | None = Field(default=None, index=True)
    required_role: str = Field(default="", index=True)
    attempt: int = 0
    max_attempts: int = 3
    assigned_agent_id: str | None = Field(default=None, index=True)
    lease_token_hash: str | None = None
    lease_expires_at: datetime | None = Field(default=None, index=True)
    not_before: datetime | None = Field(default=None, index=True)
    attempt_started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now, index=True)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = Field(default=None, sa_column=Column(Text))


class AgentSubmission(SQLModel, table=True):
    __tablename__ = "agent_submission"

    id: str = Field(default_factory=lambda: new_id("sub"), primary_key=True)
    task_id: str = Field(foreign_key="agent_task.id", index=True)
    agent_id: str = Field(index=True)
    attempt: int
    raw_result_path: str
    result_hash: str
    validation_status: str
    validation_errors: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class AgentWorker(SQLModel, table=True):
    __tablename__ = "agent_worker"

    agent_id: str = Field(primary_key=True)
    role_id: str = Field(default="", index=True)
    last_seen_at: datetime = Field(default_factory=utc_now, index=True)
    last_task_id: str | None = None
    app_name: str = ""
    app_version: str = ""
    model_name: str = ""
    model_tier: str = ""
    reasoning_effort: str = ""
    skill_name: str = ""
    skill_version: str = ""
    device_label: str = ""
    protocol_version: str = ""
    self_check: dict[str, str] = Field(default_factory=dict, sa_column=Column(JSON))
    launcher_path: str = ""
    schedule_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill_content_hash: str = ""
    deployment_verified_at: datetime | None = None
    deployment_findings: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    last_claim_status: str = ""
    tasks_completed: int = 0
    tasks_failed: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class TaskLeasePolicy(SQLModel, table=True):
    """Per-task-type lease duration configured from the management console."""

    __tablename__ = "task_lease_policy"

    task_type: str = Field(primary_key=True)
    lease_minutes: int
    updated_at: datetime = Field(default_factory=utc_now)


class CapabilityProvider(SQLModel, table=True):
    __tablename__ = "capability_provider"

    id: str = Field(primary_key=True)
    capability_id: str = Field(index=True)
    name: str
    kind: ProviderKind = Field(index=True)
    role_id: str = Field(default="", index=True)
    enabled: bool = Field(default=True, index=True)
    priority: int = 100
    version: str = ""
    status: ProviderStatus = Field(default=ProviderStatus.UNKNOWN, index=True)
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    last_seen_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ExecutorNode(SQLModel, table=True):
    __tablename__ = "executor_node"

    node_id: str = Field(primary_key=True)
    display_name: str = ""
    version: str = ""
    capabilities: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    runtime_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    max_concurrency: int = 1
    active_jobs: int = 0
    last_seen_at: datetime = Field(default_factory=utc_now, index=True)
    created_at: datetime = Field(default_factory=utc_now)


class PublicationRecord(SQLModel, table=True):
    """一次真实外部发布的记录；可由人工回填或发布 Agent 产生。"""

    __tablename__ = "publication_record"

    id: str = Field(default_factory=lambda: new_id("pub"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    mode: PublicationMode = Field(index=True)
    platform: str = Field(index=True)
    external_url: str = ""
    external_id: str = ""
    status: str = Field(default="RECORDED", index=True)
    note: str = Field(default="", sa_column=Column(Text))
    actor_id: str = ""
    published_at: datetime = Field(default_factory=utc_now, index=True)
    created_at: datetime = Field(default_factory=utc_now)


class ContentIssue(SQLModel, table=True):
    """Structured operator issue; never changes business status by itself."""

    __tablename__ = "content_issue"

    id: str = Field(default_factory=lambda: new_id("issue"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    code: str = Field(index=True)
    classification: IssueClassification = Field(default=IssueClassification.UNKNOWN, index=True)
    source_type: IssueSourceType = Field(default=IssueSourceType.POLICY, index=True)
    source_id: str = ""
    subject_sha256: str = Field(default="", index=True)
    detail_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: IssueStatus = Field(default=IssueStatus.OPEN, index=True)
    opened_at: datetime = Field(default_factory=utc_now, index=True)
    resolved_at: datetime | None = None
    resolved_by: str = ""
    resolved_subject_sha256: str = ""


class ContentRevision(SQLModel, table=True):
    """Immutable snapshot metadata used for review/rework and stale-result guards."""

    __tablename__ = "content_revision"

    id: str = Field(default_factory=lambda: new_id("rev"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    revision_no: int = Field(default=1, index=True)
    revision_type: str = Field(default="WORKFLOW", index=True)
    subject_sha256: str = Field(index=True)
    parent_revision_id: str | None = Field(default=None, index=True)
    task_id: str | None = Field(default=None, index=True)
    snapshot_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    feedback_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    actor_type: str = "human"
    actor_id: str = "admin-console"
    created_at: datetime = Field(default_factory=utc_now, index=True)


class VoiceProfile(SQLModel, table=True):
    """Explicit voice provider profile; provider switching is an admin action."""

    __tablename__ = "voice_profile"

    id: str = Field(default_factory=lambda: new_id("voice"), primary_key=True)
    name: str = Field(index=True)
    provider: str = Field(default="edge", index=True)
    voice: str = "zh-CN-YunxiNeural"
    speed: float = 1.0
    rate: str = "+20%"
    volume: str = "+0%"
    endpoint: str = ""
    enabled: bool = Field(default=True, index=True)
    is_default: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PublicationPreview(SQLModel, table=True):
    """Deterministic publish-package preview with no external side effect."""

    __tablename__ = "publication_preview"

    id: str = Field(default_factory=lambda: new_id("pubprev"), primary_key=True)
    content_id: str = Field(foreign_key="content.id", index=True)
    snapshot_sha256: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_by: str = "admin-console"
    created_at: datetime = Field(default_factory=utc_now, index=True)


class RuntimeSetting(SQLModel, table=True):
    """管理台可改配置的覆盖值；表里只存与 .env 默认值不同的项。"""

    __tablename__ = "runtime_setting"

    key: str = Field(primary_key=True)
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)


class TaskPrompt(SQLModel, table=True):
    """服务端下发给 Agent 的任务指令覆盖；只存与内置默认不同的项。"""

    __tablename__ = "task_prompt"

    task_type: str = Field(primary_key=True)
    instructions: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=utc_now)


class SchedulerOffer(SQLModel, table=True):
    """Short-lived plan pin. Not a lease: task stays PENDING."""

    __tablename__ = "scheduler_offer"

    request_id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    task_type: str = ""
    agent_id: str = ""
    scheduler_id: str = Field(default="codex", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime = Field(index=True)


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_event"

    id: str = Field(default_factory=lambda: new_id("aud"), primary_key=True)
    actor_type: str
    actor_id: str
    entity_type: str
    entity_id: str
    action: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now, index=True)
