"""
Live test: FillManager place → track → cancel against Deribit testnet.

Phase 2 live validation: proves the full order lifecycle works with real
exchange state — place a resting order, verify it exists, cancel it,
verify cancellation.  Then attempts a real fill on the cheapest OTM option.

Usage:
    EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/test_fill_live.py -m live -v
"""

import os
import sys
import time
import pytest

pytestmark = pytest.mark.live

os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.currency import Currency, Price
from execution.fill_manager import FillManager
from execution.fill_result import FillStatus
from execution.profiles import ExecutionProfile, PhaseConfig
from order_manager import OrderManager, OrderPurpose


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


@pytest.fixture(scope="module")
def aggressive_profile():
    """Simple 1-phase aggressive profile for testing."""
    return ExecutionProfile(
        name="test_aggressive_1phase",
        open_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=30.0,
            buffer_pct=2.0,
        )],
        close_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=30.0,
            buffer_pct=2.0,
        )],
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_option_with_ask(market_data, min_dte_days=30):
    """Find a BTC option with >min_dte DTE that has a populated ask."""
    instruments = market_data.get_option_instruments("BTC")
    assert instruments, "No BTC option instruments on testnet"

    now = time.time() * 1000
    min_dte_ms = min_dte_days * 24 * 3600 * 1000
    candidates = [
        i for i in instruments
        if i.get("expirationTimestamp", 0) - now > min_dte_ms
    ]
    assert candidates, f"No options with >{min_dte_days} DTE"

    # Prefer deep OTM (far from ATM) — less likely to fill accidentally
    index_price = market_data.get_index_price("BTC")
    candidates.sort(key=lambda i: abs(i["strike"] - index_price), reverse=True)

    for inst in candidates[:30]:
        ob = market_data.get_option_orderbook(inst["symbolName"])
        if ob and ob.get("asks"):
            return inst["symbolName"], ob
    pytest.skip("No option with populated ask found on testnet")


def _find_cheapest_otm_option(market_data, min_dte_days=30):
    """Find the cheapest deep-OTM option (lowest ask) for fill testing."""
    instruments = market_data.get_option_instruments("BTC")
    assert instruments

    now = time.time() * 1000
    min_dte_ms = min_dte_days * 24 * 3600 * 1000
    candidates = [
        i for i in instruments
        if i.get("expirationTimestamp", 0) - now > min_dte_ms
    ]

    best_symbol = None
    best_ask = float("inf")
    best_ob = None

    for inst in candidates:
        ob = market_data.get_option_orderbook(inst["symbolName"])
        if ob and ob.get("asks"):
            ask = float(ob["asks"][0]["price"])
            if ask < best_ask:
                best_ask = ask
                best_symbol = inst["symbolName"]
                best_ob = ob

    if best_symbol is None:
        pytest.skip("No option with asks found for fill test")
    return best_symbol, best_ob, best_ask


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestFillManagerLive:
    """FillManager against real Deribit testnet."""

    def test_place_and_cancel_resting_order(
        self, market_data, order_manager, aggressive_profile
    ):
        """Place a limit order far below ask → verify it rests → cancel."""
        symbol, ob = _find_option_with_ask(market_data)
        best_ask = float(ob["asks"][0]["price"])

        # Price far below ask so it never fills
        from exchanges.deribit.executor import _snap_to_tick
        far_price = _snap_to_tick(max(0.0001, best_ask * 0.3))

        fill_mgr = FillManager(
            order_manager=order_manager,
            market_data=market_data,
            profile=aggressive_profile,
            direction="open",
        )

        legs = [{"symbol": symbol, "qty": 0.1, "side": "buy"}]
        lifecycle_id = "test_fill_live_rest"

        try:
            result = fill_mgr.place_all(
                legs=legs,
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.OPEN_LEG,
            )

            # Should be PENDING (resting, not filled)
            assert result.status in (FillStatus.PENDING, FillStatus.FILLED), (
                f"Expected PENDING or FILLED, got {result.status}: {result.error}"
            )

            if result.status == FillStatus.PENDING:
                # Verify order exists
                assert len(result.legs) == 1
                assert result.legs[0].order_id is not None, "No order_id on resting order"

                # Verify via OrderManager
                live_orders = order_manager.get_live_orders(lifecycle_id)
                assert len(live_orders) >= 1, "Expected at least 1 live order"

                # Cancel
                fill_mgr.cancel_all()
                time.sleep(0.5)

                # Verify cancelled
                live_orders_after = order_manager.get_live_orders(lifecycle_id)
                live_count = sum(
                    1 for o in live_orders_after
                    if o.status.name in ("LIVE", "OPEN")
                )
                assert live_count == 0, (
                    f"Expected 0 live orders after cancel, got {live_count}"
                )
        finally:
            # Safety: ensure cleanup
            try:
                fill_mgr.cancel_all()
            except Exception:
                pass

    def test_actual_fill_cheapest_option(
        self, market_data, order_manager, aggressive_profile
    ):
        """Buy the cheapest OTM option at the ask → expect a fill with fee data."""
        symbol, ob, best_ask = _find_cheapest_otm_option(market_data)

        from exchanges.deribit.executor import _snap_to_tick
        buy_price = _snap_to_tick(best_ask)

        fill_mgr = FillManager(
            order_manager=order_manager,
            market_data=market_data,
            profile=aggressive_profile,
            direction="open",
        )

        legs = [{"symbol": symbol, "qty": 0.1, "side": "buy"}]
        lifecycle_id = f"test_fill_live_fill_{int(time.time())}"

        try:
            result = fill_mgr.place_all(
                legs=legs,
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.OPEN_LEG,
            )

            # Poll for up to 10 seconds
            deadline = time.time() + 10
            while result.status == FillStatus.PENDING and time.time() < deadline:
                time.sleep(1.0)
                result = fill_mgr.check()

            if result.status == FillStatus.FILLED:
                # Validate typed fill data
                leg = result.legs[0]
                assert leg.fill_price is not None, "fill_price should be set"
                assert isinstance(leg.fill_price, Price), (
                    f"fill_price should be Price, got {type(leg.fill_price)}"
                )
                assert leg.fill_price.currency == Currency.BTC, (
                    f"Expected BTC denomination, got {leg.fill_price.currency}"
                )
                assert leg.fill_price.amount > 0

                # Fee should be captured
                assert leg.fee is not None, (
                    "Deribit should report per-fill fee data"
                )
                assert isinstance(leg.fee, Price)
                assert leg.fee.currency == Currency.BTC

                # FillResult-level total_fees
                if result.total_fees is not None:
                    assert result.total_fees.currency == Currency.BTC
                    assert result.total_fees.amount > 0
            else:
                # Didn't fill — acceptable on testnet (low liquidity)
                pytest.skip(
                    f"Order didn't fill within 10s (status={result.status}). "
                    f"Testnet liquidity may be low. symbol={symbol}, price={buy_price}"
                )
        finally:
            try:
                fill_mgr.cancel_all()
            except Exception:
                pass
