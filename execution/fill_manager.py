"""
execution/fill_manager.py — FillManager: typed, N-leg fill lifecycle.

Returns FillResult (typed enum status).  Uses PricingEngine for all price
computation.  Requires OrderManager.

Phase-aware:
  - Walks through phases in order, each with its own pricing / duration.
  - Within-phase repricing at configurable intervals.
  - Grace tick: on phase exhaustion, one extra tick before FAILED to catch
    late fills from exchange reporting lag.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from execution.currency import Currency, OrderbookSnapshot, Price
from execution.fees import extract_fee, sum_fees
from execution.fill_result import FillResult, FillStatus, LegFillSnapshot
from execution.pricing import PricingEngine
from execution.profiles import ExecutionProfile, PhaseConfig

if TYPE_CHECKING:
    from order_manager import OrderManager, OrderPurpose

logger = logging.getLogger(__name__)
_execution_logger = logging.getLogger("ct.execution")


# ---------------------------------------------------------------------------
# ExecutionParams → ExecutionProfile bridge
# ---------------------------------------------------------------------------

def _bridge_params_to_profile(params: Any) -> ExecutionProfile:
    """Convert a legacy ExecutionParams to an ExecutionProfile.

    If params already has .phases, convert each ExecutionPhase to PhaseConfig.
    Otherwise, create a single aggressive phase from flat fields.
    """
    if hasattr(params, "phases") and params.phases:
        phases = []
        for ep in params.phases:
            phases.append(PhaseConfig(
                pricing=ep.pricing,
                duration_seconds=ep.duration_seconds,
                buffer_pct=ep.buffer_pct,
                fair_aggression=ep.fair_aggression,
                reprice_interval=ep.reprice_interval,
                min_price_pct_of_fair=getattr(ep, "min_price_pct_of_fair", None),
                min_floor_price=getattr(ep, "min_floor_price", None),
            ))
        return ExecutionProfile(
            name="_bridged",
            open_phases=phases,
            close_phases=phases,
        )
    # Legacy flat fields → single aggressive phase
    return ExecutionProfile(
        name="_bridged_legacy",
        open_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=params.fill_timeout_seconds,
            buffer_pct=params.aggressive_buffer_pct,
            reprice_interval=params.fill_timeout_seconds,
        )],
        close_phases=[PhaseConfig(
            pricing="aggressive",
            duration_seconds=params.fill_timeout_seconds,
            buffer_pct=params.aggressive_buffer_pct,
            reprice_interval=params.fill_timeout_seconds,
        )],
    )


# ---------------------------------------------------------------------------
# Internal leg state
# ---------------------------------------------------------------------------

@dataclass
class _LegState:
    """Internal mutable tracking for one leg."""
    symbol: str
    qty: float
    side: str
    leg_index: int
    order_id: Optional[str] = None
    filled_qty: float = 0.0
    fill_price: Optional[float] = None
    fee: Optional[Price] = None
    requote_count: int = 0
    skipped: bool = False
    skip_reason: Optional[str] = None
    _fill_baseline: float = 0.0

    @property
    def is_filled(self) -> bool:
        return self.filled_qty >= self.qty

    @property
    def remaining_qty(self) -> float:
        return max(0.0, self.qty - self.filled_qty)


# ---------------------------------------------------------------------------
# FillManager
# ---------------------------------------------------------------------------

class FillManager:
    """Manages fill detection and repricing for a batch of limit-order legs.

    Created fresh for each open or close attempt by Router.
    Stored on the trade lifecycle so LifecycleEngine can call check()
    every tick until fills complete.
    """

    def __init__(
        self,
        order_manager: "OrderManager",
        market_data: Any,
        profile: Optional[ExecutionProfile] = None,
        params: Any = None,
        direction: str = "open",
    ):
        if profile is None and params is not None:
            profile = _bridge_params_to_profile(params)
        if profile is None:
            # Bare minimum: single aggressive phase
            profile = ExecutionProfile(
                name="_default",
                open_phases=[PhaseConfig()],
                close_phases=[PhaseConfig()],
            )

        self._order_manager = order_manager
        self._market_data = market_data
        self._profile = profile
        self._pricing_engine = PricingEngine()
        self._direction = direction

        # Select phases based on direction
        self._phases: List[PhaseConfig] = (
            profile.open_phases if direction == "open" else profile.close_phases
        )
        if not self._phases:
            self._phases = [PhaseConfig()]

        self._best_effort: bool = (
            not profile.open_atomic if direction == "open"
            else profile.close_best_effort
        )

        # State
        self._legs: List[_LegState] = []
        self._skipped_symbols: List[str] = []
        self._phase_index: int = 0
        self._phase_started_at: float = 0.0
        self._last_reprice_at: float = 0.0
        self._started_at: float = 0.0
        self._lifecycle_id: Optional[str] = None
        self._purpose: Optional["OrderPurpose"] = None
        self._reduce_only: bool = False
        self._grace_exhausted: bool = False
        self._detected_currency: Optional[Currency] = None

    # -- Public API -----------------------------------------------------------

    @property
    def detected_currency(self) -> Optional[Currency]:
        """Currency detected from the first leg's orderbook. Available after place_all()."""
        return self._detected_currency

    @property
    def _current_phase(self) -> Optional[PhaseConfig]:
        if self._phase_index < len(self._phases):
            return self._phases[self._phase_index]
        return None

    def place_all(
        self,
        legs: List[Any],
        lifecycle_id: str,
        purpose: "OrderPurpose",
        reduce_only: bool = False,
    ) -> FillResult:
        """Place initial limit orders for all legs.

        Args:
            legs: TradeLeg objects (or dicts with symbol/qty/side).
            lifecycle_id: Trade lifecycle ID for OrderManager tracking.
            purpose: OrderPurpose enum value.
            reduce_only: If True, all orders placed with reduce_only flag.

        Returns:
            FillResult with status PENDING (success), FAILED (total failure),
            or REFUSED (best_effort but nothing could be placed).
        """
        self._lifecycle_id = lifecycle_id
        self._purpose = purpose
        self._reduce_only = reduce_only
        self._legs = []
        self._skipped_symbols = []

        now = time.time()
        self._started_at = now
        self._phase_started_at = now
        self._last_reprice_at = now
        self._phase_index = 0
        self._grace_exhausted = False

        phase = self._current_phase
        phase_label = f"phase 1/{len(self._phases)} ({phase.pricing})"

        _execution_logger.info({
            "event": "PHASE_ENTERED",
            "trade_id": lifecycle_id,
            "phase_index": 1,
            "phase_total": len(self._phases),
            "pricing": phase.pricing,
            "direction": self._direction,
        })

        # Pre-validate prices for all legs
        leg_data: List[tuple] = []
        for idx, leg in enumerate(legs):
            symbol = leg.symbol if hasattr(leg, "symbol") else leg["symbol"]
            qty = leg.qty if hasattr(leg, "qty") else leg["qty"]
            side = leg.side if hasattr(leg, "side") else leg["side"]

            price = self._compute_price(symbol, side, phase)

            if price is None or price.amount <= 0:
                reason = f"no valid price ({price})" if price is not None else "no orderbook"
                if self._best_effort:
                    logger.warning(
                        f"FillManager: {symbol} ({side}) — {reason}, skipping (best_effort)"
                    )
                    self._skipped_symbols.append(symbol)
                    self._legs.append(_LegState(
                        symbol=symbol, qty=qty, side=side, leg_index=idx,
                        skipped=True, skip_reason=reason,
                    ))
                    continue
                logger.error(f"FillManager: {symbol} ({side}) — {reason}")
                return self._make_result(FillStatus.REFUSED, error=reason)

            leg_data.append((idx, leg, symbol, qty, side, price))

        if not leg_data:
            return self._make_result(
                FillStatus.REFUSED,
                error="no placeable legs (all skipped or bad prices)",
            )

        # Cache the detected currency from the first price
        if leg_data and self._detected_currency is None:
            first_price = leg_data[0][5]  # price from first leg
            if isinstance(first_price, Price):
                self._detected_currency = first_price.currency

        # Place orders
        for idx, leg, symbol, qty, side, price in leg_data:
            record = self._order_manager.place_order(
                lifecycle_id=lifecycle_id,
                leg_index=idx,
                purpose=purpose,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                reduce_only=reduce_only,
            )
            if not record:
                if self._best_effort:
                    logger.warning(
                        f"FillManager: placement rejected for {symbol} — skipping (best_effort)"
                    )
                    self._skipped_symbols.append(symbol)
                    self._legs.append(_LegState(
                        symbol=symbol, qty=qty, side=side, leg_index=idx,
                        skipped=True, skip_reason="placement_rejected",
                    ))
                    continue
                # Atomic mode: cancel everything placed so far
                logger.error(f"FillManager: placement failed for {symbol} — cancelling all")
                self._cancel_placed()
                return self._make_result(
                    FillStatus.REFUSED, error=f"placement_failed:{symbol}"
                )

            ls = _LegState(
                symbol=symbol, qty=qty, side=side, leg_index=idx,
                order_id=record.order_id,
                filled_qty=record.filled_qty,
                fill_price=record.avg_fill_price,
                fee=record.fee,
            )
            self._legs.append(ls)

            # Write order_id back to caller's leg object
            if hasattr(leg, "order_id"):
                leg.order_id = record.order_id

            logger.info(
                f"FillManager: placed {side} {qty}x {symbol} @ {price} "
                f"(order {record.order_id}) [{phase_label}]"
            )

        placed = [l for l in self._legs if not l.skipped]
        if not placed:
            return self._make_result(
                FillStatus.REFUSED, error="no orders placed"
            )

        # Check if immediate fills completed everything
        if all(l.is_filled for l in placed):
            return self._make_result(FillStatus.FILLED)

        if self._skipped_symbols:
            logger.warning(
                f"FillManager: {len(placed)} placed, "
                f"{len(self._skipped_symbols)} skipped: {self._skipped_symbols}"
            )

        return self._make_result(FillStatus.PENDING)

    def check(self) -> FillResult:
        """Poll fills and handle timeouts. Call once per tick.

        Returns FillResult with status:
          FILLED   — all legs filled
          REQUOTED — timeout hit, unfilled orders cancelled and re-placed
          FAILED   — max phases exhausted or unrecoverable error
          PENDING  — still waiting
        """
        # 1. Poll each unfilled leg
        self._poll_fills()

        # 2. All filled?
        placed = [l for l in self._legs if not l.skipped]
        if all(l.is_filled for l in placed):
            return self._make_result(FillStatus.FILLED)

        # 3. Phase timeout / advancement
        result_status = self._check_phases()

        # 4. Grace tick on FAILED
        if result_status == FillStatus.FAILED:
            if not self._grace_exhausted:
                self._grace_exhausted = True
                logger.info(
                    "FillManager: phases exhausted — grace tick before failing "
                    "(exchange fill reporting lag protection)"
                )
                return self._make_result(FillStatus.PENDING)
            # Grace tick elapsed — final poll
            self._poll_fills()
            if all(l.is_filled for l in placed):
                logger.info("FillManager: last-chance poll caught late fill(s)")
                return self._make_result(FillStatus.FILLED)

        return self._make_result(result_status)

    def cancel_all(self) -> None:
        """Cancel any outstanding unfilled orders."""
        for ls in self._legs:
            if ls.order_id and not ls.is_filled and not ls.skipped:
                try:
                    self._order_manager.cancel_order(ls.order_id)
                    logger.info(f"FillManager: cancelled {ls.order_id} for {ls.symbol}")
                except Exception as e:
                    logger.warning(f"FillManager: cancel failed for {ls.order_id}: {e}")

    @property
    def all_filled(self) -> bool:
        placed = [l for l in self._legs if not l.skipped]
        return all(l.is_filled for l in placed)

    @property
    def has_skipped_legs(self) -> bool:
        return bool(self._skipped_symbols)

    @property
    def skipped_symbols(self) -> List[str]:
        return list(self._skipped_symbols)

    @property
    def filled_legs(self) -> List[_LegState]:
        return [l for l in self._legs if l.is_filled]

    @property
    def legs(self) -> List[_LegState]:
        return list(self._legs)

    # -- Internal: result building -------------------------------------------

    def _make_result(
        self,
        status: FillStatus,
        error: Optional[str] = None,
    ) -> FillResult:
        """Build a FillResult from current state."""
        leg_snaps = []
        fees: List[Optional[Price]] = []
        for ls in self._legs:
            fill_price: Optional[Price] = None
            if ls.fill_price is not None:
                # Detect currency from orderbook
                currency = self._detect_currency(ls.symbol)
                fill_price = Price(ls.fill_price, currency)

            snap = LegFillSnapshot(
                symbol=ls.symbol,
                side=ls.side,
                qty=ls.qty,
                filled_qty=ls.filled_qty,
                fill_price=fill_price,
                order_id=ls.order_id,
                skipped=ls.skipped,
                skip_reason=ls.skip_reason,
                fee=ls.fee,
            )
            leg_snaps.append(snap)
            fees.append(ls.fee)

        phase = self._current_phase
        return FillResult(
            status=status,
            legs=leg_snaps,
            phase_index=self._phase_index + 1,
            phase_total=len(self._phases),
            phase_pricing=phase.pricing if phase else self._phases[-1].pricing,
            elapsed_seconds=time.time() - self._started_at,
            error=error,
            total_fees=sum_fees(fees),
        )

    def _detect_currency(self, symbol: str) -> Currency:
        """Detect currency from orderbook for a symbol."""
        try:
            ob = self._market_data.get_option_orderbook(symbol)
            if ob:
                raw = ob.get("_currency")
                if raw:
                    return Currency(raw)
                if float(ob.get("_mark_btc", 0)) > 0:
                    return Currency.BTC
        except Exception:
            pass
        return Currency.USD

    # -- Internal: fill polling -----------------------------------------------

    def _poll_fills(self) -> None:
        """Poll OrderManager for fill updates on all legs."""
        for ls in self._legs:
            if ls.is_filled or not ls.order_id or ls.skipped:
                continue
            try:
                record = self._order_manager.poll_order(ls.order_id)
                if record:
                    new_total = ls._fill_baseline + record.filled_qty
                    if new_total > ls.filled_qty:
                        ls.filled_qty = new_total
                        ls.fill_price = record.avg_fill_price or ls.fill_price
                        logger.info(
                            f"FillManager: {ls.symbol} filled "
                            f"{ls.filled_qty}/{ls.qty} @ {ls.fill_price}"
                        )
                    elif ls.fill_price is None and record.avg_fill_price:
                        ls.fill_price = record.avg_fill_price
                    # Capture fee from order record if available
                    if record.fee and ls.fee is None:
                        ls.fee = record.fee
                    if record.is_terminal and not ls.is_filled:
                        logger.warning(
                            f"FillManager: {ls.symbol} order {ls.order_id} "
                            f"terminal {record.status.value} "
                            f"(filled {ls.filled_qty}/{ls.qty})"
                        )
            except Exception as e:
                logger.error(f"FillManager: error polling {ls.order_id}: {e}")

    # -- Internal: phase management -------------------------------------------

    def _check_phases(self) -> FillStatus:
        """Check phase timeout and advance or reprice as needed."""
        now = time.time()
        phase = self._current_phase

        if phase is None:
            logger.error("FillManager: all execution phases exhausted")
            return FillStatus.FAILED

        phase_elapsed = now - self._phase_started_at
        reprice_elapsed = now - self._last_reprice_at

        # Phase expired → advance
        if phase_elapsed >= phase.duration_seconds:
            self._phase_index += 1
            next_phase = self._current_phase
            if next_phase is None:
                logger.error("FillManager: all execution phases exhausted")
                if self._lifecycle_id:
                    _execution_logger.info({
                        "event": "EXEC_FAILED",
                        "trade_id": self._lifecycle_id,
                        "reason": "all_phases_exhausted",
                    })
                return FillStatus.FAILED

            logger.info(
                f"FillManager: phase {self._phase_index}/{len(self._phases)} "
                f"expired → advancing to phase {self._phase_index + 1} "
                f"({next_phase.pricing})"
            )
            if self._lifecycle_id:
                _execution_logger.info({
                    "event": "PHASE_ADVANCED",
                    "trade_id": self._lifecycle_id,
                    "from_phase_index": self._phase_index,
                    "to_phase_index": self._phase_index + 1,
                    "to_pricing": next_phase.pricing,
                })
            self._phase_started_at = now
            self._last_reprice_at = now
            self._requote_unfilled(is_phase_transition=True)
            return FillStatus.REQUOTED

        # Within-phase reprice
        if reprice_elapsed >= phase.reprice_interval:
            logger.info(
                f"FillManager: repricing within phase {self._phase_index + 1} "
                f"({phase.pricing}) after {reprice_elapsed:.0f}s"
            )
            self._last_reprice_at = now
            self._requote_unfilled()
            return FillStatus.REQUOTED

        return FillStatus.PENDING

    def _requote_unfilled(self, is_phase_transition: bool = False) -> None:
        """Cancel stale orders and re-place at fresh prices."""
        phase = self._current_phase
        if phase is None:
            return

        for ls in self._legs:
            if ls.is_filled or not ls.order_id or ls.skipped:
                continue

            price = self._compute_price(ls.symbol, ls.side, phase)
            if price is None:
                logger.error(f"FillManager: no price for {ls.symbol} on requote")
                continue

            # Skip if price unchanged (< 0.1% relative change)
            current_record = self._order_manager._orders.get(ls.order_id)
            if current_record and float(current_record.price) > 0:
                cur_amt = float(current_record.price)
                new_amt = float(price)
                if abs(cur_amt - new_amt) / cur_amt < 0.001:
                    logger.info(
                        f"FillManager: skipping requote for {ls.symbol} — "
                        f"price unchanged @ {price}"
                    )
                    continue

                # Directional guard (within-phase only)
                if not is_phase_transition:
                    if ls.side == "sell" and new_amt < cur_amt:
                        logger.info(
                            f"FillManager: skipping reprice for {ls.symbol} — "
                            f"new {new_amt:.6f} < current {cur_amt:.6f} "
                            f"(sell directional guard)"
                        )
                        continue
                    if ls.side == "buy" and new_amt > cur_amt:
                        logger.info(
                            f"FillManager: skipping reprice for {ls.symbol} — "
                            f"new {new_amt:.6f} > current {cur_amt:.6f} "
                            f"(buy directional guard)"
                        )
                        continue

            try:
                new_record = self._order_manager.requote_order(
                    ls.order_id, new_price=price, new_qty=ls.remaining_qty,
                )
                if new_record:
                    ls._fill_baseline = ls.filled_qty
                    ls.order_id = new_record.order_id
                    ls.requote_count += 1
                    if new_record.filled_qty > 0:
                        ls.filled_qty += new_record.filled_qty
                        ls.fill_price = new_record.avg_fill_price or ls.fill_price
                    if new_record.fee:
                        ls.fee = new_record.fee if ls.fee is None else ls.fee + new_record.fee
                    logger.info(
                        f"FillManager: requoted {ls.side} "
                        f"{ls.remaining_qty}x {ls.symbol} @ {price} "
                        f"(round {ls.requote_count})"
                    )
                else:
                    old_record = self._order_manager._orders.get(ls.order_id)
                    if old_record and old_record.status.value == "filled":
                        logger.info(f"FillManager: {ls.symbol} filled during requote")
                    else:
                        logger.warning(f"FillManager: {ls.symbol} requote failed")
            except Exception as e:
                logger.error(f"FillManager: requote exception for {ls.symbol}: {e}")

    def _cancel_placed(self) -> None:
        """Cancel all placed orders (used on atomic-mode failure)."""
        for ls in self._legs:
            if ls.order_id and not ls.is_filled:
                try:
                    self._order_manager.cancel_order(ls.order_id)
                except Exception:
                    pass

    # -- Internal: pricing ----------------------------------------------------

    def _compute_price(
        self, symbol: str, side: str, phase: PhaseConfig
    ) -> Optional[Price]:
        """Compute order price using PricingEngine. Returns Price with currency."""
        try:
            ob = self._market_data.get_option_orderbook(symbol)
            if not ob:
                return None

            snapshot = self._build_snapshot(ob, symbol)

            floor_price = None
            if phase.min_floor_price is not None:
                floor_price = Price(phase.min_floor_price, snapshot.currency)

            result = self._pricing_engine.compute(
                orderbook=snapshot,
                side=side,
                mode=phase.pricing,
                aggression=phase.fair_aggression,
                buffer_pct=phase.buffer_pct,
                min_price_pct_of_fair=phase.min_price_pct_of_fair,
                min_floor_price=floor_price,
            )

            return result.price

        except Exception as e:
            logger.error(f"FillManager: error computing price for {symbol}: {e}")
            return None

    @staticmethod
    def _build_snapshot(ob: dict, symbol: str) -> OrderbookSnapshot:
        """Convert raw orderbook dict to OrderbookSnapshot."""
        best_ask = float(ob["asks"][0]["price"]) if ob.get("asks") else None
        best_bid = float(ob["bids"][0]["price"]) if ob.get("bids") else None

        mark_btc = float(ob.get("_mark_btc", 0))
        mark_usd = float(ob.get("mark", 0))

        # Prefer explicit _currency tag (set by exchange adapters in Phase 3.1)
        raw_currency = ob.get("_currency")
        if raw_currency:
            currency = Currency(raw_currency)
            mark = mark_btc if currency == Currency.BTC else (mark_usd if mark_usd > 0 else None)
        elif mark_btc > 0:
            currency = Currency.BTC
            mark = mark_btc
        else:
            currency = Currency.USD
            mark = mark_usd if mark_usd > 0 else None

        index_price = float(ob.get("_index_price", 0)) or None

        return OrderbookSnapshot(
            symbol=symbol,
            currency=currency,
            best_bid=best_bid,
            best_ask=best_ask,
            mark=mark,
            index_price=index_price,
            timestamp=time.time(),
        )
