#!/usr/bin/env python3
"""
Smoke test — Short Straddle/Strangle strategy dry run.

Connects to Deribit production (public endpoints only — no credentials needed)
and shows exactly what options the strategy would open right now.

Usage:
    python tests/manual/smoke_short_straddle.py
    python tests/manual/smoke_short_straddle.py --offset 500
    python tests/manual/smoke_short_straddle.py --offset 0   # ATM straddle

The script:
  1. Fetches current BTC index price and option instruments from Deribit
  2. Resolves the call + put legs using the same option_selection logic
  3. Fetches real bid/ask/mark/fair for each resolved leg
  4. Prints the complete entry snapshot — what would be executed
  5. Shows the SL threshold and max-hold exit times
"""

import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

# Make sure we can import from the project root
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)

# Force Deribit adapter + production
os.environ["EXCHANGE"] = "deribit"
os.environ["TRADING_ENVIRONMENT"] = "production"

from exchanges.deribit.auth import DeribitAuth
from exchanges.deribit.market_data import DeribitMarketDataAdapter
from option_selection import strangle_by_offset, straddle, resolve_legs


def _fair(mkt):
    """Same fair-price model as the strategy. All values in USD."""
    if not mkt:
        return None
    # DeribitMarketDataAdapter.get_option_details() returns USD prices with
    # keys: "bid", "ask", "markPrice"
    bid = float(mkt.get("bid", 0) or 0)
    ask = float(mkt.get("ask", 0) or 0)
    mark = float(mkt.get("markPrice", 0) or 0)
    if bid > 0 and ask > 0:
        fair = mark if bid <= mark <= ask else (bid + ask) / 2
    elif bid > 0:
        fair = max(mark, bid) if mark > 0 else bid
    elif mark > 0:
        fair = mark
    else:
        return None
    return {"fair": fair, "bid": bid or None, "ask": ask or None, "mark": mark}


def run(offset: int, qty: int, stop_loss_pct: float, max_hold_hours: int) -> None:
    print()
    print("=" * 64)
    print("  SHORT STRADDLE/STRANGLE — DRY RUN")
    print("=" * 64)

    # ── Connect (public endpoints — no auth needed) ──────────────────
    print("\n[1] Connecting to Deribit (public endpoints)...")
    auth = DeribitAuth(client_id="", client_secret="",
                       base_url="https://www.deribit.com")
    md = DeribitMarketDataAdapter(auth)

    # ── BTC index price ──────────────────────────────────────────────
    spot = md.get_index_price(use_cache=False)
    if not spot:
        print("ERROR: Could not fetch BTC index price. Check connectivity.")
        sys.exit(1)
    print(f"[2] BTC index price: ${spot:,.2f}")

    # ── Resolve legs ─────────────────────────────────────────────────
    print(f"\n[3] Resolving legs (offset=±${offset:,}, qty={qty}, DTE=next)...")
    if offset == 0:
        leg_specs = straddle(qty=qty, dte="next", side="sell")
        structure = "ATM Straddle"
    else:
        leg_specs = strangle_by_offset(qty=qty, offset=offset, dte="next", side="sell")
        call_target = spot + offset
        put_target = spot - offset
        structure = f"Strangle  call ≈ ${call_target:,.0f}  put ≈ ${put_target:,.0f}"

    try:
        legs = resolve_legs(leg_specs, md)
    except ValueError as exc:
        print(f"ERROR: Could not resolve legs: {exc}")
        sys.exit(1)

    call_leg = next((l for l in legs if l.symbol.endswith("-C")), None)
    put_leg = next((l for l in legs if l.symbol.endswith("-P")), None)

    if not call_leg or not put_leg:
        print("ERROR: Expected one call and one put leg.")
        sys.exit(1)

    print(f"    CALL → {call_leg.symbol}")
    print(f"    PUT  → {put_leg.symbol}")

    # ── Market data per leg ──────────────────────────────────────────
    print("\n[4] Fetching market data...")
    call_mkt = md.get_option_details(call_leg.symbol)
    put_mkt = md.get_option_details(put_leg.symbol)

    call_fp = _fair(call_mkt)
    put_fp = _fair(put_mkt)

    if not call_fp or not put_fp:
        print("WARNING: Could not compute fair price for one or both legs.")

    def _row(label, mkt, fp):
        if not mkt:
            return f"    {label}: no data"
        bid = fp["bid"] or 0 if fp else 0
        ask = fp["ask"] or 0 if fp else 0
        fair = fp["fair"] if fp else 0
        mark = fp["mark"] if fp else float(mkt.get("markPrice", 0) or 0)
        delta = float(mkt.get("delta", 0) or 0)
        # mark_iv from Deribit is already in percent (e.g. 75.3 = 75.3%)
        iv = float(mkt.get("impliedVolatility", 0) or 0)
        iv_pct = f"  IV={iv:.1f}%" if iv > 0 else ""
        return (
            f"    {label}: bid=${bid:,.2f}  ask=${ask:,.2f}  "
            f"mark=${mark:,.2f}  fair=${fair:,.2f}  "
            f"delta={delta:+.3f}{iv_pct}"
        )

    print(_row("CALL", call_mkt, call_fp))
    print(_row("PUT ", put_mkt, put_fp))

    # ── Entry snapshot ───────────────────────────────────────────────
    call_fair = call_fp["fair"] if call_fp else 0.0
    put_fair = put_fp["fair"] if put_fp else 0.0
    combined_fair = call_fair + put_fair

    call_bid = call_fp["bid"] or 0 if call_fp else 0
    put_bid = put_fp["bid"] or 0 if put_fp else 0
    combined_bid = call_bid + put_bid

    now_utc = datetime.now(timezone.utc)
    sl_threshold = combined_fair * (1.0 + stop_loss_pct)
    max_hold_exit = now_utc + timedelta(hours=max_hold_hours)

    print()
    print("─" * 64)
    print(f"  Structure:        {structure}")
    print(f"  Offset:           ±${offset:,}")
    print()
    print(f"  Phase 1 open (at fair, 30s):")
    print(f"    SELL CALL @ ${call_fair:,.2f}")
    print(f"    SELL PUT  @ ${put_fair:,.2f}")
    print(f"    Combined fair:  ${combined_fair:,.2f}")
    print()
    print(f"  Phase 2 open (aggressive at bid, 30s — if Phase 1 unfilled):")
    print(f"    SELL CALL @ ${call_bid:,.2f}")
    print(f"    SELL PUT  @ ${put_bid:,.2f}")
    print(f"    Combined bid:   ${combined_bid:,.2f}")
    print()
    print(f"  Risk parameters (using fair as proxy for fill):")
    print(f"    Stop-loss pct:  {stop_loss_pct:.0%}")
    print(f"    SL threshold:   ${sl_threshold:,.2f}  (combined fair × {1 + stop_loss_pct:.1f})")
    print(f"    SL fires when buyback costs ${combined_fair * (1 + stop_loss_pct):,.2f}")
    print(f"    Max hold:       {max_hold_hours}h  → force close by {max_hold_exit.strftime('%Y-%m-%d %H:%M UTC')}")
    print("─" * 64)
    print()

    # ── Expiry info ──────────────────────────────────────────────────
    instruments = md.get_option_instruments("BTC") or []
    call_inst = next((i for i in instruments if i["symbolName"] == call_leg.symbol), None)
    if call_inst:
        exp_ts = call_inst["expirationTimestamp"] / 1000
        exp_dt = datetime.fromtimestamp(exp_ts, tz=timezone.utc)
        dte_h = (exp_dt - now_utc).total_seconds() / 3600
        print(f"  Expiry:  {exp_dt.strftime('%Y-%m-%d %H:%M UTC')}  ({dte_h:.1f}h from now)")
        print()

    print("  ✓  Dry run complete — no orders placed.")
    print()


def main():
    parser = argparse.ArgumentParser(description="Short straddle/strangle dry run")
    parser.add_argument("--offset", type=int, default=1000,
                        help="USD offset from spot (default: 1000; use 0 for ATM straddle)")
    parser.add_argument("--qty", type=int, default=1,
                        help="Contracts per leg (default: 1)")
    parser.add_argument("--sl", type=float, default=3.0,
                        help="Stop-loss as fraction of premium (default: 3.0 = 300%%)")
    parser.add_argument("--hold", type=int, default=20,
                        help="Max hold hours (default: 20)")
    args = parser.parse_args()
    run(offset=args.offset, qty=args.qty, stop_loss_pct=args.sl, max_hold_hours=args.hold)


if __name__ == "__main__":
    main()
