# CoincallTrader Live Monitoring Dashboard
# Displays real-time status in the console
# Run this manually to see current status

param(
    [string]$ServiceName = "CoincallTrader",
    [string]$LogPath = "C:\CoincallTrader\logs\trading.log",
    [int]$RefreshSeconds = 5,
    [switch]$OneShot  # Run once instead of continuous loop
)

function Get-ServiceStatus {
    $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($service) {
        return @{
            Exists = $true
            Status = $service.Status
            StartType = $service.StartType
        }
    } else {
        return @{
            Exists = $false
            Status = "Not Installed"
            StartType = "N/A"
        }
    }
}

function Get-ProcessInfo {
    $processes = Get-Process -Name python -ErrorAction SilentlyContinue
    if ($processes) {
        $info = @()
        foreach ($proc in $processes) {
            $info += @{
                PID = $proc.Id
                CPU = [math]::Round($proc.CPU, 2)
                MemoryMB = [math]::Round($proc.WorkingSet64 / 1MB, 2)
                StartTime = $proc.StartTime
            }
        }
        return $info
    }
    return $null
}

function Get-LogInfo {
    if (Test-Path $LogPath) {
        $logFile = Get-Item $LogPath
        $lastLines = Get-Content $LogPath -Tail 5 -ErrorAction SilentlyContinue
        return @{
            Exists = $true
            SizeMB = [math]::Round($logFile.Length / 1MB, 2)
            LastModified = $logFile.LastWriteTime
            MinutesSinceUpdate = [math]::Round(((Get-Date) - $logFile.LastWriteTime).TotalMinutes, 1)
            RecentLines = $lastLines
        }
    } else {
        return @{
            Exists = $false
        }
    }
}

function Get-DiskInfo {
    $drive = Get-PSDrive -Name C -ErrorAction SilentlyContinue
    if ($drive) {
        return @{
            FreeGB = [math]::Round($drive.Free / 1GB, 2)
            UsedGB = [math]::Round($drive.Used / 1GB, 2)
            TotalGB = [math]::Round(($drive.Free + $drive.Used) / 1GB, 2)
            PercentFree = [math]::Round(($drive.Free / ($drive.Free + $drive.Used)) * 100, 1)
        }
    }
    return $null
}

function Show-Dashboard {
    Clear-Host
    
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    
    Write-Host "=" * 80 -ForegroundColor Cyan
    Write-Host "CoincallTrader — Live Monitoring Dashboard" -ForegroundColor Cyan
    Write-Host "=" * 80 -ForegroundColor Cyan
    Write-Host "Last Updated: $timestamp" -ForegroundColor Gray
    Write-Host ""
    
    # Service Status
    Write-Host "SERVICE STATUS" -ForegroundColor Yellow
    Write-Host ("-" * 80) -ForegroundColor Gray
    $serviceStatus = Get-ServiceStatus
    
    if ($serviceStatus.Exists) {
        $statusColor = if ($serviceStatus.Status -eq "Running") { "Green" } else { "Red" }
        Write-Host "  Service Name:  " -NoNewline
        Write-Host $ServiceName -ForegroundColor White
        Write-Host "  Status:        " -NoNewline
        Write-Host $serviceStatus.Status -ForegroundColor $statusColor
        Write-Host "  Startup Type:  " -NoNewline
        Write-Host $serviceStatus.StartType -ForegroundColor White
    } else {
        Write-Host "  Status: " -NoNewline
        Write-Host "Service Not Installed" -ForegroundColor Red
    }
    Write-Host ""
    
    # Process Information
    Write-Host "PROCESS INFORMATION" -ForegroundColor Yellow
    Write-Host ("-" * 80) -ForegroundColor Gray
    $processes = Get-ProcessInfo
    
    if ($processes) {
        foreach ($proc in $processes) {
            Write-Host "  PID:           " -NoNewline
            Write-Host $proc.PID -ForegroundColor White
            Write-Host "  CPU Time:      " -NoNewline
            Write-Host "$($proc.CPU)s" -ForegroundColor White
            Write-Host "  Memory:        " -NoNewline
            $memColor = if ($proc.MemoryMB -gt 500) { "Yellow" } elseif ($proc.MemoryMB -gt 1000) { "Red" } else { "Green" }
            Write-Host "$($proc.MemoryMB) MB" -ForegroundColor $memColor
            if ($proc.StartTime) {
                $uptime = (Get-Date) - $proc.StartTime
                Write-Host "  Uptime:        " -NoNewline
                Write-Host "$([math]::Floor($uptime.TotalHours))h $($uptime.Minutes)m" -ForegroundColor White
            }
            Write-Host ""
        }
    } else {
        Write-Host "  No Python processes running" -ForegroundColor Red
        Write-Host ""
    }
    
    # Log File Status
    Write-Host "LOG FILE STATUS" -ForegroundColor Yellow
    Write-Host ("-" * 80) -ForegroundColor Gray
    $logInfo = Get-LogInfo
    
    if ($logInfo.Exists) {
        Write-Host "  Path:          " -NoNewline
        Write-Host $LogPath -ForegroundColor White
        Write-Host "  Size:          " -NoNewline
        Write-Host "$($logInfo.SizeMB) MB" -ForegroundColor White
        Write-Host "  Last Updated:  " -NoNewline
        $ageColor = if ($logInfo.MinutesSinceUpdate -gt 30) { "Red" } elseif ($logInfo.MinutesSinceUpdate -gt 10) { "Yellow" } else { "Green" }
        Write-Host "$($logInfo.MinutesSinceUpdate) minutes ago" -ForegroundColor $ageColor
        Write-Host ""
        Write-Host "  Recent Entries:" -ForegroundColor Cyan
        foreach ($line in $logInfo.RecentLines) {
            $lineColor = "Gray"
            if ($line -match "ERROR|CRITICAL") { $lineColor = "Red" }
            elseif ($line -match "WARNING") { $lineColor = "Yellow" }
            elseif ($line -match "INFO") { $lineColor = "White" }
            Write-Host "    $line" -ForegroundColor $lineColor
        }
    } else {
        Write-Host "  Log file not found: $LogPath" -ForegroundColor Red
    }
    Write-Host ""
    
    # Disk Space
    Write-Host "DISK SPACE" -ForegroundColor Yellow
    Write-Host ("-" * 80) -ForegroundColor Gray
    $diskInfo = Get-DiskInfo
    
    if ($diskInfo) {
        Write-Host "  Drive C:" -ForegroundColor White
        Write-Host "  Free:          " -NoNewline
        $freeColor = if ($diskInfo.FreeGB -lt 5) { "Red" } elseif ($diskInfo.FreeGB -lt 10) { "Yellow" } else { "Green" }
        Write-Host "$($diskInfo.FreeGB) GB / $($diskInfo.TotalGB) GB ($($diskInfo.PercentFree)% free)" -ForegroundColor $freeColor
    }
    Write-Host ""
    
    # Quick Actions
    Write-Host "=" * 80 -ForegroundColor Cyan
    if (-not $OneShot) {
        Write-Host "Refreshing every $RefreshSeconds seconds... Press Ctrl+C to stop" -ForegroundColor Gray
    }
    Write-Host "=" * 80 -ForegroundColor Cyan
}

# Main Loop
if ($OneShot) {
    Show-Dashboard
} else {
    while ($true) {
        Show-Dashboard
        Start-Sleep -Seconds $RefreshSeconds
    }
}
