"""Tests validating the detector against all 11 synthetic scenarios.

Evaluates the detector against the full deterministic dataset and checks that
detection decisions match ground truth exactly:

1. NORMAL                   -> NO incident
2. UPI_FAILURE_SPIKE        -> INCIDENT
3. CARD_FAILURE_SPIKE       -> INCIDENT
4. EVENING_FAILURE_SPIKE    -> INCIDENT (evaluated with comparable baseline)
5. REGIONAL_FAILURE         -> INCIDENT
6. PROVIDER_FAILURE         -> INCIDENT
7. MULTIPLE_FAILURES        -> INCIDENT (primary_dimension is None, no premature diagnosis)
8. FALSE_ALARM              -> NO incident (with same-hour comparable baseline)
9. SMALL_RANDOM_VARIATION   -> NO incident (inadmissible significance / sample noise)
10. INSUFFICIENT_DATA       -> NO incident (undefined baseline / insufficient data)
11. RECOVERY_NOT_ELIGIBLE   -> INCIDENT (detection occurs, action eligibility is separate)
"""

import unittest
from decimal import Decimal

from ...data import (
    ScenarioId,
    generate_all,
    ground_truth_for,
    incident_scenario_ids,
    restraint_scenario_ids,
)
from ...detection.config import DetectionConfig
from ...detection.detector import Detector, detect_incident
from ...domain.enums import ComparableWindowMode, IncidentStatus, IncidentType
from ...financial.engine import build_daily_hourly_baseline, compute_metrics


class ScenarioDetectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = generate_all()
        cls.detector = Detector()

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

    def test_all_11_scenarios_match_ground_truth(self):
        """Every scenario decision matches ground truth is_incident."""
        for data in self.datasets:
            truth = data.ground_truth
            with self.subTest(scenario=data.scenario_id.value):
                metrics = self._metrics(data)
                incident = self.detector.detect(metrics, merchant_id="test_merchant")

                if truth.is_incident:
                    self.assertIsNotNone(
                        incident,
                        f"Expected incident for {data.scenario_id.value}, but got None",
                    )
                    self.assertEqual(incident.status, IncidentStatus.DETECTED)
                    self.assertEqual(
                        incident.incident_type, IncidentType.PAYMENT_FAILURE_SPIKE
                    )
                    self.assertEqual(len(incident.evidence), 1)
                    self.assertEqual(incident.evidence[0].metrics, metrics)
                else:
                    self.assertIsNone(
                        incident,
                        f"Expected NO incident for {data.scenario_id.value}, but got {incident}",
                    )

    def test_normal_scenario_produces_no_incident(self):
        data = next(d for d in self.datasets if d.scenario_id is ScenarioId.NORMAL)
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNone(incident)

    def test_upi_failure_spike_triggers_incident(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.UPI_FAILURE_SPIKE
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)
        self.assertGreater(incident.metrics.deviation.absolute_percentage_points, Decimal("10.0"))

    def test_card_failure_spike_triggers_incident(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.CARD_FAILURE_SPIKE
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)
        self.assertGreater(incident.metrics.deviation.absolute_percentage_points, Decimal("5.0"))

    def test_evening_failure_spike_triggers_incident(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.EVENING_FAILURE_SPIKE
        )
        # Even with same-hour baseline, the true evening spike triggers
        metrics = self._metrics(data, ComparableWindowMode.SAME_HOUR_OF_DAY)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)
        self.assertGreater(incident.metrics.deviation.absolute_percentage_points, Decimal("15.0"))

    def test_regional_failure_triggers_incident(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.REGIONAL_FAILURE
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)

    def test_provider_failure_triggers_incident(self):
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.PROVIDER_FAILURE
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)

    def test_multiple_failures_triggers_without_premature_diagnosis(self):
        """MULTIPLE_FAILURES opens an incident, leaving primary_dimension None."""
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.MULTIPLE_FAILURES
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)
        # Detection must not prematurely assign a single dimension
        self.assertIsNone(incident.primary_dimension)
        self.assertIsNone(incident.primary_dimension_value)

    def test_false_alarm_produces_no_incident_with_same_hour_baseline(self):
        """FALSE_ALARM with proper same-hour baseline produces no incident."""
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.FALSE_ALARM
        )
        metrics = self._metrics(data, ComparableWindowMode.SAME_HOUR_OF_DAY)
        incident = self.detector.detect(metrics)
        self.assertIsNone(incident)

    def test_small_random_variation_produces_no_incident(self):
        """SMALL_RANDOM_VARIATION produces no incident due to statistical validity gating."""
        data = next(
            d for d in self.datasets
            if d.scenario_id is ScenarioId.SMALL_RANDOM_VARIATION
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNone(incident)

    def test_insufficient_data_produces_no_incident(self):
        """INSUFFICIENT_DATA produces no incident when baseline cannot be established."""
        data = next(
            d for d in self.datasets if d.scenario_id is ScenarioId.INSUFFICIENT_DATA
        )
        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNone(incident)

    def test_recovery_not_eligible_triggers_detection(self):
        """RECOVERY_NOT_ELIGIBLE triggers detection while eligibility is separate."""
        data = next(
            d for d in self.datasets
            if d.scenario_id is ScenarioId.RECOVERY_NOT_ELIGIBLE
        )
        self.assertTrue(data.ground_truth.is_incident)
        self.assertFalse(data.ground_truth.expected_action_eligible)

        metrics = self._metrics(data)
        incident = self.detector.detect(metrics)
        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, IncidentStatus.DETECTED)


if __name__ == "__main__":
    unittest.main()
