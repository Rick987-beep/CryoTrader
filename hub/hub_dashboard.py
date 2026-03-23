#!/usr/bin/env python3
"""
CoincallTrader — Hub Dashboard

Unified dashboard that aggregates all trading slots into a single view.
Reads slot state from the filesystem (logs/) and sends control commands
to each slot's minimal control endpoint on localhost.

Environment variables (.env or .env.hub):
  HUB_PASSWORD         Required — dashboard login password
  HUB_PORT             Optional — default 8080
  HUB_SLOTS_BASE       Optional — default /opt/ct (base dir for slots)

Usage:
  python hub_dashboard.py
"""

import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for

import requests as http_requests

load_dotenv()

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

# =============================================================================
# Configuration
# =============================================================================

HUB_PASSWORD = os.getenv("HUB_PASSWORD", "").strip()
HUB_PORT = int(os.getenv("HUB_PORT", "8080"))
SLOTS_BASE = Path(os.getenv("HUB_SLOTS_BASE", "/opt/ct"))

# Slot port mapping: slot-01 → 8081, slot-02 → 8082, etc.
SLOT_PORT_BASE = 8080


def slot_port(slot_id: str) -> int:
    """Control endpoint port for a given slot (01 → 8081, etc.)."""
    return SLOT_PORT_BASE + int(slot_id)


def discover_slots() -> List[Dict]:
    """
    Discover active slots by scanning /opt/ct/slot-* directories.
    Returns list of dicts with slot metadata.
    """
    slots = []
    for d in sorted(SLOTS_BASE.glob("slot-*")):
        if not d.is_dir():
            continue
        slot_id = d.name.replace("slot-", "")
        env_file = d / ".env"

        # Read basic info from .env
        slot_name = ""
        exchange = ""
        environment = ""
        dashboard_port = slot_port(slot_id)
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key == "SLOT_NAME":
                    slot_name = val
                elif key == "EXCHANGE":
                    exchange = val
                elif key == "TRADING_ENVIRONMENT":
                    environment = val
                elif key == "DASHBOARD_PORT":
                    dashboard_port = int(val)

        slots.append({
            "id": slot_id,
            "name": slot_name or f"Slot {slot_id}",
            "dir": str(d),
            "exchange": exchange,
            "environment": environment,
            "port": dashboard_port,
        })
    return slots


def read_slot_trades(slot_dir: str) -> dict:
    """Read trades_snapshot.json from a slot's logs directory."""
    snap_path = Path(slot_dir) / "logs" / "trades_snapshot.json"
    if not snap_path.exists():
        return {"trades": []}
    try:
        data = json.loads(snap_path.read_text())
        return data if isinstance(data, dict) else {"trades": []}
    except (json.JSONDecodeError, OSError):
        return {"trades": []}


def read_slot_history(slot_dir: str, limit: int = 20) -> List[Dict]:
    """Read recent entries from trade_history.jsonl."""
    hist_path = Path(slot_dir) / "logs" / "trade_history.jsonl"
    if not hist_path.exists():
        return []
    try:
        lines = hist_path.read_text().strip().splitlines()
        entries = []
        for line in lines[-limit:]:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except OSError:
        return []


def query_slot_control(port: int, endpoint: str, method: str = "GET",
                       timeout: float = 3.0) -> Optional[Dict]:
    """
    Query a slot's control endpoint on localhost.
    Returns parsed JSON or None on failure.
    """
    url = f"http://127.0.0.1:{port}{endpoint}"
    try:
        if method == "POST":
            resp = http_requests.post(url, timeout=timeout)
        else:
            resp = http_requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


# =============================================================================
# Flask App
# =============================================================================

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
)
app.secret_key = secrets.token_hex(32)


# ── Auth ─────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.headers.get("HX-Request"):
                return Response("Unauthorized", status=401)
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == HUB_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Invalid password"
    return render_template("hub_login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ── Main page ────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("hub_dashboard.html")


# ── htmx API endpoints ──────────────────────────────────────────────────

@app.route("/api/overview")
@login_required
def api_overview():
    """Aggregate status from all slots."""
    slots = discover_slots()
    slot_data = []

    for s in slots:
        # Try to get live status from control endpoint
        live = query_slot_control(s["port"], "/control/status")
        trades = read_slot_trades(s["dir"])
        history = read_slot_history(s["dir"], limit=5)

        slot_info = {
            **s,
            "online": live is not None,
            "account_id": live.get("account_id", "") if live else "",
            "strategies": live.get("strategies", []) if live else [],
            "account": live.get("account") if live else None,
            "positions": live.get("positions", []) if live else [],
            "open_orders": live.get("open_orders", []) if live else [],
            "health": live.get("health") if live else None,
            "active_trades": len([t for t in trades.get("trades", []) if t.get("state") not in ("closed", "error")]),
            "recent_history": history,
        }
        slot_data.append(slot_info)

    return render_template("_hub_overview.html", slots=slot_data)


@app.route("/api/slot/<slot_id>/detail")
@login_required
def api_slot_detail(slot_id: str):
    """Detailed view for a specific slot."""
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return "<p class='muted'>Slot not found</p>"

    live = query_slot_control(slot["port"], "/control/status")
    trades = read_slot_trades(slot["dir"])
    history = read_slot_history(slot["dir"], limit=20)

    return render_template(
        "_hub_slot_detail.html",
        slot=slot,
        live=live,
        trades=trades.get("trades", []),
        history=history,
        positions=live.get("positions", []) if live else [],
        open_orders=live.get("open_orders", []) if live else [],
        health=live.get("health") if live else None,
        account_id=live.get("account_id", "") if live else "",
        now=time.time(),
    )


@app.route("/api/slot/<slot_id>/logs")
@login_required
def api_slot_logs(slot_id: str):
    """Fetch log lines for a specific slot (used by tabbed log viewer)."""
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"lines": []})
    live = query_slot_control(slot["port"], "/control/status")
    if not live:
        return jsonify({"lines": ["(slot offline)"]})
    return jsonify({"lines": live.get("logs", [])})


# ── Control endpoints (proxy to slots) ──────────────────────────────────

@app.route("/api/slot/<slot_id>/pause", methods=["POST"])
@login_required
def slot_pause(slot_id: str):
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"ok": False, "reason": "not_found"})
    result = query_slot_control(slot["port"], "/control/pause", method="POST")
    return jsonify(result or {"ok": False, "reason": "offline"})


@app.route("/api/slot/<slot_id>/resume", methods=["POST"])
@login_required
def slot_resume(slot_id: str):
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"ok": False, "reason": "not_found"})
    result = query_slot_control(slot["port"], "/control/resume", method="POST")
    return jsonify(result or {"ok": False, "reason": "offline"})


@app.route("/api/slot/<slot_id>/stop", methods=["POST"])
@login_required
def slot_stop(slot_id: str):
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"ok": False, "reason": "not_found"})
    result = query_slot_control(slot["port"], "/control/stop", method="POST")
    return jsonify(result or {"ok": False, "reason": "offline"})


@app.route("/api/slot/<slot_id>/kill", methods=["POST"])
@login_required
def slot_kill(slot_id: str):
    slots = discover_slots()
    slot = next((s for s in slots if s["id"] == slot_id), None)
    if not slot:
        return jsonify({"ok": False, "reason": "not_found"})
    result = query_slot_control(slot["port"], "/control/kill", method="POST")
    return jsonify(result or {"ok": False, "reason": "offline"})


# =============================================================================
# Main
# =============================================================================

if __name__ == "__main__":
    if not HUB_PASSWORD:
        print("ERROR: Set HUB_PASSWORD in .env or .env.hub")
        raise SystemExit(1)

    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"[Hub] Starting on http://0.0.0.0:{HUB_PORT}")
    app.run(host="0.0.0.0", port=HUB_PORT, debug=False)
