# Order Management Refactoring Plan

**Created:** 2026-03-06
**Status:** ✅ Implemented in v1.0.0 (see CHANGELOG.md)
**Trigger:** 2026-03-05 production incident — runaway close orders built up a huge position

---

## 1. Problem Statement

On 2026-03-05 the system placed duplicate close orders because:

1. `_check_close_fills()` returned `"failed"` (requote rounds exhausted) →
   state reverted to `PENDING_CLOSE`, discarding the `LimitFillManager`.
2. Next tick: `close()` created a **new** `LimitFillManager` and placed fresh
   orders. Old orders were still live on the exchange (cancel can fail silently).
3. This repeated every 10 seconds. Each cycle placed new orders; old orders
   kept filling. Position snowballed.

The `reduce_only` fix added on 2026-03-05 is a safety net but does not fix
the structural problem: **the system has no authoritative order ledger**.

---

## 2. Design Principles

1. **Every order the system places is tracked in one central ledger** —
   from placement to terminal state.
2. **Idempotent placement** — requesting an order that already exists
   (same trade, same leg, same purpose) returns the existing record
   rather than placing a duplicate.
3. **Execution strategy is orthogonal to order tracking** — phased pricing
   (mark → mid → aggressive), timings, and requote logic remain in
   `LimitFillManager` / `ExecutionParams`. The order manager is a *transport*
   layer, not a *pricing* layer.
4. **RFQ execution is exempt** — RFQs are atomic quote-accept flows with
   no persistent order IDs. The order manager only governs orders placed via
   the `/open/option/order/create/v1` API.
5. **Strategies keep full control** of execution parameters — they decide
   phases, pricing modes, timeouts, and requote strategy via
   `ExecutionParams` / `ExecutionPhase` just as they do today.

---

## 3. Scope Boundaries

### In scope (order_manager)
- Tracking every order placed via TradeExecutor
- Idempotent placement guard (prevent duplicates per trade+leg+purpose)
- Polling order statuses from the exchange
- Persisting the order ledger for crash recovery
- Reconciliation against exchange open-orders endpoint
- Enforcing `reduce_only` on all CLOSE purpose orders
- Hard caps (max orders per lifecycle, max exposure per symbol)

### Out of scope (stays where it is)
- **Pricing logic** — `ExecutionPhase`, `_get_phased_price()`,
  `_get_aggressive_price()`, mid/mark/top-of-book calculations.
  All of this stays in `LimitFillManager` / `trade_execution.py`.
- **Phase sequencing** — the "try mark for 3 min then aggressive" flow is
  driven by `ExecutionParams.phases` and `LimitFillManager._check_phased()`.
  Unchanged.
- **RFQ execution** — `RFQExecutor` is unaffected. No orders to track.
- **Smart orderbook execution** — `SmartOrderbookExecutor` uses its own
  chunking logic. If it internally places orders via `TradeExecutor`, those
  should be routed through OrderManager. If it uses a different API path,
  it remains exempt (needs verification during implementation).
- **Strategy configuration** — `StrategyConfig`, entry/exit conditions,
  `StrategyRunner` are untouched.

---

## 4. New Module: `order_manager.py`

### 4.1 Data Model

```python
class OrderPurpose(Enum):
    OPEN_LEG   = "open_leg"    # Opening a position
    CLOSE_LEG  = "close_leg"   # Closing a position
    UNWIND     = "unwind"      # Unwinding a partially filled open

class OrderStatus(Enum):
    PENDING    = "pending"     # Placed, awaiting exchange ack
    LIVE       = "live"        # Confirmed on exchange (state=0 NEW)
    PARTIAL    = "partial"     # Partially filled (state=2)
    FILLED     = "filled"      # Fully filled (state=1)
    CANCELLED  = "cancelled"   # Cancelled (state=3,4,5)
    REJECTED   = "rejected"    # Exchange rejected / invalid (state=6)
    EXPIRED    = "expired"     # Cancelled by exercise (state=10)

@dataclass
class OrderRecord:
    """One order in the ledger. Immutable ID, mutable status fields."""
    # Identity
    order_id: str                      # Exchange order ID (set after placement)
    client_order_id: Optional[str]     # Our generated client ID (for reconciliation)

    # Linkage
    lifecycle_id: str                  # Which TradeLifecycle this belongs to
    leg_index: int                     # Index into open_legs or close_legs
    purpose: OrderPurpose              # What this order is for

    # Order details (immutable after placement)
    symbol: str
    side: int                          # 1=buy, 2=sell
    qty: float
    price: float
    reduce_only: bool

    # Status (updated by poll)
    status: OrderStatus = OrderStatus.PENDING
    filled_qty: float = 0.0
    avg_fill_price: Optional[float] = None

    # Timestamps
    placed_at: float                   # time.time() when we placed it
    updated_at: Optional[float] = None # last poll update
    terminal_at: Optional[float] = None # when it reached a terminal state

    # Supersession chain
    superseded_by: Optional[str] = None  # order_id of the replacement (on requote)
    supersedes: Optional[str] = None     # order_id this replaced

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            OrderStatus.FILLED, OrderStatus.CANCELLED,
            OrderStatus.REJECTED, OrderStatus.EXPIRED,
        )

    @property
    def is_live(self) -> bool:
        return self.status in (
            OrderStatus.PENDING, OrderStatus.LIVE, OrderStatus.PARTIAL,
        )
```

### 4.2 OrderManager API

```python
class OrderManager:
    """
    Central order ledger. All order placement and cancellation goes
    through here. Wraps TradeExecutor for the actual API calls.
    """

    def __init__(self, executor: TradeExecutor):
        self._executor = executor
        self._orders: Dict[str, OrderRecord] = {}  # order_id → record
        # Secondary index for idempotency checks
        self._active_by_key: Dict[tuple, str] = {}  # (lifecycle_id, leg_index, purpose) → order_id

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

        This is the IDEMPOTENCY GUARD — the single most important safety
        mechanism. No duplicate orders can be placed for the same leg.

        For requoting: caller must cancel_order() first (which clears the
        active slot), then place_order() with the new price. This is an
        explicit two-step: cancel-then-replace is intentional, never
        implicit.

        Enforcements:
          - CLOSE_LEG and UNWIND always set reduce_only=True regardless
            of the caller's argument.
          - Refuses to place if hard cap (max_orders_per_lifecycle) is hit.

        Returns:
            OrderRecord on success, None on failure.
            If an existing live order is found, returns it (no new order).
        """

    # ── Cancellation ─────────────────────────────────────────────────────

    def cancel_order(self, order_id: str) -> bool:
        """
        Cancel an order on the exchange and mark it CANCELLED in the ledger.

        Clears the active slot so a replacement can be placed.
        If the exchange cancel fails (order already filled, etc.), polls
        the order to get the true terminal state.
        """

    def cancel_all_for(self, lifecycle_id: str) -> int:
        """Cancel all live orders for a given lifecycle. Returns count cancelled."""

    def cancel_all(self) -> int:
        """Emergency: cancel every live order in the ledger."""

    # ── Requote (cancel + replace) ───────────────────────────────────────

    def requote_order(
        self,
        order_id: str,
        new_price: float,
        new_qty: Optional[float] = None,
    ) -> Optional[OrderRecord]:
        """
        Atomic cancel-and-replace for requoting.

        1. Polls the current order one final time (captures any last-
           second fills).
        2. If fully filled → returns None (no requote needed).
        3. Cancels the old order.
        4. Places a replacement for the remaining qty at the new price.
        5. Links old → new via superseded_by / supersedes.

        The caller (LimitFillManager) decides WHEN and at WHAT PRICE to
        requote. OrderManager only handles the safe execution.
        """

    # ── Status Polling ───────────────────────────────────────────────────

    def poll_all(self) -> None:
        """
        Poll exchange status for every non-terminal order in the ledger.

        Should be called ONCE at the start of each tick, BEFORE any
        lifecycle state transitions.

        Updates filled_qty, avg_fill_price, and status for each order.
        Detects externally cancelled orders (state=3 with no fill).
        """

    def poll_order(self, order_id: str) -> Optional[OrderRecord]:
        """Poll and update a single order. Returns updated record."""

    # ── Queries ──────────────────────────────────────────────────────────

    def get_live_orders(
        self,
        lifecycle_id: str,
        purpose: Optional[OrderPurpose] = None,
    ) -> List[OrderRecord]:
        """All non-terminal orders for a lifecycle, optionally filtered by purpose."""

    def get_all_orders(
        self,
        lifecycle_id: str,
        purpose: Optional[OrderPurpose] = None,
    ) -> List[OrderRecord]:
        """All orders (any state) for a lifecycle, optionally filtered."""

    def get_filled_for_leg(
        self,
        lifecycle_id: str,
        leg_index: int,
        purpose: OrderPurpose,
    ) -> Tuple[float, Optional[float]]:
        """
        Total filled qty and volume-weighted avg price across all orders
        (including superseded ones) for a specific leg.

        This is how close_legs and open_legs get their fill data —
        aggregated from the order chain, not from a single order.
        """

    def has_live_orders(self, lifecycle_id: str, purpose: OrderPurpose) -> bool:
        """Quick check: are there any non-terminal orders for this purpose?"""

    # ── Reconciliation ───────────────────────────────────────────────────

    def reconcile(self, exchange_open_orders: List[Dict]) -> List[str]:
        """
        Compare the ledger against the exchange's open-orders endpoint.

        Returns a list of warnings for:
          - Orders in ledger marked live but not on exchange (filled/cancelled externally)
          - Orders on exchange not in ledger (orphans — placed outside our system)

        Does NOT auto-fix — returns diagnostics for the lifecycle engine
        to act on. Orphan orders should be flagged for human review or
        auto-cancelled with a notification.
        """

    # ── Persistence ──────────────────────────────────────────────────────

    def persist_snapshot(self) -> None:
        """Write active_orders.json — all non-terminal orders."""

    def persist_event(self, order_id: str, action: str) -> None:
        """Append one line to order_ledger.jsonl (audit trail)."""

    def load_snapshot(self) -> None:
        """Load active_orders.json on startup for crash recovery."""

    # ── Safety Limits ────────────────────────────────────────────────────

    MAX_ORDERS_PER_LIFECYCLE: int = 30   # Hard cap (all orders, all legs)
    MAX_PENDING_PER_SYMBOL: int = 4      # Max live orders per symbol across all lifecycles
```

### 4.3 Relationship to `LimitFillManager`

The existing `LimitFillManager` remains the **execution strategy driver**.
It handles:
- Phase sequencing (`_check_phased()`)
- Pricing decisions (`_get_phased_price()`, aggressive, mid, mark, etc.)
- Reprice timing (duration, intervals)
- Deciding WHEN to requote

What changes: instead of calling `self._executor.place_order()` and
`self._executor.cancel_order()` directly, it calls:
- `self._order_manager.place_order(...)` for initial placement
- `self._order_manager.requote_order(order_id, new_price)` for repricing
- `self._order_manager.cancel_all_for(lifecycle_id)` for cleanup

This is a **thin substitution** — the LimitFillManager's logic, phases,
and timing are completely preserved.

```
Before:
  LimitFillManager ──→ TradeExecutor ──→ Exchange API
                       (no tracking)

After:
  LimitFillManager ──→ OrderManager ──→ TradeExecutor ──→ Exchange API
                       (ledger + guards)
```

### 4.4 Execution Strategy Independence

A strategy defines its execution approach via `ExecutionParams`:

```python
# Example: "try mark prices for 3 min, then aggressive"
StrategyConfig(
    execution_params=ExecutionParams(phases=[
        ExecutionPhase(pricing="mark", duration_seconds=180, reprice_interval=30),
        ExecutionPhase(pricing="aggressive", duration_seconds=120, buffer_pct=3.0, reprice_interval=15),
    ]),
)
```

**This is completely unchanged.** The `OrderManager` doesn't know or care
about pricing strategies. It only sees: "place order for symbol X at price P"
and "requote order 123 to price Q". The WHEN and WHAT PRICE decisions remain
100% with `LimitFillManager` driven by strategy-configured `ExecutionParams`.

Similarly, a strategy using `execution_mode="rfq"` bypasses the OrderManager
entirely — RFQ is quote-accept with no persistent orders.

---

## 5. Refactored Module Responsibilities

### 5.1 Current structure

```
trade_lifecycle.py  (1373 lines) — everything
trade_execution.py  (629 lines)  — TradeExecutor + LimitFillManager
```

### 5.2 Proposed structure

| Module | Lines (est.) | Responsibility |
|--------|-------------|----------------|
| `trade_lifecycle.py` | ~350 | `TradeState`, `TradeLeg`, `TradeLifecycle` dataclass, serialization, PnL helpers. **Pure data, no execution.** |
| `lifecycle_engine.py` | ~550 | `LifecycleEngine` (renamed from `LifecycleManager`) — state machine `tick()`, exit evaluation, transition guards, persistence, manual controls. Reads from OrderManager. |
| `order_manager.py` | ~400 | `OrderManager`, `OrderRecord`, `OrderPurpose`, `OrderStatus` — the order ledger. |
| `trade_execution.py` | ~600 | `TradeExecutor` (API client, unchanged), `LimitFillManager` (refactored to use OrderManager), `ExecutionParams`, `ExecutionPhase` (unchanged). |
| `execution_router.py` | ~200 | Extracted from LifecycleManager: `_open_rfq()`, `_open_limit()`, `_open_smart()`, `_close_rfq()`, `_close_limit()`, mode auto-detection. |

### 5.3 Dependency graph

```
strategy.py
    └── lifecycle_engine.py (creates trades, drives tick)
            ├── trade_lifecycle.py (data model)
            ├── execution_router.py (routes to correct executor)
            │       ├── rfq.py (RFQ — no OrderManager)
            │       ├── multileg_orderbook.py (smart — TBD)
            │       └── trade_execution.py / LimitFillManager
            │               └── order_manager.py (ledger + guards)
            │                       └── trade_execution.py / TradeExecutor (API transport)
            └── order_manager.py (reads order states for transition guards)
```

---

## 6. Revised Tick Flow

```
tick(account):
  ┌─────────────────────────────────────────────────────────────┐
  │ 1. order_manager.poll_all()                                 │
  │    Update every non-terminal order from exchange.            │
  │    This happens ONCE, BEFORE any lifecycle logic.            │
  └─────────────────────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────────────────────┐
  │ 2. For each active trade:                                    │
  │                                                              │
  │    OPENING:                                                  │
  │      Read order states from OrderManager                     │
  │      Sync fills to TradeLeg                                  │
  │      LimitFillManager.check() — may requote (via OM)        │
  │      If all legs filled → transition to OPEN                 │
  │      If failed → cancel remaining, unwind filled legs        │
  │                                                              │
  │    OPEN:                                                     │
  │      Evaluate exit conditions (unchanged)                    │
  │      If triggered → PENDING_CLOSE                            │
  │                                                              │
  │    PENDING_CLOSE:                                            │
  │      *** NEW GUARD: if order_manager.has_live_orders(        │
  │          trade_id, CLOSE_LEG) → DO NOTHING, wait for         │
  │          existing orders to resolve ***                       │
  │      Otherwise → execution_router.close(trade)               │
  │                                                              │
  │    CLOSING:                                                  │
  │      Read order states from OrderManager                     │
  │      Sync fills to close TradeLeg                            │
  │      LimitFillManager.check() — may requote (via OM)        │
  │      If all legs filled → CLOSED + finalize PnL              │
  │      If failed → mark all close orders terminal,             │
  │        THEN transition to PENDING_CLOSE                      │
  └─────────────────────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────────────────────┐
  │ 3. order_manager.reconcile(exchange_open_orders) [periodic] │
  │    Flag orphaned orders. Log warnings. Optional auto-cancel. │
  └─────────────────────────────────────────────────────────────┘
  ┌─────────────────────────────────────────────────────────────┐
  │ 4. Persist snapshots (trades + active orders)               │
  └─────────────────────────────────────────────────────────────┘
```

**Critical difference from today:** Step 2's `PENDING_CLOSE` has an explicit
guard. If close orders are already live on the exchange (from a previous tick
where the fill manager was discarded), we DON'T place new ones. We wait for
`poll_all()` to surface their status. This is the core fix for the runaway bug.

---

## 7. Crash Recovery

### Current
- `logs/trades_snapshot.json` — trade state, restored on startup
- No order state is persisted
- Recovery verifies exchange positions but not orders

### Proposed additions
- `logs/active_orders.json` — snapshot of all non-terminal OrderRecords
- `logs/order_ledger.jsonl` — append-only audit trail

### Recovery flow on startup

```
1. Load active_orders.json → populate OrderManager ledger
2. For each order still marked live:
   a. Poll exchange for true status
   b. If filled → update ledger, sync to trade leg
   c. If cancelled/rejected → update ledger
   d. If still live → keep tracking
3. Load trades_snapshot.json → populate LifecycleEngine
4. Reconcile: order_manager.reconcile(exchange_open_orders)
   Flag any orphans not in the ledger
5. Normal state normalization (OPENING → OPEN if positions confirmed, etc.)
6. Resume tick loop
```

This gives us full continuity across crashes. No more guessing whether
orders survived the restart.

---

## 8. Persistence Files Summary

| File | Format | Purpose | Write frequency |
|------|--------|---------|-----------------|
| `logs/trades_snapshot.json` | JSON | Trade states for crash recovery | Every tick |
| `logs/trade_history.jsonl` | JSONL | Completed trades (existing) | On trade close |
| `logs/active_orders.json` | JSON | **NEW** Non-terminal orders | Every tick |
| `logs/order_ledger.jsonl` | JSONL | **NEW** Order audit trail | On every order event |

---

## 9. Safety Invariants (Enforced by OrderManager)

| # | Invariant | Enforced by |
|---|-----------|-------------|
| 1 | No duplicate live orders for same (lifecycle, leg, purpose) | `place_order()` idempotency check |
| 2 | All CLOSE_LEG orders use `reduce_only=True` | `place_order()` forces it |
| 3 | Max N orders per lifecycle (default 30) | `place_order()` hard cap |
| 4 | Max M live orders per symbol (default 4) | `place_order()` check |
| 5 | Cancel must precede replace (explicit two-step) | `requote_order()` API design |
| 6 | No state transition while orders are in-flight | Lifecycle engine guard |
| 7 | Orphaned orders flagged and (optionally) cancelled | `reconcile()` |

---

## 10. Implementation Sequence

### Phase 1 — Core safety (prevents the bug)

1. **`order_manager.py`**: Build `OrderManager`, `OrderRecord`, enums.
   Include persistence (`active_orders.json`, `order_ledger.jsonl`).
   Write unit tests with a mock `TradeExecutor`.

2. **Wire `LimitFillManager` → `OrderManager`**: Replace direct
   `TradeExecutor.place_order()` / `cancel_order()` calls with
   `OrderManager.place_order()` / `requote_order()`.
   Preserve all phased pricing logic unchanged.

3. **Add transition guard in `tick()`**: Before calling `close()` for a
   `PENDING_CLOSE` trade, check `order_manager.has_live_orders()`.
   This is a ~5-line change in the existing `LifecycleManager.tick()`.

4. **Add `poll_all()` call at top of `tick()`**.

After Phase 1, the runaway-order bug is structurally impossible.
Estimated effort: ~2 days.

### Phase 2 — Structural cleanup

5. **Split `trade_lifecycle.py`** into `trade_lifecycle.py` (data) +
   `lifecycle_engine.py` (state machine) + `execution_router.py` (routing).

6. **Update `position_closer.py`**: Use `order_manager.cancel_all()` in
   the kill switch instead of manually iterating legs.

7. **Update crash recovery in `main.py`**: Load order ledger, reconcile
   against exchange.

Estimated effort: ~2 days.

### Phase 3 — Hardening

8. **Reconciliation**: Implement `reconcile()` with exchange open-orders
   endpoint. Run periodically (every 5th tick or similar).

9. **Smart executor integration**: Verify whether `SmartOrderbookExecutor`
   places orders via `TradeExecutor` internally. If yes, route through
   `OrderManager`.

10. **Dashboard integration**: Surface order ledger state on the dashboard
    (active orders, recent fills, orphan warnings).

11. **Telegram alerts**: Notify on orphan detection, hard cap hits, and
    reconciliation mismatches.

Estimated effort: ~2 days.

---

## 11. What Does NOT Change

- `StrategyConfig` and `StrategyRunner` — untouched
- `ExecutionParams` and `ExecutionPhase` — untouched
- `RFQExecutor` and `RFQParams` — untouched (exempt)
- Entry conditions and exit conditions — untouched
- `AccountManager` and `PositionMonitor` — untouched
- `market_data.py`, `auth.py`, `config.py` — untouched
- `persistence.py` (trade history) — untouched
- All strategy files in `strategies/` — untouched
- Pricing logic inside `LimitFillManager` — untouched (only the
  placement/cancel transport layer changes)

---

## 12. Migration Notes

- The `LimitFillManager` can be migrated incrementally: add an optional
  `order_manager` parameter. When present, use it; when absent, fall back
  to direct `TradeExecutor` calls. This allows testing the new path while
  keeping the old one as a fallback.

- The transition guard in `tick()` (checking for live orders before placing
  new close orders) can be added to the EXISTING `LifecycleManager` in a
  single commit, before any module splitting. This is the minimum viable
  fix and should go in first.

- The `BUG-2026-03-05` comments in the codebase (reduce_only, circuit
  breaker) should be preserved — they are still valid defence-in-depth
  even after OrderManager is in place.

---

## 13. Testing Strategy

| Test | Scope | What it verifies |
|------|-------|-----------------|
| Unit: OrderManager idempotency | order_manager.py | Duplicate placement returns existing order |
| Unit: OrderManager requote chain | order_manager.py | Cancel → replace links records correctly |
| Unit: OrderManager hard caps | order_manager.py | Refuses placement beyond limits |
| Unit: Reconciliation | order_manager.py | Detects orphans and phantom orders |
| Integration: LFM → OM flow | trade_execution + order_manager | Phased pricing works through OM |
| Integration: Tick guard | lifecycle_engine | PENDING_CLOSE blocked while close orders live |
| Integration: Crash recovery | main.py + order_manager | Orders restored and reconciled after restart |
| Scenario: Runaway close repro | end-to-end | Verify the 2026-03-05 bug cannot recur |

---

## Appendix A: Files to Create

- `order_manager.py` — the order ledger (new)
- `lifecycle_engine.py` — state machine (extracted from trade_lifecycle.py)
- `execution_router.py` — execution routing (extracted from trade_lifecycle.py)
- `tests/test_order_manager.py` — unit tests (new)
- `tests/test_lifecycle_engine.py` — integration tests (new)

## Appendix B: Files to Modify

- `trade_lifecycle.py` — strip down to data model only
- `trade_execution.py` — LimitFillManager uses OrderManager
- `strategy.py` — update imports (LifecycleManager → LifecycleEngine)
- `main.py` — crash recovery additions, import updates
- `position_closer.py` — use OrderManager for cancel-all
- `dashboard.py` — import updates, optional order ledger display
