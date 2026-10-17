"""A bounded new review never turns missing observations into acceptance."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import math
import shutil

import pytest
from glance_core.content_acceptance import acceptance_subject, resolved_visual_plan, verify_acceptance
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Content, RuntimeSetting, SchedulerOffer, TaskStatus, TaskType, utc_now
from glance_core.task_service import TaskConflictError
from sqlmodel import Session, select

from app.business import pipeline
from app.business.runtime import AGENT_CONSUMED, OWNED, QC_REINSPECTION
from test.business.test_content_qc_recovery import accept, failed_qc as failed_qc, recovery_rows
from test.business.test_agent_failure_recovery import record_intent, registered_client
from test.business.test_visual_joint_evidence import digest, joint_pack as joint_pack
from test.business.test_visual_plan_materialization import image_contract as image_contract


COMMITMENTS = ["CONTENT_QC_SAME_SUBJECT_FULL_REVIEW_V1", "CONTENT_QC_ALL_NINE_HARD_GATES"]


@pytest.fixture
def incomplete_qc(failed_qc, joint_pack, request):
    runtime, http, content_id, old_id, plan_id, root = failed_qc
    native_visual = registered_client(http, runtime, "visual")
    with Session(runtime.engine) as session:
        plan_task = session.get(AgentTask, plan_id)
        for task, agent_id in ((plan_task, native_visual.settings.agent_id),
                               (session.get(AgentTask, plan_task.input_data["upstream_task_ids"]["script"]),
                                session.get(AgentTask, old_id).assigned_agent_id)):
            task.assigned_agent_id = agent_id
            session.add(task)
            submission = session.exec(select(AgentSubmission).where(AgentSubmission.task_id == task.id)).one()
            submission.agent_id = task.assigned_agent_id
            session.add(submission)
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id, AuditEvent.action == "completed")).one()
            event.actor_id = task.assigned_agent_id
            session.add(event)
        session.commit()
    # A previous genuine business-QC round already consumed the shared cause budget.
    assert runtime.consume_agent(old_id)
    with Session(runtime.engine) as session:
        previous = recovery_rows(runtime)[0]
        prior = session.get(AgentTask, previous.id)
        prior.status = TaskStatus.CANCELLED
        session.add(prior)
        session.commit()
        old = session.get(AgentTask, old_id)
        evidence, review, audio = deepcopy(joint_pack[1:4])
        pack_root = root / "render" / "joint"
        shutil.copytree(joint_pack[0] / "render" / "joint", pack_root)
        plan = resolved_visual_plan(session, session.get(AgentTask, plan_id))
        assert len(plan["segments"]) == 1
        end = plan["segments"][-1]["narration_end_sec"]
        evidence["manifest"].update(visual_plan=plan, video_end_sec=end)
        for frame in evidence["manifest"]["frames"]:
            frame.update(pts_seconds=str(frame["index"] * end / 8), display_duration_sec=end / 8,
                         display_end_sec=(frame["index"] + 1) * end / 8)
        evidence["manifest"]["subtitles"]["events"] = [{"index": 0, "start_sec": 0, "end_sec": end, "text": "产品"}]
        sha = old.input_data["acceptance_subject"]["video_sha256"]
        evidence["manifest"]["input_video"] = {key: sha for key in evidence["manifest"]["input_video"]}
        path = pack_root / "manifest.json"
        path.write_text(json.dumps(evidence["manifest"]), encoding="utf-8")
        evidence["manifest_sha256"] = review["manifest_sha256"] = digest(path)
        audio.update(video_sha256=sha, segments=[{"start_sec": 0, "end_sec": end, "text": "产品",
                     "words": [{"word": "产品", "start_sec": 0, "end_sec": end, "probability": .99}]}])
        audio["signal"].update(duration_sec=end, rms_dbfs=[-20] * math.ceil(end / .1))
        (root / "render" / f"audio-evidence-{sha}.json").write_text(json.dumps(audio), encoding="utf-8")
        review.update(video_sha256=sha, segments=[{"segment_index": 0, "frame_index": 0, "pts_sec": 0,
                      "motion_continuous": True, "observation": "Actual fixture review of complete segment."}],
                      boundaries=[], subtitle_alignment=[{"event_index": 0, "word_indices": [0],
                      "observed_sync": True, "observation": "Actual fixture full subtitle/word alignment."}])
        full_review = deepcopy(review)
        review["pages"][1].update(observed=False, observation="Not observed in this review.")
        frozen = deepcopy(old.input_data)
        if getattr(request, "param", None) == "policy2":
            frozen["audio_review_policy_version"] = 2
        frozen.update(visual_evidence_version=1, visual_evidence=evidence, audio_evidence=audio,
                      context_files=[evidence["manifest_path"], *[
                          (pack_root / item["path"]).relative_to(root).as_posix()
                          for item in [*evidence["manifest"]["pages"], *evidence["manifest"]["frames"],
                                       evidence["manifest"]["subtitles"]]]])
        refs = {**frozen["upstream_task_ids"], "video_artifact_id": frozen["video_artifact_id"]}
        frozen["acceptance_subject"] = acceptance_subject(session, content_id, refs, source_context_version=1,
                                                         audio_evidence=audio, visual_evidence=evidence)
        pipeline._write_acceptance_receipt(session, session.get(Content, content_id), pipeline.MediaExecutor(root), frozen)
        qc = runtime.workflow_service(session).create_agent_task(content_id, TaskType.CONTENT_QC, frozen,
                                                                  allow_multiple=True, schema_version="3")
        result = deepcopy(old.result_data)
        result.update(subject=frozen["acceptance_subject"], visual_review=review,
                      issues=[{"severity": "error", "field": "visual_review", "message": "Incomplete native page observations."}])
        result["checks"] = {key: {"passed": key in {"facts", "technical"}, "evidence": "Current evidence; incomplete review."}
                            for key in result["checks"]}
        fault = getattr(request, "param", None)
        if fault in {"audio_issue", "physical_visual_issue"}:
            result["issues"].append({"severity": "error", "field": "audio" if fault == "audio_issue" else "visual_narration",
                                     "message": "An independently observed physical defect requires media repair."})
        elif fault in {"all_observed", "no_observed"}:
            for row in review["pages"]:
                row["observed"] = fault == "all_observed"
        elif fault == "page_sha_mismatch":
            review["pages"][0]["sha256"] = "f" * 64
        elif fault == "realtime":
            review["realtime_motion_required"] = True
        elif fault == "technical_failed":
            result["checks"]["technical"]["passed"] = False
        accept(session, runtime, qc, result, old.assigned_agent_id)
        qc_id = qc.id
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[-1]
    assert recovery.input_data["lineage"]["round"] == 2
    assert recovery.input_data["lineage"]["target_task_type"] == "short_script"
    return runtime, http, content_id, qc_id, plan_id, root, recovery, full_review


def reinspection_plan(qc_id):
    return {"failure_class": "BUSINESS_QC", "action": "create_repair_task", "target_task_type": "content_qc",
            "reason": "Complete the same subject's missing native page observations in a new full review.",
            "must_change": ["New reviewer must actually inspect every page, every segment and all subtitle alignments."],
            "must_preserve": ["Exact final, manifest, audio, upstream lineage and original negative result."],
            "must_not_do": ["Reuse prior reviewer observations as this reviewer's own, rebuild media or reset budgets."],
            "evidence_refs": [qc_id], "acceptance_checks": COMMITMENTS}


def submit_plan(case, plan=None):
    runtime, http, _, qc_id, _, _, recovery, _ = case
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assert claim["task_id"] == recovery.id
    assistant.confirm_claim(recovery.id, claim["lease_token"], assistant.settings.agent_id)
    return assistant.complete(recovery.id, claim["lease_token"], assistant.settings.agent_id,
                              plan or reinspection_plan(qc_id))


@pytest.mark.parametrize("incomplete_qc", [None, "policy2"], indirect=True)
def test_pending_legacy_recovery_creates_one_same_subject_review_without_rewriting_history(incomplete_qc):
    runtime, _, content_id, qc_id, _, root, recovery, _ = incomplete_qc
    with Session(runtime.engine) as session:
        original = session.get(AgentTask, qc_id).model_dump(mode="json")
        original_recovery = deepcopy(recovery.input_data)
        content = session.get(Content, content_id)
        version = content.script_version
    old_files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert submit_plan(incomplete_qc)["advanced"] == "CONSUMED"
    assert runtime.consume_agent(recovery.id) is False
    with Session(runtime.engine) as session:
        children = session.exec(select(AgentTask).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery.id)).all()
        assert len(children) == 1 and children[0].task_type == TaskType.CONTENT_QC
        child = children[0]
        assert child.schema_version == "3" and child.status == TaskStatus.PENDING and child.attempt == 0
        assert child.input_data["acceptance_subject"] == original["input_data"]["acceptance_subject"]
        assert child.input_data.get("audio_review_policy_version") == original["input_data"].get("audio_review_policy_version")
        for key in ("visual_evidence", "audio_evidence", "upstream_task_ids", "video_artifact_id"):
            assert child.input_data[key] == original["input_data"][key]
        assert child.input_data["lineage"] == original_recovery["lineage"]
        assert "visual_review" not in child.input_data
        assert child.input_data["acceptance_receipt_artifact_id"] != original["input_data"]["acceptance_receipt_artifact_id"]
        assert session.get(AgentTask, qc_id).model_dump(mode="json") == original
        assert session.get(AgentTask, recovery.id).input_data == original_recovery
        assert session.get(Content, content_id).script_version == version
        assert session.get(Content, content_id).current_stage == "content_qc"
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == child.id, AuditEvent.action == OWNED)).one()
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id, AuditEvent.action == AGENT_CONSUMED)).one()
    assert all(path.read_bytes() == value for path, value in old_files.items())


def accept_plan(case, plan=None):
    runtime, http, _, qc_id, _, _, recovery, _ = case
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id), plan or reinspection_plan(qc_id), assistant.settings.agent_id)


def children_for(runtime, recovery_id):
    with Session(runtime.engine) as session:
        return session.exec(select(AgentTask).where(AgentTask.input_data["recovery_task_id"].as_string() == recovery_id)).all()


@pytest.mark.parametrize("incomplete_qc", ["audio_issue", "physical_visual_issue", "all_observed", "no_observed",
                                            "page_sha_mismatch", "realtime", "technical_failed"], indirect=True)
def test_only_structural_missing_page_case_can_request_reinspection(incomplete_qc):
    runtime, _, _, _, _, _, recovery, _ = incomplete_qc
    accept_plan(incomplete_qc)
    with pytest.raises(TaskConflictError, match="REINSPECTION_NOT_ALLOWED"):
        runtime.consume_agent(recovery.id)
    assert children_for(runtime, recovery.id) == []


@pytest.mark.parametrize("damage", ["source_result", "source_input", "manifest", "page", "frame", "video", "audio",
                                     "script_input", "script_result", "plan_input", "claim", "offer", "pending",
                                     "publish_intent", "failed_publish_intent", "budget", "persisted_budget", "persisted_disabled",
                                     "origin", "source_raw", "source_consumed"])
def test_reinspection_revalidates_current_evidence_and_side_effect_boundaries(incomplete_qc, damage):
    runtime, _, content_id, qc_id, plan_id, root, recovery, _ = incomplete_qc
    accept_plan(incomplete_qc)
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, qc_id)
        plan = session.get(AgentTask, plan_id)
        script = session.get(AgentTask, plan.input_data["upstream_task_ids"]["script"])
        if damage in {"source_result", "script_result"}:
            task = source if damage == "source_result" else script
            task.result_data = {**task.result_data, "changed": True}
            session.add(task)
        elif damage in {"source_input", "script_input", "plan_input"}:
            task = {"source_input": source, "script_input": script, "plan_input": plan}[damage]
            task.input_data = {**task.input_data, "changed": True}
            session.add(task)
        elif damage in {"manifest", "page", "frame", "video", "audio", "source_raw"}:
            evidence = source.input_data["visual_evidence"]
            pack_root = (root / evidence["manifest_path"]).parent
            path = {"manifest": root / evidence["manifest_path"],
                    "page": pack_root / evidence["manifest"]["pages"][0]["path"],
                    "frame": pack_root / evidence["manifest"]["frames"][0]["path"],
                    "video": root / "final.mp4",
                    "audio": root / "render" / f"audio-evidence-{source.input_data['audio_evidence']['video_sha256']}.json",
                    "source_raw": runtime.settings.agent_submission_dir / source.id / "1" / "result.json"}[damage]
            path.write_bytes(b"changed")
        elif damage in {"claim", "pending"}:
            other = runtime.workflow_service(session).create_agent_task(content_id, TaskType.PUBLISH_PREPARE, {}, allow_multiple=True)
            other.status = TaskStatus.LEASED if damage == "claim" else TaskStatus.PENDING
            session.add(other)
        elif damage == "offer":
            session.add(SchedulerOffer(request_id="other-offer", agent_id="offered", role_id="script", task_id=qc_id,
                                       expires_at=utc_now() + timedelta(minutes=1)))
        elif damage in {"publish_intent", "failed_publish_intent"}:
            record_intent(session, content_id, "FAILED" if damage == "failed_publish_intent" else "UNKNOWN")
        elif damage == "budget":
            runtime.settings.recovery_max_rounds = 1
        elif damage in {"persisted_budget", "persisted_disabled"}:
            session.add(RuntimeSetting(key="recovery_max_rounds" if damage == "persisted_budget" else "recovery_enabled",
                                       value="1" if damage == "persisted_budget" else "false"))
        elif damage in {"origin", "source_consumed"}:
            session.delete(session.exec(select(AuditEvent).where(
                AuditEvent.entity_id == (recovery.id if damage == "origin" else source.id),
                AuditEvent.action == ("recovery_task_created" if damage == "origin" else AGENT_CONSUMED),
            )).one())
        session.commit()
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery.id)
    assert children_for(runtime, recovery.id) == []


@pytest.mark.parametrize("damage", ["source_input", "source_raw", "source_consumed", "script_input", "child_input", "child_receipt"])
def test_reinspection_replay_checks_original_native_proofs_and_immutable_receipt(incomplete_qc, damage):
    runtime, _, _, qc_id, plan_id, root, recovery, _ = incomplete_qc
    submit_plan(incomplete_qc)
    child = children_for(runtime, recovery.id)[0]
    with Session(runtime.engine) as session:
        if damage in {"source_input", "script_input", "child_input"}:
            script_id = session.get(AgentTask, plan_id).input_data["upstream_task_ids"]["script"]
            task = session.get(AgentTask, {"source_input": qc_id, "script_input": script_id, "child_input": child.id}[damage])
            task.input_data = {**task.input_data, "changed": True}
            session.add(task)
        elif damage == "source_raw":
            (runtime.settings.agent_submission_dir / qc_id / "1" / "result.json").write_text("{}", encoding="utf-8")
        elif damage == "source_consumed":
            session.delete(session.exec(select(AuditEvent).where(AuditEvent.entity_id == qc_id,
                                                                 AuditEvent.action == AGENT_CONSUMED)).one())
        else:
            receipt = session.get(Artifact, child.input_data["acceptance_receipt_artifact_id"])
            (root / receipt.relative_path).write_text("{}", encoding="utf-8")
        session.commit()
    with pytest.raises((TaskConflictError, ValueError)):
        runtime.consume_agent(recovery.id)
    assert len(children_for(runtime, recovery.id)) == 1


@pytest.mark.parametrize("target", ["short_script", None])
def test_short_script_or_default_plan_is_never_silently_converted(incomplete_qc, target):
    runtime, _, _, qc_id, _, _, recovery, _ = incomplete_qc
    plan = reinspection_plan(qc_id)
    plan["target_task_type"] = target
    submit_plan(incomplete_qc, plan)
    children = children_for(runtime, recovery.id)
    assert len(children) == 1 and children[0].task_type == TaskType.SHORT_SCRIPT


@pytest.mark.parametrize("fault", ["missing_page", *["check_" + key for key in
                                                  ("facts", "rights", "core_demonstration", "visual_narration",
                                                   "subtitles", "audio", "pacing", "identity_and_payoff", "technical")]])
def test_new_review_still_requires_every_page_and_nine_hard_checks(incomplete_qc, fault):
    runtime, _, _, qc_id, _, _, recovery, full_review = incomplete_qc
    submit_plan(incomplete_qc)
    child = children_for(runtime, recovery.id)[0]
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, qc_id)
        result = deepcopy(source.result_data)
        result.update(passed=True, confidence="high", issues=[], visual_review=deepcopy(full_review))
        for check in result["checks"].values():
            check.update(passed=True, evidence="Actual new complete independent review fixture.")
        if fault == "missing_page":
            result["visual_review"]["pages"].pop()
        else:
            result["checks"][fault.removeprefix("check_")]["passed"] = False
        with pytest.raises(ValueError):
            verify_acceptance(session, session.get(AgentTask, child.id), result)


def test_reinspection_live_contract_explains_legacy_target_and_full_own_review(incomplete_qc):
    runtime, http, _, _, _, _, recovery, _ = incomplete_qc
    assistant = registered_client(http, runtime, "assistant")
    claim = assistant.claim()
    assistant.confirm_claim(recovery.id, claim["lease_token"], assistant.settings.agent_id)
    bundle = assistant.bundle(claim["bundle_url"], claim["lease_token"])
    for text in (*COMMITMENTS, "target_task_type=content_qc", "历史冻结 lineage.target_task_type=short_script"):
        assert text in bundle["instructions"]
    with Session(runtime.engine) as session:
        assert session.get(AgentTask, recovery.id).input_data == recovery.input_data


def complete_result(source, full_review):
    result = deepcopy(source.result_data)
    result.update(passed=True, confidence="high", issues=[], visual_review=deepcopy(full_review))
    for check in result["checks"].values():
        check.update(passed=True, evidence="Actual new complete independent review fixture.")
    return result


def test_new_full_review_positive_control_is_accepted_and_old_recovery_replays_read_only(incomplete_qc):
    runtime, _, content_id, qc_id, _, _, recovery, full_review = incomplete_qc
    submit_plan(incomplete_qc)
    child = children_for(runtime, recovery.id)[0]
    with Session(runtime.engine) as session:
        result = complete_result(session.get(AgentTask, qc_id), full_review)
        verify_acceptance(session, session.get(AgentTask, child.id), result)
        accept(session, runtime, session.get(AgentTask, child.id), result,
               session.get(AgentTask, qc_id).assigned_agent_id)
    assert runtime.consume_agent(child.id)
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).current_stage == "final_review"
        record_intent(session, content_id)
    assert runtime.consume_agent(recovery.id) is False
    assert len(children_for(runtime, recovery.id)) == 1


def test_new_negative_qc_keeps_original_shared_budget_and_root(incomplete_qc):
    runtime, _, _, qc_id, _, _, recovery, _ = incomplete_qc
    submit_plan(incomplete_qc)
    child = children_for(runtime, recovery.id)[0]
    with Session(runtime.engine) as session:
        source = session.get(AgentTask, qc_id)
        accept(session, runtime, session.get(AgentTask, child.id), deepcopy(source.result_data), source.assigned_agent_id)
    assert runtime.consume_agent(child.id)
    next_recovery = recovery_rows(runtime)[-1]
    assert next_recovery.input_data["lineage"]["round"] == 3
    for key in ("root_task_id", "root_cause_signature", "max_rounds"):
        assert next_recovery.input_data["lineage"][key] == recovery.input_data["lineage"][key]
    assert runtime.consume_agent(recovery.id) is False


def test_concurrent_consumers_create_only_one_new_review_and_receipt(incomplete_qc):
    runtime, _, _, _, _, _, recovery, _ = incomplete_qc
    accept_plan(incomplete_qc)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(runtime.consume_agent, [recovery.id, recovery.id])) == [False, True]
    assert len(children_for(runtime, recovery.id)) == 1
    with Session(runtime.engine) as session:
        assert len(session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
                                                         AuditEvent.action == QC_REINSPECTION)).all()) == 1


def test_reinspection_replay_does_not_duplicate_a_leased_child(incomplete_qc):
    runtime, _, _, _, _, _, recovery, _ = incomplete_qc
    submit_plan(incomplete_qc)
    child = children_for(runtime, recovery.id)[0]
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, child.id)
        task.status = TaskStatus.LEASED
        session.add(task)
        session.commit()
    assert runtime.consume_agent(recovery.id) is False
    assert len(children_for(runtime, recovery.id)) == 1


def test_commit_failure_does_not_partially_advance_recovery(incomplete_qc, monkeypatch):
    runtime, _, content_id, _, _, _, recovery, _ = incomplete_qc
    accept_plan(incomplete_qc)
    with Session(runtime.engine) as session:
        before = session.get(Content, content_id).model_dump(mode="json")

    def fail_commit(session):
        raise RuntimeError("injected final transaction commit failure")

    monkeypatch.setattr(Session, "commit", fail_commit)
    with pytest.raises(RuntimeError, match="injected final"):
        runtime.consume_agent(recovery.id)
    assert children_for(runtime, recovery.id) == []
    with Session(runtime.engine) as session:
        assert session.get(Content, content_id).model_dump(mode="json") == before
        assert session.exec(select(AuditEvent).where(AuditEvent.entity_id == recovery.id,
                                                      AuditEvent.action.in_([QC_REINSPECTION, AGENT_CONSUMED]))).first() is None


def test_old_v2_negative_result_cannot_gain_joint_reinspection(failed_qc):
    runtime, http, _, qc_id, _, _ = failed_qc
    assert runtime.consume_agent(qc_id)
    recovery = recovery_rows(runtime)[0]
    assistant = registered_client(http, runtime, "assistant")
    with Session(runtime.engine) as session:
        accept(session, runtime, session.get(AgentTask, recovery.id), reinspection_plan(qc_id), assistant.settings.agent_id)
    with pytest.raises(TaskConflictError):
        runtime.consume_agent(recovery.id)
    assert children_for(runtime, recovery.id) == []
