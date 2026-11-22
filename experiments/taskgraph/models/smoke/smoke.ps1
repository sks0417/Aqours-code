<#
.SYNOPSIS
Model smoke test: one small request through the normal taskgraph single and run.

.DESCRIPTION
    smoke.ps1                      the whole test, with the model in .env
    smoke.ps1 -PrepareOnly         only step 1: create the repository
    smoke.ps1 -CheckRun <run_dir>  only step 4: check an existing run directory

Steps, in C:\tg\smoke\<timestamp>\ (-Root changes C:\tg\smoke):

  1. prepare  repo\ from template\ (git init, one commit with a fixed author
              and date): smoke.py prepare
  2. single   graph.json, a one-node graph of request.md:
              taskgraph single --final-check "python -m pytest -q tests"
  3. run      runs\<run_id>\: taskgraph run --workers 1 --worker-timeout 600,
              in the default Docker sandbox (aqours-code-eval:py311)
  4. check    the model, status, calls, tokens, final check, mul, and what
              the logs show: smoke.py check

Each step's output is also written to <step>.log in the smoke directory, and
the check to check.json. Nothing here is specific to a model. If a step
fails, the script says which one and where the run directory is, and exits 1.
-EnvFile is the .env whose AQOURS_CODE_MODEL is shown (default: the
repository's .env); the API key is never read.
#>
param(
    [string]$Root = 'C:\tg\smoke',
    [switch]$PrepareOnly,
    [string]$CheckRun = '',
    [string]$EnvFile = ''
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$aqours = (Resolve-Path (Join-Path $here '..\..\..\..')).Path
. (Join-Path $here '..\dotenv.ps1')
if (-not $EnvFile) {
    $EnvFile = Join-Path $aqours '.env'
}
$venvPython = Join-Path $aqours '.venv\Scripts\python.exe'
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { 'python' }
$helper = Join-Path $here 'smoke.py'
$request = Join-Path $here 'request.md'
$finalCheck = 'python -m pytest -q tests'
$steps = @{ 0 = 'preflight'; 1 = 'prepare'; 2 = 'taskgraph single'; 3 = 'taskgraph run';
            4 = 'check' }

$script:smokeDir = ''
$script:runDir = ''

function Invoke-Logged {
    <# Run a native command, show its output, copy it to $LogPath (UTF-8); return its exit code. #>
    param([string]$LogPath, [string]$Exe, [string[]]$Arguments)
    $writer = $null
    if ($LogPath) {
        $writer = New-Object System.IO.StreamWriter($LogPath, $false,
                                                    (New-Object System.Text.UTF8Encoding($false)))
    }
    try {
        # stderr lines arrive as ErrorRecords; they must not stop the script
        $ErrorActionPreference = 'Continue'
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $line = if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.Exception.Message
            } else {
                "$_"
            }
            Write-Host $line
            if ($writer) { $writer.WriteLine($line) }
        }
        return $LASTEXITCODE
    } finally {
        if ($writer) { $writer.Dispose() }
    }
}

function Stop-Smoke {
    <# Report the failed step and where to look, and exit 1. #>
    param([int]$Step, [string]$Message, [string]$LogPath = '')
    Write-Host ''
    Write-Host "SMOKE FAILED at step $Step ($($steps[$Step])): $Message"
    if ($script:smokeDir) { Write-Host "  smoke directory: $($script:smokeDir)" }
    if ($script:runDir) {
        Write-Host "  run directory  : $($script:runDir)"
    } elseif ($Step -ge 3) {
        Write-Host '  run directory  : none (the run did not start)'
    }
    if ($LogPath) { Write-Host "  log            : $LogPath" }
    exit 1
}

function Invoke-Check {
    <# Step 4; returns the exit code of smoke.py check. #>
    param([string]$RunDir, [string]$LogPath, [string]$JsonPath)
    Write-Host ''
    Write-Host "== step 4: check $RunDir"
    $arguments = @($helper, 'check', $RunDir, '--env-file', $EnvFile)
    if ($JsonPath) { $arguments += @('--json', $JsonPath) }
    return Invoke-Logged -LogPath $LogPath -Exe $python -Arguments $arguments
}

$savedEncoding = [Console]::OutputEncoding
$savedUtf8 = $env:PYTHONUTF8
$savedIoEncoding = $env:PYTHONIOENCODING
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $aqours
try {
    if ($CheckRun) {
        if (-not (Test-Path -LiteralPath $CheckRun -PathType Container)) {
            Stop-Smoke 4 "run directory not found: $CheckRun"
        }
        $script:runDir = (Resolve-Path -LiteralPath $CheckRun).Path
        $code = Invoke-Check -RunDir $script:runDir -LogPath '' -JsonPath ''
        if ($code -ne 0) {
            Stop-Smoke 4 $(if ($code -eq 1) { 'the smoke criteria are not met (see above)' }
                           else { "smoke.py check exited with code $code" })
        }
        Write-Host ''
        Write-Host 'SMOKE PASSED'
        Write-Host "  run directory  : $($script:runDir)"
        exit 0
    }

    if (-not $PrepareOnly) {
        Write-Host '== step 0: preflight'
        if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
            Stop-Smoke 0 "$EnvFile not found; switch to a model first (use_model.ps1 <name>)"
        }
        $model = Get-DotenvValue -Path $EnvFile -Name 'AQOURS_CODE_MODEL'
        if (-not $model) {
            Stop-Smoke 0 "AQOURS_CODE_MODEL is not set in $EnvFile"
        }
        Write-Host "model in .env: $model"
        $overrides = @(Get-EnvironmentOverrides)
        if ($overrides.Count) {
            Write-Host ("warning: set in this shell, and used instead of .env: " +
                        ($overrides -join ', '))
        }
    }

    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $script:smokeDir = Join-Path $Root $stamp
    if (Test-Path -LiteralPath $script:smokeDir) {
        Stop-Smoke 1 "$($script:smokeDir) already exists"
    }
    New-Item -ItemType Directory -Path $script:smokeDir -Force | Out-Null
    $repo = Join-Path $script:smokeDir 'repo'
    $graph = Join-Path $script:smokeDir 'graph.json'
    $runs = Join-Path $script:smokeDir 'runs'

    Write-Host ''
    Write-Host "== step 1: prepare $repo"
    $log = Join-Path $script:smokeDir 'prepare.log'
    $code = Invoke-Logged -LogPath $log -Exe $python -Arguments @($helper, 'prepare', $repo)
    if ($code -ne 0) { Stop-Smoke 1 "smoke.py prepare exited with code $code" $log }
    if ($PrepareOnly) {
        Write-Host ''
        Write-Host "repository: $repo"
        exit 0
    }

    Write-Host ''
    Write-Host "== step 2: taskgraph single -> $graph"
    $log = Join-Path $script:smokeDir 'single.log'
    $code = Invoke-Logged -LogPath $log -Exe $python -Arguments @(
        '-m', 'aqours_code.taskgraph', 'single', $request, '--repo', $repo, '--out', $graph,
        '--final-check', $finalCheck)
    if ($code -ne 0) { Stop-Smoke 2 "taskgraph single exited with code $code" $log }

    Write-Host ''
    Write-Host "== step 3: taskgraph run (up to about 10 minutes) -> $runs"
    $log = Join-Path $script:smokeDir 'run.log'
    $runCode = Invoke-Logged -LogPath $log -Exe $python -Arguments @(
        '-m', 'aqours_code.taskgraph', 'run', $graph, '--repo', $repo, '--out', $runs,
        '--workers', '1', '--worker-timeout', '600')
    $made = @(Get-ChildItem -LiteralPath $runs -Directory -ErrorAction SilentlyContinue)
    if ($made.Count -eq 1) {
        $script:runDir = $made[0].FullName
    }
    if (-not $script:runDir) {
        Stop-Smoke 3 "taskgraph run exited with code $runCode and made no run directory" $log
    }

    $checkLog = Join-Path $script:smokeDir 'check.log'
    $checkCode = Invoke-Check -RunDir $script:runDir -LogPath $checkLog `
                              -JsonPath (Join-Path $script:smokeDir 'check.json')
    if ($runCode -ne 0) {
        Stop-Smoke 3 "taskgraph run exited with code $runCode (the run did not succeed)" $log
    }
    if ($checkCode -ne 0) {
        Stop-Smoke 4 $(if ($checkCode -eq 1) { 'the smoke criteria are not met (see above)' }
                       else { "smoke.py check exited with code $checkCode" }) $checkLog
    }
    Write-Host ''
    Write-Host 'SMOKE PASSED'
    Write-Host "  smoke directory: $($script:smokeDir)"
    Write-Host "  run directory  : $($script:runDir)"
    exit 0
} finally {
    Pop-Location
    [Console]::OutputEncoding = $savedEncoding
    $env:PYTHONUTF8 = $savedUtf8
    $env:PYTHONIOENCODING = $savedIoEncoding
}
