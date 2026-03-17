#!/usr/bin/env python3
"""
Tests for Phase 3 — Hardening features.

Covers:
  1. Periodic reconciliation wiring in LifecycleEngine
  2. Telegram orphan/reconciliation notification methods
  3. Dashboard /api/orders endpoint
  4. Reconciliation auto-cancel + stale-fix behaviour

Run:
    pytest tests/test_phase3_hardening.py -v
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch, PropertyMock
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import OrderManager, OrderRecord, OrderPurpose, OrderStatus
from lifecycle_engine import LifecycleEngine
from telegram_notifier import TelegramNotifier
from account_manager import AccountSnapshot


# =============================================================================
# Helpers
# =============================================================================

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


# =============================================================================
# Test: Reconciliation Wiring
# =============================================================================

class TestReconciliationWiring(unittest.TestCase):
    """Verify reconciliation is called periodically in tick()."""

    def _make_engine(self):
        """Create a LifecycleEngine with mocked internals."""
        with patch("lifecycle_engine.OrderManager") as MockOM, \
             patch("lifecycle_engine.ExecutionRouter"):
            mock_am = MagicMock()
            engine = LifecycleEngine(account_manager=mock_am)
            # Ensure poll_all doesn't fail
            engine._order_manager.poll_all = MagicMock()
            engine._order_manager.persist_snapshot = MagicMock()
            return engine

    def test_reconciliation_not_called_before_n_ticks(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()

        account = fake_account()
        # Tick 4 times (RECONCILE_EVERY_N_TICKS=5)
        for _ in range(4):
            engine.tick(account)

        engine._run_reconciliation.assert_not_called()

    def test_reconciliation_called_on_nth_tick(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()

        account = fake_account()
        for _ in range(5):
            engine.tick(account)

        engine._run_reconciliation.assert_called_once()

    def test_reconciliation_called_every_n_ticks(self):
        engine = self._make_engine()
        engine._run_reconciliation = MagicMock()

        account = fake_account()
        for _ in range(15):
            engine.tick(account)

        self.assertEqual(engine._run_reconciliation.call_count, 3)

    def test_reconciliation_skipped_without_account_manager(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.ExecutionRouter"):
            engine = LifecycleEngine(account_manager=None)
            engine._order_manager.poll_all = MagicMock()
            engine._order_manager.persist_snapshot = MagicMock()

        engine._run_reconciliation = MagicMock()
        account = fake_account()
        for _ in range(10):
            engine.tick(account)

        engine._run_reconciliation.assert_not_called()


# =============================================================================
# Test: Reconciliation Logic
# =============================================================================

class TestReconciliationLogic(unittest.TestCase):
    """Verify _run_reconciliation handles orphans and stale entries."""

    def _make_engine_with_orders(self, ledger_orders=None, exchange_orders=None):
        with patch("lifecycle_engine.OrderManager") as MockOM, \
             patch("lifecycle_engine.ExecutionRouter"):
            mock_am = MagicMock()
            mock_am.get_open_orders.return_value = exchange_orders or []
            mock_executor = MagicMock()

            engine = LifecycleEngine(account_manager=mock_am, executor=mock_executor)

            # Set up real OrderManager.reconcile with real orders
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
        exchange = [{"order_id": "111"}]
        engine = self._make_engine_with_orders([order], exchange)

        engine._run_reconciliation()

        self.assertEqual(engine.last_reconciliation_warnings, [])
        self.assertIsNotNone(engine.last_reconciliation_time)

    def test_orphan_detected_and_cancelled(self):
        # Exchange has order "999" but ledger doesn't
        exchange = [{"order_id": "999"}]
        engine = self._make_engine_with_orders([], exchange)
        engine._executor.cancel_order = MagicMock(return_value=True)

        engine._run_reconciliation()

        self.assertEqual(len(engine.last_reconciliation_warnings), 1)
        self.assertIn("Orphan", engine.last_reconciliation_warnings[0])
        engine._executor.cancel_order.assert_called_once_with("999")

    def test_stale_ledger_entry_polled(self):
        # Ledger says live, but exchange doesn't have it
        # placed_at is old enough to be past the reconciliation grace period
        order = make_order("222", status=OrderStatus.LIVE, placed_at=time.time() - 60)
        engine = self._make_engine_with_orders([order], [])

        # Mock poll_order to simulate discovering it was filled
        with patch.object(engine._order_manager, "poll_order") as mock_poll:
            engine._run_reconciliation()
            mock_poll.assert_called_once_with("222")

        self.assertTrue(any("not found on exchange" in w
                            for w in engine.last_reconciliation_warnings))

    def test_pending_order_not_flagged(self):
        # PENDING orders are not yet confirmed on exchange — should not trigger warnings
        order = make_order("333", status=OrderStatus.PENDING)
        engine = self._make_engine_with_orders([order], [])

        engine._run_reconciliation()

        # No warnings — PENDING is excluded from reconciliation
        stale = [w for w in engine.last_reconciliation_warnings if "not found" in w]
        self.assertEqual(stale, [])

    def test_recently_placed_order_not_flagged(self):
        # Recently placed LIVE order should not be flagged (grace period)
        order = make_order("444", status=OrderStatus.LIVE, placed_at=time.time())
        engine = self._make_engine_with_orders([order], [])

        engine._run_reconciliation()

        stale = [w for w in engine.last_reconciliation_warnings if "not found" in w]
        self.assertEqual(stale, [])

    def test_reconciliation_properties(self):
        engine = self._make_engine_with_orders([], [])
        self.assertIsNone(engine.last_reconciliation_time)
        self.assertEqual(engine.last_reconciliation_warnings, [])

        engine._run_reconciliation()

        self.assertIsNotNone(engine.last_reconciliation_time)

    def test_reconciliation_survives_api_error(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.ExecutionRouter"):
            mock_am = MagicMock()
            mock_am.get_open_orders.side_effect = Exception("API timeout")
            engine = LifecycleEngine(account_manager=mock_am)

        # Should not raise
        engine._run_reconciliation()
        # Warnings should be empty (reconciliation aborted)
        self.assertEqual(engine.last_reconciliation_warnings, [])


# =============================================================================
# Test: Telegram Notifications
# =============================================================================

class TestTelegramReconciliationAlerts(unittest.TestCase):
    """Verify new notification methods format correctly."""

    def setUp(self):
        self.notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        self.notifier._enabled = False  # Don't actually send

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
        self.assertIn("auto-cancelled", msg)
        self.assertIn("Count: 2", msg)

    @patch.object(TelegramNotifier, "send")
    def test_reconciliation_warning_format(self, mock_send):
        notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        warnings = ["Stale order 123", "Stale order 456"]
        notifier.notify_reconciliation_warning(warnings)
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        self.assertIn("Reconciliation Warning", msg)
        self.assertIn("2 issue(s)", msg)
        self.assertIn("Stale order 123", msg)

    @patch.object(TelegramNotifier, "send")
    def test_orphan_truncation_beyond_5(self, mock_send):
        notifier = TelegramNotifier(bot_token="fake", chat_id="123")
        ids = [str(i) for i in range(8)]
        notifier.notify_orphan_detected(ids, "auto-cancelled")
        msg = mock_send.call_args[0][0]
        self.assertIn("+3 more", msg)
        self.assertIn("Count: 8", msg)


# =============================================================================
# Test: Dashboard Orders Endpoint
# =============================================================================

class TestDashboardOrders(unittest.TestCase):
    """Verify the /api/orders endpoint returns order data."""

    def _make_app(self):
        """Create a test Flask app with mock context."""
        mock_ctx = MagicMock()
        mock_runners = []

        # Set up order manager with some test orders
        live_order = make_order("100", status=OrderStatus.LIVE)
        filled_order = make_order("99", status=OrderStatus.FILLED)
        filled_order.terminal_at = time.time()
        filled_order.avg_fill_price = 105.0
        filled_order.filled_qty = 0.01

        # Use a real dict for _orders so .values() works naturally
        orders_dict = {"100": live_order, "99": filled_order}

        mock_om = MagicMock()
        mock_om._orders = orders_dict

        mock_ctx.lifecycle_manager._order_manager = mock_om
        mock_ctx.lifecycle_manager.last_reconciliation_warnings = []
        mock_ctx.lifecycle_manager.last_reconciliation_time = time.time() - 30

        # Minimal PositionMonitor mock
        mock_ctx.position_monitor.latest = None

        from dashboard import _create_app
        app = _create_app(mock_ctx, mock_runners, "testpass")
        app.config["TESTING"] = True
        return app

    def test_orders_endpoint_exists(self):
        app = self._make_app()
        with app.test_client() as client:
            # Login first
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            self.assertEqual(resp.status_code, 200)

    def test_orders_shows_live_orders(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            html = resp.data.decode()
            self.assertIn("BTCUSD-11MAR26-90000-C", html)
            self.assertIn("live", html)

    def test_orders_shows_recent_fills(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            html = resp.data.decode()
            self.assertIn("Recent Fills", html)
            self.assertIn("filled", html)

    def test_orders_shows_clean_reconciliation(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            html = resp.data.decode()
            self.assertIn("clean", html)

    def test_orders_empty_ledger(self):
        mock_ctx = MagicMock()
        mock_runners = []
        mock_om = MagicMock()
        mock_om._orders = {}
        mock_ctx.lifecycle_manager._order_manager = mock_om
        mock_ctx.lifecycle_manager.last_reconciliation_warnings = []
        mock_ctx.lifecycle_manager.last_reconciliation_time = None
        mock_ctx.position_monitor.latest = None

        from dashboard import _create_app
        app = _create_app(mock_ctx, mock_runners, "testpass")
        app.config["TESTING"] = True
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            html = resp.data.decode()
            self.assertIn("No live orders", html)


# =============================================================================
# Test: Engine Constructor
# =============================================================================

class TestEngineAccountManager(unittest.TestCase):
    """Verify LifecycleEngine accepts optional account_manager."""

    def test_default_no_account_manager(self):
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.ExecutionRouter"):
            engine = LifecycleEngine()
        self.assertIsNone(engine._account_manager)

    def test_with_account_manager(self):
        mock_am = MagicMock()
        with patch("lifecycle_engine.OrderManager"), \
             patch("lifecycle_engine.ExecutionRouter"):
            engine = LifecycleEngine(account_manager=mock_am)
        self.assertIs(engine._account_manager, mock_am)


if __name__ == "__main__":
    unittest.main()
