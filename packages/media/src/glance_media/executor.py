from __future__ import annotations

import hashlib
import html
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
from array import array
from datetime import UTC, datetime
from http.cookiejar import MozillaCookieJar
from numbers import Real
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

import httpx
import pysubs2
from scenedetect import ContentDetector, detect
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, UnsupportedError

from glance_media.bgm import mix_filter_complex
from glance_media.render_plan import RenderPlan, SourceRedaction
from glance_media.tts import TtsResult, get_tts_provider

logger = logging.getLogger(__name__)
_BOUNDARY_QUANTIZATION_TOLERANCE_SEC = 0.001
_BOUNDARY_OVERLAP_TOLERANCE_SEC = 0.05
_MIN_RENDER_SEGMENT_DURATION_SEC = 0.05
_SUBTITLE_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[._'\u2019/-][A-Za-z0-9]+)*|[^\s]")
_SUBTITLE_BREAK_CHARS = frozenset("，。！？；、,.!?;")


class NonRetryableMediaError(RuntimeError):
    """确定性失败：重试不会改变结果，调用方不应重试、不烧 huey attempt。

    消息就是给人看的中文文案，可直接写入 content.error_message。
    """


def missing_media_tools(*names: str) -> list[str]:
    """返回 PATH 中缺失的工具名；用于失败前置，避免深层英文报错。"""
    return [name for name in names if shutil.which(name) is None]


def _is_subtitle_rate_limit(message: str) -> bool:
    """只有字幕/限流类错误才值得去字幕重下；ffmpeg 缺失等合并错误直接上抱。"""
    lowered = message.casefold()
    return any(
        pattern in lowered for pattern in ("subtitle", "429", "too many requests", "rate limit")
    )


#: yt-dlp 确定性失败特征：视频已删除/私有/地区限制，重试必然同样失败。
#: 地区限制用短特征：真实文案有 "not available in your country" 与
#: "has not made this video available in your country" 两种变体。
_YTDLP_GONE_PATTERNS = (
    "video unavailable",
    "private video",
    "has been removed",
    "available in your country",
)


def _is_video_gone(message: str) -> bool:
    lowered = message.casefold()
    return any(pattern in lowered for pattern in _YTDLP_GONE_PATTERNS)


_KICKSTARTER_PAGE_MAX_BYTES = 12 * 1024 * 1024
_DIRECT_MEDIA_MAX_BYTES = 2 * 1024 * 1024 * 1024
_DOWNLOAD_VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mkv", ".webm", ".mov"})
_DEFAULT_MEDIA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_DIRECT_MP4_SUFFIXES = frozenset({".mp4", ".m4v"})
_DOWNLOAD_RESIDUAL_SUFFIXES = _DOWNLOAD_VIDEO_SUFFIXES | {".part", ".ytdl", ".json"}
_KICKSTARTER_MEDIA_PATTERN = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:mp4|m3u8)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)


def _is_kickstarter_url(url: str) -> bool:
    """Return whether *url* is a Kickstarter project page."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return (
        parsed.scheme in {"http", "https"}
        and (hostname == "kickstarter.com" or hostname.endswith(".kickstarter.com"))
        and parsed.path.casefold().startswith("/projects/")
    )


def _is_youtube_url(url: str) -> bool:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme in {"http", "https"} and (
        hostname == "youtu.be" or hostname == "youtube.com" or hostname.endswith(".youtube.com")
    )


def _is_youtube_forbidden_error(url: str, message: str) -> bool:
    if not _is_youtube_url(url):
        return False
    lowered = message.casefold()
    return "403" in lowered or "forbidden" in lowered


def is_direct_media_url(url: str) -> bool:
    """Return whether *url* looks like a real media file or HLS playlist."""
    parsed = urlparse(url)
    suffix = Path(unquote(parsed.path)).suffix.casefold()
    return parsed.scheme in {"http", "https"} and suffix in {
        ".mp4",
        ".m4v",
        ".webm",
        ".mkv",
        ".mov",
        ".m3u8",
    }


def _is_direct_mp4_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and Path(unquote(parsed.path)).suffix.casefold() in _DIRECT_MP4_SUFFIXES
    )


def _extract_kickstarter_video_urls(page: str) -> list[str]:
    """Extract current Kickstarter MP4/HLS URLs without depending on page markup."""
    # Kickstarter embeds the video object in HTML-escaped JSON. Normalize only
    # the escaping forms used by JSON/HTML so signed query parameters survive.
    normalized = html.unescape(page)
    normalized = (
        normalized.replace("\\/", "/")
        .replace("\\u0026", "&")
        .replace("\\u002f", "/")
        .replace("\\u003d", "=")
    )
    normalized = unquote(normalized)
    candidates = {
        html.unescape(match).replace("\\/", "/")
        for match in _KICKSTARTER_MEDIA_PATTERN.findall(normalized)
    }

    def sort_key(url: str) -> tuple[int, str]:
        lowered = url.casefold()
        if ".mp4" in lowered and "h264_high" in lowered:
            return 0, url
        if ".mp4" in lowered and "h264_base" in lowered:
            return 1, url
        if lowered.endswith(".mp4") or ".mp4?" in lowered:
            return 2, url
        return 3, url

    return sorted(candidates, key=sort_key)


def _load_cookie_jar(cookies_file: Path | None) -> MozillaCookieJar:
    jar = MozillaCookieJar()
    if cookies_file is not None and cookies_file.is_file():
        try:
            jar.load(str(cookies_file), ignore_discard=True, ignore_expires=True)
        except (OSError, ValueError) as exc:
            raise NonRetryableMediaError(
                "Kickstarter Cookies 文件无法读取，请重新导出 Netscape cookies.txt"
            ) from exc
    return jar


def _httpx_cookies(jar: MozillaCookieJar) -> httpx.Cookies:
    cookies = httpx.Cookies()
    for cookie in jar:
        if cookie.domain:
            cookies.set(
                cookie.name,
                cookie.value,
                domain=cookie.domain,
                path=cookie.path or "/",
            )
        else:
            cookies.set(cookie.name, cookie.value, path=cookie.path or "/")
    return cookies


def _media_headers(source_page_url: str | None, user_agent: str | None) -> dict[str, str]:
    headers = {
        "Accept": "video/mp4,video/*;q=0.9,application/octet-stream;q=0.8,*/*;q=0.1",
        "User-Agent": (user_agent or "").strip() or _DEFAULT_MEDIA_USER_AGENT,
    }
    if source_page_url:
        headers["Referer"] = source_page_url
    return headers


def _fetch_kickstarter_page(
    url: str,
    cookies_file: Path | None,
    user_agent: str | None = None,
) -> str:
    """Fetch a Kickstarter project page with an optional Netscape cookie jar."""
    jar = _load_cookie_jar(cookies_file)

    opener = build_opener(HTTPCookieProcessor(jar))
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8,zh-CN;q=0.6",
            "Referer": "https://www.kickstarter.com/",
            "User-Agent": (user_agent or "").strip() or _DEFAULT_MEDIA_USER_AGENT,
        },
    )
    try:
        with opener.open(request, timeout=30) as response:
            body = response.read(_KICKSTARTER_PAGE_MAX_BYTES + 1)
            if len(body) > _KICKSTARTER_PAGE_MAX_BYTES:
                raise NonRetryableMediaError("Kickstarter 项目页超过安全读取上限，已停止解析")
            charset = response.headers.get_content_charset() or "utf-8"
            return body.decode(charset, errors="replace")
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise NonRetryableMediaError(
                "Kickstarter 项目页拒绝访问（HTTP 403/401）：请更新该站 Cookies，"
                "并确认当前网络出口没有触发风控"
            ) from exc
        if 400 <= exc.code < 500:
            raise NonRetryableMediaError(
                f"Kickstarter 项目页不可用（HTTP {exc.code}），请检查项目链接"
            ) from exc
        raise RuntimeError(f"Kickstarter 项目页暂时无法访问（HTTP {exc.code}）") from exc
    except (TimeoutError, URLError, OSError) as exc:
        raise RuntimeError("Kickstarter 项目页网络请求失败，稍后可重试") from exc


#: ffmpeg stderr 中的确定性错误特征：能力缺失/参数错误，重试不会改变结果。
_FFMPEG_DETERMINISTIC_PATTERNS = (
    "No such filter",
    "Unknown encoder",
    "Invalid argument",
    "Filter not found",
)

#: 报错降噪：优先截取含这些标记的关键行，避免 banner + 探测信息淹没根因。
_STDERR_KEY_MARKERS = ("Error", "No such", "Invalid", "Unknown encoder", "Filter not found")


def _stderr_summary(stderr: str) -> str:
    """从 stderr 中提取关键错误行；提不到时降级为末尾原文兑底。"""
    key_lines = [
        line.strip()
        for line in stderr.splitlines()
        if any(marker in line for marker in _STDERR_KEY_MARKERS)
    ]
    if key_lines:
        return " | ".join(key_lines[-5:])
    return stderr[-2000:]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(args: list[str], *, cwd: Path, timeout: int = 600) -> None:
    if Path(args[0]).stem == "ffmpeg" and "-hide_banner" not in args:
        # 报错降噪单点：全部 ffmpeg 调用统一去 banner，stderr 只剩真报错。
        args = [args[0], "-hide_banner", *args[1:]]
    completed = subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )
    if completed.returncode:
        stderr = completed.stderr or ""
        summary = _stderr_summary(stderr)
        name = Path(args[0]).name
        if any(pattern in stderr for pattern in _FFMPEG_DETERMINISTIC_PATTERNS):
            # 能力缺失/参数错误属确定性失败：不烧 huey 重试、一次即落库转人工。
            # （仍是 RuntimeError 子类，NVENC 编码器类错误不会绕过软编回退。）
            raise NonRetryableMediaError(f"{name} 确定性失败（{completed.returncode}）：{summary}")
        raise RuntimeError(f"{name} failed ({completed.returncode}): {summary}")


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        shell=False,
    )
    return json.loads(completed.stdout)


def _finite_seconds(value: Any, *, label: str) -> float:
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"{label} must be a finite number")
    return seconds


def _clamp_to_duration(value: float, duration: float, *, label: str) -> float:
    if value <= duration:
        return value
    if value - duration <= _BOUNDARY_QUANTIZATION_TOLERANCE_SEC:
        return duration
    raise ValueError(f"{label} exceeds source video duration")


def _select_timing_boundaries(
    boundaries: list[dict[str, Any]],
    duration: float,
    *,
    required_internal_edges: int = 0,
) -> list[dict[str, Any]]:
    """Prefer enough valid sentence timing, then valid word timing."""
    typed = [item for item in boundaries if isinstance(item, dict)]
    sentence = [item for item in typed if item.get("kind") == "SentenceBoundary"]
    word = [item for item in typed if item.get("kind") == "WordBoundary"]
    legacy = [item for item in typed if "kind" not in item]
    if not math.isfinite(duration) or duration <= 0 or required_internal_edges < 0:
        return []
    fallback: list[dict[str, Any]] = []
    for selected in (sentence, word, legacy):
        if not selected:
            continue
        result: list[dict[str, Any]] = []
        previous_end = 0.0
        for item in selected:
            if not str(item.get("text") or "").strip():
                result = []
                break
            try:
                start, end = float(item["start_sec"]), float(item["end_sec"])
            except (KeyError, TypeError, ValueError):
                result = []
                break
            if (
                not math.isfinite(start)
                or not math.isfinite(end)
                or start < -_BOUNDARY_QUANTIZATION_TOLERANCE_SEC
                or end <= start
                or end > duration + _BOUNDARY_QUANTIZATION_TOLERANCE_SEC
            ):
                result = []
                break
            start, end = max(0.0, start), min(duration, end)
            if (
                result
                and abs(start - result[-1]["start_sec"]) <= _BOUNDARY_QUANTIZATION_TOLERANCE_SEC
                and abs(end - result[-1]["end_sec"]) <= _BOUNDARY_QUANTIZATION_TOLERANCE_SEC
            ):
                continue
            if result and start < previous_end:
                overlap = previous_end - start
                if overlap > _BOUNDARY_OVERLAP_TOLERANCE_SEC + 1e-9:
                    result = []
                    break
                start = previous_end
            start, end = max(start, previous_end), max(end, previous_end)
            if end <= start:
                result = []
                break
            result.append({**item, "start_sec": round(start, 6), "end_sec": round(end, 6)})
            previous_end = end
        if result and not fallback:
            fallback = result
        if len(result) > required_internal_edges:
            return result
    return fallback


def _calibrate_render_plan(
    plan_data: dict[str, Any],
    boundaries: list[dict[str, Any]],
    voice_duration: float,
) -> dict[str, Any]:
    """Calibrate only this render copy; return the original timing on unsafe input."""
    raw_segments = plan_data.get("segments")
    if not isinstance(raw_segments, list) or not all(
        isinstance(item, dict) for item in raw_segments
    ):
        return dict(plan_data)
    calibrated = {**plan_data, "segments": [dict(segment) for segment in raw_segments]}
    if not boundaries or not math.isfinite(voice_duration) or voice_duration <= 0:
        return calibrated

    try:
        segment_times = [
            (float(segment["narration_start_sec"]), float(segment["narration_end_sec"]))
            for segment in raw_segments
        ]
    except (KeyError, TypeError, ValueError):
        return calibrated
    if any(
        not math.isfinite(start) or not math.isfinite(end) or end <= start
        for start, end in segment_times
    ):
        return calibrated
    targets = [
        round((segment_times[index][0] + segment_times[index - 1][1]) / 2, 6)
        for index in range(1, len(segment_times))
    ]
    if any(
        abs(segment_times[index][0] - segment_times[index - 1][1])
        > _BOUNDARY_QUANTIZATION_TOLERANCE_SEC
        for index in range(1, len(segment_times))
    ):
        return calibrated

    if len(raw_segments) == 1:
        if voice_duration < _MIN_RENDER_SEGMENT_DURATION_SEC:
            return calibrated
        calibrated["segments"][0]["narration_start_sec"] = 0.0
        calibrated["segments"][0]["narration_end_sec"] = voice_duration
        return calibrated

    points: list[float] = []
    for boundary in boundaries[1:]:
        point = boundary["start_sec"]
        if (
            _MIN_RENDER_SEGMENT_DURATION_SEC
            <= point
            <= voice_duration - _MIN_RENDER_SEGMENT_DURATION_SEC
            and (not points or point - points[-1] > _BOUNDARY_QUANTIZATION_TOLERANCE_SEC)
        ):
            points.append(point)
    if len(points) < len(targets):
        return calibrated
    selected: list[float] = []
    next_point = 0
    previous = 0.0
    for index, target in enumerate(targets):
        remaining = len(targets) - index - 1
        available = [
            (point, point_index)
            for point_index, point in enumerate(points[next_point:], start=next_point)
            if point >= previous + _MIN_RENDER_SEGMENT_DURATION_SEC
            and voice_duration - point >= (remaining + 1) * _MIN_RENDER_SEGMENT_DURATION_SEC
            and len(points) - point_index - 1 >= remaining
        ]
        if not available:
            return calibrated
        point, point_index = min(available, key=lambda item: (abs(item[0] - target), item[0]))
        selected.append(point)
        previous, next_point = point, point_index + 1
    endpoints = [0.0, *selected, voice_duration]
    if any(
        endpoints[index + 1] - endpoints[index] < _MIN_RENDER_SEGMENT_DURATION_SEC
        for index in range(len(endpoints) - 1)
    ):
        return calibrated
    for index, segment in enumerate(calibrated["segments"]):
        segment["narration_start_sec"] = endpoints[index]
        segment["narration_end_sec"] = endpoints[index + 1]
    return calibrated


def _promote_render_output(temporary: Path, output: Path) -> None:
    """Validate a rendered MP4 before replacing the last known-good output."""
    try:
        if not temporary.is_file():
            raise RuntimeError(f"ffmpeg 未生成渲染产物：{temporary.name}")
        metadata = probe(temporary)
        if not isinstance(metadata.get("format"), dict):
            raise RuntimeError(f"渲染产物缺少可读媒体元数据：{temporary.name}")
        temporary.replace(output)
    except Exception as exc:
        temporary.unlink(missing_ok=True)
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(f"渲染产物校验失败：{temporary.name}: {exc}") from exc


#: 进程内缓存的 ffmpeg 滤镜清单；None 表示尚未探测成功。
_ffmpeg_filter_cache: frozenset[str] | None = None


def available_ffmpeg_filters() -> frozenset[str]:
    """探测本机 ffmpeg 支持的滤镜名（进程内缓存）；探测失败返回空集且不缓存。"""
    global _ffmpeg_filter_cache
    if _ffmpeg_filter_cache is not None:
        return _ffmpeg_filter_cache
    try:
        completed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names: set[str] = set()
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        # 滤镜行第三列是 IO 声明（如 V->V、A->A），表头/图例行没有。
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    _ffmpeg_filter_cache = frozenset(names)
    return _ffmpeg_filter_cache


def ensure_ass_filter() -> None:
    """渲染前置自检：ass 滤镜缺失时直接给修复指引，不进入 ffmpeg 深处报退出码 8。"""
    filters = available_ffmpeg_filters()
    if filters and "ass" not in filters:
        raise NonRetryableMediaError(
            "ffmpeg 缺少 ass 字幕滤镜，无法烧录字幕：请安装含 libass 的完整构建"
            "（编译参数含 --enable-libass）并置于 Worker 的 PATH 前列"
        )


#: 包内字体是字幕与封面的唯一字体来源，渲染结果不依赖宿主系统字体。
#: libass 会扫描 fontsdir 下的每个目录项；许可证放在扫描目录之外。
FONT_DIR = Path(__file__).with_name("fonts")
FONT_ASSET_DIR = FONT_DIR / "assets"
FONT_FILES = (
    FONT_ASSET_DIR / "NotoSansSC-Regular.otf",
    FONT_ASSET_DIR / "NotoSansSC-Bold.otf",
)

#: 单行字幕最大字数（竖屏 1080 宽、68px 字号下的安全值）。
_SUBTITLE_LINE_CHARS = 14

_SUBTITLE_THEME: dict[str, dict[str, Any]] = {
    "Default": {
        "fontname": "Noto Sans SC",
        "fontsize": 68,
        "bold": True,
        "primarycolor": pysubs2.Color(255, 255, 255),
        "outlinecolor": pysubs2.Color(0, 0, 0),
        "backcolor": pysubs2.Color(0, 0, 0, 102),
        "borderstyle": 1,
        "outline": 4,
        "shadow": 2,
        "spacing": 2,
        "alignment": 2,
        "marginv": 380,
    },
}


def ffmpeg_filter_path(path: Path) -> str:
    """Escape a filesystem path embedded in an FFmpeg filter expression."""
    return path.as_posix().replace(":", "\\:")


def hard_wrap(text: str, per_line: int) -> str:
    r"""按字数插入 ASS 硬换行 \N。

    libass 的智能折行按空格断句，中文没有空格因此整行不折、直接从画面两侧溢出，
    所以宽度必须由我们自己算。
    """
    if per_line <= 0 or len(text) <= per_line:
        return text
    lines = [text[index : index + per_line] for index in range(0, len(text), per_line)]
    return r"\N".join(lines)


def _join_boundary_line(words: list[dict[str, Any]], duration: float) -> tuple[float, float, str]:
    return (
        float(words[0]["start_sec"]),
        min(float(words[-1]["end_sec"]), duration),
        _join_boundary_text(words),
    )


def _join_boundary_text(words: list[dict[str, Any]]) -> str:
    text = ""
    for item in words:
        piece = str(item["text"]).strip()
        if not piece:
            continue
        if (
            text
            and text[-1].isascii()
            and text[-1].isalnum()
            and piece[0].isascii()
            and piece[0].isalnum()
        ):
            text += " "
        text += piece
    return text


def _subtitle_units(text: str) -> list[str]:
    units: list[str] = []
    cursor = 0
    for match in _SUBTITLE_TOKEN_RE.finditer(text):
        units.append(text[cursor : match.start()] + match.group())
        cursor = match.end()
    return units


def _subtitle_visible_length(text: str) -> int:
    return sum(not char.isspace() for char in text)


def _subtitle_parts(text: str) -> list[str]:
    """Wrap subtitles without splitting ASCII/number tokens."""
    parts: list[str] = []
    buffer = ""
    visible_length = 0
    for unit in _subtitle_units(text):
        token = unit.strip()
        if not token:
            continue
        unit_length = _subtitle_visible_length(unit)
        punctuation = token[-1] in _SUBTITLE_BREAK_CHARS
        if buffer and visible_length + unit_length > _SUBTITLE_LINE_CHARS and not punctuation:
            parts.append(buffer.strip())
            buffer = ""
            visible_length = 0
        buffer += unit
        visible_length += unit_length
        if (
            punctuation and visible_length >= _SUBTITLE_LINE_CHARS // 2
        ) or visible_length >= _SUBTITLE_LINE_CHARS:
            parts.append(buffer.strip())
            buffer = ""
            visible_length = 0
    if buffer.strip():
        parts.append(buffer.strip())
    if len(parts) > 1 and _subtitle_visible_length(parts[-1]) <= 3:
        units = _subtitle_units(_join_boundary_text([{"text": part} for part in parts[-2:]]))
        total = sum(_subtitle_visible_length(unit) for unit in units)
        length = 0
        choices = []
        for index, unit in enumerate(units[:-1], 1):
            length += _subtitle_visible_length(unit)
            if max(length, total - length) <= _SUBTITLE_LINE_CHARS:
                choices.append((abs(length - total / 2), index))
        if choices:
            _, split = min(choices)
            parts[-2:] = ["".join(units[:split]).strip(), "".join(units[split:]).strip()]
    return parts


def _split_boundary(boundary: dict[str, Any], duration: float) -> list[tuple[float, float, str]]:
    """把超长边界按标点和不可拆分 token 切成多行，按字数分配时间。"""
    text = str(boundary["text"])
    start = max(0.0, float(boundary["start_sec"]))
    end = min(max(start, float(boundary["end_sec"])), duration)
    if end <= start:
        return []
    parts = _subtitle_parts(text) or [text.strip()]
    span = end - start
    total = sum(max(_subtitle_visible_length(part), 1) for part in parts) or 1
    events: list[tuple[float, float, str]] = []
    elapsed = start
    for index, part in enumerate(parts):
        part_end = (
            end
            if index == len(parts) - 1
            else min(end, elapsed + span * _subtitle_visible_length(part) / total)
        )
        events.append((elapsed, part_end, part.strip()))
        elapsed = part_end
    return events


def layout_filter(
    layout: str,
    *,
    motion: str = "hold",
    focus_x: float = 0.5,
    focus_y: float = 0.5,
) -> str:
    if layout == "fit":
        return (
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,fps=30"
        )
    if layout == "image_card":
        # 图文模式不能把单张图当作静态背景整段循环。这里先裁成竖屏，再用
        # zoompan 生成可见的缩放/平移；字幕仍在后续的 ASS 阶段统一烧录。
        safe_x = max(0.0, min(1.0, focus_x))
        safe_y = max(0.0, min(1.0, focus_y))
        if motion == "ken_burns_out":
            zoom = "max(1,1.14-on*0.0005)"
            x_expr = f"(iw-iw/zoom)*{safe_x:.3f}"
            y_expr = f"(ih-ih/zoom)*{safe_y:.3f}"
        elif motion == "pan_left":
            zoom = "1.10"
            x_expr = "min(iw-iw/zoom,(iw-iw/zoom)*on/240)"
            y_expr = f"(ih-ih/zoom)*{safe_y:.3f}"
        elif motion == "pan_right":
            zoom = "1.10"
            x_expr = "max(0,(iw-iw/zoom)*(1-on/240))"
            y_expr = f"(ih-ih/zoom)*{safe_y:.3f}"
        elif motion == "focus_product":
            zoom = "min(1.18,1+on*0.00075)"
            x_expr = f"(iw-iw/zoom)*{safe_x:.3f}"
            y_expr = f"(ih-ih/zoom)*{safe_y:.3f}"
        elif motion == "hold":
            zoom = "1"
            x_expr = "(iw-iw/zoom)*0.5"
            y_expr = "(ih-ih/zoom)*0.5"
        else:
            zoom = "min(1.14,1+on*0.0005)"
            x_expr = f"(iw-iw/zoom)*{safe_x:.3f}"
            y_expr = f"(ih-ih/zoom)*{safe_y:.3f}"
        return (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,"
            f"zoompan=z='{zoom}':x='{x_expr}':y='{y_expr}':d=1:s=1080x1920:fps=30"
        )
    if layout == "blur_background":
        return (
            "split=2[background][foreground];"
            "[background]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=24:12[background];"
            "[foreground]scale=1080:1920:force_original_aspect_ratio=decrease"
            "[foreground];"
            "[background][foreground]overlay=(W-w)/2:(H-h)/2,fps=30"
        )
    return "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30"


def source_redaction_filter(
    redactions: list[SourceRedaction], *, source_width: int, source_height: int
) -> str:
    """Build fixed source-coordinate masks before any output layout transform."""
    if not redactions:
        return ""
    if source_width <= 0 or source_height <= 0:
        raise ValueError("Source media dimensions are required for source_redactions")
    filters: list[str] = []
    for redaction in redactions:
        if redaction.mode != "black":
            raise ValueError(f"Unsupported source redaction mode: {redaction.mode}")
        left = max(0, min(source_width - 1, math.floor(redaction.x * source_width)))
        top = max(0, min(source_height - 1, math.floor(redaction.y * source_height)))
        right = min(
            source_width,
            max(left + 1, math.ceil((redaction.x + redaction.width) * source_width)),
        )
        bottom = min(
            source_height,
            max(top + 1, math.ceil((redaction.y + redaction.height) * source_height)),
        )
        filters.append(
            f"drawbox=x={left}:y={top}:w={right - left}:h={bottom - top}:color=black:t=fill"
        )
    return ",".join(filters)


def _with_transition(video_filter: str, transition: str) -> str:
    """Apply the small deterministic transition supported by the renderer.

    ``hard_cut`` remains the default so existing footage plans keep their
    current output.  ``short_fade`` is intentionally implemented as a short
    fade-in on each segment: it is robust with the concat demuxer and avoids
    introducing a second render graph just for a cosmetic effect.
    """
    if transition == "short_fade":
        return f"{video_filter},fade=t=in:st=0:d=0.18"
    return video_filter


def _encoder_args(encoder: str) -> list[str]:
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast"]


#: 网页原图下载防护：与采集侧 fetch_bounded 同数量级的上限与最小可用尺寸。
MAX_SOURCE_IMAGE_BYTES = 8 * 1024 * 1024
MIN_SOURCE_IMAGE_EDGE = 500
_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/avif": ".avif",
}


def _reject_private_host(url: str) -> None:
    """SSRF 防护：只允许 http/https 且解析到公网地址的主机。"""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("图片地址必须是 http/https")
    for info in socket.getaddrinfo(parsed.hostname, None):
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
        ):
            raise ValueError("图片地址解析到非公网地址")


def _fetch_image(client: httpx.Client, url: str) -> tuple[bytes, str]:
    """流式读取并在超过上限时中止；返回图片字节与文件后缀。"""
    _reject_private_host(url)
    with client.stream("GET", url) as response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        extension = _IMAGE_EXTENSIONS.get(content_type)
        if extension is None:
            raise ValueError(f"非图片响应（content-type={content_type or 'unknown'}）")
        declared = response.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_SOURCE_IMAGE_BYTES:
            raise ValueError(f"图片超过 {MAX_SOURCE_IMAGE_BYTES} 字节")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_SOURCE_IMAGE_BYTES:
                raise ValueError(f"图片超过 {MAX_SOURCE_IMAGE_BYTES} 字节")
            chunks.append(chunk)
    return b"".join(chunks), extension


def _stream_dimensions(metadata: dict[str, Any]) -> tuple[int, int]:
    streams = metadata.get("streams", [])
    if not isinstance(streams, list):
        return 0, 0
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        try:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if width and height:
            return width, height
    return 0, 0


def _image_dimensions(path: Path) -> tuple[int, int]:
    """用 ffprobe 读图片尺寸（不引入新的图像库）；读不出时返回 0。"""
    try:
        return _stream_dimensions(probe(path))
    except (RuntimeError, OSError, KeyError, ValueError):
        return 0, 0


def _transcript_time(seconds: float) -> str:
    value = max(0.0, float(seconds))
    minutes = int(value // 60)
    return f"{minutes:02d}:{value - minutes * 60:05.2f}"


def pack_transcript(transcript: dict[str, Any], *, silence_threshold: float = 0.4) -> str:
    lines = ["# Packed transcript"]
    previous_end: float | None = None
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        text = " ".join(str(segment.get("text") or "").split())
        if not text:
            continue
        try:
            start = max(0.0, float(segment.get("start_sec", segment.get("start"))))
            end = max(start, float(segment.get("end_sec", segment.get("end", start))))
        except (TypeError, ValueError):
            continue
        if previous_end is not None and start - previous_end >= silence_threshold:
            lines.append(f"  [silence {start - previous_end:.2f}s]")
        lines.append(f"  [{_transcript_time(start)}-{_transcript_time(end)}] {text}")
        previous_end = max(previous_end or 0.0, end)
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _clamp_review_timestamp(duration: float, timestamp: float) -> float:
    return min(max(0.0, timestamp), max(0.0, duration - 0.05))


_DURATION_TOLERANCE_SEC = 0.5


def _review_frame_specs(
    duration: float,
    count: int,
    plan_data: dict[str, Any] | None = None,
) -> list[tuple[str, float]]:
    safe_duration = max(0.0, float(duration))
    count = max(1, count)
    specs = [
        (
            f"frame-{index + 1:02d}.jpg",
            _clamp_review_timestamp(
                safe_duration,
                safe_duration * (index + 1) / (count + 1),
            ),
        )
        for index in range(count)
    ]
    segments = plan_data.get("segments") if isinstance(plan_data, dict) else None
    if not isinstance(segments, list) or not segments or safe_duration <= 0:
        return specs

    edge = min(0.5, safe_duration / 2)
    specs.extend(
        [
            (f"first-{edge:.2f}s.jpg", _clamp_review_timestamp(safe_duration, edge)),
            (
                f"last-{max(0.0, safe_duration - edge):.2f}s.jpg",
                _clamp_review_timestamp(safe_duration, safe_duration - edge),
            ),
        ]
    )
    output_offset = 0.0
    for index, segment in enumerate(segments[:-1][:24], start=1):
        if not isinstance(segment, dict):
            continue
        try:
            segment_duration = float(segment["narration_end_sec"]) - float(
                segment["narration_start_sec"]
            )
        except (KeyError, TypeError, ValueError):
            continue
        if segment_duration <= 0:
            continue
        output_offset += segment_duration
        if not 0 < output_offset < safe_duration:
            continue
        before = _clamp_review_timestamp(safe_duration, output_offset - 0.5)
        after = _clamp_review_timestamp(safe_duration, output_offset + 0.5)
        specs.extend(
            [
                (f"cut-{index:02d}-before-{before:05.2f}s.jpg", before),
                (f"cut-{index:02d}-after-{after:05.2f}s.jpg", after),
            ]
        )
    return specs


class MediaExecutor:
    def __init__(self, content_dir: Path) -> None:
        self.root = content_dir.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _write_source_info(
        self,
        destination: Path,
        *,
        source_url: str,
        source_type: str,
        source_page_url: str | None,
        method: str,
        mime_type: str | None = None,
    ) -> None:
        """Persist stable provenance without retaining signed CDN credentials."""
        stable_source_page = (source_page_url or "").strip()
        if is_direct_media_url(stable_source_page):
            stable_source_page = ""
        if not stable_source_page and not is_direct_media_url(source_url):
            stable_source_page = source_url.strip()
        payload = {
            "source_page_url": stable_source_page,
            "source_type": source_type,
            "materialization_method": method,
            "materialized_at": datetime.now(UTC).isoformat(),
            "sha256": sha256_file(destination),
            "size_bytes": destination.stat().st_size,
            "mime_type": mime_type or mimetypes.guess_type(destination.name)[0] or "video/mp4",
        }
        (destination.parent / "info.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _discard_download_candidates(source_dir: Path, prefix: str) -> None:
        """Remove stale video candidates and yt-dlp residue from a prior attempt."""
        for path in source_dir.glob(f"{prefix}.*"):
            if path.is_file() and path.suffix.casefold() in _DOWNLOAD_RESIDUAL_SUFFIXES:
                path.unlink(missing_ok=True)

    @staticmethod
    def _validate_video_file(path: Path) -> None:
        try:
            probe(path)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise NonRetryableMediaError(f"视频文件校验失败，已丢弃无效产物：{path.name}") from exc

    def _promote_video_copy(self, source: Path, destination: Path) -> Path:
        """Copy/remux a downloaded video through a validated temporary MP4."""
        temporary = destination.parent / ".original.mp4.materialize"
        temporary.unlink(missing_ok=True)
        try:
            run_command(
                ["ffmpeg", "-y", "-i", str(source), "-c", "copy", str(temporary)],
                cwd=self.root,
            )
            self._validate_video_file(temporary)
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def _materialize_direct_mp4(
        self,
        media_url: str,
        source_type: str,
        *,
        source_page_url: str | None,
        cookies_file: Path | None,
        user_agent: str | None,
    ) -> Path:
        """Download a real MP4 URL once and atomically promote it to the local source."""
        source_dir = self.root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        destination = source_dir / "original.mp4"
        temporary = source_dir / ".original.mp4.download"
        temporary.unlink(missing_ok=True)

        # The URL may have come from an Agent or a human; do not let the media
        # executor turn it into a request to a private/local address.
        _reject_private_host(media_url)
        jar = _load_cookie_jar(cookies_file)
        try:
            with (
                httpx.Client(
                    timeout=httpx.Timeout(connect=20, read=600, write=30, pool=30),
                    follow_redirects=True,
                    headers=_media_headers(source_page_url, user_agent),
                    cookies=_httpx_cookies(jar),
                ) as client,
                client.stream("GET", media_url) as response,
            ):
                if response.status_code in {401, 403}:
                    raise NonRetryableMediaError(
                        "视频直链已过期或被站点拒绝（HTTP 401/403），请从来源页重新提取一次直链"
                    )
                response.raise_for_status()
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                )
                if content_type.startswith("text/") or content_type in {
                    "text/html",
                    "application/json",
                }:
                    raise NonRetryableMediaError(
                        f"视频直链返回了非视频内容（content-type={content_type or 'unknown'}）"
                    )
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > _DIRECT_MEDIA_MAX_BYTES:
                    raise NonRetryableMediaError(
                        f"视频直链超过安全上限（{_DIRECT_MEDIA_MAX_BYTES} 字节）"
                    )
                total = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > _DIRECT_MEDIA_MAX_BYTES:
                            raise NonRetryableMediaError(
                                f"视频直链超过安全上限（{_DIRECT_MEDIA_MAX_BYTES} 字节）"
                            )
                        handle.write(chunk)
        except NonRetryableMediaError:
            temporary.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("视频直链网络请求失败，稍后可重试") from exc
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise RuntimeError("视频直链写入本地存储失败，请检查磁盘空间") from exc

        if temporary.stat().st_size < 64:
            temporary.unlink(missing_ok=True)
            raise NonRetryableMediaError("视频直链返回内容过短，不是可用的 MP4 文件")
        with temporary.open("rb") as handle:
            header = handle.read(64)
        if b"ftyp" not in header:
            temporary.unlink(missing_ok=True)
            raise NonRetryableMediaError("视频直链返回的内容不是 MP4 文件")
        try:
            probe(temporary)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            temporary.unlink(missing_ok=True)
            raise NonRetryableMediaError("视频直链下载完成但媒体校验失败，已丢弃临时文件") from exc

        os.replace(temporary, destination)
        self._write_source_info(
            destination,
            source_url=media_url,
            source_type=source_type,
            source_page_url=source_page_url,
            method="direct-mp4",
            mime_type="video/mp4",
        )
        logger.info(
            "视频直链已落地为本地 Artifact source=%s bytes=%s",
            destination,
            destination.stat().st_size,
        )
        return destination

    def _acquire_kickstarter_fallback(
        self,
        source_url: str,
        source_dir: Path,
        *,
        source_type: str = "kickstarter",
        cookies_file: Path | None,
        user_agent: str | None,
    ) -> Path:
        """Extract a signed video URL and materialize it from the current page.

        The page URL is intentionally fetched for every attempt: Kickstarter's
        media URLs are signed and expire. MP4 URLs are downloaded and validated
        directly; HLS URLs continue through yt-dlp.
        """
        page = _fetch_kickstarter_page(source_url, cookies_file, user_agent)
        video_urls = _extract_kickstarter_video_urls(page)
        if not video_urls:
            raise NonRetryableMediaError(
                "Kickstarter 项目页没有找到可下载的视频地址，请确认项目仍有视频并更新 Cookies"
            )

        fallback_options: dict[str, Any] = {
            "format": "best[height<=720]/best",
            "merge_output_format": "mp4",
            "outtmpl": str(source_dir / "kickstarter.%(ext)s"),
            "http_headers": {
                "Referer": source_url,
                "User-Agent": (user_agent or "").strip() or _DEFAULT_MEDIA_USER_AGENT,
            },
            "noplaylist": True,
            "quiet": True,
            "retries": 2,
            "fragment_retries": 2,
        }
        if cookies_file is not None and cookies_file.is_file():
            fallback_options["cookiefile"] = str(cookies_file)

        last_error: DownloadError | None = None
        for video_url in video_urls:
            self._discard_download_candidates(source_dir, "kickstarter")
            if _is_direct_mp4_url(video_url):
                try:
                    return self._materialize_direct_mp4(
                        video_url,
                        source_type,
                        source_page_url=source_url,
                        cookies_file=cookies_file,
                        user_agent=user_agent,
                    )
                except (NonRetryableMediaError, RuntimeError) as exc:
                    logger.warning(
                        "Kickstarter 直链落地失败，尝试下一个候选：%s",
                        type(exc).__name__,
                    )
                    last_error = DownloadError(str(exc))
                    continue
            try:
                with YoutubeDL(fallback_options) as downloader:
                    downloader.download([video_url])
            except DownloadError as exc:
                last_error = exc
                logger.warning(
                    "Kickstarter 视频地址下载失败，尝试下一个候选：%s",
                    type(exc).__name__,
                )
                continue
            candidates = sorted(source_dir.glob("kickstarter.*"))
            video = next(
                (
                    path
                    for path in candidates
                    if path.suffix.lower() in {".mp4", ".mkv", ".webm"} and path.stat().st_size > 0
                ),
                None,
            )
            if video is not None:
                try:
                    self._validate_video_file(video)
                except NonRetryableMediaError as exc:
                    last_error = DownloadError(str(exc))
                    continue
                return video

        raise NonRetryableMediaError(
            "Kickstarter 视频地址已找到，但下载失败；请更新 Cookies 后再试"
        ) from last_error

    def _download_youtube_clients(
        self, source_url: str, source_dir: Path, options: dict[str, Any]
    ) -> DownloadError | None:
        last_error: DownloadError | None = None
        for client in ("android_vr", "android", "mweb"):
            self._discard_download_candidates(source_dir, "original")
            fallback_options = {
                **options,
                "extractor_args": {"youtube": {"player_client": [client]}},
            }
            try:
                with YoutubeDL(fallback_options) as downloader:
                    downloader.download([source_url])
            except DownloadError as exc:
                last_error = exc
                self._discard_download_candidates(source_dir, "original")
                continue
            if not any(
                path.is_file()
                and path.stat().st_size > 0
                and path.suffix.lower() in {".mp4", ".mkv", ".webm"}
                for path in source_dir.glob("original.*")
            ):
                self._discard_download_candidates(source_dir, "original")
                last_error = DownloadError(f"YouTube client {client} produced no video file")
                continue
            return None
        return last_error

    def acquire(
        self,
        source_url: str,
        source_type: str,
        *,
        cookies_file: Path | None = None,
        source_page_url: str | None = None,
        user_agent: str | None = None,
    ) -> Path:
        source_dir = self.root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        destination = source_dir / "original.mp4"
        if destination.is_file() and destination.stat().st_size > 0:
            try:
                probe(destination)
                return destination
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                # A killed ffmpeg/worker may leave a non-empty but unreadable canonical
                # output. Remove this exact stale file so a retry can materialize cleanly.
                logger.warning("发现损坏的原片产物，丢弃后重新获取：%s (%s)", destination, exc)
                destination.unlink(missing_ok=True)
        if _is_direct_mp4_url(source_url):
            try:
                return self._materialize_direct_mp4(
                    source_url,
                    source_type,
                    source_page_url=source_page_url,
                    cookies_file=cookies_file,
                    user_agent=user_agent,
                )
            except (NonRetryableMediaError, RuntimeError) as direct_error:
                if source_page_url and _is_kickstarter_url(source_page_url):
                    logger.warning("Kickstarter 直链不可用，尝试从来源页重新提取")
                    return self._acquire_kickstarter_fallback(
                        source_page_url,
                        source_dir,
                        source_type=source_type,
                        cookies_file=cookies_file,
                        user_agent=user_agent,
                    )
                raise direct_error
        source_path = Path(source_url)
        if source_type == "local" or source_path.is_file():
            temporary = source_dir / ".original.mp4.local-copy"
            temporary.unlink(missing_ok=True)
            shutil.copy2(source_path.resolve(), temporary)
            self._validate_video_file(temporary)
            os.replace(temporary, destination)
            self._write_source_info(
                destination,
                source_url=source_url,
                source_type=source_type,
                source_page_url=source_page_url,
                method="local-copy",
                mime_type="video/mp4",
            )
            return destination

        options = {
            "format": "bestvideo[height<=720]+bestaudio/best[height<=720]",
            "merge_output_format": "mp4",
            "outtmpl": str(source_dir / "original.%(ext)s"),
            # Current YouTube extraction requires a JavaScript runtime for the
            # signature challenge. Reuse the installed Node runtime and yt-dlp's
            # maintained EJS challenge component instead of a site-specific hack.
            "js_runtimes": {"node": {"path": None}},
            "remote_components": ["ejs:github"],
            "writeinfojson": True,
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en", "zh-Hans", "zh"],
            "noplaylist": True,
            "quiet": True,
        }
        if cookies_file is not None:
            if Path(cookies_file).is_file():
                options["cookiefile"] = str(cookies_file)
            else:
                # 配置了但文件不在：降级裸请求，不阻断下载。
                logger.warning("cookies 文件不存在，本次不携带：%s", cookies_file)
        self._discard_download_candidates(source_dir, "original")
        try:
            with YoutubeDL(options) as downloader:
                downloader.download([source_url])
        except DownloadError as exc:
            download_error: DownloadError | None = exc
            cause = exc.exc_info[1] if exc.exc_info else None
            message = str(exc)
            if _is_kickstarter_url(source_url):
                # 当前 Kickstarter 页面已把视频地址放入页面 JSON，旧版
                # yt-dlp 提取器会落到 Generic 并报 Unsupported URL；此处
                # 仅回退到页面里的官方签名地址，再由确定性执行器落地。
                video = self._acquire_kickstarter_fallback(
                    source_url,
                    source_dir,
                    source_type=source_type,
                    cookies_file=cookies_file,
                    user_agent=user_agent,
                )
                if video != destination:
                    self._promote_video_copy(video, destination)
                else:
                    self._validate_video_file(destination)
                self._write_source_info(
                    destination,
                    source_url=source_url,
                    source_type=source_type,
                    source_page_url=source_url,
                    method="kickstarter-page-video",
                    mime_type="video/mp4",
                )
                return destination
            if _is_youtube_forbidden_error(source_url, message):
                # A YouTube 403 can be client-specific. Try maintained clients
                # in order, without adding credentials, and stop on success.
                download_error = self._download_youtube_clients(source_url, source_dir, options)
                if download_error is not None:
                    cause = download_error.exc_info[1] if download_error.exc_info else None
                    message = str(download_error)
            if download_error is not None:
                if isinstance(cause, UnsupportedError) or "Unsupported URL" in message:
                    raise NonRetryableMediaError(
                        "该链接没有可用的视频提取器（Unsupported URL），"
                        "请在候选中补录可下载的视频源"
                    ) from download_error
                if "Sign in to confirm" in message:
                    # 同 IP 短期重试必然同样失败且会加深风控标记，不重试也不做字幕回退。
                    raise NonRetryableMediaError(
                        "YouTube 触发人机验证，请在设置→采集与调度配置 cookies.txt 后重试"
                    ) from download_error
                if _is_video_gone(message):
                    # 已删除/私有/地区限制属确定性失败：重试只会同样失败。
                    raise NonRetryableMediaError(
                        "视频源已不可用（已删除/设为私有/地区限制），请更换视频源或丢弃该候选"
                    ) from download_error
                if not _is_subtitle_rate_limit(message):
                    raise download_error from None
                # Subtitle endpoints are rate-limited independently from video media.
                # Preserve the real-source workflow by retrying without subtitles; the
                # transcript stage can still use local ASR or Agent research context.
                fallback_options = {
                    **options,
                    "writesubtitles": False,
                    "writeautomaticsub": False,
                    "subtitleslangs": [],
                }
                self._discard_download_candidates(source_dir, "original")
                try:
                    with YoutubeDL(fallback_options) as downloader:
                        downloader.download([source_url])
                except DownloadError as subtitle_fallback_error:
                    if not _is_youtube_forbidden_error(source_url, str(subtitle_fallback_error)):
                        raise
                    download_error = self._download_youtube_clients(
                        source_url, source_dir, fallback_options
                    )
                    if download_error is not None:
                        raise download_error from None
        candidates = sorted(source_dir.glob("original.*"))
        video = next(
            (path for path in candidates if path.suffix.lower() in {".mp4", ".mkv", ".webm"}),
            None,
        )
        if video is None:
            # 确定性失败：同一 URL 重试不会凭空长出可用视频，一次即弃不烧重试。
            raise NonRetryableMediaError(
                "yt-dlp did not produce a supported video file：该来源无可下载视频，"
                "请补录可下载的视频源 URL 后重试"
            )
        if video != destination:
            self._promote_video_copy(video, destination)
        else:
            self._validate_video_file(destination)
        self._write_source_info(
            destination,
            source_url=source_url,
            source_type=source_type,
            source_page_url=source_page_url,
            method="yt-dlp",
            mime_type="video/mp4",
        )
        return destination

    def acquire_images(self, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """图文模式的确定性取图：按 URL 清单下载网页原图到 source_images/。

        防护与采集侧一致：私网地址拒绝、8MB 上限、非图片 content-type 过滤；
        短边不足 500px 的小图丢弃；单张失败只跳过，不阻断整条内容。
        返回成功项的本地路径与尺寸，由调用方逐张登记 artifact。
        """
        target_dir = self.root / "source_images"
        target_dir.mkdir(parents=True, exist_ok=True)
        acquired: list[dict[str, Any]] = []
        with httpx.Client(
            timeout=30,
            follow_redirects=False,
            headers={"User-Agent": "glance-media/1.0"},
        ) as client:
            for index, item in enumerate(images, start=1):
                url = str(item.get("url") or "")
                try:
                    body, extension = _fetch_image(client, url)
                except (httpx.HTTPError, ValueError, OSError) as exc:
                    logger.warning("网页原图跳过 url=%s：%s", url, exc)
                    continue
                path = target_dir / f"source-{index:03d}{extension}"
                path.write_bytes(body)
                width, height = _image_dimensions(path)
                if min(width, height) < MIN_SOURCE_IMAGE_EDGE:
                    path.unlink(missing_ok=True)
                    logger.warning("网页原图尺寸不足丢弃 url=%s 尺寸=%sx%s", url, width, height)
                    continue
                acquired.append(
                    {
                        "path": path,
                        "url": url,
                        "caption": str(item.get("caption") or ""),
                        "source_page": str(item.get("source_page") or ""),
                        "width": width,
                        "height": height,
                    }
                )
        return acquired

    def analyze_scenes(self, video_path: Path) -> tuple[Path, list[dict[str, Any]]]:
        scene_dir = self.root / "scenes"
        scene_dir.mkdir(parents=True, exist_ok=True)
        detected = detect(str(video_path), ContentDetector(threshold=27.0))
        duration = float(probe(video_path)["format"]["duration"])
        intervals = [(start.get_seconds(), end.get_seconds()) for start, end in detected] or [
            (0.0, duration)
        ]
        scenes: list[dict[str, Any]] = []
        for index, (start, end) in enumerate(intervals, start=1):
            scene_id = f"scene-{index:03d}"
            thumbnail = scene_dir / f"{scene_id}.jpg"
            midpoint = min(max(start + (end - start) / 2, 0), max(duration - 0.05, 0))
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{midpoint:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=480:-2",
                    str(thumbnail),
                ],
                cwd=self.root,
                timeout=120,
            )
            scenes.append(
                {
                    "scene_id": scene_id,
                    "start_sec": round(start, 3),
                    "end_sec": round(end, 3),
                    "duration_sec": round(end - start, 3),
                    "thumbnail": str(thumbnail.relative_to(self.root)).replace("\\", "/"),
                }
            )
        scenes_path = scene_dir / "scenes.json"
        scenes_path.write_text(json.dumps({"scenes": scenes}, indent=2), encoding="utf-8")
        return scenes_path, scenes

    def ensure_transcript(
        self,
        *,
        asr_model: str = "",
        asr_device: str = "auto",
        asr_compute_type: str = "float16",
    ) -> Path:
        transcript_dir = self.root / "transcript"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        output = transcript_dir / "transcript.json"
        subtitle_files = list((self.root / "source").glob("*.vtt")) + list(
            (self.root / "source").glob("*.srt")
        )
        segments: list[dict[str, Any]] = []
        note = ""
        if subtitle_files:
            subs = pysubs2.load(str(subtitle_files[0]))
            segments = [
                {
                    "segment_id": f"seg-{index:04d}",
                    "start_sec": line.start / 1000,
                    "end_sec": line.end / 1000,
                    "text": line.plaintext,
                }
                for index, line in enumerate(subs, start=1)
            ]
        elif asr_model:
            segments, note = self._local_asr(asr_model, asr_device, asr_compute_type)
        payload: dict[str, Any] = {"segments": segments}
        packed_text = pack_transcript(payload)
        if packed_text:
            payload["packed_text"] = packed_text
        if note:
            payload["note"] = note
        output.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return output

    def extract_review_frames(
        self,
        video_path: Path,
        count: int = 5,
        *,
        plan_data: dict[str, Any] | None = None,
    ) -> list[str]:
        """Extract uniform frames plus bounded cut-boundary evidence for semantic QC."""
        duration = float(probe(video_path)["format"]["duration"])
        frame_dir = self.root / "review-frames"
        frame_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []
        for filename, timestamp in _review_frame_specs(duration, count, plan_data):
            target = frame_dir / filename
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    f"{timestamp:.3f}",
                    "-i",
                    str(video_path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=720:-2",
                    str(target),
                ],
                cwd=self.root,
            )
            paths.append(str(target.relative_to(self.root)).replace("\\", "/"))
        return paths

    def extract_cover_frame(self, video_path: Path, timestamp_sec: float, output: Path) -> Path:
        """Extract one normalized 9:16 cover base without making semantic decisions."""
        output.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{max(0.0, timestamp_sec):.3f}",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-vf",
                layout_filter("center_crop"),
                "-q:v",
                "2",
                str(output),
            ],
            cwd=self.root,
        )
        return output

    def gradient_cover_base(self, output: Path) -> Path:
        """Create a deterministic dark-blue gradient PPM used as the final cover fallback."""
        width, height = 1080, 1920
        top = (12, 18, 32)
        bottom = (20, 65, 119)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            handle.write(f"P6\n{width} {height}\n255\n".encode())
            for y in range(height):
                ratio = y / (height - 1)
                color = bytes(
                    round(start + (end - start) * ratio)
                    for start, end in zip(top, bottom, strict=True)
                )
                handle.write(color * width)
        return output

    def build_cover(self, base_image: Path, title: str) -> Path:
        """Overlay deterministic Chinese cover copy using bundled fonts and libass."""
        ensure_ass_filter()
        cover_title = "".join(title.split())[:12]
        if not cover_title:
            raise ValueError("cover title cannot be empty")
        wrapped = hard_wrap(cover_title, 6)
        temp_dir = self.root / "temp" / "cover"
        publish_dir = self.root / "publish"
        temp_dir.mkdir(parents=True, exist_ok=True)
        publish_dir.mkdir(parents=True, exist_ok=True)
        ass_path = temp_dir / "cover.ass"
        subs = pysubs2.SSAFile()
        subs.info["PlayResX"] = "1080"
        subs.info["PlayResY"] = "1920"
        subs.info["WrapStyle"] = "0"
        subs.styles["Default"] = pysubs2.SSAStyle(
            fontname="Noto Sans SC",
            fontsize=96,
            bold=True,
            primarycolor=pysubs2.Color(255, 255, 255),
            backcolor=pysubs2.Color(0, 0, 0, 153),
            borderstyle=3,
            outline=8,
            shadow=0,
            alignment=8,
            # Keep both title lines inside the centered 1080 x 1440 platform preview.
            marginv=360,
        )
        subs.append(
            pysubs2.SSAEvent(
                start=0,
                end=1000,
                text=wrapped,
                style="Default",
            )
        )
        subs.save(str(ass_path))
        output = publish_dir / "cover.jpg"
        filter_chain = (
            f"{layout_filter('center_crop')},"
            f"ass=filename='{ffmpeg_filter_path(ass_path)}':"
            f"fontsdir='{ffmpeg_filter_path(FONT_ASSET_DIR)}'"
        )
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(base_image),
                "-vf",
                filter_chain,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(output),
            ],
            cwd=self.root,
        )
        return output

    def _local_asr(
        self,
        model_name: str,
        device: str,
        compute_type: str,
        *,
        video_path: Path | None = None,
    ) -> tuple[list[dict[str, Any]], str]:
        video = video_path if video_path is not None else self.root / "source" / "original.mp4"
        if not video.is_file():
            return [], "source video missing; ASR skipped"
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return [], (
                "faster-whisper is not installed; run "
                "`uv sync --all-packages --locked` to restore the local CPU ASR dependency"
            )
        try:
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            raw_segments, _info = model.transcribe(
                str(video),
                vad_filter=True,
                word_timestamps=True,
            )
            segments = []
            for index, segment in enumerate(raw_segments, start=1):
                item: dict[str, Any] = {
                    "segment_id": f"seg-{index:04d}",
                    "start_sec": round(segment.start, 3),
                    "end_sec": round(segment.end, 3),
                    "text": segment.text.strip(),
                }
                for key in ("avg_logprob", "no_speech_prob"):
                    value = getattr(segment, key, None)
                    if isinstance(value, (int, float)) and math.isfinite(value):
                        item[key] = value
                words = []
                for word in getattr(segment, "words", None) or []:
                    word_text = str(getattr(word, "word", "") or "").strip()
                    if word_text:
                        words.append(
                            {
                                "start_sec": round(word.start, 3),
                                "end_sec": round(word.end, 3),
                                "text": word_text,
                            }
                        )
                        probability = getattr(word, "probability", None)
                        if (
                            isinstance(probability, Real)
                            and not isinstance(probability, bool)
                            and math.isfinite(probability)
                            and 0 <= probability <= 1
                        ):
                            words[-1]["probability"] = float(probability)
                if words:
                    item["words"] = words
                segments.append(item)
        except (RuntimeError, ValueError, OSError) as exc:
            return [], f"local ASR failed ({type(exc).__name__}): {exc}"
        return segments, ""

    def final_audio_evidence(
        self,
        video_path: Path,
        *,
        asr_model: str = "",
        asr_device: str = "auto",
        asr_compute_type: str = "float16",
    ) -> dict[str, Any]:
        """Independent final-track observations. No script, subtitles or TTS input.

        This is not native listening and never makes a semantic pass decision.
        Disabled/unavailable ASR is explicit evidence insufficiency, not success.
        """
        evidence: dict[str, Any] = {
            "version": 1,
            "method": "local_asr_and_pcm",
            "video_sha256": sha256_file(video_path),
            "asr_model": asr_model,
            "asr_device": asr_device,
            "asr_compute_type": asr_compute_type,
            "asr_status": "unavailable",
            "segments": [],
            "signal_status": "unavailable",
            "limitations": [
                "Not native listening; ASR can misrecognize pronunciation or hallucinate.",
                "PCM measures do not prove timbre, music balance, or absence of audible artifacts.",
                "Agent must assess evidence sufficiency; no automatic audio approval.",
            ],
        }
        cache = self.root / "render" / f"audio-evidence-{evidence['video_sha256']}.json"
        if cache.is_file():
            try:
                cached = json.loads(cache.read_text(encoding="utf-8"))
                if (
                    all(
                        cached.get(key) == evidence[key]
                        for key in (
                            "version",
                            "method",
                            "video_sha256",
                            "asr_model",
                            "asr_device",
                            "asr_compute_type",
                        )
                    )
                    and cached.get("asr_status") == "available"
                    and cached.get("segments")
                    and cached.get("signal_status") == "available"
                    and cached.get("signal", {}).get("window_sec") == 0.1
                ):
                    return cached
            except (ValueError, OSError, AttributeError):
                pass
        if asr_model:
            segments, note = self._local_asr(
                asr_model,
                asr_device,
                asr_compute_type,
                video_path=video_path,
            )
            if segments and not note:
                evidence.update(asr_status="available", segments=segments)
            else:
                evidence["asr_error_code"] = "FINAL_ASR_FAILED_OR_EMPTY"
        else:
            evidence["asr_error_code"] = "FINAL_ASR_DISABLED"
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-i",
                    str(video_path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-f",
                    "s16le",
                    "pipe:1",
                ],
                capture_output=True,
                check=True,
                timeout=120,
            )
            samples = array("h")
            samples.frombytes(result.stdout)
            if sys.byteorder != "little":
                samples.byteswap()
            if not samples:
                raise ValueError("empty decoded audio")
            rms_dbfs = []
            for start in range(0, len(samples), 1600):
                window = samples[start : start + 1600]
                rms = math.sqrt(sum(s * s for s in window) / len(window)) / 32768
                rms_dbfs.append(round(20 * math.log10(max(rms, 1e-10)), 2))
            evidence.update(
                signal_status="available",
                signal={
                    "decoded_pcm_sha256": hashlib.sha256(result.stdout).hexdigest(),
                    "sample_rate_hz": 16000,
                    "channels": 1,
                    "duration_sec": len(samples) / 16000,
                    "peak_amplitude": max(abs(s) for s in samples) / 32768,
                    "full_scale_sample_fraction": sum(abs(s) >= 32767 for s in samples)
                    / len(samples),
                    "window_sec": 0.1,
                    "rms_dbfs": rms_dbfs,
                },
            )
        except (OSError, ValueError, subprocess.SubprocessError):
            evidence["signal_error_code"] = "FINAL_AUDIO_DECODE_FAILED"
        if evidence["asr_status"] == evidence["signal_status"] == "available":
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        return evidence

    def synthesize_voice(
        self,
        narration: str,
        *,
        voice: str = "zh-CN-YunxiNeural",
        rate: str = "+20%",
        volume: str = "+0%",
        provider: str = "edge",
        provider_endpoint: str = "",
        provider_token: str = "",
        target_duration_sec: float | None = None,
    ) -> TtsResult:
        audio_dir = self.root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        requested = provider.strip().casefold() or "edge"
        implementation = get_tts_provider(requested, provider_endpoint, provider_token)
        # Publish the complete pair together; retries must never overwrite approved voice bytes.
        with TemporaryDirectory(prefix=".voice-", dir=audio_dir) as directory:
            staging = Path(directory)
            output = staging / "narration.mp3"
            result = implementation.synthesize(
                narration, output, voice=voice, rate=rate, volume=volume,
                target_duration_sec=target_duration_sec,
            )
            if (
                Path(result.audio_path).resolve() != output.resolve()
                or not output.is_file() or output.stat().st_size == 0
                or _finite_seconds(result.duration_sec, label="TTS duration") <= 0
            ):
                raise NonRetryableMediaError("TTS did not produce a valid staged voice")
            (staging / "word_boundaries.json").write_text(
                json.dumps(result.boundaries, ensure_ascii=False), encoding="utf-8",
            )
            destination = audio_dir / staging.name.removeprefix(".")
            staging.rename(destination)
        return TtsResult(
            audio_path=destination / output.name,
            duration_sec=result.duration_sec,
            boundaries=result.boundaries,
            provider=result.provider,
            fallback_reason=result.fallback_reason,
        )

    def visual_timing_evidence(self, voice_path: Path, duration: float) -> dict[str, Any] | None:
        """Freeze executor-owned TTS text/times and the exact bytes they describe."""
        duration = _finite_seconds(duration, label="TTS evidence duration")
        path = voice_path.with_name("word_boundaries.json")
        if not path.is_file():
            return None
        raw_boundaries = self._read_boundaries(path)
        selected = _select_timing_boundaries(raw_boundaries, duration)
        if raw_boundaries and not selected:
            raise NonRetryableMediaError("TTS boundaries are invalid or unsafe")
        return {
            "voice_sha256": sha256_file(voice_path),
            "boundaries_sha256": sha256_file(path),
            "duration_sec": duration,
            "boundaries": [
                {key: item[key] for key in ("text", "start_sec", "end_sec")} for item in selected
            ],
        }

    def render(
        self,
        *,
        video_path: Path,
        voice_path: Path,
        narration: str,
        opening_identity: str | None = None,
        plan_data: dict[str, Any],
        scenes: list[dict[str, Any]],
        assets: dict[str, Path] | None = None,
        generated_image_ids: set[str] | None = None,
        visual_timing_evidence: dict[str, Any] | None = None,
        encoder: str = "libx264",
        bgm_path: Path | None = None,
        bgm_volume: float = 0.15,
        bgm_meta: dict[str, Any] | None = None,
    ) -> tuple[Path, Path]:
        plan = RenderPlan.model_validate(plan_data)
        # 合成阶段必须烧录 ass 字幕：前置自检，避免编完全部片段后才在最后一步炸。
        ensure_ass_filter()
        boundaries_path = voice_path.with_name("word_boundaries.json")
        raw_boundaries = self._read_boundaries(boundaries_path) if boundaries_path.is_file() else []
        timing_boundaries: list[dict[str, Any]] | None = None
        voice_duration: float | None = None
        if visual_timing_evidence is not None:
            voice_duration = _finite_seconds(
                probe(voice_path)["format"]["duration"], label="Voice duration"
            )
            # Re-read, never regenerate the frozen approval against replacement audio.
            try:
                current = self.visual_timing_evidence(
                    voice_path, visual_timing_evidence.get("duration_sec")
                )
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                raise NonRetryableMediaError("Visual plan TTS timing evidence is invalid") from exc
            if (
                current != visual_timing_evidence
                or abs(voice_duration - visual_timing_evidence["duration_sec"])
                > _BOUNDARY_QUANTIZATION_TOLERANCE_SEC
            ):
                raise NonRetryableMediaError("Visual plan TTS timing evidence is stale or invalid")
        if raw_boundaries or visual_timing_evidence is not None:
            if visual_timing_evidence is not None:
                selected_boundaries = current["boundaries"]
            else:
                voice_duration = _finite_seconds(
                    probe(voice_path)["format"]["duration"], label="Voice duration"
                )
                selected_boundaries = _select_timing_boundaries(
                    raw_boundaries,
                    voice_duration,
                    required_internal_edges=len(plan.segments) - 1,
                )
            if not selected_boundaries and (raw_boundaries or visual_timing_evidence is None):
                raise ValueError("TTS boundaries are invalid or unsafe to use for rendering")
            calibrated_plan_data = (
                plan_data
                if visual_timing_evidence is not None
                else _calibrate_render_plan(plan_data, selected_boundaries, voice_duration)
            )
            plan = RenderPlan.model_validate(calibrated_plan_data)
            if visual_timing_evidence is not None or calibrated_plan_data != plan_data:
                timing_boundaries = selected_boundaries
                logger.info("渲染使用已核对的 TTS timing")
            else:
                logger.info("TTS boundaries 无法安全映射，保留原始画面计划时间轴")
        render_dir = self.root / "render"
        temp_dir = self.root / "temp" / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        temp_dir.mkdir(parents=True, exist_ok=True)
        scene_map = {scene["scene_id"]: scene for scene in scenes}
        clip_paths: list[Path] = []
        assets = assets or {}
        scene_segments = [segment for segment in plan.segments if segment.scene_id is not None]
        source_duration: float | None = None
        source_width = source_height = 0
        if scene_segments:
            try:
                source_metadata = probe(video_path)
                source_duration = _finite_seconds(
                    source_metadata["format"]["duration"], label="Source video duration"
                )
            except (KeyError, TypeError) as exc:
                raise ValueError("Source video duration is missing") from exc
            if source_duration <= 0:
                raise ValueError("Source video duration must be greater than 0")
            if any(segment.source_redactions for segment in scene_segments):
                source_width, source_height = _stream_dimensions(source_metadata)
                if not source_width or not source_height:
                    raise ValueError("Source video dimensions are required for source_redactions")
        for index, segment in enumerate(plan.segments):
            if segment.scene_id is None:
                asset_path = assets.get(segment.asset_id or "")
                if segment.layout != "image_card" or asset_path is None or not asset_path.is_file():
                    raise ValueError(f"Invalid image asset segment (asset_id={segment.asset_id})")
                requested_duration = segment.narration_end_sec - segment.narration_start_sec
                clip = temp_dir / f"segment-{index:03d}.mp4"
                redaction_filter = ""
                if segment.source_redactions:
                    asset_width, asset_height = _image_dimensions(asset_path)
                    redaction_filter = source_redaction_filter(
                        segment.source_redactions,
                        source_width=asset_width,
                        source_height=asset_height,
                    )
                image_filter = layout_filter(
                    "image_card",
                    motion=segment.motion,
                    focus_x=segment.focus_x,
                    focus_y=segment.focus_y,
                )
                if redaction_filter:
                    image_filter = f"{redaction_filter},{image_filter}"
                run_command(
                    [
                        "ffmpeg",
                        "-y",
                        "-loop",
                        "1",
                        "-i",
                        str(asset_path),
                        "-t",
                        f"{requested_duration:.3f}",
                        "-an",
                        "-vf",
                        _with_transition(
                            image_filter,
                            segment.transition,
                        ),
                        *_encoder_args(encoder),
                        "-pix_fmt",
                        "yuv420p",
                        str(clip),
                    ],
                    cwd=self.root,
                )
                clip_paths.append(clip)
                continue
            scene = scene_map.get(segment.scene_id)
            if scene is None:
                raise ValueError(f"Unknown scene ID: {segment.scene_id}")
            scene_id = segment.scene_id
            if "start_sec" not in scene or "end_sec" not in scene:
                raise ValueError(f"Scene {scene_id} is missing start_sec/end_sec")
            if source_duration is None:
                raise ValueError("Source video duration is missing")
            scene_start = _finite_seconds(scene["start_sec"], label=f"Scene {scene_id} start_sec")
            scene_end = _clamp_to_duration(
                _finite_seconds(scene["end_sec"], label=f"Scene {scene_id} end_sec"),
                source_duration,
                label=f"Scene {scene_id}",
            )
            if scene_start < 0 or scene_end <= scene_start:
                raise ValueError(f"Scene {scene_id} has invalid bounds")
            if segment.source_start_sec is None or segment.source_end_sec is None:
                raise ValueError(f"Scene {scene_id} requires source_start_sec/source_end_sec")
            source_start = _finite_seconds(
                segment.source_start_sec,
                label=f"Scene {scene_id} source_start_sec",
            )
            source_end = _clamp_to_duration(
                _finite_seconds(
                    segment.source_end_sec,
                    label=f"Scene {scene_id} source_end_sec",
                ),
                source_duration,
                label=f"Source interval for scene {scene_id}",
            )
            if source_start < scene_start or source_end > scene_end:
                raise ValueError(f"Source interval for scene {scene_id} exceeds scene bounds")
            if source_start < 0 or source_end > source_duration:
                raise ValueError(
                    f"Source interval for scene {scene_id} exceeds source video duration"
                )
            if source_end <= source_start:
                raise ValueError(f"Source interval for scene {scene_id} must be positive")
            requested_duration = segment.narration_end_sec - segment.narration_start_sec
            clip = temp_dir / f"segment-{index:03d}.mp4"
            video_filter = f"trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS"
            video_filter = (
                f"{video_filter},tpad=stop_mode=clone:stop_duration={requested_duration:.6f}"
            )
            if segment.source_redactions:
                redaction_filter = source_redaction_filter(
                    segment.source_redactions,
                    source_width=source_width,
                    source_height=source_height,
                )
                video_filter = f"{video_filter},{redaction_filter}"
            video_filter = f"{video_filter},{layout_filter(segment.layout)}"
            video_filter = _with_transition(video_filter, segment.transition)
            run_command(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video_path),
                    "-t",
                    f"{requested_duration:.6f}",
                    "-an",
                    "-vf",
                    video_filter,
                    *_encoder_args(encoder),
                    "-pix_fmt",
                    "yuv420p",
                    str(clip),
                ],
                cwd=self.root,
            )
            clip_paths.append(clip)
        if not clip_paths:
            raise ValueError("Visual plan produced no renderable scene segments")

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text(
            "\n".join(f"file '{path.as_posix()}'" for path in clip_paths),
            encoding="utf-8",
        )
        silent_video = temp_dir / "silent.mp4"
        run_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-c",
                "copy",
                str(silent_video),
            ],
            cwd=self.root,
        )

        if voice_duration is None:
            voice_duration = _finite_seconds(
                probe(voice_path)["format"]["duration"], label="Voice duration"
            )
        subtitle_path = self._build_subtitles(
            narration,
            voice_duration,
            opening_identity=opening_identity,
            boundaries=timing_boundaries,
            concept_intervals=[
                (segment.narration_start_sec, segment.narration_end_sec)
                for segment in plan.segments
                if segment.layout == "image_card"
                and segment.asset_id in (generated_image_ids or set())
            ],
        )
        audio_path = voice_path
        audio_filters = ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
        selected_bgm = bgm_path if bgm_path is not None and bgm_path.is_file() else None
        if selected_bgm is not None:
            audio_path = self._mix_voice_and_bgm(
                voice_path,
                selected_bgm,
                duration_sec=voice_duration,
                volume=bgm_volume,
            )
            audio_filters = []
            self._write_bgm_receipt(selected_bgm, bgm_meta)
        final_path = render_dir / "final.mp4"
        final_temporary = render_dir / ".final.tmp.mp4"
        final_temporary.unlink(missing_ok=True)
        escaped_subtitle_path = ffmpeg_filter_path(subtitle_path)
        escaped_fonts_dir = ffmpeg_filter_path(FONT_ASSET_DIR)
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(silent_video),
                "-i",
                str(audio_path),
                "-vf",
                f"ass=filename='{escaped_subtitle_path}':fontsdir='{escaped_fonts_dir}'",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                *_encoder_args(encoder),
                *audio_filters,
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-shortest",
                "-movflags",
                "+faststart",
                str(final_temporary),
            ],
            cwd=self.root,
        )
        _promote_render_output(final_temporary, final_path)
        preview_path = render_dir / "preview.mp4"
        preview_temporary = render_dir / ".preview.tmp.mp4"
        preview_temporary.unlink(missing_ok=True)
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(final_path),
                "-vf",
                "scale=720:1280",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(preview_temporary),
            ],
            cwd=self.root,
        )
        _promote_render_output(preview_temporary, preview_path)
        shutil.rmtree(temp_dir, ignore_errors=True)
        return final_path, preview_path

    def _mix_voice_and_bgm(
        self,
        voice_path: Path,
        bgm_path: Path,
        *,
        duration_sec: float,
        volume: float,
    ) -> Path:
        audio_dir = self.root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        mixed = audio_dir / "mix.m4a"
        duck = "sidechaincompress" in available_ffmpeg_filters()
        run_command(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(voice_path),
                "-stream_loop",
                "-1",
                "-i",
                str(bgm_path),
                "-filter_complex",
                mix_filter_complex(duration_sec, volume, duck=duck),
                "-map",
                "[aout]",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-t",
                f"{max(duration_sec, 0.2):.3f}",
                str(mixed),
            ],
            cwd=self.root,
        )
        return mixed

    def _write_bgm_receipt(self, bgm_path: Path, meta: dict[str, Any] | None) -> None:
        audio_dir = self.root / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        try:
            relative = str(bgm_path.resolve().relative_to(self.root.resolve())).replace("\\", "/")
        except ValueError:
            relative = bgm_path.name
        payload = {
            "file": relative,
            "path": str(bgm_path),
            "enabled": True,
        }
        if meta:
            payload.update(meta)
        (audio_dir / "bgm.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _events_from_boundaries(
        self, boundaries: list[dict[str, Any]], duration: float
    ) -> list[tuple[float, float, str]]:
        events: list[tuple[float, float, str]] = []
        line_words: list[dict[str, Any]] = []
        line_length = 0
        for word in boundaries:
            text = str(word["text"]).strip()
            if not text:
                continue
            text_length = _subtitle_visible_length(text)
            if text_length > _SUBTITLE_LINE_CHARS:
                if line_words:
                    events.append(_join_boundary_line(line_words, duration))
                    line_words = []
                    line_length = 0
                events.extend(_split_boundary(word, duration))
                continue
            if line_words and (
                line_length + text_length > _SUBTITLE_LINE_CHARS
                or float(word["start_sec"]) > float(line_words[-1]["end_sec"]) + 0.05
                or str(line_words[-1]["text"]).rstrip().endswith(tuple("。！？.!?"))
            ):
                events.append(_join_boundary_line(line_words, duration))
                line_words = []
                line_length = 0
            line_words.append(word)
            line_length += text_length
        if line_words:
            events.append(_join_boundary_line(line_words, duration))
        grouped: list[tuple[float, float, str]] = []
        for start, end, text in events:
            if grouped:
                previous_start, previous_end, previous_text = grouped[-1]
                combined = _join_boundary_text(
                    [{"text": part} for part in previous_text.split(r"\N")] + [{"text": text}]
                )
                if (
                    start <= previous_end + 0.05
                    and previous_text[-1] not in "。！？.!?"
                    and (_subtitle_visible_length(text) <= 3 or end - start < 0.4)
                    and _subtitle_visible_length(combined) <= 2 * _SUBTITLE_LINE_CHARS
                ):
                    grouped[-1] = (
                        previous_start,
                        end,
                        r"\N".join(_subtitle_parts(combined)),
                    )
                    continue
            grouped.append((start, end, text))
        return grouped

    @staticmethod
    def _read_boundaries(path: Path) -> list[dict[str, Any]]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            payload = payload.get("words") or payload.get("boundaries") or []
        return payload if isinstance(payload, list) else []

    def _subtitle_events(
        self,
        narration: str,
        duration: float,
        *,
        boundaries: list[dict[str, Any]] | None = None,
    ) -> list[tuple[float, float, str]]:
        """Resolve timing from word alignment, sentence boundaries, then character estimate."""
        if boundaries:
            logger.info("字幕时间源：与画面共用 audio/word_boundaries.json")
            return self._events_from_boundaries(boundaries, duration)
        audio_dir = self.root / "audio"
        alignment_path = audio_dir / "word_alignment.json"
        boundaries_path = audio_dir / "word_boundaries.json"
        if boundaries is None and alignment_path.is_file():
            alignment = self._read_boundaries(alignment_path)
            if alignment:
                logger.info("字幕时间源：audio/word_alignment.json")
                return self._events_from_boundaries(alignment, duration)
            logger.info("字幕时间源回退：word_alignment.json 为空，尝试 word_boundaries.json")
        else:
            logger.info("字幕时间源回退：缺少 word_alignment.json，尝试 word_boundaries.json")
        if boundaries is None and boundaries_path.is_file():
            boundaries = self._read_boundaries(boundaries_path)
            if boundaries:
                sentence_boundaries = [
                    item for item in boundaries if item.get("kind") == "SentenceBoundary"
                ]
                if sentence_boundaries:
                    boundaries = sentence_boundaries
                logger.info("字幕时间源：audio/word_boundaries.json（整句）")
                return self._events_from_boundaries(boundaries, duration)
            logger.info("字幕时间源回退：word_boundaries.json 为空，按字数估时")
        else:
            logger.info("字幕时间源回退：缺少 word_boundaries.json，按字数估时")
        sentences = [
            item.strip()
            for item in narration.replace("！", "。").replace("？", "。").split("。")
            if item.strip()
        ] or [narration]
        chunks = [part for sentence in sentences for part in _subtitle_parts(sentence)]
        total_characters = sum(_subtitle_visible_length(chunk) for chunk in chunks)
        events = []
        elapsed = 0.0
        for index, text in enumerate(chunks):
            start = elapsed
            elapsed = (
                duration
                if index == len(chunks) - 1
                else elapsed + duration * _subtitle_visible_length(text) / total_characters
            )
            events.append((start, elapsed, text))
        return events

    def _build_subtitles(
        self,
        narration: str,
        duration: float,
        *,
        opening_identity: str | None = None,
        boundaries: list[dict[str, Any]] | None = None,
        concept_intervals: list[tuple[float, float]] | None = None,
    ) -> Path:
        subtitle_dir = self.root / "transcript"
        subtitle_dir.mkdir(parents=True, exist_ok=True)
        output = subtitle_dir / "narration.ass"
        subs = pysubs2.SSAFile()
        subs.info["PlayResX"] = "1080"
        subs.info["PlayResY"] = "1920"
        subs.info["WrapStyle"] = "0"
        for name, style in _SUBTITLE_THEME.items():
            subs.styles[name] = pysubs2.SSAStyle(**style)
        for start, end, text in self._subtitle_events(
            narration,
            duration,
            boundaries=boundaries,
        ):
            subs.append(
                pysubs2.SSAEvent(
                    start=int(start * 1000),
                    end=int(end * 1000),
                    text=text,
                    style="Default",
                )
            )
        for start, end in concept_intervals or []:
            start, end = max(0.0, start), min(duration, end)
            if end > start:
                if "Concept" not in subs.styles:
                    subs.styles["Concept"] = pysubs2.SSAStyle(
                        fontname="Noto Sans SC",
                        fontsize=36,
                        bold=True,
                        primarycolor=pysubs2.Color(255, 255, 255),
                        outlinecolor=pysubs2.Color(0, 0, 0),
                        backcolor=pysubs2.Color(0, 0, 0, 64),
                        borderstyle=3,
                        outline=3,
                        shadow=0,
                        alignment=7,
                        marginl=60,
                        marginr=60,
                        marginv=100,
                    )
                subs.append(
                    pysubs2.SSAEvent(
                        start=int(start * 1000),
                        end=int(end * 1000),
                        text="AI概念示意·非实拍",
                        style="Concept",
                    )
                )
        identity = "".join(char if char.isprintable() else " " for char in (opening_identity or "")).strip()
        if identity and duration > 0:
            from PIL import ImageFont

            # Fullwidth control glyphs stay visible without becoming ASS overrides or line breaks.
            identity = identity.translate(str.maketrans({"\\": "＼", "{": "｛", "}": "｝"}))
            font = ImageFont.truetype(str(FONT_FILES[1]), 48)
            lines = [""]
            # CJK characters can wrap; other non-space tokens keep model numbers and accented names intact.
            for token in re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]|[^\s\u3400-\u4dbf\u4e00-\u9fff]+|\s+", identity):
                if lines[-1] and font.getlength(lines[-1] + token) > 692:
                    lines.append(token)
                else:
                    lines[-1] += token
            if len(lines) > 12 or any(font.getlength(line) > 692 for line in lines):
                raise NonRetryableMediaError("Opening product identity is too long for the readable safe area")
            subs.styles["OpeningIdentity"] = pysubs2.SSAStyle(
                fontname="Noto Sans SC",
                fontsize=48,
                bold=True,
                primarycolor=pysubs2.Color(255, 255, 255),
                outlinecolor=pysubs2.Color(0, 0, 0),
                backcolor=pysubs2.Color(0, 0, 0, 102),
                borderstyle=3,
                outline=4,
                shadow=0,
                alignment=8,
                marginl=100,
                marginr=280,
                marginv=320,
            )
            subs.append(pysubs2.SSAEvent(
                start=0, end=int(min(8.0, duration) * 100) * 10, text=r"\N".join(lines), style="OpeningIdentity",
            ))
        subs.save(str(output))
        return output

    def qc(
        self,
        final_path: Path,
        *,
        min_seconds: float = 20,
        max_seconds: float = 90,
        plan_data: dict[str, Any] | None = None,
        expected_duration_sec: float | None = None,
    ) -> dict[str, Any]:
        metadata = probe(final_path)
        streams = metadata["streams"]
        video = next((item for item in streams if item["codec_type"] == "video"), None)
        audio = next((item for item in streams if item["codec_type"] == "audio"), None)
        duration = float(metadata["format"]["duration"])
        errors: list[str] = []
        if video is None:
            errors.append("missing video stream")
        elif (video.get("width"), video.get("height")) != (1080, 1920):
            errors.append("video is not 1080x1920")
        if audio is None:
            errors.append("missing audio stream")
        if not min_seconds <= duration <= max_seconds:
            errors.append(
                f"duration {duration:.1f}s is outside bounds "
                f"[{min_seconds:.0f}s, {max_seconds:.0f}s]"
            )
        segments = (plan_data or {}).get("segments", []) if isinstance(plan_data, dict) else []
        timeline_end = None
        if isinstance(segments, list):
            ends = []
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                try:
                    end = float(segment.get("narration_end_sec"))
                except (TypeError, ValueError):
                    continue
                if math.isfinite(end) and end > 0:
                    ends.append(end)
            if ends:
                timeline_end = max(ends)
        target = None
        if expected_duration_sec is not None:
            try:
                candidate = float(expected_duration_sec)
            except (TypeError, ValueError):
                candidate = 0.0
            if math.isfinite(candidate) and candidate > 0:
                target = candidate
        duration_evidence = {
            "actual_duration_sec": round(duration, 3),
            "target_duration_sec": round(target, 3) if target is not None else None,
            "timeline_end_sec": round(timeline_end, 3) if timeline_end is not None else None,
            "tolerance_sec": _DURATION_TOLERANCE_SEC,
        }
        if target is not None and abs(duration - target) > _DURATION_TOLERANCE_SEC:
            errors.append(
                f"duration {duration:.2f}s differs from target {target:.2f}s "
                f"by {abs(duration - target):.2f}s"
            )
        if timeline_end is not None and abs(duration - timeline_end) > _DURATION_TOLERANCE_SEC:
            errors.append(
                f"duration {duration:.2f}s differs from timeline end {timeline_end:.2f}s "
                f"by {abs(duration - timeline_end):.2f}s"
            )
        subtitle_path = self.root / "transcript" / "narration.ass"
        if subtitle_path.is_file():
            subtitles = pysubs2.load(str(subtitle_path))
            for event in subtitles:
                if event.style != "Default":
                    continue
                lines = event.text.split(r"\N")
                if len(lines) > 2 or any(
                    _subtitle_visible_length(line) > _SUBTITLE_LINE_CHARS for line in lines
                ):
                    errors.append("subtitle_overflow")
                    break
        visual_warnings: list[dict[str, Any]] = []
        if (
            isinstance(segments, list)
            and segments
            and all(
                isinstance(segment, dict) and not segment.get("scene_id") for segment in segments
            )
        ):
            asset_ids = {
                str(segment.get("asset_id"))
                for segment in segments
                if isinstance(segment, dict) and segment.get("asset_id")
            }
            minimum_assets = 4 if duration <= 30 else 5
            if len(asset_ids) < minimum_assets:
                visual_warnings.append(
                    {
                        "code": "image_only_asset_count_low",
                        "message": "图文模式独立图片数量偏少，可能出现重复画面",
                        "asset_count": len(asset_ids),
                        "minimum": minimum_assets,
                    }
                )
            motions = {
                str(segment.get("motion") or "hold")
                for segment in segments
                if isinstance(segment, dict)
            }
            if motions and motions == {"hold"}:
                visual_warnings.append(
                    {
                        "code": "image_only_motion_missing",
                        "message": "图文模式没有缩放、平移或焦点变化",
                    }
                )
            long_segments = [
                round(
                    float(segment.get("narration_end_sec", 0))
                    - float(segment.get("narration_start_sec", 0)),
                    2,
                )
                for segment in segments
                if isinstance(segment, dict)
                and float(segment.get("narration_end_sec", 0))
                - float(segment.get("narration_start_sec", 0))
                > 8
            ]
            if long_segments:
                visual_warnings.append(
                    {
                        "code": "image_only_segment_too_long",
                        "message": "存在超过 8 秒的单图画面段",
                        "durations_sec": long_segments,
                    }
                )
        result = {
            "passed": not errors,
            "errors": errors,
            "visual_warnings": visual_warnings,
            "duration_sec": duration,
            "duration_evidence": duration_evidence,
            "video": video,
            "audio": audio,
        }
        qc_path = self.root / "render" / "qc.json"
        qc_path.parent.mkdir(parents=True, exist_ok=True)
        qc_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return result

    def package(self, metadata: dict[str, Any]) -> Path:
        publish_dir = self.root / "publish"
        publish_dir.mkdir(parents=True, exist_ok=True)
        final = self.root / "render" / "final.mp4"
        destination = publish_dir / "final.mp4"
        destination.unlink(missing_ok=True)
        try:
            os.link(final, destination)
        except OSError:
            shutil.copy2(final, destination)
        (publish_dir / "publish.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return publish_dir

    def artifact_metadata(
        self,
        path: Path,
        artifact_type: str,
        stage: str,
    ) -> dict[str, Any]:
        relative = str(path.resolve().relative_to(self.root)).replace("\\", "/")
        return {
            "artifact_type": artifact_type,
            "relative_path": relative,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "mime_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
            "producer_stage": stage,
        }
