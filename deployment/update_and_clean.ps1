# ============================================================================
# CoincallTrader — Pull Latest & Clean Logs/Snapshots
# Run as Administrator in PowerShell on the production machine
# ============================================================================

param(
    [string]$RepoPath = "C:\CoincallTrader",
    [switch]$SkipServiceRestart,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

# --- Validate ---
if (-not (Test-Path "$RepoPath\.git")) {
    Write-Error "Not a git repo: $RepoPath"
    exit 1
}

Set-Location $RepoPath

# --- Stop service (if running) ---
if (-not $SkipServiceRestart) {
    Write-Step "Stopping CoincallTrader service..."
    $svc = Get-Service -Name "CoincallTrader" -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        Stop-Service CoincallTrader -Force
        Start-Sleep -Seconds 3
        Write-Ok "Service stopped."
    } else {
        Write-Warn "Service not running (or not found). Continuing..."
    }
}

# --- Pull latest from GitHub (force-overwrite local changes) ---
Write-Step "Pulling latest code from GitHub..."
if ($DryRun) {
    Write-Warn "[DRY RUN] Would run: git fetch --all && git reset --hard origin/main"
} else {
    git fetch --all
    if ($LASTEXITCODE -ne 0) { Write-Error "git fetch failed"; exit 1 }

    git reset --hard origin/main
    if ($LASTEXITCODE -ne 0) { Write-Error "git reset failed"; exit 1 }

    $hash = git rev-parse --short HEAD
    Write-Ok "Now at commit $hash"
}

# --- Clean logs ---
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
# Also catch any other .log files in logs/
Get-ChildItem -Path "$RepoPath\logs" -Filter "*.log" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($DryRun) {
        Write-Warn "[DRY RUN] Would delete: logs\$($_.Name)"
    } else {
        Remove-Item $_.FullName -Force
        Write-Ok "Deleted logs\$($_.Name)"
    }
}

# --- Clean analysis snapshots ---
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

# --- Update dependencies ---
Write-Step "Updating pip dependencies..."
$venvPip = Join-Path $RepoPath ".venv\Scripts\pip.exe"
if (Test-Path $venvPip) {
    if ($DryRun) {
        Write-Warn "[DRY RUN] Would run: pip install -r requirements.txt"
    } else {
        & $venvPip install --quiet --upgrade pip
        & $venvPip install --quiet -r "$RepoPath\requirements.txt"
        Write-Ok "Dependencies up to date."
    }
} else {
    Write-Warn "Virtual environment not found at .venv — skipping pip install."
}

# --- Restart service ---
if (-not $SkipServiceRestart) {
    Write-Step "Starting CoincallTrader service..."
    $svc = Get-Service -Name "CoincallTrader" -ErrorAction SilentlyContinue
    if ($svc) {
        if ($DryRun) {
            Write-Warn "[DRY RUN] Would start service."
        } else {
            Start-Service CoincallTrader
            Start-Sleep -Seconds 2
            $svc = Get-Service -Name "CoincallTrader"
            Write-Ok "Service status: $($svc.Status)"
        }
    } else {
        Write-Warn "CoincallTrader service not found. Start manually: python main.py"
    }
}

Write-Step "Done!"
if (-not $DryRun) {
    $hash = git rev-parse --short HEAD
    $date = git log -1 --format="%ci"
    Write-Host "`n    Commit : $hash" -ForegroundColor White
    Write-Host "    Date   : $date" -ForegroundColor White
}
