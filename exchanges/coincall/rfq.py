"""Coincall RFQ adapter — wraps existing RFQExecutor."""

from exchanges.base import ExchangeRFQExecutor
from rfq import RFQExecutor


class CoincallRFQAdapter(ExchangeRFQExecutor):
    """Thin wrapper around RFQExecutor implementing ExchangeRFQExecutor."""

    def __init__(self):
        self._inner = RFQExecutor()

    def execute(self, legs, action="buy", timeout_seconds=60,
                min_improvement_pct=-999.0, poll_interval_seconds=3):
        return self._inner.execute(
            legs, action, timeout_seconds,
            min_improvement_pct, poll_interval_seconds,
        )

    def execute_phased(self, legs, action="buy", **kwargs):
        return self._inner.execute_phased(legs, action, **kwargs)

    def get_orderbook_cost(self, legs, action="buy"):
        return self._inner.get_orderbook_cost(legs, action)
