#!/usr/bin/env python3
"""
recorder.py — Main Daemon Entry Point

Wires all components and runs the asyncio event loop forever.

Usage:
    python -m backtester.ingest.tickrecorder.recorder
    systemctl start ct-recorder

Shutdown:
    SIGTERM or SIGINT → flush current partial file → close WebSocket → exit
"""
import asyncio
import logging
import os
import shutil
import signal
import sys
import time
from datetime import datetime, timezone

from backtester.ingest.tickrecorder import config
from backtester.ingest.tickrecorder.health import RecorderHealth, start_health_server
from backtester.ingest.tickrecorder.instruments import InstrumentTracker
from backtester.ingest.tickrecorder.snapshotter import Snapshotter
from backtester.ingest.tickrecorder.ws_client import DeribitWSClient

logger = logging.getLogger(__name__)


def _deribit_time(snap):
    # type: (Snapshotter) -> float
    """Current time in seconds, corrected to Deribit exchange clock.

    Uses the offset measured from incoming tick timestamps, so the burst loop
    anchors to Deribit time rather than the server's local clock.  Offset is
    0.0 until the first tick or startup sync arrives, which is safe — it just
    means the very first cycle uses the server clock.
    """
    return time.time() + snap.deribit_offset_secs


async def _run_recorder():
    # type: () -> None
    health = RecorderHealth()
    snap = Snapshotter()
    tracker = InstrumentTracker()
    ws = DeribitWSClient()

    # ── Startup disk check ────────────────────────────────────────────────────
    free_mb = _disk_free_mb()
    if free_mb != -1 and free_mb < config.DISK_FREE_ABORT_MB:
        logger.critical(
            "Disk critically full: only %d MB free (abort threshold %d MB). Exiting.",
            free_mb, config.DISK_FREE_ABORT_MB,
        )
        sys.exit(1)
    if free_mb != -1 and free_mb < config.DISK_FREE_WARN_MB:
        logger.warning("Low disk space: %d MB free", free_mb)
        health.notify_critical(f"Low disk space: only {free_mb} MB free on VPS.")

    # ── Crash recovery: load today's partial file if present ─────────────────
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    loaded = snap.load_partial(today_str)
    if loaded:
        logger.info("Resumed from partial file: %d snapshots already recorded today", loaded)

    # ── Sync server clock to Deribit exchange time ────────────────────────────
    await _sync_deribit_clock(snap)

    # ── Initial instrument discovery ──────────────────────────────────────────
    logger.info("Fetching initial instrument list from Deribit...")
    ok = tracker.refresh()
    if not ok:
        logger.warning("Initial instrument fetch failed — will retry on first refresh cycle")
    else:
        ws.set_instruments(tracker.instrument_names)
        logger.info("Loaded %d instruments", len(tracker.active))

    # ── Wire callbacks ────────────────────────────────────────────────────────

    def on_instruments_changed(added, expired):
        # type: (dict, set) -> None
        ws.set_instruments(tracker.instrument_names)
        snap.remove_instruments(expired)
        logger.info(
            "Instrument update applied: +%d new, -%d expired, %d total",
            len(added), len(expired), len(tracker.active),
        )

    def on_tick(channel, data):
        # type: (str, dict) -> None
        snap.on_tick(channel, data)

    def on_ws_connected():
        # type: () -> None
        health.on_connected()

    def on_ws_disconnected():
        # type: () -> None
        health.on_disconnected()

    tracker.on_change(on_instruments_changed)
    ws.on_ticker(on_tick)
    ws.on_connected(on_ws_connected)
    ws.on_disconnected(on_ws_disconnected)

    # ── Start health endpoint ─────────────────────────────────────────────────
    start_health_server(health)

    # ── Graceful shutdown via signals ─────────────────────────────────────────
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def _handle_signal(signum, frame):
        logger.info("Received signal %s — shutting down", signum)
        loop.call_soon_threadsafe(shutdown_event.set)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    # ── Start background tasks ────────────────────────────────────────────────
    burst_task = asyncio.ensure_future(_burst_snapshot_loop(snap, ws, health))
    instrument_task = asyncio.ensure_future(_instrument_refresh_loop(tracker, ws))
    disconnect_task = asyncio.ensure_future(_disconnect_alert_loop(health, ws))
    ws_task = asyncio.ensure_future(ws.run())

    health.notify_startup()
    logger.info("Tick recorder running. Data dir: %s", config.DATA_DIR)

    # ── Wait for shutdown signal ──────────────────────────────────────────────
    await shutdown_event.wait()
    logger.info("Shutdown initiated")
    health.notify_shutdown()

    # Cancel background tasks
    for task in (burst_task, instrument_task, disconnect_task):
        task.cancel()

    # Stop WebSocket cleanly
    await ws.stop()
    ws_task.cancel()

    # Final flush
    snap.flush_partial()
    logger.info("Final partial file flushed. Exiting.")


async def _sync_deribit_clock(snap):
    # type: (Snapshotter) -> None
    """Fetch Deribit server time via REST to prime the clock offset before any ticks arrive.

    This ensures the FIRST burst cycle fires at the correct Deribit-aligned boundary
    even before any WebSocket ticks have been received.  Subsequent cycles re-anchor
    automatically from live tick timestamps.
    """
    try:
        import requests as _requests
        loop = asyncio.get_event_loop()

        def _fetch():
            r = _requests.get(
                "https://www.deribit.com/api/v2/public/get_time",
                timeout=5,
            )
            return r.json()["result"]

        deribit_ms = await loop.run_in_executor(None, _fetch)
        snap.update_deribit_offset(int(deribit_ms) * 1000)
        offset_s = snap.deribit_offset_secs
        direction = "behind Deribit" if offset_s > 0 else "ahead of Deribit"
        logger.info("Deribit clock sync: server is %.3fs %s", abs(offset_s), direction)
        if abs(offset_s) > 1.0:
            logger.warning(
                "Server clock is %.3fs from Deribit — verify NTP is running!",
                abs(offset_s),
            )
    except Exception:
        logger.warning("Deribit clock pre-sync failed — using server clock for first burst cycle")


async def _burst_snapshot_loop(snap, ws, health):
    # type: (Snapshotter, DeribitWSClient, RecorderHealth) -> None
    """Drive the subscribe → snapshot → unsubscribe burst cycle.

    Timeline per cycle:
      sleep until (boundary − SNAPSHOT_LEAD_SECONDS)  →  subscribe all channels
      sleep until (boundary + 0.2s)                    →  take snapshot
      immediately                                       →  unsubscribe all channels
      sleep until next boundary − SNAPSHOT_LEAD_SECONDS →  repeat

    The +0.2s overshoot on snapshot_at is a safety buffer for timer imprecision;
    the timestamp written to parquet is always the exact floor-aligned boundary.
    """
    interval_secs = config.SNAPSHOT_INTERVAL_MIN * 60
    while True:
        # Compute absolute UTC targets for this cycle, anchored to Deribit
        # exchange time.  _deribit_time() re-reads the latest measured offset
        # on every cycle, so the schedule re-anchors automatically every 5 min.
        now = _deribit_time(snap)
        next_boundary = (int(now // interval_secs) + 1) * interval_secs
        subscribe_at = next_boundary - config.SNAPSHOT_LEAD_SECONDS
        snapshot_at = next_boundary + 0.2   # 200ms buffer — stored timestamp is exact

        # ── Sleep until subscribe time ────────────────────────────────────
        wait = subscribe_at - _deribit_time(snap)
        if wait > 0:
            await asyncio.sleep(wait)

        boundary_dt = datetime.fromtimestamp(next_boundary, tz=timezone.utc)
        logger.info("Burst open — boundary %s (%.1fs away)",
                    boundary_dt.strftime("%H:%M UTC"), next_boundary - _deribit_time(snap))

        if ws.is_connected:
            try:
                await ws.subscribe_snapshot_channels()
            except Exception:
                logger.exception("Subscribe error in burst window")
        else:
            logger.warning("WS not connected at burst start — snapshot will be incomplete")

        # ── Sleep until snapshot time ─────────────────────────────────────────
        wait = snapshot_at - _deribit_time(snap)
        if wait > 0:
            await asyncio.sleep(wait)

        # ── Take snapshot ─────────────────────────────────────────────────────
        try:
            prev_gaps = snap.gaps_today
            took = snap.maybe_snapshot()
            if took:
                health.update_from_snapshotter(snap)
                new_gaps = snap.gaps_today - prev_gaps
                if new_gaps > 0:
                    health.on_gap(new_gaps)
                free_mb = _disk_free_mb()
                if free_mb != -1 and free_mb < config.DISK_FREE_WARN_MB:
                    health.notify_critical(
                        f"Low disk space: only {free_mb} MB free. "
                        "Run sync.py to download and clean up data files."
                    )
        except Exception:
            logger.exception("Snapshot error in burst window")

        # ── Unsubscribe ───────────────────────────────────────────────────────
        if ws.is_connected:
            try:
                await ws.unsubscribe_snapshot_channels()
            except Exception:
                logger.exception("Unsubscribe error in burst window")

        logger.info("Burst closed")


async def _instrument_refresh_loop(tracker, ws):
    # type: (InstrumentTracker, DeribitWSClient) -> None
    """Refresh instrument list every INSTRUMENT_REFRESH_MIN minutes."""
    interval = config.INSTRUMENT_REFRESH_MIN * 60
    while True:
        await asyncio.sleep(interval)
        try:
            tracker.refresh()
            ws.set_instruments(tracker.instrument_names)
        except Exception:
            logger.exception("Instrument refresh loop error")


async def _disconnect_alert_loop(health, ws):
    # type: (RecorderHealth, DeribitWSClient) -> None
    """Send Telegram alert if a disconnect persists beyond the threshold."""
    while True:
        await asyncio.sleep(30)
        try:
            health.check_disconnect_alert()
        except Exception:
            logger.exception("Disconnect alert loop error")


def _disk_free_mb():
    # type: () -> int
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        return int(usage.free // (1024 * 1024))
    except Exception:
        return -1


def main():
    # type: () -> None
    config.setup_logging()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    logger.info("Starting Deribit BTC Options Tick Recorder")

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(_run_recorder())
    except SystemExit:
        raise
    except Exception:
        logger.exception("Recorder crashed")
        sys.exit(1)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
