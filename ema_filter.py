#!/usr/bin/env python3
"""
EMA Filter Module — BTC Price Trend Filter via Binance Klines

Fetches BTCUSDT Perpetual daily klines from Binance public API and
computes the EMA-20.  Exposes two entry condition factories:

  ema20_filter()       — passes when live BTC index > EMA-20
  below_ema20_filter() — passes when live BTC index < EMA-20

Caching: kline data is cached for 1 hour since daily candles change
slowly and the entry check runs every ~15 seconds.
"""

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)


# =============================================================================
# EMA Calculation
# =============================================================================

def _compute_ema(closes: list, period: int) -> float:
    """
    Compute EMA over a list of closing prices.

    Uses the standard recursive formula:
      EMA_t = close_t * α + EMA_{t-1} * (1 - α)
    where α = 2 / (period + 1).

    The first `period` values are seeded with a simple moving average.

    Args:
        closes: List of closing prices (oldest first).
        period: EMA period (e.g. 20).

    Returns:
        Current (latest) EMA value.
    """
    if len(closes) < period:
        # Not enough data — fall back to simple average
        return sum(closes) / len(closes)

    alpha = 2.0 / (period + 1)

    # Seed: SMA of the first `period` values
    ema = sum(closes[:period]) / period

    # Recursive: apply EMA formula to each subsequent close
    for close in closes[period:]:
        ema = close * alpha + ema * (1.0 - alpha)

    return ema


# =============================================================================
# Binance Kline Fetcher with Cache
# =============================================================================

_kline_cache: Optional[dict] = None
_CACHE_TTL = 3600  # 1 hour


def _fetch_daily_closes(count: int = 30) -> Optional[list]:
    """
    Fetch daily closing prices for BTCUSDT Perpetual from Binance.

    Uses the public Binance Futures API (no auth needed):
      GET https://fapi.binance.com/fapi/v1/klines

    Args:
        count: Number of daily candles to fetch (default 30).

    Returns:
        List of closing prices (oldest first), or None on failure.
    """
    global _kline_cache

    # Check cache
    if _kline_cache is not None:
        age = time.time() - _kline_cache["ts"]
        if age < _CACHE_TTL:
            logger.debug(f"EMA kline cache hit (age={age:.0f}s)")
            return _kline_cache["closes"]

    try:
        url = "https://fapi.binance.com/fapi/v1/klines"
        params = {
            "symbol": "BTCUSDT",
            "interval": "1d",
            "limit": count,
        }
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Binance kline format: [open_time, open, high, low, close, ...]
        closes = [float(candle[4]) for candle in data]

        if len(closes) < 20:
            logger.warning(f"EMA filter: only {len(closes)} candles returned, need ≥20")
            return None

        _kline_cache = {"closes": closes, "ts": time.time()}
        logger.info(
            f"EMA filter: fetched {len(closes)} daily candles, "
            f"latest close=${closes[-1]:,.0f}"
        )
        return closes

    except Exception as e:
        logger.error(f"EMA filter: failed to fetch Binance klines: {e}")
        # Return stale cache if available
        if _kline_cache is not None:
            logger.warning("EMA filter: using stale cache as fallback")
            return _kline_cache["closes"]
        return None


# =============================================================================
# Public API
# =============================================================================

def get_ema20() -> Optional[float]:
    """
    Get the current daily EMA-20 value for BTCUSDT.

    Returns:
        EMA-20 value, or None if data unavailable.
    """
    closes = _fetch_daily_closes(count=30)
    if closes is None:
        return None
    return _compute_ema(closes, 20)


def is_btc_above_ema20() -> bool:
    """
    Check if the current BTC price is above the daily EMA-20.

    Uses the most recent daily close as the "current price" proxy.

    Returns:
        True if BTC close > EMA-20, False otherwise or on data error.
    """
    closes = _fetch_daily_closes(count=30)
    if closes is None:
        logger.warning("EMA filter: no data — blocking entry (fail-safe)")
        return False

    ema = _compute_ema(closes, 20)
    current = closes[-1]
    above = current > ema

    logger.info(
        f"EMA filter: BTC=${current:,.0f}, EMA-20=${ema:,.0f} "
        f"→ {'ABOVE ✓' if above else 'BELOW ✗'}"
    )
    return above


def is_btc_below_ema20() -> bool:
    """
    Check if the live BTC index price is at or below the daily EMA-20
    (i.e. EMA-20 >= BTC index).

    Uses the live Deribit BTC index price.
    Blocks entry (fail-safe) if either data point is unavailable.

    Returns:
        True if EMA-20 >= live BTC index, False otherwise or on data error.
    """
    from market_data import get_btc_index_price  # local import avoids circular dep

    ema = get_ema20()
    if ema is None:
        logger.warning("EMA filter: no EMA data — blocking entry (fail-safe)")
        return False

    index_price = get_btc_index_price(use_cache=True)
    if index_price is None:
        logger.warning("EMA filter: no BTC index price — blocking entry (fail-safe)")
        return False

    passes = ema >= index_price
    logger.info(
        f"EMA filter: BTC index=${index_price:,.0f}, EMA-20=${ema:,.0f} "
        f"→ {'EMA ≥ BTC ✓' if passes else 'EMA < BTC ✗'}"
    )
    return passes


def ema20_filter():
    """
    Entry condition factory: passes when live BTC price > EMA-20.
    """
    def _check(account) -> bool:
        return is_btc_above_ema20()

    _check.__name__ = "ema20_filter"
    return _check


def below_ema20_filter():
    """
    Entry condition factory: passes when EMA-20 >= live BTC index.

    Opens a trade when BTC is at or below the EMA-20 line.
    Blocks entry (fail-safe) if BTC index or EMA data is unavailable.
    """
    def _check(account) -> bool:
        return is_btc_below_ema20()

    _check.__name__ = "below_ema20_filter"
    return _check
