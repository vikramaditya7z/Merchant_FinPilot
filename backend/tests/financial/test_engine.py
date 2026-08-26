"""Tests for the engine façade and dimensional breakdown.

The façade is the single numeric surface the agent reads, so these tests care
less about individual arithmetic (covered in test_calculations.py and
test_exposure.py) and more about the properties that make its output safe to hand
to an LLM:

* **Nothing is zero-filled.** Every optional field is absent exactly when the
  quantity is undefined, so "no baseline" cannot be misread as "no deviation".
* **The baseline never contains the incident.** A window included in its own
  baseline dilutes the very spike it is meant to reveal.
* **Determinism.** Same input, same output, byte for byte, including slice order —
  otherwise a stored metric cannot be re-derived from an audit record.
* **Population dimensions and failure-attribute dimensions are different things.**
  A per-slice failure rate is meaningful for the first and vacuous for the second.
"""

import unittest
from datetime import timedelta
from decimal import Decimal

from ...domain.enums import (
    BaselineMethod,
    ComparableWindowMode,
    Dimension,
    FailureCategory,
    PaymentMethod,
    PaymentStatus,
    SourceConfidence,
)
from ...domain.errors import DomainValidationError
from ...domain.metrics import Rate, RecoveryAssumption
from ...domain.money import Money
from ...domain.window import TimeWindow
from ...financial.breakdown import (
    breakdown_by,
    share_of_failures,
    slice_values,
    top_failure_contributor,
    total_counts_across,
)
from ...financial.engine import (
    COMPUTATION_VERSION,
    build_daily_hourly_baseline,
    build_hourly_baseline,
    compute_metrics,
)
from ..helpers import HOUR, NOW, T0, enriched, payment, population


class ComputeMetricsTests(unittest.TestCase):
    def test_bare_measurement_makes_no_comparative_claims(self):
        """No baseline supplied, so no deviation, significance or exposure.

        The alternative — a deviation against an implied zero baseline — would
        report every window as a total anomaly.
        """
        metrics = compute_metrics(population(succeeded=95, failed=5), HOUR, NOW)
        self.assertEqual(metrics.failure_rate, Rate(5, 100))
        self.assertIsNone(metrics.baseline)
        self.assertIsNone(metrics.deviation)
        self.assertIsNone(metrics.significance)
        self.assertIsNone(metrics.revenue_risk)
        self.assertTrue(metrics.has_sufficient_data)
        self.assertFalse(metrics.is_comparable_to_baseline)

    def test_filters_to_the_window_so_a_superset_is_safe(self):
        """Callers may pass a whole day; only the window is measured.

        If this leaked, a baseline built from the same superset would be measured
        twice and every rate would be wrong in a way no test of the arithmetic
        would catch.
        """
        inside = population(succeeded=95, failed=5, window=HOUR, prefix="in")
        later = TimeWindow(HOUR.end, HOUR.end + timedelta(hours=1))
        outside = population(succeeded=0, failed=50, window=later, prefix="out")

        metrics = compute_metrics(inside + outside, HOUR, NOW)
        self.assertEqual(metrics.counts.decided, 100)
        self.assertEqual(metrics.failure_rate, Rate(5, 100))

    def test_empty_window_reports_undefined_not_healthy(self):
        # Zero traffic is the signature of a total outage upstream. Rates must be
        # None, and emphatically not 0% failure / 100% success (ADR-004).
        metrics = compute_metrics([], HOUR, NOW)
        self.assertEqual(metrics.counts.total, 0)
        self.assertIsNone(metrics.failure_rate)
        self.assertIsNone(metrics.success_rate)
        self.assertFalse(metrics.has_sufficient_data)

    def test_full_comparison_when_a_sufficient_baseline_exists(self):
        history = self._flat_history(hours=6, succeeded=95, failed=5)
        current = population(succeeded=75, failed=25)

        metrics = compute_metrics(current, HOUR, NOW, baseline_windows=history)

        self.assertEqual(metrics.failure_rate, Rate(25, 100))
        self.assertEqual(metrics.baseline.rate, Rate(30, 600))
        self.assertTrue(metrics.baseline.is_sufficient)
        self.assertEqual(metrics.deviation.absolute_percentage_points, Decimal("20"))
        self.assertEqual(metrics.deviation.relative_lift, Decimal("5"))
        self.assertIsNotNone(metrics.significance)
        self.assertIsNotNone(metrics.revenue_risk)
        self.assertTrue(metrics.is_comparable_to_baseline)

    def test_insufficient_baseline_suppresses_every_derived_number(self):
        """The INSUFFICIENT_DATA path, and it is a normal outcome, not an error.

        Two thin history windows cannot support a deviation claim. The baseline
        object still appears — a reader needs to see *why* nothing was concluded —
        but its rate is None and nothing downstream is computed.
        """
        history = self._flat_history(hours=2, succeeded=9, failed=1)
        metrics = compute_metrics(
            population(succeeded=75, failed=25), HOUR, NOW, baseline_windows=history
        )

        self.assertIsNotNone(metrics.baseline)
        self.assertIsNone(metrics.baseline.rate)
        self.assertFalse(metrics.baseline.is_sufficient)
        self.assertEqual(metrics.baseline.decided_sample, 20)
        self.assertIsNone(metrics.deviation)
        self.assertIsNone(metrics.significance)
        self.assertIsNone(metrics.revenue_risk)
        # And the current rate is still reported: we know what happened, we just
        # cannot say whether it is abnormal.
        self.assertEqual(metrics.failure_rate, Rate(25, 100))

    def test_empty_current_window_with_a_good_baseline_still_claims_nothing(self):
        # A current rate is required for a deviation. Without it there is no
        # subtraction to perform, however good the baseline is.
        history = self._flat_history(hours=6, succeeded=95, failed=5)
        metrics = compute_metrics([], HOUR, NOW, baseline_windows=history)
        self.assertTrue(metrics.baseline.is_sufficient)
        self.assertIsNone(metrics.deviation)
        self.assertIsNone(metrics.revenue_risk)

    def test_recovery_estimate_only_appears_when_asked_for(self):
        history = self._flat_history(hours=6, succeeded=95, failed=5)
        current = population(succeeded=75, failed=25)

        without = compute_metrics(current, HOUR, NOW, baseline_windows=history)
        self.assertIsNone(without.revenue_risk.recoverable)

        with_estimate = compute_metrics(
            current,
            HOUR,
            NOW,
            baseline_windows=history,
            recovery_assumption=RecoveryAssumption(
                Decimal("0.40"), "operator input", "assumed for this test only"
            ),
        )
        self.assertIsNotNone(with_estimate.revenue_risk.recoverable)
        self.assertTrue(with_estimate.revenue_risk.recoverable.is_estimate)

    def test_median_baseline_survives_one_pathological_history_window(self):
        """Estimator choice changes the answer, so it is recorded on the result.

        Five calm hours at 5% and one total outage. Pooled is dragged upward by
        the outage; the median is not. Both are defensible; silently picking one
        without recording it is not.
        """
        history = self._flat_history(hours=5, succeeded=95, failed=5) + self._flat_history(
            hours=1, succeeded=0, failed=100, offset=5
        )
        current = population(succeeded=75, failed=25)

        pooled = compute_metrics(
            current, HOUR, NOW, baseline_windows=history, baseline_method=BaselineMethod.POOLED
        )
        median = compute_metrics(
            current,
            HOUR,
            NOW,
            baseline_windows=history,
            baseline_method=BaselineMethod.MEDIAN_OF_WINDOWS,
        )

        self.assertGreater(pooled.baseline.rate.value, median.baseline.rate.value)
        self.assertEqual(median.baseline.rate, Rate(5, 100))
        self.assertIs(pooled.baseline.method, BaselineMethod.POOLED)
        self.assertIs(median.baseline.method, BaselineMethod.MEDIAN_OF_WINDOWS)

    def test_now_is_injected_not_read_from_the_clock(self):
        # A calculation that reads the clock cannot be replayed from an audit
        # record (PROJECT_RULES 4.1).
        metrics = compute_metrics(population(succeeded=95, failed=5), HOUR, NOW)
        self.assertEqual(metrics.computed_at, NOW)

    def test_naive_now_is_rejected(self):
        from datetime import datetime

        with self.assertRaises(DomainValidationError):
            compute_metrics([], HOUR, datetime(2026, 8, 20, 12, 0, 0))

    def test_requires_a_time_window(self):
        with self.assertRaises(DomainValidationError):
            compute_metrics([], (T0, T0 + timedelta(hours=1)), NOW)  # type: ignore[arg-type]

    def test_computation_version_is_stamped_for_later_explicability(self):
        metrics = compute_metrics(population(succeeded=95, failed=5), HOUR, NOW)
        self.assertEqual(metrics.computation_version, COMPUTATION_VERSION)

    def test_identical_input_produces_an_identical_result(self):
        items = population(succeeded=95, failed=5)
        history = self._flat_history(hours=6, succeeded=95, failed=5)
        first = compute_metrics(items, HOUR, NOW, baseline_windows=history)
        second = compute_metrics(items, HOUR, NOW, baseline_windows=history)
        self.assertEqual(first, second)

    def _flat_history(self, hours: int, succeeded: int, failed: int, offset: int = 0):
        """``hours`` identical windows immediately before HOUR, as WindowCounts."""
        items = []
        windows = [
            TimeWindow(
                HOUR.start - timedelta(hours=index + 1 + offset),
                HOUR.start - timedelta(hours=index + offset),
            )
            for index in range(hours)
        ]
        for index, window in enumerate(windows):
            items.extend(
                population(
                    succeeded=succeeded,
                    failed=failed,
                    window=window,
                    prefix=f"h{index + offset}",
                )
            )
        return build_hourly_baseline(items, HOUR, hours + offset)


class BuildBaselineTests(unittest.TestCase):
    def test_hourly_baseline_excludes_the_incident_window_by_construction(self):
        """The single most dangerous defect in this layer.

        A window included in its own baseline dilutes the spike it is supposed to
        reveal — at 6 lookback hours it would understate the deviation by roughly a
        seventh, and the worse the incident the more it hides itself. Checked
        structurally (no bucket touches the window) rather than numerically.
        """
        buckets = build_hourly_baseline([], HOUR, 6)
        self.assertEqual(len(buckets), 6)
        for bucket in buckets:
            self.assertFalse(bucket.window.overlaps(HOUR))
            self.assertLessEqual(bucket.window.end, HOUR.start)

    def test_hourly_baseline_windows_are_contiguous_and_oldest_first(self):
        buckets = build_hourly_baseline([], HOUR, 4)
        self.assertEqual(buckets[-1].window.end, HOUR.start)
        for earlier, later in zip(buckets, buckets[1:]):
            self.assertEqual(earlier.window.end, later.window.start)

    def test_daily_hourly_baseline_covers_every_hour_of_the_lookback(self):
        buckets = build_daily_hourly_baseline([], HOUR, 3)
        self.assertEqual(len(buckets), 72)
        self.assertEqual(buckets[-1].window.end, HOUR.start)
        self.assertEqual(
            buckets[0].window.start, HOUR.start - timedelta(days=3)
        )

    def test_daily_hourly_baseline_requires_an_hour_aligned_window(self):
        # Otherwise "the same hour of day" would compare offset periods, which is
        # a wrong answer rather than an error.
        misaligned = TimeWindow(
            T0 + timedelta(minutes=17), T0 + timedelta(hours=1, minutes=17)
        )
        with self.assertRaises(DomainValidationError):
            build_daily_hourly_baseline([], misaligned, 3)

    def test_daily_hourly_baseline_rejects_a_non_positive_lookback(self):
        with self.assertRaises(DomainValidationError):
            build_daily_hourly_baseline([], HOUR, 0)
        with self.assertRaises(DomainValidationError):
            build_daily_hourly_baseline([], HOUR, True)  # type: ignore[arg-type]

    def test_same_hour_of_day_is_what_separates_a_pattern_from_an_incident(self):
        """The FALSE_ALARM / EVENING_FAILURE_SPIKE distinction, end to end.

        A merchant whose 10:00 hour always runs at 20% failure while the rest of
        the day runs at 5%. Compared against ALL hours, 20% looks like a 4x
        anomaly. Compared against the same hour on previous days it is exactly
        normal, and no incident should be opened.
        """
        history = []
        for day in range(1, 4):
            for hour in range(24):
                start = HOUR.start - timedelta(days=day) + timedelta(hours=hour - 10)
                window = TimeWindow(start, start + timedelta(hours=1))
                spike = start.hour == 10
                history.extend(
                    population(
                        succeeded=80 if spike else 95,
                        failed=20 if spike else 5,
                        window=window,
                        prefix=f"d{day}h{hour}",
                    )
                )

        buckets = build_daily_hourly_baseline(history, HOUR, 3)
        current = population(succeeded=80, failed=20)

        against_all = compute_metrics(
            current,
            HOUR,
            NOW,
            baseline_windows=buckets,
            comparable_mode=ComparableWindowMode.ALL,
        )
        against_same_hour = compute_metrics(
            current,
            HOUR,
            NOW,
            baseline_windows=buckets,
            comparable_mode=ComparableWindowMode.SAME_HOUR_OF_DAY,
        )

        # Same observed data, two different verdicts, and only one is right.
        self.assertGreater(against_all.deviation.absolute_percentage_points, Decimal("5"))
        self.assertEqual(
            against_same_hour.deviation.absolute_percentage_points, Decimal("0")
        )
        self.assertEqual(against_same_hour.baseline.windows_used, 3)


class PopulationDimensionBreakdownTests(unittest.TestCase):
    def _mixed_methods(self):
        # UPI is failing badly; cards are fine. 100 UPI (40 failed), 100 card
        # (2 failed).
        return (
            population(succeeded=60, failed=40, method=PaymentMethod.UPI, prefix="upi")
            + population(
                succeeded=98, failed=2, method=PaymentMethod.CARD, prefix="card"
            )
        )

    def test_slices_partition_the_population_exactly(self):
        items = self._mixed_methods()
        result = breakdown_by(items, Dimension.PAYMENT_METHOD, HOUR)
        self.assertEqual(total_counts_across(result), result.total_counts)
        self.assertEqual(result.total_counts.decided, 200)

    def test_per_slice_failure_rate_localises_the_problem(self):
        result = breakdown_by(self._mixed_methods(), Dimension.PAYMENT_METHOD, HOUR)
        rates = {item.value: item.failure_rate for item in result.slices}
        self.assertEqual(rates["upi"], Rate(40, 100))
        self.assertEqual(rates["card"], Rate(2, 100))

    def test_ordering_is_deterministic_worst_first(self):
        # Byte-identical evidence on repeat runs, and the leading slice is the
        # investigation's first candidate.
        result = breakdown_by(self._mixed_methods(), Dimension.PAYMENT_METHOD, HOUR)
        self.assertEqual(slice_values(result), ("upi", "card"))
        again = breakdown_by(self._mixed_methods(), Dimension.PAYMENT_METHOD, HOUR)
        self.assertEqual(slice_values(again), slice_values(result))

    def test_ties_break_on_value_so_order_never_wobbles(self):
        items = population(
            succeeded=90, failed=10, method=PaymentMethod.UPI, prefix="upi"
        ) + population(succeeded=90, failed=10, method=PaymentMethod.CARD, prefix="card")
        result = breakdown_by(items, Dimension.PAYMENT_METHOD, HOUR)
        self.assertEqual(slice_values(result), ("card", "upi"))

    def test_failed_gmv_is_per_slice(self):
        items = population(
            failed=2, amount_paise=500_00, method=PaymentMethod.UPI, prefix="upi"
        ) + population(failed=1, amount_paise=100_00, method=PaymentMethod.CARD, prefix="card")
        result = breakdown_by(items, Dimension.PAYMENT_METHOD, HOUR)
        by_value = {item.value: item.failed_gmv for item in result.slices}
        self.assertEqual(by_value["upi"], Money(1_000_00))
        self.assertEqual(by_value["card"], Money(100_00))

    def test_observed_dimensions_are_marked_observed(self):
        result = breakdown_by(self._mixed_methods(), Dimension.PAYMENT_METHOD, HOUR)
        for item in result.slices:
            self.assertIs(item.source_confidence, SourceConfidence.OBSERVED)

    def test_enriched_dimensions_are_marked_enriched(self):
        # Region and provider are inferred, not reported by Razorpay. A reader
        # must be able to see that before acting on a regional finding
        # (ARCHITECTURE.md 12.2).
        items = [
            enriched(payment(id=f"pay_{i}", status=PaymentStatus.FAILED, error_code="E"),
                     region="IN-KA", provider="acme")
            for i in range(5)
        ]
        for dimension in (Dimension.REGION, Dimension.PROVIDER):
            result = breakdown_by(items, dimension, HOUR)
            for item in result.slices:
                self.assertIs(item.source_confidence, SourceConfidence.ENRICHED)

    def test_missing_enrichment_becomes_an_explicit_unknown_slice(self):
        """Unattributable failures are named, not dropped.

        Dropping them would make the slices stop summing to the total, and a large
        unknown bucket is itself the finding: the enrichment is not working.
        """
        items = population(succeeded=90, failed=10)  # plain Payments, no region
        result = breakdown_by(items, Dimension.REGION, HOUR)
        self.assertEqual(slice_values(result), ("unknown",))
        self.assertEqual(total_counts_across(result), result.total_counts)

    def test_hour_of_day_slices_by_clock_hour(self):
        early = TimeWindow(T0, T0 + timedelta(hours=1))  # 10:00
        late = TimeWindow(T0 + timedelta(hours=9), T0 + timedelta(hours=10))  # 19:00
        items = population(succeeded=9, failed=1, window=early, prefix="am") + population(
            succeeded=5, failed=5, window=late, prefix="pm"
        )
        span = TimeWindow(T0, T0 + timedelta(hours=10))
        result = breakdown_by(items, Dimension.HOUR_OF_DAY, span)
        self.assertEqual(set(slice_values(result)), {"10", "19"})

    def test_rejects_a_non_dimension(self):
        with self.assertRaises(DomainValidationError):
            breakdown_by([], "payment_method", HOUR)  # type: ignore[arg-type]

    def test_rejects_a_non_window(self):
        with self.assertRaises(DomainValidationError):
            breakdown_by([], Dimension.PAYMENT_METHOD, None)  # type: ignore[arg-type]


class FailureAttributeDimensionTests(unittest.TestCase):
    """These dimensions exist only on failures, so their rates are vacuous."""

    def _coded_failures(self):
        items = population(succeeded=100, prefix="ok")
        for index in range(30):
            items.append(
                payment(
                    id=f"fail_{index:03d}",
                    status=PaymentStatus.FAILED,
                    error_code="GATEWAY_TIMEOUT" if index < 25 else "INSUFFICIENT_FUNDS",
                    minutes=index % 60,
                )
            )
        return items

    def test_successful_payments_are_excluded_entirely(self):
        result = breakdown_by(self._coded_failures(), Dimension.FAILURE_CODE, HOUR)
        # 130 payments in, 30 in the breakdown: a failure code is not a property
        # a successful payment has.
        self.assertEqual(total_counts_across(result).total, 30)
        self.assertEqual(result.total_counts.total, 130)

    def test_slice_failure_rate_is_trivially_one_and_therefore_useless(self):
        """Documented in an assertion because it is a trap.

        Every member of a failure-code slice failed, so the rate is 100% for every
        code — including a code responsible for one failure out of thousands.
        ``share_of_failures`` is the number that carries information.
        """
        result = breakdown_by(self._coded_failures(), Dimension.FAILURE_CODE, HOUR)
        for item in result.slices:
            self.assertEqual(item.failure_rate, Rate(item.counts.failed, item.counts.failed))
        self.assertEqual(share_of_failures(result, "GATEWAY_TIMEOUT"), Decimal("0.833333"))
        self.assertEqual(share_of_failures(result, "INSUFFICIENT_FUNDS"), Decimal("0.166667"))

    def test_shares_are_computed_against_the_whole_window_not_the_slices(self):
        # total_counts spans all 130 payments but only 30 failed, so the shares
        # must sum to 1 over failures.
        result = breakdown_by(self._coded_failures(), Dimension.FAILURE_CODE, HOUR)
        total = sum(share_of_failures(result, value) for value in slice_values(result))
        self.assertEqual(total, Decimal(1))

    def test_share_of_nothing_is_undefined_not_zero(self):
        result = breakdown_by(population(succeeded=50), Dimension.FAILURE_CODE, HOUR)
        self.assertIsNone(share_of_failures(result, "GATEWAY_TIMEOUT"))

    def test_share_of_an_absent_value_is_a_real_zero(self):
        # Different from the case above: failures happened, this code caused none
        # of them.
        result = breakdown_by(self._coded_failures(), Dimension.FAILURE_CODE, HOUR)
        self.assertEqual(share_of_failures(result, "NO_SUCH_CODE"), Decimal(0))

    def test_failures_without_a_code_are_bucketed_as_unknown(self):
        items = [payment(id="pay_1", status=PaymentStatus.FAILED)]  # no error_code
        result = breakdown_by(items, Dimension.FAILURE_CODE, HOUR)
        self.assertEqual(slice_values(result), ("unknown",))

    def test_failure_category_needs_enrichment_and_says_so_when_absent(self):
        plain = breakdown_by(
            [payment(id="pay_1", status=PaymentStatus.FAILED, error_code="E")],
            Dimension.FAILURE_CATEGORY,
            HOUR,
        )
        self.assertEqual(slice_values(plain), ("unknown",))

        categorised = breakdown_by(
            [
                enriched(
                    payment(id="pay_1", status=PaymentStatus.FAILED, error_code="E"),
                    failure_category=FailureCategory.GATEWAY_ERROR,
                )
            ],
            Dimension.FAILURE_CATEGORY,
            HOUR,
        )
        self.assertEqual(slice_values(categorised), (FailureCategory.GATEWAY_ERROR.value,))

    def test_share_of_failures_requires_a_breakdown(self):
        with self.assertRaises(DomainValidationError):
            share_of_failures({"upi": 5}, "upi")  # type: ignore[arg-type]


class TopContributorTests(unittest.TestCase):
    def test_names_the_worst_slice(self):
        items = population(
            succeeded=60, failed=40, method=PaymentMethod.UPI, prefix="upi"
        ) + population(succeeded=98, failed=2, method=PaymentMethod.CARD, prefix="card")
        leader = top_failure_contributor(
            breakdown_by(items, Dimension.PAYMENT_METHOD, HOUR)
        )
        self.assertEqual(leader.value, "upi")
        self.assertEqual(leader.counts.failed, 40)

    def test_none_when_nothing_failed(self):
        # Not the largest healthy slice: with no failures there is no contributor,
        # and returning one would invite the agent to investigate a non-event.
        result = breakdown_by(population(succeeded=200), Dimension.PAYMENT_METHOD, HOUR)
        self.assertIsNone(top_failure_contributor(result))

    def test_none_on_an_empty_breakdown(self):
        self.assertIsNone(
            top_failure_contributor(breakdown_by([], Dimension.PAYMENT_METHOD, HOUR))
        )

    def test_requires_a_breakdown(self):
        with self.assertRaises(DomainValidationError):
            top_failure_contributor(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
