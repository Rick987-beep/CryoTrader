"""
Phase 5 unit tests — cleanup validation.

Tests:
  - executable_pnl standalone function
  - Price JSON serialization in OrderManager._persist_event
  - Structured logging events include required keys
  - Legacy ExecutionParams import still works (backward compat)
"""

import json
import logging
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from execution.currency import Currency, Price
from trade_lifecycle import (
    TradeLeg,
    TradeLifecycle,
    TradeState,
    executable_pnl,
)


# ─── executable_pnl standalone function ─────────────────────────────────────

class TestExecutablePnl:
    """Test the extracted standalone executable_pnl function."""

    def _make_market_data(self, orderbooks):
        """Create a mock market_data that returns given orderbooks by symbol."""
        md = MagicMock()
        md.get_option_orderbook.side_effect = lambda sym: orderbooks.get(sym)
        return md

    def test_single_short_leg(self):
        """Short position: PnL = (entry - close_ask) * qty."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.010  # sold at 0.010 BTC

        md = self._make_market_data({
            "BTC-P": {
                "bids": [{"price": 0.005}],
                "asks": [{"price": 0.006}],  # buy back at 0.006
            }
        })

        pnl = executable_pnl(legs, md)
        assert pnl is not None
        assert abs(pnl - (0.010 - 0.006) * 1.0) < 1e-10  # profit = 0.004

    def test_single_long_leg(self):
        """Long position: PnL = (close_bid - entry) * qty."""
        legs = [TradeLeg(symbol="BTC-C", qty=0.5, side="buy")]
        legs[0].filled_qty = 0.5
        legs[0].fill_price = 0.020

        md = self._make_market_data({
            "BTC-C": {
                "bids": [{"price": 0.025}],  # sell at 0.025
                "asks": [{"price": 0.027}],
            }
        })

        pnl = executable_pnl(legs, md)
        assert pnl is not None
        assert abs(pnl - (0.025 - 0.020) * 0.5) < 1e-10  # profit = 0.0025

    def test_multi_leg_strangle(self):
        """Two short legs (strangle): sum of individual PnLs."""
        legs = [
            TradeLeg(symbol="BTC-C", qty=1.0, side="sell"),
            TradeLeg(symbol="BTC-P", qty=1.0, side="sell"),
        ]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.015
        legs[1].filled_qty = 1.0
        legs[1].fill_price = 0.010

        md = self._make_market_data({
            "BTC-C": {"bids": [{"price": 0.012}], "asks": [{"price": 0.013}]},
            "BTC-P": {"bids": [{"price": 0.007}], "asks": [{"price": 0.008}]},
        })

        pnl = executable_pnl(legs, md)
        expected = (0.015 - 0.013) + (0.010 - 0.008)  # 0.002 + 0.002 = 0.004
        assert pnl is not None
        assert abs(pnl - expected) < 1e-10

    def test_no_orderbook_returns_none(self):
        """Returns None when orderbook is unavailable."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.010

        md = self._make_market_data({"BTC-P": None})
        assert executable_pnl(legs, md) is None

    def test_no_asks_for_short_returns_none(self):
        """Returns None when no asks available to close short."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.010

        md = self._make_market_data({"BTC-P": {"bids": [{"price": 0.005}], "asks": []}})
        assert executable_pnl(legs, md) is None

    def test_no_bids_for_long_returns_none(self):
        """Returns None when no bids available to close long."""
        legs = [TradeLeg(symbol="BTC-C", qty=1.0, side="buy")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.020

        md = self._make_market_data({"BTC-C": {"bids": [], "asks": [{"price": 0.025}]}})
        assert executable_pnl(legs, md) is None

    def test_unfilled_leg_returns_none(self):
        """Returns None when a leg has no fill_price."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        # fill_price is None by default
        md = self._make_market_data({"BTC-P": {"bids": [{"price": 0.005}], "asks": [{"price": 0.006}]}})
        assert executable_pnl(legs, md) is None

    def test_zero_close_price_returns_none(self):
        """Returns None when close price is zero."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.010

        md = self._make_market_data({"BTC-P": {"bids": [{"price": 0.005}], "asks": [{"price": 0}]}})
        assert executable_pnl(legs, md) is None

    def test_none_market_data_returns_none(self):
        """Returns None when market_data is None."""
        legs = [TradeLeg(symbol="BTC-P", qty=1.0, side="sell")]
        legs[0].filled_qty = 1.0
        legs[0].fill_price = 0.010
        assert executable_pnl(legs, None) is None

    def test_method_delegates_to_standalone(self):
        """TradeLifecycle.executable_pnl() delegates to standalone function."""
        trade = TradeLifecycle(
            open_legs=[TradeLeg(symbol="BTC-P", qty=1.0, side="sell")],
        )
        trade.open_legs[0].filled_qty = 1.0
        trade.open_legs[0].fill_price = 0.010

        md = MagicMock()
        md.get_option_orderbook.return_value = {
            "bids": [{"price": 0.005}],
            "asks": [{"price": 0.006}],
        }
        trade._market_data = md

        pnl = trade.executable_pnl()
        assert pnl is not None
        assert abs(pnl - (0.010 - 0.006) * 1.0) < 1e-10


# ─── Price JSON serialization ───────────────────────────────────────────────

class TestPriceJsonSerialization:
    """Verify Price objects are properly serialized as floats."""

    def test_price_float_conversion(self):
        """Price(0.001, BTC) → float(price) → 0.001."""
        p = Price(0.001, Currency.BTC)
        assert float(p) == 0.001

    def test_price_in_json_dumps(self):
        """Price converted to float is JSON serializable."""
        p = Price(0.001, Currency.BTC)
        event = {"price": float(p)}
        result = json.dumps(event)
        parsed = json.loads(result)
        assert parsed["price"] == 0.001

    def test_raw_price_not_json_serializable(self):
        """Raw Price object raises TypeError in json.dumps."""
        p = Price(0.001, Currency.BTC)
        with pytest.raises(TypeError, match="not JSON serializable"):
            json.dumps({"price": p})


# ─── Structured logging events ──────────────────────────────────────────────

class TestStructuredLogging:
    """Verify enhanced structured logging events."""

    def test_trade_opened_event_has_required_keys(self):
        """TRADE_OPENED event should include fill_status, fee_total, denomination."""
        from lifecycle_engine import LifecycleEngine
        from execution.fill_result import FillResult, FillStatus, LegFillSnapshot

        captured = []

        class CapturingHandler(logging.Handler):
            def emit(self, record):
                if isinstance(record.msg, dict) and record.msg.get("event") == "TRADE_OPENED":
                    captured.append(record.msg)

        handler = CapturingHandler()
        strategy_logger = logging.getLogger("ct.strategy")
        old_level = strategy_logger.level
        strategy_logger.setLevel(logging.DEBUG)
        strategy_logger.addHandler(handler)

        try:
            # Create a mock engine with enough wiring
            mock_executor = MagicMock()
            mock_rfq = MagicMock()
            mock_md = MagicMock()

            engine = LifecycleEngine(
                executor=mock_executor,
                rfq_executor=mock_rfq,
                market_data=mock_md,
            )

            trade = engine.create(
                legs=[TradeLeg(symbol="BTC-P", qty=0.1, side="sell")],
                strategy_id="test",
            )
            trade.state = TradeState.OPENING
            trade.currency = Currency.BTC

            # Create a mock FillManager that returns FILLED
            mock_mgr = MagicMock()
            mock_leg = MagicMock()
            mock_leg.symbol = "BTC-P"
            mock_leg.filled_qty = 0.1
            mock_leg.fill_price = Price(0.001, Currency.BTC)
            mock_leg.order_id = "test_order"
            mock_leg.fee = Price(0.00001, Currency.BTC)
            mock_mgr.legs = [mock_leg]
            mock_mgr.detected_currency = Currency.BTC

            mock_result = FillResult(
                status=FillStatus.FILLED,
                legs=[LegFillSnapshot(
                    symbol="BTC-P", side="sell", qty=0.1,
                    filled_qty=0.1, fill_price=Price(0.001, Currency.BTC),
                    order_id="test_order", fee=Price(0.00001, Currency.BTC),
                )],
                phase_index=1, phase_total=1, phase_pricing="aggressive",
                elapsed_seconds=2.0,
            )
            mock_mgr.check.return_value = mock_result
            mock_mgr.has_skipped_legs = False
            trade.metadata["_open_fill_mgr"] = mock_mgr

            engine._check_open_fills(trade)

            assert len(captured) == 1
            event = captured[0]
            assert event["event"] == "TRADE_OPENED"
            assert "fill_status" in event
            assert event["fill_status"] == "filled"
            assert "fee_total" in event
            assert "denomination" in event
            assert event["denomination"] == "BTC"
            assert "legs" in event
        finally:
            strategy_logger.removeHandler(handler)
            strategy_logger.setLevel(old_level)


# ─── Legacy backward compat ─────────────────────────────────────────────────

class TestLegacyBackwardCompat:
    """Verify old ExecutionParams still importable and usable."""

    def test_execution_params_importable(self):
        """ExecutionParams can still be imported from trade_execution."""
        from trade_execution import ExecutionParams, ExecutionPhase
        ep = ExecutionParams(phases=[ExecutionPhase(pricing="fair")])
        assert len(ep.phases) == 1

    def test_trade_lifecycle_accepts_execution_params(self):
        """TradeLifecycle.execution_params field still works."""
        from trade_execution import ExecutionParams
        ep = ExecutionParams()
        t = TradeLifecycle(execution_params=ep)
        assert t.execution_params is ep

    def test_trade_lifecycle_execution_params_none_by_default(self):
        t = TradeLifecycle()
        assert t.execution_params is None
