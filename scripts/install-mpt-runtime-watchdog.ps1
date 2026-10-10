param(
    [ValidateSet('Plan','Install','Verify','Uninstall')][string]$Mode = 'Plan',
    [ValidatePattern('^MPT Runtime Watchdog(?: [A-Za-z0-9_-]+)?$')][string]$TaskName = 'MPT Runtime Watchdog',
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
$pwsh = 'C:\Program Files\PowerShell\7\pwsh.exe'
$script = Join-Path $PSScriptRoot 'mpt-runtime-watchdog.ps1'
if (-not $WebuiDataDir) { $WebuiDataDir = "storage\webui-$WebuiPort" }
foreach ($name in @('DataDir', 'WebuiDataDir', 'BusinessSettingsFile')) {
    $value = Get-Variable -Name $name -ValueOnly
    if (-not [IO.Path]::IsPathRooted($value)) { $value = Join-Path $workspace $value }
    Set-Variable -Name $name -Value ([IO.Path]::GetFullPath($value))
}
if ($Port -eq $WebuiPort -or $DataDir -eq $WebuiDataDir) { throw 'API and WebUI require independent ports and data directories.' }
foreach ($value in @($workspace, $script, $DataDir, $WebuiDataDir, $BusinessSettingsFile, $BindAddress, $PublicBaseUrl)) {
    if ($value -match '["\r\n]' -or $value.EndsWith('\')) { throw 'Task arguments cannot contain quotes, line breaks or trailing backslashes.' }
}
$backupDir = Join-Path $storageRoot "runtime-watchdog-$Port"
$backupFile = Join-Path $backupDir 'task-rollback.xml'
function Assert-TaskPaths {
    foreach ($path in @($DataDir, $WebuiDataDir, $BusinessSettingsFile, $backupDir, $backupFile)) {
        if (-not $path.StartsWith($storageRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Task paths must stay inside this MPT workspace storage.'
        }
        $candidate = $path
        while ($candidate.Length -ge $storageRoot.Length) {
            $item = Get-Item -LiteralPath $candidate -Force -ErrorAction SilentlyContinue
            if ($null -ne $item -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Redirected task paths are forbidden.' }
            $candidate = [IO.Path]::GetDirectoryName($candidate)
        }
    }
}
Assert-TaskPaths
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
if ($identity.User.Value -eq 'S-1-5-18') { throw 'MPT runtime task must use the current interactive user, never SYSTEM.' }
$description = "MPT runtime watchdog v1 | workspace=$workspace | owner=$($identity.User.Value)"
$prefix = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $script + '" '
$arguments = $prefix + '-Port ' + $Port + ' -BindAddress "' + $BindAddress + '" -DataDir "' + $DataDir +
    '" -BusinessSettingsFile "' + $BusinessSettingsFile + '" -PublicBaseUrl "' + $PublicBaseUrl +
    '" -WebuiPort ' + $WebuiPort + ' -WebuiDataDir "' + $WebuiDataDir + '"'
if ($Mode -eq 'Plan') {
    [ordered]@{mode = $Mode; task_name = $TaskName; workspace = $workspace; user = $identity.Name
        executable = $pwsh; arguments = $arguments; description = $description
        logon_type = 'Interactive'; at_logon = $true; interval_minutes = 1
        multiple_instances = 'IgnoreNew'; execution_limit_minutes = 3; rollback_xml = $backupFile} | ConvertTo-Json
    return
}
function Get-OwnedTask {
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($null -eq $task) { return $null }
    if ($task.Description -ne $description -or @($task.Actions).Count -ne 1 -or
        $task.Actions[0].Execute -ne $pwsh -or $task.Actions[0].WorkingDirectory -ne $workspace -or
        -not $task.Actions[0].Arguments.StartsWith($prefix, [StringComparison]::Ordinal) -or
        (Get-AccountSid $task.Principal.UserId) -ne $identity.User.Value -or $task.Principal.LogonType -ne 'Interactive') {
        throw 'Existing task is not owned by this MPT workspace; refusing to modify it.'
    }
    return $task
}
function Get-AccountSid($Name) {
    if ($Name -match '^S-1-') { return [Security.Principal.SecurityIdentifier]::new($Name).Value }
    return ([Security.Principal.NTAccount]::new($Name)).Translate([Security.Principal.SecurityIdentifier]).Value
}
function Assert-TaskConfiguration($Task) {
    if ($null -eq $Task) { throw 'MPT runtime task is not installed.' }
    $principalSid = Get-AccountSid $Task.Principal.UserId
    if ($principalSid -ne $identity.User.Value -or $Task.Principal.LogonType -ne 'Interactive' -or
        $Task.Principal.RunLevel -ne 'Limited' -or $Task.Actions[0].Arguments -ne $arguments -or
        $Task.Settings.MultipleInstances -ne 'IgnoreNew' -or $Task.Settings.ExecutionTimeLimit -ne 'PT3M' -or
        -not $Task.Settings.Enabled -or @($Task.Triggers).Count -ne 2 -or
        @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' -and (Get-AccountSid $_.UserId) -eq $identity.User.Value -and $_.Enabled }).Count -ne 1 -or
        @($Task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskTimeTrigger' -and $_.Enabled -and $_.Repetition.Interval -eq 'PT1M' -and -not $_.Repetition.Duration }).Count -ne 1) {
        throw 'MPT task configuration differs from the reviewed installation.'
    }
}
$existing = Get-OwnedTask
if ($Mode -eq 'Verify') {
    Assert-TaskConfiguration $existing
    $info = Get-ScheduledTaskInfo -TaskName $TaskName -TaskPath '\'
    [ordered]@{task_name = $TaskName; workspace = $workspace; state = [string]$existing.State
        last_run_time = $info.LastRunTime; last_task_result = $info.LastTaskResult; next_run_time = $info.NextRunTime} | ConvertTo-Json
    return
}
if ($Mode -eq 'Uninstall' -and $null -eq $existing) { Write-Output 'MPT runtime task is absent.'; return }
if ($null -ne $existing) {
    Assert-TaskPaths
    New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
    $xml = Export-ScheduledTask -TaskName $TaskName -TaskPath '\'
    Assert-TaskPaths
    [IO.File]::WriteAllText($backupFile, $xml, [Text.UTF8Encoding]::new($false))
    $fresh = Get-OwnedTask
    if ((Export-ScheduledTask -TaskName $TaskName -TaskPath '\') -ne $xml) { throw 'Task identity changed before mutation.' }
}
if ($Mode -eq 'Uninstall') {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath '\' -Confirm:$false
    if ($null -ne (Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue)) { throw 'MPT task removal did not complete.' }
    Write-Output "Removed MPT runtime task; rollback XML: $backupFile"
    return
}
if (-not (Test-Path -LiteralPath $BusinessSettingsFile -PathType Leaf)) { throw 'Reviewed business settings file must exist before task installation.' }
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments -WorkingDirectory $workspace
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel Limited
$triggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $identity.Name),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1))
)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$definition = New-ScheduledTask -Action $action -Principal $principal -Trigger $triggers -Settings $settings -Description $description
$null = Get-OwnedTask
Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $definition -Force | Out-Null
Assert-TaskConfiguration (Get-OwnedTask)
Write-Output "Installed and verified MPT runtime task: $TaskName"
