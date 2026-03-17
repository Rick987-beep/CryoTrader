"""Coincall authentication adapter — wraps existing CoincallAuth."""

from exchanges.base import ExchangeAuth
from auth import CoincallAuth
from config import API_KEY, API_SECRET, BASE_URL


class CoincallAuthAdapter(ExchangeAuth):
    """Thin wrapper around CoincallAuth implementing ExchangeAuth interface."""

    def __init__(self):
        self._inner = CoincallAuth(API_KEY, API_SECRET, BASE_URL)
        self.base_url = self._inner.base_url

    def get(self, endpoint, **kwargs):
        return self._inner.get(endpoint, **kwargs)

    def post(self, endpoint, data=None, **kwargs):
        return self._inner.post(endpoint, data, **kwargs)

    def is_successful(self, response):
        return self._inner.is_successful(response)
