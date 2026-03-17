"""
Deribit Executor Adapter

Implements ExchangeExecutor for Deribit via REST JSON-RPC.

Key differences from Coincall:
  - Separate /private/buy and /private/sell endpoints (no side parameter)
  - String order states ("open", "filled", "cancelled", "rejected")
  - Prices are in BTC (the adapter accepts BTC prices for order placement)
  - Variable tick sizes: price < 0.005 BTC → tick 0.0001, ≥ 0.005 → tick 0.0005
  - Min order size: 0.1 contracts
  - `label` field replaces `clientOrderId` (max 64 chars)
  - `order_id` does NOT change on edit (replaced=true)

Response normalization for order_manager:
  get_order_status() returns a dict with keys: state, fillQty, avgPrice, orderId
  matching what the order_manager already reads.
"""

import logging
import math
from typing import Optional

from exchanges.base import ExchangeExecutor

logger = logging.getLogger(__name__)


def _snap_to_tick(price: float) -> float:
    """
    Snap a BTC price to the valid Deribit tick size.

    Rules (from tick_size_steps):
      price < 0.005 BTC  → tick = 0.0001
      price >= 0.005 BTC → tick = 0.0005
    """
    if price <= 0:
        return price
    tick = 0.0005 if price >= 0.005 else 0.0001
    return round(math.floor(price / tick) * tick, 4)


class DeribitExecutorAdapter(ExchangeExecutor):
    """Order placement, cancellation, and status for Deribit."""

    def __init__(self, auth):
        self._auth = auth

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
        """
        Place an order on Deribit.

        Args:
            symbol: Deribit instrument name (e.g. "BTC-28MAR26-100000-C")
            qty: Order size in contracts (min 0.1)
            side: "buy" or "sell" (string)
            order_type: 1=limit (default), 2=market (Coincall convention kept for compat)
            price: Limit price in BTC (required for limit orders)
            client_order_id: Maps to Deribit `label` (max 64 chars)
            reduce_only: Whether this is a reduce-only order

        Returns:
            Dict with orderId on success, None on failure.
        """
        # Deribit uses separate endpoints per side
        method = "private/buy" if side == "buy" else "private/sell"

        params = {
            "instrument_name": symbol,
            "amount": qty,
            "type": "market" if order_type == 2 else "limit",
        }

        if order_type != 2 and price is not None:
            params["price"] = _snap_to_tick(price)

        if client_order_id:
            params["label"] = client_order_id[:64]

        if reduce_only:
            params["reduce_only"] = True

        resp = self._auth.call(method, params)

        if not self._auth.is_successful(resp):
            error = resp.get("error", {})
            logger.error(
                f"Deribit place_order failed: {error.get('message', 'unknown')} "
                f"(code={error.get('code')}, symbol={symbol}, side={side}, "
                f"qty={qty}, price={price})"
            )
            return None

        result = resp["result"]
        order = result.get("order", {})

        logger.info(
            f"Deribit order placed: {order.get('order_id')} "
            f"{side} {qty} {symbol} @ {order.get('price')} BTC "
            f"state={order.get('order_state')}"
        )

        return {
            "orderId": str(order.get("order_id", "")),
            "clientOrderId": order.get("label", ""),
            "state": order.get("order_state", ""),
            "fillQty": float(order.get("filled_amount", 0)),
            "avgPrice": float(order.get("average_price", 0)),
            "symbol": order.get("instrument_name", symbol),
            "side": side,
            "qty": float(order.get("amount", qty)),
            "price": float(order.get("price", 0)),
            # Immediate fills (if order crossed the spread)
            "_trades": result.get("trades", []),
        }

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by Deribit order_id."""
        resp = self._auth.call("private/cancel", {"order_id": order_id})
        if self._auth.is_successful(resp):
            logger.info(f"Deribit order cancelled: {order_id}")
            return True
        error = resp.get("error", {})
        logger.warning(
            f"Deribit cancel failed for {order_id}: "
            f"{error.get('message', 'unknown')} (code={error.get('code')})"
        )
        return False

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """
        Query current order status.

        Returns normalized dict with keys the order_manager expects:
          state, fillQty, avgPrice, orderId
        """
        resp = self._auth.call("private/get_order_state", {"order_id": order_id})
        if not self._auth.is_successful(resp):
            logger.debug(f"Deribit get_order_state failed for {order_id}: {resp.get('error')}")
            return None

        o = resp["result"]
        return {
            "orderId": str(o.get("order_id", "")),
            "state": o.get("order_state", ""),
            "fillQty": float(o.get("filled_amount", 0)),
            "avgPrice": float(o.get("average_price", 0)),
            "symbol": o.get("instrument_name", ""),
            "side": o.get("direction", ""),
            "clientOrderId": o.get("label", ""),
            "price": float(o.get("price", 0)),
            "qty": float(o.get("amount", 0)),
            "_replaced": o.get("replaced", False),
            "_order_type": o.get("order_type", ""),
            "_cancel_reason": o.get("cancel_reason", ""),
        }
