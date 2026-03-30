"""
Unit tests for option_selection module.

Tests select_option(), resolve_legs(), and structure templates
with mock market data — no exchange calls.
"""

import pytest

from option_selection import (
    LegSpec, resolve_legs, select_option,
    straddle, strangle, _filter_by_expiry,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_instrument(symbol, strike, expiry_ts, option_type="C"):
    return {
        "symbolName": symbol,
        "strike": strike,
        "expirationTimestamp": expiry_ts,
        "tradeSide": option_type,
    }


class FakeMarketData:
    """Minimal mock for option_selection tests."""

    def __init__(self, instruments=None, deltas=None):
        self._instruments = instruments or []
        self._deltas = deltas or {}

    def get_option_instruments(self, underlying="BTC"):
        return self._instruments

    def get_option_market_data(self, symbol):
        delta = self._deltas.get(symbol, 0.5)
        return {"delta": delta, "markPrice": 100.0}

    def get_option_details(self, symbol):
        delta = self._deltas.get(symbol, 0.5)
        return {"delta": delta, "markPrice": 100.0}

    def get_btc_index_price(self):
        return 87000.0


# ── LegSpec dataclass ────────────────────────────────────────────────────

class TestLegSpec:
    def test_construction(self):
        spec = LegSpec(
            option_type="P", side="sell", qty=0.8,
            strike_criteria={"type": "delta", "value": -0.10},
            expiry_criteria={"dte": 1},
        )
        assert spec.option_type == "P"
        assert spec.side == "sell"
        assert spec.qty == 0.8
        assert spec.underlying == "BTC"


# ── Expiry filtering ─────────────────────────────────────────────────────

class TestFilterByExpiry:
    def test_dte_next_picks_nearest(self):
        import time
        now_ms = time.time() * 1000
        instruments = [
            _make_instrument("BTCUSD-29MAR26-90000-C", 90000, now_ms + 86400_000),
            _make_instrument("BTCUSD-29MAR26-95000-C", 95000, now_ms + 86400_000),
            _make_instrument("BTCUSD-05APR26-90000-C", 90000, now_ms + 7 * 86400_000),
        ]
        result = _filter_by_expiry(instruments, {"dte": "next"}, "C")
        assert len(result) == 2
        assert all("29MAR26" in r["symbolName"] for r in result)

    def test_expired_options_excluded(self):
        import time
        now_ms = time.time() * 1000
        instruments = [
            _make_instrument("BTCUSD-27MAR26-90000-C", 90000, now_ms - 86400_000),  # expired
            _make_instrument("BTCUSD-29MAR26-90000-C", 90000, now_ms + 86400_000),
        ]
        result = _filter_by_expiry(instruments, {"dte": "next"}, "C")
        assert len(result) == 1
        assert "29MAR26" in result[0]["symbolName"]


# ── Structure templates ──────────────────────────────────────────────────

class TestStructureTemplates:
    def test_straddle_creates_two_legs(self):
        legs = straddle(qty=0.5, dte=1, side="buy")
        assert len(legs) == 2
        types = {l.option_type for l in legs}
        assert types == {"C", "P"}
        assert all(l.qty == 0.5 for l in legs)

    def test_strangle_creates_two_legs(self):
        legs = strangle(qty=0.3, call_delta=0.25, put_delta=-0.25, dte=1, side="buy")
        assert len(legs) == 2
        call_leg = [l for l in legs if l.option_type == "C"][0]
        put_leg = [l for l in legs if l.option_type == "P"][0]
        assert call_leg.strike_criteria["value"] == 0.25
        assert put_leg.strike_criteria["value"] == -0.25

    def test_strangle_side_mapping(self):
        legs = strangle(qty=0.1, call_delta=0.15, put_delta=-0.15, dte="next", side="sell")
        assert all(l.side == "sell" for l in legs)


# ── resolve_legs ─────────────────────────────────────────────────────────

class TestResolveLegs:
    def test_resolves_to_trade_legs(self):
        import time
        now_ms = time.time() * 1000
        instruments = [
            _make_instrument("BTCUSD-29MAR26-85000-P", 85000, now_ms + 86400_000, "P"),
        ]
        md = FakeMarketData(instruments=instruments, deltas={"BTCUSD-29MAR26-85000-P": -0.10})
        specs = [
            LegSpec(
                option_type="P", side="sell", qty=0.8,
                strike_criteria={"type": "delta", "value": -0.10},
                expiry_criteria={"dte": "next"},
            ),
        ]
        legs = resolve_legs(specs, md)
        assert len(legs) == 1
        assert legs[0].symbol == "BTCUSD-29MAR26-85000-P"
        assert legs[0].qty == 0.8
        assert legs[0].side == "sell"

    def test_raises_on_unresolvable(self):
        md = FakeMarketData(instruments=[])
        specs = [
            LegSpec(
                option_type="C", side="buy", qty=0.1,
                strike_criteria={"type": "delta", "value": 0.25},
                expiry_criteria={"dte": "next"},
            ),
        ]
        with pytest.raises(ValueError):
            resolve_legs(specs, md)
