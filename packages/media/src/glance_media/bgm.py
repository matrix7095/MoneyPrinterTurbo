from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

_AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
_DEFAULT_VOLUME = 0.15
BGM_MOODS = frozenset({"upbeat_product", "tech", "warm", "hype", "none"})
HOOK_TYPE_BGM_MOOD = {
    "number_shock": "hype",
    "contrast": "hype",
    "conflict": "hype",
    "open_loop": "tech",
    "identity": "warm",
}


@dataclass(frozen=True)
class BgmTrack:
    id: str
    path: Path
    title: str = ""
    license: str = ""
    mood: str = ""


def load_catalog(root: Path) -> list[BgmTrack]:
    """Load licensed beds from catalog.json and any audio dropped next to it."""
    if not root.is_dir():
        return []
    found: dict[str, BgmTrack] = {}
    catalog_path = root / "catalog.json"
    if catalog_path.is_file():
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        for item in payload.get("tracks", []):
            if not isinstance(item, dict):
                continue
            relative = str(item.get("file") or "").strip()
            if not relative:
                continue
            path = (root / relative).resolve()
            if not path.is_file():
                continue
            track_id = str(item.get("id") or path.stem).strip() or path.stem
            mood = str(item.get("mood") or "").strip()
            found[track_id] = BgmTrack(
                id=track_id,
                path=path,
                title=str(item.get("title") or path.stem),
                license=str(item.get("license") or ""),
                mood=mood if mood in BGM_MOODS else "",
            )
    known_files = {track.path.resolve() for track in found.values()}
    for directory in (root, root / "tracks"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.iterdir()):
            if path.suffix.lower() not in _AUDIO_SUFFIXES or not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in known_files:
                continue
            track_id = path.stem
            if track_id in found:
                continue
            found[track_id] = BgmTrack(id=track_id, path=resolved, title=path.stem)
            known_files.add(resolved)
    return list(found.values())


def resolve_bgm_mood(script: dict[str, object] | None) -> str:
    payload = script or {}
    mood = str(payload.get("bgm_mood") or "").strip()
    if mood in BGM_MOODS:
        return mood
    hook = str(payload.get("hook_type") or "").strip()
    return HOOK_TYPE_BGM_MOOD.get(hook, "upbeat_product")


def pick_track(
    tracks: list[BgmTrack],
    *,
    track_id: str = "",
    seed: str = "",
    mood: str = "",
) -> BgmTrack | None:
    wanted = track_id.strip()
    if wanted:
        for track in tracks:
            if track.id == wanted:
                return track
    if mood == "none":
        return None
    pool = tracks
    if mood:
        tagged = [track for track in tracks if track.mood == mood]
        if not tagged and mood != "upbeat_product":
            tagged = [track for track in tracks if track.mood == "upbeat_product"]
        if tagged:
            pool = tagged
    if not pool:
        return None
    if not seed:
        return pool[0]
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return pool[int(digest, 16) % len(pool)]


def clamp_volume(volume: float) -> float:
    return min(0.4, max(0.05, volume))


def mix_filter_complex(
    duration_sec: float,
    volume: float = _DEFAULT_VOLUME,
    *,
    duck: bool = True,
) -> str:
    duration = max(float(duration_sec), 0.2)
    fade_in = min(0.4, duration / 4)
    fade_out = min(1.2, duration / 3)
    fade_out_start = max(duration - fade_out, 0.0)
    music = (
        "[1:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
        f"atrim=0:{duration:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:st=0:d={fade_in:.3f},"
        f"afade=t=out:st={fade_out_start:.3f}:d={fade_out:.3f},"
        f"volume={clamp_volume(volume)}[bg]"
    )
    voice = "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[vo]"
    if duck:
        ducked = "[bg][vo]sidechaincompress=threshold=0.05:ratio=6:attack=20:release=500[ducked]"
        mixed = (
            "[vo][ducked]amix=inputs=2:duration=first:dropout_transition=0,"
            "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
        )
        return f"{music};{voice};{ducked};{mixed}"
    mixed = (
        "[vo][bg]amix=inputs=2:duration=first:dropout_transition=0,"
        "loudnorm=I=-16:TP=-1.5:LRA=11[aout]"
    )
    return f"{music};{voice};{mixed}"
