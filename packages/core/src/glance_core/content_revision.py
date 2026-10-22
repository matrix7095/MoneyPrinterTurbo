"""Explicit current-head identity for content work; NULL preserves legacy contracts."""

from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

from sqlalchemy import and_, or_
from sqlmodel import select

from glance_core.models import Content


def current_revision_clause(model):
    """Restrict a content-owned row to its authoritative current head, including NULL."""
    head = (select(Content.current_revision_id).where(Content.id == model.content_id)
            .correlate(model).scalar_subquery())
    return or_(model.revision_id == head, and_(model.revision_id.is_(None), head.is_(None)))


def is_current_revision(session, row) -> bool:
    content = session.get(Content, row.content_id) if row.content_id else None
    return content is not None and row.revision_id == content.current_revision_id


def require_current_revision(session, row) -> None:
    if not is_current_revision(session, row):
        raise ValueError("CONTENT_REVISION_STALE")


def revision_work_root(content: Content) -> Path:
    root = Path(content.content_dir).resolve()
    return root / "revisions" / content.current_revision_id if content.current_revision_id else root


def require_reconciled_publish_intents(session, content_id):
    """A different revision never hides an uncertain external click."""
    from glance_core.models import AuditEvent, as_utc
    from glance_core.task_service import AgentTaskService, TaskConflictError, stable_hash

    latest = {}
    for event in session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "publish_intent",
    )).all():
        data = event.metadata_json
        if not isinstance(data, dict) or not isinstance(data.get("content_id"), str) or not data["content_id"]:
            raise TaskConflictError("PUBLISHED_REVISION_PUBLISH_RECONCILIATION_REQUIRED")

        if data["content_id"] != content_id:
            continue
        order = (AgentTaskService._intent_sequence(event), as_utc(event.created_at), event.id)
        if event.entity_id not in latest or order > latest[event.entity_id][0]:
            latest[event.entity_id] = (order, event)
    for _, event in latest.values():
        data = event.metadata_json
        fields = ("platform", "account_id", "content_id", "video_artifact_id", "video_sha256")
        if "content_revision_id" in data:
            fields += ("content_revision_id",)
        if (any(not isinstance(data.get(key), str) for key in fields)
                or not data.get("platform") or not data.get("account_id")
                or not (data.get("video_artifact_id") or data.get("video_sha256"))
                or stable_hash({key: data[key] for key in fields}) != event.entity_id
                or data.get("intent_key") != event.entity_id
                or AgentTaskService._intent_sequence(event) < 0
                or data.get("status") not in {"PUBLISHED", "FAILED"}):
            raise TaskConflictError("PUBLISHED_REVISION_PUBLISH_RECONCILIATION_REQUIRED")


def _douyin_url_ids(value: str) -> set[str]:
    try:
        parsed = urlsplit(value.strip())
    except ValueError:
        return set()
    if parsed.scheme.lower() not in {"http", "https"} or (parsed.hostname or "").lower().removeprefix("www.") != "douyin.com":
        return set()
    parts = unquote(parsed.path).strip("/").split("/")
    candidates = [value for key, value in parse_qsl(parsed.query) if key == "modal_id"]
    if len(parts) == 2 and parts[0] == "video":
        candidates.append(parts[1])
    return {value for value in candidates if value.isascii() and value.isdigit()}


def _publication_url(value: str, platform: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return value.strip().rstrip("/")
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return value.strip().rstrip("/")
    host = parsed.hostname.lower().removeprefix("www.")
    path = unquote(parsed.path).rstrip("/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if platform == "douyin" and host == "douyin.com":
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "video":
            query = {}  # The work ID in a canonical video path is the identity.
        elif query.get("modal_id"):
            path, query = f"/video/{query['modal_id']}", {}
    authority = host + (f":{port}" if port and port not in {80, 443} else "")
    return authority + path + ("?" + urlencode(sorted(query.items())) if query else "")


def require_distinct_publication_identity(session, content_id, revision_id, platform, external_id, external_url):
    """A new revision must have a new observed platform identity, not its old post."""
    from sqlalchemy import func
    from glance_core.models import PublicationRecord
    from glance_core.task_service import TaskConflictError

    platform = platform.strip().lower()
    external_id, external_url = external_id.strip(), external_url.strip()
    normalized_url = _publication_url(external_url, platform) if external_url else ""
    identities = ({external_id} if external_id else set()) | (_douyin_url_ids(external_url) if platform == "douyin" else set())
    records = session.exec(select(PublicationRecord).where(
        PublicationRecord.content_id == content_id,
        PublicationRecord.revision_id.is_distinct_from(revision_id),
        func.lower(PublicationRecord.platform) == platform,
    )).all()
    if any((identities.intersection(({row.external_id.strip()} if row.external_id.strip() else set())
                                   | (_douyin_url_ids(row.external_url) if platform == "douyin" else set())))
           or (normalized_url and row.external_url.strip()
               and _publication_url(row.external_url, platform) == normalized_url) for row in records):
        raise TaskConflictError("PUBLICATION_EXTERNAL_IDENTITY_ALREADY_USED_BY_ANOTHER_REVISION")
    if revision_id and platform == "douyin" and not identities:
        own = session.exec(select(PublicationRecord).where(
            PublicationRecord.content_id == content_id, PublicationRecord.revision_id == revision_id,
            func.lower(PublicationRecord.platform) == platform,
            PublicationRecord.status.in_(["PUBLISHED", "RECORDED"]),
        )).all()
        if not any(row.external_url.strip() == external_url and row.external_id.strip() == external_id for row in own):
            raise TaskConflictError("PUBLICATION_EXTERNAL_IDENTITY_UNRESOLVED")
