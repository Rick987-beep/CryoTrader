#!/usr/bin/env python3
"""
CoincallTrader — Main Entry Point (Launcher)

Wires all services via TradingContext, registers strategies, and runs
the position monitor loop.  Strategy definitions live in strategies/.

Usage:
    python main.py
"""

import logging
import os
import signal
import sys
import time

from strategy import build_context, StrategyRunner
from strategies import blueprint_strangle, reverse_iron_condor_live, long_strangle_pnl_test
from persistence import TradeStatePersistence
from health_check import HealthChecker
from telegram_notifier import TelegramNotifier
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
    blueprint_strangle,
    # long_strangle_pnl_test,
    # reverse_iron_condor_live,
]


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
        notifier=notifier,
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

    # ── Start services ────────────────────────────────────────────────────
    try:
        ctx.position_monitor.start()
        logger.info(
            f"Position monitor started (interval={ctx.position_monitor._poll_interval}s) "
            f"— press Ctrl+C to stop"
        )
        
        health_checker.start()
        logger.info("Health checker started (interval=5m)")

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
            
            # Save final trade state
            all_active_trades = []
            for r in runners:
                all_active_trades.extend(r.active_trades)
            if all_active_trades:
                persistence.save_trades(all_active_trades)
            
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
    last_persistence_save = time.time()

    try:
        while True:
            try:
                time.sleep(10)
                
                # Check if all strategies are complete
                if runners and all(
                    not r._enabled and not r.active_trades for r in runners
                ):
                    logger.info("All strategies completed — auto-shutting down")
                    print("\n✓ All strategies completed — shutting down cleanly")
                    shutdown()
                
                # Periodically save trade state (every 60 seconds)
                now = time.time()
                if now - last_persistence_save > 60:
                    try:
                        all_active_trades = []
                        for r in runners:
                            all_active_trades.extend(r.active_trades)
                        if all_active_trades:
                            persistence.save_trades(all_active_trades)
                        last_persistence_save = now
                    except Exception as e:
                        logger.error(f"Failed to save trade state: {e}")
                
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