"""
execution/pricing.py — Stateless order-price calculator.

No I/O — receives an OrderbookSnapshot and returns a PricingResult.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from execution.currency import Currency, OrderbookSnapshot, Price

logger = logging.getLogger(__name__)


@dataclass
class PricingResult:
    """Result of a pricing computation."""
    price: Optional[Price]       # computed order price (None if refused)
    fair_value: Optional[Price]  # fair-value estimate (always computed when possible)
    reason: str                  # human-readable explanation
    refused: bool = False        # True if a guard blocked the price


class PricingEngine:
    """Stateless order-price calculator.

    Each pricing mode is a separate method.  Adding a new mode requires
    only a new ``_price_<mode>`` method + entry in ``_MODES``.
    """

    # ── Public API ───────────────────────────────────────────────────────

    def compute(
        self,
        orderbook: OrderbookSnapshot,
        side: str,
        mode: str,
        aggression: float = 0.0,
        buffer_pct: float = 2.0,
        min_price_pct_of_fair: Optional[float] = None,
        min_floor_price: Optional[Price] = None,
    ) -> PricingResult:
        """Compute the order price for a single leg.

        Parameters
        ----------
        orderbook : OrderbookSnapshot
        side : "buy" or "sell"
        mode : one of "fair", "aggressive", "mid", "passive", "top_of_book", "mark"
        aggression : 0.0–1.0, used by "fair" mode
        buffer_pct : % buffer for "aggressive" mode (default 2.0)
        min_price_pct_of_fair : optional floor ratio for "fair" sell orders
        min_floor_price : absolute minimum Price fallback for empty books
        """
        handler = self._MODES.get(mode)
        if handler is None:
            raise ValueError(
                f"Unknown pricing mode '{mode}', "
                f"must be one of {sorted(self._MODES)}"
            )
        return handler(
            self, orderbook, side,
            aggression=aggression,
            buffer_pct=buffer_pct,
            min_price_pct_of_fair=min_price_pct_of_fair,
            min_floor_price=min_floor_price,
        )

    def fair_value(self, orderbook: OrderbookSnapshot) -> Optional[Price]:
        """Convenience: compute fair-value estimate without an order price."""
        fv = self._compute_fair_value(orderbook)
        if fv is None:
            return None
        return Price(fv, orderbook.currency)

    # ── Fair-value calculation (shared by 'fair' mode and fair_value()) ──

    @staticmethod
    def _compute_fair_value(ob: OrderbookSnapshot) -> Optional[float]:
        """Compute a single-number fair-value estimate in native denomination.

        Priority:
          1. Full book — mark if inside [bid, ask], else midpoint.
          2. Bid only  — max(mark, bid).
          3. Ask only  — min(mark, ask).
          4. Mark only — trust the mark.
          5. Nothing   — None.
        """
        best_bid = ob.best_bid
        best_ask = ob.best_ask
        mark = ob.mark if ob.mark and ob.mark > 0 else None

        if best_bid is not None and best_ask is not None:
            if mark is not None and best_bid <= mark <= best_ask:
                return mark
            return (best_bid + best_ask) / 2.0

        if best_bid is not None:
            if mark is not None:
                return max(mark, best_bid)
            return best_bid

        if best_ask is not None:
            if mark is not None:
                return min(mark, best_ask)
            return best_ask

        if mark is not None:
            return mark

        return None

    # ── Pricing modes ────────────────────────────────────────────────────

    def _price_fair(
        self,
        ob: OrderbookSnapshot,
        side: str,
        *,
        aggression: float = 0.0,
        min_price_pct_of_fair: Optional[float] = None,
        min_floor_price: Optional[Price] = None,
        **_kw,
    ) -> PricingResult:
        fair = self._compute_fair_value(ob)
        cur = ob.currency

        if fair is None:
            # No fair value available — try min_floor_price
            if min_floor_price is not None:
                return PricingResult(
                    price=min_floor_price,
                    fair_value=None,
                    reason="no fair value, using min_floor_price",
                )
            return PricingResult(
                price=None, fair_value=None,
                reason="no fair value computable (empty book, no mark)",
            )

        fair_price = Price(fair, cur)

        # Compute order price by sliding from fair toward the aggressive side
        price_amount: Optional[float] = None
        if side == "sell":
            spread_to_bid = (fair - ob.best_bid) if ob.best_bid is not None else 0.0
            price_amount = fair - aggression * spread_to_bid

            # min_price_pct_of_fair guard (sell only)
            if min_price_pct_of_fair is not None:
                floor = fair * min_price_pct_of_fair
                if price_amount < floor:
                    logger.warning(
                        f"PricingEngine: {ob.symbol} sell price {price_amount:.6f} "
                        f"< floor {floor:.6f} "
                        f"(fair={fair:.6f} × {min_price_pct_of_fair:.0%})"
                        f" — refusing to place"
                    )
                    return PricingResult(
                        price=None, fair_value=fair_price,
                        reason=f"sell price below min_price_pct_of_fair ({min_price_pct_of_fair})",
                        refused=True,
                    )
        else:  # buy
            if ob.best_ask is not None:
                spread_to_ask = ob.best_ask - fair
                price_amount = fair + aggression * spread_to_ask
            elif ob.mark and ob.mark > 0:
                price_amount = ob.mark * (1.0 + aggression * 0.2)
            else:
                price_amount = fair

        if price_amount is not None and price_amount > 0:
            return PricingResult(
                price=Price(price_amount, cur),
                fair_value=fair_price,
                reason=f"fair (aggression={aggression})",
            )

        # Last resort: min_floor_price
        if min_floor_price is not None:
            return PricingResult(
                price=min_floor_price,
                fair_value=fair_price,
                reason="fair price zero/negative, using min_floor_price",
            )

        return PricingResult(
            price=None, fair_value=fair_price,
            reason="fair price zero/negative, no floor",
        )

    def _price_aggressive(
        self,
        ob: OrderbookSnapshot,
        side: str,
        *,
        buffer_pct: float = 2.0,
        min_floor_price: Optional[Price] = None,
        **_kw,
    ) -> PricingResult:
        cur = ob.currency
        buffer = 1 + (buffer_pct / 100.0)
        fair = self._compute_fair_value(ob)
        fair_price = Price(fair, cur) if fair is not None else None

        if side == "buy" and ob.best_ask is not None:
            p = ob.best_ask * buffer
            return PricingResult(
                price=Price(p, cur), fair_value=fair_price,
                reason=f"aggressive buy: ask × {buffer:.3f}",
            )
        if side == "sell" and ob.best_bid is not None:
            p = ob.best_bid / buffer
            return PricingResult(
                price=Price(p, cur), fair_value=fair_price,
                reason=f"aggressive sell: bid / {buffer:.3f}",
            )

        if min_floor_price is not None:
            return PricingResult(
                price=min_floor_price, fair_value=fair_price,
                reason="aggressive: no book, using min_floor_price",
            )
        return PricingResult(
            price=None, fair_value=fair_price,
            reason="aggressive: no best_ask/bid available",
        )

    def _price_mid(
        self, ob: OrderbookSnapshot, side: str, **_kw,
    ) -> PricingResult:
        cur = ob.currency
        fair = self._compute_fair_value(ob)
        fair_price = Price(fair, cur) if fair is not None else None

        if ob.best_bid is not None and ob.best_ask is not None:
            mid = (ob.best_bid + ob.best_ask) / 2.0
            return PricingResult(
                price=Price(mid, cur), fair_value=fair_price,
                reason="mid",
            )
        return PricingResult(
            price=None, fair_value=fair_price,
            reason="mid: need both bid and ask",
        )

    def _price_passive(
        self, ob: OrderbookSnapshot, side: str, **_kw,
    ) -> PricingResult:
        cur = ob.currency
        fair = self._compute_fair_value(ob)
        fair_price = Price(fair, cur) if fair is not None else None

        if side == "buy" and ob.best_bid is not None:
            return PricingResult(
                price=Price(ob.best_bid, cur), fair_value=fair_price,
                reason="passive buy: join bid",
            )
        if side == "sell" and ob.best_ask is not None:
            return PricingResult(
                price=Price(ob.best_ask, cur), fair_value=fair_price,
                reason="passive sell: join ask",
            )
        return PricingResult(
            price=None, fair_value=fair_price,
            reason=f"passive: no {'bid' if side == 'buy' else 'ask'} available",
        )

    def _price_top_of_book(
        self, ob: OrderbookSnapshot, side: str, **_kw,
    ) -> PricingResult:
        cur = ob.currency
        fair = self._compute_fair_value(ob)
        fair_price = Price(fair, cur) if fair is not None else None

        if side == "buy" and ob.best_ask is not None:
            return PricingResult(
                price=Price(ob.best_ask, cur), fair_value=fair_price,
                reason="top_of_book buy: lift ask",
            )
        if side == "sell" and ob.best_bid is not None:
            return PricingResult(
                price=Price(ob.best_bid, cur), fair_value=fair_price,
                reason="top_of_book sell: hit bid",
            )
        return PricingResult(
            price=None, fair_value=fair_price,
            reason=f"top_of_book: no {'ask' if side == 'buy' else 'bid'} available",
        )

    def _price_mark(
        self, ob: OrderbookSnapshot, side: str, **_kw,
    ) -> PricingResult:
        cur = ob.currency
        fair = self._compute_fair_value(ob)
        fair_price = Price(fair, cur) if fair is not None else None

        if ob.mark and ob.mark > 0:
            return PricingResult(
                price=Price(ob.mark, cur), fair_value=fair_price,
                reason="mark price",
            )
        # Fallback to mid
        if ob.best_bid is not None and ob.best_ask is not None:
            mid = (ob.best_bid + ob.best_ask) / 2.0
            return PricingResult(
                price=Price(mid, cur), fair_value=fair_price,
                reason="mark unavailable, using mid",
            )
        return PricingResult(
            price=None, fair_value=fair_price,
            reason="mark: no mark or book available",
        )

    # ── Mode registry ────────────────────────────────────────────────────

    _MODES = {
        "fair": _price_fair,
        "aggressive": _price_aggressive,
        "mid": _price_mid,
        "passive": _price_passive,
        "top_of_book": _price_top_of_book,
        "mark": _price_mark,
    }
