#!/usr/bin/env python3
"""
Option Chain Snapshot Capture

Captures a full BTC option chain snapshot (nearest expiry): index price,
mark prices, bid/ask, and Greeks for every strike within ±$5000 of ATM.

Saves each snapshot as a JSON file in analysis/data/.

Usage:
    # Run continuously — snapshot every full UTC hour (run for 24h+):
    python -m analysis.capture_snapshot --loop

    # Capture at a specific UTC hour (waits if needed):
    python -m analysis.capture_snapshot 12

    # Capture immediately (for testing):
    python -m analysis.capture_snapshot --now
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_data import (
    MarketData,
    get_btc_index_price,
    get_option_instruments,
    get_option_details,
)

from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def wait_until_utc_hour(hour: int):
    """Block until the given UTC hour is reached (today). Prints a countdown."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)

    if now >= target:
        logger.info(f"Target {hour:02d}:00 UTC already passed — capturing immediately.")
        return

    wait_secs = (target - now).total_seconds()
    logger.info(
        f"Waiting for {hour:02d}:00 UTC  (in {wait_secs/60:.1f} minutes, "
        f"current time {now.strftime('%H:%M:%S')} UTC)"
    )

    while True:
        now = datetime.now(timezone.utc)
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        # Log every 5 minutes, then every 30s in the last 2 minutes
        if remaining > 120:
            logger.info(f"  {remaining/60:.0f} min remaining …")
            time.sleep(min(remaining - 120, 300))
        else:
            logger.info(f"  {remaining:.0f}s remaining …")
            time.sleep(min(remaining, 30))

    logger.info(f"🕐 Target time {hour:02d}:00 UTC reached — capturing snapshot.")


def find_nearest_expiry(instruments):
    """Return the nearest unexpired expiry timestamp from the instruments list."""
    now_ms = time.time() * 1000
    valid = [i for i in instruments if i.get("expirationTimestamp", 0) > now_ms]
    if not valid:
        return None
    return min(i["expirationTimestamp"] for i in valid)


def capture_snapshot() -> dict:
    """
    Capture a snapshot of the BTC option chain for the nearest expiry.

    Returns dict with:
        timestamp_utc, index_price, expiry_ts, strikes: [...]
    """
    logger.info("Fetching BTC index price ...")
    index_price = get_btc_index_price(use_cache=False)
    if index_price is None:
        logger.error("Could not get BTC index price — skipping.")
        return None
    logger.info(f"BTC index price: ${index_price:,.2f}")

    logger.info("Fetching option instruments ...")
    instruments = get_option_instruments("BTC")
    if not instruments:
        logger.error("Could not get instruments — skipping.")
        return None

    nearest_ts = find_nearest_expiry(instruments)
    if nearest_ts is None:
        logger.error("No unexpired instruments found — skipping.")
        return None

    # Filter to nearest expiry
    expiry_instruments = [i for i in instruments if i["expirationTimestamp"] == nearest_ts]
    logger.info(
        f"Nearest expiry: {datetime.fromtimestamp(nearest_ts / 1000, tz=timezone.utc).strftime('%d%b%y')}  "
        f"({len(expiry_instruments)} instruments)"
    )

    # Group by strike — collect calls and puts
    strikes_map: dict = {}
    for inst in expiry_instruments:
        sym = inst["symbolName"]
        strike = float(inst["strike"])
        opt_type = "call" if sym.endswith("-C") else "put"
        if strike not in strikes_map:
            strikes_map[strike] = {"strike": strike, "call": None, "put": None}
        strikes_map[strike][opt_type] = sym

    # Sort by strike
    sorted_strikes = sorted(strikes_map.values(), key=lambda s: s["strike"])

    # Determine range of strikes to fetch: ATM ± $5000 (enough for K up to $3000)
    atm_strike = min(sorted_strikes, key=lambda s: abs(s["strike"] - index_price))["strike"]
    strike_range_usd = 5000
    filtered_strikes = [
        s for s in sorted_strikes
        if abs(s["strike"] - atm_strike) <= strike_range_usd
    ]
    logger.info(
        f"ATM strike: ${atm_strike:,.0f}  — fetching details for "
        f"{len(filtered_strikes)} strikes (±${strike_range_usd})"
    )

    # Fetch details for each call/put at each strike
    snapshot_strikes = []
    for i, s in enumerate(filtered_strikes):
        strike_data = {"strike": s["strike"]}

        for leg_type in ("call", "put"):
            sym = s.get(leg_type)
            if sym is None:
                strike_data[leg_type] = None
                continue

            details = get_option_details(sym)
            if details is None:
                logger.warning(f"  No details for {sym}")
                strike_data[leg_type] = {"symbol": sym, "error": "no_data"}
                continue

            strike_data[leg_type] = {
                "symbol": sym,
                "bid": _safe_float(details.get("bid")),
                "ask": _safe_float(details.get("ask")),
                "mid": _mid(details),
                "mark_price": _safe_float(details.get("markPrice")),
                "iv": _safe_float(details.get("impliedVolatility")),
                "delta": _safe_float(details.get("delta")),
                "gamma": _safe_float(details.get("gamma")),
                "theta": _safe_float(details.get("theta")),
                "vega": _safe_float(details.get("vega")),
            }

        snapshot_strikes.append(strike_data)

        # Progress
        if (i + 1) % 5 == 0 or i == len(filtered_strikes) - 1:
            logger.info(f"  [{i+1}/{len(filtered_strikes)}] strikes fetched")

        # Small delay to avoid rate-limiting
        time.sleep(0.15)

    now_utc = datetime.now(timezone.utc)
    snapshot = {
        "timestamp_utc": now_utc.isoformat(),
        "timestamp_epoch": now_utc.timestamp(),
        "index_price": index_price,
        "atm_strike": atm_strike,
        "expiry_ts": nearest_ts,
        "expiry_date": datetime.fromtimestamp(nearest_ts / 1000, tz=timezone.utc).strftime("%d%b%y"),
        "strikes": snapshot_strikes,
    }
    return snapshot


def _safe_float(val) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _mid(details: dict) -> Optional[float]:
    bid = _safe_float(details.get("bid"))
    ask = _safe_float(details.get("ask"))
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return round((bid + ask) / 2, 4)
    return _safe_float(details.get("markPrice"))


def save_snapshot(snapshot: dict, label: str) -> str:
    """Save snapshot to analysis/data/ and return the file path."""
    os.makedirs(DATA_DIR, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    filename = f"snapshot_{date_str}_{label}.json"
    filepath = os.path.join(DATA_DIR, filename)

    with open(filepath, "w") as f:
        json.dump(snapshot, f, indent=2)

    logger.info(f"Snapshot saved → {filepath}")
    return filepath


def _seconds_until_next_hour() -> float:
    """Return seconds until the next full UTC hour."""
    now = datetime.now(timezone.utc)
    # Next hour: current hour + 1, minute/second/microsecond = 0
    next_hour = now.replace(minute=0, second=0, microsecond=0)
    # timedelta to add 1 hour
    from datetime import timedelta
    next_hour = next_hour + timedelta(hours=1)
    return (next_hour - now).total_seconds()


def run_loop():
    """Capture a snapshot at every full UTC hour. Runs until interrupted."""
    logger.info("=" * 60)
    logger.info("  Hourly snapshot loop started (Ctrl+C to stop)")
    logger.info("=" * 60)

    snapshots_ok = 0
    snapshots_fail = 0

    while True:
        wait = _seconds_until_next_hour()
        next_time = datetime.now(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        from datetime import timedelta
        next_time = next_time + timedelta(hours=1)
        logger.info(
            f"Next snapshot at {next_time.strftime('%H:%M')} UTC "
            f"(in {wait/60:.1f} min). "
            f"[ok={snapshots_ok} fail={snapshots_fail}]"
        )
        time.sleep(wait)

        hour_label = f"{datetime.now(timezone.utc).hour:02d}00"
        try:
            snapshot = capture_snapshot()
            if snapshot is not None:
                save_snapshot(snapshot, hour_label)
                snapshots_ok += 1
                logger.info(
                    f"Snapshot {hour_label}: Index=${snapshot['index_price']:,.2f}, "
                    f"{len(snapshot['strikes'])} strikes."
                )
            else:
                snapshots_fail += 1
                logger.warning(f"Snapshot {hour_label}: FAILED (API error).")
        except Exception as e:
            snapshots_fail += 1
            logger.error(f"Snapshot {hour_label}: EXCEPTION — {e}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "--loop":
        try:
            run_loop()
        except KeyboardInterrupt:
            logger.info("Loop stopped by user.")
        return

    if arg == "--now":
        label = f"{datetime.now(timezone.utc).hour:02d}00"
    else:
        hour = int(arg)
        if not 0 <= hour <= 23:
            print(f"Invalid hour: {hour}. Must be 0-23.")
            sys.exit(1)
        wait_until_utc_hour(hour)
        label = f"{hour:02d}00"

    snapshot = capture_snapshot()
    if snapshot is None:
        logger.error("Snapshot failed.")
        sys.exit(1)
    save_snapshot(snapshot, label)

    logger.info(
        f"Done. Index=${snapshot['index_price']:,.2f}, "
        f"{len(snapshot['strikes'])} strikes captured."
    )


if __name__ == "__main__":
    main()
