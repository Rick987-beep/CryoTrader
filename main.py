#!/usr/bin/env python3
"""
CoincallTrader — Main Entry Point (Launcher)

Wires all services via TradingContext, registers strategies, and runs
the position monitor loop.  Strategy definitions live in strategies/.

Usage:
    python main.py
"""

import json
import logging
import os
import shutil
import signal
import sys
import time

from strategy import build_context, StrategyRunner
from trade_lifecycle import TradeLifecycle, TradeState
from strategies import blueprint_strangle, atm_straddle, atm_straddle_index_move, daily_put_sell, smoke_test_strangle
from persistence import TradeStatePersistence
from health_check import HealthChecker
from dashboard import start_dashboard
from config import ENVIRONMENT, DEPLOYMENT_TARGET

_DEV_MODE = DEPLOYMENT_TARGET == "development"

# =============================================================================
# Dev-mode startup cleanup  (DEPLOYMENT_TARGET == 'development' only)
# =============================================================================

if _DEV_MODE:
    _stale = [
        "logs/trades_snapshot.json",
        "logs/active_orders.json",
        "logs/trading.log",
    ]
    for _f in _stale:
        if os.path.exists(_f):
            os.remove(_f)
            print(f"[DEV] Removed stale {_f}")

# =============================================================================
# Logging
# =============================================================================

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG if _DEV_MODE else logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log"),
        logging.StreamHandler(),
    ],
)
if not _DEV_MODE:
    # Production: promote only key modules to INFO, keep root at WARNING.
    for _name in ("__main__", "strategy", "trade_lifecycle", "trade_execution",
                  "rfq", "account_manager", "dashboard", "persistence",
                  "strategies.daily_put_sell", "strategies.atm_straddle",
                  "strategies.blueprint_strangle", "order_manager",
                  "ema_filter", "telegram_notifier"):
        logging.getLogger(_name).setLevel(logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# Active Strategies
# =============================================================================

STRATEGIES = [
    # atm_straddle,
    # atm_straddle_index_move,
    # blueprint_strangle,
    # daily_put_sell,
    smoke_test_strangle,
]

# =============================================================================
# Corruption Helpers
# =============================================================================

def _is_corrupt_file(path: str) -> bool:
    """Check if a file is corrupted (e.g. filled with null bytes after power loss)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(512)
        if not chunk:
            return True
        # File filled with null bytes — OS allocated space but write buffer was lost
        if chunk == b"\x00" * len(chunk):
            return True
        return False
    except Exception:
        return True


def _quarantine_file(path: str) -> None:
    """Move a corrupt file aside so it doesn't block startup forever."""
    try:
        ts = int(time.time())
        quarantine = f"{path}.corrupt.{ts}"
        shutil.move(path, quarantine)
        logger.warning(f"Quarantined corrupt file: {path} → {quarantine}")
    except Exception as e:
        logger.error(f"Failed to quarantine {path}: {e}")


def _check_exchange_positions(ctx):
    """Query exchange for open positions.  Returns list or None on failure."""
    try:
        positions = ctx.account_manager.get_positions(force_refresh=True)
        return [p for p in positions if float(p.get("qty", 0)) != 0]
    except Exception as e:
        logger.error(f"Cannot reach exchange to verify positions: {e}")
        return None


def _handle_corrupt_snapshot(ctx, snapshot_file: str) -> int:
    """Recovery path when trades_snapshot.json is corrupt or unparseable.

    Checks exchange for actual open positions, quarantines the corrupt
    file, and returns 0 (start fresh) instead of None (crash-loop).
    """
    open_positions = _check_exchange_positions(ctx)

    _quarantine_file(snapshot_file)

    if open_positions is None:
        # Can't reach exchange — still start fresh rather than crash-loop.
        # Positions will be detected on the next successful poll.
        logger.warning(
            "Starting fresh despite exchange check failure — "
            "monitor positions manually"
        )
    elif open_positions:
        symbols = ", ".join(p.get("symbol", "?") for p in open_positions)
        logger.critical(
            f"Exchange has {len(open_positions)} open position(s) but "
            f"trades_snapshot.json was corrupt. Starting fresh — "
            f"MANUAL ATTENTION REQUIRED: {symbols}"
        )
    else:
        logger.info(
            "Exchange confirms NO open positions — safe to start fresh"
        )

    return 0


# =============================================================================
# Trade Recovery
# =============================================================================

def _recover_trades(ctx, runners):
    """Attempt to recover active trades from the persisted snapshot.

    Called on every startup.  If the snapshot contains active trades
    (OPEN, OPENING, PENDING_CLOSE, CLOSING), reconstructs TradeLifecycle
    objects, re-attaches exit conditions from the matching strategy
    configs, and verifies positions on the exchange.

    Additionally loads the order ledger (active_orders.json) so the
    OrderManager knows about in-flight orders from the previous session.

    This is idempotent: a clean snapshot with no active trades simply
    returns 0.  The crash-flag file is no longer used — the snapshot
    combined with exchange verification is the source of truth.

    Returns:
        Number of active trades recovered, or None on critical failure.
    """
    # ── Step 1: Load order ledger (before trade restore) ────────────────
    # This populates OrderManager so it can track orders from the previous
    # session.  If the file doesn't exist, that's fine — clean start.
    try:
        order_manager = ctx.lifecycle_manager.order_manager
        order_manager.load_snapshot()
        # Poll every recovered order for its true exchange state
        order_manager.poll_all()
        logger.info("Order ledger loaded and polled")
    except Exception as e:
        # Non-fatal: we can still recover trades without the order ledger.
        # Orders from the previous session will be "forgotten" and the
        # PENDING_CLOSE guard may place fresh close orders, but reduce_only
        # and the circuit breaker provide defence in depth.
        logger.warning(f"Order ledger recovery failed (non-fatal): {e}")

    # ── Step 2: Load trade snapshot ─────────────────────────────────────
    snapshot_file = "logs/trades_snapshot.json"
    if not os.path.exists(snapshot_file):
        logger.warning("No trades snapshot found — nothing to recover")
        return 0

    # ── Step 2a: Detect file corruption (null bytes from power loss) ────
    if _is_corrupt_file(snapshot_file):
        logger.critical(
            "trades_snapshot.json is CORRUPT (null bytes / empty) — "
            "likely caused by a hard reboot or power loss"
        )
        return _handle_corrupt_snapshot(ctx, snapshot_file)

    try:
        with open(snapshot_file, "r") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.error(
            f"Failed to parse trades snapshot: {e} — "
            f"treating as corrupt and applying recovery logic"
        )
        return _handle_corrupt_snapshot(ctx, snapshot_file)

    trades_data = snapshot.get("trades", [])
    if not trades_data:
        return 0

    # Map strategy_id → runner for exit-condition re-attachment
    runner_map = {r.strategy_id: r for r in runners}

    # Fetch live exchange positions for verification
    try:
        exchange_positions = ctx.account_manager.get_positions(force_refresh=True)
        exchange_symbols = {
            p["symbol"] for p in exchange_positions
            if float(p.get("qty", 0)) != 0
        }
        logger.info(f"Exchange has {len(exchange_symbols)} open position(s)")
    except Exception as e:
        logger.error(f"Failed to fetch exchange positions for recovery: {e}")
        return None

    recovered_active = 0

    for td in trades_data:
        state_str = td.get("state", "")
        trade_id = td.get("id", "?")

        # ── Completed trades: restore for counters (max_trades_per_day) ──
        if state_str in ("closed", "failed"):
            trade = TradeLifecycle.from_dict(td)
            ctx.lifecycle_manager.restore_trade(trade)
            continue

        # ── PENDING_OPEN: no orders placed, safe to skip ────────────────
        if state_str == "pending_open":
            logger.info(f"Skipping PENDING_OPEN trade {trade_id} (no orders placed)")
            continue

        # ── Active trades: OPEN, OPENING, PENDING_CLOSE, CLOSING ────────
        strategy_id = td.get("strategy_id")
        runner = runner_map.get(strategy_id)
        if not runner:
            logger.error(
                f"Cannot recover trade {trade_id}: "
                f"no registered strategy '{strategy_id}'"
            )
            return None

        trade = TradeLifecycle.from_dict(td)

        # Re-attach exit conditions from the strategy config
        trade.exit_conditions = list(runner.config.exit_conditions)

        # ── OPENING: orders placed but not all filled ────────────────────
        # Orders may be live on the exchange.  Use OrderManager records
        # (already loaded + polled) to determine fill status per leg.
        # This avoids confusion with positions from other applications.
        if trade.state == TradeState.OPENING:
            logger.info(f"Recovering OPENING trade {trade.id} — checking order status")

            # Poll all orders for this trade to get latest fill info
            from order_manager import OrderPurpose
            open_orders = order_manager.get_all_orders(trade.id, purpose=OrderPurpose.OPEN_LEG)

            # Cancel any still-live orders for this trade
            cancelled = order_manager.cancel_all_for(trade.id)
            if cancelled:
                logger.info(f"Trade {trade.id}: cancelled {cancelled} pending order(s) on exchange")

            # Determine which legs had fills by checking order records.
            # A requote chain means multiple orders per leg — sum fills
            # across the chain for each leg_index.
            leg_fills: dict = {}  # leg_index → (total_filled_qty, last_fill_price)
            for rec in open_orders:
                idx = rec.leg_index
                prev_qty, prev_price = leg_fills.get(idx, (0.0, None))
                if rec.filled_qty > 0:
                    leg_fills[idx] = (prev_qty + rec.filled_qty, rec.avg_fill_price or rec.price)

            filled_legs = []
            all_legs_count = len(trade.open_legs)
            for i, leg in enumerate(trade.open_legs):
                fill_qty, fill_price = leg_fills.get(i, (0.0, None))
                if fill_qty >= leg.qty:
                    leg.filled_qty = leg.qty
                    leg.fill_price = fill_price or leg.fill_price
                    filled_legs.append(leg)

            if len(filled_legs) == all_legs_count:
                # All legs fully filled → promote to OPEN
                trade.state = TradeState.OPEN
                trade.opened_at = trade.opened_at or time.time()
                logger.info(f"Trade {trade.id}: all {len(filled_legs)} legs filled via OrderManager → OPEN")
            elif filled_legs:
                # Partial fill — unwind filled legs
                trade.open_legs = filled_legs
                trade.state = TradeState.OPEN
                trade.opened_at = time.time()
                trade.state = TradeState.PENDING_CLOSE
                logger.info(
                    f"Trade {trade.id}: {len(filled_legs)}/{all_legs_count} legs filled "
                    f"— unwinding via PENDING_CLOSE"
                )
            else:
                # No fills — clean discard
                trade.state = TradeState.FAILED
                trade.error = "Crashed during opening — no fills"
                logger.info(f"Trade {trade.id}: no fills in OrderManager → FAILED")

            ctx.lifecycle_manager.restore_trade(trade)
            recovered_active += 1
            continue

        # ── OPEN, PENDING_CLOSE, CLOSING: positions must exist ───────────
        # Verify all open legs still have positions on the exchange
        for leg in trade.open_legs:
            if leg.symbol not in exchange_symbols:
                logger.error(
                    f"Cannot recover trade {trade.id}: "
                    f"position {leg.symbol} not found on exchange"
                )
                return None

        # Normalize CLOSING to safe resume point
        if trade.state == TradeState.CLOSING:
            # Close orders died with the process → retry from PENDING_CLOSE
            trade.state = TradeState.PENDING_CLOSE
            trade.close_legs = []

        ctx.lifecycle_manager.restore_trade(trade)
        recovered_active += 1

    # Pre-populate _known_closed_ids and _known_open_ids so runners don't re-fire callbacks
    for r in runners:
        for trade in r.all_trades:
            if trade.state in (TradeState.CLOSED, TradeState.FAILED):
                r._known_closed_ids.add(trade.id)
            if trade.state == TradeState.OPEN:
                r._known_open_ids.add(trade.id)

    # ── Step 3: Reconcile order ledger against exchange ─────────────────
    # Flags any orphaned orders (on exchange but not in our ledger) or
    # stale ledger entries (in ledger but not on exchange).
    try:
        exchange_open_orders = ctx.account_manager.get_open_orders(force_refresh=True)
        if exchange_open_orders is not None:
            warnings = order_manager.reconcile(exchange_open_orders)
            if warnings:
                for w in warnings:
                    logger.warning(f"Order reconciliation: {w}")
    except Exception as e:
        logger.warning(f"Order reconciliation skipped: {e}")

    return recovered_active


# =============================================================================
# Main
# =============================================================================

def main():
    """Start the trading system with error isolation and graceful recovery."""
    logger.info("=" * 60)
    logger.info("CoincallTrader starting")
    logger.info("=" * 60)

    try:
        ctx = build_context(poll_interval=10)
        logger.info(f"Context built — {ctx.auth.base_url}")
    except Exception as e:
        logger.error(f"Failed to build context: {e}", exc_info=True)
        print(f"\n✗ FATAL: Could not initialize — {e}")
        sys.exit(1)

    # ── Initialize persistence and health check ─────────────────────────
    persistence = TradeStatePersistence()
    ctx.persistence = persistence  # Wire into TradingContext for trade history logging

    health_checker = HealthChecker(
        check_interval=300,  # 5 minutes
        account_snapshot_fn=lambda: ctx.position_monitor.snapshot(),
        market_data=ctx.market_data,
    )

    # ── Register strategies ──────────────────────────────────────────────
    runners: list = []

    for factory in STRATEGIES:
        try:
            result = factory()
            configs = result if isinstance(result, list) else [result]
            for config in configs:
                runner = StrategyRunner(config, ctx)
                ctx.position_monitor.on_update(runner.tick)
                runners.append(runner)
                # Post-creation hook (e.g. multi-day state attachment)
                on_created = config.metadata.get("on_runner_created")
                if callable(on_created):
                    on_created(runner)
                logger.info(f"Strategy registered: {config.name}")
        except Exception as e:
            logger.error(f"Failed to register strategy {factory.__name__}: {e}", exc_info=True)
            print(f"✗ Warning: Could not load strategy {factory.__name__} — {e}")
            # Don't exit — continue with other strategies

    if not runners:
        logger.error("No strategies registered — exiting")
        print("\n✗ FATAL: No valid strategies to run")
        sys.exit(1)

    # ── Trade Recovery (idempotent — runs on every startup) ────────────
    recovered = _recover_trades(ctx, runners)
    if recovered is None:
        logger.error("CRITICAL: State recovery failed — manual intervention required")
        print("\n✗ CRITICAL: Could not recover trades. Check logs and positions.")
        sys.exit(1)
    elif recovered > 0:
        logger.info(f"Successfully recovered {recovered} active trade(s)")

    # ── Start services ────────────────────────────────────────────────────
    try:
        ctx.position_monitor.start()
        logger.info(
            f"Position monitor started (interval={ctx.position_monitor._poll_interval}s) "
            f"— press Ctrl+C to stop"
        )
        
        health_checker.start()
        logger.info("Health checker started (interval=5m)")

        start_dashboard(ctx, runners)
    except Exception as e:
        logger.error(f"Failed to start services: {e}", exc_info=True)
        print(f"\n✗ FATAL: Could not start services — {e}")
        sys.exit(1)

    def shutdown(sig=None, frame=None):
        logger.info("Shutting down...")
        try:
            # Persist current state before anything else — critical for crash recovery
            ctx.lifecycle_manager._persist_all_trades()
            order_manager = ctx.lifecycle_manager.order_manager
            order_manager.persist_snapshot()

            # Stop health checker first
            health_checker.stop()
            
            # Close all strategies
            for r in runners:
                r.stop()
            
            ctx.position_monitor.stop()
            logger.info("Shutdown complete")
        except Exception as e:
            logger.error(f"Error during shutdown: {e}", exc_info=True)
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # ── Main Event Loop with Error Isolation ────────────────────────────
    logger.info("Main event loop started — monitoring strategies")
    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while True:
            try:
                time.sleep(10)

                # Log health status periodically
                active_count = sum(1 for r in runners if r.active_trades)
                if active_count > 0:
                    logger.debug(f"Health check: {active_count} active trades")

                # Log when all runners have finished their daily quota
                if all(r.is_done for r in runners):
                    logger.debug("All strategies done for today — waiting for next UTC day")
                
                consecutive_errors = 0  # Reset on successful iteration
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    f"Main loop error (iteration {consecutive_errors}/{max_consecutive_errors}): {e}",
                    exc_info=True
                )
                
                if consecutive_errors >= max_consecutive_errors:
                    logger.error(
                        f"Too many consecutive errors ({consecutive_errors}) — exiting"
                    )
                    print(f"\n✗ FATAL: Main loop failed {max_consecutive_errors} times — exiting")
                    shutdown()
                
                # Back off slightly before retrying
                time.sleep(5)
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        shutdown()


if __name__ == "__main__":
    main()