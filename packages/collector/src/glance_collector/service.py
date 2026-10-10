from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from glance_core.models import (
    AgentTask,
    Candidate,
    CandidateSignal,
    CollectionRun,
    RightsStatus,
    Source,
    SourceType,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.prompts import resolve_instructions
from glance_core.research import direction_key_for, research_history
from glance_core.settings import Settings
from glance_core.task_service import TaskLaneBusyError
from glance_core.workflow import (
    WorkflowService,
    normalize_video_url,
    parse_platform_domains,
    url_on_video_platform,
)
from sqlmodel import Session, select

from glance_collector.collectors import (
    CollectedItem,
    CollectorRegistry,
    _is_markup_content_type,
)

_THUMBNAIL_BACKFILL_META = "thumbnail_backfill"
_THUMBNAIL_FORBIDDEN_BACKOFF = timedelta(hours=24)
_THUMBNAIL_RESOLVER_VERSION = 2
_EMPTY_RESULT_BACKOFF_META = "empty_result_backoff"
_EMPTY_RESULT_BACKOFF_MINUTES = (15, 30, 60)
_EMPTY_RESULT_MAX_STREAK = len(_EMPTY_RESULT_BACKOFF_MINUTES)
_ACTIVE_DISCOVERY_STATUSES = (TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED)


def _thumbnail_fallback_urls(source_url: str) -> list[str]:
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return []

    urls = [source_url]
    host = parsed.hostname or ""
    if host == "kickstarter.com" or host.endswith(".kickstarter.com"):
        path = parsed.path.rstrip("/")
        if path.startswith("/projects/"):
            urls.append(
                parsed._replace(path=f"{path}/widget/card.html", query="", fragment="").geturl()
            )
    elif host == "makerworld.com" or host.endswith(".makerworld.com"):
        match = re.search(
            r"/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d+)(?:[-/]|$)",
            parsed.path,
            re.IGNORECASE,
        )
        if match:
            urls.append(f"https://makerworld.com/api/v1/design-service/design/{match.group(1)}")

    return list(dict.fromkeys(urls))


def _valid_thumbnail_url(value: object, response_url: str) -> str:
    image = urljoin(response_url, str(value or "").strip())
    parsed = urlparse(image)
    return image if parsed.scheme in {"http", "https"} and parsed.netloc else ""


def _json_thumbnail_url(payload: object, response_url: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("coverUrl", "cover_url", "thumbnailUrl", "thumbnail_url"):
        image = _valid_thumbnail_url(payload.get(key), response_url)
        if image:
            return image
    return ""


def _thumbnail_from_response(response: httpx.Response) -> str:
    content_type = getattr(response, "headers", {}).get("content-type", "").casefold()
    if "json" in content_type:
        try:
            image = _json_thumbnail_url(response.json(), str(response.url))
        except (ValueError, TypeError):
            image = ""
        if image:
            return image

    if not _is_markup_content_type(content_type):
        return ""

    markup = getattr(response, "text", None)
    if markup is None:
        markup = bytes(getattr(response, "content", b"")).decode("utf-8", errors="replace")
    soup = BeautifulSoup(markup, "html.parser")
    for attrs in (
        {"property": "og:image"},
        {"property": "og:image:secure_url"},
        {"name": "twitter:image"},
        {"rel": "image_src"},
    ):
        tag = soup.find("meta", attrs=attrs) or soup.find("link", attrs=attrs)
        image = (
            _valid_thumbnail_url(tag.get("content") or tag.get("href"), str(response.url))
            if tag
            else ""
        )
        if image:
            return image
    if "/widget/card.html" in str(response.url):
        tag = soup.find("img", src=True)
        if tag:
            return _valid_thumbnail_url(tag["src"], str(response.url))
    return ""


def _resolve_thumbnail(client: httpx.Client, source_url: str) -> tuple[str, bool]:
    access_denied = False
    for endpoint in _thumbnail_fallback_urls(source_url):
        try:
            response = client.get(endpoint)
            response.raise_for_status()
            image = _thumbnail_from_response(response)
            if image:
                return image, False
        except httpx.HTTPStatusError as exc:
            access_denied = access_denied or exc.response.status_code in {401, 403}
        except (httpx.HTTPError, ValueError):
            continue
    return "", access_denied


def _thumbnail_backfill_blocked(signal: CandidateSignal) -> bool:
    """Avoid retrying a known access-denied page on every 30-minute sweep."""
    state = (signal.raw_metadata or {}).get(_THUMBNAIL_BACKFILL_META)
    if not isinstance(state, dict) or state.get("status") != "FORBIDDEN":
        return False
    if state.get("resolver_version") != _THUMBNAIL_RESOLVER_VERSION:
        return False
    try:
        retry_at = datetime.fromisoformat(str(state.get("next_retry_at")))
    except (TypeError, ValueError):
        return False
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return retry_at > utc_now()


def _mark_thumbnail_forbidden(signal: CandidateSignal) -> None:
    metadata = dict(signal.raw_metadata or {})
    previous = metadata.get(_THUMBNAIL_BACKFILL_META)
    attempts = int(previous.get("attempts", 0)) if isinstance(previous, dict) else 0
    metadata[_THUMBNAIL_BACKFILL_META] = {
        "status": "FORBIDDEN",
        "attempts": attempts + 1,
        "resolver_version": _THUMBNAIL_RESOLVER_VERSION,
        "next_retry_at": (utc_now() + _THUMBNAIL_FORBIDDEN_BACKOFF).isoformat(),
    }
    signal.raw_metadata = metadata


def _empty_result_next_retry_at(source: Source) -> datetime | None:
    state = (source.config_json or {}).get(_EMPTY_RESULT_BACKOFF_META)
    if not isinstance(state, dict):
        return None
    try:
        retry_at = datetime.fromisoformat(str(state.get("next_retry_at")))
    except (TypeError, ValueError):
        return None
    return as_utc(retry_at)


def _empty_result_backoff_active(source: Source, *, now: datetime | None = None) -> bool:
    retry_at = _empty_result_next_retry_at(source)
    if retry_at is None:
        return False
    return retry_at > as_utc(now or utc_now())


def _normal_poll_minutes(source: Source) -> int:
    return max(1, int(source.poll_interval_minutes))


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _record_agent_result_schedule(source: Source, *, created: int, now: datetime) -> None:
    """Persist bounded empty-result retry state without changing the schema."""
    config = dict(source.config_json or {})
    if created > 0:
        config.pop(_EMPTY_RESULT_BACKOFF_META, None)
        source.config_json = config
        source.next_poll_at = now + timedelta(minutes=_normal_poll_minutes(source))
        return

    previous = config.get(_EMPTY_RESULT_BACKOFF_META)
    previous = previous if isinstance(previous, dict) else {}
    previous_streak = _nonnegative_int(previous.get("streak", 0))
    previous_retry_at = _empty_result_next_retry_at(source)
    if previous.get("status") == "COOLDOWN" and (
        previous_retry_at is None or previous_retry_at <= now
    ):
        previous_streak = 0

    normal_minutes = _normal_poll_minutes(source)
    if previous_streak >= _EMPTY_RESULT_MAX_STREAK:
        delay_minutes = min(normal_minutes, _EMPTY_RESULT_BACKOFF_MINUTES[-1])
        status = "COOLDOWN"
        reason = "max_empty_result_streak"
        streak = _EMPTY_RESULT_MAX_STREAK
    else:
        streak = min(previous_streak + 1, _EMPTY_RESULT_MAX_STREAK)
        delay_minutes = max(
            1,
            min(normal_minutes, _EMPTY_RESULT_BACKOFF_MINUTES[streak - 1]),
        )
        status = "BACKOFF"
        reason = "no_usable_new_candidates"

    retry_at = now + timedelta(minutes=delay_minutes)
    config[_EMPTY_RESULT_BACKOFF_META] = {
        "status": status,
        "reason": reason,
        "streak": streak,
        "max_streak": _EMPTY_RESULT_MAX_STREAK,
        "delay_minutes": delay_minutes,
        "next_retry_at": retry_at.isoformat(),
    }
    source.config_json = config
    source.next_poll_at = retry_at


def _passes_filters(
    source: Source,
    item: CollectedItem,
    video_platform_domains: frozenset[str],
) -> tuple[bool, str]:
    """确定性粗筛：不过筛的条目不入库，不进 Agent 队列。规则存 source.config_json。"""
    config = source.config_json or {}
    if config.get("require_video") and not (
        normalize_video_url(item.video_url, video_platform_domains)
        or url_on_video_platform(item.url, video_platform_domains)
    ):
        return False, "video_source_required"
    haystack = f"{item.title} {item.summary}".casefold()
    exclude = [str(word).casefold() for word in config.get("exclude_keywords", []) if word]
    if any(word in haystack for word in exclude):
        return False, "exclude_keywords"
    include = [str(word).casefold() for word in config.get("include_keywords", []) if word]
    if include and not any(word in haystack for word in include):
        return False, "include_keywords"
    min_heat = config.get("min_heat")
    if min_heat is not None and item.heat_score < float(min_heat):
        return False, "min_heat"
    max_age_days = config.get("max_age_days")
    if max_age_days is not None and item.published_at is not None:
        cutoff = utc_now() - timedelta(days=int(max_age_days))
        if as_utc(item.published_at) < cutoff:
            return False, "max_age_days"
    min_title_len = config.get("min_title_len")
    if min_title_len is not None and len(item.title.strip()) < int(min_title_len):
        return False, "min_title_len"
    return True, ""


class CollectionService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.workflow = WorkflowService(
            session,
            data_dir=settings.data_dir,
            submission_root=settings.agent_submission_dir,
            lease_minutes=settings.agent_lease_minutes,
            video_platform_domains=parse_platform_domains(settings.video_platform_domains),
        )

    def create_source(self, source: Source) -> Source:
        self.session.add(source)
        self.session.commit()
        self.session.refresh(source)
        return source

    def collect_source(self, source_id: str) -> CollectionRun:
        source = self.session.get(Source, source_id)
        if source is None:
            raise KeyError(source_id)
        run = CollectionRun(source_id=source.id)
        self.session.add(run)
        self.session.commit()
        try:
            if not source.enabled:
                run.status = "SKIPPED"
                run.error_code = "SOURCE_DISABLED"
                run.error_message = "来源已禁用，本轮跳过"
                run.completed_at = utc_now()
                self.session.add(run)
                self.session.commit()
                return run
            if source.source_type == SourceType.AGENT and _empty_result_backoff_active(source):
                retry_at = _empty_result_next_retry_at(source)
                run.status = "SKIPPED"
                run.error_code = "EMPTY_RESULT_BACKOFF"
                retry_label = retry_at.isoformat() if retry_at else "稍后"
                run.error_message = f"空结果退避中，本轮跳过（下次重试 {retry_label}）"
                run.completed_at = utc_now()
                self.session.add(run)
                self.session.commit()
                return run
            if source.source_type == SourceType.AGENT:
                busy_task_id = self._schedule_agent_discovery(source, run.id)
                if busy_task_id:
                    run.status = "SKIPPED"
                    # 错误码保留在结构化字段，展示文案用人话中文。
                    run.error_code = "DIRECTION_BUSY"
                    run.error_message = f"方向执行中，本轮跳过（在途任务 {busy_task_id}）"
                    run.completed_at = utc_now()
                    now = utc_now()
                    source.last_polled_at = now
                    source.next_poll_at = max(
                        as_utc(source.next_poll_at), now + timedelta(minutes=15)
                    )
                    self.session.add(run)
                    self.session.add(source)
                    self.session.commit()
                    return run
                return self._finish_run(run, source, [], created=0, filtered=0)
            with CollectorRegistry() as registry:
                items = registry.collect(source)
            created = 0
            filtered = 0
            for item in items:
                passed, _reason = _passes_filters(
                    source, item, self.workflow.video_platform_domains
                )
                if not passed:
                    filtered += 1
                    continue
                created += self._ingest_item(source, item, collection_run_id=run.id)
            return self._finish_run(run, source, items, created=created, filtered=filtered)
        except Exception as exc:
            # 失败路径必须先回滚：DB 层异常会让 session 变脏，直接 commit 会升级为
            # PendingRollbackError 并击穿 collect_due 整轮。
            self.session.rollback()
            run = self.session.get(CollectionRun, run.id) or run
            source = self.session.get(Source, source_id) or source
            run.status = "FAILED"
            run.error_message = str(exc)[:2000]
            run.completed_at = utc_now()
            source.last_polled_at = utc_now()
            source.next_poll_at = utc_now() + timedelta(minutes=source.poll_interval_minutes)
            source.last_error = str(exc)[:2000]
            source.error_count += 1
            self.session.add(run)
            self.session.add(source)
            self.session.commit()
            return run

    def collect_due(self) -> list[str]:
        now = utc_now()
        sources = self.session.exec(
            select(Source).where(Source.enabled == True, Source.next_poll_at <= now)  # noqa: E712
        ).all()
        collected: list[str] = []
        for source in sources:
            # 单个来源的任何故障都不得饿死同轮其它来源：collect_source 内部已按来源
            # 记录失败并推进 next_poll_at，这里只兜住它自身抛出的意外。
            try:
                self.collect_source(source.id)
                collected.append(source.id)
            except Exception:
                self.session.rollback()
        return collected

    def ingest_items(self, source_id: str, items: list[CollectedItem]) -> dict[str, int]:
        source = self.session.get(Source, source_id)
        if source is None:
            raise KeyError(source_id)
        created = 0
        filtered = 0
        for item in items:
            passed, _reason = _passes_filters(source, item, self.workflow.video_platform_domains)
            if not passed:
                filtered += 1
                continue
            created += self._ingest_item(source, item)
        return {
            "discovered": len(items),
            "created": created,
            "duplicates": len(items) - created - filtered,
            "filtered": filtered,
        }

    def ingest_agent_result(self, task: AgentTask) -> int:
        source_id = str(task.input_data["source_id"])
        source = self.session.get(Source, source_id)
        if source is None or not task.result_data:
            return 0
        created = 0
        filtered = 0
        for payload in task.result_data["candidates"]:
            item = CollectedItem(
                external_id=payload["external_id"],
                url=payload["url"],
                video_url=payload.get("video_url", ""),
                thumbnail_url=payload.get("thumbnail_url", ""),
                title=payload["title"],
                source_account=payload.get("source_account") or source.name,
                published_at=payload.get("published_at"),
                summary=payload.get("summary", ""),
                heat_score=payload.get("heat_score", 0),
                raw_metadata={"agent_discovered": True},
            )
            passed, _reason = _passes_filters(source, item, self.workflow.video_platform_domains)
            if not passed:
                filtered += 1
                continue
            created += self._ingest_item(
                source,
                item,
                collection_run_id=str(task.input_data.get("collection_run_id") or "") or None,
                rights_status=RightsStatus(
                    payload.get("rights_status", RightsStatus.OFFICIAL_REFERENCE)
                ),
                rights_note=payload.get("rights_note", ""),
            )
        now = utc_now()
        source.last_polled_at = now
        source.last_success_at = now
        _record_agent_result_schedule(source, created=created, now=now)
        source.last_error = None
        source.error_count = 0
        # 精确回填任务携带的那条 run；查不到才降级取最新 run。
        # discovery 租约期间轮询会产生 SKIPPED run，盲取最新一条会把统计写到 SKIPPED 行上。
        run = self.session.get(CollectionRun, str(task.input_data.get("collection_run_id", "")))
        if run is None:
            run = self.session.exec(
                select(CollectionRun)
                .where(CollectionRun.source_id == source.id)
                .order_by(CollectionRun.started_at.desc())
            ).first()
        if run is not None:
            discovered = len(task.result_data["candidates"])
            run.discovered_count = discovered
            run.created_count = created
            run.duplicate_count = discovered - created - filtered
            run.filtered_count = filtered
            run.status = "SUCCESS"
            run.error_code = None
            run.error_message = None
            run.completed_at = utc_now()
            self.session.add(run)
        self.session.add(source)
        self.session.commit()
        return created

    def backfill_missing_thumbnails(self, limit: int = 20) -> int:
        """One-shot deterministic OG image enrichment; failures are intentionally skipped."""
        signals = self.session.exec(
            select(CandidateSignal)
            .where(CandidateSignal.thumbnail_url == "")
            .order_by(CandidateSignal.discovered_at.desc())
            # A blocked signal should not occupy the whole batch window and
            # starve later candidates that can be enriched successfully.
            .limit(max(limit * 4, limit))
        ).all()
        updated = 0
        attempted = 0
        with httpx.Client(
            timeout=httpx.Timeout(5),
            follow_redirects=True,
            headers={"User-Agent": "GlanceThumbnailEnricher/1.0"},
        ) as client:
            for signal in signals:
                if attempted >= limit:
                    break
                if _thumbnail_backfill_blocked(signal):
                    continue
                candidate = self.session.get(Candidate, signal.candidate_id)
                if candidate is None or not candidate.source_url.startswith(
                    ("http://", "https://")
                ):
                    continue
                attempted += 1
                try:
                    image, access_denied = _resolve_thumbnail(client, candidate.source_url)
                    if not image:
                        if access_denied:
                            _mark_thumbnail_forbidden(signal)
                            self.session.commit()
                        continue
                    signal.thumbnail_url = image
                    self.session.commit()
                    updated += 1
                except (httpx.HTTPError, ValueError):
                    self.session.rollback()
        return updated

    def _schedule_agent_discovery(self, source: Source, collection_run_id: str) -> str | None:
        from glance_core.runtime_config import effective_settings

        policy = effective_settings(self.session, self.settings)
        generation = (
            self.workflow.image_generation_inputs(policy) if policy.handsfree_enabled else {}
        )
        active_tasks = self.session.exec(
            select(AgentTask).where(
                AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
                AgentTask.status.in_(_ACTIVE_DISCOVERY_STATUSES),
            )
        ).all()
        for task in active_tasks:
            if str((task.input_data or {}).get("source_id") or "") == source.id:
                return task.id

        direction_key = direction_key_for(
            source_id=source.id,
            name=source.name,
            config=source.config_json or {},
        )
        history = research_history(self.session, direction_key=direction_key)
        try:
            self.workflow.tasks.create_task(
                task_type=TaskType.SOURCE_DISCOVERY,
                instructions=resolve_instructions(self.session, TaskType.SOURCE_DISCOVERY),
                input_data={
                    "source_id": source.id,
                    "collection_run_id": collection_run_id,
                    "direction_key": direction_key,
                    "name": source.name,
                    "url": source.url,
                    "scope": source.config_json.get("scope", ""),
                    "max_candidates": min(int(source.config_json.get("max_candidates", 20)), 50),
                    "history": history,
                    **(
                        {
                            **generation,
                            "require_video": bool(source.config_json.get("require_video")),
                            "allowed_production_modes": [
                                "footage",
                                *(
                                    ["image_only"]
                                    if not source.config_json.get("require_video")
                                    and generation["asset_image_enabled"]
                                    and generation["image_only_gen_budget"] > 0
                                    else []
                                ),
                            ],
                        }
                        if policy.handsfree_enabled
                        else {}
                    ),
                },
                priority=20,
                lane_key=direction_key,
            )
        except TaskLaneBusyError as exc:
            return exc.task_id
        return None

    def _ingest_item(
        self,
        source: Source,
        item: CollectedItem,
        *,
        collection_run_id: str | None = None,
        rights_status: RightsStatus = RightsStatus.OFFICIAL_REFERENCE,
        rights_note: str = "",
    ) -> int:
        candidate, created = self.workflow.ingest_candidate(
            source_type=source.source_type.value.lower(),
            source_id=item.external_id,
            source_url=item.url,
            video_url=item.video_url,
            original_title=item.title,
            source_account=item.source_account,
            direction_key=direction_key_for(
                source_id=source.id,
                name=source.name,
                config=source.config_json or {},
            ),
            published_at=item.published_at,
            rights_status=rights_status,
            rights_note=rights_note,
        )
        signal = self.session.exec(
            select(CandidateSignal).where(
                CandidateSignal.candidate_id == candidate.id,
                CandidateSignal.source_id == source.id,
            )
        ).first()
        if signal is None:
            signal = CandidateSignal(candidate_id=candidate.id, source_id=source.id)
        signal.heat_score = item.heat_score
        if item.thumbnail_url.strip() or not signal.thumbnail_url:
            signal.thumbnail_url = item.thumbnail_url
        signal.summary = item.summary
        signal.raw_metadata = item.raw_metadata
        signal.discovered_at = utc_now()
        signal.collection_run_id = collection_run_id
        self.session.add(signal)
        self.session.commit()
        # 以 workflow 层的新建标志为准：跨源 URL 合并时本地 existing 为 None 但实际未新建。
        return int(created)

    def _finish_run(
        self,
        run: CollectionRun,
        source: Source,
        items: list[CollectedItem],
        *,
        created: int,
        filtered: int = 0,
    ) -> CollectionRun:
        now = utc_now()
        run.status = "SUCCESS"
        run.discovered_count = len(items)
        run.created_count = created
        run.filtered_count = filtered
        run.duplicate_count = len(items) - created - filtered
        run.completed_at = now
        source.last_polled_at = now
        source.last_success_at = now
        source.next_poll_at = now + timedelta(minutes=source.poll_interval_minutes)
        source.last_error = None
        source.error_count = 0
        self.session.add(run)
        self.session.add(source)
        self.session.commit()
        self.session.refresh(run)
        return run
