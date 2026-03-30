"""
Unit tests for TradeLifecycle data model.

Pure data/computation tests — no network, no exchange calls.
Tests state machine data, PnL math, serialization round-trip.
"""

import time
import pytest

from trade_lifecycle import TradeState, TradeLeg, TradeLifecycle, RFQParams
from account_manager import AccountSnapshot, PositionSnapshot


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_account(positions=()):
    return AccountSnapshot(
        equity=10000.0, available_margin=8000.0,
        initial_margin=2000.0, maintenance_margin=1000.0,
        unrealized_pnl=100.0, margin_utilization=20.0,
        positions=positions, net_delta=0.5,
        net_gamma=0.01, net_theta=-0.5, net_vega=0.1,
        timestamp=time.time(),
    )


def _make_position(symbol, qty=0.1, side="long", unrealized_pnl=1.0,
                    delta=0.5, gamma=0.001, theta=-0.05, vega=0.1):
    return PositionSnapshot(
        position_id="pos-1",
        symbol=symbol, qty=qty, side=side,
        entry_price=500.0, mark_price=510.0,
        unrealized_pnl=unrealized_pnl, roi=0.02,
        delta=delta, gamma=gamma, theta=theta, vega=vega,
    )


# ── TradeLeg ─────────────────────────────────────────────────────────────

class TestTradeLeg:
    def test_is_filled(self):
        leg = TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.5, side="buy", filled_qty=0.5)
        assert leg.is_filled

    def test_not_filled(self):
        leg = TradeLeg(symbol="BTCUSD-28MAR26-100000-C", qty=0.5, side="buy", filled_qty=0.3)
        assert not leg.is_filled

    def test_close_side(self):
        assert TradeLeg(symbol="X", qty=1, side="buy").close_side == "sell"
        assert TradeLeg(symbol="X", qty=1, side="sell").close_side == "buy"

    def test_legacy_int_side_normalized(self):
        leg = TradeLeg(symbol="X", qty=1, side=1)
        assert leg.side == "buy"
        leg2 = TradeLeg(symbol="X", qty=1, side=2)
        assert leg2.side == "sell"

    def test_fill_price_cast_to_float(self):
        leg = TradeLeg(symbol="X", qty=1, side="buy", fill_price="500")
        assert leg.fill_price == 500.0
        assert isinstance(leg.fill_price, float)


# ── TradeLifecycle state ─────────────────────────────────────────────────

class TestTradeLifecycleState:
    def test_default_state(self):
        t = TradeLifecycle()
        assert t.state == TradeState.PENDING_OPEN

    def test_id_generated(self):
        t = TradeLifecycle()
        assert len(t.id) == 12

    def test_symbols_property(self):
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=1, side="buy"),
            TradeLeg(symbol="B", qty=1, side="sell"),
        ])
        assert t.symbols == ["A", "B"]

    def test_hold_seconds_none_before_open(self):
        t = TradeLifecycle()
        assert t.hold_seconds is None

    def test_hold_seconds_after_open(self):
        t = TradeLifecycle(opened_at=time.time() - 60)
        assert t.hold_seconds is not None
        assert t.hold_seconds >= 59


# ── PnL math ─────────────────────────────────────────────────────────────

class TestPnLMath:
    def test_total_entry_cost_buy(self):
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="buy", fill_price=100.0, filled_qty=0.5),
        ])
        assert t.total_entry_cost() == 50.0  # buy = +

    def test_total_entry_cost_sell(self):
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="sell", fill_price=100.0, filled_qty=0.5),
        ])
        assert t.total_entry_cost() == -50.0  # sell = -

    def test_total_entry_cost_straddle(self):
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="sell", fill_price=100.0, filled_qty=0.5),
            TradeLeg(symbol="B", qty=0.5, side="sell", fill_price=80.0, filled_qty=0.5),
        ])
        # credit: -(100*0.5) + -(80*0.5) = -90
        assert t.total_entry_cost() == -90.0

    def test_total_exit_cost(self):
        t = TradeLifecycle(close_legs=[
            TradeLeg(symbol="A", qty=0.5, side="buy", fill_price=50.0, filled_qty=0.5),
        ])
        assert t.total_exit_cost() == 25.0  # buy back = debit

    def test_finalize_close_realized_pnl(self):
        t = TradeLifecycle(
            open_legs=[
                TradeLeg(symbol="A", qty=1.0, side="sell", fill_price=100.0, filled_qty=1.0),
            ],
            close_legs=[
                TradeLeg(symbol="A", qty=1.0, side="buy", fill_price=60.0, filled_qty=1.0),
            ],
        )
        t._finalize_close()
        # entry_cost = -100 (sell credit), exit_cost = +60 (buy debit)
        # realized_pnl = -(entry + exit) = -(-100 + 60) = 40
        assert t.realized_pnl == 40.0

    def test_structure_pnl(self):
        pos = _make_position("A", qty=0.5, unrealized_pnl=10.0)
        account = _make_account(positions=(pos,))
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="buy", filled_qty=0.5),
        ])
        pnl = t.structure_pnl(account)
        # our_share = 0.5/0.5 = 1.0 → pnl = 10.0
        assert pnl == 10.0

    def test_structure_pnl_pro_rated(self):
        pos = _make_position("A", qty=1.0, unrealized_pnl=10.0)
        account = _make_account(positions=(pos,))
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="buy", filled_qty=0.5),
        ])
        pnl = t.structure_pnl(account)
        # our_share = 0.5/1.0 = 0.5 → pnl = 5.0
        assert pnl == 5.0

    def test_structure_greeks(self):
        pos = _make_position("A", qty=0.5, delta=0.5, gamma=0.001, theta=-0.05, vega=0.1)
        account = _make_account(positions=(pos,))
        t = TradeLifecycle(open_legs=[
            TradeLeg(symbol="A", qty=0.5, side="buy", filled_qty=0.5),
        ])
        greeks = t.structure_greeks(account)
        assert greeks["delta"] == pytest.approx(0.5)
        assert greeks["gamma"] == pytest.approx(0.001)
        assert greeks["theta"] == pytest.approx(-0.05)
        assert greeks["vega"] == pytest.approx(0.1)


# ── Serialization ────────────────────────────────────────────────────────

class TestSerialization:
    def test_to_dict_from_dict_round_trip(self):
        t = TradeLifecycle(
            id="test-123",
            strategy_id="daily_put_sell",
            state=TradeState.OPEN,
            execution_mode="limit",
            rfq_action="sell",
            opened_at=1000000.0,
            open_legs=[
                TradeLeg(symbol="A", qty=0.5, side="sell",
                         fill_price=100.0, filled_qty=0.5, order_id="ord-1"),
            ],
            close_legs=[
                TradeLeg(symbol="A", qty=0.5, side="buy"),
            ],
        )
        d = t.to_dict()
        restored = TradeLifecycle.from_dict(d)
        assert restored.id == "test-123"
        assert restored.strategy_id == "daily_put_sell"
        assert restored.state == TradeState.OPEN
        assert restored.execution_mode == "limit"
        assert restored.rfq_action == "sell"
        assert restored.opened_at == 1000000.0
        assert len(restored.open_legs) == 1
        assert restored.open_legs[0].symbol == "A"
        assert restored.open_legs[0].fill_price == 100.0
        assert restored.open_legs[0].filled_qty == 0.5
        assert len(restored.close_legs) == 1

    def test_from_dict_missing_optional_fields(self):
        d = {
            "id": "min",
            "state": "pending_open",
            "open_legs": [],
            "close_legs": [],
        }
        t = TradeLifecycle.from_dict(d)
        assert t.id == "min"
        assert t.state == TradeState.PENDING_OPEN
        assert t.strategy_id is None
        assert t.rfq_action == "buy"

    def test_realized_pnl_round_trips(self):
        t = TradeLifecycle(
            id="pnl-test", state=TradeState.CLOSED,
            realized_pnl=42.5, exit_cost=17.3,
        )
        d = t.to_dict()
        restored = TradeLifecycle.from_dict(d)
        assert restored.realized_pnl == 42.5
        assert restored.exit_cost == 17.3
