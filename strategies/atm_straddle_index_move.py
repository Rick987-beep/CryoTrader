"""
ATM Straddle (Index Move) — Daily Long ATM Straddle with Index Distance Exit

Opens a long ATM straddle (buy ATM call + buy ATM put) at a scheduled
UTC time every day, then closes it when either:
  1. The BTCUSD index has moved ≥ MOVE_DISTANCE_USD from the entry
     index price (symmetric — up or down), OR
  2. The hard close time is reached (same day).

The entry BTCUSD index price is captured in on_trade_opened and stored
in trade.metadata["entry_index_price"].

Repeats daily until the user stops the strategy.

Framework features used:
  ✓ straddle() structure template (ATM call + ATM put via closestStrike)
  ✓ Entry conditions: time_window, min_available_margin_pct
  ✓ Exit conditions: index_move_distance (custom), time_exit
  ✓ on_trade_opened / on_trade_closed callbacks
  ✓ max_trades_per_day for daily repeat

Usage:
    # In main.py STRATEGIES list:
    from strategies import atm_straddle_index_move
    STRATEGIES = [atm_straddle_index_move]
"""

import logging
from datetime import datetime, timezone

from market_data import get_btc_index_price
from option_selection import straddle
from strategy import (
    StrategyConfig,
    # Entry conditions
    time_window,
    min_available_margin_pct,
    # Exit conditions
    time_exit,
)
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────

# Structure
QTY = 0.01                          # BTC per leg (0.01 = small test size)
DTE = "next"                        # expiry: "next" available (nearest)

# Scheduling — when to open and close
OPEN_HOUR = 12                      # UTC hour to open the straddle
CLOSE_HOUR = 19                     # UTC hour to force-close (hard exit)
CLOSE_MINUTE = 0                    # UTC minute for the hard close

# Index move exit
MOVE_DISTANCE_USD = 1200            # close when BTC index moves $1200 from entry

# Risk / margin
MIN_MARGIN_PCT = 20                 # require ≥20% available margin before entry

# Operational limits
CHECK_INTERVAL = 30                 # seconds between entry/exit evaluations


# ─── Exit Condition: Index Move Distance ────────────────────────────────────

def index_move_distance(distance_usd):
    """
    Exit condition factory: close when the BTCUSD index has moved
    ≥ distance_usd from the entry index price (symmetric up/down).

    The entry price must be stored in trade.metadata["entry_index_price"]
    by the on_trade_opened callback.
    """
    def _check(account, trade):
        entry_price = trade.metadata.get("entry_index_price")
        if entry_price is None:
            return False

        current_price = get_btc_index_price(use_cache=True)
        if current_price is None:
            return False

        move = abs(current_price - entry_price)
        if move >= distance_usd:
            logger.info(
                f"[Index Move] BTC index moved ${move:.0f} "
                f"(entry: ${entry_price:.0f}, now: ${current_price:.0f}, "
                f"threshold: ${distance_usd})"
            )
            return True
        return False

    _check.__name__ = f"index_move_{distance_usd}usd"
    return _check


# ─── Trade Callbacks ────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """Capture the BTCUSD index price at trade open and notify."""
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price
        logger.info(
            f"[ATM Straddle Index] Opened — entry index: ${index_price:.0f}, "
            f"exit threshold: ±${MOVE_DISTANCE_USD}"
        )
    else:
        logger.warning("[ATM Straddle Index] Could not capture entry index price!")

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry_cost = trade.total_entry_cost()
    legs_text = "\n".join(
        f"  {'BUY' if leg.side == 1 else 'SELL'} {leg.qty}× {leg.symbol}"
        for leg in trade.open_legs
    )
    idx_text = f"BTC index: ${index_price:,.0f}" if index_price else "BTC index: N/A"
    try:
        get_notifier().send(
            f"📈 <b>ATM Straddle (Index Move) — Trade Opened</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"{legs_text}\n"
            f"Entry cost: ${entry_cost:.2f}\n"
            f"{idx_text}  |  Exit: ±${MOVE_DISTANCE_USD}\n"
            f"Equity: ${account.equity:,.2f}\n"
            f"Avail margin: ${account.available_margin:,.2f} "
            f"({100 - account.margin_utilization:.1f}% free)"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """Log PnL and index move at close, send notification."""
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    entry_index = trade.metadata.get("entry_index_price")
    close_index = get_btc_index_price(use_cache=False)
    index_move = abs(close_index - entry_index) if (entry_index and close_index) else None

    msg = (
        f"[ATM Straddle Index] Trade closed: {trade.id}  |  "
        f"PnL: ${pnl:+.2f}  |  ROI: {roi:+.1f}%  |  "
        f"Hold: {hold_seconds / 60:.1f} min  |  "
        f"Entry cost: ${entry_cost:.2f}"
    )
    if index_move is not None:
        msg += f"  |  Index move: ${index_move:.0f}"
    logger.info(msg)

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "✅" if pnl >= 0 else "❌"
    legs_text = ""
    if trade.close_legs:
        legs_text = "\n".join(
            f"  {'SELL' if leg.side == 2 else 'BUY'} {leg.filled_qty}× {leg.symbol} @ ${leg.fill_price}"
            for leg in trade.close_legs
        ) + "\n"
    idx_text = f"Index move: ${index_move:,.0f}" if index_move is not None else ""
    entry_idx_text = f"Entry index: ${entry_index:,.0f}" if entry_index else ""
    close_idx_text = f"Close index: ${close_index:,.0f}" if close_index else ""
    try:
        get_notifier().send(
            f"{emoji} <b>ATM Straddle (Index Move) — Trade Closed</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"PnL: <b>${pnl:+.2f}</b> ({roi:+.1f}%)\n"
            f"Hold: {hold_seconds / 60:.1f} min\n"
            f"Entry cost: ${entry_cost:.2f}\n"
            f"{legs_text}"
            f"{entry_idx_text}  →  {close_idx_text}  |  {idx_text}\n"
            f"Equity: ${account.equity:,.2f}\n"
            f"Avail margin: ${account.available_margin:,.2f} "
            f"({100 - account.margin_utilization:.1f}% free)"
        )
    except Exception:
        pass


# ─── Strategy Factory ──────────────────────────────────────────────────────

def atm_straddle_index_move() -> StrategyConfig:
    """
    Daily ATM straddle — buy ATM call + ATM put, close on BTC index
    move or time.

    Returns a StrategyConfig for registration in main.py's STRATEGIES list.
    """
    return StrategyConfig(
        name="atm_straddle_index_move",

        # ── What to trade ────────────────────────────────────────────────
        legs=straddle(
            qty=QTY,
            dte=DTE,
            side=1,          # 1 = BUY (long straddle)
        ),

        # ── When to enter ────────────────────────────────────────────────
        entry_conditions=[
            time_window(OPEN_HOUR, OPEN_HOUR + 1),
            min_available_margin_pct(MIN_MARGIN_PCT),
        ],

        # ── When to exit ─────────────────────────────────────────────────
        # ANY condition returning True triggers a close:
        #   1. Index move ≥ $MOVE_DISTANCE_USD from entry → close
        #   2. Hard time exit → close at CLOSE_HOUR:CLOSE_MINUTE UTC
        exit_conditions=[
            index_move_distance(MOVE_DISTANCE_USD),
            time_exit(CLOSE_HOUR, CLOSE_MINUTE),
        ],

        # ── How to execute ───────────────────────────────────────────────
        execution_mode="limit",

        # ── Operational limits ───────────────────────────────────────────
        max_concurrent_trades=1,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
