#!/usr/bin/env python3
"""
health.py — HTTP Health Endpoint and Telegram Alerts

Exposes GET /health on localhost:HEALTH_PORT returning JSON status.
Runs in a daemon thread so it never blocks the asyncio event loop.

Telegram alert policy (silent by design):
  SENT:     Service started, service stopping, snapshot gap detected, critical error
  NOT SENT: Individual reconnects, instrument list changes, brief disconnects < 5 min
  THROTTLE: Same alert type at most once per ALERT_THROTTLE_SECONDS (30 min default)
"""
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from backtester.ingest.tickrecorder import config

logger = logging.getLogger(__name__)


class AlertThrottle:
    """Per-type Telegram alert throttle. Ensures silence during known outage windows."""

    def __init__(self, throttle_seconds=None):
        self._throttle = throttle_seconds or config.ALERT_THROTTLE_SECONDS
        self._last_sent = {}    # type: dict  # alert_type -> monotonic timestamp
        self._lock = threading.Lock()

    def should_send(self, alert_type):
        # type: (str) -> bool
        with self._lock:
            last = self._last_sent.get(alert_type)
            if last is None or time.monotonic() - last >= self._throttle:
                self._last_sent[alert_type] = time.monotonic()
                return True
            return False

    def reset(self, alert_type):
        # type: (str) -> None
        """Force-allow next send for this type (e.g. after recovery)."""
        with self._lock:
            self._last_sent.pop(alert_type, None)


class RecorderHealth:
    """Shared health state, updated by recorder.py, read by the HTTP handler.

    Designed for lock-free reads — individual field writes are atomic in CPython.
    """

    def __init__(self):
        self.start_time = time.monotonic()
        self.ws_connected = False
        self.ws_reconnects = 0
        self.last_snapshot_ts = None        # type: Optional[datetime]
        self.instruments_tracked = 0
        self.snapshots_today = 0
        self.gaps_today = 0
        self.last_disconnect_time = None    # type: Optional[float]  # monotonic
        self._alert = AlertThrottle()
        self._notifier = None               # set lazily

    # ── Convenience setters called from recorder.py ──────────────────────────

    def update_from_snapshotter(self, snapshotter):
        # type: (object) -> None
        self.last_snapshot_ts = snapshotter.last_snapshot_ts
        self.instruments_tracked = snapshotter.instruments_tracked
        self.snapshots_today = snapshotter.snapshots_today
        self.gaps_today = snapshotter.gaps_today

    def on_connected(self):
        # type: () -> None
        """Called when WebSocket (re)connects."""
        was_disconnected = self.last_disconnect_time is not None
        downtime = 0.0
        if was_disconnected:
            downtime = time.monotonic() - self.last_disconnect_time
        self.ws_connected = True
        self.last_disconnect_time = None

        if was_disconnected and downtime >= config.DISCONNECT_ALERT_AFTER_SECONDS:
            mins = int(downtime // 60)
            self._send_alert(
                "recovery",
                f"Tick recorder reconnected after {mins}min downtime. "
                f"Gaps today: {self.gaps_today}",
            )

    def on_disconnected(self):
        # type: () -> None
        """Called when WebSocket drops."""
        self.ws_reconnects += 1
        self.ws_connected = False
        if self.last_disconnect_time is None:
            self.last_disconnect_time = time.monotonic()

    def check_disconnect_alert(self):
        # type: () -> None
        """Call periodically — sends the 'still down' alert if threshold passed."""
        if self.last_disconnect_time is None:
            return
        downtime = time.monotonic() - self.last_disconnect_time
        if downtime >= config.DISCONNECT_ALERT_AFTER_SECONDS:
            mins = int(downtime // 60)
            self._send_alert(
                "disconnect",
                f"Tick recorder disconnected for {mins}min. Will keep retrying.",
            )

    def on_gap(self, missed_count):
        # type: (int) -> None
        """Called by snapshotter when a gap is detected."""
        self._send_alert(
            "gap",
            f"Tick recorder missed {missed_count} snapshot slot(s). "
            f"Data gap recorded.",
            force=True,  # gaps always send (already throttled by their nature)
        )

    def notify_startup(self):
        # type: () -> None
        self._send_alert("startup", "Tick recorder started.", force=True)

    def notify_shutdown(self):
        # type: () -> None
        self._send_alert("shutdown", "Tick recorder stopping cleanly.", force=True)

    def notify_critical(self, message):
        # type: (str) -> None
        self._send_alert("critical", f"Tick recorder CRITICAL: {message}", force=True)

    # ── HTTP health response ──────────────────────────────────────────────────

    def to_dict(self):
        # type: () -> dict
        last_snap = None
        if self.last_snapshot_ts is not None:
            last_snap = self.last_snapshot_ts.strftime("%Y-%m-%dT%H:%M:%SZ")

        disk_free_mb = _disk_free_mb()

        return {
            "status": "ok" if self.ws_connected else "disconnected",
            "uptime_seconds": int(time.monotonic() - self.start_time),
            "last_snapshot_ts": last_snap,
            "instruments_tracked": self.instruments_tracked,
            "snapshots_today": self.snapshots_today,
            "gaps_today": self.gaps_today,
            "ws_connected": self.ws_connected,
            "ws_reconnects": self.ws_reconnects,
            "disk_free_mb": disk_free_mb,
        }

    # ── Internal ─────────────────────────────────────────────────────────────

    def _get_notifier(self):
        if self._notifier is None:
            try:
                import sys
                import os
                # Allow import from parent project root
                root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                )
                if root not in sys.path:
                    sys.path.insert(0, root)
                from telegram_notifier import get_notifier
                self._notifier = get_notifier()
            except ImportError:
                self._notifier = False   # Sentinel: not available
        return self._notifier if self._notifier is not False else None

    def _send_alert(self, alert_type, message, force=False):
        # type: (str, str, bool) -> None
        if not force and not self._alert.should_send(alert_type):
            return
        if force:
            self._alert.reset(alert_type)
        notifier = self._get_notifier()
        if notifier is not None:
            notifier.send(f"[Recorder] {message}")
        logger.info("Alert [%s]: %s", alert_type, message)


# ── HTTP Server ───────────────────────────────────────────────────────────────

def _disk_free_mb():
    # type: () -> int
    try:
        usage = shutil.disk_usage(config.DATA_DIR)
        return int(usage.free // (1024 * 1024))
    except Exception:
        return -1


class _HealthHandler(BaseHTTPRequestHandler):
    health = None  # type: RecorderHealth  # injected at server creation

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = json.dumps(self.health.to_dict()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            self.send_response(500)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress default access log spam
        pass


def start_health_server(health):
    # type: (RecorderHealth) -> HTTPServer
    """Start health HTTP server in a daemon thread. Returns the server instance."""

    class Handler(_HealthHandler):
        pass
    Handler.health = health

    server = HTTPServer(("127.0.0.1", config.HEALTH_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health endpoint running on http://127.0.0.1:%d/health", config.HEALTH_PORT)
    return server
