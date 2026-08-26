"""Payment and Order contracts.

``Payment`` mirrors the subset of the Razorpay payment entity this system needs.
Field-level shape is to be re-confirmed against official documentation at
integration time (ARCHITECTURE.md 12.1).

Two rules shape this module:

* **Observed vs derived is structural.** ``Payment`` holds only what Razorpay
  told us. Region and provider — which we have *not* verified as available on
  the payment entity — live on ``PaymentEnrichment``. Merging them into
  ``Payment`` would erase the distinction between fact and inference
  (PROJECT_RULES 2.6).
* **No ground-truth labels.** ``Payment`` is the production contract and has no
  scenario or label fields at all, so evaluation labels cannot leak into an
  agent input by accident (ADR-005).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .enums import (
    Currency,
    FailureCategory,
    OrderStatus,
    PaymentMethod,
    PaymentOutcome,
    PaymentStatus,
    SourceConfidence,
    outcome_for_status,
)
from .errors import DomainValidationError
from .money import Money
from .window import from_unix_seconds, require_utc


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{field_name} must be a non-empty string")
    # Identifiers are stored verbatim: no stripping, no case folding
    # (PROJECT_RULES 2.3).
    return value


@dataclass(frozen=True)
class Payment:
    """A payment as reported by Razorpay.

    Attributes:
        id: Razorpay payment id (``pay_...``), stored verbatim.
        order_id: Razorpay order id, when the payment was made against one.
        created_at: Event time from the source of truth, as aware UTC.
            Distinct from ingestion time (PROJECT_RULES 2.8).
        amount: Exact amount as integer minor units.
        status: Razorpay lifecycle status.
        method: Payment instrument.
        error_code, error_description, error_source, error_step, error_reason:
            Razorpay error fields, stored verbatim and only for failed payments.
    """

    id: str
    created_at: datetime
    amount: Money
    status: PaymentStatus
    method: PaymentMethod
    order_id: Optional[str] = None
    error_code: Optional[str] = None
    error_description: Optional[str] = None
    error_source: Optional[str] = None
    error_step: Optional[str] = None
    error_reason: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_identifier(self.id, "Payment.id"))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "Payment.created_at"))

        if self.order_id is not None:
            _require_identifier(self.order_id, "Payment.order_id")
        if not isinstance(self.amount, Money):
            raise DomainValidationError("Payment.amount must be a Money instance")
        if not self.amount.is_positive:
            # A zero or negative payment amount is not a real payment.
            raise DomainValidationError(
                f"Payment.amount must be positive, got {self.amount!r}"
            )
        if not isinstance(self.status, PaymentStatus):
            raise DomainValidationError(f"Payment.status invalid: {self.status!r}")
        if not isinstance(self.method, PaymentMethod):
            raise DomainValidationError(f"Payment.method invalid: {self.method!r}")

        # Error fields belong to failures only. A "captured" payment carrying an
        # error code means the mapping upstream is wrong, and we would rather
        # find out here than have it skew a failure breakdown later.
        has_error_detail = any(
            value is not None
            for value in (
                self.error_code,
                self.error_description,
                self.error_source,
                self.error_step,
                self.error_reason,
            )
        )
        if has_error_detail and self.status is not PaymentStatus.FAILED:
            raise DomainValidationError(
                f"Payment {self.id} has error details but status is {self.status.value}"
            )

    @property
    def currency(self) -> Currency:
        return self.amount.currency

    @property
    def outcome(self) -> PaymentOutcome:
        """Rate-relevant outcome. See ARCHITECTURE.md 7.2."""
        return outcome_for_status(self.status)

    @property
    def is_decided(self) -> bool:
        """Whether this payment belongs in a rate denominator."""
        return self.outcome is not PaymentOutcome.UNDECIDED

    @property
    def is_failure(self) -> bool:
        return self.outcome is PaymentOutcome.FAILED

    @property
    def is_success(self) -> bool:
        return self.outcome is PaymentOutcome.SUCCEEDED

    @classmethod
    def from_unix(
        cls,
        id: str,
        created_at_unix: int,
        amount_minor_units: int,
        status: PaymentStatus,
        method: PaymentMethod,
        currency: Currency = Currency.INR,
        **kwargs,
    ) -> "Payment":
        """Convenience constructor matching Razorpay's unix-timestamp shape."""
        return cls(
            id=id,
            created_at=from_unix_seconds(created_at_unix),
            amount=Money(amount_minor_units, currency),
            status=status,
            method=method,
            **kwargs,
        )


@dataclass(frozen=True)
class PaymentEnrichment:
    """Dimensions we derive ourselves, joined to a payment by id.

    Kept separate from ``Payment`` on purpose. As of Day 2 we have **not**
    verified that Razorpay's payment entity exposes a region/geography field,
    and ``acquirer_data`` contents vary by method, so a stable provider/route
    dimension is REQUIRES OFFICIAL DOC VERIFICATION (ARCHITECTURE.md 12.1).

    Until verified, these values are internally derived. Evidence built on them
    carries ``SourceConfidence.ENRICHED`` so a reader — and the Policy Engine —
    can tell it apart from observed fact.
    """

    payment_id: str
    region: Optional[str] = None
    segment: Optional[str] = None
    provider: Optional[str] = None
    failure_category: Optional[FailureCategory] = None
    source_confidence: SourceConfidence = SourceConfidence.ENRICHED

    def __post_init__(self) -> None:
        _require_identifier(self.payment_id, "PaymentEnrichment.payment_id")
        if self.failure_category is not None and not isinstance(
            self.failure_category, FailureCategory
        ):
            raise DomainValidationError(
                f"invalid failure_category: {self.failure_category!r}"
            )
        if not isinstance(self.source_confidence, SourceConfidence):
            raise DomainValidationError(
                f"invalid source_confidence: {self.source_confidence!r}"
            )
        if self.source_confidence is SourceConfidence.OBSERVED:
            # Enrichment is by definition not directly observed. Claiming
            # otherwise would launder an inference into a fact.
            raise DomainValidationError(
                "PaymentEnrichment cannot claim SourceConfidence.OBSERVED"
            )


@dataclass(frozen=True)
class EnrichedPayment:
    """A payment together with its derived dimensions.

    The read model the financial engine slices along. Composition, not
    inheritance, so ``.payment`` is always recoverable as pure observed fact.
    """

    payment: Payment
    enrichment: Optional[PaymentEnrichment] = None

    def __post_init__(self) -> None:
        if not isinstance(self.payment, Payment):
            raise DomainValidationError("EnrichedPayment.payment must be a Payment")
        if self.enrichment is not None:
            if not isinstance(self.enrichment, PaymentEnrichment):
                raise DomainValidationError(
                    "EnrichedPayment.enrichment must be a PaymentEnrichment"
                )
            if self.enrichment.payment_id != self.payment.id:
                raise DomainValidationError(
                    f"enrichment payment_id {self.enrichment.payment_id!r} does not match "
                    f"payment id {self.payment.id!r}"
                )

    @property
    def region(self) -> Optional[str]:
        return self.enrichment.region if self.enrichment else None

    @property
    def provider(self) -> Optional[str]:
        return self.enrichment.provider if self.enrichment else None

    @property
    def segment(self) -> Optional[str]:
        return self.enrichment.segment if self.enrichment else None

    @property
    def failure_category(self) -> Optional[FailureCategory]:
        return self.enrichment.failure_category if self.enrichment else None


@dataclass(frozen=True)
class Order:
    """A Razorpay order. Minimal: only what incident reasoning needs."""

    id: str
    created_at: datetime
    amount: Money
    status: OrderStatus
    receipt: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _require_identifier(self.id, "Order.id"))
        object.__setattr__(self, "created_at", require_utc(self.created_at, "Order.created_at"))
        if not isinstance(self.amount, Money):
            raise DomainValidationError("Order.amount must be a Money instance")
        if not self.amount.is_positive:
            raise DomainValidationError("Order.amount must be positive")
        if not isinstance(self.status, OrderStatus):
            raise DomainValidationError(f"Order.status invalid: {self.status!r}")

    @property
    def is_paid(self) -> bool:
        return self.status is OrderStatus.PAID
