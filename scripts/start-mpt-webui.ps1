param(
    [switch]$Stop,
    [ValidateRange(1,65535)][int]$Port = 8510,
    [string]$DataDir,
    [string]$ApiUrl = 'http://127.0.0.1:8000',
    [string]$AdminTokenFile = 'storage\business-8000\private\runtime.json'
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$storageRoot = Join-Path $workspace 'storage'
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not $DataDir) { $DataDir = "storage\webui-$Port" }
if (-not [IO.Path]::IsPathRooted($DataDir)) { $DataDir = Join-Path $workspace $DataDir }
if (-not [IO.Path]::IsPathRooted($AdminTokenFile)) { $AdminTokenFile = Join-Path $workspace $AdminTokenFile }
$dataDir = [IO.Path]::GetFullPath($DataDir)
$AdminTokenFile = [IO.Path]::GetFullPath($AdminTokenFile)
foreach ($path in @($dataDir, $AdminTokenFile)) {
    if (-not $path.StartsWith($storageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'WebUI data and credentials must stay inside this MPT workspace storage.'
    }
}
if ([IO.Path]::GetFileName($AdminTokenFile) -ne 'runtime.json' -or
    [IO.Path]::GetFileName([IO.Path]::GetDirectoryName($AdminTokenFile)) -ne 'private') {
    throw 'WebUI credentials must be the private API runtime.json file.'
}
$apiUri = [Uri]$ApiUrl
$localAddresses = @('127.0.0.1') + @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty IPAddress)
if ($apiUri.Scheme -ne 'http' -or $apiUri.Host -notin $localAddresses -or
    $apiUri.UserInfo -or $apiUri.AbsolutePath -ne '/' -or $apiUri.Query -or $apiUri.Fragment) {
    throw 'ApiUrl must be an HTTP URL on a local IPv4 address without credentials.'
}
$ApiUrl = $ApiUrl.TrimEnd('/')
$privateDir = Join-Path $dataDir 'private'
$stateFile = Join-Path $privateDir 'webui.json'
$stdoutFile = Join-Path $dataDir 'webui.stdout.log'
$stderrFile = Join-Path $dataDir 'webui.stderr.log'
$url = "http://127.0.0.1:$Port"
$arguments = '-m streamlit run "' + (Join-Path $workspace 'webui\Main.py') + '" --server.address 127.0.0.1 --server.port ' + $Port + ' --server.headless true --browser.gatherUsageStats false'

function Assert-ManagedPaths {
    foreach ($path in @($dataDir, $privateDir, $stateFile, $stdoutFile, $stderrFile, $AdminTokenFile)) {
        $candidate = $path
        while ($candidate.Length -ge $storageRoot.Length) {
            $item = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Redirected managed WebUI paths are forbidden.'
            }
            $candidate = [IO.Path]::GetDirectoryName($candidate)
        }
    }
}
Assert-ManagedPaths
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the locked production environment with uv first.' }
$resolvedCode = @'
import sys
from pathlib import Path
workspace = Path(sys.argv[1]).resolve(strict=True)
for raw in sys.argv[2:]:
    target = Path(raw)
    assert target == target.resolve()
    assert target.resolve().is_relative_to(workspace / "storage")
'@
& $python -c $resolvedCode $workspace $dataDir $AdminTokenFile 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Resolved WebUI paths must stay inside the real MPT workspace storage.' }
$basePython = (& $python -c 'import sys; print(sys._base_executable)').Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify this virtual environment runtime.' }

function Restrict-PrivateAccess {
    Assert-ManagedPaths
    $principals = @([Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'))
    foreach ($path in @($privateDir, $stateFile)) {
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if ($null -eq $item) { continue }
        $acl = Get-Acl -LiteralPath $path
        $acl.SetAccessRuleProtection($true, $false)
        foreach ($rule in @($acl.Access)) { $acl.RemoveAccessRuleAll($rule) }
        $inheritance = if ($item.PSIsContainer) { 'ContainerInherit,ObjectInherit' } else { 'None' }
        foreach ($principal in $principals) {
            $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                $principal, 'FullControl', $inheritance, 'None', 'Allow'))
        }
        Assert-ManagedPaths
        Set-Acl -LiteralPath $path -AclObject $acl
        $checked = Get-Acl -LiteralPath $path
        if (-not $checked.AreAccessRulesProtected -or @($checked.Access | Where-Object {
            $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $principals.Value -or
            $_.AccessControlType -ne 'Allow'
        }).Count) { throw 'Private WebUI ACL could not be restricted to the current user and SYSTEM.' }
    }
}

function Read-Token {
    Assert-ManagedPaths
    try {
        $allowed = @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'S-1-5-18')
        foreach ($path in @([IO.Path]::GetDirectoryName($AdminTokenFile), $AdminTokenFile)) {
            $acl = Get-Acl -LiteralPath $path
            if (-not $acl.AreAccessRulesProtected -or @($acl.Access | Where-Object {
                $_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value -notin $allowed -or
                $_.AccessControlType -ne 'Allow'
            }).Count) { throw 'Unprotected credentials' }
        }
        $settings = Get-Content -LiteralPath $AdminTokenFile -Raw | ConvertFrom-Json
        if ($settings.admin_token -isnot [string] -or -not $settings.admin_token) { throw 'Invalid credentials' }
        return $settings.admin_token
    } catch { throw 'Private API runtime credentials are unavailable or have an unsafe ACL.' }
}

function Get-Listeners { return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) }
function Get-Identity($Process) {
    return [pscustomobject]@{
        id = [int]$Process.ProcessId; parent_id = [int]$Process.ParentProcessId
        created_ticks = $Process.CreationDate.ToUniversalTime().Ticks
        executable = $Process.ExecutablePath; command_line = $Process.CommandLine
    }
}
function Assert-ProcessCommand($Process) {
    if ($Process.ExecutablePath -notin @($python, $basePython) -or
        $Process.CommandLine -notmatch ('\s' + [regex]::Escape($arguments) + '\s*$')) {
        throw 'Process executable, command or workspace does not match this MPT WebUI.'
    }
}
function Get-VerifiedProcess($Recorded) {
    if ($null -eq $Recorded) { return $null }
    if ($Recorded.id -isnot [long] -and $Recorded.id -isnot [int]) { throw 'Invalid saved process ID.' }
    if ($Recorded.id -le 0) { throw 'Invalid saved process ID.' }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($Recorded.id)"
    if ($null -eq $process -or $null -eq (Get-Process -Id $Recorded.id -ErrorAction SilentlyContinue)) { return $null }
    Assert-ProcessCommand $process
    $current = Get-Identity $process
    if ($current.created_ticks -ne $Recorded.created_ticks -or $current.executable -ne $Recorded.executable -or
        $current.command_line -cne $Recorded.command_line -or $current.parent_id -ne $Recorded.parent_id) {
        throw 'Saved WebUI process identity changed; refusing to stop or adopt it.'
    }
    return $process
}
function Assert-StateBoundary($State) {
    if ($State.workspace -ne $workspace -or $State.python -ne $python -or $State.base_python -ne $basePython -or
        $State.port -ne $Port -or $State.data_dir -ne $dataDir -or $State.api_url -ne $ApiUrl -or
        $State.token_file -ne $AdminTokenFile -or $null -eq $State.launcher) {
        throw 'Saved WebUI belongs to another workspace, environment or configuration.'
    }
    if ($null -ne $State.worker -and $State.worker.id -ne $State.launcher.id -and
        ($State.worker.parent_id -ne $State.launcher.id -or $State.worker.created_ticks -lt $State.launcher.created_ticks)) {
        throw 'Saved listener does not match its recorded launch chain.'
    }
}
function Save-State($State) {
    Assert-ManagedPaths
    [IO.File]::WriteAllText($stateFile, ($State | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
}
function Test-ApiReady {
    $token = Read-Token
    try {
        $response = Invoke-WebRequest "$ApiUrl/api/v1/contents" -TimeoutSec 2 -MaximumRedirection 0 -Headers @{Authorization = "Bearer $token"}
        $null = $response.Content | ConvertFrom-Json
        $anonymous = Invoke-WebRequest "$ApiUrl/api/v1/contents" -TimeoutSec 2 -MaximumRedirection 0 -SkipHttpErrorCheck
        return $response.StatusCode -eq 200 -and $anonymous.StatusCode -eq 401
    } catch { return $false }
}
function Test-WebuiReady($State) {
    if ($null -eq (Get-VerifiedProcess $State.worker)) { return $false }
    $listeners = @(Get-Listeners)
    if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or $listeners[0].OwningProcess -ne $State.worker.id) { return $false }
    try {
        $health = Invoke-WebRequest "$url/_stcore/health" -TimeoutSec 2 -MaximumRedirection 0
        return $health.StatusCode -eq 200 -and $health.Content.Trim() -eq 'ok'
    } catch { return $false }
}
function Stop-OwnedWebui($State) {
    Assert-ManagedPaths
    Assert-StateBoundary $State
    if (@(Get-Listeners | Where-Object { $_.OwningProcess -ne $State.worker.id -or $_.LocalAddress -ne '127.0.0.1' }).Count) {
        throw 'Selected port belongs to an unrecorded process; refusing to stop.'
    }
    $worker = Get-VerifiedProcess $State.worker
    $launcher = Get-VerifiedProcess $State.launcher
    if ($null -ne $worker) { Stop-Process -Id $State.worker.id }
    if ($null -ne $launcher -and $State.launcher.id -ne $State.worker.id) {
        if ($null -ne (Get-VerifiedProcess $State.launcher)) { Stop-Process -Id $State.launcher.id -ErrorAction SilentlyContinue }
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if (@(Get-Listeners).Count -eq 0 -and $null -eq (Get-VerifiedProcess $State.worker) -and
            $null -eq (Get-VerifiedProcess $State.launcher)) {
            Assert-ManagedPaths
            Remove-Item -LiteralPath $stateFile -ErrorAction SilentlyContinue
            return
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Stop did not release the WebUI port and recorded process chain.'
}

Restrict-PrivateAccess
if (Test-Path -LiteralPath $stateFile) {
    $saved = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    Assert-StateBoundary $saved
    $worker = Get-VerifiedProcess $saved.worker
    $launcher = Get-VerifiedProcess $saved.launcher
    if ($Stop) {
        Stop-OwnedWebui $saved
        Write-Output "Stopped MPT WebUI; port $Port and its recorded process chain are free."
        return
    }
    if ($null -ne $worker -or $null -ne $launcher) {
        if (-not (Test-WebuiReady $saved) -or -not (Test-ApiReady)) { throw 'Owned WebUI is running but WebUI/API readiness failed; existing process preserved.' }
        Write-Output "MPT WebUI ready: listener PID $($saved.worker.id), $url"
        return
    }
    if (@(Get-Listeners).Count) { throw 'WebUI port belongs to an unrecorded process.' }
    Assert-ManagedPaths
    Remove-Item -LiteralPath $stateFile
}
if (@(Get-Listeners).Count) { throw 'WebUI port belongs to another process; refusing to adopt or stop it.' }
if ($Stop) { Write-Output "MPT WebUI is not running; port $Port is free."; return }
if (-not (Test-ApiReady)) { throw 'API authenticated/anonymous readiness failed; WebUI was not started.' }
Assert-ManagedPaths
New-Item -ItemType Directory -Path $privateDir -Force | Out-Null
Restrict-PrivateAccess
$originalEnvironment = @{}
$state = $null
try {
    foreach ($entry in @(Get-ChildItem Env:MPT_BUSINESS_*)) {
        $originalEnvironment[$entry.Name] = $entry.Value
        [Environment]::SetEnvironmentVariable($entry.Name, $null, 'Process')
    }
    $originalEnvironment['MPT_UPSTREAM_COMPATIBILITY'] = [Environment]::GetEnvironmentVariable('MPT_UPSTREAM_COMPATIBILITY', 'Process')
    [Environment]::SetEnvironmentVariable('MPT_UPSTREAM_COMPATIBILITY', $null, 'Process')
    foreach ($entry in @{MPT_BUSINESS_API_URL = $ApiUrl; MPT_BUSINESS_ADMIN_TOKEN_FILE = $AdminTokenFile}.GetEnumerator()) {
        if (-not $originalEnvironment.ContainsKey($entry.Key)) { $originalEnvironment[$entry.Key] = $null }
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
    }
    Assert-ManagedPaths
    $launched = Start-Process -FilePath $python -WorkingDirectory $workspace -WindowStyle Hidden -PassThru `
        -ArgumentList $arguments -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    $launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $($launched.Id)"
    if ($null -eq $launcher) { throw 'Python launcher exited before identity recording.' }
    Assert-ProcessCommand $launcher
    $state = [pscustomobject]@{
        workspace = $workspace; python = $python; base_python = $basePython
        port = $Port; data_dir = $dataDir; api_url = $ApiUrl; token_file = $AdminTokenFile
        launcher = Get-Identity $launcher; worker = $null
    }
    Save-State $state
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        if ($null -eq (Get-VerifiedProcess $state.launcher)) { throw 'Python launcher exited during WebUI startup.' }
        foreach ($process in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($state.launcher.id)")) {
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
        if (Test-WebuiReady $state) {
            Write-Output "MPT WebUI ready: listener PID $($state.worker.id), launcher PID $($state.launcher.id), $url"
            return
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'WebUI readiness did not pass within 30 seconds; recorded live process preserved for inspection.'
} finally {
    foreach ($name in $originalEnvironment.Keys) { [Environment]::SetEnvironmentVariable($name, $originalEnvironment[$name], 'Process') }
}
