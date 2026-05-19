param(
    [string]$Profile = "standard"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root "runtime"
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $RuntimeDir "bulk_backfill.pid"
$OutLog = Join-Path $LogDir "bulk_backfill.out.log"
$ErrLog = Join-Path $LogDir "bulk_backfill.err.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir, $LogDir | Out-Null

if (Test-Path $PidFile) {
    $oldPid = Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -match "^\d+$") {
        $oldProcess = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($oldProcess) {
            Write-Host "Bulk backfill is already running."
            Write-Host "PID: $oldPid"
            Write-Host "Log: $OutLog"
            exit 0
        }
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python was not found in PATH."
    exit 1
}

$args = @(
    "-u",
    "scripts\bulk_backfill.py",
    "--profile", $Profile,
    "--ingest",
    "--createwiki",
    "--ftbwiki",
    "--followup"
)

$process = Start-Process `
    -FilePath $python.Source `
    -ArgumentList $args `
    -WorkingDirectory $Root `
    -WindowStyle Hidden `
    -RedirectStandardOutput $OutLog `
    -RedirectStandardError $ErrLog `
    -PassThru

Set-Content -Path $PidFile -Value $process.Id -Encoding ASCII

Write-Host "Bulk backfill started."
Write-Host "PID: $($process.Id)"
Write-Host "Profile: $Profile"
Write-Host "Log:"
Write-Host "  $OutLog"
Write-Host "  $ErrLog"
exit 0
