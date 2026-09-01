"""Tests for the deterministic investigation layer.

Validates that investigation correctly:
1. Localises single-dimension failures (UPI, Card, Regional, Provider, Time).
2. Does NOT force a single cause when multiple failures co-occur (MULTIPLE_FAILURES).
3. Safely handles restraint scenarios (NORMAL, FALSE_ALARM, SMALL_RANDOM_VARIATION, INSUFFICIENT_DATA).
4. Produces verifiable, traceable evidence attached to the report.
5. Remains strictly deterministic.
6. Integrates with the audit trail.
"""

import unittest
from datetime import timedelta
from decimal import Decimal

from ...audit.store import AuditLog
from ...data import ScenarioId, generate_all
from ...detection.detector import Detector
from ...domain.enums import (
    AuditEventType,
    ComparableWindowMode,
    Dimension,
    PaymentMethod,
    PaymentStatus,
    SourceConfidence,
)
from ...domain.errors import DomainValidationError
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.money import Money
from ...domain.window import TimeWindow
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.analyzer import INVESTIGATED_DIMENSIONS, analyze_incident
from ...investigation.enums import EvidenceStrength
from ...investigation.findings import DimensionalFinding, InvestigationReport
from ...investigation.investigator import Investigator, investigate_incident
from ..helpers import HOUR, NOW, T0, payment, population


class InvestigationUnitTests(unittest.TestCase):
    def test_investigated_dimensions_are_supported(self):
        self.assertEqual(
            INVESTIGATED_DIMENSIONS,
            (
                Dimension.PAYMENT_METHOD,
                Dimension.REGION,
                Dimension.PROVIDER,
                Dimension.FAILURE_CODE,
                Dimension.FAILURE_CATEGORY,
                Dimension.HOUR_OF_DAY,
            ),
        )

    def test_insufficient_payments_returns_insufficient_evidence(self):
        # Empty or tiny window
        report = analyze_incident(
            incident_id="inc_empty",
            window=HOUR,
            current_payments=[],
            investigated_at=NOW,
        )
        self.assertFalse(report.has_sufficient_evidence)
        self.assertEqual(len(report.primary_findings), 0)
        self.assertIn("Insufficient data", report.summary)

    def test_audit_integration_records_lifecycle_events(self):
        audit_log = AuditLog()
        detector = Detector()

        # Generate simple incident
        history = []
        for h in range(1, 7):
            w = TimeWindow(HOUR.start - timedelta(hours=h), HOUR.start - timedelta(hours=h - 1))
            history.extend(population(succeeded=95, failed=5, window=w, prefix=f"h{h}"))
        buckets = build_daily_hourly_baseline(history, HOUR, 6)

        current = population(succeeded=70, failed=30, method=PaymentMethod.UPI, window=HOUR, prefix="curr")
        incident = detector.detect_from_payments(
            payments=history + current,
            window=HOUR,
            now=NOW,
            baseline_windows=buckets,
            merchant_id="m_test",
        )
        self.assertIsNotNone(incident)

        investigator = Investigator()
        report = investigator.investigate(
            incident=incident,
            payments=current,
            baseline_payments=history,
            now=NOW,
            audit_log=audit_log,
        )

        self.assertTrue(report.has_sufficient_evidence)
        self.assertEqual(audit_log.count(), 2)

        events = audit_log.get_events(incident_id=incident.incident_id)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_type, AuditEventType.INVESTIGATION_STARTED)
        self.assertEqual(events[1].event_type, AuditEventType.INVESTIGATION_COMPLETED)


class ScenarioInvestigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = generate_all()
        cls.detector = Detector()
        cls.investigator = Investigator()

    def _get_incident_and_data(self, scenario_id: ScenarioId):
        data = next(d for d in self.datasets if d.scenario_id is scenario_id)
        mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if data.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
            comparable_mode=mode,
        )
        incident = self.detector.detect(metrics, merchant_id="test_merchant")
        return data, incident, metrics

    def test_upi_failure_spike_investigation(self):
        """UPI_FAILURE_SPIKE: Investigation localises concentration to UPI."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        self.assertTrue(len(report.primary_findings) > 0)

        # Primary finding must include UPI
        upi_findings = [
            f for f in report.primary_findings
            if f.dimension is Dimension.PAYMENT_METHOD and f.value == "upi"
        ]
        self.assertEqual(len(upi_findings), 1)
        upi_f = upi_findings[0]
        self.assertEqual(upi_f.strength, EvidenceStrength.STRONG_EVIDENCE)
        self.assertGreater(upi_f.deviation_pp, Decimal("10.0"))
        self.assertGreater(upi_f.relative_lift, Decimal("2.5"))
        self.assertEqual(upi_f.source_confidence, SourceConfidence.OBSERVED)

    def test_card_failure_spike_investigation(self):
        """CARD_FAILURE_SPIKE: Investigation localises concentration to Card."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.CARD_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        card_findings = [
            f for f in report.primary_findings
            if f.dimension is Dimension.PAYMENT_METHOD and f.value == "card"
        ]
        self.assertEqual(len(card_findings), 1)
        card_f = card_findings[0]
        self.assertEqual(card_f.strength, EvidenceStrength.STRONG_EVIDENCE)
        self.assertGreater(card_f.deviation_pp, Decimal("20.0"))
        self.assertGreater(card_f.relative_lift, Decimal("4.0"))

    def test_regional_failure_investigation(self):
        """REGIONAL_FAILURE: Investigation identifies IN-KA and tags as ENRICHED."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.REGIONAL_FAILURE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        ka_findings = [
            f for f in report.primary_findings
            if f.dimension is Dimension.REGION and f.value == "IN-KA"
        ]
        self.assertEqual(len(ka_findings), 1)
        ka_f = ka_findings[0]
        self.assertEqual(ka_f.strength, EvidenceStrength.STRONG_EVIDENCE)
        self.assertEqual(ka_f.source_confidence, SourceConfidence.ENRICHED)

    def test_provider_failure_investigation(self):
        """PROVIDER_FAILURE: Investigation identifies acquirer_b as the contributor."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.PROVIDER_FAILURE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        acq_findings = [
            f for f in report.primary_findings
            if f.dimension is Dimension.PROVIDER and f.value == "acquirer_b"
        ]
        self.assertEqual(len(acq_findings), 1)
        acq_f = acq_findings[0]
        self.assertEqual(acq_f.strength, EvidenceStrength.STRONG_EVIDENCE)
        self.assertEqual(acq_f.source_confidence, SourceConfidence.ENRICHED)

    def test_evening_failure_spike_investigation(self):
        """EVENING_FAILURE_SPIKE: Investigation identifies the evening hour concentration."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.EVENING_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        self.assertTrue(len(report.primary_findings) > 0)

    def test_multiple_failures_investigation_finds_concurrent_causes(self):
        """MULTIPLE_FAILURES: Investigation identifies multiple co-occurring concentrations."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.MULTIPLE_FAILURES)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        self.assertTrue(report.has_multiple_concentrations)

        # Must find both UPI and IN-TN in primary or secondary findings
        all_found = report.primary_findings + report.secondary_findings
        found_dims_values = {(f.dimension, f.value) for f in all_found}

        self.assertIn((Dimension.PAYMENT_METHOD, "upi"), found_dims_values)
        self.assertIn((Dimension.REGION, "IN-TN"), found_dims_values)

    def test_normal_investigation_finds_no_concentrations(self):
        """NORMAL: Investigation produces no false root-cause concentrations."""
        data, _, metrics = self._get_incident_and_data(ScenarioId.NORMAL)
        # Pass a mock incident object or investigate directly
        report = analyze_incident(
            incident_id="inc_normal",
            window=data.incident_window,
            current_payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            investigated_at=data.anchor,
        )
        self.assertTrue(report.has_sufficient_evidence)
        self.assertEqual(len(report.primary_findings), 0)
        self.assertIn("No concentrated degradation", report.summary)

    def test_false_alarm_investigation_finds_no_anomalous_dimension(self):
        """FALSE_ALARM: Same-hour baseline prevents false attribution."""
        data, _, _ = self._get_incident_and_data(ScenarioId.FALSE_ALARM)
        report = analyze_incident(
            incident_id="inc_false_alarm",
            window=data.incident_window,
            current_payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            investigated_at=data.anchor,
            same_hour_baseline=True,
        )
        self.assertTrue(report.has_sufficient_evidence)
        self.assertEqual(len(report.primary_findings), 0)

    def test_small_random_variation_investigation_flags_inadmissible_evidence(self):
        """SMALL_RANDOM_VARIATION: Low transaction volume marks findings as insufficient."""
        data, _, _ = self._get_incident_and_data(ScenarioId.SMALL_RANDOM_VARIATION)
        report = analyze_incident(
            incident_id="inc_small",
            window=data.incident_window,
            current_payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            investigated_at=data.anchor,
        )
        self.assertEqual(len(report.primary_findings), 0)

    def test_insufficient_data_investigation_returns_insufficient_evidence(self):
        """INSUFFICIENT_DATA: Abstains and explicitly reports insufficient data."""
        data, _, _ = self._get_incident_and_data(ScenarioId.INSUFFICIENT_DATA)
        report = analyze_incident(
            incident_id="inc_insufficient",
            window=data.incident_window,
            current_payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            investigated_at=data.anchor,
        )
        self.assertFalse(report.has_sufficient_evidence)
        self.assertEqual(len(report.primary_findings), 0)
        self.assertIn("Insufficient data", report.summary)

    def test_recovery_not_eligible_investigation_identifies_risk_blocked_category(self):
        """RECOVERY_NOT_ELIGIBLE: Identifies risk_blocked failure category."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.assertTrue(report.has_sufficient_evidence)
        risk_findings = [
            f for f in report.primary_findings
            if f.dimension is Dimension.FAILURE_CATEGORY and f.value == "risk_blocked"
        ]
        self.assertEqual(len(risk_findings), 1)
        self.assertEqual(risk_findings[0].strength, EvidenceStrength.STRONG_EVIDENCE)

    def test_investigation_is_deterministic(self):
        """Identical inputs produce byte-identical reports and evidence."""
        data, incident, _ = self._get_incident_and_data(ScenarioId.UPI_FAILURE_SPIKE)
        r1 = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )
        r2 = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )
        self.assertEqual(r1.summary, r2.summary)
        self.assertEqual(r1.primary_findings, r2.primary_findings)
        self.assertEqual(len(r1.evidence), len(r2.evidence))
        for ev1, ev2 in zip(r1.evidence, r2.evidence):
            self.assertEqual(ev1.evidence_id, ev2.evidence_id)
            self.assertEqual(ev1.summary, ev2.summary)


class ContextAssemblerUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        from ...db.database import Database
        from ...investigation.context import ContextAssembler
        self.db = Database(":memory:")
        self.assembler = ContextAssembler(database=self.db)

    def test_context_assembler_passes_baseline_windows_and_calculates_baseline_metrics(self) -> None:
        """When sufficient historical baseline (>=100) exists, baseline metrics are calculated."""
        from ...domain.payment import EnrichedPayment, Payment
        from ...domain.money import Money

        # 1. Seed 120 baseline payments (past 7 days, 4% failure rate)
        base_payments = []
        for i in range(120):
            status = PaymentStatus.FAILED if i < 5 else PaymentStatus.CAPTURED
            base_payments.append(
                EnrichedPayment(
                    payment=Payment(
                        id=f"pay_base_{i}",
                        amount=Money(50000),
                        status=status,
                        method=PaymentMethod.UPI,
                        created_at=NOW - timedelta(days=1, hours=i % 24, minutes=i % 60),
                        error_code="BAD_REQUEST_ERROR" if status == PaymentStatus.FAILED else None,
                        error_description="Timeout" if status == PaymentStatus.FAILED else None,
                    )
                )
            )
        self.db.save_payments(base_payments, merchant_id="m_test")

        # 2. Add 3 recent UPI failures in current window
        recent_fails = []
        for i in range(3):
            p = Payment(
                id=f"pay_recent_fail_{i}",
                amount=Money(50000),
                status=PaymentStatus.FAILED,
                method=PaymentMethod.UPI,
                created_at=NOW - timedelta(minutes=5 * i + 1),
                error_code="BAD_REQUEST_ERROR",
                error_description="UPI bank timeout",
            )
            recent_fails.append(p)
            self.db.save_payment(p, merchant_id="m_test")

        # 3. Assemble context for the triggering payment
        ctx = self.assembler.assemble(
            payment=recent_fails[0],
            merchant_id="m_test",
            now=NOW,
        )

        # 4. Verify baseline_windows was passed and baseline metrics computed
        self.assertIsNotNone(ctx.metrics.baseline)
        self.assertTrue(ctx.metrics.baseline.is_sufficient)
        self.assertEqual(ctx.metrics.baseline.decided_sample, 120)
        self.assertIsNotNone(ctx.metrics.deviation)
        self.assertIsNotNone(ctx.metrics.significance)
        self.assertGreater(ctx.metrics.significance.z_score, 3.0)

        # 5. Verify scenario classified as UPI_FAILURE_SPIKE incident
        self.assertEqual(ctx.classification.scenario_id.value, "upi_failure_spike")
        self.assertTrue(ctx.classification.is_incident)
        self.assertTrue(ctx.classification.is_action_eligible)
        self.assertIsNotNone(ctx.incident)

    def test_context_assembler_handles_insufficient_historical_baseline_safely(self) -> None:
        """When baseline has < 100 decided transactions, baseline is not sufficient and stops as INSUFFICIENT_DATA."""
        from ...domain.payment import EnrichedPayment, Payment
        from ...domain.money import Money

        # Seed only 10 historical payments (< 100)
        base_payments = [
            EnrichedPayment(
                payment=Payment(
                    id=f"pay_tiny_base_{i}",
                    amount=Money(50000),
                    status=PaymentStatus.CAPTURED,
                    method=PaymentMethod.UPI,
                    created_at=NOW - timedelta(days=1, hours=i),
                )
            )
            for i in range(10)
        ]
        self.db.save_payments(base_payments, merchant_id="m_test")

        # Trigger payment
        trigger = Payment(
            id="pay_isolated_fail",
            amount=Money(50000),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.UPI,
            created_at=NOW,
            error_code="BAD_REQUEST_ERROR",
            error_description="UPI bank timeout",
        )
        self.db.save_payment(trigger, merchant_id="m_test")

        ctx = self.assembler.assemble(
            payment=trigger,
            merchant_id="m_test",
            now=NOW,
        )

        self.assertIsNotNone(ctx.metrics.baseline)
        self.assertFalse(ctx.metrics.baseline.is_sufficient)
        self.assertEqual(ctx.classification.scenario_id.value, "insufficient_data")
        self.assertFalse(ctx.classification.is_incident)
        self.assertIsNone(ctx.incident)

    def test_context_assembler_handles_empty_db_cold_start(self) -> None:
        """When database is completely empty (no baseline), stops safely as INSUFFICIENT_DATA."""
        from ...domain.payment import Payment
        from ...domain.money import Money

        trigger = Payment(
            id="pay_cold_start",
            amount=Money(50000),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.CARD,
            created_at=NOW,
            error_code="BAD_REQUEST_ERROR",
            error_description="Card processing error",
        )

        ctx = self.assembler.assemble(
            payment=trigger,
            merchant_id="m_test",
            now=NOW,
        )

        self.assertIsNone(ctx.metrics.baseline)
        self.assertEqual(ctx.classification.scenario_id.value, "insufficient_data")
        self.assertFalse(ctx.classification.is_incident)
        self.assertIsNone(ctx.incident)


if __name__ == "__main__":
    unittest.main()
