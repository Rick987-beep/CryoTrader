"""
Short Strangle Delta TP — N-DTE BTC Short Volatility Strategy with Take Profit

Extends short_strangle_delta with:

    1. Take-profit exit — close when combined ask cost has fallen enough
       that profit_ratio >= TAKE_PROFIT_PCT.  Uses raw ask prices (no fair
       floor) to match the backtester behaviour.

    2. min_otm_pct — if the delta-selected strike is closer to ATM than
       MIN_OTM_PCT, push to the nearest qualifying OTM strike.  Blocks
       entry entirely if no qualifying strike exists.

All other behaviour (delta selection, entry window, SL, max-hold, expiry,
weekend filter, execution phases) is identical to ShortStrangleDelta.

Exit logic:
    1. Take-profit  — combined ask drops enough that
                      (premium - combined_ask) / premium >= TAKE_PROFIT_PCT.
    2. Stop-loss    — combined fair value of both legs exceeds
                      combined_premium × (1 + STOP_LOSS_PCT).
    3. Max hold     — position has been open MAX_HOLD_HOURS hours;
                      set MAX_HOLD_HOURS=0 to disable (hold to expiry).
    4. Expiry       — 08:00 UTC on the expiry date.

Open Execution (two phases, total ≤ 60s):
    Phase 1 (30s): both legs placed at fair price.
    Phase 2 (30s): any remaining unfilled leg re-priced to bid — aggressive.

Close Execution — SL/TP triggered (two phases, total ≤ 90s):
    Phase 1 (30s): both open legs bought at fair price.
    Phase 2 (60s): any remaining unfilled leg re-priced to ask — aggressive.

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
    weekday_filter,
)
from execution.profiles import get_profile
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
DELTA = _p("DELTA", 0.1)            # target absolute delta per leg (e.g. 0.25 → 25Δ)

# Scheduling
ENTRY_HOUR      = _p("ENTRY_HOUR",      18,  int)   # UTC hour to open (one-hour window)
WEEKEND_FILTER  = _p("WEEKEND_FILTER",  1,   int)   # 1 = block new opens on Sat/Sun (default on)

# Risk
STOP_LOSS_PCT    = _p("STOP_LOSS_PCT",    3.0)       # SL fires when combined fair ≥ premium × (1 + pct)
TAKE_PROFIT_PCT  = _p("TAKE_PROFIT_PCT",  0.0)       # TP: close when profit ratio ≥ this (0 = disabled)
MAX_HOLD_HOURS   = _p("MAX_HOLD_HOURS",   48, int)   # force close after N hours; 0 = disabled
MIN_OTM_PCT      = _p("MIN_OTM_PCT",      3.0)       # min OTM distance %; 0 = disabled

# Open execution
LIMIT_OPEN_FAIR_SECONDS = _p("LIMIT_OPEN_FAIR_SECONDS", 30, int)   # Phase 1: quote at fair
LIMIT_OPEN_AGG_SECONDS  = _p("LIMIT_OPEN_AGG_SECONDS",  30, int)   # Phase 2: aggressive at bid

# SL/TP close execution
CLOSE_FAIR_SECONDS = _p("CLOSE_FAIR_SECONDS", 30, int)       # Phase 1: buy at fair
CLOSE_AGG_SECONDS  = _p("CLOSE_AGG_SECONDS",  60, int)       # Phase 2: aggressive at ask

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
    """
    details = get_option_details(symbol)
    if not details:
        return None

    # ── Denomination selection ──────────────────────────────────────────────
    # The presence of '_mark_price_btc' identifies the Deribit adapter, which
    # uses BTC as the fill-price denomination.  Coincall uses USD.  Mixing
    # denominations here would produce SL/TP thresholds that are ~70,000×
    # too large (USD value treated as BTC), so never skip this branch check.
    if "_mark_price_btc" in details:
        # Deribit: all prices are BTC-native (correct fill denomination).
        bid  = float(details.get("_best_bid_btc",   0) or 0)
        ask  = float(details.get("_best_ask_btc",   0) or 0)
        mark = float(details.get("_mark_price_btc", 0) or 0)
    else:
        # Coincall: all prices are USD-native.
        bid  = float(details.get("bid",       0) or 0)
        ask  = float(details.get("ask",       0) or 0)
        mark = float(details.get("markPrice", 0) or 0)

    # ── Compute fair value ────────────────────────────────────────────────────
    # This fair value is used for SL/TP condition DETECTION only — it is NOT
    # used for order price computation (that is handled by LimitFillManager).
    #
    # Missing-data cases (ordered by data availability):
    #   bid > 0 and ask > 0 — full book: mark if inside spread, else midpoint.
    #   bid > 0, ask = 0    — ask missing: max(mark, bid) as conservative fair.
    #   bid = 0, ask > 0    — bid missing (deep-OTM near expiry, no buyers):
    #                          min(mark, ask).  TP uses fp["ask"] directly, so
    #                          fair here only affects the SL check.  min() avoids
    #                          inflating the SL level when mark < ask.
    #   mark > 0 only       — no book at all: trust the mark.
    #   all zero            — return None; caller skips the exit check entirely.
    if bid > 0 and ask > 0:
        fair = mark if bid <= mark <= ask else (bid + ask) / 2
    elif bid > 0:
        fair = max(mark, bid) if mark > 0 else bid
    elif ask > 0:
        # Bid absent — deep-OTM or expiring leg with no buyers.
        # min() ensures fair does not overshoot the ask (avoids premature SL).
        fair = min(mark, ask) if mark > 0 else ask
    elif mark > 0:
        fair = mark
    else:
        # No pricing data at all — cannot derive a fair value.
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

        return triggered

    _check.__name__ = label
    return _check


# ─── Exit Condition: Combined Ask-Price Take Profit ─────────────────────────

def _combined_tp():
    """
    Exit condition: combined ask-price based take profit.

    Fires when (premium - combined_ask) / premium >= TAKE_PROFIT_PCT.
    Uses raw ask prices — no mark/fair floor — matching backtester behaviour.
    On trigger, configures a phased limit buy-to-close on the trade.
    """
    if TAKE_PROFIT_PCT <= 0:
        # Disabled — return a no-op condition
        def _noop(account, trade):
            return False
        _noop.__name__ = "combined_ask_tp(disabled)"
        return _noop

    label = f"combined_ask_tp({TAKE_PROFIT_PCT:.0%})"

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

        combined_premium = trade.metadata.get("combined_premium")
        if combined_premium is None:
            combined_premium = float(call_leg.fill_price) + float(put_leg.fill_price)
            trade.metadata["combined_premium"] = combined_premium

        if combined_premium <= 0:
            return False

        call_fp = _fair(call_leg.symbol)
        put_fp  = _fair(put_leg.symbol)
        if not call_fp or not put_fp:
            return False

        # Use ask prices only — skip tick if ask is missing
        call_ask = call_fp.get("ask")
        put_ask  = put_fp.get("ask")
        if not call_ask or not put_ask:
            return False

        combined_ask = call_ask + put_ask
        profit_ratio = (combined_premium - combined_ask) / combined_premium
        triggered = profit_ratio >= TAKE_PROFIT_PCT

        if triggered:
            logger.info(
                f"[{trade.id}] {label} TRIGGERED: combined_ask={combined_ask:.4f} "
                f"profit_ratio={profit_ratio:.2%} >= {TAKE_PROFIT_PCT:.0%} "
                f"(premium={combined_premium:.4f})"
            )
            logger.info(
                f"[{trade.id}] TP legs: "
                f"call ask={call_ask:.4f} | put ask={put_ask:.4f}"
            )
            trade.execution_mode = "limit"
            trade.metadata["tp_triggered"]    = True
            trade.metadata["tp_triggered_at"] = time.time()

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

    # Configure max-hold close execution — override profile for max-hold exit.
    # SL/TP exits use the profile's standard close_phases (from delta_strangle_2phase).
    # Max-hold exit uses a dedicated 1-phase aggressive close profile.
    _max_hold_profile = get_profile("max_hold_close_1phase")
    trade.metadata["_max_hold_close_profile"] = _max_hold_profile

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
        f"[ShortStrangleDeltaTp] Opened {structure}: "
        f"CALL {call_leg.symbol if call_leg else '?'} @ {call_fill:.4f} | "
        f"PUT  {put_leg.symbol  if put_leg  else '?'} @ {put_fill:.4f} | "
        f"combined={combined_premium:.4f}  SL@={trade.metadata.get('sl_threshold', 0):.4f}  "
        f"BTC=${idx:,.0f}  {phase_label} {duration_s}s"
    )

    sl_thresh = trade.metadata.get('sl_threshold', 0)
    hold_label = f"{MAX_HOLD_HOURS}h" if MAX_HOLD_HOURS > 0 else "expiry"
    tp_label = f"{TAKE_PROFIT_PCT:.0%}" if TAKE_PROFIT_PCT > 0 else "off"
    otm_label = f"{MIN_OTM_PCT:.0f}%" if MIN_OTM_PCT > 0 else "off"
    try:
        get_notifier().send(
            f"\U0001f4c9 <b>Short {structure.title()} — Trade Opened</b>\n\n"
            f"Time: {ts}  |  BTC: ${idx:,.0f}\n"
            f"ID: {trade.id}\n\n"
            f"SELL {QTY}\u00d7 {call_leg.symbol if call_leg else '?'}  @ {call_fill:.4f} BTC (${call_fill * idx:,.2f})\n"
            f"SELL {QTY}\u00d7 {put_leg.symbol  if put_leg  else '?'}  @ {put_fill:.4f} BTC (${put_fill * idx:,.2f})\n\n"
            f"Combined premium: <b>{combined_premium:.4f} BTC</b> (${combined_premium * idx:,.2f})  ({vs_fair})\n"
            f"SL threshold: {sl_thresh:.4f} BTC (${sl_thresh * idx:,.2f})  (+{STOP_LOSS_PCT:.0%})\n"
            f"TP: {tp_label}  |  Min OTM: {otm_label}\n"
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
    idx   = get_btc_index_price(use_cache=False) or 0.0
    pnl   = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    pnl_usd = pnl * idx
    combined_premium = trade.metadata.get("combined_premium", 0.0)
    roi = (pnl / abs(combined_premium) * 100) if combined_premium else 0.0
    hold_seconds = trade.hold_seconds or 0

    if trade.metadata.get("tp_triggered"):
        exit_label = f"Take Profit ({TAKE_PROFIT_PCT:.0%})"
    elif trade.metadata.get("sl_triggered"):
        exit_label = f"Stop Loss ({STOP_LOSS_PCT:.0%})"
    elif MAX_HOLD_HOURS > 0 and hold_seconds >= MAX_HOLD_HOURS * 3600:
        exit_label = f"Max Hold ({MAX_HOLD_HOURS}h)"
    elif trade.metadata.get("expiry_settled"):
        exit_label = "Expiry"
    else:
        exit_label = "closed"

    logger.info(
        f"[ShortStrangleDeltaTp] Closed: {trade.id}  |  PnL: ${pnl_usd:+.2f}  |  "
        f"ROI: {roi:+.1f}%  |  Hold: {hold_seconds / 60:.1f}min  |  Exit: {exit_label}"
    )

    ts    = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "\u2705" if pnl >= 0 else "\u274c"

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

    # Fee data from FillResult (captured by LifecycleEngine)
    open_fees = float(trade.open_fees) if trade.open_fees else 0.0
    close_fees = float(trade.close_fees) if trade.close_fees else 0.0
    total_fees = open_fees + close_fees
    net_pnl = pnl - total_fees
    net_pnl_usd = net_pnl * idx

    try:
        detail_line = ""
        if trade.metadata.get("tp_triggered"):
            detail_line = (
                f"TP target: {TAKE_PROFIT_PCT:.0%} of premium  |  "
                f"Fair now: {combined_fair_now:.4f} BTC (${combined_fair_now * idx:,.2f})\n"
            )
        elif trade.metadata.get("sl_triggered"):
            sl_thresh = trade.metadata.get("sl_threshold", 0)
            detail_line = (
                f"SL threshold: {sl_thresh:.4f} BTC (${sl_thresh * idx:,.2f})  |  "
                f"Fair now: {combined_fair_now:.4f} BTC (${combined_fair_now * idx:,.2f})\n"
            )

        fee_line = ""
        if total_fees > 0:
            fee_line = (
                f"\nFees: {total_fees:.6f} BTC (${total_fees * idx:,.2f})  "
                f"[open {open_fees:.6f} + close {close_fees:.6f}]\n"
            )

        get_notifier().send(
            f"{emoji} <b>Short {structure.title()} — Trade Closed</b>\n\n"
            f"Time: {ts}  |  BTC: ${idx:,.0f}\n"
            f"ID: {trade.id}  |  Hold: {hold_seconds / 60:.1f} min\n\n"
            f"Trigger: <b>{exit_label}</b>\n"
            f"{detail_line}"
            f"\nOpen:  CALL {call_fill_open:.4f} BTC  PUT {put_fill_open:.4f} BTC  "
            f"\u2192  {combined_premium:.4f} BTC (${combined_premium * idx:,.2f})\n"
            f"Close: CALL {call_fill_close:.4f} BTC  PUT {put_fill_close:.4f} BTC  "
            f"\u2192  {combined_close:.4f} BTC (${combined_close * idx:,.2f})\n"
            f"{fee_line}"
            f"\nGross PnL: ${pnl_usd:+.2f}  ({roi:+.1f}%)\n"
            f"Net PnL: <b>${net_pnl_usd:+.2f}</b>\n"
            f"Equity: ${account.equity:,.2f}"
        )
    except Exception:
        pass


# ─── Exit Condition: Max Hold with Profile Override ─────────────────────────

def _max_hold_close():
    """
    Exit condition: max hold timer with close profile override.

    Same as strategy.max_hold_hours() but also swaps the execution profile
    to max_hold_close_1phase (single aggressive phase) for faster close.
    """
    if MAX_HOLD_HOURS <= 0:
        def _noop(account, trade):
            return False
        _noop.__name__ = "max_hold_close(disabled)"
        return _noop

    label = f"max_hold_close({MAX_HOLD_HOURS}h)"

    def _check(account, trade):
        hold = trade.hold_seconds
        if hold is None:
            return False
        triggered = hold >= MAX_HOLD_HOURS * 3600
        if triggered:
            logger.info(f"[{trade.id}] {label} triggered: held {hold/3600:.1f}h")
            # Swap to aggressive 1-phase close profile
            max_hold_profile = trade.metadata.get("_max_hold_close_profile")
            if max_hold_profile:
                trade.metadata["_execution_profile"] = max_hold_profile
        return triggered

    _check.__name__ = label
    return _check


# ─── Strategy Factory ────────────────────────────────────────────────────────

def short_strangle_delta_tp() -> StrategyConfig:
    """
    Short N-DTE strangle selected by delta — sell combined premium,
    exit on TP, SL, optional max-hold, or expiry.

    Extends short_strangle_delta with take-profit and min_otm_pct.
    """
    exit_conditions = [_combined_tp(), _combined_sl()]
    if MAX_HOLD_HOURS > 0:
        exit_conditions.append(_max_hold_close())

    return StrategyConfig(
        name="short_strangle_delta_tp",

        # ── What to trade ─────────────────────────────────────────────
        legs=strangle(
            qty=QTY,
            call_delta=+DELTA,
            put_delta=-DELTA,
            dte=DTE,
            side="sell",
            min_otm_pct=MIN_OTM_PCT,
        ),

        # ── When to enter ─────────────────────────────────────────────
        entry_conditions=[
            time_window(ENTRY_HOUR, ENTRY_HOUR + 4),
            *([
                weekday_filter(["mon", "tue", "wed", "thu", "fri"])
            ] if WEEKEND_FILTER else []),
        ],

        # ── When to exit ──────────────────────────────────────────────
        exit_conditions=exit_conditions,

        # ── How to execute ────────────────────────────────────────────
        execution_mode="limit",
        execution_profile="delta_strangle_2phase",

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
