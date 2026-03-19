"""
Production Test — Single Long Put (Deribit)

Minimal test for Deribit production: buys 0.1 BTC of BTC-21MAR26-66000-P,
holds for 30 seconds, then closes. Aggressive limit execution (cross the
spread immediately).

Requires:
  EXCHANGE=deribit  TRADING_ENVIRONMENT=production
  .env must have DERIBIT_CLIENT_ID_PROD / DERIBIT_CLIENT_SECRET_PROD
"""

import logging

from option_selection import LegSpec
from strategy import (
    StrategyConfig,
    time_window,
    max_hold_hours,
)
from trade_execution import ExecutionParams, ExecutionPhase
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


# ─── Parameters ─────────────────────────────────────────────────────────────

QTY = 0.1
STRIKE = 66000
EXPIRY = "21MAR26"
HOLD_SECONDS = 30

EXECUTION_PHASES = ExecutionParams(
    phases=[
        ExecutionPhase(pricing="aggressive", duration_seconds=30, reprice_interval=10, buffer_pct=10.0),
    ]
)


# ─── Callbacks ──────────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    logger.info(f"[PROD TEST] Trade opened: {trade.id}")
    for leg in trade.open_legs:
        logger.debug(f"  Leg: {leg.symbol}  side={leg.side}  qty={leg.qty}  fill={leg.fill_price}")
    try:
        get_notifier().notify_trade_opened(
            strategy_name="Prod Test Put (Deribit)",
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
        f"[PROD TEST] Trade closed: {trade.id}  |  "
        f"PnL: ${pnl:+.2f}  |  ROI: {roi:+.1f}%  |  "
        f"Hold: {hold_seconds:.0f}s  |  Entry: ${entry_cost:.2f}"
    )
    try:
        get_notifier().notify_trade_closed(
            strategy_name="Prod Test Put (Deribit)",
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

def prod_test_put() -> StrategyConfig:
    """Deribit production: buy 0.1 BTC-21MAR26-66000-P, hold 30s, close."""
    return StrategyConfig(
        name="prod_test_put",

        legs=[
            LegSpec(
                option_type="P",
                side="buy",
                qty=QTY,
                strike_criteria={"type": "strike", "value": STRIKE},
                expiry_criteria={"symbol": EXPIRY},
            ),
        ],

        entry_conditions=[
            time_window(0, 23),
        ],

        exit_conditions=[
            max_hold_hours(HOLD_SECONDS / 3600),
        ],

        execution_mode="limit",
        execution_params=EXECUTION_PHASES,

        max_concurrent_trades=1,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=5,

        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
