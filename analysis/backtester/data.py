"""
Market data fetching for backtesting.

Currently supports Binance 1h BTCUSDT perpetual candles with automatic
pagination for datasets > 1500 candles.

Candle dict keys: dt, hour, weekday (0=Mon), date, open, high, low, close.

To add a new data source, add a fetch_*() function returning the same format.
"""

import sys
from datetime import datetime, timezone, timedelta

try:
    import requests
except ImportError:
    print("pip install requests")
    sys.exit(1)


def fetch_binance_candles(weeks=5):
    """Fetch 1h BTCUSDT perp candles from Binance, with pagination.

    Returns list of candle dicts with keys:
        dt, hour, weekday, date, open, high, low, close
    """
    total_hours = weeks * 7 * 24
    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(weeks=weeks)
    start_ms = int(start_dt.timestamp() * 1000)

    url = "https://fapi.binance.com/fapi/v1/klines"
    all_candles = []

    print("  Fetching %d weeks of Binance 1h candles..." % weeks)
    while len(all_candles) < total_hours:
        params = {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "startTime": start_ms,
            "limit": 1500,
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            break

        for r in raw:
            dt = datetime.fromtimestamp(r[0] / 1000, tz=timezone.utc)
            all_candles.append({
                "dt": dt,
                "hour": dt.hour,
                "weekday": dt.weekday(),
                "date": dt.strftime("%Y-%m-%d"),
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
            })

        if len(raw) < 1500:
            break
        start_ms = raw[-1][0] + 1

    all_candles = all_candles[:total_hours]
    dates = sorted(set(c["date"] for c in all_candles))
    print("    Got %d candles: %s to %s (%d days)" % (
        len(all_candles), dates[0], dates[-1], len(dates)))
    return all_candles
