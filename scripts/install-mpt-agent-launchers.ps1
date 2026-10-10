param(
    [Parameter(Mandatory=$true)][string]$StagingDirectory,
    [string]$RuntimeConfig,
    [string]$ApiUrl = 'http://127.0.0.1:8010',
    [string]$SchedulerId = 'mpt-codex',
    [string[]]$PublishCapability = @(),
    [switch]$Register,
    [string[]]$Role = @(),
    [int]$MaxConcurrency = 0,
    [string[]]$SupportedModel = @(),
    [string]$ModelName = '',
    [string]$ReasoningEffort = ''
)

$ErrorActionPreference = 'Stop'
$workspace = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $workspace '.venv\Scripts\python.exe'
if (-not $RuntimeConfig) {
    $RuntimeConfig = Join-Path $workspace 'storage\business-8010\private\runtime.json'
}
if (-not (Test-Path -LiteralPath $python)) { throw 'Install the locked MPT environment with uv first.' }
$arguments = @(
    (Join-Path $PSScriptRoot 'mpt_agent_launchers.py'), 'install',
    '--staging-directory', $StagingDirectory, '--runtime-config', $RuntimeConfig,
    '--scheduler-id', $SchedulerId, '--api-url', $ApiUrl
)
foreach ($capability in $PublishCapability) { $arguments += @('--publish-capability', $capability) }
if ($Register) {
    if (-not $Role.Count -or -not $SupportedModel.Count -or
        $MaxConcurrency -lt 1 -or $MaxConcurrency -gt 16 -or -not $ModelName -or -not $ReasoningEffort) {
        throw 'Registration requires explicit roles, host capacity, supported models and model/effort.'
    }
    $arguments = @(
        (Join-Path $PSScriptRoot 'mpt_agent_launchers.py'), 'register',
        '--staging-directory', $StagingDirectory, '--max-concurrency', $MaxConcurrency,
        '--model-name', $ModelName, '--reasoning-effort', $ReasoningEffort
    )
    foreach ($selectedRole in $Role) { $arguments += @('--role', $selectedRole) }
    foreach ($model in $SupportedModel) { $arguments += @('--supported-model', $model) }
}
& $python @arguments
if ($LASTEXITCODE -ne 0) { throw 'MPT launcher staging, executable self-test or registration failed.' }
