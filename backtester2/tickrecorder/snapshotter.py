#!/usr/bin/env python3
"""
snapshotter.py — 5-Minute Aggregation and Parquet Writer

Receives raw ticks from ws_client, maintains last-known state per
instrument, and writes 5-min snapshots to zstd-compressed parquet
files that match the backtester2 schema exactly.

Memory guarantees:
  - Tick dict bounded by active instrument count (~500 entries)
  - Spot bar list bounded: only current day's 1-min bars kept
  - Daily snapshot buffer cleared at midnight UTC rotation
  - Expired instruments removed by notifying via remove_instruments()

Crash recovery:
  - After every snapshot: atomically overwrites .partial_options_YYYY-MM-DD.parquet
  - On startup: load_partial() restores today's data if a partial file exists
"""
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backtester2.tickrecorder import config

logger = logging.getLogger(__name__)

# Instrument key: (expiry_str, strike_float, is_call_bool)
InstrumentKey = Tuple[str, float, bool]

# Parquet column schema — matches backtester2 snapshot_builder.py exactly
_OPTION_COLS = [
    "timestamp", "expiry", "strike", "is_call",
    "underlying_price", "bid_price", "ask_price",
    "mark_price", "mark_iv", "delta",
]

_SPOT_COLS = ["timestamp", "open", "high", "low", "close"]


@dataclass
class _TickState:
    """Last-known ticker state for one option instrument."""
    underlying_price: float = float("nan")
    bid_price: float = float("nan")
    ask_price: float = float("nan")
    mark_price: float = float("nan")
    mark_iv: float = float("nan")
    delta: float = float("nan")


@dataclass
class _SpotMinute:
    """Accumulator for one 1-min spot OHLC bar."""
    open: float
    high: float
    low: float
    close: float
    bar_ts: int   # 1-min aligned timestamp in microseconds


class Snapshotter:
    """Aggregates live ticks into 5-min snapshots and writes daily parquets.

    Wired up by recorder.py:
        snap = Snapshotter()
        ws_client.on_ticker(snap.on_tick)
    """

    def __init__(self):
        # Tick state dict — keys are InstrumentKey tuples
        self._ticks = {}        # type: Dict[InstrumentKey, _TickState]

        # Spot (BTC-PERPETUAL) tracking
        self._spot_current_bar = None   # type: Optional[_SpotMinute]
        self._spot_bars_today = []      # type: List[_SpotMinute]  # bounded: ≤1440/day

        # Accumulated daily snapshot rows — cleared at midnight
        self._daily_option_rows = []    # type: List[dict]
        self._daily_spot_rows = []      # type: List[dict]

        # Timer state
        self._last_snapshot_ts = None   # type: Optional[datetime]
        self._next_snapshot_ts = None   # type: Optional[int]  # unix µs, aligned

        # Statistics
        self._snapshots_today = 0
        self._gaps_today = 0
        self._current_date = None       # type: Optional[str]  # "2026-03-27"

        os.makedirs(config.DATA_DIR, exist_ok=True)

    # ── External API ─────────────────────────────────────────────────────────

    def remove_instruments(self, expired_keys):
        # type: (set) -> None
        """Remove expired instruments from tick dict (called by instruments tracker).
        Keeps dict bounded — prevents unbounded growth over months."""
        removed = 0
        for key in expired_keys:
            if key in self._ticks:
                del self._ticks[key]
                removed += 1
        if removed:
            logger.debug("Removed %d expired instruments from tick buffer", removed)

    def on_tick(self, channel, data):
        # type: (str, dict) -> None
        """Handle one incoming ticker or index price message from ws_client."""
        if channel.startswith("deribit_price_index."):
            self._handle_spot_tick(data)
            return

        instrument_name = data.get("instrument_name", "")
        key = _parse_key(instrument_name)
        if key is None:
            return

        state = self._ticks.get(key)
        if state is None:
            state = _TickState()
            self._ticks[key] = state

        # Update last-known fields — use NaN for missing/zero values
        state.underlying_price = float(data.get("underlying_price") or float("nan"))
        state.bid_price = _opt_float(data.get("best_bid_price"))
        state.ask_price = _opt_float(data.get("best_ask_price"))
        state.mark_price = _opt_float(data.get("mark_price"))
        state.mark_iv = _opt_float(data.get("mark_iv"))
        greeks = data.get("greeks") or {}
        state.delta = _opt_float(greeks.get("delta"))

    def maybe_snapshot(self):
        # type: () -> bool
        """Take a snapshot if the next 5-min boundary has passed.

        Called periodically by the asyncio timer in recorder.py.
        Returns True if a snapshot was written.
        """
        now_us = _now_us()
        now_dt = _us_to_dt(now_us)
        today_str = now_dt.strftime("%Y-%m-%d")

        # Midnight rotation — clear daily buffers
        if self._current_date is not None and today_str != self._current_date:
            self._rotate_day(self._current_date)

        if self._current_date is None:
            self._current_date = today_str

        boundary_us = _aligned_boundary(now_us, config.SNAPSHOT_INTERVAL_MIN)
        if self._next_snapshot_ts is None:
            self._next_snapshot_ts = boundary_us

        if now_us < self._next_snapshot_ts:
            return False

        # Check for gap (missed boundary)
        if self._last_snapshot_ts is not None:
            expected_interval_us = config.SNAPSHOT_INTERVAL_MIN * 60 * 1_000_000
            expected_next = _aligned_boundary(
                int(self._last_snapshot_ts.timestamp() * 1_000_000),
                config.SNAPSHOT_INTERVAL_MIN
            ) + expected_interval_us
            if self._next_snapshot_ts > expected_next:
                missed = (self._next_snapshot_ts - expected_next) // expected_interval_us
                if missed > 0:
                    self._gaps_today += int(missed)
                    logger.warning(
                        "Gap detected: %d snapshot(s) missed (last=%s, now=%s)",
                        missed,
                        self._last_snapshot_ts.isoformat(),
                        now_dt.isoformat(),
                    )

        self._write_snapshot(self._next_snapshot_ts)
        self._last_snapshot_ts = now_dt
        self._snapshots_today += 1
        self._next_snapshot_ts += config.SNAPSHOT_INTERVAL_MIN * 60 * 1_000_000
        return True

    def flush_partial(self):
        # type: () -> None
        """Write current daily buffer as a partial file (crash recovery).
        Uses atomic rename to avoid corrupt reads."""
        if not self._daily_option_rows:
            return
        date_str = self._current_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self._write_parquet_atomic(date_str, partial=True)

    def load_partial(self, date_str):
        # type: (str) -> int
        """Load a partial file from a previous run into the daily buffer.

        Returns the number of snapshot rows loaded (0 if no partial exists).
        """
        partial_path = _partial_path(date_str)
        if not os.path.exists(partial_path):
            return 0
        try:
            df = pd.read_parquet(partial_path)
            self._daily_option_rows = df.to_dict("records")
            self._current_date = date_str
            if self._daily_option_rows:
                last_ts = max(r["timestamp"] for r in self._daily_option_rows)
                self._last_snapshot_ts = _us_to_dt(last_ts)
                self._next_snapshot_ts = last_ts + config.SNAPSHOT_INTERVAL_MIN * 60 * 1_000_000
            logger.info(
                "Loaded partial snapshot file: %d rows from %s",
                len(self._daily_option_rows), partial_path,
            )
            return len(df["timestamp"].unique()) if len(df) > 0 else 0
        except Exception as exc:
            logger.warning("Failed to load partial file %s: %s", partial_path, exc)
            return 0

    # ── Stats (for health.py) ────────────────────────────────────────────────

    @property
    def instruments_tracked(self):
        # type: () -> int
        return len(self._ticks)

    @property
    def snapshots_today(self):
        # type: () -> int
        return self._snapshots_today

    @property
    def gaps_today(self):
        # type: () -> int
        return self._gaps_today

    @property
    def last_snapshot_ts(self):
        # type: () -> Optional[datetime]
        return self._last_snapshot_ts

    # ── Internal ─────────────────────────────────────────────────────────────

    def _handle_spot_tick(self, data):
        # type: (dict) -> None
        """Update 1-min OHLC accumulator for deribit_price_index.btc_usd."""
        price = _opt_float(data.get("price"))
        if price is None or price != price:  # NaN check
            return

        now_us = _now_us()
        bar_interval_us = config.SPOT_INTERVAL_MIN * 60 * 1_000_000
        bar_ts = (now_us // bar_interval_us) * bar_interval_us

        if self._spot_current_bar is None or bar_ts != self._spot_current_bar.bar_ts:
            # Seal previous bar
            if self._spot_current_bar is not None:
                self._spot_bars_today.append(self._spot_current_bar)
            self._spot_current_bar = _SpotMinute(
                open=price, high=price, low=price, close=price, bar_ts=bar_ts
            )
        else:
            b = self._spot_current_bar
            if price > b.high:
                b.high = price
            if price < b.low:
                b.low = price
            b.close = price

    def _write_snapshot(self, boundary_us):
        # type: (int) -> None
        """Freeze current tick state into one 5-min snapshot row set."""
        for (expiry, strike, is_call), state in self._ticks.items():
            self._daily_option_rows.append({
                "timestamp": boundary_us,
                "expiry": expiry,
                "strike": np.float32(strike),
                "is_call": is_call,
                "underlying_price": np.float32(state.underlying_price),
                "bid_price": np.float32(state.bid_price),
                "ask_price": np.float32(state.ask_price),
                "mark_price": np.float32(state.mark_price),
                "mark_iv": np.float32(state.mark_iv),
                "delta": np.float32(state.delta),
            })

        # Seal current spot bar into today's list
        if self._spot_current_bar is not None:
            self._spot_bars_today.append(self._spot_current_bar)
            self._spot_current_bar = None

        self.flush_partial()

    def _rotate_day(self, date_str):
        # type: (str) -> None
        """Write final daily files for date_str and clear buffers."""
        logger.info("Day rotation: writing final parquets for %s", date_str)
        self._write_parquet_atomic(date_str, partial=False)

        # Clear buffers — bounded memory guarantee
        self._daily_option_rows = []
        self._daily_spot_rows = []
        self._spot_bars_today = []
        self._spot_current_bar = None
        self._snapshots_today = 0
        self._gaps_today = 0
        self._current_date = None
        self._last_snapshot_ts = None
        self._next_snapshot_ts = None

        # Remove partial file now that we have the final one
        partial = _partial_path(date_str)
        if os.path.exists(partial):
            try:
                os.remove(partial)
            except OSError:
                pass

    def _write_parquet_atomic(self, date_str, partial=False):
        # type: (str, bool) -> None
        """Write options + spot parquets. Atomic: write to .tmp then rename."""
        if not self._daily_option_rows:
            return

        # Options parquet
        opt_df = pd.DataFrame(self._daily_option_rows, columns=_OPTION_COLS)
        opt_df["expiry"] = opt_df["expiry"].astype("category")
        opt_df.sort_values(["timestamp", "expiry", "strike", "is_call"], inplace=True)
        opt_df.reset_index(drop=True, inplace=True)

        if partial:
            final_path = _partial_path(date_str)
        else:
            final_path = os.path.join(config.DATA_DIR, f"options_{date_str}.parquet")

        _atomic_write_parquet(opt_df, final_path)

        # Spot track parquet — compile from accumulated bars
        all_bars = list(self._spot_bars_today)
        if self._spot_current_bar is not None:
            all_bars.append(self._spot_current_bar)

        if all_bars:
            spot_df = pd.DataFrame([
                {
                    "timestamp": b.bar_ts,
                    "open": np.float32(b.open),
                    "high": np.float32(b.high),
                    "low": np.float32(b.low),
                    "close": np.float32(b.close),
                }
                for b in all_bars
            ], columns=_SPOT_COLS)
            spot_df.sort_values("timestamp", inplace=True)
            spot_df.drop_duplicates("timestamp", keep="last", inplace=True)
            spot_df.reset_index(drop=True, inplace=True)

            if partial:
                spot_path = _partial_path(date_str, spot=True)
            else:
                spot_path = os.path.join(config.DATA_DIR, f"spot_track_{date_str}.parquet")

            _atomic_write_parquet(spot_df, spot_path)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_key(instrument_name):
    # type: (str) -> Optional[InstrumentKey]
    """Parse BTC-28MAR26-80000-C → ("28MAR26", 80000.0, True)."""
    parts = instrument_name.split("-")
    if len(parts) != 4 or parts[0] != "BTC":
        return None
    _, expiry, strike_str, cp = parts
    if cp not in ("C", "P"):
        return None
    try:
        return (expiry, float(strike_str), cp == "C")
    except ValueError:
        return None


def _opt_float(value):
    # type: (object) -> float
    """Convert value to float; return NaN if None/zero/invalid."""
    if value is None:
        return float("nan")
    try:
        f = float(value)
        return f if f == f else float("nan")  # NaN passthrough
    except (TypeError, ValueError):
        return float("nan")


def _now_us():
    # type: () -> int
    """Current UTC time in microseconds."""
    import time as _t
    return int(_t.time() * 1_000_000)


def _us_to_dt(us):
    # type: (int) -> datetime
    return datetime.fromtimestamp(us / 1_000_000, tz=timezone.utc)


def _aligned_boundary(now_us, interval_min):
    # type: (int, int) -> int
    """Return the current (not next) interval-aligned boundary in µs."""
    interval_us = interval_min * 60 * 1_000_000
    return (now_us // interval_us) * interval_us


def _partial_path(date_str, spot=False):
    # type: (str, bool) -> str
    prefix = "spot_track" if spot else "options"
    return os.path.join(config.DATA_DIR, f".partial_{prefix}_{date_str}.parquet")


def _atomic_write_parquet(df, final_path):
    # type: (pd.DataFrame, str) -> None
    """Write parquet to a .tmp file then rename — atomic on Linux/macOS."""
    tmp_path = final_path + ".tmp"
    try:
        df.to_parquet(tmp_path, compression="zstd", index=False)
        os.replace(tmp_path, final_path)  # atomic rename
        logger.debug("Wrote %s (%d rows)", os.path.basename(final_path), len(df))
    except Exception as exc:
        logger.error("Failed to write parquet %s: %s", final_path, exc)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
