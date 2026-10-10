from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import edge_tts

from glance_media.tts.base import TtsResult

_BOUNDARY_TYPES = frozenset({"WordBoundary", "SentenceBoundary"})


def _duration(path: Path) -> float:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
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
    return float(json.loads(completed.stdout)["format"]["duration"])


class EdgeTtsProvider:
    name = "edge"

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
        del target_duration_sec
        boundaries = asyncio.run(
            self._stream(narration, output_path, voice=voice, rate=rate, volume=volume)
        )
        return TtsResult(
            audio_path=output_path,
            duration_sec=_duration(output_path),
            boundaries=boundaries,
            provider=self.name,
        )

    async def _stream(
        self,
        narration: str,
        output_path: Path,
        *,
        voice: str,
        rate: str,
        volume: str,
    ) -> list[dict[str, Any]]:
        communicate = edge_tts.Communicate(narration, voice=voice, rate=rate, volume=volume)
        boundaries: list[dict[str, Any]] = []
        with output_path.open("wb") as handle:
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    handle.write(chunk["data"])
                elif chunk["type"] in _BOUNDARY_TYPES:
                    boundaries.append(
                        {
                            "kind": chunk["type"],
                            "start_sec": chunk["offset"] / 10_000_000,
                            "end_sec": (chunk["offset"] + chunk["duration"]) / 10_000_000,
                            "text": str(chunk["text"]),
                        }
                    )
        return boundaries
