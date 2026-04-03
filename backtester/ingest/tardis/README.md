# tardis — Historic Deribit Option Data

Pull historic tick-level Deribit BTC option data from [tardis.dev](https://tardis.dev) and query it for backtesting.

## Workflow

```
1. fetch.py   →   orchestrates the whole pipeline for a date range
                  (download → extract → delete raw, day by day)
2. download.py →  low-level: fetch one day's OPTIONS.csv.gz (~9.5 GB)
3. extract.py →   filter to BTC options ≤ max_dte → compact parquet (~87–180 MB/day)
4. chain.py   →   HistoricOptionChain for fast backtest lookups
```

## Quick Start

### Fetch a date range (recommended)

```bash
export TARDIS_API_KEY="your_key_here"
python -m backtester.ingest.tardis.fetch --from 2026-03-09 --to 2026-03-23
```

Or use the detached launcher (survives closing the terminal):

```bash
export TARDIS_API_KEY="your_key_here"
bash analysis/ingest/tardis/run_fetch.sh
```

Logs to `data/fetch_log.txt`. Check progress with:

```bash
tail -f analysis/ingest/tardis/data/fetch_log.txt
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--from` | required | Start date `YYYY-MM-DD` |
| `--to` | required | End date `YYYY-MM-DD` (inclusive) |
| `--max-dte` | `28` | Drop options expiring more than N days out |
| `--api-key` | `$TARDIS_API_KEY` | API key (or set env var) |
| `--keep-raw` | off | Keep raw `.csv.gz` after extraction |

Days that already have a `.parquet` file are automatically skipped, so the run is safely resumable.

---

## API Key & Data Availability

- **Free tier** — only the 1st of each month, no key needed
- **Paid/trial** — any date within the subscription window; set `TARDIS_API_KEY`

Check your subscription:

```bash
curl "https://api.tardis.dev/v1/api-key-info" \
  -H "Authorization: Bearer $TARDIS_API_KEY"
```

---

## Individual Steps

### Download one day

```bash
python -m backtester.ingest.tardis.download 2026-03-09
python -m backtester.ingest.tardis.download 2026-03-09 --force   # re-download if exists
```

Produces `data/options_chain_2026-03-09.csv.gz` (~9.5 GB).

**Note:** tardis.dev does **not** support HTTP Range / partial content (verified March 2026). A dropped connection requires a full restart. The download module retries automatically (up to 20×, exponential backoff: 10s → 5min).

### Extract one day

```bash
python -m backtester.ingest.tardis.extract 2026-03-09
python -m backtester.ingest.tardis.extract 2026-03-09 --max-dte 7
```

Reads the `.csv.gz`, keeps BTC options whose expiry falls within `max_dte` calendar days of the trade date, writes zstd-compressed parquet.

Produces `data/btc_2026-03-09.parquet` (~87–180 MB).

---

## Querying Data

```python
from backtester.ingest.tardis import HistoricOptionChain

chain = HistoricOptionChain("analysis/ingest/tardis/data/btc_2026-03-09.parquet")

# Single option — returns dict of latest tick at or before the given time
opt = chain.get("2026-03-09 10:05", "9MAR26", 85000, is_call=True)
print(f"ask: {opt['ask_price']:.6f} BTC  mark IV: {opt['mark_iv']:.1f}%")

# ATM straddle (nearest strike to spot)
call, put = chain.get_atm_straddle("2026-03-09 12:00", "9MAR26")

# Full chain snapshot — all strikes for one expiry as a DataFrame
snap = chain.get_chain("2026-03-09 14:00", "9MAR26")
print(snap[["strike", "ask_price", "bid_price", "delta"]])

# Underlying spot price
spot = chain.get_spot("2026-03-09 15:30")

# Available expiries and strikes
print(chain.expiries())
print(chain.strikes("9MAR26"))

# Iterate minute by minute (backtest pattern)
for minute_ts in chain.minutes():
    spot = chain.get_spot(minute_ts)
    call, put = chain.get_atm_straddle(minute_ts, "9MAR26")
    ...
```

Time arguments accept `str` (`"2026-03-09 10:05"`), `datetime`, `pd.Timestamp`, or microsecond `int`.

---

## Parquet Schema

| Column | Type | Description |
|---|---|---|
| `timestamp` | int64 | Microseconds since epoch |
| `expiry` | str (dict-encoded) | e.g. `"9MAR26"` |
| `strike` | float32 | e.g. `85000.0` |
| `is_call` | bool | True = call, False = put |
| `underlying_price` | float32 | BTC spot in USD |
| `mark_price` | float32 | Mark price in BTC |
| `mark_iv` | float32 | Mark implied volatility (%) |
| `bid_price` | float32 | Best bid in BTC |
| `bid_amount` | float32 | Bid size |
| `bid_iv` | float32 | Bid IV (%) |
| `ask_price` | float32 | Best ask in BTC |
| `ask_amount` | float32 | Ask size |
| `ask_iv` | float32 | Ask IV (%) |
| `last_price` | float32 | Last trade price in BTC |
| `open_interest` | float32 | Open interest |
| `delta` | float32 | |
| `gamma` | float32 | |
| `vega` | float32 | |
| `theta` | float32 | |

---

## Performance

| Operation | Time |
|---|---|
| Load + index build | ~0.8 s |
| Single option lookup | ~40 µs |
| ATM straddle | ~90 µs |
| Full chain snapshot (all strikes) | ~3 ms |

Memory: ~87–180 MB per loaded day. Instrument slices are views into the same underlying DataFrame — no duplication.

---

## Storage & Timing

| | Per day | 15 days |
|---|---|---|
| Raw `.csv.gz` (temp) | ~9.5 GB | — (deleted after extract) |
| Parquet output (kept) | ~87–180 MB | ~1.3–2.7 GB |

At ~90 Mbps download speed: ~14 min download + ~20–25 min extract per day. 15 days ≈ 8–10 hours total.

---

## File Structure

```
ingest/tardis/
├── __init__.py       # exports HistoricOptionChain
├── download.py       # fetch one day's raw .csv.gz from tardis.dev
├── extract.py        # filter to BTC ≤ max_dte parquet
├── fetch.py          # orchestrator: download → extract → cleanup, date range loop
├── chain.py          # HistoricOptionChain class
├── run_fetch.sh      # nohup launcher — safe to close terminal
├── README.md
└── data/
    ├── fetch_log.txt            # pipeline log (when using run_fetch.sh)
    ├── fetch_log.txt.pid        # PID of running fetch process
    ├── options_chain_*.csv.gz   # raw downloads (deleted after extract by default)
    └── btc_YYYY-MM-DD.parquet  # extracted datasets — one per day
```
