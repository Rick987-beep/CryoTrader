# analysis/

Offline analysis and backtesting tools for BTC 0DTE option strategies.

## Structure

```
analysis/
├── backtester/                  # Modular Black-Scholes backtester
│   ├── backtest.py              #   CLI entry point & orchestrator
│   ├── pricing.py               #   BS pricing, strike grid, vol, Deribit fees
│   ├── data.py                  #   Binance 1h candle fetcher (with pagination)
│   ├── straddle_strangle.py     #   Strategy: long straddle/strangle + TP trigger
│   ├── metrics.py               #   Stats, equity curve, composite scoring
│   ├── reporting.py             #   Console tables + HTML report generation
│   ├── backtest_deribit_realdata.py  # Standalone: ground-truth backtest with real Deribit bid/ask
│   └── archive/                 #   Old versions & superseded scripts
├── capture_snapshot.py          # Captures BTC option chain snapshots from Coincall
├── data/                        # Snapshot JSON files
├── tardis_options/              # Deribit tardis.dev data & HistoricOptionChain module
├── PutSelling/                  # Put-selling strategy analysis
└── README.md
```

## Backtester

**What it does:** Simulates buying 0DTE BTC straddles/strangles across a parameter grid
(17,640 combos) and ranks them using a 12-metric composite score.

**Module flow:**
```
backtest.py  →  data.py (fetch candles)
             →  straddle_strangle.py (run backtest loop)
                    └── pricing.py (BS pricing, vol, fees)
             →  metrics.py (stats + equity curve)
             →  reporting.py (console + HTML)
```

**Pricing model:**
- Black-Scholes with r=0, σ from trailing 24h hourly log returns
- Deribit $500 strike grid, MIN(0.03% × index, 12.5% × leg) fee model
- ±4% slippage on theoretical price

**Usage:**
```bash
# Default: 5 weeks, weekdays only
.venv/bin/python analysis/backtester/backtest.py

# Custom window
.venv/bin/python analysis/backtester/backtest.py --weeks 8

# Include weekends
.venv/bin/python analysis/backtester/backtest.py --include-weekends
```

**Output:** Console tables + `backtest_blackscholes_report.html` in the backtester directory.

## Real-Data Backtest (Deribit)

`backtester/backtest_deribit_realdata.py` is a standalone ground-truth backtest using
actual Deribit bid/ask data from tardis.dev (no BS model). It depends on
`analysis.tardis_options.HistoricOptionChain` and a parquet data file.

This module has its own reporting layer. A future refactor could share the
reporting module with the main backtester.

## Shared Tools

| File | Purpose |
|------|---------|
| `capture_snapshot.py` | Captures full BTC option chain snapshots (nearest expiry) — index price, mark/bid/ask, Greeks. Saves to `data/` as JSON. |

## Capture Snapshots

```bash
# Hourly loop:
python -m analysis.capture_snapshot --loop

# One-shot at a specific UTC hour:
python -m analysis.capture_snapshot 12
```
