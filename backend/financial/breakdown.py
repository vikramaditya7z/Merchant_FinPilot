"""Dimensional breakdown — slicing a population to localise a problem.

This is the raw material of investigation: the agent chooses *which* dimension to
slice, and this module computes the slice. The agent never groups or counts
anything itself (PROJECT_RULES 3.5).

One important distinction the code enforces:

* **Population dimensions** (method, region, provider, hour) partition *all*
  payments, so a per-slice failure *rate* is meaningful.
* **Failure-attribute dimensions** (failure code, failure category) exist only on
  failed payments. Every payment in such a slice failed by definition, so its
  "failure rate" would be 100% and tells you nothing. For these, read
  ``share_of_failures`` — the composition of failures — instead.

Conflating the two produces a breakdown that looks authoritative and means
nothing.
"""

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from ..domain.enums import Dimension, SourceConfidence
from ..domain.errors import DomainValidationError
from ..domain.metrics import (
    RATE_PRECISION,
    DimensionBreakdown,
    DimensionSlice,
    TransactionCounts,
)
from ..domain.payment import EnrichedPayment
from ..domain.window import TimeWindow
from ..domain.enums import Currency
from .counts import count_transactions
from .exposure import failed_gmv
from .population import PaymentLike, as_payment

# Dimensions that only exist on a failed payment.
FAILURE_ONLY_DIMENSIONS = frozenset({Dimension.FAILURE_CODE, Dimension.FAILURE_CATEGORY})

# Dimensions sourced from PaymentEnrichment rather than observed Razorpay data.
# Slices along these are tagged ENRICHED so a reader can see they are inferred
# (ARCHITECTURE.md 12.2).
ENRICHED_DIMENSIONS = frozenset({Dimension.REGION, Dimension.PROVIDER})

# Bucket for payments with no value for the dimension. Named rather than dropped:
# an unattributable failure is itself a finding.
UNKNOWN_VALUE = "unknown"


def _key_for(item: PaymentLike, dimension: Dimension) -> Optional[str]:
    """Extract the dimension value for one payment, or ``None`` if inapplicable."""
    payment = as_payment(item)

    if dimension is Dimension.PAYMENT_METHOD:
        return payment.method.value
    if dimension is Dimension.HOUR_OF_DAY:
        return f"{payment.created_at.hour:02d}"
    if dimension is Dimension.FAILURE_CODE:
        if not payment.is_failure:
            return None
        return payment.error_code or UNKNOWN_VALUE
    if dimension is Dimension.FAILURE_CATEGORY:
        if not payment.is_failure:
            return None
        category = item.failure_category if isinstance(item, EnrichedPayment) else None
        return category.value if category is not None else UNKNOWN_VALUE
    if dimension is Dimension.REGION:
        region = item.region if isinstance(item, EnrichedPayment) else None
        return region or UNKNOWN_VALUE
    if dimension is Dimension.PROVIDER:
        provider = item.provider if isinstance(item, EnrichedPayment) else None
        return provider or UNKNOWN_VALUE

    raise DomainValidationError(f"unsupported dimension: {dimension!r}")


def breakdown_by(
    items: Iterable[PaymentLike],
    dimension: Dimension,
    window: TimeWindow,
    currency: Currency = Currency.INR,
    key_fn: Optional[Callable[[PaymentLike], Optional[str]]] = None,
) -> DimensionBreakdown:
    """Group a population by one dimension.

    Slices are ordered deterministically — most failures first, then most
    decided, then value alphabetically — so the same data always produces
    byte-identical evidence. ``key_fn`` is an escape hatch for a bespoke grouping;
    it must return ``None`` to exclude a payment.
    """
    if not isinstance(dimension, Dimension):
        raise DomainValidationError(f"invalid Dimension: {dimension!r}")
    if not isinstance(window, TimeWindow):
        raise DomainValidationError("breakdown_by() requires a TimeWindow")

    population = tuple(items)
    extract = key_fn or (lambda item: _key_for(item, dimension))

    grouped: Dict[str, List[PaymentLike]] = {}
    for item in population:
        key = extract(item)
        if key is None:
            continue
        grouped.setdefault(key, []).append(item)

    confidence = (
        SourceConfidence.ENRICHED
        if dimension in ENRICHED_DIMENSIONS
        else SourceConfidence.OBSERVED
    )

    slices = [
        DimensionSlice(
            dimension=dimension,
            value=value,
            counts=count_transactions(members),
            failed_gmv=failed_gmv(members, currency),
            source_confidence=confidence,
        )
        for value, members in grouped.items()
    ]
    slices.sort(key=lambda s: (-s.counts.failed, -s.counts.decided, s.value))

    return DimensionBreakdown(
        dimension=dimension,
        window=window,
        slices=tuple(slices),
        total_counts=count_transactions(population),
    )


def share_of_failures(
    breakdown: DimensionBreakdown, value: str
) -> Optional[Decimal]:
    """What fraction of all failures this slice accounts for.

    ``None`` when there were no failures at all — a share of nothing is undefined,
    not zero (ADR-004).
    """
    if not isinstance(breakdown, DimensionBreakdown):
        raise DomainValidationError("share_of_failures() requires a DimensionBreakdown")
    total_failed = breakdown.total_counts.failed
    if total_failed == 0:
        return None
    for item in breakdown.slices:
        if item.value == value:
            with localcontext() as ctx:
                ctx.prec = RATE_PRECISION
                share = Decimal(item.counts.failed) / Decimal(total_failed)
            return share.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    return Decimal(0)


def top_failure_contributor(breakdown: DimensionBreakdown) -> Optional[DimensionSlice]:
    """The slice with the most failures, or ``None`` if nothing failed.

    Only a *candidate* for the primary dimension. Concentration of failures is
    not causation, and naming a root cause is the agent's job — over verified
    evidence, not over this one number.
    """
    if not isinstance(breakdown, DimensionBreakdown):
        raise DomainValidationError("top_failure_contributor() requires a DimensionBreakdown")
    if not breakdown.slices or breakdown.total_counts.failed == 0:
        return None
    leader = breakdown.slices[0]  # already sorted by failures descending
    return leader if leader.counts.failed > 0 else None


def slice_values(breakdown: DimensionBreakdown) -> Tuple[str, ...]:
    """Dimension values present, in the breakdown's deterministic order."""
    return tuple(item.value for item in breakdown.slices)


def total_counts_across(breakdown: DimensionBreakdown) -> TransactionCounts:
    """Sum of slice counts. Equals the window total for population dimensions."""
    total = TransactionCounts(0, 0, 0)
    for item in breakdown.slices:
        total = total + item.counts
    return total
