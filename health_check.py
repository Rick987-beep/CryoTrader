#!/usr/bin/env python3
"""
Health Check Module — Observability Only

Logs system health status every 5 minutes.
Pure observability: no restart logic, no notifications.
Process supervision is handled by NSSM; daily summary by TelegramNotifier.

Provides visibility into:
- Uptime tracking
- Account equity/margin
- Active positions
- Warning escalation for high margin / low equity
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Callable

logger = logging.getLogger(__name__)


class HealthChecker:
    """Logs system health at regular intervals. Observability only — no side effects."""

    def __init__(self, check_interval: int = 300, account_snapshot_fn: Optional[Callable] = None):
        """
        Initialize health checker.

        Args:
            check_interval: Interval between health checks in seconds (default 5 min = 300s)
            account_snapshot_fn: Function to call for latest account snapshot
        """
        self.check_interval = check_interval
        self.account_snapshot_fn = account_snapshot_fn
        self._running = False
        self._thread = None
        self._start_time = time.time()

    def set_account_snapshot_fn(self, fn: Callable) -> None:
        """Set the function to fetch account snapshots."""
        self.account_snapshot_fn = fn

    def start(self) -> None:
        """Start background health check thread."""
        if self._running:
            logger.warning("HealthChecker already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._check_loop,
            name="HealthChecker",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"HealthChecker started (interval={self.check_interval}s)")

    def stop(self) -> None:
        """Stop health check thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=self.check_interval + 2)
            self._thread = None
        logger.info("HealthChecker stopped")

    def _check_loop(self) -> None:
        """Background loop: periodic health checks."""
        while self._running:
            try:
                self._log_health_status()
            except Exception as e:
                logger.error(f"Health check error: {e}", exc_info=True)

            # Sleep in small increments so stop() is responsive
            for _ in range(self.check_interval * 10):
                if not self._running:
                    return
                time.sleep(0.1)

    def _log_health_status(self) -> None:
        """Log current health status. Uses DEBUG for normal checks, WARNING for problems."""
        uptime_secs = int(time.time() - self._start_time)
        uptime_str = self._format_uptime(uptime_secs)

        status_lines = [
            f"═══════════════════════════════════════════════════════════════════",
            f"HEALTH CHECK — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC",
            f"═══════════════════════════════════════════════════════════════════",
            f"Uptime: {uptime_str}",
        ]

        log_level = logging.DEBUG  # default: quiet unless problems detected

        # Try to get account snapshot
        if self.account_snapshot_fn:
            try:
                snapshot = self.account_snapshot_fn()
                if snapshot:
                    status_lines.extend([
                        f"Equity: ${snapshot.equity:,.2f}",
                        f"Available Margin: ${snapshot.available_margin:,.2f}",
                        f"Margin Utilization: {snapshot.margin_utilization:.1f}%",
                        f"Net Delta: {snapshot.net_delta:+.4f}",
                        f"Open Positions: {snapshot.position_count}",
                    ])
                    # Escalate to WARNING if margin is high or equity is very low
                    if snapshot.margin_utilization > 80:
                        log_level = logging.WARNING
                        status_lines.append("⚠ HIGH MARGIN UTILIZATION")
                    if snapshot.equity < 100:
                        log_level = logging.WARNING
                        status_lines.append("⚠ LOW EQUITY")

                else:
                    log_level = logging.WARNING
                    status_lines.append("⚠ Account snapshot returned None")
            except Exception as e:
                log_level = logging.WARNING
                status_lines.append(f"⚠ Account snapshot failed: {e}")
        else:
            status_lines.append("Account snapshot: (not configured)")

        # Check BTC index price freshness
        try:
            from market_data import get_btc_index_price
            idx_price = get_btc_index_price(use_cache=False)
            if idx_price is not None:
                status_lines.append(f"BTC Index Price: ${idx_price:,.2f}")
            else:
                log_level = logging.WARNING
                status_lines.append("⚠ BTC INDEX PRICE UNAVAILABLE")
        except Exception as e:
            log_level = logging.WARNING
            status_lines.append(f"⚠ BTC index price check failed: {e}")

        status_lines.append(f"═══════════════════════════════════════════════════════════════════")

        # Log at DEBUG normally, WARNING only when something looks wrong
        logger.log(log_level, "\n" + "\n".join(status_lines))

    @staticmethod
    def _format_uptime(seconds: int) -> str:
        """Format uptime as human-readable string."""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60

        parts = []
        if days > 0:
            parts.append(f"{days}d")
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0:
            parts.append(f"{minutes}m")
        if secs > 0 or not parts:
            parts.append(f"{secs}s")

        return " ".join(parts)
