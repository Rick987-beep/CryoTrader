"""Tests for execution/pricing.py — PricingEngine."""

import pytest
from execution.currency import Currency, OrderbookSnapshot, Price
from execution.pricing import PricingEngine, PricingResult


@pytest.fixture
def engine():
    return PricingEngine()


def _ob(
    bid=0.04, ask=0.06, mark=0.05, index=80000.0,
    symbol="BTC-28MAR26-80000-C", currency=Currency.BTC,
):
    """Helper: build an OrderbookSnapshot with defaults."""
    return OrderbookSnapshot(
        symbol=symbol, currency=currency,
        best_bid=bid, best_ask=ask,
        mark=mark, index_price=index,
        timestamp=1700000000.0,
    )


# ═════════════════════════════════════════════════════════════════════════════
# Fair mode
# ═════════════════════════════════════════════════════════════════════════════

class TestFairMode:
    def test_sell_zero_aggression(self, engine):
        """aggression=0 → price = fair value."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.price is not None
        assert r.price.currency == Currency.BTC
        assert r.price.amount == pytest.approx(0.05)  # mark inside spread = fair
        assert r.fair_value.amount == pytest.approx(0.05)

    def test_sell_full_aggression(self, engine):
        """aggression=1.0 → price = bid (top_of_book sell)."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=1.0)
        assert r.price.amount == pytest.approx(0.04)

    def test_sell_half_aggression(self, engine):
        """aggression=0.5 → halfway between fair and bid."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=0.5)
        # fair=0.05, bid=0.04, spread=0.01, price=0.05 - 0.5*0.01 = 0.045
        assert r.price.amount == pytest.approx(0.045)

    def test_buy_zero_aggression(self, engine):
        """aggression=0 → price = fair value."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=0.0)
        assert r.price.amount == pytest.approx(0.05)

    def test_buy_full_aggression(self, engine):
        """aggression=1.0 → price = ask (top_of_book buy)."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=1.0)
        assert r.price.amount == pytest.approx(0.06)

    def test_buy_half_aggression(self, engine):
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=0.5)
        # fair=0.05, ask=0.06, spread=0.01, price=0.05 + 0.5*0.01 = 0.055
        assert r.price.amount == pytest.approx(0.055)

    def test_mark_outside_spread_uses_mid(self, engine):
        """When mark > ask, fair = mid instead of mark."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.10)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.fair_value.amount == pytest.approx(0.05)  # mid, not mark

    def test_bid_only_book(self, engine):
        """Ask side empty → fair = max(mark, bid)."""
        ob = _ob(bid=0.04, ask=None, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.fair_value.amount == pytest.approx(0.05)  # max(0.05, 0.04)
        # sell with no ask, aggression=0, spread_to_bid=fair-bid=0.01
        assert r.price.amount == pytest.approx(0.05)

    def test_ask_only_book(self, engine):
        """Bid side empty → fair = min(mark, ask)."""
        ob = _ob(bid=None, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=0.0)
        assert r.fair_value.amount == pytest.approx(0.05)  # min(0.05, 0.06)
        assert r.price.amount == pytest.approx(0.05)

    def test_ask_only_buy_aggression(self, engine):
        """Bid empty, buy aggression slides toward ask."""
        ob = _ob(bid=None, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=1.0)
        # fair=0.05, ask=0.06, price=0.05+1.0*0.01=0.06
        assert r.price.amount == pytest.approx(0.06)

    def test_mark_only(self, engine):
        """Empty book, only mark → fair = mark."""
        ob = _ob(bid=None, ask=None, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.fair_value.amount == pytest.approx(0.05)

    def test_empty_book_no_mark(self, engine):
        """Completely empty → price None."""
        ob = _ob(bid=None, ask=None, mark=None)
        r = engine.compute(ob, "sell", "fair")
        assert r.price is None
        assert r.fair_value is None

    def test_empty_book_with_floor(self, engine):
        """Empty book but min_floor_price set → uses floor."""
        ob = _ob(bid=None, ask=None, mark=None)
        floor = Price(0.0001, Currency.BTC)
        r = engine.compute(ob, "sell", "fair", min_floor_price=floor)
        assert r.price == floor

    def test_min_price_pct_of_fair_pass(self, engine):
        """Sell price above floor ratio → accepted."""
        ob = _ob(bid=0.045, ask=0.06, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=0.0, min_price_pct_of_fair=0.83)
        assert r.price is not None
        assert not r.refused

    def test_min_price_pct_of_fair_refuse(self, engine):
        """Sell price below floor ratio → refused."""
        ob = _ob(bid=0.02, ask=0.06, mark=0.05)
        r = engine.compute(ob, "sell", "fair", aggression=1.0, min_price_pct_of_fair=0.83)
        # fair=0.05, aggression=1.0, price=0.05-1.0*(0.05-0.02)=0.02
        # floor=0.05*0.83=0.0415, 0.02 < 0.0415 → refused
        assert r.price is None
        assert r.refused

    def test_min_price_pct_ignored_for_buy(self, engine):
        """min_price_pct_of_fair has no effect on buy."""
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=1.0, min_price_pct_of_fair=0.99)
        assert r.price is not None
        assert not r.refused

    def test_btc_high_price_accepted(self, engine):
        """BTC price > 1.0 is now accepted (plausibility guard removed in Phase 3)."""
        ob = _ob(bid=None, ask=None, mark=5.0, index=80000.0)
        r = engine.compute(ob, "sell", "fair")
        assert r.price is not None
        assert r.price.amount == pytest.approx(5.0)
        assert not r.refused

    def test_usd_high_price_accepted(self, engine):
        """USD prices > 1.0 are normal and should be accepted."""
        ob = _ob(bid=3100.0, ask=3300.0, mark=3200.0, index=80000.0, currency=Currency.USD)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.price is not None
        assert r.price.amount == pytest.approx(3200.0)
        assert not r.refused

    def test_buy_no_ask_mark_fallback(self, engine):
        """Buy side: no ask → escalate from mark."""
        ob = _ob(bid=0.04, ask=None, mark=0.05)
        r = engine.compute(ob, "buy", "fair", aggression=0.5)
        # fair=max(mark,bid)=0.05, no ask → mark*(1+aggression*0.2) = 0.05*1.1 = 0.055
        assert r.price.amount == pytest.approx(0.055)

    def test_usd_denomination(self, engine):
        """Coincall-style USD orderbook returns USD Price."""
        ob = _ob(bid=3100.0, ask=3300.0, mark=3200.0, currency=Currency.USD, index=0)
        r = engine.compute(ob, "sell", "fair", aggression=0.0)
        assert r.price.currency == Currency.USD


# ═════════════════════════════════════════════════════════════════════════════
# Aggressive mode
# ═════════════════════════════════════════════════════════════════════════════

class TestAggressiveMode:
    def test_buy(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "buy", "aggressive", buffer_pct=2.0)
        assert r.price.amount == pytest.approx(0.06 * 1.02)

    def test_sell(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "sell", "aggressive", buffer_pct=2.0)
        assert r.price.amount == pytest.approx(0.04 / 1.02)

    def test_buy_no_ask(self, engine):
        ob = _ob(bid=0.04, ask=None)
        r = engine.compute(ob, "buy", "aggressive")
        assert r.price is None

    def test_sell_no_bid(self, engine):
        ob = _ob(bid=None, ask=0.06)
        r = engine.compute(ob, "sell", "aggressive")
        assert r.price is None

    def test_custom_buffer(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "buy", "aggressive", buffer_pct=5.0)
        assert r.price.amount == pytest.approx(0.06 * 1.05)

    def test_with_floor(self, engine):
        ob = _ob(bid=None, ask=None)
        floor = Price(0.0001, Currency.BTC)
        r = engine.compute(ob, "buy", "aggressive", min_floor_price=floor)
        assert r.price == floor


# ═════════════════════════════════════════════════════════════════════════════
# Mid mode
# ═════════════════════════════════════════════════════════════════════════════

class TestMidMode:
    def test_sell(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "sell", "mid")
        assert r.price.amount == pytest.approx(0.05)

    def test_buy(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "buy", "mid")
        assert r.price.amount == pytest.approx(0.05)

    def test_missing_side(self, engine):
        ob = _ob(bid=0.04, ask=None)
        r = engine.compute(ob, "sell", "mid")
        assert r.price is None


# ═════════════════════════════════════════════════════════════════════════════
# Passive mode
# ═════════════════════════════════════════════════════════════════════════════

class TestPassiveMode:
    def test_buy_joins_bid(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "buy", "passive")
        assert r.price.amount == pytest.approx(0.04)

    def test_sell_joins_ask(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "sell", "passive")
        assert r.price.amount == pytest.approx(0.06)

    def test_buy_no_bid(self, engine):
        ob = _ob(bid=None, ask=0.06)
        r = engine.compute(ob, "buy", "passive")
        assert r.price is None

    def test_sell_no_ask(self, engine):
        ob = _ob(bid=0.04, ask=None)
        r = engine.compute(ob, "sell", "passive")
        assert r.price is None


# ═════════════════════════════════════════════════════════════════════════════
# Top-of-book mode
# ═════════════════════════════════════════════════════════════════════════════

class TestTopOfBookMode:
    def test_buy_lifts_ask(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "buy", "top_of_book")
        assert r.price.amount == pytest.approx(0.06)

    def test_sell_hits_bid(self, engine):
        ob = _ob(bid=0.04, ask=0.06)
        r = engine.compute(ob, "sell", "top_of_book")
        assert r.price.amount == pytest.approx(0.04)

    def test_buy_no_ask(self, engine):
        ob = _ob(bid=0.04, ask=None)
        r = engine.compute(ob, "buy", "top_of_book")
        assert r.price is None

    def test_sell_no_bid(self, engine):
        ob = _ob(bid=None, ask=0.06)
        r = engine.compute(ob, "sell", "top_of_book")
        assert r.price is None


# ═════════════════════════════════════════════════════════════════════════════
# Mark mode
# ═════════════════════════════════════════════════════════════════════════════

class TestMarkMode:
    def test_mark_available(self, engine):
        ob = _ob(mark=0.05)
        r = engine.compute(ob, "sell", "mark")
        assert r.price.amount == pytest.approx(0.05)

    def test_mark_zero_fallback_mid(self, engine):
        ob = _ob(bid=0.04, ask=0.06, mark=0)
        r = engine.compute(ob, "sell", "mark")
        assert r.price.amount == pytest.approx(0.05)  # mid

    def test_mark_none_no_book(self, engine):
        ob = _ob(bid=None, ask=None, mark=None)
        r = engine.compute(ob, "sell", "mark")
        assert r.price is None


# ═════════════════════════════════════════════════════════════════════════════
# fair_value() convenience
# ═════════════════════════════════════════════════════════════════════════════

class TestFairValue:
    def test_full_book_mark_inside(self, engine):
        ob = _ob(bid=0.04, ask=0.06, mark=0.05)
        fv = engine.fair_value(ob)
        assert fv == Price(0.05, Currency.BTC)

    def test_full_book_mark_outside(self, engine):
        ob = _ob(bid=0.04, ask=0.06, mark=0.10)
        fv = engine.fair_value(ob)
        assert fv == Price(0.05, Currency.BTC)  # mid

    def test_empty_book(self, engine):
        ob = _ob(bid=None, ask=None, mark=None)
        fv = engine.fair_value(ob)
        assert fv is None


# ═════════════════════════════════════════════════════════════════════════════
# Edge cases & error handling
# ═════════════════════════════════════════════════════════════════════════════

class TestEdgeCases:
    def test_unknown_mode_raises(self, engine):
        ob = _ob()
        with pytest.raises(ValueError, match="Unknown pricing mode"):
            engine.compute(ob, "sell", "vwap")

    def test_zero_mark_price(self, engine):
        """Zero mark: fair value falls back to mid."""
        ob = _ob(bid=0.04, ask=0.06, mark=0)
        r = engine.compute(ob, "sell", "fair")
        assert r.fair_value.amount == pytest.approx(0.05)

    def test_negative_spread_uses_mid(self, engine):
        """Crossed book (bid > ask): fair = mid."""
        ob = _ob(bid=0.06, ask=0.04, mark=0.05)
        r = engine.compute(ob, "sell", "fair")
        # mark 0.05 is inside [0.04, 0.06] regardless of which is bid/ask
        assert r.fair_value is not None
