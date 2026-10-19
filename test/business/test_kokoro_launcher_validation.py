"""Exercise the launcher's exact Python validation without starting a server."""

import io
import json
import sys
from pathlib import Path

import ctranslate2
import pytest
from glance_media.tts import kokoro


def test_launcher_accepts_only_verified_local_kokoro(monkeypatch):
    script = (Path(__file__).resolve().parents[2] / "scripts/start-mpt-business.ps1").read_text()
    validation = script.split("$validationCode = @'\n", 1)[1].split("\n'@", 1)[0]
    calls = []
    monkeypatch.setattr(kokoro, "verify_kokoro_bundle", lambda path: calls.append(("bundle", path)))
    monkeypatch.setattr(kokoro, "_runtime", lambda: calls.append(("runtime", "1.13.8")))
    monkeypatch.setattr(ctranslate2, "get_supported_compute_types", lambda _: {"int8"})
    options = {"tts_provider": "kokoro", "tts_provider_endpoint": "", "tts_voice": "kokoro:58",
               "tts_rate": "+20%", "tts_volume": "+0%", "kokoro_model_dir": "C:/frozen/model",
               "asr_model": "small", "asr_device": "cpu", "asr_compute_type": "int8"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(options)))
    exec(compile(validation, "start-mpt-business.ps1 validation", "exec"), {})
    assert calls == [("bundle", "C:/frozen/model"), ("runtime", "1.13.8")]


@pytest.mark.parametrize("failure", ["bundle", "runtime"])
def test_launcher_bundle_or_runtime_failure_stops_before_server(monkeypatch, failure, capsys):
    script = (Path(__file__).resolve().parents[2] / "scripts/start-mpt-business.ps1").read_text()
    validation = script.split("$validationCode = @'\n", 1)[1].split("\n'@", 1)[0]

    def fail(*args):
        raise ValueError("private model path details")

    monkeypatch.setattr(kokoro, "verify_kokoro_bundle", fail if failure == "bundle" else lambda _: {})
    monkeypatch.setattr(kokoro, "_runtime", fail if failure == "runtime" else lambda: None)
    options = {"tts_provider": "kokoro", "tts_provider_endpoint": "", "tts_voice": "kokoro:58",
               "tts_rate": "+20%", "tts_volume": "+0%", "kokoro_model_dir": "C:/frozen/model",
               "asr_model": "small", "asr_device": "cpu", "asr_compute_type": "int8"}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(options)))
    with pytest.raises(SystemExit) as stopped:
        exec(compile(validation, "start-mpt-business.ps1 validation", "exec"), {})
    assert stopped.value.code == 2
    diagnostic = capsys.readouterr().out
    assert "kokoro_model_dir:pinned_local_bundle_or_runtime_unavailable" in diagnostic
    assert "private model path details" not in diagnostic
