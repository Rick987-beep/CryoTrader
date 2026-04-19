#!/usr/bin/env python3
"""
Iron Condor Integration Test — Full Framework Path (Deribit Testnet)

Uses LifecycleEngine → Router → FillManager → OrderManager, same as
production strategies. Verifies the complete state machine:

    PENDING_OPEN → OPENING → OPEN → PENDING_CLOSE → CLOSING → CLOSED

With correct PnL, fees, currency, and state transitions throughout.

Each condor uses a DIFFERENT expiry to avoid order_overlap and
reduce_only conflicts when trading the same strikes.

Run:  EXCHANGE=deribit TRADING_ENVIRONMENT=testnet \
        python -m pytest tests/live/ -m live -k iron_condor -v --tb=long
"""

import os
import sys
import time
import logging
from typing import List, Dict

import pytest

# ── Environment ──────────────────────────────────────────────────────────────
os.environ.setdefault("TRADING_ENVIRONMENT", "testnet")
os.environ.setdefault("EXCHANGE", "deribit")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from exchanges.deribit.auth import DeribitAuth
from exchanges.deribit.executor import DeribitExecutorAdapter
from exchanges.deribit.market_data import DeribitMarketDataAdapter
from exchanges.deribit.account import DeribitAccountAdapter
from exchanges.deribit import DERIBIT_STATE_MAP
from lifecycle_engine import LifecycleEngine
from trade_lifecycle import TradeLeg, TradeLifecycle, TradeState
from execution.profiles import ExecutionProfile, PhaseConfig
from execution.currency import Price, Currency
from account_manager import AccountSnapshot, PositionSnapshot

pytestmark = pytest.mark.live

log = logging.getLogger("iron_condor_v2")

INTERVAL = 15  # seconds between operations


# ── Helpers ──────────────────────────────────────────────────────────────────

def _skip_if_no_creds():
    if not os.environ.get("DERIBIT_CLIENT_ID") or not os.environ.get("DERIBIT_CLIENT_SECRET"):
        pytest.skip("DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET not set")


def make_2phase_profile(name: str) -> ExecutionProfile:
    """2-phase: mid (15s, 50% aggression) → aggressive (20s, 5% buffer)."""
    return ExecutionProfile(
        name=name,
        open_phases=[
            PhaseConfig(pricing="fair", fair_aggression=0.5,
                        duration_seconds=15.0, reprice_interval=8.0),
            PhaseConfig(pricing="aggressive", duration_seconds=20.0,
                        buffer_pct=5.0, reprice_interval=10.0),
        ],
        close_phases=[
            PhaseConfig(pricing="fair", fair_aggression=0.5,
                        duration_seconds=15.0, reprice_interval=8.0),
            PhaseConfig(pricing="aggressive", duration_seconds=20.0,
                        buffer_pct=5.0, reprice_interval=10.0),
        ],
        open_atomic=False,
        close_best_effort=True,
    )


def discover_expiries(market_data: DeribitMarketDataAdapter) -> List[str]:
    """Find one expiry per month: end-of-April, May, June 2026."""
    instruments = market_data.get_option_instruments()
    assert instruments, "No instruments available"

    month_tags = ["APR26", "MAY26", "JUN26"]
    now_ts = time.time() * 1000

    month_expiries: Dict[str, Dict[str, float]] = {m: {} for m in month_tags}
    for inst in instruments:
        name = inst["symbolName"]
        if not name.startswith("BTC-"):
            continue
        parts = name.split("-")
        exp_str = parts[1]
        exp_ts = inst["expirationTimestamp"]
        if exp_ts < now_ts:
            continue
        for tag in month_tags:
            if tag in exp_str:
                if exp_str not in month_expiries[tag]:
                    month_expiries[tag][exp_str] = exp_ts
                break

    selected = []
    for tag in month_tags:
        expiries = month_expiries[tag]
        assert expiries, f"No expiries found for {tag}"
        last_exp = max(expiries, key=expiries.get)
        log.info(f"  {tag}: {last_exp}")
        selected.append(last_exp)

    return selected


def build_condor_legs(
    market_data: DeribitMarketDataAdapter, expiry: str, idx: float,
) -> List[TradeLeg]:
    """Build 4-leg iron condor for a given expiry around the ATM."""
    atm = round(idx / 1000) * 1000
    specs = [
        (int(atm - 2000), "P", "buy"),
        (int(atm - 1000), "P", "sell"),
        (int(atm + 1000), "C", "sell"),
        (int(atm + 2000), "C", "buy"),
    ]
    legs = []
    for strike, cp, side in specs:
        symbol = f"BTC-{expiry}-{strike}-{cp}"
        ob = market_data.get_option_orderbook(symbol)
        if ob:
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            bid_s = f"{bids[0]['price']:.6f}" if bids else "none"
            ask_s = f"{asks[0]['price']:.6f}" if asks else "none"
            log.info(f"    {side:4s} {symbol}  bid={bid_s}  ask={ask_s}")
        legs.append(TradeLeg(symbol=symbol, qty=1.0, side=side))
    return legs


def build_account_snapshot(account: DeribitAccountAdapter) -> AccountSnapshot:
    """Build a real AccountSnapshot from the Deribit testnet account."""
    info = account.get_account_info()
    positions = account.get_positions(force_refresh=True)

    pos_snaps = []
    for p in positions:
        direction = p.get("_direction", "buy")
        pos_snaps.append(PositionSnapshot(
            position_id=p.get("position_id", p["symbol"]),
            symbol=p["symbol"],
            qty=p.get("qty", 0),
            side="long" if direction == "buy" else "short",
            entry_price=p.get("avg_price", 0),
            mark_price=p.get("mark_price", 0),
            unrealized_pnl=p.get("unrealized_pnl", 0),
            roi=p.get("roi", 0),
            delta=p.get("delta", 0),
            gamma=p.get("gamma", 0),
            theta=p.get("theta", 0),
            vega=p.get("vega", 0),
        ))

    return AccountSnapshot(
        equity=info.get("equity", 0) if info else 0,
        available_margin=info.get("available_margin", 0) if info else 0,
        initial_margin=info.get("initial_margin", 0) if info else 0,
        maintenance_margin=info.get("maintenance_margin", 0) if info else 0,
        unrealized_pnl=info.get("unrealized_pnl", 0) if info else 0,
        positions=tuple(pos_snaps),
        timestamp=time.time(),
    )


def drive_until_state(
    engine: LifecycleEngine,
    trade: TradeLifecycle,
    target_states: set,
    account: DeribitAccountAdapter,
    label: str,
    timeout: float = 55.0,
    tick_interval: float = 2.0,
) -> TradeState:
    """Tick the engine until the trade reaches one of the target states."""
    deadline = time.time() + timeout
    last_state = trade.state

    while time.time() < deadline:
        snap = build_account_snapshot(account)
        engine.tick(snap)

        if trade.state != last_state:
            log.info(f"  [{label}] {last_state.value} → {trade.state.value}")
            last_state = trade.state

        if trade.state in target_states:
            return trade.state
        time.sleep(tick_interval)

    log.warning(f"  [{label}] timeout after {timeout}s — stuck in {trade.state.value}")
    return trade.state


# ── Test ─────────────────────────────────────────────────────────────────────

@pytest.mark.live
def test_iron_condor_full_lifecycle():
    """Open 3 iron condors on different expiries, close them, verify state."""
    _skip_if_no_creds()

    t0 = time.time()

    # ── Initialize ───────────────────────────────────────────────────
    auth = DeribitAuth()
    executor = DeribitExecutorAdapter(auth)
    market_data = DeribitMarketDataAdapter(auth)
    account = DeribitAccountAdapter(auth)

    engine = LifecycleEngine(
        executor=executor,
        rfq_executor=None,
        market_data=market_data,
        exchange_state_map=DERIBIT_STATE_MAP,
        expected_denomination=Currency.BTC,
    )

    idx = market_data.get_index_price()
    assert idx and idx > 0, f"Bad index price: {idx}"
    log.info(f"BTC index: ${idx:,.0f}")

    expiries = discover_expiries(market_data)
    profile = make_2phase_profile("iron_condor")
    trades: Dict[int, TradeLifecycle] = {}

    # ── Build legs ───────────────────────────────────────────────────
    all_legs: Dict[int, List[TradeLeg]] = {}
    for i, exp in enumerate(expiries, 1):
        log.info(f"  Condor #{i} → {exp}")
        all_legs[i] = build_condor_legs(market_data, exp, idx)

    # ── PHASE 1: Open 3 condors ─────────────────────────────────────
    log.info("=== OPENING 3 IRON CONDORS ===")

    for i in range(1, 4):
        if i > 1:
            time.sleep(INTERVAL)

        trade = engine.create(
            legs=all_legs[i],
            strategy_id=f"iron_condor_test_{i}",
            metadata={
                "_execution_profile": profile,
                "condor_num": i,
                "expiry": expiries[i - 1],
            },
        )
        trades[i] = trade

        ok = engine.open(trade.id)
        assert ok, f"Condor #{i}: engine.open() refused"

        final = drive_until_state(
            engine, trade, {TradeState.OPEN, TradeState.FAILED},
            account, f"OPEN #{i}", timeout=55.0,
        )
        assert final == TradeState.OPEN, f"Condor #{i}: expected OPEN, got {final.value}"

        log.info(f"  Condor #{i} OPENED — {len(trade.open_legs)} legs")

    # ── Gap before close ─────────────────────────────────────────────
    time.sleep(INTERVAL)

    # ── PHASE 2: Close 3 condors ─────────────────────────────────────
    log.info("=== CLOSING 3 IRON CONDORS ===")

    for i in range(1, 4):
        if i > 1:
            time.sleep(INTERVAL)

        trade = trades[i]
        assert trade.state == TradeState.OPEN

        trade.state = TradeState.PENDING_CLOSE
        trade.metadata["close_trigger"] = "test_schedule"

        final = drive_until_state(
            engine, trade, {TradeState.CLOSED, TradeState.FAILED},
            account, f"CLOSE #{i}", timeout=55.0,
        )
        assert final == TradeState.CLOSED, f"Condor #{i}: expected CLOSED, got {final.value}"

        log.info(f"  Condor #{i} CLOSED — PnL={trade.realized_pnl:+.6f} BTC")

    # ── PHASE 3: Verify state machine integrity ──────────────────────
    elapsed = time.time() - t0
    log.info(f"=== VERIFICATION ({elapsed:.0f}s elapsed) ===")

    total_pnl = 0.0
    total_fees_btc = 0.0

    for i in range(1, 4):
        trade = trades[i]

        # State
        assert trade.state == TradeState.CLOSED, f"Condor #{i}: not CLOSED"
        assert trade.realized_pnl is not None, f"Condor #{i}: no realized_pnl"
        assert trade.opened_at is not None, f"Condor #{i}: no opened_at"
        assert trade.closed_at is not None, f"Condor #{i}: no closed_at"

        # All legs filled
        for leg in trade.open_legs:
            assert leg.filled_qty > 0, f"Condor #{i}: unfilled open leg {leg.symbol}"
        for leg in trade.close_legs:
            assert leg.filled_qty > 0, f"Condor #{i}: unfilled close leg {leg.symbol}"

        # Fees captured
        assert trade.open_fees is not None, f"Condor #{i}: no open_fees"
        assert trade.close_fees is not None, f"Condor #{i}: no close_fees"

        # Currency detected from fills
        assert trade.currency == Currency.BTC, f"Condor #{i}: currency={trade.currency}"

        # Fill prices are typed Price objects
        for leg in trade.open_legs:
            if leg.fill_price is not None:
                assert isinstance(leg.fill_price, Price), \
                    f"Condor #{i}: open fill_price not Price: {type(leg.fill_price)}"

        total_pnl += trade.realized_pnl
        if trade.total_fees:
            total_fees_btc += float(trade.total_fees)

        hold_time = trade.closed_at - trade.opened_at
        log.info(f"  Condor #{i}: PnL={trade.realized_pnl:+.6f} BTC, "
                 f"fees={trade.total_fees}, hold={hold_time:.0f}s")

    log.info(f"  TOTALS: PnL={total_pnl:+.6f} fees={total_fees_btc:.6f} "
             f"net={total_pnl - total_fees_btc:+.6f} BTC")


# ── Standalone mode ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-5s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    for name in ("ct.execution", "ct.strategy", "execution.fill_manager",
                 "order_manager", "lifecycle_engine", "execution.router",
                 "exchanges.deribit.executor"):
        logging.getLogger(name).setLevel(logging.INFO)

    test_iron_condor_full_lifecycle()
