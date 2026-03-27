# CoincallTrader — Ubuntu Deployment Guide

## Philosophy: Single Source of Truth

Everything lives on your dev machine — code, `.env`, strategy config, API keys.
The deploy script rsyncs it all to the server in one step.  The only server-side
patch is `DEPLOYMENT_TARGET`, which is automatically set to `production` after
each sync.

**No git on the server.** Code is synced directly via rsync over SSH.

---

## Architecture

```
┌──────────────────────────┐                     ┌──────────────────────────────────┐
│   Dev Machine (Mac)       │  deploy-slot.sh    │   VPS (Ubuntu 24.04)             │
│                           │  ─────────────▶    │                                  │
│  accounts.toml            │                    │   /opt/ct/                       │
│  slots/slot-01.toml       │  auto-generates    │   ├── slot-01/  (strategy A)     │
│  slots/slot-02.toml       │  .env.slot-XX      │   ├── slot-02/  (strategy B)     │
│  .env  (secrets vault)    │  from .toml + .env │   ├── hub/      (dashboard)      │
│  .deploy.slots.env        │                    │   └── recorder/ (tick data)      │
│  .env.hub                 │                    │                                  │
│  .env.recorder            │                    │                                  │
└──────────────────────────┘                    └──────────────────────────────────┘
```

Each slot is fully isolated: own `.env`, own venv, own systemd service, own logs.
The hub dashboard auto-discovers slots and aggregates their data.

---

## Quick Start

```bash
# 1. One-time: setup slot + hub + recorder on the VPS
./deployment/deploy-slot.sh 01 --setup
./deployment/deploy-slot.sh hub --setup
./deployment/deploy-slot.sh recorder --setup

# 2. Deploy
./deployment/deploy-slot.sh 01
./deployment/deploy-slot.sh hub
./deployment/deploy-slot.sh recorder
```

---

## Configuration Files (Dev Machine)

### Slot Config System (TOML-based)

Slot configuration uses TOML files that are checked into git. Secrets stay in `.env`.

| File | In Git? | Purpose |
|---|---|---|
| `accounts.toml` | ✓ | Named account registry (maps friendly names → env var prefixes) |
| `slots/slot-XX.toml` | ✓ | Per-slot config: strategy, account, param overrides |
| `slot_config.py` | ✓ | TOML → `.env.slot-XX` generator |
| `.env` | ✗ | Secrets vault (API keys, tokens) — never deployed |
| `.deploy.slots.env` | ✗ | SSH connection (`VPS_HOST`, `SSH_KEY`) |
| `.env.slot-XX` | ✗ | Auto-generated from TOML + `.env` — deployed to server |
| `.env.hub` | ✗ | Hub dashboard config (`HUB_PASSWORD`, `HUB_PORT`) |

### accounts.toml

Maps friendly account names to env var prefixes. No secrets stored here.

```toml
[coincall-main]
exchange = "coincall"
api_key  = "COINCALL_API_KEY_PROD"
api_secret = "COINCALL_API_SECRET_PROD"

[deribit-main]
exchange = "deribit"
client_id     = "DERIBIT_CLIENT_ID_PROD"
client_secret = "DERIBIT_CLIENT_SECRET_PROD"
```

### slots/slot-XX.toml

```toml
slot_name = "Daily Put Sell"
account   = "coincall-main"     # references accounts.toml
strategy  = "daily_put_sell"    # module name in strategies/

environment    = "production"
dashboard_port = 8091
dashboard_mode = "control"

[params]                         # strategy-specific parameter overrides
QTY = 0.8
TARGET_DELTA = -0.10
```

The deploy script auto-generates `.env.slot-XX` from the TOML files + secrets in `.env`.
You never need to hand-edit `.env.slot-XX` files.

### .deploy.slots.env

```bash
VPS_HOST=root@46.225.137.92
SSH_KEY=                          # optional, uses default SSH key
```

### .env.hub

```bash
HUB_PASSWORD=...
HUB_PORT=8070
HUB_SLOTS_BASE=/opt/ct
```

### .env.recorder

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
RECORDER_DATA_DIR=/opt/ct/recorder/data
```

The recorder shares Telegram credentials with the trading slots. `RECORDER_DATA_DIR` sets where
parquet files are written on the server. Additional `RECORDER_*` overrides are available — see
`backtester2/tickrecorder/config.py` for the full list.

---

## Port Layout

| Service | Port | Scope |
|---|---|---|
| Hub dashboard | `HUB_PORT` in `.env.hub` (default 8070) | External (firewall) |
| Slot control endpoints | `DASHBOARD_PORT` in `.env.slot-XX` (8091, 8092, ...) | Localhost only |
| Recorder health | `8090` (fixed, not configurable) | Localhost only |

---

## Deploy Commands

```bash
# One script, slot number as parameter:
./deployment/deploy-slot.sh 01 --setup    # One-time: create dir, venv, systemd
./deployment/deploy-slot.sh 01            # Deploy: stop → sync → deps → start
./deployment/deploy-slot.sh 01 --logs     # Tail live logs
./deployment/deploy-slot.sh 01 --status   # Service status
./deployment/deploy-slot.sh 01 --restart  # Restart without redeploy
./deployment/deploy-slot.sh 01 --clean    # Wipe logs/state, restart
./deployment/deploy-slot.sh 01 --destroy  # Delete entire slot

# Hub:
./deployment/deploy-slot.sh hub --setup   # One-time: dir, venv, systemd, firewall
./deployment/deploy-slot.sh hub           # Deploy hub code
./deployment/deploy-slot.sh hub --logs    # Tail hub logs
./deployment/deploy-slot.sh hub --status  # Hub service status

# Recorder (tick data):
./deployment/deploy-slot.sh recorder --setup    # One-time: dirs, venv, systemd
./deployment/deploy-slot.sh recorder            # Deploy + start (timing window check applied)
./deployment/deploy-slot.sh recorder --logs     # Tail live logs
./deployment/deploy-slot.sh recorder --status   # Service status
./deployment/deploy-slot.sh recorder --restart  # Restart without redeploy
./deployment/deploy-slot.sh recorder --stop     # Stop the recorder
./deployment/deploy-slot.sh recorder --start    # Start the recorder

# Overview:
./deployment/deploy-slot.sh status        # All slots + hub + recorder at a glance
```

---

## What Happens During a Deploy

1. **Generate `.env`** — if `slots/slot-XX.toml` exists, runs `slot_config.py` to generate `.env.slot-XX` from TOML + secrets in `.env`
2. **Check connectivity** — verify SSH to VPS works
3. **Stop service** — graceful systemd stop
4. **Rsync code** — sync Python code, templates, strategies (excludes `.venv`, `.env`, logs, `slots/`, `accounts.toml`, `slot_config.py`)
5. **Copy `.env`** — `.env.slot-XX` → `/opt/ct/slot-XX/.env`
6. **Patch `.env`** — `DEPLOYMENT_TARGET=production` via `sed` on server
7. **Install deps** — `pip install -r requirements.txt` in server venv
8. **Start service** — start + verify it's running
9. **Show logs** — last 15 lines for verification

---

## Deployment Files

| File | Purpose |
|---|---|
| `deployment/deploy-slot.sh` | Single deploy script for all slots + hub + recorder |
| `deployment/ct-slot@.service` | systemd template unit (slot-01, slot-02, ...) |
| `deployment/ct-hub.service` | systemd unit for the hub dashboard |
| `deployment/ct-recorder.service` | systemd unit for the tick recorder |
| `deployment/rsync-exclude-slot.txt` | Files excluded from slot sync |
| `deployment/rsync-exclude-recorder.txt` | Files excluded from recorder sync |
| `deployment/server-setup-slots.sh` | One-time server base setup |
| `deployment/UBUNTU_DEPLOYMENT.md` | This document |
| `accounts.toml` | Named account registry (git-tracked) |
| `slots/slot-XX.toml` | Per-slot config (git-tracked) |
| `slot_config.py` | TOML → .env generator |

---

## Tick Recorder (Deribit BTC Options Data)

The recorder is an independent service (`ct-recorder`) that captures Deribit BTC options tick data
in 5-minute snapshots. It runs at `/opt/ct/recorder/` alongside the trading slots and is shown
as a health card in the hub dashboard.

### What it does

- Connects to Deribit via WebSocket and discovers all active BTC option instruments (~968)
- Every 5 minutes: subscribes to all ticker channels for a 10-second burst window, captures a
  full chain snapshot, then unsubscribes immediately (burst-mode keeps bandwidth to ~860 MB/day)
- Writes one row per instrument per snapshot to a daily zstd-compressed parquet file
- Tracks BTC/USD spot index as 1-minute OHLC in a separate parquet file
- Exposes a health endpoint at `localhost:8090/health` — polled by the hub dashboard
- Sends Telegram alerts on startup, shutdown, disconnection, low disk, and data gaps

### Output files

Written to `/opt/ct/recorder/data/` (configured via `RECORDER_DATA_DIR` in `.env.recorder`):

- `options_YYYY-MM-DD.parquet` — full BTC option chain, one row per instrument per 5-min snapshot
- `spot_track_YYYY-MM-DD.parquet` — 1-min BTC index OHLC

### Setup

```bash
# 1. Create .env.recorder
cp /dev/null .env.recorder
# Add: TELEGRAM_BOT_TOKEN=...
# Add: TELEGRAM_CHAT_ID=...
# Add: RECORDER_DATA_DIR=/opt/ct/recorder/data

# 2. One-time server setup
./deployment/deploy-slot.sh recorder --setup

# 3. Deploy
./deployment/deploy-slot.sh recorder
```

### Deploy timing safety

The deploy script enforces a **2-minute timing window**: it refuses to deploy within 2 minutes
of the next 5-minute snapshot boundary. This protects the subscription window (which opens 10
seconds before the boundary). If you hit this guard, wait until after the boundary and retry:

```bash
# Error: "Too close to next snapshot boundary (HH:MM UTC, Xs away) — retry after HH:MM"
# Just wait for the boundary to pass, then re-run:
./deployment/deploy-slot.sh recorder
```

---

## Adding a New Strategy Slot

1. Create `slots/slot-XX.toml` — set strategy, account, port, and any param overrides
2. `./deployment/deploy-slot.sh XX --setup` (creates dir, venv, systemd on VPS)
3. `./deployment/deploy-slot.sh XX` (generates `.env`, deploys code, starts)
4. Hub auto-discovers the new slot on next page load

### Tweaking Strategy Parameters

Edit the `[params]` section in `slots/slot-XX.toml`, then redeploy:

```bash
vim slots/slot-03.toml       # change QTY, deltas, etc.
./deployment/deploy-slot.sh 03
```

The deploy script regenerates `.env.slot-03` automatically.

### Dry Run (Preview Generated .env)

```bash
python3 slot_config.py 03 --dry   # shows what .env.slot-03 would contain
```

---

## systemd Services

Each slot runs as an instance of the `ct-slot@` template:

```bash
# From dev machine:
./deployment/deploy-slot.sh 01 --status
./deployment/deploy-slot.sh 01 --logs
./deployment/deploy-slot.sh hub --status
./deployment/deploy-slot.sh hub --logs
./deployment/deploy-slot.sh recorder --status
./deployment/deploy-slot.sh recorder --logs

# Or directly on the VPS:
sudo systemctl status ct-slot@01
sudo journalctl -u ct-slot@01 -f
sudo systemctl status ct-hub
sudo journalctl -u ct-hub -f
sudo systemctl status ct-recorder
sudo journalctl -u ct-recorder -f
```

### Crash recovery

- systemd auto-restarts on failure after 10 seconds
- Services are enabled, start automatically on reboot

### Logs

All stdout/stderr goes to journald:

```bash
sudo journalctl -u ct-slot@01 -n 100 --no-pager   # last 100 lines
sudo journalctl -u ct-slot@01 -b                    # since last boot
sudo journalctl -u ct-slot@01 --since "1 hour ago"  # time-based
```

---

## Server Details

| Property | Value |
|---|---|
| Provider | Hetzner |
| Plan | CPX22 (2 vCPU, 4 GB RAM, 80 GB SSD) |
| Location | Nuremberg, Germany |
| OS | Ubuntu 24.04 LTS |
| IP | 46.225.137.92 |
| Base directory | /opt/ct/ |
| Hub dashboard | http://46.225.137.92:8070 |
| Firewall | UFW — SSH (22) + Hub (8070) |

---

## Troubleshooting

**Can't connect to VPS:**
```bash
ssh -v root@46.225.137.92
```

**Slot won't start:**
```bash
./deployment/deploy-slot.sh 01 --logs
```

**Stale state blocking startup:**
```bash
./deployment/deploy-slot.sh 01 --clean
```

**Check all services at once:**
```bash
./deployment/deploy-slot.sh status
```

**Recorder won't deploy (timing safety error):**
```bash
# "Too close to next snapshot boundary" — wait until after the :00/:05/:10/... minute mark
./deployment/deploy-slot.sh recorder --status   # check if it's still running
# then retry once the boundary passes
```

**Recorder stopped capturing data:**
```bash
./deployment/deploy-slot.sh recorder --logs     # look for WebSocket errors or data gaps
./deployment/deploy-slot.sh recorder --restart  # restart if stuck
```

**Hub health card shows recorder offline:**
```bash
./deployment/deploy-slot.sh recorder --status
# Recorder health endpoint is on localhost:8090 — only accessible from the VPS
```
