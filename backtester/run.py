#!/usr/bin/env python3
"""
run.py — CLI entry point for backtester V2.

Usage:
    python -m backtester.run
    python -m backtester.run --strategy straddle
    python -m backtester.run --strategy put_sell
    python -m backtester.run --strategy straddle --output report.html
"""
import argparse
import os
import sys
import time
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backtester.market_replay import MarketReplay
from backtester.engine import run_grid_full
from backtester.reporting_v2 import generate_html
from backtester.strategies.straddle_strangle import ExtrusionStraddleStrangle
from backtester.strategies.daily_put_sell import DailyPutSell
from backtester.strategies.short_straddle_strangle import ShortStraddleStrangle
from backtester.strategies.short_strangle_delta import ShortStrangleDelta
from backtester.strategies.short_strangle_delta_tp import ShortStrangleDeltaTp
from backtester.config import cfg as _cfg

# ── Strategy Registry ────────────────────────────────────────────

STRATEGIES = {
    "straddle": ExtrusionStraddleStrangle,
    "put_sell": DailyPutSell,
    "short_straddle": ShortStraddleStrangle,
    "delta_strangle": ShortStrangleDelta,
    "delta_strangle_tp": ShortStrangleDeltaTp,
}

DEFAULT_OPTIONS = _cfg.data.options_parquet
DEFAULT_SPOT = _cfg.data.spot_parquet


# ── Main ─────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backtester V2")
    parser.add_argument("--strategy", default="straddle",
                        choices=list(STRATEGIES.keys()))
    parser.add_argument("--options", default=DEFAULT_OPTIONS)
    parser.add_argument("--spot", default=DEFAULT_SPOT)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    strategy_cls = STRATEGIES[args.strategy]

    print(f"\n{'='*60}")
    print(f"  Backtester V2 — {strategy_cls.name}")
    print(f"{'='*60}")

    # Load data
    t0 = time.time()
    date_range_filter = getattr(strategy_cls, "DATE_RANGE", (None, None))
    replay = MarketReplay(args.options, args.spot,
                         start=date_range_filter[0], end=date_range_filter[1])
    print(f"  Data loaded: {len(replay._timestamps):,} intervals in {time.time()-t0:.1f}s")

    # Run grid
    t1 = time.time()
    df, keys, nav_daily_df, final_nav_df = run_grid_full(
        strategy_cls, strategy_cls.PARAM_GRID, replay
    )
    grid_time = time.time() - t1

    n_combos = len(keys)
    total_trades = len(df)
    print(f"  {n_combos:,} combos, {total_trades:,} trades in {grid_time:.1f}s")

    # Date range from spot data
    first_dt = datetime.fromtimestamp(
        int(replay._spot_ts[0]) / 1_000_000, tz=timezone.utc)
    last_dt = datetime.fromtimestamp(
        int(replay._spot_ts[-1]) / 1_000_000, tz=timezone.utc)
    date_range = (first_dt.strftime("%Y-%m-%d"), last_dt.strftime("%Y-%m-%d"))

    # Console summary — top combos
    totals = (
        df.groupby("combo_idx")
        .agg(total_pnl=("pnl", "sum"),
             n=("pnl", "count"),
             wins=("pnl", lambda x: (x > 0).sum()))
        .sort_values("total_pnl", ascending=False)
    )
    print(f"\n  Top {_cfg.simulation.top_n_console} combos:")
    for row in totals.head(_cfg.simulation.top_n_console).itertuples():
        key = keys[row.Index]
        params = dict(key)
        wr = row.wins / row.n * 100 if row.n else 0
        label = " | ".join(f"{k}={_fmt_val(v)}" for k, v in sorted(params.items()))
        print(f"    {label}  →  ${row.total_pnl:,.0f}  ({row.n} trades, {wr:.0f}% win)")

    # Generate HTML report
    html = generate_html(
        strategy_name=strategy_cls.name,
        param_grid=strategy_cls.PARAM_GRID,
        df=df,
        keys=keys,
        nav_daily_df=nav_daily_df,
        final_nav_df=final_nav_df,
        date_range=date_range,
        n_intervals=len(replay._timestamps),
        runtime_s=grid_time,
        strategy_description=getattr(strategy_cls, "DESCRIPTION", ""),
    )

    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(reports_dir, exist_ok=True)
    output_path = args.output or os.path.join(reports_dir, f"{args.strategy}_report.html")
    with open(output_path, "w") as f:
        f.write(html)

    print(f"\n  Report: {output_path}")
    print(f"  Total:  {time.time()-t0:.1f}s\n")


def _fmt_val(v):
    if isinstance(v, float) and v != int(v):
        return f"{v:.2f}"
    return str(int(v) if isinstance(v, float) else v)


if __name__ == "__main__":
    main()

