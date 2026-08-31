"""Payment normalizer.

PROJECT_RULES 1.6, 2.3, 2.8 / ARCHITECTURE.md §12.

Converts arbitrary external payment payloads into immutable domain ``Payment`` instances.
Enforces exact monetary representation, UTC timezone normalization, status/method mapping,
and strict error-field invariants.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Optional, Tuple, Union

from ..domain.enums import Currency, PaymentMethod, PaymentStatus
from ..domain.errors import DomainValidationError, MoneyPrecisionError
from ..domain.money import Money
from ..domain.payment import Payment
from ..domain.window import UTC, from_unix_seconds, require_utc

STATUS_MAPPING = {
    "created": PaymentStatus.CREATED,
    "pending": PaymentStatus.CREATED,
    "initiated": PaymentStatus.CREATED,
    "in_flight": PaymentStatus.CREATED,
    "attempted": PaymentStatus.CREATED,
    "authorized": PaymentStatus.AUTHORIZED,
    "auth": PaymentStatus.AUTHORIZED,
    "captured": PaymentStatus.CAPTURED,
    "success": PaymentStatus.CAPTURED,
    "successful": PaymentStatus.CAPTURED,
    "paid": PaymentStatus.CAPTURED,
    "settled": PaymentStatus.CAPTURED,
    "complete": PaymentStatus.CAPTURED,
    "completed": PaymentStatus.CAPTURED,
    "refunded": PaymentStatus.REFUNDED,
    "failed": PaymentStatus.FAILED,
    "failure": PaymentStatus.FAILED,
    "declined": PaymentStatus.FAILED,
    "error": PaymentStatus.FAILED,
}

METHOD_MAPPING = {
    "card": PaymentMethod.CARD,
    "credit_card": PaymentMethod.CARD,
    "debit_card": PaymentMethod.CARD,
    "credit": PaymentMethod.CARD,
    "debit": PaymentMethod.CARD,
    "upi": PaymentMethod.UPI,
    "vpa": PaymentMethod.UPI,
    "bhim": PaymentMethod.UPI,
    "googlepay": PaymentMethod.UPI,
    "phonepe": PaymentMethod.UPI,
    "paytm": PaymentMethod.UPI,
    "netbanking": PaymentMethod.NETBANKING,
    "nb": PaymentMethod.NETBANKING,
    "internet_banking": PaymentMethod.NETBANKING,
    "online_banking": PaymentMethod.NETBANKING,
    "wallet": PaymentMethod.WALLET,
    "prepaid": PaymentMethod.WALLET,
    "emi": PaymentMethod.EMI,
    "unknown": PaymentMethod.UNKNOWN,
}


class PaymentNormalizer:
    """Converts raw payment dictionaries into strongly validated domain Payment contracts."""

    def normalize(self, payload: Mapping[str, Any]) -> Payment:
        """Normalize a single payment payload dictionary into a domain Payment.

        Args:
            payload: Generic dictionary representing an ingested payment event.

        Returns:
            An immutable, validated domain ``Payment`` instance.

        Raises:
            DomainValidationError: If required fields are missing, invalid, or malformed.
            MoneyPrecisionError: If amount precision is violated.
        """
        if not isinstance(payload, Mapping):
            raise DomainValidationError(
                f"payload must be a Mapping/dict, got {type(payload).__name__}"
            )

        # 1. Identifier
        payment_id = self._extract_id(payload)

        # 2. Currency
        currency = self._extract_currency(payload)

        # 3. Amount
        amount = self._extract_amount(payload, currency=currency)

        # 4. Status
        status = self._extract_status(payload)

        # 5. Method
        method = self._extract_method(payload)

        # 6. Created At (Timestamp)
        created_at = self._extract_timestamp(payload)

        # 7. Order ID
        order_id = self._extract_order_id(payload)

        # 8. Error Fields (Preserved only for failed payments)
        error_fields = self._extract_error_fields(payload, status=status)

        return Payment(
            id=payment_id,
            created_at=created_at,
            amount=amount,
            status=status,
            method=method,
            order_id=order_id,
            error_code=error_fields.get("error_code"),
            error_description=error_fields.get("error_description"),
            error_source=error_fields.get("error_source"),
            error_step=error_fields.get("error_step"),
            error_reason=error_fields.get("error_reason"),
        )

    def normalize_many(
        self, payloads: Iterable[Mapping[str, Any]]
    ) -> Tuple[Payment, ...]:
        """Normalize a sequence of raw payment payloads."""
        if not isinstance(payloads, Iterable):
            raise DomainValidationError("payloads must be an iterable of mappings")
        return tuple(self.normalize(p) for p in payloads)

    # -------------------------------------------------------------------------
    # Internal Field Extractors & Parsers
    # -------------------------------------------------------------------------

    def _extract_id(self, payload: Mapping[str, Any]) -> str:
        raw_id = (
            payload.get("id")
            or payload.get("payment_id")
            or payload.get("transaction_id")
            or payload.get("pay_id")
        )
        if raw_id is None:
            raise DomainValidationError("Payment payload is missing required id field")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise DomainValidationError(
                f"Payment.id must be a non-empty string, got {raw_id!r}"
            )
        return raw_id.strip()

    def _extract_currency(self, payload: Mapping[str, Any]) -> Currency:
        raw_curr = payload.get("currency", "INR")
        if isinstance(raw_curr, Currency):
            return raw_curr
        if not isinstance(raw_curr, str) or not raw_curr.strip():
            raise DomainValidationError(f"Invalid currency field: {raw_curr!r}")
        try:
            return Currency(raw_curr.strip().upper())
        except ValueError:
            raise DomainValidationError(f"Unsupported currency: {raw_curr!r}")

    def _extract_amount(
        self, payload: Mapping[str, Any], currency: Currency = Currency.INR
    ) -> Money:
        raw_amount = payload.get("amount")
        if raw_amount is None:
            raw_amount = (
                payload.get("amount_paise")
                or payload.get("amount_minor")
                or payload.get("amount_minor_units")
                or payload.get("amount_in_paise")
            )

        # Check explicit major-unit fields
        if raw_amount is None and "amount_rupees" in payload:
            raw_rupees = payload["amount_rupees"]
            if isinstance(raw_rupees, bool) or isinstance(raw_rupees, float):
                raise MoneyPrecisionError("amount_rupees does not accept float; use str or Decimal")
            return Money.from_rupees(raw_rupees, currency=currency)

        if raw_amount is None:
            raise DomainValidationError("Payment payload is missing required amount field")

        if isinstance(raw_amount, Money):
            if raw_amount.currency != currency:
                raise DomainValidationError(
                    f"Payment amount currency mismatch: {raw_amount.currency} != {currency}"
                )
            return raw_amount

        if isinstance(raw_amount, bool):
            raise MoneyPrecisionError("Payment amount must not be a boolean")

        if isinstance(raw_amount, float):
            raise MoneyPrecisionError(
                "Payment amount must not be a float. Pass integer minor units (paise) "
                "or decimal string via amount_rupees."
            )

        if isinstance(raw_amount, int):
            return Money(raw_amount, currency=currency)

        if isinstance(raw_amount, str):
            clean_str = raw_amount.strip()
            if not clean_str:
                raise DomainValidationError("Payment amount string cannot be empty")
            if "." in clean_str:
                return Money.from_rupees(clean_str, currency=currency)
            try:
                return Money(int(clean_str), currency=currency)
            except ValueError:
                raise DomainValidationError(f"Invalid integer amount string: {clean_str!r}")

        if isinstance(raw_amount, Decimal):
            return Money.from_rupees(raw_amount, currency=currency)

        raise DomainValidationError(f"Unsupported amount type: {type(raw_amount).__name__}")

    def _extract_status(self, payload: Mapping[str, Any]) -> PaymentStatus:
        raw_status = (
            payload.get("status")
            or payload.get("payment_status")
            or payload.get("state")
        )
        if raw_status is None:
            raise DomainValidationError("Payment payload is missing required status field")

        if isinstance(raw_status, PaymentStatus):
            return raw_status

        if not isinstance(raw_status, str) or not raw_status.strip():
            raise DomainValidationError(f"Invalid payment status: {raw_status!r}")

        key = raw_status.strip().lower()
        if key in STATUS_MAPPING:
            return STATUS_MAPPING[key]

        raise DomainValidationError(f"Unrecognised payment status: {raw_status!r}")

    def _extract_method(self, payload: Mapping[str, Any]) -> PaymentMethod:
        raw_method = (
            payload.get("method")
            or payload.get("payment_method")
            or payload.get("instrument")
            or payload.get("mode")
        )
        if raw_method is None:
            raise DomainValidationError("Payment payload is missing required method field")

        if isinstance(raw_method, PaymentMethod):
            return raw_method

        if not isinstance(raw_method, str) or not raw_method.strip():
            raise DomainValidationError(f"Invalid payment method: {raw_method!r}")

        key = raw_method.strip().lower()
        if key in METHOD_MAPPING:
            return METHOD_MAPPING[key]

        # For unknown strings, map to UNKNOWN enum so it remains observable without breaking
        return PaymentMethod.UNKNOWN

    def _extract_timestamp(self, payload: Mapping[str, Any]) -> datetime:
        raw_time = (
            payload.get("created_at")
            or payload.get("timestamp")
            or payload.get("event_time")
            or payload.get("created_at_unix")
            or payload.get("time")
        )
        if raw_time is None:
            raise DomainValidationError("Payment payload is missing required timestamp field")

        if isinstance(raw_time, datetime):
            return require_utc(raw_time, "Payment.created_at")

        if isinstance(raw_time, bool):
            raise DomainValidationError("Timestamp must not be a boolean")

        if isinstance(raw_time, int):
            # If timestamp is in milliseconds (e.g. > 100 billion, after year 1973 ms)
            if raw_time > 100_000_000_000:
                return from_unix_seconds(raw_time // 1000)
            return from_unix_seconds(raw_time)

        if isinstance(raw_time, str):
            clean_str = raw_time.strip()
            if not clean_str:
                raise DomainValidationError("Timestamp string cannot be empty")

            # Try numeric string (unix seconds/ms)
            if clean_str.isdigit():
                val = int(clean_str)
                if val > 100_000_000_000:
                    return from_unix_seconds(val // 1000)
                return from_unix_seconds(val)

            # Try ISO-8601 parsing
            try:
                # Handle standard UTC Z format
                if clean_str.endswith("Z"):
                    clean_str = clean_str[:-1] + "+00:00"
                dt = datetime.fromisoformat(clean_str)
                return require_utc(dt, "Payment.created_at")
            except ValueError as exc:
                raise DomainValidationError(
                    f"Unparseable ISO-8601 timestamp string {raw_time!r}: {str(exc)}"
                ) from exc

        raise DomainValidationError(f"Unsupported timestamp type: {type(raw_time).__name__}")

    def _extract_order_id(self, payload: Mapping[str, Any]) -> Optional[str]:
        raw_order = payload.get("order_id") or payload.get("orderId") or payload.get("order")
        if raw_order is None:
            return None
        if isinstance(raw_order, str):
            clean = raw_order.strip()
            return clean if clean else None
        return str(raw_order)

    def _extract_error_fields(
        self, payload: Mapping[str, Any], status: PaymentStatus
    ) -> Mapping[str, Optional[str]]:
        # Invariant: Non-failed payments must not carry error details
        if status is not PaymentStatus.FAILED:
            return {
                "error_code": None,
                "error_description": None,
                "error_source": None,
                "error_step": None,
                "error_reason": None,
            }

        # Check top-level or nested error object
        nested_error = payload.get("error") if isinstance(payload.get("error"), Mapping) else {}

        def _get_str(key: str, fallback_key: Optional[str] = None) -> Optional[str]:
            val = payload.get(key)
            if val is None and fallback_key:
                val = payload.get(fallback_key)
            if val is None and nested_error:
                val = nested_error.get(key)
                if val is None and fallback_key:
                    val = nested_error.get(fallback_key)
            if val is not None and isinstance(val, str) and val.strip():
                return val.strip()
            return None

        return {
            "error_code": _get_str("error_code", "code"),
            "error_description": _get_str("error_description", "description"),
            "error_source": _get_str("error_source", "source"),
            "error_step": _get_str("error_step", "step"),
            "error_reason": _get_str("error_reason", "reason"),
        }
