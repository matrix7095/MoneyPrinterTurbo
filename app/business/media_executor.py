"""Glance acquisition/QC helpers with MPT's real deterministic render backend."""

import hashlib
import json
import math
import os
import shutil
import stat
import tempfile
from pathlib import Path
from uuid import uuid4

from glance_media.executor import (
    FONT_ASSET_DIR,
    MediaExecutor,
    NonRetryableMediaError,
    _calibrate_render_plan,
    _clamp_to_duration,
    _encoder_args,
    _finite_seconds,
    _image_dimensions,
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

from app.services.video_project import RENDERER_VERSION, VideoProject, _atomic_json, _lock


class MptMediaExecutor(MediaExecutor):
    OPENING_IDENTITY_OVERLAY = True

    def __init__(self, content_dir: Path, *, revision_id: str | None = None) -> None:
        super().__init__(content_dir)
        # Keep root as the public path/asset boundary. Only mutable stage outputs
        # move; registered relative paths continue to resolve from Content.content_dir.
        self.work_root = self.root / "revisions" / revision_id if revision_id else self.root
        self.work_root.mkdir(parents=True, exist_ok=True)

    def _stage_executor(self):
        return MediaExecutor(self.work_root)

    def acquire(self, *args, **kwargs):
        return self._stage_executor().acquire(*args, **kwargs)

    def acquire_images(self, images, *, acquisition_id: str | None = None):
        directory = self.work_root / "research" / acquisition_id if acquisition_id and self.work_root != self.root else self.work_root
        return MediaExecutor(directory).acquire_images(images)

    def ensure_transcript(self, **kwargs):
        return self._stage_executor().ensure_transcript(**kwargs)

    def analyze_scenes(self, video_path):
        path, scenes = self._stage_executor().analyze_scenes(video_path)
        if self.work_root != self.root:
            for scene in scenes:
                scene["thumbnail"] = (self.work_root / scene["thumbnail"]).relative_to(self.root).as_posix()
            path.write_text(json.dumps({"scenes": scenes}, indent=2), encoding="utf-8")
        return path, scenes

    def _build_subtitles(self, *args, **kwargs):
        return self._stage_executor()._build_subtitles(*args, **kwargs)

    def _write_bgm_receipt(self, *args, **kwargs):
        return self._stage_executor()._write_bgm_receipt(*args, **kwargs)

    def qc(self, *args, **kwargs):
        return self._stage_executor().qc(*args, **kwargs)

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
        opening_identity=None,
        plan_data,
        scenes,
        assets=None,
        generated_image_ids=None,
        visual_timing_evidence=None,
        subtitle_alignment=None,
        encoder="libx264",
        bgm_path=None,
        bgm_volume=0.15,
        bgm_meta=None,
    ):
        from app.models.schema import VideoParams

        voice_path = self._local_asset(Path(voice_path))
        voice_duration = _finite_seconds(probe(voice_path)["format"]["duration"], label="Voice duration")
        if voice_duration <= 0:
            raise NonRetryableMediaError("Voice duration must be positive")
        if subtitle_alignment is not None:
            self._checked_subtitle_alignment(subtitle_alignment, narration, voice_duration, voice_path=voice_path)
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

        image_only = all(segment.layout == "image_card" and segment.scene_id is None for segment in plan.segments)
        fps = 60 if image_only else 30
        if image_only:
            # Both encoding stages must use the CPU profile validated for subpixel image motion.
            encoder = "libx264"
        ensure_ass_filter()
        subtitle_path = self._build_subtitles(
            narration,
            voice_duration,
            opening_identity=opening_identity,
            boundaries=boundaries,
            subtitle_alignment=subtitle_alignment,
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
        prepared = self.work_root / "render" / "prepared"
        prepared.mkdir(parents=True, exist_ok=True)
        scene_map = {scene["scene_id"]: scene for scene in scenes}
        source_metadata = probe(self._local_asset(Path(video_path))) if any(
            segment.scene_id is not None for segment in plan.segments
        ) else None
        project_scenes = []
        previous_frame = 0
        for index, segment in enumerate(plan.segments):
            start, end = segment.narration_start_sec, segment.narration_end_sec
            duration = end - start
            # Quantize cumulative edges once, rather than rounding every WAV duration again.
            end_frame = math.ceil(end * fps - 1e-9)
            frame_count = end_frame - previous_frame
            if frame_count <= 0:
                raise NonRetryableMediaError("Render segment is shorter than one video frame")
            start_frame = previous_frame
            previous_frame = end_frame
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
                segment.layout, motion=segment.motion, focus_x=segment.focus_x, focus_y=segment.focus_y,
                fps=fps, spatial_sampling=2 if image_only else 1,
            )
            filters = _with_transition(filters, segment.transition)
            filters += f",settb=1/{fps},setpts=PTS+{start_frame},{subtitle_filter},setpts=PTS-STARTPTS"
            footage = prepared / f"scene-{index:03d}.mp4"
            audio = prepared / f"scene-{index:03d}.wav"
            run_command(
                ["ffmpeg", "-y", "-threads", "1", "-filter_threads", "1", *source_arguments,
                 "-frames:v", str(frame_count), "-an", "-vf", filters, *_encoder_args(encoder),
                 # Temporal bit allocation can erase the last subpixel image-motion step.
                 *(["-crf", "10" if image_only else "18", "-x264-params", "mbtree=0"]
                   if segment.layout == "image_card" and encoder == "libx264" else []),
                 "-pix_fmt", "yuv420p", str(footage)],
                cwd=self.work_root,
            )
            run_command(
                ["ffmpeg", "-y", "-i", str(voice_path), "-af",
                 f"atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(audio)],
                cwd=self.work_root,
            )
            project_scenes.append({"id": f"scene-{index:03d}", "footage": str(footage),
                                   "audio": str(audio), "frame_count": frame_count})

        project = VideoProject(self.work_root / "render" / "mpt-project")
        request = {"project_id": self.work_root.name, "scenes": project_scenes, "settings": {"fps": fps}}
        if all(segment.layout == "image_card" for segment in plan.segments):
            request["settings"]["video_crf"] = 10 if image_only else 18
        if image_only:
            request["settings"].update(video_maxrate_kbps=20000, video_bufsize_kbits=40000)
        if bgm_path is not None and not Path(bgm_path).is_file():
            raise NonRetryableMediaError("Requested BGM file is missing")
        request_receipt = {
            "renderer_version": RENDERER_VERSION,
            "voice_sha256": sha256_file(voice_path),
            "bgm_sha256": sha256_file(Path(bgm_path)) if bgm_path else None,
            "bgm_volume": bgm_volume,
            "request": request,
            "prepared_sha256": [
                [sha256_file(Path(scene["footage"])), sha256_file(Path(scene["audio"]))]
                for scene in project_scenes
            ],
        }
        if subtitle_alignment is not None:
            request_receipt["subtitle_alignment"] = subtitle_alignment
        metadata = project.metadata() if (project.directory / "project.json").is_file() else {}
        revision = project.revision() if metadata.get("current_revision") else None
        # Match the frozen request before reusing an interrupted revision; never reuse replacement assets.
        receipt = self.work_root / "render" / "mpt-render-request.json"
        if revision is None or not receipt.is_file() or json.loads(receipt.read_text(encoding="utf-8")) != {
            **request_receipt, "revision_id": revision["revision_id"],
        }:
            revision = project.create_revision(request)
            _atomic_json(receipt, {**request_receipt, "revision_id": revision["revision_id"]})
        else:
            for scene in revision["scenes"]:
                for key in ("footage", "audio"):
                    project._asset_path(scene[key])
        status = project.render(revision["revision_id"], retry=True)
        output_dir = self.work_root / "render" / "revisions" / revision["revision_id"]
        output_dir.mkdir(parents=True, exist_ok=True)
        final, preview = output_dir / "final.mp4", output_dir / "preview.mp4"
        output_receipt = output_dir / "outputs.json"
        parameters = VideoParams(
            video_subject=narration,
            video_script=narration,
            subtitle_enabled=False,
            bgm_type="custom" if bgm_path else "none",
            bgm_file=str(bgm_path or ""),
            bgm_volume=bgm_volume,
        )
        # A failed QC still owns these bytes; a retry may verify them, never replace them.
        with _lock(output_dir / ".outputs.lock"):
            render_parameters = {"renderer_version": RENDERER_VERSION, "revision_id": revision["revision_id"], "fps": fps,
                                 "spatial_sampling": 2 if image_only else 1,
                                 "video_crf": request["settings"].get("video_crf", 23),
                                 "video_maxrate_kbps": request["settings"].get("video_maxrate_kbps", 0),
                                 "video_bufsize_kbits": request["settings"].get("video_bufsize_kbits", 0)}
            parameter_receipt = output_dir / "render-parameters.json"
            if parameter_receipt.exists():
                if json.loads(parameter_receipt.read_text(encoding="utf-8")) != render_parameters:
                    raise NonRetryableMediaError("Immutable render parameters changed")
            else:
                if output_receipt.exists() or final.exists() or preview.exists():
                    raise NonRetryableMediaError("Cannot add render parameters to a frozen render output")
                _atomic_json(parameter_receipt, render_parameters)
            if subtitle_alignment is not None:
                alignment_receipt = output_dir / "subtitle-alignment.json"
                if alignment_receipt.exists():
                    if json.loads(alignment_receipt.read_text(encoding="utf-8")) != subtitle_alignment:
                        raise NonRetryableMediaError("Immutable subtitle alignment receipt changed")
                else:
                    if output_receipt.exists() or final.exists() or preview.exists():
                        raise NonRetryableMediaError("Cannot add subtitle alignment to a frozen render output")
                    _atomic_json(alignment_receipt, subtitle_alignment)
            if output_receipt.is_file():
                expected = json.loads(output_receipt.read_text(encoding="utf-8"))
                if expected != {
                    path.name: sha256_file(self._local_asset(path)) for path in (final, preview, parameter_receipt)
                }:
                    raise NonRetryableMediaError("Committed render output failed integrity validation")
                return final, preview
            with tempfile.TemporaryDirectory(prefix=".render-", dir=output_dir) as directory:
                temporary = Path(directory) / "final.mp4"
                # Keep the approved CFR frames; MoviePy's rounded container duration can add/drop one.
                command = ["ffmpeg", "-y", "-i", status["export"]["path"], "-i", str(voice_path)]
                if bgm_path and parameters.bgm_volume and parameters.bgm_volume > 0:
                    fade_duration = min(3.0, voice_duration)
                    command += ["-i", str(bgm_path), "-filter_complex",
                                f"[2:a]atrim=duration={voice_duration:.9f},"
                                f"volume={parameters.bgm_volume},"
                                f"afade=t=out:st={voice_duration - fade_duration:.9f}:"
                                f"d={fade_duration:.9f}[music];"
                                "[1:a][music]amix=inputs=2:duration=first:normalize=0[a]",
                                "-map", "0:v:0", "-map", "[a]"]
                else:
                    command += ["-map", "0:v:0", "-map", "1:a:0"]
                run_command(command + ["-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                                       "-ar", "48000", "-ac", "2", "-movflags", "+faststart",
                                       str(temporary)], cwd=self.work_root)
                self._promote_immutable_output(temporary, final)
                preview_temporary = Path(directory) / "preview.mp4"
                run_command(
                    ["ffmpeg", "-y", "-i", str(final), "-vf", "scale=720:1280", "-c:v", "libx264",
                     "-preset", "veryfast", "-crf", "28", "-c:a", "aac", str(preview_temporary)],
                    cwd=self.work_root,
                )
                self._promote_immutable_output(preview_temporary, preview)
            if bgm_path:
                self._write_bgm_receipt(Path(bgm_path), bgm_meta)
            _atomic_json(output_receipt, {
                path.name: sha256_file(path) for path in (final, preview, parameter_receipt)
            })
        return final, preview

    def _promote_immutable_output(self, temporary: Path, destination: Path) -> None:
        if not isinstance(probe(temporary).get("format"), dict):
            raise NonRetryableMediaError("Rendered output has no readable media metadata")
        if destination.exists():
            if sha256_file(self._local_asset(destination)) != sha256_file(temporary):
                raise NonRetryableMediaError("Retry would replace an immutable render output")
            return
        try:
            os.link(temporary, destination)
        except OSError:
            # Render temporaries share this filesystem; rename exposes only complete bytes.
            os.rename(temporary, destination)

    def extract_review_frames(self, video_path, count=5, *, plan_data=None):
        video_path = self._local_asset(Path(video_path))
        evidence = MediaExecutor(self.root / "review-frames" / uuid4().hex)
        paths = evidence.extract_review_frames(video_path, count, plan_data=plan_data)
        return [(evidence.root / path).relative_to(self.root).as_posix() for path in paths]

    def package(self, metadata, *, final_path=None):
        source = Path(final_path) if final_path is not None else self.root / "render" / "final.mp4"
        final = self._local_asset(source)
        root = self.root.resolve()

        def is_reparse_point(path: Path) -> bool:
            try:
                info = path.lstat()
            except FileNotFoundError:
                return False
            attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & attributes)

        lexical_source = Path(os.path.abspath(source))
        if not lexical_source.is_relative_to(root):
            raise NonRetryableMediaError("Render asset must be a file within the content directory")
        current = lexical_source
        while current != root:
            if is_reparse_point(current):
                raise NonRetryableMediaError("Package source must not use reparse points")
            current = current.parent

        publish_dir = self.root / "publish"
        revisions_dir = publish_dir / "package-revisions"
        for directory in (publish_dir, revisions_dir):
            if is_reparse_point(directory):
                raise NonRetryableMediaError("Package directory must not use reparse points")
            try:
                directory.mkdir(exist_ok=True)
            except FileExistsError as exc:
                raise NonRetryableMediaError("Package directory path is not a directory") from exc
            if is_reparse_point(directory) or not directory.resolve().is_relative_to(root):
                raise NonRetryableMediaError("Package directory must stay within the content directory")

        metadata_bytes = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        source_sha256 = sha256_file(final)
        revision_id = hashlib.sha256(source_sha256.encode("ascii") + b"\0" + metadata_bytes).hexdigest()
        destination = revisions_dir / revision_id

        def verify_existing() -> Path:
            if is_reparse_point(destination) or not destination.is_dir():
                raise NonRetryableMediaError("Immutable package revision path is unsafe")
            if not destination.resolve().is_relative_to(revisions_dir.resolve()):
                raise NonRetryableMediaError("Package revision must stay within its revision directory")
            video_path, metadata_path = destination / "final.mp4", destination / "publish.json"
            if is_reparse_point(video_path) or is_reparse_point(metadata_path):
                raise NonRetryableMediaError("Immutable package revision contains a reparse point")
            video_path, metadata_path = self._local_asset(video_path), self._local_asset(metadata_path)
            if sha256_file(video_path) != source_sha256 or metadata_path.read_bytes() != metadata_bytes:
                raise NonRetryableMediaError("Immutable package revision failed integrity validation")
            return destination

        if destination.exists() or is_reparse_point(destination):
            return verify_existing()

        with tempfile.TemporaryDirectory(prefix=".package-", dir=revisions_dir) as temporary_dir:
            staging = Path(temporary_dir)
            staged_video = staging / "final.mp4"
            shutil.copy2(final, staged_video)
            if sha256_file(staged_video) != source_sha256:
                raise NonRetryableMediaError("Package source changed while it was being copied")
            with (staging / "publish.json").open("xb") as target:
                target.write(metadata_bytes)
                target.flush()
                os.fsync(target.fileno())
            try:
                os.rename(staging, destination)
            except OSError:
                if destination.exists() or is_reparse_point(destination):
                    return verify_existing()
                raise
        return verify_existing()
