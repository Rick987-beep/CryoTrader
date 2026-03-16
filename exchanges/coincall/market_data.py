"""Coincall market data adapter — wraps existing MarketData."""

from exchanges.base import ExchangeMarketData
from market_data import MarketData


class CoincallMarketDataAdapter(ExchangeMarketData):
    """Thin wrapper around MarketData implementing ExchangeMarketData interface."""

    def __init__(self):
        self._inner = MarketData()

    def get_index_price(self, underlying="BTC"):
        return self._inner.get_btc_index_price()

    def get_option_instruments(self, underlying="BTC"):
        return self._inner.get_option_instruments(underlying)

    def get_option_details(self, symbol):
        return self._inner.get_option_details(symbol)

    def get_option_orderbook(self, symbol):
        from market_data import get_option_orderbook
        return get_option_orderbook(symbol)
