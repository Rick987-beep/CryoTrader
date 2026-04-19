"""Tests for the kill switch (PositionCloser).

Verifies:
  - Exchange-agnostic side handling (Coincall int vs Deribit string)
  - reduce_only is always set
  - Lifecycle kill + OrderManager cancel + runner stop sequence
  - Phase transitions (mark → aggressive → finalize)
  - Fill detection for both exchanges
  - Telegram notifications sent at each stage
  - Price formatting (BTC vs USD)
  - No-position fast path
"""

import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call

import pytest


# ─── Helpers ────────────────────────────────────────────────────────────────

def _make_closer(exchange="deribit", positions=None):
    """Build a PositionCloser with mocked dependencies."""
    import position_closer as pc
    # Patch module-level exchange detection directly
    pc.EXCHANGE = exchange
    pc._IS_DERIBIT = exchange == "deribit"

    am = MagicMock()
    am.get_positions.return_value = positions or []

    executor = MagicMock()
    executor.place_order.return_value = {"orderId": "order-1"}
    executor.cancel_order.return_value = True
    executor.get_order_status.return_value = None

    lm = MagicMock()
    lm.kill_all.return_value = 3
    lm.order_manager.cancel_all.return_value = 2

    closer = pc.PositionCloser(
        account_manager=am,
        executor=executor,
        lifecycle_manager=lm,
    )
    return closer, am, executor, lm, pc


def _deribit_position(symbol="BTC-25APR26-90000-P", qty=-0.5, mark_btc=0.012):
    """Fake Deribit position dict."""
    return {
        "symbol": symbol,
        "qty": qty,
        "trade_side": 2,         # sell → closer needs to buy
        "mark_price": mark_btc * 85000,  # USD (unused — BTC-native preferred)
        "_mark_price_btc": mark_btc,
    }


def _coincall_position(symbol="BTCUSD-25APR26-90000-P", qty=-1, mark_usd=320.0):
    """Fake Coincall position dict."""
    return {
        "symbol": symbol,
        "qty": qty,
        "trade_side": 2,
        "mark_price": mark_usd,
    }


# ─── Unit tests: _build_legs ────────────────────────────────────────────────

class TestBuildLegs:

    def test_deribit_uses_btc_mark(self):
        closer, *_, pc = _make_closer("deribit", [_deribit_position(mark_btc=0.015)])
        legs = closer._build_legs(closer._am.get_positions())
        assert len(legs) == 1
        assert legs[0].mark_price == pytest.approx(0.015)
        assert legs[0].close_side == "buy"      # closing a sell
        assert legs[0].qty == 0.5

    def test_coincall_uses_usd_mark(self):
        closer, *_, pc = _make_closer("coincall", [_coincall_position(mark_usd=320.0)])
        legs = closer._build_legs(closer._am.get_positions())
        assert legs[0].mark_price == pytest.approx(320.0)

    def test_long_position_closed_with_sell(self):
        pos = _deribit_position()
        pos["trade_side"] = 1  # buy → closer sells
        pos["qty"] = 1.0
        closer, *_, pc = _make_closer("deribit", [pos])
        legs = closer._build_legs(closer._am.get_positions())
        assert legs[0].close_side == "sell"
        assert legs[0].qty == 1.0


# ─── Unit tests: side handling in _place_or_reprice ─────────────────────────

class TestSideHandling:

    def test_deribit_passes_string_side(self):
        closer, _, executor, _, pc = _make_closer("deribit", [_deribit_position()])
        leg = pc._CloseLeg(symbol="BTC-X", qty=0.5, close_side="buy", mark_price=0.01)
        closer._place_or_reprice(leg, 0.01)
        _, kwargs = executor.place_order.call_args
        assert kwargs["side"] == "buy"

    def test_coincall_passes_int_side_buy(self):
        closer, _, executor, _, pc = _make_closer("coincall", [_coincall_position()])
        leg = pc._CloseLeg(symbol="BTC-X", qty=1, close_side="buy", mark_price=300)
        closer._place_or_reprice(leg, 300)
        _, kwargs = executor.place_order.call_args
        assert kwargs["side"] == 1    # buy = 1

    def test_coincall_passes_int_side_sell(self):
        closer, _, executor, _, pc = _make_closer("coincall", [_coincall_position()])
        leg = pc._CloseLeg(symbol="BTC-X", qty=1, close_side="sell", mark_price=300)
        closer._place_or_reprice(leg, 300)
        _, kwargs = executor.place_order.call_args
        assert kwargs["side"] == 2    # sell = 2

    def test_reduce_only_always_set(self):
        closer, _, executor, _, pc = _make_closer("deribit", [_deribit_position()])
        leg = pc._CloseLeg(symbol="BTC-X", qty=0.5, close_side="buy", mark_price=0.01)
        closer._place_or_reprice(leg, 0.01)
        _, kwargs = executor.place_order.call_args
        assert kwargs["reduce_only"] is True


# ─── Unit tests: fill detection ─────────────────────────────────────────────

class TestFillDetection:

    def test_deribit_filled_state_string(self):
        closer, _, executor, _, pc = _make_closer("deribit")
        executor.get_order_status.return_value = {
            "state": "filled", "fillQty": 0.5, "avgPrice": 0.012,
        }
        leg = pc._CloseLeg(symbol="BTC-X", qty=0.5, close_side="buy",
                           mark_price=0.01, order_id="ord-1")
        closer._check_fills([leg])
        assert leg.filled is True
        assert leg.fill_price == pytest.approx(0.012)

    def test_coincall_filled_state_int(self):
        closer, _, executor, _, pc = _make_closer("coincall")
        executor.get_order_status.return_value = {
            "state": 1, "fillQty": 1.0, "avgPrice": 310.0,
        }
        leg = pc._CloseLeg(symbol="BTC-X", qty=1, close_side="buy",
                           mark_price=300, order_id="ord-1")
        closer._check_fills([leg])
        assert leg.filled is True
        assert leg.fill_price == pytest.approx(310.0)

    def test_fillqty_fallback(self):
        """If state is not conclusive, fillQty >= qty marks filled."""
        closer, _, executor, _, pc = _make_closer("deribit")
        executor.get_order_status.return_value = {
            "state": "open", "fillQty": 0.5, "avgPrice": 0.011,
        }
        leg = pc._CloseLeg(symbol="BTC-X", qty=0.5, close_side="buy",
                           mark_price=0.01, order_id="ord-1")
        closer._check_fills([leg])
        assert leg.filled is True

    def test_skips_already_filled(self):
        closer, _, executor, _, pc = _make_closer("deribit")
        leg = pc._CloseLeg(symbol="BTC-X", qty=0.5, close_side="buy",
                           mark_price=0.01, filled=True)
        closer._check_fills([leg])
        executor.get_order_status.assert_not_called()


# ─── Unit tests: aggressive pricing ────────────────────────────────────────

class TestAggressivePricing:

    def test_buy_adds_discount(self):
        closer, *_, pc = _make_closer("deribit")
        leg = pc._CloseLeg(symbol="X", qty=1, close_side="buy", mark_price=0.010)
        price = closer._aggressive_price(leg)
        assert price == pytest.approx(0.011)   # 10% above

    def test_sell_subtracts_discount(self):
        closer, *_, pc = _make_closer("deribit")
        leg = pc._CloseLeg(symbol="X", qty=1, close_side="sell", mark_price=0.010)
        price = closer._aggressive_price(leg)
        assert price == pytest.approx(0.009)   # 10% below


# ─── Unit tests: price formatting ──────────────────────────────────────────

class TestPriceFormatting:

    def test_deribit_btc_format(self):
        _make_closer("deribit")
        import position_closer as pc
        assert "BTC" in pc._fmt_price(0.012345)

    def test_coincall_usd_format(self):
        _make_closer("coincall")
        import position_closer as pc
        assert "$" in pc._fmt_price(320.50)


# ─── Integration: _run flow ────────────────────────────────────────────────

class TestRunFlow:

    def test_no_positions_completes_fast(self):
        closer, am, _, lm, pc = _make_closer("deribit")
        am.get_positions.return_value = []
        runners = [MagicMock(), MagicMock()]

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.os.kill"):
            mock_notif.return_value = MagicMock()
            closer._run(runners)

        assert closer.status == "done"
        lm.kill_all.assert_called_once()
        lm.order_manager.cancel_all.assert_called_once()
        for r in runners:
            r.stop.assert_called_once()

    def test_kill_sequence_order(self):
        """Verify: kill_all → cancel_all → stop runners → fetch positions."""
        closer, am, _, lm, pc = _make_closer("deribit")
        am.get_positions.return_value = []

        call_log = []
        lm.kill_all.side_effect = lambda: (call_log.append("kill_all"), 0)[1]
        lm.order_manager.cancel_all.side_effect = lambda: (call_log.append("cancel_all"), 0)[1]

        runner = MagicMock()
        runner.stop.side_effect = lambda: call_log.append("runner_stop")

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.os.kill"), \
             patch("position_closer.time.sleep"):
            mock_notif.return_value = MagicMock()
            closer._run([runner])

        assert call_log == ["kill_all", "cancel_all", "runner_stop"]

    def test_telegram_sent_on_activation(self):
        closer, am, _, _, pc = _make_closer("deribit")
        am.get_positions.return_value = []

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.os.kill"):
            notifier = MagicMock()
            mock_notif.return_value = notifier
            closer._run([])

        # At least 2 sends: activation + completion
        assert notifier.send.call_count >= 2
        first_msg = notifier.send.call_args_list[0][0][0]
        assert "KILL SWITCH" in first_msg

    def test_start_returns_false_if_running(self):
        closer, *_ = _make_closer("deribit")
        closer._running = True
        assert closer.start([]) is False

    def test_error_sets_status(self):
        closer, am, _, lm, pc = _make_closer("deribit")
        lm.kill_all.side_effect = RuntimeError("test boom")

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.os.kill"):
            mock_notif.return_value = MagicMock()
            closer._run([])

        assert "error" in closer.status
        assert "test boom" in closer.status


# ─── Integration: finalize ──────────────────────────────────────────────────

class TestFinalize:

    def test_finalize_cancels_unfilled_orders(self):
        closer, am, executor, _, pc = _make_closer("deribit")
        am.get_positions.return_value = []  # all closed after finalize

        unfilled_leg = pc._CloseLeg(
            symbol="BTC-X", qty=0.5, close_side="buy",
            mark_price=0.01, order_id="leftover-order",
        )
        filled_leg = pc._CloseLeg(
            symbol="BTC-Y", qty=1.0, close_side="sell",
            mark_price=0.02, filled=True, fill_price=0.019,
        )

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.time.sleep"):
            mock_notif.return_value = MagicMock()
            closer._finalize([filled_leg, unfilled_leg], time.time(), 2, 1)

        executor.cancel_order.assert_called_with("leftover-order")
        assert closer.status == "done"

    def test_finalize_warns_when_positions_remain(self):
        closer, am, executor, _, pc = _make_closer("deribit")
        # Exchange still shows a position after finalize
        am.get_positions.return_value = [_deribit_position()]

        leg = pc._CloseLeg(
            symbol="BTC-X", qty=0.5, close_side="buy",
            mark_price=0.01, order_id="stuck",
        )

        with patch.object(pc, "get_notifier") as mock_notif, \
             patch("position_closer.time.sleep"):
            notifier = MagicMock()
            mock_notif.return_value = notifier
            closer._finalize([leg], time.time(), 1, 1)

        assert "still open" in closer.status
        summary_msg = notifier.send.call_args[0][0]
        assert "WARNING" in summary_msg
