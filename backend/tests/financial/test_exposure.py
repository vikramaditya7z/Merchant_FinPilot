"""Tests for ``financial.exposure`` — the money figures.

These are the numbers that reach a merchant's screen and justify an action, so
they are the numbers most worth attacking. Two failure modes get the most
attention here:

* **Conflating observed loss with incremental loss.** ``failed_gmv`` is every
  failure; ``revenue_at_risk`` is only the failures beyond normal. A healthy
  business fails payments every hour. Reporting the first as the cost of an
  incident overstates it by whatever the baseline is — often by several times.
* **Rounding twice.** Money is integer paise, ratios are ``Decimal``, and the
  conversion between them has to happen exactly once. Several tests below pin
  results that differ by a single paisa depending on where the rounding lands.
"""

import unittest
from decimal import Decimal

from ...domain.enums import Currency, PaymentStatus
from ...domain.errors import DomainValidationError
from ...domain.metrics import Rate, RecoveryAssumption, TransactionCounts
from ...domain.money import Money
from ...financial.exposure import (
    compute_revenue_risk,
    excess_failed_transactions,
    expected_failures,
    failed_gmv,
    mean_failed_ticket,
    recoverable_revenue,
    revenue_at_risk,
)
from ..helpers import payment, population


def assumption(rate: str = "0.30") -> RecoveryAssumption:
    return RecoveryAssumption(
        Decimal(rate), "operator input", "assumed recovery rate for these tests"
    )


class FailedGmvTests(unittest.TestCase):
    def test_counts_only_failures(self):
        # 3 x 100.00, not 18 x 100.00: successes and in-flight payments are not
        # losses.
        items = population(succeeded=10, failed=3, undecided=5, amount_paise=100_00)
        self.assertEqual(failed_gmv(items), Money(300_00))

    def test_no_failures_is_a_genuine_zero(self):
        # Unlike a rate, an empty sum of money really is zero: nothing was lost
        # (PROJECT_RULES 1.7). Returning None here would force every caller to
        # branch on a case that has an obvious correct answer.
        self.assertEqual(failed_gmv(population(succeeded=50)), Money.zero())

    def test_exact_over_a_large_population(self):
        """Integer paise, so no accumulated drift.

        10,000 failures of 123.45 sum to exactly 1,234,500.00. In float this sum
        would be off in the last places, and it is the kind of error nobody
        notices until a reconciliation fails.
        """
        items = population(failed=10_000, amount_paise=123_45)
        self.assertEqual(failed_gmv(items).minor_units, 10_000 * 123_45)

    def test_single_currency_populations_are_all_that_exist_today(self):
        """``assert_single_currency`` cannot be exercised for real yet.

        ``Currency`` has one member, so a genuinely mixed population is
        unconstructible. This test pins the passing case; adding a second currency
        must come with tests that make this guard fire (same gap recorded in
        test_money.py and test_calculations.py).
        """
        items = [
            payment(id="pay_1", status=PaymentStatus.FAILED, error_code="E"),
            payment(id="pay_2", status=PaymentStatus.FAILED, error_code="E"),
        ]
        self.assertEqual(failed_gmv(items), Money(200_00))


class MeanFailedTicketTests(unittest.TestCase):
    def test_none_when_nothing_failed(self):
        # Not zero: with no failures there is no average ticket, and a zero would
        # silently zero out anything derived from it (ADR-004).
        self.assertIsNone(mean_failed_ticket(population(succeeded=100)))

    def test_uneven_division_rounds_once_half_up(self):
        # 100 + 100 + 101 = 301 paise over 3 failures = 100.333... -> 100.
        items = [
            payment(id="pay_1", status=PaymentStatus.FAILED, amount_paise=100, error_code="E"),
            payment(id="pay_2", status=PaymentStatus.FAILED, amount_paise=100, error_code="E"),
            payment(id="pay_3", status=PaymentStatus.FAILED, amount_paise=101, error_code="E"),
        ]
        self.assertEqual(mean_failed_ticket(items), Money(100))

    def test_rounds_up_at_exactly_half(self):
        # 1 + 2 = 3 paise over 2 failures = 1.5 -> 2, not 1.
        items = [
            payment(id="pay_1", status=PaymentStatus.FAILED, amount_paise=1, error_code="E"),
            payment(id="pay_2", status=PaymentStatus.FAILED, amount_paise=2, error_code="E"),
        ]
        self.assertEqual(mean_failed_ticket(items), Money(2))

    def test_ignores_successful_payments_of_a_different_size(self):
        # Large successes must not drag the mean *failed* ticket around.
        items = population(failed=2, amount_paise=100, prefix="f") + population(
            succeeded=100, amount_paise=1_000_00, prefix="s"
        )
        self.assertEqual(mean_failed_ticket(items), Money(100))


class ExpectedFailuresTests(unittest.TestCase):
    def test_rounds_half_up_at_the_boundary(self):
        # 5% of 10 decided is exactly 0.5. Half-up gives 1; truncation would give
        # 0 and inflate the excess by one whole transaction.
        counts = TransactionCounts(succeeded=9, failed=1)
        self.assertEqual(expected_failures(counts, Rate(1, 20)), 1)

    def test_rounds_down_below_the_boundary(self):
        counts = TransactionCounts(succeeded=9, failed=1)
        self.assertEqual(expected_failures(counts, Rate(1, 40)), 0)  # 0.25

    def test_recurring_decimal_baseline(self):
        # A third of 100 is 33.333..., which must not become 34.
        counts = TransactionCounts(succeeded=50, failed=50)
        self.assertEqual(expected_failures(counts, Rate(1, 3)), 33)

    def test_uses_decided_population_not_total(self):
        """The bug that would make an incident disappear.

        90 succeeded, 10 failed, 400 still in flight. Against a 5% baseline the
        decided population predicts 5 failures, so 5 are excess. If ``total``
        (500) were used instead, 25 failures would be "expected", the excess would
        clamp to zero, and a live incident would read as normal.
        """
        counts = TransactionCounts(succeeded=90, failed=10, undecided=400)
        self.assertEqual(counts.decided, 100)
        self.assertEqual(expected_failures(counts, Rate(5, 100)), 5)
        self.assertEqual(excess_failed_transactions(counts, Rate(5, 100)), 5)

    def test_requires_typed_arguments(self):
        with self.assertRaises(DomainValidationError):
            expected_failures((90, 10), Rate(1, 20))  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            expected_failures(TransactionCounts(90, 10), Decimal("0.05"))  # type: ignore[arg-type]


class ExcessFailuresTests(unittest.TestCase):
    def test_better_than_baseline_clamps_to_zero(self):
        # 1 failure in 100 against a 5% baseline: 4 fewer than expected. The
        # excess is 0, not -4 (PROJECT_RULES 1.8).
        counts = TransactionCounts(succeeded=99, failed=1)
        self.assertEqual(excess_failed_transactions(counts, Rate(5, 100)), 0)

    def test_exactly_at_baseline_is_zero_excess(self):
        counts = TransactionCounts(succeeded=95, failed=5)
        self.assertEqual(excess_failed_transactions(counts, Rate(5, 100)), 0)

    def test_zero_baseline_makes_every_failure_excess(self):
        # A baseline of 0% is a strong claim, and its consequence is that all
        # observed failures are incremental.
        counts = TransactionCounts(succeeded=90, failed=10)
        self.assertEqual(excess_failed_transactions(counts, Rate(0, 500)), 10)

    def test_never_exceeds_observed_failures(self):
        counts = TransactionCounts(succeeded=90, failed=10)
        for baseline in (Rate(0, 100), Rate(1, 100), Rate(5, 100), Rate(50, 100)):
            self.assertLessEqual(
                excess_failed_transactions(counts, baseline), counts.failed
            )


class RevenueAtRiskTests(unittest.TestCase):
    def test_the_separation_that_prevents_a_5x_overstatement(self):
        """``failed_gmv`` and ``revenue_at_risk`` are different numbers.

        1000 decided, 100 failed at 100.00 = 10,000.00 of failed GMV. The
        overstatement from quoting that as the incident's cost is exactly
        failed/excess, so it grows with how normal the failures are:

        * 5% baseline -> 50 expected, 50 excess -> 5,000.00 at risk (2x)
        * 8% baseline -> 80 expected, 20 excess -> 2,000.00 at risk (5x)

        A merchant told 10,000.00 in the second case is being told their loss is
        five times what it is.
        """
        items = population(succeeded=900, failed=100, amount_paise=100_00)
        counts = TransactionCounts(succeeded=900, failed=100)
        self.assertEqual(failed_gmv(items), Money(10_000_00))
        self.assertEqual(revenue_at_risk(items, counts, Rate(5, 100)), Money(5_000_00))
        self.assertEqual(revenue_at_risk(items, counts, Rate(8, 100)), Money(2_000_00))

    def test_zero_baseline_makes_risk_equal_failed_gmv_exactly(self):
        # Every failure is excess, so the share is failed/failed = 1 and the two
        # figures coincide exactly — no rounding slack.
        items = population(succeeded=90, failed=10, amount_paise=333_33)
        counts = TransactionCounts(succeeded=90, failed=10)
        self.assertEqual(
            revenue_at_risk(items, counts, Rate(0, 500)), failed_gmv(items)
        )

    def test_single_rounding_step_beats_mean_times_count(self):
        """The reason the ratio form is used (PROJECT_RULES 4.5).

        Failures of 100, 100 and 101 paise; 2 of the 3 are excess.

        * ratio form:  301 x (2/3) = 200.666... -> 201
        * mean x count: (301/3 -> 100) x 2      -> 200

        One paisa on three transactions, and a systematic bias at scale, because
        the intermediate mean is truncated before it is multiplied.
        """
        items = [
            payment(id="pay_1", status=PaymentStatus.FAILED, amount_paise=100, error_code="E"),
            payment(id="pay_2", status=PaymentStatus.FAILED, amount_paise=100, error_code="E"),
            payment(id="pay_3", status=PaymentStatus.FAILED, amount_paise=101, error_code="E"),
        ] + population(succeeded=97, prefix="ok")
        counts = TransactionCounts(succeeded=97, failed=3)
        baseline = Rate(1, 100)  # expects 1 failure of 100 decided -> excess 2

        self.assertEqual(excess_failed_transactions(counts, baseline), 2)
        self.assertEqual(revenue_at_risk(items, counts, baseline), Money(201))

        naive = mean_failed_ticket(items).multiply_by_ratio(Decimal(2))
        self.assertEqual(naive, Money(200))
        self.assertNotEqual(naive, revenue_at_risk(items, counts, baseline))

    def test_rounds_up_at_exactly_half_a_paisa(self):
        # failed_gmv 3 paise, share 1/2 -> 1.5 paise -> 2.
        items = [
            payment(id="pay_1", status=PaymentStatus.FAILED, amount_paise=1, error_code="E"),
            payment(id="pay_2", status=PaymentStatus.FAILED, amount_paise=2, error_code="E"),
        ] + population(succeeded=98, prefix="ok")
        counts = TransactionCounts(succeeded=98, failed=2)
        self.assertEqual(revenue_at_risk(items, counts, Rate(1, 100)), Money(2))

    def test_zero_when_there_is_no_excess(self):
        items = population(succeeded=99, failed=1, amount_paise=100_00)
        counts = TransactionCounts(succeeded=99, failed=1)
        self.assertEqual(revenue_at_risk(items, counts, Rate(5, 100)), Money.zero())

    def test_never_negative_and_never_above_failed_gmv(self):
        items = population(succeeded=90, failed=10, amount_paise=777_77)
        counts = TransactionCounts(succeeded=90, failed=10)
        ceiling = failed_gmv(items)
        for baseline in (Rate(0, 100), Rate(1, 100), Rate(7, 100), Rate(10, 100), Rate(90, 100)):
            at_risk = revenue_at_risk(items, counts, baseline)
            self.assertGreaterEqual(at_risk, Money.zero())
            self.assertLessEqual(at_risk, ceiling)


class RecoverableRevenueTests(unittest.TestCase):
    def test_requires_an_explicit_assumption(self):
        # There is no default recovery rate and no way to omit one (ADR-007):
        # recovery is not observable from payment data.
        with self.assertRaises(DomainValidationError):
            recoverable_revenue(Money(100_00), None)  # type: ignore[arg-type]

    def test_applies_the_rate_and_stays_an_estimate(self):
        estimate = recoverable_revenue(Money(5_000_00), assumption("0.30"))
        self.assertEqual(estimate.amount, Money(1_500_00))
        self.assertTrue(estimate.is_estimate)
        # The assumption travels with the number so a reader can challenge it.
        self.assertEqual(estimate.assumption.rate, Decimal("0.30"))
        self.assertIn("assumed", estimate.assumption.rationale)

    def test_rejects_a_float_amount(self):
        with self.assertRaises(DomainValidationError):
            recoverable_revenue(50_000.0, assumption())  # type: ignore[arg-type]

    def test_rounds_once_on_an_awkward_rate(self):
        # 1 paisa at 30% is 0.3 -> 0. Sub-paisa recoveries round to nothing
        # rather than inventing a paisa.
        self.assertEqual(recoverable_revenue(Money(1), assumption("0.30")).amount, Money(0))
        # 2 paise at 30% is 0.6 -> 1.
        self.assertEqual(recoverable_revenue(Money(2), assumption("0.30")).amount, Money(1))


class ComputeRevenueRiskTests(unittest.TestCase):
    def _items_and_counts(self):
        items = population(succeeded=900, failed=100, amount_paise=100_00)
        return items, TransactionCounts(succeeded=900, failed=100)

    def test_no_baseline_means_no_loss_figure_at_all(self):
        """Returns ``None`` rather than a number computed against nothing.

        Without a baseline, "excess" is undefined. Any figure produced here would
        be a guess that reads as a fact once it reaches a dashboard
        (PROJECT_RULES 1.10).
        """
        items, counts = self._items_and_counts()
        self.assertIsNone(compute_revenue_risk(items, counts, None))

    def test_assembles_all_three_quantities(self):
        items, counts = self._items_and_counts()
        risk = compute_revenue_risk(items, counts, Rate(5, 100))
        self.assertEqual(risk.failed_gmv, Money(10_000_00))
        self.assertEqual(risk.excess_failed_transactions, 50)
        self.assertEqual(risk.mean_failed_ticket, Money(100_00))
        self.assertEqual(risk.revenue_at_risk, Money(5_000_00))

    def test_recoverable_is_absent_unless_an_assumption_is_supplied(self):
        items, counts = self._items_and_counts()
        self.assertIsNone(compute_revenue_risk(items, counts, Rate(5, 100)).recoverable)

        with_estimate = compute_revenue_risk(
            items, counts, Rate(5, 100), assumption=assumption("0.25")
        )
        self.assertIsNotNone(with_estimate.recoverable)
        # 25% of 5,000.00 at risk.
        self.assertEqual(with_estimate.recoverable.amount, Money(1_250_00))
        self.assertTrue(with_estimate.recoverable.is_estimate)

    def test_recoverable_can_never_exceed_revenue_at_risk(self):
        # The contract enforces this; here it is checked through the real
        # computation path, at a recovery rate of 100%.
        items, counts = self._items_and_counts()
        risk = compute_revenue_risk(
            items, counts, Rate(5, 100), assumption=assumption("1.00")
        )
        self.assertEqual(risk.recoverable.amount, risk.revenue_at_risk)

    def test_no_failures_yields_a_coherent_all_zero_picture(self):
        items = population(succeeded=500)
        counts = TransactionCounts(succeeded=500, failed=0)
        risk = compute_revenue_risk(items, counts, Rate(5, 100))
        self.assertEqual(risk.failed_gmv, Money.zero())
        self.assertEqual(risk.excess_failed_transactions, 0)
        self.assertEqual(risk.revenue_at_risk, Money.zero())
        # mean_failed_ticket is undefined; the contract needs a Money, so the
        # facade substitutes zero. Safe only because excess is also zero.
        self.assertEqual(risk.mean_failed_ticket, Money.zero())

    def test_currency_is_carried_through(self):
        items, counts = self._items_and_counts()
        risk = compute_revenue_risk(items, counts, Rate(5, 100), currency=Currency.INR)
        self.assertIs(risk.failed_gmv.currency, Currency.INR)
        self.assertIs(risk.revenue_at_risk.currency, Currency.INR)


if __name__ == "__main__":
    unittest.main()
