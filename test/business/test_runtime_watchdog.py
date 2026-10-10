"""Bounded watchdog and review-only scheduled task contract."""

import json
import subprocess
import tempfile

from test.business.test_business_startup import PWSH, ROOT, free_port, invoke, isolated_directory, quote, ps


def run(script, *arguments):
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        result = subprocess.run(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File",
             str(ROOT / "scripts" / script), *map(str, arguments)],
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, timeout=120, check=False,
        )
        stdout.seek(0)
        stderr.seek(0)
        return subprocess.CompletedProcess(result.args, result.returncode,
                                           stdout.read().decode("utf-8"), stderr.read().decode("utf-8"))


def arguments(api, ui, api_port, ui_port, settings):
    return ["-Port", api_port, "-BindAddress", "127.0.0.1", "-DataDir", api,
            "-PublicBaseUrl", f"http://127.0.0.1:{api_port}", "-BusinessSettingsFile", settings,
            "-WebuiPort", ui_port, "-WebuiDataDir", ui]


def test_runtime_controllers_require_explicit_business_data_directory():
    for script in ("mpt-runtime-watchdog.ps1", "install-mpt-runtime-watchdog.ps1"):
        result = run(script, "-BusinessSettingsFile", "storage/nonexistent-settings.json",
                     "-PublicBaseUrl", "http://127.0.0.1:8000")
        assert result.returncode != 0
        assert "DataDir" in result.stderr


def test_watchdog_recovers_exited_pair_and_preserves_healthy_identity():
    api, ui = isolated_directory(), isolated_directory()
    api.mkdir(parents=True)
    settings = api / "business-settings.json"
    settings.write_text('{"handsfree_enabled":false}', encoding="utf-8")
    api_port, ui_port = free_port(), free_port()
    args = arguments(api, ui, api_port, ui_port, settings)
    try:
        first = run("mpt-runtime-watchdog.ps1", *args)
        assert first.returncode == 0, first.stdout + first.stderr
        states = [(api / "private/server.json").read_bytes(), (ui / "private/webui.json").read_bytes()]
        second = run("mpt-runtime-watchdog.ps1", *args)
        assert second.returncode == 0, second.stdout + second.stderr
        assert states == [(api / "private/server.json").read_bytes(), (ui / "private/webui.json").read_bytes()]
        for raw in reversed(states):
            state = json.loads(raw)
            ps(f"""Stop-Process -Id {state['worker']['id']}
                try {{ Stop-Process -Id {state['launcher']['id']} -ErrorAction Stop }}
                catch {{ if ($_.CategoryInfo.Category -ne 'ObjectNotFound') {{ throw }} }}
                if (Get-Process -Id {state['worker']['id']},{state['launcher']['id']} -ErrorAction SilentlyContinue) {{ throw 'Test process did not exit' }}
            """)
        recovered = run("mpt-runtime-watchdog.ps1", *args)
        assert recovered.returncode == 0, recovered.stdout + recovered.stderr
        for path, raw in zip((api / "private/server.json", ui / "private/webui.json"), states):
            assert json.loads(path.read_bytes())["worker"]["created_ticks"] != json.loads(raw)["worker"]["created_ticks"]
        plan = run("install-mpt-runtime-watchdog.ps1", *args)
        assert plan.returncode == 0, plan.stderr
        spec = json.loads(plan.stdout)
        assert spec["mode"] == "Plan" and spec["logon_type"] == "Interactive"
        assert spec["interval_minutes"] == 1 and spec["multiple_instances"] == "IgnoreNew"
        assert spec["workspace"] == str(ROOT)
        assert "admin_token" not in plan.stdout
    finally:
        from test.business.test_webui_startup import invoke as ui_invoke

        assert ui_invoke(ui_port, ui, api_port, api / "private/runtime.json", "-Stop").returncode == 0
        assert invoke(api_port, api, "-Stop").returncode == 0


def test_installer_rejects_non_mpt_name_and_shell_argument_injection():
    api, ui = isolated_directory(), isolated_directory()
    args = arguments(api, ui, free_port(), free_port(), ROOT / 'storage/settings";whoami;.json')
    rejected = run("install-mpt-runtime-watchdog.ps1", *args)
    assert rejected.returncode != 0
    rejected = run("install-mpt-runtime-watchdog.ps1", *args[:-2], "-TaskName", "Glance Service Watchdog")
    assert rejected.returncode != 0


def test_installer_real_task_definition_with_mocked_registration(tmp_path):
    api, ui = isolated_directory(), isolated_directory()
    api.mkdir(parents=True)
    settings = api / "settings.json"
    settings.write_text('{"handsfree_enabled":false}', encoding="utf-8")
    command = tmp_path / "scheduler-contract.ps1"
    command.write_text(
        """
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks
$global:mockTask = $null
$global:registered = 0
$global:removed = 0
function global:Get-ScheduledTask { param($TaskName,$TaskPath,$ErrorAction) return $global:mockTask }
function global:Export-ScheduledTask { param($TaskName,$TaskPath) return '<Task>owned rollback fixture</Task>' }
function global:Register-ScheduledTask { param($TaskName,$TaskPath,$InputObject,[switch]$Force)
    $global:mockTask=$InputObject; $global:registered++
}
function global:Unregister-ScheduledTask { param($TaskName,$TaskPath,[switch]$Confirm)
    $global:mockTask=$null; $global:removed++
}
function global:Get-ScheduledTaskInfo { param($TaskName,$TaskPath)
    return [pscustomobject]@{LastRunTime=(Get-Date);LastTaskResult=0;NextRunTime=(Get-Date).AddMinutes(1)}
}
"""
        + f"$scriptPath={quote(ROOT / 'scripts/install-mpt-runtime-watchdog.ps1')}\n"
        + f"$params=@{{Port=54321;BindAddress='127.0.0.1';DataDir={quote(api)};"
        + f"WebuiPort=54322;WebuiDataDir={quote(ui)};BusinessSettingsFile={quote(settings)};"
        + "PublicBaseUrl='http://127.0.0.1:54321'}\n"
        + """
& $scriptPath @params -Mode Install
& $scriptPath @params -Mode Verify
& $scriptPath @params -Mode Install
if ($global:registered -ne 2) { throw 'Expected only mock registrations' }
$global:mockTask.Description='another workspace'
foreach ($mode in @('Install','Uninstall','Verify')) {
    $rejected=$false
    try { & $scriptPath @params -Mode $mode } catch { $rejected=$_.Exception.Message -like '*not owned*' }
    if (-not $rejected) { throw 'Unowned task accepted' }
}
$global:mockTask.Description = "MPT runtime watchdog v1 | workspace=$((Resolve-Path (Join-Path (Split-Path $scriptPath) '..')).Path) | owner=$([Security.Principal.WindowsIdentity]::GetCurrent().User.Value)"
& $scriptPath @params -Mode Uninstall
if ($global:removed -ne 1 -or $null -ne $global:mockTask) { throw 'Mock uninstall failed' }
Write-Output 'scheduler-mock-contract-passed'
""", encoding="utf-8",
    )
    result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(command)],
                            capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "scheduler-mock-contract-passed" in result.stdout
    rollback = ROOT / "storage/runtime-watchdog-54321/task-rollback.xml"
    assert rollback.read_text(encoding="utf-8") == "<Task>owned rollback fixture</Task>"


def test_watchdog_exclusive_lock_skips_without_launching(tmp_path):
    api, ui = isolated_directory(), isolated_directory()
    api_port, ui_port = free_port(), free_port()
    watch_dir = ROOT / f"storage/runtime-watchdog-{api_port}"
    watch_dir.mkdir(parents=True)
    command = tmp_path / "lock-contract.ps1"
    command.write_text(
        f"$ErrorActionPreference='Stop'; $lock=[IO.File]::Open({quote(watch_dir / 'watchdog.lock')},'OpenOrCreate','ReadWrite','None');\n"
        + f"try {{ & {quote(ROOT / 'scripts/mpt-runtime-watchdog.ps1')} -Port {api_port} "
        + f"-WebuiPort {ui_port} -DataDir {quote(api)} -WebuiDataDir {quote(ui)} "
        + f"-BusinessSettingsFile {quote(api / 'settings.json')} -PublicBaseUrl 'http://127.0.0.1:{api_port}' }} finally {{ $lock.Dispose() }}",
        encoding="utf-8",
    )
    result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(command)],
                            capture_output=True, text=True, timeout=15, check=False)
    assert result.returncode == 0, result.stderr
    assert "already active" in result.stdout
    assert not api.exists() and not ui.exists()


def test_watchdog_and_installer_reject_redirected_storage(tmp_path):
    api, ui = isolated_directory(), isolated_directory()
    api.parent.mkdir(parents=True, exist_ok=True)
    ps(f"$null = New-Item -ItemType Junction -Path {quote(api)} -Value {quote(tmp_path)}")
    try:
        args = arguments(api, ui, free_port(), free_port(), api / "settings.json")
        for script in ("mpt-runtime-watchdog.ps1", "install-mpt-runtime-watchdog.ps1"):
            result = run(script, *args)
            assert result.returncode != 0 and "Redirected" in result.stderr
        assert not list(tmp_path.iterdir())
    finally:
        ps(f"Remove-Item -LiteralPath {quote(api)} -Force")
