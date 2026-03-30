"""
Unit tests for LimitFillManager — phased & legacy pricing, fills, requoting.

Uses MockExecutor + MockMarketData from conftest — no API calls.
"""

import time

import pytest

from trade_execution import (
    ExecutionParams,
    ExecutionPhase,
    LimitFillManager,
    _LegFillState,
)
from tests.conftest import MockExecutor, MockMarketData


def make_mgr(executor=None, market_data=None, params=None, order_manager=None):
    """Create a LimitFillManager with optional overrides."""
    ex = executor or MockExecutor()
    md = market_data or MockMarketData()
    return LimitFillManager(ex, params=params, order_manager=order_manager, market_data=md), ex, md


def make_legs(*specs):
    """Build a list of TradeLeg-like objects (dicts with symbol/qty/side)."""
    from trade_lifecycle import TradeLeg
    result = []
    for s in specs:
        if isinstance(s, tuple):
            sym, qty, side = s
        else:
            sym, qty, side = s["symbol"], s["qty"], s["side"]
        result.append(TradeLeg(symbol=sym, qty=qty, side=side))
    return result


# =============================================================================
# LegFillState properties
# =============================================================================

class TestLegFillState:
    def test_is_filled_when_enough_qty(self):
        ls = _LegFillState(symbol="X", qty=1.0, side="buy", filled_qty=1.0)
        assert ls.is_filled

    def test_not_filled_when_partial(self):
        ls = _LegFillState(symbol="X", qty=1.0, side="buy", filled_qty=0.5)
        assert not ls.is_filled

    def test_remaining_qty(self):
        ls = _LegFillState(symbol="X", qty=1.0, side="buy", filled_qty=0.3)
        assert abs(ls.remaining_qty - 0.7) < 1e-9

    def test_remaining_qty_never_negative(self):
        ls = _LegFillState(symbol="X", qty=1.0, side="buy", filled_qty=1.5)
        assert ls.remaining_qty == 0.0


# =============================================================================
# place_all — basic placement
# =============================================================================

class TestPlaceAll:
    def test_single_leg_placed(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        assert mgr.place_all(legs) is True
        assert len(mgr.filled_legs) == 1

    def test_multi_leg_placed(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        md.set_orderbook("SYM-P", bids=[{"price": "50"}], asks=[{"price": "55"}])
        legs = make_legs(("SYM-C", 0.1, "buy"), ("SYM-P", 0.1, "sell"))
        assert mgr.place_all(legs) is True
        assert len(mgr.filled_legs) == 2

    def test_no_orderbook_fails_atomic(self):
        mgr, ex, md = make_mgr()
        # no orderbook set
        legs = make_legs(("SYM-C", 0.1, "buy"))
        assert mgr.place_all(legs) is False
        assert len(mgr.filled_legs) == 0

    def test_no_orderbook_skips_best_effort(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        # SYM-P has no orderbook
        legs = make_legs(("SYM-C", 0.1, "buy"), ("SYM-P", 0.1, "sell"))
        assert mgr.place_all(legs, best_effort=True) is True
        assert len(mgr.filled_legs) == 1
        assert mgr.has_skipped_legs
        assert "SYM-P" in mgr.skipped_symbols


# =============================================================================
# place_all — atomic vs best_effort
# =============================================================================

class TestAtomicVsBestEffort:
    def test_atomic_cancels_all_on_second_failure(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        # SYM-P has no price → second leg fails
        legs = make_legs(("SYM-C", 0.1, "buy"), ("SYM-P", 0.1, "sell"))
        # First leg will succeed (priced), second has no price → atomic abort
        # Since price validation happens BEFORE placement, this actually returns False
        # without placing any orders
        result = mgr.place_all(legs, best_effort=False)
        assert result is False

    def test_best_effort_all_skipped_returns_false(self):
        mgr, ex, md = make_mgr()
        # neither has orderbook
        legs = make_legs(("SYM-C", 0.1, "buy"), ("SYM-P", 0.1, "sell"))
        assert mgr.place_all(legs, best_effort=True) is False

    def test_best_effort_places_what_it_can(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"), ("SYM-P", 0.1, "sell"))
        assert mgr.place_all(legs, best_effort=True) is True
        assert len(mgr.filled_legs) == 1
        assert mgr.skipped_symbols == ["SYM-P"]


# =============================================================================
# check() — fill detection
# =============================================================================

class TestCheckFills:
    def test_returns_filled_when_all_filled(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        # Simulate fill on the order
        oid = mgr.filled_legs[0].order_id
        ex.simulate_fill(oid, 0.1, 104.0)

        assert mgr.check() == "filled"

    def test_returns_pending_before_timeout(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)
        # No fill, but within timeout
        assert mgr.check() == "pending"

    def test_all_filled_property(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)
        assert not mgr.all_filled
        oid = mgr.filled_legs[0].order_id
        ex.simulate_fill(oid, 0.1, 104.0)
        mgr._poll_fills()
        assert mgr.all_filled


# =============================================================================
# Legacy mode — timeout-based requoting
# =============================================================================

class TestLegacyMode:
    def test_requotes_after_timeout(self):
        params = ExecutionParams(fill_timeout_seconds=0.01, max_requote_rounds=3)
        mgr, ex, md = make_mgr(params=params)
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        time.sleep(0.02)
        result = mgr.check()
        assert result == "requoted"

    def test_fails_after_max_requote_rounds(self):
        params = ExecutionParams(fill_timeout_seconds=0.01, max_requote_rounds=1)
        mgr, ex, md = make_mgr(params=params)
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        time.sleep(0.02)
        mgr.check()  # requoted (round 1)
        time.sleep(0.02)
        result = mgr.check()
        assert result == "failed"


# =============================================================================
# Phased mode — multi-phase advancement
# =============================================================================

class TestPhasedMode:
    def test_advances_phases(self):
        phases = [
            ExecutionPhase(pricing="passive", duration_seconds=10, reprice_interval=100),
            ExecutionPhase(pricing="aggressive", duration_seconds=10, reprice_interval=100),
        ]
        params = ExecutionParams(phases=phases)
        mgr, ex, md = make_mgr(params=params)
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        # Force phase 1 to expire
        mgr._phase_started_at = time.time() - 20
        result = mgr.check()
        assert result == "requoted"
        assert mgr._phase_index == 1

    def test_fails_when_all_phases_exhausted(self):
        phases = [
            ExecutionPhase(pricing="passive", duration_seconds=10, reprice_interval=100),
        ]
        params = ExecutionParams(phases=phases)
        mgr, ex, md = make_mgr(params=params)
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        # Expire the only phase
        mgr._phase_started_at = time.time() - 20
        result = mgr.check()
        assert result == "failed"

    def test_within_phase_reprice(self):
        phases = [
            ExecutionPhase(pricing="aggressive", duration_seconds=60,
                           reprice_interval=10),
        ]
        params = ExecutionParams(phases=phases)
        mgr, ex, md = make_mgr(params=params)
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)

        # Trigger within-phase reprice (reprice_interval expired, phase not expired)
        mgr._last_reprice_at = time.time() - 15
        result = mgr.check()
        assert result == "requoted"
        assert mgr._phase_index == 0  # still in phase 0

    def test_current_phase_property(self):
        phases = [
            ExecutionPhase(pricing="passive", duration_seconds=10),
            ExecutionPhase(pricing="aggressive", duration_seconds=10),
        ]
        params = ExecutionParams(phases=phases)
        mgr, ex, md = make_mgr(params=params)

        assert mgr._current_phase.pricing == "passive"
        mgr._phase_index = 1
        assert mgr._current_phase.pricing == "aggressive"
        mgr._phase_index = 2
        assert mgr._current_phase is None


# =============================================================================
# Pricing strategies
# =============================================================================

class TestPricingStrategies:
    def setup_method(self):
        self.md = MockMarketData()
        self.md.set_orderbook("SYM", bids=[{"price": "100"}], asks=[{"price": "110"}])

    def _price(self, pricing, side, **kwargs):
        phase = ExecutionPhase(pricing=pricing, **kwargs)
        params = ExecutionParams(phases=[phase])
        mgr, _, _ = make_mgr(market_data=self.md, params=params)
        return mgr._get_phased_price("SYM", side, phase)

    def test_aggressive_buy(self):
        price = self._price("aggressive", "buy", buffer_pct=5.0)
        assert price == 110 * 1.05

    def test_aggressive_sell(self):
        price = self._price("aggressive", "sell", buffer_pct=5.0)
        assert price == 100 / 1.05

    def test_mid(self):
        price = self._price("mid", "buy")
        assert price == 105.0

    def test_passive_buy(self):
        price = self._price("passive", "buy")
        assert price == 100.0  # best bid

    def test_passive_sell(self):
        price = self._price("passive", "sell")
        assert price == 110.0  # best ask

    def test_top_of_book_buy(self):
        price = self._price("top_of_book", "buy")
        assert price == 110.0  # best ask

    def test_top_of_book_sell(self):
        price = self._price("top_of_book", "sell")
        assert price == 100.0  # best bid

    def test_mark_uses_mark_field(self):
        self.md._orderbooks["SYM"]["mark"] = 104.0
        price = self._price("mark", "buy")
        assert price == 104.0

    def test_mark_falls_back_to_mid(self):
        price = self._price("mark", "buy")
        assert price == 105.0

    def test_fair_no_aggression(self):
        self.md._orderbooks["SYM"]["mark"] = 105.0
        price = self._price("fair", "sell", fair_aggression=0.0)
        assert price == 105.0  # fair = mark (within bid/ask)

    def test_fair_full_aggression_sell(self):
        self.md._orderbooks["SYM"]["mark"] = 105.0
        price = self._price("fair", "sell", fair_aggression=1.0)
        # fair=105, bid=100, spread=5, price = 105 - 1.0*5 = 100
        assert price == 100.0

    def test_fair_full_aggression_buy(self):
        self.md._orderbooks["SYM"]["mark"] = 105.0
        price = self._price("fair", "buy", fair_aggression=1.0)
        # fair=105, ask=110, spread=5, price = 105 + 1.0*5 = 110
        assert price == 110.0


# =============================================================================
# Fair pricing — min_price_pct_of_fair floor
# =============================================================================

class TestFairPriceFloor:
    def test_floor_blocks_below_threshold(self):
        md = MockMarketData()
        md.set_orderbook("SYM", bids=[{"price": "100"}], asks=[{"price": "110"}])
        md._orderbooks["SYM"]["mark"] = 105.0

        # Fair sell with high aggression, but a floor that will block it
        phase = ExecutionPhase(
            pricing="fair", fair_aggression=1.0,
            min_price_pct_of_fair=0.99,  # floor = 105 * 0.99 = 103.95
        )
        params = ExecutionParams(phases=[phase])
        mgr, _, _ = make_mgr(market_data=md, params=params)
        # price would be 100 (fair - spread), but floor is 103.95 → None
        price = mgr._get_phased_price("SYM", "sell", phase)
        assert price is None

    def test_floor_allows_above_threshold(self):
        md = MockMarketData()
        md.set_orderbook("SYM", bids=[{"price": "100"}], asks=[{"price": "110"}])
        md._orderbooks["SYM"]["mark"] = 105.0

        phase = ExecutionPhase(
            pricing="fair", fair_aggression=0.0,
            min_price_pct_of_fair=0.50,  # floor = 52.5
        )
        params = ExecutionParams(phases=[phase])
        mgr, _, _ = make_mgr(market_data=md, params=params)
        price = mgr._get_phased_price("SYM", "sell", phase)
        assert price == 105.0  # not blocked


# =============================================================================
# min_floor_price fallback
# =============================================================================

class TestMinFloorPrice:
    def test_floor_price_used_when_no_price(self):
        md = MockMarketData()
        md.set_orderbook("SYM", bids=[], asks=[])  # empty orderbook

        phase = ExecutionPhase(
            pricing="aggressive",
            min_floor_price=0.0001,
        )
        params = ExecutionParams(phases=[phase])
        mgr, _, _ = make_mgr(market_data=md, params=params)
        price = mgr._get_phased_price("SYM", "sell", phase)
        assert price == 0.0001

    def test_floor_price_not_used_when_valid_price(self):
        md = MockMarketData()
        md.set_orderbook("SYM", bids=[{"price": "100"}], asks=[{"price": "110"}])

        phase = ExecutionPhase(
            pricing="aggressive",
            min_floor_price=0.0001,
        )
        params = ExecutionParams(phases=[phase])
        mgr, _, _ = make_mgr(market_data=md, params=params)
        price = mgr._get_phased_price("SYM", "sell", phase)
        assert price > 0.0001  # should use normal aggressive price


# =============================================================================
# cancel_all
# =============================================================================

class TestCancelAll:
    def test_cancels_unfilled_orders(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)
        mgr.cancel_all()

        cancel_calls = [c for c in ex.calls if c[0] == "cancel_order"]
        assert len(cancel_calls) == 1

    def test_does_not_cancel_filled_orders(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)
        oid = mgr.filled_legs[0].order_id
        ex.simulate_fill(oid, 0.1, 104.0)
        mgr._poll_fills()  # mark as filled

        ex.calls.clear()
        mgr.cancel_all()
        cancel_calls = [c for c in ex.calls if c[0] == "cancel_order"]
        assert len(cancel_calls) == 0


# =============================================================================
# Order ID written back to leg
# =============================================================================

class TestOrderIdWriteback:
    def test_order_id_written_to_leg(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "buy"))
        mgr.place_all(legs)
        assert legs[0].order_id is not None
        assert legs[0].order_id != ""


# =============================================================================
# reduce_only passthrough
# =============================================================================

class TestReduceOnly:
    def test_reduce_only_passed_to_executor(self):
        mgr, ex, md = make_mgr()
        md.set_orderbook("SYM-C", bids=[{"price": "100"}], asks=[{"price": "105"}])
        legs = make_legs(("SYM-C", 0.1, "sell"))
        mgr.place_all(legs, reduce_only=True)
        place_calls = [c for c in ex.calls if c[0] == "place_order"]
        assert place_calls[0][1]["reduce_only"] is True
