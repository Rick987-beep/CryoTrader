# CoincallTrader — Windows Server 2022 Deployment Guide

Complete guide for deploying CoincallTrader on Windows Server 2022 for safe, continuous operation (7+ days).

---

## 📋 Prerequisites

- Windows Server 2022 with Administrator access
- Internet connection
- Your Coincall API credentials (testnet and/or production)

---

## 🔧 Step 1: Initial Server Setup & Security

### 1.1 Update Windows
```powershell
# Run Windows Update (PowerShell as Administrator)
Install-Module PSWindowsUpdate -Force
Get-WindowsUpdate
Install-WindowsUpdate -AcceptAll -AutoReboot
```

### 1.2 Configure Windows Firewall
```powershell
# Enable firewall if not already enabled
Set-NetFirewallProfile -Profile Domain,Public,Private -Enabled True

# Only allow RDP (you can restrict this further to specific IPs)
New-NetFirewallRule -DisplayName "Allow RDP" -Direction Inbound -LocalPort 3389 -Protocol TCP -Action Allow
```

### 1.3 Create Dedicated Service User (Recommended)
```powershell
# Create a user for running the service (safer than using Administrator)
$password = ConvertTo-SecureString "YourStrongPassword123!" -AsPlainText -Force
New-LocalUser -Name "coincalltrader" -Password $password -FullName "CoincallTrader Service" -Description "Service account for trading bot"
Add-LocalGroupMember -Group "Users" -Member "coincalltrader"
```

### 1.4 Disable Unnecessary Services
```powershell
# Disable services you don't need (improves security & performance)
Set-Service -Name "PrintSpooler" -StartupType Disabled
Set-Service -Name "XblAuthManager" -StartupType Disabled -ErrorAction SilentlyContinue
Set-Service -Name "XblGameSave" -StartupType Disabled -ErrorAction SilentlyContinue
```

---

## 🐍 Step 2: Install Python 3.11+

### 2.1 Download and Install Python
1. Download Python 3.11 or 3.12 from: https://www.python.org/downloads/windows/
2. Run installer with these options:
   - ✅ Add Python to PATH
   - ✅ Install for all users
   - Custom installation → ✅ pip, ✅ py launcher

### 2.2 Verify Installation
```powershell
python --version
pip --version
```

---

## 📦 Step 3: Deploy CoincallTrader

### 3.1 Choose Deployment Directory
```powershell
# Create deployment directory
New-Item -ItemType Directory -Path "C:\CoincallTrader" -Force
cd C:\CoincallTrader
```

### 3.2 Transfer Files
**Option A: Git Clone (if you have a repository)**
```powershell
# Install Git first: https://git-scm.com/download/win
git clone https://github.com/yourusername/CoincallTrader.git .
```

**Option B: Manual Transfer**
1. Use RDP file transfer or SFTP client (WinSCP, FileZilla)
2. Copy all files from your local machine to `C:\CoincallTrader`

### 3.3 Create Python Virtual Environment
```powershell
cd C:\CoincallTrader
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# If you get execution policy error:
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### 3.4 Install Dependencies
```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

### 3.5 Configure Environment Variables
```powershell
# Copy example to .env
Copy-Item .env.example .env

# Edit .env with your credentials
notepad .env
```

**In `.env` file, set:**
```
TRADING_ENVIRONMENT=production  # or testnet for testing
COINCALL_API_KEY_PROD=your_actual_api_key
COINCALL_API_SECRET_PROD=your_actual_secret
```

### 3.6 Test Run
```powershell
# Test the application manually first
python main.py

# Press Ctrl+C to stop after verifying it works
```

---

## 🔄 Step 4: Set Up as Windows Service

We'll use NSSM (Non-Sucking Service Manager) to run CoincallTrader as a service.

### 4.1 Install NSSM
```powershell
# Download NSSM from: https://nssm.cc/download
# Or use Chocolatey:
choco install nssm -y

# Verify installation
nssm --version
```

### 4.2 Create the Service
```powershell
# Run deployment script (provided below)
# Or manually configure:

nssm install CoincallTrader "C:\CoincallTrader\.venv\Scripts\python.exe" "C:\CoincallTrader\main.py"
nssm set CoincallTrader AppDirectory "C:\CoincallTrader"
nssm set CoincallTrader DisplayName "CoincallTrader Bot"
nssm set CoincallTrader Description "Automated options trading bot for Coincall"
nssm set CoincallTrader Start SERVICE_AUTO_START

# Set logging
nssm set CoincallTrader AppStdout "C:\CoincallTrader\logs\service_output.log"
nssm set CoincallTrader AppStderr "C:\CoincallTrader\logs\service_error.log"

# Set restart policy (auto-restart on failure)
nssm set CoincallTrader AppExit Default Restart
nssm set CoincallTrader AppRestartDelay 5000
nssm set CoincallTrader AppThrottle 10000
```

### 4.3 Start the Service
```powershell
# Start the service
Start-Service CoincallTrader

# Check status
Get-Service CoincallTrader

# View logs
Get-Content -Path "C:\CoincallTrader\logs\trading.log" -Tail 20 -Wait
```

---

## 📊 Step 5: Monitoring & Maintenance

### 5.1 Log Rotation Setup
```powershell
# Create log rotation script (see rotate_logs.ps1 below)
# Schedule it with Task Scheduler to run daily
```

### 5.2 Health Check Script
```powershell
# See health_check.ps1 below
# Schedule to run every 15 minutes
```

### 5.3 Configure Windows Task Scheduler for Monitoring
1. Open Task Scheduler
2. Create Basic Task:
   - Name: "CoincallTrader Health Check"
   - Trigger: Daily, repeat every 15 minutes
   - Action: Start a program → `powershell.exe`
   - Arguments: `-File C:\CoincallTrader\deployment\health_check.ps1`

---

## 🛡️ Step 6: Security Hardening

### 6.1 Restrict File Permissions
```powershell
# Only allow service user to access .env and logs
icacls "C:\CoincallTrader\.env" /inheritance:r
icacls "C:\CoincallTrader\.env" /grant:r "coincalltrader:(R)"
icacls "C:\CoincallTrader\.env" /grant:r "Administrators:(F)"
```

### 6.2 Enable Automatic Security Updates
1. Go to Settings → Windows Update
2. Enable automatic updates
3. Set active hours to avoid restarts during trading hours

### 6.3 Configure Auto-Restart After Updates
```powershell
# Register a scheduled task to restart service after updates
# This ensures the bot resumes after Windows updates
```

---

## 🚨 Step 7: Troubleshooting

### Service Won't Start
```powershell
# Check service status
nssm status CoincallTrader

# View service logs
Get-Content "C:\CoincallTrader\logs\service_error.log"

# Manually test
cd C:\CoincallTrader
.\.venv\Scripts\Activate.ps1
python main.py
```

### Check if Service is Running
```powershell
Get-Service CoincallTrader
Get-Process -Name python

# View recent logs
Get-Content -Path "C:\CoincallTrader\logs\trading.log" -Tail 50
```

### Restart Service
```powershell
Restart-Service CoincallTrader
```

### Stop Service
```powershell
Stop-Service CoincallTrader
```

---

## 📁 Directory Structure After Deployment

```
C:\CoincallTrader\
├── .venv\                      # Python virtual environment
├── .env                        # Your credentials (KEEP SECRET!)
├── logs\
│   ├── trading.log            # Application logs
│   ├── service_output.log     # Service stdout
│   └── service_error.log      # Service stderr
├── deployment\
│   ├── setup.ps1              # Automated setup script
│   ├── health_check.ps1       # Health monitoring
│   └── rotate_logs.ps1        # Log rotation
├── main.py                     # Entry point
├── config.py                   # Configuration
└── [other application files]
```

---

## ✅ Post-Deployment Checklist

- [ ] Python 3.11+ installed
- [ ] All dependencies installed in virtual environment
- [ ] `.env` file configured with correct credentials
- [ ] Application tested manually (python main.py)
- [ ] NSSM service installed and configured
- [ ] Service set to auto-start
- [ ] Service running successfully
- [ ] Logs are being written
- [ ] Health check scheduled in Task Scheduler
- [ ] Log rotation scheduled
- [ ] Firewall configured
- [ ] File permissions restricted
- [ ] Automatic updates enabled
- [ ] Backup strategy in place

---

## 🔄 Updating the Application

```powershell
# Stop the service
Stop-Service CoincallTrader

# Update code (git pull or manual file transfer)
cd C:\CoincallTrader
git pull  # or transfer new files

# Activate venv and update dependencies
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt --upgrade

# Restart service
Start-Service CoincallTrader

# Verify
Get-Service CoincallTrader
Get-Content -Path "C:\CoincallTrader\logs\trading.log" -Tail 20
```

---

## 📞 Support

For issues, check:
1. Service status: `Get-Service CoincallTrader`
2. Application logs: `C:\CoincallTrader\logs\trading.log`
3. Service logs: `C:\CoincallTrader\logs\service_error.log`
4. Windows Event Viewer: Application logs

---

**Last Updated:** February 2026
