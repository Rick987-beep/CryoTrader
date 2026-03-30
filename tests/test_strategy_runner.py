"""
Unit tests for StrategyRunner — entry gating, cooldown, callbacks.

All dependencies are mocked — no network calls.
"""

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState
from trade_execution import ExecutionParams
from tests.conftest import make_account


def make_ctx():
    """Create a mock TradingContext with minimal wiring."""
    ctx = MagicMock()
    ctx.lifecycle_manager.active_trades_for_strategy.return_value = []
    ctx.lifecycle_manager.get_trades_for_strategy.return_value = []
    ctx.lifecycle_manager.create.return_value = TradeLifecycle(
        open_legs=[TradeLeg(symbol="SYM-C", qty=0.1, side="buy")],
        strategy_id="test",
    )
    ctx.lifecycle_manager.open.return_value = True
    ctx.market_data = MagicMock()
    ctx.auth = MagicMock(spec=[])  # no 'reachable' attr
    return ctx


def make_runner(ctx=None, **config_kwargs):
    """Create a StrategyRunner with a StrategyConfig."""
    from strategy import StrategyConfig, StrategyRunner
    from option_selection import LegSpec

    ctx = ctx or make_ctx()
    defaults = dict(
        name="test_strat",
        legs=[LegSpec(option_type="C", side="buy", qty=0.1,
                     strike_criteria={"type": "delta", "value": 0.25},
                     expiry_criteria={"dte": "next"})],
        check_interval_seconds=0,
    )
    defaults.update(config_kwargs)
    config = StrategyConfig(**defaults)
    runner = StrategyRunner(config, ctx)
    return runner, ctx


# =============================================================================
# Properties
# =============================================================================

class TestProperties:
    def test_strategy_id(self):
        runner, _ = make_runner()
        assert runner.strategy_id == "test_strat"

    def test_active_trades_delegates_to_engine(self):
        runner, ctx = make_runner()
        runner.active_trades
        ctx.lifecycle_manager.active_trades_for_strategy.assert_called_with("test_strat")

    def test_all_trades_delegates_to_engine(self):
        runner, ctx = make_runner()
        runner.all_trades
        ctx.lifecycle_manager.get_trades_for_strategy.assert_called_with("test_strat")


# =============================================================================
# Entry gates
# =============================================================================

class TestEntryGates:
    @patch("strategy.resolve_legs")
    def test_opens_when_all_gates_pass(self, mock_resolve):
        mock_resolve.return_value = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        runner, ctx = make_runner()
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_called_once()
        ctx.lifecycle_manager.open.assert_called_once()

    @patch("strategy.resolve_legs")
    def test_blocked_by_max_concurrent(self, mock_resolve):
        runner, ctx = make_runner(max_concurrent_trades=1)
        # Simulate 1 active trade
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = [MagicMock()]
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_blocked_by_cooldown(self, mock_resolve):
        runner, ctx = make_runner(cooldown_seconds=3600)
        # Simulate a recent trade
        recent_trade = MagicMock()
        recent_trade.created_at = time.time()  # just now
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [recent_trade]
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_allowed_after_cooldown_expires(self, mock_resolve):
        mock_resolve.return_value = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        runner, ctx = make_runner(cooldown_seconds=1)
        old_trade = MagicMock()
        old_trade.created_at = time.time() - 100
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [old_trade]
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_called_once()

    @patch("strategy.resolve_legs")
    def test_blocked_by_max_trades_per_day(self, mock_resolve):
        runner, ctx = make_runner(max_trades_per_day=1)
        trade = MagicMock()
        trade.created_at = time.time()  # today
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [trade]
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_blocked_by_entry_condition(self, mock_resolve):
        def always_false(account):
            return False
        runner, ctx = make_runner(entry_conditions=[always_false])
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_entry_condition_error_blocks(self, mock_resolve):
        def bad_cond(account):
            raise RuntimeError("oops")
        runner, ctx = make_runner(entry_conditions=[bad_cond])
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()


# =============================================================================
# Tick throttling
# =============================================================================

class TestThrottling:
    @patch("strategy.resolve_legs")
    def test_throttled_by_check_interval(self, mock_resolve):
        mock_resolve.return_value = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        runner, ctx = make_runner(check_interval_seconds=3600)
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_called_once()

        # Second tick within interval — no new trade
        ctx.lifecycle_manager.create.reset_mock()
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()


# =============================================================================
# Enable / disable / stop
# =============================================================================

class TestControls:
    @patch("strategy.resolve_legs")
    def test_disable_prevents_entry(self, mock_resolve):
        runner, ctx = make_runner()
        runner.disable()
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_enable_resumes_entry(self, mock_resolve):
        mock_resolve.return_value = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        runner, ctx = make_runner()
        runner.disable()
        runner.enable()
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_called_once()

    def test_stop_force_closes_active(self):
        runner, ctx = make_runner()
        active = [MagicMock(id="t1"), MagicMock(id="t2")]
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = active
        runner.stop()
        assert not runner._enabled
        assert ctx.lifecycle_manager.force_close.call_count == 2


# =============================================================================
# Callbacks
# =============================================================================

class TestCallbacks:
    def test_on_trade_closed_fires_for_closed_trade(self):
        callback = MagicMock()
        runner, ctx = make_runner(on_trade_closed=callback)

        closed_trade = MagicMock()
        closed_trade.id = "t1"
        closed_trade.state = TradeState.CLOSED
        closed_trade.realized_pnl = 0.05
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [closed_trade]

        runner.tick(make_account())
        callback.assert_called_once()
        call_args = callback.call_args[0]
        assert call_args[0] is closed_trade

    def test_on_trade_closed_fires_only_once(self):
        callback = MagicMock()
        runner, ctx = make_runner(on_trade_closed=callback)

        closed_trade = MagicMock()
        closed_trade.id = "t1"
        closed_trade.state = TradeState.CLOSED
        closed_trade.realized_pnl = 0.05
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [closed_trade]

        runner.tick(make_account())
        runner.tick(make_account())
        callback.assert_called_once()

    def test_on_trade_opened_fires_for_open_trade(self):
        callback = MagicMock()
        runner, ctx = make_runner(on_trade_opened=callback)

        open_trade = MagicMock()
        open_trade.id = "t2"
        open_trade.state = TradeState.OPEN
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [open_trade]

        runner.tick(make_account())
        callback.assert_called_once()

    def test_callback_error_does_not_crash(self):
        callback = MagicMock(side_effect=RuntimeError("boom"))
        runner, ctx = make_runner(on_trade_closed=callback)

        closed_trade = MagicMock()
        closed_trade.id = "t1"
        closed_trade.state = TradeState.CLOSED
        closed_trade.realized_pnl = 0.0
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [closed_trade]

        runner.tick(make_account())  # should not raise


# =============================================================================
# is_done
# =============================================================================

class TestIsDone:
    def test_not_done_when_unlimited(self):
        runner, ctx = make_runner(max_trades_per_day=0)
        assert not runner.is_done

    def test_not_done_with_active_trades(self):
        runner, ctx = make_runner(max_trades_per_day=1)
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = [MagicMock()]
        assert not runner.is_done

    def test_done_when_quota_exhausted(self):
        runner, ctx = make_runner(max_trades_per_day=1)
        ctx.lifecycle_manager.active_trades_for_strategy.return_value = []
        trade = MagicMock()
        trade.created_at = time.time()  # today
        ctx.lifecycle_manager.get_trades_for_strategy.return_value = [trade]
        assert runner.is_done


# =============================================================================
# Exchange unreachable guard
# =============================================================================

class TestUnreachableGuard:
    @patch("strategy.resolve_legs")
    def test_skips_entry_when_unreachable(self, mock_resolve):
        runner, ctx = make_runner()
        # Add reachable attribute
        ctx.auth.reachable = False
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_not_called()

    @patch("strategy.resolve_legs")
    def test_allows_entry_when_reachable(self, mock_resolve):
        mock_resolve.return_value = [TradeLeg(symbol="SYM-C", qty=0.1, side="buy")]
        runner, ctx = make_runner()
        ctx.auth.reachable = True
        runner.tick(make_account())
        ctx.lifecycle_manager.create.assert_called_once()
