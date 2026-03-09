"""
Test Strangle 11MAR — Live Integration Test for Order Management

Opens a long 0.15δ strangle (11MAR26 expiry) to validate the v1.0.0
OrderManager + LifecycleEngine + ExecutionRouter in production with real
orders. Uses phased execution (mid → aggressive) so orders sit on the
book for ~2 minutes before crossing the spread — exercising requote
chains, idempotency guards, and the PENDING_CLOSE gate.

Another strategy may be running on the same account.  This test verifies
that OrderManager correctly tracks only *its own* orders and positions.

Usage:
    # In main.py STRATEGIES list:
    from strategies import test_strangle_11mar
    STRATEGIES = [test_strangle_11mar]
"""

import logging
import time

from option_selection import LegSpec
from strategy import (
    StrategyConfig,
    ExecutionParams,
    ExecutionPhase,
    max_hold_hours,
)
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)

# ─── Parameters ─────────────────────────────────────────────────────────────

QTY = 0.01              # BTC per leg — tiny test size
EXPIRY = "11MAR26"       # specific expiry
CALL_DELTA = 0.15        # OTM call target delta
PUT_DELTA = -0.15        # OTM put target delta
HOLD_MINUTES = 2         # hold for 2 minutes, then close

# Phased execution: ~2 min on book at mid, then aggressive cross
OPEN_CLOSE_PHASES = ExecutionParams(phases=[
    ExecutionPhase(pricing="mid",        duration_seconds=120, reprice_interval=15),
    ExecutionPhase(pricing="aggressive", duration_seconds=60,  buffer_pct=3.0, reprice_interval=10),
])


# ─── Legs ───────────────────────────────────────────────────────────────────

def _build_legs():
    """Long strangle: buy OTM call + buy OTM put, 11MAR26 expiry."""
    expiry = {"symbol": EXPIRY}
    return [
        LegSpec(
            option_type="C",
            side=1,         # BUY
            qty=QTY,
            strike_criteria={"type": "delta", "value": CALL_DELTA},
            expiry_criteria=expiry,
            underlying="BTC",
        ),
        LegSpec(
            option_type="P",
            side=1,         # BUY
            qty=QTY,
            strike_criteria={"type": "delta", "value": PUT_DELTA},
            expiry_criteria=expiry,
            underlying="BTC",
        ),
    ]


# ─── Callbacks ──────────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """Verbose logging + Telegram on open."""
    entry_cost = trade.total_entry_cost()
    legs_str = ", ".join(
        f"{l.symbol} qty={l.filled_qty} @ ${l.fill_price}"
        for l in trade.open_legs
    )
    logger.info(
        f"[TEST STRANGLE 11MAR] ===== TRADE OPENED =====\n"
        f"  Trade ID : {trade.id}\n"
        f"  State    : {trade.state.value}\n"
        f"  Legs     : {legs_str}\n"
        f"  Entry $  : ${entry_cost:.4f}\n"
        f"  Mode     : {trade.execution_mode}\n"
        f"  Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    )
    try:
        get_notifier().notify_trade_opened(
            strategy_name="Test Strangle 11MAR",
            trade_id=trade.id,
            legs=trade.open_legs,
            entry_cost=entry_cost,
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """Verbose logging + Telegram on close."""
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    open_legs_str = ", ".join(
        f"{l.symbol} qty={l.filled_qty} @ ${l.fill_price}"
        for l in trade.open_legs
    )
    close_legs_str = ", ".join(
        f"{l.symbol} qty={l.filled_qty} @ ${l.fill_price}"
        for l in (trade.close_legs or [])
    )

    logger.info(
        f"[TEST STRANGLE 11MAR] ===== TRADE CLOSED =====\n"
        f"  Trade ID  : {trade.id}\n"
        f"  State     : {trade.state.value}\n"
        f"  PnL       : ${pnl:+.4f}\n"
        f"  ROI       : {roi:+.1f}%\n"
        f"  Hold      : {hold_seconds / 60:.1f} min\n"
        f"  Entry $   : ${entry_cost:.4f}\n"
        f"  Open legs : {open_legs_str}\n"
        f"  Close legs: {close_legs_str}\n"
        f"  Timestamp : {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}"
    )
    try:
        get_notifier().notify_trade_closed(
            strategy_name="Test Strangle 11MAR",
            trade_id=trade.id,
            pnl=pnl,
            roi=roi,
            hold_minutes=hold_seconds / 60,
            entry_cost=entry_cost,
            close_legs=trade.close_legs,
        )
    except Exception:
        pass


# ─── Strategy Factory ──────────────────────────────────────────────────────

def test_strangle_11mar() -> StrategyConfig:
    """
    Live integration test: long 0.15δ strangle, 11MAR26 expiry.

    Opens immediately, holds 2 min, closes.  Phased execution keeps
    orders on the book long enough to exercise OrderManager requoting.
    """
    return StrategyConfig(
        name="test_strangle_11mar",

        legs=_build_legs(),

        # No entry conditions — open on first tick
        entry_conditions=[],

        # Close after 2 minutes
        exit_conditions=[
            max_hold_hours(HOLD_MINUTES / 60),
        ],

        execution_mode="limit",
        execution_params=OPEN_CLOSE_PHASES,

        max_concurrent_trades=1,
        max_trades_per_day=1,
        cooldown_seconds=120,
        check_interval_seconds=10,

        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
