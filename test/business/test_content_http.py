import hashlib
import asyncio
import json
import os
import subprocess
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings
from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Content,
    ContentStatus,
    StageRun,
    StageStatus,
    ProductionMode,
    RightsStatus,
    TaskStatus,
    TaskType,
)
from glance_core.security import issue_role_token
from glance_core.task_service import stable_hash
from PIL import Image
from sqlmodel import Session, select

from app.business.consumption import content_operation_lock
from app.business.runtime import AGENT_CONSUMED
from app.controllers.v1.content import router as content_router
from test.business.test_discovery_http import ADMIN, make_app, make_runtime

RESEARCH = {
    "subject": "Project A",
    "primary_sources": [
        {
            "url": "https://example.org/project-a",
            "title": "Official Project A",
            "source_type": "official",
        }
    ],
    "secondary_sources": [],
    "verified_claims": [
        {
            "id": "claim-1",
            "claim": "A research test project",
            "source_urls": ["https://example.org/project-a"],
            "confidence": "high",
        }
    ],
    "marketing_claims": [],
    "uncertain_claims": [],
    "limitations": [],
    "recommended_angle": "Explain the design",
    "recommended_verdict": "Clear functionality",
    "source_images": [],
}


def content_app(runtime):
    app = make_app(runtime)
    app.include_router(content_router)
    return app


def client_for(http, role):
    client = AgentClient(
        AgentSettings(
            token=issue_role_token("test-root", role),
            agent_id=f"{role}-worker",
            role_id=role,
        )
    )
    client.client.close()
    client.client = http
    return client


def seed_candidate(runtime):
    with Session(runtime.engine) as session:
        candidate, _ = runtime.workflow_service(session).ingest_candidate(
            source_type="agent",
            source_id="content-test-project",
            source_url="https://example.org/project-a",
            original_title="Project A",
            rights_status=RightsStatus.EXPLICIT_LICENSE,
        )
        return candidate.id


def select_and_claim(http, runtime):
    candidate_id = seed_candidate(runtime)
    response = http.post(
        f"/api/v1/candidates/{candidate_id}/select?production_mode=image_only",
        headers=ADMIN,
    )
    assert response.status_code == 200, response.text
    content_id = response.json()["id"]
    researcher = client_for(http, "research")
    claimed = researcher.claim()
    assert claimed["status"] == "CLAIMED"
    bundle = researcher.bundle(claimed["bundle_url"], claimed["lease_token"])
    assert bundle["task"]["task_id"] == claimed["task_id"]
    assert bundle["input"]["production_mode"] == "image_only"
    return content_id, researcher, claimed


def test_real_http_research_advances_once_and_qc_blocks_manual_bypass(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        content_id, researcher, claimed = select_and_claim(http, runtime)
        task_id, token = claimed["task_id"], claimed["lease_token"]
        assert (
            researcher.complete(task_id, token, "research-worker", RESEARCH)["advanced"]
            == "CONSUMED"
        )
        assert (
            researcher.complete(task_id, token, "research-worker", RESEARCH)["advanced"]
            == "CONSUMED"
        )
        script = client_for(http, "script")
        signed_script = script.claim()
        bundle = script.bundle(
            signed_script["bundle_url"], signed_script["lease_token"]
        )
        assert bundle["input"]["research"]["subject"] == "Project A"
        assert bundle["input"]["allowed_claim_ids"] == ["claim-1"]
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AgentTask).where(
                            AgentTask.task_type == TaskType.SHORT_SCRIPT
                        )
                    ).all()
                )
                == 1
            )
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(
                            AuditEvent.entity_id == task_id,
                            AuditEvent.action == AGENT_CONSUMED,
                        )
                    ).all()
                )
                == 1
            )
        assert (
            http.post(
                f"/api/v1/contents/{content_id}/approve-script", headers=ADMIN
            ).status_code
            == 409
        )
        assert (
            http.post(
                f"/api/v1/contents/{content_id}/approve-final", headers=ADMIN
            ).status_code
            == 409
        )
        assert (
            http.post(
                f"/api/v1/contents/{content_id}/publications/douyin/publish",
                headers=ADMIN,
            ).status_code
            == 409
        )
        assert http.get(f"/api/v1/contents/{content_id}").status_code == 401
        assert (
            http.get(f"/api/v1/contents/{content_id}", headers=ADMIN).json()["content"][
                "id"
            ]
            == content_id
        )


def test_advance_before_marker_failure_is_replayed_without_duplicate_tasks(
    tmp_path, monkeypatch
):
    runtime = make_runtime(tmp_path)
    original = Session.commit

    def fail_marker_once(session):
        if any(
            isinstance(row, AuditEvent) and row.action == AGENT_CONSUMED
            for row in session.new
        ):
            raise RuntimeError("Crash after advance but before consumption marker")
        return original(session)

    with TestClient(content_app(runtime)) as http:
        content_id, researcher, claimed = select_and_claim(http, runtime)
        monkeypatch.setattr(Session, "commit", fail_marker_once)
        assert (
            researcher.complete(
                claimed["task_id"], claimed["lease_token"], "research-worker", RESEARCH
            )["advanced"]
            == "PENDING"
        )
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AgentTask).where(
                            AgentTask.task_type == TaskType.SHORT_SCRIPT
                        )
                    ).all()
                )
                == 1
            )
            assert (
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == AGENT_CONSUMED)
                ).all()
                == []
            )
    monkeypatch.setattr(Session, "commit", original)
    restarted = make_runtime(tmp_path)
    with TestClient(content_app(restarted)):
        with Session(restarted.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AgentTask).where(
                            AgentTask.task_type == TaskType.SHORT_SCRIPT
                        )
                    ).all()
                )
                == 1
            )
            assert (
                len(
                    session.exec(
                        select(AuditEvent).where(AuditEvent.action == AGENT_CONSUMED)
                    ).all()
                )
                == 1
            )
            assert session.get(Content, content_id).current_stage == "short_script"


def test_content_lock_serializes_runtimes_and_releases_after_process_crash(tmp_path):
    first, second = make_runtime(tmp_path), make_runtime(tmp_path)
    entered, release, second_entered = Event(), Event(), Event()

    def work(settings, key, blocker):
        with content_operation_lock(settings, key):
            if blocker:
                entered.set()
                assert release.wait(5)
            else:
                second_entered.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_call = pool.submit(work, first.settings, "content/shared", True)
            assert entered.wait(5)
            second_call = pool.submit(work, second.settings, "content/shared", False)
            assert not second_entered.wait(0.2)
            release.set()
            first_call.result()
            second_call.result()
            assert second_entered.is_set()
        code = (
            "import os;from pathlib import Path;from types import SimpleNamespace;"
            "from app.business.consumption import content_operation_lock;"
            f"guard=content_operation_lock(SimpleNamespace(data_dir=Path({str(tmp_path)!r})), 'crash-content');"
            "guard.__enter__();os._exit(7)"
        )
        crashed = subprocess.run(
            [sys.executable, "-c", code],
            env=os.environ.copy(),
            capture_output=True,
            timeout=10,
        )
        assert crashed.returncode == 7, crashed.stderr.decode()
        with content_operation_lock(first.settings, "crash-content"):
            pass
        assert all(
            len(path.stem) == 64 for path in (tmp_path / "consumption-locks").iterdir()
        )
    finally:
        first.close()
        second.close()


def test_real_http_advance_and_admin_action_serialize_on_same_content(
    tmp_path, monkeypatch
):
    from app.business import pipeline

    runtime = make_runtime(tmp_path)
    advanced, release, admin_started = Event(), Event(), Event()
    original = pipeline.advance_after_agent

    def finish_stage_before_pause(engine, settings, task_id):
        result = original(engine, settings, task_id)
        advanced.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(pipeline, "advance_after_agent", finish_stage_before_pause)
    with TestClient(content_app(runtime)) as http:
        content_id, researcher, claimed = select_and_claim(http, runtime)

        def prepare_again():
            admin_started.set()
            return http.post(f"/api/v1/contents/{content_id}/prepare", headers=ADMIN)

        with ThreadPoolExecutor(max_workers=2) as pool:
            complete = pool.submit(
                researcher.complete,
                claimed["task_id"],
                claimed["lease_token"],
                "research-worker",
                RESEARCH,
            )
            assert advanced.wait(5)
            admin = pool.submit(prepare_again)
            assert admin_started.wait(5)
            assert not admin.done()
            release.set()
            assert complete.result()["advanced"] == "CONSUMED"
            assert admin.result().status_code == 409
        with Session(runtime.engine) as session:
            tasks = session.exec(
                select(AgentTask).where(AgentTask.content_id == content_id)
            ).all()
            assert (
                len([task for task in tasks if task.task_type == TaskType.SHORT_SCRIPT])
                == 1
            )
            assert (
                len(
                    [
                        task
                        for task in tasks
                        if task.task_type == TaskType.CONTENT_RESEARCH
                    ]
                )
                == 1
            )
            assert session.get(Content, content_id).current_stage == "short_script"


def test_http_artifact_upload_context_hash_and_path_boundaries(tmp_path):
    runtime = make_runtime(tmp_path)
    with TestClient(content_app(runtime)) as http:
        candidate_id = seed_candidate(runtime)
        with Session(runtime.engine) as session:
            content = runtime.workflow_service(session).select_candidate(
                candidate_id, production_mode=ProductionMode.IMAGE_ONLY
            )
            task = runtime.task_service(session).create_task(
                task_type=TaskType.ASSET_GENERATE_IMAGE,
                content_id=content.id,
                instructions="Generate a test image",
                input_data={"requests": [], "context_files": []},
            )
            content_id, task_id = content.id, task.id
        image_agent = client_for(http, "image")
        claimed = image_agent.claim()
        assert claimed["task_id"] == task_id
        token = claimed["lease_token"]
        stream = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(stream, format="PNG")
        body = stream.getvalue()
        first = image_agent.upload_artifact(
            task_id, token, body=body, mime_type="image/png"
        )
        second = image_agent.upload_artifact(
            task_id, token, body=body, mime_type="image/png"
        )
        assert first["artifact_id"] == second["artifact_id"]
        assert first["sha256"] == hashlib.sha256(body).hexdigest()
        assert (
            http.post(
                f"/api/v1/agent/tasks/{task_id}/artifacts",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "image/jpeg",
                },
                content=b"fake",
            ).status_code
            == 422
        )
        download = f"/api/v1/contents/{content_id}/artifacts/{first['artifact_id']}"
        assert http.get(download, headers=ADMIN).content == body
        with Session(runtime.engine) as session:
            artifact = session.get(Artifact, first["artifact_id"])
            content = session.get(Content, content_id)
            path = tmp_path / "content" / content_id / artifact.relative_path
            task = session.get(AgentTask, task_id)
            video_path = tmp_path / "content" / content_id / "source.mp4"
            video_path.write_bytes(b"not-for-this-image-agent")
            task.input_data = {
                **task.input_data,
                "context_files": ["source.mp4", artifact.relative_path],
            }
            task.input_hash = stable_hash(task.input_data)
            signed_hash = task.input_hash
            session.add(task)
            session.commit()
        bundle = image_agent.bundle(claimed["bundle_url"], token)
        assert len(bundle["context_files"]) == 1
        assert (
            bundle["context_manifest"][0]["sha256"] == hashlib.sha256(body).hexdigest()
        )
        assert (
            http.get(
                bundle["context_files"][0]["url"],
                headers={"Authorization": f"Bearer {token}"},
            ).content
            == body
        )
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, task_id).input_hash == signed_hash
            path.write_bytes(b"tampered")
        assert (
            http.get(
                bundle["context_files"][0]["url"],
                headers={"Authorization": f"Bearer {token}"},
            ).status_code
            == 409
        )
        assert http.get(download, headers=ADMIN).status_code == 409
        assert (
            http.post(
                f"/api/v1/agent/tasks/{task_id}/artifacts",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "image/png",
                },
                content=body,
            ).status_code
            == 409
        )
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            content.content_dir = str(tmp_path.parent)
            session.add(content)
            session.commit()
        assert (
            http.get(
                claimed["bundle_url"], headers={"Authorization": f"Bearer {token}"}
            ).status_code
            == 409
        )


def test_restart_does_not_advance_imported_historical_completed_tasks(tmp_path):
    runtime = make_runtime(tmp_path)
    runtime.initialize()
    with Session(runtime.engine) as session:
        candidate_id = seed_candidate(runtime)
        content = runtime.workflow_service(session).select_candidate(
            candidate_id, production_mode=ProductionMode.IMAGE_ONLY
        )
        task = runtime.task_service(session).create_task(
            task_type=TaskType.CONTENT_RESEARCH,
            content_id=content.id,
            instructions="Historical task",
            input_data={"production_mode": "image_only"},
        )
        task.status, task.result_data = TaskStatus.COMPLETED, RESEARCH
        session.add(task)
        session.commit()
    runtime.close()
    restarted = make_runtime(tmp_path)
    with TestClient(content_app(restarted)):
        with Session(restarted.engine) as session:
            assert (
                session.exec(
                    select(AgentTask).where(
                        AgentTask.task_type == TaskType.SHORT_SCRIPT
                    )
                ).all()
                == []
            )
            assert (
                session.exec(
                    select(AuditEvent).where(AuditEvent.action == AGENT_CONSUMED)
                ).all()
                == []
            )


def test_final_review_bundle_serves_signed_video_and_cover_on_new_server(tmp_path):
    runtime = make_runtime(tmp_path)
    app = content_app(runtime)
    with TestClient(app, base_url="http://127.0.0.1:8010") as http:
        candidate_id = seed_candidate(runtime)
        with Session(runtime.engine) as session:
            content = runtime.workflow_service(session).select_candidate(
                candidate_id, production_mode=ProductionMode.IMAGE_ONLY
            )
            root = tmp_path / "content" / content.id
            root.mkdir(parents=True, exist_ok=True)
            artifacts = []
            for name, kind, body, mime in (
                ("final.mp4", "final_video", b"review-video-byte-fixture", "video/mp4"),
                ("cover.png", "cover_image", b"review-cover-byte-fixture", "image/png"),
            ):
                (root / name).write_bytes(body)
                artifact = Artifact(
                    content_id=content.id,
                    relative_path=name,
                    artifact_type=kind,
                    mime_type=mime,
                    sha256=hashlib.sha256(body).hexdigest(),
                    size_bytes=len(body),
                    producer_stage="http-review-fixture",
                )
                session.add(artifact)
                artifacts.append(artifact)
            session.commit()
            video, cover = artifacts
            task = runtime.task_service(session).create_task(
                task_type=TaskType.FINAL_REVIEW_ASSIST,
                content_id=content.id,
                instructions="Review actual video and cover",
                input_data={
                    "video_artifact_id": video.id,
                    "cover_artifact_id": cover.id,
                    "video_url": f"/media/artifacts/{video.id}",
                    "cover_image_url": f"/media/artifacts/{cover.id}",
                    "context_files": [],
                },
            )
            signed_hash, task_id = task.input_hash, task.id
        assistant = client_for(http, "assistant")
        claimed = assistant.claim()
        assert claimed["task_id"] == task_id
        bundle = assistant.bundle(claimed["bundle_url"], claimed["lease_token"])
        for field, name in (
            ("video_url", "final.mp4"),
            ("cover_image_url", "cover.png"),
        ):
            url = bundle["input"][field]
            assert str(http.base_url.join(url)).startswith("http://127.0.0.1:8010/")
            destination = tmp_path / "agent-workspace" / name
            assistant.download_context(url, claimed["lease_token"], destination)
            assert destination.read_bytes() == (root / name).read_bytes()
            assert http.get(url).status_code == 401
        assert len(bundle["context_manifest"]) == 2
        with Session(runtime.engine) as session:
            assert session.get(AgentTask, task_id).input_hash == signed_hash
        (root / "final.mp4").write_bytes(b"changed-final-video")
        assert (
            http.get(
                bundle["input"]["video_url"],
                headers={"Authorization": f"Bearer {claimed['lease_token']}"},
            ).status_code
            == 409
        )


def acceptance_fixture(http, runtime, *, manual_script_approval=False):
    from app.business import pipeline
    from glance_core.content_acceptance import acceptance_subject, source_context
    from glance_media.executor import run_command

    content_id, researcher, claimed = select_and_claim(http, runtime)
    researcher.complete(
        claimed["task_id"], claimed["lease_token"], "research-worker", RESEARCH
    )
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        script = session.exec(
            select(AgentTask).where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.SHORT_SCRIPT,
            )
        ).one()
        script.status = TaskStatus.COMPLETED
        script.result_data = {
            "full_narration": "The written script is not audio evidence."
        }
        session.add(script)
        session.commit()

        def completed(kind, refs, result):
            task = flow.create_agent_task(content_id, kind, {"upstream_task_ids": refs})
            task.status, task.result_data = TaskStatus.COMPLETED, result
            session.add(task)
            session.commit()
            return task

        completed(
            TaskType.SCRIPT_QC,
            {"script": script.id, "research": claimed["task_id"]},
            {"passed": True},
        )
        if manual_script_approval:
            content = session.get(Content, content_id)
            content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
            session.add(content)
            session.commit()
            response = http.post(f"/api/v1/contents/{content_id}/approve-script", headers=ADMIN)
            assert response.status_code == 200, response.text
            session.expire_all()
        plan = completed(
            TaskType.VISUAL_PLAN,
            {"script": script.id},
            {"segments": [{"scene_id": "s1"}]},
        )
        completed(
            TaskType.VISUAL_PLAN_QC,
            {"visual_plan": plan.id, "script": script.id},
            {"passed": True},
        )
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        video = root / "render" / "final.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        run_command(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=64x64:d=1",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-shortest",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(video),
            ],
            cwd=root,
        )
        artifact = Artifact(
            content_id=content_id,
            artifact_type="final_video",
            relative_path="render/final.mp4",
            sha256=hashlib.sha256(video.read_bytes()).hexdigest(),
            size_bytes=video.stat().st_size,
            mime_type="video/mp4",
            producer_stage="render",
        )
        session.add(artifact)
        session.commit()
        refs = {
            "script": script.id,
            "visual_plan": plan.id,
            "video_artifact_id": artifact.id,
        }
        receipt_input = {
            "script": script.result_data,
            "research": RESEARCH,
            "upstream_task_ids": {"script": script.id, "visual_plan": plan.id},
            "video_artifact_id": artifact.id,
            "acceptance_contract_version": 2,
            "acceptance_origin": "manual",
            "source_context_version": 1,
            "source_context": source_context(session, content_id, refs),
            "acceptance_subject": acceptance_subject(
                session, content_id, refs, source_context_version=1
            ),
        }
        receipt = root / "render" / "acceptance-input.json"
        receipt.write_text(json.dumps(receipt_input), encoding="utf-8")
        receipt_artifact = Artifact(
            content_id=content_id,
            artifact_type="acceptance_input",
            relative_path="render/acceptance-input.json",
            mime_type="application/json",
            sha256=hashlib.sha256(receipt.read_bytes()).hexdigest(),
            size_bytes=receipt.stat().st_size,
            producer_stage="render",
        )
        session.add(receipt_artifact)
        content.business_status, content.current_stage = (
            ContentStatus.PRODUCING,
            "render",
        )
        session.add(content)
        session.commit()
        waiting = pipeline._issue_acceptance(
            session, runtime.settings, content, receipt_input
        )
        assert waiting.current_stage == "audio_evidence_waiting"
        return content_id, artifact.id, artifact.sha256, script.id, receipt_artifact.id


def patched_asr(monkeypatch, *, available):
    from glance_media.executor import MediaExecutor

    def recognize(self, model, device, compute, *, video_path):
        assert video_path.is_file()
        return (
            (
                [
                    {
                        "start_sec": 0,
                        "end_sec": 0.9,
                        "text": "Observed independent track.",
                    }
                ],
                "",
            )
            if available
            else ([], "temporary ASR failure")
        )

    monkeypatch.setattr(MediaExecutor, "_local_asr", recognize)


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
def test_manual_acceptance_recovers_audio_after_restart_without_rerender(
    tmp_path, monkeypatch
):
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path)
    runtime.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=False)
    with TestClient(content_app(runtime)) as http:
        content_id, video_id, digest, _, _ = acceptance_fixture(http, runtime)
        assert (
            http.post(f"/api/v1/contents/{content_id}/resume-acceptance").status_code
            == 401
        )
        assert (
            http.post(
                "/api/v1/contents/unknown/resume-acceptance", headers=ADMIN
            ).status_code
            == 404
        )
        with Session(runtime.engine) as session:
            assert (
                session.exec(
                    select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC)
                ).all()
                == []
            )

    restarted = make_runtime(tmp_path)
    restarted.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=True)
    monkeypatch.setattr(
        MptMediaExecutor,
        "render",
        lambda *args, **kwargs: pytest.fail("Resume must not render again"),
    )
    with TestClient(content_app(restarted)) as http:
        response = http.post(
            f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
        )
        assert response.status_code == 200, response.text
        assert response.json()["current_stage"] == "content_qc"
        assert (
            http.post(
                f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
            ).status_code
            == 409
        )
        with Session(restarted.engine) as session:
            qc = session.exec(
                select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC)
            ).one()
            assert qc.schema_version == "2"
            assert qc.input_data["acceptance_origin"] == "manual"
            assert qc.input_data["video_artifact_id"] == video_id
            assert qc.input_data["acceptance_subject"]["video_sha256"] == digest
            evidence = qc.input_data["audio_evidence"]
            assert evidence["video_sha256"] == digest
            assert evidence["asr_status"] == evidence["signal_status"] == "available"
            assert (
                evidence["segments"][0]["text"]
                != qc.input_data["script"]["full_narration"]
            )
            assert len(evidence["signal"]["decoded_pcm_sha256"]) == 64
            assert (
                len(
                    session.exec(
                        select(Artifact).where(Artifact.artifact_type == "final_video")
                    ).all()
                )
                == 1
            )
            assert (
                session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action == "handsfree_candidate_selected"
                    )
                ).all()
                == []
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize(
    "invalid",
    [
        "receipt_bytes",
        "receipt_protocol",
        "stale_video",
        "stale_lineage",
        "missing_owner",
    ],
)
def test_resume_acceptance_closes_invalid_snapshot_without_qc(
    tmp_path, monkeypatch, invalid
):
    patched_asr(monkeypatch, available=False)
    runtime = make_runtime(tmp_path)
    runtime.settings.asr_model = "test-asr"
    with TestClient(content_app(runtime)) as http:
        content_id, video_id, _, script_id, receipt_id = acceptance_fixture(
            http, runtime
        )
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            receipt = Path(content.content_dir) / "render" / "acceptance-input.json"
            if invalid == "receipt_bytes":
                receipt.write_bytes(b"changed")
            elif invalid == "receipt_protocol":
                data = json.loads(receipt.read_text())
                data["acceptance_contract_version"] = 1
                receipt.write_text(json.dumps(data), encoding="utf-8")
                artifact = session.get(Artifact, receipt_id)
                artifact.sha256, artifact.size_bytes = (
                    hashlib.sha256(receipt.read_bytes()).hexdigest(),
                    receipt.stat().st_size,
                )
                session.add(artifact)
            elif invalid == "stale_video":
                artifact = session.get(Artifact, video_id)
                (Path(content.content_dir) / artifact.relative_path).write_bytes(
                    b"changed"
                )
            elif invalid == "stale_lineage":
                script = session.get(AgentTask, script_id)
                script.result_data = {"full_narration": "Changed after rendering"}
                session.add(script)
            else:
                for event in session.exec(
                    select(AuditEvent).where(
                        AuditEvent.action == "manual_candidate_selected"
                    )
                ).all():
                    session.delete(event)
            session.commit()
        patched_asr(monkeypatch, available=True)
        response = http.post(
            f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
        )
        if invalid == "missing_owner":
            assert response.status_code == 409
        else:
            assert response.status_code == 200, response.text
            assert response.json()["business_status"] == "FAILED"
            assert response.json()["current_stage"] == "audio_evidence_closed"
        with Session(runtime.engine) as session:
            assert (
                session.exec(
                    select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC)
                ).all()
                == []
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
def test_concurrent_resume_acceptance_issues_only_one_qc(tmp_path, monkeypatch):
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path)
    runtime.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=False)
    with TestClient(content_app(runtime)) as http:
        content_id, _, _, _, _ = acceptance_fixture(http, runtime)
        patched_asr(monkeypatch, available=True)
        entered, release = Event(), Event()
        original = MptMediaExecutor.final_audio_evidence

        def collect(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            return original(self, *args, **kwargs)

        monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", collect)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(
                http.post,
                f"/api/v1/contents/{content_id}/resume-acceptance",
                headers=ADMIN,
            )
            assert entered.wait(5)
            second = pool.submit(
                http.post,
                f"/api/v1/contents/{content_id}/resume-acceptance",
                headers=ADMIN,
            )
            release.set()
            assert sorted(
                [first.result().status_code, second.result().status_code]
            ) == [200, 409]
        with Session(runtime.engine) as session:
            assert (
                len(
                    session.exec(
                        select(AgentTask).where(
                            AgentTask.task_type == TaskType.CONTENT_QC
                        )
                    ).all()
                )
                == 1
            )


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
@pytest.mark.parametrize("changed_snapshot", [False, True])
def test_crashed_running_audio_phase_recovers_from_verified_receipt(
    tmp_path, monkeypatch, changed_snapshot
):
    from app.business import pipeline
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path)
    runtime.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=False)
    original = MptMediaExecutor.final_audio_evidence

    def crash_after_running_marker(self, *args, **kwargs):
        raise SystemExit(7)

    with TestClient(content_app(runtime)) as http:
        content_id, video_id, digest, _, _ = acceptance_fixture(http, runtime)
        monkeypatch.setattr(
            MptMediaExecutor, "final_audio_evidence", crash_after_running_marker
        )
        with pytest.raises(SystemExit) as crashed:
            runtime.run_content_stage(content_id, pipeline.resume_content_acceptance)
        assert crashed.value.code == 7
        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            assert (content.business_status, content.current_stage) == (
                ContentStatus.PRODUCING,
                "audio_evidence_running",
            )
            assert (
                session.exec(
                    select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC)
                ).all()
                == []
            )
            if changed_snapshot:
                (
                    Path(content.content_dir) / "render" / "acceptance-input.json"
                ).write_bytes(b"changed-after-crash")

    restarted = make_runtime(tmp_path)
    restarted.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=True)
    monkeypatch.setattr(
        MptMediaExecutor,
        "render",
        lambda *args, **kwargs: pytest.fail("Audio recovery must not render"),
    )
    entered, release = Event(), Event()

    def collect(self, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(MptMediaExecutor, "final_audio_evidence", collect)
    with TestClient(content_app(restarted)) as http:
        with Session(restarted.engine) as session:
            content = session.get(Content, content_id)
            content.current_stage = "render"
            session.add(content)
            session.commit()
        assert (
            http.post(
                f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
            ).status_code
            == 409
        )
        with Session(restarted.engine) as session:
            content = session.get(Content, content_id)
            content.current_stage = "audio_evidence_running"
            session.add(content)
            session.commit()
        if changed_snapshot:
            response = http.post(
                f"/api/v1/contents/{content_id}/resume-acceptance", headers=ADMIN
            )
            assert response.status_code == 200
            assert response.json()["current_stage"] == "audio_evidence_closed"
            assert not entered.is_set()
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(
                    http.post,
                    f"/api/v1/contents/{content_id}/resume-acceptance",
                    headers=ADMIN,
                )
                assert entered.wait(5)
                second = pool.submit(
                    http.post,
                    f"/api/v1/contents/{content_id}/resume-acceptance",
                    headers=ADMIN,
                )
                assert not second.done()
                release.set()
                assert sorted(
                    [first.result().status_code, second.result().status_code]
                ) == [200, 409]
        with Session(restarted.engine) as session:
            artifact = session.get(Artifact, video_id)
            assert artifact.sha256 == digest
            assert (
                len(
                    session.exec(
                        select(Artifact).where(Artifact.artifact_type == "final_video")
                    ).all()
                )
                == 1
            )
            tasks = session.exec(
                select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_QC)
            ).all()
            assert len(tasks) == (0 if changed_snapshot else 1)
            if tasks:
                assert tasks[0].schema_version == "2"
                assert tasks[0].input_data["video_artifact_id"] == video_id
                assert (
                    tasks[0].input_data["acceptance_subject"]["video_sha256"] == digest
                )


def asgi_stream_upload(app, task_id, token, chunks, declared_length=None):
    statuses, read = [], []
    iterator = iter(chunks)

    async def receive():
        body, more = next(iterator)
        read.append(len(body))
        return {"type": "http.request", "body": body, "more_body": more}

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("127.0.0.1", 8010),
        "client": ("127.0.0.1", 1234),
        "path": f"/api/v1/agent/tasks/{task_id}/artifacts",
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"image/png"),
        ],
    }
    if declared_length is not None:
        scope["headers"].append((b"content-length", str(declared_length).encode()))
    asyncio.run(app(scope, receive, send))
    return statuses[0], read


@pytest.mark.parametrize(
    "kind,role,maximum",
    [
        (TaskType.ASSET_GENERATE_IMAGE, "image", 25 * 1024 * 1024),
        (TaskType.DOUYIN_PUBLISH, "publish", 10 * 1024 * 1024),
    ],
)
def test_streamed_artifact_size_cap_accepts_boundary_and_stops_oversize_early(
    tmp_path, kind, role, maximum
):
    runtime = make_runtime(tmp_path)
    app = content_app(runtime)
    with TestClient(app) as http:
        candidate_id = seed_candidate(runtime)
        with Session(runtime.engine) as session:
            content = runtime.workflow_service(session).select_candidate(
                candidate_id, production_mode=ProductionMode.IMAGE_ONLY
            )
            task = runtime.task_service(session).create_task(
                task_type=kind,
                content_id=content.id,
                instructions="Upload bounded test image",
                input_data={"requests": [], "context_files": []},
            )
            task_id = task.id
        agent = client_for(http, role)
        if role == "publish":
            from glance_core.agent_roles import role_spec

            spec = role_spec(role)
            agent.settings.skill_name = spec.skill_name
            agent.settings.skill_version = spec.skill_version
        claimed = agent.claim()
        body = b"\x89PNG\r\n\x1a\n" + b"x" * (maximum - 8)
        status, read = asgi_stream_upload(
            app,
            task_id,
            claimed["lease_token"],
            [(body[:64], True), (body[64:], False)],
        )
        assert status == 200 and sum(read) == maximum
        status, read = asgi_stream_upload(
            app,
            task_id,
            claimed["lease_token"],
            [(body, True), (b"x", True), (b"must-not-read", False)],
            declared_length=1,
        )
        assert status == 413 and read == [maximum, 1]
        status, read = asgi_stream_upload(
            app,
            task_id,
            claimed["lease_token"],
            [(b"must-not-read", False)],
            declared_length=maximum + 1,
        )
        assert status == 413 and read == []
        with Session(runtime.engine) as session:
            artifacts = session.exec(
                select(Artifact).where(Artifact.producer_stage == task_id)
            ).all()
            assert len(artifacts) == 1 and artifacts[0].size_bytes == maximum


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="ffmpeg/ffprobe required",
)
def test_explicit_encoder_failure_is_recorded_without_hidden_software_retry(
    tmp_path, monkeypatch
):
    from app.business import pipeline
    from app.business.media_executor import MptMediaExecutor

    runtime = make_runtime(tmp_path)
    runtime.settings.asr_model = "test-asr"
    patched_asr(monkeypatch, available=False)
    with TestClient(content_app(runtime)) as http:
        content_id, _, _, _, _ = acceptance_fixture(http, runtime)
        calls = []

        with Session(runtime.engine) as session:
            content = session.get(Content, content_id)
            executor = MptMediaExecutor(Path(content.content_dir))
            voice = executor.root / "audio" / "narration.mp3"
            voice.parent.mkdir(parents=True, exist_ok=True)
            voice.write_bytes(b"stub voice; mocked renderer fails before audio decode")
            pipeline.register_artifact(session, content=content, executor=executor, path=voice,
                                       artifact_type="voice", stage="voice")

        def fail_render(self, **kwargs):
            calls.append(kwargs["encoder"])
            raise RuntimeError("Unrelated source file read failure")

        monkeypatch.setattr(MptMediaExecutor, "render", fail_render)
        monkeypatch.setattr(MptMediaExecutor, "align_subtitles", lambda *args, **kwargs: {"fixture": "pre-render ASR contract"})
        settings = runtime.settings.model_copy(update={"video_encoder": "h264_nvenc"})
        with pytest.raises(RuntimeError, match="Unrelated source file read failure"):
            pipeline.render_content(runtime.engine, settings, content_id)
        assert calls == ["h264_nvenc"]
        with Session(runtime.engine) as session:
            failed = session.exec(
                select(StageRun).where(
                    StageRun.content_id == content_id,
                    StageRun.stage == "render",
                )
            ).one()
            assert failed.status == StageStatus.FAILED
            assert failed.error_code == "RuntimeError"
            assert failed.error_message == "Unrelated source file read failure"
