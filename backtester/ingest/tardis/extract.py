#!/usr/bin/env python3
"""
Extract BTC short-dated options (≤ max_dte days to expiry) from a raw
tardis.dev OPTIONS.csv.gz into a compact parquet file for backtesting.

Reads the full options_chain .csv.gz (~4.5 GB, all Deribit instruments),
filters down to BTC options whose expiry is within max_dte calendar days
of the trade date, and writes a zstd-compressed parquet with float32 columns.

The parquet schema is what HistoricOptionChain (chain.py) expects.

Usage:
    python -m backtester.ingest.tardis.extract 2026-03-09
    python -m backtester.ingest.tardis.extract 2026-03-09 --max-dte 7
    python -m backtester.ingest.tardis.extract 2026-03-09 --gz-path /tmp/OPTIONS.csv.gz
"""
import argparse
import gzip
import os
import re
import sys
import time
from datetime import date, datetime
from typing import Dict, Optional

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:
    print("pip install pyarrow", file=sys.stderr)
    sys.exit(1)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

_MONTH = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_EXPIRY_RE = re.compile(r"^(\d{1,2})([A-Z]{3})(\d{2})$")


def _parse_expiry_date(expiry_str: str) -> Optional[date]:
    """Parse Deribit expiry string to a date. '9MAR26' -> date(2026, 3, 9)."""
    m = _EXPIRY_RE.match(expiry_str)
    if not m:
        return None
    month = _MONTH.get(m.group(2))
    if month is None:
        return None
    return date(2000 + int(m.group(3)), month, int(m.group(1)))


def _safe_float(val: str) -> float:
    return float(val) if val else float("nan")


def extract(
    date_str: str,
    gz_path: Optional[str] = None,
    max_dte: int = 28,
    data_dir: str = DATA_DIR,
) -> str:
    """Extract BTC options with DTE ≤ max_dte from raw OPTIONS.csv.gz to parquet.

    Args:
        date_str:  Trade date YYYY-MM-DD used for DTE calculation and output naming.
        gz_path:   Path to OPTIONS.csv.gz. Defaults to data_dir/options_chain_{date_str}.csv.gz.
        max_dte:   Maximum calendar days-to-expiry to include (inclusive). Default 28.
        data_dir:  Output directory. Default: data/ next to this module.

    Returns:
        Path to the written parquet file.
    """
    if gz_path is None:
        gz_path = os.path.join(data_dir, f"options_chain_{date_str}.csv.gz")
    if not os.path.exists(gz_path):
        raise FileNotFoundError(f"Source file not found: {gz_path}")

    trade_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    out_path = os.path.join(data_dir, f"btc_{date_str}.parquet")

    print(f"[extract] {date_str}  source: {os.path.getsize(gz_path):,} bytes  max_dte={max_dte}")

    cols: Dict[str, list] = {
        "timestamp": [], "expiry": [], "strike": [], "is_call": [],
        "underlying_price": [], "mark_price": [], "mark_iv": [],
        "bid_price": [], "bid_amount": [], "bid_iv": [],
        "ask_price": [], "ask_amount": [], "ask_iv": [],
        "last_price": [], "open_interest": [],
        "delta": [], "gamma": [], "vega": [], "theta": [],
    }

    # Cache DTE per expiry string to avoid recomputing on every row.
    # Value is the DTE int, or None if unparseable, or -1 if out-of-range.
    dte_cache: Dict[str, Optional[int]] = {}

    matched = total = 0
    t0 = time.time()

    with gzip.open(gz_path, "rt", errors="replace") as f:
        header = f.readline().strip().split(",")
        idx = {col: i for i, col in enumerate(header)}

        for line in f:
            total += 1
            fields = line.split(",")
            sym = fields[idx["symbol"]]

            if not sym.startswith("BTC-"):
                continue

            parts = sym.split("-")
            if len(parts) != 4:
                continue

            expiry_str = parts[1]

            if expiry_str not in dte_cache:
                exp_date = _parse_expiry_date(expiry_str)
                if exp_date is None:
                    dte_cache[expiry_str] = None
                else:
                    dte_cache[expiry_str] = (exp_date - trade_date).days

            dte = dte_cache[expiry_str]
            if dte is None or dte < 0 or dte > max_dte:
                continue

            matched += 1
            cols["timestamp"].append(int(fields[idx["timestamp"]]))
            cols["expiry"].append(expiry_str)
            cols["strike"].append(float(parts[2]))
            cols["is_call"].append(parts[3].rstrip() == "C")
            cols["underlying_price"].append(_safe_float(fields[idx["underlying_price"]]))
            cols["mark_price"].append(_safe_float(fields[idx["mark_price"]]))
            cols["mark_iv"].append(_safe_float(fields[idx["mark_iv"]]))
            cols["bid_price"].append(_safe_float(fields[idx["bid_price"]]))
            cols["bid_amount"].append(_safe_float(fields[idx["bid_amount"]]))
            cols["bid_iv"].append(_safe_float(fields[idx["bid_iv"]]))
            cols["ask_price"].append(_safe_float(fields[idx["ask_price"]]))
            cols["ask_amount"].append(_safe_float(fields[idx["ask_amount"]]))
            cols["ask_iv"].append(_safe_float(fields[idx["ask_iv"]]))
            cols["last_price"].append(_safe_float(fields[idx["last_price"]]))
            cols["open_interest"].append(_safe_float(fields[idx["open_interest"]]))
            cols["delta"].append(_safe_float(fields[idx["delta"]]))
            cols["gamma"].append(_safe_float(fields[idx["gamma"]]))
            cols["vega"].append(_safe_float(fields[idx["vega"]]))
            cols["theta"].append(_safe_float(fields[idx["theta"]]))

            if matched % 500_000 == 0:
                print(
                    f"  {matched:>10,} matched / {total:>12,} scanned"
                    f"  ({time.time() - t0:.0f}s)"
                )

    elapsed = time.time() - t0
    print(f"  Scan done: {matched:,} rows from {total:,} total  ({elapsed:.0f}s)")

    if matched == 0:
        raise ValueError(f"No BTC rows with DTE ≤ {max_dte} found in {gz_path}")

    os.makedirs(data_dir, exist_ok=True)
    table = pa.table({
        "timestamp":        pa.array(cols["timestamp"],        type=pa.int64()),
        "expiry":           pa.array(cols["expiry"],           type=pa.dictionary(pa.int8(), pa.string())),
        "strike":           pa.array(cols["strike"],           type=pa.float32()),
        "is_call":          pa.array(cols["is_call"],          type=pa.bool_()),
        "underlying_price": pa.array(cols["underlying_price"], type=pa.float32()),
        "mark_price":       pa.array(cols["mark_price"],       type=pa.float32()),
        "mark_iv":          pa.array(cols["mark_iv"],          type=pa.float32()),
        "bid_price":        pa.array(cols["bid_price"],        type=pa.float32()),
        "bid_amount":       pa.array(cols["bid_amount"],       type=pa.float32()),
        "bid_iv":           pa.array(cols["bid_iv"],           type=pa.float32()),
        "ask_price":        pa.array(cols["ask_price"],        type=pa.float32()),
        "ask_amount":       pa.array(cols["ask_amount"],       type=pa.float32()),
        "ask_iv":           pa.array(cols["ask_iv"],           type=pa.float32()),
        "last_price":       pa.array(cols["last_price"],       type=pa.float32()),
        "open_interest":    pa.array(cols["open_interest"],    type=pa.float32()),
        "delta":            pa.array(cols["delta"],            type=pa.float32()),
        "gamma":            pa.array(cols["gamma"],            type=pa.float32()),
        "vega":             pa.array(cols["vega"],             type=pa.float32()),
        "theta":            pa.array(cols["theta"],            type=pa.float32()),
    })
    pq.write_table(table, out_path, compression="zstd")

    size = os.path.getsize(out_path)
    print(f"  Saved: {out_path}  ({size / 1024**2:.1f} MB,  {len(table):,} rows)")

    # Quick summary using pyarrow (fast, no Python-list iteration).
    import pyarrow.compute as pc
    ts_col = table.column("timestamp")
    t_min = datetime.utcfromtimestamp(pc.min(ts_col).as_py() / 1e6)
    t_max = datetime.utcfromtimestamp(pc.max(ts_col).as_py() / 1e6)
    print(f"    Time range: {t_min:%H:%M} – {t_max:%H:%M} UTC")
    unique_expiries = sorted(dte_cache.keys())
    print(f"    Expiries ({len(unique_expiries)}): {unique_expiries}")

    return out_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract BTC options to parquet")
    parser.add_argument("date", help="Trade date YYYY-MM-DD")
    parser.add_argument("--max-dte", type=int, default=28, help="Max days-to-expiry (default: 28)")
    parser.add_argument("--gz-path", help="Override path to OPTIONS.csv.gz")
    args = parser.parse_args()
    extract(args.date, gz_path=args.gz_path, max_dte=args.max_dte)
