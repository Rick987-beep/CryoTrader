#!/usr/bin/env python3
"""
Trade Lifecycle Manager

Orchestrates the full lifecycle of a trade from intent through execution,
position management, and closing:

    PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED

Each TradeLifecycle groups one or more legs (e.g. an Iron Condor has 4 legs).
The LifecycleManager advances every active trade through the state machine on
each tick(), which is driven by the PositionMonitor callback.

Supports three execution modes:
  - "limit"  : per-leg limit orders via TradeExecutor (parallel, with requoting)
  - "rfq"    : atomic multi-leg RFQ via RFQExecutor
  - "smart"  : multi-leg smart orderbook with chunking & continuous quoting

Exit conditions are callables with signature:
    (AccountSnapshot, TradeLifecycle) -> bool
Factory functions are provided for common patterns (profit target, max loss, etc.).
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from account_manager import AccountSnapshot, PositionSnapshot
from trade_execution import TradeExecutor, LimitFillManager, ExecutionParams, ExecutionPhase
from rfq import RFQExecutor, OptionLeg, RFQResult
from multileg_orderbook import SmartOrderbookExecutor, SmartExecConfig
from market_data import get_option_orderbook
from telegram_notifier import get_notifier

logger = logging.getLogger(__name__)


# =============================================================================
# Enums & Data Classes
# =============================================================================

class TradeState(Enum):
    """States in the trade lifecycle state machine."""
    PENDING_OPEN  = "pending_open"   # Intent created, no orders yet
    OPENING       = "opening"        # Open orders placed, waiting for fills
    OPEN          = "open"           # All legs filled, position being managed
    PENDING_CLOSE = "pending_close"  # Exit triggered, not yet ordered
    CLOSING       = "closing"        # Close orders placed, waiting for fills
    CLOSED        = "closed"         # Fully closed
    FAILED        = "failed"         # Unrecoverable error


@dataclass
class RFQParams:
    """
    Typed configuration for RFQ execution.

    Replaces the loose metadata keys (rfq_timeout_seconds, rfq_min_improvement_pct,
    rfq_fallback) with a proper dataclass.  Metadata-based usage still works as a
    fallback for backward compatibility.

    Attributes:
        timeout_seconds: Maximum time to wait for RFQ quotes (default 60).
        min_improvement_pct: Minimum improvement vs orderbook to accept a quote.
            0.0 = require beating the book; -999 = accept anything (default).
        fallback_mode: Execution mode to try if RFQ fails:
            "limit" → fall back to per-leg limit orders.
            "smart" → fall back to smart orderbook execution.
            None    → no fallback, mark trade FAILED.
    """
    timeout_seconds: float = 60.0
    min_improvement_pct: float = -999.0
    fallback_mode: Optional[str] = None


@dataclass
class TradeLeg:
    """
    A single leg within a trade lifecycle.

    Fields are populated progressively:
      - symbol/qty/side are set at creation
      - order_id is set when the order is placed
      - fill_price is set when the order fills
      - position_id is set when the position appears on the exchange
    """
    symbol: str
    qty: float
    side: int               # 1 = buy, 2 = sell

    # Populated after order placement
    order_id: Optional[str] = None

    # Populated after fill
    fill_price: Optional[float] = None
    filled_qty: float = 0.0

    # Populated when matched to exchange position
    position_id: Optional[str] = None

    def __post_init__(self):
        """Ensure fill_price is always float (API may return strings)."""
        if self.fill_price is not None:
            self.fill_price = float(self.fill_price)

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.qty

    @property
    def close_side(self) -> int:
        """Opposite side for closing this leg."""
        return 2 if self.side == 1 else 1

    @property
    def side_label(self) -> str:
        return "buy" if self.side == 1 else "sell"


# Type alias for exit condition callables
ExitCondition = Callable[[AccountSnapshot, "TradeLifecycle"], bool]


@dataclass
class TradeLifecycle:
    """
    Tracks one trade (possibly multi-leg) from intent through close.

    Attributes:
        id:               Unique identifier (UUID)
        state:            Current lifecycle state
        open_legs:        Legs for opening the position
        close_legs:       Legs for closing (auto-generated as reverse of open)
        exit_conditions:  List of callables; if ANY returns True, trigger close
        execution_mode:   "limit", "rfq", "smart", or None (auto-route)
        rfq_action:       "buy" or "sell" — passed to RFQExecutor.execute()
        smart_config:     SmartExecConfig for "smart" mode execution
        created_at:       Unix timestamp of creation
        opened_at:        Unix timestamp when all open legs filled
        closed_at:        Unix timestamp when all close legs filled
        error:            Error message if state is FAILED
        rfq_result:       RFQResult from open (if RFQ mode)
        close_rfq_result: RFQResult from close (if RFQ mode)
        metadata:         Arbitrary strategy-provided context
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    strategy_id: Optional[str] = None
    state: TradeState = TradeState.PENDING_OPEN
    open_legs: List[TradeLeg] = field(default_factory=list)
    close_legs: List[TradeLeg] = field(default_factory=list)
    exit_conditions: List[ExitCondition] = field(default_factory=list)
    execution_mode: Optional[str] = None  # "limit", "rfq", "smart", or None (auto-route)
    rfq_action: str = "buy"             # "buy" or "sell" — for the open
    smart_config: Optional[SmartExecConfig] = None  # Config for "smart" mode
    execution_params: Optional[ExecutionParams] = None  # Config for "limit" mode phases/timeouts
    rfq_params: Optional[RFQParams] = None  # Config for "rfq" mode timing/improvement
    created_at: float = field(default_factory=time.time)
    opened_at: Optional[float] = None
    closed_at: Optional[float] = None
    error: Optional[str] = None
    rfq_result: Optional[Any] = None
    close_rfq_result: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Populated at close time — persists after positions disappear
    realized_pnl: Optional[float] = None
    exit_cost: Optional[float] = None

    # -- Helpers --------------------------------------------------------------

    @property
    def symbols(self) -> List[str]:
        return [leg.symbol for leg in self.open_legs]

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def hold_seconds(self) -> Optional[float]:
        """Time since all legs opened (None if not yet open)."""
        if self.opened_at is None:
            return None
        return time.time() - self.opened_at

    def _our_share(self, leg: "TradeLeg", pos: PositionSnapshot) -> float:
        """
        Fraction of the exchange position that belongs to this lifecycle.

        The exchange aggregates all holdings in the same contract into one
        position.  If we hold 0.5 and the total position is 1.0, our share
        is 0.5.  We clamp to [0, 1] as a safety measure.
        """
        if pos.qty == 0:
            return 0.0
        our_qty = leg.filled_qty if leg.filled_qty > 0 else leg.qty
        return min(our_qty / pos.qty, 1.0)

    def structure_pnl(self, account: AccountSnapshot) -> float:
        """Unrealised PnL for THIS lifecycle's legs only (pro-rated)."""
        total = 0.0
        for leg in self.open_legs:
            pos = account.get_position(leg.symbol)
            if pos:
                total += pos.unrealized_pnl * self._our_share(leg, pos)
        return total

    def executable_pnl(self) -> Optional[float]:
        """PnL if the structure were closed at current best bid/ask prices.

        For each open leg, fetches the live orderbook and uses:
          - best BID for legs we'd SELL to close  (long positions, side=1)
          - best ASK for legs we'd BUY to close   (short positions, side=2)

        Returns the net PnL vs entry fills, or None if any orderbook is
        unavailable (safety — the calling condition should not trigger).

        Works for any multi-leg structure: straddles, strangles, iron
        condors, butterflies, etc.
        """
        total_exit_value = 0.0

        for leg in self.open_legs:
            if leg.fill_price is None:
                return None  # leg not yet filled — shouldn't be called

            orderbook = get_option_orderbook(leg.symbol)
            if not orderbook:
                logger.debug(
                    f"[{self.id}] executable_pnl: no orderbook for {leg.symbol}"
                )
                return None

            bids = orderbook.get('bids', [])
            asks = orderbook.get('asks', [])

            # To close: long (side=1) → sell → need bid;
            #           short (side=2) → buy back → need ask
            if leg.side == 1:  # long — close by selling
                if not bids:
                    logger.debug(
                        f"[{self.id}] executable_pnl: no bids for {leg.symbol}"
                    )
                    return None
                close_price = float(bids[0]['price'])
            else:  # short — close by buying
                if not asks:
                    logger.debug(
                        f"[{self.id}] executable_pnl: no asks for {leg.symbol}"
                    )
                    return None
                close_price = float(asks[0]['price'])

            if close_price <= 0:
                logger.debug(
                    f"[{self.id}] executable_pnl: zero/negative price for {leg.symbol}"
                )
                return None

            qty = leg.filled_qty if leg.filled_qty > 0 else leg.qty
            entry_price = float(leg.fill_price)

            # PnL per leg: for a BUY (side=1), profit = (close - entry) * qty
            #              for a SELL (side=2), profit = (entry - close) * qty
            if leg.side == 1:
                total_exit_value += (close_price - entry_price) * qty
            else:
                total_exit_value += (entry_price - close_price) * qty

        return total_exit_value

    def structure_delta(self, account: AccountSnapshot) -> float:
        """Delta for THIS lifecycle's legs only (pro-rated)."""
        total = 0.0
        for leg in self.open_legs:
            pos = account.get_position(leg.symbol)
            if pos:
                total += pos.delta * self._our_share(leg, pos)
        return total

    def structure_greeks(self, account: AccountSnapshot) -> Dict[str, float]:
        """Aggregated Greeks for THIS lifecycle's legs only (pro-rated)."""
        d = g = t = v = 0.0
        for leg in self.open_legs:
            pos = account.get_position(leg.symbol)
            if pos:
                share = self._our_share(leg, pos)
                d += pos.delta * share
                g += pos.gamma * share
                t += pos.theta * share
                v += pos.vega * share
        return {"delta": d, "gamma": g, "theta": t, "vega": v}

    def total_entry_cost(self) -> float:
        """Sum of fill_price * qty across all open legs (signed by side)."""
        total = 0.0
        for leg in self.open_legs:
            if leg.fill_price is not None:
                sign = 1 if leg.side == 1 else -1  # buy = debit, sell = credit
                total += sign * float(leg.fill_price) * leg.filled_qty
        return total

    def total_exit_cost(self) -> float:
        """Sum of fill_price * qty across all close legs (signed by side).

        Close legs have the opposite side to open legs, so a BUY open
        becomes a SELL close.  The sign convention matches total_entry_cost:
        buy = debit (+), sell = credit (-).
        """
        total = 0.0
        for leg in self.close_legs:
            if leg.fill_price is not None:
                sign = 1 if leg.side == 1 else -1
                total += sign * float(leg.fill_price) * leg.filled_qty
        return total

    def _finalize_close(self) -> None:
        """Capture realized PnL at close time.

        Called exactly once when the trade transitions to CLOSED.
        Computes PnL from entry vs exit fill prices so the result
        persists after exchange positions disappear.

        PnL = -(entry_cost + exit_cost)
        For a buy-to-open: entry_cost is positive (debit), exit_cost is
        negative (credit from selling), so net = credit - debit.
        """
        self.exit_cost = self.total_exit_cost()
        entry = self.total_entry_cost()
        self.realized_pnl = -(entry + self.exit_cost)

    def summary(self, account: Optional[AccountSnapshot] = None) -> str:
        legs_str = ", ".join(
            f"{l.side_label} {l.qty}x {l.symbol}" for l in self.open_legs
        )
        prefix = f"[{self.id}]"
        if self.strategy_id:
            prefix += f" ({self.strategy_id})"
        s = f"{prefix} {self.state.value} | {legs_str}"
        if account and self.state == TradeState.OPEN:
            pnl = self.structure_pnl(account)
            greeks = self.structure_greeks(account)
            s += f" | PnL={pnl:+.4f} Δ={greeks['delta']:+.4f}"
        return s

    def to_dict(self) -> Dict[str, Any]:
        """Serialize trade state for crash-recovery persistence.

        Excludes non-serializable fields (exit_conditions, rfq_result,
        smart_config, execution_params, rfq_params, metadata internals).
        Those are re-attached from the strategy config on recovery.
        """
        return {
            "id": self.id,
            "strategy_id": self.strategy_id,
            "state": self.state.value,
            "execution_mode": self.execution_mode,
            "rfq_action": self.rfq_action,
            "created_at": self.created_at,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "error": self.error,
            "realized_pnl": self.realized_pnl,
            "exit_cost": self.exit_cost,
            "open_legs": [
                {
                    "symbol": l.symbol,
                    "qty": l.qty,
                    "side": l.side,
                    "order_id": l.order_id,
                    "fill_price": l.fill_price,
                    "filled_qty": l.filled_qty,
                }
                for l in self.open_legs
            ],
            "close_legs": [
                {
                    "symbol": l.symbol,
                    "qty": l.qty,
                    "side": l.side,
                    "order_id": l.order_id,
                    "fill_price": l.fill_price,
                    "filled_qty": l.filled_qty,
                }
                for l in self.close_legs
            ],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeLifecycle":
        """Reconstruct a TradeLifecycle from a persisted dict.

        Exit conditions are NOT restored here — caller must re-attach
        them from the matching strategy config.
        """
        return cls(
            id=data["id"],
            strategy_id=data.get("strategy_id"),
            state=TradeState(data["state"]),
            execution_mode=data.get("execution_mode"),
            rfq_action=data.get("rfq_action", "buy"),
            created_at=data.get("created_at", time.time()),
            opened_at=data.get("opened_at"),
            closed_at=data.get("closed_at"),
            error=data.get("error"),
            realized_pnl=data.get("realized_pnl"),
            exit_cost=data.get("exit_cost"),
            open_legs=[
                TradeLeg(
                    symbol=l["symbol"],
                    qty=l["qty"],
                    side=l["side"],
                    order_id=l.get("order_id"),
                    fill_price=l.get("fill_price"),
                    filled_qty=l.get("filled_qty", 0.0),
                )
                for l in data.get("open_legs", [])
            ],
            close_legs=[
                TradeLeg(
                    symbol=l["symbol"],
                    qty=l["qty"],
                    side=l["side"],
                    order_id=l.get("order_id"),
                    fill_price=l.get("fill_price"),
                    filled_qty=l.get("filled_qty", 0.0),
                )
                for l in data.get("close_legs", [])
            ],
        )


# =============================================================================
# Lifecycle Manager
# =============================================================================

class LifecycleManager:
    """
    Orchestrates one or more TradeLifecycles through their state machines.

    Usage:
        manager = LifecycleManager()

        # Hook into PositionMonitor so tick() runs on every snapshot
        position_monitor.on_update(manager.tick)

        # Create a trade
        trade = manager.create(
            legs=[
                TradeLeg(symbol="BTCUSD-20FEB26-70000-C", qty=0.01, side=1),
            ],
            exit_conditions=[profit_target(50), max_hold_hours(48)],
            execution_mode="limit",
        )

        # Open it (places orders)
        manager.open(trade.id)

        # From here, tick() handles everything:
        # - Detects fills  → moves to OPEN
        # - Evaluates exit conditions  → moves to PENDING_CLOSE
        # - Places close orders  → moves to CLOSING
        # - Detects close fills  → moves to CLOSED
    """

    def __init__(
        self,
        rfq_notional_threshold: float = 50000.0,
        smart_notional_threshold: float = 10000.0,
    ):
        """
        Initialize LifecycleManager with execution routing parameters.
        
        Args:
            rfq_notional_threshold: Use RFQ for multi-leg orders >= this notional (USD)
            smart_notional_threshold: Use smart dealing for multi-leg orders >= this notional
                and < rfq_notional_threshold. Below this, falls back to limit orders per leg.
        """
        self._trades: Dict[str, TradeLifecycle] = {}
        self._executor = TradeExecutor()
        self._rfq_executor = RFQExecutor()
        self._smart_executor = SmartOrderbookExecutor()
        
        # Execution routing thresholds
        self.rfq_notional_threshold = rfq_notional_threshold
        self.smart_notional_threshold = smart_notional_threshold

    @property
    def active_trades(self) -> List[TradeLifecycle]:
        """All trades that are not CLOSED or FAILED."""
        return [
            t for t in self._trades.values()
            if t.state not in (TradeState.CLOSED, TradeState.FAILED)
        ]

    @property
    def all_trades(self) -> List[TradeLifecycle]:
        return list(self._trades.values())

    def get(self, trade_id: str) -> Optional[TradeLifecycle]:
        return self._trades.get(trade_id)

    def get_trades_for_strategy(self, strategy_id: str) -> List[TradeLifecycle]:
        """All trades (any state) belonging to a strategy."""
        return [t for t in self._trades.values() if t.strategy_id == strategy_id]

    def active_trades_for_strategy(self, strategy_id: str) -> List[TradeLifecycle]:
        """Active (not CLOSED/FAILED) trades belonging to a strategy."""
        return [t for t in self.active_trades if t.strategy_id == strategy_id]

    def _notify_trade_opened(self, trade: TradeLifecycle) -> None:
        """Send a Telegram notification when a trade reaches OPEN state."""
        try:
            get_notifier().notify_trade_opened(
                strategy_name=trade.strategy_id or "unknown",
                trade_id=trade.id,
                legs=trade.open_legs,
                entry_cost=trade.total_entry_cost(),
            )
        except Exception:
            pass  # Never let notification failure affect trading

    def restore_trade(self, trade: TradeLifecycle) -> None:
        """Inject a recovered trade into the manager's trade registry.

        Used by crash recovery to restore trades from persisted state.
        Active trades must have exit_conditions re-attached before calling.
        """
        self._trades[trade.id] = trade
        logger.info(
            f"Restored trade {trade.id} (strategy={trade.strategy_id}, "
            f"state={trade.state.value}, legs={len(trade.open_legs)})"
        )

    # -------------------------------------------------------------------------
    # Create
    # -------------------------------------------------------------------------

    def create(
        self,
        legs: List[TradeLeg],
        exit_conditions: Optional[List[ExitCondition]] = None,
        execution_mode: Optional[str] = None,
        rfq_action: str = "buy",
        smart_config: Optional[SmartExecConfig] = None,
        execution_params: Optional[ExecutionParams] = None,
        rfq_params: Optional[RFQParams] = None,
        strategy_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TradeLifecycle:
        """
        Register a new trade intent.

        Args:
            legs: TradeLeg objects defining the structure to open
            exit_conditions: Callables that trigger a close when True
            execution_mode: "limit", "rfq", "smart", or None (auto-route).
                If None, execution mode will be auto-selected based on:
                - Single leg → "limit"
                - Multi-leg with notional >= rfq_threshold → "rfq"
                - Multi-leg with notional < rfq_threshold → "smart"
            rfq_action: "buy" or "sell" — passed to RFQExecutor
            smart_config: SmartExecConfig — optional, can be provided for "smart" mode
            execution_params: ExecutionParams — optional, for "limit" mode phases/timeouts
            rfq_params: RFQParams — optional, for "rfq" mode timing/improvement
            metadata: Arbitrary context (strategy name, notes, etc.)

        Returns:
            TradeLifecycle in PENDING_OPEN state
        """
        trade = TradeLifecycle(
            open_legs=legs,
            strategy_id=strategy_id,
            exit_conditions=exit_conditions or [],
            execution_mode=execution_mode,
            rfq_action=rfq_action,
            smart_config=smart_config,
            execution_params=execution_params,
            rfq_params=rfq_params,
            metadata=metadata or {},
        )
        self._trades[trade.id] = trade
        logger.info(f"Trade {trade.id} created: {len(legs)} legs, mode={execution_mode or 'auto-route'}, strategy={strategy_id}")
        return trade

    # -------------------------------------------------------------------------
    # Execution Mode Routing
    # -------------------------------------------------------------------------

    def _determine_execution_mode(self, trade: TradeLifecycle) -> str:
        """
        Auto-determine execution mode based on trade characteristics.
        
        Called only if trade.execution_mode is None (auto-routing enabled).
        
        Logic:
          - Single leg → "limit"
          - Multi-leg, notional >= rfq_threshold → "rfq"
          - Multi-leg, smart_threshold <= notional < rfq_threshold → "smart"
          - Multi-leg, notional < smart_threshold → "limit" (fallback)
        
        Args:
            trade: TradeLifecycle to analyze
            
        Returns:
            Determined execution mode: "limit", "rfq", or "smart"
        """
        # Single leg always uses limit orders
        if len(trade.open_legs) == 1:
            logger.info(f"[{trade.id}] Single leg detected, using 'limit' mode")
            return "limit"
        
        # Multi-leg: calculate notional value
        notional = self._calculate_notional(trade.open_legs)
        logger.info(f"[{trade.id}] Multi-leg notional: ${notional:,.2f}")
        
        # Route based on notional thresholds
        if notional >= self.rfq_notional_threshold:
            logger.info(f"[{trade.id}] Notional >= ${self.rfq_notional_threshold:,.0f}, using 'rfq' mode")
            return "rfq"
        elif notional >= self.smart_notional_threshold:
            logger.info(f"[{trade.id}] ${self.smart_notional_threshold:,.0f} <= notional < ${self.rfq_notional_threshold:,.0f}, using 'smart' mode")
            return "smart"
        else:
            logger.info(f"[{trade.id}] Notional < ${self.smart_notional_threshold:,.0f}, using 'limit' mode (fallback)")
            return "limit"

    def _calculate_notional(self, legs: List[TradeLeg]) -> float:
        """
        Calculate total notional value of a multi-leg order.
        
        Notional = sum of (mark_price * qty) for each leg.
        Uses current orderbook mark prices.
        
        Args:
            legs: List of TradeLeg objects
            
        Returns:
            Total notional in USD
        """
        total_notional = 0.0
        
        for leg in legs:
            try:
                orderbook = get_option_orderbook(leg.symbol)
                if not orderbook:
                    logger.warning(f"Could not fetch orderbook for {leg.symbol}, using 0 notional")
                    continue
                
                mark_price = float(orderbook.get('mark', 0))
                if mark_price <= 0:
                    logger.warning(f"Invalid mark price for {leg.symbol}, skipping")
                    continue
                
                leg_notional = mark_price * leg.qty
                total_notional += leg_notional
                logger.debug(f"  {leg.symbol}: {leg.qty} @ ${mark_price} = ${leg_notional:,.2f}")
                
            except Exception as e:
                logger.warning(f"Error calculating notional for {leg.symbol}: {e}")
                continue
        
        return total_notional

    # -------------------------------------------------------------------------
    # Open
    # -------------------------------------------------------------------------

    def open(self, trade_id: str) -> bool:
        """
        Place orders to open a trade.

        If execution_mode is None, auto-determines best mode:
          - Single leg → "limit"
          - Multi-leg with large notional → "rfq"
          - Multi-leg with medium notional → "smart"
          - Multi-leg with small notional → "limit" (fallback)

        Then routes to the appropriate executor:
          - "limit": Individual limit orders via TradeExecutor
          - "rfq": Atomic multi-leg RFQ via RFQExecutor
          - "smart": Multi-leg smart orderbook with chunking via SmartOrderbookExecutor

        Returns True if orders were placed (not necessarily filled yet).
        """
        trade = self._trades.get(trade_id)
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False
        if trade.state != TradeState.PENDING_OPEN:
            logger.error(f"Trade {trade_id} not in PENDING_OPEN (is {trade.state.value})")
            return False

        # Auto-determine execution mode if not explicitly set
        if trade.execution_mode is None:
            trade.execution_mode = self._determine_execution_mode(trade)

        logger.info(f"Opening trade {trade_id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
            return self._open_rfq(trade)
        elif trade.execution_mode == "smart":
            return self._open_smart(trade)
        else:  # Default to "limit"
            return self._open_limit(trade)

    def _open_rfq(self, trade: TradeLifecycle) -> bool:
        """Open via RFQ — atomic multi-leg execution.

        Reads RFQ parameters from trade.rfq_params (typed) first,
        falling back to metadata keys for backward compatibility:
          - rfq_timeout_seconds (int): Override default 60s RFQ poll timeout.
          - rfq_min_improvement_pct (float): Minimum improvement vs orderbook
              to accept a quote. Default -999 (accept anything).
          - rfq_fallback (str|None): Execution mode to try if RFQ fails.
        """
        rfq_legs = [
            OptionLeg(
                instrument=leg.symbol,
                side="BUY" if leg.side == 1 else "SELL",
                qty=leg.qty,
            )
            for leg in trade.open_legs
        ]

        # Read from typed RFQParams first, fall back to metadata
        rp = trade.rfq_params
        rfq_timeout = rp.timeout_seconds if rp else trade.metadata.get("rfq_timeout_seconds", 60)
        min_improvement = rp.min_improvement_pct if rp else trade.metadata.get("rfq_min_improvement_pct", -999.0)

        result: RFQResult = self._rfq_executor.execute(
            legs=rfq_legs,
            action=trade.rfq_action,
            timeout_seconds=rfq_timeout,
            min_improvement_pct=min_improvement,
        )
        trade.rfq_result = result

        if result.success:
            # RFQ fills are atomic — all legs filled
            trade.state = TradeState.OPEN
            trade.opened_at = time.time()
            # Try to extract fill prices from RFQ result legs
            for i, leg in enumerate(trade.open_legs):
                leg.filled_qty = leg.qty
                if i < len(result.legs):
                    leg.fill_price = float(result.legs[i].get('price', 0.0))
            logger.info(f"Trade {trade.id} opened via RFQ (all legs filled)")
            self._notify_trade_opened(trade)
            return True

        # RFQ failed — try fallback if configured
        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            logger.warning(
                f"Trade {trade.id} RFQ open failed: {result.message} "
                f"— falling back to '{fallback}'"
            )
            trade.execution_mode = fallback
            if fallback == "smart":
                return self._open_smart(trade)
            else:
                return self._open_limit(trade)

        trade.state = TradeState.FAILED
        trade.error = result.message
        logger.error(f"Trade {trade.id} RFQ failed: {result.message}")
        return False

    def _open_limit(self, trade: TradeLifecycle) -> bool:
        """Open via limit orders — delegates placement to LimitFillManager.

        Creates a fill manager, places limit orders for all legs using
        the trade's ExecutionParams (phased or legacy), and stores the
        manager in trade metadata for tick-based fill checking.
        """
        trade.state = TradeState.OPENING

        # Typed field first, then metadata fallback, then defaults
        params = trade.execution_params or trade.metadata.get("execution_params") or ExecutionParams()
        mgr = LimitFillManager(self._executor, params)

        ok = mgr.place_all(trade.open_legs)
        if not ok:
            trade.error = "Failed to place one or more open orders"
            logger.error(f"Trade {trade.id}: {trade.error}")
            trade.state = TradeState.FAILED
            return False

        trade.metadata["_open_fill_mgr"] = mgr
        logger.info(f"Trade {trade.id}: all {len(trade.open_legs)} open orders placed via LimitFillManager")
        return True

    def _open_smart(self, trade: TradeLifecycle) -> bool:
        """Open via smart multi-leg orderbook execution with chunking."""
        if not trade.smart_config:
            trade.state = TradeState.FAILED
            trade.error = "smart_config required for 'smart' execution mode"
            logger.error(f"Trade {trade.id}: {trade.error}")
            return False

        trade.state = TradeState.OPENING
        
        try:
            logger.info(f"Trade {trade.id}: starting smart execution with {trade.smart_config.chunk_count} chunks")
            
            result = self._smart_executor.execute_smart_multi_leg(
                legs=trade.open_legs,
                config=trade.smart_config
            )
            
            if result.success:
                # Update legs with execution results
                for leg in trade.open_legs:
                    if leg.symbol in result.total_filled_qty:
                        leg.filled_qty = result.total_filled_qty[leg.symbol]
                
                trade.state = TradeState.OPEN
                trade.opened_at = time.time()
                logger.info(f"Trade {trade.id}: smart execution completed successfully")
                self._notify_trade_opened(trade)
                return True
            else:
                trade.state = TradeState.FAILED
                trade.error = result.message
                logger.error(f"Trade {trade.id}: smart execution failed: {result.message}")
                return False
                
        except Exception as e:
            trade.state = TradeState.FAILED
            trade.error = str(e)
            logger.error(f"Trade {trade.id}: exception in smart execution: {e}")
            return False

    # -------------------------------------------------------------------------
    # Close
    # -------------------------------------------------------------------------

    def close(self, trade_id: str) -> bool:
        """
        Place orders to close a trade.

        Generates close legs as the reverse of open legs and submits them.
        Returns True if close orders were placed.
        """
        trade = self._trades.get(trade_id)
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False
        if trade.state not in (TradeState.OPEN, TradeState.PENDING_CLOSE):
            logger.error(f"Trade {trade_id} not closeable (is {trade.state.value})")
            return False

        logger.info(f"Closing trade {trade_id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
            # RFQ needs close_legs built upfront (atomic execution)
            trade.close_legs = [
                TradeLeg(
                    symbol=leg.symbol,
                    qty=leg.filled_qty if leg.filled_qty > 0 else leg.qty,
                    side=leg.close_side,
                )
                for leg in trade.open_legs
            ]
            return self._close_rfq(trade)
        else:
            # _close_limit rebuilds close_legs itself (handles retries)
            return self._close_limit(trade)

    def _close_rfq(self, trade: TradeLifecycle) -> bool:
        """Close via RFQ — atomic multi-leg execution.
        
        Submits the SAME leg structure as the open (preserving each leg's
        original side), but reverses the action: buy→sell or sell→buy.

        Reads RFQ parameters from trade.rfq_params (typed) first,
        falling back to metadata keys for backward compatibility.
        """
        # Use the ORIGINAL open legs (preserving each leg's side)
        rfq_legs = [
            OptionLeg(
                instrument=leg.symbol,
                side="BUY" if leg.side == 1 else "SELL",
                qty=leg.filled_qty if leg.filled_qty > 0 else leg.qty,
            )
            for leg in trade.open_legs
        ]

        # Reverse the action: if we bought to open, we sell to close
        close_action = "sell" if trade.rfq_action == "buy" else "buy"

        # Read from typed RFQParams first, fall back to metadata
        rp = trade.rfq_params
        rfq_timeout = rp.timeout_seconds if rp else trade.metadata.get("rfq_timeout_seconds", 60)
        min_improvement = rp.min_improvement_pct if rp else trade.metadata.get("rfq_min_improvement_pct", -999.0)

        result: RFQResult = self._rfq_executor.execute(
            legs=rfq_legs,
            action=close_action,
            timeout_seconds=rfq_timeout,
            min_improvement_pct=min_improvement,
        )
        trade.close_rfq_result = result

        if result.success:
            trade.state = TradeState.CLOSED
            trade.closed_at = time.time()
            for i, leg in enumerate(trade.close_legs):
                leg.filled_qty = leg.qty
                if i < len(result.legs):
                    leg.fill_price = float(result.legs[i].get('price', 0.0))
            trade._finalize_close()
            logger.info(f"Trade {trade.id} closed via RFQ (PnL={trade.realized_pnl:+.4f})")
            return True

        # RFQ close failed — try fallback if configured
        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            logger.warning(
                f"Trade {trade.id} RFQ close failed: {result.message} "
                f"— falling back to '{fallback}'"
            )
            trade.execution_mode = fallback
            return self._close_limit(trade)

        # No fallback — remain in PENDING_CLOSE so next tick retries
        trade.state = TradeState.PENDING_CLOSE
        logger.error(f"Trade {trade.id} RFQ close failed: {result.message}, will retry")
        return False

    def _close_limit(self, trade: TradeLifecycle) -> bool:
        """Close via limit orders — delegates placement to LimitFillManager.

        Creates a fill manager for close legs, places aggressive limit
        orders, and stores the manager for tick-based fill checking.

        If this is a retry (previous close attempt failed/was force-closed),
        we rebuild close_legs from open_legs to clear stale order IDs and
        account for any partial close fills.
        """
        trade.state = TradeState.CLOSING

        # Rebuild close legs fresh — prevents double-ordering on retry.
        # Each close leg's qty = remaining open qty minus any already-closed qty.
        old_close_filled = {}
        if trade.close_legs:
            for cl in trade.close_legs:
                if cl.filled_qty > 0:
                    old_close_filled[cl.symbol] = cl.filled_qty

        trade.close_legs = [
            TradeLeg(
                symbol=leg.symbol,
                qty=(leg.filled_qty if leg.filled_qty > 0 else leg.qty)
                    - old_close_filled.get(leg.symbol, 0.0),
                side=leg.close_side,
            )
            for leg in trade.open_legs
            if (leg.filled_qty if leg.filled_qty > 0 else leg.qty)
               - old_close_filled.get(leg.symbol, 0.0) > 0
        ]

        if not trade.close_legs:
            # Everything already closed from a previous partial close
            trade.state = TradeState.CLOSED
            trade.closed_at = time.time()
            trade._finalize_close()
            logger.info(f"Trade {trade.id}: all close legs already filled → CLOSED (PnL={trade.realized_pnl:+.4f})")
            return True

        # BUG-2026-03-05: circuit breaker — stop retrying after N failures
        MAX_CLOSE_ATTEMPTS = 10
        count = trade.metadata.get("_close_attempt_count", 0) + 1
        trade.metadata["_close_attempt_count"] = count
        if count > MAX_CLOSE_ATTEMPTS:
            trade.state = TradeState.FAILED
            trade.error = f"Close failed after {MAX_CLOSE_ATTEMPTS} attempts — manual intervention required"
            logger.critical(f"Trade {trade.id}: {trade.error}")
            try:
                get_notifier().notify_error(f"🚨 Trade {trade.id}: {trade.error}")
            except Exception:
                pass
            return False

        params = trade.execution_params or trade.metadata.get("execution_params") or ExecutionParams()
        mgr = LimitFillManager(self._executor, params)

        # BUG-2026-03-05: reduce_only prevents close orders from building reverse positions
        ok = mgr.place_all(trade.close_legs, reduce_only=True)
        if not ok:
            logger.error(f"Trade {trade.id}: failed to place close orders, will retry (attempt {count}/{MAX_CLOSE_ATTEMPTS})")
            trade.state = TradeState.PENDING_CLOSE
            return False

        trade.metadata["_close_fill_mgr"] = mgr
        logger.info(f"Trade {trade.id}: all close orders placed via LimitFillManager")
        return True

    def _cancel_placed_orders(self, legs: List[TradeLeg]) -> None:
        """Cancel any orders already placed for the given legs (cleanup on failure)."""
        for leg in legs:
            if leg.order_id and not leg.is_filled:
                try:
                    self._executor.cancel_order(leg.order_id)
                    logger.info(f"Cancelled orphaned order {leg.order_id} for {leg.symbol}")
                except Exception as e:
                    logger.warning(f"Failed to cancel orphaned order {leg.order_id}: {e}")

    def _unwind_filled_legs(self, trade: TradeLifecycle, filled_legs: List[TradeLeg]) -> None:
        """Unwind partially-filled legs by transitioning through close cycle.

        Trims the trade's open_legs to only the filled ones, sets state to
        PENDING_CLOSE, and lets the normal tick loop handle close orders.
        """
        trade.open_legs = filled_legs
        trade.state = TradeState.OPEN
        trade.opened_at = time.time()
        trade.state = TradeState.PENDING_CLOSE  # next tick will call close()
        logger.info(
            f"Trade {trade.id}: unwinding {len(filled_legs)} filled legs "
            f"via PENDING_CLOSE"
        )

    def _check_open_fills(self, trade: TradeLifecycle) -> None:
        """Delegate fill-checking to LimitFillManager.

        Result map:
          "filled"   → all legs done → OPEN
          "requoted" → timeout hit, orders cancelled & re-placed → stay OPENING
          "failed"   → max requotes exhausted → unwind filled legs
          "pending"  → still waiting
        """
        mgr: Optional[LimitFillManager] = trade.metadata.get("_open_fill_mgr")
        if mgr is None:
            logger.error(f"Trade {trade.id}: no fill manager for OPENING state")
            return

        result = mgr.check()

        if result == "filled":
            # Sync fill data back to TradeLegs
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            trade.state = TradeState.OPEN
            trade.opened_at = time.time()
            logger.info(f"Trade {trade.id}: all open legs filled → OPEN")
            self._notify_trade_opened(trade)

        elif result == "failed":
            logger.error(f"Trade {trade.id}: fill manager exhausted requote rounds")
            # Sync partial fills back, then cancel remaining
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            mgr.cancel_all()
            # If any legs partially filled, we need to unwind them
            filled_legs = [leg for leg in trade.open_legs if leg.filled_qty > 0]
            if filled_legs:
                logger.warning(
                    f"Trade {trade.id}: {len(filled_legs)} legs have partial fills "
                    f"— unwinding"
                )
                self._unwind_filled_legs(trade, filled_legs)
            else:
                trade.state = TradeState.FAILED
                trade.error = "Fill timeout exhausted, no fills"

        elif result == "requoted":
            # Sync order_ids back (they changed after requote)
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.order_id = ls.order_id
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
            logger.debug(f"Trade {trade.id}: requoted unfilled open legs, continuing")

        # "pending" → nothing to do, wait for next tick

    def _check_close_fills(self, trade: TradeLifecycle) -> None:
        """Delegate close-fill checking to LimitFillManager.

        Result map mirrors _check_open_fills:
          "filled"   → CLOSED
          "requoted" → timeout hit, re-placed → stay CLOSING
          "failed"   → max requotes exhausted → revert to PENDING_CLOSE
          "pending"  → still waiting
        """
        mgr: Optional[LimitFillManager] = trade.metadata.get("_close_fill_mgr")
        if mgr is None:
            logger.error(f"Trade {trade.id}: no fill manager for CLOSING state")
            return

        result = mgr.check()

        if result == "filled":
            for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            trade.state = TradeState.CLOSED
            trade.closed_at = time.time()
            trade._finalize_close()
            logger.info(f"Trade {trade.id}: all close legs filled → CLOSED (PnL={trade.realized_pnl:+.4f})")

        elif result == "failed":
            logger.error(f"Trade {trade.id}: close fill manager exhausted requote rounds")
            # Sync partial fills back before reverting
            for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            mgr.cancel_all()
            # Revert to PENDING_CLOSE so next tick retries
            trade.state = TradeState.PENDING_CLOSE

        elif result == "requoted":
            for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                leg.order_id = ls.order_id
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
            logger.debug(f"Trade {trade.id}: requoted unfilled close legs, continuing")

        # "pending" → wait for next tick

    # -------------------------------------------------------------------------
    # Exit Evaluation
    # -------------------------------------------------------------------------

    def _evaluate_exits(self, trade: TradeLifecycle, account: AccountSnapshot) -> None:
        """Check exit conditions for an OPEN trade. Any True → PENDING_CLOSE."""
        for cond in trade.exit_conditions:
            try:
                if cond(account, trade):
                    cond_name = getattr(cond, '__name__', repr(cond))
                    logger.info(
                        f"Trade {trade.id}: exit condition '{cond_name}' triggered → PENDING_CLOSE"
                    )
                    trade.state = TradeState.PENDING_CLOSE
                    return
            except Exception as e:
                logger.error(f"Trade {trade.id}: error evaluating exit condition: {e}")

    # -------------------------------------------------------------------------
    # Tick — the main heartbeat
    # -------------------------------------------------------------------------

    def tick(self, account: AccountSnapshot) -> None:
        """
        Advance all active trades one step through the state machine.

        Designed to be called as a PositionMonitor callback:
            position_monitor.on_update(manager.tick)

        Each call:
          - OPENING       → check fills → maybe OPEN
          - OPEN          → evaluate exits → maybe PENDING_CLOSE
          - PENDING_CLOSE → place close orders → CLOSING
          - CLOSING       → check close fills → maybe CLOSED
        """
        for trade in self.active_trades:
            try:
                if trade.state == TradeState.OPENING:
                    self._check_open_fills(trade)

                elif trade.state == TradeState.OPEN:
                    pnl = trade.structure_pnl(account)
                    hold = trade.hold_seconds or 0
                    logger.debug(
                        f"Trade {trade.id}: OPEN hold={hold:.0f}s PnL={pnl:+.4f} "
                        f"— checking exit conditions"
                    )
                    self._evaluate_exits(trade, account)
                    # If exit triggered, place close orders immediately —
                    # don't wait 10 s for the next tick.
                    if trade.state == TradeState.PENDING_CLOSE:
                        self.close(trade.id)

                elif trade.state == TradeState.PENDING_CLOSE:
                    self.close(trade.id)

                elif trade.state == TradeState.CLOSING:
                    self._check_close_fills(trade)

            except Exception as e:
                logger.error(f"Trade {trade.id}: tick error in state {trade.state.value}: {e}")

        # Persist trade state snapshot after processing
        if self._trades:
            self._persist_all_trades()

    # -------------------------------------------------------------------------
    # Manual Controls
    # -------------------------------------------------------------------------

    def force_close(self, trade_id: str) -> bool:
        """
        Force a trade closed regardless of exit conditions or current state.

        Handles every active state:
          - OPEN          → PENDING_CLOSE (next tick places close orders)
          - PENDING_CLOSE  → no-op (already queued for close)
          - OPENING        → cancel unfilled open orders, unwind filled legs
          - CLOSING        → cancel unfilled close orders, requote
          - PENDING_OPEN   → cancel (no orders placed yet)
        """
        trade = self._trades.get(trade_id)
        if not trade:
            return False

        state = trade.state

        if state == TradeState.OPEN:
            logger.info(f"Trade {trade.id}: forced close (was OPEN)")
            trade.state = TradeState.PENDING_CLOSE
            return True

        if state == TradeState.PENDING_CLOSE:
            logger.info(f"Trade {trade.id}: already PENDING_CLOSE")
            return True

        if state in (TradeState.PENDING_OPEN, TradeState.OPENING):
            # Cancel unfilled orders and unwind any filled legs
            return self.cancel(trade_id)

        if state == TradeState.CLOSING:
            # Cancel via fill manager (has latest order IDs after requotes)
            mgr: Optional[LimitFillManager] = trade.metadata.get("_close_fill_mgr")
            if mgr is not None:
                # Sync fill data back before reverting
                for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                    leg.order_id = ls.order_id
                    leg.filled_qty = ls.filled_qty
                    leg.fill_price = ls.fill_price
                mgr.cancel_all()
            else:
                self._cancel_placed_orders(trade.close_legs)
            trade.state = TradeState.PENDING_CLOSE
            logger.info(f"Trade {trade.id}: forced re-close (was CLOSING)")
            return True

        logger.warning(f"Trade {trade.id}: cannot force close in state {state.value}")
        return False

    def kill_all(self) -> int:
        """
        Emergency termination — cancel all orders and mark every trade CLOSED.

        Used by the dashboard kill switch before handing off to PositionCloser.
        Removes all trades from the tick() processing loop so the closer can
        work on exchange positions without interference.

        Returns the number of trades terminated.
        """
        killed = 0
        for trade in list(self._trades.values()):
            if trade.state in (TradeState.CLOSED, TradeState.FAILED):
                continue

            # Cancel any tracked orders (open legs and close legs)
            for leg in trade.open_legs + trade.close_legs:
                if leg.order_id and not leg.is_filled:
                    try:
                        self._executor.cancel_order(leg.order_id)
                    except Exception:
                        pass

            # Cancel via fill managers if present (they track requoted order IDs)
            for key in ("_open_fill_mgr", "_close_fill_mgr"):
                mgr = trade.metadata.get(key)
                if mgr is not None:
                    try:
                        mgr.cancel_all()
                    except Exception:
                        pass

            prev_state = trade.state.value
            trade.state = TradeState.CLOSED
            trade.closed_at = time.time()
            trade.error = "Terminated by kill switch"
            killed += 1
            logger.info(f"Trade {trade.id}: killed (was {prev_state})")

        if killed:
            self._persist_all_trades()

        return killed

    def cancel(self, trade_id: str) -> bool:
        """
        Cancel a trade that hasn't fully opened yet.

        Cancels any outstanding open orders.  If some legs already filled
        (partial open), those positions are unwound by placing close orders
        at aggressive best-bid prices and the trade transitions to
        PENDING_CLOSE → CLOSING → CLOSED via normal tick processing.
        """
        trade = self._trades.get(trade_id)
        if not trade:
            return False
        if trade.state not in (TradeState.PENDING_OPEN, TradeState.OPENING):
            logger.warning(f"Trade {trade.id}: cannot cancel in state {trade.state.value}")
            return False

        # 1. Cancel unfilled orders — prefer the fill manager (has latest
        #    order IDs after requotes), fall back to leg-based cancel.
        mgr: Optional[LimitFillManager] = trade.metadata.get("_open_fill_mgr")
        if mgr is not None:
            # Sync latest fill state back before inspecting legs
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.order_id = ls.order_id
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
            mgr.cancel_all()
            logger.info(f"Trade {trade.id}: cancelled unfilled orders via fill manager")
        else:
            for leg in trade.open_legs:
                if leg.order_id and not leg.is_filled:
                    try:
                        self._executor.cancel_order(leg.order_id)
                        logger.info(f"Trade {trade.id}: cancelled open order {leg.order_id} for {leg.symbol}")
                    except Exception as e:
                        logger.warning(f"Trade {trade.id}: cancel failed for {leg.order_id}: {e}")

        # 2. Check if any legs DID fill — those need unwinding
        filled_legs = [l for l in trade.open_legs if l.is_filled]
        if filled_legs:
            logger.info(
                f"Trade {trade.id}: {len(filled_legs)} legs already filled "
                f"— unwinding via close orders"
            )
            self._unwind_filled_legs(trade, filled_legs)
            return True

        # 3. Nothing filled — just mark FAILED
        trade.state = TradeState.FAILED
        trade.error = "Cancelled by user"
        logger.info(f"Trade {trade.id}: cancelled (no fills)")
        return True

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _persist_all_trades(self) -> None:
        """Dump all trade states to JSON for inspection and crash recovery."""
        try:
            os.makedirs("logs", exist_ok=True)
            trades_data = [trade.to_dict() for trade in self._trades.values()]
            with open("logs/trades_snapshot.json", "w") as f:
                json.dump({"timestamp": time.time(), "trades": trades_data}, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to persist trade snapshot: {e}")

    def status_report(self, account: Optional[AccountSnapshot] = None) -> str:
        """Human-readable status of all trades."""
        if not self._trades:
            return "No trades."
        lines = [f"{'ID':<14} {'State':<15} {'Legs':>4}  Description"]
        lines.append("-" * 70)
        for trade in self._trades.values():
            lines.append(trade.summary(account))
        return "\n".join(lines)



