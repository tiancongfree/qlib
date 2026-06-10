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
    [string]$RestartCmd = ""
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

<#
.SYNOPSIS
    Restart easyths on remote machine via WinRM.
    Prerequisite: run once as Admin on BOTH machines:
      本地机:  winrm quickconfig
      本地机:  Set-Item WSMan:\localhost\Client\TrustedHosts -Value '192.168.11.244' -Force
      远程机:  winrm quickconfig
      远程机:  Set-Item WSMan:\localhost\Client\TrustedHosts -Value '192.168.11.169' -Force
      远程机:  Set-Item WSMan:\localhost\Service\Auth\Negotiate -Value $true -Force
#>
function Restart-EasyTHS {
    param([string]$RemoteHost = "192.168.11.244", [int]$Port = 7648)
    Write-Log "Attempting to restart easyths on $RemoteHost ..."

    # Quick check: if the port is alive and responding, skip restart
    try {
        $tcp = [System.Net.Sockets.TcpClient]::new()
        $tcp.ConnectAsync($RemoteHost, $Port).Wait(2000)
        if ($tcp.Connected) {
            $tcp.Close()
            Write-Log "  Port $Port is open, service might still be starting up. Skipping restart."
            return $false
        }
    } catch {}

    # Ensure WinRM is running and TrustedHosts is set
    try {
        $wsman = Get-Service -Name WinRM -ErrorAction Stop
        if ($wsman.Status -ne 'Running') {
            Write-Log "  Starting WinRM service..."
            Start-Service -Name WinRM
        }
        $current = (Get-Item WSMan:\localhost\Client\TrustedHosts -ErrorAction SilentlyContinue).Value
        if ($current -notlike "*$RemoteHost*") {
            Write-Log "  Adding $RemoteHost to TrustedHosts..."
            Set-Item WSMan:\localhost\Client\TrustedHosts -Value "$current,$RemoteHost" -Force
        }
    } catch {
        Write-Log "  WinRM setup failed. Try manually as Admin:"
        Write-Log "    winrm quickconfig"
        Write-Log "    Set-Item WSMan:\localhost\Client\TrustedHosts -Value '$RemoteHost' -Force"
        return $false
    }

    # Build credential: use stored cred file if exists, else fall back to default
    $credPath = Join-Path $PSScriptRoot "easyths_remote.cred"
    $cred = $null
    if (Test-Path $credPath) {
        try { $cred = Import-CliXml -Path $credPath } catch { $cred = $null }
    }
    $sessionParams = @{ ComputerName = $RemoteHost; ErrorAction = "Stop" }
    if ($cred) { $sessionParams.Credential = $cred }
    else { $sessionParams.Authentication = "Negotiate" }

    # 1. Find PID via Invoke-Command
    $procId = $null
    try {
        $r = Invoke-Command @sessionParams -ScriptBlock {
            param($p)
            $line = netstat -ano | Select-String ":$p "
            if ($line) { $line[0] -split '\s+' | Select-Object -Last 1 }
            else { $null }
        } -ArgumentList $Port
        $procId = $r
    } catch {
        Write-Log "  Cannot reach $RemoteHost via WinRM."
        if (-not $cred) {
            Write-Log "  Create credential file ONCE (you'll be prompted for remote password):"
            Write-Log "    Get-Credential | Export-CliXml -Path '$credPath'"
        }
        return $false
    }

    if (-not $procId) {
        Write-Log "  No process found on port $Port, starting fresh..."
    } else {
        # 2. Kill
        Write-Log "  Found PID $procId, killing..."
        try {
            Invoke-Command @sessionParams -ScriptBlock {
                param($id) taskkill /F /PID $id
            } -ArgumentList $procId | Out-Null
            Write-Log "  Killed PID $procId"
        } catch {
            Write-Log "  Kill failed: $_"
            return $false
        }
    }

    # 3. Restart via WMI (process survives WinRM session)
    Write-Log "  Restarting easyths... (waiting up to 40s)"
    try {
        $startResult = Invoke-Command @sessionParams -ScriptBlock {
            $cmd = "C:\ProgramData\miniforge3\Scripts\easyths.exe --config C:\Users\tc\easyths\config.toml"
            $r = ([wmiclass]"Win32_Process").Create($cmd, $null, $null)
            if ($r.ReturnValue -eq 0) { return "PID:$($r.ProcessId)" }
            return "ERR:$($r.ReturnValue)"
        }
        Write-Log "  Start result: $startResult"
        if ($startResult -match "^ERR:(\d+)") {
            Write-Log "  WMI Create failed (code $($Matches[1]))"
            return $false
        }
        Write-Log "  Waiting for port $Port..."
        for ($i = 0; $i -lt 20; $i++) {
            Start-Sleep -Seconds 2
            try {
                $tcp = [System.Net.Sockets.TcpClient]::new()
                $tcp.ConnectAsync($RemoteHost, $Port).Wait(2000)
                if ($tcp.Connected) { $tcp.Close(); Write-Log "  Port $Port is ready."; return $true }
            } catch {}
        }
        Write-Log "  Port $Port not ready after 40s."
        return $false
    } catch {
        Write-Log "  Restart failed: $_"
        return $false
    }
}

Write-Log "=== qlib daily sync started ==="

$pythonPath = "/home/tc/qlib/.venv/bin/python -u"
$extraArgs = ""
if ($RestartCmd) {
    $extraArgs = " --restart-cmd '$RestartCmd'"
}
$wslCmd = "cd /home/tc/qlib/examples/rolling_csi300 && $pythonPath run_workflow.py --api-key '$ApiKey' --skip-train --invest-ratio $InvestRatio$extraArgs; echo __EXIT__`$?"

Write-Log "Running: $wslCmd"

$exitCode = 0
wsl -e bash -c $wslCmd 2>&1 | ForEach-Object {
    $line = $_ -replace "`r", ""
    if ($line -match '__EXIT__(\d+)') {
        $exitCode = [int]$Matches[1]
    } elseif ($line -ne "") {
        [Console]::WriteLine($line)
        Add-Content -Path $logFile -Value $line -Encoding UTF8
    }
}

if ($exitCode -eq 0) {
    Write-Log "=== qlib daily sync completed successfully ==="
} else {
    Write-Log "=== qlib daily sync FAILED (exit code: $exitCode) ==="
    $restarted = Restart-EasyTHS
    if ($restarted) {
        Write-Log "  Retrying sync after restart..."
        $exitCode = 0
        wsl -e bash -c $wslCmd 2>&1 | ForEach-Object {
            $line = $_ -replace "`r", ""
            if ($line -match '__EXIT__(\d+)') {
                $exitCode = [int]$Matches[1]
            } elseif ($line -ne "") {
                [Console]::WriteLine($line)
                Add-Content -Path $logFile -Value $line -Encoding UTF8
            }
        }
        if ($exitCode -eq 0) {
            Write-Log "=== qlib daily sync completed successfully (after restart) ==="
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
