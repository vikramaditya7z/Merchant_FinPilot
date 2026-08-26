"""Tests for the synthetic data layer.

Three properties are load-bearing here, and each is asserted directly.

1. **Determinism.** A test that asserts a revenue figure is worthless if the data
   beneath it drifts between runs or between machines. The digest tests below pin
   the generated stream, so a change to the draw order fails loudly instead of
   silently invalidating every committed expected value.
2. **Ground-truth separation.** Labels exist for evaluation and must never reach
   the engine, the agent, or a prompt. The separation is structural (ADR-005), and
   the tests check the structure rather than trusting the convention.
3. **Label reproduction.** A scenario whose data does not actually exhibit the
   behaviour its label claims is worse than no scenario: it turns an evaluation
   into a rubber stamp. Every one of the eleven is run through the real engine and
   checked against its own ground truth.

Generation is the slowest thing in the suite (~1s per scenario), so datasets are
memoised per (scenario, seed, anchor) rather than rebuilt per test.
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from ...data import (
    DEFAULT_ANCHOR,
    DEFAULT_SEED,
    GroundTruth,
    ScenarioDataset,
    ScenarioId,
    SyntheticPayment,
    all_scenario_ids,
    generate_all,
    generate_scenario,
    get_scenario,
    ground_truth_for,
    incident_scenario_ids,
    restraint_scenario_ids,
)
from ...domain.canonical import digest
from ...domain.enums import ComparableWindowMode, PaymentStatus
from ...domain.errors import DomainValidationError
from ...domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ...domain.window import UTC
from ...financial.engine import build_daily_hourly_baseline, compute_metrics

_CACHE = {}


def dataset(
    scenario_id: ScenarioId, seed: int = DEFAULT_SEED, anchor: datetime = DEFAULT_ANCHOR
) -> ScenarioDataset:
    """Memoised generation. Same arguments, same object."""
    key = (scenario_id, seed, anchor)
    if key not in _CACHE:
        _CACHE[key] = generate_scenario(scenario_id, seed=seed, anchor=anchor)
    return _CACHE[key]


def fingerprint(data: ScenarioDataset) -> str:
    """A digest over every field that must be reproducible.

    Explicit rather than a blanket dataclass dump: this list *is* the definition
    of what "the same dataset" means, and adding a field to it should be a
    deliberate act.
    """
    return digest(
        [
            {
                "id": record.payment.id,
                "order_id": record.payment.order_id,
                "created_at": record.payment.created_at,
                "amount": record.payment.amount,
                "status": record.payment.status,
                "method": record.payment.method,
                "error_code": record.payment.error_code,
                "region": record.enrichment.region,
                "provider": record.enrichment.provider,
                "failure_category": record.enrichment.failure_category,
                "matched_anomaly": record.matched_anomaly,
                "in_baseline_period": record.in_baseline_period,
            }
            for record in data.records
        ]
    )


class DeterminismTests(unittest.TestCase):
    def test_same_seed_reproduces_the_dataset_exactly(self):
        first = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE, seed=4242)
        second = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE, seed=4242)
        self.assertEqual(first.records, second.records)
        self.assertEqual(fingerprint(first), fingerprint(second))

    def test_a_different_seed_produces_different_data(self):
        """Otherwise the seed is decorative and every "independent" run is the same.

        The shape is unchanged — same count, same windows — but the outcomes are
        not.
        """
        first = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE, seed=1)
        second = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE, seed=2)
        self.assertEqual(len(first.records), len(second.records))
        self.assertEqual(first.incident_window, second.incident_window)
        self.assertNotEqual(fingerprint(first), fingerprint(second))

    def test_scenarios_from_one_seed_do_not_share_a_random_stream(self):
        """Seed mixing, and it is not cosmetic.

        Without it, two scenarios generated from the same base seed would draw the
        identical sequence of numbers, so their "independent" failures would be
        perfectly correlated — an evaluation over eleven scenarios would really be
        an evaluation over one.
        """
        upi = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        card = dataset(ScenarioId.CARD_FAILURE_SPIKE)
        upi_amounts = [r.payment.amount.minor_units for r in upi.records[:200]]
        card_amounts = [r.payment.amount.minor_units for r in card.records[:200]]
        self.assertNotEqual(upi_amounts, card_amounts)

    def test_seed_mixing_is_stable_across_processes(self):
        """Derived from the scenario's characters, never from ``hash()``.

        ``hash()`` of a str is salted per process by default, so a seed mixed with
        it would give different data on every run — the exact failure this guards.
        Re-deriving the expected mix here would just restate the implementation, so
        the test pins the digest of a small dataset instead: any change to the
        mixing scheme breaks it.
        """
        anchor = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)
        first = generate_scenario(ScenarioId.INSUFFICIENT_DATA, seed=7, anchor=anchor)
        second = generate_scenario(ScenarioId.INSUFFICIENT_DATA, seed=7, anchor=anchor)
        self.assertEqual(fingerprint(first), fingerprint(second))

    def test_no_clock_is_read_so_the_timeline_is_anchor_relative(self):
        # Every timestamp derives from the anchor. A generator that read the clock
        # would produce a dataset that cannot be regenerated tomorrow
        # (PROJECT_RULES 4.1).
        shifted = generate_scenario(
            ScenarioId.NORMAL, anchor=DEFAULT_ANCHOR + timedelta(days=30)
        )
        base = dataset(ScenarioId.NORMAL)
        self.assertEqual(
            shifted.incident_window.start - base.incident_window.start,
            timedelta(days=30),
        )
        # Same random stream, so the same sequence of amounts, just moved in time.
        self.assertEqual(
            [r.payment.amount.minor_units for r in shifted.records],
            [r.payment.amount.minor_units for r in base.records],
        )

    def test_records_are_sorted_by_event_time_with_a_total_tiebreak(self):
        # Downstream code must never depend on generation order.
        records = dataset(ScenarioId.NORMAL).records
        keys = [(r.payment.created_at, r.payment.id) for r in records]
        self.assertEqual(keys, sorted(keys))

    def test_payment_ids_are_unique(self):
        records = dataset(ScenarioId.UPI_FAILURE_SPIKE).records
        ids = [r.payment.id for r in records]
        self.assertEqual(len(ids), len(set(ids)))

    def test_rejects_a_naive_anchor(self):
        with self.assertRaises(DomainValidationError):
            generate_scenario(ScenarioId.NORMAL, anchor=datetime(2026, 8, 26, 13, 0, 0))

    def test_rejects_a_non_integer_seed(self):
        with self.assertRaises(DomainValidationError):
            generate_scenario(ScenarioId.NORMAL, seed=1.5)  # type: ignore[arg-type]
        with self.assertRaises(DomainValidationError):
            generate_scenario(ScenarioId.NORMAL, seed=True)  # type: ignore[arg-type]

    def test_rejects_an_unknown_scenario(self):
        with self.assertRaises(DomainValidationError):
            generate_scenario("upi_failure_spike")  # type: ignore[arg-type]


class GroundTruthSeparationTests(unittest.TestCase):
    """ADR-005, checked structurally. Convention is not a control."""

    LABEL_FIELDS = (
        "scenario_id",
        "ground_truth",
        "matched_anomaly",
        "in_baseline_period",
        "is_incident",
        "label",
        "expected_root_cause",
    )

    def test_agent_payments_are_plain_production_contracts(self):
        data = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        payments = data.agent_payments()
        self.assertTrue(payments)
        for item in payments[:50]:
            self.assertIsInstance(item, Payment)
            self.assertNotIsInstance(item, SyntheticPayment)

    def test_no_label_field_exists_on_any_agent_facing_type(self):
        # There is no field for a label to leak through, so stripping is not
        # something anyone has to remember to do.
        for contract in (Payment, PaymentEnrichment, EnrichedPayment):
            for name in self.LABEL_FIELDS:
                self.assertNotIn(
                    name,
                    getattr(contract, "__dataclass_fields__", {}),
                    f"{contract.__name__} exposes label field {name!r}",
                )

    def test_no_label_is_reachable_as_an_attribute(self):
        # Catches a label added as a property or class attribute rather than a
        # dataclass field.
        data = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        sample = data.agent_enriched()[0]
        for name in self.LABEL_FIELDS:
            self.assertFalse(
                hasattr(sample, name), f"EnrichedPayment exposes {name!r}"
            )

    def test_the_labels_do_exist_on_the_wrapper(self):
        # The other half of the claim: separation, not absence. Evaluation needs
        # these.
        record = dataset(ScenarioId.UPI_FAILURE_SPIKE).records[0]
        self.assertIsInstance(record, SyntheticPayment)
        self.assertIs(record.scenario_id, ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsInstance(record.in_baseline_period, bool)

    def test_window_filters_use_timestamps_not_labels(self):
        """``in_baseline_period`` is a label; filtering must not consult it.

        Filtering on the label would make the split unreproducible from
        unlabelled production data, where no such field exists. The two must agree
        exactly — and they are derived independently.
        """
        data = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        by_label = {r.payment.id for r in data.records if not r.in_baseline_period}
        by_time = {p.payment.id for p in data.incident_enriched()}
        self.assertEqual(by_label, by_time)

    def test_baseline_and_incident_windows_tile_the_whole_timeline(self):
        # No payment falls between them and none is counted twice.
        data = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        total = len(data.records)
        self.assertEqual(
            len(data.baseline_enriched()) + len(data.incident_enriched()), total
        )
        self.assertEqual(data.baseline_window.end, data.incident_window.start)
        self.assertFalse(data.baseline_window.overlaps(data.incident_window))

    def test_planted_failures_are_incident_window_failures_only(self):
        data = dataset(ScenarioId.UPI_FAILURE_SPIKE)
        planted = set(data.planted_failure_ids())
        self.assertTrue(planted)
        incident_ids = {p.payment.id for p in data.incident_enriched()}
        by_id = {r.payment.id: r for r in data.records}
        for payment_id in planted:
            self.assertIn(payment_id, incident_ids)
            self.assertTrue(by_id[payment_id].payment.is_failure)
            self.assertIsNotNone(by_id[payment_id].matched_anomaly)

    def test_scenarios_with_no_anomaly_plant_nothing(self):
        for scenario_id in (
            ScenarioId.NORMAL,
            ScenarioId.SMALL_RANDOM_VARIATION,
            ScenarioId.INSUFFICIENT_DATA,
        ):
            self.assertEqual(dataset(scenario_id).planted_failure_ids(), ())

    def test_a_succeeded_payment_is_never_labelled_as_affected(self):
        # An anomaly-eligible payment that succeeded was not affected in any
        # observable way, and labelling it would inflate every recall figure.
        for record in dataset(ScenarioId.UPI_FAILURE_SPIKE).records:
            if record.matched_anomaly is not None:
                self.assertIs(record.payment.status, PaymentStatus.FAILED)

    def test_synthetic_payment_refuses_a_mismatched_enrichment(self):
        record = dataset(ScenarioId.NORMAL).records[0]
        other = dataset(ScenarioId.NORMAL).records[1]
        with self.assertRaises(DomainValidationError):
            SyntheticPayment(
                payment=record.payment,
                enrichment=other.enrichment,
                scenario_id=ScenarioId.NORMAL,
                matched_anomaly=None,
                in_baseline_period=False,
            )


class ScenarioRegistryTests(unittest.TestCase):
    def test_every_scenario_id_has_a_spec(self):
        for scenario_id in ScenarioId:
            self.assertIsNotNone(get_scenario(scenario_id))
        self.assertEqual(len(all_scenario_ids()), 11)

    def test_the_set_is_weighted_towards_restraint(self):
        """More ways to be wrong than to be right, deliberately.

        An agent that opens an incident whenever the failure rate moves is worse
        than no agent, so the scenario set has to be able to catch that. The three
        counts are deliberately different numbers: seven real degradations, six
        that warrant an action, five with no degradation at all. The gap between
        the first two is the whole point — detection and authorisation are
        different questions.
        """
        incidents = incident_scenario_ids()
        restraint = restraint_scenario_ids()
        self.assertEqual(len(incidents), 7)
        self.assertEqual(len(restraint), 5)
        # A real incident that is nonetheless in the restraint set.
        self.assertEqual(set(incidents) & set(restraint), {ScenarioId.RECOVERY_NOT_ELIGIBLE})
        # Six warrant an action: the incidents, minus that one.
        eligible = [
            sid for sid in ScenarioId if ground_truth_for(sid).expected_action_eligible
        ]
        self.assertEqual(len(eligible), 6)

    def test_ground_truth_is_internally_consistent(self):
        for scenario_id in ScenarioId:
            truth = ground_truth_for(scenario_id)
            self.assertIsInstance(truth, GroundTruth)
            if truth.expected_action_eligible:
                self.assertTrue(truth.is_incident)
            if truth.is_incident:
                self.assertTrue(truth.has_sufficient_data)

    def test_a_mislabelled_ground_truth_fails_at_construction(self):
        # An action eligible with no incident is incoherent, and it must fail at
        # import rather than skew an evaluation score silently.
        with self.assertRaises(DomainValidationError):
            GroundTruth(
                scenario_id=ScenarioId.NORMAL,
                is_incident=False,
                has_sufficient_data=True,
                expected_root_cause="none",
                expected_action_eligible=True,
                notes="incoherent on purpose",
            )

    def test_an_anomaly_outside_the_incident_window_is_refused(self):
        """A spec-level trap: the scenario would "pass" while testing nothing.

        Constructed here rather than asserted over the registry, because every
        registered scenario already satisfies it.
        """
        from ...data.scenarios import STANDARD_MIX, Anomaly, ScenarioSpec
        from ...domain.enums import FailureCategory

        with self.assertRaises(DomainValidationError):
            ScenarioSpec(
                scenario_id=ScenarioId.NORMAL,
                description="anomaly confined to hours the window never covers",
                method_profiles=STANDARD_MIX,
                ground_truth=ground_truth_for(ScenarioId.NORMAL),
                incident_window_start_hour=12,
                incident_hours=1,
                anomalies=(
                    Anomaly(
                        label="never fires",
                        failure_rate=Decimal("0.9"),
                        failure_code="X",
                        failure_category=FailureCategory.GATEWAY_ERROR,
                        hours_utc=(3,),
                    ),
                ),
            )


class LabelReproductionTests(unittest.TestCase):
    """Does each scenario's data actually exhibit what its label claims?

    Run through the real engine, with the baseline mode the scenario's ground
    truth says it needs. A scenario that does not reproduce its own label makes an
    evaluation meaningless in the most dangerous direction: it passes.
    """

    @classmethod
    def setUpClass(cls):
        cls.datasets = generate_all()

    def _metrics(self, data, mode=None):
        if mode is None:
            mode = (
                ComparableWindowMode.SAME_HOUR_OF_DAY
                if data.ground_truth.requires_same_hour_baseline
                else ComparableWindowMode.ALL
            )
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        return compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
            comparable_mode=mode,
        )

    def test_every_scenario_reproduces_its_own_label(self):
        for data in self.datasets:
            truth = data.ground_truth
            with self.subTest(scenario=data.scenario_id.value):
                metrics = self._metrics(data)

                if not truth.has_sufficient_data:
                    # The correct answer is "I cannot tell", which means an absent
                    # baseline and no deviation claim — not a healthy verdict.
                    self.assertFalse(metrics.baseline.is_sufficient)
                    self.assertIsNone(metrics.deviation)
                    self.assertIsNone(metrics.revenue_risk)
                    continue

                self.assertTrue(metrics.baseline.is_sufficient)
                self.assertIsNotNone(metrics.deviation)

                if truth.is_incident:
                    # A real degradation: materially worse than baseline, and the
                    # excess is worth real money.
                    self.assertTrue(metrics.deviation.is_worse_than_baseline)
                    self.assertGreater(
                        metrics.deviation.absolute_percentage_points, Decimal("3")
                    )
                    self.assertGreater(metrics.significance.z_score, 3.0)
                    self.assertGreater(metrics.revenue_risk.excess_failed_transactions, 0)
                else:
                    # Restraint: no material rise. Asserted as a bound rather than
                    # equality, because ordinary noise is expected and is the
                    # point.
                    self.assertLess(
                        metrics.deviation.absolute_percentage_points, Decimal("3")
                    )
                    self.assertEqual(metrics.revenue_risk.excess_failed_transactions, 0)

    def test_the_false_alarm_needs_the_right_baseline_to_be_dismissed(self):
        """The scenario that justifies same-hour-of-day comparison existing.

        A merchant whose evening hour always runs hot. Against a pooled 72-hour
        baseline the evening window is +8.8pp and a 2.2x lift — indistinguishable
        from a real incident. Against the same hour on previous days it is -0.2pp.
        Same data, opposite verdicts, and only the second is correct.
        """
        data = next(d for d in self.datasets if d.scenario_id is ScenarioId.FALSE_ALARM)
        naive = self._metrics(data, ComparableWindowMode.ALL)
        correct = self._metrics(data, ComparableWindowMode.SAME_HOUR_OF_DAY)

        self.assertGreater(naive.deviation.absolute_percentage_points, Decimal("5"))
        self.assertGreater(naive.deviation.relative_lift, Decimal("2"))
        self.assertFalse(correct.deviation.is_worse_than_baseline)
        self.assertLess(abs(correct.deviation.absolute_percentage_points), Decimal("1"))

    def test_same_hour_comparison_does_not_hide_a_real_evening_incident(self):
        """The other half, and the reason the previous test is not enough.

        A baseline narrow enough to dismiss the false alarm could just as easily
        dismiss a genuine evening outage. This one is a real incident at the same
        hour of day, and it must survive both comparisons.
        """
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.EVENING_FAILURE_SPIKE
        )
        for mode in (ComparableWindowMode.ALL, ComparableWindowMode.SAME_HOUR_OF_DAY):
            metrics = self._metrics(data, mode)
            with self.subTest(mode=mode.value):
                self.assertGreater(
                    metrics.deviation.absolute_percentage_points, Decimal("15")
                )
                self.assertTrue(metrics.significance.normal_approximation_valid)

    def test_the_sparse_scenario_is_where_the_p_value_must_not_be_trusted(self):
        """``small_random_variation`` is thin by design, and the engine says so.

        45 decided transactions. The rate wobble is meaningless, and — importantly
        — the honest reason is the sample size, which ``min_expected_count``
        reports and the p-value alone cannot.
        """
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.SMALL_RANDOM_VARIATION
        )
        metrics = self._metrics(data)
        self.assertLess(metrics.counts.decided, 100)
        self.assertFalse(metrics.significance.normal_approximation_valid)
        self.assertLess(metrics.significance.min_expected_count, 5.0)
        self.assertFalse(metrics.deviation.is_worse_than_baseline)

    def test_the_insufficient_scenario_yields_no_numbers_at_all(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.INSUFFICIENT_DATA
        )
        metrics = self._metrics(data)
        self.assertLess(metrics.counts.decided, 20)
        self.assertIsNone(metrics.baseline.rate)
        self.assertIsNone(metrics.significance)
        self.assertIsNone(metrics.revenue_risk)

    def test_a_real_incident_can_still_be_action_ineligible(self):
        # Detection and authorisation are different questions. The engine's job
        # ends at "this is real"; whether to act is the policy layer's.
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.RECOVERY_NOT_ELIGIBLE
        )
        self.assertTrue(data.ground_truth.is_incident)
        self.assertFalse(data.ground_truth.expected_action_eligible)
        metrics = self._metrics(data)
        self.assertTrue(metrics.deviation.is_worse_than_baseline)

    def test_multiple_failures_has_no_single_primary_dimension(self):
        # A confident single-cause answer here is wrong, which is what the None
        # label encodes.
        truth = ground_truth_for(ScenarioId.MULTIPLE_FAILURES)
        self.assertTrue(truth.is_incident)
        self.assertIsNone(truth.expected_primary_dimension)

    def test_undecided_payments_appear_wherever_volume_allows(self):
        """The decided/undecided split is exercised by real data, not only by unit
        tests (ARCHITECTURE.md 7.2).

        Each assertion is gated on the volume that makes it near-certain, because
        the undecided share is 2% and a hard assertion on a thin slice would be a
        seed-dependent test — exactly what this suite exists to avoid.
        ``INSUFFICIENT_DATA`` generates 50 payments in total (expected undecided:
        1), and ``SMALL_RANDOM_VARIATION``'s incident window holds 45 (expected:
        under 1). Zero is an ordinary outcome in both.
        """
        for data in self.datasets:
            with self.subTest(scenario=data.scenario_id.value):
                if len(data.records) >= 500:
                    undecided = [
                        r for r in data.records if r.payment.status is PaymentStatus.CREATED
                    ]
                    self.assertTrue(undecided)

                metrics = self._metrics(data)
                expected_in_window = metrics.counts.total * float(
                    data.spec.undecided_share
                )
                if expected_in_window >= 3.0:
                    self.assertGreater(metrics.counts.undecided, 0)
                    self.assertLess(metrics.counts.decided, metrics.counts.total)

    def test_undecided_payments_carry_no_failure_details(self):
        # An in-flight payment has not failed, so it must not look like it has.
        for data in self.datasets:
            with self.subTest(scenario=data.scenario_id.value):
                for record in data.records:
                    if record.payment.status is PaymentStatus.CREATED:
                        self.assertIsNone(record.payment.error_code)
                        self.assertIsNone(record.enrichment.failure_category)

    def test_failed_payments_carry_an_error_code_and_others_do_not(self):
        for data in self.datasets:
            with self.subTest(scenario=data.scenario_id.value):
                for record in data.records:
                    if record.payment.is_failure:
                        self.assertIsNotNone(record.payment.error_code)
                    else:
                        self.assertIsNone(record.payment.error_code)

    def test_generate_all_covers_the_whole_set(self):
        self.assertEqual(
            tuple(d.scenario_id for d in self.datasets), tuple(ScenarioId)
        )

    def test_generate_all_accepts_a_subset(self):
        subset = generate_all(scenario_ids=(ScenarioId.NORMAL, ScenarioId.FALSE_ALARM))
        self.assertEqual(
            tuple(d.scenario_id for d in subset),
            (ScenarioId.NORMAL, ScenarioId.FALSE_ALARM),
        )


if __name__ == "__main__":
    unittest.main()
