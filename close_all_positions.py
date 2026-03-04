#!/usr/bin/env python3
"""
Emergency Position Closer / Kill Switch Prototype
==================================================
STATUS: TO BE USED LATER — standalone utility, not yet integrated into the
        dashboard kill-switch or automated shutdown flow.

Closes ALL open option positions using two-phase execution:

  Phase 1 — Mark Price (5 min, reprice every 30s)
      Places limit sell orders at the exchange mark price for each position.
      Reprices every 30 seconds with fresh mark data.  This phase gets fills
      on liquid legs (typically calls) at fair value.

  Phase 2 — Aggressive (2 min, reprice every 15s)
      For any legs still unfilled after Phase 1, drops the sell price 10%
      below mark (or raises buy price 10% above mark for shorts).  This
      ensures fills even on illiquid legs with empty orderbooks.

Design notes:
  - Bypasses LimitFillManager because that class requires bids AND asks
    in the orderbook.  Short-DTE puts often have empty bid books, so this
    script prices directly from the mark price returned by the positions API.
  - Each leg is tracked independently (LegState class).
  - Safe to re-run: if some positions are already closed, they won't appear
    in the positions query and will be skipped.
  - Can serve as the basis for a dashboard kill-switch that triggers
    immediate close-all from the web UI with a single click.

Future integration points:
  - dashboard.py kill-switch route could instantiate this logic
  - trade_lifecycle.py forced-close could use mark-price fallback
  - Telegram /killswitch command handler

Usage:
    python close_all_positions.py
"""

import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("close_all")

from config import API_KEY, API_SECRET, BASE_URL
from account_manager import AccountManager
from trade_execution import TradeExecutor

am = AccountManager()
executor = TradeExecutor()

# ── Fetch positions ──────────────────────────────────────────────────────────
positions = am.get_positions()
if not positions:
    print("\n✓ No open positions.")
    exit(0)

print(f"\n{'='*60}")
print(f"  Found {len(positions)} open position(s) to close")
print(f"{'='*60}")
for p in positions:
    side_str = "LONG" if p['trade_side'] == 1 else "SHORT"
    print(f"  {p['symbol']}  {side_str} {p['qty']}  entry={p['avg_price']}  mark={p['mark_price']:.2f}  upnl={p['unrealized_pnl']:.4f}")
print()


# ── Track each leg ──────────────────────────────────────────────────────────
class LegState:
    def __init__(self, symbol, qty, close_side, mark_price):
        self.symbol = symbol
        self.qty = qty
        self.close_side = close_side  # 2=sell to close long, 1=buy to close short
        self.mark_price = mark_price
        self.order_id = None
        self.filled = False
        self.fill_price = None

    def __repr__(self):
        side = "SELL" if self.close_side == 2 else "BUY"
        status = f"FILLED@{self.fill_price}" if self.filled else f"PENDING (order={self.order_id})"
        return f"{self.symbol} {side} {self.qty} mark=${self.mark_price:.2f} [{status}]"


legs = []
for p in positions:
    close_side = 2 if p['trade_side'] == 1 else 1
    legs.append(LegState(
        symbol=p['symbol'],
        qty=p['qty'],
        close_side=close_side,
        mark_price=p['mark_price'],
    ))


def place_or_reprice(leg, price):
    """Cancel existing order if any, then place new order at given price."""
    if leg.order_id:
        try:
            executor.cancel_order(leg.order_id)
            logger.info(f"Cancelled {leg.order_id} for {leg.symbol}")
        except Exception as e:
            logger.warning(f"Cancel failed for {leg.order_id}: {e}")
        leg.order_id = None

    side_label = "sell" if leg.close_side == 2 else "buy"
    result = executor.place_order(
        symbol=leg.symbol,
        qty=leg.qty,
        side=leg.close_side,
        order_type=1,  # limit
        price=round(price, 2),
    )
    if result:
        leg.order_id = str(result.get('orderId', ''))
        logger.info(f"Placed {side_label} {leg.qty}x {leg.symbol} @ ${price:.2f} (order {leg.order_id})")
        return True
    else:
        logger.error(f"Failed to place {side_label} order for {leg.symbol} @ ${price:.2f}")
        return False


def check_fills(legs_list):
    """Poll order status for all unfilled legs."""
    for leg in legs_list:
        if leg.filled or not leg.order_id:
            continue
        try:
            status = executor.get_order_status(leg.order_id)
            if status:
                state = status.get('state', -1)
                fill_qty = float(status.get('fillQty', 0))
                if state == 1 or fill_qty >= leg.qty:  # FILLED
                    leg.filled = True
                    leg.fill_price = float(status.get('avgPrice', 0))
                    logger.info(f"FILLED: {leg.symbol} {fill_qty} @ ${leg.fill_price}")
        except Exception as e:
            logger.warning(f"Error checking {leg.symbol}: {e}")


def refresh_marks(legs_list):
    """Refresh mark prices from exchange positions."""
    try:
        current_positions = am.get_positions()
        mark_map = {p['symbol']: p['mark_price'] for p in current_positions}
        for leg in legs_list:
            if not leg.filled and leg.symbol in mark_map:
                leg.mark_price = mark_map[leg.symbol]
    except Exception:
        pass  # keep old marks


# ── Phase 1: Mark price for 5 minutes ───────────────────────────────────────
PHASE1_DURATION = 300   # 5 minutes
PHASE1_REPRICE = 30     # reprice every 30s
PHASE2_DURATION = 120   # 2 minutes
PHASE2_REPRICE = 15
PHASE2_DISCOUNT = 0.10  # 10% below mark for sells, 10% above for buys

print("Phase 1: Placing at mark price (5 minutes, reprice every 30s)...")
start = time.time()

# Initial placement at mark price
for leg in legs:
    if not leg.filled:
        place_or_reprice(leg, leg.mark_price)

last_reprice = time.time()

while time.time() - start < PHASE1_DURATION:
    time.sleep(10)
    check_fills(legs)

    unfilled = [l for l in legs if not l.filled]
    if not unfilled:
        break

    elapsed = time.time() - start
    filled_count = sum(1 for l in legs if l.filled)
    print(f"  [{elapsed:5.0f}s] Phase 1 — {filled_count}/{len(legs)} filled, {len(unfilled)} remaining")

    # Reprice at fresh mark
    if time.time() - last_reprice >= PHASE1_REPRICE:
        refresh_marks(legs)
        for leg in unfilled:
            place_or_reprice(leg, leg.mark_price)
        last_reprice = time.time()

# Check if done
unfilled = [l for l in legs if not l.filled]
if not unfilled:
    elapsed = time.time() - start
    print(f"\n✓ All {len(legs)} positions closed in Phase 1! ({elapsed:.0f}s)")
    for leg in legs:
        print(f"  {leg}")
    exit(0)

# ── Phase 2: Aggressive pricing for 2 minutes ───────────────────────────────
print(f"\nPhase 2: Aggressive pricing ({len(unfilled)} remaining, 10% discount, 2 minutes)...")
phase2_start = time.time()

refresh_marks(legs)
for leg in unfilled:
    if leg.close_side == 2:  # selling — go below mark
        price = leg.mark_price * (1 - PHASE2_DISCOUNT)
    else:  # buying — go above mark
        price = leg.mark_price * (1 + PHASE2_DISCOUNT)
    place_or_reprice(leg, price)

last_reprice = time.time()

while time.time() - phase2_start < PHASE2_DURATION:
    time.sleep(10)
    check_fills(legs)

    unfilled = [l for l in legs if not l.filled]
    if not unfilled:
        break

    total_elapsed = time.time() - start
    p2_elapsed = time.time() - phase2_start
    filled_count = sum(1 for l in legs if l.filled)
    print(f"  [{total_elapsed:5.0f}s] Phase 2 — {filled_count}/{len(legs)} filled, {len(unfilled)} remaining")

    if time.time() - last_reprice >= PHASE2_REPRICE:
        refresh_marks(legs)
        for leg in unfilled:
            if leg.close_side == 2:
                price = leg.mark_price * (1 - PHASE2_DISCOUNT)
            else:
                price = leg.mark_price * (1 + PHASE2_DISCOUNT)
            place_or_reprice(leg, price)
        last_reprice = time.time()

# ── Summary ──────────────────────────────────────────────────────────────────
total_elapsed = time.time() - start
filled = [l for l in legs if l.filled]
unfilled = [l for l in legs if not l.filled]

print(f"\n{'='*60}")
print(f"  Execution complete — {total_elapsed:.0f}s elapsed")
print(f"{'='*60}")
print(f"  Filled: {len(filled)}/{len(legs)}")
for leg in filled:
    print(f"    {leg}")
if unfilled:
    print(f"  UNFILLED: {len(unfilled)}")
    for leg in unfilled:
        print(f"    {leg}")
    # Cancel any remaining orders
    for leg in unfilled:
        if leg.order_id:
            try:
                executor.cancel_order(leg.order_id)
            except:
                pass

# Verify
print("\nVerifying remaining positions...")
time.sleep(3)
remaining = am.get_positions()
if not remaining:
    print("✓ All positions confirmed closed.")
else:
    print(f"WARNING: {len(remaining)} position(s) still open:")
    for p in remaining:
        print(f"  {p['symbol']}  qty={p['qty']}  mark={p['mark_price']:.2f}")
