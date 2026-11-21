# Run the split gate on the four tasks from their existing plans, then evaluate it offline.
#
#   powershell -ExecutionPolicy Bypass -File experiments\taskgraph\gate\run_four.ps1
#
# Each gate run reuses the plan's step-1 draft (--from-report) and makes one model
# call (the location). Results: C:\tg\gate\<task>.json (and <task>.json.logs\).
# Then two evaluations, both printed and written to C:\tg\gate\evaluation.md:
# first the ideal location (the full plans' files, no gate output), then the
# gate outputs. A task whose gate fails is reported and the others still run.

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
$OutDir = "C:\tg\gate"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$Tasks = [ordered]@{
    "click"   = "C:\tg\solve\click-v3\graph.json.report.json"
    "jp"      = "C:\tg\solve\jp-v31\graph.json.report.json"
    "dvc0.89" = "C:\tg\solve\sweevo-dvc089-v3c\graph.json.report.json"
    "dask"    = "C:\tg\solve\sweevo-dask0808-v3d\graph.json.report.json"
}

Push-Location $Root
try {
    $Failed = @()
    foreach ($Name in $Tasks.Keys) {
        $Out = Join-Path $OutDir "$Name.json"
        Write-Host "== gate: $Name"
        & $Python -m aqours_code.taskgraph gate --from-report $Tasks[$Name] --out $Out
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "gate for $Name ended with exit code $LASTEXITCODE"
            $Failed += $Name
        }
    }
    $Md = Join-Path $OutDir "evaluation.md"
    Write-Host "== evaluation: ideal location"
    & $Python experiments\taskgraph\gate\evaluate.py --ideal --out $Md --json (Join-Path $OutDir "evaluation_ideal.json")
    Write-Host "== evaluation: gate outputs"
    & $Python experiments\taskgraph\gate\evaluate.py --gate-dir $OutDir --out $Md --append --json (Join-Path $OutDir "evaluation_gate.json")
    if ($Failed.Count -gt 0) {
        Write-Warning ("gate gave no decision for: " + ($Failed -join ", "))
    }
}
finally {
    Pop-Location
}
