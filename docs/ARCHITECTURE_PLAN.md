# CoincallTrader Architecture & Development Plan

**Version:** 3.0  
**Date:** March 3, 2026  
**Status:** v0.8.0 — Web Dashboard (Phase 7 complete)

---

## Executive Summary

This document outlines the transformation of CoincallTrader from a simple options trading bot into a comprehensive, multi-instrument trading management system capable of running complex, time-aware strategies while maintaining code elegance and manageability.

---

## Current State (Phase 5 complete)

### Implemented
- ✅ **Authentication** — HMAC-SHA256 signing (`auth.py`), JSON + form-urlencoded
- ✅ **Configuration** — Environment switching via `.env`, strategy params in code (`config.py`)
- ✅ **Market data** — Option chains, orderbooks, BTC price, option details (`market_data.py`)
- ✅ **Option selection** — Expiry/strike/delta filtering + `LegSpec` declarative resolution + compound `find_option()` with multi-constraint support + **DTE-based expiry** (`option_selection.py`)
- ✅ **Structure templates** — `straddle()`, `strangle()` → return `List[LegSpec]` for plug-in to `StrategyConfig` (`option_selection.py`)
- ✅ **Order execution** — Limit orders, get/cancel/status queries; **ExecutionPhase** for phased pricing (mark → mid → aggressive), **ExecutionParams** with optional phases list (`trade_execution.py`)
- ✅ **RFQ execution** — Block trades for $50k+ notional multi-leg structures; **orderbook comparison fix** (correct side selection for buy/sell, unified improvement formula) (`rfq.py`)
- ✅ **Smart orderbook execution** — Chunked quoting with aggressive fallback (`multileg_orderbook.py`)
- ✅ **Trade lifecycle** — State machine (PENDING_OPEN → … → CLOSED), exit conditions, multi-leg native; **RFQParams** typed config (`trade_lifecycle.py`)
- ✅ **Exit conditions** — `profit_target`, `max_loss`, `max_hold_hours`, **`time_exit`** (absolute clock), **`utc_datetime_exit`** (specific datetime), `account_delta_limit`, `structure_delta_limit`, `leg_greek_limit` (`trade_lifecycle.py`, `strategy.py`)
- ✅ **Position monitoring** — Background polling, `AccountSnapshot`/`PositionSnapshot`, live Greeks (`account_manager.py`)
- ✅ **Strategy framework** — `TradingContext` DI, `StrategyConfig` (with `execution_params`, `rfq_params`), `StrategyRunner`, 7 entry condition factories, dry-run mode (`strategy.py`)
- ✅ **Strategy lifecycle** — `max_trades_per_day` gate, `on_trade_closed` callback, `stats` property (`strategy.py`)
- ✅ **Scheduling** — `time_window()`, `utc_time_window()`, `weekday_filter()` as entry conditions; `utc_datetime_exit()` for precise close scheduling
- ✅ **Account info** — Equity, available margin, IM/MM amounts, margin utilisation, aggregated Greeks
- ✅ **Logging** — File + console logging to `logs/trading.log` (audit trail)
- ✅ **Phase 1 Hardening** — Request timeouts (30s), @retry decorator with exponential backoff (1-2-4s), main loop error isolation (max 10 consecutive errors before exit) (`auth.py`, `retry.py`, `main.py`)
- ✅ **Phase 2 Reliability** — Market data caching with 30s TTL & max 100 entries (`market_data.py`), trade state persistence to `logs/trade_state.json` every 60s for crash recovery (`persistence.py`), background health check logging every 5 minutes (`health_check.py`), fixed `max_concurrent_trades=2` for daily rolling positions
- ✅ **Configurable Execution Timing** — `ExecutionPhase` dataclass for phased limit pricing (aggressive/mid/top_of_book/mark with duration, buffer, reprice interval); `RFQParams` typed dataclass replacing loose metadata keys; wired through `StrategyConfig` and `TradeLifecycle` with full backward compatibility (`trade_execution.py`, `trade_lifecycle.py`, `strategy.py`)
- ✅ **Telegram Notifications** — Fire-and-forget alerts via Bot API: trade opens/closes (PnL, ROI), daily account summary, startup/shutdown, critical errors. Wired at framework level — all strategies get notifications automatically (`telegram_notifier.py`)
- ✅ **Web Dashboard** — Real-time browser UI (Flask + htmx) running as daemon thread. Account summary, strategy cards with Pause/Resume/Stop controls, positions table, live log tail, kill switch. Password-protected via `DASHBOARD_PASSWORD` env var (`dashboard.py`, `templates/`)

### Not yet implemented
- ⬜ Multi-instrument support (futures, spot)
- ⬜ Margin alerts (email/webhook)
- ⬜ Historical P&L tracking
- ⬜ Expiry-aware rolling

---

## Coincall API Reference

See [API_REFERENCE.md](API_REFERENCE.md) for exchange endpoint documentation and response formats.
See [MODULE_REFERENCE.md](MODULE_REFERENCE.md) for internal module documentation and code examples.

Key facts:
- Official docs: https://docs.coincall.com/
- Options, Futures, Spot instruments supported
- Order rate limit: 60/s
- RFQ minimum notional: $50,000
- Order states: 0=NEW, 1=FILLED, 2=PARTIAL, 3=CANCELED, 6=INVALID
- Order status endpoint: `GET /open/option/order/singleQuery/v1?orderId={id}` (not path-based)

---

## Target Architecture

### Design Principles
1. **Composition over inheritance** — Callable conditions, not class hierarchies
2. **Dataclasses everywhere** — Simple, typed data containers
3. **Single responsibility** — Each module does one thing well
4. **Configuration-driven** — Strategies defined as data (`StrategyConfig`), not hardcoded
5. **Tick-driven core** — `PositionMonitor` drives `StrategyRunner.tick()` — no event queues or extra threads
6. **Fail-safe defaults** — Conservative behavior when uncertain
7. **Flat file structure** — One module per concern; add packages only when complexity demands it

### Current Directory Structure
```
CoincallTrader/
├── main.py                 # Entry point — wires TradingContext, registers runners
├── strategy.py             # Strategy framework (TradingContext, StrategyConfig, StrategyRunner)
├── config.py               # Environment config (.env loading)
├── auth.py                 # HMAC-SHA256 API authentication with timeouts & retries
├── retry.py                # @retry decorator with exponential backoff
├── market_data.py          # Option chains, orderbooks, BTC price; TTLCache caching
├── option_selection.py     # LegSpec, resolve_legs(), select_option(), find_option(), straddle(), strangle()
├── trade_execution.py      # Order placement, cancellation, status queries; ExecutionPhase, ExecutionParams
├── trade_lifecycle.py      # TradeState machine, TradeLeg, LifecycleManager, RFQParams, exit conditions (incl. time_exit)
├── multileg_orderbook.py   # Smart chunked multi-leg execution
├── rfq.py                  # RFQ block-trade execution ($50k+ notional)
├── account_manager.py      # AccountSnapshot, PositionMonitor, margin/equity queries
├── persistence.py          # TradeStatePersistence: JSON snapshots for crash recovery
├── health_check.py         # HealthChecker: background health logging every 5 minutes
├── telegram_notifier.py    # Telegram Bot API notifications (fire-and-forget)
├── dashboard.py            # Web dashboard (Flask + htmx, daemon thread)
├── templates/              # Dashboard HTML templates (Jinja2 + htmx)
│   ├── dashboard.html      # Main page with auto-polling panels
│   ├── login.html          # Password login
│   ├── _account.html       # Account metrics fragment
│   ├── _strategies.html    # Strategy cards with controls
│   ├── _positions.html     # Positions table fragment
│   └── _logs.html          # Log tail fragment
├── strategies/
│   ├── __init__.py
│   ├── blueprint_strangle.py  # Blueprint strategy — starting template for traders
│   ├── atm_straddle.py        # Daily ATM straddle with profit target + time exit
│   ├── long_strangle_pnl_test.py  # Long strangle PnL monitoring test
│   └── reverse_iron_condor_live.py  # Reverse iron condor live trading
├── requirements.txt
├── .env                    # API keys + dashboard password (gitignored)
├── docs/
│   ├── ARCHITECTURE_PLAN.md
│   ├── API_REFERENCE.md
│   └── MODULE_REFERENCE.md
├── tests/
│   ├── test_strategy_framework.py   # 72/72 unit assertions
│   ├── test_strategy_layer.py       # 50 strategy layer assertions
│   ├── test_atm_straddle.py         # ATM straddle strategy unit tests
│   ├── test_execution_timing.py     # 40/40 ExecutionPhase, RFQParams, phased execution
│   ├── test_dashboard.py            # Standalone dashboard test with mock data
│   └── test_complex_option_selection.py  # 32/32 compound selection assertions
├── logs/                   # Runtime logs (gitignored)
└── archive/                # Legacy code (gitignored)
```

**Current size:** 16 Python modules + 6 HTML templates, ~7,000 lines total

### Future additions (when needed)
- `persistence/` — SQLite state storage and crash recovery
- Futures/spot modules may extend `market_data.py` and `trade_execution.py` directly rather than adding a package hierarchy

---

## Requirements Specification

### 1. Trade Lifecycle Management

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-TL-01 | ✅ **Done** | Dynamic instrument selection based on criteria (expiry, strike, delta) — `LegSpec` + `resolve_legs()` + compound `find_option()` |
| REQ-TL-02 | ✅ **Done** | Order placement with execution mode selection (limit, RFQ, smart) — 3 modes in `LifecycleManager` |
| REQ-TL-03 | ✅ **Done** | RFQ execution for multi-leg options trades |
| REQ-TL-04 | ✅ **Done** | Position tracking: link orders → fills → positions |
| REQ-TL-05 | ✅ **Done** | Conditional exit logic (profit targets, stop losses, time decay) |
| REQ-TL-06 | Medium | Partial fill handling and execution quality tracking |
| REQ-TL-07 | Medium | Order amendment and requoting |

### 2. Scheduling & Time-Based Conditions

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-SC-01 | ✅ **Done** | Time-of-day triggers (e.g., "open position at 08:00 UTC") |
| REQ-SC-02 | ✅ **Done** | Weekday filters (e.g., "no new positions on Friday") |
| REQ-SC-03 | ✅ **Done** | Expiry awareness — DTE-based expiry selection (`{"dte": 0}`) + `time_exit()` absolute close |
| REQ-SC-04 | Medium | Month-end logic (e.g., rebalancing triggers) |
| REQ-SC-05 | Medium | Calendar awareness (exchange holidays) |
| REQ-SC-06 | Low | Cron-like arbitrary scheduling expressions |

### 3. Portfolio Hierarchy & Architecture

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-PH-01 | ✅ **Done** | Position abstraction: PositionSnapshot in account_manager.py |
| REQ-PH-02 | ✅ **Done** | Structure grouping: TradeLifecycle groups legs (e.g., strangle = 1 lifecycle, 2 legs) |
| REQ-PH-03 | ✅ **Done** | Account abstraction: AccountSnapshot with equity, margins, aggregated Greeks |
| REQ-PH-04 | ✅ **Done** | Strategy abstraction: StrategyConfig + StrategyRunner in strategy.py |
| REQ-PH-05 | Medium | Event-driven core with typed events |
| REQ-PH-06 | ✅ **Done** | Structure-level and account-level Greeks aggregation |

### 4. Multi-Instrument Support

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-MI-01 | High | Futures trading (perpetuals and dated) |
| REQ-MI-02 | Medium | Spot trading (for hedging or cash management) |
| REQ-MI-03 | High | Unified order interface across all instruments |
| REQ-MI-04 | Medium | Cross-instrument hedging logic |

### 5. Account Information

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-AI-01 | ✅ **Done** | Balance & equity queries — `AccountSnapshot` (equity, available_margin, IM, MM, utilisation) |
| REQ-AI-02 | ✅ **Partial** | Margin monitoring — entry conditions (`min_available_margin_pct`, `max_margin_utilization`); alerts (email/webhook) not yet implemented |
| REQ-AI-03 | Medium | Wallet holdings per asset |
| REQ-AI-04 | Low | Historical P&L tracking |

### 6. Web Dashboard

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-WD-01 | ✅ **Done** | Strategy status display (running, paused, stopped) — dashboard strategy cards |
| REQ-WD-02 | ✅ **Done** | Open positions view with P&L and Greeks — dashboard positions table |
| REQ-WD-03 | ✅ **Partial** | Account health (margin level displayed; equity curve not yet) |
| REQ-WD-04 | ✅ **Done** | Remote access — bind to 0.0.0.0, password-protected |
| REQ-WD-05 | ✅ **Done** | Manual intervention — Pause/Resume/Stop per strategy + kill switch |

### 7. Persistence & Recovery

| Requirement | Priority | Description |
|-------------|----------|-------------|
| REQ-PR-01 | ✅ **Partial** | Position persistence — file logging to `logs/trading.log` provides audit trail; no DB storage yet |
| REQ-PR-02 | ✅ **Partial** | Order history — logged to file; no queryable DB |
| REQ-PR-03 | Medium | Persist strategy state to database |
| REQ-PR-04 | Medium | Restart recovery: reload state on startup |

---

## Implementation Phases

### Phase 0: Foundation Cleanup — SUPERSEDED

Originally planned an event-queue architecture (`core/events.py`, `core/event_queue.py`).  
This was replaced by the simpler **tick model**: `PositionMonitor.on_update()` drives `StrategyRunner.tick()` — no event queue, no extra threads, no scheduler dependency. Design principles 1–4 and 6 are followed; principle 5 (event-driven) was intentionally swapped for the tick-driven approach.

---

### Phase 1: RFQ Execution ✅ COMPLETE (Feb 9, 2026)
**Goal:** Enable RFQ-based execution for multi-leg options trades.

**Implementation Summary:**
Created `rfq.py` module (~800 lines) with complete RFQ lifecycle management.

**Key Classes:**
- `OptionLeg` - Dataclass for leg definition (instrument, side, qty)
- `RFQQuote` - Quote from market maker with `is_we_buy`/`is_we_sell` properties
- `RFQResult` - Execution result with success, total_cost, improvement_pct
- `RFQExecutor` - Main executor with `execute(legs, action='buy'|'sell')`

**Key Learnings:**
1. Legs specify their own `side` ("BUY" or "SELL") — spreads have both; the API does not require all legs to be BUY
2. Market makers respond with two-way quotes (both BUY and SELL)
3. Quote `side` indicates MM's action: `MM SELL` = we buy, `MM BUY` = we sell
4. Accept/Cancel endpoints require `application/x-www-form-urlencoded` content type
5. Minimum notional: $50,000 (sum of strike values)
6. Quotes typically arrive within 3-5 seconds

**API Endpoints Used:**
- `POST /open/option/blocktrade/request/create/v1` (JSON)
- `GET /open/option/blocktrade/request/getQuotesReceived/v1`
- `POST /open/option/blocktrade/request/accept/v1` (form-urlencoded)
- `POST /open/option/blocktrade/request/cancel/v1` (form-urlencoded)

**Deliverables:**
- [x] `rfq.py` - Complete RFQ execution module
- [x] `tests/test_rfq_integration.py` - Integration tests
- [x] Updated `auth.py` with `use_form_data` support
- [x] Updated `docs/API_REFERENCE.md` with RFQ documentation

---

### Phase 2: Position Monitoring & Trade Lifecycle ✅ COMPLETE (Feb 10, 2026)
**Goal:** Monitor positions with live Greeks, and orchestrate trades through their full lifecycle (open → manage → close).

**Implementation Summary:**

**Part A: Position Monitoring** (added to `account_manager.py`):
- `PositionSnapshot` — frozen dataclass for a single position (Greeks, PnL, mark price)
- `AccountSnapshot` — frozen dataclass for full account state (equity, margins, aggregated Greeks)
- `PositionMonitor` — background polling with thread-safe snapshot access and callbacks
- Uses `upnlByMarkPrice` / `roiByMarkPrice` for accurate options PnL

**Part B: Trade Lifecycle** (new file `trade_lifecycle.py`):
- `TradeState` enum: PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED | FAILED
- `TradeLeg` — tracks a single leg from intent through order, fill, and position
- `TradeLifecycle` — groups legs into a trade with exit conditions and execution mode
- `LifecycleManager` — state machine that advances trades via `tick()` callback
  - Supports "limit" mode (per-leg orders via TradeExecutor) and "rfq" mode (atomic via RFQExecutor)
  - `tick()` hooks into PositionMonitor.on_update() for automatic advancement
  - `force_close()` and `cancel()` for manual intervention

**Exit Condition System:**
Exit conditions are callables `(AccountSnapshot, TradeLifecycle) -> bool`.
Factory functions provided for common patterns:
- `profit_target(pct)` — structure PnL as % of entry cost
- `max_loss(pct)` — structure loss limit
- `max_hold_hours(hours)` — time-based exit
- `account_delta_limit(threshold)` — account-level Greek limit
- `structure_delta_limit(threshold)` — structure-level Greek limit
- `leg_greek_limit(leg_index, greek, op, value)` — per-leg Greek threshold
- Custom lambdas/functions for anything else

**Key Design Decisions:**
1. Flat architecture — no Portfolio/Account wrapper classes; lifecycle IS the trade
2. Callable exit conditions instead of Strategy ABC — composable, testable, no class hierarchy
3. `tick()` model — driven by PositionMonitor, no extra threads or event queues
4. Multi-leg native — Iron Condor = one lifecycle with 4 legs

**Deliverables:**
- [x] `PositionSnapshot`, `AccountSnapshot`, `PositionMonitor` in `account_manager.py`
- [x] `trade_lifecycle.py` — TradeState, TradeLeg, TradeLifecycle, LifecycleManager
- [x] Exit condition factories (profit, loss, time, Greeks at all levels)
- [x] `tests/test_position_monitor.py` — position monitoring integration test
- [x] `tests/test_trade_lifecycle.py` — lifecycle dry-run and live test

---

### Phase 3: Smart Orderbook Execution ✅ COMPLETE (Feb 13, 2026)
**Goal:** Enable smart multi-leg orderbook execution with chunking, continuous quoting, and aggressive fallback for trades below RFQ minimum ($50k notional).

**Implementation Summary:**

**Module:** `multileg_orderbook.py` (~1000 lines)

**Core Algorithm:**
1. **Chunk Calculation** — Split total order into N proportional chunks (e.g., 0.4 contracts → 2 chunks of 0.2 each, maintaining leg ratios)
2. **Position-Aware Tracking** — Track delta from starting position using `abs(current - starting)` to handle:
   - Opens: Starting=0.0, Target=0.2 → fill delta 0.2
   - Closes: Starting=0.2, Target=0.2 → fill delta 0.2 (position goes to 0)
3. **Per-Chunk Execution:**
   - **Phase A (Quoting):** Place limit orders at calculated prices for `time_per_chunk` seconds
     - Continuous repricing every `reprice_interval` seconds (min 10s)
     - Cancel and reprice when market moves beyond `reprice_price_threshold`
     - Stop quoting legs individually as they fill (others continue)
   - **Phase B (Aggressive Fallback):** If not filled, use aggressive limit orders crossing the spread
     - Multiple retry attempts with configurable wait times
     - Exits early when all legs filled
4. **Early Termination** — Between chunks, check if target already reached and stop processing remaining chunks

**Key Classes:**
- `SmartExecConfig` — Configuration with 12+ parameters (chunk_count, time_per_chunk, quoting_strategy, etc.)
- `LegChunkState` — Per-leg state within a chunk (filled_qty, starting_position, remaining_qty, is_filled)
- `ChunkState` — State machine for chunk execution (QUOTING → FALLBACK → COMPLETED)
- `SmartExecResult` — Execution summary (success, chunks_completed, fills, costs, fallback_count)
- `SmartOrderbookExecutor` — Main executor class integrating with TradeExecutor and AccountManager

**Quoting Strategies:**
- `"top_of_book"` — Use orderbook bid/ask directly
- `"top_of_book_offset_pct"` — Offset from top by spread_pct (e.g., ±0.5%)
- `"mid"` — Use (bid + ask) / 2
- `"mark"` — Use mark price (fallbacks to mid if unavailable)

**Critical Fixes During Development:**
1. **Close Detection Bug** — Changed fill tracking from `max(0.0, current - starting)` to `abs(current - starting)` 
   - Without this, closes would return negative deltas clamped to 0
   - Algorithm would think nothing filled and loop indefinitely
   - Fix enabled both opens (0→0.1) and closes (0.2→0.1) to be tracked correctly

**Integration with LifecycleManager:**
- Opening trades: Uses `LifecycleManager.create()` with `execution_mode="smart"` and `smart_config`
- Closing trades: Currently direct call to `SmartOrderbookExecutor.execute_smart_multi_leg()` 
  - LifecycleManager doesn't yet support smart close mode (future enhancement)

**Testing Results:**
- ✅ Butterfly spread (3 legs, different quantities: 0.2/0.4/0.2)
  - Opening: 57.1s execution, 100% fills, 2 chunks
  - Closing: 65.4s execution, 100% fills, 2 chunks, positions fully closed
- ✅ Proportional chunking maintains leg ratios
- ✅ Mid-price quoting reduces slippage vs aggressive orders
- ✅ Early termination when fills complete
- ✅ Handles both increasing positions (opens) and decreasing positions (closes)

**Configuration Example:**
```python
smart_config = SmartExecConfig(
    chunk_count=2,              # Split into 2 chunks
    time_per_chunk=20.0,        # 20 seconds per chunk
    quoting_strategy="mid",     # Quote at mid-price
    reprice_interval=10.0,      # Reprice every 10s
    reprice_price_threshold=0.1,# Reprice if price moves >0.1
    min_order_qty=0.01,         # Minimum order size
    aggressive_attempts=10,     # Max fallback attempts
    aggressive_wait_seconds=5.0,# Wait 5s per attempt
    aggressive_retry_pause=1.0  # 1s between attempts
)
```

**API Integration:**
- Uses TradeExecutor for order placement/cancellation (limit orders)
- Uses AccountManager for position polling (fill detection)
- Uses market_data.get_option_orderbook() for pricing

**Deliverables:**
- [x] `multileg_orderbook.py` — Complete smart execution module
- [x] `tests/test_smart_butterfly.py` — Full lifecycle test (open + close)
- [x] `tests/close_butterfly_now.py` — Emergency close utility
- [x] Position-aware fill tracking for opens and closes
- [x] Comprehensive logging and execution reporting

---

### Phase 4: Strategy Framework ✅ COMPLETE (Feb 14, 2026)
**Goal:** Enable declarative, config-driven strategy definitions with composable entry/exit conditions, dependency injection, and dry-run mode.

**Implementation Summary:**

**New Module:** `strategy.py` (~578 lines)

**Core Classes:**
- `TradingContext` — Dependency injection container holding every service (auth, market data, executor, RFQ, smart executor, account manager, position monitor, lifecycle manager). Strategies and tests receive this instead of importing globals.
- `StrategyConfig` — Declarative strategy definition: legs (`LegSpec` list), entry conditions, exit conditions, execution mode, concurrency limits, cooldown, and dry-run flag.
- `StrategyRunner` — Tick-driven executor: checks entry conditions, resolves `LegSpec`s to concrete symbols via `resolve_legs()`, creates trade lifecycles, delegates to `LifecycleManager`.
- `build_context()` — Factory function that wires all services from `config.py` settings.

**Entry Condition Factories:**
| Factory | Description |
|---------|-------------|
| `time_window(start, end)` | UTC hour window (e.g., 8–20) |
| `weekday_filter(days)` | Day-of-week filter (e.g., Mon–Thu) |
| `min_available_margin_pct(pct)` | Minimum free margin % |
| `min_equity(usd)` | Minimum account equity |
| `max_account_delta(limit)` | Account delta threshold |
| `max_margin_utilization(pct)` | IM/equity ceiling |
| `no_existing_position_in(symbols)` | Block if already positioned |

**Modified Module:** `option_selection.py` — Added:
- `LegSpec` dataclass — declares option_type, side, qty, strike_criteria, expiry_criteria, underlying
- `resolve_legs()` — converts `list[LegSpec]` to `list[TradeLeg]` by querying market data

**Modified Module:** `trade_lifecycle.py` — Added:
- `strategy_id` field on `TradeLifecycle` for per-strategy tracking
- `_get_orderbook_price()` helper for live pricing
- `get_trades_for_strategy()` and `active_trades_for_strategy()` on `LifecycleManager`

**Modified Module:** `trade_execution.py` — Fixed:
- `get_order_status()` endpoint: `/open/option/order/singleQuery/v1?orderId={id}`
- `cancel_order()` sends orderId as `int()` per API spec
- Fill field: `fillQty` (was `executedQty`), state 3 = CANCELED (was 4)

**Modified Module:** `main.py` — Rewritten:
- Uses `build_context()` for service wiring
- Registers `StrategyRunner` instances on `PositionMonitor.on_update()`
- Signal handling (SIGINT/SIGTERM) for graceful shutdown

**Dry-Run Mode:**
- `StrategyConfig(dry_run=True)` enables simulated execution
- Uses `get_option_details()` for live pricing without placing orders
- Logs entry/exit prices, estimated PnL, and position details
- Full lifecycle reporting with `_execute_dry_run()`

**Key Design Decisions:**
1. No Strategy ABC — strategies are `StrategyConfig` data, not class hierarchies
2. Entry conditions mirror exit conditions — both `Callable[[AccountSnapshot, ...], bool]`
3. `resolve_legs()` decouples leg specification from symbol resolution
4. DI container enables testing with mock services
5. `StrategyRunner.tick()` is registered on PositionMonitor — no extra threads

**Testing Results:**
- Tests 1–7 (unit): 72/72 assertions passed
  - Config validation, context building, entry condition logic, LegSpec/resolve_legs, runner lifecycle, dry-run mode, edge cases
- Test 8a (integration dry-run): 11/11 passed — live pricing, no orders
- Test 8b (integration micro-trade): 16/16 passed — full lifecycle: opening → open → pending_close → closing → closed in 11.3s

**Deliverables:**
- [x] `strategy.py` — TradingContext, StrategyConfig, StrategyRunner, entry conditions, build_context()
- [x] `option_selection.py` updates — LegSpec dataclass, resolve_legs(), find_option() compound selection
- [x] `trade_lifecycle.py` updates — strategy_id, _get_orderbook_price(), per-strategy queries
- [x] `trade_execution.py` fixes — correct endpoint, field names, state codes
- [x] `main.py` rewrite — DI wiring, strategy registration, signal handling
- [x] `tests/test_strategy_framework.py` — 72/72 unit test assertions
- [x] `tests/test_live_dry_run.py` — 27/27 integration test assertions
- [x] `tests/test_complex_option_selection.py` — 32/32 compound option selection assertions
- [x] Workspace cleanup — 6 legacy files moved to archive/

---

### Phase 4.5: RFQ Comparison Fix + Endurance Testing ✅ COMPLETE (Feb 23, 2026)
**Goal:** Fix critical bug in RFQ orderbook comparison and validate with live market data.

**Bug Description:**
`get_orderbook_cost()` always used `leg.side` to determine ask/bid, but for simple structures (strangles) all legs have `side="BUY"`. When `action="sell"`, it should check bids (what we'd receive), not asks. This made sell-side orderbook comparison meaningless — reporting +180% "improvement" because it was comparing against the wrong side of the book.

**Fixes Applied to `rfq.py`:**
1. **`get_orderbook_cost(legs, action="buy")`** — Added `action` parameter. Computes `effectively_buying = (leg.side == "BUY") == want_to_buy` to select correct orderbook side (ask for buying, bid for selling).
2. **`calculate_improvement()`** — Unified to single formula `(orderbook - quote) / |orderbook| * 100` for both directions. Was previously inverted for sell side.
3. **`execute()`** — Passes `action=action` through to `get_orderbook_cost()`.

**Additional Changes:**
- `_close_rfq()` docstring in `trade_lifecycle.py` — removed stale "legs as BUY" comment
- Added `utc_time_window()` entry condition and `utc_datetime_exit()` exit condition to `strategy.py`
- Created `strategies/rfq_endurance.py` — 3-cycle endurance test strategy

**Validation Results:**
- Strangle: BUY quotes +0 to +4%, SELL quotes +7 to +14% (was +180%)
- Iron condor (mixed BUY/SELL legs): BUY +2 to +5.5%, SELL +6.2 to +6.3%
- 3-cycle endurance test: all cycles completed, clean shutdown

**Deliverables:**
- [x] Fixed `get_orderbook_cost()`, `calculate_improvement()`, `execute()` in `rfq.py`
- [x] Fixed stale docstrings in `trade_lifecycle.py`
- [x] `utc_time_window()` and `utc_datetime_exit()` in `strategy.py`
- [x] `strategies/rfq_endurance.py` — endurance test strategy
- [x] `tests/test_rfq_comparison.py` — strangle RFQ quote monitoring
- [x] `tests/test_rfq_iron_condor.py` — iron condor RFQ quote monitoring

---

### Phase 5: Multi-Instrument Support (2-3 days)
**Goal:** Extend trading to futures and spot markets.

**Approach:** Extend existing flat modules rather than creating a `data/` package.

**Tasks:**
1. Add futures methods to `market_data.py` and `trade_execution.py`:
   - `get_futures_instruments()`, `get_futures_orderbook(symbol)`
   - `place_futures_order()`, `get_futures_positions()`
2. Add spot methods similarly
3. Extend `LegSpec` to support non-option instruments

**API Endpoints:**
- `GET /open/futures/market/instruments/v1`
- `POST /open/futures/order/create/v1`
- `GET /open/spot/market/instruments`
- `POST /open/spot/trade/order/v1`

**Deliverables:**
- [ ] Futures support in `market_data.py` + `trade_execution.py`
- [ ] Spot support in `market_data.py` + `trade_execution.py`
- [ ] Extended `LegSpec` for non-option instruments
- [ ] Integration tests

---

### Phase 6: Account Alerts & Monitoring (partially complete)
**Goal:** Add proactive alerting on top of the existing `AccountSnapshot` infrastructure.

**Done:**
- `AccountSnapshot` with equity, available_margin, IM, MM, margin_utilisation, aggregated Greeks
- Entry conditions: `min_available_margin_pct()`, `max_margin_utilization()`, `min_equity()`
- **Telegram notifications** (v0.7.1): `telegram_notifier.py` — fire-and-forget alerts via Bot API. Trade opens/closes (PnL, ROI, hold time), daily account summary, startup/shutdown, critical errors. Wired at framework level — all strategies automatically notified.

**Remaining tasks:**
1. ~~Alert notification system~~ → Done via Telegram (`telegram_notifier.py`)
2. Wallet holdings per asset (`GET /open/account/wallet/v1`)
3. Historical P&L tracking

**Deliverables:**
- [x] `telegram_notifier.py` — TelegramNotifier class (~200 lines, fire-and-forget, rate-limited)
- [x] Framework integration — `TradingContext.notifier`, `StrategyRunner` auto-notifications on trade open/close
- [x] `HealthChecker` integration — daily summary via Telegram
- [ ] Wallet holdings integration
- [ ] P&L history logging

---

### Phase 7: Web Dashboard ✅ COMPLETE (March 3, 2026)
**Goal:** Create a simple web interface for monitoring and basic control.

**Implementation Summary:**

Chose **Flask + htmx** over FastAPI — simpler, no async rewrite, single `<script>` tag for htmx (CDN), zero JS to write. Runs as a daemon thread inside the existing process (no IPC, no separate service).

**Module:** `dashboard.py` (~280 lines)

**Core Design:**
- `DashboardLogHandler` — `logging.Handler` subclass with ring buffer (`deque`, 200 entries) attached to root logger. Captures all log output for the live tail without modifying existing log setup.
- `_create_app()` — Flask app factory. Receives `TradingContext` and `runners` list, reads them directly. Session-based auth via `DASHBOARD_PASSWORD` env var.
- `start_dashboard()` — Launches Flask on a daemon thread. If `DASHBOARD_PASSWORD` is not set, silently disables — zero impact.

**Routes:**
| Route | Method | Description |
|-------|--------|-------------|
| `/login` | GET/POST | Password login page |
| `/logout` | GET | Clear session |
| `/` | GET | Main dashboard page (full HTML) |
| `/api/account` | GET | Account metrics fragment (htmx) |
| `/api/strategies` | GET | Strategy cards fragment (htmx) |
| `/api/positions` | GET | Positions table fragment (htmx) |
| `/api/logs` | GET | Log tail fragment (htmx) |
| `/api/strategy/<name>/pause` | POST | Call `runner.disable()` |
| `/api/strategy/<name>/resume` | POST | Call `runner.enable()` |
| `/api/strategy/<name>/stop` | POST | Call `runner.stop()` |
| `/api/killswitch` | POST | Force-close all trades + Telegram alert |

**Templates:** 6 Jinja2 files in `templates/`:
- `dashboard.html` — Main page, CSS, htmx auto-polling (`every 3-5s` per panel)
- `login.html` — Login form
- `_account.html`, `_strategies.html`, `_positions.html`, `_logs.html` — htmx fragments

**Key Design Decisions:**
1. Flask over FastAPI — no async needed, simpler embedding in threaded app
2. htmx over WebSocket — polling every 3-5s is sufficient when exchange data itself polls every 10s
3. Daemon thread — if dashboard crashes, trading bot unaffected
4. No core module changes — reads existing `AccountSnapshot`, `runner.stats`, `runner._enabled`
5. Controls call existing methods — `enable()`, `disable()`, `stop()`, `force_close()`

**Deliverables:**
- [x] `dashboard.py` — Flask app, routes, log handler (~280 lines)
- [x] `templates/` — 6 HTML files (dashboard, login, 4 fragments)
- [x] `tests/test_dashboard.py` — Standalone test with mock data
- [x] `main.py` — 3 lines added (import + `start_dashboard()` call)
- [x] `requirements.txt` — Added `flask>=3.0.0`

---

### Phase 8: Persistence & Recovery (1-2 days)
**Goal:** Enable queryable state persistence and crash recovery beyond current file logging.

**Already done:**
- All trades, orders, and state transitions logged to `logs/trading.log`
- `LifecycleManager` tracks all trades in memory during runtime

**Remaining tasks:**
1. Create SQLite backend for structured persistence:
   - Trade lifecycles (state, legs, timestamps)
   - Order history (order_id, fill_price, fill_qty)
   - Strategy state (last run, cooldown, active trades)
2. Startup recovery:
   - Load persisted trades on restart
   - Reconcile with exchange position state
   - Resume StrategyRunners

**Deliverables:**
- [ ] `persistence.py` — SQLite read/write
- [ ] Database schema
- [ ] Startup recovery logic in `main.py`

---

## Priority Order Summary

| Priority | Phase | Effort | Why This Order |
|----------|-------|--------|----------------|
| 1 | **Phase 1: RFQ** | ✅ Done | Block trade execution for multi-leg options |
| 2 | **Phase 2: Position Monitoring & Lifecycle** | ✅ Done | Live monitoring, trade state machine, exit conditions |
| 3 | **Phase 3: Smart Orderbook Execution** | ✅ Done | Chunked orderbook execution for trades below RFQ minimum |
| 4 | **Phase 4: Strategy Framework** | ✅ Done | Declarative strategies, entry/exit conditions, DI, dry-run |
| 5 | Phase 5: Multi-Instrument | 2-3 days | Futures and spot support |
| 6 | Phase 6: Account Alerts | 1 day | Margin alerts, wallet, P&L history |
| 7 | **Phase 7: Dashboard** | ✅ Done | Web monitoring + controls (Flask + htmx) |
| 8 | Phase 8: Persistence | 1-2 days | State persistence and crash recovery |

**Total estimated effort:** 15-22 days of focused development (12-14 days completed)

---

## Open Questions

1. **Persistence format:** SQLite vs JSON files vs something else?
2. ~~**Dashboard auth:** Simple password vs OAuth vs VPN-only access?~~ → Answered: session-based login via `DASHBOARD_PASSWORD` env var
3. **Concurrent strategies:** Expected to run 2-3 or 10+?
4. ~~**Deployment target:** VPS, local machine, cloud?~~ → Answered: Windows Server 2022 VPS (primary), macOS locally
5. **Backtesting:** Is this a future requirement?

---

*Document maintained by the CoincallTrader development team.*
