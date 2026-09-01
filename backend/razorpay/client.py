"""Razorpay HTTP REST API client.

PROJECT_RULES 10.8, 10.9 / ARCHITECTURE.md §12.

Handles HTTP communication with Razorpay REST endpoints:
- HTTP Basic Authentication (Key ID : Key Secret)
- Standard library urllib.request (zero required third-party dependencies)
- Deterministic timeout and socket error handling
- Structured domain-safe exception normalization
- Pure telemetry / query operations (NO MUTATIONS).
"""

import base64
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Mapping, Optional

from .config import RazorpayConfig


class RazorpayError(Exception):
    """Base exception for all Razorpay client errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, error_data: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_data = error_data or {}


class RazorpayAuthError(RazorpayError):
    """Raised when authentication fails (HTTP 401)."""
    pass


class RazorpayNotFoundError(RazorpayError):
    """Raised when a requested resource is not found (HTTP 404)."""
    pass


class RazorpayAPIError(RazorpayError):
    """Raised when Razorpay returns a 4xx error."""
    pass


class RazorpayServerError(RazorpayError):
    """Raised when Razorpay returns a 5xx upstream server error."""
    pass


class RazorpayTimeoutError(RazorpayError):
    """Raised when an HTTP request times out."""
    pass


class RazorpayConnectionError(RazorpayError):
    """Raised on socket/network transport failures."""
    pass


class RazorpayClient:
    """Standard-library HTTP REST client for Razorpay API queries."""

    def __init__(self, config: Optional[RazorpayConfig] = None) -> None:
        self._config = config or RazorpayConfig.from_env()

    @property
    def config(self) -> RazorpayConfig:
        return self._config

    def _get_auth_header(self) -> str:
        if not self._config.key_id or not self._config.key_secret:
            raise RazorpayAuthError("Razorpay credentials (KEY_ID and KEY_SECRET) are not configured.")
        raw_credentials = f"{self._config.key_id}:{self._config.key_secret}"
        encoded = base64.b64encode(raw_credentials.encode("utf-8")).decode("ascii")
        return f"Basic {encoded}"

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute an authenticated HTTP request to the Razorpay API."""
        url = f"{self._config.api_base_url.rstrip('/')}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            url = f"{url}?{query}"

        headers = {
            "Authorization": self._get_auth_header(),
            "Accept": "application/json",
            "User-Agent": "Merchant-FinPilot/2.0",
        }

        body_bytes = None
        if data is not None:
            body_bytes = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(
            url=url,
            data=body_bytes,
            headers=headers,
            method=method.upper(),
        )

        ssl_cafile = os.environ.get("SSL_CERT_FILE")
        ssl_ctx = (
            ssl.create_default_context(cafile=ssl_cafile)
            if ssl_cafile and os.path.exists(ssl_cafile)
            else None
        )

        try:
            with urllib.request.urlopen(req, timeout=self._config.timeout_seconds, context=ssl_ctx) as response:
                response_bytes = response.read()
                if not response_bytes:
                    return {}
                return json.loads(response_bytes.decode("utf-8"))
        except urllib.error.HTTPError as err:
            status_code = err.code
            err_body = err.read()
            err_json: Dict[str, Any] = {}
            if err_body:
                try:
                    err_json = json.loads(err_body.decode("utf-8"))
                except Exception:
                    err_json = {"raw_error": err_body.decode("utf-8", errors="replace")}

            err_desc = (
                err_json.get("error", {}).get("description")
                or err_json.get("error_description")
                or str(err)
            )

            if status_code == 401:
                raise RazorpayAuthError(f"Razorpay authentication failed: {err_desc}", status_code=401, error_data=err_json) from err
            if status_code == 404:
                raise RazorpayNotFoundError(f"Razorpay resource not found at '{path}': {err_desc}", status_code=404, error_data=err_json) from err
            if 400 <= status_code < 500:
                raise RazorpayAPIError(f"Razorpay API client error ({status_code}): {err_desc}", status_code=status_code, error_data=err_json) from err
            raise RazorpayServerError(f"Razorpay upstream server error ({status_code}): {err_desc}", status_code=status_code, error_data=err_json) from err

        except (urllib.error.URLError, socket.timeout, TimeoutError) as err:
            if isinstance(err, (socket.timeout, TimeoutError)) or "timed out" in str(err).lower():
                raise RazorpayTimeoutError(f"Razorpay request to '{path}' timed out after {self._config.timeout_seconds}s") from err
            raise RazorpayConnectionError(f"Razorpay connection failed for '{path}': {str(err)}") from err

    def fetch_payment(self, payment_id: str) -> Dict[str, Any]:
        """Fetch a single payment by ID (GET /v1/payments/{payment_id})."""
        if not payment_id or not payment_id.strip():
            raise RazorpayAPIError("payment_id cannot be empty")
        return self.request("GET", f"payments/{urllib.parse.quote(payment_id.strip())}")

    def fetch_payments(
        self,
        from_timestamp: Optional[int] = None,
        to_timestamp: Optional[int] = None,
        count: int = 100,
        skip: int = 0,
    ) -> Dict[str, Any]:
        """Fetch multiple payments within a time range (GET /v1/payments)."""
        params: Dict[str, Any] = {"count": count, "skip": skip}
        if from_timestamp is not None:
            params["from"] = int(from_timestamp)
        if to_timestamp is not None:
            params["to"] = int(to_timestamp)
        return self.request("GET", "payments", params=params)

    def fetch_order(self, order_id: str) -> Dict[str, Any]:
        """Fetch an order by ID (GET /v1/orders/{order_id})."""
        if not order_id or not order_id.strip():
            raise RazorpayAPIError("order_id cannot be empty")
        return self.request("GET", f"orders/{urllib.parse.quote(order_id.strip())}")

    def create_payment_link(
        self,
        amount_minor_units: int,
        currency: str = "INR",
        description: str = "",
        reference_id: Optional[str] = None,
        customer: Optional[Dict[str, Any]] = None,
        notify: Optional[Dict[str, bool]] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create a payment link (POST /v1/payment_links)."""
        if amount_minor_units <= 0:
            raise RazorpayAPIError(f"amount_minor_units must be positive, got {amount_minor_units}")
        payload: Dict[str, Any] = {
            "amount": int(amount_minor_units),
            "currency": currency,
            "description": description,
        }
        if reference_id:
            payload["reference_id"] = str(reference_id)
        if customer:
            payload["customer"] = dict(customer)
        if notify:
            payload["notify"] = dict(notify)
        if notes:
            payload["notes"] = dict(notes)

        return self.request("POST", "payment_links", data=payload)

