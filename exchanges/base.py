"""
Exchange Abstraction Layer — Base Interfaces

Abstract base classes defining the contract between the trading system
and exchange-specific implementations. The rest of the system depends
only on these interfaces, never on concrete exchange implementations.

See docs/MIGRATION_PLAN_DERIBIT.md § 5 for full design rationale.
"""

from abc import ABC, abstractmethod
from typing import Any, Optional


class ExchangeAuth(ABC):
    """Authenticated HTTP client for an exchange."""

    @abstractmethod
    def get(self, endpoint: str, **kwargs) -> dict:
        """Send authenticated GET request, return parsed JSON."""
        ...

    @abstractmethod
    def post(self, endpoint: str, data: Any = None, **kwargs) -> dict:
        """Send authenticated POST request, return parsed JSON."""
        ...

    @abstractmethod
    def is_successful(self, response: dict) -> bool:
        """Check if exchange response indicates success."""
        ...


class ExchangeMarketData(ABC):
    """Read-only market data queries."""

    @abstractmethod
    def get_index_price(self, underlying: str = "BTC") -> Optional[float]:
        """Get current index/spot price for the underlying."""
        ...

    @abstractmethod
    def get_option_instruments(self, underlying: str = "BTC") -> list:
        """Get all available option instruments for the underlying."""
        ...

    @abstractmethod
    def get_option_details(self, symbol: str) -> Optional[dict]:
        """Get detailed info for a specific option (mark price, Greeks, etc)."""
        ...

    @abstractmethod
    def get_option_orderbook(self, symbol: str) -> Optional[dict]:
        """Get orderbook for a specific option."""
        ...


class ExchangeExecutor(ABC):
    """Order lifecycle operations.  Side is always 'buy' or 'sell' (string)."""

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        qty: float,
        side: str,
        order_type: int = 1,
        price: float = None,
        client_order_id: str = None,
        reduce_only: bool = False,
    ) -> Optional[dict]:
        """Place an order. Returns dict with orderId on success, None on failure."""
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by exchange order ID."""
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> Optional[dict]:
        """Query current order status. Returns exchange-specific status dict."""
        ...


class ExchangeAccountManager(ABC):
    """Account and position queries."""

    @abstractmethod
    def get_account_info(self) -> Optional[dict]:
        """Get account summary (equity, margins, PnL, etc)."""
        ...

    @abstractmethod
    def get_positions(self, force_refresh: bool = False) -> list:
        """Get open positions."""
        ...

    @abstractmethod
    def get_open_orders(self, force_refresh: bool = False) -> list:
        """Get currently open orders on the exchange."""
        ...


class ExchangeRFQExecutor(ABC):
    """RFQ / block trade execution."""

    @abstractmethod
    def execute(
        self,
        legs,
        action: str = "buy",
        timeout_seconds: int = 60,
        min_improvement_pct: float = -999.0,
        poll_interval_seconds: int = 3,
    ) -> Any:
        """Execute a complete RFQ workflow (create → poll → accept/cancel)."""
        ...

    @abstractmethod
    def execute_phased(self, legs, action: str = "buy", **kwargs) -> Any:
        """Execute RFQ with phased pricing (initial wait → gated → relaxed)."""
        ...

    @abstractmethod
    def get_orderbook_cost(self, legs, action: str = "buy") -> Optional[float]:
        """Calculate what the structure would cost on the orderbook."""
        ...
