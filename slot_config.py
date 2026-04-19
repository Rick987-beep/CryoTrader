#!/usr/bin/env python3
"""
Slot Config Generator — reads a slot .toml + accounts.toml + .env
and produces a fully resolved .env.slot-XX file.

Usage:
    python slot_config.py 01          # reads slots/slot-01.toml → .env.slot-01
    python slot_config.py 01 --dry    # print to stdout instead of writing

Called automatically by deploy-slot.sh before rsync.
"""

import os
import sys

try:
    import tomllib                     # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib            # Python 3.9/3.10 fallback

from dotenv import dotenv_values


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def load_toml(path: str) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def resolve_account(account_name: str, accounts: dict) -> dict:
    """Look up a named account from accounts.toml."""
    if account_name not in accounts:
        available = ", ".join(accounts.keys())
        print(f"Error: account '{account_name}' not found in accounts.toml")
        print(f"Available accounts: {available}")
        sys.exit(1)
    return accounts[account_name]


def resolve_secrets(account: dict, env_values: dict) -> dict:
    """Resolve actual API key/secret values from .env using the env var names."""
    api_key_var = account["api_key_env"]
    api_secret_var = account["api_secret_env"]

    api_key = env_values.get(api_key_var)
    api_secret = env_values.get(api_secret_var)

    if not api_key:
        print(f"Error: {api_key_var} not found in .env")
        sys.exit(1)
    if not api_secret:
        print(f"Error: {api_secret_var} not found in .env")
        sys.exit(1)

    return {"api_key": api_key, "api_secret": api_secret}


def generate_env(slot_id: str, slot_config: dict, account: dict,
                 secrets: dict, env_values: dict) -> str:
    """Generate the contents of a .env.slot-XX file."""
    exchange = account["exchange"]
    environment = account["environment"]
    strategy = slot_config["strategy"]
    name = slot_config.get("name", strategy)
    port = slot_config.get("port", 8090 + int(slot_id))

    lines = [
        f"# Auto-generated from slots/slot-{slot_id}.toml",
        f"# Account: {slot_config['account']} ({exchange}/{environment})",
        f"#",
        f"# Re-generate: python slot_config.py {slot_id}",
        f"# Deploy:      ./deployment/deploy-slot.sh {slot_id}",
        "",
        f"SLOT_NAME={name}",
        f"SLOT_ID={slot_id}",
        f"SLOT_STRATEGY={strategy}",
        f"EXCHANGE={exchange}",
        f"TRADING_ENVIRONMENT={environment}",
        f"DEPLOYMENT_TARGET=development",
        "",
        f"DASHBOARD_MODE=control",
        f"DASHBOARD_PORT={port}",
        f"DASHBOARD_PASSWORD={env_values.get('DASHBOARD_PASSWORD', '8420')}",
    ]

    # API credentials — key names depend on exchange
    lines.append("")
    if exchange == "coincall":
        key_env = account["api_key_env"]
        secret_env = account["api_secret_env"]
        lines.append(f"{key_env}={secrets['api_key']}")
        lines.append(f"{secret_env}={secrets['api_secret']}")
    elif exchange == "deribit":
        # config.py always reads DERIBIT_CLIENT_ID_PROD / DERIBIT_CLIENT_SECRET_PROD
        # regardless of which named account is used — write canonical names.
        env_suffix = "TEST" if environment == "testnet" else "PROD"
        lines.append(f"DERIBIT_CLIENT_ID_{env_suffix}={secrets['api_key']}")
        lines.append(f"DERIBIT_CLIENT_SECRET_{env_suffix}={secrets['api_secret']}")

    # Telegram (if available in .env)
    tg_token = env_values.get("TELEGRAM_BOT_TOKEN")
    tg_chat = env_values.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        lines.append("")
        lines.append(f"TELEGRAM_BOT_TOKEN={tg_token}")
        lines.append(f"TELEGRAM_CHAT_ID={tg_chat}")

    # Strategy parameter overrides
    params = slot_config.get("params", {})
    if params:
        lines.append("")
        lines.append("# Strategy parameter overrides")
        for key, value in params.items():
            lines.append(f"PARAM_{key.upper()}={value}")

    # Execution profile override
    exec_profile = slot_config.get("execution_profile")
    if exec_profile:
        lines.append("")
        lines.append("# Execution profile override")
        lines.append(f"EXECUTION_PROFILE={exec_profile}")

    # Per-phase execution overrides
    exec_overrides = slot_config.get("execution_overrides", {})
    if exec_overrides:
        lines.append("")
        lines.append("# Execution profile field overrides")
        for key, value in exec_overrides.items():
            lines.append(f"EXECUTION_OVERRIDE_{key}={value}")

    lines.append("")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: python slot_config.py <slot_id> [--dry]")
        print("  slot_id  Two-digit slot number (e.g. 01, 02)")
        print("  --dry    Print to stdout instead of writing file")
        sys.exit(0)

    slot_id = sys.argv[1].zfill(2)
    dry_run = "--dry" in sys.argv

    # Paths
    slot_toml = os.path.join(PROJECT_ROOT, "slots", f"slot-{slot_id}.toml")
    accounts_toml = os.path.join(PROJECT_ROOT, "accounts.toml")
    dot_env = os.path.join(PROJECT_ROOT, ".env")
    out_file = os.path.join(PROJECT_ROOT, f".env.slot-{slot_id}")

    # Validate inputs exist
    if not os.path.exists(slot_toml):
        print(f"Error: {slot_toml} not found")
        print(f"Create it first:  cp slots/slot-01.toml slots/slot-{slot_id}.toml")
        sys.exit(1)
    if not os.path.exists(accounts_toml):
        print(f"Error: accounts.toml not found in project root")
        sys.exit(1)
    if not os.path.exists(dot_env):
        print(f"Error: .env not found (needed for API secrets)")
        sys.exit(1)

    # Load everything
    slot_config = load_toml(slot_toml)
    accounts = load_toml(accounts_toml)
    env_values = dotenv_values(dot_env)

    # Resolve
    account = resolve_account(slot_config["account"], accounts)
    secrets = resolve_secrets(account, env_values)

    # Generate
    content = generate_env(slot_id, slot_config, account, secrets, env_values)

    if dry_run:
        print(content)
    else:
        with open(out_file, "w") as f:
            f.write(content)
        print(f"Generated {out_file}")
        print(f"  Strategy: {slot_config['strategy']}")
        print(f"  Account:  {slot_config['account']} ({account['exchange']})")
        params = slot_config.get("params", {})
        if params:
            print(f"  Params:   {len(params)} overrides")


if __name__ == "__main__":
    main()
