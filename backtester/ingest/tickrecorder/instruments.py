#!/usr/bin/env python3
"""
instruments.py — Deribit BTC Option Instrument Discovery

Fetches the live instrument list from Deribit REST API and maintains a
set of active (expiry, strike, is_call) keys. Callers register a callback
that fires with (new_instruments, expired_instruments) on each refresh,
so ws_client can subscribe/unsubscribe accordingly.
"""
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Dict, Optional, Set, Tuple

import requests

logger = logging.getLogger(__name__)

# REST endpoint — unauthenticated, no rate limit concern at 1 call/30 min
_INSTRUMENTS_URL = (
    "https://www.deribit.com/api/v2/public/get_instruments"
    "?currency=BTC&kind=option&expired=false"
)

# Instrument key type: (expiry_str, strike, is_call)
InstrumentKey = Tuple[str, float, bool]


@dataclass
class InstrumentMeta:
    """Metadata for one option instrument."""
    instrument_name: str    # e.g. "BTC-28MAR26-80000-C"
    expiry: str             # e.g. "28MAR26"
    strike: float
    is_call: bool


def _parse_instrument_name(name):
    # type: (str) -> Optional[InstrumentMeta]
    """Parse a Deribit option instrument name.

    Format: BTC-{DDMMMYY}-{STRIKE}-{C|P}
    Example: BTC-28MAR26-80000-C
    """
    parts = name.split("-")
    if len(parts) != 4:
        return None
    _, expiry, strike_str, cp = parts
    if cp not in ("C", "P"):
        return None
    try:
        strike = float(strike_str)
    except ValueError:
        return None
    return InstrumentMeta(
        instrument_name=name,
        expiry=expiry,
        strike=strike,
        is_call=(cp == "C"),
    )


def _fetch_instruments():
    # type: () -> Dict[InstrumentKey, InstrumentMeta]
    """Fetch all active BTC option instruments from Deribit REST.

    Returns a dict keyed by (expiry, strike, is_call).
    Raises requests.RequestException on network failure.
    """
    resp = requests.get(_INSTRUMENTS_URL, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    result = {}  # type: Dict[InstrumentKey, InstrumentMeta]
    for item in data.get("result", []):
        name = item.get("instrument_name", "")
        meta = _parse_instrument_name(name)
        if meta is None:
            continue
        key = (meta.expiry, meta.strike, meta.is_call)
        result[key] = meta

    logger.debug("Fetched %d BTC option instruments from Deribit", len(result))
    return result


class InstrumentTracker:
    """Tracks active BTC option instruments and notifies on changes.

    Usage:
        tracker = InstrumentTracker()
        tracker.on_change(my_callback)   # callback(new, expired)
        tracker.refresh()                # initial load
        # ... call tracker.refresh() every 30 min ...
    """

    def __init__(self):
        self._active = {}       # type: Dict[InstrumentKey, InstrumentMeta]
        self._callback = None   # type: Optional[Callable]
        self._last_refresh = None  # type: Optional[datetime]

    def on_change(self, callback):
        # type: (Callable[[Dict[InstrumentKey, InstrumentMeta], Set[InstrumentKey]], None]) -> None
        """Register callback fired with (new_instruments_dict, expired_keys_set)."""
        self._callback = callback

    @property
    def active(self):
        # type: () -> Dict[InstrumentKey, InstrumentMeta]
        """Current active instrument dict (read-only view)."""
        return self._active

    @property
    def instrument_names(self):
        # type: () -> list
        """Sorted list of Deribit instrument name strings for all active instruments."""
        return sorted(m.instrument_name for m in self._active.values())

    def refresh(self):
        # type: () -> bool
        """Fetch latest instrument list and fire callback if anything changed.

        Returns True on success, False if the fetch failed (previous state
        is preserved — no instruments dropped on transient network error).
        """
        try:
            fresh = _fetch_instruments()
        except Exception as exc:
            logger.warning("Instrument refresh failed: %s", exc)
            return False

        old_keys = set(self._active.keys())
        new_keys = set(fresh.keys())

        added = {k: fresh[k] for k in new_keys - old_keys}
        expired = old_keys - new_keys

        if added or expired:
            logger.info(
                "Instruments changed: +%d new, -%d expired (total %d)",
                len(added), len(expired), len(fresh),
            )
            self._active = fresh
            if self._callback is not None:
                try:
                    self._callback(added, expired)
                except Exception:
                    logger.exception("Instrument change callback raised")
        else:
            self._active = fresh
            logger.debug("Instrument refresh: no changes (%d instruments)", len(fresh))

        self._last_refresh = datetime.now(timezone.utc)
        return True
