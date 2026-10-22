"""Pinned offline Kokoro Chinese speech on sherpa-onnx's CPU runtime."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import re
import stat
import subprocess
import tempfile
import wave
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from glance_core.settings import resolve_kokoro_voice

from glance_media.tts.base import TtsResult

RUNTIME_VERSION = "1.13.8"
MODEL_ID = "kokoro-v1.1-zh"
MANIFEST_NAME = "bundle-manifest.json"
_RUNTIME_MANIFEST = {
    "distribution": "sherpa-onnx", "version": RUNTIME_VERSION, "core_version": RUNTIME_VERSION,
    "python": "cp311", "platform": "win_amd64", "provider": "cpu",
    "bundled_onnxruntime_version": "1.28.2",
}
# Preserve the pinned upstream generator's normalization order.
_RULE_FILES = ("phone-zh.fst", "date-zh.fst", "number-zh.fst")
_SUPPORTING_LICENSE_DOCUMENTS = {
    "licenses/Kokoro-MODEL_CARD.md", "licenses/THIRD_PARTY_NOTICES.txt",
}
_REQUIRED_FILES = {
    "model.onnx": "model", "voices.bin": "voices", "tokens.txt": "tokens",
    "lexicon-us-en.txt": "lexicon", "lexicon-zh.txt": "lexicon",
    "date-zh.fst": "rule", "number-zh.fst": "rule", "phone-zh.fst": "rule",
}
_PERCENT = re.compile(r"([+-]?\d+(?:\.\d+)?)%")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SENTENCE_END = re.compile(r'''(?:[。！？!?]+|\.(?=["'”’」』）)]*(?:\s|$)))["'”’」』）)]*\s*''')


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _reject_redirect(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Kokoro bundle contains a redirected path")


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError("Kokoro manifest contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("Kokoro manifest contains an unsafe path")
    if path.as_posix() != value:
        raise ValueError("Kokoro manifest contains a noncanonical path")
    return value


def verify_kokoro_bundle(model_dir: str) -> dict:
    """Verify every file, including all text and runtime license evidence."""
    if not model_dir or not Path(model_dir).is_absolute():
        raise ValueError("Kokoro requires an absolute server-configured model directory")
    root = Path(model_dir)
    for path in (root, *root.parents):
        _reject_redirect(path)
    root = root.resolve(strict=True)
    manifest_path = root / MANIFEST_NAME
    _reject_redirect(manifest_path)
    if manifest_path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("Kokoro manifest is too large")
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if (
        not isinstance(manifest, dict) or manifest.get("schema_version") != 1
        or manifest.get("model_id") != MODEL_ID or manifest.get("runtime") != _RUNTIME_MANIFEST
    ):
        raise ValueError("Kokoro model/runtime manifest is not the pinned contract")
    rows = manifest.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Kokoro manifest files are missing")
    declared: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Kokoro manifest file entry is invalid")
        relative = _relative_path(row.get("path"))
        if relative == MANIFEST_NAME or relative.casefold() in {p.casefold() for p in declared}:
            raise ValueError("Kokoro manifest contains duplicate files")
        if not _SHA256.fullmatch(str(row.get("sha256", ""))):
            raise ValueError("Kokoro manifest SHA256 is invalid")
        size = row.get("size_bytes")
        if type(size) is not int or size <= 0 or not isinstance(row.get("role"), str):
            raise ValueError("Kokoro manifest file metadata is invalid")
        declared[relative] = row
    actual = set()
    for path in root.rglob("*"):
        _reject_redirect(path)
        if path.is_file() and path.name != MANIFEST_NAME:
            actual.add(path.relative_to(root).as_posix())
        elif not path.is_dir() and path != manifest_path:
            raise ValueError("Kokoro bundle contains a nonregular file")
    if actual != set(declared):
        raise ValueError("Kokoro bundle file inventory does not match manifest")
    for relative, row in declared.items():
        path = root / relative
        if path.stat().st_size != row["size_bytes"] or _sha256(path) != row["sha256"]:
            raise ValueError(f"Kokoro bundle checksum mismatch: {relative}")
    if any(declared.get(path, {}).get("role") != role for path, role in _REQUIRED_FILES.items()):
        raise ValueError("Kokoro bundle required files are missing")
    if not any(path.startswith("espeak-ng-data/") for path in declared):
        raise ValueError("Kokoro bundle phonemizer data is missing")
    if not any(path.startswith("dict/") for path in declared):
        raise ValueError("Kokoro bundle Chinese dictionary is missing")
    licenses = manifest.get("licenses")
    if not isinstance(licenses, list) or not licenses:
        raise ValueError("Kokoro bundle license evidence is missing")
    frozen_licenses, total_size = [], 0
    for license_row in licenses:
        if not isinstance(license_row, dict):
            raise ValueError("Kokoro license entry is invalid")
        relative = _relative_path(license_row.get("path"))
        row = declared.get(relative, {})
        if row.get("role") != "license" or license_row.get("sha256") != row.get("sha256"):
            raise ValueError("Kokoro license hash does not match manifest")
        for key in ("spdx_id", "scope", "source_url"):
            if not isinstance(license_row.get(key), str) or not license_row[key].strip():
                raise ValueError("Kokoro license attribution is incomplete")
        total_size += row["size_bytes"]
        if row["size_bytes"] > 1024 * 1024 or total_size > 4 * 1024 * 1024:
            raise ValueError("Kokoro license evidence exceeds receipt size limit")
        text = (root / relative).read_bytes().decode("utf-8")
        if not text.strip():
            raise ValueError("Kokoro license text is empty")
        frozen_licenses.append({**license_row, "text": text})
    licensed_paths = {row["path"] for row in frozen_licenses}
    supporting_paths = {path for path, row in declared.items() if row["role"] == "license"} - licensed_paths
    if supporting_paths - _SUPPORTING_LICENSE_DOCUMENTS or len(licensed_paths) != len(frozen_licenses):
        raise ValueError("Kokoro bundle has unaccounted license files")
    supporting_documents = []
    for relative in sorted(supporting_paths):
        row = declared[relative]
        total_size += row["size_bytes"]
        if row["size_bytes"] > 1024 * 1024 or total_size > 4 * 1024 * 1024:
            raise ValueError("Kokoro license evidence exceeds receipt size limit")
        text = (root / relative).read_bytes().decode("utf-8")
        if not text.strip():
            raise ValueError("Kokoro supporting license document is empty")
        supporting_documents.append({"path": relative, "sha256": row["sha256"], "text": text,
                                     "kind": "supporting_document"})
    return {
        "model_id": MODEL_ID, "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "runtime": manifest["runtime"], "files": rows, "licenses": frozen_licenses,
        "supporting_license_documents": supporting_documents,
        "source": manifest.get("source", {}),
        "publisher_rights_summary": manifest.get("rights_summary", ""),
        "provenance_limits": manifest.get("provenance_limits", []),
        "rights_summary": "Bundled license texts and source attribution are recorded; "
        "this receipt does not independently establish speaker consent or legal authorization.",
    }


def kokoro_parameters(voice: str, rate: str, volume: str) -> tuple[int, float, float]:
    _, sid = resolve_kokoro_voice(voice)
    values = []
    for name, raw, low, high in (("rate", rate, 0.5, 2.0), ("volume", volume, 0.01, 2.0)):
        match = _PERCENT.fullmatch(raw.strip()) if isinstance(raw, str) else None
        if match is None:
            raise ValueError(f"Invalid Kokoro {name} percentage")
        value = 1 + float(match.group(1)) / 100
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Kokoro {name} is out of range")
        values.append(value)
    return sid, values[0], values[1]


def _runtime():
    for name in ("sherpa-onnx", "sherpa-onnx-core"):
        try:
            version = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError("Install the pinned optional kokoro extra before local speech") from exc
        if version != RUNTIME_VERSION:
            raise RuntimeError("Kokoro requires the pinned sherpa-onnx 1.13.8 runtime")
    import sherpa_onnx

    if sherpa_onnx.__version__ != RUNTIME_VERSION:
        raise RuntimeError("Loaded sherpa-onnx runtime version does not match its distribution")
    return sherpa_onnx


def _sentences(narration: str) -> list[str]:
    ends = []
    for match in _SENTENCE_END.finditer(narration):
        # shortcut: common English titles only; broader English prose needs a dedicated segmenter.
        if (narration[match.start()] == "."
                and re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr)$", narration[:match.start()], re.IGNORECASE)):
            continue
        ends.append(match.end())
    if not ends or ends[-1] != len(narration):
        ends.append(len(narration))
    return [narration[start:end] for start, end in zip([0, *ends[:-1]], ends, strict=True)]


class KokoroProvider:
    name = "kokoro"

    def __init__(self, model_dir: str) -> None:
        self.model_dir = model_dir

    def synthesize(
        self, narration: str, output_path: Path, *, voice: str, rate: str, volume: str,
        target_duration_sec: float | None = None,
    ) -> TtsResult:
        del target_duration_sec
        sid, speed, gain = kokoro_parameters(voice, rate, volume)
        voice_name, _ = resolve_kokoro_voice(voice)
        if not isinstance(narration, str) or not narration.strip() or "\0" in narration:
            raise ValueError("Kokoro narration must contain nonempty text")
        bundle = verify_kokoro_bundle(self.model_dir)
        runtime = _runtime()
        root = Path(self.model_dir)
        config = runtime.OfflineTtsConfig(
            model=runtime.OfflineTtsModelConfig(
                kokoro=runtime.OfflineTtsKokoroModelConfig(
                    model=str(root / "model.onnx"), voices=str(root / "voices.bin"),
                    tokens=str(root / "tokens.txt"), data_dir=str(root / "espeak-ng-data"),
                    dict_dir=str(root / "dict"),
                    lexicon=",".join(str(root / name) for name in ("lexicon-us-en.txt", "lexicon-zh.txt")),
                ), num_threads=2, provider="cpu",
            ),
            rule_fsts=",".join(str(root / name) for name in _RULE_FILES),
        )
        if not config.validate():
            raise RuntimeError("Kokoro CPU model configuration is invalid")
        generated_at = datetime.now(timezone.utc).isoformat()
        import numpy as np

        engine = runtime.OfflineTts(config)
        chunks, sentences = [], []
        text_start, sample_start, peak = 0, 0, 0.0
        # Keep model articulation at 1x; apply the requested tempo after complete sentences.
        for text in _sentences(narration):
            audio = engine.generate(text, sid=sid, speed=1.0)
            samples = np.asarray(audio.samples, dtype=np.float32)
            if (
                audio.sample_rate != 24000 or samples.ndim != 1 or not samples.size
                or not np.isfinite(samples).all()
            ):
                raise RuntimeError("Kokoro returned invalid PCM samples or sample rate")
            samples = samples * gain
            chunk_peak = float(np.max(np.abs(samples)))
            if not math.isfinite(chunk_peak) or chunk_peak <= 0 or chunk_peak > 1.0:
                raise RuntimeError("Kokoro PCM is silent or would clip")
            pcm_chunk = np.rint(samples * 32767).astype("<i2")
            chunks.append(pcm_chunk)
            sentences.append({
                "text_start_char": text_start, "text_end_char": text_start + len(text),
                "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "pcm_start_sample": sample_start, "pcm_end_sample": sample_start + int(samples.size),
                "pcm16_sha256": hashlib.sha256(pcm_chunk.tobytes()).hexdigest(),
            })
            text_start += len(text)
            sample_start += int(samples.size)
            peak = max(peak, chunk_peak)
        pcm = np.concatenate(chunks)
        duration = pcm.size / 24000
        output_path.parent.mkdir(parents=True, exist_ok=True)
        ffmpeg_version = subprocess.run(
            ["ffmpeg", "-version"], capture_output=True, text=True, timeout=60, check=True, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        if not ffmpeg_version.strip():
            raise RuntimeError("Kokoro conversion version evidence is empty")
        with tempfile.TemporaryDirectory(prefix="glance-kokoro-") as directory:
            wav_path = Path(directory) / "narration.wav"
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(pcm.tobytes())
            command = ["ffmpeg", "-nostdin", "-y", "-i", str(wav_path)]
            if speed != 1.0:
                command.extend(["-af", f"atempo={speed}"])
            command.extend(["-codec:a", "libmp3lame", "-q:a", "2", str(output_path)])
            subprocess.run(
                command,
                capture_output=True, text=True, timeout=120, check=True, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError("Kokoro conversion did not produce audio")
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(output_path)],
            capture_output=True, text=True, timeout=60, check=True, shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        encoded_duration = float(json.loads(probe.stdout)["format"]["duration"])
        if not math.isfinite(encoded_duration) or encoded_duration <= 0:
            raise RuntimeError("Kokoro encoded audio duration is invalid")
        receipt = {
            "schema_version": 2, "provider": self.name, "generated_at": generated_at,
            "bundle": bundle, "runtime": {**bundle["runtime"], "provider": "cpu", "num_threads": 2},
            "voice": {"id": voice_name, "sid": sid},
            "narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
            "parameters": {"rate": rate, "requested_speed": speed, "model_speed": 1.0,
                           "tempo_factor": speed, "volume": volume, "gain": gain,
                           "rule_fsts": list(_RULE_FILES)},
            "synthesis": {"strategy": "sentence-model-1x-atempo-v1", "sentence_count": len(sentences),
                          "sentences": sentences},
            "audio": {"pcm_stage": "pre_tempo", "sample_rate": 24000, "sample_count": int(pcm.size),
                      "pcm_duration_sec": duration, "encoded_duration_sec": encoded_duration,
                      "peak_amplitude": peak,
                      "pcm16_sha256": hashlib.sha256(pcm.tobytes()).hexdigest()},
            "conversion": {"command": command, "ffmpeg_version": ffmpeg_version,
                           "tempo_filter": f"atempo={speed}" if speed != 1.0 else None},
            "output_sha256": _sha256(output_path), "output_size_bytes": output_path.stat().st_size,
        }
        return TtsResult(output_path, encoded_duration, [], self.name, provenance=receipt)
