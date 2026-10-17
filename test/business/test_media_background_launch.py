import array
import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest

from app.services import video_project
from glance_media import executor


def test_media_run_command_hides_window(monkeypatch, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(executor.subprocess, "run", run)
    executor.run_command(["ffmpeg", "-version"], cwd=tmp_path)

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_media_probe_hides_window(monkeypatch, tmp_path):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"streams": [], "format": {}}')

    monkeypatch.setattr(executor.subprocess, "run", run)
    assert executor.probe(tmp_path / "video.mp4") == {"streams": [], "format": {}}

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_media_capability_probe_hides_window(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="..C scale V->V scale\n")

    monkeypatch.setattr(executor, "_ffmpeg_filter_cache", None)
    monkeypatch.setattr(executor.subprocess, "run", run)
    assert executor.available_ffmpeg_filters() == frozenset({"scale"})

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_final_audio_decode_hides_window(monkeypatch, tmp_path):
    calls = []
    samples = array.array("h", [1000] * 1600).tobytes()
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fixture")
    media = executor.MediaExecutor(tmp_path / "content")

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=samples)

    monkeypatch.setattr(executor.subprocess, "run", run)
    evidence = media.final_audio_evidence(video)

    assert evidence["signal_status"] == "available"
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.fixture
def final_audio_cache(monkeypatch, tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"final-track-fixture")
    media = executor.MediaExecutor(tmp_path / "content")
    calls = []

    def recognize(model, device, compute, *, video_path):
        assert video_path == video
        calls.append((model, device, compute))
        return [{"start_sec": 0, "end_sec": 0.1, "text": model}], ""

    monkeypatch.setattr(media, "_local_asr", recognize)
    monkeypatch.setattr(executor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        stdout=array.array("h", [1000] * 1600).tobytes()
    ))
    return media, video, calls


@pytest.mark.parametrize("change", [
    {"asr_model": "large-v3"}, {"asr_device": "cuda"}, {"asr_compute_type": "int8"},
])
def test_final_audio_cache_isolates_asr_identity(final_audio_cache, change):
    media, video, calls = final_audio_cache
    original = media.final_audio_evidence(video, asr_model="medium")
    original_path, = (media.root / "render").glob("audio-evidence-*.json")
    original_bytes = original_path.read_bytes()

    changed = media.final_audio_evidence(video, **{"asr_model": "medium", **change})

    assert changed != original
    assert original_path.read_bytes() == original_bytes
    assert len(list((media.root / "render").glob("audio-evidence-*.json"))) == 2
    assert media.final_audio_evidence(video, asr_model="medium") == original
    assert len(calls) == 2


def test_final_audio_cache_hits_same_identity(final_audio_cache, monkeypatch):
    media, video, calls = final_audio_cache
    original = media.final_audio_evidence(video, asr_model="medium")
    path, = (media.root / "render").glob("audio-evidence-*.json")
    original_bytes = path.read_bytes()
    monkeypatch.setattr(executor.subprocess, "run", lambda *args, **kwargs: pytest.fail("cache must skip PCM"))

    assert media.final_audio_evidence(video, asr_model="medium") == original
    assert path.read_bytes() == original_bytes
    assert len(calls) == 1


def test_final_audio_cache_path_uses_only_complete_stable_identity(final_audio_cache):
    media, video, _ = final_audio_cache
    evidence = media.final_audio_evidence(video, asr_model="medium")
    identity = {key: evidence[key] for key in (
        "version", "method", "video_sha256", "asr_model", "asr_device", "asr_compute_type",
    )}
    fingerprint = hashlib.sha256(json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()

    assert media.final_audio_evidence_path(evidence) == media.root / "render" / f"audio-evidence-{fingerprint}.json"
    assert media.final_audio_evidence_path(dict(reversed(list(identity.items())))) == media.final_audio_evidence_path(evidence)
    for key in identity:
        assert media.final_audio_evidence_path({**identity, key: "changed"}) != media.final_audio_evidence_path(evidence)


def test_final_audio_cache_reads_matching_legacy_without_rewriting(final_audio_cache, monkeypatch):
    media, video, calls = final_audio_cache
    original = media.final_audio_evidence(video, asr_model="medium")
    path, = (media.root / "render").glob("audio-evidence-*.json")
    path.unlink()
    legacy = path.parent / f"audio-evidence-{original['video_sha256']}.json"
    original_bytes = (json.dumps(original, indent=2) + "\n").encode("utf-8")
    legacy.write_bytes(original_bytes)
    monkeypatch.setattr(executor.subprocess, "run", lambda *args, **kwargs: pytest.fail("legacy cache must skip PCM"))

    assert media.final_audio_evidence(video, asr_model="medium") == original
    assert legacy.read_bytes() == original_bytes
    assert list(legacy.parent.glob("audio-evidence-*.json")) == [legacy]
    assert len(calls) == 1


@pytest.mark.parametrize("damage", ["json", "identity", "signal"])
def test_final_audio_cache_rebuilds_invalid_identity_without_legacy_fallback(final_audio_cache, damage):
    media, video, calls = final_audio_cache
    original = media.final_audio_evidence(video, asr_model="medium")
    identity = media.final_audio_evidence_path(original)
    legacy = identity.parent / f"audio-evidence-{original['video_sha256']}.json"
    legacy_bytes = json.dumps(original, indent=2).encode("utf-8")
    legacy.write_bytes(legacy_bytes)
    invalid = {**original, **({"asr_device": "cuda"} if damage == "identity" else {"signal_status": "unavailable"})}
    identity.write_text("{" if damage == "json" else json.dumps(invalid), encoding="utf-8")

    assert media.final_audio_evidence(video, asr_model="medium") == original
    assert json.loads(identity.read_text(encoding="utf-8")) == original
    assert legacy.read_bytes() == legacy_bytes
    assert len(calls) == 2


@pytest.mark.parametrize("change", [
    {"version": 2}, {"method": "other"}, {"video_sha256": "a" * 64},
    {"asr_model": "large-v3"}, {"asr_device": "cuda"}, {"asr_compute_type": "int8"},
    {"asr_status": "unavailable"}, {"segments": []}, {"signal_status": "unavailable"},
    {"signal": {"window_sec": 0.2}},
])
def test_final_audio_cache_preserves_mismatched_legacy(final_audio_cache, change):
    media, video, calls = final_audio_cache
    original = media.final_audio_evidence(video, asr_model="medium")
    path, = (media.root / "render").glob("audio-evidence-*.json")
    path.unlink()
    legacy = path.parent / f"audio-evidence-{original['video_sha256']}.json"
    original_bytes = json.dumps({**original, **change}, indent=2).encode("utf-8")
    legacy.write_bytes(original_bytes)

    assert media.final_audio_evidence(video, asr_model="medium") == original
    assert legacy.read_bytes() == original_bytes
    assert len(list(legacy.parent.glob("audio-evidence-*.json"))) == 2
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["disabled", "asr", "decode"])
def test_final_audio_cache_does_not_cache_incomplete_evidence(final_audio_cache, monkeypatch, failure):
    media, video, _ = final_audio_cache
    if failure == "asr":
        monkeypatch.setattr(media, "_local_asr", lambda *args, **kwargs: ([], "failed"))
    elif failure == "decode":
        def fail(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "ffmpeg")
        monkeypatch.setattr(executor.subprocess, "run", fail)

    evidence = media.final_audio_evidence(video, asr_model="" if failure == "disabled" else "medium")

    if failure == "decode":
        assert evidence["signal_status"] == "unavailable"
        assert evidence["signal_error_code"] == "FINAL_AUDIO_DECODE_FAILED"
    else:
        assert evidence["asr_status"] == "unavailable"
        assert evidence["asr_error_code"] == ("FINAL_ASR_DISABLED" if failure == "disabled" else "FINAL_ASR_FAILED_OR_EMPTY")
    assert not list((media.root / "render").glob("audio-evidence-*.json"))


def test_video_project_tool_version_hides_window(monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ffmpeg version test\n")

    monkeypatch.setattr(video_project.subprocess, "run", run)
    assert video_project._tool_version("ffmpeg") == "ffmpeg version test"

    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_video_project_run_hides_window(monkeypatch, tmp_path):
    calls = []

    class Process:
        returncode = 0

        def communicate(self, timeout):
            return "rendered", None

        def poll(self):
            return self.returncode

    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(video_project.subprocess, "Popen", popen)
    project = video_project.VideoProject(tmp_path / "project")

    assert (
        project._run(["ffmpeg", "-version"], tmp_path / "cancel.json", 5) == "rendered"
    )
    assert calls[0][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
