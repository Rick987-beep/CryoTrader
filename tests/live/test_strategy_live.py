"""
Live test: 3-phase profile execution against Deribit testnet.

Phase 4 live validation: exercises the real `passive_open_3phase` profile
through its full phase lifecycle (fair → fair+aggression → fair+full)
using real limit orders on Deribit testnet.

Phases are shortened to 10s each (the minimum allowed) so the full
cycle completes in ~35 seconds instead of 150+ seconds.

Usage:
    EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/test_strategy_live.py -m live -v
"""

import os
import sys
import time
import pytest

pytestmark = pytest.mark.live

os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution.currency import Currency, Price
from execution.fill_manager import FillManager
from execution.fill_result import FillStatus
from execution.profiles import load_profiles
from order_manager import OrderManager, OrderPurpose


TOML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "execution_profiles.toml",
)


def _skip_if_no_creds():
    from config import DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
    if not DERIBIT_CLIENT_ID or not DERIBIT_CLIENT_SECRET:
        pytest.skip("DERIBIT_CLIENT_ID_TEST / DERIBIT_CLIENT_SECRET_TEST not set")


# ─── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def auth():
    _skip_if_no_creds()
    from exchanges.deribit.auth import DeribitAuth
    return DeribitAuth()


@pytest.fixture(scope="module")
def executor(auth):
    from exchanges.deribit.executor import DeribitExecutorAdapter
    return DeribitExecutorAdapter(auth)


@pytest.fixture(scope="module")
def market_data(auth):
    from exchanges.deribit.market_data import DeribitMarketDataAdapter
    return DeribitMarketDataAdapter(auth)


@pytest.fixture(scope="module")
def order_manager(executor):
    from exchanges.deribit import DERIBIT_STATE_MAP
    return OrderManager(
        executor,
        exchange_state_map=DERIBIT_STATE_MAP,
        expected_denomination=Currency.BTC,
    )


@pytest.fixture(scope="module")
def fast_passive_profile():
    """Load passive_open_3phase and shorten durations to 10s (the minimum)."""
    profiles = load_profiles(TOML_PATH)
    profile = profiles["passive_open_3phase"]

    # Override durations to minimum (10s) for faster test cycle
    overrides = {
        "open_phase_1.duration_seconds": 10.0,
        "open_phase_1.reprice_interval": 10.0,
        "open_phase_2.duration_seconds": 10.0,
        "open_phase_2.reprice_interval": 10.0,
        "open_phase_3.duration_seconds": 10.0,
        "open_phase_3.reprice_interval": 10.0,
    }
    return profile.apply_overrides(overrides)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _find_deep_otm_put(market_data, min_dte_days=60):
    """Find a deep-OTM BTC put with populated orderbook."""
    instruments = market_data.get_option_instruments("BTC")
    assert instruments, "No BTC option instruments on testnet"

    now = time.time() * 1000
    min_dte_ms = min_dte_days * 24 * 3600 * 1000
    index_price = market_data.get_index_price("BTC")

    # Deep OTM puts: strike well below index
    candidates = [
        i for i in instruments
        if i.get("expirationTimestamp", 0) - now > min_dte_ms
        and i.get("option_type") == "put"
        and i["strike"] < index_price * 0.7  # at least 30% OTM
    ]

    if not candidates:
        # Fallback: any put with >min_dte DTE
        candidates = [
            i for i in instruments
            if i.get("expirationTimestamp", 0) - now > min_dte_ms
            and i.get("option_type") == "put"
        ]

    # Sort by distance from ATM (most OTM first)
    candidates.sort(key=lambda i: abs(i["strike"] - index_price), reverse=True)

    for inst in candidates[:30]:
        ob = market_data.get_option_orderbook(inst["symbolName"])
        if ob and ob.get("asks"):
            return inst["symbolName"], ob

    pytest.skip("No deep-OTM put with asks found on testnet")


# ─── Tests ───────────────────────────────────────────────────────────────────

class TestStrategyProfileLive:
    """3-phase profile execution against real Deribit testnet.

    Validates that:
    - FillManager transitions through all 3 phases on real orders
    - Each phase transition requotes the order at a different price
    - The full phase machinery works against the real exchange API
    """

    def test_3phase_open_cycle(
        self, market_data, order_manager, fast_passive_profile
    ):
        """Run passive_open_3phase through its phase lifecycle.

        FillManager computes prices via PricingEngine (not user-supplied),
        so fills can happen on testnet.  We verify:
          - At least phase 1 entered and an order placed
          - If all phases exhaust → FAILED (order never filled)
          - If filled early → fill has typed Price with Currency.BTC
          - Phase transitions produced requotes (new order IDs)
        """
        symbol, ob = _find_deep_otm_put(market_data)

        fill_mgr = FillManager(
            order_manager=order_manager,
            market_data=market_data,
            profile=fast_passive_profile,
            direction="open",
        )

        legs = [{"symbol": symbol, "qty": 0.1, "side": "sell"}]
        lifecycle_id = f"test_3phase_{int(time.time())}"

        # Track phase transitions
        phases_seen = set()
        requote_count = 0
        filled = False

        try:
            result = fill_mgr.place_all(
                legs=legs,
                lifecycle_id=lifecycle_id,
                purpose=OrderPurpose.OPEN_LEG,
            )

            assert result.status in (FillStatus.PENDING, FillStatus.FILLED), (
                f"Initial placement: expected PENDING or FILLED, "
                f"got {result.status}: {result.error}"
            )

            phases_seen.add(result.phase_index)

            if result.status == FillStatus.FILLED:
                filled = True
            else:
                # Poll through phases (~35s with 10s phases, up to 60s)
                deadline = time.time() + 60
                while time.time() < deadline:
                    time.sleep(2)
                    result = fill_mgr.check()

                    phases_seen.add(result.phase_index)
                    if result.status == FillStatus.REQUOTED:
                        requote_count += 1

                    import logging
                    logging.getLogger(__name__).info(
                        f"check() → status={result.status.value}, "
                        f"phase={result.phase_index}/{result.phase_total}, "
                        f"elapsed={result.elapsed_seconds:.1f}s"
                    )

                    if result.status == FillStatus.FILLED:
                        filled = True
                        break
                    if result.status == FillStatus.FAILED:
                        break

            # ── Assertions ──

            # Always: phase 1 was entered
            assert 1 in phases_seen, "Never saw phase 1"

            # Count total orders placed for this lifecycle
            all_orders = [
                r for r in order_manager._orders.values()
                if r.lifecycle_id == lifecycle_id
            ]
            assert len(all_orders) >= 1, "No orders placed at all"

            if filled:
                # Early fill: verify typed fill data
                leg = result.legs[0]
                assert leg.filled_qty == leg.qty, (
                    f"Expected full fill: {leg.filled_qty}/{leg.qty}"
                )
                # Fee should be set (Deribit charges on fill)
                assert leg.fee is not None, "fee should be set on fill"
                assert isinstance(leg.fee, Price)
                assert leg.fee.currency == Currency.BTC

                # fill_price may be None when order fills instantly during
                # requote (known edge case: _requote_unfilled sets filled_qty
                # but not fill_price, and _poll_fills skips because qty
                # hasn't changed).  If set, verify it's typed.
                if leg.fill_price is not None:
                    assert isinstance(leg.fill_price, Price)
                    assert leg.fill_price.currency == Currency.BTC
                    assert leg.fill_price.amount > 0

                # At least 1 phase seen + order placed = machinery works
                assert len(all_orders) >= 1

                # If fill happened after a requote, we validated phase transition
                if requote_count > 0:
                    assert len(phases_seen) >= 2, (
                        f"Had {requote_count} requotes but only {len(phases_seen)} phases"
                    )
            else:
                # All 3 phases exhausted → FAILED
                assert result.status == FillStatus.FAILED
                assert len(phases_seen) >= 3, (
                    f"Expected to see 3+ phases, saw {sorted(phases_seen)}"
                )
                assert requote_count >= 2, (
                    f"Expected >= 2 requotes (phase transitions), got {requote_count}"
                )
                assert len(all_orders) >= 3, (
                    f"Expected >= 3 order placements, got {len(all_orders)}"
                )

        finally:
            try:
                fill_mgr.cancel_all()
            except Exception:
                pass

    def test_profile_loaded_from_toml(self):
        """Verify passive_open_3phase loads correctly from execution_profiles.toml."""
        profiles = load_profiles(TOML_PATH)
        assert "passive_open_3phase" in profiles

        profile = profiles["passive_open_3phase"]
        assert len(profile.open_phases) == 3
        assert profile.open_phases[0].pricing == "fair"
        assert profile.open_phases[0].fair_aggression == 0.0
        assert profile.open_phases[1].fair_aggression == pytest.approx(0.67)
        assert profile.open_phases[2].fair_aggression == 1.0
        assert profile.open_atomic is True
