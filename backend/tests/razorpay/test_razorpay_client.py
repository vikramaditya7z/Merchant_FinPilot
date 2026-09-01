"""Unit tests for Razorpay REST API Client.

PROJECT_RULES 10.8, 10.9 / ARCHITECTURE.md §12.
"""

import io
import json
import socket
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from backend.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayClient,
    RazorpayConnectionError,
    RazorpayNotFoundError,
    RazorpayServerError,
    RazorpayTimeoutError,
)
from backend.razorpay.config import RazorpayConfig


class TestRazorpayClient(unittest.TestCase):
    """Test suite for RazorpayClient authentication, error handling, and API methods."""

    def setUp(self) -> None:
        self.config = RazorpayConfig(
            key_id="rzp_test_12345",
            key_secret="secret_abcde",
            webhook_secret="whsec_xyz",
            api_base_url="https://api.razorpay.com/v1",
            timeout_seconds=5.0,
        )
        self.client = RazorpayClient(config=self.config)

    def test_auth_header_generation(self) -> None:
        """Verify standard HTTP Basic Authentication header encoding."""
        header = self.client._get_auth_header()
        self.assertTrue(header.startswith("Basic "))
        # Base64 of 'rzp_test_12345:secret_abcde'
        self.assertEqual(header, "Basic cnpwX3Rlc3RfMTIzNDU6c2VjcmV0X2FiY2Rl")

    def test_missing_credentials_raises_auth_error(self) -> None:
        """Client must fail safely if credentials are missing."""
        empty_client = RazorpayClient(config=RazorpayConfig())
        with self.assertRaises(RazorpayAuthError):
            empty_client._get_auth_header()

    @patch("urllib.request.urlopen")
    def test_fetch_payment_success(self, mock_urlopen: MagicMock) -> None:
        """Verify successful single payment fetch."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "id": "pay_test_001",
            "entity": "payment",
            "amount": 50000,
            "currency": "INR",
            "status": "captured",
            "method": "upi",
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        payment = self.client.fetch_payment("pay_test_001")
        self.assertEqual(payment["id"], "pay_test_001")
        self.assertEqual(payment["amount"], 50000)
        self.assertEqual(payment["status"], "captured")

    @patch("urllib.request.urlopen")
    def test_fetch_payments_list_success(self, mock_urlopen: MagicMock) -> None:
        """Verify list payments query with timestamps."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "entity": "collection",
            "count": 2,
            "items": [
                {"id": "pay_01", "amount": 10000, "status": "captured"},
                {"id": "pay_02", "amount": 20000, "status": "failed"},
            ],
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        res = self.client.fetch_payments(from_timestamp=1700000000, to_timestamp=1700003600, count=10)
        self.assertEqual(res["count"], 2)
        self.assertEqual(len(res["items"]), 2)

    @patch("urllib.request.urlopen")
    def test_fetch_order_success(self, mock_urlopen: MagicMock) -> None:
        """Verify fetch order by ID."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "id": "order_test_99",
            "entity": "order",
            "amount": 75000,
            "status": "paid",
        }).encode("utf-8")
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        order = self.client.fetch_order("order_test_99")
        self.assertEqual(order["id"], "order_test_99")
        self.assertEqual(order["status"], "paid")

    @patch("urllib.request.urlopen")
    def test_401_unauthorized_error(self, mock_urlopen: MagicMock) -> None:
        """Verify 401 HTTP response translates to RazorpayAuthError."""
        err_body = json.dumps({"error": {"description": "Invalid API Key"}}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://api.razorpay.com/v1/payments",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=io.BytesIO(err_body),
        )
        mock_urlopen.side_effect = http_err

        with self.assertRaises(RazorpayAuthError) as ctx:
            self.client.fetch_payment("pay_invalid")
        self.assertEqual(ctx.exception.status_code, 401)
        self.assertIn("Invalid API Key", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_404_not_found_error(self, mock_urlopen: MagicMock) -> None:
        """Verify 404 HTTP response translates to RazorpayNotFoundError."""
        err_body = json.dumps({"error": {"description": "Payment not found"}}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://api.razorpay.com/v1/payments/pay_nonexistent",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=io.BytesIO(err_body),
        )
        mock_urlopen.side_effect = http_err

        with self.assertRaises(RazorpayNotFoundError) as ctx:
            self.client.fetch_payment("pay_nonexistent")
        self.assertEqual(ctx.exception.status_code, 404)

    @patch("urllib.request.urlopen")
    def test_500_upstream_server_error(self, mock_urlopen: MagicMock) -> None:
        """Verify 5xx HTTP response translates to RazorpayServerError."""
        err_body = json.dumps({"error": {"description": "Internal Server Error"}}).encode("utf-8")
        http_err = urllib.error.HTTPError(
            url="https://api.razorpay.com/v1/payments",
            code=502,
            msg="Bad Gateway",
            hdrs={},
            fp=io.BytesIO(err_body),
        )
        mock_urlopen.side_effect = http_err

        with self.assertRaises(RazorpayServerError) as ctx:
            self.client.fetch_payment("pay_123")
        self.assertEqual(ctx.exception.status_code, 502)

    @patch("urllib.request.urlopen")
    def test_timeout_error(self, mock_urlopen: MagicMock) -> None:
        """Verify network timeout raises RazorpayTimeoutError."""
        mock_urlopen.side_effect = socket.timeout("timed out")
        with self.assertRaises(RazorpayTimeoutError):
            self.client.fetch_payment("pay_timeout")

    @patch("urllib.request.urlopen")
    def test_connection_error(self, mock_urlopen: MagicMock) -> None:
        """Verify network socket failure raises RazorpayConnectionError."""
        mock_urlopen.side_effect = urllib.error.URLError("DNS resolution failed")
        with self.assertRaises(RazorpayConnectionError):
            self.client.fetch_payment("pay_conn")


if __name__ == "__main__":
    unittest.main()
