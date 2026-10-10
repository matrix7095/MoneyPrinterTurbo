from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout_path: Path
    stderr_path: Path


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int) -> None:
        super().__init__(f"Command exited with {returncode}: {args[0]}")
        self.args_list = tuple(args)
        self.returncode = returncode


class CommandRunner:
    """Run allowlisted tools without a shell and persist both output streams."""

    def __init__(self, allowed_executables: set[str]) -> None:
        self.allowed_executables = {item.casefold() for item in allowed_executables}

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        stdout_path: Path,
        stderr_path: Path,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        if not args:
            raise ValueError("Command arguments cannot be empty")
        executable = Path(args[0]).name.casefold()
        if executable not in self.allowed_executables:
            raise ValueError(f"Executable is not allowlisted: {executable}")
        cwd = cwd.resolve()
        if not cwd.is_dir():
            raise ValueError(f"Working directory does not exist: {cwd}")
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        stderr_path.parent.mkdir(parents=True, exist_ok=True)
        safe_env = {"PATH": os.environ.get("PATH", "")}
        if env:
            safe_env.update(env)
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                list(args),
                cwd=cwd,
                env=safe_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                timeout=timeout_seconds,
                check=False,
                shell=False,
                creationflags=creationflags,
            )
        result = CommandResult(completed.returncode, stdout_path, stderr_path)
        if result.returncode:
            raise CommandError(args, result.returncode)
        return result
