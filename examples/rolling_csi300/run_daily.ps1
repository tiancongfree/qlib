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

    Restart remote easyths on connection failure:
      .\run_daily.ps1 -RestartCmd "powershell.exe -Command \"...\""
#> 

param(
    [string]$ApiKey = "snf81kqdvb07xgcymu6hi4wterza2jo9",
    [string]$LogDir = "$env:USERPROFILE\qlib_sync_logs",
    [string]$InvestRatio = "0.95",
    [string]$RestartCmd = "",
    [string]$RemoteExePath = "C:\ProgramData\miniforge3\Scripts\easyths.exe",
    [string]$RemoteExeArgs = "--config C:\Users\tc\easyths\config.toml"
)

# ��� WSL �����������
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
    'System\.Management\.Automation\.RemoteException',
    'Downloading artifacts:',
    'INFO - qlib\.timer',
    'INFO - qlib\.RecorderCollector',
    'INFO - qlib\.workflow - .* starts running',
    'INFO - qlib\.backtest caller',
    'WARNING - qlib\.Rolling',
    'WARNING - qlib\.BaseExecutor'
)

function Test-LogNoise {
    param([string]$Line)
    foreach ($p in $NoisePatterns) {
        if ($Line -match $p) { return $true }
    }
    return $false
}

<#
.SYNOPSIS
    Restart easyths on remote machine via SSH.
    Uses the existing scheduled task 'easyths_server' which starts:
      python -m easyths.main --config C:\Users\tc\easyths\config.toml
#>
function Restart-EasyTHS {
    param(
        [string]$RemoteHost = "192.168.11.244",
        [int]$Port = 7648,
        [string]$RemoteUser = "tc",
        [string]$SshPass = "896573",
        [string]$TaskName = "easyths_server"
    )
    Write-Log "Attempting to restart easyths on $RemoteHost via SSH ..."

    # Quick check: if port is alive, skip
    try {
        $tcp = [System.Net.Sockets.TcpClient]::new()
        $tcp.ConnectAsync($RemoteHost, $Port).Wait(2000)
        if ($tcp.Connected) {
            $tcp.Close()
            Write-Log "  Port $Port is open, easyths may still be starting. Skipping restart."
            return $false
        }
    } catch {}

    # Ensure SSH command is available
    $sshExe = Get-Command "ssh" -ErrorAction SilentlyContinue
    if (-not $sshExe) {
        Write-Log "  ssh not found on this machine. Install OpenSSH Client."
        return $false
    }

    # Find easyths/python process via SSH (tasklist | findstr)
    Write-Log "  Checking remote process..."
    $pidOut = ssh -o BatchMode=yes -o ConnectTimeout=5 $RemoteUser@$RemoteHost "tasklist /FO CSV /NH | findstr /i python" 2>$null
    $foundPid = $null
    $foundPids = @()
    if ($pidOut) {
        foreach ($line in $pidOut) {
            $parts = $line -split '",'
            if ($parts.Count -ge 2) {
                $pid = ($parts[1] -replace '"','').Trim()
                if ($pid -match '^\d+$') { $foundPids += $pid }
            }
        }
    }
    if ($foundPids.Count -gt 0) {
        Write-Log "  Found python PIDs: $($foundPids -join ', ')"
        ssh -o BatchMode=yes $RemoteUser@$RemoteHost "taskkill /F /PID $($foundPids[0])" 2>$null | Out-Null
        Write-Log "  Killed PID $($foundPids[0])"
        Start-Sleep -Seconds 3
    } else {
        Write-Log "  No python process found (may still start fresh)."
    }

    # Start via scheduled task (this correctly uses --config C:\Users\tc\easyths\config.toml)
    Write-Log "  Running scheduled task '$TaskName'..."
    $taskOut = ssh -o BatchMode=yes $RemoteUser@$RemoteHost "schtasks /run /tn $TaskName" 2>$null
    Write-Log "  Task result: $taskOut"

    Write-Log "  Waiting for port $Port (up to 40s)..."
    $startTime = Get-Date
    while ((Get-Date) -lt $startTime.AddSeconds(40)) {
        Start-Sleep -Seconds 3
        try {
            $tcp = [System.Net.Sockets.TcpClient]::new()
            $tcp.ConnectAsync($RemoteHost, $Port).Wait(3000)
            if ($tcp.Connected) {
                $tcp.Close()
                Write-Log "  Port $Port is ready."
                return $true
            }
        } catch {}
    }
    Write-Log "  Port $Port not ready after 40s."
    return $false
}

Write-Log "=== qlib daily sync started ==="

$pythonPath = "/home/tc/qlib/.venv/bin/python -u"
$extraArgs = ""
if ($RestartCmd) {
    $extraArgs = " --restart-cmd '$RestartCmd'"
}
$wslCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath run_workflow.py --api-key '$ApiKey' --skip-train --sync=True --invest-ratio $InvestRatio $extraArgs; echo __EXIT__`$?"
$icDecayCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath analyze_ic_decay.py --exp_name rolling_csi300_lgbm --freq quarterly --output_dir /home/tc/qlib/examples/rolling_csi300; echo __EXIT__`$?"
$equityCurveCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath analyze_equity_curve.py --exp_name rolling_csi300_lgbm; echo __EXIT__`$?"
$ddAlertCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath dd_alert.py --exp_name rolling_csi300_lgbm; echo __EXIT__`$?"
$monitorIcCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath monitor_ic.py --exp_name rolling_csi300_lgbm_ndrop1; echo __EXIT__`$?"

Write-Log "Running: $($wslCmd -replace [regex]::Escape($ApiKey), '****')"

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
    Write-Log "=== qlib daily sync completed successfully ==="
    # Run IC decay analysis; the visualization plot is saved to:
    #   /home/tc/qlib/examples/rolling_csi300/ic_decay_quarterly_rolling_csi300_lgbm.png
    Write-Log "Running IC decay analysis..."
    wsl -u tc bash -c $icDecayCmd 2>&1 | ForEach-Object {
        $line = $_ -replace "`r", ""
        if ($line -match '__EXIT__(\d+)') {
            Write-Log "IC decay analysis exit code: $([int]$Matches[1])"
        } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
            [Console]::WriteLine($line)
            Add-Content -Path $logFile -Value $line -Encoding UTF8
        }
    }
    # Run equity curve analysis; the interactive HTML is saved to:
    #   /home/tc/qlib/examples/rolling_csi300/equity_curve_rolling_csi300_lgbm.html
    Write-Log "Running equity curve analysis..."
    wsl -u tc bash -c $equityCurveCmd 2>&1 | ForEach-Object {
        $line = $_ -replace "`r", ""
        if ($line -match '__EXIT__(\d+)') {
            Write-Log "Equity curve analysis exit code: $([int]$Matches[1])"
        } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
            [Console]::WriteLine($line)
            Add-Content -Path $logFile -Value $line -Encoding UTF8
        }
    }
    # Run drawdown alert; emails when a new 5% drawdown tier is crossed.
    Write-Log "Running drawdown alert..."
    wsl -u tc bash -c $ddAlertCmd 2>&1 | ForEach-Object {
        $line = $_ -replace "`r", ""
        if ($line -match '__EXIT__(\d+)') {
            Write-Log "Drawdown alert exit code: $([int]$Matches[1])"
        } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
            [Console]::WriteLine($line)
            Add-Content -Path $logFile -Value $line -Encoding UTF8
        }
    }
    # Run IC health monitor; warns on sustained low RankIC.
    Write-Log "Running IC monitor..."
    wsl -u tc bash -c $monitorIcCmd 2>&1 | ForEach-Object {
        $line = $_ -replace "`r", ""
        if ($line -match '__EXIT__(\d+)') {
            Write-Log "IC monitor exit code: $([int]$Matches[1])"
        } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
            [Console]::WriteLine($line)
            Add-Content -Path $logFile -Value $line -Encoding UTF8
        }
    }
} else {
    Write-Log "=== qlib daily sync FAILED (exit code: $exitCode) ==="
    $restarted = Restart-EasyTHS
    if ($restarted) {
        Write-Log "  Retrying sync after restart..."
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
            Write-Log "=== qlib daily sync completed successfully (after restart) ==="
            # Run IC decay analysis; the visualization plot is saved to:
            #   /home/tc/qlib/examples/rolling_csi300/ic_decay_quarterly_rolling_csi300_lgbm.png
            Write-Log "Running IC decay analysis..."
            wsl -u tc bash -c $icDecayCmd 2>&1 | ForEach-Object {
                $line = $_ -replace "`r", ""
                if ($line -match '__EXIT__(\d+)') {
                    Write-Log "IC decay analysis exit code: $([int]$Matches[1])"
                } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
                    [Console]::WriteLine($line)
                    Add-Content -Path $logFile -Value $line -Encoding UTF8
                }
            }
            # Run equity curve analysis; the interactive HTML is saved to:
            #   /home/tc/qlib/examples/rolling_csi300/equity_curve_rolling_csi300_lgbm.html
            Write-Log "Running equity curve analysis..."
            wsl -u tc bash -c $equityCurveCmd 2>&1 | ForEach-Object {
                $line = $_ -replace "`r", ""
                if ($line -match '__EXIT__(\d+)') {
                    Write-Log "Equity curve analysis exit code: $([int]$Matches[1])"
                } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
                    [Console]::WriteLine($line)
                    Add-Content -Path $logFile -Value $line -Encoding UTF8
                }
            }
            # Run drawdown alert; emails when a new 5% drawdown tier is crossed.
            Write-Log "Running drawdown alert..."
            wsl -u tc bash -c $ddAlertCmd 2>&1 | ForEach-Object {
                $line = $_ -replace "`r", ""
                if ($line -match '__EXIT__(\d+)') {
                    Write-Log "Drawdown alert exit code: $([int]$Matches[1])"
                } elseif ($line -ne "" -and -not (Test-LogNoise $line)) {
                    [Console]::WriteLine($line)
                    Add-Content -Path $logFile -Value $line -Encoding UTF8
                }
            }
        } else {
            Write-Log "=== qlib daily sync FAILED again after restart (exit code: $exitCode) ==="
        }
    }
}

# Keep only last 30 logs
Get-ChildItem $LogDir -Filter "qlib_sync_*.log" |
    Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 |
    Remove-Item -Force -ErrorAction SilentlyContinue

exit $exitCode
