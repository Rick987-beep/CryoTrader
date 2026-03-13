#!/usr/bin/env python3
"""
Lifecycle Engine — Trade State Machine

Orchestrates TradeLifecycle objects through the state machine:
    PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED

Extracted from the original LifecycleManager in trade_lifecycle.py.
The execution routing logic lives in execution_router.py.

Usage:
    from lifecycle_engine import LifecycleEngine

    engine = LifecycleEngine()
    position_monitor.on_update(engine.tick)

    trade = engine.create(legs=[...], exit_conditions=[...])
    engine.open(trade.id)
    # tick() handles everything from here
"""

import json
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from account_manager import AccountManager, AccountSnapshot, PositionSnapshot
from trade_execution import TradeExecutor, LimitFillManager, ExecutionParams
from rfq import RFQExecutor
from order_manager import OrderManager, OrderPurpose, OrderStatus
from execution_router import ExecutionRouter
from trade_lifecycle import (
    ExitCondition,
    RFQParams,
    TradeLeg,
    TradeLifecycle,
    TradeState,
)

logger = logging.getLogger(__name__)


class LifecycleEngine:
    """
    Orchestrates one or more TradeLifecycles through their state machines.

    Usage:
        engine = LifecycleEngine()

        # Hook into PositionMonitor so tick() runs on every snapshot
        position_monitor.on_update(engine.tick)

        # Create a trade
        trade = engine.create(
            legs=[TradeLeg(symbol="BTCUSD-20FEB26-70000-C", qty=0.01, side=1)],
            exit_conditions=[profit_target(50), max_hold_hours(48)],
            execution_mode="limit",
        )

        # Open it (places orders)
        engine.open(trade.id)

        # From here, tick() handles everything
    """

    # Reconciliation runs every N ticks (~50s at 10s poll interval)
    RECONCILE_EVERY_N_TICKS: int = 5

    def __init__(
        self,
        rfq_notional_threshold: float = 50000.0,
        account_manager: Optional[AccountManager] = None,
    ):
        self._trades: Dict[str, TradeLifecycle] = {}
        self._executor = TradeExecutor()
        self._rfq_executor = RFQExecutor()
        self._order_manager = OrderManager(self._executor)
        self._account_manager = account_manager
        self._tick_counter: int = 0
        self._last_reconciliation_warnings: List[str] = []
        self._last_reconciliation_time: Optional[float] = None
        self._notifier = None  # lazy-loaded TelegramNotifier

        self._router = ExecutionRouter(
            executor=self._executor,
            rfq_executor=self._rfq_executor,
            order_manager=self._order_manager,
            rfq_notional_threshold=rfq_notional_threshold,
        )

        self.rfq_notional_threshold = rfq_notional_threshold

    @property
    def order_manager(self) -> OrderManager:
        """Access the order ledger (for external queries, crash recovery, etc.)."""
        return self._order_manager

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

    def restore_trade(self, trade: TradeLifecycle) -> None:
        """Inject a recovered trade into the engine's trade registry."""
        self._trades[trade.id] = trade
        logger.info(
            f"Restored trade {trade.id} (strategy={trade.strategy_id}, "
            f"state={trade.state.value}, legs={len(trade.open_legs)})"
        )

    # ── Create ───────────────────────────────────────────────────────────

    def create(
        self,
        legs: List[TradeLeg],
        exit_conditions: Optional[List[ExitCondition]] = None,
        execution_mode: Optional[str] = None,
        rfq_action: str = "buy",
        execution_params: Optional[ExecutionParams] = None,
        rfq_params: Optional[RFQParams] = None,
        strategy_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TradeLifecycle:
        """Register a new trade intent. Returns TradeLifecycle in PENDING_OPEN."""
        trade = TradeLifecycle(
            open_legs=legs,
            strategy_id=strategy_id,
            exit_conditions=exit_conditions or [],
            execution_mode=execution_mode,
            rfq_action=rfq_action,
            execution_params=execution_params,
            rfq_params=rfq_params,
            metadata=metadata or {},
        )
        self._trades[trade.id] = trade
        logger.info(
            f"Trade {trade.id} created: {len(legs)} legs, "
            f"mode={execution_mode or 'auto-route'}, strategy={strategy_id}"
        )
        return trade

    # ── Open / Close ─────────────────────────────────────────────────────

    def open(self, trade_id: str) -> bool:
        """Place orders to open a trade. Routes via ExecutionRouter."""
        trade = self._trades.get(trade_id)
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False
        if trade.state != TradeState.PENDING_OPEN:
            logger.error(f"Trade {trade_id} not in PENDING_OPEN (is {trade.state.value})")
            return False
        return self._router.open(trade)

    def close(self, trade_id: str) -> bool:
        """Place orders to close a trade. Routes via ExecutionRouter."""
        trade = self._trades.get(trade_id)
        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return False
        if trade.state not in (TradeState.OPEN, TradeState.PENDING_CLOSE):
            logger.error(f"Trade {trade_id} not closeable (is {trade.state.value})")
            return False
        return self._router.close(trade)

    # ── Fill checking ────────────────────────────────────────────────────

    def _check_open_fills(self, trade: TradeLifecycle) -> None:
        """Delegate fill-checking to LimitFillManager."""
        mgr: Optional[LimitFillManager] = trade.metadata.get("_open_fill_mgr")
        if mgr is None:
            logger.error(f"Trade {trade.id}: no fill manager for OPENING state")
            return

        result = mgr.check()

        if result == "filled":
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            trade.state = TradeState.OPEN
            trade.opened_at = time.time()
            logger.info(f"Trade {trade.id}: all open legs filled → OPEN")

        elif result == "failed":
            logger.error(f"Trade {trade.id}: fill manager exhausted requote rounds")
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            mgr.cancel_all()
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
            for ls, leg in zip(mgr.filled_legs, trade.open_legs):
                leg.order_id = ls.order_id
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
            logger.debug(f"Trade {trade.id}: requoted unfilled open legs, continuing")

    def _check_close_fills(self, trade: TradeLifecycle) -> None:
        """Delegate close-fill checking to LimitFillManager."""
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
            for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
                leg.order_id = ls.order_id
            mgr.cancel_all()
            trade.state = TradeState.PENDING_CLOSE

        elif result == "requoted":
            for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                leg.order_id = ls.order_id
                leg.filled_qty = ls.filled_qty
                leg.fill_price = ls.fill_price
            logger.debug(f"Trade {trade.id}: requoted unfilled close legs, continuing")

    def _unwind_filled_legs(self, trade: TradeLifecycle, filled_legs: List[TradeLeg]) -> None:
        """Unwind partially-filled legs by transitioning through close cycle."""
        trade.open_legs = filled_legs
        trade.state = TradeState.OPEN
        trade.opened_at = time.time()
        trade.state = TradeState.PENDING_CLOSE
        logger.info(
            f"Trade {trade.id}: unwinding {len(filled_legs)} filled legs "
            f"via PENDING_CLOSE"
        )

    # ── Exit evaluation ──────────────────────────────────────────────────

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

    # ── Tick — the main heartbeat ────────────────────────────────────────

    def tick(self, account: AccountSnapshot) -> None:
        """
        Advance all active trades one step through the state machine.

        Designed to be called as a PositionMonitor callback:
            position_monitor.on_update(engine.tick)
        """
        # Step 1: Poll all non-terminal orders from the exchange.
        try:
            self._order_manager.poll_all()
        except Exception as e:
            logger.error(f"OrderManager poll_all error: {e}")

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
                    if trade.state == TradeState.PENDING_CLOSE:
                        self.close(trade.id)

                elif trade.state == TradeState.PENDING_CLOSE:
                    # GUARD: If close orders are already live on the exchange
                    # (from a previous tick), do NOT place new ones.
                    if self._order_manager.has_live_orders(trade.id, OrderPurpose.CLOSE_LEG):
                        logger.debug(
                            f"Trade {trade.id}: PENDING_CLOSE — live close orders exist, "
                            f"waiting for resolution"
                        )
                    else:
                        self.close(trade.id)

                elif trade.state == TradeState.CLOSING:
                    self._check_close_fills(trade)

            except Exception as e:
                logger.error(f"Trade {trade.id}: tick error in state {trade.state.value}: {e}")

        # Periodic reconciliation against exchange open-orders
        self._tick_counter += 1
        if (self._account_manager
                and self._tick_counter % self.RECONCILE_EVERY_N_TICKS == 0):
            self._run_reconciliation()

        # Persist trade state snapshot after processing
        if self._trades:
            self._persist_all_trades()
            try:
                self._order_manager.persist_snapshot()
            except Exception as e:
                logger.error(f"OrderManager persist_snapshot error: {e}")

    # ── Manual controls ──────────────────────────────────────────────────

    def force_close(self, trade_id: str) -> bool:
        """Force a trade closed regardless of exit conditions or current state."""
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
            return self.cancel(trade_id)

        if state == TradeState.CLOSING:
            mgr: Optional[LimitFillManager] = trade.metadata.get("_close_fill_mgr")
            if mgr is not None:
                for ls, leg in zip(mgr.filled_legs, trade.close_legs):
                    leg.order_id = ls.order_id
                    leg.filled_qty = ls.filled_qty
                    leg.fill_price = ls.fill_price
                mgr.cancel_all()
            else:
                self._router.cancel_placed_orders(trade.close_legs)
            trade.state = TradeState.PENDING_CLOSE
            logger.info(f"Trade {trade.id}: forced re-close (was CLOSING)")
            return True

        logger.warning(f"Trade {trade.id}: cannot force close in state {state.value}")
        return False

    def kill_all(self) -> int:
        """Emergency termination — cancel all orders and mark every trade CLOSED."""
        killed = 0
        for trade in list(self._trades.values()):
            if trade.state in (TradeState.CLOSED, TradeState.FAILED):
                continue

            for leg in trade.open_legs + trade.close_legs:
                if leg.order_id and not leg.is_filled:
                    try:
                        self._executor.cancel_order(leg.order_id)
                    except Exception:
                        pass

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
        """Cancel a trade that hasn't fully opened yet."""
        trade = self._trades.get(trade_id)
        if not trade:
            return False
        if trade.state not in (TradeState.PENDING_OPEN, TradeState.OPENING):
            logger.warning(f"Trade {trade.id}: cannot cancel in state {trade.state.value}")
            return False

        mgr: Optional[LimitFillManager] = trade.metadata.get("_open_fill_mgr")
        if mgr is not None:
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

        filled_legs = [l for l in trade.open_legs if l.is_filled]
        if filled_legs:
            logger.info(
                f"Trade {trade.id}: {len(filled_legs)} legs already filled "
                f"— unwinding via close orders"
            )
            self._unwind_filled_legs(trade, filled_legs)
            return True

        trade.state = TradeState.FAILED
        trade.error = "Cancelled by user"
        logger.info(f"Trade {trade.id}: cancelled (no fills)")
        return True

    # ── Reconciliation ────────────────────────────────────────────────────

    def _get_notifier(self):
        """Lazy-load TelegramNotifier to avoid circular imports."""
        if self._notifier is None:
            try:
                from telegram_notifier import get_notifier
                self._notifier = get_notifier()
            except Exception:
                self._notifier = None
        return self._notifier

    def _run_reconciliation(self) -> None:
        """
        Periodic reconciliation against exchange open-orders endpoint.

        Detects:
          - Stale ledger entries (ledger says live, exchange says gone) → poll to fix
          - Orphan orders (on exchange but not in ledger) → auto-cancel + alert
        """
        try:
            exchange_orders = self._account_manager.get_open_orders(force_refresh=True)
        except Exception as e:
            logger.warning(f"Reconciliation: failed to fetch exchange open orders: {e}")
            return

        warnings = self._order_manager.reconcile(exchange_orders)
        self._last_reconciliation_time = time.time()
        self._last_reconciliation_warnings = warnings

        if not warnings:
            logger.debug("Reconciliation: ledger and exchange in sync")
            return

        logger.warning(f"Reconciliation: {len(warnings)} issue(s) found")

        # Handle stale ledger entries — poll them to discover true state
        stale_warnings = [w for w in warnings if "not found on exchange" in w]
        for w in stale_warnings:
            # Extract order_id from warning: "Ledger order {id} ..."
            parts = w.split()
            if len(parts) >= 3:
                stale_id = parts[2]
                if stale_id in self._order_manager._orders:
                    self._order_manager.poll_order(stale_id)

        # Handle orphan orders — auto-cancel + notify
        orphan_warnings = [w for w in warnings if "Orphan order" in w]
        orphan_ids = []
        for w in orphan_warnings:
            parts = w.split()
            if len(parts) >= 3:
                orphan_id = parts[2]
                orphan_ids.append(orphan_id)
                try:
                    self._executor.cancel_order(orphan_id)
                    logger.warning(f"Reconciliation: auto-cancelled orphan order {orphan_id}")
                except Exception as e:
                    logger.error(f"Reconciliation: failed to cancel orphan {orphan_id}: {e}")

        # Telegram alerts
        notifier = self._get_notifier()
        if notifier:
            if orphan_ids:
                notifier.notify_orphan_detected(orphan_ids, "auto-cancelled")
            if stale_warnings:
                notifier.notify_reconciliation_warning(stale_warnings)

    @property
    def last_reconciliation_warnings(self) -> List[str]:
        """Most recent reconciliation warnings (empty if clean)."""
        return self._last_reconciliation_warnings

    @property
    def last_reconciliation_time(self) -> Optional[float]:
        """Timestamp of the last reconciliation run."""
        return self._last_reconciliation_time

    # ── Persistence ──────────────────────────────────────────────────────

    def _persist_all_trades(self) -> None:
        """Dump all trade states to JSON for crash recovery.

        Uses write-to-temp → fsync → atomic rename to prevent corruption
        if the process or OS crashes mid-write.
        """
        try:
            os.makedirs("logs", exist_ok=True)
            trades_data = [trade.to_dict() for trade in self._trades.values()]
            target = "logs/trades_snapshot.json"
            tmp = target + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"timestamp": time.time(), "trades": trades_data}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, target)
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
