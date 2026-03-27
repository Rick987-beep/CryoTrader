#!/usr/bin/env bash
# ===========================================================================
# CoincallTrader — Slot Deployment Script
#
# Deploys the trading bot to named slots on the production server,
# or deploys the hub dashboard.
#
# Prerequisites:
#   - .deploy.slots.env in the project root (SSH connection settings)
#   - deployment/slots.yaml with slot definitions
#   - Per-slot .env files (e.g. .env.slot-01) — gitignored
#
# Usage:
#   ./deployment/deploy-slot.sh 01              Deploy code + .env to slot-01
#   ./deployment/deploy-slot.sh 01 --setup      First-time slot setup (dir, venv, service)
#   ./deployment/deploy-slot.sh 01 --clean      Stop service, wipe logs/state
#   ./deployment/deploy-slot.sh 01 --destroy    Stop service, delete entire slot
#   ./deployment/deploy-slot.sh 01 --stop       Stop slot-01
#   ./deployment/deploy-slot.sh 01 --start      Start slot-01
#   ./deployment/deploy-slot.sh 01 --restart    Restart slot-01
#   ./deployment/deploy-slot.sh 01 --logs       Tail slot-01 journalctl
#   ./deployment/deploy-slot.sh 01 --status     Show slot-01 service status
#
#   ./deployment/deploy-slot.sh hub             Deploy hub dashboard
#   ./deployment/deploy-slot.sh hub --setup     First-time hub setup
#   ./deployment/deploy-slot.sh hub --stop      Stop hub
#   ./deployment/deploy-slot.sh hub --start     Start hub
#   ./deployment/deploy-slot.sh hub --logs      Tail hub logs
#
#   ./deployment/deploy-slot.sh status          Show all slots overview
#   ./deployment/deploy-slot.sh --help          Show this help
# ===========================================================================
set -euo pipefail

# ── Resolve paths ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Load deploy configuration ──────────────────────────────────────────
DEPLOY_ENV="${PROJECT_ROOT}/.deploy.slots.env"
if [[ ! -f "$DEPLOY_ENV" ]]; then
    echo "Error: $DEPLOY_ENV not found."
    echo "Create it with:"
    echo "  VPS_HOST=root@YOUR_VPS_IP"
    echo "  SSH_KEY=~/.ssh/your_key  (optional)"
    exit 1
fi
# shellcheck disable=SC1090
source "$DEPLOY_ENV"

: "${VPS_HOST:?Set VPS_HOST in .deploy.slots.env}"
: "${SSH_KEY:=}"

CT_BASE="/opt/ct"

# ── SSH / rsync options ─────────────────────────────────────────────────
SSH_OPTS="-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
if [[ -n "$SSH_KEY" ]]; then
    SSH_OPTS="$SSH_OPTS -i $SSH_KEY"
fi

# ── Terminal colours ────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

step()  { echo -e "\n${CYAN}▸ $1${NC}"; }
ok()    { echo -e "  ${GREEN}✓ $1${NC}"; }
warn()  { echo -e "  ${YELLOW}⚠ $1${NC}"; }
fail()  { echo -e "  ${RED}✗ $1${NC}"; exit 1; }

remote() {
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$VPS_HOST" "$@"
}

check_connection() {
    remote "echo 'ok'" >/dev/null 2>&1 || fail "Cannot reach $VPS_HOST"
}

# ===========================================================================
# Slot helpers
# ===========================================================================

slot_dir() { echo "$CT_BASE/slot-$1"; }
slot_service() { echo "ct-slot@$1"; }

validate_slot() {
    local slot="$1"
    if ! [[ "$slot" =~ ^[0-9]{2}$ ]]; then
        fail "Invalid slot ID '$slot' — must be two digits (01–10)"
    fi
}

get_env_file() {
    local slot="$1"
    local toml_file="$PROJECT_ROOT/slots/slot-$slot.toml"
    local env_file="$PROJECT_ROOT/.env.slot-$slot"

    # If a slot .toml config exists, generate .env from it
    if [[ -f "$toml_file" ]]; then
        step "Generating .env.slot-$slot from slots/slot-$slot.toml" >&2
        python3 "$PROJECT_ROOT/slot_config.py" "$slot" >&2
        ok ".env.slot-$slot generated" >&2
    fi

    if [[ ! -f "$env_file" ]]; then
        fail "Slot env file not found: .env.slot-$slot\nCreate slots/slot-$slot.toml or .env.slot-$slot manually"
    fi
    echo "$env_file"
}

# ===========================================================================
# Slot commands
# ===========================================================================

cmd_slot_deploy() {
    local slot="$1"
    validate_slot "$slot"
    local env_file; env_file="$(get_env_file "$slot")"
    local dir; dir="$(slot_dir "$slot")"
    local svc; svc="$(slot_service "$slot")"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping $svc"
    remote "sudo systemctl stop $svc 2>/dev/null || true"
    ok "Service stopped (or was not running)"

    step "Syncing code to $VPS_HOST:$dir"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    rsync -azv --delete \
        --exclude-from="$SCRIPT_DIR/rsync-exclude-slot.txt" \
        -e "$ssh_cmd" \
        "$PROJECT_ROOT/" \
        "$VPS_HOST:$dir/"
    ok "Code synced"

    step "Deploying slot .env"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$env_file" "$VPS_HOST:$dir/.env"
    ok ".env.slot-$slot → $dir/.env"

    step "Patching .env for production"
    remote "sed -i 's/^DEPLOYMENT_TARGET=.*/DEPLOYMENT_TARGET=production/' $dir/.env"
    ok "DEPLOYMENT_TARGET set to production"

    step "Installing Python dependencies"
    remote "cd $dir && .venv/bin/pip install -q -r requirements.txt"
    ok "Dependencies up to date"

    step "Starting $svc"
    remote "sudo systemctl start $svc"
    sleep 2
    if remote "sudo systemctl is-active --quiet $svc"; then
        ok "Slot $slot is running"
    else
        fail "Slot $slot failed to start — check: ./deployment/deploy-slot.sh $slot --logs"
    fi

    step "Recent logs"
    remote "sudo journalctl -u $svc -n 15 --no-pager" || true

    echo -e "\n${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Slot $slot deployed!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
}

cmd_slot_setup() {
    local slot="$1"
    validate_slot "$slot"
    local dir; dir="$(slot_dir "$slot")"
    local svc; svc="$(slot_service "$slot")"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Creating slot directory"
    remote "mkdir -p $dir/logs"
    ok "$dir created"

    step "Creating Python venv"
    remote "
        if [ ! -d $dir/.venv ]; then
            python3 -m venv $dir/.venv
            $dir/.venv/bin/pip install --upgrade pip -q
        fi
    "
    ok "Virtual environment ready"

    step "Installing systemd template (if needed)"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$SCRIPT_DIR/ct-slot@.service" "$VPS_HOST:/etc/systemd/system/ct-slot@.service"
    remote "sudo systemctl daemon-reload"
    remote "sudo systemctl enable $svc 2>/dev/null || true"
    ok "Service $svc enabled"

    echo -e "\n${GREEN}Slot $slot setup complete!${NC}"
    echo -e "Next: deploy with:  ${CYAN}./deployment/deploy-slot.sh $slot${NC}"
}

cmd_slot_clean() {
    local slot="$1"
    validate_slot "$slot"
    local dir; dir="$(slot_dir "$slot")"
    local svc; svc="$(slot_service "$slot")"

    step "Stopping $svc"
    check_connection
    remote "sudo systemctl stop $svc 2>/dev/null || true"
    ok "Service stopped"

    step "Wiping logs and state"
    remote "rm -rf $dir/logs/*"
    ok "Slot $slot logs cleared"
    echo -e "${YELLOW}Slot $slot is stopped with clean state. Re-deploy to start.${NC}"
}

cmd_slot_destroy() {
    local slot="$1"
    validate_slot "$slot"
    local dir; dir="$(slot_dir "$slot")"
    local svc; svc="$(slot_service "$slot")"

    echo -e "${RED}This will PERMANENTLY delete slot $slot ($dir) on the server.${NC}"
    read -rp "Type 'yes' to confirm: " confirm
    if [[ "$confirm" != "yes" ]]; then
        echo "Aborted."
        exit 0
    fi

    check_connection

    step "Stopping and disabling $svc"
    remote "sudo systemctl stop $svc 2>/dev/null || true"
    remote "sudo systemctl disable $svc 2>/dev/null || true"
    ok "Service disabled"

    step "Deleting $dir"
    remote "rm -rf $dir"
    ok "Slot $slot destroyed"
}

cmd_slot_stop()    { validate_slot "$1"; check_connection; remote "sudo systemctl stop $(slot_service "$1")"; ok "Slot $1 stopped"; }
cmd_slot_start()   { validate_slot "$1"; check_connection; remote "sudo systemctl start $(slot_service "$1")"; sleep 2; ok "Slot $1 started"; }
cmd_slot_restart() { validate_slot "$1"; check_connection; remote "sudo systemctl restart $(slot_service "$1")"; sleep 2; ok "Slot $1 restarted"; }
cmd_slot_logs()    { validate_slot "$1"; check_connection; remote "sudo journalctl -u $(slot_service "$1") -f"; }
cmd_slot_status()  { validate_slot "$1"; check_connection; remote "sudo systemctl status $(slot_service "$1") --no-pager"; }

# ===========================================================================
# Hub commands
# ===========================================================================

cmd_hub_deploy() {
    local hub_dir="$CT_BASE/hub"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping ct-hub"
    remote "sudo systemctl stop ct-hub 2>/dev/null || true"
    ok "Hub stopped (or was not running)"

    step "Syncing hub to $VPS_HOST:$hub_dir"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    rsync -azv --delete \
        --exclude '.venv' \
        --exclude '.env' \
        --exclude '__pycache__' \
        --exclude 'test_hub_local.py' \
        -e "$ssh_cmd" \
        "$PROJECT_ROOT/hub/" \
        "$VPS_HOST:$hub_dir/"
    ok "Hub code synced"

    step "Deploying hub .env"
    if [[ -f "$PROJECT_ROOT/.env.hub" ]]; then
        # shellcheck disable=SC2086
        scp $SSH_OPTS "$PROJECT_ROOT/.env.hub" "$VPS_HOST:$hub_dir/.env"
        ok ".env.hub → $hub_dir/.env"
    else
        warn "No .env.hub found — hub will use defaults"
    fi

    step "Installing Python dependencies"
    remote "cd $hub_dir && .venv/bin/pip install -q -r requirements.txt"
    ok "Dependencies up to date"

    step "Starting ct-hub"
    remote "sudo systemctl start ct-hub"
    sleep 2
    if remote "sudo systemctl is-active --quiet ct-hub"; then
        ok "Hub is running"
    else
        fail "Hub failed to start — check: ./deployment/deploy-slot.sh hub --logs"
    fi

    echo -e "\n${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Hub deployed!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
}

cmd_hub_setup() {
    local hub_dir="$CT_BASE/hub"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Creating hub directory"
    remote "mkdir -p $hub_dir"
    ok "$hub_dir created"

    step "Creating Python venv"
    remote "
        if [ ! -d $hub_dir/.venv ]; then
            python3 -m venv $hub_dir/.venv
            $hub_dir/.venv/bin/pip install --upgrade pip -q
        fi
    "
    ok "Hub venv ready"

    step "Installing systemd service"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$SCRIPT_DIR/ct-hub.service" "$VPS_HOST:/etc/systemd/system/ct-hub.service"
    remote "sudo systemctl daemon-reload"
    remote "sudo systemctl enable ct-hub 2>/dev/null || true"
    ok "ct-hub service enabled"

    # Read hub port from .env.hub (default 8070)
    local hub_port=8070
    if [[ -f "$PROJECT_ROOT/.env.hub" ]]; then
        local _p
        _p=$(grep -E '^HUB_PORT=' "$PROJECT_ROOT/.env.hub" | cut -d= -f2 | tr -d ' ')
        [[ -n "$_p" ]] && hub_port="$_p"
    fi
    step "Opening firewall port $hub_port"
    remote "sudo ufw allow ${hub_port}/tcp >/dev/null 2>&1 || true"
    ok "Port $hub_port open"

    echo -e "\n${GREEN}Hub setup complete!${NC}"
    echo -e "Next: deploy with:  ${CYAN}./deployment/deploy-slot.sh hub${NC}"
}

cmd_hub_stop()    { check_connection; remote "sudo systemctl stop ct-hub"; ok "Hub stopped"; }
cmd_hub_start()   { check_connection; remote "sudo systemctl start ct-hub"; sleep 2; ok "Hub started"; }
cmd_hub_restart() { check_connection; remote "sudo systemctl restart ct-hub"; sleep 2; ok "Hub restarted"; }
cmd_hub_logs()    { check_connection; remote "sudo journalctl -u ct-hub -f"; }
cmd_hub_status()  { check_connection; remote "sudo systemctl status ct-hub --no-pager"; }

# ===========================================================================
# Recorder commands
# ===========================================================================

cmd_recorder_deploy() {
    local rec_dir="$CT_BASE/recorder"
    local env_file="$PROJECT_ROOT/.env.recorder"

    if [[ ! -f "$env_file" ]]; then
        fail ".env.recorder not found.\nCreate it with:\n  TELEGRAM_BOT_TOKEN=...\n  TELEGRAM_CHAT_ID=...\n  RECORDER_DATA_DIR=/opt/ct/recorder/data"
    fi

    # Safety check: refuse to deploy within 2 minutes of the next 5-min snapshot boundary.
    # Deploying too close risks missing a snapshot (subscribe window opens 10s before boundary).
    local now_secs interval=300 next_boundary secs_until
    now_secs=$(date +%s)
    next_boundary=$(( (now_secs / interval + 1) * interval ))
    secs_until=$(( next_boundary - now_secs ))
    if (( secs_until < 120 )); then
        local next_time
        next_time=$(date -u -d "@$next_boundary" '+%H:%M UTC' 2>/dev/null \
                    || date -u -r "$next_boundary" '+%H:%M UTC')
        fail "Too close to next snapshot boundary ($next_time, ${secs_until}s away).\nRetry after $next_time."
    fi
    echo -e "  ${GREEN}✓${NC} Safe deploy window (${secs_until}s until next snapshot)"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Stopping ct-recorder"
    remote "sudo systemctl stop ct-recorder 2>/dev/null || true"
    ok "Service stopped (or was not running)"

    step "Syncing recorder to $VPS_HOST:$rec_dir"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    rsync -azv --delete \
        --exclude-from="$SCRIPT_DIR/rsync-exclude-recorder.txt" \
        -e "$ssh_cmd" \
        "$PROJECT_ROOT/" \
        "$VPS_HOST:$rec_dir/"
    ok "Code synced"

    step "Deploying .env.recorder"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$env_file" "$VPS_HOST:$rec_dir/.env"
    ok ".env.recorder → $rec_dir/.env"

    step "Installing Python dependencies"
    remote "cd $rec_dir && .venv/bin/pip install -q -r requirements.txt"
    ok "Dependencies up to date"

    step "Starting ct-recorder"
    remote "sudo systemctl start ct-recorder"
    sleep 2
    if remote "sudo systemctl is-active --quiet ct-recorder"; then
        ok "Recorder is running"
    else
        fail "Recorder failed to start — check: ./deployment/deploy-slot.sh recorder --logs"
    fi

    step "Recent logs"
    remote "sudo journalctl -u ct-recorder -n 15 --no-pager" || true

    echo -e "\n${GREEN}════════════════════════════════════════${NC}"
    echo -e "${GREEN}  Recorder deployed!${NC}"
    echo -e "${GREEN}════════════════════════════════════════${NC}"
}

cmd_recorder_setup() {
    local rec_dir="$CT_BASE/recorder"

    step "Checking VPS connectivity"
    check_connection
    ok "Connected to $VPS_HOST"

    step "Creating recorder directories"
    remote "mkdir -p $rec_dir/data $rec_dir/logs"
    ok "$rec_dir/data and $rec_dir/logs created"

    step "Creating Python venv"
    remote "
        if [ ! -d $rec_dir/.venv ]; then
            python3 -m venv $rec_dir/.venv
            $rec_dir/.venv/bin/pip install --upgrade pip -q
        fi
    "
    ok "Virtual environment ready"

    step "Installing systemd service"
    local ssh_cmd="ssh $SSH_OPTS"
    # shellcheck disable=SC2086
    scp $SSH_OPTS "$SCRIPT_DIR/ct-recorder.service" "$VPS_HOST:/etc/systemd/system/ct-recorder.service"
    remote "sudo systemctl daemon-reload"
    remote "sudo systemctl enable ct-recorder 2>/dev/null || true"
    ok "ct-recorder service enabled"

    echo -e "\n${GREEN}Recorder setup complete!${NC}"
    echo -e "Next steps:"
    echo -e "  1. Create ${CYAN}.env.recorder${NC} (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, RECORDER_DATA_DIR)"
    echo -e "  2. Deploy with: ${CYAN}./deployment/deploy-slot.sh recorder${NC}"
}

cmd_recorder_stop()    { check_connection; remote "sudo systemctl stop ct-recorder"; ok "Recorder stopped"; }
cmd_recorder_start()   { check_connection; remote "sudo systemctl start ct-recorder"; sleep 2; ok "Recorder started"; }
cmd_recorder_restart() { check_connection; remote "sudo systemctl restart ct-recorder"; sleep 2; ok "Recorder restarted"; }
cmd_recorder_logs()    { check_connection; remote "sudo journalctl -u ct-recorder -f"; }
cmd_recorder_status()  { check_connection; remote "sudo systemctl status ct-recorder --no-pager"; }

# ===========================================================================
# Overview
# ===========================================================================

cmd_status_all() {
    check_connection

    echo -e "${CYAN}═══ CoincallTrader — Slot Overview ═══${NC}"
    echo ""

    # Hub
    local hub_status
    hub_status=$(remote "sudo systemctl is-active ct-hub 2>/dev/null || echo 'inactive'")
    if [[ "$hub_status" == "active" ]]; then
        echo -e "  Hub        ${GREEN}●${NC} running"
    else
        echo -e "  Hub        ${RED}●${NC} $hub_status"
    fi

    # Recorder
    local rec_status
    rec_status=$(remote "sudo systemctl is-active ct-recorder 2>/dev/null || echo 'inactive'")
    if [[ "$rec_status" == "active" ]]; then
        echo -e "  Recorder   ${GREEN}●${NC} running"
    else
        echo -e "  Recorder   ${RED}●${NC} $rec_status"
    fi

    echo ""

    # Slots 01–10
    for i in $(seq -w 1 10); do
        local svc="ct-slot@$i"
        local dir="$CT_BASE/slot-$i"

        local svc_status
        svc_status=$(remote "sudo systemctl is-active $svc 2>/dev/null || echo 'inactive'")
        local dir_exists
        dir_exists=$(remote "[ -d $dir ] && echo 'yes' || echo 'no'")

        if [[ "$dir_exists" == "no" ]]; then
            echo -e "  Slot $i   ${NC}○${NC} empty"
        elif [[ "$svc_status" == "active" ]]; then
            # Try to read strategy name from .env
            local name
            name=$(remote "grep -oP '^SLOT_NAME=\K.*' $dir/.env 2>/dev/null || echo '(unnamed)'")
            echo -e "  Slot $i   ${GREEN}●${NC} running   $name"
        else
            echo -e "  Slot $i   ${RED}●${NC} $svc_status"
        fi
    done

    echo ""
}

# ===========================================================================
# Help
# ===========================================================================

cmd_help() {
    echo "Usage: deploy-slot.sh <target> [command]"
    echo ""
    echo "Targets:"
    echo "  01–10       Slot number (two digits)"
    echo "  hub         Hub dashboard"
    echo "  recorder    Tick recorder service"
    echo "  status      Overview of all slots"
    echo ""
    echo "Slot commands:"
    echo "  (default)   Full deploy (stop → sync → deps → start)"
    echo "  --setup     First-time slot setup (dir, venv, service)"
    echo "  --clean     Stop service, wipe logs/state"
    echo "  --destroy   Stop service, delete entire slot directory"
    echo "  --stop      Stop the slot service"
    echo "  --start     Start the slot service"
    echo "  --restart   Restart the slot service"
    echo "  --logs      Tail live logs (Ctrl+C to stop)"
    echo "  --status    Show service status"
    echo ""
    echo "Hub commands:"
    echo "  (default)   Deploy hub dashboard"
    echo "  --setup     First-time hub setup"
    echo "  --stop      Stop hub"
    echo "  --start     Start hub"
    echo "  --restart   Restart hub"
    echo "  --logs      Tail hub logs"
    echo "  --status    Show hub status"
    echo ""
    echo "Recorder commands:"
    echo "  (default)   Full deploy (stop → sync → deps → start)"
    echo "  --setup     First-time setup (dir, venv, service install)"
    echo "  --stop      Stop the recorder"
    echo "  --start     Start the recorder"
    echo "  --restart   Restart the recorder"
    echo "  --logs      Tail live logs"
    echo "  --status    Show service status"
}

# ===========================================================================
# Main dispatch
# ===========================================================================

TARGET="${1:-}"
COMMAND="${2:-}"

case "$TARGET" in
    ""|--help|-h)
        cmd_help
        ;;
    status)
        cmd_status_all
        ;;
    hub)
        case "$COMMAND" in
            "")         cmd_hub_deploy ;;
            --setup)    cmd_hub_setup ;;
            --stop)     cmd_hub_stop ;;
            --start)    cmd_hub_start ;;
            --restart)  cmd_hub_restart ;;
            --logs)     cmd_hub_logs ;;
            --status)   cmd_hub_status ;;
            *)          fail "Unknown hub command: $COMMAND" ;;
        esac
        ;;
    recorder)
        case "$COMMAND" in
            "")         cmd_recorder_deploy ;;
            --setup)    cmd_recorder_setup ;;
            --stop)     cmd_recorder_stop ;;
            --start)    cmd_recorder_start ;;
            --restart)  cmd_recorder_restart ;;
            --logs)     cmd_recorder_logs ;;
            --status)   cmd_recorder_status ;;
            *)          fail "Unknown recorder command: $COMMAND" ;;
        esac
        ;;
    *)
        case "$COMMAND" in
            "")         cmd_slot_deploy "$TARGET" ;;
            --setup)    cmd_slot_setup "$TARGET" ;;
            --clean)    cmd_slot_clean "$TARGET" ;;
            --destroy)  cmd_slot_destroy "$TARGET" ;;
            --stop)     cmd_slot_stop "$TARGET" ;;
            --start)    cmd_slot_start "$TARGET" ;;
            --restart)  cmd_slot_restart "$TARGET" ;;
            --logs)     cmd_slot_logs "$TARGET" ;;
            --status)   cmd_slot_status "$TARGET" ;;
            *)          fail "Unknown command: $COMMAND" ;;
        esac
        ;;
esac
