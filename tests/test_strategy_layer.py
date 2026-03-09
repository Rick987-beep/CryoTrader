#!/usr/bin/env python3
"""
Tests for the strategy layer additions:

  - DTE-based expiry selection
  - time_exit() exit condition
  - max_trades_per_day gate
  - Structure templates (straddle, strangle)
  - on_trade_closed callback
  - StrategyRunner.stats property

Tests 1-7: Pure unit tests — no API calls.
Test 8: Read-only integration test — hits live API, no orders.

Run:
    python3 tests/test_strategy_layer.py
"""

import os
import sys
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from account_manager import AccountSnapshot, PositionSnapshot
from lifecycle_engine import LifecycleEngine
from trade_lifecycle import (
    TradeLifecycle, TradeLeg, TradeState,
)
from option_selection import LegSpec, straddle, strangle
from strategy import (
    StrategyConfig, StrategyRunner, TradingContext,
    time_window, weekday_filter, min_available_margin_pct, min_equity,
    profit_target, max_loss, max_hold_hours, time_exit,
)

# Track results
_results = []


def record(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    symbol = "✓" if passed else "✗"
    print(f"  {symbol} {name}" + (f"  ({detail})" if detail else ""))


# =============================================================================
# Helpers
# =============================================================================

def fake_account(
    equity=10000.0,
    available_margin=8000.0,
    initial_margin=2000.0,
    net_delta=0.5,
    margin_utilization=20.0,
    positions=(),
) -> AccountSnapshot:
    return AccountSnapshot(
        equity=equity,
        available_margin=available_margin,
        initial_margin=initial_margin,
        maintenance_margin=1000.0,
        unrealized_pnl=100.0,
        margin_utilization=margin_utilization,
        positions=positions,
        net_delta=net_delta,
        net_gamma=0.01,
        net_theta=-0.5,
        net_vega=0.1,
        timestamp=time.time(),
    )


def fake_position(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side="long",
                   unrealized_pnl=20.0):
    return PositionSnapshot(
        position_id="pos123",
        symbol=symbol,
        qty=qty,
        side=side,
        entry_price=500.0,
        mark_price=520.0,
        unrealized_pnl=unrealized_pnl,
        roi=0.04,
        delta=0.25,
        gamma=0.001,
        theta=-1.0,
        vega=5.0,
        timestamp=time.time(),
    )


# =============================================================================
# TEST 1: time_exit() exit condition
# =============================================================================

def test_1_time_exit():
    print("\n--- Test 1: time_exit() exit condition ---")

    acct = fake_account()

    # Create a fake trade
    trade = TradeLifecycle(
        open_legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        state=TradeState.OPEN,
    )
    trade.opened_at = time.time() - 3600  # opened 1h ago

    now = datetime.now(timezone.utc)

    # time_exit in the past → should trigger
    past_hour = (now.hour - 1) % 24
    te_past = time_exit(past_hour, 0)
    record("time_exit (past hour triggers)", te_past(acct, trade))

    # time_exit in the future → should NOT trigger
    future_hour = (now.hour + 1) % 24
    te_future = time_exit(future_hour, 0)
    record("time_exit (future hour blocked)", not te_future(acct, trade))

    # time_exit at exactly current hour, minute 0 → should trigger if minute > 0
    # (or at minute=0 exactly — which is "at or after")
    te_now = time_exit(now.hour, 0)
    record("time_exit (current hour triggers)", te_now(acct, trade))

    # Verify __name__ is set
    te = time_exit(19, 30)
    record("time_exit __name__", "19:30" in getattr(te, "__name__", ""))


# =============================================================================
# TEST 2: straddle() structure template
# =============================================================================

def test_2_straddle_template():
    print("\n--- Test 2: straddle() structure template ---")

    legs = straddle(qty=0.5, dte=0, side=1)
    record("straddle returns 2 legs", len(legs) == 2)
    record("straddle leg 0 is call", legs[0].option_type == "C")
    record("straddle leg 1 is put", legs[1].option_type == "P")
    record("straddle leg 0 side=buy", legs[0].side == 1)
    record("straddle leg 1 side=buy", legs[1].side == 1)
    record("straddle leg qty", legs[0].qty == 0.5 and legs[1].qty == 0.5)
    record("straddle ATM strike", legs[0].strike_criteria == {"type": "closestStrike", "value": 0})
    record("straddle DTE expiry", legs[0].expiry_criteria == {"dte": 0})
    record("straddle underlying", legs[0].underlying == "BTC")

    # Sell straddle
    sell_legs = straddle(qty=0.1, dte=2, side=2)
    record("sell straddle side", sell_legs[0].side == 2)
    record("sell straddle dte=2", sell_legs[0].expiry_criteria == {"dte": 2})


# =============================================================================
# TEST 3: strangle() structure template
# =============================================================================

def test_3_strangle_template():
    print("\n--- Test 3: strangle() structure template ---")

    legs = strangle(qty=0.2, call_delta=0.30, put_delta=-0.30, dte=1, side=2)
    record("strangle returns 2 legs", len(legs) == 2)
    record("strangle leg 0 is call", legs[0].option_type == "C")
    record("strangle leg 1 is put", legs[1].option_type == "P")
    record("strangle leg 0 side=sell", legs[0].side == 2)
    record("strangle call delta", legs[0].strike_criteria == {"type": "delta", "value": 0.30})
    record("strangle put delta", legs[1].strike_criteria == {"type": "delta", "value": -0.30})
    record("strangle DTE", legs[0].expiry_criteria == {"dte": 1})

    # Default values
    default_legs = strangle(qty=0.1)
    record("strangle default side=sell", default_legs[0].side == 2)
    record("strangle default call_delta", default_legs[0].strike_criteria["value"] == 0.25)
    record("strangle default put_delta", default_legs[1].strike_criteria["value"] == -0.25)
    record("strangle default dte=0", default_legs[0].expiry_criteria == {"dte": 0})


# =============================================================================
# TEST 4: DTE-based expiry filtering
# =============================================================================

def test_4_dte_expiry_filter():
    print("\n--- Test 4: DTE-based expiry filtering ---")

    from option_selection import _filter_by_expiry, _utc_day_start_ms

    # Verify _utc_day_start_ms
    day_start = _utc_day_start_ms()
    now_ms = int(time.time() * 1000)
    record("_utc_day_start_ms is today", day_start <= now_ms)
    record("_utc_day_start_ms < now", now_ms - day_start < 86400_000)

    # Build fake option instruments with known expiry timestamps
    today_start = _utc_day_start_ms()
    today_expiry = today_start + 16 * 3600_000  # 16:00 UTC today (typical expiry)
    tomorrow_expiry = today_start + 86400_000 + 16 * 3600_000
    next_week_expiry = today_start + 7 * 86400_000 + 16 * 3600_000

    fake_instruments = [
        {"symbolName": "BTCUSD-TODAY-90000-C", "strike": 90000, "expirationTimestamp": today_expiry},
        {"symbolName": "BTCUSD-TODAY-100000-C", "strike": 100000, "expirationTimestamp": today_expiry},
        {"symbolName": "BTCUSD-TOMORROW-90000-C", "strike": 90000, "expirationTimestamp": tomorrow_expiry},
        {"symbolName": "BTCUSD-TOMORROW-100000-C", "strike": 100000, "expirationTimestamp": tomorrow_expiry},
        {"symbolName": "BTCUSD-NEXTWEEK-90000-C", "strike": 90000, "expirationTimestamp": next_week_expiry},
        {"symbolName": "BTCUSD-TODAY-90000-P", "strike": 90000, "expirationTimestamp": today_expiry},
    ]

    # DTE=0 should return today's calls
    now_utc = datetime.now(timezone.utc)
    # Only test if we're before 16:00 UTC (otherwise today's options have expired)
    if now_utc.hour < 16:
        result = _filter_by_expiry(fake_instruments, {"dte": 0}, "C")
        record("dte=0 returns today's calls", len(result) == 2, f"got {len(result)}")
        record("dte=0 correct expiry", all(r["expirationTimestamp"] == today_expiry for r in result))
    else:
        record("dte=0 (skipped, past 16:00 UTC)", True, "today's options expired")

    # DTE=1 should return tomorrow's calls
    result_1 = _filter_by_expiry(fake_instruments, {"dte": 1}, "C")
    record("dte=1 returns tomorrow's calls", len(result_1) == 2, f"got {len(result_1)}")

    # DTE=7 should return next week
    result_7 = _filter_by_expiry(fake_instruments, {"dte": 7}, "C")
    record("dte=7 returns next week", len(result_7) == 1, f"got {len(result_7)}")

    # DTE with put filter
    if now_utc.hour < 16:
        result_p = _filter_by_expiry(fake_instruments, {"dte": 0}, "P")
        record("dte=0 put filter", len(result_p) == 1)
    else:
        record("dte=0 put filter (skipped)", True, "past 16:00 UTC")

    # DTE with no matching expiry
    result_none = _filter_by_expiry(fake_instruments, {"dte": 30}, "C")
    record("dte=30 no match", len(result_none) == 0)


# =============================================================================
# TEST 5: max_trades_per_day gate
# =============================================================================

def test_5_max_trades_per_day():
    print("\n--- Test 5: max_trades_per_day gate ---")

    current_hour = datetime.now(timezone.utc).hour
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[datetime.now(timezone.utc).weekday()]

    config = StrategyConfig(
        name="test_daily_limit",
        legs=[
            LegSpec("C", side=1, qty=0.1,
                    strike_criteria={"type": "strike", "value": 100000},
                    expiry_criteria={"symbol": "28MAR26"}),
        ],
        entry_conditions=[
            time_window(current_hour, (current_hour + 2) % 24),
            weekday_filter([today_name]),
        ],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=5,        # High limit — not the blocker
        max_trades_per_day=1,            # THIS is the gate we're testing
        cooldown_seconds=0,
        check_interval_seconds=0,
    )

    from strategy import build_context
    ctx = build_context()
    runner = StrategyRunner(config, ctx)
    acct = fake_account()

    # No trades today → should open
    record("should_open (no trades today)", runner._should_open(acct))

    # Create a trade for today → should block
    t = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="test_daily_limit",
    )
    t.state = TradeState.CLOSED  # Close it so max_concurrent isn't the blocker
    record("should_open (1 trade today, limit=1)", not runner._should_open(acct))

    # Config with max_trades_per_day=0 (unlimited)
    config2 = StrategyConfig(
        name="test_unlimited",
        legs=config.legs,
        entry_conditions=config.entry_conditions,
        exit_conditions=config.exit_conditions,
        max_concurrent_trades=5,
        max_trades_per_day=0,  # Unlimited
        cooldown_seconds=0,
        check_interval_seconds=0,
    )
    runner2 = StrategyRunner(config2, ctx)
    record("should_open (unlimited)", runner2._should_open(acct))


# =============================================================================
# TEST 6: on_trade_closed callback
# =============================================================================

def test_6_on_trade_closed():
    print("\n--- Test 6: on_trade_closed callback ---")

    closed_trades = []

    def track_closed(trade, account):
        closed_trades.append(trade.id)

    current_hour = datetime.now(timezone.utc).hour
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[datetime.now(timezone.utc).weekday()]

    config = StrategyConfig(
        name="test_callback",
        legs=[
            LegSpec("C", side=1, qty=0.1,
                    strike_criteria={"type": "strike", "value": 100000},
                    expiry_criteria={"symbol": "28MAR26"}),
        ],
        entry_conditions=[
            time_window(current_hour, (current_hour + 2) % 24),
            weekday_filter([today_name]),
        ],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=5,
        max_trades_per_day=0,
        cooldown_seconds=0,
        check_interval_seconds=0,
        on_trade_closed=track_closed,
    )

    from strategy import build_context
    ctx = build_context()
    runner = StrategyRunner(config, ctx)
    acct = fake_account()

    # Create a trade and close it
    t = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="test_callback",
    )

    # Before closing: callback should not fire
    runner._check_closed_trades(acct)
    record("callback not fired (trade open)", len(closed_trades) == 0)

    # Close the trade
    t.state = TradeState.CLOSED
    runner._check_closed_trades(acct)
    record("callback fired on close", len(closed_trades) == 1)
    record("callback received correct trade", closed_trades[0] == t.id)

    # Repeat tick — should NOT fire again (idempotent)
    runner._check_closed_trades(acct)
    record("callback idempotent", len(closed_trades) == 1)

    # FAILED trade also fires callback
    t2 = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-90000-P", qty=0.1, side=1)],
        strategy_id="test_callback",
    )
    t2.state = TradeState.FAILED
    runner._check_closed_trades(acct)
    record("callback on FAILED trade", len(closed_trades) == 2)


# =============================================================================
# TEST 7: StrategyRunner.stats property
# =============================================================================

def test_7_stats():
    print("\n--- Test 7: StrategyRunner.stats property ---")

    current_hour = datetime.now(timezone.utc).hour
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[datetime.now(timezone.utc).weekday()]

    config = StrategyConfig(
        name="test_stats",
        legs=[
            LegSpec("C", side=1, qty=0.1,
                    strike_criteria={"type": "strike", "value": 100000},
                    expiry_criteria={"symbol": "28MAR26"}),
        ],
        entry_conditions=[
            time_window(current_hour, (current_hour + 2) % 24),
            weekday_filter([today_name]),
        ],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=5,
        cooldown_seconds=0,
        check_interval_seconds=0,
    )

    from strategy import build_context
    ctx = build_context()
    runner = StrategyRunner(config, ctx)

    # No trades → default stats
    s0 = runner.stats
    record("stats.total (empty)", s0["total"] == 0)
    record("stats.today_trades (empty)", s0["today_trades"] == 0)

    # Create and close trades
    t1 = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="test_stats",
    )
    t1.state = TradeState.CLOSED
    t1.opened_at = time.time() - 3600
    t1.closed_at = time.time()

    t2 = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-90000-P", qty=0.1, side=1)],
        strategy_id="test_stats",
    )
    t2.state = TradeState.CLOSED
    t2.opened_at = time.time() - 7200
    t2.closed_at = time.time()

    s1 = runner.stats
    record("stats.total (2 closed)", s1["total"] == 2)
    record("stats.today_trades", s1["today_trades"] == 2)
    record("stats.avg_hold_seconds > 0", s1["avg_hold_seconds"] > 0)

    # FAILED trades NOT counted in closed stats
    t3 = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-80000-C", qty=0.1, side=1)],
        strategy_id="test_stats",
    )
    t3.state = TradeState.FAILED
    s2 = runner.stats
    record("stats.total (FAILED excluded)", s2["total"] == 2)


# =============================================================================
# TEST 8: DTE expiry with live market data (READ-ONLY)
# =============================================================================

def test_8_dte_live():
    print("\n--- Test 8: DTE expiry with live market data ---")
    print("  (Calling Coincall API — read-only, no orders placed)")

    from option_selection import _filter_by_expiry
    from market_data import get_option_instruments

    instruments = get_option_instruments("BTC")
    if not instruments:
        record("get_option_instruments", False, "No instruments returned")
        return

    record("instruments loaded", True, f"{len(instruments)} total")

    # Find 0DTE options (if any exist today)
    calls_0dte = _filter_by_expiry(instruments, {"dte": 0}, "C")
    if calls_0dte:
        record("0DTE calls found", True, f"{len(calls_0dte)} options")
        # All should share the same expiry timestamp
        expiries = set(opt.get("expirationTimestamp") for opt in calls_0dte)
        record("0DTE single expiry date", len(expiries) == 1)
        # Log one sample
        sample = calls_0dte[0]["symbolName"]
        record("0DTE sample", True, sample)
    else:
        record("0DTE calls", True, "none available today (weekend/holiday?)")

    # Find options expiring within 7 days
    calls_week = _filter_by_expiry(instruments, {"dte": 3, "dte_min": 0, "dte_max": 7}, "C")
    if calls_week:
        record("0-7 DTE calls found", True, f"{len(calls_week)} options")
    else:
        record("0-7 DTE calls", True, "none available in 7-day window")


# =============================================================================
# Runner
# =============================================================================

def main():
    print("=" * 60)
    print("Strategy Layer Test Suite")
    print("=" * 60)

    # Unit tests (no API)
    test_1_time_exit()
    test_2_straddle_template()
    test_3_strangle_template()
    test_4_dte_expiry_filter()
    test_5_max_trades_per_day()
    test_6_on_trade_closed()
    test_7_stats()

    # Integration test (read-only API)
    test_8_dte_live()

    # Summary
    print("\n" + "=" * 60)
    passed = sum(1 for _, p, _ in _results if p)
    failed = sum(1 for _, p, _ in _results if not p)
    total = len(_results)
    print(f"Results: {passed}/{total} passed, {failed} failed")

    if failed > 0:
        print("\nFailed tests:")
        for name, p, detail in _results:
            if not p:
                print(f"  ✗ {name}  ({detail})")

    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
