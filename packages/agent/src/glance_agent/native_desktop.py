from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

POWERSHELL_EXECUTABLE = Path(r"C:\Program Files\PowerShell\7\pwsh.exe")
PROCESS_QUERY_TIMEOUT_SECONDS = 10
VERSION_TIMEOUT_SECONDS = 5
_PROCESS_QUERY = (
    "$ErrorActionPreference = 'Stop'; "
    "$processes = @(Get-CimInstance -ClassName Win32_Process "
    "-Filter \"Name = 'codex.exe'\" "
    "-Property Name,ExecutablePath -ErrorAction Stop | "
    "Select-Object -Property Name,ExecutablePath); "
    "ConvertTo-Json -InputObject $processes -Compress"
)


@dataclass(frozen=True, slots=True)
class NativeDesktopBinary:
    path: Path
    sha256: str
    version: str


class NativeDesktopResolutionError(RuntimeError):
    """The running Codex Desktop binary could not be resolved safely."""


def enumerate_native_desktop_processes() -> list[dict[str, object]]:
    """Read only process names and executable paths from Windows CIM."""
    try:
        result = subprocess.run(
            [
                str(POWERSHELL_EXECUTABLE),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _PROCESS_QUERY,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PROCESS_QUERY_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeDesktopResolutionError("PowerShell process query failed") from exc

    if result.returncode != 0:
        raise NativeDesktopResolutionError("PowerShell process query failed")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise NativeDesktopResolutionError("PowerShell returned invalid process JSON") from exc

    if payload is None:
        return []
    if isinstance(payload, Mapping):
        payload = [payload]
    if not isinstance(payload, list) or any(not isinstance(row, Mapping) for row in payload):
        raise NativeDesktopResolutionError("PowerShell returned an invalid process list")
    return [dict(row) for row in payload]


def resolve_native_desktop_binary(
    processes: Sequence[Mapping[str, object]] | None = None,
    *,
    local_app_data: Path | str | None = None,
) -> NativeDesktopBinary:
    """Identify the unique running Codex Desktop binary and report its identity."""
    if processes is None:
        processes = enumerate_native_desktop_processes()

    app_data = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not app_data:
        raise NativeDesktopResolutionError("LOCALAPPDATA is unavailable")
    vendor_bin_root = (Path(app_data) / "OpenAI" / "Codex" / "bin").resolve()

    candidates: dict[str, Path] = {}
    for process in processes:
        if not isinstance(process, Mapping):
            raise NativeDesktopResolutionError("PowerShell returned an invalid process row")
        name = process.get("Name")
        if not isinstance(name, str):
            raise NativeDesktopResolutionError("PowerShell returned a process without a name")
        if name.casefold() != "codex.exe":
            continue

        executable = process.get("ExecutablePath")
        if not isinstance(executable, str) or not executable.strip():
            raise NativeDesktopResolutionError("Running codex.exe has no executable path")
        path = Path(executable)
        if not path.is_absolute():
            raise NativeDesktopResolutionError("Running codex.exe path is not absolute")
        path = path.resolve()
        if (
            path.name.casefold() != "codex.exe"
            or os.path.normcase(str(path.parent.parent)) != os.path.normcase(str(vendor_bin_root))
        ):
            continue
        candidates[os.path.normcase(str(path))] = path

    if not candidates:
        raise NativeDesktopResolutionError("No running Codex Desktop binary was found")
    if len(candidates) != 1:
        raise NativeDesktopResolutionError("Multiple running Codex Desktop binaries were found")

    path = next(iter(candidates.values()))
    if not path.is_file():
        raise NativeDesktopResolutionError("Running Codex Desktop binary does not exist")
    try:
        with path.open("rb") as stream:
            sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError as exc:
        raise NativeDesktopResolutionError("Running Codex Desktop binary could not be read") from exc

    try:
        result = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=VERSION_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NativeDesktopResolutionError("Codex Desktop --version failed") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise NativeDesktopResolutionError("Codex Desktop --version failed")

    return NativeDesktopBinary(path=path, sha256=sha256, version=result.stdout.strip())
