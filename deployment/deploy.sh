#!/usr/bin/env bash
# ===========================================================================
# CoincallTrader — One-Button Deploy
#
# Syncs local code to the Ubuntu VPS, installs deps, and restarts the
# systemd service.  Run from your dev machine (macOS).
#
# Prerequisites:
#   - .deploy.env in the project root (see .deploy.env for template)
#   - SSH key access to the VPS
#   - Server set up via: ./deployment/deploy.sh --setup
#
# Usage:
#   ./deployment/deploy.sh              Full deploy (stop → sync → deps → start)
#   ./deployment/deploy.sh --dry-run    Preview what rsync would transfer
#   ./deployment/deploy.sh --setup      Run one-time server setup (first use)
#   ./deployment/deploy.sh --env        Copy local .env to the VPS
#   ./deployment/deploy.sh --stop       Stop the service
#   ./deployment/deploy.sh --start      Start the service
#   ./deployment/deploy.sh --restart    Restart the service
#   ./deployment/deploy.sh --status     Show service status
#   ./deployment/deploy.sh --logs       Tail live logs (Ctrl+C to stop)
#   ./deployment/deploy.sh --ssh        Open an SSH session to the VPS
#   ./deployment/deploy.sh --help       Show this help
# ===========================================================================
set -euo pipefail

# ── Resolve project paths ───────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Load deploy configuration ──────────────────────────────────────────
DEPLOY_ENV="$PROJECT_ROOT/.deploy.env"
if [[ ! -f "$DEPLOY_ENV" ]]; then
    echo "Error: $DEPLOY_ENV not found."
    echo "Create it from the template — see deployment/deploy.sh --help"
    exit 1
fi
# shellcheck disable=SC1090
source "$DEPLOY_ENV"

# Validate required settings
: "${VPS_HOST:?Set VPS_HOST in .deploy.env (e.g. root@123.45.67.89)}"
: "${VPS_APP_DIR:=/opt/coincalltrader}"
: "${VPS_SERVICE:=coincalltrader}"
: "${SSH_KEY:=}"

# ── SSH / rsync options ─────────────────────────────────────────────────
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
if [[ -n "$SSH_KEY" ]]; then
    SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# ── Terminal colours ────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'  # No colour

step()  { echo -e "\n${CYAN}▸ $1${NC}"; }
ok()    { echo -e "  ${GREEN}✓ $1${NC}"; }
warn()  { echo -e "  ${YELLOW}⚠ $1${NC}"; }
fail()  { echo -e "  ${RED}✗ $1${NC}"; exit 1; }

# ── Remote command helper ───────────────────────────────────────────────
remote() {
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$VPS_HOST" "$@"
}

# ── Connectivity check (reused by several commands) ─────────────────────
check_connection() {
    remote "echo 'ok'" >/dev/null 2>&1 || fail "Cannot reach $VPS_HOST — check SSH key and .deploy.env"
}

# ===========================================================================
# Commands
# ===========================================================================

cmd_deploy() {
    # Full deployment: stop → sync code → install deps → start
    local dry_run="${1:-}"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping service on VPS"
    remote "sudo systemctl stop $VPS_SERVICE 2>/dev/null || true"
    ok "Service stopped (or was not running)"

    step "Syncing code to $VPS_HOST:$VPS_APP_DIR"
    local rsync_opts="-azv --delete --exclude-from=$SCRIPT_DIR/rsync-exclude.txt"
    if [[ "$dry_run" == "--dry-run" ]]; then
        rsync_opts="$rsync_opts --dry-run"
        warn "DRY RUN — no files will be transferred"
    fi

    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    rsync $rsync_opts -e "$ssh_cmd" \
        "$PROJECT_ROOT/" \
        "$VPS_HOST:$VPS_APP_DIR/"
    ok "Code synced"

    if [[ "$dry_run" == "--dry-run" ]]; then
        echo -e "\n${YELLOW}Dry run complete — no changes made on the server.${NC}"
        return
    fi

    step "Installing / updating Python dependencies"
    remote "cd $VPS_APP_DIR && .venv/bin/pip install -q -r requirements.txt"
    ok "Dependencies up to date"

    step "Updating systemd service file"
    local ssh_cmd="ssh $SSH_OPTS"
    # The deployment/ dir is excluded from rsync, so copy the service file directly
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$SCRIPT_DIR/coincalltrader.service" "$VPS_HOST:/etc/systemd/system/$VPS_SERVICE.service"
    remote "sudo systemctl daemon-reload"
    ok "systemd reloaded"

    step "Starting service"
    remote "sudo systemctl start $VPS_SERVICE"
    sleep 2

    if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
        ok "Service is running"
    else
        fail "Service failed to start — check logs with: ./deployment/deploy.sh --logs"
    fi

    step "Recent log output"
    remote "sudo journalctl -u $VPS_SERVICE -n 20 --no-pager" || true

    echo -e "\n${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Deploy complete!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
}

cmd_setup() {
    # One-time server setup: run server-setup.sh on the VPS
    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Running server setup on $VPS_HOST"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$VPS_HOST" "bash -s" < "$SCRIPT_DIR/server-setup.sh"

    echo -e "\n${GREEN}Server setup complete!${NC}"
    echo -e "Next: copy your .env file with:  ${CYAN}./deployment/deploy.sh --env${NC}"
    echo -e "Then deploy with:                ${CYAN}./deployment/deploy.sh${NC}"
}

cmd_env() {
    # Copy the local .env file to the VPS
    local env_file="$PROJECT_ROOT/.env"
    if [[ ! -f "$env_file" ]]; then
        fail ".env file not found at $env_file"
    fi

    step "Copying .env to VPS (with DEPLOYMENT_TARGET=production)"
    check_connection

    # Copy to a temp file, override DEPLOYMENT_TARGET for the server
    local tmp_env
    tmp_env=$(mktemp)
    sed 's/^DEPLOYMENT_TARGET=.*/DEPLOYMENT_TARGET=production/' "$env_file" > "$tmp_env"

    # shellcheck disable=SC2086
    scp $SSH_OPTS "$tmp_env" "$VPS_HOST:$VPS_APP_DIR/.env"
    rm -f "$tmp_env"
    ok ".env copied to $VPS_HOST:$VPS_APP_DIR/.env (DEPLOYMENT_TARGET set to production)"
}

cmd_stop() {
    step "Stopping service"
    check_connection
    remote "sudo systemctl stop $VPS_SERVICE"
    ok "Service stopped"
}

cmd_start() {
    step "Starting service"
    check_connection
    remote "sudo systemctl start $VPS_SERVICE"
    sleep 2
    if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
        ok "Service is running"
    else
        fail "Service failed to start"
    fi
}

cmd_restart() {
    step "Restarting service"
    check_connection
    remote "sudo systemctl restart $VPS_SERVICE"
    sleep 2
    if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
        ok "Service is running"
    else
        fail "Service failed to start"
    fi
}

cmd_status() {
    check_connection
    echo ""
    remote "sudo systemctl status $VPS_SERVICE --no-pager -l" || true
}

cmd_logs() {
    check_connection
    step "Tailing live logs (Ctrl+C to stop)"
    remote "sudo journalctl -u $VPS_SERVICE -f --no-pager"
}

cmd_clean() {
    # Clean restart: stop → delete all logs/snapshots → start fresh
    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping service"
    remote "sudo systemctl stop $VPS_SERVICE 2>/dev/null || true"
    ok "Service stopped"

    step "Deleting logs and state files on VPS"
    # These are the files that can cause stale-state or corruption issues:
    #   trading.log          — application log
    #   trades_snapshot.json — active trade state (crash recovery)
    #   active_orders.json   — order ledger snapshot (crash recovery)
    #   order_ledger.jsonl   — order audit trail
    #   trade_history.jsonl  — completed trade history (append-only)
    #   *.corrupt.*          — quarantined corrupt files
    remote "rm -f $VPS_APP_DIR/logs/trading.log \
                 $VPS_APP_DIR/logs/trades_snapshot.json \
                 $VPS_APP_DIR/logs/active_orders.json \
                 $VPS_APP_DIR/logs/order_ledger.jsonl \
                 $VPS_APP_DIR/logs/trade_history.jsonl \
                 $VPS_APP_DIR/logs/*.corrupt.* \
                 $VPS_APP_DIR/logs/*.log"
    ok "All logs and snapshots deleted"

    step "Starting service (clean state)"
    remote "sudo systemctl start $VPS_SERVICE"
    sleep 2
    if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
        ok "Service is running with clean state"
    else
        fail "Service failed to start"
    fi

    step "Recent log output"
    remote "sudo journalctl -u $VPS_SERVICE -n 10 --no-pager" || true

    echo -e "\n${GREEN}Clean restart complete — all logs and snapshots cleared.${NC}"
}

cmd_update() {
    # Update OS packages on the VPS
    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Updating package lists"
    remote "sudo apt-get update -qq"
    ok "Package lists updated"

    step "Upgrading packages"
    remote "sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq"
    ok "Packages upgraded"

    step "Checking if reboot is needed"
    if remote "test -f /var/run/reboot-required" 2>/dev/null; then
        warn "Reboot required (kernel or system update). Run: ./deployment/deploy.sh --reboot"
    else
        ok "No reboot needed"
    fi
}

cmd_reboot() {
    # Reboot the VPS and wait for it to come back
    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping service before reboot"
    remote "sudo systemctl stop $VPS_SERVICE 2>/dev/null || true"
    ok "Service stopped"

    step "Rebooting VPS"
    remote "sudo reboot" 2>/dev/null || true
    echo -e "  ${YELLOW}Waiting for VPS to come back...${NC}"

    # Wait for SSH to become available again (up to 120 seconds)
    local attempts=0
    local max_attempts=24
    while [[ $attempts -lt $max_attempts ]]; do
        sleep 5
        attempts=$((attempts + 1))
        if remote "echo ok" >/dev/null 2>&1; then
            ok "VPS is back online (after ~$((attempts * 5))s)"
            break
        fi
    done

    if [[ $attempts -ge $max_attempts ]]; then
        fail "VPS did not come back after 120 seconds — check Hetzner console"
    fi

    # systemd should auto-start the service after boot
    sleep 3
    step "Checking service status"
    if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
        ok "Service is running (auto-started after boot)"
    else
        warn "Service is not running — starting it now"
        remote "sudo systemctl start $VPS_SERVICE"
        sleep 2
        if remote "sudo systemctl is-active --quiet $VPS_SERVICE"; then
            ok "Service is running"
        else
            fail "Service failed to start after reboot"
        fi
    fi

    echo -e "\n${GREEN}Reboot complete.${NC}"
}

cmd_health() {
    # Quick health check: uptime, disk, memory, service status
    check_connection

    echo -e "\n${CYAN}═══ VPS Health Check ═══${NC}"
    echo ""

    echo -e "${CYAN}▸ Uptime${NC}"
    remote "uptime"

    echo -e "\n${CYAN}▸ Disk Usage${NC}"
    remote "df -h / | tail -1 | awk '{printf \"  Used: %s / %s (%s)\\n\", \$3, \$2, \$5}'"

    echo -e "\n${CYAN}▸ Memory${NC}"
    remote "free -h | awk '/^Mem:/ {printf \"  Used: %s / %s\\n\", \$3, \$2}'"

    echo -e "\n${CYAN}▸ Service Status${NC}"
    remote "sudo systemctl is-active $VPS_SERVICE 2>/dev/null" && ok "$VPS_SERVICE is running" || warn "$VPS_SERVICE is not running"

    echo -e "\n${CYAN}▸ Logs Directory${NC}"
    remote "ls -lh $VPS_APP_DIR/logs/ 2>/dev/null || echo '  (empty)'"

    if remote "test -f /var/run/reboot-required" 2>/dev/null; then
        echo -e "\n${YELLOW}  ⚠ Reboot required (pending OS update)${NC}"
    fi
    echo ""
}

cmd_ssh() {
    step "Opening SSH session to $VPS_HOST"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$VPS_HOST"
}

cmd_help() {
    cat <<'EOF'
CoincallTrader — Deployment Tool
════════════════════════════════════════

Usage:  ./deployment/deploy.sh [command]

Commands:
  (no args)    Full deploy: stop → sync → deps → start
  --dry-run    Preview rsync changes (nothing transferred)
  --setup      One-time server setup (run once on fresh VPS)
  --env        Copy .env to the VPS
  --stop       Stop the service
  --start      Start the service
  --restart    Restart the service
  --clean      Clean restart: delete all logs/snapshots, then start fresh
  --status     Show service status + uptime
  --logs       Tail live logs (Ctrl+C to stop)
  --health     Quick health check (disk, memory, uptime, service)
  --update     Update OS packages on the VPS
  --reboot     Reboot the VPS, wait for it, verify service
  --ssh        Open interactive SSH session
  --help       Show this help

Configuration:
  Create .deploy.env in the project root with:
    VPS_HOST=root@your-vps-ip
    VPS_APP_DIR=/opt/coincalltrader     (default)
    VPS_SERVICE=coincalltrader          (default)
    SSH_KEY=~/.ssh/id_ed25519           (optional)

First-time setup:
  1.  ./deployment/deploy.sh --setup    (prepare the VPS)
  2.  ./deployment/deploy.sh --env      (copy .env credentials)
  3.  ./deployment/deploy.sh            (deploy & start)
EOF
}

# ===========================================================================
# Main — route to the right command
# ===========================================================================
case "${1:-}" in
    --dry-run)    cmd_deploy --dry-run ;;
    --setup)      cmd_setup ;;
    --env)        cmd_env ;;
    --stop)       cmd_stop ;;
    --start)      cmd_start ;;
    --restart)    cmd_restart ;;
    --clean)      cmd_clean ;;
    --status)     cmd_status ;;
    --logs)       cmd_logs ;;
    --health)     cmd_health ;;
    --update)     cmd_update ;;
    --reboot)     cmd_reboot ;;
    --ssh)        cmd_ssh ;;
    --help|-h)    cmd_help ;;
    "")           cmd_deploy ;;
    *)            fail "Unknown command: $1 (try --help)" ;;
esac
