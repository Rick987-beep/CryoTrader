# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] - 2026-03-02

### Added - Configurable Execution Timing & RFQ Parameters

#### ExecutionPhase — Phased Limit Order Pricing (`trade_execution.py`)
- **`ExecutionPhase` dataclass** — Declarative pricing phase for limit orders. Fields: `pricing` (`"aggressive"` | `"mid"` | `"top_of_book"` | `"mark"`), `duration_seconds` (min 10s), `buffer_pct`, `reprice_interval` (min 10s).
- **`ExecutionParams.phases`** — Optional `List[ExecutionPhase]` on `ExecutionParams`. When set, `LimitFillManager` uses phased execution instead of legacy single-mode behavior.
- **`LimitFillManager` phased execution** — Rewrote with `_check_phased()` / `_check_legacy()` split. Phase-aware pricing via `_get_phased_price()` supports four pricing modes, automatic phase advancement on duration expiry, per-phase reprice intervals. Legacy mode preserved when `phases=None`.

#### RFQParams — Typed RFQ Configuration (`trade_lifecycle.py`)
- **`RFQParams` dataclass** — Typed container replacing loose metadata keys. Fields: `timeout_seconds` (default 60), `min_improvement_pct` (default -999), `fallback_mode` (default None).
- **`TradeLifecycle.execution_params`** and **`TradeLifecycle.rfq_params`** — Optional typed fields on the trade object. `LifecycleManager` reads from these first, falls back to `metadata` dict for backward compatibility.

#### Wiring Through Strategy Layer (`strategy.py`, `strategies/blueprint_strangle.py`)
- **`StrategyConfig.execution_params`** and **`StrategyConfig.rfq_params`** — Optional fields that flow through to `LifecycleManager.create()`.
- **`blueprint_strangle.py`** — Updated docstring and added commented-out examples for phased execution and RFQ params configuration.

### Testing
- **`tests/test_execution_timing.py`** (NEW) — 40/40 assertions covering ExecutionPhase validation, ExecutionParams legacy/phased modes, RFQParams defaults/custom, TradeLifecycle new fields, StrategyConfig new fields, LimitFillManager initialization.
- Existing test suites pass: `test_strategy_framework.py` 72/72, `test_strategy_layer.py` 49/50 (1 pre-existing 0DTE market data failure).

### Files Changed
- MODIFIED: `trade_execution.py` (+200 lines, ExecutionPhase, phased LimitFillManager)
- MODIFIED: `trade_lifecycle.py` (+40 lines, RFQParams, typed param fields)
- MODIFIED: `strategy.py` (+8 lines, wiring execution_params/rfq_params)
- MODIFIED: `strategies/blueprint_strangle.py` (+20 lines, documentation and examples)
- NEW: `tests/test_execution_timing.py` (159 lines, 40 assertions)

### Backward Compatibility
All new fields default to `None`. Existing strategies, metadata-based configuration, and the state machine are fully preserved.

---

## [0.6.0] - 2026-02-24

### Added - Phase 1 & 2 Hardening (48-Hour Reliability)

#### Phase 1: Core Resilience (`revision 0.6.0`)
- **Request Timeouts** (`auth.py`) — All API calls wrapped with 30-second timeout via `_request_with_timeout()` method
- **@retry Decorator** (`retry.py`, NEW) — Exponential backoff (1s → 2s → 4s) for transient errors only (ConnectionError, Timeout); deliberately does NOT retry on HTTP errors so legitimate 4xx/5xx fail fast
- **Error Isolation in Main Loop** (`main.py`) — Try-except around each iteration, consecutive error counter (max 10 before exit), auto-recovery between iterations

#### Phase 2: Operational Visibility & Recovery (`revision 0.6.0`)
- **Market Data Caching** (`market_data.py`, `TTLCache` NEW) — 30-second TTL caching on `get_option_instruments()` and `get_option_details()`, max 100 entries per cache; reduces API load ~70% on burst queries
- **Trade State Persistence** (`persistence.py`, NEW) — `TradeStatePersistence` class auto-saves active trades to `logs/trade_state.json` every 60 seconds (throttled) with timestamp, trade count, and per-trade state (id, symbol, legs, entry cost, created_at)
- **Health Check Logging** (`health_check.py`, NEW) — `HealthChecker` background thread logs account snapshot (equity, margin, positions, portfolio delta, uptime) to `logs/health.log` every 5 minutes
- **Main Loop Enhancement** (`main.py`) — Instantiates and wires `HealthChecker` and `TradeStatePersistence` at startup, saves trade state in main loop every 60s, proper cleanup on shutdown
- **Bug Fix: max_concurrent_trades** (`strategies/reverse_iron_condor_live.py`) — Changed from 1 to 2 to allow 55-minute overlap between daily rolling positions (7:05 entry while previous day's 8:00 exit is pending)

### Validated
- Phase 1 tested via high-frequency API operations with intentional failures
- Phase 2 tested: TTLCache expiry + max_size enforcement ✅, TradeStatePersistence save/load ✅, HealthChecker start/stop lifecycle ✅, MarketData cache attributes ✅
- RFQ test confirms all 4 legs sent to API (butterfly display was Coincall UI bug, not our bug) ✅
- Reverse iron condor RFQ test passes with correct 1DTE selection and deltas

### Files Changed
- NEW: `retry.py` (47 lines, @retry decorator with exponential backoff)
- NEW: `persistence.py` (114 lines, TradeStatePersistence class + JSON snapshots)
- NEW: `health_check.py` (133 lines, HealthChecker background logging)
- MODIFIED: `auth.py` (+5 lines, _request_with_timeout() with @retry decorator)
- MODIFIED: `market_data.py` (+70 lines, TTLCache class + cache integration in get_option_instruments & get_option_details)
- MODIFIED: `main.py` (+15 lines, persistence & health_checker instantiation & wiring)
- MODIFIED: `strategies/reverse_iron_condor_live.py` (max_concurrent_trades: 1 → 2)

---

## [0.5.1] - 2026-02-23

### Fixed - RFQ Orderbook Comparison Bug
- **`get_orderbook_cost()`** (`rfq.py`) — Added `action` parameter. Previously always used `leg.side` to pick ask/bid, but legs are always BUY for simple structures. When `action="sell"`, we should check bids (what we'd receive), not asks. Now computes `effectively_buying = (leg.side == "BUY") == want_to_buy` to select the correct orderbook side.
- **`calculate_improvement()`** (`rfq.py`) — Unified formula to `(orderbook - quote) / |orderbook| * 100` for both buy and sell directions. Sell-side formula was previously inverted, showing +180% "improvement" (nonsensical). After fix: BUY quotes +0 to +4%, SELL quotes +7 to +14% (realistic).
- **`execute()`** (`rfq.py`) — Now passes `action=action` to `get_orderbook_cost()`.
- **`_close_rfq()` docstring** (`trade_lifecycle.py`) — Removed stale "legs as BUY (Coincall requirement)" comment; now says "preserving each leg's side". Documented `rfq_min_improvement_pct` metadata key.

### Added
- **`utc_time_window(start, end)`** (`strategy.py`) — Entry condition accepting `datetime.time` objects for precise UTC scheduling (complements hour-based `time_window()`)
- **`utc_datetime_exit(dt)`** (`strategy.py`) — Exit condition triggering at a specific UTC datetime (complements `time_exit()` which is daily)
- **`strategies/rfq_endurance.py`** — 3-cycle RFQ endurance test strategy with UTC-scheduled open/close windows
- **`tests/test_rfq_comparison.py`** — Strangle RFQ quote vs orderbook monitoring test (validates RFQ comparison fix)
- **`tests/test_rfq_iron_condor.py`** — Iron condor RFQ quote monitoring test (validates mixed BUY/SELL legs)

### Validated
- 3-cycle endurance test: all cycles completed with clean shutdown, RFQ fill within 5 seconds
- Strangle comparison: BUY quotes +0 to +4% improvement, SELL quotes +7 to +14% (no longer +180%)
- Iron condor comparison: mixed BUY/SELL legs produce sensible improvement numbers

---

## [0.5.0] - 2026-02-17

### Changed - Architecture Cleanup
- **Strategies module**: Moved strategy definitions to `strategies/` package — each strategy is a standalone factory function
- **main.py**: Slimmed to a pure launcher — loads `STRATEGIES` list, wires context, starts monitor
- **Removed dry-run mode**: `dry_run` field removed from `StrategyConfig` and all dry-run execution logic removed from `StrategyRunner`
- **Removed module-level globals**: No more auto-instantiation on import (`trade_executor`, `account_manager`, `position_monitor`, `rfq_executor`, `lifecycle_manager`)
- **Removed convenience functions**: `place_order()`, `cancel_order()`, `get_order_status()`, `execute_rfq()`, `create_strangle_legs()`, `create_spread_legs()`, etc. — use class methods directly
- **Cleaned config.py**: Removed 9 dead config dicts (`WS_OPTIONS`, `ENDPOINTS`, `ACCOUNT_CONFIG`, `RISK_CONFIG`, `TRADING_CONFIG`, `OPEN_POSITION_CONDITIONS`, `CLOSE_POSITION_CONDITIONS`, `POSITION_CONFIG`, `LOGGING_CONFIG`)
- **Cleaned multileg_orderbook.py**: Removed dead fields (`active_order_id`, `target_price`), dead methods (`_calculate_chunks()`, `_check_and_update_fills()`), dead factory (`create_smart_config()`)
- **Cleaned rfq.py**: Removed `TakerAction` enum, `execute_with_fallback()`, `__main__` block
- **Unified logging**: `option_selection.py` now uses `logger` throughout (was mixing `logging.*` and `logger.*`)
- **Removed redundant imports**: Cleaned inline `import requests` in `market_data.py`
- **Updated docstrings**: Fixed stale attribute docs and usage examples

### Fixed (pre-cleanup, committed in 301f2af)
- Cancel stale order IDs: no longer tries to cancel already-resolved orders
- Close retry double-order: prevents duplicate close orders on retry
- force_close() CLOSING state: properly handles trades stuck in CLOSING
- _check_close_fills fill sync: correctly syncs fill data on close

---

## [0.4.1] - 2026-02-17

### Added - Compound Option Selection
- **`find_option()`** (`option_selection.py`) — single-call compound option selection
  - Expiry constraints: `min_days`, `max_days`, `target` ("near"/"far"/"mid")
  - Strike constraints: `below_atm`, `above_atm`, `min_strike`, `max_strike`, `min_distance_pct`, `max_distance_pct`, `min_otm_pct`, `max_otm_pct`
  - Delta constraints: `min`, `max`, `target`
  - Ranking strategies: `delta_mid`, `delta_target`, `strike_atm`, `strike_otm`, `strike_itm`
  - Returns enriched dict with `symbolName`, `strike`, `delta`, `days_to_expiry`, `distance_pct`, `index_price`
  - Smart delta budget: applies non-delta filters first, then fetches deltas for at most 10 options (prioritised by ATM proximity)
- **Internal helpers**: `_find_filter_expiry()`, `_find_filter_strike()`, `_find_enrich_deltas()`, `_find_filter_delta()`, `_find_rank()`, `_otm_pct()`
- **Test suite**: `tests/test_complex_option_selection.py` — 32/32 assertions
  - Steps 1–4: manual pipeline (expiry → strike → delta enrichment → validation)
  - Step 5: `select_option()` backward-compatibility round-trip
  - Step 6: `find_option()` compound criteria end-to-end

### Changed
- Updated module docstring in `option_selection.py` to document both APIs
- Improved inline comments and docstrings for all `find_option` helpers

### Documentation
- Updated `README.md` — highlights, project structure, `find_option()` usage table
- Updated `docs/ARCHITECTURE_PLAN.md` — option selection status, Phase 4 deliverables, architecture diagram
- Updated `docs/API_REFERENCE.md` — new `find_option()` reference section
- Updated `CHANGELOG.md` — this entry

---

## [0.4.0] - 2026-02-14

### Added - Strategy Framework (Phase 4)
- **Strategy framework** (`strategy.py` ~578 lines)
  - `TradingContext` — dependency injection container holding all services
  - `StrategyConfig` — declarative strategy definition (legs, entry/exit conditions, execution mode, concurrency, cooldown, dry-run)
  - `StrategyRunner` — tick-driven executor: entry checks → leg resolution → trade creation → lifecycle management
  - `build_context()` — factory wiring all services from config.py settings
  - **Entry condition factories**: `time_window()`, `weekday_filter()`, `min_available_margin_pct()`, `min_equity()`, `max_account_delta()`, `max_margin_utilization()`, `no_existing_position_in()`
  - **Dry-run mode**: `StrategyConfig(dry_run=True)` — fetches live prices, simulates execution without placing orders
- **LegSpec dataclass** (`option_selection.py`) — declares option_type, side, qty, strike/expiry criteria
- **resolve_legs()** (`option_selection.py`) — converts LegSpec list to TradeLeg list via market data queries
- **strategy_id tracking** (`trade_lifecycle.py`) — per-strategy trade identification
- **_get_orderbook_price()** (`trade_lifecycle.py`) — live orderbook pricing helper
- **get_trades_for_strategy() / active_trades_for_strategy()** (`trade_lifecycle.py`)
- **Test suites**:
  - `tests/test_strategy_framework.py` — 72/72 unit assertions (config, context, conditions, LegSpec, runner, dry-run, edge cases)
  - `tests/test_live_dry_run.py` — 27/27 integration assertions (11 dry-run + 16 micro-trade lifecycle)

### Changed
- **main.py** — completely rewritten: uses `build_context()` for DI, registers `StrategyRunner` instances on `PositionMonitor.on_update()`, signal handling for graceful shutdown
- **trade_execution.py** — `get_order_status()` now uses correct endpoint: `GET /open/option/order/singleQuery/v1?orderId={id}` (was path-based URL returning 404)
- **trade_execution.py** — `cancel_order()` sends orderId as `int()` per API spec
- **trade_lifecycle.py** — fill detection uses `fillQty` field (was `executedQty`), state 3 = CANCELED (was 4)

### Removed
- Moved 6 pre-strategy legacy files to `archive/`: `check_positions.py` (old), `test_smart_strangle.py` (old), and 4 other superseded scripts

---

## [0.3.0] - 2026-02-13

### Added - Smart Orderbook Execution (Phase 3)
- **Smart multi-leg orderbook execution** (`multileg_orderbook.py`)
  - Proportional chunking algorithm splits orders into configurable chunks
  - Continuous quoting with multiple strategies (top-of-book, mid, mark)
  - Automatic repricing based on market movement thresholds
  - Aggressive fallback with limit orders crossing the spread
  - Position-aware fill tracking for both opens and closes
  - Early termination when target fills reached between chunks
- **SmartExecConfig** - 12+ configurable parameters for fine-tuning execution
- **ChunkState** - State machine tracking chunk execution (QUOTING → FALLBACK → COMPLETED)
- **LegChunkState** - Per-leg tracking within chunks (filled_qty, remaining_qty, is_filled)
- **SmartExecResult** - Comprehensive execution summary with fills, costs, timings

### Changed
- **Position tracking algorithm** - Now uses `abs(current - starting)` instead of `max(0, current - starting)`
  - Critical fix enabling close detection (decreasing positions)
  - Without this, closes would return negative deltas clamped to 0
  - Algorithm would loop indefinitely thinking nothing filled
- **LifecycleManager integration** - Smart mode now supported for opening trades
  - `execution_mode="smart"` with optional `smart_config`
  - Closing via direct SmartOrderbookExecutor call (LifecycleManager smart close TBD)

### Fixed
- Close position detection in smart execution
- Fill tracking for both increasing and decreasing positions
- Early chunk termination logic

### Testing
- **test_smart_butterfly.py** - Full lifecycle test (open + wait + close)
  - 3-leg butterfly with different quantities (0.2/0.4/0.2)
  - Opening: 57.1s, 100% fills, 2 chunks
  - Closing: 65.4s, 100% fills, 2 chunks, complete position closure
- **close_butterfly_now.py** - Emergency position closer with trade_side awareness

### Documentation
- Updated `docs/ARCHITECTURE_PLAN.md` with Phase 3 details
- Updated `README.md` with smart execution highlights
- Added comprehensive inline comments to `multileg_orderbook.py`

---

## [0.2.0] - 2026-02-10

### Added - Position Monitoring & Trade Lifecycle (Phase 2)
- **PositionSnapshot** - Frozen dataclass for single position with Greeks, PnL, mark price
- **AccountSnapshot** - Frozen dataclass for account state (equity, margins, aggregated Greeks)
- **PositionMonitor** - Background polling with thread-safe snapshot access and callbacks
- **TradeLifecycle** - State machine managing trade lifecycle (PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED)
- **TradeLeg** - Individual leg tracking from intent through order, fill, and position
- **LifecycleManager** - Orchestrates trades with `tick()` callback pattern
- **Exit condition system** - Composable callables for exit logic
  - `profit_target(pct)` - Exit on structure PnL % of entry cost
  - `max_loss(pct)` - Exit on structure loss limit
  - `max_hold_hours(hours)` - Time-based exit
  - `account_delta_limit(threshold)` - Account-level Greek limit
  - `structure_delta_limit(threshold)` - Structure-level Greek limit
  - `leg_greek_limit(leg_idx, greek, op, value)` - Per-leg Greek threshold

### Changed
- Enhanced `account_manager.py` with position monitoring infrastructure
- `trade_lifecycle.py` supports both "limit" and "rfq" execution modes

### Documentation
- Created `docs/ARCHITECTURE_PLAN.md` Phase 2 documentation
- Added position monitoring and lifecycle examples

---

## [0.1.0] - 2026-02-09

### Added - RFQ Execution (Phase 1)
- **RFQ execution system** (`rfq.py`) for multi-leg block trades
  - `OptionLeg` - Dataclass for leg definition (instrument, side, qty)
  - `RFQQuote` - Quote from market maker with direction helpers
  - `RFQResult` - Execution result with cost, improvement metrics
  - `RFQExecutor` - Main executor with `execute(legs, action='buy'|'sell')`
- **Best-quote selection** - Automatically selects best quote from multiple market makers
- **Quote polling** - Configurable polling interval and max wait time
- **Minimum notional validation** - $50,000 minimum for RFQ trades

### Changed
- **auth.py** - Added `use_form_data` parameter for form-urlencoded content type
  - RFQ accept/cancel endpoints require this format
- **Symbol format** - Confirmed BTCUSD-{expiry}-{strike}-{C/P} format
- **Side parameters** - Using integers (1=BUY, 2=SELL) instead of strings

### Fixed
- RFQ quote interpretation (`MM SELL` = we buy, `MM BUY` = we sell)
- Content-Type handling for different API endpoints
- Quote direction logic in best-quote selection

### Documentation
- Created `docs/API_REFERENCE.md` with RFQ endpoint documentation
- Created `docs/ARCHITECTURE_PLAN.md` with full roadmap
- Added RFQ examples and integration tests

---

## [0.0.1] - 2026-02-08 (Initial)

### Added - Foundation
- Basic options trading functionality
- HMAC-SHA256 authentication (`auth.py`)
- Environment switching (testnet ↔ production) via `config.py`
- Market data retrieval (`market_data.py`)
- Option selection logic (`option_selection.py`)
- Basic order placement/cancellation (`trade_execution.py`)
- Scheduler-based execution (APScheduler in `main.py`)
- Config-driven strategy parameters
- Logging infrastructure

### Infrastructure
- Python 3.9+ compatibility
- Requirements.txt with core dependencies
- .env configuration support
- Basic project structure

---

## Version Comparison

| Version | Key Feature | Lines of Code | Test Coverage |
|---------|-------------|---------------|---------------|
| 0.4.0 | Strategy Framework | ~578 (strategy.py) + modifications | 72/72 unit + 27/27 integration |
| 0.3.0 | Smart Orderbook Execution | ~1000 (multileg_orderbook.py) | Butterfly lifecycle test |
| 0.2.0 | Position Monitoring & Lifecycle | ~800 (trade_lifecycle.py, account_manager.py) | Position monitor, lifecycle tests |
| 0.1.0 | RFQ Block Trades | ~800 (rfq.py) | RFQ integration tests |
| 0.0.1 | Foundation | ~500 (core modules) | Basic functionality |

---

## Migration Notes

### Upgrading to 0.4.0
- **main.py rewritten** — If you customised main.py, review the new version. It now uses `build_context()` + `StrategyRunner` instead of APScheduler
- **Strategy definition** — Replace old `config.py` strategy dicts with `StrategyConfig` + `LegSpec` objects
- **Entry conditions** — Use factory functions from `strategy.py` instead of hardcoded checks
- **Order status** — `get_order_status()` now uses the correct endpoint; no user action needed
- **Fill tracking** — Uses `fillQty` field and state 3 = CANCELED; no user action needed
- **Legacy files** — 6 scripts moved to `archive/`; import paths may need updating if referenced

### Upgrading to 0.3.0
- **LifecycleManager** now supports `execution_mode="smart"` with `smart_config` parameter
- **Position tracking** - No code changes required, but close detection now works correctly
- **Test files** - Moved to `tests/` folder (test_smart_butterfly.py, close_butterfly_now.py)

### Upgrading to 0.2.0
- **Exit conditions** - Replace old exit logic with new exit condition callables
- **Position tracking** - Use `PositionMonitor` instead of manual position queries
- **Trade management** - Use `LifecycleManager` instead of direct TradeExecutor calls

### Upgrading to 0.1.0
- **RFQ integration** - For large trades ($50k+), use RFQExecutor instead of direct orders
- **Authentication** - auth.py now supports both JSON and form-urlencoded content types
- **Symbol format** - Ensure using BTCUSD-{expiry}-{strike}-{C/P} format

---

## Upcoming Features

See [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) for the complete roadmap.

**Next up (Phase 5):**
- Multi-instrument support (futures, spot)
- Unified order interface across instruments
- Cross-instrument hedging

---

*For detailed technical documentation, see individual module docstrings and [docs/](docs/) folder.*
