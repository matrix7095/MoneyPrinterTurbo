from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import httpx
from glance_core.settings import validate_tts_provider_endpoint

from glance_media.tts.base import TtsResult

_DISPLAY_LIMIT = 14
_RATE_PATTERN = re.compile(r"([+-])?(\d+(?:\.\d+)?)%")
_WIDE_WIDTHS = frozenset({"W", "F", "A"})
_BREAK_CHARS = frozenset("。！？；;.!?，,")


def display_width(text: str) -> int:
    """Count CosyVoice display units: wide/ambiguous glyphs count as two."""
    return sum(2 if unicodedata.east_asian_width(char) in _WIDE_WIDTHS else 1 for char in text)


def rate_to_speed(rate: str) -> float:
    """Map Edge-style +20% rate strings onto CosyVoice's 0.80-1.20 speed."""
    match = _RATE_PATTERN.fullmatch((rate or "").strip())
    if match is None:
        return 1.0
    sign = -1.0 if match.group(1) == "-" else 1.0
    speed = 1.0 + sign * float(match.group(2)) / 100.0
    return max(0.8, min(1.2, speed))


def split_narration(text: str, limit: int = _DISPLAY_LIMIT) -> list[str]:
    """Split a full script into CosyVoice-legal segments without dropping punctuation."""
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    if not normalized:
        raise ValueError("配音文本不能为空")
    if display_width(normalized) <= limit:
        return [normalized]

    pieces: list[str] = []
    current = ""
    for char in normalized:
        current += char
        if char in _BREAK_CHARS:
            pieces.append(current)
            current = ""
    if current:
        pieces.append(current)

    segments: list[str] = []
    bucket = ""
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        if display_width(piece) > limit:
            if bucket:
                segments.append(bucket)
                bucket = ""
            segments.extend(_hard_wrap(piece, limit))
            continue
        candidate = f"{bucket}{piece}" if bucket else piece
        if display_width(candidate) <= limit:
            bucket = candidate
        else:
            segments.append(bucket)
            bucket = piece
    if bucket:
        segments.append(bucket)
    return segments


def _hard_wrap(text: str, limit: int) -> list[str]:
    parts: list[str] = []
    current = ""
    width = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in _WIDE_WIDTHS else 1
        if current and width + char_width > limit:
            parts.append(current)
            current = char
            width = char_width
            continue
        current += char
        width += char_width
    if current:
        parts.append(current)
    return parts


def describe_cosyvoice_error(
    exc: BaseException,
    *,
    status: int | None = None,
    body: dict[str, object] | None = None,
) -> str:
    code = str((body or {}).get("error_code") or "")
    message = str((body or {}).get("message") or "")
    if status == 401 or code == "UNAUTHORIZED":
        return (
            "本机 CosyVoice 拒绝访问：缺少或无效的访问令牌。"
            "请在 .env 配置 GLANCE_COSYVOICE_TOKEN 后重启 Glance。"
        )
    if code == "INVALID_IDEMPOTENCY_KEY":
        return "本机 CosyVoice 拒绝了请求：幂等键无效。"
    if "14 display units" in message:
        return "配音文本分段后仍超过 CosyVoice 单段上限。"
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return "连不上本机 CosyVoice 服务。请确认 127.0.0.1:8765 已启动。"
    if message:
        return f"本机 CosyVoice 配音失败：{message}"
    return f"本机 CosyVoice 配音失败：{type(exc).__name__}"


def probe_cosyvoice_readiness(endpoint: str) -> dict[str, object]:
    """Probe only the isolated loopback CosyVoice readiness contract."""
    target = endpoint.strip().rstrip("/")
    if not target:
        return {"status": "NOT_CONFIGURED", "endpoint": False}
    try:
        _provider, target = validate_tts_provider_endpoint("cosyvoice3", target)
    except ValueError:
        return {"status": "BLOCKED_ENDPOINT", "endpoint": True}
    try:
        response = httpx.get(f"{target}/readyz", timeout=2.0)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # pragma: no cover - depends on local service state
        return {"status": "DEGRADED", "endpoint": True, "detail": type(exc).__name__}
    if not isinstance(payload, dict):
        return {"status": "DEGRADED", "endpoint": True, "service_status": "UNKNOWN"}
    service_status = str(payload.get("status") or "").upper()
    return {
        "status": "READY" if service_status in {"READY", "OK"} else "DEGRADED",
        "endpoint": True,
        "service_status": service_status or "UNKNOWN",
    }


class CosyVoiceProvider:
    """Explicit loopback HTTP adapter for the isolated CosyVoice service.

    The media package never imports torch or a model runtime. Provider selection is
    made by the server/administrator; a failed CosyVoice request is surfaced to the
    caller and is not silently switched to Edge TTS.
    """

    name = "cosyvoice3"

    def __init__(self, endpoint: str, token: str = "") -> None:
        _provider, self.endpoint = validate_tts_provider_endpoint("cosyvoice3", endpoint)
        self.token = token.strip()

    def synthesize(
        self,
        narration: str,
        output_path: Path,
        *,
        voice: str,
        rate: str,
        volume: str,
        target_duration_sec: float | None = None,
    ) -> TtsResult:
        del volume, target_duration_sec
        if not self.token:
            raise RuntimeError(
                "本机 CosyVoice 缺少访问令牌。请在 .env 配置 GLANCE_COSYVOICE_TOKEN 后重启 Glance。"
            )
        voice_id = voice if ":" in voice else f"cosyvoice3:{voice}"
        speed = rate_to_speed(rate)
        segments = split_narration(narration)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="glance-cosyvoice-") as temp_dir:
            wav_paths = [
                self._synthesize_segment(
                    Path(temp_dir) / f"segment-{index:03d}.wav",
                    text=text,
                    voice_id=voice_id,
                    speed=speed,
                    request_id=f"{output_path.stem}-{index:03d}",
                    segment_id=f"seg-{index:03d}",
                )
                for index, text in enumerate(segments, start=1)
            ]
            source = Path(temp_dir) / "narration.wav"
            _concat_wavs(wav_paths, source)
            _convert_to_mp3(source, output_path)
        return TtsResult(
            audio_path=output_path,
            duration_sec=_probe_duration(output_path),
            boundaries=[],
            provider=self.name,
        )

    def _synthesize_segment(
        self,
        wav_path: Path,
        *,
        text: str,
        voice_id: str,
        speed: float,
        request_id: str,
        segment_id: str,
    ) -> Path:
        payload = {
            "request_id": request_id,
            "segment_id": segment_id,
            "text": text,
            "voice_id": voice_id,
            "speed": speed,
            "seed": None,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Idempotency-Key": _idempotency_key(payload),
        }
        try:
            response = httpx.post(
                f"{self.endpoint}/v1/synthesize",
                json=payload,
                headers=headers,
                timeout=120.0,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(describe_cosyvoice_error(exc)) from exc
        if response.status_code != 200:
            body = _json_body(response)
            raise RuntimeError(
                describe_cosyvoice_error(
                    RuntimeError(body.get("message") or response.reason_phrase),
                    status=response.status_code,
                    body=body,
                )
            )
        wav_path.write_bytes(response.content)
        return wav_path


def _idempotency_key(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_body(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _concat_wavs(paths: list[Path], output: Path) -> None:
    if len(paths) == 1:
        output.write_bytes(paths[0].read_bytes())
        return
    listing = output.with_name("concat.txt")
    listing.write_text(
        "".join(f"file '{path.as_posix()}'\n" for path in paths),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(listing),
                "-c",
                "copy",
                str(output),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("CosyVoice 分段音频拼接失败") from exc


def _convert_to_mp3(source: Path, output_path: Path) -> None:
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-codec:a",
                "libmp3lame",
                str(output_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
            shell=False,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("CosyVoice audio conversion failed") from exc


def _probe_duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        shell=False,
    )
    return float(completed.stdout.strip())


__all__ = [
    "CosyVoiceProvider",
    "describe_cosyvoice_error",
    "display_width",
    "probe_cosyvoice_readiness",
    "rate_to_speed",
    "split_narration",
]
