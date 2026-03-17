"""
Deribit Block RFQ Adapter

Implements ExchangeRFQExecutor for Deribit's block trade RFQ system.

Key differences from Coincall:
  - 25 BTC minimum (vs Coincall $50k) — most current strategies won't qualify
  - Blind auction: taker sees aggregated best bid/ask after 5s delay
  - Ratio-based pricing: legs expressed as GCD-reduced ratios
  - Multi-maker fills: one RFQ can produce multiple block trades
  - Trigger orders: good_til_cancelled keeps crossing order open

The execute() and execute_phased() methods follow the same RFQResult contract
as the Coincall adapter, so the rest of the system can use them interchangeably.
"""

import logging
import math
import time
from typing import Any, List, Optional

from exchanges.base import ExchangeRFQExecutor

logger = logging.getLogger(__name__)

# Deribit requires 25 BTC minimum for block RFQ
BLOCK_RFQ_MIN_CONTRACTS = 25.0


def _compute_gcd(values):
    """Compute GCD of a list of floats (rounded to 1 decimal for contracts)."""
    def gcd_pair(a, b):
        # Work in tenths to handle 0.1 increments
        a_int = int(round(a * 10))
        b_int = int(round(b * 10))
        while b_int:
            a_int, b_int = b_int, a_int % b_int
        return a_int / 10.0
    result = values[0]
    for v in values[1:]:
        result = gcd_pair(result, v)
    return result if result > 0 else 1.0


class DeribitRFQAdapter(ExchangeRFQExecutor):
    """Block RFQ execution on Deribit."""

    def __init__(self, auth):
        self._auth = auth

    def execute(
        self,
        legs,
        action: str = "buy",
        timeout_seconds: int = 60,
        min_improvement_pct: float = -999.0,
        poll_interval_seconds: int = 3,
    ) -> Any:
        """
        Execute a Deribit block RFQ: create → poll → accept/cancel.

        Args:
            legs: List of objects with .instrument, .side, .qty attributes
                  (or dicts with those keys).
            action: "buy" or "sell" (overall direction).
            timeout_seconds: Max time to wait for quotes.
            min_improvement_pct: Minimum improvement over orderbook cost.
            poll_interval_seconds: How often to poll for quotes.

        Returns:
            RFQResult-compatible dict with: success, request_id, state,
            total_cost, message, trades.
        """
        from rfq import RFQResult, RFQState

        # Build the leg descriptions for Deribit
        deribit_legs = self._normalize_legs(legs)
        if not deribit_legs:
            return RFQResult(
                success=False, state=RFQState.CANCELLED,
                message="Failed to normalize legs for Deribit RFQ",
            )

        # Check minimum size
        total_contracts = sum(abs(l.get("amount", 0)) for l in deribit_legs)
        if total_contracts < BLOCK_RFQ_MIN_CONTRACTS:
            return RFQResult(
                success=False, state=RFQState.CANCELLED,
                message=f"Below Deribit minimum ({total_contracts} < {BLOCK_RFQ_MIN_CONTRACTS} BTC contracts)",
            )

        # Create RFQ
        create_resp = self._auth.call("private/create_block_rfq", {
            "legs": deribit_legs,
        })
        if not self._auth.is_successful(create_resp):
            error = create_resp.get("error", {})
            return RFQResult(
                success=False, state=RFQState.CANCELLED,
                message=f"RFQ creation failed: {error.get('message', 'unknown')}",
            )

        rfq_id = create_resp["result"].get("block_rfq_id")
        logger.info(f"Deribit block RFQ created: {rfq_id}")

        # Poll for quotes
        deadline = time.time() + timeout_seconds
        best_quote = None

        while time.time() < deadline:
            time.sleep(poll_interval_seconds)
            rfqs = self._get_rfqs()
            if not rfqs:
                continue

            our_rfq = None
            for rfq in rfqs:
                if rfq.get("block_rfq_id") == rfq_id:
                    our_rfq = rfq
                    break

            if not our_rfq:
                continue

            state = our_rfq.get("state", "")
            if state in ("expired", "cancelled", "filled"):
                break

            # Check for quotes (aggregated bids/asks after 5s)
            bids = our_rfq.get("bids", [])
            asks = our_rfq.get("asks", [])

            if action == "sell" and bids:
                best_quote = {"direction": "sell", "price": max(b["price"] for b in bids)}
            elif action == "buy" and asks:
                best_quote = {"direction": "buy", "price": min(a["price"] for a in asks)}

            if best_quote:
                break

        if not best_quote:
            # Cancel the RFQ
            self._auth.call("private/cancel_block_rfq", {"block_rfq_id": rfq_id})
            return RFQResult(
                success=False, request_id=rfq_id, state=RFQState.CANCELLED,
                message="No quotes received within timeout",
            )

        # Accept (cross) the RFQ
        amounts = [abs(l["amount"]) for l in deribit_legs]
        gcd = _compute_gcd(amounts)
        ratio_legs = []
        for l in deribit_legs:
            sign = 1 if l["direction"] == "buy" else -1
            ratio_legs.append({
                "instrument_name": l["instrument_name"],
                "ratio": str(sign * int(round(abs(l["amount"]) / gcd))),
            })

        accept_resp = self._auth.call("private/accept_block_rfq", {
            "block_rfq_id": rfq_id,
            "direction": best_quote["direction"],
            "price": best_quote["price"],
            "amount": gcd,
            "legs": ratio_legs,
        })

        if self._auth.is_successful(accept_resp):
            logger.info(f"Deribit block RFQ accepted: {rfq_id} @ {best_quote['price']}")
            return RFQResult(
                success=True,
                request_id=rfq_id,
                state=RFQState.FILLED,
                total_cost=best_quote["price"],
                message="Block RFQ filled",
            )
        else:
            error = accept_resp.get("error", {})
            return RFQResult(
                success=False,
                request_id=rfq_id,
                state=RFQState.CANCELLED,
                message=f"RFQ accept failed: {error.get('message', 'unknown')}",
            )

    def execute_phased(self, legs, action: str = "buy", **kwargs) -> Any:
        """
        Phased RFQ execution — maps to Deribit good_til_cancelled crossing.

        For Deribit, we create the RFQ and place a GTC crossing order at
        the desired price, letting it sit until a maker matches or the
        RFQ expires. This is the Deribit-native equivalent of our phased
        execution on Coincall.
        """
        # Simplified: delegate to execute() for now.
        # Full GTC-based phased execution can be added when we have
        # positions large enough to qualify for block RFQ (25 BTC min).
        return self.execute(legs, action, **kwargs)

    def get_orderbook_cost(self, legs, action: str = "buy") -> Optional[float]:
        """Calculate what the structure would cost on the orderbook."""
        total_cost = 0.0
        for leg in legs:
            instr = getattr(leg, "instrument", None) or leg.get("instrument", "")
            side = getattr(leg, "side", None) or leg.get("side", "")
            qty = abs(getattr(leg, "qty", 0) or leg.get("qty", 0))

            resp = self._auth.call("public/get_order_book", {
                "instrument_name": instr,
                "depth": 5,
            })
            if not self._auth.is_successful(resp):
                return None

            ob = resp["result"]
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])

            # Determine which side of the book we hit
            if (action == "buy" and side == "buy") or (action == "sell" and side == "sell"):
                # We're paying the ask
                if not asks:
                    return None
                total_cost += asks[0][0] * qty
            else:
                # We're hitting the bid (receiving)
                if not bids:
                    return None
                total_cost -= bids[0][0] * qty

        return total_cost

    def _normalize_legs(self, legs) -> list:
        """Convert leg objects/dicts to Deribit block RFQ leg format."""
        result = []
        for leg in legs:
            instr = getattr(leg, "instrument", None) or leg.get("instrument", "")
            side = getattr(leg, "side", None) or leg.get("side", "")
            qty = abs(getattr(leg, "qty", 0) or leg.get("qty", 0))
            if not instr or not side or qty <= 0:
                logger.warning(f"Invalid leg: {leg}")
                return []
            result.append({
                "instrument_name": instr,
                "direction": side,
                "amount": qty,
            })
        return result

    def _get_rfqs(self) -> list:
        """Poll for current block RFQs."""
        resp = self._auth.call("private/get_block_rfqs", {"currency": "BTC"})
        if self._auth.is_successful(resp):
            return resp["result"]
        return []
