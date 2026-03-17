#!/usr/bin/env python3
"""
Configuration Module

Centralized configuration for the Coincall trading bot.
Supports both testnet and production environments with simple switching.

To switch environments:
  Set TRADING_ENVIRONMENT variable in .env file:
    TRADING_ENVIRONMENT=testnet   (default)
    TRADING_ENVIRONMENT=production
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =============================================================================
# DEPLOYMENT TARGET
# =============================================================================

# Deployment target: development (macOS) or production (Windows Server)
DEPLOYMENT_TARGET = os.getenv('DEPLOYMENT_TARGET', 'development').lower()

if DEPLOYMENT_TARGET not in ['development', 'production']:
    raise ValueError(f"Invalid DEPLOYMENT_TARGET: '{DEPLOYMENT_TARGET}'. Must be 'development' or 'production'")

# =============================================================================
# EXCHANGE SELECTION
# =============================================================================

# Which exchange to use: 'coincall' (default) or 'deribit' (Phase 2)
EXCHANGE = os.getenv('EXCHANGE', 'coincall').lower()

if EXCHANGE not in ['coincall', 'deribit']:
    raise ValueError(f"Invalid EXCHANGE: '{EXCHANGE}'. Must be 'coincall' or 'deribit'")

# =============================================================================
# ENVIRONMENT SELECTION
# =============================================================================

# Simple environment switcher - change this or set TRADING_ENVIRONMENT in .env
ENVIRONMENT = os.getenv('TRADING_ENVIRONMENT', 'testnet').lower()

if ENVIRONMENT not in ['testnet', 'production']:
    raise ValueError(f"Invalid TRADING_ENVIRONMENT: '{ENVIRONMENT}'. Must be 'testnet' or 'production'")

# =============================================================================
# TESTNET CONFIGURATION
# =============================================================================

TESTNET = {
    'base_url': 'https://betaapi.coincall.com',
    'api_key': os.getenv('COINCALL_API_KEY_TEST'),
    'api_secret': os.getenv('COINCALL_API_SECRET_TEST'),
    'ws_options': 'wss://betaws.coincall.com/options',
    'ws_futures': 'wss://betaws.coincall.com/futures',
    'ws_spot': 'wss://betaws.coincall.com/spot',
}

# =============================================================================
# PRODUCTION CONFIGURATION
# =============================================================================

PRODUCTION = {
    'base_url': 'https://api.coincall.com',
    'api_key': os.getenv('COINCALL_API_KEY_PROD'),
    'api_secret': os.getenv('COINCALL_API_SECRET_PROD'),
    'ws_options': 'wss://ws.coincall.com/options',
    'ws_futures': 'wss://ws.coincall.com/futures',
    'ws_spot': 'wss://ws.coincall.com/spot',
}

# =============================================================================
# ACTIVE CONFIGURATION (Selected by TRADING_ENVIRONMENT)
# =============================================================================

ACTIVE_CONFIG = TESTNET if ENVIRONMENT == 'testnet' else PRODUCTION

# Export commonly used values for convenience
BASE_URL = ACTIVE_CONFIG['base_url']
API_KEY = ACTIVE_CONFIG['api_key']
API_SECRET = ACTIVE_CONFIG['api_secret']

# =============================================================================
# DERIBIT CONFIGURATION
# =============================================================================

DERIBIT_TESTNET = {
    'base_url': 'https://test.deribit.com',
    'client_id': os.getenv('DERIBIT_CLIENT_ID_TEST'),
    'client_secret': os.getenv('DERIBIT_CLIENT_SECRET_TEST'),
}

DERIBIT_PRODUCTION = {
    'base_url': 'https://www.deribit.com',
    'client_id': os.getenv('DERIBIT_CLIENT_ID_PROD'),
    'client_secret': os.getenv('DERIBIT_CLIENT_SECRET_PROD'),
}

DERIBIT_CONFIG = DERIBIT_TESTNET if ENVIRONMENT == 'testnet' else DERIBIT_PRODUCTION
DERIBIT_BASE_URL = DERIBIT_CONFIG['base_url']
DERIBIT_CLIENT_ID = DERIBIT_CONFIG['client_id']
DERIBIT_CLIENT_SECRET = DERIBIT_CONFIG['client_secret']


# =============================================================================
# CONFIGURATION VALIDATION
# =============================================================================

def validate_config():
    """Validate that all required configuration is present"""
    if EXCHANGE == 'coincall':
        required = {'API_KEY': API_KEY, 'API_SECRET': API_SECRET}
    else:
        required = {'DERIBIT_CLIENT_ID': DERIBIT_CLIENT_ID,
                     'DERIBIT_CLIENT_SECRET': DERIBIT_CLIENT_SECRET}

    missing = [k for k, v in required.items() if not v]
    if missing:
        env_str = f"({ENVIRONMENT} mode)" if ENVIRONMENT else ""
        raise ValueError(
            f"Missing required API credentials for {EXCHANGE} {env_str}: {', '.join(missing)}\n"
            f"Please set environment variables in .env file."
        )


# Validate on import
validate_config()

# Print configuration status
print(f"[CONFIG] Deployment: {DEPLOYMENT_TARGET.upper()}")
print(f"[CONFIG] Exchange: {EXCHANGE.upper()}")
print(f"[CONFIG] Environment: {ENVIRONMENT.upper()}")
if EXCHANGE == 'coincall':
    print(f"[CONFIG] Base URL: {BASE_URL}")
    print(f"[CONFIG] API Key: {API_KEY[:20]}..." if API_KEY else "[CONFIG] API Key: NOT SET")
else:
    print(f"[CONFIG] Base URL: {DERIBIT_BASE_URL}")
    print(f"[CONFIG] Client ID: {DERIBIT_CLIENT_ID[:20]}..." if DERIBIT_CLIENT_ID else "[CONFIG] Client ID: NOT SET")