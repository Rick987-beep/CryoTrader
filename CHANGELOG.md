# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.15.1] - 2026-04-19

### Added — Execution Layer Refactoring Plan

- **`docs/upgrades/execution-layer-refactoring.md`** — Comprehensive plan for extracting the execution layer into a typed `execution/` package; reviewed and updated: denomination consistency (all arithmetic in native currency, single USD conversion at end), `Price(amount, currency)` as the universal value type, `currency: Currency` field on `TradeLifecycle`, `realized_pnl` as `Optional[Price]` with separate `realized_pnl_usd`, dropped `FillFees` in favour of `Price`, 5-phase implementation plan with ~35 test cases per phase

### Added — Backtester: Batman Calendar Strategy

- **`backtester/strategies/batman_calendar.py`** (`BatmanCalendar`) — New calendar spread strategy registered in CLI as `batman_calendar`

### Added — Backtester: Short Strangle Offset Strategy

- **`backtester/strategies/short_strangle_offset.py`** (`ShortStrangleOffset`) — Replaces `ShortStraddleStrangle` and `ShortStrangleDelta` with a unified offset-based strangle; registered as `short_straddle`

### Changed — Backtester: Timestamped Report Filenames

- **`backtester/run.py`** — Report output filenames now include a UTC timestamp (`YYYYMMDD_HHMMSS`) to prevent overwriting previous runs

### Removed — Backtester: Superseded Strategies

- **`backtester/strategies/deltaswipswap1m.py`** — Deleted; superseded by `deltaswipswap`
- **`backtester/strategies/short_straddle_strangle.py`** — Deleted; superseded by `short_strangle_offset`
- **`backtester/strategies/short_strangle_delta.py`** — Deleted; superseded by `short_strangle_offset`

### Fixed

- **`.env`** — Deribit testnet credential env-var names corrected from `_TESTNET` to `_TEST` suffix to match `config.py` expectations

## [1.15.0] - 2026-04-17

### Changed — Project Renamed: CoincallTrader → CryoTrader

- **All files** — Project renamed from CoincallTrader to CryoTrader throughout: module docstrings, comments, deployment scripts, service files, templates, docs, agent definitions, and `requirements.txt` header; GitHub repository renamed to match
- **`.copilot-instructions.md`** — Updated project name and description

### Added — Backtester: Walk-Forward Optimisation

- **`backtester/walk_forward.py`** — `run_walk_forward()` produces a `WFOResult` with per-window `WFOWindow` records; each window slices IS/OOS periods, runs the full `PARAM_GRID` on IS, selects the best-scoring combo, then freezes that combo for an OOS evaluation; reports `oos_win_rate`, `oos_total_pnl`, `oos_avg_sharpe`, and a stitched daily OOS equity curve across all windows
- **`backtester/run.py`** — `--wfo` flag triggers walk-forward validation and appends a WFO section to the HTML report; `--is-days`, `--oos-days`, `--step-days` control window sizes (defaults: 45/15/15)

### Added — Backtester: Experiment System

- **`backtester/experiment.py`** — `Experiment` dataclass loaded from TOML; `build_sensitivity_grid()` perturbs named parameters around best-known values using `DeviationSpec` (type: `pct`, `abs`, or `fixed`); WFO window params (`wfo_is_days`, `wfo_oos_days`, `wfo_step_days`) also stored per experiment
- **`backtester/experiments/delta_strangle_tp_v1.toml`** — First experiment definition: sensitivity grid + WFO config for `ShortStrangleDeltaTp`
- **`backtester/run.py`** — `--experiment <name>` loads a TOML experiment; `--mode sensitivity` builds the sensitivity grid (forces `--robustness`); `--mode wfo` uses experiment WFO windows

### Added — Backtester: Statistical Robustness

- **`backtester/robustness.py`** — `deflated_sharpe_ratio()` implements Bailey & López de Prado (2014) DSR: corrects the observed best-combo Sharpe for the number of tested combinations and for non-normality (skew + kurtosis) of trade returns; DSR ≥ 0.95 indicates strong evidence of edge; `_robustness_stats()` computes grid-wide PnL distribution and per-parameter sensitivity stats
- **`requirements.txt`** — Added `scipy>=1.11` (required for DSR normal CDF/PPF)

### Added — Backtester: Chart Module

- **`backtester/reporting_charts.py`** — Pure SVG chart generators extracted from `reporting_v2.py`: `equity_chart_svg`, `fan_chart_svg`, `histogram_svg`, `marginal_bar_chart_svg`, `sparkline_svg`; all functions are stateless and return self-contained SVG strings with no external dependencies
- **`backtester/reporting_v2.py`** — Now imports all chart functions from `reporting_charts.py`; report generation logic unchanged

### Added — Backtester: Weekend Strangle Strategy

- **`backtester/strategies/short_strangle_weekend.py`** (`ShortStrangleWeekend`) — Short N-DTE delta-selected strangle opened only on weekend days; `open_days` param selects `"saturday"`, `"sunday"`, or `"both"`; all other behaviour (delta selection, SL, TP, max-hold, expiry settlement) mirrors `ShortStrangleDeltaTp`; registered in CLI as `weekend_strangle`

### Changed — Backtester: Short Strangle Weekly Cap

- **`backtester/strategies/short_strangle_weekly_cap.py`** — Added `leg_mode` parameter: `"strangle"` (default), `"put"`, or `"call"` — allows capacity-managed single-leg selling; updated `PARAM_GRID` for broader delta/SL/TP/hold sweep focused on put-sell; extended `DATE_RANGE` to `2025-12-16 – 2026-04-15`

### Fixed — LimitFillManager Grace Tick

- **`trade_execution.py`** — `LimitFillManager.check()` now parks in `"pending"` for one extra tick when all phases exhaust before immediately returning `"failed"`; on the grace tick it does a final fresh poll; protects against exchange fill-reporting lag at the phase boundary where a fill arrives just after the timeout decision; field `_grace_exhausted` tracks whether the grace has been consumed

### Changed — `put_sell_80dte` Mid-Price SL/TP

- **`strategies/put_sell_80dte.py`** — Stop-loss and take-profit evaluation switched from fair-price to mid-price `(bid + ask) / 2`; fair-price model description removed from module docstring; behaviour is otherwise identical

### Removed — Legacy Tardis Ingest Module

- **`backtester/ingest/tardis/`** — Deleted (`chain.py`, `download.py`, `extract.py`, `fetch.py`, `quality_check.py`, `_validate.py`, `run_fetch.sh`); superseded by `backtester/ingest/bulkdownloadTardis/` (added in v1.13.0)
- **`backtester/ingest/snapshot_builder.py`** — Removed from ingest subpackage; snapshot building is handled by `backtester/ingest/tickrecorder/snapshotter.py`

## [1.14.0] - 2026-04-15

### Added — Structured Logging Infrastructure

- **`logging_setup.py`** — New module extracted from `main.py`; initialises three structured JSONL tracks: `ct.health → logs/health.jsonl`, `ct.strategy → logs/strategy.jsonl`, `ct.execution → logs/execution.jsonl`; each uses a `TimedRotatingFileHandler` with independent retention; root logger continues writing human-readable text to `trading.log`
- **`lifecycle_engine.py`** — Emits structured `ct.strategy` JSONL events for every trade lifecycle transition: `TRADE_OPENING`, `TRADE_OPENED`, `TRADE_OPEN_FAILED`, `TRADE_CLOSED`, `EXIT_TRIGGERED`, `TRADE_CANCELLED`, `RECONCILE_WARN`
- **`order_manager.py`** — Emits structured `ct.execution` JSONL events for `ORDER_PLACED`, `ORDER_REQUOTED`, `ORDER_FILLED`, `ORDER_CANCELLED`
- **`health_check.py`** — Health checker now emits a single structured `ct.health` record per cycle (JSON dict with equity, margin, BTC price, uptime); WARNING escalations go to the root logger instead of being embedded in the health record
- **`dashboard.py`** — Added `_WarningBridgeHandler`: attaches to root logger and forwards `WARNING+` records into the dashboard log ring buffer alongside `ct.strategy` `INFO+` events; fixes gap where exchange errors were invisible in the dashboard log tail
- **`main.py`** — Logging initialisation delegated to `logging_setup.setup_logging()`; emits `DEPLOY_STARTED` event to `ct.strategy` on startup

### Added — Live Strategy: Short Strangle Delta TP

- **`strategies/short_strangle_delta_tp.py`** — New live strategy extending `ShortStrangleDelta` with: (1) take-profit exit when combined ask cost falls enough that `(premium − ask) / premium ≥ TAKE_PROFIT_PCT`; (2) `min_otm_pct` minimum OTM distance guard that pushes delta-selected strikes further OTM when they are too close to ATM

### Added — Option Selection: min_otm_pct Guard

- **`option_selection.py`** — Delta selection now respects an optional `min_otm_pct` strike criterion; if the delta-selected strike is closer to ATM than the configured percentage, it is pushed to the nearest qualifying OTM strike; wired into the `strangle()` helper

### Fixed

- **`slot_config.py`** — Deribit `generate_env()` now always writes the canonical `DERIBIT_CLIENT_ID_PROD` / `DERIBIT_CLIENT_SECRET_PROD` names (or `_TEST` for testnet) regardless of the named account in `accounts.toml`; previously wrote arbitrary env-var names that `config.py` did not read

### Changed

- **`exchanges/deribit/market_data.py`** — Added clarifying comments on all three orderbook fields (`bids`/`asks`, `mark`, `_mark_btc`, `_index_price`) explaining BTC vs USD denomination and when each should be used

### Added — Backtester Improvements

- **`backtester/reporting_v2.py`** — Robustness section now renders a PnL histogram SVG (`_histogram_svg`) showing the distribution of all parameter-combo results with a vertical marker at the live combo's PnL; `_select_pairs` refactored to read pre-ranked pairs from `GridResult.heatmap_pairs` (computed once in `results.py`) instead of re-scoring on every report render
- **`backtester/config.toml`** — `options_parquet` and `spot_parquet` now accept a directory path; `MarketReplay` loads all per-day parquet files (`options_YYYY-MM-DD.parquet` / `spot_YYYY-MM-DD.parquet`) from that directory, as produced by the Tardis bulk-download pipeline
- **`backtester/strategies/short_strangle_delta_tp.py`** — Backtester variant of `ShortStrangleDeltaTP` with configurable `take_profit_pct` and `min_otm_pct`

## [1.13.0] - 2026-04-12

### Added — Put Sell 80 DTE Live Strategy

- **`strategies/put_sell_80dte.py`** — New live strategy: sells one BTC OTM put per day near -0.15δ targeting the nearest monthly expiry ≥ 80 DTE (±15 day window); entry 13:00–14:00 UTC; TP at 95% premium captured; SL at 250% loss (price = 3.5× fill); EMA-20 filter blocks entry in downtrend; up to 90 concurrent open trades (monthly expiry clustering); phased limit execution (fair → bid+33% spread → bid)
- **`slots/slot-01.toml`** — Reconfigured slot 01 for `put_sell_80dte` (was `daily_put_sell`): qty=0.1, delta=-0.15, dte=80, entry 13–14 UTC, SL=250%, TP=95%, max_concurrent=90

### Added — Backtester: New Strategies

- **`backtester/strategies/deltaswipswap.py`** — Long straddle/strangle with dynamic delta hedging via BTC-PERPETUAL (gamma scalping); opens ATM straddle or OTM strangle, neutralises delta at entry via a perp position, then re-hedges whenever |net portfolio delta| ≥ configurable threshold; closed at `close_hour` UTC
- **`backtester/strategies/deltaswipswap1m.py`** — 1-minute candle variant of `deltaswipswap` for higher-frequency hedge testing
- **`backtester/strategies/short_strangle_weekly_cap.py`** — Capacity-managed weekly short strangle (extends `short_strangle_weekly_tp`): caps simultaneous open positions via `target_max_open` and limits `max_daily_new` new openings per day; exits per-position via TP, SL, or `max_hold_days`
- **`backtester/strategies/short_strangle_weekly_tp.py`** — Weekly-DTE short strangle with configurable take-profit and stop-loss

### Added — Backtester: GridResult and Results Module

- **`backtester/results.py`** — New `GridResult` class: self-contained container for all engine output; runs vectorised stats (Sharpe, Sortino, Calmar, Omega, Ulcer Index, monthly consistency, max drawdown), percentile-rank combo scoring (8-metric weighted formula from `config.toml`), and equity curve computation (top-N combos only); `reporting_v2.py` now reads exclusively from `GridResult` attributes

### Added — Backtester: Bulk Tardis Ingest Tooling

- **`backtester/ingest/bulkdownloadTardis/`** — Tardis bulk-download pipeline: `bulk_fetch.py` downloads compressed `.tar.gz` day-files from `tardis.dev/datasets/`, `stream_extract.py` streams and decodes each file into a normalised parquet per day, `clean.py` validates and repairs gaps, `fixup_midnight.py` fixes midnight-boundary timestamp issues; `run_bulk.sh` orchestrates a full date-range download

### Added — Backtester: Analysis and Pricing

- **`backtester/analysis/risk_worst_case.py`** — Worst-case scenario analysis for backtester output
- **`backtester/pricing.py`** — `deribit_perp_fee()` (0.05% taker fee), `bs_call_delta()`, `bs_put_delta()` (Black-Scholes δ at r=0) for use by delta-hedging strategies

### Added — Deployment and Agents

- **`deployment/ssh-server.sh`** — Convenience SSH wrapper that reads `servers.toml` for connection details
- **`.github/agents/production-health-check.agent.md`** — Copilot agent definition for automated production health checks
- **`docs/upgrades/`** — Upgrade guides: backtester scoring/equity upgrade, exchange trade log integration, logging upgrade

### Changed

- **`strategies/short_strangle_delta.py`** — Added `WEEKEND_FILTER` param (default on): blocks new opens on Saturday and Sunday; uses `weekday_filter(["mon","tue","wed","thu","fri"])` entry condition
- **`deployment/deploy-slot.sh`** — Deploy host now read from `servers.toml` `[prod]` section (`user`, `ip`, `base_path`) instead of `.deploy.slots.env`; SSH key resolution falls back: `.env` → `.deploy.slots.env` (legacy)
- **`backtester/engine.py`** — Added `run_grid_full()` entry point returning `(df, keys, nav_daily_df, final_nav_df)`; NAV tracking uses reprice cache (`pos._last_reprice_usd` set by `strategy_base._reprice_legs`) to avoid redundant double-repricing per tick
- **`backtester/market_replay.py`** — Significant rework of replay internals
- **`backtester/reporting_v2.py`** — Reporting now reads exclusively from `GridResult`; no longer calls engine functions directly
- **`backtester/strategy_base.py`** — `_reprice_legs` caches result in `pos._last_reprice_usd` for engine NAV accounting
- **`backtester/config.py`** / **`backtester/config.toml`** — Updated for new scoring/fees/perp config
- **`.gitignore`** — Added exclusions for `backtester/ingest/bulkdownloadTardis/data/`, `*.parquet`, and other large data directories

### Removed

- **`backtester/BACKTEST_V2_PLAN.md`** — Stale planning document (work is complete)
- **`backtester/ingest/tardis/BULK_DOWNLOAD_PLAN.md`** — Superseded by `bulkdownloadTardis/` implementation
- **`docs/MIGRATION_PLAN_DERIBIT.md`** — Stale migration plan (migration complete)

## [1.12.0] - 2026-04-03

### Added — Short Strangle Delta Strategy

- **`strategies/short_strangle_delta.py`** — New live strategy: sells an OTM BTC strangle on the Deribit expiry N calendar days ahead; legs selected by target absolute delta (e.g. `delta=0.15` → nearest strike to ±15Δ) rather than a fixed USD offset from spot; configurable via `qty`, `dte`, `delta`, `entry_hour`, `stop_loss_pct`, `max_hold_hours`; two-phase open and close execution (fair price → aggressive bid/ask re-price after 30s); `MAX_CONCURRENT = DTE + 1` to allow day-overlap
- **`tests/test_short_strangle_delta_tp.py`** — Unit tests for the delta strangle take-profit logic
- **`tests/manual/smoke_short_straddle.py`** — Manual smoke test for the short straddle/strangle strategy

### Changed

- **`slots/slot-02.toml`** — Reconfigured slot 02 for `short_strangle_delta` (dte=2, delta=0.15, entry 12:00 UTC, SL=300%, max_hold disabled); previous short straddle/strangle config removed
- **`strategies/short_straddle_strangle.py`** — Minor comment fix: "backtester2" → "backtester"

### Changed — Backtester Restructure (`backtester2/` → `backtester/`)

- **`backtester/`** — Complete reorganisation of `backtester2/` into a cleaner package layout: tick recorder and ingest tooling moved under `backtester/ingest/` (`tickrecorder/`, `tardis/`, `snapshot_builder.py`); engine, strategy, and reporting files live directly under `backtester/`; new `backtester/strategies/short_strangle_delta.py` and `short_strangle_delta_tp.py` for backtesting the new strategy
- **`deployment/ct-recorder.service`** — Updated `ExecStart` module path: `backtester2.tickrecorder.recorder` → `backtester.ingest.tickrecorder.recorder`
- **`deployment/rsync-exclude-recorder.txt`** — Exclusion paths updated for new `backtester/ingest/` layout; removed stale `backtester2/` entries
- **`deployment/rsync-exclude-slot.txt`** — `backtester2/` exclusion updated to `backtester/`

## [1.11.0] - 2026-03-31

### Added — Short Straddle / Strangle Strategy

- **`strategies/short_straddle_strangle.py`** — New 1DTE BTC short volatility strategy: sells an ATM straddle (OFFSET=0) or OTM strangle (OFFSET>0) on the nearest available Deribit expiry; configurable via `qty`, `offset`, `entry_hour`, `stop_loss_pct`, and `max_hold_hours`
- **`option_selection.py`** — `strangle_by_offset()`: selects OTM call and put legs by USD distance from spot (e.g. OFFSET=1000 → nearest strike to spot±$1000); offset=0 produces an ATM straddle
- **`tests/test_short_straddle_strangle.py`** — Pure unit tests for the new strategy and `strangle_by_offset()`
- **`slots/slot-02.toml`** — Slot 02 configured for Short Straddle/Strangle (Deribit, offset=$1500, entry 12:00 UTC, SL=250%, max_hold=16h)

### Changed

- **`main.py`** — Graceful shutdown now calls `r.disable()` instead of `r.stop()` to prevent a race where the PositionMonitor background thread overwrites the persisted OPEN state with PENDING_CLOSE, causing an immediate close on the next restart
- **`market_data.py`** — `get_option_market_data()` now routes through the active exchange adapter instead of the legacy Coincall-only path; returns `bid`, `ask`, `mark_price`, `implied_volatility`
- **`ema_filter.py`** — `is_btc_above_ema20()` now uses `>=` (at or above) instead of `>`; price source documented as Binance BTCUSDT daily close (already fetched for EMA computation); log message updated to show "entry allowed / blocked"
- **`strategies/daily_put_sell.py`** — Entry filter updated to `ema20_filter()` (renamed from `below_ema20_filter()`)
- **`strategies/__init__.py`** — Cleared all static imports; module is now intentionally empty with a docstring explaining the dynamic import approach used in production (prevents cross-strategy import side-effects)
- **`hub/templates/_hub_recorder.html`** — Recorder health card now shows a "Missed Today" counter (`gaps_today`)
- **`tests/conftest.py`** — Added `_mute_telegram` autouse fixture: replaces the Telegram singleton with a no-op instance for all fast tests to prevent accidental live sends

### Changed — Backtester2

- **`backtester2/config.py`** — Added `ScoringConfig` dataclass (`min_trades`, `w_sharpe`, `w_pnl`, `w_max_dd`, `w_dd_days`, `w_profit_factor`) and config loader
- **`backtester2/config.toml`** — Added `[scoring]` section with default weights (Sharpe 30%, PnL 25%, max-DD 20%, DD-days 15%, profit-factor 10%)
- **`backtester2/reporting_v2.py`** — Added `_score_combos()`: percentile-ranked composite scoring across eligible combos; equity chart now starts at Day 0 / capital baseline, adds SVG clip path to prevent overflow, x-axis labels renumbered from 0
- **`backtester2/strategies/straddle_strangle.py`** — Significant refactor to align with upstream `strangle_by_offset()` selection logic
- **`backtester2/tickrecorder/recorder.py`**, **`backtester2/tickrecorder/ws_client.py`** — Tick recorder reliability improvements

## [1.10.0] - 2026-03-30

### Added — Below-EMA-20 Entry Filter & Limit-Only Execution

- **`ema_filter.py`** — `below_ema20_filter()` / `is_btc_below_ema20()`: new entry condition that passes when the live BTC Deribit index price is at or below the EMA-20; blocks entry (fail-safe) if either data point is unavailable

### Changed

- **`strategies/daily_put_sell.py`** — Removed RFQ open path entirely; now limit-only (`execution_mode="limit"`) with 3 phases: 45s at fair → 45s stepped → 60s at bid; re-enabled entry filter (`below_ema20_filter()` — only sell puts when BTC ≤ EMA-20); Telegram open message now shows total collected premium, phase label, and fill-vs-fair/fill-vs-bid; expiry detection now reads `metadata["expiry_settled"]` flag instead of hold-time heuristic
- **`lifecycle_engine.py`** — `_handle_expiry()` sets `metadata["expiry_settled"] = True` on the trade before calling `_finalize_close()`; removed duplicate Telegram notification (now handled by `on_trade_closed` callback)
- **`market_data.py`** — `get_orderbook()` now enriches the depth dict with mark price fetched from option details when the Coincall orderbook endpoint omits it
- **`backtester2/engine.py`** — `run_grid_full()` now returns `(df, keys)` (pandas DataFrame + key list) instead of `Dict[Tuple, List[Trade]]`; trades are decomposed and discarded immediately (~10× RAM reduction); progress interval configurable via `config.toml`
- **`backtester2/reporting_v2.py`** — Ported to `(df, keys)` engine output; vectorised per-combo stats via pandas groupby (one pass for all combos)
- **`backtester2/strategies/daily_put_sell.py`** — Synced with production strategy refactor
- **`backtester2/tickrecorder/snapshotter.py`** — Various improvements to the tick recorder

### Added — Test Suite Consolidation

- **`tests/conftest.py`** — Shared fixtures: `MockExecutor`, `MockMarketData`, `make_account()`, `make_position()`
- **`tests/test_conditions.py`**, **`tests/test_execution_params.py`**, **`tests/test_fill_manager.py`**, **`tests/test_lifecycle_engine.py`**, **`tests/test_option_selection.py`**, **`tests/test_persistence.py`**, **`tests/test_reconciliation.py`**, **`tests/test_strategy_runner.py`**, **`tests/test_trade_lifecycle.py`**, **`tests/test_deribit_symbols.py`** — New consolidated fast test suite (244 tests, ~1.6s); replaces the old scattered test files
- **`tests/live/`** — Live Deribit testnet integration tests (25+ tests, skipped unless credentials present)
- **`tests/manual/reachability.py`** — Interactive network-toggle script
- **`pyproject.toml`** — pytest config: `addopts = "-m 'not live'"`, `live` marker

## [1.9.0] - 2026-03-27

### Added — Exchange Reachability Monitoring & Tick Recorder

#### Exchange Reachability Tracking
- **`exchanges/base.py`** — `reachable` abstract property added to `ExchangeAuth` interface
- **`auth.py`** — `reachable` property with consecutive failure counter (threshold: 3); auto HTTP session refresh after 5 sustained failures; 4xx client errors treated as reachable (exchange is responding)
- **`exchanges/deribit/auth.py`** — Same reachability tracking for Deribit: `_record_success()` / `_record_failure()` on all GET, POST, and RPC calls
- **`exchanges/coincall/auth.py`** — Exposes `reachable` from inner `CoincallAuth`
- **`account_manager.py`** (`PositionMonitor`) — Adaptive poll backoff (3× interval, max 60s) when exchange is unreachable; Telegram alert on down/recovered transitions; accepts optional `auth` argument

#### Tick Recorder Service
- **`backtester2/tickrecorder/`** — New service: captures 5-min Deribit IV snapshots to disk for backtesting
- **`deployment/ct-recorder.service`** — systemd unit for the tick recorder
- **`deployment/rsync-exclude-recorder.txt`** — rsync exclude list for recorder deployments
- **`deployment/deploy-slot.sh`** — `recorder` target: full deploy, setup (dir/venv/service), start/stop/restart/logs/status commands; `status-all` now shows recorder health
- **`hub/hub_dashboard.py`** — Exchange health lights (Coincall + Deribit probes) in overview; `/api/recorder` endpoint returning recorder health card with next-snapshot countdown
- **`hub/templates/_hub_recorder.html`** — Recorder health card template
- **`hub/templates/_hub_overview.html`** — Exchange status indicator lights (CC / DB) in summary bar

### Changed
- **`hub/templates/hub_dashboard.html`** — CSS for recorder card and exchange health lights
- **`backtester2/strategies/straddle_strangle.py`** — Strategy updates

### Removed
- **`backtester2/metrics.py`**, **`backtester2/reporting.py`** — Replaced by updated implementations
- **`backtester2/test_phase2.py`**, **`backtester2/test_phase2_validation.py`**, **`backtester2/test_phase3.py`** — Dev test scripts removed

## [1.8.0] - 2026-03-26

### Added — Phased Execution & Fee-Aware Notifications (Long Strangle)

#### Phased Close Execution
- **`strategies/long_strangle_index_move.py`** — Sets `ExecutionParams` with 3
  close phases on trade open: Phase 1 (30s fair), Phase 2 (180s aggressive),
  Phase 3 (4h fair with `min_floor_price=0.0001` BTC — keeps deep-OTM leg
  visible in orderbook without blocking the close).
- **`trade_execution.py`** — Added `min_floor_price: Optional[float]` field to
  `ExecutionPhase`. `LimitFillManager` uses it as a last-resort fallback when
  computed price is None or zero (e.g. no bids for deep-OTM leg), instead of
  skipping the order entirely.

#### Telegram Notification Overhaul (Long Strangle)
- **Open message** — Per-leg fill price + cost in BTC/USD, entry fees (Deribit
  fee model: `min(0.03% × index, 12.5% × option_price)`), total outlay.
- **Close message** — Full entry/exit breakdown: per-leg exit proceeds, exit
  fees, net proceeds, gross PnL, total fees, net PnL with ROI %, index at
  entry → close, index move distance.
- Added `_leg_fee_btc()` and `_btc_usd()` helper functions.

### Changed
- **`slots/slot-02.toml`** — `close_hour` 19 → 18; `move_distance_usd` 1500 → 1100
- **`deployment/rsync-exclude-slot.txt`** — Added `backtester2/` to rsync slot
  exclude list (dev-only, not deployed to slots)

## [1.7.0] - 2026-03-25

### Added — Backtester V2 (Real Deribit Prices)

Full options backtester using historic Deribit prices from Tardis, replacing
the V1 Black-Scholes model with real bid/ask data.

#### Data Foundation
- **`snapshot_builder.py`** — One-time conversion of raw Tardis tick parquets
  into 5-min option snapshots + 1-min BTC spot OHLC bars. 15 days builds in
  ~128s, producing 1.99M option rows and 21.5K spot bars.

#### Market Replay Engine
- **`market_replay.py`** — Loads snapshots into memory, iterates `MarketState`
  objects with full option chain access, spot data, and O(1) excursion lookups
  via pre-computed cumulative max/min arrays.
- **`strategy_base.py`** — `Trade`/`OpenPosition` dataclasses, `Strategy`
  protocol (typing.Protocol), composable entry/exit condition helpers
  (time_window, stop_loss_pct, index_move_trigger, etc.).

#### Strategies
- **`strategies/straddle_strangle.py`** — Buy 0DTE ATM straddle or OTM
  strangle, exit on BTC index extrusion. 840-combo parameter grid.
- **`strategies/daily_put_sell.py`** — Sell 1DTE OTM put, exit on stop-loss
  or expiry. 20-combo parameter grid.

#### Engine & Reporting
- **`engine.py`** — Single-pass multi-combo grid evaluation. All 840 straddle
  combos (50,025 trades) complete in ~21s on M1 Mac.
- **`reporting_v2.py`** — Strategy-agnostic HTML report generator. Auto-discovers
  parameter names, generates heatmaps for all 2D param pairs, equity curves,
  trade logs. No V1 coupling.
- **`run.py`** — Clean CLI entry point (~115 lines). No V1 imports or shims.

#### Data Integrity
- Option prices are BTC-denominated, converted to USD via `price × spot`.
- Entry at ask, exit at bid (conservative worst-fill pricing).
- Deribit fee model: `MIN(0.03% × index, 12.5% × option_price)` per leg.
- NaN guards for illiquid strikes with missing bid prices in raw data.

## [1.6.2] - 2026-03-25

### Security Hardening — Dashboard & Hub

Applied OWASP-aligned hardening to both the slot dashboard (`dashboard.py`)
and the hub dashboard (`hub/hub_dashboard.py`):

- **Session cookie flags** — `SESSION_COOKIE_HTTPONLY=True` (blocks JS
  `document.cookie` access, mitigates XSS token theft) and
  `SESSION_COOKIE_SAMESITE="Lax"` (blocks session cookie in cross-site
  top-level POST, mitigates CSRF). `SECURE` intentionally omitted: HTTPS
  termination is handled by nginx in front.
- **Brute-force login rate limiter** — 5 consecutive wrong passwords
  triggers a 60-second per-IP soft-lock. Counter resets on first success.
  Applied to both `dashboard.py` and `hub/hub_dashboard.py`.
- **Constant-time password comparison** — `==` replaced with
  `secrets.compare_digest()`, preventing timing side-channel attacks that
  could otherwise enumerate password characters byte-by-byte.
- **`localhost_only` decorator** (`dashboard.py`) — All `/control/*` routes
  now explicitly reject requests from any remote IP, regardless of bind
  address. Covers `127.0.0.1`, `::1`, and `::ffff:127.0.0.1`.

### Bug Fixes

#### exchanges/deribit/executor.py — Clamp snap-to-tick to minimum valid tick
Deep OTM options near expiry can have prices below `0.0001 BTC` (Deribit's
minimum tick). `_snap_to_tick()` was calling `math.floor` on these, producing
`0.0` — a price Deribit rejects with "Invalid params".

- Added `max(snapped, tick)` clamp: sub-tick prices now floor to `0.0001`
  (or `0.0005` for prices ≥ 0.005) instead of `0.0`.
- Added three regression tests in `tests/test_deribit_integration.py`.

#### execution_router.py / lifecycle_engine.py / trade_execution.py — Best-effort close mode
Previously, if a single leg's computed price was rejected during a close
attempt (e.g. a deep OTM that got snap-to-tick clamped to 0), the entire
batch would fail and block all open legs from closing.

- **`LimitFillManager.place_all(best_effort=True)`** — new flag for close
  orders. In best-effort mode, legs with missing/bad prices are skipped and
  logged; remaining legs are placed normally. Returns `True` if ≥1 order
  was placed. Skipped symbols accessible via `has_skipped_legs` /
  `skipped_symbols` properties.
- **`ExecutionRouter._close_limit`** — passes `best_effort=True` to
  `place_all` for all close orders. Improved `MAX_CLOSE_ATTEMPTS` error to
  list remaining open symbols for easier manual intervention.
- **`LifecycleEngine._check_close_fills`** — `_sync_fills()` helper replaces
  index-based `zip()` with symbol-based matching, which is required when
  best-effort skips legs (fill manager has fewer entries than
  `trade.close_legs`). After a "filled" result, checks `has_skipped_legs`:
  if skipped legs remain they go back to `PENDING_CLOSE` for retry next
  tick; otherwise transitions to `CLOSED` as before.

### Strategy Updates

#### strategies/daily_put_sell.py — Liquidity guard (`MIN_BID_DISCOUNT_PCT`)
Added a minimum fill-price floor to the limit open execution phases:

- **`MIN_BID_DISCOUNT_PCT = 17`** (overridable via `PARAM_MIN_BID_DISCOUNT_PCT`)
  — the strategy refuses to sell below `fair × (1 − 17%) = fair × 0.83`.
- Phase 2.2 (`min_price_pct_of_fair = 0.83`): order skipped if
  `bid + 33%·spread < fair × 0.83`.
- Phase 2.3 (`min_price_pct_of_fair = 0.83`): order skipped if
  `bid < fair × 0.83`.
- In thin weekend/overnight markets where bid is far from fair, phases 2.2
  and 2.3 will refuse to place and the trade simply does not open.
- The stop-loss close bypasses this check — once in a trade we always close.
- Docstring updated to document the liquidity guard and the new `PARAM_*` var.

### Removed

- **`multileg_orderbook.py`** — Deleted. This was an obsolete standalone
  prototype for multi-leg RFQ orderbook probing that was superseded by the
  current `rfq.py` + `execution_router.py` architecture.

---

## [1.6.1] - 2026-03-24

### Bug Fixes & Pipeline Additions

#### daily_put_sell — Remove TP limit order system
The proactive take-profit limit order introduced in a prior session was
removed. It relied on internal framework state in ways that interacted
poorly with the lifecycle engine, and the strategy's natural exit — letting
the option expire worthless — already captures 100% of premium. The TP
infrastructure added complexity without reliable benefit.

- **Removed** `TP_CAPTURE_PCT` param and `tp_capture_pct` from `slots/slot-01.toml`
- **Removed** `_ctx` / `_capture_context` / `_tp_filled_exit` / `_place_tp_limit_order`
- **Removed** `on_runner_created` hook (no longer needed)
- **Removed** unused imports: `OrderPurpose`, `OrderStatus`, `TradeState`
- **Updated** `_on_trade_opened`: no longer places TP order after fill
- **Updated** `_on_trade_closed`: no TP order cancellation; exit reasons
  simplified to SL or expiry only
- **Updated** module docstring: reflects SL-or-expiry exit model

#### long_strangle_index_move — Fix BTC→USD display in Telegram
Deribit denominated all prices in BTC; the Telegram notifications were
showing raw BTC values as if they were USD figures.

- Entry cost, PnL, and fill prices now converted to USD using BTC index
  price at close (falls back to BTC display if index unavailable)
- ROI now calculated on USD values
- Close leg lines show `@ $XX,XXX` (USD) instead of `@ 0.00XXXX`

#### deployment/deploy-slot.sh — Fix stdout pollution in get_env_file()
`step` and `ok` log messages inside `get_env_file()` were writing to stdout,
corrupting the env file path that callers received. Redirected to stderr
(`>&2`).

#### analysis/tardis_options — Pipeline v2: fetch, retry, resumability
Full rewrite of the download/extract pipeline:

- **New `fetch.py`** — Orchestrator for a date range: downloads raw
  `.csv.gz`, extracts to `.parquet`, deletes raw file, then moves to the
  next day. Automatically skips dates that already have a parquet.
  Safely resumable — interrupt and re-run without re-downloading.
- **New `run_fetch.sh`** — Detached shell launcher (`nohup`); survives
  closing the terminal. Logs to `data/fetch_log.txt`.
- **New `_validate.py`** — Post-extract validation checks.
- **`download.py`** — Rewritten: retry loop up to 20×, exponential
  backoff (10 s → 5 min cap). Documented that tardis.dev does not support
  HTTP Range / partial content (verified March 2026); dropped connections
  require a full restart (handled automatically by retry loop).
- **`extract.py`** — Refactored: DTE filter now uses calendar days from
  trade date; zstd compression; cleaner public API.
- **`README.md`** — Comprehensive rewrite reflecting new pipeline
  (`fetch.py` quickstart, individual step docs, API key notes).

---

## [1.6.0] - 2026-03-23

### Slot Configuration System — One-File Deploy

Replaces the manual workflow of editing `.env.slot-XX` files, uncommenting
strategy imports in `main.py`, and remembering API key names. Now: edit a
`.toml` file, run deploy.

### Added
- **`accounts.toml`** — Named account registry mapping friendly names
  (`coincall-main`, `deribit-main`, etc.) to env var names. No secrets stored.
- **`slots/` directory** — Per-slot TOML config files (`slot-01.toml`,
  `slot-02.toml`). Each declares strategy, account, and parameter overrides.
  Edit params here, then deploy.
- **`slot_config.py`** — Reads a slot `.toml` + `accounts.toml` + `.env`,
  generates a fully resolved `.env.slot-XX`. Supports `--dry` for preview.
- **Dynamic strategy import** — `main.py` reads `SLOT_STRATEGY` env var and
  imports the strategy module dynamically. No more editing `main.py` or
  `strategies/__init__.py` to change which strategy runs.
- **Env-overridable strategy params** — Strategy modules now read `PARAM_*`
  env vars with `_p()` helper, falling back to hardcoded defaults. Allows
  running the same strategy with different parameters on different slots.

### Changed
- **`deploy-slot.sh`** — `get_env_file()` now auto-generates `.env.slot-XX`
  from `slots/slot-XX.toml` if the TOML exists, before rsync.
- **`rsync-exclude-slot.txt`** — Excludes `slots/`, `accounts.toml`,
  `slot_config.py` from VPS sync (local-only config tooling).
- **`strategies/daily_put_sell.py`** — 19 params now overridable via env.
- **`strategies/long_strangle_index_move.py`** — 8 params now overridable.
- **`strategies/__init__.py`** — Removed imports of archived strategies
  (`atm_straddle`, `straddle_10utc`).
- **`main.py`** — Removed imports of archived strategies from dev fallback.
- **`.env`** — Cleaned up: removed duplicate `PRODBIG` alias, obsolete
  comments. Now clearly documented as secrets vault + dev defaults.
- **`requirements.txt`** — Added `tomli` (TOML parser for Python 3.9).

### Archived
- `strategies/atm_straddle.py` → `archive/`
- `strategies/straddle_10utc.py` → `archive/`

---

## [1.5.0] - 2026-03-20

### Slot Architecture — Multi-Strategy Deployment

New deployment architecture for running multiple strategies in parallel on a
single server, each in an isolated "slot" with a centralised hub dashboard.

### Added
- **Slot deployment system** — Single `deploy-slot.sh` script manages slots
  01–10 and a hub dashboard. Each slot has its own `.env`, venv, systemd
  service, and logs directory under `/opt/ct/slot-XX/`.
- **Hub dashboard** (`hub/hub_dashboard.py`) — Centralised Flask + htmx
  dashboard that auto-discovers slots by scanning `/opt/ct/slot-*` directories.
  Shows account ID, positions, open orders, health status, and tabbed log
  viewer per slot. Password-protected, dark theme.
- **Control endpoint** — `DASHBOARD_MODE=control` exposes a localhost-only
  JSON endpoint (`/control/status`) with positions, orders, health, logs, and
  account ID. Hub reads this to aggregate slot data.
- **systemd template units** — `ct-slot@.service` (template for slots),
  `ct-hub.service` (hub dashboard).
- **Deployment config files** (gitignored): `.deploy.slots.env` (SSH config),
  `.env.slot-XX` (per-slot config), `.env.hub` (hub config).
- Hub templates: `hub_dashboard.html`, `_hub_overview.html`,
  `_hub_slot_detail.html`.

### Changed
- **`dashboard.py`** — `/control/status` now returns positions, open orders,
  health (uptime, snapshot age, margin/equity warnings), recent logs, and
  account ID. Log handler attached in control mode. Fixed stats access from
  attribute syntax to dict `.get()` (stats property returns a dict, not object).
- **`main.py`** — Removed stale imports of archived strategies
  (`smoke_test_strangle`, `prod_test_put`).
- **`deploy-slot.sh`** — Hub rsync excludes `.venv`, `.env`, `__pycache__`,
  `test_hub_local.py`. Hub firewall port reads from `.env.hub` instead of
  hardcoded 8080.

### Deployment
- Slot-01 deployed to production with `straddle_10utc` strategy on Deribit
  testnet (port 8091, localhost only).
- Hub deployed on port 8070 (external).
- Existing legacy services on ports 8080/8081 untouched.

---

## [1.4.4] - 2026-03-20

### Deribit Production Live + Multi-Instance Deployment

### Critical Fixes
- **Index price routing** — `get_btc_index_price()` in `market_data.py` was
  hardwired to the Coincall `MarketData` class, bypassing the exchange adapter
  layer entirely. On Deribit, this returned the wrong price (~$67k from Coincall
  testnet instead of ~$70k from Deribit). Refactored all convenience functions
  in `market_data.py` to route through the exchange adapter via a lazy
  `_get_adapter()` initializer using `build_exchange()`.
- **Take-profit logic** — Changed from option PnL-based (`dollar_profit_target`)
  to BTC index excursion-based (`_index_excursion_tp`). TP now triggers when
  `|BTC_now - BTC_at_entry| >= $1,000`, matching the metric from the hourly
  excursion analysis.
- **Entry condition signature** — `_weekday_only()` inner function had exit
  condition signature `(account, trade)` but entry conditions only receive
  `(account)`. Fixed to `(account)`.

### Strategy Updates (`strategies/straddle_10utc.py`)
- Execution phases changed: Phase 1 from mark-price → **mid-price** (60s, 15s
  reprice), Phase 2 aggressive (60s, 10s reprice, 5% buffer). Derived from
  testnet fill observations.
- Factory function renamed `atm_str_fixpnl_deribit()` → `straddle_10utc()` for
  consistency with filename.
- Added `_on_trade_opened` callback that captures `entry_index_price` in trade
  metadata for the excursion TP exit condition.

### Multi-Instance Deployment
- **Separate directory pattern** — Deribit instance deploys to
  `/opt/cryotrader-deribit` alongside the existing Coincall instance at
  `/opt/cryotrader`. Each has its own systemd service, venv, and logs.
- **Dashboard port separation** — Coincall on `:8080`, Deribit on `:8081`
  (configured via `Environment=DASHBOARD_PORT=8081` in the Deribit systemd unit).
- **Parameterized deploy script** — `deploy.sh` now reads `DEPLOY_ENV` from
  the environment if set, enabling wrapper scripts per exchange.
- **Service-aware file selection** — `deploy.sh` picks the correct `.service`
  file and `server-setup-*.sh` script based on `$VPS_SERVICE`.

### Exchange Adapter Improvements
- `ExchangeMarketData.get_index_price()` now accepts `use_cache: bool = True`
  parameter across all adapters (base, Deribit, Coincall).
- Deribit adapter honours `use_cache=False` to bypass its 10-second cache for
  fresh prices at trade open/close.

### Added
- `deployment/deploy-deribit.sh` — One-command deploy wrapper for Deribit
- `deployment/cryotrader-deribit.service` — systemd unit for Deribit instance
- `deployment/server-setup-deribit.sh` — VPS setup for Deribit (dir, venv, port 8081)
- `smoke_test_deribit.py` — Production readiness check (auth, index price,
  account, instruments, strategy config) without placing orders
- Multi-instance deployment section in `UBUNTU_DEPLOYMENT.md`

### Changed
- `market_data.py` — Convenience functions route through exchange adapter
- `exchanges/base.py` — `get_index_price()` accepts `use_cache` parameter
- `exchanges/deribit/market_data.py` — Respects `use_cache=False`
- `exchanges/coincall/market_data.py` — Passes `use_cache` through
- `deployment/deploy.sh` — Parameterized `DEPLOY_ENV`, service-aware file selection
- `deployment/rsync-exclude.txt` — Excludes `.deploy.deribit.env`

### Removed
- `strategies/test_excursion_straddle.py` — Testnet-only test strategy (archived)

### First Live Trade (Deribit Production)
- ATM straddle BTC-21MAR26-70500 (0.1 qty per leg)
- Call filled at mid price (Phase 1), put filled aggressive (Phase 2)
- Entry index price correctly captured from Deribit (~$70,500)
- Excursion TP and time exit monitoring active

---

## [1.4.3] - 2026-03-19

### Deribit Production Validated + New Data-Driven Strategy

### Deribit Production Test — Successful
- **First live trade on Deribit production** — Full lifecycle validated:
  BUY 0.1 BTC-21MAR26-66000-P → filled → held 30s → closed.
  Strategy: `prod_test_put` (aggressive limit, single-phase 10% buffer).
  Confirmed: auth, order placement, fill detection, position monitoring,
  close execution, PnL finalization all working on production.
- **`strategies/prod_test_put.py`** — Minimal production test strategy
  for Deribit: buys 0.1 BTC of a specific put, holds 30s, closes.

### Optimal Entry Window Analysis — Refreshed (Feb 19 – Mar 19, 2026)
- Re-ran hourly excursion analysis and structure backtest with 4 weeks
  of current Binance 1h candle data (672 candles).
- **Key findings:**
  - Best short window: 14:00→16:00 UTC (2h), $1,295 avg excursion, $648/hr
  - Best 4h+ window: 14:00→18:00 UTC (4h), $1,709 avg excursion, $427/hr
  - Top backtest combo: ATM straddle, 10:00 UTC entry, $1,000 TP, 9h hold
    → avg PnL $908, 100% win rate over 20 days
  - ATM straddle is the only profitable structure; all strangles underwater

### Added
- **`strategies/straddle_10utc.py`** — New strategy: `ATM_Str_fixpnl_Deribit`.
  Data-driven daily long ATM straddle for Deribit, derived from the refreshed
  Optimal Entry Window analysis:
  - Entry: 10:00 UTC, weekdays only
  - Structure: long ATM straddle, 0.1 BTC/leg, next-day expiry
  - TP: $1,000 USD fixed-dollar profit target (custom exit condition with
    BTC→USD conversion via Deribit position `floating_profit_loss_usd`)
  - Time exit: 19:00 UTC (9h max hold)
  - Execution: two-phase limit orders — Phase 1: 3 min mark-price quoting
    (reprice 30s), Phase 2: 3 min aggressive crossing (3% buffer, reprice 30s)
  - Telegram notifications with full PnL reporting in BTC and USD

### Changed
- Updated `analysis/optimal_entry_window/hourly_excursion.json` — fresh data
- Updated `analysis/optimal_entry_window/hourly_excursion_report.html` — fresh heatmaps
- Updated `analysis/optimal_entry_window/backtest_report.html` — fresh backtest results

### Files
- NEW: `strategies/straddle_10utc.py`, `strategies/prod_test_put.py`
- MODIFIED: `strategies/__init__.py`, `main.py`
- UPDATED: `analysis/optimal_entry_window/` (3 data/report files refreshed)

---

## [1.4.2] - 2026-03-19

### Deribit Phase 3 — Integration Testing & Compatibility Fixes

### Added
- **Account snapshot integration test** (`tests/test_deribit_integration.py`, Test 7) — 5 tests verifying account info fields, margin consistency, position structure, PositionMonitor→AccountSnapshot round-trip, and open orders. All passing on Deribit testnet.
- **Kill switch integration test** (`tests/test_deribit_integration.py`, Test 8) — End-to-end test: place ATM call → fill → run PositionCloser → verify ALL positions closed. Asserts `closer.status == "done"`. Pending clean testnet account (illiquid position expires 20 Mar).
- **BTC-native mark price field** (`exchanges/deribit/market_data.py`) — `_mark_btc` field added to `get_option_orderbook()` return dict for downstream BTC-price consumers.

### Fixed
- **`position_closer.py` Deribit compatibility** — Four fixes:
  - `_check_fills`: Deribit returns `state == "filled"` (string), not `state == 1` (int). Added string check alongside int.
  - `_place_or_reprice`: Removed `round(price, 2)` that truncated BTC prices like 0.005 → 0.01.
  - `_build_legs`: Uses `_mark_price_btc` when available (Deribit BTC-native), `abs(qty)` for signed quantities.
  - `_refresh_marks`: Same BTC-native mark price preference.
- **`trade_execution.py` Deribit fixes** — Passive pricing mode uses `_mark_btc` field; relative price tolerance (0.1% instead of absolute $0.01); false fill detection checks actual order status; Phase 3 aggressive buffer increased to 10%.
- **`execution_router.py` regression** (`d3748b4`) — Direct `from market_data import get_option_market_data` import re-introduced during refactor. Fixed to use `self._market_data.get_option_details()`. Also fixed dict key `mark_price` → `markPrice`.

### Changed
- **`strategies/smoke_test_strangle.py` rewrite** — Full rewrite for Deribit testnet validation: QTY=1.0, ATM call + OTM put structure, 3-phase execution (passive 20s → mark 20s → aggressive 10% 20s), detailed phase logging.
- **Migration plan updated** — Phase 3 checklist reflects completed items (smoke test, account snapshot, position_closer fixes, Coincall regression). Open questions updated (orphan recovery resolved, Coincall regression done).

### Test Results
| Suite | Count | Status |
|-------|-------|--------|
| Unit tests (8 suites) | 97 | ✅ All passing |
| Deribit integration (Tests 1–7) | 30 | ✅ All passing |
| Kill switch (Test 8) | 1 | ⏳ Pending clean testnet account |

### Smoke Test Validation (Deribit Testnet, 19 March)
```
Strategy:     smoke_test_strangle (1.0 BTC, ATM call + OTM put)
3 runs completed:
  Run 1: price_too_high — mark price was USD, Deribit expects BTC → fixed _mark_btc
  Run 2: All 3 phases ran, orders accepted
  Run 3: Both legs filled (put Phase 2, call Phase 3 aggressive)
         Trade opened → held 63s → max_hold_hours close → both close legs filled
         PnL = -0.0007 BTC
```

### Files
- MODIFIED: `position_closer.py`, `trade_execution.py`, `execution_router.py`
- MODIFIED: `exchanges/deribit/market_data.py`, `strategies/smoke_test_strangle.py`
- MODIFIED: `tests/test_deribit_integration.py` (added Tests 7 & 8)
- MODIFIED: `docs/MIGRATION_PLAN_DERIBIT.md`, `docs/MODULE_REFERENCE.md`

---

## [1.4.1] - 2026-03-19

### RFQ Gate Fix & Strategy Cleanup

### Fixed
- **RFQ acceptance gate sign flip** (`rfq.py`) — Gate was accepting quotes up to 2.2% *worse* than orderbook (`min_ok = -mark_floor_pct`). Flipped to require quotes to *beat* orderbook by ≥2% (`min_ok = min_book_improvement_pct`).

### Changed
- **Renamed misleading RFQ parameter** — `mark_floor_pct` → `min_book_improvement_pct` and `RFQ_MARK_FLOOR_PCT` → `RFQ_MIN_BOOK_IMPROVEMENT_PCT` across `rfq.py`, `execution_router.py`, and `daily_put_sell.py`. The old name implied mark price comparison; the actual logic compares against orderbook.
- **Removed unreachable Phase 3 from daily_put_sell** — `RFQ_RELAX_AFTER=300` was unreachable with `RFQ_OPEN_TIMEOUT=60`. Removed the constant and metadata key from the strategy. Phase 3 logic remains in the `rfq.py` engine for future use by other strategies.
- **Updated module docstring** (`daily_put_sell.py`) — Reflects current phased RFQ behavior: Phase 1 (0–20s silent), Phase 2 (20–60s gated ≥2%), timeout → limit fallback.
- **Suppressed werkzeug logs** (`main.py`) — Set werkzeug logger to ERROR level at startup to eliminate Flask request log noise.
- **Switched .env to main production account** — `COINCALL_API_KEY_PROD` / `COINCALL_API_SECRET_PROD` now point to the main (big) account credentials.

### Files
- MODIFIED: `rfq.py`, `execution_router.py`, `strategies/daily_put_sell.py`, `main.py`

---

## [1.4.0-wip] - 2026-03-17

### ⚠️ Work in Progress — Deribit Migration Phase 2

Phase 2 (Deribit adapters + exchange-agnostic refactor + testnet validation) is complete.
Full trade lifecycle validated on Deribit testnet: option selection → buy orders filled → position monitored → sell orders filled → CLOSED.
Next: Phase 3 (production cutover with real strategy sizes).

### Added
- **Deribit auth adapter** (`exchanges/deribit/auth.py`) — OAuth2 client_credentials + refresh_token lifecycle. Thread-safe lazy refresh at 80% of 900s TTL.
- **Deribit market data adapter** (`exchanges/deribit/market_data.py`) — Instruments, ticker, orderbook, index price. BTC-native orderbook prices for executor; USD-converted prices for display/details.
- **Deribit executor adapter** (`exchanges/deribit/executor.py`) — Separate `/private/buy` and `/private/sell` endpoints. `_snap_to_tick()` handles variable tick sizes (0.0001 below 0.005 BTC, 0.0005 above). `label` field as client order ID.
- **Deribit account adapter** (`exchanges/deribit/account.py`) — USD-denominated via `total_equity_usd` fields. Unsigned `size` + `direction` → signed qty normalization. Portfolio-level Greeks.
- **Smoke test strategy** (`strategies/smoke_test_strangle.py`) — Quick validation strangle: 0.1 BTC, ATM ±2 strikes, 60s hold. Purpose-built for exchange integration testing.

### Changed
- **Exchange-agnostic refactor** — 6 core modules refactored to accept exchange adapters via dependency injection instead of importing Coincall modules directly:
  - `option_selection.py` — `market_data` parameter on selection functions
  - `execution_router.py` — `market_data` in constructor
  - `trade_execution.py` — `market_data` in `LimitFillManager`
  - `lifecycle_engine.py` — Passes `market_data` to router + fill manager; sets `_market_data` on create/restore
  - `account_manager.py` — `PositionMonitor` receives `account_manager` adapter
  - `strategy.py` — Wires all adapters through `build_context()`
- **`health_check.py`** — Now accepts `market_data` adapter; uses `get_index_price()` instead of Coincall's hardcoded `get_btc_index_price()`
- **`trade_lifecycle.py`** — Added `_market_data` field; `executable_pnl()` uses injected adapter instead of importing Coincall's `get_option_orderbook()`
- **`main.py`** — Wires `market_data` adapter into `HealthChecker`; builds exchange components from factory

### Fixed
- **Orderbook format mismatch** (`exchanges/deribit/market_data.py`) — Deribit returns `[[price, amount]]`; code expected `[{"price": x, "qty": y}]`. Adapter now returns dict format with BTC-native prices.
- **USD vs BTC price confusion** — Orderbook initially converted all prices to USD; executor expects BTC. Fixed: orderbook returns BTC-native prices; `mark` field stays USD for display.
- **Wrong BTC index price** — `health_check.py` imported Coincall's `get_btc_index_price()` returning $67,456 while Deribit's actual index was $74,405. Fixed via market_data DI injection.
- **`trade_lifecycle.py` Coincall import** — `executable_pnl()` imported `from market_data import get_option_orderbook` (Coincall's module). Fixed via `_market_data` adapter field.
- **BTC price truncation** — `round(x, 2)` in `LimitFillManager` truncated BTC prices like 0.0035 → 0.00. Removed all `round(x, 2)` from price path; executor's `_snap_to_tick()` handles precision.
- **Min order size rejected** — `qty=0.01` below Deribit minimum 0.1 BTC. Updated smoke test QTY to 0.1.

### Test Results
| Suite | Count | Status |
|-------|-------|--------|
| Unit tests (8 suites) | 97 | ✅ All passing |
| Deribit integration (5 suites) | 25 | ✅ All passing |
| **Total** | **122** | **✅** |

### End-to-End Validation (Deribit Testnet)
```
Strategy:     smoke_test_strangle (0.1 BTC, ATM ±2 strikes, 60s hold)
Instruments:  BTC-18MAR26-75000-C @ 0.0033, BTC-18MAR26-73500-P @ 0.0034
Result:       Open → FILLED → 60s hold → Close → FILLED → CLOSED (PnL ≈ $0.00)
Debug cycles: 5 iterations from first run to success
```

### Files
- NEW: `exchanges/deribit/__init__.py`, `auth.py`, `market_data.py`, `executor.py`, `account.py`
- NEW: `tests/deribit/test_deribit_auth.py`, `test_deribit_market_data.py`, `test_deribit_account.py`, `test_deribit_orders.py`, `test_deribit_symbols.py`
- NEW: `strategies/smoke_test_strangle.py`
- MODIFIED: `option_selection.py`, `execution_router.py`, `trade_execution.py`, `lifecycle_engine.py`, `account_manager.py`, `strategy.py`
- MODIFIED: `health_check.py`, `trade_lifecycle.py`, `main.py`
- MODIFIED: `docs/MIGRATION_PLAN_DERIBIT.md` (rewritten for Phase 2 completion)

### Known Issues
- Orphaned positions from killed bot runs are not recovered on restart
- `rfq.py` still imports Coincall modules directly (not behind abstraction)
- Coincall path not live-tested since the exchange-agnostic refactor

---

## [1.3.0-wip] - 2026-03-16

### ⚠️ Work in Progress — Deribit Migration Phase 1

Phase 1 (Exchange Abstraction Layer) of the Coincall → Deribit migration is complete.
The system still runs on Coincall — no behavior changes. `EXCHANGE=coincall` (default).
Next: Phase 2 (Deribit adapter implementation).

### Added
- **Exchange abstraction layer** (`exchanges/`) — 5 abstract base classes defining the exchange contract (`ExchangeAuth`, `ExchangeMarketData`, `ExchangeExecutor`, `ExchangeAccountManager`, `ExchangeRFQExecutor`)
- **Coincall adapters** (`exchanges/coincall/`) — 5 thin adapter classes wrapping existing Coincall modules behind the new interfaces
- **Exchange factory** (`exchanges/__init__.py`) — `build_exchange("coincall")` returns all exchange components; raises `NotImplementedError` for `"deribit"` (Phase 2)
- **Exchange config** (`config.py`) — `EXCHANGE` env var (default: `"coincall"`) with validation

### Changed
- **Side encoding normalized** — All internal code now uses `"buy"` / `"sell"` strings instead of `1` / `2` integers. Affected: `TradeLeg.side`, `LegSpec.side`, `OrderRecord.side`, `_LegFillState.side`, `LegChunkState.side`, `_CloseLeg.close_side`. `CoincallExecutorAdapter` converts back to int at the API boundary.
- **Backward compatibility** — `TradeLeg.__post_init__` and `OrderRecord.from_dict()` auto-convert legacy int sides from crash-recovery snapshots
- **`OrderManager`** — Accepts `exchange_state_map` parameter for exchange-specific order status mapping (defaults to Coincall map)
- **`LifecycleEngine`** — Accepts `executor`, `rfq_executor`, `exchange_state_map` parameters via dependency injection (defaults to Coincall)
- **`strategy.py`** — `build_context()` now uses `build_exchange()` factory; `TradingContext` fields typed as `Any` for exchange-agnostic DI
- **All 4 strategy files** updated to use string side encoding
- **Templates** (`_orders.html`) — Side display uses `o.side|upper` instead of int ternary
- **Documentation** — `MODULE_REFERENCE.md`, `.copilot-instructions.md`, `MIGRATION_PLAN_DERIBIT.md` updated with string side encoding and Phase 1 completion status

### Fixed
- **Test reconciliation mocks** (`test_order_manager.py`, `test_phase3_hardening.py`) — Pre-existing bug: test mocks used raw API key `orderId` instead of normalized `order_id` (as returned by `account_manager.get_open_orders()`). Production `account_manager.py` normalization is untouched.

### Test Results (Phase 1 verification)
| Suite | Result |
|-------|--------|
| test_phase2_structural.py | 67/67 ✅ |
| test_phase3_hardening.py | 23/23 ✅ |
| test_order_manager.py | 85/85 ✅ |
| test_strategy_framework.py | 71/71 ✅ |
| test_execution_timing.py | 40/40 ✅ |
| test_atm_straddle.py | 34/34 ✅ |
| test_strategy_layer.py | 49/50 ⚠️ (1 pre-existing: strangle default dte changed from 0 to "next") |

### Files
- NEW: `exchanges/__init__.py`, `exchanges/base.py`
- NEW: `exchanges/coincall/__init__.py`, `auth.py`, `market_data.py`, `executor.py`, `account.py`, `rfq.py`
- MODIFIED: `config.py`, `trade_lifecycle.py`, `option_selection.py`, `order_manager.py`, `execution_router.py`, `position_closer.py`, `lifecycle_engine.py`, `strategy.py`, `telegram_notifier.py`, `multileg_orderbook.py`, `trade_execution.py`
- MODIFIED: `strategies/` (all 4 strategy files)
- MODIFIED: `templates/_orders.html`
- MODIFIED: All 7 test files in `tests/`
- MODIFIED: `docs/MODULE_REFERENCE.md`, `.copilot-instructions.md`, `docs/MIGRATION_PLAN_DERIBIT.md`

## [1.1.1] - 2026-03-17

### Production Hotfix (deployed from `hotfix/1.1.1` branch, merged back to main)

**Context:** v1.2.0 reconciliation fix was never deployed because the Deribit migration (v1.3.0-wip) had
already altered core modules. Production was running v1.1.0. Hotfix branched from v1.1.0, applied two
targeted fixes, deployed to production, and merged back to main.

### Fixed
- **Order reconciliation key mismatch** (`order_manager.py`) — `reconcile()` used `orderId` (camelCase) but `account_manager.get_open_orders()` returns `order_id` (snake_case). Every live order appeared "not found on exchange", causing a Telegram warning every 60 seconds.
- **TP/SL idempotent order collision** (`execution_router.py`) — `_close_limit()` now cancels existing `CLOSE_LEG` orders (e.g. TP limit order) before placing SL close orders. Previously, `OrderManager`'s idempotent guard returned the stale TP order (at $9.50) instead of placing an aggressive close, adding ~35 seconds of SL execution delay.
- **Test mocks** (`test_order_manager.py`, `test_phase3_hardening.py`) — reconciliation test mocks updated to use `order_id` (snake_case) matching `account_manager.get_open_orders()` output.

### Incident Reference
See `analysis/2026-03-17_overnight_sl_analysis.md` for full post-mortem of the March 17 stop-loss event.

## [1.2.1] - 2026-03-16

### Added
- **Deribit market data test** (`tests/deribit/test_deribit_market_data.py`) — explored instrument list, ticker, orderbook, index price shapes; confirmed BTC-denominated pricing and always-populated Greeks
- **Deribit account data test** (`tests/deribit/test_deribit_account.py`) — explored account summary, positions, open orders, trade history; confirmed unsigned size + direction field, portfolio margining, 47 account fields
- **Deribit order round-trip test** (`tests/deribit/test_deribit_orders.py`) — full lifecycle on testnet: place→read→modify→cancel, place→fill→verify position→close→verify gone, edge cases (reduce_only, min size, invalid instrument); 27/27 checks passed
- **Deribit symbol translation test** (`tests/deribit/test_deribit_symbols.py`) — parse+reconstruct round-trip for all 1134 testnet and 918 production BTC options; zero failures, no decimal strikes, date verification
- **Deribit resilience test** (`tests/deribit/test_deribit_resilience.py`) — rate limit probing (25 rapid calls, no throttle), error shapes for invalid token/scope/instrument, token refresh lifecycle (old token invalidated after refresh)

## [1.2.0] - 2026-03-16

### Added
- **Deribit migration plan** (`docs/MIGRATION_PLAN_DERIBIT.md`) — comprehensive 950+ line plan covering exchange abstraction layer, module-by-module migration, RFQ/Block Trade abstraction design, phased rollout, and live integration test plan
- **Deribit auth test** (`tests/deribit/test_auth_quick.py`) — verified connectivity to both testnet and production accounts

### Fixed
- **Order reconciliation key mismatch** (`order_manager.py`) — fixed `orderId` → `order_id` in exchange open-order set used for ledger reconciliation

## [1.1.0] - 2026-03-13

### Added

#### Daily Put Sell Strategy (`strategies/daily_put_sell.py`)
Automated 1–2 DTE OTM put selling with trend filter and multi-layered exit logic.

**Entry conditions:**
- **EMA-20 trend filter** — Only sells puts when BTC > daily EMA-20 (Binance klines)
- **Time window** — Configurable UTC entry window (default 03:00–04:00)
- **Minimum margin** — Requires ≥20% available margin

**Execution:**
- **Open:** Phased RFQ execution (30s silent → gated at 2.2% of mark → relaxed after 5min)
- **Take profit:** Proactive limit buy order placed immediately after open at 10% of entry premium
- **Stop loss:** Exit at 70% loss (mark PnL), close via standard RFQ with 15s timeout
- **Expiry:** If neither TP nor SL fires, option expires worthless (full win)

**Lifecycle features:**
- `on_trade_opened` — Places TP limit order, sends Telegram notification
- `on_trade_closed` — Cancels orphaned TP order, logs PnL, sends Telegram notification
- `on_runner_created` hook — Captures `TradingContext` for callback use
- `_tp_filled_exit()` — Custom exit condition detecting TP limit order fill
- `max_concurrent_trades=2` — Handles expiry overlap between consecutive days

**Backtest results (2024-01-01 to 2025-03-10):**
Win rate: 93.1% | Avg winner: $27.81 | Avg loser: -$44.29
Profit factor: 8.52 | Total return: +66.2% | Max drawdown: -3.8%

#### EMA Filter Module (`ema_filter.py`)
Binance BTCUSDT Perpetual daily kline fetcher with EMA-20 calculation.
- `get_ema20()` — Returns current EMA-20 value
- `is_btc_above_ema20()` — Boolean trend check
- `ema20_filter()` — Entry condition factory for `StrategyConfig`
- 1-hour kline cache with stale-cache fallback on API errors
- Standard recursive EMA formula seeded with SMA of first N values

#### Phased RFQ Execution (`rfq.py` — `execute_phased()`)
Three-phase quote acceptance for better-than-market fills:
1. **Initial wait** (0–30s): Collect quotes silently
2. **Gated** (30s–5min): Accept if within 2.2% of orderbook baseline
3. **Relaxed** (5min+): Accept any quote

Configured per-strategy via `metadata` keys: `rfq_phased`, `rfq_initial_wait_seconds`,
`rfq_mark_floor_pct`, `rfq_relax_after_seconds`.

#### Phased RFQ Routing (`execution_router.py`)
`_open_rfq()` detects `trade.metadata["rfq_phased"]` and routes to `execute_phased()`
with strategy-specific timeout/floor/relaxation parameters.

### Fixed

#### RFQ Single-Leg Direction (`rfq.py`)
- Coincall requires single-leg RFQs to be submitted with side="BUY"
- `create_rfq()` now auto-flips single-leg SELL to BUY for submission
- Quote acceptance still uses the actual trade direction (sell-side quotes)
- Two-way quotes from market makers are filtered by the `action` parameter

#### Stop Loss PnL Mode (`strategies/daily_put_sell.py`)
- Changed `max_loss()` from `pnl_mode="executable"` to `pnl_mode="mark"`
- Executable PnL uses ask price (cost to close), which on wide-spread OTM options
  can show -125% loss immediately after opening (bid $20 / ask $46)
- Mark PnL uses mid-price, preventing false SL triggers from wide spreads

### Files Changed
- NEW: `ema_filter.py` — EMA-20 trend filter (Binance klines)
- NEW: `strategies/daily_put_sell.py` — Daily put sell strategy
- MODIFIED: `rfq.py` — `execute_phased()`, single-leg BUY fix
- MODIFIED: `execution_router.py` — Phased RFQ routing
- MODIFIED: `strategies/__init__.py` — Registered `daily_put_sell`
- MODIFIED: `main.py` — Added `daily_put_sell` to STRATEGIES, DEBUG logging

---

## [1.0.4] - 2026-03-13

### Fixed

#### Critical: Crash Recovery & State File Resilience (RC1 + RC2)

On March 12 at 21:52 CET the VPS suffered a hypervisor-level hard reboot.
Two cascading issues kept the bot offline for 17+ hours:

1. **State file corruption** — `trades_snapshot.json` and `active_orders.json`
   were filled with `\x00` null bytes (OS had allocated disk space but pending
   writes were still in the kernel buffer when power was lost).
2. **Permanent crash loop** — On every restart `_recover_trades()` hit a JSON
   parse error on the null-byte file, returned `None`, and `main.py` called
   `sys.exit(1)`. NSSM restarted with exponential backoff, repeating forever.

#### RC1 — Atomic / safe file writes
- **`lifecycle_engine.py`** — `_persist_all_trades()` now writes to a `.tmp`
  file, calls `f.flush()` + `os.fsync()`, then atomically renames via
  `os.replace()`. Prevents half-written or buffered-only data from corrupting
  `trades_snapshot.json` on power loss.
- **`order_manager.py`** — `persist_snapshot()` already used temp + rename but
  was missing `os.fsync()`. Added `f.flush()` + `os.fsync()` before the rename
  so the data is guaranteed on disk before the file becomes visible.

#### RC2 — Corrupted state no longer causes a permanent crash loop
- **`main.py`** — Three new helpers: `_is_corrupt_file()` (detects null-byte
  files), `_quarantine_file()` (moves corrupt files to `<name>.corrupt.<ts>`
  for forensics), and `_handle_corrupt_snapshot()` (queries exchange for actual
  open positions, quarantines the file, and returns 0 to start fresh instead of
  returning None which triggered `sys.exit(1)`).
- Both the null-byte detection path and the `json.load()` parse-error path now
  route through `_handle_corrupt_snapshot()`, eliminating the crash loop.
- If the exchange has open positions, a `CRITICAL` log with the symbols is
  emitted so the operator knows to check manually — but the bot **runs** rather
  than dying forever.
- **`order_manager.py`** — `load_snapshot()` now detects null-byte corruption,
  quarantines the file, and starts with an empty ledger (non-fatal). New
  `_quarantine()` static method handles the move + log.

### Files Changed
- MODIFIED: `lifecycle_engine.py` (`_persist_all_trades` — atomic write)
- MODIFIED: `order_manager.py` (`persist_snapshot` — fsync; `load_snapshot` — corruption recovery)
- MODIFIED: `main.py` (corruption helpers, `_recover_trades` recovery path)

---

## [1.0.3] - 2026-03-12

### Fixed

#### Critical: Stale BTC Index Price — Take Profit Never Triggered
- **Root cause:** `get_btc_index_price()` in `market_data.py` directly iterated `_details_cache._cache.items()`, bypassing the `TTLCache.get()` TTL check. Expired option detail entries (with an old `indexPrice`) were read as if fresh, then used to re-populate `_index_cache` with a new timestamp — creating an infinite stale-cache loop. The index price never updated, so `index_move_distance()` never detected real BTC movement.
- **Impact:** On March 11, the ATM straddle index-move strategy opened at 12:00 UTC with entry index $69,194 (threshold ±$1,200). BTC rose ~$1,300+ through the afternoon (confirmed by option deltas and prices), but the bot saw a frozen index of $69,461 the entire 7 hours. The index-based TP never triggered; the trade closed at 19:00 UTC via `time_exit` at a $3.64 loss.

#### Fix 1 — TTLCache: `fresh_items()` method (NEW)
- **`market_data.py`** — Added `TTLCache.fresh_items()`: yields only non-expired `(key, value)` pairs and evicts stale entries during iteration. Provides a proper TTL-enforced iteration API.

#### Fix 2 — `get_btc_index_price()` step 1 enforces TTL
- **`market_data.py`** — Replaced `self._details_cache._cache.items()` with `self._details_cache.fresh_items()`. Expired option detail entries are now skipped and evicted, breaking the stale-cache loop.

#### Fix 3 — Frozen-price detection
- **`market_data.py`** — New `_update_index_cache()` helper centralises cache writes. Logs a `WARNING` if the index price hasn't changed for > 60 seconds (possible stale feed). All index-price source logging upgraded from `DEBUG` to `INFO` for production visibility.

#### Fix 4 — Exit condition forces fresh fetch
- **`strategies/atm_straddle_index_move.py`** — `index_move_distance()` now calls `get_btc_index_price(use_cache=False)`. Exit evaluation is safety-critical and runs only every 30s, so the extra API call is acceptable and eliminates secondary caching risk.

#### Fix 5 — Health check monitors index price
- **`health_check.py`** — Every 5-minute health check now fetches the BTC index price with `use_cache=False` and logs a `WARNING` if unavailable, providing early alerting for feed problems.

### Files Changed
- MODIFIED: `market_data.py` (TTLCache.fresh_items, get_btc_index_price fix, _update_index_cache, frozen-price detection)
- MODIFIED: `strategies/atm_straddle_index_move.py` (use_cache=False for exit condition)
- MODIFIED: `health_check.py` (index price freshness check)

---

## [1.0.2] - 2026-03-11

### Added

#### ATM Straddle — Index Move Strategy
- **`strategies/atm_straddle_index_move.py`** (NEW) — Daily long ATM straddle that closes when the BTCUSD index moves ≥ $N from entry (symmetric up/down), instead of using option PnL. Entry index price captured in `trade.metadata["entry_index_price"]` via `on_trade_opened`. Default distance: $1200 (parameterized for daily adjustment).

#### BTC Index Price Support
- **`market_data.py`** — New `get_btc_index_price()` method + module-level convenience function. Sources (in order): cached option detail `indexPrice`, fresh option detail fetch, Binance perpetual fallback. 30s cache.

### Changed

#### Telegram Notifications — Equity & Margin
- **`strategies/atm_straddle.py`** — Trade opened/closed notifications now include equity, available margin, and margin % free.
- **`strategies/atm_straddle_index_move.py`** — Same enrichment, plus BTC index entry/close prices and move distance.

#### Daily Repeat Fix
- **`main.py`** — Removed auto-`sys.exit(0)` when all strategies hit their daily quota. The process now stays alive, and `max_trades_per_day` counters naturally reset at UTC midnight. Strategies repeat indefinitely.

#### Cleanup
- **`strategies/__init__.py`**, **`main.py`** — Removed `test_strangle_11mar` (file deleted in prior session).

### Files Changed
- NEW: `strategies/atm_straddle_index_move.py`
- NEW: `analysis/README.md`
- MODIFIED: `market_data.py` (get_btc_index_price)
- MODIFIED: `main.py` (daily repeat fix, strategy wiring)
- MODIFIED: `strategies/atm_straddle.py` (enriched notifications)
- MODIFIED: `strategies/__init__.py` (new strategy export, dead import removed)

---

## [1.0.1] - 2026-03-11

### Added — Phase 3 Hardening

Live-testing revealed three issues (all fixed) and prompted additional hardening.

#### Reconciliation Grace Period
- **`order_manager.py`** — `reconcile()` now skips PENDING orders and orders placed within the last 30 seconds. Prevents false "stale ledger entry" warnings for orders that haven't been acknowledged by the exchange yet.

#### Requote Skip-if-Unchanged
- **`trade_execution.py`** — `_requote_unfilled()` skips requoting when the new price is within $0.01 of the existing order price (tolerance check instead of exact float `==`). Avoids wasteful cancel+replace cycles on stable markets. Logged at INFO level when skipped.

#### Strategy Restart Prevention
- **`strategies/test_strangle_11mar.py`** — Set `max_trades_per_day=1` (was 3) and `cooldown_seconds=120` (was 0) to prevent the strategy from restarting after a successful close.

#### Dashboard Orders Panel
- **`dashboard.py`** — Added `/api/orders` route exposing the `OrderManager` ledger.
- **`templates/_orders.html`** — New htmx fragment: active orders table with status, purpose, timestamps.

#### Testing
- **`tests/test_phase3_hardening.py`** (NEW) — 21 assertions: reconciliation (Telegram alerts, dashboard /api/orders route, grace period for PENDING orders, grace period for recently placed orders), structural integration.

### Changed — Code Cleanup

Post-v1.0.0 cleanup: removed dead code, stale backward-compatibility shims, and unused strategies.

#### Removed
- **`SmartOrderbookExecutor` integration** — Removed from `ExecutionRouter`. The `multileg_orderbook.py` module still exists as a standalone tool but is no longer routed to by the engine. `ExecutionRouter` now only supports `limit` and `rfq` modes.
- **`LifecycleManager` backward-compat alias** — Removed `__getattr__` lazy re-export from `trade_lifecycle.py`. Use `from lifecycle_engine import LifecycleEngine` directly.
- **`SmartExecConfig`** — Removed from `trade_lifecycle.py` (was dead code after smart executor decoupling).
- **`ctx.notifier` and `ctx.smart_executor`** — Removed from `TradingContext` in `strategy.py`.
- **`strategies/long_strangle_pnl_test.py`** (DELETED) — Unused PnL monitoring test strategy.
- **`strategies/reverse_iron_condor_live.py`** (DELETED) — Unused reverse iron condor strategy.

### Documentation
- **`docs/MODULE_REFERENCE.md`** — Updated TradingContext fields, fixed LifecycleManager→LifecycleEngine references, removed backward-compat note, updated Telegram integration (strategy-level opt-in), added reconciliation grace period and requote skip docs, added `/api/orders` route.
- **`docs/ARCHITECTURE_PLAN.md`** — Updated directory structure (removed dead strategies, added `_orders.html`), fixed ExecutionRouter description (limit/rfq only), updated test counts, fixed LifecycleManager→LifecycleEngine references.

### Files Changed
- MODIFIED: `order_manager.py` (reconciliation grace period)
- MODIFIED: `trade_execution.py` (requote skip-if-unchanged)
- MODIFIED: `strategies/test_strangle_11mar.py` (max_trades_per_day, cooldown)
- MODIFIED: `dashboard.py` (+/api/orders route)
- MODIFIED: `execution_router.py` (smart executor removed)
- MODIFIED: `lifecycle_engine.py` (SmartExecConfig references removed)
- MODIFIED: `trade_lifecycle.py` (LifecycleManager alias, SmartExecConfig removed)
- MODIFIED: `strategy.py` (ctx.notifier, ctx.smart_executor removed)
- MODIFIED: `strategies/__init__.py` (dead strategy imports removed)
- DELETED: `strategies/long_strangle_pnl_test.py`
- DELETED: `strategies/reverse_iron_condor_live.py`
- NEW: `templates/_orders.html`
- NEW: `tests/test_phase3_hardening.py` (21 assertions)
- NEW: `strategies/test_strangle_11mar.py` (live test strategy)

---

## [1.0.0] - 2026-03-09

### Added — Order Management & Structural Split

Major architectural release: central order ledger preventing duplicate/runaway orders, and structural split of the monolithic `trade_lifecycle.py` into three focused modules.

#### Phase 1: Core Safety — OrderManager

- **`order_manager.py`** (NEW, ~600 lines) — Central order ledger wrapping `TradeExecutor`. Every order placement and cancellation goes through here.
  - **Idempotent placement:** Dedup key `(lifecycle_id, leg_index, purpose)` — calling `place_order()` twice returns the existing live order instead of creating a duplicate
  - **Supersession chains:** `requote_order()` atomically cancels the old order, places a new one, and links them via `superseded_by`/`supersedes` fields
  - **Hard caps:** 30 orders per lifecycle, 4 pending per symbol — prevents runaway order accumulation
  - **Safety enforcement:** `CLOSE_LEG` and `UNWIND` purposes always force `reduce_only=True` regardless of caller
  - **JSONL audit log:** Every state change appended to `logs/order_audit.jsonl` (append-only, never truncated)
  - **JSON snapshots:** `logs/active_orders.json` written on `persist_snapshot()` for crash recovery
  - **`poll_all()`** — Batch poll all live orders from exchange, update statuses
  - **`reconcile(exchange_orders)`** — Compare ledger against exchange open orders, detect orphans and stale entries
  - **`has_live_orders(lifecycle_id)`** — PENDING_CLOSE guard used by tick() to prevent double-close
- **`trade_execution.py`** — `LimitFillManager._place_single()` and `_requote_unfilled()` now route through `OrderManager` when present. Backward compatible — works without it.

#### Phase 2: Structural Split

- **`trade_lifecycle.py`** (TRIMMED, ~450 lines) — Now data-only: `TradeState`, `TradeLeg`, `TradeLifecycle`, `RFQParams`, `ExitCondition`, PnL helpers (`structure_pnl`, `executable_pnl`, `structure_greeks`). All state-machine logic removed.
- **`lifecycle_engine.py`** (NEW, ~500 lines) — `LifecycleEngine` class (renamed from `LifecycleManager`). Full state machine: `create()`, `open()`, `close()`, `tick()`, `force_close()`, `kill_all()`, `cancel()`, `restore_trade()`. Creates and owns `ExecutionRouter` and `OrderManager`. Exposes `order_manager` property.
- **`execution_router.py`** (NEW, ~400 lines) — `ExecutionRouter` class. Routes open/close to correct executor:
  - Single leg → always "limit"
  - Multi-leg, notional ≥ $50k → "rfq"
  - Multi-leg, $10k ≤ notional < $50k → "smart"
  - Multi-leg, notional < $10k → "limit" (fallback)
  - Close circuit breaker: 10 attempts → FAILED with critical log
  - All close orders enforce `reduce_only=True`
- **Backward compatibility:** `from trade_lifecycle import LifecycleManager` still works — resolves to `LifecycleEngine` via `__getattr__` lazy re-export. All existing strategy code and tests unchanged.

#### Integration Updates

- **`position_closer.py`** — Added `order_manager.cancel_all()` call after `kill_all()` in `_run()`. Belt-and-suspenders: both lifecycle-level and order-level cleanup on kill switch.
- **`main.py` crash recovery** — Three-step recovery:
  1. Load order ledger via `order_manager.load_snapshot()` + `poll_all()` to get true exchange state
  2. (Existing) Load `trades_snapshot.json`, verify exchange positions, normalize states
  3. Reconcile order ledger against exchange open orders via `reconcile()`

### Testing

- **`tests/test_order_manager.py`** (NEW) — 85 assertions: idempotency, supersession chains, hard caps, persistence round-trip, reconciliation, cancel_all
- **`tests/test_phase2_structural.py`** (NEW) — 71 assertions: backward-compat imports, ExecutionRouter open/close/circuit-breaker, LifecycleEngine API surface, position_closer integration, crash recovery code paths, persistence round-trip, mode auto-detection, strategy import chain
- All existing tests pass unchanged: execution_timing 40/40, strategy_framework 72/72, atm_straddle 34/34, strategy_layer 50/51

### Files Changed
- NEW: `order_manager.py` (~600 lines)
- NEW: `lifecycle_engine.py` (~500 lines)
- NEW: `execution_router.py` (~400 lines)
- NEW: `tests/test_order_manager.py` (85 assertions)
- NEW: `tests/test_phase2_structural.py` (71 assertions)
- MODIFIED: `trade_lifecycle.py` (1389 → ~450 lines, data-only + lazy re-export)
- MODIFIED: `trade_execution.py` (LimitFillManager routes through OrderManager)
- MODIFIED: `position_closer.py` (+cancel_all() on kill)
- MODIFIED: `main.py` (crash recovery: order ledger load + poll + reconcile)

### Architecture Notes
- `OrderManager` wraps `TradeExecutor` (not vice versa) — existing executor code unaffected
- `LifecycleEngine` is the single owner of `OrderManager` and `ExecutionRouter`
- Strategies never interact with `OrderManager` or `ExecutionRouter` directly — they set `execution_mode` on `StrategyConfig` and everything flows through `LifecycleEngine`
- Circular import between the three split modules resolved via `__getattr__` lazy import in `trade_lifecycle.py`

---

## [0.9.4] - 2026-03-07

### Changed — Telegram Notifications Moved to Strategy Level

Telegram notifications are now a strategy opt-in concern, not a framework responsibility. Infrastructure modules no longer call the notifier — each strategy decides what to notify and when.

#### Removed from framework
- **`main.py`** — Removed `TelegramNotifier` creation, `ctx.notifier` wiring, startup/shutdown/error/daily-summary notifications
- **`dashboard.py`** — Removed `notify_strategy_paused/resumed/stopped()` calls
- **`trade_lifecycle.py`** — Removed `_notify_trade_opened()` method and all call sites, removed `notify_error()` on close circuit breaker
- **`position_closer.py`** — Removed `notifier` parameter, `_notify()` method, and all Telegram progress calls
- **`strategy.py`** — Removed `TelegramNotifier`/`get_notifier` imports, `ctx.notifier` field from `TradingContext`, auto `notify_trade_closed()` from `_check_closed_trades()`
- **`telegram_notifier.py`** — Removed `maybe_send_daily_summary()`, `notify_strategy_paused/resumed/stopped()`, daily-summary state

#### Added to framework
- **`strategy.py`** — New `on_trade_opened` callback on `StrategyConfig` (mirrors existing `on_trade_closed`). New `_check_opened_trades()` detection in `StrategyRunner` with `_known_open_ids` tracking. Recovery in `main.py` pre-populates open IDs to prevent re-firing.

#### Strategy-level Telegram (opt-in)
- **`strategies/atm_straddle.py`** — Added `get_notifier()` import, `_on_trade_opened()` and Telegram calls in `_on_trade_closed()`, both wired to `StrategyConfig`
- **`strategies/blueprint_strangle.py`** — Same pattern

### Design Rationale
The singleton `get_notifier()` remains available for any strategy to import and use. Infrastructure modules (lifecycle, dashboard, kill switch) stay silent — their events are logged but not pushed to Telegram. This keeps notification decisions where they belong: in the strategy.

---

## [0.9.3] - 2026-03-05

### Fixed — Runaway Short Position on Close (Critical)

- **`reduce_only` on all close orders** (`trade_execution.py`, `trade_lifecycle.py`) — Close orders now set `reduceOnly=1` on the exchange API, making it physically impossible for close orders to build a reverse position. This is the primary fix: even if retry logic has bugs, the exchange rejects any order that would exceed the open position size.
- **Pre-validate all leg prices before placing orders** (`trade_execution.py`) — `LimitFillManager.place_all()` now gathers prices for ALL legs in a first pass. If any leg has no orderbook liquidity, it returns `False` immediately with zero orders placed. This eliminates the partial-placement-then-cancel race condition where one leg fills instantly while the cancel for rollback arrives too late.
- **Close attempt circuit breaker** (`trade_lifecycle.py`) — `_close_limit()` now tracks `_close_attempt_count` on the trade. After 10 failed attempts, the trade transitions to `FAILED` and sends a Telegram alert for manual intervention. Prevents infinite retry loops.

### Root Cause

A straddle close at 19:00 UTC triggered when one leg (put) had no orderbook bids. The old `place_all()` placed leg 1 (call sell), then discovered leg 2 had no price, tried to cancel leg 1, but it had already filled. This fill was silently lost. On retry, `_close_limit()` rebuilt close legs from scratch (unaware of the fill), placed another sell for the full quantity, and the cycle repeated 72 times — accumulating a ~0.64 BTC naked short position until margin was exhausted.

### Files Changed
- `trade_execution.py` — `place_order()`: added `reduce_only` param; `place_all()`: added `reduce_only` param + price pre-validation; `_requote_unfilled()`: passes `reduce_only` through
- `trade_lifecycle.py` — `_close_limit()`: `reduce_only=True` on close orders + circuit breaker (10 attempts → FAILED + Telegram alert)

---

## [0.9.2] - 2026-03-05

### Fixed

- **Entry cost $0.00 in Telegram** — "Trade Opened" notification was sent before limit-mode fills completed, so `total_entry_cost()` returned zero. Moved notification from `strategy.py` to `trade_lifecycle.py`, firing at the actual OPEN state transition where fill prices are populated.

### Changed

- **`telegram_notifier.py`** — Added `get_notifier()` module-level singleton factory. Any module can import and call it without DI wiring (same pattern as `logging.getLogger()`).
- **`trade_lifecycle.py`** — Added `_notify_trade_opened()` helper on `LifecycleManager`. Called at all 3 OPEN transitions: `_check_open_fills` (limit), `_open_rfq`, `_open_smart`.
- **`strategy.py`** — Removed premature `notify_trade_opened` call from `_open_trade()`. Close notification migrated from `self.ctx.notifier` to `get_notifier()`.

---

## [0.9.1] - 2026-03-05

### Removed - Streamlined Supervision

- **`deployment/health_check.ps1`** (DELETED) — PowerShell health check script was redundant with NSSM and caused restart loops when the bot was idle (stale log detection triggered unnecessary service restarts every ~45 min).
- **`close_all_positions.py`** (DELETED) — Obsolete standalone prototype, superseded by `position_closer.py` which is integrated with the dashboard kill switch.
- **Crash flag** (`logs/.running`) — Removed `RUNNING_FLAG` write/cleanup logic from `main.py`. The file was fragile (not cleaned on kill -9 or NSSM stop) and unnecessary — the trade snapshot + exchange verification is the actual safety net.

### Changed

- **`health_check.py`** — Removed `notifier` parameter and `notify_daily_summary()` call. Now pure observability: logs health status every 5 min, escalates on high margin/low equity, no side effects.
- **`telegram_notifier.py`** — Renamed `notify_daily_summary()` → `maybe_send_daily_summary()` to clarify it's date-gated and safe to call on every tick.
- **`main.py`** — Trade recovery (`_recover_trades()`) now runs on every startup (idempotent — no active trades = no-op). Added `_maybe_send_daily_summary()` to main event loop (called every 10s, date-gated inside notifier). Removed crash flag write/cleanup.
- **`deployment/WINDOWS_DEPLOYMENT.md`** — Removed health_check.ps1 Task Scheduler section; added note that NSSM is the sole process supervisor.
- **`PROJECT_CONTEXT.md`** — Updated threading model, module descriptions, and resilience section.

---

## [0.9.0] - 2026-03-04

### Added - Hardened Operations

- **`position_closer.py`** (NEW) — `PositionCloser` class: two-phase mark-price position closer for the dashboard kill switch. Phase 1 places limit orders at mark price (5 min, reprice every 30s). Phase 2 drops to ±10% off mark for aggressive fills on illiquid legs (2 min, reprice every 15s). Runs in background thread with Telegram progress notifications.
- **`LifecycleManager.kill_all()`** (NEW method) — emergency termination: cancels all tracked orders (including fill-manager requoted IDs) and marks every active trade CLOSED. Used by the kill switch before handing off to PositionCloser.
- **`TradeLifecycle.to_dict()` / `from_dict()`** (NEW methods) — full trade serialization and deserialization for crash recovery. `to_dict()` captures all recovery-critical fields; `from_dict()` reconstructs the trade with exit conditions left empty for the caller to re-attach.
- **`LifecycleManager.restore_trade()`** (NEW method) — re-injects a recovered `TradeLifecycle` into the active trade tracking dict.
- **Crash recovery** — `main.py` writes a `logs/.running` crash flag on startup; clears it on clean shutdown. On restart with flag present, `_recover_trades()` loads the trade snapshot, verifies exchange positions, re-attaches exit conditions from strategy configs, and normalizes transient states (OPENING→OPEN, CLOSING→PENDING_CLOSE). All-or-nothing: fails to manual intervention if state is inconsistent.
- **Dashboard `/api/killswitch/status`** (NEW route) — poll kill switch progress (idle/phase1/phase2/done).
- **Telegram `notify_strategy_paused()`**, **`notify_strategy_resumed()`**, **`notify_strategy_stopped()`** — new notification helpers for dashboard control actions.

### Changed

- **Dashboard kill switch** — now uses `PositionCloser` (two-phase mark-price close in background thread) instead of the old `force_close` loop. Returns immediately; progress reported via Telegram and the new status endpoint.
- **`persistence.py`** — stripped to trade history only (`save_completed_trade`, `load_trade_history`). Removed `save_trades()`, `load_trades()`, `clear()` — active trade persistence now handled by `LifecycleManager._persist_all_trades()` via `to_dict()`.
- **`telegram_notifier.py`** — daily summary now wall-clock gated at 07:00 UTC (immune to restarts), accepts `positions` tuple with individual position details, removed `margin_utilization` parameter.
- **`strategies/atm_straddle.py`** — `OPEN_HOUR` changed from 12 to 13 (entry window now 13:00–14:00 UTC).

### Fixed

- **Self-shutdown bug** — removed `_enabled = False` side effect from `max_trades_per_day` gate in `strategy.py`. The gate now purely blocks entry without disabling the runner. This was the root cause of the duplicate trade incident (runner disabled → main.py auto-shutdown → NSSM restart → new trade on empty state).
- **Auto-shutdown removed** — `main.py` no longer shuts down when all runners are disabled. The process stays alive for position management and dashboard access.
- **`_check_closed_trades` always runs** — moved above the `if not self._enabled: return` guard in `StrategyRunner.tick()`, so trade close callbacks, persistence, and notifications always fire even when entry is paused.
- **`notify_daily_summary` crash** — method referenced undefined `_last_daily_summary` and `_daily_interval` attributes. Rewritten to use wall-clock `_last_daily_date` date tracking.
- **`kill_all()` state logging** — logged `trade.state.value` after already setting it to CLOSED, always showing "was closed". Now captures `prev_state` before mutation.

---

## [0.8.1] - 2026-03-04

### Added - Executable PnL & Instant Close

- **`TradeLifecycle.executable_pnl()`** (NEW method) — computes PnL using live orderbook best bid/ask instead of mark prices. Uses best bid for closing longs, best ask for closing shorts. Returns `None` when orderbook unavailable (safe skip). Works for any multi-leg structure.
- **`pnl_mode` parameter** on `profit_target()` and `max_loss()` — `"mark"` (default, backward compatible) or `"executable"` (orderbook-based). Documented in PROJECT_CONTEXT.md.
- **Instant close-order placement** — when an exit condition triggers, `close()` is now called in the same tick instead of waiting 10 seconds for the next poll cycle.

### Changed

- `strategies/atm_straddle.py` — switched `profit_target` to `pnl_mode="executable"`
- `main.py` — activated `atm_straddle` as sole strategy (replaced `blueprint_strangle`)
- `account_manager.py` — demoted "Retrieved N open orders" log from INFO to DEBUG
- `trade_lifecycle.py` — demoted requote log messages from INFO to DEBUG
- `PROJECT_CONTEXT.md` — added PnL evaluation modes documentation

### Fixed

- Exit conditions no longer trigger on inflated mark prices that don't reflect executable bid/ask liquidity
- Close orders are placed immediately after exit evaluation, not 10 seconds later

---

## [0.8.0] - 2026-03-03

### Added - Web Dashboard

- **`dashboard.py`** (NEW) — Real-time web dashboard built with Flask + htmx. Runs as a daemon thread inside the existing process. Password-protected via `DASHBOARD_PASSWORD` env var. Silently disabled when not configured.
- **Dashboard features:**
  - Account summary panel (equity, margin, UPnL, net Greeks)
  - Strategy status cards with Pause / Resume / Stop controls
  - Open positions table (symbol, side, qty, entry/mark, UPnL, ROI, delta)
  - Live log tail (last 80 entries, auto-refreshing every 3s via in-memory ring buffer)
  - Kill switch with two-step confirmation (ARM → CONFIRM), sends Telegram alert
  - Session-based login page
  - htmx auto-polling (each panel refreshes independently every 3-5s)
- **`templates/`** (NEW directory) — Jinja2 templates: `dashboard.html`, `login.html`, `_account.html`, `_strategies.html`, `_positions.html`, `_logs.html`
- **`tests/test_dashboard.py`** (NEW) — Standalone dashboard test with mock data (fake positions, strategies, log entries)
- **`DashboardLogHandler`** — `logging.Handler` subclass with ring buffer (deque) to capture recent log entries for display without modifying existing log setup

### Configuration
- Two new optional `.env` variables: `DASHBOARD_PASSWORD` (required to enable), `DASHBOARD_PORT` (default 8080)
- If `DASHBOARD_PASSWORD` is not set, dashboard is completely disabled — zero impact on trading bot

### Architecture Notes
- No changes to any core module (`strategy.py`, `account_manager.py`, `trade_lifecycle.py`, etc.)
- Dashboard reads existing objects: `ctx.position_monitor.latest`, `runner.stats`, `runner._enabled`
- Controls call existing methods: `runner.enable()`, `runner.disable()`, `runner.stop()`, `ctx.lifecycle_manager.force_close()`
- Daemon thread — if dashboard crashes, trading bot is unaffected

### Files Changed
- NEW: `dashboard.py` (~280 lines)
- NEW: `templates/` (6 HTML files)
- NEW: `tests/test_dashboard.py` (~230 lines)
- MODIFIED: `main.py` (+3 lines, import + `start_dashboard()` call)
- MODIFIED: `requirements.txt` (+1 line, `flask>=3.0.0`)

---

## [0.7.1] - 2026-03-03

### Added - Telegram Notifications

- **`telegram_notifier.py`** (NEW) — `TelegramNotifier` class sends high-level alerts to Telegram via Bot API. Fire-and-forget with 1 msg/sec rate limiting; never crashes the bot on failure. Silently no-ops when `TELEGRAM_BOT_TOKEN` is not set.
- **Notifications wired at framework level** — all strategies automatically get trade open/close alerts without per-strategy code:
  - System startup/shutdown
  - Trade opened (strategy, legs, entry cost)
  - Trade closed (PnL, ROI, hold time)
  - Daily account summary (equity, UPnL, margin, delta, positions) — throttled to 1×/day
  - Critical errors (consecutive main loop failures)
- **`TradingContext.notifier`** (`strategy.py`) — optional `TelegramNotifier` field on the DI container
- **`HealthChecker.notifier`** (`health_check.py`) — optional notifier param triggers daily Telegram summary alongside health checks

### Configuration
- Two new optional `.env` variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- If not set, all notifications silently disabled — fully backward compatible

### Files Changed
- NEW: `telegram_notifier.py` (~115 lines)
- MODIFIED: `strategy.py` (+15 lines, notifier on TradingContext, trade open/close notifications)
- MODIFIED: `main.py` (+10 lines, notifier instantiation, startup/shutdown/error alerts)
- MODIFIED: `health_check.py` (+12 lines, daily summary via notifier)

---

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
