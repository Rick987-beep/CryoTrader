"""
Daily Put Sell — BTC 1DTE OTM Put Selling Strategy (v2)

Sells a 1DTE BTC put option near -0.10 delta every day during 03:00–04:00 UTC.

Fair Price Model:
  At 3 AM the orderbook for far-OTM options is thin.  The exchange mark
  price can be far from tradeable reality.  We compute a "fair price":
    - mark, if it sits between bid and ask (mark is reasonable)
    - mid = (bid+ask)/2, if mark is outside the spread (mark is stale/off)
    - max(mark, bid), if only bid exists (no ask side)
    - mark alone, if the book is empty (last resort)
  fairspread = fair - bid  (measures how far bid is from fair value)

Open Execution (sell put — patient, up to ~5 min total):
  Phase 1 — RFQ (20s silent + up to 3 min gated):
    Collect market-maker quotes for 20s, then accept if the quote is
    at least bid + 33% of fairspread.  MMs rarely beat the book at 3 AM,
    so this often times out — but costs nothing to try.
  Phase 2.1 — Limit at fair (60s):
    Place limit sell at our computed fair price.
  Phase 2.2 — Limit at bid + 33% of spread (60s):
    Step closer to bid — one third of the way from bid to fair.
  Phase 2.3 — Limit at bid (60s):
    Hit the bid — aggressive fill to ensure entry.

Stop Loss (fair-price based, 70% of premium):
  SL threshold = fill_price × 1.7 (70% loss).  Each tick, we recompute
  fair price.  If fair_price >= SL threshold, close via phased limit
  buy-to-close: 15s at fair → 15s stepping toward ask → aggressive at ask.
  Skips RFQ entirely for fast execution on SL.

Take Profit (fill-price based, 90% capture):
  Limit buy at fill_price × 0.10 placed immediately after open.
  Sits in the book until filled or cancelled by SL/expiry.

Expiry:
  If neither TP nor SL fires, the option expires worthless (full win).
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
)
from trade_execution import ExecutionParams, ExecutionPhase
from trade_lifecycle import RFQParams, TradeState
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────

# Structure
QTY = 0.8                            # BTC per leg (~$68k notional)
TARGET_DELTA = -0.10                 # OTM put delta target
DTE = 1                              # 1 day to expiry

# Scheduling — UTC hours
ENTRY_HOUR_START = 3                 # Open window: 03:00 UTC
ENTRY_HOUR_END = 4                   # Close window: 04:00 UTC

# Risk
MIN_MARGIN_PCT = 10                  # Require ≥10% available margin
STOP_LOSS_PCT = 70                   # 70% loss of premium collected
TP_CAPTURE_PCT = 0.90                # Buy back at 10% of fill price (90% profit)

# RFQ open — phased execution
RFQ_OPEN_TIMEOUT = 200               # 20s silent + 180s (3 min) gated window
RFQ_INITIAL_WAIT = 20                # Collect quotes silently for 20s
RFQ_SPREAD_FRACTION = 0.33           # Accept if quote ≥ bid + 33% of fairspread

# Limit open fallback — phased after RFQ timeout
LIMIT_OPEN_FAIR_SECONDS = 60         # Phase 2.1: quote at fair price
LIMIT_OPEN_PARTIAL_SECONDS = 60      # Phase 2.2: quote at bid + 33% fairspread
LIMIT_OPEN_BID_SECONDS = 60          # Phase 2.3: aggressive at bid

# SL close — phased limit buy-to-close (no RFQ)
SL_CLOSE_FAIR_SECONDS = 15           # Buy at fair price
SL_CLOSE_STEP_SECONDS = 15           # Step toward ask
SL_CLOSE_AGG_SECONDS = 60            # Aggressive at ask

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


# ─── Fair Price Calculation ─────────────────────────────────────────────────
# At 3 AM, far-OTM option books are thin.  Exchange mark can diverge wildly
# from what's actually tradeable.  This function computes a "fair" price by
# cross-referencing mark against the orderbook:
#   - mark between bid and ask  →  trust mark
#   - mark outside bid/ask      →  use mid = (bid+ask)/2
#   - only bid exists            →  max(mark, bid)
#   - no book at all             →  mark (last resort)
#
# fairspread = fair - bid  measures the gap between bid and fair value.
# fairspread_ask = ask - fair  measures the gap on the ask side.

def compute_fair_price(symbol: str) -> Optional[dict]:
    """
    Compute fair price, bid, ask, and spreads for an option symbol.

    Returns dict with keys: fair, bid, ask, mark, fairspread, fairspread_ask.
    Returns None if no market data is available at all.
    """
    mkt = get_option_market_data(symbol)
    if not mkt:
        return None

    bid = float(mkt.get('bid', 0) or 0)
    ask = float(mkt.get('ask', 0) or 0)
    mark = float(mkt.get('mark_price', 0) or 0)

    if bid > 0 and ask > 0:
        # Both sides of the book exist — best case
        if bid <= mark <= ask:
            fair = mark
        else:
            fair = (bid + ask) / 2
        fairspread = fair - bid
        fairspread_ask = ask - fair
    elif bid > 0:
        # Only bid side — no ask in book
        fair = max(mark, bid) if mark > 0 else bid
        fairspread = fair - bid
        fairspread_ask = 0.0
    elif mark > 0:
        # No book at all — use mark as last resort
        fair = mark
        fairspread = 0.0
        fairspread_ask = 0.0
    else:
        return None

    return {
        'fair': fair,
        'bid': bid if bid > 0 else None,
        'ask': ask if ask > 0 else None,
        'mark': mark,
        'fairspread': fairspread,
        'fairspread_ask': fairspread_ask,
    }


# ─── Dynamic RFQ Gate ──────────────────────────────────────────────────────
# The RFQ improvement gate is a percentage: how much better than the orderbook
# bid the quote needs to be.  We compute this dynamically at trade time from
# the fair price model:  quote ≥ bid + 33% * fairspread  ↔  improvement ≥ X%.

def _compute_rfq_gate(trade) -> float:
    """
    Callable for metadata['rfq_min_book_improvement_pct'].

    Called by execution_router just before submitting the RFQ.
    Computes the improvement % threshold from the current fair price.
    """
    if not trade.open_legs:
        return 999.0

    symbol = trade.open_legs[0].symbol
    fp = compute_fair_price(symbol)

    if not fp or not fp['bid'] or fp['fairspread'] <= 0:
        logger.warning(
            f"[DailyPutSell] No bid or zero fairspread for {symbol} "
            f"— RFQ gate set high (will likely time out)"
        )
        return 999.0

    # Improvement = (quote - bid) / bid × 100
    # We want quote ≥ bid + fraction × fairspread
    # So: min_improvement = fraction × fairspread / bid × 100
    gate_pct = RFQ_SPREAD_FRACTION * fp['fairspread'] / fp['bid'] * 100
    logger.info(
        f"[DailyPutSell] RFQ gate: bid=${fp['bid']:.2f} fair=${fp['fair']:.2f} "
        f"spread=${fp['fairspread']:.2f} → min_improvement={gate_pct:.1f}%"
    )
    return gate_pct


# ─── Exit Condition: Fair-Price Stop Loss ───────────────────────────────────
# For a short put, we lose money when the option price RISES (underlying drops,
# put goes ITM).  SL fires when the current fair price reaches 1.7× fill price,
# meaning we'd lose 70% of the premium we collected.
#
# On trigger, this condition also configures the close execution: switches from
# RFQ to limit mode with phased pricing (fair → step toward ask → aggressive).

def _fair_price_sl():
    """
    Exit condition: fair-price based stop loss.

    SL threshold = fill_price × (1 + STOP_LOSS_PCT/100).
    Triggers when fair_price ≥ SL threshold.

    On trigger, configures phased limit close (buy-to-close) and sets
    execution_mode to 'limit' so the close bypasses RFQ.
    """
    label = f"fair_price_sl({STOP_LOSS_PCT}%)"

    def _check(account, trade) -> bool:
        leg = trade.open_legs[0] if trade.open_legs else None
        if not leg or not leg.fill_price:
            return False

        # Compute or retrieve SL threshold (set once, stored in metadata)
        sl_threshold = trade.metadata.get("sl_threshold")
        if sl_threshold is None:
            sl_threshold = float(leg.fill_price) * (1.0 + STOP_LOSS_PCT / 100.0)
            trade.metadata["sl_threshold"] = sl_threshold

        # Get current fair price
        fp = compute_fair_price(leg.symbol)
        if not fp or fp['fair'] <= 0:
            return False  # no data — skip this tick (safe)

        triggered = fp['fair'] >= sl_threshold
        if triggered:
            loss_pct = (fp['fair'] - float(leg.fill_price)) / float(leg.fill_price) * 100
            logger.info(
                f"[{trade.id}] {label} TRIGGERED: fair=${fp['fair']:.2f} "
                f">= threshold=${sl_threshold:.2f} "
                f"(fill=${leg.fill_price:.2f}, loss={loss_pct:.1f}%)"
            )
            logger.info(
                f"[{trade.id}] SL prices: "
                f"bid=${fp['bid'] or 0:.2f}  ask=${fp['ask'] or 0:.2f}  "
                f"mid=${((fp['bid'] or 0) + (fp['ask'] or 0)) / 2:.2f}  "
                f"fair=${fp['fair']:.2f}  mark=${fp['mark']:.2f}  "
                f"fairspread=${fp['fairspread']:.2f}"
            )

            # Configure phased limit close (buy-to-close, no RFQ)
            trade.execution_mode = "limit"
            trade.metadata["sl_triggered"] = True
            trade.execution_params = ExecutionParams(phases=[
                # Phase 1: buy at fair price (passive)
                ExecutionPhase(
                    pricing="fair", fair_aggression=0.0,
                    duration_seconds=SL_CLOSE_FAIR_SECONDS,
                    reprice_interval=SL_CLOSE_FAIR_SECONDS,
                ),
                # Phase 2: step toward ask (more aggressive)
                ExecutionPhase(
                    pricing="fair", fair_aggression=0.33,
                    duration_seconds=SL_CLOSE_STEP_SECONDS,
                    reprice_interval=SL_CLOSE_STEP_SECONDS,
                ),
                # Phase 3: aggressive at ask (or mark×1.2 if no ask)
                ExecutionPhase(
                    pricing="fair", fair_aggression=1.0,
                    duration_seconds=SL_CLOSE_AGG_SECONDS,
                    reprice_interval=15,
                ),
            ])

        return triggered

    _check.__name__ = label
    return _check


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
        leg = trade.open_legs[0] if trade.open_legs else None
        fp = compute_fair_price(leg.symbol) if leg else None
        logger.info(
            f"[DailyPutSell] TP order {tp_order_id} FILLED — "
            f"finalizing trade {trade.id}"
        )
        if fp and leg:
            logger.info(
                f"[DailyPutSell] TP fill prices: "
                f"bid=${fp['bid'] or 0:.2f}  ask=${fp['ask'] or 0:.2f}  "
                f"fair=${fp['fair']:.2f}  mark=${fp['mark']:.2f}  "
                f"fill=${record.avg_fill_price or 0:.2f}  "
                f"entry=${leg.fill_price:.2f}"
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
    Called when the short put trade is opened (RFQ or limit filled).

    1. Computes fair price and SL threshold from fill price.
    2. Places a limit buy TP order at 10% of fill price.
    3. Logs entry details and sends Telegram notification.
    """
    # Capture entry index price
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price

    leg = trade.open_legs[0] if trade.open_legs else None
    premium = leg.fill_price if leg and leg.fill_price else 0

    # Compute fair price at open and SL threshold
    fair_at_open = None
    if leg:
        fp = compute_fair_price(leg.symbol)
        if fp:
            fair_at_open = fp['fair']
            trade.metadata["fair_at_open"] = fair_at_open
            trade.metadata["bid_at_open"] = fp['bid']
            trade.metadata["ask_at_open"] = fp['ask']
            trade.metadata["fairspread_at_open"] = fp['fairspread']

    # SL threshold: fill_price × 1.7 for 70% loss
    sl_threshold = None
    if premium and premium > 0:
        sl_threshold = float(premium) * (1.0 + STOP_LOSS_PCT / 100.0)
        trade.metadata["sl_threshold"] = sl_threshold

    # Mark price at open (may already be in metadata from execution_router)
    mark_at_open = None
    if leg:
        mark_at_open = trade.metadata.get(f"mark_at_open_{leg.symbol}")
        if not mark_at_open:
            mkt = get_option_market_data(leg.symbol)
            if mkt:
                mark_at_open = mkt.get('mark_price', 0)
                trade.metadata[f"mark_at_open_{leg.symbol}"] = mark_at_open

    logger.info(
        f"[DailyPutSell] Opened: SELL {leg.symbol if leg else '?'} "
        f"@ ${premium:.4f}  |  fair=${fair_at_open:.4f}  |  "
        f"mark=${mark_at_open:.4f}  |  "
        f"SL@=${sl_threshold:.4f}  |  BTC=${index_price:,.0f}"
        if (fair_at_open and mark_at_open and sl_threshold and index_price)
        else
        f"[DailyPutSell] Opened: SELL {leg.symbol if leg else '?'} "
        f"@ ${premium:.4f}"
    )

    # Log detailed pricing snapshot at entry
    if leg:
        fp = compute_fair_price(leg.symbol)
        if fp:
            logger.info(
                f"[DailyPutSell] Entry prices: "
                f"bid=${fp['bid'] or 0:.2f}  ask=${fp['ask'] or 0:.2f}  "
                f"mid=${((fp['bid'] or 0) + (fp['ask'] or 0)) / 2:.2f}  "
                f"fair=${fp['fair']:.2f}  mark=${fp['mark']:.2f}  "
                f"fairspread=${fp['fairspread']:.2f}"
            )

    # Place TP limit order
    _place_tp_limit_order(trade)

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Execution mode: RFQ result message or "Limit"
    exec_mode = "unknown"
    if trade.rfq_result and trade.rfq_result.success:
        exec_mode = trade.rfq_result.message or "RFQ"
    elif trade.rfq_result and not trade.rfq_result.success:
        exec_mode = f"Limit (RFQ failed: {trade.rfq_result.message})"
    else:
        exec_mode = "Limit"

    # Opening duration
    duration_s = int(trade.opened_at - trade.created_at) if trade.opened_at and trade.created_at else 0

    # Price block
    bid = fp['bid'] or 0 if fp else 0
    ask = fp['ask'] or 0 if fp else 0
    mid = (bid + ask) / 2 if (bid and ask) else 0

    # Fill vs fair
    fill_vs_fair = ""
    if fair_at_open and premium:
        diff = premium - fair_at_open
        diff_pct = diff / fair_at_open * 100
        fill_vs_fair = f"Fill vs fair: ${premium:.2f} vs ${fair_at_open:.2f} ({diff_pct:+.1f}%)"

    try:
        get_notifier().send(
            f"📉 <b>Daily Put Sell — Trade Opened</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"SELL {leg.filled_qty}× {leg.symbol}\n\n"
            f"Premium: <b>${premium:.2f}</b>\n"
            f"Execution: {exec_mode}\n"
            f"Duration: {duration_s}s\n\n"
            f"Prices at open:\n"
            f"  mark=${mark_at_open or 0:.2f}  mid=${mid:.2f}  fair=${fair_at_open or 0:.2f}\n"
            f"  bid=${bid:.2f}  ask=${ask:.2f}\n"
            f"{fill_vs_fair}\n\n"
            f"BTC index: ${index_price:,.0f}" if index_price else "BTC index: N/A"
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

    # Determine exit reason — priority: metadata flags > PnL > hold time
    exit_reason = "unknown"
    if trade.metadata.get("tp_finalized"):
        exit_reason = "TP (limit fill)"
    elif trade.metadata.get("sl_triggered"):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss, fair-price)"
    elif pnl <= -(abs(entry_cost) * STOP_LOSS_PCT / 100):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss)"
    elif pnl > 0:
        exit_reason = "profit"
    elif hold_seconds > 82800 and abs(pnl) < abs(entry_cost) * 0.05:
        exit_reason = "expiry (worthless)"

    logger.info(
        f"[DailyPutSell] Closed: {trade.id}  |  PnL: ${pnl:+.4f}  |  "
        f"ROI: {roi:+.1f}%  |  Hold: {hold_seconds/60:.1f}min  |  "
        f"Exit: {exit_reason}"
    )

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "✅" if pnl >= 0 else "❌"

    # Trigger details
    leg = trade.open_legs[0] if trade.open_legs else None
    entry_price = float(leg.fill_price) if leg and leg.fill_price else 0
    sl_threshold = trade.metadata.get("sl_threshold")
    tp_price = trade.metadata.get("tp_price")

    trigger_text = ""
    if trade.metadata.get("tp_finalized"):
        trigger_text = (
            f"Trigger: <b>Take Profit</b>\n"
            f"TP target: ${tp_price:.2f} ({TP_CAPTURE_PCT*100:.0f}% of ${entry_price:.2f} entry)"
            if tp_price else "Trigger: <b>Take Profit</b>"
        )
    elif trade.metadata.get("sl_triggered"):
        trigger_text = (
            f"Trigger: <b>Stop Loss</b>\n"
            f"SL threshold: ${sl_threshold:.2f} ({STOP_LOSS_PCT}% loss on ${entry_price:.2f} entry)"
            if sl_threshold else "Trigger: <b>Stop Loss</b>"
        )
    elif exit_reason.startswith("expiry"):
        trigger_text = "Trigger: <b>Expiry</b> (option expired worthless)"
    else:
        trigger_text = f"Trigger: <b>{exit_reason}</b>"

    # Price snapshot at close
    close_fill = None
    close_symbol = None
    if trade.close_legs and trade.close_legs[0].fill_price:
        close_fill = float(trade.close_legs[0].fill_price)
        close_symbol = trade.close_legs[0].symbol
    elif leg:
        close_symbol = leg.symbol

    fp = compute_fair_price(close_symbol) if close_symbol else None
    price_text = ""
    if fp:
        c_bid = fp['bid'] or 0
        c_ask = fp['ask'] or 0
        c_mid = (c_bid + c_ask) / 2 if (c_bid and c_ask) else 0
        price_text = (
            f"\nPrices at close:\n"
            f"  mark=${fp['mark']:.2f}  mid=${c_mid:.2f}  fair=${fp['fair']:.2f}\n"
            f"  bid=${c_bid:.2f}  ask=${c_ask:.2f}"
        )

    # Fill vs fair at close
    fill_vs_fair = ""
    if close_fill and fp and fp['fair'] > 0:
        diff = close_fill - fp['fair']
        diff_pct = diff / fp['fair'] * 100
        fill_vs_fair = f"\nFill vs fair: ${close_fill:.2f} vs ${fp['fair']:.2f} ({diff_pct:+.1f}%)"

    # BTC index
    close_index = get_btc_index_price(use_cache=False)
    idx_text = f"BTC index: ${close_index:,.0f}" if close_index else "BTC index: N/A"

    try:
        get_notifier().send(
            f"{emoji} <b>Daily Put Sell — Trade Closed</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"{trigger_text}\n\n"
            f"PnL: <b>${pnl:+.2f}</b> ({roi:+.1f}%)\n"
            f"Hold: {hold_seconds/60:.1f} min\n"
            f"{price_text}"
            f"{fill_vs_fair}\n\n"
            f"{idx_text}"
        )
    except Exception:
        pass


# ─── Strategy Factory ──────────────────────────────────────────────────────

def daily_put_sell() -> StrategyConfig:
    """
    Daily BTC put selling strategy (v2).

    Sells a 1DTE OTM put (~10 delta) daily during 03:00–04:00 UTC.
    Uses fair-price model for execution and risk management.
    """
    return StrategyConfig(
        name="daily_put_sell",

        # ── What to trade ────────────────────────────────────────────
        legs=[
            LegSpec(
                option_type="P",
                side="sell",
                qty=QTY,
                strike_criteria={"type": "delta", "value": TARGET_DELTA},
                expiry_criteria={"dte": DTE},
            ),
        ],

        # ── When to enter ────────────────────────────────────────────
        entry_conditions=[
            time_window(ENTRY_HOUR_START, ENTRY_HOUR_END),
            # ema20_filter(),                       # TEST: disabled
            # min_available_margin_pct(MIN_MARGIN_PCT),  # TEST: disabled
        ],

        # ── When to exit ─────────────────────────────────────────────
        # 1. TP: _tp_filled_exit detects the standing limit buy fill
        #    and finalizes the trade directly (no close flow needed).
        # 2. SL: _fair_price_sl at 70% loss based on fair price vs fill.
        #    On trigger, it switches to limit mode and configures phased
        #    buy-to-close (15s fair → 15s step → aggressive at ask).
        # 3. Expiry: option expires worthless → full premium captured.
        exit_conditions=[
            _tp_filled_exit(),
            _fair_price_sl(),
        ],

        # ── How to execute ───────────────────────────────────────────
        # OPEN path: RFQ phased → limit phased fallback.
        #   RFQ: 20s silent + 3 min gated (bid + 33% fairspread).
        #   Limit fallback: 60s at fair → 60s at bid+33%spread → 60s at bid.
        # CLOSE path: Configured dynamically by _fair_price_sl when SL fires
        #   (limit phased, no RFQ). For non-SL closes (manual/emergency),
        #   rfq_params provides a reasonable RFQ close as fallback.
        execution_mode="rfq",
        rfq_action="sell",

        # rfq_params: used for non-SL close paths (manual close, emergencies)
        rfq_params=RFQParams(
            timeout_seconds=15,
            min_improvement_pct=-999,    # accept any quote for emergency close
            fallback_mode="limit",
        ),

        # execution_params: used for limit open fallback (after RFQ timeout)
        # fair pricing with aggression 0→0.67→1.0 steps from fair→spread→bid
        execution_params=ExecutionParams(phases=[
            # Phase 2.1: sell at fair price
            ExecutionPhase(
                pricing="fair", fair_aggression=0.0,
                duration_seconds=LIMIT_OPEN_FAIR_SECONDS,
                reprice_interval=30,
            ),
            # Phase 2.2: sell at bid + 33% of fairspread
            ExecutionPhase(
                pricing="fair", fair_aggression=0.67,
                duration_seconds=LIMIT_OPEN_PARTIAL_SECONDS,
                reprice_interval=30,
            ),
            # Phase 2.3: sell at bid (aggressive)
            ExecutionPhase(
                pricing="fair", fair_aggression=1.0,
                duration_seconds=LIMIT_OPEN_BID_SECONDS,
                reprice_interval=15,
            ),
        ]),

        # ── Operational limits ───────────────────────────────────────
        max_concurrent_trades=MAX_CONCURRENT,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,

        # ── Metadata ─────────────────────────────────────────────────
        # RFQ phased open configuration (read by execution_router):
        #   - rfq_min_book_improvement_pct is a callable that computes
        #     the gate dynamically from fair price at trade time
        #   - relax_after=999 ensures the gate never relaxes (no Phase 3)
        metadata={
            "rfq_phased": True,
            "rfq_initial_wait_seconds": RFQ_INITIAL_WAIT,
            "rfq_min_book_improvement_pct": _compute_rfq_gate,
            "rfq_timeout_seconds": RFQ_OPEN_TIMEOUT,
            "rfq_relax_after_seconds": 999,
            "on_runner_created": _capture_context,
        },
    )
