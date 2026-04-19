"""Tests for execution/fees.py — fee extraction and summation."""

import pytest
from execution.currency import Currency, DenominationError, Price
from execution.fees import extract_fee, sum_fees


class TestExtractFee:
    def test_single_trade_fee(self):
        trades = [{"fee": 0.0003, "fee_currency": "BTC"}]
        result = extract_fee(trades, Currency.BTC)
        assert result == Price(0.0003, Currency.BTC)

    def test_multiple_trades_sum(self):
        trades = [
            {"fee": 0.0003, "fee_currency": "BTC"},
            {"fee": 0.0002, "fee_currency": "BTC"},
        ]
        result = extract_fee(trades, Currency.BTC)
        assert result.amount == pytest.approx(0.0005)
        assert result.currency == Currency.BTC

    def test_empty_trades(self):
        assert extract_fee([], Currency.BTC) is None

    def test_none_trades(self):
        assert extract_fee(None, Currency.BTC) is None

    def test_zero_fees(self):
        trades = [{"fee": 0, "fee_currency": "BTC"}]
        assert extract_fee(trades, Currency.BTC) is None

    def test_missing_fee_key(self):
        trades = [{"other_field": 123}]
        assert extract_fee(trades, Currency.BTC) is None

    def test_negative_fee_uses_abs(self):
        """Deribit maker rebates are negative; we take absolute value."""
        trades = [{"fee": -0.0001, "fee_currency": "BTC"}]
        result = extract_fee(trades, Currency.BTC)
        assert result.amount == pytest.approx(0.0001)

    def test_usd_denomination(self):
        trades = [{"fee": 1.50, "fee_currency": "USD"}]
        result = extract_fee(trades, Currency.USD)
        assert result == Price(1.50, Currency.USD)


class TestSumFees:
    def test_sum_two_fees(self):
        fees = [Price(0.0003, Currency.BTC), Price(0.0002, Currency.BTC)]
        result = sum_fees(fees)
        assert result.amount == pytest.approx(0.0005)
        assert result.currency == Currency.BTC

    def test_sum_with_nones(self):
        fees = [None, Price(0.0003, Currency.BTC), None]
        result = sum_fees(fees)
        assert result == Price(0.0003, Currency.BTC)

    def test_all_nones(self):
        assert sum_fees([None, None]) is None

    def test_empty_list(self):
        assert sum_fees([]) is None

    def test_mixed_currencies_raises(self):
        fees = [Price(0.0003, Currency.BTC), Price(1.0, Currency.USD)]
        with pytest.raises(DenominationError):
            sum_fees(fees)

    def test_single_fee(self):
        fees = [Price(0.0003, Currency.BTC)]
        result = sum_fees(fees)
        assert result == Price(0.0003, Currency.BTC)
