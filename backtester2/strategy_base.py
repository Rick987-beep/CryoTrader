#!/usr/bin/env python3
"""
strategy_base.py — Strategy protocol, data types, and composable condition helpers.

Defines the contract between strategies and the backtest engine:
    - Strategy protocol (configure → on_market_state → on_end → reset)
    - Trade / OpenPosition dataclasses
    - Reusable entry conditions (time_window, weekday_only, etc.)
    - Reusable exit conditions (index_move_trigger, max_hold_hours, etc.)

Strategies compose conditions from these helpers — same pattern as
production's StrategyConfig but stripped of execution concerns.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------

@dataclass
class Trade:
    """A completed (closed) trade with full P&L accounting."""
    entry_time: datetime
    exit_time: datetime
    entry_spot: float           # BTC spot at entry
    exit_spot: float            # BTC spot at exit
    entry_price_usd: float      # Total premium paid/received (all legs, USD)
    exit_price_usd: float       # Total premium at close (all legs, USD)
    fees: float                 # Round-trip Deribit fees (USD)
    pnl: float                  # Net P&L after fees (USD)
    triggered: bool             # Whether primary exit trigger fired
    exit_reason: str            # "trigger", "time_exit", "max_hold", "expiry", etc.
    exit_hour: int              # Hours held (int, for V1 metrics compat)
    entry_date: str             # "YYYY-MM-DD"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenPosition:
    """Internal state held by a strategy while a trade is open."""
    entry_time: datetime
    entry_spot: float
    legs: List[Dict[str, Any]]  # [{strike, is_call, expiry, side, qty, entry_price}]
    entry_price_usd: float      # Total premium paid/received (sum of legs)
    fees_open: float            # Entry fees (USD)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------------------------
# Strategy protocol (structural typing — no base class needed)
# ------------------------------------------------------------------
# Any class with these attributes/methods satisfies the protocol.
# We use a runtime-checkable Protocol for type checking, but
# strategies don't need to inherit from anything.

try:
    from typing import Protocol, runtime_checkable
except ImportError:
    # Python 3.7 fallback
    from typing_extensions import Protocol, runtime_checkable


@runtime_checkable
class Strategy(Protocol):
    """Protocol for backtest strategies.

    Lifecycle:
        1. configure(params)         — set parameters for this run
        2. on_market_state(state)    — called each 5-min tick
        3. on_end(state)             — force-close at end of data
        4. reset()                   — clear state for next run
    """

    name: str  # type: str

    def configure(self, params):
        # type: (Dict[str, Any]) -> None
        """Set parameters for this backtest run."""
        ...

    def on_market_state(self, state):
        # type: (Any) -> List[Trade]
        """Process one time step. Return completed trades (if any)."""
        ...

    def on_end(self, state):
        # type: (Any) -> List[Trade]
        """Force-close any open positions at end of data."""
        ...

    def reset(self):
        # type: () -> None
        """Clear internal state between grid runs."""
        ...

    def describe_params(self):
        # type: () -> Dict[str, Any]
        """Return current parameters for result labeling."""
        ...


# ------------------------------------------------------------------
# Type aliases for condition callables
# ------------------------------------------------------------------

# Entry condition: (MarketState) → bool
EntryCondition = Callable  # Callable[[MarketState], bool]

# Exit condition: (MarketState, OpenPosition) → Optional[str]
# Returns None to hold, or a reason string to exit.
ExitCondition = Callable  # Callable[[MarketState, OpenPosition], Optional[str]]


# ------------------------------------------------------------------
# Entry conditions (composable)
# ------------------------------------------------------------------

def time_window(start_hour, end_hour):
    # type: (int, int) -> EntryCondition
    """Allow entry only during UTC hour range [start_hour, end_hour).

    Handles wrap-around (e.g. start=22, end=4 → 22:00–03:59).
    """
    def check(state):
        h = state.dt.hour
        if start_hour <= end_hour:
            return start_hour <= h < end_hour
        else:
            # Wrap-around: e.g. 22–04 means 22,23,0,1,2,3
            return h >= start_hour or h < end_hour
    return check


def weekday_only():
    # type: () -> EntryCondition
    """Block entries on Saturday (5) and Sunday (6)."""
    def check(state):
        return state.dt.weekday() < 5
    return check


def at_interval(minute_offset=0):
    # type: (int) -> EntryCondition
    """Only allow entry at specific minute-of-hour (default: top of hour).

    Use minute_offset=0 for hourly entries, 30 for half-hour, etc.
    """
    def check(state):
        return state.dt.minute == minute_offset
    return check


# ------------------------------------------------------------------
# Exit conditions (composable)
# ------------------------------------------------------------------

def index_move_trigger(distance_usd):
    # type: (float) -> ExitCondition
    """Exit when BTC spot moves >= distance_usd from entry spot.

    Uses 1-min spot bars for intra-bar excursion detection, so a
    spike within a 5-min interval isn't missed.
    """
    def check(state, pos):
        # Check current spot
        excursion = abs(state.spot - pos.entry_spot)
        if excursion >= distance_usd:
            return "trigger"
        # Check 1-min bars for intra-bar spikes
        for bar in state.spot_bars:
            up = abs(bar.high - pos.entry_spot)
            down = abs(pos.entry_spot - bar.low)
            if up >= distance_usd or down >= distance_usd:
                return "trigger"
        return None
    return check


def max_hold_hours(hours):
    # type: (int) -> ExitCondition
    """Force-close after N hours held."""
    def check(state, pos):
        held_s = (state.dt - pos.entry_time).total_seconds()
        if held_s >= hours * 3600:
            return "max_hold"
        return None
    return check


def time_exit(hour, minute=0):
    # type: (int, int) -> ExitCondition
    """Hard close at specific UTC wall-clock time (same day as entry)."""
    def check(state, pos):
        if pos.entry_time.date() != state.dt.date():
            return None  # Only fires on entry day
        target_mins = hour * 60 + minute
        current_mins = state.dt.hour * 60 + state.dt.minute
        if current_mins >= target_mins:
            return "time_exit"
        return None
    return check


def stop_loss_pct(pct):
    # type: (float) -> ExitCondition
    """Close when unrealized loss exceeds pct (as fraction, e.g. 0.5 = 50%).

    Handles both long and short premium via 'direction' in metadata.
    """
    def check(state, pos):
        current_usd = _reprice_legs(state, pos)
        if current_usd is None:
            return None
        if pos.metadata.get("direction") == "sell":
            # Short premium: loss = current cost to buy back exceeds received
            loss_ratio = (current_usd - pos.entry_price_usd) / max(pos.entry_price_usd, 0.01)
            if loss_ratio >= pct:
                return "stop_loss"
        else:
            # Long premium: loss = value dropped below entry cost
            loss_ratio = (pos.entry_price_usd - current_usd) / max(pos.entry_price_usd, 0.01)
            if loss_ratio >= pct:
                return "stop_loss"
        return None
    return check


def profit_target_pct(pct):
    # type: (float) -> ExitCondition
    """Close when unrealized profit reaches pct (as fraction)."""
    def check(state, pos):
        current_usd = _reprice_legs(state, pos)
        if current_usd is None:
            return None
        if pos.metadata.get("direction") == "sell":
            # Short premium: profit = premium received > current cost to buy back
            profit_ratio = (pos.entry_price_usd - current_usd) / max(pos.entry_price_usd, 0.01)
        else:
            # Long premium: profit = current value > entry cost
            profit_ratio = (current_usd - pos.entry_price_usd) / max(pos.entry_price_usd, 0.01)
        if profit_ratio >= pct:
            return "profit_target"
        return None
    return check


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _reprice_legs(state, pos):
    # type: (Any, OpenPosition) -> Optional[float]
    """Reprice all legs at current market. Returns total USD value.

    For long positions: uses bid (what you'd get selling).
    For short positions: uses ask (what it'd cost to buy back), floored at
    mark to prevent wide-spread false SL triggers in thin early-morning books.
    If ask is missing (=0) for a short leg, returns None (SL skips that tick).
    """
    total = 0.0
    direction = pos.metadata.get("direction", "buy")
    for leg in pos.legs:
        quote = state.get_option(
            leg["expiry"], leg["strike"], leg["is_call"]
        )
        if quote is None:
            return None
        if direction == "sell":
            if quote.ask == 0.0:
                return None  # Missing ask — skip this tick rather than misprice
            # Floor: use max(ask, mark) so a stale/thin ask can't price below mark.
            # This is still conservative — we never price better than ask.
            effective_ask = max(quote.ask, quote.mark)
            total += effective_ask * quote.spot
        else:
            total += quote.bid_usd  # Proceed from closing long
    return total


def close_trade(state, pos, reason, current_usd=None, fees_close=0.0):
    # type: (Any, OpenPosition, str, Optional[float], float) -> Trade
    """Helper to build a Trade from an OpenPosition being closed.

    Handles both long and short PnL formulas.
    """
    if current_usd is None:
        current_usd = _reprice_legs(state, pos) or 0.0

    total_fees = pos.fees_open + fees_close
    direction = pos.metadata.get("direction", "buy")
    if direction == "sell":
        pnl = pos.entry_price_usd - current_usd - total_fees
    else:
        pnl = current_usd - pos.entry_price_usd - total_fees

    held_s = (state.dt - pos.entry_time).total_seconds()
    return Trade(
        entry_time=pos.entry_time,
        exit_time=state.dt,
        entry_spot=pos.entry_spot,
        exit_spot=state.spot,
        entry_price_usd=pos.entry_price_usd,
        exit_price_usd=current_usd,
        fees=total_fees,
        pnl=pnl,
        triggered=(reason == "trigger"),
        exit_reason=reason,
        exit_hour=int(held_s / 3600),
        entry_date=pos.entry_time.strftime("%Y-%m-%d"),
        metadata={**pos.metadata},
    )
