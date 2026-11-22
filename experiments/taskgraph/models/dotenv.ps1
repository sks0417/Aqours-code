# Read single settings from a .env file, for use_model.ps1 and smoke\smoke.ps1.
#
# Dot-source it:  . (Join-Path $PSScriptRoot 'dotenv.ps1')
#
# Only the lines whose name is asked for are looked at, so a caller that never
# asks for AQOURS_CODE_API_KEY never sees the key. The rules are those of
# aqours_code.taskgraph.gate._dotenv_value: an optional "export ", the last
# assignment wins, surrounding quotes are removed, and an unquoted value ends
# at " #".

# The settings that differ between model files and may be shown. The API key
# is deliberately not one of them.
$ModelSettingNames = @('AQOURS_CODE_PROVIDER', 'AQOURS_CODE_BASE_URL', 'AQOURS_CODE_MODEL')

# Every setting a model file may change; an environment variable of the same
# name wins over .env (aqours_code.config loads .env with override=False).
$ModelOverrideNames = @('AQOURS_CODE_PROVIDER', 'AQOURS_CODE_BASE_URL', 'AQOURS_CODE_MODEL',
                        'AQOURS_CODE_API_KEY')

function Get-DotenvValue {
    <# The value of $Name in the .env file $Path, or $null if it is not assigned. #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $value = $null
    foreach ($raw in [System.IO.File]::ReadAllLines($Path, [System.Text.Encoding]::UTF8)) {
        $line = $raw.Trim()
        if ($line.StartsWith('export ')) {
            $line = $line.Substring(7).TrimStart()
        }
        $eq = $line.IndexOf('=')
        if ($eq -lt 0 -or $line.Substring(0, $eq).Trim() -ne $Name) {
            continue
        }
        $rest = $line.Substring($eq + 1).Trim()
        if ($rest.Length -gt 1 -and ($rest[0] -eq "'" -or $rest[0] -eq '"') -and
                $rest.EndsWith([string]$rest[0])) {
            $rest = $rest.Substring(1, $rest.Length - 2)
        } else {
            $hash = $rest.IndexOf(' #')
            if ($hash -ge 0) {
                $rest = $rest.Substring(0, $hash)
            }
            $rest = $rest.Trim()
        }
        $value = $rest
    }
    return $value
}

function Get-EnvironmentOverrides {
    <# Names of the model settings that are set as environment variables. #>
    $names = @()
    foreach ($name in $ModelOverrideNames) {
        if ([Environment]::GetEnvironmentVariable($name)) {
            $names += $name
        }
    }
    return $names
}
