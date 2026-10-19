"""Plan review receives exact generation facts without approving its own unfinished QC."""

from copy import deepcopy
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from glance_core import content_acceptance as acceptance
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, Content, RightsStatus, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from glance_core.task_schemas import RecoveryPlan

from app.business.runtime import AGENT_CONSUMED, OWNED
from test.business.test_agent_failure_recovery import registered_client
from test.business.test_audio_repair_visual_context import audio_rewrite_images as audio_rewrite_images
from test.business.test_audio_repair_visual_context import failed_image_contract as failed_image_contract
from test.business.test_audio_repair_visual_context import image_contract as image_contract
from test.business.test_deployment_http import app_for
from test.business.test_visual_plan_materialization import record_accepted_completion


@pytest.fixture
def pre_review_images(audio_rewrite_images):
    runtime, content_id, old_ids, expected, _ = audio_rewrite_images
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        content = session.get(Content, content_id)
        candidate = session.get(Candidate, content.candidate_id)
        candidate.rights_status = RightsStatus.OFFICIAL_REFERENCE
        session.add(candidate)
        previous = session.get(AgentTask, old_ids["plan"])
        segments = deepcopy(previous.result_data["segments"])
        segments[2].pop("asset_request")
        segments[2]["asset_id"] = expected[2]
        flow.invalidate_visual_plan(content_id, reason="voice_redub")
        plan = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, {
            **previous.input_data, "upstream_task_ids": {"script": old_ids["new_script"]},
            "allowed_asset_ids": expected, "allowed_layouts": ["image_card"],
            "voice_duration_sec": 21.0, "context_files": [],
        }, allow_multiple=True)
        record_accepted_completion(session, runtime, plan, {"segments": segments})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=plan.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(plan.result_data)}))
        session.commit()
        qc = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN_QC, {
            "upstream_task_ids": {"visual_plan": plan.id, "script": old_ids["new_script"]},
            "allowed_asset_ids": expected, "allowed_layouts": ["image_card"], "scenes": [],
            "voice_duration_sec": 21.0, "context_files": [],
        }, allow_multiple=True)
        session.commit()
        ids = {**old_ids, "plan": plan.id, "qc": qc.id}
        frozen = {task.id: (deepcopy(task.input_data), task.input_hash, deepcopy(task.result_data), task.status)
                  for task in session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()}
    return runtime, content_id, ids, expected, frozen


def test_real_leased_plan_qc_bundle_has_generation_provenance_without_self_approval(pre_review_images, tmp_path):
    runtime, content_id, ids, expected, frozen = pre_review_images
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == ids["qc"]
        bundle = client.bundle(claim["bundle_url"], claim["lease_token"])
        context = bundle["input"]["source_context_read_only"]
        assert context["evidence_gaps"] == []
        assert context["registration"]["rights_status"] == "OFFICIAL_REFERENCE"
        facts = context["generation_provenance"]
        assert facts["license_assessed"] is False and facts["composition_qc_assessed"] is False
        assert [row["artifact_id"] for row in facts["segments"]] == expected
        for index, row in enumerate(facts["segments"]):
            assert row["segment_index"] == index
            assert row["use"]["visual_plan_task_id"] == ids["plan"]
            assert row["request"].get("reference_artifact_id") is None
            assert row["generation_receipt"]["accepted"] and row["generation_receipt"]["consumed"]
            assert row["generation_receipt"]["status"] == "CANCELLED"
            assert row["prior_asset_qc"]
        with Session(runtime.engine) as session:
            plan, qc = session.get(AgentTask, ids["plan"]), session.get(AgentTask, ids["qc"])
            assert qc.status == TaskStatus.LEASED and qc.result_data is None
            with pytest.raises(ValueError, match="accepted/consumed proof missing"):
                acceptance.resolved_visual_plan(session, plan)
            for key, snapshot in frozen.items():
                task = session.get(AgentTask, key)
                assert (task.input_data, task.input_hash, task.result_data) == snapshot[:3]
                if key != qc.id:
                    assert task.status == snapshot[3]


def test_pre_review_helper_does_not_remove_real_source_registration_gap(pre_review_images):
    runtime, content_id, ids, _, _ = pre_review_images
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, ids["qc"])
        refs = {**qc.input_data["upstream_task_ids"], "research": "agt_not_current"}
        context = acceptance.visual_review_source_context(session, content_id, refs, review_task=qc)
        assert any("research" in gap for gap in context["evidence_gaps"])


def test_research_only_pre_review_reads_registration_without_final_original_media_gate(pre_review_images):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        candidate = session.get(Candidate, content.candidate_id)
        candidate.rights_status = RightsStatus.RESEARCH_ONLY
        session.add(candidate)
        session.commit()
        qc = session.get(AgentTask, ids["qc"])
        refs = qc.input_data["upstream_task_ids"]
        context = acceptance.visual_review_source_context(session, content_id, refs, review_task=qc)
        assert context["evidence_gaps"] == []
        assert context["registration"]["rights_status"] == "RESEARCH_ONLY"
        assert [row["artifact_id"] for row in context["generation_provenance"]["segments"]] == expected
        assert context["generation_provenance"]["license_assessed"] is False
        assert "original_media" not in context
        final_context = acceptance.source_context(session, content_id, refs)
        assert final_context["evidence_gaps"]
        with pytest.raises(ValueError, match="accepted/consumed proof missing"):
            acceptance.resolved_visual_plan(session, session.get(AgentTask, ids["plan"]))
        assert candidate.rights_status == RightsStatus.RESEARCH_ONLY


@pytest.mark.parametrize("duplicate_to_future", [False, True])
def test_research_context_repair_preserves_prepared_plan_use_mappings(pre_review_images, duplicate_to_future):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        previous = session.get(AgentTask, ids["plan"])
        raw = deepcopy(previous.result_data)
        if duplicate_to_future:
            raw["segments"][2]["asset_id"] = expected[0]
        flow.invalidate_visual_plan(content_id, reason="new-test-selection")
        plan = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, previous.input_data, allow_multiple=True)
        record_accepted_completion(session, runtime, plan, raw)
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=plan.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(raw)}))
        session.commit()
        prepared = deepcopy(raw)
        prepared["segments"][0]["purpose"] = "prepared selected purpose"
        if duplicate_to_future:
            prepared["segments"][2].pop("asset_id")
            prepared["segments"][2].update(asset_request={"prompt": "replace repeated image with a new concept"},
                                              purpose="prepared future intent")
        target = flow.create_agent_task(content_id, TaskType.VISUAL_PLAN_QC, {
            "upstream_task_ids": {"visual_plan": plan.id, "script": ids["new_script"]},
            "allowed_asset_ids": expected, "prepared_visual_plan": prepared,
            "visual_plan_result_sha256": stable_hash(raw),
        }, allow_multiple=True)
        record_accepted_completion(session, runtime, target, {"passed": False, "issues": [{"field": "rights"}]})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=target.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(target.result_data)}))
        recovery = flow.create_agent_task(content_id, TaskType.RECOVERY, {"lineage": {"round": 1}}, allow_multiple=True)
        session.commit()
        repair = flow._create_context_repair_from_recovery(recovery, RecoveryPlan(
            failure_class="BUSINESS_QC", action="reroute_to_role", target_task_type="content_research",
            reason="read selected composition provenance"), target_task=target)
        context = repair.input_data["source_context_read_only"]
        assert context["asset_uses"][0]["purpose"] == "prepared selected purpose"
        assert context["generation_provenance"]["segments"][0]["use"]["purpose"] == "prepared selected purpose"
        assert context["visual_plan"]["selection_sha256"] == stable_hash(prepared)
        if duplicate_to_future:
            assert {use["segment_index"] for use in context["asset_uses"]} == {0, 1, 3, 4, 5, 6}
            assert context["generation_intents"][0]["segment_index"] == 2
            assert context["generation_intents"][0]["use"]["purpose"] == "prepared future intent"
        assert target.result_data["passed"] is False


@pytest.mark.parametrize("damage", [None, "hash", "bool_version", "missing_version", "facts", "raw_format", "final_version"])
def test_reissued_qc_freezes_exact_pre_review_facts_and_rejects_drift(pre_review_images, tmp_path, damage):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        qc = session.get(AgentTask, ids["qc"])
        context = acceptance.visual_review_source_context(session, content_id, qc.input_data["upstream_task_ids"],
                                                         review_task=qc, include_references=True)
        inputs = {**qc.input_data, "visual_review_context_version": 1,
                  "visual_review_source_context": context, "visual_review_source_context_sha256": stable_hash(context)}
        if damage == "hash":
            inputs["visual_review_source_context_sha256"] = "changed"
        elif damage == "bool_version":
            inputs["visual_review_context_version"] = True
        elif damage == "missing_version":
            inputs.pop("visual_review_context_version")
        elif damage == "facts":
            altered = deepcopy(context)
            altered["generation_provenance"]["segments"][0]["generation_receipt"]["consumed"] = False
            inputs.update(visual_review_source_context=altered, visual_review_source_context_sha256=stable_hash(altered))
        elif damage == "final_version":
            inputs["source_context_version"] = 1
        elif damage == "raw_format":
            artifact = session.get(Artifact, expected[2])
            accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == artifact.producer_stage,
                                                                 AgentSubmission.validation_status == "ACCEPTED")).one()
            raw = Path(accepted.raw_result_path)
            raw.write_text(json.dumps(json.loads(raw.read_text(encoding="utf-8")), indent=2), encoding="utf-8")
        qc.input_data, qc.input_hash = inputs, stable_hash(inputs)
        session.add(qc)
        session.commit()
    with TestClient(app_for(runtime, tmp_path)) as http:
        client = registered_client(http, runtime, "visual")
        claim = client.claim()
        assert claim["task_id"] == ids["qc"]
        response = http.get(claim["bundle_url"], headers={"Authorization": f"Bearer {claim['lease_token']}"})
        assert response.status_code == (200 if damage is None else 409), response.text
        if damage is None:
            assert response.json()["input"]["source_context_read_only"] == context


def test_prepared_future_generation_is_an_intent_and_does_not_require_future_qc(pre_review_images):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        plan, qc = session.get(AgentTask, ids["plan"]), session.get(AgentTask, ids["qc"])
        prepared = deepcopy(plan.result_data)
        prepared["segments"][2].pop("asset_id")
        prepared["segments"][2]["asset_request"] = {"prompt": "new generic concept"}
        qc.input_data = {**qc.input_data, "prepared_visual_plan": prepared,
                         "visual_plan_result_sha256": stable_hash(plan.result_data)}
        qc.input_hash = stable_hash(qc.input_data)
        session.add(qc)
        session.commit()
        context = acceptance.visual_review_source_context(session, content_id, qc.input_data["upstream_task_ids"],
                                                         review_task=qc, include_references=True)
        assert context["evidence_gaps"] == []
        assert [entry["artifact_id"] for entry in context["generation_provenance"]["segments"]] == expected[:2] + expected[3:]
        assert context["generation_intents"][0]["segment_index"] == 2
        assert context["reference_uses"] == []
        assert expected[2] not in {entry["artifact_id"] for entry in context["artifacts"]}


@pytest.mark.parametrize("damage", ["prepared_hash", "qc_hash", "qc_script", "reference"])
def test_pre_review_selection_and_reference_fail_closed(pre_review_images, damage):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        plan, qc = session.get(AgentTask, ids["plan"]), session.get(AgentTask, ids["qc"])
        prepared = deepcopy(plan.result_data)
        prepared["segments"][2].pop("asset_id")
        prepared["segments"][2]["asset_request"] = {"prompt": "new generic concept"}
        if damage == "reference":
            prepared["segments"][2]["asset_request"]["reference_artifact_id"] = expected[0]
        qc.input_data = {**qc.input_data, "prepared_visual_plan": prepared,
                         "visual_plan_result_sha256": "changed" if damage == "prepared_hash" else stable_hash(plan.result_data)}
        if damage == "qc_script":
            qc.input_data["upstream_task_ids"] = {"visual_plan": plan.id, "script": "agt_missing"}
        qc.input_hash = "changed" if damage == "qc_hash" else stable_hash(qc.input_data)
        session.add(qc)
        session.commit()
        with pytest.raises(ValueError):
            acceptance.visual_review_source_context(session, content_id,
                                                     {"visual_plan": plan.id, "script": ids["new_script"]}, review_task=qc)


@pytest.mark.parametrize("missing_raw", [False, True])
def test_existing_research_context_repair_uses_pre_review_facts_and_blocks_bad_receipts(pre_review_images, missing_raw):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        target = session.get(AgentTask, ids["qc"])
        record_accepted_completion(session, runtime, target, {"passed": False, "issues": [
            {"field": "rights", "message": "exact generation provenance needed"}]})
        session.add(AuditEvent(actor_type="system", actor_id="fixture", entity_type="agent_task",
                               entity_id=target.id, action=AGENT_CONSUMED,
                               metadata_json={"result_hash": stable_hash(target.result_data)}))
        recovery = flow.create_agent_task(content_id, TaskType.RECOVERY, {"lineage": {"round": 1}}, allow_multiple=True)
        session.commit()
        if missing_raw:
            artifact = session.get(Artifact, expected[2])
            accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == artifact.producer_stage,
                                                                 AgentSubmission.validation_status == "ACCEPTED")).one()
            Path(accepted.raw_result_path).unlink()
        plan = RecoveryPlan(failure_class="BUSINESS_QC", action="reroute_to_role",
                            target_task_type="content_research", reason="read exact generation facts")
        task = flow._create_context_repair_from_recovery(recovery, plan, target_task=target)
        if missing_raw:
            assert task is None
            assert session.get(Content, content_id).current_stage == "recovery_blocked"
        else:
            context = task.input_data["source_context_read_only"]
            assert context["scope"] == "visual_plan_pre_review"
            assert context["evidence_gaps"] == []
            assert [row["artifact_id"] for row in context["generation_provenance"]["segments"]] == expected
            assert context["reference_uses"] == []
        assert target.result_data["passed"] is False


@pytest.mark.parametrize("damage", ["missing_raw", "raw_bytes", "missing_accepted", "actor", "consume",
                                    "owner_actor", "consume_actor",
                                    "image_bytes", "input_hash", "reference", "foreign", "stale", "undeclared"])
def test_pre_review_context_rejects_unproven_materials(pre_review_images, damage):
    runtime, content_id, ids, expected, _ = pre_review_images
    with Session(runtime.engine) as session:
        plan, qc = session.get(AgentTask, ids["plan"]), session.get(AgentTask, ids["qc"])
        artifact = session.get(Artifact, expected[2])
        generation = session.get(AgentTask, artifact.producer_stage)
        accepted = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == generation.id,
                                                             AgentSubmission.validation_status == "ACCEPTED")).one()
        if damage == "missing_raw":
            Path(accepted.raw_result_path).unlink()
        elif damage == "raw_bytes":
            Path(accepted.raw_result_path).write_text("{}", encoding="utf-8")
        elif damage == "missing_accepted":
            session.delete(accepted)
        elif damage == "actor":
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                         AuditEvent.action == "completed")).one()
            event.actor_id = "different-agent"
            session.add(event)
        elif damage == "consume":
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                         AuditEvent.action == AGENT_CONSUMED)).one()
            session.delete(event)
        elif damage in {"owner_actor", "consume_actor"}:
            action = OWNED if damage == "owner_actor" else AGENT_CONSUMED
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                         AuditEvent.action == action)).one()
            event.actor_type = "human"
            session.add(event)
        elif damage == "image_bytes":
            (Path(session.get(Content, content_id).content_dir) / artifact.relative_path).write_bytes(b"changed")
        elif damage == "input_hash":
            generation.input_hash = "changed"
            session.add(generation)
        elif damage == "reference":
            inputs = deepcopy(generation.input_data)
            inputs["requests"][0]["asset_request"]["reference_artifact_id"] = expected[0]
            generation.input_data, generation.input_hash = inputs, stable_hash(inputs)
            owned = session.exec(select(AuditEvent).where(AuditEvent.entity_id == generation.id,
                                                         AuditEvent.action == OWNED)).one()
            owned.metadata_json = {"input_hash": generation.input_hash}
            session.add_all([generation, owned])
        elif damage == "foreign":
            candidate = Candidate(source_type="agent", source_id="foreign", source_url="https://example.org/foreign",
                                  original_title="Foreign")
            session.add(candidate)
            session.flush()
            foreign = Content(candidate_id=candidate.id, title="Foreign", content_dir=str(runtime.settings.content_dir / "foreign"))
            session.add(foreign)
            session.flush()
            artifact.content_id = foreign.id
            session.add(artifact)
        elif damage == "stale":
            runtime.workflow_service(session).create_agent_task(content_id, TaskType.VISUAL_PLAN,
                                                                {**plan.input_data, "newer": True}, allow_multiple=True)
        else:
            plan.input_data = {**plan.input_data, "allowed_asset_ids": expected[:2] + expected[3:]}
            plan.input_hash = stable_hash(plan.input_data)
            owned = session.exec(select(AuditEvent).where(AuditEvent.entity_id == plan.id,
                                                         AuditEvent.action == OWNED)).one()
            owned.metadata_json = {"input_hash": plan.input_hash}
            session.add_all([plan, owned])
        session.commit()
        with pytest.raises(ValueError):
            acceptance.visual_review_source_context(session, content_id, qc.input_data["upstream_task_ids"],
                                                     review_task=qc)
