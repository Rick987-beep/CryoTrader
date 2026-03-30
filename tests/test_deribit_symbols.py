"""
Unit tests for Deribit symbol translation and tick snapping — pure, no API calls.

Extracted from test_deribit_integration.py.
"""

from exchanges.deribit.symbols import (
    parse_deribit_symbol,
    build_deribit_symbol,
    coincall_to_deribit,
    deribit_to_coincall,
)


class TestParseDeribitSymbol:
    def test_standard_symbol(self):
        result = parse_deribit_symbol("BTC-28MAR26-100000-C")
        assert result is not None
        assert result["underlying"] == "BTC"
        assert result["day"] == "28"
        assert result["month"] == "MAR"
        assert result["year"] == "26"
        assert result["strike"] == "100000"
        assert result["option_type"] == "C"

    def test_single_digit_day(self):
        result = parse_deribit_symbol("BTC-3APR26-74000-P")
        assert result is not None
        assert result["day"] == "3"
        assert result["month"] == "APR"

    def test_reject_perpetual(self):
        assert parse_deribit_symbol("BTC-PERPETUAL") is None

    def test_reject_future(self):
        assert parse_deribit_symbol("BTC-28MAR26") is None


class TestBuildDeribitSymbol:
    def test_build_unpadded_day(self):
        sym = build_deribit_symbol("BTC", "03", "APR", "26", "74000", "C")
        assert sym == "BTC-3APR26-74000-C"


class TestSymbolConversion:
    def test_coincall_to_deribit(self):
        assert coincall_to_deribit("BTCUSD-03APR26-74000-C") == "BTC-3APR26-74000-C"
        assert coincall_to_deribit("BTCUSD-28MAR26-100000-P") == "BTC-28MAR26-100000-P"

    def test_deribit_to_coincall(self):
        assert deribit_to_coincall("BTC-3APR26-74000-C") == "BTCUSD-03APR26-74000-C"
        assert deribit_to_coincall("BTC-28MAR26-100000-P") == "BTCUSD-28MAR26-100000-P"


class TestTickSizeSnap:
    def test_below_threshold(self):
        from exchanges.deribit.executor import _snap_to_tick
        assert _snap_to_tick(0.0035) == 0.0035
        assert _snap_to_tick(0.00351) == 0.0035

    def test_above_threshold(self):
        from exchanges.deribit.executor import _snap_to_tick
        assert _snap_to_tick(0.005) == 0.005
        assert _snap_to_tick(0.0053) == 0.005
        assert _snap_to_tick(0.021) == 0.021
        assert _snap_to_tick(0.0212) == 0.021

    def test_sub_tick_clamps_to_minimum(self):
        from exchanges.deribit.executor import _snap_to_tick
        assert _snap_to_tick(0.000098) == 0.0001
        assert _snap_to_tick(0.00005) == 0.0001
        assert _snap_to_tick(0.00001) == 0.0001
