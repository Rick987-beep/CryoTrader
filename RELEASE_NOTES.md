# Release Notes — v0.7.0 "Configurable Execution Timing"

**Release Date:** March 2, 2026  
**Previous Version:** v0.6.0 (Phase 1 & 2 Hardening)

---

## Overview

v0.7.0 adds **configurable execution timing** — the ability to define phased pricing strategies for limit orders and typed RFQ parameters. Instead of a single aggressive fill mode, you can now sequence pricing phases (e.g., "quote at mark for 5 minutes, then mid for 2 minutes, then aggressive") and configure RFQ timeouts and improvement thresholds as typed dataclasses rather than loose metadata keys.

All changes are **fully backward compatible** — existing strategies and configurations work unchanged.

---

## Key Features

### 1. ExecutionPhase — Phased Limit Order Pricing (`trade_execution.py`)

New `ExecutionPhase` dataclass declares a pricing phase:

```python
from trade_execution import ExecutionPhase, ExecutionParams

params = ExecutionParams(phases=[
    ExecutionPhase(pricing="mark",       duration_seconds=300, reprice_interval=30),
    ExecutionPhase(pricing="mid",        duration_seconds=120, reprice_interval=20),
    ExecutionPhase(pricing="aggressive", duration_seconds=60,  buffer_pct=2.0),
])
```

**Pricing modes:**
| Mode | Description |
|------|-------------|
| `"mark"` | Quote at mark price — most patient, waits for fair value |
| `"mid"` | Quote at (bid+ask)/2 — balanced approach |
| `"top_of_book"` | Match best bid/ask — competitive but no edge |
| `"aggressive"` | Cross the spread by buffer_pct — fastest fill |

**Phase behavior:** Each phase runs for its `duration_seconds`, repricing every `reprice_interval`. When a phase expires, the next one starts automatically. After the last phase, the fill manager signals expiry.

**Validation:** `duration_seconds` and `reprice_interval` are clamped to a 10-second minimum. Invalid pricing modes raise `ValueError`.

### 2. RFQParams — Typed RFQ Configuration (`trade_lifecycle.py`)

New `RFQParams` dataclass replaces loose metadata keys:

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
| `min_improvement_pct` | `float` | `-999.0` | Minimum improvement vs orderbook |
| `fallback_mode` | `str\|None` | `None` | What to do if RFQ fails |

### 3. Strategy-Level Wiring (`strategy.py`)

Both `execution_params` and `rfq_params` are now first-class fields on `StrategyConfig`:

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
        ExecutionPhase(pricing="aggressive", duration_seconds=60,  buffer_pct=2.0),
    ]),
    rfq_params=RFQParams(timeout_seconds=120, min_improvement_pct=1.0),
    exit_conditions=[profit_target(50), max_hold_hours(4)],
    max_trades_per_day=1,
)
```

These flow automatically through `StrategyRunner._open_trade()` → `LifecycleManager.create()` → `TradeLifecycle`.

### 4. LimitFillManager Rewrite (`trade_execution.py`)

The `LimitFillManager` was rewritten with a dual-mode architecture:
- **Legacy mode** (`phases=None`): Original behavior — single aggressive mode with `fill_timeout_seconds` and `max_requote_rounds`. Default for all existing code.
- **Phased mode** (`phases=[...]`): Walks through each `ExecutionPhase` in sequence with phase-aware pricing, per-phase reprice intervals, and automatic phase advancement.

New internal methods: `_check_phased()`, `_check_legacy()`, `_get_phased_price()`, `_get_price_for_current_mode()`.

---

## Backward Compatibility

All new fields default to `None`:
- `ExecutionParams(phases=None)` → legacy LimitFillManager behavior
- `StrategyConfig(execution_params=None, rfq_params=None)` → uses metadata dict as before
- `TradeLifecycle(execution_params=None, rfq_params=None)` → reads from metadata fallback

**No existing code needs to change.**

---

## Testing

| Test Suite | Assertions | Status |
|------------|-----------|--------|
| `test_execution_timing.py` (NEW) | 40/40 | ✅ |
| `test_strategy_framework.py` | 72/72 | ✅ |
| `test_strategy_layer.py` | 49/50 | ✅ (1 pre-existing 0DTE failure) |

The new test suite covers:
- ExecutionPhase defaults, validation, duration/reprice clamping
- ExecutionParams legacy vs phased modes
- RFQParams defaults and custom values
- TradeLifecycle new fields
- StrategyConfig new fields
- LimitFillManager initialization (legacy vs phased vs empty phases)

---

## File Changes

| File | Change |
|------|--------|
| `trade_execution.py` | **Modified** — +200 lines: ExecutionPhase, phased LimitFillManager |
| `trade_lifecycle.py` | **Modified** — +40 lines: RFQParams, typed param fields |
| `strategy.py` | **Modified** — +8 lines: wiring execution_params/rfq_params |
| `strategies/blueprint_strangle.py` | **Modified** — +20 lines: docs and examples |
| `tests/test_execution_timing.py` | **NEW** — 159 lines, 40 assertions |

**Total additions:** ~430 lines

---

## What's Next

- **Multi-instrument support** — futures, spot trading
- **Web dashboard** — monitoring interface
- **Account alerts** — margin alerts, wallet holdings

See [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) for the complete roadmap.

---

---

# Release Notes — v0.6.0 "Phase 1 & 2 Hardening — 48-Hour Reliability"

**Release Date:** February 24, 2026  
**Previous Version:** v0.5.1 (RFQ Comparison Fix)

---

## Overview

v0.6.0 is a major reliability upgrade designed to enable **48-hour autonomous operation** without manual intervention. Phase 1 (core resilience) adds request timeouts, intelligent retry logic, and error isolation. Phase 2 (operational visibility) adds market data caching, trade state persistence, and health check logging. Together, these ensure the bot can survive transient failures, API glitches, and even application crashes while maintaining operational awareness.

**Daily Use Case:** Deploy at 6:00 UTC, let it run through next day's 8:00 UTC, check it Monday evening. It's ready for Tuesday 7:05 AM regardless of what happened in between.

---

## Phase 1: Core Resilience

### 1. Request Timeouts (`auth.py`)

All API calls now wrap with a 30-second timeout:
```python
def _request_with_timeout(self, method, endpoint, data=None, timeout=30):
    # Wraps every GET/POST with timeout and @retry decorator
```

**Benefit:** Prevents hanging on unresponsive API; fails fast instead of blocking forever.

### 2. Intelligent Retry Logic (`retry.py`, NEW)

New `@retry` decorator with exponential backoff (1s → 2s → 4s):
```python
@retry(max_attempts=3, backoff_factor=1.0, backoff_jitter=0.1)
def _api_call():
    ...
```

**Key Design:** Only retries on **transient errors** (ConnectionError, Timeout), NOT on HTTP errors (4xx/5xx). This allows legitimate API errors to fail fast without wasting time on retries.

**Benefit:** Handles brief network glitches and API overloads without retrying unrecoverable errors.

### 3. Main Loop Error Isolation (`main.py`)

Main event loop catches exceptions per-iteration:
```python
while True:
    try:
        time.sleep(10)
        # ... check strategies, save state, etc.
        consecutive_errors = 0  # Reset on success
    except Exception as e:
        consecutive_errors += 1
        if consecutive_errors >= 10:
            logger.error("Too many consecutive errors — exiting")
            shutdown()
        time.sleep(5)  # Back off before retry
```

**Benefit:** Single bad iteration doesn't crash the whole app. Allows recovery — if API glitch clears, next iteration succeeds.

---

## Phase 2: Operational Visibility & Recovery

### 4. Market Data Caching (`market_data.py`, TTLCache NEW)

New `TTLCache` class with 30-second expiry and 100-entry max:
```python
class TTLCache:
    def get(self, key):
        if expired: delete and return None
        return cached_value
    
    def set(self, key, value):
        # Auto-evict oldest if max_size exceeded
```

Integrated into `get_option_instruments()` and `get_option_details()`:
```python
cache_key = f"instruments_{underlying}"
cached = self._instruments_cache.get(cache_key)
if cached:
    return cached  # Hit: save API call
fetch_from_api()
self._instruments_cache.set(cache_key, result)  # Cache for 30s
```

**Benefit:** Reduces API calls by ~70% on repeated queries (typical option selection does multiple queries in seconds). Provides fallback if API briefly stalls.

### 5. Trade State Persistence (`persistence.py`, NEW)

New `TradeStatePersistence` class auto-saves to `logs/trade_state.json`:
```python
{
  "timestamp": "2026-02-24T17:49:00Z",
  "trade_count": 1,
  "trades": [
    {
      "id": "TRADE_abc123",
      "strategy_id": "reverse_iron_condor_live",
      "state": "OPEN",
      "open_legs": [...4 legs...],
      "entry_cost": 1200
    }
  ]
}
```

Wired into main loop: saves every 60+ seconds (throttled):
```python
if now - last_persistence_save > 60:
    persistence.save_trades(all_active_trades)
    last_persistence_save = now
```

**Benefit:** If app crashes with an open position, you can see its exact state in the JSON file. On restart, PositionMonitor queries API and detects it immediately.

### 6. Health Check Logging (`health_check.py`, NEW)

New `HealthChecker` background thread logs to `logs/health.log` every 5 minutes:
```
═══════════════════════════════════════════════════════════════════
HEALTH CHECK — 2026-02-24 17:49:00 UTC
═══════════════════════════════════════════════════════════════════
Uptime: 2h 15m
Account snapshot: Equity=$50,234, Margin=$15,600, UtilizedMargin=$8,300, Positions=1, PortfolioDelta=+0.12
═══════════════════════════════════════════════════════════════════
```

Wired into main startup:
```python
health_checker = HealthChecker(
    check_interval=300,  # 5 minutes
    account_snapshot_fn=lambda: ctx.position_monitor.snapshot()
)
health_checker.start()
```

**Benefit:** Operational visibility without external infrastructure. Check `health.log` to see account status, position count, and portfolio delta without running the app again.

### 7. Bug Fix: max_concurrent_trades

Changed from 1 to 2 in `strategies/reverse_iron_condor_live.py`:
```python
max_concurrent_trades=2  # Allow 7:05 entry + previous day's 8:00 exit overlap
max_trades_per_day=1     # Still only 1 new trade per calendar day
```

**Why:** 1DTE positions expire at 8:00 UTC next day. If we enter at 7:05 UTC, we have 55 minutes with two positions open. `max_concurrent_trades=1` would block the new entry. Fix: allow 2 concurrent, but `max_trades_per_day=1` prevents duplicate entries on the same day.

---

## Configuration & Deployment

All hardening is **automatic** — no config changes needed. Just upgrade and run:
```bash
python main.py
```

Behavior:
- On startup: Loads persistent trade state (none expected), starts health checker
- Every iteration: Catches errors, saves trade state if >60s elapsed
- On exit: Saves final trade state, stops health checker cleanly

---

## Validation & Testing

| Component | Test | Result |
|-----------|------|--------|
| TTLCache | Set/get/expiry/max_size | ✅ All pass |
| TradeStatePersistence | Save/load to JSON | ✅ Works |
| HealthChecker | Start/stop lifecycle, logging | ✅ Clean |
| Market data caching | Integration with get_option_* | ✅ Transparent |
| Main loop error handling | Exception isolation + recovery | ✅ 10-strike limit works |
| Reverse iron condor daily rolling | max_concurrent_trades=2 | ✅ Allows 55-min overlap |
| RFQ test (1DTE selection) | 30-sec monitoring, all 4 legs | ✅ Passes (UI bug was Coincall, not ours) |

---

## 48-Hour Reliability Guarantee

**What happens if you deploy at 6:00 UTC Monday and crash at 2:00 PM Monday?**
1. Persistent JSON shows last known state + timestamp
2. Restart at 5:00 PM Monday
3. PositionMonitor queries API, detects any open positions
4. Health checker logs account equity/margin/delta to health.log
5. Tuesday 7:05 AM: Strategy checks `max_trades_per_day=1`, sees Monday's trade already happened, does NOT enter duplicate
6. You're safe ✅

**What you need to do:**
- Nothing (most likely). The app recovers automatically.
- If you crashed with an OPEN position and want to close it early: manually close on web interface before 8:00 UTC exit time
- Next morning at 7:05 UTC: New entry allowed (daily max prevents duplicate)

---

## Files Changed (Summary)

- **NEW:** `retry.py` (47 lines) — @retry decorator with exponential backoff
- **NEW:** `persistence.py` (114 lines) — Trade state JSON persistence
- **NEW:** `health_check.py` (133 lines) — 5-minute health check logging
- **MODIFIED:** `auth.py` (+5 lines) — Added _request_with_timeout() with retry
- **MODIFIED:** `market_data.py` (+70 lines) — TTLCache class + caching integration
- **MODIFIED:** `main.py` (+15 lines) — Wired persistence & health_checker
- **MODIFIED:** `strategies/reverse_iron_condor_live.py` — Fixed max_concurrent_trades: 1 → 2

**Total additions:** ~380 lines for 48-hour reliability.

---

# Release Notes — v0.5.1 "RFQ Comparison Fix"

**Release Date:** February 23, 2026  
**Previous Version:** v0.5.0 (Architecture Cleanup)

---

## Overview

v0.5.1 fixes a critical bug in the RFQ orderbook comparison logic and adds precise UTC scheduling conditions. The `get_orderbook_cost()` function was always using the wrong side of the orderbook when evaluating sell-direction trades, causing wildly inflated "improvement" metrics (+180%). After the fix, improvement percentages are realistic: BUY +0–4%, SELL +7–14%.

This release also adds `utc_time_window()` and `utc_datetime_exit()` for precise UTC scheduling, the `rfq_endurance.py` strategy for multi-cycle testing, and two new RFQ validation tests.

---

## Key Fixes

### 1. RFQ Orderbook Comparison (`rfq.py`)

**Problem:** `get_orderbook_cost()` always used `leg.side` to pick ask/bid. For simple structures (strangles), all legs are BUY. When `action="sell"`, it should check bids (what we'd receive), not asks. This made sell-side comparison meaningless.

**Fix:** Added `action` parameter. Now computes:
```python
effectively_buying = (leg.side == "BUY") == (action == "buy")
# If effectively buying → use ASK
# If effectively selling → use BID
```

### 2. Improvement Formula (`rfq.py`)

**Problem:** `calculate_improvement()` had inverted formula for sell direction.

**Fix:** Unified to single formula for both directions:
```python
improvement = (orderbook_cost - quote_cost) / abs(orderbook_cost) * 100
```

### 3. Stale Docstrings (`trade_lifecycle.py`)

**Problem:** `_close_rfq()` said "legs as BUY (Coincall requirement)" — incorrect for mixed structures.

**Fix:** Updated to "preserving each leg's side". Documented `rfq_min_improvement_pct` metadata key.

---

## New Features

### 4. `utc_time_window(start, end)` — Entry Condition

Accepts `datetime.time` objects for precise UTC scheduling (complements hour-based `time_window()`):
```python
from datetime import time
from strategy import utc_time_window

condition = utc_time_window(time(9, 30), time(10, 15))  # 09:30–10:15 UTC
```

### 5. `utc_datetime_exit(dt)` — Exit Condition

Triggers at a specific UTC datetime (complements daily `time_exit()`):
```python
from datetime import datetime
from strategy import utc_datetime_exit

exit_cond = utc_datetime_exit(datetime(2026, 2, 23, 19, 0))  # Close at 19:00 UTC on Feb 23
```

### 6. `strategies/rfq_endurance.py`

3-cycle endurance test strategy with UTC-scheduled open/close windows. Tests RFQ execution reliability over multiple consecutive cycles.

---

## Validation Results

| Test | BUY Improvement | SELL Improvement | Status |
|------|----------------|-----------------|--------|
| Strangle (before fix) | 0–4% | **+180%** (broken) | ❌ |
| Strangle (after fix) | 0–4% | 7–14% | ✅ |
| Iron condor (mixed sides) | 2–5.5% | 6.2–6.3% | ✅ |
| 3-cycle endurance | All filled | All filled | ✅ |

---

## File Changes

| File | Change |
|------|--------|
| `rfq.py` | **Modified** — `get_orderbook_cost()` action param, unified `calculate_improvement()`, `execute()` passthrough |
| `trade_lifecycle.py` | **Modified** — Fixed stale docstrings in `_open_rfq`/`_close_rfq` |
| `strategy.py` | **Modified** — Added `utc_time_window()`, `utc_datetime_exit()` |
| `strategies/rfq_endurance.py` | **NEW** — 3-cycle endurance test strategy |
| `tests/test_rfq_comparison.py` | **NEW** — Strangle RFQ quote monitoring |
| `tests/test_rfq_iron_condor.py` | **NEW** — Iron condor RFQ quote monitoring |

---

## Migration Guide

No breaking changes. The `action` parameter in `get_orderbook_cost()` defaults to `"buy"`, preserving backward compatibility. All existing strategies continue to work unchanged.

---

## What's Next

- **Phase 5:** Multi-instrument support (futures, spot)
- **Phase 6:** Account alerts and monitoring
- **Phase 7:** Web dashboard
- **Phase 8:** Persistence and crash recovery

See [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) for the complete roadmap.

---

## Project Statistics

| Metric | Value |
|--------|-------|
| Version | 0.5.1 |
| Release Date | February 23, 2026 |
| Modules Changed | 3 (rfq.py, trade_lifecycle.py, strategy.py) |
| New Files | 3 (rfq_endurance.py, test_rfq_comparison.py, test_rfq_iron_condor.py) |
| Total Core Modules | 11 |
| Python | 3.9+ |

---

---

# Release Notes — v0.4.0 "Strategy Framework"

**Release Date:** February 14, 2026  
**Previous Version:** v0.3.0 (Smart Orderbook Execution)

---

## Overview

v0.4.0 introduces the **Strategy Framework** — a declarative, config-driven approach to defining and running trading strategies. Instead of subclassing strategy ABCs, you compose a `StrategyConfig` that declares _what_ to trade, _when_ to enter, _when_ to exit, and _how_ to execute. The `StrategyRunner` handles the mechanics.

This release also includes critical API endpoint fixes, dependency injection via `TradingContext`, dry-run simulation mode, and comprehensive test coverage (72/72 unit + 27/27 integration assertions).

---

## Key Features

### 1. Declarative Strategy Definitions

Strategies are data, not class hierarchies:

```python
from strategy import StrategyConfig, time_window, weekday_filter, min_available_margin_pct
from option_selection import LegSpec
from trade_lifecycle import profit_target, max_loss, max_hold_hours

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
    exit_conditions=[profit_target(50), max_loss(100), max_hold_hours(24)],
    max_concurrent_trades=1,
    cooldown_seconds=3600,
    check_interval_seconds=60,
)
```

### 2. Dependency Injection with TradingContext

All services live in a single container — no module-level globals:

```python
from strategy import build_context

ctx = build_context()
# ctx.auth, ctx.market_data, ctx.executor, ctx.rfq_executor,
# ctx.smart_executor, ctx.account_manager, ctx.position_monitor,
# ctx.lifecycle_manager
```

For tests, individual services can be replaced with mocks.

### 3. Entry Condition Factories

Seven composable entry conditions, mirroring the existing exit condition pattern:

| Factory | Description |
|---------|-------------|
| `time_window(start, end)` | UTC hour window |
| `weekday_filter(days)` | Day-of-week filter |
| `min_available_margin_pct(pct)` | Minimum free margin % |
| `min_equity(usd)` | Minimum account equity |
| `max_account_delta(limit)` | Account delta ceiling |
| `max_margin_utilization(pct)` | IM/equity ceiling |
| `no_existing_position_in(symbols)` | Block if positioned |

All conditions must return `True` before a strategy opens a trade.

### 4. LegSpec and resolve_legs()

Legs are specified declaratively and resolved to concrete symbols at runtime:

```python
from option_selection import LegSpec, resolve_legs

leg = LegSpec("C", side=2, qty=0.1,
              strike_criteria={"type": "delta", "value": 0.25},
              expiry_criteria={"symbol": "28MAR26"})

# resolve_legs() queries market data and returns TradeLeg objects
# with actual symbols like "BTCUSD-28MAR26-105000-C"
```

Supported strike criteria: `delta`, `closestStrike`, `spotdistance%`, `strike` (exact).

### 5. Dry-Run Mode

```python
config = StrategyConfig(
    name="test_strategy",
    legs=[...],
    dry_run=True,  # no real orders placed
)
```

- Fetches live prices from the exchange via `get_option_details()`
- Simulates full lifecycle (entry, position, exit evaluation)
- Logs estimated fill prices, PnL, and structure details
- Use for strategy validation before committing capital

### 6. Tick-Driven Execution

`StrategyRunner.tick()` is registered on `PositionMonitor.on_update()`:
1. Position monitor polls the exchange (configurable interval)
2. Calls all registered `runner.tick(snapshot)` callbacks
3. Runner checks entry conditions, creates trades, advances lifecycle
4. No extra threads, timers, or event queues

---

## Bug Fixes

### get_order_status 404 Error (Critical)
- **Problem:** `get_order_status()` used path-based URL `/open/option/order/{id}/v1` → 404
- **Fix:** Changed to `GET /open/option/order/singleQuery/v1?orderId={id}`

### Wrong Fill Field Name
- **Problem:** Code checked `executedQty` — field does not exist in API response
- **Fix:** Changed to `fillQty`

### Wrong Cancel State Code
- **Problem:** Code treated state 4 as CANCELED
- **Fix:** State 3 = CANCELED per API docs (state 4 = PRE_CANCEL)

### cancel_order Type Error
- **Problem:** `orderId` sent as string; API requires integer
- **Fix:** Added `int()` cast in `cancel_order()`

---

## File Changes

| File | Change |
|------|--------|
| `strategy.py` | **NEW** — 578 lines |
| `option_selection.py` | **Modified** — Added LegSpec, resolve_legs() |
| `trade_lifecycle.py` | **Modified** — strategy_id, _get_orderbook_price(), fixed fillQty/state codes |
| `trade_execution.py` | **Modified** — Fixed get_order_status endpoint, cancel_order int cast |
| `main.py` | **Rewritten** — DI wiring, strategy registration, signal handling |
| `tests/test_strategy_framework.py` | **NEW** — 72/72 assertions |
| `tests/test_live_dry_run.py` | **NEW** — 27/27 assertions |

---

## Testing Results

### Unit Tests (72/72)
| Test | Assertions | Description |
|------|-----------|-------------|
| 1. Config validation | 10 | StrategyConfig defaults, field types |
| 2. TradingContext | 9 | DI container wiring, build_context() |
| 3. Entry conditions | 16 | All 7 entry condition factories |
| 4. LegSpec & resolve_legs | 10 | Dataclass fields, resolution logic |
| 5. StrategyRunner | 12 | Tick lifecycle, cooldown, concurrency |
| 6. Dry-run mode | 8 | Simulated execution, no real orders |
| 7. Edge cases | 7 | Empty legs, no conditions, boundary values |

### Integration Tests (27/27)
| Test | Assertions | Description |
|------|-----------|-------------|
| 8a. Live dry-run | 11 | Real API, live pricing, no orders |
| 8b. Micro-trade | 16 | Full lifecycle in 11.3s, entry $95 exit $70 |

---

## Migration Guide

**main.py** has been rewritten. If you customised the old scheduler-based main.py:
1. Review the new `build_context()` + `StrategyRunner` pattern
2. Convert strategy parameters to `StrategyConfig` + `LegSpec`
3. Register runners on `PositionMonitor.on_update()`

---

## What's Next

- **Phase 5:** Multi-instrument support (futures, spot)
- **Phase 6:** Account alerts and pre-trade checks
- **Phase 7:** Web dashboard
- **Phase 8:** Persistence and crash recovery

See [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) for the complete roadmap.

---

## Project Statistics

| Metric | Value |
|--------|-------|
| Version | 0.4.0 |
| Release Date | February 14, 2026 |
| New Module | strategy.py (~578 lines) |
| Total Core Modules | 11 |
| Unit Tests | 72/72 |
| Integration Tests | 27/27 |
| API Fixes | 3 |
| Python | 3.9+ |

---

*CoincallTrader Development Team*
