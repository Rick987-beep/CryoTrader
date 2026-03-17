#!/usr/bin/env python3
"""
Deribit Integration Tests — Testnet

Validates all Deribit adapters against the live testnet API.
Follows the migration plan test sequencing:
  Test 1: Auth lifecycle
  Test 2: Market data (instruments, ticker, orderbook, index)
  Test 3: Account data (summary, positions)
  Test 4: Symbol translation (round-trip all instruments)
  Test 5: Order management (place, read, edit, cancel)
  Test 6: Rate limits & error handling

Requirements:
  - DERIBIT_CLIENT_ID_TEST and DERIBIT_CLIENT_SECRET_TEST in .env
  - TRADING_ENVIRONMENT=testnet
  - Network access to https://test.deribit.com

Usage:
  python -m pytest tests/test_deribit_integration.py -v
  python -m pytest tests/test_deribit_integration.py -v -k test_1  # auth only
"""

import os
import sys
import time
import pytest

# Ensure testnet
os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from exchanges.deribit.auth import DeribitAuth
from exchanges.deribit.symbols import (
    parse_deribit_symbol,
    build_deribit_symbol,
    coincall_to_deribit,
    deribit_to_coincall,
)


def _skip_if_no_creds():
    """Skip test if testnet credentials are not configured."""
    from config import DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
    if not DERIBIT_CLIENT_ID or not DERIBIT_CLIENT_SECRET:
        pytest.skip("DERIBIT_CLIENT_ID_TEST / DERIBIT_CLIENT_SECRET_TEST not set")


@pytest.fixture(scope="module")
def auth():
    """Shared DeribitAuth instance for the test module."""
    _skip_if_no_creds()
    return DeribitAuth()


@pytest.fixture(scope="module")
def market_data(auth):
    from exchanges.deribit.market_data import DeribitMarketDataAdapter
    return DeribitMarketDataAdapter(auth)


@pytest.fixture(scope="module")
def account(auth):
    from exchanges.deribit.account import DeribitAccountAdapter
    return DeribitAccountAdapter(auth)


@pytest.fixture(scope="module")
def executor(auth):
    from exchanges.deribit.executor import DeribitExecutorAdapter
    return DeribitExecutorAdapter(auth)


# ═════════════════════════════════════════════════════════════════════════════
# Test 1: Authentication Lifecycle
# ═════════════════════════════════════════════════════════════════════════════

class TestAuth:

    def test_1a_initial_auth(self, auth):
        """client_credentials grant obtains a valid token."""
        auth._ensure_token()
        assert auth._access_token is not None
        assert auth._refresh_token is not None
        assert auth._token_expires_at > time.time()
        assert auth._token_refresh_at > time.time()

    def test_1b_token_reuse(self, auth):
        """Subsequent calls reuse the existing token (no re-auth)."""
        token_before = auth._access_token
        auth._ensure_token()
        assert auth._access_token == token_before

    def test_1c_token_refresh(self, auth):
        """Force a refresh and verify tokens change."""
        old_access = auth._access_token
        old_refresh = auth._refresh_token
        auth._do_refresh()
        assert auth._access_token != old_access
        assert auth._refresh_token != old_refresh

    def test_1d_authenticated_call(self, auth):
        """A private API call succeeds with the current token."""
        resp = auth.call("private/get_account_summary", {"currency": "BTC"})
        assert "result" in resp
        assert "equity" in resp["result"]

    def test_1e_is_successful(self, auth):
        """is_successful correctly identifies success vs error."""
        good = {"result": {"equity": 1.0}}
        bad = {"error": {"code": 13009, "message": "unauthorized"}}
        assert auth.is_successful(good) is True
        assert auth.is_successful(bad) is False


# ═════════════════════════════════════════════════════════════════════════════
# Test 2: Market Data
# ═════════════════════════════════════════════════════════════════════════════

class TestMarketData:

    def test_2a_index_price(self, market_data):
        """BTC index price is a positive number."""
        price = market_data.get_index_price("BTC")
        assert price is not None
        assert price > 10000  # BTC should be above $10k

    def test_2b_instruments(self, market_data):
        """Instruments list is non-empty with correct fields."""
        instruments = market_data.get_option_instruments("BTC")
        assert instruments is not None
        assert len(instruments) > 100  # Deribit typically has 500+

        inst = instruments[0]
        assert "symbolName" in inst
        assert "strike" in inst
        assert "expirationTimestamp" in inst
        assert inst["strike"] > 0
        assert inst["expirationTimestamp"] > 0

    def test_2c_ticker(self, market_data):
        """Ticker for a specific instrument returns expected fields."""
        instruments = market_data.get_option_instruments("BTC")
        assert instruments
        symbol = instruments[0]["symbolName"]

        details = market_data.get_option_details(symbol)
        assert details is not None
        assert "delta" in details
        assert "markPrice" in details
        assert "indexPrice" in details
        assert "impliedVolatility" in details
        # markPrice should be in USD (converted from BTC)
        assert details["markPrice"] >= 0
        assert details["indexPrice"] > 10000

    def test_2d_orderbook(self, market_data):
        """Orderbook returns bids and asks."""
        instruments = market_data.get_option_instruments("BTC")
        assert instruments
        # Pick a liquid instrument (ATM-ish)
        symbol = instruments[len(instruments) // 2]["symbolName"]

        ob = market_data.get_option_orderbook(symbol)
        assert ob is not None
        assert "bids" in ob
        assert "asks" in ob
        # At least one side should have levels for a liquid option
        assert isinstance(ob["bids"], list)
        assert isinstance(ob["asks"], list)


# ═════════════════════════════════════════════════════════════════════════════
# Test 3: Account Data
# ═════════════════════════════════════════════════════════════════════════════

class TestAccount:

    def test_3a_account_summary(self, account):
        """Account summary returns normalized fields."""
        info = account.get_account_info()
        assert info is not None
        assert "equity" in info
        assert "available_margin" in info
        assert "initial_margin" in info
        assert "maintenance_margin" in info
        assert "timestamp" in info
        # Equity should be non-negative
        assert info["equity"] >= 0

    def test_3b_positions(self, account):
        """Positions returns a list (may be empty on testnet)."""
        positions = account.get_positions()
        assert isinstance(positions, list)
        # If there are positions, check field names
        if positions:
            p = positions[0]
            assert "symbol" in p
            assert "qty" in p
            assert "trade_side" in p
            assert "delta" in p

    def test_3c_open_orders(self, account):
        """Open orders returns a list (may be empty)."""
        orders = account.get_open_orders()
        assert isinstance(orders, list)
        if orders:
            o = orders[0]
            assert "order_id" in o
            assert "symbol" in o
            assert "state" in o


# ═════════════════════════════════════════════════════════════════════════════
# Test 4: Symbol Translation
# ═════════════════════════════════════════════════════════════════════════════

class TestSymbols:

    def test_4a_parse_deribit_symbol(self):
        """Parse standard Deribit symbols."""
        result = parse_deribit_symbol("BTC-28MAR26-100000-C")
        assert result is not None
        assert result["underlying"] == "BTC"
        assert result["day"] == "28"
        assert result["month"] == "MAR"
        assert result["year"] == "26"
        assert result["strike"] == "100000"
        assert result["option_type"] == "C"

    def test_4b_parse_single_digit_day(self):
        """Parse symbol with single-digit day."""
        result = parse_deribit_symbol("BTC-3APR26-74000-P")
        assert result is not None
        assert result["day"] == "3"
        assert result["month"] == "APR"

    def test_4c_build_deribit_symbol(self):
        """Build a Deribit symbol from components."""
        sym = build_deribit_symbol("BTC", "03", "APR", "26", "74000", "C")
        assert sym == "BTC-3APR26-74000-C"  # day should be unpadded

    def test_4d_coincall_to_deribit(self):
        """Convert Coincall symbol to Deribit."""
        assert coincall_to_deribit("BTCUSD-03APR26-74000-C") == "BTC-3APR26-74000-C"
        assert coincall_to_deribit("BTCUSD-28MAR26-100000-P") == "BTC-28MAR26-100000-P"

    def test_4e_deribit_to_coincall(self):
        """Convert Deribit symbol to Coincall."""
        assert deribit_to_coincall("BTC-3APR26-74000-C") == "BTCUSD-03APR26-74000-C"
        assert deribit_to_coincall("BTC-28MAR26-100000-P") == "BTCUSD-28MAR26-100000-P"

    def test_4f_round_trip_all_instruments(self, market_data):
        """Parse every instrument from Deribit and verify round-trip."""
        instruments = market_data.get_option_instruments("BTC")
        assert instruments

        failures = []
        for inst in instruments:
            name = inst["symbolName"]
            parsed = parse_deribit_symbol(name)
            if parsed is None:
                failures.append(f"PARSE FAIL: {name}")
                continue
            rebuilt = build_deribit_symbol(
                parsed["underlying"], parsed["day"], parsed["month"],
                parsed["year"], parsed["strike"], parsed["option_type"],
            )
            if rebuilt != name:
                failures.append(f"ROUND-TRIP FAIL: {name} → {rebuilt}")

        assert not failures, f"{len(failures)} failures:\n" + "\n".join(failures[:10])

    def test_4g_reject_non_option(self):
        """Non-option symbols (perpetual, futures) should return None."""
        assert parse_deribit_symbol("BTC-PERPETUAL") is None
        assert parse_deribit_symbol("BTC-28MAR26") is None  # future, no strike


# ═════════════════════════════════════════════════════════════════════════════
# Test 5: Order Management (place → read → cancel)
# ═════════════════════════════════════════════════════════════════════════════

class TestOrders:

    def test_5a_place_and_cancel(self, executor, market_data):
        """Place a limit order far below market, verify status, then cancel."""
        instruments = market_data.get_option_instruments("BTC")
        assert instruments

        # Pick a liquid instrument
        symbol = instruments[len(instruments) // 2]["symbolName"]
        details = market_data.get_option_details(symbol)
        if not details or details.get("_best_ask_btc", 0) <= 0:
            pytest.skip(f"No ask for {symbol}")

        # Place a buy order way below market (should not fill)
        far_price = max(0.0001, details["_best_bid_btc"] * 0.5)
        from exchanges.deribit.executor import _snap_to_tick
        far_price = _snap_to_tick(far_price)

        result = executor.place_order(
            symbol=symbol,
            qty=0.1,
            side="buy",
            order_type=1,
            price=far_price,
            client_order_id="test_smoke_001",
        )
        assert result is not None, "place_order returned None"
        assert "orderId" in result
        assert result["state"] == "open"
        order_id = result["orderId"]

        # Read order status
        status = executor.get_order_status(order_id)
        assert status is not None
        assert status["state"] == "open"
        assert status["orderId"] == order_id
        assert status["clientOrderId"] == "test_smoke_001"

        # Cancel
        cancelled = executor.cancel_order(order_id)
        assert cancelled is True

        # Verify cancelled state
        time.sleep(0.5)
        status = executor.get_order_status(order_id)
        assert status is not None
        assert status["state"] == "cancelled"

    def test_5b_invalid_instrument(self, executor):
        """Placing an order on a non-existent instrument fails gracefully."""
        result = executor.place_order(
            symbol="BTC-1JAN99-999999-C",
            qty=0.1,
            side="buy",
            order_type=1,
            price=0.001,
        )
        assert result is None  # Should return None on failure

    def test_5c_tick_size_snap(self):
        """Tick size snapping works correctly."""
        from exchanges.deribit.executor import _snap_to_tick
        # Below 0.005: tick = 0.0001
        assert _snap_to_tick(0.0035) == 0.0035
        assert _snap_to_tick(0.00351) == 0.0035
        # At/above 0.005: tick = 0.0005
        assert _snap_to_tick(0.005) == 0.005
        assert _snap_to_tick(0.0053) == 0.005
        assert _snap_to_tick(0.021) == 0.021
        assert _snap_to_tick(0.0212) == 0.021


# ═════════════════════════════════════════════════════════════════════════════
# Test 6: Error Handling & Rate Limits
# ═════════════════════════════════════════════════════════════════════════════

class TestResilience:

    def test_6a_error_detection(self, auth):
        """Calling a non-existent method returns error, not crash."""
        resp = auth.call("private/nonexistent_method", {})
        assert "error" in resp
        assert auth.is_successful(resp) is False

    def test_6b_expired_instrument(self, auth):
        """Querying an expired instrument returns error gracefully."""
        resp = auth.call("public/ticker", {"instrument_name": "BTC-1JAN20-50000-C"})
        assert "error" in resp

    def test_6c_rapid_fire(self, auth):
        """Multiple rapid calls don't get throttled."""
        results = []
        for _ in range(10):
            resp = auth.call("public/get_index_price", {"index_name": "btc_usd"})
            results.append("result" in resp)
        success_rate = sum(results) / len(results)
        assert success_rate >= 0.9, f"Only {success_rate*100:.0f}% succeeded"
