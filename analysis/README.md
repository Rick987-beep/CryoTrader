# analysis/

Offline analysis tools for calibrating strategy parameters using real option chain data.

## Structure

```
analysis/
├── capture_snapshot.py          # Shared: captures BTC option chain snapshots
├── data/                        # Shared: snapshot JSON files
│   └── snapshot_YYYYMMDD_HHMM.json
├── optimal_entry_window/        # Study: optimal 0DTE entry time & structure width
│   ├── analyze_grid.py          #   Single-day straddle vs strangle PnL grid
│   ├── build_grid.py            #   Multi-day 2D grid (strike offset × realized move)
│   ├── backtest_structures.py   #   Full backtest using Binance candles + √t decay model
│   ├── hourly_excursion.py      #   Optimal entry/exit window finder (Binance 1h candles)
│   ├── RESEARCH_PLAN_optimal_window.html  # Research plan
│   ├── backtest_report.html     #   Latest backtest results
│   ├── hourly_excursion_report.html       #   Latest excursion heatmap
│   └── hourly_excursion.json    #   Raw excursion data
└── README.md
```

## Shared Tools

| File | Purpose |
|------|---------|
| `capture_snapshot.py` | Captures full BTC option chain snapshots (nearest expiry) — index price, mark/bid/ask, Greeks for strikes ±$5k of ATM. Saves to `data/` as JSON. Supports hourly loop, scheduled capture, or one-shot. |

## Optimal Entry Window Study

**Goal:** Determine the best UTC entry hour and structure width (straddle vs strangle ±K)
for 0DTE BTC options, balancing move capture against theta decay.

**Approach:**
1. **Hourly excursion analysis** (`hourly_excursion.py`) — fetches 4+ weeks of Binance 1h candles, computes average max BTC excursion for every (entry_hour, exit_hour) combination, ranks windows by efficiency ($/hr).
2. **Structure backtest** (`backtest_structures.py`) — simulates buying straddles/strangles at various entry times using Binance candle data and a √t theta-decay model calibrated from real Coincall snapshots. Tests take-profit scenarios and hold durations.
3. **Snapshot-based grid** (`analyze_grid.py`, `build_grid.py`) — uses real Coincall option chain snapshots to compute actual PnL for each structure width across realized BTC moves.

**Key findings** are in the HTML reports in `optimal_entry_window/`.

## Usage

```bash
# Capture snapshots every hour (shared data collection):
python -m analysis.capture_snapshot --loop

# Capture at a specific UTC hour:
python -m analysis.capture_snapshot 12

# Run optimal entry window analyses:
python -m analysis.optimal_entry_window.hourly_excursion --weeks 4
python -m analysis.optimal_entry_window.backtest_structures --weeks 8
python -m analysis.optimal_entry_window.analyze_grid 20260311
python -m analysis.optimal_entry_window.build_grid
```
