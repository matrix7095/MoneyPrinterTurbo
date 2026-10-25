"""New image-only work must collect source media before requesting AI supplements."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from glance_core.agent_roles import primary_role_for
from glance_core.database import build_engine, create_db
from glance_core.models import AgentSubmission, AgentTask, Artifact, AuditEvent, Candidate, Content, ContentStatus, ProductionMode, RightsStatus, TaskStatus, TaskType
from glance_core.prompts import default_instructions, lint_prompt
from glance_core.settings import Settings
from glance_core.task_schemas import AssetRequest, ContentResearchResult, IMAGE_BASE_FORBIDDEN_ELEMENTS, schema_for
from glance_core.task_service import AgentTaskService, TaskConflictError, TaskValidationError, stable_hash
from glance_core.workflow import WorkflowService
from sqlmodel import Session, select

from app.business import pipeline
from test.business.test_content_http import RESEARCH


@pytest.mark.parametrize("kind", [TaskType.CONTENT_RESEARCH, TaskType.VISUAL_PLAN, TaskType.VISUAL_PLAN_QC])
def test_source_priority_prompt_fields_are_declared(kind):
    warnings = " ".join(lint_prompt(kind, default_instructions(kind)))
    for field in ("source_media_priority_version", "source_media_search", "source_gap_reason", "video_url"):
        assert field not in warnings


@pytest.fixture
def subject(tmp_path):
    settings = Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'test.db'}",
                        asset_image_enabled=True)
    engine = build_engine(settings)
    create_db(engine)
    with Session(engine) as session:
        candidate = Candidate(source_type="agent", source_id="source-first", source_url="https://example.org/product",
                              original_title="Product")
        session.add(candidate)
        session.commit()
        content = Content(candidate_id=candidate.id, title="Product", content_dir=str(tmp_path / "content"),
                          production_mode=ProductionMode.IMAGE_ONLY)
        session.add(content)
        session.commit()
        assets = []
        for kind in ("source_image", "agent_generated_image"):
            asset = Artifact(content_id=content.id, artifact_type=kind, relative_path=f"{kind}.png",
                             sha256="a" * 64, size_bytes=1, mime_type="image/png", producer_stage="fixture")
            session.add(asset)
            session.commit()
            assets.append(asset)
        yield session, settings, content, assets
    engine.dispose()


def issue(subject, kind, inputs=None, *, legacy=False):
    session, settings, content, _ = subject
    inputs = {"production_mode": "image_only", **(inputs or {})}
    if kind == TaskType.VISUAL_PLAN and not legacy and not inputs.get("upstream_task_ids"):
        research = accepted_research(subject, originals=bool(inputs.get("source_images")))
        script = bound_script(subject, research)
        inputs["upstream_task_ids"] = {"script": script.id}
    service = AgentTaskService(session, settings.agent_submission_dir)
    if legacy:
        task = AgentTask(task_type=kind, prompt_name=kind.value, content_id=content.id, input_data=deepcopy(inputs),
                         input_hash=stable_hash(inputs), instructions="Legacy fixture", result_schema=schema_for(kind))
        session.add(task)
        session.commit()
    else:
        task = service.create_task(task_type=kind, content_id=content.id, input_data=inputs, instructions="Fixture")
    claimed = service.claim(agent_id="fixture-worker", role_id=primary_role_for(kind), capabilities={kind}, task_id=task.id)
    assert claimed is not None
    return service, claimed[0], claimed[1]


def complete(service, task, token, result):
    return service.complete(task_id=task.id, lease_token=token, agent_id="fixture-worker", result=result)


def accepted_research(subject, *, originals=True):
    session, _, _, assets = subject
    service, task, token = issue(subject, TaskType.CONTENT_RESEARCH)
    value = deepcopy(RESEARCH)
    if originals:
        value["source_images"] = [{"url": "https://example.org/product.png", "caption": "Product",
                                   "source_page": "https://example.org/product"}]
    complete(service, task, token, value)
    if originals:
        asset = assets[0]
        session.add(AuditEvent(actor_type="system", actor_id="pipeline", entity_type="agent_task",
                               entity_id=task.id, action="source_media_images_acquired", metadata_json={
                                   "result_hash": stable_hash(task.result_data), "images": [{
                                       "artifact_id": asset.id, "url": value["source_images"][0]["url"],
                                       "relative_path": asset.relative_path, "sha256": asset.sha256}],
                                   "unavailable_image_urls": []}))
        session.commit()
    return task


def bound_script(subject, research, *, packaged=False):
    session, settings, content, _ = subject
    inputs = {"upstream_task_ids": {"research": research.id}}
    if packaged:
        inputs["packaged_script_revision"] = True
    task = AgentTaskService(session, settings.agent_submission_dir).create_task(
        task_type=TaskType.SHORT_SCRIPT, content_id=content.id, input_data=inputs, instructions="Fixture")
    task.status, task.result_data = TaskStatus.COMPLETED, {"full_narration": "产品测试。"}
    session.add(task)
    session.commit()
    return task


def request(gap=True):
    value = {"information": "预约流程示意", "style_direction": "极简原理示意图",
             "subject": "an abstract shared reservation flow", "aspect_ratio": "9:16"}
    if gap:
        value["source_gap_reason"] = "官方图只展示产品外观，缺少预约状态转换示意；不重绘实际产品结构。"
    return value


def plan_input(subject):
    _, _, _, assets = subject
    return {"voice_duration_sec": 6, "asset_image_enabled": True, "image_only_gen_budget": 8,
            "allowed_layouts": ["image_card"], "allowed_asset_ids": [asset.id for asset in assets],
            "allowed_reference_artifact_ids": [assets[0].id],
            "source_images": [{"artifact_id": assets[0].id, "caption": "Official product view"}]}


def segments(subject, *, original=True, gap=True, reused=False):
    _, _, _, assets = subject
    first = {"narration_start_sec": 0, "narration_end_sec": 3, "layout": "image_card", "purpose": "产品身份"}
    first.update(asset_id=assets[0 if original else 1].id)
    second = {"narration_start_sec": 3, "narration_end_sec": 6, "layout": "image_card", "purpose": "预约原理"}
    second.update({"asset_id": assets[1].id} if reused else {"asset_request": request(gap)})
    return {"segments": [first, second]}


@pytest.mark.parametrize("invalid", ["missing", "empty_pages", "secondary_only", "blank_video_note", "bad_url", "blank_gap"])
def test_new_research_requires_evidenced_media_search(subject, invalid):
    result = deepcopy(RESEARCH)
    if invalid == "missing":
        result.pop("source_media_search")
    elif invalid == "empty_pages":
        result["source_media_search"]["checked_pages"] = []
    elif invalid == "secondary_only":
        result["source_media_search"]["checked_pages"][0]["source_type"] = "secondary"
    elif invalid == "blank_video_note":
        result["source_media_search"]["video_search_note"] = "  "
    elif invalid == "bad_url":
        result["source_media_search"]["checked_pages"][0]["url"] = "not a page"
    else:
        result["source_media_search"]["image_gap_reason"] = "  "
    service, task, token = issue(subject, TaskType.CONTENT_RESEARCH)
    with pytest.raises(TaskValidationError):
        complete(service, task, token, result)
    assert task.status == TaskStatus.LEASED
    assert task.attempt == 1


def test_missing_research_inventory_can_be_corrected_on_same_attempt(subject):
    service, task, token = issue(subject, TaskType.CONTENT_RESEARCH)
    missing = deepcopy(RESEARCH)
    missing.pop("source_media_search")
    with pytest.raises(TaskValidationError):
        complete(service, task, token, missing)
    completed = complete(service, task, token, RESEARCH)
    assert completed.status == TaskStatus.COMPLETED and completed.attempt == 1
    session = subject[0]
    assert [row.validation_status for row in session.exec(select(AgentSubmission)).all()] == ["REJECTED", "ACCEPTED"]


def test_available_images_need_search_record_but_not_an_invented_gap(subject):
    result = deepcopy(RESEARCH)
    result["source_images"] = [{"url": "https://example.org/product.png", "caption": "Product", "source_page": "https://example.org/product"}]
    result["source_media_search"]["image_gap_reason"] = ""
    service, task, token = issue(subject, TaskType.CONTENT_RESEARCH)
    assert complete(service, task, token, result).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("reused", [False, True])
def test_new_plan_cannot_ignore_registered_originals(subject, reused):
    service, task, token = issue(subject, TaskType.VISUAL_PLAN, plan_input(subject))
    with pytest.raises(TaskValidationError) as error:
        complete(service, task, token, segments(subject, original=False, reused=reused))
    assert any("已有相关产品原图" in entry["message"] for entry in error.value.errors)


def test_original_plus_explicit_ai_gap_is_accepted(subject):
    service, task, token = issue(subject, TaskType.VISUAL_PLAN, plan_input(subject))
    assert complete(service, task, token, segments(subject)).status == TaskStatus.COMPLETED


def test_ai_request_needs_specific_gap_even_when_original_is_used(subject):
    service, task, token = issue(subject, TaskType.VISUAL_PLAN, plan_input(subject))
    with pytest.raises(TaskValidationError) as error:
        complete(service, task, token, segments(subject, gap=False))
    assert any(entry["path"][-1] == "source_gap_reason" for entry in error.value.errors)


def test_exhausted_original_search_can_use_ai_supplements(subject):
    inputs = {**plan_input(subject), "source_images": [], "allowed_asset_ids": [], "allowed_reference_artifact_ids": []}
    result = segments(subject)
    for segment in result["segments"]:
        segment.pop("asset_id", None)
        segment["asset_request"] = request()
    service, task, token = issue(subject, TaskType.VISUAL_PLAN, inputs)
    assert complete(service, task, token, result).status == TaskStatus.COMPLETED


def test_generated_asset_cannot_be_declared_as_an_official_original(subject):
    inputs = plan_input(subject)
    inputs["source_images"] = [{"artifact_id": subject[3][1].id}]
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_SOURCE_WHITELIST_CHANGED"):
        issue(subject, TaskType.VISUAL_PLAN, inputs)


def test_authoritative_mode_and_version_cannot_be_downgraded(subject):
    service, task, _ = issue(subject, TaskType.CONTENT_RESEARCH,
                             {"production_mode": "footage", "source_media_priority_version": False})
    assert task.input_data["production_mode"] == "image_only"
    assert type(task.input_data["source_media_priority_version"]) is int
    assert task.input_data["source_media_priority_version"] == 1


def test_legacy_result_shapes_and_hashes_do_not_gain_new_none_fields():
    research = deepcopy(RESEARCH)
    research.pop("source_media_search")
    normalized = ContentResearchResult.model_validate(research).model_dump(mode="json")
    assert normalized == research
    assert stable_hash(normalized) == stable_hash(research)
    old_request = {**request(False), "reference_artifact_id": None, "text_rendering_enabled": False,
                   "forbidden_elements": list(IMAGE_BASE_FORBIDDEN_ELEMENTS),
                   "min_width": 1080, "min_height": 1920, "seed": None}
    normalized_request = AssetRequest.model_validate(old_request).model_dump(mode="json")
    assert normalized_request == old_request
    assert stable_hash(normalized_request) == stable_hash(old_request)


@pytest.mark.parametrize("kind", [TaskType.CONTENT_RESEARCH, TaskType.VISUAL_PLAN])
def test_legacy_accepted_results_remain_valid_and_idempotent(subject, kind):
    result = deepcopy(RESEARCH) if kind == TaskType.CONTENT_RESEARCH else segments(subject, original=False, gap=False)
    if kind == TaskType.CONTENT_RESEARCH:
        result.pop("source_media_search")
    service, task, token = issue(subject, kind, plan_input(subject), legacy=True)
    accepted = complete(service, task, token, result)
    frozen = deepcopy(accepted.result_data)
    assert "source_media_priority_version" not in task.input_data
    assert complete(service, task, token, result).result_data == frozen
    assert len(subject[0].exec(select(AgentSubmission)).all()) == 1


def test_footage_research_keeps_existing_contract(subject):
    subject[2].production_mode = ProductionMode.FOOTAGE
    subject[0].add(subject[2])
    subject[0].commit()
    result = deepcopy(RESEARCH)
    result.pop("source_media_search")
    service, task, token = issue(subject, TaskType.CONTENT_RESEARCH)
    assert "source_media_priority_version" not in task.input_data
    assert complete(service, task, token, result).status == TaskStatus.COMPLETED


def test_workflow_dedup_does_not_backfill_old_task_inputs(subject):
    session, settings, content, _ = subject
    legacy = AgentTask(task_type=TaskType.CONTENT_RESEARCH, prompt_name="content_research", content_id=content.id, instructions="Legacy",
                       input_data={"production_mode": "image_only"}, input_hash=stable_hash({"production_mode": "image_only"}))
    session.add(legacy)
    session.commit()
    frozen = deepcopy(legacy.input_data), legacy.input_hash, deepcopy(legacy.result_schema)
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    returned = flow.create_agent_task(content.id, TaskType.CONTENT_RESEARCH, {"production_mode": "image_only"})
    assert returned.id == legacy.id
    assert (returned.input_data, returned.input_hash, returned.result_schema) == frozen


@pytest.mark.parametrize("new_contract", [False, True])
def test_server_cannot_invent_ai_gaps_for_new_source_first_plans(subject, new_contract):
    _, settings, content, assets = subject
    original = {"segments": [{"narration_start_sec": 0, "narration_end_sec": 30,
                              "asset_id": assets[0].id, "layout": "image_card", "purpose": "Product"}]}
    inputs = {"voice_duration_sec": 30, "image_only_gen_budget": 8, "allowed_reference_artifact_ids": [assets[0].id]}
    if new_contract:
        inputs["source_media_priority_version"] = 1
    task = AgentTask(task_type=TaskType.VISUAL_PLAN, input_data=inputs, result_data=deepcopy(original))
    prepared, stats = pipeline._ensure_image_only_asset_requests(content=content, settings=settings, plan_task=task, script={})
    assert task.result_data == original
    if new_contract:
        assert prepared == original and stats["injected"] == 0
    else:
        assert len(prepared["segments"]) == 4 and stats["injected"] == 3


def test_visual_qc_receives_exact_source_priority_contract(subject):
    session, settings, content, _ = subject
    inputs = {**plan_input(subject), "production_mode": "image_only", "source_media_priority_version": 1,
              "source_media_search": RESEARCH["source_media_search"]}
    plan = AgentTask(task_type=TaskType.VISUAL_PLAN, prompt_name="visual_plan", instructions="Fixture",
                     content_id=content.id, input_data=inputs,
                     input_hash=stable_hash(inputs), status=TaskStatus.COMPLETED)
    session.add(plan)
    session.commit()
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    qc = flow.create_agent_task(content.id, TaskType.VISUAL_PLAN_QC,
                               {"upstream_task_ids": {"visual_plan": plan.id}, "source_media_priority_version": 0})
    for key in ("source_media_priority_version", "source_media_search", "source_images"):
        assert qc.input_data[key] == inputs[key]


def legacy_research(subject):
    service, research, token = issue(subject, TaskType.CONTENT_RESEARCH, legacy=True)
    value = deepcopy(RESEARCH)
    value.pop("source_media_search")
    complete(service, research, token, value)
    return research


def test_legacy_empty_research_cannot_issue_a_new_all_ai_plan(subject):
    session, settings, content, _ = subject
    research = legacy_research(subject)
    script = bound_script(subject, research)
    session.refresh(research)
    frozen = deepcopy(research.model_dump(mode="json"))
    inputs = {**plan_input(subject), "source_images": [], "allowed_asset_ids": [],
              "upstream_task_ids": {"script": script.id}}
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_AUDIT_REQUIRED"):
        AgentTaskService(session, settings.agent_submission_dir).create_task(
            task_type=TaskType.VISUAL_PLAN, content_id=content.id, input_data=inputs, instructions="Fixture")
    assert research.model_dump(mode="json") == frozen
    assert session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN)).first() is None


@pytest.mark.parametrize("script_ref", ["", " \t\r\n", None, 0, False, [], {}])
def test_audited_research_cannot_replace_a_nonempty_real_script_reference(subject, script_ref):
    session, settings, content, _ = subject
    research = accepted_research(subject, originals=False)
    inputs = {**plan_input(subject), "source_images": [], "allowed_asset_ids": [],
              "upstream_task_ids": {"script": script_ref, "research": research.id}}
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_SCRIPT_LINEAGE_REQUIRED"):
        AgentTaskService(session, settings.agent_submission_dir).create_task(
            task_type=TaskType.VISUAL_PLAN, content_id=content.id, input_data=inputs, instructions="Fixture")
    assert session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN)).first() is None


@pytest.mark.parametrize("padding", [" ", "\t"])
def test_script_reference_must_match_the_actual_id_exactly(subject, padding):
    session, settings, content, _ = subject
    research = accepted_research(subject, originals=False)
    script = bound_script(subject, research)
    inputs = {**plan_input(subject), "source_images": [], "allowed_asset_ids": [],
              "upstream_task_ids": {"script": f"{padding}{script.id}{padding}", "research": research.id}}
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_SCRIPT_LINEAGE_CHANGED"):
        AgentTaskService(session, settings.agent_submission_dir).create_task(
            task_type=TaskType.VISUAL_PLAN, content_id=content.id, input_data=inputs, instructions="Fixture")


@pytest.mark.parametrize("damage", ["input_hash", "result_hash", "submission", "foreign", "script_ref", "stale_script"])
def test_plan_requires_current_script_and_accepted_research(subject, damage):
    session, settings, content, _ = subject
    research = accepted_research(subject)
    script = bound_script(subject, research)
    if damage == "input_hash":
        research.input_hash = "changed"
    elif damage == "result_hash":
        research.result_data = {**research.result_data, "recommended_verdict": "changed after acceptance"}
    elif damage == "submission":
        session.delete(session.exec(select(AgentSubmission).where(AgentSubmission.task_id == research.id)).one())
    elif damage == "foreign":
        research.content_id = "content_foreign"
    elif damage == "script_ref":
        script.input_data = {**script.input_data, "upstream_task_ids": {"research": "agt_missing"}}
        script.input_hash = stable_hash(script.input_data)
    else:
        newer = AgentTask(task_type=TaskType.SHORT_SCRIPT, prompt_name="short_script", instructions="Pending replacement",
                          content_id=content.id, input_data={}, input_hash=stable_hash({}))
        session.add(newer)
    session.add(research)
    session.add(script)
    session.commit()
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_"):
        AgentTaskService(session, settings.agent_submission_dir).create_task(
            task_type=TaskType.VISUAL_PLAN, content_id=content.id,
            input_data={**plan_input(subject), "upstream_task_ids": {"script": script.id}}, instructions="Fixture")


def test_caller_cannot_replace_a_real_audit_or_omit_registered_originals(subject):
    session, settings, content, _ = subject
    research = accepted_research(subject)
    script = bound_script(subject, research)
    service = AgentTaskService(session, settings.agent_submission_dir)
    inputs = {**plan_input(subject), "upstream_task_ids": {"script": script.id}}
    forged = deepcopy(RESEARCH["source_media_search"])
    forged["video_search_note"] = "Caller asserted no official assets"
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_AUDIT_CHANGED"):
        service.create_task(task_type=TaskType.VISUAL_PLAN, content_id=content.id,
                            input_data={**inputs, "source_media_search": forged}, instructions="Fixture")
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_SOURCE_WHITELIST_CHANGED"):
        service.create_task(task_type=TaskType.VISUAL_PLAN, content_id=content.id,
                            input_data={**inputs, "source_images": [], "allowed_asset_ids": []}, instructions="Fixture")


def test_plan_rechecks_authoritative_audit_when_accepted_research_changes(subject):
    service, plan, token = issue(subject, TaskType.VISUAL_PLAN, plan_input(subject))
    session = subject[0]
    script = session.get(AgentTask, plan.input_data["upstream_task_ids"]["script"])
    research = session.get(AgentTask, script.input_data["upstream_task_ids"]["research"])
    research.result_data = {**research.result_data, "recommended_angle": "changed after plan issue"}
    session.add(research)
    session.commit()
    with pytest.raises(TaskValidationError) as error:
        complete(service, plan, token, segments(subject))
    assert "SOURCE_MEDIA_ACCEPTED_RESEARCH_CHANGED" in repr(error.value.errors)


@pytest.mark.parametrize("success_count", [0, 1])
def test_actual_acquisition_binds_successes_and_download_gaps_to_plan_and_qc(subject, monkeypatch, success_count):
    session, settings, content, _ = subject
    service, research, token = issue(subject, TaskType.CONTENT_RESEARCH)
    value = deepcopy(RESEARCH)
    value["source_images"] = [{"url": f"https://example.org/product-{index}.png", "caption": f"Product {index}",
                               "source_page": "https://example.org/product"} for index in range(2)]
    value["source_media_search"]["image_gap_reason"] = ""
    complete(service, research, token, value)
    path = Path(content.content_dir) / "source_images" / "source.png"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fixture original image")
    acquired = [{**value["source_images"][0], "path": path, "width": 1080, "height": 1920}][:success_count]
    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", lambda self, images: acquired)
    registered = pipeline._acquire_source_images(session, content, research.result_data, research)
    script = bound_script(subject, research)
    inputs = {**plan_input(subject), "upstream_task_ids": {"script": script.id}, "source_images": registered,
              "allowed_asset_ids": [row["artifact_id"] for row in registered], "allowed_reference_artifact_ids": []}
    plan = service.create_task(task_type=TaskType.VISUAL_PLAN, content_id=content.id, input_data=inputs, instructions="Fixture")
    evidence = plan.input_data["source_media_acquisition"]
    assert evidence["requested_image_urls"] == [item["url"] for item in value["source_images"]]
    assert evidence["downloaded_image_urls"] == [item["url"] for item in acquired]
    assert evidence["unavailable_image_urls"] == evidence["requested_image_urls"][success_count:]
    assert plan.input_data["source_media_search"]["image_gap_reason"] == ""
    result = segments(subject)
    if registered:
        result["segments"][0]["asset_id"] = registered[0]["artifact_id"]
    else:
        result["segments"][0].pop("asset_id")
        result["segments"][0]["asset_request"] = request()
    claimed = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.VISUAL_PLAN),
                            capabilities={TaskType.VISUAL_PLAN}, task_id=plan.id)
    complete(service, claimed[0], claimed[1], result)
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    qc = flow.create_agent_task(content.id, TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id}})
    assert qc.input_data["source_media_acquisition"] == evidence


def test_legacy_script_returns_to_real_research_before_voice_and_keeps_history(subject, monkeypatch):
    session, settings, content, _ = subject
    research = legacy_research(subject)
    old_script = bound_script(subject, research)
    service, qc, token = issue(subject, TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": old_script.id, "research": research.id}})
    complete(service, qc, token, {"passed": True, "issues": []})
    candidate = session.get(Candidate, content.candidate_id)
    candidate.rights_status = RightsStatus.RESEARCH_ONLY
    content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
    session.add(candidate)
    session.add(content)
    session.commit()
    frozen = {task.id: deepcopy(task.model_dump(mode="json")) for task in (research, old_script, qc)}
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: pytest.fail("voice before audited research"))
    engine = session.get_bind()
    result = pipeline.approve_script(engine, settings, content.id, manual_approval=True)
    assert (result.business_status, result.current_stage) == (ContentStatus.WAITING_AGENT, "agent_context")
    session.expire_all()
    refresh = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_RESEARCH,
                                                  AgentTask.status == TaskStatus.PENDING)).one()
    assert refresh.id != research.id and refresh.input_data["source_media_refresh_of"] == research.id
    assert refresh.attempt == 0 and refresh.dedup_key
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    content.business_status = ContentStatus.SCRIPT_REVIEW
    session.add(content)
    session.commit()
    pipeline._refresh_source_media_research(session, flow, content)
    assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_RESEARCH)).all()) == 2
    claim = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.CONTENT_RESEARCH),
                           capabilities={TaskType.CONTENT_RESEARCH}, task_id=refresh.id)
    complete(service, claim[0], claim[1], RESEARCH)
    pipeline.advance_after_agent(engine, settings, refresh.id)
    session.expire_all()
    new_script = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT,
                                                     AgentTask.status == TaskStatus.PENDING)).one()
    assert new_script.id != old_script.id and new_script.input_data["upstream_task_ids"]["research"] == refresh.id
    for task_id, previous in frozen.items():
        assert session.get(AgentTask, task_id).model_dump(mode="json") == previous
    assert session.exec(select(Artifact).where(Artifact.artifact_type == "voice")).first() is None


@pytest.mark.parametrize("entry", ["prepare", "approve"])
def test_published_legacy_content_is_never_returned_to_research(subject, monkeypatch, entry):
    session, settings, content, _ = subject
    legacy_research(subject)
    content.business_status = ContentStatus.PUBLISHED
    session.add(content)
    session.commit()
    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *args: None)
    action = pipeline.prepare_content if entry == "prepare" else pipeline.approve_script
    with pytest.raises(pipeline.NonRetryableMediaError, match="Published"):
        action(session.get_bind(), settings, content.id)
    session.expire_all()
    assert content.business_status == ContentStatus.PUBLISHED
    assert len(session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_RESEARCH)).all()) == 1


def test_packaged_revision_preserves_authoritative_audit_and_exact_material_whitelist(subject, monkeypatch):
    session, settings, content, _ = subject
    service, research, token = issue(subject, TaskType.CONTENT_RESEARCH)
    value = deepcopy(RESEARCH)
    value["source_images"] = [{"url": "https://example.org/current-product.png", "caption": "Official product view",
                               "source_page": "https://example.org/product"}]
    complete(service, research, token, value)
    root = Path(content.content_dir)
    image = root / "official.png"
    root.mkdir(parents=True)
    image.write_bytes(b"official source fixture")
    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", lambda self, images: [
        {**value["source_images"][0], "path": image, "width": 1080, "height": 1920}])
    rows = pipeline._acquire_source_images(session, content, research.result_data, research)
    previous = {"segments": [{"asset_id": rows[0]["artifact_id"], "layout": "image_card",
                              "narration_start_sec": 0, "narration_end_sec": 30, "purpose": "Product view"}]}
    script = bound_script(subject, research, packaged=True)
    script.input_data = {**script.input_data, "previous_plan": previous}
    script.input_hash = stable_hash(script.input_data)
    session.add(script)
    session.commit()
    service, qc, token = issue(subject, TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}})
    complete(service, qc, token, {"passed": True, "issues": []})
    candidate = session.get(Candidate, content.candidate_id)
    candidate.rights_status = RightsStatus.RESEARCH_ONLY
    content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
    session.add(candidate)
    session.add(content)
    session.commit()
    voice = root / "voice.wav"
    voice.write_bytes(b"new voice fixture")
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: SimpleNamespace(audio_path=voice, duration_sec=30))
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: {})
    monkeypatch.setattr(pipeline, "_require_script_assist", lambda *args, **kwargs: None)
    result = pipeline.approve_script(session.get_bind(), settings, content.id)
    assert result.current_stage == "visual_plan"
    session.expire_all()
    plan = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN)).one()
    assert plan.input_data["source_media_search"] == research.result_data["source_media_search"]
    assert plan.input_data["source_media_acquisition"]["research_task_id"] == research.id
    assert plan.input_data["allowed_asset_ids"] == [rows[0]["artifact_id"]]
    assert plan.input_data["source_images"] == rows
    assert plan.input_data["previous_plan"] == previous
    assert plan.input_data["source_media_priority_version"] == 1


@pytest.fixture
def footage_subject(subject):
    session, _, content, assets = subject
    content.production_mode = ProductionMode.FOOTAGE
    session.add(content)
    root = Path(content.content_dir)
    root.mkdir(parents=True, exist_ok=True)
    for asset in assets:
        path = root / asset.relative_path
        path.write_bytes(b"source fixture")
        asset.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        asset.size_bytes = path.stat().st_size
        session.add(asset)
    session.commit()
    return subject


@pytest.mark.parametrize("research_first", [False, True])
def test_footage_research_registers_images_once_across_scene_batches(footage_subject, monkeypatch, research_first):
    session, settings, content, _ = footage_subject
    service, research, token = issue(footage_subject, TaskType.CONTENT_RESEARCH, {"source_media_priority_version": 1})
    result = deepcopy(RESEARCH)
    result["source_images"] = [{"url": "https://example.org/product.png", "caption": "Official product",
                                "source_page": "https://example.org/product"}]
    complete(service, research, token, result)
    root = Path(content.content_dir)
    path = root / "official.png"
    path.write_bytes(b"official image fixture")
    calls = []
    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", lambda self, images: calls.append(images) or [
        {**result["source_images"][0], "path": path, "width": 1080, "height": 1920}])
    scene_tasks = []
    for index in (1, 2):
        scenes = [{"scene_id": f"s{index}", "start_sec": (index - 1) * 15, "end_sec": index * 15}]
        inputs = {"scenes": scenes, "batch_index": index, "batch_count": 2}
        task = AgentTask(task_type=TaskType.SCENE_ANALYZE, prompt_name="scene_analyze", content_id=content.id,
                         instructions="Fixture", input_data=inputs, input_hash=stable_hash(inputs),
                         result_data={"scenes": scenes})
        session.add(task)
        scene_tasks.append(task)
    session.commit()
    if research_first:
        pipeline.advance_after_agent(session.get_bind(), settings, research.id)
    for task in scene_tasks:
        task.status = TaskStatus.COMPLETED
        session.add(task)
        session.commit()
        pipeline.advance_after_agent(session.get_bind(), settings, task.id)
    if not research_first:
        pipeline.advance_after_agent(session.get_bind(), settings, research.id)
    session.expire_all()
    script = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SHORT_SCRIPT)).one()
    assert len(calls) == 1
    assert script.input_data["source_media_priority_version"] == 1
    assert script.input_data["upstream_task_ids"]["research"] == research.id
    receipts = session.exec(select(AuditEvent).where(AuditEvent.action == "source_media_images_acquired")).all()
    assert len(receipts) == 1
    assert len(receipts[0].metadata_json["images"]) == 1
    frozen = deepcopy(receipts[0].metadata_json)
    pipeline._acquire_source_images(session, content, result, research)
    assert len(calls) == 1
    session.refresh(receipts[0])
    assert receipts[0].metadata_json == frozen


@pytest.fixture
def footage_plan(footage_subject, monkeypatch):
    session, settings, content, assets = footage_subject
    service, research, token = issue(footage_subject, TaskType.CONTENT_RESEARCH, {"source_media_priority_version": 1})
    value = deepcopy(RESEARCH)
    value["source_images"] = [{"url": "https://example.org/product.png", "caption": "Official product",
                               "source_page": "https://example.org/product"}]
    complete(service, research, token, value)
    root = Path(content.content_dir)
    official = root / "official.png"
    official.write_bytes(b"official product fixture")
    monkeypatch.setattr(pipeline.MediaExecutor, "acquire_images", lambda self, images: [
        {**value["source_images"][0], "path": official, "width": 1080, "height": 1920}])
    rows = pipeline._acquire_source_images(session, content, research.result_data, research)
    session.add(AuditEvent(actor_type="system", actor_id="mpt-business", entity_type="agent_task", entity_id=research.id,
                           action="mpt_agent_consumed", metadata_json={"result_hash": stable_hash(research.result_data)}))
    script = bound_script(footage_subject, research)
    scenes = [{"scene_id": "s1", "start_sec": 0, "end_sec": 30, "thumbnail": "scenes/s1.jpg"}]
    scene_inputs = {"scenes": scenes}
    scene = AgentTask(task_type=TaskType.SCENE_ANALYZE, prompt_name="scene_analyze", content_id=content.id,
                      instructions="Fixture", input_data=scene_inputs, input_hash=stable_hash(scene_inputs),
                      status=TaskStatus.COMPLETED, result_data={"scenes": scenes})
    session.add(scene)
    (root / "scenes").mkdir()
    (root / "scenes/scenes.json").write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
    (root / "scenes/s1.jpg").write_bytes(b"scene thumbnail fixture")
    session.commit()
    script.input_data = {**script.input_data, "upstream_task_ids": {"research": research.id, "scene_analysis": scene.id}}
    script.input_hash = stable_hash(script.input_data)
    session.add(script)
    session.commit()
    service, qc, token = issue(footage_subject, TaskType.SCRIPT_QC, {"upstream_task_ids": {"script": script.id, "research": research.id}})
    complete(service, qc, token, {"passed": True, "issues": []})
    candidate = session.get(Candidate, content.candidate_id)
    candidate.rights_status = RightsStatus.SELF_OWNED
    content.business_status, content.current_stage = ContentStatus.SCRIPT_REVIEW, "script_review"
    session.add_all([candidate, content])
    session.commit()
    voice = root / "voice.wav"
    voice.write_bytes(b"voice fixture")
    boundaries = root / "word_boundaries.json"
    boundaries.write_text('{"boundaries": []}', encoding="utf-8")
    timing = {"voice_sha256": hashlib.sha256(voice.read_bytes()).hexdigest(),
              "boundaries_sha256": hashlib.sha256(boundaries.read_bytes()).hexdigest()}
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: SimpleNamespace(audio_path=voice, duration_sec=30))
    monkeypatch.setattr(pipeline.MediaExecutor, "visual_timing_evidence", lambda *args: timing)
    monkeypatch.setattr(pipeline, "_require_script_assist", lambda *args, **kwargs: None)

    def approve():
        pipeline.approve_script(session.get_bind(), settings, content.id)
        session.expire_all()
        return session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.VISUAL_PLAN)).one()

    return footage_subject, research, script, rows, approve


@pytest.mark.parametrize("ai_enabled", [False, True])
def test_footage_official_image_reaches_script_qc_and_visual_plan_with_ai_on_or_off(footage_plan, ai_enabled):
    from app.business import bundles
    subject, research, script, rows, approve = footage_plan
    session, settings, content, _ = subject
    settings.asset_image_enabled = ai_enabled
    frozen = deepcopy(script.input_data), script.input_hash
    source = bundles._script_source_context(script, session)
    assert source["source_images"][0]["artifact_id"] == rows[0]["artifact_id"]
    assert rows[0]["relative_path"] in bundles.context_files_for(script, session)
    qc = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.SCRIPT_QC)).one()
    assert bundles._script_source_context(qc, session) == source
    assert rows[0]["relative_path"] in bundles.context_files_for(qc, session)
    assert (script.input_data, script.input_hash) == frozen
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    review = flow.create_agent_task(content.id, TaskType.SCRIPT_REVIEW_ASSIST,
        {"upstream_task_ids": {"script": script.id, "script_qc": qc.id, "research": research.id}})
    service = AgentTaskService(session, settings.agent_submission_dir)
    review, review_token = service.claim(agent_id="fixture-reviewer", role_id=primary_role_for(TaskType.SCRIPT_REVIEW_ASSIST),
        capabilities={TaskType.SCRIPT_REVIEW_ASSIST}, task_id=review.id)
    bundle = bundles.build_bundle(review.id, review_token, service, session, flow, failure_recovery=lambda *a, **k: {})
    assert bundle["input"]["research"]["source_images"] == source["source_images"]
    assert bundle["input"]["source_images"] == source["source_images"]
    assert rows[0]["artifact_id"] in {item["input_ref"] for item in bundle["context_manifest"]}
    plan = approve()
    assert plan.input_data["source_media_priority_version"] == 1
    assert plan.input_data["source_media_acquisition"]["research_task_id"] == research.id
    assert plan.input_data["source_images"] == rows
    assert rows[0]["artifact_id"] in plan.input_data["allowed_asset_ids"]
    assert "image_card" in plan.input_data["allowed_layouts"]
    assert {"scenes/s1.jpg", rows[0]["relative_path"]}.issubset(plan.input_data["context_files"])
    service = AgentTaskService(session, settings.agent_submission_dir)
    task, token = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.VISUAL_PLAN),
                                capabilities={TaskType.VISUAL_PLAN}, task_id=plan.id)
    result = {"segments": [
        {"scene_id": "s1", "layout": "center_crop", "narration_start_sec": 0, "narration_end_sec": 15,
         "source_start_sec": 0, "source_end_sec": 15, "purpose": "官方原片"},
        {"asset_id": rows[0]["artifact_id"], "layout": "image_card", "narration_start_sec": 15,
         "narration_end_sec": 30, "purpose": "官方产品图"},
    ]}
    complete(service, task, token, result)
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    visual_qc = flow.create_agent_task(content.id, TaskType.VISUAL_PLAN_QC, {"upstream_task_ids": {"visual_plan": plan.id}})
    assert visual_qc.input_data["source_images"] == rows
    assert visual_qc.input_data["source_media_acquisition"] == plan.input_data["source_media_acquisition"]
    from glance_core.content_acceptance import joint_visual_plan_supported
    assert not joint_visual_plan_supported(result)


@pytest.mark.parametrize("original", ["scene", "mixed", "all_ai", "ai_no_gap"])
def test_footage_source_priority_accepts_video_without_forcing_images_and_rejects_all_ai(footage_plan, original):
    subject, _, _, rows, approve = footage_plan
    session, settings, _, assets = subject
    plan = approve()
    service = AgentTaskService(session, settings.agent_submission_dir)
    task, token = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.VISUAL_PLAN),
                                capabilities={TaskType.VISUAL_PLAN}, task_id=plan.id)
    first = {"scene_id": "s1", "layout": "center_crop", "narration_start_sec": 0,
             "narration_end_sec": 30, "source_start_sec": 0, "source_end_sec": 30, "purpose": "官方原片"}
    result = {"segments": [first]}
    if original == "all_ai":
        result = {"segments": [{"asset_id": assets[1].id, "layout": "image_card", "narration_start_sec": 0,
                                 "narration_end_sec": 30, "purpose": "已有生成图"}]}
    elif original in {"mixed", "ai_no_gap"}:
        first["narration_end_sec"] = first["source_end_sec"] = 10
        result["segments"].extend([
            {"asset_id": rows[0]["artifact_id"], "layout": "image_card", "narration_start_sec": 10,
             "narration_end_sec": 20, "purpose": "官方细节图"},
            {"asset_request": request(gap=original != "ai_no_gap"), "layout": "image_card", "narration_start_sec": 20,
             "narration_end_sec": 30, "purpose": "现有素材没有的原理示意"},
        ])
    if original in {"all_ai", "ai_no_gap"}:
        with pytest.raises(TaskValidationError) as error:
            complete(service, task, token, result)
        assert any("优先直用" in row["message"] or row["path"][-1] == "source_gap_reason" for row in error.value.errors)
    else:
        assert complete(service, task, token, result).status == TaskStatus.COMPLETED


@pytest.mark.parametrize("damage", ["bytes", "path", "revision", "receipt", "unlisted", "result_hash"])
def test_footage_source_images_fail_closed_before_voice_when_evidence_changes(footage_plan, monkeypatch, damage):
    from glance_core.models import ContentRevision
    subject, research, _, rows, approve = footage_plan
    session, _, content, assets = subject
    artifact = session.get(Artifact, rows[0]["artifact_id"])
    receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                   AuditEvent.action == "source_media_images_acquired")).one()
    if damage == "bytes":
        (Path(content.content_dir) / artifact.relative_path).write_bytes(b"changed")
    elif damage == "path":
        artifact.relative_path = "../outside.png"
        metadata = deepcopy(receipt.metadata_json)
        metadata["images"][0]["relative_path"] = artifact.relative_path
        receipt.metadata_json = metadata
        session.add_all([artifact, receipt])
    elif damage == "revision":
        revision = ContentRevision(content_id=content.id, subject_sha256="b" * 64)
        session.add(revision)
        session.commit()
        artifact.revision_id = revision.id
        session.add(artifact)
    elif damage == "receipt":
        session.delete(receipt)
    elif damage == "unlisted":
        metadata = deepcopy(receipt.metadata_json)
        metadata["images"][0]["artifact_id"] = assets[0].id
        receipt.metadata_json = metadata
        session.add(receipt)
    else:
        research.result_data = {**research.result_data, "recommended_verdict": "changed after acceptance"}
        session.add(research)
    session.commit()
    monkeypatch.setattr(pipeline.MediaExecutor, "synthesize_voice", lambda *args, **kwargs: pytest.fail("evidence failure must precede TTS"))
    with pytest.raises(TaskConflictError, match="SOURCE_MEDIA_"):
        approve()


def test_footage_research_repair_preserves_explicit_source_priority_without_upgrading_legacy(footage_subject):
    session, settings, content, _ = footage_subject
    flow = WorkflowService(session, data_dir=settings.data_dir, submission_root=settings.agent_submission_dir, lease_minutes=45)
    for enabled in (False, True):
        inputs = {"source_media_priority_version": 1} if enabled else {}
        target = AgentTask(task_type=TaskType.CONTENT_RESEARCH, prompt_name="content_research", content_id=content.id,
                           instructions="Frozen fixture", input_data=inputs, input_hash=stable_hash(inputs))
        frozen = deepcopy(target.input_data), target.input_hash
        repair = flow._research_repair_input(content, target)
        assert (repair.get("source_media_priority_version") == 1) is enabled
        assert (target.input_data, target.input_hash) == frozen


def test_new_footage_prepare_explicitly_issues_source_media_contract(footage_subject, monkeypatch):
    session, settings, content, _ = footage_subject
    candidate = session.get(Candidate, content.candidate_id)
    candidate.rights_status = RightsStatus.SELF_OWNED
    session.add(candidate)
    session.commit()
    root = Path(content.content_dir)
    source = root / "source/original.mp4"
    source.parent.mkdir()
    source.write_bytes(b"original video fixture")
    transcript = root / "transcript/transcript.json"
    transcript.parent.mkdir()
    transcript.write_text('{"segments": []}', encoding="utf-8")
    scenes_path = root / "scenes/scenes.json"
    scenes_path.parent.mkdir()
    scenes = [{"scene_id": "s1", "start_sec": 0, "end_sec": 30, "thumbnail": "scenes/s1.jpg"}]
    scenes_path.write_text(json.dumps({"scenes": scenes}), encoding="utf-8")
    monkeypatch.setattr(pipeline, "_require_media_tools", lambda *args: None)
    monkeypatch.setattr(pipeline.MediaExecutor, "acquire", lambda *a, **k: source)
    monkeypatch.setattr(pipeline.MediaExecutor, "ensure_transcript", lambda *a, **k: transcript)
    monkeypatch.setattr(pipeline.MediaExecutor, "analyze_scenes", lambda *a, **k: (scenes_path, scenes))
    pipeline.prepare_content(session.get_bind(), settings, content.id)
    research = session.exec(select(AgentTask).where(AgentTask.task_type == TaskType.CONTENT_RESEARCH)).one()
    assert research.input_data["production_mode"] == "footage"
    assert research.input_data["source_media_priority_version"] == 1
    assert session.exec(select(Artifact).where(Artifact.artifact_type == "source_video")).one().relative_path == "source/original.mp4"


@pytest.fixture
def approved_footage_plan(footage_plan):
    subject, research, script, rows, approve = footage_plan
    session, settings, _, _ = subject
    plan = approve()
    service = AgentTaskService(session, settings.agent_submission_dir)
    task, token = service.claim(agent_id="fixture-worker", role_id=primary_role_for(TaskType.VISUAL_PLAN),
                                capabilities={TaskType.VISUAL_PLAN}, task_id=plan.id)
    result = {"segments": [
        {"scene_id": "s1", "layout": "center_crop", "narration_start_sec": 0, "narration_end_sec": 15,
         "source_start_sec": 0, "source_end_sec": 15, "purpose": "原片"},
        {"asset_id": rows[0]["artifact_id"], "layout": "image_card", "narration_start_sec": 15,
         "narration_end_sec": 30, "purpose": "官方图"},
    ]}
    complete(service, task, token, result)
    service, qc, token = issue(subject, TaskType.VISUAL_PLAN_QC,
        {"upstream_task_ids": {"visual_plan": plan.id, "script": script.id}})
    complete(service, qc, token, {"passed": True, "issues": []})
    return subject, research, rows, plan


def test_footage_pipeline_passes_official_images_and_preserves_existing_renderer_assets(approved_footage_plan, monkeypatch):
    subject, _, rows, _ = approved_footage_plan
    session, settings, content, assets = subject
    captured = {}

    class RenderCaptured(RuntimeError):
        pass

    def capture(self, **kwargs):
        captured.update(kwargs)
        raise RenderCaptured("Stop at the renderer boundary; no simulated final QC")

    monkeypatch.setattr(pipeline.MediaExecutor, "align_subtitles", lambda *a, **k: {})
    monkeypatch.setattr(pipeline.MediaExecutor, "render", capture)
    with pytest.raises(RenderCaptured):
        pipeline.render_content(session.get_bind(), settings, content.id)
    assert captured["video_path"] == Path(content.content_dir) / "source/original.mp4"
    assert captured["assets"][rows[0]["artifact_id"]] == Path(content.content_dir) / rows[0]["relative_path"]
    assert captured["assets"][assets[1].id] == Path(content.content_dir) / assets[1].relative_path
    assert captured["generated_image_ids"] == {assets[1].id}
    assert [segment.get("scene_id") for segment in captured["plan_data"]["segments"]] == ["s1", None]


@pytest.mark.parametrize("damage", ["bytes", "path", "hash", "receipt", "receipt_id"])
def test_footage_render_rechecks_source_images_after_visual_qc(approved_footage_plan, monkeypatch, damage):
    subject, research, rows, _ = approved_footage_plan
    session, settings, content, _ = subject
    artifact = session.get(Artifact, rows[0]["artifact_id"])
    receipt = session.exec(select(AuditEvent).where(AuditEvent.entity_id == research.id,
                                                   AuditEvent.action == "source_media_images_acquired")).one()
    if damage == "bytes":
        (Path(content.content_dir) / artifact.relative_path).write_bytes(b"changed after QC")
    elif damage == "path":
        artifact.relative_path = "../changed.png"
        session.add(artifact)
    elif damage == "hash":
        artifact.sha256 = "b" * 64
        session.add(artifact)
    elif damage == "receipt":
        session.delete(receipt)
    else:
        replacement = Artifact(content_id=content.id, revision_id=artifact.revision_id, artifact_type="source_image",
            relative_path=artifact.relative_path, sha256=artifact.sha256, size_bytes=artifact.size_bytes,
            mime_type=artifact.mime_type, producer_stage="fixture")
        session.add(replacement)
        session.commit()
        metadata = deepcopy(receipt.metadata_json)
        metadata["images"][0]["artifact_id"] = replacement.id
        receipt.metadata_json = metadata
        session.add(receipt)
    session.commit()
    monkeypatch.setattr(pipeline.MediaExecutor, "align_subtitles", lambda *a, **k: pytest.fail("changed source must precede ASR"))
    monkeypatch.setattr(pipeline.MediaExecutor, "render", lambda *a, **k: pytest.fail("changed source must precede render"))
    with pytest.raises(pipeline.NonRetryableMediaError, match="Source media changed after visual QC"):
        pipeline.render_content(session.get_bind(), settings, content.id)
