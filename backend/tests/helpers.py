"""Shared helpers for the test suite.

Constructing a payment takes several required fields, and a test that spells all
of them out on every line hides the one field it actually cares about. These
builders default everything and let a test override only what matters.

Timestamps are fixed constants, never ``utcnow()``: a suite whose fixtures move
with the clock passes or fails for reasons unrelated to the code.
"""

from datetime import datetime, timedelta
from typing import Iterable, List, Optional

from ..domain.enums import Currency, FailureCategory, PaymentMethod, PaymentStatus
from ..domain.money import Money
from ..domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ..domain.window import UTC, TimeWindow

# Fixed reference instants. T0 is an hour boundary so hourly bucketing lines up.
T0 = datetime(2026, 8, 20, 10, 0, 0, tzinfo=UTC)
HOUR = TimeWindow(T0, T0 + timedelta(hours=1))
NOW = T0 + timedelta(hours=2)

DEFAULT_AMOUNT_PAISE = 100_00  # 100.00


def payment(
    id: str = "pay_test",
    status: PaymentStatus = PaymentStatus.CAPTURED,
    amount_paise: int = DEFAULT_AMOUNT_PAISE,
    method: PaymentMethod = PaymentMethod.UPI,
    created_at: Optional[datetime] = None,
    minutes: int = 0,
    error_code: Optional[str] = None,
    currency: Currency = Currency.INR,
) -> Payment:
    """A payment with sensible defaults.

    ``minutes`` offsets from ``T0``, which is usually clearer in a test than
    building a datetime. Error details are attached only to failures, matching
    the ``Payment`` invariant.
    """
    when = created_at if created_at is not None else T0 + timedelta(minutes=minutes)
    is_failure = status is PaymentStatus.FAILED
    return Payment(
        id=id,
        created_at=when,
        amount=Money(amount_paise, currency),
        status=status,
        method=method,
        error_code=error_code if is_failure else None,
    )


def enriched(
    payment_obj: Payment,
    region: Optional[str] = None,
    provider: Optional[str] = None,
    failure_category: Optional[FailureCategory] = None,
) -> EnrichedPayment:
    """Attach derived dimensions to a payment."""
    return EnrichedPayment(
        payment=payment_obj,
        enrichment=PaymentEnrichment(
            payment_id=payment_obj.id,
            region=region,
            provider=provider,
            failure_category=failure_category,
        ),
    )


def population(
    succeeded: int = 0,
    failed: int = 0,
    undecided: int = 0,
    amount_paise: int = DEFAULT_AMOUNT_PAISE,
    method: PaymentMethod = PaymentMethod.UPI,
    window: TimeWindow = HOUR,
    prefix: str = "pay",
    error_code: str = "TEST_ERROR",
) -> List[Payment]:
    """Exactly N succeeded / failed / undecided payments inside ``window``.

    Timestamps are spread evenly so hourly bucketing has something to bucket, and
    ids are unique so duplicate-detection logic is exercised honestly.
    """
    total = succeeded + failed + undecided
    if total == 0:
        return []
    step = window.duration_seconds / (total + 1)
    items: List[Payment] = []
    index = 0
    for status, count in (
        (PaymentStatus.CAPTURED, succeeded),
        (PaymentStatus.FAILED, failed),
        (PaymentStatus.CREATED, undecided),
    ):
        for _ in range(count):
            index += 1
            items.append(
                Payment(
                    id=f"{prefix}_{index:04d}",
                    created_at=window.start + timedelta(seconds=step * index),
                    amount=Money(amount_paise, Currency.INR),
                    status=status,
                    method=method,
                    error_code=error_code if status is PaymentStatus.FAILED else None,
                )
            )
    return items


def paise(items: Iterable[Payment]) -> int:
    """Total minor units across payments. For asserting on sums."""
    return sum(item.amount.minor_units for item in items)
