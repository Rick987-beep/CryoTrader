# Backtester V2 — Architecture & Implementation Plan

**Date:** 2026-03-25  
**Status:** Implementation Complete — Profiling Outstanding  
**Goal:** Real-data backtester using historic Tardis/Deribit option prices

---

## 1. What We Have Today

| Component | State | Reusable? |
|---|---|---|
| `straddle_strangle.py` | Strategy + param grid + backtest loop in one file | **Extract** — strategy logic reusable, loop/grid must decouple |
| `pricing.py` | BS model, vol estimation, Deribit fees | **Reuse** fee model; BS pricing becomes optional (validation) |
| `metrics.py` | Stats, equity curves, Sortino/Calmar/scoring | **Reuse as-is** — already strategy-agnostic |
| `reporting.py` | Console + HTML report generation | **Moved to archive** — `reporting_v2.py` built instead (no V1 coupling) |
| `data.py` | Binance hourly candles | **Replace** — swapped out entirely |
| `HistoricOptionChain` | Fast parquet-backed lookup (~40µs/option) | **Build on** — core data access layer |
| Tardis parquet data | 15 days (Mar 9–23, 2026), ~600–750 MB/day, 13 GB total | **Primary data source** |

### Key Limitation of V1
The entire strategy evaluation loop (walk candles → BS pricing → check triggers → record PnL) is a single monolithic function. Strategy, data source, parameter iteration, and market simulation are entangled.

---

## 2. Design Decisions: Market Replay & Data Format

### 2.1 The Memory Problem

**Raw parquet data:** 13 GB across 15 files (600–750 MB each, ~22–27M rows/day).  
**Machine:** M1 Mac, 16 GB RAM.  
**Conclusion:** Cannot load all 15 files simultaneously.

### 2.2 Approach: Strategy-Scoped Snapshots + Spot Track

**Inspiration:** Zipline (Quantopian), Backtrader, and QuantConnect all separate data ingestion from strategy execution. The standard pattern is to pre-aggregate tick data into regular time bars, then run strategies against those bars.

For options this means: instead of tick-level binary-search lookups at runtime, we **pre-build 5-minute-resolution snapshots** of the option chain, scoped to the expiries each strategy actually needs.

#### Why 5-minute resolution for options?
- Our strategies operate on hour-level timescales (not HFT)
- V1 used *hourly* candles — 5-min is already 12× finer
- Reduces data volume by ~500× vs tick-level
- At 5-min, 400 days of strategy-scoped data fits comfortably in 16 GB RAM
- Options prices don't change meaningfully within 5 minutes for our strategies

#### Why a separate 1-minute spot price track?
The BTC index price *does* move fast enough to matter for excursion triggers. A $500 BTC move (~0.6%) can happen in under 5 minutes during volatile periods. Spot data is tiny (one float per minute), so we keep it at 1-min resolution cost-free.

- **Options snapshots:** 5-min intervals (11:00, 11:05, 11:10, ...)
- **Spot track:** 1-min OHLC bars (open/high/low/close per minute) for precise excursion detection

#### Why strategy-scoped?
Most strategies only use 1–2 expiries (0DTE straddle uses nearest 0DTE; put-selling uses 1DTE). Loading all 12 expiries when a strategy needs 1 wastes ~90% of the data. The snapshot builder takes an **expiry filter** parameter so each strategy's data bundle contains only relevant instruments.

#### Snapshot format: one row per (5-min interval, expiry, strike, is_call)

**Option snapshot file:**

| Column | Type | Description |
|---|---|---|
| `timestamp` | int64 | 5-min-aligned timestamp (µs, floored) |
| `expiry` | category | Expiry code ("9MAR26") |
| `strike` | float32 | Strike price USD |
| `is_call` | bool | Call/put flag |
| `underlying_price` | float32 | BTC spot at this interval |
| `bid_price` | float32 | Best bid (BTC) — last known at 5-min boundary |
| `ask_price` | float32 | Best ask (BTC) |
| `mark_price` | float32 | Mark price (BTC) |
| `mark_iv` | float32 | Mark implied vol (%) |
| `delta` | float32 | Option delta |

10 columns × 4 bytes avg = ~40 bytes/row.

**Spot track file (separate):**

| Column | Type | Description |
|---|---|---|
| `timestamp` | int64 | 1-min-aligned timestamp (µs) |
| `open` | float32 | Spot at start of minute |
| `high` | float32 | Max spot during minute |
| `low` | float32 | Min spot during minute |
| `close` | float32 | Spot at end of minute |

5 columns × 4 bytes = 20 bytes/row. Shared by all strategies.

#### Size estimates

**Per-strategy option snapshots (0DTE only = ~40 instruments × 2 types):**

| Timeframe | 5-min intervals/day | Rows/day | File size/day | Total |
|---|---|---|---|---|
| 15 days | 288 | ~23,000 | ~0.9 MB | **~14 MB** |
| 400 days | 288 | ~23,000 | ~0.9 MB | **~360 MB** |

**Full chain (all expiries, ~200 instruments):**

| Timeframe | Rows/day | File size/day | Total |
|---|---|---|---|
| 15 days | ~57,600 | ~2.3 MB | **~35 MB** |
| 400 days | ~57,600 | ~2.3 MB | **~920 MB** |

**Spot track (1-min, strategy-agnostic):**

| Timeframe | Rows/day | File size/day | Total |
|---|---|---|---|
| 15 days | 1,440 | 28 KB | **~420 KB** |
| 400 days | 1,440 | 28 KB | **~11 MB** |

**Bottom line:** Even at 400 days with all expiries, total data is <1 GB. Strategy-scoped 0DTE snapshots: <400 MB. Spot track: always negligible.

#### Pre-computation cost

Run once per new data day (~60–90s per day via `HistoricOptionChain`). Store results per strategy scope:
- `options_20260309_20260323.parquet` (all expiries; filtered to strategy scope at load time)
- `spot_track_20260309_20260323.parquet` (shared spot OHLC)

### 2.3 Alternative Considered: Day-at-a-Time Streaming

Load one parquet at a time via `HistoricOptionChain`, run strategy per day, stitch results. Rejected because:
- Strategies that hold positions overnight can't see the next day's chain
- Extra complexity managing day boundaries
- The snapshot approach is both simpler and faster

### 2.4 Alternative Considered: NumPy Tensor Cube

Pre-load all data into a 4D NumPy array indexed by `[minute_idx, expiry_idx, strike_idx, field_idx]`. Direct O(1) array access instead of hash lookups. Rejected because:
- The strike grid is sparse and varies per expiry — wastes memory with NaN padding
- Complexity of index mapping outweighs benefit for our scale
- Minute snapshots in a pandas DataFrame with categorical expiry + MultiIndex give us "fast enough" (sub-ms lookups via `.loc`)

### 2.5 Chosen Architecture: Three-Layer Data Stack

```
Layer 1: Raw parquet (tick-level, 13 GB on disk, existing)
    ↓  snapshot_builder.py (run once per strategy scope)
Layer 2a: Option snapshots (5-min, parquet on disk, ~14–360 MB)
Layer 2b: Spot track (1-min OHLC, parquet on disk, ~0.4–11 MB)
    ↓  market_replay.py (loads into memory at backtest time)
Layer 3: In-memory runtime data (pandas/NumPy, strategy-scoped subset)
    ↓
Strategy code (sees: current time, spot OHLC, option chain snapshot)
```

**Layer 1** stays as-is (source of truth). **Layer 2** files are built once and stored on disk. **Layer 3** is ephemeral — exists only during a backtest run.

#### Layer 1 → Layer 2: One-time snapshot build (runs rarely)

- **Trigger:** Only when new raw tick data arrives from Tardis (e.g., you download a new week of data).
- **What it does:** Reads raw tick parquets one day at a time via `HistoricOptionChain`, samples at 5-min intervals, writes compressed parquet files to `backtester2/snapshots/`.
- **Format:** Parquet with zstd compression. No database — plain files. Parquet gives us columnar reads, type safety, and instant schema inspection.
- **Output files:**
  - `spot_track_YYYYMMDD_YYYYMMDD.parquet` — shared, built once for all strategies
  - `options_YYYYMMDD_YYYYMMDD.parquet` — full option chain (all expiries)
- **Idempotent:** If the snapshot already covers a date range, it's skipped. Adding new data days appends.

#### Layer 2 → Layer 3: Strategy-scoped runtime load (runs every backtest)

- **Trigger:** Every time you run a backtest.
- **What it does:** `MarketReplay.__init__()` loads the snapshot parquet into memory and filters to only the expiries the strategy needs. The result lives in Python memory as:
  - A pandas DataFrame of option quotes, grouped by 5-min timestamp → dict-like access
  - NumPy arrays for the spot track (timestamps, open, high, low, close)
  - Pre-computed cumulative max/min arrays for instant excursion lookups
- **No extra files written** — this is a pure in-memory filtering step.
- **Cost:** ~0.1–0.5s load + filter for 15 days; ~1–3s for 400 days.
- **Why not pre-filter on disk?** The full snapshot is small enough (<1 GB even at 400 days). Filtering at load time keeps the file count low and avoids rebuild when you add a strategy. One snapshot file serves all strategies.

The spot track is built once and shared by all strategies. Option data is filtered at runtime based on what the strategy declares it needs.

---

## 3. Module Architecture

```
backtester2/
├── run.py                   # V2 entry point / CLI
├── snapshot_builder.py      # Layer 1→2: tick parquet → 5-min snapshots + 1-min spot track
├── market_replay.py         # Core: iterates snapshots, provides MarketState + spot track
├── engine.py                # Orchestrator: run_grid() + run_grid_full()
├── strategy_base.py         # Strategy protocol + Trade/OpenPosition + condition helpers
│                            #   incl. close_trade() helper, at_interval() condition
├── strategies/
│   ├── __init__.py
│   ├── straddle_strangle.py # Long straddle/strangle + index move exit
│   └── daily_put_sell.py   # Short OTM put, SL or expiry exit
├── pricing.py               # Existing: BS model, vol estimation, Deribit fees
├── reporting_v2.py          # Strategy-agnostic HTML reports, no V1 coupling
└── snapshots/               # Generated snapshot artifacts (gitignored)
    ├── spot_track_20260309_20260323.parquet
    └── options_20260309_20260323.parquet
```

### 3.1 Module Responsibilities

#### `snapshot_builder.py` — Build 5-Min Snapshots + Spot Track (one-time)

**Input:** Directory of raw tick parquet files  
**Output:** Two parquet files in `backtester2/snapshots/`:
- `options_YYYYMMDD_YYYYMMDD.parquet` — all expiries, 5-min resolution
- `spot_track_YYYYMMDD_YYYYMMDD.parquet` — 1-min OHLC, shared

**When to run:** Only when new raw tick data arrives from Tardis. Not part of the backtest loop.

Process:
1. **Spot track** (built first, fast):
   - For each raw parquet file (one per day):
     - Group by minute, compute OHLC of `underlying_price`
   - Concatenate all days, write single parquet

2. **Option snapshots** (all expiries):
   - For each raw parquet file:
     - Load via `HistoricOptionChain`
     - Sample at 5-min intervals (take last-known update at each boundary)
     - For each 5-min interval × expiry × strike × call/put:
       - Record last-known bid/ask/mark/iv/delta
   - Concatenate all days, write single parquet

3. **Idempotent:** Skip days already covered by existing snapshot. When new data arrives, rebuild covering the full date range.

**Performance target:** ~60–90s per day, ~15–20 min for full 15-day build (one-time).

**Note:** We store ALL expiries in the snapshot file. Filtering to strategy-relevant expiries happens at load time in `MarketReplay`, not here. This keeps the build step simple and means adding a new strategy never requires rebuilding snapshots.

#### `market_replay.py` — Market State Iterator

**Input:** Strategy-scoped option snapshot + spot track (loaded into RAM)  
**Output:** Iterator yielding `MarketState` objects at each 5-min time step

```python
@dataclass
class OptionQuote:
    strike: float
    is_call: bool
    expiry: str
    bid: float          # BTC-denominated
    ask: float
    mark: float
    mark_iv: float
    delta: float
    # bid_usd / ask_usd / mark_usd — computed @property (bid/ask/mark × spot)
    # Not stored fields — calculated on access to save memory

@dataclass
class SpotBar:
    """1-minute OHLC bar for BTC spot price."""
    timestamp: int
    open: float
    high: float
    low: float
    close: float

@dataclass
class MarketState:
    timestamp: int            # Microseconds (5-min aligned)
    dt: datetime              # UTC datetime
    spot: float               # BTC/USD (close of current 5-min bar)
    spot_bars: List[SpotBar]  # 1-min bars since last MarketState (up to 5)

    def get_option(self, expiry, strike, is_call) -> Optional[OptionQuote]:
        """Single option lookup."""

    def get_chain(self, expiry) -> List[OptionQuote]:
        """All options for one expiry."""

    def get_atm_strike(self, expiry) -> float:
        """ATM strike (nearest to spot)."""

    def get_straddle(self, expiry, strike=None) -> Tuple[OptionQuote, OptionQuote]:
        """ATM or specific-strike call+put pair."""

    def get_strangle(self, expiry, offset) -> Tuple[OptionQuote, OptionQuote]:
        """OTM call+put pair at ±offset from ATM."""

    def expiries(self) -> List[str]:
        """Available expiries at this moment."""

    def spot_high_since(self, entry_time: int) -> float:
        """Highest spot since entry_time (from spot track). For excursion."""

    def spot_low_since(self, entry_time: int) -> float:
        """Lowest spot since entry_time (from spot track). For excursion."""
```

**Implementation:** 
- Pre-group the option snapshot DataFrame by 5-min interval
- Load the full spot track (1-min OHLC) as NumPy arrays for fast range queries
- On each `next()`, create `MarketState` with:
  - Current option chain slice (dict-keyed by `(expiry, strike, is_call)`) — O(1) per option
  - The 5 spot bars since last state (or fewer at boundaries)
  - Running high/low lookups via pre-computed cumulative max/min arrays on the spot track

**Key design choice:** `MarketReplay` is a simple iterator, not an event bus. No pub/sub, no callbacks. Strategies pull data via `MarketState`, they don't register handlers. This keeps the hot loop tight and Pythonic.

```python
class MarketReplay:
    def __init__(self, snapshot_path: str,
                 spot_track_path: str,
                 start: Optional[TimeArg] = None,
                 end: Optional[TimeArg] = None,
                 step_minutes: int = 5):
        """Load snapshot + spot track and configure time range."""

    def __iter__(self) -> Iterator[MarketState]:
        """Yield MarketState for each 5-min step."""

    @property
    def timestamps(self) -> np.ndarray:
        """All available 5-min timestamps."""

    @property
    def time_range(self) -> Tuple[datetime, datetime]:
        """Data coverage."""

    @property
    def spot_track(self) -> np.ndarray:
        """Full 1-min spot array (for vectorized excursion precompute)."""
```

**`step_minutes` parameter:** Defaults to 5 (matching snapshot resolution). Can be set to 10, 15, or 60 for coarser/faster grid scans. Cannot go below 5 without rebuilding snapshots.

#### `strategy_base.py` — Strategy Protocol + Building Blocks

The backtester's strategy definition is intentionally simpler than the production `StrategyConfig` — no execution routing, no RFQ, no account management. What remains is the pure signal logic: **when to enter, what to hold, when to exit, and how to price it.**

The design mirrors production's compositional pattern (entry/exit conditions as callables) but strips out everything related to live execution.

##### Core Data Types

```python
@dataclass
class Trade:
    entry_time:      datetime
    exit_time:       datetime
    entry_spot:      float        # BTC spot at entry
    exit_spot:       float        # BTC spot at exit
    entry_price_usd: float        # Total premium paid/received (all legs, USD)
    exit_price_usd:  float        # Total premium at close (all legs, USD)
    fees:            float        # Round-trip Deribit fees
    pnl:             float        # Net P&L after fees
    triggered:       bool         # Whether primary exit trigger fired (vs forced/time)
    exit_reason:     str          # "trigger", "time_exit", "max_hold", "expiry", "end_of_data"
    exit_hour:       int          # Hours held (for V1 metrics compat)
    entry_date:      str          # "YYYY-MM-DD"
    metadata:        dict         # Strategy-specific (legs, strikes, trigger, etc.)

@dataclass
class OpenPosition:
    """Internal state held by strategy while a trade is open."""
    entry_time:      datetime
    entry_spot:      float
    legs:            List[dict]   # [{strike, is_call, expiry, side, qty, entry_price}, ...]
    entry_price_usd: float        # Total premium paid/received (sum of legs)
    fees_open:       float        # Entry fees
    metadata:        dict         # Strategy-specific data (entry_index, trigger, etc.)
```

##### Strategy Protocol

```python
class Strategy(Protocol):
    """Protocol for backtest strategies.

    Lifecycle:
        1. configure(params)         — set parameters for this run
        2. on_market_state(state)    — called each 5-min tick. Enter/exit/hold.
        3. on_end()                  — force-close at end of data
        4. reset()                   — clear state for next run (reuse instance)
    """

    name: str

    def configure(self, params: Dict[str, Any]) -> None:
        """Set parameters for this run."""

    def on_market_state(self, state: MarketState) -> List[Trade]:
        """Process one time step. Return completed trades (if any)."""

    def on_end(self, state: MarketState) -> List[Trade]:
        """Force-close any open positions. Called once at end of data."""

    def reset(self) -> None:
        """Clear internal state (open positions). Called between grid runs."""

    def describe_params(self) -> Dict[str, Any]:
        """Return current parameters for result labeling."""
```

##### Entry & Exit Condition Helpers (composable, like production)

```python
# ── Entry conditions: (MarketState) → bool ──

def time_window(start_hour: int, end_hour: int):
    """Allow entry only during UTC hour range."""
    def check(state: MarketState) -> bool:
        return start_hour <= state.dt.hour < end_hour
    return check

def weekday_only():
    """Block entries on Saturday/Sunday."""
    def check(state: MarketState) -> bool:
        return state.dt.weekday() < 5
    return check

def max_entries_per_day(n: int):
    """Limit entries to N per calendar day. Needs strategy state."""
    # Passed as param; strategy tracks its own count
    ...

# ── Exit conditions: (MarketState, OpenPosition) → Optional[str] ──
# Return None to hold, or a reason string to exit.

def index_move_trigger(distance_usd: float):
    """Exit when BTC moves ≥ distance from entry."""
    def check(state: MarketState, pos: OpenPosition) -> Optional[str]:
        excursion = abs(state.spot - pos.entry_spot)
        # Also check 1-min spot bars for intra-bar excursion
        for bar in state.spot_bars:
            excursion = max(excursion, abs(bar.high - pos.entry_spot),
                           abs(pos.entry_spot - bar.low))
        if excursion >= distance_usd:
            return "trigger"
        return None
    return check

def max_hold_hours(hours: int):
    """Force-close after N hours held."""
    def check(state: MarketState, pos: OpenPosition) -> Optional[str]:
        held = (state.dt - pos.entry_time).total_seconds() / 3600
        if held >= hours:
            return "max_hold"
        return None
    return check

def time_exit(hour: int, minute: int = 0):
    """Hard close at specific UTC wall-clock time."""
    def check(state: MarketState, pos: OpenPosition) -> Optional[str]:
        if state.dt.hour >= hour and state.dt.minute >= minute:
            if pos.entry_time.date() == state.dt.date():
                return "time_exit"
        return None
    return check

def stop_loss_pct(pct: float):
    """Close when unrealized loss exceeds pct% of entry premium."""
    def check(state: MarketState, pos: OpenPosition) -> Optional[str]:
        current_value = _reprice_position(state, pos)
        loss_pct = (current_value - pos.entry_price_usd) / abs(pos.entry_price_usd)
        if pos.metadata.get("direction") == "sell":
            loss_pct = -loss_pct  # For short premium, loss = price going up
        if loss_pct <= -pct:
            return "stop_loss"
        return None
    return check

def profit_target_pct(pct: float):
    """Close when unrealized profit reaches pct% of entry premium."""
    def check(state: MarketState, pos: OpenPosition) -> Optional[str]:
        current_value = _reprice_position(state, pos)
        profit_pct = (current_value - pos.entry_price_usd) / abs(pos.entry_price_usd)
        if pos.metadata.get("direction") == "sell":
            profit_pct = -profit_pct  # For short, profit = price going down
        if profit_pct >= pct:
            return "profit_target"
        return None
    return check
```

##### How Strategies Use These Building Blocks

The strategy's `on_market_state()` follows a simple pattern:

```python
def on_market_state(self, state):
    trades = []

    # 1. Check exits on open position
    if self._position:
        for exit_cond in self._exit_conditions:
            reason = exit_cond(state, self._position)
            if reason:
                trades.append(self._close(state, reason))
                break

    # 2. Check entry if flat
    if not self._position:
        if all(cond(state) for cond in self._entry_conditions):
            self._open(state)

    return trades
```

This maps directly to production's StrategyRunner pattern:
- `entry_conditions` → all must pass (AND logic)
- `exit_conditions` → any triggers close (OR logic, first match wins)
- One position at a time (configurable via `max_concurrent_trades` later)

---

#### Concrete Strategy Implementations

##### `strategies/straddle_strangle.py` — Long Straddle/Strangle + Index Extrusion

Maps to production's `atm_straddle_index_move`. The extrusion exit uses 1-min spot bars for precise trigger detection.

```python
class ExtrusionStraddleStrangle:
    """Buy 0DTE ATM straddle or OTM strangle, exit on BTC index move."""

    name = "extrusion_straddle_strangle"

    # ── Grid parameters ──
    # Full grid: 7 offsets × 10 triggers × 12 holds × 6 hours = 5,040 combos.
    # The PARAM_GRID below is a scoped subset; adjust values to taste.
    # entry_hour is a GRID DIMENSION (not a filter): each combo tries one
    # specific hour, weekdays only, one trade per day.
    PARAM_GRID = {
        "offset":        [0, 500, 1000, 1500, 2000, 2500, 3000],
        "index_trigger": [300, 400, 500, 600, 700, 800, 1000, 1200, 1500, 2000],
        "max_hold":      [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
        "entry_hour":    [3, 6, 9, 12, 15, 19],   # ← grid dim (not a filter)
    }

    def configure(self, params):
        self.offset = params["offset"]
        self.trigger = params["index_trigger"]
        self.max_hold = params["max_hold"]
        self.entry_hour = params.get("entry_hour", 9)   # fixed UTC hour
        self.pricing_mode = params.get("pricing_mode", "real")
        self._position = None

        # Compose conditions from params
        self._entry_conditions = [
            weekday_only(),
            time_window(self.entry_hour, self.entry_hour + 1),  # 1-hour window
        ]
        self._exit_conditions = [
            index_move_trigger(self.trigger),
            max_hold_hours(self.max_hold),
        ]

    def _open(self, state):
        expiry = _nearest_valid_expiry(state)  # 0DTE before 08:00, ~1DTE after
        call, put = state.get_strangle(expiry, self.offset)  # offset=0 → straddle
        # Price at ask (buying), convert BTC→USD
        entry_usd = (call.ask_usd + put.ask_usd)
        fees = deribit_fee_per_leg(state.spot, call.ask_usd) + \
               deribit_fee_per_leg(state.spot, put.ask_usd)
        self._position = OpenPosition(
            entry_time=state.dt, entry_spot=state.spot,
            legs=[{...call...}, {...put...}],
            entry_price_usd=entry_usd, fees_open=fees,
            metadata={"offset": self.offset, "expiry": expiry, "direction": "buy"},
        )

    def _close(self, state, reason):
        # Price at bid (selling)
        call, put = ...  # Look up current prices for held strikes
        exit_usd = (call.bid_usd + put.bid_usd)
        fees_close = ...
        pnl = exit_usd - self._position.entry_price_usd \
              - self._position.fees_open - fees_close
        # close_trade() helper from strategy_base handles PnL formula + Trade creation
        trade = close_trade(state, self._position, reason, exit_usd, fees_close)
        self._position = None
        return trade
```

##### `strategies/daily_put_sell.py` — Short OTM Put (simplified)

Maps to production's `daily_put_sell`. Simplified: no RFQ, no phased execution, no EMA filter. Pure signal logic.

```python
class DailyPutSell:
    """Sell 1DTE OTM put daily, exit on stop-loss or expiry."""

    name = "daily_put_sell"

    # ── Grid parameters ──
    PARAM_GRID = {
        "target_delta":  [-0.05, -0.10, -0.15, -0.20],
        "stop_loss_pct": [0.5, 0.7, 1.0, 1.5, 2.0],
        "entry_hour":    [3],  # Fixed for now; expandable
    }

    def configure(self, params):
        self.target_delta = params["target_delta"]
        self.sl_pct = params["stop_loss_pct"]
        self.entry_hour = params.get("entry_hour", 3)
        self.pricing_mode = params.get("pricing_mode", "real")
        self._position = None
        self._trades_today = 0
        self._last_date = None

        self._entry_conditions = [
            weekday_only(),
            time_window(self.entry_hour, self.entry_hour + 1),
        ]
        self._exit_conditions = [
            stop_loss_pct(self.sl_pct),
            # No explicit TP — held to expiry if SL doesn't hit
        ]

    def on_market_state(self, state):
        trades = []

        # Reset daily counter
        today = state.dt.date()
        if today != self._last_date:
            self._trades_today = 0
            self._last_date = today

        # Check if position expired (past expiry time)
        if self._position:
            if _is_expired(state, self._position):
                trades.append(self._close(state, "expiry"))

        # Check exit conditions on open position
        if self._position:
            for exit_cond in self._exit_conditions:
                reason = exit_cond(state, self._position)
                if reason:
                    trades.append(self._close(state, reason))
                    break

        # Check entry if flat + max 1 per day
        if not self._position and self._trades_today < 1:
            if all(cond(state) for cond in self._entry_conditions):
                self._try_open(state)

        return trades

    def _try_open(self, state):
        expiry = _nearest_1dte_expiry(state)
        chain = state.get_chain(expiry)
        # Find put nearest to target_delta
        puts = [q for q in chain if not q.is_call and q.delta is not None]
        best = min(puts, key=lambda q: abs(q.delta - self.target_delta))
        if best.bid_usd < 1:  # Skip if premium too low
            return
        # Sell at bid (conservative: worst fill for seller)
        entry_usd = best.bid_usd  # We receive premium
        fees = deribit_fee_per_leg(state.spot, best.bid_usd)
        self._position = OpenPosition(
            entry_time=state.dt, entry_spot=state.spot,
            legs=[{"strike": best.strike, "is_call": False, "expiry": expiry,
                   "side": "sell", "entry_price": best.bid}],
            entry_price_usd=entry_usd, fees_open=fees,
            metadata={"target_delta": self.target_delta, "actual_delta": best.delta,
                       "expiry": expiry, "direction": "sell"},
        )
        self._trades_today += 1

    def _close(self, state, reason):
        leg = self._position.legs[0]
        if reason == "expiry":
            # At expiry: if spot > strike, put expires worthless (profit = full premium)
            # If spot < strike, put is ITM (loss = strike - spot, capped at premium direction)
            exit_usd = max(0, leg["strike"] - state.spot)  # Intrinsic value owed
        else:
            # Mid-trade exit: buy back at ask
            quote = state.get_option(leg["expiry"], leg["strike"], is_call=False)
            exit_usd = quote.ask_usd if quote else 0

        fees_close = deribit_fee_per_leg(state.spot, exit_usd)
        # Short premium PnL: received - paid_to_close - fees
        pnl = self._position.entry_price_usd - exit_usd \
              - self._position.fees_open - fees_close
        trade = Trade(
            entry_time=self._position.entry_time, exit_time=state.dt,
            entry_spot=self._position.entry_spot, exit_spot=state.spot,
            entry_price_usd=self._position.entry_price_usd,
            exit_price_usd=exit_usd, fees=self._position.fees_open + fees_close,
            pnl=pnl, triggered=(reason == "stop_loss"), exit_reason=reason,
            exit_hour=int((state.dt - self._position.entry_time).total_seconds() / 3600),
            entry_date=self._position.entry_time.strftime("%Y-%m-%d"),
            metadata={**self._position.metadata, "sl_pct": self.sl_pct},
        )
        self._position = None
        return trade
```

##### How the two strategies compare

| Aspect | Extrusion Straddle | Daily Put Sell |
|---|---|---|
| **Direction** | Long premium (buy) | Short premium (sell) |
| **Legs** | 2 (call + put) | 1 (put only) |
| **Expiry** | 0DTE (same-day) | 1DTE (next-day) |
| **Entry pricing** | Buy at ask | Sell at bid |
| **Exit pricing** | Sell at bid | Buy at ask (or expire) |
| **Primary exit** | Index move trigger | Stop-loss or expiry |
| **PnL formula** | exit_received - entry_paid - fees | entry_received - exit_cost - fees |
| **Grid params** | offset, trigger, max_hold (840 combos) | delta, stop_loss (20 combos) |
| **Entry window** | 00:00–20:00 UTC | 03:00–04:00 UTC |

Both follow the same `Strategy` protocol and share the same entry/exit condition helpers. The engine doesn't care — it just calls `on_market_state()` and collects trades.

**Dual pricing modes (real vs BS):**

Each strategy can run in two pricing modes, set via `configure()`:

| Mode | Entry price | Exit price | Use case |
|---|---|---|---|
| `"real"` | Ask from Tardis data | Bid from Tardis data | Ground truth backtest |
| `"bs"` | BS-calculated (spot + IV + DTE) | BS-calculated | Model validation, fallback when data is missing |

- **`real` mode (default):** Buy at ask, sell at bid (worst fill — conservative). Slippage starts at 0 since spread already accounts for market friction.
- **`bs` mode:** Uses `pricing.py` Black-Scholes with the IV and spot from the snapshot data (so it still uses real IV, not estimated vol). Applies the existing 4% slippage model.
- **Comparison runs:** Run the same combo in both modes to quantify model bias (how much does BS overestimate/underestimate vs real fills?).
- **Fallback:** If a specific option quote has NaN bid/ask in real data (illiquid strike), the strategy can fall back to BS pricing for that leg and flag the trade.
- **Fees:** Same Deribit fee model from `pricing.py` in both modes.

**Expiry selection:** The strategy picks which expiry to trade. In V1 this was implicit (BS with fixed DTE). In V2, the strategy selects the nearest 0DTE expiry from `state.expiries()`. This is a strategy-level concern, not a market replay concern.

#### `engine.py` — Orchestrator

Connects parameter grids to strategies to market replay. This is the "run N backtests fast" module.

```python
def run_grid(
    strategy_cls,           # Strategy class
    param_grid: Dict[str, List],  # {"offset": [0,500,...], "trigger": [300,400,...]}
    replay: MarketReplay,   # Pre-loaded market data
) -> Dict[Tuple, List[Trade]]:
    """Run all parameter combos against market data.

    Returns: dict of param_tuple → list of Trade
    """
```

**Performance strategy for 10,000+ parameter combos:**

The naive approach (create `MarketReplay` → iterate all minutes → done, repeat for next combo) would be O(combos × minutes), which is fine for minutes but wasteful because we're re-scanning the same market data for each combo.

**Better: single-pass multi-strategy evaluation.**

```
For each minute in market data:
    state = MarketState(minute)
    For each active parameter combo:
        trades = strategy_instance.on_market_state(state)
        results[combo].extend(trades)
```

This is **O(intervals × combos)** in the inner loop, but the `MarketState` construction happens only once per 5-min interval. With 4,320 intervals (15 days × 288/day) and 840 combos, the inner loop runs ~3.6M iterations — very fast.

**At 400 days:** 400 × 288 = 115,200 intervals × 840 combos = ~97M iterations. Still manageable in pure Python (~10–20s).

**Optimization 1: Vectorized entry/exit checks.**
Instead of 840 separate strategy instances, group by shared structure (same offset = same strikes). For each offset:
- Compute entry prices once (from `MarketState`)
- Check all trigger×hold combos as vectorized comparisons

This reduces the inner loop from 840 to effectively 7 (offsets) × entry logic + bulk trigger checks.

**Optimization 2: Pre-compute spot excursion arrays.**
The 1-min spot track is loaded as a NumPy array. Pre-compute cumulative max/min arrays so that "max BTC price between entry and now" is a single array lookup O(1), not a scan. This makes excursion triggers essentially free regardless of hold duration.

**Estimated runtime (V2) — 15 days:**
- Snapshot load: ~0.5s (14 MB strategy-scoped parquet)
- 5-min iteration (naive): ~5–10s for 840 combos
- 5-min iteration (vectorized): ~1–3s
- Metrics + reporting: ~5–10s
- **Total: ~10–25s**

**Estimated runtime (V2) — 400 days:**
- Snapshot load: ~3s (360 MB)
- 5-min iteration (naive): ~20–40s
- 5-min iteration (vectorized): ~5–10s
- Metrics + reporting: ~10–20s
- **Total: ~20–70s**

#### `run.py` — CLI Entry Point

```python
"""
Usage:
    python -m analysis.backtester.run                    # Default strategy, default params
    python -m analysis.backtester.run --strategy extrusion --grid full
    python -m analysis.backtester.run --strategy extrusion --offset 0 --trigger 500 --max-hold 4
    python -m analysis.backtester.run --rebuild-snapshots
    python -m analysis.backtester.run --report-only results.json
"""
```

Steps:
1. Build/load minute snapshots (cached)
2. Instantiate strategy
3. Generate parameter grid (or use CLI overrides)
4. Run via `engine.run_grid()`
5. Compute metrics via `metrics.compute_stats()` + `metrics.compute_equity_metrics()`
6. Generate report via `reporting.generate_html()`

---

## 4. Data Flow Diagram

```
┌──────────────────────── ONE-TIME PREP ────────────────────────────────┐
│                                                                       │
│  tardis_options/data/btc_2026-03-09.parquet  (748 MB, tick)          │
│  tardis_options/data/btc_2026-03-10.parquet  (726 MB, tick)          │
│  ...                                                                  │
│  tardis_options/data/btc_2026-03-23.parquet  (700 MB, tick)          │
│                          │                                            │
│                  snapshot_builder.py                                   │
│                  ┌───────┴────────┐                                   │
│                  ▼                ▼                                   │
│  spot_track.parquet    options_all.parquet                        │
│  (1-min OHLC, 420 KB)  (5-min, all expiries, ~35 MB)               │
│  [shared]               [single file, filtered at load time]        │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘

┌──────────────────────── BACKTEST RUN ─────────────────────────────────┐
│                                                                       │
│  spot_track.parquet         ───────┐                                   │
│  options_all.parquet ───────────▼                                   │
│                        MarketReplay                                │
│                    (filters to strategy expiries)                   │
│                               │                                       │
│                               ▼                                       │
│                       MarketState (per 5-min)                         │
│                       + SpotBars (1-min OHLC)                         │
│                       + spot_high_since / spot_low_since              │
│                               │                                       │
│  param_grid  ──► engine ──────┤                                       │
│                               ▼                                       │
│              Strategy.on_market_state(state)                          │
│                               │                                       │
│                               ▼                                       │
│              List[Trade] per combo                                    │
│                               │                                       │
│              metrics → reporting → backtest_v2_report.html            │
│                                                                       │
└───────────────────────────────────────────────────────────────────────┘
```

---

## 5. Interface Contracts

### 5.1 Snapshot Parquet Schemas

**Option snapshot** (`options_YYYYMMDD_YYYYMMDD.parquet`):
```
timestamp:        int64     (µs, floored to 5-min boundary)
expiry:           category  (e.g. "9MAR26")
strike:           float32   (USD)
is_call:          bool
underlying_price: float32   (BTC spot, USD)
bid_price:        float32   (BTC-denominated)
ask_price:        float32   (BTC-denominated)
mark_price:       float32   (BTC-denominated)
mark_iv:          float32   (%)
delta:            float32
```
Sorted by `(timestamp, expiry, strike, is_call)`. Partitioned/indexed by `timestamp`.

**Spot track** (`spot_track_YYYYMMDD_YYYYMMDD.parquet`):
```
timestamp:        int64     (µs, floored to 1-min boundary)
open:             float32   (BTC spot USD, first tick of minute)
high:             float32   (max during minute)
low:              float32   (min during minute)
close:            float32   (last tick of minute)
```
Sorted by `timestamp`. Shared by all strategies.

### 5.2 Trade Record

```python
@dataclass
class Trade:
    entry_time:      datetime
    exit_time:       datetime
    entry_price_usd: float     # Total premium paid (sum of legs, in USD)
    exit_price_usd:  float     # Total premium received (sum of legs, in USD)
    fees:            float     # Total fees (open + close)
    pnl:             float     # exit_price - entry_price - fees
    triggered:       bool      # Whether the strategy's trigger fired
    exit_hour:       int       # Hours held (for compatibility with V1 metrics)
    entry_date:      str       # "YYYY-MM-DD"
    metadata:        dict      # Strategy-specific: offset, trigger, strikes, etc.
```

**Compatibility note:** V1 `metrics.py` expects `(pnl, triggered, exit_h, entry_date)` tuples. The engine converts `Trade` objects to this format before passing to metrics, keeping metrics.py untouched.

### 5.3 Results Dict (engine → metrics/reporting)

Same shape as V1 for backward compat:

```python
results: Dict[Tuple, List[Tuple[float, bool, int, str]]]
# key: param combo tuple (e.g. (500, 10, 800, 4))
# value: list of (pnl, triggered, exit_hour, entry_date)
```

### 5.4 Parameter Grid

```python
param_grid = {
    "offset": [0, 500, 1000, 1500, 2000, 2500, 3000],
    "index_trigger": [300, 400, 500, 600, 700, 800, 1000, 1200, 1500, 2000],
    "max_hold": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
    "max_entry_hour": [20],  # Fixed for now, expandable
}
# itertools.product() → 840 combos (without entry_hour in grid)
# Entry hour is implicit: strategy enters at valid hours within market data
```

**Note on entry_hour in V2:** `entry_hour` IS a grid dimension — each combo specifies one fixed UTC entry hour. This gives fine-grained control over which time of day the strategy enters, which proved important for the index-move strategy where entry timing significantly affects results. Full grid: 7 offsets × 10 triggers × 12 holds × 6 hours = 5,040 combos.

---

## 6. Key Design Decisions Summary

| Decision | Choice | Rationale |
|---|---|---|
| **Option resolution** | 5-minute snapshots | Non-HFT strategies; 12× finer than V1 hourly; scales to 400+ days |
| **Spot resolution** | 1-minute OHLC bars | Precise excursion detection; negligible size (~11 MB at 400 days) |
| **Data scoping** | Runtime expiry filter in MarketReplay | One snapshot file serves all strategies; no rebuild when adding strategies |
| **Snapshot format** | Parquet with zstd | Reuse existing tooling; fast load (~0.5s for 15d scope) |
| **Replay model** | Pull-based iterator | Simple, no framework overhead, easy to debug |
| **Strategy interface** | `typing.Protocol` | Lightweight, duck-typed, no forced inheritance |
| **Pricing** | Dual: real bid/ask (default) + BS mode | Real = ground truth; BS = fallback + model comparison |
| **Fee model** | Reuse `pricing.deribit_fee_per_leg()` | Already validated against production |
| **Grid execution** | Single-pass multi-combo | Market data scanned once; combos evaluated in parallel |
| **Metrics** | Inline in `reporting_v2.py` (`combo_stats`, `equity_metrics`) | V1 `metrics.py` moved to archive |
| **Reporting** | `reporting_v2.py` (built from scratch) | No V1 coupling; auto-discovers params |
| **Entry-hour handling** | Filter, not grid dimension | Reduces combos 21×; strategy sees all entry windows naturally |
| **400-day scaling** | Strategy-scoped <400 MB; spot <11 MB | Fits M1 16 GB RAM with room to spare |

---

## 7. Differences from Professional Backtesting Frameworks

| Feature | Zipline/Backtrader/QC | Our V2 | Why |
|---|---|---|---|
| Broker simulation | Full (slippage, partial fills, margin) | None (direct premium PnL) | Options premium = P&L; no margin sim needed |
| Portfolio management | Multi-asset, rebalancing | Single-position per strategy | Our strategies hold one structure at a time |
| Order types | Market, limit, stop, etc. | Buy-at-ask, sell-at-bid | Sufficient for short-holding premium strategies |
| Event system | pub/sub, event queue | Simple iterator | Less overhead; strategies are stateless between ticks |
| Live trading bridge | Yes (IB, Alpaca, etc.) | No (separate production system) | Our production bot is a different codebase |
| Multi-strategy | Composition, allocation | Isolated runs per strategy | We compare strategies by running them separately |

**What we borrow from the pros:**
- **Zipline:** Separation of data bundles (our snapshots) from strategy logic
- **Backtrader:** `next()` method pattern (our `on_market_state()`)
- **QuantConnect:** Parameter optimization grid (our `engine.run_grid()`)
- **Vectorbt:** Vectorized signal processing for speed (our Optimization 2)

---

## 8. Implementation Plan (Phased)

### Phase 1: Data Foundation (snapshot_builder.py) ✅
- Build `snapshot_builder.py` — one-time raw tick → 5-min snapshot conversion
- Produce full-chain option snapshots (all expiries; strategy filtering at load time)
- Produce 1-min spot track (shared, OHLC bars)
- Validate: row counts, time coverage, spot continuity, option price sanity
- Benchmark: RAM usage, build time, load time, confirm 400-day projections
- **Result:** 128s build, 1,998,184 option rows, 21,573 spot bars

### Phase 2: Market Replay (market_replay.py + strategy_base.py) ✅
- Implement `MarketState` dataclass with option lookup methods
- Implement `MarketReplay` iterator over snapshot data
- Define `Strategy` protocol and `Trade` dataclass
- Unit test: iterate replay, verify spot prices, option quotes

### Phase 3: Strategy Ports ✅
- Port extrusion straddle/strangle from V1 (`strategies/straddle_strangle.py`)
- Port simplified daily put sell (`strategies/daily_put_sell.py`)
- Implement dual pricing: real bid/ask (default) + BS mode
- Handle expiry selection: `_nearest_valid_expiry()` (handles 0DTE before 08:00, ~1DTE after — correct for all entry hours)
- Validate: run single combo of each strategy, verify PnL arithmetic
- **NaN bug found & fixed:** illiquid strikes with NaN bid in raw data; added NaN guards
- **`close_trade()` helper added to `strategy_base`:** encapsulates PnL formula + Trade construction for both long and short legs; imported by both strategies

### Phase 4: Engine + Grid (engine.py) ✅
- Implement `run_grid()` with single-pass multi-combo evaluation (returns V1-compatible tuples)
- `run_grid_full()` returns full Trade objects for reporting
- Validate: full grid produces same structure as V1 `results` dict

### Phase 5: Integration + Reports (run.py + reporting_v2.py) ✅
- Clean CLI (`run.py`, ~115 lines) — no V1 coupling, no shims; uses `run_grid_full`
- **`reporting_v2.py` built from scratch** (not V1 reuse) — strategy-agnostic, works directly with `Dict[Tuple, List[Trade]]`, auto-discovers parameter names
- Report sections: best combo + sparkline, top 20, heatmaps for all 2D param pairs, daily equity, trade log
- Both strategies tested end-to-end:
  - Straddle: 840 combos, 50,025 trades, 20.9s → HTML report
  - Put sell: 20 combos, 160 trades, 5.4s → HTML report
- **`entry_hour` added to straddle grid:** full grid = 5,040 combos; `PARAM_GRID` in code is a prunable subset scoped to current analysis window

### Phase 6: Performance Profiling ⬜
- Profile the hot loop (identify bottlenecks)
- Implement vectorized spot-track for excursion triggers
- Consider Numba/NumPy vectorization if needed
- Target: full 840-combo grid in <30s (currently ~20s — already meets target)

---

## 9. MVP Definition

**"It works" means:**
1. `snapshot_builder.py` produces valid 0DTE option snapshot + spot track from existing 15 days
2. `run.py` with default params (offset=0, trigger=500, max_hold=4) produces:
   - A list of trades with real-data PnL
   - Stats via `metrics.compute_stats()`
   - An HTML report via `reporting.generate_html()`
3. The report accurately reflects real Deribit bid/ask prices, not BS model prices
4. Runtime < 30 seconds end-to-end (excluding one-time snapshot build)

**"It's useful" means (post-MVP):**
- Full 840-combo grid scan completes in <60 seconds
- Composite scoring ranks combos
- HTML report includes heatmaps and equity curves
- Results are directly comparable to V1 (same metrics, same report format)
- Adding a new strategy (e.g., iron condor) requires only a new strategy file implementing the Protocol

---

## 10. Open Questions & Future Considerations

1. **Multi-day positions:** Current data covers 15 days. Strategies that hold overnight need seamless day transitions. The snapshot approach handles this naturally (all days in one file).

2. **Expiry rollover:** When 0DTE expires, the strategy should roll to next day's 0DTE. The strategy handles this, not the replay engine.

3. **Greeks-based strategies:** The snapshot includes delta. Future strategies could use delta/gamma for hedging backtests.

4. **Put-selling strategy:** The `PutSelling/` analysis has a fully specced daily short-put strategy. Porting it as a second strategy would validate the framework's modularity. Would need a `1dte` expiry scope.

5. **Comparison mode:** Run V1 (BS-priced) and V2 (real-priced) on the same date range, same combos. Quantify model bias of BS approach.

6. **More data:** The Tardis pipeline can fetch more days. Architecture is designed for 400+ days. More data = more statistical significance.

7. **Walk-forward optimization:** Split data into train/test periods. Optimize on train, validate on test. Prevents overfitting. Particularly important once we have 3+ months of data.

8. **Adaptive step resolution:** Some strategies may want 1-min option snapshots for specific time windows (e.g., around expiry). Could support a "high-res window" config in snapshot_builder without rebuilding the full dataset.

9. **Snapshot versioning:** As we accumulate months of data, snapshot files get large. Consider date-range partitioned snapshots (e.g., monthly chunks) that can be loaded selectively.
