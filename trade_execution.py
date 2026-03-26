#!/usr/bin/env python3
"""
Trade Execution Module — Transport & Fill Management Layer

Provides three building blocks used by ExecutionRouter for limit-mode trades:

  TradeExecutor      — thin Coincall REST client: place_order, cancel_order,
                       get_order_status.  Not used for Deribit — that exchange
                       uses DeribitExecutorAdapter in exchanges/deribit/executor.py
                       which shares the ExchangeExecutor interface.

  ExecutionParams    — per-trade fill-management config: phases list (new) or
                       legacy flat timeout/requote fields.  Attached to
                       TradeLifecycle at create time by the strategy.

  LimitFillManager   — manages a batch of limit orders through their fill lifecycle.
                       Called by LifecycleEngine._check_open_fills and
                       ._check_close_fills every tick.

                       Open mode  (best_effort=False, default): atomic — any
                       pricing failure cancels the entire batch.
                       Close mode (best_effort=True): lenient — legs with bad
                       prices are skipped and logged; remaining legs are placed.
                       LifecycleEngine retries skipped legs on the next tick.

                       check() returns one of: "filled" | "requoted" | "failed" | "pending"
                       filled_legs / has_skipped_legs / skipped_symbols expose state.

Exchange-agnostic: LimitFillManager calls self._executor.place_order() which
is injected — the same code drives Coincall and Deribit without modification.
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
            "passive"    — best bid for buys, best ask for sells (most passive)
            "aggressive" — ask × (1 + buffer) for buys, bid / (1 + buffer) for sells
            "mid"        — (bid + ask) / 2
            "top_of_book" — best ask for buys, best bid for sells (no buffer)
            "mark"       — orderbook mark price (falls back to mid if unavailable)
            "fair"       — computed fair price (mark if between bid/ask, else mid).
                           Use fair_aggression to interpolate toward the aggressive
                           side: 0.0 = fair, 1.0 = top_of_book.  For sells:
                           price = fair - aggression * (fair - bid).  For buys:
                           price = fair + aggression * (ask - fair).  When ask is
                           missing (buy side, SL close), escalates using mark.
        duration_seconds: How long to stay in this phase before advancing.
            Must be ≥ 10 (one polling tick).
        buffer_pct: % buffer applied when pricing="aggressive" (default 2.0).
            Ignored by other pricing modes.
        fair_aggression: Interpolation factor for pricing="fair" (default 0.0).
            0.0 = at fair price, 1.0 = at top_of_book.  Ignored by other modes.
        reprice_interval: Seconds between cancel-and-requote within this phase
            (default 30.0).  Set to a value > duration_seconds to never reprice
            within the phase (place once, wait for fill or phase timeout).
        min_price_pct_of_fair: Optional floor for pricing="fair" sell orders.
            If set (e.g. 0.83), the computed price must be ≥ fair × this ratio.
            Returns None if the floor is not met — causing the order to be
            skipped/failed rather than placed at an unacceptable price.
            Has no effect on buy orders or other pricing modes.
        min_floor_price: Absolute minimum price (in BTC) used as a last-resort
            fallback when pricing would otherwise return None (e.g. no bids in
            orderbook).  If set (e.g. 0.0001), a None or zero computed price is
            replaced with this value instead of skipping the leg.  Useful for
            deep-OTM legs that may expire worthless — placing at the minimum
            tick ensures the order is visible without blocking the close.
            Has no effect when a valid price is already computed.
    """
    pricing: str = "aggressive"
    duration_seconds: float = 30.0
    buffer_pct: float = 2.0
    fair_aggression: float = 0.0
    reprice_interval: float = 30.0
    min_price_pct_of_fair: Optional[float] = None
    min_floor_price: Optional[float] = None

    def __post_init__(self):
        allowed = {"aggressive", "mid", "top_of_book", "mark", "passive", "fair"}
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

    Created fresh for each open or close attempt by ExecutionRouter.
    Stored in trade.metadata["_open_fill_mgr"] / "_close_fill_mgr"] so
    LifecycleEngine can call check() on it every tick until fills complete.

    Pricing modes (set per-phase via ExecutionPhase.pricing):
      passive, mid, top_of_book, aggressive, mark, fair

    Two execution modes selected by ExecutionParams:
      Legacy (phases=None)  — single aggressive phase, timeout-based requote.
      Phased (phases=list)  — walks through phases in order; each phase can
                              reprice within itself and then escalates on expiry.

    place_all() flags:
      best_effort=False (default, OPEN) — atomic: any failure cancels all.
      best_effort=True  (CLOSE)         — lenient: skip bad legs, place the rest.
          Skipped symbols available via has_skipped_legs / skipped_symbols.
          LifecycleEngine checks has_skipped_legs after "filled" to decide
          whether to go CLOSED or back to PENDING_CLOSE for the skipped legs.

    check() return values (called once per tick by LifecycleEngine):
      "filled"   — all placed legs filled → CLOSED (or PENDING_CLOSE if skips)
      "requoted" — timeout/phase expired, orders re-placed at fresh prices
      "failed"   — all phases/requote rounds exhausted → PENDING_CLOSE (close retry)
      "pending"  — still waiting for fills, no action needed

    This class does NOT own or mutate TradeLifecycle state.
    """

    def __init__(self, executor: "TradeExecutor", params: Optional[ExecutionParams] = None,
                 order_manager: Optional[Any] = None, market_data=None):
        self._executor = executor
        self._params = params or ExecutionParams()
        self._order_manager = order_manager
        self._market_data = market_data
        self._legs: List[_LegFillState] = []
        self._skipped_symbols: List[str] = []
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
                   best_effort: bool = False,
                   lifecycle_id: Optional[str] = None, purpose: Optional[Any] = None) -> bool:
        """
        Place initial limit orders for all legs.

        Args:
            legs: List of dicts with keys: symbol, qty, side, order_id (out).
                  Each dict is a TradeLeg-like object (duck-typed).
            reduce_only: If True, all orders are placed with reduceOnly flag.
            best_effort: If True (use for close orders), skip legs with bad prices
                  or rejected placements rather than aborting the entire batch.
                  Returns True if ≥1 order was placed; False if none could be placed.
                  Skipped symbols are accessible via skipped_symbols.
                  If False (default, for open orders), any failure cancels all
                  already-placed orders and returns False.
            lifecycle_id: Trade lifecycle ID (for OrderManager tracking).
            purpose: OrderPurpose enum value (for OrderManager tracking).

        Returns:
            True if all orders placed successfully (best_effort=False), or
            True if at least one order was placed (best_effort=True).
            False on total failure.
        """
        self._lifecycle_id = lifecycle_id
        self._purpose = purpose
        self._legs = []
        self._skipped_symbols = []
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

        # Pre-validate all prices before placing any orders.
        # In best_effort mode, skip legs with bad prices rather than aborting.
        leg_data = []
        for leg in legs:
            symbol = leg.symbol if hasattr(leg, 'symbol') else leg['symbol']
            qty = leg.qty if hasattr(leg, 'qty') else leg['qty']
            side = leg.side if hasattr(leg, 'side') else leg['side']
            price = self._get_price_for_current_mode(symbol, side)
            if price is None:
                if best_effort:
                    logger.warning(f"LimitFillManager: no orderbook price for {symbol} ({side}) — skipping leg (best_effort)")
                    self._skipped_symbols.append(symbol)
                    continue
                logger.error(f"LimitFillManager: no orderbook price for {symbol} ({side})")
                return False  # no orders placed yet — nothing to cancel
            if price <= 0:
                if best_effort:
                    logger.warning(f"LimitFillManager: price {price} <= 0 for {symbol} ({side}) — skipping leg (best_effort)")
                    self._skipped_symbols.append(symbol)
                    continue
                logger.error(f"LimitFillManager: computed price {price} <= 0 for {symbol} ({side}) — refusing to place order")
                return False
            leg_data.append((leg, symbol, qty, side, price))

        if not leg_data:
            logger.error("LimitFillManager: no placeable legs (all skipped or bad prices)")
            return False

        for idx, (leg, symbol, qty, side, price) in enumerate(leg_data):
            result = self._place_single(symbol, qty, side, price, reduce_only, idx)
            if not result:
                if best_effort:
                    logger.warning(f"LimitFillManager: placement rejected for {symbol} — skipping leg (best_effort)")
                    self._skipped_symbols.append(symbol)
                    continue
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

        if not self._legs:
            logger.error("LimitFillManager: no orders were successfully placed")
            return False

        if self._skipped_symbols:
            logger.warning(
                f"LimitFillManager: {len(self._legs)} order(s) placed, "
                f"{len(self._skipped_symbols)} skipped: {self._skipped_symbols}"
            )
        else:
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
            self._requote_unfilled(is_phase_transition=True)
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

    @property
    def has_skipped_legs(self) -> bool:
        """True if any legs were skipped during a best_effort place_all."""
        return bool(self._skipped_symbols)

    @property
    def skipped_symbols(self) -> List[str]:
        """Symbols skipped during a best_effort place_all (bad price or rejected)."""
        return list(self._skipped_symbols)

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

    def _requote_unfilled(self, is_phase_transition: bool = False) -> None:
        """Cancel stale orders and re-place at fresh prices (phase-aware).

        Args:
            is_phase_transition: True when called at a phase boundary (intentional
                aggression step). The directional guard is bypassed so that phase
                advances — which deliberately move the price closer to the market —
                are never blocked.  False (default) for within-phase repricing, where
                moving the price in the wrong direction (lower for sell, higher for
                buy) would harm fill quality without any strategic benefit.
        """
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
                if current_record and current_record.price > 0:
                    # Use relative tolerance (0.1%) to handle both USD and BTC prices
                    if abs(current_record.price - price) / current_record.price < 0.001:
                        logger.info(
                            f"LimitFillManager: skipping requote for {ls.symbol} — "
                            f"price unchanged @ {price}"
                        )
                        continue

                    # Directional guard (within-phase only): never reprice to a worse level.
                    # For sell orders, worse = lower price (less premium collected).
                    # For buy orders, worse = higher price (more cost to close).
                    # Phase transitions bypass this guard — they are intentional aggression steps.
                    if not is_phase_transition:
                        if ls.side == "sell" and price < current_record.price:
                            logger.info(
                                f"LimitFillManager: skipping within-phase reprice for {ls.symbol} — "
                                f"new price ${price:.4f} < current ${current_record.price:.4f} "
                                f"(sell directional guard)"
                            )
                            continue
                        elif ls.side == "buy" and price > current_record.price:
                            logger.info(
                                f"LimitFillManager: skipping within-phase reprice for {ls.symbol} — "
                                f"new price ${price:.4f} > current ${current_record.price:.4f} "
                                f"(buy directional guard)"
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
                            f"{ls.remaining_qty}x {ls.symbol} @ {price} "
                            f"(round {ls.requote_count}) [via OrderManager]"
                        )
                    else:
                        # requote returned None — check if order was actually filled
                        old_record = self._order_manager._orders.get(ls.order_id)
                        if old_record and old_record.status.value == "filled":
                            logger.info(f"LimitFillManager: {ls.symbol} filled during requote")
                        else:
                            logger.warning(f"LimitFillManager: {ls.symbol} requote failed (cancel+replace failed)")
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

            elif phase.pricing == "passive":
                if side == "buy" and best_bid is not None:
                    return best_bid
                elif side == "sell" and best_ask is not None:
                    return best_ask

            elif phase.pricing == "top_of_book":
                if side == "buy" and best_ask is not None:
                    return best_ask
                elif side == "sell" and best_bid is not None:
                    return best_bid

            elif phase.pricing == "mark":
                # Prefer BTC-native mark (Deribit); fall back to USD mark (Coincall)
                mark_btc = float(ob.get('_mark_btc', 0))
                if mark_btc > 0:
                    return mark_btc
                mark = float(ob.get('mark', 0))
                if mark > 0:
                    return mark
                # Fall back to mid if mark unavailable
                if best_bid is not None and best_ask is not None:
                    return (best_bid + best_ask) / 2

            elif phase.pricing == "fair":
                # Fair price: mark if between bid/ask, else mid.
                # fair_aggression interpolates toward the aggressive side:
                #   SELL: fair → bid  |  BUY: fair → ask
                mark = float(ob.get('mark', 0)) or float(ob.get('_mark_btc', 0))

                if best_bid is not None and best_ask is not None:
                    if best_bid <= mark <= best_ask:
                        fair = mark
                    else:
                        fair = (best_bid + best_ask) / 2
                elif best_bid is not None:
                    fair = max(mark, best_bid) if mark > 0 else best_bid
                elif mark > 0:
                    fair = mark
                else:
                    return None

                a = phase.fair_aggression
                if side == "sell":
                    spread = fair - best_bid if best_bid is not None else 0
                    price = fair - a * spread
                    if phase.min_price_pct_of_fair is not None:
                        floor = fair * phase.min_price_pct_of_fair
                        if price < floor:
                            logger.warning(
                                f"LimitFillManager: {symbol} computed price ${price:.4f} "
                                f"< floor ${floor:.4f} "
                                f"(fair=${fair:.4f} × {phase.min_price_pct_of_fair:.0%}) "
                                f"— refusing to place order"
                            )
                            return None
                    return price
                else:  # buy
                    if best_ask is not None:
                        spread = best_ask - fair
                        return fair + a * spread
                    elif mark > 0:
                        # No ask: escalate using mark (SL damage-control)
                        return mark * (1.0 + a * 0.2)
                    return fair

            price = None
        except Exception as e:
            logger.error(f"LimitFillManager: error computing {phase.pricing} price for {symbol}: {e}")
            price = None

        # min_floor_price: last-resort fallback for deep-OTM legs with no bids.
        # Only activates when the computed price is None or zero — never overrides
        # a valid price, and never interacts with min_price_pct_of_fair.
        if (price is None or price <= 0) and phase.min_floor_price is not None:
            logger.info(
                f"LimitFillManager: no valid price for {symbol} ({side}) "
                f"— using min_floor_price {phase.min_floor_price} BTC"
            )
            return phase.min_floor_price

        return price

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
