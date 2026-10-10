param(
    [ValidateRange(1,65535)][int]$Port = 8000,
    [string]$BindAddress = '0.0.0.0',
    [Parameter(Mandatory)][string]$DataDir,
    [Parameter(Mandatory)][string]$BusinessSettingsFile,
    [Parameter(Mandatory)][string]$PublicBaseUrl,
    [ValidateRange(1,65535)][int]$WebuiPort = 8510,
    [string]$WebuiDataDir
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$storageRoot = Join-Path $workspace 'storage'
if (-not $WebuiDataDir) { $WebuiDataDir = "storage\webui-$WebuiPort" }
foreach ($name in @('DataDir', 'WebuiDataDir', 'BusinessSettingsFile')) {
    $value = Get-Variable -Name $name -ValueOnly
    if (-not [IO.Path]::IsPathRooted($value)) { $value = Join-Path $workspace $value }
    Set-Variable -Name $name -Value ([IO.Path]::GetFullPath($value))
}
if ($Port -eq $WebuiPort -or $DataDir -eq $WebuiDataDir) { throw 'API and WebUI require independent ports and data directories.' }
$watchDir = Join-Path $storageRoot "runtime-watchdog-$Port"
$lockFile = Join-Path $watchDir 'watchdog.lock'
$logFile = Join-Path $watchDir 'watchdog.log'
function Assert-WatchdogPaths {
    foreach ($path in @($DataDir, $WebuiDataDir, $BusinessSettingsFile, $watchDir, $lockFile, $logFile)) {
        if (-not $path.StartsWith($storageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Watchdog paths must stay inside this MPT workspace storage.'
        }
        $candidate = $path
        while ($candidate.Length -ge $storageRoot.Length) {
            $item = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Redirected watchdog paths are forbidden.' }
            $candidate = [IO.Path]::GetDirectoryName($candidate)
        }
    }
}
Assert-WatchdogPaths
New-Item -ItemType Directory -Path $watchDir -Force | Out-Null
Assert-WatchdogPaths
try { $lock = [IO.File]::Open($lockFile, 'OpenOrCreate', 'ReadWrite', 'None') }
catch [IO.IOException] {
    if (($_.Exception.HResult -band 0xffff) -notin @(32, 33)) { throw }
    Write-Output 'An MPT runtime check is already active.'
    return
}
try {
    $api = @{Port = $Port; BindAddress = $BindAddress; DataDir = $DataDir
        BusinessSettingsFile = $BusinessSettingsFile; PublicBaseUrl = $PublicBaseUrl}
    $probeAddress = if ($BindAddress -eq '0.0.0.0') { '127.0.0.1' } else { $BindAddress }
    $ui = @{Port = $WebuiPort; DataDir = $WebuiDataDir; ApiUrl = "http://${probeAddress}:$Port"
        AdminTokenFile = (Join-Path $DataDir 'private\runtime.json')}
    # Existing scripts only adopt healthy owned instances or recover exited ones.
    & (Join-Path $PSScriptRoot 'start-mpt-business.ps1') @api
    & (Join-Path $PSScriptRoot 'start-mpt-webui.ps1') @ui
    Assert-WatchdogPaths
    [IO.File]::AppendAllText($logFile, "$(Get-Date -Format o) API and WebUI ready.`n", [Text.UTF8Encoding]::new($false))
} catch {
    Assert-WatchdogPaths
    # Log no exception body: errors can contain caller-supplied configuration.
    [IO.File]::AppendAllText($logFile, "$(Get-Date -Format o) Runtime check failed; inspect owned instance state.`n", [Text.UTF8Encoding]::new($false))
    throw
} finally { $lock.Dispose() }
