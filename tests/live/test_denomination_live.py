"""
Live test: Denomination guard against Deribit testnet.

Phase 3 live validation: proves that:
  1. A correctly denominated BTC Price passes through to the exchange.
  2. A bogus USD Price is rejected locally (DenominationError) before
     reaching the API.

Usage:
    EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/test_denomination_live.py -m live -v
"""

import os
import sys
import time
import pytest

pytestmark = pytest.mark.live

os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.currency import Currency, DenominationError, OrderbookSnapshot, Price
from execution.pricing import PricingEngine
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
def order_manager_with_guard(executor):
    """OrderManager with BTC denomination guard enabled."""
    from exchanges.deribit import DERIBIT_STATE_MAP
    return OrderManager(
        executor,
        exchange_state_map=DERIBIT_STATE_MAP,
        expected_denomination=Currency.BTC,
    )


@pytest.fixture(scope="module")
def engine():
    return PricingEngine()


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_option_with_ask(market_data, min_dte_days=30):
    """Find a BTC option with populated orderbook."""
    instruments = market_data.get_option_instruments("BTC")
    assert instruments

    now = time.time() * 1000
    min_dte_ms = min_dte_days * 24 * 3600 * 1000
    candidates = [
        i for i in instruments
        if i.get("expirationTimestamp", 0) - now > min_dte_ms
    ]
    assert candidates, f"No options with >{min_dte_days} DTE"

    index_price = market_data.get_index_price("BTC")
    # Pick deep OTM to avoid accidental fills
    candidates.sort(key=lambda i: abs(i["strike"] - index_price), reverse=True)

    for inst in candidates[:30]:
        ob = market_data.get_option_orderbook(inst["symbolName"])
        if ob and ob.get("asks") and ob.get("bids"):
            return inst["symbolName"], ob
    pytest.skip("No option with populated bid+ask found on testnet")


def _raw_ob_to_snapshot(raw_ob: dict, symbol: str) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol=symbol,
        currency=Currency.BTC,
        best_bid=float(raw_ob["bids"][0]["price"]) if raw_ob.get("bids") else None,
        best_ask=float(raw_ob["asks"][0]["price"]) if raw_ob.get("asks") else None,
        mark=float(raw_ob.get("_mark_btc", 0)) or None,
        index_price=float(raw_ob.get("_index_price", 0)) or None,
        timestamp=0.0,
    )


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestDenominationLive:
    """Denomination guard against real Deribit testnet."""

    def test_btc_price_accepted_by_exchange(
        self, market_data, order_manager_with_guard, engine
    ):
        """Compute fair price (BTC) → place order → verify accepted → cancel."""
        symbol, raw_ob = _find_option_with_ask(market_data)
        ob = _raw_ob_to_snapshot(raw_ob, symbol)

        # Compute fair sell price (returns Price with currency=BTC)
        result = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert result.price is not None, "PricingEngine returned None price"
        assert result.price.currency == Currency.BTC

        # Use a price well below the computed fair to rest without filling
        from exchanges.deribit.executor import _snap_to_tick
        resting_price = Price(
            _snap_to_tick(max(0.0001, result.price.amount * 0.3)),
            Currency.BTC,
        )

        # Place order — should be accepted (correct denomination)
        record = order_manager_with_guard.place_order(
            lifecycle_id="test_denom_btc",
            leg_index=0,
            purpose=OrderPurpose.OPEN_LEG,
            symbol=symbol,
            side="buy",
            qty=0.1,
            price=resting_price,
        )

        try:
            assert record is not None, (
                f"Order rejected by exchange for {symbol} at {resting_price}"
            )
            assert record.order_id, "No order_id returned"
        finally:
            # Cancel the order
            if record and record.order_id:
                order_manager_with_guard.cancel_order(record.order_id)
                time.sleep(0.3)

    def test_usd_price_rejected_locally(
        self, market_data, order_manager_with_guard
    ):
        """Bogus USD price must raise DenominationError before reaching API."""
        symbol, _ = _find_option_with_ask(market_data)

        bogus_usd_price = Price(3200.0, Currency.USD)

        with pytest.raises(DenominationError) as exc_info:
            order_manager_with_guard.place_order(
                lifecycle_id="test_denom_usd",
                leg_index=0,
                purpose=OrderPurpose.OPEN_LEG,
                symbol=symbol,
                side="buy",
                qty=0.1,
                price=bogus_usd_price,
            )

        # Verify the error message is clear
        assert "USD" in str(exc_info.value)
        assert "BTC" in str(exc_info.value)

    def test_float_price_bypasses_guard(
        self, market_data, order_manager_with_guard
    ):
        """Legacy float prices bypass the denomination guard (backward compat)."""
        symbol, raw_ob = _find_option_with_ask(market_data)

        # Use a float price far below ask
        from exchanges.deribit.executor import _snap_to_tick
        best_ask = float(raw_ob["asks"][0]["price"])
        far_price = _snap_to_tick(max(0.0001, best_ask * 0.3))

        # Should not raise — floats bypass the guard
        record = order_manager_with_guard.place_order(
            lifecycle_id="test_denom_float",
            leg_index=0,
            purpose=OrderPurpose.OPEN_LEG,
            symbol=symbol,
            side="buy",
            qty=0.1,
            price=far_price,  # plain float
        )

        try:
            assert record is not None, "Float price order rejected unexpectedly"
        finally:
            if record and record.order_id:
                order_manager_with_guard.cancel_order(record.order_id)
                time.sleep(0.3)
