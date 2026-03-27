# Deribit BTC Options Tick Recorder

Records all live BTC options on Deribit in 5-minute snapshots. Runs as a standalone systemd service (`ct-recorder`) on the production VPS and is visible in the hub dashboard.

## What it does

- Connects to Deribit via WebSocket and discovers all active BTC option instruments (~968).
- Every 5 minutes, subscribes to all option ticker channels for a 10-second burst window, captures a snapshot of the full chain, then unsubscribes immediately.
- Writes one row per instrument per snapshot to a zstd-compressed daily parquet file, in the same format used by backtester2.
- Tracks the BTC/USD spot index as 1-minute OHLC in a separate parquet file.
- Exposes a health JSON endpoint on `localhost:8090/health` (shown as a card in the hub dashboard).
- Sends Telegram alerts on startup, shutdown, disconnection, low disk, or data gaps.

## Output files

Written to `/opt/ct/recorder/data/` on the VPS. Completed at midnight UTC and rolled to a new date file.

**`options_YYYY-MM-DD.parquet`** — 5-min option chain snapshots

| Column | Type | Notes |
|---|---|---|
| `timestamp` | int64 µs | 5-minute boundary (UTC) |
| `expiry` | category | e.g. `28MAR26` |
| `strike` | float32 | e.g. `85000.0` |
| `is_call` | bool | True = call, False = put |
| `underlying_price` | float32 | BTC index price at snapshot |
| `bid_price` | float32 | BTC-denominated |
| `ask_price` | float32 | BTC-denominated |
| `mark_price` | float32 | BTC-denominated |
| `mark_iv` | float32 | Implied vol in % (e.g. `42.5`) |
| `delta` | float32 | |

**`spot_track_YYYY-MM-DD.parquet`** — 1-min BTC index OHLC

| Column | Type |
|---|---|
| `timestamp` | int64 µs, 1-min boundary |
| `open` | float32 |
| `high` | float32 |
| `low` | float32 |
| `close` | float32 |

## Bandwidth

Burst-mode keeps bandwidth to ~860 MB/day (vs ~32 GB/day always-on). Each burst subscribes for ~10 seconds per 300-second interval.

## Deployment

```bash
# First time
./deployment/deploy-slot.sh recorder --setup

# Deploy / redeploy
./deployment/deploy-slot.sh recorder

# Logs / status
./deployment/deploy-slot.sh recorder --logs
./deployment/deploy-slot.sh recorder --status
```

Requires `.env.recorder` in the project root:
```
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
RECORDER_DATA_DIR=/opt/ct/recorder/data
```

## Modules

| File | Role |
|---|---|
| `recorder.py` | Entry point, orchestrates all tasks |
| `ws_client.py` | WebSocket connection, burst subscribe/unsubscribe |
| `snapshotter.py` | Aggregates ticks into snapshots, writes parquet |
| `instruments.py` | Fetches and refreshes the instrument list from Deribit REST |
| `health.py` | HTTP health endpoint + Telegram alerting |
| `config.py` | All settings (env-var overridable) |
| `merge.py` | Merges daily partial files on rollover |
| `sync.py` | Optional: rsync completed files to a local archive |
