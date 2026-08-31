"""Comprehensive tests for Payment Ingestion, Normalization, Enrichment, and Service layers."""

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.domain.enums import (
    Currency,
    FailureCategory,
    PaymentMethod,
    PaymentStatus,
    SourceConfidence,
)
from backend.domain.errors import DomainValidationError, MoneyPrecisionError
from backend.domain.money import Money
from backend.domain.payment import EnrichedPayment, Payment
from backend.domain.window import UTC
from backend.ingestion.enricher import PaymentEnricher
from backend.ingestion.normalizer import PaymentNormalizer
from backend.ingestion.service import IngestionService


class TestPaymentNormalizer(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = PaymentNormalizer()

    def test_valid_payment_normalization_unix_seconds(self) -> None:
        payload = {
            "id": "pay_test_001",
            "amount": 50000,  # 500.00 INR in paise
            "currency": "INR",
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
            "order_id": "order_001",
        }
        payment = self.normalizer.normalize(payload)
        self.assertEqual(payment.id, "pay_test_001")
        self.assertEqual(payment.amount, Money(50000, Currency.INR))
        self.assertEqual(payment.status, PaymentStatus.CAPTURED)
        self.assertEqual(payment.method, PaymentMethod.UPI)
        self.assertEqual(payment.created_at, datetime(2024, 8, 26, 13, 0, 0, tzinfo=UTC))
        self.assertEqual(payment.order_id, "order_001")
        self.assertIsNone(payment.error_code)

    def test_valid_payment_normalization_iso_timestamp(self) -> None:
        payload = {
            "id": "pay_test_002",
            "amount": 250000,
            "status": "authorized",
            "method": "card",
            "created_at": "2026-08-26T13:30:00Z",
        }
        payment = self.normalizer.normalize(payload)
        self.assertEqual(payment.id, "pay_test_002")
        self.assertEqual(payment.status, PaymentStatus.AUTHORIZED)
        self.assertEqual(payment.method, PaymentMethod.CARD)
        self.assertEqual(payment.created_at, datetime(2026, 8, 26, 13, 30, 0, tzinfo=UTC))

    def test_timestamp_with_timezone_offset(self) -> None:
        payload = {
            "id": "pay_test_offset",
            "amount": 10000,
            "status": "captured",
            "method": "netbanking",
            "created_at": "2026-08-26T18:30:00+05:30",  # 13:00 UTC
        }
        payment = self.normalizer.normalize(payload)
        self.assertEqual(payment.created_at, datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC))

    def test_timestamp_milliseconds(self) -> None:
        payload = {
            "id": "pay_test_ms",
            "amount": 10000,
            "status": "captured",
            "method": "wallet",
            "created_at": 1724677200000,  # Milliseconds
        }
        payment = self.normalizer.normalize(payload)
        self.assertEqual(payment.created_at, datetime(2024, 8, 26, 13, 0, 0, tzinfo=UTC))

    def test_amount_formats(self) -> None:
        # 1. String integer paise
        p1 = self.normalizer.normalize({
            "id": "pay_p1",
            "amount": "15000",
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
        })
        self.assertEqual(p1.amount, Money(15000, Currency.INR))

        # 2. String decimal rupees
        p2 = self.normalizer.normalize({
            "id": "pay_p2",
            "amount": "150.50",
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
        })
        self.assertEqual(p2.amount, Money(15050, Currency.INR))

        # 3. Explicit amount_rupees
        p3 = self.normalizer.normalize({
            "id": "pay_p3",
            "amount_rupees": "99.00",
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
        })
        self.assertEqual(p3.amount, Money(9900, Currency.INR))

        # 4. Rejection of floats
        with self.assertRaises(MoneyPrecisionError):
            self.normalizer.normalize({
                "id": "pay_float",
                "amount": 150.50,  # Float rejected
                "status": "captured",
                "method": "upi",
                "created_at": 1724677200,
            })

    def test_rejection_of_zero_or_negative_amount(self) -> None:
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({
                "id": "pay_zero",
                "amount": 0,
                "status": "captured",
                "method": "upi",
                "created_at": 1724677200,
            })

        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({
                "id": "pay_neg",
                "amount": -500,
                "status": "captured",
                "method": "upi",
                "created_at": 1724677200,
            })

    def test_status_and_method_case_insensitivity(self) -> None:
        p = self.normalizer.normalize({
            "id": "pay_case",
            "amount": 1000,
            "status": "SUCCESS",
            "method": "CREDIT_CARD",
            "created_at": 1724677200,
        })
        self.assertEqual(p.status, PaymentStatus.CAPTURED)
        self.assertEqual(p.method, PaymentMethod.CARD)

    def test_unknown_method_fallback(self) -> None:
        p = self.normalizer.normalize({
            "id": "pay_unknown",
            "amount": 1000,
            "status": "captured",
            "method": "CRYPTO_TOKEN",
            "created_at": 1724677200,
        })
        self.assertEqual(p.method, PaymentMethod.UNKNOWN)

    def test_failed_payment_error_fields_preserved(self) -> None:
        payload = {
            "id": "pay_failed_001",
            "amount": 100000,
            "status": "failed",
            "method": "upi",
            "created_at": 1724677200,
            "error_code": "BAD_REQUEST_ERROR:gateway_timeout",
            "error_description": "Gateway connection timed out after 30s",
            "error_source": "gateway",
            "error_step": "payment_authorization",
            "error_reason": "gateway_timeout",
        }
        payment = self.normalizer.normalize(payload)
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.assertEqual(payment.error_code, "BAD_REQUEST_ERROR:gateway_timeout")
        self.assertEqual(payment.error_description, "Gateway connection timed out after 30s")
        self.assertEqual(payment.error_source, "gateway")
        self.assertEqual(payment.error_step, "payment_authorization")
        self.assertEqual(payment.error_reason, "gateway_timeout")

    def test_captured_payment_error_fields_cleared(self) -> None:
        # If payload carries leftover error fields on a successful payment, clear them
        payload = {
            "id": "pay_captured_clean",
            "amount": 100000,
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
            "error_code": "STALE_ERROR",
        }
        payment = self.normalizer.normalize(payload)
        self.assertIsNone(payment.error_code)

    def test_malformed_payload_rejections(self) -> None:
        # Non-mapping
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize("not a dict")  # type: ignore

        # Missing ID
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({"amount": 1000, "status": "captured", "method": "upi", "created_at": 1724677200})

        # Empty string ID
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({"id": "   ", "amount": 1000, "status": "captured", "method": "upi", "created_at": 1724677200})

        # Missing timestamp
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({"id": "pay_01", "amount": 1000, "status": "captured", "method": "upi"})

        # Invalid timestamp string
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({"id": "pay_01", "amount": 1000, "status": "captured", "method": "upi", "created_at": "invalid-time"})

        # Invalid currency
        with self.assertRaises(DomainValidationError):
            self.normalizer.normalize({"id": "pay_01", "amount": 1000, "status": "captured", "method": "upi", "created_at": 1724677200, "currency": "XYZ"})


class TestPaymentEnricher(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = PaymentNormalizer()
        self.enricher = PaymentEnricher()

    def test_enrichment_dimensions(self) -> None:
        raw = {
            "id": "pay_enr_001",
            "amount": 50000,
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
            "region": "South",
            "provider": "HDFC",
            "segment": "enterprise",
        }
        payment = self.normalizer.normalize(raw)
        enriched = self.enricher.enrich(payment, raw_payload=raw)

        self.assertEqual(enriched.payment, payment)
        self.assertEqual(enriched.region, "South")
        self.assertEqual(enriched.provider, "HDFC")
        self.assertEqual(enriched.segment, "enterprise")
        self.assertIsNone(enriched.failure_category)
        self.assertEqual(enriched.enrichment.source_confidence, SourceConfidence.ENRICHED)

    def test_enrichment_acquirer_data_fallback(self) -> None:
        raw = {
            "id": "pay_enr_002",
            "amount": 50000,
            "status": "captured",
            "method": "card",
            "created_at": 1724677200,
            "acquirer_data": {
                "bank": "ICICI"
            }
        }
        payment = self.normalizer.normalize(raw)
        enriched = self.enricher.enrich(payment, raw_payload=raw)
        self.assertEqual(enriched.provider, "ICICI")

    def test_enrichment_failure_category_classification(self) -> None:
        test_cases = [
            ("GATEWAY_ERROR:gateway_timeout", FailureCategory.TIMEOUT),
            ("BAD_REQUEST_ERROR:insufficient_funds", FailureCategory.INSUFFICIENT_FUNDS),
            ("BAD_REQUEST_ERROR:auth_failed", FailureCategory.AUTHENTICATION_FAILED),
            ("GATEWAY_ERROR:issuer_unavailable", FailureCategory.ISSUER_UNAVAILABLE),
            ("GATEWAY_ERROR:downstream_failure", FailureCategory.GATEWAY_ERROR),
            ("BAD_REQUEST_ERROR:card_expired", FailureCategory.INVALID_INSTRUMENT),
            ("RISK_BLOCKED:fraud_detected", FailureCategory.RISK_BLOCKED),
            ("CUSTOMER_DROPPED:user_cancelled", FailureCategory.CUSTOMER_DROPPED),
            ("UNKNOWN_ERROR_CODE", FailureCategory.UNKNOWN),
        ]

        for error_code, expected_cat in test_cases:
            raw = {
                "id": f"pay_err_{expected_cat.value}",
                "amount": 50000,
                "status": "failed",
                "method": "upi",
                "created_at": 1724677200,
                "error_code": error_code,
            }
            payment = self.normalizer.normalize(raw)
            enriched = self.enricher.enrich(payment, raw_payload=raw)
            self.assertEqual(enriched.failure_category, expected_cat)


class TestIngestionService(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.service = IngestionService(database=self.db, audit_log=self.audit_log)

    def tearDown(self) -> None:
        self.db.close()

    def test_single_payment_ingestion_and_persistence(self) -> None:
        payload = {
            "id": "pay_single_001",
            "amount": 75000,
            "status": "captured",
            "method": "upi",
            "created_at": "2026-08-26T13:00:00Z",
            "region": "West",
            "provider": "AXIS",
        }
        enriched = self.service.ingest_payment(payload)
        self.assertEqual(enriched.payment.id, "pay_single_001")

        # Verify saved in SQLite database
        saved = self.db.get_payment("pay_single_001")
        self.assertIsNotNone(saved)
        self.assertEqual(saved.id, "pay_single_001")
        self.assertEqual(saved.amount, Money(75000, Currency.INR))

        saved_enr = self.db.get_enriched_payment("pay_single_001")
        self.assertIsNotNone(saved_enr)
        self.assertEqual(saved_enr.region, "West")
        self.assertEqual(saved_enr.provider, "AXIS")

        # Verify audit log
        self.assertEqual(len(self.audit_log.events), 1)
        self.assertEqual(self.audit_log.events[0].event_type.value, "fact_ingested")
        self.assertTrue(self.audit_log.verify_integrity()[0])

    def test_batch_ingestion_with_mixed_valid_and_invalid(self) -> None:
        payloads = [
            {
                "id": "pay_b1",
                "amount": 10000,
                "status": "captured",
                "method": "upi",
                "created_at": 1724677200,
                "region": "North",
            },
            {
                "id": "pay_invalid",
                # missing amount and status
                "created_at": 1724677200,
            },
            {
                "id": "pay_b2",
                "amount": 20000,
                "status": "failed",
                "method": "card",
                "created_at": 1724677200,
                "error_code": "GATEWAY_ERROR:timeout",
                "provider": "SBI",
            },
        ]

        result = self.service.ingest_batch(payloads, batch_id="batch_test_01")
        self.assertEqual(result.total_received, 3)
        self.assertEqual(result.ingested, 2)
        self.assertEqual(result.failed, 1)
        self.assertFalse(result.is_all_successful)

        # Check items
        self.assertTrue(result.items[0].success)
        self.assertFalse(result.items[1].success)
        self.assertIn("missing", result.items[1].error.lower())
        self.assertTrue(result.items[2].success)

        # Check DB contents
        self.assertIsNotNone(self.db.get_payment("pay_b1"))
        self.assertIsNone(self.db.get_payment("pay_invalid"))
        self.assertIsNotNone(self.db.get_payment("pay_b2"))

        # Check audit log
        self.assertEqual(len(self.audit_log.events), 1)
        event = self.audit_log.events[0]
        self.assertEqual(event.payload["ingested_count"], 2)
        self.assertEqual(event.payload["failed_count"], 1)

    def test_idempotent_duplicate_ingestion(self) -> None:
        payload1 = {
            "id": "pay_idem_001",
            "amount": 10000,
            "status": "created",
            "method": "upi",
            "created_at": 1724677200,
        }
        self.service.ingest_payment(payload1)
        saved1 = self.db.get_payment("pay_idem_001")
        self.assertEqual(saved1.status, PaymentStatus.CREATED)

        # Ingest updated status for same ID
        payload2 = {
            "id": "pay_idem_001",
            "amount": 10000,
            "status": "captured",
            "method": "upi",
            "created_at": 1724677200,
        }
        self.service.ingest_payment(payload2)
        saved2 = self.db.get_payment("pay_idem_001")
        self.assertEqual(saved2.status, PaymentStatus.CAPTURED)

        # Total payments in DB should still be exactly 1
        all_payments = self.db.list_payments()
        self.assertEqual(len(all_payments), 1)


if __name__ == "__main__":
    unittest.main()
