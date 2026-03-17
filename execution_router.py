#!/usr/bin/env python3
"""
Execution Router

Extracted from LifecycleEngine — routes trade open/close to the correct
executor based on execution_mode (limit, rfq, or auto-detected).

This module is the bridge between the lifecycle state machine and the
concrete execution backends:
  - "limit"  → LimitFillManager (per-leg limit orders)
  - "rfq"    → RFQExecutor (atomic multi-leg)
"""

import logging
import time
from typing import List, Optional

from trade_execution import TradeExecutor, LimitFillManager, ExecutionParams
from rfq import RFQExecutor, OptionLeg, RFQResult
from order_manager import OrderManager, OrderPurpose

logger = logging.getLogger(__name__)

# Avoid circular import — these are imported at function scope or via
# TYPE_CHECKING in consuming modules.  We import the concrete types we
# need directly from trade_lifecycle (data-only module).
from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState, RFQParams


class ExecutionRouter:
    """
    Routes trade open/close to the correct executor.

    Created and owned by LifecycleEngine.  Strategies never interact with
    this directly — they set execution_mode on TradeLifecycle and the
    engine delegates here.
    """

    def __init__(
        self,
        executor: TradeExecutor,
        rfq_executor: RFQExecutor,
        order_manager: OrderManager,
        market_data,
        rfq_notional_threshold: float = 50000.0,
    ):
        self._executor = executor
        self._rfq_executor = rfq_executor
        self._order_manager = order_manager
        self._market_data = market_data
        self.rfq_notional_threshold = rfq_notional_threshold

    # ── Open ─────────────────────────────────────────────────────────────

    def open(self, trade: TradeLifecycle) -> bool:
        """
        Place orders to open a trade.

        Auto-determines execution mode if not set on the trade.
        Routes to the appropriate executor.

        Returns True if orders were placed (not necessarily filled).
        """
        if trade.execution_mode is None:
            trade.execution_mode = self._determine_execution_mode(trade)

        logger.info(f"Opening trade {trade.id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
            return self._open_rfq(trade)
        else:
            return self._open_limit(trade)

    def close(self, trade: TradeLifecycle) -> bool:
        """
        Place orders to close a trade.

        Generates close legs as the reverse of open legs and submits them.
        Returns True if close orders were placed.
        """
        logger.info(f"Closing trade {trade.id} via {trade.execution_mode}")

        if trade.execution_mode == "rfq":
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
            return self._close_limit(trade)

    # ── Mode auto-detection ──────────────────────────────────────────────

    def _determine_execution_mode(self, trade: TradeLifecycle) -> str:
        """
        Auto-determine execution mode based on trade characteristics.

        Logic:
          - Single leg → "limit"
          - Multi-leg, notional >= rfq_threshold → "rfq"
          - Multi-leg, notional < rfq_threshold → "limit"
        """
        if len(trade.open_legs) == 1:
            logger.info(f"[{trade.id}] Single leg detected, using 'limit' mode")
            return "limit"

        notional = self._calculate_notional(trade.open_legs)
        logger.info(f"[{trade.id}] Multi-leg notional: ${notional:,.2f}")

        if notional >= self.rfq_notional_threshold:
            logger.info(f"[{trade.id}] Notional >= ${self.rfq_notional_threshold:,.0f}, using 'rfq' mode")
            return "rfq"
        else:
            logger.info(f"[{trade.id}] Notional < ${self.rfq_notional_threshold:,.0f}, using 'limit' mode")
            return "limit"

    def _calculate_notional(self, legs: List[TradeLeg]) -> float:
        """Calculate total notional value of a multi-leg order."""
        total_notional = 0.0
        for leg in legs:
            try:
                orderbook = self._market_data.get_option_orderbook(leg.symbol)
                if not orderbook:
                    logger.warning(f"Could not fetch orderbook for {leg.symbol}, using 0 notional")
                    continue
                mark_price = float(orderbook.get('mark', 0))
                if mark_price <= 0:
                    logger.warning(f"Invalid mark price for {leg.symbol}, skipping")
                    continue
                total_notional += mark_price * leg.qty
            except Exception as e:
                logger.warning(f"Error calculating notional for {leg.symbol}: {e}")
        return total_notional

    # ── Open implementations ─────────────────────────────────────────────

    def _open_rfq(self, trade: TradeLifecycle) -> bool:
        """Open via RFQ — atomic multi-leg execution.

        Supports phased execution when trade.metadata contains:
            rfq_phased: True
            rfq_initial_wait_seconds: int (default 30)
            rfq_mark_floor_pct: float (default 2.2)
            rfq_relax_after_seconds: int (default 300)
        """
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

        # Phased execution: strategy can opt in via metadata.
        # When phased, metadata timeout overrides rfq_params (which is for close).
        if trade.metadata.get("rfq_phased"):
            phased_timeout = trade.metadata.get("rfq_timeout_seconds", rfq_timeout)
            result: RFQResult = self._rfq_executor.execute_phased(
                legs=rfq_legs,
                action=trade.rfq_action,
                timeout_seconds=phased_timeout,
                initial_wait_seconds=trade.metadata.get("rfq_initial_wait_seconds", 30),
                mark_floor_pct=trade.metadata.get("rfq_mark_floor_pct", 2.2),
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
                    leg.fill_price = float(result.legs[i].get('price', 0.0))
            logger.info(f"Trade {trade.id} opened via RFQ (all legs filled)")
            return True

        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            logger.warning(
                f"Trade {trade.id} RFQ open failed: {result.message} "
                f"— falling back to '{fallback}'"
            )
            trade.execution_mode = fallback
            return self._open_limit(trade)

        trade.state = TradeState.FAILED
        trade.error = result.message
        logger.error(f"Trade {trade.id} RFQ failed: {result.message}")
        return False

    def _open_limit(self, trade: TradeLifecycle) -> bool:
        """Open via limit orders — delegates placement to LimitFillManager."""
        trade.state = TradeState.OPENING

        params = trade.execution_params or trade.metadata.get("execution_params") or ExecutionParams()
        mgr = LimitFillManager(self._executor, params, order_manager=self._order_manager, market_data=self._market_data)

        ok = mgr.place_all(
            trade.open_legs,
            lifecycle_id=trade.id,
            purpose=OrderPurpose.OPEN_LEG,
        )
        if not ok:
            trade.error = "Failed to place one or more open orders"
            logger.error(f"Trade {trade.id}: {trade.error}")
            trade.state = TradeState.FAILED
            return False

        trade.metadata["_open_fill_mgr"] = mgr
        logger.info(f"Trade {trade.id}: all {len(trade.open_legs)} open orders placed via LimitFillManager")
        return True

    # ── Close implementations ────────────────────────────────────────────

    def _close_rfq(self, trade: TradeLifecycle) -> bool:
        """Close via RFQ — atomic multi-leg execution."""
        rfq_legs = [
            OptionLeg(
                instrument=leg.symbol,
                side=leg.side.upper(),
                qty=leg.filled_qty if leg.filled_qty > 0 else leg.qty,
            )
            for leg in trade.open_legs
        ]

        close_action = "sell" if trade.rfq_action == "buy" else "buy"

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

        fallback = rp.fallback_mode if rp else trade.metadata.get("rfq_fallback")
        if fallback:
            logger.warning(
                f"Trade {trade.id} RFQ close failed: {result.message} "
                f"— falling back to '{fallback}'"
            )
            trade.execution_mode = fallback
            return self._close_limit(trade)

        trade.state = TradeState.PENDING_CLOSE
        logger.error(f"Trade {trade.id} RFQ close failed: {result.message}, will retry")
        return False

    def _close_limit(self, trade: TradeLifecycle) -> bool:
        """Close via limit orders — delegates placement to LimitFillManager."""
        trade.state = TradeState.CLOSING

        # Rebuild close legs fresh — prevents double-ordering on retry.
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
            return False

        # Cancel any existing close_leg orders (e.g. TP limit order) before
        # placing SL close orders — prevents idempotent collision in OrderManager.
        if self._order_manager:
            existing = self._order_manager.get_live_orders(
                trade.id, purpose=OrderPurpose.CLOSE_LEG
            )
            for rec in existing:
                self._order_manager.cancel_order(rec.order_id)
                logger.info(
                    f"Trade {trade.id}: cancelled existing close order "
                    f"{rec.order_id} ({rec.symbol} @ {rec.price}) before SL close"
                )

        params = trade.execution_params or trade.metadata.get("execution_params") or ExecutionParams()
        mgr = LimitFillManager(self._executor, params, order_manager=self._order_manager, market_data=self._market_data)

        # BUG-2026-03-05: reduce_only prevents close orders from building reverse positions
        ok = mgr.place_all(
            trade.close_legs,
            reduce_only=True,
            lifecycle_id=trade.id,
            purpose=OrderPurpose.CLOSE_LEG,
        )
        if not ok:
            logger.error(f"Trade {trade.id}: failed to place close orders, will retry (attempt {count}/{MAX_CLOSE_ATTEMPTS})")
            trade.state = TradeState.PENDING_CLOSE
            return False

        trade.metadata["_close_fill_mgr"] = mgr
        logger.info(f"Trade {trade.id}: all close orders placed via LimitFillManager")
        return True

    # ── Helpers ──────────────────────────────────────────────────────────

    def cancel_placed_orders(self, legs: List[TradeLeg]) -> None:
        """Cancel any orders already placed for the given legs (cleanup on failure)."""
        for leg in legs:
            if leg.order_id and not leg.is_filled:
                try:
                    self._executor.cancel_order(leg.order_id)
                    logger.info(f"Cancelled orphaned order {leg.order_id} for {leg.symbol}")
                except Exception as e:
                    logger.warning(f"Failed to cancel orphaned order {leg.order_id}: {e}")
