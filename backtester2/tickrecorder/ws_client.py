#!/usr/bin/env python3
"""
ws_client.py — Deribit WebSocket Connection Manager

Manages a single unauthenticated WebSocket connection to Deribit.
The connection is kept open permanently (heartbeat only) between snapshots.
Channels are subscribed only during the 10-second burst window before each
5-minute snapshot boundary, then unsubscribed immediately after.

Reconnect strategy (maintenance-aware):
  Attempt 1–3:   RECONNECT_FAST_DELAY  (5s)  — transient blip
  Attempt 4–6:   RECONNECT_MID_DELAY   (30s) — brief outage
  Attempt 7+:    RECONNECT_MAX_DELAY   (120s) — maintenance window

All callbacks are synchronous and called from the asyncio event loop.
Heavy work (parquet writes) must not block the callback.
"""
import asyncio
import json
import logging
import time
from typing import Callable, Dict, List, Optional, Set

import websockets
import websockets.exceptions

from backtester2.tickrecorder import config

logger = logging.getLogger(__name__)


class DeribitWSClient:
    """Persistent unauthenticated WebSocket client for Deribit public channels.

    Usage:
        client = DeribitWSClient()
        client.on_ticker(my_tick_handler)  # called with (channel, data) dict
        await client.run()                 # runs forever, reconnecting as needed
    """

    def __init__(self):
        self._tick_callback = None      # type: Optional[Callable]
        self._connected_callback = None # type: Optional[Callable]
        self._instruments = []          # type: List[str]  # Deribit instrument names
        self._subscribed = set()        # type: Set[str]   # currently subscribed channels
        self._ws = None
        self._running = False
        self._attempt = 0
        self._connected = False
        self._connect_time = None       # type: Optional[float]  # monotonic
        self._msg_id = 0
        self._heartbeat_task = None     # type: Optional[asyncio.Task]

    # ── Public API ────────────────────────────────────────────────────────────

    def on_ticker(self, callback):
        # type: (Callable[[str, dict], None]) -> None
        """Register callback fired on every ticker message.

        callback(channel: str, data: dict)
          channel — e.g. "ticker.BTC-28MAR26-80000-C.100ms"
          data    — raw Deribit ticker payload dict
        """
        self._tick_callback = callback

    def on_connected(self, callback):
        # type: (Callable[[], None]) -> None
        """Register callback fired each time the connection is (re)established
        and all channels have been subscribed."""
        self._connected_callback = callback

    def set_instruments(self, instrument_names):
        # type: (List[str]) -> None
        """Replace the set of instrument names to subscribe to.

        Safe to call at any time — a live connection will subscribe/unsubscribe
        the delta immediately.  Called from InstrumentTracker on change.
        """
        self._instruments = list(instrument_names)

    @property
    def is_connected(self):
        # type: () -> bool
        return self._connected

    @property
    def reconnect_count(self):
        # type: () -> int
        return max(0, self._attempt - 1)

    async def run(self):
        # type: () -> None
        """Run forever: connect, stream ticks, reconnect on any failure."""
        self._running = True
        while self._running:
            await self._connect_and_stream()
            if not self._running:
                break
            delay = self._backoff_delay()
            logger.info("Reconnecting in %ds (attempt %d)", delay, self._attempt)
            await asyncio.sleep(delay)

    async def stop(self):
        # type: () -> None
        """Signal the run loop to stop after the current connection closes."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _backoff_delay(self):
        # type: () -> int
        if self._attempt <= 3:
            return config.RECONNECT_FAST_DELAY
        if self._attempt <= 6:
            return config.RECONNECT_MID_DELAY
        return config.RECONNECT_MAX_DELAY

    def _next_id(self):
        # type: () -> int
        self._msg_id += 1
        return self._msg_id

    async def _send(self, ws, method, params=None):
        # type: (object, str, Optional[dict]) -> None
        """Send a JSON-RPC request. Fire-and-forget — never blocks on response."""
        msg = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or {},
        }
        try:
            await ws.send(json.dumps(msg))
        except Exception as exc:
            logger.debug("Send failed (%s): %s", method, exc)
            raise  # propagate to trigger reconnect

    async def subscribe_snapshot_channels(self):
        # type: () -> None
        """Subscribe to all option ticker channels + index. Called at burst-window open.

        Safe to call on a live connection. No-op if not currently connected.
        """
        if self._ws is None:
            logger.warning("subscribe_snapshot_channels: not connected, skipping")
            return
        channels = [f"ticker.{name}.100ms" for name in self._instruments]
        channels.append("deribit_price_index.btc_usd")
        batch_size = 100
        for i in range(0, len(channels), batch_size):
            batch = channels[i : i + batch_size]
            await self._send(self._ws, "public/subscribe", {"channels": batch})
        self._subscribed = set(channels)
        logger.info("Burst subscribe: %d channels", len(channels))

    async def unsubscribe_snapshot_channels(self):
        # type: () -> None
        """Unsubscribe all snapshot channels. Called immediately after snapshot is taken."""
        if self._ws is None or not self._subscribed:
            self._subscribed = set()
            return
        channels = list(self._subscribed)
        batch_size = 100
        for i in range(0, len(channels), batch_size):
            batch = channels[i : i + batch_size]
            await self._send(self._ws, "public/unsubscribe", {"channels": batch})
        self._subscribed = set()
        logger.info("Burst unsubscribe: %d channels", len(channels))

    async def _heartbeat_loop(self, ws):
        # type: (object) -> None
        """Send public/test every 15s to detect stale connections."""
        while True:
            await asyncio.sleep(15)
            try:
                await self._send(ws, "public/test")
            except Exception:
                break  # ws is dead; let _connect_and_stream handle reconnect

    async def _connect_and_stream(self):
        # type: () -> None
        self._attempt += 1
        self._connected = False
        logger.info("Connecting to Deribit WebSocket (attempt %d)", self._attempt)

        try:
            async with websockets.connect(
                config.DERIBIT_WS_URL,
                ping_interval=None,      # We manage heartbeat ourselves
                ping_timeout=None,
                close_timeout=5,
                max_size=10 * 1024 * 1024,  # 10 MB message limit
            ) as ws:
                self._ws = ws
                self._connect_time = time.monotonic()
                self._attempt = 0        # Reset backoff on successful connect
                self._connected = True
                logger.info("Connected to Deribit WebSocket (idle — burst subscribe on demand)")

                if self._connected_callback is not None:
                    try:
                        self._connected_callback()
                    except Exception:
                        logger.exception("on_connected callback raised")

                self._heartbeat_task = asyncio.ensure_future(
                    self._heartbeat_loop(ws)
                )

                async for raw in ws:
                    self._handle_message(raw)

        except websockets.exceptions.ConnectionClosedOK:
            logger.info("WebSocket closed cleanly")
        except websockets.exceptions.ConnectionClosedError as exc:
            logger.warning("WebSocket closed with error: %s", exc)
        except OSError as exc:
            logger.warning("WebSocket OS error: %s", exc)
        except Exception as exc:
            logger.warning("WebSocket unexpected error: %s", exc)
        finally:
            self._connected = False
            self._ws = None
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                self._heartbeat_task = None

    def _handle_message(self, raw):
        # type: (str) -> None
        """Parse and dispatch an incoming WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Subscription data arrives as: {"method": "subscription", "params": {...}}
        if msg.get("method") != "subscription":
            return

        params = msg.get("params", {})
        channel = params.get("channel", "")
        data = params.get("data", {})

        if not (channel.startswith("ticker.") or channel.startswith("deribit_price_index.")):
            return

        if self._tick_callback is not None:
            try:
                self._tick_callback(channel, data)
            except Exception:
                logger.exception("Tick callback raised on channel %s", channel)
