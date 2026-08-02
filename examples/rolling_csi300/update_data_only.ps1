<#
.SYNOPSIS
    Daily qlib data update only (no backtest, no trading sync).
.DESCRIPTION
    Runs update_baostock.py inside WSL to refresh qlib data from baostock.
    Designed to be scheduled via Windows Task Scheduler (e.g. 21:00 nightly).

    After data update, run run_daily.ps1 in the morning to backtest + submit orders.
#>

param(
    [string]$LogDir = "$env:USERPROFILE\qlib_sync_logs"
)

# 解决 WSL 输出中文乱码
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logFile = Join-Path $LogDir "qlib_update_$timestamp.log"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-Log {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

$NoisePatterns = @(
    'keys in group:',
    'Mean of empty slice',
    'return np\.nanmean\(self\.data\)',
    'Gym has been unmaintained',
    'Please upgrade to Gymnasium',
    "replace 'import gym'",
    'migration guide at https://gymnasium',
    'INFO - qlib\.Initialization',
    'FutureWarning: The filesystem tracking backend',
    'return FileStore\(store_uri, store_uri\)',
    'Downloading artifacts:'
)

function Test-LogNoise {
    param([string]$Line)
    foreach ($p in $NoisePatterns) {
        if ($Line -match $p) { return $true }
    }
    return $false
}

Write-Log "=== qlib data update started ==="

$pythonPath = "/home/tc/qlib/.venv/bin/python -u"
$wslCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath update_baostock.py; echo __EXIT__`$?"

Write-Log "Running: $wslCmd"

$exitCode = 0
wsl -u tc bash -c $wslCmd 2>&1 | ForEach-Object {
    $line = $_ -replace "`r", ""
    if ($line -match '__EXIT__(\d+)') {
        $exitCode = [int]$Matches[1]
    } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
        [Console]::WriteLine($line)
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
}

if ($exitCode -eq 0) {
    Write-Log "=== qlib data update completed successfully ==="
} else {
    Write-Log "=== qlib data update FAILED (exit code: $exitCode) ==="
}

# Keep only last 30 logs
Get-ChildItem $LogDir -Filter "qlib_update_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exitCode
