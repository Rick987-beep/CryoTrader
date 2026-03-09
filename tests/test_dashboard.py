#!/usr/bin/env python3
"""
Standalone dashboard test — no API keys, no strategies, just the web UI.

Usage:
    python3 test_dashboard.py

Then open http://localhost:8080 in your browser.
"""

import logging
import os
import sys
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

# Minimal stubs so dashboard.py can run without real services

@dataclass(frozen=True)
class PositionSnapshot:
    position_id: str = "pos-001"
    symbol: str = "BTCUSD-28MAR26-95000-C"
    qty: float = 0.5
    side: str = "short"
    entry_price: float = 0.0320
    mark_price: float = 0.0285
    unrealized_pnl: float = 1.75
    roi: float = 0.109
    delta: float = -0.23
    gamma: float = 0.0001
    theta: float = -0.0045
    vega: float = 0.12
    timestamp: float = 0.0


@dataclass(frozen=True)
class AccountSnapshot:
    equity: float = 12450.00
    available_margin: float = 8320.00
    initial_margin: float = 4130.00
    maintenance_margin: float = 2065.00
    unrealized_pnl: float = 3.50
    margin_utilization: float = 33.2
    positions: tuple = ()
    net_delta: float = -0.15
    net_gamma: float = 0.0002
    net_theta: float = -0.009
    net_vega: float = 0.24
    timestamp: float = 0.0

    @property
    def position_count(self):
        return len(self.positions)

    def get_position(self, symbol):
        for p in self.positions:
            if p.symbol == symbol:
                return p
        return None

    def summary_str(self):
        return f"Equity=${self.equity:.2f} UPnL=${self.unrealized_pnl:.2f}"


# Fake positions
_positions = (
    PositionSnapshot(
        position_id="pos-001",
        symbol="BTCUSD-28MAR26-95000-C",
        qty=0.5, side="short",
        entry_price=0.0320, mark_price=0.0285,
        unrealized_pnl=1.75, roi=0.109,
        delta=-0.23, gamma=0.0001, theta=-0.0045, vega=0.12,
        timestamp=time.time(),
    ),
    PositionSnapshot(
        position_id="pos-002",
        symbol="BTCUSD-28MAR26-85000-P",
        qty=0.5, side="short",
        entry_price=0.0180, mark_price=0.0195,
        unrealized_pnl=-0.75, roi=-0.083,
        delta=0.08, gamma=0.00008, theta=-0.0035, vega=0.10,
        timestamp=time.time(),
    ),
)

_snapshot = AccountSnapshot(
    equity=12450.00,
    available_margin=8320.00,
    initial_margin=4130.00,
    maintenance_margin=2065.00,
    unrealized_pnl=1.00,
    margin_utilization=33.2,
    positions=_positions,
    net_delta=-0.15,
    net_gamma=0.00018,
    net_theta=-0.008,
    net_vega=0.22,
    timestamp=time.time(),
)


class FakePositionMonitor:
    @property
    def latest(self):
        return _snapshot


class FakeLifecycleEngine:
    def force_close(self, trade_id):
        print(f"[FAKE] force_close({trade_id})")


class FakeNotifier:
    def send(self, msg):
        print(f"[FAKE TELEGRAM] {msg}")


class FakeContext:
    position_monitor = FakePositionMonitor()
    lifecycle_manager = FakeLifecycleEngine()
    notifier = FakeNotifier()


# Fake StrategyRunner
class FakeRunner:
    def __init__(self, name, enabled=True, active_trades_count=1):
        self._enabled = enabled
        self._name = name
        self._active_trades_count = active_trades_count
        self.config = type("C", (), {
            "name": name,
            "max_concurrent_trades": 2,
        })()

    @property
    def active_trades(self):
        # Return a list of fake trade objects
        class FakeTrade:
            id = "abc123"
        return [FakeTrade()] * self._active_trades_count

    @property
    def stats(self):
        return {
            "total": 5,
            "wins": 3,
            "losses": 2,
            "win_rate": 0.6,
            "total_pnl": 8.45,
            "avg_hold_seconds": 5400.0,
            "today_trades": 1,
            "today_pnl": 2.10,
        }

    def status(self, account=None):
        return f"Strategy: {self._name}\n  Enabled: {self._enabled}\n  Active trades: {self._active_trades_count}/2"

    def enable(self):
        self._enabled = True
        print(f"[FAKE] {self._name} enabled")

    def disable(self):
        self._enabled = False
        print(f"[FAKE] {self._name} disabled")

    def stop(self):
        self._enabled = False
        self._active_trades_count = 0
        print(f"[FAKE] {self._name} stopped")


def main():
    # Set up logging so the dashboard log tail has something to show
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler()],
    )
    logger = logging.getLogger("test_dashboard")

    # Patch config.ENVIRONMENT so dashboard doesn't try to load real .env
    import config
    # config is already loaded, ENVIRONMENT is set — no patching needed

    from dashboard import start_dashboard

    ctx = FakeContext()
    runners = [
        FakeRunner("blueprint_strangle", enabled=True, active_trades_count=1),
        FakeRunner("reverse_iron_condor", enabled=False, active_trades_count=0),
    ]

    logger.info("Starting dashboard in test mode with fake data...")
    start_dashboard(ctx, runners)

    logger.info("Dashboard should be running — open http://localhost:8080")
    logger.info(f"Password: (whatever you set in DASHBOARD_PASSWORD)")

    # Generate some fake log entries to populate the log tail
    def fake_logs():
        msgs = [
            "Position monitor tick — 2 positions, equity=$12,450.00",
            "blueprint_strangle: evaluating entry conditions...",
            "blueprint_strangle: time_window(8-20 UTC): hour=14 — ok",
            "Health check: 1 active trade(s)",
            "blueprint_strangle: trade abc123 OPEN hold=3600s PnL=+1.7500",
        ]
        i = 0
        while True:
            time.sleep(4)
            logger.info(msgs[i % len(msgs)])
            i += 1

    t = threading.Thread(target=fake_logs, daemon=True)
    t.start()

    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down test dashboard")


if __name__ == "__main__":
    main()
