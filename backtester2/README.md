# Backtester V2

Options backtester using real historic Deribit prices from Tardis. Replays 5-minute option snapshots + 1-minute BTC spot bars, evaluates parameter grids across strategies, and generates self-contained HTML reports.

## Architecture

```
Raw Tardis ticks (13 GB)
    │  snapshot_builder.py  (run once)
    ▼
Option snapshots (5-min, parquet) + Spot track (1-min OHLC)
    │  market_replay.py  (load into RAM)
    ▼
MarketState iterator → engine.py (single-pass multi-combo grid)
    │                       │
    ▼                       ▼
Strategy.on_market_state()  →  List[Trade]  →  reporting_v2.py  →  HTML
```

## Modules

| File | Purpose |
|---|---|
| `snapshot_builder.py` | One-time conversion: raw tick parquets → 5-min option snapshots + 1-min spot OHLC |
| `market_replay.py` | Loads snapshots into memory, iterates `MarketState` objects with option chain + spot data |
| `strategy_base.py` | `Trade`/`OpenPosition` dataclasses, `Strategy` protocol, composable entry/exit conditions |
| `engine.py` | Single-pass grid runner: evaluates all parameter combos in one data scan |
| `reporting_v2.py` | Strategy-agnostic HTML report: best combo, top 20, heatmaps, equity curve, trade log |
| `run.py` | CLI entry point |
| `pricing.py` | Black-Scholes model, Deribit fee calculation |
| `metrics.py` | Stats, equity curves, Sharpe/Sortino/Calmar scoring |

## Strategies

| Strategy | Class | Combos | Description |
|---|---|---|---|
| `straddle` | `ExtrusionStraddleStrangle` | 5,040 | Buy nearest-expiry ATM straddle/OTM strangle, exit on BTC index move |
| `put_sell` | `DailyPutSell` | 20 | Sell 1DTE OTM put, exit on stop-loss or expiry |

## Key Design Notes

### Option prices and IV
- All option prices in the snapshots are **BTC-denominated** (e.g. `0.0068 BTC`). Converted to USD via `price × spot`.
- `mark_iv` is stored as a **percentage** (e.g. `39.8` = 39.8% IV). Divide by 100 before passing to `bs_call`/`bs_put`. Both strategies do this correctly.

### Expiry selection (`straddle_strangle`)
- `_nearest_valid_expiry()` picks the closest expiry whose 08:00 UTC deadline hasn't passed yet.
- Before 08:00 UTC: today's expiry is used (0DTE). After 08:00 UTC: tomorrow's expiry is used (~1DTE).
- This is essential for afternoon/evening entry hours (12, 15, 19 UTC) — a naive "today's expiry" approach would block all post-08:00 entries.

### Trigger detection
- `index_move_trigger()` in `strategy_base.py` checks two things per 5-min tick:
  1. The 5-min close spot vs entry spot.
  2. Every 1-min bar high and low inside that 5-min window.
- This ensures intra-bar price spikes are never missed, even if price reverses before the next snapshot.

### One trade per day
- `straddle_strangle` tracks `_last_trade_date` (stamped from `entry_time`) to prevent re-entry on the same calendar day. `reset()` clears it between grid combos.

### Fees
- Deribit model: `MIN(0.03% × index, 12.5% × option_price)` per leg per trade side.
- At typical BTC prices (~$65k–$85k) the index cap = **0.0003 BTC/leg** and usually binds for options priced above ~0.0024 BTC.

## Quick Start

### 1. Build snapshots (one-time, ~2 min)

Requires raw Tardis parquets in `backtester2/tardis_options/`.

```bash
python -m backtester2.snapshot_builder
```

Output: `backtester2/snapshots/options_*.parquet` + `spot_track_*.parquet`

### 2. Run a backtest

```bash
# Straddle/strangle grid (5,040 combos)
python -m backtester2.run --strategy straddle

# Put sell (20 combos)
python -m backtester2.run --strategy put_sell

# Custom output path
python -m backtester2.run --strategy straddle --output my_report.html
```

### 3. View the report

Open the generated HTML file in a browser. Sections include:
- **Best combo** with sparkline equity curve
- **Top 20 combos** ranked by total PnL
- **Heatmaps** for every 2D parameter pair (auto-generated)
- **Daily equity** table with drawdown metrics
- **Trade log** for the best combo

## Adding a New Strategy

1. Create `strategies/my_strategy.py` implementing the `Strategy` protocol:
   - `configure(params)` — set parameters
   - `on_market_state(state) → List[Trade]` — process each 5-min tick
   - `on_end(state) → List[Trade]` — force-close at end of data
   - `reset()` — clear state between grid runs (including any per-day counters)
   - `describe_params() → dict`
   - Class attributes: `name: str`, `PARAM_GRID: dict`

2. Register in `run.py`:
   ```python
   from backtester2.strategies.my_strategy import MyStrategy
   STRATEGIES["my_strat"] = MyStrategy
   ```

3. Run: `python -m backtester2.run --strategy my_strat`

## Performance

On M1 Mac (15 days of data, 4,310 intervals):

| Strategy | Combos | Time |
|---|---|---|
| Straddle/strangle | 5,040 | ~2 min |
| Put sell | 20 | ~5s |

## Requirements

Python 3.9+. Dependencies: `pandas`, `numpy`, `pyarrow`.
