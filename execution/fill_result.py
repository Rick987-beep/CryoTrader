"""
execution/fill_result.py — Structured fill result types.

Replaces string-based fill returns ("filled", "requoted", "failed", "pending")
with typed dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from execution.currency import Price


class FillStatus(Enum):
    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    REQUOTED = "requoted"
    FAILED = "failed"
    REFUSED = "refused"


@dataclass
class LegFillSnapshot:
    """Per-leg fill state at a point in time."""
    symbol: str
    side: str
    qty: float
    filled_qty: float
    fill_price: Optional[Price]
    order_id: Optional[str]
    skipped: bool = False
    skip_reason: Optional[str] = None
    fee: Optional[Price] = None


@dataclass
class FillResult:
    """Structured result from FillManager.place_all() or .check().

    INVARIANT: all legs in a single FillResult are always on the same exchange,
    so all fill prices and fees share a single denomination (BTC for Deribit,
    USD for Coincall). Cross-exchange trades are not supported.
    """
    status: FillStatus
    legs: List[LegFillSnapshot]
    phase_index: int
    phase_total: int
    phase_pricing: str
    elapsed_seconds: float
    error: Optional[str] = None
    total_fees: Optional[Price] = None

    @property
    def all_filled(self) -> bool:
        return all(l.filled_qty >= l.qty for l in self.legs if not l.skipped)

    @property
    def has_skipped(self) -> bool:
        return any(l.skipped for l in self.legs)

    @property
    def skipped_symbols(self) -> List[str]:
        return [l.symbol for l in self.legs if l.skipped]
