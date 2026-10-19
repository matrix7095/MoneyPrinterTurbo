"""Replacement renders retain frozen media and review evidence from failed QC."""

import json
import shutil
from pathlib import Path

import pytest
from glance_media.executor import NonRetryableMediaError, probe, run_command, sha256_file
from glance_media.executor import _aligned_subtitle_words, _subtitle_alignment_hash
from PIL import Image

from app.business import media_executor
from app.business.media_executor import MptMediaExecutor


@pytest.fixture
def render_fixture(tmp_path, monkeypatch):
    executor = MptMediaExecutor(tmp_path / "content")
    voice = executor.root / "voice.wav"
    voice.write_bytes(b"frozen voice")
    image = executor.root / "image.png"
    Image.new("RGB", (20, 20), "red").save(image)
    arguments = dict(
        video_path=executor.root / "unused.mp4", voice_path=voice, narration="test",
        scenes=[], assets={"image": image}, plan_data={"segments": [{
            "asset_id": "image", "narration_start_sec": 0, "narration_end_sec": 0.4,
            "layout": "image_card", "purpose": "test", "motion": "hold",
        }]},
    )
    monkeypatch.setattr(media_executor, "probe", lambda _: {"format": {"duration": 0.4}})
    monkeypatch.setattr(media_executor, "ensure_ass_filter", lambda: None)
    monkeypatch.setattr(executor, "_build_subtitles", lambda *a, **kw: executor.root / "unused.ass")

    def fake_command(command, **kwargs):
        target = Path(command[-1])
        source = Path(command[command.index("-i") + 1])
        voice = Path(command[command.index("-i", command.index("-i") + 1) + 1]).read_bytes() if target.name == "final.mp4" else b""
        target.write_bytes(source.read_bytes() + voice + target.suffix.encode())

    monkeypatch.setattr(media_executor, "run_command", fake_command)
    projects = {}

    class Project:
        def __init__(self, directory):
            self.directory = directory
            self.directory.mkdir(parents=True, exist_ok=True)
            projects.setdefault(directory, {})
            self.state = projects[directory]

        def metadata(self):
            return {"current_revision": self.state["revision"]["revision_id"]}

        def revision(self):
            return self.state["revision"]

        def create_revision(self, request):
            revision_id = f"revision-{self.state.get('count', 0) + 1}"
            self.state["count"] = self.state.get("count", 0) + 1
            self.state["revision"] = {**request, "revision_id": revision_id}
            (self.directory / "project.json").write_text("{}", encoding="utf-8")
            self.export = self.directory / "export.mp4"
            self.export.write_bytes(Path(request["scenes"][0]["footage"]).read_bytes())
            return self.state["revision"]

        def _asset_path(self, path):
            return path

        def render(self, revision, **kwargs):
            return {"export": {"path": str(self.directory / "export.mp4")}}

    monkeypatch.setattr(media_executor, "VideoProject", Project)

    return executor, arguments


def test_revision_render_keeps_legacy_and_previous_outputs(render_fixture):
    executor, arguments = render_fixture
    legacy = executor.root / "render"
    legacy.mkdir()
    for name in ("final.mp4", "preview.mp4"):
        (legacy / name).write_bytes(b"original failed-QC media")
    first = executor.render(**arguments)
    first_bytes = [path.read_bytes() for path in first]
    arguments["voice_path"].write_bytes(b"new approved voice")
    second = executor.render(**arguments)
    assert first[0].parent != second[0].parent
    assert all(path.is_relative_to(legacy / "revisions") for path in (*first, *second))
    assert [path.read_bytes() for path in first] == first_bytes
    assert all((legacy / name).read_bytes() == b"original failed-QC media"
               for name in ("final.mp4", "preview.mp4"))
    assert executor.render(**arguments) == second


def test_committed_output_tampering_fails_before_regeneration(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    final, preview = executor.render(**arguments)
    receipt = final.with_name("outputs.json").read_bytes()
    final.write_bytes(b"tampered")
    actual_command = media_executor.run_command
    def no_final_mux(command, **kwargs):
        if Path(command[-1]).name == "final.mp4":
            pytest.fail("must not regenerate")
        return actual_command(command, **kwargs)
    monkeypatch.setattr(media_executor, "run_command", no_final_mux)
    with pytest.raises(NonRetryableMediaError, match="integrity"):
        executor.render(**arguments)
    assert final.read_bytes() == b"tampered"
    assert preview.is_file() and final.with_name("outputs.json").read_bytes() == receipt


def test_interrupted_render_same_bytes_resume_and_changed_bytes_fail(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    actual_command = media_executor.run_command

    def interrupt_preview(command, **kwargs):
        if Path(command[-1]).name == "preview.mp4":
            raise RuntimeError("interrupted preview")
        actual_command(command, **kwargs)

    monkeypatch.setattr(media_executor, "run_command", interrupt_preview)
    with pytest.raises(RuntimeError, match="interrupted"):
        executor.render(**arguments)
    final = next((executor.root / "render" / "revisions").glob("*/final.mp4"))
    before = final.read_bytes()
    assert not final.with_name("outputs.json").exists()
    monkeypatch.setattr(media_executor, "run_command", actual_command)
    def changed_mux(command, **kwargs):
        if Path(command[-1]).name == "final.mp4":
            Path(command[-1]).write_bytes(b"different retry bytes")
        else:
            actual_command(command, **kwargs)
    monkeypatch.setattr(media_executor, "run_command", changed_mux)
    with pytest.raises(NonRetryableMediaError, match="immutable"):
        executor.render(**arguments)
    assert final.read_bytes() == before and not final.with_name("outputs.json").exists()
    monkeypatch.setattr(media_executor, "run_command", actual_command)
    resumed = executor.render(**arguments)
    assert resumed[0] == final and final.read_bytes() == before and resumed[1].is_file()
    assert json.loads(final.with_name("outputs.json").read_text()) == {
        path.name: sha256_file(path) for path in resumed
    }


def test_bgm_change_selects_a_new_delivery_revision(render_fixture):
    executor, arguments = render_fixture
    first = executor.render(**arguments)
    music = executor.root / "music.wav"
    music.write_bytes(b"approved music")
    second = executor.render(**arguments, bgm_path=music)
    assert first[0].parent != second[0].parent
    third = executor.render(**arguments, bgm_path=music, bgm_volume=0.1)
    assert second[0].parent != third[0].parent
    with pytest.raises(NonRetryableMediaError, match="BGM"):
        executor.render(**arguments, bgm_path=executor.root / "missing.wav")


def test_revision_receipt_write_interruption_never_reuses_another_request(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    original_voice = arguments["voice_path"].read_bytes()
    first = executor.render(**arguments)
    frozen = first[0].read_bytes()
    actual_write = media_executor._atomic_json

    def interrupt_receipt(path, value):
        if path.name == "mpt-render-request.json":
            raise OSError("receipt interrupted after new revision")
        return actual_write(path, value)

    arguments["voice_path"].write_bytes(b"different voice")
    monkeypatch.setattr(media_executor, "_atomic_json", interrupt_receipt)
    with pytest.raises(OSError, match="receipt interrupted"):
        executor.render(**arguments)
    arguments["voice_path"].write_bytes(original_voice)
    monkeypatch.setattr(media_executor, "_atomic_json", actual_write)
    recovered = executor.render(**arguments)
    assert recovered[0].read_bytes() == frozen
    assert recovered[0].parent != first[0].parent
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text())
    assert receipt["revision_id"] == recovered[0].parent.name
    assert first[0].read_bytes() == frozen


def test_atomic_rename_fallback_never_exposes_a_partial_output(tmp_path, monkeypatch):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "temporary.mp4"
    source.write_bytes(b"complete media")
    destination = executor.root / "final.mp4"
    monkeypatch.setattr(media_executor, "probe", lambda _: {"format": {"duration": 1}})
    monkeypatch.setattr(media_executor.os, "link", lambda *a: (_ for _ in ()).throw(OSError("no link")))
    rename = media_executor.os.rename
    monkeypatch.setattr(media_executor.os, "rename",
                        lambda *a: (_ for _ in ()).throw(OSError("rename interrupted")))
    monkeypatch.setattr(media_executor.shutil, "copyfileobj", lambda *a: pytest.fail("no partial copy"))
    with pytest.raises(OSError, match="rename interrupted"):
        executor._promote_immutable_output(source, destination)
    assert not destination.exists()
    assert source.read_bytes() == b"complete media"
    monkeypatch.setattr(media_executor.os, "rename", rename)
    executor._promote_immutable_output(source, destination)
    assert destination.read_bytes() == b"complete media" and not source.exists()
    source.write_bytes(b"different media")
    with pytest.raises(NonRetryableMediaError, match="immutable"):
        executor._promote_immutable_output(source, destination)
    assert destination.read_bytes() == b"complete media"


@pytest.mark.parametrize("missing", [True, False])
def test_explicit_package_path_never_uses_legacy_alias(tmp_path, missing):
    executor = MptMediaExecutor(tmp_path / "content")
    (executor.root / "render").mkdir()
    (executor.root / "render" / "final.mp4").write_bytes(b"old failed video")
    path = executor.root / "missing.mp4" if missing else tmp_path / "outside.mp4"
    if not missing:
        path.write_bytes(b"outside")
    with pytest.raises(NonRetryableMediaError, match="within"):
        executor.package({"title": "new"}, final_path=path)
    assert not (executor.root / "publish").exists()


def test_package_uses_current_output_and_preserves_metadata(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    (executor.root / "render").mkdir()
    legacy = executor.root / "render" / "final.mp4"
    legacy.write_bytes(b"old video")
    old_publish = executor.root / "publish"
    old_publish.mkdir()
    legacy_package = {
        old_publish / "final.mp4": b"historical package",
        old_publish / "publish.json": b"historical metadata",
        old_publish / "cover.jpg": b"historical cover",
    }
    for path, content in legacy_package.items():
        path.write_bytes(content)
    current = executor.root / "current.mp4"
    current.write_bytes(b"current accepted video")
    metadata = {"title": "标题", "sources": ["https://example.com/"], "narration": "test"}
    directory = executor.package(metadata, final_path=current)
    assert (directory / "final.mp4").read_bytes() == current.read_bytes()
    assert json.loads((directory / "publish.json").read_text(encoding="utf-8")) == metadata
    assert legacy.read_bytes() == b"old video"
    default_package = executor.package(metadata)
    assert default_package != directory
    assert (default_package / "final.mp4").read_bytes() == legacy.read_bytes()
    assert {path: path.read_bytes() for path in legacy_package} == legacy_package


def test_package_revisions_preserve_previous_video_and_metadata_bytes(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    old_render = executor.root / "render" / "revisions" / "old" / "final.mp4"
    new_render = executor.root / "render" / "revisions" / "new" / "final.mp4"
    old_render.parent.mkdir(parents=True)
    new_render.parent.mkdir(parents=True)
    old_render.write_bytes(b"old rendered video")
    new_render.write_bytes(b"new rendered video")
    old_metadata = {"title": "旧版", "narration": "旧配音"}
    new_metadata = {"title": "新版", "narration": "新配音"}

    old_package = executor.package(old_metadata, final_path=old_render)
    frozen_old_video = (old_package / "final.mp4").read_bytes()
    frozen_old_metadata = (old_package / "publish.json").read_bytes()
    new_package = executor.package(new_metadata, final_path=new_render)

    assert old_package != new_package
    assert (new_package / "final.mp4").read_bytes() == b"new rendered video"
    assert json.loads((new_package / "publish.json").read_text(encoding="utf-8")) == new_metadata
    assert (old_package / "final.mp4").read_bytes() == frozen_old_video
    assert (old_package / "publish.json").read_bytes() == frozen_old_metadata
    assert old_render.read_bytes() == b"old rendered video"
    assert new_render.read_bytes() == b"new rendered video"


def test_package_metadata_change_creates_a_new_immutable_revision(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"same rendered video")
    first = executor.package({"title": "标题一"}, final_path=source)
    first_metadata = (first / "publish.json").read_bytes()

    second = executor.package({"title": "标题二"}, final_path=source)

    assert first != second
    assert (first / "final.mp4").read_bytes() == b"same rendered video"
    assert (second / "final.mp4").read_bytes() == b"same rendered video"
    assert (first / "publish.json").read_bytes() == first_metadata
    assert json.loads((second / "publish.json").read_text(encoding="utf-8")) == {"title": "标题二"}


def test_package_reuses_identical_revision_and_rejects_tampering(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"same rendered video")
    metadata = {"title": "稳定"}
    first = executor.package(metadata, final_path=source)
    assert executor.package(metadata, final_path=source) == first

    packaged_video = first / "final.mp4"
    packaged_video.write_bytes(b"tampered package")
    with pytest.raises(NonRetryableMediaError, match="integrity"):
        executor.package(metadata, final_path=source)
    assert packaged_video.read_bytes() == b"tampered package"
    assert source.read_bytes() == b"same rendered video"


def test_package_copy_succeeds_without_hard_link_support(tmp_path, monkeypatch):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"complete video")
    monkeypatch.setattr(media_executor.os, "link", lambda *a: pytest.fail("package should use an independent copy"))

    package = executor.package({"title": "copy fallback"}, final_path=source)

    assert (package / "final.mp4").read_bytes() == b"complete video"
    assert source.read_bytes() == b"complete video"


def test_package_partial_staging_failure_never_publishes_a_revision(tmp_path, monkeypatch):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"complete video")
    legacy = executor.root / "publish" / "final.mp4"
    legacy.parent.mkdir()
    legacy.write_bytes(b"legacy video")
    def partial_copy(_, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("copy interrupted")

    monkeypatch.setattr(media_executor.shutil, "copy2", partial_copy)
    with pytest.raises(OSError, match="copy interrupted"):
        executor.package({"title": "interrupted"}, final_path=source)

    revisions = executor.root / "publish" / "package-revisions"
    assert list(revisions.iterdir()) == []
    assert legacy.read_bytes() == b"legacy video"
    assert source.read_bytes() == b"complete video"


def test_package_rejects_external_symlink_source(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"external video")
    alias = executor.root / "outside.mp4"
    try:
        alias.symlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(NonRetryableMediaError, match="within"):
        executor.package({"title": "unsafe"}, final_path=alias)
    assert not (executor.root / "publish").exists()


def test_package_rejects_internal_reparse_source(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"local video")
    alias = executor.root / "alias.mp4"
    try:
        alias.symlink_to(source)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(NonRetryableMediaError, match="reparse"):
        executor.package({"title": "unsafe"}, final_path=alias)
    assert not (executor.root / "publish").exists()


def test_package_rejects_reparse_revision_directory(tmp_path):
    executor = MptMediaExecutor(tmp_path / "content")
    source = executor.root / "render" / "final.mp4"
    source.parent.mkdir()
    source.write_bytes(b"local video")
    outside = tmp_path / "outside-revisions"
    outside.mkdir()
    revisions = executor.root / "publish" / "package-revisions"
    revisions.parent.mkdir()
    try:
        revisions.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(NonRetryableMediaError, match="reparse"):
        executor.package({"title": "unsafe"}, final_path=source)
    assert list(outside.iterdir()) == []


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg/ffprobe required")
def test_two_real_renders_and_review_frames_remain_immutable(tmp_path):
    executor = MptMediaExecutor(tmp_path / "real-content")
    legacy = executor.root / "render"
    legacy.mkdir()
    for name in ("final.mp4", "preview.mp4"):
        (legacy / name).write_bytes(b"frozen historical bytes")
    old_frame = executor.root / "review-frames" / "frame-01.jpg"
    old_frame.parent.mkdir()
    old_frame.write_bytes(b"frozen historical frame")
    voice = executor.root / "voice.wav"
    run_command(["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4",
                 "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(voice)], cwd=executor.root)
    image = executor.root / "image.png"
    arguments = dict(video_path=executor.root / "unused.mp4", voice_path=voice, narration="测试。",
                     scenes=[], assets={"image": image}, generated_image_ids={"image"},
                     plan_data={"segments": [{"asset_id": "image", "narration_start_sec": 0,
                                               "narration_end_sec": 0.4, "layout": "image_card",
                                               "purpose": "concept image", "motion": "hold"}]})
    Image.new("RGB", (800, 800), "red").save(image)
    first = executor.render(**arguments)
    first_hashes = [sha256_file(path) for path in first]
    frames = executor.extract_review_frames(first[0], count=1)
    frame_hashes = {path: sha256_file(executor.root / path) for path in frames}
    Image.new("RGB", (800, 800), "blue").save(image)
    second = executor.render(**arguments)
    new_frames = executor.extract_review_frames(second[0], count=1)
    assert set(frames).isdisjoint(new_frames)
    assert first[0].parent != second[0].parent
    assert [sha256_file(path) for path in first] == first_hashes
    assert {path: sha256_file(executor.root / path) for path in frames} == frame_hashes
    assert old_frame.read_bytes() == b"frozen historical frame"
    assert all((legacy / name).read_bytes() == b"frozen historical bytes"
               for name in ("final.mp4", "preview.mp4"))
    for output in (*first, *second):
        assert probe(output)["format"]["duration"]
        run_command(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], cwd=executor.root)
    assert (executor.package({"title": "new"}, final_path=second[0]) / "final.mp4").read_bytes() == second[0].read_bytes()


def test_renderer_upgrade_creates_new_revision_without_replacing_old_outputs(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", "local-ffmpeg-v1")
    old = executor.render(**arguments)
    frozen = {path: path.read_bytes() for path in old}
    monkeypatch.setattr(media_executor, "RENDERER_VERSION", "local-ffmpeg-v2-cfr-pcm")
    new = executor.render(**arguments)
    assert old[0].parent != new[0].parent
    assert {path: path.read_bytes() for path in old} == frozen
    receipt = json.loads((executor.root / "render/mpt-render-request.json").read_text())
    assert receipt["renderer_version"] == "local-ffmpeg-v2-cfr-pcm"
    assert executor.render(**arguments) == new


def _subtitle_alignment(arguments, end=.3):
    narration = arguments["narration"]
    segments = [{"text": narration, "start_sec": 0, "end_sec": end,
                 "words": [{"text": narration, "start_sec": 0, "end_sec": end}]}]
    words = _aligned_subtitle_words(narration, segments, .4)
    import hashlib
    return {"version": 1, "method": "strict_voice_asr_words", "duration_sec": .4,
            "voice_sha256": sha256_file(arguments["voice_path"]),
            "narration_sha256": hashlib.sha256(narration.encode()).hexdigest(),
            "asr_model": "fixture", "asr_device": "cpu", "asr_compute_type": "int8",
            "segments": segments, "segments_sha256": _subtitle_alignment_hash(segments),
            "words": words, "mapping_sha256": _subtitle_alignment_hash(words)}


def test_each_alignment_revision_keeps_its_own_immutable_receipt(render_fixture):
    executor, arguments = render_fixture
    first_alignment = _subtitle_alignment(arguments, .25)
    first = executor.render(**arguments, subtitle_alignment=first_alignment)
    receipt = first[0].parent / "subtitle-alignment.json"
    frozen = receipt.read_bytes()
    second_alignment = _subtitle_alignment(arguments, .35)
    second = executor.render(**arguments, subtitle_alignment=second_alignment)
    assert first[0].parent != second[0].parent
    assert receipt.read_bytes() == frozen
    assert json.loads(receipt.read_text()) == first_alignment
    assert json.loads((second[0].parent / "subtitle-alignment.json").read_text()) == second_alignment
    assert executor.render(**arguments, subtitle_alignment=second_alignment) == second


def test_tampered_alignment_receipt_is_rejected(render_fixture):
    executor, arguments = render_fixture
    alignment = _subtitle_alignment(arguments)
    final, _ = executor.render(**arguments, subtitle_alignment=alignment)
    receipt = final.parent / "subtitle-alignment.json"
    receipt.write_text(json.dumps({**alignment, "asr_model": "tampered"}))
    with pytest.raises(NonRetryableMediaError, match="receipt changed"):
        executor.render(**arguments, subtitle_alignment=alignment)


def test_alignment_is_not_backfilled_into_frozen_legacy_output(render_fixture):
    executor, arguments = render_fixture
    final, _ = executor.render(**arguments)
    alignment = _subtitle_alignment(arguments)
    request = executor.root / "render/mpt-render-request.json"
    request.write_text(json.dumps({**json.loads(request.read_text()), "subtitle_alignment": alignment}))
    with pytest.raises(NonRetryableMediaError, match="frozen render"):
        executor.render(**arguments, subtitle_alignment=alignment)
    assert not (final.parent / "subtitle-alignment.json").exists()


def test_interrupted_alignment_receipt_write_recovers_without_partial_json(render_fixture, monkeypatch):
    executor, arguments = render_fixture
    alignment = _subtitle_alignment(arguments)
    original = media_executor.os.replace

    def interrupt(source, destination):
        if Path(destination).name == "subtitle-alignment.json":
            raise OSError("interrupted before atomic rename")
        return original(source, destination)

    monkeypatch.setattr(media_executor.os, "replace", interrupt)
    with pytest.raises(OSError, match="atomic rename"):
        executor.render(**arguments, subtitle_alignment=alignment)
    assert list((executor.root / "render/revisions").glob("*/subtitle-alignment.json")) == []
    monkeypatch.setattr(media_executor.os, "replace", original)
    final, _ = executor.render(**arguments, subtitle_alignment=alignment)
    assert json.loads((final.parent / "subtitle-alignment.json").read_text()) == alignment
