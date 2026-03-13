#!/usr/bin/env python3
"""
0DTE Option Structure Backtest (v2 — corrected PnL model)
=========================================================

Simulates buying straddles/strangles at various entry times, then tracks
hourly P&L using Binance 1h candles + a √t decay model calibrated from
real Coincall snapshot data.

PnL model (no double-counting):
  net_pnl = gross_gain(|BTC move|) − theta_lost(hold_hours) − trading_cost

  gross_gain:
    Straddle: |BTC move|
    Strangle ±K: max(0, |BTC move| − K)

  theta_lost:
    premium(entry_dte) − premium(entry_dte − hours_held)
    where premium ∝ √(hours_remaining)

  TP detection: uses running max excursion from candle highs/lows
    (TP can trigger on any intra-hour touch)
  Forced exit: uses close-to-close BTC move at max_hold
    (actual exit price, not the ghost of a past spike)
"""

import argparse
import json
import math
import os
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

try:
    import requests
except ImportError:
    print("ERROR: requests library required. pip install requests")
    sys.exit(1)

# ── Configuration ─────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")

# Structures to test: offset from ATM in $ (0 = straddle)
OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]

# Take-profit targets (net profit after all costs, in $)
TP_TARGETS = [50, 100, 150, 200, 300, 400, 500, 600, 800, 1000]

# Max hold windows in hours
MAX_HOLDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

# Cost model
FEE_PER_CONTRACT = 14.0   # $14 per contract per open or close
NUM_CONTRACTS = 2          # straddle/strangle = 1 call + 1 put
ROUND_TRIP_FEES = FEE_PER_CONTRACT * NUM_CONTRACTS * 2  # $56
SLIPPAGE_PCT = 0.042       # 4.2% round-trip on premium

# Premium calibration from snapshot data (12 Mar 2026 expiry)
# These are approximate mark prices at ~22h DTE when spot ≈ ATM
# Derived from snapshot_20260311_0900 (DTE=22.9h, spot=69565, ATM=69500)
ENTRY_PREMIUMS_AT_22H = {
    0:    1438,   # ATM straddle
    500:  995,    # strangle ±500
    1000: 681,    # strangle ±1000
    1500: 442,    # strangle ±1500
    2000: 286,    # strangle ±2000
    2500: 182,    # strangle ±2500
    3000: 118,    # strangle ±3000
}

# BTC options on Coincall expire at 08:00 UTC
EXPIRY_HOUR = 8


def hours_to_expiry(entry_hour):
    """
    Compute hours remaining to 08:00 UTC next day expiry.
    Entry at 14:00 → 18h; entry at 02:00 → 6h; entry at 08:00 → 24h.
    """
    if entry_hour >= EXPIRY_HOUR:
        return 24 - entry_hour + EXPIRY_HOUR
    else:
        return EXPIRY_HOUR - entry_hour


def premium_at_dte(offset, dte_hours):
    """
    Estimate premium using √t decay model.
    Premium(t) = P_ref × √(t / t_ref)
    where P_ref = premium at 22h DTE (from snapshot calibration).
    """
    t_ref = 22.0
    p_ref = ENTRY_PREMIUMS_AT_22H[offset]
    if dte_hours <= 0:
        return 0.0
    return p_ref * math.sqrt(dte_hours / t_ref)


def total_cost(entry_premium):
    """Total round-trip cost: fixed fees + slippage on premium."""
    return ROUND_TRIP_FEES + SLIPPAGE_PCT * entry_premium


def theta_lost(offset, entry_dte, hours_held):
    """
    Theta cost: how much premium decays over the hold period.
    theta_lost = premium(entry_dte) - premium(entry_dte - hours_held)
    Always positive (you lose this amount).
    """
    exit_dte = entry_dte - hours_held
    return premium_at_dte(offset, entry_dte) - premium_at_dte(offset, max(exit_dte, 0))


def gross_gain(offset, btc_move_abs):
    """
    Gross gain from BTC move on a straddle/strangle.

    Straddle (offset=0): one leg gains full intrinsic = |move|.
    Strangle ±K: gain only starts once |move| > K, then = |move| - K.
    """
    return max(0.0, btc_move_abs - offset)


def net_pnl(offset, btc_move_abs, entry_dte, hours_held, cost):
    """
    Clean P&L formula: gain from move - theta lost - trading cost.
    No double-counting — theta is a subtracted cost, not an added value.
    """
    return gross_gain(offset, btc_move_abs) - theta_lost(offset, entry_dte, hours_held) - cost


# ── Binance Data Fetcher (reuse from hourly_excursion.py) ─────────

def fetch_binance_candles(weeks=4):
    """Fetch 1h BTCUSDT perp candles from Binance."""
    limit = weeks * 7 * 24
    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {"symbol": "BTCUSDT", "interval": "1h", "limit": min(limit, 1500)}

    print(f"Fetching {params['limit']} hourly candles from Binance...")
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    candles = []
    for r in raw:
        dt = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc)
        candles.append({
            "dt": dt,
            "hour": dt.hour,
            "weekday": dt.weekday(),
            "date": dt.strftime("%Y-%m-%d"),
            "open": float(r[1]),
            "high": float(r[2]),
            "low": float(r[3]),
            "close": float(r[4]),
        })
    print(f"  Got {len(candles)} candles: {candles[0]['date']} to {candles[-1]['date']}")
    return candles


def filter_weekdays(candles):
    """Keep only Mon-Fri candles."""
    filtered = [c for c in candles if c["weekday"] < 5]
    print(f"  Weekday candles: {len(filtered)} (excluded {len(candles) - len(filtered)} weekend)")
    return filtered


# ── Backtest Engine ───────────────────────────────────────────────

def run_backtest(candles, max_window=12):
    """
    Run the full backtest across all parameter combinations.

    For each entry candle:
      1. Walk forward hour by hour, tracking running max excursion
      2. At each hour h, compute net_pnl using the clean formula:
            net_pnl = gross_gain(move) - theta_lost(h hours) - cost
      3. For TP detection: use running max excursion (candle highs/lows)
         because TP can trigger on any intra-hour touch
      4. For forced exit at max_hold: use |close - entry| because
         that's the actual exit price, not the peak that already passed

    Returns: dict of results keyed by (offset, entry_hour, tp, max_hold)
    """
    sorted_candles = sorted(candles, key=lambda c: c["dt"])
    results = defaultdict(list)
    n = len(sorted_candles)

    for i, entry_candle in enumerate(sorted_candles):
        entry_hour = entry_candle["hour"]
        s_entry = entry_candle["open"]
        entry_dte = hours_to_expiry(entry_hour)

        for offset in OFFSETS:
            entry_premium = premium_at_dte(offset, entry_dte)
            if entry_premium < 5:
                continue

            cost = total_cost(entry_premium)

            # Pre-compute per-hour data: running max excursion + close move
            hourly_data = []
            running_high = s_entry
            running_low = s_entry

            for h in range(1, max(MAX_HOLDS) + 1):
                j = i + h
                if j >= n:
                    break
                c = sorted_candles[j]

                # Verify continuity (no weekend/data gaps)
                expected_dt = entry_candle["dt"] + timedelta(hours=h)
                if c["dt"] != expected_dt:
                    break

                if entry_dte - h <= 0:
                    break  # Past expiry

                running_high = max(running_high, c["high"])
                running_low = min(running_low, c["low"])
                max_move = max(running_high - s_entry, s_entry - running_low)
                close_move = abs(c["close"] - s_entry)

                hourly_data.append({
                    "h": h,
                    "max_move": max_move,
                    "close_move": close_move,
                })

            if not hourly_data:
                continue

            # For each (tp, max_hold), simulate the trade
            for max_hold in MAX_HOLDS:
                if max_hold > entry_dte:
                    continue

                # Track which TPs get hit (earliest hour)
                tp_hit = {tp: None for tp in TP_TARGETS}

                for hd in hourly_data:
                    if hd["h"] > max_hold:
                        break

                    # TP check: use running max excursion
                    pnl_at_peak = net_pnl(offset, hd["max_move"],
                                          entry_dte, hd["h"], cost)

                    for tp in TP_TARGETS:
                        if tp_hit[tp] is None and pnl_at_peak >= tp:
                            tp_hit[tp] = tp  # Exit at TP

                # Forced exit P&L: use close price at max_hold
                last_hd = None
                for hd in hourly_data:
                    if hd["h"] <= max_hold:
                        last_hd = hd
                if last_hd is None:
                    continue

                forced_exit_pnl = net_pnl(offset, last_hd["close_move"],
                                          entry_dte, last_hd["h"], cost)

                for tp in TP_TARGETS:
                    key = (offset, entry_hour, tp, max_hold)
                    if tp_hit[tp] is not None:
                        results[key].append(tp_hit[tp])
                    else:
                        results[key].append(forced_exit_pnl)

    return results


def compute_stats(results):
    """Compute summary statistics for each parameter combo."""
    stats = {}
    for key, pnls in results.items():
        if not pnls:
            continue
        wins = sum(1 for p in pnls if p > 0)
        stats[key] = {
            "n": len(pnls),
            "win_rate": wins / len(pnls),
            "avg_pnl": statistics.mean(pnls),
            "median_pnl": statistics.median(pnls),
            "total_pnl": sum(pnls),
            "max_loss": min(pnls),
            "max_win": max(pnls),
        }
    return stats


# ── Console Output ────────────────────────────────────────────────

def print_top_combos(stats, n=20):
    """Print the top N combos by average P&L."""
    print()
    print("=" * 100)
    print(f"  TOP {n} PARAMETER COMBINATIONS BY AVG P&L")
    print("=" * 100)

    ranked = []
    for key, s in stats.items():
        offset, entry_h, tp, max_hold = key
        if s["n"] < 5:  # minimum sample size
            continue
        label = "straddle" if offset == 0 else f"±{offset}"
        ranked.append({
            "label": label,
            "offset": offset,
            "entry": f"{entry_h:02d}:00",
            "tp": tp,
            "max_hold": max_hold,
            "avg_pnl": s["avg_pnl"],
            "median_pnl": s["median_pnl"],
            "win_rate": s["win_rate"],
            "total_pnl": s["total_pnl"],
            "n": s["n"],
        })

    ranked.sort(key=lambda x: x["avg_pnl"], reverse=True)

    header = (f"  {'Structure':>10}  {'Entry':>6}  {'TP':>5}  {'MaxH':>5}  "
              f"{'Avg PnL':>9}  {'Med PnL':>9}  {'Win%':>6}  {'Total':>10}  {'n':>4}")
    print(header)
    print("  " + "-" * (len(header) - 2))

    for r in ranked[:n]:
        print(
            f"  {r['label']:>10}  {r['entry']:>6}  ${r['tp']:>4}  {r['max_hold']:>4}h  "
            f"${r['avg_pnl']:>8.0f}  ${r['median_pnl']:>8.0f}  "
            f"{r['win_rate']:>5.0%}  ${r['total_pnl']:>9.0f}  {r['n']:>4}"
        )

    return ranked


def print_structure_summary(stats):
    """Print overall performance by structure (aggregated across all params)."""
    print()
    print("=" * 80)
    print("  PERFORMANCE BY STRUCTURE (all entry times, TP targets, hold windows)")
    print("=" * 80)

    by_structure = defaultdict(list)
    for key, s in stats.items():
        offset = key[0]
        by_structure[offset].append(s["avg_pnl"])

    print(f"\n  {'Structure':<20} {'Avg of Avg PnL':>15} {'n combos':>10}")
    print(f"  {'-'*50}")
    for offset in OFFSETS:
        pnls = by_structure.get(offset, [])
        if pnls:
            label = "ATM straddle" if offset == 0 else f"Strangle ±{offset}"
            print(f"  {label:<20} ${statistics.mean(pnls):>14.0f} {len(pnls):>10}")


def print_entry_hour_summary(stats):
    """Print best entry hour (best avg P&L across all structures and params)."""
    print()
    print("=" * 80)
    print("  BEST ENTRY HOURS (best avg P&L combo per hour)")
    print("=" * 80)

    best_by_hour = {}
    for key, s in stats.items():
        offset, entry_h, tp, max_hold = key
        if s["n"] < 5:
            continue
        if entry_h not in best_by_hour or s["avg_pnl"] > best_by_hour[entry_h]["avg_pnl"]:
            label = "straddle" if offset == 0 else f"±{offset}"
            best_by_hour[entry_h] = {
                "avg_pnl": s["avg_pnl"],
                "label": label,
                "tp": tp,
                "max_hold": max_hold,
                "win_rate": s["win_rate"],
                "n": s["n"],
            }

    print(f"\n  {'Hour':>6} {'Best Structure':>15} {'TP':>6} {'MaxH':>5} {'Avg PnL':>10} {'Win%':>6}")
    print(f"  {'-'*55}")
    for h in range(24):
        if h in best_by_hour:
            b = best_by_hour[h]
            print(f"  {h:02d}:00  {b['label']:>15} ${b['tp']:>5} {b['max_hold']:>4}h "
                  f"${b['avg_pnl']:>9.0f} {b['win_rate']:>5.0%}")


# ── HTML Report ───────────────────────────────────────────────────

def generate_html(stats, ranked, meta):
    """Generate a styled HTML report with heatmaps and rankings."""

    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
           max-width: 1600px; margin: 20px auto; padding: 0 20px; background: #fafafa; color: #222; }
    h1 { border-bottom: 3px solid #333; padding-bottom: 10px; }
    h2 { margin-top: 40px; color: #333; }
    h3 { margin-top: 25px; color: #555; }
    .subtitle { color: #666; margin-top: -10px; font-style: italic; }
    .meta { background: #eef; padding: 12px 18px; border-radius: 6px; margin: 16px 0; }
    .meta span { margin-right: 30px; }
    .summary-box { background: #e8f5e9; border: 2px solid #4caf50; border-radius: 8px;
                   padding: 16px 24px; margin: 20px 0; }
    .summary-box h3 { margin: 0 0 8px; color: #2e7d32; }
    .summary-box p { margin: 4px 0; font-size: 15px; }
    .cost-box { background: #fff3e0; border: 2px solid #ff9800; border-radius: 8px;
                padding: 16px 24px; margin: 20px 0; }
    .cost-box h3 { margin: 0 0 8px; color: #e65100; }
    .table-wrap { overflow-x: auto; }
    table { border-collapse: collapse; font-size: 13px; margin: 10px 0 30px; }
    th, td { padding: 5px 8px; text-align: right; border: 1px solid #ccc; white-space: nowrap; }
    th { background: #333; color: #fff; font-weight: 600; position: sticky; top: 0; }
    .entry-col { text-align: left; font-weight: 600; background: #f0f0f0 !important;
                 color: #333 !important; min-width: 55px; }
    .empty { color: #bbb; background: #f8f8f8; }
    table.ranked { font-size: 14px; }
    table.ranked td { text-align: center; }
    table.ranked tr:nth-child(2) td { background: #fff9c4; font-weight: 700; }
    table.ranked tr:nth-child(3) td { background: #fff9c4; }
    table.ranked tr:nth-child(4) td { background: #fff9c4; }
    .positive { color: #2e7d32; font-weight: 600; }
    .negative { color: #c62828; }
    .note { background: #fff3e0; padding: 10px 16px; border-left: 4px solid #ff9800;
            margin: 20px 0; border-radius: 4px; font-size: 14px; }
    """

    def heatmap_color(val, vmin, vmax):
        if val is None or vmax == vmin:
            return "#f8f8f8"
        t = (val - vmin) / (vmax - vmin)
        if t < 0.5:
            r, g = 255, int(255 * (t * 2))
        else:
            r, g = int(255 * (2 - t * 2)), 255
        return f"rgb({r},{g},80)"

    def build_heatmap_table(title, subtitle, row_labels, col_labels, data_fn):
        """Build a heatmap table. data_fn(row, col) → value or None."""
        all_vals = []
        for r in row_labels:
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    all_vals.append(v)
        if not all_vals:
            return ""
        vmin, vmax = min(all_vals), max(all_vals)

        rows = [f'<h3>{title}</h3>']
        if subtitle:
            rows.append(f'<p class="subtitle">{subtitle}</p>')
        rows.append('<div class="table-wrap"><table>')
        rows.append('<tr><th class="entry-col"></th>')
        for c in col_labels:
            rows.append(f'<th>{c}</th>')
        rows.append('</tr>')

        for r in row_labels:
            rows.append(f'<tr><td class="entry-col">{r}</td>')
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    bg = heatmap_color(v, vmin, vmax)
                    brightness = (0.299 * int(bg[4:].split(',')[0]) +
                                  0.587 * int(bg.split(',')[1]) +
                                  0.114 * int(bg.split(',')[2].rstrip(')')))
                    tc = "#000" if brightness > 140 else "#fff"
                    css_class = "positive" if v > 0 else "negative" if v < 0 else ""
                    rows.append(f'<td style="background:{bg};color:{tc}">${v:,.0f}</td>')
                else:
                    rows.append('<td class="empty">—</td>')
            rows.append('</tr>')
        rows.append('</table></div>')
        return '\n'.join(rows)

    parts = []
    parts.append(f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>0DTE Option Structure Backtest</title>
<style>{css}</style>
</head><body>
<h1>0DTE Option Structure Backtest</h1>
<div class="meta">
  <span><strong>Generated:</strong> {meta['generated']}</span>
  <span><strong>Data:</strong> {meta['weeks']} weeks of Binance 1h candles</span>
  <span><strong>Date range:</strong> {meta['date_range'][0]} to {meta['date_range'][1]}</span>
  <span><strong>Weekdays:</strong> {meta['n_days']}</span>
</div>

<div class="cost-box">
  <h3>Cost Model</h3>
  <p><strong>Fees:</strong> $14/contract × 2 legs × 2 (open+close) = <strong>$56</strong> per round-trip</p>
  <p><strong>Slippage:</strong> 2.1% × 2 ways = 4.2% of entry premium</p>
  <p><strong>Decay:</strong> √t model calibrated from real Coincall 0DTE snapshot data</p>
  <p><strong>Structures:</strong> ATM straddle + strangles ±$500 to ±$3,000</p>
</div>

<div class="note">
  <strong>Methodology:</strong> P&L = gross_gain(|BTC move|) &minus; theta_lost(hold_hours) &minus; trading_cost.<br>
  No double-counting of time value. TP detection uses running max excursion from candle highs/lows.
  Forced exits at max hold use the close-to-close BTC move (not peak). Theta modeled via &radic;t decay
  calibrated from real Coincall 0DTE snapshot data.
</div>
""")

    # ── Top 20 ranked table ─────────────────────────────────────
    parts.append('<h2>Top 20 Parameter Combinations</h2>')
    parts.append('<p class="subtitle">Ranked by average P&L per trade (min 5 samples)</p>')
    parts.append('<table class="ranked">')
    parts.append('<tr><th>#</th><th>Structure</th><th>Entry</th><th>TP Target</th>'
                 '<th>Max Hold</th><th>Avg PnL</th><th>Median PnL</th>'
                 '<th>Win Rate</th><th>Total PnL</th><th>Trades</th></tr>')
    for i, r in enumerate(ranked[:20], 1):
        pnl_class = "positive" if r["avg_pnl"] > 0 else "negative"
        parts.append(
            f'<tr><td>{i}</td><td>{r["label"]}</td><td>{r["entry"]}</td>'
            f'<td>${r["tp"]}</td><td>{r["max_hold"]}h</td>'
            f'<td class="{pnl_class}">${r["avg_pnl"]:,.0f}</td>'
            f'<td class="{pnl_class}">${r["median_pnl"]:,.0f}</td>'
            f'<td>{r["win_rate"]:.0%}</td>'
            f'<td class="{pnl_class}">${r["total_pnl"]:,.0f}</td>'
            f'<td>{r["n"]}</td></tr>'
        )
    parts.append('</table>')

    # ── Heatmaps per structure: Entry Hour × Max Hold ───────────
    parts.append('<h2>Heatmaps: Best Avg P&L by Entry Hour × Max Hold</h2>')
    parts.append('<p class="subtitle">For each (entry_hour, max_hold), shows the best avg P&L across all TP targets</p>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else f"Strangle ±${offset}"

        def make_data_fn(off):
            def data_fn(row_label, col_label):
                entry_h = int(row_label.split(":")[0])
                max_hold = int(col_label.replace("h", ""))
                best = None
                for tp in TP_TARGETS:
                    key = (off, entry_h, tp, max_hold)
                    s = stats.get(key)
                    if s and s["n"] >= 5:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                return best
            return data_fn

        row_labels = [f"{h:02d}:00" for h in range(24)]
        col_labels = [f"{h}h" for h in MAX_HOLDS]
        parts.append(build_heatmap_table(
            label, f"Best avg P&L across TP targets",
            row_labels, col_labels, make_data_fn(offset)))

    # ── Heatmaps per structure: TP Target × Max Hold (best entry hour) ─
    parts.append('<h2>Heatmaps: TP Target × Max Hold (best entry hour)</h2>')
    parts.append('<p class="subtitle">For each (TP, max_hold), shows the best avg P&L across all entry hours</p>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else f"Strangle ±${offset}"

        def make_tp_fn(off):
            def data_fn(row_label, col_label):
                tp = int(row_label.replace("$", ""))
                max_hold = int(col_label.replace("h", ""))
                best = None
                for entry_h in range(24):
                    key = (off, entry_h, tp, max_hold)
                    s = stats.get(key)
                    if s and s["n"] >= 5:
                        if best is None or s["avg_pnl"] > best:
                            best = s["avg_pnl"]
                return best
            return data_fn

        row_labels = [f"${tp}" for tp in TP_TARGETS]
        col_labels = [f"{h}h" for h in MAX_HOLDS]
        parts.append(build_heatmap_table(
            label, f"Best avg P&L across entry hours",
            row_labels, col_labels, make_tp_fn(offset)))

    # ── Summary: best per structure ─────────────────────────────
    parts.append('<h2>Best Configuration Per Structure</h2>')
    parts.append('<table class="ranked">')
    parts.append('<tr><th>Structure</th><th>Entry</th><th>TP</th><th>Max Hold</th>'
                 '<th>Avg PnL</th><th>Win Rate</th><th>Entry Premium</th>'
                 '<th>Cost</th><th>Trades</th></tr>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else f"Strangle ±${offset}"
        best_key = None
        best_avg = -1e18
        for key, s in stats.items():
            if key[0] == offset and s["n"] >= 5 and s["avg_pnl"] > best_avg:
                best_avg = s["avg_pnl"]
                best_key = key
        if best_key:
            s = stats[best_key]
            off, eh, tp, mh = best_key
            dte = hours_to_expiry(eh)
            prem = premium_at_dte(off, dte)
            c = total_cost(prem)
            pnl_class = "positive" if s["avg_pnl"] > 0 else "negative"
            parts.append(
                f'<tr><td>{label}</td><td>{eh:02d}:00</td><td>${tp}</td>'
                f'<td>{mh}h</td><td class="{pnl_class}">${s["avg_pnl"]:,.0f}</td>'
                f'<td>{s["win_rate"]:.0%}</td><td>${prem:,.0f}</td>'
                f'<td>${c:,.0f}</td><td>{s["n"]}</td></tr>'
            )
    parts.append('</table>')

    parts.append('\n</body></html>')
    return '\n'.join(parts)


# ── Main ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="0DTE Option Structure Backtest")
    parser.add_argument("--weeks", type=int, default=4, help="Weeks of Binance data (default: 4)")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML export")
    args = parser.parse_args()

    # Fetch data
    candles = fetch_binance_candles(weeks=args.weeks)
    weekday_candles = filter_weekdays(candles)

    dates = sorted(set(c["date"] for c in weekday_candles))
    n_days = len(dates)
    print(f"  {n_days} weekdays: {dates[0]} to {dates[-1]}")

    # Show cost model
    print("\n  Cost model:")
    for offset in OFFSETS:
        label = "straddle" if offset == 0 else f"±{offset}"
        prem_14h = premium_at_dte(offset, 18)  # example: entry at 14:00
        c = total_cost(prem_14h)
        print(f"    {label:>10}: premium @14:00 = ${prem_14h:.0f}, total cost = ${c:.0f} "
              f"(fees $56 + slippage ${SLIPPAGE_PCT * prem_14h:.0f})")

    # Run backtest
    print("\n  Running backtest...")
    print(f"    Structures: {len(OFFSETS)}")
    print(f"    Entry hours: 24")
    print(f"    TP targets: {len(TP_TARGETS)}")
    print(f"    Max holds: {len(MAX_HOLDS)}")
    total_combos = len(OFFSETS) * 24 * len(TP_TARGETS) * len(MAX_HOLDS)
    print(f"    Total combos: {total_combos:,}")

    results = run_backtest(weekday_candles)
    stats = compute_stats(results)

    print(f"\n  Stats computed for {len(stats):,} combos with data")

    # Console output
    ranked = print_top_combos(stats)
    print_structure_summary(stats)
    print_entry_hour_summary(stats)

    # HTML report
    if not args.no_html:
        meta = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "weeks": args.weeks,
            "n_days": n_days,
            "date_range": [dates[0], dates[-1]],
        }
        html = generate_html(stats, ranked, meta)
        html_path = os.path.join(SCRIPT_DIR, "backtest_report.html")
        with open(html_path, "w") as f:
            f.write(html)
        print(f"\n  HTML report → {html_path}")


if __name__ == "__main__":
    main()
