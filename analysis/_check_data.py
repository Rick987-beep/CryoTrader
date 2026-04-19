"""Quick check: UTC timestamps and bid/ask data quality."""
import pandas as pd

df = pd.read_parquet("backtester/ingest/tardis/data/btc_0dte_1dte_2025-03-01.parquet")

ts_min, ts_max = df["timestamp"].min(), df["timestamp"].max()
print("Timestamp range (microseconds since epoch):")
print("  min:", ts_min, "=", pd.Timestamp(ts_min, unit="us", tz="UTC"))
print("  max:", ts_max, "=", pd.Timestamp(ts_max, unit="us", tz="UTC"))
print()

print("Columns:", list(df.columns))
print()

# Sample bid/ask for ATM at 10:05 UTC
t_target = int(pd.Timestamp("2025-03-01 10:05", tz="UTC").timestamp() * 1e6)
mask = (df["expiry"] == "2MAR25") & (df["strike"] == 85000.0) & (df["is_call"] == True)
sub = df[mask].copy()
sub = sub[sub["timestamp"] <= t_target]
row = sub.iloc[-1]
spot = row["underlying_price"]

print("Sample ATM 85000 call at 10:05 UTC:")
print("  mark_price: %.8f BTC = $%.2f" % (row["mark_price"], row["mark_price"] * spot))
print("  bid_price:  %.8f BTC = $%.2f" % (row["bid_price"], row["bid_price"] * spot))
print("  ask_price:  %.8f BTC = $%.2f" % (row["ask_price"], row["ask_price"] * spot))
print("  bid_amount: %.1f" % row["bid_amount"])
print("  ask_amount: %.1f" % row["ask_amount"])
print("  spot:       $%.2f" % spot)
print()

# Bid/Ask NaN rates
print("Bid/Ask NaN rates:")
for col in ["bid_price", "ask_price", "bid_amount", "ask_amount"]:
    nan_count = df[col].isna().sum()
    pct = nan_count / len(df) * 100
    print("  %s NaN: %d / %d (%.1f%%)" % (col, nan_count, len(df), pct))

# Zero rates
print()
print("Zero rates:")
for col in ["bid_price", "ask_price"]:
    zero_count = (df[col] == 0).sum()
    print("  %s == 0: %d (%.1f%%)" % (col, zero_count, zero_count / len(df) * 100))
