"""
Blueprint Strangle — Starting Template for New Strategies

Copy this file and adapt it to build your own strategy.  It demonstrates
the recommended way to use the CoincallTrader framework: declarative
configuration via StrategyConfig, framework-provided entry/exit conditions,
and the on_trade_closed callback for logging results.

What this strategy does:
  - Buys a small BTC strangle (OTM call + OTM put) once per day
  - Enters during a configurable UTC time window
  - Exits on profit target, max loss, or time limit
  - Logs trade results when each trade closes

Framework features demonstrated:
  ✓ strangle() structure template for leg definition
  ✓ Entry conditions: time_window, weekday_filter, min_available_margin_pct
  ✓ Exit conditions: profit_target, max_loss, max_hold_hours
  ✓ on_trade_closed callback for trade result logging
  ✓ Execution via limit orders (orderbook)

Not covered here (see other strategies or MODULE_REFERENCE.md):
  - RFQ execution (rfq_endurance.py)
  - Smart orderbook execution (MODULE_REFERENCE.md § Smart Orderbook)
  - Custom multi-day state tracking (long_strangle_pnl_test.py in archive/)
  - Multi-leg structures like iron condors (reverse_iron_condor_live.py)

Usage:
    # In main.py STRATEGIES list:
    from strategies import blueprint_strangle
    STRATEGIES = [blueprint_strangle]
"""

import logging

from option_selection import strangle
from strategy import (
    StrategyConfig,
    # Entry conditions
    time_window,
    weekday_filter,
    min_available_margin_pct,
    # Exit conditions
    profit_target,
    max_loss,
    max_hold_hours,
)

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
#
# Adjust these to change the strategy's behaviour.  All parameters are
# gathered here so you don't need to hunt through the code.
#

# Structure
QTY = 0.01                          # BTC per leg (keep small for testing)
CALL_DELTA = 0.15                   # target call delta (further OTM = smaller)
PUT_DELTA = -0.15                   # target put delta (further OTM = smaller)
DTE = "next"                        # expiry: "next" available, or int (0=0DTE, 1=1DTE, …)
SIDE = 1                            # 1 = BUY (long strangle), 2 = SELL (short strangle)

# Entry conditions
ENTRY_START_HOUR = 8                # UTC hour — earliest entry
ENTRY_END_HOUR = 20                 # UTC hour — latest entry (exclusive)
TRADING_DAYS = ["mon", "tue", "wed", "thu", "fri"]  # no weekends
MIN_MARGIN_PCT = 30                 # require ≥30% available margin

# Exit conditions
PROFIT_TARGET_PCT = 50              # close at +50% of entry cost
MAX_LOSS_PCT = 100                  # close at −100% of entry cost (full loss)
MAX_HOLD_HOURS = 24                 # hard close after 24 hours

# Operational limits
MAX_CONCURRENT = 1                  # only one open trade at a time
MAX_PER_DAY = 1                     # one trade per calendar day (UTC)
COOLDOWN_SECONDS = 300              # 5 min between trade attempts
CHECK_INTERVAL = 30                 # seconds between entry/exit evaluations


# ─── Trade Result Callback ──────────────────────────────────────────────────

def _on_trade_closed(trade, account) -> None:
    """
    Called by the framework when a trade transitions to CLOSED or FAILED.

    Use this to log results, update external systems, or trigger follow-up
    actions.  The callback receives the TradeLifecycle object (with PnL,
    hold time, leg details) and the current AccountSnapshot.
    """
    pnl = trade.structure_pnl(account)
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    logger.info(
        f"Trade closed: {trade.id}  |  "
        f"PnL: ${pnl:+.2f}  |  ROI: {roi:+.1f}%  |  "
        f"Hold: {hold_seconds / 60:.1f} min  |  "
        f"Entry cost: ${entry_cost:.2f}"
    )


# ─── Strategy Factory ──────────────────────────────────────────────────────

def blueprint_strangle() -> StrategyConfig:
    """
    Blueprint strangle strategy — long OTM strangle with standard exits.

    Returns a StrategyConfig for registration in main.py's STRATEGIES list.
    The framework handles everything else: leg resolution, order execution,
    fill tracking, exit evaluation, and position monitoring.
    """
    return StrategyConfig(
        name="blueprint_strangle",

        # ── What to trade ────────────────────────────────────────────────
        # strangle() returns [LegSpec(call), LegSpec(put)] — the framework
        # resolves these to real option symbols at entry time.
        legs=strangle(
            qty=QTY,
            call_delta=CALL_DELTA,
            put_delta=PUT_DELTA,
            dte=DTE,
            side=SIDE,
        ),

        # ── When to enter ────────────────────────────────────────────────
        # ALL conditions must be True to open a trade.
        # The framework also enforces max_concurrent_trades, max_trades_per_day,
        # and cooldown_seconds automatically — no need to code those here.
        entry_conditions=[
            time_window(ENTRY_START_HOUR, ENTRY_END_HOUR),
            weekday_filter(TRADING_DAYS),
            min_available_margin_pct(MIN_MARGIN_PCT),
        ],

        # ── When to exit ─────────────────────────────────────────────────
        # ANY condition returning True triggers a close.
        exit_conditions=[
            profit_target(PROFIT_TARGET_PCT),
            max_loss(MAX_LOSS_PCT),
            max_hold_hours(MAX_HOLD_HOURS),
        ],

        # ── How to execute ───────────────────────────────────────────────
        # "limit" = orderbook limit orders (good for small sizes)
        # "rfq"   = block trades via RFQ (requires $50k+ notional)
        # "smart" = chunked orderbook execution
        # "auto"  = framework picks based on notional size
        execution_mode="limit",

        # ── Operational limits ───────────────────────────────────────────
        max_concurrent_trades=MAX_CONCURRENT,
        max_trades_per_day=MAX_PER_DAY,
        cooldown_seconds=COOLDOWN_SECONDS,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────────
        on_trade_closed=_on_trade_closed,
    )
