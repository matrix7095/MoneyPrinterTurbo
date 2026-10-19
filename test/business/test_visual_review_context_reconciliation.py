"""An admin can reissue only a proved circular pre-review bundle, preserving history."""

from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from glance_core.content_acceptance import visual_review_source_context
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, ContentStatus, PublicationMode, PublicationRecord, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_service import stable_hash
from glance_media.executor import sha256_file
from sqlmodel import Session, select

from app.business.content_run import ContentRun, OFFER_ACTION, save_run
from app.business.content_host import CLAIM
from app.business.runtime import VISUAL_REVIEW_LEGACY_SOURCE
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_deployment_http import app_for, enable, scheduler
from test.business.test_content_run_http import start, scoped_dispatch
from test.business.test_discovery_http import ADMIN
from test.business.test_visual_pre_review_source_context import pre_review_images as pre_review_images
from test.business.test_visual_pre_review_source_context import audio_rewrite_images as audio_rewrite_images
from test.business.test_visual_pre_review_source_context import failed_image_contract as failed_image_contract
from test.business.test_visual_pre_review_source_context import image_contract as image_contract
from test.business.test_visual_plan_materialization import record_accepted_completion
from test.business.test_visual_repair_context_reconciliation import consumed, frozen_history, assert_frozen


def endpoint(content_id):
    return f"/api/v1/contents/{content_id}/reconcile-visual-plan-review-context"


@pytest.fixture
def review_reconciliation(pre_review_images, tmp_path):
    runtime, cid, ids, image_ids, _ = pre_review_images
    with TestClient(app_for(runtime, tmp_path)) as http:
        visual = registered_client(http, runtime, "visual")
        assistant = registered_client(http, runtime, "assistant")
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            plan, qc = session.get(AgentTask, ids["plan"]), session.get(AgentTask, ids["qc"])
            content = session.get(Content, cid)
            voice_path = Path(content.content_dir) / "narration.mp3"
            voice_path.write_bytes(b"frozen voice")
            voice = Artifact(content_id=cid, artifact_type="voice", relative_path=voice_path.name,
                             sha256=sha256_file(voice_path), size_bytes=voice_path.stat().st_size,
                             mime_type="audio/mpeg", producer_stage="voice")
            session.add(voice)
            plan.input_data = {**plan.input_data, "voice_artifact": {
                key: getattr(voice, key) for key in ("id", "relative_path", "sha256", "size_bytes")}}
            plan.input_hash = stable_hash(plan.input_data)
            from app.business.runtime import OWNED
            owned = session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan.id, AuditEvent.action == OWNED)).one()
            owned.metadata_json = {"input_hash": plan.input_hash}
            session.add_all([plan, owned])
            issues = [{"field": "rights", "severity": "error", "segment_index": 0,
                       "message": "、".join(image_ids) + ": source_context_read_only.evidence_gaps: visual material accepted/consumed proof missing"}]
            started = utc_now()
            qc.started_at, qc.lease_token_hash = started, "a" * 64
            record_accepted_completion(session, runtime, qc, {"passed": False, "issues": issues})
            qc.completed_at = started + timedelta(seconds=2)
            session.add(qc)
            consumed(session, qc)
            session.add(AuditEvent(actor_type="agent", actor_id=visual.settings.agent_id,
                                   entity_type="agent_task", entity_id=qc.id, action="bundle_served",
                                   created_at=started + timedelta(seconds=1),
                                   metadata_json={"served_prompt_hash": stable_hash(qc.instructions), "schema_version": qc.schema_version}))
            session.commit()
            recovery = flow.create_qc_recovery_task(qc, plan)
            result = {"failure_class": "BUSINESS_QC", "action": "reroute_to_role",
                      "target_task_type": "content_research", "reason": "missing delivered provenance"}
            record_accepted_completion(session, runtime, recovery, result)
            session.commit()
            flow.apply_recovery_plan(recovery)
            consumed(session, recovery)
            for task, agent in ((plan, visual.settings.agent_id), (qc, visual.settings.agent_id),
                                (recovery, assistant.settings.agent_id)):
                task.assigned_agent_id = agent
                session.add(task)
                submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
                submission.agent_id = agent
                session.add(submission)
                completed = session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id,
                                                                  AuditEvent.action == "completed")).one()
                completed.actor_id = agent
                session.add(completed)
            content = session.get(Content, cid)
            assert content.current_stage == "recovery_blocked"
            now = utc_now()
            child = {"child_id": "fixture-child", "parent_thread_id": "fixture-parent", "native_agent_type": "executor",
                     "model": "gpt-6.1-sol", "reasoning_effort": "high", "ephemeral": True,
                     "started": {"parent_thread_id": "fixture-parent", "parent_turn_id": "fixture-turn", "item_id": "fixture-item",
                                 "agent_path": "/root/fixture", "child_id": "fixture-child"},
                     "metadata_rpc_id": "fixture-rpc", "issued_prompt_sha256": "f" * 64}
            worker = {"task_id": qc.id, "agent_id": visual.settings.agent_id, "run_id": "run_" + "c" * 32,
                      "run_generation": 29, "host_epoch": 1, "input_hash": qc.input_hash,
                      "content_id": cid, "task_type": "visual_plan_qc", "role_id": "visual", "scheduler_id": "mpt-codex"}
            offer_hash = stable_hash(worker)
            session.add(AuditEvent(actor_type="system", actor_id="mpt-content-host", entity_type="agent_task",
                                   entity_id=qc.id, action=CLAIM, created_at=started + timedelta(milliseconds=1), metadata_json={
                                       "task_id": qc.id, "agent_id": visual.settings.agent_id, "request_id": "fixture-request",
                                       "claim_id": "fixture-claim", "offer_hash": offer_hash, "run_id": worker["run_id"],
                                       "generation": 29, "host_epoch": 1, "parent_thread_id": "fixture-parent",
                                       "child_id": "fixture-child", "started_at": started.isoformat(), "lease_token_hash": qc.lease_token_hash}))
            session.add(AuditEvent(actor_type="scheduler", actor_id="mpt-codex", entity_type="scheduler_offer",
                                   entity_id="fixture-request", action=OFFER_ACTION, metadata_json={"worker": worker, "worker_hash": offer_hash}))
            run = ContentRun(run_id=worker["run_id"], content_id=cid, generation=29,
                             state="stopped", stop_requested=True, stop_reason="operator_stop",
                             created_at=(now - timedelta(minutes=10)).isoformat(),
                             expires_at=(now + timedelta(minutes=110)).isoformat(), host_epoch=1,
                             parent_thread_id="fixture-parent", process_identity={
                                 "pid": 1234, "started_at": (now - timedelta(hours=1)).isoformat(),
                                 "instance_id": "fixture-host", "binary_path": str(tmp_path / "host.exe"),
                                 "binary_sha256": "a" * 64, "version": "fixture"},
                             host_observed_at=(now - timedelta(minutes=5)).isoformat(),
                             intents={"fixture-request": {"state": "TERMINAL", "host_epoch": 1, "offer_hash": offer_hash,
                                                          "child": child, "claim_id": "fixture-claim"}})
            save_run(session, run, "mpt_content_run_drained")
            session.commit()
            proof_path = tmp_path / "storage" / "smoke" / "historical-deployment.json"
            proof_path.parent.mkdir(parents=True)
            proof_path.write_text(json.dumps({"at": (now - timedelta(minutes=20)).isoformat(), "phase": "after",
                "head": "f" * 40, "target_commit": "f" * 40, "database_media_and_negatives_unchanged": True,
                "source_sha256": {"packages/core/src/glance_core/content_acceptance.py": VISUAL_REVIEW_LEGACY_SOURCE}}), encoding="utf-8")
            expected = {"version": 1, "evidence_mode": "accepted_failure_plus_deployed_derivation_v1", "run": {key: getattr(run, key) for key in (
                "run_id", "generation", "host_epoch", "parent_thread_id", "process_identity")},
                "corrected_context_sha256": stable_hash(visual_review_source_context(session, cid,
                    qc.input_data["upstream_task_ids"], review_task=qc, include_references=True)),
                "historical_deployment": {"relative_path": str(proof_path.relative_to(tmp_path)),
                                          "sha256": sha256_file(proof_path), "revision": "f" * 40}}
            for key, task in (("plan", plan), ("qc", qc), ("recovery", recovery)):
                raw = runtime.settings.agent_submission_dir / task.id / str(task.attempt) / "result.json"
                expected[key] = {"task_id": task.id, "input_hash": task.input_hash,
                                 "result_hash": stable_hash(task.result_data), "attempt": task.attempt,
                                 "raw_file_sha256": sha256_file(raw)}
            ids.update(recovery=recovery.id, voice=voice.id)
        yield runtime, http, cid, expected, ids, image_ids, proof_path, visual


def test_reissues_review_without_changing_any_accepted_history(review_reconciliation):
    runtime, http, cid, expected, ids, image_ids, _, _ = review_reconciliation
    before = frozen_history(runtime)
    assert http.post(endpoint(cid), json=expected).status_code == 401
    response = http.post(endpoint(cid), headers=ADMIN, json=expected)
    assert response.status_code == 200, response.text
    new_id = response.json()["visual_plan_qc_task_id"]
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, new_id)
        assert task.status == TaskStatus.PENDING and task.attempt == 0 and task.result_data is None
        assert task.input_data["supersedes_qc_id"] == ids["qc"]
        assert task.input_data["visual_review_context_version"] == 1
        assert task.input_data["original_recovery_task_id"] == ids["recovery"]
        assert task.input_data["original_recovery_lineage"]["round"] == 1
        assert task.input_data["original_recovery_lineage"]["max_rounds"] == 3
        assert [row["artifact_id"] for row in task.input_data["visual_review_source_context"]["generation_provenance"]["segments"]] == image_ids
        assert session.get(Content, cid).current_stage == "visual_plan_qc"
        assert session.get(AgentTask, ids["qc"]).result_data["passed"] is False
        assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.RECOVERY,
                                                       AgentTask.input_data["failure"]["code"].as_string() == "VISUAL_PLAN_QC_FAILED")).all()) == 1
    # Replays never recreate a task or reopen content after later progress.
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, new_id)
        record_accepted_completion(session, runtime, task, {"passed": False, "issues": [
            {"severity": "error", "field": "composition", "message": "new genuine finding"}]})
        consumed(session, task)
        session.commit()
        flow = runtime.workflow_service(session)
        next_recovery = flow.create_qc_recovery_task(task, session.get(AgentTask, ids["plan"]))
        assert next_recovery.id != ids["recovery"]
        assert next_recovery.input_data["lineage"]["round"] == 2
        previous_lineage = session.get(AgentTask, ids["recovery"]).input_data["lineage"]
        assert next_recovery.input_data["lineage"]["root_task_id"] == previous_lineage["root_task_id"]
        assert next_recovery.input_data["lineage"]["root_cause_signature"] == previous_lineage["root_cause_signature"]
        assert next_recovery.input_data["lineage"]["repair_fingerprint"] != previous_lineage["repair_fingerprint"]
        stage_before = session.get(Content, cid).current_stage
    replay = http.post(endpoint(cid), headers=ADMIN, json=expected)
    assert replay.status_code == 200 and replay.json()["replayed"], replay.text
    assert replay.json()["visual_plan_qc_task_id"] == new_id
    with Session(runtime.engine) as session:
        assert session.get(Content, cid).current_stage == stage_before


def test_fresh_qc_is_normally_claimable_with_frozen_facts_and_no_approval(review_reconciliation):
    runtime, http, cid, expected, ids, _, _, client = review_reconciliation
    response = http.post(endpoint(cid), headers=ADMIN, json=expected)
    assert response.status_code == 200, response.text
    new_id = response.json()["visual_plan_qc_task_id"]
    enable(http, enabled=False)
    run = start(http, cid)
    plan = scheduler(http).scheduler_plan("mpt-codex", 2, run_id=run["run_id"])
    worker = plan["worker_requests"][0]
    assert worker["task_id"] == new_id
    dispatched = scoped_dispatch(http, worker)
    assert dispatched.status_code == 200, dispatched.text
    claim = dispatched.json()
    assert claim["task_id"] == new_id
    bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
    assert bundle["input"]["source_context_read_only"] == bundle["input"]["visual_review_source_context"]
    assert bundle["input"]["source_context_read_only"]["evidence_gaps"] == []
    from glance_core.content_acceptance import resolved_visual_plan
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, new_id)
        assert task.status == TaskStatus.LEASED and task.result_data is None
        with pytest.raises(ValueError, match="accepted/consumed proof missing"):
            resolved_visual_plan(session, session.get(AgentTask, ids["plan"]))


def test_replay_after_runtime_restart_is_read_only(review_reconciliation):
    runtime, http, cid, expected, _, _, _, _ = review_reconciliation
    first = http.post(endpoint(cid), headers=ADMIN, json=expected)
    assert first.status_code == 200, first.text
    from app.business.runtime import BusinessRuntime
    restarted = BusinessRuntime(runtime.settings)
    restarted.workspace_root = runtime.workspace_root
    try:
        replay = restarted.reconcile_visual_plan_review_context(cid, expected)
        assert replay["replayed"] and replay["visual_plan_qc_task_id"] == first.json()["visual_plan_qc_task_id"]
    finally:
        restarted.close()


def test_concurrent_requests_create_one_review_receipt(review_reconciliation):
    runtime, http, cid, expected, _, _, _, _ = review_reconciliation
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: http.post(endpoint(cid), headers=ADMIN, json=expected), range(2)))
    assert all(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert len({response.json()["visual_plan_qc_task_id"] for response in responses}) == 1
    assert sorted(response.json()["replayed"] for response in responses) == [False, True]
    from app.business.runtime import VISUAL_REVIEW_CONTEXT
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.action == VISUAL_REVIEW_CONTEXT)).all()) == 1


def test_receipt_write_failure_rolls_back_new_task_and_content(review_reconciliation, monkeypatch):
    runtime, _, cid, expected, _, _, _, _ = review_reconciliation
    from app.business.runtime import VISUAL_REVIEW_CONTEXT
    before = frozen_history(runtime)
    with Session(runtime.engine) as session:
        content_before = session.get(Content, cid).model_dump()
        task_ids = set(session.exec(select(AgentTask.id)).all())
    original_add = Session.add

    def reject_receipt(session, item, *args, **kwargs):
        if isinstance(item, AuditEvent) and item.action == VISUAL_REVIEW_CONTEXT:
            raise RuntimeError("synthetic receipt unavailable")
        return original_add(session, item, *args, **kwargs)

    monkeypatch.setattr(Session, "add", reject_receipt)
    with pytest.raises(RuntimeError, match="synthetic receipt unavailable"):
        runtime.reconcile_visual_plan_review_context(cid, expected)
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert session.get(Content, cid).model_dump() == content_before
        assert set(session.exec(select(AgentTask.id)).all()) == task_ids


@pytest.mark.parametrize("damage", ["result_hash", "raw_hash", "raw_bytes", "actor", "stale_plan", "stale_qc",
                                  "facts_hash", "proof_gap", "asset_bytes", "blocked_event", "recovery_budget",
                                  "active_lease", "active_offer", "publication", "terminal", "run", "rights",
                                  "late_receipt", "late_registration", "native_claim", "native_offer", "prompt",
                                  "proof_late", "non_prereview", "genuine_error", "new_voice"])
def test_rejects_unproved_or_active_context_without_writes(review_reconciliation, damage):
    runtime, http, cid, expected, ids, image_ids, proof_path, _ = review_reconciliation
    with Session(runtime.engine) as session:
        qc, recovery = session.get(AgentTask, ids["qc"]), session.get(AgentTask, ids["recovery"])
        if damage in {"result_hash", "raw_hash"}:
            expected["qc"]["result_hash" if damage == "result_hash" else "raw_file_sha256"] = "0" * 64
        elif damage == "raw_bytes":
            raw = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc.id)).one()
            Path(raw.raw_result_path).write_text("{}", encoding="utf-8")
        elif damage == "actor":
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id, AuditEvent.action == "completed")).one()
            row.actor_id = "intruder"
            session.add(row)
        elif damage in {"stale_plan", "stale_qc"}:
            runtime.workflow_service(session).create_agent_task(cid, TaskType.VISUAL_PLAN if damage == "stale_plan" else TaskType.VISUAL_PLAN_QC,
                                                                {"newer": True}, allow_multiple=True)
        elif damage == "facts_hash":
            expected["corrected_context_sha256"] = "0" * 64
        elif damage == "proof_gap":
            historical = json.loads(proof_path.read_text(encoding="utf-8"))
            historical["source_sha256"] = {}
            proof_path.write_text(json.dumps(historical), encoding="utf-8")
            expected["historical_deployment"]["sha256"] = sha256_file(proof_path)
        elif damage == "asset_bytes":
            artifact = session.get(Artifact, image_ids[2])
            (Path(session.get(Content, cid).content_dir) / artifact.relative_path).write_bytes(b"changed")
        elif damage == "blocked_event":
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == "recovery_escalated")).one()
            row.actor_id = "different"
            session.add(row)
        elif damage == "recovery_budget":
            recovery.input_data = {**recovery.input_data, "lineage": {**recovery.input_data["lineage"], "round": 0}}
            session.add(recovery)
        elif damage == "active_lease":
            qc.status = TaskStatus.LEASED
            session.add(qc)
        elif damage == "active_offer":
            session.add(SchedulerOffer(request_id="active", task_id=qc.id, expires_at=utc_now() + timedelta(minutes=1)))
        elif damage == "publication":
            session.add(PublicationRecord(content_id=cid, platform="douyin", mode=PublicationMode.EXECUTOR))
        elif damage == "terminal":
            content = session.get(Content, cid)
            content.business_status = ContentStatus.PUBLISHED
            session.add(content)
        elif damage == "run":
            expected["run"]["generation"] += 1
        elif damage == "rights":
            from glance_core.models import Candidate, RightsStatus
            content = session.get(Content, cid)
            candidate = session.get(Candidate, content.candidate_id)
            candidate.rights_status = RightsStatus.REJECTED
            session.add(candidate)
        elif damage in {"late_receipt", "late_registration"}:
            delivery = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id,
                                                             AuditEvent.action == "bundle_served")).one()
            if damage == "late_receipt":
                from app.business.runtime import AGENT_CONSUMED
                generation = session.get(Artifact, image_ids[0]).producer_stage
                row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation,
                                                            AuditEvent.action == AGENT_CONSUMED)).one()
                row.created_at = delivery.created_at + timedelta(seconds=1)
            else:
                from glance_core.models import Candidate
                row = session.get(Candidate, session.get(Content, cid).candidate_id)
                row.updated_at = delivery.created_at + timedelta(seconds=1)
            session.add(row)
        elif damage == "native_claim":
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id, AuditEvent.action == CLAIM)).one()
            row.metadata_json = {**row.metadata_json, "agent_id": "intruder"}
            session.add(row)
        elif damage == "native_offer":
            row = session.exec(select(AuditEvent).where(AuditEvent.action == OFFER_ACTION)).one()
            row.actor_type = "human"
            session.add(row)
        elif damage == "prompt":
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id, AuditEvent.action == "bundle_served")).one()
            row.metadata_json = {**row.metadata_json, "served_prompt_hash": "0" * 64}
            session.add(row)
        elif damage == "proof_late":
            historical = json.loads(proof_path.read_text(encoding="utf-8"))
            historical["at"] = (utc_now() + timedelta(hours=1)).isoformat()
            proof_path.write_text(json.dumps(historical), encoding="utf-8")
            expected["historical_deployment"]["sha256"] = sha256_file(proof_path)
        elif damage == "non_prereview":
            qc.input_data = {**qc.input_data, "source_context_version": 1}
            session.add(qc)
        elif damage == "genuine_error":
            qc.result_data = {"passed": False, "issues": [*qc.result_data["issues"],
                {"severity": "error", "field": "composition", "message": "genuine unrelated blocker"}]}
            session.add(qc)
        elif damage == "new_voice":
            current = session.get(Artifact, ids["voice"])
            session.add(Artifact(content_id=cid, artifact_type="voice", relative_path="different.mp3",
                                 sha256="a" * 64, size_bytes=3, producer_stage="voice", mime_type="audio/mpeg",
                                 created_at=current.created_at + timedelta(seconds=1)))
        if damage == "non_prereview":
            from app.business.runtime import OWNED
            qc.input_hash = stable_hash(qc.input_data)
            expected["qc"]["input_hash"] = qc.input_hash
            row = session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id, AuditEvent.action == OWNED)).one()
            row.metadata_json = {"input_hash": qc.input_hash}
            session.add_all([qc, row])
        elif damage == "genuine_error":
            from app.business.runtime import AGENT_CONSUMED
            digest = stable_hash(qc.result_data)
            accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == qc.id)).one()
            accepted.result_hash = digest
            Path(accepted.raw_result_path).write_text(json.dumps(qc.result_data), encoding="utf-8")
            for row in session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc.id,
                                                             AuditEvent.action.in_(["completed", AGENT_CONSUMED]))).all():
                row.metadata_json = {"result_hash": digest}
                session.add(row)
            expected["qc"].update(result_hash=digest, raw_file_sha256=sha256_file(Path(accepted.raw_result_path)))
            session.add(accepted)
        session.commit()
    before = frozen_history(runtime)
    response = http.post(endpoint(cid), headers=ADMIN, json=expected)
    assert response.status_code == 409, response.text
    if damage == "genuine_error":
        assert response.json()["detail"] == "VISUAL_REVIEW_EXACT_PREEXISTING_FACTS_UNPROVEN"
    if damage == "non_prereview":
        assert response.json()["detail"] == "VISUAL_REVIEW_NOT_LEGACY_PRE_REVIEW"
    assert_frozen(runtime, before)
    with Session(runtime.engine) as session:
        assert not [row for row in session.exec(select(AgentTask)).all() if row.input_data.get("visual_review_context_version")]
