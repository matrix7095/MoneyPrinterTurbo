"""Glance acquisition/QC helpers with MPT's real deterministic render backend."""

import json
from pathlib import Path

from glance_media.executor import (
    FONT_ASSET_DIR,
    MediaExecutor,
    NonRetryableMediaError,
    _calibrate_render_plan,
    _clamp_to_duration,
    _encoder_args,
    _finite_seconds,
    _image_dimensions,
    _promote_render_output,
    _select_timing_boundaries,
    _stream_dimensions,
    _with_transition,
    ensure_ass_filter,
    ffmpeg_filter_path,
    layout_filter,
    probe,
    run_command,
    sha256_file,
    source_redaction_filter,
)
from glance_media.render_plan import RenderPlan

from app.services.video_project import VideoProject, _atomic_json


class MptMediaExecutor(MediaExecutor):
    def _local_asset(self, path: Path) -> Path:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.root.resolve()) or not resolved.is_file():
            raise NonRetryableMediaError("Render asset must be a file within the content directory")
        return resolved

    def render(
        self,
        *,
        video_path,
        voice_path,
        narration,
        plan_data,
        scenes,
        assets=None,
        generated_image_ids=None,
        visual_timing_evidence=None,
        encoder="libx264",
        bgm_path=None,
        bgm_volume=0.15,
        bgm_meta=None,
    ):
        from app.models.schema import VideoParams
        from app.services.video import generate_video

        voice_path = self._local_asset(Path(voice_path))
        voice_duration = _finite_seconds(probe(voice_path)["format"]["duration"], label="Voice duration")
        if voice_duration <= 0:
            raise NonRetryableMediaError("Voice duration must be positive")
        plan = RenderPlan.model_validate(plan_data)
        raw_boundaries_path = voice_path.with_name("word_boundaries.json")
        raw_boundaries = self._read_boundaries(self._local_asset(raw_boundaries_path)) if (
            raw_boundaries_path.is_file()
        ) else []
        boundaries = _select_timing_boundaries(
            raw_boundaries, voice_duration, required_internal_edges=len(plan.segments) - 1
        )
        if visual_timing_evidence is not None:
            try:
                current = self.visual_timing_evidence(voice_path, visual_timing_evidence.get("duration_sec"))
            except (AttributeError, OSError, TypeError, ValueError) as exc:
                raise NonRetryableMediaError("Visual plan TTS timing evidence is invalid") from exc
            if (
                current != visual_timing_evidence
                or abs(voice_duration - visual_timing_evidence["duration_sec"]) > 0.001
            ):
                raise NonRetryableMediaError("Visual plan TTS timing evidence is stale or invalid")
            boundaries = current["boundaries"]
        elif raw_boundaries:
            if not boundaries:
                raise NonRetryableMediaError("TTS boundaries are unsafe for rendering")
            plan = RenderPlan.model_validate(_calibrate_render_plan(plan_data, boundaries, voice_duration))
        previous = 0.0
        for segment in plan.segments:
            if abs(segment.narration_start_sec - previous) > 0.001:
                raise NonRetryableMediaError("Render narration timeline contains a gap or overlap")
            previous = segment.narration_end_sec
        if abs(previous - voice_duration) > 0.05:
            raise NonRetryableMediaError("Render timeline does not cover the actual narration")

        ensure_ass_filter()
        subtitle_path = self._build_subtitles(
            narration,
            voice_duration,
            boundaries=boundaries,
            concept_intervals=[
                (segment.narration_start_sec, segment.narration_end_sec)
                for segment in plan.segments
                if segment.asset_id in (generated_image_ids or set())
            ],
        )
        # Burn the original ASS styles, including AI disclosure, before MPT snapshots each scene.
        subtitle_filter = (
            f"ass=filename='{ffmpeg_filter_path(subtitle_path)}':"
            f"fontsdir='{ffmpeg_filter_path(FONT_ASSET_DIR)}'"
        )
        prepared = self.root / "render" / "prepared"
        prepared.mkdir(parents=True, exist_ok=True)
        scene_map = {scene["scene_id"]: scene for scene in scenes}
        source_metadata = probe(self._local_asset(Path(video_path))) if any(
            segment.scene_id is not None for segment in plan.segments
        ) else None
        project_scenes = []
        for index, segment in enumerate(plan.segments):
            start, end = segment.narration_start_sec, segment.narration_end_sec
            duration = end - start
            if segment.scene_id is not None:
                scene = scene_map.get(segment.scene_id)
                if scene is None:
                    raise NonRetryableMediaError(f"Unknown scene ID: {segment.scene_id}")
                source_duration = _finite_seconds(source_metadata["format"]["duration"], label="Source duration")
                source_start = segment.source_start_sec
                source_end = _clamp_to_duration(segment.source_end_sec, source_duration, label="Source interval")
                if source_start < float(scene["start_sec"]) or source_end > float(scene["end_sec"]) + 0.001:
                    raise NonRetryableMediaError("Render source interval exceeds approved scene bounds")
                if source_end <= source_start:
                    raise NonRetryableMediaError("Render source interval is empty")
                source = self._local_asset(Path(video_path))
                source_arguments = ["-i", str(source)]
                filters = (
                    f"trim=start={source_start:.6f}:end={source_end:.6f},setpts=PTS-STARTPTS,"
                    f"tpad=stop_mode=clone:stop_duration={duration:.6f}"
                )
                width, height = _stream_dimensions(source_metadata)
            else:
                source = (assets or {}).get(segment.asset_id)
                if source is None or segment.layout != "image_card":
                    raise NonRetryableMediaError("Image-card segment requires a registered local asset")
                source = self._local_asset(Path(source))
                source_arguments = ["-loop", "1", "-i", str(source)]
                filters = "setpts=PTS-STARTPTS"
                width, height = _image_dimensions(source)
            if segment.source_redactions:
                filters += "," + source_redaction_filter(
                    segment.source_redactions, source_width=width, source_height=height
                )
            filters += "," + layout_filter(
                segment.layout, motion=segment.motion, focus_x=segment.focus_x, focus_y=segment.focus_y
            )
            filters = _with_transition(filters, segment.transition)
            filters += f",setpts=PTS+{start:.6f}/TB,{subtitle_filter},setpts=PTS-STARTPTS"
            footage = prepared / f"scene-{index:03d}.mp4"
            audio = prepared / f"scene-{index:03d}.wav"
            run_command(
                ["ffmpeg", "-y", "-threads", "1", "-filter_threads", "1", *source_arguments,
                 "-t", f"{duration:.6f}", "-an", "-vf", filters, *_encoder_args(encoder),
                 "-pix_fmt", "yuv420p", str(footage)],
                cwd=self.root,
            )
            run_command(
                ["ffmpeg", "-y", "-i", str(voice_path), "-af",
                 f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(audio)],
                cwd=self.root,
            )
            project_scenes.append({"id": f"scene-{index:03d}", "footage": str(footage), "audio": str(audio)})

        project = VideoProject(self.root / "render" / "mpt-project")
        request = {"project_id": self.root.name, "scenes": project_scenes}
        request_receipt = {
            "request": request,
            "prepared_sha256": [
                [sha256_file(Path(scene["footage"])), sha256_file(Path(scene["audio"]))]
                for scene in project_scenes
            ],
        }
        metadata = project.metadata() if (project.directory / "project.json").is_file() else {}
        revision = project.revision() if metadata.get("current_revision") else None
        # Match the frozen request before reusing an interrupted revision; never reuse replacement assets.
        receipt = self.root / "render" / "mpt-render-request.json"
        if revision is None or not receipt.is_file() or json.loads(receipt.read_text(encoding="utf-8")) != request_receipt:
            revision = project.create_revision(request)
            _atomic_json(receipt, request_receipt)
        else:
            for scene in revision["scenes"]:
                for key in ("footage", "audio"):
                    project._asset_path(scene[key])
        status = project.render(revision["revision_id"], retry=True)
        temporary = self.root / "render" / ".final.tmp.mp4"
        parameters = VideoParams(
            video_subject=narration,
            video_script=narration,
            subtitle_enabled=False,
            bgm_type="custom" if bgm_path else "none",
            bgm_file=str(bgm_path or ""),
            bgm_volume=bgm_volume,
        )
        if bgm_path is not None and not Path(bgm_path).is_file():
            raise NonRetryableMediaError("Requested BGM file is missing")
        bgm_succeeded = generate_video(
            video_path=status["export"]["path"], audio_path=str(voice_path), subtitle_path="",
            output_file=str(temporary), params=parameters,
            bgm_file_override=str(bgm_path) if bgm_path else None,
        )
        if bgm_path and not bgm_succeeded:
            raise NonRetryableMediaError("Requested BGM could not be mixed")
        if bgm_path:
            self._write_bgm_receipt(Path(bgm_path), bgm_meta)
        final = self.root / "render" / "final.mp4"
        _promote_render_output(temporary, final)
        preview = self.root / "render" / "preview.mp4"
        preview_temporary = self.root / "render" / ".preview.tmp.mp4"
        run_command(
            ["ffmpeg", "-y", "-i", str(final), "-vf", "scale=720:1280", "-c:v", "libx264",
             "-preset", "veryfast", "-crf", "28", "-c:a", "aac", str(preview_temporary)],
            cwd=self.root,
        )
        _promote_render_output(preview_temporary, preview)
        return final, preview
