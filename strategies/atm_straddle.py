"""
ATM Straddle — Daily Long ATM Straddle with Profit Target + Time Exit

Opens a long ATM straddle (buy ATM call + buy ATM put) at a scheduled
UTC time every day, then closes it when either:
  1. Profit reaches the configurable take-profit %, OR
  2. The hard close time is reached (same day).

Repeats daily until the user stops the strategy.

Framework features used:
  ✓ straddle() structure template (ATM call + ATM put via closestStrike)
  ✓ Entry conditions: time_window, min_available_margin_pct
  ✓ Exit conditions: profit_target, time_exit
  ✓ on_trade_closed callback for result logging
  ✓ max_trades_per_day for daily repeat

Usage:
    # In main.py STRATEGIES list:
    from strategies import atm_straddle
    STRATEGIES = [atm_straddle]
"""

import logging

from option_selection import straddle
from strategy import (
    StrategyConfig,
    # Entry conditions
    time_window,
    min_available_margin_pct,
    # Exit conditions
    profit_target,
    time_exit,
)

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
#
# Adjust these to change the strategy's behaviour.  All tunables are
# gathered here at the top for easy configuration.
#

# Structure
QTY = 0.01                          # BTC per leg (0.01 = small test size)
DTE = "next"                        # expiry: "next" available (nearest, typically next-day 08:00 UTC)

# Scheduling — when to open and close
OPEN_HOUR = 12                      # UTC hour to open the straddle
CLOSE_HOUR = 19                     # UTC hour to force-close (hard exit)
CLOSE_MINUTE = 0                    # UTC minute for the hard close

# Profit target
TAKE_PROFIT_PCT = 30                # close at +30% of entry cost

# Risk / margin
MIN_MARGIN_PCT = 20                 # require ≥20% available margin before entry

# Operational limits
CHECK_INTERVAL = 30                 # seconds between entry/exit evaluations


# ─── Trade Result Callback ──────────────────────────────────────────────────

def _on_trade_closed(trade, account) -> None:
    """
    Called by the framework when a trade transitions to CLOSED or FAILED.

    Logs PnL, ROI, and hold time for the daily straddle.
    """
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    logger.info(
        f"[ATM Straddle] Trade closed: {trade.id}  |  "
        f"PnL: ${pnl:+.2f}  |  ROI: {roi:+.1f}%  |  "
        f"Hold: {hold_seconds / 60:.1f} min  |  "
        f"Entry cost: ${entry_cost:.2f}"
    )


# ─── Strategy Factory ──────────────────────────────────────────────────────

def atm_straddle() -> StrategyConfig:
    """
    Daily ATM straddle — buy ATM call + ATM put, close on profit or time.

    Returns a StrategyConfig for registration in main.py's STRATEGIES list.
    """
    return StrategyConfig(
        name="atm_straddle_daily",

        # ── What to trade ────────────────────────────────────────────────
        # straddle() returns [LegSpec(ATM call), LegSpec(ATM put)].
        # Both legs use closestStrike=0 so they resolve to the strike
        # nearest to spot — a true ATM straddle.
        legs=straddle(
            qty=QTY,
            dte=DTE,
            side=1,          # 1 = BUY (long straddle)
        ),

        # ── When to enter ────────────────────────────────────────────────
        # time_window gates entry to a 1-hour window starting at OPEN_HOUR.
        # With check_interval=30 and max_trades_per_day=1, the trade opens
        # on the first tick after OPEN_HOUR:00 UTC.
        entry_conditions=[
            time_window(OPEN_HOUR, OPEN_HOUR + 1),
            min_available_margin_pct(MIN_MARGIN_PCT),
        ],

        # ── When to exit ─────────────────────────────────────────────────
        # ANY condition returning True triggers a close:
        #   1. Profit target hit → close early (win)
        #   2. Hard time exit → close at CLOSE_HOUR:CLOSE_MINUTE UTC
        exit_conditions=[
            profit_target(TAKE_PROFIT_PCT, pnl_mode="executable"),
            time_exit(CLOSE_HOUR, CLOSE_MINUTE),
        ],

        # ── How to execute ───────────────────────────────────────────────
        execution_mode="limit",

        # ── Operational limits ───────────────────────────────────────────
        max_concurrent_trades=1,       # one straddle at a time
        max_trades_per_day=1,          # one per day, repeat next day
        cooldown_seconds=0,            # no cooldown needed (1/day enforced above)
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────────
        on_trade_closed=_on_trade_closed,
    )
