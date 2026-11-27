[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$SymphonyRoot,
    [string]$Root,
    [string]$Profiles,
    [string]$Plan,
    [ValidateSet("investigate", "implement")][string]$Mode = "investigate",
    [switch]$Check,
    [switch]$PromptForLinearKey
)

$ErrorActionPreference = "Stop"
if (!$env:OSS_LINEAR_API_KEY -and !$env:LINEAR_API_KEY -and !$PromptForLinearKey) {
    [Console]::Error.WriteLine("Factory Symphony requires a read credential for Linear. Use -PromptForLinearKey to enter it locally.")
    exit 77
}
$checkout = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
if (!$Root) { $Root = Join-Path $checkout ".factory-local" }
if (!$Profiles) { $Profiles = Join-Path $Root "profiles" }
$env:FACTORY_ROOT = (Resolve-Path -LiteralPath $Root).Path
$env:FACTORY_PROFILES = (Resolve-Path -LiteralPath $Profiles).Path
if ($Plan) { $env:FACTORY_PLAN = (Resolve-Path -LiteralPath $Plan).Path }
$env:FACTORY_SYMPHONY_ROOT = (Resolve-Path -LiteralPath $SymphonyRoot).Path
$env:FACTORY_MODE = $Mode
$env:FACTORY_PYTHON = python -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Python could not identify its executable." }

if (!(Get-Command elixir.bat -ErrorAction SilentlyContinue) -or !(Get-Command erl.exe -ErrorAction SilentlyContinue)) {
    $versions = python -c "import os,pathlib,json,tomllib; print(json.dumps(tomllib.loads((pathlib.Path(os.environ['FACTORY_SYMPHONY_ROOT'])/'mise.toml').read_text())['tools']))" | ConvertFrom-Json
    $locations = @($env:MISE_DATA_DIR, (Join-Path $env:LOCALAPPDATA "mise"),
                   (Join-Path $env:USERPROFILE "AppData/Local/mise")) | Where-Object { $_ } | Select-Object -Unique
    foreach ($tool in @("elixir","erlang")) {
        $command = if ($tool -eq "elixir") { "elixir.bat" } else { "erl.exe" }
        if (Get-Command $command -ErrorAction SilentlyContinue) { continue }
        $version = $versions.$tool
        foreach ($location in $locations) {
            $installed = Get-ChildItem -Directory -LiteralPath (Join-Path $location "installs/$tool") -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -eq $version -or $_.Name.StartsWith("$version.") } |
                Sort-Object { [version]($_.Name -replace '[^0-9.].*$', '') } -Descending |
                Select-Object -First 1
            if ($installed -and (Test-Path -LiteralPath (Join-Path $installed.FullName "bin/$command"))) {
                $env:PATH = (Join-Path $installed.FullName "bin") + ";" + $env:PATH
                break
            }
        }
        if (!(Get-Command $command -ErrorAction SilentlyContinue)) { throw "Place the installed $tool bin directory on PATH." }
    }
}
$entry = Join-Path $checkout "workflows/symphony/worker_boot.exs"
$localKey = $null
try {
    if (!$env:OSS_LINEAR_API_KEY -and !$env:LINEAR_API_KEY) {
        $localKey = Read-Host "Read-only Linear credential for this factory instance" -AsSecureString
        if (!$localKey.Length) { exit 77 }
        $env:OSS_LINEAR_API_KEY = [Net.NetworkCredential]::new("", $localKey).Password
    }
    if ($Check) { & elixir.bat $entry --check } else { & elixir.bat $entry }
    $resultCode = $LASTEXITCODE
} finally {
    if ($null -ne $localKey) {
        [Environment]::SetEnvironmentVariable("OSS_LINEAR_API_KEY", $null, "Process")
        $localKey.Dispose()
    }
}
exit $resultCode
