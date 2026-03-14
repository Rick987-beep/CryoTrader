# ============================================================================
# CoincallTrader — Clean Restart (Stop → Clean → Start)
# Run as Administrator in PowerShell on the production machine
# ============================================================================

param(
    [string]$RepoPath = "C:\CoincallTrader",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# --- 1) Stop all running instances ---
Write-Step "Stopping CoincallTrader..."

# Stop the NSSM service
$svc = Get-Service -Name "CoincallTrader" -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    if ($DryRun) {
        Write-Warn "[DRY RUN] Would stop CoincallTrader service."
    } else {
        Stop-Service CoincallTrader -Force
        Start-Sleep -Seconds 3
        Write-Ok "Service stopped."
    }
} else {
    Write-Warn "Service not running (or not installed)."
}

# Kill any stray python processes running main.py
$stray = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*main.py*" }
if ($stray) {
    foreach ($p in $stray) {
        if ($DryRun) {
            Write-Warn "[DRY RUN] Would kill PID $($p.ProcessId): $($p.CommandLine)"
        } else {
            Stop-Process -Id $p.ProcessId -Force
            Write-Ok "Killed stray process PID $($p.ProcessId)"
        }
    }
} else {
    Write-Ok "No stray python processes found."
}

# --- 2) Clean logs and snapshots ---
Write-Step "Cleaning logs..."
$logFiles = @(
    "logs\trading.log",
    "logs\active_orders.json",
    "logs\order_ledger.jsonl",
    "logs\trades_snapshot.json",
    "logs\service_output.log",
    "logs\service_error.log"
)
foreach ($f in $logFiles) {
    $path = Join-Path $RepoPath $f
    if (Test-Path $path) {
        if ($DryRun) {
            Write-Warn "[DRY RUN] Would delete: $f"
        } else {
            Remove-Item $path -Force
            Write-Ok "Deleted $f"
        }
    }
}
Get-ChildItem -Path "$RepoPath\logs" -Filter "*.log" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($DryRun) {
        Write-Warn "[DRY RUN] Would delete: logs\$($_.Name)"
    } else {
        Remove-Item $_.FullName -Force
        Write-Ok "Deleted logs\$($_.Name)"
    }
}

Write-Step "Cleaning analysis snapshots..."
$snapshotDir = Join-Path $RepoPath "analysis\data"
if (Test-Path $snapshotDir) {
    $snapshots = Get-ChildItem -Path $snapshotDir -File | Where-Object { $_.Name -ne ".gitkeep" }
    foreach ($s in $snapshots) {
        if ($DryRun) {
            Write-Warn "[DRY RUN] Would delete: analysis\data\$($s.Name)"
        } else {
            Remove-Item $s.FullName -Force
            Write-Ok "Deleted analysis\data\$($s.Name)"
        }
    }
} else {
    Write-Warn "No analysis\data directory found."
}

# --- 3) Start via NSSM ---
Write-Step "Starting CoincallTrader service via NSSM..."
$svc = Get-Service -Name "CoincallTrader" -ErrorAction SilentlyContinue
if (-not $svc) {
    Write-Error "CoincallTrader service not installed. Register it first with nssm install."
    exit 1
}

if ($DryRun) {
    Write-Warn "[DRY RUN] Would start CoincallTrader service."
} else {
    Start-Service CoincallTrader
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name "CoincallTrader"
    if ($svc.Status -eq "Running") {
        Write-Ok "Service is running."
    } else {
        Write-Error "Service failed to start. Status: $($svc.Status)"
        Write-Host "    Check logs: Get-Content '$RepoPath\logs\service_error.log' -Tail 30" -ForegroundColor Yellow
        exit 1
    }
}

Write-Step "Done! Clean restart complete."
Write-Host "    Tail logs: Get-Content '$RepoPath\logs\trading.log' -Tail 20 -Wait" -ForegroundColor White
