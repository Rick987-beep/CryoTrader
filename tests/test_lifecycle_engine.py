"""
Unit tests for LifecycleEngine state machine.

Patches ExecutionRouter and OrderManager to test state transitions
without exchange calls.
"""

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState, ExitCondition
from trade_execution import ExecutionParams, LimitFillManager
from tests.conftest import MockExecutor, MockMarketData, make_account


def make_engine(**kwargs):
    """Create a LifecycleEngine with fully-mocked dependencies."""
    with patch("lifecycle_engine.ExecutionRouter") as MockRouter, \
         patch("lifecycle_engine.OrderManager") as MockOM:
        mock_router_inst = MockRouter.return_value
        mock_om_inst = MockOM.return_value
        mock_om_inst.poll_all = MagicMock()
        mock_om_inst.persist_snapshot = MagicMock()
        mock_om_inst.has_live_orders = MagicMock(return_value=False)
        mock_om_inst.cancel_all_for = MagicMock()

        from lifecycle_engine import LifecycleEngine
        engine = LifecycleEngine(
            executor=MockExecutor(),
            rfq_executor=MagicMock(),
            market_data=MockMarketData(),
            **kwargs,
        )
        return engine, mock_router_inst, mock_om_inst


# =============================================================================
# Trade creation
# =============================================================================

class TestCreate:
    def test_creates_trade_in_pending_open(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        assert trade.state == TradeState.PENDING_OPEN
        assert trade.id in [t.id for t in engine.all_trades]

    def test_create_with_strategy_id(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs, strategy_id="test_strat")
        assert trade.strategy_id == "test_strat"

    def test_create_with_exit_conditions(self):
        engine, router, om = make_engine()
        exit_cond = MagicMock(return_value=False)
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs, exit_conditions=[exit_cond])
        assert len(trade.exit_conditions) == 1

    def test_create_with_metadata(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs, metadata={"key": "val"})
        assert trade.metadata["key"] == "val"


# =============================================================================
# Open
# =============================================================================

class TestOpen:
    def test_open_routes_to_router(self):
        engine, router, om = make_engine()
        router.open.return_value = True
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        result = engine.open(trade.id)
        assert result is True
        router.open.assert_called_once_with(trade)

    def test_open_fails_if_not_pending_open(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.OPEN  # wrong state
        result = engine.open(trade.id)
        assert result is False
        router.open.assert_not_called()

    def test_open_fails_for_unknown_trade(self):
        engine, router, om = make_engine()
        result = engine.open("nonexistent")
        assert result is False


# =============================================================================
# Close
# =============================================================================

class TestClose:
    def test_close_routes_to_router(self):
        engine, router, om = make_engine()
        router.close.return_value = True
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.OPEN
        result = engine.close(trade.id)
        router.close.assert_called_once_with(trade)

    def test_close_allowed_from_pending_close(self):
        engine, router, om = make_engine()
        router.close.return_value = True
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.PENDING_CLOSE
        result = engine.close(trade.id)
        assert result is True

    def test_close_fails_from_wrong_state(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.PENDING_OPEN
        result = engine.close(trade.id)
        assert result is False


# =============================================================================
# active_trades / get / get_trades_for_strategy
# =============================================================================

class TestTradeAccess:
    def test_active_trades_excludes_closed(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        t1 = engine.create(legs=legs)
        t2 = engine.create(legs=legs)
        t2.state = TradeState.CLOSED
        active = engine.active_trades
        assert len(active) == 1
        assert active[0].id == t1.id

    def test_get_by_id(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        assert engine.get(trade.id) is trade
        assert engine.get("nope") is None

    def test_get_trades_for_strategy(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        t1 = engine.create(legs=legs, strategy_id="strat_a")
        t2 = engine.create(legs=legs, strategy_id="strat_b")
        result = engine.get_trades_for_strategy("strat_a")
        assert len(result) == 1
        assert result[0].id == t1.id

    def test_active_trades_for_strategy(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        t1 = engine.create(legs=legs, strategy_id="s")
        t2 = engine.create(legs=legs, strategy_id="s")
        t2.state = TradeState.CLOSED
        result = engine.active_trades_for_strategy("s")
        assert len(result) == 1


# =============================================================================
# restore_trade
# =============================================================================

class TestRestore:
    def test_restore_adds_trade(self):
        engine, router, om = make_engine()
        trade = TradeLifecycle(
            open_legs=[TradeLeg(symbol="SYM-C", qty=0.1, side="buy")],
        )
        engine.restore_trade(trade)
        assert engine.get(trade.id) is trade


# =============================================================================
# Tick — state machine advancement
# =============================================================================

class TestTick:
    def test_tick_calls_poll_all(self):
        engine, router, om = make_engine()
        account = make_account()
        engine.tick(account)
        om.poll_all.assert_called_once()

    def test_tick_opening_calls_check_open_fills(self):
        engine, router, om = make_engine()
        router.open.return_value = True
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.OPENING

        # Set up a mock fill manager
        mock_mgr = MagicMock()
        mock_mgr.check.return_value = "filled"
        mock_mgr.filled_legs = [MagicMock(filled_qty=0.1, fill_price=100.0, order_id="1001")]
        trade.metadata["_open_fill_mgr"] = mock_mgr

        engine.tick(make_account())
        mock_mgr.check.assert_called_once()
        assert trade.state == TradeState.OPEN

    def test_tick_open_evaluates_exit_conditions(self):
        engine, router, om = make_engine()
        exit_cond = MagicMock(return_value=True)
        exit_cond.__name__ = "test_exit"
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs, exit_conditions=[exit_cond])
        trade.state = TradeState.OPEN
        trade.opened_at = time.time()

        # Patch _is_trade_expired to return False
        with patch.object(engine, '_is_trade_expired', return_value=False):
            engine.tick(make_account())

        assert trade.state == TradeState.PENDING_CLOSE or trade.state == TradeState.CLOSING

    def test_tick_pending_close_places_close_orders(self):
        engine, router, om = make_engine()
        router.close.return_value = True
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.PENDING_CLOSE
        om.has_live_orders.return_value = False

        engine.tick(make_account())
        router.close.assert_called_once_with(trade)

    def test_tick_pending_close_skips_when_live_orders_exist(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.PENDING_CLOSE
        om.has_live_orders.return_value = True

        engine.tick(make_account())
        router.close.assert_not_called()

    def test_tick_closing_checks_close_fills(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy", fill_price=100.0, filled_qty=0.1)]
        trade = engine.create(legs=legs)
        trade.state = TradeState.CLOSING
        trade.opened_at = time.time()

        mock_mgr = MagicMock()
        mock_mgr.check.return_value = "filled"
        mock_mgr.has_skipped_legs = False
        mock_mgr.filled_legs = [MagicMock(
            symbol="SYM-C", filled_qty=0.1, fill_price=105.0, order_id="2001"
        )]
        mock_mgr.skipped_symbols = []
        trade.close_legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="sell")]
        trade.metadata["_close_fill_mgr"] = mock_mgr

        with patch.object(engine, '_is_trade_expired', return_value=False):
            engine.tick(make_account())

        assert trade.state == TradeState.CLOSED


# =============================================================================
# force_close
# =============================================================================

class TestForceClose:
    def test_force_close_from_open(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.OPEN
        result = engine.force_close(trade.id)
        assert result is True
        assert trade.state == TradeState.PENDING_CLOSE

    def test_force_close_unknown_trade(self):
        engine, router, om = make_engine()
        assert engine.force_close("nope") is False

    def test_force_close_already_closed(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        trade.state = TradeState.CLOSED
        assert engine.force_close(trade.id) is False


# =============================================================================
# cancel
# =============================================================================

class TestCancel:
    def test_cancel_pending_trade(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        trade = engine.create(legs=legs)
        assert trade.state == TradeState.PENDING_OPEN
        result = engine.cancel(trade.id)
        assert result is True
        assert trade.state == TradeState.FAILED


# =============================================================================
# kill_all
# =============================================================================

class TestKillAll:
    def test_kills_active_trades(self):
        engine, router, om = make_engine()
        legs = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        t1 = engine.create(legs=legs)
        t2 = engine.create(legs=legs)
        t1.state = TradeState.OPEN
        t2.state = TradeState.OPENING
        killed = engine.kill_all()
        assert killed == 2
