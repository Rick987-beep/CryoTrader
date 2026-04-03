#!/bin/bash
# Runs the full tardis fetch pipeline detached from the terminal.
# Output goes to data/fetch_log.txt — safe to close VS Code.
#
# Usage: bash analysis/ingest/tardis/run_fetch.sh
#
# Monitor: tail -f analysis/ingest/tardis/data/fetch_log.txt

set -e

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LOG="$REPO_DIR/analysis/ingest/tardis/data/fetch_log.txt"
VENV="$REPO_DIR/.venv/bin/activate"

mkdir -p "$REPO_DIR/analysis/ingest/tardis/data"

if [ -z "$TARDIS_API_KEY" ]; then
  echo "ERROR: TARDIS_API_KEY is not set." >&2
  exit 1
fi

echo "Starting tardis fetch pipeline (detached)..."
echo "Log: $LOG"
echo "PID will be in: $LOG.pid"

source "$VENV"

# caffeinate prevents macOS from sleeping while the pipeline runs.
# It exits automatically when the Python process finishes.
nohup caffeinate -i python -m backtester.ingest.tardis.fetch \
  --from 2026-03-10 \
  --to 2026-03-23 \
  >> "$LOG" 2>&1 &

echo $! > "$LOG.pid"
echo "Started. PID: $!"
echo ""
echo "Monitor progress:"
echo "  tail -f $LOG"
echo ""
echo "Check if still running:"
echo "  ps -p \$(cat $LOG.pid)"
