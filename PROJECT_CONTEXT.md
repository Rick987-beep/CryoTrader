# CoincallTrader — Project Context & Knowledge Base

**Last Updated:** 2 March 2026  
**Maintainer:** Ulrik Deichsel

This document captures important context, decisions, and setup information for continuity when working on different machines or with AI assistants.

---

## 🎯 Project Overview

**Purpose:** Automated options trading bot for Coincall exchange  
**Language:** Python 3.9+  
**Architecture:** Tick-driven with position monitoring loop  
**Deployment Target:** Windows Server 2022 VPS (primary), also runs on macOS locally  
**Current Version:** 0.7.0 — Configurable Execution Timing

---

## 🏗️ Architecture

### Core Components

1. **main.py** — Entry point, wires everything together, runs position monitor loop
2. **strategy.py** — Strategy framework, TradingContext DI, StrategyConfig, StrategyRunner
3. **config.py** — Environment configuration (testnet/production switching)
4. **auth.py** — API authentication and request signing (with timeouts & retries)
5. **retry.py** — @retry decorator with exponential backoff
6. **market_data.py** — Market data fetching and caching (30s TTL)
7. **option_selection.py** — Option filtering, selection logic, LegSpec, find_option()
8. **trade_execution.py** — Order placement, LimitFillManager (with phased pricing), ExecutionPhase, ExecutionParams
9. **rfq.py** — Request-for-Quote handling ($50k+ notional)
10. **trade_lifecycle.py** — Trade state machine, LifecycleManager, RFQParams, exit conditions
11. **multileg_orderbook.py** — Smart chunked multi-leg execution
12. **account_manager.py** — AccountSnapshot, PositionMonitor, margin/equity queries
13. **persistence.py** — Trade state persistence (JSON snapshots for crash recovery)
14. **health_check.py** — Background health check logging (5-min intervals)

### Strategy Modules
- **strategies/blueprint_strangle.py** — Blueprint strangle strategy (starting template for new strategies)
- **strategies/reverse_iron_condor_live.py** — Reverse iron condor live trading strategy
- **strategies/long_strangle_pnl_test.py** — Long strangle PnL monitoring test
- **strategies/rfq_endurance.py** — 3-cycle RFQ endurance test strategy with UTC scheduling

---

## 🔧 Configuration

### Environment Variables (.env)

```bash
TRADING_ENVIRONMENT=testnet  # or 'production'
COINCALL_API_KEY_TEST=...
COINCALL_API_SECRET_TEST=...
COINCALL_API_KEY_PROD=...
COINCALL_API_SECRET_PROD=...
```

### Key Config Details
- Environment switching via `TRADING_ENVIRONMENT` in .env
- Testnet: https://betaapi.coincall.com
- Production: https://api.coincall.com
- See config.py for full configuration structure

---

## 💻 Development Environment

### Local (macOS)
- Python 3.9+ in virtual environment (.venv)
- Dependencies: requests, python-dotenv, websockets (see requirements.txt)
- Development and testing happens here
- VS Code with GitHub Copilot

### Production (Windows Server 2022 VPS)
- Same Python version and dependencies
- Runs as Windows Service via NSSM
- Auto-restart on failure
- Scheduled health checks and log rotation
- VS Code with GitHub Copilot for deployment/debugging

---

## 🚀 Deployment

### Deployment Method: Windows Service (NSSM)
- **Location:** C:\CoincallTrader
- **Service Name:** CoincallTrader
- **Auto-start:** Yes
- **Restart Policy:** Auto-restart on failure with 5-second delay

### Deployment Scripts (deployment/)
1. **health_check.ps1** — Service health monitoring (runs every 15 min)
2. **monitor_dashboard.ps1** — Real-time status dashboard

### Deployment Workflow
1. Develop locally on Mac
2. Test in testnet mode
3. Commit and push to Git
4. RDP to VPS → Open VS Code
5. Pull latest code
6. Restart service if needed
7. Monitor via dashboard

---

## �️ 48-Hour Reliability Features (Phase 1 & 2 Hardening)

### Phase 1: Core Resilience
- **Request Timeouts**: All API calls wrapped with 30-second timeout (`auth.py`)
- **Retry Logic**: @retry decorator with exponential backoff (1s → 2s → 4s) for transient errors only (ConnectionError, Timeout), NOT HTTP errors (`retry.py`)
- **Error Isolation**: Main loop catches per-iteration exceptions, allows up to 10 consecutive errors before exit, auto-recovery between iterations (`main.py`)

**Result:** Handles brief network glitches, API overload, temporary stalls without crashing

### Phase 2: Operational Visibility & Recovery
- **Market Data Caching**: 30-second TTL with 100-entry LRU cache on option chains and details (`market_data.py`). Reduces API load ~70% on burst queries, provides fallback if API stalls
- **Trade State Persistence**: Auto-save active trades to `logs/trade_state.json` every 60 seconds. Enables recovery if app crashes mid-position (`persistence.py`)
- **Health Check Logging**: Background thread logs account equity, margin, positions, and portfolio delta every 5 minutes to `logs/health.log` (`health_check.py`)
- **Crash Recovery**: If app crashes and restarts (even hours later), PositionMonitor immediately detects all live positions via API. `max_trades_per_day=1` prevents duplicate entries next day

**Result:** 48-hour autonomous operation with operational visibility; safe recovery if crash detected

### Configuration
All hardening is built-in and automatic — no configuration needed. See `main.py` for startup flow (persistence, health_checker initialization and start).

---

## 📊 Monitoring & Operations


### Logs
- **Application:** C:\CoincallTrader\logs\trading.log
- **Trade State:** C:\CoincallTrader\logs\trade_state.json (updated every 60s, persistence snapshots)
- **Health Check:** C:\CoincallTrader\logs\health.log (updated every 5 min, account equity/margin/positions/delta)
- **Service Output:** C:\CoincallTrader\logs\service_output.log
- **Service Errors:** C:\CoincallTrader\logs\service_error.log

### Health Checks
- Service status (must be "Running")
- Log freshness (<30 min since last write)
- Memory usage (<1GB)
- Disk space (>5GB free)
- No critical errors in recent logs

### Common Commands (VPS)
```powershell
# Service control
Start-Service CoincallTrader
Stop-Service CoincallTrader
Restart-Service CoincallTrader
Get-Service CoincallTrader

# View logs
Get-Content logs\trading.log -Tail 20 -Wait

# Monitor dashboard
.\deployment\monitor_dashboard.ps1

# Health check
.\deployment\health_check.ps1
```

---

## 🔐 Security

### Best Practices
- Never commit .env to Git
- Restrict RDP access to specific IPs
- Use strong passwords
- Keep Windows updated
- Run service as non-admin user (optional but recommended)
- File permissions: .env should only be readable by service user

### Firewall
- Only RDP (3389) inbound allowed
- All other ports blocked by default

---

## 📝 Important Decisions & Rationale

### Why Windows Server?
- User has access to powerful Windows VPS
- NSSM makes service management simple
- PowerShell scripts for automation
- RDP provides easy access

### Why NSSM over Native Windows Service?
- Much simpler to configure
- Better restart policies
- Easy logging configuration
- No need to write service wrapper code

### Why Position Monitor Loop?
- Simpler than WebSocket for MVP
- More reliable for long-running operation
- Easy to debug and monitor
- WebSockets can be added later if needed

### Why Testnet/Production Switch?
- Safe testing without risk
- Easy to switch environments
- Same code runs in both
- Prevents accidental production trades during development

---

## 🐛 Known Issues & Gotchas

### PowerShell Execution Policy
- May need: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`
- Scripts include bypass flags where appropriate

### Virtual Environment Activation
- Windows: `.\.venv\Scripts\Activate.ps1`
- macOS/Linux: `source .venv/bin/activate`

### Log File Locking
- Service must be stopped to rotate logs
- rotate_logs.ps1 handles this automatically

### Time Zones
- Important for options expiry calculations
- VPS should be set to appropriate timezone

---

## 📚 Documentation Index

### Deployment Docs
- [WINDOWS_DEPLOYMENT.md](deployment/WINDOWS_DEPLOYMENT.md) — Full deployment guide

### Project Docs
- [README.md](README.md) — Project overview
- [CHANGELOG.md](CHANGELOG.md) — Version history
- [RELEASE_NOTES.md](RELEASE_NOTES.md) — Release notes
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) — Coincall exchange API reference
- [docs/MODULE_REFERENCE.md](docs/MODULE_REFERENCE.md) — Internal module reference
- [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) — Architecture documentation

---

## 🔄 Development Workflow

1. **Plan** — Define strategy or feature
2. **Develop** — Write code on Mac locally
3. **Test** — Run in testnet mode (`TRADING_ENVIRONMENT=testnet`)
4. **Review** — Check logs, verify behavior
5. **Commit** — Git commit with descriptive message
6. **Push** — Push to GitHub
7. **Deploy** — Pull on VPS, restart service
8. **Monitor** — Watch logs and dashboard for first hour
9. **Verify** — Check after 24h, 3d, 7d for stability

---

## 🎯 Active Strategies

### blueprint_strangle
- **Status:** Default template (active in main.py)
- **Module:** strategies/blueprint_strangle.py
- **Description:** Blueprint strangle — starting point for new strategies
- **Risk Level:** Low (small qty, 0.01 BTC)

### reverse_iron_condor_live
- **Status:** Available (commented out in main.py)
- **Module:** strategies/reverse_iron_condor_live.py
- **Description:** Daily 1DTE reverse iron condor via RFQ
- **Risk Level:** Medium (0.5 BTC per leg)

### long_strangle_pnl_test
- **Status:** Test/validation tool
- **Module:** strategies/long_strangle_pnl_test.py
- **Description:** 2-hour PnL monitoring test with profit/time exits
- **Risk Level:** Low (0.01 BTC)

### rfq_endurance
- **Status:** Test/validation tool
- **Module:** strategies/rfq_endurance.py
- **Description:** 3-cycle scheduled RFQ strangle test with UTC time windows
- **Risk Level:** Low (0.5 qty, short hold times)

---

## 💡 Tips for AI Assistants (GitHub Copilot)

When working on this project:

1. **Check TRADING_ENVIRONMENT** — Ask user if testnet or production
2. **Always use virtual environment** — Activate .venv first
3. **Follow logging patterns** — Use existing logger instances
4. **Security first** — Never log API credentials
5. **Windows paths** — Remember backslash escaping on Windows
6. **Service impact** — Mention if changes require service restart
7. **Testing required** — Always test in testnet first

### Quick Context Commands
```powershell
# Show current environment
Get-Content .env | Select-String "TRADING_ENVIRONMENT"

# Check if service is running
Get-Service CoincallTrader

# See recent activity
Get-Content logs\trading.log -Tail 30
```

---

## 🆘 Emergency Procedures

### Service Won't Start
1. Check service status: `nssm status CoincallTrader`
2. View error log: `Get-Content logs\service_error.log`
3. Test manually: Stop service → activate venv → `python main.py`
4. Check for: missing dependencies, .env misconfiguration, API credential issues

### High Memory Usage
1. Check process: `Get-Process python`
2. Review logs for errors or exceptions
3. Restart service: `Restart-Service CoincallTrader`
4. If persistent: investigate memory leak

### Disk Full
1. Check space: `Get-PSDrive C`
2. Run log rotation: `.\deployment\rotate_logs.ps1`
3. Clean old archives: `Remove-Item logs\archive\* -Force`

### Lost Connectivity to Exchange
1. Check internet: `Test-NetConnection api.coincall.com -Port 443`
2. Review recent logs for API errors
3. Verify API credentials in .env
4. Restart service: `Restart-Service CoincallTrader`

---

## 📞 Contacts & Resources

### Exchange
- **Coincall Testnet:** https://beta.coincall.com/
- **Coincall Production:** https://www.coincall.com/
- **API Docs:** [Link to API documentation]
- **Support:** support@coincall.com

### Infrastructure
- **VPS Provider:** [Add provider name and support link]
- **Repository:** [Add GitHub repo link if applicable]

---

## 🔖 Quick References

### File Paths (VPS)
```
C:\CoincallTrader\          — Application root
C:\CoincallTrader\.env      — Credentials (SECRET!)
C:\CoincallTrader\logs\     — All logs
C:\CoincallTrader\deployment\  — Deployment scripts
```

### Key Environment Variables
- `TRADING_ENVIRONMENT` — testnet or production
- `COINCALL_API_KEY_TEST` — Testnet API key
- `COINCALL_API_SECRET_TEST` — Testnet API secret
- `COINCALL_API_KEY_PROD` — Production API key (CAREFUL!)
- `COINCALL_API_SECRET_PROD` — Production API secret (CAREFUL!)

---

**Note:** Keep this document updated as the project evolves. When switching machines or AI assistant contexts, read this file first for quick ramp-up.

---

**End of Context Document**
