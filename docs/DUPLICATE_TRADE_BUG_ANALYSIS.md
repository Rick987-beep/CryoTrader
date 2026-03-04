# Duplicate Trade Bug — Root Cause Analysis & Fix Proposals

> **Status:** Analysis complete, no code changes implemented. Ready for dev machine.
> **Date:** 2025-03-04
> **Incident:** 4 BTC option positions opened instead of 2 (two straddles instead of one) during the 12–13 UTC entry window.
> **Net PnL on emergency close:** +$4.03

---

## 1. What Happened (Timeline)

On March 4, 2025, the NSSM Windows service `CoincallTrader` was started for production around 12:34 UTC. Between 12:34 and 14:18 UTC, the service stopped and restarted **multiple times**. Each restart opened a **new** ATM straddle because the bot had no memory of the previous one.

```
12:34:14  — Service started (manual `nssm start`)
13:00:xx  — Trade 1 opened: 71000-strike straddle (id: a84400ba)
13:33:21  — Service received STOP control → exited code 0 → NSSM restarted
13:33:23  — Service restarted (no memory of Trade 1)
13:33:xx  — Trade 2 opened: 70500-strike straddle (id: 7a0620c7)
14:18:xx  — Service received STOP control → exited code 0 → NSSM restarted
~16:18    — Service manually stopped for investigation
```

Result: **2 straddles open = 4 option positions** on the exchange, when only 1 straddle should have existed.

---

## 2. Root Causes (Three Independent Failures)

### 2A. NSSM Restarts on Clean Exit (Configuration Bug)

**File:** NSSM service registry settings
**Setting:** `AppExit Default = Restart`
**Problem:** NSSM is configured to restart the process on ANY exit, including clean exit code 0. When main.py's `shutdown()` calls `sys.exit(0)`, NSSM treats it as "process died" and restarts. This creates an infinite restart loop when the auto-shutdown logic triggers (all strategies completed → `sys.exit(0)` → NSSM restart → strategy loop starts fresh).

**Evidence:** Windows Application Event Log shows every shutdown was preceded by "Service CoincallTrader received STOP control" (event ID 1040). All process exits showed exit code 0. NSSM then immediately restarted the service.

**Why the STOP control?** Two possible sources:
1. The Python auto-shutdown logic: when `all(not r._enabled and not r.active_trades)` is true (which happens after `max_trades_per_day` is reached and the trade closes or force-closes), `shutdown()` → `sys.exit(0)` causes the process to exit. NSSM detects the exit and restarts.
2. An external Windows signal (RDP disconnect, Windows Update, etc.) sends STOP to the service. NSSM then restarts.

In either case, `AppExit Default = Restart` ensures the bot comes back, loses its state, and opens a duplicate trade.

### 2B. No State Recovery on Restart (Code Bug)

**Files:** `main.py` lines 130–224, `persistence.py`, `strategy.py` lines 640–700

**Problem:** The bot saves active trade state to `logs/trade_state.json` every 60 seconds (via `TradeStatePersistence.save_trades()`), and `persistence.py` has a `load_trades()` method — but **main.py never calls it on startup**. The loaded state is never used to restore `LifecycleManager._trades`, so after a restart every in-memory collection is empty:

- `LifecycleManager._trades` → empty dict → `active_trades_for_strategy()` returns `[]`
- `StrategyRunner.all_trades` → empty list → `max_trades_per_day` counter = 0
- `StrategyRunner.active_trades` → empty list → `max_concurrent_trades` gate passes

This means every gate in `_should_enter()` (strategy.py line 640) passes after a restart:
1. **Gate 1** `max_concurrent_trades`: 0 active (empty) < 1 → PASS
2. **Gate 2** `cooldown_seconds`: 0, no cooldown → PASS
3. **Gate 3** `max_trades_per_day`: 0 today (empty `all_trades`) < 1 → PASS
4. **Gate 4** `time_window(12, 13)`: still within window → PASS
5. **Gate 4** `min_available_margin_pct(20)`: margin available → PASS

Result: Bot opens a new trade immediately after restart, duplicating the one already open on the exchange.

### 2C. No Exchange Position Awareness on Entry (Missing Safety Net)

**File:** `strategies/atm_straddle.py` lines 106–137

**Problem:** The `atm_straddle` strategy does NOT use the `no_existing_position_in()` entry condition (which exists in strategy.py line 248). This condition checks the exchange AccountSnapshot for existing positions before allowing entry. If it were present, it would block entry even with empty in-memory state because it queries the LIVE exchange positions.

The `no_existing_position_in()` factory takes a list of symbols, but for a dynamic strategy like ATM straddle (where the strike changes daily), you'd need a broader check — e.g., "no open BTC option positions at all" rather than checking specific symbols.

---

## 3. Secondary Issue: Force-Close Doesn't Close on Exchange

**File:** `trade_lifecycle.py` (search for "force" or "r.stop()")
**File:** `main.py` line 141 (`r.stop()` in shutdown handler)

When `shutdown()` is called, `r.stop()` iterates active trades and "force-closes" them. But this only:
1. Sets `trade.state` to CLOSED in memory
2. Saves state to persistence

It does **NOT** send close orders to the exchange. So after shutdown + restart:
- The positions remain open on Coincall
- The bot has no record of them (in-memory state is empty)
- The bot opens new positions

This is not a direct cause of the duplicate (the duplicate is caused by 2B/2C), but it makes the shutdown → restart scenario worse: the bot thinks it cleaned up but the exchange still has live positions.

---

## 4. Proposed Fixes

### Fix 1: NSSM Configuration (Quick, do first)

Change NSSM to only restart on non-zero (crash) exits:

```powershell
# On the VPS (production):
nssm set CoincallTrader AppExit Default Restart   # keep: restart on crashes
nssm set CoincallTrader AppExit 0 Exit            # NEW: do NOT restart on clean exit (code 0)
```

This prevents the loop: auto-shutdown → exit 0 → restart → duplicate trade.

**Caveat:** If the bot needs to run continuously across days (strategy repeats daily), then `sys.exit(0)` in auto-shutdown is wrong. Instead, the main loop should keep running and re-enable the strategy the next day. See Fix 4.

### Fix 2: Load Persisted State on Startup (Critical)

**File to modify:** `main.py`
**Where:** After `persistence = TradeStatePersistence()` and before the main loop, add state recovery.

Pseudocode:
```python
# After creating persistence and lifecycle_manager, before registering strategies:
saved_state = persistence.load_trades()
if saved_state and saved_state.get("trades"):
    for trade_data in saved_state["trades"]:
        if trade_data["state"] in ("OPEN", "OPENING", "PENDING_CLOSE", "CLOSING"):
            # Verify position still exists on exchange before restoring
            # If exchange confirms position exists → restore to lifecycle_manager
            # If exchange says no position → skip (was closed externally)
            logger.info(f"Recovering trade {trade_data['id']} from persistence")
            # Implementation: lifecycle_manager.restore_trade(trade_data)
```

This requires a new method on `LifecycleManager` (e.g., `restore_trade()`) that:
1. Recreates a `TradeLifecycle` object from the persisted data
2. Verifies the position still exists on the exchange (via account positions API)
3. Re-attaches it to the in-memory `_trades` dict with the correct strategy_id
4. Re-attaches exit conditions from the strategy config

**Complexity:** Medium-high. The `TradeLifecycle` object has many fields (legs, order IDs, fill prices, etc.) that need to be serialized/deserialized correctly. The current `save_trades()` only saves a subset of fields.

**Alternative (simpler):** Instead of full state recovery, just use Fix 3 as the primary guard.

### Fix 3: Add Exchange Position Check to Entry (Simple Safety Net)

**File to modify:** `strategies/atm_straddle.py`
**Where:** Add a new entry condition to the `entry_conditions` list.

The existing `no_existing_position_in(symbols)` requires specific symbols, which is impractical for ATM straddles (strike changes daily). Two options:

**Option A:** Create a new entry condition `no_open_option_positions(underlying="BTC")` that checks the exchange AccountSnapshot for ANY open BTC option positions:

```python
# New factory in strategy.py:
def no_open_option_positions(underlying: str = "BTC") -> EntryCondition:
    """Block entry if any option positions exist for the given underlying."""
    def _check(account: AccountSnapshot) -> bool:
        positions = account.option_positions  # or however positions are accessed
        for pos in positions:
            if underlying in pos.symbol and pos.qty != 0:
                logger.info(f"no_open_option_positions: found {pos.symbol} qty={pos.qty} — blocked")
                return False
        return True
    _check.__name__ = f"no_open_option_positions({underlying})"
    return _check
```

Then in `atm_straddle.py`:
```python
entry_conditions=[
    time_window(OPEN_HOUR, OPEN_HOUR + 1),
    min_available_margin_pct(MIN_MARGIN_PCT),
    no_open_option_positions("BTC"),  # ← NEW: prevent duplicates on restart
],
```

**Option B:** Broader — check the positions API directly in a custom condition, without needing to know the symbol:

```python
# Custom entry condition using the REST API directly:
def no_btc_option_positions() -> EntryCondition:
    """Query exchange for any open BTC option positions."""
    def _check(account: AccountSnapshot) -> bool:
        # account.positions comes from the /positions endpoint
        btc_opts = [p for p in account.positions if "BTC" in p.get("symbol", "") and float(p.get("qty", 0)) != 0]
        if btc_opts:
            logger.info(f"Blocked entry: {len(btc_opts)} existing BTC option position(s)")
            return False
        return True
    _check.__name__ = "no_btc_option_positions"
    return _check
```

**This is the most important fix** — it prevents duplicates regardless of whether state recovery works, because it checks the actual exchange, not in-memory state.

### Fix 4: Eliminate Auto-Shutdown for Daily Strategies

**File to modify:** `main.py` lines 172–176

**Problem:** The auto-shutdown triggers `sys.exit(0)` when all strategies are disabled and have no active trades. For a daily-repeat strategy, this happens every day after the trade closes and `max_trades_per_day` disables the runner. Combined with NSSM restart, it creates a restart cycle.

**Fix:** Instead of shutting down, re-enable the strategy for the next day:

```python
# Replace the auto-shutdown block with:
if runners and all(not r._enabled and not r.active_trades for r in runners):
    # Check if any strategy wants to repeat tomorrow
    has_daily_strategies = any(r.config.max_trades_per_day > 0 for r in runners)
    if has_daily_strategies:
        # Don't shutdown — sleep until next day's entry window
        logger.info("All daily strategies completed — waiting for next trading day")
        # Optionally: sleep until OPEN_HOUR tomorrow, or just keep the loop running
        # The runners will re-enable themselves at midnight UTC or on the next tick 
        # when the date changes and max_trades_per_day resets
        pass  # Let the main loop continue sleeping
    else:
        logger.info("All strategies completed — auto-shutting down")
        shutdown()
```

**Note:** This also requires `StrategyRunner` to re-enable itself when the UTC date changes. Currently, once `_enabled = False`, it stays disabled forever (until restart). Add a date-change check at the start of `tick()`:

```python
# In StrategyRunner.tick() — before checking _enabled:
if self.config.max_trades_per_day > 0:
    today = datetime.now(timezone.utc).date()
    if today != self._last_active_date:
        self._enabled = True
        self._last_active_date = today
        logger.info(f"[{self._strategy_id}] New day detected — re-enabling")
```

### Fix 5: Make Force-Close Actually Close Positions (Important)

**File to modify:** `trade_lifecycle.py`
**Where:** The force-close / `stop()` pathway

When `StrategyRunner.stop()` is called during shutdown, it should send actual close orders to the exchange for any open positions, not just mark them as closed in memory. This ensures that:
1. If shutdown is intentional (user stops the service), positions are cleaned up
2. If NSSM doesn't restart, orphaned positions don't accumulate

Implementation: reuse the existing close pathway (the same one triggered by exit conditions) but in "immediate" mode without waiting for fills. Or integrate with the close_all_positions.py script logic.

### Fix 6: Improve Persistence Serialization (Medium Priority)

**File to modify:** `persistence.py`

Current `save_trades()` only saves a subset of `TradeLifecycle` fields (id, strategy_id, state, created_at, open_legs basic info, entry_cost). For full state recovery (Fix 2), the serialization needs to include:

- All leg details (symbol, qty, side, order_id, fill_price, filled_qty, close order info)
- Trade timing (opened_at, created_at)
- Exit conditions config (or at least strategy_id so they can be re-attached)
- The execution mode
- RFQ action info if applicable

Consider adding `to_dict()` / `from_dict()` methods to `TradeLifecycle` for complete round-trip serialization.

---

## 5. Recommended Implementation Order

| Priority | Fix | Effort | Impact |
|----------|-----|--------|--------|
| 1 | **Fix 1** — NSSM `AppExit 0 Exit` | 1 min | Prevents restart-on-clean-exit loop |
| 2 | **Fix 3** — `no_open_option_positions()` entry condition | 30 min | Prevents duplicates even without state recovery |
| 3 | **Fix 4** — Remove auto-shutdown, add day-rollover | 1–2 hr | Makes daily strategies work as continuous service |
| 4 | **Fix 5** — Force-close sends exchange orders | 2–3 hr | Prevents orphaned positions on shutdown |
| 5 | **Fix 2 + Fix 6** — Full state recovery | 4–6 hr | Complete crash recovery with position reconciliation |

**Minimum viable fix for next production run: Fix 1 + Fix 3.** These two alone prevent the duplicate trade scenario with minimal code changes and no complex state management.

---

## 6. Key File Locations

| File | Lines | What to look at |
|------|-------|-----------------|
| `main.py` | 130–155 | `shutdown()` handler — calls `r.stop()` then `sys.exit(0)` |
| `main.py` | 171–176 | Auto-shutdown check — triggers when all strategies complete |
| `strategy.py` | 248–258 | `no_existing_position_in()` — existing factory, needs broadening |
| `strategy.py` | 593–600 | `active_trades` property — delegates to lifecycle_manager (in-memory) |
| `strategy.py` | 640–700 | `_should_enter()` — all entry gates, all in-memory |
| `strategy.py` | 665–683 | `max_trades_per_day` gate — uses `all_trades` (in-memory, empty on restart) |
| `trade_lifecycle.py` | 425 | `active_trades_for_strategy()` — purely in-memory |
| `trade_lifecycle.py` | ~960+ | Force-close pathway — marks CLOSED but doesn't send orders |
| `persistence.py` | 37–86 | `save_trades()` — saves subset of fields every 60s |
| `persistence.py` | 88–105 | `load_trades()` — EXISTS but is never called from main.py |
| `strategies/atm_straddle.py` | 106–137 | `atm_straddle()` factory — missing `no_existing_position_in` |
| `close_all_positions.py` | entire | Emergency closer — reusable logic for Fix 5 |

---

## 7. Environment Notes

- Python 3.9+ compatibility required (use `Optional[X]`, not `X | None`)
- Production exchange: `https://api.coincall.com`
- NSSM 2.24 at `C:\tools\nssm.exe`
- Service name: `CoincallTrader`
- Logs: `C:\CoincallTrader\logs\trading.log`
- State file: `C:\CoincallTrader\logs\trade_state.json`
- Service is currently **STOPPED** on the VPS — do not restart until fixes are implemented

---

## 8. ADDENDUM: Why Does CoincallTrader Shut Itself Down? (The Real Bug)

> **Core question:** The bot is meant to run forever as a Windows service, monitoring positions and repeating daily. Why does it have shutdown logic that triggers during normal operation?

### 8A. The Self-Shutdown Chain (Step by Step)

There is a **deterministic self-destruction sequence** that fires every single day during normal operation. It is not a crash — it is the bot intentionally killing itself:

**Step 1 — `max_trades_per_day` auto-disables the runner**

File: `strategy.py` lines 665–683, inside `_should_open()`:

```python
# Gate 3: max trades per calendar day (UTC)
if self.config.max_trades_per_day > 0:
    today_count = sum(1 for t in self.all_trades if ...)
    if today_count >= self.config.max_trades_per_day:
        if not self.active_trades:          # ← trade already closed
            self._enabled = False           # ← PERMANENTLY DISABLED
            return False
```

After the daily straddle closes (via profit target or time exit), the next `tick()` evaluates `_should_open()`. Gate 3 sees `today_count = 1 >= max_trades_per_day (1)` and `active_trades = []` (trade is now CLOSED). It sets `self._enabled = False`. This is **irreversible** — nothing in the code ever sets it back to True. The runner is dead.

**Step 2 — `tick()` stops processing entirely**

File: `strategy.py` line 612:

```python
def tick(self, account):
    if not self._enabled:
        return                  # ← exits immediately, skips EVERYTHING
```

Once `_enabled = False`, the runner:
- Does NOT evaluate entry conditions (expected — we don't want new trades)
- Does NOT call `_check_closed_trades()` (unexpected — means the `on_trade_closed` callback won't fire for trades that close AFTER disable)
- Does NOT log PnL summaries
- Is effectively inert

**Step 3 — main.py detects "all strategies completed" and calls `shutdown()`**

File: `main.py` lines 171–176:

```python
if runners and all(
    not r._enabled and not r.active_trades for r in runners
):
    logger.info("All strategies completed — auto-shutting down")
    shutdown()
```

Every 10 seconds, the main loop checks: are ALL runners disabled AND have no active trades? After Step 1, this is True. The main loop calls `shutdown()`.

**Step 4 — `shutdown()` force-closes trades (in memory only) and calls `sys.exit(0)`**

File: `main.py` lines 130–155:

```python
def shutdown(sig=None, frame=None):
    for r in runners:
        r.stop()           # force-closes in memory — does NOT close on exchange
    sys.exit(0)            # clean exit
```

**Step 5 — NSSM sees exit code 0, restarts the service**

NSSM config: `AppExit Default = Restart`. Exit code 0 is treated identically to a crash. The bot restarts from scratch with empty state.

**Step 6 — The cycle repeats**

New process starts → empty in-memory state → `max_trades_per_day = 0` → if still in `time_window(12, 13)` → opens a DUPLICATE trade → closes it → auto-disables → auto-shutdowns → NSSM restarts → …

### 8B. Why This Design Is Wrong

The auto-shutdown + auto-disable logic was written for **one-shot strategies** (run a test, open one trade, observe the result, exit). It is fundamentally incompatible with:

1. **A Windows service** — services are expected to run indefinitely. NSSM restarts "dead" services because that's its entire purpose.
2. **Daily-repeating strategies** — `max_trades_per_day=1` means "one trade per day, repeat tomorrow." But `_enabled = False` is permanent, and the auto-shutdown fires before tomorrow arrives.
3. **Position monitoring** — once `_enabled = False`, the runner's `tick()` returns immediately. Even `_check_closed_trades()` stops running. If the LifecycleManager is still monitoring the trade (it does — it has its own tick), the runner won't notice the close event.

The `_enabled` flag conflates two different concepts:
- **"Don't open new trades right now"** (temporary — should reset daily)
- **"This strategy is finished forever"** (permanent — should trigger cleanup)

### 8C. What Should Happen Instead

**The bot should NEVER initiate its own shutdown during normal operation.**

The correct behavior for a daily strategy running as a service:

1. **12:00 UTC**: Entry window opens → open straddle → `max_concurrent_trades` blocks further entries while trade is active
2. **Trade closes** (profit target or 19:00 time exit): `max_trades_per_day` blocks further entries for today → **but the bot keeps running**
3. **00:00 UTC next day**: The day counter resets → entry evaluation resumes → waits for 12:00 window
4. **Repeat forever**

This means:
- `_enabled` should NEVER be set to False by `max_trades_per_day`. The gate should just return False (block entry) without disabling the runner.
- The auto-shutdown check in `main.py` should be REMOVED entirely (or only trigger on explicit user stop signals).
- `max_trades_per_day` already correctly blocks entry — it doesn't need `_enabled = False` as a secondary lock.

### 8D. Proposed Fix (Detailed)

**Fix A — Remove auto-disable from `max_trades_per_day` gate (strategy.py line 672–679)**

BEFORE:
```python
if today_count >= self.config.max_trades_per_day:
    if not self.active_trades:
        self._enabled = False       # ← REMOVE THIS
    return False
```

AFTER:
```python
if today_count >= self.config.max_trades_per_day:
    # Just block entry — don't disable the runner.
    # The counter resets naturally when the UTC date changes.
    return False
```

This alone fixes the cascade. Gate 3 still blocks duplicate entries, but the runner stays alive. When UTC midnight passes, `today_count` becomes 0 again, and the strategy can trade again the next day.

**Fix B — Remove auto-shutdown from main.py (lines 171–176)**

BEFORE:
```python
if runners and all(not r._enabled and not r.active_trades for r in runners):
    logger.info("All strategies completed — auto-shutting down")
    shutdown()
```

AFTER: Remove this block entirely. Or replace with a log message:
```python
# Removed: auto-shutdown is incompatible with NSSM service mode.
# The bot runs indefinitely. Use 'nssm stop CoincallTrader' for manual shutdown.
```

The main loop should be an unconditional `while True` that only exits on:
- SIGINT / SIGTERM (manual stop via `nssm stop` or Ctrl+C)
- Fatal consecutive errors (the existing 10-error safety net is fine)

**Fix C — Keep `tick()` running even when entry is blocked**

The current `tick()` returns immediately when `_enabled = False`. After Fix A, `_enabled` will only be False via explicit `disable()` or `stop()` calls. But as a safety improvement, `_check_closed_trades()` should run unconditionally — even if entry evaluation is paused:

BEFORE:
```python
def tick(self, account):
    if not self._enabled:
        return
    self._check_closed_trades(account)
    ...
```

AFTER:
```python
def tick(self, account):
    # Always process trade close events, even when entry is paused
    self._check_closed_trades(account)
    
    if not self._enabled:
        return
    ...
```

This ensures `on_trade_closed` callbacks, persistence writes, and Telegram notifications still fire even if the runner was disabled for some other reason.

### 8E. Why `max_trades_per_day` Doesn't Even Need Fixing Functionally

The `max_trades_per_day` gate (without the `_enabled = False` poison pill) already works correctly as a rate limiter:

- Tick at 13:01 → `_should_open()` → Gate 3: `today_count=1 >= 1` → returns False → no new trade
- Tick at 13:31 → same → False
- Tick at 00:01 next day → Gate 3: `today_count=0 < 1` → passes → Gate 4 `time_window(12,13)`: 00:01 not in window → False
- Tick at 12:01 next day → all gates pass → new trade opens

The `_enabled = False` was purely unnecessary. The gate already blocks. The disable was a premature optimization ("stop checking since we know it'll fail") that turned into a death sentence for the process.

### 8F. Summary of What Caused the March 4 Duplicate Trades

The exact sequence:
1. Service started ~12:34 UTC
2. Straddle 1 opened ~13:00 (within the time window)
3. Straddle 1 hit time_exit at 19:00 → LifecycleManager closed it
4. At 19:00+30s: runner `tick()` → `_should_open()` → Gate 3 sees `today_count=1, active=0` → sets `_enabled = False`
5. Within 10s: main loop sees `all(not r._enabled and not r.active_trades)` → calls `shutdown()` → `sys.exit(0)`
6. NSSM restarts service (exit code 0 → Restart)
7. Fresh process: empty state → `max_trades_per_day` counter = 0 → but it's now ~19:00, outside `time_window(12,13)` → no duplicate THIS time

**However**, if any shutdown happens DURING the 12–13 window (due to an earlier similar cycle, external STOP signal, or any other reason), the restart opens a duplicate because:
- The time window is still open
- The in-memory trade counter is zero
- There's no exchange position check

The Windows Event Log confirmed: the shutdowns on March 4 were from "STOP control" signals — which could be the auto-shutdown mechanism OR external Windows events. Either way, the auto-shutdown creates a window of vulnerability that should not exist.

### 8G. Files to Modify (Implementation Checklist)

| File | Line(s) | Change |
|------|---------|--------|
| `strategy.py` | 672–679 | Remove `self._enabled = False` from max_trades_per_day gate |
| `strategy.py` | 612 | Move `_check_closed_trades()` above the `_enabled` guard |
| `main.py` | 171–176 | Remove auto-shutdown block entirely |

These three changes total ~10 lines of modification and eliminate the self-shutdown behavior completely.
