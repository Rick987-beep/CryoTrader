#!/usr/bin/env python3
"""
Order Manager — Central Order Ledger

Tracks every order the system places, from placement to terminal state.
Prevents duplicate orders via idempotent placement, enforces safety limits,
and provides crash recovery via persistent snapshots.

Design principles (from ORDER_MANAGEMENT_PLAN.md):
  1. Every order is tracked in one central ledger.
  2. Idempotent placement — same (lifecycle, leg, purpose) returns existing.
  3. Execution strategy is orthogonal — OrderManager is transport, not pricing.
  4. RFQ execution is exempt (no persistent order IDs).

Usage:
    from order_manager import OrderManager, OrderPurpose
    from trade_execution import TradeExecutor

    om = OrderManager(TradeExecutor())

    record = om.place_order(
        lifecycle_id="abc123",
        leg_index=0,
        purpose=OrderPurpose.OPEN_LEG,
        symbol="BTCUSD-28MAR26-100000-C",
        side=1,
        qty=0.1,
        price=500.0,
    )

    # On each tick:
    om.poll_all()

    # Requote:
    om.requote_order(record.order_id, new_price=510.0)

    # Check before placing close orders:
    if not om.has_live_orders(lifecycle_id, OrderPurpose.CLOSE_LEG):
        om.place_order(...)
"""

import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LOGS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")


# =============================================================================
# Enums
# =============================================================================

class OrderPurpose(Enum):
    OPEN_LEG = "open_leg"
    CLOSE_LEG = "close_leg"
    UNWIND = "unwind"


class OrderStatus(Enum):
    PENDING = "pending"
    LIVE = "live"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


# Exchange state → our status mapping
_EXCHANGE_STATE_MAP = {
    0: OrderStatus.LIVE,       # NEW
    1: OrderStatus.FILLED,     # FILLED
    2: OrderStatus.PARTIAL,    # PARTIALLY_FILLED
    3: OrderStatus.CANCELLED,  # CANCELED
    4: OrderStatus.CANCELLED,  # PRE_CANCEL
    5: OrderStatus.CANCELLED,  # CANCELING
    6: OrderStatus.REJECTED,   # INVALID
    10: OrderStatus.EXPIRED,   # CANCEL_BY_EXERCISE
}

_TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.EXPIRED,
})

_LIVE_STATUSES = frozenset({
    OrderStatus.PENDING,
    OrderStatus.LIVE,
    OrderStatus.PARTIAL,
})


# =============================================================================
# Order Record
# =============================================================================

@dataclass
class OrderRecord:
    """One order in the ledger. Immutable ID, mutable status fields."""

    # Identity
    order_id: str
    client_order_id: Optional[str] = None

    # Linkage
    lifecycle_id: str = ""
    leg_index: int = 0
    purpose: OrderPurpose = OrderPurpose.OPEN_LEG

    # Order details (immutable after placement)
    symbol: str = ""
    side: int = 1
    qty: float = 0.0
    price: float = 0.0
    reduce_only: bool = False

    # Status (updated by poll)
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None

    # Timestamps
    placed_at: float = field(default_factory=time.time)
    updated_at: Optional[float] = None
    terminal_at: Optional[float] = None

    # Supersession chain
    superseded_by: Optional[str] = None
    supersedes: Optional[str] = None

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    @property
    def is_live(self) -> bool:
        return self.status in _LIVE_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "lifecycle_id": self.lifecycle_id,
            "leg_index": self.leg_index,
            "purpose": self.purpose.value,
            "symbol": self.symbol,
            "side": self.side,
            "qty": self.qty,
            "price": self.price,
            "reduce_only": self.reduce_only,
            "status": self.status.value,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "placed_at": self.placed_at,
            "updated_at": self.updated_at,
            "terminal_at": self.terminal_at,
            "superseded_by": self.superseded_by,
            "supersedes": self.supersedes,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "OrderRecord":
        return OrderRecord(
            order_id=d["order_id"],
            client_order_id=d.get("client_order_id"),
            lifecycle_id=d.get("lifecycle_id", ""),
            leg_index=d.get("leg_index", 0),
            purpose=OrderPurpose(d.get("purpose", "open_leg")),
            symbol=d.get("symbol", ""),
            side=d.get("side", 1),
            qty=d.get("qty", 0.0),
            price=d.get("price", 0.0),
            reduce_only=d.get("reduce_only", False),
            status=OrderStatus(d.get("status", "pending")),
            filled_qty=d.get("filled_qty", 0.0),
            avg_fill_price=d.get("avg_fill_price"),
            placed_at=d.get("placed_at", 0.0),
            updated_at=d.get("updated_at"),
            terminal_at=d.get("terminal_at"),
            superseded_by=d.get("superseded_by"),
            supersedes=d.get("supersedes"),
        )


# =============================================================================
# Order Manager
# =============================================================================

class OrderManager:
    """
    Central order ledger.  All order placement and cancellation goes
    through here.  Wraps TradeExecutor for the actual API calls.
    """

    MAX_ORDERS_PER_LIFECYCLE: int = 30
    MAX_PENDING_PER_SYMBOL: int = 4

    def __init__(self, executor: Any):
        """
        Args:
            executor: TradeExecutor instance (or mock for testing).
        """
        self._executor = executor
        self._orders: Dict[str, OrderRecord] = {}  # order_id → record
        # Secondary index: (lifecycle_id, leg_index, purpose) → order_id
        self._active_by_key: Dict[Tuple[str, int, str], str] = {}
        self._next_client_id: int = int(time.time() * 1000)

    # ── Placement ────────────────────────────────────────────────────────

    def place_order(
        self,
        lifecycle_id: str,
        leg_index: int,
        purpose: OrderPurpose,
        symbol: str,
        side: int,
        qty: float,
        price: float,
        reduce_only: bool = False,
    ) -> Optional[OrderRecord]:
        """
        Place an order, or return the existing live order if one already
        exists for (lifecycle_id, leg_index, purpose).

        Enforcements:
          - CLOSE_LEG and UNWIND always force reduce_only=True.
          - Refuses to place if hard cap (max_orders_per_lifecycle) is hit.
          - Refuses to place if max pending per symbol is hit.

        Returns:
            OrderRecord on success, None on failure.
            If an existing live order is found, returns it (no new order).
        """
        # --- Idempotency guard ---
        key = (lifecycle_id, leg_index, purpose.value)
        existing_id = self._active_by_key.get(key)
        if existing_id and existing_id in self._orders:
            existing = self._orders[existing_id]
            if existing.is_live:
                logger.info(
                    f"OrderManager: idempotent hit — returning existing order "
                    f"{existing.order_id} for {symbol} ({purpose.value})"
                )
                return existing

        # --- Safety: force reduce_only for close/unwind ---
        if purpose in (OrderPurpose.CLOSE_LEG, OrderPurpose.UNWIND):
            reduce_only = True

        # --- Hard cap: max orders per lifecycle ---
        lifecycle_count = sum(
            1 for r in self._orders.values()
            if r.lifecycle_id == lifecycle_id
        )
        if lifecycle_count >= self.MAX_ORDERS_PER_LIFECYCLE:
            logger.error(
                f"OrderManager: hard cap hit — {lifecycle_count} orders "
                f"for lifecycle {lifecycle_id} (max {self.MAX_ORDERS_PER_LIFECYCLE})"
            )
            return None

        # --- Hard cap: max pending per symbol ---
        pending_for_symbol = sum(
            1 for r in self._orders.values()
            if r.symbol == symbol and r.is_live
        )
        if pending_for_symbol >= self.MAX_PENDING_PER_SYMBOL:
            logger.error(
                f"OrderManager: hard cap hit — {pending_for_symbol} live orders "
                f"for {symbol} (max {self.MAX_PENDING_PER_SYMBOL})"
            )
            return None

        # --- Generate client order ID ---
        self._next_client_id += 1
        client_order_id = str(self._next_client_id)

        # --- Place via executor ---
        result = self._executor.place_order(
            symbol=symbol,
            qty=qty,
            side=side,
            order_type=1,
            price=price,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
        )
        if not result:
            logger.error(f"OrderManager: executor failed to place order for {symbol}")
            return None

        order_id = str(result.get("orderId", ""))
        if not order_id:
            logger.error(f"OrderManager: executor returned no orderId for {symbol}")
            return None

        # --- Record in ledger ---
        now = time.time()
        record = OrderRecord(
            order_id=order_id,
            client_order_id=client_order_id,
            lifecycle_id=lifecycle_id,
            leg_index=leg_index,
            purpose=purpose,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            reduce_only=reduce_only,
            status=OrderStatus.PENDING,
            placed_at=now,
        )
        self._orders[order_id] = record
        self._active_by_key[key] = order_id

        self.persist_event(order_id, "placed")
        logger.info(
            f"OrderManager: placed order {order_id} — "
            f"{symbol} {'buy' if side == 1 else 'sell'} {qty} @ {price} "
            f"({purpose.value}, lifecycle={lifecycle_id}, leg={leg_index})"
        )
        return record

    # ── Cancellation ─────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order on the exchange and mark it CANCELLED in the ledger.

        Clears the active slot so a replacement can be placed.
        If the exchange cancel fails, polls the order to get true state.
        """
        record = self._orders.get(order_id)
        if not record:
            logger.warning(f"OrderManager: cancel — order {order_id} not in ledger")
            return False

        if record.is_terminal:
            logger.info(f"OrderManager: order {order_id} already terminal ({record.status.value})")
            return True

        success = self._executor.cancel_order(order_id)

        if success:
            self._mark_terminal(record, OrderStatus.CANCELLED)
            logger.info(f"OrderManager: cancelled order {order_id}")
        else:
            # Cancel failed — poll to discover true state
            logger.warning(f"OrderManager: cancel failed for {order_id}, polling for true state")
            self.poll_order(order_id)

        return success

    def cancel_all_for(self, lifecycle_id: str) -> int:
        """Cancel all live orders for a given lifecycle. Returns count cancelled."""
        count = 0
        for record in list(self._orders.values()):
            if record.lifecycle_id == lifecycle_id and record.is_live:
                if self.cancel_order(record.order_id):
                    count += 1
        return count

    def cancel_all(self) -> int:
        """Emergency: cancel every live order in the ledger."""
        count = 0
        for record in list(self._orders.values()):
            if record.is_live:
                if self.cancel_order(record.order_id):
                    count += 1
        return count

    # ── Requote (cancel + replace) ───────────────────────────────────────

    def requote_order(
        self,
        order_id: str,
        new_price: float,
        new_qty: Optional[float] = None,
    ) -> Optional[OrderRecord]:
        """
        Atomic cancel-and-replace for requoting.

        1. Polls the current order (captures last-second fills).
        2. If fully filled → returns None (no requote needed).
        3. Cancels the old order.
        4. Places a replacement for the remaining qty at the new price.
        5. Links old → new via superseded_by / supersedes.

        Returns:
            New OrderRecord on success, None if fully filled or on failure.
        """
        record = self._orders.get(order_id)
        if not record:
            logger.warning(f"OrderManager: requote — order {order_id} not in ledger")
            return None

        # 1. Poll for latest state
        self.poll_order(order_id)

        # 2. If fully filled, no requote needed
        if record.status == OrderStatus.FILLED:
            logger.info(f"OrderManager: requote skipped — {order_id} already filled")
            return None

        # 3. Cancel old order
        remaining = record.qty - record.filled_qty
        if remaining <= 0:
            logger.info(f"OrderManager: requote skipped — {order_id} no remaining qty")
            return None

        self.cancel_order(order_id)

        # 4. Place replacement
        replacement = self.place_order(
            lifecycle_id=record.lifecycle_id,
            leg_index=record.leg_index,
            purpose=record.purpose,
            symbol=record.symbol,
            side=record.side,
            qty=new_qty if new_qty is not None else remaining,
            price=new_price,
            reduce_only=record.reduce_only,
        )

        if replacement is None:
            logger.error(f"OrderManager: requote — replacement failed for {order_id}")
            return None

        # 5. Link the chain
        record.superseded_by = replacement.order_id
        replacement.supersedes = record.order_id

        self.persist_event(order_id, "superseded")
        self.persist_event(replacement.order_id, "requoted_from")
        logger.info(
            f"OrderManager: requoted {order_id} → {replacement.order_id} "
            f"@ {new_price} (remaining {replacement.qty})"
        )
        return replacement

    # ── Status Polling ───────────────────────────────────────────────────

    def poll_all(self) -> None:
        """
        Poll exchange status for every non-terminal order in the ledger.

        Should be called ONCE at the start of each tick, BEFORE any
        lifecycle state transitions.
        """
        live_orders = [r for r in self._orders.values() if r.is_live]
        for record in live_orders:
            self.poll_order(record.order_id)

    def poll_order(self, order_id: str) -> Optional[OrderRecord]:
        """Poll and update a single order. Returns updated record."""
        record = self._orders.get(order_id)
        if not record or record.is_terminal:
            return record

        try:
            info = self._executor.get_order_status(order_id)
            if not info:
                return record

            now = time.time()
            record.updated_at = now

            # Update fill data
            fill_qty = float(info.get("fillQty", 0))
            if fill_qty > record.filled_qty:
                record.filled_qty = fill_qty
                avg_price = info.get("avgPrice")
                if avg_price:
                    record.avg_fill_price = float(avg_price)

            # Map exchange state to our status
            state_code = info.get("state")
            if state_code is not None:
                new_status = _EXCHANGE_STATE_MAP.get(int(state_code))
                if new_status and new_status != record.status:
                    old_status = record.status
                    record.status = new_status
                    if new_status in _TERMINAL_STATUSES:
                        self._mark_terminal(record, new_status)
                    logger.debug(
                        f"OrderManager: {order_id} status {old_status.value} → {new_status.value}"
                    )

        except Exception as e:
            logger.error(f"OrderManager: error polling order {order_id}: {e}")

        return record

    # ── Queries ──────────────────────────────────────────────────────────

    def get_live_orders(
        self,
        lifecycle_id: str,
        purpose: Optional[OrderPurpose] = None,
    ) -> List[OrderRecord]:
        """All non-terminal orders for a lifecycle, optionally filtered by purpose."""
        results = []
        for r in self._orders.values():
            if r.lifecycle_id == lifecycle_id and r.is_live:
                if purpose is None or r.purpose == purpose:
                    results.append(r)
        return results

    def get_all_orders(
        self,
        lifecycle_id: str,
        purpose: Optional[OrderPurpose] = None,
    ) -> List[OrderRecord]:
        """All orders (any state) for a lifecycle, optionally filtered."""
        results = []
        for r in self._orders.values():
            if r.lifecycle_id == lifecycle_id:
                if purpose is None or r.purpose == purpose:
                    results.append(r)
        return results

    def get_filled_for_leg(
        self,
        lifecycle_id: str,
        leg_index: int,
        purpose: OrderPurpose,
    ) -> Tuple[float, Optional[float]]:
        """
        Total filled qty and volume-weighted avg price across all orders
        (including superseded ones) for a specific leg.

        Returns:
            (total_filled_qty, vwap) — vwap is None if no fills.
        """
        total_qty = 0.0
        total_cost = 0.0

        for r in self._orders.values():
            if (r.lifecycle_id == lifecycle_id
                    and r.leg_index == leg_index
                    and r.purpose == purpose
                    and r.filled_qty > 0):
                total_qty += r.filled_qty
                price = r.avg_fill_price if r.avg_fill_price else r.price
                total_cost += r.filled_qty * price

        if total_qty <= 0:
            return (0.0, None)
        return (total_qty, total_cost / total_qty)

    def has_live_orders(self, lifecycle_id: str, purpose: OrderPurpose) -> bool:
        """Quick check: are there any non-terminal orders for this purpose?"""
        for r in self._orders.values():
            if (r.lifecycle_id == lifecycle_id
                    and r.purpose == purpose
                    and r.is_live):
                return True
        return False

    # ── Reconciliation ───────────────────────────────────────────────────

    def reconcile(self, exchange_open_orders: List[Dict]) -> List[str]:
        """
        Compare the ledger against the exchange's open-orders endpoint.

        Returns a list of warning strings for:
          - Orders in ledger marked live but not on exchange
          - Orders on exchange not in ledger (orphans)

        Skips PENDING orders (not yet confirmed by exchange) and orders
        placed within the last 30 seconds (exchange API propagation delay).
        """
        GRACE_PERIOD = 30.0  # seconds — skip recently-placed orders
        now = time.time()
        warnings = []
        exchange_ids = {str(o.get("order_id", "")) for o in exchange_open_orders}

        # Check ledger orders against exchange
        for record in self._orders.values():
            if not record.is_live:
                continue
            # Skip PENDING — not yet confirmed on exchange
            if record.status == OrderStatus.PENDING:
                continue
            # Skip recently-placed orders (exchange API propagation delay)
            if now - record.placed_at < GRACE_PERIOD:
                continue
            if record.order_id not in exchange_ids:
                msg = (
                    f"Ledger order {record.order_id} ({record.symbol}) marked "
                    f"{record.status.value} but not found on exchange"
                )
                warnings.append(msg)
                logger.warning(f"OrderManager reconcile: {msg}")

        # Check exchange orders against ledger
        ledger_ids = set(self._orders.keys())
        for oid in exchange_ids:
            if oid and oid not in ledger_ids:
                msg = f"Orphan order {oid} found on exchange but not in ledger"
                warnings.append(msg)
                logger.warning(f"OrderManager reconcile: {msg}")

        return warnings

    # ── Persistence ──────────────────────────────────────────────────────

    def persist_snapshot(self) -> None:
        """Write active_orders.json — all non-terminal orders."""
        snapshot = [r.to_dict() for r in self._orders.values() if not r.is_terminal]
        path = os.path.join(LOGS_DIR, "active_orders.json")
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            tmp_path = path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(snapshot, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception as e:
            logger.error(f"OrderManager: failed to persist snapshot: {e}")

    def persist_event(self, order_id: str, action: str) -> None:
        """Append one line to order_ledger.jsonl (audit trail)."""
        record = self._orders.get(order_id)
        if not record:
            return
        event = {
            "ts": time.time(),
            "action": action,
            "order_id": order_id,
            "lifecycle_id": record.lifecycle_id,
            "leg_index": record.leg_index,
            "purpose": record.purpose.value,
            "symbol": record.symbol,
            "side": record.side,
            "qty": record.qty,
            "price": record.price,
            "status": record.status.value,
            "filled_qty": record.filled_qty,
        }
        path = os.path.join(LOGS_DIR, "order_ledger.jsonl")
        try:
            os.makedirs(LOGS_DIR, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(event) + "\n")
        except Exception as e:
            logger.error(f"OrderManager: failed to persist event: {e}")

    def load_snapshot(self) -> None:
        """Load active_orders.json on startup for crash recovery.

        Handles file corruption gracefully (e.g. null bytes from power
        loss): quarantines the corrupt file and starts with an empty
        ledger rather than propagating the error.
        """
        path = os.path.join(LOGS_DIR, "active_orders.json")
        if not os.path.exists(path):
            logger.info("OrderManager: no active_orders.json found, starting fresh")
            return

        # Detect null-byte corruption (power-loss / hard reboot)
        try:
            with open(path, "rb") as f:
                raw = f.read(512)
            if not raw or raw == b"\x00" * len(raw):
                self._quarantine(path, "null bytes")
                return
        except Exception:
            pass

        try:
            with open(path, "r") as f:
                data = json.load(f)
            for d in data:
                record = OrderRecord.from_dict(d)
                self._orders[record.order_id] = record
                # Rebuild active index for live orders
                if record.is_live:
                    key = (record.lifecycle_id, record.leg_index, record.purpose.value)
                    self._active_by_key[key] = record.order_id
            logger.info(f"OrderManager: loaded {len(data)} orders from snapshot")
        except Exception as e:
            self._quarantine(path, str(e))

    @staticmethod
    def _quarantine(path: str, reason: str) -> None:
        """Move a corrupt snapshot aside so it doesn't block future startups."""
        try:
            ts = int(time.time())
            dest = f"{path}.corrupt.{ts}"
            os.replace(path, dest)
            logger.warning(
                f"OrderManager: active_orders.json is corrupt ({reason}) "
                f"— quarantined to {dest}, starting with empty ledger"
            )
        except Exception as qe:
            logger.error(f"OrderManager: quarantine failed: {qe}")

    # ── Internal helpers ─────────────────────────────────────────────────

    def _mark_terminal(self, record: OrderRecord, status: OrderStatus) -> None:
        """Mark an order as terminal and clear its active slot."""
        record.status = status
        record.terminal_at = time.time()
        record.updated_at = record.terminal_at

        # Clear the active slot so a replacement can be placed
        key = (record.lifecycle_id, record.leg_index, record.purpose.value)
        if self._active_by_key.get(key) == record.order_id:
            del self._active_by_key[key]

        self.persist_event(record.order_id, f"terminal_{status.value}")
