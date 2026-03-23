#!/usr/bin/env python3
"""
0DTE Backtest — Modular Entry Point
====================================

Orchestrates: data.py → straddle_strangle.py → metrics.py → reporting.py

Usage:
    .venv/bin/python analysis/backtester/backtest.py
    .venv/bin/python analysis/backtester/backtest.py --weeks 8
    .venv/bin/python analysis/backtester/backtest.py --include-weekends

Outputs:
    Console — comprehensive tables + composite parameter ranking
    HTML    — backtest_report.html (heatmaps, top combos, recommendation)
"""

import argparse
import os
import statistics
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from pricing import price_structure, estimate_vol, VOL_LOOKBACK, DEFAULT_VOL
from data import fetch_binance_candles
from metrics import compute_stats
from straddle_strangle import (
    OFFSETS, INDEX_TRIGGERS, MAX_HOLDS, MAX_ENTRY_HOUR, MIN_SAMPLES,
    run_backtest,
)
from reporting import (
    print_data_summary, print_entry_premiums, print_btc_range,
    print_trigger_hit_rate, print_avg_pnl_table, print_conversion_efficiency,
    print_top_combos, print_best_per_structure, print_hourly_summary,
    print_robust_selection, print_equity_deep_dive, generate_html,
)


def main():
    parser = argparse.ArgumentParser(description="0DTE Backtest (BS)")
    parser.add_argument("--weeks", type=int, default=5,
                        help="Weeks of Binance data (default: 5)")
    parser.add_argument("--include-weekends", action="store_true",
                        help="Allow entries on Saturday and Sunday")
    args = parser.parse_args()

    weekdays_only = not args.include_weekends

    all_candles = fetch_binance_candles(weeks=args.weeks)

    dates = sorted(set(c["date"] for c in all_candles))
    print("  %d total days: %s to %s" % (len(dates), dates[0], dates[-1]))

    if weekdays_only:
        entry_dates = sorted(set(
            c["date"] for c in all_candles if c["weekday"] < 5))
        print("  %d weekday entry days (all candles kept for walk-forward)"
              % len(entry_dates))
    else:
        entry_dates = dates

    # Vol sanity check
    sorted_c = sorted(all_candles, key=lambda c: c["dt"])
    sample_vols = []
    for idx in range(VOL_LOOKBACK, len(sorted_c), 24):
        sample_vols.append(estimate_vol(sorted_c, idx))
    if sample_vols:
        print("\n  Vol check (sampled daily):")
        print("    Range:  %.0f%% – %.0f%%" % (
            min(sample_vols) * 100, max(sample_vols) * 100))
        print("    Mean:   %.0f%%" % (statistics.mean(sample_vols) * 100))
        print("    Median: %.0f%%" % (statistics.median(sample_vols) * 100))

    # BS premium sanity check
    avg_btc = statistics.mean([c["open"] for c in all_candles])
    avg_vol = statistics.mean(sample_vols) if sample_vols else DEFAULT_VOL
    print("\n  BS premium sanity (at avg BTC=$%s, vol=%.0f%%, 22h DTE):" % (
        "{:,.0f}".format(avg_btc), avg_vol * 100))
    for offset in [0, 500, 1000, 1500, 2000, 3000]:
        total, _, _, K_c, K_p = price_structure(avg_btc, offset, 22, avg_vol)
        label = "straddle" if offset == 0 else "+/-%d" % offset
        print("    %10s: $%s  (K_call=%d, K_put=%d)" % (
            label, "{:,.0f}".format(total), K_c, K_p))

    # Run backtest
    total_combos = (len(OFFSETS) * (MAX_ENTRY_HOUR + 1)
                    * len(INDEX_TRIGGERS) * len(MAX_HOLDS))
    print("\n  Running backtest (BS + realized vol)...")
    print("    Structures:  %d" % len(OFFSETS))
    print("    Triggers:    %d" % len(INDEX_TRIGGERS))
    print("    Max holds:   %d" % len(MAX_HOLDS))
    print("    Param combos: %d" % total_combos)

    results, btc_ranges, entry_spots, entry_vols, ep = run_backtest(
        all_candles, weekdays_only=weekdays_only)

    total_trades = sum(len(v) for v in results.values())
    print("    Total trades simulated: %s" % "{:,}".format(total_trades))

    stats = compute_stats(results)
    valid = sum(1 for s in stats.values() if s["n"] >= MIN_SAMPLES)
    print("    Combos with >= %d samples: %d" % (MIN_SAMPLES, valid))

    meta = {
        "weeks": args.weeks,
        "weekdays_only": weekdays_only,
        "n_entry_days": len(entry_dates),
        "date_range": [dates[0], dates[-1]],
    }

    # Console output
    print_data_summary(all_candles, entry_spots, entry_vols, meta)
    print_entry_premiums(ep, entry_spots, entry_vols)
    print_btc_range(btc_ranges)
    print_trigger_hit_rate(stats)
    print_avg_pnl_table(stats)
    print_conversion_efficiency(stats, ep)
    print_top_combos(stats, ep)
    print_best_per_structure(stats, ep)
    print_hourly_summary(stats, entry_spots, entry_vols, ep)
    candidates = print_robust_selection(stats, ep, results)
    if candidates:
        print_equity_deep_dive(results, candidates)

    # HTML report
    html = generate_html(stats, btc_ranges, entry_spots, entry_vols, ep, meta,
                         results=results)
    html_path = os.path.join(SCRIPT_DIR, "backtest_blackscholes_report.html")
    with open(html_path, "w") as f:
        f.write(html)
    print("\n  HTML report -> %s" % html_path)


if __name__ == "__main__":
    main()
