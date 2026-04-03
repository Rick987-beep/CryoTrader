#!/usr/bin/env python3
"""
Quality check across all tardis parquet files.
Checks: row counts, time coverage, data gaps, NaN rates, expiry consistency,
spot price sanity, bid/ask spread sanity.
"""
import os
import sys
import glob
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pyarrow.compute as pc

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
INTERVAL_MIN = 5  # expected snapshot interval

def check_file(path: str) -> dict:
    date_str = os.path.basename(path).replace("btc_", "").replace(".parquet", "")
    table = pq.read_table(path)
    df = table.to_pandas()

    result = {"date": date_str, "rows": len(df), "warnings": []}

    # ── Time coverage ──────────────────────────────────────────
    ts = pd.to_datetime(df["timestamp"], unit="us", utc=True)
    t_start = ts.min()
    t_end = ts.max()
    result["t_start"] = t_start.strftime("%H:%M")
    result["t_end"] = t_end.strftime("%H:%M")

    # Check coverage: should span close to full 24h
    span_hours = (t_end - t_start).total_seconds() / 3600
    result["span_h"] = round(span_hours, 1)
    if span_hours < 22:
        result["warnings"].append(f"Short span: only {span_hours:.1f}h covered")

    # ── Gap detection: find 5-min windows with no data at all ──
    df["minute_bucket"] = (df["timestamp"] // (5 * 60 * 1_000_000)) * (5 * 60 * 1_000_000)
    covered_buckets = df["minute_bucket"].nunique()
    expected_buckets = int(span_hours * 60 / INTERVAL_MIN)
    gap_count = max(0, expected_buckets - covered_buckets)
    result["5min_buckets"] = covered_buckets
    result["gaps"] = gap_count
    if gap_count > 10:
        result["warnings"].append(f"{gap_count} missing 5-min windows")

    # ── Expiries ───────────────────────────────────────────────
    expiries = sorted(df["expiry"].unique().tolist())
    result["expiries"] = len(expiries)
    result["expiry_list"] = expiries

    # Same-day expiry should be present
    day_dt = date.fromisoformat(date_str)
    day_fmt = day_dt.strftime("%-d") + day_dt.strftime("%b").upper() + day_dt.strftime("%y")
    if day_fmt not in expiries:
        result["warnings"].append(f"No same-day expiry ({day_fmt}) in data")

    # ── Spot price sanity ──────────────────────────────────────
    spot = df["underlying_price"].dropna()
    result["spot_min"] = round(float(spot.min()), 0)
    result["spot_max"] = round(float(spot.max()), 0)
    spot_range_pct = (spot.max() - spot.min()) / spot.mean() * 100
    if spot_range_pct > 15:
        result["warnings"].append(f"Spot range unusually wide: {spot_range_pct:.1f}%")

    # ── NaN rates ──────────────────────────────────────────────
    for col in ["bid_price", "ask_price", "mark_price", "mark_iv", "delta"]:
        nan_rate = df[col].isna().mean() * 100
        if nan_rate > 30:
            result["warnings"].append(f"{col} NaN rate: {nan_rate:.1f}%")
    result["bid_nan_pct"] = round(df["bid_price"].isna().mean() * 100, 1)
    result["ask_nan_pct"] = round(df["ask_price"].isna().mean() * 100, 1)

    # ── Bid/ask sanity: ask should be >= bid ───────────────────
    valid = df.dropna(subset=["bid_price", "ask_price"])
    inverted = (valid["ask_price"] < valid["bid_price"]).sum()
    if inverted > 0:
        result["warnings"].append(f"{inverted:,} rows with ask < bid")

    # ── Strike count (proxy for chain completeness) ────────────
    result["strikes"] = int(df["strike"].nunique())

    return result


def main():
    paths = sorted(glob.glob(os.path.join(DATA_DIR, "btc_2026-*.parquet")))
    if not paths:
        print("No parquet files found in", DATA_DIR)
        sys.exit(1)

    print(f"Quality check — {len(paths)} files\n")
    print(f"{'Date':<13} {'Rows':>10} {'Span':>6} {'5mBkts':>7} {'Gaps':>5} "
          f"{'Expir':>5} {'Strikes':>8} {'Spot range':>20} {'bidNaN':>7} {'askNaN':>7}  Warnings")
    print("-" * 120)

    all_warnings = []
    for path in paths:
        r = check_file(path)
        spot_range = f"${r['spot_min']:,.0f}–${r['spot_max']:,.0f}"
        warn_str = " | ".join(r["warnings"]) if r["warnings"] else "OK"
        print(f"{r['date']:<13} {r['rows']:>10,} {r['span_h']:>5.1f}h {r['5min_buckets']:>7,} "
              f"{r['gaps']:>5} {r['expiries']:>5} {r['strikes']:>8,} "
              f"{spot_range:>20}  {r['bid_nan_pct']:>5.1f}%  {r['ask_nan_pct']:>5.1f}%  {warn_str}")
        if r["warnings"]:
            all_warnings.append((r["date"], r["warnings"]))

    print("-" * 120)
    print(f"\nTotal files: {len(paths)}")
    if all_warnings:
        print(f"\nFiles with warnings ({len(all_warnings)}):")
        for d, w in all_warnings:
            for msg in w:
                print(f"  {d}: {msg}")
    else:
        print("\nAll files passed quality checks.")


if __name__ == "__main__":
    main()
