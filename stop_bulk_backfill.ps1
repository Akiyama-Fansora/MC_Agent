param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root "runtime"
$PidFile = Join-Path $RuntimeDir "bulk_backfill.pid"

if (-not (Test-Path $PidFile)) {
    Write-Host "No bulk backfill PID file found."
    exit 0
}

$pidText = Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pidText -notmatch "^\d+$") {
    Remove-Item -Path $PidFile -Force
    Write-Host "Invalid PID file removed."
    exit 0
}

$process = Get-Process -Id ([int]$pidText) -ErrorAction SilentlyContinue
if (-not $process) {
    Remove-Item -Path $PidFile -Force
    Write-Host "Bulk backfill is not running."
    exit 0
}

Write-Host "Stopping bulk backfill PID $pidText..."
Stop-Process -Id ([int]$pidText) -Force
Remove-Item -Path $PidFile -Force
Write-Host "Bulk backfill stopped."
exit 0
