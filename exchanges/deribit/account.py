"""
Deribit Account Adapter

Implements ExchangeAccountManager for Deribit.
Normalizes account/position/order responses to Coincall-compatible field names.

Key differences handled:
  - Deribit reports values in BTC; we use total_equity_usd for USD values
  - Position size is unsigned + direction field → normalized to signed qty
  - Position Greeks are TOTAL (not per-contract) — kept as-is since
    AccountSnapshot uses total Greeks for portfolio-level aggregation
  - Order states are strings ("open", "filled", ...) not integers
"""

import logging
import time
from typing import Any, Dict, List, Optional

from exchanges.base import ExchangeAccountManager

logger = logging.getLogger(__name__)


class DeribitAccountAdapter(ExchangeAccountManager):
    """Deribit account queries with Coincall-compatible response shapes."""

    def __init__(self, auth):
        self._auth = auth

    def get_account_info(self, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        Get account summary.

        Returns dict normalized to Coincall field names:
          equity, available_margin, initial_margin, maintenance_margin,
          unrealized_pnl, timestamp, etc.
        """
        resp = self._auth.call("private/get_account_summary", {"currency": "BTC"})
        if not self._auth.is_successful(resp):
            logger.warning(f"Deribit get_account_summary failed: {resp.get('error')}")
            return None

        s = resp["result"]

        return {
            # USD-denominated values (preferred for cross-exchange compatibility)
            "equity": float(s.get("total_equity_usd", 0)),
            "available_margin": float(s.get("total_equity_usd", 0))
                - float(s.get("total_initial_margin_usd", 0)),
            "initial_margin": float(s.get("total_initial_margin_usd", 0)),
            "maintenance_margin": float(s.get("total_maintenance_margin_usd", 0)),
            "unrealized_pnl": float(s.get("session_upl", 0)),
            "margin_ratio_initial": 0.0,   # Deribit doesn't expose ratio directly
            "margin_ratio_maintenance": 0.0,
            "timestamp": time.time(),
            # BTC-denominated values (for reference)
            "_equity_btc": float(s.get("equity", 0)),
            "_balance_btc": float(s.get("balance", 0)),
            "_available_funds_btc": float(s.get("available_funds", 0)),
            "_initial_margin_btc": float(s.get("initial_margin", 0)),
            "_maintenance_margin_btc": float(s.get("maintenance_margin", 0)),
            # Portfolio Greeks
            "_delta_total": float(s.get("delta_total", 0)),
            "_options_gamma": float(s.get("options_gamma", 0)),
            "_options_vega": float(s.get("options_vega", 0)),
            "_options_theta": float(s.get("options_theta", 0)),
            "_margin_model": s.get("margin_model", ""),
        }

    def get_positions(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get open option positions.

        Returns list of dicts normalized to Coincall field names:
          position_id (= instrument_name), symbol, qty (signed), trade_side,
          avg_price, mark_price, unrealized_pnl, delta, gamma, theta, vega, etc.
        """
        resp = self._auth.call("private/get_positions", {
            "currency": "BTC",
            "kind": "option",
        })
        if not self._auth.is_successful(resp):
            logger.warning(f"Deribit get_positions failed: {resp.get('error')}")
            return []

        positions = resp["result"]
        result = []
        for p in positions:
            size = float(p.get("size", 0))
            direction = p.get("direction", "zero")

            # Filter out closed positions (size=0, direction="zero")
            if size == 0 or direction == "zero":
                continue

            # Normalize: signed qty (positive=long, negative=short)
            qty = size if direction == "buy" else -size
            trade_side = 1 if direction == "buy" else 2
            index_price = float(p.get("index_price", 0))

            result.append({
                "position_id": p.get("instrument_name", ""),
                "symbol": p.get("instrument_name", ""),
                "display_name": p.get("instrument_name", ""),
                "qty": qty,
                "trade_side": trade_side,
                "avg_price": float(p.get("average_price_usd", 0)),
                "mark_price": float(p.get("mark_price", 0)) * index_price,
                "index_price": index_price,
                "unrealized_pnl": float(p.get("floating_profit_loss_usd", 0)),
                "roi": 0.0,  # Deribit doesn't provide ROI directly
                # Greeks — Deribit reports TOTAL, not per-contract
                "delta": float(p.get("delta", 0)),
                "gamma": float(p.get("gamma", 0)),
                "theta": float(p.get("theta", 0)),
                "vega": float(p.get("vega", 0)),
                # BTC-native values
                "_avg_price_btc": float(p.get("average_price", 0)),
                "_mark_price_btc": float(p.get("mark_price", 0)),
                "_floating_pnl_btc": float(p.get("floating_profit_loss", 0)),
                "_direction": direction,
                "_size_unsigned": size,
            })

        return result

    def get_open_orders(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Get currently open orders.

        Returns list of dicts normalized to Coincall field names:
          order_id, client_order_id, symbol, qty, filled_qty, remaining_qty,
          price, avg_price, trade_side, state, etc.
        """
        resp = self._auth.call("private/get_open_orders_by_currency", {
            "currency": "BTC",
            "kind": "option",
        })
        if not self._auth.is_successful(resp):
            logger.warning(f"Deribit get_open_orders failed: {resp.get('error')}")
            return []

        orders = resp["result"]
        result = []
        for o in orders:
            direction = o.get("direction", "buy")
            trade_side = 1 if direction == "buy" else 2

            result.append({
                "order_id": str(o.get("order_id", "")),
                "client_order_id": o.get("label", ""),
                "symbol": o.get("instrument_name", ""),
                "display_name": o.get("instrument_name", ""),
                "qty": float(o.get("amount", 0)),
                "remaining_qty": float(o.get("amount", 0)) - float(o.get("filled_amount", 0)),
                "filled_qty": float(o.get("filled_amount", 0)),
                "price": float(o.get("price", 0)),
                "avg_price": float(o.get("average_price", 0)),
                "trade_side": trade_side,
                "state": o.get("order_state", ""),
                "create_time": o.get("creation_timestamp", 0),
                "update_time": o.get("last_update_timestamp", 0),
            })

        return result
