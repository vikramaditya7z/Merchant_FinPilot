"""Tests for ``domain.metrics`` — rates, counts, and the undefined-is-not-zero rule.

The central assertion in this file is that a rate over an empty population is
``None`` and never ``0`` (ADR-004). A zero failure rate means "nothing failed",
which is excellent news; ``None`` means "we have no idea", which may be an
outage. A system that renders the second as the first reports an outage as
health.
"""

import unittest
from datetime import timedelta
from decimal import Decimal

from ...domain.enums import BaselineMethod, Dimension, SourceConfidence
from ...domain.errors import DomainValidationError, MoneyPrecisionError
from ...domain.metrics import (
    RATE_PRECISION,
    BaselineFailureRate,
    Deviation,
    DimensionBreakdown,
    DimensionSlice,
    FinancialMetrics,
    Rate,
    RecoverableRevenue,
    RecoveryAssumption,
    RevenueRisk,
    SignificanceResult,
    TransactionCounts,
    WindowCounts,
)
from ...domain.money import Money
from ...domain.window import TimeWindow
from ..helpers import HOUR, NOW, T0


class RateTests(unittest.TestCase):
    def test_empty_population_gives_none_not_zero(self):
        # The single most important assertion in the suite.
        self.assertIsNone(Rate.of(0, 0))

    def test_zero_numerator_over_real_population_is_a_real_zero(self):
        rate = Rate.of(0, 100)
        self.assertIsNotNone(rate)
        self.assertEqual(rate.value, Decimal(0))

    def test_all_failed_is_exactly_one(self):
        self.assertEqual(Rate(50, 50).value, Decimal(1))

    def test_rate_remembers_its_population(self):
        # 3/4 and 750/1000 are the same quotient and very different evidence.
        small, large = Rate(3, 4), Rate(750, 1_000)
        self.assertEqual(small.value, large.value)
        self.assertNotEqual(small.denominator, large.denominator)

    def test_numerator_cannot_exceed_denominator(self):
        with self.assertRaises(DomainValidationError):
            Rate(11, 10)

    def test_direct_construction_with_zero_denominator_raises(self):
        with self.assertRaises(DomainValidationError):
            Rate(0, 0)

    def test_negative_counts_are_rejected(self):
        with self.assertRaises(DomainValidationError):
            Rate(-1, 10)
        with self.assertRaises(DomainValidationError):
            Rate.of(1, -10)

    def test_float_counts_are_rejected(self):
        with self.assertRaises(DomainValidationError):
            Rate(1.0, 10)  # type: ignore[arg-type]

    def test_complement(self):
        self.assertEqual(Rate(10, 100).complement(), Rate(90, 100))

    def test_as_percent_is_display_only(self):
        self.assertEqual(Rate(1, 3).as_percent(2), Decimal("33.33"))
        self.assertEqual(Rate(2, 3).as_percent(2), Decimal("66.67"))

    def test_success_and_failure_rates_sum_to_exactly_one(self):
        """The identity the engine relies on to compute both rates independently.

        If this ever failed, deriving one rate as ``1 - other`` would be the only
        safe option, and every rate would lose its numerator/denominator
        provenance. Checked across many denominators including the awkward
        thirds, sevenths and elevenths.
        """
        for denominator in range(1, 401):
            for numerator in {0, 1, denominator // 3, denominator // 2, denominator}:
                if numerator > denominator:
                    continue
                failure = Rate(numerator, denominator)
                success = Rate(denominator - numerator, denominator)
                self.assertEqual(
                    failure.value + success.value,
                    Decimal(1),
                    f"identity broke at {numerator}/{denominator} "
                    f"(precision {RATE_PRECISION})",
                )


class TransactionCountsTests(unittest.TestCase):
    def test_decided_excludes_undecided(self):
        counts = TransactionCounts(succeeded=90, failed=10, undecided=25)
        self.assertEqual(counts.decided, 100)
        self.assertEqual(counts.total, 125)

    def test_undecided_only_population_has_no_decided_members(self):
        # 50 in-flight payments and nothing settled. Any failure rate here is
        # undefined, not 0%.
        counts = TransactionCounts(succeeded=0, failed=0, undecided=50)
        self.assertFalse(counts.has_decided_population)
        self.assertIsNone(Rate.of(counts.failed, counts.decided))

    def test_addition(self):
        total = TransactionCounts(1, 2, 3) + TransactionCounts(10, 20, 30)
        self.assertEqual((total.succeeded, total.failed, total.undecided), (11, 22, 33))

    def test_negative_counts_rejected(self):
        with self.assertRaises(DomainValidationError):
            TransactionCounts(-1, 0, 0)

    def test_bool_counts_rejected(self):
        with self.assertRaises(DomainValidationError):
            TransactionCounts(True, 0, 0)


class WindowCountsTests(unittest.TestCase):
    def test_failure_rate_of_empty_window_is_none(self):
        empty = WindowCounts(HOUR, TransactionCounts(0, 0, 0))
        self.assertIsNone(empty.failure_rate)

    def test_failure_rate(self):
        counted = WindowCounts(HOUR, TransactionCounts(95, 5))
        self.assertEqual(counted.failure_rate, Rate(5, 100))


class BaselineFailureRateTests(unittest.TestCase):
    def test_insufficient_baseline_carries_none_and_says_why(self):
        baseline = BaselineFailureRate(
            method=BaselineMethod.POOLED,
            rate=None,
            windows_considered=3,
            windows_used=0,
            decided_sample=12,
            min_decided_required=100,
        )
        self.assertFalse(baseline.is_sufficient)
        self.assertIsNone(baseline.value)
        # The sample size survives so a reader can see how far short it fell.
        self.assertEqual(baseline.decided_sample, 12)

    def test_windows_used_cannot_exceed_considered(self):
        with self.assertRaises(DomainValidationError):
            BaselineFailureRate(BaselineMethod.POOLED, None, 2, 3, 0, 100)


class DeviationTests(unittest.TestCase):
    def test_relative_lift_may_be_none(self):
        # A zero-failure baseline makes lift undefined, not infinite.
        deviation = Deviation(
            current=Rate(5, 100),
            baseline=Rate(0, 500),
            absolute_percentage_points=Decimal("5"),
            relative_lift=None,
        )
        self.assertIsNone(deviation.relative_lift)
        self.assertTrue(deviation.is_worse_than_baseline)

    def test_float_percentage_points_rejected(self):
        with self.assertRaises(MoneyPrecisionError):
            Deviation(Rate(5, 100), Rate(1, 100), 4.0, None)  # type: ignore[arg-type]

    def test_improvement_is_not_worse_than_baseline(self):
        deviation = Deviation(
            Rate(1, 100), Rate(5, 100), Decimal("-4"), Decimal("0.2")
        )
        self.assertFalse(deviation.is_worse_than_baseline)


class SignificanceResultTests(unittest.TestCase):
    def test_floats_are_the_one_permitted_exception(self):
        result = SignificanceResult(
            z_score=2.2065, p_value=0.02735, current_decided=100, baseline_decided=2_400
        )
        self.assertIsInstance(result.z_score, float)

    def test_decimal_statistics_are_rejected(self):
        # Not because Decimal is wrong, but because the type must be consistent:
        # downstream code treats these as floats.
        with self.assertRaises(DomainValidationError):
            SignificanceResult(Decimal("2.2"), 0.03, 100, 100)  # type: ignore[arg-type]

    def test_p_value_out_of_range_rejected(self):
        with self.assertRaises(DomainValidationError):
            SignificanceResult(2.0, 1.5, 100, 100)

    def test_a_confident_p_value_on_thin_data_is_flagged_invalid(self):
        """The z-test's own precondition, carried on the result.

        p=0.002 with 0.63 expected failures is arithmetically correct and
        statistically inadmissible. A caller reading only ``p_value`` cannot tell
        this apart from a real incident.
        """
        thin = SignificanceResult(
            z_score=3.0915,
            p_value=0.00199,
            current_decided=12,
            baseline_decided=1_000,
            min_expected_count=0.63,
        )
        self.assertFalse(thin.normal_approximation_valid)

    def test_the_textbook_threshold_is_inclusive(self):
        # Exactly 5 expected events is conventionally sufficient, so the boundary
        # must not be exclusive.
        at_boundary = SignificanceResult(2.0, 0.05, 100, 1_000, min_expected_count=5.0)
        self.assertTrue(at_boundary.normal_approximation_valid)
        just_below = SignificanceResult(2.0, 0.05, 100, 1_000, min_expected_count=4.999)
        self.assertFalse(just_below.normal_approximation_valid)

    def test_min_expected_count_defaults_to_a_conservative_zero(self):
        # A result built without the field is treated as inadmissible rather than
        # as admissible-by-omission: a caller that forgets to supply it must not
        # accidentally get a clean bill of health.
        legacy = SignificanceResult(2.0, 0.05, 100, 1_000)
        self.assertEqual(legacy.min_expected_count, 0.0)
        self.assertFalse(legacy.normal_approximation_valid)

    def test_negative_min_expected_count_rejected(self):
        with self.assertRaises(DomainValidationError):
            SignificanceResult(2.0, 0.05, 100, 1_000, min_expected_count=-0.1)

    def test_non_float_min_expected_count_rejected(self):
        with self.assertRaises(DomainValidationError):
            SignificanceResult(2.0, 0.05, 100, 1_000, min_expected_count=5)  # type: ignore[arg-type]


class RecoveryEstimateTests(unittest.TestCase):
    def test_assumption_requires_source_and_rationale(self):
        with self.assertRaises(DomainValidationError):
            RecoveryAssumption(Decimal("0.3"), "", "a proper rationale here")
        with self.assertRaises(DomainValidationError):
            RecoveryAssumption(Decimal("0.3"), "manual", "short")

    def test_assumption_rejects_float_rate(self):
        with self.assertRaises(MoneyPrecisionError):
            RecoveryAssumption(0.3, "manual", "a proper rationale here")  # type: ignore[arg-type]

    def test_recoverable_revenue_cannot_be_presented_as_fact(self):
        assumption = RecoveryAssumption(
            Decimal("0.3"), "operator input", "assumed from prior recovery campaigns"
        )
        estimate = RecoverableRevenue(Money(10_000), assumption)
        self.assertTrue(estimate.is_estimate)
        with self.assertRaises(DomainValidationError):
            RecoverableRevenue(Money(10_000), assumption, is_estimate=False)


class RevenueRiskTests(unittest.TestCase):
    def _assumption(self) -> RecoveryAssumption:
        return RecoveryAssumption(
            Decimal("0.25"), "operator input", "assumed recovery rate for testing"
        )

    def test_recoverable_cannot_exceed_revenue_at_risk(self):
        with self.assertRaises(DomainValidationError):
            RevenueRisk(
                failed_gmv=Money(100_000),
                excess_failed_transactions=5,
                mean_failed_ticket=Money(10_000),
                revenue_at_risk=Money(50_000),
                recoverable=RecoverableRevenue(Money(60_000), self._assumption()),
            )

    def test_negative_money_rejected(self):
        with self.assertRaises(DomainValidationError):
            RevenueRisk(Money(-1), 0, Money(0), Money(0))

    def test_failed_gmv_may_exceed_revenue_at_risk(self):
        # The normal case, and the distinction the architecture insists on: all
        # 10 failures are 1000 of GMV, but only the 5 excess failures are the
        # incident's cost.
        risk = RevenueRisk(
            failed_gmv=Money(100_000),
            excess_failed_transactions=5,
            mean_failed_ticket=Money(10_000),
            revenue_at_risk=Money(50_000),
        )
        self.assertGreater(risk.failed_gmv, risk.revenue_at_risk)


class FinancialMetricsInvariantTests(unittest.TestCase):
    """The ADR-004 invariant, enforced in both directions."""

    def _metrics(self, counts, failure, success, **kwargs):
        return FinancialMetrics(
            window=HOUR,
            counts=counts,
            failure_rate=failure,
            success_rate=success,
            baseline=kwargs.get("baseline"),
            deviation=kwargs.get("deviation"),
            significance=kwargs.get("significance"),
            revenue_risk=kwargs.get("revenue_risk"),
            computed_at=NOW,
            computation_version="test-1",
        )

    def test_rates_required_when_population_is_decided(self):
        with self.assertRaises(DomainValidationError):
            self._metrics(TransactionCounts(90, 10), None, None)

    def test_rates_forbidden_when_population_is_empty(self):
        # Zero-filling an empty window is the failure mode this prevents.
        with self.assertRaises(DomainValidationError):
            self._metrics(TransactionCounts(0, 0, 5), Rate(0, 1), Rate(1, 1))

    def test_empty_window_is_valid_with_all_rates_none(self):
        metrics = self._metrics(TransactionCounts(0, 0, 5), None, None)
        self.assertFalse(metrics.has_sufficient_data)
        self.assertFalse(metrics.is_comparable_to_baseline)

    def test_deviation_requires_a_current_rate(self):
        with self.assertRaises(DomainValidationError):
            self._metrics(
                TransactionCounts(0, 0, 5),
                None,
                None,
                deviation=Deviation(Rate(1, 10), Rate(1, 100), Decimal("9"), None),
            )

    def test_naive_computed_at_is_rejected(self):
        from datetime import datetime

        with self.assertRaises(DomainValidationError):
            FinancialMetrics(
                window=HOUR,
                counts=TransactionCounts(1, 0),
                failure_rate=Rate(0, 1),
                success_rate=Rate(1, 1),
                baseline=None,
                deviation=None,
                significance=None,
                revenue_risk=None,
                computed_at=datetime(2026, 8, 20, 10, 0, 0),  # no tzinfo
                computation_version="test-1",
            )


class DimensionBreakdownTests(unittest.TestCase):
    def _slice(self, value, dimension=Dimension.PAYMENT_METHOD, failed=1):
        return DimensionSlice(
            dimension=dimension,
            value=value,
            counts=TransactionCounts(10, failed),
            failed_gmv=Money(1_000),
        )

    def test_mismatched_slice_dimension_rejected(self):
        with self.assertRaises(DomainValidationError):
            DimensionBreakdown(
                dimension=Dimension.PAYMENT_METHOD,
                window=HOUR,
                slices=(self._slice("upi"), self._slice("IN-KA", Dimension.REGION)),
                total_counts=TransactionCounts(20, 2),
            )

    def test_duplicate_values_rejected(self):
        with self.assertRaises(DomainValidationError):
            DimensionBreakdown(
                dimension=Dimension.PAYMENT_METHOD,
                window=HOUR,
                slices=(self._slice("upi"), self._slice("upi")),
                total_counts=TransactionCounts(20, 2),
            )

    def test_slice_carries_source_confidence(self):
        enriched_slice = DimensionSlice(
            dimension=Dimension.REGION,
            value="IN-KA",
            counts=TransactionCounts(10, 5),
            failed_gmv=Money(5_000),
            source_confidence=SourceConfidence.ENRICHED,
        )
        self.assertIs(enriched_slice.source_confidence, SourceConfidence.ENRICHED)

    def test_slice_failure_rate_of_empty_slice_is_none(self):
        empty = DimensionSlice(
            dimension=Dimension.PAYMENT_METHOD,
            value="emi",
            counts=TransactionCounts(0, 0, 3),
            failed_gmv=Money.zero(),
        )
        self.assertIsNone(empty.failure_rate)


class WindowTests(unittest.TestCase):
    """Half-open windows must tile exactly, or transactions get double-counted."""

    def test_windows_are_half_open(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        self.assertTrue(window.contains(T0))
        self.assertFalse(window.contains(T0 + timedelta(hours=1)))

    def test_adjacent_windows_do_not_overlap(self):
        first = TimeWindow(T0, T0 + timedelta(hours=1))
        second = TimeWindow(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
        self.assertFalse(first.overlaps(second))
        # And the boundary instant belongs to exactly one of them.
        boundary = T0 + timedelta(hours=1)
        self.assertNotEqual(first.contains(boundary), second.contains(boundary))

    def test_zero_length_window_rejected(self):
        with self.assertRaises(DomainValidationError):
            TimeWindow(T0, T0)

    def test_reversed_window_rejected(self):
        with self.assertRaises(DomainValidationError):
            TimeWindow(T0 + timedelta(hours=1), T0)

    def test_naive_datetime_rejected(self):
        from datetime import datetime

        with self.assertRaises(DomainValidationError):
            TimeWindow(datetime(2026, 8, 20, 10), datetime(2026, 8, 20, 11))

    def test_preceding_window_is_strictly_before(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        previous = window.preceding()
        self.assertEqual(previous.end, window.start)
        self.assertFalse(previous.overlaps(window))


if __name__ == "__main__":
    unittest.main()
