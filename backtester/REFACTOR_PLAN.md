# Backtester Strategy Layer — Refactoring Plan

**Audience:** AI coding agents executing the changes described below.  
**Status:** Analysis complete, implementation not started.  
**Scope:** `backtester/strategies/` — 9 strategy files share large swaths of verbatim-identical helper code. Goal: extract into shared modules, then update all strategies to import from them. No logic changes except one explicit behaviour decision noted in Phase 3.

---

## File map (before → after)

| New file | What moves there |
|---|---|
| `backtester/expiry_utils.py` | `_parse_expiry_date`, `_expiry_dt_utc`, `_select_expiry`, `_select_expiry_for_week`, `_nearest_valid_expiry`, `_MONTH_MAP`, weekday name dict + `_parse_open_days` / `_open_days_label` |
| `backtester/bt_option_selection.py` | `_select_by_delta`, `_apply_min_otm` |
| `backtester/strategy_base.py` (extend) | `check_expiry()` free function, `close_short_strangle()` free function, `ShortStrangleBase` mixin for `on_end`/`reset`/`_check_expiry`/`_check_take_profit`/`_close` boilerplate |

> **Naming note:** The new backtester option-selection module is called `bt_option_selection.py`  
> (not `option_selection.py`) to avoid any confusion with the live-system `option_selection.py`  
> in the repo root, which works on exchange API objects and has an incompatible interface.

---

## Strategies affected

| File | Phase 1 | Phase 2 | Phase 3 |
|---|---|---|---|
| `straddle_strangle.py` | ✓ expiry helpers | – | partial (on_end/reset only) |
| `daily_put_sell.py` | ✓ expiry helpers | – | partial (on_end/reset only) |
| `short_strangle_offset.py` | ✓ expiry helpers | ✓ delta select | ✓ strangle base |
| `short_strangle_delta_tp.py` | ✓ expiry helpers | ✓ delta select + min_otm | ✓ strangle base |
| `short_strangle_turbulence_tp.py` | ✓ expiry helpers | ✓ delta select | ✓ strangle base |
| `short_strangle_weekend.py` | ✓ expiry helpers + open_days | ✓ delta select + min_otm | ✓ strangle base |
| `short_strangle_weekly_tp.py` | ✓ expiry helpers + weekly | ✓ delta select | ✓ strangle base |
| `short_strangle_weekly_cap.py` | ✓ expiry helpers + weekly | ✓ delta select | ✓ strangle base |
| `batman_calendar.py` | ✓ expiry helpers | ✓ delta select | ✗ unique 4-leg close |
| `deltaswipswap.py` | ✓ expiry helpers | ✓ delta select | ✗ options+perp close |

---

## Phase 1 — `backtester/expiry_utils.py`

### Why first
`_parse_expiry_date` is copy-pasted verbatim into **all 9** files. It is the highest-impact,
zero-ambiguity extraction. The new module has no dependencies on strategy state.

### Functions to define in `backtester/expiry_utils.py`

```python
from functools import lru_cache
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

_MONTH_MAP: Dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

@lru_cache(maxsize=128)
def parse_expiry_date(expiry_code: str) -> Optional[datetime]: ...

@lru_cache(maxsize=128)
def expiry_dt_utc(expiry_code: str, tzinfo: Any) -> Optional[datetime]: ...

def select_expiry(state: Any, dte: int) -> Optional[str]: ...
# Scans state.expiries(); uses parse_expiry_date(); returns matching expiry code.

def select_expiry_for_week(state: Any, target_weeks: int) -> Optional[str]: ...
# Identical copy from weekly_tp and weekly_cap.

def nearest_valid_expiry(state: Any) -> Optional[str]: ...
# Identical copy from straddle_strangle and deltaswipswap.

# Weekday utilities (currently only in short_strangle_weekend.py):
_WEEKDAY_NAMES: Dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

def parse_open_days(value: Any) -> Optional[set]: ...
def open_days_label(days: Optional[set]) -> str: ...
```

**Note on `lru_cache` sizes:** The individual strategy files use inconsistent sizes
(`maxsize=64` in some, `maxsize=128` in others). Standardise to `maxsize=128` in
the shared module — safe because cache keys are short strings.

### Strategy update pattern (same for all 9 files)

1. Remove the local `_MONTH_MAP`, `_parse_expiry_date`, `_expiry_dt_utc`, `_select_expiry` definitions.
2. Add import:
   ```python
   from backtester.expiry_utils import (
       parse_expiry_date, expiry_dt_utc, select_expiry,
   )
   ```
3. Update all call sites: `_parse_expiry_date(x)` → `parse_expiry_date(x)`, etc.
   In `short_strangle_weekly_tp` and `short_strangle_weekly_cap` also import `select_expiry_for_week`.
   In `straddle_strangle` and `deltaswipswap` also import `nearest_valid_expiry`.
   In `short_strangle_weekend` also import `parse_open_days`, `open_days_label`.

### Tests to run after Phase 1

```bash
# Full suite — must stay green (244 tests, ~1.7 s)
python -m pytest tests/ -v

# Focused smoke — the two strategy-specific suites
python -m pytest tests/test_short_straddle_strangle.py tests/test_short_strangle_delta_tp.py -v

# Quick import check for all 9 strategies + new module
python -c "
from backtester import expiry_utils
from backtester.strategies import (
    straddle_strangle, daily_put_sell,
    short_strangle_offset, short_strangle_delta_tp,
    short_strangle_turbulence_tp, short_strangle_weekend,
    short_strangle_weekly_tp, short_strangle_weekly_cap,
    batman_calendar, deltaswipswap,
)
print('all imports OK')
"

# Verify lru_cache round-trip
python -c "
from backtester.expiry_utils import parse_expiry_date, expiry_dt_utc
from datetime import timezone
d = parse_expiry_date('21APR26')
assert d is not None and d.day == 21 and d.month == 4 and d.year == 2026
dt = expiry_dt_utc('21APR26', timezone.utc)
assert dt is not None and dt.hour == 8
print('expiry_utils round-trip OK')
"
```

---

## Phase 2 — `backtester/bt_option_selection.py`

### Functions to define

```python
from typing import Any, List, Optional

def select_by_delta(chain: List[Any], target_delta: float) -> Optional[Any]:
    """Return the option in `chain` whose delta is closest to `target_delta`.
    Prefers non-zero delta candidates; falls back to full chain if all zero.
    """

def apply_min_otm(
    chain: List[Any],
    selected: Any,
    spot: float,
    min_pct: float,
    is_call: bool,
) -> Optional[Any]:
    """Push `selected` outward if it is within `min_pct`% of spot.
    Call: requires strike >= spot*(1+min_pct/100).
    Put:  requires strike <= spot*(1-min_pct/100).
    Returns None if no qualifying strike exists.
    """
```

### Strategy update pattern

1. Remove local `_select_by_delta` and `_apply_min_otm` from the 7 affected files.
2. Add import:
   ```python
   from backtester.bt_option_selection import select_by_delta, apply_min_otm
   ```
3. Update call sites: `_select_by_delta(...)` → `select_by_delta(...)`, etc.

### Tests to run after Phase 2

```bash
python -m pytest tests/ -v

# Spot-check option selection via the delta_tp test suite
python -m pytest tests/test_short_strangle_delta_tp.py tests/test_short_strangle_delta_tp_real.py -v

# Import smoke (same command as Phase 1, re-run)
python -c "
from backtester.bt_option_selection import select_by_delta, apply_min_otm
from backtester.strategies import (
    short_strangle_delta_tp, short_strangle_weekend,
    short_strangle_turbulence_tp, short_strangle_weekly_tp,
    short_strangle_weekly_cap, short_strangle_offset,
    straddle_strangle, batman_calendar, deltaswipswap,
)
print('bt_option_selection imports OK')
"
```

---

## Phase 3 — `strategy_base.py` additions + `ShortStrangleBase` mixin

This is the most complex phase. Read all notes before touching any file.

### 3a — `check_expiry` free function (zero ambiguity)

Add to `strategy_base.py`:

```python
def check_expiry(state: Any, pos: "OpenPosition") -> Optional[str]:
    """Return 'expiry' if the position's expiry_dt has been reached, else None.
    Reads pos.metadata['expiry_dt']; returns None if key is absent.
    """
    exp_dt = pos.metadata.get("expiry_dt")
    if exp_dt is None:
        return None
    return "expiry" if state.dt >= exp_dt else None
```

Update `_check_expiry` in all 8 affected strategy classes to simply call this:

```python
def _check_expiry(self, state, pos):
    from backtester.strategy_base import check_expiry
    return check_expiry(state, pos)
```

Or remove `_check_expiry` entirely and inline `check_expiry(state, pos)` at all call sites.
Recommend the latter (fewer indirections).

### 3b — Zero-ask inconsistency: MUST RESOLVE BEFORE UNIFYING `_check_take_profit`

There are two conflicting behaviours across the strategy files:

| Strategy | ask == 0 behaviour |
|---|---|
| `short_strangle_delta_tp.py` | **Skip tick** — returns `None` when ask ≤ 0; treats 0-ask as missing data |
| `short_strangle_weekend.py` | **Fire TP** — treats ask == 0 as genuine signal (worthless option) |
| `short_strangle_turbulence_tp.py` | Same as `delta_tp` (skip) |
| `short_strangle_weekly_tp.py` | Same as `delta_tp` (skip) |
| `short_strangle_weekly_cap.py` | Same as `delta_tp` (skip) |
| `short_strangle_offset.py` | No TP — irrelevant |

**Decision required from user before this function is unified.**  
The two semantics produce different trade counts (weekend fires TP on deep-OTM legs
that show ask=0 late in the day; delta_tp skips those ticks and closes at max_hold
or expiry instead). This is a backtest-calibration choice, not a bug to auto-fix.

**Ask the user:** "Should ask==0 on a strangle leg be treated as (a) genuine TP
signal — leg is worthless, close it — or (b) missing quote — skip tick?"

Once decided, write `check_take_profit_strangle(state, pos, tp_pct, ask_zero_is_tp)` 
in `strategy_base.py` and update all 5 strategies consistently.

### 3c — `close_short_strangle` free function

The `_close` method in `delta_tp`, `weekend`, `turbulence_tp`, `offset`, `weekly_tp`,
`weekly_cap` is near-identical:

```python
def close_short_strangle(
    state: Any,
    pos: "OpenPosition",
    reason: str,
    spot: float,
) -> "Trade":
    """
    Build a Trade for a short strangle close.
    - On 'expiry': intrinsic settlement (max(0, spot-call_strike), etc.)
    - Otherwise:   buy back at ask, fallback to Deribit min tick (0.0001 BTC)
    Appends strategy-specific metadata keys AFTER the call site (caller's responsibility).
    """
```

The minor differences between strategy `_close` implementations are only in the
`trade.metadata` keys appended after the common close logic. So the free function
handles the common part; callers append their own keys:

```python
# In strategy:
def _close(self, state, pos, reason):
    trade = close_short_strangle(state, pos, reason, state.spot)
    trade.metadata["dte"]           = self._dte
    trade.metadata["stop_loss_pct"] = self._sl_pct
    # ...
    return trade
```

### 3d — `on_end` / `reset` boilerplate mixin (optional, lowest priority)

8 of 9 strategy classes have identical `on_end` and `reset` methods:

```python
def on_end(self, state):
    for pos in list(self._positions):
        t = self._close(state, pos, "expiry")
        self._trades.append(t)

def reset(self):
    self._positions = []
    self._trades = []
    self._last_trade_date = None
```

These can be provided by a `StrategyMixin` class in `strategy_base.py`:

```python
class ShortStrangleMixin:
    def on_end(self, state):
        for pos in list(self._positions):
            t = self._close(state, pos, "expiry")
            self._trades.append(t)

    def reset(self):
        self._positions = []
        self._trades = []
        self._last_trade_date = None
```

Strategy classes inherit from `ShortStrangleMixin` (and not from anything else —
the protocol is still structural). Opt-out: `batman_calendar` and `deltaswipswap`
have meaningfully different `reset`/`on_end` logic.

**This is lowest priority.** The mixin saves 6–8 lines per file but adds inheritance
where there was none. Only implement if the user explicitly wants it.

### Tests to run after Phase 3

```bash
# Full suite
python -m pytest tests/ -v

# Strategy-specific suites
python -m pytest \
  tests/test_short_straddle_strangle.py \
  tests/test_short_strangle_delta_tp.py \
  tests/test_short_strangle_delta_tp_real.py \
  -v

# Smoke: instantiate and configure each strategy to catch attribute errors
python -c "
from backtester.strategies.short_strangle_delta_tp import ShortStrangleDeltaTp
from backtester.strategies.short_strangle_weekend import ShortStrangleWeekend
from backtester.strategies.short_strangle_turbulence_tp import ShortStrangleTurbulenceTp
from backtester.strategies.short_strangle_weekly_tp import ShortStrangleWeeklyTp
from backtester.strategies.short_strangle_weekly_cap import ShortStrangleWeeklyCap
from backtester.strategies.short_strangle_offset import ShortStrangleOffset

params_base = dict(delta=0.15, stop_loss_pct=2.0, take_profit_pct=0.5, dte=1)
for cls, params in [
    (ShortStrangleDeltaTp, params_base),
    (ShortStrangleWeekend, params_base),
    (ShortStrangleTurbulenceTp, params_base),
    (ShortStrangleWeeklyTp, dict(delta=0.15, stop_loss_pct=2.0, take_profit_pct=0.5, target_weeks=1)),
    (ShortStrangleWeeklyCap, dict(delta=0.15, stop_loss_pct=2.0, take_profit_pct=0.5, target_weeks=1)),
    (ShortStrangleOffset, dict(stop_loss_pct=2.0, dte=1)),
]:
    s = cls()
    s.configure(params)
    assert hasattr(s, '_positions')
    assert hasattr(s, '_trades')
print('all strategy configure() smoke tests OK')
"
```

---

## Implementation order

1. **Phase 1** — `backtester/expiry_utils.py` — create + update all 9 strategies.
2. **Phase 2** — `backtester/bt_option_selection.py` — create + update 7 strategies.
3. **Phase 3a** — `check_expiry` free function in `strategy_base.py`.
4. **Phase 3b** — Ask user to resolve zero-ask semantics, then unify `_check_take_profit`.
5. **Phase 3c** — `close_short_strangle` free function in `strategy_base.py`.
6. **Phase 3d** — `ShortStrangleMixin` (only if user explicitly requests).

Run the full test suite (`python -m pytest tests/ -v`) after each phase. Do not batch phases.

---

## Do NOT touch

- `backtester/strategies/batman_calendar.py` — unique 4-leg close logic; `_close` is genuinely different.
- `backtester/strategies/deltaswipswap.py` — options + BTC-PERPETUAL hedge; `_close` + `reset` are genuinely different.
- `option_selection.py` (repo root, live system) — incompatible interface (exchange API objects vs `OptionQuote` dataclasses from `MarketReplay`). Never merge with `bt_option_selection.py`.
- `strategy_base.py` existing functions — only add; do not rename or restructure existing exports.

---

## Invariants to preserve

- `lru_cache` on `parse_expiry_date` and `expiry_dt_utc` must be retained in the shared module. These are called per-tick inside the engine hot loop.
- `_EXPIRY_HOUR_UTC` / `EXPIRY_HOUR_UTC` is imported from `backtester.pricing`. Keep that import path; do not hardcode the value in `expiry_utils.py`.
- Strategy files must not import from each other — only from shared modules.
- No changes to `Trade`, `OpenPosition`, or `close_trade` signatures in `strategy_base.py`.
