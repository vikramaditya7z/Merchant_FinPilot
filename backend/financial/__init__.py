"""The deterministic financial engine.

Financial truth for Merchant FinPilot. Pure functions, standard library only, no
I/O, no clock, no randomness, no LLM (ADR-001, PROJECT_RULES 4.1).

Every number that reaches a decision is produced here and can be re-derived from
its inputs. The LLM reads these values; it never computes them
(PROJECT_RULES 1.2).

Start at :func:`engine.compute_metrics` — the single façade over the whole layer.
"""

from .baseline import baseline_failure_rate, select_comparable_windows
from .breakdown import (
    breakdown_by,
    share_of_failures,
    slice_values,
    top_failure_contributor,
    total_counts_across,
)
from .counts import count_transactions
from .deviation import absolute_deviation_pp, compute_deviation, relative_lift
from .engine import (
    COMPUTATION_VERSION,
    build_daily_hourly_baseline,
    build_hourly_baseline,
    compute_metrics,
)
from .exposure import (
    compute_revenue_risk,
    excess_failed_transactions,
    expected_failures,
    failed_gmv,
    mean_failed_ticket,
    recoverable_revenue,
    revenue_at_risk,
)
from .population import (
    as_payment,
    assert_single_currency,
    decided,
    failures,
    in_window,
    normalize,
    successes,
)
from .rates import failure_rate, success_rate
from .significance import two_proportion_significance
from .windows import bucket_counts, hourly_buckets, preceding_windows, split_into_buckets

__all__ = [
    "COMPUTATION_VERSION",
    # population
    "as_payment",
    "normalize",
    "in_window",
    "failures",
    "successes",
    "decided",
    "assert_single_currency",
    # counts & rates
    "count_transactions",
    "failure_rate",
    "success_rate",
    # windows
    "split_into_buckets",
    "hourly_buckets",
    "preceding_windows",
    "bucket_counts",
    # baseline
    "select_comparable_windows",
    "baseline_failure_rate",
    # deviation & significance
    "absolute_deviation_pp",
    "relative_lift",
    "compute_deviation",
    "two_proportion_significance",
    # exposure
    "failed_gmv",
    "mean_failed_ticket",
    "expected_failures",
    "excess_failed_transactions",
    "revenue_at_risk",
    "recoverable_revenue",
    "compute_revenue_risk",
    # breakdown
    "breakdown_by",
    "share_of_failures",
    "top_failure_contributor",
    "slice_values",
    "total_counts_across",
    # façade
    "compute_metrics",
    "build_hourly_baseline",
    "build_daily_hourly_baseline",
]
