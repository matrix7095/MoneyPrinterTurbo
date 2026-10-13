from __future__ import annotations

import hashlib
import hmac
import io
import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
import typer
from platformdirs import user_data_path
from pydantic import ValidationError

from glance_agent.client import AgentClient
from glance_agent.doctor import diagnostic_snapshot
from glance_agent.scheduler_handoff import consume_offer, materialize_offer, validate_worker
from glance_agent.settings import PUBLISH_REQUIRED_CAPABILITIES, AgentSettings
from glance_agent.validator import validate_result
from glance_agent.workspace import (
    file_sha256,
    gc_workspaces,
    load_json,
    prepare_workspace,
    remove_workspace,
    resolve_task_directory,
)

app = typer.Typer(no_args_is_help=False, add_completion=False)

_ROLE_SKILLS = {
    "discovery": "glance-discovery-agent",
    "assistant": "glance-assistant-agent",
    "research": "glance-research-agent",
    "script": "glance-script-agent",
    "visual": "glance-visual-agent",
    "image": "glance-image-agent",
    "video": "glance-video-agent",
    "publish": "glance-publish-agent",
}


def _role_token(root_token: str, role: str) -> str:
    signature = hmac.new(
        root_token.encode("utf-8"),
        f"glance-agent-role:{role}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"glance-role.{role}.{signature}"


def _scheduler_token(root_token: str, scheduler_id: str) -> str:
    signature = hmac.new(
        root_token.encode("utf-8"),
        f"glance-agent-scheduler:{scheduler_id}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"glance-scheduler.{scheduler_id}.{signature}"


_HTTP_STATUS_LABELS = {
    401: "UNAUTHORIZED",
    404: "NOT_FOUND",
    409: "CONFLICT",
    422: "REJECTED",
}

#: dispatch 退出码：有任务 0、无任务 10、已有在途任务 11、环境故障 20。
DISPATCH_EXIT_TASK = 0
DISPATCH_EXIT_NO_TASK = 10
DISPATCH_EXIT_AT_CAPACITY = 11
DISPATCH_EXIT_ENV_FAILURE = 20

#: 文案与服务端 glance_core.task_instructions 的模板同源；架构测试断言两者一致。
NO_TASK_MESSAGE = "本轮无任务，直接结束；不加载 Skill、记忆或工作区，不建待办不写总结。"
CAPACITY_MESSAGE = (
    "已有在途任务（AT_CAPACITY），本轮直接结束，不重复唤醒、dispatch、claim 或 inspect。"
)
ENV_FAILURE_TEMPLATE = (
    "环境故障（原文：{detail}），报告一次后结束本轮，不加载 Skill、不重试、不写入记忆。"
)

#: 同一 agent-id 已有 dispatch 在跑时的固定文案（BUG-8）。
DISPATCH_BUSY_MESSAGE = "已有 dispatch 在运行，本轮跳过。"


def _capacity_message(active_task_ids: list[str] | None = None) -> str:
    """Render the standalone fallback when the server omits its capacity message.

    The published Agent tool is intentionally installable without the Glance workspace
    packages. The normal server response already carries the canonical message; this
    fallback stays byte-for-byte aligned with ``CAPACITY_MESSAGE`` for malformed/legacy
    responses without importing ``glance_core`` at CLI startup.
    """
    suffix = ""
    if active_task_ids:
        suffix = " 当前在途任务：" + "、".join(active_task_ids) + "。"
    return CAPACITY_MESSAGE + suffix


def _sandbox_hint(detail: str, exc: BaseException) -> str:
    """EPERM/PermissionError 类失败追加沙箱提示，与服务未启动（Connection refused）区分。"""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    if any(isinstance(item, PermissionError) for item in chain) or "EPERM" in detail:
        return f"{detail}；疑似宿主沙箱禁止网络访问，请检查宿主的网络与写入权限配置"
    return detail


def output(value: dict) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False))


def _task_workspace(config: AgentSettings, requested_task_id: str) -> tuple[Path, str]:
    try:
        return resolve_task_directory(config.workspace_dir, requested_task_id)
    except FileNotFoundError as exc:
        output(
            {
                "status": "NO_WORKSPACE",
                "detail": str(exc),
                "hint": (
                    "本地任务 ID 或工作区未找到，服务器租约未经验证；这不代表 LEASE_EXPIRED。"
                    "从已下发 prompt 原样复制 task_id，或读取其绝对工作区中的 task.json；"
                    "不要猜测其他任务或据此上报租约过期。"
                ),
            }
        )
        raise typer.Exit(2) from None


def _task_response(response: dict, requested_task_id: str, resolved_task_id: str) -> dict:
    """Make a safe local task-ID correction visible to the calling Agent."""
    if response.get("lease_expires_at"):
        expiry = datetime.fromisoformat(str(response["lease_expires_at"]).replace("Z", "+00:00"))
        expiry = expiry.replace(tzinfo=UTC) if expiry.tzinfo is None else expiry.astimezone(UTC)
        response = {**response, "lease_expires_at": expiry.isoformat()}
    if requested_task_id == resolved_task_id:
        return response
    return {
        **response,
        "task_id": resolved_task_id,
        "requested_task_id": requested_task_id,
        "task_id_corrected": True,
    }


def _report_handoff_failure(
    client: AgentClient,
    config: AgentSettings,
    response: dict[str, object],
    *,
    error_code: str,
    message: str,
) -> bool:
    """Report one terminal dispatch handoff failure while the reservation token is available.

    A scheduled Worker has not started yet when local bundle materialization fails, so it
    cannot use the normal ``fail`` command: that command reads ``.lease.json`` from the
    task directory that failed to materialize.  The dispatch response still owns the
    reservation token, which lets the launcher close this exact handoff once without a
    retry or a second claim.
    """
    task_id = str(response.get("task_id") or "")
    lease_token = str(response.get("lease_token") or "")
    if not task_id or not lease_token:
        return False
    try:
        client.fail(
            task_id,
            lease_token,
            config.agent_id,
            retryable=False,
            error_code=error_code,
            message=message[:4000],
        )
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return False
    return True


@contextmanager
def _api_errors() -> Iterator[None]:
    """API 与 workspace 错误始终输出结构化 JSON，Agent 可直接解析，绝不落裸 traceback。"""
    try:
        yield
    except FileNotFoundError as exc:
        output({"status": "NO_WORKSPACE", "detail": str(exc)})
        raise typer.Exit(2) from None
    except OSError as exc:
        output({"status": "NO_WORKSPACE", "detail": str(exc)})
        raise typer.Exit(2) from None
    except httpx.HTTPStatusError as exc:
        response = exc.response
        try:
            detail = response.json().get("detail")
        except ValueError:
            detail = response.text[:500]
        output(
            {
                "status": _HTTP_STATUS_LABELS.get(response.status_code, "HTTP_ERROR"),
                "http_status": response.status_code,
                "detail": detail,
            }
        )
        raise typer.Exit(3) from None
    except httpx.HTTPError as exc:
        output({"status": "UNREACHABLE", "detail": _sandbox_hint(str(exc), exc)})
        raise typer.Exit(4) from None
    except ValidationError as exc:
        env_names = {
            "token": "GLANCE_AGENT_TOKEN",
            "GLANCE_AGENT_ID": "GLANCE_AGENT_ID",
            "role_id": "GLANCE_AGENT_ROLE_ID",
        }
        missing = [
            env_names.get(
                str(error["loc"][-1]),
                f"GLANCE_AGENT_{str(error['loc'][-1]).upper()}",
            )
            for error in exc.errors()
            if error.get("type") == "missing"
        ]
        output(
            {
                "status": "NO_CONFIG",
                "missing": missing,
                "hint": "请通过 bootstrap 生成的启动器执行",
            }
        )
        raise typer.Exit(2) from None


def settings() -> AgentSettings:
    config = AgentSettings()
    config.workspace_dir = _writable_workspace(config.workspace_dir)
    return config


def _writable_workspace(requested: Path) -> Path:
    """Return a workspace root that the current host can actually write.

    Launchers are normally generated inside the Glance checkout. A Codex Worker can
    run in a stricter sandbox than the process that generated that launcher, so the
    checkout-backed workspace may be readable but not writable. Probe the directory
    before dispatch; fall back to host-provided temporary storage before trying the
    current working directory and the standard per-user Agent data directory. The
    same resolved path is used by dispatch, confirm-claim, inspect and submit.
    """

    requested_path = requested.expanduser().resolve()
    temp_path = (Path(tempfile.gettempdir()) / "glance-agent" / "tasks").resolve()
    cwd_path = (Path.cwd() / ".glance-agent" / "tasks").resolve()
    fallback_path = (Path(user_data_path("glance-agent")) / "tasks").resolve()
    candidates: list[Path] = []
    for candidate in (requested_path, temp_path, cwd_path, fallback_path):
        if candidate not in candidates:
            candidates.append(candidate)
    first_error: OSError | None = None
    for candidate in tuple(candidates):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f".write-probe-{os.getpid()}"
            probe.write_text("", encoding="utf-8")
            with suppress(OSError):
                probe.unlink()
            return candidate
        except OSError as exc:
            first_error = first_error or exc
    detail = f"Agent 工作区不可写：{requested_path}；候选目录均不可写：" + "、".join(
        str(candidate) for candidate in candidates
    )
    raise PermissionError(detail) from first_error


@app.callback(invoke_without_command=True)
def root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        output({"status": "NO_COMMAND", "hint": "run: <launcher> dispatch"})
        raise typer.Exit(2)


def _process_alive(pid: int) -> bool:
    """跨平台进程存活检测：Windows 不能用 os.kill(pid, 0)（会终止进程）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def _dispatch_lock(config: AgentSettings) -> Iterator[bool]:
    """同 agent-id 的 dispatch 单实例保护（BUG-8）。

    锁文件写 PID，跨平台不依赖 flock；残留锁（PID 已死）自动接管。
    产出 True 表示拿到锁，False 表示已有存活的 dispatch 进程。
    """
    workspace = config.workspace_dir.expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    lock_path = workspace / f".dispatch-{config.agent_id}.lock"
    if lock_path.is_file():
        try:
            holder = int(json.loads(lock_path.read_text(encoding="utf-8")).get("pid") or 0)
        except (OSError, ValueError, TypeError):
            holder = 0
        if holder and holder != os.getpid() and _process_alive(holder):
            yield False
            return
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "agent_id": config.agent_id}), encoding="utf-8"
    )
    try:
        yield True
    finally:
        with suppress(OSError):
            if (
                lock_path.is_file()
                and int(json.loads(lock_path.read_text(encoding="utf-8")).get("pid") or 0)
                == os.getpid()
            ):
                lock_path.unlink()


def _download_context(
    client: AgentClient,
    bundle: dict,
    lease_token: str,
    workspace: Path,
    names: set[str] | None = None,
) -> None:
    context_files = bundle.get("context_files", [])
    if not context_files:
        return
    context_root = (workspace / "context").resolve()

    def target_for(name: str) -> Path:
        target = (context_root / name).resolve()
        if context_root not in target.parents:
            raise ValueError(f"上下文文件名越界：{name}")
        return target

    selected = [
        context_file
        for context_file in context_files
        if names is None or str(context_file.get("name") or "") in names
    ]
    if not selected:
        return
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(context_files)))) as pool:
        futures = [
            pool.submit(
                client.download_context,
                context_file["url"],
                lease_token,
                target_for(str(context_file["name"])),
            )
            for context_file in selected
        ]
        for future in futures:
            future.result()


def _manifest_gaps(bundle: dict, workspace: Path) -> list[str]:
    """按 context_manifest 逐项校对已下载素材（BUG-9）；返回不一致项描述。"""
    gaps: list[str] = []
    context_root = (workspace / "context").resolve()
    for entry in bundle.get("context_manifest", []) or []:
        name = str(entry.get("name") or "")
        reference = f"{name}（{entry.get('input_ref') or 'input'}）"
        if entry.get("missing"):
            gaps.append(f"{reference}：服务端素材文件不存在")
            continue
        target = (context_root / name).resolve()
        if context_root not in target.parents:
            gaps.append(f"{reference}：上下文文件名越界")
            continue
        if not target.is_file():
            gaps.append(f"{reference}：未下载到工作区")
            continue
        expected_size = int(entry.get("size_bytes") or 0)
        actual_size = target.stat().st_size
        if expected_size and actual_size != expected_size:
            gaps.append(f"{reference}：字节数不符（{actual_size} ≠ {expected_size}）")
            continue
        expected_hash = str(entry.get("sha256") or "")
        if expected_hash and file_sha256(target) != expected_hash:
            gaps.append(f"{reference}：内容哈希不符")
    return gaps


def _verify_context(
    client: AgentClient,
    bundle: dict,
    lease_token: str,
    workspace: Path,
) -> list[str]:
    """预检不通过时重下一次；仍不一致则返回缺失清单，禁止静默缺图开工。"""
    gaps = _manifest_gaps(bundle, workspace)
    if not gaps:
        return []
    # Only retry files that are absent/corrupt. A single bad thumbnail must not
    # redownload the other 92 verified scene images.
    expected_names = {
        str(entry.get("name") or "")
        for entry in bundle.get("context_manifest", []) or []
        if not entry.get("missing")
    }
    context_root = (workspace / "context").resolve()
    retry_names = {
        name
        for name in expected_names
        if not (context_root / name).resolve().is_file()
        or any(gap.startswith(f"{name}（") for gap in gaps)
    }
    _download_context(client, bundle, lease_token, workspace, names=retry_names)
    return _manifest_gaps(bundle, workspace)


def bundled_doctor_skill_path() -> Path:
    installed = Path(__file__).resolve().parent / "skill" / "glance-ops-doctor"
    if installed.is_dir():
        return installed
    checkout = Path(__file__).resolve().parents[2] / "skill" / "glance-ops-doctor"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("Bundled glance-ops-doctor skill was not found")


def bundled_scheduler_skill_path() -> Path:
    installed = Path(__file__).resolve().parent / "skill" / "glance-scheduler-agent"
    if installed.is_dir():
        return installed
    checkout = Path(__file__).resolve().parents[2] / "skill" / "glance-scheduler-agent"
    if checkout.is_dir():
        return checkout
    raise FileNotFoundError("Bundled glance-scheduler-agent skill was not found")


def bundled_role_skill_path(role: str) -> tuple[str, Path]:
    skill_name = _ROLE_SKILLS.get(role)
    if skill_name is None:
        raise ValueError(f"Unknown role {role!r}; choose {', '.join(_ROLE_SKILLS)}")
    installed = Path(__file__).resolve().parent / "skill" / skill_name
    if installed.is_dir():
        return skill_name, installed
    checkout = Path(__file__).resolve().parents[2] / "skill" / skill_name
    if checkout.is_dir():
        return skill_name, checkout
    raise FileNotFoundError(f"Bundled {skill_name} skill was not found")


def _bundled_shared_path() -> Path:
    for candidate in (
        Path(__file__).resolve().parent / "skill" / "_shared",
        Path(__file__).resolve().parents[2] / "skill" / "_shared",
    ):
        if candidate.is_dir():
            return candidate
    raise FileNotFoundError("Bundled shared Agent protocol was not found")


def _bundled_skill_version(source: Path) -> str:
    """读取捆绑技能的 role.json 版本；服务端据此比对发现安装副本漂移。"""
    role_file = source / "role.json"
    if not role_file.is_file():
        return ""
    try:
        return str(json.loads(role_file.read_text(encoding="utf-8")).get("skill_version") or "")
    except (OSError, ValueError):
        return ""


def _required_skill_version(source: Path) -> str:
    version = _bundled_skill_version(source)
    if not version:
        raise ValueError(f"Bundled Agent Skill is missing a valid role.json: {source}")
    return version


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        digest.update(file.relative_to(path).as_posix().encode())
        digest.update(file.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _launcher_environment(config: AgentSettings) -> dict[str, str]:
    capabilities = config.runtime_report()["capabilities"]
    return {
        "GLANCE_AGENT_API_URL": config.api_url,
        "GLANCE_AGENT_TOKEN": config.token.get_secret_value(),
        "GLANCE_AGENT_ID": config.agent_id,
        "GLANCE_AGENT_ROLE_ID": config.role_id,
        "GLANCE_AGENT_WORKSPACE_DIR": str(config.workspace_dir.expanduser().resolve()),
        "GLANCE_AGENT_APP_NAME": config.app_name,
        "GLANCE_AGENT_APP_VERSION": config.app_version,
        "GLANCE_AGENT_MODEL_NAME": config.model_name,
        "GLANCE_AGENT_MODEL_TIER": config.model_tier,
        "GLANCE_AGENT_REASONING_EFFORT": config.reasoning_effort,
        "GLANCE_AGENT_SKILL_NAME": config.skill_name,
        "GLANCE_AGENT_SKILL_VERSION": config.skill_version,
        "GLANCE_AGENT_DEVICE_LABEL": config.device_label,
        "GLANCE_AGENT_PROTOCOL_VERSION": config.protocol_version,
        "GLANCE_AGENT_CAPABILITIES": json.dumps(capabilities, ensure_ascii=False),
    }


def _missing_publish_capabilities(capabilities: list[str]) -> list[str]:
    configured = {value.strip() for value in capabilities if value.strip()}
    return [value for value in PUBLISH_REQUIRED_CAPABILITIES if value not in configured]


@app.command("create-launcher")
def create_launcher(
    output_path: Annotated[
        Path,
        typer.Option("--output", help="Absolute .ps1 or .sh launcher path."),
    ],
    skill_name: Annotated[str, typer.Option("--skill-name")] = "",
    skill_path: Annotated[
        Path | None,
        typer.Option(
            "--skill-path",
            help="Installed role Skill path; refresh its role.json version on each launch.",
        ),
    ] = None,
    capabilities: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help="Explicit host capability; repeat for each value (required for publish).",
        ),
    ] = None,
) -> None:
    """Write a CWD-independent launcher with explicit Agent configuration."""
    with _api_errors():
        config = settings()
    if skill_name:
        config.skill_name = skill_name
    if capabilities is not None:
        config.capabilities = list(capabilities)
    if config.role_id == "publish":
        config.capabilities = list(config.runtime_report()["capabilities"])
        missing = _missing_publish_capabilities(config.capabilities)
        if missing:
            output(
                {
                    "status": "PUBLISH_CAPABILITIES_REQUIRED",
                    "missing": missing,
                    "hint": "Repeat --capability for every required host capability.",
                }
            )
            raise typer.Exit(2)
    target = output_path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    executable_path = Path(sys.argv[0]).resolve()
    if os.name == "nt" and executable_path.suffix.lower() != ".exe":
        windows_executable = executable_path.with_suffix(".exe")
        if windows_executable.is_file():
            executable_path = windows_executable
    executable = str(executable_path)
    environment = _launcher_environment(config)
    resolved_skill_path = skill_path.expanduser().resolve() if skill_path else None
    if target.suffix.lower() == ".ps1":
        lines = [
            "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)",
            "$ErrorActionPreference = 'Stop'",
        ]
        lines.extend(
            "$env:{} = '{}'".format(key, value.replace("'", "''"))
            for key, value in environment.items()
        )
        if resolved_skill_path:
            escaped_executable = executable.replace("'", "''")
            escaped_skill_path = str(resolved_skill_path).replace("'", "''")
            lines.extend(
                [
                    f"$skillVersion = & '{escaped_executable}' skill-version "
                    f"--skill-path '{escaped_skill_path}'",
                    "if ($LASTEXITCODE -ne 0 -or "
                    "[string]::IsNullOrWhiteSpace(($skillVersion -join ''))) {",
                    f"    throw 'Unable to read Agent Skill version from {escaped_skill_path}'",
                    "}",
                    "$env:GLANCE_AGENT_SKILL_VERSION = "
                    "($skillVersion | Select-Object -Last 1).ToString().Trim()",
                ]
            )
        lines.extend(
            [
                # dispatch/confirm-claim 与其它子命令一样经参数转发；下面是条件唤起
                # （调度式）参考片段，具体唤起命令因宿主而异，以占位符表达：
                "# 条件唤起参考（调度式）：系统计时器执行本启动器 dispatch --wait 0，",
                "# 退出码 0 时用 stdout 唤起一次宿主会话（退出码 10/11/20 不唤起）：",
                "#   $prompt = & <本启动器> dispatch --wait 0",
                "#   if ($LASTEXITCODE -eq 0) { <宿主会话唤起命令> $prompt }",
                "# 若目标是 .sh，Windows PowerShell 必须经 Git Bash 调用，不得直接执行 .sh：",
                (
                    "#   $prompt = & 'C:\\Program Files\\Git\\bin\\bash.exe' "
                    "'<本启动器>.sh' dispatch --wait 0"
                ),
                "# 需覆盖触发瞬间到达的任务可用 --wait 5；切勿用满额长轮询。",
                "# 空 stdout/未知输出是 HANDOFF_ERROR，不是 NO_TASK；宿主只按明确状态行事。",
                "# 宿主会话只按任务 prompt 行事：不要自行 claim、inspect 或重复 dispatch。",
                "& '{}' @CommandArgs".format(executable.replace("'", "''")),
                "exit $LASTEXITCODE",
            ]
        )
    elif target.suffix.lower() == ".sh":
        import shlex

        lines = ["#!/usr/bin/env sh", "set -eu"]
        lines.extend(f"export {key}={shlex.quote(value)}" for key, value in environment.items())
        if resolved_skill_path:
            lines.extend(
                [
                    f"GLANCE_AGENT_SKILL_VERSION=$({shlex.quote(executable)} "
                    f"skill-version --skill-path {shlex.quote(str(resolved_skill_path))})",
                    "export GLANCE_AGENT_SKILL_VERSION",
                ]
            )
        lines.extend(
            [
                "# 条件唤起参考（调度式）：系统计时器执行本启动器 dispatch --wait 0，",
                "# 退出码 0 时用 stdout 唤起一次宿主会话（退出码 10/11/20 不唤起）：",
                '#   prompt="$(<本启动器> dispatch --wait 0)" && <宿主会话唤起命令> "$prompt"',
                "# Windows PowerShell 不得直接执行本 .sh；必须使用 Git Bash 的 bash.exe。",
                "# 需覆盖触发瞬间到达的任务可用 --wait 5；切勿用满额长轮询。",
                "# 空 stdout/未知输出是 HANDOFF_ERROR，不是 NO_TASK；宿主只按明确状态行事。",
                "# 宿主会话只按任务 prompt 行事：不要自行 claim、inspect 或重复 dispatch。",
                f'exec {shlex.quote(executable)} "$@"',
            ]
        )
    else:
        output({"status": "INVALID_PATH", "detail": "Launcher must end in .ps1 or .sh"})
        raise typer.Exit(2)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if target.suffix.lower() == ".sh":
        target.chmod(0o700)
    output(
        {
            "status": "CREATED",
            "path": str(target),
            "executable": executable,
            "cwd_independent": True,
        }
    )


@app.command("skill-version")
def skill_version(
    skill_path: Annotated[Path, typer.Option("--skill-path")],
) -> None:
    """Print the installed role Skill version for launcher self-refresh."""
    version = _bundled_skill_version(skill_path.expanduser().resolve())
    if not version:
        typer.echo(f"Skill version not found: {skill_path}", err=True)
        raise typer.Exit(2)
    typer.echo(version)


@app.command("install-role-skill")
def install_role_skill(
    role: Annotated[str, typer.Option("--role")],
    target: Annotated[Path, typer.Option("--target")],
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Install one platform-defined role Skill into an external Agent host."""
    try:
        skill_name, source = bundled_role_skill_path(role)
    except (ValueError, FileNotFoundError) as exc:
        output({"status": "INVALID_ROLE", "detail": str(exc)})
        raise typer.Exit(2) from None
    destination = target.expanduser().resolve() / skill_name
    if destination.exists() and not force:
        output({"status": "EXISTS", "path": str(destination), "hint": "Use --force."})
        raise typer.Exit(2)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    references = destination / "references"
    references.mkdir(exist_ok=True)
    for shared in _bundled_shared_path().glob("*.md"):
        shutil.copy2(shared, references / shared.name)
    output(
        {
            "status": "INSTALLED",
            "role_id": role,
            "skill_name": skill_name,
            "path": str(destination),
        }
    )


@app.command()
def gc() -> None:
    """Delete finished local task workspaces that no longer hold a lease."""
    config = settings()
    removed = gc_workspaces(config.workspace_dir)
    output({"status": "GC", "removed": removed, "workspace": str(config.workspace_dir)})


@app.command()
def status() -> None:
    """List active local workspaces without claiming another task."""
    with _api_errors():
        config = settings()
        tasks = []
        if config.workspace_dir.exists():
            for directory in sorted(config.workspace_dir.iterdir()):
                lease_file = directory / ".lease.json"
                task_file = directory / "task.json"
                if not lease_file.is_file() or not task_file.is_file():
                    continue
                lease = load_json(lease_file)
                if lease.get("agent_id") != config.agent_id:
                    continue
                task = load_json(task_file)
                tasks.append(
                    {
                        "task_id": task.get("task_id"),
                        "task_type": task.get("task_type"),
                        "lease_expires_at": task.get("lease_expires_at"),
                        "result_written": (directory / "result.json").is_file(),
                        "workspace": str(directory),
                    }
                )
        output({"status": "ACTIVE" if tasks else "IDLE", "tasks": tasks})


@app.command()
def selftest(
    launcher: Annotated[Path | None, typer.Option("--launcher")] = None,
    skill_path: Annotated[Path | None, typer.Option("--skill-path")] = None,
) -> None:
    """Verify token, role, launcher and Skill without claiming a task."""
    try:
        config = settings()
    except ValidationError:
        # BUG-2：直接执行时环境变量未注入，明确告知完整的启动器命令。
        launcher_path = str(launcher.expanduser().resolve()) if launcher else "<启动器路径>"
        output(
            {
                "status": "NO_CONFIG",
                "hint": (
                    "请通过启动器执行：sh "
                    f'"{launcher_path}" selftest'
                    "（Windows 用 pwsh -File）；直接运行 glance-agent 拿不到 GLANCE_AGENT_* 配置"
                ),
            }
        )
        raise typer.Exit(2) from None
    with _api_errors():
        server = AgentClient(config).whoami()
        launcher_ok = launcher is None or launcher.expanduser().resolve().is_file()
        skill_ok = skill_path is None or skill_path.expanduser().resolve().is_dir()
        findings: list[str] = []
        if server.get("known_device_label") not in ("", config.device_label):
            findings.append("WARN_ID_CONFLICT")
        output(
            {
                "status": "READY" if launcher_ok and skill_ok else "WARN",
                "server": server,
                "launcher": {"path": str(launcher or ""), "valid": launcher_ok},
                "skill": {
                    "path": str(skill_path or ""),
                    "valid": skill_ok,
                    "content_hash": _tree_hash(skill_path) if skill_ok and skill_path else "",
                },
                "findings": findings,
            }
        )


@app.command()
def bootstrap(
    api_url: Annotated[str, typer.Option("--api-url")],
    token: Annotated[str, typer.Option("--token")],
    agent_id: Annotated[str, typer.Option("--agent-id")],
    role: Annotated[str, typer.Option("--role")],
    skill_target: Annotated[Path, typer.Option("--skill-target")],
    launcher: Annotated[Path, typer.Option("--launcher")],
    app_name: Annotated[str, typer.Option("--app-name")] = "",
    model_name: Annotated[str, typer.Option("--model-name")] = "",
    model_tier: Annotated[str, typer.Option("--model-tier")] = "",
    reasoning_effort: Annotated[str, typer.Option("--reasoning-effort")] = "",
    device_label: Annotated[str, typer.Option("--device-label")] = "",
    capabilities: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help="Explicit host capability; repeat for each value (required for publish).",
        ),
    ] = None,
    request_selftest: Annotated[bool, typer.Option("--request-selftest/--no-selftest")] = True,
) -> None:
    """Install Skill, create launcher, self-test and report deployment."""
    configured_capabilities = list(capabilities or [])
    if role == "publish":
        if not configured_capabilities:
            configured_capabilities = list(PUBLISH_REQUIRED_CAPABILITIES)
        missing = _missing_publish_capabilities(configured_capabilities)
        if missing:
            output(
                {
                    "status": "PUBLISH_CAPABILITIES_REQUIRED",
                    "missing": missing,
                    "hint": "Repeat --capability for every required host capability.",
                }
            )
            raise typer.Exit(2)
    skill_name, source = bundled_role_skill_path(role)
    skill_version = _required_skill_version(source)
    destination = skill_target.expanduser().resolve() / skill_name
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination)
    references = destination / "references"
    references.mkdir(exist_ok=True)
    for shared in _bundled_shared_path().glob("*.md"):
        shutil.copy2(shared, references / shared.name)
    os.environ.update(
        {
            "GLANCE_AGENT_API_URL": api_url,
            "GLANCE_AGENT_TOKEN": token,
            "GLANCE_AGENT_ID": agent_id,
            "GLANCE_AGENT_ROLE_ID": role,
            "GLANCE_AGENT_SKILL_NAME": skill_name,
            "GLANCE_AGENT_SKILL_VERSION": skill_version,
            "GLANCE_AGENT_WORKSPACE_DIR": str(
                launcher.expanduser().resolve().parent / "workspaces"
            ),
            "GLANCE_AGENT_APP_NAME": app_name,
            "GLANCE_AGENT_MODEL_NAME": model_name,
            "GLANCE_AGENT_MODEL_TIER": model_tier,
            "GLANCE_AGENT_REASONING_EFFORT": reasoning_effort,
            "GLANCE_AGENT_DEVICE_LABEL": device_label or platform.node(),
            "GLANCE_AGENT_CAPABILITIES": json.dumps(configured_capabilities, ensure_ascii=False),
        }
    )
    with redirect_stdout(io.StringIO()):
        create_launcher(launcher, skill_name, destination, capabilities=configured_capabilities)
    config = settings()
    skill_hash = _tree_hash(destination)
    with _api_errors():
        whoami = AgentClient(config).whoami()
        report = AgentClient(config).deployment(
            {
                "agent_id": agent_id,
                "role_id": role,
                "launcher_path": str(launcher.expanduser().resolve()),
                "skill": {"name": skill_name, "path": str(destination), "content_hash": skill_hash},
                "host": {
                    "app_name": app_name,
                    "os": platform.system().lower(),
                    "device_label": device_label or platform.node(),
                },
                "model": {
                    "name": model_name,
                    "tier": model_tier,
                    "reasoning_effort": reasoning_effort,
                },
                "self_check": {
                    "cli": "pass",
                    "connectivity": "pass",
                    "skill_installed": "pass",
                    "launcher_executable": "pass",
                },
                "protocol_version": config.protocol_version,
                "request_selftest": request_selftest,
            }
        )
    output(
        {
            "status": "READY" if report["status"] == "VERIFIED" else report["status"],
            "launcher": str(launcher.expanduser().resolve()),
            "skill": {"name": skill_name, "path": str(destination), "content_hash": skill_hash},
            "server": {
                "reachable": True,
                "role_verified": whoami["role_id"] == role,
                "pending_tasks": whoami["pending_count"],
            },
            "findings": report.get("findings", []),
            "next_step": [
                "角色 Skill 与 launcher 已安装；不要为业务角色创建周期任务。",
                "业务 Worker 只能由 glance-scheduler-agent 按服务端 worker_request 创建。",
            ],
        }
    )


@app.command("bootstrap-scheduler")
def bootstrap_scheduler(
    api_url: Annotated[str, typer.Option("--api-url")],
    token: Annotated[str, typer.Option("--token")],
    scheduler_id: Annotated[str, typer.Option("--scheduler-id")],
    skill_target: Annotated[Path, typer.Option("--skill-target")],
    launcher_dir: Annotated[Path, typer.Option("--launcher-dir")],
    scheduler_launcher: Annotated[Path, typer.Option("--scheduler-launcher")],
    app_name: Annotated[str, typer.Option("--app-name")] = "Codex App",
    device_label: Annotated[str, typer.Option("--device-label")] = "",
    capabilities: Annotated[
        list[str] | None,
        typer.Option(
            "--capability",
            help="Explicit host capability; repeat for each value (required for publish).",
        ),
    ] = None,
) -> None:
    """Install all business Skills/launchers plus one read-only scheduler launcher."""
    suffix = scheduler_launcher.suffix.lower()
    if suffix not in {".ps1", ".sh"}:
        output({"status": "INVALID_PATH", "detail": "Scheduler launcher must end in .ps1 or .sh"})
        raise typer.Exit(2)
    try:
        scheduler_source = bundled_scheduler_skill_path()
        scheduler_skill_version = _required_skill_version(scheduler_source)
    except (FileNotFoundError, ValueError) as exc:
        output({"status": "INVALID_SKILL", "detail": str(exc)})
        raise typer.Exit(2) from None
    configured_capabilities = (
        list(capabilities) if capabilities is not None else list(PUBLISH_REQUIRED_CAPABILITIES)
    )
    missing_publish_capabilities = _missing_publish_capabilities(configured_capabilities)
    if missing_publish_capabilities:
        output(
            {
                "status": "PUBLISH_CAPABILITIES_REQUIRED",
                "missing": missing_publish_capabilities,
                "hint": "Repeat --capability for every required host capability.",
            }
        )
        raise typer.Exit(2)
    launcher_root = launcher_dir.expanduser().resolve()
    launcher_root.mkdir(parents=True, exist_ok=True)
    installed_workers: list[dict[str, str]] = []
    for role, skill_name in _ROLE_SKILLS.items():
        launcher = launcher_root / f"{scheduler_id}-{role}{suffix}"
        with redirect_stdout(io.StringIO()):
            bootstrap(
                api_url=api_url,
                token=_role_token(token, role),
                agent_id=f"{scheduler_id}-{role}",
                role=role,
                skill_target=skill_target,
                launcher=launcher,
                app_name=app_name,
                model_name="",
                model_tier="",
                reasoning_effort="",
                device_label=device_label,
                capabilities=configured_capabilities if role == "publish" else None,
                request_selftest=False,
            )
        installed_workers.append(
            {"role_id": role, "skill_name": skill_name, "launcher": str(launcher)}
        )

    scheduler_destination = skill_target.expanduser().resolve() / "glance-scheduler-agent"
    if scheduler_destination.exists():
        shutil.rmtree(scheduler_destination)
    scheduler_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(scheduler_source, scheduler_destination)
    os.environ.update(
        {
            "GLANCE_AGENT_API_URL": api_url,
            "GLANCE_AGENT_TOKEN": _scheduler_token(token, scheduler_id),
            "GLANCE_AGENT_ID": scheduler_id,
            "GLANCE_AGENT_ROLE_ID": "scheduler",
            "GLANCE_AGENT_SKILL_NAME": "glance-scheduler-agent",
            "GLANCE_AGENT_SKILL_VERSION": scheduler_skill_version,
            "GLANCE_AGENT_WORKSPACE_DIR": str(launcher_root / "scheduler-workspace"),
            "GLANCE_AGENT_APP_NAME": app_name,
            "GLANCE_AGENT_DEVICE_LABEL": device_label or platform.node(),
            "GLANCE_AGENT_CAPABILITIES": "[]",
        }
    )
    with redirect_stdout(io.StringIO()):
        create_launcher(scheduler_launcher, "glance-scheduler-agent", scheduler_destination)
    # A bootstrap plan can already contain live offers: freeze and return it, don't discard it.
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            scheduler_plan(scheduler_id=scheduler_id, contract_version=2)
    except typer.Exit:
        typer.echo(captured.getvalue(), nl=False)
        raise
    plan = json.loads(captured.getvalue())
    output(
        {
            "status": "READY",
            "scheduler_id": scheduler_id,
            "scheduler_launcher": str(scheduler_launcher.expanduser().resolve()),
            "scheduler_skill": str(scheduler_destination),
            "workers": installed_workers,
            "plan_status": plan.get("status"),
            "initial_plan": plan,
            "next_step": (
                "先按 initial_plan 的 v2 契约处理本次 offers；仅创建一个固定周期任务；"
                f"每次运行：{scheduler_launcher.expanduser().resolve()} "
                f"scheduler-plan --scheduler-id {scheduler_id} --contract-version 2"
            ),
        }
    )


@app.command("scheduler-plan")
def scheduler_plan(
    scheduler_id: Annotated[str, typer.Option("--scheduler-id")],
    contract_version: Annotated[int, typer.Option("--contract-version", min=1, max=2)] = 1,
    run_id: Annotated[str, typer.Option("--run-id")] = "",
) -> None:
    """Read one server-owned plan without claiming or changing any business task."""
    with _api_errors():
        if run_id and contract_version != 2:
            raise ValueError("Content runs require scheduler contract 2")
        config = settings()
        deadline = time.time() + 540  # Earlier than the server's ten-minute offer expiry.
        client = AgentClient(config)
        plan = (
            client.scheduler_plan(scheduler_id, contract_version=2, **({"run_id": run_id} if run_id else {}))
            if contract_version == 2
            else client.scheduler_plan(scheduler_id)
        )
        if not isinstance(plan, dict):
            output({"status": "HANDOFF_ERROR", "message": "invalid plan shape"})
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
        if run_id and (
            plan.get("run_id") != run_id
            or type(plan.get("run_generation")) is not int
            or plan["run_generation"] < 1
            or not isinstance(plan.get("content_id"), str)
        ):
            output({"status": "HANDOFF_ERROR", "message": "invalid content run plan", "worker_requests": []})
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
        if contract_version == 2 and (
            type(plan.get("contract_version")) is not int
            or plan.get("contract_version") != 2
            or not isinstance(plan.get("status"), str)
            or plan.get("status")
            not in {"READY", "NO_TASK", "DISABLED", "BLOCKED", "WAITING_UPSTREAM"}
            or (
                plan.get("status") == "WAITING_UPSTREAM"
                and (
                    type(plan.get("retry_after_sec")) is not int
                    or not 1 <= plan["retry_after_sec"] <= 30
                    or plan.get("worker_requests") != []
                )
            )
        ):
            output({"status": "HANDOFF_ERROR", "message": "invalid v2 plan", "worker_requests": []})
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
        if plan.get("status") == "READY":
            try:
                if config.role_id != "scheduler" or config.agent_id != scheduler_id:
                    raise ValueError("wrong scheduler launcher")
                roles = _handoff_roles()
                root = config.workspace_dir.resolve().parent
                workers = plan["worker_requests"]
                if not isinstance(workers, list) or not 1 <= len(workers) <= 16:
                    raise ValueError("invalid worker requests")
                if run_id and any(
                    worker.get("run_id") != run_id
                    or worker.get("run_generation") != plan["run_generation"]
                    or worker.get("content_id") != plan["content_id"]
                    for worker in workers
                ):
                    raise ValueError("worker outside content run")
                launchers = [
                    validate_worker(worker, scheduler_id, root, roles) for worker in workers
                ]
                if len({worker["request_id"] for worker in workers}) != len(workers):
                    raise ValueError("duplicate offer")
                for worker, launcher in zip(workers, launchers, strict=True):
                    materialize_offer(worker, scheduler_id, launcher, deadline)
            except (OSError, ValueError, TypeError, KeyError) as exc:
                output({"status": "HANDOFF_ERROR", "message": str(exc), "worker_requests": []})
                raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        output(plan)


def _handoff_roles() -> dict:
    return {
        role: load_json(bundled_role_skill_path(role)[1] / "role.json") for role in _ROLE_SKILLS
    }


@app.command("dispatch-offer")
def dispatch_offer(
    offer: Annotated[Path, typer.Option("--offer")],
    digest: Annotated[str, typer.Option("--digest")],
) -> None:
    """Consume one frozen local scheduler offer, without model-transcribed identifiers."""
    try:
        worker = consume_offer(offer, digest, settings(), _handoff_roles())
    except (OSError, ValueError, TypeError, KeyError) as exc:
        typer.echo(f"HANDOFF_ERROR: {exc}")
        raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
    captured = io.StringIO()
    try:
        with redirect_stdout(captured):
            dispatch(
                wait=0,
                launcher=worker["launcher"],
                task_type=worker["task_type"],
                task_id=worker["task_id"],
                agent_id=worker["agent_id"],
                model_name=worker["model"],
                reasoning_effort=worker["reasoning_effort"],
                run_id=worker.get("run_id", ""),
                run_generation=worker.get("run_generation"),
                request_id=worker["request_id"] if worker.get("run_id") else "",
            )
    except typer.Exit as exc:
        if exc.exit_code == DISPATCH_EXIT_NO_TASK:
            typer.echo("HANDOFF_ERROR: frozen offer no longer claimable; do not retry")
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        typer.echo(captured.getvalue(), nl=False)
        raise
    typer.echo(captured.getvalue(), nl=False)


@app.command("install-doctor-skill")
def install_doctor_skill(
    target: Annotated[Path, typer.Option("--target")],
    force: Annotated[bool, typer.Option("--force")] = False,
) -> None:
    """Install the local, read-only operations doctor Skill."""
    destination = target.expanduser().resolve() / "glance-ops-doctor"
    if destination.exists() and not force:
        output({"status": "EXISTS", "path": str(destination), "hint": "Use --force."})
        raise typer.Exit(2)
    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundled_doctor_skill_path(), destination)
    output({"status": "INSTALLED", "path": str(destination)})


@app.command("doctor-snapshot")
def doctor_snapshot(
    database: Annotated[Path, typer.Option("--database")],
    limit: Annotated[int, typer.Option("--limit", min=1, max=500)] = 50,
) -> None:
    """Read local structured incident logs without HTTP or database writes."""
    try:
        output(diagnostic_snapshot(database, limit=limit))
    except FileNotFoundError as exc:
        output({"status": "DATABASE_NOT_FOUND", "detail": str(exc)})
        raise typer.Exit(2) from None


@app.command()
def claim(
    wait: Annotated[int, typer.Option("--wait", min=0, max=840)] = 0,
) -> None:
    """Claim one compatible task and materialize its workspace."""
    with _api_errors():
        config = settings()
        client = AgentClient(config)
        response = client.claim(wait=wait)
        if response["status"] != "CLAIMED":
            output(response)
            return
        gc_workspaces(config.workspace_dir)
        bundle = client.bundle(response["bundle_url"], response["lease_token"])
        workspace = prepare_workspace(
            config.workspace_dir,
            bundle=bundle,
            lease_token=response["lease_token"],
            api_url=config.api_url,
            agent_id=config.agent_id,
        )
        _download_context(client, bundle, response["lease_token"], workspace)
        gaps = _verify_context(client, bundle, response["lease_token"], workspace)
    if gaps:
        # BUG-9：素材与清单不一致时拒绝进入工作，不得静默缺图开工。
        output(
            {
                "status": "CONTEXT_INCOMPLETE",
                "task_id": response["task_id"],
                "missing": gaps,
                "hint": "任务包素材与 input 清单不一致，请用 fail 上报环境故障后结束本轮",
            }
        )
        raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
    output(
        {
            "status": "CLAIMED",
            "task_id": response["task_id"],
            "task_type": response["task_type"],
            "workspace": str(workspace),
            "instructions": bundle.get("instructions", ""),
            "required_fields": sorted(bundle.get("result_schema", {}).get("required", [])),
        }
    )


@app.command()
def dispatch(
    wait: Annotated[int | None, typer.Option("--wait", min=0, max=840)] = None,
    launcher: Annotated[str, typer.Option("--launcher")] = "",
    task_type: Annotated[str, typer.Option("--task-type")] = "",
    agent_id: Annotated[str, typer.Option("--agent-id")] = "",
    model_name: Annotated[str, typer.Option("--model-name")] = "",
    reasoning_effort: Annotated[str, typer.Option("--reasoning-effort")] = "",
    task_id: Annotated[str, typer.Option("--task-id")] = "",
    run_id: Annotated[str, typer.Option("--run-id")] = "",
    run_generation: Annotated[int | None, typer.Option("--run-generation", min=1)] = None,
    request_id: Annotated[str, typer.Option("--request-id")] = "",
) -> None:
    """调度入口：stdout 直接输出给 Agent 的指令文本，退出码 0/10/11/20。

    --wait 缺省时由服务端按 agent_claim_wait_cap 决定长轮询（直接唤起式）；
    调度式（计时器跑，按退出码决定是否唤起）应传 --wait 0 秒级返回。
    """
    if (run_id or run_generation is not None or request_id) and not (
        run_id and run_generation is not None and request_id
    ):
        typer.echo("HANDOFF_ERROR: a complete frozen run offer is required")
        raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
    try:
        config = settings()
    except ValidationError:
        typer.echo(ENV_FAILURE_TEMPLATE.format(detail="启动器缺少 GLANCE_AGENT_* 配置"))
        raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
    except OSError as exc:
        typer.echo(ENV_FAILURE_TEMPLATE.format(detail=str(exc)))
        raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
    if agent_id:
        config.agent_id = agent_id
    if model_name:
        config.model_name = model_name
    if reasoning_effort:
        config.reasoning_effort = reasoning_effort
    with _dispatch_lock(config) as acquired:
        if not acquired:
            # 计时器周期短于等待时长时会叠出并发长轮询；本轮直接跳过不发请求。
            typer.echo(DISPATCH_BUSY_MESSAGE)
            raise typer.Exit(DISPATCH_EXIT_NO_TASK)
        client = AgentClient(config)
        response: dict[str, object] = {}
        try:
            response = client.dispatch(
                wait=wait, launcher=launcher, task_type=task_type, task_id=task_id,
                **({"run_id": run_id, "run_generation": run_generation, "request_id": request_id} if run_id else {}),
            )
            gaps: list[str] = []
            if response.get("should_run"):
                if task_id and response.get("task_id") != task_id:
                    raise ValueError("HANDOFF_ERROR: dispatch returned a different task")
                prompt = str(response.get("prompt") or "").strip()
                if not prompt:
                    # 先验证交接文本，再请求任务包，避免 malformed 响应继续扩大租约副作用。
                    _report_handoff_failure(
                        client,
                        config,
                        response,
                        error_code="HANDOFF_ERROR",
                        message="dispatch 返回 should_run=true 但缺少任务 prompt",
                    )
                    typer.echo(
                        ENV_FAILURE_TEMPLATE.format(
                            detail="dispatch 返回 should_run=true 但缺少任务 prompt"
                        )
                    )
                    raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
                bundle = client.bundle(response["bundle_url"], response["lease_token"])
                gc_workspaces(
                    config.workspace_dir,
                    keep_task_id=str(response.get("task_id") or ""),
                )
                workspace = prepare_workspace(
                    config.workspace_dir,
                    bundle=bundle,
                    lease_token=response["lease_token"],
                    api_url=config.api_url,
                    agent_id=config.agent_id,
                )
                _download_context(client, bundle, response["lease_token"], workspace)
                gaps = _verify_context(client, bundle, response["lease_token"], workspace)
        except httpx.HTTPStatusError as exc:
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="BUNDLE_HANDOFF_ERROR",
                message=f"任务包交接 HTTP {exc.response.status_code}: {exc.response.text[:300]}",
            )
            typer.echo(
                ENV_FAILURE_TEMPLATE.format(
                    detail=f"HTTP {exc.response.status_code}: {exc.response.text[:300]}"
                )
            )
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        except httpx.HTTPError as exc:
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="BUNDLE_HANDOFF_ERROR",
                message=f"任务包交接失败：{_sandbox_hint(str(exc), exc)}",
            )
            typer.echo(ENV_FAILURE_TEMPLATE.format(detail=_sandbox_hint(str(exc), exc)))
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        except FileNotFoundError as exc:
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="NO_WORKSPACE",
                message=str(exc),
            )
            typer.echo(ENV_FAILURE_TEMPLATE.format(detail=str(exc)))
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        except OSError as exc:
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="NO_WORKSPACE",
                message=str(exc),
            )
            typer.echo(ENV_FAILURE_TEMPLATE.format(detail=str(exc)))
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        except (KeyError, TypeError, ValueError) as exc:
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="HANDOFF_ERROR",
                message=f"任务包交接数据无效：{exc}",
            )
            typer.echo(ENV_FAILURE_TEMPLATE.format(detail=f"任务包交接数据无效：{exc}"))
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE) from None
        if not response.get("should_run"):
            reason = str(response.get("reason") or "")
            if reason not in {"NO_TASK", "AT_CAPACITY"}:
                typer.echo(
                    ENV_FAILURE_TEMPLATE.format(
                        detail=f"dispatch 返回未知状态：{reason or '缺少 reason'}"
                    )
                )
                raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
            if reason == "AT_CAPACITY":
                active_task_ids = [
                    str(task_id) for task_id in response.get("active_task_ids", []) if task_id
                ]
                typer.echo(str(response.get("message") or _capacity_message(active_task_ids)))
            else:
                typer.echo(str(response.get("message") or NO_TASK_MESSAGE))
            raise typer.Exit(
                DISPATCH_EXIT_AT_CAPACITY
                if response.get("reason") == "AT_CAPACITY"
                else DISPATCH_EXIT_NO_TASK
            )
        if gaps:
            detail = "任务包素材与 input 清单不一致：" + "；".join(gaps[:10])
            _report_handoff_failure(
                client,
                config,
                response,
                error_code="ENVIRONMENT_MISSING_CONTEXT",
                message=detail,
            )
            typer.echo(ENV_FAILURE_TEMPLATE.format(detail=detail))
            raise typer.Exit(DISPATCH_EXIT_ENV_FAILURE)
        typer.echo(prompt)
        raise typer.Exit(DISPATCH_EXIT_TASK)


@app.command("confirm-claim")
def confirm_claim(task_id: str) -> None:
    """把 dispatch 预约租约转为正式租约；Agent 会话启动后的第一步。"""
    config = settings()
    with _api_errors():
        directory, resolved_task_id = _task_workspace(config, task_id)
        lease = load_json(directory / ".lease.json")
        response = AgentClient(config).confirm_claim(
            resolved_task_id,
            str(lease["lease_token"]),
            str(lease["agent_id"]),
        )
    output(_task_response(response, task_id, resolved_task_id))


@app.command("register-publish-intent")
def register_publish_intent(task_id: str) -> None:
    """登记唯一发布点击授权；不执行平台点击或重试。"""
    config = settings()
    with _api_errors():
        directory, resolved_task_id = _task_workspace(config, task_id)
        lease = load_json(directory / ".lease.json")
        response = AgentClient(config).register_publish_intent(
            resolved_task_id,
            str(lease["lease_token"]),
            str(lease["agent_id"]),
        )
    output(_task_response(response, task_id, resolved_task_id))


app.command("publish-intent")(register_publish_intent)


@app.command()
def inspect(task_id: str) -> None:
    """Print task metadata and the required output path."""
    config = settings()
    directory, _resolved_task_id = _task_workspace(config, task_id)
    output(
        {
            "task": load_json(directory / "task.json"),
            "instructions": str(directory / "instructions.md"),
            "input": str(directory / "input.json"),
            "schema": str(directory / "result.schema.json"),
            "result": str(directory / "result.json"),
        }
    )


@app.command()
def validate(task_id: str) -> None:
    """Validate result.json against the task schema."""
    config = settings()
    directory, resolved_task_id = _task_workspace(config, task_id)
    _result, errors = validate_result(directory)
    if errors:
        output(
            _task_response(
                {"status": "INVALID", "task_id": resolved_task_id, "errors": errors},
                task_id,
                resolved_task_id,
            )
        )
        raise typer.Exit(2)
    output(
        _task_response({"status": "VALID", "task_id": resolved_task_id}, task_id, resolved_task_id)
    )


@app.command()
def submit(task_id: str) -> None:
    """Validate and submit result.json for a claimed task."""
    with _api_errors():
        config = settings()
        directory, resolved_task_id = _task_workspace(config, task_id)
        result, errors = validate_result(directory)
        if errors:
            output(
                _task_response(
                    {"status": "INVALID", "task_id": resolved_task_id, "errors": errors},
                    task_id,
                    resolved_task_id,
                )
            )
            raise typer.Exit(2)
        lease = load_json(directory / ".lease.json")
        response = AgentClient(config).complete(
            resolved_task_id,
            lease["lease_token"],
            lease["agent_id"],
            result,
        )
        remove_workspace(directory)
    output(_task_response(response, task_id, resolved_task_id))


@app.command()
def heartbeat(task_id: str) -> None:
    """Extend a claimed task lease."""
    config = settings()
    with _api_errors():
        directory, resolved_task_id = _task_workspace(config, task_id)
        lease = load_json(directory / ".lease.json")
        response = AgentClient(config).heartbeat(resolved_task_id, lease["lease_token"])
    output(_task_response(response, task_id, resolved_task_id))


@app.command("check-similar")
def check_similar(
    task_id: str,
    query: str,
    url: Annotated[str, typer.Option("--url")] = "",
    limit: Annotated[int, typer.Option("--limit", min=1, max=30)] = 10,
) -> None:
    """Query live candidate/content history while analyzing a leased task."""
    config = settings()
    directory, resolved_task_id = _task_workspace(config, task_id)
    lease = load_json(directory / ".lease.json")
    task_input = load_json(directory / "input.json")
    with _api_errors():
        response = AgentClient(config).similar(
            task_id=resolved_task_id,
            lease_token=str(lease["lease_token"]),
            agent_id=str(lease["agent_id"]),
            query=query,
            url=url,
            direction_key=str(task_input.get("direction_key") or ""),
            limit=limit,
        )
    output(_task_response(response, task_id, resolved_task_id))


@app.command("upload-artifact")
def upload_artifact(task_id: str, file: Path) -> None:
    """Upload one generated image or video through the leased task."""
    config = settings()
    directory, resolved_task_id = _task_workspace(config, task_id)
    lease = load_json(directory / ".lease.json")
    source = file.expanduser().resolve()
    if not source.is_file():
        output({"status": "FILE_NOT_FOUND", "path": str(source)})
        raise typer.Exit(2)
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    with _api_errors():
        response = AgentClient(config).upload_artifact(
            resolved_task_id,
            str(lease["lease_token"]),
            body=source.read_bytes(),
            mime_type=mime_type,
        )
    output(_task_response(response, task_id, resolved_task_id))


@app.command("prepare-image")
def prepare_image(
    task_id: str,
    file: Path,
    segment_index: Annotated[int, typer.Option("--segment-index", min=0)],
) -> None:
    """Prepare image dimensions from a frozen segment request, without uploading."""
    from glance_agent.image_preparation import prepare_image as prepare

    with _api_errors():
        config = settings()
        try:
            directory, resolved_task_id = _task_workspace(config, task_id)
            if config.role_id != "image":
                raise ValueError("prepare-image requires the image role")
            lease = load_json(directory / ".lease.json")
            agent_id = lease.get("agent_id") if isinstance(lease, dict) else None
            # Role launchers reload the base identity; dispatch stores the frozen fresh identity.
            if not isinstance(agent_id, str) or not re.fullmatch(
                re.escape(config.agent_id) + r"(?:-[0-9a-f]{12})?", agent_id
            ):
                raise ValueError("Image preparation requires this agent's local task lease")
            response = prepare(directory, resolved_task_id, agent_id, file, segment_index)
        except typer.Exit:
            raise
        except (ValueError, RuntimeError) as exc:
            output({"status": "PREPARATION_UNAVAILABLE" if isinstance(exc, RuntimeError) else "REJECTED", "detail": str(exc)})
            raise typer.Exit(2) from None
    output(_task_response(response, task_id, resolved_task_id))


@app.command()
def fail(
    task_id: str,
    code: Annotated[str, typer.Option("--code")],
    message: Annotated[str, typer.Option("--message")],
    retryable: Annotated[bool, typer.Option("--retryable/--terminal")] = False,
) -> None:
    """Report that the Agent cannot complete a claimed task."""
    config = settings()
    with _api_errors():
        directory, resolved_task_id = _task_workspace(config, task_id)
        lease = load_json(directory / ".lease.json")
        task_meta = load_json(directory / "task.json")
        response = AgentClient(config).fail(
            resolved_task_id,
            lease["lease_token"],
            lease["agent_id"],
            retryable=retryable,
            error_code=code,
            message=message,
            # 租约已失效时服务端按 agent + attempt 匹配允许迟到上报。
            attempt=task_meta.get("attempt"),
        )
        remove_workspace(directory)
    output(_task_response(response, task_id, resolved_task_id))


@app.command()
def result_path(task_id: str) -> None:
    """Print the only path where the Agent may write its result."""
    config = settings()
    directory, _resolved_task_id = _task_workspace(config, task_id)
    typer.echo(str(Path(directory) / "result.json"))
