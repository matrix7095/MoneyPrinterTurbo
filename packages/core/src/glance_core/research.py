from __future__ import annotations

import json
from difflib import SequenceMatcher
from typing import Any

from sqlmodel import Session, col, desc, select

from glance_core.models import AgentTask, Candidate, Content, TaskStatus, TaskType
from glance_core.normalize import normalize_title, normalize_url

#: 历史上下文的字符预算：只靠条数上限撑不住体积（长标题 + 长 URL 满载可达 12K），
#: 故再加一层总量预算，保证满载时整份 input.json 仍在 5K 字符以内。
#: 按“本方向候选 > 已制作 > 历史搜索”分配，超出预算时从最旧的条目开始舍弃。
_HISTORY_CHAR_BUDGET = 3400
_HISTORY_BUDGET_SHARES = {
    "direction_candidates": 0.4,
    "already_produced": 0.3,
    "recent_searches": 0.3,
}

#: 单条字段上限：标题够人/Agent 判断是否同一项目即可，URL 保留身份识别能力。
_HISTORY_TITLE_CHARS = 80
_HISTORY_URL_CHARS = 200


def _fit_within(entries: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """按预算保留前面（最新）的条目；超出即停，不做折中截断以免产生残局 JSON。"""
    kept: list[dict[str, Any]] = []
    used = 0
    for entry in entries:
        size = len(json.dumps(entry, ensure_ascii=False))
        if kept and used + size > budget:
            break
        kept.append(entry)
        used += size
    return kept


def direction_key_for(*, source_id: str, name: str, config: dict[str, Any]) -> str:
    """Stable operator-controlled lane; scope/name are safe deterministic fallbacks."""
    raw = str(config.get("direction_key") or config.get("scope") or name).strip()
    return normalize_title(raw)[:120] or f"source{normalize_title(source_id)[:80]}"


def research_history(
    session: Session,
    *,
    direction_key: str,
    limit: int = 15,
) -> dict[str, list[dict[str, Any]]]:
    """搜索任务的历史上下文：只给查重必需的最小字段，控制 input.json 体积。"""
    direction_rows = session.exec(
        select(Candidate)
        .where(Candidate.direction_key == direction_key)
        .order_by(desc(Candidate.created_at))
        .limit(limit)
    ).all()
    produced_rows = session.exec(
        select(Content, Candidate)
        .where(Content.candidate_id == Candidate.id)
        .order_by(desc(Content.created_at))
        .limit(limit)
    ).all()
    recent_searches = session.exec(
        select(AgentTask)
        .where(
            AgentTask.task_type == TaskType.SOURCE_DISCOVERY,
            AgentTask.status == TaskStatus.COMPLETED,
        )
        .order_by(desc(AgentTask.completed_at))
        .limit(limit * 3)
    ).all()
    return {
        "direction_candidates": _fit_within(
            [
                {
                    "title": (row.title_zh or row.original_title)[:_HISTORY_TITLE_CHARS],
                    "url": row.source_url[:_HISTORY_URL_CHARS],
                    "status": row.status.value,
                }
                for row in direction_rows
            ],
            int(_HISTORY_CHAR_BUDGET * _HISTORY_BUDGET_SHARES["direction_candidates"]),
        ),
        "already_produced": _fit_within(
            [
                {
                    "title": content.title[:_HISTORY_TITLE_CHARS],
                    "url": candidate.source_url[:_HISTORY_URL_CHARS],
                    "status": content.business_status.value,
                }
                for content, candidate in produced_rows
            ],
            int(_HISTORY_CHAR_BUDGET * _HISTORY_BUDGET_SHARES["already_produced"]),
        ),
        "recent_searches": _fit_within(
            [
                {
                    "task_id": task.id,
                    "completed_at": task.completed_at.isoformat() if task.completed_at else None,
                    "notes": str((task.result_data or {}).get("notes") or "")[:200],
                }
                for task in recent_searches
                if task.input_data.get("direction_key") == direction_key
            ][:limit],
            int(_HISTORY_CHAR_BUDGET * _HISTORY_BUDGET_SHARES["recent_searches"]),
        ),
    }


def find_similar_projects(
    session: Session,
    *,
    query: str,
    url: str = "",
    direction_key: str = "",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return lexical evidence only; the external Agent makes the semantic judgment."""
    normalized_query = normalize_title(query)
    normalized_url = normalize_url(url)
    rows = session.exec(select(Candidate).order_by(desc(Candidate.created_at)).limit(1500)).all()
    content_by_candidate = {
        content.candidate_id: content
        for content in session.exec(
            select(Content).where(col(Content.candidate_id).in_([row.id for row in rows]))
        ).all()
    }
    matches: list[tuple[float, Candidate, list[str]]] = []
    for row in rows:
        title = row.title_zh or row.original_title
        candidate_title = normalize_title(title)
        reasons: list[str] = []
        score = 0.0
        if normalized_url and normalize_url(row.source_url) == normalized_url:
            score = 1.0
            reasons.append("same_url")
        if normalized_query and candidate_title:
            ratio = SequenceMatcher(None, normalized_query, candidate_title).ratio()
            if normalized_query in candidate_title or candidate_title in normalized_query:
                ratio = max(ratio, 0.92)
                reasons.append("title_contains")
            if ratio >= 0.35:
                score = max(score, ratio)
                reasons.append("title_lexical")
        if direction_key and row.direction_key == direction_key:
            score += 0.05
            reasons.append("same_direction")
        if score >= 0.35:
            matches.append((min(score, 1.0), row, reasons))
    matches.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
    result = []
    for score, candidate, reasons in matches[:limit]:
        content = content_by_candidate.get(candidate.id)
        result.append(
            {
                "candidate_id": candidate.id,
                "title": candidate.title_zh or candidate.original_title,
                "original_title": candidate.original_title,
                "url": candidate.source_url,
                "candidate_status": candidate.status.value,
                "direction_key": candidate.direction_key,
                "content_id": content.id if content else None,
                "content_status": content.business_status.value if content else None,
                "lexical_score": round(score, 3),
                "reasons": reasons,
                "created_at": candidate.created_at.isoformat(),
            }
        )
    return result
