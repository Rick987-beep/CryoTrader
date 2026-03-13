#!/usr/bin/env python3
"""
Hourly Excursion Analysis — Optimal Entry/Exit Window Finder

Fetches 4 weeks of BTCUSDT 1h candles from Binance, then computes:
  A) Max Excursion Table — for each (entry_hour, exit_hour) pair,
     the average max move from entry in either direction.
  B) Efficiency Table — excursion per hour of hold time.
  C) Top-10 windows ranked by efficiency.

Uses highs and lows within each window (not just open-to-close),
because our strategy uses a take-profit target that can be hit
at any point during the window.

Usage:
    python analysis/hourly_excursion.py
    python analysis/hourly_excursion.py --weeks 8
    python analysis/hourly_excursion.py --max-window 8
"""

import argparse
import json
import os
import sys
import statistics
from collections import defaultdict
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("ERROR: requests library required. pip install requests")
    sys.exit(1)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
HTML_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_binance_candles(weeks=4):
    """Fetch 1h BTCUSDT perp candles from Binance. Returns list of dicts."""
    limit = weeks * 7 * 24  # hours in N weeks
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
            "weekday": dt.weekday(),  # 0=Mon, 6=Sun
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


def group_by_date(candles):
    """Group candles by date. Returns {date_str: [candles sorted by hour]}."""
    by_date = defaultdict(list)
    for c in candles:
        by_date[c["date"]].append(c)
    # Sort each day's candles by hour
    for d in by_date:
        by_date[d].sort(key=lambda c: c["hour"])
    return dict(by_date)


def compute_excursions(candles, max_window=12):
    """
    For each (entry_hour, window_length) pair, compute max excursion
    across all days in the dataset.

    Uses a flat chronological candle list to correctly handle windows
    that cross midnight.

    Returns: {(entry_hour, exit_hour, window_length): [list of excursion values]}
    """
    results = defaultdict(list)

    # Build a position-indexed list — candles must be sorted chronologically
    sorted_candles = sorted(candles, key=lambda c: c["dt"])

    for i, entry_candle in enumerate(sorted_candles):
        entry_hour = entry_candle["hour"]
        s_entry = entry_candle["open"]

        running_high = -1e18
        running_low = 1e18

        for w in range(1, max_window + 1):
            j = i + w - 1  # index of the candle w-1 hours after entry
            if j >= len(sorted_candles):
                break

            c = sorted_candles[j]

            # Verify this candle is exactly (w-1) hours after entry
            expected_dt = entry_candle["dt"] + timedelta(hours=w - 1)
            if c["dt"] != expected_dt:
                break  # gap in data (weekend, missing candle) — stop this window

            running_high = max(running_high, c["high"])
            running_low = min(running_low, c["low"])

            excursion_up = running_high - s_entry
            excursion_dn = s_entry - running_low
            max_excursion = max(excursion_up, excursion_dn)

            exit_hour = (entry_hour + w) % 24
            results[(entry_hour, exit_hour, w)].append(max_excursion)

    return results


def print_table(title, hours_range, max_window, cell_fn, col_width=8):
    """Print a triangular table with entry hours as rows, window lengths as columns."""
    print()
    print("=" * 80)
    print(f"  {title}")
    print("=" * 80)
    print()

    # Header: window lengths
    header = f"{'Entry':>7}"
    for w in range(1, max_window + 1):
        header += f"  {w:>{col_width-2}}h"
    print(header)
    print("-" * len(header))

    for entry_h in hours_range:
        row = f"  {entry_h:02d}:00"
        for w in range(1, max_window + 1):
            exit_h = (entry_h + w) % 24
            val = cell_fn(entry_h, exit_h, w)
            if val is not None:
                row += f"  {val:>{col_width}.0f}"
            else:
                row += f"  {'—':>{col_width}}"
        print(row)


def heatmap_color(val, vmin, vmax):
    """Return CSS background color from green (high) to red (low)."""
    if val is None or vmax == vmin:
        return "#f8f8f8"
    t = (val - vmin) / (vmax - vmin)  # 0..1
    # Red (low) → Yellow (mid) → Green (high)
    if t < 0.5:
        r, g = 255, int(255 * (t * 2))
    else:
        r, g = int(255 * (2 - t * 2)), 255
    return f"rgb({r},{g},80)"


def generate_html(stats, excursions, ranked, ranked_long, meta, max_window):
    """Generate a styled HTML report with heatmap tables."""
    hours_range = range(0, 24)

    def _build_html_table(title, subtitle, cell_fn, fmt="${:,.0f}"):
        """Build one heatmap table as HTML string."""
        # Collect all values for color scaling
        all_vals = []
        for entry_h in hours_range:
            for w in range(1, max_window + 1):
                exit_h = (entry_h + w) % 24
                v = cell_fn(entry_h, exit_h, w)
                if v is not None:
                    all_vals.append(v)
        if not all_vals:
            return ""
        vmin, vmax = min(all_vals), max(all_vals)

        rows = []
        rows.append(f'<h2>{title}</h2>')
        if subtitle:
            rows.append(f'<p class="subtitle">{subtitle}</p>')
        rows.append('<div class="table-wrap"><table>')
        # Header
        rows.append('<tr><th class="entry-col">Entry</th>')
        for w in range(1, max_window + 1):
            rows.append(f'<th>{w}h</th>')
        rows.append('</tr>')
        # Data rows
        for entry_h in hours_range:
            rows.append(f'<tr><td class="entry-col">{entry_h:02d}:00</td>')
            for w in range(1, max_window + 1):
                exit_h = (entry_h + w) % 24
                v = cell_fn(entry_h, exit_h, w)
                if v is not None:
                    bg = heatmap_color(v, vmin, vmax)
                    # Dark text on light bg, light on dark
                    brightness = 0.299 * int(bg[4:].split(',')[0]) + 0.587 * int(bg.split(',')[1]) + 0.114 * int(bg.split(',')[2].rstrip(')'))
                    text_color = "#000" if brightness > 140 else "#fff"
                    formatted = fmt.format(v)
                    rows.append(f'<td style="background:{bg};color:{text_color}">{formatted}</td>')
                else:
                    rows.append('<td class="empty">—</td>')
            rows.append('</tr>')
        rows.append('</table></div>')
        return '\n'.join(rows)

    def _build_ranked_table(title, subtitle, data, limit=10):
        """Build a ranked list table."""
        rows = []
        rows.append(f'<h2>{title}</h2>')
        if subtitle:
            rows.append(f'<p class="subtitle">{subtitle}</p>')
        rows.append('<table class="ranked">')
        rows.append('<tr><th>#</th><th>Entry</th><th>Exit</th><th>Hours</th>'
                     '<th>Avg Excursion</th><th>Median Excursion</th>'
                     '<th>Efficiency $/hr</th><th>n</th></tr>')
        for i, r in enumerate(data[:limit], 1):
            rows.append(
                f'<tr><td>{i}</td><td>{r["entry"]}</td><td>{r["exit"]}</td>'
                f'<td>{r["hours"]}</td><td>${r["avg_excursion"]:,.0f}</td>'
                f'<td>${r["median_excursion"]:,.0f}</td>'
                f'<td>${r["efficiency"]:,.0f}</td><td>{r["n"]}</td></tr>'
            )
        rows.append('</table>')
        return '\n'.join(rows)

    # Build cell functions
    def mean_fn(e, x, w):
        s = stats.get((e, x, w))
        return s["mean"] if s else None

    def eff_fn(e, x, w):
        s = stats.get((e, x, w))
        return s["mean"] / w if s else None

    def median_fn(e, x, w):
        s = stats.get((e, x, w))
        return s["median"] if s else None

    # CSS
    css = """
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
           max-width: 1400px; margin: 20px auto; padding: 0 20px; background: #fafafa; color: #222; }
    h1 { border-bottom: 3px solid #333; padding-bottom: 10px; }
    h2 { margin-top: 40px; color: #333; }
    .subtitle { color: #666; margin-top: -10px; font-style: italic; }
    .meta { background: #eef; padding: 12px 18px; border-radius: 6px; margin: 16px 0; }
    .meta span { margin-right: 30px; }
    .summary-box { background: #e8f5e9; border: 2px solid #4caf50; border-radius: 8px;
                   padding: 16px 24px; margin: 20px 0; }
    .summary-box h3 { margin: 0 0 8px; color: #2e7d32; }
    .summary-box p { margin: 4px 0; font-size: 15px; }
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
    .note { background: #fff3e0; padding: 10px 16px; border-left: 4px solid #ff9800;
            margin: 20px 0; border-radius: 4px; font-size: 14px; }
    """

    generated = meta.get("generated", "")
    date_range = meta.get("date_range", ["?", "?"])
    n_days = meta.get("n_days", "?")
    weeks = meta.get("weeks", "?")

    parts = []
    parts.append(f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>Hourly Excursion Analysis — BTC</title>
<style>{css}</style>
</head><body>
<h1>BTC Hourly Excursion Analysis</h1>
<div class="meta">
  <span><strong>Generated:</strong> {generated}</span>
  <span><strong>Data:</strong> {weeks} weeks ({date_range[0]} to {date_range[1]})</span>
  <span><strong>Weekdays:</strong> {n_days}</span>
  <span><strong>Source:</strong> Binance BTCUSDT Perp 1h candles (OHLC)</span>
</div>

<div class="note">
  <strong>What is Max Excursion?</strong> For each window, we track the highest high and lowest low
  across all hourly candles. Max excursion = max(highest_high − entry, entry − lowest_low).
  This captures the peak move in either direction, including intra-candle wicks — the move
  that would hit a take-profit target.
</div>
""")

    # Summary box
    if ranked:
        best = ranked[0]
        best_long = ranked_long[0] if ranked_long else None
        parts.append('<div class="summary-box">')
        parts.append('<h3>Key Findings</h3>')
        parts.append(f'<p><strong>Best short window:</strong> {best["entry"]} → {best["exit"]} '
                      f'({best["hours"]}h) — avg ${best["avg_excursion"]:,.0f} excursion, '
                      f'${best["efficiency"]:,.0f}/hr efficiency</p>')
        if best_long:
            parts.append(f'<p><strong>Best 4h+ window:</strong> {best_long["entry"]} → {best_long["exit"]} '
                          f'({best_long["hours"]}h) — avg ${best_long["avg_excursion"]:,.0f} excursion, '
                          f'${best_long["efficiency"]:,.0f}/hr efficiency</p>')
        parts.append('</div>')

    # Tables
    parts.append(_build_html_table(
        "Average Max Excursion ($)",
        "Mean of the furthest BTC moved from entry price within each window",
        mean_fn))

    parts.append(_build_html_table(
        "Efficiency ($/hr)",
        "Excursion divided by window length — helps compare short vs long windows on theta cost",
        eff_fn))

    parts.append(_build_html_table(
        "Median Max Excursion ($)",
        "Median is more robust to outlier days (e.g. one massive crash pulling up the average)",
        median_fn))

    # Ranked tables
    parts.append(_build_ranked_table(
        "Top-10 Windows by Efficiency",
        "Minimum 2h window to filter single-candle noise",
        ranked))

    parts.append(_build_ranked_table(
        "Top-10 Windows by Efficiency (4h+ only)",
        "Longer windows for strategies that need more time to play out",
        ranked_long))

    parts.append('\n</body></html>')
    return '\n'.join(parts)


def main():
    parser = argparse.ArgumentParser(description="Hourly excursion analysis")
    parser.add_argument("--weeks", type=int, default=4, help="Weeks of data (default: 4)")
    parser.add_argument("--max-window", type=int, default=12, help="Max hold window in hours (default: 12)")
    parser.add_argument("--save", action="store_true", help="Save raw data to JSON")
    parser.add_argument("--no-html", action="store_true", help="Skip HTML export")
    args = parser.parse_args()

    candles = fetch_binance_candles(weeks=args.weeks)
    weekday_candles = filter_weekdays(candles)
    by_date = group_by_date(weekday_candles)

    n_days = len(by_date)
    dates = sorted(by_date.keys())
    print(f"  {n_days} weekdays: {dates[0]} to {dates[-1]}")

    excursions = compute_excursions(weekday_candles, max_window=args.max_window)

    # Compute stats
    stats = {}
    for key, vals in excursions.items():
        entry_h, exit_h, w = key
        stats[key] = {
            "mean": statistics.mean(vals),
            "median": statistics.median(vals),
            "n": len(vals),
            "w": w,
        }

    hours_range = range(0, 24)

    # ── Table A: Average Max Excursion ────────────────────────────────
    def mean_excursion(entry_h, exit_h, w):
        s = stats.get((entry_h, exit_h, w))
        return s["mean"] if s else None

    print_table(
        "AVG MAX EXCURSION ($) — mean furthest BTC moved from entry price",
        hours_range, args.max_window, mean_excursion
    )

    # ── Table B: Efficiency ($/hr) ────────────────────────────────────
    def efficiency(entry_h, exit_h, w):
        s = stats.get((entry_h, exit_h, w))
        return s["mean"] / w if s else None

    print_table(
        "EFFICIENCY ($/hr) — excursion per hour of theta exposure",
        hours_range, args.max_window, efficiency
    )

    # ── Table C: Median Max Excursion ─────────────────────────────────
    def median_excursion(entry_h, exit_h, w):
        s = stats.get((entry_h, exit_h, w))
        return s["median"] if s else None

    print_table(
        "MEDIAN MAX EXCURSION ($) — robust to outliers",
        hours_range, args.max_window, median_excursion
    )

    # ── Table D: Top-10 windows by efficiency ─────────────────────────
    ranked = []
    for key, s in stats.items():
        entry_h, exit_h, w = key
        if w >= 2:  # at least 2h window
            ranked.append({
                "entry": f"{entry_h:02d}:00",
                "exit": f"{exit_h:02d}:00",
                "hours": w,
                "avg_excursion": s["mean"],
                "median_excursion": s["median"],
                "efficiency": s["mean"] / w,
                "n": s["n"],
            })

    ranked.sort(key=lambda x: x["efficiency"], reverse=True)
    ranked_long = [r for r in ranked if r["hours"] >= 4]

    print()
    print("=" * 80)
    print("  TOP-10 WINDOWS BY EFFICIENCY ($/hr)")
    print("  (minimum 2h window to filter noise)")
    print("=" * 80)
    print()

    print(f"  {'Entry':>7}  {'Exit':>7}  {'Hours':>5}  {'Avg Exc':>10}  {'Med Exc':>10}  {'Eff $/hr':>10}  {'n':>4}")
    print("  " + "-" * 62)
    for r in ranked[:10]:
        print(
            f"  {r['entry']:>7}  {r['exit']:>7}  {r['hours']:>5}  "
            f"${r['avg_excursion']:>8,.0f}  ${r['median_excursion']:>8,.0f}  "
            f"${r['efficiency']:>8,.0f}  {r['n']:>4}"
        )

    print()
    print("=" * 80)
    print("  TOP-10 WINDOWS BY EFFICIENCY ($/hr) — 4h+ windows only")
    print("=" * 80)
    print()

    print(f"  {'Entry':>7}  {'Exit':>7}  {'Hours':>5}  {'Avg Exc':>10}  {'Med Exc':>10}  {'Eff $/hr':>10}  {'n':>4}")
    print("  " + "-" * 62)
    for r in ranked_long[:10]:
        print(
            f"  {r['entry']:>7}  {r['exit']:>7}  {r['hours']:>5}  "
            f"${r['avg_excursion']:>8,.0f}  ${r['median_excursion']:>8,.0f}  "
            f"${r['efficiency']:>8,.0f}  {r['n']:>4}"
        )

    # ── Summary ───────────────────────────────────────────────────────
    if ranked:
        best = ranked[0]
        best_long = ranked_long[0] if ranked_long else None
        print()
        print("=" * 80)
        print(f"  BEST SHORT WINDOW: {best['entry']} → {best['exit']} ({best['hours']}h)")
        print(f"    Avg excursion: ${best['avg_excursion']:,.0f}   Efficiency: ${best['efficiency']:,.0f}/hr")
        if best_long:
            print(f"  BEST 4h+ WINDOW:  {best_long['entry']} → {best_long['exit']} ({best_long['hours']}h)")
            print(f"    Avg excursion: ${best_long['avg_excursion']:,.0f}   Efficiency: ${best_long['efficiency']:,.0f}/hr")
        print("=" * 80)

    # ── Save raw data ─────────────────────────────────────────────────
    if args.save:
        os.makedirs(DATA_DIR, exist_ok=True)
        save_data = {
            "generated": datetime.now(timezone.utc).isoformat(),
            "weeks": args.weeks,
            "n_days": n_days,
            "date_range": [dates[0], dates[-1]],
            "windows": []
        }
        for key, s in sorted(stats.items()):
            entry_h, exit_h, w = key
            save_data["windows"].append({
                "entry_hour": entry_h,
                "exit_hour": exit_h,
                "window_hours": w,
                "mean_excursion": round(s["mean"], 2),
                "median_excursion": round(s["median"], 2),
                "n": s["n"],
            })
        filepath = os.path.join(HTML_DIR, "hourly_excursion.json")
        with open(filepath, "w") as f:
            json.dump(save_data, f, indent=2)
        print(f"\nRaw data saved → {filepath}")

    # ── HTML export ───────────────────────────────────────────────────
    if not args.no_html:
        meta = {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "weeks": args.weeks,
            "n_days": n_days,
            "date_range": [dates[0], dates[-1]],
        }
        html = generate_html(stats, excursions, ranked, ranked_long, meta, args.max_window)
        html_path = os.path.join(HTML_DIR, "hourly_excursion_report.html")
        with open(html_path, "w") as f:
            f.write(html)
        print(f"HTML report saved → {html_path}")


if __name__ == "__main__":
    main()
