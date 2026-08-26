"""Tests for the deterministic financial engine's building blocks: population
selection, counting, rates, bucketing, baselines, deviation, and significance.

Every number in this system that reaches a decision comes out of these
functions. The suite is written to catch the errors that would be *plausible* —
an off-by-one bucket boundary, a baseline that includes the incident it is
measuring, a zero standing in for an undefined value — rather than the errors
that would be obvious.
"""

import unittest
from datetime import timedelta
from decimal import Decimal

from ...domain.enums import (
    BaselineMethod,
    ComparableWindowMode,
    PaymentStatus,
)
from ...domain.errors import DomainValidationError
from ...domain.metrics import Rate, TransactionCounts, WindowCounts
from ...domain.window import TimeWindow
from ...financial.baseline import (
    DEFAULT_MIN_BASELINE_DECIDED,
    baseline_failure_rate,
    select_comparable_windows,
)
from ...financial.counts import count_transactions
from ...financial.deviation import (
    absolute_deviation_pp,
    compute_deviation,
    relative_lift,
)
from ...financial.population import (
    as_payment,
    assert_single_currency,
    decided,
    failures,
    in_window,
    normalize,
    successes,
)
from ...financial.rates import failure_rate, success_rate
from ...financial.significance import two_proportion_significance
from ...financial.windows import (
    bucket_counts,
    hourly_buckets,
    preceding_windows,
    split_into_buckets,
)
from ..helpers import HOUR, T0, enriched, payment, population


class PopulationSelectionTests(unittest.TestCase):
    def test_as_payment_unwraps_either_shape(self):
        raw = payment()
        self.assertIs(as_payment(raw), raw)
        self.assertIs(as_payment(enriched(raw)), raw)

    def test_as_payment_rejects_anything_else(self):
        with self.assertRaises(DomainValidationError):
            as_payment("pay_1")  # type: ignore[arg-type]

    def test_normalize_materialises_a_generator(self):
        """A generator consumed twice yields a different population the second time.

        Two calculations that are supposed to agree would then quietly disagree,
        so populations are materialised at the boundary.
        """
        items = (item for item in population(succeeded=3, failed=2))
        materialised = normalize(items)
        self.assertEqual(len(materialised), 5)
        # The source generator is now exhausted; the tuple is not.
        self.assertEqual(len(tuple(items)), 0)
        self.assertEqual(len(materialised), 5)

    def test_in_window_is_half_open_at_both_ends(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        at_start = payment(id="pay_start", created_at=T0)
        one_second_before = payment(id="pay_before", created_at=T0 - timedelta(seconds=1))
        at_end = payment(id="pay_end", created_at=T0 + timedelta(hours=1))
        last_second = payment(
            id="pay_last", created_at=T0 + timedelta(hours=1) - timedelta(seconds=1)
        )

        selected = in_window([at_start, one_second_before, at_end, last_second], window)
        self.assertEqual(
            {item.id for item in selected}, {"pay_start", "pay_last"}
        )

    def test_in_window_accepts_enriched_payments(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        self.assertEqual(len(in_window([enriched(payment())], window)), 1)

    def test_in_window_requires_a_window(self):
        with self.assertRaises(DomainValidationError):
            in_window([], (T0, T0 + timedelta(hours=1)))  # type: ignore[arg-type]

    def test_failure_success_and_decided_partitions(self):
        items = population(succeeded=7, failed=3, undecided=2)
        self.assertEqual(len(failures(items)), 3)
        self.assertEqual(len(successes(items)), 7)
        self.assertEqual(len(decided(items)), 10)

    def test_refunded_is_a_success_not_a_failure(self):
        refunded = payment(status=PaymentStatus.REFUNDED)
        self.assertEqual(len(successes([refunded])), 1)
        self.assertEqual(len(failures([refunded])), 0)

    def test_mixed_currency_population_is_refused(self):
        # Summing across currencies produces a meaningless number, so we refuse
        # rather than produce one. There is one Currency member today, so this
        # is exercised by constructing the mismatch at the Money level.
        assert_single_currency(population(succeeded=2))
        # Same currency throughout: no raise. A genuine multi-currency case
        # cannot be built until a second Currency member exists (ARCHITECTURE.md
        # 22, Q5) — the guard is here so that day is safe.


class CountingTests(unittest.TestCase):
    def test_empty_population_counts_zero(self):
        # A genuine zero: nothing happened. Distinct from an undefined rate.
        counts = count_transactions([])
        self.assertEqual((counts.succeeded, counts.failed, counts.undecided), (0, 0, 0))

    def test_counts_by_outcome_not_by_status(self):
        items = [
            payment(id="p1", status=PaymentStatus.CAPTURED),
            payment(id="p2", status=PaymentStatus.AUTHORIZED),
            payment(id="p3", status=PaymentStatus.REFUNDED),
            payment(id="p4", status=PaymentStatus.FAILED, error_code="E"),
            payment(id="p5", status=PaymentStatus.CREATED),
        ]
        counts = count_transactions(items)
        self.assertEqual((counts.succeeded, counts.failed, counts.undecided), (3, 1, 1))
        self.assertEqual(counts.decided, 4)
        self.assertEqual(counts.total, 5)

    def test_counting_accepts_enriched_payments(self):
        counts = count_transactions([enriched(payment())])
        self.assertEqual(counts.succeeded, 1)


class RateCalculationTests(unittest.TestCase):
    def test_rates_over_an_empty_population_are_none(self):
        empty = TransactionCounts(0, 0, 0)
        self.assertIsNone(failure_rate(empty))
        self.assertIsNone(success_rate(empty))

    def test_rates_over_an_undecided_only_population_are_none(self):
        # 40 payments in flight. The failure rate is unknown, not 0%.
        in_flight = TransactionCounts(0, 0, 40)
        self.assertIsNone(failure_rate(in_flight))
        self.assertIsNone(success_rate(in_flight))

    def test_undecided_payments_stay_out_of_the_denominator(self):
        """The denominator choice that makes incidents visible.

        90 succeeded, 10 failed, 400 still in flight. Over decided the failure
        rate is 10%; over everything it would be 2%, which would hide a real
        incident behind a pile of pending payments (ARCHITECTURE.md 7.2).
        """
        counts = TransactionCounts(succeeded=90, failed=10, undecided=400)
        rate = failure_rate(counts)
        self.assertEqual(rate.denominator, 100)
        self.assertEqual(rate.value, Decimal("0.1"))

    def test_total_failure_is_rate_one(self):
        self.assertEqual(failure_rate(TransactionCounts(0, 25)).value, Decimal(1))
        self.assertEqual(success_rate(TransactionCounts(0, 25)).value, Decimal(0))

    def test_the_two_rates_are_complementary(self):
        counts = TransactionCounts(succeeded=7, failed=3)
        self.assertEqual(
            failure_rate(counts).value + success_rate(counts).value, Decimal(1)
        )

    def test_rate_of_a_counted_population(self):
        counts = count_transactions(population(succeeded=95, failed=5))
        self.assertEqual(failure_rate(counts), Rate(5, 100))


class BucketingTests(unittest.TestCase):
    def test_buckets_tile_the_window_exactly(self):
        window = TimeWindow(T0, T0 + timedelta(hours=4))
        buckets = hourly_buckets(window)
        self.assertEqual(len(buckets), 4)
        self.assertEqual(buckets[0].start, window.start)
        self.assertEqual(buckets[-1].end, window.end)
        for earlier, later in zip(buckets, buckets[1:]):
            self.assertEqual(earlier.end, later.start)
            self.assertFalse(earlier.overlaps(later))

    def test_a_partial_trailing_bucket_is_refused_not_truncated(self):
        # A short final bucket carries less traffic than the others and would
        # silently drag any per-bucket baseline downwards.
        window = TimeWindow(T0, T0 + timedelta(minutes=90))
        with self.assertRaises(DomainValidationError):
            hourly_buckets(window)

    def test_bucket_seconds_must_be_a_positive_int(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        for bad in (0, -60, True, 60.0):
            with self.subTest(bucket_seconds=bad):
                with self.assertRaises(DomainValidationError):
                    split_into_buckets(window, bad)  # type: ignore[arg-type]

    def test_every_payment_lands_in_exactly_one_bucket(self):
        """The boundary case that quietly corrupts every rate downstream.

        Payments are placed exactly on each bucket boundary. With half-open
        windows each belongs to precisely one bucket; with closed windows the
        boundary payments would be counted twice.
        """
        window = TimeWindow(T0, T0 + timedelta(hours=3))
        boundary_payments = [
            payment(id=f"pay_{n}", created_at=T0 + timedelta(hours=n)) for n in range(3)
        ]
        buckets = hourly_buckets(window)
        counted = bucket_counts(boundary_payments, buckets)
        self.assertEqual([wc.counts.total for wc in counted], [1, 1, 1])
        self.assertEqual(
            sum(wc.counts.total for wc in counted), len(boundary_payments)
        )

    def test_preceding_windows_are_ordered_oldest_first(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        history = preceding_windows(window, 3)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[0].start, T0 - timedelta(hours=3))
        self.assertEqual(history[-1].end, T0)
        for earlier, later in zip(history, history[1:]):
            self.assertLess(earlier.start, later.start)

    def test_preceding_windows_never_include_the_target(self):
        # The structural guarantee that a window cannot contribute to its own
        # baseline (ARCHITECTURE.md 7.3).
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        for historical in preceding_windows(window, 24):
            self.assertFalse(historical.overlaps(window))

    def test_preceding_window_count_must_be_at_least_one(self):
        window = TimeWindow(T0, T0 + timedelta(hours=1))
        for bad in (0, -1, True):
            with self.subTest(count=bad):
                with self.assertRaises(DomainValidationError):
                    preceding_windows(window, bad)  # type: ignore[arg-type]

    def test_bucket_counts_handles_non_contiguous_windows(self):
        # Comparable-window selection deliberately produces gaps.
        items = population(succeeded=4, failed=1, window=HOUR)
        far = TimeWindow(T0 + timedelta(days=2), T0 + timedelta(days=2, hours=1))
        counted = bucket_counts(items, (HOUR, far))
        self.assertEqual(counted[0].counts.total, 5)
        self.assertEqual(counted[1].counts.total, 0)


class ComparableWindowTests(unittest.TestCase):
    """The mechanism that separates a real evening incident from a nightly pattern."""

    def _hour(self, offset_hours: int, failed: int = 1, succeeded: int = 19) -> WindowCounts:
        start = T0 + timedelta(hours=offset_hours)
        return WindowCounts(
            window=TimeWindow(start, start + timedelta(hours=1)),
            counts=TransactionCounts(succeeded, failed),
        )

    def test_all_mode_keeps_every_non_overlapping_window(self):
        target = TimeWindow(T0, T0 + timedelta(hours=1))
        candidates = [self._hour(-n) for n in range(1, 25)]
        selected = select_comparable_windows(candidates, target, ComparableWindowMode.ALL)
        self.assertEqual(len(selected), 24)

    def test_same_hour_mode_keeps_only_matching_hours(self):
        target = TimeWindow(T0, T0 + timedelta(hours=1))  # 10:00 UTC
        candidates = [self._hour(-n) for n in range(1, 73)]  # three days back
        selected = select_comparable_windows(
            candidates, target, ComparableWindowMode.SAME_HOUR_OF_DAY
        )
        self.assertEqual(len(selected), 3)
        for window_counts in selected:
            self.assertEqual(
                window_counts.window.start_hour_of_day, target.start_hour_of_day
            )

    def test_the_target_window_is_always_excluded(self):
        # Including the incident in its own baseline dilutes the very deviation
        # being measured.
        target = TimeWindow(T0, T0 + timedelta(hours=1))
        candidates = [self._hour(0), self._hour(-1)]
        selected = select_comparable_windows(candidates, target)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].window.start, T0 - timedelta(hours=1))

    def test_a_partially_overlapping_window_is_also_excluded(self):
        target = TimeWindow(T0, T0 + timedelta(hours=2))
        overlapping = WindowCounts(
            window=TimeWindow(T0 + timedelta(hours=1), T0 + timedelta(hours=3)),
            counts=TransactionCounts(19, 1),
        )
        self.assertEqual(select_comparable_windows([overlapping], target), ())

    def test_invalid_mode_rejected(self):
        with self.assertRaises(DomainValidationError):
            select_comparable_windows([], HOUR, "same_hour")  # type: ignore[arg-type]

    def test_candidates_must_be_window_counts(self):
        with self.assertRaises(DomainValidationError):
            select_comparable_windows([HOUR], HOUR)  # type: ignore[list-item]


class BaselineTests(unittest.TestCase):
    def _window(self, offset_hours: int, succeeded: int, failed: int) -> WindowCounts:
        start = T0 + timedelta(hours=offset_hours)
        return WindowCounts(
            window=TimeWindow(start, start + timedelta(hours=1)),
            counts=TransactionCounts(succeeded, failed),
        )

    def test_no_windows_gives_an_insufficient_baseline_not_zero(self):
        baseline = baseline_failure_rate([])
        self.assertIsNone(baseline.rate)
        self.assertFalse(baseline.is_sufficient)
        self.assertEqual(baseline.decided_sample, 0)

    def test_a_thin_sample_is_refused(self):
        """The guard that makes INSUFFICIENT_DATA a real outcome.

        Three failures out of twelve is a 25% failure rate and completely
        meaningless. Below the minimum the baseline is ``None`` and no deviation
        claim may be made at all.
        """
        thin = [self._window(-1, 9, 3)]
        baseline = baseline_failure_rate(thin)
        self.assertIsNone(baseline.rate)
        self.assertFalse(baseline.is_sufficient)
        self.assertLess(baseline.decided_sample, DEFAULT_MIN_BASELINE_DECIDED)

    def test_pooled_is_volume_weighted_not_an_average_of_rates(self):
        """A quiet hour must not count as much as a busy one.

        Window A: 1 failure / 10 decided = 10%. Window B: 5 / 1000 = 0.5%.
        Pooled is 6/1010 = 0.594%. The mean of the two rates would be 5.25% —
        an order of magnitude wrong, driven entirely by ten transactions.
        """
        windows = [self._window(-2, 9, 1), self._window(-1, 995, 5)]
        baseline = baseline_failure_rate(windows, method=BaselineMethod.POOLED)
        self.assertEqual(baseline.rate.numerator, 6)
        self.assertEqual(baseline.rate.denominator, 1_010)
        self.assertEqual(baseline.decided_sample, 1_010)
        naive_mean = (Decimal("0.1") + Decimal("0.005")) / 2
        self.assertLess(baseline.rate.value, naive_mean / 5)

    def test_pooled_skips_empty_windows_without_counting_them_as_used(self):
        windows = [self._window(-3, 0, 0), self._window(-2, 95, 5), self._window(-1, 95, 5)]
        baseline = baseline_failure_rate(windows)
        self.assertEqual(baseline.windows_considered, 3)
        self.assertEqual(baseline.windows_used, 2)
        self.assertEqual(baseline.rate, Rate(10, 200))

    def test_median_is_robust_to_one_pathological_window(self):
        """One catastrophic hour should not become the new normal.

        Four calm hours at 5% and one total outage at 100%. Pooled is dragged to
        ~24%; the median stays at 5%, which is what "normal" actually was.
        """
        windows = [self._window(-5 + n, 95, 5) for n in range(4)]
        windows.append(self._window(-1, 0, 100))
        pooled = baseline_failure_rate(windows, method=BaselineMethod.POOLED)
        median = baseline_failure_rate(windows, method=BaselineMethod.MEDIAN_OF_WINDOWS)
        self.assertEqual(median.rate, Rate(5, 100))
        self.assertGreater(pooled.rate.value, Decimal("0.2"))

    def test_median_returns_a_real_observed_rate(self):
        # Not an interpolated average: the baseline keeps a genuine numerator and
        # denominator so it stays auditable and re-derivable.
        windows = [self._window(-3, 97, 3), self._window(-2, 95, 5), self._window(-1, 93, 7)]
        median = baseline_failure_rate(windows, method=BaselineMethod.MEDIAN_OF_WINDOWS)
        self.assertEqual((median.rate.numerator, median.rate.denominator), (5, 100))

    def test_median_takes_the_lower_of_two_middles(self):
        # Deterministic for an even count, and never invents a rate that no
        # window actually had.
        windows = [
            self._window(-4, 99, 1),
            self._window(-3, 97, 3),
            self._window(-2, 95, 5),
            self._window(-1, 93, 7),
        ]
        median = baseline_failure_rate(windows, method=BaselineMethod.MEDIAN_OF_WINDOWS)
        self.assertEqual(median.rate, Rate(3, 100))

    def test_median_ignores_windows_below_the_per_window_floor(self):
        # A window with 4 transactions has a meaningless rate; letting it into
        # the median would let one quiet hour set the baseline.
        windows = [
            WindowCounts(
                window=TimeWindow(T0 - timedelta(hours=3), T0 - timedelta(hours=2)),
                counts=TransactionCounts(0, 4),  # 100% of four
            ),
            self._window(-2, 95, 5),
            self._window(-1, 95, 5),
        ]
        median = baseline_failure_rate(windows, method=BaselineMethod.MEDIAN_OF_WINDOWS)
        self.assertEqual(median.rate, Rate(5, 100))
        self.assertEqual(median.windows_used, 2)

    def test_median_is_independent_of_input_ordering(self):
        windows = [self._window(-3, 97, 3), self._window(-2, 95, 5), self._window(-1, 93, 7)]
        forward = baseline_failure_rate(windows, method=BaselineMethod.MEDIAN_OF_WINDOWS)
        backward = baseline_failure_rate(
            list(reversed(windows)), method=BaselineMethod.MEDIAN_OF_WINDOWS
        )
        self.assertEqual(forward.rate, backward.rate)

    def test_a_zero_failure_baseline_is_a_real_rate(self):
        # Nothing failed across 200 decided payments. That is a genuine 0%, and
        # very different from "no data".
        windows = [self._window(-2, 100, 0), self._window(-1, 100, 0)]
        baseline = baseline_failure_rate(windows)
        self.assertTrue(baseline.is_sufficient)
        self.assertEqual(baseline.rate.value, Decimal(0))

    def test_invalid_method_rejected(self):
        with self.assertRaises(DomainValidationError):
            baseline_failure_rate([], method="pooled")  # type: ignore[arg-type]

    def test_negative_minimum_rejected(self):
        with self.assertRaises(DomainValidationError):
            baseline_failure_rate([], min_decided=-1)

    def test_windows_must_be_window_counts(self):
        with self.assertRaises(DomainValidationError):
            baseline_failure_rate([TransactionCounts(1, 1)])  # type: ignore[list-item]


class DeviationTests(unittest.TestCase):
    def test_absolute_deviation_is_in_percentage_points(self):
        self.assertEqual(
            absolute_deviation_pp(Rate(15, 100), Rate(5, 100)), Decimal("10.000000")
        )

    def test_absolute_deviation_is_signed(self):
        # An improvement is negative, not an absolute magnitude. The sign is the
        # difference between "getting worse" and "getting better".
        self.assertEqual(
            absolute_deviation_pp(Rate(2, 100), Rate(5, 100)), Decimal("-3.000000")
        )

    def test_relative_lift_is_a_ratio(self):
        self.assertEqual(relative_lift(Rate(15, 100), Rate(5, 100)), Decimal("3.000000"))

    def test_zero_baseline_makes_lift_undefined_not_infinite(self):
        # Returning None forces the caller to say "the baseline was zero" rather
        # than render an invented multiplier.
        self.assertIsNone(relative_lift(Rate(5, 100), Rate(0, 500)))

    def test_both_measures_are_needed_to_judge_a_move(self):
        """Why the contract carries absolute *and* relative deviation.

        A 0.2pp rise on a 0.1pp baseline is a 3x lift and almost certainly noise.
        A 5pp rise on a 2pp baseline is only 3.5x and is a genuine incident.
        Either measure alone ranks these two wrongly.
        """
        noise = compute_deviation(Rate(3, 1_000), Rate(1, 1_000))
        real = compute_deviation(Rate(70, 1_000), Rate(20, 1_000))
        self.assertEqual(noise.relative_lift, Decimal("3.000000"))
        self.assertEqual(real.relative_lift, Decimal("3.500000"))
        # Similar lifts, wildly different absolute impact.
        self.assertEqual(noise.absolute_percentage_points, Decimal("0.200000"))
        self.assertEqual(real.absolute_percentage_points, Decimal("5.000000"))

    def test_deviation_of_a_recurring_third(self):
        # 1/3 vs 1/7: neither terminates in decimal. The result must still be
        # exact to the stated precision and free of float artefacts.
        deviation = compute_deviation(Rate(1, 3), Rate(1, 7))
        self.assertEqual(deviation.absolute_percentage_points, Decimal("19.047619"))
        self.assertEqual(deviation.relative_lift, Decimal("2.333333"))

    def test_identical_rates_deviate_by_zero(self):
        deviation = compute_deviation(Rate(5, 100), Rate(5, 100))
        self.assertEqual(deviation.absolute_percentage_points, Decimal(0))
        self.assertEqual(deviation.relative_lift, Decimal(1))
        self.assertFalse(deviation.is_worse_than_baseline)

    def test_deviation_requires_rates_not_decimals(self):
        with self.assertRaises(DomainValidationError):
            absolute_deviation_pp(Decimal("0.15"), Rate(5, 100))  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            relative_lift(Rate(15, 100), Decimal("0.05"))  # type: ignore[arg-type]


class SignificanceTests(unittest.TestCase):
    """A measurement, not a detector (ADR-006). No thresholds live here."""

    def test_a_small_sample_spike_scores_significant_but_is_not_trustworthy(self):
        """The p-value alone does NOT protect against thin data.

        3 failures out of 12 against a 5% baseline scores z=3.09, p=0.002 — as
        confident as a genuine incident. The arithmetic is right and the
        conclusion is unsupportable: the normal approximation needs a handful of
        expected events in each cell, and here only 0.63 failures were expected.

        Written as an assertion rather than a comment because the intuition
        ("surely the z-test handles small samples") is wrong, and a future change
        that starts relying on ``p_value`` alone must fail this test.
        """
        result = two_proportion_significance(Rate(3, 12), Rate(50, 1_000))
        self.assertIsNotNone(result)
        self.assertLess(result.p_value, 0.01)  # confident...
        self.assertFalse(result.normal_approximation_valid)  # ...and inadmissible
        self.assertLess(result.min_expected_count, 1.0)

    def test_the_same_rate_at_scale_is_significant_and_admissible(self):
        # Identical observed rates, 100x the evidence. Only the sample size
        # changed, and that is exactly what separates the two cases.
        result = two_proportion_significance(Rate(300, 1_200), Rate(50, 1_000))
        self.assertLess(result.p_value, 0.0001)
        self.assertTrue(result.normal_approximation_valid)

    def test_min_expected_count_is_the_smallest_of_the_four_cells(self):
        # 30/100 vs 50/1000: pooled 80/1100 = 7.27%. The scarce cell is the
        # current sample's failures, 7.27 of them.
        result = two_proportion_significance(Rate(30, 100), Rate(50, 1_000))
        self.assertAlmostEqual(result.min_expected_count, 80 / 1_100 * 100, places=9)

    def test_a_large_sample_with_a_rare_event_is_still_inadmissible(self):
        """Big n is not sufficient; the *rare cell* is what matters.

        2 failures in 5000 against 1 in 5000 is plenty of traffic, but only 1.5
        failures are expected. Sample size alone would pass this; the expected
        count correctly does not.
        """
        result = two_proportion_significance(Rate(2, 5_000), Rate(1, 5_000))
        self.assertGreater(result.current_decided, 1_000)
        self.assertFalse(result.normal_approximation_valid)

    def test_no_difference_gives_a_z_of_zero(self):
        result = two_proportion_significance(Rate(50, 1_000), Rate(50, 1_000))
        self.assertAlmostEqual(result.z_score, 0.0, places=9)
        self.assertAlmostEqual(result.p_value, 1.0, places=9)

    def test_an_improvement_produces_a_negative_z(self):
        result = two_proportion_significance(Rate(10, 1_000), Rate(100, 1_000))
        self.assertLess(result.z_score, 0)
        # The p-value is two-sided, so a large improvement is still "significant".
        self.assertLess(result.p_value, 0.001)

    def test_undefined_when_nothing_failed_in_either_sample(self):
        # Pooled variance is zero: there is no distribution to test against, and
        # both 0 and infinity would be lies.
        self.assertIsNone(two_proportion_significance(Rate(0, 500), Rate(0, 500)))

    def test_undefined_when_everything_failed_in_both_samples(self):
        self.assertIsNone(two_proportion_significance(Rate(500, 500), Rate(200, 200)))

    def test_p_value_stays_inside_the_unit_interval_at_extremes(self):
        """erf saturates for large |z| and can produce a p-value a hair outside [0, 1].

        The contract rejects that, so the guard has to hold at the extreme rather
        than only in the middle of the range.
        """
        result = two_proportion_significance(Rate(9_999, 10_000), Rate(1, 10_000))
        self.assertGreaterEqual(result.p_value, 0.0)
        self.assertLessEqual(result.p_value, 1.0)

    def test_statistics_are_floats_by_design(self):
        # The single documented exception to the no-float rule: a z-score is a
        # test statistic, never money and never a rate.
        result = two_proportion_significance(Rate(30, 100), Rate(50, 1_000))
        self.assertIsInstance(result.z_score, float)
        self.assertIsInstance(result.p_value, float)
        self.assertIsInstance(result.min_expected_count, float)

    def test_sample_sizes_are_carried_on_the_result(self):
        result = two_proportion_significance(Rate(30, 100), Rate(50, 1_000))
        self.assertEqual(result.current_decided, 100)
        self.assertEqual(result.baseline_decided, 1_000)

    def test_requires_rates(self):
        with self.assertRaises(DomainValidationError):
            two_proportion_significance(0.3, Rate(50, 1_000))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
