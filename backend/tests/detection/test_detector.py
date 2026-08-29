"""Unit tests for the detection layer.

Tests:
1. DetectionConfig validation, float rejection, and severity ordering.
2. Metric evaluation against all individual threshold gates.
3. Statistical significance gating (especially normal_approximation_valid).
4. Missing/undefined baseline and deviation handling (ADR-004).
5. Incident opening, evidence construction, severity classification.
6. Deduplication identity (incident_key stability).
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from ...detection.config import DEFAULT_RULE_VERSION, DetectionConfig
from ...detection.detector import Detector, detect_from_payments, detect_incident
from ...detection.evaluator import (
    DetectionEvaluation,
    DetectionReason,
    determine_severity,
    evaluate_metrics,
)
from ...domain.enums import (
    BaselineMethod,
    ComparableWindowMode,
    Currency,
    IncidentStatus,
    IncidentType,
    PaymentMethod,
    PaymentStatus,
    Severity,
    SourceConfidence,
)
from ...domain.errors import DomainValidationError, MoneyPrecisionError
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.metrics import (
    BaselineFailureRate,
    Deviation,
    FinancialMetrics,
    Rate,
    RevenueRisk,
    SignificanceResult,
    TransactionCounts,
)
from ...domain.money import Money
from ...domain.window import UTC, TimeWindow
from ..helpers import HOUR, NOW, T0, payment, population


class DetectionConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self):
        cfg = DetectionConfig()
        self.assertEqual(cfg.min_absolute_deviation_pp, Decimal("3.0"))
        self.assertEqual(cfg.min_relative_lift, Decimal("1.5"))
        self.assertEqual(cfg.min_decided_count, 30)
        self.assertEqual(cfg.rule_version, DEFAULT_RULE_VERSION)
        self.assertTrue(cfg.require_normal_approximation_valid)

    def test_float_rejected_for_deviation_pp(self):
        with self.assertRaises(MoneyPrecisionError):
            DetectionConfig(min_absolute_deviation_pp=3.0)  # type: ignore[arg-type]

    def test_float_rejected_for_relative_lift(self):
        with self.assertRaises(MoneyPrecisionError):
            DetectionConfig(min_relative_lift=1.5)  # type: ignore[arg-type]

    def test_float_rejected_for_severity_thresholds(self):
        with self.assertRaises(MoneyPrecisionError):
            DetectionConfig(critical_deviation_pp=15.0)  # type: ignore[arg-type]

    def test_negative_or_zero_deviation_pp_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_absolute_deviation_pp=Decimal("0.0"))
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_absolute_deviation_pp=Decimal("-1.0"))

    def test_relative_lift_below_one_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_relative_lift=Decimal("0.9"))
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_relative_lift=Decimal("1.0"))

    def test_non_positive_decided_count_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_decided_count=0)
        with self.assertRaises(DomainValidationError):
            DetectionConfig(min_decided_count=-5)

    def test_invalid_max_p_value_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(max_p_value=0.0)
        with self.assertRaises(DomainValidationError):
            DetectionConfig(max_p_value=1.5)
        with self.assertRaises(DomainValidationError):
            DetectionConfig(max_p_value=Decimal("0.05"))  # type: ignore[arg-type]

    def test_inconsistent_severity_deviation_order_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(
                critical_deviation_pp=Decimal("5.0"),
                high_deviation_pp=Decimal("10.0"),  # high > critical
                medium_deviation_pp=Decimal("3.0"),
            )

    def test_inconsistent_severity_lift_order_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(
                critical_relative_lift=Decimal("2.0"),
                high_relative_lift=Decimal("3.0"),  # high > critical
                medium_relative_lift=Decimal("1.5"),
            )

    def test_blank_rule_version_rejected(self):
        with self.assertRaises(DomainValidationError):
            DetectionConfig(rule_version="")


class MetricEvaluationTests(unittest.TestCase):
    def _metrics(
        self,
        succeeded: int = 80,
        failed: int = 20,
        baseline_rate: Rate = Rate(5, 100),
        baseline_sufficient: bool = True,
        normal_valid: bool = True,
        p_value: float = 0.0001,
        z_score: float = 5.0,
        excess_failed: int = 15,
        window: TimeWindow = HOUR,
    ) -> FinancialMetrics:
        counts = TransactionCounts(succeeded=succeeded, failed=failed)
        fail_r = Rate(failed, succeeded + failed)
        succ_r = Rate(succeeded, succeeded + failed)
        base = BaselineFailureRate(
            method=BaselineMethod.POOLED,
            rate=baseline_rate if baseline_sufficient else None,
            windows_considered=3,
            windows_used=3 if baseline_sufficient else 0,
            decided_sample=300 if baseline_sufficient else 10,
            min_decided_required=100,
        )
        dev = (
            Deviation(
                current=fail_r,
                baseline=baseline_rate,
                absolute_percentage_points=(fail_r.value - baseline_rate.value) * 100,
                relative_lift=(fail_r.value / baseline_rate.value)
                if baseline_rate.value > 0
                else None,
            )
            if baseline_sufficient
            else None
        )
        sig = (
            SignificanceResult(
                z_score=z_score,
                p_value=p_value,
                current_decided=counts.decided,
                baseline_decided=300,
                min_expected_count=10.0 if normal_valid else 1.2,
            )
            if baseline_sufficient
            else None
        )
        risk = (
            RevenueRisk(
                failed_gmv=Money(failed * 100_00),
                excess_failed_transactions=excess_failed,
                mean_failed_ticket=Money(100_00),
                revenue_at_risk=Money(excess_failed * 100_00),
            )
            if baseline_sufficient
            else None
        )
        return FinancialMetrics(
            window=window,
            counts=counts,
            failure_rate=fail_r,
            success_rate=succ_r,
            baseline=base,
            deviation=dev,
            significance=sig,
            revenue_risk=risk,
            computed_at=NOW,
            computation_version="financial-engine-1",
        )

    def test_happy_path_triggers_incident(self):
        metrics = self._metrics(succeeded=80, failed=20)  # 20% vs 5% baseline
        eval_res = evaluate_metrics(metrics)
        self.assertTrue(eval_res.triggered)
        self.assertEqual(eval_res.reasons, (DetectionReason.TRIGGERED,))
        self.assertIsNotNone(eval_res.severity)

    def test_insufficient_decided_count_blocks_trigger(self):
        metrics = self._metrics(succeeded=8, failed=2)  # only 10 decided (< 30)
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSUFFICIENT_DECIDED_COUNT, eval_res.reasons)

    def test_insufficient_baseline_blocks_trigger(self):
        metrics = self._metrics(baseline_sufficient=False)
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSUFFICIENT_BASELINE, eval_res.reasons)

    def test_improvement_not_worse_than_baseline_blocks_trigger(self):
        # 3% failure vs 5% baseline (improvement)
        metrics = self._metrics(
            succeeded=97, failed=3, baseline_rate=Rate(5, 100), excess_failed=0
        )
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.NOT_WORSE_THAN_BASELINE, eval_res.reasons)

    def test_small_deviation_below_threshold_blocks_trigger(self):
        # 6% failure vs 5% baseline (+1pp, < 3pp threshold)
        metrics = self._metrics(
            succeeded=94, failed=6, baseline_rate=Rate(5, 100), excess_failed=1
        )
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSUFFICIENT_ABSOLUTE_DEVIATION, eval_res.reasons)

    def test_insufficient_relative_lift_blocks_trigger(self):
        # Config requiring 3.0x lift, but observed is 2.0x (10% vs 5%)
        cfg = DetectionConfig(
            min_absolute_deviation_pp=Decimal("3.0"),
            min_relative_lift=Decimal("3.0"),
        )
        metrics = self._metrics(succeeded=90, failed=10, baseline_rate=Rate(5, 100))
        eval_res = evaluate_metrics(metrics, config=cfg)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSUFFICIENT_RELATIVE_LIFT, eval_res.reasons)

    def test_zero_excess_failures_blocks_trigger(self):
        metrics = self._metrics(succeeded=80, failed=20, excess_failed=0)
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSUFFICIENT_EXCESS_FAILURES, eval_res.reasons)

    def test_invalid_normal_approximation_blocks_trigger(self):
        """CRITICAL: Gating statistical evidence using normal_approximation_valid.

        A low p-value (e.g. 0.0001) on thin data where normal_approximation_valid is False
        must NEVER trigger an incident.
        """
        metrics = self._metrics(
            succeeded=80, failed=20, normal_valid=False, p_value=0.0001
        )
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INVALID_NORMAL_APPROXIMATION, eval_res.reasons)

    def test_high_p_value_blocks_trigger(self):
        metrics = self._metrics(succeeded=80, failed=20, p_value=0.25)
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSIGNIFICANT_P_VALUE, eval_res.reasons)

    def test_low_z_score_blocks_trigger(self):
        metrics = self._metrics(succeeded=80, failed=20, z_score=1.2)
        eval_res = evaluate_metrics(metrics)
        self.assertFalse(eval_res.triggered)
        self.assertIn(DetectionReason.INSIGNIFICANT_Z_SCORE, eval_res.reasons)


class SeverityDeterminationTests(unittest.TestCase):
    def _metrics_with_dev(self, pp: Decimal, lift: Decimal) -> FinancialMetrics:
        fail_r = Rate(20, 100)
        base_r = Rate(5, 100)
        return FinancialMetrics(
            window=HOUR,
            counts=TransactionCounts(80, 20),
            failure_rate=fail_r,
            success_rate=Rate(80, 100),
            baseline=BaselineFailureRate(
                method=BaselineMethod.POOLED,
                rate=base_r,
                windows_considered=3,
                windows_used=3,
                decided_sample=300,
                min_decided_required=100,
            ),
            deviation=Deviation(
                current=fail_r,
                baseline=base_r,
                absolute_percentage_points=pp,
                relative_lift=lift,
            ),
            significance=None,
            revenue_risk=None,
            computed_at=NOW,
            computation_version="financial-engine-1",
        )

    def test_critical_severity(self):
        cfg = DetectionConfig()
        # pp >= 15.0
        m1 = self._metrics_with_dev(Decimal("16.0"), Decimal("2.0"))
        self.assertEqual(determine_severity(m1, cfg), Severity.CRITICAL)
        # lift >= 4.0
        m2 = self._metrics_with_dev(Decimal("5.0"), Decimal("4.5"))
        self.assertEqual(determine_severity(m2, cfg), Severity.CRITICAL)

    def test_high_severity(self):
        cfg = DetectionConfig()
        # pp >= 8.0, < 15.0
        m = self._metrics_with_dev(Decimal("9.0"), Decimal("2.0"))
        self.assertEqual(determine_severity(m, cfg), Severity.HIGH)

    def test_medium_severity(self):
        cfg = DetectionConfig()
        # pp >= 4.0, < 8.0
        m = self._metrics_with_dev(Decimal("5.0"), Decimal("1.9"))
        self.assertEqual(determine_severity(m, cfg), Severity.MEDIUM)

    def test_low_severity(self):
        cfg = DetectionConfig()
        # pp >= 3.0, < 4.0
        m = self._metrics_with_dev(Decimal("3.2"), Decimal("1.6"))
        self.assertEqual(determine_severity(m, cfg), Severity.LOW)


class DetectorTests(unittest.TestCase):
    def _metrics(self, pp: Decimal = Decimal("10.0"), lift: Decimal = Decimal("3.0")):
        fail_r = Rate(15, 100)
        base_r = Rate(5, 100)
        return FinancialMetrics(
            window=HOUR,
            counts=TransactionCounts(85, 15),
            failure_rate=fail_r,
            success_rate=Rate(85, 100),
            baseline=BaselineFailureRate(
                method=BaselineMethod.POOLED,
                rate=base_r,
                windows_considered=3,
                windows_used=3,
                decided_sample=300,
                min_decided_required=100,
            ),
            deviation=Deviation(
                current=fail_r,
                baseline=base_r,
                absolute_percentage_points=pp,
                relative_lift=lift,
            ),
            significance=SignificanceResult(
                z_score=6.0,
                p_value=0.00001,
                current_decided=100,
                baseline_decided=300,
                min_expected_count=10.0,
            ),
            revenue_risk=RevenueRisk(
                failed_gmv=Money(15 * 100_00),
                excess_failed_transactions=10,
                mean_failed_ticket=Money(100_00),
                revenue_at_risk=Money(1_000_00),
            ),
            computed_at=NOW,
            computation_version="financial-engine-1",
        )

    def test_detect_creates_valid_financial_incident(self):
        metrics = self._metrics()
        incident = detect_incident(metrics, merchant_id="merchant_123")
        self.assertIsNotNone(incident)
        self.assertEqual(incident.merchant_id, "merchant_123")
        self.assertEqual(incident.incident_type, IncidentType.PAYMENT_FAILURE_SPIKE)
        self.assertEqual(incident.status, IncidentStatus.DETECTED)
        self.assertEqual(incident.window, HOUR)
        self.assertEqual(incident.metrics, metrics)
        self.assertIsNone(incident.primary_dimension)
        self.assertIsNone(incident.primary_dimension_value)
        self.assertEqual(len(incident.evidence), 1)

        # Evidence validity
        ev = incident.evidence[0]
        self.assertEqual(ev.incident_id, incident.incident_id)
        self.assertEqual(ev.window, HOUR)
        self.assertEqual(ev.source_confidence, SourceConfidence.OBSERVED)
        self.assertEqual(ev.metrics, metrics)
        self.assertGreaterEqual(len(ev.summary), 10)

    def test_detect_returns_none_when_criteria_not_met(self):
        # 1pp deviation -> below threshold
        metrics = self._metrics(pp=Decimal("1.0"), lift=Decimal("1.2"))
        incident = detect_incident(metrics)
        self.assertIsNone(incident)

    def test_incident_key_is_deterministic_and_deduplicates(self):
        metrics = self._metrics()
        inc1 = detect_incident(metrics, merchant_id="merchant_1")
        inc2 = detect_incident(metrics, merchant_id="merchant_1")
        self.assertEqual(inc1.incident_key, inc2.incident_key)

    def test_incident_key_differs_by_merchant_and_window(self):
        metrics1 = self._metrics()
        inc1 = detect_incident(metrics1, merchant_id="merchant_1")
        inc2 = detect_incident(metrics1, merchant_id="merchant_2")
        self.assertNotEqual(inc1.incident_key, inc2.incident_key)

        later_window = TimeWindow(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
        # build metrics for later window
        metrics2 = FinancialMetrics(
            window=later_window,
            counts=metrics1.counts,
            failure_rate=metrics1.failure_rate,
            success_rate=metrics1.success_rate,
            baseline=metrics1.baseline,
            deviation=metrics1.deviation,
            significance=metrics1.significance,
            revenue_risk=metrics1.revenue_risk,
            computed_at=NOW + timedelta(hours=1),
            computation_version="financial-engine-1",
        )
        inc3 = detect_incident(metrics2, merchant_id="merchant_1")
        self.assertNotEqual(inc1.incident_key, inc3.incident_key)

    def test_detect_from_payments_helper(self):
        from ...financial.engine import build_hourly_baseline

        # Generate 6 hours of calm baseline (5% failure) and 1 hour spike (25% failure)
        history = []
        for h in range(1, 7):
            w = TimeWindow(HOUR.start - timedelta(hours=h), HOUR.start - timedelta(hours=h - 1))
            history.extend(population(succeeded=95, failed=5, window=w, prefix=f"h{h}"))
        buckets = build_hourly_baseline(history, HOUR, 6)

        current = population(succeeded=75, failed=25, window=HOUR, prefix="curr")
        all_payments = history + current

        incident = detect_from_payments(
            payments=all_payments,
            window=HOUR,
            now=NOW,
            baseline_windows=buckets,
            merchant_id="m_test",
        )
        self.assertIsNotNone(incident)
        self.assertEqual(incident.status, IncidentStatus.DETECTED)
        self.assertEqual(incident.severity, Severity.CRITICAL)


if __name__ == "__main__":
    unittest.main()
