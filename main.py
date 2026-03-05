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
import signal
import sys
import time

from strategy import build_context, StrategyRunner
from trade_lifecycle import TradeLifecycle, TradeState
from strategies import blueprint_strangle, reverse_iron_condor_live, long_strangle_pnl_test, atm_straddle
from persistence import TradeStatePersistence
from health_check import HealthChecker
from telegram_notifier import TelegramNotifier
from dashboard import start_dashboard
from config import ENVIRONMENT

# =============================================================================
# Logging
# =============================================================================

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("logs/trading.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# =============================================================================
# Active Strategies
# =============================================================================

STRATEGIES = [
    atm_straddle,
    # blueprint_strangle,
    # long_strangle_pnl_test,
    # reverse_iron_condor_live,
]

# =============================================================================
# Trade Recovery
# =============================================================================

def _recover_trades(ctx, runners):
    """Attempt to recover active trades from the persisted snapshot.

    Called on every startup.  If the snapshot contains active trades
    (OPEN, OPENING, PENDING_CLOSE, CLOSING), reconstructs TradeLifecycle
    objects, re-attaches exit conditions from the matching strategy
    configs, and verifies positions on the exchange.

    This is idempotent: a clean snapshot with no active trades simply
    returns 0.  The crash-flag file is no longer used — the snapshot
    combined with exchange verification is the source of truth.

    Returns:
        Number of active trades recovered, or None on critical failure.
    """
    snapshot_file = "logs/trades_snapshot.json"
    if not os.path.exists(snapshot_file):
        logger.warning("No trades snapshot found — nothing to recover")
        return 0

    try:
        with open(snapshot_file, "r") as f:
            snapshot = json.load(f)
    except Exception as e:
        logger.error(f"Failed to parse trades snapshot: {e}")
        return None

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

        # Verify all open legs still have positions on the exchange
        for leg in trade.open_legs:
            if leg.symbol not in exchange_symbols:
                logger.error(
                    f"Cannot recover trade {trade.id}: "
                    f"position {leg.symbol} not found on exchange"
                )
                return None

        # Normalize transient states to safe resume points
        if trade.state == TradeState.OPENING:
            # Positions confirmed on exchange → treat as filled
            trade.state = TradeState.OPEN
            trade.opened_at = trade.opened_at or time.time()
            for leg in trade.open_legs:
                if leg.filled_qty == 0:
                    leg.filled_qty = leg.qty
        elif trade.state == TradeState.CLOSING:
            # Close orders died with the process → retry from PENDING_CLOSE
            trade.state = TradeState.PENDING_CLOSE
            trade.close_legs = []

        ctx.lifecycle_manager.restore_trade(trade)
        recovered_active += 1

    # Pre-populate _known_closed_ids so runners don't re-fire callbacks
    for r in runners:
        for trade in r.all_trades:
            if trade.state in (TradeState.CLOSED, TradeState.FAILED):
                r._known_closed_ids.add(trade.id)

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

    # ── Initialize persistence, health check, and notifications ──────────
    persistence = TradeStatePersistence()
    ctx.persistence = persistence  # Wire into TradingContext for trade history logging

    notifier = TelegramNotifier()
    ctx.notifier = notifier  # Wire into TradingContext for strategy notifications

    health_checker = HealthChecker(
        check_interval=300,  # 5 minutes
        account_snapshot_fn=lambda: ctx.position_monitor.snapshot(),
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
        notifier.notify_error(
            "CRITICAL: Trade recovery failed. "
            "Check exchange positions and logs manually."
        )
        print("\n✗ CRITICAL: Could not recover trades. Check logs and positions.")
        sys.exit(1)
    elif recovered > 0:
        logger.info(f"Successfully recovered {recovered} active trade(s)")
        notifier.notify_error(
            f"Trade recovery: restored {recovered} active trade(s). "
            f"Resuming normal operation."
        )

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

        notifier.notify_startup(ENVIRONMENT)
    except Exception as e:
        logger.error(f"Failed to start services: {e}", exc_info=True)
        print(f"\n✗ FATAL: Could not start services — {e}")
        sys.exit(1)

    def shutdown(sig=None, frame=None):
        logger.info("Shutting down...")
        try:
            notifier.notify_shutdown()

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

    def _maybe_send_daily_summary():
        """Trigger daily Telegram summary if due (date-gated inside notifier)."""
        try:
            snapshot = ctx.position_monitor.snapshot()
            if snapshot:
                notifier.maybe_send_daily_summary(
                    equity=snapshot.equity,
                    unrealized_pnl=snapshot.unrealized_pnl,
                    net_delta=snapshot.net_delta,
                    positions=snapshot.positions,
                )
        except Exception:
            pass  # Never let notification failure affect the main loop

    try:
        while True:
            try:
                time.sleep(10)

                # Daily Telegram summary (date-gated, at most once per day)
                _maybe_send_daily_summary()
                
                # Log health status periodically
                active_count = sum(1 for r in runners if r.active_trades)
                if active_count > 0:
                    logger.debug(f"Health check: {active_count} active trades")
                
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
                    notifier.notify_error(
                        f"Main loop failed {max_consecutive_errors} consecutive times — shutting down.\n"
                        f"Last error: {e}"
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