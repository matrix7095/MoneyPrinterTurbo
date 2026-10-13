from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from glance_agent import native_desktop
from glance_agent.native_desktop import (
    NativeDesktopResolutionError,
    enumerate_native_desktop_processes,
    resolve_native_desktop_binary,
)


def _binary(local_app_data: Path, build: str = "desktop-build") -> Path:
    path = local_app_data / "OpenAI" / "Codex" / "bin" / build / "codex.exe"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"test codex executable")
    return path


def _process(path: Path, *, name: str = "codex.exe") -> dict[str, str]:
    return {"Name": name, "ExecutablePath": str(path)}


def _version_runner(monkeypatch, output: str = "codex-cli 0.162.0-alpha.17.2\n"):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(native_desktop.subprocess, "run", run)
    return calls


def test_resolves_unique_running_desktop_binary_and_returns_identity(tmp_path, monkeypatch):
    executable = _binary(tmp_path)
    calls = _version_runner(monkeypatch)

    result = resolve_native_desktop_binary([_process(executable)], local_app_data=tmp_path)

    assert result.path == executable.resolve()
    assert result.path.is_absolute()
    assert result.sha256 == hashlib.sha256(executable.read_bytes()).hexdigest()
    assert result.version == "codex-cli 0.162.0-alpha.17.2"
    assert calls[0][0] == [str(executable.resolve()), "--version"]
    assert calls[0][1]["timeout"] == native_desktop.VERSION_TIMEOUT_SECONDS
    assert calls[0][1]["check"] is False
    assert calls[0][1]["shell"] is False
    assert calls[0][1]["encoding"] == "utf-8"


def test_deduplicates_multiple_processes_for_the_same_binary(tmp_path, monkeypatch):
    executable = _binary(tmp_path)
    calls = _version_runner(monkeypatch)

    result = resolve_native_desktop_binary(
        [_process(executable), _process(executable)], local_app_data=tmp_path
    )

    assert result.path == executable.resolve()
    assert len(calls) == 1


def test_rejects_when_only_codex_process_is_outside_desktop_vendor_root(tmp_path):
    outside = _binary(tmp_path / "other-local-app-data")

    with pytest.raises(NativeDesktopResolutionError, match="No running Codex Desktop binary"):
        resolve_native_desktop_binary([_process(outside)], local_app_data=tmp_path)


def test_ignores_outside_codex_process_when_unique_desktop_binary_is_running(
    tmp_path, monkeypatch
):
    outside = _binary(tmp_path / "other-local-app-data")
    desktop = _binary(tmp_path)
    calls = _version_runner(monkeypatch)

    result = resolve_native_desktop_binary(
        [_process(outside), _process(desktop)], local_app_data=tmp_path
    )

    assert result.path == desktop.resolve()
    assert len(calls) == 1


def test_rejects_multiple_running_desktop_binaries(tmp_path):
    first = _binary(tmp_path, "build-one")
    second = _binary(tmp_path, "build-two")

    with pytest.raises(NativeDesktopResolutionError, match="Multiple running"):
        resolve_native_desktop_binary(
            [_process(first), _process(second)], local_app_data=tmp_path
        )


def test_rejects_when_no_codex_process_is_running(tmp_path):
    with pytest.raises(NativeDesktopResolutionError, match="No running Codex Desktop binary"):
        resolve_native_desktop_binary([], local_app_data=tmp_path)


def test_rejects_missing_binary_file(tmp_path):
    missing = tmp_path / "OpenAI" / "Codex" / "bin" / "desktop-build" / "codex.exe"

    with pytest.raises(NativeDesktopResolutionError, match="does not exist"):
        resolve_native_desktop_binary([_process(missing)], local_app_data=tmp_path)


def test_rejects_invalid_json_from_powershell(monkeypatch):
    def run(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(native_desktop.subprocess, "run", run)

    with pytest.raises(NativeDesktopResolutionError, match="invalid process JSON"):
        enumerate_native_desktop_processes()


def test_reports_powershell_query_failure(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=1, stdout="", stderr="query failed")

    monkeypatch.setattr(native_desktop.subprocess, "run", run)

    with pytest.raises(NativeDesktopResolutionError, match="process query failed"):
        enumerate_native_desktop_processes()

    args, kwargs = calls[0]
    assert args[:4] == [
        str(native_desktop.POWERSHELL_EXECUTABLE),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
    ]
    assert "Get-CimInstance -ClassName Win32_Process" in args[-1]
    assert "-Property Name,ExecutablePath -ErrorAction Stop" in args[-1]
    assert "-Filter \"Name = 'codex.exe'\"" in args[-1]
    assert "Select-Object -Property Name,ExecutablePath" in args[-1]
    assert "CommandLine" not in args[-1]
    assert "Token" not in args[-1]
    assert "User" not in args[-1]
    assert kwargs["timeout"] == native_desktop.PROCESS_QUERY_TIMEOUT_SECONDS
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["shell"] is False


def test_reports_codex_version_failure(tmp_path, monkeypatch):
    executable = _binary(tmp_path)

    def run(args, **kwargs):
        assert args == [str(executable.resolve()), "--version"]
        assert kwargs["timeout"] == native_desktop.VERSION_TIMEOUT_SECONDS
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["shell"] is False
        return SimpleNamespace(returncode=1, stdout="", stderr="version failed")

    monkeypatch.setattr(native_desktop.subprocess, "run", run)

    with pytest.raises(NativeDesktopResolutionError, match="--version failed"):
        resolve_native_desktop_binary([_process(executable)], local_app_data=tmp_path)


def test_reports_powershell_timeout(monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(native_desktop.subprocess, "run", run)

    with pytest.raises(NativeDesktopResolutionError, match="process query failed"):
        enumerate_native_desktop_processes()


def test_ignores_other_process_names(tmp_path, monkeypatch):
    executable = _binary(tmp_path)
    calls = _version_runner(monkeypatch)

    result = resolve_native_desktop_binary(
        [_process(executable, name="codex-helper.exe"), _process(executable)],
        local_app_data=tmp_path,
    )

    assert result.path == executable.resolve()
    assert len(calls) == 1
