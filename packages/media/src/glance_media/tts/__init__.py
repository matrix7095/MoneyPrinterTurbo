from __future__ import annotations

from collections.abc import Callable

from glance_media.tts.base import TtsProvider, TtsResult
from glance_media.tts.cosyvoice import CosyVoiceProvider
from glance_media.tts.edge import EdgeTtsProvider

_PROVIDERS: dict[str, Callable[[str], TtsProvider]] = {
    "edge": lambda _endpoint: EdgeTtsProvider(),
    "cosyvoice3": lambda endpoint: CosyVoiceProvider(endpoint),
}


def register_tts_provider(name: str, factory: Callable[[str], TtsProvider]) -> None:
    """Extension seam for future providers without coupling MediaExecutor to them."""
    _PROVIDERS[name.strip().casefold()] = factory


def get_tts_provider(name: str, endpoint: str = "", token: str = "") -> TtsProvider:
    key = name.strip().casefold() or "edge"
    if key != "edge" and not endpoint.strip():
        raise LookupError(f"TTS Provider {key} 未配置 endpoint")
    factory = _PROVIDERS.get(key)
    if factory is None:
        raise LookupError(f"TTS Provider 未注册：{key}")
    if key == "cosyvoice3":
        return CosyVoiceProvider(endpoint, token)
    return factory(endpoint)


__all__ = [
    "CosyVoiceProvider",
    "EdgeTtsProvider",
    "TtsProvider",
    "TtsResult",
    "get_tts_provider",
    "register_tts_provider",
]
