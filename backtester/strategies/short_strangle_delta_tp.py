#!/usr/bin/env python3
"""
short_strangle_delta_tp.py — Short N-DTE strangle (delta-selected) with SL + TP + time/expiry exit.

Identical to short_strangle_delta.py but adds a take-profit exit:

    take_profit_pct — close when the cost to buy back both legs (at ask)
                      has fallen to  entry_premium × (1 - take_profit_pct).

    Example: take_profit_pct=0.60 → close when combined ask drops to 40 %
             of the premium collected at entry.

TP repricing uses raw ask prices (no mark/fair floor) — simple bid/ask only.
SL repricing is unchanged from the original strategy.

All other behaviour (delta selection, entry window, SL, max-hold, expiry
settlement) is identical to ShortStrangleDelta.
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
# Helpers (identical to short_strangle_delta.py)
# ------------------------------------------------------------------

@lru_cache(maxsize=64)
def _parse_expiry_date(expiry_code):
    # type: (str) -> Optional[datetime]
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
    exp_date = _parse_expiry_date(expiry_code)
    if exp_date is None:
        return None
    return exp_date.replace(hour=EXPIRY_HOUR_UTC, tzinfo=tzinfo)


def _select_expiry(state, dte):
    # type: (Any, int) -> Optional[str]
    target_date = state.dt.date() + timedelta(days=dte)
    for exp in state.expiries():
        exp_date = _parse_expiry_date(exp)
        if exp_date is not None and exp_date.date() == target_date:
            return exp
    return None


def _select_by_delta(chain, target_delta):
    # type: (list, float) -> Optional[Any]
    candidates = [q for q in chain if q.delta != 0.0]
    if not candidates:
        candidates = chain
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(q.delta - target_delta))


def _apply_min_otm(chain, selected, spot, min_pct, is_call):
    # type: (list, Any, float, float, bool) -> Optional[Any]
    """If `selected` is within min_pct% of spot, push to the nearest strike
    that satisfies the minimum OTM distance.  Returns None if none exists.

    Call leg: strike must be >= spot * (1 + min_pct/100)
    Put  leg: strike must be <= spot * (1 - min_pct/100)
    """
    factor = min_pct / 100.0
    if is_call:
        floor = spot * (1.0 + factor)
        if selected.strike >= floor:
            return selected  # already far enough out
        # find the nearest qualifying strike (lowest call strike >= floor)
        candidates = sorted(
            [q for q in chain if q.strike >= floor],
            key=lambda q: q.strike
        )
    else:
        floor = spot * (1.0 - factor)
        if selected.strike <= floor:
            return selected  # already far enough out
        # find the nearest qualifying strike (highest put strike <= floor)
        candidates = sorted(
            [q for q in chain if q.strike <= floor],
            key=lambda q: q.strike, reverse=True
        )
    return candidates[0] if candidates else None


# ------------------------------------------------------------------
# Strategy
# ------------------------------------------------------------------

class ShortStrangleDeltaTp:
    """Sell N-DTE OTM strangle (delta-selected); exit on TP, SL, time exit, or expiry."""

    name = "short_strangle_delta_tp"
    DATE_RANGE = ("2025-11-10", "2026-04-15")
    DESCRIPTION = (
        "Sells a strangle on a Deribit expiry N calendar days ahead (dte=1/2/3), "
        "with legs chosen by target delta (e.g. delta=0.25 → 25-delta call + put). "
        "Adds a take-profit: close when combined ask drops to (1-tp_pct) × entry premium. "
        "TP uses raw ask prices. SL uses the same repricing as the base strategy. "
        "One entry per day; up to dte+1 positions open concurrently. "
        "Entries allowed 01:00–23:00 UTC. "
        "Exits on take-profit, stop-loss, optional max hold duration, or expiry settlement."
    )

    PARAM_GRID = {
        # Discovery grid: broad, sparse — find candidate regions.
        # Sensitivity analysis and WFO use experiment files in backtester/experiments/.
        
        "dte":              [1],
        "delta":            [0.10, 0.125, 0.15],
        "entry_hour":       [14, 16, 18, 20, 22],
        "stop_loss_pct":    [0, 3.0, 4.0, 5.0, 6.0],
        "take_profit_pct":  [0, 0.5, 0.90],
        "max_hold_hours":   [0],
        "skip_weekends":    [1],
        "min_otm_pct":      [0],
    }

    def __init__(self):
        self._positions = []          # type: List[OpenPosition]
        self._dte = 1
        self._max_concurrent = 1
        self._delta = 0.25
        self._sl_pct = 1.0
        self._tp_pct = 0.50
        self._entry_hour = 10
        self._max_hold_hours = 0
        self._skip_weekends = 0
        self._min_otm_pct = 0
        self._last_trade_date = None  # type: Optional[Any]
        self._entry_conditions = []
        self._exit_conditions = []

    def configure(self, params):
        # type: (Dict[str, Any]) -> None
        self._dte = params.get("dte", 1)
        self._delta = params["delta"]
        self._sl_pct = params["stop_loss_pct"]
        self._tp_pct = params["take_profit_pct"]
        self._entry_hour = params.get("entry_hour", 10)
        self._max_hold_hours = params.get("max_hold_hours", 0)
        self._skip_weekends = params.get("skip_weekends", 0)
        self._min_otm_pct = params.get("min_otm_pct", 0)
        self._max_concurrent = self._dte + 1
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

        to_close = []
        for pos in list(self._positions):
            reason = self._check_expiry(state, pos)
            if reason is None:
                reason = self._check_take_profit(state, pos)
            if reason is None:
                for exit_cond in self._exit_conditions:
                    reason = exit_cond(state, pos)
                    if reason:
                        break
            if reason and reason != "expiry":
                expiry = pos.metadata["expiry"]
                if (state.get_option(expiry, pos.metadata["call_strike"], True) is None
                        or state.get_option(expiry, pos.metadata["put_strike"], False) is None):
                    reason = None  # data gap — retry next tick
            if reason:
                trades.append(self._close(state, pos, reason))
                to_close.append(pos)
        for pos in to_close:
            self._positions.remove(pos)

        if len(self._positions) < self._max_concurrent:
            today = state.dt.date()
            if self._last_trade_date != today:
                if self._skip_weekends and state.dt.weekday() >= 5:  # 5=Sat, 6=Sun
                    pass
                elif all(cond(state) for cond in self._entry_conditions):
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
            "dte":              self._dte,
            "delta":            self._delta,
            "stop_loss_pct":    self._sl_pct,
            "take_profit_pct":  self._tp_pct,
            "entry_hour":       self._entry_hour,
            "max_hold_hours":   self._max_hold_hours,
            "skip_weekends":    self._skip_weekends,
            "min_otm_pct":     self._min_otm_pct,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_expiry(self, state, pos):
        # type: (Any, OpenPosition) -> Optional[str]
        exp_dt = pos.metadata.get("expiry_dt")
        if exp_dt is None:
            return None
        if state.dt >= exp_dt:
            return "expiry"
        return None

    def _check_take_profit(self, state, pos):
        # type: (Any, OpenPosition) -> Optional[str]
        """Close when combined ask cost drops to (1 - tp_pct) × entry premium.

        Uses raw ask prices — no mark/fair floor.
        Returns None if ask data is missing for either leg (skip tick).
        """
        if self._tp_pct <= 0:
            return None
        expiry = pos.metadata["expiry"]
        call_q = state.get_option(expiry, pos.metadata["call_strike"], True)
        put_q  = state.get_option(expiry, pos.metadata["put_strike"], False)
        if call_q is None or put_q is None:
            return None
        # ask == 0 means the option is essentially worthless (no market maker quoting).
        # Treat as 0 rather than skipping — a zero ask is a genuine TP signal.
        call_ask_usd = call_q.ask_usd if call_q.ask > 0 else 0.0
        put_ask_usd  = put_q.ask_usd  if put_q.ask  > 0 else 0.0
        current_usd = call_ask_usd + put_ask_usd
        _ep = pos.entry_price_usd
        profit_ratio = (_ep - current_usd) / (_ep if _ep > 0.01 else 0.01)
        if profit_ratio >= self._tp_pct:
            return "take_profit"
        return None

    def _try_open(self, state):
        # type: (Any) -> None
        expiry = _select_expiry(state, self._dte)
        if expiry is None:
            return

        chain = state.get_chain(expiry)
        if not chain:
            return

        calls = [q for q in chain if q.is_call]
        puts  = [q for q in chain if not q.is_call]

        call = _select_by_delta(calls, +self._delta)
        put  = _select_by_delta(puts,  -self._delta)

        if call is None or put is None:
            return

        if self._min_otm_pct > 0:
            call = _apply_min_otm(calls, call, state.spot, self._min_otm_pct, is_call=True)
            put  = _apply_min_otm(puts,  put,  state.spot, self._min_otm_pct, is_call=False)
            if call is None or put is None:
                return  # no qualifying strike this tick — skip entry

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
                "target_delta":    self._delta,
                "expiry":          expiry,
                "expiry_dt":       exp_dt,
                "direction":       "sell",
                "call_strike":     call.strike,
                "put_strike":      put.strike,
                "call_delta":      call.delta,
                "put_delta":       put.delta,
            },
        )
        self._positions.append(pos)
        self._last_trade_date = state.dt.date()

    def _close(self, state, pos, reason):
        # type: (Any, OpenPosition, str) -> Trade
        expiry      = pos.metadata["expiry"]
        call_strike = pos.metadata["call_strike"]
        put_strike  = pos.metadata["put_strike"]

        if reason == "expiry":
            call_exit_usd = max(0.0, state.spot - call_strike)
            put_exit_usd  = max(0.0, put_strike  - state.spot)
        else:
            # Buy back at ask; fall back to Deribit min-tick (0.0001 BTC) on
            # missing/zero ask — options quoted at 0 are essentially worthless
            # but are never free to close on Deribit.
            _min_tick_usd = 0.0001 * state.spot
            call_q = state.get_option(expiry, call_strike, True)
            put_q  = state.get_option(expiry, put_strike,  False)
            call_exit_usd = (call_q.ask_usd if call_q and call_q.ask > 0
                             else _min_tick_usd)
            put_exit_usd  = (put_q.ask_usd if put_q and put_q.ask > 0
                             else _min_tick_usd)

        exit_usd   = call_exit_usd + put_exit_usd
        fees_close = 0.0 if reason == "expiry" else (
            deribit_fee_per_leg(state.spot, call_exit_usd) +
            deribit_fee_per_leg(state.spot, put_exit_usd)
        )

        trade = close_trade(state, pos, reason, exit_usd, fees_close)
        trade.metadata["dte"]              = self._dte
        trade.metadata["stop_loss_pct"]    = self._sl_pct
        trade.metadata["take_profit_pct"]  = self._tp_pct
        trade.metadata["max_hold_hours"]   = self._max_hold_hours
        return trade
