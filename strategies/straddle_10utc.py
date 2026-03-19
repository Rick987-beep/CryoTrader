"""
ATM_Str_fixpnl_Deribit — Daily Long ATM Straddle with Fixed-Dollar TP

Data-driven strategy derived from the Optimal Entry Window analysis
(analysis/optimal_entry_window/).  Top parameter combo from 4-week
backtest (Feb 19 – Mar 19, 2026):

    Structure:  Long ATM straddle (buy call + buy put at nearest strike)
    Entry:      10:00 UTC daily (weekdays only)
    Expiry:     Next-day (08:00 UTC tomorrow ≈ 22h DTE at entry)
    Quantity:   0.1 BTC per leg
    TP:         $1,000 USD net PnL (fixed dollar, not percentage)
    Time exit:  19:00 UTC (9h after entry — hard close)
    Execution:  Two-phase orderbook limit orders
                  Phase 1 (3 min): mark-price quoting, reprice every 30s
                  Phase 2 (3 min): aggressive limits crossing the spread

Exchange: Deribit (BTC-denominated option prices → USD conversion at boundary)

BTC↔USD PnL conversion:
    Deribit option prices are in BTC.  The custom dollar_profit_target exit
    condition uses structure_pnl() which reads position unrealized_pnl,
    already converted to USD by the DeribitAccountAdapter.  Fallback:
    executable_pnl() returns BTC PnL × index_price → USD.

Usage:
    # In main.py STRATEGIES list:
    from strategies import straddle_10utc
    STRATEGIES = [straddle_10utc]
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from option_selection import straddle
from strategy import (
    StrategyConfig,
    # Entry conditions
    time_window,
    min_available_margin_pct,
    # Exit conditions
    time_exit,
)
from trade_execution import ExecutionParams, ExecutionPhase
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# ─── Strategy Parameters ────────────────────────────────────────────────────

# Structure
QTY = 0.1                           # BTC per leg
DTE = "next"                        # Nearest expiry (next-day 08:00 UTC)

# Scheduling — when to open and close (UTC)
OPEN_HOUR = 10                      # Entry at 10:00 UTC
CLOSE_HOUR = 19                     # Hard exit at 19:00 UTC (9h hold)
CLOSE_MINUTE = 0

# Take-profit — fixed dollar amount
TP_USD = 1000.0                     # Close at +$1,000 net USD PnL

# Risk / margin
MIN_MARGIN_PCT = 20                 # Require ≥20% available margin

# Execution — two-phase limit order plan
PHASE1_SECONDS = 180                # Phase 1: 3 min at mark price
PHASE1_REPRICE = 30                 # Reprice every 30s in phase 1
PHASE2_SECONDS = 180                # Phase 2: 3 min aggressive
PHASE2_BUFFER_PCT = 3.0             # Cross spread with 3% buffer
PHASE2_REPRICE = 30                 # Reprice every 30s in phase 2

# Operational
CHECK_INTERVAL = 30                 # Seconds between entry/exit evaluations
STRATEGY_NAME = "ATM_Str_fixpnl_Deribit"


# ─── Execution Configuration ────────────────────────────────────────────────

def _build_execution_params() -> ExecutionParams:
    """Two-phase limit order plan: passive mark → aggressive cross."""
    return ExecutionParams(
        phases=[
            ExecutionPhase(
                pricing="mark",
                duration_seconds=PHASE1_SECONDS,
                reprice_interval=PHASE1_REPRICE,
            ),
            ExecutionPhase(
                pricing="aggressive",
                duration_seconds=PHASE2_SECONDS,
                buffer_pct=PHASE2_BUFFER_PCT,
                reprice_interval=PHASE2_REPRICE,
            ),
        ],
    )


# ─── Custom Entry Condition: Weekday Filter ─────────────────────────────────

def _weekday_only():
    """Entry condition: skip Saturday (5) and Sunday (6)."""
    def _check(account, trade) -> bool:
        return datetime.now(timezone.utc).weekday() < 5
    _check.__name__ = "weekday_only"
    return _check


# ─── Custom Exit Condition: Fixed-Dollar Profit Target ──────────────────────

def _dollar_profit_target(usd_target: float):
    """
    Exit condition: close when structure net PnL reaches +$usd_target.

    PnL source (in priority order):
      1. structure_pnl(account) — uses position unrealized_pnl from
         DeribitAccountAdapter, which is already USD-denominated
         (floating_profit_loss_usd).
      2. executable_pnl() fallback — BTC-native PnL × index_price → USD.
         Used only if structure_pnl returns 0 (position not yet reflected).

    Args:
        usd_target: Dollar profit target (e.g. 1000.0 for $1,000).
    """
    label = f"dollar_tp(${usd_target:.0f})"

    def _check(account, trade) -> bool:
        # Primary: structure_pnl uses account position data (USD on Deribit)
        pnl_usd = trade.structure_pnl(account)

        # Fallback: if position not yet reflected, try executable_pnl
        if pnl_usd == 0.0 and trade.open_legs:
            btc_pnl = trade.executable_pnl()
            if btc_pnl is not None and btc_pnl != 0.0:
                # Convert BTC PnL to USD using current index price
                from market_data import get_btc_index_price
                index_price = get_btc_index_price(use_cache=True)
                if index_price and index_price > 0:
                    pnl_usd = btc_pnl * index_price

        triggered = pnl_usd >= usd_target
        if triggered:
            logger.info(
                f"[{trade.id}] {label} triggered: PnL=${pnl_usd:+,.2f} "
                f"≥ target ${usd_target:,.0f}"
            )
        return triggered

    _check.__name__ = label
    return _check


# ─── Trade Callbacks ────────────────────────────────────────────────────────

def _on_trade_opened(trade, account) -> None:
    """Send Telegram notification when straddle opens."""
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry_cost = trade.total_entry_cost()
    legs_text = "\n".join(
        f"  {leg.side.upper()} {leg.filled_qty}× {leg.symbol} "
        f"@ {leg.fill_price:.6f} BTC"
        for leg in trade.open_legs
        if leg.fill_price is not None
    )

    # Capture entry index price for close reporting
    from market_data import get_btc_index_price
    index_price = get_btc_index_price(use_cache=False)
    if index_price:
        trade.metadata["entry_index_price"] = index_price
        entry_cost_usd = abs(entry_cost) * index_price
    else:
        entry_cost_usd = 0.0

    logger.info(
        f"[{STRATEGY_NAME}] Opened: {trade.id} | "
        f"Entry cost: {entry_cost:.6f} BTC (${entry_cost_usd:,.2f}) | "
        f"BTC=${index_price:,.0f}" if index_price else
        f"[{STRATEGY_NAME}] Opened: {trade.id} | Entry cost: {entry_cost:.6f} BTC"
    )

    try:
        get_notifier().send(
            f"📈 <b>{STRATEGY_NAME} — Trade Opened</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"{legs_text}\n"
            f"Entry cost: {entry_cost:.6f} BTC (${entry_cost_usd:,.2f})\n"
            f"BTC: ${index_price:,.0f}\n" if index_price else ""
            f"TP target: ${TP_USD:,.0f} | Hard close: {CLOSE_HOUR}:00 UTC\n"
            f"Equity: ${account.equity:,.2f}"
        )
    except Exception:
        pass


def _on_trade_closed(trade, account) -> None:
    """Log PnL and send Telegram notification when straddle closes."""
    # Compute PnL in USD
    pnl_btc = trade.realized_pnl if trade.realized_pnl is not None else 0.0
    entry_cost = trade.total_entry_cost()
    hold_seconds = trade.hold_seconds or 0

    # BTC→USD conversion for realized PnL
    from market_data import get_btc_index_price
    index_price = get_btc_index_price(use_cache=False)
    pnl_usd = pnl_btc * index_price if index_price else 0.0
    entry_cost_usd = abs(entry_cost) * index_price if index_price else 0.0
    roi = (pnl_btc / abs(entry_cost) * 100) if entry_cost else 0.0

    # Determine exit reason
    exit_reason = "unknown"
    if pnl_usd >= TP_USD * 0.9:  # within 10% of target → likely TP
        exit_reason = f"TP (${pnl_usd:,.0f})"
    elif hold_seconds >= (CLOSE_HOUR - OPEN_HOUR) * 3600 - 120:
        exit_reason = f"time exit ({CLOSE_HOUR}:00 UTC)"
    elif pnl_usd > 0:
        exit_reason = "profit (other)"
    else:
        exit_reason = f"time exit (PnL ${pnl_usd:+,.0f})"

    logger.info(
        f"[{STRATEGY_NAME}] Closed: {trade.id} | "
        f"PnL: {pnl_btc:+.6f} BTC (${pnl_usd:+,.2f}) | "
        f"ROI: {roi:+.1f}% | Hold: {hold_seconds/60:.1f} min | "
        f"Exit: {exit_reason}"
    )

    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    emoji = "✅" if pnl_usd >= 0 else "❌"
    close_detail = ""
    if trade.close_legs:
        close_detail = "\n".join(
            f"  {leg.side.upper()} {leg.filled_qty}× {leg.symbol} "
            f"@ {leg.fill_price:.6f} BTC"
            for leg in trade.close_legs
            if leg.fill_price is not None
        )
    try:
        get_notifier().send(
            f"{emoji} <b>{STRATEGY_NAME} — Trade Closed</b>\n"
            f"Time: {ts}\n"
            f"ID: {trade.id}\n"
            f"Exit: {exit_reason}\n"
            f"PnL: <b>${pnl_usd:+,.2f}</b> ({pnl_btc:+.6f} BTC, {roi:+.1f}%)\n"
            f"Hold: {hold_seconds/60:.1f} min\n"
            f"Entry cost: ${entry_cost_usd:,.2f}\n"
            f"{close_detail}\n"
            f"BTC: ${index_price:,.0f}\n" if index_price else ""
            f"Equity: ${account.equity:,.2f}"
        )
    except Exception:
        pass


# ─── Strategy Factory ──────────────────────────────────────────────────────

def atm_str_fixpnl_deribit() -> StrategyConfig:
    """
    ATM_Str_fixpnl_Deribit — daily long ATM straddle with $1,000 TP.

    Derived from Optimal Entry Window analysis: enters at 10:00 UTC,
    takes profit at $1,000, hard-closes at 19:00 UTC, weekdays only.

    Returns a StrategyConfig for registration in main.py's STRATEGIES list.
    """
    return StrategyConfig(
        name=STRATEGY_NAME,

        # ── What to trade ────────────────────────────────────────────
        legs=straddle(
            qty=QTY,
            dte=DTE,
            side="buy",
        ),

        # ── When to enter ────────────────────────────────────────────
        # All conditions must be True:
        #   1. Weekday (Mon–Fri)
        #   2. Inside the 10:00–11:00 UTC window
        #   3. Sufficient margin available
        entry_conditions=[
            _weekday_only(),
            time_window(OPEN_HOUR, OPEN_HOUR + 1),
            min_available_margin_pct(MIN_MARGIN_PCT),
        ],

        # ── When to exit ─────────────────────────────────────────────
        # ANY condition returning True triggers a close:
        #   1. Dollar profit target → close at +$1,000 USD
        #   2. Time exit → hard close at 19:00 UTC (9h hold)
        exit_conditions=[
            _dollar_profit_target(TP_USD),
            time_exit(CLOSE_HOUR, CLOSE_MINUTE),
        ],

        # ── How to execute ───────────────────────────────────────────
        # Orderbook-based limit orders, two phases:
        #   Phase 1: 3 min quoting at mark price
        #   Phase 2: 3 min aggressive (cross spread with buffer)
        execution_mode="limit",
        execution_params=_build_execution_params(),

        # ── Operational limits ───────────────────────────────────────
        max_concurrent_trades=1,
        max_trades_per_day=1,
        cooldown_seconds=0,
        check_interval_seconds=CHECK_INTERVAL,

        # ── Callbacks ────────────────────────────────────────────────
        on_trade_opened=_on_trade_opened,
        on_trade_closed=_on_trade_closed,
    )
