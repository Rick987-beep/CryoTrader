"""
indicators/data.py — Generic Binance Spot Kline Fetcher

Provides a single function, fetch_klines(), that works for any symbol and
interval supported by the Binance spot public API. Intended as the shared
data layer for all indicators in this package.

Binance endpoint: GET https://api.binance.com/api/v3/klines
No authentication required.

Supported intervals (Binance notation):
  Minutes : 1m 3m 5m 15m 30m
  Hours   : 1h 2h 4h 6h 8h 12h
  Days+   : 1d 3d 1w 1M

Cache:
  Results are cached per (symbol, interval) for a TTL derived from the
  interval length — short intervals expire quickly, daily bars are kept longer.
  Pass force_refresh=True to bypass the cache.
"""

import logging
import time
from typing import Optional

import pandas as pd
import requests

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_MAX_BARS_PER_REQUEST = 1000  # Binance hard limit

# Cache TTL per interval (seconds). Shorter intervals refresh more often.
_INTERVAL_TTL: dict[str, int] = {
    "1m":  60,
    "3m":  90,
    "5m":  150,
    "15m": 300,
    "30m": 600,
    "1h":  1800,
    "2h":  3600,
    "4h":  7200,
    "6h":  10800,
    "8h":  14400,
    "12h": 21600,
    "1d":  3600,   # daily bars change slowly; 1h TTL is fine
    "3d":  7200,
    "1w":  14400,
    "1M":  86400,
}
_DEFAULT_TTL = 300

# =============================================================================
# Cache
# =============================================================================

_cache: dict[tuple[str, str], dict] = {}  # key: (symbol, interval)


def _cache_get(symbol: str, interval: str) -> Optional[pd.DataFrame]:
    key = (symbol.upper(), interval)
    entry = _cache.get(key)
    if entry is None:
        return None
    ttl = _INTERVAL_TTL.get(interval, _DEFAULT_TTL)
    age = time.time() - entry["ts"]
    if age > ttl:
        logger.debug("Cache expired for %s %s (age=%.0fs)", symbol, interval, age)
        return None
    logger.debug("Cache hit for %s %s (age=%.0fs)", symbol, interval, age)
    return entry["df"]


def _cache_set(symbol: str, interval: str, df: pd.DataFrame) -> None:
    _cache[(symbol.upper(), interval)] = {"df": df, "ts": time.time()}


# =============================================================================
# Core Fetcher
# =============================================================================

def fetch_klines(
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    lookback_bars: int = 1500,
    force_refresh: bool = False,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLC klines from the Binance spot public API.

    Paginates automatically when lookback_bars > 1000 (Binance per-request limit).
    Results are cached per (symbol, interval) — see _INTERVAL_TTL for TTLs.

    Args:
        symbol:        Binance spot symbol, e.g. "BTCUSDT", "ETHUSDT".
        interval:      Binance interval string, e.g. "15m", "1h", "1d".
        lookback_bars: Number of most-recent bars to return.
        force_refresh: Bypass cache and fetch fresh data.

    Returns:
        DataFrame with columns [open, high, low, close, volume] and a
        UTC DatetimeIndex (bar open times), oldest bar first.
        Returns None on network failure with no cached fallback.
    """
    symbol = symbol.upper()

    if not force_refresh:
        cached = _cache_get(symbol, interval)
        if cached is not None:
            # Trim to requested lookback in case cache has more rows
            return cached.iloc[-lookback_bars:].copy()

    all_bars: list[list] = []
    end_time: Optional[int] = None  # None = fetch up to the current bar
    remaining = lookback_bars

    try:
        while remaining > 0:
            batch = min(remaining, _MAX_BARS_PER_REQUEST)
            params: dict = {"symbol": symbol, "interval": interval, "limit": batch}
            if end_time is not None:
                params["endTime"] = end_time

            resp = requests.get(_BINANCE_KLINES_URL, params=params, timeout=10)
            resp.raise_for_status()
            data: list[list] = resp.json()

            if not data:
                break

            # Binance returns bars oldest-first; prepend to all_bars
            all_bars = data + all_bars
            remaining -= len(data)

            if len(data) < batch:
                # Reached the beginning of available history
                break

            # Next page ends just before the oldest bar we got
            end_time = int(data[0][0]) - 1

    except Exception as exc:
        logger.error("fetch_klines(%s, %s): %s", symbol, interval, exc)
        # Return stale cache as fallback
        stale = _cache.get((symbol, interval))
        if stale is not None:
            logger.warning("Returning stale cache for %s %s", symbol, interval)
            return stale["df"].iloc[-lookback_bars:].copy()
        return None

    if not all_bars:
        logger.error("fetch_klines(%s, %s): no data returned", symbol, interval)
        return None

    # Binance kline format:
    # [open_time, open, high, low, close, volume, close_time, ...]
    df = pd.DataFrame(all_bars, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "trades", "taker_base", "taker_quote", "ignore",
    ])
    df = df[["open_time", "open", "high", "low", "close", "volume"]].copy()
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df.set_index("open_time", inplace=True)
    df.index.name = "timestamp"
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)

    # Drop duplicate timestamps that can appear at pagination boundaries
    df = df[~df.index.duplicated(keep="last")]
    df.sort_index(inplace=True)

    _cache_set(symbol, interval, df)

    logger.info(
        "fetch_klines(%s, %s): fetched %d bars (%s → %s)",
        symbol, interval, len(df),
        df.index[0].strftime("%Y-%m-%d %H:%M"),
        df.index[-1].strftime("%Y-%m-%d %H:%M"),
    )

    return df.iloc[-lookback_bars:].copy()
