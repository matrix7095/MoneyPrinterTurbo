from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class TtsResult:
    audio_path: Path
    duration_sec: float
    boundaries: list[dict] = field(default_factory=list)
    provider: str = ""
    fallback_reason: str = ""

    def __iter__(self):
        """Keep the former ``path, duration = synthesize_voice(...)`` API compatible."""
        yield self.audio_path
        yield self.duration_sec


class TtsProvider(Protocol):
    name: str

    def synthesize(
        self,
        narration: str,
        output_path: Path,
        *,
        voice: str,
        rate: str,
        volume: str,
        target_duration_sec: float | None = None,
    ) -> TtsResult: ...
