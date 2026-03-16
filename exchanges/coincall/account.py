"""Coincall account adapter — wraps existing AccountManager."""

from exchanges.base import ExchangeAccountManager
from account_manager import AccountManager


class CoincallAccountAdapter(ExchangeAccountManager):
    """Thin wrapper around AccountManager implementing ExchangeAccountManager."""

    def __init__(self):
        self._inner = AccountManager()

    def get_account_info(self):
        return self._inner.get_account_info()

    def get_positions(self, force_refresh=False):
        return self._inner.get_positions(force_refresh=force_refresh)

    def get_open_orders(self, force_refresh=False):
        return self._inner.get_open_orders(force_refresh=force_refresh)
