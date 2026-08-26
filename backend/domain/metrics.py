"""Financial measurement contracts.

Every value here is produced by ``backend/financial/`` and carries its own
inputs, so any number can be independently recomputed from an audit record
(ARCHITECTURE.md 16).

Two conventions the whole system depends on:

* **Undefined is not zero** (ADR-004). A rate over an empty population is
  ``None``. A ratio over a zero denominator is ``None``. Callers must handle
  "we don't know".
* **Facts and estimates are different types.** ``revenue_at_risk`` is derived
  from observed data. ``RecoverableRevenue`` rests on an assumption, so it
  cannot be constructed without one.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Optional, Tuple

from .enums import BaselineMethod, Dimension, SourceConfidence
from .errors import DomainValidationError, MoneyPrecisionError
from .money import Money
from .window import TimeWindow, require_utc

# Working precision for every rate computation. Verified sufficient for the
# identity success_rate + failure_rate == 1 to hold exactly (see tests).
RATE_PRECISION = 28

# Display precision only. Never used for comparison or arithmetic.
RATE_DISPLAY_DP = 4

# Smallest expected cell count at which the two-proportion z-test's normal
# approximation is conventionally considered sound. The textbook figure; some
# authors demand 10. Detection may require more, never less
# (see SignificanceResult.min_expected_count).
MIN_EXPECTED_COUNT_FOR_NORMAL_APPROXIMATION = 5.0

ZERO = Decimal(0)
ONE = Decimal(1)


def _require_count(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an int, got {value!r}")
    if value < 0:
        raise DomainValidationError(f"{field_name} must be non-negative, got {value}")
    return value


def _require_decimal(value: Decimal, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyPrecisionError(f"{field_name} must be a Decimal, not a float")
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, Decimal):
        raise DomainValidationError(
            f"{field_name} must be a Decimal, got {type(value).__name__}"
        )
    if not value.is_finite():
        raise DomainValidationError(f"{field_name} must be finite, got {value}")
    return value


@dataclass(frozen=True)
class Rate:
    """A ratio that remembers where it came from.

    Carrying the numerator and denominator rather than just the quotient means
    a rate is auditable and re-derivable, and that "3 of 4" is distinguishable
    from "750 of 1000" — a distinction that decides whether a deviation is
    signal or noise.

    Construct via :meth:`of`, which returns ``None`` for an empty population
    instead of a misleading zero.
    """

    numerator: int
    denominator: int

    def __post_init__(self) -> None:
        _require_count(self.numerator, "Rate.numerator")
        _require_count(self.denominator, "Rate.denominator")
        if self.denominator == 0:
            raise DomainValidationError(
                "Rate with a zero denominator is undefined; use Rate.of() which returns None"
            )
        if self.numerator > self.denominator:
            raise DomainValidationError(
                f"Rate numerator {self.numerator} exceeds denominator {self.denominator}"
            )

    @classmethod
    def of(cls, numerator: int, denominator: int) -> Optional["Rate"]:
        """Build a rate, or ``None`` when the population is empty (ADR-004)."""
        _require_count(numerator, "numerator")
        _require_count(denominator, "denominator")
        if denominator == 0:
            return None
        return cls(numerator, denominator)

    @property
    def value(self) -> Decimal:
        """Exact quotient in ``[0, 1]``, at working precision."""
        with localcontext() as ctx:
            ctx.prec = RATE_PRECISION
            return Decimal(self.numerator) / Decimal(self.denominator)

    def as_percent(self, decimal_places: int = RATE_DISPLAY_DP) -> Decimal:
        """Percentage, quantized for display only."""
        exponent = Decimal(1).scaleb(-decimal_places)
        return (self.value * 100).quantize(exponent, rounding=ROUND_HALF_UP)

    def complement(self) -> "Rate":
        """The rate of the other outcome over the same population."""
        return Rate(self.denominator - self.numerator, self.denominator)

    def __str__(self) -> str:
        return f"{self.as_percent()}% ({self.numerator}/{self.denominator})"


@dataclass(frozen=True)
class TransactionCounts:
    """Counts over a population of payments, split by rate relevance.

    ``decided`` is the denominator for every rate. ``undecided`` (in-flight)
    payments are counted but deliberately excluded — see ARCHITECTURE.md 7.2 for
    why including them masks real incidents.
    """

    succeeded: int
    failed: int
    undecided: int = 0

    def __post_init__(self) -> None:
        _require_count(self.succeeded, "TransactionCounts.succeeded")
        _require_count(self.failed, "TransactionCounts.failed")
        _require_count(self.undecided, "TransactionCounts.undecided")

    @property
    def decided(self) -> int:
        """Payments that reached a terminal outcome. The rate denominator."""
        return self.succeeded + self.failed

    @property
    def total(self) -> int:
        """Every payment observed, including in-flight."""
        return self.decided + self.undecided

    @property
    def has_decided_population(self) -> bool:
        return self.decided > 0

    def __add__(self, other: "TransactionCounts") -> "TransactionCounts":
        if not isinstance(other, TransactionCounts):
            return NotImplemented
        return TransactionCounts(
            succeeded=self.succeeded + other.succeeded,
            failed=self.failed + other.failed,
            undecided=self.undecided + other.undecided,
        )

    def __str__(self) -> str:
        return (
            f"{self.total} total ({self.succeeded} ok, {self.failed} failed, "
            f"{self.undecided} in flight)"
        )


@dataclass(frozen=True)
class WindowCounts:
    """Counts for one time bucket. The unit a baseline is pooled from."""

    window: TimeWindow
    counts: TransactionCounts

    def __post_init__(self) -> None:
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("WindowCounts.window must be a TimeWindow")
        if not isinstance(self.counts, TransactionCounts):
            raise DomainValidationError("WindowCounts.counts must be TransactionCounts")

    @property
    def failure_rate(self) -> Optional[Rate]:
        return Rate.of(self.counts.failed, self.counts.decided)


@dataclass(frozen=True)
class BaselineFailureRate:
    """A baseline, plus enough context to judge whether to trust it.

    ``rate`` is ``None`` when the sample was too small. That is not an error —
    it is the honest answer, and it is what makes ``INSUFFICIENT_DATA`` a
    first-class outcome rather than a silent "0%, all healthy".
    """

    method: BaselineMethod
    rate: Optional[Rate]
    windows_considered: int
    windows_used: int
    decided_sample: int
    min_decided_required: int

    def __post_init__(self) -> None:
        if not isinstance(self.method, BaselineMethod):
            raise DomainValidationError(f"invalid BaselineMethod: {self.method!r}")
        if self.rate is not None and not isinstance(self.rate, Rate):
            raise DomainValidationError("BaselineFailureRate.rate must be a Rate or None")
        _require_count(self.windows_considered, "windows_considered")
        _require_count(self.windows_used, "windows_used")
        _require_count(self.decided_sample, "decided_sample")
        _require_count(self.min_decided_required, "min_decided_required")
        if self.windows_used > self.windows_considered:
            raise DomainValidationError("windows_used cannot exceed windows_considered")

    @property
    def is_sufficient(self) -> bool:
        return self.rate is not None

    @property
    def value(self) -> Optional[Decimal]:
        return self.rate.value if self.rate is not None else None


@dataclass(frozen=True)
class Deviation:
    """How far the current rate sits from baseline.

    Two measures, both meaningful, neither sufficient alone: 0.2pp on a 0.1pp
    baseline is a 3x lift but probably noise, while 5pp on a 2pp baseline is a
    smaller lift and a real incident.
    """

    current: Rate
    baseline: Rate
    absolute_percentage_points: Decimal
    relative_lift: Optional[Decimal]

    def __post_init__(self) -> None:
        if not isinstance(self.current, Rate) or not isinstance(self.baseline, Rate):
            raise DomainValidationError("Deviation.current/baseline must be Rate")
        _require_decimal(self.absolute_percentage_points, "absolute_percentage_points")
        if self.relative_lift is not None:
            _require_decimal(self.relative_lift, "relative_lift")

    @property
    def is_worse_than_baseline(self) -> bool:
        return self.absolute_percentage_points > ZERO


@dataclass(frozen=True)
class SignificanceResult:
    """Whether a difference is distinguishable from sampling noise.

    ``float`` is used here, and only here. A z-score is a statistic, never money
    and never a rate — the one permitted use of float in this codebase
    (PROJECT_RULES 1.6).

    This is a *measurement*, not a detector: it reports how unlikely the
    observed difference is under the null hypothesis and decides nothing
    (ADR-006).

    ``min_expected_count`` is the smallest of the four expected cell counts under
    the pooled proportion, and it is the honest health warning on the p-value.
    The z-test approximates a binomial with a normal, and that approximation
    degrades badly on thin data: 3 failures out of 12 against a 5% baseline comes
    out at p=0.002 — confident, and not trustworthy, because the expected failure
    count in the current sample is 0.63. A caller that reads ``p_value`` without
    reading this number will page a merchant over twelve transactions.

    Reported rather than enforced, deliberately: the value is the measurement,
    and the threshold a caller demands of it is a detection policy (ADR-006).
    ``normal_approximation_valid`` applies the textbook convention for
    convenience; ``detection/`` may require more.
    """

    z_score: float
    p_value: float
    current_decided: int
    baseline_decided: int
    min_expected_count: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.z_score, float) or not isinstance(self.p_value, float):
            raise DomainValidationError("SignificanceResult statistics must be floats")
        if not (0.0 <= self.p_value <= 1.0):
            raise DomainValidationError(f"p_value out of range: {self.p_value}")
        _require_count(self.current_decided, "current_decided")
        _require_count(self.baseline_decided, "baseline_decided")
        if not isinstance(self.min_expected_count, float):
            raise DomainValidationError("min_expected_count must be a float")
        if self.min_expected_count < 0.0:
            raise DomainValidationError(
                f"min_expected_count cannot be negative: {self.min_expected_count}"
            )

    @property
    def normal_approximation_valid(self) -> bool:
        """Whether the p-value rests on a sound approximation.

        ``False`` means the arithmetic is right and the conclusion is not
        supportable — treat it as insufficient data, not as evidence.
        """
        return self.min_expected_count >= MIN_EXPECTED_COUNT_FOR_NORMAL_APPROXIMATION


@dataclass(frozen=True)
class DimensionSlice:
    """One value of one dimension, with its own counts and exposure."""

    dimension: Dimension
    value: str
    counts: TransactionCounts
    failed_gmv: Money
    source_confidence: SourceConfidence = SourceConfidence.OBSERVED

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, Dimension):
            raise DomainValidationError(f"invalid Dimension: {self.dimension!r}")
        if not isinstance(self.value, str) or not self.value:
            raise DomainValidationError("DimensionSlice.value must be a non-empty string")
        if not isinstance(self.counts, TransactionCounts):
            raise DomainValidationError("DimensionSlice.counts must be TransactionCounts")
        if not isinstance(self.failed_gmv, Money):
            raise DomainValidationError("DimensionSlice.failed_gmv must be Money")
        if self.failed_gmv.is_negative:
            raise DomainValidationError("failed_gmv cannot be negative")
        if not isinstance(self.source_confidence, SourceConfidence):
            raise DomainValidationError("invalid source_confidence")

    @property
    def failure_rate(self) -> Optional[Rate]:
        return Rate.of(self.counts.failed, self.counts.decided)


@dataclass(frozen=True)
class RecoveryAssumption:
    """An explicitly-owned assumption about how much revenue is recoverable.

    There is no default (ADR-007). A recovery rate is not observable from
    payment data, and a defaulted assumption silently becomes a fact — so the
    caller must state the rate, where it came from, and why.
    """

    rate: Decimal
    source: str
    rationale: str

    def __post_init__(self) -> None:
        rate = _require_decimal(self.rate, "RecoveryAssumption.rate")
        if not (ZERO <= rate <= ONE):
            raise DomainValidationError(f"recovery rate must be in [0, 1], got {rate}")
        object.__setattr__(self, "rate", rate)
        if not isinstance(self.source, str) or not self.source.strip():
            raise DomainValidationError("RecoveryAssumption.source is required")
        if not isinstance(self.rationale, str) or len(self.rationale.strip()) < 10:
            raise DomainValidationError(
                "RecoveryAssumption.rationale must explain the assumption (>= 10 chars)"
            )


@dataclass(frozen=True)
class RecoverableRevenue:
    """An estimate. Always flagged as one, everywhere it is rendered."""

    amount: Money
    assumption: RecoveryAssumption
    is_estimate: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.amount, Money):
            raise DomainValidationError("RecoverableRevenue.amount must be Money")
        if self.amount.is_negative:
            raise DomainValidationError("RecoverableRevenue.amount cannot be negative")
        if not isinstance(self.assumption, RecoveryAssumption):
            raise DomainValidationError("RecoverableRevenue requires a RecoveryAssumption")
        if self.is_estimate is not True:
            # Not configurable: this value can never be presented as a fact.
            raise DomainValidationError("RecoverableRevenue.is_estimate must be True")


@dataclass(frozen=True)
class RevenueRisk:
    """Money exposed by a degradation. Facts and estimates kept separate.

    Attributes:
        failed_gmv: Observed. Total amount of failed payments in the window.
        excess_failed_transactions: Derived. Failures above what the baseline
            predicts, clamped at zero — negative excess is meaningless.
        mean_failed_ticket: Derived. Average value of a failed payment.
        revenue_at_risk: Derived. ``excess x mean_failed_ticket``.
        recoverable: Estimate, present only when an assumption was supplied.
    """

    failed_gmv: Money
    excess_failed_transactions: int
    mean_failed_ticket: Money
    revenue_at_risk: Money
    recoverable: Optional[RecoverableRevenue] = None

    def __post_init__(self) -> None:
        for name in ("failed_gmv", "mean_failed_ticket", "revenue_at_risk"):
            amount = getattr(self, name)
            if not isinstance(amount, Money):
                raise DomainValidationError(f"RevenueRisk.{name} must be Money")
            if amount.is_negative:
                raise DomainValidationError(f"RevenueRisk.{name} cannot be negative")
        _require_count(self.excess_failed_transactions, "excess_failed_transactions")
        if self.recoverable is not None:
            if not isinstance(self.recoverable, RecoverableRevenue):
                raise DomainValidationError("RevenueRisk.recoverable must be RecoverableRevenue")
            if self.recoverable.amount > self.revenue_at_risk:
                raise DomainValidationError(
                    "recoverable revenue cannot exceed revenue at risk"
                )
        if self.failed_gmv.currency is not self.revenue_at_risk.currency:
            raise DomainValidationError("RevenueRisk currencies must match")


@dataclass(frozen=True)
class FinancialMetrics:
    """The complete deterministic measurement of one window.

    The single output of the financial engine and the only numeric input the
    agent ever reads. ``computation_version`` is recorded so a stored metric
    stays explicable after the engine changes.
    """

    window: TimeWindow
    counts: TransactionCounts
    failure_rate: Optional[Rate]
    success_rate: Optional[Rate]
    baseline: Optional[BaselineFailureRate]
    deviation: Optional[Deviation]
    significance: Optional[SignificanceResult]
    revenue_risk: Optional[RevenueRisk]
    computed_at: datetime
    computation_version: str

    def __post_init__(self) -> None:
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("FinancialMetrics.window must be a TimeWindow")
        if not isinstance(self.counts, TransactionCounts):
            raise DomainValidationError("FinancialMetrics.counts must be TransactionCounts")
        object.__setattr__(
            self, "computed_at", require_utc(self.computed_at, "FinancialMetrics.computed_at")
        )
        if not isinstance(self.computation_version, str) or not self.computation_version:
            raise DomainValidationError("computation_version is required")

        # An empty decided population means no rate exists. Anything else would
        # be an engine bug, so assert the invariant rather than tolerate it.
        if self.counts.has_decided_population:
            if self.failure_rate is None or self.success_rate is None:
                raise DomainValidationError(
                    "rates must be present when a decided population exists"
                )
        else:
            if self.failure_rate is not None or self.success_rate is not None:
                raise DomainValidationError(
                    "rates must be None when no decided population exists (ADR-004)"
                )

        if self.deviation is not None and self.failure_rate is None:
            raise DomainValidationError("deviation requires a current failure rate")

    @property
    def has_sufficient_data(self) -> bool:
        return self.counts.has_decided_population

    @property
    def is_comparable_to_baseline(self) -> bool:
        return self.deviation is not None


@dataclass(frozen=True)
class DimensionBreakdown:
    """All slices of one dimension over one window, plus the window total.

    Slices are ordered deterministically by the producing function so evidence
    is byte-stable across runs.
    """

    dimension: Dimension
    window: TimeWindow
    slices: Tuple[DimensionSlice, ...]
    total_counts: TransactionCounts

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, Dimension):
            raise DomainValidationError(f"invalid Dimension: {self.dimension!r}")
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("DimensionBreakdown.window must be a TimeWindow")
        if not isinstance(self.slices, tuple):
            raise DomainValidationError("DimensionBreakdown.slices must be a tuple")
        for item in self.slices:
            if not isinstance(item, DimensionSlice):
                raise DomainValidationError("slices must contain DimensionSlice")
            if item.dimension is not self.dimension:
                raise DomainValidationError("slice dimension does not match breakdown")
        values = [item.value for item in self.slices]
        if len(values) != len(set(values)):
            raise DomainValidationError("duplicate dimension values in breakdown")
