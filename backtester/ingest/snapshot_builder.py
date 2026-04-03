#!/usr/bin/env python3
"""
snapshot_builder.py — Build 5-min option snapshots and 1-min spot OHLC
from raw tick-level Tardis parquet data.

Reads raw tick-level parquet files (one per day, ~600-750 MB each) from
ingest/tardis/data/ and produces two compact snapshot artifacts:

1. **Option snapshots** (5-min intervals): last-known state of every
   instrument at each 5-minute boundary. Uses vectorised binary search
   per instrument for correct "last known at boundary" semantics —
   instruments that haven't ticked recently still appear.

2. **Spot track** (1-min OHLC): open/high/low/close of BTC underlying
   price per minute. Used for precise excursion trigger detection.

Both outputs are zstd-compressed Parquet files stored in data/.

Usage:
    python -m backtester.ingest.snapshot_builder
    python -m backtester.ingest.snapshot_builder --data-dir path/to/data
    python -m backtester.ingest.snapshot_builder --interval 5 --spot-interval 1
"""
import glob
import os
import time as _time
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from backtester.config import cfg as _cfg

# Lazy import — only needed when building (avoids import cost for other modules)
_HistoricOptionChain = None


def _get_chain_class():
    """Lazy-import HistoricOptionChain to avoid circular / startup cost."""
    global _HistoricOptionChain
    if _HistoricOptionChain is None:
        from backtester.ingest.tardis.chain import HistoricOptionChain
        _HistoricOptionChain = HistoricOptionChain
    return _HistoricOptionChain


# Columns to keep in the option snapshot (from the raw tick data)
SNAPSHOT_COLS = [
    "expiry", "strike", "is_call", "underlying_price",
    "bid_price", "ask_price", "mark_price", "mark_iv", "delta",
]


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _five_min_boundaries(minutes_us, interval_min=5):
    # type: (np.ndarray, int) -> np.ndarray
    """Generate interval-aligned boundary timestamps from a minute array.

    Args:
        minutes_us: Array of minute-aligned timestamps in microseconds.
        interval_min: Interval in minutes (default 5).

    Returns:
        Sorted array of unique interval-aligned timestamps (µs).
    """
    interval_us = interval_min * 60 * 1_000_000
    floored = minutes_us // interval_us * interval_us
    return np.unique(floored)


def _build_option_snapshots(chain, interval_min=5):
    # type: (object, int) -> pd.DataFrame
    """Build option snapshots from a loaded HistoricOptionChain.

    For each interval boundary, finds the last-known state of every
    instrument using vectorised binary search (np.searchsorted).
    Instruments that haven't ticked since a previous boundary still
    appear with their most recent values — no data gaps.

    Args:
        chain: Loaded HistoricOptionChain for one day.
        interval_min: Snapshot interval in minutes.

    Returns:
        DataFrame with one row per (boundary, expiry, strike, is_call).
    """
    boundaries = _five_min_boundaries(chain.minutes(), interval_min)
    if len(boundaries) == 0:
        return pd.DataFrame(columns=["timestamp"] + SNAPSHOT_COLS)

    frames = []
    for (expiry, strike, is_call), inst_df in chain._instruments.items():
        ts_arr = inst_df["timestamp"].values

        # Vectorised: find last-known index for ALL boundaries at once
        idxs = np.searchsorted(ts_arr, boundaries, side="right") - 1
        valid = idxs >= 0
        if not valid.any():
            continue

        valid_boundaries = boundaries[valid]
        valid_indices = idxs[valid]

        selected = inst_df.iloc[valid_indices][SNAPSHOT_COLS].reset_index(drop=True)
        selected["timestamp"] = valid_boundaries
        frames.append(selected)

    if not frames:
        return pd.DataFrame(columns=["timestamp"] + SNAPSHOT_COLS)

    result = pd.concat(frames, ignore_index=True)
    result.sort_values(
        ["timestamp", "expiry", "strike", "is_call"], inplace=True
    )
    result.reset_index(drop=True, inplace=True)
    return result


def _build_spot_track(chain, interval_min=1):
    # type: (object, int) -> pd.DataFrame
    """Build spot OHLC bars from a loaded HistoricOptionChain.

    Uses the internal spot-price arrays (deduplicated per timestamp)
    to compute open/high/low/close within each bar interval.

    Args:
        chain: Loaded HistoricOptionChain for one day.
        interval_min: Bar interval in minutes (default 1).

    Returns:
        DataFrame with columns: timestamp, open, high, low, close.
    """
    spot_df = pd.DataFrame({
        "timestamp": chain._spot_ts,
        "price": chain._spot_px.astype(np.float64),
    })

    interval_us = interval_min * 60 * 1_000_000
    spot_df["ts_bar"] = spot_df["timestamp"] // interval_us * interval_us

    ohlc = spot_df.groupby("ts_bar")["price"].agg(
        open="first", high="max", low="min", close="last"
    ).reset_index()
    ohlc.rename(columns={"ts_bar": "timestamp"}, inplace=True)

    # float32 for compactness
    for col in ["open", "high", "low", "close"]:
        ohlc[col] = ohlc[col].astype(np.float32)

    return ohlc


# ------------------------------------------------------------------
# File discovery
# ------------------------------------------------------------------

def discover_parquets(data_dir, pattern="btc_2026-*.parquet"):
    # type: (str, str) -> List[str]
    """Find raw tick parquet files matching pattern, sorted by name.

    Args:
        data_dir: Directory containing parquet files.
        pattern: Glob pattern for file matching.

    Returns:
        Sorted list of absolute paths.
    """
    paths = sorted(glob.glob(os.path.join(data_dir, pattern)))
    if not paths:
        raise FileNotFoundError(
            f"No parquet files matching '{pattern}' in {data_dir}"
        )
    return paths


def _extract_date(path):
    # type: (str) -> str
    """Extract date string from filename like 'btc_2026-03-09.parquet'."""
    basename = os.path.basename(path).replace(".parquet", "")
    for part in basename.split("_"):
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return basename


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def build_snapshots(
    data_dir=None,            # type: Optional[str]
    output_dir=None,          # type: Optional[str]
    pattern="btc_2026-*.parquet",  # type: str
    interval_min=None,        # type: Optional[int]
    spot_interval_min=None,   # type: Optional[int]
):
    # type: (...) -> Tuple[str, str]
    """Build snapshot parquets from raw tick data.

    Processes each day's parquet independently (fits in 16 GB RAM),
    concatenates results, and writes two output files with zstd
    compression.

    Args:
        data_dir: Path to raw tick parquets.
            Default: ingest/tardis/data/ relative to this file.
        output_dir: Output directory.
            Default: data/ relative to this file.
        pattern: Glob pattern for input files.
        interval_min: Option snapshot interval (default 5 minutes).
        spot_interval_min: Spot OHLC interval (default 1 minute).

    Returns:
        Tuple of (option_snapshot_path, spot_track_path).
    """
    ChainClass = _get_chain_class()

    # Resolve defaults from config (or caller overrides)
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if data_dir is None:
        data_dir = _cfg.data.tardis_data_dir
    if output_dir is None:
        output_dir = _cfg.data.snapshots_dir
    if interval_min is None:
        interval_min = _cfg.data.snapshot_interval_min
    if spot_interval_min is None:
        spot_interval_min = _cfg.data.spot_interval_min

    os.makedirs(output_dir, exist_ok=True)

    paths = discover_parquets(data_dir, pattern)
    print(f"Found {len(paths)} parquet files to process")

    all_options = []    # type: List[pd.DataFrame]
    all_spot = []       # type: List[pd.DataFrame]
    dates = []          # type: List[str]

    t_total = _time.time()
    for i, path in enumerate(paths, 1):
        date_str = _extract_date(path)
        dates.append(date_str)
        print(f"\n[{i}/{len(paths)}] Processing {date_str}...")

        t0 = _time.time()
        chain = ChainClass(path)

        # Option snapshots
        opt_df = _build_option_snapshots(chain, interval_min)
        all_options.append(opt_df)

        # Spot OHLC
        spot_df = _build_spot_track(chain, spot_interval_min)
        all_spot.append(spot_df)

        elapsed = _time.time() - t0
        print(
            f"  Options: {len(opt_df):,} rows, "
            f"Spot: {len(spot_df):,} bars ({elapsed:.1f}s)"
        )

        # Free memory before loading next day
        del chain

    # Concatenate all days
    print("\nConcatenating...")
    options_df = pd.concat(all_options, ignore_index=True)
    spot_df = pd.concat(all_spot, ignore_index=True)

    # Sort final output
    options_df.sort_values(
        ["timestamp", "expiry", "strike", "is_call"], inplace=True
    )
    options_df.reset_index(drop=True, inplace=True)
    spot_df.sort_values("timestamp", inplace=True)
    spot_df.reset_index(drop=True, inplace=True)

    # Optimise dtypes for storage
    options_df["expiry"] = options_df["expiry"].astype("category")

    # Build output filenames from date range
    date_start = min(dates).replace("-", "")
    date_end = max(dates).replace("-", "")

    opt_path = os.path.join(
        output_dir, f"options_{date_start}_{date_end}.parquet"
    )
    spot_path = os.path.join(
        output_dir, f"spot_track_{date_start}_{date_end}.parquet"
    )

    # Write with configured compression
    options_df.to_parquet(opt_path, compression=_cfg.data.parquet_compression, index=False)
    spot_df.to_parquet(spot_path, compression=_cfg.data.parquet_compression, index=False)

    elapsed_total = _time.time() - t_total
    opt_size = os.path.getsize(opt_path)
    spot_size = os.path.getsize(spot_path)

    print(f"\n{'=' * 60}")
    print(f"Snapshot build complete ({elapsed_total:.1f}s)")
    print(
        f"  Options: {len(options_df):,} rows -> "
        f"{opt_path} ({opt_size / 1_000_000:.1f} MB)"
    )
    print(
        f"  Spot:    {len(spot_df):,} bars  -> "
        f"{spot_path} ({spot_size / 1_000:.0f} KB)"
    )
    print(f"  Date range: {min(dates)} to {max(dates)}")
    print(f"{'=' * 60}")

    return opt_path, spot_path


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    # Allow running from repo root: python -m backtester.ingest.snapshot_builder
    # or directly: python backtester/snapshot_builder.py
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    parser = argparse.ArgumentParser(
        description="Build 5-min option snapshots and 1-min spot OHLC "
                    "from raw Tardis tick data."
    )
    parser.add_argument(
        "--data-dir", default=None,
        help="Path to raw tick parquets (default: ingest/tardis/data/)",
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Output directory (default: data/)",
    )
    parser.add_argument(
        "--pattern", default="btc_2026-*.parquet",
        help="Glob pattern for input files (default: btc_2026-*.parquet)",
    )
    parser.add_argument(
        "--interval", type=int, default=5,
        help="Option snapshot interval in minutes (default: 5)",
    )
    parser.add_argument(
        "--spot-interval", type=int, default=1,
        help="Spot OHLC interval in minutes (default: 1)",
    )

    args = parser.parse_args()
    build_snapshots(
        data_dir=args.data_dir,
        output_dir=args.output_dir,
        pattern=args.pattern,
        interval_min=args.interval,
        spot_interval_min=args.spot_interval,
    )
