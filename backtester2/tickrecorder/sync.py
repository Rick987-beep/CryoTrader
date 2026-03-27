#!/usr/bin/env python3
"""
sync.py — Download Parquet Files from VPS and Optionally Clean Up Server

Wraps rsync over SSH to pull completed daily parquets to the local dev
machine. With --delete-after, removes transferred files from the server
after verifying checksums — but never touches the current or previous day.

Usage:
    # Download last 7 days (dry run — default)
    python -m backtester2.tickrecorder.sync --days 7

    # Download last 30 days, then delete from server
    python -m backtester2.tickrecorder.sync --days 30 --delete-after --confirm

    # Download all available data
    python -m backtester2.tickrecorder.sync --all

Config (read from .env):
    RECORDER_VPS_HOST       e.g. ct@46.225.137.92
    RECORDER_VPS_DATA_DIR   e.g. /opt/ct/recorder/data
    RECORDER_SSH_KEY        e.g. /Users/you/.ssh/id_rsa  (optional)
"""
import argparse
import hashlib
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Local target directory (relative to this file's location)
_LOCAL_DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

# Minimum days to always keep on server (never delete current or previous day)
_KEEP_DAYS_ON_SERVER = 2


def _get_env(name, required=True):
    # type: (str, bool) -> Optional[str]
    val = os.getenv(name, "").strip()
    if not val and required:
        logger.error("Missing required env var: %s", name)
        sys.exit(1)
    return val or None


def _date_range(days):
    # type: (int) -> List[str]
    """Return list of YYYY-MM-DD strings for the last N days, oldest first."""
    today = datetime.now(timezone.utc).date()
    return [
        (today - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days - 1, -1, -1)
    ]


def _safe_to_delete(date_str):
    # type: (str) -> bool
    """Return True only if the date is old enough to safely remove from server."""
    today = datetime.now(timezone.utc).date()
    try:
        file_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return False
    return (today - file_date).days >= _KEEP_DAYS_ON_SERVER


def _ssh_cmd(vps_host, ssh_key):
    # type: (str, Optional[str]) -> List[str]
    cmd = ["ssh"]
    if ssh_key:
        cmd += ["-i", ssh_key]
    cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no"]
    return cmd


def _rsync(vps_host, ssh_key, remote_dir, local_dir, filenames):
    # type: (str, Optional[str], str, str, List[str]) -> bool
    """rsync listed files from remote to local. Returns True on success."""
    ssh_str = "ssh -o BatchMode=yes -o StrictHostKeyChecking=no"
    if ssh_key:
        ssh_str += f" -i {ssh_key}"

    # Build include list — rsync filter
    filter_args = []
    for f in filenames:
        filter_args += ["--include", f]
    filter_args += ["--exclude", "*"]

    cmd = [
        "rsync", "-avz", "--progress",
        "-e", ssh_str,
    ] + filter_args + [
        f"{vps_host}:{remote_dir}/",
        f"{local_dir}/",
    ]

    logger.info("rsync: transferring %d files", len(filenames))
    result = subprocess.run(cmd)
    return result.returncode == 0


def _local_sha256(path):
    # type: (str) -> str
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _remote_sha256(vps_host, ssh_key, remote_path):
    # type: (str, Optional[str], str) -> Optional[str]
    cmd = _ssh_cmd(vps_host, ssh_key) + [vps_host, f"sha256sum {remote_path}"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    # sha256sum output: "<hash>  <filename>"
    parts = result.stdout.strip().split()
    return parts[0] if parts else None


def _remote_delete(vps_host, ssh_key, remote_paths):
    # type: (str, Optional[str], List[str]) -> bool
    """Delete a list of files on the remote server."""
    files_str = " ".join(f'"{p}"' for p in remote_paths)
    cmd = _ssh_cmd(vps_host, ssh_key) + [vps_host, f"rm -f {files_str}"]
    result = subprocess.run(cmd)
    return result.returncode == 0


def _remote_list(vps_host, ssh_key, remote_dir):
    # type: (str, Optional[str], str) -> List[str]
    """List parquet filenames in remote data directory."""
    cmd = _ssh_cmd(vps_host, ssh_key) + [
        vps_host, f"ls {remote_dir}/*.parquet 2>/dev/null || true"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        return []
    return [
        os.path.basename(p.strip())
        for p in result.stdout.strip().splitlines()
        if p.strip().endswith(".parquet")
    ]


def run(days=None, all_files=False, delete_after=False, confirm=False, dry_run=True):
    # type: (Optional[int], bool, bool, bool, bool) -> int
    """Main sync logic. Returns exit code (0 = success)."""
    vps_host = _get_env("RECORDER_VPS_HOST")
    remote_dir = _get_env("RECORDER_VPS_DATA_DIR")
    ssh_key = _get_env("RECORDER_SSH_KEY", required=False)

    os.makedirs(_LOCAL_DATA_DIR, exist_ok=True)

    # Determine which dates to sync
    if all_files:
        remote_files = _remote_list(vps_host, ssh_key, remote_dir)
        dates_to_sync = sorted(set(
            f.replace("options_", "").replace(".parquet", "")
            for f in remote_files
            if f.startswith("options_") and not f.startswith(".partial")
        ))
    else:
        days = days or 7
        dates_to_sync = _date_range(days)

    if not dates_to_sync:
        logger.info("No dates to sync.")
        return 0

    logger.info("Dates to sync: %s to %s (%d days)",
                dates_to_sync[0], dates_to_sync[-1], len(dates_to_sync))

    # Build file list
    filenames = []
    for date_str in dates_to_sync:
        filenames.append(f"options_{date_str}.parquet")
        filenames.append(f"spot_track_{date_str}.parquet")

    if dry_run:
        logger.info("[DRY RUN] Would download: %s", ", ".join(filenames))
        logger.info("Pass --confirm to actually transfer files.")
        return 0

    # Transfer
    ok = _rsync(vps_host, ssh_key, remote_dir, _LOCAL_DATA_DIR, filenames)
    if not ok:
        logger.error("rsync failed. Aborting.")
        return 1

    # Verify + delete
    if delete_after and confirm:
        to_delete = []
        failed_verify = []

        for date_str in dates_to_sync:
            if not _safe_to_delete(date_str):
                logger.info("Skipping delete for %s (too recent, keeping on server)", date_str)
                continue

            for prefix in ("options", "spot_track"):
                fname = f"{prefix}_{date_str}.parquet"
                local_path = os.path.join(_LOCAL_DATA_DIR, fname)
                remote_path = f"{remote_dir}/{fname}"

                if not os.path.exists(local_path):
                    logger.warning("Local file missing after transfer: %s", fname)
                    failed_verify.append(fname)
                    continue

                local_hash = _local_sha256(local_path)
                remote_hash = _remote_sha256(vps_host, ssh_key, remote_path)

                if remote_hash is None:
                    logger.warning("Could not get remote checksum for %s", fname)
                    failed_verify.append(fname)
                    continue

                if local_hash == remote_hash:
                    to_delete.append(remote_path)
                    logger.debug("Verified %s — queued for deletion", fname)
                else:
                    logger.warning(
                        "Checksum mismatch for %s (local=%s, remote=%s) — keeping on server",
                        fname, local_hash[:12], remote_hash[:12],
                    )
                    failed_verify.append(fname)

        if failed_verify:
            logger.warning(
                "%d file(s) failed verification — not deleting those from server",
                len(failed_verify),
            )

        if to_delete:
            logger.info("Deleting %d verified files from server...", len(to_delete))
            deleted_ok = _remote_delete(vps_host, ssh_key, to_delete)
            if deleted_ok:
                logger.info("Server cleanup complete. %d files removed.", len(to_delete))
            else:
                logger.warning("Server delete command returned non-zero exit code.")
                return 1
    elif delete_after and not confirm:
        logger.info("--delete-after specified but --confirm missing. No files deleted.")

    logger.info("Sync complete.")
    return 0


def main():
    # type: () -> None
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Download recorder parquets from VPS and optionally clean up server."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--days", type=int, metavar="N",
                       help="Sync last N days (default: 7)")
    group.add_argument("--all", action="store_true",
                       help="Sync all available data files on server")
    parser.add_argument("--delete-after", action="store_true",
                        help="Delete synced files from server after checksum verification "
                             "(keeps last 2 days on server regardless)")
    parser.add_argument("--confirm", action="store_true",
                        help="Actually transfer and delete (without this flag, dry run only)")

    args = parser.parse_args()

    dry_run = not args.confirm

    if dry_run:
        logger.info("DRY RUN mode (pass --confirm to execute)")

    sys.exit(run(
        days=args.days,
        all_files=args.all,
        delete_after=args.delete_after,
        confirm=args.confirm,
        dry_run=dry_run,
    ))


if __name__ == "__main__":
    main()
