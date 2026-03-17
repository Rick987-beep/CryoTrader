"""
Exchange Provider Factory

Constructs the correct set of exchange adapters based on the EXCHANGE
config setting.  Default is 'coincall' for backward compatibility.

Usage:
    from exchanges import build_exchange

    components = build_exchange()
    # components['auth'], components['executor'], etc.
"""

from exchanges.base import (
    ExchangeAuth,
    ExchangeMarketData,
    ExchangeExecutor,
    ExchangeAccountManager,
    ExchangeRFQExecutor,
)


def build_exchange(exchange_name: str = None) -> dict:
    """
    Create exchange adapter instances for the given exchange.

    Args:
        exchange_name: 'coincall' or 'deribit'. If None, reads from config.EXCHANGE.

    Returns:
        Dict with keys: auth, market_data, executor, account_manager,
        rfq_executor, state_map.
    """
    if exchange_name is None:
        from config import EXCHANGE
        exchange_name = EXCHANGE

    if exchange_name == "coincall":
        from exchanges.coincall import build_coincall
        return build_coincall()

    elif exchange_name == "deribit":
        from exchanges.deribit import build_deribit
        return build_deribit()

    else:
        raise ValueError(
            f"Unknown exchange: '{exchange_name}'. "
            f"Supported: 'coincall', 'deribit' (Phase 2)."
        )
