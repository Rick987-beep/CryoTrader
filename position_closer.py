#!/usr/bin/env python3
"""
Position Closer — Two-Phase Mark-Price Close (Kill Switch)

Closes ALL open exchange positions using a graceful two-phase approach:

  Phase 1 — Mark Price (5 min default)
      Places limit orders at the exchange mark price with reduce_only.
      Reprices every 30s with fresh mark data.

  Phase 2 — Aggressive (2 min default)
      Drops sell price 10% below mark (raises buy price 10% above mark).
      Reprices every 15s.

Designed for the dashboard kill switch:
  - Runs in a background thread (non-blocking)
  - Kills all lifecycle-managed trades (cancels fill managers + orders)
  - Disables all strategy runners
  - Closes remaining exchange positions directly
  - Sends Telegram notifications at each stage
  - Safe to re-run (skips already-closed positions)

Exchange compatibility:
  - Coincall (slot-01): USD-denominated prices, int side (1=buy, 2=sell)
  - Deribit  (slot-02): BTC-denominated prices, string side ("buy"/"sell")
  Both are handled transparently via the adapter layer.
"""

import logging
import os
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from config import EXCHANGE
from telegram_notifier import get_notifier

if TYPE_CHECKING:
    from strategy import StrategyRunner
    from lifecycle_engine import LifecycleEngine

logger = logging.getLogger(__name__)

# Deribit uses BTC-native prices; Coincall uses USD.
_IS_DERIBIT = EXCHANGE == "deribit"


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


def _fmt_price(price: float) -> str:
    """Format a price for logging — BTC (6dp) or USD (2dp)."""
    if _IS_DERIBIT:
        return f"{price:.6f} BTC"
    return f"${price:.2f}"


# =============================================================================
# Position Closer
# =============================================================================

class PositionCloser:
    """
    Two-phase mark-price position closer for the dashboard kill switch.

    Usage:
        closer = PositionCloser(executor, lifecycle_engine)
        closer.start(runners)     # non-blocking, runs in background thread
        closer.is_running         # True while closing
        closer.status             # human-readable phase string
    """

    # Timing parameters
    PHASE1_DURATION: int = 300     # 5 minutes at mark price
    PHASE1_REPRICE: int = 30       # reprice every 30s
    PHASE2_DURATION: int = 120     # 2 minutes aggressive
    PHASE2_REPRICE: int = 15       # reprice every 15s
    PHASE2_DISCOUNT: float = 0.10  # 10% aggressive pricing
    POLL_INTERVAL: int = 10        # check fills every 10s

    def __init__(
        self,
        account_manager: Any,
        executor: Any,
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
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

        try:
            self._notify(
                f"\U0001f6a8 <b>KILL SWITCH ACTIVATED</b>\n\n"
                f"Time: {ts}\n"
                f"Exchange: {EXCHANGE}\n"
                f"Stopping strategies, cancelling orders, closing positions..."
            )

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
                self._notify(
                    "\u2705 <b>Kill switch complete</b>\n\n"
                    f"Killed {killed} lifecycle trade(s)\n"
                    f"Stopped {len(runners)} strategy runner(s)\n"
                    "No exchange positions to close"
                )
                return

            # 5. Build leg trackers
            legs = self._build_legs(positions)
            logger.warning(
                f"Kill switch: {len(legs)} position(s) to close: "
                + ", ".join(f"{l.symbol} ({l.side_label} {l.qty})" for l in legs)
            )

            # 6. Phase 1: mark price
            self._status = f"phase1 ({len(legs)} legs)"
            self._run_phase(
                legs,
                phase_name="Phase 1 (mark price)",
                duration=self.PHASE1_DURATION,
                reprice_interval=self.PHASE1_REPRICE,
                price_fn=lambda leg: leg.mark_price,
            )

            unfilled = [l for l in legs if not l.filled]
            if not unfilled:
                self._finalize(legs, start_time, killed, len(runners))
                return

            # 7. Phase 2: aggressive pricing
            self._status = f"phase2 ({len(unfilled)} remaining)"
            self._refresh_marks(legs)
            self._run_phase(
                legs,
                phase_name="Phase 2 (aggressive)",
                duration=self.PHASE2_DURATION,
                reprice_interval=self.PHASE2_REPRICE,
                price_fn=self._aggressive_price,
            )

            # 8. Finalize
            self._finalize(legs, start_time, killed, len(runners))

        except Exception as e:
            logger.error(f"Kill switch error: {e}", exc_info=True)
            self._status = f"error: {e}"
            self._notify(f"\u274c <b>Kill switch ERROR</b>\n\n{e}")
        finally:
            self._running = False
            # Trigger clean shutdown of the main process so systemd
            # sees a clean exit and the crash flag is removed.
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

        # Coincall executor expects side as int (1=buy, 2=sell);
        # Deribit adapter expects side as string ("buy"/"sell").
        if _IS_DERIBIT:
            side: Any = leg.close_side
        else:
            side = 1 if leg.close_side == "buy" else 2

        result = self._executor.place_order(
            symbol=leg.symbol,
            qty=leg.qty,
            side=side,
            order_type=1,  # limit
            price=price,
            reduce_only=True,
        )

        if result:
            leg.order_id = str(result.get("orderId", ""))
            logger.info(
                f"Kill switch: {leg.side_label} {leg.qty}x {leg.symbol} "
                f"@ {_fmt_price(price)} (order {leg.order_id})"
            )
            return True

        logger.error(
            f"Kill switch: failed to place order for {leg.symbol} "
            f"@ {_fmt_price(price)}"
        )
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
                # Coincall: state == 1 (filled int)
                # Deribit:  state == "filled" (filled string)
                if state == 1 or state == "filled" or fill_qty >= leg.qty:
                    leg.filled = True
                    leg.fill_price = float(status.get("avgPrice", 0))
                    logger.info(
                        f"Kill switch: FILLED {leg.symbol} "
                        f"@ {_fmt_price(leg.fill_price)}"
                    )
            except Exception as e:
                logger.warning(f"Kill switch: error checking {leg.symbol}: {e}")

    def _refresh_marks(self, legs: List[_CloseLeg]) -> None:
        """Refresh mark prices from fresh exchange position data."""
        try:
            positions = self._am.get_positions(force_refresh=True)
            mark_map: Dict[str, float] = {}
            for p in positions:
                # Deribit: BTC-native mark; Coincall: USD mark
                mark = p.get("_mark_price_btc") or p["mark_price"]
                mark_map[p["symbol"]] = mark
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
            # Deribit: BTC-native mark price; Coincall: USD mark price.
            mark = p.get("_mark_price_btc") or p["mark_price"]
            legs.append(_CloseLeg(
                symbol=p["symbol"],
                qty=abs(p["qty"]),
                close_side=close_side,
                mark_price=mark,
            ))
        return legs

    def _aggressive_price(self, leg: _CloseLeg) -> float:
        """10% worse than mark — guarantees fills on illiquid legs."""
        if leg.close_side == "sell":  # selling → go below mark
            return leg.mark_price * (1 - self.PHASE2_DISCOUNT)
        else:  # buying → go above mark
            return leg.mark_price * (1 + self.PHASE2_DISCOUNT)

    def _finalize(
        self,
        legs: List[_CloseLeg],
        start_time: float,
        killed_trades: int,
        stopped_runners: int,
    ) -> None:
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
        ok = not remaining
        emoji = "\u2705" if ok else "\u26a0\ufe0f"
        lines = [
            f"{emoji} <b>Kill switch complete</b> ({elapsed:.0f}s)\n",
            f"Exchange: {EXCHANGE}",
            f"Lifecycle trades killed: {killed_trades}",
            f"Strategy runners stopped: {stopped_runners}",
            f"Positions closed: {len(filled)}/{len(legs)}",
        ]
        for leg in filled:
            lines.append(
                f"  \u2022 {leg.symbol} {leg.side_label} {leg.qty} "
                f"@ {_fmt_price(leg.fill_price)}"
            )
        if unfilled:
            lines.append(f"\n<b>Unfilled: {len(unfilled)}</b>")
            for leg in unfilled:
                lines.append(f"  \u2022 {leg.symbol} {leg.side_label} {leg.qty}")
        if remaining:
            lines.append(
                f"\n<b>WARNING: {len(remaining)} position(s) still open on exchange</b>"
            )

        summary = "\n".join(lines)
        logger.warning(f"Kill switch summary:\n{summary}")
        self._notify(summary)

        self._status = "done" if ok else f"done ({len(remaining)} still open)"

    def _notify(self, message: str) -> None:
        """Send a Telegram notification (fire-and-forget)."""
        try:
            get_notifier().send(message)
        except Exception:
            pass
