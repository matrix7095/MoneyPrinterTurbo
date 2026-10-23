"""New administrator evidence opens research, never changes a failed rights decision."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, Content, ContentStatus, ProductionMode, PublicationMode, PublicationRecord, RuntimeSetting, TaskType, utc_now
from glance_core.task_service import stable_hash
from app.business import pipeline
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_content_http import RESEARCH
from test.business.test_deployment_http import app_for
from test.business.test_discovery_http import ADMIN, make_runtime
from test.business.test_semantic_http import SCRIPT


RIGHTS_QC = {"passed": False, "issues": [{"severity": "error", "field": "rights", "message": "Actual license scope needs verification"}]}
PLAN = {"failure_class": "BUSINESS_QC", "action": "reroute_to_role", "target_task_type": "content_research",
        "reason": "Verify the missing license evidence independently", "must_preserve": ["Original evidence and recovery budget"]}
EVIDENCE = "Fixture new license excerpt supplied by its holder: permissions cover the two specified images; verify issuer, scope and conditions independently."


@pytest.fixture
def subject(tmp_path, monkeypatch):
    from PIL import Image

    def acquire(executor, images, **kwargs):
        rows = []
        for index, image in enumerate(images):
            path = executor.root / f"image-{index}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (1080, 1920), (60 * index, 120, 20)).save(path)
            rows.append({**image, "path": path, "width": 1080, "height": 1920})
        return rows

    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", acquire)
    runtime = make_runtime(tmp_path)
    with TestClient(app_for(runtime, tmp_path)) as http:
        researcher = registered_client(http, runtime, "research")
        script_agent = registered_client(http, runtime, "script")
        assistant = registered_client(http, runtime, "assistant")
        with Session(runtime.engine) as session:
            candidate, _ = runtime.workflow_service(session).ingest_candidate(
                source_type="agent", source_id="rights-evidence", source_url="https://example.org/product", original_title="Product")
            content = Content(candidate_id=candidate.id, title="Product", content_dir=str(tmp_path / "content"),
                              production_mode=ProductionMode.IMAGE_ONLY, business_status=ContentStatus.PUBLISHED)
            session.add(content)
            session.commit()
            publication = PublicationRecord(content_id=content.id, platform="douyin", mode=PublicationMode.EXECUTOR, status="PUBLISHED",
                                            external_id="7698443917590629670", external_url="https://www.douyin.com/video/7698443917590629670")
            session.add(publication)
            session.commit()
            cid, pub_id = content.id, publication.id
        revision = http.post(f"/api/v1/contents/{cid}/published-revisions", headers=ADMIN, json={
            "request_key": "rework", "expected_revision_id": None, "publication_id": pub_id,
            "feedback": "Use corresponding official images after verifying applicable permission."})
        assert revision.status_code == 200, revision.text
        assert http.post(f"/api/v1/contents/{cid}/prepare", headers=ADMIN).status_code == 200
        value = deepcopy(RESEARCH)
        value["source_images"] = [{"url": f"https://example.org/original-{index}.png", "caption": f"Product {index}",
                                   "source_page": "https://example.org/product"} for index in range(2)]
        research = researcher.claim()
        assert researcher.complete(research["task_id"], research["lease_token"], researcher.settings.agent_id, value)["advanced"] == "CONSUMED"
        script = script_agent.claim()
        bundle = script_agent.bundle(script["bundle_url"], script["lease_token"])
        result = deepcopy(SCRIPT)
        for row, image in zip(result["visual_requirements"], bundle["input"]["source_images"], strict=True):
            row["preferred_source"] = image["artifact_id"]
        assert script_agent.complete(script["task_id"], script["lease_token"], script_agent.settings.agent_id, result)["advanced"] == "CONSUMED"
        qc = script_agent.claim()
        assert script_agent.complete(qc["task_id"], qc["lease_token"], script_agent.settings.agent_id, RIGHTS_QC)["advanced"] == "CONSUMED"
        recovery = assistant.claim()
        assert assistant.complete(recovery["task_id"], recovery["lease_token"], assistant.settings.agent_id, PLAN)["advanced"] == "CONSUMED"
        with Session(runtime.engine) as session:
            content = session.get(Content, cid)
            assert content.current_stage == "recovery_blocked"
            assert content.error_message == "角色路由动作与失败分类不匹配"
            ids = {"content": cid, "revision": content.current_revision_id, "research": research["task_id"],
                   "script": script["task_id"], "qc": qc["task_id"], "recovery": recovery["task_id"], "publication": pub_id}
        yield runtime, http, researcher, script_agent, assistant, ids, value


def endpoint(ids):
    return f"/api/v1/contents/{ids['content']}/rights-evidence"


def payload(runtime, ids, **updates):
    with Session(runtime.engine) as session:
        hashes = {f"{name}_result_hash": stable_hash(session.get(AgentTask, ids[name]).result_data)
                  for name in ("research", "qc", "recovery")}
    return {"request_key": "new-license-terms", "expected_revision_id": ids["revision"],
            **{f"{name}_task_id": ids[name] for name in ("research", "qc", "recovery")},
            **hashes, "evidence_text": EVIDENCE, **updates}


def snapshots(runtime):
    with Session(runtime.engine) as session:
        return {(model.__tablename__, row.key if model is RuntimeSetting else row.id): row.model_dump(mode="json")
                for model in (AgentTask, AgentSubmission, Artifact, PublicationRecord, RuntimeSetting, Candidate, AuditEvent)
                for row in session.exec(select(model)).all()}


def test_manual_recovery_boundary_preflight_and_new_evidence_are_explicit_and_authenticated(subject):
    runtime, http, _, _, _, ids, _ = subject
    before = snapshots(runtime)
    body = payload(runtime, ids)
    assert http.post(endpoint(ids), json=body).status_code == 401
    params = {name: body[name] for name in ("expected_revision_id", "research_task_id", "qc_task_id", "recovery_task_id")}
    assert http.get(endpoint(ids) + "/preflight", params=params).status_code == 401
    preflight = http.get(endpoint(ids) + "/preflight", params=params, headers=ADMIN)
    assert preflight.status_code == 200, preflight.text
    assert snapshots(runtime) == before
    response = http.post(endpoint(ids), json=body, headers=ADMIN)
    assert response.status_code == 200, response.text
    rid = response.json()["research_task_id"]
    after = snapshots(runtime)
    assert all(after[key] == value for key, value in before.items())
    with Session(runtime.engine) as session:
        research = session.get(AgentTask, rid)
        assert research.input_data["rights_evidence_request"] == body
        assert research.input_data["rights_evidence_text_sha256"] == hashlib.sha256(EVIDENCE.encode()).hexdigest()
        assert research.input_data["lineage"] == session.get(AgentTask, ids["recovery"]).input_data["lineage"]
        assert session.get(AgentTask, ids["qc"]).result_data["passed"] is False
    assert http.post(endpoint(ids), json=body, headers=ADMIN).json()["research_task_id"] == rid


def complete_new_round(subject, body):
    runtime, http, researcher, script_agent, _, ids, value = subject
    response = http.post(endpoint(ids), json=body, headers=ADMIN)
    assert response.status_code == 200, response.text
    rid = response.json()["research_task_id"]
    claim = researcher.claim()
    assert claim["task_id"] == rid
    bundle = researcher.bundle(claim["bundle_url"], claim["lease_token"])
    assert bundle["input"]["rights_evidence_request"]["evidence_text"] == body["evidence_text"]
    assert bundle["input"]["previous_script_qc"]["passed"] is False
    # Research is permitted to find the evidence insufficient, without upgrading rights.
    research_result = deepcopy(value)
    research_result["limitations"].append("New submitted excerpt still lacks independent verification of issuer and scope.")
    assert researcher.complete(rid, claim["lease_token"], researcher.settings.agent_id, research_result)["advanced"] == "CONSUMED"
    script = script_agent.claim()
    assert script["task_id"] != ids["script"] and script["task_type"] == TaskType.SHORT_SCRIPT.value
    script_bundle = script_agent.bundle(script["bundle_url"], script["lease_token"])
    result = deepcopy(SCRIPT)
    for row, image in zip(result["visual_requirements"], script_bundle["input"]["source_images"], strict=True):
        row["preferred_source"] = image["artifact_id"]
    assert script_agent.complete(script["task_id"], script["lease_token"], script_agent.settings.agent_id, result)["advanced"] == "CONSUMED"
    qc = script_agent.claim()
    assert qc["task_type"] == TaskType.SCRIPT_QC.value and qc["task_id"] != ids["qc"]
    assert script_agent.complete(qc["task_id"], qc["lease_token"], script_agent.settings.agent_id, RIGHTS_QC)["advanced"] == "CONSUMED"
    return rid, script["task_id"], qc["task_id"]


def test_new_research_and_script_qc_keep_actual_recovery_root_round_and_prior_records(subject):
    runtime, _, _, _, _, ids, _ = subject
    before = snapshots(runtime)
    rid, script_id, qc_id = complete_new_round(subject, payload(runtime, ids))
    after = snapshots(runtime)
    assert all(after[key] == value for key, value in before.items())
    with Session(runtime.engine) as session:
        old = session.get(AgentTask, ids["recovery"])
        new = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.RECOVERY,
                                                   AgentTask.id != old.id)).one()
        lineage = new.input_data["lineage"]
        assert lineage["root_task_id"] == old.input_data["lineage"]["root_task_id"]
        assert lineage["round"] == 2 and old.input_data["lineage"]["round"] == 1
        assert lineage["max_rounds"] == old.input_data["lineage"]["max_rounds"]
        assert lineage["parent_task_id"] == qc_id and lineage["repair_target_task_id"] == script_id
        assert session.get(AgentTask, qc_id).result_data["passed"] is False
        assert session.get(AgentTask, rid).input_data["recovery_round"] == 1
        assert session.exec(select(Candidate)).one().rights_status.value == "OFFICIAL_REFERENCE"


def test_repeated_unverified_evidence_exhausts_the_original_recovery_budget(subject):
    runtime, http, _, _, assistant, first_ids, _ = subject
    ids = dict(first_ids)
    for round_number in (2, 3):
        body = payload(runtime, ids, request_key=f"evidence-{round_number}", evidence_text=EVIDENCE + f" Additional scope detail {round_number}.")
        rid, script_id, qc_id = complete_new_round((*subject[:5], ids, subject[6]), body)
        recovery = assistant.claim()
        with Session(runtime.engine) as session:
            task = session.get(AgentTask, recovery["task_id"])
            assert task.input_data["lineage"]["round"] == round_number
            assert task.input_data["lineage"]["max_rounds"] == 3
            assert task.input_data["lineage"]["root_task_id"] == first_ids["qc"]
        assert assistant.complete(recovery["task_id"], recovery["lease_token"], assistant.settings.agent_id, PLAN)["advanced"] == "CONSUMED"
        ids.update(research=rid, script=script_id, qc=qc_id, recovery=recovery["task_id"])
    before = snapshots(runtime)
    response = http.post(endpoint(ids), headers=ADMIN, json=payload(runtime, ids,
        request_key="evidence-four", evidence_text=EVIDENCE + " Yet another detail."))
    assert response.status_code == 409 and "BUDGET_EXHAUSTED" in response.text
    assert snapshots(runtime) == before


@pytest.mark.parametrize("change", ["text", "research", "qc", "recovery", "request_key"])
def test_same_key_or_same_source_cannot_open_another_research(subject, change):
    runtime, http, _, _, _, ids, _ = subject
    body = payload(runtime, ids)
    assert http.post(endpoint(ids), json=body, headers=ADMIN).status_code == 200
    modified = deepcopy(body)
    if change == "text":
        modified["evidence_text"] += " changed"
    elif change == "request_key":
        modified["request_key"] = "another-key"
    else:
        modified[change + "_task_id"] = "agt_other"
    before = snapshots(runtime)
    assert http.post(endpoint(ids), json=modified, headers=ADMIN).status_code == 409
    assert snapshots(runtime) == before


@pytest.mark.parametrize("damage", ["revision", "research_hash", "qc_hash", "recovery_hash", "input_hash", "raw", "mixed_error", "non_rights", "consumed", "active_task", "voice", "budget", "receipt"])
def test_changed_subject_proof_or_ineligible_state_never_creates_research(subject, damage):
    runtime, http, _, _, _, ids, _ = subject
    body = payload(runtime, ids)
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, ids["qc"])
        if damage == "revision":
            body["expected_revision_id"] = "rev_wrong"
        elif damage.endswith("_hash") and damage != "input_hash":
            body[damage.replace("_hash", "_result_hash")] = "0" * 64
        elif damage == "input_hash":
            qc.input_hash = "changed"
            session.add(qc)
        elif damage == "raw":
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc.id)).one()
            Path(submission.raw_result_path).write_text("{}", encoding="utf-8")
        elif damage in {"mixed_error", "non_rights"}:
            result = deepcopy(qc.result_data)
            error = {"severity": "error", "field": "facts", "message": "Wrong product claim"}
            result["issues"] = [*result["issues"], error] if damage == "mixed_error" else [error]
            # Keep the accepted proof coherent to exercise semantic eligibility itself.
            qc.result_data = result
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc.id)).one()
            submission.result_hash = stable_hash(result)
            Path(submission.raw_result_path).write_text(json.dumps(result), encoding="utf-8")
            for event in session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id,
                                                              AuditEvent.action.in_(["completed", "mpt_agent_consumed"]))).all():
                event.metadata_json = {**event.metadata_json, "result_hash": submission.result_hash}
                session.add(event)
            session.add_all([qc, submission])
            body["qc_result_hash"] = submission.result_hash
        elif damage == "consumed":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id,
                                                                 AuditEvent.action == "mpt_agent_consumed")).one())
        elif damage == "active_task":
            session.add(AgentTask(content_id=ids["content"], revision_id=ids["revision"], task_type=TaskType.CONTENT_RESEARCH,
                                 prompt_name="research", instructions="active", input_hash=stable_hash({})))
        elif damage == "voice":
            session.add(Artifact(content_id=ids["content"], revision_id=ids["revision"], artifact_type="voice",
                                 relative_path="voice.wav", mime_type="audio/wav", sha256="a" * 64, size_bytes=1, producer_stage="voice"))
        elif damage == "budget":
            # Current policy may narrow, never expand the frozen remaining allowance.
            runtime.settings.recovery_max_rounds = 1
        else:
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == ids["recovery"],
                                                           AuditEvent.action == "recovery_escalated")).one()
            event.metadata_json = {"reason": "changed", "action": "reroute_to_role"}
            session.add(event)
        session.commit()
    before = snapshots(runtime)
    response = http.post(endpoint(ids), json=body, headers=ADMIN)
    assert response.status_code == 409, response.text
    assert snapshots(runtime) == before


@pytest.mark.parametrize("different_key", [False, True])
def test_concurrent_requests_create_one_research(subject, different_key):
    runtime, http, _, _, _, ids, _ = subject
    first = payload(runtime, ids)
    second = {**first, "request_key": "other-key"} if different_key else deepcopy(first)
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda body: http.post(endpoint(ids), json=body, headers=ADMIN), [first, second]))
    assert sorted(row.status_code for row in responses) == ([200, 409] if different_key else [200, 200])
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_rights_evidence_requested")).all()) == 1
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_RESEARCH,
                                                        AgentTask.id != ids["research"])).all()) == 1


@pytest.mark.parametrize("parent_kind", ["research", "script"])
@pytest.mark.parametrize("damage", ["marker", "hash", "receipt", "upstream", "source_bytes", "larger_budget"])
def test_continuation_never_silently_discards_a_proven_recovery_root(subject, parent_kind, damage):
    from app.business.rights_evidence import continuation_inputs
    from glance_core.task_service import TaskConflictError

    runtime, http, researcher, script_agent, _, ids, value = subject
    response = http.post(endpoint(ids), json=payload(runtime, ids), headers=ADMIN)
    assert response.status_code == 200
    rid = response.json()["research_task_id"]
    claim = researcher.claim()
    assert researcher.complete(rid, claim["lease_token"], researcher.settings.agent_id, value)["advanced"] == "CONSUMED"
    with Session(runtime.engine) as session:
        if parent_kind == "research":
            parent = session.get(AgentTask, rid)
        else:
            parent = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                           AgentTask.id != ids["script"])).one()
        data = deepcopy(parent.input_data)
        if damage == "marker":
            for key in list(data):
                if key.startswith("rights_evidence_"):
                    data.pop(key)
            parent.input_data = data
            parent.input_hash = stable_hash(data)
        elif damage == "hash":
            parent.input_hash = "changed"
        elif damage == "upstream":
            if parent_kind == "script":
                data["upstream_task_ids"] = {"research": ids["research"]}
            else:
                data["lineage"] = {**data["lineage"], "root_task_id": "wrong-root"}
            parent.input_data = data
            parent.input_hash = stable_hash(data)
        elif damage == "receipt":
            receipt = session.exec(select(AuditEvent).where(AuditEvent.action == "mpt_rights_evidence_requested")).one()
            receipt.metadata_json = {**receipt.metadata_json, "research_input_hash": "changed"}
            session.add(receipt)
        elif damage == "source_bytes":
            artifact = session.exec(select(Artifact).where(Artifact.content_id == ids["content"])).first()
            content = session.get(Content, ids["content"])
            (Path(content.content_dir) / artifact.relative_path).write_bytes(b"changed image")
        session.add(parent)
        session.commit()
        with pytest.raises((TaskConflictError, ValueError)):
            continuation_inputs(session, parent, recovery_max_rounds=100 if damage == "larger_budget" else 3)


@pytest.mark.parametrize("field,value", [("evidence_text", " \n"), ("request_key", ""), ("qc_result_hash", "not-a-hash"),
                                          ("command", "anything"), ("path", "C:/arbitrary/file"), ("expected_revision_id", None)])
def test_request_accepts_only_strict_frozen_text_and_subject_fields(subject, field, value):
    runtime, http, _, _, _, ids, _ = subject
    before = snapshots(runtime)
    assert http.post(endpoint(ids), json={**payload(runtime, ids), field: value}, headers=ADMIN).status_code == 422
    assert snapshots(runtime) == before


@pytest.mark.parametrize("state", ["stopped", "running", "unresolved_child", "execution_running"])
def test_drained_run_retains_budget_and_active_work_is_rejected(subject, monkeypatch, state):
    from app.business.conductor_launch import LaunchRecord, _save
    from app.business.content_run import ContentRun, save_run

    runtime, http, _, _, _, ids, _ = subject
    now = utc_now()
    run = ContentRun(run_id="run_" + "a" * 32, content_id=ids["content"], revision_id=ids["revision"], generation=45,
                     state="running" if state == "running" else "stopped", created_at=now.isoformat(),
                     expires_at=(now + timedelta(minutes=90)).isoformat(), stop_requested=state != "running", ready_rounds=3)
    with Session(runtime.engine) as session:
        save_run(session, run, "fixture_frozen_run_budget")
        _save(session, LaunchRecord(launch_id="fixture", content_id=ids["content"], run_id=run.run_id, generation=45,
                                   state="RUNNING" if state == "execution_running" else "FINISHED", created_at=now.isoformat()), commit=False)
        session.commit()
    if state == "unresolved_child":
        monkeypatch.setattr("app.business.content_host.child_work_unresolved", lambda _: True)
    before = snapshots(runtime)
    response = http.post(endpoint(ids), json=payload(runtime, ids), headers=ADMIN)
    assert response.status_code == (200 if state == "stopped" else 409), response.text
    after = snapshots(runtime)
    assert all(after[key] == value for key, value in before.items())


def test_unreconciled_publish_intent_is_not_silently_reused(subject):
    runtime, http, _, _, _, ids, _ = subject
    with Session(runtime.engine) as session:
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="publish_intent", entity_id="unresolved",
                               action="publish_intent", metadata_json={"content_id": ids["content"], "status": "UPLOADING"}))
        session.commit()
    before = snapshots(runtime)
    assert http.post(endpoint(ids), json=payload(runtime, ids), headers=ADMIN).status_code == 409
    assert snapshots(runtime) == before
