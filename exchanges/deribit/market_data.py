"""
Deribit Market Data Adapter

Implements ExchangeMarketData for Deribit using REST JSON-RPC.
Normalizes instrument and ticker responses to the field names the
rest of the system expects (Coincall-compatible shapes):

  instruments → symbolName, strike, expirationTimestamp
  details     → delta, indexPrice, markPrice (USD), bid (USD), ask (USD), impliedVolatility

All BTC-denominated prices are converted to USD at the adapter boundary.
"""

import logging
import time
from typing import Any, Dict, List, Optional

from exchanges.base import ExchangeMarketData

logger = logging.getLogger(__name__)


class DeribitMarketDataAdapter(ExchangeMarketData):
    """Deribit market data with Coincall-compatible response shapes."""

    def __init__(self, auth):
        self._auth = auth
        self._index_cache = None
        self._index_cache_time = 0.0

    # ── ExchangeMarketData interface ─────────────────────────────────

    def get_index_price(self, underlying: str = "BTC") -> Optional[float]:
        index_name = "btc_usd" if underlying.upper() == "BTC" else "eth_usd"
        # 10s cache to reduce API load during burst queries
        now = time.time()
        if self._index_cache and now - self._index_cache_time < 10:
            return self._index_cache

        resp = self._auth.call("public/get_index_price", {"index_name": index_name})
        if "result" not in resp:
            logger.warning(f"Deribit get_index_price failed: {resp.get('error')}")
            return self._index_cache  # return stale cache on failure
        price = resp["result"].get("index_price")
        if price and price > 0:
            self._index_cache = float(price)
            self._index_cache_time = now
        return self._index_cache

    def get_option_instruments(self, underlying: str = "BTC") -> Optional[List[Dict[str, Any]]]:
        """
        Get all active option instruments.

        Returns list of dicts normalized to Coincall field names:
          symbolName, strike, expirationTimestamp, option_type,
          min_trade_amount, tick_size, tick_size_steps
        """
        currency = underlying.upper()
        resp = self._auth.call("public/get_instruments", {
            "currency": currency,
            "kind": "option",
            "expired": False,
        })
        if "result" not in resp:
            logger.warning(f"Deribit get_instruments failed: {resp.get('error')}")
            return None

        instruments = resp["result"]
        normalized = []
        for inst in instruments:
            name = inst.get("instrument_name", "")
            # Skip non-option instruments
            if not (name.endswith("-C") or name.endswith("-P")):
                continue
            normalized.append({
                "symbolName": name,
                "strike": float(inst.get("strike", 0)),
                "expirationTimestamp": inst.get("expiration_timestamp", 0),
                "option_type": "call" if name.endswith("-C") else "put",
                "min_trade_amount": inst.get("min_trade_amount", 0.1),
                "tick_size": inst.get("tick_size", 0.0001),
                "tick_size_steps": inst.get("tick_size_steps", []),
                "contract_size": inst.get("contract_size", 1.0),
                "block_trade_min_trade_amount": inst.get("block_trade_min_trade_amount", 25),
                # Keep raw Deribit name for internal use
                "_instrument_name": name,
            })
        logger.debug(f"Deribit: fetched {len(normalized)} option instruments for {currency}")
        return normalized if normalized else None

    def get_option_details(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get ticker data for an option, normalized to Coincall field names.

        Prices are converted from BTC to USD using the bundled index_price.
        """
        resp = self._auth.call("public/ticker", {"instrument_name": symbol})
        if "result" not in resp:
            logger.debug(f"Deribit ticker failed for {symbol}: {resp.get('error')}")
            return None

        t = resp["result"]
        index_price = float(t.get("index_price", 0))
        greeks = t.get("greeks", {})

        # BTC → USD conversion for prices
        mark_btc = float(t.get("mark_price", 0))
        bid_btc = float(t.get("best_bid_price", 0))
        ask_btc = float(t.get("best_ask_price", 0))

        return {
            "symbolName": symbol,
            "markPrice": mark_btc * index_price if index_price else 0,
            "bid": bid_btc * index_price if index_price else 0,
            "ask": ask_btc * index_price if index_price else 0,
            "delta": float(greeks.get("delta", 0)),
            "gamma": float(greeks.get("gamma", 0)),
            "theta": float(greeks.get("theta", 0)),
            "vega": float(greeks.get("vega", 0)),
            "indexPrice": index_price,
            "impliedVolatility": float(t.get("mark_iv", 0)),
            "openInterest": float(t.get("open_interest", 0)),
            "volume24h": float(t.get("volume", 0)),
            # BTC-native prices (useful for order placement)
            "_mark_price_btc": mark_btc,
            "_best_bid_btc": bid_btc,
            "_best_ask_btc": ask_btc,
            "_best_bid_amount": float(t.get("best_bid_amount", 0)),
            "_best_ask_amount": float(t.get("best_ask_amount", 0)),
            "_underlying_price": float(t.get("underlying_price", 0)),
            "_min_price": float(t.get("min_price", 0)),
            "_max_price": float(t.get("max_price", 0)),
        }

    def get_option_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get orderbook for an option.

        Returns bids/asks in BTC-native pricing (Deribit's native unit).
        Order placement and PnL calculations need native prices.
        The _index_price field allows callers to convert to USD if needed.
        """
        resp = self._auth.call("public/get_order_book", {
            "instrument_name": symbol,
            "depth": 10,
        })
        if "result" not in resp:
            logger.debug(f"Deribit orderbook failed for {symbol}: {resp.get('error')}")
            return None

        ob = resp["result"]
        index_price = float(ob.get("index_price", 0))

        # Normalise to dict format: [{"price": <btc_float>, "qty": <amount>}, ...]
        def to_dicts(levels):
            return [{"price": price, "qty": amount} for price, amount in levels]

        return {
            "bids": to_dicts(ob.get("bids", [])),
            "asks": to_dicts(ob.get("asks", [])),
            "mark": float(ob.get("mark_price", 0)) * index_price,  # USD for notional calc
            "_index_price": index_price,
        }
