"""Regression coverage for the actual immutable cover and publication evidence bundle."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image, ImageChops
from sqlmodel import Session, select

from glance_core.models import AgentTask, Artifact, AuditEvent, Content, ContentStatus, ProductionMode, TaskStatus, TaskType
from glance_core.task_service import stable_hash
from glance_media.executor import MediaExecutor
from test.business.test_discovery_http import make_runtime


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="actual libass cover required")
def test_ai_cover_is_disclosed_crop_safe_balanced_and_immutable(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1920), "black").save(source)
    legacy = tmp_path / "publish" / "cover.jpg"
    legacy.parent.mkdir()
    legacy.write_bytes(b"old frozen cover")
    old_ass = tmp_path / "temp" / "cover" / "cover.ass"
    old_ass.parent.mkdir(parents=True)
    old_ass.write_bytes(b"old frozen ASS")
    executor = MediaExecutor(tmp_path)
    output = executor.build_cover(source, "共享专注工作舱", ai_generated=True)
    receipt = json.loads(output.with_name("composition.json").read_text(encoding="utf-8"))
    assert legacy.read_bytes() == b"old frozen cover"
    assert old_ass.read_bytes() == b"old frozen ASS"
    assert output != legacy and "cover-revisions" in output.parts
    assert receipt["disclosure"] == "AI概念示意·非实拍"
    assert receipt["title_lines"] == ["共享专注", "工作舱"]
    assert receipt["source_sha256"] == digest(source)
    assert receipt["cover_sha256"] == digest(output)
    with Image.open(output) as cover:
        bounds = ImageChops.difference(cover.convert("RGB"), Image.new("RGB", cover.size)).getbbox()
        assert bounds and 656 < bounds[1] < bounds[3] < 1264
        # The disclosure has independently rendered pixels below the title.
        assert cover.crop((0, 1050, 1080, 1200)).getbbox() is not None
    assert executor.build_cover(source, "共享专注工作舱", ai_generated=True) == output
    other = executor.build_cover(source, "临时专注空间", ai_generated=True)
    assert other != output and receipt["cover_sha256"] == digest(output)
    output.write_bytes(b"tampered")
    with pytest.raises(Exception, match="[Ii]mmutable|integrity"):
        executor.build_cover(source, "共享专注工作舱", ai_generated=True)


def seed_package(runtime, *, revision=True):
    runtime.initialize()
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        candidate, _ = flow.ingest_candidate(source_type="agent", source_id="package", source_url="https://example.org/package", original_title="package")
        root = runtime.settings.content_dir / "fixture"
        root.mkdir(parents=True)
        content = Content(candidate_id=candidate.id, title="fixture", content_dir=str(root), business_status=ContentStatus.PACKAGED, production_mode=ProductionMode.IMAGE_ONLY)
        session.add(content)
        session.commit()
        def artifact(kind, relative, data):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            item = Artifact(content_id=content.id, artifact_type=kind, relative_path=relative, sha256=digest(path), size_bytes=len(data), mime_type="image/jpeg" if kind == "cover" else "video/mp4", producer_stage="package")
            session.add(item)
            session.commit()
            return item
        video = artifact("publish_video", "publish/final.mp4", b"actual final")
        cover = artifact("cover", "publish/cover-revisions/new/cover.jpg" if revision else "publish/cover.jpg", b"actual cover")
        if revision:
            receipt = {"version": 1, "source_sha256": "a" * 64, "cover_sha256": cover.sha256, "cover_size_bytes": cover.size_bytes, "disclosure": "AI概念示意·非实拍"}
            (root / cover.relative_path).with_name("composition.json").write_text(json.dumps(receipt), encoding="utf-8")
            session.add(AuditEvent(actor_type="system", actor_id="pipeline", entity_type="content", entity_id=content.id,
                                   action="cover_ai_upgraded", metadata_json={"artifact_id": cover.id, "base_source": "generated_image"}))
            session.commit()
        return content.id, video.id, cover.id


def test_review_contains_current_bytes_and_explicit_evidence_gaps(tmp_path):
    runtime = make_runtime(tmp_path)
    content_id, video_id, cover_id = seed_package(runtime)
    with Session(runtime.engine) as session:
        payload = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
        evidence = payload["production_evidence"]
        assert evidence["publish_video"]["artifact_id"] == video_id
        assert evidence["publish_video"]["sha256"] == hashlib.sha256(b"actual final").hexdigest()
        assert evidence["cover"]["artifact_id"] == cover_id
        assert evidence["cover_composition"]["data"]["disclosure"] == "AI概念示意·非实拍"
        assert evidence["evidence_gaps"]
        assert evidence["license_assessed"] is False
        assert evidence["voice"]["provider"] == "unknown"
        assert evidence["bgm"]["usage"] == "unknown"
        assert "publish/cover-revisions/new/composition.json" in payload["context_files"]


def test_legacy_packaged_review_is_not_reopened_by_upgrade(tmp_path):
    runtime = make_runtime(tmp_path)
    content_id, _, _ = seed_package(runtime, revision=False)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        old_input = flow.build_publish_review_input(content_id, settings=runtime.settings)
        assert "production_evidence" not in old_input
        old = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, old_input)
        old.status, old.result_data = TaskStatus.COMPLETED, {"verdict": "BLOCKED"}
        session.add(old)
        session.commit()
        session.refresh(old)
        before = stable_hash(old.model_dump(mode="json"))
        assert flow.schedule_manual_assist_tasks(settings=runtime.settings) == []
        session.refresh(old)
        assert stable_hash(old.model_dump(mode="json")) == before


def test_changed_cover_creates_one_fresh_review_preserving_old_blocked(tmp_path):
    runtime = make_runtime(tmp_path)
    content_id, _, cover_id = seed_package(runtime)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        old = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, {"cover_artifact_id": "old"})
        old.status, old.result_data = TaskStatus.COMPLETED, {"verdict": "BLOCKED"}
        session.add(old)
        session.commit()
        session.refresh(old)
        before = stable_hash(old.model_dump(mode="json"))
        created = flow.schedule_manual_assist_tasks(settings=runtime.settings)
        assert len(created) == 1 and created[0].input_data["cover_artifact_id"] == cover_id
        assert created[0].input_data["production_evidence"]["cover"]["artifact_id"] == cover_id
        assert flow.schedule_manual_assist_tasks(settings=runtime.settings) == []
        session.refresh(old)
        assert stable_hash(old.model_dump(mode="json")) == before
        assert len(session.exec(select(AgentTask).where(AgentTask.content_id == content_id)).all()) == 2


def test_authenticated_rebuild_http_is_immutable_idempotent_and_metadata_queues_review(tmp_path):
    from fastapi.testclient import TestClient
    from glance_core.models import AuditEvent
    from glance_core.models import utc_now
    from app.business import pipeline
    from test.business.test_content_http import content_app
    from test.business.test_discovery_http import ADMIN

    runtime = make_runtime(tmp_path)
    runtime.settings.cover_ai_enabled = True
    runtime.settings.asset_image_enabled = True
    runtime.settings.publish_prepare_enabled = True
    runtime.settings.archive_enabled = False
    content_id, _, legacy_id = seed_package(runtime, revision=False)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        base = root / "agent-assets" / "base.png"
        base.parent.mkdir()
        Image.new("RGB", (1080, 1920), "black").save(base)
        script = flow.create_agent_task(content_id, TaskType.SHORT_SCRIPT, {})
        script.status, script.result_data = TaskStatus.COMPLETED, {"cover_title": "共享专注工作舱"}
        session.add(script)
        generation = flow.create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {"asset_purpose": "cover", "requests": []}, allow_multiple=True)
        image = Artifact(content_id=content_id, artifact_type="agent_generated_image", relative_path=base.relative_to(root).as_posix(), sha256=digest(base), size_bytes=base.stat().st_size, mime_type="image/png", producer_stage=generation.id)
        session.add(image)
        session.commit()
        generation_id = generation.id
        generation.status, generation.result_data, generation.completed_at = TaskStatus.COMPLETED, {"images": [{"artifact_id": image.id}]}, utc_now()
        session.add(generation)
        session.commit()
        old = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, flow.build_publish_review_input(content_id, settings=runtime.settings))
        old.status, old.result_data = TaskStatus.COMPLETED, {"verdict": "BLOCKED", "reasons": ["missing disclosure"]}
        session.add(old)
        session.commit()
        session.refresh(old)
        old_id, old_hash = old.id, stable_hash(old.model_dump(mode="json"))
        legacy_path = root / session.get(Artifact, legacy_id).relative_path
        old_bytes = legacy_path.read_bytes()

    with TestClient(content_app(runtime)) as http:
        endpoint = f"/api/v1/contents/{content_id}/rebuild-cover"
        assert http.post(endpoint, json={}).status_code == 401
        assert http.post(endpoint, headers=ADMIN, json={"use_source_frame": True}).status_code == 422
        first = http.post(endpoint, headers=ADMIN, json={})
        assert first.status_code == 200, first.text
        second = http.post(endpoint, headers=ADMIN, json={})
        assert second.status_code == 200, second.text
        assert first.json()["id"] == second.json()["id"] != legacy_id
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            prepare = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_PREPARE)).all()
            assert len(prepare) == 1
            assert prepare[0].input_data["cover_artifact_id"] == first.json()["id"]
            assert session.get(AgentTask, old_id).result_data["verdict"] == "BLOCKED"
            prepare[0].status, prepare[0].result_data = TaskStatus.COMPLETED, {"description_zh": "metadata fixture"}
            session.add(prepare[0])
            session.commit()
            prepare_id = prepare[0].id
        pipeline.advance_after_agent(runtime.engine, runtime.settings, prepare_id)
        pipeline.advance_after_agent(runtime.engine, runtime.settings, prepare_id)
        with Session(runtime.engine) as session:
            reviews = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all()
            assert len(reviews) == 2
            fresh = next(task for task in reviews if task.id != old_id)
            direct = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            assert fresh.input_data == direct
            assert fresh.input_data["production_evidence"]["cover_generation"]["task_id"] == generation_id
            assert stable_hash(session.get(AgentTask, old_id).model_dump(mode="json")) == old_hash
            assert legacy_path.read_bytes() == old_bytes
            assert len(session.exec(select(Artifact).where(Artifact.content_id == content_id, Artifact.artifact_type == "cover")).all()) == 2
            assert not session.exec(select(AuditEvent).where(AuditEvent.action == "publish_intent_registered")).all()
        # A -> B -> A is a new current selection; repeated A stays idempotent.
        b = http.post(endpoint, headers=ADMIN, json={"title": "临时专注空间"})
        a = http.post(endpoint, headers=ADMIN, json={"title": "共享专注工作舱"})
        again = http.post(endpoint, headers=ADMIN, json={"title": "共享专注工作舱"})
        assert b.status_code == a.status_code == again.status_code == 200
        assert a.json()["id"] != first.json()["id"] and again.json()["id"] == a.json()["id"]
        with Session(runtime.engine) as session:
            latest = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            assert latest["cover_artifact_id"] == a.json()["id"]
            assert latest["production_evidence"]["cover_composition"]["data"]["source_binding"]["generation_task_id"] == generation_id
            old_generation = session.get(AgentTask, generation_id)
            from datetime import timedelta
            old_generation.completed_at = utc_now() + timedelta(hours=1)
            session.add(old_generation)
            newer = runtime.workflow_service(session).create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {"asset_purpose": "cover", "requests": [{"segment_index": 0}]}, allow_multiple=True)
            newer_id = newer.id
            newer_image = Artifact(content_id=content_id, artifact_type="agent_generated_image", relative_path=base.relative_to(root).as_posix(), sha256=digest(base), size_bytes=base.stat().st_size, mime_type="image/png", producer_stage=newer_id)
            session.add(newer_image)
            session.commit()
            newer.status, newer.result_data, newer.completed_at = TaskStatus.COMPLETED, {"images": [{"artifact_id": newer_image.id}]}, utc_now()
            session.add(newer)
            session.commit()
        newer_response = http.post(endpoint, headers=ADMIN, json={})
        assert newer_response.status_code == 200, newer_response.text
        with Session(runtime.engine) as session:
            latest = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            assert latest["production_evidence"]["cover_generation"]["task_id"] == newer_id
            pending = runtime.workflow_service(session).create_agent_task(content_id, TaskType.ASSET_GENERATE_IMAGE, {"asset_purpose": "cover", "requests": [{"segment_index": 1}]}, allow_multiple=True)
            assert pending.status == TaskStatus.PENDING
        with Session(runtime.engine) as session:
            before_content = stable_hash(session.get(Content, content_id).model_dump(mode="json"))
            before_artifacts = [item.id for item in session.exec(select(Artifact).where(Artifact.content_id == content_id)).all()]
        # A late completion of an older generation cannot replace the current selection either.
        pipeline.advance_after_agent(runtime.engine, runtime.settings, generation_id)
        assert http.post(endpoint, headers=ADMIN, json={"title": "失败不改旧文案"}).status_code == 409
        with Session(runtime.engine) as session:
            assert stable_hash(session.get(Content, content_id).model_dump(mode="json")) == before_content
            assert [item.id for item in session.exec(select(Artifact).where(Artifact.content_id == content_id)).all()] == before_artifacts
            assert stable_hash(session.get(AgentTask, old_id).model_dump(mode="json")) == old_hash


def test_new_package_never_reads_or_downloads_escaping_receipts(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    content_id, _, cover_id = seed_package(runtime)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        root = Path(content.content_dir).resolve()
        outside = root.parent / "outside"
        outside.mkdir()
        (outside / "composition.json").write_text('{"secret":"outside"}', encoding="utf-8")
        cover = session.get(Artifact, cover_id)
        cover.relative_path = "publish/cover-revisions/../../../outside/cover.jpg"
        session.add(cover)
        session.commit()
        original = Path.read_bytes
        def guarded(path):
            assert path.resolve().is_relative_to(root), "unsafe external read attempted"
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", guarded)
        payload = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
        assert payload["production_evidence"]["cover_composition"]["data"] == {}
        assert cover.relative_path not in payload["context_files"]
        assert "outside" not in json.dumps(payload["production_evidence"])


def test_new_package_reparse_receipts_do_not_escape_content_root(tmp_path, monkeypatch):
    runtime = make_runtime(tmp_path)
    content_id, _, cover_id = seed_package(runtime)
    with Session(runtime.engine) as session:
        root = Path(session.get(Content, content_id).content_dir).resolve()
        external = root.parent / "external"
        external.mkdir()
        (external / "mpt-render-request.json").write_text('{"secret":"outside"}', encoding="utf-8")
        try:
            (root / "render").symlink_to(external, target_is_directory=True)
        except OSError:
            pytest.skip("directory symlink permission unavailable")
        original = Path.read_bytes
        def guarded(path):
            assert path.resolve().is_relative_to(root), "unsafe reparse read attempted"
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", guarded)
        payload = runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
        assert payload["production_evidence"]["bgm"]["usage"] == "unknown"
        assert "outside" not in json.dumps(payload["production_evidence"])


def test_review_binds_seven_generated_images_accepted_qc_and_historical_voice_request(tmp_path):
    from glance_core.models import AgentSubmission, AuditEvent, StageRun, StageStatus, utc_now
    from app.business.pipeline import _manual_approval_signature

    runtime = make_runtime(tmp_path)
    content_id, video_id, _ = seed_package(runtime)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        root = Path(content.content_dir)
        def task(kind, inputs, result):
            item = AgentTask(content_id=content_id, task_type=kind, prompt_name="fixture", instructions="fixture",
                             input_data=inputs, input_hash=stable_hash(inputs), status=TaskStatus.COMPLETED,
                             result_data=result, attempt=1, completed_at=utc_now())
            session.add(item)
            session.commit()
            session.refresh(item)
            return item
        def artifact(kind, relative, data, producer="render"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            item = Artifact(content_id=content_id, artifact_type=kind, relative_path=relative, sha256=digest(path),
                            size_bytes=len(data), mime_type="image/png" if "image" in kind else "audio/mpeg", producer_stage=producer)
            session.add(item)
            session.commit()
            session.refresh(item)
            return item
        research = task(TaskType.CONTENT_RESEARCH, {}, {"primary_sources": [], "secondary_sources": []})
        script = task(TaskType.SHORT_SCRIPT, {"upstream_task_ids": {"research": research.id}}, {"full_narration": "测试", "estimated_duration_sec": 26.8})
        voice = artifact("voice", "audio/frozen/narration.mp3", b"frozen voice")
        voice_binding = {key: getattr(voice, key) for key in ("id", "relative_path", "sha256", "size_bytes")}
        plan = task(TaskType.VISUAL_PLAN, {"upstream_task_ids": {"script": script.id}, "voice_artifact": voice_binding}, {"segments": []})
        requests = [{"segment_index": i, "asset_request": {"information": f"concept {i}"}} for i in range(7)]
        generation = task(TaskType.ASSET_GENERATE_IMAGE, {"upstream_task_ids": {"visual_plan": plan.id}, "requests": requests}, {})
        images = [artifact("agent_generated_image", f"agent-assets/{i}.png", f"image {i}".encode(), generation.id) for i in range(7)]
        generation.result_data = {"images": [{"artifact_id": item.id, "segment_index": i, "tool_name": "native_image", "width": 1080, "height": 1920, "prompt_summary": f"concept {i}"} for i, item in enumerate(images)]}
        plan.result_data = {"segments": [{"asset_id": item.id, "layout": "image_card"} for item in images]}
        session.add(generation)
        session.add(plan)
        session.commit()
        plan_qc = task(TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id, "script": script.id}}, {"passed": True})
        asset_qc = task(TaskType.ASSET_QC, {"upstream_task_ids": {"visual_plan": plan.id, "asset_generate_image": generation.id}}, {"passed": True})
        final = artifact("final_video", "render/revisions/current/final.mp4", b"actual final")
        qc = task(TaskType.CONTENT_QC, {"upstream_task_ids": {"script": script.id, "visual_plan": plan.id}, "video_artifact_id": final.id}, {"passed": True, "checks": {"subtitles_synced": True}, "visual_review": {"pages": ["do not duplicate huge pages"]}})
        tasks = [research, script, plan, generation, plan_qc, asset_qc, qc]
        for item in tasks:
            raw = runtime.settings.agent_submission_dir / item.id / "1" / "result.json"
            raw.parent.mkdir(parents=True)
            raw.write_text(json.dumps(item.result_data), encoding="utf-8")
            session.add(AgentSubmission(task_id=item.id, agent_id="fixture", attempt=1, raw_result_path=str(raw), result_hash=stable_hash(item.result_data), validation_status="ACCEPTED"))
        proof = {"id": script.id, "input_hash": script.input_hash, "result_hash": stable_hash(script.result_data), "status": "COMPLETED", "attempt": 1}
        approval = AuditEvent(actor_type="human", actor_id="admin-api", entity_type="content", entity_id=content_id,
                              action="manual_script_approved", metadata_json={"contract_version": 1, "producer": "mpt-admin-approve-script", "tasks": {"short_script": proof}, "voice_artifact": voice_binding})
        approval.metadata_json = {**approval.metadata_json, "signature": _manual_approval_signature(runtime.settings, approval)}
        session.add(approval)
        options = {"voice": runtime.settings.tts_voice, "rate": runtime.settings.tts_rate, "volume": runtime.settings.tts_volume,
                   "provider": runtime.settings.tts_provider, "provider_endpoint": stable_hash(runtime.settings.tts_provider_endpoint), "target_duration_sec": 26.8}
        request_hash = stable_hash({"version": 1, "script": proof, "approval": {"short_script": proof}, "voice_options": options, "automatic_scope": None})
        stage = StageRun(content_id=content_id, stage="voice", status=StageStatus.SUCCESS, input_hash=request_hash)
        session.add(stage)
        session.commit()
        (root / "render/mpt-render-request.json").write_text(json.dumps({"revision_id": "current", "voice_sha256": voice.sha256, "bgm_sha256": None, "bgm_volume": .15}), encoding="utf-8")
        (root / "render/revisions/current/outputs.json").write_text(json.dumps({"final.mp4": final.sha256}), encoding="utf-8")
        flow = runtime.workflow_service(session)
        payload = flow.build_publish_review_input(content_id, settings=runtime.settings)
        evidence = payload["production_evidence"]
        assert flow.build_publish_review_input(content_id)["production_evidence"] == evidence
        assert evidence["current_final_qc_subject_matches"] is True
        assert evidence["content_qc"]["accepted_raw_verified"] is True
        assert "visual_review" not in evidence["content_qc"]["result"]
        assert len(evidence["images"]) == 7
        assert all(item["generation_receipt"]["accepted_raw_verified"] for item in evidence["images"])
        assert [item["artifact"]["artifact_id"] for item in evidence["images"]] == [item.id for item in images]
        assert evidence["asset_qc"][0]["task_id"] == asset_qc.id
        assert evidence["plan_qc"]["task_id"] == plan_qc.id
        assert evidence["voice"]["provider"] == "unknown"
        assert evidence["voice"]["historical_request"]["options"]["provider"] == runtime.settings.tts_provider
        assert evidence["voice"]["historical_request"]["actual_provider_verified"] is False
        assert evidence["bgm"]["usage"] == "none"
        assert str(root) not in json.dumps(evidence)
        stage.input_hash = "0" * 64
        session.add(stage)
        session.commit()
        changed = flow.build_publish_review_input(content_id, settings=runtime.settings)["production_evidence"]
        assert "historical_request" not in changed["voice"]
        assert changed["voice"]["provider"] == "unknown"
        # A stale passed QC cannot certify newly replaced final bytes.
        artifact("final_video", "render/revisions/replacement/final.mp4", b"replacement")
        assert flow.build_publish_review_input(content_id, settings=runtime.settings)["production_evidence"]["current_final_qc_subject_matches"] is False


def test_cover_composer_rejects_redirected_revision_write_directory(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (1080, 1920), "black").save(source)
    external = tmp_path.parent / (tmp_path.name + "-outside")
    external.mkdir()
    (tmp_path / "publish").mkdir()
    try:
        (tmp_path / "publish/cover-revisions").symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    with pytest.raises(Exception, match="escapes content root"):
        MediaExecutor(tmp_path).build_cover(source, "安全封面", ai_generated=True)
    assert list(external.iterdir()) == []


def seed_named_context_package(runtime, *, image_names=("image.png", "image.png"), same_bytes=True):
    content_id, video_id, _ = seed_package(runtime)
    with Session(runtime.engine) as session:
        root = Path(session.get(Content, content_id).content_dir)
        publish = session.get(Artifact, video_id)
        final_path = root / "render/revisions/current/final.mp4"
        final_path.parent.mkdir(parents=True)
        final_path.write_bytes((root / publish.relative_path).read_bytes())
        final = Artifact(content_id=content_id, artifact_type="final_video", relative_path=final_path.relative_to(root).as_posix(), sha256=digest(final_path), size_bytes=final_path.stat().st_size, mime_type="video/mp4", producer_stage="render")
        session.add(final)
        images = []
        for index, name in enumerate(image_names):
            image_path = root / f"source-images/{index}" / name
            image_path.parent.mkdir(parents=True)
            image_path.write_bytes(b"same image bytes" if same_bytes else (b"image bytes " + bytes(index + 1)))
            image = Artifact(content_id=content_id, artifact_type="source_image", relative_path=image_path.relative_to(root).as_posix(), sha256=digest(image_path), size_bytes=image_path.stat().st_size, mime_type="image/png", producer_stage="source")
            session.add(image)
            images.append(image)
        session.commit()
        plan_input = {}
        plan = AgentTask(content_id=content_id, task_type=TaskType.VISUAL_PLAN, prompt_name="fixture", instructions="fixture", status=TaskStatus.COMPLETED, input_data=plan_input, input_hash=stable_hash(plan_input), result_data={"segments": [{"asset_id": image.id} for image in images]})
        session.add(plan)
        session.commit()
        qc_input = {"video_artifact_id": final.id, "upstream_task_ids": {"visual_plan": plan.id}}
        qc = AgentTask(content_id=content_id, task_type=TaskType.CONTENT_QC, prompt_name="fixture", instructions="fixture", status=TaskStatus.COMPLETED, input_data=qc_input, input_hash=stable_hash(qc_input), result_data={"passed": True})
        session.add(qc)
        session.commit()
    return content_id


@pytest.mark.parametrize("names", [("image.png", "image.png"), ("A.png", "a.png")])
def test_review_real_http_bundle_sdk_context_has_unique_windows_names_and_no_manifest_gaps(tmp_path, names):
    from fastapi.testclient import TestClient
    from glance_agent.cli import _download_context, _manifest_gaps
    from app.business.bundles import context_files_for
    from test.business.test_content_http import content_app, client_for

    runtime = make_runtime(tmp_path)
    content_id = seed_named_context_package(runtime, image_names=names)
    with TestClient(content_app(runtime), base_url="http://127.0.0.1:8010") as http:
        with Session(runtime.engine) as session:
            flow = runtime.workflow_service(session)
            payload = flow.build_publish_review_input(content_id, settings=runtime.settings)
            task = flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, payload)
            task_id = task.id
            paths = context_files_for(task, session)
            # publish/final.mp4 takes precedence over the identical QC final copy.
            assert paths[0] == "publish/final.mp4"
        assistant = client_for(http, "assistant")
        claim = assistant.dispatch(wait=0, task_id=task_id)
        assert claim["should_run"] and claim["task_id"] == task_id
        assistant.confirm_claim(task_id, claim["lease_token"], assistant.settings.agent_id)
        bundle = assistant.bundle(claim["bundle_url"], claim["lease_token"])
        workspace = tmp_path / "isolated-sdk-workspace"
        (workspace / "context").mkdir(parents=True)
        _download_context(assistant, bundle, claim["lease_token"], workspace)
        gaps = _manifest_gaps(bundle, workspace)
        bundle_names = [entry["name"] for entry in bundle["context_files"]]
        print(json.dumps({"sdk_context_names": bundle_names, "manifest_gaps": gaps}))
        assert gaps == []
        assert len(bundle_names) == len({name.casefold() for name in bundle_names})
        assert len(bundle_names) == 4  # video, cover, composition, one identical image.
        assert [entry["name"] for entry in bundle["context_manifest"]] == bundle_names


@pytest.mark.parametrize("names", [("image.png", "image.png"), ("A.png", "a.png")])
def test_review_conflicting_windows_context_names_fail_before_review_creation(tmp_path, names):
    runtime = make_runtime(tmp_path)
    content_id = seed_named_context_package(runtime, image_names=names, same_bytes=False)
    with Session(runtime.engine) as session:
        flow = runtime.workflow_service(session)
        with pytest.raises(ValueError, match="context.*filename.*collision"):
            payload = flow.build_publish_review_input(content_id, settings=runtime.settings)
            flow.create_agent_task(content_id, TaskType.PUBLISH_REVIEW_ASSIST, payload)
        assert session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).all() == []


@pytest.mark.parametrize("mode", [ProductionMode.FOOTAGE, ProductionMode.IMAGE_ONLY])
def test_non_ai_package_keeps_legacy_review_semantics_and_sdk_bundle(tmp_path, mode):
    from fastapi.testclient import TestClient
    from glance_agent.cli import _download_context, _manifest_gaps
    from app.business import pipeline
    from test.business.test_content_http import content_app, client_for

    runtime = make_runtime(tmp_path)
    runtime.settings.publish_prepare_enabled = True
    runtime.settings.archive_enabled = False
    content_id, _, _ = seed_package(runtime, revision=False)
    with Session(runtime.engine) as session:
        content = session.get(Content, content_id)
        content.production_mode = mode
        session.add(content)
        session.commit()
        flow = runtime.workflow_service(session)
        script = flow.create_agent_task(content_id, TaskType.SHORT_SCRIPT, {})
        script.status, script.result_data = TaskStatus.COMPLETED, {"cover_title": "实拍工作空间"}
        session.add(script)
        session.commit()
        # The real non-AI fallback composer writes a new revision without generation claims.
        cover = pipeline._build_and_register_cover(session, runtime.settings, content)
        assert "cover-revisions" in cover.relative_path
        receipt = json.loads((Path(content.content_dir) / cover.relative_path).with_name("composition.json").read_text(encoding="utf-8"))
        assert receipt["source_binding"] is None
        assert flow._is_ai_cover_artifact(content_id, cover.id) is False
        pipeline._queue_post_package_tasks(session, flow, content, runtime.settings, script.result_data, {}, cover_artifact_id=cover.id)
        prepare = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_PREPARE)).one()
        prepare.status, prepare.result_data = TaskStatus.COMPLETED, {"description_zh": "real footage"}
        session.add(prepare)
        session.commit()
        prepare_id = prepare.id
    pipeline.advance_after_agent(runtime.engine, runtime.settings, prepare_id)
    with TestClient(content_app(runtime), base_url="http://127.0.0.1:8010") as http:
        with Session(runtime.engine) as session:
            task = session.exec(select(AgentTask).where(AgentTask.content_id == content_id, AgentTask.task_type == TaskType.PUBLISH_REVIEW_ASSIST)).one()
            assert "production_evidence" not in task.input_data
            assert set(task.input_data) == {"script", "research", "publish_artifact_id", "publish_video_url", "publish_prepare", "cover_artifact_id", "cover_image_url", "context_files"}
            assert task.input_data == runtime.workflow_service(session).build_publish_review_input(content_id, settings=runtime.settings)
            task_id = task.id
        assistant = client_for(http, "assistant")
        claim = assistant.dispatch(wait=0, task_id=task_id)
        assert claim["should_run"]
        assistant.confirm_claim(task_id, claim["lease_token"], assistant.settings.agent_id)
        bundle = assistant.bundle(claim["bundle_url"], claim["lease_token"])
        workspace = tmp_path / "footage-sdk-workspace"
        (workspace / "context").mkdir(parents=True)
        _download_context(assistant, bundle, claim["lease_token"], workspace)
        assert _manifest_gaps(bundle, workspace) == []
        assert [item["name"] for item in bundle["context_files"]] == ["final.mp4", "cover.jpg"]
