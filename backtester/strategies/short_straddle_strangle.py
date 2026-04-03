#!/usr/bin/env python3
"""
short_straddle_strangle.py — Short 1DTE straddle/strangle, SL + time/expiry exit.

Sells an ATM straddle (offset=0) or OTM strangle (offset>0) on the next-day
Deribit expiry, collects the combined call+put premium, and exits when the
first of these conditions fires:

    1. Stop-loss     — cost to buy back both legs exceeds stop_loss_pct ×
                       premium received (e.g. stop_loss_pct=1.0 → close when
                       buyback costs 2× what we sold for, i.e. a 100% loss).
    2. Max hold time — position has been open for max_hold_hours hours;
                       set max_hold_hours=0 to disable and hold to expiry.
    3. Expiry        — 08:00 UTC on the expiry date; settled at intrinsic value
                       (call + put payoffs owed to counterparty); no close fees.

One entry per day: entry is blocked if a trade was already opened today.
Up to 2 positions may be open concurrently (e.g. yesterday's strangle still
live when today's pre-08:00 entry fires). Entries allowed 01:00–23:00 UTC.

Expiry selection:
    Always targets the next calendar day's expiry (08:00 UTC on date+1).
    Entry at 01:00 UTC gives ~31 h to expiry; entry at 20:00 UTC gives ~12 h.
    Tick is skipped silently if no matching expiry exists in the snapshot.

Grid parameters:
    offset        — USD distance from ATM for strangle legs (0 = ATM straddle)
    entry_hour    — UTC hour at which to enter (one-hour window; valid 01–23,
                    all days including weekends; expiry always next calendar day)
    stop_loss_pct   — stop-loss as a fraction of premium received
    max_hold_hours  — maximum hours to hold before forced close;
                      0 = disabled, hold all the way to expiry

Pricing:
    Sell at bid, buy back at ask (or mark if ask is absent).

Fees:
    Deribit model: MIN(0.03% × index, 12.5% × option_price) per leg,
    charged on both open and close.
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
# Helpers (shared with daily_put_sell pattern)
# ------------------------------------------------------------------

@lru_cache(maxsize=64)
def _parse_expiry_date(expiry_code):
    # type: (str) -> Optional[datetime]
    """Parse Deribit expiry code like '15MAR26' to a datetime."""
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


def _select_expiry(state):
    # type: (Any) -> Optional[str]
    """Return the expiry for the following calendar day.

    All entries target tomorrow's Deribit expiry (08:00 UTC on date+1),
    regardless of the current UTC hour. Gives ~31 h to expiry at 01:00 UTC
    and ~12 h to expiry at 20:00 UTC.
    """
    target_date = state.dt.date() + timedelta(days=1)
    for exp in state.expiries():
        exp_date = _parse_expiry_date(exp)
        if exp_date is not None and exp_date.date() == target_date:
            return exp
    return None  # No expiry for tomorrow → entry silently skipped


# ------------------------------------------------------------------
# Strategy
# ------------------------------------------------------------------

class ShortStraddleStrangle:
    """Sell 1DTE ATM straddle or OTM strangle; exit on SL, time exit, or expiry."""

    name = "short_straddle_strangle"
    DATE_RANGE = ("2026-03-09", "2026-03-23")
    DESCRIPTION = (
        "Sells a straddle or strangle on the next calendar day's Deribit expiry. "
        "One entry per day; up to 2 positions open concurrently (yesterday's may "
        "overlap with today's pre-08:00 entry). Entries allowed 01:00–23:00 UTC. "
        "Exits on stop-loss, optional max hold duration, or expiry settlement."
    )

    PARAM_GRID = {
        "offset":          [0, 500, 1000, 1500, 2000],
        "entry_hour":      [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
        "stop_loss_pct":   [0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5],
        "max_hold_hours":  [0,8,10,12,14,16,18,20,22]  # 0 = hold to expiry
    }

    def __init__(self):
        self._positions = []        # type: List[OpenPosition]
        self._max_concurrent = 2
        self._offset = 0
        self._sl_pct = 1.0
        self._entry_hour = 10
        self._max_hold_hours = 0
        self._last_trade_date = None  # type: Optional[Any]
        self._entry_conditions = []
        self._exit_conditions = []

    def configure(self, params):
        # type: (Dict[str, Any]) -> None
        self._offset = params["offset"]
        self._sl_pct = params["stop_loss_pct"]
        self._entry_hour = params.get("entry_hour", 10)
        self._max_hold_hours = params.get("max_hold_hours", 0)
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
                # Guard: verify option data is available before closing.
                # The 00:00 UTC snapshot is a day-boundary artifact: it exists
                # in the parquet but carries only 1–68 of ~470 instruments.
                # Our specific legs are almost never among them, so get_option()
                # would return None and _close() would price the exit at $0 —
                # giving a phantom full-premium profit.  Defer to next tick.
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
            "offset":         self._offset,
            "stop_loss_pct":  self._sl_pct,
            "entry_hour":     self._entry_hour,
            "max_hold_hours": self._max_hold_hours,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_expiry(self, state, pos):
        # type: (Any, OpenPosition) -> Optional[str]
        """Check if the given position's expiry deadline has passed."""
        exp_dt = pos.metadata.get("expiry_dt")
        if exp_dt is None:
            return None
        if state.dt >= exp_dt:
            return "expiry"
        return None

    def _try_open(self, state):
        # type: (Any) -> None
        """Sell ATM straddle or OTM strangle on the following day's expiry."""
        expiry = _select_expiry(state)
        if expiry is None:
            return

        if self._offset == 0:
            call, put = state.get_straddle(expiry)
        else:
            call, put = state.get_strangle(expiry, self._offset)

        if call is None or put is None:
            return

        # Skip if either bid is zero (no market)
        if call.bid <= 0 or put.bid <= 0:
            return

        call_entry_usd = call.bid_usd
        put_entry_usd = put.bid_usd
        entry_usd = call_entry_usd + put_entry_usd
        if entry_usd <= 0:
            return

        fee_call = deribit_fee_per_leg(state.spot, call_entry_usd)
        fee_put = deribit_fee_per_leg(state.spot, put_entry_usd)
        exp_dt = _expiry_dt_utc(expiry, state.dt.tzinfo)

        pos = OpenPosition(
            entry_time=state.dt,
            entry_spot=state.spot,
            legs=[
                {
                    "strike": call.strike, "is_call": True,
                    "expiry": expiry, "side": "sell",
                    "entry_price": call.bid, "entry_price_usd": call_entry_usd,
                },
                {
                    "strike": put.strike, "is_call": False,
                    "expiry": expiry, "side": "sell",
                    "entry_price": put.bid, "entry_price_usd": put_entry_usd,
                },
            ],
            entry_price_usd=entry_usd,
            fees_open=fee_call + fee_put,
            metadata={
                "offset":      self._offset,
                "expiry":      expiry,
                "expiry_dt":   exp_dt,
                "direction":   "sell",
                "call_strike": call.strike,
                "put_strike":  put.strike,
            },
        )
        self._positions.append(pos)
        self._last_trade_date = state.dt.date()

    def _close(self, state, pos, reason):
        # type: (Any, OpenPosition, str) -> Trade
        """Close a short straddle/strangle position and record the trade."""
        expiry = pos.metadata["expiry"]
        call_strike = pos.metadata["call_strike"]
        put_strike = pos.metadata["put_strike"]

        if reason == "expiry":
            # What we owe at settlement: intrinsic of each leg
            call_exit_usd = max(0.0, state.spot - call_strike)
            put_exit_usd = max(0.0, put_strike - state.spot)
        else:
            # Buy back at ask; fall back to mark if ask is absent.
            # Last resort: use the leg's own entry price (zero net move assumed)
            # rather than 0.0, which would produce a phantom full-premium profit.
            call_q = state.get_option(expiry, call_strike, True)
            put_q = state.get_option(expiry, put_strike, False)
            call_exit_usd = (call_q.ask_usd if call_q and call_q.ask > 0
                             else (call_q.mark_usd if call_q
                                   else pos.legs[0]["entry_price_usd"]))
            put_exit_usd = (put_q.ask_usd if put_q and put_q.ask > 0
                            else (put_q.mark_usd if put_q
                                  else pos.legs[1]["entry_price_usd"]))

        exit_usd = call_exit_usd + put_exit_usd
        fees_close = 0.0 if reason == "expiry" else (
            deribit_fee_per_leg(state.spot, call_exit_usd) +
            deribit_fee_per_leg(state.spot, put_exit_usd)
        )

        trade = close_trade(state, pos, reason, exit_usd, fees_close)
        trade.metadata["stop_loss_pct"] = self._sl_pct
        trade.metadata["max_hold_hours"] = self._max_hold_hours
        return trade
