<#
.SYNOPSIS
Switch the model: copy .env.<name> of the repository root to .env.

.DESCRIPTION
    use_model.ps1 <name>

copies <root>\.env.<name> over <root>\.env and prints AQOURS_CODE_PROVIDER,
AQOURS_CODE_BASE_URL and AQOURS_CODE_MODEL of the new .env. It never prints
the API key: only those three lines are read. If .env.<name> does not exist,
it stops with an error and leaves .env as it was.

-RepoRoot is for tests; it defaults to the repository this script is in.
#>
param(
    [Parameter(Mandatory = $true, Position = 0)][string]$Name,
    [string]$RepoRoot = ''
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dotenv.ps1')

if (-not $RepoRoot) {
    $RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
}
if ($Name -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') {
    [Console]::Error.WriteLine("error: invalid model name '$Name' (letters, digits, '-' and '_')")
    exit 2
}
$source = Join-Path $RepoRoot ".env.$Name"
$target = Join-Path $RepoRoot '.env'
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    # not -Filter: on Windows '.env.*' also matches '.env'
    $known = @(Get-ChildItem -LiteralPath $RepoRoot -Force -File |
               Where-Object { $_.Name -like '.env.?*' -and $_.Name -ne '.env.example' } |
               ForEach-Object { $_.Name.Substring(5) } | Sort-Object)
    $list = if ($known.Count) { $known -join ', ' } else { 'none' }
    [Console]::Error.WriteLine("error: $source not found; .env was not changed (available: $list)")
    exit 1
}

[System.IO.File]::Copy($source, $target, $true)
Write-Output "copied .env.$Name to $target"
foreach ($setting in $ModelSettingNames) {
    $value = Get-DotenvValue -Path $target -Name $setting
    if ($null -eq $value) {
        $value = '(not set)'
    }
    Write-Output "$setting=$value"
}
$overrides = @(Get-EnvironmentOverrides)
if ($overrides.Count) {
    Write-Output ("warning: set in this shell, and used instead of .env: " +
                  ($overrides -join ', '))
}
exit 0
