"""HTTP cancellation is atomic and leaves all previous production evidence intact."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path

import pytest
from glance_core.content_acceptance import acceptance_subject
from glance_core.models import (
    AgentSubmission,
    AgentTask,
    Artifact,
    AuditEvent,
    Content,
    ContentStatus,
    PublicationMode,
    PublicationRecord,
    SchedulerOffer,
    StageRun,
    StageStatus,
    TaskStatus,
    TaskType,
    utc_now,
)
from glance_core.task_service import stable_hash
from sqlmodel import Session, select

from app.business import media_executor, pipeline
from app.business.conductor_launch import LaunchRecord, _save
from app.business.content_run import ContentRun, load_run, save_run
from app.business.pending_render_revision import REQUESTED
from test.business.test_discovery_http import ADMIN
from test.business.test_kokoro_voice_rebuild import (
    contract as contract,
    prove_completed,
    sha,
)


@pytest.fixture
def pending(contract, monkeypatch):
    runtime, http, ids, _, _, _ = contract
    cid = ids["content"]
    assert (
        http.post(f"/api/v1/contents/{cid}/rebuild-voice", headers=ADMIN).status_code
        == 200
    )
    for kind in (TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC):
        with Session(runtime.engine) as session:
            task = session.exec(
                select(AgentTask).where(
                    AgentTask.content_id == cid,
                    AgentTask.task_type == kind,
                    AgentTask.status == TaskStatus.PENDING,
                )
            ).one()
            task.status, task.completed_at = TaskStatus.COMPLETED, utc_now()
            task.result_data = (
                deepcopy(task.input_data["previous_plan"])
                if kind == TaskType.VISUAL_PLAN
                else {"passed": True}
            )
            prove_completed(session, runtime, task)
            task_id = task.id
        pipeline.advance_after_agent(runtime.engine, runtime.settings, task_id)
    with Session(runtime.engine) as session:
        content = session.get(Content, cid)
        root = Path(content.content_dir)
        directory = root / "render/revisions/current"
        directory.mkdir(parents=True)
        final, preview = directory / "final.mp4", directory / "preview.mp4"
        final.write_bytes(b"immutable current defective video")
        preview.write_bytes(b"immutable current preview")
        (directory / "outputs.json").write_text(
            json.dumps({p.name: sha(p.read_bytes()) for p in (final, preview)})
        )
        artifact = Artifact(
            content_id=cid,
            artifact_type="final_video",
            relative_path=final.relative_to(root).as_posix(),
            sha256=sha(final.read_bytes()),
            size_bytes=final.stat().st_size,
            mime_type="video/mp4",
            producer_stage="render",
        )
        session.add(artifact)
        session.commit()
        flow = runtime.workflow_service(session)
        script, plan = (
            flow.completed_task_for(cid, kind)
            for kind in (TaskType.SHORT_SCRIPT, TaskType.VISUAL_PLAN)
        )
        refs = {"script": script.id, "visual_plan": plan.id}
        audio = {"video_sha256": artifact.sha256}
        visual = {"manifest": {"input_video": {"expected_sha256": artifact.sha256}}}
        data = {
            "upstream_task_ids": refs,
            "video_artifact_id": artifact.id,
            "acceptance_contract_version": 2,
            "source_context_version": 1,
            "audio_evidence_version": 1,
            "visual_evidence_version": 1,
            "audio_evidence": audio,
            "visual_evidence": visual,
            "acceptance_subject": acceptance_subject(
                session,
                cid,
                {**refs, "video_artifact_id": artifact.id},
                source_context_version=1,
                audio_evidence=audio,
                visual_evidence=visual,
            ),
        }
        pipeline._write_acceptance_receipt(
            session, content, media_executor.MptMediaExecutor(root), data
        )
        qc = flow.create_agent_task(
            cid, TaskType.CONTENT_QC, data, allow_multiple=True, schema_version="3"
        )
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="mpt-business",
                entity_type="agent_task",
                entity_id=qc.id,
                action="mpt_task_owned",
                metadata_json={"input_hash": qc.input_hash},
            )
        )
        content.business_status, content.current_stage = (
            ContentStatus.WAITING_AGENT,
            "content_qc",
        )
        session.add(content)
        now = utc_now()
        process = {
            "pid": 1234,
            "started_at": (now - timedelta(hours=1)).isoformat(),
            "instance_id": "ended-host",
            "binary_path": "C:/fixture/host.exe",
            "binary_sha256": "b" * 64,
            "version": "fixture",
        }
        run = ContentRun(
            run_id="run_" + "a" * 32,
            content_id=cid,
            generation=7,
            state="stopped",
            stop_requested=True,
            created_at=(now - timedelta(minutes=10)).isoformat(),
            expires_at=(now + timedelta(minutes=60)).isoformat(),
            host_epoch=1,
            parent_thread_id="ended-parent",
            process_identity=process,
            host_observed_at=(now - timedelta(minutes=5)).isoformat(),
            intents={
                "ended": {
                    "state": "TERMINAL",
                    "host_epoch": 1,
                    "offer_hash": "proof",
                    "child": None,
                    "claim_id": None,
                }
            },
        )
        save_run(session, run, "mpt_content_run_drained")
        execution = LaunchRecord(
            launch_id="ended",
            content_id=cid,
            run_id=run.run_id,
            generation=run.generation,
            state="FINISHED",
            created_at=run.created_at,
            exited_at=now.isoformat(),
            exit_code=0,
        )
        _save(session, execution)
        raw = json.dumps(
            {
                "revision_id": "current",
                "renderer_version": "previous",
                "voice_sha256": plan.input_data["voice_artifact"]["sha256"],
            }
        ).encode()
        (root / "render/mpt-render-request.json").write_bytes(raw)
        expected = {
            "version": 1,
            "run": {
                field: getattr(run, field)
                for field in (
                    "run_id",
                    "generation",
                    "host_epoch",
                    "parent_thread_id",
                    "process_identity",
                )
            },
            "execution_sha256": stable_hash(execution.model_dump()),
            "qc": {"task_id": qc.id, "input_hash": qc.input_hash, "attempt": 0},
            "final_artifact_id": artifact.id,
            "final_sha256": artifact.sha256,
            "render_request_sha256": sha(raw),
            "previous_renderer_version": "previous",
            "renderer_version": "changed",
        }
        session.commit()
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", "changed")
    return runtime, http, cid, expected


def snapshot(runtime):
    with Session(runtime.engine) as session:
        tables = {
            kind.__name__: {
                row.id: row.model_dump(mode="json")
                for row in session.exec(select(kind)).all()
            }
            for kind in (AgentTask, Artifact, Content, AuditEvent)
        }
        root = Path(session.exec(select(Content)).one().content_dir)
        files = {
            p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*")
            if p.is_file()
        }
        return tables, files


def post(pending, expected=None):
    _, http, cid, original = pending
    return http.post(
        f"/api/v1/contents/{cid}/request-rerender",
        json=expected or original,
        headers=ADMIN,
    )


def test_exact_cancellation_preserves_all_history_and_replays_after_later_render(
    pending,
):
    runtime, _, cid, expected = pending
    before, files = snapshot(runtime)
    response = post(pending)
    assert response.status_code == 200, response.text
    after, current_files = snapshot(runtime)
    assert current_files == files
    assert before["Artifact"] == after["Artifact"]
    qc_id = expected["qc"]["task_id"]
    for task_id, old in before["AgentTask"].items():
        if task_id != qc_id:
            assert after["AgentTask"][task_id] == old
    old, cancelled = before["AgentTask"][qc_id], after["AgentTask"][qc_id]
    assert {k: v for k, v in old.items() if k not in {"status", "dedup_key"}} == {
        k: v for k, v in cancelled.items() if k not in {"status", "dedup_key"}
    }
    assert cancelled["status"] == "CANCELLED" and cancelled["dedup_key"] is None
    assert after["Content"][cid]["business_status"] == "PRODUCING"
    assert after["Content"][cid]["current_stage"] == "render_ready"
    assert len(after["AuditEvent"]) == len(before["AuditEvent"]) + 2
    assert all(
        after["AuditEvent"][key] == value for key, value in before["AuditEvent"].items()
    )
    # Normal rendering replaces the global request and advances content independently.
    with Session(runtime.engine) as session:
        content = session.get(Content, cid)
        (Path(content.content_dir) / "render/mpt-render-request.json").write_bytes(
            b"later revision"
        )
        content.business_status, content.current_stage = (
            ContentStatus.WAITING_AGENT,
            "content_qc",
        )
        session.add(content)
        session.commit()
    frozen = snapshot(runtime)
    assert post(pending).json() == {**response.json(), "replayed": True}
    assert snapshot(runtime) == frozen
    changed = deepcopy(expected)
    changed["renderer_version"] = "another"
    assert post(pending, changed).status_code == 409


def test_concurrent_requests_have_one_atomic_cancellation(pending):
    runtime, _, _, _ = pending
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: post(pending), range(2)))
    assert [r.status_code for r in responses] == [200, 200]
    assert sorted(r.json()["replayed"] for r in responses) == [False, True]
    with Session(runtime.engine) as session:
        assert (
            len(
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == REQUESTED)
                ).all()
            )
            == 1
        )


@pytest.mark.parametrize("expired", [True, False])
def test_other_historical_task_offer_only_blocks_while_live(pending, expired):
    runtime, _, cid, expected = pending
    with Session(runtime.engine) as session:
        old_task = session.exec(select(AgentTask).where(
            AgentTask.content_id == cid, AgentTask.task_type == TaskType.RECOVERY,
            AgentTask.status == TaskStatus.COMPLETED,
        )).one()
        offer = SchedulerOffer(request_id="historical-other-task", task_id=old_task.id,
            expires_at=utc_now() + timedelta(minutes=-60 if expired else 60))
        session.add(offer)
        session.commit()
        session.refresh(offer)
        offer_before = offer.model_dump(mode="json")
    before, files = snapshot(runtime)
    response = post(pending)
    assert response.status_code == (200 if expired else 409), response.text
    after, current_files = snapshot(runtime)
    assert current_files == files and before["Artifact"] == after["Artifact"]
    assert all(after["AgentTask"][key] == value for key, value in before["AgentTask"].items()
               if key != expected["qc"]["task_id"])
    if not expired:
        assert before == after
    with Session(runtime.engine) as session:
        assert session.get(SchedulerOffer, "historical-other-task").model_dump(mode="json") == offer_before


def test_cancel_commit_is_rolled_back_with_final_audit_failure(pending, monkeypatch):
    runtime, _, _, _ = pending
    before = snapshot(runtime)
    actual = Session.add

    def fail(self, instance, *args, **kwargs):
        if isinstance(instance, AuditEvent) and instance.action == REQUESTED:
            raise RuntimeError("audit insert interrupted")
        return actual(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", fail)
    with pytest.raises(RuntimeError, match="audit insert interrupted"):
        post(pending)
    assert snapshot(runtime) == before


@pytest.mark.parametrize(
    "blocker",
    [
        "hash",
        "run",
        "host",
        "version",
        "same-version",
        "receipt",
        "final",
        "offered",
        "claimed",
        "submission",
        "completed",
        "failed",
        "leased",
        "other-task",
        "stage",
        "publication",
        "intent",
        "new-qc",
        "voice",
        "live-host",
        "unresolved-child",
        "expired-offer",
        "old-final",
        "bytes",
        "completed-plan-replaced",
        "creation",
    ],
)
def test_refuses_changed_or_previously_started_subject_without_mutation(
    pending, monkeypatch, blocker
):
    runtime, _, cid, expected = pending
    expected = deepcopy(expected)
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, expected["qc"]["task_id"])
        if blocker == "hash":
            expected["qc"]["input_hash"] = "f" * 64
        elif blocker == "run":
            expected["run"]["generation"] += 1
        elif blocker == "host":
            expected["run"]["process_identity"]["pid"] += 1
        elif blocker == "version":
            expected["renderer_version"] = "unknown"
        elif blocker == "same-version":
            expected["renderer_version"] = "previous"
            monkeypatch.setattr(media_executor, "RENDERER_VERSION", "previous")
        elif blocker == "receipt":
            expected["render_request_sha256"] = "f" * 64
        elif blocker == "final":
            expected["final_sha256"] = "f" * 64
        elif blocker == "offered":
            session.add(
                AuditEvent(
                    actor_type="scheduler",
                    actor_id="scheduler",
                    entity_type="scheduler_offer",
                    entity_id="expired",
                    action="mpt_content_run_offer",
                    metadata_json={"worker": {"task_id": task.id}},
                )
            )
        elif blocker == "claimed":
            session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id="past",
                    entity_type="agent_task",
                    entity_id=task.id,
                    action="claimed",
                )
            )
        elif blocker == "submission":
            session.add(
                AgentSubmission(
                    task_id=task.id,
                    agent_id="past",
                    attempt=0,
                    raw_result_path="missing",
                    result_hash="old",
                    validation_status="REJECTED",
                )
            )
        elif blocker in {"completed", "failed", "leased"}:
            task.status = TaskStatus(blocker.upper())
            session.add(task)
        elif blocker in {"other-task", "new-qc"}:
            session.add(
                AgentTask(
                    content_id=cid,
                    task_type=TaskType.CONTENT_QC
                    if blocker == "new-qc"
                    else TaskType.RECOVERY,
                    input_hash="other",
                    prompt_name="other",
                    instructions="other",
                )
            )
        elif blocker == "stage":
            session.add(
                StageRun(
                    content_id=cid,
                    stage="render",
                    status=StageStatus.RUNNING,
                    input_hash="running",
                )
            )
        elif blocker == "publication":
            session.add(
                PublicationRecord(
                    content_id=cid, platform="douyin", mode=PublicationMode.EXECUTOR
                )
            )
        elif blocker == "intent":
            session.add(
                AuditEvent(
                    actor_type="agent",
                    actor_id="worker",
                    entity_type="publish_intent",
                    entity_id="intent",
                    action="intent",
                    metadata_json={"content_id": cid},
                )
            )
        elif blocker == "voice":
            plan = runtime.workflow_service(session).completed_task_for(
                cid, TaskType.VISUAL_PLAN
            )
            voice = session.get(Artifact, plan.input_data["voice_artifact"]["id"])
            (
                Path(session.get(Content, cid).content_dir) / voice.relative_path
            ).write_bytes(b"changed speech")
        elif blocker in {"live-host", "unresolved-child"}:
            run = load_run(session)
            if blocker == "live-host":
                run.host_observed_at = utc_now().isoformat()
            else:
                run.intents = {"unfinished": {"state": "CLAIMED"}}
            save_run(session, run, "fixture-change")
        elif blocker == "expired-offer":
            session.add(SchedulerOffer(request_id="expired", task_id=task.id,
                expires_at=utc_now() - timedelta(minutes=1)))
        elif blocker == "old-final":
            old = session.exec(select(Artifact).where(Artifact.content_id == cid,
                Artifact.artifact_type == "final_video", Artifact.id != expected["final_artifact_id"])).one()
            expected["final_artifact_id"], expected["final_sha256"] = old.id, old.sha256
        elif blocker == "bytes":
            artifact = session.get(Artifact, expected["final_artifact_id"])
            (Path(session.get(Content, cid).content_dir) / artifact.relative_path).write_bytes(b"tampered")
        elif blocker == "completed-plan-replaced":
            session.add(AgentTask(content_id=cid, task_type=TaskType.VISUAL_PLAN, status=TaskStatus.FAILED,
                input_hash="new failed plan", prompt_name="plan", instructions="plan"))
        elif blocker == "creation":
            event = session.exec(select(AuditEvent).where(AuditEvent.entity_id == task.id,
                AuditEvent.action == "created")).one()
            event.actor_id = "changed"
            session.add(event)
        session.commit()
    before = snapshot(runtime)
    response = post(pending, expected)
    assert response.status_code == 409, response.text
    assert snapshot(runtime) == before


def test_request_is_admin_only_and_strict(pending):
    _, http, cid, expected = pending
    endpoint = f"/api/v1/contents/{cid}/request-rerender"
    assert http.post(endpoint, json=expected).status_code == 401
    for invalid in (
        {**expected, "extra": True},
        {**expected, "renderer_version": 123},
        {**expected, "qc": {**expected["qc"], "attempt": 1}},
        {**expected, "qc": {**expected["qc"], "attempt": False}},
        {**expected, "version": True},
    ):
        assert http.post(endpoint, json=invalid, headers=ADMIN).status_code == 422


def test_replay_refuses_rewritten_receipt_even_when_artifact_metadata_is_rehashed(
    pending,
):
    runtime, _, cid, expected = pending
    assert post(pending).status_code == 200
    with Session(runtime.engine) as session:
        task = session.get(AgentTask, expected["qc"]["task_id"])
        receipt = session.get(
            Artifact, task.input_data["acceptance_receipt_artifact_id"]
        )
        raw = b"{}"
        (
            Path(session.get(Content, cid).content_dir) / receipt.relative_path
        ).write_bytes(raw)
        receipt.sha256, receipt.size_bytes = sha(raw), len(raw)
        session.add(receipt)
        session.commit()
    before = snapshot(runtime)
    assert post(pending).status_code == 409
    assert snapshot(runtime) == before
