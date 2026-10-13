"""Local, frozen scheduler offers; never execute server-provided command text."""

import hashlib
import json
import re
import shlex
import time
from pathlib import Path


def offer_path(launcher: Path, request_id: str) -> Path:
    _, index, nonce = request_id.rsplit(":", 2)
    name = f"worker-{index}-{nonce}"
    return launcher.parent / "scheduler-offers" / f"{name}{launcher.suffix}"


def validate_worker(worker: dict, scheduler_id: str, root: Path, roles: dict) -> Path:
    if not isinstance(worker, dict):
        raise ValueError("invalid worker request shape")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scheduler_id):
        raise ValueError("invalid scheduler identity")
    role = worker.get("role_id")
    spec = roles.get(role)
    if not spec or worker.get("task_type") not in spec["task_types"]:
        raise ValueError("role/task mismatch")
    if worker.get("skill_name") != spec["skill_name"]:
        raise ValueError("role/skill mismatch")
    if worker.get("model") not in {"gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-6.1-sol", "gpt-6-luna"}:
        raise ValueError("unsupported model")
    if worker.get("reasoning_effort") not in {"medium", "high", "xhigh", "max"}:
        raise ValueError("unsupported effort")
    if not re.fullmatch(r"agt_[0-9a-f]{32}", str(worker.get("task_id", ""))):
        raise ValueError("invalid task identity")
    scope = ("run_id", "run_generation", "content_id", "input_hash")
    if any(field in worker for field in scope):
        if (
            not all(field in worker for field in scope)
            or not re.fullmatch(r"run_[0-9a-f]{32}", str(worker["run_id"]))
            or type(worker["run_generation"]) is not int or worker["run_generation"] < 1
            or not re.fullmatch(r"content_[0-9a-f]{32}", str(worker["content_id"]))
            or not isinstance(worker["input_hash"], str) or not worker["input_hash"]
        ):
            raise ValueError("invalid content run offer")
    if not re.fullmatch(
        re.escape(f"{scheduler_id}-{role}-") + r"[0-9a-f]{12}",
        str(worker.get("agent_id", "")),
    ):
        raise ValueError("invalid worker identity")
    if not re.fullmatch(
        re.escape(worker["task_type"]) + r":(?:[1-9]|1[0-6]):[0-9a-f]{12}",
        str(worker.get("request_id", "")),
    ):
        raise ValueError("invalid request identity")
    launcher = Path(worker["launcher"])
    if launcher.suffix not in {".ps1", ".sh"}:
        raise ValueError("unsupported launcher")
    expected = root / f"{scheduler_id}-{role}{launcher.suffix}"
    if launcher != expected or launcher.resolve() != expected or not launcher.is_file():
        raise ValueError("launcher outside controlled directory or missing")
    return launcher


def materialize_offer(worker: dict, scheduler_id: str, launcher: Path, deadline: float) -> None:
    path = offer_path(launcher, worker["request_id"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.resolve() != path.parent:
        raise ValueError("redirected offer directory")
    frozen = {
        key: worker[key]
        for key in (
            "request_id",
            "task_id",
            "task_type",
            "role_id",
            "skill_name",
            "model",
            "reasoning_effort",
            "agent_id",
            "launcher",
        )
    }
    frozen.update(
        scheduler_id=scheduler_id,
        deadline=deadline,
        launcher_hash=hashlib.sha256(launcher.read_bytes()).hexdigest(),
    )
    if "run_id" in worker:
        frozen.update({key: worker[key] for key in ("run_id", "run_generation", "content_id", "input_hash")})
    payload = json.dumps(frozen, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    envelope = path.with_suffix(".json")
    if path.suffix == ".ps1":

        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"

        command = (
            "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Extra)\n"
            "$ErrorActionPreference='Stop'\n"
            "try {\n"
            "if ($Extra.Count) { throw 'offer accepts no arguments' }\n"
            "$sha = [System.Security.Cryptography.SHA256]::Create()\n"
            "try {\n"
            f"$bytes = [System.IO.File]::ReadAllBytes({quote(launcher)})\n"
            "$hash = [System.BitConverter]::ToString($sha.ComputeHash($bytes)).Replace('-', '')\n"
            "} finally { $sha.Dispose() }\n"
            f"if ($hash -ne '{frozen['launcher_hash']}') {{ throw 'launcher changed' }}\n"
            f"& {quote(launcher)} dispatch-offer --offer {quote(envelope)} --digest '{digest}'\n"
            "exit $LASTEXITCODE\n"
            '} catch { Write-Output "HANDOFF_ERROR: offer execution failed '
            '($($_.CategoryInfo.Reason))"; exit 20 }\n'
        )
    else:
        command = (
            "#!/bin/sh\n"
            'if [ "$#" -ne 0 ]; then '
            "echo 'HANDOFF_ERROR: offer accepts no arguments'; exit 20; fi\n"
            f"if [ ! -f {shlex.quote(str(launcher))} ]; then "
            "echo 'HANDOFF_ERROR: launcher missing'; exit 20; fi\n"
            f"hash=$(sha256sum {shlex.quote(str(launcher))}) || "
            "{ echo 'HANDOFF_ERROR: cannot verify launcher'; exit 20; }\n"
            f'if [ "${{hash%% *}}" != {frozen["launcher_hash"]} ]; then '
            "echo 'HANDOFF_ERROR: launcher changed'; exit 20; fi\n"
            f"exec {shlex.quote(str(launcher))} dispatch-offer "
            f"--offer {shlex.quote(str(envelope))} --digest {digest}\n"
        )
    # Exclusive creation: an existing/consumed offer is never overwritten or redispatched.
    with envelope.open("xb") as stream:
        stream.write(payload)
    with path.open("x", encoding="utf-8-sig" if path.suffix == ".ps1" else "utf-8") as stream:
        stream.write(command)
    path.chmod(0o700)


def consume_offer(path: Path, digest: str, config, roles: dict) -> dict:
    if path != path.resolve() or path.suffix != ".json" or path.parent.name != "scheduler-offers":
        raise ValueError("invalid offer path")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("offer identity changed")
    worker = json.loads(payload)
    launcher = validate_worker(worker, worker["scheduler_id"], path.parent.parent, roles)
    if path != offer_path(launcher, worker["request_id"]).with_suffix(".json"):
        raise ValueError("offer path/identity mismatch")
    if config.role_id != worker["role_id"] or config.agent_id != (
        f"{worker['scheduler_id']}-{worker['role_id']}"
    ):
        raise ValueError("wrong role launcher identity")
    if hashlib.sha256(launcher.read_bytes()).hexdigest() != worker["launcher_hash"]:
        raise ValueError("launcher changed")
    deadline = worker["deadline"]
    if not isinstance(deadline, (float, int)) or not time.time() < deadline <= time.time() + 600:
        raise ValueError("expired/invalid offer")
    with path.with_suffix(".consumed").open("x"):
        pass
    return worker
