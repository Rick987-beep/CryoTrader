#!/usr/bin/env python3
"""
Trade Execution Module — Transport & Fill Management Layer

Provides:
  1. TradeExecutor  — thin API client (place, cancel, query orders)
  2. ExecutionParams — per-trade fill-management configuration
  3. LimitFillManager — tracks a set of pending leg orders, polls fills,
     and requotes on timeout.  Used by trade_lifecycle for "limit" mode.

Environment-agnostic — works the same for testnet and production.
The environment is controlled via config.py.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from config import BASE_URL, API_KEY, API_SECRET
from auth import CoincallAuth

logger = logging.getLogger(__name__)


class TradeExecutor:
    """Executes trades and manages orders"""

    def __init__(self):
        """Initialize trade executor with authenticated API client"""
        self.auth = CoincallAuth(API_KEY, API_SECRET, BASE_URL)

    def place_order(
        self,
        symbol: str,
        qty: float,
        side: int,
        order_type: int = 1,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        reduce_only: bool = False,  # BUG-2026-03-05: prevent close orders from building reverse positions
    ) -> Optional[Dict[str, Any]]:
        """Place a single order. Returns dict with orderId or None on error."""
        try:
            payload = {
                'symbol': symbol,
                'qty': qty,
                'tradeSide': side,
                'tradeType': order_type,
            }
            
            if price is not None:
                payload['price'] = price
            
            # BUG-2026-03-05: reduce_only ensures close orders can never exceed open position
            if reduce_only:
                payload['reduceOnly'] = 1
            
            if client_order_id:
                payload['clientOrderId'] = int(client_order_id)
            
            response = self.auth.post('/open/option/order/create/v1', payload)
            
            if self.auth.is_successful(response):
                order_id = response.get('data')
                logger.info(f"Order placed: {order_id} for {symbol}")
                return {'orderId': order_id}
            else:
                logger.error(f"Order failed for {symbol}: {response.get('msg')}")
                return None
        
        except Exception as e:
            logger.error(f"Exception placing order for {symbol}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order by ID
        
        Args:
            order_id: Order ID to cancel
            
        Returns:
            True if cancelled successfully
        """
        try:
            response = self.auth.post('/open/option/order/cancel/v1', {'orderId': int(order_id)})
            
            if self.auth.is_successful(response):
                logger.info(f"Order cancelled: {order_id}")
                return True
            else:
                logger.error(f"Failed to cancel order {order_id}: {response.get('msg')}")
                return False
        
        except Exception as e:
            logger.error(f"Exception cancelling order {order_id}: {e}")
            return False

    def get_order_status(self, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Get order status by ID.

        Uses the singleQuery endpoint:
            GET /open/option/order/singleQuery/v1?orderId={id}

        Returns fields like orderId, symbol, qty, fillQty, remainQty,
        price, avgPrice, state, tradeSide, etc.

        State enum (options):
            0=NEW, 1=FILLED, 2=PARTIALLY_FILLED, 3=CANCELED,
            4=PRE_CANCEL, 5=CANCELING, 6=INVALID, 10=CANCEL_BY_EXERCISE

        Args:
            order_id: Order ID

        Returns:
            Order information dict or None on error
        """
        try:
            response = self.auth.get(f'/open/option/order/singleQuery/v1?orderId={order_id}')

            if self.auth.is_successful(response):
                return response.get('data', {})
            else:
                logger.error(f"Failed to get order status for {order_id}: {response.get('msg')}")
                return None

        except Exception as e:
            logger.error(f"Exception getting order status for {order_id}: {e}")
            return None


# =============================================================================
# Execution Parameters — configurable per-trade
# =============================================================================

@dataclass
class ExecutionPhase:
    """
    One phase in a multi-phase execution plan.

    The LimitFillManager walks through phases in order.  When a phase's
    duration expires and legs are still unfilled, stale orders are cancelled
    and the manager advances to the next phase with new pricing.

    Attributes:
        pricing: How to price orders in this phase:
            "aggressive" — ask × (1 + buffer) for buys, bid / (1 + buffer) for sells
            "mid"        — (bid + ask) / 2
            "top_of_book" — best ask for buys, best bid for sells (no buffer)
            "mark"       — orderbook mark price (falls back to mid if unavailable)
        duration_seconds: How long to stay in this phase before advancing.
            Must be ≥ 10 (one polling tick).
        buffer_pct: % buffer applied when pricing="aggressive" (default 2.0).
            Ignored by other pricing modes.
        reprice_interval: Seconds between cancel-and-requote within this phase
            (default 30.0).  Set to a value > duration_seconds to never reprice
            within the phase (place once, wait for fill or phase timeout).
    """
    pricing: str = "aggressive"
    duration_seconds: float = 30.0
    buffer_pct: float = 2.0
    reprice_interval: float = 30.0

    def __post_init__(self):
        allowed = {"aggressive", "mid", "top_of_book", "mark"}
        if self.pricing not in allowed:
            raise ValueError(f"Unknown pricing '{self.pricing}', must be one of {allowed}")
        if self.duration_seconds < 10:
            self.duration_seconds = 10.0
        if self.reprice_interval < 10:
            self.reprice_interval = 10.0


@dataclass
class ExecutionParams:
    """
    Per-trade fill-management configuration.

    Strategies set these at trade-creation time to control how aggressively
    orders are filled.  Stored on TradeLifecycle so the LimitFillManager
    can read them.

    Two usage modes (backward-compatible):

    1. **Flat fields (legacy / default):** Set fill_timeout_seconds,
       aggressive_buffer_pct, max_requote_rounds.  This creates a single
       implicit aggressive phase that requotes every fill_timeout_seconds
       up to max_requote_rounds times — identical to the original behavior.

    2. **Phases list (new):** Provide an ordered list of ExecutionPhase
       objects.  Each phase defines its own pricing, duration, and reprice
       interval.  When all phases are exhausted without a fill, the manager
       returns "failed".  When phases is set, the flat fields are ignored.

    Attributes:
        fill_timeout_seconds: (Legacy) Seconds before requoting. Ignored if phases set.
        aggressive_buffer_pct: (Legacy) % beyond best price. Ignored if phases set.
        max_requote_rounds: (Legacy) Max requote cycles. Ignored if phases set.
        phases: Ordered list of ExecutionPhase objects. None = use legacy flat fields.
    """
    fill_timeout_seconds: float = 30.0
    aggressive_buffer_pct: float = 2.0
    max_requote_rounds: int = 10
    phases: Optional[List[ExecutionPhase]] = None


# =============================================================================
# Limit Fill Manager — tracks pending orders, polls fills, requotes on timeout
# =============================================================================

@dataclass
class _LegFillState:
    """Internal: tracks one leg's order and fill progress."""
    symbol: str
    qty: float
    side: str          # "buy" or "sell"
    order_id: Optional[str] = None
    filled_qty: float = 0.0
    fill_price: Optional[float] = None
    requote_count: int = 0
    started_at: float = field(default_factory=time.time)

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.qty

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)

    @property
    def side_label(self) -> str:
        return self.side


class LimitFillManager:
    """
    Manages fill detection and requoting for a set of limit-order legs.

    Supports two execution modes (selected automatically by ExecutionParams):

    **Legacy mode** (params.phases is None):
      Single aggressive pricing phase with timeout-based requoting.
      Identical to the original behaviour.

    **Phased mode** (params.phases is a list):
      Walks through an ordered list of ExecutionPhase objects.  Each phase
      defines its own pricing strategy, duration, and reprice interval.
      When a phase expires, orders are cancelled and the next phase begins.
      After the last phase exhausts, returns ``"failed"``.

    Lifecycle:
      1. Caller creates the manager with an executor + params.
      2. ``place_all(legs)`` places initial orders for every leg.
      3. Each tick, caller invokes ``check()`` which:
         a. Polls order status for every unfilled leg.
         b. If all filled → returns ``"filled"``.
         c. If phase/timeout elapsed → advance or requote → returns ``"requoted"``.
         d. If all phases/requote rounds exhausted → returns ``"failed"``.
         e. Otherwise → returns ``"pending"``.
      4. ``cancel_all()`` cancels any outstanding orders (for cleanup).
      5. ``filled_legs`` returns the final fill details.

    This class does NOT own the TradeLifecycle state machine — it is a
    helper that trade_lifecycle drives via its tick loop.
    """

    def __init__(self, executor: "TradeExecutor", params: Optional[ExecutionParams] = None,
                 order_manager: Optional[Any] = None, market_data=None):
        self._executor = executor
        self._params = params or ExecutionParams()
        self._order_manager = order_manager
        self._market_data = market_data
        self._legs: List[_LegFillState] = []
        self._round_started_at: float = time.time()

        # Phased execution state
        self._using_phases: bool = self._params.phases is not None and len(self._params.phases) > 0
        self._phase_index: int = 0
        self._phase_started_at: float = time.time()
        self._last_reprice_at: float = time.time()

        # OrderManager context (set by place_all when order_manager is active)
        self._lifecycle_id: Optional[str] = None
        self._purpose: Optional[Any] = None

    # -- Public API -----------------------------------------------------------

    @property
    def _current_phase(self) -> Optional[ExecutionPhase]:
        """Current execution phase, or None if not using phases or all exhausted."""
        if not self._using_phases:
            return None
        phases = self._params.phases
        if self._phase_index < len(phases):
            return phases[self._phase_index]
        return None

    def place_all(self, legs: List[Dict[str, Any]], reduce_only: bool = False,
                   lifecycle_id: Optional[str] = None, purpose: Optional[Any] = None) -> bool:
        """
        Place initial limit orders for all legs.

        Args:
            legs: List of dicts with keys: symbol, qty, side, order_id (out).
                  Each dict is a TradeLeg-like object (duck-typed).
            reduce_only: If True, all orders are placed with reduceOnly flag.
            lifecycle_id: Trade lifecycle ID (for OrderManager tracking).
            purpose: OrderPurpose enum value (for OrderManager tracking).

        Returns:
            True if all orders placed successfully.
            On failure, already-placed orders are cancelled.
        """
        self._lifecycle_id = lifecycle_id
        self._purpose = purpose
        self._legs = []
        self._reduce_only = reduce_only  # BUG-2026-03-05: remember for requotes
        now = time.time()
        self._round_started_at = now
        self._phase_started_at = now
        self._last_reprice_at = now
        self._phase_index = 0

        if self._using_phases:
            phase = self._current_phase
            phase_label = f"phase 1/{len(self._params.phases)} ({phase.pricing})"
        else:
            phase_label = "aggressive (legacy)"

        # BUG-2026-03-05: pre-validate all prices before placing any orders.
        # Prevents partial placement when one leg has no orderbook liquidity.
        leg_data = []
        for leg in legs:
            symbol = leg.symbol if hasattr(leg, 'symbol') else leg['symbol']
            qty = leg.qty if hasattr(leg, 'qty') else leg['qty']
            side = leg.side if hasattr(leg, 'side') else leg['side']
            price = self._get_price_for_current_mode(symbol, side)
            if price is None:
                logger.error(f"LimitFillManager: no orderbook price for {symbol} ({side})")
                return False  # no orders placed yet — nothing to cancel
            leg_data.append((leg, symbol, qty, side, price))

        for idx, (leg, symbol, qty, side, price) in enumerate(leg_data):
            result = self._place_single(symbol, qty, side, price, reduce_only, idx)
            if not result:
                logger.error(f"LimitFillManager: failed to place order for {symbol}")
                self.cancel_all()
                return False

            state = _LegFillState(
                symbol=symbol, qty=qty, side=side,
                order_id=str(result.get('orderId', '') if isinstance(result, dict) else getattr(result, 'order_id', '')),
            )
            self._legs.append(state)

            # Write order_id back to the caller's leg object
            if hasattr(leg, 'order_id'):
                leg.order_id = state.order_id
            side_label = state.side_label
            logger.info(
                f"LimitFillManager: placed {side_label} {qty}x {symbol} @ ${price} "
                f"(order {state.order_id}) [{phase_label}]"
            )

        logger.info(f"LimitFillManager: all {len(self._legs)} orders placed, awaiting fills [{phase_label}]")
        return True

    def check(self) -> str:
        """
        Poll fills and handle timeouts.  Call once per tick.

        Returns:
            "filled"   — all legs filled
            "requoted" — timeout hit, unfilled orders cancelled and re-placed
            "failed"   — max requote rounds exhausted or unrecoverable error
            "pending"  — still waiting for fills
        """
        # 1. Poll each unfilled leg
        self._poll_fills()

        # 2. All filled?
        if all(ls.is_filled for ls in self._legs):
            return "filled"

        # 3. Timeout / phase advancement
        if self._using_phases:
            return self._check_phased()
        else:
            return self._check_legacy()

    def _poll_fills(self) -> None:
        """Poll order status for all unfilled legs.

        When OrderManager is active, reads status from the ledger (which was
        already updated by poll_all() at the top of tick).  Otherwise, polls
        the exchange directly via TradeExecutor.
        """
        for ls in self._legs:
            if ls.is_filled or not ls.order_id:
                continue
            try:
                if self._order_manager:
                    record = self._order_manager.poll_order(ls.order_id)
                    if record:
                        if record.filled_qty > ls.filled_qty:
                            ls.filled_qty = record.filled_qty
                            ls.fill_price = record.avg_fill_price or ls.fill_price
                            logger.info(
                                f"LimitFillManager: {ls.symbol} filled "
                                f"{ls.filled_qty}/{ls.qty} @ {ls.fill_price}"
                            )
                        if record.is_terminal and not ls.is_filled:
                            logger.warning(
                                f"LimitFillManager: {ls.symbol} order {ls.order_id} "
                                f"reached terminal state {record.status.value} "
                                f"(filled {ls.filled_qty}/{ls.qty})"
                            )
                else:
                    info = self._executor.get_order_status(ls.order_id)
                    if info:
                        executed = float(info.get('fillQty', 0))
                        if executed > ls.filled_qty:
                            ls.filled_qty = executed
                            ls.fill_price = float(info.get('avgPrice', 0)) or ls.fill_price
                            logger.info(
                                f"LimitFillManager: {ls.symbol} filled "
                                f"{ls.filled_qty}/{ls.qty} @ {ls.fill_price}"
                            )
                        # Detect externally-cancelled orders
                        state_code = info.get('state')
                        if state_code == 3 and not ls.is_filled:
                            logger.warning(
                                f"LimitFillManager: {ls.symbol} order {ls.order_id} was cancelled externally "
                                f"(filled {ls.filled_qty}/{ls.qty})"
                            )
            except Exception as e:
                logger.error(f"LimitFillManager: error checking {ls.order_id}: {e}")

    def _check_legacy(self) -> str:
        """Original timeout-based requoting logic (when phases is None)."""
        elapsed = time.time() - self._round_started_at
        if elapsed > self._params.fill_timeout_seconds:
            unfilled = [ls for ls in self._legs if not ls.is_filled]
            if any(ls.requote_count >= self._params.max_requote_rounds for ls in unfilled):
                logger.error(
                    f"LimitFillManager: max requote rounds "
                    f"({self._params.max_requote_rounds}) exhausted"
                )
                return "failed"

            logger.warning(
                f"LimitFillManager: timeout ({elapsed:.0f}s > "
                f"{self._params.fill_timeout_seconds}s) — requoting unfilled legs"
            )
            self._requote_unfilled()
            return "requoted"

        return "pending"

    def _check_phased(self) -> str:
        """Phase-based execution: advance through phases, reprice within phases."""
        now = time.time()
        phase = self._current_phase

        if phase is None:
            # All phases exhausted
            logger.error("LimitFillManager: all execution phases exhausted — failed")
            return "failed"

        phase_elapsed = now - self._phase_started_at
        reprice_elapsed = now - self._last_reprice_at

        # Phase expired → advance to next phase
        if phase_elapsed >= phase.duration_seconds:
            self._phase_index += 1
            next_phase = self._current_phase
            if next_phase is None:
                logger.error("LimitFillManager: all execution phases exhausted — failed")
                return "failed"

            logger.info(
                f"LimitFillManager: phase {self._phase_index}/{len(self._params.phases)} "
                f"({phase.pricing}) expired after {phase_elapsed:.0f}s "
                f"→ advancing to phase {self._phase_index + 1} ({next_phase.pricing})"
            )
            self._phase_started_at = now
            self._last_reprice_at = now
            self._requote_unfilled()
            return "requoted"

        # Within-phase reprice interval elapsed → reprice at same pricing
        if reprice_elapsed >= phase.reprice_interval:
            logger.info(
                f"LimitFillManager: repricing within phase {self._phase_index + 1} "
                f"({phase.pricing}) after {reprice_elapsed:.0f}s"
            )
            self._last_reprice_at = now
            self._requote_unfilled()
            return "requoted"

        return "pending"

    def cancel_all(self) -> None:
        """Cancel any outstanding unfilled orders."""
        for ls in self._legs:
            if ls.order_id and not ls.is_filled:
                try:
                    if self._order_manager:
                        self._order_manager.cancel_order(ls.order_id)
                    else:
                        self._executor.cancel_order(ls.order_id)
                    logger.info(f"LimitFillManager: cancelled {ls.order_id} for {ls.symbol}")
                except Exception as e:
                    logger.warning(f"LimitFillManager: cancel failed for {ls.order_id}: {e}")

    @property
    def all_filled(self) -> bool:
        return all(ls.is_filled for ls in self._legs)

    @property
    def filled_legs(self) -> List[_LegFillState]:
        """Read-only access to leg states (for extracting fill details)."""
        return list(self._legs)

    @property
    def partially_filled_legs(self) -> List[_LegFillState]:
        """Legs that have some but not all qty filled."""
        return [ls for ls in self._legs if ls.filled_qty > 0 and not ls.is_filled]

    @property
    def unfilled_legs(self) -> List[_LegFillState]:
        """Legs with zero fills."""
        return [ls for ls in self._legs if ls.filled_qty == 0]

    # -- Internal -------------------------------------------------------------

    def _place_single(self, symbol: str, qty: float, side: str,
                      price: float, reduce_only: bool, leg_index: int) -> Optional[Any]:
        """Place a single order — routes through OrderManager if available."""
        if self._order_manager and self._lifecycle_id and self._purpose:
            record = self._order_manager.place_order(
                lifecycle_id=self._lifecycle_id,
                leg_index=leg_index,
                purpose=self._purpose,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
            if record:
                return {"orderId": record.order_id}
            return None
        else:
            return self._executor.place_order(
                symbol=symbol, qty=qty, side=side, order_type=1, price=price,
                reduce_only=reduce_only,  # BUG-2026-03-05: pass reduce_only to exchange
            )

    def _requote_unfilled(self) -> None:
        """Cancel stale orders and re-place at fresh prices (phase-aware)."""
        self._round_started_at = time.time()  # reset timeout for legacy mode

        for idx, ls in enumerate(self._legs):
            if ls.is_filled:
                continue
            if not ls.order_id:
                continue

            price = self._get_price_for_current_mode(ls.symbol, ls.side)
            if price is None:
                logger.error(f"LimitFillManager: no price for {ls.symbol} on requote")
                continue

            # Skip requote if price hasn't changed — avoids unnecessary cancel+replace API calls
            if self._order_manager:
                current_record = self._order_manager._orders.get(ls.order_id)
                if current_record and abs(current_record.price - price) < 0.01:
                    logger.info(
                        f"LimitFillManager: skipping requote for {ls.symbol} — "
                        f"price unchanged @ ${price}"
                    )
                    continue

            if self._order_manager:
                # Use OrderManager's atomic requote (cancel + replace + chain)
                try:
                    new_record = self._order_manager.requote_order(
                        ls.order_id, new_price=price, new_qty=ls.remaining_qty,
                    )
                    if new_record:
                        ls.order_id = new_record.order_id
                        ls.requote_count += 1
                        # Sync any fills captured during the poll inside requote
                        if new_record.filled_qty > 0:
                            ls.filled_qty += new_record.filled_qty
                        logger.info(
                            f"LimitFillManager: requoted {ls.side_label} "
                            f"{ls.remaining_qty}x {ls.symbol} @ ${price} "
                            f"(round {ls.requote_count}) [via OrderManager]"
                        )
                    else:
                        # requote returned None — order was fully filled during poll
                        logger.info(f"LimitFillManager: {ls.symbol} filled during requote")
                except Exception as e:
                    logger.error(f"LimitFillManager: requote exception for {ls.symbol}: {e}")
            else:
                # Legacy path: direct cancel + re-place
                try:
                    self._executor.cancel_order(ls.order_id)
                    logger.info(f"LimitFillManager: cancelled stale order {ls.order_id} for {ls.symbol}")
                except Exception as e:
                    logger.warning(f"LimitFillManager: cancel failed for {ls.order_id}: {e}")

                try:
                    result = self._executor.place_order(
                        symbol=ls.symbol,
                        qty=ls.remaining_qty,
                        side=ls.side,
                        order_type=1,
                        price=price,
                        reduce_only=getattr(self, '_reduce_only', False),  # BUG-2026-03-05
                    )
                    if result:
                        ls.order_id = str(result.get('orderId', ''))
                        ls.requote_count += 1
                        logger.info(
                            f"LimitFillManager: requoted {ls.side_label} "
                            f"{ls.remaining_qty}x {ls.symbol} @ ${price} "
                            f"(round {ls.requote_count})"
                        )
                    else:
                        logger.error(f"LimitFillManager: requote failed for {ls.symbol}")
                except Exception as e:
                    logger.error(f"LimitFillManager: requote exception for {ls.symbol}: {e}")

    # -- Pricing Helpers -------------------------------------------------------

    def _get_price_for_current_mode(self, symbol: str, side: str) -> Optional[float]:
        """
        Get order price based on the current execution mode.

        In phased mode: delegates to the current phase's pricing strategy.
        In legacy mode: uses aggressive pricing with buffer.
        """
        if self._using_phases:
            phase = self._current_phase
            if phase is not None:
                return self._get_phased_price(symbol, side, phase)
            # Fallback if phases exhausted (shouldn't happen, but safe)
            return self._get_aggressive_price(symbol, side)
        else:
            return self._get_aggressive_price(symbol, side)

    def _get_phased_price(self, symbol: str, side: str, phase: ExecutionPhase) -> Optional[float]:
        """Compute price according to the phase's pricing strategy."""
        try:
            ob = self._market_data.get_option_orderbook(symbol)
            if not ob:
                return None

            best_ask = float(ob['asks'][0]['price']) if ob.get('asks') else None
            best_bid = float(ob['bids'][0]['price']) if ob.get('bids') else None

            if phase.pricing == "aggressive":
                buffer = 1 + (phase.buffer_pct / 100.0)
                if side == "buy" and best_ask is not None:
                    return best_ask * buffer
                elif side == "sell" and best_bid is not None:
                    return best_bid / buffer

            elif phase.pricing == "mid":
                if best_bid is not None and best_ask is not None:
                    return (best_bid + best_ask) / 2

            elif phase.pricing == "top_of_book":
                if side == "buy" and best_ask is not None:
                    return best_ask
                elif side == "sell" and best_bid is not None:
                    return best_bid

            elif phase.pricing == "mark":
                mark = float(ob.get('mark', 0))
                if mark > 0:
                    return mark
                # Fall back to mid if mark unavailable
                if best_bid is not None and best_ask is not None:
                    return (best_bid + best_ask) / 2

            return None
        except Exception as e:
            logger.error(f"LimitFillManager: error computing {phase.pricing} price for {symbol}: {e}")
            return None

    def _get_aggressive_price(self, symbol: str, side: str) -> Optional[float]:
        """Fetch best bid/ask and apply aggressive buffer (legacy mode)."""
        try:
            ob = self._market_data.get_option_orderbook(symbol)
            if not ob:
                return None

            buffer = 1 + (self._params.aggressive_buffer_pct / 100.0)

            if side == "buy" and ob.get('asks'):
                raw = float(ob['asks'][0]['price'])
                return raw * buffer
            elif side == "sell" and ob.get('bids'):
                raw = float(ob['bids'][0]['price'])
                return raw / buffer

            return None
        except Exception as e:
            logger.error(f"LimitFillManager: error fetching price for {symbol}: {e}")
            return None
