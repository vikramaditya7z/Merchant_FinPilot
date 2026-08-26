"""The deterministic financial engine façade.

One call, one complete, auditable measurement of one window. This is the only
numeric input the agent ever reads (ARCHITECTURE.md 8).

The engine is a pure function of its arguments. In particular ``now`` is
**injected, not read from the clock** (PROJECT_RULES 4.1) — a calculation that
reads the clock cannot be replayed from an audit record, which would make every
stored metric unverifiable.
"""

from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from ..domain.enums import BaselineMethod, ComparableWindowMode, Currency
from ..domain.errors import DomainValidationError
from ..domain.metrics import (
    BaselineFailureRate,
    FinancialMetrics,
    RecoveryAssumption,
    WindowCounts,
)
from ..domain.window import TimeWindow, require_utc
from .baseline import (
    DEFAULT_MIN_BASELINE_DECIDED,
    DEFAULT_MIN_WINDOW_DECIDED,
    baseline_failure_rate,
    select_comparable_windows,
)
from .counts import count_transactions
from .deviation import compute_deviation
from .exposure import compute_revenue_risk
from .population import PaymentLike, assert_single_currency, in_window
from .rates import failure_rate, success_rate
from .significance import two_proportion_significance
from .windows import bucket_counts, hourly_buckets, preceding_windows

# Bumped whenever a calculation changes. Stored on every FinancialMetrics so a
# historical number stays explicable after the engine evolves
# (ARCHITECTURE.md 16).
COMPUTATION_VERSION = "financial-engine-1"


def compute_metrics(
    items: Iterable[PaymentLike],
    window: TimeWindow,
    now: datetime,
    baseline_windows: Optional[Sequence[WindowCounts]] = None,
    baseline_method: BaselineMethod = BaselineMethod.POOLED,
    comparable_mode: ComparableWindowMode = ComparableWindowMode.ALL,
    min_baseline_decided: int = DEFAULT_MIN_BASELINE_DECIDED,
    min_window_decided: int = DEFAULT_MIN_WINDOW_DECIDED,
    currency: Currency = Currency.INR,
    recovery_assumption: Optional[RecoveryAssumption] = None,
) -> FinancialMetrics:
    """Measure one window, with an optional baseline comparison.

    Args:
        items: The payment population. Filtered to ``window`` internally, so the
            caller may pass a superset.
        window: The window under measurement.
        now: Injected computation time.
        baseline_windows: Historical per-window counts. Omit for a bare
            measurement with no deviation claim.
        baseline_method: Pooled (default) or median-of-windows.
        comparable_mode: Whether to restrict the baseline to the same hour of day.
        recovery_assumption: Supply only if a recoverable-revenue estimate is
            wanted, and only with a stated source (ADR-007).

    Returns:
        A ``FinancialMetrics`` in which every optional field is ``None`` exactly
        when the underlying quantity is genuinely undefined — no zero-filling
        (ADR-004). ``baseline``/``deviation``/``significance``/``revenue_risk``
        are absent when there is not enough data to support them, which is the
        ``INSUFFICIENT_DATA`` path rather than an error.
    """
    if not isinstance(window, TimeWindow):
        raise DomainValidationError("compute_metrics() requires a TimeWindow")
    now = require_utc(now, "now")

    population = in_window(items, window)
    assert_single_currency(population)

    counts = count_transactions(population)
    current_failure_rate = failure_rate(counts)
    current_success_rate = success_rate(counts)

    baseline: Optional[BaselineFailureRate] = None
    deviation = None
    significance = None
    revenue_risk = None

    if baseline_windows is not None:
        comparable = select_comparable_windows(baseline_windows, window, comparable_mode)
        baseline = baseline_failure_rate(
            comparable,
            method=baseline_method,
            min_decided=min_baseline_decided,
            min_window_decided=min_window_decided,
        )

        # Deviation, significance and exposure all require BOTH a current rate
        # and a sufficient baseline. Without both, we make no claim at all
        # rather than an unfounded one.
        if current_failure_rate is not None and baseline.rate is not None:
            deviation = compute_deviation(current_failure_rate, baseline.rate)
            significance = two_proportion_significance(current_failure_rate, baseline.rate)
            revenue_risk = compute_revenue_risk(
                population,
                counts,
                baseline.rate,
                currency=currency,
                assumption=recovery_assumption,
            )

    return FinancialMetrics(
        window=window,
        counts=counts,
        failure_rate=current_failure_rate,
        success_rate=current_success_rate,
        baseline=baseline,
        deviation=deviation,
        significance=significance,
        revenue_risk=revenue_risk,
        computed_at=now,
        computation_version=COMPUTATION_VERSION,
    )


def build_hourly_baseline(
    items: Iterable[PaymentLike], window: TimeWindow, lookback_windows: int
) -> tuple:
    """Bucket the ``lookback_windows`` periods before ``window`` into counts.

    A convenience for the common case where the incident window is one hour and
    the baseline is the preceding N hours. The incident window is excluded by
    construction — ``preceding_windows`` only looks backwards
    (ARCHITECTURE.md 7.3).
    """
    population = tuple(items)
    history = preceding_windows(window, lookback_windows)
    return bucket_counts(population, history)


def build_daily_hourly_baseline(
    items: Iterable[PaymentLike], window: TimeWindow, lookback_days: int
) -> tuple:
    """Bucket the ``lookback_days`` before ``window`` into hourly counts.

    Produces the candidate set for ``ComparableWindowMode.SAME_HOUR_OF_DAY``:
    pass the result to ``compute_metrics`` and it will keep only the buckets
    matching the incident window's hour.

    ``window`` must start on an hour boundary. Otherwise the hourly buckets would
    not line up with it, and "the same hour of day" would silently compare
    partially-offset periods.
    """
    if isinstance(lookback_days, bool) or not isinstance(lookback_days, int):
        raise DomainValidationError("lookback_days must be an int")
    if lookback_days < 1:
        raise DomainValidationError("lookback_days must be at least 1")
    if (window.start.minute, window.start.second, window.start.microsecond) != (0, 0, 0):
        raise DomainValidationError(
            "build_daily_hourly_baseline() requires an hour-aligned window start"
        )

    population = tuple(items)
    span = TimeWindow(window.start - timedelta(days=lookback_days), window.start)
    return bucket_counts(population, hourly_buckets(span))
