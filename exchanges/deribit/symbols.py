"""
Deribit Symbol Parser / Builder

Converts between Deribit instrument names and structured components.

Deribit format: {UNDERLYING}-{D}[D]{MMM}{YY}-{STRIKE}-{C|P}
  e.g.  BTC-3APR26-74000-C   or   BTC-28MAR26-100000-P

Coincall format: {UNDERLYING}USD-{DD}{MMM}{YY}-{STRIKE}-{C|P}
  e.g.  BTCUSD-03APR26-74000-C
"""

import re
from typing import Optional, Dict

# Regex: 1-2 digit day, 3-letter month, 2-digit year
_DERIBIT_RE = re.compile(
    r"^([A-Z]+)-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$"
)

_COINCALL_RE = re.compile(
    r"^([A-Z]+)USD-(\d{2})([A-Z]{3})(\d{2})-(\d+)-([CP])$"
)


def parse_deribit_symbol(symbol: str) -> Optional[Dict[str, str]]:
    """
    Parse a Deribit instrument name into components.

    Returns dict with keys: underlying, day, month, year, strike, option_type
    or None if the symbol doesn't match the expected format.
    """
    m = _DERIBIT_RE.match(symbol)
    if not m:
        return None
    return {
        "underlying": m.group(1),
        "day": m.group(2),        # "3" or "28" (no zero-pad)
        "month": m.group(3),      # "APR", "MAR"
        "year": m.group(4),       # "26"
        "strike": m.group(5),     # "74000"
        "option_type": m.group(6),  # "C" or "P"
    }


def build_deribit_symbol(
    underlying: str, day: str, month: str, year: str,
    strike: str, option_type: str,
) -> str:
    """Build a Deribit instrument name from components."""
    # Deribit uses unpadded day (strip leading zero)
    d = str(int(day))
    return f"{underlying}-{d}{month}{year}-{strike}-{option_type}"


def coincall_to_deribit(symbol: str) -> Optional[str]:
    """
    Convert a Coincall symbol to a Deribit symbol.

    BTCUSD-03APR26-74000-C  →  BTC-3APR26-74000-C
    """
    m = _COINCALL_RE.match(symbol)
    if not m:
        return None
    underlying = m.group(1)  # "BTC" (strip "USD" suffix)
    day = str(int(m.group(2)))  # "03" → "3"
    month = m.group(3)
    year = m.group(4)
    strike = m.group(5)
    opt = m.group(6)
    return f"{underlying}-{day}{month}{year}-{strike}-{opt}"


def deribit_to_coincall(symbol: str) -> Optional[str]:
    """
    Convert a Deribit symbol to a Coincall symbol.

    BTC-3APR26-74000-C  →  BTCUSD-03APR26-74000-C
    """
    parts = parse_deribit_symbol(symbol)
    if not parts:
        return None
    day = parts["day"].zfill(2)  # "3" → "03"
    return (
        f"{parts['underlying']}USD-{day}{parts['month']}{parts['year']}"
        f"-{parts['strike']}-{parts['option_type']}"
    )
