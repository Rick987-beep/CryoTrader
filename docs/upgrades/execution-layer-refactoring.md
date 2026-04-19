# Execution Layer Refactoring — Review & Implementation Plan

**Date:** 2026-04-15  
**Status:** Proposal  
**Scope:** The "lower" trade execution layer — everything below `LifecycleEngine` that places orders, calculates prices, tracks order state, and reports results.

---

## Table of Contents

1. [Current State Assessment](#1-current-state-assessment)
2. [Problem Summary](#2-problem-summary)
3. [Target Architecture](#3-target-architecture)
4. [Module Design](#4-module-design)
5. [Data Structures](#5-data-structures)
6. [Pricing Engine](#6-pricing-engine)
7. [Execution Profiles & Configuration](#7-execution-profiles--configuration)
8. [Price Denomination & Currency Handling](#8-price-denomination--currency-handling)
9. [Multi-Leg Execution (3- and 4-Leg Structures)](#9-multi-leg-execution-3--and-4-leg-structures)
10. [RFQ Integration & Hybrid Execution](#10-rfq-integration--hybrid-execution)
11. [Fee Tracking & Reporting](#11-fee-tracking--reporting)
12. [Logging Integration](#12-logging-integration)
13. [State Reporting & Observability](#13-state-reporting--observability)
14. [Implementation Phases](#14-implementation-phases)
15. [Testing Strategy (Unit, Integration, Live)](#15-testing-strategy-unit-integration-live)
16. [File Inventory & Diffs](#16-file-inventory--diffs)

---

## 1. Current State Assessment

### 1.1 Module Map (execution layer, 5,566 lines)

| Module | Lines | Responsibility | Problems |
|--------|-------|---------------|----------|
| `trade_execution.py` | 1040 | `TradeExecutor` (Coincall REST), `ExecutionPhase`, `ExecutionParams`, `LimitFillManager` | **God module** — mixes transport, data definitions, pricing logic, and fill management. Pricing logic (300+ lines inside `_get_phased_price`) is deeply embedded in the fill manager. |
| `order_manager.py` | 772 | `OrderRecord`, `OrderManager` (ledger, idempotent placement, requote, reconcile) | Solid, but `OrderRecord` carries bag-of-fields that overlap with `TradeLeg`. State map coupling to Coincall ints, patched for Deribit strings. |
| `execution_router.py` | 412 | Routes open/close to limit or RFQ; mark-logging side effects | Routes + assembles `LimitFillManager` instances — tight coupling to FillManager constructor. Also stashes mark prices in `trade.metadata` (not its job). |
| `lifecycle_engine.py` | 689 | State machine, tick, reconciliation, persistence | Clean design, but fill-result → leg sync code is repeated (open + close), and close-retry logic is baked in. |
| `trade_lifecycle.py` | 440 | `TradeState`, `TradeLeg`, `TradeLifecycle`, `RFQParams`, `ExitCondition` | Data definitions mixed with PnL/Greeks computation that pulls live orderbooks. `executable_pnl()` does I/O inside a dataclass. |

### 1.2 Data Flow (current)

```
Strategy
  │  sets ExecutionParams (phases list or legacy flat fields)
  ▼
LifecycleEngine.open(trade_id)
  │  delegates to ExecutionRouter.open(trade)
  ▼
ExecutionRouter._open_limit(trade)
  │  creates LimitFillManager(executor, params, order_manager, market_data)
  │  calls mgr.place_all(legs)  ← pricing computed inside LFM
  │  stashes mgr in trade.metadata["_open_fill_mgr"]
  ▼
LifecycleEngine.tick()
  │  calls _check_open_fills(trade)
  │  reads mgr from metadata, calls mgr.check()
  │  syncs fill state back to TradeLeg fields
  ▼
(fills complete → OPEN → exit conditions → PENDING_CLOSE → same path for close)
```

### 1.3 Known Incidents & Pain Points

| Date | Incident | Root Cause |
|------|----------|-----------|
| 2026-04-15 | `price_too_high` — Deribit rejected order | USD mark price submitted as BTC price. `_get_phased_price` short-circuited on non-zero USD mark. Fixed inline with 40+ lines of denomination logic and plausibility guard. |
| 2026-03-05 | Close orders building reverse positions | `reduce_only` not passed through requote path. Patched in 3 places with `BUG-2026-03-05` comments. |
| Ongoing | Pricing logic sprawl | `_get_phased_price` is 150+ lines with nested if/elif for 6 pricing modes. Each new mode requires editing deep inside `LimitFillManager`. Adding "mid-with-offset" or "VWAP-snap" would be high-risk. |
| Ongoing | State handshake via `trade.metadata` dict | Fill managers stashed in `trade.metadata["_open_fill_mgr"]` — untyped, not serializable, invisible to tests without knowing the key name. |
| Ongoing | No currency type safety | Prices flow as raw `float` everywhere. A BTC price and a USD price are the same Python type. The plausibility guard (`price > 1.0`) is the only runtime check. |
| Ongoing | No centralized execution defaults | Each strategy hard-codes its `ExecutionParams`. No TOML/config for execution profiles. Changing the default close escalation means editing every strategy file. |
| Ongoing | Legacy/phased bifurcation | `ExecutionParams` has dual modes (flat legacy fields vs phases list). `LimitFillManager` has `_check_legacy()` and `_check_phased()`. Two code paths to maintain forever. |
| Ongoing | Zero fee tracking | Exchange fees are not captured at fill time, not stored in `OrderRecord` or `TradeLeg`, not deducted from PnL. Telegram notifications and `realized_pnl` are always fee-blind. |
| Ongoing | RFQ module is dormant but coupled | `rfq.py` (839 lines) is Coincall-only, not used in production, but wired into `ExecutionRouter`. No path to use RFQ as a "first try" before falling back to limit phases. |

---

## 2. Problem Summary

1. **`trade_execution.py` is a monolith** — transport, pricing, fill management, and data definitions are tangled in one 1040-line file.
2. **Pricing logic is buried** — the core price computation (fair value, denomination handling, aggression interpolation, guards) lives inside a private method of `LimitFillManager`. Strategies can't unit-test pricing independently or compose custom pricing functions.
3. **No currency type safety** — a BTC price and a USD price are both `float`. The system relies on convention and a single plausibility guard.
4. **Execution config is scattered** — every strategy builds `ExecutionParams` inline. No shared profiles, no config file.
5. **State handover is ad-hoc** — `trade.metadata` is a `Dict[str, Any]` grab-bag used for fill managers, mark prices, SL thresholds, fair prices, Telegram formatting data, and internal flags.
6. **Fill result reporting is primitive** — `LimitFillManager.check()` returns a string ("filled"/"requoted"/"failed"/"pending"). The caller then manually zips `mgr.filled_legs` to `trade.open_legs` by index or symbol. No structured result object.
7. **Fees are invisible** — the system has zero awareness of trading fees. `realized_pnl` is always overstated. No `fee` field on `OrderRecord`, `TradeLeg`, or `FillResult`. The existing `exchange-trade-log-integration.md` plan describes post-close reconciliation, but the execution layer should at least capture the fee data that the exchange returns at fill time.
8. **RFQ has no hybrid mode** — `ExecutionRouter` routes to either limit or RFQ, one-shot. There's no way to try RFQ for N minutes and then fall back to phased limit if the quote is bad. This was a common production pattern before RFQ was disabled.
9. **Only 1- and 2-leg structures tested** — the code uses `for leg in trade.open_legs` loops everywhere, so N-leg should work mechanically. But no test coverage for 3-leg (butterfly) or 4-leg (iron condor) structures ensures that best_effort partial fills, unwind logic, and PnL accounting work correctly for N>2.

---

## 3. Target Architecture

### 3.1 Design Principles

1. **Pricing is a first-class, independent module.** Strategies can call pricing functions directly for SL evaluation, display, or custom logic — without going through a fill manager.
2. **Prices carry their denomination.** A `Price` value object knows whether it's BTC or USD.
3. **Execution profiles are declarative and configurable.** Strategies name a profile; the profile lives in a TOML file and can be overridden per-slot.
4. **Fill management reports structured results.** No string returns. A `FillResult` dataclass describes what happened, what filled, what's pending, and why.
5. **State flows through typed objects, not `metadata` dicts.** Fill contexts, mark snapshots, and pricing snapshots are explicit fields on `TradeLifecycle`.
6. **Single code path for phased execution.** Legacy flat fields are removed. A 1-phase profile replaces the old behavior.

### 3.2 Target Module Map

```
execution/
├── __init__.py           # Public re-exports
├── pricing.py            # PricingEngine: stateless price computation
├── fill_manager.py       # FillManager: order lifecycle (place, poll, requote, cancel)
├── fill_result.py        # FillResult, LegFillState dataclasses
├── router.py             # ExecutionRouter: routes open/close to limit/rfq/hybrid
├── profiles.py           # ExecutionProfile, PhaseConfig — loaded from TOML + code
├── currency.py           # Price, Currency, denomination conversion
└── fees.py               # Fee extraction helpers: parse fee data from exchange responses

order_manager.py          # Stays — already well-factored
trade_lifecycle.py        # Stays — trimmed of I/O methods
lifecycle_engine.py       # Stays — simplified fill sync via FillResult
rfq.py                    # Stays — but gains ExchangeRFQExecutor adapter for Deribit
```

### 3.3 Dependency Graph (target)

```
Strategy
  │  names an ExecutionProfile (e.g. "passive_open_3phase")
  │  optionally overrides individual phase parameters
  ▼
LifecycleEngine
  │  resolves profile → PhaseConfig list (or hybrid RFQ-then-limit)
  │  passes to ExecutionRouter
  ▼
ExecutionRouter
  │  for "hybrid": tries RFQ → on timeout/bad-price → hands over to FillManager
  │  for "limit":  creates FillManager(phases, pricing_engine, order_manager)
  │  for "rfq":    delegates to RFQExecutor directly
  ▼
FillManager
  │  asks PricingEngine.compute(symbol, side, mode, orderbook) for each leg
  │  places/requotes via OrderManager
  │  captures per-fill fees from exchange response (as Price objects)
  │  returns FillResult (includes fees per leg)
  ▼
LifecycleEngine
  │  reads FillResult.legs → syncs to TradeLifecycle.open_legs
  │  accumulates fees on TradeLifecycle
  │  emits structured log events to ct.execution + ct.strategy
```

### 3.4 Target Strategies

Three strategies will be rewritten against the new execution API:

| Strategy | Legs | Profile | Notes |
|----------|------|---------|-------|
| `short_strangle_delta_tp` | 2 (call + put) | `delta_strangle_2phase` | SL + TP + max-hold + expiry |
| `put_sell_80dte` | 1 (put) | `passive_open_3phase` | SL + TP + expiry, EMA filter |
| `long_strangle_index_move` | 2 (call + put) | `aggressive_2phase` | Index-move exit + time hard close |

Other strategies (`daily_put_sell`, `atm_straddle_index_move`, `short_straddle_strangle`, `blueprint_strangle`) are not in active production. They will not be ported unless needed later — the old code can remain in `archive/` as reference.

---

## 4. Module Design

### 4.1 `execution/pricing.py` — PricingEngine

**Purpose:** Stateless computation of order prices. Given an orderbook snapshot and a pricing mode, returns a `Price` (with denomination).

```python
class PricingEngine:
    """Stateless order-price calculator.  No I/O — receives orderbook data."""

    def compute(
        self,
        orderbook: OrderbookSnapshot,
        side: str,                     # "buy" or "sell"
        mode: str,                     # "fair", "aggressive", "mid", "passive", "top_of_book", "mark"
        aggression: float = 0.0,       # 0.0–1.0, used by "fair" mode
        buffer_pct: float = 2.0,       # used by "aggressive" mode
        min_price_pct_of_fair: Optional[float] = None,
        min_floor_price: Optional[Price] = None,
    ) -> PricingResult:
        """
        Compute the order price for a single leg.

        Returns PricingResult containing:
          - price: Optional[Price] — the computed price (None if refused)
          - fair_value: Price — the fair value estimate (always computed)
          - reason: str — human-readable explanation ("fair inside spread", "bid-only fallback", etc.)
          - refused: bool — True if a guard (floor, plausibility) blocked the price
        """
```

**Key characteristics:**
- Each pricing mode is a separate private method. Adding a new mode is a one-method addition + registration.
- Denomination is resolved at the top of `compute()` from `OrderbookSnapshot.currency`.
- The BTC plausibility guard moves here — it's a pricing concern, not a fill-management concern.
- The `compute_fair_price()` function currently in `put_sell_80dte.py` becomes `PricingEngine.fair_value()` — a public method strategies can call directly for SL evaluation, display, etc.

**Pricing modes (initial):**

| Mode | Sell price | Buy price |
|------|-----------|-----------|
| `fair` | `fair_value - aggression * (fair - bid)` | `fair_value + aggression * (ask - fair)` |
| `aggressive` | `bid / (1 + buffer)` | `ask * (1 + buffer)` |
| `mid` | `(bid + ask) / 2` | `(bid + ask) / 2` |
| `passive` | `ask` (join the ask) | `bid` (join the bid) |
| `top_of_book` | `bid` (hit the bid) | `ask` (lift the ask) |
| `mark` | mark price | mark price |

New modes can be added by implementing:
```python
def _price_<mode>(self, ob, side, **kwargs) -> Optional[Price]:
```

### 4.2 `execution/currency.py` — Price & Currency

**Purpose:** Type-safe price representation that prevents denomination errors at the type level.

```python
class Currency(Enum):
    BTC = "BTC"
    USD = "USD"
    ETH = "ETH"

@dataclass(frozen=True)
class Price:
    """A price value with its denomination."""
    amount: float
    currency: Currency

    def to_btc(self, index_price: float) -> "Price":
        """Convert to BTC denomination (no-op if already BTC)."""
        if self.currency == Currency.BTC:
            return self
        return Price(self.amount / index_price, Currency.BTC)

    def to_usd(self, index_price: float) -> "Price":
        """Convert to USD denomination (no-op if already USD)."""
        if self.currency == Currency.USD:
            return self
        return Price(self.amount * index_price, Currency.USD)

    def __float__(self) -> float:
        """Raw numeric value — use only when denomination is already verified."""
        return self.amount

@dataclass
class OrderbookSnapshot:
    """Typed snapshot of an orderbook at a point in time."""
    symbol: str
    currency: Currency          # denomination of bid/ask/mark prices
    best_bid: Optional[float]
    best_ask: Optional[float]
    mark: Optional[float]       # native denomination (BTC for Deribit, USD for Coincall)
    index_price: Optional[float]  # always USD
    timestamp: float
```

**Why this matters:** The 2026-04-15 `price_too_high` incident happened because a USD float was treated as a BTC float. With typed prices, `order_manager.place_order()` can assert that `price.currency` matches the exchange's expected denomination before submitting. The exchange adapters already know their denomination — we just need to propagate it.

### 4.3 `execution/fill_manager.py` — FillManager

**Purpose:** Manages a batch of limit orders through phased fill lifecycle. Extracted from current `LimitFillManager`, but:
- Receives a `PricingEngine` instead of computing prices internally.
- Returns `FillResult` instead of strings.
- Only supports phased execution (legacy mode removed; a 1-phase profile replaces it).

```python
class FillManager:
    def __init__(
        self,
        order_manager: OrderManager,
        pricing_engine: PricingEngine,
        market_data: ExchangeMarketData,
        phases: List[PhaseConfig],
    ):
        ...

    def place_all(self, legs, lifecycle_id, purpose, ...) -> FillResult:
        """Place initial orders for all legs. Returns structured result."""

    def check(self) -> FillResult:
        """Poll fills, handle phase transitions. Returns structured result."""

    def cancel_all(self) -> None:
        """Cancel all outstanding orders."""
```

### 4.4 `execution/fill_result.py` — FillResult

**Purpose:** Structured result object replacing string returns and manual index-zipping.

```python
class FillStatus(Enum):
    PENDING = "pending"       # still waiting for fills
    FILLED = "filled"         # all placed legs filled
    PARTIAL = "partial"       # some legs filled, some still pending
    REQUOTED = "requoted"     # phase timeout, orders repriced
    FAILED = "failed"         # all phases exhausted
    REFUSED = "refused"       # pricing refused to place (guards triggered)

@dataclass
class LegFillSnapshot:
    """Per-leg fill state at a point in time."""
    symbol: str
    side: str
    qty: float
    filled_qty: float
    fill_price: Optional[Price]
    order_id: Optional[str]
    skipped: bool             # True if this leg was skipped (best_effort)
    skip_reason: Optional[str]
    fee: Optional[Price]      # exchange-reported fee for this leg, native denomination (None if not yet known)

@dataclass
class FillResult:
    """Structured result from FillManager.place_all() or .check()."""
    status: FillStatus
    legs: List[LegFillSnapshot]
    phase_index: int                 # current phase (0-based)
    phase_total: int                 # total configured phases
    phase_pricing: str               # current phase's pricing mode
    elapsed_seconds: float
    error: Optional[str] = None
    total_fees: Optional[Price] = None  # sum of all leg fees in native denomination (None if incomplete)

    # INVARIANT: all legs in a single FillResult are always on the same exchange,
    # so all fill prices and fees share a single denomination (BTC for Deribit,
    # USD for Coincall). Cross-exchange trades are not supported.

    @property
    def all_filled(self) -> bool:
        return all(l.filled_qty >= l.qty for l in self.legs if not l.skipped)

    @property
    def has_skipped(self) -> bool:
        return any(l.skipped for l in self.legs)

    @property
    def skipped_symbols(self) -> List[str]:
        return [l.symbol for l in self.legs if l.skipped]

    def sync_to_trade_legs(self, trade_legs: List[TradeLeg]) -> None:
        """Write fill state back to TradeLeg objects (by symbol match)."""
        by_symbol = {l.symbol: l for l in self.legs}
        for leg in trade_legs:
            snap = by_symbol.get(leg.symbol)
            if snap:
                leg.filled_qty = snap.filled_qty
                leg.fill_price = snap.fill_price  # Price object — preserves denomination
                leg.order_id = snap.order_id
```

**Impact on LifecycleEngine:** The repeated `_sync_fills()` code in `_check_open_fills` and `_check_close_fills` is replaced by `result.sync_to_trade_legs(trade.open_legs)`. The `if result == "filled"` string checks become `if result.status == FillStatus.FILLED`.

### 4.5 `execution/profiles.py` — ExecutionProfile & PhaseConfig

**Purpose:** Named, configurable execution profiles.  Can be loaded from TOML, constructed inline in Python, or a mix of both (see Section 7.1 for resolution order).

```python
@dataclass
class PhaseConfig:
    """One phase in a multi-phase execution plan. Replaces ExecutionPhase."""
    pricing: str = "aggressive"           # fair, aggressive, top_of_book, mid, best_bid, best_ask
    duration_seconds: float = 30.0        # min: 10s (clamped)
    buffer_pct: float = 2.0              # for aggressive mode: % above/below fair
    fair_aggression: float = 0.0         # for fair mode: 0.0=pure fair, 1.0=full aggression
    reprice_interval: float = 30.0       # seconds between requotes within this phase (min: 10s)
    min_price_pct_of_fair: Optional[float] = None   # floor as fraction of fair (e.g. 0.83)
    min_floor_price: Optional[float] = None          # absolute price floor (native denomination)
    reprice_skip_tolerance: float = 0.001            # don't requote if new price within 0.1% of current

@dataclass
class ExecutionProfile:
    """A named execution plan — the strategy-facing configuration object.
    
    Can be created three ways:
      1. load_profiles("execution_profiles.toml")["passive_open_3phase"]
      2. ExecutionProfile(name="custom", open_phases=[...], ...)
      3. load + .with_overrides({...})
    """
    name: str
    open_phases: List[PhaseConfig]
    close_phases: List[PhaseConfig]

    # Retry & error handling (profile-level)
    max_close_attempts: int = 10          # close retry cycles before circuit-breaker
    max_requote_rounds: int = 10          # total requote iterations across all phases
    close_best_effort: bool = True        # close skips unpriceable legs
    open_atomic: bool = True              # open fails → cancel all open legs

    # Hybrid RFQ → limit fallback (optional)
    rfq_mode: str = "never"               # "never" / "hybrid" / "always"
    rfq_timeout_seconds: float = 60.0     # how long to wait for an acceptable RFQ quote
    rfq_min_improvement_pct: float = -999.0  # minimum book improvement to accept RFQ
    rfq_fallback: str = "limit"           # what to do if RFQ fails: "limit" or "abort"

    def with_overrides(self, overrides: Dict[str, Any]) -> "ExecutionProfile":
        """Return a copy with individual fields overridden.
        Keys like 'open_phase_1.duration_seconds' target phase fields.
        Top-level keys like 'max_close_attempts' target profile fields.
        """

def load_profiles(path: str = "execution_profiles.toml") -> Dict[str, ExecutionProfile]:
    """Load named profiles from TOML config file."""

def get_profile(name: str, overrides: Dict = None) -> ExecutionProfile:
    """Get a profile by name, applying optional per-field overrides."""
```

### 4.6 `execution/router.py` — ExecutionRouter (simplified)

Current `ExecutionRouter` both routes and constructs `LimitFillManager` instances. In the new design:
- Routing logic (limit vs RFQ vs hybrid vs auto-detect) stays.
- **Hybrid mode (`rfq_first=True`):** The router first dispatches to `RFQExecutor` with the profile's `rfq_timeout_seconds` and `rfq_min_improvement_pct`. If the RFQ times out or the best quote doesn't meet the improvement threshold, the router seamlessly creates a `FillManager` with the profile's `open_phases` and falls back to per-leg limit execution. The trade's `metadata["rfq_attempted"]` flag records the attempt for logging/Telegram.
- Fill manager construction uses `FillManager(order_manager, pricing_engine, phases)`.
- Mark-price logging moves to an event hook on `FillResult` instead of being embedded in the router.

### 4.7 `execution/fees.py` — Fee Helpers

**Purpose:** Helper functions for extracting and normalizing fee data from exchange responses. No separate `FillFees` type — fees use `Price` directly.

```python
# NOTE: Fees use the same `Price` type as fill prices and order prices.
# There is no separate FillFees type — `Price(amount, currency)` already
# carries the denomination.  This avoids having two overlapping types
# (FillFees vs Price) for the same concept.
#
# Where the exchange returns fee data, it is captured as:
#   fee = Price(amount=0.00032, currency=Currency.BTC)  # Deribit
#   fee = Price(amount=1.25,    currency=Currency.USD)  # Coincall
#
# Conversion to USD for display/reporting uses Price.to_usd(index_price).
```

**Where fees come from:**

| Exchange | Source | Fee fields |
|----------|--------|-----------|
| Deribit | `get_order_status()` response → `order.trades[].fee`, `order.trades[].fee_currency` | Per-trade, in BTC (or ETH for ETH options) |
| Coincall | `get_order_status()` → `data.fee` field, or trade history endpoint | Per-order, in USD |

**Integration points:**
- `OrderRecord` gains `fee: Optional[Price]` — populated when `poll_order()` sees a fill with fee data, in native denomination.
- `LegFillSnapshot` (in `FillResult`) exposes the fee for each leg as `Optional[Price]`.
- `TradeLifecycle` gains fee tracking fields in both native denomination and USD (see §5.1).
- `realized_pnl` computation in `_finalize_close()` deducts fees in native denomination, then converts the net result to USD (see §11.3).

**Design note:** This is "best effort at fill time" fee capture — not the post-close reconciliation described in `exchange-trade-log-integration.md`. That reconciliation (querying the exchange trade log for confirmed fills + fees) is a complementary, later step. The execution layer captures what it can from the order status response; the reconciler fixes up any discrepancy.

### 4.8 Files that stay largely unchanged

- **`order_manager.py`** — Already well-factored. Changes: accept `Price` objects where it currently takes `float price`, add denomination assertion before calling executor. Add `fee: Optional[Price]` to `OrderRecord` (uses `Price` — no separate fee type).
- **`rfq.py`** — Stays as-is for Coincall. The `ExchangeRFQExecutor` ABC already exists in `exchanges/base.py`. For Deribit block trades, a `DeribitRFQAdapter` would be added when needed. The hybrid routing in `execution/router.py` calls RFQ through the existing interface — no changes to `rfq.py` internals.
- **`trade_lifecycle.py`** — Keep as data-only module. Move `executable_pnl()` and `compute_fair_price()` out (they do I/O). Add typed fields for fill context and fees instead of `metadata` stashing.
- **`lifecycle_engine.py`** — Simplify fill sync code. Replace string-based status checks with `FillResult` pattern matching.

---

## 5. Data Structures

### 5.1 TradeLifecycle — New Typed Fields

Replace the `metadata` grab-bag for execution state with first-class fields:

```python
@dataclass
class TradeLifecycle:
    # ... existing fields ...

    # NEW: trade-level denomination — the source of truth for this trade cycle.
    # Set at creation from the exchange adapter. All prices, fills, fees, and PnL
    # on this trade are in this denomination. USD values are derived for display only.
    # Persisted in to_dict() so from_dict() can reconstruct Price objects.
    currency: Currency = Currency.BTC       # BTC (Deribit), USD (Coincall), ETH (future)

    # NEW: typed execution context (replaces metadata stashing)
    open_fill_context: Optional["FillManager"] = field(default=None, repr=False)
    close_fill_context: Optional["FillManager"] = field(default=None, repr=False)
    open_profile: Optional[ExecutionProfile] = None
    close_profile: Optional[ExecutionProfile] = None

    # NEW: pricing snapshots at trade events (replaces metadata mark_at_open/close)
    open_pricing_snapshot: Optional[Dict[str, PricingResult]] = None   # symbol → snapshot
    close_pricing_snapshot: Optional[Dict[str, PricingResult]] = None

    # NEW: fee tracking — stored in BOTH native denomination and USD.
    # Native is authoritative (matches exchange records); USD is for display/logging.
    open_fees: Optional[Price] = None         # fees from opening fills (native denomination)
    close_fees: Optional[Price] = None        # fees from closing fills (native denomination)
    total_fees: Optional[Price] = None        # open + close fees (native denomination)
    open_fees_usd: Optional[float] = None     # open fees converted to USD at fill time
    close_fees_usd: Optional[float] = None    # close fees converted to USD at fill time
    total_fees_usd: Optional[float] = None    # total fees in USD (for display/Telegram/logs)

    # NEW: PnL — replaces old `realized_pnl: float` (which was denomination-ambiguous).
    # Native is authoritative; USD is for display/Telegram/logs.
    realized_pnl: Optional[Price] = None      # net PnL in native denomination (was: bare float)
    realized_pnl_usd: Optional[float] = None  # net PnL converted to USD at close time

    # KEEP: metadata dict for strategy-specific data (SL thresholds, etc.)
    metadata: Dict[str, Any] = field(default_factory=dict)
```

### 5.2 TradeLeg — Fill Price as Typed Price

```python
@dataclass
class TradeLeg:
    symbol: str
    qty: float
    side: str

    order_id: Optional[str] = None
    fill_price: Optional[Price] = None    # was: Optional[float]
    filled_qty: float = 0.0
    position_id: Optional[str] = None
```

**Decision:** `fill_price` is `Optional[Price]` — the typed approach. All consumers that need a raw float call `fill_price.amount` (or `float(fill_price)` via `__float__`). This is a one-time migration cost in Phase 3 that permanently eliminates the denomination ambiguity. The `Price.__float__()` method provides backward-compatible raw access where denomination has already been verified.

**Denomination invariant:** All `fill_price` values on legs within a single `TradeLifecycle` share the same `Currency` (a trade does not span exchanges). PnL, fees, and entry/exit cost are all computed in this native denomination first, then converted to USD for display.

---

## 6. Pricing Engine — Detailed Design

### 6.1 Fair Value Computation (extracted from `_get_phased_price`)

The current 150-line fair-price computation in `LimitFillManager._get_phased_price` becomes:

```python
class PricingEngine:
    def fair_value(self, ob: OrderbookSnapshot) -> Optional[Price]:
        """
        Compute exchange-denomination fair value from an orderbook snapshot.

        Priority:
          1. Full book: mark if inside [bid, ask], else midpoint
          2. Bid only: max(mark, bid)
          3. Ask only: min(mark, ask)
          4. Mark only: mark
          5. Empty: None
        """
```

This is the same logic currently in `_get_phased_price`, but:
- It's a standalone, testable function.
- It returns `Price(amount, ob.currency)` — denomination is automatic.
- Strategies can call `pricing_engine.fair_value(ob)` directly for SL evaluation (replacing `compute_fair_price()` in `put_sell_80dte.py`).

### 6.2 Denomination Resolution

Currently handled by a large comment block and manual `mark_btc` / `mark_usd` selection in `_get_phased_price`. In the new design:

```python
# In the exchange adapter (boundary):
def get_option_orderbook(self, symbol) -> dict:
    # ... fetch from exchange ...
    # Adapter already knows denomination.
    # New: include _currency field.
    result["_currency"] = "BTC"  # or "USD" for Coincall
    return result

# In PricingEngine — reads _currency, constructs OrderbookSnapshot:
ob = OrderbookSnapshot(
    symbol=symbol,
    currency=Currency(raw_ob["_currency"]),
    best_bid=...,
    best_ask=...,
    mark=float(raw_ob.get("_mark_btc", 0)) or float(raw_ob.get("mark", 0)),
    index_price=float(raw_ob.get("_index_price", 0)),
    timestamp=time.time(),
)
```

The adapter boundary is the right place to resolve denomination. The pricing engine never needs to guess.

### 6.3 Strategy-Facing Convenience

Strategies currently call `compute_fair_price(symbol)` (a function in `put_sell_80dte.py` that fetches market data and computes fair value). This moves to:

```python
# In strategy code:
from execution.pricing import PricingEngine

pricing = PricingEngine()
ob = market_data.get_option_orderbook(symbol)
result = pricing.fair_value(OrderbookSnapshot.from_raw(ob))
# result.price.amount, result.price.currency
```

Or, for convenience, `PricingEngine` can accept raw orderbook dicts and construct the snapshot internally.

---

## 7. Execution Profiles & Configuration

### 7.1 Profile Resolution Order

An `ExecutionProfile` can come from three sources.  The first one that provides a value wins:

1. **Strategy inline** — the strategy constructs an `ExecutionProfile` in Python code. Most specific; used when the strategy needs full programmatic control (e.g. building phases dynamically based on DTE or market conditions).
2. **Slot TOML override** — the slot config file overrides individual fields of a named profile.
3. **Named profile from library** — `execution_profiles.toml` ships a catalogue of reusable standard profiles.

A strategy is **never forced** to use the TOML file.  It can:
- Reference a named profile: `open_profile = "passive_open_3phase"`
- Build its own inline: `open_profile = ExecutionProfile(phases=[...], ...)`
- Start from a named profile and override fields at runtime

```python
# Option A — named profile (simplest):
config = StrategyConfig(
    name="put_sell_80dte",
    open_profile="passive_open_3phase",
    close_profile="sl_close_3phase",
)

# Option B — inline profile (full control):
config = StrategyConfig(
    name="long_strangle_index_move",
    close_profile=ExecutionProfile(
        close_best_effort=True,
        max_close_attempts=15,        # override system default
        close_phases=[
            PhaseConfig(pricing="fair", duration_seconds=30, ...),
            PhaseConfig(pricing="aggressive", duration_seconds=180, ...),
            PhaseConfig(pricing="aggressive", duration_seconds=14400,
                        min_floor_price=0.0001, reprice_interval=60),
        ],
    ),
)

# Option C — named profile + runtime overrides:
profile = load_profile("aggressive_2phase")
profile = profile.with_overrides({
    "open_phase_1.duration_seconds": LIMIT_OPEN_FAIR_SECONDS,
    "close_phase_2.min_floor_price": 0.0001,
})
config = StrategyConfig(name="short_strangle_delta_tp", open_profile=profile, ...)
```

### 7.2 Profile-Level Parameters

These sit at the top of each profile (not inside a phase):

| Parameter | Type | Default | What it controls |
|-----------|------|---------|-----------------|
| `open_atomic` | bool | `true` | If any open leg fails, cancel all open legs |
| `close_best_effort` | bool | `true` | On close, try all legs even if some fail |
| `max_close_attempts` | int | `10` | Total close retry cycles before circuit-breaker fires (currently `MAX_CLOSE_ATTEMPTS` in `execution_router.py`) |
| `max_requote_rounds` | int | `10` | Max requote iterations across all phases combined (currently in `ExecutionParams`) |
| `rfq_mode` | str | `"never"` | `"never"` / `"hybrid"` / `"always"` — RFQ routing strategy |
| `rfq_timeout_seconds` | float | `60.0` | How long to wait for an RFQ quote before falling back |
| `rfq_min_improvement_pct` | float | `-999.0` | Minimum quote improvement vs best book to accept |

### 7.3 Per-Phase Parameters

Each `open_phase_N` / `close_phase_N` block supports:

| Parameter | Type | Default | What it controls |
|-----------|------|---------|-----------------|
| `pricing` | str | `"aggressive"` | Pricing mode: `fair`, `aggressive`, `top_of_book`, `mid`, `best_bid`, `best_ask` |
| `duration_seconds` | float | `30.0` | How long this phase runs before escalating to the next (min: 10s) |
| `reprice_interval` | float | `30.0` | Seconds between requote attempts within this phase (min: 10s) |
| `buffer_pct` | float | `2.0` | For `aggressive` mode: percent above/below fair to place the order |
| `fair_aggression` | float | `0.0` | For `fair` mode: 0.0 = pure fair, 1.0 = full aggression toward top-of-book |
| `min_price_pct_of_fair` | float | `None` | Floor: don't sell below this fraction of fair value (e.g. `0.83` = skip if price < 83% of fair) |
| `min_floor_price` | float | `None` | Absolute price floor in **exchange-native denomination** (e.g. `0.0001` BTC on Deribit, `0.01` USD on Coincall) |
| `reprice_skip_tolerance` | float | `0.001` | Don't requote if new price is within this relative distance of current price (avoids churn) |

### 7.4 System Guardrails (not configurable — hardcoded safety nets)

These protect against bugs and runaway behavior.  They are **not** exposed in profiles or TOML because they should never be tweaked for business reasons:

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| `MAX_ORDERS_PER_LIFECYCLE` | 30 | `order_manager.py` | Hard cap on total orders per trade lifecycle — prevents infinite requote loops |
| `MAX_PENDING_PER_SYMBOL` | 4 | `order_manager.py` | Max simultaneous open orders per symbol — prevents order flooding |
| `RECONCILE_EVERY_N_TICKS` | 5 | `lifecycle_engine.py` | How often to reconcile exchange state (~50s at 10s poll) |
| `GRACE_PERIOD` | 30s | `order_manager.py` | Delay before treating an unresolved order as stale for reconciliation |
| `duration_seconds` min clamp | 10s | `execution/profiles.py` | Prevents phases shorter than 10s (API rate limit safety) |
| `reprice_interval` min clamp | 10s | `execution/profiles.py` | Same — prevents excessively frequent requotes |

### 7.5 TOML Library File

New file: `execution_profiles.toml` — a catalogue of reusable standard profiles. Strategies reference these by name or ignore them entirely.

```toml
# Standard execution profile library.
# Strategies can use these by name, define their own inline, or mix both.
# Phases are numbered: open_phase_1, open_phase_2, ... (executed in order).
#
# DENOMINATION CONVENTION: All price values (min_floor_price, buffer amounts)
# are in the exchange's native denomination: BTC for Deribit options, USD for
# Coincall options. Profiles are exchange-aware — a Deribit strategy must not
# use a Coincall-denominated floor price.

# ── Passive 3-phase open (put_sell_80dte default) ──────────────────────
[profiles.passive_open_3phase]
open_atomic = true
max_requote_rounds = 10

[profiles.passive_open_3phase.open_phase_1]
pricing = "fair"
duration_seconds = 45
reprice_interval = 45

[profiles.passive_open_3phase.open_phase_2]
pricing = "fair"
fair_aggression = 0.33
duration_seconds = 45
reprice_interval = 45
min_price_pct_of_fair = 0.83

[profiles.passive_open_3phase.open_phase_3]
pricing = "top_of_book"
duration_seconds = 60
reprice_interval = 30
min_price_pct_of_fair = 0.83


# ── SL close 3-phase (put_sell_80dte default) ─────────────────────────
[profiles.sl_close_3phase]
close_best_effort = true
max_close_attempts = 10
max_requote_rounds = 10

[profiles.sl_close_3phase.close_phase_1]
pricing = "fair"
fair_aggression = 0.0
duration_seconds = 15
reprice_interval = 15

[profiles.sl_close_3phase.close_phase_2]
pricing = "fair"
fair_aggression = 0.33
duration_seconds = 15
reprice_interval = 15

[profiles.sl_close_3phase.close_phase_3]
pricing = "fair"
fair_aggression = 1.0
duration_seconds = 60
reprice_interval = 15


# ── Aggressive 2-phase (short_strangle_delta_tp default) ──────────────
[profiles.aggressive_2phase]
open_atomic = true
close_best_effort = true
max_close_attempts = 10
max_requote_rounds = 10

[profiles.aggressive_2phase.open_phase_1]
pricing = "fair"
duration_seconds = 30
reprice_interval = 30

[profiles.aggressive_2phase.open_phase_2]
pricing = "aggressive"
buffer_pct = 2.0
duration_seconds = 180
reprice_interval = 30

[profiles.aggressive_2phase.close_phase_1]
pricing = "fair"
duration_seconds = 30
reprice_interval = 30

[profiles.aggressive_2phase.close_phase_2]
pricing = "aggressive"
buffer_pct = 2.0
duration_seconds = 180
reprice_interval = 30
min_floor_price = 0.0001


# ── Single-phase fallback ─────────────────────────────────────────────
[profiles.default_single_phase]
open_atomic = true
close_best_effort = true
max_close_attempts = 10
max_requote_rounds = 10

[profiles.default_single_phase.open_phase_1]
pricing = "aggressive"
buffer_pct = 2.0
duration_seconds = 30
reprice_interval = 30

[profiles.default_single_phase.close_phase_1]
pricing = "aggressive"
buffer_pct = 2.0
duration_seconds = 30
reprice_interval = 30
```

The loader collects keys matching `open_phase_*` / `close_phase_*`, sorts by suffix number, and builds the ordered phase list.

### 7.6 Per-Slot Overrides

Slot TOML files can override profile parameters:

```toml
# slots/slot-01.toml
[strategy]
name = "put_sell_80dte"

[execution]
open_profile = "passive_open_3phase"
close_profile = "sl_close_3phase"

# Override specific phase parameters for this slot:
[execution.overrides]
"open_phase_1.duration_seconds" = 60
"close_phase_3.duration_seconds" = 90
"max_close_attempts" = 15
```

---

## 8. Price Denomination & Currency Handling

### 8.1 Current State

- Deribit: order prices in BTC, display prices in USD. Adapter converts BTC→USD at the `get_option_details` boundary but leaves orderbook in BTC.
- Coincall: everything in USD.
- `_mark_btc` and `mark` coexist in orderbook dicts. The `fair` pricing mode has a 40-line block to pick the right one.
- The plausibility guard (`price > 1.0 and index_price > 0`) is the only runtime catch.

### 8.2 Target State

1. **Exchange adapters tag orderbooks with `_currency`**: `"BTC"` for Deribit, `"USD"` for Coincall. This already exists implicitly via `_mark_btc` vs `mark` — we make it explicit.

2. **`OrderbookSnapshot` always provides `mark` in native denomination.** No more `_mark_btc` / `mark` ambiguity. The adapter resolves this. One field, one denomination.

3. **`PricingEngine.compute()` returns `Price(amount, currency)`.**

4. **`OrderManager.place_order()` validates denomination.** The exchange adapter knows its expected denomination. If `price.currency != exchange.denomination`, refuse with a clear error. This is the definitive fix for the `price_too_high` class of bugs.

5. **Display/logging always converts to USD.** `Price.to_usd(index_price)` for Telegram, logs, dashboard.

### 8.3 Future: ETH Options

When ETH options are added:
- ETH options on Deribit are denominated in ETH.
- `Currency.ETH` is already in the enum.
- `OrderbookSnapshot.currency = Currency.ETH`.
- `PricingEngine` doesn't change — it works with whatever denomination the orderbook carries.
- `Price.to_usd()` works for ETH just as it does for BTC — just needs the ETH index price.

---

## 9. Multi-Leg Execution (3- and 4-Leg Structures)

### 9.1 Current State

The system currently handles 1-leg (put sell) and 2-leg (strangle, straddle) structures. The code uses `for leg in trade.open_legs` loops, so N-leg structures are mechanically supported. However, there are specific areas that need attention for 3-leg (butterfly) and 4-leg (iron condor) structures.

### 9.2 Areas Requiring N-Leg Attention

| Area | Current Behavior | Required for N>2 |
|------|-----------------|-------------------|
| **Atomic open** | All legs or none — failure of any one leg cancels the batch and unwinds any partial fills | Works correctly for N legs. No changes needed — the `place_all(best_effort=False)` path is already N-agnostic. |
| **Best-effort close** | Skips legs that can't be priced, retries next tick | Works correctly for N legs. `skipped_symbols` tracking is already per-leg. |
| **Unwind on partial** | `_unwind_filled_legs()` takes the subset of filled legs and pushes through close cycle | Must work for any subset of N legs. Currently correct — it takes a `List[TradeLeg]` of arbitrary length. |
| **PnL calculation** | `total_entry_cost()` and `_finalize_close()` iterate all legs | Must correctly handle mixed buy/sell legs in the same structure (e.g., iron condor has 2 sells + 2 buys). **Needs review:** verify sign handling for mixed-side structures. |
| **Notional calculation** | `_calculate_notional()` in router sums mark × qty for auto-mode detection | Correct for N legs. |
| **RFQ leg assembly** | `OptionLeg` list built from `trade.open_legs` | Correct for N legs — RFQ protocol already supports multi-leg. |
| **Reconciliation** | Symbol-based matching between fill manager legs and trade close legs | Correct for N legs — uses `{ls.symbol: ls}` dict lookup, not index alignment. |

### 9.3 Design Requirements

1. **`PhaseConfig` applies uniformly to all legs.** A 4-leg iron condor in phase 1 ("fair") prices all 4 legs at fair value. This is correct — per-leg pricing overrides are not needed.

2. **Mixed-side structures:** An iron condor has 2 buy legs (wings) and 2 sell legs (body). The `PricingEngine` already dispatches `buy` vs `sell` per-leg. The `FillResult` tracks each leg independently. No change needed.

3. **Partial-fill unwind priority:** When 2 of 4 legs fill on an atomic open and the other 2 fail, the unwind must close the 2 filled legs. The current `_unwind_filled_legs()` handles this. **Add test coverage** for the N=3 and N=4 cases.

4. **PnL for mixed structures:** `total_entry_cost()` uses `sign = 1 if leg.side == "buy" else -1`. This is correct for iron condors (buy wings = debit, sell body = credit). **Add test coverage** to verify.

5. **Executable PnL for N legs:** `executable_pnl()` iterates all legs and uses bid for sell-close, ask for buy-close. Correct for N legs. If any single leg has no orderbook data, returns None (conservative). This is acceptable.

### 9.4 Testing Plan for N-Leg

New test cases to add in `tests/test_multileg_execution.py`:

- 3-leg butterfly: buy 1x C(K-d), sell 2x C(K), buy 1x C(K+d) — open, fill, PnL calc, close
- 4-leg iron condor: sell P(K1), buy P(K2), sell C(K3), buy C(K4) — open, partial fill (2 of 4), unwind
- 4-leg: best_effort close where 1 leg can't be priced — verify other 3 close, retry on next tick
- PnL sign verification for all 4 legs in iron condor (2 debit + 2 credit)

---

## 10. RFQ Integration & Hybrid Execution

### 10.1 Current RFQ State

- `rfq.py` (839 lines): fully functional Coincall RFQ executor. Handles create → poll → accept/cancel workflow.
- `ExchangeRFQExecutor` ABC in `exchanges/base.py`: defines `execute()`, `execute_phased()`, `get_orderbook_cost()`.
- `ExecutionRouter` routes to RFQ when `execution_mode == "rfq"` or when notional >= threshold (auto-detect).
- `RFQParams` on `TradeLifecycle`: timeout, min_improvement, fallback_mode.
- **Not in production:** RFQ was used historically, then disabled. No Deribit RFQ adapter exists.

### 10.2 Hybrid Mode Design

The most common use case is: "try RFQ for N minutes, and if we don't get a good quote, fall back to phased limit execution." This is configured at the `ExecutionProfile` level:

```toml
[profiles.rfq_then_limit]
rfq_first = true
rfq_timeout_seconds = 300           # try RFQ for 5 minutes
rfq_min_improvement_pct = 2.0       # must beat orderbook by 2%
rfq_fallback = "limit"              # fall back to limit phases

[[profiles.rfq_then_limit.open_phases]]
pricing = "fair"
duration_seconds = 45
# ... etc (limit phases for fallback)
```

**Router logic for hybrid mode:**

```python
def open(self, trade, profile):
    if profile.rfq_first:
        rfq_result = self._try_rfq(trade, profile)
        if rfq_result.success:
            return self._finalize_rfq_open(trade, rfq_result)
        # RFQ failed or timed out — fall through to limit
        logger.info(f"[{trade.id}] RFQ failed ({rfq_result.message}), falling back to limit")
        trade.metadata["rfq_attempted"] = True
        trade.metadata["rfq_failure_reason"] = rfq_result.message

    return self._open_limit(trade, profile.open_phases)
```

### 10.3 RFQ Adapter Architecture

The `ExchangeRFQExecutor` ABC is already exchange-agnostic. If Deribit block trades are needed later, add `exchanges/deribit/rfq.py` implementing the same ABC. The router doesn't need to change — it calls `self._rfq_executor.execute(legs, ...)` regardless of exchange.

### 10.4 RFQ for Close Orders

RFQ close is already implemented in `ExecutionRouter._close_rfq()`. The hybrid mode extends this: a profile can specify `rfq_first` for close as well (separate from open), or use plain limit close phases. Default: limit close only (RFQ close is unreliable in fast markets).

---

## 11. Fee Tracking & Reporting

### 11.1 Design: Two-Layer Fee Capture

| Layer | When | Source | Accuracy |
|-------|------|--------|----------|
| **Execution-time capture** (this refactoring) | At fill poll time | `get_order_status()` response | Good — matches exchange reality within seconds |
| **Post-close reconciliation** (future, per `exchange-trade-log-integration.md`) | Minutes after close | Exchange trade log API | Definitive — confirmed fills + fees |

This refactoring implements layer 1. Layer 2 is a separate, complementary upgrade.

### 11.2 Exchange Fee Sources

**Deribit:**
- `get_order_status()` response includes `order.trades[]` array with `fee` (BTC) and `fee_currency` per fill.
- The executor adapter's `get_order_status()` return dict can be extended to include a `_fees` field.
- Fee is per-trade (a single order may have multiple trades/fills), always in the underlying currency (BTC for BTC options, ETH for ETH options).

**Coincall:**
- Trade history endpoint returns `fee` in USD per order.
- The `get_order_status()` response may not include fees directly — may need a secondary query to the trade history endpoint.

### 11.3 Data Flow

```
FillManager._poll_fills()
  │  calls order_manager.poll_order(order_id)
  │  OrderManager reads fee from exchange response → OrderRecord.fee = Price(amount, currency)
  ▼
FillManager.check() returns FillResult
  │  FillResult.legs[i].fee = OrderRecord.fee (propagated, native denomination)
  │  FillResult.total_fees = sum of leg fees (same denomination — single exchange invariant)
  ▼
LifecycleEngine._check_open_fills()
  │  result.sync_to_trade_legs() → writes fills + fees to TradeLeg
  │  trade.open_fees = result.total_fees                          # native (e.g. Price(0.00032, BTC))
  │  trade.open_fees_usd = result.total_fees.to_usd(index_price)  # converted for display
  ▼
(same for close fills → trade.close_fees, trade.close_fees_usd)
  ▼
trade._finalize_close(index_price)
  │  # PnL computed in NATIVE denomination first (entry/exit costs are native):
  │  native_pnl = -(entry_cost + exit_cost)              # e.g. 0.0042 BTC
  │  native_fees = total_fees.amount                      # e.g. 0.00064 BTC
  │  net_native_pnl = native_pnl - native_fees            # e.g. 0.00356 BTC
  │  #
  │  # Then convert the NET result to USD for display/logging:
  │  trade.realized_pnl = Price(net_native_pnl, trade.currency)  # authoritative, native
  │  trade.realized_pnl_usd = net_native_pnl * index_price       # for Telegram/logs
  │  trade.total_fees = open_fees + close_fees            # native
  │  trade.total_fees_usd = open_fees_usd + close_fees_usd
```

**Key denomination rule:** PnL arithmetic (entry cost − exit cost − fees) is always performed in the exchange's native denomination. This avoids mixing BTC PnL with USD fees. Conversion to USD happens once, at the end, on the net result.

### 11.4 Reporting

- **Telegram notifications:** Include fee in PnL breakdown: `"PnL: +$142.50 (fees: -0.00012 BTC / -$8.34, net: +$134.16)"` — shows both native and USD fee
- **`ct.strategy` log:** `TRADE_CLOSED` event includes `fees_usd` field.
- **`ct.execution` log:** `ORDER_FILLED` event includes `fee` and `fee_currency` fields.
- **Dashboard:** Display fee-adjusted PnL.

### 11.5 Graceful Degradation

If the exchange doesn't report fees in the order status response (or there's an error fetching them):
- `OrderRecord.fee` stays `None`.
- `FillResult.total_fees` stays `None`.
- `realized_pnl` is computed without fee adjustment (same as today).
- The post-close reconciler (future layer 2) can fix this up later.

No code path should fail because fees are unavailable.

---

## 12. Logging Integration

### 12.1 Current Logging Architecture

The application uses 4 log tracks (implemented in `logging_setup.py`):

| Track | Logger | File | Format | Content |
|-------|--------|------|--------|---------|
| Catch-all | root | `logs/trading.log` | Human-readable text | All module logs |
| Health | `ct.health` | `logs/health.jsonl` | Structured JSONL | 5-min account snapshots |
| Strategy | `ct.strategy` | `logs/strategy.jsonl` | Structured JSONL | Lifecycle events |
| Execution | `ct.execution` | `logs/execution.jsonl` | Structured JSONL | Order/phase events |

The `ct.*` loggers use `JsonlFormatter` which auto-injects `ts`, `slot`, `strategy` fields. If `record.msg` is a dict, fields are merged in; otherwise wrapped as `{"msg": str(...)}`.

### 12.2 Rules for New Execution Code

All new modules in `execution/` must use the existing track loggers correctly:

1. **`execution/pricing.py`:** Uses root logger for debug/warning text. No JSONL events — pricing is a pure computation, not an event.

2. **`execution/fill_manager.py`:** Emits to `ct.execution` for:
   - `PHASE_ENTERED` — when entering a new phase (with `phase_index`, `phase_total`, `pricing`, `direction`)
   - `PHASE_ADVANCED` — when phase timeout triggers advancement
   - `ORDER_PLACED` — delegated (already emitted by `OrderManager`)
   - `ORDER_REQUOTED` — delegated (already emitted by `OrderManager`)
   - `EXEC_FAILED` — when all phases exhausted

3. **`execution/router.py`:** Emits to `ct.execution` for:
   - `RFQ_ATTEMPTED` — when hybrid mode tries RFQ
   - `RFQ_FALLBACK` — when RFQ fails and router falls back to limit

4. **`lifecycle_engine.py`:** Emits to `ct.strategy` for:
   - `TRADE_OPENED` — includes `fees_usd` if available
   - `TRADE_CLOSED` — includes `realized_pnl`, `fees_usd`, `net_pnl`
   - All existing events continue unchanged.

5. **Module logger naming convention:**
   ```python
   # In execution/fill_manager.py
   logger = logging.getLogger(__name__)                    # → "execution.fill_manager" → root → trading.log
   _execution_logger = logging.getLogger("ct.execution")   # → execution.jsonl (JSONL)
   ```

6. **Never log sensitive data** (API keys, secrets) to any track.

7. **Dict events to `ct.*` loggers must be flat dicts** — the `JsonlFormatter` merges them with `ts`/`slot`/`strategy`. Nested dicts are fine for data fields but keep event types at the top level.

### 12.3 New Event Types

| Event | Track | Trigger | Key Fields |
|-------|-------|---------|-----------|
| `RFQ_ATTEMPTED` | `ct.execution` | Hybrid mode starts RFQ | `trade_id`, `timeout_s`, `min_improvement_pct` |
| `RFQ_FALLBACK` | `ct.execution` | RFQ failed, falling back | `trade_id`, `rfq_reason`, `fallback_mode` |
| `FEE_CAPTURED` | `ct.execution` | Fee data received from exchange | `trade_id`, `order_id`, `fee`, `fee_currency` |
| `FILL_COMPLETE` | `ct.execution` | All legs in a batch filled | `trade_id`, `direction`, `legs_count`, `total_fees_usd`, `elapsed_s` |

---

## 13. State Reporting & Observability

### 13.1 FillResult → LifecycleEngine → Logger

Currently, fill status is communicated as:
1. `LimitFillManager.check()` returns a string (`"filled"`, `"requoted"`, `"failed"`, `"pending"`).
2. `LifecycleEngine._check_open_fills()` manually reads `mgr.filled_legs`, zips them to trade legs, and logs.
3. `_execution_logger` receives hand-crafted dict events.

Target:
1. `FillManager.check()` returns `FillResult` — contains all state.
2. `LifecycleEngine` calls `result.sync_to_trade_legs(trade.open_legs)` — one line.
3. `FillResult` has a `.to_log_event()` method for structured logging.
4. Strategy callbacks receive `FillResult` as an argument:

```python
def on_trade_opened(trade: TradeLifecycle, result: FillResult, account: AccountSnapshot):
    """Called when all open legs are filled."""
    for leg_snap in result.legs:
        logger.info(f"Filled {leg_snap.symbol} @ {leg_snap.fill_price}")
```

### 13.2 Execution Events (structured logging)

The `ct.execution` logger already receives structured JSONL events. New events from `FillResult`:

```json
{"event": "FILL_RESULT", "trade_id": "abc123", "status": "filled", "phase": 2, "phase_pricing": "fair", "elapsed_s": 32.1, "legs": [...]}
{"event": "FILL_RESULT", "trade_id": "abc123", "status": "requoted", "phase": 1, "next_phase": 2, "next_pricing": "aggressive"}
{"event": "FILL_RESULT", "trade_id": "abc123", "status": "failed", "reason": "all_phases_exhausted"}
```

### 13.3 LifecycleEngine → Strategy Callbacks

Currently strategies define `on_trade_opened(trade, account)` and `on_trade_closed(trade, account)`. Extend with:

```python
on_trade_opened(trade, result: FillResult, account)
on_trade_closed(trade, result: FillResult, account)
on_fill_progress(trade, result: FillResult, account)   # optional: called on "requoted" / "partial"
```

The `on_fill_progress` callback enables strategies to react to partial fills (e.g., widen the next phase, send a Telegram update, adjust SL).

### 13.4 Future: Execution Quality / Slippage Tracking

Not in scope for the initial refactoring, but the infrastructure makes it nearly free to add later. The idea: for every fill, record `slippage = fill_price - fair_value_at_decision_time`. Combined with the fee data from Section 11, this gives true execution cost per trade.

- `FillResult` already carries `fill_price` and the `PricingEngine` computes `fair_value` at decision time — the only addition is snapshotting fair value when the fill manager starts and storing the delta.
- Aggregate slippage per profile over time answers: "is `passive_open_3phase` actually saving us money vs `aggressive_2phase`?"
- This is the crypto equivalent of Transaction Cost Analysis (TCA) used by institutional desks to evaluate their execution algos.

---

## 14. Implementation Phases

### Phase 1: Extract & Structure (foundation) — ~3-4 sessions

**Goal:** Create the `execution/` package, extract pricing and data structures. No behavioral changes.

| Step | Work |
|------|------|
| 1.1 | Create `execution/` package directory with `__init__.py` |
| 1.2 | Extract `execution/currency.py`: `Currency`, `Price`, `OrderbookSnapshot` |
| 1.3 | Extract `execution/fill_result.py`: `FillStatus`, `LegFillSnapshot`, `FillResult` |
| 1.4 | Extract `execution/pricing.py`: `PricingEngine` — port all 6 pricing modes from `_get_phased_price` and `_get_aggressive_price`. Unit test each mode independently. |
| 1.5 | Extract `execution/profiles.py`: `PhaseConfig`, `ExecutionProfile`, TOML loader, RFQ hybrid fields |
| 1.6 | Extract `execution/fees.py`: fee extraction helpers — parse exchange responses into `Price` fee objects, no I/O |
| 1.7 | Create `execution_profiles.toml` with profiles matching current strategy configs for 3 target strategies |
| 1.8 | Wire `PricingEngine` into existing `LimitFillManager` — delegate price computation instead of inline. Behavioral equivalence. |
| 1.9 | Write comprehensive tests: `test_pricing_engine.py`, `test_currency.py`, `test_fees.py`, `test_execution_profiles.py` |
| 1.10 | Add `test_multileg_execution.py` scaffolding (3-leg butterfly, 4-leg iron condor data structures) |

**Deliverable:** `execution/` package exists. `PricingEngine`, currency, fees, profiles are tested. Old code delegates to new code. All ~330+ existing tests pass. No production behavior change.

**Testing after Phase 1:**

Run `python -m pytest tests/ -v`. All ~330+ existing tests must still pass (extraction must not break anything). Additionally, the new test files must pass:

- `test_pricing_engine.py`: For each of the 6 pricing modes (`fair`, `aggressive`, `top_of_book`, `mid`, `best_bid`, `best_ask`), construct an `OrderbookSnapshot` with known values and assert the returned `Price` for both `side="sell"` and `side="buy"`. Cover edge cases: empty bid side (only ask available), empty ask side, zero mark price, negative spread. Verify that `PricingEngine.fair_value(ob)` returns a sane mid-based value. ~50 cases.
- `test_currency.py`: Construct `Price(0.05, Currency.BTC)` and `Price(3200.0, Currency.USD)`. Test arithmetic (`Price + Price` same currency works, mixed currencies raise). Test `to_usd(index_price)` conversion. Test frozen immutability (assigning to `.amount` raises). ~15 cases.
- `test_fees.py`: Construct `Price(amount=0.0003, currency=Currency.BTC)` as a fee. Test `to_usd(index_price)` conversion. Test `None` propagation (no fee data → fee stays `None`). Test fee summation: two `Price` fees with same currency sum correctly. ~10 cases.
- `test_execution_profiles.py`: Load the `execution_profiles.toml` file created in step 1.7. Verify `passive_open_3phase` has 3 open phases in order. Verify `aggressive_2phase` has `open_atomic=True`. Test that loading a non-existent profile raises `ValueError`. Test override merging: override `open_phase_1.duration_seconds` and verify only that field changes. ~15 cases.

**Live test (Deribit testnet):** `tests/live/test_pricing_live.py` — Fetch a real BTC option orderbook from Deribit testnet using the existing `DeribitMarketData` adapter. Pass it to `PricingEngine.compute()` for all 6 modes. Assert every returned price is a `Price` object with `currency == Currency.BTC` and `amount > 0`. This validates that real orderbook shapes parse correctly. Mark `@pytest.mark.live`. Use `_skip_if_no_creds()` pattern from `test_deribit_integration.py`. Pick a liquid near-ATM option with >30 DTE so the orderbook is likely populated.

### Phase 2: FillManager, FillResult & Fee Capture — ~3-4 sessions

**Goal:** Replace string-based fill returns with `FillResult`. Add fee capture. N-leg aware.

| Step | Work |
|------|------|
| 2.1 | Create `execution/fill_manager.py`: new `FillManager` class that uses `PricingEngine` and returns `FillResult`. N-leg aware (variable number of legs). |
| 2.2 | Remove legacy mode from fill manager (convert any strategy using flat `ExecutionParams` to a 1-phase profile) |
| 2.3 | Create `execution/router.py`: migrate from `execution_router.py`, add RFQ hybrid routing (try RFQ first → limit fallback on timeout/rejection) |
| 2.4 | Update `order_manager.py`: capture fee from exchange order response into `OrderRecord.fee: Optional[Price]` (native denomination) |
| 2.5 | Wire fee data through `FillResult`: `LegFillSnapshot.fee` → `FillResult.total_fees` (as `Price` in native denomination) |
| 2.6 | Update `lifecycle_engine.py`: replace `_check_open_fills` / `_check_close_fills` manual sync with `result.sync_to_trade_legs()`. Replace string comparisons with `FillStatus` enum checks. Emit structured events to `ct.execution` logger. |
| 2.7 | Add `currency: Currency` field to `TradeLifecycle` (set at creation from exchange adapter). Add `open_fees`, `close_fees`, `total_fees` (as `Price`, native denomination) + `*_usd` variants. Add `realized_pnl: Optional[Price]` + `realized_pnl_usd: Optional[float]` (replaces old bare `realized_pnl: float`). Remove `metadata["_open_fill_mgr"]` / `metadata["_close_fill_mgr"]` stashing |
| 2.8 | Update `TradeLifecycle.to_dict()` / `from_dict()`: serialize `currency` field, serialize `Price` objects (as `{amount, currency}` dicts), reconstruct on load. Ensure crash recovery preserves denomination info |
| 2.9 | Preserve grace tick behavior from current `LimitFillManager` in new `FillManager`: on first phase exhaustion, return `PENDING` for one extra tick, then do a final poll before `FAILED` |
| 2.10 | Update strategy callbacks to receive `FillResult` |
| 2.11 | Delete old `LimitFillManager`, `ExecutionParams`, `ExecutionPhase` from `trade_execution.py` |
| 2.12 | Write `test_fill_manager.py`, `test_execution_router.py` (including hybrid fallback tests) |

**Deliverable:** Fill lifecycle uses typed results. Fee data captured. RFQ hybrid routing available. `trade_execution.py` shrinks to just `TradeExecutor` (~120 lines). All tests updated and passing.

**Testing after Phase 2:**

Run `python -m pytest tests/ -v`. All existing tests updated to the new types must pass. New test files:

- `test_fill_manager.py`: Mock `OrderManager` and `PricingEngine`. Test a 2-leg strangle open: instantiate `FillManager` with a 2-phase profile, call `tick()` repeatedly, verify it transitions from phase 1 → phase 2 after `duration_seconds`. Verify it calls `pricing_engine.compute()` with the correct mode per phase. Verify `FillResult.status` goes `PENDING → OPEN → FILLED` as mocked fills arrive. Test `best_effort` close: one leg fills, the other stays unfilled after all phases → result is `PARTIAL`. Test `cancel_all()` cleans up open orders. Test N-leg: 4-leg iron condor with 3 fills and 1 unfilled → verify `FillResult.legs` has correct per-leg status. Test grace tick: after all phases exhaust, first `.check()` returns `PENDING` (grace), second `.check()` does a final poll and returns `FILLED` if a late fill arrived, or `FAILED` otherwise. Test that `FillResult.legs[].fee` is `Price` with correct denomination when exchange reports fees. ~35 cases.
- `test_execution_router.py`: Test that `route(notional=500, profile=...)` returns a `LimitRoute` for small notional. Test that a profile with `rfq_mode="hybrid"` first attempts RFQ (mock `rfq.request_quote()` returning a quote), and on timeout falls back to limit. Test that `rfq_mode="always"` skips limit entirely. Test `rfq_mode="never"` (default) never calls RFQ. ~20 cases.

**Live test (Deribit testnet):** `tests/live/test_fill_live.py` — Place a real limit order on a deep-OTM BTC option via `FillManager` with a 1-phase aggressive profile. Use a price far below the ask so it rests in the book without filling. Verify `OrderManager.get_open_orders()` shows the order. Then cancel it. Verify it disappears. This validates the full place → track → cancel path against the real API. Then test an actual fill: pick the cheapest deep-OTM option (e.g. 0.0001 BTC), place an aggressive buy at the ask. Wait up to 10 seconds for fill. If filled, verify `FillResult.legs[0].fill_price` is populated and `FillResult.legs[0].fee` is non-None (Deribit always returns fee data on fills). Use a `finally` block to cancel any unfilled orders. Mark `@pytest.mark.live`.

### Phase 3: Currency Type Safety — ~1-2 sessions

**Goal:** Propagate `Price` objects through the system. Add denomination validation.

| Step | Work |
|------|------|
| 3.1 | Add `_currency` field to exchange adapter orderbook responses |
| 3.2 | `PricingEngine.compute()` returns `Price(amount, currency)` instead of `float` |
| 3.3 | `FillResult.legs[].fill_price` becomes `Optional[Price]` |
| 3.4 | `OrderManager.place_order()` accepts `Price` and validates denomination before calling executor |
| 3.5 | `TradeLeg.fill_price` becomes `Optional[Price]` (decision made in §5.2). Update all consumers to use `fill_price.amount` or `float(fill_price)` where they previously used the bare float. Ensure `total_entry_cost()` / `total_exit_cost()` operate in native denomination. |
| 3.6 | Delete the BTC plausibility guard from pricing (it's now redundant — denomination is validated at placement) |
| 3.7 | Add tests: attempt to submit a USD price to Deribit → expect rejection |

**Deliverable:** Denomination errors are caught at `place_order()` boundary. The `price_too_high` class of bugs is structurally impossible.

**Testing after Phase 3:**

Run `python -m pytest tests/ -v`. Focus on the denomination boundary:

- Update `test_pricing_engine.py`: every `PricingEngine.compute()` call now returns `Price(amount, currency)` instead of `float`. Update all assertions. Add new cases: construct an `OrderbookSnapshot` with `currency=Currency.BTC`, verify returned price has `currency=Currency.BTC`. Same for `Currency.USD`.
- New cases in `test_fill_manager.py` or `test_currency.py`: call `OrderManager.place_order(price=Price(3200.0, Currency.USD), exchange="deribit")` → must raise `DenominationError` because Deribit expects BTC. Call with `Price(0.05, Currency.BTC)` → must succeed (mock the executor). This is the key safety gate.
- Regression: run the full `test_lifecycle_engine.py` suite. The `FillResult` objects now carry `Price` instead of `float` in `fill_price` — verify PnL computation still works by checking `trade.realized_pnl` (native `Price`) and `trade.realized_pnl_usd` against expected values with known index prices.
- New: test that `trade.currency` is set correctly at creation (e.g. `Currency.BTC` for Deribit trades) and that `to_dict()` → `from_dict()` round-trips the `currency` field and all `Price` objects correctly.

**Live test (Deribit testnet):** In `tests/live/test_denomination_live.py` — Fetch a real BTC option orderbook. Pass it through `PricingEngine.compute(ob, mode="fair", side="sell")`. Take the returned `Price` and pass it to `OrderManager.place_order()`. Verify the order is accepted by Deribit (it should be — the denomination is correct). Cancel immediately. Then construct a bogus `Price(3200.0, Currency.USD)` for the same symbol and attempt `place_order()` — verify it raises `DenominationError` locally (never reaches the API). This proves the guard works against a real exchange context. Mark `@pytest.mark.live`.

### Phase 4: Profiles & Strategy Rewrites — ~2 sessions

**Goal:** Rewrite the 3 active strategies to use named profiles, `PricingEngine`, and typed `FillResult`.

| Step | Work |
|------|------|
| 4.1 | Populate `execution_profiles.toml` with all current strategy execution configs |
| 4.2 | Add profile loading to startup (`main.py` or `strategy.py build_context()`) |
| 4.3 | Rewrite `short_strangle_delta_tp.py` to use named profiles + `PricingEngine` |
| 4.4 | Rewrite `put_sell_80dte.py` to use named profiles + `PricingEngine.fair_value()` for SL |
| 4.5 | Rewrite `long_strangle_index_move.py` to use named profiles + 3-phase close via `FillManager` |
| 4.6 | Add per-slot profile overrides to slot TOML schema |
| 4.7 | Add fee summaries to Telegram notifications (open/close messages) |

**Deliverable:** All 3 active strategies use named execution profiles. Execution timing/pricing is configurable via TOML. Fee data visible in Telegram messages.

**Testing after Phase 4:**

Run `python -m pytest tests/ -v`. Strategy-level tests:

- For each of the 3 strategies (`short_strangle_delta_tp`, `put_sell_80dte`, `long_strangle_index_move`), verify the strategy's `build_execution_config()` (or equivalent) resolves to the correct named profile from `execution_profiles.toml`. Mock the profile loader and assert the returned `ExecutionProfile` has the expected number of phases, pricing modes, and flags (`open_atomic`, `close_best_effort`).
- Test per-slot override: load a slot TOML that overrides `open_phase_1.duration_seconds = 120`, verify the resolved profile reflects the override while other fields are unchanged.
- Test Telegram message formatting: mock a completed trade with `open_fees=Price(0.0003, Currency.BTC)` and `close_fees=Price(0.0002, Currency.BTC)`, render the close message, assert it contains both the native fee amounts (BTC) and USD equivalent.
- Regression: run all existing strategy tests (`test_put_sell_80dte.py`, `test_short_strangle_delta.py`, etc.) — they must pass with the new profile-based execution path.

**Live test (Deribit testnet):** `tests/live/test_strategy_live.py` — Run `put_sell_80dte` through a single open cycle on Deribit testnet. Use the real `passive_open_3phase` profile loaded from TOML. Pick a deep-OTM put (~10 delta) with >60 DTE. Execute the 3-phase open with real orderbook data and real limit orders. The test should:
1. Start phase 1 (fair pricing), place a sell limit order, wait `duration_seconds`.
2. Transition to phase 2, verify the order is repriced with `fair_aggression=0.33`.
3. Transition to phase 3 (top_of_book), verify the order is repriced at the best bid.
4. After all phases, cancel any unfilled orders in `finally`.
5. Assert that at least 3 `place_order` / `edit_order` calls were made (one per phase) and that each used a different price.
This does NOT need to result in a fill — it validates the phase machinery against the real API. If a fill happens (cheap option), verify `FillResult.status == FILLED` and fee is captured. Mark `@pytest.mark.live`.

### Phase 5: Live Testing & Cleanup — ~2 sessions

| Step | Work |
|------|------|
| 5.1 | Consolidate live test files from Phase 1-4 into `tests/live/test_execution_live.py` (pricing, fills, fees, denomination, phased execution) |
| 5.2 | Run full live test suite: `python -m pytest tests/live/ -m live -v` — all live tests from every phase must pass |
| 5.3 | Delete dead code: `_check_legacy`, legacy `ExecutionParams` flat fields, `execution_router.py` |
| 5.4 | Move `compute_fair_price()` out of strategies — strategies use `PricingEngine.fair_value()` directly |
| 5.5 | Move `executable_pnl()` out of `TradeLifecycle` (it does I/O) — make it a function that receives an orderbook snapshot |
| 5.6 | Clean up `trade.metadata` — remove keys that are now typed fields |
| 5.7 | Verify logging integration: `ct.execution` events include fill results, fees, denomination info |
| 5.8 | Update CHANGELOG, docstrings, `memories/repo/coincall-trader.md` |
| 5.9 | Full test run (fast + live), deploy to test slot for smoke test |

**Testing after Phase 5:**

Run both suites: `python -m pytest tests/ -v` (fast, ~2s) and `python -m pytest tests/live/ -m live -v` (testnet, ~60s).

- Fast suite: all ~370+ tests pass (original ~330 + new execution tests). Zero import errors — verify no code references deleted modules (`ExecutionParams`, `ExecutionPhase`, `LimitFillManager`, `execution_router`).
- Logging check: run a mock lifecycle tick in a test that captures `ct.execution` log output (use `logging` handler capture). Verify the structured JSON contains `fill_status`, `legs`, `fee_total`, `denomination` keys.
- Cleanup validation: `grep -r "ExecutionParams\|ExecutionPhase\|LimitFillManager" *.py strategies/ execution/` must return zero hits outside `archive/`.

**Final live test (Deribit testnet):** `tests/live/test_full_lifecycle_live.py` — Execute a complete open → hold → close cycle for a single cheap deep-OTM BTC put on Deribit testnet:
1. Select option: use `DeribitMarketData` to find BTC puts with >60 DTE and ask price < 0.001 BTC.
2. Open: run the `passive_open_3phase` profile. Place a sell at the ask (aggressive, to get filled). Wait up to 15s for fill.
3. Verify: `FillResult.status == FILLED`, `FillResult.legs[0].fee` is not None, `trade.open_fees` is populated.
4. Close: immediately trigger close using `sl_close_3phase` profile. Place a buy at the ask (aggressive). Wait up to 15s.
5. Verify: `trade.close_fees` is populated, `trade.total_fees_usd` > 0, `trade.realized_pnl` is a `Price` with `currency == Currency.BTC`, and `trade.realized_pnl_usd` is a finite number.
6. Teardown: if any orders remain open, cancel them. If a position remains, close at market.
This is the ultimate integration test — it exercises pricing, fill management, fee capture, denomination safety, profile loading, and lifecycle state all against the real Deribit testnet. Mark `@pytest.mark.live`. It's fine if it costs a small amount of testnet balance.

---

## 15. Testing Reference

### 15.1 Backward Compatibility

**Not a concern** — per owner's direction. Only three strategies will be ported to the new API: `short_strangle_delta_tp`, `put_sell_80dte`, `long_strangle_index_move`. Other strategies (`daily_put_sell`, `atm_straddle_index_move`, `short_straddle_strangle`, `blueprint_strangle`) are not in active production and can remain in `archive/` as reference. Old `ExecutionParams` / `ExecutionPhase` imports will be removed.

### 15.2 Test File Summary

See the **"Testing after Phase N"** blocks in Section 14 for detailed per-phase instructions (what to test, how, expected outcomes). The table below is a quick reference for file → module mapping.

| Test File | Module Under Test | Type | Phase |
|-----------|-------------------|------|-------|
| `tests/test_pricing_engine.py` | `execution/pricing.py` | unit | 1 |
| `tests/test_currency.py` | `execution/currency.py` | unit | 1 |
| `tests/test_fees.py` | `execution/fees.py` + `Price` fee usage | unit | 1 |
| `tests/test_execution_profiles.py` | `execution/profiles.py` | unit | 1 |
| `tests/test_fill_manager.py` | `execution/fill_manager.py` | unit | 2 |
| `tests/test_execution_router.py` | `execution/router.py` | unit | 2 |
| `tests/test_fill_result.py` | `execution/fill_result.py` | unit | 2 |
| `tests/test_multileg_execution.py` | multi-leg lifecycle | unit | 2 |
| `tests/test_lifecycle_engine.py` | `lifecycle_engine.py` (updated) | integration | 2-3 |
| `tests/live/test_pricing_live.py` | PricingEngine vs real orderbook | live | 1 |
| `tests/live/test_fill_live.py` | Place → track → cancel → fill | live | 2 |
| `tests/live/test_denomination_live.py` | Denomination guard end-to-end | live | 3 |
| `tests/live/test_strategy_live.py` | 3-phase open cycle, real orders | live | 4 |
| `tests/live/test_full_lifecycle_live.py` | Open → hold → close, fee capture | live | 5 |

### 15.3 Running Tests

```bash
# Fast unit + integration tests (default, ~2s):
python -m pytest tests/ -v

# All live tests on Deribit testnet (~60s):
python -m pytest tests/live/ -m live -v

# Single live test file:
python -m pytest tests/live/test_pricing_live.py -m live -v
```

### 15.4 Live Test Guidelines

1. **All live tests use `@pytest.mark.live`** — skipped in the default fast test run (`-m 'not live'`).
2. **Use module-scoped fixtures** for auth, market_data, executor, account (reuse connections). Follow the pattern in `tests/live/test_deribit_integration.py`.
3. **Place orders far off-market** by default to avoid accidental fills (except where fill is intended for testing).
4. **Always cancel orders in `finally`/teardown** — never leave orphan orders on testnet.
5. **Test on cheap deep-OTM options** to minimize testnet balance impact.
6. **Mark fill-dependent tests with `@pytest.mark.slow`** — they may need to wait for tick cycles.
7. **Credentials:** `DERIBIT_CLIENT_ID_TEST` and `DERIBIT_CLIENT_SECRET_TEST` must be in `.env`. Use `_skip_if_no_creds()` guard.

### 15.5 Deployment Strategy

1. Land each phase as a separate commit.
2. After Phase 1: run full fast test suite + live pricing test on Deribit testnet.
3. After Phase 2: run live fill test. Deploy to a test slot (slot-03 or similar) on production VPS, let it run `short_strangle_delta_tp` for 1-2 days with small qty (0.1 BTC).
4. After Phase 3: re-run live denomination test — specifically the guard rejection test.
5. After Phase 4: run live strategy test (3-phase open cycle).
6. After Phase 5: run full lifecycle live test. Full production deploy across active slots (01, 02).

---

## 16. File Inventory & Diffs

### New Files

| File | Purpose | Est. Lines |
|------|---------|-----------|
| `execution/__init__.py` | Public re-exports | ~20 |
| `execution/pricing.py` | PricingEngine — all pricing modes, fair value | ~200 |
| `execution/currency.py` | Price, Currency, OrderbookSnapshot value objects | ~80 |
| `execution/fill_manager.py` | FillManager — phased fill logic, N-leg aware | ~350 |
| `execution/fill_result.py` | FillResult, LegFillSnapshot, FillStatus | ~100 |
| `execution/profiles.py` | ExecutionProfile, PhaseConfig, TOML loader, hybrid RFQ fields | ~120 |
| `execution/router.py` | ExecutionRouter — limit / RFQ / hybrid routing | ~250 |
| `execution/fees.py` | Fee extraction helpers, exchange response parsing | ~40 |
| `execution_profiles.toml` | Default execution profiles for 3 active strategies | ~100 |
| `tests/test_pricing_engine.py` | PricingEngine unit tests | ~200 |
| `tests/test_fill_manager.py` | FillManager unit tests (phase transitions, requote, N-leg) | ~150 |
| `tests/test_fill_result.py` | FillResult unit tests (fee aggregation, multi-leg sync) | ~60 |
| `tests/test_execution_profiles.py` | Profile loading/override/hybrid RFQ tests | ~60 |
| `tests/test_currency.py` | Price/Currency denomination + arithmetic tests | ~60 |
| `tests/test_fees.py` | Fee extraction from exchange responses, Price fee usage, USD conversion | ~40 |
| `tests/test_execution_router.py` | Router routing logic: limit, RFQ, hybrid fallback | ~80 |
| `tests/test_multileg_execution.py` | 3-leg butterfly, 4-leg iron condor lifecycle | ~80 |
| `tests/live/test_pricing_live.py` | Live: PricingEngine vs real Deribit orderbook | ~40 |
| `tests/live/test_fill_live.py` | Live: place → track → cancel → fill | ~60 |
| `tests/live/test_denomination_live.py` | Live: denomination guard end-to-end | ~40 |
| `tests/live/test_strategy_live.py` | Live: 3-phase open cycle with real orders | ~80 |
| `tests/live/test_full_lifecycle_live.py` | Live: open → hold → close, fee capture | ~100 |

### Modified Files

| File | Changes |
|------|---------|
| `trade_execution.py` | Shrinks from 1040 → ~120 lines (only `TradeExecutor` thin wrapper remains) |
| `order_manager.py` | Accept `Price` in `place_order()`, add denomination assertion, capture fee from response |
| `lifecycle_engine.py` | Use `FillResult.sync_to_trade_legs()`, enum status checks, emit to `ct.execution` logger |
| `trade_lifecycle.py` | Add `currency: Currency` field. Add typed fields (`open_fees`, `close_fees`, `total_fees` as `Price`, plus `*_usd` variants). Replace bare `realized_pnl: float` with `realized_pnl: Optional[Price]` + `realized_pnl_usd: Optional[float]`. Update `to_dict()` / `from_dict()` for `Price` serialization. Remove I/O methods |
| `execution_router.py` | Deleted (moved to `execution/router.py`) |
| `rfq.py` | Add `RfqResult.fee`, integrate with hybrid fallback in `execution/router.py` |
| `strategy.py` | Add profile resolution in `StrategyConfig` / `StrategyRunner` |
| `strategies/short_strangle_delta_tp.py` | Use named profiles, PricingEngine, typed FillResult |
| `strategies/put_sell_80dte.py` | Use named profiles, PricingEngine for SL fair value |
| `strategies/long_strangle_index_move.py` | Use named profiles, 3-phase close via FillManager |
| `exchanges/deribit/market_data.py` | Add `_currency: "BTC"` to orderbook response |
| `exchanges/coincall/market_data.py` | Add `_currency: "USD"` to orderbook response |
| `logging_setup.py` | No changes needed — `ct.execution` logger already exists |

### Deleted Files

| File | Reason |
|------|--------|
| `execution_router.py` | Moved to `execution/router.py` |

### Out of Scope (archive, no changes)

These strategies are NOT in active production and will NOT be ported:
`daily_put_sell.py`, `atm_straddle_index_move.py`, `short_straddle_strangle.py`, `blueprint_strangle.py`.
They remain in `strategies/` or `archive/` as reference.

### Net Impact

- **Before:** 5,566 lines across 5 files (tangled concerns)
- **After (estimated):** ~1,180 lines in `execution/` package + ~120 remaining in `trade_execution.py` + simplified `lifecycle_engine.py` and `trade_lifecycle.py` ≈ similar total lines, but each module has a single clear responsibility.
- ~1,000 lines of new test code (unit + live) covering pricing, fills, fees, multi-leg, denomination safety, and execution profiles.
- The pricing logic that was 300+ lines buried inside `LimitFillManager` becomes a standalone 200-line module with independent tests.
- The denomination error class is structurally eliminated.
- Fee data captured at fill time and aggregated on close — visible in strategy.jsonl and Telegram notifications.
- RFQ hybrid mode enables block-trade-first execution with automatic limit fallback.
- Execution configuration becomes a TOML file instead of scattered strategy code.
