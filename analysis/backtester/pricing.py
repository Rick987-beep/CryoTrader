"""
Black-Scholes option pricing, strike grid, vol estimation, and fee model.

Reusable across any crypto option backtesting strategy.
This is a leaf module with zero local imports — usable by any strategy or script.

Key design decisions:
    - r=0 in BS (negligible for sub-24h options)
    - norm_cdf via math.erf (no scipy dependency)
    - Strike grid defaults to Deribit's $500 steps
    - Vol clamped to 15%-300% to prevent numerical blowups
    - Fees use Deribit's MIN(0.03% x index, 12.5% x leg_price) model

Used by: straddle_strangle.py, reporting.py
"""

import math
import statistics

# ── Constants ─────────────────────────────────────────────────────

HOURS_PER_YEAR = 8760.0
STRIKE_STEP = 500          # Deribit 0DTE strike grid
VOL_LOOKBACK = 24          # trailing candles for vol
MIN_VOL_CANDLES = 6        # minimum candles for vol estimate
DEFAULT_VOL = 0.55         # fallback annualized vol
EXPIRY_HOUR_UTC = 8        # Deribit 0DTE expiry


# ── Strike Grid ───────────────────────────────────────────────────

def snap_strike(price, step=STRIKE_STEP):
    """Round to nearest strike increment (e.g. $500 on Deribit)."""
    return round(price / step) * step


def get_strikes(spot, offset, step=STRIKE_STEP):
    """Snapped call/put strikes for a structure.
    offset=0 → ATM straddle; offset>0 → strangle."""
    K_call = snap_strike(spot + offset, step)
    K_put = snap_strike(spot - offset, step)
    return K_call, K_put


# ── Black-Scholes ─────────────────────────────────────────────────

def norm_cdf(x):
    """Standard normal CDF via math.erf (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(S, K, T_years, sigma):
    """BS call price with r=0."""
    if T_years <= 1e-12 or sigma <= 1e-12:
        return max(0.0, S - K)
    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return S * norm_cdf(d1) - K * norm_cdf(d2)


def bs_put(S, K, T_years, sigma):
    """BS put price with r=0."""
    if T_years <= 1e-12 or sigma <= 1e-12:
        return max(0.0, K - S)
    sqrt_T = math.sqrt(T_years)
    d1 = (math.log(S / K) + 0.5 * sigma * sigma * T_years) / (sigma * sqrt_T)
    d2 = d1 - sigma * sqrt_T
    return K * norm_cdf(-d2) - S * norm_cdf(-d1)


def price_structure(spot, offset, dte_hours, sigma, step=STRIKE_STEP):
    """Price a straddle/strangle at entry using BS with snapped strikes.
    Returns (total, call_price, put_price, K_call, K_put) in USD."""
    T = dte_hours / HOURS_PER_YEAR
    K_call, K_put = get_strikes(spot, offset, step)
    call_price = bs_call(spot, K_call, T, sigma)
    put_price = bs_put(spot, K_put, T, sigma)
    return call_price + put_price, call_price, put_price, K_call, K_put


def price_at_exit(exit_spot, K_call, K_put, remaining_hours, sigma):
    """Price at exit using BS with the SAME strikes from entry.
    Returns (total, call_price, put_price) in USD."""
    T = remaining_hours / HOURS_PER_YEAR
    call_price = bs_call(exit_spot, K_call, T, sigma)
    put_price = bs_put(exit_spot, K_put, T, sigma)
    return call_price + put_price, call_price, put_price


def hours_to_expiry(entry_hour, expiry_hour=EXPIRY_HOUR_UTC):
    """Hours until next-day expiry (default 08:00 UTC)."""
    return 24 + expiry_hour - entry_hour


# ── Realized Vol ──────────────────────────────────────────────────

def estimate_vol(sorted_candles, entry_index, lookback=VOL_LOOKBACK):
    """Annualized vol from trailing hourly log returns.
    Resets on gaps > 2h. Returns DEFAULT_VOL if insufficient data."""
    start = max(0, entry_index - lookback)
    window = sorted_candles[start:entry_index]

    if len(window) < 2:
        return DEFAULT_VOL

    log_rets = []
    for j in range(1, len(window)):
        dt_gap = (window[j]["dt"] - window[j - 1]["dt"]).total_seconds()
        if dt_gap > 7200:
            log_rets.clear()
            continue
        prev_close = window[j - 1]["close"]
        curr_close = window[j]["close"]
        if prev_close > 0 and curr_close > 0:
            log_rets.append(math.log(curr_close / prev_close))

    if len(log_rets) < MIN_VOL_CANDLES:
        return DEFAULT_VOL

    hourly_std = statistics.stdev(log_rets)
    annualized = hourly_std * math.sqrt(HOURS_PER_YEAR)
    return max(0.15, min(annualized, 3.0))


# ── Fee Model ─────────────────────────────────────────────────────

def deribit_fee_per_leg(btc_price, leg_price_usd):
    """Deribit: MIN(0.03% of index, 12.5% of option price) per leg per trade."""
    base = 0.0003 * btc_price
    cap = 0.125 * max(leg_price_usd, 0)
    return min(base, cap)
