"""Stage MPT launchers without changing host skills, automation or server state."""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

import httpx
from glance_agent import cli
from glance_agent.client import AgentClient
from glance_agent.settings import AgentSettings, PUBLISH_REQUIRED_CAPABILITIES
from glance_core.security import issue_role_token, issue_scheduler_token

WORKSPACE = Path(__file__).resolve().parents[1]
MPT_MODELS = {"gpt-6.1-sol", "gpt-6-luna"}
DEFAULT_API_URL = "http://127.0.0.1:8010"


def validated_api_url(value: str) -> str:
    # Validate before loading credentials; no DNS, redirects or alternate URL spellings.
    match = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})", value) if isinstance(value, str) else None
    if match is None or int(match[1]) > 65535:
        raise ValueError("API URL must be canonical HTTP 127.0.0.1 with a valid explicit port")
    return value


def controlled_stage(path: Path, workspace: Path = WORKSPACE) -> Path:
    path = path.absolute()
    root = workspace / "storage" / "agent-launcher-staging"
    if path != path.resolve() or not path.is_relative_to(root) or path == root:
        raise ValueError("Use a fresh directory inside storage/agent-launcher-staging")
    return path


def read_root_token(path: Path) -> str:
    # Never use source GLANCE credentials or embed credentials in command lines/launchers.
    data = json.loads(path.read_text(encoding="utf-8"))
    token = data.get("bootstrap_agent_token")
    if not isinstance(token, str) or not token or token == "change-me-before-network-use":
        raise ValueError("Independent MPT bootstrap credentials are required")
    return token


def launcher_environment(manifest: dict) -> dict[str, str]:
    api_url = validated_api_url(manifest.get("api_url", DEFAULT_API_URL))
    role = manifest["role_id"]
    root_token = read_root_token(Path(manifest["runtime_config"]))
    token = (
        issue_scheduler_token(root_token, manifest["scheduler_id"])
        if role == "scheduler"
        else issue_role_token(root_token, role)
    )
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("GLANCE_AGENT_") and key != "MPT_UPSTREAM_COMPATIBILITY"
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
    }
    environment.update({
        # A trusted bypass also prevents urllib/httpx falling back to Windows registry proxies.
        "NO_PROXY": "*",
        "GLANCE_AGENT_API_URL": api_url,
        "GLANCE_AGENT_TOKEN": token,
        "GLANCE_AGENT_ID": manifest["agent_id"],
        "GLANCE_AGENT_ROLE_ID": role,
        "GLANCE_AGENT_WORKSPACE_DIR": manifest["workspace_dir"],
        "GLANCE_AGENT_SKILL_NAME": manifest["skill_name"],
        "GLANCE_AGENT_SKILL_VERSION": manifest["skill_version"],
        "GLANCE_AGENT_APP_NAME": "Codex App",
        "GLANCE_AGENT_DEVICE_LABEL": platform.node(),
        "GLANCE_AGENT_CAPABILITIES": json.dumps(manifest["capabilities"], ensure_ascii=False),
        # The frozen server offer supplies the model/effort to dispatch-offer.
        "GLANCE_AGENT_MODEL_NAME": "",
        "GLANCE_AGENT_MODEL_TIER": "",
        "GLANCE_AGENT_REASONING_EFFORT": "",
    })
    return environment


def check_launcher(manifest_path: Path, manifest: dict):
    result = subprocess.run(
        ["C:/Program Files/PowerShell/7/pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
         str(manifest_path.with_suffix(".ps1")), "skill-version", "--skill-path", manifest["skill_path"]],
        cwd=WORKSPACE, capture_output=True, text=True, timeout=30, check=False,
    )
    if result.returncode or result.stdout.strip() != manifest["skill_version"]:
        raise ValueError("Staged launcher executable check failed")


def install(stage: Path, runtime_config: Path, scheduler_id: str, capabilities: list[str],
            api_url: str = DEFAULT_API_URL) -> dict:
    api_url = validated_api_url(api_url)
    stage = controlled_stage(stage)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", scheduler_id):
        raise ValueError("Invalid scheduler identity")
    runtime_config = runtime_config.resolve(strict=True)
    if not runtime_config.is_relative_to(WORKSPACE / "storage"):
        raise ValueError("Runtime credentials must belong to this MPT workspace storage")
    read_root_token(runtime_config)
    if stage.exists():
        raise ValueError("Staging directory already exists; never overwrite launchers or offers")
    stage.mkdir(parents=True)
    roles = [*cli._ROLE_SKILLS, "scheduler"]
    for role in roles:
        if role == "scheduler":
            name, source = "glance-scheduler-agent", cli.bundled_scheduler_skill_path()
        else:
            name, source = cli.bundled_role_skill_path(role)
        target = stage / "skills" / name
        shutil.copytree(source, target)
        if role != "scheduler":
            references = target / "references"
            references.mkdir(exist_ok=True)
            for shared in cli._bundled_shared_path().glob("*.md"):
                shutil.copy2(shared, references / shared.name)
        identity = scheduler_id if role == "scheduler" else f"{scheduler_id}-{role}"
        manifest = {
            "workspace": str(WORKSPACE), "runtime_config": str(runtime_config),
            "api_url": api_url,
            "scheduler_id": scheduler_id, "agent_id": identity, "role_id": role,
            "skill_name": name, "skill_version": cli._required_skill_version(target),
            "skill_path": str(target),
            "workspace_dir": str(stage / ("scheduler-workspace" if role == "scheduler" else "workspaces")),
            "capabilities": capabilities if role == "publish" else [],
        }
        manifest_path = stage / f"{identity}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        launcher = stage / f"{identity}.ps1"
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        launcher.write_text(
            "param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CommandArgs)\n"
            "$ErrorActionPreference = 'Stop'\n"
            f"& {quote(sys.executable)} {quote(Path(__file__).resolve())} run "
            f"--manifest {quote(manifest_path)} @CommandArgs\nexit $LASTEXITCODE\n",
            encoding="utf-8-sig",
        )
    # Real executable checks; no deployment report, claim or scheduler-plan at install time.
    for manifest_path in stage.glob("*.json"):
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        check_launcher(manifest_path, manifest)
    return {
        "status": "STAGED", "stage": str(stage), "launcher_count": len(roles),
        "api_url": api_url,
        "executable_selftest": "PASS", "deployment_verified": False,
        "scheduler_launcher": str(stage / f"{scheduler_id}.ps1"),
        "publish_capabilities": capabilities,
        "next_step": "Register selected roles with explicit host capacity/models in an authorized deployment window.",
    }


def register(stage: Path, roles: list[str], max_concurrency: int,
             supported_models: list[str], model_name: str, reasoning_effort: str,
             client_factory=None) -> dict:
    stage = controlled_stage(stage)
    if (type(max_concurrency) is not int or not 1 <= max_concurrency <= 16
            or not supported_models or len(set(supported_models)) != len(supported_models)
            or not set(supported_models).issubset(MPT_MODELS)
            or model_name not in supported_models
            or reasoning_effort not in {"medium", "high", "xhigh", "max"}
            or not roles or len(set(roles)) != len(roles)
            or not set(roles).issubset(cli._ROLE_SKILLS)):
        raise ValueError("Explicit role, host capacity and supported model/effort are required")
    manifests = []
    for role in roles:
        candidates = list(stage.glob(f"*-{role}.json"))
        if len(candidates) != 1:
            raise ValueError("Staged role manifest is missing or ambiguous")
        path = candidates[0]
        manifest = json.loads(path.read_text(encoding="utf-8"))
        validated_api_url(manifest.get("api_url", DEFAULT_API_URL))
        if manifest["workspace"] != str(WORKSPACE) or manifest["role_id"] != role:
            raise ValueError("Role manifest belongs to another MPT deployment")
        if role == "publish" and not set(PUBLISH_REQUIRED_CAPABILITIES).issubset(manifest["capabilities"]):
            raise ValueError("Publish capabilities must be explicitly verified and staged")
        manifests.append((path, manifest))
    reports = []
    for path, manifest in manifests:
        check_launcher(path, manifest)
        environment = launcher_environment(manifest)
        config = AgentSettings(
            _env_file=None, _env_prefix="MPT_STAGING_UNUSED_",
            api_url=environment["GLANCE_AGENT_API_URL"],
            token=environment["GLANCE_AGENT_TOKEN"], agent_id=manifest["agent_id"],
            role_id=manifest["role_id"], workspace_dir=manifest["workspace_dir"],
            skill_name=manifest["skill_name"], skill_version=manifest["skill_version"],
            app_name="Codex App", device_label=platform.node(),
            model_name=model_name, reasoning_effort=reasoning_effort,
            capabilities=manifest["capabilities"],
        )
        client = AgentClient(config, trust_env=False) if client_factory is None else client_factory(config)
        try:
            whoami = client.whoami()
            if whoami.get("role_id") != manifest["role_id"]:
                raise ValueError("Authenticated role does not match this launcher")
            response = client.deployment({
                "agent_id": manifest["agent_id"], "role_id": manifest["role_id"],
                "launcher_path": str(path.with_suffix(".ps1")),
                "skill": {"name": manifest["skill_name"], "path": manifest["skill_path"],
                          "content_hash": cli._tree_hash(Path(manifest["skill_path"]))},
                "host": {"app_name": "Codex App", "os": platform.system().lower(),
                         "device_label": config.device_label, "max_concurrency": max_concurrency,
                         "supported_models": supported_models},
                "model": {"name": model_name, "reasoning_effort": reasoning_effort},
                "self_check": {"cli": "pass", "connectivity": "pass",
                               "skill_installed": "pass", "launcher_executable": "pass"},
                "protocol_version": config.protocol_version, "request_selftest": False,
            })
        finally:
            client.client.close()
        # Keep only defined, non-secret report fields. Never report pending registration as verified.
        report = {"role_id": manifest["role_id"], "status": response.get("status"),
                  "findings": [{"code": item.get("code"), "level": item.get("level")}
                               for item in response.get("findings", [])]}
        reports.append(report)
        if report["status"] != "VERIFIED":
            return {"status": "REGISTRATION_INCOMPLETE", "roles": reports}
    return {"status": "REGISTERED", "roles": reports, "business_selftest": "NOT_REQUESTED"}


def run(manifest_path: Path, command: list[str]):
    stage = controlled_stage(manifest_path.parent)
    if manifest_path != manifest_path.resolve() or manifest_path.parent != stage:
        raise ValueError("Invalid launcher manifest path")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest["workspace"] != str(WORKSPACE):
        raise ValueError("Launcher belongs to another MPT workspace")
    if not command:
        raise ValueError("Specify an Agent CLI command")
    environment = launcher_environment(manifest)
    os.environ.clear()
    os.environ.update(environment)
    cli.app(args=command, prog_name="mpt-agent")


def main(argv=None, *, client_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    staging = actions.add_parser("install")
    staging.add_argument("--staging-directory", type=Path, required=True)
    staging.add_argument("--runtime-config", type=Path, required=True)
    staging.add_argument("--api-url", default=DEFAULT_API_URL)
    staging.add_argument("--scheduler-id", default="mpt-codex")
    staging.add_argument("--publish-capability", action="append", default=[])
    registering = actions.add_parser("register")
    registering.add_argument("--staging-directory", type=Path, required=True)
    registering.add_argument("--role", action="append", required=True)
    registering.add_argument("--max-concurrency", type=int, required=True)
    registering.add_argument("--supported-model", action="append", required=True)
    registering.add_argument("--model-name", required=True)
    registering.add_argument("--reasoning-effort", required=True)
    launching = actions.add_parser("run")
    launching.add_argument("--manifest", type=Path, required=True)
    launching.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            print(json.dumps(install(args.staging_directory, args.runtime_config,
                                     args.scheduler_id, args.publish_capability, args.api_url), ensure_ascii=False))
        elif args.action == "register":
            result = register(args.staging_directory, args.role, args.max_concurrency,
                              args.supported_model, args.model_name, args.reasoning_effort,
                              client_factory=client_factory)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["status"] == "REGISTERED" else 20
        else:
            run(args.manifest, args.command)
    except (OSError, ValueError, KeyError, httpx.HTTPError, subprocess.TimeoutExpired):
        # Credential parsing, HTTP payloads and filesystem errors may contain secrets.
        print("HANDOFF_ERROR: MPT launcher configuration or self-test failed", file=sys.stderr)
        return 20
    return 0


if __name__ == "__main__":
    sys.exit(main())
