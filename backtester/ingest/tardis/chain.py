#!/usr/bin/env python3
"""
HistoricOptionChain — fast in-memory option chain for backtesting.

=== OVERVIEW ===
Loads a parquet file of tick-level Deribit option data (extracted from
tardis.dev options_chain) and provides instant lookups by time, expiry,
strike, and call/put.

=== DATA MODEL ===
The source data is TICK-LEVEL: each row is one instrument updating at one
microsecond. At any given microsecond, only a few of the ~200 instruments
are present. To answer "what's the 85000 call worth at 10:05?", we need
the LATEST update for that instrument at or before 10:05.

Internally, we split the DataFrame by instrument key (expiry, strike, is_call)
and store each as a sorted-by-timestamp DataFrame slice. Lookups use binary
search (searchsorted) → O(log n) per instrument, typically ~15 binary search
steps for 20k rows per instrument.

=== MEMORY ===
Holds the full parquet DataFrame in memory (~87MB for one day of BTC 0DTE+1DTE).
No duplication — instrument slices are views into the same underlying DataFrame.

=== PERFORMANCE ===
- Load + index build: ~0.8s
- Single option lookup: ~40µs (binary search)
- Full chain snapshot (all strikes, one expiry): ~3ms
- ATM straddle lookup: ~90µs

=== USAGE ===
    from backtester.ingest.tardis import HistoricOptionChain

    chain = HistoricOptionChain("analysis/ingest/tardis/data/btc_0dte_1dte_2025-03-01.parquet")

    # Get a single option
    opt = chain.get("2025-03-01 10:05", "2MAR25", 85000, is_call=True)
    print(f"Mark: {opt['mark_price']:.6f} BTC, IV: {opt['mark_iv']:.1f}%")

    # Get full chain snapshot (all strikes) for an expiry
    snap = chain.get_chain("2025-03-01 14:00", "2MAR25")
    print(snap[["strike", "mark_price", "delta"]])

    # Get ATM straddle
    call, put = chain.get_atm_straddle("2025-03-01 12:00", "2MAR25")

    # Get underlying price at a point in time
    spot = chain.get_spot("2025-03-01 15:30")

    # Iterate through minutes (backtest pattern)
    for minute in chain.minutes():
        spot = chain.get_spot(minute)
        call, put = chain.get_atm_straddle(minute, "2MAR25")
        ...

=== TIME ARGUMENTS ===
All time arguments accept:
    - str:        "2025-03-01 10:05"  or  "2025-03-01 10:05:30"
    - int:        microsecond timestamp (e.g. 1740823500000000)
    - datetime:   datetime.datetime object
    - Timestamp:  pd.Timestamp object

=== PARQUET SCHEMA (expected columns) ===
    timestamp        int64    — microseconds since epoch
    expiry           str      — e.g. "2MAR25"
    strike           float32  — e.g. 85000.0
    is_call          bool     — True=call, False=put
    underlying_price float32  — BTC spot price in USD
    mark_price       float32  — option mark price in BTC
    mark_iv          float32  — mark implied volatility (%)
    bid_price        float32  — best bid in BTC
    bid_amount       float32  — bid size
    bid_iv           float32  — bid implied volatility (%)
    ask_price        float32  — best ask in BTC
    ask_amount       float32  — ask size
    ask_iv           float32  — ask implied volatility (%)
    last_price       float32  — last trade price in BTC
    open_interest    float32  — open interest
    delta            float32  — option delta
    gamma            float32  — option gamma
    vega             float32  — option vega
    theta            float32  — option theta
"""
import time as _time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

# Type alias for flexible time input
TimeArg = Union[str, int, datetime, pd.Timestamp]


class HistoricOptionChain:
    """Fast in-memory option chain for backtesting with binary-search lookups."""

    def __init__(self, parquet_path):
        # type: (str) -> None
        """Load parquet and build per-instrument index.

        Args:
            parquet_path: Path to the parquet file with option tick data.
        """
        t0 = _time.time()
        self._df = pd.read_parquet(parquet_path)
        self._df.sort_values("timestamp", inplace=True)

        # Build per-instrument index: (expiry, strike, is_call) → DataFrame slice
        # Each slice is sorted by timestamp (inherited from the full sort above).
        self._instruments = {}  # type: Dict[Tuple[str, float, bool], pd.DataFrame]
        for key, group in self._df.groupby(
            ["expiry", "strike", "is_call"], observed=True
        ):
            self._instruments[key] = group

        # Cache sorted unique values for convenience
        self._expiries = sorted(self._df["expiry"].unique())
        self._strikes = {}  # type: Dict[str, np.ndarray]
        for exp in self._expiries:
            mask = self._df["expiry"] == exp
            self._strikes[exp] = np.sort(self._df.loc[mask, "strike"].unique())

        # Build sorted array of unique minute boundaries for iteration
        minutes = self._df["timestamp"].values // 60_000_000 * 60_000_000
        self._minutes = np.unique(minutes)

        # Spot-price index: sorted timestamps → underlying_price
        # Take one row per timestamp (all instruments at same ts have same spot)
        spot_df = self._df.drop_duplicates(subset="timestamp", keep="first")
        self._spot_ts = spot_df["timestamp"].values  # already sorted
        self._spot_px = spot_df["underlying_price"].values

        elapsed = _time.time() - t0
        n_inst = len(self._instruments)
        print(
            f"HistoricOptionChain loaded: {len(self._df):,} ticks, "
            f"{n_inst} instruments, {len(self._minutes)} minutes, "
            f"expiries {self._expiries} ({elapsed:.2f}s)"
        )

    # ------------------------------------------------------------------
    # Time conversion
    # ------------------------------------------------------------------
    @staticmethod
    def _to_us(t):
        # type: (TimeArg) -> int
        """Convert any time argument to microseconds since epoch."""
        if isinstance(t, (int, np.integer)):
            return int(t)
        if isinstance(t, str):
            t = pd.Timestamp(t, tz="UTC")
        elif isinstance(t, datetime):
            t = pd.Timestamp(t)
        if isinstance(t, pd.Timestamp):
            if t.tz is None:
                t = t.tz_localize("UTC")
            return int(t.timestamp() * 1_000_000)
        raise TypeError(f"Cannot convert {type(t)} to timestamp")

    # ------------------------------------------------------------------
    # Core lookup: single instrument at a point in time
    # ------------------------------------------------------------------
    def get(self, t, expiry, strike, is_call=True):
        # type: (TimeArg, str, float, bool) -> Optional[pd.Series]
        """Get the latest option data at or before time t.

        Returns a pandas Series with all fields (mark_price, delta, etc.),
        or None if no data exists before t for this instrument.
        """
        ts = self._to_us(t)
        key = (expiry, float(strike), bool(is_call))
        inst = self._instruments.get(key)
        if inst is None:
            return None
        idx = inst["timestamp"].searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return inst.iloc[idx]

    # ------------------------------------------------------------------
    # Chain snapshot: all strikes at a point in time
    # ------------------------------------------------------------------
    def get_chain(self, t, expiry, is_call=None):
        # type: (TimeArg, str, Optional[bool]) -> pd.DataFrame
        """Get a full chain snapshot: latest data for every strike.

        Args:
            t:       Target time.
            expiry:  Expiry string, e.g. "2MAR25".
            is_call: None=both calls and puts, True=calls only, False=puts only.

        Returns:
            DataFrame with one row per (strike, is_call), sorted by strike.
        """
        ts = self._to_us(t)
        rows = []
        for key, inst in self._instruments.items():
            exp, strike, call = key
            if exp != expiry:
                continue
            if is_call is not None and call != is_call:
                continue
            idx = inst["timestamp"].searchsorted(ts, side="right") - 1
            if idx >= 0:
                rows.append(inst.iloc[idx])
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame(rows).sort_values(["strike", "is_call"])

    # ------------------------------------------------------------------
    # ATM helpers
    # ------------------------------------------------------------------
    def get_spot(self, t):
        # type: (TimeArg) -> float
        """Get the underlying (BTC) price at time t."""
        ts = self._to_us(t)
        idx = np.searchsorted(self._spot_ts, ts, side="right") - 1
        if idx < 0:
            idx = 0
        return float(self._spot_px[idx])

    def get_atm_strike(self, t, expiry):
        # type: (TimeArg, str) -> float
        """Get the ATM strike (closest to spot) for an expiry at time t."""
        spot = self.get_spot(t)
        strikes = self._strikes.get(expiry)
        if strikes is None or len(strikes) == 0:
            raise ValueError(f"No strikes for expiry {expiry}")
        idx = np.searchsorted(strikes, spot)
        candidates = []
        if idx > 0:
            candidates.append(strikes[idx - 1])
        if idx < len(strikes):
            candidates.append(strikes[idx])
        return float(min(candidates, key=lambda s: abs(s - spot)))

    def get_atm(self, t, expiry, is_call=True):
        # type: (TimeArg, str, bool) -> Optional[pd.Series]
        """Get the ATM option (call or put) at time t."""
        strike = self.get_atm_strike(t, expiry)
        return self.get(t, expiry, strike, is_call)

    def get_atm_straddle(self, t, expiry):
        # type: (TimeArg, str) -> Tuple[Optional[pd.Series], Optional[pd.Series]]
        """Get the ATM straddle (call, put) at time t.

        Returns (call, put) tuple. Both use the same ATM strike.
        """
        strike = self.get_atm_strike(t, expiry)
        call = self.get(t, expiry, strike, is_call=True)
        put = self.get(t, expiry, strike, is_call=False)
        return call, put

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    def expiries(self):
        # type: () -> List[str]
        """List available expiry strings."""
        return list(self._expiries)

    def strikes(self, expiry):
        # type: (str) -> List[float]
        """List available strikes for an expiry, sorted ascending."""
        return list(self._strikes.get(expiry, []))

    def minutes(self):
        # type: () -> np.ndarray
        """Array of unique minute timestamps (microseconds), for iteration."""
        return self._minutes

    def time_range(self):
        # type: () -> Tuple[pd.Timestamp, pd.Timestamp]
        """Return (start, end) timestamps of the data."""
        return (
            pd.Timestamp(int(self._df["timestamp"].min()), unit="us", tz="UTC"),
            pd.Timestamp(int(self._df["timestamp"].max()), unit="us", tz="UTC"),
        )

    def __repr__(self):
        t_start, t_end = self.time_range()
        return (
            f"HistoricOptionChain("
            f"{len(self._df):,} ticks, "
            f"{len(self._instruments)} instruments, "
            f"expiries={self._expiries}, "
            f"{t_start:%Y-%m-%d %H:%M} to {t_end:%H:%M} UTC)"
        )


# ------------------------------------------------------------------
# Self-test when run directly
# ------------------------------------------------------------------
if __name__ == "__main__":
    import os
    import timeit

    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "data",
        "btc_0dte_1dte_2025-03-01.parquet",
    )
    if not os.path.exists(path):
        # Try the old location
        path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "tardis_data",
            "btc_0dte_1dte_2025-03-01.parquet",
        )
    chain = HistoricOptionChain(path)
    print(chain)
    print()

    t = "2025-03-01 10:05"
    spot = chain.get_spot(t)
    print(f"Spot at {t}: ${spot:,.2f}")

    atm_strike = chain.get_atm_strike(t, "2MAR25")
    print(f"ATM strike: ${atm_strike:,.0f}")

    call = chain.get_atm(t, "2MAR25", is_call=True)
    put = chain.get_atm(t, "2MAR25", is_call=False)
    if call is not None and put is not None:
        call_usd = float(call["mark_price"]) * float(call["underlying_price"])
        put_usd = float(put["mark_price"]) * float(put["underlying_price"])
        print(f"ATM Call:  ${call_usd:,.2f}  (IV={call['mark_iv']:.1f}%, delta={call['delta']:.4f})")
        print(f"ATM Put:   ${put_usd:,.2f}  (IV={put['mark_iv']:.1f}%, delta={put['delta']:.4f})")
        print(f"Straddle:  ${call_usd + put_usd:,.2f}")

    n = 1000
    elapsed = timeit.timeit(lambda: chain.get(t, "2MAR25", 85000, True), number=n)
    print(f"\nBenchmark: single lookup {elapsed / n * 1e6:.1f}us ({n} iters)")
    elapsed = timeit.timeit(lambda: chain.get_chain(t, "2MAR25"), number=n)
    print(f"Benchmark: full chain    {elapsed / n * 1e3:.2f}ms ({n} iters)")
    elapsed = timeit.timeit(lambda: chain.get_atm_straddle(t, "2MAR25"), number=n)
    print(f"Benchmark: ATM straddle  {elapsed / n * 1e6:.1f}us ({n} iters)")
