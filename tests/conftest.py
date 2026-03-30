"""
Shared test fixtures for the CoincallTrader test suite.

Provides mock implementations of exchange adapters so the entire
engine stack can be tested without network calls.
"""

import time
import pytest
from dataclasses import dataclass
from unittest.mock import MagicMock

from account_manager import AccountSnapshot, PositionSnapshot
from order_manager import OrderManager


# =============================================================================
# Mock Executor — records calls, returns configurable responses
# =============================================================================

class MockExecutor:
    """
    Mock ExchangeExecutor that records all calls and returns configurable results.
    Used across OrderManager, LimitFillManager, and LifecycleEngine tests.
    """

    def __init__(self):
        self.calls = []
        self._next_order_id = 1001
        self._order_statuses = {}
        self._cancel_fail_ids = set()

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
        if order_id in self._order_statuses:
            self._order_statuses[order_id]["state"] = 3
        return True

    def get_order_status(self, order_id):
        self.calls.append(("get_order_status", {"order_id": order_id}))
        return self._order_statuses.get(order_id)

    # -- Test helpers --

    def simulate_fill(self, order_id, filled_qty, avg_price, full=True):
        if order_id in self._order_statuses:
            s = self._order_statuses[order_id]
            s["fillQty"] = filled_qty
            s["avgPrice"] = avg_price
            s["remainQty"] = s["qty"] - filled_qty
            s["state"] = 1 if full else 2  # FILLED or PARTIAL

    def simulate_cancel(self, order_id):
        if order_id in self._order_statuses:
            self._order_statuses[order_id]["state"] = 3


# =============================================================================
# Mock Market Data
# =============================================================================

class MockMarketData:
    """Mock ExchangeMarketData — returns configurable orderbooks and instruments."""

    def __init__(self):
        self._orderbooks = {}
        self._instruments = []
        self._index_price = 87000.0
        self._tickers = {}

    def get_option_orderbook(self, symbol):
        return self._orderbooks.get(symbol)

    def get_option_instruments(self, underlying="BTC"):
        return self._instruments

    def get_btc_index_price(self):
        return self._index_price

    def get_option_market_data(self, symbol):
        return self._tickers.get(symbol, {})

    def set_orderbook(self, symbol, bids=None, asks=None):
        self._orderbooks[symbol] = {
            "bids": bids or [],
            "asks": asks or [],
        }

    def set_instruments(self, instruments):
        self._instruments = instruments


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_executor():
    return MockExecutor()


@pytest.fixture
def mock_market_data():
    return MockMarketData()


@pytest.fixture
def order_manager(mock_executor):
    return OrderManager(mock_executor)


def make_account(**kwargs) -> AccountSnapshot:
    """Create an AccountSnapshot with sensible defaults."""
    defaults = dict(
        equity=10000.0,
        available_margin=8000.0,
        initial_margin=2000.0,
        maintenance_margin=1000.0,
        unrealized_pnl=100.0,
        margin_utilization=20.0,
        positions=(),
        net_delta=0.5,
        net_gamma=0.01,
        net_theta=-0.5,
        net_vega=0.1,
        timestamp=time.time(),
    )
    defaults.update(kwargs)
    return AccountSnapshot(**defaults)


def make_position(**kwargs) -> PositionSnapshot:
    """Create a PositionSnapshot with sensible defaults."""
    defaults = dict(
        position_id="pos-1",
        symbol="BTCUSD-28MAR26-100000-C",
        qty=0.1,
        side="long",
        entry_price=500.0,
        mark_price=510.0,
        unrealized_pnl=1.0,
        roi=0.02,
        delta=0.5,
        gamma=0.001,
        theta=-0.05,
        vega=0.1,
    )
    defaults.update(kwargs)
    return PositionSnapshot(**defaults)
