#!/usr/bin/env python3
"""
Straddle vs Strangle Analysis Grid

Takes a 12:00 snapshot (entry) and a 19:00 snapshot (exit) and computes
PnL for every (strike_offset, realized_move) combination.

Output: a table showing, for each structure width K = 0, 500, 1000, …, 3000:
  - Entry cost (premium paid for both legs)
  - PnL at exit given the realized BTC move
  - Efficiency = PnL / entry_cost
  - Theta paid (7-hour decay cost)
  - Greeks at entry

Also prints the optimal K for the day's realized move size.

Usage:
    python -m analysis.analyze_grid 20260310
    python -m analysis.analyze_grid 20260310 --details
"""

import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")

OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]
HYPOTHETICAL_MOVES = [0, 500, 1000, 1500, 2000, 2500, 3000]


def load_snapshot(date_str: str, label: str) -> dict:
    filepath = os.path.join(DATA_DIR, f"snapshot_{date_str}_{label}.json")
    if not os.path.exists(filepath):
        print(f"ERROR: Snapshot file not found: {filepath}")
        sys.exit(1)
    with open(filepath) as f:
        return json.load(f)


def find_strike(strikes: list, target: float):
    """Find the strike entry closest to target value."""
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s["strike"] - target))

def implied_spot(snapshot: dict) -> float:
    """Derive implied spot from put-call parity at ATM: S = K + C - P."""
    atm = snapshot["atm_strike"]
    for s in snapshot["strikes"]:
        if s["strike"] == atm:
            c = (s.get("call") or {}).get("mark_price")
            p = (s.get("put") or {}).get("mark_price")
            if c and p:
                return atm + (c - p)
    # Fallback to reported (may be stale)
    return snapshot["index_price"]

def leg_price(leg) -> Optional[float]:
    """Extract mark price from a leg dict (the exchange's theoretical fair value)."""
    if leg is None or isinstance(leg, dict) and "error" in leg:
        return None
    mp = leg.get("mark_price")
    if mp is not None and mp > 0:
        return mp
    return None


def build_structure_entry(strikes: list, atm: float, offset: float):
    """
    For a given offset K, find the call at atm+K and put at atm-K,
    return entry cost and greeks.
    """
    call_strike_target = atm + offset
    put_strike_target = atm - offset

    call_row = find_strike(strikes, call_strike_target)
    put_row = find_strike(strikes, put_strike_target)

    if call_row is None or put_row is None:
        return None

    call_leg = call_row.get("call")
    put_leg = put_row.get("put")

    call_mark = leg_price(call_leg)
    put_mark = leg_price(put_leg)

    if call_mark is None or put_mark is None:
        return None

    # Actual strikes used (may differ slightly from target due to rounding)
    actual_call_strike = call_row["strike"]
    actual_put_strike = put_row["strike"]

    return {
        "offset": offset,
        "call_strike": actual_call_strike,
        "put_strike": actual_put_strike,
        "call_symbol": call_leg.get("symbol", "?"),
        "put_symbol": put_leg.get("symbol", "?"),
        "call_mark": call_mark,
        "put_mark": put_mark,
        "entry_cost": call_mark + put_mark,
        "call_delta": call_leg.get("delta"),
        "put_delta": put_leg.get("delta"),
        "call_gamma": call_leg.get("gamma"),
        "put_gamma": put_leg.get("gamma"),
        "call_theta": call_leg.get("theta"),
        "put_theta": put_leg.get("theta"),
        "call_iv": call_leg.get("iv"),
        "put_iv": put_leg.get("iv"),
    }


def build_structure_exit(strikes: list, call_strike: float, put_strike: float):
    """Look up exit prices for the exact call/put strikes."""
    call_row = find_strike(strikes, call_strike)
    put_row = find_strike(strikes, put_strike)

    if call_row is None or put_row is None:
        return None

    call_leg = call_row.get("call")
    put_leg = put_row.get("put")

    call_mark = leg_price(call_leg)
    put_mark = leg_price(put_leg)

    if call_mark is None or put_mark is None:
        return None

    return {
        "call_mark": call_mark,
        "put_mark": put_mark,
        "exit_value": call_mark + put_mark,
    }


def analyze(date_str: str, show_details: bool = False):
    entry = load_snapshot(date_str, "1200")
    exit_ = load_snapshot(date_str, "1900")

    entry_spot = implied_spot(entry)
    exit_spot = implied_spot(exit_)
    realized_move = exit_spot - entry_spot
    abs_move = abs(realized_move)
    direction = "UP" if realized_move >= 0 else "DOWN"

    atm = entry["atm_strike"]

    print("=" * 80)
    print("  STRADDLE vs STRANGLE ANALYSIS")
    print(f"  Date: {date_str}   Expiry: {entry.get('expiry_date', '?')}")
    print(f"  Entry (12:00 UTC): implied spot = ${entry_spot:,.2f}   ATM strike = ${atm:,.0f}")
    print(f"  Exit  (19:00 UTC): implied spot = ${exit_spot:,.2f}")
    print(f"  Realized move: ${realized_move:+,.2f}  ({direction} ${abs_move:,.2f})")
    print("=" * 80)

    # ── Build entry structures ────────────────────────────────────────────
    structures = []
    for K in OFFSETS:
        s = build_structure_entry(entry["strikes"], atm, K)
        if s is None:
            print(f"  WARNING: Could not build structure for K={K}")
            continue
        structures.append(s)

    if not structures:
        print("ERROR: No structures could be built. Check snapshot data.")
        sys.exit(1)

    # ── Compute actual PnL ────────────────────────────────────────────────
    print()
    print("─── ACTUAL PnL (realized move) ─────────────────────────────────────────────")
    print()
    header = (
        f"{'Offset':>8}  {'Call K':>8}  {'Put K':>8}  "
        f"{'Entry$':>9}  {'Exit$':>9}  {'PnL$':>9}  {'Eff%':>7}  "
        f"{'Θ/hr':>7}"
    )
    print(header)
    print("─" * len(header))

    best_pnl = -999999
    best_k = None

    for s in structures:
        ex = build_structure_exit(exit_["strikes"], s["call_strike"], s["put_strike"])
        if ex is None:
            print(f"  K={s['offset']:>5}  — exit data missing")
            continue

        pnl = ex["exit_value"] - s["entry_cost"]
        eff = (pnl / s["entry_cost"]) * 100 if s["entry_cost"] > 0 else 0

        # Theta per hour (sum of both legs, theta is daily so /24)
        theta_c = s["call_theta"] or 0
        theta_p = s["put_theta"] or 0
        theta_hourly = (theta_c + theta_p) / 24

        if pnl > best_pnl:
            best_pnl = pnl
            best_k = s["offset"]

        print(
            f"  K={s['offset']:>4}  "
            f"${s['call_strike']:>7,.0f}  ${s['put_strike']:>7,.0f}  "
            f"${s['entry_cost']:>8,.2f}  ${ex['exit_value']:>8,.2f}  "
            f"${pnl:>+8,.2f}  {eff:>+6.1f}%  "
            f"${theta_hourly:>6,.2f}"
        )

    print()
    print(f"  ★ Best structure for today's ${abs_move:,.0f} move: K = {best_k}")
    print()

    # ── Greeks at entry ───────────────────────────────────────────────────
    if show_details:
        print("─── GREEKS AT ENTRY ────────────────────────────────────────────────────────")
        print()
        print(
            f"{'Offset':>8}  {'Δ call':>8}  {'Δ put':>8}  {'Σ|Δ|':>7}  "
            f"{'Γ call':>9}  {'Γ put':>9}  {'IV c':>6}  {'IV p':>6}"
        )
        for s in structures:
            dc = s["call_delta"] or 0
            dp = s["put_delta"] or 0
            gc = s["call_gamma"] or 0
            gp = s["put_gamma"] or 0
            ic = s["call_iv"] or 0
            ip = s["put_iv"] or 0
            print(
                f"  K={s['offset']:>4}  {dc:>+8.4f}  {dp:>+8.4f}  {abs(dc)+abs(dp):>7.4f}  "
                f"{gc:>9.6f}  {gp:>9.6f}  {ic:>5.1f}%  {ip:>5.1f}%"
            )
        print()

    # ── Hypothetical grid ─────────────────────────────────────────────────
    print("─── HYPOTHETICAL PnL GRID (estimated from entry greeks) ─────────────────────")
    print("    Rows = strike offset K,  Columns = hypothetical |move| in USD")
    print("    Values = estimated PnL ($) using delta + gamma approximation")
    print()

    # Header row
    col_w = 9
    km_label = 'K \\ Move'
    row = f"{km_label:>8}"
    for M in HYPOTHETICAL_MOVES:
        row += f"  ${M:>{col_w-1},}"
    print(row)
    print("─" * (8 + (col_w + 2) * len(HYPOTHETICAL_MOVES)))

    for s in structures:
        dc = s["call_delta"] or 0
        dp = s["put_delta"] or 0
        gc = s["call_gamma"] or 0
        gp = s["put_gamma"] or 0
        tc = s["call_theta"] or 0
        tp = s["put_theta"] or 0
        entry_cost = s["entry_cost"]

        # Theta loss over 7 hours (theta is per-day, so * 7/24)
        theta_loss = (tc + tp) * (7 / 24)  # negative number (loss)

        row = f"  K={s['offset']:>4}"
        for M in HYPOTHETICAL_MOVES:
            # Delta-gamma approximation for BOTH directions, take the better one
            # For +M: PnL ≈ Δ_c*M + Δ_p*M + 0.5*(Γ_c+Γ_p)*M² + theta_loss
            # For -M: PnL ≈ -Δ_c*M - Δ_p*M + 0.5*(Γ_c+Γ_p)*M² + theta_loss
            # A long straddle/strangle profits from move in either direction,
            # so we take the better outcome (symmetric intent)
            pnl_up = dc * M + dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_dn = -dc * M - dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_best = max(pnl_up, pnl_dn)

            row += f"  {pnl_best:>{col_w},.2f}"
        print(row)

    print()

    # ── Efficiency grid ───────────────────────────────────────────────────
    print("─── EFFICIENCY GRID (PnL / entry_cost, %) ──────────────────────────────────")
    print()

    row = f"{km_label:>8}"
    for M in HYPOTHETICAL_MOVES:
        row += f"  ${M:>{col_w-1},}"
    print(row)
    print("─" * (8 + (col_w + 2) * len(HYPOTHETICAL_MOVES)))

    for s in structures:
        dc = s["call_delta"] or 0
        dp = s["put_delta"] or 0
        gc = s["call_gamma"] or 0
        gp = s["put_gamma"] or 0
        tc = s["call_theta"] or 0
        tp = s["put_theta"] or 0
        entry_cost = s["entry_cost"]
        theta_loss = (tc + tp) * (7 / 24)

        row = f"  K={s['offset']:>4}"
        for M in HYPOTHETICAL_MOVES:
            pnl_up = dc * M + dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_dn = -dc * M - dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_best = max(pnl_up, pnl_dn)
            eff = (pnl_best / entry_cost) * 100 if entry_cost > 0 else 0
            row += f"  {eff:>{col_w-1}.1f}%"
        print(row)

    print()

    # ── Optimal K per move size ───────────────────────────────────────────
    print("─── OPTIMAL STRUCTURE PER ANTICIPATED MOVE ──────────────────────────────────")
    print()

    for M in HYPOTHETICAL_MOVES:
        best_eff = -999999
        best_k_hyp = 0
        best_pnl_hyp = 0

        for s in structures:
            dc = s["call_delta"] or 0
            dp = s["put_delta"] or 0
            gc = s["call_gamma"] or 0
            gp = s["put_gamma"] or 0
            tc = s["call_theta"] or 0
            tp = s["put_theta"] or 0
            entry_cost = s["entry_cost"]
            theta_loss = (tc + tp) * (7 / 24)

            pnl_up = dc * M + dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_dn = -dc * M - dp * M + 0.5 * (gc + gp) * M * M + theta_loss
            pnl_best = max(pnl_up, pnl_dn)
            eff = (pnl_best / entry_cost) * 100 if entry_cost > 0 else 0

            if eff > best_eff:
                best_eff = eff
                best_k_hyp = s["offset"]
                best_pnl_hyp = pnl_best

        label = "ATM straddle" if best_k_hyp == 0 else f"±${best_k_hyp:,} strangle"
        print(
            f"  Move ${M:>5,}  →  {label:<22}  "
            f"PnL ≈ ${best_pnl_hyp:>+8,.2f}   Efficiency {best_eff:>+6.1f}%"
        )

    print()
    print("=" * 80)
    print("  NOTE: Hypothetical grid uses delta-gamma approximation from entry greeks.")
    print("  The 'Actual PnL' row above uses real exit prices and is the ground truth.")
    print("=" * 80)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    date_str = sys.argv[1]
    show_details = "--details" in sys.argv

    analyze(date_str, show_details)


if __name__ == "__main__":
    main()
