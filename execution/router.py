"""
execution/router.py — Typed execution router.

Routes trade open/close to the correct backend (limit via FillManager, or
RFQ) and returns FillResult.  Owned by LifecycleEngine — strategies never
interact with this directly.
"""

from __future__ import annotations

import logging
import time
from typing import Any, List, Optional, TYPE_CHECKING

from execution.fill_manager import FillManager
from execution.fill_result import FillResult, FillStatus
from execution.profiles import ExecutionProfile

if TYPE_CHECKING:
    from order_manager import OrderManager, OrderPurpose
    from rfq import RFQExecutor, OptionLeg, RFQResult
    from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState, RFQParams

logger = logging.getLogger(__name__)
_execution_logger = logging.getLogger("ct.execution")


class Router:
    """Routes trade open/close to the correct executor.

    Created and owned by LifecycleEngine.  Strategies set execution_mode
    on TradeLifecycle and the router resolves the right backend.
    """

    def __init__(
        self,
        executor: Any,
        rfq_executor: Any,
        order_manager: "OrderManager",
        market_data: Any,
        rfq_notional_threshold: float = 50000.0,
    ):
        self._executor = executor
        self._rfq_executor = rfq_executor
        self._order_manager = order_manager
        self._market_data = market_data
        self.rfq_notional_threshold = rfq_notional_threshold

    # ── Open ─────────────────────────────────────────────────────────────

    def open(self, trade: "TradeLifecycle") -> FillResult:
        """Place orders to open a trade.

        Auto-determines execution mode if not set.
        Returns FillResult (PENDING on success, REFUSED/FAILED on error).
        """
        from trade_lifecycle import TradeState

        if trade.execution_mode is None:
            trade.execution_mode = self._determine_execution_mode(trade)

        logger.info(f"Opening trade {trade.id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
            return self._open_rfq(trade)
        return self._open_limit(trade)

    def close(self, trade: "TradeLifecycle") -> FillResult:
        """Place orders to close a trade.

        Returns FillResult (PENDING on success, REFUSED/FAILED on error).
        """
        from trade_lifecycle import TradeLeg, TradeState

        logger.info(f"Closing trade {trade.id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
            trade.close_legs = [
                TradeLeg(
                    symbol=leg.symbol,
                    qty=leg.filled_qty,
                    side=leg.close_side,
                )
                for leg in trade.open_legs
                if leg.filled_qty > 0
            ]
            return self._close_rfq(trade)
        return self._close_limit(trade)

    # ── Mode auto-detection ──────────────────────────────────────────────

    def _determine_execution_mode(self, trade: "TradeLifecycle") -> str:
        if len(trade.open_legs) == 1:
            return "limit"
        notional = self._calculate_notional(trade.open_legs)
        if notional >= self.rfq_notional_threshold:
            return "rfq"
        return "limit"

    def _calculate_notional(self, legs: list) -> float:
        total = 0.0
        for leg in legs:
            try:
                ob = self._market_data.get_option_orderbook(leg.symbol)
                if ob:
                    mark = float(ob.get("mark", 0))
                    if mark > 0:
                        total += mark * leg.qty
            except Exception as e:
                logger.warning(f"Error calculating notional for {leg.symbol}: {e}")
        return total

    # ── Open implementations ─────────────────────────────────────────────

    def _open_rfq(self, trade: "TradeLifecycle") -> FillResult:
        """Open via RFQ — atomic multi-leg execution."""
        from rfq import OptionLeg, RFQResult
        from trade_lifecycle import TradeState

        rfq_legs = [
            OptionLeg(
                instrument=leg.symbol,
                side=leg.side.upper(),
                qty=leg.qty,
            )
            for leg in trade.open_legs
        ]

        rp = trade.rfq_params
        rfq_timeout = rp.timeout_seconds if rp else trade.metadata.get("rfq_timeout_seconds", 60)
        min_improvement = rp.min_improvement_pct if rp else trade.metadata.get("rfq_min_improvement_pct", -999.0)

        if trade.metadata.get("rfq_phased"):
            phased_timeout = trade.metadata.get("rfq_timeout_seconds", rfq_timeout)
            min_improve = trade.metadata.get("rfq_min_book_improvement_pct", 2.2)
            if callable(min_improve):
                min_improve = min_improve(trade)
            result: RFQResult = self._rfq_executor.execute_phased(
                legs=rfq_legs,
                action=trade.rfq_action,
                timeout_seconds=phased_timeout,
                initial_wait_seconds=trade.metadata.get("rfq_initial_wait_seconds", 30),
                min_book_improvement_pct=min_improve,
                relax_after_seconds=trade.metadata.get("rfq_relax_after_seconds", 300),
            )
        else:
            result: RFQResult = self._rfq_executor.execute(
                legs=rfq_legs,
                action=trade.rfq_action,
                timeout_seconds=rfq_timeout,
                min_improvement_pct=min_improvement,
            )
        trade.rfq_result = result

        if result.success:
            trade.state = TradeState.OPEN
            trade.opened_at = time.time()
            for i, leg in enumerate(trade.open_legs):
                leg.filled_qty = leg.qty
                if i < len(result.legs):
                    leg.fill_price = float(result.legs[i].get("price", 0.0))
            for leg in trade.open_legs:
                mkt = self._market_data.get_option_details(leg.symbol)
                if mkt:
                    mark = float(mkt.get("markPrice", 0))
                    trade.metadata[f"mark_at_open_{leg.symbol}"] = mark
            logger.info(f"Trade {trade.id} opened via RFQ (all legs filled)")
            # Return FILLED result for RFQ
            from execution.fill_result import LegFillSnapshot
            from execution.currency import Price, Currency
            fill_legs = []
            for leg in trade.open_legs:
                fill_legs.append(LegFillSnapshot(
                    symbol=leg.symbol, side=leg.side, qty=leg.qty,
                    filled_qty=leg.filled_qty,
                    fill_price=Price(leg.fill_price, Currency.BTC) if leg.fill_price else None,
                    order_id=leg.order_id,
                ))
            return FillResult(
                status=FillStatus.FILLED, legs=fill_legs,
                phase_index=1, phase_total=1, phase_pricing="rfq",
                elapsed_seconds=0.0,
            )

        # RFQ failed — try fallback
        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            logger.warning(f"Trade {trade.id} RFQ failed: {result.message} — fallback to '{fallback}'")
            trade.execution_mode = fallback
            return self._open_limit(trade)

        trade.state = TradeState.FAILED
        trade.error = result.message
        return FillResult(
            status=FillStatus.FAILED, legs=[], phase_index=1, phase_total=1,
            phase_pricing="rfq", elapsed_seconds=0.0, error=result.message,
        )

    def _open_limit(self, trade: "TradeLifecycle") -> FillResult:
        """Open via limit orders — delegates to FillManager."""
        from trade_lifecycle import TradeState
        from order_manager import OrderPurpose

        trade.state = TradeState.OPENING

        profile = self._resolve_profile(trade)
        mgr = FillManager(
            order_manager=self._order_manager,
            market_data=self._market_data,
            profile=profile,
            params=trade.execution_params,
            direction="open",
        )

        result = mgr.place_all(
            trade.open_legs,
            lifecycle_id=trade.id,
            purpose=OrderPurpose.OPEN_LEG,
        )

        if result.status in (FillStatus.REFUSED, FillStatus.FAILED):
            trade.error = result.error or "Failed to place open orders"
            trade.state = TradeState.FAILED
            return result

        trade.metadata["_open_fill_mgr"] = mgr
        logger.info(
            f"Trade {trade.id}: {len(trade.open_legs)} open orders placed via FillManager"
        )
        return result

    # ── Close implementations ────────────────────────────────────────────

    def _close_rfq(self, trade: "TradeLifecycle") -> FillResult:
        """Close via RFQ."""
        from rfq import OptionLeg, RFQResult
        from trade_lifecycle import TradeState

        rfq_legs = [
            OptionLeg(
                instrument=leg.symbol,
                side=leg.side.upper(),
                qty=leg.filled_qty,
            )
            for leg in trade.open_legs
            if leg.filled_qty > 0
        ]
        close_action = "sell" if trade.rfq_action == "buy" else "buy"

        rp = trade.rfq_params
        rfq_timeout = rp.timeout_seconds if rp else 60
        min_improvement = rp.min_improvement_pct if rp else -999.0

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
                    leg.fill_price = float(result.legs[i].get("price", 0.0))
            trade._finalize_close()
            from execution.fill_result import LegFillSnapshot
            from execution.currency import Price, Currency
            fill_legs = []
            for leg in trade.close_legs:
                fill_legs.append(LegFillSnapshot(
                    symbol=leg.symbol, side=leg.side, qty=leg.qty,
                    filled_qty=leg.filled_qty,
                    fill_price=Price(leg.fill_price, Currency.BTC) if leg.fill_price else None,
                    order_id=leg.order_id,
                ))
            return FillResult(
                status=FillStatus.FILLED, legs=fill_legs,
                phase_index=1, phase_total=1, phase_pricing="rfq",
                elapsed_seconds=0.0,
            )

        # RFQ close failed — fallback
        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            trade.execution_mode = fallback
            return self._close_limit(trade)

        trade.state = TradeState.PENDING_CLOSE
        return FillResult(
            status=FillStatus.FAILED, legs=[], phase_index=1, phase_total=1,
            phase_pricing="rfq", elapsed_seconds=0.0, error=result.message,
        )

    def _close_limit(self, trade: "TradeLifecycle") -> FillResult:
        """Close via limit orders — delegates to FillManager."""
        from trade_lifecycle import TradeLeg, TradeState
        from order_manager import OrderPurpose

        trade.state = TradeState.CLOSING

        # Rebuild close legs — prevents double-ordering on retry.
        # Only include legs that were actually filled on open (filled_qty > 0).
        old_close_filled = {}
        if trade.close_legs:
            for cl in trade.close_legs:
                if cl.filled_qty > 0:
                    old_close_filled[cl.symbol] = cl.filled_qty

        trade.close_legs = [
            TradeLeg(
                symbol=leg.symbol,
                qty=leg.filled_qty - old_close_filled.get(leg.symbol, 0.0),
                side=leg.close_side,
            )
            for leg in trade.open_legs
            if leg.filled_qty > 0
               and leg.filled_qty - old_close_filled.get(leg.symbol, 0.0) > 0
        ]

        if not trade.close_legs:
            trade.state = TradeState.CLOSED
            trade.closed_at = time.time()
            trade._finalize_close()
            return FillResult(
                status=FillStatus.FILLED, legs=[], phase_index=1, phase_total=1,
                phase_pricing="n/a", elapsed_seconds=0.0,
            )

        # Circuit breaker
        MAX_CLOSE_ATTEMPTS = 10
        count = trade.metadata.get("_close_attempt_count", 0) + 1
        trade.metadata["_close_attempt_count"] = count
        if count > MAX_CLOSE_ATTEMPTS:
            remaining = [l.symbol for l in trade.close_legs if l.filled_qty == 0]
            trade.state = TradeState.FAILED
            trade.error = (
                f"Close failed after {MAX_CLOSE_ATTEMPTS} attempts — "
                f"manual intervention required. Remaining: {remaining}"
            )
            logger.critical(f"Trade {trade.id}: {trade.error}")
            return FillResult(
                status=FillStatus.FAILED, legs=[], phase_index=1, phase_total=1,
                phase_pricing="n/a", elapsed_seconds=0.0, error=trade.error,
            )

        # Cancel existing close orders before placing new ones
        existing = self._order_manager.get_live_orders(
            trade.id, purpose=OrderPurpose.CLOSE_LEG
        )
        for rec in existing:
            self._order_manager.cancel_order(rec.order_id)
            logger.info(
                f"Trade {trade.id}: cancelled existing close order "
                f"{rec.order_id} ({rec.symbol})"
            )

        profile = self._resolve_profile(trade)
        mgr = FillManager(
            order_manager=self._order_manager,
            market_data=self._market_data,
            profile=profile,
            params=trade.execution_params,
            direction="close",
        )

        result = mgr.place_all(
            trade.close_legs,
            lifecycle_id=trade.id,
            purpose=OrderPurpose.CLOSE_LEG,
            reduce_only=True,
        )

        if result.status in (FillStatus.REFUSED, FillStatus.FAILED):
            logger.error(
                f"Trade {trade.id}: failed to place close orders "
                f"(attempt {count}/{MAX_CLOSE_ATTEMPTS})"
            )
            trade.state = TradeState.PENDING_CLOSE
            return result

        trade.metadata["_close_fill_mgr"] = mgr
        logger.info(f"Trade {trade.id}: close orders placed via FillManager")
        return result

    # ── Helpers ──────────────────────────────────────────────────────────

    def _resolve_profile(self, trade: "TradeLifecycle") -> Optional[ExecutionProfile]:
        """Get ExecutionProfile from trade metadata or None (let FillManager bridge)."""
        return trade.metadata.get("_execution_profile")

    def cancel_placed_orders(self, legs: list) -> None:
        """Cancel any orders already placed for the given legs."""
        for leg in legs:
            if hasattr(leg, "order_id") and leg.order_id:
                if hasattr(leg, "is_filled") and leg.is_filled:
                    continue
                try:
                    self._order_manager.cancel_order(leg.order_id)
                    logger.info(f"Cancelled orphaned order {leg.order_id} for {leg.symbol}")
                except Exception as e:
                    logger.warning(f"Failed to cancel {leg.order_id}: {e}")
