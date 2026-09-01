"""Razorpay Webhook verification, deduplication, and event normalization.

PROJECT_RULES 10.8, 10.9 / ARCHITECTURE.md §12.3.

Responsibilities:
- Constant-time HMAC-SHA256 signature verification on raw request bytes.
- Strict rejection of unsigned, forged, or secret-mismatched webhooks.
- Idempotent deduplication of event deliveries.
- Normalization of Razorpay payment payloads into FinPilot domain contracts.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from ..domain.canonical import digest
from ..domain.enums import Currency, PaymentMethod, PaymentStatus
from ..domain.errors import DomainValidationError
from ..domain.payment import Payment
from ..ingestion.normalizer import PaymentNormalizer
from .config import RazorpayConfig


class WebhookVerificationError(Exception):
    """Raised when webhook signature verification fails."""
    pass


class WebhookPayloadError(Exception):
    """Raised when webhook body is malformed or missing required envelope fields."""
    pass


@dataclass(frozen=True)
class WebhookProcessingResult:
    """Outcome of processing an incoming Razorpay webhook."""

    success: bool
    event_id: Optional[str]
    event_type: Optional[str]
    is_duplicate: bool
    status_code: int
    message: str
    normalized_payment: Optional[Payment] = None
    raw_payload: Optional[Dict[str, Any]] = None


class RazorpayWebhookHandler:
    """Verifies and processes incoming Razorpay webhook payloads."""

    SUPPORTED_EVENTS: Set[str] = {
        "payment.authorized",
        "payment.captured",
        "payment.failed",
        "payment.disputed",
        "payment_link.paid",
        "payment_link.partially_paid",
        "payment_link.cancelled",
        "payment_link.expired",
        "order.paid",
        "refund.created",
        "refund.processed",
        "refund.failed",
    }

    def __init__(
        self,
        config: Optional[RazorpayConfig] = None,
        normalizer: Optional[PaymentNormalizer] = None,
    ) -> None:
        self._config = config or RazorpayConfig.from_env()
        self._normalizer = normalizer or PaymentNormalizer()
        self._processed_events: Set[str] = set()

    @property
    def config(self) -> RazorpayConfig:
        return self._config

    @property
    def normalizer(self) -> PaymentNormalizer:
        return self._normalizer

    def verify_signature(self, raw_body: bytes, signature: Optional[str]) -> bool:
        """Verify HMAC-SHA256 signature over raw request bytes.

        Args:
            raw_body: The exact raw bytes of the incoming HTTP request body.
            signature: The value of the 'X-Razorpay-Signature' header.

        Returns:
            True if the signature matches using constant-time comparison.

        Raises:
            WebhookVerificationError: If webhook secret is not configured or signature is missing.
        """
        secret = self._config.webhook_secret
        if not secret:
            raise WebhookVerificationError("RAZORPAY_WEBHOOK_SECRET is not configured on the server.")

        if not signature or not signature.strip():
            return False

        computed_signature = hmac.new(
            secret.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(computed_signature, signature.strip())

    def is_event_processed(self, event_id: str) -> bool:
        """Check if an event ID has already been processed."""
        return event_id in self._processed_events

    def mark_event_processed(self, event_id: str) -> None:
        """Record an event ID as processed for idempotency."""
        self._processed_events.add(event_id)

    def parse_and_validate(
        self, raw_body: bytes, signature: Optional[str]
    ) -> Tuple[Dict[str, Any], str, str]:
        """Verify signature and parse raw JSON into a validated event envelope.

        Returns:
            Tuple of (parsed_payload, event_id, event_type).
        """
        if not self.verify_signature(raw_body, signature):
            raise WebhookVerificationError("Invalid or missing X-Razorpay-Signature.")

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception as exc:
            raise WebhookPayloadError(f"Malformed JSON in webhook body: {str(exc)}") from exc

        if not isinstance(payload, dict):
            raise WebhookPayloadError("Webhook root payload must be a JSON object.")

        event_type = payload.get("event")
        if not event_type or not isinstance(event_type, str):
            raise WebhookPayloadError("Webhook missing required 'event' field.")

        event_id = payload.get("event_id") or payload.get("id")
        if not event_id:
            # Fall back to deterministic digest of payload + timestamp
            created_at_val = payload.get("created_at", "")
            raw_str = raw_body.decode(errors="ignore")
            event_id = f"evt_{digest(f'{event_type}:{created_at_val}:{raw_str}')[:16]}"

        return payload, str(event_id), event_type

    def extract_payment_dict(self, payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        """Extract payment dictionary from standard Razorpay webhook envelope."""
        # Standard Razorpay structure: payload.payment.entity
        payload_section = payload.get("payload", {})
        if isinstance(payload_section, dict):
            payment_sec = payload_section.get("payment", {})
            if isinstance(payment_sec, dict) and "entity" in payment_sec:
                return dict(payment_sec["entity"])
            # In case payload is directly entity
            if "entity" in payload_section:
                return dict(payload_section["entity"])

        # Fallback if payment is top-level
        if "payment" in payload and isinstance(payload["payment"], dict):
            return dict(payload["payment"].get("entity", payload["payment"]))

        # Direct payment entity payload
        if payload.get("entity") == "payment":
            return dict(payload)

        return None

    def process_webhook(
        self, raw_body: bytes, signature: Optional[str]
    ) -> WebhookProcessingResult:
        """Verify, deduplicate, and normalize an incoming webhook payload.

        Args:
            raw_body: Raw request body bytes.
            signature: X-Razorpay-Signature header string.

        Returns:
            WebhookProcessingResult with normalized domain Payment if applicable.
        """
        try:
            payload, event_id, event_type = self.parse_and_validate(raw_body, signature)
        except WebhookVerificationError as exc:
            return WebhookProcessingResult(
                success=False,
                event_id=None,
                event_type=None,
                is_duplicate=False,
                status_code=401,
                message=f"Webhook signature verification failed: {str(exc)}",
            )
        except WebhookPayloadError as exc:
            return WebhookProcessingResult(
                success=False,
                event_id=None,
                event_type=None,
                is_duplicate=False,
                status_code=400,
                message=f"Invalid webhook payload: {str(exc)}",
            )

        # Idempotency check
        if self.is_event_processed(event_id):
            return WebhookProcessingResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                is_duplicate=True,
                status_code=200,
                message=f"Event '{event_id}' already processed (idempotent skip).",
                raw_payload=payload,
            )

        # Event type check
        if event_type not in self.SUPPORTED_EVENTS:
            self.mark_event_processed(event_id)
            return WebhookProcessingResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                is_duplicate=False,
                status_code=200,
                message=f"Event '{event_type}' ignored (non-telemetry event).",
                raw_payload=payload,
            )

        # Extract payment
        payment_dict = self.extract_payment_dict(payload)
        if not payment_dict:
            self.mark_event_processed(event_id)
            return WebhookProcessingResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                is_duplicate=False,
                status_code=200,
                message=f"Event '{event_type}' processed with no payment entity.",
                raw_payload=payload,
            )

        # Normalize to domain Payment
        try:
            normalized_payment = self._normalizer.normalize(payment_dict)
            self.mark_event_processed(event_id)
            return WebhookProcessingResult(
                success=True,
                event_id=event_id,
                event_type=event_type,
                is_duplicate=False,
                status_code=200,
                message=f"Payment '{normalized_payment.id}' successfully normalized from '{event_type}'.",
                normalized_payment=normalized_payment,
                raw_payload=payload,
            )
        except Exception as exc:
            return WebhookProcessingResult(
                success=False,
                event_id=event_id,
                event_type=event_type,
                is_duplicate=False,
                status_code=422,
                message=f"Payment normalization failed: {str(exc)}",
                raw_payload=payload,
            )
