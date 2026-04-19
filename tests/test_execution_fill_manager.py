"""
Tests for execution.fill_manager.FillManager — the new typed fill manager.

Uses mock OrderManager + MarketData (no API calls).
"""

import time
from unittest.mock import MagicMock

import pytest

from execution.currency import Currency, Price
from execution.fill_manager import FillManager, _bridge_params_to_profile, _LegState
from execution.fill_result import FillResult, FillStatus
from execution.profiles import ExecutionProfile, PhaseConfig
from order_manager import OrderPurpose, OrderRecord, OrderStatus
from trade_lifecycle import TradeLeg


# =============================================================================
# Helpers
# =============================================================================

def _make_md(books=None):
    """Mock market data with configurable orderbooks."""
    md = MagicMock()
    default_book = {
        "bids": [{"price": 0.0100}],
        "asks": [{"price": 0.0110}],
        "mark": 0.0105,
        "_mark_btc": 0.0105,
        "_index_price": 50000.0,
    }
    _books = books or {}

    def get_ob(symbol):
        return _books.get(symbol, default_book)

    md.get_option_orderbook = MagicMock(side_effect=get_ob)
    return md


def _make_om():
    """Mock OrderManager with auto-incrementing order IDs."""
    om = MagicMock()
    om._orders = {}  # fill_manager accesses _orders directly for price check
    om._records = om._orders  # alias
    _counter = [0]

    def place_order(lifecycle_id, leg_index, purpose, symbol, side, qty, price, reduce_only=False):
        _counter[0] += 1
        oid = f"ORD-{_counter[0]}"
        rec = OrderRecord(
            order_id=oid, client_order_id=str(_counter[0]),
            lifecycle_id=lifecycle_id, leg_index=leg_index, purpose=purpose,
            symbol=symbol, side=side, qty=qty, price=price,
            reduce_only=reduce_only, status=OrderStatus.PENDING,
            placed_at=time.time(),
        )
        om._orders[oid] = rec
        return rec

    def poll_order(order_id):
        return om._orders.get(order_id)

    def requote_order(order_id, new_price, new_qty):
        old = om._orders.get(order_id)
        if not old:
            return None
        _counter[0] += 1
        oid = f"ORD-{_counter[0]}"
        rec = OrderRecord(
            order_id=oid, client_order_id=str(_counter[0]),
            lifecycle_id=old.lifecycle_id, leg_index=old.leg_index,
            purpose=old.purpose, symbol=old.symbol, side=old.side,
            qty=new_qty, price=new_price, reduce_only=old.reduce_only,
            status=OrderStatus.PENDING, placed_at=time.time(),
        )
        om._orders[oid] = rec
        return rec

    om.place_order = MagicMock(side_effect=place_order)
    om.poll_order = MagicMock(side_effect=poll_order)
    om.requote_order = MagicMock(side_effect=requote_order)
    om.cancel_order = MagicMock(return_value=True)
    return om


def _legs(*specs):
    """Build TradeLeg list from (symbol, qty, side) tuples."""
    return [TradeLeg(symbol=s, qty=q, side=sd) for s, q, sd in specs]


def _profile(**kw):
    """Build a simple ExecutionProfile for testing."""
    phases = kw.pop("phases", [PhaseConfig(pricing="aggressive")])
    return ExecutionProfile(name="test", open_phases=phases, **kw)


# =============================================================================
# _LegState basics
# =============================================================================

class TestLegState:
    def test_is_filled(self):
        ls = _LegState(symbol="X", qty=1.0, side="buy", leg_index=0, filled_qty=1.0)
        assert ls.is_filled

    def test_not_filled(self):
        ls = _LegState(symbol="X", qty=1.0, side="buy", leg_index=0, filled_qty=0.5)
        assert not ls.is_filled

    def test_remaining_qty(self):
        ls = _LegState(symbol="X", qty=1.0, side="buy", leg_index=0, filled_qty=0.3)
        assert abs(ls.remaining_qty - 0.7) < 1e-9

    def test_remaining_never_negative(self):
        ls = _LegState(symbol="X", qty=1.0, side="buy", leg_index=0, filled_qty=1.5)
        assert ls.remaining_qty == 0.0


# =============================================================================
# place_all — success
# =============================================================================

class TestPlaceAllSuccess:
    def test_single_leg(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("SYM-C", 0.1, "sell"))

        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.status == FillStatus.PENDING
        assert len(result.legs) == 1
        assert legs[0].order_id is not None
        om.place_order.assert_called_once()

    def test_multi_leg(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("CALL", 0.1, "sell"), ("PUT", 0.1, "sell"))

        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.status == FillStatus.PENDING
        assert len(result.legs) == 2
        assert om.place_order.call_count == 2

    def test_order_id_written_back(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("SYM", 0.1, "buy"))

        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)
        assert legs[0].order_id is not None
        assert legs[0].order_id.startswith("ORD-")


# =============================================================================
# place_all — atomic vs best_effort
# =============================================================================

class TestPlaceAllAtomic:
    def test_no_orderbook_refuses(self):
        om = _make_om()
        md = MagicMock()
        md.get_option_orderbook = MagicMock(return_value=None)
        profile = _profile()
        profile.open_atomic = True
        mgr = FillManager(om, md, profile=profile, direction="open")

        legs = _legs(("NO-OB", 0.1, "sell"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.status == FillStatus.REFUSED
        om.place_order.assert_not_called()

    def test_placement_failure_cancels_already_placed(self):
        """Second leg fails → first leg's order is cancelled (atomic)."""
        call_count = [0]
        om = _make_om()
        orig = om.place_order.side_effect

        def fail_second(*a, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                return None
            return orig(*a, **kw)

        om.place_order = MagicMock(side_effect=fail_second)
        md = _make_md()
        profile = _profile()
        profile.open_atomic = True
        mgr = FillManager(om, md, profile=profile, direction="open")

        legs = _legs(("A", 0.1, "sell"), ("B", 0.1, "sell"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.status == FillStatus.REFUSED
        om.cancel_order.assert_called()


class TestPlaceAllBestEffort:
    def test_skips_bad_orderbook(self):
        books = {
            "GOOD": {
                "bids": [{"price": 0.01}], "asks": [{"price": 0.02}],
                "_mark_btc": 0.015, "mark": 0.015, "_index_price": 50000,
            },
            "BAD": None,
        }
        om = _make_om()
        md = MagicMock()
        md.get_option_orderbook = MagicMock(side_effect=lambda s: books.get(s))

        profile = ExecutionProfile(
            name="test",
            close_phases=[PhaseConfig(pricing="aggressive")],
            close_best_effort=True,
        )
        mgr = FillManager(om, md, profile=profile, direction="close")

        legs = _legs(("GOOD", 0.1, "buy"), ("BAD", 0.1, "buy"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.CLOSE_LEG, reduce_only=True)

        assert result.status == FillStatus.PENDING
        skipped = [l for l in result.legs if l.skipped]
        assert len(skipped) == 1
        assert skipped[0].symbol == "BAD"

    def test_all_skipped_returns_refused(self):
        om = _make_om()
        md = MagicMock()
        md.get_option_orderbook = MagicMock(return_value=None)
        profile = ExecutionProfile(
            name="test",
            close_phases=[PhaseConfig()],
            close_best_effort=True,
        )
        mgr = FillManager(om, md, profile=profile, direction="close")

        legs = _legs(("A", 0.1, "buy"), ("B", 0.1, "buy"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.CLOSE_LEG, reduce_only=True)

        assert result.status == FillStatus.REFUSED


# =============================================================================
# Immediate fills
# =============================================================================

class TestImmediateFill:
    def test_immediate_fill_returns_filled(self):
        om = MagicMock()
        om.place_order = MagicMock(return_value=OrderRecord(
            order_id="ORD-1", client_order_id="1", lifecycle_id="T1",
            leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="SYM", side="sell", qty=0.1, price=0.01,
            status=OrderStatus.FILLED, filled_qty=0.1,
            avg_fill_price=0.01, placed_at=time.time(),
            fee=Price(0.0003, Currency.BTC),
        ))
        md = _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")

        legs = _legs(("SYM", 0.1, "sell"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.status == FillStatus.FILLED
        assert result.total_fees.amount == 0.0003


# =============================================================================
# check — FILLED
# =============================================================================

class TestCheckFilled:
    def test_all_filled(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(phases=[PhaseConfig(duration_seconds=60)]), direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        oid = legs[0].order_id
        om._records[oid].filled_qty = 0.1
        om._records[oid].avg_fill_price = 0.0105

        result = mgr.check()
        assert result.status == FillStatus.FILLED


# =============================================================================
# check — PENDING
# =============================================================================

class TestCheckPending:
    def test_unfilled_within_phase(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(phases=[PhaseConfig(duration_seconds=600)]), direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        result = mgr.check()
        assert result.status == FillStatus.PENDING


# =============================================================================
# check — REQUOTED (phase advancement)
# =============================================================================

class TestCheckPhaseAdvance:
    def test_advances_to_next_phase(self):
        om, md = _make_om(), _make_md()
        profile = _profile(phases=[
            PhaseConfig(pricing="fair", duration_seconds=10, reprice_interval=999),
            PhaseConfig(pricing="aggressive", duration_seconds=30),
        ])
        mgr = FillManager(om, md, profile=profile, direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        mgr._phase_started_at = time.time() - 15  # phase 1 expired

        result = mgr.check()
        assert result.status == FillStatus.REQUOTED
        assert result.phase_index == 2

    def test_within_phase_reprice(self):
        om, md = _make_om(), _make_md()
        profile = _profile(phases=[
            PhaseConfig(pricing="aggressive", duration_seconds=120, reprice_interval=15),
        ])
        mgr = FillManager(om, md, profile=profile, direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        mgr._last_reprice_at = time.time() - 20
        mgr._phase_started_at = time.time() - 20

        result = mgr.check()
        assert result.status in (FillStatus.REQUOTED, FillStatus.PENDING)


# =============================================================================
# check — FAILED + grace tick
# =============================================================================

class TestCheckFailed:
    def test_grace_tick_then_fail(self):
        om, md = _make_om(), _make_md()
        profile = _profile(phases=[PhaseConfig(pricing="aggressive", duration_seconds=10)])
        mgr = FillManager(om, md, profile=profile, direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        # Simulate all phases exhausted
        mgr._phase_started_at = time.time() - 15
        mgr._phase_index = 1  # past last phase

        # First call: grace tick → PENDING
        r1 = mgr.check()
        assert r1.status == FillStatus.PENDING
        assert mgr._grace_exhausted is True

        # Second call: truly FAILED
        r2 = mgr.check()
        assert r2.status == FillStatus.FAILED

    def test_grace_catches_late_fill(self):
        om, md = _make_om(), _make_md()
        profile = _profile(phases=[PhaseConfig(pricing="aggressive", duration_seconds=10)])
        mgr = FillManager(om, md, profile=profile, direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        oid = legs[0].order_id
        mgr._phase_started_at = time.time() - 15
        mgr._phase_index = 1

        # Grace tick
        r1 = mgr.check()
        assert r1.status == FillStatus.PENDING

        # Fill arrives during grace
        om._records[oid].filled_qty = 0.1
        om._records[oid].avg_fill_price = 0.0105

        r2 = mgr.check()
        assert r2.status == FillStatus.FILLED


# =============================================================================
# cancel_all
# =============================================================================

class TestCancelAll:
    def test_cancels_unfilled(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("A", 0.1, "sell"), ("B", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        mgr.cancel_all()
        assert om.cancel_order.call_count == 2

    def test_skips_filled(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        mgr._legs[0].filled_qty = 0.1  # mark filled

        mgr.cancel_all()
        om.cancel_order.assert_not_called()


# =============================================================================
# Properties
# =============================================================================

class TestProperties:
    def test_all_filled_property(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert not mgr.all_filled
        mgr._legs[0].filled_qty = 0.1
        assert mgr.all_filled

    def test_has_skipped_legs(self):
        md = MagicMock()
        md.get_option_orderbook = MagicMock(side_effect=lambda s: None if s == "BAD" else {
            "bids": [{"price": 0.01}], "asks": [{"price": 0.02}],
            "_mark_btc": 0.015, "mark": 0.015, "_index_price": 50000,
        })
        om = _make_om()
        profile = ExecutionProfile(
            name="test",
            close_phases=[PhaseConfig()],
            close_best_effort=True,
        )
        mgr = FillManager(om, md, profile=profile, direction="close")

        legs = _legs(("GOOD", 0.1, "buy"), ("BAD", 0.1, "buy"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.CLOSE_LEG, reduce_only=True)

        assert mgr.has_skipped_legs
        assert "BAD" in mgr.skipped_symbols

    def test_legs_returns_all(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("A", 0.1, "sell"), ("B", 0.2, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert len(mgr.legs) == 2
        assert mgr.legs[0].symbol == "A"
        assert mgr.legs[1].symbol == "B"


# =============================================================================
# Fee capture
# =============================================================================

class TestFeeCapture:
    def test_fee_from_immediate_fill(self):
        om = MagicMock()
        fee = Price(0.0005, Currency.BTC)
        om.place_order = MagicMock(return_value=OrderRecord(
            order_id="ORD-1", client_order_id="1", lifecycle_id="T1",
            leg_index=0, purpose=OrderPurpose.OPEN_LEG,
            symbol="SYM", side="sell", qty=0.1, price=0.01,
            status=OrderStatus.PENDING, placed_at=time.time(),
            fee=fee,
        ))
        md = _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")

        legs = _legs(("SYM", 0.1, "sell"))
        result = mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        assert result.total_fees is not None
        assert result.total_fees.amount == 0.0005
        assert result.total_fees.currency == Currency.BTC

    def test_fee_from_poll(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(phases=[PhaseConfig(duration_seconds=600)]), direction="open")
        legs = _legs(("SYM", 0.1, "sell"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        oid = legs[0].order_id
        om._records[oid].filled_qty = 0.1
        om._records[oid].avg_fill_price = 0.0105
        om._records[oid].fee = Price(0.0002, Currency.BTC)

        result = mgr.check()
        assert result.status == FillStatus.FILLED
        assert result.total_fees.amount == 0.0002


# =============================================================================
# ExecutionParams → ExecutionProfile bridge
# =============================================================================

class TestBridge:
    def test_phased_params(self):
        from trade_execution import ExecutionParams, ExecutionPhase

        params = ExecutionParams(phases=[
            ExecutionPhase(pricing="fair", duration_seconds=30, fair_aggression=0.3),
            ExecutionPhase(pricing="aggressive", duration_seconds=60, buffer_pct=3.0),
        ])
        profile = _bridge_params_to_profile(params)

        assert profile.name == "_bridged"
        assert len(profile.open_phases) == 2
        assert profile.open_phases[0].pricing == "fair"
        assert profile.open_phases[0].fair_aggression == 0.3
        assert profile.open_phases[1].buffer_pct == 3.0

    def test_legacy_flat_params(self):
        from trade_execution import ExecutionParams

        params = ExecutionParams(fill_timeout_seconds=45, aggressive_buffer_pct=5.0)
        profile = _bridge_params_to_profile(params)

        assert profile.name == "_bridged_legacy"
        assert len(profile.open_phases) == 1
        assert profile.open_phases[0].pricing == "aggressive"
        assert profile.open_phases[0].duration_seconds == 45

    def test_fill_manager_accepts_params(self):
        from trade_execution import ExecutionParams, ExecutionPhase

        om, md = _make_om(), _make_md()
        params = ExecutionParams(phases=[
            ExecutionPhase(pricing="aggressive", duration_seconds=30),
        ])
        mgr = FillManager(om, md, params=params, direction="open")
        assert mgr._profile.name == "_bridged"


# =============================================================================
# _make_result correctness
# =============================================================================

class TestMakeResult:
    def test_result_leg_data(self):
        om, md = _make_om(), _make_md()
        mgr = FillManager(om, md, profile=_profile(), direction="open")
        legs = _legs(("A", 0.1, "sell"), ("B", 0.2, "buy"))
        mgr.place_all(legs, lifecycle_id="T1", purpose=OrderPurpose.OPEN_LEG)

        result = mgr._make_result(FillStatus.PENDING)
        assert len(result.legs) == 2
        assert result.legs[0].symbol == "A"
        assert result.legs[1].symbol == "B"
        assert result.phase_total == 1
