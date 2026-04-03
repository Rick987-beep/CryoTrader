"""
Short Strangle Delta — N-DTE BTC Short Volatility Strategy

Sells an OTM strangle on the Deribit expiry N calendar days ahead (dte=1/2/3).
Legs are selected by target delta rather than a fixed USD offset from ATM:

    - Call leg: strike whose delta is closest to  +DELTA  (e.g. +0.25)
    - Put  leg: strike whose delta is closest to  -DELTA  (e.g. -0.25)

Corresponds directly to the backtester short_strangle_delta strategy.

Exit logic:
    1. Stop-loss    — combined fair value of both legs exceeds
                      combined_premium × (1 + STOP_LOSS_PCT).
    2. Max hold     — position has been open MAX_HOLD_HOURS hours;
                      set MAX_HOLD_HOURS=0 to disable (hold to expiry).
    3. Expiry       — 08:00 UTC on the expiry date; position expires
                      worthless / at intrinsic; no active close needed.

One entry per day; MAX_CONCURRENT = DTE + 1 (e.g. 1DTE → 2 concurrent,
2DTE → 3, 3DTE → 4) to allow the prior day's position to overlap before
expiry settlement.

Open Execution (two phases, total ≤ 60s):
    Phase 1 (30s): both legs placed at fair price (fair = mid if mark off).
    Phase 2 (30s): any remaining unfilled leg re-priced to bid — aggressive
                   fill to eliminate legging risk.

Close Execution — SL triggered (two phases, total ≤ 60s):
    Phase 1 (30s): both open legs bought at fair price.
    Phase 2 (30s): any remaining unfilled leg re-priced to ask — aggressive.

Close Execution — Max-hold / manual (single aggressive phase, 30s):
    Both legs bought at ask in one shot.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from market_data import get_btc_index_price, get_option_details
from option_selection import strangle
from strategy import (
    StrategyConfig,
    max_hold_hours,
    time_window,
)
from trade_execution import ExecutionParams, ExecutionPhase
from trade_lifecycle import RFQParams
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────
# Overridable via PARAM_* env vars (set by slot .toml config at deploy time).

import os as _os
def _p(name, default, cast=float):
    """Read PARAM_<NAME> from env, falling back to default."""
    return cast(_os.getenv(f"PARAM_{name}", str(default)))

# Structure
QTY   = _p("QTY",   1.0)            # contracts per leg (Deribit min=0.1)
DTE   = _p("DTE",   1, int)         # calendar days to target expiry (1, 2, or 3)
DELTA = _p("DELTA", 0.25)           # target absolute delta per leg (e.g. 0.25 → 25Δ)

# Scheduling
ENTRY_HOUR = _p("ENTRY_HOUR", 4, int)        # UTC hour to open (one-hour window)

# Risk
STOP_LOSS_PCT   = _p("STOP_LOSS_PCT",   1.0)       # SL fires when combined fair ≥ premium × (1 + pct)
MAX_HOLD_HOURS  = _p("MAX_HOLD_HOURS",  0, int)    # force close after N hours; 0 = disabled

# Open execution
LIMIT_OPEN_FAIR_SECONDS = _p("LIMIT_OPEN_FAIR_SECONDS", 30, int)   # Phase 1: quote at fair
LIMIT_OPEN_AGG_SECONDS  = _p("LIMIT_OPEN_AGG_SECONDS",  30, int)   # Phase 2: aggressive at bid

# SL close execution
SL_CLOSE_FAIR_SECONDS = _p("SL_CLOSE_FAIR_SECONDS", 30, int)       # Phase 1: buy at fair
SL_CLOSE_AGG_SECONDS  = _p("SL_CLOSE_AGG_SECONDS",  30, int)       # Phase 2: aggressive at ask

# Max-hold close execution
HOLD_CLOSE_AGG_SECONDS = _p("HOLD_CLOSE_AGG_SECONDS", 30, int)     # Single aggressive phase at ask

# Operational
MAX_CONCURRENT = DTE + 1                    # 1DTE→2, 2DTE→3, 3DTE→4 (overlap window)
CHECK_INTERVAL = _p("CHECK_INTERVAL", 15, int)


# ─── Fair Price Helper ──────────────────────────────────────────────────────

def _fair(symbol):
    # type: (str) -> Optional[dict]
    """
    Compute fair price for one option leg in fill_price-native units.

    On Deribit, fill_price is BTC-denominated, so BTC-native prices
    (_mark_price_btc, _best_bid_btc, _best_ask_btc) are used.
    On Coincall, fill_price is USD, so USD prices (markPrice, bid, ask) are used.

    Returns dict with: fair, bid, ask, mark, index_price.
    Returns None if no market data.

    Fair price logic:
      - mark, if mark is within bid/ask spread
      - mid = (bid+ask)/2, if mark is outside spread
      - max(mark, bid), if only bid side exists
      - mark alone, if book is completely empty
    """
    details = get_option_details(symbol)
    if not details:
        return None

    # Deribit exposes BTC-native price fields; Coincall prices are USD-native.
    # Use whichever matches fill_price units so SL comparisons stay consistent.
    if "_mark_price_btc" in details:
        bid  = float(details.get("_best_bid_btc",  0) or 0)
        ask  = float(details.get("_best_ask_btc",  0) or 0)
        mark = float(details.get("_mark_price_btc", 0) or 0)
    else:
        bid  = float(details.get("bid",       0) or 0)
        ask  = float(details.get("ask",       0) or 0)
        mark = float(details.get("markPrice", 0) or 0)

    if bid > 0 and ask > 0:
        fair = mark if bid <= mark <= ask else (bid + ask) / 2
    elif bid > 0:
        fair = max(mark, bid) if mark > 0 else bid
    elif mark > 0:
        fair = mark
    else:
        return None

    index_price = float(details.get("indexPrice", 0) or 0)

    return {
        "fair":        fair,
        "bid":         bid  if bid  > 0 else None,
        "ask":         ask  if ask  > 0 else None,
        "mark":        mark,
        "index_price": index_price,
    }


# ─── Exit Condition: Combined Fair-Price Stop Loss ──────────────────────────

def _combined_sl():
    """
    Exit condition: combined fair-price based stop loss.

    Fires when (call_fair + put_fair) >= combined_fill_premium × (1 + STOP_LOSS_PCT).
    On trigger, configures a phased limit buy-to-close on the trade.
    """
    label = f"combined_fair_sl({STOP_LOSS_PCT:.0%})"

    def _check(account, trade):
        open_legs = trade.open_legs
        if len(open_legs) < 2:
            return False

        call_leg = next((l for l in open_legs if l.symbol.endswith("-C")), None)
        put_leg  = next((l for l in open_legs if l.symbol.endswith("-P")), None)
        if not call_leg or not put_leg:
            return False

        if not call_leg.fill_price or not put_leg.fill_price:
            return False

        sl_threshold = trade.metadata.get("sl_threshold")
        if sl_threshold is None:
            combined_premium = float(call_leg.fill_price) + float(put_leg.fill_price)
            sl_threshold = combined_premium * (1.0 + STOP_LOSS_PCT)
            trade.metadata["sl_threshold"]    = sl_threshold
            trade.metadata["combined_premium"] = combined_premium

        call_fp = _fair(call_leg.symbol)
        put_fp  = _fair(put_leg.symbol)
        if not call_fp or not put_fp:
            return False

        combined_fair = call_fp["fair"] + put_fp["fair"]
        triggered = combined_fair >= sl_threshold

        if triggered:
            combined_premium = trade.metadata.get("combined_premium", 0)
            loss_pct = (combined_fair - combined_premium) / combined_premium * 100 if combined_premium else 0
            logger.info(
                f"[{trade.id}] {label} TRIGGERED: combined_fair={combined_fair:.4f} "
                f">= threshold={sl_threshold:.4f} "
                f"(premium={combined_premium:.4f}, loss={loss_pct:.1f}%)"
            )
            logger.info(
                f"[{trade.id}] SL legs: "
                f"call fair={call_fp['fair']:.4f} bid={call_fp['bid'] or 0:.4f} ask={call_fp['ask'] or 0:.4f} | "
                f"put  fair={put_fp['fair']:.4f} bid={put_fp['bid'] or 0:.4f} ask={put_fp['ask'] or 0:.4f}"
            )
            trade.execution_mode = "limit"
            trade.metadata["sl_triggered"]    = True
            trade.metadata["sl_triggered_at"] = time.time()
            trade.execution_params = ExecutionParams(phases=[
                # Phase 1: buy both legs at fair (passive)
                ExecutionPhase(
                    pricing="fair", fair_aggression=0.0,
                    duration_seconds=SL_CLOSE_FAIR_SECONDS,
                    reprice_interval=SL_CLOSE_FAIR_SECONDS,
                ),
                # Phase 2: aggressive — any unfilled leg bought at ask
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

def _structure_label():
    # type: () -> str
    """Human-readable structure label, e.g. '25Δ strangle (1DTE)'."""
    delta_pct = int(round(DELTA * 100))
    return f"{delta_pct}\u0394 strangle ({DTE}DTE)"


def _on_trade_opened(trade, account):
    # type: (...) -> None
    """Log entry details and send Telegram notification."""
    index_price = get_btc_index_price(use_cache=False)
    if index_price is not None:
        trade.metadata["entry_index_price"] = index_price

    open_legs = trade.open_legs
    call_leg = next((l for l in open_legs if l.symbol.endswith("-C")), None)
    put_leg  = next((l for l in open_legs if l.symbol.endswith("-P")), None)

    call_fill = float(call_leg.fill_price) if call_leg and call_leg.fill_price else 0.0
    put_fill  = float(put_leg.fill_price)  if put_leg  and put_leg.fill_price  else 0.0
    combined_premium = call_fill + put_fill

    if combined_premium > 0:
        sl_threshold = combined_premium * (1.0 + STOP_LOSS_PCT)
        trade.metadata["sl_threshold"]    = sl_threshold
        trade.metadata["combined_premium"] = combined_premium

    # Configure max-hold close execution (aggressive single phase)
    trade.execution_params = ExecutionParams(phases=[
        ExecutionPhase(
            pricing="fair", fair_aggression=1.0,
            duration_seconds=HOLD_CLOSE_AGG_SECONDS,
            reprice_interval=15,
        ),
    ])

    duration_s = int(trade.opened_at - trade.created_at) if trade.opened_at and trade.created_at else 0
    phase_label = "Phase 1 (at fair)" if duration_s <= LIMIT_OPEN_FAIR_SECONDS else "Phase 2 (at bid)"

    call_fp = _fair(call_leg.symbol) if call_leg else None
    put_fp  = _fair(put_leg.symbol)  if put_leg  else None
    call_fair = call_fp["fair"] if call_fp else 0.0
    put_fair  = put_fp["fair"]  if put_fp  else 0.0
    combined_fair = call_fair + put_fair

    vs_fair = (
        f"{(combined_premium - combined_fair) / combined_fair * 100:+.1f}% vs fair"
        if combined_fair > 0 else ""
    )

    structure = _structure_label()
    ts  = datetime.now(timezone.utc).strftime("%H:%M UTC")
    idx = index_price or 0.0

    logger.info(
        f"[ShortStrangleDelta] Opened {structure}: "
        f"CALL {call_leg.symbol if call_leg else '?'} @ {call_fill:.4f} | "
        f"PUT  {put_leg.symbol  if put_leg  else '?'} @ {put_fill:.4f} | "
        f"combined={combined_premium:.4f}  SL@={trade.metadata.get('sl_threshold', 0):.4f}  "
        f"BTC=${idx:,.0f}  {phase_label} {duration_s}s"
    )

    sl_thresh = trade.metadata.get('sl_threshold', 0)
    hold_label = f"{MAX_HOLD_HOURS}h" if MAX_HOLD_HOURS > 0 else "expiry"
    try:
        get_notifier().send(
            f"📉 <b>Short {structure.title()} — Trade Opened</b>\n\n"
            f"Time: {ts}  |  BTC: ${idx:,.0f}\n"
            f"ID: {trade.id}\n\n"
            f"SELL {QTY}\u00d7 {call_leg.symbol if call_leg else '?'}  @ {call_fill:.4f} BTC (${call_fill * idx:,.2f})\n"
            f"SELL {QTY}\u00d7 {put_leg.symbol  if put_leg  else '?'}  @ {put_fill:.4f} BTC (${put_fill * idx:,.2f})\n\n"
            f"Combined premium: <b>{combined_premium:.4f} BTC</b> (${combined_premium * idx:,.2f})  ({vs_fair})\n"
            f"SL threshold: {sl_thresh:.4f} BTC (${sl_thresh * idx:,.2f})  (+{STOP_LOSS_PCT:.0%})\n"
            f"Max hold: {hold_label}  |  {phase_label}  {duration_s}s\n\n"
            f"Fair call: {call_fair:.4f} BTC (${call_fair * idx:,.2f})  |  "
            f"Fair put: {put_fair:.4f} BTC (${put_fair * idx:,.2f})  |  "
            f"Fair combined: {combined_fair:.4f} BTC (${combined_fair * idx:,.2f})\n"
            f"Equity: ${account.equity:,.2f}  |  "
            f"Avail: ${account.available_margin:,.2f}"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account):
    # type: (...) -> None
    """Log close details and send Telegram notification."""
    pnl = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    combined_premium = trade.metadata.get("combined_premium", 0.0)
    roi = (pnl / abs(combined_premium) * 100) if combined_premium else 0.0
    hold_seconds = trade.hold_seconds or 0

    if trade.metadata.get("sl_triggered"):
        exit_label = f"Stop Loss ({STOP_LOSS_PCT:.0%})"
    elif MAX_HOLD_HOURS > 0 and hold_seconds >= MAX_HOLD_HOURS * 3600:
        exit_label = f"Max Hold ({MAX_HOLD_HOURS}h)"
    elif trade.metadata.get("expiry_settled"):
        exit_label = "Expiry"
    else:
        exit_label = "closed"

    logger.info(
        f"[ShortStrangleDelta] Closed: {trade.id}  |  PnL: ${pnl:+.4f}  |  "
        f"ROI: {roi:+.1f}%  |  Hold: {hold_seconds / 60:.1f}min  |  Exit: {exit_label}"
    )

    ts    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "\u2705" if pnl >= 0 else "\u274c"
    idx   = get_btc_index_price(use_cache=False) or 0.0

    open_legs  = trade.open_legs
    call_open  = next((l for l in open_legs if l.symbol.endswith("-C")), None)
    put_open   = next((l for l in open_legs if l.symbol.endswith("-P")), None)
    call_fill_open = float(call_open.fill_price) if call_open and call_open.fill_price else 0.0
    put_fill_open  = float(put_open.fill_price)  if put_open  and put_open.fill_price  else 0.0

    close_legs  = trade.close_legs or []
    call_close  = next((l for l in close_legs if l.symbol.endswith("-C")), None)
    put_close   = next((l for l in close_legs if l.symbol.endswith("-P")), None)
    call_fill_close = float(call_close.fill_price) if call_close and call_close.fill_price else 0.0
    put_fill_close  = float(put_close.fill_price)  if put_close  and put_close.fill_price  else 0.0
    combined_close  = call_fill_close + put_fill_close

    call_sym = call_close.symbol if call_close else (call_open.symbol if call_open else None)
    put_sym  = put_close.symbol  if put_close  else (put_open.symbol  if put_open  else None)
    call_fp  = _fair(call_sym) if call_sym else None
    put_fp   = _fair(put_sym)  if put_sym  else None
    combined_fair_now = (
        (call_fp["fair"] if call_fp else 0.0) + (put_fp["fair"] if put_fp else 0.0)
    )

    structure = _structure_label()

    try:
        sl_line = ""
        if trade.metadata.get("sl_triggered"):
            sl_thresh = trade.metadata.get("sl_threshold", 0)
            sl_line = (
                f"SL threshold: {sl_thresh:.4f} BTC (${sl_thresh * idx:,.2f})  |  "
                f"Fair now: {combined_fair_now:.4f} BTC (${combined_fair_now * idx:,.2f})\n"
            )

        get_notifier().send(
            f"{emoji} <b>Short {structure.title()} — Trade Closed</b>\n\n"
            f"Time: {ts}  |  BTC: ${idx:,.0f}\n"
            f"ID: {trade.id}  |  Hold: {hold_seconds / 60:.1f} min\n\n"
            f"Trigger: <b>{exit_label}</b>\n"
            f"{sl_line}"
            f"\nOpen:  CALL {call_fill_open:.4f} BTC  PUT {put_fill_open:.4f} BTC  "
            f"\u2192  {combined_premium:.4f} BTC (${combined_premium * idx:,.2f})\n"
            f"Close: CALL {call_fill_close:.4f} BTC  PUT {put_fill_close:.4f} BTC  "
            f"\u2192  {combined_close:.4f} BTC (${combined_close * idx:,.2f})\n\n"
            f"PnL: <b>${pnl:+.2f}</b>  ({roi:+.1f}%)\n"
            f"Equity: ${account.equity:,.2f}"
        )
    except Exception:
        pass


# ─── Open Execution Params ───────────────────────────────────────────────────
#
# Phase 1 (30s): quote both legs at fair price.
# Phase 2 (30s): any unfilled leg repriced to bid — eliminates legging risk.

_OPEN_PARAMS = ExecutionParams(phases=[
    ExecutionPhase(
        pricing="fair", fair_aggression=0.0,
        duration_seconds=LIMIT_OPEN_FAIR_SECONDS,
        reprice_interval=LIMIT_OPEN_FAIR_SECONDS,
    ),
    ExecutionPhase(
        pricing="fair", fair_aggression=1.0,
        duration_seconds=LIMIT_OPEN_AGG_SECONDS,
        reprice_interval=15,
    ),
])


# ─── Strategy Factory ────────────────────────────────────────────────────────

def short_strangle_delta() -> StrategyConfig:
    """
    Short N-DTE strangle selected by delta — sell combined premium,
    exit on SL, optional max-hold, or expiry.

    Backtester2 param grid: dte=[1,2,3], delta=[0.1..0.3],
    entry_hour=[1,4,8,12,16,20], stop_loss_pct=[0.75..3.5],
    max_hold_hours=[0,12,36,60].
    """
    exit_conditions = [_combined_sl()]
    if MAX_HOLD_HOURS > 0:
        exit_conditions.append(max_hold_hours(MAX_HOLD_HOURS))

    return StrategyConfig(
        name="short_strangle_delta",

        # ── What to trade ─────────────────────────────────────────────
        legs=strangle(
            qty=QTY,
            call_delta=+DELTA,
            put_delta=-DELTA,
            dte=DTE,
            side="sell",
        ),

        # ── When to enter ─────────────────────────────────────────────
        entry_conditions=[
            time_window(ENTRY_HOUR, ENTRY_HOUR + 4),
        ],

        # ── When to exit ──────────────────────────────────────────────
        exit_conditions=exit_conditions,

        # ── How to execute ────────────────────────────────────────────
        execution_mode="limit",
        execution_params=_OPEN_PARAMS,

        # RFQ fallback for emergency manual closes only
        rfq_params=RFQParams(
            timeout_seconds=15,
            min_improvement_pct=-999,
            fallback_mode="limit",
        ),

        # ── Operational limits ────────────────────────────────────────
        max_concurrent_trades=MAX_CONCURRENT,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ─────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
