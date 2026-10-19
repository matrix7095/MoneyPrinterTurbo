"""Add vendor provenance and official output-use sources without changing old receipts."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from glance_core.content_acceptance import resolved_visual_plan
from glance_core.models import AgentTask, Artifact, AuditEvent, Content, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from app.business.image_preparation_evidence import (
    MAX_IMAGE, PreparationBinding, PreparationReceipt, _json, _replay, _safe, _verify,
)

MAX_BODY = 4096
MAX_REPORT = 4 * 1024 * 1024
MAX_DOCUMENT = 1024 * 1024
ACTION = "image_use_evidence_registered"
VALIDATOR_RELATIVE = "verification-tools/c2patool-0.28.1/c2patool/c2patool.exe"
VALIDATOR_SHA256 = "31f1e87c05be3aba9277f4b5fd20148f5f809678196e54b2fc549ef9eee8370d"
TERMS_PROFILE = "openai-native-image-output-use-20261020"
TERMS_RELATIVE = "verification-tools/c2patool-0.28.1/openai-image-output-use-basis-20261020.json"
TERMS_SHA256 = "273a412b66c259b4021ad2610a113f9e04181115e90b72854451aeb62f7d074b"
CONFIG_RELATIVE = "verification-tools/c2patool-0.28.1/offline-settings-final-20261020/settings.json"
CONFIG_SHA256 = "58fcba38d23e32d6a91eb3f8c8d44e12da0365988428d295314351291078f204"


class ImageUseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    preparation_receipt_artifact_id: str = Field(pattern=r"^asset_[0-9a-f]{32}$")
    terms_profile: str = Field(min_length=1, max_length=80)
    native_image_item_id: str = Field(pattern=r"^exec-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _artifact_bytes(root: Path, artifact: Artifact, limit: int) -> bytes:
    path = _safe(root / artifact.relative_path, root)
    if not 0 < artifact.size_bytes <= limit or path.stat().st_size != artifact.size_bytes:
        raise ValueError("Image-use evidence file size changed or exceeds its bound")
    raw = path.read_bytes()
    if len(raw) != artifact.size_bytes or _sha(raw) != artifact.sha256:
        raise ValueError("Image-use evidence artifact bytes changed")
    return raw


def _current_prepared_ids(session: Session, content: Content, root: Path) -> set[str]:
    plan = session.exec(select(AgentTask).where(
        AgentTask.content_id == content.id, AgentTask.task_type == TaskType.VISUAL_PLAN,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    cover = session.exec(select(Artifact).where(
        Artifact.content_id == content.id, Artifact.artifact_type == "cover",
    ).order_by(Artifact.created_at.desc(), Artifact.id.desc())).first()
    if plan is None or cover is None or not cover.relative_path.replace("\\", "/").startswith("publish/cover-revisions/"):
        raise ValueError("Current versioned cover and visual plan are required")
    _artifact_bytes(root, cover, MAX_IMAGE)
    composition_path = _safe((root / cover.relative_path).with_name("composition.json"), root)
    compositions = session.exec(select(Artifact).where(Artifact.content_id == content.id,
        Artifact.artifact_type == "cover_composition",
        Artifact.relative_path == composition_path.relative_to(root).as_posix())).all()
    if len(compositions) != 1:
        raise ValueError("Current registered cover composition is unavailable or ambiguous")
    composition = _json(_artifact_bytes(root, compositions[0], MAX_DOCUMENT))
    if (not isinstance(composition, dict) or composition.get("cover_sha256") != cover.sha256
            or composition.get("cover_size_bytes") != cover.size_bytes):
        raise ValueError("Current cover composition is not bound to the registered cover")
    binding = composition.get("source_binding") or {}
    base = session.get(Artifact, binding.get("artifact_id", ""))
    if (base is None or base.content_id != content.id or base.artifact_type != "agent_generated_image"
            or base.sha256 != composition.get("source_sha256") or base.size_bytes != composition.get("source_size_bytes")
            or base.producer_stage != binding.get("generation_task_id")):
        raise ValueError("Current cover generated-image source is unavailable")
    return {base.id, *(segment["asset_id"] for segment in resolved_visual_plan(session, plan).get("segments", []))}


def _preparation(session: Session, settings, content_id: str, prepared_id: str, receipt_id: str):
    content = session.get(Content, content_id)
    if content is None:
        raise ValueError("Image-use content is unavailable")
    root = _safe(Path(content.content_dir).absolute(), settings.content_dir.resolve())
    registrations = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "artifact", AuditEvent.entity_id == prepared_id,
        AuditEvent.action == "image_preparation_registered",
    )).all()
    event = registrations[0] if len(registrations) == 1 else None
    metadata = event.metadata_json if event else {}
    receipt = session.get(Artifact, receipt_id)
    original = session.get(Artifact, metadata.get("original_artifact_id", ""))
    if (event is None or event.actor_type != "admin" or event.actor_id != "mpt-admin-api"
            or metadata.get("receipt_artifact_id") != receipt_id or receipt is None or original is None
            or receipt.content_id != content_id or original.content_id != content_id
            or receipt.artifact_type != "image_preparation_receipt" or original.artifact_type != "image_preparation_original"
            or metadata.get("record_sha256") != receipt.sha256):
        raise ValueError("Unique authenticated image-preparation registration is required")
    record = _json(_artifact_bytes(root, receipt, MAX_DOCUMENT))
    if not isinstance(record, dict):
        raise ValueError("Image-preparation receipt must be an object")
    binding = PreparationBinding.model_validate({key: record[key] for key in PreparationBinding.model_fields})
    actual_root, task, prepared, _, _ = _verify(session, settings, content_id, prepared_id, binding)
    if (actual_root != root or receipt.producer_stage != task.id or original.producer_stage != task.id
            or metadata.get("task_id") != task.id or metadata.get("prepared_sha256") != prepared.sha256
            or metadata.get("source_receipt_sha256") != record.get("source_receipt_sha256")
            or record.get("source_sha256") != original.sha256 or record.get("source_size_bytes") != original.size_bytes
            or record.get("replay_verified") is not True or record.get("raw_input_bytes_verified") is not False
            or record.get("license_verified") is not False or record.get("vendor_generation_receipt_verified") is not False):
        raise ValueError("Historical preparation receipt identities or limitations changed")
    original_bytes = _artifact_bytes(root, original, MAX_IMAGE)
    replay = PreparationReceipt(status="PREPARED", task_id=task.id, segment_index=binding.segment_index,
        path="unused", source_path="unused", input_sha256=record["claimed_raw_input_sha256"],
        sha256=prepared.sha256, **{key: record[key] for key in (
            "source_sha256", "source_width", "source_height", "width", "height", "method", "upscaled")})
    _replay(original_bytes, task.input_data, binding.segment_index, replay)
    if prepared_id not in _current_prepared_ids(session, content, root):
        raise ValueError("Image-use evidence must bind current video or cover source images")
    return root, task, prepared, original, receipt, original_bytes


def _native_record(session: Session, settings, task: AgentTask, receipt: Artifact,
                   item_id: str, original_bytes: bytes) -> dict:
    content_root = Path(session.get(Content, task.content_id).content_dir).resolve()
    preparation = _json(_artifact_bytes(content_root, receipt, MAX_DOCUMENT))
    run_id = preparation["run_id"]
    directory = _safe(settings.data_dir / "private" / "content-runs" / run_id, settings.data_dir.resolve())
    state_path, events_path = _safe(directory / "state.json", directory), _safe(directory / "events.jsonl", directory)
    if not 0 < state_path.stat().st_size <= MAX_DOCUMENT or not 0 < events_path.stat().st_size <= 64 * MAX_DOCUMENT:
        raise ValueError("Native image journal exceeds its bounded evidence format")
    state_raw = state_path.read_bytes()
    state = _json(state_raw)
    if (not isinstance(state, dict) or not isinstance(state.get("intents"), dict)
            or any(not isinstance(intent, dict) for intent in state["intents"].values())):
        raise ValueError("Native journal state format changed")
    matches = [intent for intent in state["intents"].values() if intent.get("task_id") == task.id]
    if (state.get("run_id") != run_id or state.get("content_id") != task.content_id
            or state.get("generation") != preparation["run_generation"] or len(matches) != 1
            or state.get("phase") not in {"CONDUCTOR_STOP_REQUESTED", "CONDUCTOR_NO_TASK", "CONDUCTOR_RUN_EXPIRED"}
            or any(intent.get("last_observation") != "TERMINAL" for intent in state["intents"].values())):
        raise ValueError("Native image task/run association is unavailable or ambiguous")
    intent = matches[0]
    child, business = intent.get("child") or {}, intent.get("business") or {}
    child_id = child.get("child_id", "")
    if (not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", child_id)
            or child.get("parent_thread_id") != state.get("parent_thread_id")
            or (child.get("started") or {}).get("child_id") != child_id
            or (child.get("started") or {}).get("parent_thread_id") != state.get("parent_thread_id")
            or business.get("task_id") != task.id or business.get("current_attempt") != task.attempt
            or business.get("task_status") != "COMPLETED" or business.get("accepted") is not True
            or business.get("consumed") is not True
            or business.get("accepted_result_hash") != stable_hash(task.result_data)
            or business.get("consumed_result_hash") != stable_hash(task.result_data)):
        raise ValueError("Native child does not have the exact accepted and consumed generation result")
    claims = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "agent_task",
        AuditEvent.entity_id == task.id, AuditEvent.action == "mpt_content_run_claim")).all()
    claims = [row for row in claims if row.metadata_json.get("run_id") == run_id
              and row.metadata_json.get("generation") == preparation["run_generation"]]
    if (len(claims) != 1 or claims[0].metadata_json.get("child_id") != child_id
            or claims[0].metadata_json.get("claim_id") != business.get("claim_id")
            or claims[0].metadata_json.get("parent_thread_id") != state.get("parent_thread_id")
            or claims[0].metadata_json.get("host_epoch") != state.get("host_epoch")):
        raise ValueError("Native host child is not bound to the authenticated generation claim")
    previous, count, total, started, completed = 0, 0, 0, [], []
    with events_path.open("rb") as stream:
        while raw := stream.readline(256 * 1024 + 1):
            count += 1
            total += len(raw)
            if count > 100_000 or len(raw) > 256 * 1024 or total > 64 * MAX_DOCUMENT:
                raise ValueError("Native journal record exceeds its bound")
            event = _json(raw)
            if not isinstance(event, dict):
                raise ValueError("Native journal event must be an object")
            if type(event.get("seq")) is not int or event["seq"] <= previous:
                raise ValueError("Native journal sequence is invalid")
            previous = event["seq"]
            item = event.get("item") or {}
            if not isinstance(item, dict):
                raise ValueError("Native journal item must be an object")
            if (event.get("kind") != "native" or event.get("threadId") != child_id
                    or item.get("id") != item_id or item.get("type") != "imageGeneration"):
                continue
            if event.get("method") == "item/started":
                started.append(event["seq"])
            elif event.get("method") == "item/completed":
                if (item.get("status") != "completed" or not re.fullmatch(r"[0-9a-f]{64}", event.get("payload_sha256", ""))
                        or type(event.get("payload_bytes")) is not int or event["payload_bytes"] <= 0):
                    raise ValueError("Native image completion is incomplete")
                completed.append({"sequence": event["seq"], "source_line_sha256": _sha(raw),
                    "payload_sha256": event["payload_sha256"], "payload_bytes": event["payload_bytes"]})
    if len(started) != 1 or len(completed) != 1 or started[0] >= completed[0]["sequence"]:
        raise ValueError("Exactly one native image tool start and completion are required")
    generated_root = Path.home() / ".codex" / "generated_images"
    generated_path = _safe(generated_root / child_id / f"{item_id}.png", generated_root.resolve())
    if generated_path.stat().st_size != len(original_bytes) or generated_path.read_bytes() != original_bytes:
        raise ValueError("Retained native tool output differs from the registered original")
    return {"run_id": run_id, "run_generation": preparation["run_generation"], "task_id": task.id,
        "child_id": child_id, "item_id": item_id, "item_type": "imageGeneration", "status": "completed",
        "state_sha256": _sha(state_raw), "original_sha256": _sha(original_bytes), **completed[0],
        "limitation": "Server-owned native host observation and retained tool output; not a vendor-signed account, prompt, or licensing receipt. The native event payload digest is not the PNG digest."}


def _run_bounded(command: list[str], directory: Path) -> bytes:
    """Bound both time and disk output; never open a Windows console or invoke a shell."""
    output, error = directory / "stdout.json", directory / "stderr.txt"
    with output.open("xb") as stdout, error.open("xb") as stderr:
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                cwd=directory, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
            deadline = time.monotonic() + 45
            try:
                while process.poll() is None:
                    if time.monotonic() > deadline or output.stat().st_size + error.stat().st_size > MAX_REPORT:
                        raise ValueError("Image provenance validator exceeded its time/output bound")
                    time.sleep(.05)
                if process.returncode != 0 or error.stat().st_size or output.stat().st_size + error.stat().st_size > MAX_REPORT:
                    raise ValueError("Image provenance validator failed or exceeded its output bound")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    return output.read_bytes()


def _pinned_file(settings, relative: str, digest: str, limit: int) -> tuple[Path, bytes]:
    path = _safe(settings.data_dir / relative, settings.data_dir.resolve())
    if not 0 < path.stat().st_size <= limit:
        raise ValueError("Pinned image-use verifier file exceeds its bound")
    raw = path.read_bytes()
    if len(raw) > limit or _sha(raw) != digest:
        raise ValueError("Pinned image-use verifier or official terms assessment changed")
    return path, raw


def _report_summary(raw: bytes) -> dict:
    report = _json(raw)
    if not isinstance(report, dict) or not isinstance(report.get("manifests"), dict):
        raise ValueError("Native image provenance report format is unavailable")
    active = report.get("active_manifest")
    manifest = report["manifests"].get(active)
    if not isinstance(active, str) or not isinstance(manifest, dict):
        raise ValueError("Native image provenance active manifest is unavailable")
    signature = manifest.get("signature_info") or {}
    generators = manifest.get("claim_generator_info") or []
    actions = [action for assertion in manifest.get("assertions", [])
        if isinstance(assertion, dict) and assertion.get("label") == "c2pa.actions.v2"
        for action in assertion.get("data", {}).get("actions", []) if isinstance(action, dict)]
    if (not any(isinstance(generator, dict) and generator.get("name") == "OpenAI Media Service API" for generator in generators)
            or signature.get("issuer") != "OpenAI OpCo, LLC" or signature.get("common_name") != "OpenAI Media Service"
            or not any(action.get("action") == "c2pa.created"
                and action.get("softwareAgent") == {"name": "ChatGPT", "version": "gpt-image"}
                and action.get("digitalSourceType") == "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"
                for action in actions)):
        raise ValueError("Native provenance generator assertions do not match the observed OpenAI image tool")
    validation = report.get("validation_results", {}).get("activeManifest")
    if not isinstance(validation, dict) or report.get("validation_state") not in {"Trusted", "Valid", "Invalid", "Untrusted"}:
        raise ValueError("Native provenance validation status is unavailable")
    codes = {}
    for category in ("success", "informational", "failure"):
        rows = validation.get(category)
        if not isinstance(rows, list) or any(not isinstance(row, dict) or not isinstance(row.get("code"), str) for row in rows):
            raise ValueError("Native provenance validation results are incomplete")
        codes[category] = [row["code"] for row in rows]
    # Report actual verifier failures; host tool attribution is a separate, required proof.
    successes = set(codes["success"])
    return {"active_manifest": active, "validation_state": report["validation_state"], "validation_codes": codes,
        "issuer": signature["issuer"], "common_name": signature["common_name"],
        "signing_credential_trusted": "signingCredential.trusted" in successes,
        "claim_signature_validated": "claimSignature.validated" in successes,
        "asset_data_hash_matched": "assertion.dataHash.match" in successes,
        "timestamp_trusted": "timeStamp.trusted" in successes and "timeStamp.untrusted" not in codes["informational"] + codes["failure"],
        "limitation": "The complete pinned offline validator report is retained, including all failures and informational warnings. These individual cryptographic observations do not establish an account contract, original prompts, online revocation, CAWG identity, third-party clearance or output-use permission."}


def _validate_original(settings, original_bytes: bytes) -> tuple[bytes, dict]:
    _, executable = _pinned_file(settings, VALIDATOR_RELATIVE, VALIDATOR_SHA256, 32 * MAX_DOCUMENT)
    _, configuration = _pinned_file(settings, CONFIG_RELATIVE, CONFIG_SHA256, MAX_DOCUMENT)
    parent = _safe(settings.data_dir / "private", settings.data_dir.resolve())
    parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="image-use-verification-", dir=parent) as directory:
        temporary = Path(directory)
        # Isolate verified bytes from sidecar settings and caller-controlled executable paths.
        configuration_dir = temporary / "configuration"
        configuration_dir.mkdir()
        validator, config, original = temporary / "c2patool.exe", configuration_dir / "settings.json", temporary / "original.png"
        validator.write_bytes(executable)
        config.write_bytes(configuration)
        original.write_bytes(original_bytes)
        report = _run_bounded([str(validator), str(original), "--settings", str(config)], temporary)
    if not report or len(report) > MAX_REPORT:
        raise ValueError("Image provenance validator returned no bounded report")
    return report, {"name": "c2patool", "version": "0.28.1", "executable_sha256": VALIDATOR_SHA256,
        "offline_settings_sha256": CONFIG_SHA256,
        "scope": "Pinned official generator trust anchors and official default EKUs; remote manifest, network hosts, OCSP fetch and redirects disabled. Timestamp trust is reported separately."}


def _store(session: Session, root: Path, content_id: str, task_id: str, destination: Path,
           name: str, payload: bytes, kind: str) -> Artifact:
    relative = (destination / name).relative_to(root).as_posix()
    rows = session.exec(select(Artifact).where(Artifact.content_id == content_id,
        Artifact.relative_path == relative, Artifact.artifact_type == kind)).all()
    if len(rows) > 1 or (rows and (rows[0].sha256 != _sha(payload) or rows[0].size_bytes != len(payload)
            or rows[0].producer_stage != task_id)):
        raise ValueError("Immutable image-use artifact association changed")
    artifact = rows[0] if rows else Artifact(content_id=content_id, relative_path=relative,
        artifact_type=kind, sha256=_sha(payload), size_bytes=len(payload), mime_type="application/json", producer_stage=task_id)
    session.add(artifact)
    return artifact


def register_image_use(engine, settings, content_id: str, prepared_id: str, body: bytes) -> dict:
    if not body or len(body) > MAX_BODY:
        raise ValueError("Image-use registration exceeds its bounded JSON request")
    request = ImageUseRequest.model_validate(_json(body))
    if request.terms_profile != TERMS_PROFILE:
        raise ValueError("Only the pinned official output-use assessment profile is supported")
    _, terms_bytes = _pinned_file(settings, TERMS_RELATIVE, TERMS_SHA256, MAX_DOCUMENT)
    terms = _json(terms_bytes)
    if terms.get("profile") != TERMS_PROFILE or terms.get("assessment_type") != "official_public_terms_and_publication_policy_use_basis":
        raise ValueError("Pinned official terms assessment profile is invalid")
    with Session(engine) as session:
        args = session, settings, content_id, prepared_id, request.preparation_receipt_artifact_id
        root, task, prepared, original, preparation, original_bytes = _preparation(*args)
        native = _native_record(session, settings, task, preparation, request.native_image_item_id, original_bytes)
        report_bytes, validator = _validate_original(settings, original_bytes)
        summary = _report_summary(report_bytes)
        record = {"version": 1, "prepared_artifact_id": prepared.id, "prepared_sha256": prepared.sha256,
            "original_artifact_id": original.id, "original_sha256": original.sha256,
            "preparation_receipt_artifact_id": preparation.id, "preparation_receipt_sha256": preparation.sha256,
            "task_id": task.id, "task_input_sha256": task.input_hash, "task_result_sha256": stable_hash(task.result_data),
            "native_host": native, "native_host_output_verified": True, "c2pa": summary, "validator": validator,
            "validator_report_sha256": _sha(report_bytes), "terms_document_sha256": _sha(terms_bytes),
            "terms_profile": TERMS_PROFILE, "official_public_terms_assessment_registered": True,
            "license_verified": False, "specific_account_agreement_verified": False,
            "historical_raw_input_bytes_verified": False,
            "limitation": "Additive supporting evidence only. Historical receipts, accepted task inputs/results, false verification flags, quality gates and publication authorization are unchanged. Public terms assessment is not a per-image license certificate or legal guarantee."}
        record_bytes = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        digest = _sha(record_bytes)

        def prior_registration():
            rows = session.exec(select(AuditEvent).where(AuditEvent.entity_type == "artifact",
                AuditEvent.entity_id == prepared_id, AuditEvent.action == ACTION)).all()
            if len(rows) > 1 or (rows and (rows[0].actor_type != "admin" or rows[0].actor_id != "mpt-admin-api"
                    or rows[0].metadata_json.get("record_sha256") != digest
                    or rows[0].metadata_json.get("prepared_sha256") != prepared.sha256
                    or rows[0].metadata_json.get("original_sha256") != original.sha256)):
                raise ValueError("Prepared image already has conflicting registered use evidence")
            return rows[0] if rows else None

        prior_registration()
        parent = _safe(root / "image-use-evidence", root)
        parent.mkdir(parents=True, exist_ok=True)
        destination = _safe(parent / digest, root)
        payloads = {f"image-use-{digest}.json": (record_bytes, "image_use_receipt", "receipt"),
            f"c2pa-report-{_sha(report_bytes)}.json": (report_bytes, "image_use_validator_report", "validator_report"),
            f"image-terms-{_sha(terms_bytes)}.json": (terms_bytes, "image_use_terms_document", "terms_document")}
        if not destination.exists():
            with TemporaryDirectory(prefix=".image-use-", dir=parent) as directory:
                staging = Path(directory)
                for name, (payload, _, _) in payloads.items():
                    (staging / name).write_bytes(payload)
                staging.rename(destination)
        for name, (payload, _, _) in payloads.items():
            target = _safe(destination / name, root)
            if not target.is_file() or target.read_bytes() != payload:
                raise ValueError("Immutable image-use evidence is incomplete or changed")
        session.expire_all()
        _preparation(*args)
        if _native_record(session, settings, task, preparation, request.native_image_item_id, original_bytes) != native:
            raise ValueError("Native generation evidence changed during verification")
        prior = prior_registration()
        metadata = {"version": 1, "record_sha256": digest, "prepared_sha256": prepared.sha256,
            "original_sha256": original.sha256}
        for name, (payload, kind, key) in payloads.items():
            asset = _store(session, root, content_id, task.id, destination, name, payload, kind)
            metadata[f"{key}_artifact_id"] = asset.id
        if prior is None:
            session.add(AuditEvent(actor_type="admin", actor_id="mpt-admin-api", entity_type="artifact",
                entity_id=prepared_id, action=ACTION, metadata_json=metadata))
        elif prior.metadata_json != metadata:
            raise ValueError("Registered image-use audit association changed")
        session.commit()
        return {"prepared_artifact_id": prepared_id, **metadata}
