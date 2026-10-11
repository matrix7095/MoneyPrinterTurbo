"""Deterministic binding of a v2 Agent acceptance to its exact current materials."""

import hashlib
from copy import deepcopy
from math import isfinite
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlmodel import Session, select

from glance_core.models import (
    AgentSubmission,
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    CandidateStatus,
    Content,
    ContentStatus,
    ProductionMode,
    RightsStatus,
    Source,
    TaskStatus,
    TaskType,
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def public_source_url(value):
    if not isinstance(value, str):
        return ""
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""
        secrets = ("token", "signature", "credential", "auth", "cookie", "x-amz")
        if any(secret in unquote(parsed.fragment).lower() for secret in secrets) or any(
            any(secret in key.lower() for secret in secrets) for key, _ in parse_qsl(parsed.query)
        ):
            return ""
    except ValueError:
        return ""
    return value


def candidate_source_context(session: Session, candidate: Candidate) -> dict:
    """Finite registration only; never source config, credentials or a license decision."""
    context = {
        "candidate_id": candidate.id,
        "source_type": candidate.source_type,
        "source_account": candidate.source_account,
        "source_url": public_source_url(candidate.source_url),
        "video_url": public_source_url(candidate.video_url),
        "video_source_page_url": public_source_url(candidate.video_source_page_url),
        "rights_status": candidate.rights_status.value,
        "rights_note": candidate.rights_note,
    }
    source = session.get(Source, candidate.source_id)
    if source:
        context["source_registration"] = {
            "source_type": source.source_type.value,
            "url": public_source_url(source.url),
            "default_rights_status": source.default_rights_status.value,
        }
    return context


def _unchanged_material_task(session: Session, task: AgentTask, *, owned: bool = False, consumed: bool = True) -> None:
    from glance_core.task_service import stable_hash

    if stable_hash(task.input_data) != task.input_hash:
        raise ValueError("visual material task input changed")
    submissions = session.exec(select(AgentSubmission).where(
        AgentSubmission.task_id == task.id, AgentSubmission.attempt == task.attempt,
        AgentSubmission.validation_status == "ACCEPTED",
    )).all()
    receipts = session.exec(select(AuditEvent).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id == task.id,
        AuditEvent.action.in_(["completed", "mpt_agent_consumed", "mpt_task_owned"]),
    )).all()
    completed = [event for event in receipts if event.action == "completed"]
    markers = [event for event in receipts if event.action == "mpt_agent_consumed"]
    if owned and (len(submissions) != 1 or not any(event.action == "mpt_task_owned" for event in receipts)
                  or len(completed) != 1 or completed[0].actor_type != "agent"
                  or completed[0].actor_id != submissions[0].agent_id
                  or submissions[0].agent_id != task.assigned_agent_id
                  or completed[0].metadata_json.get("result_hash") != submissions[0].result_hash
                  or (consumed and not markers)):
        raise ValueError("visual material accepted/consumed proof missing")
    if (submissions and (len(submissions) != 1
                         or submissions[0].result_hash != stable_hash(task.result_data))) or any(
        event.metadata_json.get("input_hash" if event.action == "mpt_task_owned" else "result_hash")
        != (task.input_hash if event.action == "mpt_task_owned" else stable_hash(task.result_data))
        for event in receipts if event.action != "completed"
    ):
        raise ValueError("visual material task accepted result/ownership changed")


def prepared_visual_plan(session: Session, plan: AgentTask) -> dict:
    """Use only the current PlanQC's frozen server preparation; keep Agent bytes intact."""
    from glance_core.task_service import stable_hash

    _unchanged_material_task(session, plan)
    current = session.exec(select(AgentTask).where(
        AgentTask.content_id == plan.content_id, AgentTask.task_type == TaskType.VISUAL_PLAN,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    if current is None or current.id != plan.id or plan.status != TaskStatus.COMPLETED:
        raise ValueError("visual material plan missing/stale")
    qc = session.exec(select(AgentTask).where(
        AgentTask.content_id == plan.content_id, AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    result = plan.result_data or {}
    if qc is not None and "prepared_visual_plan" in qc.input_data:
        _unchanged_material_task(session, qc)
        if (qc.status != TaskStatus.COMPLETED or (qc.result_data or {}).get("passed") is not True
                or qc.input_data.get("upstream_task_ids", {}).get("visual_plan") != plan.id
                or qc.input_data.get("visual_plan_result_sha256") != stable_hash(result)
                or not isinstance(qc.input_data["prepared_visual_plan"], dict)):
            raise ValueError("prepared visual plan approval missing/stale")
        result = qc.input_data["prepared_visual_plan"]
    return deepcopy(result)


def _owned_visual_chain(session: Session, plan: AgentTask) -> bool:
    # Downstream publication ownership must not reclassify a portable visual graph.
    rows = session.exec(select(AgentTask).where(AgentTask.content_id == plan.content_id)).all()
    ids = [row.id for row in rows if row.id == plan.id or (
        row.task_type in {TaskType.VISUAL_PLAN_QC, TaskType.ASSET_GENERATE_IMAGE, TaskType.ASSET_QC}
        and row.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id
        and row.input_data.get("asset_purpose") != "cover")]
    return session.exec(select(AuditEvent.id).where(
        AuditEvent.entity_type == "agent_task", AuditEvent.entity_id.in_(ids),
        AuditEvent.action.in_(["mpt_task_owned", "mpt_agent_consumed"]),
    )).first() is not None


def _visual_parent_chain(session: Session, plan: AgentTask, *, owned: bool) -> None:
    def current(kind, ref=None):
        row = session.exec(select(AgentTask).where(
            AgentTask.content_id == plan.content_id, AgentTask.task_type == kind,
            AgentTask.status != TaskStatus.CANCELLED,
        ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
        if row is None or row.status != TaskStatus.COMPLETED or (ref is not None and row.id != ref):
            raise ValueError(f"visual material {kind.value} lineage missing/stale")
        _unchanged_material_task(session, row, owned=owned)
        return row

    script_id = plan.input_data.get("upstream_task_ids", {}).get("script")
    if not script_id:
        raise ValueError("visual material script lineage missing")
    script = current(TaskType.SHORT_SCRIPT, script_id)
    research_id = script.input_data.get("upstream_task_ids", {}).get("research")
    if not research_id:
        raise ValueError("visual material research lineage missing")
    current(TaskType.CONTENT_RESEARCH, research_id)
    qc = current(TaskType.SCRIPT_QC)
    if (qc.result_data or {}).get("passed") is not True or qc.input_data.get("upstream_task_ids", {}).get("script") != script.id or (
            qc.input_data.get("upstream_task_ids", {}).get("research", research_id) != research_id):
        raise ValueError("visual material script QC lineage missing/stale")


def resolved_visual_plan(session: Session, plan: AgentTask) -> dict:
    """Derive requested images from exact current generation and passed QC, without writes."""
    result = prepared_visual_plan(session, plan)
    owned = _owned_visual_chain(session, plan)
    if owned:
        _visual_parent_chain(session, plan, owned=True)
    _unchanged_material_task(session, plan, owned=owned)
    plan_qc = session.exec(select(AgentTask).where(
        AgentTask.content_id == plan.content_id, AgentTask.task_type == TaskType.VISUAL_PLAN_QC,
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    consuming = session.info.get("consuming_agent_task_id")
    if plan_qc is None and owned:
        raise ValueError("Current VISUAL_PLAN_QC missing")
    if plan_qc is not None:
        _unchanged_material_task(session, plan_qc, owned=owned, consumed=plan_qc.id != consuming)
        if (plan_qc.status != TaskStatus.COMPLETED or (plan_qc.result_data or {}).get("passed") is not True
                or plan_qc.input_data.get("upstream_task_ids", {}).get("visual_plan") != plan.id):
            raise ValueError("Current VISUAL_PLAN_QC must be completed and passed")
    content = session.get(Content, plan.content_id)
    tasks = session.exec(select(AgentTask).where(
        AgentTask.content_id == plan.content_id,
        AgentTask.task_type.in_([TaskType.ASSET_GENERATE_IMAGE, TaskType.ASSET_QC]),
        AgentTask.status != TaskStatus.CANCELLED,
    ).order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).all()
    reused = {}
    for index, segment in enumerate(result.get("segments", [])):
        request = segment.get("asset_request")
        if not request:
            artifact = session.get(Artifact, segment["asset_id"]) if segment.get("asset_id") else None
            if artifact is not None and artifact.artifact_type == "agent_generated_image":
                material = generated_image_material(session, content, artifact.id)
                reused[index] = material
                if owned:
                    _unchanged_material_task(session, session.get(AgentTask, material["generation_task_id"]), owned=True)
                    _unchanged_material_task(session, session.get(AgentTask, material["generation_plan_id"]), owned=True)
                if material["generation_plan_id"] != plan.id and artifact.id not in plan.input_data.get("allowed_asset_ids", []):
                    raise ValueError("visual material reuse declaration missing")
            continue
        generation = next((task for task in tasks if task.task_type == TaskType.ASSET_GENERATE_IMAGE
                           and task.input_data.get("asset_purpose") != "cover"
                           and task.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id
                           and any(entry.get("segment_index") == index for entry in task.input_data.get("requests", []))), None)
        if generation is None or generation.status != TaskStatus.COMPLETED:
            raise ValueError(f"Current plan image generation is incomplete: {index}")
        _unchanged_material_task(session, generation, owned=owned)
        requests = [entry for entry in generation.input_data.get("requests", []) if entry.get("segment_index") == index]
        images = [image for image in (generation.result_data or {}).get("images", []) if image.get("segment_index") == index]
        if len(requests) != 1 or requests[0].get("asset_request") != request or len(images) != 1:
            raise ValueError("visual material generation request/coverage changed")
        image = images[0]
        qc = next((task for task in tasks if task.task_type == TaskType.ASSET_QC
                   and task.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id
                   and (task.input_data.get("full_generated_assets") is True
                        or task.input_data.get("upstream_task_ids", {}).get("asset_generate_image") == generation.id)), None)
        if qc is None or qc.status != TaskStatus.COMPLETED or (qc.result_data or {}).get("passed") is not True:
            raise ValueError("Current ASSET_QC must be completed and passed")
        _unchanged_material_task(session, qc, owned=owned, consumed=qc.id != consuming)
        checked = [entry for entry in qc.input_data.get("assets", []) if entry.get("segment_index") == index]
        if (len(checked) != 1 or checked[0].get("artifact_id") != image.get("artifact_id")
                or checked[0].get("request") != request or checked[0].get("generation") != image):
            raise ValueError("visual material asset QC coverage changed")
        artifact = session.get(Artifact, image.get("artifact_id"))
        if (content is None or artifact is None or artifact.content_id != content.id
                or artifact.artifact_type != "agent_generated_image" or not artifact.mime_type.startswith("image/")
                or artifact.producer_stage != generation.id):
            raise ValueError("visual material artifact lineage changed")
        root = Path(content.content_dir).resolve()
        path = (root / artifact.relative_path).resolve()
        if (not path.is_relative_to(root) or not path.is_file()
                or path.stat().st_size != artifact.size_bytes or _file_sha256(path) != artifact.sha256):
            raise ValueError("visual material artifact bytes changed")
        segment.pop("asset_request")
        segment["asset_id"] = artifact.id
    # Full-composition approval must also cover reused assets and reject extras/duplicates.
    full = next((task for task in tasks if task.task_type == TaskType.ASSET_QC
                 and task.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id
                 and task.input_data.get("full_generated_assets") is True), None)
    if owned and reused and full is None:
        raise ValueError("Full current asset QC required for reused images")
    if full is not None:
        _unchanged_material_task(session, full, owned=owned, consumed=full.id != consuming)
        expected = {index: segment["asset_id"] for index, segment in enumerate(result.get("segments", []))
                    if segment.get("asset_id")}
        entries = full.input_data.get("assets", [])
        if (full.status != TaskStatus.COMPLETED or (full.result_data or {}).get("passed") is not True
                or len(entries) != len(expected)
                or {entry.get("segment_index"): entry.get("artifact_id") for entry in entries} != expected):
            raise ValueError("Full current asset QC materials/coverage changed")
        for index, material in reused.items():
            entry = next(entry for entry in entries if entry.get("segment_index") == index)
            if any(entry.get(key) != value for key, value in material.items()):
                raise ValueError("Full current asset QC reuse lineage changed")
    return result


def source_context(
    session: Session, content_id: str, refs: dict, *, include_references: bool = False
) -> dict:
    """Registered provenance, not a license decision; only exact completed lineage.

    Source-video artifacts are content-owned acquisition records, NOT evidence of
    permission. Scene/source association comes from the declared scene-task input.
    Never expose artifact paths, source config, or signed download credentials.
    """
    from glance_core.task_service import stable_hash

    gaps = []

    public_url = public_source_url

    def completed(ref, kind):
        row = session.get(AgentTask, ref) if isinstance(ref, str) else None
        if (
            row is None
            or row.content_id != content_id
            or row.task_type != kind
            or row.status != TaskStatus.COMPLETED
            or stable_hash(row.input_data) != row.input_hash
        ):
            gaps.append(f"{kind.value}: exact completed input lineage unavailable")
            return None
        return row

    content = session.get(Content, content_id)
    candidate = session.get(Candidate, content.candidate_id) if content else None
    if candidate is None:
        return {"version": 1, "evidence_gaps": ["candidate: source registration unavailable"]}
    registered = {
        "candidate_id": candidate.id,
        "source_id": candidate.source_id,
        "source_type": candidate.source_type,
        "source_account": candidate.source_account,
        "source_url": public_url(candidate.source_url),
        "video_url": public_url(candidate.video_url),
        "video_source_page_url": public_url(candidate.video_source_page_url),
        "rights_status": candidate.rights_status.value,
        "rights_note": candidate.rights_note,
    }
    context = {"version": 1, "registration": registered, "scenes": [], "artifacts": []}
    source = session.get(Source, candidate.source_id)
    if source:
        context["source_registration"] = {
            "source_id": source.id,
            "source_type": source.source_type.value,
            "url": public_url(source.url),
            "default_rights_status": source.default_rights_status.value,
        }
    script = completed(refs.get("script"), TaskType.SHORT_SCRIPT)
    plan = completed(refs["visual_plan"], TaskType.VISUAL_PLAN) if "visual_plan" in refs else None
    if plan and (
        script is None or plan.input_data.get("upstream_task_ids", {}).get("script") != script.id
    ):
        gaps.append("visual_plan: script/source lineage mismatch")
        plan = None
    material_plan = plan.result_data if plan else {}
    if plan:
        try:
            material_plan = resolved_visual_plan(session, plan)
        except ValueError as exc:
            gaps.append(str(exc))
    if script:
        upstream = script.input_data.get("upstream_task_ids", {})
        research = completed(upstream.get("research"), TaskType.CONTENT_RESEARCH)
        if refs.get("research") is not None and (
            research is None or refs["research"] not in (research.id, [research.id])
        ):
            gaps.append("research: declared QC/script source references conflict")
            research = None
        if research:
            context["research_task_id"] = research.id
            context["source_images"] = [
                {
                    key: (image.get(key, "") if key == "caption" else public_url(image.get(key)))
                    for key in ("url", "caption", "source_page")
                }
                for image in research.result_data.get("source_images", [])
                if isinstance(image, dict) and public_url(image.get("url"))
            ]
        scene_refs = upstream.get("scene_analysis", [])
        scene_refs = [scene_refs] if isinstance(scene_refs, str) else scene_refs
        if (
            not isinstance(scene_refs, list)
            or len(scene_refs) > 60
            or any(not isinstance(x, str) for x in scene_refs)
        ):
            gaps.append("scene_analysis: invalid declared batch references")
            scene_refs = []
        if not scene_refs and content.production_mode.value != "image_only":
            gaps.append("scene_analysis: no declared source lineage")
        seen = set()
        for ref in dict.fromkeys(scene_refs) if all(isinstance(x, str) for x in scene_refs) else []:
            scene_task = completed(ref, TaskType.SCENE_ANALYZE)
            if scene_task is None:
                continue
            declared = scene_task.input_data.get("candidate", {}).get("source_url")
            if not public_url(declared) or public_url(declared) != registered["source_url"]:
                gaps.append("scene_analysis: source registration mismatch")
                continue
            for scene in scene_task.input_data.get("scenes", []):
                scene_id = scene.get("scene_id") if isinstance(scene, dict) else None
                times = (
                    [scene.get(key) for key in ("start_sec", "end_sec", "duration_sec")]
                    if scene_id
                    else []
                )
                if (
                    not scene_id
                    or scene_id in seen
                    or len(times) != 3
                    or any(type(t) not in (int, float) or not isfinite(t) or t < 0 for t in times)
                    or times[1] <= times[0]
                    or abs(times[1] - times[0] - times[2]) > 0.05
                ):
                    gaps.append("scene_analysis: missing/ambiguous source scene or invalid timing")
                    continue
                seen.add(scene_id)
                context["scenes"].append(
                    {
                        "scene_id": scene_id,
                        "scene_analysis_task_id": scene_task.id,
                        "scene_input_sha256": scene_task.input_hash,
                        "scene_result_sha256": stable_hash(scene_task.result_data),
                        "source_url": public_url(declared),
                        "source_id": candidate.source_id,
                        **dict(zip(("start_sec", "end_sec", "duration_sec"), times, strict=True)),
                    }
                )
        if plan:
            used = {
                s.get("scene_id")
                for s in material_plan.get("segments", [])
                if isinstance(s, dict) and s.get("scene_id")
            }
            if not used.issubset(seen):
                gaps.append("visual_plan: used scene has no exact source mapping")
            context["scenes"] = [scene for scene in context["scenes"] if scene["scene_id"] in used]
    assets = {
        s.get("asset_id")
        for s in material_plan.get("segments", [])
        if isinstance(s, dict) and s.get("asset_id")
    }
    references = []
    if include_references and plan:
        allowed = plan.input_data.get("allowed_reference_artifact_ids", [])
        for index, segment in enumerate(prepared_visual_plan(session, plan).get("segments", [])):
            request = segment.get("asset_request") if isinstance(segment, dict) else None
            if not isinstance(request, dict) or not request.get("reference_artifact_id"):
                continue
            ref = request["reference_artifact_id"]
            if not isinstance(ref, str) or not isinstance(allowed, list) or ref not in allowed:
                gaps.append(
                    "reference: requested artifact is not in exact plan reference whitelist"
                )
                continue
            references.append({"segment_index": index, "reference_artifact_id": ref})
    reference_ids = {r["reference_artifact_id"] for r in references}
    required_images = assets | reference_ids
    artifacts = session.exec(select(Artifact).where(Artifact.content_id == content_id)).all()
    # render_content passes this canonical acquired file to MediaExecutor. Other
    # content-owned source videos are NOT necessarily used, so must not be frozen.
    source_videos = [
        a
        for a in artifacts
        if a.artifact_type == "source_video"
        and a.relative_path.replace("\\", "/") == "source/original.mp4"
    ]
    if context["scenes"] and len(source_videos) != 1:
        gaps.append("artifact: acquired render source missing/ambiguous")
    source_id = source_videos[0].id if context["scenes"] and len(source_videos) == 1 else None
    found_assets = set()
    for artifact in artifacts:
        if artifact.id != source_id and artifact.id not in required_images:
            continue
        if artifact.id in required_images and artifact.artifact_type not in {
            "source_image",
            "agent_generated_image",
        }:
            gaps.append("artifact: used image asset type unavailable for this renderer")
            continue
        root = Path(content.content_dir).resolve()
        path = (root / artifact.relative_path).resolve()
        if (
            not path.is_relative_to(root)
            or not path.is_file()
            or path.stat().st_size != artifact.size_bytes
        ):
            gaps.append("artifact: registered source material unavailable")
            continue
        if _file_sha256(path) != artifact.sha256:
            gaps.append("artifact: actual source material hash changed")
            continue
        found_assets.add(artifact.id)
        context["artifacts"].append(
            {
                "artifact_id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "registered_sha256": artifact.sha256,
                "size_bytes": artifact.size_bytes,
            }
        )
    if not assets.issubset(found_assets):
        gaps.append("artifact: used asset missing/foreign/changed")
    if not reference_ids.issubset(found_assets):
        gaps.append("reference: requested asset missing/foreign/changed")
    for scene in context["scenes"]:
        if source_id in found_assets:
            scene["source_video_artifact_id"] = source_id
    if context["scenes"] and not any(
        a["artifact_type"] == "source_video" for a in context["artifacts"]
    ):
        gaps.append("artifact: no registered acquired source video")
    context["artifacts"].sort(key=lambda item: item["artifact_id"])
    if candidate.rights_status == RightsStatus.RESEARCH_ONLY:
        try:
            context["original_media"] = _original_image_lineage(session, content, plan, context)
        except ValueError as exc:
            gaps.append(str(exc))
    if include_references:
        context["reference_uses"] = [
            r for r in references if r["reference_artifact_id"] in found_assets
        ]
        proven_originals = {
            s["artifact_id"] for s in context.get("original_media", {}).get("segments", [])
        }
        if any(
            a["artifact_type"] == "agent_generated_image"
            and a["artifact_id"] in assets
            and a["artifact_id"] not in proven_originals
            for a in context["artifacts"]
        ):
            gaps.append(
                "reference: resolved generation reference lineage unavailable in this context"
            )
    context["evidence_gaps"] = list(dict.fromkeys(gaps))
    return context


def completed_image_origin(session: Session, task: AgentTask, *, bind_result: bool = True) -> bool:
    """Cancelled origin is usable only after an accepted completion and system invalidation."""
    from glance_core.task_service import stable_hash

    if task.status == TaskStatus.COMPLETED:
        return True
    if task.status != TaskStatus.CANCELLED or task.completed_at is None:
        return False
    submissions = session.exec(
        select(AgentSubmission).where(
            AgentSubmission.task_id == task.id,
            AgentSubmission.attempt == task.attempt,
            AgentSubmission.validation_status == "ACCEPTED",
        )
    ).all()
    if len(submissions) != 1:
        return False
    accepted = submissions[0]
    if bind_result and accepted.result_hash != stable_hash(task.result_data):
        return False
    events = session.exec(
        select(AuditEvent).where(
            AuditEvent.entity_type == "agent_task",
            AuditEvent.entity_id == task.id,
        )
    ).all()
    if any(e.action == "cancelled" and e.actor_type != "system" for e in events):
        return False
    completed = [
        e
        for e in events
        if e.action == "completed"
        and e.actor_type == "agent"
        and e.actor_id == accepted.agent_id
        and e.metadata_json.get("result_hash") == accepted.result_hash
    ]
    return len(completed) == 1 and any(
        e.action == "cancelled"
        and e.actor_type == "system"
        and e.created_at >= completed[0].created_at
        for e in events
    )


def generated_image_material(session: Session, content: Content, artifact_id: str) -> dict:
    """Exact bytes and generation identity; NOT quality approval or a license."""
    from glance_core.task_service import stable_hash

    artifact = session.get(Artifact, artifact_id)
    generation = session.get(AgentTask, artifact.producer_stage) if artifact else None
    if (
        artifact is None
        or artifact.content_id != content.id
        or artifact.artifact_type != "agent_generated_image"
        or generation is None
        or generation.content_id != content.id
        or generation.task_type != TaskType.ASSET_GENERATE_IMAGE
        or not completed_image_origin(session, generation)
        or stable_hash(generation.input_data) != generation.input_hash
        or set(generation.input_data.get("upstream_task_ids", {})) != {"visual_plan"}
    ):
        raise ValueError("generated image origin unavailable")
    plan = session.get(AgentTask, generation.input_data["upstream_task_ids"]["visual_plan"])
    if (
        plan is None
        or plan.content_id != content.id
        or plan.task_type != TaskType.VISUAL_PLAN
        or not completed_image_origin(session, plan, bind_result=False)
        or stable_hash(plan.input_data) != plan.input_hash
    ):
        raise ValueError("generated image plan unavailable")
    if _owned_visual_chain(session, plan):
        _unchanged_material_task(session, plan, owned=True)
        _unchanged_material_task(session, generation, owned=True,
                                 consumed=generation.id != session.info.get("consuming_agent_task_id"))
    images = [
        i
        for i in generation.result_data.get("images", [])
        if isinstance(i, dict) and i.get("artifact_id") == artifact.id
    ]
    index = images[0].get("segment_index") if len(images) == 1 else None
    requests = [
        r
        for r in generation.input_data.get("requests", [])
        if isinstance(r, dict) and r.get("segment_index") == index
    ]
    if (
        type(index) is not int
        or index < 0
        or len(requests) != 1
        or sum(
            isinstance(i, dict) and i.get("segment_index") == index
            for i in generation.result_data.get("images", [])
        )
        != 1
        or not isinstance(requests[0].get("asset_request"), dict)
        or requests[0]["asset_request"].get("reference_artifact_id")
    ):
        raise ValueError("generated image request missing/ambiguous/reference")
    root = Path(content.content_dir).resolve()
    path = (root / artifact.relative_path).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.stat().st_size != artifact.size_bytes
        or _file_sha256(path) != artifact.sha256
    ):
        raise ValueError("generated image bytes unavailable/changed")
    return {
        "artifact_id": artifact.id,
        "artifact_url": f"/media/artifacts/{artifact.id}",
        "generation_task_id": generation.id,
        "generation_plan_id": plan.id,
        "generation_segment_index": index,
        "generation_input_sha256": generation.input_hash,
        "generation_result_sha256": stable_hash(generation.result_data),
        "artifact_sha256": artifact.sha256,
        "request": requests[0]["asset_request"],
        "generation": images[0],
    }


def _original_image_lineage(
    session: Session, content: Content, plan: AgentTask | None, context: dict
):
    """Prove the used images' origin, not permission or semantic quality."""
    from glance_core.task_service import stable_hash

    if (
        content.production_mode != ProductionMode.IMAGE_ONLY
        or plan is None
        or context["scenes"]
        or plan.input_data.get("production_mode") != ProductionMode.IMAGE_ONLY.value
        or plan.input_data.get("scenes")
        or plan.input_data.get("source_images")
        or plan.input_data.get("allowed_reference_artifact_ids")
    ):
        raise ValueError("original_media: image-only plan required")
    segments = resolved_visual_plan(session, plan).get("segments", [])
    if not segments or any(
        not isinstance(segment, dict)
        or segment.get("layout") != "image_card"
        or segment.get("scene_id")
        or segment.get("asset_request")
        or not segment.get("asset_id")
        for segment in segments
    ):
        raise ValueError("original_media: unresolved or footage material")
    materials = {a["artifact_id"]: a for a in context["artifacts"]}

    def completed_origin(task: AgentTask, *, bind_result: bool = True) -> bool:
        return completed_image_origin(session, task, bind_result=bind_result)

    asset_checks = session.exec(
        select(AgentTask)
        .where(
            AgentTask.content_id == content.id,
            AgentTask.task_type == TaskType.ASSET_QC,
            AgentTask.status.in_([TaskStatus.COMPLETED, TaskStatus.CANCELLED]),
        )
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    ).all()
    evidence = []
    for index, segment in enumerate(segments):
        artifact = session.get(Artifact, segment["asset_id"])
        generation = session.get(AgentTask, artifact.producer_stage) if artifact else None
        if (
            artifact is None
            or artifact.content_id != content.id
            or artifact.artifact_type != "agent_generated_image"
            or artifact.id not in materials
            or generation is None
            or generation.content_id != content.id
            or generation.task_type != TaskType.ASSET_GENERATE_IMAGE
            or not completed_origin(generation)
            or stable_hash(generation.input_data) != generation.input_hash
            or set(generation.input_data.get("upstream_task_ids", {})) != {"visual_plan"}
        ):
            raise ValueError("original_media: exact generation/artifact lineage unavailable")
        images = [
            i
            for i in generation.result_data.get("images", [])
            if isinstance(i, dict) and i.get("artifact_id") == artifact.id
        ]
        original_index = images[0].get("segment_index") if len(images) == 1 else None
        requests = [
            r
            for r in generation.input_data.get("requests", [])
            if isinstance(r, dict) and r.get("segment_index") == original_index
        ]
        if (
            type(original_index) is not int
            or original_index < 0
            or len(requests) != 1
            or len(images) != 1
            or sum(
                isinstance(i, dict) and i.get("segment_index") == original_index
                for i in generation.result_data.get("images", [])
            )
            != 1
            or images[0].get("artifact_id") != artifact.id
            or not isinstance(requests[0].get("asset_request"), dict)
            or requests[0]["asset_request"].get("reference_artifact_id")
        ):
            raise ValueError("original_media: missing/ambiguous generation or reference image")
        original_plan = session.get(
            AgentTask, generation.input_data["upstream_task_ids"].get("visual_plan")
        )
        # Original plan results may legitimately contain executor asset backfills.
        # They are not used to approve the new composition: current PlanQC does that.
        if (
            original_plan is None
            or original_plan.content_id != content.id
            or original_plan.task_type != TaskType.VISUAL_PLAN
            or not completed_origin(original_plan, bind_result=False)
            or stable_hash(original_plan.input_data) != original_plan.input_hash
            or original_plan.input_data.get("production_mode") != "image_only"
            or original_plan.input_data.get("scenes")
            or original_plan.input_data.get("source_images")
            or original_plan.input_data.get("allowed_reference_artifact_ids")
            or (
                original_plan.id != plan.id
                and artifact.id not in plan.input_data.get("allowed_asset_ids", [])
            )
        ):
            raise ValueError("original_media: exact original plan/reuse declaration unavailable")
        qc = next(
            (
                q
                for q in asset_checks
                if q.input_data.get("upstream_task_ids", {})
                == {
                    "visual_plan": original_plan.id,
                    "asset_generate_image": generation.id,
                }
            ),
            None,
        )
        # A fresh full-composition QC may approve old candidates without upgrading
        # their failed original QC. Every current generated segment must be covered.
        current_qc = next(
            (
                q
                for q in asset_checks
                if q.input_data.get("upstream_task_ids", {}).get("visual_plan") == plan.id
                and q.input_data.get("full_generated_assets") is True
            ),
            None,
        )
        if current_qc is not None:
            expected = {i: s["asset_id"] for i, s in enumerate(segments)}
            entries = current_qc.input_data.get("assets", [])
            if (
                len(entries) != len(expected)
                or {a.get("segment_index"): a.get("artifact_id") for a in entries} != expected
            ):
                raise ValueError("original_media: full current asset QC coverage unavailable")
            material = generated_image_material(session, content, artifact.id)
            current_entry = next((a for a in entries if a.get("segment_index") == index), {})
            if any(current_entry.get(k) != v for k, v in material.items()):
                raise ValueError("original_media: current asset QC origin/hash mismatch")
            qc = current_qc
            original_index_for_qc = index
        else:
            original_index_for_qc = original_index
        checked = [
            a
            for a in (qc.input_data.get("assets", []) if qc else [])
            if isinstance(a, dict) and a.get("segment_index") == original_index_for_qc
        ]
        if (
            qc is None
            or not completed_origin(qc)
            or qc.result_data.get("passed") is not True
            or stable_hash(qc.input_data) != qc.input_hash
            or len(checked) != 1
            or checked[0].get("artifact_id") != artifact.id
            or checked[0].get("request") != requests[0]["asset_request"]
            or checked[0].get("generation") != images[0]
        ):
            raise ValueError("original_media: exact passed asset QC unavailable")
        evidence.append(
            {
                "segment_index": index,
                "artifact_id": artifact.id,
                "generation_task_id": generation.id,
                "generation_input_sha256": generation.input_hash,
                "generation_result_sha256": stable_hash(generation.result_data),
                "asset_qc_task_id": qc.id,
                "asset_qc_input_sha256": qc.input_hash,
                "asset_qc_result_sha256": stable_hash(qc.result_data),
            }
        )
        if original_plan.id != plan.id or original_index != index:
            evidence[-1].update(
                generation_plan_id=original_plan.id,
                generation_plan_input_sha256=original_plan.input_hash,
                generation_segment_index=original_index,
            )
    return {"method": "exact_generated_images_without_reference", "segments": evidence}


def is_handsfree_content(session: Session, content_id: str) -> bool:
    return (
        session.exec(
            select(AuditEvent.id).where(
                AuditEvent.action == "handsfree_candidate_selected",
                AuditEvent.entity_type == "content",
                AuditEvent.entity_id == content_id,
                AuditEvent.actor_type == "system",
                AuditEvent.actor_id == "handsfree-coordinator",
            )
        ).first()
        is not None
    )


def acceptance_origin(session: Session, content_id: str) -> str:
    if is_handsfree_content(session, content_id):
        return "handsfree"
    approved = session.exec(
        select(AuditEvent.id).where(
            AuditEvent.action == "manual_candidate_selected",
            AuditEvent.entity_type == "content",
            AuditEvent.entity_id == content_id,
            AuditEvent.actor_type == "human",
        )
    ).first()
    if approved is None:
        raise ValueError("acceptance content lacks explicit production ownership")
    return "manual"


def acceptance_subject(
    session: Session,
    content_id: str,
    refs: dict,
    *,
    source_context_version: int | None = None,
    audio_evidence: dict | None = None,
    _archived_revalidation: bool = False,
) -> dict[str, str]:
    from glance_core.task_service import stable_hash

    content = session.get(Content, content_id)
    acceptance_origin(session, content_id)
    if (content is None or content.business_status == ContentStatus.PUBLISHED
            or (content.business_status == ContentStatus.ARCHIVED and not _archived_revalidation)):
        raise ValueError("acceptance content missing or terminal")
    candidate = session.get(Candidate, content.candidate_id)
    if (
        candidate is None
        or candidate.status != CandidateStatus.SELECTED
        or (
            candidate.rights_status
            not in {
                RightsStatus.SELF_OWNED,
                RightsStatus.EXPLICIT_LICENSE,
                RightsStatus.OFFICIAL_REFERENCE,
            }
            and candidate.rights_status != RightsStatus.RESEARCH_ONLY
        )
    ):
        raise ValueError("acceptance candidate/rights unavailable")
    subject = {
        "content_id": content.id,
        "candidate_id": candidate.id,
        "candidate_sha256": stable_hash(
            {
                "id": candidate.id,
                "rights": candidate.rights_status,
                "rights_note": candidate.rights_note,
                "source_url": candidate.source_url,
                "video_url": candidate.video_url,
                "original_title": candidate.original_title,
            }
        ),
    }
    tasks = {}
    for key, kind in (("script", TaskType.SHORT_SCRIPT), ("visual_plan", TaskType.VISUAL_PLAN)):
        task = session.get(AgentTask, refs.get(key)) if isinstance(refs.get(key), str) else None
        latest = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content.id,
                AgentTask.task_type == kind,
                AgentTask.status == TaskStatus.COMPLETED,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        ).first()
        if task is None or task != latest or task.task_type != kind or not task.result_data:
            raise ValueError(f"acceptance {key} lineage missing/stale")
        if stable_hash(task.input_data) != task.input_hash:
            raise ValueError(f"acceptance {key} input changed")
        tasks[key] = task
        subject[f"{key}_task_id"] = task.id
        subject[f"{key}_sha256"] = stable_hash(task.result_data)
    if (
        tasks["visual_plan"].input_data.get("upstream_task_ids", {}).get("script")
        != tasks["script"].id
    ):
        raise ValueError("acceptance plan/script mismatch")
    if _owned_visual_chain(session, tasks["visual_plan"]):
        resolved_visual_plan(session, tasks["visual_plan"])
    research_id = tasks["script"].input_data.get("upstream_task_ids", {}).get("research")
    research = session.get(AgentTask, research_id) if isinstance(research_id, str) else None
    if (
        research is None
        or research.content_id != content.id
        or research.task_type != TaskType.CONTENT_RESEARCH
        or research.status != TaskStatus.COMPLETED
        or stable_hash(research.input_data) != research.input_hash
    ):
        raise ValueError("acceptance research lineage unavailable")
    subject["research_task_id"] = research.id
    subject["research_sha256"] = stable_hash(research.result_data)
    for key, kind, target in (
        ("script_qc", TaskType.SCRIPT_QC, "script"),
        ("plan_qc", TaskType.VISUAL_PLAN_QC, "visual_plan"),
    ):
        qc = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content.id,
                AgentTask.task_type == kind,
                AgentTask.status == TaskStatus.COMPLETED,
            )
            .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
        ).first()
        if (
            qc is None
            or qc.input_data.get("upstream_task_ids", {}).get(target) != tasks[target].id
            or not qc.result_data.get("passed")
            or stable_hash(qc.input_data) != qc.input_hash
        ):
            raise ValueError(f"acceptance {key} unavailable/stale/failed")
        subject[f"{key}_task_id"] = qc.id
        subject[f"{key}_sha256"] = stable_hash(qc.result_data)
    artifact = session.get(Artifact, refs.get("video_artifact_id"))
    latest_artifact = session.exec(
        select(Artifact)
        .where(
            Artifact.content_id == content.id,
            Artifact.artifact_type == "final_video",
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ).first()
    if artifact is None or artifact != latest_artifact or artifact.artifact_type != "final_video":
        raise ValueError("acceptance video missing/stale/foreign")
    root = Path(content.content_dir).resolve()
    path = (root / artifact.relative_path).resolve()
    if (
        not path.is_relative_to(root)
        or not path.is_file()
        or path.stat().st_size != artifact.size_bytes
    ):
        raise ValueError("acceptance video file missing/invalid")
    if _file_sha256(path) != artifact.sha256:
        raise ValueError("acceptance actual video hash changed")
    subject.update(video_artifact_id=artifact.id, video_sha256=artifact.sha256)
    context = None
    if candidate.rights_status == RightsStatus.RESEARCH_ONLY:
        context = source_context(session, content_id, refs)
        if (
            source_context_version != 1
            or context.get("evidence_gaps")
            or not context.get("original_media")
        ):
            raise ValueError("acceptance research-only original media unavailable")
    if source_context_version is not None:
        if type(source_context_version) is not int or source_context_version != 1:
            raise ValueError("SOURCE_CONTEXT_VERSION_UNSUPPORTED")
        if context is None:
            context = source_context(session, content_id, refs)
        subject["source_context_sha256"] = stable_hash(context)
    if audio_evidence is not None:
        if audio_evidence.get("video_sha256") != artifact.sha256:
            raise ValueError("AUDIO_EVIDENCE_VIDEO_MISMATCH")
        subject["audio_evidence_sha256"] = stable_hash(audio_evidence)
    return subject


def verify_acceptance(session: Session, task: AgentTask, result: dict, *, _archived_revalidation: bool = False) -> None:
    from glance_core.task_service import stable_hash

    if task.task_type != TaskType.CONTENT_QC or task.schema_version != "2":
        raise ValueError("legacy QC is not a v2 acceptance")
    if (
        task.input_data.get("acceptance_contract_version") != 2
        or task.input_data.get("acceptance_origin") != acceptance_origin(session, task.content_id)
    ):
        raise ValueError("acceptance contract/origin missing")
    if stable_hash(task.input_data) != task.input_hash:
        raise ValueError("acceptance task snapshot changed")
    refs = {
        **task.input_data.get("upstream_task_ids", {}),
        "video_artifact_id": task.input_data.get("video_artifact_id"),
    }
    version = task.input_data.get("source_context_version")
    audio_version = task.input_data.get("audio_evidence_version")
    audio = task.input_data.get("audio_evidence") if audio_version is not None else None
    if audio_version is not None and (
        type(audio_version) is not int or audio_version != 1 or not isinstance(audio, dict)
    ):
        raise ValueError("AUDIO_EVIDENCE_VERSION_UNSUPPORTED")
    current = acceptance_subject(
        session, task.content_id, refs, source_context_version=version, audio_evidence=audio,
        _archived_revalidation=_archived_revalidation,
    )
    if (
        audio is not None
        and result.get("passed")
        and (
            audio.get("method") != "local_asr_and_pcm"
            or audio.get("asr_status") != "available"
            or not audio.get("segments")
            or audio.get("signal_status") != "available"
        )
    ):
        raise ValueError("AUDIO_EVIDENCE_INCOMPLETE")
    if version is not None:
        context = source_context(session, task.content_id, refs)
        if task.input_data.get("source_context") != context:
            raise ValueError("SOURCE_CONTEXT_CHANGED")
        if result.get("passed") and context.get("evidence_gaps"):
            raise ValueError("SOURCE_CONTEXT_INCOMPLETE")
    if task.input_data.get("acceptance_subject") != current or result.get("subject") != current:
        raise ValueError("acceptance subject changed/mismatched")
