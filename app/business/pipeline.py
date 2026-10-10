from __future__ import annotations

import json
from contextlib import suppress
from copy import deepcopy
from math import ceil, isfinite
from pathlib import Path

from glance_collector import CollectionService
from glance_core.archive import ArchiveService
from glance_core.content_acceptance import (
    acceptance_origin,
    acceptance_subject,
    is_handsfree_content,
    source_context,
    verify_acceptance,
)
from glance_core.database import begin_immediate
from glance_core.models import (
    AgentTask,
    Artifact,
    AuditEvent,
    Candidate,
    CandidateStatus,
    Content,
    ContentStatus,
    ProductionMode,
    RightsStatus,
    TaskStatus,
    TaskType,
    as_utc,
    utc_now,
)
from glance_core.runtime_config import effective_settings
from glance_core.settings import DEFAULT_COVER_STYLE_PRESET, Settings
from glance_core.task_schemas import ContentAcceptanceResult
from glance_core.task_service import stable_hash
from glance_core.workflow import (
    WorkflowService,
    handsfree_review_policy,
    log_stage,
    normalize_video_url,
    parse_platform_domains,
)
from glance_media.bgm import load_catalog, pick_track, resolve_bgm_mood
from glance_media.executor import (
    NonRetryableMediaError,
    is_direct_media_url,
    missing_media_tools,
    sha256_file,
)
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.business.media_executor import MptMediaExecutor as MediaExecutor


SUPPORTED_TASK_TYPES = frozenset(
    {
        TaskType.SOURCE_DISCOVERY,
        TaskType.CANDIDATE_ENRICH,
        TaskType.CANDIDATE_ASSIST,
        TaskType.CONTENT_RESEARCH,
        TaskType.SCENE_ANALYZE,
        TaskType.SHORT_SCRIPT,
        TaskType.SCRIPT_QC,
        TaskType.SCRIPT_REVIEW_ASSIST,
        TaskType.VISUAL_PLAN,
        TaskType.VISUAL_PLAN_QC,
        TaskType.ASSET_GENERATE_IMAGE,
        TaskType.ASSET_QC,
        TaskType.CONTENT_QC,
        TaskType.FINAL_REVIEW_ASSIST,
        TaskType.PUBLISH_PREPARE,
        TaskType.PUBLISH_REVIEW_ASSIST,
        TaskType.DOUYIN_PUBLISH,
        TaskType.MEDIA_SOURCE_ASSIST,
        TaskType.RECOVERY,
    }
)


def _require_current_qc(session: Session, content_id: str, kind: TaskType, targets: dict) -> AgentTask:
    task = session.exec(
        select(AgentTask)
        .where(AgentTask.content_id == content_id, AgentTask.task_type == kind)
        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
    ).first()
    if task is None or task.status != TaskStatus.COMPLETED or not (task.result_data or {}).get("passed"):
        raise NonRetryableMediaError(f"Current {kind.value} must be completed and passed")
    declared = (task.input_data or {}).get("upstream_task_ids") or {}
    if stable_hash(task.input_data) != task.input_hash or any(
        declared.get(key) != target.id for key, target in targets.items() if target is not None
    ) or any(target is None for target in targets.values()):
        raise NonRetryableMediaError(f"Current {kind.value} lineage is missing or stale")
    return task


def _verify_rights(session: Session, content: Content) -> None:
    candidate = session.get(Candidate, content.candidate_id)
    allowed = {RightsStatus.SELF_OWNED, RightsStatus.EXPLICIT_LICENSE}
    if content.production_mode == ProductionMode.IMAGE_ONLY:
        allowed |= {RightsStatus.OFFICIAL_REFERENCE, RightsStatus.RESEARCH_ONLY}
    if candidate is None or candidate.rights_status not in allowed:
        raise NonRetryableMediaError("Candidate rights do not authorize this production mode")


def verify_publish_ready(session: Session, settings: Settings, content_id: str) -> None:
    """MPT's release boundary: old manual QC warnings never authorize publishing."""
    content = session.get(Content, content_id)
    if content is None:
        raise KeyError(content_id)
    _verify_rights(session, content)
    flow = workflow(session, settings)
    script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
    research = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
    plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
    _require_current_qc(session, content_id, TaskType.SCRIPT_QC, {"script": script, "research": research})
    _require_current_qc(session, content_id, TaskType.VISUAL_PLAN_QC, {"visual_plan": plan, "script": script})
    qc = _require_current_qc(session, content_id, TaskType.CONTENT_QC, {"script": script, "visual_plan": plan})
    if qc.schema_version != "2":
        raise NonRetryableMediaError("Publication requires schema-v2 acceptance")
    if qc.input_data.get("source_context_version") != 1 or qc.input_data.get("audio_evidence_version") != 1:
        raise NonRetryableMediaError("Publication requires source and independent final-audio evidence")
    verify_acceptance(session, qc, qc.result_data or {})
    video_id = str((qc.input_data or {}).get("video_artifact_id") or "")
    artifact = session.get(Artifact, video_id)
    latest = session.exec(
        select(Artifact).where(Artifact.content_id == content_id, Artifact.artifact_type == "final_video")
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    ).first()
    if artifact is None or latest is None or artifact.id != latest.id:
        raise NonRetryableMediaError("Content QC does not reference the latest final video")
    root = Path(content.content_dir).resolve()
    path = (root / artifact.relative_path).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256:
        raise NonRetryableMediaError("Content QC video failed file-integrity validation")


def _require_media_tools(*names: str) -> None:
    """失败前置：依赖缺失时直接给出中文可读错误，不进入深层英文报错。"""
    missing = missing_media_tools(*names)
    if missing:
        raise NonRetryableMediaError(
            f"服务器未安装 {'、'.join(missing)}，请先安装并重启 worker 后重试"
        )


def workflow(session: Session, settings: Settings) -> WorkflowService:
    return WorkflowService(
        session,
        data_dir=settings.data_dir,
        submission_root=settings.agent_submission_dir,
        lease_minutes=settings.agent_lease_minutes,
        video_platform_domains=parse_platform_domains(settings.video_platform_domains),
        recovery_enabled=settings.recovery_enabled,
        recovery_max_rounds=settings.recovery_max_rounds,
        recovery_same_task_retries=settings.recovery_same_task_retries,
        recovery_unknown_attempts=settings.recovery_unknown_attempts,
        recovery_max_parallel=settings.recovery_max_parallel,
    )


def _cap_transcript(transcript: dict, limit: int = 8000) -> dict:
    """Keep paragraph boundaries while bounding semantic task input size."""
    compact = json.loads(json.dumps(transcript, ensure_ascii=False))
    text = str(compact.get("text") or "")
    if len(text) > limit:
        boundary = text.rfind("\n", 0, limit)
        compact["text"] = text[: boundary if boundary > limit // 2 else limit]
        compact["truncated"] = True
    segments = compact.get("segments")
    if isinstance(segments, list):
        kept = []
        size = 0
        for segment in segments:
            encoded = json.dumps(segment, ensure_ascii=False)
            if size + len(encoded) > limit:
                compact["truncated"] = True
                break
            kept.append(segment)
            size += len(encoded)
        compact["segments"] = kept
    packed_text = str(compact.get("packed_text") or "")
    if len(packed_text) > limit:
        boundary = packed_text.rfind("\n", 0, limit)
        compact["packed_text"] = packed_text[: boundary if boundary > limit // 2 else limit]
        compact["truncated"] = True
    return compact


def _technical_qc_summary(qc: dict) -> dict:
    """Pass only review-relevant media facts to CONTENT_QC.

    ``MediaExecutor.qc`` keeps the complete ffprobe stream dictionaries for the
    local qc artifact. Those dictionaries contain unstable, low-value fields
    (tags, dispositions, bit rates, and encoder metadata) that do not help a
    semantic reviewer but do consume Agent input tokens.
    """
    video = qc.get("video") if isinstance(qc.get("video"), dict) else {}
    audio = qc.get("audio") if isinstance(qc.get("audio"), dict) else {}
    video_fields = (
        "codec_name",
        "width",
        "height",
        "pix_fmt",
        "r_frame_rate",
        "avg_frame_rate",
    )
    audio_fields = ("codec_name", "sample_rate", "channels", "channel_layout")
    return {
        "passed": bool(qc.get("passed")),
        "errors": [str(error) for error in qc.get("errors", [])],
        "visual_warnings": qc.get("visual_warnings", []),
        "duration_sec": round(float(qc.get("duration_sec") or 0), 2),
        "video": {key: video[key] for key in video_fields if key in video},
        "audio": {key: audio[key] for key in audio_fields if key in audio},
    }


def register_artifact(
    session: Session,
    *,
    content: Content,
    executor: MediaExecutor,
    path: Path,
    artifact_type: str,
    stage: str,
    deduplicate: bool = True,
) -> Artifact:
    metadata = executor.artifact_metadata(path, artifact_type, stage)
    if deduplicate:
        existing = session.exec(
            select(Artifact).where(
                Artifact.content_id == content.id,
                Artifact.artifact_type == artifact_type,
                Artifact.relative_path == metadata["relative_path"],
                Artifact.sha256 == metadata["sha256"],
            )
        ).first()
        if existing:
            return existing
    artifact = Artifact(content_id=content.id, **metadata)
    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


def _materialize_candidate_source_link(session: Session, candidate: Candidate) -> None:
    """Replace a consumed signed media URL with its stable source page."""
    if not is_direct_media_url(candidate.video_url):
        return
    stable_page = (candidate.video_source_page_url or candidate.source_url).strip()
    if not stable_page or is_direct_media_url(stable_page):
        return
    candidate.video_url = stable_page
    candidate.updated_at = utc_now()
    session.add(candidate)
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="media-worker",
            entity_type="candidate",
            entity_id=candidate.id,
            action="video_url_materialized",
            metadata_json={
                "source_page_url": stable_page,
                "materialization_method": "local-source-artifact",
            },
        )
    )
    session.commit()


def _effective_cover_title(content: Content, script: dict) -> str:
    return (content.cover_title_override or str(script.get("cover_title") or "")).strip()[:12]


def _fallback_cover_base(
    session: Session,
    content: Content,
    executor: MediaExecutor,
) -> tuple[Path, str]:
    """Resolve the explicit non-AI cover path when AI cover generation is disabled."""
    target = executor.root / "temp" / "cover" / "fallback-base.jpg"
    source_video = executor.root / "source" / "original.mp4"
    scene_task = session.exec(
        select(AgentTask)
        .where(
            AgentTask.content_id == content.id,
            AgentTask.task_type == TaskType.SCENE_ANALYZE,
            AgentTask.status == TaskStatus.COMPLETED,
        )
        .order_by(AgentTask.completed_at.desc())
    ).first()
    scenes = (scene_task.result_data or {}).get("scenes", []) if scene_task else []
    ranked = sorted(
        (scene for scene in scenes if scene.get("visual_quality") is not None),
        key=lambda scene: float(scene.get("visual_quality") or 0),
        reverse=True,
    )
    if source_video.is_file() and ranked:
        scene_times: dict[str, dict] = {}
        scene_file = executor.root / "scenes" / "scenes.json"
        try:
            scene_times = {
                str(item.get("scene_id")): item
                for item in json.loads(scene_file.read_text(encoding="utf-8")).get("scenes", [])
            }
        except (OSError, ValueError, TypeError):
            scene_times = {}
        scene = scene_times.get(str(ranked[0].get("scene_id")))
        if scene:
            midpoint = (float(scene.get("start_sec") or 0) + float(scene.get("end_sec") or 0)) / 2
            try:
                return (
                    executor.extract_cover_frame(source_video, midpoint, target),
                    "scene_analysis",
                )
            except Exception:
                pass
    final_video = executor.root / "render" / "final.mp4"
    if final_video.is_file():
        try:
            from glance_media.executor import probe

            duration = float(probe(final_video)["format"]["duration"])
            return executor.extract_cover_frame(
                final_video, duration * 0.3, target
            ), "final_30_percent"
        except Exception:
            pass
    gradient = executor.root / "temp" / "cover" / "gradient.ppm"
    return executor.gradient_cover_base(gradient), "gradient"


def _build_and_register_cover(
    session: Session,
    settings: Settings,
    content: Content,
    *,
    base_image: Path | None = None,
    actor_id: str = "pipeline",
    action: str = "cover_built",
) -> Artifact:
    flow = workflow(session, settings)
    script = flow.result_for(content.id, TaskType.SHORT_SCRIPT) or {}
    executor = MediaExecutor(Path(content.content_dir).resolve())
    source, fallback_stage = (
        (base_image, "generated_image")
        if base_image is not None
        else _fallback_cover_base(session, content, executor)
    )
    cover = executor.build_cover(source, _effective_cover_title(content, script))
    artifact = register_artifact(
        session,
        content=content,
        executor=executor,
        path=cover,
        artifact_type="cover",
        stage="package",
        deduplicate=False,
    )
    session.add(
        AuditEvent(
            actor_type="human" if actor_id != "pipeline" else "system",
            actor_id=actor_id,
            entity_type="content",
            entity_id=content.id,
            action=action,
            metadata_json={
                "artifact_id": artifact.id,
                "cover_title": _effective_cover_title(content, script),
                "base_source": fallback_stage,
            },
        )
    )
    session.commit()
    return artifact


def _create_cover_generation_task(
    flow: WorkflowService,
    content: Content,
    settings: Settings,
    script: dict,
) -> AgentTask:
    brief = str(script.get("cover_visual_brief") or "").strip()
    style = str(settings.cover_style_preset or "").strip() or DEFAULT_COVER_STYLE_PRESET
    request = {
        "information": (
            f"封面底图：{brief}。主体置下三分之二或左侧，上部四分之一保持简洁以供大字叠加"
        ),
        "style_direction": style,
        "subject": brief,
        "aspect_ratio": "9:16",
        "min_width": 1080,
        "min_height": 1920,
        "forbidden_elements": [
            "任何文字与水印",
            "可识别的真实产品外观",
            "真实人物面部",
            "品牌标识",
        ],
    }
    return flow.create_agent_task(
        content.id,
        TaskType.ASSET_GENERATE_IMAGE,
        {
            "asset_purpose": "cover",
            "requests": [{"segment_index": 0, "asset_request": request}],
            "script": script,
        },
        priority=65,
        allow_multiple=True,
    )


def _cover_ai_required(settings: Settings) -> bool:
    return bool(settings.cover_ai_enabled and settings.asset_image_enabled)


def _queue_post_package_tasks(
    session: Session,
    flow: WorkflowService,
    content: Content,
    settings: Settings,
    script: dict,
    research: dict,
    *,
    cover_artifact_id: str | None = None,
) -> Content:
    """Make a content publishable only after its final cover is registered."""
    if content.business_status != ContentStatus.PUBLISHED:
        content = flow.mark_status(content.id, ContentStatus.PACKAGED, "package")
    flow.set_error(content.id, None, None)

    if not cover_artifact_id:
        cover = session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content.id,
                Artifact.artifact_type == "cover",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        cover_artifact_id = cover.id if cover is not None else None

    if settings.publish_prepare_enabled:
        flow.create_agent_task(
            content.id,
            TaskType.PUBLISH_PREPARE,
            {
                "script": script,
                "research": research,
                "cover_artifact_id": cover_artifact_id or "",
            },
            priority=35,
        )
    else:
        # With no metadata-preparation Agent enabled, build the review input directly.
        # When preparation is enabled, schedule_manual_assist_tasks creates review only
        # after its result exists so the review cannot consume a partial package.
        flow.create_agent_task(
            content.id,
            TaskType.PUBLISH_REVIEW_ASSIST,
            flow.build_publish_review_input(
                content.id,
                settings=settings,
                script=script,
                research=research,
                publish_prepare={},
                publish_policy=(
                    handsfree_review_policy()
                    if settings.handsfree_enabled and settings.handsfree_auto_publish_douyin_enabled
                    and is_handsfree_content(session, content.id)
                    else None
                ),
            ),
            priority=40,
        )
    if settings.archive_enabled:
        try:
            ArchiveService(session, settings).snapshot(content.id)
        except Exception as exc:  # 归档失败不阻断打包，只记审计。
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="archive-service",
                    entity_type="content",
                    entity_id=content.id,
                    action="archive_failed",
                    metadata_json={"error": str(exc)[:500]},
                )
            )
            session.commit()
    session.refresh(content)
    return content


def _source_image_index_path(content: Content) -> Path:
    return Path(content.content_dir).resolve() / "source_images" / "index.json"


def source_image_index(content: Content) -> list[dict]:
    """已下载网页原图的登记清单（artifact_id / caption / 尺寸）。"""
    path = _source_image_index_path(content)
    if not path.is_file():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("images", []))
    except (OSError, ValueError, TypeError):
        return []


def _acquire_source_images(
    session: Session,
    content: Content,
    research: dict,
) -> list[dict]:
    """图文模式的 acquire 节点：把研究回传的配图 URL 下载并逐张登记。"""
    images = list(research.get("source_images") or [])
    if not images:
        return []
    executor = MediaExecutor(Path(content.content_dir).resolve())
    acquired = log_stage(
        session,
        content.id,
        "acquire_images",
        lambda: executor.acquire_images(images),
    )
    entries: list[dict] = []
    for item in acquired:
        artifact = register_artifact(
            session,
            content=content,
            executor=executor,
            path=item["path"],
            artifact_type="source_image",
            stage="acquire_images",
        )
        entries.append(
            {
                "artifact_id": artifact.id,
                "artifact_url": f"/media/artifacts/{artifact.id}",
                "relative_path": artifact.relative_path,
                "url": item["url"],
                "caption": item["caption"],
                "source_page": item["source_page"],
                "width": item["width"],
                "height": item["height"],
            }
        )
    index_path = _source_image_index_path(content)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps({"images": entries}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return entries


_IMAGE_ONLY_DEFAULT_STYLE = (
    "写实产品摄影，干净自然光，统一的中性背景，真实材质与清晰轮廓，竖屏构图，不含文字"
)


def _image_only_minimum_assets(duration_sec: float) -> int:
    return 4 if duration_sec <= 30 else 5


def _split_image_segment(segment: dict, parts: int) -> list[dict]:
    """Split one image segment without changing the narration timeline."""
    start = float(segment["narration_start_sec"])
    end = float(segment["narration_end_sec"])
    span = end - start
    result: list[dict] = []
    for index in range(parts):
        item = dict(segment)
        item["narration_start_sec"] = round(start + span * index / parts, 3)
        item["narration_end_sec"] = round(
            end if index == parts - 1 else start + span * (index + 1) / parts,
            3,
        )
        if item.get("asset_request"):
            # A reference image may only be redrawn once. The first piece keeps
            # the reference; extra pieces become pure generated views.
            request = dict(item["asset_request"])
            if index:
                request.pop("reference_artifact_id", None)
            item["asset_request"] = request
            item.pop("asset_id", None)
        result.append(item)
    return result


def _build_image_only_asset_request(
    *,
    content: Content,
    script: dict,
    segment: dict,
    style_direction: str,
    source_ids: set[str],
    used_reference_ids: set[str],
) -> dict:
    opening = script.get("opening") if isinstance(script.get("opening"), dict) else {}
    identity = str(
        opening.get("product_identity") or content.title_zh or content.title or "the product"
    ).strip()
    special = str(opening.get("special_value") or "").strip()
    purpose = str(segment.get("purpose") or "产品关键特征展示").strip()
    request = {
        "information": (
            f"补充产品展示画面：{purpose}。"
            f"主体是 {identity}；画面围绕旁白对应的信息点，保持真实产品外观，"
            "使用静态构图表达，不凭空添加动作，不生成文字。"
        ),
        "style_direction": style_direction,
        "subject": (
            f"realistic vertical product presentation of {identity}; "
            f"highlight {special or purpose}; clean natural lighting, accurate product shape"
        ),
        "aspect_ratio": "9:16",
        "min_width": 1080,
        "min_height": 1920,
    }
    source_asset_id = str(segment.get("asset_id") or "")
    if source_asset_id in source_ids and source_asset_id not in used_reference_ids:
        request["reference_artifact_id"] = source_asset_id
        used_reference_ids.add(source_asset_id)
    return request


def _ensure_image_only_asset_requests(
    *,
    content: Content,
    settings: Settings,
    plan_task: AgentTask,
    script: dict,
) -> tuple[dict, dict[str, int]]:
    """Deterministically fill a too-sparse image plan with bounded Agent requests.

    The worker does not invent a new claim or decide what the product does. It
    only turns repeated/overlong image slots already selected by the visual
    Agent into bounded static-image requests, using that segment's existing
    purpose and the script's opening as context for the image Agent.
    """
    plan = dict(plan_task.result_data or {})
    segments = [dict(segment) for segment in plan.get("segments", [])]
    if not segments or content.production_mode != ProductionMode.IMAGE_ONLY:
        return plan, {"minimum": 0, "asset_count": 0, "injected": 0, "budget": 0}
    duration = float(plan_task.input_data.get("voice_duration_sec") or 0)
    minimum = _image_only_minimum_assets(duration)
    budget = (
        min(
            int(plan_task.input_data.get("image_only_gen_budget") or 0),
            int(settings.image_only_gen_budget),
        )
        if settings.asset_image_enabled
        else 0
    )
    source_ids = {
        str(item) for item in plan_task.input_data.get("allowed_reference_artifact_ids", [])
    }
    requests = [
        segment.get("asset_request") for segment in segments if segment.get("asset_request")
    ]
    asset_ids = {str(segment.get("asset_id")) for segment in segments if segment.get("asset_id")}
    asset_count = len(asset_ids) + len(requests)
    if asset_count >= minimum or budget <= len(requests):
        return plan, {
            "minimum": minimum,
            "asset_count": asset_count,
            "injected": 0,
            "budget": budget,
        }

    # Use the visual Agent's first style so injected requests remain schema
    # compatible with the rest of the plan.
    style_direction = next(
        (
            str(request.get("style_direction"))
            for request in requests
            if isinstance(request, dict) and request.get("style_direction")
        ),
        _IMAGE_ONLY_DEFAULT_STYLE,
    )
    used_reference_ids = {
        str(request.get("reference_artifact_id"))
        for request in requests
        if isinstance(request, dict) and request.get("reference_artifact_id")
    }

    # A long single-image slot cannot become a multi-image video by merely
    # adding metadata, so split the longest eligible slot first. Keep the total
    # number of segments within the schema's eight-image ceiling.
    target_segments = min(8, max(minimum, ceil(duration / 8)))
    while len(segments) < target_segments:
        candidates = [
            (index, segment)
            for index, segment in enumerate(segments)
            if segment.get("asset_id") or segment.get("asset_request")
        ]
        if not candidates:
            break
        index, segment = max(
            candidates,
            key=lambda item: (
                float(item[1]["narration_end_sec"]) - float(item[1]["narration_start_sec"])
            ),
        )
        if segment.get("asset_request") and len(requests) >= budget:
            # Splitting a generated segment would exceed the image budget.
            asset_candidates = [item for item in candidates if item[1].get("asset_id")]
            if not asset_candidates:
                break
            index, segment = max(
                asset_candidates,
                key=lambda item: (
                    float(item[1]["narration_end_sec"]) - float(item[1]["narration_start_sec"])
                ),
            )
        pieces = _split_image_segment(segment, 2)
        segments[index : index + 1] = pieces
        requests = [item.get("asset_request") for item in segments if item.get("asset_request")]

    injected = 0
    seen_asset_ids: set[str] = set()
    for segment in segments:
        asset_id = str(segment.get("asset_id") or "")
        if not asset_id:
            continue
        if asset_id not in seen_asset_ids:
            seen_asset_ids.add(asset_id)
            continue
        if len(requests) >= budget or len(asset_ids) + len(requests) >= minimum:
            continue
        segment["asset_request"] = _build_image_only_asset_request(
            content=content,
            script=script,
            segment=segment,
            style_direction=style_direction,
            source_ids=source_ids,
            used_reference_ids=used_reference_ids,
        )
        segment.pop("asset_id", None)
        requests.append(segment["asset_request"])
        injected += 1

    final_asset_ids = {
        str(segment.get("asset_id")) for segment in segments if segment.get("asset_id")
    }
    plan["segments"] = segments
    return plan, {
        "minimum": minimum,
        "asset_count": len(final_asset_ids) + len(requests),
        "injected": injected,
        "budget": budget,
    }


def prepare_content(engine: Engine, settings: Settings, content_id: str) -> Content:
    _require_media_tools("ffmpeg", "ffprobe")
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        candidate = session.get(Candidate, content.candidate_id)
        if candidate is None:
            raise KeyError(content.candidate_id)
        flow = workflow(session, settings)
        base = {
            "content_id": content.id,
            "candidate": {
                "title": candidate.original_title,
                "source_url": candidate.source_url,
                "source_account": candidate.source_account,
            },
        }
        if content.production_mode == ProductionMode.IMAGE_ONLY:
            if is_handsfree_content(session, content.id):
                from glance_core.content_acceptance import candidate_source_context

                context = candidate_source_context(session, candidate)
                base["source_context_read_only"] = context
                base["candidate"]["source_url"] = context["source_url"]
            # 图文模式：无原片可下载，acquire / transcribe / scene_detect 三节点跳过，
            # 直接进入事实研究（研究结果同时回传网页配图 URL）。
            flow.mark_status(content.id, ContentStatus.PREPARING_CONTEXT, "research")
            flow.create_agent_task(
                content.id,
                TaskType.CONTENT_RESEARCH,
                {
                    **base,
                    "transcript": {"segments": []},
                    "production_mode": ProductionMode.IMAGE_ONLY.value,
                    **flow.image_generation_inputs(settings),
                },
                priority=30,
            )
            return flow.mark_status(content.id, ContentStatus.WAITING_AGENT, "agent_context")
        flow.mark_status(content.id, ContentStatus.PREPARING_CONTEXT, "acquire")
        root = Path(content.content_dir).resolve()
        executor = MediaExecutor(root)
        cookies_file = (
            Path(settings.ytdlp_cookies_file) if settings.ytdlp_cookies_file.strip() else None
        )
        video = log_stage(
            session,
            content.id,
            "acquire",
            lambda: executor.acquire(
                candidate.video_url or candidate.source_url,
                candidate.source_type,
                cookies_file=cookies_file,
                source_page_url=candidate.video_source_page_url or candidate.source_url,
                user_agent=settings.media_user_agent,
            ),
        )
        _materialize_candidate_source_link(session, candidate)
        register_artifact(
            session,
            content=content,
            executor=executor,
            path=video,
            artifact_type="source_video",
            stage="acquire",
        )
        transcript_path = log_stage(
            session,
            content.id,
            "transcribe",
            lambda: executor.ensure_transcript(
                asr_model=settings.asr_model,
                asr_device=settings.asr_device,
                asr_compute_type=settings.asr_compute_type,
            ),
        )
        register_artifact(
            session,
            content=content,
            executor=executor,
            path=transcript_path,
            artifact_type="transcript",
            stage="transcribe",
        )
        scenes_path, scenes = log_stage(
            session,
            content.id,
            "scene_detect",
            lambda: executor.analyze_scenes(video),
        )
        register_artifact(
            session,
            content=content,
            executor=executor,
            path=scenes_path,
            artifact_type="scenes",
            stage="scene_detect",
        )
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        # REQ-9：百场景级素材按批拆分，单批失败只重试该批；未超阈值行为不变。
        batch_size = max(1, int(settings.scene_batch_size))
        batches = [
            scenes[start : start + batch_size] for start in range(0, len(scenes), batch_size)
        ]
        batches = batches or [[]]
        batch_count = len(batches)
        packed_transcript = str(_cap_transcript(transcript).get("packed_text") or "")
        for batch_index, batch in enumerate(batches, start=1):
            payload = {
                **base,
                "scenes": batch,
                "context_files": [scene["thumbnail"] for scene in batch],
            }
            if packed_transcript:
                payload["transcript"] = {"packed_text": packed_transcript}
            if batch_count > 1:
                payload["batch_index"] = batch_index
                payload["batch_count"] = batch_count
            flow.create_agent_task(
                content.id,
                TaskType.SCENE_ANALYZE,
                payload,
                priority=30,
                allow_multiple=batch_count > 1,
            )
        flow.create_agent_task(
            content.id,
            TaskType.CONTENT_RESEARCH,
            {
                **base,
                "transcript": _cap_transcript(transcript),
                "production_mode": content.production_mode.value,
            },
            priority=30,
        )
        return flow.mark_status(content.id, ContentStatus.WAITING_AGENT, "agent_context")


def _reference_artifact_paths(
    session: Session,
    content_id: str,
    requests: list[tuple[int, dict]],
) -> list[str]:
    """参考重绘涉及的原图相对路径：随任务包下发到 workspace/context/。"""
    reference_ids = [
        str(request["reference_artifact_id"])
        for _index, request in requests
        if request.get("reference_artifact_id")
    ]
    if not reference_ids:
        return []
    artifacts = session.exec(
        select(Artifact).where(
            Artifact.content_id == content_id,
            Artifact.id.in_(reference_ids),
        )
    ).all()
    return [artifact.relative_path for artifact in artifacts]


def _upstream_task_id(task: AgentTask, field: str) -> str:
    value = (task.input_data or {}).get("upstream_task_ids", {}).get(field)
    return str(value or "")


def _current_visual_plan_for_asset_task(
    session: Session,
    flow: WorkflowService,
    task: AgentTask,
) -> AgentTask | None:
    """Return the visual plan that owns an image task, rejecting late old results."""
    current_plan = flow.completed_task_for(task.content_id or "", TaskType.VISUAL_PLAN)
    if current_plan is None:
        return None
    upstream_plan_id = _upstream_task_id(task, "visual_plan")
    if upstream_plan_id:
        return current_plan if upstream_plan_id == current_plan.id else None
    # Tasks created before version tagging cannot be associated by ID. Their creation time
    # still lets us reject a result that predates the current visual plan.
    if as_utc(current_plan.created_at) > as_utc(task.created_at):
        return None
    return current_plan


def _asset_qc_input(
    session: Session,
    flow: WorkflowService,
    task: AgentTask | None,
    plan_task: AgentTask,
) -> dict | None:
    from glance_core.content_acceptance import generated_image_material

    images = (task.result_data or {}).get("images", []) if task else []
    if not plan_task.result_data:
        return None
    if plan_task.input_data.get("original_reuse") is not True:
        if not task or not images:
            return None
        requests = {
            entry["segment_index"]: entry.get("asset_request") or {}
            for entry in task.input_data.get("requests", [])
        }
        assets = [
            {
                "segment_index": image["segment_index"],
                "artifact_id": image.get("artifact_id"),
                "artifact_url": f"/media/artifacts/{image['artifact_id']}"
                if image.get("artifact_id")
                else None,
                "request": requests.get(image["segment_index"], {}),
                "generation": image,
            }
            for image in sorted(images, key=lambda i: i["segment_index"])
        ]
        paths = {
            a.id: a.relative_path
            for a in session.exec(
                select(Artifact).where(
                    Artifact.content_id == task.content_id,
                    Artifact.id.in_([a["artifact_id"] for a in assets]),
                )
            ).all()
        }
        return {
            "upstream_task_ids": {"visual_plan": plan_task.id, "asset_generate_image": task.id},
            "assets": assets,
            "context_files": [paths[a["artifact_id"]] for a in assets if a["artifact_id"] in paths],
            "script": flow.result_for(task.content_id or "", TaskType.SHORT_SCRIPT),
        }
    if (
        plan_task.status != TaskStatus.COMPLETED
        or stable_hash(plan_task.input_data) != plan_task.input_hash
    ):
        raise ValueError("Original reuse requires unchanged completed current plan")
    generated_by_index = {i["segment_index"]: i["artifact_id"] for i in images}
    content = session.get(Content, plan_task.content_id)
    assets = []
    for index, segment in enumerate(plan_task.result_data.get("segments", [])):
        asset_id = generated_by_index.get(index) or segment.get("asset_id")
        artifact = session.get(Artifact, asset_id) if asset_id else None
        if artifact and artifact.artifact_type == "agent_generated_image":
            if index not in generated_by_index and asset_id not in plan_task.input_data.get(
                "allowed_asset_ids", []
            ):
                raise ValueError("Reused image is not declared by current plan")
            material = generated_image_material(session, content, asset_id)
            if index in generated_by_index and (
                material["generation_task_id"] != task.id
                or material["generation_plan_id"] != plan_task.id
            ):
                raise ValueError("Generated image is not from current plan/task")
            if index in generated_by_index and (
                (segment.get("asset_request") and segment["asset_request"] != material["request"])
                or (not segment.get("asset_request") and segment.get("asset_id") != asset_id)
            ):
                raise ValueError("Generated request differs from current plan")
            assets.append({"segment_index": index, **material})
        elif segment.get("asset_request") or segment.get("asset_id"):
            raise ValueError("Current plan image generation is incomplete")
    if not assets:
        return None
    artifact_ids = [str(item["artifact_id"]) for item in assets if item.get("artifact_id")]
    generated = {}
    if artifact_ids:
        generated = {
            artifact.id: artifact.relative_path
            for artifact in session.exec(
                select(Artifact).where(
                    Artifact.content_id == plan_task.content_id,
                    Artifact.id.in_(artifact_ids),
                )
            ).all()
        }
    return {
        "upstream_task_ids": {
            "visual_plan": plan_task.id,
            **({"asset_generate_image": task.id} if task else {}),
        },
        "full_generated_assets": True,
        "assets": assets,
        "context_files": [
            generated[str(item["artifact_id"])]
            for item in assets
            if str(item.get("artifact_id") or "") in generated
        ],
        "script": flow.result_for(plan_task.content_id or "", TaskType.SHORT_SCRIPT),
    }


def _audit_stale_agent_result(session: Session, task: AgentTask, *, current_plan_id: str) -> None:
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="pipeline",
            entity_type="agent_task",
            entity_id=task.id,
            action="stale_agent_result_ignored",
            metadata_json={
                "task_type": task.task_type.value,
                "current_visual_plan_id": current_plan_id,
                "upstream_visual_plan_id": _upstream_task_id(task, "visual_plan"),
            },
        )
    )
    session.commit()


def _audit_stale_lineage(
    session: Session,
    task: AgentTask,
    *,
    reason: str,
    current_task_ids: dict[str, str | list[str] | None] | None = None,
) -> None:
    """Record and ignore a valid result that no longer belongs to the active lineage."""
    session.add(
        AuditEvent(
            actor_type="system",
            actor_id="pipeline",
            entity_type="agent_task",
            entity_id=task.id,
            action="stale_agent_result_ignored",
            metadata_json={
                "task_type": task.task_type.value,
                "reason": reason,
                "declared_upstream_task_ids": (task.input_data or {}).get("upstream_task_ids", {}),
                "current_task_ids": current_task_ids or {},
            },
        )
    )
    session.commit()


# Stage order protects accepted-task replay after a later media action committed.
# Runtime owns the content file lock and final consumed marker; no DB lock spans media IO.
_STAGE_ORDER = {
    "content_research": 10, "scene_analyze": 10, "short_script": 20,
    "script_qc": 30, "script_review": 40, "voice_gate": 40,
    "visual_plan": 50, "visual_plan_qc": 60, "asset_generate": 65,
    "asset_generate_image": 65, "asset_qc": 70, "asset_qc_missing": 70,
    "render_ready": 80, "render": 90, "technical_qc": 90,
    "audio_evidence_waiting": 95, "audio_evidence_running": 95,
    "content_qc": 100, "content_qc_failed": 100, "final_review": 110,
    "cover_generate": 120, "publish_prepare": 130, "packaged": 140,
}
_TASK_OUTPUT_ORDER = {
    TaskType.CONTENT_RESEARCH: 20, TaskType.SCENE_ANALYZE: 20,
    TaskType.SHORT_SCRIPT: 30, TaskType.SCRIPT_QC: 40,
    TaskType.VISUAL_PLAN: 60, TaskType.VISUAL_PLAN_QC: 65,
    TaskType.ASSET_GENERATE_IMAGE: 70, TaskType.ASSET_QC: 80,
    TaskType.CONTENT_QC: 110,
}


def advance_after_agent(engine: Engine, settings: Settings, task_id: str) -> str | None:
    with Session(engine) as session:
        task = session.get(AgentTask, task_id)
        if task is None or task.status != TaskStatus.COMPLETED:
            return None
        content = session.get(Content, task.content_id) if task.content_id else None
        terminal = content is not None and content.business_status in {
            ContentStatus.PUBLISHED, ContentStatus.ARCHIVED, ContentStatus.REJECTED,
        }
        terminal = terminal or (content is not None and content.business_status == ContentStatus.PACKAGED
                                and task.task_type in _TASK_OUTPUT_ORDER
                                and not (task.task_type == TaskType.ASSET_GENERATE_IMAGE
                                         and task.input_data.get("asset_purpose") == "cover"))
        target_order = _TASK_OUTPUT_ORDER.get(task.task_type)
        progressed = (
            content is not None and target_order is not None
            and _STAGE_ORDER.get(content.current_stage, -1) >= target_order
            and not (task.task_type == TaskType.ASSET_GENERATE_IMAGE
                     and task.input_data.get("asset_purpose") == "cover")
        )
    if task.task_type == TaskType.DOUYIN_PUBLISH or not (terminal or progressed):
        action = _advance_after_agent(engine, settings, task_id)
        if action is not None:
            return action
    with Session(engine) as session:
        content = session.get(Content, task.content_id) if task.content_id else None
        current = effective_settings(session, settings)
        if (content is not None and content.business_status == ContentStatus.PACKAGED
            and task.task_type == TaskType.PUBLISH_REVIEW_ASSIST):
            from app.business.publish_policy import queue_auto_publish

            queue_auto_publish(session, settings, content.id)
            return None
        if (content is None or content.business_status in {
            ContentStatus.PACKAGED, ContentStatus.PUBLISHED, ContentStatus.ARCHIVED,
            ContentStatus.REJECTED,
        } or not current.handsfree_enabled or not current.handsfree_auto_review_enabled
            or not is_handsfree_content(session, content.id)):
            return None
        if task.task_type == TaskType.SCRIPT_QC and content.current_stage == "script_review":
            return "voice"
        if task.task_type in {TaskType.VISUAL_PLAN_QC, TaskType.ASSET_QC,
                              TaskType.ASSET_GENERATE_IMAGE} and content.current_stage == "render_ready":
            return "render"
        if task.task_type == TaskType.CONTENT_QC and content.current_stage == "final_review":
            return "package"
    return None


def _advance_after_agent(
    engine: Engine,
    settings: Settings,
    task_id: str,
) -> str | None:
    with Session(engine) as session:
        task = session.get(AgentTask, task_id)
        if task is None or task.status != TaskStatus.COMPLETED:
            return
        flow = workflow(session, settings)
        if task.task_type == TaskType.CANDIDATE_ENRICH:
            result = task.result_data or {}
            items = result.get("items", [])
            # Agent 只允许回写本批输入中的候选；未知 ID 记入审计而不是静默丢弃。
            allowed_ids = {
                entry.get("candidate_id")
                for entry in task.input_data.get("items", [])
                if entry.get("candidate_id")
            }
            touched = False
            unknown_ids: list[str] = []
            untranslated: list[str] = []
            untranslated_summaries: list[str] = []
            for item in items:
                item_id = item.get("candidate_id")
                if allowed_ids and item_id not in allowed_ids:
                    unknown_ids.append(str(item_id))
                    continue
                candidate = session.get(Candidate, item_id)
                if candidate is None or not item.get("scores"):
                    continue  # 结果缺失的候选保持 NEW，等下轮批量打包。
                candidate.category = item["category"]
                summary_zh = str(item.get("summary") or "").strip()
                summary_valid = _looks_translated(summary_zh)
                if summary_valid:
                    candidate.summary = summary_zh
                else:
                    untranslated_summaries.append(candidate.id)
                title_zh = str(item.get("title_zh") or "").strip()
                if _looks_translated(title_zh):
                    candidate.title_zh = title_zh
                elif title_zh:
                    # Agent 把原标题原样抄了回来：不写入，让它下轮重新入批。
                    untranslated.append(candidate.id)
                candidate.score = item["scores"]["total"]
                candidate.suggested_hook = item["suggested_hook"]
                # BUG-7：只接受视频平台播放页或真实媒体直链，不把来源文章/项目页
                # 重复写入 video_url；不覆盖人工或采集已确认的有效 video_url。
                current_video_url = normalize_video_url(
                    candidate.video_url, flow.video_platform_domains
                )
                if current_video_url != (candidate.video_url or "").strip():
                    candidate.video_url = current_video_url
                enriched_video_url = normalize_video_url(
                    str(item.get("video_url") or ""), flow.video_platform_domains
                )
                if enriched_video_url and not candidate.video_url.strip():
                    candidate.video_url = enriched_video_url
                # 只有尚未评估的候选才推进状态：迟到的结果或补翻批次绝不能把
                # 已选题（SELECTED）/已忽略（IGNORED）的候选倒退回 ENRICHED。
                if (
                    candidate.status == CandidateStatus.NEW
                    and _looks_translated(candidate.title_zh)
                    and summary_valid
                ):
                    candidate.status = CandidateStatus.ENRICHED
                candidate.updated_at = utc_now()
                session.add(candidate)
                touched = True
            if unknown_ids:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="enrich_unknown_candidates",
                        metadata_json={"unknown_candidate_ids": unknown_ids[:20]},
                    )
                )
            if untranslated:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="enrich_title_not_translated",
                        metadata_json={"candidate_ids": untranslated[:20]},
                    )
                )
            if untranslated_summaries:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="enrich_summary_not_translated",
                        metadata_json={"candidate_ids": untranslated_summaries[:20]},
                    )
                )
            if touched or unknown_ids or untranslated or untranslated_summaries:
                session.commit()
            flow.schedule_enrich_batch(effective_settings(session, settings).enrich_batch_size)
            return
        if task.task_type == TaskType.CANDIDATE_ASSIST:
            current = effective_settings(session, settings)
            allowed_ids = {
                str(item.get("candidate_id"))
                for item in task.input_data.get("items", [])
                if item.get("candidate_id")
            }
            for item in (task.result_data or {}).get("items", []):
                candidate_id = str(item.get("candidate_id") or "")
                if candidate_id not in allowed_ids:
                    continue
                candidate = session.get(Candidate, candidate_id)
                if candidate is None or candidate.status != CandidateStatus.ENRICHED:
                    continue
                candidate.assist_advised_at = utc_now()
                session.add(candidate)
                if (
                    current.assistant_auto_reject_enabled
                    and item.get("verdict") == "REJECT"
                    and item.get("confidence") == "high"
                    and candidate.score <= current.assistant_auto_reject_max_score
                ):
                    candidate.status = CandidateStatus.IGNORED
                    candidate.updated_at = utc_now()
                    session.add(candidate)
                    session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id="assistant-policy",
                            entity_type="candidate",
                            entity_id=candidate.id,
                            action="candidate_auto_rejected",
                            metadata_json={
                                "assistant_task_id": task.id,
                                "score": candidate.score,
                                "threshold": current.assistant_auto_reject_max_score,
                                "reasons": item.get("reasons") or [],
                            },
                        )
                    )
            session.commit()
            flow.schedule_candidate_assist_batch(current.enrich_batch_size, settings=current)
            return
        if task.task_type == TaskType.SOURCE_DISCOVERY:
            CollectionService(session, settings).ingest_agent_result(task)
            return
        if task.task_type == TaskType.RECOVERY:
            flow.apply_recovery_plan(task)
            return
        if task.task_type == TaskType.DOUYIN_PUBLISH:
            flow.record_agent_publication_result(task)
            return
        if task.task_type == TaskType.PUBLISH_PREPARE:
            current = effective_settings(session, settings)
            flow.schedule_manual_assist_tasks(
                publish_policy=handsfree_review_policy() if current.handsfree_enabled
                and current.handsfree_auto_publish_douyin_enabled else None,
                settings=settings,
            )
            return
        if task.task_type == TaskType.MEDIA_SOURCE_ASSIST:
            content = session.get(Content, task.content_id) if task.content_id else None
            result = task.result_data or {}
            if (not settings.handsfree_enabled or content is None
                or not is_handsfree_content(session, content.id)
                or content.business_status != ContentStatus.FAILED
                or content.error_code != "PREPARE_FAILED"):
                return  # Manual suggestions remain visible through their persisted task results.
            if str(result.get("verdict") or "").lower() != "found":
                return
            page = str(result.get("suggested_video_url") or "").strip()
            video = normalize_video_url(
                str(result.get("direct_video_url") or "") or page, flow.video_platform_domains
            )
            if video:
                flow.adopt_video_source(
                    content.id, video, source_page_url=page,
                    actor_id="handsfree-coordinator", actor_type="system", task_id=task.id,
                )
                return "prepare"
            return
        if not task.content_id:
            return

        content_id = task.content_id
        if task.task_type == TaskType.SCRIPT_REVIEW_ASSIST:
            content = session.get(Content, content_id)
            script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            qc_task = flow.completed_task_for(content_id, TaskType.SCRIPT_QC)
            research_task = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
            declared = (task.input_data or {}).get("upstream_task_ids") or {}
            current_ids = {
                "script": script_task.id if script_task else None,
                "script_qc": qc_task.id if qc_task else None,
                "research": research_task.id if research_task else None,
            }
            if any(
                declared.get(key) and current_id != str(declared.get(key))
                for key, current_id in current_ids.items()
            ):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="script_review_assist_is_not_current",
                    current_task_ids=current_ids,
                )
                return
            if content is None or script_task is None or qc_task is None or research_task is None:
                return
            # 本阶段助手只提供意见，不自动批准、重写或推进配音；人工审核面板拥有最终
            # 的脚本通过/打回权。保留原始 verdict 与 issues，便于人工追溯。
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="assistant-policy",
                    entity_type="content",
                    entity_id=content_id,
                    action="script_review_assist_recorded",
                    metadata_json={
                        "assistant_task_id": task.id,
                        "verdict": (task.result_data or {}).get("verdict"),
                        "issue_count": len((task.result_data or {}).get("issues", [])),
                        "auto_approve": False,
                    },
                )
            )
            session.commit()
            return
        if task.task_type in {TaskType.SCENE_ANALYZE, TaskType.CONTENT_RESEARCH}:
            content = session.get(Content, content_id)
            if content is None:
                return
            if task.task_type == TaskType.CONTENT_RESEARCH:
                current_research = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
                if current_research is None or current_research.id != task.id:
                    _audit_stale_lineage(
                        session,
                        task,
                        reason="content_research_is_not_current",
                        current_task_ids={
                            "content_research": current_research.id if current_research else None
                        },
                    )
                    return
            else:
                current_scene_tasks = flow.scene_analysis_tasks(content_id)
                if current_scene_tasks and task.id not in {item.id for item in current_scene_tasks}:
                    _audit_stale_lineage(
                        session,
                        task,
                        reason="scene_analysis_batch_is_not_current",
                        current_task_ids={
                            "scene_analysis": [item.id for item in current_scene_tasks]
                        },
                    )
                    return
            research_task = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
            if research_task is None:
                return
            image_only = content.production_mode == ProductionMode.IMAGE_ONLY
            repair_plan = research_task.input_data.get("recovery_plan") or {}
            rights_research_repair = (
                isinstance(repair_plan, dict)
                and repair_plan.get("failure_class") in {"BUSINESS_QC", "RIGHTS_OR_PUBLISHING"}
                and repair_plan.get("target_task_type") == TaskType.CONTENT_RESEARCH.value
                and bool(research_task.input_data.get("recovery_task_id"))
                and is_handsfree_content(session, content_id)
            )
            script_input: dict = {
                "min_video_seconds": settings.min_video_seconds,
                "max_video_seconds": settings.max_video_seconds,
                "target_duration_sec": 35,
                "style": "面向中文大众，结果前置，给出明确判断",
                "production_mode": content.production_mode.value,
                "allowed_claim_ids": [
                    str(claim.get("id"))
                    for claim in (research_task.result_data or {}).get("verified_claims", [])
                    if claim.get("id")
                ],
                **flow.script_audience_inputs(content, research_task, settings=settings),
            }
            if image_only:
                # 图文模式：研究完成后先把配图下载登记，再直达口播脚本。
                _acquire_source_images(session, content, research_task.result_data or {})
                flow.create_agent_task(
                    content_id,
                    TaskType.SHORT_SCRIPT,
                    {
                        **script_input,
                        "upstream_task_ids": {"research": research_task.id},
                    },
                    priority=40,
                    allow_multiple=rights_research_repair,
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "short_script")
                return
            scene_tasks = flow.scene_analysis_tasks(content_id)
            merged = flow.merged_scene_analysis(content_id)
            if merged is None:
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "scene_analyze")
                return  # 尚有场景批次未完成，等最后一批到齐再推进。
            gaps = flow.scene_batch_gaps(content_id, merged)
            if gaps:
                flow.set_error(content_id, "SCENE_BATCH_INCOMPLETE", "；".join(gaps))
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "scene_analyze")
                return
            if content is not None and content.error_code == "SCENE_BATCH_INCOMPLETE":
                flow.set_error(content_id, None, None)
            scene_reference: str | list[str] = (
                [item.id for item in scene_tasks] if len(scene_tasks) > 1 else scene_tasks[0].id
            )
            flow.create_agent_task(
                content_id,
                TaskType.SHORT_SCRIPT,
                {
                    **script_input,
                    "upstream_task_ids": {
                        "research": research_task.id,
                        "scene_analysis": scene_reference,
                    },
                    "materialized_context": {
                        "transcript": "transcript/transcript.json",
                    },
                },
                priority=40,
                allow_multiple=rights_research_repair,
            )
            flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "short_script")
        elif task.task_type == TaskType.SHORT_SCRIPT:
            current_script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            research_task = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
            declared_research_id = _upstream_task_id(task, "research")
            if current_script is None or current_script.id != task.id:
                _audit_stale_lineage(
                    session,
                    task,
                    reason="short_script_is_not_current",
                    current_task_ids={
                        "short_script": current_script.id if current_script else None,
                        "content_research": research_task.id if research_task else None,
                    },
                )
                return
            if declared_research_id and (
                research_task is None or declared_research_id != research_task.id
            ):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="short_script_research_dependency_is_stale",
                    current_task_ids={
                        "short_script": current_script.id,
                        "content_research": research_task.id if research_task else None,
                    },
                )
                return
            if research_task is None:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="task_dependency_blocked",
                        metadata_json={
                            "downstream": TaskType.SCRIPT_QC.value,
                            "missing": [TaskType.CONTENT_RESEARCH.value],
                        },
                    )
                )
                flow.set_error(
                    content_id,
                    "AGENT_TASK_DEPENDENCY_MISSING",
                    "脚本质检等待已完成的事实研究",
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "content_research")
                return
            flow.create_agent_task(
                content_id,
                TaskType.SCRIPT_QC,
                {
                    "upstream_task_ids": {
                        "script": task.id,
                        "research": research_task.id if research_task else "",
                    },
                    "min_video_seconds": settings.min_video_seconds,
                    "max_video_seconds": settings.max_video_seconds,
                },
                priority=45,
                allow_multiple=True,
            )
            flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "script_qc")
        elif task.task_type == TaskType.SCRIPT_QC:
            script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            research_task = flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)
            declared_script_id = _upstream_task_id(task, "script")
            declared_research_id = _upstream_task_id(task, "research")
            if declared_script_id and (script_task is None or declared_script_id != script_task.id):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="script_qc_script_dependency_is_stale",
                    current_task_ids={
                        "short_script": script_task.id if script_task else None,
                        "content_research": research_task.id if research_task else None,
                    },
                )
                return
            if declared_research_id and (
                research_task is None or declared_research_id != research_task.id
            ):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="script_qc_research_dependency_is_stale",
                    current_task_ids={
                        "short_script": script_task.id if script_task else None,
                        "content_research": research_task.id if research_task else None,
                    },
                )
                return
            missing = []
            if script_task is None:
                missing.append(TaskType.SHORT_SCRIPT.value)
            if research_task is None:
                missing.append(TaskType.CONTENT_RESEARCH.value)
            if missing:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="task_dependency_blocked",
                        metadata_json={
                            "downstream": TaskType.SCRIPT_REVIEW_ASSIST.value,
                            "missing": missing,
                        },
                    )
                )
                flow.set_error(
                    content_id,
                    "AGENT_TASK_DEPENDENCY_MISSING",
                    "脚本人工审核辅助等待完整的脚本和事实研究",
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "short_script")
                return
            if not (task.result_data or {}).get("passed"):
                # The migrated application treats semantic QC as a release gate in manual flows too.
                flow.create_qc_recovery_task(task, script_task)
                flow.set_error(content_id, "SCRIPT_QC_FAILED", "脚本质检未通过")
                return
            flow.create_agent_task(
                content_id,
                TaskType.SCRIPT_REVIEW_ASSIST,
                {
                    "script": script_task.result_data,
                    "script_qc": task.result_data,
                    "research": research_task.result_data,
                    "upstream_task_ids": {
                        "script": script_task.id,
                        "script_qc": task.id,
                        "research": research_task.id,
                    },
                },
                priority=50,
            )
            flow.set_error(content_id, None, None)
            flow.mark_status(content_id, ContentStatus.SCRIPT_REVIEW, "script_review")
        elif task.task_type == TaskType.VISUAL_PLAN:
            script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            current_plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            declared_script_id = _upstream_task_id(task, "script")
            if current_plan is None or current_plan.id != task.id:
                _audit_stale_lineage(
                    session,
                    task,
                    reason="visual_plan_is_not_current",
                    current_task_ids={
                        "visual_plan": current_plan.id if current_plan else None,
                        "short_script": script_task.id if script_task else None,
                    },
                )
                return
            if declared_script_id and (script_task is None or declared_script_id != script_task.id):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="visual_plan_script_dependency_is_stale",
                    current_task_ids={
                        "visual_plan": current_plan.id,
                        "short_script": script_task.id if script_task else None,
                    },
                )
                return
            if script_task is None:
                # 不把缺少上游脚本的 QC 伪装成可执行 Agent 任务；否则会消耗
                # 一轮视觉 Agent token 后才在运行时暴露环境/输入缺失。
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="agent_task",
                        entity_id=task.id,
                        action="task_dependency_blocked",
                        metadata_json={
                            "downstream": TaskType.VISUAL_PLAN_QC.value,
                            "missing": [TaskType.SHORT_SCRIPT.value],
                        },
                    )
                )
                flow.set_error(
                    content_id,
                    "AGENT_TASK_DEPENDENCY_MISSING",
                    "画面计划质检等待已完成的口播脚本",
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "short_script")
                return
            content = session.get(Content, content_id)
            if content is not None and content.production_mode == ProductionMode.IMAGE_ONLY:
                original_plan = dict(task.result_data or {})
                augmented_plan, asset_stats = _ensure_image_only_asset_requests(
                    content=content,
                    settings=settings,
                    plan_task=task,
                    script=script_task.result_data or {},
                )
                if augmented_plan != original_plan:
                    task.result_data = augmented_plan
                    session.add(task)
                    session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id="pipeline",
                            entity_type="content",
                            entity_id=content_id,
                            action="image_only_plan_augmented",
                            metadata_json={
                                "visual_plan_task_id": task.id,
                                **asset_stats,
                            },
                        )
                    )
                    session.commit()
                if asset_stats["asset_count"] < asset_stats["minimum"]:
                    session.add(
                        AuditEvent(
                            actor_type="system",
                            actor_id="pipeline",
                            entity_type="content",
                            entity_id=content_id,
                            action="image_only_asset_shortfall_warning_recorded",
                            metadata_json={
                                "visual_plan_task_id": task.id,
                                **asset_stats,
                            },
                        )
                    )
                    session.commit()
            flow.create_agent_task(
                content_id,
                TaskType.VISUAL_PLAN_QC,
                {
                    "upstream_task_ids": {
                        "visual_plan": task.id,
                        "script": script_task.id,
                    },
                    "scenes": task.input_data.get("scenes", []),
                    "voice_duration_sec": task.input_data.get("voice_duration_sec"),
                    "allowed_layouts": task.input_data.get("allowed_layouts", []),
                    "allowed_asset_ids": task.input_data.get("allowed_asset_ids", []),
                    "context_files": task.input_data.get("context_files", []),
                },
                priority=55,
                allow_multiple=True,
            )
            flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "visual_plan_qc")
        elif task.task_type == TaskType.VISUAL_PLAN_QC:
            plan_task = _current_visual_plan_for_asset_task(session, flow, task)
            latest_plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            if plan_task is None:
                _audit_stale_agent_result(
                    session,
                    task,
                    current_plan_id=latest_plan.id if latest_plan else "missing",
                )
                return
            if not (task.result_data or {}).get("passed"):
                flow.create_qc_recovery_task(task, plan_task)
                return
            flow.set_error(content_id, None, None)
            segments = (plan_task.result_data or {}).get("segments", []) if plan_task else []
            requests = [
                (index, segment["asset_request"])
                for index, segment in enumerate(segments)
                if segment.get("asset_request")
            ]
            if requests and settings.asset_image_enabled:
                script = flow.result_for(content_id, TaskType.SHORT_SCRIPT)
                # V8：整条内容单任务一次产出全部插图；script 只下发一份。
                # 幂等由 dedup_key（content_id + 输入哈希，即画面计划结果）裁决；
                # allow_multiple 保留是因为同 content 下还存在封面/重生图任务。
                reference_paths = _reference_artifact_paths(session, content_id, requests)
                flow.create_agent_task(
                    content_id,
                    TaskType.ASSET_GENERATE_IMAGE,
                    {
                        "requests": [
                            {
                                "segment_index": index,
                                "asset_request": request,
                                # 参考重绘：附原图可访问地址，生图 Agent 以它作参考图输入。
                                **(
                                    {
                                        "reference_artifact_url": (
                                            f"/media/artifacts/{request['reference_artifact_id']}"
                                        )
                                    }
                                    if request.get("reference_artifact_id")
                                    else {}
                                ),
                            }
                            for index, request in requests
                        ],
                        "script": script,
                        "upstream_task_ids": {"visual_plan": plan_task.id},
                        **({"context_files": reference_paths} if reference_paths else {}),
                    },
                    priority=56,
                    allow_multiple=True,
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "asset_generate")
            else:
                qc_input = _asset_qc_input(session, flow, None, plan_task)
                if qc_input:
                    flow.create_agent_task(
                        content_id, TaskType.ASSET_QC, qc_input, priority=58, allow_multiple=True
                    )
                    flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "asset_qc")
                else:
                    flow.mark_status(content_id, ContentStatus.PRODUCING, "render_ready")
        elif task.task_type == TaskType.ASSET_GENERATE_IMAGE:
            if task.input_data.get("asset_purpose") == "cover":
                images = (task.result_data or {}).get("images", [])
                artifact_id = str((images[0].get("artifact_id") if images else "") or "")
                generated = session.get(Artifact, artifact_id) if artifact_id else None
                content = session.get(Content, content_id)
                if generated is None or content is None:
                    raise ValueError("封面生图任务缺少可用 artifact_id")
                base_image = Path(content.content_dir).resolve() / generated.relative_path
                if not base_image.is_file():
                    raise FileNotFoundError(base_image)
                cover_artifact = _build_and_register_cover(
                    session,
                    settings,
                    content,
                    base_image=base_image,
                    action="cover_ai_upgraded",
                )
                _queue_post_package_tasks(
                    session,
                    flow,
                    content,
                    settings,
                    flow.result_for(content_id, TaskType.SHORT_SCRIPT) or {},
                    flow.result_for(content_id, TaskType.CONTENT_RESEARCH) or {},
                    cover_artifact_id=cover_artifact.id,
                )
                return
            # V8：单任务完成后直接按 segment_index 批量进入素材质检，
            # 不再按任务数计数聚合。
            plan_task = _current_visual_plan_for_asset_task(session, flow, task)
            current_plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            if plan_task is None:
                if current_plan is not None:
                    _audit_stale_agent_result(session, task, current_plan_id=current_plan.id)
                else:
                    _audit_stale_agent_result(session, task, current_plan_id="missing")
                return
            qc_input = _asset_qc_input(session, flow, task, plan_task) if plan_task else None
            if qc_input:
                flow.create_agent_task(
                    content_id,
                    TaskType.ASSET_QC,
                    qc_input,
                    priority=58,
                    allow_multiple=True,
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "asset_qc")
            else:
                flow.mark_status(content_id, ContentStatus.PRODUCING, "render_ready")
        elif task.task_type == TaskType.ASSET_QC:
            plan_task = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            if plan_task is None or not plan_task.result_data:
                raise ValueError("Asset QC requires a completed visual plan")
            if (
                _upstream_task_id(task, "visual_plan")
                and _upstream_task_id(task, "visual_plan") != plan_task.id
            ):
                _audit_stale_agent_result(session, task, current_plan_id=plan_task.id)
                return
            if not (task.result_data or {}).get("passed"):
                flow.create_qc_recovery_task(task, plan_task)
                return
            if task.input_data.get("full_generated_assets") is True:
                generation_id = _upstream_task_id(task, "asset_generate_image")
                generation = session.get(AgentTask, generation_id) if generation_id else None
                expected = _asset_qc_input(session, flow, generation, plan_task)
                if (
                    expected is None
                    or expected != task.input_data
                    or stable_hash(task.input_data) != task.input_hash
                ):
                    raise ValueError("Full current asset QC materials/coverage changed")
            asset_tasks = session.exec(
                select(AgentTask)
                .where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                    AgentTask.status == TaskStatus.COMPLETED,
                )
                .order_by(AgentTask.created_at)
            ).all()
            # 按 segment_index 取最新一张：后续重生任务的图覆盖早期图。
            latest_by_segment: dict[int, dict] = {}
            for asset_task in asset_tasks:
                if asset_task.input_data.get("asset_purpose") == "cover":
                    continue
                asset_plan_id = _upstream_task_id(asset_task, "visual_plan")
                if asset_plan_id:
                    if asset_plan_id != plan_task.id:
                        continue
                elif as_utc(asset_task.created_at) < as_utc(plan_task.created_at):
                    continue
                for image in (asset_task.result_data or {}).get("images", []):
                    latest_by_segment[int(image["segment_index"])] = image
            segments = [dict(segment) for segment in plan_task.result_data["segments"]]
            missing_indexes: list[int] = []
            for index, segment in enumerate(segments):
                image = latest_by_segment.get(index)
                if segment.get("asset_request") and image is None:
                    missing_indexes.append(index)
                    continue
                if image is not None:
                    segment.pop("asset_request", None)
                    segment["asset_id"] = image["artifact_id"]

            if missing_indexes:
                flow.set_error(
                    content_id,
                    "ASSET_GENERATION_INCOMPLETE",
                    "仍有画面段缺少可用图片素材："
                    + ",".join(str(index) for index in missing_indexes),
                )
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "asset_qc_missing")
            else:
                plan_task.result_data = {**plan_task.result_data, "segments": segments}
                session.add(plan_task)
                session.commit()
                flow.set_error(content_id, None, None)
                flow.mark_status(content_id, ContentStatus.PRODUCING, "render_ready")
        elif task.task_type == TaskType.CONTENT_QC:
            if task.schema_version == "2":
                verify_acceptance(session, task, task.result_data or {})
                if not (task.result_data or {}).get("passed"):
                    flow.set_error(content_id, "CONTENT_QC_FAILED", "成片验收未通过")
                    return
                flow.set_error(content_id, None, None)
                flow.mark_status(content_id, ContentStatus.FINAL_REVIEW, "final_review")
                return
            declared_upstream = (task.input_data or {}).get("upstream_task_ids") or {}
            current_script = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
            current_plan = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
            declared_script_id = str(declared_upstream.get("script") or "")
            declared_plan_id = str(declared_upstream.get("visual_plan") or "")
            if declared_script_id and (
                current_script is None or declared_script_id != current_script.id
            ):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="content_qc_script_dependency_is_stale",
                    current_task_ids={
                        "short_script": current_script.id if current_script else None,
                        "visual_plan": current_plan.id if current_plan else None,
                    },
                )
                return
            if declared_plan_id and (current_plan is None or declared_plan_id != current_plan.id):
                _audit_stale_lineage(
                    session,
                    task,
                    reason="content_qc_visual_plan_dependency_is_stale",
                    current_task_ids={
                        "short_script": current_script.id if current_script else None,
                        "visual_plan": current_plan.id if current_plan else None,
                    },
                )
                return
            if not (task.result_data or {}).get("passed"):
                flow.set_error(content_id, "CONTENT_QC_FAILED", "成片质检未通过")
                flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "content_qc_failed")
                return
            review_input = dict(task.input_data or {})
            final_artifact = session.exec(
                select(Artifact)
                .where(
                    Artifact.content_id == content_id,
                    Artifact.artifact_type == "final_video",
                )
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
            ).first()
            review_input["video_artifact_id"] = final_artifact.id if final_artifact else None
            review_input["video_url"] = (
                f"/media/artifacts/{final_artifact.id}" if final_artifact else None
            )
            review_input["content_qc"] = task.result_data or {}
            flow.create_agent_task(
                content_id,
                TaskType.FINAL_REVIEW_ASSIST,
                review_input,
                priority=61,
                allow_multiple=True,
            )
            flow.set_error(content_id, None, None)
            flow.mark_status(content_id, ContentStatus.FINAL_REVIEW, "final_review")


def repair_stalled_agent_stage(
    engine: Engine,
    settings: Settings,
    content_id: str,
    *,
    actor_id: str = "admin-console",
) -> tuple[Content, AgentTask]:
    """Reconcile a known orphaned Agent stage after an operator confirms the repair.

    This is intentionally narrow: it repairs an image-only/asset pipeline that has a
    completed image-generation task but no active ASSET_QC task. It never changes a
    content business decision and is idempotent on the generated task input.
    """
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status != ContentStatus.WAITING_AGENT:
            raise ValueError("只有等待 Agent 的内容可以修复当前节点")
        if content.current_stage != "asset_qc":
            raise ValueError("当前节点不是可自动补回的素材质检节点")

        active = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if active is not None:
            return content, active

        flow = workflow(session, settings)
        image_tasks = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                AgentTask.status == TaskStatus.COMPLETED,
            )
            .order_by(AgentTask.completed_at.desc(), AgentTask.created_at.desc())
        ).all()
        image_task = next(
            (
                candidate
                for candidate in image_tasks
                if candidate.input_data.get("asset_purpose") != "cover"
                and (candidate.result_data or {}).get("images")
            ),
            None,
        )
        if image_task is None:
            raise ValueError("找不到已完成的批量生图结果，无法补回素材质检")
        plan_task = _current_visual_plan_for_asset_task(session, flow, image_task)
        if plan_task is None:
            raise ValueError("批量生图结果属于旧画面计划，不能直接恢复")
        qc_input = _asset_qc_input(session, flow, image_task, plan_task)
        if qc_input is None:
            raise ValueError("批量生图结果缺少可质检的图片")
        qc_task = flow.create_agent_task(
            content_id,
            TaskType.ASSET_QC,
            qc_input,
            priority=58,
            allow_multiple=True,
        )
        flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "asset_qc")
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="stalled_agent_stage_repaired",
                metadata_json={
                    "stage": "asset_qc",
                    "source_task_id": image_task.id,
                    "repair_task_id": qc_task.id,
                    "visual_plan_task_id": plan_task.id,
                },
            )
        )
        session.commit()
        session.refresh(content)
        session.refresh(qc_task)
        return content, qc_task


def repair_stalled_content_qc(
    engine: Engine,
    settings: Settings,
    content_id: str,
    *,
    actor_id: str = "admin-console",
) -> tuple[Content, AgentTask]:
    """Rebuild one orphaned CONTENT_QC task from its immutable review bundle.

    A worker may have materialized the review frames successfully while the Agent
    interpreted the old task's context paths incorrectly.  The original task and
    result stay immutable; this creates a new task only after validating the current
    script, visual plan, video artifact, and every declared review frame.
    """
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.WAITING_AGENT, ContentStatus.FAILED}:
            raise ValueError("只有等待 Agent 或失败内容可以补回成片质检")
        if content.current_stage not in {"content_qc", "content_qc_failed", "recovery_blocked"}:
            raise ValueError("当前节点不是可补回的成片质检节点")
        active = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.CONTENT_QC,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if active is not None:
            return content, active

        flow = workflow(session, settings)
        script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        plan_task = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
        research = flow.result_for(content_id, TaskType.CONTENT_RESEARCH)
        if script_task is None or plan_task is None or not research:
            raise ValueError("成片质检缺少当前脚本、画面计划或研究结果")
        final_artifact = session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content_id,
                Artifact.artifact_type == "final_video",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        if final_artifact is None:
            raise ValueError("成片质检缺少 final_video artifact")
        root = Path(content.content_dir).resolve()
        final_path = (root / final_artifact.relative_path).resolve()
        if not final_path.is_file() or not final_path.is_relative_to(root):
            raise ValueError("成片质检的 final_video 文件不存在")

        source = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.CONTENT_QC,
                AgentTask.status == TaskStatus.FAILED,
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if source is None:
            raise ValueError("找不到可重建的成片质检失败任务")
        source_input = dict(source.input_data or {})
        context_files = [
            str(item).replace("\\", "/")
            for item in (source_input.get("context_files") or [])
            if str(item).strip()
        ]
        if not context_files:
            raise ValueError("成片质检失败任务没有声明 review frames")
        for relative in context_files:
            candidate = Path(relative)
            target = (root / candidate).resolve()
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or not target.is_relative_to(root)
            ):
                raise ValueError("成片质检上下文路径不安全")
            if not target.is_file():
                raise ValueError(f"成片质检 review frame 不存在：{relative}")

        input_data = dict(source_input)
        input_data.update(
            {
                "script": script_task.result_data or {},
                "research": research,
                "video_artifact_id": final_artifact.id,
                "video_url": f"/media/artifacts/{final_artifact.id}",
                "context_files": context_files,
                "upstream_task_ids": {
                    "script": script_task.id,
                    "visual_plan": plan_task.id,
                },
                "manual_repair_source_task_id": source.id,
                "manual_repair_round": int(source_input.get("manual_repair_round") or 0) + 1,
            }
        )
        qc_task = flow.create_agent_task(
            content_id,
            TaskType.CONTENT_QC,
            input_data,
            priority=60,
            allow_multiple=True,
        )
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "content_qc"
        content.error_code = "CONTENT_QC_REPAIR_STARTED"
        content.error_message = "服务端已重建成片质检任务，等待 Agent 按 context/ 路径复核"
        content.updated_at = utc_now()
        session.add(content)
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="content_qc_stage_repaired",
                metadata_json={
                    "source_task_id": source.id,
                    "repair_task_id": qc_task.id,
                    "script_task_id": script_task.id,
                    "visual_plan_task_id": plan_task.id,
                    "frame_count": len(context_files),
                },
            )
        )
        session.commit()
        session.refresh(content)
        session.refresh(qc_task)
        return content, qc_task


def repair_stalled_asset_generation(
    engine: Engine,
    settings: Settings,
    content_id: str,
    *,
    actor_id: str = "admin-console",
) -> tuple[Content, AgentTask]:
    """Create a fresh, lineage-bound image task after an exhausted asset failure."""
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.WAITING_AGENT, ContentStatus.FAILED}:
            raise ValueError("只有等待 Agent 或失败内容可以重建生图任务")
        if content.current_stage not in {
            "asset_generate",
            "asset_regenerate",
            "asset_qc_failed",
            "recovery_blocked",
        }:
            raise ValueError("当前节点不是可重建的生图节点")
        active = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                AgentTask.status.in_([TaskStatus.PENDING, TaskStatus.LEASED, TaskStatus.SUBMITTED]),
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if active is not None:
            return content, active

        flow = workflow(session, settings)
        script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        plan_task = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
        if script_task is None or plan_task is None:
            raise ValueError("生图任务缺少当前脚本或画面计划")
        source = session.exec(
            select(AgentTask)
            .where(
                AgentTask.content_id == content_id,
                AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                AgentTask.status == TaskStatus.FAILED,
            )
            .order_by(AgentTask.created_at.desc())
        ).first()
        if source is None:
            raise ValueError("找不到可重建的生图失败任务")
        source_input = dict(source.input_data or {})
        requests = source_input.get("requests")
        if not isinstance(requests, list) or not requests:
            raise ValueError("生图失败任务没有可复用的定向 requests")
        source_plan_id = str((source_input.get("upstream_task_ids") or {}).get("visual_plan") or "")
        if source_plan_id and source_plan_id != plan_task.id:
            raise ValueError("生图失败任务属于旧画面计划，不能直接重建")

        input_data = {
            "upstream_task_ids": {"visual_plan": plan_task.id},
            "requests": requests,
            "script": script_task.result_data or {},
            "manual_repair_source_task_id": source.id,
            "manual_repair_round": int(source_input.get("manual_repair_round") or 0) + 1,
        }
        for key in ("qc_issues", "previous_artifact_ids"):
            if source_input.get(key) is not None:
                input_data[key] = source_input[key]
        task = flow.create_agent_task(
            content_id,
            TaskType.ASSET_GENERATE_IMAGE,
            input_data,
            priority=59,
            allow_multiple=True,
        )
        content.business_status = ContentStatus.WAITING_AGENT
        content.current_stage = "asset_generate"
        content.error_code = "ASSET_REPAIR_STARTED"
        content.error_message = "服务端已创建新的生图任务版本，等待 Agent 生成并上传 Artifact"
        content.updated_at = utc_now()
        session.add(content)
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="asset_generation_repaired",
                metadata_json={
                    "source_task_id": source.id,
                    "repair_task_id": task.id,
                    "visual_plan_task_id": plan_task.id,
                    "request_count": len(requests),
                },
            )
        )
        session.commit()
        session.refresh(content)
        session.refresh(task)
        return content, task


def approve_script(engine: Engine, settings: Settings, content_id: str) -> Content:
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        flow = workflow(session, settings)
        _verify_rights(session, content)
        _require_current_qc(
            session, content_id, TaskType.SCRIPT_QC,
            {"script": flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT),
             "research": flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)},
        )
        script = flow.result_for(content_id, TaskType.SHORT_SCRIPT)
        qc = flow.result_for(content_id, TaskType.SCRIPT_QC)
        if not script or qc is None:
            raise ValueError("Script and a completed script QC are required")
        root = Path(content.content_dir).resolve()
        executor = MediaExecutor(root)
        voice_options = {
            "voice": settings.tts_voice,
            "rate": settings.tts_rate,
            "volume": settings.tts_volume,
            "provider": settings.tts_provider,
            "provider_endpoint": settings.tts_provider_endpoint,
            "target_duration_sec": float(script.get("estimated_duration_sec") or 0) or None,
        }
        provider_token = settings.cosyvoice_token.get_secret_value()
        if provider_token:
            voice_options["provider_token"] = provider_token
        tts_result = log_stage(
            session,
            content.id,
            "voice",
            lambda: executor.synthesize_voice(
                script["full_narration"],
                **voice_options,
            ),
        )
        voice_path = tts_result.audio_path
        voice_duration = tts_result.duration_sec
        timing_evidence = executor.visual_timing_evidence(voice_path, voice_duration)
        if timing_evidence is None:
            raise NonRetryableMediaError("Generated voice timing sidecar is missing")
        if not settings.min_video_seconds <= voice_duration <= settings.max_video_seconds:
            message = (
                f"voice duration {voice_duration:.1f}s is outside bounds "
                f"[{settings.min_video_seconds:.0f}s, {settings.max_video_seconds:.0f}s]; "
                "reject the script for a rewrite"
            )
            flow.set_error(content_id, "VOICE_DURATION", message)
            flow.mark_status(content_id, ContentStatus.SCRIPT_REVIEW, "voice_gate")
            raise ValueError(message)
        voice_artifact = register_artifact(
            session,
            content=content,
            executor=executor,
            path=voice_path,
            artifact_type="voice",
            stage="voice",
        )
        voice_binding = {
            "id": voice_artifact.id,
            "relative_path": voice_artifact.relative_path,
            "sha256": voice_artifact.sha256,
            "size_bytes": voice_artifact.size_bytes,
        }
        flow.set_error(content_id, None, None)
        # 重新配音后旧画面计划按新时长作废重建。
        script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        if script_task is None:
            raise ValueError("Script task reference is required")
        previous_plan = (script_task.input_data or {}).get("previous_plan") or flow.result_for(
            content_id, TaskType.VISUAL_PLAN
        )
        failed_content_qc = flow.latest_failed_content_qc(content_id)
        content_qc_rework = {}
        if failed_content_qc is not None:
            content_qc_rework = {
                "qc_issues": (failed_content_qc.result_data or {}).get("issues", []),
                "previous_plan": previous_plan or {},
                "previous_qc_task_id": failed_content_qc.id,
            }
        flow.invalidate_visual_plan(content_id)
        image_only = content.production_mode == ProductionMode.IMAGE_ONLY
        generated_asset_ids = [
            artifact.id
            for artifact in session.exec(
                select(Artifact).where(
                    Artifact.content_id == content_id,
                    Artifact.artifact_type.in_(["agent_generated_image", "agent_generated_video"]),
                )
            ).all()
        ]
        if image_only:
            # 图文模式：全部片段为 image_card，可用素材是已下载的网页原图，
            # 生图段（参考重绘 + 纯生图）受管理台预算限制。
            source_images = source_image_index(content)
            source_ids = [str(item["artifact_id"]) for item in source_images]
            budget = settings.image_only_gen_budget if settings.asset_image_enabled else 0
            plan_input = {
                "upstream_task_ids": {"script": script_task.id},
                "voice_duration_sec": voice_duration,
                "visual_timing_evidence": timing_evidence,
                "voice_artifact": voice_binding,
                "scenes": [],
                "allowed_layouts": ["image_card"],
                "asset_image_enabled": settings.asset_image_enabled,
                "production_mode": ProductionMode.IMAGE_ONLY.value,
                "image_only_gen_budget": budget,
                "source_images": source_images,
                "context_files": [str(item["relative_path"]) for item in source_images],
                "allowed_asset_ids": [*source_ids, *generated_asset_ids],
                "allowed_reference_artifact_ids": source_ids,
                **content_qc_rework,
            }
            flow.create_agent_task(content_id, TaskType.VISUAL_PLAN, plan_input, priority=50)
            return flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "visual_plan")
        scene_data = json.loads((root / "scenes" / "scenes.json").read_text(encoding="utf-8"))
        scene_tasks = flow.scene_analysis_tasks(content_id)
        if not scene_tasks:
            raise ValueError("Script and scene task references are required")
        flow.create_agent_task(
            content_id,
            TaskType.VISUAL_PLAN,
            {
                "upstream_task_ids": {
                    "script": script_task.id,
                    "scene_analysis": (
                        [item.id for item in scene_tasks]
                        if len(scene_tasks) > 1
                        else scene_tasks[0].id
                    ),
                },
                "voice_duration_sec": voice_duration,
                "visual_timing_evidence": timing_evidence,
                "voice_artifact": voice_binding,
                "scenes": scene_data["scenes"],
                "production_mode": ProductionMode.FOOTAGE.value,
                "allowed_layouts": [
                    "center_crop",
                    "blur_background",
                    "fit",
                    *(["image_card"] if settings.asset_image_enabled else []),
                ],
                "asset_image_enabled": settings.asset_image_enabled,
                "allowed_asset_ids": generated_asset_ids,
                "context_files": [
                    str(scene["thumbnail"])
                    for scene in scene_data["scenes"]
                    if scene.get("thumbnail")
                ],
                **content_qc_rework,
            },
            priority=50,
        )
        return flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "visual_plan")


def _looks_translated(title_zh: str) -> bool:
    """中文标题至少要含一个中日韩字符，否则就是把英文原标题抄了回来。"""
    return any("一" <= char <= "鿿" for char in title_zh)


_RENDER_SEGMENT_FIELDS = {
    "scene_id",
    "asset_id",
    "narration_start_sec",
    "narration_end_sec",
    "source_start_sec",
    "source_end_sec",
    "source_redactions",
    "layout",
    "purpose",
    "motion",
    "transition",
    "focus_x",
    "focus_y",
}


def _render_plan_data(plan: dict) -> dict:
    """Adapt semantic planning output to the strict deterministic renderer contract."""
    return {
        "segments": [
            {key: value for key, value in segment.items() if key in _RENDER_SEGMENT_FIELDS}
            for segment in plan.get("segments", [])
        ]
    }


def _valid_duration(value: object) -> float | None:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if isfinite(duration) and duration > 0 else None


def _render_voice_path(
    session: Session, content: Content, timing_evidence: dict | None, voice_binding: dict | None = None,
) -> Path:
    """Resolve frozen voice bytes through their real Artifact, including verified legacy pairs."""
    root = Path(content.content_dir).resolve()
    voices = session.exec(
        select(Artifact).where(Artifact.content_id == content.id, Artifact.artifact_type == "voice")
    ).all()
    # Only the single registered legacy alias can predate frozen voice identity.
    if timing_evidence is None and (
        voice_binding is not None or len(voices) != 1 or voices[0].relative_path != "audio/narration.mp3"
    ):
        raise NonRetryableMediaError("Visual plan has no frozen voice binding")
    for artifact in voices:
        if voice_binding is not None and (
            not isinstance(voice_binding, dict)
            or any(voice_binding.get(key) != getattr(artifact, key)
                   for key in ("id", "relative_path", "sha256", "size_bytes"))
        ):
            continue
        if timing_evidence is not None and artifact.sha256 != timing_evidence.get("voice_sha256"):
            continue
        path = (root / artifact.relative_path).resolve()
        if (
            not path.is_relative_to(root) or not path.is_file()
            or path.stat().st_size != artifact.size_bytes or sha256_file(path) != artifact.sha256
        ):
            continue
        if timing_evidence is not None:
            boundaries = path.with_name("word_boundaries.json").resolve()
            if (
                not boundaries.is_relative_to(root) or not boundaries.is_file()
                or sha256_file(boundaries) != timing_evidence.get("boundaries_sha256")
            ):
                continue
        return path
    raise NonRetryableMediaError("Approved voice Artifact or its frozen timing pair is missing or changed")


def render_content(engine: Engine, settings: Settings, content_id: str) -> Content:
    _require_media_tools("ffmpeg", "ffprobe")
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        flow = workflow(session, settings)
        plan_task = flow.completed_task_for(content_id, TaskType.VISUAL_PLAN)
        script_task = flow.completed_task_for(content_id, TaskType.SHORT_SCRIPT)
        _verify_rights(session, content)
        _require_current_qc(
            session, content_id, TaskType.SCRIPT_QC,
            {"script": script_task, "research": flow.completed_task_for(content_id, TaskType.CONTENT_RESEARCH)},
        )
        _require_current_qc(
            session, content_id, TaskType.VISUAL_PLAN_QC,
            {"visual_plan": plan_task, "script": script_task},
        )
        plan = plan_task.result_data if plan_task else None
        script = script_task.result_data if script_task else None
        if not plan or not script:
            missing = "、".join(
                name for name, value in (("画面计划", plan), ("口播脚本", script)) if not value
            )
            # 前置产物缺失是确定性失败：重试不会凭空长出产物。
            raise NonRetryableMediaError(f"渲染前置产物缺失（{missing}），请先完成上游阶段")
        render_plan_data = _render_plan_data(plan)
        if ("visual_timing_evidence" in plan_task.input_data or "voice_artifact" in plan_task.input_data) and (
            not isinstance(plan_task.input_data.get("visual_timing_evidence"), dict)
            or ("voice_artifact" in plan_task.input_data
                and not isinstance(plan_task.input_data["voice_artifact"], dict))
            or stable_hash(plan_task.input_data) != plan_task.input_hash
        ):
            raise NonRetryableMediaError("Visual plan timing input hash mismatch")
        root = Path(content.content_dir).resolve()
        executor = MediaExecutor(root)
        scenes_file = root / "scenes" / "scenes.json"
        # 图文模式没有原片与场景清单：画面全由图片片段构成。
        scenes = (
            json.loads(scenes_file.read_text(encoding="utf-8"))["scenes"]
            if scenes_file.is_file()
            else []
        )

        def do_render() -> tuple[Path, Path]:
            image_artifacts = session.exec(
                select(Artifact).where(
                    Artifact.content_id == content_id,
                    Artifact.artifact_type.in_(["agent_generated_image", "source_image"]),
                )
            ).all()
            artifact_paths = {
                artifact.id: root / artifact.relative_path for artifact in image_artifacts
            }
            selected_bgm = None
            bgm_mood = resolve_bgm_mood(script)
            if settings.bgm_enabled:
                selected_bgm = pick_track(
                    load_catalog(settings.data_dir / "bgm"),
                    track_id=settings.bgm_track_id,
                    seed=content_id,
                    mood=bgm_mood,
                )
            arguments = {
                "video_path": root / "source" / "original.mp4",
                "voice_path": _render_voice_path(
                    session, content, plan_task.input_data.get("visual_timing_evidence"),
                    plan_task.input_data.get("voice_artifact"),
                ),
                "narration": script["full_narration"],
                "plan_data": render_plan_data,
                "scenes": scenes,
                "assets": artifact_paths,
                "generated_image_ids": {
                    artifact.id
                    for artifact in image_artifacts
                    if artifact.artifact_type == "agent_generated_image"
                },
                **(
                    {"visual_timing_evidence": plan_task.input_data["visual_timing_evidence"]}
                    if "visual_timing_evidence" in plan_task.input_data
                    else {}
                ),
                "bgm_path": None if selected_bgm is None else selected_bgm.path,
                "bgm_volume": settings.bgm_volume,
                "bgm_meta": None
                if selected_bgm is None
                else {
                    "id": selected_bgm.id,
                    "title": selected_bgm.title,
                    "license": selected_bgm.license,
                    "mood": selected_bgm.mood or bgm_mood,
                },
            }
            return executor.render(**arguments, encoder=settings.video_encoder)

        final_path, preview_path = log_stage(session, content.id, "render", do_render)
        for path, artifact_type in ((final_path, "final_video"), (preview_path, "preview_video")):
            register_artifact(
                session,
                content=content,
                executor=executor,
                path=path,
                artifact_type=artifact_type,
                stage="render",
            )
        expected_duration_sec = _valid_duration(
            (plan_task.input_data or {}).get("voice_duration_sec") if plan_task else None
        )
        if expected_duration_sec is None:
            expected_duration_sec = _valid_duration(script.get("estimated_duration_sec"))
        qc = executor.qc(
            final_path,
            min_seconds=settings.min_video_seconds,
            max_seconds=settings.max_video_seconds,
            plan_data=render_plan_data,
            expected_duration_sec=expected_duration_sec,
        )
        if not qc["passed"]:
            duration_evidence = qc.get("duration_evidence") or {}
            duration_error = any(
                "differs from target" in str(error) or "differs from timeline end" in str(error)
                for error in qc.get("errors", [])
            )
            if (
                duration_error
                and any(
                    duration_evidence.get(key) is not None
                    for key in ("target_duration_sec", "timeline_end_sec")
                )
                and plan_task is not None
            ):
                recovery = flow.create_recovery_task(
                    plan_task,
                    failure_class="BUSINESS_QC",
                    code="RENDER_DURATION_MISMATCH",
                    message="；".join(qc.get("errors", [])),
                    validation_errors=[
                        {
                            "field": "duration",
                            "message": "成片实际时长与脚本目标或画面时间轴不一致",
                            "evidence": duration_evidence,
                        }
                    ],
                    repair_target_task_id=plan_task.id,
                    actor_id="pipeline",
                )
                if recovery is not None or flow.recovery_capacity_deferred:
                    session.refresh(content)
                    return content
            flow.set_error(content_id, "TECHNICAL_QC", "; ".join(qc["errors"]))
            return flow.mark_status(content_id, ContentStatus.FAILED, "technical_qc")
        flow.set_error(content_id, None, None)
        final_artifact = session.exec(
            select(Artifact)
            .where(
                Artifact.content_id == content_id,
                Artifact.artifact_type == "final_video",
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).first()
        review_input = {
            "script": script,
            "video_artifact_id": final_artifact.id if final_artifact else None,
            "video_url": (f"/media/artifacts/{final_artifact.id}" if final_artifact else None),
            "technical_qc": _technical_qc_summary(qc),
            "upstream_task_ids": {
                **({"script": script_task.id} if script_task else {}),
                **({"visual_plan": plan_task.id} if plan_task else {}),
            },
        }
        review_input["context_files"] = log_stage(
            session,
            content.id,
            "content_qc_frames",
            lambda: executor.extract_review_frames(final_path, plan_data=render_plan_data),
        )
        review_input["subtitles"] = script["full_narration"]
        review_input["research"] = flow.result_for(content_id, TaskType.CONTENT_RESEARCH)
        # Both explicit manual selection and Handsfree own a real v2 subject.
        # Ownership never substitutes for the independent audio/source/QC evidence.
        review_input["acceptance_contract_version"] = 2
        review_input["acceptance_origin"] = acceptance_origin(session, content_id)
        review_input["source_context_version"] = 1
        review_input["source_context"] = source_context(
            session, content_id, review_input["upstream_task_ids"]
        )
        review_input["acceptance_subject"] = acceptance_subject(
            session,
            content_id,
            {
                **review_input["upstream_task_ids"],
                "video_artifact_id": review_input["video_artifact_id"],
            },
            source_context_version=1,
        )
        research_id = review_input["acceptance_subject"]["research_task_id"]
        review_input["research"] = session.get(AgentTask, research_id).result_data
        receipt = root / "render" / "acceptance-input.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(review_input, ensure_ascii=False), encoding="utf-8")
        register_artifact(
            session,
            content=content,
            executor=executor,
            path=receipt,
            artifact_type="acceptance_input",
            stage="render",
        )
        return _issue_acceptance(session, settings, content, review_input)


def _issue_acceptance(
    session: Session,
    settings: Settings,
    content: Content,
    review_input: dict,
) -> Content:
    """Evidence/environment failure is media work, never a semantic repair round."""
    flow = workflow(session, settings)
    refs = {
        **review_input["upstream_task_ids"],
        "video_artifact_id": review_input["video_artifact_id"],
    }
    try:
        current = acceptance_subject(session, content.id, refs, source_context_version=1)
        if current != review_input["acceptance_subject"]:
            raise ValueError("ACCEPTANCE_MATERIALS_CHANGED")
    except ValueError:
        flow.set_error(
            content.id, "ACCEPTANCE_MATERIALS_CHANGED", "待验收成片依赖已变化，关闭旧媒体证据快照"
        )
        return flow.mark_status(content.id, ContentStatus.FAILED, "audio_evidence_closed")
    artifact = session.get(Artifact, refs["video_artifact_id"])
    executor = MediaExecutor(Path(content.content_dir).resolve())

    def collect_audio():
        evidence = executor.final_audio_evidence(
            executor.root / artifact.relative_path,
            asr_model=settings.asr_model,
            asr_device=settings.asr_device,
            asr_compute_type=settings.asr_compute_type,
        )
        if (
            evidence.get("asr_status") != "available"
            or evidence.get("signal_status") != "available"
            or not evidence.get("segments")
        ):
            raise RuntimeError("AUDIO_EVIDENCE_UNAVAILABLE")
        return evidence

    try:
        audio = log_stage(
            session,
            content.id,
            "final_audio_evidence",
            collect_audio,
            input_hash=stable_hash(
                {
                    "video_sha256": artifact.sha256,
                    "model": settings.asr_model,
                    "device": settings.asr_device,
                    "compute": settings.asr_compute_type,
                }
            ),
        )
    except (RuntimeError, OSError, ValueError):
        flow.set_error(
            content.id,
            "AUDIO_EVIDENCE_UNAVAILABLE",
            "成片独立音频证据未就绪；保留成片，自动补做媒体证据，不消耗内容修复预算",
        )
        return flow.mark_status(content.id, ContentStatus.WAITING_AGENT, "audio_evidence_waiting")
    begin_immediate(session)
    try:
        current = acceptance_subject(session, content.id, refs, source_context_version=1)
        if current != review_input["acceptance_subject"]:
            raise ValueError("ACCEPTANCE_MATERIALS_CHANGED")
    except ValueError:
        flow.set_error(
            content.id, "ACCEPTANCE_MATERIALS_CHANGED", "音频证据生成期间依赖变化，关闭旧快照"
        )
        return flow.mark_status(content.id, ContentStatus.FAILED, "audio_evidence_closed")
    complete_input = {
        **review_input,
        "audio_evidence_version": 1,
        "audio_evidence": audio,
        "acceptance_subject": acceptance_subject(
            session, content.id, refs, source_context_version=1, audio_evidence=audio
        ),
    }
    flow.create_agent_task(
        content.id,
        TaskType.CONTENT_QC,
        complete_input,
        priority=60,
        allow_multiple=True,
        schema_version="2",
        handsfree=is_handsfree_content(session, content.id),
    )
    flow.set_error(content.id, None, None)
    return flow.mark_status(content.id, ContentStatus.WAITING_AGENT, "content_qc")


def transition_pending_acceptance_evidence(session: Session, settings: Settings) -> None:
    """Supersede only never-executed legacy v2 offers; preserve every snapshot/result."""
    if not settings.handsfree_enabled or not settings.handsfree_auto_review_enabled:
        return
    task_ids = session.exec(
        select(AgentTask.id).where(
            AgentTask.task_type == TaskType.CONTENT_QC,
            AgentTask.schema_version == "2",
            AgentTask.status == TaskStatus.PENDING,
            AgentTask.attempt == 0,
        )
    ).all()
    for task_id in task_ids:
        begin_immediate(session)
        task = session.get(AgentTask, task_id)
        if (
            task.status != TaskStatus.PENDING
            or task.attempt != 0
            or task.assigned_agent_id
            or "audio_evidence_version" in task.input_data
            or not task.content_id
            or not is_handsfree_content(session, task.content_id)
        ):
            continue
        content = session.get(Content, task.content_id)
        if (
            content is None
            or content.business_status != ContentStatus.WAITING_AGENT
            or content.current_stage != "content_qc"
        ):
            continue
        try:
            verify_acceptance(
                session,
                task,
                {"subject": task.input_data.get("acceptance_subject"), "passed": False},
            )
        except (ValueError, OSError, TypeError, KeyError):
            continue
        identity = task.input_data["acceptance_subject"]
        failed = next(
            (
                previous
                for previous in session.exec(
                    select(AgentTask).where(
                        AgentTask.content_id == content.id,
                        AgentTask.task_type == TaskType.CONTENT_QC,
                        AgentTask.status.in_([TaskStatus.COMPLETED, TaskStatus.CANCELLED]),
                    )
                ).all()
                if (previous.result_data or {}).get("passed") is False
                and all(
                    previous.input_data.get("acceptance_subject", {}).get(key) == identity.get(key)
                    for key in (
                        "video_sha256",
                        "script_sha256",
                        "visual_plan_sha256",
                        "research_sha256",
                    )
                )
            ),
            None,
        )
        flow = workflow(session, settings)
        if failed:
            # Do not silently re-review known failed bytes. Existing controller
            # consumes the unchanged completed failure with its original budget.
            content.business_status = ContentStatus.FINAL_REVIEW
            content.current_stage = "final_review"
            content.updated_at = utc_now()
            session.add(content)
            flow.tasks.cancel(task.id, actor_id="handsfree-coordinator", actor_type="system")
            plan_ref = failed.input_data.get("upstream_task_ids", {}).get("visual_plan")
            target = session.get(AgentTask, plan_ref) if isinstance(plan_ref, str) else None
            if (
                target is not None
                and target.content_id == content.id
                and target.task_type == TaskType.VISUAL_PLAN
                and target.status == TaskStatus.COMPLETED
                and stable_hash(target.input_data) == target.input_hash
            ):
                flow.create_qc_recovery_task(failed, target, actor_id="handsfree-coordinator")
            else:
                flow.set_error(
                    content.id, "ACCEPTANCE_MATERIALS_CHANGED", "既有失败的精确返工目标不可用"
                )
                flow.mark_status(content.id, ContentStatus.FAILED, "audio_evidence_closed")
            continue
        review_input = deepcopy(task.input_data)
        refs = {
            **review_input["upstream_task_ids"],
            "video_artifact_id": review_input["video_artifact_id"],
        }
        review_input["source_context_version"] = 1
        review_input["source_context"] = source_context(session, content.id, refs)
        review_input["acceptance_subject"] = acceptance_subject(
            session, content.id, refs, source_context_version=1
        )
        review_input["script"] = session.get(AgentTask, refs["script"]).result_data
        review_input["research"] = session.get(
            AgentTask, review_input["acceptance_subject"]["research_task_id"]
        ).result_data
        executor = MediaExecutor(Path(content.content_dir).resolve())
        receipt = executor.root / "render" / "acceptance-input.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(review_input, ensure_ascii=False), encoding="utf-8")
        receipt_artifact = Artifact(
            content_id=content.id,
            **executor.artifact_metadata(receipt, "acceptance_input", "render"),
        )
        session.add(receipt_artifact)
        content.current_stage = "audio_evidence_waiting"
        content.error_code = "AUDIO_EVIDENCE_UNAVAILABLE"
        content.error_message = "旧未执行验收已撤销，同一成片自动补充独立媒体证据"
        content.updated_at = utc_now()
        session.add(content)
        session.add(
            AuditEvent(
                actor_type="system",
                actor_id="handsfree-coordinator",
                entity_type="content",
                entity_id=content.id,
                action="handsfree_acceptance_evidence_superseded",
                metadata_json={
                    "cancelled_task_id": task.id,
                    "original_input_hash": task.input_hash,
                    "video_artifact_id": refs["video_artifact_id"],
                    "receipt_artifact_id": receipt_artifact.id,
                },
            )
        )
        # cancel commits the receipt, transition and system audit atomically with
        # its status change. SQLite claim cannot lease the old offer in between.
        flow.tasks.cancel(task.id, actor_id="handsfree-coordinator", actor_type="system")
    session.commit()


def resume_content_acceptance(engine: Engine, settings: Settings, content_id: str) -> Content:
    """Resume ONLY a hash-verified, server-owned render receipt, without rendering."""
    with Session(engine) as session:
        begin_immediate(session)
        content = session.get(Content, content_id)
        if (
            content is None
            or (content.business_status, content.current_stage) not in {
                (ContentStatus.WAITING_AGENT, "audio_evidence_waiting"),
                (ContentStatus.PRODUCING, "audio_evidence_running"),
            }
        ):
            return content
        # Caller holds the content OS lock: a live collector cannot be mistaken
        # for an orphan after a crash. Revalidate the same receipt before retrying.
        acceptance_origin(session, content_id)
        receipts = session.exec(
            select(Artifact).where(
                Artifact.content_id == content_id,
                Artifact.artifact_type == "acceptance_input",
                Artifact.relative_path == "render/acceptance-input.json",
            )
        ).all()
        root = Path(content.content_dir).resolve()
        path = root / "render" / "acceptance-input.json"
        if (
            not path.is_file()
            or not path.resolve().is_relative_to(root)
            or sum(
                a.sha256 == sha256_file(path) and a.size_bytes == path.stat().st_size
                for a in receipts
            )
            != 1
        ):
            flow = workflow(session, settings)
            flow.set_error(
                content_id, "ACCEPTANCE_RECEIPT_INVALID", "媒体验收快照丢失或变化，已关闭"
            )
            return flow.mark_status(content_id, ContentStatus.FAILED, "audio_evidence_closed")
        try:
            review_input = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(review_input, dict)
                or type(review_input.get("acceptance_contract_version")) is not int
                or review_input.get("acceptance_contract_version") != 2
                or review_input.get("acceptance_origin") != acceptance_origin(session, content_id)
                or type(review_input.get("source_context_version")) is not int
                or review_input.get("source_context_version") != 1
                or not isinstance(review_input.get("source_context"), dict)
                or not isinstance(review_input.get("upstream_task_ids"), dict)
                or any(not isinstance(review_input["upstream_task_ids"].get(key), str)
                       for key in ("script", "visual_plan"))
                or not isinstance(review_input.get("acceptance_subject"), dict)
                or not isinstance(review_input.get("video_artifact_id"), str)
            ):
                raise ValueError("Invalid v2 acceptance receipt")
        except (ValueError, TypeError, OSError):
            flow = workflow(session, settings)
            flow.set_error(content_id, "ACCEPTANCE_RECEIPT_INVALID", "媒体验收快照协议无效，已关闭")
            return flow.mark_status(content_id, ContentStatus.FAILED, "audio_evidence_closed")
        # Claim this deterministic media phase atomically, before log_stage's
        # commits release the DB lock. A second consumer sees running and exits.
        workflow(session, settings).mark_status(
            content_id, ContentStatus.PRODUCING, "audio_evidence_running"
        )
        return _issue_acceptance(
            session, settings, content, review_input
        )


def approve_final(engine: Engine, settings: Settings, content_id: str) -> Content:
    _require_media_tools("ffmpeg", "ffprobe")
    with Session(engine) as session:
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        settings = effective_settings(session, settings)
        flow = workflow(session, settings)
        verify_publish_ready(session, settings, content_id)
        acceptance = flow.completed_task_for(content_id, TaskType.CONTENT_QC)
        if acceptance is not None and acceptance.schema_version == "2":
            verify_acceptance(session, acceptance, acceptance.result_data or {})
            decision = ContentAcceptanceResult.model_validate(acceptance.result_data)
            if not decision.passed:
                raise NonRetryableMediaError("成片验收未通过，禁止打包")
        script = flow.result_for(content_id, TaskType.SHORT_SCRIPT)
        research = flow.result_for(content_id, TaskType.CONTENT_RESEARCH)
        if not script or not research:
            missing = "、".join(
                name for name, value in (("口播脚本", script), ("事实研究", research)) if not value
            )
            raise NonRetryableMediaError(f"打包前置产物缺失（{missing}），请先完成上游阶段")
        executor = MediaExecutor(Path(content.content_dir).resolve())
        package_dir = log_stage(
            session,
            content.id,
            "package",
            lambda: executor.package(
                {
                    "content_id": content.id,
                    "title_options": script["title_options"],
                    "cover_title": _effective_cover_title(content, script),
                    "narration": script["full_narration"],
                    "sources": research["primary_sources"] + research["secondary_sources"],
                }
            ),
        )
        register_artifact(
            session,
            content=content,
            executor=executor,
            path=package_dir / "final.mp4",
            artifact_type="publish_video",
            stage="package",
        )
        if _cover_ai_required(settings):
            cover_task = _create_cover_generation_task(flow, content, settings, script)
            content = flow.mark_status(content_id, ContentStatus.WAITING_AGENT, "cover_generate")
            session.add(
                AuditEvent(
                    actor_type="system",
                    actor_id="pipeline",
                    entity_type="content",
                    entity_id=content_id,
                    action="cover_ai_pending",
                    metadata_json={
                        "task_id": cover_task.id,
                        "style_preset": settings.cover_style_preset,
                    },
                )
            )
            session.commit()
        else:
            try:
                cover_artifact = _build_and_register_cover(session, settings, content)
            except Exception as exc:
                session.add(
                    AuditEvent(
                        actor_type="system",
                        actor_id="pipeline",
                        entity_type="content",
                        entity_id=content_id,
                        action="cover_fallback_failed",
                        metadata_json={"error": str(exc)[:500]},
                    )
                )
                session.commit()
                cover_artifact = None
            content = _queue_post_package_tasks(
                session,
                flow,
                content,
                settings,
                script,
                research,
                cover_artifact_id=cover_artifact.id if cover_artifact else None,
            )
        session.refresh(content)
        session.expunge(content)
        return content


def rebuild_cover(
    engine: Engine,
    settings: Settings,
    content_id: str,
    *,
    actor_id: str,
    title: str | None = None,
    use_source_frame: bool = False,
) -> Artifact:
    """Rebuild a packaged cover immediately; this path never creates an Agent task."""
    with Session(engine) as session:
        settings = effective_settings(session, settings)
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.PACKAGED, ContentStatus.PUBLISHED}:
            raise ValueError("只有待发布或已发布内容可以重建封面")
        if title is not None:
            normalized = "".join(title.split())
            if not 1 <= len(normalized) <= 12:
                raise ValueError("封面文案需为 1-12 个字")
            content.cover_title_override = normalized
            content.updated_at = utc_now()
            session.add(content)
            session.commit()
        base_image: Path | None = None
        if not use_source_frame:
            completed = session.exec(
                select(AgentTask)
                .where(
                    AgentTask.content_id == content_id,
                    AgentTask.task_type == TaskType.ASSET_GENERATE_IMAGE,
                    AgentTask.status == TaskStatus.COMPLETED,
                )
                .order_by(AgentTask.completed_at.desc())
            ).all()
            for task in completed:
                if task.input_data.get("asset_purpose") != "cover":
                    continue
                images = (task.result_data or {}).get("images", [])
                artifact_id = str((images[0].get("artifact_id") if images else "") or "")
                artifact = session.get(Artifact, artifact_id) if artifact_id else None
                candidate = (
                    Path(content.content_dir).resolve() / artifact.relative_path
                    if artifact
                    else None
                )
                if candidate and candidate.is_file():
                    base_image = candidate
                    break
        if _cover_ai_required(settings) and base_image is None:
            raise ValueError("AI 封面尚未生成完成，不能使用实拍帧或渐变封面")
        artifact = _build_and_register_cover(
            session,
            settings,
            content,
            base_image=base_image,
            actor_id=actor_id,
            action="cover_source_frame_selected" if use_source_frame else "cover_title_rebuilt",
        )
        if settings.archive_enabled:
            with suppress(Exception):
                ArchiveService(session, settings).snapshot(content_id)
        return artifact


def regenerate_cover(
    engine: Engine,
    settings: Settings,
    content_id: str,
    *,
    actor_id: str,
) -> AgentTask:
    """Request one asynchronous cover base regeneration."""
    with Session(engine, expire_on_commit=False) as session:
        settings = effective_settings(session, settings)
        content = session.get(Content, content_id)
        if content is None:
            raise KeyError(content_id)
        if content.business_status not in {ContentStatus.PACKAGED, ContentStatus.PUBLISHED}:
            raise ValueError("只有待发布或已发布内容可以重新生成封面")
        if not settings.cover_ai_enabled or not settings.asset_image_enabled:
            raise ValueError("封面 AI 或生图能力开关未启用")
        flow = workflow(session, settings)
        script = flow.result_for(content_id, TaskType.SHORT_SCRIPT) or {}
        task = _create_cover_generation_task(flow, content, settings, script)
        session.add(
            AuditEvent(
                actor_type="human",
                actor_id=actor_id,
                entity_type="content",
                entity_id=content_id,
                action="cover_ai_regenerated",
                metadata_json={"task_id": task.id},
            )
        )
        session.commit()
        return task
