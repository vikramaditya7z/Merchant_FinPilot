"""Unit tests for Razorpay Webhook Handler.

PROJECT_RULES 10.8, 10.9 / ARCHITECTURE.md §12.3.
"""

import hashlib
import hmac
import json
import unittest

from backend.domain.enums import Currency, PaymentMethod, PaymentStatus
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.webhook import (
    RazorpayWebhookHandler,
    WebhookPayloadError,
    WebhookProcessingResult,
    WebhookVerificationError,
)


class TestRazorpayWebhook(unittest.TestCase):
    """Test suite for signature verification, deduplication, and payload normalization."""

    def setUp(self) -> None:
        self.secret = "whsec_test_secret_998877"
        self.config = RazorpayConfig(
            key_id="rzp_test_key",
            key_secret="rzp_test_secret",
            webhook_secret=self.secret,
        )
        self.handler = RazorpayWebhookHandler(config=self.config)

    def _sign(self, body_bytes: bytes) -> str:
        """Helper to generate valid HMAC-SHA256 signature."""
        return hmac.new(
            self.secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()

    def test_valid_signature_verification(self) -> None:
        """Verify valid signature is accepted."""
        body = b'{"event":"payment.captured"}'
        sig = self._sign(body)
        self.assertTrue(self.handler.verify_signature(body, sig))

    def test_invalid_signature_rejection(self) -> None:
        """Verify invalid/tampered signature is rejected."""
        body = b'{"event":"payment.captured"}'
        self.assertFalse(self.handler.verify_signature(body, "invalid_signature_hex"))

    def test_tampered_body_rejection(self) -> None:
        """Verify signature generated on different payload is rejected."""
        sig = self._sign(b'{"event":"payment.captured","amount":100}')
        tampered_body = b'{"event":"payment.captured","amount":200}'
        self.assertFalse(self.handler.verify_signature(tampered_body, sig))

    def test_missing_signature_rejection(self) -> None:
        """Verify missing/empty signature is rejected."""
        body = b'{"event":"payment.captured"}'
        self.assertFalse(self.handler.verify_signature(body, None))
        self.assertFalse(self.handler.verify_signature(body, ""))

    def test_unconfigured_secret_raises_error(self) -> None:
        """Verify handler raises WebhookVerificationError if secret is missing."""
        no_sec_handler = RazorpayWebhookHandler(config=RazorpayConfig())
        with self.assertRaises(WebhookVerificationError):
            no_sec_handler.verify_signature(b'{}', "some_sig")

    def test_process_payment_captured_event(self) -> None:
        """Verify processing and normalizing standard payment.captured webhook."""
        payload = {
            "entity": "event",
            "event_id": "evt_capture_001",
            "event": "payment.captured",
            "created_at": 1700001000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_captured_123",
                        "entity": "payment",
                        "amount": 250000,  # 2,500.00 INR
                        "currency": "INR",
                        "status": "captured",
                        "order_id": "order_xyz_456",
                        "method": "upi",
                        "created_at": 1700001000,
                    }
                }
            }
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = self._sign(raw_body)

        result = self.handler.process_webhook(raw_body, sig)
        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.event_id, "evt_capture_001")
        self.assertFalse(result.is_duplicate)
        self.assertIsNotNone(result.normalized_payment)

        p = result.normalized_payment
        self.assertEqual(p.id, "pay_captured_123")
        self.assertEqual(p.amount.minor_units, 250000)
        self.assertEqual(p.status, PaymentStatus.CAPTURED)
        self.assertEqual(p.method, PaymentMethod.UPI)
        self.assertEqual(p.order_id, "order_xyz_456")

    def test_process_payment_failed_event(self) -> None:
        """Verify processing and normalizing payment.failed with error codes."""
        payload = {
            "entity": "event",
            "event_id": "evt_failed_002",
            "event": "payment.failed",
            "created_at": 1700002000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_failed_999",
                        "entity": "payment",
                        "amount": 100000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "card",
                        "created_at": 1700002000,
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment was declined by issuer bank",
                        "error_source": "issuer",
                        "error_step": "payment_authentication",
                        "error_reason": "payment_failed",
                    }
                }
            }
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = self._sign(raw_body)

        result = self.handler.process_webhook(raw_body, sig)
        self.assertTrue(result.success)
        self.assertEqual(result.event_id, "evt_failed_002")
        self.assertIsNotNone(result.normalized_payment)

        p = result.normalized_payment
        self.assertEqual(p.id, "pay_failed_999")
        self.assertEqual(p.status, PaymentStatus.FAILED)
        self.assertEqual(p.method, PaymentMethod.CARD)
        self.assertEqual(p.error_code, "BAD_REQUEST_ERROR")
        self.assertEqual(p.error_description, "Payment was declined by issuer bank")
        self.assertEqual(p.error_source, "issuer")

    def test_idempotent_duplicate_webhook_processing(self) -> None:
        """Verify the exact same webhook is deduplicated and marked duplicate."""
        payload = {
            "event_id": "evt_dedup_001",
            "event": "payment.captured",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_dedup_1",
                        "amount": 5000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "created_at": 1700000000,
                    }
                }
            }
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = self._sign(raw_body)

        # 1. First delivery
        r1 = self.handler.process_webhook(raw_body, sig)
        self.assertTrue(r1.success)
        self.assertFalse(r1.is_duplicate)
        self.assertEqual(r1.status_code, 200)

        # 2. Duplicate redelivery
        r2 = self.handler.process_webhook(raw_body, sig)
        self.assertTrue(r2.success)
        self.assertTrue(r2.is_duplicate)
        self.assertEqual(r2.status_code, 200)
        self.assertIn("already processed", r2.message)

    def test_unsupported_event_type_handled_gracefully(self) -> None:
        """Verify unsupported/non-payment events return 200 OK without failing."""
        payload = {
            "event_id": "evt_invoice_paid",
            "event": "invoice.paid",
            "payload": {"invoice": {"entity": {"id": "inv_123"}}}
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = self._sign(raw_body)

        result = self.handler.process_webhook(raw_body, sig)
        self.assertTrue(result.success)
        self.assertEqual(result.status_code, 200)
        self.assertIn("ignored", result.message)

    def test_malformed_json_body_returns_400(self) -> None:
        """Verify malformed JSON body returns 400."""
        malformed_bytes = b"not_a_valid_json_at_all{"
        sig = self._sign(malformed_bytes)

        result = self.handler.process_webhook(malformed_bytes, sig)
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 400)
        self.assertIn("Malformed JSON", result.message)

    def test_invalid_signature_returns_401(self) -> None:
        """Verify invalid signature returns 401."""
        body = b'{"event":"payment.captured"}'
        result = self.handler.process_webhook(body, "bad_signature")
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 401)
        self.assertIn("signature verification failed", result.message)


if __name__ == "__main__":
    unittest.main()
