#!/usr/bin/env python3
"""
Full pipeline: download OPTIONS.csv.gz per day from tardis.dev, extract BTC
options with DTE ≤ max_dte into parquet, then delete the raw file.

Designed for bulk date-range downloads. Skips days that already have a parquet.

API key:
    Set TARDIS_API_KEY environment variable, or pass --api-key.
    Without a key only the 1st of each month is available (free tier).

Output:
    analysis/ingest/tardis/data/btc_YYYY-MM-DD.parquet  (one file per day)

    ~87 MB per day for BTC ≤28 DTE. 15 days ≈ 1.3 GB total.

Usage:
    # Fetch full trial window (March 9–23 2026)
    python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23

    # Fetch with explicit key and custom DTE cap
    python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23 \\
        --api-key YOUR_KEY --max-dte 7

    # Keep raw .csv.gz files after extraction (useful for debugging)
    python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23 --keep-raw
"""
import argparse
import os
import sys
import time
from datetime import date, timedelta
from typing import List, Optional

from backtester.ingest.tardis.download import download
from backtester.ingest.tardis.extract import extract

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _date_range(from_date: str, to_date: str) -> List[date]:
    """Return a list of dates from from_date to to_date inclusive."""
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
    if start > end:
        raise ValueError(f"from_date {from_date} is after to_date {to_date}")
    days = []
    current = start
    while current <= end:
        days.append(current)
        current += timedelta(days=1)
    return days


def fetch(
    from_date: str,
    to_date: str,
    api_key: Optional[str] = None,
    max_dte: int = 28,
    keep_raw: bool = False,
    data_dir: str = DATA_DIR,
    day_retries: int = 3,
) -> List[str]:
    """Download and extract BTC options data for a date range.

    For each day:
      1. Skip if btc_YYYY-MM-DD.parquet already exists.
      2. Download OPTIONS.csv.gz (~9.5 GB per day — no server-side resume).
      3. Extract BTC rows with DTE ≤ max_dte into parquet (~180 MB).
      4. Delete the raw .csv.gz (unless keep_raw=True).

    Designed to run unattended for hours inside tmux or screen.
    download() retries internally (up to 20x, exponential backoff) for transient
    TCP drops and 5xx errors. day_retries handles rarer full-day failures.

    IMPORTANT: tardis.dev does NOT support HTTP Range. A connection drop means the
    entire ~9.5 GB file must be re-downloaded. Run in tmux to survive terminal
    disconnects:
        tmux new -s tardis
        TARDIS_API_KEY=... python -m backtester.ingest.tardis.fetch --from ... --to ...

    Args:
        from_date:   Start date YYYY-MM-DD (inclusive).
        to_date:     End date YYYY-MM-DD (inclusive).
        api_key:     Tardis.dev API key. Falls back to TARDIS_API_KEY env var.
        max_dte:     Max calendar days-to-expiry to keep (default 28).
        keep_raw:    Keep OPTIONS.csv.gz after extraction (default False).
        data_dir:    Directory for all files. Default: data/ next to this module.
        day_retries: Day-level retry attempts before marking a day failed (default 3).

    Returns:
        List of parquet paths successfully written this run.
    """
    if api_key is None:
        api_key = os.environ.get("TARDIS_API_KEY")

    os.makedirs(data_dir, exist_ok=True)
    dates = _date_range(from_date, to_date)

    written: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []

    print(f"{'='*60}")
    print(f"Tardis fetch pipeline")
    print(f"  Range:   {from_date} → {to_date}  ({len(dates)} days)")
    print(f"  Max DTE: {max_dte}")
    print(f"  Output:  {data_dir}")
    print(f"  API key: {'set' if api_key else 'NOT SET — only 1st-of-month available'}")
    print(f"{'='*60}\n")

    t_total = time.time()

    for i, d in enumerate(dates, 1):
        date_str = d.isoformat()
        parquet_path = os.path.join(data_dir, f"btc_{date_str}.parquet")

        print(f"[{i}/{len(dates)}] {date_str}")

        if os.path.exists(parquet_path):
            size = os.path.getsize(parquet_path) / 1024**2
            print(f"  ✓ Already done: {parquet_path} ({size:.1f} MB) — skipping\n")
            skipped.append(date_str)
            continue

        # Retry loop at the day level. download() already retries internally
        # for network drops (up to max_retries), but this outer loop catches
        # rarer failures such as a bad extract or a transient server error that
        # slips past the inner loop.
        # Note: tardis.dev does NOT support HTTP Range, so each retry re-downloads
        # the full ~9.5 GB file. Keep day_retries low (default 3) to avoid wasting
        # bandwidth on a persistently broken day.
        gz_path: Optional[str] = None
        day_success = False
        for day_attempt in range(1, day_retries + 1):
            gz_path = None
            try:
                gz_path = download(date_str, api_key=api_key, data_dir=data_dir)
                out = extract(date_str, gz_path=gz_path, max_dte=max_dte, data_dir=data_dir)
                written.append(out)
                day_success = True
                break
            except Exception as e:
                print(f"  ✗ Day attempt {day_attempt}/{day_retries} failed: {e}",
                      file=sys.stderr)
                # Clean up any partial raw file before retrying.
                if gz_path and os.path.exists(gz_path):
                    os.unlink(gz_path)
                if day_attempt < day_retries:
                    delay = 60 * day_attempt  # 1 min, 2 min, ...
                    print(f"  Waiting {delay}s before day-level retry...", file=sys.stderr)
                    time.sleep(delay)

        if not day_success:
            failed.append(date_str)

        # Clean up raw file once all attempts for this day are done.
        if not keep_raw and gz_path and os.path.exists(gz_path):
            os.unlink(gz_path)
            print(f"  Deleted raw: {gz_path}")

        print()

    elapsed = time.time() - t_total

    print(f"{'='*60}")
    print(f"Pipeline complete in {elapsed:.0f}s")
    print(f"  Written:  {len(written)} days")
    print(f"  Skipped:  {len(skipped)} days (already existed)")
    print(f"  Failed:   {len(failed)} days")
    if failed:
        print(f"  Failed:   {', '.join(failed)}", file=sys.stderr)
    if written:
        total_mb = sum(os.path.getsize(p) / 1024**2 for p in written)
        print(f"  New data: {total_mb:.0f} MB parquet written")
    all_parquets = [
        os.path.join(data_dir, f)
        for f in os.listdir(data_dir)
        if f.startswith("btc_") and f.endswith(".parquet")
    ]
    if all_parquets:
        total_all = sum(os.path.getsize(p) / 1024**2 for p in all_parquets)
        print(f"  Total in data/: {len(all_parquets)} parquet files, {total_all:.0f} MB")

    return written


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Download and extract Deribit BTC options data from tardis.dev",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23
  python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23 --max-dte 7
  TARDIS_API_KEY=YOUR_KEY python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23
        """,
    )
    parser.add_argument("--from", dest="from_date", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--api-key", help="Tardis.dev API key (or set TARDIS_API_KEY env var)")
    parser.add_argument("--max-dte", type=int, default=28, help="Max days-to-expiry (default: 28)")
    parser.add_argument("--keep-raw", action="store_true", help="Keep OPTIONS.csv.gz after extraction")
    args = parser.parse_args()

    fetch(
        from_date=args.from_date,
        to_date=args.to_date,
        api_key=args.api_key,
        max_dte=args.max_dte,
        keep_raw=args.keep_raw,
    )
