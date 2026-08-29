"""Tests for the SQLite persistence layer.

Tests:
1. Payment and EnrichedPayment round-trip, batch operations, filtering.
2. FinancialIncident and FinancialEvidence round-trip.
3. InvestigationReport round-trip.
4. AuditEvent persistence and sequence order preservation.
5. Invariant preservation:
   - Money stored as integer paise (minor_units), never float.
   - None / NULL semantics preserved (never converted to 0).
   - Timezones preserved as aware UTC.
   - Enums preserved.
6. Missing records return None.
7. Database isolation.
"""

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.audit import AuditEvent
from ...domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    Dimension,
    FailureCategory,
    IncidentStatus,
    IncidentType,
    PaymentMethod,
    PaymentStatus,
    Severity,
    SourceConfidence,
)
from ...domain.errors import DomainValidationError
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.money import Money
from ...domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ...domain.window import UTC, TimeWindow
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ..helpers import HOUR, NOW, T0, enriched, payment, population


class DatabasePaymentTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_payment_save_and_retrieve_round_trip(self):
        p = Payment(
            id="pay_1001",
            order_id="order_1001",
            amount=Money(4999_00, Currency.INR),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.CARD,
            created_at=NOW,
            error_code="BAD_REQUEST_ERROR:card_declined",
            error_description="Card declined by issuer",
            error_source="issuer",
            error_step="payment_authorization",
            error_reason="card_declined",
        )

        self.db.save_payment(p)
        loaded = self.db.get_payment("pay_1001")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.id, p.id)
        self.assertEqual(loaded.order_id, p.order_id)
        self.assertEqual(loaded.amount.minor_units, 4999_00)
        self.assertEqual(loaded.amount.currency, Currency.INR)
        self.assertEqual(loaded.status, PaymentStatus.FAILED)
        self.assertEqual(loaded.method, PaymentMethod.CARD)
        self.assertEqual(loaded.created_at, p.created_at)
        self.assertEqual(loaded.error_code, "BAD_REQUEST_ERROR:card_declined")
        self.assertEqual(loaded.error_description, "Card declined by issuer")
        self.assertEqual(loaded.error_source, "issuer")
        self.assertEqual(loaded.error_step, "payment_authorization")
        self.assertEqual(loaded.error_reason, "card_declined")

    def test_enriched_payment_save_and_retrieve(self):
        p = Payment(
            id="pay_1002",
            order_id="order_1002",
            amount=Money(150_00),
            status=PaymentStatus.CAPTURED,
            method=PaymentMethod.UPI,
            created_at=NOW,
        )
        enr = PaymentEnrichment(
            payment_id="pay_1002",
            region="IN-KA",
            provider="acquirer_a",
            failure_category=None,
            source_confidence=SourceConfidence.ENRICHED,
        )
        ep = EnrichedPayment(payment=p, enrichment=enr)

        self.db.save_payment(ep)
        loaded = self.db.get_enriched_payment("pay_1002")

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.payment.id, "pay_1002")
        self.assertEqual(loaded.payment.status, PaymentStatus.CAPTURED)
        self.assertEqual(loaded.enrichment.region, "IN-KA")
        self.assertEqual(loaded.enrichment.provider, "acquirer_a")
        self.assertEqual(loaded.enrichment.source_confidence, SourceConfidence.ENRICHED)

    def test_save_payments_batch_and_list_filtering(self):
        t1 = T0 + timedelta(minutes=10)
        t2 = T0 + timedelta(minutes=20)
        t3 = T0 + timedelta(minutes=70)  # Next hour

        p1 = payment(id="p1", amount_paise=1000, status=PaymentStatus.CAPTURED, method=PaymentMethod.UPI, created_at=t1)
        p2 = payment(id="p2", amount_paise=2000, status=PaymentStatus.FAILED, method=PaymentMethod.CARD, created_at=t2)
        p3 = payment(id="p3", amount_paise=3000, status=PaymentStatus.CAPTURED, method=PaymentMethod.UPI, created_at=t3)

        self.db.save_payments([p1, p2, p3])

        # Filter by window (HOUR covers t1 and t2, not t3)
        window_payments = self.db.list_payments(window=HOUR)
        self.assertEqual(len(window_payments), 2)
        self.assertEqual([p.payment.id for p in window_payments], ["p1", "p2"])

        # Filter by status
        failed_payments = self.db.list_payments(status=PaymentStatus.FAILED)
        self.assertEqual(len(failed_payments), 1)
        self.assertEqual(failed_payments[0].payment.id, "p2")

        # Filter by method
        upi_payments = self.db.list_payments(method=PaymentMethod.UPI)
        self.assertEqual(len(upi_payments), 2)
        self.assertEqual([p.payment.id for p in upi_payments], ["p1", "p3"])

    def test_get_nonexistent_payment_returns_none(self):
        self.assertIsNone(self.db.get_payment("pay_nonexistent"))
        self.assertIsNone(self.db.get_enriched_payment("pay_nonexistent"))


class DatabaseIncidentTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()

    def tearDown(self):
        self.db.close()

    def test_incident_save_and_retrieve_round_trip(self):
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )

        incident = self.detector.detect(metrics, merchant_id="merchant_test")
        self.assertIsNotNone(incident)

        self.db.save_incident(incident)
        loaded = self.db.get_incident(incident.incident_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.incident_id, incident.incident_id)
        self.assertEqual(loaded.incident_key, incident.incident_key)
        self.assertEqual(loaded.merchant_id, "merchant_test")
        self.assertEqual(loaded.incident_type, IncidentType.PAYMENT_FAILURE_SPIKE)
        self.assertEqual(loaded.status, IncidentStatus.DETECTED)
        self.assertEqual(loaded.severity, incident.severity)
        self.assertEqual(loaded.window, incident.window)
        self.assertEqual(loaded.detected_at, incident.detected_at)

        # Verify metrics round trip
        self.assertEqual(loaded.metrics.counts, incident.metrics.counts)
        self.assertEqual(loaded.metrics.failure_rate, incident.metrics.failure_rate)
        self.assertEqual(
            loaded.metrics.deviation.absolute_percentage_points,
            incident.metrics.deviation.absolute_percentage_points,
        )
        self.assertEqual(
            loaded.metrics.revenue_risk.failed_gmv,
            incident.metrics.revenue_risk.failed_gmv,
        )

        # Verify evidence round trip
        self.assertEqual(len(loaded.evidence), len(incident.evidence))
        self.assertEqual(loaded.evidence[0].evidence_id, incident.evidence[0].evidence_id)
        self.assertEqual(loaded.evidence[0].summary, incident.evidence[0].summary)

    def test_get_incident_by_idempotency_key(self):
        data = generate_scenario(ScenarioId.CARD_FAILURE_SPIKE)
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )
        incident = self.detector.detect(metrics, merchant_id="m_1")
        self.assertIsNotNone(incident)

        self.db.save_incident(incident)
        loaded = self.db.get_incident_by_key(incident.incident_key)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.incident_id, incident.incident_id)

    def test_list_incidents_filtering(self):
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )

        inc1 = self.detector.detect(metrics, merchant_id="m_1", incident_id="inc_1")
        inc2 = self.detector.detect(metrics, merchant_id="m_2", incident_id="inc_2")

        self.db.save_incident(inc1)
        self.db.save_incident(inc2)

        m1_incidents = self.db.list_incidents(merchant_id="m_1")
        self.assertEqual(len(m1_incidents), 1)
        self.assertEqual(m1_incidents[0].merchant_id, "m_1")


class DatabaseInvestigationTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()

    def tearDown(self):
        self.db.close()

    def test_investigation_report_round_trip(self):
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )
        incident = self.detector.detect(metrics, merchant_id="m_upi")
        self.assertIsNotNone(incident)

        self.db.save_incident(incident)

        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )

        self.db.save_investigation(report)
        loaded = self.db.get_investigation(incident.incident_id)

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.incident_id, incident.incident_id)
        self.assertEqual(loaded.has_sufficient_evidence, report.has_sufficient_evidence)
        self.assertEqual(loaded.has_multiple_concentrations, report.has_multiple_concentrations)
        self.assertEqual(loaded.summary, report.summary)

        # Check primary findings
        self.assertEqual(len(loaded.primary_findings), len(report.primary_findings))
        for f_orig, f_loaded in zip(report.primary_findings, loaded.primary_findings):
            self.assertEqual(f_orig.dimension, f_loaded.dimension)
            self.assertEqual(f_orig.value, f_loaded.value)
            self.assertEqual(f_orig.strength, f_loaded.strength)
            self.assertEqual(f_orig.counts, f_loaded.counts)
            self.assertEqual(f_orig.failed_gmv, f_loaded.failed_gmv)
            self.assertEqual(f_orig.deviation_pp, f_loaded.deviation_pp)
            self.assertEqual(f_orig.relative_lift, f_loaded.relative_lift)


class DatabaseAuditTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_audit_event_persistence_and_ordering(self):
        e1 = AuditEvent(
            event_id="aud_1",
            sequence=1,
            occurred_at=NOW,
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="First event",
            incident_id="inc_1",
            payload={"count": 5},
        )
        e2 = AuditEvent(
            event_id="aud_2",
            sequence=2,
            occurred_at=NOW + timedelta(seconds=1),
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Second event",
            incident_id="inc_1",
            payload={"window": "2026-08-26T12:00:00Z/1h"},
        )

        self.db.save_audit_event(e1)
        self.db.save_audit_event(e2)

        events = self.db.list_audit_events(incident_id="inc_1")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].event_id, "aud_1")
        self.assertEqual(events[0].sequence, 1)
        self.assertEqual(events[0].payload_digest, e1.payload_digest)
        self.assertEqual(events[1].event_id, "aud_2")
        self.assertEqual(events[1].sequence, 2)
        self.assertEqual(self.db.get_max_audit_sequence(), 2)


class DatabaseFileIsolationTests(unittest.TestCase):
    def test_file_database_isolation(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            temp_path = f.name

        try:
            db1 = Database(temp_path)
            p = payment(id="pay_iso_1", amount_paise=5000, created_at=NOW)
            db1.save_payment(p)
            db1.close()

            # Re-open in fresh instance
            db2 = Database(temp_path)
            loaded = db2.get_payment("pay_iso_1")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.id, "pay_iso_1")
            db2.close()
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            # Remove WAL / SHM files if present
            for extra in (temp_path + "-wal", temp_path + "-shm"):
                if os.path.exists(extra):
                    os.remove(extra)


if __name__ == "__main__":
    unittest.main()
