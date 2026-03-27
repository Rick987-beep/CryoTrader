#!/usr/bin/env python3
"""
config.py — Tick Recorder Settings

All settings are overridable via environment variables prefixed RECORDER_.
Load your .env before importing this module.
"""
import logging
import logging.handlers
import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _int(name, default):
    # type: (str, int) -> int
    val = os.getenv(name, "").strip()
    if val:
        try:
            return int(val)
        except ValueError:
            pass
    return default


def _str(name, default):
    # type: (str, str) -> str
    return os.getenv(name, default).strip() or default


# ── Deribit ───────────────────────────────────────────────────────────────────

DERIBIT_WS_URL = "wss://www.deribit.com/ws/api/v2"

# ── Snapshot / storage ────────────────────────────────────────────────────────

SNAPSHOT_INTERVAL_MIN = _int("RECORDER_SNAPSHOT_INTERVAL_MIN", 5)
SPOT_INTERVAL_MIN = _int("RECORDER_SPOT_INTERVAL_MIN", 1)
DATA_DIR = _str("RECORDER_DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))

# ── Reconnect backoff ─────────────────────────────────────────────────────────

# Attempt 1-3:  RECONNECT_FAST_DELAY    (transient blip)
# Attempt 4-6:  RECONNECT_MID_DELAY     (brief outage)
# Attempt 7+:   RECONNECT_MAX_DELAY     (maintenance window — runs indefinitely)
RECONNECT_FAST_DELAY = _int("RECORDER_RECONNECT_FAST_DELAY", 5)
RECONNECT_MID_DELAY = _int("RECORDER_RECONNECT_MID_DELAY", 30)
RECONNECT_MAX_DELAY = _int("RECORDER_RECONNECT_MAX_DELAY", 120)

# ── Instrument refresh ────────────────────────────────────────────────────────

INSTRUMENT_REFRESH_MIN = _int("RECORDER_INSTRUMENT_REFRESH_MIN", 30)

# ── Burst-subscribe window ────────────────────────────────────────────────────

# How many seconds before a snapshot boundary to subscribe to all channels.
# 10s gives an 8s+ buffer for Deribit to push the initial state dump.
SNAPSHOT_LEAD_SECONDS = _int("RECORDER_SNAPSHOT_LEAD_SECONDS", 10)

# ── Health endpoint ───────────────────────────────────────────────────────────

HEALTH_PORT = _int("RECORDER_HEALTH_PORT", 8090)

# ── Disk guard ────────────────────────────────────────────────────────────────

DISK_FREE_WARN_MB = _int("RECORDER_DISK_FREE_WARN_MB", 500)
DISK_FREE_ABORT_MB = _int("RECORDER_DISK_FREE_ABORT_MB", 100)

# ── Telegram alert throttle ───────────────────────────────────────────────────

# Minimum seconds between repeated Telegram alerts of the same type.
ALERT_THROTTLE_SECONDS = _int("RECORDER_ALERT_THROTTLE_SECONDS", 1800)  # 30 min

# After how many seconds of disconnection to send the first gap alert.
DISCONNECT_ALERT_AFTER_SECONDS = _int("RECORDER_DISCONNECT_ALERT_AFTER_SECONDS", 300)  # 5 min

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_MAX_BYTES = _int("RECORDER_LOG_MAX_BYTES", 10_000_000)   # 10 MB
LOG_BACKUP_COUNT = _int("RECORDER_LOG_BACKUP_COUNT", 3)      # 3 rotated files = 40 MB max
LOG_LEVEL = _str("RECORDER_LOG_LEVEL", "INFO")


def setup_logging(log_file=None):
    # type: (str | None) -> None
    """Configure root logger.

    If log_file is given, uses RotatingFileHandler (capped at LOG_MAX_BYTES
    × LOG_BACKUP_COUNT). Otherwise logs to stdout, suitable for systemd
    journal capture.
    """
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    if log_file:
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
        )
    else:
        handler = logging.StreamHandler(sys.stdout)

    handler.setFormatter(fmt)
    root.addHandler(handler)
