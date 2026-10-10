param(
    [switch]$Stop,
    [ValidateRange(1,65535)][int]$Port = 8010,
    [Alias('Host')][string]$BindAddress = '127.0.0.1',
    [string]$DataDir,
    [string]$BusinessSettingsFile,
    [string]$PublicBaseUrl
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $workspace '.venv\Scripts\python.exe'
$bindIp = [Net.IPAddress]::Parse($BindAddress)
if ($bindIp.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $bindIp.ToString() -ne $BindAddress) {
    throw 'Host must be an explicit IPv4 address.'
}
if (-not $DataDir) { $DataDir = "storage\business-$Port" }
if (-not [IO.Path]::IsPathRooted($DataDir)) { $DataDir = Join-Path $workspace $DataDir }
$dataDir = [IO.Path]::GetFullPath($DataDir)
$storageRoot = [IO.Path]::GetFullPath((Join-Path $workspace 'storage'))
if (-not $dataDir.StartsWith($storageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'DataDir must be an independent directory inside this MPT workspace storage.'
}
$ancestor = $dataDir
while ($ancestor.Length -ge $storageRoot.Length) {
    if ((Test-Path -LiteralPath $ancestor) -and
        ((Get-Item -LiteralPath $ancestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'Redirected storage directories are not permitted.'
    }
    $ancestor = [IO.Path]::GetDirectoryName($ancestor)
}
$privateDir = Join-Path $dataDir 'private'
$stateFile = Join-Path $privateDir 'server.json'
$settingsFile = Join-Path $privateDir 'runtime.json'
$proofFile = Join-Path $privateDir 'runtime-public.json'
$databaseDir = Join-Path $dataDir 'database'
$stdoutFile = Join-Path $dataDir 'server.stdout.log'
$stderrFile = Join-Path $dataDir 'server.stderr.log'

function Assert-ManagedPaths {
    # Check every startup-owned directory/file, including dangling links and
    # SQLite sidecars, before reads, ACL changes, writes or process operations.
    $paths = @($dataDir, $privateDir, $stateFile, $settingsFile, $proofFile,
        $databaseDir, $stdoutFile, $stderrFile)
    foreach ($database in @('app.db', 'huey.db')) {
        foreach ($suffix in @('', '-wal', '-shm', '-journal')) {
            $paths += Join-Path $databaseDir ($database + $suffix)
        }
    }
    foreach ($path in $paths) {
        $candidate = $path
        while ($candidate.Length -ge $storageRoot.Length) {
            $item = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Redirected managed startup paths are forbidden.'
            }
            $candidate = [IO.Path]::GetDirectoryName($candidate)
        }
    }
}
Assert-ManagedPaths

function Restrict-PrivateAccess {
    Assert-ManagedPaths
    $principals = @([Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))
    foreach ($path in @($privateDir, $settingsFile)) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { continue }
        $acl = Get-Acl -LiteralPath $path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleAll($rule) }
        $inheritance = if ($item.PSIsContainer) { 'ContainerInherit,ObjectInherit' } else { 'None' }
        foreach ($principal in $principals) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $principal, 'FullControl', $inheritance, 'None', 'Allow'
            ))
        }
        Assert-ManagedPaths
        Set-Acl -LiteralPath $path -AclObject $acl
        $checked = Get-Acl -LiteralPath $path
        if (-not $checked.AreAccessRulesProtected -or @($checked.Access | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $principals.Value -or
            $_.AccessControlType -ne 'Allow'
        }).Count) { throw 'Private runtime ACL could not be restricted to the current user and SYSTEM.' }
    }
}
Restrict-PrivateAccess
$probeAddress = if ($BindAddress -eq '0.0.0.0') { '127.0.0.1' } else { $BindAddress }
$url = "http://${probeAddress}:$Port"
if (-not $PublicBaseUrl) { $PublicBaseUrl = $url }
$publicUri = [Uri]$PublicBaseUrl
$localAddresses = @('127.0.0.1') + @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress)
if ($publicUri.Scheme -ne 'http' -or $publicUri.Port -ne $Port -or $publicUri.Host -notin $localAddresses -or
    $publicUri.UserInfo -or $publicUri.AbsolutePath -ne '/' -or $publicUri.Query -or $publicUri.Fragment -or
    ($BindAddress -ne '0.0.0.0' -and $publicUri.Host -ne $BindAddress)) {
    throw 'PublicBaseUrl must be an HTTP URL for the selected port on a bound local IPv4 address.'
}
$PublicBaseUrl = $PublicBaseUrl.TrimEnd('/')
$arguments = '-m uvicorn app.asgi:app --host ' + $BindAddress + ' --port ' + $Port + ' --app-dir "' + $workspace + '"'
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the locked production environment with uv first.' }
$resolvedCode = @'
import sys
from pathlib import Path
workspace = Path(sys.argv[1]).resolve(strict=True)
storage = workspace / "storage"
target = Path(sys.argv[2])
assert target == target.resolve()
assert target.resolve().is_relative_to(storage) and target.resolve() != storage
'@
& $python -c $resolvedCode $workspace $dataDir 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Resolved DataDir must stay inside the real MPT workspace storage.' }
$basePython = (& $python -c 'import sys; print(sys._base_executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify this virtual environment runtime.' }

$businessOptions = [ordered]@{
    asr_model = 'medium'; asr_device = 'cpu'; asr_compute_type = 'int8'
    tts_provider = 'edge'; tts_voice = 'zh-CN-YunxiNeural'; tts_rate = '+20%'; tts_volume = '+0%'
    handsfree_enabled = $false; handsfree_auto_topic_enabled = $true
    handsfree_auto_review_enabled = $true; handsfree_auto_publish_douyin_enabled = $true
    handsfree_min_candidate_score = 70; handsfree_daily_publish_limit = 1
    handsfree_publish_interval_minutes = 120
    bgm_enabled = $true; bgm_track_id = ''; bgm_volume = 0.15
}
if (-not $Stop -and $BusinessSettingsFile) {
    if (-not [IO.Path]::IsPathRooted($BusinessSettingsFile)) { $BusinessSettingsFile = Join-Path $workspace $BusinessSettingsFile }
    $BusinessSettingsFile = [IO.Path]::GetFullPath($BusinessSettingsFile)
    if (-not $BusinessSettingsFile.StartsWith($workspace + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Business settings must belong to this MPT workspace.'
    }
    $settingsAncestor = $BusinessSettingsFile
    while ($settingsAncestor.Length -gt $workspace.Length) {
        if ((Test-Path -LiteralPath $settingsAncestor) -and
            ((Get-Item -LiteralPath $settingsAncestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'Redirected business settings are forbidden.'
        }
        $settingsAncestor = [IO.Path]::GetDirectoryName($settingsAncestor)
    }
    try { $options = Get-Content -LiteralPath $BusinessSettingsFile -Raw | ConvertFrom-Json -AsHashtable }
    catch { throw 'Business settings JSON could not be read.' }
    if ($options -isnot [System.Collections.IDictionary]) { throw 'Business settings must be a JSON object.' }
    $unknown = @($options.Keys | Where-Object { -not $businessOptions.Contains($_) })
    if ($unknown.Count) {
        $labels = @($unknown | ForEach-Object { [regex]::Replace([string]$_, '[^A-Za-z0-9_]', '?').Substring(0, [Math]::Min(([string]$_).Length, 80)) })
        throw ('Unknown or private business settings fields are forbidden: ' + ($labels -join ', '))
    }
    foreach ($key in $options.Keys) {
        $businessOptions[$key] = $options[$key]
    }
}
$optionsJson = $businessOptions | ConvertTo-Json -Compress
if (-not $Stop) {
    $validationCode = @'
import json, sys
from typing import Annotated
from pydantic import TypeAdapter, ValidationError
from glance_core.settings import Settings
options = json.load(sys.stdin)
failures = []
if options["tts_provider"] != "edge":
    failures.append("tts_provider:approved_provider_required")
for key, value in options.items():
    field = Settings.model_fields[key]
    # BaseSettings constructor parses environment values; validate the raw JSON
    # field independently so strings/bools cannot be coerced before strict checking.
    try:
        TypeAdapter(Annotated[field.annotation, field]).validate_python(value, strict=True)
    except ValidationError as error:
        failures.extend(f"{key}:{item['type']}" for item in error.errors(include_input=False, include_context=False, include_url=False))
if not failures:
    import edge_tts
    for key, argument in (("tts_voice", "voice"), ("tts_rate", "rate"), ("tts_volume", "volume")):
        try:
            edge_tts.Communicate("startup validation", **{argument: options[key]})
        except (ValueError, TypeError):
            failures.append(f"{key}:invalid_edge_parameter")
    from pathlib import Path
    from faster_whisper.utils import available_models
    import ctranslate2
    model = options["asr_model"]
    local = Path(model)
    if model and model not in available_models() and not (
        local.is_dir() and (local / "model.bin").is_file() and (local / "config.json").is_file()
    ):
        failures.append("asr_model:unsupported_model_or_local_directory")
    device = options["asr_device"]
    if device not in {"cpu", "cuda", "auto"}:
        failures.append("asr_device:unsupported_device")
    else:
        try:
            effective_device = "cuda" if device == "auto" and ctranslate2.get_cuda_device_count() else device
            if effective_device == "auto":
                effective_device = "cpu"
            supported = ctranslate2.get_supported_compute_types(effective_device)
            if options["asr_compute_type"] not in supported | {"auto", "default"}:
                failures.append("asr_compute_type:unsupported_compute_type")
        except (RuntimeError, ValueError):
            failures.append("asr_device:unavailable_device")
if failures:
    print("Invalid business settings fields: " + ", ".join(failures))
    sys.exit(2)
'@
    $diagnostics = @($optionsJson | & $python -c $validationCode 2>$null)
    if ($LASTEXITCODE -ne 0) {
        if ($diagnostics.Count) { throw ($diagnostics -join ' ') }
        throw 'Business settings fail strict type/range or approved TTS validation.'
    }
}

function Read-PrivateSettings {
    Restrict-PrivateAccess
    try { $value = Get-Content -LiteralPath $settingsFile -Raw | ConvertFrom-Json -AsHashtable }
    catch { throw 'Private runtime credentials could not be read.' }
    if ($value -isnot [System.Collections.IDictionary] -or $value.Count -ne 2 -or
        $value.Keys.Where({ $_ -notin @('admin_token', 'bootstrap_agent_token') }).Count -or
        $value.admin_token -isnot [string] -or $value.bootstrap_agent_token -isnot [string] -or
        -not $value.admin_token -or -not $value.bootstrap_agent_token) {
        throw 'Private runtime credentials must contain only independent admin and agent tokens.'
    }
    return $value
}

function Get-Listeners {
    return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-Identity($Process) {
    return [pscustomobject]@{
        id = [int]$Process.ProcessId
        parent_id = [int]$Process.ParentProcessId
        created_ticks = $Process.CreationDate.ToUniversalTime().Ticks
        executable = $Process.ExecutablePath
        command_line = $Process.CommandLine
    }
}

function Assert-ProcessCommand($Process) {
    if ($Process.ExecutablePath -notin @($python, $basePython) -or
        $Process.CommandLine -notmatch ('\s' + [regex]::Escape($arguments) + '\s*$')) {
        throw 'Process executable, command or workspace does not match this MPT server.'
    }
}

function Get-VerifiedProcess($Recorded) {
    if ($null -eq $Recorded) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($Recorded.id)"
    if ($null -eq $process -or $null -eq (Get-Process -Id $Recorded.id -ErrorAction SilentlyContinue)) { return $null }
    Assert-ProcessCommand $process
    $current = Get-Identity $process
    # PID reuse cannot grant stop authority: freeze creation time, command and parent.
    if ($current.created_ticks -ne $Recorded.created_ticks -or
        $current.executable -ne $Recorded.executable -or
        $current.command_line -ne $Recorded.command_line -or
        $current.parent_id -ne $Recorded.parent_id) {
        throw 'Saved server process identity changed; refusing to stop or adopt it.'
    }
    return $process
}

function Save-State($State) {
    Assert-ManagedPaths
    [IO.File]::WriteAllText($stateFile, ($State | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}

function Assert-StateBoundary($State) {
    if ($State.workspace -ne $workspace -or $State.python -ne $python -or $State.base_python -ne $basePython) {
        throw 'Saved server belongs to another workspace or environment.'
    }
    if ($null -eq $State.port) {
        # Previously verified 8010 records remain usable only at their original boundary.
        if ($Port -ne 8010 -or $BindAddress -ne '127.0.0.1' -or
            $dataDir -ne (Join-Path $workspace 'storage\business-8010') -or $BusinessSettingsFile) {
            throw 'Legacy server state is restricted to the original isolated 8010 deployment.'
        }
    } elseif ($State.port -ne $Port -or $State.bind_address -ne $BindAddress -or $State.data_dir -ne $dataDir) {
        throw 'Saved server port, bind address or data directory does not match this request.'
    }
}

function Test-BusinessReady($State, $Settings) {
    if ($null -eq (Get-VerifiedProcess $State.worker)) { return $false }
    $listeners = @(Get-Listeners)
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne $BindAddress -or
        $listeners[0].OwningProcess -ne $State.worker.id) { return $false }
    try {
        $response = Invoke-WebRequest "$url/api/v1/contents" -TimeoutSec 2 -Headers @{
            Authorization = "Bearer $($Settings.admin_token)"
        }
        if ($response.StatusCode -ne 200) { return $false }
        $null = $response.Content | ConvertFrom-Json
        $anonymous = Invoke-WebRequest "$url/api/v1/contents" -TimeoutSec 2 -SkipHttpErrorCheck
        return $anonymous.StatusCode -eq 401
    } catch { return $false }
}

function Stop-OwnedServer($State) {
    Assert-ManagedPaths
    Assert-StateBoundary $State
    if (@(Get-Listeners | Where-Object { $_.OwningProcess -ne $State.worker.id -or $_.LocalAddress -ne $BindAddress }).Count) {
        throw 'Selected port belongs to an unrecorded process; refusing to stop.'
    }
    if ($null -ne $State.worker -and $State.worker.id -ne $State.launcher.id -and
        ($State.worker.parent_id -ne $State.launcher.id -or $State.worker.created_ticks -lt $State.launcher.created_ticks)) {
        throw 'Saved listener no longer belongs to the recorded launch chain.'
    }
    # The venv launcher starts a base-Python child; stopping only the launcher orphans it.
    $worker = Get-VerifiedProcess $State.worker
    $launcher = Get-VerifiedProcess $State.launcher
    if ($null -ne $worker) { Stop-Process -Id $State.worker.id }
    if ($null -ne $launcher -and $State.launcher.id -ne $State.worker.id) {
        $launcher = Get-VerifiedProcess $State.launcher
        if ($null -ne $launcher) { Stop-Process -Id $State.launcher.id -ErrorAction SilentlyContinue }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if (@(Get-Listeners).Count -eq 0 -and
            $null -eq (Get-VerifiedProcess $State.worker) -and
            $null -eq (Get-VerifiedProcess $State.launcher)) {
            Assert-ManagedPaths
            Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
            return
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Stop did not release port $Port and both recorded processes."
}

Assert-ManagedPaths
Restrict-PrivateAccess
if (Test-Path -LiteralPath $stateFile) {
    $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    Assert-StateBoundary $saved
    if ($null -ne $saved.worker -and $saved.worker.id -ne $saved.launcher.id -and
        ($saved.worker.parent_id -ne $saved.launcher.id -or $saved.worker.created_ticks -lt $saved.launcher.created_ticks)) {
        throw 'Saved listener does not match its launch chain.'
    }
    $worker = Get-VerifiedProcess $saved.worker
    $launcher = Get-VerifiedProcess $saved.launcher
    if ($Stop) {
        Stop-OwnedServer $saved
        Write-Output "Stopped MPT business server; port $Port and its recorded process chain are free."
        return
    }
    if ($null -ne $worker -or $null -ne $launcher) {
        if ($null -ne $saved.port -and ($saved.options_json -ne $optionsJson -or $saved.public_base_url -ne $PublicBaseUrl)) {
            throw 'Running server settings differ; stop the recorded instance before changing configuration.'
        }
        $settings = Read-PrivateSettings
        if (-not (Test-BusinessReady $saved $settings)) { throw 'Owned server is running but business readiness failed.' }
        Write-Output "MPT business server ready: listener PID $($saved.worker.id), $url"
        return
    }
    if (@(Get-Listeners).Count) { throw "Port $Port belongs to an unrecorded process." }
    Assert-ManagedPaths
    Remove-Item -LiteralPath $stateFile
}
if ($Stop) {
    if (@(Get-Listeners).Count) { throw "Port $Port belongs to an unrecorded process; refusing to stop it." }
    Write-Output "MPT business server is not running; port $Port is free."
    return
}
if (@(Get-Listeners).Count) { throw "Port $Port is already owned by another process." }

# Check the actual install; provider extras belong only in the upstream test env.
& $python -c 'from importlib.metadata import distributions; banned={"openai","litellm","dashscope","google-genai","twelvelabs"}; assert not banned.intersection(d.metadata["Name"].lower() for d in distributions()), "Model SDKs must stay in the isolated upstream environment"'
if ($LASTEXITCODE -ne 0) { throw 'Production dependency check failed.' }
Assert-ManagedPaths
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
Restrict-PrivateAccess
if (-not (Test-Path -LiteralPath $settingsFile)) {
    Assert-ManagedPaths
    $settings = @{
        admin_token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
        bootstrap_agent_token = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    }
    [IO.File]::WriteAllText($settingsFile, ($settings | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
}
$settings = Read-PrivateSettings
$environment = @{
    MPT_BUSINESS_ENABLED = '1'
    MPT_BUSINESS_DATA_DIR = $dataDir
    MPT_BUSINESS_DATABASE_URL = "sqlite:///$($dataDir.Replace('\', '/'))/database/app.db"
    MPT_BUSINESS_PUBLIC_BASE_URL = $PublicBaseUrl
    MPT_BUSINESS_HOST = $BindAddress
    MPT_BUSINESS_PORT = [string]$Port
    MPT_BUSINESS_ADMIN_TOKEN = $settings.admin_token
    MPT_BUSINESS_BOOTSTRAP_AGENT_TOKEN = $settings.bootstrap_agent_token
    MPT_STARTUP_OPTIONS = $optionsJson
    MPT_UPSTREAM_COMPATIBILITY = $null
}
$originalEnvironment = @{}
$state = $null
try {
    # Inherited MPT business variables cannot silently enable Handsfree or redirect data.
    foreach ($entry in @(Get-ChildItem Env:MPT_BUSINESS_*)) {
        $originalEnvironment[$entry.Name] = $entry.Value
        [Environment]::SetEnvironmentVariable($entry.Name, $null, 'Process')
    }
    foreach ($key in $businessOptions.Keys) {
        $value = $businessOptions[$key]
        $environment['MPT_BUSINESS_' + $key.ToUpperInvariant()] = if ($value -is [bool]) { $value.ToString().ToLowerInvariant() } else { [string]$value }
    }
    foreach ($name in $environment.Keys) {
        if (-not $originalEnvironment.ContainsKey($name)) {
            $originalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
        }
        [Environment]::SetEnvironmentVariable($name, $environment[$name], 'Process')
    }
    $proofCode = @'
import json
import os
import sys
from pathlib import Path
import app.asgi
import glance_agent
import glance_collector
import glance_core
import glance_media
from app.config import config
from app.business.runtime import settings_from_config
from app.utils.utils import public_dir
settings = settings_from_config(config._cfg, config.root_dir)
root = Path(config.root_dir).resolve()
modules = {"app": app.asgi.__file__, "agent": glance_agent.__file__, "collector": glance_collector.__file__, "core": glance_core.__file__, "media": glance_media.__file__}
assert all(Path(path).resolve().is_relative_to(root) for path in modules.values())
assert settings.data_dir.resolve() == Path(os.environ["MPT_BUSINESS_DATA_DIR"]).resolve()
assert settings.database_url == os.environ["MPT_BUSINESS_DATABASE_URL"]
assert (settings.host, settings.port) == (os.environ["MPT_BUSINESS_HOST"], int(os.environ["MPT_BUSINESS_PORT"]))
assert settings.public_base_url == os.environ["MPT_BUSINESS_PUBLIC_BASE_URL"]
expected = json.loads(os.environ["MPT_STARTUP_OPTIONS"])
assert all(getattr(settings, key) == value for key, value in expected.items())
assert not any(name.startswith(("glance_server", "glance_worker", "openai", "litellm", "dashscope", "google.genai")) for name in sys.modules)
print(json.dumps({"modules": modules, "config_file": config.config_file, "static_public_dir": public_dir(), "webui_source": str(root / "webui/Main.py"), "database_url": settings.database_url, "data_dir": str(settings.data_dir), "public_base_url": settings.public_base_url, "host": settings.host, "port": settings.port, "asr_model": settings.asr_model, "asr_device": settings.asr_device, "asr_compute_type": settings.asr_compute_type, "business_settings": {key: getattr(settings, key) for key in expected}}))
'@
    $proof = @(& $python -c $proofCode)
    if ($LASTEXITCODE -ne 0) { throw 'Independent runtime settings or module provenance check failed.' }
    $publicProof = $proof[-1] | ConvertFrom-Json
    Assert-ManagedPaths
    [IO.File]::WriteAllText($proofFile, ($publicProof | ConvertTo-Json -Depth 4), [Text.UTF8Encoding]::new($false))
    Assert-ManagedPaths
    $launched = Start-Process -FilePath $python -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
        -ArgumentList $arguments -RedirectStandardOutput $stdoutFile `
        -RedirectStandardError $stderrFile
    $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($launched.Id)"
    if ($null -eq $launcher) { throw 'Python launcher exited before its identity could be recorded.' }
    Assert-ProcessCommand $launcher
    $state = [pscustomobject]@{
        workspace = $workspace; python = $python; base_python = $basePython
        port = $Port; bind_address = $BindAddress; data_dir = $dataDir
        public_base_url = $PublicBaseUrl; options_json = $optionsJson
        launcher = Get-Identity $launcher; worker = $null
    }
    Save-State $state
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if ($null -eq (Get-VerifiedProcess $state.launcher)) { throw 'Python launcher exited during startup.' }
        foreach ($process in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($state.launcher.id)")) {
            # Windows may attach conhost; only the verified Python child is ours.
            if ($process.ExecutablePath -ne $basePython) { continue }
            Assert-ProcessCommand $process
            if ($process.CreationDate.ToUniversalTime().Ticks -lt $state.launcher.created_ticks) { throw 'Child predates its launcher.' }
            $state.worker = Get-Identity $process
            Save-State $state
        }
        if ($null -eq $state.worker -and @(Get-Listeners | Where-Object OwningProcess -eq $state.launcher.id).Count) {
            $state.worker = $state.launcher
            Save-State $state
        }
        if (Test-BusinessReady $state $settings) {
            Write-Output "MPT business server ready: listener PID $($state.worker.id), launcher PID $($state.launcher.id), $url"
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Business readiness did not pass within 30 seconds; inspect private server logs.'
} catch {
    if ($null -ne $state) { Stop-OwnedServer $state }
    throw
} finally {
    foreach ($name in $originalEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process')
    }
}
