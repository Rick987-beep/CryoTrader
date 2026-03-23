"""
Strategy: Long Straddle / Strangle with Index-Move TP Trigger.

Buys 0DTE BTC option structures and exits on an underlying index-move trigger.

To add a new strategy: create a new file with its own parameter grid and
run_backtest() returning the same 5-tuple (results, all_candles, ...).

Parameter grid (17,640 combos):
    OFFSETS        — strike widths [0, 500, ..., 3000]
    INDEX_TRIGGERS — BTC move to take profit [300..2000]
    MAX_HOLDS      — hold window [1..12] hours
    Entry hours    — 00:00-20:00 UTC (weekdays only by default)

Depends on: pricing.py (BS model, vol, fees)
"""

import statistics
from collections import defaultdict
from datetime import timedelta

from pricing import (
    price_structure, price_at_exit, hours_to_expiry, estimate_vol,
    deribit_fee_per_leg,
)

# ── Strategy Parameters ───────────────────────────────────────────

OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]

INDEX_TRIGGERS = [300, 400, 500, 600, 700, 800, 1000, 1200, 1500, 2000]

MAX_HOLDS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]

SLIPPAGE = 0.04            # ±4% on BS price

MAX_ENTRY_HOUR = 20

MIN_SAMPLES = 5


# ── Labels ────────────────────────────────────────────────────────

def offset_label(offset):
    return "straddle" if offset == 0 else "+/-%d" % offset


def offset_label_short(offset):
    return "straddle" if offset == 0 else "±%d" % offset


# ── Backtest Loop ─────────────────────────────────────────────────

def run_backtest(all_candles, weekdays_only=True):
    """Run the straddle/strangle backtest with BS pricing and realized vol.

    Returns:
        results:        dict key → list of (pnl, triggered, exit_h, entry_date)
        btc_ranges:     dict (entry_hour, hold_h) → [range_vals]
        entry_spots:    dict entry_hour → [spots]
        entry_vols:     dict entry_hour → [vol estimates]
        entry_premiums: dict (entry_hour, offset) → [premium_usd]
    """
    sorted_candles = sorted(all_candles, key=lambda c: c["dt"])
    n = len(sorted_candles)

    results = defaultdict(list)
    btc_ranges = defaultdict(list)
    entry_spots = defaultdict(list)
    entry_vols = defaultdict(list)
    entry_premiums = defaultdict(list)

    n_entries = 0

    for i, entry_candle in enumerate(sorted_candles):
        entry_hour = entry_candle["hour"]
        if entry_hour > MAX_ENTRY_HOUR:
            continue

        if weekdays_only and entry_candle["weekday"] >= 5:
            continue

        entry_price = entry_candle["open"]
        entry_dte = hours_to_expiry(entry_hour)
        sigma = estimate_vol(sorted_candles, i)

        entry_spots[entry_hour].append(entry_price)
        entry_vols[entry_hour].append(sigma)
        n_entries += 1

        # Walk forward — uses ALL candles including weekends
        running_high = entry_price
        running_low = entry_price
        hourly_data = []

        for h in range(1, max(MAX_HOLDS) + 1):
            j = i + h
            if j >= n:
                break
            c = sorted_candles[j]

            expected_dt = entry_candle["dt"] + timedelta(hours=h)
            if c["dt"] != expected_dt:
                break

            remaining_dte = entry_dte - h
            if remaining_dte <= 0:
                break

            running_high = max(running_high, c["high"])
            running_low = min(running_low, c["low"])
            max_excursion = max(
                running_high - entry_price, entry_price - running_low)

            hourly_data.append({
                "h": h,
                "max_excursion": max_excursion,
                "close": c["close"],
                "remaining_dte": remaining_dte,
            })

            btc_ranges[(entry_hour, h)].append(running_high - running_low)

        if not hourly_data:
            continue

        for offset in OFFSETS:
            entry_total, entry_call, entry_put, K_call, K_put = \
                price_structure(entry_price, offset, entry_dte, sigma)

            if entry_total < 3:
                continue

            entry_premiums[(entry_hour, offset)].append(entry_total)

            entry_paid = entry_total * (1 + SLIPPAGE)
            fee_open = (deribit_fee_per_leg(entry_price, entry_call)
                        + deribit_fee_per_leg(entry_price, entry_put))

            for max_hold in MAX_HOLDS:
                if max_hold > entry_dte - 1:
                    continue

                last_valid_h = hourly_data[-1]["h"] if hourly_data else 0
                if last_valid_h < max_hold:
                    continue

                for trigger in INDEX_TRIGGERS:
                    triggered = False
                    exit_pnl = None
                    exit_h = 0

                    for hd in hourly_data:
                        if hd["h"] > max_hold:
                            break

                        if hd["max_excursion"] >= trigger:
                            exit_spot = entry_price + trigger
                            remaining = hd["remaining_dte"]

                            exit_total, exit_call, exit_put = price_at_exit(
                                exit_spot, K_call, K_put, remaining, sigma)

                            exit_received = exit_total * (1 - SLIPPAGE)
                            fee_close = (
                                deribit_fee_per_leg(exit_spot, exit_call)
                                + deribit_fee_per_leg(exit_spot, exit_put))

                            exit_pnl = (exit_received - entry_paid
                                        - fee_open - fee_close)
                            exit_h = hd["h"]
                            triggered = True
                            break

                    if not triggered:
                        last_hd = None
                        for hd in hourly_data:
                            if hd["h"] <= max_hold:
                                last_hd = hd
                        if last_hd is None:
                            continue

                        exit_spot = last_hd["close"]
                        remaining = last_hd["remaining_dte"]

                        exit_total, exit_call, exit_put = price_at_exit(
                            exit_spot, K_call, K_put, remaining, sigma)

                        exit_received = exit_total * (1 - SLIPPAGE)
                        fee_close = (
                            deribit_fee_per_leg(exit_spot, exit_call)
                            + deribit_fee_per_leg(exit_spot, exit_put))

                        exit_pnl = (exit_received - entry_paid
                                    - fee_open - fee_close)
                        exit_h = last_hd["h"]

                    if exit_pnl is not None:
                        key = (offset, entry_hour, trigger, max_hold)
                        results[key].append((exit_pnl, triggered, exit_h,
                                             entry_candle["date"]))

    print("    Weekday entries: %d" % n_entries)
    return results, btc_ranges, entry_spots, entry_vols, entry_premiums
