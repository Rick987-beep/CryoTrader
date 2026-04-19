"""
test_multileg_execution.py — Scaffolding for multi-leg fill testing.

Phase 1 scaffolding only: data structures for 3-leg butterfly and
4-leg iron condor.  Actual fill-manager integration tests come in Phase 2.
"""

import pytest
from execution.currency import Currency, OrderbookSnapshot, Price
from execution.fill_result import FillResult, FillStatus, LegFillSnapshot


# ═════════════════════════════════════════════════════════════════════════════
# Test data helpers
# ═════════════════════════════════════════════════════════════════════════════

def _make_ob(symbol: str, bid=0.04, ask=0.06, mark=0.05, currency=Currency.BTC):
    return OrderbookSnapshot(
        symbol=symbol, currency=currency,
        best_bid=bid, best_ask=ask,
        mark=mark, index_price=80000.0,
        timestamp=1700000000.0,
    )


def _make_leg(
    symbol: str, side: str, qty: float,
    filled: bool = False,
    fill_price: float = 0.0,
    fee: float = 0.0,
    skipped: bool = False,
) -> LegFillSnapshot:
    return LegFillSnapshot(
        symbol=symbol,
        side=side,
        qty=qty,
        filled_qty=qty if filled else 0.0,
        fill_price=Price(fill_price, Currency.BTC) if fill_price else None,
        order_id=None,
        skipped=skipped,
        fee=Price(fee, Currency.BTC) if fee else None,
    )


def _make_result(status: FillStatus, legs: list) -> FillResult:
    return FillResult(
        status=status, legs=legs,
        phase_index=0, phase_total=1,
        phase_pricing="fair", elapsed_seconds=0.0,
    )


# ═════════════════════════════════════════════════════════════════════════════
# 3-leg butterfly data
# ═════════════════════════════════════════════════════════════════════════════

BUTTERFLY_LEGS = [
    {"symbol": "BTC-28MAR26-75000-C", "side": "buy", "qty": 1},  # wing
    {"symbol": "BTC-28MAR26-80000-C", "side": "sell", "qty": 2},  # body
    {"symbol": "BTC-28MAR26-85000-C", "side": "buy", "qty": 1},  # wing
]

BUTTERFLY_ORDERBOOKS = {
    leg["symbol"]: _make_ob(leg["symbol"], bid=0.04 + i * 0.01, ask=0.06 + i * 0.01)
    for i, leg in enumerate(BUTTERFLY_LEGS)
}


class TestButterflyScaffold:
    """Verify 3-leg butterfly data structures are well-formed."""

    def test_butterfly_has_3_legs(self):
        assert len(BUTTERFLY_LEGS) == 3

    def test_butterfly_sides(self):
        sides = [l["side"] for l in BUTTERFLY_LEGS]
        assert sides == ["buy", "sell", "buy"]

    def test_butterfly_body_double_qty(self):
        assert BUTTERFLY_LEGS[1]["qty"] == 2

    def test_butterfly_fill_result_all_filled(self):
        legs = [
            _make_leg(l["symbol"], l["side"], l["qty"],
                      filled=True, fill_price=0.05, fee=0.0001)
            for l in BUTTERFLY_LEGS
        ]
        result = _make_result(FillStatus.FILLED, legs)
        assert result.all_filled
        assert not result.has_skipped
        assert result.skipped_symbols == []

    def test_butterfly_fill_result_partial_skipped(self):
        """Skipped legs are excluded from all_filled; has_skipped detects them."""
        legs = [
            _make_leg(BUTTERFLY_LEGS[0]["symbol"], "buy", 1,
                      filled=True, fill_price=0.05),
            _make_leg(BUTTERFLY_LEGS[1]["symbol"], "sell", 2,
                      skipped=True),
            _make_leg(BUTTERFLY_LEGS[2]["symbol"], "buy", 1,
                      filled=True, fill_price=0.06),
        ]
        result = _make_result(FillStatus.PARTIAL, legs)
        # non-skipped legs are all filled, but has_skipped=True
        assert result.all_filled  # only checks non-skipped
        assert result.has_skipped
        assert BUTTERFLY_LEGS[1]["symbol"] in result.skipped_symbols

    def test_butterfly_fill_result_unfilled(self):
        """Unfilled (not skipped) leg blocks all_filled."""
        legs = [
            _make_leg(BUTTERFLY_LEGS[0]["symbol"], "buy", 1,
                      filled=True, fill_price=0.05),
            _make_leg(BUTTERFLY_LEGS[1]["symbol"], "sell", 2),  # not filled, not skipped
            _make_leg(BUTTERFLY_LEGS[2]["symbol"], "buy", 1,
                      filled=True, fill_price=0.06),
        ]
        result = _make_result(FillStatus.PARTIAL, legs)
        assert not result.all_filled


# ═════════════════════════════════════════════════════════════════════════════
# 4-leg iron condor data
# ═════════════════════════════════════════════════════════════════════════════

IRON_CONDOR_LEGS = [
    {"symbol": "BTC-28MAR26-70000-P", "side": "buy", "qty": 1},   # long OTM put
    {"symbol": "BTC-28MAR26-75000-P", "side": "sell", "qty": 1},  # short ATM put
    {"symbol": "BTC-28MAR26-85000-C", "side": "sell", "qty": 1},  # short ATM call
    {"symbol": "BTC-28MAR26-90000-C", "side": "buy", "qty": 1},   # long OTM call
]

IRON_CONDOR_ORDERBOOKS = {
    leg["symbol"]: _make_ob(leg["symbol"], bid=0.02 + i * 0.005, ask=0.04 + i * 0.005)
    for i, leg in enumerate(IRON_CONDOR_LEGS)
}


class TestIronCondorScaffold:
    """Verify 4-leg iron condor data structures."""

    def test_iron_condor_has_4_legs(self):
        assert len(IRON_CONDOR_LEGS) == 4

    def test_iron_condor_sides(self):
        sides = [l["side"] for l in IRON_CONDOR_LEGS]
        assert sides == ["buy", "sell", "sell", "buy"]

    def test_iron_condor_all_qty_1(self):
        assert all(l["qty"] == 1 for l in IRON_CONDOR_LEGS)

    def test_iron_condor_fill_result_all_filled(self):
        legs = [
            _make_leg(l["symbol"], l["side"], l["qty"],
                      filled=True, fill_price=0.03 + i * 0.005, fee=0.0001)
            for i, l in enumerate(IRON_CONDOR_LEGS)
        ]
        result = _make_result(FillStatus.FILLED, legs)
        assert result.all_filled
        assert len(result.legs) == 4

    def test_iron_condor_3_of_4_skipped(self):
        """3 filled + 1 skipped: all non-skipped filled, has_skipped=True."""
        legs = [
            _make_leg(IRON_CONDOR_LEGS[0]["symbol"], "buy", 1,
                      filled=True, fill_price=0.03),
            _make_leg(IRON_CONDOR_LEGS[1]["symbol"], "sell", 1,
                      filled=True, fill_price=0.04),
            _make_leg(IRON_CONDOR_LEGS[2]["symbol"], "sell", 1,
                      filled=True, fill_price=0.035),
            _make_leg(IRON_CONDOR_LEGS[3]["symbol"], "buy", 1,
                      skipped=True),
        ]
        result = _make_result(FillStatus.PARTIAL, legs)
        assert result.all_filled  # non-skipped all filled
        assert result.has_skipped
        assert IRON_CONDOR_LEGS[3]["symbol"] in result.skipped_symbols

    def test_iron_condor_3_of_4_unfilled(self):
        """3 filled + 1 unfilled (not skipped): all_filled=False."""
        legs = [
            _make_leg(IRON_CONDOR_LEGS[0]["symbol"], "buy", 1,
                      filled=True, fill_price=0.03),
            _make_leg(IRON_CONDOR_LEGS[1]["symbol"], "sell", 1,
                      filled=True, fill_price=0.04),
            _make_leg(IRON_CONDOR_LEGS[2]["symbol"], "sell", 1,
                      filled=True, fill_price=0.035),
            _make_leg(IRON_CONDOR_LEGS[3]["symbol"], "buy", 1),  # unfilled
        ]
        result = _make_result(FillStatus.PARTIAL, legs)
        assert not result.all_filled
