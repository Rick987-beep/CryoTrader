"""
execution/currency.py — Type-safe price and denomination primitives.

Currency enum, Price value object, and OrderbookSnapshot dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class Currency(Enum):
    """Denomination of a price value."""
    BTC = "BTC"
    USD = "USD"
    ETH = "ETH"


class DenominationError(Exception):
    """Raised when an operation mixes incompatible denominations."""


@dataclass(frozen=True)
class Price:
    """A price value with its denomination.  Immutable."""
    amount: float
    currency: Currency

    # ── Conversion ───────────────────────────────────────────────────────

    def to_usd(self, index_price: float) -> "Price":
        """Convert to USD denomination (no-op if already USD)."""
        if self.currency == Currency.USD:
            return self
        if self.currency == Currency.BTC:
            return Price(self.amount * index_price, Currency.USD)
        if self.currency == Currency.ETH:
            return Price(self.amount * index_price, Currency.USD)
        raise DenominationError(f"Cannot convert {self.currency} to USD")

    def to_btc(self, index_price: float) -> "Price":
        """Convert to BTC denomination (no-op if already BTC)."""
        if self.currency == Currency.BTC:
            return self
        if self.currency == Currency.USD:
            if index_price <= 0:
                raise DenominationError("index_price must be > 0 for USD→BTC")
            return Price(self.amount / index_price, Currency.BTC)
        raise DenominationError(f"Cannot convert {self.currency} to BTC")

    # ── Arithmetic (same-currency only) ──────────────────────────────────

    def __add__(self, other: "Price") -> "Price":
        if not isinstance(other, Price):
            return NotImplemented
        if self.currency != other.currency:
            raise DenominationError(
                f"Cannot add {self.currency.value} + {other.currency.value}"
            )
        return Price(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Price") -> "Price":
        if not isinstance(other, Price):
            return NotImplemented
        if self.currency != other.currency:
            raise DenominationError(
                f"Cannot subtract {self.currency.value} - {other.currency.value}"
            )
        return Price(self.amount - other.amount, self.currency)

    def __neg__(self) -> "Price":
        return Price(-self.amount, self.currency)

    def __mul__(self, scalar: float) -> "Price":
        if isinstance(scalar, Price):
            raise DenominationError("Cannot multiply Price × Price")
        return Price(self.amount * scalar, self.currency)

    def __rmul__(self, scalar: float) -> "Price":
        return self.__mul__(scalar)

    # ── Comparison (same-currency only) ──────────────────────────────────

    def _check_comparable(self, other: "Price") -> None:
        if not isinstance(other, Price):
            raise TypeError(f"Cannot compare Price with {type(other)}")
        if self.currency != other.currency:
            raise DenominationError(
                f"Cannot compare {self.currency.value} with {other.currency.value}"
            )

    def __lt__(self, other: "Price") -> bool:
        self._check_comparable(other)
        return self.amount < other.amount

    def __le__(self, other: "Price") -> bool:
        self._check_comparable(other)
        return self.amount <= other.amount

    def __gt__(self, other: "Price") -> bool:
        self._check_comparable(other)
        return self.amount > other.amount

    def __ge__(self, other: "Price") -> bool:
        self._check_comparable(other)
        return self.amount >= other.amount

    # ── Convenience ──────────────────────────────────────────────────────

    def __float__(self) -> float:
        """Raw numeric value — use only when denomination is already verified."""
        return self.amount

    def __format__(self, format_spec: str) -> str:
        """Support f-string formatting (e.g. f'{price:.4f}')."""
        if format_spec:
            return format(self.amount, format_spec)
        return repr(self)

    def __repr__(self) -> str:
        return f"Price({self.amount}, {self.currency.value})"

    def to_dict(self) -> dict:
        """Serialize for JSON / persistence."""
        return {"amount": self.amount, "currency": self.currency.value}

    @classmethod
    def from_dict(cls, d: dict) -> "Price":
        """Deserialize from JSON / persistence."""
        return cls(amount=d["amount"], currency=Currency(d["currency"]))


@dataclass
class OrderbookSnapshot:
    """Typed snapshot of an orderbook at a point in time."""
    symbol: str
    currency: Currency
    best_bid: Optional[float]
    best_ask: Optional[float]
    mark: Optional[float]          # native denomination (BTC for Deribit, USD for Coincall)
    index_price: Optional[float]   # always USD
    timestamp: float
