"""
Live test fixtures — skip when credentials are missing.
"""

import os
import pytest


def _has_deribit_creds():
    return os.environ.get("DERIBIT_CLIENT_ID") and os.environ.get("DERIBIT_CLIENT_SECRET")


skip_no_creds = pytest.mark.skipif(
    not _has_deribit_creds(),
    reason="DERIBIT_CLIENT_ID / DERIBIT_CLIENT_SECRET not set",
)
