"""Register retained historical adaptation bytes without reviving a generation lease."""
from __future__ import annotations

import hashlib
import io
import json
import stat
import warnings
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from zipfile import BadZipFile, ZIP_STORED, ZipFile

from glance_agent.image_preparation import _dimensions, _plain_path
from glance_core.content_acceptance import _unchanged_material_task, completed_image_origin
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, TaskType, as_utc
from glance_core.task_service import stable_hash
from PIL import Image, ImageOps, __version__ as pillow_version
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.business.content_host import offer_receipt

MAX_BODY = 26 * 1024 * 1024
MAX_IMAGE = 25 * 1024 * 1024
MAX_JSON = 256 * 1024
ACTION = "image_preparation_registered"
METHOD = "Pillow EXIF orientation, LANCZOS proportional resize with centered padding; no added detail or visual QC"
SHA = r"^[0-9a-f]{64}$"


class PreparationBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    task_id: str = Field(pattern=r"^agt_[0-9a-f]{32}$")
    attempt: int = Field(ge=1)
    input_sha256: str = Field(pattern=SHA)
    result_sha256: str = Field(pattern=SHA)
    accepted_raw_sha256: str = Field(pattern=SHA)
    prepared_artifact_id: str = Field(pattern=r"^asset_[0-9a-f]{32}$")
    prepared_sha256: str = Field(pattern=SHA)
    segment_index: int = Field(ge=0)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{32}$")
    run_generation: int = Field(ge=1)
    permission_event_id: str = Field(pattern=r"^aud_[0-9a-f]{32}$")


class PreparationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    status: Literal["PREPARED"]
    task_id: str
    segment_index: int = Field(ge=0)
    path: str = Field(max_length=4096)
    source_path: str = Field(max_length=4096)
    width: int = Field(gt=0, le=16384)
    height: int = Field(gt=0, le=16384)
    source_width: int = Field(gt=0, le=16384)
    source_height: int = Field(gt=0, le=16384)
    source_sha256: str = Field(pattern=SHA)
    input_sha256: str = Field(pattern=SHA)
    sha256: str = Field(pattern=SHA)
    method: Literal[METHOD]
    upscaled: bool


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(raw: bytes):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON fields are not accepted")
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except RecursionError as exc:
        raise ValueError("JSON nesting exceeds the bounded evidence format") from exc


def read_archive(body: bytes) -> tuple[PreparationBinding, PreparationReceipt, bytes, bytes]:
    if not body or len(body) > MAX_BODY:
        raise ValueError("Image preparation archive exceeds the bounded upload size")
    try:
        with ZipFile(io.BytesIO(body)) as zipped:
            entries = zipped.infolist()
            names = [entry.filename for entry in entries]
            if len(names) != 3 or set(names) != {"manifest.json", "preparation.json", "original.bin"}:
                raise ValueError("Image preparation archive must contain exactly the three fixed members")
            for entry in entries:
                mode = entry.external_attr >> 16
                if (entry.compress_type != ZIP_STORED or entry.flag_bits & 1 or entry.is_dir()
                        or stat.S_ISLNK(mode) or (entry.external_attr & 0x400)
                        or entry.file_size != entry.compress_size
                        or entry.file_size > (MAX_IMAGE if entry.filename == "original.bin" else MAX_JSON)):
                    raise ValueError("Unsafe image preparation archive member")
            manifest = PreparationBinding.model_validate(_json(zipped.read("manifest.json")))
            raw_receipt = zipped.read("preparation.json")
            receipt = PreparationReceipt.model_validate(_json(raw_receipt))
            original = zipped.read("original.bin")
    except (BadZipFile, UnicodeError, OSError) as exc:
        raise ValueError("Invalid image preparation archive") from exc
    if not original:
        raise ValueError("Original image bytes are empty")
    return manifest, receipt, original, raw_receipt


def _safe(path: Path, root: Path) -> Path:
    try:
        _plain_path(path)
    except ValueError as exc:
        raise ValueError("Image preparation evidence refuses redirected paths") from exc
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("Image preparation evidence path escapes content root")
    return resolved


def _verify(session, settings, content_id: str, prepared_id: str, binding: PreparationBinding):
    content = session.get(Content, content_id)
    task = session.get(AgentTask, binding.task_id)
    artifact = session.get(Artifact, prepared_id)
    if (content is None or content.business_status != ContentStatus.PACKAGED
            or content.production_mode != ProductionMode.IMAGE_ONLY
            or prepared_id != binding.prepared_artifact_id or artifact is None
            or artifact.content_id != content_id or artifact.artifact_type != "agent_generated_image"
            or artifact.sha256 != binding.prepared_sha256 or artifact.producer_stage != binding.task_id
            or task is None or task.content_id != content_id or task.task_type != TaskType.ASSET_GENERATE_IMAGE
            or task.attempt != binding.attempt or task.input_hash != binding.input_sha256
            or stable_hash(task.input_data) != binding.input_sha256
            or stable_hash(task.result_data) != binding.result_sha256
            or not completed_image_origin(session, task)):
        raise ValueError("Historical image task or prepared artifact binding changed")
    _unchanged_material_task(session, task, owned=True, consumed=True)
    markers = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
        AuditEvent.action.in_(["mpt_task_owned", "mpt_agent_consumed"]))).all()
    if any(row.actor_type != "system" for row in markers):
        raise ValueError("Historical image ownership or consumption actor changed")
    root = Path(content.content_dir).absolute()
    if not root.resolve().is_relative_to(settings.content_dir.resolve()):
        raise ValueError("Content root is outside configured storage")
    root = _safe(root, root.resolve())
    prepared_path = _safe(root / artifact.relative_path, root)
    if not 0 < artifact.size_bytes <= MAX_IMAGE or prepared_path.stat().st_size != artifact.size_bytes:
        raise ValueError("Prepared artifact exceeds the bounded image size")
    prepared = prepared_path.read_bytes()
    if len(prepared) != artifact.size_bytes or _sha(prepared) != artifact.sha256:
        raise ValueError("Actual prepared artifact bytes changed")
    images = [item for item in (task.result_data or {}).get("images", [])
              if item.get("artifact_id") == artifact.id and item.get("segment_index") == binding.segment_index]
    requests = [item for item in task.input_data.get("requests", []) if item.get("segment_index") == binding.segment_index]
    if (len(images) != 1 or len(requests) != 1 or requests[0].get("asset_request", {}).get("reference_artifact_id")):
        raise ValueError("Exact non-reference generated-image segment is unavailable")
    submissions = session.exec(select(AgentSubmission).where(
        AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
        AgentSubmission.validation_status == "ACCEPTED",
    )).all()
    if len(submissions) != 1 or submissions[0].result_hash != binding.result_sha256:
        raise ValueError("Unique accepted generation result is unavailable")
    accepted = submissions[0]
    raw_root = settings.agent_submission_dir.resolve()
    raw_path = _safe(raw_root / task.id / str(task.attempt) / "result.json", raw_root)
    recorded = Path(accepted.raw_result_path)
    if not recorded.is_absolute():
        recorded = settings.data_dir.parent / recorded
    if recorded.resolve() != raw_path:
        raise ValueError("Accepted result path binding changed")
    raw = raw_path.read_bytes()
    if _sha(raw) != binding.accepted_raw_sha256 or stable_hash(_json(raw)) != accepted.result_hash:
        raise ValueError("Accepted generation raw bytes changed")
    permission = session.get(AuditEvent, binding.permission_event_id)
    expected = {"run_id": binding.run_id, "generation": binding.run_generation, "content_id": content_id,
                "allow_local_image_adaptation": True,
                "scope": "new_ai_concept_images_frozen_minimum_size_preserve_original_and_processing_record_independent_qc"}
    if (permission is None or permission.actor_type != "admin" or permission.actor_id != "mpt-admin-api"
            or permission.entity_type != "content_run" or permission.entity_id != binding.run_id
            or permission.action != "mpt_content_run_image_adaptation_permission_recorded"
            or permission.metadata_json.get("allow_local_image_adaptation") is not True
            or any(permission.metadata_json.get(key) != value for key, value in expected.items())):
        raise ValueError("Historical run did not explicitly authorize this image adaptation")
    claims = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
        AuditEvent.action == "mpt_content_run_claim",
    )).all()
    claims = [row for row in claims if row.metadata_json.get("run_id") == binding.run_id
              and row.metadata_json.get("generation") == binding.run_generation
              and row.metadata_json.get("agent_id") == accepted.agent_id]
    if len(claims) != 1 or claims[0].actor_type != "system" or claims[0].actor_id != "mpt-content-host":
        raise ValueError("Historical image run claim is unavailable")
    claim = claims[0]
    worker, offer_hash = offer_receipt(session, claim.metadata_json.get("request_id"))
    if (claim.metadata_json.get("offer_hash") != offer_hash
            or any(worker.get(key) != value for key, value in {"task_id": task.id, "content_id": content_id,
                "input_hash": task.input_hash, "agent_id": accepted.agent_id, "run_id": binding.run_id,
                "run_generation": binding.run_generation, "host_epoch": claim.metadata_json.get("host_epoch"),
                "task_type": TaskType.ASSET_GENERATE_IMAGE.value}.items())
            or not as_utc(permission.created_at) <= as_utc(claim.created_at) <= as_utc(artifact.created_at) <= as_utc(accepted.created_at)):
        raise ValueError("Historical image authorization and offer association changed")
    uploads = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "artifact", AuditEvent.entity_id == artifact.id,
                                                  AuditEvent.action == "generated_artifact_uploaded")).all()
    if (len(uploads) != 1 or uploads[0].actor_type != "agent" or uploads[0].actor_id != accepted.agent_id
            or uploads[0].metadata_json.get("task_id") != task.id
            or uploads[0].metadata_json.get("mime_type") != artifact.mime_type):
        raise ValueError("Prepared artifact upload association changed")
    return root, task, artifact, prepared, requests[0]["asset_request"]


def _replay(original: bytes, task_input: dict, index: int, receipt: PreparationReceipt) -> str:
    width, height = _dimensions(task_input, index)
    if (receipt.source_sha256 != _sha(original) or (receipt.width, receipt.height) != (width, height)):
        raise ValueError("Original bytes or frozen preparation dimensions changed")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            with Image.open(io.BytesIO(original)) as image:
                if (image.format not in {"PNG", "JPEG", "WEBP"} or getattr(image, "n_frames", 1) != 1
                        or image.width * image.height > 64_000_000
                        or image.size != (receipt.source_width, receipt.source_height)):
                    raise ValueError("Original image format or observed dimensions are invalid")
                image.load()
                suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}[image.format]
                oriented = ImageOps.exif_transpose(image)
                if abs((oriented.width / oriented.height) / (width / height) - 1) > .02:
                    raise ValueError("Original aspect ratio exceeds frozen preparation tolerance")
                alpha = "A" in oriented.getbands() or "transparency" in oriented.info
                output = ImageOps.pad(oriented.convert("RGBA" if alpha else "RGB"), (width, height),
                                      method=Image.Resampling.LANCZOS, color=(0, 0, 0, 0) if alpha else "white")
                buffer = io.BytesIO()
                output.save(buffer, format="PNG")
                if (_sha(buffer.getvalue()) != receipt.sha256
                        or receipt.upscaled != (width > oriented.width or height > oriented.height)):
                    raise ValueError("Replayed preparation does not equal accepted prepared image bytes")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning, OSError) as exc:
            raise ValueError("Original image could not be safely replayed") from exc
    return suffix


def register_image_preparation(engine, settings, content_id: str, prepared_id: str, body: bytes) -> dict:
    binding, receipt, original, raw_receipt = read_archive(body)
    with Session(engine) as session:
        root, task, prepared, prepared_bytes, request = _verify(session, settings, content_id, prepared_id, binding)
        if (receipt.task_id != task.id or receipt.segment_index != binding.segment_index
                or receipt.sha256 != prepared.sha256):
            raise ValueError("Preparation receipt does not bind the accepted prepared artifact")
        suffix = _replay(original, task.input_data, binding.segment_index, receipt)
        with Image.open(io.BytesIO(prepared_bytes)) as observed:
            if observed.format != "PNG" or observed.size != (receipt.width, receipt.height):
                raise ValueError("Registered prepared image dimensions or format changed")
        record = {**binding.model_dump(), "source_sha256": receipt.source_sha256, "source_size_bytes": len(original),
                  "source_width": receipt.source_width, "source_height": receipt.source_height,
                  "width": receipt.width, "height": receipt.height, "method": receipt.method, "upscaled": receipt.upscaled,
                  "source_receipt_sha256": _sha(raw_receipt), "claimed_raw_input_sha256": receipt.input_sha256,
                  "raw_input_bytes_verified": False, "raw_input_limitation": "Historical workspace input bytes are not retained in this registration; this digest is an uploaded receipt claim, not the canonical task input hash.",
                  "frozen_request_sha256": stable_hash(request), "replay_verified": True, "replay_pillow_version": pillow_version,
                  "license_verified": False, "vendor_generation_receipt_verified": False,
                  "scope": "admin_registered_retained_original_and_replayed_processing_evidence"}
        record_bytes = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        digest = _sha(record_bytes)
        prior = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "artifact", AuditEvent.entity_id == prepared_id,
                                                     AuditEvent.action == ACTION)).all()
        if len(prior) > 1 or (prior and (prior[0].actor_type != "admin" or prior[0].actor_id != "mpt-admin-api"
                or prior[0].metadata_json.get("record_sha256") != digest
                or prior[0].metadata_json.get("task_id") != binding.task_id
                or prior[0].metadata_json.get("prepared_sha256") != binding.prepared_sha256
                or prior[0].metadata_json.get("source_receipt_sha256") != record["source_receipt_sha256"])):
            raise ValueError("Prepared artifact already has conflicting registered preparation evidence")
        parent = _safe(root / "image-preparations", root)
        parent.mkdir(parents=True, exist_ok=True)
        destination = _safe(parent / digest, root)
        source_name, record_name = f"original-{receipt.source_sha256}{suffix}", f"preparation-{digest}.json"
        payloads = {source_name: original, record_name: record_bytes}
        if not destination.exists():
            with TemporaryDirectory(prefix=".image-preparation-", dir=parent) as directory:
                staging = Path(directory)
                for name, payload in payloads.items():
                    (staging / name).write_bytes(payload)
                staging.rename(destination)
        for name, payload in payloads.items():
            target = _safe(destination / name, root)
            if not target.is_file() or target.read_bytes() != payload:
                raise ValueError("Immutable image preparation evidence is incomplete or changed")
        session.expire_all()
        _verify(session, settings, content_id, prepared_id, binding)
        prior = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "artifact", AuditEvent.entity_id == prepared_id,
                                                     AuditEvent.action == ACTION)).all()
        if len(prior) > 1 or (prior and (prior[0].actor_type != "admin" or prior[0].actor_id != "mpt-admin-api"
                or prior[0].metadata_json.get("record_sha256") != digest
                or prior[0].metadata_json.get("task_id") != binding.task_id
                or prior[0].metadata_json.get("prepared_sha256") != binding.prepared_sha256
                or prior[0].metadata_json.get("source_receipt_sha256") != record["source_receipt_sha256"])):
            raise ValueError("Prepared artifact already has conflicting registered preparation evidence")
        artifacts = []
        for name, payload, kind, mime in ((source_name, original, "image_preparation_original", {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[suffix]),
                                           (record_name, record_bytes, "image_preparation_receipt", "application/json")):
            relative = (destination / name).relative_to(root).as_posix()
            existing = session.exec(select(Artifact).where(Artifact.content_id == content_id, Artifact.relative_path == relative, Artifact.artifact_type == kind)).all()
            if len(existing) > 1 or (existing and (existing[0].sha256 != _sha(payload) or existing[0].size_bytes != len(payload) or existing[0].producer_stage != binding.task_id)):
                raise ValueError("Registered immutable preparation artifact changed")
            artifact = existing[0] if existing else Artifact(content_id=content_id, relative_path=relative, artifact_type=kind,
                sha256=_sha(payload), size_bytes=len(payload), mime_type=mime, producer_stage=binding.task_id)
            session.add(artifact)
            artifacts.append(artifact)
        if not prior:
            session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="artifact", entity_id=prepared_id, action=ACTION,
                metadata_json={"version": 1, "record_sha256": digest, "task_id": binding.task_id, "prepared_sha256": binding.prepared_sha256,
                               "source_receipt_sha256": record["source_receipt_sha256"], "original_artifact_id": artifacts[0].id, "receipt_artifact_id": artifacts[1].id}))
        elif (prior[0].metadata_json.get("original_artifact_id") != artifacts[0].id or prior[0].metadata_json.get("receipt_artifact_id") != artifacts[1].id):
            raise ValueError("Registered preparation audit binding changed")
        session.commit()
        return {"prepared_artifact_id": prepared_id, "original_artifact_id": artifacts[0].id,
                "receipt_artifact_id": artifacts[1].id, "record_sha256": digest}
