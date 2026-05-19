param(
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root "runtime"
$PidFile = Join-Path $RuntimeDir "mcagent_web.pid"

$pids = @()

$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($item in $listeners) {
    if ($item.OwningProcess) {
        $pids += [int]$item.OwningProcess
    }
}

if (Test-Path $PidFile) {
    $pidText = (Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($pidText -match "^\d+$") {
        $pids += [int]$pidText
    }
}

$pids = $pids | Sort-Object -Unique

if (-not $pids.Count) {
    Write-Host "No MCagent web service found on port $Port."
    if (Test-Path $PidFile) {
        Remove-Item -Path $PidFile -Force
    }
    exit 0
}

foreach ($pidValue in $pids) {
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($process) {
        Write-Host "Stopping PID $pidValue ($($process.ProcessName))..."
        Stop-Process -Id $pidValue -Force
    }
}

Start-Sleep -Seconds 1
if (Test-Path $PidFile) {
    Remove-Item -Path $PidFile -Force
}

$stillRunning = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($stillRunning) {
    Write-Error "Port $Port is still occupied. Please check Task Manager."
    exit 1
}

Write-Host "MCagent web service stopped. Port $Port is free."
exit 0
