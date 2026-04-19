"""
Unit tests for reconciliation logic and Telegram alerts.

Migrated from test_phase3_hardening.py — tests reconciliation wiring
in LifecycleEngine, orphan detection, and notification formatting.
All mocked — no network calls.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from order_manager import OrderManager, OrderRecord, OrderPurpose, OrderStatus
from lifecycle_engine import LifecycleEngine
from telegram_notifier import TelegramNotifier
from account_manager import AccountSnapshot


# ── Helpers ──────────────────────────────────────────────────────────────

def fake_account(**kwargs) -> AccountSnapshot:
    defaults = dict(
        equity=10000.0, available_margin=8000.0, initial_margin=2000.0,
        maintenance_margin=1000.0, unrealized_pnl=100.0, margin_utilization=20.0,
        positions=(), net_delta=0.5, net_gamma=0.01, net_theta=-0.5, net_vega=0.1,
        timestamp=time.time(),
    )
    defaults.update(kwargs)
    return AccountSnapshot(**defaults)


def make_order(order_id, status=OrderStatus.LIVE, **kwargs):
    defaults = dict(
        order_id=order_id, lifecycle_id="trade-1", leg_index=0,
        purpose=OrderPurpose.OPEN_LEG, symbol="BTCUSD-11MAR26-90000-C",
        side="buy", qty=0.01, price=100.0, placed_at=time.time(),
    )
    defaults.update(kwargs)
    r = OrderRecord(**defaults)
    r.status = status
    return r


# ── Reconciliation Wiring ────────────────────────────────────────────────

class TestReconciliationWiring(unittest.TestCase):
    def _make_engine(self):
        with patch("lifecycle_engine.OrderManager") as MockOM, \
             patch("lifecycle_engine.Router"):
            mock_am = MagicMock()
            engine = LifecycleEngine(account_manager=mock_am)
            engine._order_manager.poll_all = MagicMock()
            engine._order_manager.persist_snapshot = MagicMock()
            return engine

    def test_not_called_before_n_ticks(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()
        account = fake_account()
        for _ in range(4):
            engine.tick(account)
        engine._run_reconciliation.assert_not_called()

    def test_called_on_nth_tick(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()
        account = fake_account()
        for _ in range(5):
            engine.tick(account)
        engine._run_reconciliation.assert_called_once()

    def test_called_every_n_ticks(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()
        account = fake_account()
        for _ in range(15):
            engine.tick(account)
        self.assertEqual(engine._run_reconciliation.call_count, 3)

    def test_skipped_without_account_manager(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.Router"):
            engine = LifecycleEngine(account_manager=None)
            engine._order_manager.poll_all = MagicMock()
            engine._order_manager.persist_snapshot = MagicMock()
        engine._run_reconciliation = MagicMock()
        account = fake_account()
        for _ in range(10):
            engine.tick(account)
        engine._run_reconciliation.assert_not_called()


# ── Reconciliation Logic ─────────────────────────────────────────────────

class TestReconciliationLogic(unittest.TestCase):
    def _make_engine_with_orders(self, ledger_orders=None, exchange_orders=None):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.Router"):
            mock_am = MagicMock()
            mock_am.get_open_orders.return_value = exchange_orders or []
            mock_executor = MagicMock()
            engine = LifecycleEngine(account_manager=mock_am, executor=mock_executor)
            real_om = OrderManager.__new__(OrderManager)
            real_om._orders = {}
            real_om._active_by_key = {}
            real_om._executor = engine._executor
            if ledger_orders:
                for o in ledger_orders:
                    real_om._orders[o.order_id] = o
            engine._order_manager = real_om
            return engine

    def test_clean_reconciliation(self):
        order = make_order("111", status=OrderStatus.LIVE)
        engine = self._make_engine_with_orders([order], [{"order_id": "111"}])
        engine._run_reconciliation()
        self.assertEqual(engine.last_reconciliation_warnings, [])

    def test_orphan_detected_and_cancelled(self):
        engine = self._make_engine_with_orders([], [{"order_id": "999"}])
        engine._executor.cancel_order = MagicMock(return_value=True)
        engine._run_reconciliation()
        self.assertEqual(len(engine.last_reconciliation_warnings), 1)
        self.assertIn("Orphan", engine.last_reconciliation_warnings[0])
        engine._executor.cancel_order.assert_called_once_with("999")

    def test_stale_ledger_entry_polled(self):
        order = make_order("222", status=OrderStatus.LIVE, placed_at=time.time() - 60)
        engine = self._make_engine_with_orders([order], [])
        with patch.object(engine._order_manager, "poll_order") as mock_poll:
            engine._run_reconciliation()
            mock_poll.assert_called_once_with("222")

    def test_pending_order_not_flagged(self):
        order = make_order("333", status=OrderStatus.PENDING)
        engine = self._make_engine_with_orders([order], [])
        engine._run_reconciliation()
        stale = [w for w in engine.last_reconciliation_warnings if "not found" in w]
        self.assertEqual(stale, [])

    def test_recently_placed_not_flagged(self):
        order = make_order("444", status=OrderStatus.LIVE, placed_at=time.time())
        engine = self._make_engine_with_orders([order], [])
        engine._run_reconciliation()
        stale = [w for w in engine.last_reconciliation_warnings if "not found" in w]
        self.assertEqual(stale, [])

    def test_survives_api_error(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.Router"):
            mock_am = MagicMock()
            mock_am.get_open_orders.side_effect = Exception("API timeout")
            engine = LifecycleEngine(account_manager=mock_am)
        engine._run_reconciliation()
        self.assertEqual(engine.last_reconciliation_warnings, [])


# ── Telegram Notifications ───────────────────────────────────────────────

class TestTelegramReconciliationAlerts(unittest.TestCase):
    def setUp(self):
        self.notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        self.notifier._enabled = False

    def test_notify_orphan_detected_exists(self):
        self.assertTrue(hasattr(self.notifier, "notify_orphan_detected"))

    def test_notify_reconciliation_warning_exists(self):
        self.assertTrue(hasattr(self.notifier, "notify_reconciliation_warning"))

    @patch.object(TelegramNotifier, "send")
    def test_orphan_message_format(self, mock_send):
        notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        notifier.notify_orphan_detected(["111", "222"], "auto-cancelled")
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        self.assertIn("Orphan Orders Detected", msg)
        self.assertIn("111", msg)
        self.assertIn("222", msg)
        self.assertIn("Count: 2", msg)

    @patch.object(TelegramNotifier, "send")
    def test_reconciliation_warning_format(self, mock_send):
        notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        notifier.notify_reconciliation_warning(["Stale order 123", "Stale order 456"])
        msg = mock_send.call_args[0][0]
        self.assertIn("Reconciliation Warning", msg)
        self.assertIn("2 issue(s)", msg)

    @patch.object(TelegramNotifier, "send")
    def test_orphan_truncation_beyond_5(self, mock_send):
        notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        notifier.notify_orphan_detected([str(i) for i in range(8)], "auto-cancelled")
        msg = mock_send.call_args[0][0]
        self.assertIn("+3 more", msg)
        self.assertIn("Count: 8", msg)


# ── Engine Constructor ───────────────────────────────────────────────────

class TestEngineAccountManager(unittest.TestCase):
    def test_default_no_account_manager(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.Router"):
            engine = LifecycleEngine()
        self.assertIsNone(engine._account_manager)

    def test_with_account_manager(self):
        mock_am = MagicMock()
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.Router"):
            engine = LifecycleEngine(account_manager=mock_am)
        self.assertIs(engine._account_manager, mock_am)
