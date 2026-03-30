#!/usr/bin/env python3
"""
Market Data Module

Provides exchange-agnostic convenience functions that route through
the active exchange adapter (Coincall or Deribit).  The adapter is
selected automatically from config.EXCHANGE at first use.

Also contains the Coincall-specific MarketData class (used by the
CoincallMarketDataAdapter) and TTLCache utility.
"""

import logging
import requests
import time
from typing import Dict, List, Optional, Any, Tuple
from config import BASE_URL, API_KEY, API_SECRET
from auth import CoincallAuth

logger = logging.getLogger(__name__)


# Simple cache with TTL
class TTLCache:
    """Simple dict-based cache with time-to-live and max size."""

    def __init__(self, ttl_seconds: int = 30, max_size: int = 100):
        """
        Initialize cache.

        Args:
            ttl_seconds: Time-to-live for entries (default 30s)
            max_size: Maximum number of entries (default 100)
        """
        self.ttl_seconds = ttl_seconds
        self.max_size = max_size
        self._cache: Dict[str, Tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if it exists and hasn't expired."""
        if key not in self._cache:
            return None

        value, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl_seconds:
            del self._cache[key]
            return None

        return value

    def set(self, key: str, value: Any) -> None:
        """Set cache entry, evicting oldest if at capacity."""
        if len(self._cache) >= self.max_size:
            # Evict oldest entry
            oldest_key = min(self._cache.keys(), key=lambda k: self._cache[k][1])
            del self._cache[oldest_key]

        self._cache[key] = (value, time.time())

    def fresh_items(self):
        """Yield (key, value) pairs for entries that have NOT expired.

        Also evicts any expired entries encountered during iteration,
        keeping the internal dict clean.
        """
        now = time.time()
        expired = []
        for key, (value, ts) in list(self._cache.items()):
            if now - ts > self.ttl_seconds:
                expired.append(key)
            else:
                yield key, value
        for key in expired:
            del self._cache[key]

    def clear(self) -> None:
        """Clear all cache entries."""
        self._cache.clear()


class MarketData:
    """Handles market data retrieval with TTL caching for API resilience"""

    def __init__(self):
        """Initialize market data client with caching"""
        self.auth = CoincallAuth(API_KEY, API_SECRET, BASE_URL)
        self._price_cache = None
        self._price_cache_time = None
        self._index_cache = None
        self._index_cache_time = None
        self._instruments_cache = TTLCache(ttl_seconds=30, max_size=10)
        self._details_cache = TTLCache(ttl_seconds=30, max_size=200)

    def get_btc_futures_price(self, use_cache: bool = True) -> float:
        """
        Get BTC/USDT perpetual futures price from Coincall, fallback to Binance.

        Args:
            use_cache: Use cached price if available (cache expires every 30 seconds)

        Returns:
            BTC/USDT futures price
        """
        import time
        
        # Check cache
        if use_cache and self._price_cache is not None:
            if time.time() - self._price_cache_time < 30:
                return self._price_cache

        try:
            # Try Coincall futures ticker endpoint
            response = self.auth.get('/open/futures/ticker/BTCUSDT')
            
            if self.auth.is_successful(response):
                data = response.get('data', {})
                price_fields = ['lastPrice', 'price', 'markPrice']
                for field in price_fields:
                    if field in data:
                        price = float(data[field])
                        if price > 0:
                            self._price_cache = price
                            self._price_cache_time = time.time()
                            logger.debug(f"BTC/USDT futures price from Coincall: {price}")
                            return price

        except Exception as e:
            logger.warning(f"Coincall futures price failed: {e}")

        # Try Binance API as fallback
        try:
            response = requests.get('https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT', timeout=5)
            if response.status_code == 200:
                data = response.json()
                price = float(data.get('price', 0))
                if price > 0:
                    self._price_cache = price
                    self._price_cache_time = time.time()
                    logger.info(f"BTC/USDT from Binance: {price}")
                    return price
        except Exception as e:
            logger.warning(f"Binance price failed: {e}")

        # Final fallback
        fallback_price = 72000.0
        logger.warning(f"Using fallback price: {fallback_price}")
        return fallback_price

    def get_btc_index_price(self, use_cache: bool = True) -> Optional[float]:
        """
        Get the BTCUSD index price from Coincall.

        Tries (in order):
          1. indexPrice from a *fresh* (TTL-valid) cached option detail
          2. Fetch a near-ATM BTC option detail to extract indexPrice
          3. Binance perpetual price as final fallback

        Returns:
            BTCUSD index price, or None if all sources fail.
        """
        # Check cache
        if use_cache and self._index_cache is not None:
            if time.time() - self._index_cache_time < 30:
                return self._index_cache

        # 1) Extract indexPrice from a *fresh* cached option detail.
        #    Uses fresh_items() to enforce TTL — expired entries are skipped
        #    and evicted, preventing the stale-cache loop.
        for _key, details in self._details_cache.fresh_items():
            if isinstance(details, dict) and 'indexPrice' in details:
                price = float(details['indexPrice'])
                if price > 0:
                    self._update_index_cache(price, "option detail cache")
                    return price

        # 2) Fetch a near-ATM option to get indexPrice (1 API call)
        try:
            instruments = self.get_option_instruments('BTC')
            if instruments:
                symbol = instruments[0].get('symbolName')
                if symbol:
                    details = self.get_option_details(symbol)
                    if details and 'indexPrice' in details:
                        price = float(details['indexPrice'])
                        if price > 0:
                            self._update_index_cache(price, "option detail fetch")
                            return price
        except Exception as e:
            logger.warning(f"BTC index from option detail failed: {e}")

        # 3) Binance perpetual as final fallback (perp ≈ index)
        try:
            response = requests.get(
                'https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT',
                timeout=5,
            )
            if response.status_code == 200:
                data = response.json()
                price = float(data.get('price', 0))
                if price > 0:
                    self._update_index_cache(price, "Binance fallback")
                    return price
        except Exception as e:
            logger.warning(f"Binance fallback for index price failed: {e}")

        logger.warning("Could not retrieve BTC index price from any source")
        return None

    def _update_index_cache(self, price: float, source: str) -> None:
        """Store a new index price, log the source, and warn if frozen."""
        now = time.time()

        # Detect frozen price: same value for > 60s is suspicious
        if (self._index_cache is not None
                and self._index_cache == price
                and self._index_cache_time is not None
                and now - self._index_cache_time > 60):
            stale_secs = int(now - self._index_cache_time)
            logger.warning(
                f"BTC index price unchanged at ${price:,.2f} for {stale_secs}s "
                f"(source: {source}) — possible stale feed"
            )

        self._index_cache = price
        self._index_cache_time = now
        logger.info(f"BTC index price ({source}): ${price:,.2f}")

    def get_option_instruments(self, underlying: str = 'BTC') -> Optional[List[Dict[str, Any]]]:
        """
        Get available option instruments from Coincall (cached for 30s).

        Args:
            underlying: Underlying symbol (BTC, ETH, etc.)

        Returns:
            List of option instruments or None if failed
        """
        # Check cache first
        cache_key = f"instruments_{underlying}"
        cached = self._instruments_cache.get(cache_key)
        if cached is not None:
            logger.debug(f"Using cached instruments for {underlying}")
            return cached

        try:
            # Try the correct endpoint as a public request (no auth)
            endpoint = f'/open/option/getInstruments/{underlying}'
            logger.debug(f"Fetching instruments for {underlying}")
            url = f"{self.auth.base_url}{endpoint}"
            response = requests.get(url, timeout=10)
            
            if response.status_code == 200:
                try:
                    data = response.json()
                    if data.get('code') == 0 and data.get('data'):
                        instruments = data['data']
                        if isinstance(instruments, list) and len(instruments) > 0:
                            # Cache the result
                            self._instruments_cache.set(cache_key, instruments)
                            logger.debug(f"Retrieved {len(instruments)} option instruments for {underlying}")
                            return instruments
                except Exception as e:
                    logger.debug(f"JSON parse error: {e}")
            
            # If public request fails, try with authentication
            logger.debug("Public request failed, trying with authentication")
            response = self.auth.get(endpoint)
            if self.auth.is_successful(response):
                data = response.get('data', [])
                if isinstance(data, list) and len(data) > 0:
                    # Cache the result
                    self._instruments_cache.set(cache_key, data)
                    logger.debug(f"Retrieved {len(data)} option instruments for {underlying} with auth")
                    return data
            
            logger.error(f"Failed to get option instruments for {underlying}")
            return None
        
        except Exception as e:
            logger.error(f"Error getting option instruments for {underlying}: {e}")
            return None

    def get_option_details(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed information for a specific option (cached for 30s).

        Args:
            symbol: Option symbol

        Returns:
            Dict with option details or None if failed
        """
        # Check cache first
        cached = self._details_cache.get(symbol)
        if cached is not None:
            logger.debug(f"Using cached details for {symbol}")
            return cached

        try:
            # Try the option details endpoint
            response = self.auth.get(f'/open/option/detail/v1/{symbol}')
            
            if self.auth.is_successful(response):
                details = response.get('data', {})
                # Cache the result
                self._details_cache.set(symbol, details)
                logger.debug(f"Retrieved details for {symbol}")
                return details
            else:
                logger.debug(f"Option details endpoint failed for {symbol}: {response.get('msg')}")
                
                # Try as public request
                url = f"{self.auth.base_url}/open/option/detail/v1/{symbol}"
                response = requests.get(url, timeout=10)
                
                if response.status_code == 200:
                    data = response.json()
                    if data.get('code') == 0 and data.get('data'):
                        details = data['data']
                        # Cache the result
                        self._details_cache.set(symbol, details)
                        logger.debug(f"Retrieved details for {symbol} (public)")
                        return details
                
                logger.error(f"Failed to get details for {symbol}: {response.get('msg')}")
                return None

        except Exception as e:
            logger.error(f"Error getting option details for {symbol}: {e}")
            return None

    def get_option_greeks(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        Extract Greeks from option details

        Args:
            symbol: Option symbol

        Returns:
            Dict with delta, theta, vega, gamma or None if failed
        """
        try:
            details = self.get_option_details(symbol)
            if not details:
                return None

            greeks = {
                'delta': float(details.get('delta', 0)),
                'theta': float(details.get('theta', 0)),
                'vega': float(details.get('vega', 0)),
                'gamma': float(details.get('gamma', 0)),
            }
            return greeks

        except Exception as e:
            logger.error(f"Error extracting greeks for {symbol}: {e}")
            return None

    def get_option_market_data(self, symbol: str) -> Optional[Dict[str, float]]:
        """
        Extract market data from option details

        Args:
            symbol: Option symbol

        Returns:
            Dict with bid, ask, mark_price, implied_volatility or None if failed
        """
        try:
            details = self.get_option_details(symbol)
            if not details:
                return None

            market_data = {
                'bid': float(details.get('bid', 0)),
                'ask': float(details.get('ask', 0)),
                'mark_price': float(details.get('markPrice', 0)),
                'implied_volatility': float(details.get('impliedVolatility', 0)),
            }
            return market_data

        except Exception as e:
            logger.error(f"Error extracting market data for {symbol}: {e}")
            return None

    def get_option_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get option orderbook depth (100-level)

        Args:
            symbol: Option symbol

        Returns:
            Dict with orderbook data (bids, asks) or None if failed
        """
        try:
            # Correct endpoint per Coincall API docs
            response = self.auth.get(f'/open/option/order/orderbook/v1/{symbol}')
            
            if self.auth.is_successful(response):
                depth = response.get('data', {})
                # Enrich with mark price from option details if orderbook
                # doesn't include it (Coincall orderbook endpoint omits mark)
                if not depth.get('mark'):
                    details = self.get_option_details(symbol)
                    if details:
                        mark = details.get('markPrice', 0)
                        if mark:
                            depth['mark'] = float(mark)
                return depth
            else:
                logger.error(f"Failed to get orderbook for {symbol}: {response.get('msg')}")
                return None

        except Exception as e:
            logger.error(f"Error getting orderbook for {symbol}: {e}")
            return None


# Global instance
market_data = MarketData()


# =============================================================================
# Exchange-aware routing layer
# =============================================================================
# The convenience functions below route through the active exchange adapter
# (Coincall or Deribit) so callers always get exchange-correct data.
# The adapter is lazily initialized on first use.

_exchange_market_data = None   # ExchangeMarketData instance


def _get_adapter():
    """Lazily build and cache the exchange market_data adapter."""
    global _exchange_market_data
    if _exchange_market_data is None:
        from exchanges import build_exchange
        components = build_exchange()
        _exchange_market_data = components['market_data']
    return _exchange_market_data


# Convenience functions
def get_btc_futures_price(use_cache: bool = True) -> float:
    """Get BTC/USDT futures price (Coincall-only, kept for legacy callers)."""
    return market_data.get_btc_futures_price(use_cache)


def get_btc_index_price(use_cache: bool = True) -> Optional[float]:
    """Get BTCUSD index price via the active exchange adapter."""
    adapter = _get_adapter()
    return adapter.get_index_price("BTC", use_cache=use_cache)


def get_option_instruments(underlying: str = 'BTC') -> Optional[List[Dict[str, Any]]]:
    """Get available option instruments via the active exchange adapter."""
    adapter = _get_adapter()
    return adapter.get_option_instruments(underlying)


def get_option_details(symbol: str) -> Optional[Dict[str, Any]]:
    """Get option details via the active exchange adapter."""
    adapter = _get_adapter()
    return adapter.get_option_details(symbol)


def get_option_greeks(symbol: str) -> Optional[Dict[str, float]]:
    """Get option Greeks (Coincall-only, kept for legacy callers)."""
    return market_data.get_option_greeks(symbol)


def get_option_market_data(symbol: str) -> Optional[Dict[str, float]]:
    """Get option market data (Coincall-only, kept for legacy callers)."""
    return market_data.get_option_market_data(symbol)


def get_option_orderbook(symbol: str) -> Optional[Dict[str, Any]]:
    """Get option orderbook via the active exchange adapter."""
    adapter = _get_adapter()
    return adapter.get_option_orderbook(symbol)
