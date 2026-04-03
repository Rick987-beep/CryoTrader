#!/usr/bin/env python3
"""
short_strangle_delta.py — Short N-DTE strangle selected by delta, SL + time/expiry exit.

Sells an OTM strangle on a Deribit expiry `dte` calendar days ahead (1, 2, or 3).
Legs are selected by target delta rather than a fixed USD offset from ATM:

    - Call leg: strike whose delta is closest to  +target_delta  (e.g. +0.25)
    - Put  leg: strike whose delta is closest to  -target_delta  (e.g. -0.25)

Delta values are read directly from the parquet snapshot (no API calls).
If no option with a non-zero delta exists for the expiry, the entry is
skipped silently — same "nearest available" policy as option_selection.py.

Exit logic:
    1. Stop-loss     — cost to buy back both legs exceeds stop_loss_pct ×
                       premium received.
    2. Max hold time — position has been open for max_hold_hours hours;
                       set max_hold_hours=0 to disable and hold to expiry.
    3. Expiry        — 08:00 UTC on the expiry date; settled at intrinsic
                       value; no close fees.

One entry per day; up to `dte + 1` positions may be open concurrently (a new
entry in the 01:00–07:59 UTC window can overlap with the prior day's position
before it expires at 08:00 UTC).
Entries allowed 01:00–23:00 UTC.

Grid parameters:
    dte             — days-to-expiry of the target expiry (1, 2, or 3)
    delta           — target absolute delta for each leg (e.g. 0.25 → sell
                      25-delta call and 25-delta put)
    entry_hour      — UTC hour at which to enter (one-hour window; valid 01–23)
    stop_loss_pct   — stop-loss as a fraction of premium received
    max_hold_hours  — maximum hours to hold; 0 = disabled (hold to expiry)

Pricing / fees:
    Sell at bid, buy back at ask (or mark if ask is absent).
    Deribit fee model: MIN(0.03% × index, 12.5% × option_price) per leg.
"""
import re
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Dict, List, Optional

from backtester.pricing import deribit_fee_per_leg, EXPIRY_HOUR_UTC
from backtester.strategy_base import (
    OpenPosition, Trade, close_trade,
    time_window, stop_loss_pct, max_hold_hours,
)


# ------------------------------------------------------------------
# Helpers (shared pattern with short_straddle_strangle)
# ------------------------------------------------------------------

@lru_cache(maxsize=64)
def _parse_expiry_date(expiry_code):
    # type: (str) -> Optional[datetime]
    """Parse a Deribit expiry code like '15MAR26' to a date-only datetime."""
    month_map = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
        "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
        "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    }
    m = re.match(r"(\d{1,2})([A-Z]{3})(\d{2})", expiry_code)
    if not m:
        return None
    day = int(m.group(1))
    month = month_map.get(m.group(2))
    year = 2000 + int(m.group(3))
    if month is None:
        return None
    return datetime(year, month, day)


@lru_cache(maxsize=64)
def _expiry_dt_utc(expiry_code, tzinfo):
    # type: (str, Any) -> Optional[datetime]
    """Return the UTC expiry deadline datetime for an expiry code."""
    exp_date = _parse_expiry_date(expiry_code)
    if exp_date is None:
        return None
    return exp_date.replace(hour=EXPIRY_HOUR_UTC, tzinfo=tzinfo)


def _select_expiry(state, dte):
    # type: (Any, int) -> Optional[str]
    """Return the expiry for `dte` calendar days ahead.

    E.g. dte=1 targets tomorrow's expiry (1DTE), dte=2 targets the day
    after tomorrow (2DTE), dte=3 targets three days out (3DTE).
    """
    target_date = state.dt.date() + timedelta(days=dte)
    for exp in state.expiries():
        exp_date = _parse_expiry_date(exp)
        if exp_date is not None and exp_date.date() == target_date:
            return exp
    return None


def _select_by_delta(chain, target_delta):
    # type: (list, float) -> Optional[Any]
    """Pick the option whose delta is closest to target_delta.

    Mirrors option_selection.py:
        min(options_list, key=lambda x: abs(x.get('delta', 0) - target_delta))

    Options with delta == 0.0 are skipped (missing / unpriced data).
    If no option has a non-zero delta, returns the closest by delta
    (including zeroes) rather than returning None, consistent with the
    "nearest available" contract.
    """
    candidates = [q for q in chain if q.delta != 0.0]
    if not candidates:
        candidates = chain  # last resort: use whatever is there
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(q.delta - target_delta))


# ------------------------------------------------------------------
# Strategy
# ------------------------------------------------------------------

class ShortStrangleDelta:
    """Sell N-DTE OTM strangle selected by delta; exit on SL, time exit, or expiry."""

    name = "short_strangle_delta"
    DATE_RANGE = ("2026-03-09", "2026-03-23")
    DESCRIPTION = (
        "Sells a strangle on a Deribit expiry N calendar days ahead (dte=1/2/3), "
        "with legs chosen by target delta (e.g. delta=0.25 → 25-delta call + put). "
        "One entry per day; up to dte+1 positions open concurrently. "
        "Entries allowed 01:00–23:00 UTC. "
        "Exits on stop-loss, optional max hold duration, or expiry settlement."
    )

    PARAM_GRID = {
        "dte":            [1, 2],
        "delta":          [0.1, 0.15, 0.2, 0.25, 0.3],
        "entry_hour":     [1, 4, 8, 12, 16, 20],
        "stop_loss_pct":  [0.75, 1.0, 1.5, 2.0, 3.0, 3.5],
        "max_hold_hours": [0, 8, 12, 24,36, 60]  # 0 = hold to expiry
    }

    def __init__(self):
        self._positions = []          # type: List[OpenPosition]
        self._dte = 1
        self._max_concurrent = 1      # set dynamically to _dte in configure()
        self._delta = 0.25
        self._sl_pct = 1.0
        self._entry_hour = 10
        self._max_hold_hours = 0
        self._last_trade_date = None  # type: Optional[Any]
        self._entry_conditions = []
        self._exit_conditions = []

    def configure(self, params):
        # type: (Dict[str, Any]) -> None
        self._dte = params.get("dte", 1)
        self._delta = params["delta"]
        self._sl_pct = params["stop_loss_pct"]
        self._entry_hour = params.get("entry_hour", 10)
        self._max_hold_hours = params.get("max_hold_hours", 0)
        self._max_concurrent = self._dte + 1  # e.g. 1DTE → 2, 2DTE → 3, 3DTE → 4
        self._positions = []
        self._last_trade_date = None

        self._entry_conditions = [
            time_window(self._entry_hour, self._entry_hour + 1),
        ]
        self._exit_conditions = [
            stop_loss_pct(self._sl_pct),
        ]
        if self._max_hold_hours > 0:
            self._exit_conditions.append(max_hold_hours(self._max_hold_hours))

    def on_market_state(self, state):
        # type: (Any) -> List[Trade]
        trades = []

        # Check exits on all open positions
        to_close = []
        for pos in list(self._positions):
            reason = self._check_expiry(state, pos)
            if reason is None:
                for exit_cond in self._exit_conditions:
                    reason = exit_cond(state, pos)
                    if reason:
                        break
            if reason and reason != "expiry":
                # Guard against 00:00 UTC day-boundary snapshots that carry
                # only a thin slice of the full chain. If either leg is absent
                # we defer the exit to the next tick rather than pricing at $0.
                expiry = pos.metadata["expiry"]
                if (state.get_option(expiry, pos.metadata["call_strike"], True) is None
                        or state.get_option(expiry, pos.metadata["put_strike"], False) is None):
                    reason = None  # data gap — retry next tick
            if reason:
                trades.append(self._close(state, pos, reason))
                to_close.append(pos)
        for pos in to_close:
            self._positions.remove(pos)

        # Check entry: one entry per day, up to _max_concurrent concurrent
        if len(self._positions) < self._max_concurrent:
            today = state.dt.date()
            if self._last_trade_date != today:
                if all(cond(state) for cond in self._entry_conditions):
                    self._try_open(state)

        return trades

    def on_end(self, state):
        # type: (Any) -> List[Trade]
        trades = []
        for pos in list(self._positions):
            trades.append(self._close(state, pos, "end_of_data"))
        self._positions.clear()
        return trades

    def reset(self):
        # type: () -> None
        self._positions = []
        self._last_trade_date = None

    def describe_params(self):
        # type: () -> Dict[str, Any]
        return {
            "dte":            self._dte,
            "delta":          self._delta,
            "stop_loss_pct":  self._sl_pct,
            "entry_hour":     self._entry_hour,
            "max_hold_hours": self._max_hold_hours,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_expiry(self, state, pos):
        # type: (Any, OpenPosition) -> Optional[str]
        """Return 'expiry' if the position's expiry deadline has passed."""
        exp_dt = pos.metadata.get("expiry_dt")
        if exp_dt is None:
            return None
        if state.dt >= exp_dt:
            return "expiry"
        return None

    def _try_open(self, state):
        # type: (Any) -> None
        """Sell delta-selected OTM strangle on the expiry `dte` days ahead."""
        expiry = _select_expiry(state, self._dte)
        if expiry is None:
            return

        chain = state.get_chain(expiry)
        if not chain:
            return

        calls = [q for q in chain if q.is_call]
        puts  = [q for q in chain if not q.is_call]

        # Mirror option_selection.py: nearest delta to +target / -target
        call = _select_by_delta(calls, +self._delta)
        put  = _select_by_delta(puts,  -self._delta)

        if call is None or put is None:
            return

        # Skip if either bid is zero (no market)
        if call.bid <= 0 or put.bid <= 0:
            return

        call_entry_usd = call.bid_usd
        put_entry_usd  = put.bid_usd
        entry_usd = call_entry_usd + put_entry_usd
        if entry_usd <= 0:
            return

        fee_call = deribit_fee_per_leg(state.spot, call_entry_usd)
        fee_put  = deribit_fee_per_leg(state.spot, put_entry_usd)
        exp_dt   = _expiry_dt_utc(expiry, state.dt.tzinfo)

        pos = OpenPosition(
            entry_time=state.dt,
            entry_spot=state.spot,
            legs=[
                {
                    "strike": call.strike, "is_call": True,
                    "expiry": expiry, "side": "sell",
                    "entry_price": call.bid, "entry_price_usd": call_entry_usd,
                    "entry_delta": call.delta,
                },
                {
                    "strike": put.strike, "is_call": False,
                    "expiry": expiry, "side": "sell",
                    "entry_price": put.bid, "entry_price_usd": put_entry_usd,
                    "entry_delta": put.delta,
                },
            ],
            entry_price_usd=entry_usd,
            fees_open=fee_call + fee_put,
            metadata={
                "target_delta": self._delta,
                "expiry":       expiry,
                "expiry_dt":    exp_dt,
                "direction":    "sell",
                "call_strike":  call.strike,
                "put_strike":   put.strike,
                "call_delta":   call.delta,
                "put_delta":    put.delta,
            },
        )
        self._positions.append(pos)
        self._last_trade_date = state.dt.date()

    def _close(self, state, pos, reason):
        # type: (Any, OpenPosition, str) -> Trade
        """Close a short strangle position and record the trade."""
        expiry     = pos.metadata["expiry"]
        call_strike = pos.metadata["call_strike"]
        put_strike  = pos.metadata["put_strike"]

        if reason == "expiry":
            # What we owe at settlement: intrinsic value of each leg
            call_exit_usd = max(0.0, state.spot - call_strike)
            put_exit_usd  = max(0.0, put_strike  - state.spot)
        else:
            # Buy back at ask; fall back to mark if ask is absent.
            # Last resort: entry price (zero net move) rather than 0.0
            # to avoid phantom full-premium profit on missing data.
            call_q = state.get_option(expiry, call_strike, True)
            put_q  = state.get_option(expiry, put_strike,  False)
            call_exit_usd = (call_q.ask_usd if call_q and call_q.ask > 0
                             else (call_q.mark_usd if call_q
                                   else pos.legs[0]["entry_price_usd"]))
            put_exit_usd  = (put_q.ask_usd if put_q and put_q.ask > 0
                             else (put_q.mark_usd if put_q
                                   else pos.legs[1]["entry_price_usd"]))

        exit_usd   = call_exit_usd + put_exit_usd
        fees_close = 0.0 if reason == "expiry" else (
            deribit_fee_per_leg(state.spot, call_exit_usd) +
            deribit_fee_per_leg(state.spot, put_exit_usd)
        )

        trade = close_trade(state, pos, reason, exit_usd, fees_close)
        trade.metadata["dte"]            = self._dte
        trade.metadata["stop_loss_pct"]  = self._sl_pct
        trade.metadata["max_hold_hours"] = self._max_hold_hours
        return trade
