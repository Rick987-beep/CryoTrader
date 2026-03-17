"""
Smoke Test Strangle — Phase 1 Live Verification

Opens a tiny long strangle, holds ~60 seconds, then closes.
Proves the exchange abstraction layer works end-to-end on production.

Expected behavior:
  1. Opens 0.1 BTC long strangle (±0.30 delta, next expiry)
  2. Aggressive limit fills both legs
  3. Holds for ~60 seconds (max_hold_hours exit)
  4. Closes via aggressive limit
  5. Logs result + Telegram notification

Account: subaccount (~$7,300) — swap .env PROD keys before running.
"""

import logging

from option_selection import strangle
from strategy import (
    StrategyConfig,
    time_window,
    max_hold_hours,
)
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Parameters ─────────────────────────────────────────────────────────────

QTY = 0.1
CALL_DELTA = 0.30
PUT_DELTA = -0.30
DTE = "next"
SIDE = "buy"
HOLD_MINUTES = 1


# ─── Callbacks ──────────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    logger.info(f"[SMOKE TEST] Trade opened: {trade.id}")
    for leg in trade.open_legs:
        logger.info(f"  Leg: {leg.symbol}  side={leg.side}  qty={leg.qty}")
    try:
        get_notifier().notify_trade_opened(
            strategy_name="Smoke Test Strangle",
            trade_id=trade.id,
            legs=trade.open_legs,
            entry_cost=trade.total_entry_cost(),
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    logger.info(
        f"[SMOKE TEST] Trade closed: {trade.id}  |  "
        f"PnL: ${pnl:+.2f}  |  ROI: {roi:+.1f}%  |  "
        f"Hold: {hold_seconds:.0f}s  |  Entry: ${entry_cost:.2f}"
    )
    try:
        get_notifier().notify_trade_closed(
            strategy_name="Smoke Test Strangle",
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

def smoke_test_strangle() -> StrategyConfig:
    """Open and close a 0DTE delta-30 strangle. Minimum viable product."""
    return StrategyConfig(
        name="smoke_test_strangle",

        legs=strangle(
            qty=QTY,
            call_delta=CALL_DELTA,
            put_delta=PUT_DELTA,
            dte=DTE,
            side=SIDE,
        ),

        entry_conditions=[
            time_window(0, 23),
        ],

        exit_conditions=[
            max_hold_hours(HOLD_MINUTES / 60),
        ],

        execution_mode="limit",

        max_concurrent_trades=1,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=5,

        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
