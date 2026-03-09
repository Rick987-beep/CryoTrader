# CoincallTrader — Module Reference

**Last Updated:** March 9, 2026

Internal documentation for the CoincallTrader application modules.
For Coincall exchange API endpoints, see [API_REFERENCE.md](API_REFERENCE.md).

---

## Strategy Framework

See [strategy.py](../strategy.py) for the implementation.

### Quick Start
```python
from strategy import build_context, StrategyConfig, StrategyRunner
from strategy import time_window, weekday_filter, min_available_margin_pct
from option_selection import LegSpec
from trade_lifecycle import profit_target, max_loss, max_hold_hours

ctx = build_context()

config = StrategyConfig(
    name="short_strangle_daily",
    legs=[
        LegSpec("C", side=2, qty=0.1,
                strike_criteria={"type": "delta", "value": 0.25},
                expiry_criteria={"symbol": "28MAR26"}),
        LegSpec("P", side=2, qty=0.1,
                strike_criteria={"type": "delta", "value": -0.25},
                expiry_criteria={"symbol": "28MAR26"}),
    ],
    entry_conditions=[
        time_window(8, 20),
        weekday_filter(["mon", "tue", "wed", "thu"]),
        min_available_margin_pct(50),
    ],
    exit_conditions=[
        profit_target(50),
        max_loss(100),
        max_hold_hours(24),
    ],
    max_concurrent_trades=1,
    cooldown_seconds=3600,
    check_interval_seconds=60,
)

runner = StrategyRunner(config, ctx)
ctx.position_monitor.on_update(runner.tick)
ctx.position_monitor.start()
```

### Quick Start — Daily 0DTE Straddle (using structure templates)
```python
from strategy import build_context, StrategyConfig, StrategyRunner
from strategy import time_window, min_available_margin_pct
from option_selection import straddle
from trade_lifecycle import profit_target, time_exit

ctx = build_context()

config = StrategyConfig(
    name="daily_0dte_straddle",
    legs=straddle(qty=0.1, dte=0, side=1),       # Buy ATM call + put, 0DTE
    entry_conditions=[
        time_window(9, 10),                        # Open 09:00-09:59 UTC
        min_available_margin_pct(30),
    ],
    exit_conditions=[
        profit_target(50),                         # Close at +50% of entry cost
        time_exit(19, 0),                          # Hard close at 19:00 UTC
    ],
    max_concurrent_trades=1,
    max_trades_per_day=1,                          # One trade per calendar day
    check_interval_seconds=30,
)

runner = StrategyRunner(config, ctx)
ctx.position_monitor.on_update(runner.tick)
ctx.position_monitor.start()
```

### Key Classes
| Class | Purpose |
|-------|---------|
| `TradingContext` | DI container: auth, market_data, executor, rfq_executor, account_manager, position_monitor, lifecycle_manager, persistence (optional) |
| `StrategyConfig` | Declarative definition: name, legs, entry/exit conditions, execution_mode, max_concurrent, max_trades_per_day, cooldown, execution_params, rfq_params, on_trade_opened, on_trade_closed |
| `StrategyRunner` | Tick-driven executor: checks entries, resolves legs, creates trades, delegates to LifecycleEngine. Exposes `stats` property. |

### Entry Condition Factories
| Factory | Signature | Description |
|---------|-----------|-------------|
| `time_window(start, end)` | `int, int → EntryCondition` | UTC hour window (e.g., 8–20) |
| `utc_time_window(start, end)` | `time, time → EntryCondition` | UTC time window with `datetime.time` precision |
| `weekday_filter(days)` | `list[str] → EntryCondition` | Weekday filter (e.g., `["mon","tue","wed","thu"]`) |
| `min_available_margin_pct(pct)` | `float → EntryCondition` | Minimum available margin as % of equity |
| `min_equity(usd)` | `float → EntryCondition` | Minimum account equity in USD |
| `max_account_delta(limit)` | `float → EntryCondition` | Block if account delta exceeds threshold |
| `max_margin_utilization(pct)` | `float → EntryCondition` | IM/equity ceiling |
| `no_existing_position_in(symbols)` | `list[str] → EntryCondition` | Block if already positioned in given symbols |

### Structure Templates
| Helper | Signature | Description |
|--------|-----------|-------------|
| `straddle(qty, dte, side, underlying)` | `→ list[LegSpec]` | ATM call + ATM put (same strike). `dte=0` for 0DTE, `side=1` buy / `side=2` sell |
| `strangle(qty, call_delta, put_delta, dte, side, underlying)` | `→ list[LegSpec]` | OTM call + OTM put by delta targets. Default: 0.25 / -0.25, sell |

### DTE-Based Expiry Selection
In addition to `{"symbol": "28MAR26"}` and `{"minExp": N, "maxExp": N}`, LegSpec now supports:
```python
expiry_criteria={"dte": 0}           # 0DTE — today's expiry
expiry_criteria={"dte": 1}           # Tomorrow's expiry
expiry_criteria={"dte": 3, "dte_min": 0, "dte_max": 7}  # 0-7 day range, prefer 3
```

### LegSpec Dataclass
| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `option_type` | `str` | required | `"C"` or `"P"` |
| `side` | `int` | required | 1=BUY, 2=SELL |
| `qty` | `float` | required | Contract quantity |
| `strike_criteria` | `dict` | required | Resolution method: `{"type": "delta", "value": 0.25}`, `{"type": "closestStrike"}`, `{"type": "spotdistance%", "value": 10}` |
| `expiry_criteria` | `dict` | required | Filter: `{"symbol": "28MAR26"}` |
| `underlying` | `str` | `"BTC"` | Underlying asset |

### find_option() — Compound Selection

For strategies needing multiple simultaneous constraints, `find_option()` replaces ad-hoc filtering chains with a single declarative call.

```python
from option_selection import find_option

# OTM put, 6-13 days, delta between -0.45 and -0.15, at least 0.5% below ATM
option = find_option(
    option_type="P",
    expiry={"min_days": 6, "max_days": 13, "target": "near"},
    strike={"below_atm": True, "min_distance_pct": 0.5},
    delta={"min": -0.45, "max": -0.15},
    rank_by="delta_mid",
)

# OTM call, 1-3 weeks, 2%+ above ATM, delta target 0.30
option = find_option(
    option_type="C",
    expiry={"min_days": 7, "max_days": 21, "target": "mid"},
    strike={"above_atm": True, "min_distance_pct": 2.0},
    delta={"target": 0.30},
    rank_by="delta_target",
)
```

#### Parameters
| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `underlying` | `str` | `"BTC"` | Underlying symbol |
| `option_type` | `str` | `"P"` | `"C"` or `"P"` |
| `expiry` | `dict` | `{}` | `min_days`, `max_days` (int), `target` (`"near"`/`"far"`/`"mid"`) |
| `strike` | `dict` | `{}` | `below_atm`/`above_atm` (bool), `min_strike`/`max_strike` (float), `min_distance_pct`/`max_distance_pct` (float, % from ATM), `min_otm_pct`/`max_otm_pct` (float, directional OTM %) |
| `delta` | `dict` | `{}` | `min`/`max` (float, strict bounds), `target` (float, used by `delta_target` ranking) |
| `rank_by` | `str` | `"delta_mid"` | `"delta_mid"`, `"delta_target"`, `"strike_atm"`, `"strike_otm"`, `"strike_itm"` |

#### Return Value
Enriched option dict or `None`:
| Key | Type | Description |
|-----|------|-------------|
| `symbolName` | `str` | e.g. `"BTCUSD-27FEB26-64000-P"` |
| `strike` | `float` | Strike price |
| `delta` | `float` | Option delta at selection time |
| `days_to_expiry` | `float` | Days until expiration |
| `distance_pct` | `float` | Absolute % distance from ATM |
| `index_price` | `float` | Index price at selection time |
| `expirationTimestamp` | `int` | Expiry timestamp (ms) |
| ... | | All original API fields preserved |

#### Filter Pipeline
1. **Option type** — keep only C or P
2. **Expiry window** — filter to min/max days, collapse to single expiry date
3. **Strike filters** — ATM direction, absolute bounds, distance %, OTM %
4. **Delta enrichment** — fetch deltas for up to 10 candidates (nearest ATM first)
5. **Delta filter** — keep options within delta range
6. **Ranking** — pick single winner from survivors

### StrategyRunner Lifecycle
1. `tick(snapshot)` is called on each PositionMonitor update
2. `_check_opened_trades()` fires `on_trade_opened` for newly opened trades
3. `_check_closed_trades()` fires `on_trade_closed` for newly finished trades
4. Entry conditions checked — all must return `True`
5. `resolve_legs()` converts `LegSpec` list to concrete `TradeLeg` list
6. `LifecycleEngine.create()` creates trade with exit conditions
7. `LifecycleEngine.open()` begins execution
8. Subsequent ticks advance lifecycle (fill checks, exit evaluations)
9. `runner.stop()` for graceful shutdown
9. `runner.stats` for win/loss/hold-time aggregates

---

## Trade Lifecycle (Data Layer)

See [trade_lifecycle.py](../trade_lifecycle.py) — pure data module containing dataclasses, enums, and PnL helpers. No state-machine logic.

### Module Split (v1.0.0)

The original monolithic `trade_lifecycle.py` was split into three focused modules:

| Module | Responsibility | Lines |
|--------|---------------|-------|
| `trade_lifecycle.py` | Data: `TradeState`, `TradeLeg`, `TradeLifecycle`, `RFQParams`, `ExitCondition`, PnL helpers | ~450 |
| `lifecycle_engine.py` | State machine: `LifecycleEngine` (ticks, state transitions, creates router + order manager) | ~500 |
| `execution_router.py` | Routing: `ExecutionRouter` (dispatches open/close to limit/rfq/smart executor) | ~400 |

**Import path:** `from lifecycle_engine import LifecycleEngine`. The backward-compat `LifecycleManager` alias has been removed.

### Key Classes (trade_lifecycle.py)
| Class | Purpose |
|-------|---------|
| `TradeState` | Enum: PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED \| FAILED |
| `TradeLeg` | Single leg: symbol, qty, side, order_id, fill_price, filled_qty |
| `TradeLifecycle` | Groups legs with exit conditions; computes PnL, Greeks (pro-rated by our qty share). Optional `execution_params` and `rfq_params` typed fields. Serializable via `to_dict()/from_dict()`. |
| `RFQParams` | Typed RFQ config: `timeout_seconds`, `min_improvement_pct`, `fallback_mode` |
| `ExitCondition` | Named tuple: `(name, check_fn)` — callable `(AccountSnapshot, TradeLifecycle) → bool` |

### Key Classes (lifecycle_engine.py)
| Class | Purpose |
|-------|---------|
| `LifecycleEngine` | State machine: `create()`, `open()`, `close()`, `tick()`, `force_close()`, `kill_all()`, `cancel()`, `restore_trade()`. Owns `ExecutionRouter` and `OrderManager`. Exposes `order_manager` property. |

### Key Classes (execution_router.py)
| Class | Purpose |
|-------|---------|
| `ExecutionRouter` | Routes open/close to correct executor (limit or rfq). Mode auto-detection by notional: single-leg → limit, multi-leg ≥$50k → rfq, else limit fallback. Close circuit breaker (10 attempts → FAILED). All close orders enforce `reduce_only=True`. |

### Quick Start
```python
from strategy import build_context, StrategyConfig, StrategyRunner
from trade_lifecycle import profit_target, max_loss, max_hold_hours

# build_context() creates LifecycleEngine (with OrderManager + ExecutionRouter) internally
ctx = build_context()

# Strategies interact via StrategyConfig — never touch LifecycleEngine directly
config = StrategyConfig(
    name="example",
    legs=strangle(qty=0.01, side=1),
    exit_conditions=[profit_target(50), max_hold_hours(4)],
    execution_mode="limit",
)

runner = StrategyRunner(config, ctx)
ctx.position_monitor.on_update(runner.tick)
```

### Exit Condition Factories
| Factory | Signature | Description |
|---------|-----------|-------------|
| `profit_target(pct)` | `float → Callable` | Close when structure PnL ≥ pct of entry cost |
| `max_loss(pct)` | `float → Callable` | Close when structure loss ≥ pct of entry cost |
| `max_hold_hours(hours)` | `float → Callable` | Close after N hours |
| `time_exit(hour, minute)` | `int, int → Callable` | Close at or after a specific UTC wall-clock time (e.g., `time_exit(19, 0)`) |
| `utc_datetime_exit(dt)` | `datetime → Callable` | Close at or after a specific UTC datetime |
| `account_delta_limit(thr)` | `float → Callable` | Close when account delta exceeds threshold |
| `structure_delta_limit(thr)` | `float → Callable` | Close when structure delta exceeds threshold |
| `leg_greek_limit(idx, greek, op, val)` | `... → Callable` | Close when a specific leg's Greek crosses a limit |

### Position Scaling
The lifecycle tracks our filled quantity vs. the exchange's total position quantity:
- `_our_share(leg, pos)` = `our_filled_qty / exchange_total_qty` (clamped to [0, 1])
- Applied to `structure_pnl()`, `structure_delta()`, `structure_greeks()`
- Prevents contamination when the account has positions from other sources

---

## Order Manager

See [order_manager.py](../order_manager.py) — central order ledger preventing duplicate and runaway orders.

### Purpose
Every order placement and cancellation goes through `OrderManager`. It wraps `TradeExecutor` and adds:
- **Idempotent placement** — dedup key `(lifecycle_id, leg_index, purpose)` prevents duplicate orders
- **Supersession chains** — `requote_order()` atomically cancels old + places new + links them
- **Hard caps** — 30 orders per lifecycle, 4 pending per symbol
- **Safety enforcement** — close/unwind orders always force `reduce_only=True`
- **JSONL audit** — every state change appended to `logs/order_audit.jsonl`
- **JSON snapshots** — `logs/active_orders.json` for crash recovery
- **Exchange reconciliation** — `reconcile()` detects orphans and stale entries. Skips PENDING orders and orders placed within the last 30 seconds (grace period to avoid false positives on newly placed orders).

### Key Classes
| Class | Purpose |
|-------|---------|
| `OrderRecord` | Dataclass: order_id, lifecycle_id, leg_index, purpose, symbol, side, qty, price, status, filled_qty, supersedes/superseded_by |
| `OrderPurpose` | Enum: `OPEN_LEG`, `CLOSE_LEG`, `UNWIND` |
| `OrderStatus` | Enum: `PENDING`, `PLACED`, `PARTIAL`, `FILLED`, `CANCELLED`, `FAILED` |
| `OrderManager` | Central ledger: `place_order()`, `cancel_order()`, `cancel_all()`, `requote_order()`, `poll_all()`, `reconcile()`, `persist_snapshot()`, `load_snapshot()` |

### Quick Start
```python
from order_manager import OrderManager, OrderPurpose

# OrderManager wraps an executor (created automatically by LifecycleEngine)
om = OrderManager(executor)

# Place an order (idempotent — returns existing if already placed)
record = om.place_order(
    lifecycle_id="trade-123",
    leg_index=0,
    purpose=OrderPurpose.OPEN_LEG,
    symbol="BTCUSD-28MAR26-100000-C",
    side=1, qty=0.1, price=500.0,
)

# Requote (atomic cancel + replace + chain)
new_record = om.requote_order(record.order_id, new_price=510.0)

# Poll all live orders from exchange
om.poll_all()

# Reconcile against exchange state
warnings = om.reconcile(exchange_open_orders)

# Persist for crash recovery
om.persist_snapshot()
```

### Integration Points
- `LimitFillManager` routes through `OrderManager` when present (backward compatible — works without it)
- `LifecycleEngine` creates and owns the `OrderManager` instance
- `LifecycleEngine.tick()` checks `has_live_orders()` before allowing close (PENDING_CLOSE guard)
- `position_closer.py` calls `order_manager.cancel_all()` after `kill_all()`
- `main.py` crash recovery: `load_snapshot()` → `poll_all()` → `reconcile()`

### RFQParams Dataclass

Typed container for RFQ execution parameters, replacing loose `metadata` keys:

```python
from trade_lifecycle import RFQParams

rfq_params = RFQParams(
    timeout_seconds=300,        # Wait up to 5 minutes for quotes
    min_improvement_pct=2.0,    # Require 2% improvement vs orderbook
    fallback_mode="limit",      # Fall back to limit orders if RFQ fails
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout_seconds` | `float` | `60.0` | How long to wait for RFQ quotes |
| `min_improvement_pct` | `float` | `-999.0` | Minimum improvement vs orderbook (-999 = accept anything) |
| `fallback_mode` | `str\|None` | `None` | What to do if RFQ fails (e.g., `"limit"`) |

---

## Trade Execution — Configurable Timing

See [trade_execution.py](../trade_execution.py) for the implementation.

### ExecutionPhase Dataclass

Declares a pricing phase for the `LimitFillManager`. Multiple phases can be sequenced to start conservatively and escalate:

```python
from trade_execution import ExecutionPhase, ExecutionParams

params = ExecutionParams(phases=[
    ExecutionPhase(pricing="mark",       duration_seconds=300, reprice_interval=30),
    ExecutionPhase(pricing="mid",        duration_seconds=120, reprice_interval=20),
    ExecutionPhase(pricing="aggressive", duration_seconds=60,  buffer_pct=3.0),
])
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pricing` | `str` | `"aggressive"` | Pricing mode: `"aggressive"`, `"mid"`, `"top_of_book"`, `"mark"` |
| `duration_seconds` | `float` | `30.0` | How long this phase lasts (min 10s, auto-clamped) |
| `buffer_pct` | `float` | `2.0` | Buffer % for aggressive pricing |
| `reprice_interval` | `float` | `30.0` | Seconds between reprices in this phase (min 10s) |

### Pricing Modes

| Mode | Buy Price | Sell Price | Use Case |
|------|-----------|------------|----------|
| `"mark"` | Mark price | Mark price | Most patient; wait for fair value |
| `"mid"` | (bid+ask)/2 | (bid+ask)/2 | Balanced |
| `"top_of_book"` | Best ask | Best bid | Match best available |
| `"aggressive"` | Best ask × (1 + buffer%) | Best bid × (1 - buffer%) | Cross the spread; fastest fill |

### Phased vs Legacy Mode

- **Legacy** (`phases=None`): Single aggressive mode with `fill_timeout_seconds` and `max_requote_rounds`. This is the default.
- **Phased** (`phases=[...]`): LimitFillManager walks through each phase in sequence. When a phase’s `duration_seconds` expires, it advances to the next phase. After the last phase, the fill manager signals expiry.

### ExecutionParams Dataclass

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `fill_timeout_seconds` | `float` | `30.0` | Fill timeout per round (legacy mode) |
| `aggressive_buffer_pct` | `float` | `2.0` | Aggressive buffer % (legacy mode) |
| `max_requote_rounds` | `int` | `10` | Max requote rounds (legacy mode) |
| `phases` | `list[ExecutionPhase]\|None` | `None` | Phased execution config (overrides legacy) |

### Close-Order Safety (v0.9.3)

**`reduce_only` flag:** All close orders are placed with `reduceOnly=1` on the exchange API. This is an exchange-level guarantee that a close order can never exceed the open position size — it physically cannot create a reverse position regardless of retry logic bugs.

**Price pre-validation:** `place_all()` validates prices for ALL legs before placing ANY orders. If one leg has no orderbook liquidity, no orders are placed at all. This prevents the partial-placement race condition where one leg fills while the other's cancel arrives too late.

**Circuit breaker:** `_close_limit()` tracks close attempts per trade. After 10 failed attempts, the trade transitions to `FAILED` with a critical log. This prevents infinite retry loops when market conditions make closing impossible.

**Requote skip-if-unchanged:** `_requote_unfilled()` skips requoting when the new price is within $0.01 of the existing order price (tolerance check), avoiding wasteful cancel+replace cycles on stable markets. Logged at INFO level when skipped.

```python
# Close orders are always reduce_only — set automatically by _close_limit()
mgr.place_all(trade.close_legs, reduce_only=True)

# place_order() passes it to the exchange API
payload['reduceOnly'] = 1  # exchange rejects if order > open position
```

### Complete Strategy Example

```python
from strategy import StrategyConfig
from option_selection import strangle
from trade_execution import ExecutionParams, ExecutionPhase
from trade_lifecycle import RFQParams, profit_target, max_hold_hours

config = StrategyConfig(
    name="patient_strangle",
    legs=strangle(qty=0.01, call_delta=0.15, put_delta=-0.15, dte="next", side=1),
    execution_mode="limit",
    execution_params=ExecutionParams(phases=[
        ExecutionPhase(pricing="mark",       duration_seconds=300, reprice_interval=30),
        ExecutionPhase(pricing="mid",        duration_seconds=120, reprice_interval=20),
        ExecutionPhase(pricing="aggressive", duration_seconds=60,  buffer_pct=2.0),
    ]),
    rfq_params=RFQParams(timeout_seconds=120, min_improvement_pct=1.0),
    exit_conditions=[profit_target(50), max_hold_hours(4)],
    max_trades_per_day=1,
)
```

---

## Position Monitoring

See [account_manager.py](../account_manager.py) for position monitoring implementation.

### Quick Start
```python
from account_manager import PositionMonitor

monitor = PositionMonitor(poll_interval=5)

# Register a callback (called on every poll)
monitor.on_update(lambda snapshot: print(snapshot.summary_str()))

monitor.start()
# ... monitor runs in background thread ...
snap = monitor.snapshot()  # Thread-safe current snapshot
monitor.stop()
```

### Key Classes
| Class | Purpose |
|-------|---------|
| `PositionSnapshot` | Frozen dataclass: symbol, qty, side, avgPrice, markPrice, delta, gamma, vega, theta, unrealized_pnl, roi |
| `AccountSnapshot` | Frozen dataclass: equity, available_margin, im/mm amounts, positions list, aggregated Greeks, `get_position()`, `summary_str()` |
| `PositionMonitor` | Background polling thread with callbacks, `snapshot()`, `start()`, `stop()`, `on_update()` |

### Position Fields
Uses `upnlByMarkPrice` and `roiByMarkPrice` for accurate options PnL (not `upnl`/`roi` which use last trade price). Also captures `lastPrice`, `indexPrice`, `value` fields.

---

## RFQ Executor

See [rfq.py](../rfq.py) for the implementation.
For the underlying exchange endpoints, see [API_REFERENCE.md](API_REFERENCE.md#rfq-block-trades).

### Quick Start
```python
from rfq import RFQExecutor, OptionLeg

# Define a strangle structure
legs = [
    OptionLeg('BTCUSD-28FEB26-100000-C', 'BUY', 1.0),
    OptionLeg('BTCUSD-28FEB26-90000-P', 'BUY', 1.0),
]

# Open a long position (BUY the strangle)
rfq = RFQExecutor()
result = rfq.execute(legs, action='buy', timeout_seconds=60)

if result.success:
    print(f"Bought for ${result.total_cost:.2f}")

# Later: Close the position (SELL the strangle)
result = rfq.execute(legs, action='sell', timeout_seconds=60)
if result.success:
    print(f"Sold for ${abs(result.total_cost):.2f}")
```

### Key Concepts

**Direction Logic:**
- Legs specify their own `side` ("BUY" or "SELL") — simple structures have all BUY, but spreads/condors can have mixed sides
- Market makers respond with two-way quotes (both BUY and SELL sides)
- The quote's `side` field indicates the **market maker's** action, not ours:
  - MM `SELL` = they sell to us = **WE BUY** = positive cost (we pay)
  - MM `BUY` = they buy from us = **WE SELL** = negative cost (we receive)
- Use the `action` parameter to filter: `'buy'` or `'sell'`

**Orderbook Comparison (v0.5.1):**
- `get_orderbook_cost(legs, action)` correctly selects ask/bid based on whether we're effectively buying or selling each leg
- For each leg: `effectively_buying = (leg.side == "BUY") == (action == "buy")`
  - If effectively buying → use orderbook ASK (what we'd pay)
  - If effectively selling → use orderbook BID (what we'd receive)
- `calculate_improvement()` uses unified formula: `(orderbook - quote) / |orderbook| * 100`
- Positive = RFQ is cheaper than orderbook (good); negative = RFQ is more expensive

**Quote Selection (Best-Quote Logic):**
- All valid quotes are sorted by price (cheapest first for buys, highest first for sells)
- Every quote is logged with rank, cost, and improvement vs. orderbook mid-price
- `min_improvement_pct` parameter gates acceptance: set to 0 to require beating the book, or -999 to accept anything
- On accept failure (quote expired), automatically falls through to next-best quote
- Quotes with <1s remaining until expiry are skipped

**Timing (observed in production):**
- Quotes typically arrive within 3-5 seconds
- Default poll interval: 3 seconds
- Recommended timeout: 60 seconds

### Key Classes
| Class | Purpose |
|-------|---------|
| `OptionLeg` | Dataclass for leg definition (instrument, side, qty) |
| `RFQState` | Enum: PENDING, ACTIVE, FILLED, CANCELLED, EXPIRED |
| `RFQQuote` | Quote received from market maker (with `is_we_buy`, `is_we_sell` properties) |
| `RFQResult` | Execution result with all details |
| `RFQExecutor` | Main executor class |

### Key Methods (RFQExecutor)
| Method | Purpose |
|--------|---------|
| `execute(legs, action, timeout_seconds, min_improvement_pct)` | Execute RFQ with best-quote selection |
| `get_orderbook_cost(legs, action)` | Calculate equivalent orderbook cost for comparison |
| `calculate_improvement(quote_cost, orderbook_cost)` | Compute improvement percentage |

---

## Smart Orderbook Execution

See [multileg_orderbook.py](../multileg_orderbook.py) for the implementation.

### Quick Start
```python
from multileg_orderbook import SmartOrderbookExecutor, SmartExecConfig
from trade_lifecycle import TradeLeg

# Configure execution parameters
smart_config = SmartExecConfig(
    chunk_count=2,                  # Split into 2 chunks
    time_per_chunk=20.0,            # 20 seconds per chunk
    quoting_strategy="mid",         # Quote at mid-price
    reprice_interval=10.0,          # Reprice every 10s
    reprice_price_threshold=0.1,    # Reprice if price moves >0.1
    aggressive_attempts=10,         # Max fallback attempts
    aggressive_wait_seconds=5.0     # Wait 5s per attempt
)

# Define multi-leg structure
legs = [
    TradeLeg(symbol="BTCUSD-27FEB26-80000-C", qty=0.2, side=1),  # BUY
    TradeLeg(symbol="BTCUSD-27FEB26-82000-C", qty=0.4, side=2),  # SELL
    TradeLeg(symbol="BTCUSD-27FEB26-84000-C", qty=0.2, side=1),  # BUY
]

# Execute with smart chunking
executor = SmartOrderbookExecutor()
result = executor.execute_smart_multi_leg(legs, smart_config)

if result.success:
    print(f"Executed {result.chunks_completed}/{result.chunks_total} chunks")
    print(f"Total time: {result.execution_time:.1f}s")
    print(f"Fallbacks: {result.fallback_count}")
```

### Algorithm Overview

**Phase 1: Chunk Calculation**
- Splits total order into N proportional chunks
- Each chunk maintains leg quantity ratios
- Example: 0.4 contracts → 2 chunks of 0.2 each

**Phase 2: Per-Chunk Execution**
1. **Quoting Phase** (config.time_per_chunk seconds)
   - Place limit orders for all legs at calculated prices
   - Monitor fills continuously (0.5s polling)
   - Reprice when market moves beyond threshold
   - Stop quoting individual legs as they fill
2. **Aggressive Fallback** (if not fully filled)
   - Place limit orders crossing the spread
   - Multiple retry attempts with configurable waits
   - Exit early when all legs filled

**Phase 3: Early Termination**
- Between chunks, check if target already reached
- Stop processing remaining chunks if filled

### Key Concepts

**Position-Aware Tracking:**
- Tracks delta from starting position: `abs(current - starting)`
- Works for both opens (0.0 → 0.2) and closes (0.2 → 0.0)
- Critical for close detection — without abs(), closes fail

**Quoting Strategies:**
| Strategy | Description |
|----------|-------------|
| `"top_of_book"` | Use orderbook bid/ask directly |
| `"top_of_book_offset_pct"` | Offset from top by spread_pct |
| `"mid"` | Use (bid + ask) / 2 (recommended) |
| `"mark"` | Use mark price (fallback to mid if unavailable) |

**Aggressive Fallback:**
- BUY orders: Quote at ASK (lift the offer)
- SELL orders: Quote at BID (hit the bid)
- Ensures execution while minimizing market impact vs market orders

### Key Classes

| Class | Purpose |
|-------|---------|
| `SmartExecConfig` | Configuration with 12+ parameters (chunk_count, time_per_chunk, quoting_strategy, etc.) |
| `LegChunkState` | Per-leg state within a chunk (filled_qty, remaining_qty, is_filled) |
| `ChunkState` | State machine for chunk execution (QUOTING → FALLBACK → COMPLETED) |
| `SmartExecResult` | Execution summary (success, chunks_completed, fills, costs, fallback_count) |
| `SmartOrderbookExecutor` | Main executor integrating with TradeExecutor and AccountManager |
| `ChunkPhase` | Enum: QUOTING, FALLBACK, COMPLETED |

### Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `chunk_count` | 5 | Number of chunks to split order into |
| `time_per_chunk` | 600.0 | Time allowed per chunk in seconds |
| `quoting_strategy` | "top_of_book" | Pricing strategy |
| `spread_pct` | 0.5 | Spread offset as % for offset strategy |
| `reprice_interval` | 10.0 | How often to reprice (minimum 10s) |
| `reprice_price_threshold` | 0.1 | Minimum price change to trigger repricing |
| `min_order_qty` | 0.01 | Minimum order size to submit |
| `aggressive_attempts` | 10 | Number of aggressive fill attempts |
| `aggressive_wait_seconds` | 5.0 | Max wait per aggressive attempt |
| `aggressive_retry_pause` | 1.0 | Pause between aggressive attempts |

### Integration Status

`SmartOrderbookExecutor` is a standalone module. It is **not** integrated into `ExecutionRouter` — the router only supports `limit` and `rfq` modes. To use smart execution, call `SmartOrderbookExecutor.execute_smart_multi_leg()` directly.

### Use Cases

**Good for:**
- Trades below RFQ minimum ($50k notional)
- Multi-leg structures requiring price improvement
- Minimizing market impact
- Strategies where execution speed is not critical

**Not ideal for:**
- Urgent execution (use aggressive market orders)
- Very large trades (use RFQ for better pricing)
- Extremely illiquid options

### Performance

Tested with 3-leg butterfly (0.2/0.4/0.2 contracts):
- **Opening**: 57.1s, 100% fills, 2 chunks
- **Closing**: 65.4s, 100% fills, complete position closure
- **Slippage**: Minimal due to mid-price quoting

---

## Telegram Notifications

See [telegram_notifier.py](../telegram_notifier.py) for the implementation.

### Overview

Fire-and-forget Telegram alerts via the Bot API.  If `TELEGRAM_BOT_TOKEN` is not set, the notifier silently no-ops — zero impact on the trading bot.

### Setup
1. Message `@BotFather` on Telegram → `/newbot` → copy the bot token
2. Send any message to your new bot, then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`
3. Add to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   TELEGRAM_CHAT_ID=123456789
   ```

### Key Class
| Class | Purpose |
|-------|----------|
| `TelegramNotifier` | Thread-safe sender. All `send()` calls are rate-limited (1 msg/s) and wrapped in try/except — a Telegram failure never crashes the bot. |

### Notification Helpers
| Method | When it fires |
|--------|---------------|
| `notify_startup(environment)` | System boot |
| `notify_shutdown()` | Graceful shutdown |
| `notify_trade_opened(strategy, trade_id, legs, cost)` | Trade enters OPEN state |
| `notify_trade_closed(strategy, trade_id, pnl, roi, hold_min, cost)` | Trade enters CLOSED state |
| `notify_daily_summary(equity, upnl, net_delta, positions)` | Once per day at 07:00 UTC (wall-clock gated) |
| `notify_error(message)` | Consecutive failures in main loop |

### Integration
- Singleton access via `get_notifier()` — any module can import and call it without DI wiring.
- **Strategy-level opt-in:** Each strategy decides what to notify and when. Infrastructure modules (lifecycle engine, dashboard, kill switch) stay silent.
- Example: `strategies/atm_straddle.py` uses `on_trade_opened` and `on_trade_closed` callbacks to send Telegram alerts.

---

## Web Dashboard

See [dashboard.py](../dashboard.py) and [templates/](../templates/) for the implementation.

### Overview

Lightweight Flask + htmx dashboard that runs on a daemon thread inside the existing process.  It reads `TradingContext` and `StrategyRunner` state directly — no IPC or database needed.

### Setup
```
DASHBOARD_PASSWORD=your_secret   # required — dashboard disabled without it
DASHBOARD_PORT=8080              # optional, default 8080
```

### Wiring (automatic via main.py)
```python
from dashboard import start_dashboard
start_dashboard(ctx, runners, host="0.0.0.0", port=8080)
```

### Key Classes
| Class | Purpose |
|-------|----------|
| `DashboardLogHandler` | `logging.Handler` with a ring buffer (`deque`, maxlen 200). Attached to root logger on startup. |
| `_create_app()` | Flask app factory — builds all routes and returns the `Flask` instance. |
| `start_dashboard()` | Entry point: reads `DASHBOARD_PASSWORD`, attaches the log handler, spawns daemon thread running Flask. |

### Routes
| Route | Method | Auth | Description |
|-------|--------|------|-------------|
| `/login` | GET/POST | — | Session-based password login |
| `/logout` | GET | — | Clears session, redirects to login |
| `/` | GET | ✓ | Main dashboard page |
| `/api/account` | GET | ✓ | htmx fragment: equity, margin, Greeks |
| `/api/strategies` | GET | ✓ | htmx fragment: strategy cards with stats |
| `/api/positions` | GET | ✓ | htmx fragment: open positions table |
| `/api/orders` | GET | ✓ | htmx fragment: active orders from OrderManager ledger |
| `/api/logs` | GET | ✓ | htmx fragment: live log tail |
| `/api/strategy/<name>/pause` | POST | ✓ | Pause (disable ticks) for a strategy |
| `/api/strategy/<name>/resume` | POST | ✓ | Resume a paused strategy |
| `/api/strategy/<name>/stop` | POST | ✓ | Permanently stop a strategy |
| `/api/killswitch` | POST | ✓ | Activate kill switch — two-phase mark-price close of all positions |
| `/api/killswitch/status` | GET | ✓ | Poll kill switch progress (idle/phase1/phase2/done) |

### Kill Switch — PositionCloser

The kill switch uses `PositionCloser` (see [position_closer.py](../position_closer.py)) to close all exchange positions.
This is an **emergency procedure** — not part of normal strategy operation.  It runs in a background thread and
performs a complete shutdown sequence:

1. `LifecycleEngine.kill_all()` — cancel all tracked orders, mark all trades CLOSED
2. `OrderManager.cancel_all()` — belt-and-suspenders cleanup of all orders in the ledger
3. `StrategyRunner.stop()` on all runners — prevent new trades
3. Phase 1: limit orders at mark price (5 min, reprice every 30s)
4. Phase 2: aggressive pricing ±10% off mark (2 min, reprice every 15s)
5. Verify positions closed on exchange
6. Send Telegram summary

Dashboard returns immediately; progress reported via Telegram and `/api/killswitch/status`.

### Design Decisions
- **htmx polling** — each panel re-fetches its own fragment every 3–5 s; no WebSocket needed.
- **Session auth** — password stored in env, compared on login, stored in Flask session cookie.
- **Daemon thread** — if the main process dies, the dashboard dies with it. No orphan servers.
- **Read-only by default** — the dashboard reads existing objects. Only the control endpoints (pause/resume/stop/kill) mutate state.

---

## Health Check

See [health_check.py](../health_check.py) for the implementation.

### Overview

Background thread that logs system health every 5 minutes. Provides visibility into API connectivity, account equity/margin, and uptime. Integrates with TelegramNotifier for daily account summaries.

### Key Class
| Class | Purpose |
|-------|----------|
| `HealthChecker` | Daemon thread: polls `account_snapshot_fn()` on interval, logs at DEBUG (normal) or WARNING (high margin / low equity). Triggers `notifier.notify_daily_summary()` once per ~23 h. |

### Key Methods
| Method | Purpose |
|--------|----------|
| `start()` | Launch background health-check thread |
| `stop()` | Stop thread (join with timeout) |
| `set_account_snapshot_fn(fn)` | Set the callable that returns an `AccountSnapshot` |

### Escalation Rules
- **Normal**: logged at `DEBUG` (suppressed unless log level lowered)
- **Margin utilization > 80%**: escalated to `WARNING`
- **Equity < $100**: escalated to `WARNING`

---

## Trade State Persistence

See [persistence.py](../persistence.py) for the implementation.

### Overview

Saves and recovers active trade state to/from JSON. Provides crash recovery and operational visibility.

### Key Class
| Class | Purpose |
|-------|----------|
| `TradeStatePersistence` | Writes `logs/trade_state.json` on every tick (throttled to 60 s). Appends completed trades to `logs/trade_history.jsonl`. |

### Key Methods
| Method | Purpose |
|--------|----------|
| `save_trades(trades)` | Snapshot active trades to `trade_state.json` (throttled) |
| `load_trades()` | Load last saved state for crash recovery |
| `clear()` | Remove the state file |
| `save_completed_trade(trade)` | Append a finished trade to `trade_history.jsonl` |
| `load_trade_history()` | Read all completed trade records |

### Files
| File | Format | Purpose |
|------|--------|---------|
| `logs/trade_state.json` | JSON | Current active-trade snapshot (overwritten each save) |
| `logs/trade_history.jsonl` | JSON Lines | Append-only log of completed trades |
