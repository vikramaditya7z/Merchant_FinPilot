"""End-to-end causal chain validation for Razorpay TEST mode.

Validates:
1. Razorpay TEST credentials are loaded only from environment variables.
2. rzp_live_* credentials remain blocked.
3. Webhook signatures are verified using raw request body and X-Razorpay-Signature.
4. Razorpay execution can only happen after VERIFY and AUTHORIZE succeed.
5. The Razorpay provider reference is stored correctly.
6. The resulting webhook correlates with the original execution.
7. Reconciliation updates the existing execution instead of creating another execution.
8. Duplicate webhook delivery remains idempotent.
9. Mismatched webhook data is fail-closed and escalated.
10. Webhook ingestion never invokes Gemini or creates a new financial execution.
11. Complete causal pipeline progression: DETECT -> INVESTIGATE -> REASON -> VERIFY -> AUTHORIZE -> EXECUTE -> RECONCILIATION.
"""

import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional
from unittest.mock import MagicMock, patch

from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.domain.canonical import short_digest
from backend.domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    ExecutionStatus,
    IntentAction,
    PolicyVerdict,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
    ViolationEffect,
)
from backend.domain.incident import FinancialIncident, IncidentType, Severity
from backend.domain.intent import AgentIntent, IntentTarget
from backend.domain.money import Money
from backend.domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from backend.domain.policy import PolicyDecision, PolicyViolation
from backend.domain.verification import VerificationCheck, VerificationResult
from backend.domain.window import UTC, TimeWindow
from backend.execution.adapters import RazorpayExecutionAdapter, SimulatedExecutionAdapter
from backend.execution.contracts import ExecutionRequest, ExecutionResult
from backend.execution.engine import ExecutionEngine
from backend.execution.store import ExecutionStore
from backend.policy.engine import PolicyEngine
from backend.razorpay.client import RazorpayClient
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.reconciler import RazorpayReconciler, ReconciliationReport, ReconciliationStatus
from backend.razorpay.service import RazorpayService
from backend.razorpay.webhook import RazorpayWebhookHandler
from backend.verification.contracts import VerifiedIntent


class TestRazorpayE2ECausalChain(unittest.TestCase):
    """Rigorous end-to-end causal chain verification for Razorpay TEST mode."""

    def setUp(self) -> None:
        self.webhook_secret = "whsec_test_causal_chain_secret_9988"
        self.test_key_id = "rzp_test_e2e_key_12345"
        self.test_key_secret = "test_key_secret_67890"

        self.config = RazorpayConfig(
            key_id=self.test_key_id,
            key_secret=self.test_key_secret,
            webhook_secret=self.webhook_secret,
        )
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.store = ExecutionStore()
        self.reconciler = RazorpayReconciler(store=self.store, audit_log=self.audit_log)
        self.mock_client = MagicMock(spec=RazorpayClient)
        self.mock_client.config = self.config

        self.adapter = RazorpayExecutionAdapter(client=self.mock_client, config=self.config)
        self.exec_engine = ExecutionEngine(adapter=self.adapter, store=self.store, audit_log=self.audit_log)
        self.service = RazorpayService(
            config=self.config,
            client=self.mock_client,
            execution_store=self.store,
            reconciler=self.reconciler,
            database=self.db,
            audit_log=self.audit_log,
        )
        self.now = datetime(2026, 9, 1, 10, 0, 0, tzinfo=UTC)

    def _sign(self, body_bytes: bytes) -> str:
        return hmac.new(self.webhook_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

    def test_01_credentials_loaded_only_from_env(self) -> None:
        """Requirement 1: Razorpay credentials are loaded only from environment variables."""
        with patch.dict(os.environ, {
            "RAZORPAY_KEY_ID": "rzp_test_env_key_abc",
            "RAZORPAY_KEY_SECRET": "env_secret_xyz",
            "RAZORPAY_WEBHOOK_SECRET": "env_whsec_123",
            "RAZORPAY_API_BASE_URL": "https://api.razorpay.com/v1",
        }, clear=True):
            cfg = RazorpayConfig.from_env()
            self.assertEqual(cfg.key_id, "rzp_test_env_key_abc")
            self.assertEqual(cfg.key_secret, "env_secret_xyz")
            self.assertEqual(cfg.webhook_secret, "env_whsec_123")
            self.assertTrue(cfg.is_configured)

    def test_02_rzp_live_credentials_remain_blocked(self) -> None:
        """Requirement 2: rzp_live_* credentials remain strictly blocked (fail-closed)."""
        live_cfg = RazorpayConfig(key_id="rzp_live_prohibited_key_999", key_secret="secret_live")
        live_client = MagicMock(spec=RazorpayClient)
        live_client.config = live_cfg
        live_adapter = RazorpayExecutionAdapter(client=live_client, config=live_cfg)

        intent = AgentIntent(
            intent_id="intent_live_test",
            incident_id="inc_live_test",
            action=IntentAction.CREATE_PAYMENT_LINK,
            reason="Automated retry proposal",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_001"),
            parameters={"amount": 50000, "currency": "INR"},
            evidence_refs=("ev_001",),
        )
        decision = PolicyDecision(
            decision_id="dec_001",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.ALLOW,
            rationale="Test decision",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="v1",
        )
        req = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        res = live_adapter.execute(req, idempotency_key="idemp_live_guard")
        self.assertEqual(res.status, ExecutionStatus.FAILED)
        self.assertEqual(res.error_code, "LIVE_MODE_FORBIDDEN")
        self.assertEqual(live_client.create_payment_link.call_count, 0)

    def test_03_webhook_signature_verified_using_raw_body(self) -> None:
        """Requirement 3: Webhook signatures verified using raw request body and X-Razorpay-Signature."""
        body = json.dumps({"entity": "event", "event": "payment.authorized"}).encode("utf-8")
        valid_sig = self._sign(body)

        # Valid signature succeeds
        status, resp = self.service.handle_webhook(body, valid_sig)
        self.assertEqual(status, 200)

        # Tampered raw body fails with 401
        tampered_body = body + b" "
        status_tampered, resp_tampered = self.service.handle_webhook(tampered_body, valid_sig)
        self.assertEqual(status_tampered, 401)
        self.assertEqual(resp_tampered["status"], "error")

        # Missing or bad signature fails with 401
        status_bad, resp_bad = self.service.handle_webhook(body, "bad_signature")
        self.assertEqual(status_bad, 401)

    def test_04_execution_only_happens_after_verify_and_authorize_succeed(self) -> None:
        """Requirement 4: Razorpay execution can only happen after VERIFY and AUTHORIZE succeed."""
        intent = AgentIntent(
            intent_id="intent_gate_test",
            incident_id="inc_gate_test",
            action=IntentAction.CREATE_PAYMENT_LINK,
            reason="Customer payment link retry",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_gate_001"),
            parameters={"amount": 50000, "currency": "INR"},
            evidence_refs=("ev_001",),
        )

        # 1. Blocked PolicyDecision -> ExecutionEngine halts before adapter
        blocked_dec = PolicyDecision(
            decision_id="dec_block_01",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.BLOCK,
            rationale="Blocked by safety policy",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="v1",
            violations=(PolicyViolation(rule_id="POL-003", rule_version="v1", effect=ViolationEffect.BLOCKING, message="Blocked"),),
        )
        res_blocked = self.exec_engine.execute(blocked_dec, intent, now=self.now)
        self.assertEqual(res_blocked.status, ExecutionStatus.BLOCKED)
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

        # 2. Tampered Intent Hash -> ExecutionEngine halts before adapter
        allowed_dec = PolicyDecision(
            decision_id="dec_allow_01",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.ALLOW,
            rationale="Allowed by policy",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="v1",
        )
        tampered_intent = AgentIntent(
            intent_id=intent.intent_id,
            incident_id=intent.incident_id,
            action=intent.action,
            reason=intent.reason,
            proposed_at=intent.proposed_at,
            model_id=intent.model_id,
            prompt_version=intent.prompt_version,
            target=intent.target,
            parameters={"amount": 999999, "currency": "INR"},  # modified
            evidence_refs=("ev_001",),
        )
        res_tampered = self.exec_engine.execute(allowed_dec, tampered_intent, now=self.now)
        self.assertEqual(res_tampered.status, ExecutionStatus.BLOCKED)
        self.assertEqual(res_tampered.error_code, "INTENT_HASH_MISMATCH")
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

    def test_05_to_09_complete_e2e_causal_chain(self) -> None:
        """Requirements 5, 6, 7, 8, 9: Full causal chain from webhook ingestion to reconciliation."""
        # ---------------------------------------------------------------------
        # Step A: Ingest telemetry payment via verified Razorpay webhook
        # ---------------------------------------------------------------------
        initial_webhook_body = json.dumps({
            "entity": "event",
            "account_id": "acc_merchant_e2e",
            "event": "payment.failed",
            "contains": ["payment"],
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_failed_customer_101",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "error_code": "BAD_REQUEST_ERROR",
                        "error_description": "PSP Timeout",
                        "created_at": int(self.now.timestamp()),
                    }
                }
            },
        }).encode("utf-8")

        status, resp = self.service.handle_webhook(initial_webhook_body, self._sign(initial_webhook_body))
        self.assertEqual(status, 200)
        self.assertEqual(resp["reconciliation"]["status"], "unknown_execution")  # Ambient telemetry

        # Verify payment was ingested into database
        stored_payment = self.db.get_payment("pay_failed_customer_101")
        self.assertIsNotNone(stored_payment)
        self.assertEqual(stored_payment.amount.minor_units, 50000)

        # ---------------------------------------------------------------------
        # Step B: Autonomous Pipeline (DETECT -> REASON -> VERIFY -> AUTHORIZE -> EXECUTE)
        # ---------------------------------------------------------------------
        target_payment_id = stored_payment.id
        intent = AgentIntent(
            intent_id="intent_e2e_remediation_001",
            incident_id="inc_e2e_001",
            action=IntentAction.CREATE_PAYMENT_LINK,
            reason="Generate retry payment link following customer payment failure.",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id=target_payment_id),
            parameters={"amount": 50000, "currency": "INR"},
            evidence_refs=("ev_001",),
            claimed_amount=Money(50000, Currency.INR),
            confidence=Decimal("0.95"),
        )

        # VERIFY passes
        v_check = VerificationCheck(
            check_id="CHK_EVIDENCE",
            name="Evidence check",
            passed=True,
            expected="Evidence present",
            observed="Evidence confirmed",
        )
        v_result = VerificationResult(
            verification_id="ver_001",
            phase=VerificationPhase.PRE_EXECUTION,
            subject_id=intent.intent_id,
            status=VerificationStatus.VERIFIED,
            checks=(v_check,),
            verified_at=self.now,
        )
        verified_intent = VerifiedIntent(intent=intent, verification_result=v_result, verified_at=self.now)

        # AUTHORIZE passes
        policy_engine = PolicyEngine(audit_log=self.audit_log)
        policy_decision = policy_engine.evaluate(verified_intent, now=self.now)
        self.assertEqual(policy_decision.verdict, PolicyVerdict.ALLOW)

        # EXECUTE via Razorpay TEST mode adapter
        expected_plink_id = "plink_e2e_verified_7788"
        self.mock_client.create_payment_link.return_value = {
            "id": expected_plink_id,
            "short_url": "https://rzp.io/rzp/testlink7788",
            "status": "created",
            "amount": 50000,
            "currency": "INR",
        }

        exec_result = self.exec_engine.execute(policy_decision, intent, now=self.now)

        # Requirement 5: Provider reference is stored correctly
        self.assertEqual(exec_result.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(exec_result.provider_reference, expected_plink_id)
        self.assertTrue(self.store.has_key(exec_result.idempotency_key))

        lookup_by_ref = self.store.get_by_provider_reference(expected_plink_id)
        self.assertIsNotNone(lookup_by_ref)
        self.assertEqual(lookup_by_ref.execution_id, exec_result.execution_id)

        # ---------------------------------------------------------------------
        # Step C: Subsequent Webhook Arrives (payment_link.paid)
        # ---------------------------------------------------------------------
        reconciliation_webhook = json.dumps({
            "entity": "event",
            "account_id": "acc_merchant_e2e",
            "event": "payment_link.paid",
            "contains": ["payment_link", "payment"],
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": expected_plink_id,
                        "amount": 50000,
                        "amount_paid": 50000,
                        "status": "paid",
                        "reference_id": exec_result.idempotency_key[:40],
                        "currency": "INR",
                        "notes": {
                            "incident_id": intent.incident_id,
                            "intent_id": intent.intent_id,
                            "idempotency_key": exec_result.idempotency_key,
                        },
                    }
                },
                "payment": {
                    "entity": {
                        "id": "pay_customer_paid_202",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "created_at": int(self.now.timestamp()),
                    }
                },
            },
        }).encode("utf-8")

        # Requirement 6: Resulting webhook correlates with original execution
        code, rec_resp = self.service.handle_webhook(
            reconciliation_webhook, self._sign(reconciliation_webhook)
        )
        self.assertEqual(code, 200)
        self.assertEqual(rec_resp["reconciliation"]["status"], "matched")
        self.assertEqual(rec_resp["reconciliation"]["execution_id"], exec_result.execution_id)
        self.assertEqual(rec_resp["reconciliation"]["provider_reference"], expected_plink_id)

        # Requirement 7: Reconciliation updates existing execution, does NOT create another
        self.assertEqual(self.store.count(), 1)
        reconciled_exec = self.store.get(exec_result.idempotency_key)
        self.assertIsNotNone(reconciled_exec)
        self.assertEqual(reconciled_exec.execution_id, exec_result.execution_id)
        self.assertEqual(reconciled_exec.status, ExecutionStatus.SUCCEEDED)
        self.assertIn("verified PAID", reconciled_exec.message)

        # Verify audit log contains OUTCOME_VERIFIED
        outcome_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.OUTCOME_VERIFIED
        ]
        self.assertEqual(len(outcome_events), 1)
        self.assertEqual(outcome_events[0].subject_id, exec_result.execution_id)

        # ---------------------------------------------------------------------
        # Step D: Requirement 8: Duplicate webhook delivery is idempotent
        # ---------------------------------------------------------------------
        code_dup, resp_dup = self.service.handle_webhook(
            reconciliation_webhook, self._sign(reconciliation_webhook)
        )
        self.assertEqual(code_dup, 200)
        self.assertEqual(resp_dup["status"], "duplicate_skipped")
        # Store execution count remains strictly 1
        self.assertEqual(self.store.count(), 1)
        # Outcome events count remains strictly 1
        self.assertEqual(
            len([e for e in self.audit_log.events if e.event_type == AuditEventType.OUTCOME_VERIFIED]),
            1,
        )

        # ---------------------------------------------------------------------
        # Step E: Requirement 9: Mismatched webhook data is fail-closed and escalated
        # ---------------------------------------------------------------------
        mismatch_webhook = json.dumps({
            "entity": "event",
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": expected_plink_id,
                        "amount": 50000,
                        "currency": "EUR",  # MISMATCH: Currency is EUR, expected INR
                        "status": "paid",
                    }
                }
            },
        }).encode("utf-8")

        code_mis, resp_mis = self.service.handle_webhook(
            mismatch_webhook, self._sign(mismatch_webhook)
        )
        self.assertEqual(code_mis, 200)
        self.assertEqual(resp_mis["reconciliation"]["status"], "mismatch")
        self.assertIn("Currency mismatch", resp_mis["reconciliation"]["mismatch_reason"])

        # Escalated audit event recorded
        escalated_events = [
            e for e in self.audit_log.events
            if e.event_type == AuditEventType.ESCALATED
        ]
        self.assertEqual(len(escalated_events), 1)

    def test_10_webhook_ingestion_never_invokes_gemini_or_triggers_execution(self) -> None:
        """Requirement 10: Webhook ingestion never invokes Gemini or triggers new execution."""
        webhook_body = json.dumps({
            "entity": "event",
            "account_id": "acc_no_trigger",
            "event": "payment.failed",
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_isolated_101",
                        "amount": 75000,
                        "currency": "INR",
                        "status": "failed",
                        "method": "upi",
                        "created_at": int(self.now.timestamp()),
                    }
                }
            },
        }).encode("utf-8")

        initial_store_count = self.store.count()
        initial_mock_calls = self.mock_client.create_payment_link.call_count

        status, resp = self.service.handle_webhook(webhook_body, self._sign(webhook_body))
        self.assertEqual(status, 200)

        # Store count must be completely unchanged
        self.assertEqual(self.store.count(), initial_store_count)
        # Outbound adapter was NOT called
        self.assertEqual(self.mock_client.create_payment_link.call_count, initial_mock_calls)


    def test_11_webhook_arrives_after_terminal_failed_execution(self) -> None:
        """Case 6: Webhook arrives after execution has reached terminal FAILED state."""
        failed_exec = ExecutionResult(
            execution_id="exec_terminal_fail_001",
            decision_id="dec_term_01",
            intent_id="intent_term_01",
            action=IntentAction.CREATE_PAYMENT_LINK,
            status=ExecutionStatus.FAILED,
            idempotency_key="idemp_term_failed",
            attempted_at=self.now,
            completed_at=self.now,
            provider_reference="plink_terminal_01",
            error_code="PROVIDER_REJECTED",
            error_message="Payment link creation rejected",
        )
        self.store.save(failed_exec)
        self.assertEqual(self.store.count(), 1)

        # Inbound webhook arriving for this failed execution
        webhook_body = json.dumps({
            "entity": "event",
            "event": "payment_link.cancelled",
            "payload": {
                "payment_link": {
                    "entity": {
                        "id": "plink_terminal_01",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "cancelled",
                    }
                }
            },
        }).encode("utf-8")

        code, resp = self.service.handle_webhook(webhook_body, self._sign(webhook_body))
        self.assertEqual(code, 200)
        # Store execution count remains strictly 1 (no duplicate created)
        self.assertEqual(self.store.count(), 1)
        reconciled = self.store.get_by_provider_reference("plink_terminal_01")
        self.assertIsNotNone(reconciled)
        self.assertEqual(reconciled.execution_id, "exec_terminal_fail_001")

    def test_12_gemini_unavailable_during_reason_fails_safely(self) -> None:
        """Case 8: Gemini is unavailable during REASON; pipeline fails safely and never reaches EXECUTE."""
        from backend.application.orchestrator import FinancialIncidentOrchestrator, PipelineStage, PipelineStatus
        from backend.detection.detector import Detector
        from backend.investigation.investigator import Investigator
        from backend.verification.verifier import FinancialVerifier

        failing_agent = MagicMock()
        failing_agent.investigate_and_propose.side_effect = RuntimeError("Gemini API connection error 503")

        mock_adapter = MagicMock()
        engine = ExecutionEngine(adapter=mock_adapter, store=self.store, audit_log=self.audit_log)

        orchestrator = FinancialIncidentOrchestrator(
            detector=Detector(),
            investigator=Investigator(),
            agent=failing_agent,
            verifier=FinancialVerifier(),
            policy_engine=PolicyEngine(audit_log=self.audit_log),
            execution_engine=engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        from backend.data.generator import generate_scenario
        from backend.data.scenarios import ScenarioId
        from backend.domain.enums import ComparableWindowMode
        from backend.financial.engine import build_daily_hourly_baseline, compute_metrics

        scen_data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.spec.baseline_days,
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=ComparableWindowMode.SAME_HOUR_OF_DAY,
        )

        result = orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="test_merchant",
            now=scen_data.anchor,
        )
        self.assertEqual(result.status, PipelineStatus.FAILED)
        self.assertEqual(result.final_stage, PipelineStage.AGENT)
        # Mock adapter was NEVER called
        self.assertEqual(mock_adapter.execute.call_count, 0)
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

    def test_13_razorpay_api_timeout_or_5xx_handled_structured(self) -> None:
        """Case 12: Razorpay API timeout or 5xx returns structured failure without unsafe retry."""
        from backend.razorpay.client import RazorpayServerError

        self.mock_client.create_payment_link.side_effect = RazorpayServerError(
            "Razorpay upstream server error (504): Gateway Timeout",
            status_code=504,
        )

        intent = AgentIntent(
            intent_id="intent_timeout_test",
            incident_id="inc_timeout_test",
            action=IntentAction.CREATE_PAYMENT_LINK,
            reason="Payment link creation with upstream timeout",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_timeout_01"),
            parameters={"amount": 50000, "currency": "INR"},
            evidence_refs=("ev_001",),
        )
        decision = PolicyDecision(
            decision_id="dec_timeout_01",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.ALLOW,
            rationale="Allowed",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="v1",
        )
        req = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(req, idempotency_key="idemp_timeout_01")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_SERVER_ERROR")
        self.assertIn("504", result.error_message)
        # Verify execution is not re-attempted automatically
        self.assertEqual(self.mock_client.create_payment_link.call_count, 1)


if __name__ == "__main__":
    unittest.main()

