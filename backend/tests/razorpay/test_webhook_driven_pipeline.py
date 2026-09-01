"""Tests for Phase 1: Webhook-driven Autonomous Financial-Agent Pipeline.

Validates the full event-driven loop:
1. Razorpay payment.failed webhook
2. Raw-body HMAC verification
3. Deduplication
4. Payment & Trigger persistence in SQLite
5. Asynchronous background pipeline dispatch (non-blocking fast ACK)
6. Real context assembly & deterministic 11-scenario classification
7. Gemini investigation & AgentIntent generation
8. FinancialVerifier (12 checks) & PolicyEngine (10 rules) safety gating
9. Razorpay TEST execution creating authentic payment link
10. Webhook reconciliation verifying outcome to SUCCEEDED
11. Complete cryptographic audit trail
"""

import hmac
import hashlib
import json
import time
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional

from backend.agent.agent import FinancialAgent
from backend.agent.contracts import LLMMessage
from backend.agent.provider import MockLLMProvider
from backend.api.app import FinPilotApp
from backend.api.router import FinancialIncidentAPI
from backend.application.contracts import PipelineStatus
from backend.application.orchestrator import FinancialIncidentOrchestrator
from backend.application.trigger import BackgroundJobDispatcher, IncidentTrigger, TriggerStatus
from backend.audit.store import AuditLog
from backend.data.ground_truth import ScenarioId
from backend.db.database import Database
from backend.detection.detector import Detector
from backend.domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    IntentAction,
    PaymentMethod,
    PaymentStatus,
    PolicyVerdict,
    TargetEntityType,
)
from backend.domain.money import Money
from backend.domain.payment import EnrichedPayment, Payment
from backend.domain.window import UTC, TimeWindow
from backend.execution.adapters import RazorpayExecutionAdapter, SimulatedExecutionAdapter
from backend.execution.engine import ExecutionEngine
from backend.execution.store import ExecutionStore
from backend.investigation.classifier import ScenarioClassification, ScenarioClassifier
from backend.investigation.context import ContextAssembler
from backend.investigation.investigator import Investigator
from backend.policy.engine import PolicyEngine
from backend.razorpay.client import RazorpayClient
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.service import RazorpayService
from backend.tools.registry import create_default_registry
from backend.verification.verifier import FinancialVerifier


def compute_test_signature(body: bytes, secret: str = "rzp_webhook_secret_test_123") -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class MockRazorpayClient(RazorpayClient):
    """Offline test client producing authentic Razorpay entity responses."""

    def __init__(self, config: Optional[RazorpayConfig] = None) -> None:
        super().__init__(config=config or RazorpayConfig(
            key_id="rzp_test_mock_123",
            key_secret="mock_secret_abc",
            webhook_secret="rzp_webhook_secret_test_123",
        ))
        self.created_links: list[Dict[str, Any]] = []

    def create_payment_link(
        self,
        amount_minor_units: int,
        currency: str = "INR",
        description: str = "",
        reference_id: Optional[str] = None,
        customer: Optional[Dict[str, Any]] = None,
        notify: Optional[Dict[str, bool]] = None,
        notes: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        plink_id = f"plink_mock_{int(time.time() * 1000)}"
        resp = {
            "id": plink_id,
            "entity": "payment_link",
            "amount": amount_minor_units,
            "currency": currency,
            "status": "created",
            "short_url": f"https://rzp.io/rzp/mock_{plink_id}",
            "description": description or "FinPilot Automated Recovery",
            "customer": customer or {},
            "reference_id": reference_id or "",
            "notes": notes or {},
        }
        self.created_links.append(resp)
        return resp


class TestWebhookDrivenPipeline(unittest.TestCase):
    """Test suite for Phase 1 webhook-driven autonomous financial resolution."""

    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.exec_store = ExecutionStore()
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier(audit_log=self.audit_log)
        self.policy_engine = PolicyEngine(audit_log=self.audit_log)

        self.rzp_config = RazorpayConfig(
            key_id="rzp_test_dummy_key_123",
            key_secret="dummy_secret_456",
            webhook_secret="rzp_webhook_secret_test_123",
        )
        self.rzp_client = MockRazorpayClient(config=self.rzp_config)
        self.rzp_adapter = RazorpayExecutionAdapter(
            client=self.rzp_client,
            config=self.rzp_config,
        )
        self.execution_engine = ExecutionEngine(
            adapter=self.rzp_adapter,
            store=self.exec_store,
            audit_log=self.audit_log,
        )

        def mock_llm_handler(messages, schemas):
            inc_id = None
            for m in messages:
                content = m.content or ""
                if "Financial Incident '" in content:
                    inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()

            target_id = "test_merchant"
            target_type = TargetEntityType.MERCHANT.value
            action = IntentAction.NOTIFY_MERCHANT.value
            ev_refs = []
            claimed_amount_paise = None

            if inc_id:
                inc = self.db.get_incident(inc_id)
                if inc is not None:
                    target_id = inc.merchant_id or "test_merchant"
                    if inc.evidence:
                        ev_refs = [e.evidence_id for e in inc.evidence]
                        for ev in inc.evidence:
                            for word in ev.summary.split():
                                if word.startswith("pay_"):
                                    p = self.db.get_payment(word.strip(":,.;()"))
                                    if p is not None and p.is_failure:
                                        action = IntentAction.CREATE_PAYMENT_LINK.value
                                        target_type = TargetEntityType.PAYMENT.value
                                        target_id = p.id
                                        claimed_amount_paise = p.amount.minor_units
                                        break

            response = {
                "reasoning": f"Autonomous reasoning over incident '{inc_id}'. Evidence confirms payment failure degradation.",
                "verified_facts": ["Payment failure verified in active evaluation window."],
                "findings": [
                    {
                        "title": "Payment Failure Root Cause",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": ev_refs[0] if ev_refs else None,
                        "summary": "UPI failure requiring recovery link.",
                    }
                ],
                "uncertainty_or_limitations": ["Mock reasoning test environment."],
                "proposed_intent": {
                    "action": action,
                    "target_type": target_type,
                    "target_id": target_id,
                    "reason": f"Autonomous remediation warranted for {target_id}.",
                    "evidence_refs": ev_refs,
                    "parameters": {"amount": claimed_amount_paise or 50000, "currency": "INR"},
                    "claimed_amount_paise": claimed_amount_paise,
                    "confidence": "0.95",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(response)}\n```")

        mock_provider = MockLLMProvider(handler=mock_llm_handler)
        registry = create_default_registry()
        bound_tools = registry.bind(self.db)
        self.agent = FinancialAgent(
            provider=mock_provider,
            tools=bound_tools,
            audit_log=self.audit_log,
        )

        self.orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=self.agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        self.dispatcher = BackgroundJobDispatcher(max_workers=2)
        self.context_assembler = ContextAssembler(database=self.db)

        self.rzp_service = RazorpayService(
            config=self.rzp_config,
            client=self.rzp_client,
            database=self.db,
            audit_log=self.audit_log,
            execution_store=self.exec_store,
            orchestrator=self.orchestrator,
            dispatcher=self.dispatcher,
            context_assembler=self.context_assembler,
        )

        self.api = FinancialIncidentAPI(
            orchestrator=self.orchestrator,
            database=self.db,
            audit_log=self.audit_log,
            razorpay_service=self.rzp_service,
        )

    def tearDown(self) -> None:
        self.dispatcher.shutdown(wait=True)
        self.db.close()

    # -----------------------------------------------------------------------
    # 1. Webhook Fast ACK and Persistence
    # -----------------------------------------------------------------------

    def test_valid_payment_failed_webhook_creates_incident_and_returns_200_fast(self) -> None:
        """Valid payment.failed webhook responds < 200ms and persists trigger job."""
        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_failed_live_101",
                        "amount": 75000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Payment failed due to NPCI bank gateway failure",
                        "error_source": "bank",
                        "error_step": "payment_authorization",
                        "error_reason": "payment_failed",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        t0 = time.time()
        status_code, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
            merchant_id="merchant_alpha",
        )
        elapsed_ms = (time.time() - t0) * 1000

        self.assertEqual(status_code, 200)
        self.assertLess(elapsed_ms, 200.0, "Webhook ACK must be non-blocking and fast (< 200ms)")
        self.assertEqual(resp["status"], "processed")
        self.assertIn("job_id", resp)
        self.assertIn("incident_id", resp)

        # Verify payment is persisted in DB
        payment = self.db.get_payment("pay_failed_live_101")
        self.assertIsNotNone(payment)
        self.assertEqual(payment.status, PaymentStatus.FAILED)
        self.assertEqual(payment.amount.minor_units, 75000)

        # Verify trigger record exists in DB
        job = self.db.get_trigger(resp["job_id"])
        self.assertIsNotNone(job)
        self.assertEqual(job["merchant_id"], "merchant_alpha")
        self.assertEqual(job["payment_id"], "pay_failed_live_101")

    def test_invalid_signature_does_not_create_incident(self) -> None:
        """Forged or invalid HMAC signature returns 401 and stores nothing."""
        payload = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_forged_999", "amount": 50000, "status": "failed"}}}}
        body = json.dumps(payload).encode("utf-8")
        sig = "invalid_forged_signature_hex"

        status_code, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
        )
        self.assertEqual(status_code, 401)
        self.assertIsNone(self.db.get_payment("pay_forged_999"))
        self.assertEqual(len(self.db.list_triggers()), 0)

    def test_duplicate_webhook_does_not_create_duplicate_incident(self) -> None:
        """Duplicate webhook event is skipped and does not re-queue incident."""
        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_dedup_001",
                        "amount": 25000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "card",
                        "error_code": "AUTHENTICATION_FAILED",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        status1, resp1 = self.rzp_service.handle_webhook(raw_body=body, signature=sig)
        self.assertEqual(status1, 200)

        status2, resp2 = self.rzp_service.handle_webhook(raw_body=body, signature=sig)
        self.assertEqual(status2, 200)
        self.assertEqual(resp2["status"], "duplicate_skipped")

        triggers = self.db.list_triggers()
        self.assertEqual(len(triggers), 1, "Only one incident trigger must be created for duplicate events")

    # -----------------------------------------------------------------------
    # 2. Async Background Processing to Razorpay Execution
    # -----------------------------------------------------------------------

    def test_payment_failed_triggers_background_pipeline_to_execution(self) -> None:
        """payment.failed webhook automatically runs pipeline and creates payment link in TEST mode."""
        # Seed historical baseline and active window payments for merchant_gold to establish genuine failure spike context
        now_dt = datetime.now(timezone.utc)
        baseline_payments = []
        for i in range(20):
            p = Payment(
                id=f"pay_base_{i}",
                order_id=f"order_base_{i}",
                amount=Money(100000),
                status=PaymentStatus.CAPTURED,
                method=PaymentMethod.UPI,
                created_at=now_dt - timedelta(days=1, hours=i % 5),
            )
            baseline_payments.append(EnrichedPayment(payment=p))
        for i in range(3):
            p = Payment(
                id=f"pay_recent_fail_{i}",
                order_id=f"order_recent_{i}",
                amount=Money(120000),
                status=PaymentStatus.FAILED,
                method=PaymentMethod.UPI,
                error_code="GATEWAY_ERROR",
                error_description="UPI PSP issuer timeout",
                created_at=now_dt - timedelta(minutes=10 * i + 1),
            )
            baseline_payments.append(EnrichedPayment(payment=p))
        self.db.save_payments(baseline_payments, merchant_id="merchant_gold")

        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_auto_exec_123",
                        "amount": 120000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "error_code": "GATEWAY_ERROR",
                        "error_description": "UPI PSP issuer timeout",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        status, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
            merchant_id="merchant_gold",
        )
        self.assertEqual(status, 200)
        job_id = resp["job_id"]

        # Wait for background job to finish
        deadline = time.time() + 5.0
        job_completed = False
        while time.time() < deadline:
            job = self.db.get_trigger(job_id)
            if job and job["status"] == TriggerStatus.COMPLETED.value:
                job_completed = True
                break
            time.sleep(0.05)

        self.assertTrue(job_completed, "Background incident job must reach COMPLETED status")

        # Verify authentic Razorpay payment link was created via adapter
        self.assertEqual(len(self.rzp_client.created_links), 1)
        created = self.rzp_client.created_links[0]
        self.assertEqual(created["amount"], 120000)
        self.assertEqual(created["currency"], "INR")
        self.assertTrue(created["id"].startswith("plink_mock_"))

        # Verify audit trail contains complete causal chain
        events = self.audit_log.get_events()
        event_types = [e.event_type for e in events]
        self.assertIn(AuditEventType.FACT_INGESTED, event_types)
        self.assertIn(AuditEventType.INCIDENT_DETECTED, event_types)
        self.assertIn(AuditEventType.INVESTIGATION_STARTED, event_types)
        self.assertIn(AuditEventType.INTENT_VERIFIED, event_types)
        self.assertIn(AuditEventType.POLICY_DECIDED, event_types)
        self.assertIn(AuditEventType.ACTION_RESULT_RECORDED, event_types)

    # -----------------------------------------------------------------------
    # 3. Deterministic Scenario Classification Grounding
    # -----------------------------------------------------------------------

    def test_scenario_classifier_identifies_canonical_scenarios(self) -> None:
        """Classifier correctly categorizes UPI, Card, Risk-Blocked, and Normal."""
        classifier = ScenarioClassifier()
        now = datetime.now(timezone.utc)

        # 1. Normal
        p_ok = Payment(
            id="pay_norm_1",
            amount=Money(50000),
            status=PaymentStatus.CAPTURED,
            method=PaymentMethod.UPI,
            created_at=now,
        )
        c_ok = classifier.classify(p_ok)
        self.assertEqual(c_ok.scenario_id, ScenarioId.NORMAL)
        self.assertFalse(c_ok.is_incident)

        # 2. Risk Blocked (RECOVERY_NOT_ELIGIBLE - always wins on fraud/risk)
        p_risk = Payment(
            id="pay_risk_1",
            amount=Money(50000),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.CARD,
            error_code="RISK_BLOCKED",
            error_source="risk",
            error_description="Transaction flagged by fraud rules",
            created_at=now,
        )
        c_risk = classifier.classify(p_risk)
        self.assertEqual(c_risk.scenario_id, ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertTrue(c_risk.is_incident)
        self.assertFalse(c_risk.is_action_eligible)

        # 3. Fresh merchant / Cold-start isolated UPI failure -> INSUFFICIENT_DATA
        p_upi_isolated = Payment(
            id="pay_upi_iso_1",
            amount=Money(50000),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.UPI,
            error_code="BAD_REQUEST_ERROR",
            error_description="UPI issuer unavailable",
            created_at=now,
        )
        c_upi_iso = classifier.classify(p_upi_isolated)
        self.assertEqual(c_upi_iso.scenario_id, ScenarioId.INSUFFICIENT_DATA)
        self.assertFalse(c_upi_iso.is_incident)
        self.assertFalse(c_upi_iso.is_action_eligible)

        # 4. Fresh merchant / Cold-start isolated Card failure -> INSUFFICIENT_DATA
        p_card_isolated = Payment(
            id="pay_card_iso_1",
            amount=Money(50000),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.CARD,
            error_code="BAD_REQUEST_ERROR",
            error_description="3DS authentication failed",
            created_at=now,
        )
        c_card_iso = classifier.classify(p_card_isolated)
        self.assertEqual(c_card_iso.scenario_id, ScenarioId.INSUFFICIENT_DATA)
        self.assertFalse(c_card_iso.is_incident)
        self.assertFalse(c_card_iso.is_action_eligible)

        # 5. UPI Degradation with evidence (cluster of UPI failures)
        upi_recent = [
            Payment(id=f"pay_upi_rec_{i}", amount=Money(50000), status=PaymentStatus.FAILED, method=PaymentMethod.UPI, created_at=now)
            for i in range(4)
        ] + [
            Payment(id=f"pay_ok_rec_{i}", amount=Money(50000), status=PaymentStatus.CAPTURED, method=PaymentMethod.UPI, created_at=now)
            for i in range(2)
        ]
        c_upi = classifier.classify(p_upi_isolated, recent_payments=upi_recent)
        self.assertEqual(c_upi.scenario_id, ScenarioId.UPI_FAILURE_SPIKE)
        self.assertTrue(c_upi.is_incident)
        self.assertTrue(c_upi.is_action_eligible)

        # 6. Card Auth Degradation with evidence (cluster of Card failures)
        card_recent = [
            Payment(id=f"pay_card_rec_{i}", amount=Money(50000), status=PaymentStatus.FAILED, method=PaymentMethod.CARD, created_at=now)
            for i in range(4)
        ] + [
            Payment(id=f"pay_ok_card_rec_{i}", amount=Money(50000), status=PaymentStatus.CAPTURED, method=PaymentMethod.CARD, created_at=now)
            for i in range(2)
        ]
        c_card = classifier.classify(p_card_isolated, recent_payments=card_recent)
        self.assertEqual(c_card.scenario_id, ScenarioId.CARD_FAILURE_SPIKE)
        self.assertTrue(c_card.is_incident)
        self.assertTrue(c_card.is_action_eligible)

    def test_cold_start_isolated_upi_failure_webhook_classifies_insufficient_data(self) -> None:
        """Isolated UPI failure on a fresh merchant without historical baseline stops safely as INSUFFICIENT_DATA."""
        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_cold_upi_001",
                        "amount": 75000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "UPI bank server timeout",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        status, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
            merchant_id="merchant_cold_new",
        )
        self.assertEqual(status, 200)
        job_id = resp["job_id"]

        # Wait for background job
        deadline = time.time() + 5.0
        job_completed = False
        while time.time() < deadline:
            job = self.db.get_trigger(job_id)
            if job and job["status"] == TriggerStatus.COMPLETED.value:
                job_completed = True
                break
            time.sleep(0.05)

        self.assertTrue(job_completed)
        job = self.db.get_trigger(job_id)
        self.assertEqual(job["status"], TriggerStatus.COMPLETED.value)
        # Ensure no payment link mutation happened on un-evidenced isolated failure
        self.assertEqual(len(self.rzp_client.created_links), 0)

    def test_cold_start_isolated_card_failure_webhook_classifies_insufficient_data(self) -> None:
        """Isolated Card failure on a fresh merchant without historical baseline stops safely as INSUFFICIENT_DATA."""
        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_cold_card_001",
                        "amount": 90000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "card",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Card 3DS authentication failure",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        status, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
            merchant_id="merchant_cold_card_new",
        )
        self.assertEqual(status, 200)
        job_id = resp["job_id"]

        # Wait for background job
        deadline = time.time() + 5.0
        job_completed = False
        while time.time() < deadline:
            job = self.db.get_trigger(job_id)
            if job and job["status"] == TriggerStatus.COMPLETED.value:
                job_completed = True
                break
            time.sleep(0.05)

        self.assertTrue(job_completed)
        # Ensure no payment link created for isolated card failure
        self.assertEqual(len(self.rzp_client.created_links), 0)

    # -----------------------------------------------------------------------
    # 4. Safety Gates: Risk-Blocked Policy Protection
    # -----------------------------------------------------------------------

    def test_risk_blocked_failure_is_blocked_by_policy_gate(self) -> None:
        """When payment failure is risk-blocked, Policy Engine denies execution (POL-005)."""
        payload = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_fraud_blocked_001",
                        "amount": 99000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "card",
                        "error_code": "RISK_BLOCKED",
                        "error_source": "risk",
                        "error_description": "Card blacklisted by merchant fraud rules",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body = json.dumps(payload).encode("utf-8")
        sig = compute_test_signature(body)

        status, resp = self.rzp_service.handle_webhook(
            raw_body=body,
            signature=sig,
            merchant_id="merchant_secure",
        )
        self.assertEqual(status, 200)

        # Wait for worker
        time.sleep(0.5)

        # Razorpay API MUST NOT be called for fraud-blocked transaction
        self.assertEqual(len(self.rzp_client.created_links), 0)

    # -----------------------------------------------------------------------
    # 5. Subsequent Webhook Reconciliation
    # -----------------------------------------------------------------------

    def test_webhook_reconciliation_verifies_completed_outcome(self) -> None:
        """Subsequent payment_link.paid webhook correlates with execution and marks OUTCOME_VERIFIED."""
        # Seed baseline payments for merchant_default to establish failure spike context
        now_dt = datetime.now(timezone.utc)
        baseline_payments = [
            EnrichedPayment(
                payment=Payment(
                    id=f"pay_recon_base_{i}",
                    order_id=f"order_recon_base_{i}",
                    amount=Money(40000),
                    status=PaymentStatus.CAPTURED,
                    method=PaymentMethod.UPI,
                    created_at=now_dt - timedelta(days=1, hours=i % 5),
                )
            )
            for i in range(20)
        ] + [
            EnrichedPayment(
                payment=Payment(
                    id=f"pay_recon_fail_rec_{i}",
                    order_id=f"order_recon_fail_rec_{i}",
                    amount=Money(40000),
                    status=PaymentStatus.FAILED,
                    method=PaymentMethod.UPI,
                    error_code="BAD_REQUEST_ERROR",
                    error_description="Bank timeout",
                    created_at=now_dt - timedelta(minutes=10 * i + 1),
                )
            )
            for i in range(3)
        ]
        self.db.save_payments(baseline_payments, merchant_id="merchant_default")

        # 1. Ingest failed payment and let pipeline create link
        payload1 = {
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_recon_fail_01",
                        "amount": 40000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "Bank timeout",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body1 = json.dumps(payload1).encode("utf-8")
        self.rzp_service.handle_webhook(raw_body=body1, signature=compute_test_signature(body1))

        # Wait for execution
        time.sleep(0.5)
        self.assertEqual(len(self.rzp_client.created_links), 1)
        created_plink_id = self.rzp_client.created_links[0]["id"]

        # 2. Customer pays link -> Razorpay sends payment_link.paid webhook
        payload2 = {
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": created_plink_id,
                        "amount": 40000,
                        "status": "paid",
                        "notes": {"merchant_id": "merchant_default"},
                    }
                },
                "payment": {
                    "entity": {
                        "id": "pay_recon_paid_02",
                        "amount": 40000,
                        "status": "captured",
                        "method": "upi",
                        "created_at": int(datetime.now(timezone.utc).timestamp()),
                    }
                }
            }
        }
        body2 = json.dumps(payload2).encode("utf-8")
        status2, resp2 = self.rzp_service.handle_webhook(
            raw_body=body2,
            signature=compute_test_signature(body2),
        )

        self.assertEqual(status2, 200)
        self.assertIn("reconciliation", resp2)
        self.assertEqual(resp2["reconciliation"]["status"], "matched")
        self.assertEqual(resp2["reconciliation"]["reconciled_status"], "succeeded")

        # Verify audit log recorded OUTCOME_VERIFIED
        events = self.audit_log.get_events()
        verified_events = [e for e in events if e.event_type == AuditEventType.OUTCOME_VERIFIED]
        self.assertEqual(len(verified_events), 1)

    # -----------------------------------------------------------------------
    # 6. Incident Status API Endpoints
    # -----------------------------------------------------------------------

    def test_incident_job_api_endpoints(self) -> None:
        """API endpoints GET /api/v1/incidents/jobs and /jobs/{id} return job status."""
        trigger = IncidentTrigger.create(
            merchant_id="merchant_api_test",
            event_id="evt_test_api_001",
            event_type="payment.failed",
            payment_id="pay_test_api_001",
        )
        self.db.save_trigger(trigger.to_dict())

        # 1. List Jobs
        code1, body1 = self.api.handle_list_incident_jobs(merchant_id="merchant_api_test")
        self.assertEqual(code1, 200)
        self.assertEqual(body1["count"], 1)
        self.assertEqual(body1["jobs"][0]["job_id"], trigger.job_id)

        # 2. Get Job by ID
        code2, body2 = self.api.handle_get_incident_job(trigger.job_id)
        self.assertEqual(code2, 200)
        self.assertEqual(body2["job_id"], trigger.job_id)
        self.assertEqual(body2["payment_id"], "pay_test_api_001")

        # 3. Not Found
        code3, body3 = self.api.handle_get_incident_job("job_non_existent")
        self.assertEqual(code3, 404)

    # -----------------------------------------------------------------------
    # 7. Worker Error Handling Regression Test
    # -----------------------------------------------------------------------

    def test_worker_unexpected_exception_persists_failed_status_and_stops_pipeline_audit(self) -> None:
        """When worker encounters an unexpected exception, trigger is marked FAILED and PIPELINE_STOPPED is audited."""
        trigger = IncidentTrigger.create(
            merchant_id="merchant_err_test",
            event_id="evt_err_001",
            event_type="payment.failed",
            payment_id="pay_err_001",
        )
        self.db.save_trigger(trigger.to_dict())

        payment = Payment(
            id="pay_err_001",
            order_id="order_err_001",
            amount=Money(minor_units=500000, currency=Currency.INR),
            status=PaymentStatus.FAILED,
            method=PaymentMethod.UPI,
            created_at=datetime.now(timezone.utc),
            error_code="GATEWAY_ERROR",
            error_description="PSP gateway timeout",
        )

        # Force context_assembler to raise an unexpected RuntimeError
        original_assemble = self.rzp_service._context_assembler.assemble
        def faulty_assemble(*args, **kwargs):
            raise RuntimeError("Simulated upstream worker failure during context assembly")

        self.rzp_service._context_assembler.assemble = faulty_assemble
        try:
            # Execute worker directly
            self.rzp_service._process_incident_job(
                trigger=trigger,
                payment=payment,
                merchant_id="merchant_err_test",
            )
        finally:
            self.rzp_service._context_assembler.assemble = original_assemble

        # Verify trigger in DB is marked FAILED with error message and completed_at
        saved_trigger = self.db.get_trigger(trigger.job_id)
        self.assertIsNotNone(saved_trigger)
        self.assertEqual(saved_trigger["status"], TriggerStatus.FAILED.value)
        self.assertIn("Simulated upstream worker failure", saved_trigger["error_message"])
        self.assertIsNotNone(saved_trigger["completed_at"])

        # Verify audit log contains PIPELINE_STOPPED without raising AttributeError
        events = self.audit_log.get_events()
        stopped_events = [e for e in events if e.event_type == AuditEventType.PIPELINE_STOPPED]
        self.assertGreaterEqual(len(stopped_events), 1)
        last_stopped = stopped_events[-1]
        self.assertEqual(last_stopped.incident_id, trigger.incident_id)
        self.assertIn("Simulated upstream worker failure", last_stopped.payload["error"])

    # -----------------------------------------------------------------------
    # 8. Multi-Webhook Event Aggregation Regression Tests
    # -----------------------------------------------------------------------

    def test_closely_arriving_webhooks_aggregate_into_single_incident_window(self) -> None:
        """Multiple closely arriving payment.failed webhooks are queued and aggregated into the active incident window."""
        now_dt = datetime.now(timezone.utc)

        # 1. Seed 150 baseline payments across past 7 days (4% failure rate)
        baseline_payments = [
            EnrichedPayment(
                payment=Payment(
                    id=f"pay_agg_base_{i}",
                    amount=Money(50000),
                    status=PaymentStatus.FAILED if i < 6 else PaymentStatus.CAPTURED,
                    method=PaymentMethod.UPI,
                    created_at=now_dt - timedelta(days=1, hours=i % 24, minutes=i % 60),
                )
            )
            for i in range(150)
        ]
        self.db.save_payments(baseline_payments, merchant_id="merchant_agg_test")

        # 2. Dispatch 3 closely arriving payment.failed webhooks (within last 30 seconds up to now)
        job_ids = []
        for i in range(3):
            ts = int(now_dt.timestamp()) - (2 - i) * 5
            payload = {
                "event": "payment.failed",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_agg_fail_{i+1}",
                            "amount": 45000,
                            "currency": "INR",
                            "status": "failed",
                            "method": "upi",
                            "error_code": "BAD_REQUEST_ERROR",
                            "error_description": "UPI bank server timeout",
                            "created_at": ts,
                        }
                    }
                }
            }
            body = json.dumps(payload).encode("utf-8")
            sig = compute_test_signature(body)

            status, resp = self.rzp_service.handle_webhook(
                raw_body=body,
                signature=sig,
                merchant_id="merchant_agg_test",
            )
            self.assertEqual(status, 200)
            self.assertIn("job_id", resp)
            self.assertIn("incident_id", resp)
            self.assertEqual(resp["job_status"], "queued")
            job_ids.append(resp["job_id"])

        # 3. Wait for background worker to complete the jobs
        deadline = time.time() + 5.0
        target_job = job_ids[-1]
        completed = False
        while time.time() < deadline:
            job = self.db.get_trigger(target_job)
            if job and job["status"] in (TriggerStatus.COMPLETED.value, TriggerStatus.FAILED.value):
                completed = True
                break
            time.sleep(0.05)

        self.assertTrue(completed, "Target job must complete in background")

        # 4. Verify the investigation saw all 3 failure transactions
        final_job = self.db.get_trigger(target_job)
        self.assertEqual(final_job["status"], TriggerStatus.COMPLETED.value)
        pipe_res = json.loads(final_job["payload_json"])
        scen = pipe_res["scenario_classification"]

        self.assertEqual(scen["scenario_id"], "upi_failure_spike")
        self.assertTrue(scen["is_incident"])
        self.assertTrue(scen["is_action_eligible"])

        # 5. Verify payment link creation executed
        self.assertGreaterEqual(len(self.rzp_client.created_links), 1)

    def test_separate_merchants_do_not_merge_incidents(self) -> None:
        """Failures for different merchants remain isolated."""
        now_dt = datetime.now(timezone.utc)
        payload_a = {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {"id": "pay_iso_a", "amount": 25000, "status": "failed", "method": "upi", "error_code": "GATEWAY_ERROR", "created_at": int(now_dt.timestamp())}}}
        }
        payload_b = {
            "event": "payment.failed",
            "payload": {"payment": {"entity": {"id": "pay_iso_b", "amount": 35000, "status": "failed", "method": "card", "error_code": "GATEWAY_ERROR", "created_at": int(now_dt.timestamp())}}}
        }

        body_a = json.dumps(payload_a).encode("utf-8")
        body_b = json.dumps(payload_b).encode("utf-8")

        s_a, r_a = self.rzp_service.handle_webhook(raw_body=body_a, signature=compute_test_signature(body_a), merchant_id="merchant_aaa")
        s_b, r_b = self.rzp_service.handle_webhook(raw_body=body_b, signature=compute_test_signature(body_b), merchant_id="merchant_bbb")

        self.assertEqual(s_a, 200)
        self.assertEqual(s_b, 200)
        self.assertNotEqual(r_a["job_id"], r_b["job_id"])
        self.assertNotEqual(r_a["incident_id"], r_b["incident_id"])


if __name__ == "__main__":
    unittest.main()
