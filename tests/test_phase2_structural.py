#!/usr/bin/env python3
"""
Phase 2 Structural Tests

Validates the trade_lifecycle.py → lifecycle_engine.py + execution_router.py
split, backward compatibility, position_closer order_manager integration,
and crash recovery with order ledger.

Run:
    python3 tests/test_phase2_structural.py
"""

import json
import os
import sys
import time
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
# Mock Executor (no API calls)
# =============================================================================

class MockExecutor:
    """Minimal mock for TradeExecutor — tracks calls, returns fake IDs."""

    def __init__(self):
        self._next_id = 1000
        self.placed = []
        self.cancelled = []
        self.poll_responses = {}  # order_id → status dict

    def place_order(self, **kwargs):
        self._next_id += 1
        oid = str(self._next_id)
        self.placed.append({"orderId": oid, **kwargs})
        return {"orderId": oid}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_order_status(self, order_id):
        if order_id in self.poll_responses:
            return self.poll_responses[order_id]
        return {"state": 0, "fillQty": "0", "avgPrice": "0", "orderId": order_id}


# =============================================================================
# Test 1: Backward-compatible imports from trade_lifecycle
# =============================================================================

print("\n=== Test 1: Core imports from trade_lifecycle ===")

from trade_lifecycle import (
    TradeState,
    TradeLeg,
    TradeLifecycle,
    RFQParams,
    ExitCondition,
)

check("TradeState importable from trade_lifecycle", TradeState is not None)
check("TradeLeg importable from trade_lifecycle", TradeLeg is not None)
check("TradeLifecycle importable from trade_lifecycle", TradeLifecycle is not None)
check("RFQParams importable from trade_lifecycle", RFQParams is not None)
check("ExitCondition importable from trade_lifecycle", ExitCondition is not None)

from lifecycle_engine import LifecycleEngine


# =============================================================================
# Test 2: Direct imports from new modules
# =============================================================================

print("\n=== Test 2: Direct imports from new modules ===")

from lifecycle_engine import LifecycleEngine as LE2
from execution_router import ExecutionRouter
from order_manager import OrderManager, OrderPurpose, OrderStatus

check("LifecycleEngine importable from lifecycle_engine", LE2 is not None)
check("ExecutionRouter importable from execution_router", ExecutionRouter is not None)
check("OrderManager importable from order_manager", OrderManager is not None)


# =============================================================================
# Test 3: ExecutionRouter construction
# =============================================================================

print("\n=== Test 3: ExecutionRouter construction ===")

mock = MockExecutor()
om = OrderManager(mock)

# Minimal mock for RFQ executor
class FakeRFQExecutor:
    pass

router = ExecutionRouter(
    executor=mock,
    rfq_executor=FakeRFQExecutor(),
    order_manager=om,
    rfq_notional_threshold=50000.0,
)

check("ExecutionRouter constructs with all deps", router is not None)
check("ExecutionRouter has open method", hasattr(router, 'open'))
check("ExecutionRouter has close method", hasattr(router, 'close'))
check("rfq_notional_threshold set", router.rfq_notional_threshold == 50000.0)


# =============================================================================
# Test 4: LifecycleEngine API surface
# =============================================================================

print("\n=== Test 4: LifecycleEngine API surface ===")

# Check that LifecycleEngine has all the expected methods
expected_methods = [
    'create', 'open', 'close', 'tick',
    'force_close', 'kill_all', 'cancel',
    'restore_trade', 'get', 'status_report',
    'get_trades_for_strategy', 'active_trades_for_strategy',
]
expected_properties = ['active_trades', 'all_trades', 'order_manager']

for name in expected_methods:
    check(f"LifecycleEngine.{name} exists", hasattr(LifecycleEngine, name))

for name in expected_properties:
    # Properties are descriptors on the class
    check(
        f"LifecycleEngine.{name} property exists",
        isinstance(getattr(LifecycleEngine, name, None), property),
    )


# =============================================================================
# Test 5: ExecutionRouter limit mode open (with mock, patched orderbook)
# =============================================================================

print("\n=== Test 5: ExecutionRouter limit open ===")

# Patch get_option_orderbook so LimitFillManager gets a fake price
import trade_execution as _te_mod

_orig_get_ob = _te_mod.get_option_orderbook


def _fake_orderbook(symbol):
    """Return a synthetic orderbook with bid/ask for any symbol."""
    return {
        "bids": [{"price": "100.0", "qty": "10"}],
        "asks": [{"price": "101.0", "qty": "10"}],
    }


_te_mod.get_option_orderbook = _fake_orderbook

mock2 = MockExecutor()
om2 = OrderManager(mock2)
router2 = ExecutionRouter(
    executor=mock2,
    rfq_executor=FakeRFQExecutor(),
    order_manager=om2,
)

trade = TradeLifecycle(
    open_legs=[TradeLeg(symbol="BTCUSD-TEST-100000-C", qty=0.1, side=1)],
    execution_mode="limit",
)

result = router2.open(trade)
check("limit open returns True", result is True)
check("trade state is OPENING", trade.state == TradeState.OPENING)
check("1 order placed", len(mock2.placed) == 1)
check(
    "placed order has correct symbol",
    mock2.placed[0]["symbol"] == "BTCUSD-TEST-100000-C",
)
check("fill manager stored in metadata", "_open_fill_mgr" in trade.metadata)

# Restore original
_te_mod.get_option_orderbook = _orig_get_ob


# =============================================================================
# Test 6: ExecutionRouter limit close (with mock, patched orderbook)
# =============================================================================

print("\n=== Test 6: ExecutionRouter limit close ===")

_te_mod.get_option_orderbook = _fake_orderbook
close_trade = TradeLifecycle(
    open_legs=[
        TradeLeg(symbol="BTCUSD-TEST-100000-C", qty=0.1, side=1, filled_qty=0.1, fill_price=500.0),
    ],
    execution_mode="limit",
    state=TradeState.OPEN,
)
close_trade.opened_at = time.time() - 60

mock3 = MockExecutor()
om3 = OrderManager(mock3)
router3 = ExecutionRouter(
    executor=mock3,
    rfq_executor=FakeRFQExecutor(),
    order_manager=om3,
)

result = router3.close(close_trade)
check("limit close returns True", result is True)
check("close trade state is CLOSING", close_trade.state == TradeState.CLOSING)
check("close legs created", len(close_trade.close_legs) == 1)
check(
    "close leg has opposite side",
    close_trade.close_legs[0].side == 2,  # original was buy (1), close is sell (2)
)
check("fill manager stored for close", "_close_fill_mgr" in close_trade.metadata)
check(
    "close order placed with reduce_only",
    any(p.get("reduce_only") for p in mock3.placed),
)
_te_mod.get_option_orderbook = _orig_get_ob

# =============================================================================
# Test 7: ExecutionRouter close circuit breaker
# =============================================================================

print("\n=== Test 7: Close circuit breaker ===")

breaker_trade = TradeLifecycle(
    open_legs=[
        TradeLeg(symbol="BTCUSD-TEST-CB", qty=0.1, side=1, filled_qty=0.1, fill_price=500.0),
    ],
    execution_mode="limit",
    state=TradeState.OPEN,
    metadata={"_close_attempt_count": 10},  # Already at max
)
breaker_trade.opened_at = time.time() - 60

mock4 = MockExecutor()
om4 = OrderManager(mock4)
router4 = ExecutionRouter(
    executor=mock4,
    rfq_executor=FakeRFQExecutor(),
    order_manager=om4,
)

result = router4.close(breaker_trade)
check("circuit breaker returns False", result is False)
check("trade state is FAILED", breaker_trade.state == TradeState.FAILED)
check("error mentions max attempts", "10 attempts" in (breaker_trade.error or ""))


# =============================================================================
# Test 8: trade_lifecycle.py is data-only
# =============================================================================

print("\n=== Test 8: trade_lifecycle.py is data-only ===")

import inspect
import trade_lifecycle

# Get all classes defined directly in trade_lifecycle (not imported)
tl_classes = [
    name for name, obj in inspect.getmembers(trade_lifecycle, inspect.isclass)
    if obj.__module__ == "trade_lifecycle"
]

check(
    "TradeState defined in trade_lifecycle",
    "TradeState" in tl_classes,
)
check(
    "TradeLeg defined in trade_lifecycle",
    "TradeLeg" in tl_classes,
)
check(
    "TradeLifecycle defined in trade_lifecycle",
    "TradeLifecycle" in tl_classes,
)


# =============================================================================
# Test 9: LifecycleEngine is defined in lifecycle_engine module
# =============================================================================

print("\n=== Test 9: Module locations ===")

import lifecycle_engine
import execution_router

le_classes = [
    name for name, obj in inspect.getmembers(lifecycle_engine, inspect.isclass)
    if obj.__module__ == "lifecycle_engine"
]
er_classes = [
    name for name, obj in inspect.getmembers(execution_router, inspect.isclass)
    if obj.__module__ == "execution_router"
]

check("LifecycleEngine defined in lifecycle_engine", "LifecycleEngine" in le_classes)
check("ExecutionRouter defined in execution_router", "ExecutionRouter" in er_classes)


# =============================================================================
# Test 10: Position closer cancel_all integration
# =============================================================================

print("\n=== Test 10: PositionCloser uses order_manager.cancel_all ===")

# Verify the code path exists by reading the source
import position_closer
source = inspect.getsource(position_closer.PositionCloser._run)
check(
    "position_closer._run calls order_manager.cancel_all",
    "order_manager.cancel_all" in source,
)
check(
    "position_closer._run still calls kill_all first",
    "kill_all" in source,
)


# =============================================================================
# Test 11: Crash recovery loads order ledger
# =============================================================================

print("\n=== Test 11: Crash recovery order ledger loading ===")

import main as main_module
recover_source = inspect.getsource(main_module._recover_trades)

check(
    "_recover_trades loads order ledger",
    "load_snapshot" in recover_source,
)
check(
    "_recover_trades polls orders after load",
    "poll_all" in recover_source,
)
check(
    "_recover_trades calls reconcile",
    "reconcile" in recover_source,
)


# =============================================================================
# Test 12: OrderManager persistence round-trip with LifecycleEngine
# =============================================================================

print("\n=== Test 12: OrderManager persistence round-trip ===")

# Create a temp directory for persistence
tmp_dir = tempfile.mkdtemp()
orig_logs_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")

from order_manager import OrderManager as OM, OrderPurpose as OP, LOGS_DIR

mock_ex = MockExecutor()
om_persist = OM(mock_ex)

rec = om_persist.place_order(
    lifecycle_id="persist-test",
    leg_index=0,
    purpose=OP.OPEN_LEG,
    symbol="BTCUSD-PERSIST-TEST",
    side=1,
    qty=0.5,
    price=100.0,
)

check("placement succeeded", rec is not None)

# Persist
om_persist.persist_snapshot()
snapshot_path = os.path.join(LOGS_DIR, "active_orders.json")
check("active_orders.json exists", os.path.exists(snapshot_path))

# Load into new instance
mock_ex2 = MockExecutor()
om_loaded = OM(mock_ex2)
om_loaded.load_snapshot()

loaded_orders = om_loaded.get_all_orders("persist-test")
check("loaded 1 order", len(loaded_orders) == 1)
if loaded_orders:
    lo = loaded_orders[0]
    check("loaded order has correct symbol", lo.symbol == "BTCUSD-PERSIST-TEST")
    check("loaded order has correct qty", lo.qty == 0.5)
    check("loaded order has correct price", lo.price == 100.0)
    check("loaded order has correct lifecycle_id", lo.lifecycle_id == "persist-test")
    check("loaded order has correct purpose", lo.purpose == OP.OPEN_LEG)


# =============================================================================
# Test 13: Mode auto-detection via ExecutionRouter
# =============================================================================

print("\n=== Test 13: Execution mode auto-detection ===")

# Single-leg → always "limit"
single_trade = TradeLifecycle(
    open_legs=[TradeLeg(symbol="BTCUSD-TEST-AUTO", qty=0.1, side=1)],
)
mode = router._determine_execution_mode(single_trade)
check("single leg → limit", mode == "limit")

# Multi-leg with no orderbook data → "limit" fallback (notional=0 < threshold)
multi_trade = TradeLifecycle(
    open_legs=[
        TradeLeg(symbol="BTCUSD-TEST-AUTO-1", qty=0.1, side=1),
        TradeLeg(symbol="BTCUSD-TEST-AUTO-2", qty=0.1, side=2),
    ],
)
mode2 = router._determine_execution_mode(multi_trade)
check(
    "multi-leg with 0 notional → limit fallback",
    mode2 == "limit",
    f"got mode={mode2}",
)


# =============================================================================
# Test 14: strategy.py imports still work (comprehensive)
# =============================================================================

print("\n=== Test 14: strategy.py import chain ===")

from strategy import (
    StrategyConfig, StrategyRunner, TradingContext,
    profit_target, max_loss, max_hold_hours,
    time_window, weekday_filter, min_available_margin_pct,
)

check("StrategyConfig importable", StrategyConfig is not None)
check("StrategyRunner importable", StrategyRunner is not None)
check("TradingContext importable", TradingContext is not None)
check("profit_target importable", profit_target is not None)
check("max_loss importable", max_loss is not None)
check("max_hold_hours importable", max_hold_hours is not None)


# =============================================================================
# Summary
# =============================================================================

print("\n" + "=" * 60)
print(f"Results: {passed} passed, {failed} failed")
print("=" * 60)

if failed:
    print(f"\n{failed} test(s) FAILED")
    sys.exit(1)
else:
    print("All tests passed!")
