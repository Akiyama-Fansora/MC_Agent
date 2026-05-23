param(
    [int]$Port = 8765,
    [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $Root "runtime"
$LogDir = Join-Path $Root "logs"
$PidFile = Join-Path $RuntimeDir "mcagent_web.pid"
$OutLog = Join-Path $LogDir "mcagent_web.out.log"
$ErrLog = Join-Path $LogDir "mcagent_web.err.log"

New-Item -ItemType Directory -Force -Path $RuntimeDir, $LogDir | Out-Null

$EnvFile = Join-Path $Root ".env"
if (Test-Path -LiteralPath $EnvFile) {
    Get-Content -LiteralPath $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            return
        }
        $name, $value = $line.Split("=", 2)
        $name = $name.Trim()
        $value = $value.Trim().Trim('"').Trim("'")
        if ($name) {
            [Environment]::SetEnvironmentVariable($name, $value, "Process")
        }
    }
}

$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1

if ($existing) {
    $pidValue = $existing.OwningProcess
    Set-Content -Path $PidFile -Value $pidValue -Encoding ASCII
    Write-Host "MCagent web service is already running."
    Write-Host "PID: $pidValue"
    Write-Host "URL: http://${HostName}:$Port"
    exit 0
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    Write-Error "Python was not found in PATH."
    exit 1
}

$args = @(
    "api.py",
    "--host", $HostName,
    "--port", "$Port"
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

Start-Sleep -Seconds 2
$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.OwningProcess -eq $process.Id } |
    Select-Object -First 1

if ($listener) {
    Write-Host "MCagent web service started."
    Write-Host "PID: $($process.Id)"
    Write-Host "URL: http://${HostName}:$Port"
    Write-Host "Logs:"
    Write-Host "  $OutLog"
    Write-Host "  $ErrLog"
    exit 0
}

Write-Host "Start command was sent, but port $Port is not listening yet."
Write-Host "Check logs:"
Write-Host "  $OutLog"
Write-Host "  $ErrLog"
exit 1
