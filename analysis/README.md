# analysis/

Offline option chain analysis tools for calibrating strategy parameters.

## Modules

| File | Purpose |
|------|---------|
| `capture_snapshot.py` | Captures full BTC option chain snapshots (nearest expiry) — index price, mark/bid/ask, Greeks for strikes ±$5k of ATM. Saves to `data/` as JSON. Supports hourly loop, scheduled capture, or immediate one-shot. |
| `analyze_grid.py` | Computes PnL grids over (strike offset, realized BTC move) using a 12:00 entry snapshot and 19:00 exit snapshot. Reports entry cost, PnL, efficiency, theta decay, and optimal structure width for each day. |

## Data

Snapshots are stored in `data/` as `snapshot_YYYYMMDD_HHMM.json`.

## Usage

```bash
# Capture snapshots every hour (run 24h+):
python -m analysis.capture_snapshot --loop

# Capture at a specific UTC hour:
python -m analysis.capture_snapshot 12

# Analyze a day's entry/exit:
python -m analysis.analyze_grid 20260310
```
