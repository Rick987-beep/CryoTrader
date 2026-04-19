"""Tests for execution/currency.py — Price, Currency, OrderbookSnapshot."""

import pytest
from execution.currency import (
    Currency,
    DenominationError,
    OrderbookSnapshot,
    Price,
)


# ── Construction & basics ────────────────────────────────────────────────────

class TestPriceConstruction:
    def test_btc_price(self):
        p = Price(0.05, Currency.BTC)
        assert p.amount == 0.05
        assert p.currency == Currency.BTC

    def test_usd_price(self):
        p = Price(3200.0, Currency.USD)
        assert p.amount == 3200.0
        assert p.currency == Currency.USD

    def test_eth_price(self):
        p = Price(1.5, Currency.ETH)
        assert p.amount == 1.5
        assert p.currency == Currency.ETH

    def test_frozen_immutability(self):
        p = Price(0.05, Currency.BTC)
        with pytest.raises(AttributeError):
            p.amount = 0.10  # type: ignore[misc]
        with pytest.raises(AttributeError):
            p.currency = Currency.USD  # type: ignore[misc]

    def test_repr(self):
        p = Price(0.05, Currency.BTC)
        assert repr(p) == "Price(0.05, BTC)"

    def test_float_conversion(self):
        p = Price(0.05, Currency.BTC)
        assert float(p) == 0.05


# ── Arithmetic ───────────────────────────────────────────────────────────────

class TestPriceArithmetic:
    def test_add_same_currency(self):
        a = Price(0.05, Currency.BTC)
        b = Price(0.03, Currency.BTC)
        result = a + b
        assert result.amount == pytest.approx(0.08)
        assert result.currency == Currency.BTC

    def test_add_mixed_currency_raises(self):
        a = Price(0.05, Currency.BTC)
        b = Price(100.0, Currency.USD)
        with pytest.raises(DenominationError):
            a + b

    def test_sub_same_currency(self):
        a = Price(0.05, Currency.BTC)
        b = Price(0.03, Currency.BTC)
        result = a - b
        assert result.amount == pytest.approx(0.02)

    def test_sub_mixed_currency_raises(self):
        a = Price(0.05, Currency.BTC)
        b = Price(100.0, Currency.USD)
        with pytest.raises(DenominationError):
            a - b

    def test_neg(self):
        p = Price(0.05, Currency.BTC)
        result = -p
        assert result.amount == -0.05
        assert result.currency == Currency.BTC

    def test_mul_scalar(self):
        p = Price(0.05, Currency.BTC)
        result = p * 2.0
        assert result.amount == pytest.approx(0.10)
        assert result.currency == Currency.BTC

    def test_rmul_scalar(self):
        p = Price(0.05, Currency.BTC)
        result = 3.0 * p
        assert result.amount == pytest.approx(0.15)

    def test_mul_price_raises(self):
        a = Price(0.05, Currency.BTC)
        b = Price(0.03, Currency.BTC)
        with pytest.raises(DenominationError):
            a * b  # type: ignore[operator]


# ── Comparison ───────────────────────────────────────────────────────────────

class TestPriceComparison:
    def test_lt(self):
        assert Price(0.03, Currency.BTC) < Price(0.05, Currency.BTC)

    def test_le(self):
        assert Price(0.05, Currency.BTC) <= Price(0.05, Currency.BTC)

    def test_gt(self):
        assert Price(0.05, Currency.BTC) > Price(0.03, Currency.BTC)

    def test_ge(self):
        assert Price(0.05, Currency.BTC) >= Price(0.05, Currency.BTC)

    def test_mixed_currency_compare_raises(self):
        with pytest.raises(DenominationError):
            Price(0.05, Currency.BTC) < Price(100.0, Currency.USD)

    def test_equality(self):
        assert Price(0.05, Currency.BTC) == Price(0.05, Currency.BTC)

    def test_inequality_amount(self):
        assert Price(0.05, Currency.BTC) != Price(0.06, Currency.BTC)

    def test_inequality_currency(self):
        assert Price(0.05, Currency.BTC) != Price(0.05, Currency.USD)


# ── Conversion ───────────────────────────────────────────────────────────────

class TestPriceConversion:
    def test_btc_to_usd(self):
        p = Price(0.05, Currency.BTC)
        result = p.to_usd(80000.0)
        assert result.amount == pytest.approx(4000.0)
        assert result.currency == Currency.USD

    def test_usd_to_usd_noop(self):
        p = Price(3200.0, Currency.USD)
        result = p.to_usd(80000.0)
        assert result is p  # exact same object

    def test_usd_to_btc(self):
        p = Price(4000.0, Currency.USD)
        result = p.to_btc(80000.0)
        assert result.amount == pytest.approx(0.05)
        assert result.currency == Currency.BTC

    def test_btc_to_btc_noop(self):
        p = Price(0.05, Currency.BTC)
        result = p.to_btc(80000.0)
        assert result is p

    def test_usd_to_btc_zero_index_raises(self):
        p = Price(4000.0, Currency.USD)
        with pytest.raises(DenominationError):
            p.to_btc(0)

    def test_eth_to_usd(self):
        p = Price(1.5, Currency.ETH)
        result = p.to_usd(3000.0)
        assert result.amount == pytest.approx(4500.0)


# ── Serialization ────────────────────────────────────────────────────────────

class TestPriceSerialization:
    def test_to_dict(self):
        p = Price(0.05, Currency.BTC)
        d = p.to_dict()
        assert d == {"amount": 0.05, "currency": "BTC"}

    def test_from_dict(self):
        d = {"amount": 0.05, "currency": "BTC"}
        p = Price.from_dict(d)
        assert p == Price(0.05, Currency.BTC)

    def test_round_trip(self):
        original = Price(3200.0, Currency.USD)
        assert Price.from_dict(original.to_dict()) == original


# ── OrderbookSnapshot ────────────────────────────────────────────────────────

class TestOrderbookSnapshot:
    def test_construction(self):
        ob = OrderbookSnapshot(
            symbol="BTC-28MAR26-80000-C",
            currency=Currency.BTC,
            best_bid=0.04,
            best_ask=0.06,
            mark=0.05,
            index_price=80000.0,
            timestamp=1700000000.0,
        )
        assert ob.symbol == "BTC-28MAR26-80000-C"
        assert ob.currency == Currency.BTC
        assert ob.best_bid == 0.04
        assert ob.best_ask == 0.06

    def test_partial_book(self):
        ob = OrderbookSnapshot(
            symbol="BTC-28MAR26-120000-P",
            currency=Currency.BTC,
            best_bid=None,
            best_ask=0.001,
            mark=0.0008,
            index_price=80000.0,
            timestamp=1700000000.0,
        )
        assert ob.best_bid is None
        assert ob.best_ask == 0.001


# ── Phase 3: Format support ──────────────────────────────────────────────────

class TestPriceFormat:
    """Price.__format__ supports f-string formatting."""

    def test_format_fixed(self):
        p = Price(0.0123, Currency.BTC)
        assert f"{p:.4f}" == "0.0123"

    def test_format_default(self):
        p = Price(0.05, Currency.BTC)
        assert f"{p}" == "Price(0.05, BTC)"

    def test_format_2f(self):
        p = Price(3200.0, Currency.USD)
        assert f"{p:.2f}" == "3200.00"
