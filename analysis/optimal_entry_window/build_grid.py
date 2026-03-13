#!/usr/bin/env python3
"""
Build the 2D analysis grid: (strike_offset K) × (realized move M)
using actual mark prices from hourly snapshots.

For every pair of snapshots (entry, exit) where exit > entry,
compute actual PnL for each structure K = 0, 500, …, 3000.
Then bin results by realized move size and display the grid.
"""

import json
import glob
import os
import sys
from collections import defaultdict

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]
MOVE_BINS = [0, 400, 800, 1200, 1600, 2000, 2500, 3000, 999999]
MOVE_LABELS = ["0-400", "400-800", "800-1.2k", "1.2-1.6k", "1.6-2k", "2-2.5k", "2.5-3k", "3k+"]


def load_all_snapshots():
    files = sorted(glob.glob(os.path.join(DATA_DIR, "snapshot_*.json")))
    snapshots = []
    for f in files:
        with open(f) as fh:
            d = json.load(fh)
        d["_file"] = os.path.basename(f)
        d["_implied_spot"] = implied_spot(d)
        snapshots.append(d)
    return snapshots


def implied_spot(snap):
    atm = snap["atm_strike"]
    for s in snap["strikes"]:
        if s["strike"] == atm:
            c = (s.get("call") or {}).get("mark_price")
            p = (s.get("put") or {}).get("mark_price")
            if c and p:
                return atm + (c - p)
    return snap["index_price"]


def mark_price(leg):
    if leg is None or "error" in leg:
        return None
    mp = leg.get("mark_price")
    return mp if mp and mp > 0 else None


def find_strike(strikes, target):
    if not strikes:
        return None
    return min(strikes, key=lambda s: abs(s["strike"] - target))


def structure_value(strikes, atm, offset):
    """Return combined mark price of call@atm+offset + put@atm-offset."""
    call_row = find_strike(strikes, atm + offset)
    put_row = find_strike(strikes, atm - offset)
    if call_row is None or put_row is None:
        return None
    cm = mark_price(call_row.get("call"))
    pm = mark_price(put_row.get("put"))
    if cm is None or pm is None:
        return None
    return cm + pm


def move_bin(abs_move):
    for i in range(len(MOVE_BINS) - 1):
        if abs_move < MOVE_BINS[i + 1]:
            return i
    return len(MOVE_BINS) - 2


def main():
    snapshots = load_all_snapshots()
    print(f"Loaded {len(snapshots)} snapshots\n")

    # We need snapshots on the SAME expiry to compare entry vs exit
    # Group by expiry
    by_expiry = defaultdict(list)
    for s in snapshots:
        by_expiry[s["expiry_ts"]].append(s)

    # Grid accumulators: grid[K_idx][move_bin] = list of (pnl, eff)
    pnl_grid = [[[] for _ in MOVE_LABELS] for _ in OFFSETS]
    eff_grid = [[[] for _ in MOVE_LABELS] for _ in OFFSETS]

    pair_count = 0

    for expiry, snaps in sorted(by_expiry.items()):
        snaps.sort(key=lambda s: s["timestamp_epoch"])

        # Only use snapshots with the same ATM strike (same strike grid)
        # to ensure we're pricing the same contracts
        for i, entry in enumerate(snaps):
            for exit_ in snaps[i + 1:]:
                hours = (exit_["timestamp_epoch"] - entry["timestamp_epoch"]) / 3600
                if hours < 0.5 or hours > 12:
                    continue  # skip very short or very long windows

                entry_spot = entry["_implied_spot"]
                exit_spot = exit_["_implied_spot"]
                abs_move = abs(exit_spot - entry_spot)
                mb = move_bin(abs_move)

                atm = entry["atm_strike"]

                for k_idx, K in enumerate(OFFSETS):
                    entry_val = structure_value(entry["strikes"], atm, K)
                    exit_val = structure_value(exit_["strikes"], atm, K)
                    if entry_val is None or exit_val is None or entry_val <= 0:
                        continue

                    pnl = exit_val - entry_val
                    eff = (pnl / entry_val) * 100

                    pnl_grid[k_idx][mb].append(pnl)
                    eff_grid[k_idx][mb].append(eff)

                pair_count += 1

    print(f"Analyzed {pair_count} entry→exit pairs across {len(by_expiry)} expiries\n")

    # ── PnL Grid ──────────────────────────────────────────────────────────
    col_w = 12
    print("=" * 80)
    print("  ACTUAL PnL GRID  (avg $ per structure, from mark prices)")
    print("  Rows = strike offset K,  Columns = realized |move| bucket")
    print("=" * 80)
    print()

    header = f"{'K':>6}"
    for label in MOVE_LABELS:
        header += f"  {label:>{col_w}}"
    print(header)
    print("─" * len(header))

    for k_idx, K in enumerate(OFFSETS):
        label = "ATM" if K == 0 else f"±{K}"
        row = f"{label:>6}"
        for mb in range(len(MOVE_LABELS)):
            vals = pnl_grid[k_idx][mb]
            if vals:
                avg = sum(vals) / len(vals)
                row += f"  {avg:>{col_w-1},.1f}$"
            else:
                row += f"  {'—':>{col_w}}"
        print(row)

    print()
    print("  (n = sample count per cell)")
    header = f"{'K':>6}"
    for label in MOVE_LABELS:
        header += f"  {label:>{col_w}}"
    print(header)
    print("─" * len(header))
    for k_idx, K in enumerate(OFFSETS):
        label = "ATM" if K == 0 else f"±{K}"
        row = f"{label:>6}"
        for mb in range(len(MOVE_LABELS)):
            n = len(pnl_grid[k_idx][mb])
            row += f"  {n:>{col_w}}"
        print(row)

    # ── Efficiency Grid ───────────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  EFFICIENCY GRID  (avg PnL / entry_cost, %)")
    print("  Positive = profitable after theta. Higher = better bang per buck.")
    print("=" * 80)
    print()

    header = f"{'K':>6}"
    for label in MOVE_LABELS:
        header += f"  {label:>{col_w}}"
    print(header)
    print("─" * len(header))

    for k_idx, K in enumerate(OFFSETS):
        label = "ATM" if K == 0 else f"±{K}"
        row = f"{label:>6}"
        for mb in range(len(MOVE_LABELS)):
            vals = eff_grid[k_idx][mb]
            if vals:
                avg = sum(vals) / len(vals)
                row += f"  {avg:>{col_w-1}.1f}%"
            else:
                row += f"  {'—':>{col_w}}"
        print(row)

    # ── Best K per move bucket ────────────────────────────────────────────
    print()
    print("=" * 80)
    print("  OPTIMAL STRUCTURE PER MOVE SIZE")
    print("=" * 80)
    print()

    for mb, label in enumerate(MOVE_LABELS):
        best_eff = -999999
        best_k = "?"
        best_pnl = 0
        n = 0
        for k_idx, K in enumerate(OFFSETS):
            vals = eff_grid[k_idx][mb]
            if vals:
                avg_eff = sum(vals) / len(vals)
                avg_pnl = sum(pnl_grid[k_idx][mb]) / len(pnl_grid[k_idx][mb])
                if avg_eff > best_eff:
                    best_eff = avg_eff
                    best_k = "ATM straddle" if K == 0 else f"±${K:,} strangle"
                    best_pnl = avg_pnl
                    n = len(vals)
        if n > 0:
            print(f"  Move {label:>10}  →  {best_k:<22}  avg PnL ${best_pnl:>+8,.1f}   eff {best_eff:>+6.1f}%   (n={n})")
        else:
            print(f"  Move {label:>10}  →  no data")

    print()


if __name__ == "__main__":
    main()
