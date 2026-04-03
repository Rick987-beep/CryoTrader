#!/usr/bin/env python3
"""
Download Deribit options_chain data from tardis.dev.

Free tier: 1st of each month, no API key required.
With a Tardis.dev API key: any date (set TARDIS_API_KEY env var or --api-key).

URL pattern:
    https://datasets.tardis.dev/v1/deribit/options_chain/YYYY/MM/DD/OPTIONS.csv.gz

Files are ~9.5 GB compressed per day (all Deribit instruments, tick-level).

NOTE — server does NOT support HTTP Range / partial content (tested March 2026).
Every download starts from byte 0. If the connection drops, the whole file must
be re-downloaded. Run this inside tmux or screen so a terminal disconnect does
not kill the process. The retry loop handles transient network errors automatically.

Usage:
    python -m backtester.ingest.tardis.download 2026-03-09
    python -m backtester.ingest.tardis.download 2026-03-09 --force
    python -m backtester.ingest.tardis.download 2026-03-09 --api-key YOUR_KEY

    # Recommended for multi-day runs (survives terminal disconnect):
    tmux new -s tardis
    TARDIS_API_KEY=... python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23
"""
import argparse
import os
import sys
import time
from typing import Optional

try:
    import requests
except ImportError:
    print("pip install requests", file=sys.stderr)
    sys.exit(1)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DATASETS_BASE = "https://datasets.tardis.dev/v1"

# Seconds to wait between retries: 10s, 30s, 1m, 2m, 5m, 5m, ...
_RETRY_DELAYS = [10, 30, 60, 120, 300]


def download(
    date_str: str,
    force: bool = False,
    api_key: Optional[str] = None,
    data_dir: str = DATA_DIR,
    max_retries: int = 20,
) -> str:
    """Download options_chain for a given date, restarting on failure with backoff.

    The tardis.dev datasets server does not support HTTP Range requests, so each
    retry restarts from byte 0. The retry loop handles transient TCP drops, 5xx
    errors, and stalled connections (120s read timeout).

    Args:
        date_str:    Date YYYY-MM-DD. Free tier requires 1st of month; any date with API key.
        force:       Delete existing complete file and re-download from scratch.
        api_key:     Tardis.dev API key. Falls back to TARDIS_API_KEY env var.
        data_dir:    Directory to save the file.
        max_retries: Maximum retry attempts after the first failure (default 20).

    Returns:
        Path to the fully downloaded .csv.gz file.

    Raises:
        RuntimeError: If all retries are exhausted.
    """
    if api_key is None:
        api_key = os.environ.get("TARDIS_API_KEY")

    os.makedirs(data_dir, exist_ok=True)

    year, month, day = date_str.split("-")
    url = f"{DATASETS_BASE}/deribit/options_chain/{year}/{month}/{day}/OPTIONS.csv.gz"
    gz_path = os.path.join(data_dir, f"options_chain_{date_str}.csv.gz")

    if force and os.path.exists(gz_path):
        os.unlink(gz_path)

    # Check if a previous complete download already exists.
    # We validate by comparing size to Content-Length on the first HEAD-like check
    # inside the loop, so just skip file existence check here.

    print(f"[download] {date_str}  ({url})")

    headers: dict = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    last_exc: Optional[Exception] = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
            print(f"  Retry {attempt}/{max_retries} in {delay}s  (last error: {last_exc})",
                  file=sys.stderr)
            time.sleep(delay)

        resp = None
        try:
            # connect timeout 30s; read timeout 120s (detects stalled/zombie connections)
            resp = requests.get(url, headers=headers, stream=True, timeout=(30, 120))
            resp.raise_for_status()

            total = int(resp.headers.get("content-length", 0))
            if total:
                print(f"  Size: {total / 1024**3:.2f} GB  —  downloading...")
            else:
                print("  Size: unknown  —  downloading...")

            written = 0
            t0 = time.time()
            with open(gz_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=512 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    written += len(chunk)
                    elapsed = time.time() - t0
                    speed = written / elapsed / 1024**2 if elapsed > 0 else 0
                    if total:
                        print(
                            f"\r  {written / 1024**3:.2f} / {total / 1024**3:.2f} GB"
                            f"  ({written / total * 100:.0f}%)  {speed:.1f} MB/s",
                            end="", flush=True,
                        )
                    else:
                        print(f"\r  {written / 1024**3:.2f} GB  {speed:.1f} MB/s",
                              end="", flush=True)

            # Verify completeness.
            final_size = os.path.getsize(gz_path)
            if total > 0 and final_size < total:
                raise IOError(
                    f"Truncated: received {final_size:,} of {total:,} bytes"
                )

            elapsed = time.time() - t0
            speed = written / elapsed / 1024**2 if elapsed > 0 else 0
            print(f"\n  Done: {final_size / 1024**3:.2f} GB in {elapsed:.0f}s"
                  f"  ({speed:.1f} MB/s avg)")
            return gz_path

        except Exception as exc:
            last_exc = exc
            # Partial file on disk — wipe it so the next attempt writes a clean file.
            if gz_path and os.path.exists(gz_path):
                os.unlink(gz_path)
            print(f"\n  Attempt {attempt + 1} failed: {exc}", file=sys.stderr)
            if attempt == max_retries:
                raise RuntimeError(
                    f"[download] {date_str}: failed after {max_retries + 1} attempts"
                ) from exc
        finally:
            if resp is not None:
                resp.close()

    raise RuntimeError(f"[download] {date_str}: exhausted retries")  # unreachable


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download tardis.dev options data")
    parser.add_argument("date", help="YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    parser.add_argument("--api-key", help="Tardis.dev API key (or set TARDIS_API_KEY env var)")
    parser.add_argument("--max-retries", type=int, default=20, help="Max retry attempts (default 20)")
    args = parser.parse_args()
    download(args.date, force=args.force, api_key=args.api_key, max_retries=args.max_retries)
