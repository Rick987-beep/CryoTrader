"""
Unit tests for OrderManager — the central order ledger.

All tests use MockExecutor from conftest — no API calls.
"""

import json
import os
import shutil
import tempfile
import time

import pytest

from order_manager import (
    OrderManager, OrderRecord, OrderPurpose, OrderStatus,
)
from tests.conftest import MockExecutor


def fresh_om():
    """Create a fresh OrderManager + MockExecutor pair."""
    m = MockExecutor()
    om = OrderManager(m)
    return om, m


# ── Test 1: Idempotent placement ────────────────────────────────────────

class TestIdempotentPlacement:
    def test_first_placement_returns_record(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        assert r is not None

    def test_second_placement_returns_same_record(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        r2 = om.place_order(
            lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        assert r2.order_id == r1.order_id

    def test_executor_called_only_once(self):
        om, mock = fresh_om()
        om.place_order(
            lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.place_order(
            lifecycle_id="trade-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        place_calls = [c for c in mock.calls if c[0] == "place_order"]
        assert len(place_calls) == 1


# ── Test 2: Placement records correct fields ────────────────────────────

class TestPlacementFields:
    def test_fields_recorded_correctly(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-2", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.5, price=300.0,
        )
        assert r.order_id != ""
        assert r.lifecycle_id == "trade-2"
        assert r.leg_index == 1
        assert r.purpose == OrderPurpose.OPEN_LEG
        assert r.symbol == "BTCUSD-28MAR26-90000-P"
        assert r.side == "sell"
        assert r.qty == 0.5
        assert r.price == 300.0
        assert r.status == OrderStatus.PENDING
        assert abs(r.placed_at - time.time()) < 5
        assert r.client_order_id is not None


# ── Test 3: reduce_only forced for CLOSE_LEG and UNWIND ─────────────────

class TestReduceOnly:
    def test_close_leg_forces_reduce_only(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-3", leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="sell", qty=0.1, price=500.0,
            reduce_only=False,
        )
        assert r.reduce_only is True
        call = [c for c in mock.calls if c[0] == "place_order"][-1]
        assert call[1]["reduce_only"] is True

    def test_unwind_forces_reduce_only(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-3", leg_index=1, purpose=OrderPurpose.UNWIND,
            symbol="BTCUSD-28MAR26-90000-P", side="buy", qty=0.1, price=300.0,
            reduce_only=False,
        )
        assert r.reduce_only is True

    def test_open_leg_respects_caller_choice(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-3", leg_index=2, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-80000-C", side="buy", qty=0.1, price=400.0,
            reduce_only=False,
        )
        assert r.reduce_only is False


# ── Test 4: Hard cap — max orders per lifecycle ──────────────────────────

class TestHardCapPerLifecycle:
    def test_exceeding_cap_returns_none(self):
        om, mock = fresh_om()
        om.MAX_ORDERS_PER_LIFECYCLE = 3
        for i in range(3):
            r = om.place_order(
                lifecycle_id="trade-cap", leg_index=i, purpose=OrderPurpose.OPEN_LEG,
                symbol=f"BTCUSD-28MAR26-{80000+i*1000}-C", side="buy", qty=0.1, price=100.0,
            )
            assert r is not None
        r4 = om.place_order(
            lifecycle_id="trade-cap", leg_index=3, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-83000-C", side="buy", qty=0.1, price=100.0,
        )
        assert r4 is None


# ── Test 5: Hard cap — max pending per symbol ────────────────────────────

class TestHardCapPerSymbol:
    def test_exceeding_symbol_cap_returns_none(self):
        om, mock = fresh_om()
        om.MAX_PENDING_PER_SYMBOL = 2
        om.place_order(
            lifecycle_id="trade-sym-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.place_order(
            lifecycle_id="trade-sym-2", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=510.0,
        )
        r3 = om.place_order(
            lifecycle_id="trade-sym-3", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=520.0,
        )
        assert r3 is None

    def test_different_symbol_still_works(self):
        om, mock = fresh_om()
        om.MAX_PENDING_PER_SYMBOL = 2
        om.place_order(
            lifecycle_id="trade-sym-1", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.place_order(
            lifecycle_id="trade-sym-2", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=510.0,
        )
        r = om.place_order(
            lifecycle_id="trade-sym-3", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.1, price=300.0,
        )
        assert r is not None


# ── Test 6: cancel_order ─────────────────────────────────────────────────

class TestCancelOrder:
    def test_cancel_succeeds(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-cancel", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        assert r.is_live
        ok = om.cancel_order(r.order_id)
        assert ok is True
        assert r.is_terminal
        assert r.status == OrderStatus.CANCELLED
        assert r.terminal_at is not None

    def test_replacement_after_cancel(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-cancel", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.cancel_order(r1.order_id)
        r2 = om.place_order(
            lifecycle_id="trade-cancel", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=510.0,
        )
        assert r2 is not None
        assert r2.order_id != r1.order_id


# ── Test 7: requote_order ────────────────────────────────────────────────

class TestRequoteOrder:
    def test_requote_returns_new_record(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-rq", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.5, price=500.0,
        )
        r2 = om.requote_order(r1.order_id, new_price=510.0)
        assert r2 is not None
        assert r2.order_id != r1.order_id
        assert r2.price == 510.0
        assert r2.qty == 0.5
        assert r1.superseded_by == r2.order_id
        assert r2.supersedes == r1.order_id
        assert r1.is_terminal

    def test_requote_filled_order_returns_none(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-rq-filled", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        mock.simulate_fill(r.order_id, filled_qty=0.1, avg_price=499.0, full=True)
        result = om.requote_order(r.order_id, new_price=510.0)
        assert result is None
        assert r.status == OrderStatus.FILLED

    def test_requote_partial_fill_adjusts_qty(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-rq-partial", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=1.0, price=500.0,
        )
        mock.simulate_fill(r1.order_id, filled_qty=0.4, avg_price=499.0, full=False)
        r2 = om.requote_order(r1.order_id, new_price=510.0)
        assert r2 is not None
        assert abs(r2.qty - 0.6) < 0.001


# ── Test 8: poll_order ───────────────────────────────────────────────────

class TestPollOrder:
    def test_partial_fill_detected(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-poll", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=1.0, price=500.0,
        )
        mock.simulate_fill(r.order_id, filled_qty=0.3, avg_price=499.5, full=False)
        om.poll_order(r.order_id)
        assert r.filled_qty == 0.3
        assert r.avg_fill_price == 499.5
        assert r.status == OrderStatus.PARTIAL
        assert r.is_live

    def test_full_fill_detected(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-poll", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=1.0, price=500.0,
        )
        mock.simulate_fill(r.order_id, filled_qty=1.0, avg_price=499.8, full=True)
        om.poll_order(r.order_id)
        assert r.filled_qty == 1.0
        assert r.status == OrderStatus.FILLED
        assert r.is_terminal


# ── Test 9: poll_all ─────────────────────────────────────────────────────

class TestPollAll:
    def test_polls_all_non_terminal(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-pa", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        r2 = om.place_order(
            lifecycle_id="trade-pa", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.2, price=300.0,
        )
        mock.simulate_fill(r1.order_id, filled_qty=0.1, avg_price=500.0, full=True)
        om.poll_all()
        assert r1.status == OrderStatus.FILLED
        assert r2.status == OrderStatus.LIVE

    def test_skips_terminal_on_second_poll(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-pa", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.place_order(
            lifecycle_id="trade-pa", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.2, price=300.0,
        )
        mock.simulate_fill(r1.order_id, filled_qty=0.1, avg_price=500.0, full=True)
        om.poll_all()
        calls_before = len([c for c in mock.calls if c[0] == "get_order_status"])
        om.poll_all()
        calls_after = len([c for c in mock.calls if c[0] == "get_order_status"])
        assert calls_after - calls_before == 1  # only r2


# ── Test 10: get_filled_for_leg aggregation ──────────────────────────────

class TestFilledAggregation:
    def test_aggregates_across_supersession_chain(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-agg", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=1.0, price=500.0,
        )
        mock.simulate_fill(r1.order_id, filled_qty=0.3, avg_price=499.0, full=False)
        om.poll_order(r1.order_id)
        r2 = om.requote_order(r1.order_id, new_price=510.0)
        mock.simulate_fill(r2.order_id, filled_qty=0.7, avg_price=509.0, full=True)
        om.poll_order(r2.order_id)

        total_qty, vwap = om.get_filled_for_leg("trade-agg", 0, OrderPurpose.OPEN_LEG)
        assert abs(total_qty - 1.0) < 0.001
        expected_vwap = (0.3 * 499.0 + 0.7 * 509.0) / 1.0
        assert abs(vwap - expected_vwap) < 0.01


# ── Test 11: has_live_orders ─────────────────────────────────────────────

class TestHasLiveOrders:
    def test_no_live_orders_initially(self):
        om, mock = fresh_om()
        assert not om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG)

    def test_has_live_after_placement(self):
        om, mock = fresh_om()
        om.place_order(
            lifecycle_id="trade-x", leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="sell", qty=0.1, price=500.0,
        )
        assert om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG)
        assert not om.has_live_orders("trade-x", OrderPurpose.OPEN_LEG)

    def test_no_live_after_cancel(self):
        om, mock = fresh_om()
        r = om.place_order(
            lifecycle_id="trade-x", leg_index=0, purpose=OrderPurpose.CLOSE_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="sell", qty=0.1, price=500.0,
        )
        om.cancel_order(r.order_id)
        assert not om.has_live_orders("trade-x", OrderPurpose.CLOSE_LEG)


# ── Test 12: cancel_all_for ──────────────────────────────────────────────

class TestCancelAllFor:
    def test_cancels_correct_lifecycle(self):
        om, mock = fresh_om()
        om.place_order(
            lifecycle_id="trade-caf", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        om.place_order(
            lifecycle_id="trade-caf", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.1, price=300.0,
        )
        om.place_order(
            lifecycle_id="trade-other", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-80000-C", side="buy", qty=0.1, price=200.0,
        )
        count = om.cancel_all_for("trade-caf")
        assert count == 2
        assert not om.has_live_orders("trade-caf", OrderPurpose.OPEN_LEG)
        assert om.has_live_orders("trade-other", OrderPurpose.OPEN_LEG)


# ── Test 13: Persistence round-trip ──────────────────────────────────────

class TestPersistence:
    def test_save_and_load_snapshot(self):
        import order_manager as om_module
        orig_logs = om_module.LOGS_DIR
        tmp_dir = tempfile.mkdtemp()
        om_module.LOGS_DIR = tmp_dir
        try:
            om, mock = fresh_om()
            r1 = om.place_order(
                lifecycle_id="trade-persist", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
                symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.5, price=500.0,
            )
            r2 = om.place_order(
                lifecycle_id="trade-persist", leg_index=1, purpose=OrderPurpose.OPEN_LEG,
                symbol="BTCUSD-28MAR26-90000-P", side="sell", qty=0.3, price=300.0,
            )
            mock.simulate_fill(r2.order_id, filled_qty=0.3, avg_price=299.0, full=True)
            om.poll_order(r2.order_id)
            om.persist_snapshot()

            om2 = OrderManager(mock)
            om2.load_snapshot()
            loaded = om2.get_all_orders("trade-persist")
            assert len(loaded) == 1
            assert loaded[0].symbol == "BTCUSD-28MAR26-100000-C"
            assert loaded[0].qty == 0.5
            assert om2.has_live_orders("trade-persist", OrderPurpose.OPEN_LEG)
        finally:
            om_module.LOGS_DIR = orig_logs
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_event_log_jsonl(self):
        import order_manager as om_module
        orig_logs = om_module.LOGS_DIR
        tmp_dir = tempfile.mkdtemp()
        om_module.LOGS_DIR = tmp_dir
        try:
            om, mock = fresh_om()
            r = om.place_order(
                lifecycle_id="trade-log", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
                symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
            )
            om.cancel_order(r.order_id)

            ledger_path = os.path.join(tmp_dir, "order_ledger.jsonl")
            assert os.path.exists(ledger_path)
            with open(ledger_path, "r") as f:
                lines = [json.loads(line) for line in f if line.strip()]
            assert len(lines) >= 2
            actions = [l["action"] for l in lines]
            assert "placed" in actions
            assert any("terminal" in a for a in actions)
        finally:
            om_module.LOGS_DIR = orig_logs
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Test 14: OrderRecord serialization ───────────────────────────────────

class TestOrderRecordSerialization:
    def test_to_dict_from_dict_round_trip(self):
        record = OrderRecord(
            order_id="test-123", client_order_id="456",
            lifecycle_id="trade-rt", leg_index=2,
            purpose=OrderPurpose.CLOSE_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="sell",
            qty=0.5, price=500.0, reduce_only=True,
            status=OrderStatus.PARTIAL, filled_qty=0.2,
            avg_fill_price=498.0, placed_at=1000000.0,
            updated_at=1000010.0, terminal_at=None,
            superseded_by="test-456", supersedes="test-000",
        )
        d = record.to_dict()
        restored = OrderRecord.from_dict(d)
        assert restored.order_id == "test-123"
        assert restored.purpose == OrderPurpose.CLOSE_LEG
        assert restored.status == OrderStatus.PARTIAL
        assert restored.filled_qty == 0.2
        assert restored.avg_fill_price == 498.0
        assert restored.superseded_by == "test-456"
        assert restored.reduce_only is True


# ── Test 15: Reconciliation ──────────────────────────────────────────────

class TestReconciliation:
    def test_orphan_detected(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-recon", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        exchange_orders = [
            {"order_id": r1.order_id},
            {"order_id": "999999"},
        ]
        warnings = om.reconcile(exchange_orders)
        assert any("999999" in w for w in warnings)
        assert not any(r1.order_id in w and "not found" in w for w in warnings)

    def test_phantom_ledger_order_detected(self):
        om, mock = fresh_om()
        r1 = om.place_order(
            lifecycle_id="trade-recon", leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="BTCUSD-28MAR26-100000-C", side="buy", qty=0.1, price=500.0,
        )
        r1.placed_at = r1.placed_at - 60
        r1.status = OrderStatus.LIVE
        warnings = om.reconcile([])
        assert any(r1.order_id in w for w in warnings)
