#!/usr/bin/env python3
"""Diagnose why some (hour, offset) combos are missing from the backtest."""
import os, sys
import numpy as np
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from backtester.ingest.tardis import HistoricOptionChain

PARQUET = os.path.join(os.path.dirname(__file__),
    "backtester", "ingest", "tardis", "data", "btc_0dte_1dte_2025-03-01.parquet")
EXPIRY = "2MAR25"
OFFSETS = [0, 500, 1000, 1500, 2000, 2500, 3000]

chain = HistoricOptionChain(PARQUET)
strikes = np.array(chain.strikes(EXPIRY))
print("Strikes:", list(strikes))
print()

def nearest(arr, target):
    idx = np.searchsorted(arr, target)
    cands = []
    if idx > 0: cands.append(arr[idx-1])
    if idx < len(arr): cands.append(arr[idx])
    return float(min(cands, key=lambda s: abs(s - target))) if cands else None

for hour in range(21):
    dt = datetime(2025, 3, 1, hour, 0, tzinfo=timezone.utc)
    spot = chain.get_spot(dt)
    atm = nearest(strikes, spot)
    print("=== %02d:00 UTC  spot=$%.0f  ATM=$%.0f ===" % (hour, spot, atm))

    for offset in OFFSETS:
        if offset == 0:
            cs = ps = atm
        else:
            cs = nearest(strikes, atm + offset)
            ps = nearest(strikes, atm - offset)

        call = chain.get(dt, EXPIRY, cs, is_call=True)
        put = chain.get(dt, EXPIRY, ps, is_call=False)

        if call is None:
            print("  %-10s  call strike $%.0f -> NO DATA" % ("straddle" if offset==0 else "±%d"%offset, cs))
            continue
        if put is None:
            print("  %-10s  put strike $%.0f -> NO DATA" % ("straddle" if offset==0 else "±%d"%offset, ps))
            continue

        ca = float(call["ask_price"])
        pa = float(put["ask_price"])
        cb = float(call["bid_price"])
        pb = float(put["bid_price"])
        cm = float(call["mark_price"])
        pm = float(put["mark_price"])

        reason = ""
        if np.isnan(ca): reason += " call_ask=NaN"
        if np.isnan(pa): reason += " put_ask=NaN"
        if np.isnan(cb): reason += " call_bid=NaN"
        if np.isnan(pb): reason += " put_bid=NaN"

        status = "SKIP (ask NaN)" if (np.isnan(ca) or np.isnan(pa)) else "OK"
        print("  %-10s  C=$%.0f P=$%.0f  c_ask=%.6f p_ask=%.6f c_bid=%.6f p_bid=%.6f  %s%s" % (
            "straddle" if offset==0 else "±%d"%offset,
            cs, ps, ca, pa, cb, pb, status, reason))
    print()
