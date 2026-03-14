#!/usr/bin/env bash
# ===========================================================================
# CoincallTrader — One-Time Server Setup (Ubuntu 24.04)
#
# Run this ONCE on a fresh Hetzner VPS to prepare it for deployments.
# It is executed remotely by deploy.sh --setup, or you can run it manually:
#
#   ssh root@YOUR_VPS_IP < deployment/server-setup.sh
#
# What it does:
#   1. Updates system packages
#   2. Installs Python 3, pip, venv
#   3. Creates /opt/coincalltrader with venv
#   4. Installs the systemd service
#   5. Configures UFW firewall (SSH + dashboard)
#   6. Creates logs directory
#
# Safe to re-run — all steps are idempotent.
# ===========================================================================
set -euo pipefail

APP_DIR="/opt/coincalltrader"
SERVICE_NAME="coincalltrader"
DASHBOARD_PORT=8080

echo "═══════════════════════════════════════════════════════"
echo " CoincallTrader — Server Setup"
echo "═══════════════════════════════════════════════════════"

# ── 1) System update ────────────────────────────────────────────────────
echo ""
echo "▸ Updating system packages..."
apt-get update -qq
apt-get upgrade -y -qq
echo "  ✓ System up to date"

# ── 2) Install Python & essentials ─────────────────────────────────────
echo ""
echo "▸ Installing Python and build tools..."
apt-get install -y -qq python3 python3-pip python3-venv rsync
echo "  ✓ Python $(python3 --version 2>&1 | awk '{print $2}') ready"

# ── 3) Create application directory ────────────────────────────────────
echo ""
echo "▸ Setting up application directory..."
mkdir -p "$APP_DIR/logs"
echo "  ✓ $APP_DIR created"

# ── 4) Create Python virtual environment ───────────────────────────────
echo ""
echo "▸ Creating Python virtual environment..."
if [ ! -d "$APP_DIR/.venv" ]; then
    python3 -m venv "$APP_DIR/.venv"
    echo "  ✓ Virtual environment created"
else
    echo "  ✓ Virtual environment already exists"
fi

# Upgrade pip inside the venv
"$APP_DIR/.venv/bin/pip" install --upgrade pip -q
echo "  ✓ pip upgraded"

# ── 5) Install systemd service ─────────────────────────────────────────
echo ""
echo "▸ Installing systemd service..."

# The service file is deployed via rsync to $APP_DIR/deployment/
# on the first deploy.  For initial setup, we create a minimal placeholder
# that will be overwritten by the real one.
if [ ! -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<'UNIT'
[Unit]
Description=CoincallTrader — Options Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/coincalltrader
ExecStart=/opt/coincalltrader/.venv/bin/python main.py
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=coincalltrader
LimitNOFILE=65535
MemoryMax=1G
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/opt/coincalltrader/logs
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
UNIT
    echo "  ✓ Service file installed"
else
    echo "  ✓ Service file already exists"
fi

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" 2>/dev/null || true
echo "  ✓ Service enabled (will start on boot)"

# ── 6) Configure firewall ──────────────────────────────────────────────
echo ""
echo "▸ Configuring firewall (ufw)..."
apt-get install -y -qq ufw

# Ensure SSH is allowed BEFORE enabling the firewall
ufw allow OpenSSH >/dev/null 2>&1

# Allow dashboard port
ufw allow "$DASHBOARD_PORT/tcp" >/dev/null 2>&1

# Enable if not already active
if ! ufw status | grep -q "Status: active"; then
    echo "y" | ufw enable >/dev/null 2>&1
fi

echo "  ✓ Firewall active — SSH + port $DASHBOARD_PORT open"

# ── 7) Summary ──────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════"
echo " Setup complete!"
echo ""
echo "   App dir:    $APP_DIR"
echo "   Python:     $(python3 --version 2>&1)"
echo "   Venv pip:   $($APP_DIR/.venv/bin/pip --version 2>&1 | awk '{print $2}')"
echo "   Service:    $SERVICE_NAME (enabled, not yet started)"
echo "   Firewall:   SSH + port $DASHBOARD_PORT"
echo ""
echo " Next steps:"
echo "   1. Copy your .env file to the server:"
echo "      scp .env root@\$(hostname -I | awk '{print \$1}'):$APP_DIR/.env"
echo "   2. Run deploy.sh from your dev machine to sync code & start"
echo "═══════════════════════════════════════════════════════"
