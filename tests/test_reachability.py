#!/usr/bin/env python3
"""
Manual reachability test — run this, then toggle your network on/off.

Usage:
    python tests/test_reachability.py

What to watch for:
  1. Starts with "reachable=True" and successful polls
  2. Disconnect WiFi/network
  3. After ~3 failed requests → "reachable=False" + UNREACHABLE log
  4. After ~5 failures → "Session refreshed" log
  5. Reconnect network
  6. Next successful request → "reachable=True" + RECONNECTED log

Press Ctrl+C to stop.
"""

import logging
import sys
import time
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_reachability")

from config import EXCHANGE


def test_with_coincall():
    from auth import CoincallAuth
    from config import API_KEY, API_SECRET, BASE_URL

    auth = CoincallAuth(API_KEY, API_SECRET, BASE_URL)
    logger.info(f"Testing Coincall auth — {BASE_URL}")

    poll_count = 0
    while True:
        poll_count += 1
        result = auth.get("/open/account/summary/v1")
        success = auth.is_successful(result)

        equity = None
        if success:
            equity = result.get("data", {}).get("equity")

        logger.info(
            f"[poll {poll_count:>3}] "
            f"reachable={auth.reachable}  "
            f"failures={auth._consecutive_failures}  "
            f"success={success}  "
            f"equity={equity}"
        )
        time.sleep(3)


def test_with_deribit():
    from exchanges.deribit.auth import DeribitAuth

    auth = DeribitAuth()
    logger.info(f"Testing Deribit auth — {auth.base_url}")

    poll_count = 0
    while True:
        poll_count += 1
        result = auth.call("public/get_index_price", {"index_name": "btc_usd"})
        success = auth.is_successful(result)

        price = None
        if success:
            price = result.get("result", {}).get("index_price")

        logger.info(
            f"[poll {poll_count:>3}] "
            f"reachable={auth.reachable}  "
            f"failures={auth._consecutive_failures}  "
            f"success={success}  "
            f"price={price}"
        )
        time.sleep(3)


if __name__ == "__main__":
    logger.info(f"Exchange: {EXCHANGE}")
    logger.info("Polling every 3s — disconnect/reconnect network to test")
    logger.info("Press Ctrl+C to stop\n")

    try:
        if EXCHANGE == "deribit":
            test_with_deribit()
        else:
            test_with_coincall()
    except KeyboardInterrupt:
        logger.info("\nStopped by user")
