#!/usr/bin/env python3
"""
Option Selection Module

Two public APIs for finding options:

1. select_option() — legacy single-criterion selection.
   Picks one option by expiry + one strike criterion (delta, closestStrike,
   spotdistance%, or exact strike).  Used by LegSpec / resolve_legs().

2. find_option() — compound multi-constraint selection (recommended).
   Accepts simultaneous expiry window, strike filters (ATM direction,
   distance %, OTM %), delta range, and a ranking strategy.  Returns an
   enriched dict with delta, days-to-expiry, distance-from-ATM, and the
   index price at selection time.

Both functions are purely additive — they share the same market-data helpers
but neither depends on the other.

Also exports:
- LegSpec dataclass  — declarative leg template for strategies
- resolve_legs()     — converts LegSpec list → TradeLeg list
"""

import time
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional
logger = logging.getLogger(__name__)


# =============================================================================
# Leg Specification — Declarative Leg Templates
# =============================================================================

@dataclass
class LegSpec:
    """
    Declarative leg template — resolved to a concrete TradeLeg at trade time.

    Strategies define legs as LegSpecs with criteria (e.g., "25-delta call").
    At execution time, resolve_legs() calls select_option() for each spec
    and returns TradeLeg instances with real symbols.

    Attributes:
        option_type: "C" for call, "P" for put
        side: 1=buy, 2=sell
        qty: Quantity for this leg
        strike_criteria: e.g. {"type": "delta", "value": 0.25}
        expiry_criteria: e.g. {"symbol": "28MAR26"}
        underlying: Underlying asset (default "BTC")
    """
    option_type: str
    side: str
    qty: float
    strike_criteria: dict
    expiry_criteria: dict
    underlying: str = "BTC"


def resolve_legs(specs: List[LegSpec], market_data) -> list:
    """
    Resolve a list of LegSpec templates into concrete TradeLeg objects.

    Each LegSpec's criteria are passed to select_option() to find the
    matching symbol. Returns a list of TradeLeg instances ready for
    LifecycleManager.create().

    Args:
        specs: List of LegSpec templates
        market_data: ExchangeMarketData adapter for the active exchange

    Returns:
        List of TradeLeg objects with resolved symbols

    Raises:
        ValueError: If any leg cannot be resolved to a real option symbol
    """
    from trade_lifecycle import TradeLeg

    resolved = []
    for i, spec in enumerate(specs):
        symbol = select_option(
            expiry_criteria=spec.expiry_criteria,
            strike_criteria=spec.strike_criteria,
            option_type=spec.option_type,
            underlying=spec.underlying,
            market_data=market_data,
        )
        if symbol is None:
            raise ValueError(
                f"Could not resolve leg {i}: {spec.option_type} "
                f"strike={spec.strike_criteria} expiry={spec.expiry_criteria}"
            )
        resolved.append(TradeLeg(symbol=symbol, qty=spec.qty, side=spec.side))
        logger.info(f"Resolved leg {i}: {spec.option_type} {spec.strike_criteria} -> {symbol}")

    return resolved


# =============================================================================
# Option Selection
# =============================================================================

def select_option(expiry_criteria, strike_criteria, option_type='C', underlying='BTC', market_data=None):
    """
    Select an option based on expiry and strike criteria.

    Args:
        expiry_criteria (dict): Expiry criteria - either {'symbol': '5FEB26'} or {'minExp': days, 'maxExp': days}
        strike_criteria (dict): Strike criteria - {'type': 'delta', 'value': 0.25} or other types
        option_type (str): 'C' for call, 'P' for put
        underlying (str): Underlying symbol, default 'BTC'
        market_data: ExchangeMarketData adapter for the active exchange

    Returns:
        str: Option symbol or None if not found
    """
    try:
        # Get available options
        options_list = market_data.get_option_instruments(underlying)
        if not options_list:
            return None

        # Filter by expiry
        expiry_options = _filter_by_expiry(options_list, expiry_criteria, option_type)
        if not expiry_options:
            return None

        # For delta selection, fetch delta for each option
        if strike_criteria.get('type') == 'delta':
            expiry_options = _add_delta_to_options(
                expiry_options, market_data, target_delta=strike_criteria.get('value')
            )

        # Select strike based on criteria
        selected_option = _select_by_strike_criteria(expiry_options, strike_criteria, market_data)

        if selected_option:
            delta_info = f", delta: {selected_option.get('delta', 'N/A')}"
            logger.info(f"Selected option: {selected_option['symbolName']} (strike: {selected_option['strike']}{delta_info})")
            return selected_option['symbolName']

        return None

    except Exception as e:
        logger.error(f"Error selecting option: {e}")
        return None


def _filter_by_expiry(options_list, expiry_criteria, option_type):
    """
    Filter options by expiry criteria.

    Args:
        options_list (list): List of option instruments
        expiry_criteria (dict): Expiry criteria
        option_type (str): 'C' or 'P'

    Returns:
        list: Filtered options
    """
    # Three expiry matching modes supported:
    # - symbol: match by symbolName substring like '-4FEB26-' (preferred, no ms math)
    # - dte: dynamic days-to-expiry (0 = today, 1 = tomorrow, etc.)
    # - minExp/maxExp: legacy days-based matching using expirationTimestamp

    if isinstance(expiry_criteria, dict) and 'dte' in expiry_criteria:
        dte = expiry_criteria['dte']
        now_ms = time.time() * 1000
        today_start_ms = _utc_day_start_ms()

        if dte == "next":
            # "next" — pick the nearest available expiry that hasn't expired yet
            valid_options = [
                opt for opt in options_list
                if opt.get('expirationTimestamp', 0) > now_ms
                and opt['symbolName'].endswith('-' + option_type)
            ]
            if not valid_options:
                logger.error(f"No unexpired options for type {option_type}")
                return []

            # Find the soonest expiry timestamp
            nearest_ts = min(opt['expirationTimestamp'] for opt in valid_options)
            expiry_options = [
                opt for opt in valid_options
                if opt['expirationTimestamp'] == nearest_ts
            ]
            days_away = (nearest_ts - now_ms) / 86400_000
            logger.info(
                f"DTE='next': selected expiry {expiry_options[0]['symbolName'].split('-')[1]} "
                f"({days_away:.1f} days away, {len(expiry_options)} strikes)"
            )
            return expiry_options

        # Numeric DTE matching
        dte_min = expiry_criteria.get('dte_min', dte)
        dte_max = expiry_criteria.get('dte_max', dte)

        min_expiry_ms = today_start_ms + dte_min * 86400_000
        # max is end-of-day for dte_max
        max_expiry_ms = today_start_ms + (dte_max + 1) * 86400_000 - 1

        valid_options = [
            opt for opt in options_list
            if min_expiry_ms <= opt.get('expirationTimestamp', 0) <= max_expiry_ms
            and opt['symbolName'].endswith('-' + option_type)
        ]
        if not valid_options:
            logger.error(
                f"No options with DTE in [{dte_min}, {dte_max}] and type {option_type}"
            )
            return []

        # Collapse to the single nearest-DTE expiry
        target_ms = today_start_ms + dte * 86400_000 + 43200_000  # noon of target day
        closest = min(valid_options, key=lambda x: abs(x['expirationTimestamp'] - target_ms))
        expiry_ts = closest['expirationTimestamp']
        expiry_options = [
            opt for opt in valid_options
            if opt['expirationTimestamp'] == expiry_ts
        ]
        return expiry_options

    elif isinstance(expiry_criteria, dict) and 'symbol' in expiry_criteria:
        sym = expiry_criteria['symbol']
        # Match symbolName containing the expiry token and option type
        expiry_options = [opt for opt in options_list if (f"-{sym}-" in opt.get('symbolName', '')) and opt['symbolName'].endswith('-' + option_type)]
        if not expiry_options:
            logger.error(f"No options matching symbol expiry {sym} and type {option_type}")
            return []
    else:
        # Legacy time-based matching
        current_time = time.time() * 1000  # Convert to milliseconds for API
        min_expiry = current_time + expiry_criteria['minExp'] * 86400 * 1000
        max_expiry = current_time + expiry_criteria['maxExp'] * 86400 * 1000

        # Filter options by expiry range and type
        valid_options = [opt for opt in options_list if min_expiry <= opt['expirationTimestamp'] <= max_expiry and opt['symbolName'].endswith('-' + option_type)]
        if not valid_options:
            logger.error(f"No options within expiry range {expiry_criteria} and type {option_type}")
            return []

        # Find closest expiry and filter to that expiry
        target_expiry = (min_expiry + max_expiry) / 2
        closest_expiry_opt = min(valid_options, key=lambda x: abs(x['expirationTimestamp'] - target_expiry))
        expiry_date = closest_expiry_opt['expirationTimestamp']
        expiry_options = [opt for opt in valid_options if opt['expirationTimestamp'] == expiry_date]

    return expiry_options


def _add_delta_to_options(options_list, market_data, target_delta: float = None):
    """
    Add delta values to option instruments by fetching details.

    When a target_delta is provided the list is pre-sorted so that strikes
    most likely to match the target are queried first (high strikes for
    low call deltas, low strikes for low put deltas).  This lets us cap
    API calls at a reasonable number without cutting off OTM options.

    Args:
        options_list (list): List of option instruments
        market_data: ExchangeMarketData adapter
        target_delta (float|None): Target delta for pre-sorting heuristic

    Returns:
        list: Options with delta added
    """
    MAX_API_CALLS = 50  # enough for any single expiry

    # Pre-sort: order strikes so the region likely to contain target_delta
    # is queried first.  For small positive deltas (far-OTM calls) we want
    # high strikes first; for small negative deltas (far-OTM puts) we want
    # low strikes first; otherwise sort ascending (ATM region first).
    if target_delta is not None:
        if 0 < target_delta < 0.25:
            # Low call delta → high strikes first
            sorted_options = sorted(options_list, key=lambda o: -o.get('strike', 0))
        elif -0.25 < target_delta < 0:
            # Low put delta → low strikes first
            sorted_options = sorted(options_list, key=lambda o: o.get('strike', 0))
        else:
            # Near-ATM → sort ascending (default)
            sorted_options = sorted(options_list, key=lambda o: o.get('strike', 0))
    else:
        sorted_options = list(options_list)

    options_with_delta = []
    for opt in sorted_options[:MAX_API_CALLS]:
        try:
            details = market_data.get_option_details(opt['symbolName'])
            if details and 'delta' in details:
                delta = float(details['delta'])
                opt['delta'] = delta
                options_with_delta.append(opt)
            else:
                logger.warning(f"Could not get delta details for {opt['symbolName']}: {details}")
        except Exception as e:
            logger.warning(f"Could not get delta for {opt['symbolName']}: {e}")

    return options_with_delta


def _select_by_strike_criteria(options_list, strike_criteria, market_data):
    """
    Select option based on strike criteria.

    Args:
        options_list (list): List of option instruments
        strike_criteria (dict): Strike selection criteria
        market_data: ExchangeMarketData adapter

    Returns:
        dict: Selected option instrument or None
    """
    criteria_type = strike_criteria['type']

    if criteria_type == 'closestStrike':
        target_strike = strike_criteria['value']
        if target_strike == 0:
            # 0 means "ATM" — use current spot price
            target_strike = market_data.get_index_price()
            logger.info(f"closestStrike: value=0 → using spot price ${target_strike:.0f} as ATM")
        return min(options_list, key=lambda x: abs(x['strike'] - target_strike))

    elif criteria_type == 'delta':
        target_delta = strike_criteria['value']
        return min(options_list, key=lambda x: abs(x.get('delta', 0) - target_delta))

    elif criteria_type == 'spotdistance %':
        spot_price = market_data.get_index_price()
        pct = strike_criteria['value'] / 100
        target_price = spot_price * (1 + pct)
        return min(options_list, key=lambda x: abs(x['strike'] - target_price))

    elif criteria_type == 'strike':
        # Exact strike match
        target_strike = strike_criteria['value']
        exact_matches = [opt for opt in options_list if float(opt.get('strike', 0)) == float(target_strike)]
        if not exact_matches:
            logger.error(f"No exact strike {target_strike} found in expiry options")
            return None
        return exact_matches[0]

    else:
        logger.error(f"Invalid strike criteria type: {criteria_type}")
        return None


# =============================================================================
# Compound Option Selection — find_option()
#
# Applies filters in strict order:
#   1. option_type  (C / P)
#   2. expiry       (day window → pick single expiry date)
#   3. strike       (ATM direction, bounds, distance %, OTM %)
#   4. delta        (fetch from API, filter by range)
#   5. rank         (pick one winner from survivors)
#
# Non-delta filters are applied BEFORE delta enrichment to minimise
# the number of API calls (max 10 per invocation).
# =============================================================================


def find_option(
    underlying: str = "BTC",
    option_type: str = "P",
    expiry: Optional[Dict] = None,
    strike: Optional[Dict] = None,
    delta: Optional[Dict] = None,
    rank_by: str = "delta_mid",
    market_data = None,
) -> Optional[Dict]:
    """
    Find the best option matching multiple simultaneous constraints.

    Applies filters in order: expiry → strike → delta, then ranks
    survivors to pick the single best match. All existing code is
    untouched — this is an additive feature.

    Args:
        underlying: Underlying symbol (default "BTC")
        option_type: "C" for call, "P" for put
        expiry: Expiry constraints dict. Keys (all optional):
            - min_days (int): Expiry >= N days from now
            - max_days (int): Expiry <= N days from now
            - target ("near"/"far"/"mid"): Prefer closest to min, max, or midpoint.
              Default: "near"
        strike: Strike constraints dict. Keys (all optional):
            - below_atm (bool): Strike < index price
            - above_atm (bool): Strike > index price
            - min_strike (float): Strike >= value
            - max_strike (float): Strike <= value
            - min_distance_pct (float): At least X% away from ATM
            - max_distance_pct (float): At most X% away from ATM
            - min_otm_pct (float): At least X% OTM (directional)
            - max_otm_pct (float): At most X% OTM (directional)
        delta: Delta constraints dict. Keys (all optional):
            - min (float): Delta > value (use negative for puts)
            - max (float): Delta < value
            - target (float): Prefer delta closest to this value
        rank_by: Ranking method for survivors:
            - "delta_mid": Closest to midpoint of delta min/max (default)
            - "delta_target": Closest to delta["target"]
            - "strike_atm": Closest strike to ATM
            - "strike_otm": Most OTM strike
            - "strike_itm": Most ITM strike

    Returns:
        Enriched option dict with symbolName, strike, delta,
        days_to_expiry, distance_pct, index_price, etc.
        None if no option satisfies all constraints.
    """
    try:
        expiry = expiry or {}
        strike = strike or {}
        delta = delta or {}

        # -- Fetch index price --
        index_price = market_data.get_index_price(underlying)
        if not index_price or index_price <= 0:
            logger.error("find_option: could not get index price")
            return None

        # -- Fetch instruments --
        instruments = market_data.get_option_instruments(underlying)
        if not instruments:
            logger.error("find_option: no instruments returned")
            return None

        logger.info(f"find_option: {len(instruments)} instruments, index=${index_price:,.0f}")

        # -- Step 1: Filter by option type --
        options = [o for o in instruments if o.get("symbolName", "").endswith("-" + option_type)]
        if not options:
            logger.error(f"find_option: no {option_type} options found")
            return None

        # -- Step 2: Filter by expiry --
        options = _find_filter_expiry(options, expiry)
        if not options:
            logger.error("find_option: no options after expiry filter")
            return None
        logger.info(f"find_option: {len(options)} options after expiry filter")

        # -- Step 3: Filter by strike --
        options = _find_filter_strike(options, strike, index_price, option_type)
        if not options:
            logger.error("find_option: no options after strike filter")
            return None
        logger.info(f"find_option: {len(options)} options after strike filter")

        # -- Step 4: Fetch deltas (smart budget) --
        needs_delta = bool(delta.get("min") is not None or delta.get("max") is not None
                          or delta.get("target") is not None
                          or rank_by in ("delta_mid", "delta_target"))

        if needs_delta:
            options = _find_enrich_deltas(options, market_data, index_price, max_calls=10)
            if not options:
                logger.error("find_option: no options with delta data")
                return None

        # -- Step 5: Filter by delta --
        if delta.get("min") is not None or delta.get("max") is not None:
            options = _find_filter_delta(options, delta)
            if not options:
                logger.error("find_option: no options after delta filter")
                return None
            logger.info(f"find_option: {len(options)} options after delta filter")

        # -- Step 6: Rank and pick winner --
        winner = _find_rank(options, delta, rank_by, index_price, option_type)
        if not winner:
            logger.error("find_option: ranking returned no winner")
            return None

        # -- Enrich the result --
        now_ms = time.time() * 1000
        winner["days_to_expiry"] = round((winner["expirationTimestamp"] - now_ms) / 86400_000, 1)
        winner["distance_pct"] = round(abs(float(winner["strike"]) - index_price) / index_price * 100, 2)
        winner["index_price"] = index_price

        logger.info(
            f"find_option: SELECTED {winner['symbolName']}  "
            f"strike={winner['strike']}  delta={winner.get('delta', 'N/A')}  "
            f"days={winner['days_to_expiry']}  dist={winner['distance_pct']}%"
        )
        return winner

    except Exception as e:
        logger.error(f"find_option: unexpected error: {e}", exc_info=True)
        return None


# -----------------------------------------------------------------------------
# find_option() internals — private helpers
# -----------------------------------------------------------------------------

def _find_filter_expiry(options: list, expiry: dict) -> list:
    """
    Filter options by expiry window and collapse to a single expiry date.

    Args:
        options: Pre-filtered option list (already limited to correct type).
        expiry: Dict with optional keys:
            min_days  — earliest acceptable expiry (default 0)
            max_days  — latest acceptable expiry (default ~10 years)
            target    — "near" (closest to min_days), "far" (closest to
                        max_days), or "mid" (closest to window midpoint).
                        Default: "near".

    Returns:
        Options at the single chosen expiry date, or [] if none in window.
    """
    now_ms = time.time() * 1000

    min_days = expiry.get("min_days", 0)
    max_days = expiry.get("max_days", 3650)  # ~10 years = no limit
    target = expiry.get("target", "near")

    min_ms = now_ms + min_days * 86400_000
    max_ms = now_ms + max_days * 86400_000

    # Keep options within the expiry window
    in_window = [o for o in options if min_ms <= o.get("expirationTimestamp", 0) <= max_ms]
    if not in_window:
        return []

    # Collect distinct expiry timestamps
    expiry_dates = sorted(set(o["expirationTimestamp"] for o in in_window))

    # Pick the target expiry date
    if target == "near":
        chosen_ts = expiry_dates[0]
    elif target == "far":
        chosen_ts = expiry_dates[-1]
    elif target == "mid":
        mid_ms = (min_ms + max_ms) / 2
        chosen_ts = min(expiry_dates, key=lambda ts: abs(ts - mid_ms))
    else:
        logger.warning(f"find_option: unknown expiry target '{target}', using 'near'")
        chosen_ts = expiry_dates[0]

    days_out = round((chosen_ts - now_ms) / 86400_000, 1)
    logger.info(f"find_option: chose expiry {days_out}d out ({len(expiry_dates)} expiries in window)")

    return [o for o in in_window if o["expirationTimestamp"] == chosen_ts]


def _find_filter_strike(options: list, strike: dict, index_price: float, option_type: str) -> list:
    """
    Filter options by strike constraints.

    Applies each supplied key as an independent filter (AND logic):
        below_atm / above_atm  — ATM direction
        min_strike / max_strike — absolute bounds
        min_distance_pct / max_distance_pct — abs % from ATM
        min_otm_pct / max_otm_pct — directional OTM %

    Args:
        options: Options at the chosen expiry.
        strike: Constraint dict (all keys optional).
        index_price: Current index/futures price.
        option_type: "C" or "P" (needed for OTM direction).

    Returns:
        Filtered list (may be empty).
    """
    result = list(options)

    # below_atm / above_atm
    if strike.get("below_atm"):
        result = [o for o in result if float(o["strike"]) < index_price]
    if strike.get("above_atm"):
        result = [o for o in result if float(o["strike"]) > index_price]

    # Absolute bounds
    if strike.get("min_strike") is not None:
        result = [o for o in result if float(o["strike"]) >= strike["min_strike"]]
    if strike.get("max_strike") is not None:
        result = [o for o in result if float(o["strike"]) <= strike["max_strike"]]

    # Distance from ATM (absolute %)
    if strike.get("min_distance_pct") is not None:
        min_dist = strike["min_distance_pct"] / 100
        result = [o for o in result if abs(float(o["strike"]) - index_price) / index_price >= min_dist]
    if strike.get("max_distance_pct") is not None:
        max_dist = strike["max_distance_pct"] / 100
        result = [o for o in result if abs(float(o["strike"]) - index_price) / index_price <= max_dist]

    # OTM % (directional)
    if strike.get("min_otm_pct") is not None or strike.get("max_otm_pct") is not None:
        filtered = []
        for o in result:
            otm_pct = _otm_pct(float(o["strike"]), index_price, option_type)
            if otm_pct < 0:
                continue  # ITM — exclude
            if strike.get("min_otm_pct") is not None and otm_pct < strike["min_otm_pct"]:
                continue
            if strike.get("max_otm_pct") is not None and otm_pct > strike["max_otm_pct"]:
                continue
            filtered.append(o)
        result = filtered

    return result


def _otm_pct(strike: float, index_price: float, option_type: str) -> float:
    """
    Calculate OTM percentage (directional).
    Puts: (index - strike) / index * 100  (positive = OTM)
    Calls: (strike - index) / index * 100  (positive = OTM)
    """
    if option_type == "P":
        return (index_price - strike) / index_price * 100
    else:
        return (strike - index_price) / index_price * 100


def _find_enrich_deltas(options: list, market_data, index_price: float, max_calls: int = 10) -> list:
    """
    Fetch deltas from the exchange for up to *max_calls* options.

    When more candidates survive than the budget allows, the options
    closest to ATM are prioritised — they are most likely to fall within
    a useful delta range and therefore most valuable to enrich.

    Each option dict is mutated in-place with a "delta" key.

    Returns:
        List of options that were successfully enriched.
    """
    # Sort by proximity to ATM so the budget covers the most useful strikes
    sorted_opts = sorted(options, key=lambda o: abs(float(o["strike"]) - index_price))
    to_fetch = sorted_opts[:max_calls]

    enriched = []
    for opt in to_fetch:
        try:
            details = market_data.get_option_details(opt["symbolName"])
            if details and "delta" in details:
                opt["delta"] = float(details["delta"])
                enriched.append(opt)
            else:
                logger.debug(f"find_option: no delta for {opt['symbolName']}")
        except Exception as e:
            logger.debug(f"find_option: delta fetch failed for {opt['symbolName']}: {e}")

    return enriched


def _find_filter_delta(options: list, delta: dict) -> list:
    """
    Keep only options whose delta falls strictly within (min, max).

    Options without a delta value are silently dropped.
    """
    result = []
    d_min = delta.get("min")
    d_max = delta.get("max")

    for o in options:
        d = o.get("delta")
        if d is None:
            continue
        if d_min is not None and d <= d_min:
            continue
        if d_max is not None and d >= d_max:
            continue
        result.append(o)
    return result


def _utc_day_start_ms() -> int:
    """Return millisecond timestamp for the start of today (00:00 UTC)."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _find_rank(options: list, delta: dict, rank_by: str, index_price: float, option_type: str):
    """
    Pick the single best option from the surviving candidates.

    Ranking strategies:
        delta_mid    — closest delta to midpoint of delta min/max
        delta_target — closest delta to delta["target"] (falls back to midpoint)
        strike_atm   — strike closest to index price
        strike_otm   — strike furthest from index price
        strike_itm   — strike closest to index price (same as strike_atm)
    """
    if not options:
        return None

    if rank_by == "delta_mid":
        d_min = delta.get("min", 0)
        d_max = delta.get("max", 0)
        midpoint = (d_min + d_max) / 2 if (d_min or d_max) else 0
        return min(options, key=lambda o: abs(o.get("delta", 0) - midpoint))

    elif rank_by == "delta_target":
        target = delta.get("target")
        if target is None:
            # Fall back to midpoint
            d_min = delta.get("min", 0)
            d_max = delta.get("max", 0)
            target = (d_min + d_max) / 2
        return min(options, key=lambda o: abs(o.get("delta", 0) - target))

    elif rank_by == "strike_atm":
        return min(options, key=lambda o: abs(float(o["strike"]) - index_price))

    elif rank_by == "strike_otm":
        return max(options, key=lambda o: abs(float(o["strike"]) - index_price))

    elif rank_by == "strike_itm":
        return min(options, key=lambda o: abs(float(o["strike"]) - index_price))

    else:
        logger.warning(f"find_option: unknown rank_by '{rank_by}', using delta_mid")
        d_min = delta.get("min", 0)
        d_max = delta.get("max", 0)
        midpoint = (d_min + d_max) / 2 if (d_min or d_max) else 0
        return min(options, key=lambda o: abs(o.get("delta", 0) - midpoint))


# =============================================================================
# Structure Templates — convenience builders that return List[LegSpec]
# =============================================================================

def straddle(
    qty: float,
    dte = "next",
    side: str = "buy",
    underlying: str = "BTC",
) -> List[LegSpec]:
    """
    ATM straddle — buy (or sell) a call and a put at the same ATM strike.

    Args:
        qty: Contract quantity per leg.
        dte: Days to expiry (0 = today / 0DTE).
        side: "buy" or "sell".
        underlying: Underlying asset.

    Returns:
        List of two LegSpec objects [ATM call, ATM put].
    """
    expiry = {"dte": dte}
    strike = {"type": "closestStrike", "value": 0}  # 0 → resolved to spot price
    return [
        LegSpec(
            option_type="C",
            side=side,
            qty=qty,
            strike_criteria=strike,
            expiry_criteria=expiry,
            underlying=underlying,
        ),
        LegSpec(
            option_type="P",
            side=side,
            qty=qty,
            strike_criteria=strike,
            expiry_criteria=expiry,
            underlying=underlying,
        ),
    ]


def straddle(
    qty: float,
    dte = "next",
    side: str = "buy",
    underlying: str = "BTC",
) -> List[LegSpec]:
    """
    ATM straddle — buy (or sell) an ATM call and an ATM put at the same strike.

    Both legs use closestStrike=0 (ATM) so they resolve to the strike
    nearest to the current spot price.

    Args:
        qty: Contract quantity per leg.
        dte: Days to expiry — "next" for nearest available, or int (0=0DTE, 1=1DTE, …).
        side: "buy" (long straddle) or "sell" (short straddle).
        underlying: Underlying asset.

    Returns:
        List of two LegSpec objects [ATM call, ATM put].
    """
    expiry = {"dte": dte}
    atm = {"type": "closestStrike", "value": 0}
    return [
        LegSpec(
            option_type="C",
            side=side,
            qty=qty,
            strike_criteria=atm,
            expiry_criteria=expiry,
            underlying=underlying,
        ),
        LegSpec(
            option_type="P",
            side=side,
            qty=qty,
            strike_criteria=atm,
            expiry_criteria=expiry,
            underlying=underlying,
        ),
    ]


def strangle(
    qty: float,
    call_delta: float = 0.25,
    put_delta: float = -0.25,
    dte = "next",
    side: str = "sell",
    underlying: str = "BTC",
) -> List[LegSpec]:
    """
    OTM strangle — sell (or buy) an OTM call and an OTM put.

    Args:
        qty: Contract quantity per leg.
        call_delta: Target delta for the call leg (positive).
        put_delta: Target delta for the put leg (negative).
        dte: Days to expiry (0 = today / 0DTE).
        side: "buy" or "sell".
        underlying: Underlying asset.

    Returns:
        List of two LegSpec objects [OTM call, OTM put].
    """
    expiry = {"dte": dte}
    return [
        LegSpec(
            option_type="C",
            side=side,
            qty=qty,
            strike_criteria={"type": "delta", "value": call_delta},
            expiry_criteria=expiry,
            underlying=underlying,
        ),
        LegSpec(
            option_type="P",
            side=side,
            qty=qty,
            strike_criteria={"type": "delta", "value": put_delta},
            expiry_criteria=expiry,
            underlying=underlying,
        ),
    ]