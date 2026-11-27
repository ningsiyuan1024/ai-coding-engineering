[CmdletBinding()]
param(
    [string]$Profiles = ".factory-local/profiles",
    [string]$SymphonyRoot,
    [int]$Interval = 300
)

$ErrorActionPreference = "Stop"
$checkout = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$privateRoot = Join-Path $checkout ".factory-local"
$profilesPath = (Resolve-Path -LiteralPath $Profiles).Path
$statusFile = Join-Path $privateRoot "dispatcher-status.json"
if (Test-Path -LiteralPath $statusFile) {
    $last = Get-Content -Raw -LiteralPath $statusFile | ConvertFrom-Json
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$last.pid)"
    if ($existing -and $existing.CommandLine -match 'factory\.dispatch' -and
        $existing.CommandLine -like '*--observe-only*' -and $existing.CommandLine.Contains($profilesPath)) {
        Write-Output "Factory observer already running (PID $($last.pid))."
        return
    }
}
$serviceDir = New-Item -ItemType Directory -Force -Path (Join-Path $privateRoot "service")
$arguments = @("-B", "-m", "factory.dispatch", "--watch", "--observe-only",
               "--interval", "$Interval", "--profiles", ('"{0}"' -f $profilesPath))
if ($SymphonyRoot) {
    $arguments += @("--symphony-root", ('"{0}"' -f (Resolve-Path -LiteralPath $SymphonyRoot).Path))
}
$stamp = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$interpreter = python -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0) { throw "Python could not identify its executable." }
$worker = Start-Process -WindowStyle Hidden -FilePath $interpreter -ArgumentList $arguments `
    -WorkingDirectory $checkout -RedirectStandardOutput (Join-Path $serviceDir "$stamp.out") `
    -RedirectStandardError (Join-Path $serviceDir "$stamp.err") -PassThru
Write-Output "Factory observer launched (launcher PID $($worker.Id)). Worker heartbeat: $statusFile"
