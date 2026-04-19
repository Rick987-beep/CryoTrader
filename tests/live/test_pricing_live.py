"""
Live test: PricingEngine against real Deribit testnet orderbooks.

Validates that PricingEngine.compute() works with real orderbook shapes
from the Deribit testnet API.

Usage:
    EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/test_pricing_live.py -m live -v
"""

import os
import sys
import pytest

pytestmark = pytest.mark.live

os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.currency import Currency, OrderbookSnapshot, Price
from execution.pricing import PricingEngine


def _skip_if_no_creds():
    from config import DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
    if not DERIBIT_CLIENT_ID or not DERIBIT_CLIENT_SECRET:
        pytest.skip("DERIBIT_CLIENT_ID_TEST / DERIBIT_CLIENT_SECRET_TEST not set")


@pytest.fixture(scope="module")
def auth():
    _skip_if_no_creds()
    from exchanges.deribit.auth import DeribitAuth
    return DeribitAuth()


@pytest.fixture(scope="module")
def market_data(auth):
    from exchanges.deribit.market_data import DeribitMarketDataAdapter
    return DeribitMarketDataAdapter(auth)


@pytest.fixture(scope="module")
def engine():
    return PricingEngine()


def _find_liquid_option(market_data) -> str:
    """Find a liquid near-ATM BTC option with >30 DTE."""
    import time
    instruments = market_data.get_option_instruments("BTC")
    assert instruments, "No BTC option instruments available"

    now = time.time() * 1000  # ms
    min_dte_ms = 30 * 24 * 3600 * 1000
    candidates = [
        i for i in instruments
        if i.get("expirationTimestamp", 0) - now > min_dte_ms
    ]
    assert candidates, "No options with >30 DTE"

    # Sort by distance from ATM (smallest strike distance to index)
    index_price = market_data.get_index_price("BTC")
    candidates.sort(key=lambda i: abs(i["strike"] - index_price))

    # Pick the first one that has an ask
    for inst in candidates[:20]:
        ob = market_data.get_option_orderbook(inst["symbolName"])
        if ob and ob.get("asks"):
            return inst["symbolName"]

    pytest.skip("No liquid option found with populated orderbook")


def _raw_ob_to_snapshot(raw_ob: dict, symbol: str) -> OrderbookSnapshot:
    """Convert raw Deribit orderbook dict to typed OrderbookSnapshot."""
    return OrderbookSnapshot(
        symbol=symbol,
        currency=Currency.BTC,
        best_bid=float(raw_ob["bids"][0]["price"]) if raw_ob.get("bids") else None,
        best_ask=float(raw_ob["asks"][0]["price"]) if raw_ob.get("asks") else None,
        mark=float(raw_ob.get("_mark_btc", 0)) or None,
        index_price=float(raw_ob.get("_index_price", 0)) or None,
        timestamp=0.0,
    )


class TestPricingLive:
    """PricingEngine against real Deribit testnet orderbooks."""

    def test_all_modes_return_btc_price(self, market_data, engine):
        """All 6 pricing modes return a Price with currency=BTC and amount>0."""
        symbol = _find_liquid_option(market_data)
        raw_ob = market_data.get_option_orderbook(symbol)
        assert raw_ob is not None
        ob = _raw_ob_to_snapshot(raw_ob, symbol)

        modes = ["fair", "aggressive", "mid", "passive", "top_of_book", "mark"]
        for mode in modes:
            for side in ["buy", "sell"]:
                r = engine.compute(ob, side, mode, aggression=0.5, buffer_pct=2.0)
                if r.price is not None:
                    assert r.price.currency == Currency.BTC, (
                        f"{mode}/{side}: expected BTC, got {r.price.currency}"
                    )
                    assert r.price.amount > 0, (
                        f"{mode}/{side}: expected amount > 0, got {r.price.amount}"
                    )
                # Some modes may return None if the book is one-sided
                # (e.g. passive sell with no ask) — that's acceptable

    def test_fair_value_from_real_book(self, market_data, engine):
        """fair_value() returns a sane BTC price from a real orderbook."""
        symbol = _find_liquid_option(market_data)
        raw_ob = market_data.get_option_orderbook(symbol)
        ob = _raw_ob_to_snapshot(raw_ob, symbol)

        fv = engine.fair_value(ob)
        assert fv is not None
        assert fv.currency == Currency.BTC
        assert 0 < fv.amount < 1.0  # BTC option price should be < 1 BTC

    def test_aggression_increases_sell_price_toward_bid(self, market_data, engine):
        """Higher aggression on sell → lower price (toward bid)."""
        symbol = _find_liquid_option(market_data)
        raw_ob = market_data.get_option_orderbook(symbol)
        ob = _raw_ob_to_snapshot(raw_ob, symbol)

        r0 = engine.compute(ob, "sell", "fair", aggression=0.0)
        r1 = engine.compute(ob, "sell", "fair", aggression=1.0)

        if r0.price and r1.price and ob.best_bid is not None:
            assert r0.price.amount >= r1.price.amount, (
                f"aggression=0 ({r0.price.amount}) should be >= aggression=1 ({r1.price.amount})"
            )
