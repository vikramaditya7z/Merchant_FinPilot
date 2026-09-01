"""Unit and safety tests for Razorpay Webhook Execution Reconciliation.

PROJECT_RULES 7.3, 7.4, 10.7, 10.8, 10.9 / ARCHITECTURE.md §12, §13, §14, §15.
"""

import hashlib
import hmac
import json
import unittest
from datetime import datetime
from decimal import Decimal

from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.domain.canonical import short_digest
from backend.domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    ExecutionStatus,
    IntentAction,
    TargetEntityType,
)
from backend.domain.intent import AgentIntent, IntentTarget
from backend.domain.money import Money
from backend.domain.window import UTC
from backend.execution.contracts import ExecutionResult
from backend.execution.store import ExecutionStore
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.reconciler import RazorpayReconciler, ReconciliationReport, ReconciliationStatus
from backend.razorpay.service import RazorpayService
from backend.razorpay.webhook import RazorpayWebhookHandler


class TestRazorpayReconciliation(unittest.TestCase):
    """Test suite for Razorpay execution webhook correlation and reconciliation."""

    def setUp(self) -> None:
        self.secret = "whsec_test_reconcile_456"
        self.config = RazorpayConfig(
            key_id="rzp_test_recon_key",
            key_secret="sec_recon_key",
            webhook_secret=self.secret,
        )
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.store = ExecutionStore()
        self.reconciler = RazorpayReconciler(store=self.store, audit_log=self.audit_log)
        self.service = RazorpayService(
            config=self.config,
            execution_store=self.store,
            reconciler=self.reconciler,
            database=self.db,
            audit_log=self.audit_log,
        )
        self.now = datetime(2026, 8, 31, 14, 0, 0, tzinfo=UTC)

    def _sign_payload(self, body_dict: dict) -> bytes:
        raw_bytes = json.dumps(body_dict).encode("utf-8")
        sig = hmac.new(self.secret.encode("utf-8"), raw_bytes, hashlib.sha256).hexdigest()
        return raw_bytes, sig

    def _record_sample_execution(
        self,
        provider_ref: str = "plink_test_001",
        idempotency_key: str = "idemp_recon_001",
        action: IntentAction = IntentAction.CREATE_PAYMENT_LINK,
        status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    ) -> ExecutionResult:
        exec_res = ExecutionResult(
            execution_id=f"exec_{short_digest({'key': idempotency_key})}",
            decision_id="dec_recon_001",
            intent_id="intent_recon_001",
            action=action,
            status=status,
            idempotency_key=idempotency_key,
            attempted_at=self.now,
            completed_at=self.now,
            provider_reference=provider_ref,
            response_digest="digest_initial_001",
            is_simulation=True,
            message="Initial test execution recorded.",
        )
        self.store.save(exec_res)
        return exec_res

    def test_successful_payment_link_paid_reconciliation(self) -> None:
        """Verify payment_link.paid webhook correlates with outbound execution and updates state."""
        plink_id = "plink_test_1001"
        idemp_key = "idemp_recon_1001"
        initial_exec = self._record_sample_execution(provider_ref=plink_id, idempotency_key=idemp_key)

        payload = {
            "entity": "event",
            "account_id": "acc_test_merchant",
            "event": "payment_link.paid",
            "contains": ["payment_link", "payment"],
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": 50000,
                        "amount_paid": 50000,
                        "status": "paid",
                        "reference_id": idemp_key,
                        "currency": "INR",
                        "notes": {
                            "incident_id": "inc_001",
                            "intent_id": "intent_recon_001",
                        },
                    }
                },
                "payment": {
                    "entity": {
                        "id": "pay_test_payment_99",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "created_at": 1700000000,
                    }
                },
            },
        }

        raw_bytes, sig = self._sign_payload(payload)
        status_code, resp = self.service.handle_webhook(raw_bytes, sig)

        self.assertEqual(status_code, 200)
        self.assertEqual(resp["status"], "processed")
        self.assertIn("reconciliation", resp)
        self.assertEqual(resp["reconciliation"]["status"], "matched")
        self.assertEqual(resp["reconciliation"]["reconciled_status"], "succeeded")
        self.assertEqual(resp["reconciliation"]["provider_reference"], plink_id)

        # Check that execution in store is updated
        updated_exec = self.store.get(idemp_key)
        self.assertIsNotNone(updated_exec)
        self.assertEqual(updated_exec.status, ExecutionStatus.SUCCEEDED)
        self.assertIn("verified PAID", updated_exec.message)

        # Check Audit Log contains OUTCOME_VERIFIED event
        outcome_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.OUTCOME_VERIFIED
        ]
        self.assertEqual(len(outcome_events), 1)
        self.assertEqual(outcome_events[0].subject_id, initial_exec.execution_id)
        self.assertEqual(outcome_events[0].payload["reconciled_status"], "succeeded")

    def test_payment_link_cancelled_reconciliation_marks_failed(self) -> None:
        """Verify payment_link.cancelled webhook updates execution status to FAILED."""
        plink_id = "plink_test_cancelled"
        idemp_key = "idemp_recon_cancelled"
        self._record_sample_execution(provider_ref=plink_id, idempotency_key=idemp_key)

        payload = {
            "entity": "event",
            "event": "payment_link.cancelled",
            "contains": ["payment_link"],
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": 25000,
                        "status": "cancelled",
                        "reference_id": idemp_key,
                        "currency": "INR",
                    }
                }
            },
        }

        raw_bytes, sig = self._sign_payload(payload)
        status_code, resp = self.service.handle_webhook(raw_bytes, sig)

        self.assertEqual(status_code, 200)
        self.assertEqual(resp["reconciliation"]["status"], "matched")
        self.assertEqual(resp["reconciliation"]["reconciled_status"], "failed")

        # Verify updated store state
        updated_exec = self.store.get(idemp_key)
        self.assertIsNotNone(updated_exec)
        self.assertEqual(updated_exec.status, ExecutionStatus.FAILED)
        self.assertIn("terminal state", updated_exec.message)

    def test_invalid_signature_rejected_without_reconciliation(self) -> None:
        """Safety Gate: Webhook with invalid signature is rejected (401) and does not reconcile state."""
        plink_id = "plink_test_forged"
        idemp_key = "idemp_recon_forged"
        self._record_sample_execution(provider_ref=plink_id, idempotency_key=idemp_key)

        payload = {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {"entity": {"id": plink_id, "amount": 50000, "status": "paid"}}
            },
        }

        raw_bytes = json.dumps(payload).encode("utf-8")
        status_code, resp = self.service.handle_webhook(raw_bytes, signature="invalid_forged_sig")

        self.assertEqual(status_code, 401)
        self.assertEqual(resp["status"], "error")

        # Verify store was untouched
        exec_res = self.store.get(idemp_key)
        self.assertIsNotNone(exec_res)
        self.assertEqual(exec_res.message, "Initial test execution recorded.")

    def test_duplicate_replayed_webhook_is_idempotent(self) -> None:
        """Idempotency: Replayed webhooks with the same event ID are skipped safely."""
        plink_id = "plink_test_dup"
        idemp_key = "idemp_recon_dup"
        self._record_sample_execution(provider_ref=plink_id, idempotency_key=idemp_key)

        payload = {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": 50000,
                        "status": "paid",
                        "reference_id": idemp_key,
                    }
                }
            },
        }

        raw_bytes, sig = self._sign_payload(payload)

        # First delivery
        code1, resp1 = self.service.handle_webhook(raw_bytes, sig)
        self.assertEqual(code1, 200)
        self.assertEqual(resp1["status"], "processed")

        # Second delivery (replay)
        code2, resp2 = self.service.handle_webhook(raw_bytes, sig)
        self.assertEqual(code2, 200)
        self.assertEqual(resp2["status"], "duplicate_skipped")

        # Outcome verified audit event should be logged exactly once
        outcome_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.OUTCOME_VERIFIED
        ]
        self.assertEqual(len(outcome_events), 1)

    def test_action_mismatch_detected_and_escalated(self) -> None:
        """Safety Gate: Webhook event type incompatible with execution action triggers MISMATCH."""
        plink_id = "plink_test_mismatch"
        idemp_key = "idemp_recon_mismatch"
        # Recorded execution was NOTIFY_MERCHANT, not CREATE_PAYMENT_LINK
        self._record_sample_execution(
            provider_ref=plink_id,
            idempotency_key=idemp_key,
            action=IntentAction.NOTIFY_MERCHANT,
        )

        payload = {
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": plink_id,
                        "amount": 50000,
                        "status": "paid",
                        "reference_id": idemp_key,
                    }
                }
            },
        }

        raw_bytes, sig = self._sign_payload(payload)
        status_code, resp = self.service.handle_webhook(raw_bytes, sig)

        self.assertEqual(status_code, 200)
        self.assertEqual(resp["reconciliation"]["status"], "mismatch")
        self.assertIn("Action mismatch", resp["reconciliation"]["mismatch_reason"])

        # Audit should record ESCALATED event for the mismatch
        escalation_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.ESCALATED
        ]
        self.assertEqual(len(escalation_events), 1)
        self.assertIn("Action mismatch", escalation_events[0].summary)

    def test_unknown_execution_processed_as_general_telemetry(self) -> None:
        """General ambient payment webhooks without matching execution are ingested as telemetry."""
        payload = {
            "entity": "event",
            "account_id": "acc_ambient_merchant",
            "event": "payment.captured",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_ambient_999",
                        "amount": 15000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "card",
                        "created_at": 1700000000,
                    }
                }
            },
        }

        raw_bytes, sig = self._sign_payload(payload)
        status_code, resp = self.service.handle_webhook(raw_bytes, sig)

        self.assertEqual(status_code, 200)
        self.assertEqual(resp["status"], "processed")
        self.assertEqual(resp["reconciliation"]["status"], "unknown_execution")

        # Payment is stored in DB
        saved_payment = self.db.get_payment("pay_ambient_999")
        self.assertIsNotNone(saved_payment)
        self.assertEqual(saved_payment.amount.minor_units, 15000)

        # Ingestion audit event recorded
        ingested_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.FACT_INGESTED
        ]
        self.assertEqual(len(ingested_events), 1)


if __name__ == "__main__":
    unittest.main()
