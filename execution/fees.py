"""
execution/fees.py — Fee extraction helpers.

Parse exchange responses into Price fee objects.  No I/O.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from execution.currency import Currency, Price


def extract_fee(
    trades: List[Dict[str, Any]],
    currency: Currency,
) -> Optional[Price]:
    """Extract and sum fees from the _trades array in an executor response.

    Deribit returns per-fill fee data in the trades array:
        [{"fee": 0.0003, "fee_currency": "BTC", ...}, ...]

    Returns None if no trades or all fees are zero/missing.
    """
    if not trades:
        return None

    total = 0.0
    for t in trades:
        fee = t.get("fee", 0)
        if fee:
            total += abs(float(fee))  # abs(): Deribit maker rebates are negative; we track gross fees

    if total == 0.0:
        return None
    return Price(total, currency)


def sum_fees(fees: List[Optional[Price]]) -> Optional[Price]:
    """Sum a list of Optional[Price] fees.  Returns None if all are None."""
    total: Optional[Price] = None
    for fee in fees:
        if fee is not None:
            if total is None:
                total = fee
            else:
                total = total + fee  # raises DenominationError if mixed
    return total
