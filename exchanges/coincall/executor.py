"""Coincall executor adapter — wraps existing TradeExecutor.

Translates string side ('buy'/'sell') to Coincall int side (1/2)
at the API boundary.
"""

from exchanges.base import ExchangeExecutor
from trade_execution import TradeExecutor


def _side_to_int(side: str) -> int:
    """Convert normalized string side to Coincall int encoding."""
    return 1 if side == "buy" else 2


class CoincallExecutorAdapter(ExchangeExecutor):
    """Wraps TradeExecutor, translating string sides to int."""

    def __init__(self):
        self._inner = TradeExecutor()

    def place_order(self, symbol, qty, side, order_type=1, price=None,
                    client_order_id=None, reduce_only=False):
        return self._inner.place_order(
            symbol=symbol,
            qty=qty,
            side=_side_to_int(side),
            order_type=order_type,
            price=price,
            client_order_id=client_order_id,
            reduce_only=reduce_only,
        )

    def cancel_order(self, order_id):
        return self._inner.cancel_order(order_id)

    def get_order_status(self, order_id):
        return self._inner.get_order_status(order_id)
