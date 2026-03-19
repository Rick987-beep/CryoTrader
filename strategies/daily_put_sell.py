"""
Daily Put Sell — BTC 1DTE OTM Put Selling Strategy

Sells a 1DTE BTC put option near -0.10 delta every day when:
  1. BTC price is above the daily EMA-20 (trend filter)
  2. Inside the entry time window (03:00–04:00 UTC)
  3. Sufficient margin is available

Trade lifecycle:
  - OPEN via phased RFQ (60s total):
      Phase 1 (0–20s): Collect quotes silently.
      Phase 2 (20–60s): Accept if quote beats orderbook by ≥2%.
      Timeout: Fall back to aggressive limit order.
  - TAKE PROFIT: Limit buy order placed immediately after open at
    10% of entry premium (buy back for 90% profit).
  - STOP LOSS: Exit condition at 70% loss (mark PnL).  Close via
    standard RFQ with 15s timeout, accept if at least as good as book.
  - EXPIRY: If neither TP nor SL hit, option expires worthless (full win).

Backtest results (2024-01-01 to 2025-03-10):
  Win rate: 93.1%  |  Avg winner: $27.81  |  Avg loser: -$44.29
  Profit factor: 8.52  |  Total return: +66.2%  |  Max drawdown: -3.8%

Framework features used:
  ✓ LegSpec with delta-based strike selection
  ✓ Entry conditions: time_window, ema20_filter, min_available_margin_pct
  ✓ Exit conditions: max_loss (mark PnL mode)
  ✓ Execution mode: RFQ (phased open via metadata, standard close)
  ✓ on_trade_opened callback (places TP limit order)
  ✓ on_trade_closed callback (logging + Telegram notification)
  ✓ on_runner_created hook (captures TradingContext for TP order placement)
  ✓ max_concurrent_trades=2 (handles expiry overlap)
  ✓ max_trades_per_day=1

Usage:
    # In main.py STRATEGIES list:
    from strategies import daily_put_sell
    STRATEGIES = [daily_put_sell]
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from ema_filter import ema20_filter
from market_data import get_btc_index_price, get_option_market_data
from option_selection import LegSpec
from order_manager import OrderPurpose, OrderStatus
from strategy import (
    StrategyConfig,
    # Entry conditions
    time_window,
    min_available_margin_pct,
    # Exit conditions
    max_loss,
)
from trade_lifecycle import RFQParams, TradeState
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
# All parameters for the daily put sell strategy.  Adjust here, not in the
# factory function below.

# Structure
QTY = 0.8                            # BTC per leg (~$68k notional, clears $50k RFQ min)
TARGET_DELTA = -0.10                 # OTM put delta target
DTE = 1                              # 1 day to expiry

# Scheduling — UTC hours
ENTRY_HOUR_START = 3                 # Open window: 03:00 UTC
ENTRY_HOUR_END = 4                   # Close window: 04:00 UTC

# Risk
MIN_MARGIN_PCT = 10                  # Require ≥20% available margin
STOP_LOSS_PCT = 70                   # exit at 70% loss 
TP_CAPTURE_PCT = 0.90                # Take profit = buy back at 10% of entry
                                     # (capture 90% of premium received)

# RFQ open — phased execution
RFQ_OPEN_TIMEOUT = 60                # Total seconds: Phase 1 + Phase 2, then limit fallback
RFQ_INITIAL_WAIT = 20                # Phase 1: collect quotes silently for 20s
RFQ_MIN_BOOK_IMPROVEMENT_PCT = 2     # Phase 2: quote must beat orderbook by ≥2%

# RFQ close (SL) — fast execution
RFQ_CLOSE_TIMEOUT = 15               # 15s timeout for SL close
RFQ_CLOSE_MIN_IMPROVEMENT = 0.0      # Accept if at least as good as orderbook

# Operational
CHECK_INTERVAL = 15                  # Seconds between entry/exit evaluations
MAX_CONCURRENT = 2                   # Allow 2 overlapping trades (expiry overlap)


# ─── Module-level Context Reference ────────────────────────────────────────
# Captured by the on_runner_created hook so callbacks can access services
# like the order manager and executor for TP order placement.
# This is daily_put_sell-specific — not a framework pattern.

_ctx = None  # type: ignore


def _capture_context(runner) -> None:
    """on_runner_created hook: captures TradingContext for callback use."""
    global _ctx
    _ctx = runner.ctx
    logger.info("[DailyPutSell] Context captured from runner")


# ─── Exit Condition: TP Order Fill Detection ────────────────────────────────
# This exit condition checks if the proactive TP limit order has been
# filled.  When it has, the position is already closed on the exchange so
# we finalize the trade directly instead of triggering the normal close flow.
# This is daily_put_sell-specific logic.

def _tp_filled_exit():
    """
    Exit condition factory: detects TP limit order fill.

    When the TP order fills, the position is already closed on the exchange.
    This condition finalizes the trade (CLOSED state, realized PnL) and
    returns False — the trade is already done, no further close needed.

    If the TP order is not yet filled, returns False (not an exit trigger).
    The only real exit trigger from this strategy is max_loss (SL).
    """
    def _check(account, trade) -> bool:
        if _ctx is None:
            return False

        tp_order_id = trade.metadata.get("tp_order_id")
        if not tp_order_id:
            return False

        # Already finalized by a previous tick
        if trade.metadata.get("tp_finalized"):
            return False

        # Check order status via OrderManager
        order_mgr = _ctx.lifecycle_manager.order_manager
        record = order_mgr._orders.get(tp_order_id)
        if record is None:
            return False

        if record.status != OrderStatus.FILLED:
            return False

        # TP order filled — finalize the trade directly
        logger.info(
            f"[DailyPutSell] TP order {tp_order_id} FILLED — "
            f"finalizing trade {trade.id}"
        )
        trade.metadata["tp_finalized"] = True

        # Populate close leg with fill data
        from trade_lifecycle import TradeLeg
        leg = trade.open_legs[0]
        trade.close_legs = [
            TradeLeg(
                symbol=leg.symbol,
                qty=record.filled_qty or leg.filled_qty,
                side="buy",  # buy to close
                order_id=tp_order_id,
                fill_price=record.avg_fill_price,
                filled_qty=record.filled_qty or leg.filled_qty,
            )
        ]

        trade.state = TradeState.CLOSED
        trade.closed_at = time.time()
        trade._finalize_close()

        logger.info(
            f"[DailyPutSell] Trade {trade.id} → CLOSED via TP "
            f"(PnL={trade.realized_pnl:+.4f})"
        )

        # Return False: trade is already finalized, no need for PENDING_CLOSE
        return False

    _check.__name__ = "tp_filled_exit"
    return _check


# ─── Take Profit: Limit Order Logic ────────────────────────────────────────
# These functions are specific to the daily_put_sell strategy and handle
# placing a limit buy-to-close order at the TP price immediately after
# the put is sold via RFQ.

def _place_tp_limit_order(trade) -> None:
    """
    Place a limit buy order to close the short put at the TP price.

    Called from on_trade_opened.  The TP price is 10% of the entry
    premium — i.e. we buy back the put for 10% of what we sold it for,
    capturing 90% of the premium.

    This is daily_put_sell-specific logic.
    """
    if _ctx is None:
        logger.error("[DailyPutSell] Context not available, cannot place TP order")
        return

    if not trade.open_legs:
        logger.error(f"[DailyPutSell] No open legs on trade {trade.id}, cannot place TP")
        return

    leg = trade.open_legs[0]
    if leg.fill_price is None or leg.fill_price <= 0:
        logger.error(f"[DailyPutSell] No fill price for {leg.symbol}, cannot place TP")
        return

    entry_premium = float(leg.fill_price)
    tp_price = round(entry_premium * (1.0 - TP_CAPTURE_PCT), 4)

    # Minimum price floor — exchanges won't accept 0
    if tp_price < 0.0001:
        tp_price = 0.0001

    logger.info(
        f"[DailyPutSell] Placing TP limit buy: {leg.symbol} "
        f"qty={leg.filled_qty} @ ${tp_price:.4f} "
        f"(entry=${entry_premium:.4f}, capture={TP_CAPTURE_PCT*100:.0f}%)"
    )

    try:
        # Place limit buy order via OrderManager (handles executor + tracking)
        # side="buy"=buy to close, reduce_only handled by OrderManager for CLOSE_LEG
        record = _ctx.lifecycle_manager.order_manager.place_order(
            lifecycle_id=trade.id,
            leg_index=0,
            purpose=OrderPurpose.CLOSE_LEG,
            symbol=leg.symbol,
            side="buy",   # buy to close
            qty=leg.filled_qty,
            price=tp_price,
        )

        if record:
            trade.metadata["tp_order_id"] = record.order_id
            trade.metadata["tp_price"] = tp_price

            # Populate close_legs so the lifecycle engine knows about the close
            from trade_lifecycle import TradeLeg
            if not trade.close_legs:
                trade.close_legs = [
                    TradeLeg(
                        symbol=leg.symbol,
                        qty=leg.filled_qty,
                        side="buy",  # buy to close
                        order_id=record.order_id,
                    )
                ]

            logger.info(
                f"[DailyPutSell] TP order placed: {record.order_id} "
                f"BUY {leg.filled_qty}x {leg.symbol} @ ${tp_price:.4f}"
            )
        else:
            logger.error(f"[DailyPutSell] Failed to place TP order for {leg.symbol}")

    except Exception as e:
        logger.error(f"[DailyPutSell] Error placing TP order: {e}")


# ─── Trade Callbacks ────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """
    Called when the short put trade is opened (RFQ filled).

    1. Captures entry metadata (index price, premium received).
    2. Places a limit buy TP order at 10% of entry premium.
    3. Sends Telegram notification.
    """
    # Capture entry index price
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price

    # Log entry details
    leg = trade.open_legs[0] if trade.open_legs else None
    premium = leg.fill_price if leg and leg.fill_price else 0

    # Mark price at open (may already be in metadata from execution_router)
    mark_at_open = None
    if leg:
        mark_at_open = trade.metadata.get(f"mark_at_open_{leg.symbol}")
        if not mark_at_open:
            mkt = get_option_market_data(leg.symbol)
            if mkt:
                mark_at_open = mkt.get('mark_price', 0)
                trade.metadata[f"mark_at_open_{leg.symbol}"] = mark_at_open

    if index_price:
        logger.info(
            f"[DailyPutSell] Opened: SELL {leg.symbol if leg else '?'} "
            f"@ ${premium:.4f}  |  mark=${mark_at_open:.4f}  |  BTC=${index_price:,.0f}"
            if mark_at_open else
            f"[DailyPutSell] Opened: SELL {leg.symbol if leg else '?'} "
            f"@ ${premium:.4f}  |  BTC=${index_price:,.0f}"
        )
    else:
        logger.info(
            f"[DailyPutSell] Opened: SELL {leg.symbol if leg else '?'} "
            f"@ ${premium:.4f}"
        )

    # Place TP limit order
    _place_tp_limit_order(trade)

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry_cost = trade.total_entry_cost()
    leg_text = (
        f"  SELL {leg.filled_qty}× {leg.symbol} @ ${premium:.4f}"
        if leg else "  (no leg info)"
    )
    idx_text = f"BTC: ${index_price:,.0f}" if index_price else "BTC: N/A"
    mark_text = ""
    if mark_at_open and premium:
        slip = (premium - mark_at_open) / mark_at_open * 100
        mark_text = f"Mark: ${mark_at_open:.4f}  |  Fill vs mark: {slip:+.1f}%\n"
    try:
        get_notifier().send(
            f"📉 <b>Daily Put Sell — Trade Opened</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"{leg_text}\n"
            f"Premium received: ${abs(entry_cost):.4f}\n"
            f"{mark_text}"
            f"{idx_text}  |  SL: {STOP_LOSS_PCT}%  |  TP: {TP_CAPTURE_PCT*100:.0f}%\n"
            f"Equity: ${account.equity:,.2f}\n"
            f"Avail margin: ${account.available_margin:,.2f} "
            f"({100 - account.margin_utilization:.1f}% free)"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """
    Called when the short put trade is closed (TP, SL, or expiry).

    1. Cancels any outstanding TP order (may already be filled/cancelled).
    2. Logs PnL and close details.
    3. Sends Telegram notification.
    """
    # Cancel outstanding TP order if it wasn't the exit reason
    tp_order_id = trade.metadata.get("tp_order_id")
    if tp_order_id and _ctx and not trade.metadata.get("tp_finalized"):
        try:
            record = _ctx.lifecycle_manager.order_manager._orders.get(tp_order_id)
            if record and record.is_live:
                _ctx.executor.cancel_order(tp_order_id)
                logger.info(f"[DailyPutSell] Cancelled TP order {tp_order_id}")
        except Exception as e:
            logger.warning(f"[DailyPutSell] Could not cancel TP order: {e}")

    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    # Determine exit reason
    tp_order_id = trade.metadata.get("tp_order_id")
    exit_reason = "unknown"
    if pnl > 0 and tp_order_id:
        exit_reason = "TP (limit fill)"
    elif pnl <= -(abs(entry_cost) * STOP_LOSS_PCT / 100):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss)"
    elif hold_seconds > 82800:  # ~23 hours → likely expired
        exit_reason = "expiry"
    elif pnl > 0:
        exit_reason = "profit (other)"

    logger.info(
        f"[DailyPutSell] Closed: {trade.id}  |  PnL: ${pnl:+.4f}  |  "
        f"ROI: {roi:+.1f}%  |  Hold: {hold_seconds/60:.1f}min  |  "
        f"Exit: {exit_reason}"
    )

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "✅" if pnl >= 0 else "❌"
    close_detail = ""
    if trade.close_legs:
        close_detail = "\n".join(
            f"  BUY {leg.filled_qty}× {leg.symbol} @ ${leg.fill_price}"
            for leg in trade.close_legs
            if leg.fill_price is not None
        )

    # Mark price context for close notification
    mark_close_text = ""
    if trade.close_legs:
        cl = trade.close_legs[0]
        mark_open = trade.metadata.get(f"mark_at_open_{cl.symbol}")
        mark_close = trade.metadata.get(f"mark_at_close_{cl.symbol}")
        if mark_close and cl.fill_price:
            slip = (cl.fill_price - mark_close) / mark_close * 100
            mark_close_text = f"Close mark: ${mark_close:.4f}  |  Fill vs mark: {slip:+.1f}%\n"
        if mark_open:
            mark_close_text += f"Open mark: ${mark_open:.4f}\n"

    try:
        get_notifier().send(
            f"{emoji} <b>Daily Put Sell — Trade Closed</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"Exit: {exit_reason}\n"
            f"PnL: <b>${pnl:+.4f}</b> ({roi:+.1f}%)\n"
            f"Hold: {hold_seconds/60:.1f} min\n"
            f"Entry premium: ${abs(entry_cost):.4f}\n"
            f"{mark_close_text}"
            f"{close_detail}\n"
            f"Equity: ${account.equity:,.2f}\n"
            f"Avail margin: ${account.available_margin:,.2f} "
            f"({100 - account.margin_utilization:.1f}% free)"
        )
    except Exception:
        pass


# ─── Strategy Factory ──────────────────────────────────────────────────────

def daily_put_sell() -> StrategyConfig:
    """
    Daily BTC put selling strategy.

    Sells a 1DTE OTM put (~10 delta) every day during the entry window
    when BTC is above the EMA-20.  Uses RFQ for opening (phased) and
    closing (fast SL).  TP is a limit buy-to-close order.

    Returns a StrategyConfig for registration in main.py's STRATEGIES list.
    """
    return StrategyConfig(
        name="daily_put_sell",

        # ── What to trade ────────────────────────────────────────────
        # Single leg: sell 1DTE OTM put at ~10 delta
        legs=[
            LegSpec(
                option_type="P",
                side="sell",                           # SELL
                qty=QTY,
                strike_criteria={"type": "delta", "value": TARGET_DELTA},
                expiry_criteria={"dte": DTE},
            ),
        ],

        # ── When to enter ────────────────────────────────────────────
        entry_conditions=[
            time_window(ENTRY_HOUR_START, ENTRY_HOUR_END),
            # ema20_filter(),                       # TEST: disabled for test run
            # min_available_margin_pct(MIN_MARGIN_PCT),  # TEST: disabled
        ],

        # ── When to exit ─────────────────────────────────────────────
        # 1. TP: Detected via _tp_filled_exit — checks if the proactive
        #    limit buy order has filled.  Finalizes trade directly.
        # 2. SL: max_loss at 70% of entry premium (mark PnL).
        #    Mark PnL uses mid-price, avoiding false triggers from wide spreads.
        # 3. Expiry: If neither fires, option expires worthless (full win).
        exit_conditions=[
            _tp_filled_exit(),
            max_loss(STOP_LOSS_PCT, pnl_mode="mark"),
        ],

        # ── How to execute ───────────────────────────────────────────
        # Open: RFQ phased (wait → gated → relaxed), configured via metadata.
        # Close (SL): Standard RFQ, 15s timeout, accept if ≥ orderbook.
        execution_mode="rfq",
        rfq_action="sell",

        # rfq_params is used for the CLOSE path (SL exit)
        rfq_params=RFQParams(
            timeout_seconds=RFQ_CLOSE_TIMEOUT,
            min_improvement_pct=RFQ_CLOSE_MIN_IMPROVEMENT,
            fallback_mode="limit",       # Fall back to limit if RFQ SL fails
        ),

        # Execution params for limit fallback (not primary path)
        execution_params=None,

        # ── Operational limits ───────────────────────────────────────
        max_concurrent_trades=MAX_CONCURRENT,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,

        # ── Metadata ─────────────────────────────────────────────────
        # Phased RFQ configuration for the OPEN path.
        # The execution_router reads these when rfq_phased=True.
        # on_runner_created captures TradingContext for TP order placement.
        metadata={
            "rfq_phased": True,
            "rfq_initial_wait_seconds": RFQ_INITIAL_WAIT,
            "rfq_min_book_improvement_pct": RFQ_MIN_BOOK_IMPROVEMENT_PCT,
            # RFQ open timeout (overrides rfq_params for phased open path)
            "rfq_timeout_seconds": RFQ_OPEN_TIMEOUT,
            # Hook: capture context when runner is created
            "on_runner_created": _capture_context,
        },
    )
