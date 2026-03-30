"""
Unit tests for entry and exit condition factories.

All conditions are pure functions — no network, no exchange calls.
They take AccountSnapshot/TradeLifecycle and return bool.
"""

import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

from account_manager import AccountSnapshot, PositionSnapshot
from trade_lifecycle import TradeLifecycle, TradeLeg, TradeState
from strategy import (
    time_window, weekday_filter, min_equity, min_available_margin_pct,
    max_account_delta, max_margin_utilization, no_existing_position_in,
    utc_time_window,
    profit_target, max_loss, max_hold_hours, time_exit,
    utc_datetime_exit, account_delta_limit, structure_delta_limit,
    leg_greek_limit,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _account(**kwargs):
    defaults = dict(
        equity=10000.0, available_margin=8000.0,
        initial_margin=2000.0, maintenance_margin=1000.0,
        unrealized_pnl=0.0, margin_utilization=20.0,
        positions=(), net_delta=0.5,
        net_gamma=0.01, net_theta=-0.5, net_vega=0.1,
        timestamp=time.time(),
    )
    defaults.update(kwargs)
    return AccountSnapshot(**defaults)


def _position(symbol, **kwargs):
    defaults = dict(
        position_id="pos-1",
        qty=0.1, side="long", entry_price=500.0, mark_price=510.0,
        unrealized_pnl=1.0, roi=0.02, delta=0.5, gamma=0.001,
        theta=-0.05, vega=0.1,
    )
    defaults.update(kwargs)
    return PositionSnapshot(symbol=symbol, **defaults)


def _trade_with_legs(fill_price=100.0, filled_qty=0.5, side="sell",
                     opened_at=None, market_data=None):
    t = TradeLifecycle(
        state=TradeState.OPEN,
        opened_at=opened_at or time.time(),
        open_legs=[
            TradeLeg(symbol="A", qty=0.5, side=side,
                     fill_price=fill_price, filled_qty=filled_qty),
        ],
    )
    t._market_data = market_data
    return t


# ═════════════════════════════════════════════════════════════════════════
# Entry Conditions
# ═════════════════════════════════════════════════════════════════════════

class TestTimeWindow:
    @patch("strategy.datetime")
    def test_within_window(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 28, 10, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = time_window(8, 12)
        assert cond(_account()) is True

    @patch("strategy.datetime")
    def test_outside_window(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 28, 14, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = time_window(8, 12)
        assert cond(_account()) is False

    @patch("strategy.datetime")
    def test_wrap_past_midnight(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 28, 23, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = time_window(22, 6)
        assert cond(_account()) is True


class TestWeekdayFilter:
    @patch("strategy.datetime")
    def test_allowed_day(self, mock_dt):
        # Saturday = weekday 5
        mock_dt.now.return_value = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = weekday_filter(["sat", "sun"])
        assert cond(_account()) is True

    @patch("strategy.datetime")
    def test_blocked_day(self, mock_dt):
        # Saturday = weekday 5
        mock_dt.now.return_value = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = weekday_filter(["mon", "tue"])
        assert cond(_account()) is False

    def test_invalid_day_raises(self):
        with pytest.raises(ValueError):
            weekday_filter(["xyz"])


class TestMinEquity:
    def test_above_threshold(self):
        assert min_equity(5000)(_account(equity=10000)) is True

    def test_below_threshold(self):
        assert min_equity(15000)(_account(equity=10000)) is False


class TestMinAvailableMarginPct:
    def test_sufficient_margin(self):
        cond = min_available_margin_pct(50)
        assert cond(_account(equity=10000, available_margin=8000)) is True

    def test_insufficient_margin(self):
        cond = min_available_margin_pct(90)
        assert cond(_account(equity=10000, available_margin=5000)) is False

    def test_zero_equity(self):
        cond = min_available_margin_pct(50)
        assert cond(_account(equity=0)) is False


class TestMaxAccountDelta:
    def test_within_threshold(self):
        assert max_account_delta(1.0)(_account(net_delta=0.5)) is True

    def test_exceeds_threshold(self):
        assert max_account_delta(0.3)(_account(net_delta=0.5)) is False

    def test_negative_delta(self):
        assert max_account_delta(1.0)(_account(net_delta=-0.8)) is True


class TestMaxMarginUtilization:
    def test_within_threshold(self):
        assert max_margin_utilization(50)(_account(margin_utilization=20)) is True

    def test_exceeds_threshold(self):
        assert max_margin_utilization(15)(_account(margin_utilization=20)) is False


class TestNoExistingPosition:
    def test_no_positions(self):
        cond = no_existing_position_in(["BTCUSD-28MAR26-100000-C"])
        assert cond(_account(positions=())) is True

    def test_has_position_blocks(self):
        pos = _position("BTCUSD-28MAR26-100000-C")
        cond = no_existing_position_in(["BTCUSD-28MAR26-100000-C"])
        assert cond(_account(positions=(pos,))) is False


class TestUtcTimeWindow:
    def test_within_window(self):
        now = datetime.now(timezone.utc)
        start = now - timedelta(minutes=5)
        end = now + timedelta(minutes=5)
        assert utc_time_window(start, end)(_account()) is True

    def test_outside_window(self):
        now = datetime.now(timezone.utc)
        start = now + timedelta(hours=1)
        end = now + timedelta(hours=2)
        assert utc_time_window(start, end)(_account()) is False


# ═════════════════════════════════════════════════════════════════════════
# Exit Conditions
# ═════════════════════════════════════════════════════════════════════════

class TestProfitTarget:
    def test_triggers_above_threshold(self):
        # sell 0.5 at 100 → entry_cost = -50
        # position unrealized_pnl = 40 (80% of |50|)
        pos = _position("A", qty=0.5, unrealized_pnl=40.0)
        account = _account(positions=(pos,))
        trade = _trade_with_legs(fill_price=100.0, filled_qty=0.5, side="sell")
        cond = profit_target(50, pnl_mode="mark")
        assert cond(account, trade) is True

    def test_does_not_trigger_below_threshold(self):
        pos = _position("A", qty=0.5, unrealized_pnl=10.0)
        account = _account(positions=(pos,))
        trade = _trade_with_legs(fill_price=100.0, filled_qty=0.5, side="sell")
        cond = profit_target(50, pnl_mode="mark")
        assert cond(account, trade) is False

    def test_zero_entry_cost(self):
        account = _account()
        trade = TradeLifecycle(state=TradeState.OPEN, open_legs=[])
        cond = profit_target(50)
        assert cond(account, trade) is False


class TestMaxLoss:
    def test_triggers_on_loss(self):
        pos = _position("A", qty=0.5, unrealized_pnl=-40.0)
        account = _account(positions=(pos,))
        trade = _trade_with_legs(fill_price=100.0, filled_qty=0.5, side="sell")
        cond = max_loss(50, pnl_mode="mark")
        assert cond(account, trade) is True

    def test_does_not_trigger_small_loss(self):
        pos = _position("A", qty=0.5, unrealized_pnl=-10.0)
        account = _account(positions=(pos,))
        trade = _trade_with_legs(fill_price=100.0, filled_qty=0.5, side="sell")
        cond = max_loss(50, pnl_mode="mark")
        assert cond(account, trade) is False


class TestMaxHoldHours:
    def test_triggers_after_hours(self):
        trade = _trade_with_legs(opened_at=time.time() - 7200)  # 2 hours ago
        cond = max_hold_hours(1)
        assert cond(_account(), trade) is True

    def test_does_not_trigger_before_hours(self):
        trade = _trade_with_legs(opened_at=time.time() - 1800)  # 30 min ago
        cond = max_hold_hours(1)
        assert cond(_account(), trade) is False

    def test_returns_false_if_not_opened(self):
        trade = TradeLifecycle()
        cond = max_hold_hours(1)
        assert cond(_account(), trade) is False


class TestTimeExit:
    @patch("strategy.datetime")
    def test_triggers_after_cutoff(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 28, 19, 30, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = time_exit(19, 0)
        assert cond(_account(), _trade_with_legs()) is True

    @patch("strategy.datetime")
    def test_does_not_trigger_before_cutoff(self, mock_dt):
        mock_dt.now.return_value = datetime(2026, 3, 28, 18, 30, tzinfo=timezone.utc)
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        cond = time_exit(19, 0)
        assert cond(_account(), _trade_with_legs()) is False


class TestUtcDatetimeExit:
    def test_triggers_past_datetime(self):
        dt = datetime.now(timezone.utc) - timedelta(minutes=5)
        cond = utc_datetime_exit(dt)
        assert cond(_account(), _trade_with_legs()) is True

    def test_does_not_trigger_future(self):
        dt = datetime.now(timezone.utc) + timedelta(hours=1)
        cond = utc_datetime_exit(dt)
        assert cond(_account(), _trade_with_legs()) is False


class TestAccountDeltaLimit:
    def test_triggers_above_threshold(self):
        cond = account_delta_limit(0.3)
        assert cond(_account(net_delta=0.5), _trade_with_legs()) is True

    def test_does_not_trigger_within_threshold(self):
        cond = account_delta_limit(1.0)
        assert cond(_account(net_delta=0.5), _trade_with_legs()) is False


class TestStructureDeltaLimit:
    def test_triggers_above_threshold(self):
        pos = _position("A", qty=0.5, delta=0.8)
        account = _account(positions=(pos,))
        trade = _trade_with_legs()
        cond = structure_delta_limit(0.5)
        assert cond(account, trade) is True

    def test_does_not_trigger_within_threshold(self):
        pos = _position("A", qty=0.5, delta=0.3)
        account = _account(positions=(pos,))
        trade = _trade_with_legs()
        cond = structure_delta_limit(0.5)
        assert cond(account, trade) is False


class TestLegGreekLimit:
    def test_delta_exceeds_threshold(self):
        pos = _position("A", delta=0.8)
        account = _account(positions=(pos,))
        trade = _trade_with_legs()
        cond = leg_greek_limit(0, "delta", ">", 0.5)
        assert cond(account, trade) is True

    def test_theta_below_threshold(self):
        pos = _position("A", theta=-10.0)
        account = _account(positions=(pos,))
        trade = _trade_with_legs()
        cond = leg_greek_limit(0, "theta", "<", -5.0)
        assert cond(account, trade) is True

    def test_invalid_leg_index(self):
        cond = leg_greek_limit(5, "delta", ">", 0.5)
        assert cond(_account(), _trade_with_legs()) is False

    def test_no_position_returns_false(self):
        account = _account(positions=())
        trade = _trade_with_legs()
        cond = leg_greek_limit(0, "delta", ">", 0.5)
        assert cond(account, trade) is False
