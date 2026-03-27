#!/usr/bin/env python3
"""
Coincall API Authentication Module

Handles all API authentication and request signing according to Coincall API v2.0.1 spec.
This module abstracts away authentication details from higher-level modules.

Authentication:
  - Signature prehash includes request parameters as query string format
  - POST: prehash = METHOD + ENDPOINT + ?param1=val1&param2=val2&uuid=key&ts=ts&x-req-ts-diff=diff
  - Signature: HMAC-SHA256(api_secret, prehash).hexdigest().upper()

Content Types:
  - Most POST endpoints: application/json (default)
  - RFQ accept/cancel endpoints: application/x-www-form-urlencoded (use_form_data=True)
"""

import hashlib
import hmac
import json
import time
import logging
import requests
from typing import Dict, Any, Optional

from retry import retry

logger = logging.getLogger(__name__)

# Default timeout for all API requests (30 seconds)
DEFAULT_REQUEST_TIMEOUT = 30.0


class CoincallAuth:
    """Handles Coincall API authentication and request signing"""

    # After this many consecutive request failures, mark exchange as unreachable
    _UNREACHABLE_THRESHOLD = 3
    # After this many consecutive failures, recreate the HTTP session
    _SESSION_REFRESH_THRESHOLD = 5

    def __init__(self, api_key: str, api_secret: str, base_url: str):
        """
        Initialize authentication handler
        
        Args:
            api_key: Coincall API key
            api_secret: Coincall API secret
            base_url: Base URL for API (e.g., https://api.coincall.com)
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url
        self.session = requests.Session()
        self._consecutive_failures = 0

    @property
    def reachable(self) -> bool:
        """True when the exchange is responding normally."""
        return self._consecutive_failures < self._UNREACHABLE_THRESHOLD

    def _record_success(self) -> None:
        """Reset failure counter on a successful request."""
        if self._consecutive_failures > 0:
            logger.info(
                f"Coincall connection restored after {self._consecutive_failures} failure(s)"
            )
        self._consecutive_failures = 0

    def _record_failure(self) -> None:
        """Increment failure counter; refresh session after sustained failures."""
        self._consecutive_failures += 1
        if self._consecutive_failures == self._UNREACHABLE_THRESHOLD:
            logger.warning(
                f"Coincall marked UNREACHABLE after "
                f"{self._consecutive_failures} consecutive failures"
            )
        if self._consecutive_failures >= self._SESSION_REFRESH_THRESHOLD:
            logger.warning("Refreshing HTTP session to drop stale connections")
            try:
                self.session.close()
            except Exception:
                pass
            self.session = requests.Session()

    def _create_signature(
        self, 
        method: str, 
        endpoint: str, 
        ts: int, 
        x_req_ts_diff: int = 5000,
        data: Optional[Dict[str, Any]] = None
    ) -> str:
        """Create HMAC SHA256 signature for API request."""
        def flatten_params(d):
            """Flatten dict to sorted list of (key, value) tuples for query string."""
            items = []
            for k, v in sorted(d.items()):
                if v is None:
                    continue
                if isinstance(v, (dict, list)):
                    v = json.dumps(v, separators=(',', ':'))
                items.append((k, str(v)))
            return items
        
        # Build prehash: METHOD + ENDPOINT + ?params&uuid=...&ts=...&x-req-ts-diff=...
        prehash = f'{method}{endpoint}'
        
        if method.upper() == 'POST' and data:
            param_list = flatten_params(data)
            if param_list:
                prehash += '?' + '&'.join(f"{k}={v}" for k, v in param_list)
        
        # Append auth parameters
        auth_suffix = f"uuid={self.api_key}&ts={ts}&x-req-ts-diff={x_req_ts_diff}"
        prehash += ('&' if '?' in prehash else '?') + auth_suffix
        
        # Sign the prehash
        return hmac.new(
            self.api_secret.encode('utf-8'),
            prehash.encode('utf-8'),
            hashlib.sha256
        ).hexdigest().upper()

    def _get_headers(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
        """Get authentication headers for API request."""
        ts = int(time.time() * 1000)
        x_req_ts_diff = 5000
        signature = self._create_signature(method, endpoint, ts, x_req_ts_diff, data)
        
        return {
            'X-CC-APIKEY': self.api_key,
            'sign': signature,
            'ts': str(ts),
            'X-REQ-TS-DIFF': str(x_req_ts_diff),
            'Content-Type': 'application/json'
        }

    @retry(
        max_attempts=3,
        backoff_factor=1.0,
        exceptions=(
            requests.ConnectionError,
            requests.Timeout,
        )
    )
    def _request_with_timeout(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        data: Optional[Dict[str, Any]] = None,
        use_form_data: bool = False,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> requests.Response:
        """
        Internal request method with automatic retry on transient failures.
        
        Retries on connection errors, timeouts, and 5xx server errors.
        Raises on client errors (4xx) and after max retries exceeded.
        """
        if method.upper() == 'GET':
            return self.session.get(url, headers=headers, timeout=timeout)
        elif method.upper() == 'POST':
            if use_form_data and data:
                headers = dict(headers)
                headers['Content-Type'] = 'application/x-www-form-urlencoded'
                return self.session.post(url, data=data, headers=headers, timeout=timeout)
            else:
                return self.session.post(url, json=data, headers=headers, timeout=timeout)
        else:
            raise ValueError(f"Unsupported HTTP method: {method}")

    def request(
        self, 
        method: str, 
        endpoint: str, 
        data: Optional[Dict[str, Any]] = None,
        use_form_data: bool = False,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> Dict[str, Any]:
        """
        Make authenticated API request with timeout and automatic retries.
        
        Args:
            method: HTTP method ('GET' or 'POST')
            endpoint: API endpoint (e.g., '/open/option/order/create/v1')
            data: Request payload (for POST)
            use_form_data: Use form-urlencoded instead of JSON
            timeout: Request timeout in seconds (default 30s)
        
        Returns:
            Parsed JSON response dict, or error dict on failure
        """
        headers = self._get_headers(method, endpoint, data)
        url = f'{self.base_url}{endpoint}'
        
        try:
            response = self._request_with_timeout(
                method=method,
                url=url,
                headers=headers,
                data=data,
                use_form_data=use_form_data,
                timeout=timeout,
            )
            response.raise_for_status()
            self._record_success()
            return response.json()
        except requests.HTTPError as e:
            # Client errors (4xx) are valid responses — exchange is reachable
            if e.response is not None and e.response.status_code < 500:
                self._record_success()
            else:
                self._record_failure()
            logger.error(f"HTTP error {e.response.status_code}: {e}")
            return {'code': e.response.status_code, 'msg': str(e), 'data': None}
        except requests.Timeout as e:
            self._record_failure()
            logger.error(f"API request timeout after {timeout}s: {e}")
            return {'code': 408, 'msg': 'Request timeout', 'data': None}
        except requests.RequestException as e:
            self._record_failure()
            logger.error(f"API request failed (after retries): {e}")
            return {'code': 500, 'msg': str(e), 'data': None}

    def get(self, endpoint: str) -> Dict[str, Any]:
        """Make GET request"""
        return self.request('GET', endpoint)

    def post(self, endpoint: str, data: Dict[str, Any], use_form_data: bool = False) -> Dict[str, Any]:
        """Make POST request"""
        return self.request('POST', endpoint, data, use_form_data)

    def is_successful(self, response: Dict[str, Any]) -> bool:
        """Check if API response code is 0 (success)."""
        return response.get('code') == 0
