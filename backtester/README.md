# Backtester

BTC options backtester using real historic Deribit prices. Replays 5-minute
option snapshots + 1-minute BTC spot OHLC bars, evaluates parameter grids
across strategies, and generates self-contained HTML reports with equity
curves, heatmaps, and trade logs.

Data sources: [Tardis](https://tardis.dev) historic tick data and the live
tick recorder (`ingest/tickrecorder/`) — both produce the same parquet schema.

---

## Directory layout

```
backtester/
├── engine.py              # Single-pass grid runner
├── market_replay.py       # Snapshot loader → MarketState iterator
├── strategy_base.py       # Trade/OpenPosition dataclasses, Strategy protocol
├── run.py                 # CLI entry point
├── pricing.py             # Black-Scholes, Deribit fee model
├── reporting_v2.py        # Self-contained HTML report generator
├── config.py / config.toml
├── check_parquet.py       # Dev utility: data quality checks on snapshot files
│
├── strategies/            # One file per strategy
│   ├── daily_put_sell.py
│   ├── short_straddle_strangle.py
│   ├── short_strangle_delta.py
│   ├── short_strangle_delta_tp.py
│   └── straddle_strangle.py
│
├── ingest/                # Everything that produces data for the backtester
│   ├── snapshot_builder.py   # Converts raw tick parquets → backtester snapshots
│   ├── tardis/               # Tardis.dev fetch/extract pipeline + HistoricOptionChain
│   ├── tickrecorder/         # Live Deribit WS recorder (also runs on VPS)
│   └── raw/                  # Raw daily parquets from tickrecorder (gitignored)
│
└── data/                  # Processed snapshots ready for the engine (gitignored)
```

---

## Data pipeline

```
Tardis raw ticks          Live tick recorder (VPS)
(ingest/tardis/data/)     (ingest/raw/ after sync)
         └──────────────┬──────────────┘
               ingest/snapshot_builder.py
                         │
              ┌──────────▼──────────┐
              │  data/              │
              │  options_*.parquet  │  5-min option snapshots
              │  spot_track_*.parquet│  1-min BTC spot OHLC
              └──────────┬──────────┘
                 market_replay.py
                         │
              MarketState iterator
                         │
                    engine.py
              (single-pass grid)
                         │
           ┌─────────────▼─────────────┐
           │  Strategy.on_market_state │  × N combos simultaneously
           └─────────────┬─────────────┘
                  List[Trade]
                         │
               reporting_v2.py
                         │
               report.html
```

---

## Strategies

| CLI key | Class | Combos | Description |
|---|---|---|---|
| `put_sell` | `DailyPutSell` | 770 | Sell 1DTE OTM put, selected by delta; exit on SL or expiry |
| `short_straddle` | `ShortStraddleStrangle` | 4,860 | Sell 1DTE ATM straddle/OTM strangle by offset; SL + time/expiry exit |
| `delta_strangle` | `ShortStrangleDelta` | 2,160 | Sell 0–1DTE strangle selected by delta; SL + time/expiry exit |
| `delta_strangle_tp` | `ShortStrangleDeltaTp` | 5,000 | Same as above + take-profit leg |
| `straddle` | `ExtrusionStraddleStrangle` | 4,800 | Buy nearest-expiry straddle/strangle, exit on BTC index move |

---

## Quick start

### 1. Get data

**Option A — Tardis (historic, one-time):**
```bash
# Fetch + extract raw tick parquets for a date range
TARDIS_API_KEY=your_key python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23
```

**Option B — tick recorder (rolling live data):**
```bash
# Sync recent daily parquets from VPS into ingest/raw/
python -m backtester.ingest.tickrecorder.sync --days 14
```

### 2. Build snapshots (~2 min for 15 days)

```bash
python -m backtester.ingest.snapshot_builder
```

Output written to `data/options_<from>_<to>.parquet` and `data/spot_track_<from>_<to>.parquet`.
Update the two path keys in `config.toml` to point to the new files.

### 3. Run a backtest

```bash
python -m backtester.run --strategy put_sell
python -m backtester.run --strategy delta_strangle
python -m backtester.run --strategy short_straddle --output my_report.html
```

### 4. View the report

Open the generated HTML file in a browser:
- **Best combo** — parameters, metrics, sparkline equity curve
- **Top 20 combos** — ranked by composite score (Sharpe, PnL, drawdown)
- **Heatmaps** — auto-generated for every 2D parameter pair
- **Daily equity** — day-by-day PnL with drawdown metrics
- **Trade log** — every entry/exit for the best combo

---

## Key design notes

### Option prices and IV
- All prices in snapshots are **BTC-denominated** (e.g. `0.0068 BTC`). Converted to USD via `price × spot`.
- `mark_iv` is stored as a **percentage** (e.g. `39.8` = 39.8%). Divide by 100 before passing to `bs_call`/`bs_put`.

### Expiry selection
- The delta-based strategies support `dte` ∈ {0, 1}: 0DTE uses today's expiry (pre-08:00 UTC), 1DTE uses tomorrow's.
- `_select_expiry()` never selects an expiry whose 08:00 UTC deadline has already passed — so a 0DTE entry at 12:00 UTC is blocked (no valid 0DTE), and 1DTE is selected instead.

### Intra-bar trigger detection
- `index_move_trigger()` checks **both** the 5-min close and every 1-min high/low inside that window. Price spikes that reverse before the next 5-min snapshot are never missed.

### Fees
- Deribit model: `min(0.03% × index, 12.5% × option_price)` per leg per side.
- At BTC ~$80k the index cap ≈ 0.0003 BTC/leg and typically binds for options above ~0.0024 BTC.

### Composite scoring
- Combos are ranked by a percentile-weighted score across Sharpe, total PnL, max drawdown %, drawdown duration, and profit factor. Weights configured in `config.toml` `[scoring]`.

---

## Performance

On M1 Mac, 15 days of data (4,027 × 5-min intervals):

| Strategy | Combos | Time |
|---|---|---|
| `delta_strangle_tp` | 5,000 | ~2.5 min |
| `short_straddle` | 4,860 | ~2 min |
| `put_sell` | 770 | ~15 s |

The engine runs a single data pass for the full combo grid — all combo instances receive the same `MarketState` simultaneously. Key optimisations:

- **Pre-converted option groups** — `groupby` slices stored as plain tuple lists, not `itertuples()` namedtuples (~5× faster load).
- **Lazy `OptionQuote` construction** — raw tuples stored in `_build_state`; `OptionQuote` objects only created on `get_option()` calls, with a per-tick cache. A typical chain has ~400+ instruments; most strategies touch 1–2 per tick.
- **LRU-cached expiry parsing** — `_parse_expiry_date` / `_expiry_dt_utc` cached. Without it, regex ran 1.5M times per grid run.
- **Pre-computed expiry deadline** — stored in `pos.metadata['expiry_dt']` at entry; `_check_expiry` reads it directly.

---

## Adding a strategy

1. Create `strategies/my_strategy.py` implementing the `Strategy` protocol:
   - `name: str` — CLI-safe identifier
   - `PARAM_GRID: dict` — `{param: [values]}` for grid search
   - `configure(params)` — apply one combo's parameters
   - `on_market_state(state) → List[Trade]` — called every 5-min tick
   - `on_end(state) → List[Trade]` — force-close any open position at data end
   - `reset()` — clear all state between grid combos (including date counters)
   - `describe_params() → dict` — return current param values for reporting

2. Register in `run.py`:
   ```python
   from backtester.strategies.my_strategy import MyStrategy
   STRATEGIES["my_strat"] = MyStrategy
   ```

3. Run: `python -m backtester.run --strategy my_strat`

---

## Dev utilities

```bash
# Check data quality of a snapshot parquet
python -m backtester.check_parquet

# Quick sanity-check a single tardis raw file
python backtester/ingest/tardis/_validate.py path/to/file.parquet

# Full quality sweep across all tardis raw parquets
python -m backtester.ingest.tardis.quality_check
```

---

Python 3.9+. Dependencies: `pandas`, `numpy`, `pyarrow`, `tomli` (Python < 3.11).
