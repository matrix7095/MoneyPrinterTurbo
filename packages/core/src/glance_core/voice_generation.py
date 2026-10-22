"""Verify content-local speech receipts against signed, server-owned lineage."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import stat
from pathlib import Path

from glance_core.models import AgentTask, Artifact, AuditEvent, Content, StageRun, StageStatus, TaskStatus, TaskType
from glance_core.settings import KOKORO_VOICES, Settings, resolve_kokoro_voice
from glance_core.task_service import stable_hash
from sqlmodel import Session

GENERATION_ACTION = "voice_generation_bound"
_SHA = re.compile(r"[0-9a-f]{64}")
_RUNTIME = {
    "distribution": "sherpa-onnx", "version": "1.13.8", "core_version": "1.13.8",
    "python": "cp311", "platform": "win_amd64", "provider": "cpu",
    "bundled_onnxruntime_version": "1.28.2",
}


def artifact_binding(artifact: Artifact) -> dict:
    return {key: getattr(artifact, key) for key in ("id", "relative_path", "sha256", "size_bytes")}


def script_binding(script: AgentTask) -> dict:
    return {"id": script.id, "input_hash": script.input_hash, "result_hash": stable_hash(script.result_data),
            "status": script.status.value, "attempt": script.attempt}


def verified_artifact_bytes(content: Content, artifact: Artifact, *, limit: int | None = None) -> bytes:
    root = Path(content.content_dir).resolve(strict=True)
    relative = artifact.relative_path
    if (artifact.content_id != content.id or not isinstance(relative, str) or not relative
            or "\\" in relative or ":" in relative or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
        raise ValueError("Voice evidence artifact path is unsafe")
    path = root / relative
    for parent in (path, *path.parents):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Voice evidence artifact path is redirected")
        if parent == root:
            break
    if (not path.is_file() or path.stat().st_size != artifact.size_bytes
            or (limit is not None and artifact.size_bytes > limit)):
        raise ValueError("Voice evidence artifact size is invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ValueError("Voice evidence artifact bytes changed")
    return data


def _signature(settings: Settings, event: AuditEvent) -> str:
    secret = settings.admin_token.get_secret_value()
    if not secret.strip() or secret == "change-me-before-network-use":
        raise ValueError("Voice generation binding requires the configured admin signing secret")
    unsigned = {key: value for key, value in event.metadata_json.items() if key != "signature"}
    message = stable_hash({"event_id": event.id, "content_id": event.entity_id, "receipt": unsigned})
    return hmac.new(secret.encode(), message.encode("ascii"), hashlib.sha256).hexdigest()


def _sentence_tempo_receipt(data: dict, narration: str) -> None:
    parameters, synthesis, conversion = (data.get(key) for key in ("parameters", "synthesis", "conversion"))
    if not all(isinstance(value, dict) for value in (parameters, synthesis, conversion)) or "speed" in parameters:
        raise ValueError("Voice generation sentence/tempo evidence is missing or ambiguous")
    parsed = {}
    for key, numeric_key, low, high in (("rate", "requested_speed", 0.5, 2.0), ("volume", "gain", 0.01, 2.0)):
        raw = parameters.get(key)
        match = re.fullmatch(r"([+-]?\d+(?:\.\d+)?)%", raw.strip()) if isinstance(raw, str) else None
        number = parameters.get(numeric_key)
        if (match is None or type(number) not in {int, float} or not math.isfinite(number)
                or not low <= number <= high or abs(number - (1 + float(match.group(1)) / 100)) > 1e-12):
            raise ValueError("Voice generation requested sentence/tempo parameters are inconsistent")
        parsed[key] = float(number)
    if (type(parameters.get("model_speed")) not in {int, float} or parameters["model_speed"] != 1.0
            or type(parameters.get("tempo_factor")) not in {int, float}
            or parameters["tempo_factor"] != parsed["rate"]):
        raise ValueError("Voice generation model speed and output tempo are inconsistent")
    audio = data["audio"]
    if (audio.get("pcm_stage") != "pre_tempo"
            or abs(audio["encoded_duration_sec"] - audio["pcm_duration_sec"] / parsed["rate"]) > 0.15):
        raise ValueError("Voice generation pre-tempo PCM and encoded duration are inconsistent")
    rows = synthesis.get("sentences")
    if (synthesis.get("strategy") != "sentence-model-1x-atempo-v1" or not isinstance(rows, list) or not rows
            or type(synthesis.get("sentence_count")) is not int or synthesis["sentence_count"] != len(rows)):
        raise ValueError("Voice generation sentence inventory is invalid")
    text_start, sample_start = 0, 0
    for row in rows:
        if (not isinstance(row, dict)
                or any(type(row.get(key)) is not int for key in
                       ("text_start_char", "text_end_char", "pcm_start_sample", "pcm_end_sample"))
                or row["text_start_char"] != text_start or row["pcm_start_sample"] != sample_start
                or not text_start < row["text_end_char"] <= len(narration)
                or not sample_start < row["pcm_end_sample"] <= audio["sample_count"]
                or not _SHA.fullmatch(str(row.get("pcm16_sha256", "")))):
            raise ValueError("Voice generation sentence PCM/text coverage is inconsistent")
        text = narration[text_start:row["text_end_char"]]
        if not text.strip() or row.get("text_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ValueError("Voice generation sentence text binding changed")
        text_start, sample_start = row["text_end_char"], row["pcm_end_sample"]
    if text_start != len(narration) or sample_start != audio["sample_count"]:
        raise ValueError("Voice generation sentences do not cover the complete narration/PCM")
    tempo_filter = f"atempo={parsed['rate']}" if parsed["rate"] != 1.0 else None
    command, version = conversion.get("command"), conversion.get("ffmpeg_version")
    if (not isinstance(command, list) or len(command) != (12 if tempo_filter else 10)
            or any(not isinstance(item, str) or not item for item in command)
            or command != ["ffmpeg", "-nostdin", "-y", "-i", command[4]]
            + (["-af", tempo_filter] if tempo_filter else []) + ["-codec:a", "libmp3lame", "-q:a", "2", command[-1]]
            or conversion.get("tempo_filter") != tempo_filter
            or not isinstance(version, str) or not version.startswith("ffmpeg version ")):
        raise ValueError("Voice generation FFmpeg tempo recipe/version is inconsistent")


def _receipt(content: Content, voice: Artifact, receipt: Artifact, script: AgentTask) -> dict:
    verified_artifact_bytes(content, voice)
    if (receipt.artifact_type != "tts_generation_receipt" or voice.artifact_type != "voice"
            or Path(receipt.relative_path).parent != Path(voice.relative_path).parent):
        raise ValueError("Voice generation receipt has the wrong artifact identity")
    data = json.loads(verified_artifact_bytes(content, receipt, limit=8 * 1024 * 1024))
    narration = (script.result_data or {}).get("full_narration")
    if not isinstance(narration, str):
        raise ValueError("Voice generation approved narration is unavailable")
    if (not isinstance(data, dict) or data.get("schema_version") not in (1, 2) or data.get("provider") != "kokoro"
            or data.get("output_sha256") != voice.sha256 or data.get("output_size_bytes") != voice.size_bytes
            or data.get("narration_sha256") != hashlib.sha256(narration.encode("utf-8")).hexdigest()):
        raise ValueError("Voice generation receipt does not match approved narration/audio")
    selected_voice = data.get("voice", {})
    if (not isinstance(selected_voice, dict) or set(selected_voice) != {"id", "sid"}
            or selected_voice.get("id") not in KOKORO_VOICES
            or type(selected_voice.get("sid")) is not int
            or selected_voice["sid"] != KOKORO_VOICES[selected_voice["id"]]):
        raise ValueError("Voice generation canonical Kokoro voice/name pair is invalid")
    runtime, bundle = data.get("runtime", {}), data.get("bundle", {})
    if (not isinstance(runtime, dict) or runtime != {**_RUNTIME, "num_threads": 2}
            or not isinstance(bundle, dict) or bundle.get("runtime") != _RUNTIME
            or bundle.get("model_id") != "kokoro-v1.1-zh"
            or not _SHA.fullmatch(str(bundle.get("manifest_sha256", "")))):
        raise ValueError("Voice generation pinned model/runtime identity is invalid")
    if not isinstance(data.get("generated_at"), str) or not data["generated_at"]:
        raise ValueError("Voice generation timestamp is missing")
    audio = data.get("audio", {})
    if not isinstance(audio, dict) or audio.get("sample_rate") != 24000:
        raise ValueError("Voice generation sample rate is invalid")
    for key in ("sample_count", "pcm_duration_sec", "encoded_duration_sec", "peak_amplitude"):
        number = audio.get(key)
        if type(number) not in {float, int} or not math.isfinite(number) or number <= 0:
            raise ValueError("Voice generation PCM/duration metadata is invalid")
    if (type(audio["sample_count"]) is not int or audio["peak_amplitude"] > 1
            or abs(audio["sample_count"] / 24000 - audio["pcm_duration_sec"]) > 1e-8
            or not _SHA.fullmatch(str(audio.get("pcm16_sha256", "")))):
        raise ValueError("Voice generation PCM metadata is inconsistent")
    rows = bundle.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Voice generation model file inventory is missing")
    files = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("path"), str)
                or row["path"] in files or not _SHA.fullmatch(str(row.get("sha256", "")))
                or type(row.get("size_bytes")) is not int or row["size_bytes"] <= 0):
            raise ValueError("Voice generation model file inventory is invalid")
        files[row["path"]] = row
    licenses, documents = bundle.get("licenses"), bundle.get("supporting_license_documents")
    if not isinstance(licenses, list) or not licenses or not isinstance(documents, list):
        raise ValueError("Voice generation license evidence is missing")
    seen, total = set(), 0
    for row in [*licenses, *documents]:
        if not isinstance(row, dict) or not isinstance(row.get("path"), str) or row["path"] in seen:
            raise ValueError("Voice generation license metadata is invalid")
        seen.add(row["path"])
        file = files.get(row["path"], {})
        text = row.get("text")
        if (not isinstance(text, str) or not text.strip() or file.get("role") != "license"
                or row.get("sha256") != file.get("sha256")):
            raise ValueError("Voice generation license file binding is invalid")
        encoded = text.encode("utf-8")
        total += len(encoded)
        if (len(encoded) != file["size_bytes"] or len(encoded) > 1024 * 1024 or total > 4 * 1024 * 1024
                or hashlib.sha256(encoded).hexdigest() != row["sha256"]):
            raise ValueError("Voice generation embedded license bytes changed")
    if seen != {path for path, row in files.items() if row.get("role") == "license"}:
        raise ValueError("Voice generation license inventory is incomplete")
    for row in licenses:
        if any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("spdx_id", "scope", "source_url")):
            raise ValueError("Voice generation license attribution is incomplete")
    if data["schema_version"] == 2:
        _sentence_tempo_receipt(data, narration)
    return data


def bind_voice_generation(
    session: Session, settings: Settings, content: Content, voice: Artifact, receipt: Artifact,
    script: AgentTask, stage: StageRun, *, operation: str, previous_plan: dict | None = None,
) -> tuple[AuditEvent, dict]:
    data = _receipt(content, voice, receipt, script)
    name, sid = resolve_kokoro_voice(settings.tts_voice)
    parameters = data.get("parameters", {})
    if (data["voice"] != {"id": name, "sid": sid}
            or not isinstance(parameters, dict) or parameters.get("rate") != settings.tts_rate
            or parameters.get("volume") != settings.tts_volume):
        raise ValueError("Voice generation response does not match requested voice/parameters")
    if (stage.content_id != content.id or stage.stage != "voice"
            or stage.status not in {StageStatus.RUNNING, StageStatus.SUCCESS}):
        raise ValueError("Voice generation producing stage is invalid")
    event = AuditEvent(actor_type="executor", actor_id="mpt-kokoro-generation", entity_type="content",
                       entity_id=content.id, action=GENERATION_ACTION, metadata_json={
                           "contract_version": 1, "producer": "mpt-kokoro-generation", "operation": operation,
                           "script": script_binding(script), "voice_artifact": artifact_binding(voice),
                           "receipt_artifact": artifact_binding(receipt), "stage_id": stage.id,
                           "voice_input_hash": stage.input_hash, "previous_plan": previous_plan,
                           "model_manifest_sha256": data["bundle"]["manifest_sha256"],
                           "narration_sha256": data["narration_sha256"],
                       })
    event.metadata_json = {**event.metadata_json, "signature": _signature(settings, event)}
    session.add(event)
    return event, _summary(event, data, receipt)


def _summary(event: AuditEvent, data: dict, receipt: Artifact) -> dict:
    bundle = data["bundle"]
    summary = {
        "binding_event_id": event.id, "receipt_artifact": artifact_binding(receipt),
        "provider": "kokoro", "actual_provider_verified": True,
        "generated_at": data["generated_at"], "model_id": bundle["model_id"],
        "model_manifest_sha256": bundle["manifest_sha256"], "runtime": data["runtime"],
        "voice": data["voice"], "narration_sha256": data["narration_sha256"],
        "script": event.metadata_json["script"], "stage_id": event.metadata_json["stage_id"],
        "voice_input_hash": event.metadata_json["voice_input_hash"],
        "parameters": data.get("parameters", {}), "audio": data["audio"],
        "licenses": [{key: row[key] for key in ("path", "sha256", "spdx_id", "scope", "source_url")}
                     for row in bundle["licenses"]],
        "supporting_license_documents": [{"path": row["path"], "sha256": row["sha256"]}
                                         for row in bundle["supporting_license_documents"]],
        "rights_summary": bundle.get("rights_summary", ""),
        "publisher_rights_summary": bundle.get("publisher_rights_summary", ""),
        "provenance_limits": bundle.get("provenance_limits", []),
        "limitation": "Verified local generation and retained license texts do not independently prove speaker consent or legal authorization.",
    }
    if data["schema_version"] == 2:
        summary.update(receipt_schema_version=2, synthesis=data["synthesis"], conversion=data["conversion"])
    return summary


def verify_voice_generation(
    session: Session, settings: Settings, content: Content, voice_binding: dict,
    generation: dict, script: AgentTask,
) -> tuple[dict, Artifact]:
    if not isinstance(generation, dict):
        raise ValueError("Voice generation summary is missing")
    event = session.get(AuditEvent, generation.get("binding_event_id", ""))
    if (event is None or event.entity_type != "content" or event.entity_id != content.id
            or event.action != GENERATION_ACTION or event.actor_type != "executor"
            or event.actor_id != "mpt-kokoro-generation"):
        raise ValueError("Voice generation signed binding is missing")
    metadata = event.metadata_json
    if not isinstance(metadata, dict):
        raise ValueError("Voice generation signed metadata is invalid")
    signature = metadata.get("signature")
    if (metadata.get("contract_version") != 1 or metadata.get("producer") != "mpt-kokoro-generation"
            or not isinstance(signature, str) or not _SHA.fullmatch(signature)
            or not hmac.compare_digest(signature, _signature(settings, event))
            or metadata.get("voice_artifact") != voice_binding
            or script.content_id != content.id or script.task_type != TaskType.SHORT_SCRIPT
            or script.status != TaskStatus.COMPLETED or stable_hash(script.input_data) != script.input_hash
            or metadata.get("script") != script_binding(script)):
        raise ValueError("Voice generation signature or approved script binding changed")
    voice = session.get(Artifact, voice_binding.get("id", ""))
    receipt_binding = metadata.get("receipt_artifact", {})
    receipt = session.get(Artifact, receipt_binding.get("id", ""))
    stage = session.get(StageRun, metadata.get("stage_id", ""))
    if (voice is None or receipt is None or artifact_binding(voice) != voice_binding
            or artifact_binding(receipt) != receipt_binding or stage is None or stage.content_id != content.id
            or stage.stage != "voice" or stage.status != StageStatus.SUCCESS
            or stage.input_hash != metadata.get("voice_input_hash")):
        raise ValueError("Voice generation registered artifacts/stage changed")
    data = _receipt(content, voice, receipt, script)
    if (data["bundle"]["manifest_sha256"] != metadata.get("model_manifest_sha256")
            or data["narration_sha256"] != metadata.get("narration_sha256")):
        raise ValueError("Voice generation model/narration binding changed")
    summary = _summary(event, data, receipt)
    if stable_hash(summary) != stable_hash(generation):
        raise ValueError("Voice generation summary changed")
    return summary, receipt
