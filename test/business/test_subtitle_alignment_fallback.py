"""Subtitle-only pronoun equivalence and one bounded, unprompted local fallback."""

from copy import deepcopy
from itertools import permutations
from types import SimpleNamespace
from array import array
import json
import sys

import pytest

from glance_core.settings import Settings
from glance_media import executor as media
from test.business.test_subtitle_word_alignment import segments
from test.business.test_media_background_launch import final_audio_cache as final_audio_cache


@pytest.mark.parametrize("observed,approved", list(permutations("他她它", 2)))
def test_v2_pronouns_map_positionally_and_keep_observed_word_times(observed, approved):
    raw = segments([(observed + "可以", .1, .5)])
    words = media._aligned_subtitle_words(approved + "可以。", raw, 1, version=2)
    assert words[0]["text"] == approved + "可以。"
    assert words[0]["pronoun_mappings"] == [{"observed": observed, "approved": approved,
        "observed_char_index": 0, "source_char_index": 0, "asr_word_index": 0, "start_sec": .1, "end_sec": .5}]
    assert raw[0]["words"][0]["text"] == observed + "可以"
    with pytest.raises(media.NonRetryableMediaError):
        media._aligned_subtitle_words(approved + "可以。", raw, 1)


@pytest.mark.parametrize("approved,observed", [("想", "像"), ("长", "涨"), ("2.5", "25"), ("它可以", "他"), ("它他", "他")])
def test_v2_rejects_other_homophones_numbers_and_missing_characters(approved, observed):
    with pytest.raises(media.NonRetryableMediaError):
        media._aligned_subtitle_words(approved, segments([(observed, .1, .5)]), 1, version=2)


def test_v2_never_applies_equivalence_between_raw_asr_words_and_segment_text():
    raw = segments([("他", .1, .5)])
    raw[0]["text"] = "它"
    with pytest.raises(media.NonRetryableMediaError):
        media._aligned_subtitle_words("它", raw, 1, version=2)


def shifted_segment_words(first="他可以", second="完成", first_word="他", second_word="可以完成"):
    raw = segments([(first_word, .1, .4)]) + segments([(second_word, .5, .9)])
    raw[0]["text"], raw[1]["text"] = first, second
    return raw


def test_v2_rejects_words_moved_across_segments_while_v1_keeps_historical_semantics():
    raw = shifted_segment_words()
    assert media._aligned_subtitle_words("他可以完成。", raw, 1)
    with pytest.raises(media.NonRetryableMediaError, match="segment text disagree"):
        media._aligned_subtitle_words("它可以完成。", raw, 1, version=2)


def test_v2_segment_check_keeps_existing_punctuation_and_whitespace_normalization():
    raw = segments([(" 他，可以 ", .1, .4)]) + segments([("完成", .5, .9)])
    raw[0]["text"] = "他可以！"
    assert media._aligned_subtitle_words("它可以完成。", raw, 1, version=2)


def test_subtitle_fallback_is_disabled_by_default():
    assert Settings(_env_file=None).subtitle_alignment_fallback_model_dir == ""


@pytest.fixture
def fallback_case(tmp_path, monkeypatch):
    voice = tmp_path / "voice.mp3"
    voice.write_bytes(b"unchanged voice")
    executor = media.MediaExecutor(tmp_path)
    primary = segments([("像它完成", .1, 2.1)])
    monkeypatch.setattr(executor, "_local_asr", lambda *a, **k: (deepcopy(primary), ""))
    monkeypatch.setattr(media, "probe", lambda _: {"format": {"duration": 2.4}})
    samples = array("h", [1000] * (24000 * 24 // 10))
    samples[26400:31200] = array("h", [0] * 4800)  # 1.10–1.30s silence.
    state = {"pcm": samples.tobytes(), "halves": [[("想他", .1, 1.)], [("完成", 1.4, 2.1)]]}
    monkeypatch.setattr(media.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=state["pcm"]))
    model_dir = tmp_path / "local-model"
    model_dir.mkdir()
    for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json"):
        (model_dir / name).write_bytes(b"{}")
    calls = []

    class Model:
        def __init__(self, directory, **kwargs):
            calls.append(("load", directory, kwargs))

        def transcribe(self, path, **kwargs):
            index = sum(row[0] == "transcribe" for row in calls)
            calls.append(("transcribe", path, kwargs))
            words = state["halves"][index]
            raw = SimpleNamespace(start=words[0][1], end=words[-1][2], text="".join(row[0] for row in words),
                words=[SimpleNamespace(word=text, start=start, end=end, probability=.9) for text, start, end in words])
            return iter([raw]), SimpleNamespace()

    monkeypatch.setitem(sys.modules, "faster_whisper", SimpleNamespace(WhisperModel=Model))

    def align(model=None):
        return executor.align_subtitles(voice, "想它完成。", asr_model="medium", asr_device="cpu", asr_compute_type="int8",
                                        fallback_model_dir=str(model_dir) if model is None else model)

    return executor, voice, model_dir, calls, state, primary, align


def test_fallback_uses_one_local_model_two_full_global_clips_and_preserves_primary(fallback_case):
    executor, voice, directory, calls, _, primary, align = fallback_case
    result = align()
    assert result["version"] == 2 and result["asr_model"] == "medium"
    assert result["pronoun_mappings"][0]["observed"] == "他"
    evidence = result["recognition_evidence"]
    assert evidence["primary"]["segments"] == primary
    assert evidence["fallback"]["pcm"]["halves"] == [[0., 1.2], [1.2, 2.4]]
    assert len(calls) == 3 and calls[0] == ("load", str(directory), {"device": "cpu", "compute_type": "int8", "local_files_only": True})
    for row, bounds in zip(calls[1:], [[0., 1.2], [1.2, 2.4]], strict=True):
        assert row == ("transcribe", str(voice), {"clip_timestamps": bounds, "vad_filter": False,
                                                "condition_on_previous_text": False, "word_timestamps": True})
    assert executor._checked_subtitle_alignment(result, "想它完成。", 2.4, voice_path=voice)
    records = list((executor.root / "render/subtitle-asr-evidence").glob("*.json"))
    assert len(records) == 1 and json.loads(records[0].read_text(encoding="utf-8"))["recognition_evidence"] == evidence


def test_exact_fallback_still_has_v2_provenance(fallback_case):
    _, _, _, _, state, _, align = fallback_case
    state["halves"][0] = [("想它", .1, 1.)]
    result = align()
    assert result["version"] == 2 and result["pronoun_mappings"] == []
    assert len(result["recognition_evidence"]["fallback"]["halves"]) == 2


@pytest.mark.parametrize("exception_type", [RuntimeError, OSError])
@pytest.mark.parametrize("phase", ["load", "transcribe"])
def test_fallback_failure_retains_original_diagnostics_and_cause(fallback_case, monkeypatch, exception_type, phase):
    executor, _, _, _, _, _, align = fallback_case
    original = exception_type("private backend diagnostic: model kernel failed")
    model = sys.modules["faster_whisper"].WhisperModel

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(model, "__init__" if phase == "load" else "transcribe", fail)
    with pytest.raises(media.NonRetryableMediaError, match="original evidence retained") as failure:
        align()
    assert failure.value.__cause__ is original
    assert str(original) not in str(failure.value)
    records = list((executor.root / "render/subtitle-asr-evidence").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["exception"] == {"type": exception_type.__name__, "message": str(original)}
    assert record["recognition_evidence"]["fallback"]["model"]


def test_cross_segment_primary_is_rejected_before_loading_fallback(fallback_case):
    _, _, _, calls, _, primary, align = fallback_case
    primary[:] = shifted_segment_words("像它", "完成", "像", "它完成")
    with pytest.raises(media.NonRetryableMediaError, match="segment text disagree"):
        align()
    assert calls == []


def test_v2_rejects_rehashed_cross_segment_primary_evidence(fallback_case):
    executor, voice, _, _, _, _, align = fallback_case
    result = align()
    result["recognition_evidence"]["primary"]["segments"] = shifted_segment_words("像它", "完成", "像", "它完成")
    result["recognition_evidence_sha256"] = media._subtitle_alignment_hash(result["recognition_evidence"])
    with pytest.raises(media.NonRetryableMediaError, match="segment text disagree"):
        executor._checked_subtitle_alignment(result, "想它完成。", 2.4, voice_path=voice)


def test_fallback_half_rejects_cross_segment_words():
    with pytest.raises(media.NonRetryableMediaError, match="segment text disagree"):
        media._validate_subtitle_half(shifted_segment_words(), 0, 1, 2)


@pytest.mark.parametrize("model", ["", "large-v3", "org/model", "https://example.org/model"])
def test_disabled_or_remote_models_never_start_fallback(fallback_case, model):
    _, _, _, calls, _, _, align = fallback_case
    with pytest.raises(media.NonRetryableMediaError):
        align(model)
    assert calls == []


@pytest.mark.parametrize("missing", ["model.bin", "config.json", "tokenizer.json", "vocabulary.json"])
def test_incomplete_local_model_never_loads_or_downloads(fallback_case, missing):
    _, _, directory, calls, _, _, align = fallback_case
    (directory / missing).unlink()
    with pytest.raises(media.NonRetryableMediaError):
        align()
    assert calls == []


def test_no_internal_silence_fails_without_arbitrary_cut(fallback_case):
    executor, _, _, calls, state, _, align = fallback_case
    state["pcm"] = array("h", [1000] * 57600).tobytes()
    with pytest.raises(media.NonRetryableMediaError, match="silence"):
        align()
    assert calls == []
    assert list((executor.root / "render/subtitle-asr-evidence").glob("*.json"))


@pytest.mark.parametrize("damage", ["mapping", "primary", "parameters", "coverage", "pcm_sha", "model_bytes", "version"])
def test_v2_mapping_and_evidence_tampering_is_rejected_even_with_rehashed_evidence(fallback_case, damage):
    executor, voice, directory, _, _, _, align = fallback_case
    result = align()
    fallback = result["recognition_evidence"]["fallback"]
    if damage == "mapping":
        result["pronoun_mappings"][0]["observed"] = "她"
    elif damage == "primary":
        result["recognition_evidence"]["primary"]["segments"][0]["text"] = "changed"
    elif damage == "parameters":
        fallback["parameters"]["hotwords"] = "想它完成"
    elif damage == "coverage":
        fallback["pcm"]["halves"][1][0] = 1.3
    elif damage == "pcm_sha":
        fallback["pcm"]["decoded_pcm_sha256"] = "0" * 64
    elif damage == "model_bytes":
        (directory / "model.bin").write_bytes(b"changed")
    else:
        result["version"] = 1
    result["recognition_evidence_sha256"] = media._subtitle_alignment_hash(result["recognition_evidence"])
    with pytest.raises(media.NonRetryableMediaError):
        executor._checked_subtitle_alignment(result, "想它完成。", 2.4, voice_path=voice)


def test_half_relative_timestamps_are_rejected_without_offset_guessing(fallback_case):
    _, _, _, _, state, _, align = fallback_case
    state["halves"][1] = [("完成", .1, .8)]
    with pytest.raises(media.NonRetryableMediaError, match="global"):
        align()


def test_default_and_strict_primary_success_do_not_touch_fallback(fallback_case, monkeypatch):
    executor, voice, _, calls, _, _, _ = fallback_case
    monkeypatch.setattr(executor, "_local_asr", lambda *a, **k: (segments([("产品", .1, 1.)]), ""))
    result = executor.align_subtitles(voice, "产品。", asr_model="medium", fallback_model_dir="not-even-a-directory")
    assert result["version"] == 1 and calls == [] and "recognition_evidence" not in result


def test_pronoun_only_primary_uses_v2_receipt_without_a_model_fallback(fallback_case, monkeypatch):
    executor, voice, _, calls, _, _, _ = fallback_case
    raw = segments([("他", .1, .5)])
    monkeypatch.setattr(executor, "_local_asr", lambda *a, **k: (deepcopy(raw), ""))
    result = executor.align_subtitles(voice, "它。", asr_model="medium")
    assert result["version"] == 2 and calls == []
    assert result["recognition_evidence"]["primary"]["segments"] == raw
    assert "fallback" not in result["recognition_evidence"]
    assert executor._checked_subtitle_alignment(result, "它。", 2.4, voice_path=voice)


def test_setting_persists_independently_of_main_and_final_qc_asr(tmp_path):
    from glance_core.database import build_engine, create_db
    from glance_core.runtime_config import effective_settings, save_overrides
    from sqlmodel import Session

    base = Settings(_env_file=None, data_dir=tmp_path, database_url=f"sqlite:///{tmp_path / 'settings.db'}",
                    asr_model="medium", asr_device="cpu", asr_compute_type="int8")
    engine = build_engine(base)
    create_db(engine)
    with Session(engine) as session:
        assert save_overrides(session, {"subtitle_alignment_fallback_model_dir": str(tmp_path / "model")}) == 1
        current = effective_settings(session, base)
        assert current.subtitle_alignment_fallback_model_dir == str(tmp_path / "model")
        assert (current.asr_model, current.asr_device, current.asr_compute_type) == ("medium", "cpu", "int8")
    engine.dispose()


def test_final_audio_qc_keeps_medium_identity_and_cache_without_subtitle_fallback(final_audio_cache, monkeypatch):
    executor, video, calls = final_audio_cache
    monkeypatch.setattr(executor, "_subtitle_fallback", lambda *a, **k: pytest.fail("subtitle strategy leaked into final QC"))
    settings = Settings(_env_file=None, asr_model="medium", asr_device="cpu", asr_compute_type="int8",
                        subtitle_alignment_fallback_model_dir="configured-only-for-subtitles")
    evidence = executor.final_audio_evidence(video, asr_model=settings.asr_model, asr_device=settings.asr_device,
                                             asr_compute_type=settings.asr_compute_type)
    assert calls == [("medium", "cpu", "int8")]
    path = executor.final_audio_evidence_path(evidence)
    before = path.read_bytes()
    assert executor.final_audio_evidence(video, asr_model="medium", asr_device="cpu", asr_compute_type="int8") == evidence
    assert path.read_bytes() == before and calls == [("medium", "cpu", "int8")]
