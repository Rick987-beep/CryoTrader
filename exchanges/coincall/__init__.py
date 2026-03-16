"""
Coincall Exchange Adapter Package

Thin wrappers around the existing Coincall-specific modules (auth.py,
market_data.py, trade_execution.py, account_manager.py, rfq.py).
These adapters implement the exchange abstraction interfaces and handle
side encoding translation (string ↔ int).
"""

from order_manager import OrderStatus

# Coincall exchange state codes → internal OrderStatus
COINCALL_STATE_MAP = {
    0: OrderStatus.LIVE,        # NEW
    1: OrderStatus.FILLED,      # FILLED
    2: OrderStatus.PARTIAL,     # PARTIALLY_FILLED
    3: OrderStatus.CANCELLED,   # CANCELED
    4: OrderStatus.CANCELLED,   # PRE_CANCEL
    5: OrderStatus.CANCELLED,   # CANCELING
    6: OrderStatus.REJECTED,    # INVALID
    10: OrderStatus.EXPIRED,    # CANCEL_BY_EXERCISE
}


def build_coincall() -> dict:
    """Construct all Coincall adapter instances."""
    from exchanges.coincall.auth import CoincallAuthAdapter
    from exchanges.coincall.market_data import CoincallMarketDataAdapter
    from exchanges.coincall.executor import CoincallExecutorAdapter
    from exchanges.coincall.account import CoincallAccountAdapter
    from exchanges.coincall.rfq import CoincallRFQAdapter

    return {
        "auth": CoincallAuthAdapter(),
        "market_data": CoincallMarketDataAdapter(),
        "executor": CoincallExecutorAdapter(),
        "account_manager": CoincallAccountAdapter(),
        "rfq_executor": CoincallRFQAdapter(),
        "state_map": COINCALL_STATE_MAP,
    }
