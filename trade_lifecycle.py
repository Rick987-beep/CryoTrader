#!/usr/bin/env python3
"""
Trade Lifecycle — Data Model

Pure data definitions for the trade lifecycle:
  - TradeState enum
  - RFQParams configuration
  - TradeLeg dataclass
  - TradeLifecycle dataclass (with PnL helpers, serialization)
  - ExitCondition type alias

The state machine that drives trades through these states lives in
lifecycle_engine.py (LifecycleEngine).  Execution routing lives in
execution_router.py (ExecutionRouter).

"""

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from account_manager import AccountSnapshot, PositionSnapshot
from trade_execution import ExecutionParams, ExecutionPhase
from market_data import get_option_orderbook
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
    side: str               # "buy" or "sell"

    # Populated after order placement
    order_id: Optional[str] = None

    # Populated after fill
    fill_price: Optional[float] = None
    filled_qty: float = 0.0

    # Populated when matched to exchange position
    position_id: Optional[str] = None

    def __post_init__(self):
        """Ensure fill_price is always float and side is normalized."""
        if self.fill_price is not None:
            self.fill_price = float(self.fill_price)
        # Backward compat: convert legacy int side (1/2) to string
        if isinstance(self.side, int):
            self.side = "buy" if self.side == 1 else "sell"

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.qty

    @property
    def close_side(self) -> str:
        """Opposite side for closing this leg."""
        return "sell" if self.side == "buy" else "buy"

    @property
    def side_label(self) -> str:
        return self.side


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
        execution_mode:   "limit", "rfq", or None (auto-route)
        rfq_action:       "buy" or "sell" — passed to RFQExecutor.execute()
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
    execution_mode: Optional[str] = None  # "limit", "rfq", or None (auto-route)
    rfq_action: str = "buy"             # "buy" or "sell" — for the open
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
          - best BID for legs we'd SELL to close  (long positions, side="buy")
          - best ASK for legs we'd BUY to close   (short positions, side="sell")

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

            # To close: long (side="buy") → sell → need bid;
            #           short (side="sell") → buy back → need ask
            if leg.side == "buy":  # long — close by selling
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

            # PnL per leg: for a BUY (side="buy"), profit = (close - entry) * qty
            #              for a SELL (side="sell"), profit = (entry - close) * qty
            if leg.side == "buy":
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
                sign = 1 if leg.side == "buy" else -1  # buy = debit, sell = credit
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
                sign = 1 if leg.side == "buy" else -1
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
        execution_params, rfq_params, metadata internals).
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


__all__ = [
    "TradeState",
    "RFQParams",
    "TradeLeg",
    "ExitCondition",
    "TradeLifecycle",
]

