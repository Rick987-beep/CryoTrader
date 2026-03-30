#!/usr/bin/env python3
"""
engine.py — Grid runner for backtesting strategies.

Single-pass multi-combo evaluation: iterates market data once and evaluates
all parameter combinations simultaneously. MarketState is constructed once
per 5-min interval; each strategy instance processes it independently.

Usage:
    from backtester2.engine import run_grid, run_single
    from backtester2.strategies.straddle_strangle import ExtrusionStraddleStrangle

    results = run_grid(
        ExtrusionStraddleStrangle,
        ExtrusionStraddleStrangle.PARAM_GRID,
        replay,
    )
    # results: dict of param_tuple → list of (pnl, triggered, exit_hour, entry_date)
"""
import itertools
import time as _time
from typing import Any, Dict, List, Optional, Tuple, Type

from backtester2.config import cfg as _cfg
from backtester2.strategy_base import Trade

_progress_interval = _cfg.simulation.progress_interval


def _grid_combos(param_grid):
    # type: (Dict[str, List]) -> List[Dict[str, Any]]
    """Expand a parameter grid dict into a list of param dicts.

    Example:
        {"a": [1, 2], "b": [10, 20]} → [{"a":1,"b":10}, {"a":1,"b":20}, ...]
    """
    keys = sorted(param_grid.keys())
    values = [param_grid[k] for k in keys]
    combos = []
    for vals in itertools.product(*values):
        combos.append(dict(zip(keys, vals)))
    return combos


def _params_to_key(params):
    # type: (Dict[str, Any]) -> Tuple
    """Convert a params dict to a hashable tuple key for results dict."""
    return tuple(sorted(params.items()))


def _trade_to_tuple(trade):
    # type: (Trade) -> Tuple[float, bool, int, str]
    """Convert Trade to V1-compatible (pnl, triggered, exit_hour, entry_date)."""
    return (trade.pnl, trade.triggered, trade.exit_hour, trade.entry_date)


def run_single(strategy_cls, params, replay):
    # type: (Type, Dict[str, Any], Any) -> List[Trade]
    """Run a single parameter combo and return Trade objects.

    Useful for debugging or inspecting individual trade details.
    """
    strategy = strategy_cls()
    strategy.configure(params)

    trades = []
    last_state = None
    for state in replay:
        result = strategy.on_market_state(state)
        trades.extend(result)
        last_state = state

    if last_state is not None:
        trades.extend(strategy.on_end(last_state))

    return trades


def run_grid(
    strategy_cls,       # type: Type
    param_grid,         # type: Dict[str, List]
    replay,             # type: Any
    extra_params=None,  # type: Optional[Dict[str, Any]]
    progress=True,      # type: bool
):
    # type: (...) -> Dict[Tuple, List[Tuple[float, bool, int, str]]]
    """Run all parameter combos in a single pass over market data.

    Creates one strategy instance per combo, iterates market data once,
    and feeds each MarketState to all instances simultaneously.

    Args:
        strategy_cls: Strategy class (must have configure/on_market_state/on_end/reset).
        param_grid: Dict of param_name → list of values.
        replay: MarketReplay instance (iterable of MarketState).
        extra_params: Optional fixed params merged into every combo
                      (e.g. {"pricing_mode": "real"}).
        progress: Print progress updates.

    Returns:
        Dict of param_tuple → list of (pnl, triggered, exit_hour, entry_date).
        Compatible with V1 metrics.compute_stats().
    """
    combos = _grid_combos(param_grid)
    n_combos = len(combos)

    if progress:
        print(f"Running {n_combos} parameter combos...")

    # Create and configure one strategy instance per combo
    instances = []  # type: List[Any]
    keys = []       # type: List[Tuple]
    for params in combos:
        full_params = dict(params)
        if extra_params:
            full_params.update(extra_params)
        strategy = strategy_cls()
        strategy.configure(full_params)
        instances.append(strategy)
        keys.append(_params_to_key(params))

    # Results: key → list of V1-compatible tuples
    results = {k: [] for k in keys}

    # Single-pass: iterate market data once
    t0 = _time.time()
    n_states = 0
    last_state = None

    for state in replay:
        n_states += 1
        for i, strategy in enumerate(instances):
            trades = strategy.on_market_state(state)
            for trade in trades:
                results[keys[i]].append(_trade_to_tuple(trade))
        last_state = state

        # Progress every N states (configured in config.toml)
        if progress and n_states % _progress_interval == 0:
            elapsed = _time.time() - t0
            print(f"  {n_states} states processed ({elapsed:.1f}s)...")

    # Force-close any remaining positions
    if last_state is not None:
        for i, strategy in enumerate(instances):
            trades = strategy.on_end(last_state)
            for trade in trades:
                results[keys[i]].append(_trade_to_tuple(trade))

    elapsed = _time.time() - t0
    total_trades = sum(len(v) for v in results.values())

    if progress:
        print(
            f"Grid complete: {n_combos} combos × {n_states} states "
            f"= {total_trades:,} trades in {elapsed:.1f}s"
        )

    return results


def run_grid_full(
    strategy_cls,       # type: Type
    param_grid,         # type: Dict[str, List]
    replay,             # type: Any
    extra_params=None,  # type: Optional[Dict[str, Any]]
    progress=True,      # type: bool
):
    """Run all parameter combos in a single pass over market data.

    Accumulates trades into flat lists, then builds a memory-efficient
    pandas DataFrame (~10× less RAM than keeping Trade objects alive).

    Args:
        strategy_cls: Strategy class (configure/on_market_state/on_end/reset).
        param_grid:   Dict of param_name → list of values.
        replay:       MarketReplay instance (iterable of MarketState).
        extra_params: Optional fixed params merged into every combo.
        progress:     Print progress updates.

    Returns:
        Tuple of (df, keys):
        - df:   pandas DataFrame, one row per closed trade.
                Column "combo_idx" (int16/int32) is an index into keys.
        - keys: List[Tuple], where keys[i] is the param tuple for combo_idx i.
    """
    import pandas as pd

    combos = _grid_combos(param_grid)
    n_combos = len(combos)

    if progress:
        print(f"Running {n_combos} parameter combos...")

    instances = []  # type: List[Any]
    keys = []       # type: List[Tuple]
    for params in combos:
        full_params = dict(params)
        if extra_params:
            full_params.update(extra_params)
        strategy = strategy_cls()
        strategy.configure(full_params)
        instances.append(strategy)
        keys.append(_params_to_key(params))

    # Flat lists — Trade objects are decomposed immediately and discarded
    _combo_idx = []
    _entry_time = []
    _exit_time = []
    _entry_spot = []
    _exit_spot = []
    _entry_price_usd = []
    _exit_price_usd = []
    _fees = []
    _pnl = []
    _triggered = []
    _exit_reason = []
    _exit_hour = []
    _entry_date = []

    def _append(i, trade):
        _combo_idx.append(i)
        _entry_time.append(trade.entry_time)
        _exit_time.append(trade.exit_time)
        _entry_spot.append(trade.entry_spot)
        _exit_spot.append(trade.exit_spot)
        _entry_price_usd.append(trade.entry_price_usd)
        _exit_price_usd.append(trade.exit_price_usd)
        _fees.append(trade.fees)
        _pnl.append(trade.pnl)
        _triggered.append(trade.triggered)
        _exit_reason.append(trade.exit_reason)
        _exit_hour.append(trade.exit_hour)
        _entry_date.append(trade.entry_date)

    t0 = _time.time()
    n_states = 0
    last_state = None

    for state in replay:
        n_states += 1
        for i, strategy in enumerate(instances):
            for trade in strategy.on_market_state(state):
                _append(i, trade)
        last_state = state

        if progress and n_states % _progress_interval == 0:
            elapsed = _time.time() - t0
            print(f"  {n_states} states processed ({elapsed:.1f}s)...")

    if last_state is not None:
        for i, strategy in enumerate(instances):
            for trade in strategy.on_end(last_state):
                _append(i, trade)

    elapsed = _time.time() - t0
    total_trades = len(_pnl)

    if progress:
        print(
            f"Grid complete: {n_combos} combos × {n_states} states "
            f"= {total_trades:,} trades in {elapsed:.1f}s"
        )

    # Build DataFrame with compact dtypes
    idx_dtype = "int16" if n_combos <= 32767 else "int32"
    df = pd.DataFrame({
        "combo_idx":       pd.array(_combo_idx, dtype=idx_dtype),
        "entry_time":      pd.to_datetime(_entry_time),
        "exit_time":       pd.to_datetime(_exit_time),
        "entry_spot":      pd.array(_entry_spot, dtype="float32"),
        "exit_spot":       pd.array(_exit_spot, dtype="float32"),
        "entry_price_usd": pd.array(_entry_price_usd, dtype="float32"),
        "exit_price_usd":  pd.array(_exit_price_usd, dtype="float32"),
        "fees":            pd.array(_fees, dtype="float32"),
        "pnl":             pd.array(_pnl, dtype="float32"),
        "triggered":       _triggered,
        "exit_reason":     pd.Categorical(_exit_reason),
        "exit_hour":       pd.array(_exit_hour, dtype="int16"),
        "entry_date":      _entry_date,
    })
    return df, keys
