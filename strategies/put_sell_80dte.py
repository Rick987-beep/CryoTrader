"""
Put Sell 80 DTE — BTC Medium-Term OTM Put Selling Strategy

Sells one 80 DTE BTC put option near -0.15 delta every day during 13:00–14:00 UTC.
Up to 90 concurrent open trades (each cohort shares the same monthly expiry).

EMA Filter:
  Entry is blocked when BTC's most recent daily close is below the EMA-20.
  This avoids selling puts into a sustained downtrend.

Open Execution (sell put — limit only, up to ~2.5 min total):
  Phase 1 — Limit at fair (45s).
  Phase 2 — Limit at bid + 33% of spread (45s).
    Skipped if computed price < fair × (1 − MIN_BID_DISCOUNT_PCT%).
  Phase 3 — Limit at bid (60s).
    Skipped if bid < fair × (1 − MIN_BID_DISCOUNT_PCT%).

Take Profit (95% of premium captured):
  TP threshold = fill_price × 0.05.
  Each tick, we compute mid price (bid+ask)/2.  If mid <= TP threshold, close
  via phased limit buy-to-close: 15s at fair → 15s stepping toward ask →
  aggressive at ask.

Stop Loss (mid-price based, 250% of premium):
  SL threshold = fill_price × 3.50 (250% loss).
  If mid >= SL threshold, close via same phased limit buy-to-close.

Expiry Selection:
  Target DTE is 80, but exact-day matching would miss most days since monthly
  expirations are ~30 days apart.  A ±15-day window (dte_min=65, dte_max=95)
  is used so that exactly one monthly expiry is always in range, allowing a
  new entry every calendar day.  The nearest-expiry logic in option_selection
  picks whichever expiry timestamp is closest to 80 days from today.

Expiry Outcome:
  If neither TP nor SL fires, the option expires worthless (full win).

Key differences vs daily_put_sell:
  - DTE 80 (monthly expiry) vs 1 DTE
  - Delta -0.15 vs -0.10
  - QTY 0.1 BTC vs 0.8 BTC
  - TP at 95% (new)
  - SL at 250% vs 70%
  - Entry 13:00 UTC vs 03:00 UTC
  - Check interval 60s vs 15s (80 DTE options move slowly)
  - Max concurrent 90 (monthly expiry clustering)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from ema_filter import ema20_filter
from market_data import get_btc_index_price, get_option_market_data
from option_selection import LegSpec
from strategy import (
    StrategyConfig,
    time_window,
)
from trade_lifecycle import RFQParams
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
# Overridable via PARAM_* env vars (set by slot .toml config at deploy time).

import os as _os
def _p(name, default, cast=float):
    """Read PARAM_<NAME> from env, falling back to default."""
    val = _os.getenv(f"PARAM_{name}", str(default))
    return int(float(val)) if cast is int else cast(val)

# Structure
QTY = _p("QTY", 0.1)                              # BTC per leg
TARGET_DELTA = _p("TARGET_DELTA", -0.15)           # OTM put delta target
DTE = _p("DTE", 80, int)                           # ~80 days to expiry (monthly)

# Scheduling — UTC hours
ENTRY_HOUR_START = _p("ENTRY_HOUR_START", 13, int) # Open window: 13:00 UTC
ENTRY_HOUR_END   = _p("ENTRY_HOUR_END",   14, int) # Close window: 14:00 UTC

# Risk
TAKE_PROFIT_PCT = _p("TAKE_PROFIT_PCT", 95, int)   # 95% of premium captured
STOP_LOSS_PCT   = _p("STOP_LOSS_PCT",  250, int)   # 250% loss of premium collected

# Limit open — phased execution (same timings as daily_put_sell)
LIMIT_OPEN_FAIR_SECONDS    = _p("LIMIT_OPEN_FAIR_SECONDS",    45, int)
LIMIT_OPEN_PARTIAL_SECONDS = _p("LIMIT_OPEN_PARTIAL_SECONDS", 45, int)
LIMIT_OPEN_BID_SECONDS     = _p("LIMIT_OPEN_BID_SECONDS",     60, int)

# Liquidity guard — minimum acceptable open fill price
MIN_BID_DISCOUNT_PCT = _p("MIN_BID_DISCOUNT_PCT", 17)

# TP/SL close — phased limit buy-to-close (no RFQ)
TP_CLOSE_FAIR_SECONDS = _p("TP_CLOSE_FAIR_SECONDS", 15, int)
TP_CLOSE_STEP_SECONDS = _p("TP_CLOSE_STEP_SECONDS", 15, int)
TP_CLOSE_AGG_SECONDS  = _p("TP_CLOSE_AGG_SECONDS",  60, int)

SL_CLOSE_FAIR_SECONDS = _p("SL_CLOSE_FAIR_SECONDS", 15, int)
SL_CLOSE_STEP_SECONDS = _p("SL_CLOSE_STEP_SECONDS", 15, int)
SL_CLOSE_AGG_SECONDS  = _p("SL_CLOSE_AGG_SECONDS",  60, int)

# Operational
CHECK_INTERVAL  = _p("CHECK_INTERVAL",  30, int)   # 30s — safe up to ~80 concurrent positions (cache TTL=30s)
MAX_CONCURRENT  = _p("MAX_CONCURRENT",  90, int)   # Monthly expiry clustering


# ─── Option Price Snapshot ──────────────────────────────────────────────────

def get_option_prices(symbol: str) -> Optional[dict]:
    """
    Fetch bid, ask, mid, and mark for an option symbol.

    Mid price hierarchy (never falls back to mark price):
      - Two-sided quote: mid = (bid + ask) / 2
      - Ask only (no bids — common for near-worthless deep OTM options):
        mid = ask  (the executable buy price)
      - No ask: returns None — TP/SL check is skipped this tick.

    Returns dict with keys: bid, ask, mid, mark.
    """
    mkt = get_option_market_data(symbol)
    if not mkt:
        return None

    bid  = float(mkt.get('bid',  0) or 0)
    ask  = float(mkt.get('ask',  0) or 0)
    mark = float(mkt.get('mark_price', 0) or 0)

    if ask <= 0:
        return None  # no executable price available

    mid = (bid + ask) / 2.0 if bid > 0 else ask

    return {
        'bid':  bid if bid > 0 else None,
        'ask':  ask,
        'mid':  mid,
        'mark': mark,
    }


# ─── Exit Condition: Take Profit ────────────────────────────────────────────
# For a short put, we profit when the option price falls (time decay / BTC rises).
# TP fires when mid price drops to fill_price × (1 - TAKE_PROFIT_PCT/100),
# meaning we have captured TAKE_PROFIT_PCT% of the premium sold.

def _mid_price_tp():
    """
    Exit condition: mid-price based take profit.

    TP threshold = fill_price × (1 - TAKE_PROFIT_PCT/100).
    Triggers when mid price (bid+ask)/2 <= TP threshold.
    On trigger, configures phased limit buy-to-close.
    """
    label = f"mid_price_tp({TAKE_PROFIT_PCT}%)"

    def _check(account, trade) -> bool:
        leg = trade.open_legs[0] if trade.open_legs else None
        if not leg or not leg.fill_price:
            return False

        tp_threshold = trade.metadata.get("tp_threshold")
        if tp_threshold is None:
            tp_threshold = float(leg.fill_price) * (1.0 - TAKE_PROFIT_PCT / 100.0)
            trade.metadata["tp_threshold"] = tp_threshold

        px = get_option_prices(leg.symbol)
        if not px:
            return False

        triggered = px['mid'] <= tp_threshold
        if triggered:
            captured_pct = (float(leg.fill_price) - px['mid']) / float(leg.fill_price) * 100
            mid_source = "ask-only" if not px['bid'] else "mid"
            logger.info(
                f"[{trade.id}] {label} TRIGGERED: {mid_source}=${px['mid']:.4f} "
                f"<= threshold=${tp_threshold:.4f} "
                f"(fill=${leg.fill_price:.4f}, captured={captured_pct:.1f}%)"
            )
            trade.execution_mode = "limit"
            trade.metadata["tp_triggered"]    = True
            trade.metadata["tp_triggered_at"] = time.time()

        return triggered

    _check.__name__ = label
    return _check


# ─── Exit Condition: Stop Loss ───────────────────────────────────────────────
# SL fires when mid price reaches fill_price × (1 + STOP_LOSS_PCT/100),
# meaning we'd lose STOP_LOSS_PCT% of the premium we collected.

def _mid_price_sl():
    """
    Exit condition: mid-price based stop loss.

    SL threshold = fill_price × (1 + STOP_LOSS_PCT/100).
    Triggers when mid price (bid+ask)/2 >= SL threshold.
    On trigger, configures phased limit buy-to-close.
    """
    label = f"mid_price_sl({STOP_LOSS_PCT}%)"

    def _check(account, trade) -> bool:
        leg = trade.open_legs[0] if trade.open_legs else None
        if not leg or not leg.fill_price:
            return False

        sl_threshold = trade.metadata.get("sl_threshold")
        if sl_threshold is None:
            sl_threshold = float(leg.fill_price) * (1.0 + STOP_LOSS_PCT / 100.0)
            trade.metadata["sl_threshold"] = sl_threshold

        px = get_option_prices(leg.symbol)
        if not px:
            return False

        triggered = px['mid'] >= sl_threshold
        if triggered:
            loss_pct = (px['mid'] - float(leg.fill_price)) / float(leg.fill_price) * 100
            logger.info(
                f"[{trade.id}] {label} TRIGGERED: mid=${px['mid']:.4f} "
                f">= threshold=${sl_threshold:.4f} "
                f"(fill=${leg.fill_price:.4f}, loss={loss_pct:.1f}%)"
            )
            logger.info(
                f"[{trade.id}] SL prices: "
                f"bid=${px['bid']:.4f}  ask=${px['ask']:.4f}  "
                f"mid=${px['mid']:.4f}  mark=${px['mark']:.4f}"
            )
            trade.execution_mode = "limit"
            trade.metadata["sl_triggered"]    = True
            trade.metadata["sl_triggered_at"] = time.time()

        return triggered

    _check.__name__ = label
    return _check


# ─── Trade Callbacks ────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """Called when the short put trade is opened."""
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price

    leg     = trade.open_legs[0] if trade.open_legs else None
    premium = leg.fill_price if leg and leg.fill_price else 0

    px = get_option_prices(leg.symbol) if leg else None
    mid_at_open = None
    if px:
        mid_at_open = px['mid']
        trade.metadata["mid_at_open"] = mid_at_open
        trade.metadata["bid_at_open"] = px['bid']
        trade.metadata["ask_at_open"] = px['ask']

    if premium and premium > 0:
        trade.metadata["sl_threshold"] = float(premium) * (1.0 + STOP_LOSS_PCT / 100.0)
        trade.metadata["tp_threshold"] = float(premium) * (1.0 - TAKE_PROFIT_PCT / 100.0)

    mark_at_open = px['mark'] if px else None
    if mark_at_open:
        trade.metadata[f"mark_at_open_{leg.symbol}"] = mark_at_open

    sl_threshold = trade.metadata.get("sl_threshold")
    tp_threshold = trade.metadata.get("tp_threshold")

    logger.info(
        f"[PutSell80DTE] Opened: SELL {leg.symbol if leg else '?'} "
        f"@ ${premium:.4f}  |  mid=${mid_at_open:.4f}  |  "
        f"mark=${mark_at_open:.4f}  |  "
        f"TP@=${tp_threshold:.4f}  SL@=${sl_threshold:.4f}  |  "
        f"BTC=${index_price:,.0f}"
    )
    if px:
        logger.info(
            f"[PutSell80DTE] Entry prices: "
            f"bid=${px['bid']:.4f}  ask=${px['ask']:.4f}  "
            f"mid=${px['mid']:.4f}  mark=${px['mark']:.4f}"
        )

    # Telegram notification
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    duration_s = int(trade.opened_at - trade.created_at) if trade.opened_at and trade.created_at else 0

    if duration_s <= LIMIT_OPEN_FAIR_SECONDS:
        phase_label = "Phase 1 (at fair)"
    elif duration_s <= LIMIT_OPEN_FAIR_SECONDS + LIMIT_OPEN_PARTIAL_SECONDS:
        phase_label = "Phase 2 (stepped)"
    else:
        phase_label = "Phase 3 (at bid)"

    bid = px['bid'] if px else 0
    ask = px['ask'] if px else 0
    mid = px['mid'] if px else 0

    collected  = float(premium) * float(leg.filled_qty) if leg and leg.filled_qty else 0.0
    vs_mid     = f"vs mid {(premium - mid_at_open) / mid_at_open * 100:+.1f}%" if mid_at_open else ""
    vs_bid     = f"vs bid {(premium - bid) / bid * 100:+.1f}%" if bid else ""
    distances  = "  ·  ".join(x for x in [vs_mid, vs_bid] if x)

    try:
        get_notifier().send(
            f"📉 <b>Put Sell 80 DTE — Trade Opened</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"SELL {leg.filled_qty}\u00d7 {leg.symbol}\n\n"
            f"Collected: <b>${collected:.2f}</b>  ({leg.filled_qty}\u00d7 ${premium:.4f})\n"
            f"{phase_label}  ·  {duration_s}s  ·  {distances}\n\n"
            f"Prices at open:\n"
            f"  mark=${mark_at_open or 0:.4f}  mid=${mid:.4f}\n"
            f"  bid=${bid:.4f}  ask=${ask:.4f}\n\n"
            f"TP @ ${tp_threshold:.4f}  |  SL @ ${sl_threshold:.4f}\n\n"
            f"BTC index: ${index_price:,.0f}" if index_price else "BTC index: N/A"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """Called when the short put trade is closed (TP, SL, or expiry)."""
    pnl        = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    roi        = (pnl / abs(entry_cost) * 100) if entry_cost else 0.0
    hold_seconds = trade.hold_seconds or 0

    exit_reason = "unknown"
    if trade.metadata.get("tp_triggered"):
        exit_reason = f"TP ({TAKE_PROFIT_PCT}% captured, mid-price)"
    elif trade.metadata.get("sl_triggered"):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss, mid-price)"
    elif pnl <= -(abs(entry_cost) * STOP_LOSS_PCT / 100):
        exit_reason = f"SL ({STOP_LOSS_PCT}% loss)"
    elif trade.metadata.get("expiry_settled"):
        exit_reason = "expiry (worthless)"
    elif pnl > 0:
        exit_reason = "profit"

    logger.info(
        f"[PutSell80DTE] Closed: {trade.id}  |  PnL: ${pnl:+.4f}  |  "
        f"ROI: {roi:+.1f}%  |  Hold: {hold_seconds/3600:.1f}h  |  "
        f"Exit: {exit_reason}"
    )

    ts       = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji    = "✅" if pnl >= 0 else "❌"

    leg           = trade.open_legs[0] if trade.open_legs else None
    entry_price   = float(leg.fill_price) if leg and leg.fill_price else 0
    sl_threshold  = trade.metadata.get("sl_threshold")
    tp_threshold  = trade.metadata.get("tp_threshold")

    if trade.metadata.get("tp_triggered"):
        trigger_text = (
            f"Trigger: <b>Take Profit</b>\n"
            f"TP threshold: ${tp_threshold:.4f} ({TAKE_PROFIT_PCT}% on ${entry_price:.4f} entry)"
            if tp_threshold else "Trigger: <b>Take Profit</b>"
        )
    elif trade.metadata.get("sl_triggered"):
        trigger_text = (
            f"Trigger: <b>Stop Loss</b>\n"
            f"SL threshold: ${sl_threshold:.4f} ({STOP_LOSS_PCT}% loss on ${entry_price:.4f} entry)"
            if sl_threshold else "Trigger: <b>Stop Loss</b>"
        )
    elif exit_reason.startswith("expiry"):
        trigger_text = "Trigger: <b>Expiry</b> (option expired worthless)"
    else:
        trigger_text = f"Trigger: <b>{exit_reason}</b>"

    close_fill   = None
    close_symbol = None
    if trade.close_legs and trade.close_legs[0].fill_price:
        close_fill   = float(trade.close_legs[0].fill_price)
        close_symbol = trade.close_legs[0].symbol
    elif leg:
        close_symbol = leg.symbol

    px         = get_option_prices(close_symbol) if close_symbol else None
    price_text = ""
    if px:
        price_text = (
            f"\nPrices at close:\n"
            f"  mark=${px['mark']:.4f}  mid=${px['mid']:.4f}\n"
            f"  bid=${px['bid']:.4f}  ask=${px['ask']:.4f}"
        )

    fill_vs_mid = ""
    if close_fill and px and px['mid'] > 0:
        diff     = close_fill - px['mid']
        diff_pct = diff / px['mid'] * 100
        fill_vs_mid = f"\nFill vs mid: ${close_fill:.4f} vs ${px['mid']:.4f} ({diff_pct:+.1f}%)"

    close_qty = (
        (trade.close_legs[0].filled_qty if (trade.close_legs and trade.close_legs[0].filled_qty) else None)
        or (leg.filled_qty if leg else '?')
    )

    close_exec = ""
    if trade.metadata.get("tp_triggered"):
        tp_triggered_at = trade.metadata.get("tp_triggered_at")
        if tp_triggered_at and trade.closed_at:
            close_duration = trade.closed_at - float(tp_triggered_at)
            if close_duration < TP_CLOSE_FAIR_SECONDS:
                close_exec = "Execution: TP close — Phase 1 (at fair)"
            elif close_duration < TP_CLOSE_FAIR_SECONDS + TP_CLOSE_STEP_SECONDS:
                close_exec = "Execution: TP close — Phase 2 (stepped)"
            else:
                close_exec = "Execution: TP close — Phase 3 (at ask)"
        else:
            close_exec = "Execution: TP close (phased limit)"
    elif trade.metadata.get("sl_triggered"):
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

    close_index = get_btc_index_price(use_cache=False)
    idx_text    = f"BTC index: ${close_index:,.0f}" if close_index else "BTC index: N/A"

    # Fee data from FillResult (captured by LifecycleEngine)
    open_fees = float(trade.open_fees) if trade.open_fees else 0.0
    close_fees = float(trade.close_fees) if trade.close_fees else 0.0
    total_fees = open_fees + close_fees
    net_pnl = pnl - total_fees
    idx_for_fees = close_index or 0.0

    try:
        close_exec_line = f"{close_exec}\n" if close_exec else ""
        fee_line = ""
        if total_fees > 0:
            fee_line = (
                f"\nFees: {total_fees:.6f} (${total_fees * idx_for_fees:,.2f})  "
                f"[open {open_fees:.6f} + close {close_fees:.6f}]\n"
            )

        get_notifier().send(
            f"{emoji} <b>Put Sell 80 DTE — Trade Closed</b>\n\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"BUY {close_qty}\u00d7 {close_symbol}\n"
            f"{trigger_text}\n"
            f"{close_exec_line}"
            f"\nGross PnL: ${pnl:+.2f} ({roi:+.1f}%)\n"
            f"{fee_line}"
            f"Net PnL: <b>${net_pnl:+.2f}</b>\n"
            f"Hold: {hold_seconds/3600:.1f}h\n"
            f"{price_text}"
            f"{fill_vs_mid}\n\n"
            f"{idx_text}"
        )
    except Exception:
        pass


# ─── Strategy Factory ────────────────────────────────────────────────────────

def put_sell_80dte() -> StrategyConfig:
    """
    BTC put selling strategy — 80 DTE, delta -0.15, TP 95%, SL 250%.

    Sells one OTM put per day at 13:00 UTC.  Blocked when BTC daily close
    is below EMA-20.  Up to 90 concurrent trades (monthly expiry clustering).
    """
    return StrategyConfig(
        name="put_sell_80dte",

        # ── What to trade ────────────────────────────────────────────
        legs=[
            LegSpec(
                option_type="P",
                side="sell",
                qty=QTY,
                strike_criteria={"type": "delta", "value": TARGET_DELTA},
                # dte_min/dte_max: ±15 days around target DTE.
                # Monthly expirations are ~30 days apart, so this window always
                # captures exactly one expiry and allows a new entry every day.
                # The nearest-DTE logic in option_selection.py then picks the
                # expiry whose timestamp is closest to DTE days from today.
                expiry_criteria={"dte": DTE, "dte_min": DTE - 15, "dte_max": DTE + 15},
            ),
        ],

        # ── When to enter ────────────────────────────────────────────
        entry_conditions=[
            time_window(ENTRY_HOUR_START, ENTRY_HOUR_END),
            ema20_filter(),   # block when BTC daily close < EMA-20
        ],

        # ── When to exit ─────────────────────────────────────────────
        # TP: 95% of premium captured (mid price drops to 5% of entry).
        # SL: 250% loss (mid price rises to 3.5× entry).
        # Expiry: option expires worthless → full premium captured.
        exit_conditions=[
            _mid_price_tp(),
            _mid_price_sl(),
        ],

        # ── How to execute ───────────────────────────────────────────
        execution_mode="limit",

        rfq_params=RFQParams(
            timeout_seconds=15,
            min_improvement_pct=-999,
            fallback_mode="limit",
        ),

        execution_profile="passive_open_3phase",

        # ── Operational limits ───────────────────────────────────────
        max_concurrent_trades=MAX_CONCURRENT,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
