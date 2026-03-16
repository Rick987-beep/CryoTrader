#!/usr/bin/env python3
"""
Position Closer — Two-Phase Mark-Price Close

Closes ALL open exchange positions using a graceful two-phase approach:

  Phase 1 — Mark Price (configurable, default 5 min)
      Places limit orders at the exchange mark price.
      Reprices every 30s with fresh mark data.

  Phase 2 — Aggressive (configurable, default 2 min)
      Drops sell price 10% below mark (raises buy price 10% above mark).
      Reprices every 15s.

Designed for the dashboard kill switch:
  - Runs in a background thread (non-blocking)
  - Disables all strategy runners before closing
  - Cancels all lifecycle-managed orders and marks trades CLOSED
  - Safe to re-run (skips already-closed positions)
"""

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from account_manager import AccountManager
    from strategy import StrategyRunner
    from trade_execution import TradeExecutor
    from lifecycle_engine import LifecycleEngine

logger = logging.getLogger(__name__)


# =============================================================================
# Internal Leg Tracker
# =============================================================================

@dataclass
class _CloseLeg:
    """Tracks one position being closed on the exchange."""
    symbol: str
    qty: float
    close_side: str          # "sell" to close long, "buy" to close short
    mark_price: float
    order_id: Optional[str] = None
    filled: bool = False
    fill_price: Optional[float] = None

    @property
    def side_label(self) -> str:
        return self.close_side.upper()

    def __repr__(self) -> str:
        status = f"FILLED @ ${self.fill_price:.2f}" if self.filled else f"PENDING (order={self.order_id})"
        return f"{self.symbol} {self.side_label} {self.qty} mark=${self.mark_price:.2f} [{status}]"


# =============================================================================
# Position Closer
# =============================================================================

class PositionCloser:
    """
    Two-phase mark-price position closer for the dashboard kill switch.

    Usage:
        closer = PositionCloser(account_manager, executor, lifecycle_engine, notifier)
        closer.start(runners)     # non-blocking, runs in background thread
        closer.is_running         # True while closing
        closer.status             # human-readable phase string
    """

    # Timing parameters
    PHASE1_DURATION: int = 300     # 5 minutes
    PHASE1_REPRICE: int = 30       # reprice every 30s
    PHASE2_DURATION: int = 120     # 2 minutes
    PHASE2_REPRICE: int = 15       # reprice every 15s
    PHASE2_DISCOUNT: float = 0.10  # 10% aggressive pricing
    POLL_INTERVAL: int = 10        # check fills every 10s

    def __init__(
        self,
        account_manager: "AccountManager",
        executor: "TradeExecutor",
        lifecycle_manager: "LifecycleEngine",
    ):
        self._am = account_manager
        self._executor = executor
        self._lm = lifecycle_manager
        self._running = False
        self._status = "idle"
        self._thread: Optional[threading.Thread] = None

    # -- Public interface -----------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status(self) -> str:
        return self._status

    def start(self, runners: Optional[List["StrategyRunner"]] = None) -> bool:
        """
        Trigger close-all in a background thread.

        Returns False if already running.
        """
        if self._running:
            logger.warning("PositionCloser already running — ignoring duplicate start")
            return False

        self._thread = threading.Thread(
            target=self._run,
            args=(runners or [],),
            name="PositionCloser",
            daemon=True,
        )
        self._thread.start()
        return True

    # -- Core execution -------------------------------------------------------

    def _run(self, runners: List["StrategyRunner"]) -> None:
        """Main execution flow — runs in background thread."""
        self._running = True
        self._status = "starting"
        start_time = time.time()

        try:
            # 1. Kill all lifecycle orders and mark trades CLOSED first,
            #    so runner.stop() force_close calls become harmless no-ops.
            killed = self._lm.kill_all()
            logger.warning(f"Kill switch: {killed} lifecycle trade(s) terminated")

            # 1b. Cancel every order tracked in the central ledger.
            # kill_all() cancels via fill managers and leg order_ids, but
            # the OrderManager may track additional orders (e.g. from
            # requote chains).  This is belt-and-suspenders.
            try:
                om_cancelled = self._lm.order_manager.cancel_all()
                if om_cancelled:
                    logger.warning(f"Kill switch: {om_cancelled} order(s) cancelled via OrderManager")
            except Exception as e:
                logger.warning(f"Kill switch: OrderManager cancel_all failed: {e}")

            # 2. Stop all strategy runners (sets _enabled=False, no new trades)
            for r in runners:
                r.stop()
            logger.warning(f"Kill switch: {len(runners)} strategy runner(s) stopped")

            # 3. Let cancellations settle on the exchange
            time.sleep(2)

            # 4. Fetch fresh exchange positions
            positions = self._am.get_positions(force_refresh=True)
            if not positions:
                self._status = "done"
                logger.info("Kill switch: no open positions found")
                return

            # 5. Build leg trackers
            legs = self._build_legs(positions)

            # 6. Phase 1: mark price
            self._status = "phase1"
            self._run_phase(
                legs,
                phase_name="Phase 1 (mark price)",
                duration=self.PHASE1_DURATION,
                reprice_interval=self.PHASE1_REPRICE,
                price_fn=lambda leg: leg.mark_price,
            )

            unfilled = [l for l in legs if not l.filled]
            if not unfilled:
                self._finalize(legs, start_time)
                return

            # 7. Phase 2: aggressive pricing
            self._status = "phase2"
            self._refresh_marks(legs)
            self._run_phase(
                legs,
                phase_name="Phase 2 (aggressive)",
                duration=self.PHASE2_DURATION,
                reprice_interval=self.PHASE2_REPRICE,
                price_fn=self._aggressive_price,
            )

            # 8. Finalize
            self._finalize(legs, start_time)

        except Exception as e:
            logger.error(f"Kill switch error: {e}", exc_info=True)
            self._status = f"error: {e}"
        finally:
            self._running = False
            # Trigger clean shutdown of the main process so crash flag
            # is removed and NSSM (production) sees a clean exit.
            logger.info("Kill switch complete — requesting process shutdown")
            os.kill(os.getpid(), signal.SIGTERM)

    # -- Phase runner ---------------------------------------------------------

    def _run_phase(
        self,
        legs: List[_CloseLeg],
        phase_name: str,
        duration: int,
        reprice_interval: int,
        price_fn,
    ) -> None:
        """Run a single timed phase: place orders, poll fills, reprice."""
        phase_start = time.time()

        # Initial placement
        for leg in legs:
            if not leg.filled:
                self._place_or_reprice(leg, price_fn(leg))

        last_reprice = time.time()

        while time.time() - phase_start < duration:
            time.sleep(self.POLL_INTERVAL)
            self._check_fills(legs)

            unfilled = [l for l in legs if not l.filled]
            if not unfilled:
                break

            filled_count = sum(1 for l in legs if l.filled)
            elapsed = time.time() - phase_start
            logger.info(
                f"Kill switch {phase_name}: {filled_count}/{len(legs)} filled, "
                f"{len(unfilled)} remaining ({elapsed:.0f}s)"
            )

            # Reprice at fresh marks
            if time.time() - last_reprice >= reprice_interval:
                self._refresh_marks(legs)
                for leg in unfilled:
                    self._place_or_reprice(leg, price_fn(leg))
                last_reprice = time.time()

    # -- Order management -----------------------------------------------------

    def _place_or_reprice(self, leg: _CloseLeg, price: float) -> bool:
        """Cancel existing order (if any) and place a new limit order."""
        if leg.order_id:
            try:
                self._executor.cancel_order(leg.order_id)
            except Exception as e:
                logger.warning(f"Kill switch: cancel failed for {leg.order_id}: {e}")
            leg.order_id = None

        result = self._executor.place_order(
            symbol=leg.symbol,
            qty=leg.qty,
            side=leg.close_side,
            order_type=1,  # limit
            price=round(price, 2),
        )

        if result:
            leg.order_id = str(result.get("orderId", ""))
            logger.info(
                f"Kill switch: {leg.side_label} {leg.qty}x {leg.symbol} "
                f"@ ${price:.2f} (order {leg.order_id})"
            )
            return True

        logger.error(f"Kill switch: failed to place order for {leg.symbol} @ ${price:.2f}")
        return False

    def _check_fills(self, legs: List[_CloseLeg]) -> None:
        """Poll order status for all unfilled legs."""
        for leg in legs:
            if leg.filled or not leg.order_id:
                continue
            try:
                status = self._executor.get_order_status(leg.order_id)
                if not status:
                    continue
                state = status.get("state", -1)
                fill_qty = float(status.get("fillQty", 0))
                if state == 1 or fill_qty >= leg.qty:  # FILLED
                    leg.filled = True
                    leg.fill_price = float(status.get("avgPrice", 0))
                    logger.info(f"Kill switch: FILLED {leg.symbol} @ ${leg.fill_price:.2f}")
            except Exception as e:
                logger.warning(f"Kill switch: error checking {leg.symbol}: {e}")

    def _refresh_marks(self, legs: List[_CloseLeg]) -> None:
        """Refresh mark prices from fresh exchange position data."""
        try:
            positions = self._am.get_positions(force_refresh=True)
            mark_map = {p["symbol"]: p["mark_price"] for p in positions}
            for leg in legs:
                if not leg.filled and leg.symbol in mark_map:
                    leg.mark_price = mark_map[leg.symbol]
        except Exception:
            pass  # keep previous marks

    # -- Helpers --------------------------------------------------------------

    def _build_legs(self, positions: List[Dict[str, Any]]) -> List[_CloseLeg]:
        """Convert raw position dicts to _CloseLeg trackers."""
        legs = []
        for p in positions:
            close_side = "sell" if p["trade_side"] == 1 else "buy"
            legs.append(_CloseLeg(
                symbol=p["symbol"],
                qty=p["qty"],
                close_side=close_side,
                mark_price=p["mark_price"],
            ))
        return legs

    def _aggressive_price(self, leg: _CloseLeg) -> float:
        """10% worse than mark — guarantees fills on illiquid legs."""
        if leg.close_side == "sell":  # selling → go below mark
            return leg.mark_price * (1 - self.PHASE2_DISCOUNT)
        else:  # buying → go above mark
            return leg.mark_price * (1 + self.PHASE2_DISCOUNT)

    def _finalize(self, legs: List[_CloseLeg], start_time: float) -> None:
        """Cancel unfilled orders, verify, and send summary."""
        elapsed = time.time() - start_time
        filled = [l for l in legs if l.filled]
        unfilled = [l for l in legs if not l.filled]

        # Cancel any remaining orders
        for leg in unfilled:
            if leg.order_id:
                try:
                    self._executor.cancel_order(leg.order_id)
                except Exception:
                    pass

        # Verify with exchange
        time.sleep(3)
        remaining = self._am.get_positions(force_refresh=True)

        # Build summary
        lines = [
            f"{'✅' if not remaining else '⚠️'} <b>Kill switch complete</b> ({elapsed:.0f}s)",
            f"Closed: {len(filled)}/{len(legs)}",
        ]
        for leg in filled:
            lines.append(f"  • {leg.symbol} {leg.side_label} {leg.qty} @ ${leg.fill_price:.2f}")
        if unfilled:
            lines.append(f"<b>Unfilled: {len(unfilled)}</b>")
            for leg in unfilled:
                lines.append(f"  • {leg.symbol} {leg.side_label} {leg.qty}")
        if remaining:
            lines.append(f"<b>WARNING: {len(remaining)} position(s) still open</b>")

        summary = "\n".join(lines)
        logger.warning(f"Kill switch summary:\n{summary}")

        self._status = "done" if not remaining else f"done ({len(remaining)} still open)"
