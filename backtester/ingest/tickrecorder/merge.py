#!/usr/bin/env python3
"""
merge.py — Combine Daily Parquets into a Backtester2-Compatible Range File

Reads daily options_YYYY-MM-DD.parquet and spot_track_YYYY-MM-DD.parquet
files produced by the recorder, concatenates a date range, and writes
output files matching the backtester snapshot naming convention:

    options_YYYYMMDD_YYYYMMDD.parquet
    spot_track_YYYYMMDD_YYYYMMDD.parquet

The output files are drop-in replacements for the Tardis-sourced snapshots
in backtester/data/. The backtester engine works unchanged.

Usage:
    # Merge March 20–27 from recorder/data/ into backtester/data/
    python -m backtester.ingest.tickrecorder.merge --from 2026-03-20 --to 2026-03-27

    # Custom source and output directories
    python -m backtester.ingest.tickrecorder.merge \\
        --from 2026-03-20 --to 2026-03-27 \\
        --data-dir /opt/ct/recorder/data \\
        --output-dir backtester/snapshots
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import List, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_DEFAULT_OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "snapshots"
)


def _date_range(from_str, to_str):
    # type: (str, str) -> List[str]
    """Return list of YYYY-MM-DD strings inclusive."""
    start = datetime.strptime(from_str, "%Y-%m-%d")
    end = datetime.strptime(to_str, "%Y-%m-%d")
    if start > end:
        raise ValueError(f"--from {from_str} is after --to {to_str}")
    dates = []
    cur = start
    while cur <= end:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    return dates


def _bt2_date(date_str):
    # type: (str) -> str
    """Convert YYYY-MM-DD to YYYYMMDD (backtester filename convention)."""
    return date_str.replace("-", "")


def _load_daily(data_dir, date_str, prefix):
    # type: (str, str, str) -> Optional[pd.DataFrame]
    """Load one daily parquet file. Returns None if file doesn't exist."""
    path = os.path.join(data_dir, f"{prefix}_{date_str}.parquet")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        logger.debug("Loaded %s: %d rows", os.path.basename(path), len(df))
        return df
    except Exception as exc:
        logger.warning("Failed to read %s: %s", path, exc)
        return None


def merge(from_date, to_date, data_dir=None, output_dir=None):
    # type: (str, str, Optional[str], Optional[str]) -> int
    """Merge daily parquets for the given date range. Returns exit code."""
    data_dir = data_dir or _DEFAULT_DATA_DIR
    output_dir = output_dir or _DEFAULT_OUTPUT_DIR

    os.makedirs(output_dir, exist_ok=True)

    dates = _date_range(from_date, to_date)
    logger.info("Merging %d day(s): %s to %s", len(dates), from_date, to_date)

    # ── Options ───────────────────────────────────────────────────────────────
    opt_frames = []
    missing_opt = []
    for d in dates:
        df = _load_daily(data_dir, d, "options")
        if df is not None:
            opt_frames.append(df)
        else:
            missing_opt.append(d)

    if missing_opt:
        logger.warning("Missing options files for %d day(s): %s",
                        len(missing_opt), ", ".join(missing_opt))

    if not opt_frames:
        logger.error("No options data found for the requested range. Aborting.")
        return 1

    opt_merged = pd.concat(opt_frames, ignore_index=True)
    opt_merged["expiry"] = opt_merged["expiry"].astype("category")
    opt_merged.sort_values(
        ["timestamp", "expiry", "strike", "is_call"], inplace=True
    )
    opt_merged.reset_index(drop=True, inplace=True)

    # ── Spot track ────────────────────────────────────────────────────────────
    spot_frames = []
    missing_spot = []
    for d in dates:
        df = _load_daily(data_dir, d, "spot_track")
        if df is not None:
            spot_frames.append(df)
        else:
            missing_spot.append(d)

    if missing_spot:
        logger.warning("Missing spot_track files for %d day(s): %s",
                        len(missing_spot), ", ".join(missing_spot))

    if not spot_frames:
        logger.error("No spot_track data found. Aborting.")
        return 1

    spot_merged = pd.concat(spot_frames, ignore_index=True)
    spot_merged.sort_values("timestamp", inplace=True)
    spot_merged.drop_duplicates("timestamp", keep="last", inplace=True)
    spot_merged.reset_index(drop=True, inplace=True)

    # ── Write output ─────────────────────────────────────────────────────────
    from_bt2 = _bt2_date(from_date)
    to_bt2 = _bt2_date(to_date)

    opt_out = os.path.join(output_dir, f"options_{from_bt2}_{to_bt2}.parquet")
    spot_out = os.path.join(output_dir, f"spot_track_{from_bt2}_{to_bt2}.parquet")

    opt_merged.to_parquet(opt_out, compression="zstd", index=False)
    spot_merged.to_parquet(spot_out, compression="zstd", index=False)

    opt_mb = os.path.getsize(opt_out) / 1024 / 1024
    spot_kb = os.path.getsize(spot_out) / 1024

    logger.info("Written: %s (%.1f MB, %d rows)", os.path.basename(opt_out),
                opt_mb, len(opt_merged))
    logger.info("Written: %s (%.1f KB, %d rows)", os.path.basename(spot_out),
                spot_kb, len(spot_merged))

    if missing_opt or missing_spot:
        logger.warning(
            "Output contains gaps due to missing daily files. "
            "Backtester2 will skip missing 5-min boundaries naturally."
        )

    return 0


def main():
    # type: () -> None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Merge recorder daily parquets into a backtester range file."
    )
    parser.add_argument("--from", dest="from_date", required=True,
                        metavar="YYYY-MM-DD", help="Start date (inclusive)")
    parser.add_argument("--to", dest="to_date", required=True,
                        metavar="YYYY-MM-DD", help="End date (inclusive)")
    parser.add_argument("--data-dir", default=None,
                        help="Source directory with daily parquets "
                             f"(default: tickrecorder/data/)")
    parser.add_argument("--output-dir", default=None,
                        help="Output directory "
                             f"(default: backtester/data/)")

    args = parser.parse_args()

    sys.exit(merge(
        from_date=args.from_date,
        to_date=args.to_date,
        data_dir=args.data_dir,
        output_dir=args.output_dir,
    ))


if __name__ == "__main__":
    main()
