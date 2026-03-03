# CoincallTrader — Project Context for AI Agents

**Version:** 0.8.0  
**Last Updated:** 3 March 2026  
**Python:** 3.9+ (no 3.10+ syntax — use `Optional[X]`, not `X | None`)

Automated BTC/ETH options trading bot for the Coincall exchange.
Config-driven strategies, tick-based execution, zero-subclassing design.

For deeper detail, see:
- [docs/MODULE_REFERENCE.md](docs/MODULE_REFERENCE.md) — every class, dataclass, and factory function
- [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) — phased roadmap and requirements
- [docs/API_REFERENCE.md](docs/API_REFERENCE.md) — Coincall REST API endpoints

---

## 1  Mental Model

```
PositionMonitor (10 s poll, daemon thread)
  │
  ├─► LifecycleManager.tick(snapshot)   — advances every active trade's state machine
  │
  └─► StrategyRunner.tick(snapshot)     — per strategy:
        1. check_closed_trades()        — fire on_trade_closed callback
        2. entry gate (all conditions)  — time, weekday, margin, equity, delta
        3. resolve_legs()               — LegSpec → concrete TradeLeg via option_selection
        4. lifecycle_manager.create()   — creates TradeLifecycle with exit conditions
        5. lifecycle_manager.open()     — routes to limit / rfq / smart executor
```

Everything is driven by the PositionMonitor callback — no extra threads or event
loops for strategy logic.

### Trade State Machine

```
PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED
                                                           └─► FAILED
```

Exit conditions are evaluated every tick while OPEN.  Any single exit returning
True triggers PENDING_CLOSE.

---

## 2  Key Abstractions

### TradingContext (DI container — `strategy.py`)
```
auth              CoincallAuth         — HMAC-signed requests
market_data       MarketData           — option chains, orderbooks (30 s LRU cache)
executor          TradeExecutor        — single-leg limit orders, LimitFillManager
rfq_executor      RFQExecutor          — atomic multi-leg RFQ ($50k+ notional)
smart_executor    SmartOrderbookExecutor — chunked multi-leg with repricing
account_manager   AccountManager       — balance, positions, margin queries
position_monitor  PositionMonitor      — background poller → callbacks
lifecycle_manager LifecycleManager     — trade state machine
persistence       TradeStatePersistence (optional) — crash recovery JSON
notifier          TelegramNotifier     (optional) — fire-and-forget alerts
```
Created by `build_context()`.  For tests, replace any field with a mock.

### StrategyConfig (dataclass — `strategy.py`)
Declares *what*, *when*, *how*:
- `legs: List[LegSpec]` — resolved to concrete symbols at trade time
- `entry_conditions: List[EntryCondition]` — all must pass (AND)
- `exit_conditions: List[ExitCondition]` — any triggers close (OR)
- `execution_mode: "limit" | "rfq" | "smart" | "auto"`
- `execution_params: ExecutionParams` — phased pricing for limit mode
- `rfq_params: RFQParams` — timeout, improvement threshold, fallback
- `max_concurrent_trades`, `max_trades_per_day`, `cooldown_seconds`
- `on_trade_closed: Callable` — callback with (trade, snapshot)

### AccountSnapshot / PositionSnapshot (frozen dataclasses — `account_manager.py`)
Thread-safe, immutable snapshots.  `AccountSnapshot` carries: equity,
available_margin, margin_utilization, net_delta/gamma/theta/vega,
and a tuple of `PositionSnapshot` objects (each with symbol, qty, side,
entry/mark price, UPnL, ROI, per-position Greeks).

### Condition Factories
**Entry** (`strategy.py`): `time_window`, `utc_time_window`, `weekday_filter`,
`min_available_margin_pct`, `min_equity`, `max_account_delta`,
`max_margin_utilization`, `no_existing_position_in`

**Exit** (`strategy.py` + `trade_lifecycle.py`): `profit_target`, `max_loss`,
`max_hold_hours`, `time_exit`, `utc_datetime_exit`, `account_delta_limit`,
`structure_delta_limit`, `leg_greek_limit`

### LegSpec → TradeLeg Resolution (`option_selection.py`)
`LegSpec(option_type, side, qty, strike_criteria, expiry_criteria)` is resolved
at trade time via `resolve_legs()`.  Strike criteria: `delta`, `closestStrike`,
`spotdistance%`.  Expiry criteria: `{"symbol": "28MAR26"}`, `{"dte": 0}`,
min/max range.  `find_option()` provides compound filtering + ranking.

Structure templates: `straddle(qty, dte, side)`, `strangle(qty, call_delta, put_delta, dte, side)`.

---

## 3  Execution Pipeline

Three modes, routable via `execution_mode` or auto-selected by notional size:

| Mode | Module | When | How |
|------|--------|------|-----|
| `limit` | `trade_execution.py` | Default / small trades | Per-leg limit orders via `LimitFillManager`. Supports phased pricing: `ExecutionPhase(pricing, duration, buffer_pct, reprice_interval)` — walks through mark → mid → aggressive. |
| `rfq` | `rfq.py` | $50k+ notional | Atomic multi-leg RFQ. Best-quote selection with orderbook comparison. `RFQParams` configures timeout, min improvement, fallback mode. |
| `smart` | `multileg_orderbook.py` | $10k–$50k or multi-leg | Chunked execution with continuous quoting. `SmartExecConfig` configures chunks, time per chunk, pricing strategy, repricing. |
| `auto` | `trade_lifecycle.py` | Default mode | Routes by notional: smart ≥ $10k, rfq ≥ $50k, else limit. |

---

## 4  Threading Model

All daemon threads — if the main process dies, everything dies.

| Thread | Source | Interval |
|--------|--------|----------|
| Main | `main.py` — sleep loop, persistence saves, auto-shutdown detection | 10 s |
| PositionMonitor | `account_manager.py` — polls positions → fires callbacks | 10 s |
| HealthChecker | `health_check.py` — logs status, triggers Telegram daily summary | 5 min |
| Dashboard | `dashboard.py` — Flask + htmx web server | continuous |

---

## 5  Module Map

### Core
| File | Purpose |
|------|---------|
| `main.py` | Entry point. Wires context, registers strategies, starts services, runs main loop. |
| `strategy.py` | `TradingContext`, `build_context()`, `StrategyConfig`, `StrategyRunner`, entry/exit condition factories. |
| `config.py` | `TRADING_ENVIRONMENT`, `API_KEY`, `API_SECRET`, `BASE_URL`. Reads `.env` via dotenv. |
| `auth.py` | `CoincallAuth` — HMAC-SHA256 signing, 30 s request timeout. |
| `retry.py` | `@retry` decorator — exponential backoff (1 → 2 → 4 s), only for ConnectionError/Timeout. |

### Market Data & Selection
| File | Purpose |
|------|---------|
| `market_data.py` | `MarketData` — option chains, orderbooks, Greeks. 30 s LRU cache (100 entries). |
| `option_selection.py` | `LegSpec`, `resolve_legs()`, `find_option()`, `straddle()`, `strangle()`. |

### Execution
| File | Purpose |
|------|---------|
| `trade_execution.py` | `TradeExecutor`, `LimitFillManager`, `ExecutionParams`, `ExecutionPhase`. |
| `rfq.py` | `RFQExecutor`, `OptionLeg`, `RFQResult`. Best-quote logic, orderbook comparison. |
| `multileg_orderbook.py` | `SmartOrderbookExecutor`, `SmartExecConfig`. Chunked multi-leg with quoting + aggressive fallback. |
| `trade_lifecycle.py` | `TradeState`, `TradeLeg`, `TradeLifecycle`, `LifecycleManager`, `RFQParams`. State machine, PnL tracking, position scaling. |

### Account & Monitoring
| File | Purpose |
|------|---------|
| `account_manager.py` | `AccountManager`, `AccountSnapshot`, `PositionSnapshot`, `PositionMonitor`. |
| `persistence.py` | `TradeStatePersistence` — `trade_state.json` (active), `trade_history.jsonl` (completed). |
| `health_check.py` | `HealthChecker` — logs every 5 min, escalates on high margin/low equity, triggers daily Telegram summary. |

### Notifications & UI
| File | Purpose |
|------|---------|
| `telegram_notifier.py` | `TelegramNotifier` — startup/shutdown, trade open/close, daily summary, errors. Fire-and-forget, rate-limited. |
| `dashboard.py` | Flask + htmx web dashboard. Session auth, daemon thread. Routes: account, strategies, positions, logs, pause/resume/stop, kill switch. |
| `templates/` | 6 HTML files: `dashboard.html`, `login.html`, `_account.html`, `_strategies.html`, `_positions.html`, `_logs.html`. |

### Strategies
| File | Purpose |
|------|---------|
| `strategies/__init__.py` | Imports all strategy factory functions. |
| `strategies/blueprint_strangle.py` | Template strangle — starting point for new strategies. Active in `main.py`. |
| `strategies/atm_straddle.py` | Daily ATM straddle with profit target + time exit. |
| `strategies/reverse_iron_condor_live.py` | Daily 1DTE reverse iron condor via RFQ. |
| `strategies/long_strangle_pnl_test.py` | PnL monitoring test (2 h hold). |

---

## 6  How to Write a New Strategy

A strategy is a **function** that returns a `StrategyConfig`.  No subclassing.

```python
# strategies/my_strategy.py
from strategy import StrategyConfig, time_window, min_available_margin_pct
from option_selection import strangle
from trade_lifecycle import profit_target, max_hold_hours

def my_strategy() -> StrategyConfig:
    return StrategyConfig(
        name="my_strategy",
        legs=strangle(qty=0.01, call_delta=0.25, put_delta=-0.25, dte=1, side=2),
        entry_conditions=[time_window(8, 20), min_available_margin_pct(50)],
        exit_conditions=[profit_target(50), max_hold_hours(24)],
        max_concurrent_trades=1,
    )
```

Then register in `strategies/__init__.py` and add to `STRATEGIES` list in `main.py`.

---

## 7  Environment & Config

```bash
# .env
TRADING_ENVIRONMENT=testnet          # or 'production'
COINCALL_API_KEY_TEST=...
COINCALL_API_SECRET_TEST=...
COINCALL_API_KEY_PROD=...
COINCALL_API_SECRET_PROD=...
TELEGRAM_BOT_TOKEN=...              # optional — blank disables
TELEGRAM_CHAT_ID=...                # optional
DASHBOARD_PASSWORD=...              # optional — blank disables dashboard
DASHBOARD_PORT=8080                 # optional, default 8080
```

- Testnet: `https://betaapi.coincall.com`
- Production: `https://api.coincall.com`
- `config.py` reads `.env` via `python-dotenv` and exposes `API_KEY`, `API_SECRET`, `BASE_URL`, `ENVIRONMENT`.

---

## 8  Resilience

All hardening is built-in — no configuration needed:

- **Request timeouts**: 30 s on every API call (`auth.py`)
- **@retry**: Exponential backoff for ConnectionError/Timeout only (`retry.py`)
- **Error isolation**: Main loop tolerates up to 10 consecutive errors, then exits and notifies via Telegram (`main.py`)
- **Market data cache**: 30 s TTL, 100-entry LRU (`market_data.py`)
- **Trade persistence**: Active trades saved to JSON every 60 s; completed trades appended to JSONL (`persistence.py`)
- **Crash recovery**: PositionMonitor detects live positions on restart; `max_trades_per_day` prevents duplicates

---

## 9  Deployment

- **Dev**: macOS, `.venv`, testnet
- **Prod**: Windows Server 2022 VPS, NSSM service (`CoincallTrader`), auto-restart
- **Workflow**: develop locally → testnet → commit → push → pull on VPS → restart service
- **Deployment docs**: [deployment/WINDOWS_DEPLOYMENT.md](deployment/WINDOWS_DEPLOYMENT.md)

---

## 10  Coding Conventions

- **Dataclasses everywhere** — `StrategyConfig`, `AccountSnapshot`, `PositionSnapshot`, `RFQParams`, `ExecutionParams`, `ExecutionPhase`, `TradeLeg`, `SmartExecConfig` are all `@dataclass`
- **Frozen dataclasses** for thread-safe snapshots (`AccountSnapshot`, `PositionSnapshot`)
- **Factory functions** for conditions — return callables, not classes
- **Optional services** use `Optional[X] = None` on `TradingContext` (persistence, notifier)
- **Fire-and-forget** for Telegram — every send wrapped in try/except, never crashes the bot
- **Logging** via `logging.getLogger(__name__)` in every module
- **No global mutable state** — everything flows through `TradingContext`

---

## 11  Documentation Index

| Document | Audience | Content |
|----------|----------|---------|
| [README.md](README.md) | Humans | Overview, quickstart, structure |
| [CHANGELOG.md](CHANGELOG.md) | Humans | Version-by-version changes |
| [RELEASE_NOTES.md](RELEASE_NOTES.md) | Humans | Latest release highlights |
| [docs/MODULE_REFERENCE.md](docs/MODULE_REFERENCE.md) | AI + Humans | Every class, method, dataclass, factory function with signatures and tables |
| [docs/ARCHITECTURE_PLAN.md](docs/ARCHITECTURE_PLAN.md) | AI + Humans | Phased roadmap, requirements, design decisions |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | AI + Humans | Coincall exchange REST API endpoints and fields |
| [deployment/WINDOWS_DEPLOYMENT.md](deployment/WINDOWS_DEPLOYMENT.md) | Ops | VPS setup, NSSM, PowerShell scripts |
