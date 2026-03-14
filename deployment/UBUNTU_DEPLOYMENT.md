# CoincallTrader — Ubuntu Deployment Guide

One-button deployment from macOS to an Ubuntu 24.04 VPS via rsync + systemd.

Replaces the previous Windows Server + NSSM setup (still available in
`WINDOWS_DEPLOYMENT.md`, `clean_restart.ps1`, etc.).

---

## Architecture

```
┌─────────────────────┐          rsync + SSH          ┌──────────────────────┐
│   Dev Machine (Mac)  │  ─────────────────────────▶  │   VPS (Ubuntu 24.04) │
│                      │                               │                      │
│  VS Code + .venv     │     ./deploy.sh               │  /opt/coincalltrader  │
│  Edit code locally   │     (stop → sync → start)     │  systemd service     │
│                      │                               │  journalctl logs     │
└─────────────────────┘                               └──────────────────────┘
```

**No git on the server.** Code is synced directly via rsync over SSH.

---

## Prerequisites

- macOS with SSH key (`~/.ssh/id_ed25519`)
- Hetzner VPS (or any Ubuntu 24.04 server) with your SSH key installed
- `.deploy.env` configured in the project root (see below)

---

## Quick Start (3 commands)

```bash
# 1. Prepare the VPS (one-time)
./deployment/deploy.sh --setup

# 2. Copy your .env credentials to the VPS (one-time, or when credentials change)
./deployment/deploy.sh --env

# 3. Deploy & start
./deployment/deploy.sh
```

That's it. The bot is running.

---

## Files

| File | Purpose |
|---|---|
| `deployment/deploy.sh` | Main deploy script — run from your Mac |
| `deployment/server-setup.sh` | One-time VPS setup (Python, venv, systemd, firewall) |
| `deployment/coincalltrader.service` | systemd unit file (installed automatically) |
| `deployment/rsync-exclude.txt` | Files/dirs excluded from sync |
| `.deploy.env` | Your VPS connection settings (gitignored) |

---

## .deploy.env Configuration

Create `.deploy.env` in the project root:

```bash
# SSH target
VPS_HOST=root@46.225.137.92

# Application directory on the VPS
VPS_APP_DIR=/opt/coincalltrader

# systemd service name
VPS_SERVICE=coincalltrader

# (Optional) SSH key path — leave empty for default
SSH_KEY=
```

This file is gitignored and stays on your dev machine only.

---

## Deploy Script Commands

| Command | What it does |
|---|---|
| `./deployment/deploy.sh` | **Full deploy**: stop → sync code → install deps → start |
| `./deployment/deploy.sh --dry-run` | Preview what would be synced (no changes) |
| `./deployment/deploy.sh --setup` | One-time server setup |
| `./deployment/deploy.sh --env` | Copy `.env` to the VPS |
| `./deployment/deploy.sh --stop` | Stop the service |
| `./deployment/deploy.sh --start` | Start the service |
| `./deployment/deploy.sh --restart` | Restart the service |
| `./deployment/deploy.sh --clean` | **Clean restart**: delete all logs/snapshots, then start fresh |
| `./deployment/deploy.sh --status` | Show service status + uptime |
| `./deployment/deploy.sh --logs` | Tail live logs (Ctrl+C to stop) |
| `./deployment/deploy.sh --health` | Quick health check (disk, memory, uptime, service) |
| `./deployment/deploy.sh --update` | Update OS packages on the VPS |
| `./deployment/deploy.sh --reboot` | Reboot VPS, wait for it, verify service |
| `./deployment/deploy.sh --ssh` | Open SSH session to VPS |

---

## What Gets Synced

rsync transfers only application code. These are **excluded** (see `rsync-exclude.txt`):

- `.venv/` — the VPS has its own venv
- `.env`, `.deploy.env` — secrets stay separate
- `logs/` — preserved on the VPS across deploys
- `archive/`, `analysis/`, `docs/`, `tests/` — dev-only
- `deployment/` — except the service file which is copied explicitly
- `.git/`, `__pycache__/`, IDE files

---

## systemd Service

The bot runs as a systemd service called `coincalltrader`.

```bash
# These all work from your Mac via deploy.sh:
./deployment/deploy.sh --status
./deployment/deploy.sh --logs
./deployment/deploy.sh --stop

# Or directly on the VPS:
sudo systemctl status coincalltrader
sudo journalctl -u coincalltrader -f
sudo systemctl stop coincalltrader
```

### Crash recovery

systemd automatically restarts the service on crash (after a 10-second delay).
This replaces NSSM's restart behaviour. Clean stops (SIGTERM, `--stop`) do not
trigger a restart.

### Logs

All stdout/stderr goes to journald. No separate log file management needed.

```bash
# Last 100 lines
sudo journalctl -u coincalltrader -n 100 --no-pager

# Since last boot
sudo journalctl -u coincalltrader -b

# Since a specific time
sudo journalctl -u coincalltrader --since "2026-03-14 10:00"
```

---

## Clean Restart

When the application has stale state (corrupted snapshots, leftover orders from
a previous session), use `--clean` to wipe everything and start fresh:

```bash
./deployment/deploy.sh --clean
```

This deletes all files in `logs/` on the VPS:
- `trading.log` — application log
- `trades_snapshot.json` — active trade state (crash recovery)
- `active_orders.json` — order ledger snapshot (crash recovery)
- `order_ledger.jsonl` — order audit trail
- `trade_history.jsonl` — completed trade history
- `*.corrupt.*` — quarantined corrupt files

Then restarts the service with a clean slate.

---

## Server Maintenance

All maintenance is done from your Mac — no need to SSH in.

### OS updates
```bash
./deployment/deploy.sh --update     # apt update + upgrade
```
If a reboot is needed (e.g. kernel update), it will tell you.

### Rebooting
```bash
./deployment/deploy.sh --reboot     # reboots, waits, verifies service
```
Waits up to 2 minutes for the VPS to come back, then checks the service.

### Health check
```bash
./deployment/deploy.sh --health     # uptime, disk, memory, service, logs
```

---

## Typical Daily Workflow

1. Edit code on your Mac in VS Code
2. `./deployment/deploy.sh` — deploys in ~5 seconds
3. `./deployment/deploy.sh --logs` — watch it run
4. `./deployment/deploy.sh --stop` — when done trading

---

## Server Details

| Property | Value |
|---|---|
| Provider | Hetzner |
| Plan | CPX22 (2 vCPU, 4 GB RAM, 80 GB SSD) |
| Location | Nuremberg, Germany (eu-central) |
| OS | Ubuntu 24.04 LTS |
| IP | 46.225.137.92 |
| App directory | /opt/coincalltrader |
| Python | 3.12.3 (system) |
| Firewall | UFW — SSH (22) + Dashboard (8080) |

---

## Updating .env on the Server

When you change API keys or switch between testnet/production:

```bash
# Edit .env locally, then:
./deployment/deploy.sh --env
./deployment/deploy.sh --restart
```

---

## Troubleshooting

**Can't connect to VPS:**
```bash
ssh -v root@46.225.137.92   # verbose SSH for debugging
```

**Service won't start:**
```bash
./deployment/deploy.sh --logs   # check error output
./deployment/deploy.sh --ssh    # SSH in and inspect manually
```

**Need to start fresh (clean logs + state):**
```bash
./deployment/deploy.sh --ssh
# Then on the VPS:
sudo systemctl stop coincalltrader
rm -f /opt/coincalltrader/logs/*
sudo systemctl start coincalltrader
```
