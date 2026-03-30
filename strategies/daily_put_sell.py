"""
Daily Put Sell — BTC 1DTE OTM Put Selling Strategy

Sells a 1DTE BTC put option near -0.10 delta every day during 03:00–04:00 UTC.

Fair Price Model:
  At 3 AM the orderbook for far-OTM options is thin.  The exchange mark
  price can be far from tradeable reality.  We compute a "fair price":
    - mark, if it sits between bid and ask (mark is reasonable)
    - mid = (bid+ask)/2, if mark is outside the spread (mark is stale/off)
    - max(mark, bid), if only bid exists (no ask side)
    - mark alone, if the book is empty (last resort)
  fairspread = fair - bid  (measures how far bid is from fair value)

Open Execution (sell put — limit only, up to ~2.5 min total):
  Phase 1 — Limit at fair (45s):
    Place limit sell at our computed fair price.
  Phase 2 — Limit at bid + 33% of spread (45s):
    Step closer to bid — one third of the way from bid to fair.
    Skipped if computed price < fair × (1 − MIN_BID_DISCOUNT_PCT%).
  Phase 3 — Limit at bid (60s):
    Hit the bid — aggressive fill to ensure entry.
    Skipped if bid < fair × (1 − MIN_BID_DISCOUNT_PCT%).

Minimum Fill Price (liquidity guard):
  MIN_BID_DISCOUNT_PCT controls the worst acceptable sell price relative to
  fair value.  Default 17%: we won't sell below fair × 0.83.  In thin weekend
  or overnight markets where bid is far from fair, Phases 2 and 3 will
  refuse to place and the trade simply does not open.  The stop loss bypasses
  this check — once in a trade we close no matter what.

Stop Loss (fair-price based, 70% of premium):
  SL threshold = fill_price × 1.7 (70% loss).  Each tick, we recompute
  fair price.  If fair_price >= SL threshold, close via phased limit
  buy-to-close: 15s at fair → 15s stepping toward ask → aggressive at ask.

Expiry:
  If SL does not fire, the option expires worthless (full win).
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from ema_filter import below_ema20_filter
from market_data import get_btc_index_price, get_option_market_data
from option_selection import LegSpec
from strategy import (
    StrategyConfig,
    time_window,
)
from trade_execution import ExecutionParams, ExecutionPhase
from trade_lifecycle import RFQParams
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
# Overridable via PARAM_* env vars (set by slot .toml config at deploy time).
# Defaults below match the current production values.

import os as _os
def _p(name, default, cast=float):
    """Read PARAM_<NAME> from env, falling back to default."""
    return cast(_os.getenv(f"PARAM_{name}", str(default)))

# Structure
QTY = _p("QTY", 0.8)                             # BTC per leg (~$68k notional)
TARGET_DELTA = _p("TARGET_DELTA", -0.10)          # OTM put delta target
DTE = _p("DTE", 1, int)                           # 1 day to expiry

# Scheduling — UTC hours
ENTRY_HOUR_START = _p("ENTRY_HOUR_START", 3, int) # Open window: 03:00 UTC
ENTRY_HOUR_END = _p("ENTRY_HOUR_END", 4, int)     # Close window: 04:00 UTC

# Risk
STOP_LOSS_PCT = _p("STOP_LOSS_PCT", 70, int)      # 70% loss of premium collected

# Limit open — phased execution
LIMIT_OPEN_FAIR_SECONDS = _p("LIMIT_OPEN_FAIR_SECONDS", 45, int)       # Phase 1: quote at fair price
LIMIT_OPEN_PARTIAL_SECONDS = _p("LIMIT_OPEN_PARTIAL_SECONDS", 45, int) # Phase 2: quote at bid + 33% fairspread
LIMIT_OPEN_BID_SECONDS = _p("LIMIT_OPEN_BID_SECONDS", 60, int)         # Phase 3: aggressive at bid

# Liquidity guard — minimum acceptable open fill price
MIN_BID_DISCOUNT_PCT = _p("MIN_BID_DISCOUNT_PCT", 17)          # Won't sell below fair × (1 - %/100)

# SL close — phased limit buy-to-close (no RFQ)
SL_CLOSE_FAIR_SECONDS = _p("SL_CLOSE_FAIR_SECONDS", 15, int)   # Buy at fair price
SL_CLOSE_STEP_SECONDS = _p("SL_CLOSE_STEP_SECONDS", 15, int)   # Step toward ask
SL_CLOSE_AGG_SECONDS = _p("SL_CLOSE_AGG_SECONDS", 60, int)     # Aggressive at ask

# Operational
CHECK_INTERVAL = _p("CHECK_INTERVAL", 15, int)    # Seconds between entry/exit evaluations
MAX_CONCURRENT = _p("MAX_CONCURRENT", 2, int)      # Allow 2 overlapping trades (expiry overlap)


# ─── Fair Price Calculation ─────────────────────────────────────────────────

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



# ─── Exit Condition: Fair-Price Stop Loss ───────────────────────────────────
# For a short put, we lose money when the option price RISES (underlying drops,
# put goes ITM).  SL fires when the current fair price reaches 1.7× fill price,
# meaning we'd lose 70% of the premium we collected.
#
# On trigger, also configures the close execution: switches to phased limit
# buy-to-close (fair → step toward ask → aggressive).

def _fair_price_sl():
    """
    Exit condition: fair-price based stop loss.

    SL threshold = fill_price × (1 + STOP_LOSS_PCT/100).
    Triggers when fair_price ≥ SL threshold.
    On trigger, configures phased limit buy-to-close.
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
            trade.metadata["sl_triggered_at"] = time.time()
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


# ─── Trade Callbacks ────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """
    Called when the short put trade is opened.

    1. Computes fair price and SL threshold from fill price.
    2. Logs entry details and sends Telegram notification.
    """
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price

    leg = trade.open_legs[0] if trade.open_legs else None
    premium = leg.fill_price if leg and leg.fill_price else 0

    fp = compute_fair_price(leg.symbol) if leg else None
    fair_at_open = None
    if fp:
        fair_at_open = fp['fair']
        trade.metadata["fair_at_open"] = fair_at_open
        trade.metadata["bid_at_open"] = fp['bid']
        trade.metadata["ask_at_open"] = fp['ask']
        trade.metadata["fairspread_at_open"] = fp['fairspread']

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
        f"mark=${mark_at_open:.4f}  |  SL@=${sl_threshold:.4f}  |  BTC=${index_price:,.0f}"
    )
    if fp:
        logger.info(
            f"[DailyPutSell] Entry prices: "
            f"bid=${fp['bid'] or 0:.2f}  ask=${fp['ask'] or 0:.2f}  "
            f"mid=${((fp['bid'] or 0) + (fp['ask'] or 0)) / 2:.2f}  "
            f"fair=${fp['fair']:.2f}  mark=${fp['mark']:.2f}  "
            f"fairspread=${fp['fairspread']:.2f}"
        )

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # Opening duration
    duration_s = int(trade.opened_at - trade.created_at) if trade.opened_at and trade.created_at else 0

    # Execution phase (inferred from duration vs phase schedule)
    if duration_s <= LIMIT_OPEN_FAIR_SECONDS:
        phase_label = "Phase 1 (at fair)"
    elif duration_s <= LIMIT_OPEN_FAIR_SECONDS + LIMIT_OPEN_PARTIAL_SECONDS:
        phase_label = "Phase 2 (stepped)"
    else:
        phase_label = "Phase 3 (at bid)"

    # Price block
    bid = fp['bid'] or 0 if fp else 0
    ask = fp['ask'] or 0 if fp else 0
    mid = (bid + ask) / 2 if (bid and ask) else 0

    # Fill distances
    collected = float(premium) * float(leg.filled_qty) if leg and leg.filled_qty else 0.0
    vs_fair = f"vs fair {(premium - fair_at_open) / fair_at_open * 100:+.1f}%" if fair_at_open else ""
    vs_bid  = f"vs bid {(premium - bid) / bid * 100:+.1f}%" if bid else ""
    distances = "  ·  ".join(x for x in [vs_fair, vs_bid] if x)

    try:
        get_notifier().send(
            f"📉 <b>Daily Put Sell — Trade Opened</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"SELL {leg.filled_qty}× {leg.symbol}\n\n"
            f"Collected: <b>${collected:.2f}</b>  ({leg.filled_qty}× ${premium:.2f})\n"
            f"{phase_label}  ·  {duration_s}s  ·  {distances}\n\n"
            f"Prices at open:\n"
            f"  mark=${mark_at_open or 0:.2f}  mid=${mid:.2f}  fair=${fair_at_open or 0:.2f}\n"
            f"  bid=${bid:.2f}  ask=${ask:.2f}\n\n"
            f"BTC index: ${index_price:,.0f}" if index_price else "BTC index: N/A"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """
    Called when the short put trade is closed (SL or expiry).

    1. Logs PnL and close details.
    2. Sends Telegram notification.
    """
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    # Determine exit reason — priority: metadata flags > PnL > hold time
    exit_reason = "unknown"
    if trade.metadata.get("sl_triggered"):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss, fair-price)"
    elif pnl <= -(abs(entry_cost) * STOP_LOSS_PCT / 100):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss)"
    elif trade.metadata.get("expiry_settled"):
        exit_reason = "expiry (worthless)"
    elif pnl > 0:
        exit_reason = "profit"

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

    trigger_text = ""
    if trade.metadata.get("sl_triggered"):
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

    # Close execution phase (inferred from metadata and timing)
    close_qty = (trade.close_legs[0].filled_qty if (trade.close_legs and trade.close_legs[0].filled_qty) else None) or (leg.filled_qty if leg else '?')
    close_exec = ""
    if trade.metadata.get("sl_triggered"):
        sl_triggered_at = trade.metadata.get("sl_triggered_at")
        if sl_triggered_at and trade.closed_at:
            close_duration = trade.closed_at - float(sl_triggered_at)
            if close_duration < SL_CLOSE_FAIR_SECONDS:
                close_exec = "Execution: SL close — Phase 1 (at fair)"
            elif close_duration < SL_CLOSE_FAIR_SECONDS + SL_CLOSE_STEP_SECONDS:
                close_exec = "Execution: SL close — Phase 2 (stepped)"
            else:
                close_exec = "Execution: SL close — Phase 3 (at ask)"
        else:
            close_exec = "Execution: SL close (phased limit)"
    elif exit_reason.startswith("expiry"):
        close_exec = "Execution: expired"

    # BTC index
    close_index = get_btc_index_price(use_cache=False)
    idx_text = f"BTC index: ${close_index:,.0f}" if close_index else "BTC index: N/A"

    try:
        close_exec_line = f"{close_exec}\n" if close_exec else ""
        get_notifier().send(
            f"{emoji} <b>Daily Put Sell — Trade Closed</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"BUY {close_qty}\u00d7 {close_symbol}\n"
            f"{trigger_text}\n"
            f"{close_exec_line}"
            f"\nPnL: <b>${pnl:+.2f}</b> ({roi:+.1f}%)\n"
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
            below_ema20_filter(),
        ],

        # ── When to exit ─────────────────────────────────────────────
        # SL: _fair_price_sl at 70% loss based on fair price vs fill.
        #     On trigger, switches to limit mode and configures phased
        #     buy-to-close (15s fair → 15s step → aggressive at ask).
        # Expiry: option expires worthless → full premium captured.
        exit_conditions=[
            _fair_price_sl(),
        ],

        # ── How to execute ───────────────────────────────────────────
        # OPEN path: limit only, 3 phases (45s fair → 45s stepped → 60s bid).
        # CLOSE path: Configured dynamically by _fair_price_sl when SL fires
        #   (limit phased, no RFQ). For non-SL closes (manual/emergency),
        #   rfq_params provides a reasonable RFQ close as fallback.
        execution_mode="limit",

        # rfq_params: used for non-SL close paths (manual close, emergencies)
        rfq_params=RFQParams(
            timeout_seconds=15,
            min_improvement_pct=-999,    # accept any quote for emergency close
            fallback_mode="limit",
        ),

        # execution_params: limit open — fair pricing, aggression 0→0.67→1.0
        execution_params=ExecutionParams(phases=[
            # Phase 1: sell at fair price
            ExecutionPhase(
                pricing="fair", fair_aggression=0.0,
                duration_seconds=LIMIT_OPEN_FAIR_SECONDS,
                reprice_interval=LIMIT_OPEN_FAIR_SECONDS,
            ),
            # Phase 2: sell at bid + 33% of fairspread
            # min_price_pct_of_fair: refuse to place if computed price < fair × floor
            ExecutionPhase(
                pricing="fair", fair_aggression=0.67,
                duration_seconds=LIMIT_OPEN_PARTIAL_SECONDS,
                reprice_interval=LIMIT_OPEN_PARTIAL_SECONDS,
                min_price_pct_of_fair=1.0 - MIN_BID_DISCOUNT_PCT / 100.0,
            ),
            # Phase 3: sell at bid — tracks bid every 15s
            # min_price_pct_of_fair: refuse to place if bid < fair × floor
            ExecutionPhase(
                pricing="fair", fair_aggression=1.0,
                duration_seconds=LIMIT_OPEN_BID_SECONDS,
                reprice_interval=15,
                min_price_pct_of_fair=1.0 - MIN_BID_DISCOUNT_PCT / 100.0,
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
    )
