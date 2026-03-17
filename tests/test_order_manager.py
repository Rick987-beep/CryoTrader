#!/usr/bin/env python3
"""
Unit tests for OrderManager — the central order ledger.

Tests 1–15 from the Phase 1 test plan.
All tests use a MockExecutor — no API calls.

Run:
    python3 tests/test_order_manager.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import (
    OrderManager, OrderRecord, OrderPurpose, OrderStatus,
    LOGS_DIR,
)

# Track results
_results = []
passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  ✓ {name}" + (f"  ({detail})" if detail else ""))
        passed += 1
    else:
        print(f"  ✗ {name}" + (f"  ({detail})" if detail else ""))
        failed += 1


# =============================================================================
# Mock Executor — records all calls, returns configurable responses
# =============================================================================

class MockExecutor:
    """
    Mock TradeExecutor that records calls and returns configurable results.

    Usage:
        mock = MockExecutor()
        mock.place_order(...)  # Records the call, returns {'orderId': '1001'}
        mock.calls  # [('place_order', {...}), ...]
    """

    def __init__(self):
        self.calls = []
        self._next_order_id = 1001
        self._order_statuses = {}  # order_id → status dict
        self._cancel_fail_ids = set()  # order_ids where cancel should fail

    def place_order(self, symbol, qty, side, order_type=1, price=None,
                    client_order_id=None, reduce_only=False):
        self.calls.append(("place_order", {
            "symbol": symbol, "qty": qty, "side": side,
            "order_type": order_type, "price": price,
            "client_order_id": client_order_id,
            "reduce_only": reduce_only,
        }))
        oid = str(self._next_order_id)
        self._next_order_id += 1
        # Default status: NEW (state=0)
        self._order_statuses[oid] = {
            "orderId": int(oid),
            "symbol": symbol,
            "qty": qty,
            "fillQty": 0,
            "remainQty": qty,
            "price": price,
            "avgPrice": 0,
            "state": 0,  # NEW
        }
        return {"orderId": oid}

    def cancel_order(self, order_id):
        self.calls.append(("cancel_order", {"order_id": order_id}))
        if order_id in self._cancel_fail_ids:
            return False
        # Update mock status to CANCELED
        if order_id in self._order_statuses:
            self._order_statuses[order_id]["state"] = 3  # CANCELED
        return True

    def get_order_status(self, order_id):
        self.calls.append(("get_order_status", {"order_id": order_id}))
        return self._order_statuses.get(order_id)

    # --- Test helpers ---

    def simulate_fill(self, order_id, filled_qty, avg_price, full=True):
        """Simulate an order being filled on the exchange."""
        if order_id in self._order_statuses:
            s = self._order_statuses[order_id]
            s["fillQty"] = filled_qty
            s["avgPrice"] = avg_price
            s["remainQty"] = s["qty"] - filled_qty
            s["state"] = 1 if full else 2  # FILLED or PARTIAL

    def simulate_cancel(self, order_id):
        """Simulate an order being cancelled externally."""
        if order_id in self._order_statuses:
            self._order_statuses[order_id]["state"] = 3


# =============================================================================
# Helper to get a clean OrderManager (no leftover persistence files)
# =============================================================================

def fresh_om(mock=None):
    """Create a fresh OrderManager with a new MockExecutor."""
    m = mock or MockExecutor()
    om = OrderManager(m)
    return om, m


# =============================================================================
# Test 1: Idempotent placement
# =============================================================================

print("=== Test 1: Idempotent placement ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
r2 = om.place_order(
    lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)

check("first placement returns a record", r1 is not None)
check("second placement returns same record", r2 is not None and r2.order_id == r1.order_id)

place_calls = [c for c in mock.calls if c[0] == "place_order"]
check("executor.place_order called only once", len(place_calls) == 1,
      f"got {len(place_calls)}")


# =============================================================================
# Test 2: Placement records correct fields
# =============================================================================

print("\n=== Test 2: Placement records correct fields ===")

om, mock = fresh_om()
r = om.place_order(
    lifecycle_id="trade-2", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-90000-P", side=2, qty=0.5, price=300.0,
)

check("order_id is set", r is not None and r.order_id != "")
check("lifecycle_id correct", r.lifecycle_id == "trade-2")
check("leg_index correct", r.leg_index == 1)
check("purpose correct", r.purpose == OrderPurpose.OPEN_LEG)
check("symbol correct", r.symbol == "BTCUSD-28MAR26-90000-P")
check("side correct", r.side == 2)
check("qty correct", r.qty == 0.5)
check("price correct", r.price == 300.0)
check("status is PENDING", r.status == OrderStatus.PENDING)
check("placed_at is recent", abs(r.placed_at - time.time()) < 5)
check("client_order_id is set", r.client_order_id is not None)


# =============================================================================
# Test 3: reduce_only forced for CLOSE_LEG and UNWIND
# =============================================================================

print("\n=== Test 3: reduce_only forced for CLOSE_LEG/UNWIND ===")

om, mock = fresh_om()

# CLOSE_LEG — caller passes reduce_only=False, but it should be forced True
r_close = om.place_order(
    lifecycle_id="trade-3", leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=2, qty=0.1, price=500.0,
    reduce_only=False,
)
check("CLOSE_LEG record has reduce_only=True", r_close is not None and r_close.reduce_only)

close_call = [c for c in mock.calls if c[0] == "place_order"][-1]
check("executor called with reduce_only=True for CLOSE_LEG",
      close_call[1]["reduce_only"] is True)

# UNWIND — same behavior
r_unwind = om.place_order(
    lifecycle_id="trade-3", leg_index=1, purpose=OrderPurpose.UNWIND,
    symbol="BTCUSD-28MAR26-90000-P", side=1, qty=0.1, price=300.0,
    reduce_only=False,
)
check("UNWIND record has reduce_only=True", r_unwind is not None and r_unwind.reduce_only)

unwind_call = [c for c in mock.calls if c[0] == "place_order"][-1]
check("executor called with reduce_only=True for UNWIND",
      unwind_call[1]["reduce_only"] is True)

# OPEN_LEG — should respect caller's choice
r_open = om.place_order(
    lifecycle_id="trade-3", leg_index=2, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-80000-C", side=1, qty=0.1, price=400.0,
    reduce_only=False,
)
check("OPEN_LEG respects reduce_only=False", r_open is not None and not r_open.reduce_only)


# =============================================================================
# Test 4: Hard cap — max orders per lifecycle
# =============================================================================

print("\n=== Test 4: Hard cap — max orders per lifecycle ===")

om, mock = fresh_om()
om.MAX_ORDERS_PER_LIFECYCLE = 3  # Lower for testing

for i in range(3):
    r = om.place_order(
        lifecycle_id="trade-cap", leg_index=i, purpose=OrderPurpose.OPEN_LEG,
        symbol=f"BTCUSD-28MAR26-{80000 + i * 1000}-C", side=1, qty=0.1, price=100.0,
    )
    check(f"order {i+1}/3 placed", r is not None)

# 4th should be refused
r4 = om.place_order(
    lifecycle_id="trade-cap", leg_index=3, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-83000-C", side=1, qty=0.1, price=100.0,
)
check("4th order refused (hard cap)", r4 is None)


# =============================================================================
# Test 5: Hard cap — max pending per symbol
# =============================================================================

print("\n=== Test 5: Hard cap — max pending per symbol ===")

om, mock = fresh_om()
om.MAX_PENDING_PER_SYMBOL = 2  # Lower for testing

r_a = om.place_order(
    lifecycle_id="trade-sym-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
r_b = om.place_order(
    lifecycle_id="trade-sym-2", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=510.0,
)
check("first two orders placed for same symbol", r_a is not None and r_b is not None)

r_c = om.place_order(
    lifecycle_id="trade-sym-3", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=520.0,
)
check("3rd order for same symbol refused", r_c is None)

# Different symbol should still work
r_d = om.place_order(
    lifecycle_id="trade-sym-3", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-90000-P", side=2, qty=0.1, price=300.0,
)
check("different symbol still works", r_d is not None)


# =============================================================================
# Test 6: cancel_order
# =============================================================================

print("\n=== Test 6: cancel_order ===")

om, mock = fresh_om()
r = om.place_order(
    lifecycle_id="trade-cancel", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
check("order placed", r is not None)
check("order is live before cancel", r.is_live)

ok = om.cancel_order(r.order_id)
check("cancel_order returns True", ok)
check("order is terminal after cancel", r.is_terminal)
check("order status is CANCELLED", r.status == OrderStatus.CANCELLED)
check("terminal_at is set", r.terminal_at is not None)

# Active slot should be cleared — can place a new order for same key
r2 = om.place_order(
    lifecycle_id="trade-cancel", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=510.0,
)
check("replacement order placed after cancel", r2 is not None and r2.order_id != r.order_id)


# =============================================================================
# Test 7: requote_order — cancel + replace + chain
# =============================================================================

print("\n=== Test 7: requote_order ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-rq", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.5, price=500.0,
)
check("initial order placed", r1 is not None)

r2 = om.requote_order(r1.order_id, new_price=510.0)
check("requote returns new record", r2 is not None)
check("new order has different ID", r2.order_id != r1.order_id)
check("new order has new price", r2.price == 510.0)
check("new order qty = original (no partial fill)", r2.qty == 0.5)
check("old order superseded_by points to new", r1.superseded_by == r2.order_id)
check("new order supersedes points to old", r2.supersedes == r1.order_id)
check("old order is terminal", r1.is_terminal)


# =============================================================================
# Test 8: requote fully-filled order → None
# =============================================================================

print("\n=== Test 8: requote fully-filled order ===")

om, mock = fresh_om()
r = om.place_order(
    lifecycle_id="trade-rq-filled", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
# Simulate full fill on exchange
mock.simulate_fill(r.order_id, filled_qty=0.1, avg_price=499.0, full=True)

result = om.requote_order(r.order_id, new_price=510.0)
check("requote returns None for filled order", result is None)
check("original order is now FILLED", r.status == OrderStatus.FILLED)


# =============================================================================
# Test 9: poll_order updates status/fills
# =============================================================================

print("\n=== Test 9: poll_order ===")

om, mock = fresh_om()
r = om.place_order(
    lifecycle_id="trade-poll", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=1.0, price=500.0,
)

# Simulate partial fill
mock.simulate_fill(r.order_id, filled_qty=0.3, avg_price=499.5, full=False)
om.poll_order(r.order_id)
check("partial fill detected", r.filled_qty == 0.3)
check("avg_fill_price updated", r.avg_fill_price == 499.5)
check("status is PARTIAL", r.status == OrderStatus.PARTIAL)
check("order still live", r.is_live)

# Simulate full fill
mock.simulate_fill(r.order_id, filled_qty=1.0, avg_price=499.8, full=True)
om.poll_order(r.order_id)
check("full fill detected", r.filled_qty == 1.0)
check("status is FILLED", r.status == OrderStatus.FILLED)
check("order is terminal", r.is_terminal)


# =============================================================================
# Test 10: poll_all updates all non-terminal orders
# =============================================================================

print("\n=== Test 10: poll_all ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-pa", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
r2 = om.place_order(
    lifecycle_id="trade-pa", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-90000-P", side=2, qty=0.2, price=300.0,
)

# Fill r1, leave r2 as NEW
mock.simulate_fill(r1.order_id, filled_qty=0.1, avg_price=500.0, full=True)

status_calls_before = len([c for c in mock.calls if c[0] == "get_order_status"])
om.poll_all()
status_calls_after = len([c for c in mock.calls if c[0] == "get_order_status"])

check("poll_all polled 2 orders", status_calls_after - status_calls_before == 2)
check("r1 is now FILLED after poll_all", r1.status == OrderStatus.FILLED)
check("r2 is still LIVE after poll_all", r2.status == OrderStatus.LIVE)

# Poll again — r1 is terminal, should only poll r2
status_calls_before = len([c for c in mock.calls if c[0] == "get_order_status"])
om.poll_all()
status_calls_after = len([c for c in mock.calls if c[0] == "get_order_status"])
check("second poll_all only polls 1 (r2)", status_calls_after - status_calls_before == 1)


# =============================================================================
# Test 11: get_filled_for_leg — aggregates across supersession chain
# =============================================================================

print("\n=== Test 11: get_filled_for_leg aggregation ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-agg", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=1.0, price=500.0,
)
# Simulate partial fill before requote
mock.simulate_fill(r1.order_id, filled_qty=0.3, avg_price=499.0, full=False)
om.poll_order(r1.order_id)

# Requote for remaining
r2 = om.requote_order(r1.order_id, new_price=510.0)
# Simulate fill of the replacement
mock.simulate_fill(r2.order_id, filled_qty=0.7, avg_price=509.0, full=True)
om.poll_order(r2.order_id)

total_qty, vwap = om.get_filled_for_leg("trade-agg", 0, OrderPurpose.OPEN_LEG)
check("total filled qty is 1.0", abs(total_qty - 1.0) < 0.001, f"got {total_qty}")
expected_vwap = (0.3 * 499.0 + 0.7 * 509.0) / 1.0
check("VWAP is correct", abs(vwap - expected_vwap) < 0.01,
      f"got {vwap:.2f}, expected {expected_vwap:.2f}")


# =============================================================================
# Test 12: has_live_orders
# =============================================================================

print("\n=== Test 12: has_live_orders ===")

om, mock = fresh_om()
check("no live orders initially", not om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG))

r = om.place_order(
    lifecycle_id="trade-x", leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=2, qty=0.1, price=500.0,
)
check("has live orders after placement", om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG))
check("no live OPEN_LEG orders", not om.has_live_orders("trade-x", OrderPurpose.OPEN_LEG))

om.cancel_order(r.order_id)
check("no live orders after cancel", not om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG))


# =============================================================================
# Test 13: cancel_all_for
# =============================================================================

print("\n=== Test 13: cancel_all_for ===")

om, mock = fresh_om()
om.place_order(
    lifecycle_id="trade-caf", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)
om.place_order(
    lifecycle_id="trade-caf", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-90000-P", side=2, qty=0.1, price=300.0,
)
# Different lifecycle — should NOT be cancelled
om.place_order(
    lifecycle_id="trade-other", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-80000-C", side=1, qty=0.1, price=200.0,
)

count = om.cancel_all_for("trade-caf")
check("cancelled 2 orders", count == 2, f"got {count}")
check("no live orders for trade-caf", not om.has_live_orders("trade-caf", OrderPurpose.OPEN_LEG))
check("trade-other still has live orders",
      om.has_live_orders("trade-other", OrderPurpose.OPEN_LEG))


# =============================================================================
# Test 14: Persistence — save & load snapshot
# =============================================================================

print("\n=== Test 14: Persistence round-trip ===")

# Use a temp directory for logs
import tempfile
_orig_logs = order_manager_mod = None

# We need to patch LOGS_DIR for this test
import order_manager as om_module
_orig_logs = om_module.LOGS_DIR
tmp_dir = tempfile.mkdtemp()
om_module.LOGS_DIR = tmp_dir

try:
    om, mock = fresh_om()
    r1 = om.place_order(
        lifecycle_id="trade-persist", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
        symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.5, price=500.0,
    )
    r2 = om.place_order(
        lifecycle_id="trade-persist", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
        symbol="BTCUSD-28MAR26-90000-P", side=2, qty=0.3, price=300.0,
    )
    # Fill r2 — should NOT appear in snapshot (terminal)
    mock.simulate_fill(r2.order_id, filled_qty=0.3, avg_price=299.0, full=True)
    om.poll_order(r2.order_id)

    om.persist_snapshot()

    # Load into a new manager
    om2 = OrderManager(mock)
    om2.load_snapshot()

    loaded = om2.get_all_orders("trade-persist")
    check("loaded 1 non-terminal order from snapshot", len(loaded) == 1,
          f"got {len(loaded)}")
    if loaded:
        check("loaded order has correct symbol", loaded[0].symbol == "BTCUSD-28MAR26-100000-C")
        check("loaded order has correct qty", loaded[0].qty == 0.5)
        check("idempotency index rebuilt",
              om2.has_live_orders("trade-persist", OrderPurpose.OPEN_LEG))
finally:
    om_module.LOGS_DIR = _orig_logs
    # Clean up temp files
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)


# =============================================================================
# Test 15: Persistence — event log (JSONL audit trail)
# =============================================================================

print("\n=== Test 15: Event log (JSONL) ===")

import order_manager as om_module
_orig_logs = om_module.LOGS_DIR
tmp_dir = tempfile.mkdtemp()
om_module.LOGS_DIR = tmp_dir

try:
    om, mock = fresh_om()
    r = om.place_order(
        lifecycle_id="trade-log", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
        symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
    )
    om.cancel_order(r.order_id)

    ledger_path = os.path.join(tmp_dir, "order_ledger.jsonl")
    check("ledger file created", os.path.exists(ledger_path))

    with open(ledger_path, "r") as f:
        lines = [json.loads(line) for line in f if line.strip()]

    check("at least 2 events logged", len(lines) >= 2, f"got {len(lines)}")
    actions = [l["action"] for l in lines]
    check("'placed' event in log", "placed" in actions)
    check("terminal event in log", any("terminal" in a for a in actions))
    if lines:
        check("event has order_id", "order_id" in lines[0])
        check("event has symbol", "symbol" in lines[0])
finally:
    om_module.LOGS_DIR = _orig_logs
    shutil.rmtree(tmp_dir, ignore_errors=True)


# =============================================================================
# Test 16: Requote with partial fill — remaining qty
# =============================================================================

print("\n=== Test 16: Requote with partial fill ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-rq-partial", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=1.0, price=500.0,
)
# Partially fill 0.4 of 1.0
mock.simulate_fill(r1.order_id, filled_qty=0.4, avg_price=499.0, full=False)

r2 = om.requote_order(r1.order_id, new_price=510.0)
check("requote succeeds after partial fill", r2 is not None)
check("new order qty is remaining 0.6", r2 is not None and abs(r2.qty - 0.6) < 0.001,
      f"got {r2.qty if r2 else 'None'}")


# =============================================================================
# Test 17: OrderRecord serialization round-trip
# =============================================================================

print("\n=== Test 17: OrderRecord to_dict/from_dict ===")

record = OrderRecord(
    order_id="test-123",
    client_order_id="456",
    lifecycle_id="trade-rt",
    leg_index=2,
    purpose=OrderPurpose.CLOSE_LEG,
    symbol="BTCUSD-28MAR26-100000-C",
    side=2,
    qty=0.5,
    price=500.0,
    reduce_only=True,
    status=OrderStatus.PARTIAL,
    filled_qty=0.2,
    avg_fill_price=498.0,
    placed_at=1000000.0,
    updated_at=1000010.0,
    terminal_at=None,
    superseded_by="test-456",
    supersedes="test-000",
)

d = record.to_dict()
restored = OrderRecord.from_dict(d)

check("order_id round-trips", restored.order_id == "test-123")
check("purpose round-trips", restored.purpose == OrderPurpose.CLOSE_LEG)
check("status round-trips", restored.status == OrderStatus.PARTIAL)
check("filled_qty round-trips", restored.filled_qty == 0.2)
check("avg_fill_price round-trips", restored.avg_fill_price == 498.0)
check("superseded_by round-trips", restored.superseded_by == "test-456")
check("reduce_only round-trips", restored.reduce_only is True)


# =============================================================================
# Test 18: Reconciliation
# =============================================================================

print("\n=== Test 18: Reconciliation ===")

om, mock = fresh_om()
r1 = om.place_order(
    lifecycle_id="trade-recon", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C", side=1, qty=0.1, price=500.0,
)

# Exchange shows our order + an orphan
exchange_orders = [
    {"order_id": r1.order_id},
    {"order_id": "999999"},  # orphan — not in our ledger
]
warnings = om.reconcile(exchange_orders)
check("orphan detected", any("999999" in w for w in warnings))
check("our order not flagged", not any(r1.order_id in w and "not found" in w for w in warnings))

# Now simulate exchange showing our order as gone
# Promote order to LIVE first (reconcile skips PENDING orders)
r1.status = OrderStatus.LIVE
r1.placed_at = r1.placed_at - 60  # past grace period
exchange_orders_empty = []
warnings2 = om.reconcile(exchange_orders_empty)
check("phantom ledger order detected", any(r1.order_id in w for w in warnings2))


# =============================================================================
# Summary
# =============================================================================

print(f"\n{'=' * 60}")
print(f"Results: {passed} passed, {failed} failed")
print(f"{'=' * 60}")

if failed == 0:
    print("All tests passed!")
else:
    print(f"FAILURES: {failed}")
    sys.exit(1)
