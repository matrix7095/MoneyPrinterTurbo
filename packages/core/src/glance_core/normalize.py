"""Deterministic URL/title normalization used for cross-source dedup."""

from __future__ import annotations

import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_KEYS = {"fbclid", "gclid", "igshid", "spm", "mc_cid", "mc_eid", "ref_src"}


def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    if not parts.netloc:
        # Local paths and opaque identifiers: lowercase only, keep them comparable.
        return url.casefold().rstrip("/")
    host = parts.netloc.casefold().removeprefix("www.")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_KEYS
        ]
    )
    return urlunsplit((parts.scheme.casefold(), host, parts.path.rstrip("/"), query, ""))


def normalize_title(title: str) -> str:
    normalized = unicodedata.normalize("NFKC", title or "")
    return "".join(
        char for char in normalized if unicodedata.category(char)[0] in {"L", "N"}
    ).casefold()
