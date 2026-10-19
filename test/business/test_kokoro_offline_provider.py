"""Offline Kokoro contracts without installing the optional model runtime."""

import hashlib
import importlib.metadata
import json
import subprocess
import sys
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from glance_core.database import build_engine, create_db
from glance_core.models import RuntimeSetting
from glance_core.runtime_config import EDITABLE_KEYS, effective_settings, save_overrides
from glance_core.settings import Settings, validate_tts_provider_endpoint
from glance_media import executor as media
from glance_media.tts import TtsResult, get_tts_provider
from glance_media.tts import kokoro
from sqlmodel import Session


def digest(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    files = {**kokoro._REQUIRED_FILES, "espeak-ng-data/en_dict": "data",
             "dict/zh.dict": "data", "licenses/LICENSE.txt": "license"}
    rows = []
    for name, role in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = f"fixture {name}".encode()
        path.write_bytes(data)
        rows.append({"path": name, "role": role, "size_bytes": len(data), "sha256": digest(data)})
    license_file = next(row for row in rows if row["role"] == "license")
    manifest = {
        "schema_version": 1, "model_id": kokoro.MODEL_ID,
        "runtime": {"distribution": "sherpa-onnx", "version": "1.13.8", "core_version": "1.13.8",
                    "python": "cp311", "platform": "win_amd64", "provider": "cpu",
                    "bundled_onnxruntime_version": "1.28.2"},
        "files": rows,
        "licenses": [{"path": license_file["path"], "sha256": license_file["sha256"],
                      "spdx_id": "Apache-2.0", "scope": "fixture only", "source_url": "https://example.com/license"}],
    }
    (root / kokoro.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return root, manifest


def write_manifest(bundle):
    root, manifest = bundle
    (root / kokoro.MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")


def test_complete_bundle_freezes_all_bytes_and_license_text(bundle):
    root, manifest = bundle
    evidence = kokoro.verify_kokoro_bundle(str(root))
    assert evidence["manifest_sha256"] == digest((root / kokoro.MANIFEST_NAME).read_bytes())
    assert evidence["files"] == manifest["files"]
    assert evidence["runtime"] == manifest["runtime"]
    assert evidence["licenses"][0]["text"] == "fixture licenses/LICENSE.txt"
    assert "does not independently establish" in evidence["rights_summary"]


def test_frozen_supporting_license_documents_preserve_hashes_without_inventing_spdx(bundle):
    root, manifest = bundle
    for name in sorted(kokoro._SUPPORTING_LICENSE_DOCUMENTS):
        data = f"supporting evidence {name}".encode()
        (root / name).write_bytes(data)
        manifest["files"].append({"path": name, "role": "license", "size_bytes": len(data), "sha256": digest(data)})
    write_manifest(bundle)
    evidence = kokoro.verify_kokoro_bundle(str(root))
    docs = evidence["supporting_license_documents"]
    assert {row["path"] for row in docs} == kokoro._SUPPORTING_LICENSE_DOCUMENTS
    assert all(row["kind"] == "supporting_document" and "spdx_id" not in row for row in docs)
    assert all(row["sha256"] == digest(row["text"].encode()) for row in docs)


def test_license_and_supporting_text_round_trip_exact_crlf_bytes(bundle):
    root, manifest = bundle
    for name in ("licenses/LICENSE.txt", "licenses/THIRD_PARTY_NOTICES.txt"):
        data = b"Exact license bytes\r\nSecond line\r\n"
        (root / name).write_bytes(data)
        row = {"path": name, "role": "license", "size_bytes": len(data), "sha256": digest(data)}
        manifest["files"] = [item for item in manifest["files"] if item["path"] != name] + [row]
        if name == "licenses/LICENSE.txt":
            manifest["licenses"][0]["sha256"] = row["sha256"]
    write_manifest(bundle)
    evidence = kokoro.verify_kokoro_bundle(str(root))
    for row in evidence["licenses"] + evidence["supporting_license_documents"]:
        assert row["text"].encode("utf-8") == (root / row["path"]).read_bytes()
        assert digest(row["text"].encode("utf-8")) == row["sha256"]


@pytest.mark.parametrize("tamper", ["file", "missing", "extra", "size", "runtime", "license", "missing-license", "unsafe", "duplicate", "data", "dictionary"])
def test_bundle_fails_closed_before_optional_runtime(bundle, monkeypatch, tamper):
    root, manifest = bundle
    if tamper == "file":
        (root / "model.onnx").write_bytes(b"tampered")
    elif tamper == "missing":
        (root / "voices.bin").unlink()
    elif tamper == "extra":
        (root / "undeclared").write_text("unexpected")
    elif tamper == "size":
        manifest["files"][0]["size_bytes"] += 1
    elif tamper == "runtime":
        manifest["runtime"]["version"] = "unreviewed"
    elif tamper == "license":
        manifest["licenses"][0]["sha256"] = "0" * 64
    elif tamper == "missing-license":
        manifest["licenses"] = []
    elif tamper == "unsafe":
        manifest["files"][0]["path"] = "../outside"
    elif tamper == "duplicate":
        manifest["files"].append(dict(manifest["files"][0]))
    else:
        prefix = "dict/" if tamper == "dictionary" else "espeak-ng-data/"
        for row in list(manifest["files"]):
            if row["path"].startswith(prefix):
                (root / row["path"]).unlink()
                manifest["files"].remove(row)
    write_manifest(bundle)
    monkeypatch.setattr(kokoro, "_runtime", lambda: pytest.fail("runtime used before bundle verification"))
    with pytest.raises(ValueError):
        kokoro.KokoroProvider(str(root)).synthesize("中文", root / "out.mp3", voice="kokoro:58", rate="+20%", volume="+0%")


@pytest.mark.parametrize("relative", ["/outside", "a\\b", "C:/outside", "./model.onnx", "a//b", "a/../b"])
def test_manifest_paths_are_canonical_relative_paths(relative):
    with pytest.raises(ValueError):
        kokoro._relative_path(relative)


def test_redirected_bundle_file_is_rejected(bundle, tmp_path):
    root, _ = bundle
    outside = tmp_path / "model"
    outside.write_bytes((root / "model.onnx").read_bytes())
    (root / "model.onnx").unlink()
    (root / "model.onnx").symlink_to(outside)
    with pytest.raises(ValueError, match="redirected"):
        kokoro.verify_kokoro_bundle(str(root))


@pytest.mark.parametrize("voice,rate,volume", [
    ("zh-CN-YunxiNeural", "+20%", "+0%"), ("kokoro:0", "+20%", "+0%"),
    ("kokoro:58", "NaN%", "+0%"), ("kokoro:58", "+101%", "+0%"),
    ("kokoro:58", "-51%", "+0%"), ("kokoro:58", "+20%", "-100%"),
    ("kokoro:58", "1; command", "+0%"),
])
def test_invalid_voice_and_numeric_parameters_fail(voice, rate, volume):
    with pytest.raises(ValueError):
        kokoro.kokoro_parameters(voice, rate, volume)


@pytest.fixture
def fake_runtime(monkeypatch):
    calls = {}

    def config(**kwargs):
        return SimpleNamespace(**kwargs)

    def tts_config(**kwargs):
        calls["config"] = config(**kwargs)
        calls["config"].validate = lambda: True
        return calls["config"]

    def engine(configuration):
        assert configuration == calls["config"]

        def generate(text, **kwargs):
            calls["generate"] = (text, kwargs)
            return calls["audio"]

        return SimpleNamespace(generate=generate)

    calls["audio"] = SimpleNamespace(samples=np.array([-0.5, 0, 0.5], dtype=np.float32), sample_rate=24000)
    runtime = SimpleNamespace(OfflineTtsConfig=tts_config, OfflineTtsModelConfig=config,
                              OfflineTtsKokoroModelConfig=config, OfflineTts=engine)
    monkeypatch.setattr(kokoro, "_runtime", lambda: runtime)

    def convert(args, **kwargs):
        if args[0] == "ffprobe":
            calls["ffprobe"] = (args, kwargs)
            return SimpleNamespace(stdout='{"format":{"duration":"0.048"}}')
        calls["subprocess"] = (args, kwargs)
        with wave.open(args[args.index("-i") + 1], "rb") as wav:
            calls["wav"] = (wav.getparams(), wav.readframes(wav.getnframes()))
        Path(args[-1]).write_bytes(b"converted mp3 fixture")

    monkeypatch.setattr(kokoro.subprocess, "run", convert)
    return calls


def test_cpu_chinese_generation_pcm_and_receipt(bundle, fake_runtime, tmp_path):
    root, _ = bundle
    result = get_tts_provider("kokoro", model_dir=str(root)).synthesize(
        "中文 2026年10月", tmp_path / "voice.mp3", voice="kokoro:58", rate="+20%", volume="+0%")
    assert fake_runtime["generate"] == ("中文 2026年10月", {"sid": 58, "speed": 1.2})
    config = fake_runtime["config"]
    assert config.model.provider == "cpu" and config.model.num_threads == 2
    assert config.model.kokoro.model == str(root / "model.onnx")
    assert config.model.kokoro.dict_dir == str(root / "dict")
    assert config.model.kokoro.lexicon == f"{root / 'lexicon-us-en.txt'},{root / 'lexicon-zh.txt'}"
    assert config.rule_fsts == ",".join(str(root / name) for name in ("phone-zh.fst", "date-zh.fst", "number-zh.fst"))
    params, pcm = fake_runtime["wav"]
    assert (params.nchannels, params.sampwidth, params.framerate) == (1, 2, 24000)
    assert np.frombuffer(pcm, dtype="<i2").tolist() == [-16384, 0, 16384]
    command, options = fake_runtime["subprocess"]
    assert command[0] == "ffmpeg" and "-nostdin" in command
    assert options["shell"] is False and options["check"] is True
    assert options["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    assert result.duration_sec == 0.048 and result.boundaries == []
    assert fake_runtime["ffprobe"][1]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
    receipt = result.provenance
    assert receipt["provider"] == "kokoro" and receipt["voice"] == {"id": "zm_009", "sid": 58}
    assert receipt["output_sha256"] == digest(result.audio_path.read_bytes())
    assert receipt["narration_sha256"] == digest("中文 2026年10月".encode())
    assert receipt["audio"]["pcm16_sha256"] == digest(pcm)
    assert receipt["audio"]["pcm_duration_sec"] == 3 / 24000
    assert receipt["audio"]["encoded_duration_sec"] == result.duration_sec
    assert receipt["generated_at"] and receipt["bundle"]["licenses"][0]["text"]


@pytest.mark.parametrize("voice,sid,name", [("kokoro:58", 58, "zm_009"), ("zm_009", 58, "zm_009"),
                                          ("kokoro:3", 3, "zf_001"), ("zf_001", 3, "zf_001")])
def test_only_two_approved_prebuilt_voices_record_truthful_canonical_pairs(bundle, fake_runtime, tmp_path, voice, sid, name):
    result = kokoro.KokoroProvider(str(bundle[0])).synthesize("中文", tmp_path / "voice.mp3", voice=voice, rate="+0%", volume="+0%")
    assert fake_runtime["generate"] == ("中文", {"sid": sid, "speed": 1.0})
    assert result.provenance["voice"] == {"id": name, "sid": sid}
    settings = Settings(_env_file=None, tts_provider="kokoro", tts_voice=voice)
    assert settings.tts_voice == voice


@pytest.mark.parametrize("samples,rate", [([], 24000), ([float("nan")], 24000),
    ([float("inf")], 24000), ([[0.2]], 24000), ([0.0], 24000), ([1.01], 24000), ([0.2], 16000)])
def test_invalid_pcm_never_reaches_converter(bundle, fake_runtime, tmp_path, samples, rate):
    fake_runtime["audio"] = SimpleNamespace(samples=samples, sample_rate=rate)
    with pytest.raises(RuntimeError):
        kokoro.KokoroProvider(str(bundle[0])).synthesize("中文", tmp_path / "out.mp3",
                                                      voice="zm_009", rate="+20%", volume="+0%")
    assert "subprocess" not in fake_runtime


def test_wrong_runtime_version_has_no_fallback(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "1.13.7")
    with pytest.raises(RuntimeError, match="pinned"):
        kokoro._runtime()


@pytest.mark.parametrize("field,value", [("python", "cp312"), ("platform", "linux_x86_64"),
    ("provider", "cuda"), ("bundled_onnxruntime_version", "1.27.0"), ("extra", "undeclared")])
def test_bundle_runtime_matches_actual_frozen_windows_schema(bundle, field, value):
    root, manifest = bundle
    assert kokoro.verify_kokoro_bundle(str(root))["runtime"]["bundled_onnxruntime_version"] == "1.28.2"
    manifest["runtime"][field] = value
    write_manifest(bundle)
    with pytest.raises(ValueError, match="pinned contract"):
        kokoro.verify_kokoro_bundle(str(root))


def test_compatible_tuple_result_and_existing_providers(tmp_path):
    result = TtsResult(tmp_path / "edge.mp3", 2, [], "edge")
    assert tuple(result) == (result.audio_path, 2) and result.provenance == {}
    assert get_tts_provider("edge").name == "edge"
    assert get_tts_provider("cosyvoice3", "http://127.0.0.1:8765", "token").name == "cosyvoice3"
    with pytest.raises(ValueError, match="endpoint"):
        get_tts_provider("kokoro", "http://127.0.0.1:8765")


def test_executor_atomically_publishes_and_returns_generation_receipt(bundle, fake_runtime, tmp_path):
    executor = media.MediaExecutor(tmp_path / "content")
    first = executor.synthesize_voice("中文", provider="kokoro", model_dir=str(bundle[0]), voice="kokoro:58")
    receipt_path = first.audio_path.with_name("tts_generation_receipt.json")
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == first.provenance
    assert first.audio_path.with_name("word_boundaries.json").read_text() == "[]"
    original = first.audio_path.read_bytes()
    fake_runtime["audio"].samples = [float("nan")]
    with pytest.raises(RuntimeError):
        executor.synthesize_voice("second", provider="kokoro", model_dir=str(bundle[0]), voice="kokoro:58")
    assert first.audio_path.read_bytes() == original
    assert list((executor.root / "audio").iterdir()) == [first.audio_path.parent]


def test_executor_rejects_forged_receipt_before_publication(tmp_path, monkeypatch):
    class Provider:
        def synthesize(self, narration, output, **kwargs):
            output.write_bytes(b"fixture")
            return TtsResult(output, 1, [], "kokoro", provenance={"provider": "edge"})

    monkeypatch.setattr(media, "get_tts_provider", lambda *args, **kwargs: Provider())
    executor = media.MediaExecutor(tmp_path)
    with pytest.raises(media.NonRetryableMediaError, match="receipt"):
        executor.synthesize_voice("中文", provider="kokoro")
    assert list((tmp_path / "audio").iterdir()) == []


@pytest.mark.parametrize("provider", ["edge", "kokoro"])
def test_kokoro_executor_never_accepts_unverified_or_fallback_result(tmp_path, monkeypatch, provider):
    class Provider:
        def synthesize(self, narration, output, **kwargs):
            output.write_bytes(b"fixture")
            return TtsResult(output, 1, [], provider)

    monkeypatch.setattr(media, "get_tts_provider", lambda *args, **kwargs: Provider())
    with pytest.raises(media.NonRetryableMediaError, match="offline generation receipt"):
        media.MediaExecutor(tmp_path).synthesize_voice("中文", provider="kokoro")
    assert list((tmp_path / "audio").iterdir()) == []


def test_kokoro_settings_and_runtime_pair_preserve_server_model_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("GLANCE_KOKORO_MODEL_DIR", str(tmp_path / "server-model"))
    base = Settings(_env_file=None, database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    assert "kokoro_model_dir" not in EDITABLE_KEYS
    engine = build_engine(base)
    create_db(engine)
    with Session(engine) as session:
        with pytest.raises(ValueError, match="Kokoro"):
            save_overrides(session, {"tts_provider": "kokoro"})
        save_overrides(session, {"tts_provider": "kokoro", "tts_voice": "kokoro:58",
                                 "kokoro_model_dir": "attacker-controlled"})
        effective = effective_settings(session, base)
        assert effective.tts_provider == "kokoro" and effective.tts_voice == "kokoro:58"
        assert effective.kokoro_model_dir == str(tmp_path / "server-model")
        assert session.get(RuntimeSetting, "kokoro_model_dir") is None
        with pytest.raises(ValueError):
            save_overrides(session, {"tts_provider_endpoint": "http://127.0.0.1:8765"})
    engine.dispose()
    assert validate_tts_provider_endpoint("kokoro", "") == ("kokoro", "")
    with pytest.raises(ValueError):
        Settings(_env_file=None, tts_provider="kokoro", tts_voice="zh-CN-YunxiNeural")


def test_importing_provider_does_not_import_torch_or_optional_runtime():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; import glance_media.tts; "
         "assert not any(n in sys.modules for n in ('torch', 'sherpa_onnx', 'soundfile'))"],
        capture_output=True, text=True, timeout=15, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert result.returncode == 0, result.stderr
