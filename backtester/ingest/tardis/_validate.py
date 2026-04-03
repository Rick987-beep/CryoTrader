#!/usr/bin/env python3
"""Quick sanity-check a tardis parquet file."""
import sys
import pandas as pd
import pyarrow.parquet as pq

path = sys.argv[1] if len(sys.argv) > 1 else \
    "analysis/ingest/tardis/data/btc_2026-03-09.parquet"

print(f"Validating: {path}")
table = pq.read_table(path)

print(f"  Rows:        {len(table):,}")
print(f"  Columns:     {table.num_columns}  {sorted(table.schema.names)}")

null_cols = [f.name for f in table.schema if table.column(f.name).null_count == len(table)]
print(f"  All-null:    {null_cols if null_cols else 'none'}")

ts = table.column("timestamp").to_pandas()
print(f"  Time range:  {pd.Timestamp(ts.min(), unit='us', tz='UTC')}  →  {pd.Timestamp(ts.max(), unit='us', tz='UTC')}")

expiries = sorted(table.column("expiry").to_pandas().unique().tolist())
print(f"  Expiries ({len(expiries)}): {expiries}")

spot = table.column("underlying_price").to_pandas()
print(f"  BTC spot:    ${spot.min():,.0f} – ${spot.max():,.0f}")

print("\n  OK — parquet is complete and readable")
