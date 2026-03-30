"""
Unit tests for Dashboard Flask routes.

Uses Flask test client with mock TradingContext — no network calls.
"""

import time
import unittest
from unittest.mock import MagicMock

from order_manager import OrderRecord, OrderPurpose, OrderStatus


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


class TestDashboardOrders(unittest.TestCase):
    def _make_app(self):
        mock_ctx = MagicMock()
        mock_runners = []

        live_order = make_order("100", status=OrderStatus.LIVE)
        filled_order = make_order("99", status=OrderStatus.FILLED)
        filled_order.terminal_at = time.time()
        filled_order.avg_fill_price = 105.0
        filled_order.filled_qty = 0.01

        mock_om = MagicMock()
        mock_om._orders = {"100": live_order, "99": filled_order}
        mock_ctx.lifecycle_manager._order_manager = mock_om
        mock_ctx.lifecycle_manager.last_reconciliation_warnings = []
        mock_ctx.lifecycle_manager.last_reconciliation_time = time.time() - 30
        mock_ctx.position_monitor.latest = None

        from dashboard import _create_app
        app = _create_app(mock_ctx, mock_runners, "testpass")
        app.config["TESTING"] = True
        return app

    def test_orders_endpoint_exists(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            resp = client.get("/api/orders")
            self.assertEqual(resp.status_code, 200)

    def test_orders_shows_live_orders(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            html = client.get("/api/orders").data.decode()
            self.assertIn("BTCUSD-11MAR26-90000-C", html)
            self.assertIn("live", html)

    def test_orders_shows_recent_fills(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            html = client.get("/api/orders").data.decode()
            self.assertIn("Recent Fills", html)
            self.assertIn("filled", html)

    def test_orders_shows_clean_reconciliation(self):
        app = self._make_app()
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            html = client.get("/api/orders").data.decode()
            self.assertIn("clean", html)

    def test_orders_empty_ledger(self):
        mock_ctx = MagicMock()
        mock_om = MagicMock()
        mock_om._orders = {}
        mock_ctx.lifecycle_manager._order_manager = mock_om
        mock_ctx.lifecycle_manager.last_reconciliation_warnings = []
        mock_ctx.lifecycle_manager.last_reconciliation_time = None
        mock_ctx.position_monitor.latest = None

        from dashboard import _create_app
        app = _create_app(mock_ctx, [], "testpass")
        app.config["TESTING"] = True
        with app.test_client() as client:
            client.post("/login", data={"password": "testpass"})
            html = client.get("/api/orders").data.decode()
            self.assertIn("No live orders", html)
