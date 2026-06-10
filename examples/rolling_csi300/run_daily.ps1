<#
.SYNOPSIS
    Daily qlib workflow: update data, run backtest, sync positions to easyths.
.DESCRIPTION
    Runs the qlib pipeline inside WSL with real-time output.
    Designed to be scheduled via Windows Task Scheduler for daily execution.

    Schedule:
      Program:  C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe
      Args:     -ExecutionPolicy Bypass -File C:\path\to\run_daily.ps1
      Trigger:  Daily at 15:30 (after market close)
#>

param(
    [string]$ApiKey = "snf81kqdvb07xgcymu6hi4wterza2jo9",
    [string]$LogDir = "$env:USERPROFILE\qlib_sync_logs",
    [string]$InvestRatio = "0.95"
)

# 解决 WSL 输出中文乱码
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $LogDir "qlib_sync_$timestamp.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

Write-Log "=== qlib daily sync started ==="

$pythonPath = "/home/tc/qlib/.venv/bin/python -u"
$wslCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath run_workflow.py --api-key '$ApiKey' --skip-train --invest-ratio $InvestRatio; echo __EXIT__$?"

Write-Log "Running: $wslCmd"

$exitCode = 0
wsl -e bash -c $wslCmd 2>&1 | ForEach-Object {
    $line = $_
    if ($line -match '__EXIT__(\d+)') {
        $exitCode = [int]$Matches[1]
    } else {
        Write-Host $line
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
}

if ($exitCode -eq 0) {
    Write-Log "=== qlib daily sync completed successfully ==="
} else {
    Write-Log "=== qlib daily sync FAILED (exit code: $exitCode) ==="
}

# Keep only last 30 logs
Get-ChildItem $LogDir -Filter "qlib_sync_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exitCode
