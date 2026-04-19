"""
Tests for execution.router.Router — the typed execution router.

Uses mock OrderManager, MarketData, Executor, RFQExecutor (no API calls).
"""

import time
from unittest.mock import MagicMock, patch

import pytest

from execution.fill_result import FillResult, FillStatus
from execution.profiles import ExecutionProfile, PhaseConfig
from execution.router import Router
from order_manager import OrderPurpose, OrderRecord, OrderStatus
from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState


# =============================================================================
# Helpers
# =============================================================================

def _make_md():
    md = MagicMock()
    md.get_option_orderbook = MagicMock(return_value={
        "bids": [{"price": 0.0100}],
        "asks": [{"price": 0.0110}],
        "mark": 0.0105,
        "_mark_btc": 0.0105,
        "_index_price": 50000.0,
    })
    md.get_option_details = MagicMock(return_value={"markPrice": 0.0105})
    return md


def _make_om():
    om = MagicMock()
    _counter = [0]

    def place_order(lifecycle_id, leg_index, purpose, symbol, side, qty, price, reduce_only=False):
        _counter[0] += 1
        oid = f"ORD-{_counter[0]}"
        return OrderRecord(
            order_id=oid, client_order_id=str(_counter[0]),
            lifecycle_id=lifecycle_id, leg_index=leg_index, purpose=purpose,
            symbol=symbol, side=side, qty=qty, price=price,
            reduce_only=reduce_only, status=OrderStatus.PENDING,
            placed_at=time.time(),
        )

    om.place_order = MagicMock(side_effect=place_order)
    om.poll_order = MagicMock(return_value=None)
    om.cancel_order = MagicMock(return_value=True)
    om.get_live_orders = MagicMock(return_value=[])
    return om


def _make_trade(
    open_legs=None,
    state=TradeState.PENDING_OPEN,
    execution_mode="limit",
    metadata=None,
):
    """Create a minimal TradeLifecycle with mocked fields."""
    trade = MagicMock(spec=TradeLifecycle)
    trade.id = "T-001"
    trade.state = state
    trade.execution_mode = execution_mode
    trade.execution_params = None
    trade.rfq_params = None
    trade.rfq_action = "sell"
    trade.open_legs = open_legs or [TradeLeg(symbol="SYM-C", qty=0.1, side="sell")]
    trade.close_legs = []
    trade.metadata = metadata or {}
    trade.opened_at = None
    trade.closed_at = None
    trade.error = None
    trade.rfq_result = None
    trade.close_rfq_result = None
    return trade


def _make_router(om=None, md=None):
    """Build Router with mock deps."""
    om = om or _make_om()
    md = md or _make_md()
    executor = MagicMock()
    rfq_executor = MagicMock()
    return Router(executor, rfq_executor, om, md), om, md


# =============================================================================
# open — limit mode
# =============================================================================

class TestOpenLimit:
    def test_open_returns_pending(self):
        router, om, md = _make_router()
        trade = _make_trade()

        result = router.open(trade)

        assert isinstance(result, FillResult)
        assert result.status == FillStatus.PENDING
        assert trade.state == TradeState.OPENING
        assert "_open_fill_mgr" in trade.metadata

    def test_open_places_orders(self):
        router, om, md = _make_router()
        trade = _make_trade(open_legs=[
            TradeLeg(symbol="CALL", qty=0.1, side="sell"),
            TradeLeg(symbol="PUT", qty=0.1, side="sell"),
        ])

        result = router.open(trade)

        assert result.status == FillStatus.PENDING
        assert om.place_order.call_count == 2

    def test_open_no_orderbook_fails(self):
        om = _make_om()
        md = MagicMock()
        md.get_option_orderbook = MagicMock(return_value=None)
        router = Router(MagicMock(), MagicMock(), om, md)
        trade = _make_trade()

        result = router.open(trade)

        assert result.status in (FillStatus.REFUSED, FillStatus.FAILED)
        assert trade.state == TradeState.FAILED

    def test_open_stores_fill_manager(self):
        router, om, md = _make_router()
        trade = _make_trade()

        router.open(trade)

        mgr = trade.metadata.get("_open_fill_mgr")
        assert mgr is not None
        assert hasattr(mgr, "check")
        assert hasattr(mgr, "legs")


# =============================================================================
# open — auto mode detection
# =============================================================================

class TestModeDetection:
    def test_single_leg_defaults_to_limit(self):
        router, om, md = _make_router()
        trade = _make_trade(execution_mode=None)

        router.open(trade)

        assert trade.execution_mode == "limit"

    def test_multi_leg_low_notional_uses_limit(self):
        router, om, md = _make_router()
        trade = _make_trade(
            execution_mode=None,
            open_legs=[
                TradeLeg(symbol="C", qty=0.1, side="sell"),
                TradeLeg(symbol="P", qty=0.1, side="sell"),
            ],
        )
        # mark=0.0105, qty=0.1 each → notional ~1.05, well below 50000
        router.open(trade)
        assert trade.execution_mode == "limit"


# =============================================================================
# close — limit mode
# =============================================================================

class TestCloseLimit:
    def test_close_returns_pending(self):
        router, om, md = _make_router()
        trade = _make_trade(state=TradeState.OPEN, execution_mode="limit")
        trade.open_legs = [
            TradeLeg(symbol="SYM-C", qty=0.1, side="sell", fill_price=0.01, filled_qty=0.1),
        ]

        result = router.close(trade)

        assert isinstance(result, FillResult)
        assert result.status == FillStatus.PENDING
        assert trade.state == TradeState.CLOSING
        assert "_close_fill_mgr" in trade.metadata

    def test_close_builds_close_legs(self):
        router, om, md = _make_router()
        trade = _make_trade(state=TradeState.OPEN, execution_mode="limit")
        leg = TradeLeg(symbol="SYM-C", qty=0.1, side="sell", fill_price=0.01, filled_qty=0.1)
        trade.open_legs = [leg]

        router.close(trade)

        assert len(trade.close_legs) > 0
        assert trade.close_legs[0].symbol == "SYM-C"
        assert trade.close_legs[0].side == "buy"  # opposite of sell

    def test_close_cancels_existing_orders(self):
        om = _make_om()
        existing_rec = OrderRecord(
            order_id="OLD-1", client_order_id="0", lifecycle_id="T-001",
            leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
            symbol="SYM-C", side="buy", qty=0.1, price=0.01,
            status=OrderStatus.PENDING, placed_at=time.time(),
        )
        om.get_live_orders = MagicMock(return_value=[existing_rec])
        md = _make_md()
        router = Router(MagicMock(), MagicMock(), om, md)
        trade = _make_trade(state=TradeState.OPEN, execution_mode="limit")
        trade.open_legs = [
            TradeLeg(symbol="SYM-C", qty=0.1, side="sell", fill_price=0.01, filled_qty=0.1),
        ]

        router.close(trade)

        om.cancel_order.assert_called_with("OLD-1")


# =============================================================================
# close — circuit breaker
# =============================================================================

class TestCircuitBreaker:
    def test_fails_after_max_attempts(self):
        router, om, md = _make_router()
        trade = _make_trade(state=TradeState.OPEN, execution_mode="limit")
        trade.open_legs = [
            TradeLeg(symbol="SYM-C", qty=0.1, side="sell", fill_price=0.01, filled_qty=0.1),
        ]
        trade.metadata["_close_attempt_count"] = 10  # already at max

        result = router.close(trade)

        assert result.status == FillStatus.FAILED
        assert trade.state == TradeState.FAILED
        assert "manual intervention" in trade.error


# =============================================================================
# close — already fully closed legs
# =============================================================================

class TestCloseEdgeCases:
    def test_no_remaining_qty_transitions_to_closed(self):
        router, om, md = _make_router()
        trade = _make_trade(state=TradeState.OPEN, execution_mode="limit")
        # Open leg with 0.1 filled
        open_leg = TradeLeg(symbol="SYM-C", qty=0.1, side="sell", fill_price=0.01, filled_qty=0.1)
        trade.open_legs = [open_leg]
        # Close leg already fully filled from a previous attempt
        trade.close_legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy", fill_price=0.01, filled_qty=0.1)]

        result = router.close(trade)

        assert result.status == FillStatus.FILLED
        assert trade.state == TradeState.CLOSED


# =============================================================================
# open — RFQ mode
# =============================================================================

class TestOpenRFQ:
    def test_rfq_success_returns_filled(self):
        om = _make_om()
        md = _make_md()
        rfq_executor = MagicMock()
        rfq_result = MagicMock()
        rfq_result.success = True
        rfq_result.legs = [{"price": 0.01}]
        rfq_executor.execute = MagicMock(return_value=rfq_result)

        router = Router(MagicMock(), rfq_executor, om, md)
        trade = _make_trade(execution_mode="rfq")

        result = router.open(trade)

        assert result.status == FillStatus.FILLED
        assert trade.state == TradeState.OPEN
        assert trade.opened_at is not None

    def test_rfq_failure_with_fallback(self):
        om = _make_om()
        md = _make_md()
        rfq_executor = MagicMock()
        rfq_result = MagicMock()
        rfq_result.success = False
        rfq_result.message = "no quotes"
        rfq_executor.execute = MagicMock(return_value=rfq_result)

        router = Router(MagicMock(), rfq_executor, om, md)
        trade = _make_trade(execution_mode="rfq")
        rp = MagicMock()
        rp.fallback_mode = "limit"
        rp.timeout_seconds = 60
        rp.min_improvement_pct = -999
        trade.rfq_params = rp

        result = router.open(trade)

        # Should have fallen back to limit
        assert trade.execution_mode == "limit"
        assert result.status == FillStatus.PENDING

    def test_rfq_failure_no_fallback(self):
        om = _make_om()
        md = _make_md()
        rfq_executor = MagicMock()
        rfq_result = MagicMock()
        rfq_result.success = False
        rfq_result.message = "no quotes"
        rfq_executor.execute = MagicMock(return_value=rfq_result)

        router = Router(MagicMock(), rfq_executor, om, md)
        trade = _make_trade(execution_mode="rfq")
        rp = MagicMock()
        rp.fallback_mode = None
        rp.timeout_seconds = 60
        rp.min_improvement_pct = -999
        trade.rfq_params = rp

        result = router.open(trade)

        assert result.status == FillStatus.FAILED
        assert trade.state == TradeState.FAILED


# =============================================================================
# resolve_profile
# =============================================================================

class TestResolveProfile:
    def test_uses_profile_from_metadata(self):
        router, om, md = _make_router()
        profile = ExecutionProfile(
            name="custom",
            open_phases=[PhaseConfig(pricing="fair", duration_seconds=30)],
        )
        trade = _make_trade(metadata={"_execution_profile": profile})

        router.open(trade)

        mgr = trade.metadata["_open_fill_mgr"]
        assert mgr._profile.name == "custom"

    def test_falls_back_to_params_bridge(self):
        from trade_execution import ExecutionParams, ExecutionPhase

        router, om, md = _make_router()
        trade = _make_trade()
        trade.execution_params = ExecutionParams(phases=[
            ExecutionPhase(pricing="aggressive", duration_seconds=30),
        ])

        router.open(trade)

        mgr = trade.metadata["_open_fill_mgr"]
        assert mgr._profile.name == "_bridged"


# =============================================================================
# cancel_placed_orders
# =============================================================================

class TestCancelPlacedOrders:
    def test_cancels_orders_on_legs(self):
        router, om, md = _make_router()
        leg1 = MagicMock(order_id="ORD-1", is_filled=False)
        leg2 = MagicMock(order_id="ORD-2", is_filled=True)  # filled — skip

        router.cancel_placed_orders([leg1, leg2])

        om.cancel_order.assert_called_once_with("ORD-1")

    def test_skips_legs_without_order_id(self):
        router, om, md = _make_router()
        leg = MagicMock(spec=[])  # no order_id attribute

        router.cancel_placed_orders([leg])

        om.cancel_order.assert_not_called()
