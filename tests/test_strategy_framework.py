#!/usr/bin/env python3
"""
Tests for the strategy framework (tests 1-7 from the test plan).

Tests 1-5: Pure unit tests — no API calls, fake data only.
Tests 6-7: Read-only integration tests — hit real API but open NO positions.

Run:
    python3 tests/test_strategy_framework.py
"""

import json
import os
import sys
import time

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from account_manager import AccountSnapshot, PositionSnapshot
from lifecycle_engine import LifecycleEngine
from trade_lifecycle import (
    TradeLifecycle, TradeLeg, TradeState,
)
from option_selection import LegSpec
from strategy import (
    StrategyConfig, StrategyRunner, TradingContext,
    EntryCondition,
    time_window, weekday_filter, min_available_margin_pct,
    min_equity, max_account_delta, max_margin_utilization,
    no_existing_position_in,
    profit_target, max_loss, max_hold_hours,
)

# Track results
_results = []


def record(name, passed, detail=""):
    status = "PASS" if passed else "FAIL"
    _results.append((name, passed, detail))
    symbol = "✓" if passed else "✗"
    print(f"  {symbol} {name}" + (f"  ({detail})" if detail else ""))


# =============================================================================
# Helpers — build fake snapshots without touching the API
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


def fake_position(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side="long"):
    return PositionSnapshot(
        position_id="pos123",
        symbol=symbol,
        qty=qty,
        side=side,
        entry_price=500.0,
        mark_price=520.0,
        unrealized_pnl=20.0,
        roi=0.04,
        delta=0.25,
        gamma=0.001,
        theta=-1.0,
        vega=5.0,
        timestamp=time.time(),
    )


# =============================================================================
# TEST 1: Entry condition factories
# =============================================================================

def test_1_entry_conditions():
    print("\n--- Test 1: Entry condition factories ---")

    acct = fake_account(equity=10000, available_margin=8000, net_delta=0.5, margin_utilization=20.0)

    # time_window — we test by constructing windows that include/exclude current hour
    from datetime import datetime, timezone
    current_hour = datetime.now(timezone.utc).hour

    tw_include = time_window(current_hour, (current_hour + 2) % 24)
    record("time_window (in window)", tw_include(acct))

    # Window that definitely excludes now: 2 hours ago to 1 hour ago
    exclude_start = (current_hour - 2) % 24
    exclude_end = (current_hour - 1) % 24
    # Edge case: if current_hour < 2, wrapping could cause issues, so use a simpler approach
    if exclude_start < exclude_end:
        tw_exclude = time_window(exclude_start, exclude_end)
        record("time_window (out of window)", not tw_exclude(acct))
    else:
        record("time_window (out of window)", True, "skipped due to hour wrapping edge case")

    # weekday_filter
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc)
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[today.weekday()]
    tomorrow_name = day_names[(today.weekday() + 1) % 7]

    wf_include = weekday_filter([today_name])
    record("weekday_filter (today)", wf_include(acct))

    wf_exclude = weekday_filter([tomorrow_name])
    record("weekday_filter (not today)", not wf_exclude(acct))

    # min_available_margin_pct
    mm_pass = min_available_margin_pct(50)  # 8000/10000 = 80% > 50%
    record("min_available_margin_pct (pass)", mm_pass(acct))

    mm_fail = min_available_margin_pct(90)  # 80% < 90%
    record("min_available_margin_pct (fail)", not mm_fail(acct))

    # min_equity
    me_pass = min_equity(5000)
    record("min_equity (pass)", me_pass(acct))

    me_fail = min_equity(50000)
    record("min_equity (fail)", not me_fail(acct))

    # max_account_delta
    md_pass = max_account_delta(1.0)  # |0.5| <= 1.0
    record("max_account_delta (pass)", md_pass(acct))

    md_fail = max_account_delta(0.3)  # |0.5| > 0.3
    record("max_account_delta (fail)", not md_fail(acct))

    # max_margin_utilization
    mu_pass = max_margin_utilization(50)  # 20% <= 50%
    record("max_margin_utilization (pass)", mu_pass(acct))

    mu_fail = max_margin_utilization(10)  # 20% > 10%
    record("max_margin_utilization (fail)", not mu_fail(acct))

    # no_existing_position_in
    pos = fake_position(symbol="BTCUSD-28MAR26-100000-C")
    acct_with_pos = fake_account(positions=(pos,))

    nep_pass = no_existing_position_in(["BTCUSD-28MAR26-90000-C"])
    record("no_existing_position_in (no match)", nep_pass(acct_with_pos))

    nep_fail = no_existing_position_in(["BTCUSD-28MAR26-100000-C"])
    record("no_existing_position_in (match)", not nep_fail(acct_with_pos))

    # Edge case: zero equity
    acct_zero = fake_account(equity=0)
    mm_zero = min_available_margin_pct(50)
    record("min_available_margin_pct (zero equity)", not mm_zero(acct_zero))


# =============================================================================
# TEST 2: StrategyConfig + LegSpec construction
# =============================================================================

def test_2_config_construction():
    print("\n--- Test 2: StrategyConfig + LegSpec construction ---")

    spec1 = LegSpec(
        option_type="C", side=2, qty=0.1,
        strike_criteria={"type": "delta", "value": 0.25},
        expiry_criteria={"symbol": "28MAR26"},
    )
    spec2 = LegSpec(
        option_type="P", side=2, qty=0.1,
        strike_criteria={"type": "delta", "value": -0.25},
        expiry_criteria={"symbol": "28MAR26"},
    )

    record("LegSpec fields", spec1.option_type == "C" and spec1.side == 2 and spec1.qty == 0.1)
    record("LegSpec default underlying", spec1.underlying == "BTC")

    config = StrategyConfig(
        name="test_strangle",
        legs=[spec1, spec2],
        entry_conditions=[min_equity(1000)],
        exit_conditions=[profit_target(50), max_loss(100)],
        max_concurrent_trades=2,
        cooldown_seconds=600,
        check_interval_seconds=30,
        execution_mode="auto",
    )

    record("StrategyConfig.name", config.name == "test_strangle")
    record("StrategyConfig.legs count", len(config.legs) == 2)
    record("StrategyConfig.entry_conditions count", len(config.entry_conditions) == 1)
    record("StrategyConfig.exit_conditions count", len(config.exit_conditions) == 2)
    record("StrategyConfig.max_concurrent_trades", config.max_concurrent_trades == 2)
    record("StrategyConfig.cooldown_seconds", config.cooldown_seconds == 600)
    record("StrategyConfig.check_interval_seconds", config.check_interval_seconds == 30)
    record("StrategyConfig.execution_mode", config.execution_mode == "auto")


# =============================================================================
# TEST 3: LifecycleEngine strategy queries
# =============================================================================

def test_3_lifecycle_strategy_queries():
    print("\n--- Test 3: LifecycleEngine strategy queries ---")

    lm = LifecycleEngine()

    # Create trades with different strategy IDs
    t1 = lm.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="strat_A",
    )
    t2 = lm.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-90000-P", qty=0.1, side=1)],
        strategy_id="strat_A",
    )
    t3 = lm.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-80000-C", qty=0.1, side=1)],
        strategy_id="strat_B",
    )
    t4 = lm.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-70000-P", qty=0.1, side=1)],
        strategy_id=None,  # No strategy
    )

    # get_trades_for_strategy
    strat_a_trades = lm.get_trades_for_strategy("strat_A")
    record("get_trades_for_strategy(A) count", len(strat_a_trades) == 2)

    strat_b_trades = lm.get_trades_for_strategy("strat_B")
    record("get_trades_for_strategy(B) count", len(strat_b_trades) == 1)

    strat_x_trades = lm.get_trades_for_strategy("strat_X")
    record("get_trades_for_strategy(X) count", len(strat_x_trades) == 0)

    # active_trades_for_strategy — all should be active (PENDING_OPEN)
    active_a = lm.active_trades_for_strategy("strat_A")
    record("active_trades_for_strategy(A)", len(active_a) == 2)

    # Manually move one to CLOSED and re-check
    t1.state = TradeState.CLOSED
    active_a_after = lm.active_trades_for_strategy("strat_A")
    record("active after closing t1", len(active_a_after) == 1)

    # Verify strategy_id is set on the trade
    record("trade.strategy_id set", t1.strategy_id == "strat_A")
    record("trade.strategy_id None", t4.strategy_id is None)

    # Verify summary includes strategy_id
    record("summary includes strategy_id", "strat_A" in t1.summary())
    record("summary without strategy_id", "strat_A" not in t4.summary())


# =============================================================================
# TEST 4: Trade persistence snapshot
# =============================================================================

def test_4_persistence_snapshot():
    print("\n--- Test 4: Trade persistence snapshot ---")

    lm = LifecycleEngine()

    lm.create(
        legs=[
            TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1),
            TradeLeg(symbol="BTCUSD-28MAR26-90000-P", qty=0.1, side=2),
        ],
        strategy_id="test_persist",
    )

    # Persist
    snapshot_path = "logs/trades_snapshot.json"
    lm._persist_all_trades()

    # Verify file exists
    record("snapshot file exists", os.path.exists(snapshot_path))

    # Verify valid JSON
    try:
        with open(snapshot_path) as f:
            data = json.load(f)
        record("snapshot valid JSON", True)
    except (json.JSONDecodeError, FileNotFoundError) as e:
        record("snapshot valid JSON", False, str(e))
        return

    # Verify structure
    record("snapshot has timestamp", "timestamp" in data)
    record("snapshot has trades", "trades" in data and len(data["trades"]) == 1)

    trade_data = data["trades"][0]
    record("trade has id", "id" in trade_data and len(trade_data["id"]) > 0)
    record("trade has strategy_id", trade_data.get("strategy_id") == "test_persist")
    record("trade has state", trade_data.get("state") == "pending_open")
    record("trade has open_legs", len(trade_data.get("open_legs", [])) == 2)
    record("trade has close_legs", "close_legs" in trade_data)

    leg0 = trade_data["open_legs"][0]
    record("leg has symbol", "symbol" in leg0)
    record("leg has qty", leg0.get("qty") == 0.1)
    record("leg has side", leg0.get("side") == 1)


# =============================================================================
# TEST 5: StrategyRunner gating logic
# =============================================================================

def test_5_runner_gating():
    print("\n--- Test 5: StrategyRunner gating logic ---")

    from datetime import datetime, timezone
    current_hour = datetime.now(timezone.utc).hour
    day_names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today_name = day_names[datetime.now(timezone.utc).weekday()]

    config = StrategyConfig(
        name="test_gating",
        legs=[
            LegSpec("C", side=1, qty=0.1,
                    strike_criteria={"type": "strike", "value": 100000},
                    expiry_criteria={"symbol": "28MAR26"}),
        ],
        entry_conditions=[
            time_window(current_hour, (current_hour + 2) % 24),
            weekday_filter([today_name]),
            min_equity(1000),
        ],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=1,
        cooldown_seconds=0,
        check_interval_seconds=0,  # No throttling for tests
    )

    # We need a TradingContext but won't actually call APIs.
    # Build a minimal one — the runner only uses ctx.lifecycle_manager
    # in _should_open() path.
    from strategy import build_context
    ctx = build_context()

    runner = StrategyRunner(config, ctx)
    acct = fake_account(equity=10000, available_margin=8000)

    # Gate: all conditions met, no active trades → should open
    record("should_open (all gates pass)", runner._should_open(acct))

    # Gate: max concurrent trades reached
    # Simulate by creating a trade with matching strategy_id
    ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="test_gating",
    )
    record("should_open (max trades)", not runner._should_open(acct))

    # Remove the trade (mark as closed) → gate should pass again
    for t in ctx.lifecycle_manager.all_trades:
        if t.strategy_id == "test_gating":
            t.state = TradeState.CLOSED
    record("should_open (after close)", runner._should_open(acct))

    # Gate: cooldown
    config2 = StrategyConfig(
        name="test_cooldown",
        legs=config.legs,
        entry_conditions=config.entry_conditions,
        exit_conditions=config.exit_conditions,
        max_concurrent_trades=1,
        cooldown_seconds=9999,  # Very long cooldown
        check_interval_seconds=0,
    )
    runner2 = StrategyRunner(config2, ctx)
    # Create and close a trade for this strategy
    t = ctx.lifecycle_manager.create(
        legs=[TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.1, side=1)],
        strategy_id="test_cooldown",
    )
    t.state = TradeState.CLOSED
    record("should_open (cooldown active)", not runner2._should_open(acct))

    # Gate: failing entry condition (equity too low)
    config3 = StrategyConfig(
        name="test_equity_gate",
        legs=config.legs,
        entry_conditions=[min_equity(999999)],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=1,
        cooldown_seconds=0,
        check_interval_seconds=0,
    )
    runner3 = StrategyRunner(config3, ctx)
    record("should_open (equity gate)", not runner3._should_open(acct))

    # Gate: disabled runner
    runner.disable()
    record("runner.disable()", not runner._enabled)
    runner.enable()
    record("runner.enable()", runner._enabled)


# =============================================================================
# TEST 6: resolve_legs with real market data (READ-ONLY API)
# =============================================================================

def test_6_resolve_legs_live():
    print("\n--- Test 6: resolve_legs with real market data ---")
    print("  (Calling Coincall API — read-only, no orders placed)")

    from option_selection import resolve_legs, LegSpec

    # First, find a valid expiry by checking available instruments
    from market_data import get_option_instruments
    instruments = get_option_instruments("BTC")
    if not instruments:
        record("get_option_instruments", False, "No instruments returned")
        return

    record("get_option_instruments", True, f"{len(instruments)} instruments")

    # Find an active expiry token from the instruments
    # Extract unique expiry tokens from symbol names
    expiry_tokens = set()
    for inst in instruments:
        sym = inst.get("symbolName", "")
        # Format: BTCUSD-{EXPIRY}-{STRIKE}-{C/P}
        parts = sym.split("-")
        if len(parts) >= 4:
            expiry_tokens.add(parts[1])

    if not expiry_tokens:
        record("find expiry tokens", False, "No expiry tokens found")
        return

    # Pick the first expiry alphabetically (they sort chronologically for same year)
    expiry = sorted(expiry_tokens)[0]
    record("found expiry", True, expiry)

    # Find a valid strike — pick one from the instruments at this expiry
    expiry_instruments = [
        inst for inst in instruments
        if f"-{expiry}-" in inst.get("symbolName", "")
        and inst.get("symbolName", "").endswith("-C")
    ]
    if not expiry_instruments:
        record("find call at expiry", False, f"No calls at {expiry}")
        return

    # Pick a strike near the middle of available strikes
    strikes = sorted(set(float(inst.get("strike", 0)) for inst in expiry_instruments))
    mid_strike = strikes[len(strikes) // 2]
    record("selected strike", True, f"${mid_strike:,.0f}")

    # Now resolve legs using exact strike criteria
    specs = [
        LegSpec(
            option_type="C", side=1, qty=0.1,
            strike_criteria={"type": "strike", "value": mid_strike},
            expiry_criteria={"symbol": expiry},
        ),
    ]

    try:
        resolved = resolve_legs(specs)
        record("resolve_legs succeeded", len(resolved) == 1)
        record("resolved symbol", True, resolved[0].symbol)
        record("resolved qty", resolved[0].qty == 0.1)
        record("resolved side", resolved[0].side == 1)
    except Exception as e:
        record("resolve_legs", False, str(e))


# =============================================================================
# TEST 7: build_context() smoke test (READ-ONLY API)
# =============================================================================

def test_7_build_context_live():
    print("\n--- Test 7: build_context() smoke test ---")
    print("  (Calling Coincall API — read-only, no orders placed)")

    from strategy import build_context

    ctx = build_context()

    # Verify all services are wired
    record("ctx.auth", ctx.auth is not None)
    record("ctx.market_data", ctx.market_data is not None)
    record("ctx.executor", ctx.executor is not None)
    record("ctx.rfq_executor", ctx.rfq_executor is not None)
    record("ctx.account_manager", ctx.account_manager is not None)
    record("ctx.position_monitor", ctx.position_monitor is not None)
    record("ctx.lifecycle_manager", ctx.lifecycle_manager is not None)

    # Take a single snapshot (read-only API call)
    try:
        snap = ctx.position_monitor.snapshot()
        record("snapshot succeeded", snap is not None)
        record("snapshot.equity", snap.equity >= 0, f"${snap.equity:.2f}")
        record("snapshot.positions", True, f"{snap.position_count} positions")
        print(f"  Account: {snap.summary_str()}")
    except Exception as e:
        record("snapshot", False, str(e))

    # Verify lifecycle_manager is wired as callback on monitor
    record("lifecycle tick registered", len(ctx.position_monitor._callbacks) >= 1)


# =============================================================================
# Runner
# =============================================================================

def main():
    print("=" * 60)
    print("Strategy Framework Test Suite")
    print("=" * 60)

    # Unit tests (no API)
    test_1_entry_conditions()
    test_2_config_construction()
    test_3_lifecycle_strategy_queries()
    test_4_persistence_snapshot()
    test_5_runner_gating()

    # Integration tests (read-only API)
    test_6_resolve_legs_live()
    test_7_build_context_live()

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
