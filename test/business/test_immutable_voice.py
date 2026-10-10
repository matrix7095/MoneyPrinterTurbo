"""Voice regeneration never changes approved bytes or silently replaces their timing."""

import json
from pathlib import Path

import pytest
from glance_core.database import build_engine, create_db
from glance_core.models import AgentTask, Artifact, Candidate, Content, ProductionMode, TaskType
from glance_core.settings import Settings
from glance_media import executor as portable
from glance_media.executor import NonRetryableMediaError, sha256_file
from glance_media.tts import TtsResult
from sqlmodel import Session, select

from app.business import pipeline
from app.business.media_executor import MptMediaExecutor
from test.business.test_media_pipeline import media_contract as media_contract


@pytest.fixture
def voice_contract(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, data_dir=tmp_path / "data",
                        database_url=f"sqlite:///{(tmp_path / 'business.db').as_posix()}")
    engine = build_engine(settings)
    create_db(engine)
    with Session(engine) as session:
        candidate = Candidate(source_type="agent", source_id="voice-test",
                              source_url="https://example.com", original_title="Voice")
        session.add(candidate)
        session.commit()
        content = Content(candidate_id=candidate.id, title="Voice",
                          content_dir=str(tmp_path / "content"))
        session.add(content)
        session.commit()
        executor = MptMediaExecutor(Path(content.content_dir))

        class Provider:
            fail = False

            def synthesize(self, narration, output, **kwargs):
                output.write_bytes(narration.encode())
                if self.fail:
                    raise RuntimeError("TTS failed after writing partial audio")
                return TtsResult(output, 1.0, [{"text": narration, "start_sec": 0.0,
                                              "end_sec": 1.0}], "stub")

        provider = Provider()
        monkeypatch.setattr(portable, "get_tts_provider", lambda *args: provider)

        def create(narration):
            result = executor.synthesize_voice(narration)
            artifact = pipeline.register_artifact(session, content=content, executor=executor,
                                                 path=result.audio_path, artifact_type="voice", stage="voice")
            evidence = executor.visual_timing_evidence(result.audio_path, result.duration_sec)
            return result, artifact, evidence

        yield session, content, executor, provider, create
    engine.dispose()


def test_regeneration_preserves_registered_voice_and_sidecar(voice_contract):
    _, _, _, _, create = voice_contract
    first, first_artifact, evidence = create("first")
    sidecar = first.audio_path.with_name("word_boundaries.json")
    original_bounds = sidecar.read_bytes()
    second, second_artifact, _ = create("second")
    assert first.audio_path != second.audio_path
    assert first_artifact.id != second_artifact.id
    assert sha256_file(first.audio_path) == first_artifact.sha256 == evidence["voice_sha256"]
    assert sidecar.read_bytes() == original_bounds
    assert sha256_file(sidecar) == evidence["boundaries_sha256"]


@pytest.mark.parametrize("failure", ["provider", "boundaries", "promotion"])
def test_failed_generation_preserves_previous_pair_and_cleans_partial_output(voice_contract, monkeypatch, failure):
    _, _, executor, provider, create = voice_contract
    first, artifact, evidence = create("first")
    before = set((executor.root / "audio").iterdir())
    if failure == "provider":
        provider.fail = True
    elif failure == "boundaries":
        synthesize = provider.synthesize

        def invalid_boundaries(*args, **kwargs):
            result = synthesize(*args, **kwargs)
            return TtsResult(result.audio_path, result.duration_sec, [object()], result.provider)

        monkeypatch.setattr(provider, "synthesize", invalid_boundaries)
    else:
        def fail_promotion(*args):
            raise OSError("promotion failed")

        monkeypatch.setattr(Path, "rename", fail_promotion)
    with pytest.raises((RuntimeError, TypeError, OSError)):
        executor.synthesize_voice("failed")
    assert set((executor.root / "audio").iterdir()) == before
    assert sha256_file(first.audio_path) == artifact.sha256
    assert executor.visual_timing_evidence(first.audio_path, 1.0) == evidence


def test_frozen_plan_selects_previous_artifact_even_after_new_voice_and_alias(voice_contract):
    session, content, executor, _, create = voice_contract
    first, _, evidence = create("first")
    create("second")
    alias = executor.root / "audio" / "narration.mp3"
    alias.write_bytes(b"third")
    alias.with_name("word_boundaries.json").write_text("[]", encoding="utf-8")
    assert pipeline._render_voice_path(session, content, evidence) == first.audio_path.resolve()
    assert executor.visual_timing_evidence(first.audio_path, 1.0) == evidence


@pytest.mark.parametrize("tamper", ["audio", "boundaries", "size", "missing", "outside"])
def test_frozen_plan_rejects_unavailable_or_changed_pair(voice_contract, tmp_path, tamper):
    session, content, _, _, create = voice_contract
    result, artifact, evidence = create("first")
    create("second")
    if tamper == "audio":
        result.audio_path.write_bytes(b"wrong")
    elif tamper == "boundaries":
        result.audio_path.with_name("word_boundaries.json").write_text("[]", encoding="utf-8")
    elif tamper == "missing":
        result.audio_path.unlink()
    elif tamper == "size":
        artifact.size_bytes += 1
    else:
        outside = tmp_path / "outside.mp3"
        outside.write_bytes(result.audio_path.read_bytes())
        artifact.relative_path = str(outside)
    session.add(artifact)
    session.commit()
    with pytest.raises(NonRetryableMediaError, match="voice|Voice"):
        pipeline._render_voice_path(session, content, evidence)


def test_legacy_alias_requires_registered_bytes_and_exact_frozen_pair(voice_contract):
    session, content, executor, _, _ = voice_contract
    audio = executor.root / "audio"
    audio.mkdir()
    voice = audio / "narration.mp3"
    voice.write_bytes(b"legacy")
    (audio / "word_boundaries.json").write_text(json.dumps([
        {"text": "legacy", "start_sec": 0, "end_sec": 1}]), encoding="utf-8")
    artifact = pipeline.register_artifact(session, content=content, executor=executor, path=voice,
                                         artifact_type="voice", stage="voice")
    evidence = executor.visual_timing_evidence(voice, 1.0)
    assert pipeline._render_voice_path(session, content, evidence) == voice.resolve()
    voice.write_bytes(b"replacement")
    with pytest.raises(NonRetryableMediaError, match="voice|Voice"):
        pipeline._render_voice_path(session, content, evidence)
    assert artifact.sha256 == evidence["voice_sha256"]


def test_explicit_empty_boundaries_do_not_read_mutable_alias(voice_contract):
    _, _, executor, _, _ = voice_contract
    audio = executor.root / "audio"
    audio.mkdir()
    (audio / "word_boundaries.json").write_text(json.dumps([
        {"text": "WRONG ALIAS", "start_sec": 0, "end_sec": 1}]), encoding="utf-8")
    assert executor._subtitle_events("correct", 1.0, boundaries=[]) == [(0.0, 1.0, "correct")]


def test_renderer_rejects_new_voice_with_old_frozen_plan(voice_contract, monkeypatch):
    _, _, executor, _, create = voice_contract
    _, _, evidence = create("first")
    second, _, _ = create("second")
    monkeypatch.setattr("app.business.media_executor.probe", lambda _: {"format": {"duration": 1.0}})
    with pytest.raises(NonRetryableMediaError, match="stale or invalid"):
        executor.render(video_path=executor.root / "unused.mp4", voice_path=second.audio_path,
                        narration="first", plan_data={"segments": [
                            {"asset_id": "image", "narration_start_sec": 0, "narration_end_sec": 1,
                             "layout": "image_card", "purpose": "test"}]}, scenes=[],
                        visual_timing_evidence=evidence)


def test_empty_tts_boundaries_still_freeze_approved_voice(voice_contract, monkeypatch):
    session, content, executor, provider, create = voice_contract
    synthesize = provider.synthesize

    def empty_boundaries(*args, **kwargs):
        result = synthesize(*args, **kwargs)
        return TtsResult(result.audio_path, result.duration_sec, [], result.provider)

    monkeypatch.setattr(provider, "synthesize", empty_boundaries)
    first, artifact, evidence = create("approved")
    second, _, _ = create("replacement")
    assert evidence is not None
    assert evidence["boundaries"] == []
    assert evidence["voice_sha256"] == artifact.sha256
    assert evidence["boundaries_sha256"] == sha256_file(first.audio_path.with_name("word_boundaries.json"))
    assert pipeline._render_voice_path(session, content, evidence) == first.audio_path.resolve()

    monkeypatch.setattr("app.business.media_executor.probe", lambda _: {"format": {"duration": 1.0}})
    with pytest.raises(NonRetryableMediaError, match="stale or invalid"):
        executor.render(video_path=executor.root / "unused.mp4", voice_path=second.audio_path,
                        narration="approved", plan_data={"segments": [
                            {"asset_id": "image", "narration_start_sec": 0, "narration_end_sec": 1,
                             "layout": "image_card", "purpose": "test"}]}, scenes=[],
                        visual_timing_evidence=evidence)


def test_new_immutable_voice_without_binding_never_defaults_to_latest(voice_contract):
    session, content, _, _, create = voice_contract
    create("approved")
    create("replacement")
    with pytest.raises(NonRetryableMediaError, match="voice|Voice"):
        pipeline._render_voice_path(session, content, None)


def test_invalid_nonempty_timing_is_not_downgraded_to_unbound_voice(voice_contract):
    _, _, executor, _, create = voice_contract
    result, _, _ = create("approved")
    result.audio_path.with_name("word_boundaries.json").write_text(json.dumps([
        {"text": "bad timing", "start_sec": 2, "end_sec": 3}]), encoding="utf-8")
    with pytest.raises(NonRetryableMediaError, match="boundaries"):
        executor.visual_timing_evidence(result.audio_path, 1.0)


@pytest.mark.parametrize("field", ["id", "relative_path", "sha256", "size_bytes"])
def test_frozen_voice_artifact_metadata_cannot_be_substituted(voice_contract, field):
    session, content, _, _, create = voice_contract
    first, artifact, evidence = create("approved")
    binding = {key: getattr(artifact, key) for key in ("id", "relative_path", "sha256", "size_bytes")}
    assert pipeline._render_voice_path(session, content, evidence, binding) == first.audio_path.resolve()
    binding[field] = "wrong" if field != "size_bytes" else artifact.size_bytes + 1
    with pytest.raises(NonRetryableMediaError, match="voice|Voice"):
        pipeline._render_voice_path(session, content, evidence, binding)


@pytest.mark.parametrize("mode", [ProductionMode.IMAGE_ONLY, ProductionMode.FOOTAGE])
@pytest.mark.parametrize("native_boundaries", [True, False])
def test_new_approval_freezes_voice_identity_even_without_native_timing(media_contract, monkeypatch, mode,
                                                                     native_boundaries):
    engine, session, settings, content, _, _, _, _, _, _, completed = media_contract
    content.production_mode = mode
    session.add(content)
    session.commit()
    if mode == ProductionMode.FOOTAGE:
        completed(TaskType.SCENE_ANALYZE, {}, {"scenes": []})
        scenes = Path(content.content_dir) / "scenes" / "scenes.json"
        scenes.parent.mkdir()
        scenes.write_text('{"scenes": []}', encoding="utf-8")

    class Provider:
        def synthesize(self, narration, output, **kwargs):
            output.write_bytes(b"approved stub voice")
            return TtsResult(output, 30.0, [{"text": narration, "start_sec": 0, "end_sec": 30}]
                             if native_boundaries else [], "stub")

    monkeypatch.setattr(portable, "get_tts_provider", lambda *args: Provider())
    pipeline.approve_script(engine, settings, content.id)
    session.expire_all()
    task = session.exec(select(AgentTask).where(AgentTask.content_id == content.id,
                                              AgentTask.task_type == TaskType.VISUAL_PLAN,
                                              AgentTask.input_data.is_not(None))
                        .order_by(AgentTask.created_at.desc(), AgentTask.id.desc())).first()
    evidence, binding = task.input_data["visual_timing_evidence"], task.input_data["voice_artifact"]
    artifact = session.get(Artifact, binding["id"])
    assert artifact.content_id == content.id
    assert evidence["voice_sha256"] == binding["sha256"] == artifact.sha256
    assert binding["size_bytes"] == artifact.size_bytes
    assert binding["relative_path"] == artifact.relative_path
    assert bool(evidence["boundaries"]) is native_boundaries
    assert pipeline._render_voice_path(session, content, evidence, binding).is_file()
