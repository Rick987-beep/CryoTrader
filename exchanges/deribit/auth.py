"""
Deribit Authentication — OAuth2 Token Lifecycle

Implements ExchangeAuth for Deribit using client_credentials grant.
Manages token refresh proactively at ~80% TTL (720s of 900s default).
Thread-safe: a single Lock serializes refresh so concurrent callers
never use an invalidated token.

All private API calls go through `get` / `post` which auto-refresh
the bearer token when it's close to expiry.
"""

import logging
import threading
import time
from typing import Any, Dict, Optional

import requests

from exchanges.base import ExchangeAuth
from config import DERIBIT_BASE_URL, DERIBIT_CLIENT_ID, DERIBIT_CLIENT_SECRET
from retry import retry

logger = logging.getLogger(__name__)

# Refresh the token when this fraction of TTL has elapsed (80% of 900s = 720s).
_REFRESH_FRACTION = 0.80

DEFAULT_REQUEST_TIMEOUT = 30.0


class DeribitAuth(ExchangeAuth):
    """
    OAuth2 bearer-token auth for the Deribit JSON-RPC API.

    On first use (or after expiry), authenticates via client_credentials.
    Proactively refreshes before the token expires.  Old tokens are
    invalidated immediately by Deribit, so the swap must be atomic.
    """

    # After this many consecutive request failures, mark exchange as unreachable
    _UNREACHABLE_THRESHOLD = 3
    # After this many consecutive failures, recreate the HTTP session
    _SESSION_REFRESH_THRESHOLD = 5

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self._client_id = client_id or DERIBIT_CLIENT_ID
        self._client_secret = client_secret or DERIBIT_CLIENT_SECRET
        self.base_url = base_url or DERIBIT_BASE_URL

        self._session = requests.Session()
        self._lock = threading.Lock()
        self._consecutive_failures = 0

        # Token state (guarded by _lock)
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._token_expires_at: float = 0.0   # epoch when token expires
        self._token_refresh_at: float = 0.0   # epoch when we should refresh

    @property
    def reachable(self) -> bool:
        """True when the exchange is responding normally."""
        return self._consecutive_failures < self._UNREACHABLE_THRESHOLD

    def _record_success(self) -> None:
        """Reset failure counter on a successful request."""
        if self._consecutive_failures > 0:
            logger.info(
                f"Deribit connection restored after {self._consecutive_failures} failure(s)"
            )
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Increment failure counter; refresh session after sustained failures."""
        self._consecutive_failures += 1
        if self._consecutive_failures == self._UNREACHABLE_THRESHOLD:
            logger.warning(
                f"Deribit marked UNREACHABLE after "
                f"{self._consecutive_failures} consecutive failures"
            )
        if self._consecutive_failures >= self._SESSION_REFRESH_THRESHOLD:
            logger.warning("Refreshing HTTP session to drop stale connections")
            try:
                self._session.close()
            except Exception:
                pass
            self._session = requests.Session()

    # ── Public ExchangeAuth interface ────────────────────────────────

    def get(self, endpoint: str, **kwargs) -> dict:
        """Authenticated GET (maps to JSON-RPC over HTTP GET for public endpoints)."""
        self._ensure_token()
        url = f"{self.base_url}/api/v2{endpoint}"
        try:
            resp = self._request_with_retry("GET", url, **kwargs)
            result = self._parse(resp)
            if "error" in result:
                self._record_failure()
            else:
                self._record_success()
            return result
        except requests.RequestException as e:
            self._record_failure()
            logger.error(f"Deribit GET {endpoint} failed: {e}")
            return {"error": {"code": -1, "message": str(e)}}

    def post(self, endpoint: str, data: Any = None, **kwargs) -> dict:
        """Authenticated POST — wraps payload in JSON-RPC 2.0 envelope."""
        self._ensure_token()
        url = f"{self.base_url}/api/v2{endpoint}"
        body = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": endpoint.lstrip("/"),
            "params": data or {},
        }
        try:
            resp = self._request_with_retry("POST", url, json_body=body, **kwargs)
            result = self._parse(resp)
            if "error" in result:
                self._record_failure()
            else:
                self._record_success()
            return result
        except requests.RequestException as e:
            self._record_failure()
            logger.error(f"Deribit POST {endpoint} failed: {e}")
            return {"error": {"code": -1, "message": str(e)}}

    def is_successful(self, response: dict) -> bool:
        """Deribit success = 'result' key present and no 'error' key."""
        return "result" in response and "error" not in response

    # ── JSON-RPC helpers ─────────────────────────────────────────────

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        """
        High-level JSON-RPC call.

        Args:
            method: e.g. "public/get_instruments" or "private/buy"
            params: method parameters

        Returns the full parsed response (with 'result' or 'error').
        """
        is_public = method.startswith("public/")

        if not is_public:
            self._ensure_token()

        url = f"{self.base_url}/api/v2/{method}"
        body = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": method,
            "params": params or {},
        }
        headers = {"Content-Type": "application/json"}
        if not is_public:
            headers["Authorization"] = f"Bearer {self._access_token}"

        try:
            resp = self._session.post(
                url, json=body, headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT
            )
            result = self._parse(resp)
            if "error" in result:
                self._record_failure()
            else:
                self._record_success()
            return result
        except requests.RequestException as e:
            self._record_failure()
            logger.error(f"Deribit RPC {method} failed: {e}")
            return {"error": {"code": -1, "message": str(e)}}

    # ── Token lifecycle ──────────────────────────────────────────────

    def _ensure_token(self):
        """Obtain or refresh the token if needed. Thread-safe."""
        now = time.time()
        if self._access_token and now < self._token_refresh_at:
            return  # Token still fresh

        with self._lock:
            # Double-check after acquiring lock (another thread may have refreshed).
            now = time.time()
            if self._access_token and now < self._token_refresh_at:
                return

            if self._refresh_token and now < self._token_expires_at:
                self._do_refresh()
            else:
                self._do_auth()

    def _do_auth(self):
        """Authenticate with client_credentials grant."""
        logger.info("Deribit: authenticating (client_credentials)")
        resp = self._auth_request({
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
        })
        self._apply_token(resp)

    def _do_refresh(self):
        """Refresh using the single-use refresh_token."""
        logger.info("Deribit: refreshing token")
        try:
            resp = self._auth_request({
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
            })
            self._apply_token(resp)
        except Exception as e:
            logger.warning(f"Deribit: token refresh failed ({e}), re-authenticating")
            self._do_auth()

    def _auth_request(self, params: dict) -> dict:
        """Call public/auth and return the parsed response."""
        url = f"{self.base_url}/api/v2/public/auth"
        body = {
            "jsonrpc": "2.0",
            "id": int(time.time() * 1000),
            "method": "public/auth",
            "params": params,
        }
        resp = self._session.post(
            url, json=body, headers={"Content-Type": "application/json"},
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )
        parsed = self._parse(resp)
        if "error" in parsed:
            raise RuntimeError(f"Deribit auth failed: {parsed['error']}")
        return parsed

    def _apply_token(self, resp: dict):
        """Extract tokens from auth response and set expiry schedule."""
        result = resp["result"]
        self._access_token = result["access_token"]
        self._refresh_token = result["refresh_token"]
        ttl = result.get("expires_in", 900)
        now = time.time()
        self._token_expires_at = now + ttl
        self._token_refresh_at = now + ttl * _REFRESH_FRACTION
        logger.info(
            f"Deribit: token acquired (TTL={ttl}s, refresh in {int(ttl * _REFRESH_FRACTION)}s)"
        )

    # ── HTTP helpers ─────────────────────────────────────────────────

    @retry(
        max_attempts=3,
        backoff_factor=1.0,
        exceptions=(requests.ConnectionError, requests.Timeout),
    )
    def _request_with_retry(
        self, method: str, url: str, json_body: dict = None, **kwargs
    ) -> requests.Response:
        headers = {"Content-Type": "application/json"}
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if method == "GET":
            return self._session.get(url, headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT)
        return self._session.post(url, json=json_body, headers=headers, timeout=DEFAULT_REQUEST_TIMEOUT)

    @staticmethod
    def _parse(resp: requests.Response) -> dict:
        """
        Parse a Deribit HTTP response.

        Deribit returns HTTP 200 for success and HTTP 400 for ALL errors.
        The real status is in the JSON body ('result' vs 'error').
        """
        try:
            data = resp.json()
        except ValueError:
            return {"error": {"code": -1, "message": f"Invalid JSON: {resp.text[:200]}"}}

        # JSON-RPC envelope: unwrap for consistency but keep error structure
        if "error" in data:
            return {"error": data["error"]}
        if "result" in data:
            return {"result": data["result"]}
        # Bare response (e.g., public GET with query params)
        return data
