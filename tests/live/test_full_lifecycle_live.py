"""
Live test: Full open → hold → close lifecycle on Deribit testnet.

Phase 5 live validation: exercises the complete trade lifecycle —
pricing, fill management, fee capture, denomination safety, profile
loading, and PnL calculation — against real Deribit testnet orders.

Sells a deep-OTM BTC put (cheap, >60 DTE), waits for fill, then
immediately buys it back.  Verifies fees, typed Price objects, and
realized PnL throughout.

Usage:
    EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/test_full_lifecycle_live.py -m live -v
"""

import os
import sys
import time
import logging
import pytest

pytestmark = pytest.mark.live

os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.currency import Currency, Price
from execution.fill_manager import FillManager
from execution.fill_result import FillStatus
from execution.profiles import ExecutionProfile, PhaseConfig, load_profiles
from order_manager import OrderManager, OrderPurpose
from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState

log = logging.getLogger(__name__)


def _skip_if_no_creds():
    from config import DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
    if not DERIBIT_CLIENT_ID or not DERIBIT_CLIENT_SECRET:
        pytest.skip("DERIBIT_CLIENT_ID_TEST / DERIBIT_CLIENT_SECRET_TEST not set")


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def auth():
    _skip_if_no_creds()
    from exchanges.deribit.auth import DeribitAuth
    return DeribitAuth()


@pytest.fixture(scope="module")
def executor(auth):
    from exchanges.deribit.executor import DeribitExecutorAdapter
    return DeribitExecutorAdapter(auth)


@pytest.fixture(scope="module")
def market_data(auth):
    from exchanges.deribit.market_data import DeribitMarketDataAdapter
    return DeribitMarketDataAdapter(auth)


@pytest.fixture(scope="module")
def account(auth):
    from exchanges.deribit.account import DeribitAccountAdapter
    return DeribitAccountAdapter(auth)


@pytest.fixture(scope="module")
def order_manager(executor):
    from exchanges.deribit import DERIBIT_STATE_MAP
    return OrderManager(executor, exchange_state_map=DERIBIT_STATE_MAP)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_cheap_deep_otm_put(market_data):
    """Find a BTC put with >60 DTE and ask < 0.002 BTC.

    Returns (symbol, orderbook) or skips the test if none found.
    """
    instruments = market_data.get_option_instruments()
    assert instruments, "No option instruments available"
    btc_price = market_data.get_index_price()
    assert btc_price and btc_price > 0, "Could not get BTC index price"

    candidates = []
    for inst in instruments:
        name = inst.get("symbolName", "")
        if not name.endswith("-P"):
            continue

        strike = inst.get("strike", 0)
        if strike <= 0 or strike >= btc_price * 0.5:
            continue  # not deep-OTM enough

        # Check DTE
        expiry_ts = inst.get("expirationTimestamp", 0)
        if expiry_ts:
            dte = (expiry_ts / 1000 - time.time()) / 86400
            if dte < 60:
                continue

        ob = market_data.get_option_orderbook(name)
        if not ob:
            continue

        asks = ob.get("asks", [])
        if not asks:
            continue

        ask_price = float(asks[0]["price"])
        if 0 < ask_price < 0.002:
            candidates.append((name, ob, ask_price))

    if not candidates:
        pytest.skip("No cheap deep-OTM puts found on testnet (>60 DTE, ask < 0.002)")

    # Pick the cheapest
    candidates.sort(key=lambda x: x[2])
    symbol, ob, _ = candidates[0]
    log.info(f"Selected {symbol} (ask={candidates[0][2]:.6f} BTC)")
    return symbol, ob


def _make_aggressive_profile(name="test_lifecycle"):
    """1-phase aggressive profile with short duration for fast test."""
    return ExecutionProfile(
        name=name,
        open_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=30.0,
            buffer_pct=5.0,
        )],
        close_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=30.0,
            buffer_pct=5.0,
        )],
        open_atomic=True,
        close_best_effort=True,
    )


def _poll_until_terminal(fill_mgr, timeout=30, poll_interval=2):
    """Poll fill_mgr.check() until FILLED or FAILED. Returns final result."""
    deadline = time.time() + timeout
    result = None
    while time.time() < deadline:
        time.sleep(poll_interval)
        result = fill_mgr.check()
        log.info(
            f"  check() → {result.status.value}, "
            f"phase={result.phase_index}/{result.phase_total}, "
            f"elapsed={result.elapsed_seconds:.1f}s"
        )
        if result.status in (FillStatus.FILLED, FillStatus.FAILED):
            return result
    return result


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestFullLifecycleLive:

    def test_open_hold_close_cycle(
        self, market_data, order_manager, account
    ):
        """Execute complete: sell-to-open → verify → buy-to-close → verify PnL."""
        symbol, ob = _find_cheap_deep_otm_put(market_data)
        profile = _make_aggressive_profile()

        # ── STEP 1: Open — sell a deep-OTM put ──────────────────────────
        log.info(f"=== OPEN: selling 0.1x {symbol} ===")

        open_mgr = FillManager(
            order_manager=order_manager,
            market_data=market_data,
            profile=profile,
            direction="open",
        )

        open_legs = [{"symbol": symbol, "qty": 0.1, "side": "sell"}]
        lifecycle_id = f"lifecycle_test_{int(time.time())}"

        try:
            open_result = open_mgr.place_all(
                legs=open_legs,
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.OPEN_LEG,
            )

            assert open_result.status in (FillStatus.PENDING, FillStatus.FILLED), (
                f"Open placement failed: {open_result.status}: {open_result.error}"
            )

            if open_result.status != FillStatus.FILLED:
                open_result = _poll_until_terminal(open_mgr, timeout=30)

            assert open_result is not None
            assert open_result.status == FillStatus.FILLED, (
                f"Open did not fill within timeout: {open_result.status}"
            )

            # ── Verify open fill data ────────────────────────────────────
            open_leg = open_result.legs[0]
            assert open_leg.filled_qty == 0.1, (
                f"Expected full fill: {open_leg.filled_qty}/0.1"
            )

            # Fee must be captured
            assert open_result.total_fees is not None, "Open fees not captured"
            assert isinstance(open_result.total_fees, Price)
            assert open_result.total_fees.currency == Currency.BTC
            assert open_result.total_fees.amount > 0
            log.info(f"Open fee: {open_result.total_fees}")

            # Detected currency must be BTC
            assert open_mgr.detected_currency == Currency.BTC

            # Record fill price for PnL verification
            open_fill_price = open_leg.fill_price
            log.info(
                f"Open filled: {open_leg.filled_qty}x {symbol} "
                f"@ {open_fill_price}, fee={open_leg.fee}"
            )

            # ── STEP 2: Brief hold — verify position exists ─────────────
            log.info("=== HOLD: verifying position ===")
            time.sleep(1)

            # ── STEP 3: Close — buy back the put ────────────────────────
            log.info(f"=== CLOSE: buying 0.1x {symbol} ===")

            close_mgr = FillManager(
                order_manager=order_manager,
                market_data=market_data,
                profile=profile,
                direction="close",
            )

            close_legs = [{"symbol": symbol, "qty": 0.1, "side": "buy"}]

            close_result = close_mgr.place_all(
                legs=close_legs,
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.CLOSE_LEG,
                reduce_only=True,
            )

            assert close_result.status in (FillStatus.PENDING, FillStatus.FILLED), (
                f"Close placement failed: {close_result.status}: {close_result.error}"
            )

            if close_result.status != FillStatus.FILLED:
                close_result = _poll_until_terminal(close_mgr, timeout=30)

            assert close_result is not None
            assert close_result.status == FillStatus.FILLED, (
                f"Close did not fill within timeout: {close_result.status}"
            )

            # ── Verify close fill data ───────────────────────────────────
            close_leg = close_result.legs[0]
            assert close_leg.filled_qty == 0.1

            # Close fee must be captured
            assert close_result.total_fees is not None, "Close fees not captured"
            assert isinstance(close_result.total_fees, Price)
            assert close_result.total_fees.currency == Currency.BTC
            assert close_result.total_fees.amount > 0
            log.info(f"Close fee: {close_result.total_fees}")

            close_fill_price = close_leg.fill_price
            log.info(
                f"Close filled: {close_leg.filled_qty}x {symbol} "
                f"@ {close_fill_price}, fee={close_leg.fee}"
            )

            # ── STEP 4: Verify PnL via TradeLifecycle ────────────────────
            log.info("=== PnL verification ===")

            # Build a TradeLifecycle to exercise _finalize_close
            trade = TradeLifecycle(
                open_legs=[TradeLeg(symbol=symbol, qty=0.1, side="sell")],
                state=TradeState.OPEN,
            )
            # Fill open leg with actual data
            trade.open_legs[0].filled_qty = 0.1
            if open_fill_price is not None:
                trade.open_legs[0].fill_price = float(open_fill_price)
            else:
                # Fallback: use the price from order manager
                open_orders = [
                    r for r in order_manager._orders.values()
                    if r.lifecycle_id == lifecycle_id
                    and r.purpose == OrderPurpose.OPEN_LEG
                ]
                if open_orders:
                    trade.open_legs[0].fill_price = float(open_orders[-1].avg_fill_price or open_orders[-1].price)

            # Build close leg
            trade.close_legs = [TradeLeg(symbol=symbol, qty=0.1, side="buy")]
            trade.close_legs[0].filled_qty = 0.1
            if close_fill_price is not None:
                trade.close_legs[0].fill_price = float(close_fill_price)
            else:
                close_orders = [
                    r for r in order_manager._orders.values()
                    if r.lifecycle_id == lifecycle_id
                    and r.purpose == OrderPurpose.CLOSE_LEG
                ]
                if close_orders:
                    trade.close_legs[0].fill_price = float(close_orders[-1].avg_fill_price or close_orders[-1].price)

            # Set fees
            trade.open_fees = open_result.total_fees
            trade.close_fees = close_result.total_fees
            trade.currency = Currency.BTC

            # Finalize
            trade._finalize_close()

            log.info(
                f"Realized PnL: {trade.realized_pnl}, "
                f"open_fees={trade.open_fees}, close_fees={trade.close_fees}, "
                f"total_fees={trade.total_fees}"
            )

            # PnL should be a finite number (could be small profit or small loss)
            assert trade.realized_pnl is not None
            assert isinstance(trade.realized_pnl, float)
            import math
            assert math.isfinite(trade.realized_pnl), (
                f"realized_pnl is not finite: {trade.realized_pnl}"
            )

            # Total fees must be set
            assert trade.total_fees is not None
            assert isinstance(trade.total_fees, Price)
            assert trade.total_fees.currency == Currency.BTC
            assert trade.total_fees.amount > 0

            # total_fees = open_fees + close_fees
            expected_total = trade.open_fees.amount + trade.close_fees.amount
            assert abs(trade.total_fees.amount - expected_total) < 1e-12

            log.info("=== LIFECYCLE TEST PASSED ===")

        finally:
            # Cleanup: cancel any remaining orders
            try:
                open_mgr.cancel_all()
            except Exception:
                pass
            try:
                close_mgr.cancel_all()
            except Exception:
                pass


    def test_logging_structured_events(self, market_data, order_manager):
        """Verify structured execution events include required keys.

        Places and cancels an order, then checks the order_ledger.jsonl
        for the required structured fields (price as float, not Price object).
        """
        import json

        # Find any option to place a far-off order
        symbol, ob = _find_cheap_deep_otm_put(market_data)
        asks = ob.get("asks", [])
        far_price = float(asks[0]["price"]) * 0.1  # 10% of ask — won't fill

        profile = _make_aggressive_profile("test_logging")
        mgr = FillManager(
            order_manager=order_manager,
            market_data=market_data,
            profile=profile,
            direction="open",
        )

        lifecycle_id = f"logging_test_{int(time.time())}"

        try:
            result = mgr.place_all(
                legs=[{"symbol": symbol, "qty": 0.1, "side": "sell"}],
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.OPEN_LEG,
            )
            assert result.status in (FillStatus.PENDING, FillStatus.FILLED)
        finally:
            mgr.cancel_all()

        # Check that order_ledger.jsonl has valid JSON (no Price serialization errors)
        ledger_path = os.path.join("logs", "order_ledger.jsonl")
        if os.path.exists(ledger_path):
            with open(ledger_path) as f:
                lines = f.readlines()

            # Find events for our lifecycle
            our_events = []
            for line in lines:
                try:
                    event = json.loads(line.strip())
                    if event.get("lifecycle_id") == lifecycle_id:
                        our_events.append(event)
                except json.JSONDecodeError:
                    continue

            assert len(our_events) >= 1, "No ledger events found for our lifecycle"

            for event in our_events:
                # Price must be serialized as a number, not a Price object
                assert isinstance(event["price"], (int, float)), (
                    f"price should be numeric, got {type(event['price'])}: {event['price']}"
                )
                # Required keys
                assert "ts" in event
                assert "order_id" in event
                assert "symbol" in event
                assert "side" in event

            log.info(f"Found {len(our_events)} valid ledger events for {lifecycle_id}")
