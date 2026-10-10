from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from calendar import timegm
from contextlib import suppress
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin, urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup
from glance_core.models import Source, SourceType
from pydantic import BaseModel, ConfigDict, Field


class CollectedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str
    url: str
    video_url: str = ""
    title: str
    source_account: str = ""
    published_at: datetime | None = None
    thumbnail_url: str = ""
    summary: str = ""
    heat_score: float = Field(default=0, ge=0, le=100)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


#: 单次采集响应体上限：唯一的 Huey worker 线程与媒体管线共享，超大响应会拖死整条流水线。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

_MARKUP_CONTENT_TYPES = {
    "text/html",
    "application/xhtml+xml",
}


def _is_markup_content_type(content_type: str) -> bool:
    """Return whether a response is safe to send through an HTML/XML parser."""
    media_type = content_type.partition(";")[0].strip().casefold()
    return not media_type or media_type in _MARKUP_CONTENT_TYPES or media_type.endswith("+xml")


def _read_bounded(response: httpx.Response) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ValueError(f"Response exceeds {MAX_RESPONSE_BYTES} bytes (declared {declared})")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError(f"Response exceeds {MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def fetch_bounded(client: httpx.Client, url: str) -> bytes:
    """流式读取并在超过上限时中止，避免恶意或异常来源耗尽内存与 worker 时间。"""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        return _read_bounded(response)


def fetch_bounded_html(client: httpx.Client, url: str) -> str | None:
    """Read a bounded markup response with replacement decoding for malformed bytes."""
    with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "")
        body = _read_bounded(response)
        encoding = response.encoding or "utf-8"
    if not _is_markup_content_type(content_type):
        return None
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


def _public_url(url: str, *, allow_private: bool = False) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Source URL must use HTTP or HTTPS")
    if allow_private:
        return url
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("Source URL resolves to a non-public address")
    return url


def _published(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        value = entry.get(key)
        if value:
            return datetime.fromtimestamp(timegm(value), tz=UTC)
    for key in ("published", "updated"):
        value = entry.get(key)
        if not value:
            continue
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
        except (TypeError, ValueError):
            continue
    return None


def _thumbnail(entry: Any) -> str:
    thumbnails = entry.get("media_thumbnail") or []
    if thumbnails and thumbnails[0].get("url"):
        return str(thumbnails[0]["url"])
    media = entry.get("media_content") or []
    if media and media[0].get("url"):
        return str(media[0]["url"])
    return ""


def _freshness_score(published_at: datetime | None, rank: int) -> float:
    rank_score = max(0.0, 100.0 - rank * 4.0)
    if published_at is None:
        return rank_score
    age_hours = max(0.0, (datetime.now(UTC) - published_at).total_seconds() / 3600)
    recency_score = max(0.0, 100.0 - age_hours / 2)
    return round(rank_score * 0.45 + recency_score * 0.55, 2)


class FeedCollector:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def collect(self, source: Source) -> list[CollectedItem]:
        source_url = _feed_url(source)
        url = _public_url(
            source_url,
            allow_private=bool(source.config_json.get("allow_private")),
        )
        parsed = feedparser.parse(fetch_bounded(self.client, url))
        if parsed.bozo and not parsed.entries:
            raise ValueError(f"Invalid feed: {parsed.bozo_exception}")
        account = str(parsed.feed.get("title") or source.name)
        results: list[CollectedItem] = []
        for rank, entry in enumerate(parsed.entries[:50]):
            link = str(entry.get("link") or "").strip()
            title = str(entry.get("title") or "").strip()
            if not link or not title:
                continue
            published_at = _published(entry)
            external_id = str(entry.get("id") or entry.get("guid") or link)
            results.append(
                CollectedItem(
                    external_id=external_id,
                    url=link,
                    title=title,
                    source_account=account,
                    published_at=published_at,
                    thumbnail_url=_thumbnail(entry),
                    summary=BeautifulSoup(
                        str(entry.get("summary") or ""),
                        "html.parser",
                    ).get_text(" ", strip=True)[:1000],
                    heat_score=_freshness_score(published_at, rank),
                    raw_metadata={
                        "rank": rank + 1,
                        "feed": url,
                        "tags": [
                            str(tag.get("term")) for tag in entry.get("tags", []) if tag.get("term")
                        ],
                    },
                )
            )
        return results


def _feed_url(source: Source) -> str:
    url = source.url.strip()
    if source.source_type == SourceType.YOUTUBE:
        if "youtube.com/feeds/videos.xml" in url:
            return url
        if url.startswith("UC"):
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={url}"
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[-2] == "channel" and parts[-1].startswith("UC"):
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={parts[-1]}"
        raise ValueError("YouTube sources require a channel ID, /channel/ URL, or feed URL")
    if source.source_type == SourceType.REDDIT and not url.endswith((".rss", ".xml")):
        return f"{url.rstrip('/')}/.rss"
    return url


class ProductSiteCollector:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def collect(self, source: Source) -> list[CollectedItem]:
        url = _public_url(
            source.url,
            allow_private=bool(source.config_json.get("allow_private")),
        )
        html = fetch_bounded_html(self.client, url)
        if html is None:
            return []
        soup = BeautifulSoup(html, "html.parser")
        alternate = soup.select_one(
            'link[rel~="alternate"][type="application/rss+xml"],'
            'link[rel~="alternate"][type="application/atom+xml"]'
        )
        if alternate and alternate.get("href"):
            feed_source = source.model_copy(update={"url": urljoin(url, alternate["href"])})
            return FeedCollector(self.client).collect(feed_source)

        results = self._json_ld_items(soup, source)
        if results:
            return results[:50]
        return self._article_links(soup, source)[:50]

    def _json_ld_items(self, soup: BeautifulSoup, source: Source) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.string or "")
            except (TypeError, json.JSONDecodeError):
                continue
            nodes = payload if isinstance(payload, list) else [payload]
            for node in nodes:
                if isinstance(node, dict) and isinstance(node.get("@graph"), list):
                    nodes.extend(node["@graph"])
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                kind = node.get("@type")
                kinds = set(kind if isinstance(kind, list) else [kind])
                if not kinds.intersection({"Product", "NewsArticle", "BlogPosting", "VideoObject"}):
                    continue
                item_url = str(node.get("url") or node.get("contentUrl") or source.url)
                title = str(node.get("name") or node.get("headline") or "").strip()
                if not title:
                    continue
                published_at = None
                date_value = node.get("datePublished") or node.get("uploadDate")
                if date_value:
                    with suppress(ValueError):
                        published_at = datetime.fromisoformat(
                            str(date_value).replace("Z", "+00:00")
                        ).astimezone(UTC)
                image = node.get("thumbnailUrl") or node.get("image") or ""
                if isinstance(image, list):
                    image = image[0] if image else ""
                external_id = hashlib.sha256(
                    f"{item_url}|{title}|{date_value or ''}".encode()
                ).hexdigest()
                items.append(
                    CollectedItem(
                        external_id=external_id,
                        url=urljoin(source.url, item_url),
                        title=title,
                        source_account=source.name,
                        published_at=published_at,
                        thumbnail_url=str(image),
                        summary=str(node.get("description") or "")[:1000],
                        heat_score=_freshness_score(published_at, len(items)),
                        raw_metadata={"schema_type": sorted(str(value) for value in kinds)},
                    )
                )
        return items

    def _article_links(self, soup: BeautifulSoup, source: Source) -> list[CollectedItem]:
        items: list[CollectedItem] = []
        seen: set[str] = set()
        for anchor in soup.select("article a[href], main article a[href]"):
            title = anchor.get_text(" ", strip=True)
            link = urljoin(source.url, str(anchor.get("href")))
            if len(title) < 8 or link in seen:
                continue
            seen.add(link)
            items.append(
                CollectedItem(
                    external_id=hashlib.sha256(link.encode()).hexdigest(),
                    url=link,
                    title=title[:300],
                    source_account=source.name,
                    heat_score=max(10.0, 80.0 - len(items) * 3),
                    raw_metadata={"discovery": "article_link"},
                )
            )
        return items


class CollectorRegistry:
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=httpx.Timeout(25, connect=10),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/138 Safari/537.36 "
                    "GlanceCollector/0.1"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "application/rss+xml,application/atom+xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> CollectorRegistry:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def collect(self, source: Source) -> list[CollectedItem]:
        if source.source_type == SourceType.PRODUCT_SITE:
            return ProductSiteCollector(self.client).collect(source)
        if source.source_type in {
            SourceType.RSS,
            SourceType.YOUTUBE,
            SourceType.RSSHUB,
            SourceType.REDDIT,
            SourceType.TRENDRADAR,
        }:
            return FeedCollector(self.client).collect(source)
        raise ValueError(f"Source type {source.source_type.value} requires an Agent")
