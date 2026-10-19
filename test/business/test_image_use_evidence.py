"""Additive image evidence verifies retained host output without granting publication."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.business import image_use_evidence as module
from glance_core.models import AgentTask, Artifact, AuditEvent
from glance_core.task_service import stable_hash
from test.business.test_content_http import content_app
from test.business.test_discovery_http import ADMIN
from test.business.test_image_preparation_evidence import archive, evidence_fixture as evidence_fixture, post, sha


@pytest.fixture
def registered(evidence_fixture, monkeypatch, tmp_path):
    runtime, content_id, manifest, receipt, original, root = evidence_fixture
    with TestClient(content_app(runtime)) as http:
        response = post(http, content_id, manifest["prepared_artifact_id"], archive(manifest, receipt, original))
        assert response.status_code == 200, response.text
    monkeypatch.setattr(module, "_current_prepared_ids", lambda *args: {manifest["prepared_artifact_id"]})
    return runtime, content_id, manifest, response.json(), original, root


@pytest.fixture
def native_registered(registered, monkeypatch, tmp_path):
    runtime, content_id, manifest, registration, original, root = registered
    child = "01234567-1234-1234-1234-0123456789ab"
    parent = "11234567-1234-1234-1234-0123456789ab"
    item = "exec-21234567-1234-1234-1234-0123456789ab"
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, manifest["task_id"])
        claim = session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id,
            AuditEvent.action == "mpt_content_run_claim")).one()
        claim.metadata_json = {**claim.metadata_json, "child_id": child, "parent_thread_id": parent}
        session.add(claim)
        session.commit()
        state = {"version": 1, "run_id": manifest["run_id"], "content_id": content_id,
            "generation": manifest["run_generation"], "host_epoch": 1, "phase": "CONDUCTOR_STOP_REQUESTED",
            "parent_thread_id": parent, "intents": {"fixture": {
                "task_id": task.id, "last_observation": "TERMINAL", "child": {"child_id": child, "parent_thread_id": parent,
                    "started": {"child_id": child, "parent_thread_id": parent}},
                "business": {"task_id": task.id, "task_status": "COMPLETED", "current_attempt": 1,
                    "accepted": True, "consumed": True, "accepted_result_hash": manifest["result_sha256"],
                    "consumed_result_hash": manifest["result_sha256"], "claim_id": "claim-fixture"}}}}
    directory = runtime.settings.data_dir / "private/content-runs" / manifest["run_id"]
    directory.mkdir(parents=True)
    (directory / "state.json").write_text(json.dumps(state), encoding="utf-8")
    events = [{"seq": 1, "kind": "native", "method": "item/started", "threadId": child,
        "item": {"id": item, "type": "imageGeneration"}},
        {"seq": 2, "kind": "native", "method": "item/completed", "threadId": child,
        "item": {"id": item, "type": "imageGeneration", "status": "completed"},
        "payload_sha256": "a" * 64, "payload_bytes": 100}]
    events_path = directory / "events.jsonl"
    events_path.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    native_path = Path.home() / ".codex/generated_images" / child / f"{item}.png"
    native_path.parent.mkdir(parents=True)
    native_path.write_bytes(original)
    return registered, item, directory, native_path


def test_preparation_revalidated_without_changing_old_receipt_or_task(registered):
    runtime, content_id, manifest, registration, original, root = registered
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, manifest["task_id"])
        before = task.model_dump(mode="json")
        receipt = session.get(Artifact, registration["receipt_artifact_id"])
        previous_bytes = (root / receipt.relative_path).read_bytes()
        result = module._preparation(session, runtime.settings, content_id, manifest["prepared_artifact_id"], receipt.id)
        assert result[-1] == original
        assert task.model_dump(mode="json") == before
        assert (root / receipt.relative_path).read_bytes() == previous_bytes
        old = json.loads(previous_bytes)
        assert old["license_verified"] is old["vendor_generation_receipt_verified"] is old["raw_input_bytes_verified"] is False


def test_native_host_exact_output_is_separate_from_payload_digest(native_registered):
    (runtime, content_id, manifest, registration, original, root), item, directory, native_path = native_registered
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, manifest["task_id"])
        receipt = session.get(Artifact, registration["receipt_artifact_id"])
        result = module._native_record(session, runtime.settings, task, receipt, item, original)
    assert result["original_sha256"] == sha(original)
    assert result["payload_sha256"] == "a" * 64
    assert "not a vendor-signed" in result["limitation"]
    assert "source_path" not in result and "payload" not in result


@pytest.mark.parametrize("damage", ["wrong_output", "wrong_child", "unconsumed", "result", "duplicate_event", "sequence", "missing_start"])
def test_native_host_mismatch_fails_closed(native_registered, damage):
    (runtime, content_id, manifest, registration, original, root), item, directory, native_path = native_registered
    state_path, events_path = directory / "state.json", directory / "events.jsonl"
    state = json.loads(state_path.read_text())
    events = [json.loads(line) for line in events_path.read_text().splitlines()]
    intent = next(iter(state["intents"].values()))
    if damage == "wrong_output":
        native_path.write_bytes(b"wrong")
    elif damage == "wrong_child":
        intent["child"]["child_id"] = "31234567-1234-1234-1234-0123456789ab"
    elif damage == "unconsumed":
        intent["business"]["consumed"] = False
    elif damage == "result":
        intent["business"]["accepted_result_hash"] = "0" * 64
    elif damage == "duplicate_event":
        events.append({**events[-1], "seq": 3})
    elif damage == "sequence":
        events[-1]["seq"] = 1
    elif damage == "missing_start":
        events = events[1:]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    events_path.write_text("".join(json.dumps(row) + "\n" for row in events), encoding="utf-8")
    with Session(runtime.engine) as session:
        with pytest.raises(ValueError):
            module._native_record(session, runtime.settings, session.get(AgentTask, manifest["task_id"]),
                session.get(Artifact, registration["receipt_artifact_id"]), item, original)


def test_request_refuses_paths_and_claimed_license_flags():
    request = {"version": 1, "preparation_receipt_artifact_id": "asset_" + "1" * 32,
        "terms_profile": "fixture", "native_image_item_id": "exec-21234567-1234-1234-1234-0123456789ab"}
    assert module.ImageUseRequest.model_validate(request)
    for addition in ({"path": "C:/private/file"}, {"license_verified": True}, {"validator": "cmd.exe"}, {"trusted": True}):
        with pytest.raises(ValueError):
            module.ImageUseRequest.model_validate({**request, **addition})


def validator_report(*, failure=False):
    return json.dumps({"active_manifest": "urn:c2pa:fixture", "manifests": {"urn:c2pa:fixture": {
        "claim_generator_info": [{"name": "OpenAI Media Service API"}],
        "signature_info": {"issuer": "OpenAI OpCo, LLC", "common_name": "OpenAI Media Service"},
        "assertions": [{"label": "c2pa.actions.v2", "data": {"actions": [{"action": "c2pa.created",
            "softwareAgent": {"name": "ChatGPT", "version": "gpt-image"},
            "digitalSourceType": "http://cv.iptc.org/newscodes/digitalsourcetype/trainedAlgorithmicMedia"}]}}]}},
        "validation_state": "Invalid" if failure else "Trusted", "validation_results": {"activeManifest": {
            "success": [{"code": code} for code in ["signingCredential.trusted", "claimSignature.validated", "assertion.dataHash.match"]],
            "informational": [{"code": "timeStamp.untrusted"}],
            "failure": [{"code": "signingCredential.invalid", "explanation": "missing EKU"}] if failure else []}}}).encode()


@pytest.fixture
def importable(native_registered, monkeypatch):
    registered, item, directory, native_path = native_registered
    runtime, content_id, manifest, registration, original, root = registered
    terms = json.dumps({"profile": module.TERMS_PROFILE,
        "assessment_type": "official_public_terms_and_publication_policy_use_basis",
        "limitations": ["Specific account contract not independently verified"]}).encode()
    terms_path = runtime.settings.data_dir / module.TERMS_RELATIVE
    terms_path.parent.mkdir(parents=True)
    terms_path.write_bytes(terms)
    monkeypatch.setattr(module, "TERMS_SHA256", sha(terms))
    monkeypatch.setattr(module, "_validate_original", lambda settings, raw: (validator_report(), {"version": "fixture"}))
    request = {"version": 1, "preparation_receipt_artifact_id": registration["receipt_artifact_id"],
        "terms_profile": module.TERMS_PROFILE, "native_image_item_id": item}
    return registered, request


def post_use(http, content_id, prepared_id, request, *, headers=ADMIN):
    return http.post(f"/api/v1/contents/{content_id}/image-use-evidence/{prepared_id}", headers=headers, json=request)


def test_admin_registration_is_immutable_idempotent_and_never_queues_or_grants_license(importable):
    (runtime, content_id, manifest, registration, original, root), request = importable
    with Session(runtime.engine) as session:
        task_before = stable_hash(session.get(AgentTask, manifest["task_id"]).model_dump(mode="json"))
        prep = session.get(Artifact, registration["receipt_artifact_id"])
        prep_before = (root / prep.relative_path).read_bytes()
        task_ids = session.exec(select(AgentTask.id)).all()
    with TestClient(content_app(runtime)) as http:
        assert post_use(http, content_id, manifest["prepared_artifact_id"], request, headers={}).status_code == 401
        first = post_use(http, content_id, manifest["prepared_artifact_id"], request)
        assert first.status_code == 200, first.text
        second = post_use(http, content_id, manifest["prepared_artifact_id"], request)
        assert second.status_code == 200 and second.json() == first.json()
    with Session(runtime.engine) as session:
        assert stable_hash(session.get(AgentTask, manifest["task_id"]).model_dump(mode="json")) == task_before
        assert session.exec(select(AgentTask.id)).all() == task_ids
        assert (root / prep.relative_path).read_bytes() == prep_before
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == module.ACTION)).all()) == 1
        record = session.get(Artifact, first.json()["receipt_artifact_id"])
        data = json.loads((root / record.relative_path).read_bytes())
        assert data["native_host_output_verified"] is True
        assert data["license_verified"] is data["specific_account_agreement_verified"] is False
        assert data["historical_raw_input_bytes_verified"] is False
        assert data["c2pa"]["timestamp_trusted"] is False
        assert data["c2pa"]["claim_signature_validated"] is True


@pytest.mark.parametrize("damage", ["terms", "request_flag", "unknown_profile", "receipt_id", "report", "audit", "immutable_file"])
def test_registration_rejects_missing_forged_or_conflicting_evidence(importable, monkeypatch, damage):
    (runtime, content_id, manifest, registration, original, root), request = importable
    with TestClient(content_app(runtime)) as http:
        if damage in {"audit", "immutable_file"}:
            first = post_use(http, content_id, manifest["prepared_artifact_id"], request)
            assert first.status_code == 200, first.text
            with Session(runtime.engine) as session:
                if damage == "audit":
                    event = session.exec(select(AuditEvent).where(AuditEvent.action == module.ACTION)).one()
                    event.actor_type = "agent"
                    session.add(event)
                    session.commit()
                else:
                    asset = session.get(Artifact, first.json()["validator_report_artifact_id"])
                    (root / asset.relative_path).write_bytes(b"tampered")
        if damage == "terms":
            (runtime.settings.data_dir / module.TERMS_RELATIVE).write_bytes(b"fake")
        elif damage == "request_flag":
            request["license_verified"] = True
        elif damage == "unknown_profile":
            request["terms_profile"] = "forged"
        elif damage == "receipt_id":
            request["preparation_receipt_artifact_id"] = "asset_" + "0" * 32
        elif damage == "report":
            monkeypatch.setattr(module, "_validate_original", lambda *args: (b'{"trusted":true}', {}))
        response = post_use(http, content_id, manifest["prepared_artifact_id"], request)
        assert response.status_code == 409, response.text


def test_validator_failures_and_timestamp_warning_are_preserved():
    result = module._report_summary(validator_report(failure=True))
    assert result["validation_state"] == "Invalid"
    assert result["validation_codes"]["failure"] == ["signingCredential.invalid"]
    assert result["validation_codes"]["informational"] == ["timeStamp.untrusted"]
    assert result["timestamp_trusted"] is False


def test_streaming_request_cap_does_not_trust_content_length(importable):
    (runtime, content_id, manifest, *_), _ = importable
    with TestClient(content_app(runtime)) as http:
        url = f"/api/v1/contents/{content_id}/image-use-evidence/{manifest['prepared_artifact_id']}"
        assert http.post(url, headers={**ADMIN, "Content-Type": "application/zip"}, content=b"x").status_code == 415
        assert http.post(url, headers={**ADMIN, "Content-Type": "application/json", "Content-Length": "1"},
            content=(b"x" * 2048 for _ in range(3))).status_code == 413


def test_validator_replay_uses_only_pinned_bytes_and_isolated_settings(registered, monkeypatch):
    runtime, *_ = registered
    monkeypatch.setattr(module, "_pinned_file", lambda settings, relative, digest, limit: (Path(relative), b"fixture"))
    def run(command, directory):
        assert command[2] == "--settings"
        assert Path(command[0]).parent == directory
        assert Path(command[1]).read_bytes() == b"original"
        assert Path(command[3]).parent != directory
        assert [path.name for path in Path(command[3]).parent.iterdir()] == ["settings.json"]
        assert Path(command[0]).read_bytes() == Path(command[3]).read_bytes() == b"fixture"
        return validator_report()
    monkeypatch.setattr(module, "_run_bounded", run)
    report, details = module._validate_original(runtime.settings, b"original")
    assert report == validator_report()
    assert details["offline_settings_sha256"] == module.CONFIG_SHA256


def test_eight_additive_imports_fresh_review_and_real_flat_sdk_bundle(evidence_fixture, monkeypatch, tmp_path):
    from test.business import test_image_preparation_evidence as previous
    runtime, content_id, _, _, _, _ = evidence_fixture
    terms = json.dumps({"profile": module.TERMS_PROFILE,
        "assessment_type": "official_public_terms_and_publication_policy_use_basis"}).encode()
    path = runtime.settings.data_dir / module.TERMS_RELATIVE
    path.parent.mkdir(parents=True)
    path.write_bytes(terms)
    monkeypatch.setattr(module, "TERMS_SHA256", sha(terms))
    monkeypatch.setattr(module, "_native_record", lambda session, settings, task, receipt, item, original:
        {"task_id": task.id, "original_sha256": sha(original), "item_id": item})
    monkeypatch.setattr(module, "_validate_original", lambda *args: (validator_report(), {"version": "fixture"}))
    seen = []
    base_post = previous.post

    def add_use(http, target_id, prepared_id, body, *, headers=ADMIN):
        response = base_post(http, target_id, prepared_id, body, headers=headers)
        assert response.status_code == 200, response.text
        with Session(runtime.engine) as session:
            before = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            repeated = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            assert stable_hash(before) == stable_hash(repeated)
            if not seen:
                assert "image_use_evidence" not in before["production_evidence"]
        request = {"version": 1, "preparation_receipt_artifact_id": response.json()["receipt_artifact_id"],
            "terms_profile": module.TERMS_PROFILE, "native_image_item_id": "exec-21234567-1234-1234-1234-0123456789ab"}
        registration = post_use(http, target_id, prepared_id, request)
        assert registration.status_code == 200, registration.text
        seen.append(prepared_id)
        with Session(runtime.engine) as session:
            after = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            use = after["production_evidence"]["image_use_evidence"]
            assert len(use["records"]) == len(seen)
            assert use["complete"] is (len(seen) == 8)
            # Only supporting evidence and its verified context files are additive.
            stripped_before, stripped_after = json.loads(json.dumps(before)), json.loads(json.dumps(after))
            stripped_before["production_evidence"].pop("image_use_evidence", None)
            stripped_after["production_evidence"].pop("image_use_evidence")
            stripped_before.pop("context_files")
            stripped_after.pop("context_files")
            assert stripped_before == stripped_after
        return response

    monkeypatch.setattr(previous, "post", add_use)
    previous.test_eight_imports_refresh_one_normal_review_and_real_sdk_context(evidence_fixture, tmp_path)
    assert len(seen) == 8
    with Session(runtime.engine) as session:
        from glance_core.models import TaskType
        reviews = session.exec(select(AgentTask).where(AgentTask.content_id == content_id,
            AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all()
        assert len(reviews) == 2
        old, fresh = sorted(reviews, key=lambda task: task.created_at)
        assert "image_use_evidence" not in old.input_data["production_evidence"]
        assert fresh.input_data["production_evidence"]["image_use_evidence"]["complete"] is True
