from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlmodel import Session, select

from glance_core.models import (
    AgentSubmission,
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    CandidateSignal,
    CandidateStatus,
    CollectionRun,
    Content,
    ContentIssue,
    ContentStatus,
    IssueClassification,
    IssueSourceType,
    IssueStatus,
    ProductionMode,
    PublicationMode,
    PublicationRecord,
    RightsStatus,
    Source,
    StageRun,
    StageStatus,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.normalize import normalize_title, normalize_url
from glance_core.prompts import resolve_instructions
from glance_core.research import direction_key_for, research_history
from glance_core.settings import Settings
from glance_core.task_instructions import _SCRIPT_FACT_FIDELITY_RULE, TASK_INSTRUCTIONS
from glance_core.task_schemas import RecoveryFailureEnvelope, RecoveryPlan
from glance_core.task_service import (
    AgentTaskService,
    TaskConflictError,
    TaskLaneBusyError,
    stable_hash,
)


def handsfree_review_policy() -> dict[str, object]:
    """Return the server-owned facts that make a Handsfree review publish-ready."""
    return {
        "platform_fixed": "douyin",
        "timing": "immediate_when_gates_pass",
        "initiated_by": "handsfree",
        "user_clicked": False,
        "explicit_user_opt_in": True,
    }


def _failed_segment_indexes(issues: list, segment_count: int) -> set[int] | None:
    from glance_core.task_schemas import AssetRequest, QCIssue, VisualSegment

    indexes = set()
    try:
        for raw in issues:
            issue = QCIssue.model_validate(raw)
            if issue.severity != "error":
                continue
            index = raw.get("segment_index")
            if index is not None and type(index) is not int:
                return None
            path = re.fullmatch(
                r"segments\[(0|[1-9][0-9]*)\](?:\.([a-z_]+)(?:\.([a-z_]+))?)?", issue.field
            )
            asset_path = re.fullmatch(
                r"assets\[(0|[1-9][0-9]*)\]\.request\.([a-z_]+)", issue.field
            )
            if path:
                field, nested = path.group(2, 3)
                if field and field not in VisualSegment.model_fields:
                    return None
                if nested and (field != "asset_request" or nested not in AssetRequest.model_fields):
                    return None
                field_index = int(path.group(1))
                if index is not None and index != field_index:
                    return None
                index = field_index
            elif asset_path:
                field_index = int(asset_path.group(1))
                if asset_path.group(2) not in AssetRequest.model_fields or index != field_index:
                    return None
                index = field_index
            elif issue.field not in (VisualSegment.model_fields | AssetRequest.model_fields) and (
                issue.field not in {"assets", "continuity"}
            ):
                return None
            if index is None or not 0 <= index < segment_count:
                return None
            indexes.add(index)
    except (ValueError, TypeError, AttributeError):
        return None
    return indexes


#: 兼容既有导入点：指令正文已移到 task_instructions，这里保持转出。
__all__ = [
    "TASK_INSTRUCTIONS",
    "DuplicateTopicError",
    "WorkflowService",
    "candidate_production_blockers",
    "candidate_video_ready",
    "log_stage",
    "normalize_video_url",
    "parse_platform_domains",
    "url_on_video_platform",
]

logger = logging.getLogger(__name__)

MAX_AUTO_SCRIPT_REWRITES = 3
_SCRIPT_DEFAULT_TARGET_SECONDS = 35
_SCRIPT_DEFAULT_MIN_SECONDS = 20
_SCRIPT_DEFAULT_MAX_SECONDS = 90
_SCRIPT_CHARS_PER_SECOND = 5
# Allow one final bounded repair beyond the legacy two-attempt budget; every
# rewrite still has to pass the existing SCRIPT_QC gate before production.
# 助手建议触发的自动重写必须有固定上限，避免低置信度建议在审核链路中循环。
MAX_AUTO_ASSIST_REVISIONS = 1
SOURCE_RECOVERY_MAX_COOLDOWN = timedelta(hours=1)
RECOVERY_ALLOWED_ACTIONS = frozenset(
    {
        "retry_same_task",
        "reroute_source_discovery",
        "create_repair_task",
        "repair_task_context",
        "rebuild_bundle",
        "reroute_to_role",
        "mark_recovery_blocked",
        "escalate_to_human",
    }
)


def _script_rewrite_note(note: str) -> str:
    return (
        f"{_SCRIPT_FACT_FIDELITY_RULE}\n"
        "局部返工约束优先于下面笼统重写建议：对照 previous_script，仅修改 issue 相关字段及其必要的"
        "一致性联动，不重新创作整篇。未被问题或当前权威证据否定的标题、事实/计划产品限定、"
        "权益表述、opening 和 3-8 秒 payoff 必须保留；body 术语简化不允许顺手删限定或移动兑现。"
        "不得锁住已指出的错误或矛盾字段：有证据的相关修改应说明原因，同步 full_narration、"
        "retention_beats、visual_requirements 与真实时长，仍须完整重新 QC，不把未报错当永久通过。\n"
        f"{note.strip()}"
    ).strip()


def _script_rewrite_duration(
    previous_script: dict | None, script_input: dict | None
) -> tuple[float, int, float, float]:
    """Derive a bounded next-script target from the previous narration length."""
    input_data = script_input if isinstance(script_input, dict) else {}

    def duration_bound(name: str, fallback: float) -> float:
        try:
            value = float(input_data.get(name, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if math.isfinite(value) and value > 0 else fallback

    minimum = duration_bound("min_video_seconds", _SCRIPT_DEFAULT_MIN_SECONDS)
    maximum = max(minimum, duration_bound("max_video_seconds", _SCRIPT_DEFAULT_MAX_SECONDS))
    previous = previous_script if isinstance(previous_script, dict) else {}
    narration = previous.get("full_narration")
    readable_chars = sum(char.isalnum() for char in narration) if isinstance(narration, str) else 0
    target = _SCRIPT_DEFAULT_TARGET_SECONDS
    if readable_chars:
        target = max(target, round(readable_chars / _SCRIPT_CHARS_PER_SECOND))
    return max(minimum, min(maximum, float(target))), readable_chars, minimum, maximum


_RECOVERY_INPUT_FIELDS = frozenset(
    {
        "upstream_task_ids",
        "materialized_context",
        "reviewer_note",
        "previous_script",
        "script_qc_issues",
        "content_qc_issues",
        "previous_qc_task_id",
        "production_mode",
        "target_duration_sec",
        "style",
        "min_video_seconds",
        "max_video_seconds",
        "allowed_claim_ids",
        "allowed_asset_ids",
        "allowed_layouts",
        "allowed_reference_artifact_ids",
        "asset_image_enabled",
        "image_only_gen_budget",
        "voice_duration_sec",
        "scene_id",
        "artifact_id",
    }
)
_SCRIPT_RESULT_FIELDS = (
    "title_options",
    "hook_type",
    "bgm_mood",
    "opening",
    "retention_beats",
    "cta",
    "cover_title",
    "cover_visual_brief",
    "body",
    "verdict",
    "full_narration",
    "estimated_duration_sec",
    "claims_used",
    "visual_requirements",
)
_VISUAL_PLAN_DOWNSTREAM_TASK_TYPES = {
    TaskType.VISUAL_PLAN,
    TaskType.VISUAL_PLAN_QC,
    TaskType.ASSET_GENERATE_IMAGE,
    TaskType.ASSET_GENERATE_VIDEO,
    TaskType.ASSET_QC,
}


class DuplicateTopicError(ValueError):
    def __init__(self, conflicts: list[str]) -> None:
        super().__init__(f"Duplicate topic: recent content exists ({', '.join(conflicts)})")
        self.conflicts = conflicts


def log_stage(
    session: Session,
    content_id: str,
    stage: str,
    fn: Callable[[], Any],
    *,
    input_hash: str = "",
) -> Any:
    """Wrap a deterministic media stage with a RUNNING→SUCCESS/FAILED StageRun record.

    ``attempt`` is part of the operator-facing execution record. Reusing ``1`` for every
    retry made a stage look as if it had only ever run once, which hid the actual source of
    repeat work. Allocate the next attempt for this content/stage before inserting the run.
    """
    previous_runs = session.exec(
        select(StageRun).where(
            StageRun.content_id == content_id,
            StageRun.stage == stage,
        )
    ).all()
    next_attempt = max((int(run.attempt or 0) for run in previous_runs), default=0) + 1
    run = StageRun(
        content_id=content_id,
        stage=stage,
        status=StageStatus.RUNNING,
        input_hash=input_hash,
        attempt=next_attempt,
        started_at=utc_now(),
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    try:
        result = fn()
    except Exception as exc:
        run.status = StageStatus.FAILED
        run.completed_at = utc_now()
        run.error_code = type(exc).__name__
        run.error_message = str(exc)[:2000]
        session.add(run)
        session.commit()
        raise
    run.status = StageStatus.SUCCESS
    run.completed_at = utc_now()
    session.add(run)
    session.commit()
    # 全链路单点的阶段成功可见性：尾日志即可回答“跑到哪个阶段、耗时多久”。
    elapsed = int((run.completed_at - run.started_at).total_seconds())
    logger.info("阶段完成 stage=%s content_id=%s 耗时=%ds", stage, content_id, elapsed)
    return result


def _contains_chinese(value: str | None) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in (value or ""))


def parse_platform_domains(value: str) -> frozenset[str]:
    """把逗号分隔的视频平台域名配置解析为归一化域名集合。"""
    return frozenset(
        item.strip().lower().removeprefix("www.")
        for item in (value or "").split(",")
        if item.strip()
    )


#: 启动默认白名单（不读 .env）：未显式传入配置时的兜底值。
DEFAULT_VIDEO_PLATFORM_DOMAINS: frozenset[str] = parse_platform_domains(
    str(Settings.model_fields["video_platform_domains"].default)
)
_DIRECT_MEDIA_SUFFIXES = (".mp4", ".m4v", ".m3u8", ".webm", ".mkv", ".mov")


def url_on_video_platform(url: str, domains: frozenset[str]) -> bool:
    """URL 的主机名是否命中视频平台白名单（按域名后缀匹配）。"""
    if not url or not domains:
        return False
    host = urlparse(url if "://" in url else f"https://{url}").hostname or ""
    host = host.lower().removeprefix("www.")
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def normalize_video_url(video_url: str, domains: frozenset[str]) -> str:
    """Keep only a playable page or a real media URL in the video field."""
    value = (video_url or "").strip()
    if not value or "://" not in value:
        return value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    if url_on_video_platform(value, domains) or parsed.path.casefold().endswith(
        _DIRECT_MEDIA_SUFFIXES
    ):
        return value
    return ""


def candidate_video_ready(candidate: Candidate, domains: frozenset[str]) -> bool:
    """是否已有可下载的视频源。

    两段式判据（V10 BUG-7）：有效的 `video_url`（视频平台播放页或真实媒体直链）
    即就绪；为空时只有来源页本身落在视频平台白名单里才算就绪。「站点有 yt-dlp
    提取器」不等于「页面有视频」——新闻媒体或 Kickstarter 项目页不能把自身 URL
    重复填入 `video_url`。
    本机文件路径（无 URL scheme）由确定性执行器直读，始终就绪。
    """
    if normalize_video_url(candidate.video_url, domains):
        return True
    source_url = (candidate.source_url or "").strip()
    if source_url and "://" not in source_url:
        return True
    return url_on_video_platform(source_url, domains)


def _safe_video_url_for_audit(url: str) -> str:
    """Drop query credentials when an ephemeral direct URL enters an audit event."""
    parsed = urlparse(url)
    if parsed.path.casefold().endswith((".mp4", ".m4v", ".m3u8", ".webm", ".mkv", ".mov")):
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", "", ""))
    return url


def candidate_production_blockers(
    candidate: Candidate,
    *,
    video_ready: bool,
    production_mode: ProductionMode = ProductionMode.FOOTAGE,
) -> list[str]:
    """候选能否进入制作的单一判据：选中校验、前端展示与自动选题共用。

    返回的阻断原因可直接面向运营展示；空列表表示可选中。
    image_only（图文模式）不需要可下载视频源，只用网页原图与生图混排成片。
    """
    blockers: list[str] = []
    if not video_ready and production_mode != ProductionMode.IMAGE_ONLY:
        blockers.append("先补录视频源再选题")
    return blockers


class WorkflowService:
    def __init__(
        self,
        session: Session,
        *,
        data_dir: Path,
        submission_root: Path,
        lease_minutes: int,
        video_platform_domains: frozenset[str] | None = None,
        recovery_enabled: bool = True,
        recovery_max_rounds: int = 3,
        recovery_same_task_retries: int = 1,
        recovery_unknown_attempts: int = 1,
        recovery_max_parallel: int = 1,
        base_settings: Settings | None = None,
        opening_identity_overlay: bool = False,
    ) -> None:
        self.session = session
        self.base_settings = base_settings
        self.opening_identity_overlay = opening_identity_overlay is True
        self.data_dir = data_dir
        self.tasks = AgentTaskService(session, submission_root, lease_minutes)
        self.recovery_enabled = bool(recovery_enabled)
        self.recovery_max_rounds = max(0, int(recovery_max_rounds))
        self.recovery_same_task_retries = max(0, int(recovery_same_task_retries))
        self.recovery_unknown_attempts = max(0, int(recovery_unknown_attempts))
        self.recovery_max_parallel = max(1, int(recovery_max_parallel))
        self.recovery_capacity_deferred = False
        #: 可选注入：视频平台域名白名单（运行时可在管理台调整）。
        self.video_platform_domains = (
            DEFAULT_VIDEO_PLATFORM_DOMAINS
            if video_platform_domains is None
            else video_platform_domains
        )

    def video_ready(self, candidate: Candidate) -> bool:
        return candidate_video_ready(candidate, self.video_platform_domains)

    @staticmethod
    def image_generation_inputs(settings: Settings) -> dict[str, Any]:
        return {
            "asset_image_enabled": settings.asset_image_enabled,
            "image_only_gen_budget": settings.image_only_gen_budget
            if settings.asset_image_enabled
            else 0,
        }

    def handsfree_candidate_mode(self, candidate: Candidate, settings: Settings) -> ProductionMode:
        if (
            settings.handsfree_enabled
            and not (
                self.video_ready(candidate)
                and candidate.rights_status
                in {RightsStatus.SELF_OWNED, RightsStatus.EXPLICIT_LICENSE}
            )
            and settings.asset_image_enabled
            and settings.image_only_gen_budget > 0
        ):
            return ProductionMode.IMAGE_ONLY
        return ProductionMode.FOOTAGE

    def production_blockers(
        self,
        candidate: Candidate,
        *,
        production_mode: ProductionMode = ProductionMode.FOOTAGE,
    ) -> list[str]:
        return candidate_production_blockers(
            candidate,
            video_ready=self.video_ready(candidate),
            production_mode=production_mode,
        )

    def ingest_candidate(
        self,
        *,
        source_type: str,
        source_id: str,
        source_url: str,
        video_url: str = "",
        original_title: str,
        source_account: str = "",
        direction_key: str = "",
        rights_status: RightsStatus = RightsStatus.OFFICIAL_REFERENCE,
        rights_note: str = "",
        published_at: datetime | None = None,
    ) -> tuple[Candidate, bool]:
        """入库一条候选；返回 (candidate, created)，created 标识是否真正新建。

        跨源 URL 合并命中已有候选时 created=False，避免采集统计 created 虚高。
        """
        existing = self.session.exec(
            select(Candidate).where(
                Candidate.source_type == source_type,
                Candidate.source_id == source_id,
            )
        ).first()
        normalized_video = normalize_video_url(video_url, self.video_platform_domains)

        def backfill_video(candidate: Candidate) -> None:
            if normalized_video and not normalize_video_url(
                candidate.video_url, self.video_platform_domains
            ):
                candidate.video_url = normalized_video
                candidate.updated_at = utc_now()
                self.session.add(candidate)
                self.session.commit()

        if existing:
            backfill_video(existing)
            return existing, False
        normalized = normalize_url(source_url)
        if normalized:
            merged = self.session.exec(
                select(Candidate).where(Candidate.normalized_url == normalized)
            ).first()
            if merged:
                # 跨源同一事件：信号由 CollectionService 照常追加。
                backfill_video(merged)
                return merged, False
        candidate = Candidate(
            source_type=source_type,
            source_id=source_id,
            source_url=source_url,
            video_url=normalized_video,
            normalized_url=normalized,
            source_account=source_account,
            direction_key=direction_key,
            original_title=original_title,
            rights_status=rights_status,
            rights_note=rights_note,
            published_at=as_utc(published_at) if published_at is not None else None,
        )
        self.session.add(candidate)
        self.session.commit()
        self.session.refresh(candidate)
        return candidate, True

    def select_candidate(
        self,
        candidate_id: str,
        *,
        force: bool = False,
        production_mode: ProductionMode = ProductionMode.FOOTAGE,
    ) -> Content:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        existing = self.session.exec(
            select(Content).where(Content.candidate_id == candidate_id)
        ).first()
        if existing:
            return existing
        if not force:
            blockers = self.production_blockers(candidate, production_mode=production_mode)
            if blockers:
                raise ValueError("；".join(blockers))
            conflicts = self._topic_conflicts(candidate)
            if conflicts:
                raise DuplicateTopicError(conflicts)
        content_id = f"content_{candidate.id.removeprefix('cand_')}"
        directory = (self.data_dir / "content" / content_id).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        content = Content(
            id=content_id,
            candidate_id=candidate.id,
            title=candidate.original_title,
            title_zh=candidate.title_zh,
            content_dir=str(directory),
            production_mode=production_mode,
        )
        candidate.status = CandidateStatus.SELECTED
        candidate.updated_at = utc_now()
        self.session.add(candidate)
        self.session.add(content)
        self.session.commit()
        self.session.refresh(content)
        return content

    def ignore_candidate(self, candidate_id: str, *, actor_id: str = "admin") -> Candidate:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if candidate.status == CandidateStatus.SELECTED:
            raise ValueError("Selected candidate cannot be ignored")
        candidate.status = CandidateStatus.IGNORED
        candidate.updated_at = utc_now()
        self.session.add(candidate)
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="candidate",
                entity_id=candidate.id,
                action="candidate_ignored",
                metadata_json={"reason": "人工决定暂不制作"},
            )
        )
        self.session.commit()
        self.session.refresh(candidate)
        return candidate

    def _topic_conflicts(self, candidate: Candidate) -> list[str]:
        cutoff = utc_now() - timedelta(days=30)
        rows = self.session.exec(
            select(Content, Candidate).where(
                Content.candidate_id == Candidate.id,
                Content.created_at >= cutoff,
            )
        ).all()
        normalized = candidate.normalized_url or normalize_url(candidate.source_url)
        title_fp = normalize_title(candidate.original_title)
        return [
            content.id
            for content, other in rows
            if other.id != candidate.id
            and (
                (normalized and other.normalized_url == normalized)
                or (title_fp and normalize_title(other.original_title) == title_fp)
            )
        ]

    def schedule_enrich_batch(self, batch_size: int = 15) -> AgentTask | None:
        """把待评估候选按热度打包成一个批量 enrich 任务；一次在飞最多一个。

        除 NEW 之外，还会带上任何仍在使用、但缺中文标题或中文摘要的候选——
        否则旧条目会永远以英文原标题或空摘要留在列表与生产中心里。
        补翻不会改变状态：回写侧只对 NEW 推进到 ENRICHED。
        """
        batch_size = max(1, min(batch_size, 20))
        in_flight = self.session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.CANDIDATE_ENRICH,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
            )
        ).first()
        if in_flight is not None:
            return None
        latest_signal_id = (
            select(CandidateSignal.id)
            .where(CandidateSignal.candidate_id == Candidate.id)
            .order_by(CandidateSignal.discovered_at.desc(), CandidateSignal.id.desc())
            .limit(1)
            .correlate(Candidate)
            .scalar_subquery()
        )
        rows = self.session.exec(
            select(Candidate, CandidateSignal)
            .outerjoin(CandidateSignal, CandidateSignal.id == latest_signal_id)
            .where(
                Candidate.status != CandidateStatus.IGNORED,
                (Candidate.status == CandidateStatus.NEW)
                | (~Candidate.title_zh.op("GLOB")("*[一-龥]*"))
                | (~Candidate.summary.op("GLOB")("*[一-龥]*")),
            )
            .order_by(CandidateSignal.heat_score.desc(), Candidate.created_at.desc())
            .limit(batch_size)
        ).all()
        # SQL 先完成状态、分页与最新 signal 收敛；中文判断仍在 Python 精确处理，
        # 避免 SQLite 无 Unicode 正则时把已补齐条目误派发。
        picked_rows = [
            (candidate, signal)
            for candidate, signal in rows
            if candidate.status == CandidateStatus.NEW
            or not _contains_chinese(candidate.title_zh)
            or not _contains_chinese(candidate.summary)
        ]
        if not picked_rows:
            return None
        items = [
            {
                "candidate_id": candidate.id,
                "title": candidate.original_title,
                "source_url": candidate.source_url,
                "source_account": candidate.source_account,
                "source_summary": signal.summary if signal else "",
                "heat_score": signal.heat_score if signal else 0,
            }
            for candidate, signal in picked_rows
        ]
        return self.tasks.create_task(
            task_type=TaskType.CANDIDATE_ENRICH,
            content_id=None,
            instructions=resolve_instructions(self.session, TaskType.CANDIDATE_ENRICH, base_settings=self.base_settings),
            input_data={"items": items},
            priority=int(max(signal.heat_score if signal else 0 for _, signal in picked_rows)),
            schema_version="2",
        )

    def schedule_candidate_assist_batch(
        self, batch_size: int = 15, *, settings: Settings | None = None
    ) -> AgentTask | None:
        """把尚无助手建议的待人工候选打包；助手缺失不影响人工直接处理。"""
        batch_size = max(1, min(batch_size, 20))
        active = self.session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.CANDIDATE_ASSIST,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
            )
        ).first()
        if active is not None:
            return None
        latest_signal_id = (
            select(CandidateSignal.id)
            .where(CandidateSignal.candidate_id == Candidate.id)
            .order_by(CandidateSignal.discovered_at.desc(), CandidateSignal.id.desc())
            .limit(1)
            .correlate(Candidate)
            .scalar_subquery()
        )
        rows = self.session.exec(
            select(Candidate, CandidateSignal)
            .outerjoin(CandidateSignal, CandidateSignal.id == latest_signal_id)
            .where(
                Candidate.status == CandidateStatus.ENRICHED,
                Candidate.assist_advised_at.is_(None),
            )
            .order_by(Candidate.score.desc(), Candidate.created_at.desc())
            .limit(batch_size)
        ).all()
        if not rows:
            return None
        items = []
        from glance_core.content_acceptance import candidate_source_context
        from glance_core.runtime_config import effective_settings

        policy = effective_settings(self.session, settings or self.base_settings or Settings())
        handsfree = policy.handsfree_enabled
        for candidate, signal in rows:
            context = candidate_source_context(self.session, candidate) if handsfree else None
            items.append(
                {
                    "candidate_id": candidate.id,
                    "title": candidate.title_zh or candidate.original_title,
                    "summary": candidate.summary,
                    "score": candidate.score,
                    "suggested_hook": candidate.suggested_hook,
                    "source_account": candidate.source_account,
                    "source_url": candidate.source_url,
                    "heat_score": signal.heat_score if signal else 0,
                    **({**context, "source_context_read_only": context} if context else {}),
                    **(
                        {"production_mode": self.handsfree_candidate_mode(candidate, policy).value}
                        if handsfree
                        else {}
                    ),
                }
            )
        return self.tasks.create_task(
            task_type=TaskType.CANDIDATE_ASSIST,
            content_id=None,
            instructions=resolve_instructions(self.session, TaskType.CANDIDATE_ASSIST, base_settings=self.base_settings),
            input_data={
                "items": items,
                **(self.image_generation_inputs(policy) if handsfree else {}),
            },
            priority=max(candidate.score for candidate, _ in rows),
        )

    def build_publish_review_input(
        self,
        content_id: str,
        *,
        script: dict | None = None,
        research: dict | None = None,
        publish_prepare: dict | None = None,
        publish_policy: dict | None = None,
        settings: Settings | None = None,
    ) -> dict:
        """Build the server-owned publish-review bundle from current artifacts."""
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        publish_artifact = self.session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content_id,
                Artifact.artifact_type == "publish_video",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        cover = self.session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content_id,
                Artifact.artifact_type == "cover",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        context_files = [
            path
            for path in [
                publish_artifact.relative_path if publish_artifact else "",
                cover.relative_path if cover else "",
            ]
            if path
        ]
        payload = {
            "script": self.result_for(content_id, TaskType.SHORT_SCRIPT)
            if script is None
            else script,
            "research": self.result_for(content_id, TaskType.CONTENT_RESEARCH)
            if research is None
            else research,
            "publish_artifact_id": publish_artifact.id if publish_artifact else None,
            "publish_video_url": (
                f"/media/artifacts/{publish_artifact.id}" if publish_artifact else None
            ),
            "publish_prepare": (
                self.result_for(content_id, TaskType.PUBLISH_PREPARE) or {}
                if publish_prepare is None
                else publish_prepare
            ),
            "cover_artifact_id": cover.id if cover else "",
            "cover_image_url": f"/media/artifacts/{cover.id}" if cover else "",
            "context_files": context_files,
        }
        # Older packages keep their frozen payload and dedup identity during maintenance.
        if (content.production_mode == ProductionMode.IMAGE_ONLY and cover
                and cover.relative_path.replace("\\", "/").startswith("publish/cover-revisions/")
                and self._is_ai_cover_artifact(content.id, cover.id)):
            from glance_core.runtime_config import effective_settings

            evidence_base = settings if settings is not None else self.base_settings
            evidence_settings = effective_settings(self.session, evidence_base) if evidence_base is not None else None
            payload["production_evidence"] = self._publish_production_evidence(
                content, publish_artifact, cover, context_files, evidence_settings
            )
        if publish_policy is not None and publish_policy.get("initiated_by") == "handsfree":
            from glance_core.content_acceptance import is_handsfree_content
            from glance_core.runtime_config import effective_settings

            # A scanner's global opt-in never changes a manual item's identity.
            # Missing base settings cannot infer authorization from process env.
            current = effective_settings(self.session, settings) if settings is not None else None
            if (current is None or not current.handsfree_enabled
                or not current.handsfree_auto_publish_douyin_enabled
                or not is_handsfree_content(self.session, content_id)):
                publish_policy = None
        if publish_policy is not None:
            payload["publish_policy"] = dict(publish_policy)
        return payload

    def _publish_production_evidence(
        self, content: Content, publish: Artifact | None, cover: Artifact, context_files: list[str],
        settings: Settings | None,
    ) -> dict:
        """Actual package receipts for independent review; provenance never grants a license."""
        from glance_core.content_acceptance import generated_image_material, resolved_visual_plan, source_context

        root = Path(content.content_dir).resolve()
        context_files.clear()  # Only verified, content-contained files enter the new package bundle.
        gaps: list[str] = []
        evidence: dict = {"version": 1, "license_assessed": False, "evidence_gaps": gaps}

        def file_record(path: Path, label: str) -> dict | None:
            path = path.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                gaps.append(f"{label}: file unavailable")
                return None
            return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size}

        def json_record(path: Path, label: str) -> tuple[dict, dict | None]:
            path = path.resolve()
            if not path.is_relative_to(root) or not path.is_file():
                gaps.append(f"{label}: receipt unavailable/unsafe")
                return {}, None
            try:
                raw = path.read_bytes()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("object required")
                return data, {"sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
            except (OSError, ValueError, UnicodeError):
                gaps.append(f"{label}: receipt invalid")
                return {}, None

        context_names: dict[str, str] = {}

        def add_context(relative_path: str, sha256: str) -> None:
            # The CLI downloads into one flat directory on Windows, even for distinct source paths.
            name = Path(relative_path).name.casefold()
            existing_sha = context_names.get(name)
            if existing_sha is not None:
                if existing_sha != sha256:
                    raise ValueError("Publish review context filename collision with different bytes")
                return
            context_names[name] = sha256
            context_files.append(relative_path)

        def artifact_record(artifact: Artifact | None, label: str) -> dict | None:
            if artifact is None or artifact.content_id != content.id:
                gaps.append(f"{label}: artifact unavailable")
                return None
            actual = file_record(root / artifact.relative_path, label)
            valid = actual == {"sha256": artifact.sha256, "size_bytes": artifact.size_bytes}
            if not valid:
                gaps.append(f"{label}: registered bytes changed/unavailable")
            else:
                add_context(artifact.relative_path, artifact.sha256)
            return {"artifact_id": artifact.id, "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes, "artifact_type": artifact.artifact_type,
                    "bytes_verified": valid}

        def task_record(task: AgentTask | None, label: str) -> dict | None:
            if task is None or task.content_id != content.id:
                gaps.append(f"{label}: task unavailable")
                return None
            records = self.session.exec(select(AgentSubmission).where(
                AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
                AgentSubmission.validation_status == "ACCEPTED",
            )).all()
            accepted = False
            raw_sha = None
            if len(records) == 1:
                submission = records[0]
                raw = Path(submission.raw_result_path)
                if not raw.is_absolute():
                    raw = self.data_dir.parent / raw
                expected = (self.tasks.submission_root / task.id / str(task.attempt) / "result.json").resolve()
                try:
                    if raw.resolve() == expected and expected.is_relative_to(self.tasks.submission_root.resolve()):
                        data = expected.read_bytes()
                        accepted = (stable_hash(json.loads(data)) == submission.result_hash
                                    == stable_hash(task.result_data))
                        if accepted:
                            raw_sha = hashlib.sha256(data).hexdigest()
                except (OSError, ValueError, UnicodeError):
                    pass
            unchanged = stable_hash(task.input_data) == task.input_hash
            if not accepted or not unchanged:
                gaps.append(f"{label}: accepted raw result/input verification unavailable")
            return {"task_id": task.id, "task_type": task.task_type.value, "status": task.status.value,
                    "attempt": task.attempt, "input_sha256": task.input_hash,
                    "result_sha256": stable_hash(task.result_data), "accepted_raw_verified": accepted,
                    "raw_file_sha256": raw_sha, "input_verified": unchanged,
                    "upstream_task_ids": deepcopy(task.input_data.get("upstream_task_ids", {})),
                    "result": (deepcopy({key: value for key, value in (task.result_data or {}).items()
                                         if key in {"passed", "checks", "issues", "summary", "confidence"}})
                               if task.task_type == TaskType.CONTENT_QC else deepcopy(task.result_data))}

        evidence["publish_video"] = artifact_record(publish, "publish_video")
        evidence["cover"] = artifact_record(cover, "cover")
        composition_path = root / cover.relative_path
        composition_path = composition_path.with_name("composition.json")
        composition, composition_file = json_record(composition_path, "cover_composition")
        if (composition.get("cover_sha256") != cover.sha256
                or composition.get("cover_size_bytes") != cover.size_bytes):
            composition = {}
            gaps.append("cover_composition: receipt missing/invalid/unbound")
        elif composition_file:
            add_context(composition_path.resolve().relative_to(root).as_posix(), composition_file["sha256"])
        composition = {key: value for key, value in composition.items() if key in {
            "version", "source_sha256", "source_size_bytes", "title_lines", "disclosure", "source_binding",
            "cover_sha256", "cover_size_bytes", "composer", "crop_safe_center",
        }}
        if isinstance(composition.get("source_binding"), dict):
            composition["source_binding"] = {key: value for key, value in composition["source_binding"].items()
                                             if key in {"artifact_id", "generation_task_id", "generation_input_sha256", "generation_result_sha256"}}
        evidence["cover_composition"] = {**(composition_file or {}), "data": composition}
        binding = composition.get("source_binding") or {}
        base = self.session.get(Artifact, binding.get("artifact_id", ""))
        generation = self.session.get(AgentTask, binding.get("generation_task_id", ""))
        if (base is None or base.content_id != content.id
                or base.artifact_type != "agent_generated_image"
                or base.sha256 != composition.get("source_sha256")
                or base.size_bytes != composition.get("source_size_bytes")
                or generation is None or generation.content_id != content.id
                or generation.task_type != TaskType.ASSET_GENERATE_IMAGE
                or generation.id != base.producer_stage
                or generation.input_hash != binding.get("generation_input_sha256")
                or stable_hash(generation.result_data) != binding.get("generation_result_sha256")
                or not any(item.get("artifact_id") == base.id for item in (generation.result_data or {}).get("images", []))):
            base = generation = None
            gaps.append("cover_base: exact artifact/generation identity unavailable")
        evidence["cover_base"] = artifact_record(base, "cover_base")
        evidence["cover_generation"] = task_record(generation, "cover_generation")
        if generation:
            evidence["cover_generation"]["requests"] = deepcopy(generation.input_data.get("requests", []))

        script = self.completed_task_for(content.id, TaskType.SHORT_SCRIPT)
        plan = self.completed_task_for(content.id, TaskType.VISUAL_PLAN)
        qc = self.completed_task_for(content.id, TaskType.CONTENT_QC)
        evidence["script"] = task_record(script, "script")
        evidence["visual_plan"] = task_record(plan, "visual_plan")
        evidence["content_qc"] = task_record(qc, "content_qc")
        final = self.session.get(Artifact, qc.input_data.get("video_artifact_id")) if qc else None
        evidence["final_video"] = artifact_record(final, "final_video")
        latest_final = self.session.exec(select(Artifact).where(
            Artifact.content_id == content.id, Artifact.artifact_type == "final_video",
        ).order_by(Artifact.created_at.desc(), Artifact.id.desc())).first()
        subject_matches = bool(
            qc and qc.status == TaskStatus.COMPLETED and (qc.result_data or {}).get("passed") is True
            and final and latest_final and final.id == latest_final.id and publish and final.sha256 == publish.sha256
            and qc.input_data.get("upstream_task_ids", {}).get("script") == (script.id if script else None)
            and qc.input_data.get("upstream_task_ids", {}).get("visual_plan") == (plan.id if plan else None)
        )
        evidence["current_final_qc_subject_matches"] = subject_matches
        if not subject_matches:
            gaps.append("content_qc: passed current script/plan/final binding unavailable")
        if qc:
            evidence["acceptance_subject"] = deepcopy(qc.input_data.get("acceptance_subject", {}))
            evidence["source_context_at_acceptance"] = deepcopy(qc.input_data.get("source_context", {}))
        refs = {"script": script.id if script else None, "visual_plan": plan.id if plan else None}
        evidence["registered_source_context"] = source_context(self.session, content.id, refs)
        evidence["images"] = []
        if plan:
            try:
                segments = resolved_visual_plan(self.session, plan).get("segments", [])
            except ValueError:
                segments = []
                gaps.append("images: resolved current plan unavailable")
            for index, segment in enumerate(segments):
                asset = self.session.get(Artifact, segment.get("asset_id", ""))
                item = {"segment_index": index, "artifact": artifact_record(asset, f"image_{index}")}
                if asset and asset.artifact_type == "agent_generated_image":
                    try:
                        material = generated_image_material(self.session, content, asset.id)
                        item["generation"] = material
                        item["generation_receipt"] = task_record(
                            self.session.get(AgentTask, material["generation_task_id"]), f"image_{index}_generation"
                        )
                    except ValueError:
                        gaps.append(f"image_{index}: exact generation lineage unavailable")
                evidence["images"].append(item)
        # Imported processing evidence is separate from generation claims, quality approval and rights.
        expected_preparations = {item["artifact"]["artifact_id"] for item in evidence["images"]
                                 if item.get("artifact") and item["artifact"].get("artifact_type") == "agent_generated_image"}
        if base is not None:
            expected_preparations.add(base.id)
        preparation_records, missing_preparations = [], []
        for prepared_id in sorted(expected_preparations):
            registrations = self.session.exec(select(AuditEvent).where(
                AuditEvent.entity_type == "artifact", AuditEvent.entity_id == prepared_id,
                AuditEvent.action == "image_preparation_registered",
            )).all()
            prepared = self.session.get(Artifact, prepared_id)
            registration = registrations[0] if len(registrations) == 1 else None
            metadata = registration.metadata_json if registration else {}
            original = self.session.get(Artifact, metadata.get("original_artifact_id", ""))
            receipt_artifact = self.session.get(Artifact, metadata.get("receipt_artifact_id", ""))
            valid = bool(registration and registration.actor_type == "admin" and registration.actor_id == "mpt-admin-api"
                and prepared and original and receipt_artifact and original.content_id == receipt_artifact.content_id == content.id
                and original.artifact_type == "image_preparation_original" and receipt_artifact.artifact_type == "image_preparation_receipt"
                and original.producer_stage == receipt_artifact.producer_stage == prepared.producer_stage
                and metadata.get("prepared_sha256") == prepared.sha256
                and metadata.get("record_sha256") == receipt_artifact.sha256)
            record = {}
            if valid:
                original_info = artifact_record(original, "preparation_original")
                receipt_info = artifact_record(receipt_artifact, "preparation_receipt")
                valid = bool(original_info["bytes_verified"] and receipt_info["bytes_verified"])
                if valid:
                    record, _ = json_record(root / receipt_artifact.relative_path, "preparation_receipt")
                    generation = self.session.get(AgentTask, prepared.producer_stage)
                    valid = bool(generation and record.get("prepared_artifact_id") == prepared.id
                        and record.get("prepared_sha256") == prepared.sha256 and record.get("task_id") == generation.id
                        and record.get("input_sha256") == generation.input_hash == stable_hash(generation.input_data)
                        and record.get("result_sha256") == stable_hash(generation.result_data)
                        and record.get("source_sha256") == original.sha256 and record.get("source_size_bytes") == original.size_bytes
                        and record.get("source_receipt_sha256") == metadata.get("source_receipt_sha256")
                        and record.get("replay_verified") is True and record.get("raw_input_bytes_verified") is False
                        and record.get("license_verified") is False and record.get("vendor_generation_receipt_verified") is False)
            if valid:
                preparation_records.append({"prepared_artifact_id": prepared_id, "original": original_info,
                                            "receipt": receipt_info, "processing": record})
            else:
                missing_preparations.append(prepared_id)
        preparation_complete = bool(evidence["images"] and base is not None and expected_preparations
                                    and not missing_preparations and all(item.get("artifact") for item in evidence["images"]))
        evidence["local_adaptation"] = {
            "receipt_status": "complete" if preparation_complete else "incomplete",
            "complete": preparation_complete, "expected_artifact_ids": sorted(expected_preparations),
            "missing_artifact_ids": missing_preparations, "records": preparation_records,
            "limitation": "Registered originals and processing replay do not establish vendor-native generation provenance or a license; historical workspace input bytes remain unverified.",
        }
        # Additive registrations affect only freshly issued review inputs, never old task receipts.
        use_records = []
        registered_use_ids = set()
        preparations = {row["prepared_artifact_id"]: row for row in preparation_records}
        for prepared_id in sorted(expected_preparations):
            registrations = self.session.exec(select(AuditEvent).where(
                AuditEvent.entity_type == "artifact", AuditEvent.entity_id == prepared_id,
                AuditEvent.action == "image_use_evidence_registered",
            )).all()
            if not registrations:
                continue
            if (len(registrations) != 1 or registrations[0].actor_type != "admin"
                    or registrations[0].actor_id != "mpt-admin-api" or prepared_id not in preparations):
                raise ValueError("Image-use registration or current preparation binding changed")
            metadata = registrations[0].metadata_json
            prepared = self.session.get(Artifact, prepared_id)
            files = {}
            for key, kind in (("receipt", "image_use_receipt"), ("validator_report", "image_use_validator_report"),
                              ("terms_document", "image_use_terms_document")):
                asset = self.session.get(Artifact, metadata.get(f"{key}_artifact_id", ""))
                if (asset is None or asset.content_id != content.id or asset.artifact_type != kind
                        or asset.producer_stage != prepared.producer_stage):
                    raise ValueError("Registered image-use evidence artifact association changed")
                files[key] = artifact_record(asset, f"image_use_{key}")
                if not files[key]["bytes_verified"]:
                    raise ValueError("Registered image-use evidence bytes changed")
            receipt_asset = self.session.get(Artifact, files["receipt"]["artifact_id"])
            record, _ = json_record(root / receipt_asset.relative_path, "image_use_receipt")
            preparation = preparations[prepared_id]
            if (record.get("version") != 1 or record.get("prepared_artifact_id") != prepared_id
                    or record.get("prepared_sha256") != prepared.sha256
                    or record.get("original_artifact_id") != preparation["original"]["artifact_id"]
                    or record.get("original_sha256") != preparation["original"]["sha256"]
                    or record.get("preparation_receipt_artifact_id") != preparation["receipt"]["artifact_id"]
                    or record.get("preparation_receipt_sha256") != preparation["receipt"]["sha256"]
                    or record.get("task_id") != prepared.producer_stage
                    or record.get("task_input_sha256") != preparation["processing"]["input_sha256"]
                    or record.get("task_result_sha256") != preparation["processing"]["result_sha256"]
                    or record.get("validator_report_sha256") != files["validator_report"]["sha256"]
                    or record.get("terms_document_sha256") != files["terms_document"]["sha256"]
                    or metadata.get("record_sha256") != files["receipt"]["sha256"]
                    or metadata.get("prepared_sha256") != prepared.sha256
                    or metadata.get("original_sha256") != record.get("original_sha256")
                    or record.get("license_verified") is not False):
                raise ValueError("Registered image-use receipt identities or limitations changed")
            use_records.append({"prepared_artifact_id": prepared_id, **files, "data": record})
            registered_use_ids.add(prepared_id)
        if registered_use_ids:
            evidence["image_use_evidence"] = {
                "version": 1, "complete": registered_use_ids == expected_preparations,
                "expected_artifact_ids": sorted(expected_preparations),
                "missing_artifact_ids": sorted(expected_preparations - registered_use_ids), "records": use_records,
                "limitation": "Supporting native tool/provenance observations and official output-use terms sources are separate from a verified account contract, prompt provenance, third-party rights clearance, and publication approval.",
            }
        evidence["plan_qc"] = task_record(self.completed_task_for(content.id, TaskType.VISUAL_PLAN_QC), "plan_qc")
        image_qcs = self.session.exec(select(AgentTask).where(
            AgentTask.content_id == content.id, AgentTask.task_type == TaskType.ASSET_QC,
            AgentTask.status == TaskStatus.COMPLETED,
        ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).all()
        evidence["asset_qc"] = [task_record(item, "asset_qc") for item in image_qcs
                                 if plan and item.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id]
        voice_binding = plan.input_data.get("voice_artifact", {}) if plan else {}
        voice = self.session.get(Artifact, voice_binding.get("id", ""))
        voice_record = artifact_record(voice, "voice")
        evidence["voice"] = {"artifact": voice_record, "provider": "unknown",
                             "provider_evidence": "historical voice has no persisted provider receipt",
                             "plan_binding_matches": bool(voice and voice.sha256 == voice_binding.get("sha256"))}
        if plan and plan.input_data.get("voice_generation"):
            from glance_core.voice_generation import verify_voice_generation

            try:
                if settings is None or script is None:
                    raise ValueError("configured verifier/current script unavailable")
                generation, generation_artifact = verify_voice_generation(
                    self.session, settings, content, voice_binding, plan.input_data["voice_generation"], script,
                )
                evidence["voice"].update(
                    provider="kokoro", provider_evidence="signed local generation receipt matches current script and voice bytes",
                    generation=generation, generation_receipt=artifact_record(generation_artifact, "voice_generation_receipt"),
                )
            except (ValueError, OSError, TypeError, KeyError):
                gaps.append("voice: current local generation receipt failed verification")
        # A matching signed historical request proves requested options, not the provider's response or rights.
        if settings is not None and script and voice and voice_record and voice_record["bytes_verified"]:
            approvals = self.session.exec(select(AuditEvent).where(
                AuditEvent.entity_type == "content", AuditEvent.entity_id == content.id,
                AuditEvent.action == "manual_script_approved", AuditEvent.actor_type == "human",
                AuditEvent.actor_id == "admin-api",
            )).all()
            options = {"voice": settings.tts_voice, "rate": settings.tts_rate, "volume": settings.tts_volume,
                       "provider": settings.tts_provider, "provider_endpoint": stable_hash(settings.tts_provider_endpoint),
                       "target_duration_sec": float((script.result_data or {}).get("estimated_duration_sec") or 0) or None}
            proof = {"id": script.id, "input_hash": script.input_hash, "result_hash": stable_hash(script.result_data),
                     "status": script.status.value, "attempt": script.attempt}
            for approval in approvals:
                receipt = approval.metadata_json
                unsigned = {key: value for key, value in receipt.items() if key != "signature"}
                recorded_signature = receipt.get("signature")
                if not isinstance(recorded_signature, str) or len(recorded_signature) != 64 or not recorded_signature.isascii():
                    continue
                signature = hmac.new(settings.admin_token.get_secret_value().encode(),
                    stable_hash({"event_id": approval.id, "content_id": content.id, "receipt": unsigned}).encode("ascii"),
                    hashlib.sha256).hexdigest()
                if (not hmac.compare_digest(recorded_signature, signature)
                        or receipt.get("contract_version") != 1 or receipt.get("producer") != "mpt-admin-approve-script"
                        or (receipt.get("tasks") or {}).get("short_script") != proof
                        or receipt.get("voice_artifact") != voice_binding):
                    continue
                requested_hash = stable_hash({"version": 1, "script": proof, "approval": receipt["tasks"],
                                              "voice_options": options, "automatic_scope": None})
                stages = self.session.exec(select(StageRun).where(
                    StageRun.content_id == content.id, StageRun.stage == "voice", StageRun.status == StageStatus.SUCCESS,
                    StageRun.input_hash == requested_hash,
                )).all()
                if len(stages) == 1:
                    evidence["voice"]["historical_request"] = {"options": options, "stage_id": stages[0].id,
                        "input_sha256": requested_hash, "approval_event_id": approval.id,
                        "approval_signature_verified": True, "actual_provider_verified": False, "license_verified": False}
                    break
        evidence["bgm"] = {"usage": "unknown", "license": "unknown"}
        request, request_file = json_record(root / "render" / "mpt-render-request.json", "render")
        outputs, _ = json_record((root / final.relative_path).with_name("outputs.json"), "render_outputs") if final else ({}, None)
        if (request_file and final and request.get("revision_id") == Path(final.relative_path).parent.name
                and outputs.get("final.mp4") == final.sha256
                and voice and request.get("voice_sha256") == voice.sha256):
            evidence["render_receipt"] = {**request_file, "revision_id": request["revision_id"],
                                          "voice_sha256": voice.sha256,
                                          "bgm_sha256": request.get("bgm_sha256"), "bgm_volume": request.get("bgm_volume")}
            evidence["bgm"] = {"usage": "used" if request.get("bgm_sha256") else "none",
                               "sha256": request.get("bgm_sha256"), "license": "unknown" if request.get("bgm_sha256") else "not_applicable"}
        else:
            gaps.append("render: exact voice/BGM receipt unavailable")
        # Generation tools and QC results are evidence for review, never legal permission.
        return evidence

    def schedule_manual_assist_tasks(
        self, *, publish_policy: dict | None = None, settings: Settings | None = None
    ) -> list[AgentTask]:
        """为当前所有人工内容节点补建助手任务；已有任务保持幂等。"""
        created: list[AgentTask] = []
        contents = self.session.exec(
            select(Content).where(
                Content.business_status.in_(
                    [
                        ContentStatus.SCRIPT_REVIEW,
                        ContentStatus.FINAL_REVIEW,
                        ContentStatus.PACKAGED,
                    ]
                )
            )
        ).all()
        for content in contents:
            if content.business_status == ContentStatus.SCRIPT_REVIEW:
                task_type = TaskType.SCRIPT_REVIEW_ASSIST
                script_task = self.completed_task_for(content.id, TaskType.SHORT_SCRIPT)
                script_qc_task = self.completed_task_for(content.id, TaskType.SCRIPT_QC)
                research_task = self.completed_task_for(content.id, TaskType.CONTENT_RESEARCH)
                script = script_task.result_data if script_task else None
                script_qc = script_qc_task.result_data if script_qc_task else None
                research = research_task.result_data if research_task else None
                # Recovery can leave the business status at SCRIPT_REVIEW while an
                # upstream task is missing. Do not create an empty assistant task;
                # the next scheduler pass can retry once the dependencies exist.
                if (
                    script_task is None
                    or script_qc_task is None
                    or research_task is None
                    or not script
                    or not script_qc
                    or not research
                ):
                    continue
                input_data = {
                    "script": script,
                    "script_qc": script_qc,
                    "research": research,
                    "upstream_task_ids": {
                        "script": script_task.id,
                        "script_qc": script_qc_task.id,
                        "research": research_task.id,
                    },
                }
            elif content.business_status == ContentStatus.FINAL_REVIEW:
                acceptance = self.completed_task_for(content.id, TaskType.CONTENT_QC)
                if acceptance is not None and acceptance.schema_version in {"2", "3"}:
                    continue  # This exact new task already owns the complete final-review contract.
                task_type = TaskType.FINAL_REVIEW_ASSIST
                artifact = self.session.exec(
                    select(Artifact)
                    .where(
                        Artifact.content_id == content.id,
                        Artifact.artifact_type == "final_video",
                    )
                    .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                ).first()
                input_data = {
                    "script": self.result_for(content.id, TaskType.SHORT_SCRIPT),
                    "video_artifact_id": artifact.id if artifact else None,
                    "video_url": f"/media/artifacts/{artifact.id}" if artifact else None,
                }
            else:
                task_type = TaskType.PUBLISH_REVIEW_ASSIST
                pending_prepare = self.session.exec(
                    select(AgentTask).where(
                        AgentTask.content_id == content.id,
                        AgentTask.task_type == TaskType.PUBLISH_PREPARE,
                        AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
                    )
                ).first()
                if pending_prepare is not None:
                    continue
                input_data = self.build_publish_review_input(
                    content.id, publish_policy=publish_policy, settings=settings
                )

            if task_type == TaskType.SCRIPT_REVIEW_ASSIST:
                active_review = self.session.exec(
                    select(AgentTask).where(
                        AgentTask.content_id == content.id,
                        AgentTask.task_type == task_type,
                        AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
                    )
                ).first()
                latest_review = self.completed_task_for(content.id, task_type)

                def comparable_input(review: AgentTask | None) -> dict | None:
                    if review is None:
                        return None
                    return {
                        key: value
                        for key, value in review.input_data.items()
                        if key != "automatic_review_version"
                    }

                if (
                    comparable_input(active_review) == input_data
                    or comparable_input(latest_review) == input_data
                ):
                    continue

                # Preserve Handsfree's one-time reassessment marker when replacing an
                # earlier completed review with a newer input bundle.
                if latest_review is not None:
                    input_data = {**input_data, "automatic_review_version": 1}
                created.append(
                    self.create_agent_task(
                        content.id,
                        task_type,
                        input_data,
                        priority=50,
                        allow_multiple=True,
                    )
                )
                continue

            if task_type == TaskType.PUBLISH_REVIEW_ASSIST:
                active_review = self.session.exec(
                    select(AgentTask).where(
                        AgentTask.content_id == content.id,
                        AgentTask.task_type == task_type,
                        AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
                    )
                ).first()
                latest_review = self.completed_task_for(content.id, task_type)
                if (active_review is not None and active_review.input_data == input_data) or (
                    latest_review is not None and latest_review.input_data == input_data
                ):
                    continue
                created.append(
                    self.create_agent_task(
                        content.id,
                        task_type,
                        input_data,
                        priority=50,
                        allow_multiple=True,
                    )
                )
                continue

            existing = self.session.exec(
                select(AgentTask).where(
                    AgentTask.content_id == content.id,
                    AgentTask.task_type == task_type,
                    AgentTask.status.in_(
                        [TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.COMPLETED]
                    ),
                )
            ).first()
            if existing is None:
                created.append(
                    self.create_agent_task(content.id, task_type, input_data, priority=50)
                )
        return created

    def schedule_media_source_assist(
        self,
        content_id: str,
        *,
        error_code: str,
        error_message: str,
    ) -> AgentTask | None:
        """为下载确定性失败的内容创建「找可用视频源」助手任务（REQ-2）。

        任务只产出建议；Handsfree 是否自动采纳由已验证的 URL 类型决定，归档仍由人触发。
        """
        content = self.session.get(Content, content_id)
        if content is None:
            return None
        candidate = self.session.get(Candidate, content.candidate_id)
        if candidate is None:
            return None
        return self.create_agent_task(
            content_id,
            TaskType.MEDIA_SOURCE_ASSIST,
            {
                "content_id": content_id,
                "title": candidate.title_zh or candidate.original_title,
                "original_title": candidate.original_title,
                "source_url": candidate.source_url,
                "video_url": candidate.video_url,
                "source_account": candidate.source_account,
                "failure_error_code": error_code,
                "failure_message": error_message[:1000],
            },
            priority=55,
        )

    def adopt_video_source(
        self,
        content_id: str,
        video_url: str,
        *,
        source_page_url: str = "",
        actor_id: str = "admin",
        actor_type: str = "human",
        task_id: str = "",
    ) -> Content:
        """采纳已验证的视频源：回填候选 video_url 并重置素材准备。"""
        url = (video_url or "").strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("视频源必须是 http/https 链接")
        page_url = (source_page_url or "").strip()
        if page_url and not page_url.startswith(("http://", "https://")):
            raise ValueError("视频来源页必须是 http/https 链接")
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        candidate = self.session.get(Candidate, content.candidate_id)
        if candidate is None:
            raise KeyError(content.candidate_id)
        url = normalize_video_url(url, self.video_platform_domains)
        if not url:
            raise ValueError("视频源必须是白名单视频平台播放页或 .mp4/.m3u8 直链")
        candidate.video_url = url
        # Page URL is stable provenance for a signed CDN URL. For a normal
        # video-page suggestion, the adopted URL itself is the stable page.
        candidate.video_source_page_url = page_url or url
        candidate.updated_at = utc_now()
        content.error_code = None
        content.error_message = None
        content.business_status = ContentStatus.PREPARING_CONTEXT
        content.current_stage = "acquire"
        content.updated_at = utc_now()
        self.session.add(candidate)
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type=actor_type,
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="video_source_adopted",
                metadata_json={
                    "video_url": _safe_video_url_for_audit(url),
                    "source_page_url": candidate.video_source_page_url,
                    "candidate_id": candidate.id,
                    "assist_task_id": task_id,
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        return content

    def restore_candidate(self, candidate_id: str, *, actor_id: str = "admin") -> Candidate:
        candidate = self.session.get(Candidate, candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        if candidate.status != CandidateStatus.IGNORED:
            raise ValueError("Only ignored candidates can be restored")
        candidate.status = CandidateStatus.ENRICHED if candidate.summary else CandidateStatus.NEW
        candidate.updated_at = utc_now()
        self.session.add(candidate)
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="candidate",
                entity_id=candidate.id,
                action="candidate_restored",
                metadata_json={"reason": "人工恢复助手或人工淘汰的候选"},
            )
        )
        self.session.commit()
        self.session.refresh(candidate)
        return candidate

    def expire_stale_enrich_tasks(self, ttl_hours: int) -> int:
        """取消超时 PENDING 的 enrich 任务；旧单条格式任务一并清理（候选保持 NEW）。"""
        cutoff = utc_now() - timedelta(hours=ttl_hours)
        pending = self.session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.CANDIDATE_ENRICH,
                AgentTask.status == TaskStatus.PENDING,
            )
        ).all()
        cancelled = 0
        for task in pending:
            if as_utc(task.created_at) < cutoff:
                self.tasks.cancel(task.id, actor_id="enrich-ttl")
                cancelled += 1
        return cancelled

    def create_agent_task(
        self,
        content_id: str,
        task_type: TaskType,
        input_data: dict,
        *,
        priority: int = 0,
        allow_multiple: bool = False,
        handsfree: bool | None = None,
        schema_version: str = "1",
    ) -> AgentTask:
        if task_type == TaskType.VISUAL_PLAN_QC:
            input_data = self._visual_qc_generation_policy(content_id, input_data)
        if task_type in {TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC}:
            input_data = dict(input_data)
            input_data.pop("opening_identity_overlay", None)
            overlay = self.opening_identity_input(content_id, task_type, input_data)
            if overlay:
                input_data["opening_identity_overlay"] = overlay
        if not allow_multiple:
            existing = self.session.exec(
                select(AgentTask).where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == task_type,
                    AgentTask.status.in_(
                        [TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.COMPLETED]
                    ),
                )
            ).first()
            if existing:
                return existing
        return self.tasks.create_task(
            task_type=task_type,
            content_id=content_id,
            instructions=resolve_instructions(
                self.session, task_type, handsfree=handsfree, schema_version=schema_version,
                base_settings=self.base_settings,
                opening_identity_overlay=self.opening_identity_overlay,
            ),
            input_data=input_data,
            priority=priority,
            schema_version=schema_version,
        )

    def _visual_qc_generation_policy(self, content_id: str, input_data: dict) -> dict:
        ref = input_data.get("upstream_task_ids", {}).get("visual_plan")
        plan = self.session.get(AgentTask, ref) if isinstance(ref, str) else None
        if (
            plan is None
            or plan.content_id != content_id
            or plan.task_type != TaskType.VISUAL_PLAN
            or plan.status != TaskStatus.COMPLETED
            or stable_hash(plan.input_data) != plan.input_hash
        ):
            return dict(input_data)
        return {
            **input_data,
            **{
                key: plan.input_data[key]
                for key in (
                    "production_mode",
                    "asset_image_enabled",
                    "image_only_gen_budget",
                    "visual_timing_evidence",
                    "opening_identity_overlay",
                )
                if key in plan.input_data
            },
        }

    def opening_identity_input(self, content_id: str, task_type: TaskType, input_data: dict) -> dict:
        """Describe an existing renderer mechanism without approving its future pixels."""
        if not self.opening_identity_overlay or task_type not in {TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC}:
            return {}
        from glance_core.content_acceptance import _unchanged_material_task

        refs = input_data.get("upstream_task_ids") or {}
        if task_type == TaskType.VISUAL_PLAN_QC:
            ref = refs.get("visual_plan")
            plan = self.session.get(AgentTask, ref) if isinstance(ref, str) else None
            if (plan is None or plan.content_id != content_id or plan.task_type != TaskType.VISUAL_PLAN
                or plan.status != TaskStatus.COMPLETED):
                return {}
            try:
                _unchanged_material_task(self.session, plan, owned=True,
                                         consumed=plan.id != self.session.info.get("consuming_agent_task_id"))
            except ValueError:
                return {}
            overlay = self.opening_identity_input(content_id, TaskType.VISUAL_PLAN, plan.input_data)
            if (refs.get("script", overlay.get("script_task_id")) != overlay.get("script_task_id")
                or ("opening_identity_overlay" in plan.input_data
                    and plan.input_data["opening_identity_overlay"] != overlay)):
                return {}
            return overlay
        ref = refs.get("script")
        script = self.session.get(AgentTask, ref) if isinstance(ref, str) else None
        if (script is None or script.content_id != content_id or script.task_type != TaskType.SHORT_SCRIPT
            or script.status != TaskStatus.COMPLETED):
            return {}
        try:
            _unchanged_material_task(self.session, script, owned=True)
        except ValueError:
            return {}
        opening = (script.result_data or {}).get("opening")
        identity = opening.get("product_identity") if isinstance(opening, dict) else None
        duration = input_data.get("voice_duration_sec")
        if (not isinstance(identity, str) or not any(char.isprintable() and not char.isspace() for char in identity)
            or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0):
            return {}
        end_sec = int(min(8, duration) * 100) / 100
        if end_sec <= 0:
            return {}
        return {"renderer": "mpt", "source_field": "script.opening.product_identity",
                "script_task_id": script.id, "script_result_sha256": stable_hash(script.result_data),
                "text": identity, "start_sec": 0, "end_sec": end_sec}

    def script_audience_inputs(
        self,
        content: Content,
        research: AgentTask | None = None,
        *,
        settings: Settings | None = None,
    ) -> dict[str, Any]:
        candidate = self.session.get(Candidate, content.candidate_id)
        data = (research.result_data or {}) if research is not None else {}
        inputs: dict[str, Any] = {
            "suggested_hook": str((candidate.suggested_hook if candidate else "") or ""),
            "recommended_angle": str(data.get("recommended_angle") or ""),
        }
        if content.production_mode == ProductionMode.IMAGE_ONLY:
            from glance_core.runtime_config import effective_settings

            policy = effective_settings(self.session, settings or self.base_settings or Settings())
            inputs.update(self.image_generation_inputs(policy))
        return inputs

    @staticmethod
    def classify_recovery_failure(
        task: AgentTask,
        *,
        code: str = "",
        retryable: bool = False,
    ) -> str:
        """Classify a failure without allowing the Agent to choose a safer class."""
        if (
            task.task_type
            in {
                TaskType.SCRIPT_QC,
                TaskType.VISUAL_PLAN_QC,
                TaskType.ASSET_QC,
                TaskType.CONTENT_QC,
            }
            and task.status == TaskStatus.COMPLETED
        ):
            return "BUSINESS_QC" if not (task.result_data or {}).get("passed") else "UNKNOWN"
        upper = f"{code} {task.error_code or ''}".upper()
        if any(word in upper for word in ("RIGHT", "LICENSE", "COPYRIGHT", "PUBLISH")):
            return "RIGHTS_OR_PUBLISHING"
        if any(
            word in upper
            for word in ("CONTEXT", "BUNDLE", "DEPENDENCY", "MISSING_INPUT", "WORKSPACE")
        ):
            return "CONTEXT_MISSING"
        if any(word in upper for word in ("CONTRACT", "SCHEMA", "INVALID_INPUT", "422")):
            return "TASK_CONTRACT"
        if retryable or any(
            word in upper
            for word in (
                "TIMEOUT",
                "NETWORK",
                "LEASE",
                "EXECUTOR",
                "ENVIRONMENT_UNREACHABLE",
                "WINERROR 10061",
            )
        ):
            return "TRANSIENT_EXECUTOR"
        return "UNKNOWN"

    def _latest_submission_errors(self, task_id: str) -> list[dict[str, Any]]:
        submission = self.session.exec(
            select(AgentSubmission)
            .where(AgentSubmission.task_id == task_id)
            .order_by(AgentSubmission.created_at.desc())
        ).first()
        if submission is None:
            return []
        return [dict(item) for item in (submission.validation_errors or [])[:30]]

    def _latest_submission_result(self, task: AgentTask) -> dict[str, Any]:
        submission = self.session.exec(
            select(AgentSubmission)
            .where(AgentSubmission.task_id == task.id)
            .order_by(AgentSubmission.created_at.desc())
        ).first()
        if submission is None:
            return {}
        try:
            raw = json.loads(Path(submission.raw_result_path).read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        if task.task_type == TaskType.SHORT_SCRIPT:
            return {key: raw[key] for key in _SCRIPT_RESULT_FIELDS if key in raw}
        if task.task_type in {
            TaskType.SCRIPT_QC,
            TaskType.VISUAL_PLAN_QC,
            TaskType.ASSET_QC,
            TaskType.CONTENT_QC,
        }:
            return {
                key: raw[key] for key in ("passed", "issues", "suggested_revision") if key in raw
            }
        return {
            key: raw[key]
            for key in ("artifact_id", "images", "segments", "verdict", "summary")
            if key in raw
        }

    def _recovery_current_input(
        self, task: AgentTask, *, full_scenes: bool = False
    ) -> dict[str, Any]:
        current = {
            key: task.input_data[key] for key in _RECOVERY_INPUT_FIELDS if key in task.input_data
        }
        overlay = self.opening_identity_input(task.content_id or "", task.task_type, task.input_data)
        if overlay:
            field = ("opening_identity_overlay" if "opening_identity_overlay" in task.input_data
                     else "opening_identity_overlay_read_only")
            current[field] = overlay
        gaps: list[str] = []
        subject = task.input_data.get("acceptance_subject")
        if task.task_type == TaskType.CONTENT_QC and isinstance(subject, dict):
            from glance_core.content_acceptance import public_source_url

            # Existing server-issued evidence fields only. Unknown nested data is
            # omitted as a whole, never projected into apparently complete proof.
            allowed = {
                "version",
                "registration",
                "candidate_id",
                "source_id",
                "source_type",
                "source_account",
                "source_url",
                "video_url",
                "video_source_page_url",
                "rights_status",
                "rights_note",
                "source_registration",
                "url",
                "default_rights_status",
                "scenes",
                "scene_id",
                "scene_analysis_task_id",
                "scene_input_sha256",
                "scene_result_sha256",
                "start_sec",
                "end_sec",
                "duration_sec",
                "source_video_artifact_id",
                "artifacts",
                "artifact_id",
                "artifact_type",
                "registered_sha256",
                "size_bytes",
                "research_task_id",
                "source_images",
                "caption",
                "source_page",
                "evidence_gaps",
                "method",
                "video_sha256",
                "asr_model",
                "asr_device",
                "asr_compute_type",
                "asr_status",
                "segments",
                "segment_id",
                "text",
                "avg_logprob",
                "no_speech_prob",
                "words",
                "word",
                "probability",
                "signal_status",
                "signal",
                "limitations",
                "decoded_pcm_sha256",
                "sample_rate_hz",
                "channels",
                "peak_amplitude",
                "full_scale_sample_fraction",
                "window_sec",
                "rms_dbfs",
                "asr_error_code",
                "signal_error_code",
            }

            def safe(value, key="", depth=0):
                if depth > 8:
                    return False
                if isinstance(value, dict):
                    return all(k in allowed and safe(v, k, depth + 1) for k, v in value.items())
                if isinstance(value, list):
                    return len(value) <= 4000 and all(safe(v, key, depth + 1) for v in value)
                if isinstance(value, str):
                    if key in {
                        "url",
                        "source_url",
                        "video_url",
                        "video_source_page_url",
                        "source_page",
                    }:
                        return not value or public_source_url(value) == value
                    if key == "source_id" and "://" in value:
                        return public_source_url(value) == value
                    return len(value) <= 8000
                return value is None or isinstance(value, (int, float, bool))

            evidence = {}
            for name in ("source_context", "audio_evidence"):
                value = task.input_data.get(name)
                if (
                    isinstance(value, dict)
                    and safe(value)
                    and stable_hash(task.input_data) == task.input_hash
                    and subject.get(f"{name}_sha256") == stable_hash(value)
                    and subject.get("content_id") == task.content_id
                ):
                    evidence[name] = deepcopy(value)
                else:
                    gaps.append(f"{name}: frozen evidence identity unavailable")
            if evidence:
                current["acceptance_evidence"] = {
                    "source_task_id": task.id,
                    "source_input_hash": task.input_hash,
                    "subject": {
                        key: subject[key]
                        for key in (
                            "content_id",
                            "video_sha256",
                            "video_artifact_id",
                            "script_task_id",
                            "visual_plan_task_id",
                            "source_context_sha256",
                            "audio_evidence_sha256",
                            "visual_evidence_sha256",
                        )
                        if key in subject and isinstance(subject[key], str)
                    },
                    **evidence,
                }
                visual = task.input_data.get("visual_evidence")
                if (task.schema_version == "3" and isinstance(visual, dict)
                        and stable_hash(task.input_data) == task.input_hash
                        and subject.get("visual_evidence_sha256") == stable_hash(visual)):
                    manifest = visual.get("manifest", {})
                    current["acceptance_evidence"]["visual_evidence_binding"] = {
                        "manifest_sha256": visual.get("manifest_sha256"),
                        "method": "native_contact_pages_asr_pcm",
                        "frame_count": len(manifest.get("frames", [])),
                        "page_count": len(manifest.get("pages", [])),
                        "generated_only": True,
                    }
                    if "audio_review_policy_version" in task.input_data:
                        policy = task.input_data["audio_review_policy_version"]
                        if type(policy) is int and policy in {1, 2}:
                            current["acceptance_evidence"]["visual_evidence_binding"]["audio_review_policy_version"] = policy
                        else:
                            gaps.append("audio_review_policy_version: frozen policy unsupported")

        def references(owner: AgentTask | None) -> dict:
            refs = owner.input_data.get("upstream_task_ids") if owner else None
            return refs if isinstance(refs, dict) else {}

        def completed(owner: AgentTask | None, field: str, task_type: TaskType) -> list[AgentTask]:
            ids = references(owner).get(field)
            if isinstance(ids, str):
                ids = [ids]
            if not task.content_id or not isinstance(ids, list) or not 0 < len(ids) <= 60:
                return []
            if task_type != TaskType.SCENE_ANALYZE and len(ids) != 1:
                return []
            rows = []
            for task_id in ids:
                row = self.session.get(AgentTask, task_id) if isinstance(task_id, str) else None
                if (
                    row is None
                    or row.content_id != task.content_id
                    or row.task_type != task_type
                    or row.status != TaskStatus.COMPLETED
                    or (full_scenes and stable_hash(row.input_data) != row.input_hash)
                ):
                    return []
                rows.append(row)
            return rows

        def fields(value: Any, names: tuple[str, ...]) -> dict:
            if not isinstance(value, dict):
                return {}
            result = {}
            for name in names:
                item = value.get(name)
                text_limit = 2000 if name in ("id", "claim", "source_urls") else 8000
                if isinstance(item, str):
                    if name == "preferred_source" and any(char in item for char in "/\\:"):
                        continue
                    if len(item) > text_limit:
                        gaps.append(f"{name}: oversized field omitted")
                        continue
                    result[name] = item
                elif isinstance(item, (int, float)) and not isinstance(item, bool):
                    result[name] = item
                elif isinstance(item, list):
                    item_limit = (
                        3 if name in ("source_urls", "claims_used", "title_options") else 30
                    )
                    if len(item) > item_limit:
                        gaps.append(f"{name}: only first {item_limit} entries provided")
                    if any(isinstance(text, str) and len(text) > text_limit for text in item):
                        gaps.append(f"{name}: oversized entries omitted")
                    result[name] = [
                        text
                        for text in item[:item_limit]
                        if isinstance(text, str)
                        and len(text) <= text_limit
                        and (name != "source_urls" or text.startswith(("https://", "http://")))
                    ]
            return result

        # Fixed dependency depth; never substitute another version from content latest.
        upstream = references(task)
        plans = completed(task, "visual_plan", TaskType.VISUAL_PLAN)
        plan = plans[0] if plans else (task if task.task_type == TaskType.VISUAL_PLAN else None)
        scripts = completed(task if "script" in upstream else plan, "script", TaskType.SHORT_SCRIPT)
        script = scripts[0] if scripts else None
        if script and references(plan).get("script") not in (None, script.id, [script.id]):
            script = None
            plan = None
        research_owner = task if "research" in upstream else script
        research_tasks = completed(research_owner, "research", TaskType.CONTENT_RESEARCH)
        research_task = research_tasks[0] if research_tasks else None
        if (
            research_task
            and "research" in references(script)
            and references(script)["research"] not in (research_task.id, [research_task.id])
        ):
            research_task = None
            gaps.append("research: conflicting exact references; no authoritative claims provided")
        research = (research_task.result_data or {}) if research_task else {}
        used = (script.result_data or {}).get("claims_used", []) if script else []
        claims = research.get("verified_claims") or []
        # Keep the actual script's claims first, not whichever research entries came first.
        claims = sorted(
            (claim for claim in claims if isinstance(claim, dict) and claim.get("id")),
            key=lambda claim: claim["id"] not in used,
        )
        if len(claims) > 10:
            gaps.append("verified_claims: only 10 entries provided, script claims first")
        verified_claims = []
        for claim in claims[:10]:
            fact = fields(claim, ("id", "claim", "source_urls", "confidence"))
            if (
                not isinstance(fact.get("id"), str)
                or not fact["id"].strip()
                or not isinstance(fact.get("claim"), str)
                or not fact["claim"].strip()
                or not fact.get("source_urls")
                or fact.get("confidence") not in ("high", "medium", "low")
            ):
                gaps.append("verified_claims: incomplete filtered fact omitted; not authoritative")
                continue
            verified_claims.append(fact)
        if verified_claims:
            current["verified_claims"] = verified_claims
            current["allowed_claim_ids"] = [str(claim["id"]) for claim in verified_claims]
        current.update(
            fields(research, ("marketing_claims", "uncertain_claims", "recommended_verdict"))
        )
        if script:
            raw_script = script.result_data or {}
            script_result = fields(raw_script, _SCRIPT_RESULT_FIELDS)
            script_result["opening"] = fields(
                raw_script.get("opening"),
                (
                    "product_identity",
                    "special_value",
                    "hook_line",
                    "payoff_line",
                    "scene",
                ),
            )
            for key, names in (
                ("retention_beats", ("at_sec", "beat", "kind")),
                ("visual_requirements", ("purpose", "keywords", "preferred_source")),
            ):
                script_result[key] = [
                    fields(item, names)
                    for item in (raw_script.get(key) or [])[:30]
                    if isinstance(item, dict)
                ]
                if len(raw_script.get(key) or []) > 30:
                    gaps.append(f"script.{key}: only first 30 entries provided")
            current["script"] = script_result
        scene_owner = (
            task
            if "scene_analysis" in upstream
            else plan
            if "scene_analysis" in references(plan)
            else script
        )
        scene_tasks = completed(scene_owner, "scene_analysis", TaskType.SCENE_ANALYZE)
        if scene_tasks:
            current["scene_analysis_sources"] = [
                {
                    "task_id": row.id,
                    "result_sha256": stable_hash(row.result_data),
                    "input_hash": row.input_hash,
                    "scene_count": len((row.result_data or {}).get("scenes") or []),
                }
                for row in scene_tasks
            ]
            scenes = []
            for scene_task in scene_tasks:
                timings = {
                    row.get("scene_id"): fields(row, ("start_sec", "end_sec", "duration_sec"))
                    for row in (scene_task.input_data.get("scenes") or [])
                    if isinstance(row, dict) and isinstance(row.get("scene_id"), str)
                }
                rows = (scene_task.result_data or {}).get("scenes") or []
                if not full_scenes and len(rows) > 120 - len(scenes):
                    gaps.append("scene_analysis: scene count exceeds 120; remaining scenes omitted")
                for row in rows if full_scenes else rows[: max(0, 120 - len(scenes))]:
                    if not isinstance(row, dict) or not isinstance(row.get("scene_id"), str):
                        continue
                    scene = fields(
                        row,
                        (
                            "scene_id",
                            "description",
                            "visual_quality",
                            "information_value",
                            "recommended_usage",
                        ),
                    )
                    scene.update(timings.get(row["scene_id"], {}))
                    if full_scenes:
                        times = [scene.get(key) for key in ("start_sec", "end_sec", "duration_sec")]
                        if not (
                            all(
                                type(value) in {int, float} and math.isfinite(value) and value >= 0
                                for value in times
                            )
                            and times[0] < times[1]
                            and abs(times[1] - times[0] - times[2]) <= 0.001
                        ):
                            gaps.append(f"scene {row['scene_id']}: valid source timing unavailable")
                            for key in ("start_sec", "end_sec", "duration_sec"):
                                scene.pop(key, None)
                    for key, limit in (("description", 360), ("recommended_usage", 240)):
                        if full_scenes and isinstance(row.get(key), str):
                            scene[key] = row[key]
                            continue
                        if isinstance(scene.get(key), str):
                            if len(scene[key]) > limit:
                                gaps.append(f"scene {row['scene_id']}.{key}: prose compacted")
                            scene[key] = scene[key][:limit]
                    scenes.append(scene)
            current["scene_analysis"] = {"scenes": scenes}
        if gaps:
            current["evidence_gaps"] = list(dict.fromkeys(gaps))[:30]
            current["evidence_usage"] = (
                "Incomplete evidence: never treat omitted/compacted details as proof. "
                "Reroute to the appropriate role with full inputs or mark_recovery_blocked."
            )
        current["available_sources"] = self._recovery_available_sources(task.content_id)
        return current

    def _recovery_history(self, content_id: str | None) -> list[dict[str, Any]]:
        if not content_id:
            return []
        rows = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.RECOVERY,
            )
            .order_by(AgentTask.created_at)
        ).all()
        history: list[dict[str, Any]] = []
        for row in rows:
            lineage = row.input_data.get("lineage") or {}
            failure = row.input_data.get("failure") or {}
            result = row.result_data or {}
            failure_class = failure.get("failure_class")
            code = str(failure.get("code") or "AGENT_TASK_FAILED")
            history.append(
                {
                    "task_id": row.id,
                    "round": lineage.get("round", 0),
                    "repair_fingerprint": lineage.get("repair_fingerprint", ""),
                    "root_cause_signature": lineage.get("root_cause_signature")
                    or self._root_cause_signature(failure_class, code),
                    "source_task_id": failure.get("source_task_id"),
                    "status": row.status.value,
                    "action": result.get("action"),
                    "failure_class": failure_class,
                    "code": code,
                    "must_change": list(result.get("must_change") or [])[:20],
                }
            )
        return history

    def _recovery_available_sources(
        self, content_id: str | None
    ) -> dict[str, list[dict[str, Any]]]:
        """Expose bounded lineage metadata so Recovery can route missing dependencies safely."""
        if not content_id:
            return {"tasks": [], "artifacts": []}
        tasks = self.session.exec(
            select(AgentTask)
            .where(AgentTask.content_id == content_id)
            .order_by(AgentTask.created_at.desc())
            .limit(60)
        ).all()
        task_refs = [
            {
                "task_id": task.id,
                "task_type": task.task_type.value,
                "status": task.status.value,
                "input_hash": task.input_hash,
                "result_keys": sorted((task.result_data or {}).keys())[:30],
                "recovery_task_id": str(task.input_data.get("recovery_task_id") or ""),
            }
            for task in tasks
        ]
        artifacts = self.session.exec(
            select(Artifact)
            .where(Artifact.content_id == content_id)
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            .limit(100)
        ).all()
        artifact_refs = [
            {
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "size_bytes": artifact.size_bytes,
                "mime_type": artifact.mime_type,
                "sha256": artifact.sha256,
            }
            for artifact in artifacts
        ]
        return {"tasks": task_refs, "artifacts": artifact_refs}

    @staticmethod
    def _repair_fingerprint(
        source_task: AgentTask,
        *,
        failure_class: str,
        code: str,
    ) -> str:
        """Stable identity for one immutable input/failure pair across manual requeues."""
        return stable_hash(
            {
                "content_id": source_task.content_id,
                "task_type": source_task.task_type.value,
                "input_hash": source_task.input_hash,
                "failure_class": failure_class,
                "code": code or source_task.error_code or "AGENT_TASK_FAILED",
            }
        )[:24]

    @staticmethod
    def _root_cause_signature(failure_class: str | None, code: str | None) -> str:
        """Group recovery rounds by failure class and stable error code."""
        normalized_class = str(failure_class or "UNKNOWN").strip().upper() or "UNKNOWN"
        normalized_code = str(code or "").strip().upper() or "AGENT_TASK_FAILED"
        return stable_hash({"failure_class": normalized_class, "code": normalized_code})[:24]

    def _recovery_for_fingerprint(
        self, content_id: str | None, repair_fingerprint: str
    ) -> AgentTask | None:
        if not content_id or not repair_fingerprint:
            return None
        rows = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.RECOVERY,
            )
            .order_by(AgentTask.created_at.desc())
        ).all()
        for row in rows:
            lineage = row.input_data.get("lineage") or {}
            if lineage.get("repair_fingerprint") == repair_fingerprint:
                return row
        return None

    def create_recovery_task(
        self,
        source_task: AgentTask,
        *,
        failure_class: str | None = None,
        code: str = "",
        message: str = "",
        retryable: bool = False,
        validation_errors: list[dict[str, Any]] | None = None,
        repair_target_task_id: str | None = None,
        actor_id: str = "pipeline",
        force_new: bool = False,
        historical_reconcile: bool = False,
    ) -> AgentTask | None:
        """Create one short-lived Recovery Agent task, idempotently.

        The method only writes a server-built envelope. It never changes the source task to
        success and never grants the recovery Agent arbitrary input or side effects.
        """
        self.recovery_capacity_deferred = False
        if source_task.task_type == TaskType.RECOVERY:
            return None
        source_attempt = max(0, int(source_task.attempt))
        existing_rows = [
            row
            for row in self.session.exec(
                select(AgentTask)
                .where(AgentTask.task_type == TaskType.RECOVERY)
                .order_by(AgentTask.created_at.desc())
            ).all()
            if str((row.input_data.get("failure") or {}).get("source_task_id") or "")
            == source_task.id
            and int(
                (row.input_data.get("failure") or {}).get("source_attempt")
                if (row.input_data.get("failure") or {}).get("source_attempt") is not None
                else -1
            )
            == source_attempt
        ]
        if existing_rows:
            latest = existing_rows[0]
            if not force_new:
                return latest
            if latest.status == TaskStatus.COMPLETED and force_new:
                related = self.session.exec(
                    select(AgentTask).where(AgentTask.content_id == source_task.content_id)
                ).all()
                if any(
                    row.task_type != TaskType.RECOVERY
                    and row.status
                    in {
                        TaskStatus.PENDING,
                        TaskStatus.LEASED,
                        TaskStatus.SUBMITTED,
                        TaskStatus.COMPLETED,
                    }
                    and str(row.input_data.get("recovery_task_id") or "") == latest.id
                    for row in related
                ):
                    return latest

        failure_class = failure_class or self.classify_recovery_failure(
            source_task, code=code, retryable=retryable
        )
        if failure_class not in {
            "BUSINESS_QC",
            "TASK_CONTRACT",
            "CONTEXT_MISSING",
            "TRANSIENT_EXECUTOR",
            "RIGHTS_OR_PUBLISHING",
            "UNKNOWN",
        }:
            failure_class = "UNKNOWN"
        normalized_code = code or source_task.error_code or "AGENT_TASK_FAILED"
        root_cause_signature = self._root_cause_signature(failure_class, normalized_code[:120])
        repair_fingerprint = self._repair_fingerprint(
            source_task,
            failure_class=failure_class,
            code=normalized_code,
        )
        all_history = self._recovery_history(source_task.content_id)
        history = all_history[-10:]
        # A transient executor failure is allowed one Recovery task per lease attempt;
        # the existing same-task retry budget controls how many attempts are possible.
        # For contract/context failures the input/error pair is immutable, so creating
        # another Recovery task would only replay the same broken package forever.
        if failure_class != "TRANSIENT_EXECUTOR":
            existing_fingerprint_recovery = self._recovery_for_fingerprint(
                source_task.content_id, repair_fingerprint
            )
            if existing_fingerprint_recovery is not None:
                existing_source_id = str(
                    (existing_fingerprint_recovery.input_data.get("failure") or {}).get(
                        "source_task_id"
                    )
                    or ""
                )
                if existing_source_id != source_task.id:
                    self.session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id=actor_id,
                            entity_type="agent_task",
                            entity_id=source_task.id,
                            action="recovery_duplicate_linked",
                            metadata_json={
                                "repair_fingerprint": repair_fingerprint,
                                "recovery_task_id": existing_fingerprint_recovery.id,
                                "source_task_id": existing_source_id,
                            },
                        )
                    )
                    self.session.commit()
                # A fingerprinted recovery is already the canonical chain for this source.
                # Even force_new historical reconciliation must stay idempotent; a cancelled
                # descendant may simply be waiting for an upstream repair.
                return existing_fingerprint_recovery
        auto_repair_target = self._deterministic_repair_target(
            source_task,
            code=code,
            failure_class=failure_class,
            allow_workspace_rebuild=historical_reconcile,
        )
        round_number = 1 + sum(
            item.get("root_cause_signature") == root_cause_signature for item in all_history
        )
        if round_number > self.recovery_max_rounds and auto_repair_target is None:
            reason = (
                f"恢复预算已耗尽（{round_number}/{self.recovery_max_rounds}），"
                "不会重试原任务；请人工创建新的修复版本"
            )
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="agent_task",
                    entity_id=source_task.id,
                    action="recovery_budget_exhausted",
                    metadata_json={
                        "round": round_number,
                        "max_rounds": self.recovery_max_rounds,
                        "failure_class": failure_class,
                        "code": normalized_code[:120],
                        "root_cause_signature": root_cause_signature,
                    },
                )
            )
            self.session.commit()
            self._mark_source_recovery_blocked(source_task, reason=reason, actor_id=actor_id)
            return None
        if round_number > self.recovery_max_rounds and auto_repair_target is not None:
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="agent_task",
                    entity_id=source_task.id,
                    action="recovery_deterministic_repair_over_budget",
                    metadata_json={
                        "round": round_number,
                        "max_rounds": self.recovery_max_rounds,
                        "target_task_type": auto_repair_target,
                        "failure_class": failure_class,
                        "code": normalized_code[:120],
                        "root_cause_signature": root_cause_signature,
                    },
                )
            )
        if failure_class == "UNKNOWN":
            unknown_rounds = sum(1 for item in history if item.get("failure_class") == "UNKNOWN")
            if unknown_rounds >= self.recovery_unknown_attempts:
                reason = (
                    f"未知失败恢复已达到上限（{unknown_rounds}/{self.recovery_unknown_attempts}），"
                    "不会继续派发；请人工判断"
                )
                self.session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id=actor_id,
                        entity_type="agent_task",
                        entity_id=source_task.id,
                        action="recovery_unknown_escalated",
                        metadata_json={
                            "attempts": unknown_rounds,
                            "max_attempts": self.recovery_unknown_attempts,
                        },
                    )
                )
                self.session.commit()
                self._mark_source_recovery_blocked(source_task, reason=reason, actor_id=actor_id)
                return None
        if not self.recovery_enabled:
            reason = "服务端恢复功能已关闭，未创建恢复任务；请人工处理"
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id=actor_id,
                    entity_type="agent_task",
                    entity_id=source_task.id,
                    action="recovery_disabled",
                )
            )
            self.session.commit()
            self._mark_source_recovery_blocked(source_task, reason=reason, actor_id=actor_id)
            return None
        active_count = len(
            self.session.exec(
                select(AgentTask).where(
                    AgentTask.task_type == TaskType.RECOVERY,
                    AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
                )
            ).all()
        )
        if active_count >= self.recovery_max_parallel and auto_repair_target is None:
            self.recovery_capacity_deferred = True
            target_id = repair_target_task_id or source_task.id
            target = self.session.get(AgentTask, target_id)
            identity = {
                "content_id": source_task.content_id,
                "source_task_type": source_task.task_type.value,
                "source_attempt": source_attempt,
                "source_status": source_task.status.value,
                "input_hash": source_task.input_hash,
                "target_task_type": target.task_type.value if target else None,
                "target_input_hash": target.input_hash if target else None,
            }
            pending = [
                event
                for event in self._pending_recovery_deferrals()
                if event.entity_id == source_task.id
            ]
            if not any(
                all(event.metadata_json.get(key) == value for key, value in identity.items())
                and event.metadata_json["request"]["repair_target_task_id"] == target_id
                for event in pending
            ):
                for event in pending:
                    self.session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id=actor_id,
                            entity_type="audit_event",
                            entity_id=event.id,
                            action="recovery_capacity_resolved",
                            metadata_json={
                                "reason": "superseded_by_explicit_current_request",
                                "source_task_id": source_task.id,
                            },
                        )
                    )
                self.session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id=actor_id,
                        entity_type="agent_task",
                        entity_id=source_task.id,
                        action="recovery_capacity_deferred",
                        metadata_json={
                            **identity,
                            "request": {
                                "failure_class": failure_class,
                                "code": normalized_code[:120],
                                "message": (message or source_task.error_message or "")[:4000],
                                "retryable": retryable,
                                "validation_errors": (
                                    validation_errors
                                    or self._latest_submission_errors(source_task.id)
                                )[:30],
                                "repair_target_task_id": target_id,
                                "actor_id": actor_id,
                                "force_new": force_new,
                                "historical_reconcile": historical_reconcile,
                            },
                        },
                    )
                )
                self.session.commit()
            return None

        if source_task.status in {TaskStatus.PENDING, TaskStatus.LEASED}:
            # A recovery owns the failed attempt. Do not leave the old queue entry
            # visible to a later scheduler pass while recovery is in progress.
            self.tasks.cancel(source_task.id, actor_id="recovery-coordinator")
            source_task = self.session.get(AgentTask, source_task.id) or source_task

        validation_errors = validation_errors or self._latest_submission_errors(source_task.id)
        envelope = RecoveryFailureEnvelope(
            failure_class=failure_class,
            code=(code or source_task.error_code or "AGENT_TASK_FAILED")[:120],
            message=(message or source_task.error_message or "")[:4000],
            source_task_id=source_task.id,
            source_task_type=source_task.task_type.value,
            source_attempt=source_attempt,
            retryable=retryable,
            validation_errors=validation_errors[:30],
        )
        content = (
            self.session.get(Content, source_task.content_id) if source_task.content_id else None
        )
        target_id = repair_target_task_id or source_task.id
        repair_target = self.session.get(AgentTask, target_id)
        source_lineage = source_task.input_data.get("lineage") or {}
        same_task_retries = sum(
            1
            for item in history
            if item.get("action") == "retry_same_task"
            and item.get("source_task_id") == source_task.id
        )
        lineage = {
            "root_task_id": source_lineage.get("root_task_id", source_task.id),
            "parent_task_id": source_task.id,
            "repair_target_task_id": target_id,
            "content_id": source_task.content_id,
            "round": round_number,
            "max_rounds": self.recovery_max_rounds,
            "source_attempt": source_attempt,
            "same_task_retries": same_task_retries,
            "repair_fingerprint": repair_fingerprint,
            "root_cause_signature": root_cause_signature,
            "target_task_type": (
                auto_repair_target
                or (
                    repair_target.task_type.value
                    if repair_target is not None
                    else source_task.task_type.value
                )
            ),
        }
        allowed_actions = set(RECOVERY_ALLOWED_ACTIONS)
        try:
            self.tasks.check_requeue(target_id, allow_attempt_extension=False)
        except (KeyError, TaskConflictError):
            allowed_actions.discard("retry_same_task")
        if (
            failure_class != "TRANSIENT_EXECUTOR"
            or same_task_retries >= self.recovery_same_task_retries
        ):
            allowed_actions.discard("retry_same_task")
        policy = {
            "version": "recovery-v1",
            "enabled": self.recovery_enabled,
            "max_rounds": self.recovery_max_rounds,
            "same_task_retries": self.recovery_same_task_retries,
            "unknown_attempts": self.recovery_unknown_attempts,
            "max_parallel": self.recovery_max_parallel,
            "allowed_actions": sorted(allowed_actions),
            "hard_limits": {
                "cannot_publish": True,
                "cannot_delete": True,
                "cannot_modify_evidence": True,
                "cannot_modify_business_status": True,
                "cannot_execute_commands": True,
            },
        }
        input_data = {
            "failure": envelope.model_dump(mode="json"),
            "lineage": lineage,
            "current_input": self._recovery_current_input(source_task),
            "current_result": (
                (repair_target.result_data or self._latest_submission_result(repair_target))
                if repair_target is not None
                else source_task.result_data or self._latest_submission_result(source_task)
            ),
            "failure_result": source_task.result_data
            or self._latest_submission_result(source_task),
            "repair_history": history,
            "policy": policy,
            "allowed_actions": sorted(allowed_actions),
            "source_context": {
                "content_id": source_task.content_id,
                "content_title": content.title_zh or content.title if content else "",
                "source_task_type": source_task.task_type.value,
            },
        }
        if content is not None:
            candidate = self.session.get(Candidate, content.candidate_id)
            if candidate is not None:
                input_data["source_context"]["candidate"] = self._candidate_task_input(candidate)
        recovery = self.tasks.create_task(
            task_type=TaskType.RECOVERY,
            content_id=source_task.content_id,
            instructions=resolve_instructions(self.session, TaskType.RECOVERY, base_settings=self.base_settings, opening_identity_overlay=self.opening_identity_overlay),
            input_data=input_data,
            priority=60,
        )
        if content is not None:
            content.business_status = ContentStatus.WAITING_AGENT
            content.current_stage = "recovery"
            content.error_code = envelope.code
            content.error_message = envelope.message or "Agent 任务失败，等待助手 Agent 诊断恢复"
            content.updated_at = utc_now()
            self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id=actor_id,
                entity_type="agent_task",
                entity_id=recovery.id,
                action="recovery_task_created",
                metadata_json={
                    "source_task_id": source_task.id,
                    "failure_class": failure_class,
                    "code": envelope.code,
                    "root_cause_signature": root_cause_signature,
                    "round": round_number,
                    "target_task_id": target_id,
                },
            )
        )
        if auto_repair_target is not None:
            auto_plan = RecoveryPlan(
                failure_class=failure_class,
                action="repair_task_context",
                target_task_type=auto_repair_target,
                reason="服务端已确认这是可由控制面元数据和已有素材确定性修复的任务包问题",
                must_change=(
                    ["production_mode 必须取内容表的制作模式"]
                    if auto_repair_target == TaskType.CONTENT_RESEARCH.value
                    else (
                        ["context_files 必须与已有场景缩略图逐项对应"]
                        if auto_repair_target
                        in {TaskType.SCENE_ANALYZE.value, TaskType.VISUAL_PLAN_QC.value}
                        else [
                            "production_mode 必须取内容表的制作模式",
                            "脚本修复必须重新物化已完成的研究和场景依赖",
                        ]
                    )
                ),
                must_preserve=["原失败任务、原始媒体和审计记录"],
                must_not_do=["不得覆盖原失败任务或凭空生成素材路径"],
                evidence_refs=[source_task.id],
                acceptance_checks=["新任务输入通过服务端任务包预检"],
                reviewer_note="这是服务端确定性修复，不需要重复重试原任务。",
            )
            recovery.status = TaskStatus.COMPLETED
            recovery.result_data = auto_plan.model_dump(mode="json")
            recovery.completed_at = utc_now()
            self.session.add(recovery)
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="recovery-coordinator",
                    entity_type="agent_task",
                    entity_id=recovery.id,
                    action="recovery_auto_plan_created",
                    metadata_json={
                        "source_task_id": source_task.id,
                        "target_task_type": auto_repair_target,
                        "reason": "deterministic_task_bundle_repair",
                    },
                )
            )
        self.session.commit()
        self.session.refresh(recovery)
        if auto_repair_target is not None:
            self.apply_recovery_plan(recovery)
        return recovery

    def _pending_recovery_deferrals(self) -> list[AuditEvent]:
        resolved = select(AuditEvent.entity_id).where(
            AuditEvent.action == "recovery_capacity_resolved"
        )
        return list(
            self.session.exec(
                select(AuditEvent)
                .where(
                    AuditEvent.action == "recovery_capacity_deferred",
                    AuditEvent.id.notin_(resolved),
                )
                .order_by(AuditEvent.created_at)
            ).all()
        )

    def has_deferred_recovery(self, content_id: str) -> bool:
        return any(
            event.metadata_json.get("content_id") == content_id
            for event in self._pending_recovery_deferrals()
        )

    def retry_deferred_recoveries(self) -> list[str]:
        """Replay only recorded capacity deferrals, never archived history or guessed lineage."""
        admitted = []
        for event in self._pending_recovery_deferrals()[:100]:
            data = event.metadata_json
            source = self.session.get(AgentTask, event.entity_id)
            target = self.session.get(AgentTask, data["request"]["repair_target_task_id"])
            content = (
                self.session.get(Content, source.content_id)
                if source and source.content_id
                else None
            )
            if (
                source is not None
                and source.content_id == data.get("content_id")
                and source.task_type.value == data.get("source_task_type")
                and source.attempt == data.get("source_attempt")
                and source.input_hash == data.get("input_hash")
                and source.status.value == data.get("source_status")
                and source.status != TaskStatus.CANCELLED
                and target is not None
                and target.content_id == source.content_id
                and target.task_type.value == data.get("target_task_type")
                and target.input_hash == data.get("target_input_hash")
                and target.status != TaskStatus.CANCELLED
                and (not source.content_id or content is not None)
                and (
                    content is None
                    or content.business_status
                    not in {ContentStatus.ARCHIVED, ContentStatus.PUBLISHED}
                )
            ):
                recovery = self.create_recovery_task(source, **data["request"])
                if self.recovery_capacity_deferred:
                    break
                if recovery is not None:
                    admitted.append(recovery.id)
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="recovery-coordinator",
                    entity_type="audit_event",
                    entity_id=event.id,
                    action="recovery_capacity_resolved",
                    metadata_json={"source_task_id": event.entity_id},
                )
            )
            self.session.commit()
            if admitted:
                break
        return admitted

    def _proven_visual_asset_resolution(
        self, plan: AgentTask, source: AgentTask, tasks: list[AgentTask], events: list[AuditEvent]
    ) -> bool:
        """Prove a complete result changed only by exact, approved asset materialization."""
        if plan.task_type != TaskType.VISUAL_PLAN or source.completed_at is None:
            return False

        def accepted_result(task: AgentTask) -> tuple[dict[str, Any], str] | None:
            if (
                task.content_id != source.content_id
                or task.status != TaskStatus.COMPLETED
                or task.completed_at is None
                or as_utc(task.completed_at) > as_utc(source.created_at)
                or task.input_hash != stable_hash(task.input_data)
            ):
                return None
            submissions = self.session.exec(
                select(AgentSubmission).where(
                    AgentSubmission.task_id == task.id,
                    AgentSubmission.attempt == task.attempt,
                    AgentSubmission.validation_status == "ACCEPTED",
                )
            ).all()
            if len(submissions) != 1:
                return None
            submission = submissions[0]
            root = self.tasks.submission_root.resolve()
            expected = (root / task.id / str(task.attempt) / "result.json").resolve()
            recorded = Path(submission.raw_result_path)
            if not recorded.is_absolute():
                recorded = self.data_dir.parent / recorded
            if root not in expected.parents or recorded.resolve() != expected:
                return None
            try:
                raw = json.loads(expected.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if not isinstance(raw, dict) or stable_hash(raw) != submission.result_hash:
                return None
            matching = [
                event
                for event in events
                if event.entity_id == task.id
                and event.entity_type == "agent_task"
                and event.action == "completed"
                and event.actor_type == "agent"
                and event.actor_id == submission.agent_id
                and event.metadata_json.get("result_hash") == submission.result_hash
                and as_utc(submission.created_at)
                <= as_utc(event.created_at)
                <= as_utc(source.created_at)
            ]
            if len(matching) != 1:
                return None
            return raw, submission.agent_id

        original = accepted_result(plan)
        current = plan.result_data or {}
        if original is None or not isinstance(original[0].get("segments"), list):
            return False
        rebuilt = deepcopy(original[0])
        segments = current.get("segments")
        if not isinstance(segments, list) or len(segments) != len(rebuilt["segments"]):
            return False
        by_id = {task.id: task for task in tasks}
        changed = False
        for index, (before, after) in enumerate(zip(rebuilt["segments"], segments, strict=True)):
            if before == after:
                continue
            if (
                not isinstance(before, dict)
                or not isinstance(after, dict)
                or not isinstance(before.get("asset_request"), dict)
                or before.get("asset_id") is not None
                or before.get("scene_id") is not None
                or not isinstance(after.get("asset_id"), str)
            ):
                return False
            artifact = self.session.get(Artifact, after["asset_id"])
            generation = by_id.get(artifact.producer_stage) if artifact else None
            if (
                artifact is None
                or artifact.content_id != source.content_id
                or artifact.artifact_type != "agent_generated_image"
                or not artifact.mime_type.startswith("image/")
                or generation is None
                or generation.task_type != TaskType.ASSET_GENERATE_IMAGE
                or (generation.input_data.get("upstream_task_ids") or {}).get("visual_plan")
                != plan.id
                or generation.input_data.get("asset_purpose") == "cover"
            ):
                return False
            generated = accepted_result(generation)
            if (
                generated is None
                or generated[0] != generation.result_data
                or stable_hash(generated[0]) != stable_hash(generation.result_data)
            ):
                return False
            owners = [
                task
                for task in tasks
                if task.task_type == TaskType.ASSET_GENERATE_IMAGE
                and (task.input_data.get("upstream_task_ids") or {}).get("visual_plan") == plan.id
                and task.input_data.get("asset_purpose") != "cover"
                and any(
                    isinstance(entry, dict) and entry.get("segment_index") == index
                    for entry in task.input_data.get("requests", [])
                )
            ]
            if len(owners) != 1 or owners[0].id != generation.id:
                return False
            requests = [
                entry
                for entry in generation.input_data.get("requests", [])
                if isinstance(entry, dict) and entry.get("segment_index") == index
            ]
            images = [
                image
                for image in generated[0].get("images", [])
                if isinstance(image, dict) and image.get("segment_index") == index
            ]
            if (
                len(requests) != 1
                or len(images) != 1
                or requests[0].get("asset_request") != before["asset_request"]
                or images[0].get("artifact_id") != artifact.id
            ):
                return False
            approvals = [
                task
                for task in tasks
                if task.task_type == TaskType.ASSET_QC
                and (task.input_data.get("upstream_task_ids") or {}).get("visual_plan") == plan.id
                and (task.input_data.get("upstream_task_ids") or {}).get("asset_generate_image")
                == generation.id
            ]
            if len(approvals) != 1:
                return False
            qc = approvals[0]
            approved = accepted_result(qc)
            if (
                approved is None
                or approved[0] != qc.result_data
                or stable_hash(approved[0]) != stable_hash(qc.result_data)
                or approved[0].get("passed") is not True
            ):
                return False
            assets = [
                entry
                for entry in qc.input_data.get("assets", [])
                if isinstance(entry, dict) and entry.get("segment_index") == index
            ]
            if (
                len(assets) != 1
                or assets[0].get("artifact_id") != artifact.id
                or assets[0].get("request") != before["asset_request"]
                or assets[0].get("generation") != images[0]
                or not as_utc(plan.completed_at)
                <= as_utc(generation.created_at)
                <= as_utc(artifact.created_at)
                <= as_utc(generation.completed_at)
                <= as_utc(qc.created_at)
                <= as_utc(qc.completed_at)
                <= as_utc(source.created_at)
            ):
                return False
            uploads = self.session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "artifact",
                    AuditEvent.entity_id == artifact.id,
                    AuditEvent.action == "generated_artifact_uploaded",
                )
            ).all()
            if (
                len(uploads) != 1
                or uploads[0].actor_type != "agent"
                or uploads[0].actor_id != generated[1]
                or uploads[0].metadata_json.get("task_id") != generation.id
                or uploads[0].metadata_json.get("mime_type") != artifact.mime_type
                or not as_utc(generation.created_at)
                <= as_utc(uploads[0].created_at)
                <= as_utc(generation.completed_at)
            ):
                return False
            before.pop("asset_request")
            before["asset_id"] = artifact.id
            changed = True
        # No projection: preserve every top-level/segment field, type, order and value.
        return changed and rebuilt == current and stable_hash(rebuilt) == stable_hash(current)

    def reconcile_capacity_archives(self) -> list[str]:
        """Undo only an audit-proven SYSTEM capacity skip, through the normal factory."""
        if (
            not self.recovery_enabled
            or len(
                self.session.exec(
                    select(AgentTask).where(
                        AgentTask.task_type == TaskType.RECOVERY,
                        AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
                    )
                ).all()
            )
            >= self.recovery_max_parallel
        ):
            return []
        resolved = select(AuditEvent.entity_id).where(
            AuditEvent.action == "recovery_capacity_archive_reconciled"
        )
        skips = self.session.exec(
            select(AuditEvent)
            .where(
                AuditEvent.action == "handsfree_content_skipped",
                AuditEvent.actor_type == "system",
                AuditEvent.actor_id == "handsfree-coordinator",
                AuditEvent.entity_type == "content",
                AuditEvent.id.notin_(resolved),
            )
            .order_by(AuditEvent.created_at.desc())
            .limit(100)
        ).all()
        for skip in skips:
            previous = skip.metadata_json.get("previous") or {}
            content = self.session.get(Content, skip.entity_id)
            if (
                previous
                != {
                    "business_status": ContentStatus.FAILED.value,
                    "current_stage": "recovery_blocked",
                    "error_code": "RECOVERY_BLOCKED",
                }
                or content is None
                or content.business_status != ContentStatus.ARCHIVED
                or content.current_stage != "archived"
                or content.error_code != "HANDSFREE_SKIPPED"
                # _skip_content timestamps the content just before writing the audit.
                or not 0
                <= (as_utc(skip.created_at) - as_utc(content.updated_at)).total_seconds()
                <= 1
            ):
                continue
            candidate = self.session.get(Candidate, content.candidate_id)
            if (
                candidate is None
                or skip.metadata_json.get("candidate_id") != candidate.id
                or candidate.status != CandidateStatus.IGNORED
                or not 0
                <= (as_utc(skip.created_at) - as_utc(candidate.updated_at)).total_seconds()
                <= 1
                or candidate.rights_status
                not in {
                    RightsStatus.SELF_OWNED,
                    RightsStatus.EXPLICIT_LICENSE,
                    RightsStatus.OFFICIAL_REFERENCE,
                }
                or self._topic_conflicts(candidate)
                or any(
                    normalize_url(other.source_url) == normalize_url(candidate.source_url)
                    or normalize_title(other.original_title)
                    == normalize_title(candidate.original_title)
                    for other in self.session.exec(
                        select(Candidate)
                        .join(Content, Content.candidate_id == Candidate.id)
                        .where(
                            Candidate.id != candidate.id,
                            Content.business_status != ContentStatus.ARCHIVED,
                        )
                    ).all()
                )
            ):
                continue
            tasks = self.session.exec(
                select(AgentTask).where(AgentTask.content_id == content.id)
            ).all()
            ids = [content.id, candidate.id, *(task.id for task in tasks)]
            events = self.session.exec(
                select(AuditEvent)
                .where(AuditEvent.entity_id.in_(ids))
                .order_by(AuditEvent.created_at, AuditEvent.id)
            ).all()
            # Later user/system changes, replacements, or hard stops invalidate the chain.
            if (
                any(as_utc(event.created_at) > as_utc(skip.created_at) for event in events)
                or any(
                    event.action == "handsfree_content_skipped"
                    and event.id != skip.id
                    and as_utc(event.created_at) >= as_utc(skip.created_at)
                    for event in events
                )
                or any(
                    event.action
                    in {
                        "recovery_budget_exhausted",
                        "recovery_blocked",
                        "recovery_escalated",
                        "recovery_unknown_escalated",
                        "recovery_disabled",
                        "candidate_ignored",
                        "content_archived",
                    }
                    for event in events
                )
                or any(
                    task.status in {TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED}
                    or as_utc(task.created_at) > as_utc(skip.created_at)
                    for task in tasks
                )
                or self.session.exec(
                    select(PublicationRecord).where(PublicationRecord.content_id == content.id)
                ).first()
                is not None
                or self.session.exec(
                    select(ContentIssue).where(
                        ContentIssue.content_id == content.id,
                        ContentIssue.classification == IssueClassification.POLICY,
                    )
                ).first()
                is not None
            ):
                continue
            window = [
                event
                for event in events
                if 0 <= (as_utc(skip.created_at) - as_utc(event.created_at)).total_seconds() <= 30
            ]
            blocked = [
                event
                for event in window
                if event.action == "recovery_concurrency_blocked"
                and event.actor_type == "system"
                and event.actor_id == "handsfree-coordinator"
                and event.entity_type == "agent_task"
            ]
            if len(blocked) != 1:
                continue
            capacity = blocked[0]
            active, maximum = (
                capacity.metadata_json.get(key) for key in ("active", "max_parallel")
            )
            if (
                not isinstance(active, int)
                or isinstance(active, bool)
                or not isinstance(maximum, int)
                or isinstance(maximum, bool)
                or not active >= maximum > 0
            ):
                continue
            source_events = [
                event
                for event in window
                if event.entity_id == capacity.entity_id
                and as_utc(event.created_at) >= as_utc(capacity.created_at)
                and event.action.startswith("recovery_")
            ]
            if (
                len(source_events) != 2
                or {event.action for event in source_events}
                != {"recovery_concurrency_blocked", "recovery_blocked_without_task"}
                or any(
                    as_utc(event.created_at) < as_utc(capacity.created_at)
                    or event.actor_type != "system"
                    or event.actor_id != capacity.actor_id
                    for event in source_events
                )
                or any(
                    event.actor_type == "human"
                    and as_utc(event.created_at) >= as_utc(capacity.created_at)
                    # cancel() records actor_type=human even for server-issued skips.
                    and not (
                        event.action == "cancelled"
                        and event.actor_id == "handsfree-skip"
                        and event.entity_id in (skip.metadata_json.get("cancelled_task_ids") or [])
                        and as_utc(event.created_at) <= as_utc(skip.created_at)
                    )
                    for event in events
                )
            ):
                continue
            source = self.session.get(AgentTask, capacity.entity_id)
            if (
                source is None
                or source.content_id != content.id
                or source.task_type != TaskType.CONTENT_QC
                or source.status != TaskStatus.COMPLETED
                or (source.result_data or {}).get("passed") is not False
                or source.input_hash != stable_hash(source.input_data)
                or source.completed_at is None
                or as_utc(source.completed_at) > as_utc(capacity.created_at)
                or any(
                    task.task_type == TaskType.CONTENT_QC
                    and task.id != source.id
                    and as_utc(task.created_at) >= as_utc(source.created_at)
                    for task in tasks
                )
            ):
                continue
            plan_id = (source.input_data.get("upstream_task_ids") or {}).get("visual_plan")
            plan = self.session.get(AgentTask, plan_id) if isinstance(plan_id, str) else None
            history = self._recovery_history(content.id)
            signature = self._root_cause_signature("BUSINESS_QC", "CONTENT_QC_FAILED")
            fingerprint = self._repair_fingerprint(
                source, failure_class="BUSINESS_QC", code="CONTENT_QC_FAILED"
            )
            if (
                plan is None
                or plan.content_id != content.id
                or plan.task_type != TaskType.VISUAL_PLAN
                or plan.status != TaskStatus.COMPLETED
                or not (plan.result_data or {}).get("segments")
                or plan.input_hash != stable_hash(plan.input_data)
                or plan.completed_at is None
                or as_utc(plan.completed_at) > as_utc(source.created_at)
                or any(
                    item["source_task_id"] == source.id or item["repair_fingerprint"] == fingerprint
                    for item in history
                )
                or sum(item["root_cause_signature"] == signature for item in history)
                >= self.recovery_max_rounds
            ):
                continue
            cancelled = skip.metadata_json.get("cancelled_task_ids")
            if not isinstance(cancelled, list) or any(
                not any(
                    task.id == task_id
                    and task.task_type == TaskType.FINAL_REVIEW_ASSIST
                    and task.status == TaskStatus.CANCELLED
                    for task in tasks
                )
                for task_id in cancelled
            ):
                continue
            if any(
                as_utc(task.created_at) >= as_utc(skip.created_at)
                and task.id not in {source.id, plan.id, *cancelled}
                for task in tasks
            ):
                continue
            if not any(
                event.entity_id == source.id
                and event.action == "completed"
                and event.actor_type == "agent"
                and event.metadata_json.get("result_hash") == stable_hash(source.result_data)
                for event in events
            ):
                continue
            if not any(
                event.entity_id == plan.id
                and event.action == "completed"
                and event.actor_type == "agent"
                and event.metadata_json.get("result_hash") == stable_hash(plan.result_data)
                for event in events
            ) and not self._proven_visual_asset_resolution(plan, source, tasks, events):
                continue
            video_id = source.input_data.get("video_artifact_id")
            video = self.session.get(Artifact, video_id) if isinstance(video_id, str) else None
            if (
                video is None
                or video.artifact_type != "final_video"
                or not video.mime_type.startswith("video/")
            ):
                continue
            artifact_ids = {video.id}
            for segment in plan.result_data["segments"]:
                if isinstance(segment, dict) and segment.get("asset_id"):
                    artifact_ids.add(segment["asset_id"])
                request = segment.get("asset_request") if isinstance(segment, dict) else None
                if isinstance(request, dict) and request.get("reference_artifact_id"):
                    artifact_ids.add(request["reference_artifact_id"])
            root = Path(content.content_dir).resolve()
            intact = self.data_dir.resolve() in root.parents
            try:
                for artifact_id in artifact_ids:
                    artifact = self.session.get(Artifact, artifact_id)
                    if (
                        artifact is None
                        or artifact.content_id != content.id
                        or as_utc(artifact.created_at) > as_utc(source.created_at)
                    ):
                        intact = False
                        break
                    resolved_file = self._content_file(root, artifact.relative_path)
                    if (
                        resolved_file is None
                        or resolved_file[1].stat().st_size != artifact.size_bytes
                    ):
                        intact = False
                        break
                    with resolved_file[1].open("rb") as stream:
                        if hashlib.file_digest(stream, "sha256").hexdigest() != artifact.sha256:
                            intact = False
                            break
                if any(
                    self._content_file(root, path) is None
                    for path in source.input_data.get("context_files", [])
                ):
                    intact = False
            except (OSError, TypeError, ValueError):
                intact = False
            if not intact:
                continue
            recovery = self.create_qc_recovery_task(source, plan, actor_id="handsfree-coordinator")
            if (
                recovery is None
                or content.business_status != ContentStatus.WAITING_AGENT
                or content.current_stage != "recovery"
            ):
                return []
            candidate.status = CandidateStatus.SELECTED
            candidate.updated_at = utc_now()
            self.session.add(candidate)
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="recovery-coordinator",
                    entity_type="audit_event",
                    entity_id=skip.id,
                    action="recovery_capacity_archive_reconciled",
                    metadata_json={
                        "content_id": content.id,
                        "source_task_id": source.id,
                        "target_task_id": plan.id,
                        "recovery_task_id": recovery.id,
                        "capacity_audit_id": capacity.id,
                        "input_hash": source.input_hash,
                        "target_input_hash": plan.input_hash,
                        "candidate_id": candidate.id,
                        "previous_candidate_status": CandidateStatus.IGNORED.value,
                        "candidate_status": CandidateStatus.SELECTED.value,
                    },
                )
            )
            self.session.commit()
            return [recovery.id]
        return []

    def create_qc_recovery_task(
        self,
        qc_task: AgentTask,
        repair_target_task: AgentTask,
        *,
        actor_id: str = "pipeline",
    ) -> AgentTask | None:
        issues = [
            issue
            for issue in ((qc_task.result_data or {}).get("issues") or [])
            if isinstance(issue, dict)
        ]
        message = "；".join(
            f"{issue.get('field', 'unknown')}: {issue.get('message', '')}" for issue in issues
        )[:4000]
        return self.create_recovery_task(
            qc_task,
            failure_class="BUSINESS_QC",
            code=f"{qc_task.task_type.value.upper()}_FAILED",
            message=message or "质检未通过",
            validation_errors=issues,
            repair_target_task_id=repair_target_task.id,
            actor_id=actor_id,
        )

    def _cancel_open_content_tasks(
        self, content_id: str, task_types: set[TaskType], *, reason: str
    ) -> int:
        """Cancel only in-flight descendants; completed history remains immutable."""
        tasks = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type.in_(task_types),
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
            )
        ).all()
        for task in tasks:
            self.tasks.cancel(task.id, actor_id=reason)
        return len(tasks)

    def _mark_recovery_blocked(self, recovery_task: AgentTask, *, reason: str, action: str) -> None:
        content = (
            self.session.get(Content, recovery_task.content_id)
            if recovery_task.content_id
            else None
        )
        if content is not None:
            target_type = str(
                (recovery_task.input_data.get("lineage") or {}).get("target_task_type") or ""
            )
            content.business_status = (
                ContentStatus.SCRIPT_REVIEW
                if target_type in {TaskType.SHORT_SCRIPT.value, TaskType.SCRIPT_QC.value}
                else ContentStatus.FAILED
            )
            content.current_stage = "recovery_blocked"
            content.error_code = "RECOVERY_BLOCKED"
            content.error_message = reason[:2000]
            content.updated_at = utc_now()
            self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=recovery_task.id,
                action="recovery_blocked"
                if action == "mark_recovery_blocked"
                else "recovery_escalated",
                metadata_json={"reason": reason[:2000], "action": action},
            )
        )
        self.session.commit()

    def _mark_source_recovery_blocked(
        self, source_task: AgentTask, *, reason: str, actor_id: str
    ) -> None:
        """Make a budget/coordination stop visible even when no Recovery row exists."""
        content = (
            self.session.get(Content, source_task.content_id) if source_task.content_id else None
        )
        if content is not None:
            target_type = str(
                (source_task.input_data.get("lineage") or {}).get("target_task_type") or ""
            )
            content.business_status = (
                ContentStatus.SCRIPT_REVIEW
                if target_type in {TaskType.SHORT_SCRIPT.value, TaskType.SCRIPT_QC.value}
                else ContentStatus.FAILED
            )
            content.current_stage = "recovery_blocked"
            content.error_code = "RECOVERY_BLOCKED"
            content.error_message = reason[:2000]
            content.updated_at = utc_now()
            self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id=actor_id,
                entity_type="agent_task",
                entity_id=source_task.id,
                action="recovery_blocked_without_task",
                metadata_json={"reason": reason[:2000]},
            )
        )
        self.session.commit()

    def _create_script_repair_from_recovery(
        self,
        recovery_task: AgentTask,
        plan: RecoveryPlan,
        *,
        target_task: AgentTask,
    ) -> AgentTask | None:
        if target_task.task_type == TaskType.SCRIPT_REVIEW_ASSIST:
            return self._create_script_review_repair_from_recovery(
                recovery_task, plan, target_task=target_task
            )
        content = (
            self.session.get(Content, recovery_task.content_id)
            if recovery_task.content_id
            else None
        )
        if content is None:
            self._mark_recovery_blocked(
                recovery_task,
                reason="脚本修复缺少 content_id，不能安全生成子任务",
                action=plan.action,
            )
            return None
        script_task = None
        if target_task.task_type == TaskType.VISUAL_PLAN:
            upstream = target_task.input_data.get("upstream_task_ids") or {}
            script_id = upstream.get("script")
            script_task = (
                self.session.get(AgentTask, script_id) if isinstance(script_id, str) else None
            )
            if (
                script_task is None
                or script_task.content_id != content.id
                or script_task.task_type != TaskType.SHORT_SCRIPT
                or script_task.status != TaskStatus.COMPLETED
                or not script_task.result_data
            ):
                self._mark_recovery_blocked(
                    recovery_task,
                    reason="视觉转脚本修复缺少对应的已完成脚本，不能猜测 lineage",
                    action=plan.action,
                )
                return None
            script_upstream = script_task.input_data.get("upstream_task_ids") or {}
            research_id = script_upstream.get("research")
            research_task = (
                self.session.get(AgentTask, research_id) if isinstance(research_id, str) else None
            )
            scene_ids = (
                upstream.get("scene_analysis") or script_upstream.get("scene_analysis") or []
            )
            if isinstance(scene_ids, str):
                scene_ids = [scene_ids]
            scene_tasks = [
                self.session.get(AgentTask, task_id) if isinstance(task_id, str) else None
                for task_id in (scene_ids if isinstance(scene_ids, list) else [None])
            ]
        else:
            research_task = self.completed_task_for(content.id, TaskType.CONTENT_RESEARCH)
            scene_tasks = self.scene_analysis_tasks(content.id)
        if (
            research_task is None
            or research_task.content_id != content.id
            or research_task.task_type != TaskType.CONTENT_RESEARCH
            or research_task.status != TaskStatus.COMPLETED
            or any(
                task is None
                or task.content_id != content.id
                or task.task_type != TaskType.SCENE_ANALYZE
                or task.status != TaskStatus.COMPLETED
                for task in scene_tasks
            )
        ) or (content.production_mode != ProductionMode.IMAGE_ONLY and not scene_tasks):
            self._mark_recovery_blocked(
                recovery_task,
                reason="脚本修复缺少已完成的事实研究或场景分析，不能猜测上下文",
                action=plan.action,
            )
            return None

        research_result = research_task.result_data or {}
        allowed_claim_ids = [
            str(claim.get("id"))
            for claim in (research_result.get("verified_claims") or [])
            if isinstance(claim, dict) and claim.get("id")
        ]
        current_result = (
            script_task.result_data
            if script_task is not None
            else recovery_task.input_data.get("current_result") or {}
        )
        if not isinstance(current_result, dict):
            current_result = {}
        if not current_result:
            current_result = target_task.result_data or self._latest_submission_result(target_task)
        previous_script = {
            key: current_result[key] for key in _SCRIPT_RESULT_FIELDS if key in current_result
        }
        failure = recovery_task.input_data.get("failure") or {}
        failure_errors = list(failure.get("validation_errors") or [])
        if not failure_errors and target_task.task_type in {
            TaskType.SCRIPT_QC,
            TaskType.CONTENT_QC,
        }:
            failure_errors = list((target_task.result_data or {}).get("issues") or [])
        issue_lines = []
        for issue in failure_errors[:30]:
            if not isinstance(issue, dict):
                continue
            field = (
                issue.get("field")
                or ".".join(str(item) for item in issue.get("path", []))
                or "unknown"
            )
            issue_lines.append(
                f"- [{issue.get('severity', 'error')}] {field}: {issue.get('message', '')}"
            )
        note_parts = [
            _script_rewrite_note(plan.reviewer_note),
            "这是恢复链的新任务，不要修改原失败记录；修复后必须重新通过 script_qc。",
        ]
        if issue_lines:
            note_parts.append("服务端记录的问题：\n" + "\n".join(issue_lines))
        if plan.must_change:
            note_parts.append("必须修改：\n" + "\n".join(f"- {item}" for item in plan.must_change))
        if plan.must_preserve:
            note_parts.append(
                "必须保留：\n" + "\n".join(f"- {item}" for item in plan.must_preserve)
            )
        if plan.must_not_do:
            note_parts.append("禁止事项：\n" + "\n".join(f"- {item}" for item in plan.must_not_do))
        if plan.acceptance_checks:
            note_parts.append(
                "验收：\n" + "\n".join(f"- {item}" for item in plan.acceptance_checks)
            )
        transcript_path = Path(content.content_dir).resolve() / "transcript" / "transcript.json"
        upstream_task_ids: dict[str, str | list[str]] = {"research": research_task.id}
        if scene_tasks:
            upstream_task_ids["scene_analysis"] = [task.id for task in scene_tasks]
        input_data: dict[str, Any] = {
            "upstream_task_ids": upstream_task_ids,
            **(
                {"materialized_context": {"transcript": "transcript/transcript.json"}}
                if transcript_path.is_file()
                else {"transcript": {"segments": []}}
            ),
            "target_duration_sec": 35,
            "style": "面向中文大众，结果前置，给出明确判断",
            "reviewer_note": "\n\n".join(part for part in note_parts if part),
            "previous_script": previous_script,
            "production_mode": content.production_mode.value,
            "allowed_claim_ids": allowed_claim_ids,
            "min_video_seconds": 20,
            "max_video_seconds": 90,
            "recovery_task_id": recovery_task.id,
            "recovery_round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
            "recovery_plan": plan.model_dump(mode="json"),
            "must_change": plan.must_change,
            "must_preserve": plan.must_preserve,
            "must_not_do": plan.must_not_do,
            "acceptance_checks": plan.acceptance_checks,
            "previous_qc_task_id": (
                target_task.id
                if target_task.task_type in {TaskType.SCRIPT_QC, TaskType.CONTENT_QC}
                else failure.get("source_task_id")
            ),
            **self.script_audience_inputs(content, research_task),
        }
        self._cancel_open_content_tasks(
            content.id,
            {
                TaskType.SHORT_SCRIPT,
                TaskType.SCRIPT_QC,
                TaskType.SCRIPT_REVIEW_ASSIST,
                *_VISUAL_PLAN_DOWNSTREAM_TASK_TYPES,
            },
            reason="recovery-repair",
        )
        repair = self.tasks.create_task(
            task_type=TaskType.SHORT_SCRIPT,
            content_id=content.id,
            instructions=resolve_instructions(self.session, TaskType.SHORT_SCRIPT, base_settings=self.base_settings, opening_identity_overlay=self.opening_identity_overlay),
            input_data=input_data,
            priority=52,
        )
        content.script_version += 1
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "recovery_repair"
        content.error_code = "RECOVERY_REPAIR_STARTED"
        content.error_message = "助手已生成结构化整改要求，等待新的脚本修复任务"
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=repair.id,
                action="recovery_repair_task_created",
                metadata_json={
                    "recovery_task_id": recovery_task.id,
                    "source_task_id": failure.get("source_task_id"),
                    "target_task_type": TaskType.SHORT_SCRIPT.value,
                    "round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
                },
            )
        )
        self.session.commit()
        self.session.refresh(repair)
        return repair

    def _create_script_review_repair_from_recovery(
        self,
        recovery_task: AgentTask,
        plan: RecoveryPlan,
        *,
        target_task: AgentTask,
    ) -> AgentTask | None:
        """Rebuild a malformed manual script-review task from completed dependencies."""
        content = (
            self.session.get(Content, recovery_task.content_id)
            if recovery_task.content_id
            else None
        )
        if content is None:
            self._mark_recovery_blocked(
                recovery_task,
                reason="脚本审核修复缺少 content_id，不能安全生成子任务",
                action=plan.action,
            )
            return None
        script_task = self.completed_task_for(content.id, TaskType.SHORT_SCRIPT)
        script_qc_task = self.completed_task_for(content.id, TaskType.SCRIPT_QC)
        research_task = self.completed_task_for(content.id, TaskType.CONTENT_RESEARCH)
        if script_task is None or script_qc_task is None or research_task is None:
            self._mark_recovery_blocked(
                recovery_task,
                reason="脚本审核修复缺少已完成的脚本、脚本质检或事实研究",
                action=plan.action,
            )
            return None
        input_data = {
            "upstream_task_ids": {
                "script": script_task.id,
                "script_qc": script_qc_task.id,
                "research": research_task.id,
            },
            "recovery_task_id": recovery_task.id,
            "recovery_round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
        }
        self._cancel_open_content_tasks(
            content.id,
            {TaskType.SCRIPT_REVIEW_ASSIST},
            reason="recovery-review-repair",
        )
        repair = self.tasks.create_task(
            task_type=TaskType.SCRIPT_REVIEW_ASSIST,
            content_id=content.id,
            instructions=resolve_instructions(self.session, TaskType.SCRIPT_REVIEW_ASSIST, base_settings=self.base_settings, opening_identity_overlay=self.opening_identity_overlay),
            input_data=input_data,
            priority=50,
        )
        content.business_status = ContentStatus.SCRIPT_REVIEW
        content.current_stage = TaskType.SCRIPT_REVIEW_ASSIST.value
        content.error_code = "RECOVERY_REPAIR_STARTED"
        content.error_message = "服务端已补齐脚本审核依赖，等待新的审核助手任务"
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=repair.id,
                action="recovery_repair_task_created",
                metadata_json={
                    "recovery_task_id": recovery_task.id,
                    "source_task_id": target_task.id,
                    "target_task_type": TaskType.SCRIPT_REVIEW_ASSIST.value,
                    "round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
                },
            )
        )
        self.session.commit()
        self.session.refresh(repair)
        return repair

    def _deterministic_repair_target(
        self,
        source_task: AgentTask,
        *,
        code: str,
        failure_class: str,
        allow_workspace_rebuild: bool = False,
    ) -> str | None:
        """Return a repair target only when the server can prove the fix is safe.

        These checks deliberately cover package construction failures only.  They do not
        infer research, script, visual, rights, or publishing decisions from an Agent
        failure.  A missing authoritative mode or a missing context path whose source file
        is present can be rebuilt deterministically; everything else stays on the Recovery
        Agent/manual path.
        """
        # Exact machine codes only; mentioning a bundle is not authorization.
        codes = {code, source_task.error_code}
        context_codes = {"ENVIRONMENT_MISSING_CONTEXT", "MISSING_CONTEXT_ARTIFACT", "NO_WORKSPACE"}
        package_codes = context_codes | {"ENVIRONMENT_MISSING_INPUT", "TASK_CONTRACT_MISMATCH"}
        package_failure = failure_class in {"TASK_CONTRACT", "CONTEXT_MISSING"} and bool(
            codes & package_codes
        )
        if source_task.task_type in {TaskType.SHORT_SCRIPT, TaskType.SCRIPT_REVIEW_ASSIST}:
            content = (
                self.session.get(Content, source_task.content_id)
                if source_task.content_id
                else None
            )
            if content is None or not package_failure:
                return None
            research_task = self.completed_task_for(content.id, TaskType.CONTENT_RESEARCH)
            if source_task.task_type == TaskType.SCRIPT_REVIEW_ASSIST:
                missing_review_input = any(
                    not source_task.input_data.get(field)
                    for field in ("script", "script_qc", "research")
                )
                if not missing_review_input:
                    return None
                if research_task is None:
                    return TaskType.CONTENT_RESEARCH.value
                script_task = self.completed_task_for(content.id, TaskType.SHORT_SCRIPT)
                script_qc = self.completed_task_for(content.id, TaskType.SCRIPT_QC)
                if script_task is None or script_qc is None:
                    return TaskType.SHORT_SCRIPT.value
                # The review task itself is the malformed node.  The RecoveryPlan schema
                # routes through the short-script business role; the server then creates a
                # fresh review-assistant child from the completed upstream results.
                return TaskType.SHORT_SCRIPT.value
            mode = source_task.input_data.get("production_mode")
            if research_task is None:
                return TaskType.CONTENT_RESEARCH.value
            if (
                content.production_mode != ProductionMode.IMAGE_ONLY
                and not self.scene_analysis_tasks(content.id)
            ):
                return TaskType.SCENE_ANALYZE.value
            if mode != content.production_mode.value:
                return TaskType.SHORT_SCRIPT.value
            return None
        if source_task.task_type == TaskType.CONTENT_RESEARCH:
            content = (
                self.session.get(Content, source_task.content_id)
                if source_task.content_id
                else None
            )
            if (
                content is not None
                and failure_class in {"TASK_CONTRACT", "CONTEXT_MISSING"}
                and bool(codes & package_codes)
                and (
                    source_task.input_data.get("production_mode") != content.production_mode.value
                    or (allow_workspace_rebuild and "NO_WORKSPACE" in codes)
                )
            ):
                return TaskType.CONTENT_RESEARCH.value
            return None
        if source_task.task_type == TaskType.VISUAL_PLAN_QC:
            if failure_class != "CONTEXT_MISSING" or not codes & context_codes:
                return None
            content = (
                self.session.get(Content, source_task.content_id)
                if source_task.content_id
                else None
            )
            if content is None:
                return None
            try:
                self._visual_plan_qc_repair_input(content, source_task)
            except ValueError:
                return None
            return TaskType.VISUAL_PLAN_QC.value
        if source_task.task_type != TaskType.SCENE_ANALYZE:
            return None
        if failure_class != "CONTEXT_MISSING" or not codes & context_codes:
            return None
        content = (
            self.session.get(Content, source_task.content_id) if source_task.content_id else None
        )
        if content is None:
            return None
        try:
            self._scene_repair_input(content, source_task)
        except ValueError:
            return None
        return TaskType.SCENE_ANALYZE.value

    def repair_known_task_bundle(
        self,
        source_task: AgentTask,
        *,
        actor_id: str = "recovery-reconcile",
    ) -> AgentTask | None:
        """Reconcile a historical deterministic package failure once, without requeueing it."""
        if source_task.task_type == TaskType.DOUYIN_PUBLISH:
            recoveries = self.session.exec(
                select(AgentTask)
                .where(
                    AgentTask.task_type == TaskType.RECOVERY,
                    AgentTask.status == TaskStatus.COMPLETED,
                )
                .order_by(AgentTask.created_at.desc())
            ).all()
            recovery = next(
                (
                    row
                    for row in recoveries
                    if str((row.input_data.get("failure") or {}).get("source_task_id") or "")
                    == source_task.id
                    and int(
                        (row.input_data.get("failure") or {}).get("source_attempt")
                        if (row.input_data.get("failure") or {}).get("source_attempt") is not None
                        else -1
                    )
                    == max(0, int(source_task.attempt))
                ),
                None,
            )
            if recovery is None:
                return None
            try:
                plan = RecoveryPlan.model_validate(recovery.result_data or {})
            except ValueError:
                return None
            if plan.failure_class != "CONTEXT_MISSING" or plan.action != "rebuild_bundle":
                return None
            return recovery if self.apply_recovery_plan(recovery) is not None else None
        code = source_task.error_code or ""
        failure_class = self.classify_recovery_failure(source_task, code=code)
        if (
            self._deterministic_repair_target(
                source_task,
                code=code,
                failure_class=failure_class,
                allow_workspace_rebuild=True,
            )
            is None
        ):
            return None
        return self.create_recovery_task(
            source_task,
            failure_class=failure_class,
            code=code,
            message=source_task.error_message or "",
            actor_id=actor_id,
            force_new=True,
            historical_reconcile=True,
        )

    @staticmethod
    def _candidate_task_input(candidate: Candidate) -> dict[str, str]:
        return {
            "title": candidate.original_title,
            "source_url": candidate.source_url,
            "source_account": candidate.source_account,
        }

    @staticmethod
    def _content_file(root: Path, relative_path: str) -> tuple[str, Path] | None:
        normalized = str(relative_path or "").replace("\\", "/").strip()
        if not normalized:
            return None
        candidate = Path(normalized)
        if candidate.is_absolute() or ".." in candidate.parts:
            return None
        target = (root / candidate).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return normalized, target

    def _scene_repair_input(self, content: Content, target_task: AgentTask) -> dict[str, Any]:
        candidate = self.session.get(Candidate, content.candidate_id)
        if candidate is None:
            raise ValueError("场景修复缺少候选来源，不能安全重建任务包")
        raw_scenes = target_task.input_data.get("scenes") or []
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise ValueError("场景修复缺少原始 scenes 清单，不能猜测镜头")
        root = Path(content.content_dir).resolve()
        scenes: list[dict[str, Any]] = []
        context_files: list[str] = []
        seen_scene_ids: set[str] = set()
        for raw_scene in raw_scenes:
            if not isinstance(raw_scene, dict):
                raise ValueError("场景修复遇到无效场景记录")
            scene = dict(raw_scene)
            scene_id = str(scene.get("scene_id") or "").strip()
            thumbnail = str(scene.get("thumbnail") or "").replace("\\", "/").strip()
            if not scene_id or scene_id in seen_scene_ids or not thumbnail:
                raise ValueError("场景修复缺少唯一 scene_id 或 thumbnail")
            seen_scene_ids.add(scene_id)
            resolved = self._content_file(root, thumbnail)
            if resolved is None:
                # 兼容旧任务把 basename 写进 input 的情况；只允许回到固定 scenes/目录。
                basename = Path(thumbnail).name
                resolved = self._content_file(root, f"scenes/{basename}")
            if resolved is None:
                raise ValueError(f"场景缩略图不存在：{thumbnail}")
            canonical, _path = resolved
            scene["thumbnail"] = canonical
            scenes.append(scene)
            context_files.append(canonical)
        payload: dict[str, Any] = {
            "content_id": content.id,
            "candidate": self._candidate_task_input(candidate),
            "scenes": scenes,
            "context_files": context_files,
        }
        for key in ("batch_index", "batch_count"):
            if key in target_task.input_data:
                payload[key] = target_task.input_data[key]
        return payload

    def _visual_plan_qc_repair_input(
        self, content: Content, target_task: AgentTask
    ) -> dict[str, Any]:
        """Rebuild only the context paths for an existing visual-plan QC task."""
        raw_scenes = target_task.input_data.get("scenes") or []
        if not isinstance(raw_scenes, list) or not raw_scenes:
            raise ValueError("画面计划质检修复缺少原始 scenes 清单，不能猜测镜头")
        root = Path(content.content_dir).resolve()
        scenes: list[dict[str, Any]] = []
        context_files: list[str] = []
        seen_scene_ids: set[str] = set()
        for raw_scene in raw_scenes:
            if not isinstance(raw_scene, dict):
                raise ValueError("画面计划质检修复遇到无效场景记录")
            scene = dict(raw_scene)
            scene_id = str(scene.get("scene_id") or "").strip()
            thumbnail = str(scene.get("thumbnail") or "").replace("\\", "/").strip()
            if not scene_id or scene_id in seen_scene_ids or not thumbnail:
                raise ValueError("画面计划质检修复缺少唯一 scene_id 或 thumbnail")
            seen_scene_ids.add(scene_id)
            resolved = self._content_file(root, thumbnail)
            if resolved is None:
                resolved = self._content_file(root, f"scenes/{Path(thumbnail).name}")
            if resolved is None:
                raise ValueError(f"画面计划质检缩略图不存在：{thumbnail}")
            canonical, _path = resolved
            scene["thumbnail"] = canonical
            scenes.append(scene)
            context_files.append(canonical)
        payload = dict(target_task.input_data)
        payload["scenes"] = scenes
        payload["context_files"] = context_files
        return self._visual_qc_generation_policy(content.id, payload)

    def _publish_repair_input(self, content: Content, target_task: AgentTask) -> dict[str, Any]:
        """Rebuild a publish bundle only from the task's immutable Artifact IDs."""
        original = target_task.input_data
        if str(original.get("platform") or "").strip().lower() != "douyin":
            raise ValueError("抖音发布修复的 platform 快照无效")
        snapshot_status = str((original.get("content") or {}).get("business_status") or "").upper()
        if snapshot_status not in {ContentStatus.PACKAGED.value, ContentStatus.PUBLISHED.value}:
            raise ValueError("抖音发布快照不是已打包或已发布状态")

        root = Path(content.content_dir).resolve()

        def verify_artifact(
            artifact_id: str, *, label: str, allowed_type: set[str], mime_prefix: str
        ) -> tuple[Artifact, str]:
            artifact = self.session.get(Artifact, artifact_id)
            if (
                artifact is None
                or artifact.content_id != content.id
                or artifact.artifact_type not in allowed_type
                or not artifact.mime_type.lower().startswith(mime_prefix)
            ):
                raise ValueError(f"{label} Artifact 不属于当前内容或类型不受支持")
            resolved = self._content_file(root, artifact.relative_path)
            if resolved is None:
                raise ValueError(f"{label} Artifact 文件不存在：{artifact.relative_path}")
            canonical, path = resolved
            if path.stat().st_size != artifact.size_bytes:
                raise ValueError(f"{label} Artifact 大小与登记值不一致")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != artifact.sha256:
                raise ValueError(f"{label} Artifact SHA-256 与登记值不一致")
            return artifact, canonical

        video_id = str(original.get("video_artifact_id") or "").strip()
        if not video_id:
            raise ValueError("抖音发布缺少固定的视频 Artifact ID")
        video, video_path = verify_artifact(
            video_id,
            label="视频",
            allowed_type={"publish_video", "final_video"},
            mime_prefix="video/",
        )
        declared_sha256 = str(original.get("video_sha256") or "").strip()
        if declared_sha256 and declared_sha256 != video.sha256:
            raise ValueError("视频 SHA-256 与用户点击时的快照不一致")

        cover: Artifact | None = None
        cover_path = ""
        cover_id = str(original.get("cover_artifact_id") or "").strip()
        if cover_id:
            cover, cover_path = verify_artifact(
                cover_id,
                label="封面",
                allowed_type={"cover"},
                mime_prefix="image/",
            )
            if cover_path == video_path:
                raise ValueError("视频和封面不能引用同一个文件")

        payload = dict(original)
        payload["video_artifact_id"] = video.id
        payload["video_sha256"] = video.sha256
        payload["cover_artifact_id"] = cover.id if cover is not None else ""
        payload["context_files"] = [video_path, *([cover_path] if cover_path else [])]
        return payload

    def confirm_publish_not_published(
        self, content_id: str, *, actor_id: str = "admin-console"
    ) -> Content:
        """Restore a safe publish retry only after an explicit human confirmation."""
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status in {ContentStatus.PACKAGED, ContentStatus.PUBLISHED}:
            return content
        timeout_state = (
            content.business_status == ContentStatus.WAITING_AGENT
            and content.current_stage == "recovery"
            and content.error_code == "PUBLISH_UI_TIMEOUT"
        ) or (
            content.business_status == ContentStatus.FAILED
            and content.current_stage == "recovery_blocked"
            and content.error_code == "RECOVERY_BLOCKED"
        )
        handsfree_needs_user_state = (
            content.business_status == ContentStatus.ARCHIVED
            and content.current_stage == "archived"
            and content.error_code == "HANDSFREE_SKIPPED"
        )
        if not timeout_state and not handsfree_needs_user_state:
            raise ValueError("当前内容不是可确认重试的抖音发布超时状态")

        published_record = self.session.exec(
            select(PublicationRecord).where(
                PublicationRecord.content_id == content_id,
                PublicationRecord.platform == "douyin",
                PublicationRecord.status == "PUBLISHED",
            )
        ).first()
        if published_record is not None:
            raise ValueError("抖音已有已发布记录，不能重复发布")

        publish_tasks = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.DOUYIN_PUBLISH,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        ).all()
        if any(
            str((task.result_data or {}).get("status") or "").upper() == "PUBLISHED"
            for task in publish_tasks
        ):
            raise ValueError("已有抖音发布任务返回 PUBLISHED，不能重复发布")

        if handsfree_needs_user_state:
            target_task = publish_tasks[0] if publish_tasks else None
            if (
                target_task is None
                or target_task.status != TaskStatus.COMPLETED
                or str((target_task.result_data or {}).get("status") or "").upper() != "NEEDS_USER"
            ):
                raise ValueError("找不到最近一次无人值守抖音登录阻塞任务")
            recovery_source = "handsfree_archived_needs_user"
        else:
            target_task = self.session.exec(
                select(AgentTask)
                .where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == TaskType.DOUYIN_PUBLISH,
                    AgentTask.status == TaskStatus.FAILED,
                    AgentTask.error_code == "PUBLISH_UI_TIMEOUT",
                )
                .order_by(AgentTask.created_at.desc())
            ).first()
            if target_task is None:
                raise ValueError("找不到可安全确认的抖音发布超时任务")
            recovery_source = "publish_ui_timeout"
        snapshot_status = str(
            (target_task.input_data.get("content") or {}).get("business_status") or ""
        ).upper()
        if snapshot_status != ContentStatus.PACKAGED.value:
            raise ValueError("抖音发布任务快照不是待发布状态")

        self._publish_repair_input(content, target_task)
        content.business_status = ContentStatus.PACKAGED
        content.current_stage = "publish_pending"
        content.error_code = None
        content.error_message = None
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="publish_retry_confirmed_not_published",
                metadata_json={
                    "source_task_id": target_task.id,
                    "platform": "douyin",
                    "reason": "human_confirmed_no_external_publish",
                    "recovery_source": recovery_source,
                    "video_artifact_id": target_task.input_data.get("video_artifact_id", ""),
                    "cover_artifact_id": target_task.input_data.get("cover_artifact_id", ""),
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        return content

    def _create_publish_repair_from_recovery(
        self,
        recovery_task: AgentTask,
        plan: RecoveryPlan,
        *,
        target_task: AgentTask,
    ) -> AgentTask | None:
        content = (
            self.session.get(Content, recovery_task.content_id)
            if recovery_task.content_id
            else None
        )
        if (
            content is None
            or target_task.task_type != TaskType.DOUYIN_PUBLISH
            or target_task.status
            not in {TaskStatus.FAILED, TaskStatus.REJECTED, TaskStatus.CANCELLED}
        ):
            self._mark_recovery_blocked(
                recovery_task,
                reason="抖音发布任务包修复的内容或目标状态不一致，不能安全重建",
                action=plan.action,
            )
            return None
        try:
            input_data = self._publish_repair_input(content, target_task)
        except ValueError as exc:
            self._mark_recovery_blocked(recovery_task, reason=str(exc), action=plan.action)
            return None

        input_data["retry_of"] = target_task.id
        input_data["recovery_task_id"] = recovery_task.id
        input_data["recovery_round"] = (recovery_task.input_data.get("lineage") or {}).get(
            "round", 0
        )
        repair = self.tasks.create_task(
            task_type=TaskType.DOUYIN_PUBLISH,
            content_id=content.id,
            instructions=resolve_instructions(self.session, TaskType.DOUYIN_PUBLISH, base_settings=self.base_settings),
            input_data=input_data,
            priority=70,
        )
        repair.max_attempts = max(1, int(target_task.max_attempts or 2))
        self.session.add(repair)
        if content.business_status == ContentStatus.WAITING_AGENT or (
            content.business_status == ContentStatus.FAILED
            and content.current_stage == "recovery_blocked"
            and content.error_code == "RECOVERY_BLOCKED"
        ):
            content.business_status = ContentStatus(input_data["content"]["business_status"])
            content.current_stage = "publish_pending"
            content.error_code = None
            content.error_message = None
            content.updated_at = utc_now()
            self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=repair.id,
                action="recovery_publish_bundle_rebuilt",
                metadata_json={
                    "recovery_task_id": recovery_task.id,
                    "source_task_id": target_task.id,
                    "target_task_type": TaskType.DOUYIN_PUBLISH.value,
                    "video_artifact_id": input_data["video_artifact_id"],
                    "cover_artifact_id": input_data["cover_artifact_id"],
                    "round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
                },
            )
        )
        self.session.commit()
        self.session.refresh(repair)
        return repair

    def _research_repair_input(self, content: Content, target_task: AgentTask) -> dict[str, Any]:
        from glance_core.runtime_config import effective_settings

        candidate = self.session.get(Candidate, content.candidate_id)
        if candidate is None:
            raise ValueError("研究修复缺少候选来源，不能安全重建任务包")
        transcript = target_task.input_data.get("transcript")
        if not isinstance(transcript, dict):
            transcript_path = Path(content.content_dir).resolve() / "transcript" / "transcript.json"
            try:
                transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                transcript = {"segments": []}
        if not isinstance(transcript, dict):
            transcript = {"segments": []}
        return {
            "content_id": content.id,
            "candidate": self._candidate_task_input(candidate),
            "transcript": transcript,
            "production_mode": content.production_mode.value,
            **(
                self.image_generation_inputs(effective_settings(self.session, self.base_settings or Settings()))
                if content.production_mode == ProductionMode.IMAGE_ONLY
                else {}
            ),
        }

    def _create_context_repair_from_recovery(
        self,
        recovery_task: AgentTask,
        plan: RecoveryPlan,
        *,
        target_task: AgentTask,
    ) -> AgentTask | None:
        content = (
            self.session.get(Content, recovery_task.content_id)
            if recovery_task.content_id
            else None
        )
        target_type = plan.target_task_type or target_task.task_type.value
        if content is None or target_type not in {
            TaskType.CONTENT_RESEARCH.value,
            TaskType.SCENE_ANALYZE.value,
            TaskType.VISUAL_PLAN_QC.value,
        }:
            self._mark_recovery_blocked(
                recovery_task,
                reason="任务包修复的内容或目标类型不一致，不能猜测 lineage",
                action=plan.action,
            )
            return None
        try:
            repair_type = TaskType(target_type)
            if repair_type == TaskType.CONTENT_RESEARCH:
                input_data = self._research_repair_input(content, target_task)
            elif repair_type == TaskType.SCENE_ANALYZE:
                input_data = self._scene_repair_input(content, target_task)
            elif repair_type == TaskType.VISUAL_PLAN_QC:
                input_data = self._visual_plan_qc_repair_input(content, target_task)
            else:
                self._mark_recovery_blocked(
                    recovery_task,
                    reason="当前自动任务包修复只支持事实研究、场景分析和视觉质检",
                    action=plan.action,
                )
                return None
        except ValueError as exc:
            self._mark_recovery_blocked(recovery_task, reason=str(exc), action=plan.action)
            return None
        input_data["recovery_task_id"] = recovery_task.id
        input_data["recovery_round"] = (recovery_task.input_data.get("lineage") or {}).get(
            "round", 0
        )
        if repair_type == TaskType.CONTENT_RESEARCH and plan.failure_class in {
            "BUSINESS_QC",
            "RIGHTS_OR_PUBLISHING",
        }:
            from glance_core.content_acceptance import (
                _owned_visual_chain, candidate_source_context, source_context, visual_review_source_context,
            )

            candidate = self.session.get(Candidate, content.candidate_id)
            if candidate:
                context = candidate_source_context(self.session, candidate)
                refs = {
                    key: value
                    for key, value in (
                        target_task.input_data.get("upstream_task_ids") or {}
                    ).items()
                    if key in {"script", "visual_plan", "research"}
                }
                if target_task.task_type == TaskType.SHORT_SCRIPT:
                    refs["script"] = target_task.id
                elif target_task.task_type == TaskType.VISUAL_PLAN:
                    refs["visual_plan"] = target_task.id
                visual = (
                    self.session.get(AgentTask, refs["visual_plan"])
                    if isinstance(refs.get("visual_plan"), str)
                    else None
                )
                valid_visual = (
                    visual is not None
                    and visual.content_id == content.id
                    and visual.task_type == TaskType.VISUAL_PLAN
                    and visual.status == TaskStatus.COMPLETED
                    and stable_hash(visual.input_data) == visual.input_hash
                )
                if valid_visual:
                    refs.setdefault(
                        "script", visual.input_data.get("upstream_task_ids", {}).get("script")
                    )
                pre_review = (valid_visual and content.production_mode == ProductionMode.IMAGE_ONLY
                              and _owned_visual_chain(self.session, visual))
                if pre_review:
                    try:
                        context.update(visual_review_source_context(
                            self.session, content.id, refs,
                            review_task=target_task if target_task.task_type == TaskType.VISUAL_PLAN_QC else None,
                            include_references=True,
                        ))
                    except ValueError as exc:
                        self._mark_recovery_blocked(recovery_task, reason=str(exc), action=plan.action)
                        return None
                else:
                    context.update(source_context(self.session, content.id, refs, include_references=True))
                script = (
                    self.session.get(AgentTask, refs["script"])
                    if isinstance(refs.get("script"), str)
                    else None
                )
                if (
                    valid_visual
                    and script is not None
                    and script.content_id == content.id
                    and script.task_type == TaskType.SHORT_SCRIPT
                    and script.status == TaskStatus.COMPLETED
                    and stable_hash(script.input_data) == script.input_hash
                    and visual.input_data.get("upstream_task_ids", {}).get("script") == script.id
                ):
                    context["visual_plan_task_id"] = visual.id
                    context["visual_plan_input_sha256"] = visual.input_hash
                    context["visual_plan_result_sha256"] = stable_hash(visual.result_data)
                    context["script_task_id"] = script.id
                    context["script_input_sha256"] = script.input_hash
                    context["script_result_sha256"] = stable_hash(script.result_data)
                    if not pre_review:
                        assets = {a["artifact_id"] for a in context["artifacts"]}
                        context["asset_uses"] = [
                            {
                                "segment_index": index,
                                **{
                                    key: segment[key]
                                    for key in (
                                        "asset_id",
                                        "layout",
                                        "purpose",
                                        "narration_start_sec",
                                        "narration_end_sec",
                                    )
                                    if key in segment
                                },
                            }
                            for index, segment in enumerate(visual.result_data.get("segments", []))
                            if isinstance(segment, dict) and segment.get("asset_id") in assets
                        ]
                        for use in context["reference_uses"]:
                            segment = visual.result_data["segments"][use["segment_index"]]
                            use.update(
                                {
                                    key: segment[key]
                                    for key in (
                                        "layout",
                                        "purpose",
                                        "narration_start_sec",
                                        "narration_end_sec",
                                    )
                                    if key in segment
                                }
                            )
                input_data["source_context_read_only"] = context
                input_data["candidate"]["source_url"] = context["source_url"]
            input_data["recovery_plan"] = plan.model_dump(mode="json")
        repair = self.tasks.create_task(
            task_type=repair_type,
            content_id=content.id,
            instructions=resolve_instructions(self.session, repair_type, base_settings=self.base_settings, opening_identity_overlay=self.opening_identity_overlay),
            input_data=input_data,
            priority=52,
        )
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = repair_type.value
        content.error_code = "RECOVERY_REPAIR_STARTED"
        content.error_message = "服务端已重建任务包，等待新的 Agent 任务执行"
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=repair.id,
                action="recovery_repair_task_created",
                metadata_json={
                    "recovery_task_id": recovery_task.id,
                    "source_task_id": target_task.id,
                    "target_task_type": repair_type.value,
                    "round": (recovery_task.input_data.get("lineage") or {}).get("round", 0),
                },
            )
        )
        self.session.commit()
        self.session.refresh(repair)
        return repair

    def _visual_repair_generated_assets(
        self, previous: AgentTask, failure: dict
    ) -> tuple[list, list, set]:
        from glance_core.content_acceptance import completed_image_origin, generated_image_material

        content = self.session.get(Content, previous.content_id)
        excluded = set(previous.input_data.get("excluded_generated_asset_ids", []))
        if (
            content is None
            or content.production_mode != ProductionMode.IMAGE_ONLY
            or previous.input_data.get("production_mode") != ProductionMode.IMAGE_ONLY.value
        ):
            return [], ["reuse: explicit original image-only route required"], excluded
        segments = previous.result_data.get("segments", [])
        indexes = _failed_segment_indexes(failure.get("validation_errors", []), len(segments))
        if indexes is None:
            return [], ["reuse: failed segment has no exact index"], excluded
        plans = []
        cursor = previous
        for _ in range(self.recovery_max_rounds + 1):
            if (
                cursor.id in {p.id for p in plans}
                or cursor.content_id != previous.content_id
                or cursor.task_type != TaskType.VISUAL_PLAN
                or not completed_image_origin(self.session, cursor, bind_result=False)
                or stable_hash(cursor.input_data) != cursor.input_hash
            ):
                return [], ["reuse: previous plan lineage unavailable/ambiguous"], excluded
            plans.append(cursor)
            parent_id = cursor.input_data.get("previous_plan_task_id")
            if not parent_id:
                break
            cursor = self.session.get(AgentTask, parent_id)
            if cursor is None:
                return [], ["reuse: previous plan missing"], excluded
        else:
            return [], ["reuse: previous plan chain exceeds repair budget"], excluded
        candidates, gaps = {}, []
        generations = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content.id,
                AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
            )
        ).all()
        asset_checks = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content.id,
                AgentTask.task_type == TaskType.ASSET_QC,
            )
        ).all()
        for origin in plans:
            for generation in generations:
                if generation.input_data.get("upstream_task_ids") != {"visual_plan": origin.id}:
                    continue
                checks = [
                    q
                    for q in asset_checks
                    if q.input_data.get("upstream_task_ids")
                    == {
                        "visual_plan": origin.id,
                        "asset_generate_image": generation.id,
                    }
                ]
                if len(checks) != 1 or not completed_image_origin(self.session, checks[0]):
                    gaps.append("reuse: exact asset QC missing/ambiguous")
                    continue
                qc = checks[0]
                if stable_hash(qc.input_data) != qc.input_hash:
                    gaps.append("reuse: asset QC input changed")
                    continue
                bad_indexes = _failed_segment_indexes(
                    qc.result_data.get("issues", []), len(origin.result_data.get("segments", []))
                )
                if bad_indexes is None or (
                    qc.result_data.get("passed") is not True and not bad_indexes
                ):
                    gaps.append("reuse: failed asset QC has no exact segment evidence")
                    continue
                if not bad_indexes.issubset(
                    {a.get("segment_index") for a in qc.input_data.get("assets", [])}
                ):
                    gaps.append("reuse: failed asset segment has no exact material")
                    continue
                for entry in qc.input_data.get("assets", []):
                    if entry.get("segment_index") in bad_indexes:
                        excluded.add(entry.get("artifact_id"))
                        continue
                    try:
                        material = generated_image_material(
                            self.session, content, entry["artifact_id"]
                        )
                    except (KeyError, ValueError):
                        gaps.append("reuse: asset origin/bytes unavailable")
                        continue
                    if (
                        material["generation_task_id"] != generation.id
                        or material["generation_segment_index"] != entry.get("segment_index")
                        or material["request"] != entry.get("request")
                        or material["generation"] != entry.get("generation")
                    ):
                        gaps.append("reuse: QC generation/request mismatch")
                        continue
                    candidates[material["artifact_id"]] = material
        for index in indexes:
            excluded.add(segments[index].get("asset_id"))
        return (
            [m for key, m in candidates.items() if key not in excluded],
            list(dict.fromkeys(gaps)),
            excluded,
        )

    def visual_repair_input(
        self, previous: AgentTask, recovery_task: AgentTask, plan: RecoveryPlan, *, source: AgentTask | None
    ) -> dict:
        """Build a new context from caller-verified frozen origins without mutating history."""
        content = self.session.get(Content, previous.content_id)
        if content is None:
            raise ValueError("视觉修复内容不存在")
        failure = recovery_task.input_data.get("failure") or {}
        qc_source = source is not None and source.task_type in {
            TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC, TaskType.CONTENT_QC,
        }
        if qc_source and (
            source.content_id != content.id
            or failure.get("source_task_type") != source.task_type.value
            or source.result_data.get("passed") is not False
            or not isinstance(failure.get("validation_errors"), list)
            or any(not isinstance(issue, dict) for issue in failure["validation_errors"])
            or failure["validation_errors"] != source.result_data.get("issues")
        ):
            raise ValueError("视觉修复质检来源或问题证据不一致")
        input_data = deepcopy(previous.input_data)
        input_data.update(
            previous_plan=deepcopy(previous.result_data),
            previous_plan_task_id=previous.id,
            recovery_task_id=recovery_task.id,
            rework_feedback=plan.model_dump(mode="json"),
        )
        reusable, reuse_gaps, excluded = self._visual_repair_generated_assets(previous, failure)
        input_data["reusable_generated_assets"] = reusable
        input_data["original_reuse"] = bool(reusable)
        input_data["reusable_assets_evidence_gaps"] = reuse_gaps
        input_data.pop("previous_qc_task_id", None)
        input_data.pop("qc_issues", None)
        if qc_source:
            input_data["previous_qc_task_id"] = source.id
            input_data["qc_issues"] = deepcopy(failure.get("validation_errors", []))
        indexes = (
            _failed_segment_indexes(
                failure.get("validation_errors", []), len(previous.result_data.get("segments", []))
            )
            or set()
        )
        if source and source.content_id == content.id and source.task_type == TaskType.ASSET_QC:
            excluded.update(
                a.get("artifact_id")
                for a in source.input_data.get("assets", [])
                if a.get("segment_index") in indexes
            )
        excluded.update(
            s.get("asset_id")
            for index, s in enumerate(previous.result_data.get("segments", []))
            if index in indexes
        )
        input_data["excluded_generated_asset_ids"] = sorted(
            a for a in excluded if isinstance(a, str)
        )
        input_data["allowed_asset_ids"] = list(
            dict.fromkeys(
                [
                    *[
                        a
                        for a in input_data.get("allowed_asset_ids", [])
                        if (artifact := self.session.get(Artifact, a)) is not None
                        and artifact.content_id == content.id
                        and artifact.artifact_type != "agent_generated_image"
                    ],
                    *[m["artifact_id"] for m in reusable],
                ]
            )
        )
        input_data["context_files"] = list(
            dict.fromkeys(
                [
                    *input_data.get("context_files", []),
                    *[self.session.get(Artifact, m["artifact_id"]).relative_path for m in reusable],
                ]
            )
        )
        duration_evidence = next(
            (
                item.get("evidence")
                for item in (failure.get("validation_errors") or [])
                if isinstance(item, dict) and isinstance(item.get("evidence"), dict)
            ),
            None,
        )
        if duration_evidence is not None:
            input_data["duration_evidence"] = duration_evidence
        return input_data

    def _create_visual_repair_from_recovery(
        self, recovery_task: AgentTask, plan: RecoveryPlan, *, target_task: AgentTask
    ) -> AgentTask | None:
        content = self.session.get(Content, recovery_task.content_id)
        previous = self.completed_task_for(recovery_task.content_id, TaskType.VISUAL_PLAN)
        existing = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == recovery_task.content_id,
                AgentTask.task_type == TaskType.VISUAL_PLAN,
            )
        ).all()
        children = [task for task in existing if task.input_data.get("recovery_task_id") == recovery_task.id]
        if children:
            superseded = {
                task.input_data["supersedes_task_id"] for task in children
                if task.input_data.get("visual_repair_context_version") == 1
                and task.input_data.get("supersedes_task_id") in {child.id for child in children}
            }
            current = [task for task in children if task.id not in superseded]
            if len(current) != 1 or len(superseded) != len(children) - 1:
                raise TaskConflictError("RECOVERY_VISUAL_REPAIR_REVISION_AMBIGUOUS")
            return current[0]
        if (
            content is None
            or content.business_status != ContentStatus.WAITING_AGENT
            or content.current_stage != "recovery"
            or previous is None
            or previous.id != target_task.id
            or not previous.result_data
            or not (previous.input_data or {}).get("upstream_task_ids")
            or not (previous.input_data or {}).get("production_mode")
        ):
            self._mark_recovery_blocked(
                recovery_task, reason="视觉修复目标已过期或缺少完整上下文", action=plan.action
            )
            return None
        failure = recovery_task.input_data.get("failure") or {}
        source = self.session.get(AgentTask, failure.get("source_task_id", ""))
        try:
            if source is not None and source.task_type in {
                TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC, TaskType.CONTENT_QC,
            } and source.status != TaskStatus.COMPLETED:
                raise ValueError("视觉修复质检来源未完成")
            input_data = self.visual_repair_input(previous, recovery_task, plan, source=source)
        except ValueError as exc:
            self._mark_recovery_blocked(recovery_task, reason=str(exc), action=plan.action)
            return None
        self.invalidate_visual_plan(
            content.id, include_review_tasks=True, reason="recovery-visual-repair"
        )
        repair = self.create_agent_task(
            content.id, TaskType.VISUAL_PLAN, input_data, priority=52, allow_multiple=True
        )
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "visual_plan"
        content.error_code = "RECOVERY_REPAIR_STARTED"
        content.error_message = "恢复 Agent 已签发视觉修复计划，等待重做并重新质检"
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.commit()
        return repair

    def apply_recovery_plan(self, recovery_task: AgentTask) -> AgentTask | None:
        """Execute a validated RecoveryPlan through the server action whitelist."""
        try:
            plan = RecoveryPlan.model_validate(recovery_task.result_data or {})
        except ValueError as exc:
            self._mark_recovery_blocked(
                recovery_task, reason=f"恢复计划无效：{exc}", action="mark_recovery_blocked"
            )
            return None
        failure = recovery_task.input_data.get("failure") or {}
        if plan.failure_class != failure.get("failure_class"):
            self._mark_recovery_blocked(
                recovery_task,
                reason="恢复计划的 failure_class 与服务端 FailureEnvelope 不一致",
                action="mark_recovery_blocked",
            )
            return None
        source_task = self.session.get(AgentTask, str(failure.get("source_task_id") or ""))
        if plan.action == "reroute_source_discovery":
            if (
                source_task is None
                or source_task.task_type != TaskType.SOURCE_DISCOVERY
                or plan.failure_class != "TRANSIENT_EXECUTOR"
                or not self._source_discovery_reroute_allowed(source_task, failure)
            ):
                self._mark_recovery_blocked(
                    recovery_task,
                    reason=(
                        "来源发现重路由只允许无候选副作用的 ENVIRONMENT_UNREACHABLE/WinError 10061"
                    ),
                    action=plan.action,
                )
                return None
            return self._reroute_source_discovery(recovery_task, source_task)
        target_id = str(
            (recovery_task.input_data.get("lineage") or {}).get("repair_target_task_id")
            or failure.get("source_task_id")
            or ""
        )
        target_task = self.session.get(AgentTask, target_id)
        if source_task is None or target_task is None:
            self._mark_recovery_blocked(
                recovery_task,
                reason="恢复链父任务或修复目标不存在，不能猜测 lineage",
                action=plan.action,
            )
            return None
        if plan.action == "rebuild_bundle":
            if plan.failure_class != "CONTEXT_MISSING":
                self._mark_recovery_blocked(
                    recovery_task,
                    reason="发布任务包重建只允许处理上下文文件缺失",
                    action=plan.action,
                )
                return None
            return self._create_publish_repair_from_recovery(
                recovery_task, plan, target_task=target_task
            )
        if plan.action == "retry_same_task":
            blocked = self.session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "agent_task",
                    AuditEvent.entity_id == recovery_task.id,
                    AuditEvent.actor_id == "recovery-coordinator",
                    AuditEvent.action == "recovery_escalated",
                )
            ).all()
            if any(e.metadata_json.get("action") == plan.action for e in blocked):
                return None
            retry_events = self.session.exec(
                select(AuditEvent).where(
                    AuditEvent.entity_type == "agent_task",
                    AuditEvent.action == "recovery_same_task_retried",
                )
            ).all()
            already_retried = next(
                (
                    event
                    for event in retry_events
                    if (event.metadata_json or {}).get("recovery_task_id") == recovery_task.id
                ),
                None,
            )
            if already_retried is not None:
                return target_task
            if plan.failure_class != "TRANSIENT_EXECUTOR" or target_task.status not in {
                TaskStatus.FAILED,
                TaskStatus.REJECTED,
                TaskStatus.CANCELLED,
            }:
                self._mark_recovery_blocked(
                    recovery_task, reason="只有暂态执行器失败允许重试原任务", action=plan.action
                )
                return None
            retries = int(
                (recovery_task.input_data.get("lineage") or {}).get("same_task_retries", 0)
            )
            if retries >= self.recovery_same_task_retries:
                self._mark_recovery_blocked(
                    recovery_task, reason="同任务暂态重试预算已耗尽", action=plan.action
                )
                return None
            if (
                source_task.content_id != recovery_task.content_id
                or target_task.content_id != recovery_task.content_id
            ):
                self._mark_recovery_blocked(
                    recovery_task, reason="重试目标不属于同一恢复内容", action=plan.action
                )
                return None
            try:
                retry = self.tasks.requeue(
                    target_task.id, actor_id="recovery-coordinator", allow_attempt_extension=False
                )
            except TaskConflictError as exc:
                self._mark_recovery_blocked(recovery_task, reason=str(exc), action=plan.action)
                return None
            self.session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="recovery-coordinator",
                    entity_type="agent_task",
                    entity_id=retry.id,
                    action="recovery_same_task_retried",
                    metadata_json={"recovery_task_id": recovery_task.id},
                )
            )
            self.session.commit()
            return retry
        if plan.action in {"create_repair_task", "repair_task_context", "reroute_to_role"}:
            from glance_core.content_acceptance import is_handsfree_content

            target_type = plan.target_task_type or target_task.task_type.value
            content = (
                self.session.get(Content, recovery_task.content_id)
                if recovery_task.content_id
                else None
            )
            rights_research = (
                target_type == TaskType.CONTENT_RESEARCH.value
                and source_task.content_id == recovery_task.content_id == target_task.content_id
                and failure.get("source_task_type") == source_task.task_type.value
                and target_task.task_type
                in {
                    TaskType.SHORT_SCRIPT,
                    TaskType.SCRIPT_QC,
                    TaskType.SCRIPT_REVIEW_ASSIST,
                    TaskType.VISUAL_PLAN,
                    TaskType.VISUAL_PLAN_QC,
                    TaskType.CONTENT_QC,
                }
                and content is not None
                and content.business_status not in {ContentStatus.ARCHIVED, ContentStatus.PUBLISHED}
                and stable_hash(source_task.input_data) == source_task.input_hash
                and self._repair_fingerprint(
                    source_task,
                    failure_class=failure.get("failure_class"),
                    code=failure.get("code", ""),
                )
                == (recovery_task.input_data.get("lineage") or {}).get("repair_fingerprint")
                and is_handsfree_content(self.session, content.id)
                and (
                    (
                        plan.failure_class == "BUSINESS_QC"
                        and source_task.task_type
                        in {TaskType.SCRIPT_QC, TaskType.VISUAL_PLAN_QC, TaskType.CONTENT_QC}
                        and source_task.status == TaskStatus.COMPLETED
                        and (source_task.result_data or {}).get("passed") is False
                        and any(
                            isinstance(issue, dict)
                            and issue.get("field")
                            in {
                                "rights",
                                "source_context_read_only.registration.rights_status",
                                "source_context_read_only.registration.rights_note",
                                "source_context.registration.rights_status",
                                "source_context.registration.rights_note",
                            }
                            and issue.get("severity") == "error"
                            for issue in (source_task.result_data or {}).get("issues", [])
                        )
                    )
                    or (
                        plan.failure_class == "RIGHTS_OR_PUBLISHING"
                        and source_task.task_type
                        in {
                            TaskType.SHORT_SCRIPT,
                            TaskType.SCRIPT_QC,
                            TaskType.SCRIPT_REVIEW_ASSIST,
                            TaskType.VISUAL_PLAN,
                            TaskType.VISUAL_PLAN_QC,
                        }
                        and source_task.status == TaskStatus.FAILED
                        and source_task.error_code
                        == failure.get("code")
                        == "RIGHTS_EVIDENCE_MISSING"
                    )
                )
            )
            allowed_repair_types = {
                TaskType.VISUAL_PLAN.value,
                TaskType.SHORT_SCRIPT.value,
                TaskType.CONTENT_RESEARCH.value,
                TaskType.SCENE_ANALYZE.value,
                TaskType.VISUAL_PLAN_QC.value,
            }
            if target_type not in allowed_repair_types:
                self._mark_recovery_blocked(
                    recovery_task,
                    reason="当前版本只允许服务端为脚本、事实研究或场景任务生成修复子任务",
                    action=plan.action,
                )
                return None
            if target_type in {
                TaskType.CONTENT_RESEARCH.value,
                TaskType.SCENE_ANALYZE.value,
            } and plan.action not in {"repair_task_context", "reroute_to_role"}:
                self._mark_recovery_blocked(
                    recovery_task,
                    reason="事实研究和场景任务只能使用服务端任务包修复动作",
                    action=plan.action,
                )
                return None
            if plan.action == "create_repair_task" and plan.failure_class != "BUSINESS_QC":
                self._mark_recovery_blocked(
                    recovery_task, reason="业务质检修复动作与失败分类不匹配", action=plan.action
                )
                return None
            if target_type == TaskType.VISUAL_PLAN.value:
                if plan.action != "create_repair_task" or plan.failure_class != "BUSINESS_QC":
                    self._mark_recovery_blocked(
                        recovery_task, reason="视觉返工只接受业务质检修复计划", action=plan.action
                    )
                    return None
                return self._create_visual_repair_from_recovery(
                    recovery_task, plan, target_task=target_task
                )
            workspace_failure = "WORKSPACE" in str(failure.get("code") or "").upper()
            if (
                plan.action == "repair_task_context"
                and plan.failure_class
                not in {
                    "TASK_CONTRACT",
                    "CONTEXT_MISSING",
                }
                and not (plan.failure_class == "UNKNOWN" and workspace_failure)
            ):
                self._mark_recovery_blocked(
                    recovery_task, reason="上下文修复动作与失败分类不匹配", action=plan.action
                )
                return None
            if plan.action == "reroute_to_role" and not (
                plan.failure_class
                in {
                    "TASK_CONTRACT",
                    "CONTEXT_MISSING",
                    "UNKNOWN",
                }
                or (
                    plan.failure_class == "BUSINESS_QC"
                    and target_type == TaskType.SHORT_SCRIPT.value
                )
                or rights_research
            ):
                self._mark_recovery_blocked(
                    recovery_task, reason="角色路由动作与失败分类不匹配", action=plan.action
                )
                return None
            if target_type in {
                TaskType.CONTENT_RESEARCH.value,
                TaskType.SCENE_ANALYZE.value,
                TaskType.VISUAL_PLAN_QC.value,
            }:
                return self._create_context_repair_from_recovery(
                    recovery_task, plan, target_task=target_task
                )
            return self._create_script_repair_from_recovery(
                recovery_task, plan, target_task=target_task
            )
        self._mark_recovery_blocked(
            recovery_task,
            reason=(
                "恢复动作已记录，服务端不会自动执行该高风险或未实现动作；"
                "请按 Recovery Center 提示人工处理"
            ),
            action=plan.action,
        )
        return None

    def _source_discovery_reroute_allowed(
        self, source_task: AgentTask, failure: dict[str, Any]
    ) -> bool:
        code = str(failure.get("code") or source_task.error_code or "").upper()
        message = str(failure.get("message") or source_task.error_message or "").upper()
        if code != "ENVIRONMENT_UNREACHABLE" and "WINERROR 10061" not in message:
            return False
        if source_task.result_data and source_task.result_data.get("candidates"):
            return False
        source_id = str((source_task.input_data or {}).get("source_id") or "")
        run_id = str((source_task.input_data or {}).get("collection_run_id") or "")
        if not source_id or not run_id:
            return False
        run = self.session.get(CollectionRun, run_id)
        return bool(
            run
            and run.source_id == source_id
            and run.discovered_count == 0
            and run.created_count == 0
        )

    def _reroute_source_discovery(
        self, recovery_task: AgentTask, source_task: AgentTask
    ) -> AgentTask | None:
        data = source_task.input_data or {}
        source = self.session.get(Source, str(data.get("source_id") or ""))
        if source is None or not source.enabled:
            self._mark_recovery_blocked(
                recovery_task,
                reason="来源不存在或已停用，必须人工处理",
                action="reroute_source_discovery",
            )
            return None
        direction_key = direction_key_for(
            source_id=source.id, name=source.name, config=source.config_json or {}
        )
        existing = self.session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
                AgentTask.lane_key == direction_key,
            )
        ).all()
        for row in existing:
            if str((row.input_data or {}).get("recovery_task_id") or "") == recovery_task.id:
                return row
        active = next(
            (
                row
                for row in existing
                if row.status
                in {
                    TaskStatus.PENDING,
                    TaskStatus.LEASED,
                    TaskStatus.SUBMITTED,
                }
            ),
            None,
        )
        if active is not None:
            return active
        try:
            reroute = self.tasks.create_task(
                task_type=TaskType.SOURCE_DISCOVERY,
                instructions=resolve_instructions(self.session, TaskType.SOURCE_DISCOVERY, base_settings=self.base_settings),
                input_data={
                    **data,
                    "direction_key": direction_key,
                    "history": research_history(self.session, direction_key=direction_key),
                    "recovery_task_id": recovery_task.id,
                },
                priority=20,
                lane_key=direction_key,
            )
        except TaskLaneBusyError as exc:
            reroute = self.session.get(AgentTask, exc.task_id)
            if reroute is None:
                raise
        now = utc_now()
        cooldown_until = min(as_utc(source.next_poll_at), now + SOURCE_RECOVERY_MAX_COOLDOWN)
        if cooldown_until > now:
            reroute.not_before = cooldown_until
            self.session.add(reroute)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="recovery-coordinator",
                entity_type="agent_task",
                entity_id=reroute.id,
                action="recovery_source_discovery_rerouted",
                metadata_json={
                    "recovery_task_id": recovery_task.id,
                    "source_task_id": source_task.id,
                },
            )
        )
        self.session.commit()
        return reroute

    def result_for(self, content_id: str, task_type: TaskType) -> dict | None:
        task = self.completed_task_for(content_id, task_type)
        return task.result_data if task else None

    def latest_failed_content_qc(self, content_id: str) -> AgentTask | None:
        """Return the latest completed CONTENT_QC task whose result did not pass."""
        tasks = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.CONTENT_QC,
                AgentTask.status == TaskStatus.COMPLETED,
            )
            .order_by(AgentTask.completed_at.desc())
        ).all()
        return next(
            (task for task in tasks if not (task.result_data or {}).get("passed")),
            None,
        )

    def completed_task_for(self, content_id: str, task_type: TaskType) -> AgentTask | None:
        """Return the newest completed task in the content's immutable task lineage.

        A superseded task can finish after its replacement. Ordering only by completion
        time would then make the late result look current and feed stale inputs into the
        next stage; creation time identifies the replacement lineage, with completion time
        as the tie-breaker for retries of the same creation window.
        """
        return self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == task_type,
                AgentTask.status == TaskStatus.COMPLETED,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.completed_at.desc())
        ).first()

    def scene_analysis_tasks(self, content_id: str) -> list[AgentTask]:
        """按批号排序的已完成场景分析任务；未分批时列表里只有一条。

        同一批号有多次完成（重试/重建）时取最新那一条。
        """
        tasks = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.SCENE_ANALYZE,
                AgentTask.status == TaskStatus.COMPLETED,
            )
        ).all()
        latest: dict[int, AgentTask] = {}
        for task in tasks:
            index = int(task.input_data.get("batch_index") or 1)
            current = latest.get(index)
            if current is None or (
                as_utc(task.created_at),
                as_utc(task.completed_at or task.created_at),
            ) >= (
                as_utc(current.created_at),
                as_utc(current.completed_at or current.created_at),
            ):
                latest[index] = task
        return [latest[key] for key in sorted(latest)]

    def merged_scene_analysis(self, content_id: str) -> dict | None:
        """全部批次完成时按 scene_id 合并为完整 `scenes[]`；未集齐返回 None。"""
        tasks = self.scene_analysis_tasks(content_id)
        if not tasks:
            return None
        expected = max(int(task.input_data.get("batch_count") or 1) for task in tasks)
        if len(tasks) < expected:
            return None
        scenes = [
            scene for task in tasks for scene in (task.result_data or {}).get("scenes", []) or []
        ]
        scenes.sort(key=lambda scene: str(scene.get("scene_id")))
        return {"scenes": scenes}

    def scene_batch_gaps(self, content_id: str, merged: dict) -> list[str]:
        """合并结果对账：与派发时下发的场景集合逆向比对，返回缺失/重复描述。"""
        expected: list[str] = []
        for task in self.scene_analysis_tasks(content_id):
            expected.extend(
                str(scene.get("scene_id"))
                for scene in task.input_data.get("scenes", []) or []
                if scene.get("scene_id")
            )
        got = [str(scene.get("scene_id")) for scene in merged.get("scenes", []) or []]
        problems: list[str] = []
        missing = sorted(set(expected) - set(got))
        if missing:
            problems.append(f"缺少场景：{'、'.join(missing[:20])}")
        duplicated = sorted({item for item in got if got.count(item) > 1})
        if duplicated:
            problems.append(f"场景重复：{'、'.join(duplicated[:20])}")
        return problems

    def mark_status(
        self, content_id: str, status: ContentStatus, stage: str | None = None
    ) -> Content:
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        content.business_status = status
        content.current_stage = stage
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.commit()
        self.session.refresh(content)
        return content

    def record_publication(
        self,
        content_id: str,
        *,
        platform: str,
        external_url: str = "",
        external_id: str = "",
        note: str = "",
        mode: PublicationMode = PublicationMode.MANUAL,
        actor_id: str = "admin",
    ) -> PublicationRecord:
        """Create or update one platform record; a video may be published to many platforms."""
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.PACKAGED, ContentStatus.PUBLISHED}:
            raise ValueError("Only PACKAGED or PUBLISHED content can record publication")
        platform = platform.strip()
        if not platform:
            raise ValueError("Platform is required")
        record = self.session.exec(
            select(PublicationRecord).where(
                PublicationRecord.content_id == content_id,
                PublicationRecord.platform == platform,
            )
        ).first()
        created = record is None
        if record is None:
            record = PublicationRecord(content_id=content_id, platform=platform, mode=mode)
        record.mode = mode
        record.external_url = external_url.strip()
        record.external_id = external_id.strip()
        record.note = note.strip()
        record.actor_id = actor_id
        record.published_at = utc_now()
        if content.business_status == ContentStatus.PACKAGED:
            content.business_status = ContentStatus.PUBLISHED
            content.current_stage = "published"
            content.updated_at = utc_now()
        self.session.add(record)
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="human" if mode == PublicationMode.MANUAL else "system",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="publication_recorded",
                metadata_json={
                    "platform": platform,
                    "external_url": record.external_url,
                    "external_id": record.external_id,
                    "mode": mode.value,
                    "operation": "created" if created else "updated",
                },
            )
        )
        self.session.commit()
        self.session.refresh(record)
        return record

    def record_agent_publication_result(self, task: AgentTask) -> PublicationRecord | None:
        """Persist a validated publish-Agent result without treating a failure as published."""
        if task.task_type != TaskType.DOUYIN_PUBLISH or not task.content_id or not task.result_data:
            return None
        content = self.session.get(Content, task.content_id)
        if content is None:
            raise KeyError(task.content_id)
        result = task.result_data
        platform = (
            str(result.get("platform") or task.input_data.get("platform") or "").strip().lower()
        )
        status = str(result.get("status") or "UNKNOWN").strip().upper()
        external_url = str(result.get("external_url") or "").strip()
        external_id = str(result.get("external_id") or "").strip()
        evidence_id = str(result.get("evidence_artifact_id") or "").strip()
        message = str(result.get("message") or "").strip()
        note_parts = [
            part
            for part in (message, f"证据 Artifact: {evidence_id}" if evidence_id else "")
            if part
        ]
        note = "；".join(note_parts)
        actor_id = task.assigned_agent_id or "publish-agent"
        expected_platform = str(task.input_data.get("platform") or "").strip().lower()
        if platform != expected_platform:
            raise ValueError(f"发布平台必须与任务目标一致：{expected_platform}")
        record = self.session.exec(
            select(PublicationRecord).where(
                PublicationRecord.content_id == task.content_id,
                PublicationRecord.platform == platform,
            )
        ).first()
        if record is None:
            record = PublicationRecord(
                content_id=task.content_id,
                platform=platform,
                mode=PublicationMode.EXECUTOR,
            )
            created = True
        else:
            created = False
        if status == "PUBLISHED":
            self.tasks.validate_published_publish_result(
                task,
                external_id=external_id,
                external_url=external_url,
                evidence_artifact_id=evidence_id,
            )
        if (
            not created
            and record.mode == PublicationMode.EXECUTOR
            and record.status == status
            and record.external_url == external_url
            and record.external_id == external_id
            and record.note == note
        ):
            return record
        intent = self.tasks.reconcile_publish_intent(
            task,
            status=status,
            actor_id=actor_id,
            reason=message,
            external_id=external_id,
            external_url=external_url,
            evidence_artifact_id=evidence_id,
        )
        if status == "PUBLISHED" and intent is None:
            raise ValueError("PUBLISHED 必须先登记 publish submit intent")
        record.mode = PublicationMode.EXECUTOR
        record.status = status
        record.external_url = external_url
        record.external_id = external_id
        record.note = note
        record.actor_id = actor_id
        record.published_at = utc_now()
        if status == "PUBLISHED" and content.business_status in {
            ContentStatus.PACKAGED,
            ContentStatus.PUBLISHED,
        }:
            content.business_status = ContentStatus.PUBLISHED
            content.current_stage = "published"
            content.updated_at = utc_now()
        self.session.add(record)
        self.session.add(content)
        if status in {"FAILED", "UNKNOWN", "NEEDS_USER"}:
            issue = self.session.exec(
                select(ContentIssue).where(
                    ContentIssue.content_id == task.content_id,
                    ContentIssue.source_type == IssueSourceType.PUBLICATION,
                    ContentIssue.source_id == task.id,
                    ContentIssue.status == IssueStatus.OPEN,
                )
            ).first()
            if issue is None:
                publish_policy = task.input_data.get("publish_policy") or {}
                handsfree_publish = publish_policy.get("initiated_by") == "handsfree"
                issue_detail = {
                    "status": status,
                    "message": (
                        "Handsfree 已安全终止此条发布；未核实的发布结果不会重试。"
                        if handsfree_publish
                        else message or "抖音发布未完成，需要人工处理"
                    ),
                    "external_id": external_id,
                    "external_url": external_url,
                }
                if handsfree_publish:
                    issue_detail["next_action"] = "automatic_skip_without_retry"
                classification = (
                    IssueClassification.POLICY
                    if status == "NEEDS_USER"
                    else IssueClassification.UNKNOWN
                )
                self.session.add(
                    ContentIssue(
                        content_id=task.content_id,
                        code=f"DOUYIN_PUBLISH_{status}",
                        classification=classification,
                        source_type=IssueSourceType.PUBLICATION,
                        source_id=task.id,
                        subject_sha256=str(task.input_data.get("video_sha256") or ""),
                        detail_json=issue_detail,
                    )
                )
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id=actor_id,
                entity_type="content",
                entity_id=task.content_id,
                action="publication_agent_result",
                metadata_json={
                    "task_id": task.id,
                    "platform": platform,
                    "status": status,
                    "external_url": external_url,
                    "external_id": external_id,
                    "evidence_artifact_id": evidence_id,
                    "operation": "created" if created else "updated",
                },
            )
        )
        self.session.commit()
        self.session.refresh(record)
        return record

    def _is_ai_cover_artifact(self, content_id: str, artifact_id: str) -> bool:
        events = self.session.exec(
            select(AuditEvent).where(
                AuditEvent.entity_type == "content",
                AuditEvent.entity_id == content_id,
                AuditEvent.action.in_(["cover_ai_upgraded", "cover_title_rebuilt"]),
            )
        ).all()
        return any(
            (event.metadata_json or {}).get("artifact_id") == artifact_id
            and (event.metadata_json or {}).get("base_source") == "generated_image"
            for event in events
        )

    def queue_douyin_publish(
        self,
        content_id: str,
        *,
        settings: Settings,
        initiated_by: str = "operator",
        user_clicked: bool = True,
        allow_retry: bool = True,
    ) -> dict[str, Any]:
        """Create the single-account publish task for manual or Handsfree callers."""
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.PACKAGED, ContentStatus.PUBLISHED}:
            raise ValueError("只有已打包或已发布内容可以发布到抖音")

        video = self.session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content_id,
                Artifact.artifact_type.in_(["publish_video", "final_video"]),
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        if video is None:
            raise ValueError("缺少可发布的视频产物")
        cover = self.session.exec(
            select(Artifact)
            .where(Artifact.content_id == content_id, Artifact.artifact_type == "cover")
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        if (
            content.business_status == ContentStatus.PACKAGED
            and settings.cover_ai_enabled
            and settings.asset_image_enabled
            and (cover is None or not self._is_ai_cover_artifact(content_id, cover.id))
        ):
            raise ValueError("AI 封面尚未生成完成，不能进入抖音发布队列")

        target = {
            "platform": "douyin",
            "account_id": "local-default",
            "content_id": content_id,
            "video_artifact_id": video.id,
            "video_sha256": video.sha256,
        }
        target["intent_key"] = stable_hash(target)
        existing = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.DOUYIN_PUBLISH,
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        completed_record = self.session.exec(
            select(PublicationRecord).where(
                PublicationRecord.content_id == content_id,
                PublicationRecord.platform == "douyin",
                PublicationRecord.status.in_(["PUBLISHED", "RECORDED"]),
            )
        ).first()
        if completed_record is not None:
            return {
                "status": "ALREADY_COMPLETED",
                "task_id": existing.id if existing is not None else None,
                "task_type": TaskType.DOUYIN_PUBLISH.value,
                "platform": "douyin",
            }
        if existing is not None and existing.status == TaskStatus.COMPLETED:
            previous_status = str((existing.result_data or {}).get("status") or "").upper()
            if previous_status == "PUBLISHED":
                return {
                    "status": "ALREADY_COMPLETED",
                    "task_id": existing.id,
                    "task_type": TaskType.DOUYIN_PUBLISH.value,
                    "platform": "douyin",
                }
        existing_intent = self.tasks.publish_intent_for_target(target)
        if existing_intent is not None:
            return {
                "status": "RECONCILE_REQUIRED",
                "task_id": existing_intent.get("task_id") or None,
                "task_type": TaskType.DOUYIN_PUBLISH.value,
                "platform": "douyin",
                "intent_key": existing_intent["intent_key"],
                "intent_status": existing_intent["intent_status"],
            }
        if existing is not None and existing.status in {
            TaskStatus.PENDING,
            TaskStatus.LEASED,
            TaskStatus.SUBMITTED,
        }:
            return {
                "status": "ALREADY_QUEUED",
                "task_id": existing.id,
                "task_type": TaskType.DOUYIN_PUBLISH.value,
                "platform": "douyin",
            }
        if existing is not None and existing.status == TaskStatus.COMPLETED:
            previous_status = str((existing.result_data or {}).get("status") or "").upper()
            if not allow_retry:
                return {
                    "status": "PUBLISH_BLOCKED",
                    "task_id": existing.id,
                    "task_type": TaskType.DOUYIN_PUBLISH.value,
                    "platform": "douyin",
                    "reason": previous_status or "previous_attempt_not_published",
                }

        candidate = self.session.get(Candidate, content.candidate_id)

        def latest_result(task_type: TaskType) -> dict[str, Any]:
            row = self.session.exec(
                select(AgentTask)
                .where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == task_type,
                    AgentTask.status == TaskStatus.COMPLETED,
                )
                .order_by(AgentTask.completed_at.desc(), AgentTask.created_at.desc())
            ).first()
            return dict(row.result_data or {}) if row is not None else {}

        content_artifacts = self.session.exec(
            select(Artifact)
            .where(Artifact.content_id == content_id)
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).all()
        previous_input = existing.input_data if existing is not None else {}
        publish_attempt = int(previous_input.get("publish_attempt") or 0) + 1
        input_data = {
            "platform": "douyin",
            "account": {
                "account_id": "local-default",
                # SAU stores the configured session as douyin_local-default.json.
                "account_name": "local-default",
                "single_account": True,
            },
            "publish_attempt": publish_attempt,
            "retry_of": existing.id if existing is not None else "",
            "video_artifact_id": video.id,
            "cover_artifact_id": cover.id if cover is not None else "",
            "video_sha256": video.sha256,
            "content": {
                "id": content.id,
                "title": content.title,
                "title_zh": content.title_zh,
                "production_mode": content.production_mode.value,
                "business_status": content.business_status.value,
                "script_version": content.script_version,
            },
            "candidate": {
                "title": candidate.title_zh or candidate.original_title if candidate else "",
                "summary": candidate.summary if candidate else "",
                "source_url": candidate.source_url if candidate else "",
                "rights_status": candidate.rights_status.value if candidate else "",
                "rights_note": candidate.rights_note if candidate else "",
            },
            "script": latest_result(TaskType.SHORT_SCRIPT),
            "research": latest_result(TaskType.CONTENT_RESEARCH),
            "publish_prepare": latest_result(TaskType.PUBLISH_PREPARE),
            "artifacts": [
                {
                    "id": artifact.id,
                    "type": artifact.artifact_type,
                    "relative_path": artifact.relative_path,
                    "sha256": artifact.sha256,
                    "size_bytes": artifact.size_bytes,
                    "mime_type": artifact.mime_type,
                }
                for artifact in content_artifacts
            ],
            "context_files": [
                path
                for path in [
                    video.relative_path,
                    cover.relative_path if cover is not None else "",
                ]
                if path
            ],
            "publish_policy": {
                "creative_fields": "free_under_current_prompt",
                "single_local_account": True,
                "platform_fixed": "douyin",
                "initiated_by": initiated_by,
                "user_clicked": bool(user_clicked),
                "explicit_user_opt_in": initiated_by == "handsfree",
                "no_blind_retry_after_publish_click": True,
            },
        }
        task = self.create_agent_task(
            content_id,
            TaskType.DOUYIN_PUBLISH,
            input_data,
            priority=70,
            allow_multiple=True,
            handsfree=initiated_by == "handsfree",
        )
        if task.max_attempts != 2:
            task.max_attempts = 2
            self.session.add(task)
            self.session.commit()
        return {
            "status": "QUEUED",
            "task_id": task.id,
            "task_type": TaskType.DOUYIN_PUBLISH.value,
            "platform": "douyin",
            "publish_attempt": publish_attempt,
        }

    def set_error(self, content_id: str, code: str | None, message: str | None) -> Content:
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        content.error_code = code
        content.error_message = message
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.commit()
        self.session.refresh(content)
        return content

    def invalidate_visual_plan(
        self, content_id: str, *, include_review_tasks: bool = False, reason: str = "voice_redub",
        preserve_packaged_materials: bool = False,
    ) -> int:
        """作废旧画面链路，允许按新输入重建。"""
        task_types = set(_VISUAL_PLAN_DOWNSTREAM_TASK_TYPES)
        if include_review_tasks:
            task_types.update(
                {
                    TaskType.CONTENT_QC,
                    TaskType.FINAL_REVIEW_ASSIST,
                    TaskType.PUBLISH_PREPARE,
                    TaskType.PUBLISH_REVIEW_ASSIST,
                }
            )
        return self._cancel_content_tasks(
            content_id, task_types, reason=reason,
            preserve_packaged_materials=preserve_packaged_materials,
        )

    def _queue_script_rewrite(
        self,
        content: Content,
        *,
        research_task: AgentTask,
        scene_tasks: list[AgentTask],
        previous_script: dict | None,
        reviewer_note: str,
        reason: str,
        priority: int,
        auto_rewrite_attempt: int | None = None,
        auto_assist_revision_attempt: int | None = None,
        script_qc_issues: list[dict] | None = None,
        previous_qc_task_id: str | None = None,
        target_duration_sec: float | None = None,
        min_video_seconds: float | None = None,
        max_video_seconds: float | None = None,
        previous_plan: dict | None = None,
        preserve_packaged_materials: bool = False,
    ) -> AgentTask:
        transcript_path = Path(content.content_dir).resolve() / "transcript" / "transcript.json"
        previous_script_data = previous_script if isinstance(previous_script, dict) else {}
        previous_script = {
            key: previous_script_data.get(key)
            for key in _SCRIPT_RESULT_FIELDS
            if key in previous_script_data
        }
        upstream_task_ids: dict[str, str | list[str]] = {"research": research_task.id}
        if scene_tasks:
            upstream_task_ids["scene_analysis"] = [task.id for task in scene_tasks]
        self._cancel_content_tasks(
            content.id,
            {
                TaskType.SHORT_SCRIPT,
                TaskType.SCRIPT_QC,
                TaskType.SCRIPT_REVIEW_ASSIST,
                *_VISUAL_PLAN_DOWNSTREAM_TASK_TYPES,
            },
            reason=reason,
            preserve_packaged_materials=preserve_packaged_materials,
        )
        input_data = {
            "upstream_task_ids": upstream_task_ids,
            **(
                {"materialized_context": {"transcript": "transcript/transcript.json"}}
                if transcript_path.is_file()
                else {"transcript": {"segments": []}}
            ),
            "target_duration_sec": (
                _SCRIPT_DEFAULT_TARGET_SECONDS
                if target_duration_sec is None
                else target_duration_sec
            ),
            "style": "面向中文大众，结果前置，给出明确判断",
            "reviewer_note": _script_rewrite_note(reviewer_note),
            "previous_script": previous_script,
            "production_mode": content.production_mode.value,
            "allowed_claim_ids": [
                str(claim.get("id"))
                for claim in (research_task.result_data or {}).get("verified_claims", [])
                if isinstance(claim, dict) and claim.get("id")
            ],
            **self.script_audience_inputs(content, research_task),
        }
        if auto_rewrite_attempt is not None:
            input_data["auto_rewrite_attempt"] = auto_rewrite_attempt
        if previous_plan is not None:
            input_data["previous_plan"] = previous_plan
        if preserve_packaged_materials:
            input_data["packaged_script_revision"] = True
        if auto_assist_revision_attempt is not None:
            input_data["auto_assist_revision_attempt"] = auto_assist_revision_attempt
        if script_qc_issues is not None:
            input_data["script_qc_issues"] = script_qc_issues
        if previous_qc_task_id:
            input_data["previous_qc_task_id"] = previous_qc_task_id
        if min_video_seconds is not None:
            input_data["min_video_seconds"] = min_video_seconds
        if max_video_seconds is not None:
            input_data["max_video_seconds"] = max_video_seconds
        if preserve_packaged_materials:
            # create_task commits: publish the revision state with its frozen input.
            content.script_version += 1
            content.error_code = content.error_message = None
            content.business_status = ContentStatus.WAITING_AGENT
            content.current_stage = "script_rewrite"
            content.updated_at = utc_now()
            self.session.add(content)
        rewrite = self.create_agent_task(
            content.id,
            TaskType.SHORT_SCRIPT,
            input_data,
            priority=priority,
        )
        if not preserve_packaged_materials:
            content.script_version += 1
        content.error_code = None
        content.error_message = None
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "script_rewrite"
        content.updated_at = utc_now()
        self.session.add(content)
        return rewrite

    @staticmethod
    def _assistant_revision_note(review_result: dict[str, Any], qc_result: dict[str, Any]) -> str:
        """Turn structured assistant feedback into bounded input for a new script task."""
        parts = ["脚本审核助手提出了自动改进建议，请局部修正当前脚本并保留已验证事实边界。"]
        verdict = str(review_result.get("verdict") or "").strip()
        if verdict:
            parts.append(f"助手结论：{verdict}")
        summary = str(review_result.get("summary") or "").strip()
        if summary:
            parts.append(f"助手摘要：{summary}")
        reasons = [
            str(item).strip() for item in (review_result.get("reasons") or []) if str(item).strip()
        ]
        if reasons:
            parts.append("助手判断依据：\n" + "\n".join(f"- {item}" for item in reasons[:8]))
        issues = [item for item in (review_result.get("issues") or []) if isinstance(item, dict)]
        issue_lines = []
        for issue in issues[:30]:
            severity = str(issue.get("severity") or "warning")
            message = str(issue.get("message") or "").strip()
            if not message:
                continue
            timecode = issue.get("timecode_sec")
            location = f"时间点 {timecode}s" if timecode is not None else "脚本"
            issue_lines.append(f"- [{severity}] {location}: {message}")
        if issue_lines:
            parts.append("助手发现的问题：\n" + "\n".join(issue_lines))
        checklist = [
            str(item).strip()
            for item in (review_result.get("checklist") or [])
            if str(item).strip()
        ]
        if checklist:
            parts.append(
                "建议一并落实的检查项：\n" + "\n".join(f"- {item}" for item in checklist[:20])
            )
        suggested_revision = qc_result.get("suggested_revision")
        if isinstance(suggested_revision, dict) and suggested_revision:
            lines = [
                f"- {key}: {str(suggested_revision[key]).strip()}"
                for key in sorted(suggested_revision)
                if str(suggested_revision[key]).strip()
            ]
            if lines:
                parts.append("脚本质检建议的改法：\n" + "\n".join(lines[:20]))
        return "\n\n".join(parts)[:4000]

    def auto_revise_script_from_assist(
        self, content_id: str, review_task: AgentTask
    ) -> AgentTask | None:
        """Create one bounded script rewrite from the current assistant review."""
        content = self.session.get(Content, content_id)
        script_task = self.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        qc_task = self.completed_task_for(content_id, TaskType.SCRIPT_QC)
        research_task = self.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
        if (
            content is None
            or content.business_status != ContentStatus.SCRIPT_REVIEW
            or script_task is None
            or qc_task is None
            or research_task is None
            or not (qc_task.result_data or {}).get("passed")
        ):
            return None
        declared = (review_task.input_data or {}).get("upstream_task_ids") or {}
        current_ids = {
            "script": script_task.id,
            "script_qc": qc_task.id,
            "research": research_task.id,
        }
        if any(
            declared.get(key) and str(declared.get(key)) != current_id
            for key, current_id in current_ids.items()
        ):
            return None
        qc_script_id = str(
            ((qc_task.input_data or {}).get("upstream_task_ids") or {}).get("script") or ""
        )
        if qc_script_id and qc_script_id != script_task.id:
            return None
        attempt = int((script_task.input_data or {}).get("auto_assist_revision_attempt") or 0)
        if attempt >= MAX_AUTO_ASSIST_REVISIONS:
            return None
        review_result = review_task.result_data or {}
        qc_result = qc_task.result_data or {}
        rewrite = self._queue_script_rewrite(
            content,
            research_task=research_task,
            scene_tasks=self.scene_analysis_tasks(content_id),
            previous_script=script_task.result_data,
            reviewer_note=self._assistant_revision_note(review_result, qc_result),
            reason="script-review-assist-auto-rewrite",
            priority=47,
            auto_assist_revision_attempt=attempt + 1,
        )
        issues = [item for item in (review_result.get("issues") or []) if isinstance(item, dict)]
        suggested_revision = qc_result.get("suggested_revision")
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="assistant-policy",
                entity_type="content",
                entity_id=content_id,
                action="script_assist_auto_rewrite_requested",
                metadata_json={
                    "assistant_task_id": review_task.id,
                    "script_qc_task_id": qc_task.id,
                    "rewrite_task_id": rewrite.id,
                    "attempt": attempt + 1,
                    "verdict": review_result.get("verdict"),
                    "issue_count": len(issues),
                    "qc_suggestion_fields": sorted(suggested_revision)
                    if isinstance(suggested_revision, dict)
                    else [],
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        self.session.refresh(rewrite)
        return rewrite

    def auto_rewrite_script(
        self,
        content_id: str,
        script_task: AgentTask,
        qc_task: AgentTask,
    ) -> bool:
        """Automatically requeue a deterministic script rewrite after failed QC.

        Returns False when the bounded automatic retry budget is exhausted or the QC
        result has no actionable issue list, allowing the caller to escalate to a
        human review queue without pretending the script passed.
        """
        existing_rewrite = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.SHORT_SCRIPT,
                AgentTask.status.in_(
                    [
                        TaskStatus.PENDING,
                        TaskStatus.LEASED,
                        TaskStatus.SUBMITTED,
                        TaskStatus.COMPLETED,
                    ]
                ),
            )
        ).all()
        if any(
            str((task.input_data or {}).get("previous_qc_task_id") or "") == qc_task.id
            for task in existing_rewrite
        ):
            return True
        qc_result = qc_task.result_data or {}
        if qc_result.get("passed"):
            return False
        attempt = int((script_task.input_data or {}).get("auto_rewrite_attempt") or 0)
        if attempt >= MAX_AUTO_SCRIPT_REWRITES:
            return False
        issues = [issue for issue in (qc_result.get("issues") or []) if isinstance(issue, dict)]
        if not issues:
            return False
        content = self.session.get(Content, content_id)
        research_task = self.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
        scene_tasks = self.scene_analysis_tasks(content_id)
        if content is None or research_task is None:
            return False
        qc_upstream = (qc_task.input_data or {}).get("upstream_task_ids") or {}
        if (qc_upstream.get("script") and str(qc_upstream["script"]) != script_task.id) or (
            qc_upstream.get("research") and str(qc_upstream["research"]) != research_task.id
        ):
            return False
        if content.production_mode != ProductionMode.IMAGE_ONLY and not scene_tasks:
            return False
        assistant_revision_attempt = (script_task.input_data or {}).get(
            "auto_assist_revision_attempt"
        )
        target_duration_sec, readable_chars, min_video_seconds, max_video_seconds = (
            _script_rewrite_duration(script_task.result_data, script_task.input_data)
        )
        issue_lines = [
            f"- [{issue.get('severity', 'warning')}] {issue.get('field', 'unknown')}: "
            f"{issue.get('message', '')}"
            for issue in issues
        ]
        suggested_revision = qc_result.get("suggested_revision")
        suggested_lines = (
            [
                f"- {key}: {str(suggested_revision[key]).strip()}"
                for key in sorted(suggested_revision)
                if str(suggested_revision[key]).strip()
            ]
            if isinstance(suggested_revision, dict)
            else []
        )
        reviewer_note_parts = [
            (
                f"服务端目标时长：{target_duration_sec:g} 秒；"
                f"上一版旁白有 {readable_chars} 个可读字符，"
                f"按每秒约 {_SCRIPT_CHARS_PER_SECOND} 字计算，已限制在 "
                f"{min_video_seconds:g}-{max_video_seconds:g} 秒范围内。"
            ),
            "上一版脚本未通过脚本质检，请按下面的问题局部修正及必要的一致性联动；"
            "保留事实边界，不要把官方宣传语改写成已证实事实。\n" + "\n".join(issue_lines),
        ]
        if suggested_lines:
            reviewer_note_parts.append("质检建议的改法：\n" + "\n".join(suggested_lines[:20]))
        next_attempt = attempt + 1
        rewrite = self._queue_script_rewrite(
            content,
            research_task=research_task,
            scene_tasks=scene_tasks,
            previous_script=script_task.result_data,
            reviewer_note="\n\n".join(reviewer_note_parts),
            reason="script-qc-auto-rewrite",
            priority=46,
            auto_rewrite_attempt=next_attempt,
            auto_assist_revision_attempt=(
                int(assistant_revision_attempt) if assistant_revision_attempt is not None else None
            ),
            script_qc_issues=issues,
            previous_qc_task_id=qc_task.id,
            target_duration_sec=target_duration_sec,
            min_video_seconds=min_video_seconds,
            max_video_seconds=max_video_seconds,
        )
        content.error_code = "SCRIPT_QC_FAILED"
        content.error_message = (
            f"脚本质检未通过，已自动返工（第 {next_attempt}/{MAX_AUTO_SCRIPT_REWRITES} 次）"
        )
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="system",
                actor_id="pipeline",
                entity_type="content",
                entity_id=content.id,
                action="script_auto_rewrite_requested",
                metadata_json={
                    "qc_task_id": qc_task.id,
                    "rewrite_task_id": rewrite.id,
                    "attempt": next_attempt,
                    "issue_count": len(issues),
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        return True

    def reject_script(
        self, content_id: str, note: str, *, packaged_previous_plan: dict | None = None
    ) -> Content:
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        packaged_revision = (
            content.business_status == ContentStatus.PACKAGED and packaged_previous_plan is not None
        )
        if not packaged_revision and content.business_status not in {ContentStatus.SCRIPT_REVIEW, ContentStatus.FAILED}:
            raise ValueError("Only SCRIPT_REVIEW or FAILED content can have its script rejected")
        research_task = self.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
        scene_tasks = self.scene_analysis_tasks(content_id)
        previous_script = self.result_for(content_id, TaskType.SHORT_SCRIPT)
        if research_task is None or (
            content.production_mode != ProductionMode.IMAGE_ONLY and not scene_tasks
        ):
            raise ValueError("脚本重写缺少已完成的研究或场景分析结果")
        rewrite = self._queue_script_rewrite(
            content,
            research_task=research_task,
            scene_tasks=scene_tasks,
            previous_script=previous_script,
            reviewer_note=note,
            reason="script_rejected",
            priority=45,
            previous_plan=packaged_previous_plan if packaged_revision else None,
            preserve_packaged_materials=packaged_revision,
        )
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id="admin",
                entity_type="content",
                entity_id=content.id,
                action="script_rejected",
                metadata_json={
                    "note": note,
                    "script_version": content.script_version,
                    "rewrite_task_id": rewrite.id,
                    **({"packaged_script_revision": True} if packaged_revision else {}),
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        return content

    def remediate_content_qc(
        self, content_id: str, *, actor_id: str = "admin-console"
    ) -> tuple[Content, AgentTask]:
        """Start a structured script rewrite for a failed rendered-content QC.

        The rewrite is deliberately routed through the existing SHORT_SCRIPT -> SCRIPT_QC ->
        SCRIPT_REVIEW chain. A human still approves the rewritten script before voice, visual
        planning, rendering, and a fresh CONTENT_QC run can proceed.
        """
        content = self.session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.error_code != "CONTENT_QC_FAILED":
            raise ValueError("内容当前不是成片语义质检失败状态")
        if content.business_status != ContentStatus.WAITING_AGENT:
            raise ValueError("只有等待 Agent 的质检失败内容可以发起整改")

        existing = self.session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.SHORT_SCRIPT,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED]),
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if existing is not None and content.current_stage == "content_qc_remediation":
            return content, existing

        qc_task = self.latest_failed_content_qc(content_id)
        if qc_task is None:
            raise ValueError("找不到已完成的成片语义质检结果")
        issues = list((qc_task.result_data or {}).get("issues") or [])
        if not issues:
            raise ValueError("成片语义质检没有可整改的问题清单")

        research_task = self.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
        previous_script_task = self.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        scene_tasks = self.scene_analysis_tasks(content_id)
        if (
            research_task is None
            or previous_script_task is None
            or (content.production_mode != ProductionMode.IMAGE_ONLY and not scene_tasks)
        ):
            raise ValueError("缺少研究结果或上一版脚本，无法发起整改")
        previous_script_data = previous_script_task.result_data or {}
        previous_script = {
            key: previous_script_data.get(key)
            for key in (
                "title_options",
                "hook_type",
                "bgm_mood",
                "opening",
                "retention_beats",
                "cta",
                "cover_title",
                "cover_visual_brief",
                "body",
                "verdict",
                "full_narration",
                "estimated_duration_sec",
                "claims_used",
                "visual_requirements",
            )
            if key in previous_script_data
        }
        previous_plan_task = self.completed_task_for(content_id, TaskType.VISUAL_PLAN)
        transcript_path = Path(content.content_dir).resolve() / "transcript" / "transcript.json"
        issue_lines = [
            f"- [{issue.get('severity', 'warning')}] {issue.get('field', 'unknown')}: "
            f"{issue.get('message', '')}"
            for issue in issues
        ]
        reviewer_note = _script_rewrite_note(
            "上一版成片语义质检未通过，请按下面的问题重写脚本，并保留事实边界；"
            "不要把官方宣传语改写成已证实事实。\n" + "\n".join(issue_lines)
        )

        self._cancel_content_tasks(
            content_id,
            {
                TaskType.SHORT_SCRIPT,
                TaskType.SCRIPT_QC,
                TaskType.SCRIPT_REVIEW_ASSIST,
                *_VISUAL_PLAN_DOWNSTREAM_TASK_TYPES,
            },
            reason="content-qc-remediation",
        )
        upstream_task_ids: dict[str, str | list[str]] = {"research": research_task.id}
        if scene_tasks:
            upstream_task_ids["scene_analysis"] = [task.id for task in scene_tasks]
        rewrite = self.create_agent_task(
            content_id,
            TaskType.SHORT_SCRIPT,
            {
                "upstream_task_ids": {
                    **upstream_task_ids,
                    **(
                        {"previous_plan": previous_plan_task.id}
                        if previous_plan_task is not None
                        else {}
                    ),
                },
                **(
                    {"materialized_context": {"transcript": "transcript/transcript.json"}}
                    if transcript_path.is_file()
                    else {"transcript": {"segments": []}}
                ),
                "target_duration_sec": 35,
                "style": "面向中文大众，结果前置，给出明确判断",
                "reviewer_note": reviewer_note,
                "previous_script": previous_script,
                "content_qc_issues": issues,
                "previous_qc_task_id": qc_task.id,
                "production_mode": content.production_mode.value,
                "allowed_claim_ids": [
                    str(claim.get("id"))
                    for claim in (research_task.result_data or {}).get("verified_claims", [])
                    if isinstance(claim, dict) and claim.get("id")
                ],
                **self.script_audience_inputs(content, research_task),
            },
            priority=52,
        )
        content.error_code = "CONTENT_QC_FAILED"
        content.error_message = "成片语义质检未通过，已发起整改；脚本审核通过后将重新渲染并复检"
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "content_qc_remediation"
        content.updated_at = utc_now()
        self.session.add(content)
        self.session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="content_qc_remediation_requested",
                metadata_json={
                    "qc_task_id": qc_task.id,
                    "rewrite_task_id": rewrite.id,
                    "issue_count": len(issues),
                },
            )
        )
        self.session.commit()
        self.session.refresh(content)
        return content, rewrite

    def _cancel_content_tasks(
        self,
        content_id: str,
        task_types: set[TaskType],
        *,
        reason: str,
        preserve_packaged_materials: bool = False,
    ) -> int:
        tasks = self.session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type.in_(task_types),
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.COMPLETED]),
            )
        ).all()
        if preserve_packaged_materials:
            # A manual packaged revision reuses its cover and retains negative evidence.
            tasks = [task for task in tasks if not (
                (task.input_data or {}).get("asset_purpose") == "cover"
                or (task.status == TaskStatus.COMPLETED and (
                    (task.result_data or {}).get("passed") is False
                    or (task.result_data or {}).get("verdict") in {"REVISE", "HUMAN", "READY", "BLOCKED", "REJECTED", "REJECT"}
                ))
            )]
        for task in tasks:
            if task.status in {TaskStatus.PENDING, TaskStatus.LEASED}:
                self.tasks.cancel(task.id, actor_id=reason)
            else:
                task.status = TaskStatus.CANCELLED
                # 与 cancel()/fail()/REJECTED 约定对齐：终态任务必须释放 dedup_key，
                # 否则同输入重建任务会被 dedup 命中这条 CANCELLED 任务永久挡死。
                task.dedup_key = None
                self.session.add(task)
                self.session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id=reason,
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="cancelled",
                        metadata_json={"reason": reason},
                    )
                )
        if tasks:
            if preserve_packaged_materials:
                self.session.flush()
            else:
                self.session.commit()
        return len(tasks)
