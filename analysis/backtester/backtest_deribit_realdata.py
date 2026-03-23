#!/usr/bin/env python3
"""
0DTE Backtest — Real Deribit Bid/Ask Data + Index-Move Triggers
===============================================================

Backtest using actual Deribit option prices from tardis.dev.
This is the ground-truth reference — no pricing model assumptions,
just real bid/ask spreads as they were on the exchange.

Strategy (same logic as backtest_blackscholes.py):
  1. ENTRY — Buy structure at ask price at a given UTC hour.
  2. TRIGGER — When |BTC_now - BTC_entry| >= trigger, close at bid.
  3. EXIT — If trigger doesn't fire within max_hold, forced sell at bid.

Parameter grid:
  - Structures: ATM straddle, strangles ±500 to ±3000
  - Entry hours: 00:00–20:00 UTC
  - Index triggers: $300–$2000
  - Max hold: 1–12 hours
  - Resolution: 5-minute checkpoints

Data:   Real Deribit bid/ask from tardis.dev (free tier: 1st of month only)
Cost:   $56 round-trip fees (Coincall model: $14/contract × 2 legs × 2)
Note:   Limited to 1 day of data (March 1, 2025). Use backtest_blackscholes.py
        for multi-week statistical analysis with synthetic pricing.

Usage:
    .venv/bin/python analysis/optimal_entry_window/backtest_deribit_realdata.py

See also: analysis/tardis_options/ for the data download/extraction pipeline.
"""

import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import numpy as np

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.join(SCRIPT_DIR, "..", "..")
sys.path.insert(0, PROJECT_ROOT)

from analysis.tardis_options import HistoricOptionChain

# ── Configuration ─────────────────────────────────────────────────

OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]

# Index-move triggers: sell when |spot_now - spot_entry| >= trigger
INDEX_TRIGGERS = [300, 400, 500, 600, 700, 800, 1000, 1200, 1500, 2000]

MAX_HOLDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

FEE_PER_CONTRACT = 14.0
NUM_LEGS = 2
ROUND_TRIP_FEES = FEE_PER_CONTRACT * NUM_LEGS * 2  # $56

MAX_ENTRY_HOUR = 20
CHECK_INTERVAL_MIN = 5
MAX_INDEX_DIVERGENCE = 100.0

EXPIRY = "2MAR25"
EXPIRY_DT = datetime(2025, 3, 2, 8, 0, tzinfo=timezone.utc)

PARQUET_PATH = os.path.join(
    SCRIPT_DIR, "..", "tardis_options", "data",
    "btc_0dte_1dte_2025-03-01.parquet",
)


# ── Binance Crosscheck ───────────────────────────────────────────

def fetch_binance_5min_candles(date_str="2025-03-01"):
    start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = start_ms + 86400 * 1000

    url = "https://fapi.binance.com/fapi/v1/klines"
    params = {
        "symbol": "BTCUSDT",
        "interval": "5m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 500,
    }

    print("  Fetching Binance 5m candles for crosscheck...")
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    raw = resp.json()

    binance = {}
    for r in raw:
        dt = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc)
        binance[dt] = float(r[4])
    print("    Got %d Binance 5m candles (%s to %s UTC)" % (
        len(binance),
        min(binance.keys()).strftime("%H:%M"),
        max(binance.keys()).strftime("%H:%M"),
    ))
    return binance


def find_binance_price(binance_candles, dt):
    bar_minute = (dt.minute // 5) * 5
    bar_dt = dt.replace(minute=bar_minute, second=0, microsecond=0)
    price = binance_candles.get(bar_dt)
    if price is not None:
        return price, bar_dt
    prev_bar = bar_dt - timedelta(minutes=5)
    price = binance_candles.get(prev_bar)
    if price is not None:
        return price, prev_bar
    return None, None


# ── Helpers ───────────────────────────────────────────────────────

def find_nearest_strike(strikes, target):
    idx = np.searchsorted(strikes, target)
    candidates = []
    if idx > 0:
        candidates.append(strikes[idx - 1])
    if idx < len(strikes):
        candidates.append(strikes[idx])
    if not candidates:
        return None
    return float(min(candidates, key=lambda s: abs(s - target)))


def offset_label(offset):
    return "straddle" if offset == 0 else "±%d" % offset


def get_entry_cost(chain, t, call_strike, put_strike):
    call = chain.get(t, EXPIRY, call_strike, is_call=True)
    put = chain.get(t, EXPIRY, put_strike, is_call=False)
    if call is None or put is None:
        return None
    spot = float(call["underlying_price"])
    call_ask = float(call["ask_price"])
    put_ask = float(put["ask_price"])
    call_bid = float(call["bid_price"])
    put_bid = float(put["bid_price"])
    call_mark = float(call["mark_price"])
    put_mark = float(put["mark_price"])
    if np.isnan(call_ask) or np.isnan(put_ask):
        return None
    return {
        "ask_usd": (call_ask + put_ask) * spot,
        "bid_usd": (call_bid + put_bid) * spot if not (np.isnan(call_bid) or np.isnan(put_bid)) else None,
        "mark_usd": (call_mark + put_mark) * spot,
        "spot": spot,
        "call": call,
        "put": put,
        "call_strike": call_strike,
        "put_strike": put_strike,
    }


def get_exit_value(chain, t, call_strike, put_strike):
    call = chain.get(t, EXPIRY, call_strike, is_call=True)
    put = chain.get(t, EXPIRY, put_strike, is_call=False)
    if call is None or put is None:
        return None
    spot = float(call["underlying_price"])
    call_bid = float(call["bid_price"])
    put_bid = float(put["bid_price"])
    if np.isnan(call_bid) or np.isnan(put_bid):
        return None
    return {
        "bid_usd": (call_bid + put_bid) * spot,
        "spot": spot,
    }


# ── Backtest Engine ───────────────────────────────────────────────

def run_backtest(chain, binance_candles):
    """Run backtest with index-move TP triggers.

    Returns:
        results:    dict (offset, entry_hour, trigger, max_hold) → result dict
        entry_info: dict (offset, entry_hour) → entry details
        spot_ranges: dict (entry_hour, hold_hours) → range info
        warnings:   list of crosscheck warning strings
    """
    strikes = np.array(chain.strikes(EXPIRY))
    _, t_end = chain.time_range()
    t_end_us = int(t_end.value // 1000)

    results = {}
    entry_info = {}
    spot_ranges = {}
    warnings = []

    # Pre-compute spot at every 5-min interval
    spot_series = {}
    for m in range(0, 24 * 60, CHECK_INTERVAL_MIN):
        dt = datetime(2025, 3, 1, m // 60, m % 60, tzinfo=timezone.utc)
        us = int(dt.timestamp() * 1_000_000)
        if us > t_end_us:
            break
        spot_series[m] = chain.get_spot(dt)

    for entry_hour in range(MAX_ENTRY_HOUR + 1):
        entry_dt = datetime(2025, 3, 1, entry_hour, 0, tzinfo=timezone.utc)
        entry_us = int(entry_dt.timestamp() * 1_000_000)

        if entry_us > t_end_us:
            continue

        dte_hours = (EXPIRY_DT - entry_dt).total_seconds() / 3600
        if dte_hours <= 1:
            continue

        # Binance crosscheck (informational only)
        spot_deribit = chain.get_spot(entry_dt)
        binance_px, _ = find_binance_price(binance_candles, entry_dt)
        if binance_px is not None:
            divergence = abs(spot_deribit - binance_px)
            if divergence > MAX_INDEX_DIVERGENCE:
                warnings.append(
                    "%02d:00 UTC — Deribit $%.0f vs Binance $%.0f (diff $%.0f)"
                    % (entry_hour, spot_deribit, binance_px, divergence)
                )

        atm = find_nearest_strike(strikes, spot_deribit)

        # BTC range for each hold window
        entry_min = entry_hour * 60
        for hold_h in MAX_HOLDS:
            end_min = entry_min + hold_h * 60
            spots_in_window = []
            for m in range(entry_min, end_min + CHECK_INTERVAL_MIN, CHECK_INTERVAL_MIN):
                if m in spot_series:
                    spots_in_window.append(spot_series[m])
            if spots_in_window:
                lo, hi = min(spots_in_window), max(spots_in_window)
                spot_ranges[(entry_hour, hold_h)] = {
                    "low": lo, "high": hi, "range": hi - lo,
                    "entry_spot": spot_deribit,
                }

        for offset in OFFSETS:
            if offset == 0:
                call_strike = put_strike = atm
            else:
                call_strike = find_nearest_strike(strikes, atm + offset)
                put_strike = find_nearest_strike(strikes, atm - offset)

            if call_strike is None or put_strike is None:
                continue

            entry = get_entry_cost(chain, entry_dt, call_strike, put_strike)
            if entry is None:
                continue

            ask_entry = entry["ask_usd"]
            mark_entry = entry["mark_usd"]
            entry_spot = entry["spot"]

            entry_info[(offset, entry_hour)] = {
                "ask_premium": ask_entry,
                "mark_premium": mark_entry,
                "spread_cost": ask_entry - mark_entry,
                "fees": ROUND_TRIP_FEES,
                "spot": entry_spot,
                "atm": atm,
                "call_strike": entry["call_strike"],
                "put_strike": entry["put_strike"],
                "call_iv": float(entry["call"]["mark_iv"]),
                "put_iv": float(entry["put"]["mark_iv"]),
                "dte_hours": dte_hours,
            }

            # Walk forward at 5-min intervals, recording index move + option value
            max_check_min = int(min(max(MAX_HOLDS), dte_hours - 0.5) * 60)
            trace = []  # list of {minutes, spot, index_move, bid_value, net_pnl}

            for minutes in range(CHECK_INTERVAL_MIN, max_check_min + 1, CHECK_INTERVAL_MIN):
                check_dt = entry_dt + timedelta(minutes=minutes)
                check_us = int(check_dt.timestamp() * 1_000_000)
                if check_us > t_end_us:
                    break

                exit_val = get_exit_value(chain, check_dt, call_strike, put_strike)
                if exit_val is None:
                    continue

                bid_exit = exit_val["bid_usd"]
                spot_now = exit_val["spot"]
                index_move = abs(spot_now - entry_spot)
                net_pnl = bid_exit - ask_entry - ROUND_TRIP_FEES

                trace.append({
                    "minutes": minutes,
                    "hours": minutes / 60,
                    "spot": spot_now,
                    "index_move": index_move,
                    "bid_value": bid_exit,
                    "net_pnl": net_pnl,
                })

            # Evaluate each (trigger, max_hold) combination
            for max_hold in MAX_HOLDS:
                if max_hold > dte_hours - 0.5:
                    continue

                for trigger in INDEX_TRIGGERS:
                    key = (offset, entry_hour, trigger, max_hold)

                    triggered = False
                    exit_pnl = None
                    exit_minutes = None
                    exit_move = None
                    forced_exit_pnl = None
                    forced_exit_min = 0

                    for cv in trace:
                        if cv["hours"] > max_hold + 0.01:
                            break
                        # Check if index moved enough to trigger TP
                        if not triggered and cv["index_move"] >= trigger:
                            triggered = True
                            exit_pnl = cv["net_pnl"]
                            exit_minutes = cv["minutes"]
                            exit_move = cv["index_move"]
                            break  # sell immediately on trigger
                        forced_exit_pnl = cv["net_pnl"]
                        forced_exit_min = cv["minutes"]

                    if triggered:
                        results[key] = {
                            "pnl": exit_pnl,
                            "exit_type": "triggered",
                            "exit_minutes": exit_minutes,
                            "index_move_at_exit": exit_move,
                        }
                    elif forced_exit_pnl is not None:
                        # Check we actually reached the end of the hold window
                        if (max_hold * 60 - forced_exit_min) <= CHECK_INTERVAL_MIN:
                            results[key] = {
                                "pnl": forced_exit_pnl,
                                "exit_type": "expired",
                                "exit_minutes": forced_exit_min,
                                "index_move_at_exit": trace[-1]["index_move"] if trace else 0,
                            }

    return results, entry_info, spot_ranges, warnings


# ── Console Output ────────────────────────────────────────────────

def print_data_summary(chain, entry_info, warnings):
    t_start, t_end = chain.time_range()
    print()
    print("=" * 80)
    print("  0DTE BACKTEST v3 — Index-Move Take Profit")
    print("  2025-03-01 (Saturday) | bid/ask pricing")
    print("=" * 80)
    print("  Data:     %s to %s UTC" % (t_start.strftime("%Y-%m-%d %H:%M"), t_end.strftime("%H:%M")))
    print("  Expiry:   %s (08:00 UTC March 2)" % EXPIRY)
    print("  Source:   Deribit options_chain via tardis.dev")
    print("  Pricing:  BUY at ask, SELL at bid")
    print("  Fees:     $%.0f round-trip ($%.0f/contract x %d legs x 2)" % (
        ROUND_TRIP_FEES, FEE_PER_CONTRACT, NUM_LEGS))
    print("  Cutoff:   entries 00:00–%02d:00 UTC" % MAX_ENTRY_HOUR)
    print("  TP logic: Sell when |index - entry_index| >= trigger")
    print("  Triggers: %s" % INDEX_TRIGGERS)

    spots = [info["spot"] for info in entry_info.values()]
    if spots:
        print("  Spot:     $%s – $%s" % ("{:,.0f}".format(min(spots)), "{:,.0f}".format(max(spots))))

    if warnings:
        print()
        print("  ℹ Index divergence (Deribit vs Binance, >$%.0f):" % MAX_INDEX_DIVERGENCE)
        for w in warnings:
            print("    %s" % w)
        print("    (Entries NOT skipped)")


def print_entry_premiums(entry_info):
    print()
    print("=" * 100)
    print("  ENTRY PREMIUMS (what you pay per structure per hour)")
    print("  Fees: $%.0f round-trip | Spread = ask − mark" % ROUND_TRIP_FEES)
    print("=" * 100)

    hours = sorted(set(h for _, h in entry_info.keys()))

    print("  %5s %5s %9s  %10s  %9s %9s %7s %6s %7s %7s" % (
        "Hour", "DTE", "Spot", "Structure", "Ask$", "Mark$", "Spread", "Fees",
        "CallIV", "PutIV"))
    print("  " + "-" * 92)

    for hour in hours:
        for offset in OFFSETS:
            key = (offset, hour)
            if key not in entry_info:
                continue
            info = entry_info[key]
            print(
                "  %02d:00 %4.0fh $%8s  %10s  $%7s $%7s $%5.0f  $%4.0f  %5.1f%% %5.1f%%" % (
                    hour, info["dte_hours"],
                    "{:,.0f}".format(info["spot"]),
                    offset_label(offset),
                    "{:,.0f}".format(info["ask_premium"]),
                    "{:,.0f}".format(info["mark_premium"]),
                    info["spread_cost"], ROUND_TRIP_FEES,
                    info["call_iv"], info["put_iv"],
                )
            )
        if hour != hours[-1]:
            print()


def print_btc_range(spot_ranges):
    print()
    print("=" * 110)
    print("  BTC INDEX RANGE (high − low) PER ENTRY HOUR × HOLD WINDOW")
    print("  Available index excursion to capture")
    print("=" * 110)

    hours = sorted(set(h for h, _ in spot_ranges.keys()))
    holds = sorted(set(mh for _, mh in spot_ranges.keys()))

    header = "  %6s" % "Entry"
    for mh in holds:
        header += "  %6s" % ("%dh" % mh)
    print(header)
    print("  " + "-" * (7 + 8 * len(holds)))

    for hour in hours:
        row = "  %02d:00" % hour
        for mh in holds:
            sr = spot_ranges.get((hour, mh))
            if sr:
                row += "  $%5.0f" % sr["range"]
            else:
                row += "  %6s" % "—"
        print(row)


def print_trigger_hit_rate(results):
    """Show how often each trigger fires at each entry hour (across all structures and holds)."""
    print()
    print("=" * 110)
    print("  TRIGGER HIT RATE — How often BTC moved enough to trigger TP")
    print("  (across all structures and max holds)")
    print("=" * 110)

    hours = sorted(set(k[1] for k in results.keys()))
    triggers = sorted(set(k[2] for k in results.keys()))

    header = "  %6s" % "Entry"
    for trig in triggers:
        header += "  %6s" % ("$%d" % trig)
    print(header)
    print("  " + "-" * (7 + 8 * len(triggers)))

    for hour in hours:
        row = "  %02d:00" % hour
        for trig in triggers:
            total = 0
            hit = 0
            for key, res in results.items():
                if key[1] == hour and key[2] == trig:
                    total += 1
                    if res["exit_type"] == "triggered":
                        hit += 1
            if total > 0:
                pct = hit / total * 100
                row += "  %5.0f%%" % pct
            else:
                row += "  %6s" % "—"
        print(row)


def print_pnl_when_triggered(results, entry_info):
    """For each structure × trigger, show average P&L when the trigger fired."""
    print()
    print("=" * 110)
    print("  P&L WHEN TRIGGERED — What you earn when the index move happens")
    print("  (average across entry hours and holds where trigger fired)")
    print("=" * 110)

    triggers = sorted(set(k[2] for k in results.keys()))

    print("  %10s" % "Structure", end="")
    for trig in triggers:
        print("  %8s" % ("$%d" % trig), end="")
    print()
    print("  " + "-" * (11 + 10 * len(triggers)))

    for offset in OFFSETS:
        row = "  %10s" % offset_label(offset)
        for trig in triggers:
            pnls = []
            for key, res in results.items():
                if key[0] == offset and key[2] == trig and res["exit_type"] == "triggered":
                    pnls.append(res["pnl"])
            if pnls:
                avg = sum(pnls) / len(pnls)
                row += "  $%7.0f" % avg
            else:
                row += "  %8s" % "—"
        print(row)


def print_conversion_efficiency(results, entry_info, spot_ranges):
    """Show P&L as % of premium paid — how efficiently the structure converts moves to profit."""
    print()
    print("=" * 110)
    print("  CONVERSION EFFICIENCY — P&L ÷ Premium (triggered exits only)")
    print("  Higher = structure converts index move more efficiently")
    print("=" * 110)

    triggers = sorted(set(k[2] for k in results.keys()))

    print("  %10s" % "Structure", end="")
    for trig in triggers:
        print("  %8s" % ("$%d" % trig), end="")
    print()
    print("  " + "-" * (11 + 10 * len(triggers)))

    for offset in OFFSETS:
        row = "  %10s" % offset_label(offset)
        for trig in triggers:
            ratios = []
            for key, res in results.items():
                if key[0] == offset and key[2] == trig and res["exit_type"] == "triggered":
                    info = entry_info.get((offset, key[1]))
                    if info and info["ask_premium"] > 0:
                        ratios.append(res["pnl"] / info["ask_premium"] * 100)
            if ratios:
                avg = sum(ratios) / len(ratios)
                row += "  %7.0f%%" % avg
            else:
                row += "  %8s" % "—"
        print(row)


def print_top_combos(results, entry_info, n=30):
    print()
    print("=" * 110)
    print("  TOP %d COMBINATIONS BY P&L (index-move triggers)" % n)
    print("=" * 110)

    ranked = []
    for key, res in results.items():
        offset, entry_h, trigger, max_hold = key
        info = entry_info.get((offset, entry_h), {})
        ranked.append({
            "label": offset_label(offset),
            "entry": "%02d:00" % entry_h,
            "trigger": trigger,
            "max_hold": max_hold,
            "pnl": res["pnl"],
            "exit_type": res["exit_type"],
            "exit_min": res["exit_minutes"],
            "premium": info.get("ask_premium", 0),
        })

    ranked.sort(key=lambda x: x["pnl"], reverse=True)

    print("  %10s  %6s  %8s  %5s  %9s  %8s  %7s  %s" % (
        "Structure", "Entry", "Trigger", "MaxH", "P&L", "Premium", "Return", "Exit"))
    print("  " + "-" * 80)

    for r in ranked[:n]:
        ret = r["pnl"] / r["premium"] * 100 if r["premium"] > 0 else 0
        exit_str = "Trig @%dm" % r["exit_min"] if r["exit_type"] == "triggered" else "Expired"
        print(
            "  %10s  %6s  $%6d  %4dh  $%8s  $%7s  %5.0f%%  %s" % (
                r["label"], r["entry"], r["trigger"],
                r["max_hold"], "{:,.0f}".format(r["pnl"]),
                "{:,.0f}".format(r["premium"]),
                ret, exit_str,
            )
        )
    return ranked


def print_best_per_structure(results, entry_info):
    print()
    print("=" * 80)
    print("  BEST CONFIGURATION PER STRUCTURE")
    print("=" * 80)

    print("\n  %-15s %6s %8s %5s %8s %8s %7s  %s" % (
        "Structure", "Entry", "Trigger", "Hold", "P&L", "Premium", "Return", "Exit"))
    print("  " + "-" * 75)

    for offset in OFFSETS:
        best_key = None
        best_pnl = -1e18
        for key, res in results.items():
            if key[0] == offset and res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_key = key
        if best_key:
            off, eh, trig, mh = best_key
            res = results[best_key]
            info = entry_info.get((off, eh), {})
            prem = info.get("ask_premium", 0)
            ret = best_pnl / prem * 100 if prem > 0 else 0
            label = "ATM straddle" if offset == 0 else "Strangle +/-%d" % offset
            exit_str = "Trig @%dm" % res["exit_minutes"] if res["exit_type"] == "triggered" else "Expired"
            print(
                "  %-15s %02d:00 $%6d %4dh $%7s $%7s %6.0f%%  %s" % (
                    label, eh, trig, mh,
                    "{:,.0f}".format(best_pnl),
                    "{:,.0f}".format(prem),
                    ret, exit_str,
                )
            )


def print_hourly_summary(results, entry_info):
    print()
    print("=" * 80)
    print("  BEST RESULT PER ENTRY HOUR")
    print("=" * 80)

    print("\n  %6s %9s %15s %8s %5s %8s %7s  %s" % (
        "Hour", "Spot", "Best Structure", "Trigger", "Hold", "P&L", "Return", "Exit"))
    print("  " + "-" * 75)

    for hour in range(MAX_ENTRY_HOUR + 1):
        best_key = None
        best_pnl = -1e18
        for key, res in results.items():
            if key[1] == hour and res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_key = key
        if best_key is None:
            continue
        off, eh, trig, mh = best_key
        res = results[best_key]
        info = entry_info.get((off, eh), {})
        spot = info.get("spot", 0)
        prem = info.get("ask_premium", 0)
        ret = best_pnl / prem * 100 if prem > 0 else 0
        exit_str = "Trig @%dm" % res["exit_minutes"] if res["exit_type"] == "triggered" else "Expired"
        print(
            "  %02d:00 $%8s  %15s $%6d %4dh $%7s %6.0f%%  %s" % (
                hour, "{:,.0f}".format(spot), offset_label(off),
                trig, mh, "{:,.0f}".format(best_pnl), ret, exit_str,
            )
        )


# ── HTML Report ───────────────────────────────────────────────────

def generate_html(results, entry_info, spot_ranges, warnings):
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
    .logic-box { background: #e3f2fd; border: 2px solid #1976d2; border-radius: 8px;
                 padding: 16px 24px; margin: 20px 0; }
    .logic-box h3 { margin: 0 0 8px; color: #0d47a1; }
    .warning-box { background: #fce4ec; border: 2px solid #e53935; border-radius: 8px;
                   padding: 16px 24px; margin: 20px 0; }
    .warning-box h3 { margin: 0 0 8px; color: #b71c1c; }
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
    table.prem td { text-align: center; font-size: 13px; }
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
        return "rgb(%d,%d,80)" % (r, g)

    def pnl_class(v):
        if v > 0:
            return "positive"
        if v < 0:
            return "negative"
        return ""

    def build_heatmap(title, subtitle, row_labels, col_labels, data_fn, fmt="$"):
        all_vals = []
        for r in row_labels:
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    all_vals.append(v)
        if not all_vals:
            return ""
        vmin, vmax = min(all_vals), max(all_vals)
        rows = ['<h3>%s</h3>' % title]
        if subtitle:
            rows.append('<p class="subtitle">%s</p>' % subtitle)
        rows.append('<div class="table-wrap"><table>')
        rows.append('<tr><th class="entry-col"></th>')
        for c in col_labels:
            rows.append('<th>%s</th>' % c)
        rows.append('</tr>')
        for r in row_labels:
            rows.append('<tr><td class="entry-col">%s</td>' % r)
            for c in col_labels:
                v = data_fn(r, c)
                if v is not None:
                    bg = heatmap_color(v, vmin, vmax)
                    if fmt == "$":
                        cell = '$%s' % "{:,.0f}".format(v)
                    elif fmt == "%":
                        cell = '%.0f%%' % v
                    else:
                        cell = str(v)
                    rows.append('<td style="background:%s">%s</td>' % (bg, cell))
                else:
                    rows.append('<td class="empty">&mdash;</td>')
            rows.append('</tr>')
        rows.append('</table></div>')
        return '\n'.join(rows)

    # ── Collect metadata ──
    spots = [info["spot"] for info in entry_info.values()]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    hours_with_data = sorted(set(h for _, h in entry_info.keys()))

    parts = []
    parts.append("""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>0DTE Backtest v3 &mdash; Index-Move Take Profit</title>
<style>%s</style>
</head><body>
<h1>0DTE Backtest v3 &mdash; Index-Move Take Profit</h1>
<div class="meta">
  <span><strong>Generated:</strong> %s</span>
  <span><strong>Date:</strong> 2025-03-01 (Saturday)</span>
  <span><strong>Expiry:</strong> 2MAR25 (08:00 UTC March 2)</span>
  <span><strong>Source:</strong> Deribit options_chain via tardis.dev</span>
</div>
""" % (css, now))

    # Logic box
    parts.append("""
<div class="logic-box">
  <h3>TP Logic: Index-Move Trigger</h3>
  <p><strong>Entry:</strong> Buy structure at ask price</p>
  <p><strong>TP trigger:</strong> Sell at bid when |BTC_now &minus; BTC_entry| &ge; trigger amount</p>
  <p><strong>Safety net:</strong> If trigger never fires, forced sell at bid at max_hold</p>
  <p><strong>Triggers tested:</strong> %s</p>
  <p><strong>Key question:</strong> When BTC moves $X, what does each structure actually return?</p>
</div>
""" % ", ".join("$%d" % t for t in INDEX_TRIGGERS))

    if spots:
        parts.append("""
<div class="summary-box">
  <h3>Market Conditions</h3>
  <p><strong>BTC Spot:</strong> $%s &ndash; $%s</p>
  <p><strong>Entry Window:</strong> 00:00&ndash;%02d:00 UTC</p>
  <p><strong>N=1</strong> &mdash; single day, results are illustrative not statistical</p>
</div>
""" % ("{:,.0f}".format(min(spots)), "{:,.0f}".format(max(spots)), MAX_ENTRY_HOUR))

    parts.append("""
<div class="cost-box">
  <h3>Cost Model</h3>
  <p><strong>Pricing:</strong> BUY at ask, SELL at bid (real Deribit spreads)</p>
  <p><strong>Fees:</strong> $%.0f round-trip ($%.0f/contract &times; %d legs &times; 2)</p>
  <p><strong>P&amp;L:</strong> bid_exit &minus; ask_entry &minus; $%.0f</p>
</div>
""" % (ROUND_TRIP_FEES, FEE_PER_CONTRACT, NUM_LEGS, ROUND_TRIP_FEES))

    if warnings:
        parts.append('<div class="warning-box">\n  <h3>Index Divergence (Deribit vs Binance)</h3>')
        for w in warnings:
            parts.append('  <p>%s</p>' % w)
        parts.append('  <p><em>Entries NOT skipped.</em></p>\n</div>')

    # ── Entry Premiums ──
    parts.append('<h2>Entry Premiums</h2>')
    parts.append('<p class="subtitle">What you pay to open each structure at each hour</p>')
    parts.append('<table class="prem">')
    parts.append('<tr><th>Hour</th><th>DTE</th><th>Spot</th>'
                 '<th>Structure</th><th>Call K</th><th>Put K</th>'
                 '<th>Ask</th><th>Mark</th><th>Spread</th><th>Fees</th>'
                 '<th>Call IV</th><th>Put IV</th></tr>')

    for hour in hours_with_data:
        for offset in OFFSETS:
            key = (offset, hour)
            if key not in entry_info:
                continue
            info = entry_info[key]
            parts.append(
                '<tr><td>%02d:00</td><td>%.0fh</td><td>$%s</td>'
                '<td>%s</td><td>$%s</td><td>$%s</td>'
                '<td><strong>$%s</strong></td><td>$%s</td><td>$%.0f</td><td>$%.0f</td>'
                '<td>%.1f%%</td><td>%.1f%%</td></tr>' % (
                    hour, info["dte_hours"],
                    "{:,.0f}".format(info["spot"]),
                    offset_label(offset),
                    "{:,.0f}".format(info["call_strike"]),
                    "{:,.0f}".format(info["put_strike"]),
                    "{:,.0f}".format(info["ask_premium"]),
                    "{:,.0f}".format(info["mark_premium"]),
                    info["spread_cost"], ROUND_TRIP_FEES,
                    info["call_iv"], info["put_iv"],
                )
            )
    parts.append('</table>')

    # ── BTC Range Heatmap ──
    range_hours = sorted(set(h for h, _ in spot_ranges.keys()))
    range_holds = sorted(set(mh for _, mh in spot_ranges.keys()))

    def range_fn(row, col):
        h = int(row.split(":")[0])
        mh = int(col.replace("h", ""))
        sr = spot_ranges.get((h, mh))
        return sr["range"] if sr else None

    parts.append('<h2>BTC Index Range (Available Excursion)</h2>')
    parts.append('<p class="subtitle">High &minus; Low during each entry &times; hold window. '
                 'Compare with P&amp;L tables to see capture efficiency.</p>')
    parts.append(build_heatmap(
        "BTC Range ($)", None,
        ["%02d:00" % h for h in range_hours],
        ["%dh" % h for h in range_holds],
        range_fn,
    ))

    # ── Trigger Hit Rate Heatmap ──
    parts.append('<h2>Trigger Hit Rate</h2>')
    parts.append('<p class="subtitle">% of (structure, max_hold) combos where the trigger fired at each entry hour</p>')

    triggers = sorted(set(k[2] for k in results.keys()))

    def trigger_hit_fn(row, col):
        h = int(row.split(":")[0])
        trig = int(col.replace("$", "").replace(",", ""))
        total = 0
        hit = 0
        for key, res in results.items():
            if key[1] == h and key[2] == trig:
                total += 1
                if res["exit_type"] == "triggered":
                    hit += 1
        if total > 0:
            return hit / total * 100
        return None

    parts.append(build_heatmap(
        "Trigger Hit Rate", "Higher = BTC moved enough more often",
        ["%02d:00" % h for h in hours_with_data],
        ["$%d" % t for t in triggers],
        trigger_hit_fn, fmt="%",
    ))

    # ── Core heatmaps: P&L by Entry × Trigger, per structure ──
    parts.append('<h2>P&amp;L Heatmaps: Entry Hour &times; Index Trigger</h2>')
    parts.append('<p class="subtitle">Best P&amp;L across max holds when trigger fires '
                 '(or forced exit if not). Net of fees + spread.</p>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else "Strangle &plusmn;$%d" % offset

        def make_entry_trig_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                trig = int(col.replace("$", "").replace(",", ""))
                best = None
                for mh in MAX_HOLDS:
                    key = (off, h, trig, mh)
                    if key in results:
                        v = results[key]["pnl"]
                        if best is None or v > best:
                            best = v
                return best
            return fn

        parts.append(build_heatmap(
            label, "Best P&amp;L across max holds",
            ["%02d:00" % h for h in hours_with_data],
            ["$%d" % t for t in triggers],
            make_entry_trig_fn(offset),
        ))

    # ── P&L by Entry × MaxHold, per structure (best trigger) ──
    parts.append('<h2>P&amp;L Heatmaps: Entry Hour &times; Max Hold</h2>')
    parts.append('<p class="subtitle">Best P&amp;L across all triggers. Shows optimal hold time per entry.</p>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else "Strangle &plusmn;$%d" % offset

        def make_entry_hold_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                mh = int(col.replace("h", ""))
                best = None
                for trig in INDEX_TRIGGERS:
                    key = (off, h, trig, mh)
                    if key in results:
                        v = results[key]["pnl"]
                        if best is None or v > best:
                            best = v
                return best
            return fn

        parts.append(build_heatmap(
            label, "Best P&amp;L across triggers",
            ["%02d:00" % h for h in hours_with_data],
            ["%dh" % h for h in MAX_HOLDS],
            make_entry_hold_fn(offset),
        ))

    # ── Conversion efficiency: P&L / Premium by structure × trigger ──
    parts.append('<h2>Conversion Efficiency: P&amp;L &divide; Premium</h2>')
    parts.append('<p class="subtitle">Average return on premium when trigger fires. '
                 'Higher = structure converts index move into profit more efficiently.</p>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else "Strangle &plusmn;$%d" % offset

        def make_eff_fn(off):
            def fn(row, col):
                h = int(row.split(":")[0])
                trig = int(col.replace("$", "").replace(",", ""))
                # Find best triggered result across holds
                best_pnl = None
                for mh in MAX_HOLDS:
                    key = (off, h, trig, mh)
                    if key in results and results[key]["exit_type"] == "triggered":
                        v = results[key]["pnl"]
                        if best_pnl is None or v > best_pnl:
                            best_pnl = v
                if best_pnl is not None:
                    info = entry_info.get((off, h))
                    if info and info["ask_premium"] > 0:
                        return best_pnl / info["ask_premium"] * 100
                return None
            return fn

        parts.append(build_heatmap(
            label, "P&amp;L &divide; Premium when triggered",
            ["%02d:00" % h for h in hours_with_data],
            ["$%d" % t for t in triggers],
            make_eff_fn(offset), fmt="%",
        ))

    # ── Top 30 combos ──
    ranked = []
    for key, res in results.items():
        offset, entry_h, trigger, max_hold = key
        info = entry_info.get((offset, entry_h), {})
        ranked.append({
            "label": offset_label(offset),
            "entry_h": entry_h,
            "trigger": trigger,
            "max_hold": max_hold,
            "pnl": res["pnl"],
            "exit_type": res["exit_type"],
            "exit_min": res["exit_minutes"],
            "premium": info.get("ask_premium", 0),
        })
    ranked.sort(key=lambda x: x["pnl"], reverse=True)

    parts.append('<h2>Top 30 Combinations</h2>')
    parts.append('<table class="ranked">')
    parts.append('<tr><th>#</th><th>Structure</th><th>Entry</th><th>Trigger</th>'
                 '<th>Max Hold</th><th>P&amp;L</th><th>Premium</th>'
                 '<th>Return</th><th>Exit</th></tr>')
    for i, r in enumerate(ranked[:30], 1):
        pc = pnl_class(r["pnl"])
        ret = r["pnl"] / r["premium"] * 100 if r["premium"] > 0 else 0
        exit_str = "Trig @%dm" % r["exit_min"] if r["exit_type"] == "triggered" else "Expired"
        parts.append(
            '<tr><td>%d</td><td>%s</td><td>%02d:00</td><td>$%d</td><td>%dh</td>'
            '<td class="%s">$%s</td><td>$%s</td><td>%.0f%%</td><td>%s</td></tr>' % (
                i, r["label"], r["entry_h"], r["trigger"], r["max_hold"],
                pc, "{:,.0f}".format(r["pnl"]),
                "{:,.0f}".format(r["premium"]),
                ret, exit_str,
            )
        )
    parts.append('</table>')

    # ── Best per structure ──
    parts.append('<h2>Best Configuration Per Structure</h2>')
    parts.append('<table class="ranked">')
    parts.append('<tr><th>Structure</th><th>Entry</th><th>Trigger</th><th>Max Hold</th>'
                 '<th>P&amp;L</th><th>Premium</th><th>Return</th><th>Exit</th></tr>')

    for offset in OFFSETS:
        label = "ATM Straddle" if offset == 0 else "Strangle &plusmn;$%d" % offset
        best_key = None
        best_pnl = -1e18
        for key, res in results.items():
            if key[0] == offset and res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_key = key
        if best_key:
            off, eh, trig, mh = best_key
            res = results[best_key]
            info = entry_info.get((off, eh), {})
            prem = info.get("ask_premium", 0)
            ret = best_pnl / prem * 100 if prem > 0 else 0
            pc = pnl_class(best_pnl)
            exit_str = "Trig @%dm" % res["exit_minutes"] if res["exit_type"] == "triggered" else "Expired"
            parts.append(
                '<tr><td>%s</td><td>%02d:00</td><td>$%d</td><td>%dh</td>'
                '<td class="%s">$%s</td><td>$%s</td><td>%.0f%%</td><td>%s</td></tr>' % (
                    label, eh, trig, mh,
                    pc, "{:,.0f}".format(best_pnl),
                    "{:,.0f}".format(prem),
                    ret, exit_str,
                )
            )
    parts.append('</table>')

    # ── Best per entry hour ──
    parts.append('<h2>Best Result Per Entry Hour</h2>')
    parts.append('<table class="ranked">')
    parts.append('<tr><th>Hour</th><th>Spot</th><th>Structure</th><th>Trigger</th>'
                 '<th>Hold</th><th>P&amp;L</th><th>Premium</th><th>Return</th><th>Exit</th></tr>')

    for hour in range(MAX_ENTRY_HOUR + 1):
        best_key = None
        best_pnl = -1e18
        for key, res in results.items():
            if key[1] == hour and res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_key = key
        if best_key is None:
            continue
        off, eh, trig, mh = best_key
        res = results[best_key]
        info = entry_info.get((off, eh), {})
        spot = info.get("spot", 0)
        prem = info.get("ask_premium", 0)
        ret = best_pnl / prem * 100 if prem > 0 else 0
        pc = pnl_class(best_pnl)
        exit_str = "Trig @%dm" % res["exit_minutes"] if res["exit_type"] == "triggered" else "Expired"
        parts.append(
            '<tr><td>%02d:00</td><td>$%s</td><td>%s</td><td>$%d</td><td>%dh</td>'
            '<td class="%s">$%s</td><td>$%s</td><td>%.0f%%</td><td>%s</td></tr>' % (
                hour, "{:,.0f}".format(spot), offset_label(off),
                trig, mh,
                pc, "{:,.0f}".format(best_pnl),
                "{:,.0f}".format(prem),
                ret, exit_str,
            )
        )
    parts.append('</table>')

    parts.append('\n</body></html>')
    return '\n'.join(parts)


# ── Main ──────────────────────────────────────────────────────────

def main():
    if not os.path.exists(PARQUET_PATH):
        print("Parquet file not found: %s" % PARQUET_PATH)
        print("Run the tardis_options download + extract pipeline first.")
        sys.exit(1)

    chain = HistoricOptionChain(PARQUET_PATH)

    strikes = chain.strikes(EXPIRY)
    print("\n  Available strikes for %s: %d" % (EXPIRY, len(strikes)))
    print("  Range: $%s - $%s" % ("{:,.0f}".format(strikes[0]), "{:,.0f}".format(strikes[-1])))
    print("  Increments: %s" % sorted(set(
        int(strikes[i+1] - strikes[i]) for i in range(len(strikes)-1)
    )))

    binance_candles = fetch_binance_5min_candles("2025-03-01")

    print("\n  Running backtest v3 (index-move triggers)...")
    print("    Structures:  %d (offsets: %s)" % (len(OFFSETS), OFFSETS))
    print("    Triggers:    %d (%s)" % (len(INDEX_TRIGGERS), INDEX_TRIGGERS))
    print("    Max holds:   %d (%s)" % (len(MAX_HOLDS), MAX_HOLDS))
    print("    Check:       every %d min" % CHECK_INTERVAL_MIN)
    print("    Entry cutoff: %02d:00 UTC" % MAX_ENTRY_HOUR)
    print("    Pricing:     BUY at ask, SELL at bid")
    print("    Fees:        $%.0f round-trip" % ROUND_TRIP_FEES)

    results, entry_info, spot_ranges, warnings = run_backtest(chain, binance_candles)
    print("    Results: %d combos" % len(results))

    # Console output
    print_data_summary(chain, entry_info, warnings)
    print_entry_premiums(entry_info)
    print_btc_range(spot_ranges)
    print_trigger_hit_rate(results)
    print_pnl_when_triggered(results, entry_info)
    print_conversion_efficiency(results, entry_info, spot_ranges)
    print_top_combos(results, entry_info)
    print_best_per_structure(results, entry_info)
    print_hourly_summary(results, entry_info)

    # HTML report
    html = generate_html(results, entry_info, spot_ranges, warnings)
    html_path = os.path.join(SCRIPT_DIR, "backtest_deribit_realdata_report.html")
    with open(html_path, "w") as f:
        f.write(html)
    print("\n  HTML report -> %s" % html_path)


if __name__ == "__main__":
    main()
