#!/usr/bin/env python3
"""
Web Dashboard Module

Lightweight Flask + htmx dashboard for CoincallTrader.
Runs on a daemon thread inside the existing process — reads TradingContext
and StrategyRunner state directly, no IPC needed.

Features:
  - Account summary (equity, margin, Greeks)
  - Strategy status cards with Pause / Resume / Stop controls
  - Open positions table
  - Live log tail
  - Kill switch (aggressive close-all)

Setup:
  Add to .env:
    DASHBOARD_PASSWORD=your_secret     (required — dashboard is disabled without it)
    DASHBOARD_PORT=8080                (optional, default 8080)

Usage (wired automatically via main.py):
    from dashboard import start_dashboard
    start_dashboard(ctx, runners, host="0.0.0.0", port=8080)
"""

import logging
import os
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timezone
from functools import wraps
from typing import TYPE_CHECKING, List, Optional

from flask import Flask, Response, redirect, render_template, request, session, url_for
from position_closer import PositionCloser

if TYPE_CHECKING:
    from strategy import StrategyRunner, TradingContext

logger = logging.getLogger(__name__)

# Maximum log lines to keep in memory for the live tail
_LOG_TAIL_LINES = 200


# =============================================================================
# In-memory log handler — captures recent log entries for the dashboard
# =============================================================================

class DashboardLogHandler(logging.Handler):
    """Ring-buffer handler that keeps the last N log records for display."""

    def __init__(self, maxlen: int = _LOG_TAIL_LINES):
        super().__init__()
        self.records: deque = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(self.format(record))
        except Exception:
            pass  # Never break application logging


# Singleton handler — attached to root logger on first start
_log_handler: Optional[DashboardLogHandler] = None


def _get_log_lines(n: int = 80) -> List[str]:
    """Return the last *n* formatted log lines."""
    if _log_handler is None:
        return ["(log capture not initialised)"]
    return list(_log_handler.records)[-n:]


# =============================================================================
# Flask App Factory
# =============================================================================

def _create_app(
    ctx: "TradingContext",
    runners: List["StrategyRunner"],
    password: str,
) -> Flask:
    """Build and return the Flask application."""

    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    )
    app.secret_key = secrets.token_hex(32)

    from config import ENVIRONMENT

    # ── Auth helpers ─────────────────────────────────────────────────────

    def login_required(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("authenticated"):
                if request.headers.get("HX-Request"):
                    return Response("Unauthorized", status=401)
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    # ── Pages ────────────────────────────────────────────────────────────

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if request.form.get("password") == password:
                session["authenticated"] = True
                return redirect(url_for("index"))
            error = "Invalid password"
        return render_template("login.html", error=error)

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def index():
        return render_template(
            "dashboard.html",
            environment=ENVIRONMENT.upper(),
        )

    # ── htmx Fragment Endpoints ──────────────────────────────────────────

    @app.route("/api/account")
    @login_required
    def api_account():
        snap = ctx.position_monitor.latest
        if not snap:
            return "<p class='muted'>Waiting for first snapshot...</p>"

        ts = datetime.fromtimestamp(snap.timestamp, tz=timezone.utc).strftime("%H:%M:%S UTC")
        return render_template("_account.html", snap=snap, ts=ts)

    @app.route("/api/strategies")
    @login_required
    def api_strategies():
        snap = ctx.position_monitor.latest
        strategy_data = []
        for r in runners:
            strategy_data.append({
                "name": r.config.name,
                "enabled": r._enabled,
                "active_trades": len(r.active_trades),
                "max_trades": r.config.max_concurrent_trades,
                "stats": r.stats,
                "status_lines": r.status(snap).split("\n") if snap else [],
            })
        return render_template("_strategies.html", strategies=strategy_data)

    @app.route("/api/positions")
    @login_required
    def api_positions():
        snap = ctx.position_monitor.latest
        if not snap or not snap.positions:
            return "<p class='muted'>No open positions</p>"
        return render_template("_positions.html", positions=snap.positions)

    @app.route("/api/orders")
    @login_required
    def api_orders():
        om = ctx.lifecycle_manager._order_manager
        engine = ctx.lifecycle_manager

        # Collect live (non-terminal) orders, sorted newest first
        live_orders = sorted(
            [r for r in om._orders.values() if r.is_live],
            key=lambda r: r.placed_at,
            reverse=True,
        )

        # Recent terminal orders (last 10, most recent first)
        recent_terminal = sorted(
            [r for r in om._orders.values() if r.is_terminal],
            key=lambda r: (r.terminal_at or r.placed_at),
            reverse=True,
        )[:10]

        recon_warnings = engine.last_reconciliation_warnings
        recon_time = engine.last_reconciliation_time

        return render_template(
            "_orders.html",
            live_orders=live_orders,
            recent_terminal=recent_terminal,
            recon_warnings=recon_warnings,
            recon_time=recon_time,
            now=time.time(),
        )

    @app.route("/api/logs")
    @login_required
    def api_logs():
        lines = _get_log_lines(80)
        return render_template("_logs.html", lines=lines)

    # ── Control Endpoints ────────────────────────────────────────────────

    def _find_runner(name: str):
        for r in runners:
            if r.config.name == name:
                return r
        return None

    @app.route("/api/strategy/<name>/pause", methods=["POST"])
    @login_required
    def strategy_pause(name: str):
        r = _find_runner(name)
        if r:
            r.disable()
            logger.info(f"[Dashboard] Strategy '{name}' paused by user")
        return api_strategies()

    @app.route("/api/strategy/<name>/resume", methods=["POST"])
    @login_required
    def strategy_resume(name: str):
        r = _find_runner(name)
        if r:
            r.enable()
            logger.info(f"[Dashboard] Strategy '{name}' resumed by user")
        return api_strategies()

    @app.route("/api/strategy/<name>/stop", methods=["POST"])
    @login_required
    def strategy_stop(name: str):
        r = _find_runner(name)
        if r:
            r.stop()
            logger.info(f"[Dashboard] Strategy '{name}' stopped by user")
        return api_strategies()

    # ── Kill switch (two-phase mark-price close) ────────────────────────

    closer = PositionCloser(
        account_manager=ctx.account_manager,
        executor=ctx.executor,
        lifecycle_manager=ctx.lifecycle_manager,
    )

    @app.route("/api/killswitch", methods=["POST"])
    @login_required
    def killswitch():
        """Activate kill switch — close all positions via two-phase mark-price."""
        if closer.is_running:
            return (
                f'<span class="kill-result">'
                f'Kill switch already running — {closer.status}'
                f'</span>'
            )

        closer.start(runners)
        logger.warning("[Dashboard] KILL SWITCH activated by user")
        return (
            '<span class="kill-result">'
            'Kill switch activated — closing positions (check Telegram for progress)'
            '</span>'
        )

    @app.route("/api/killswitch/status")
    @login_required
    def killswitch_status():
        """Poll kill switch progress."""
        return f'<span class="kill-status">{closer.status}</span>'

    return app


# =============================================================================
# Startup
# =============================================================================

def start_dashboard(
    ctx: "TradingContext",
    runners: List["StrategyRunner"],
    host: str = "0.0.0.0",
    port: int = 8080,
) -> None:
    """
    Launch the dashboard on a daemon thread.

    Reads DASHBOARD_PASSWORD from the environment.  If not set, the
    dashboard is disabled and a warning is logged.
    """
    global _log_handler

    password = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if not password:
        logger.warning(
            "Dashboard disabled — set DASHBOARD_PASSWORD in .env to enable"
        )
        return

    port = int(os.getenv("DASHBOARD_PORT", str(port)))

    # Attach in-memory log handler to root logger
    _log_handler = DashboardLogHandler(maxlen=_LOG_TAIL_LINES)
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s  %(levelname)-7s  %(name)s — %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger().addHandler(_log_handler)

    app = _create_app(ctx, runners, password)

    def _run():
        # Suppress Flask/Werkzeug startup banner noise
        wlog = logging.getLogger("werkzeug")
        wlog.setLevel(logging.WARNING)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    thread = threading.Thread(target=_run, name="Dashboard", daemon=True)
    thread.start()
    logger.info(f"Dashboard started on http://{host}:{port}")
