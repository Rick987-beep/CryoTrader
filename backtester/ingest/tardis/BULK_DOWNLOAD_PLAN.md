# Bulk Tardis Download Plan — ~400 Days of Deribit BTC Options

## Status (as of 1 Apr 2026)

| | |
|---|---|
| **Already have (tick-level)** | Mar 9–23 2026 (15 days, ~500–750 MB/day tick parquets) |
| **Already have (snapshots)** | Mar 9–23 2026 in `data/options_*.parquet` + `spot_track_*.parquet` |
| **Single test file** | 2025-03-01 (0dte/1dte only) |
| **Target range** | ~Feb 6 2025 → Mar 23 2026 (~400 days) |
| **Still needed** | ~385 days |
| **Home Mac download** | 5 MB/s — too slow (~30 min/day download alone → 8+ days) |
| **Solution** | Hetzner server in Nuremberg, ~100 MB/s inbound, 4 parallel workers |

---

## Pipeline Design

### The core insight: stream directly to snapshots, skip tick parquets

The existing pipeline has two stages:
1. `extract.py` → writes a **tick-level parquet** (~500–750 MB/day)
2. `snapshot_builder.py` → reads tick parquet → writes **snapshots** (~1–2 MB/day)

The tick parquet is pure overhead — we don't use it for backtesting, only the snapshots are consumed by `market_replay.py`.

**New pipeline: one pass from raw .csv.gz → snapshots, no tick parquet written**

```
OPTIONS.csv.gz (~9.5 GB)
        │
        ▼
  stream_extract.py         ← NEW — streams gzip line by line
        │
        ├── maintains: dict[instrument] → (last known quote)
        │   updated on every matching row
        │
        ├── at each 5-min boundary: snapshot all instruments → append to list
        │
        └── from underlying_price column: 1-min OHLC bars built inline
        │
        ▼
  Two small parquet files written:
    data/options_YYYY-MM-DD.parquet   (~1–2 MB)
    data/spot_YYYY-MM-DD.parquet      (~20 KB)
        │
        ▼
  raw .csv.gz deleted
```

Peak disk per worker at any moment: **1 raw file (~9.5 GB) + growing output (~2 MB)**

---

## Output Schema

Matches exactly what `market_replay.py` and the backtester already consume.

### `data/options_YYYY-MM-DD.parquet`

| Column | Type | Notes |
|---|---|---|
| `timestamp` | int64 | Microseconds since epoch, 5-min boundaries |
| `expiry` | category | e.g. `"9MAR26"` |
| `strike` | float32 | e.g. `85000.0` |
| `is_call` | bool | |
| `underlying_price` | float32 | BTC spot at snapshot time |
| `bid_price` | float32 | BTC-denominated |
| `ask_price` | float32 | |
| `mark_price` | float32 | |
| `mark_iv` | float32 | % |
| `delta` | float32 | |

~288 boundaries/day × ~300 instruments ≈ 86,000 rows. Target size: ~1–2 MB/day.

### `data/spot_YYYY-MM-DD.parquet`

| Column | Type | Notes |
|---|---|---|
| `timestamp` | int64 | Microseconds, 1-min boundaries |
| `open` | float32 | |
| `high` | float32 | |
| `low` | float32 | |
| `close` | float32 | |

~1440 rows/day. Target size: ~20 KB/day.

---

## New Scripts to Write

Three small, self-contained scripts. No dependency on the existing pipeline.

| Script | Purpose |
|---|---|
| `stream_extract.py` | Core: stream one day's .csv.gz → two snapshot parquets |
| `bulk_fetch.py` | Orchestrator: date-range loop, download + stream_extract + delete raw, skips days already done |
| `run_bulk.sh` | tmux launcher: splits date range into 4 workers, one per window |

`stream_extract.py` key design:
- Streams gzip line by line — never loads full file into memory
- Maintains `last_quote: dict[(expiry, strike, is_call)] → row_values` — updated on every matching row
- Time advances: at each 5-min UTC boundary, flush `last_quote` to snapshot list
- Spot OHLC: accumulate `underlying_price` per 1-min bucket inline
- Writes both parquets at end, deletes raw file

---

## Data Size Estimates

| | Per day | 400 days |
|---|---|---|
| Raw `.csv.gz` (Tardis inbound, deleted after extract) | ~9.5 GB | ~3.8 TB inbound |
| Options snapshot (kept) | ~1–2 MB | ~400–800 MB |
| Spot track (kept) | ~20 KB | ~8 MB |
| **Total output** | **~2 MB** | **~800 MB** |
| **Peak disk per worker** | 1 raw file + output = **~10 GB** | |
| **4 workers simultaneously** | 4 × ~10 GB = **~40 GB peak** | |

---

## Server Choice

**CCX33 — Hetzner dedicated AMD, Nuremberg (NBG)**  
With streaming-to-snapshots, disk requirements drop dramatically. CCX33 is now more than enough.

| Spec | Value |
|---|---|
| CPU | 8 vCPU AMD dedicated |
| RAM | 32 GB |
| SSD | 240 GB (need ~40 GB peak + OS = well within limits) |
| Network | 1 Gbit/s inbound — **free inbound** at Hetzner |
| Outbound | 20 TB included |
| **Price** | €0.077/hr |
| **Est. cost** | ~€1.50–2.00 total |

---

## Timing Estimate

Per day per worker:
- Download ~9.5 GB at ~100 MB/s: **~1.5–2 min**
- Stream extract + snapshot build: **~8–12 min** (gzip decompression dominates)
- Delete raw: instant

**4 parallel workers, 100 days each: ~100 × 10 min = ~17 hrs wall-clock**

The bottleneck is decompressing and scanning the raw CSV, not network. 4 workers fully parallelize this.

---

## Step-by-Step Setup

### 1. Create the server

1. Go to https://console.hetzner.com/
2. New Project → Add Server
   - Location: **Nuremberg (NBG1)**
   - Image: **Ubuntu 24.04**
   - Type: Dedicated → **CCX33**
   - SSH key: paste your `~/.ssh/id_ed25519.pub`
3. Note the server IP

### 2. Bootstrap

```bash
ssh root@<SERVER_IP>

apt-get update && apt-get install -y python3-pip python3-venv git rsync tmux

# Copy the new scripts to the server (run from Mac after writing them)
rsync -av \
  /Users/ulrikdeichsel/CoincallTrader/backtester/tardis_bulk/ \
  root@<SERVER_IP>:/bulk/

# On the server
cd /bulk
python3 -m venv .venv
source .venv/bin/activate
pip install requests pyarrow zstandard

python -c "import requests, pyarrow, zstandard; print('OK')"
```

### 3. API key

```bash
echo 'export TARDIS_API_KEY="your_key_here"' >> ~/.bashrc
source ~/.bashrc
```

### 4. Smoke test — one free day

```bash
cd /bulk && source .venv/bin/activate

# First of any month is free on Tardis
python stream_extract.py --date 2025-04-01

ls -lh data/
# options_2025-04-01.parquet  ~1-2 MB
# spot_2025-04-01.parquet     ~20 KB
```

---

## Running 4 Parallel Workers

Split ~400 days into 4 blocks. Adjust boundaries based on your Tardis subscription window.

| Worker | From | To | ~Days |
|---|---|---|---|
| A | 2025-02-06 | 2025-06-15 | 130 |
| B | 2025-06-16 | 2025-10-23 | 130 |
| C | 2025-10-24 | 2026-02-04 | 104 |
| D | 2026-02-05 | 2026-03-08 | 31 (Mar 9–23 already done on Mac) |

`run_bulk.sh` will open tmux with 4 windows and start each worker automatically:

```bash
bash run_bulk.sh
# Detach: Ctrl-B D
# Reattach any time: tmux attach -t bulk
```

Or manually:

```bash
tmux new -s bulk

# Window 0
python bulk_fetch.py --from 2025-02-06 --to 2025-06-15 --worker A

# Ctrl-B C
python bulk_fetch.py --from 2025-06-16 --to 2025-10-23 --worker B

# Ctrl-B C
python bulk_fetch.py --from 2025-10-24 --to 2026-02-04 --worker C

# Ctrl-B C
python bulk_fetch.py --from 2026-02-05 --to 2026-03-08 --worker D
```

`bulk_fetch.py` skips days that already have both output parquets → safely resumable if a worker crashes.

---

## Monitoring

```bash
# How many days done across all workers?
ls /bulk/data/options_*.parquet | wc -l

# Disk usage — should stay well under 240 GB
df -h /

# Check a specific worker
tmux attach -t bulk
# Ctrl-B 0/1/2/3 to switch windows

# Quick sanity check on a finished day
python -c "
import pyarrow.parquet as pq
f = pq.read_metadata('data/options_2025-06-01.parquet')
print(f.num_rows, 'rows')
"
```

---

## Getting Data Back to Mac

Total output is ~800 MB — rsync takes under 3 minutes.

```bash
# From Mac
rsync -av --progress \
  root@<SERVER_IP>:/bulk/data/ \
  /Users/ulrikdeichsel/CoincallTrader/backtester/data/
# No -z flag: parquets are already zstd-compressed
```

Merge with the existing 15-day snapshots already on Mac before running backtests.  
`market_replay.py` currently loads a single multi-day parquet — may need a small loader change to read per-day files and concatenate, or pre-merge on the server.

---

## Cost Breakdown

| Phase | Duration | Cost |
|---|---|---|
| 4 parallel workers running | ~17 hrs | €1.31 |
| Setup + testing + buffer | ~3 hrs | €0.23 |
| **Total** | **~20 hrs** | **~€1.55** |

Destroy the server immediately after rsync to Mac.

---

## IMPORTANT: No HTTP Range Support

`tardis.dev` does **not** support partial content / HTTP Range. A dropped mid-download connection requires a full restart of that day's ~9.5 GB file. Running in tmux on Hetzner (not your laptop) eliminates all local disconnection risk. `bulk_fetch.py` will retry failed days automatically.

---

## Cleanup

```bash
# Verify before destroying
ls /bulk/data/options_*.parquet | wc -l   # should be ~400
ls /bulk/data/spot_*.parquet | wc -l       # same

# After rsync to Mac confirmed — delete server from Hetzner console
# https://console.hetzner.com/ → Server → Delete
```

---

## Backtester Memory Design (400-day scale)

### Why the current approach breaks at scale

`MarketReplay` currently calls `col.tolist()` per timestamp group to produce Python list-of-tuples stored in `_opt_groups`. Each Python float is a 24-byte heap object.

| | 15 days (current) | 400 days |
|---|---|---|
| Total option rows | ~1.3M | ~34.5M |
| `_opt_groups` RAM (Python objects) | ~260 MB | **~7 GB** — not viable |
| Load time | ~1 s | ~60–90 s |

### Recommended format: columnar NumPy arrays

Never box into Python objects. Keep every column as a contiguous NumPy array, sorted by `(timestamp, expiry_idx, strike, is_call)`.

```
Option data (34.5M rows):
  timestamp:        int64[34.5M]      276 MB
  expiry_idx:       uint8[34.5M]       35 MB   ← index into string table of ~30 expiries
  strike:          float32[34.5M]     138 MB
  is_call:            bool[34.5M]      35 MB
  bid_price:       float32[34.5M]     138 MB
  ask_price:       float32[34.5M]     138 MB
  mark_price:      float32[34.5M]     138 MB
  mark_iv:         float32[34.5M]     138 MB
  delta:           float32[34.5M]     138 MB
  underlying_px:   float32[34.5M]     138 MB
  ────────────────────────────────────────────
  Subtotal:                          ~1.31 GB

Timestamp index (tiny):
  ts_sorted:        int64[115,200]      0.9 MB  — sorted unique timestamps
  ts_starts:        int32[115,200]      0.5 MB  — start row for each timestamp
  ts_lens:          int32[115,200]      0.5 MB  — row count per timestamp

Spot track (1-min, 400 days):
  spot_ts:          int64[576,000]      4.6 MB
  open/high/low/close: float32         ~9.2 MB
  cum_high/cum_low: float64            ~9.2 MB  ← O(1) excursion lookups, keep float64
  ────────────────────────────────────────────
  Subtotal:                            ~23 MB

══════════════════════════════════════════════
TOTAL IN RAM:                        ~1.35 GB
══════════════════════════════════════════════
```

Parquet on disk (~800 MB) expands ~1.7× in RAM — vs ~8× with Python object boxing.

### Per-tick option lookup

At each of 115,200 ticks, a strategy typically accesses 1–3 instruments. With the timestamp index this is a vectorised scan over ~300 rows:

```python
start  = ts_starts[ts_idx]
length = ts_lens[ts_idx]

# Vectorised comparison on 300 elements — ~0.5 µs
mask = (
    (expiry_idx[start:start+length] == target_exp_idx) &
    (strike[start:start+length]     == target_strike)  &
    (is_call[start:start+length]    == target_is_call)
)
row = start + int(np.argmax(mask))
bid_val  = float(bid[row])
ask_val  = float(ask[row])
# etc.
```

No Python float allocation until a result is consumed by strategy logic.

### Loading all 400 days at startup

Use PyArrow dataset API to read all per-day parquets in one pass, then convert to NumPy:

```python
import pyarrow.dataset as ds
import numpy as np

table = ds.dataset("backtester/data/", format="parquet").to_table(
    columns=["timestamp","expiry","strike","is_call",
             "bid_price","ask_price","mark_price","mark_iv","delta","underlying_price"]
)
opt_df = table.to_pandas()

# Encode expiry strings as uint8 indices — avoids boxing
expiry_cat   = opt_df["expiry"].astype("category")
expiry_table = list(expiry_cat.cat.categories)     # ~30 strings
expiry_idx   = expiry_cat.cat.codes.values.astype(np.uint8)

timestamps = opt_df["timestamp"].values             # int64, already sorted
strike     = opt_df["strike"].values.astype(np.float32)
bid        = opt_df["bid_price"].values.astype(np.float32)
# ... etc for remaining columns

# Build timestamp index in one pass
ts_sorted, ts_starts, ts_counts = np.unique(
    timestamps, return_index=True, return_counts=True
)
ts_lens = ts_counts.astype(np.int32)
ts_starts = ts_starts.astype(np.int32)
```

Load time estimate: ~10–15 s for 400 days. After that, iteration over all 115,200 ticks costs ~0.1 s in data access — strategy logic will dominate runtime.

### Impact on market_replay.py

The new `MarketReplay` for the full 400-day backtester should:
- Replace `_opt_groups` (dict of Python tuple lists) with the columnar NumPy layout above
- Replace per-group `col.tolist()` with a single bulk `to_pandas()` + `.values` pass at load
- Keep the `MarketState` / `OptionQuote` API unchanged — strategies don't need to change
- Accept a directory path (all per-day parquets) in addition to a single merged file
