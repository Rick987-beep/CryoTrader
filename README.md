# CoincallTrader

A strategy-driven options trading system for the [Coincall](https://www.coincall.com/) exchange.  
Strategies are declared as configuration — not coded as classes — and the framework handles entry checks, leg resolution, execution, lifecycle management, and exits automatically.

**Current version:** 0.9.1 — Streamlined Supervision

## Highlights

- **Declarative strategy framework**: Define _what_ to trade, _when_ to enter, _when_ to exit, and _how_ to execute — all via `StrategyConfig` ✅
- **Modular strategies**: Each strategy lives in `strategies/` as a standalone factory function ✅
- **Dependency injection**: `TradingContext` wires every service; strategies and tests receive the same container ✅
- **Entry conditions**: Composable factories — `time_window()`, `utc_time_window()`, `weekday_filter()`, `min_available_margin_pct()`, `min_equity()`, `max_account_delta()`, `max_margin_utilization()`, `no_existing_position_in()` ✅
- **Leg specifications**: `LegSpec` dataclass resolves strike/expiry criteria into concrete symbols at runtime ✅
- **Trade lifecycle**: Full open → manage → close state machine with automatic exit evaluation ✅
- **Exit conditions**: `profit_target()`, `max_loss()`, `max_hold_hours()`, `time_exit()`, `utc_datetime_exit()`, `account_delta_limit()`, `structure_delta_limit()`, `leg_greek_limit()` ✅
- **Three execution modes**: Limit orders (with LimitFillManager), RFQ block trades ($50 k+), and smart orderbook (chunked quoting with aggressive fallback) ✅
- **LimitFillManager**: Fill detection, configurable phased pricing (mark → mid → aggressive), requote management ✅
- **ExecutionPhase**: Declarative pricing phases for limit orders — duration, buffer, reprice interval per phase ✅
- **RFQParams**: Typed RFQ configuration (timeout, improvement threshold, fallback mode) ✅
- **Telegram notifications**: Trade opens/closes, daily account summary (07:00 UTC), strategy pause/resume/stop, critical errors ✅
- **Web dashboard**: Real-time browser UI (Flask + htmx) — account status, strategy controls, positions table, log tail, kill switch ✅
- **Kill switch**: Two-phase mark-price position closer — mark price (5 min) then aggressive ±10% (2 min), with Telegram progress ✅
- **Crash recovery**: Idempotent trade recovery from snapshot on every startup — verifies exchange positions, re-attaches exit conditions, all-or-nothing restore ✅
- **Position monitoring**: Background polling with live Greeks, PnL, account snapshots, and tick-driven strategy execution ✅
- **Multi-leg native**: Strangles, Iron Condors, Butterflies — any structure as one lifecycle ✅
- **HMAC-SHA256 authentication**: Secure API access via `auth.py` ✅
- **Phase 1 Hardening**: Request timeouts (30s), exponential backoff retries (1-2-4s), main loop error isolation ✅ 
- **Phase 2 Reliability**: Market data caching (30s TTL), trade state persistence (tick snapshots), health check logging (5min intervals, observability only) ✅

## Quick Start

### 1. Install dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure environment
Copy `.env.example` to `.env` and set your API keys:
```
TRADING_ENVIRONMENT=production   # or testnet

COINCALL_API_KEY_PROD=your_key
COINCALL_API_SECRET_PROD=your_secret
```

### 3. Define a strategy
Create a factory function in `strategies/` (see `strategies/blueprint_strangle.py` for a working example):

```python
# strategies/my_strategy.py
from strategy import StrategyConfig, max_hold_hours, min_available_margin_pct
from option_selection import strangle
from trade_execution import ExecutionParams

def my_strategy() -> StrategyConfig:
    return StrategyConfig(
        name="my_strategy",
        legs=strangle(qty=0.01, call_delta=0.15, put_delta=-0.15, dte="next", side=1),
        entry_conditions=[min_available_margin_pct(30)],
        exit_conditions=[max_hold_hours(1)],
        max_concurrent_trades=1,
        max_trades_per_day=2,
        cooldown_seconds=60,
        check_interval_seconds=30,
        metadata={
            "execution_params": ExecutionParams(
                fill_timeout_seconds=30.0,
                aggressive_buffer_pct=2.0,
                max_requote_rounds=10,
            ),
        },
    )
```

Then register it in `strategies/__init__.py` and add to the `STRATEGIES` list in `main.py`.

### 4. Run
```bash
python main.py
```

## Project Structure

```
CoincallTrader/
├── main.py                         # Entry point — loads strategies, wires context, starts monitor
├── strategies/
│   ├── __init__.py                 # Re-exports strategy factories
│   ├── blueprint_strangle.py       # Blueprint strangle — starting template for new strategies
│   ├── atm_straddle.py            # Daily ATM straddle with profit target + time exit
│   ├── reverse_iron_condor_live.py # Reverse iron condor live trading strategy
│   └── long_strangle_pnl_test.py   # Long strangle PnL monitoring test
├── strategy.py                     # StrategyConfig, StrategyRunner, entry/exit condition factories
├── config.py                       # Environment & global config (.env loading)
├── auth.py                         # HMAC-SHA256 API authentication
├── retry.py                        # @retry decorator with exponential backoff
├── market_data.py                  # Market data (option chains, orderbooks, BTC price)
├── option_selection.py             # LegSpec, resolve_legs(), select_option(), find_option()
├── trade_execution.py              # TradeExecutor, LimitFillManager, ExecutionParams, ExecutionPhase
├── trade_lifecycle.py              # TradeState machine, TradeLeg, LifecycleManager, RFQParams
├── multileg_orderbook.py           # SmartOrderbookExecutor — chunked multi-leg execution
├── rfq.py                          # RFQExecutor — block-trade execution ($50k+ notional)
├── account_manager.py              # AccountManager, PositionMonitor, AccountSnapshot
├── persistence.py                  # Trade history log (append-only JSONL)
├── position_closer.py              # Emergency two-phase position closer (kill switch)
├── health_check.py                 # Background health check logging (5-min intervals)
├── telegram_notifier.py            # Telegram Bot API notifications (trade alerts, daily summary)
├── dashboard.py                    # Web dashboard (Flask + htmx, daemon thread, password-protected)
├── templates/
│   ├── dashboard.html              # Main dashboard page (htmx auto-polling panels)
│   ├── login.html                  # Login page
│   ├── _account.html               # Account metrics fragment
│   ├── _strategies.html            # Strategy cards fragment
│   ├── _positions.html             # Positions table fragment
│   └── _logs.html                  # Log tail fragment
├── deployment/
│   ├── health_check.ps1            # Windows service health monitoring
│   └── monitor_dashboard.ps1       # Real-time status dashboard (PowerShell)
├── docs/
│   ├── ARCHITECTURE_PLAN.md        # Roadmap, phases, requirements
│   ├── API_REFERENCE.md            # Coincall exchange API endpoints & formats
│   └── MODULE_REFERENCE.md         # Internal module docs (strategies, lifecycle, execution)
├── tests/
│   ├── test_strategy_framework.py  # Unit tests — config, context, conditions
│   ├── test_strategy_layer.py      # Strategy layer integration tests
│   ├── test_atm_straddle.py        # ATM straddle strategy unit tests
│   ├── test_execution_timing.py    # ExecutionPhase, RFQParams, phased execution
│   └── test_dashboard.py           # Standalone dashboard test with mock data
├── logs/                           # Runtime logs (gitignored)
├── archive/                        # Legacy code (gitignored)
├── CHANGELOG.md
├── RELEASE_NOTES.md
├── PROJECT_CONTEXT.md
└── requirements.txt
```

## Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│  main.py                                             │
│  STRATEGIES list → build_context() → TradingContext  │
│  StrategyRunner.tick() registered on PositionMonitor │
└────────────────┬─────────────────────────────────────┘
                 │ on each tick
   ┌─────────────▼──────────────┐
   │  StrategyRunner            │
   │  • check entry conditions  │
   │  • resolve LegSpecs        │
   │  • create trade lifecycle  │
   │  • LifecycleManager.tick() │
   │    evaluates exit conds    │
   └─────┬─────────────┬───────┘
         │             │
   ┌─────▼─────┐ ┌────▼───────────┐
   │ option_   │ │ trade_         │
   │ selection │ │ lifecycle.py   │
   │ LegSpec → │ │ TradeState FSM │
   │ TradeLeg  │ │ exit conditions│
   │ find_     │ └──────┬─────────┘
   │ option()  │        │
   └───────────┘        │
         ┌──────────────┼──────────────┐
         │              │              │
   ┌─────▼─────┐ ┌─────▼─────┐ ┌─────▼──────────┐
   │ trade_    │ │ rfq.py    │ │ multileg_      │
   │ execution │ │ $50k+     │ │ orderbook.py   │
   │ Limit +   │ │ block     │ │ smart chunked  │
   │ FillMgr   │ │ trades    │ │ quoting        │
   └───────────┘ └───────────┘ └────────────────┘
```

## Configuration

### StrategyConfig fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | Unique strategy identifier |
| `legs` | `list[LegSpec]` | What to trade — option type, side, qty, strike/expiry criteria |
| `entry_conditions` | `list[EntryCondition]` | All must pass before opening |
| `exit_conditions` | `list[ExitCondition]` | Any triggers a close |
| `execution_mode` | `str` | `"auto"`, `"limit"`, `"rfq"`, or `"smart"` |
| `max_concurrent_trades` | `int` | Max simultaneous open trades |
| `max_trades_per_day` | `int` | Daily trade limit (0 = unlimited) |
| `cooldown_seconds` | `float` | Delay between new trades |
| `check_interval_seconds` | `float` | Throttle between entry checks |
| `on_trade_closed` | `Callable` | Optional callback when a trade closes |
| `execution_params` | `ExecutionParams` | Optional execution timing config (phased pricing, fill timeout) |
| `rfq_params` | `RFQParams` | Optional RFQ config (timeout, improvement threshold, fallback) |
| `metadata` | `dict` | Arbitrary context passed to lifecycle |

### ExecutionParams fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `fill_timeout_seconds` | `float` | `30.0` | Time before requoting (legacy mode) |
| `aggressive_buffer_pct` | `float` | `2.0` | How far past mid to price aggressively |
| `max_requote_rounds` | `int` | `10` | Max requote attempts before failure |
| `phases` | `list[ExecutionPhase]` | `None` | Optional phased pricing (overrides legacy mode) |

### ExecutionPhase fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `pricing` | `str` | `"aggressive"` | Pricing mode: `"aggressive"`, `"mid"`, `"top_of_book"`, `"mark"` |
| `duration_seconds` | `float` | `30.0` | How long to stay in this phase (min 10s) |
| `buffer_pct` | `float` | `2.0` | Buffer % for aggressive pricing |
| `reprice_interval` | `float` | `30.0` | Seconds between reprices (min 10s) |

### RFQParams fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timeout_seconds` | `float` | `60.0` | RFQ quote timeout |
| `min_improvement_pct` | `float` | `-999.0` | Minimum improvement vs orderbook (-999 = accept anything) |
| `fallback_mode` | `str` | `None` | Fallback if RFQ fails (e.g. `"limit"`) |

### LegSpec fields

| Field | Type | Description |
|-------|------|-------------|
| `option_type` | `str` | `"C"` or `"P"` |
| `side` | `int` | `1` = BUY, `2` = SELL |
| `qty` | `float` | Contract quantity |
| `strike_criteria` | `dict` | `{"type": "delta", "value": 0.25}`, `{"type": "closestStrike"}`, etc. |
| `expiry_criteria` | `dict` | `{"symbol": "28MAR26"}` or `{"dte": "next"}` |
| `underlying` | `str` | Default `"BTC"` |

### find_option() — Compound Selection

For strategies that need multiple simultaneous constraints, use `find_option()`:

```python
from option_selection import find_option

option = find_option(
    option_type="P",
    expiry={"min_days": 6, "max_days": 13, "target": "near"},
    strike={"below_atm": True, "min_distance_pct": 0.5},
    delta={"min": -0.45, "max": -0.15},
    rank_by="delta_mid",
)
```

### Entry condition factories

| Factory | Description |
|---------|-------------|
| `time_window(start_hour, end_hour)` | UTC hour window |
| `utc_time_window(start, end)` | UTC time window with `datetime.time` precision |
| `weekday_filter(days)` | e.g. `["mon", "tue", "wed", "thu"]` |
| `min_available_margin_pct(pct)` | Minimum free margin % |
| `min_equity(usd)` | Minimum account equity |
| `max_account_delta(limit)` | Account delta threshold |
| `max_margin_utilization(pct)` | IM/equity ceiling |
| `no_existing_position_in(symbols)` | Block if already positioned |

### Exit condition factories

| Factory | Description |
|---------|-------------|
| `profit_target(usd)` | Close when profit ≥ threshold |
| `max_loss(usd)` | Close when loss ≥ threshold |
| `max_hold_hours(hours)` | Close after time limit |
| `time_exit(hour, minute)` | Close at specific UTC time daily |
| `utc_datetime_exit(dt)` | Close at specific UTC datetime |
| `account_delta_limit(limit)` | Close if account delta exceeds limit |
| `structure_delta_limit(limit)` | Close if structure delta exceeds limit |
| `leg_greek_limit(greek, limit)` | Close if any leg Greek exceeds limit |

## Testing

```bash
# Unit tests
python -m pytest tests/test_strategy_framework.py -v

# Compound option selection (hits live API)
python3 tests/test_complex_option_selection.py
```

## Documentation

- **[Architecture Plan](docs/ARCHITECTURE_PLAN.md)** — Phases, requirements, and roadmap
- **[API Reference](docs/API_REFERENCE.md)** — Coincall exchange API endpoints and formats
- **[Module Reference](docs/MODULE_REFERENCE.md)** — Internal module documentation (strategies, lifecycle, execution)
- **[Changelog](CHANGELOG.md)** — Version history
- **[Release Notes](RELEASE_NOTES.md)** — Detailed release notes

## Roadmap

1. ✅ Foundation — auth, config, market data, option selection
2. ✅ RFQ execution — block trades with best-quote selection
3. ✅ Position monitoring — live Greeks, PnL, account snapshots
4. ✅ Trade lifecycle — open → manage → close state machine
5. ✅ Smart orderbook execution — chunked quoting with aggressive fallback
6. ✅ Strategy framework — declarative configs, entry/exit conditions, DI
7. ✅ **Architecture cleanup** — modular strategies, clean layering, dead code removal
8. ✅ **RFQ comparison fix** — correct orderbook side selection, unified improvement formula
9. ✅ **48-hour reliability** — timeouts, retries, persistence, health checks
10. ✅ **Configurable execution timing** — phased pricing, typed RFQ params
11. ✅ **Web dashboard** — real-time browser UI with strategy controls and kill switch
12. ✅ **Hardened operations** — crash recovery, kill switch (two-phase mark-price close), self-shutdown fix, Telegram enhancements
13. ⬜ Multi-instrument — futures, spot trading

## Disclaimer

⚠️ **Trading involves significant risk of loss.** This software is provided as-is, without warranty. Use at your own risk. Always test with small positions before scaling up.
