"""Unit and safety tests for RazorpayExecutionAdapter in TEST mode.

PROJECT_RULES 1.4, 6.4, 7.1, 7.5, 10.8, 10.9 / ARCHITECTURE.md §12, §13.
"""

import json
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from backend.audit.store import AuditLog
from backend.domain.canonical import digest, short_digest
from backend.domain.enums import (
    Currency,
    ExecutionStatus,
    IntentAction,
    PolicyVerdict,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
    ViolationEffect,
)
from backend.domain.intent import AgentIntent, IntentTarget
from backend.domain.money import Money
from backend.domain.policy import PolicyDecision, PolicyViolation
from backend.domain.verification import VerificationCheck, VerificationResult
from backend.domain.window import UTC
from backend.execution.adapters import SimulatedExecutionAdapter
from backend.execution.contracts import ExecutionRequest, ExecutionResult
from backend.execution.engine import ExecutionEngine
from backend.execution.store import ExecutionStore
from backend.policy.engine import PolicyEngine
from backend.razorpay.adapter import RazorpayExecutionAdapter
from backend.razorpay.client import (
    RazorpayAPIError,
    RazorpayAuthError,
    RazorpayClient,
    RazorpayConnectionError,
    RazorpayNotFoundError,
    RazorpayServerError,
    RazorpayTimeoutError,
)
from backend.razorpay.config import RazorpayConfig
from backend.verification.contracts import VerifiedIntent


class TestRazorpayExecutionAdapter(unittest.TestCase):
    """Comprehensive test suite for Razorpay Execution Adapter safety invariants and execution."""

    def setUp(self) -> None:
        self.secret_key = "rzp_test_secret_9988"
        self.config = RazorpayConfig(
            key_id="rzp_test_key_12345",
            key_secret=self.secret_key,
            webhook_secret="whsec_123",
            api_base_url="https://api.razorpay.com/v1",
        )
        self.mock_client = MagicMock(spec=RazorpayClient)
        self.mock_client.config = self.config
        self.adapter = RazorpayExecutionAdapter(client=self.mock_client, config=self.config)
        self.now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def _create_sample_intent(
        self,
        action: IntentAction = IntentAction.CREATE_PAYMENT_LINK,
        amount_paise: int = 50000,
    ) -> AgentIntent:
        return AgentIntent(
            intent_id="intent_test_001",
            incident_id="inc_test_001",
            action=action,
            reason="Generate payment link for customer retry following PSP failure.",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_failed_101"),
            parameters={"amount": amount_paise, "currency": "INR"},
            evidence_refs=("ev_001",),
            claimed_amount=Money(amount_paise, Currency.INR),
            confidence=Decimal("0.95"),
        )

    def _create_policy_decision(
        self,
        intent: AgentIntent,
        verdict: PolicyVerdict = PolicyVerdict.ALLOW,
    ) -> PolicyDecision:
        return PolicyDecision(
            decision_id="dec_test_001",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=verdict,
            rationale="Authorized by deterministic policy engine.",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="pol-v1",
            violations=(),
        )

    def test_live_keys_forbidden_in_adapter(self) -> None:
        """Safety Gate: Live Razorpay credentials ('rzp_live_') MUST be rejected immediately."""
        live_config = RazorpayConfig(
            key_id="rzp_live_dangerous_key",
            key_secret="live_secret",
        )
        live_client = MagicMock(spec=RazorpayClient)
        live_client.config = live_config
        live_adapter = RazorpayExecutionAdapter(client=live_client, config=live_config)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = live_adapter.execute(request, idempotency_key="idemp_live_test")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "LIVE_MODE_FORBIDDEN")
        self.assertIn("strictly forbidden", result.error_message)
        # Verify no external call was made
        self.assertEqual(live_client.create_payment_link.call_count, 0)

    def test_missing_credentials_fails_closed(self) -> None:
        """Safety Gate: Unconfigured credentials must return structured FAILED without crashing."""
        empty_config = RazorpayConfig()
        empty_client = MagicMock(spec=RazorpayClient)
        empty_client.config = empty_config
        empty_adapter = RazorpayExecutionAdapter(client=empty_client, config=empty_config)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = empty_adapter.execute(request, idempotency_key="idemp_empty_test")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_NOT_CONFIGURED")

    def test_successful_test_payment_link_creation(self) -> None:
        """Verify successful payment link creation in TEST mode returns structured receipt."""
        self.mock_client.create_payment_link.return_value = {
            "id": "plink_test_7788",
            "short_url": "https://rzp.io/i/testlink7788",
            "status": "created",
            "amount": 50000,
            "currency": "INR",
        }

        intent = self._create_sample_intent(action=IntentAction.CREATE_PAYMENT_LINK, amount_paise=50000)
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_success_001")

        self.assertEqual(result.status, ExecutionStatus.SUCCEEDED)
        self.assertTrue(result.is_simulation)
        self.assertEqual(result.provider_reference, "plink_test_7788")
        self.assertIsNotNone(result.response_digest)
        self.assertIn("plink_test_7788", result.message)
        self.assertIn("https://rzp.io/i/testlink7788", result.message)

        # Check call arguments
        self.mock_client.create_payment_link.assert_called_once()
        kwargs = self.mock_client.create_payment_link.call_args[1]
        self.assertEqual(kwargs["amount_minor_units"], 50000)
        self.assertEqual(kwargs["currency"], "INR")

    def test_secrets_never_leak_in_results_or_messages(self) -> None:
        """Security: Razorpay secrets must never appear in any result or message."""
        self.mock_client.create_payment_link.return_value = {
            "id": "plink_safe_123",
            "short_url": "https://rzp.io/i/safe123",
        }

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_secret_test")

        result_str = str(result)
        self.assertNotIn(self.secret_key, result_str)
        self.assertNotIn(self.secret_key, result.message or "")
        self.assertNotIn(self.secret_key, result.response_digest or "")

    def test_notify_merchant_test_execution(self) -> None:
        """Verify NOTIFY_MERCHANT test action returns structured receipt."""
        intent = self._create_sample_intent(action=IntentAction.NOTIFY_MERCHANT)
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_notif_test")
        self.assertEqual(result.status, ExecutionStatus.SUCCEEDED)
        self.assertTrue(result.is_simulation)
        self.assertIn("rzp_test_notif_", result.provider_reference)
        self.assertIn("merchant notification recorded", result.message)

    def test_recommend_only_test_execution(self) -> None:
        """Verify RECOMMEND_ONLY test action returns structured receipt."""
        intent = self._create_sample_intent(action=IntentAction.RECOMMEND_ONLY)
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_rec_test")
        self.assertEqual(result.status, ExecutionStatus.SUCCEEDED)
        self.assertIn("rzp_test_rec_", result.provider_reference)

    def test_razorpay_auth_error_handling(self) -> None:
        """Verify RazorpayAuthError produces structured FAILED result."""
        self.mock_client.create_payment_link.side_effect = RazorpayAuthError("Invalid API credentials", status_code=401)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_auth_err")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_AUTH_ERROR")
        self.assertIn("Invalid API credentials", result.error_message)

    def test_razorpay_not_found_error_handling(self) -> None:
        """Verify RazorpayNotFoundError produces structured FAILED result."""
        self.mock_client.create_payment_link.side_effect = RazorpayNotFoundError("Resource not found", status_code=404)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_not_found")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_NOT_FOUND")

    def test_razorpay_api_error_handling(self) -> None:
        """Verify RazorpayAPIError produces structured FAILED result."""
        self.mock_client.create_payment_link.side_effect = RazorpayAPIError("Bad Request: invalid amount", status_code=400)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_api_err")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_API_ERROR")

    def test_razorpay_server_error_handling(self) -> None:
        """Verify RazorpayServerError produces structured FAILED result."""
        self.mock_client.create_payment_link.side_effect = RazorpayServerError("Gateway Timeout on Upstream", status_code=504)

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_server_err")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_SERVER_ERROR")

    def test_razorpay_timeout_error_handling(self) -> None:
        """Verify RazorpayTimeoutError produces structured UNKNOWN result (fail-safe)."""
        self.mock_client.create_payment_link.side_effect = RazorpayTimeoutError("HTTP request timed out after 10s")

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_timeout_err")
        # In financial architecture, timeout is UNKNOWN outcome (not retried)
        self.assertEqual(result.status, ExecutionStatus.UNKNOWN)
        self.assertEqual(result.error_code, "RAZORPAY_TIMEOUT")

    def test_razorpay_connection_error_handling(self) -> None:
        """Verify RazorpayConnectionError produces structured FAILED result."""
        self.mock_client.create_payment_link.side_effect = RazorpayConnectionError("DNS resolution failed")

        intent = self._create_sample_intent()
        decision = self._create_policy_decision(intent)
        request = ExecutionRequest(decision=decision, intent=intent, mode="test", requested_at=self.now)

        result = self.adapter.execute(request, idempotency_key="idemp_conn_err")
        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertEqual(result.error_code, "RAZORPAY_CONNECTION_ERROR")


class TestRazorpayExecutionSafetyGates(unittest.TestCase):
    """Test suite proving deterministic safety gates before Razorpay execution."""

    def setUp(self) -> None:
        self.audit_log = AuditLog()
        self.store = ExecutionStore()
        self.config = RazorpayConfig(
            key_id="rzp_test_key_abc",
            key_secret="secret_xyz",
        )
        self.mock_client = MagicMock(spec=RazorpayClient)
        self.mock_client.config = self.config
        self.adapter = RazorpayExecutionAdapter(client=self.mock_client, config=self.config)
        self.engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
        )
        self.now = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

    def _create_intent(self) -> AgentIntent:
        return AgentIntent(
            intent_id="intent_gate_001",
            incident_id="inc_gate_001",
            action=IntentAction.CREATE_PAYMENT_LINK,
            reason="Generate retry payment link following verified PSP timeout degradation.",
            proposed_at=self.now,
            model_id="gemini-3.1-flash-lite-preview",
            prompt_version="v2.0",
            target=IntentTarget(entity_type=TargetEntityType.PAYMENT, entity_id="pay_001"),
            parameters={"amount": 50000, "currency": "INR"},
            evidence_refs=("ev_001",),
            claimed_amount=Money(50000, Currency.INR),
            confidence=Decimal("0.95"),
        )

    def test_unverified_intent_blocked_by_policy(self) -> None:
        """Safety Gate: An unverified intent cannot form a VerifiedIntent or reach ExecutionEngine."""
        intent = self._create_intent()
        failed_v_result = VerificationResult(
            verification_id="ver_failed_001",
            phase=VerificationPhase.PRE_EXECUTION,
            subject_id=intent.intent_id,
            status=VerificationStatus.REJECTED,
            checks=(
                VerificationCheck(
                    check_id="CHK_EVIDENCE",
                    name="Evidence Resolution",
                    passed=False,
                    expected="Valid evidence ref",
                    observed="Unresolved reference",
                    detail="Evidence reference not found in incident pool.",
                ),
            ),
            summary="Verification REJECTED",
            verified_at=self.now,
        )

        # 1. VerifiedIntent structural invariant rejects unverified results
        from backend.domain.errors import DomainValidationError
        with self.assertRaises(DomainValidationError):
            VerifiedIntent(
                intent=intent,
                verification_result=failed_v_result,
                verified_at=self.now,
            )

        # 2. PolicyEngine rejects raw unverified intents
        policy_engine = PolicyEngine(audit_log=self.audit_log)
        with self.assertRaises(DomainValidationError):
            policy_engine.evaluate(intent, now=self.now)  # type: ignore

        # 3. ExecutionEngine rejects invalid/missing policy decisions
        invalid_decision = None
        result = self.engine.execute(invalid_decision, intent, now=self.now)  # type: ignore
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.error_code, "INVALID_DECISION")
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

    def test_blocked_policy_decision_cannot_reach_razorpay(self) -> None:
        """Safety Gate: A PolicyDecision with BLOCK cannot execute."""
        intent = self._create_intent()
        blocked_decision = PolicyDecision(
            decision_id="dec_blocked_001",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.BLOCK,
            rationale="Policy blocked by safety rule.",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="pol-v1",
            violations=(
                PolicyViolation(
                    rule_id="POL-003",
                    rule_version="pol-v1",
                    effect=ViolationEffect.BLOCKING,
                    message="Action blocked by policy.",
                ),
            ),
        )

        result = self.engine.execute(blocked_decision, intent, now=self.now)
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.error_code, "POLICY_NOT_ALLOWED")
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

    def test_intent_tampering_between_authorize_and_execute_blocked(self) -> None:
        """Safety Gate: If intent is tampered/altered after policy authorization, execution is BLOCKED."""
        original_intent = self._create_intent()
        authorized_decision = PolicyDecision(
            decision_id="dec_auth_001",
            intent_id=original_intent.intent_id,
            intent_hash=original_intent.content_hash(),
            verdict=PolicyVerdict.ALLOW,
            rationale="Policy allowed original intent.",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="pol-v1",
        )

        # Create tampered intent with modified amount (e.g. 50000 -> 99999)
        tampered_intent = AgentIntent(
            intent_id=original_intent.intent_id,
            incident_id=original_intent.incident_id,
            action=original_intent.action,
            reason=original_intent.reason,
            proposed_at=original_intent.proposed_at,
            model_id=original_intent.model_id,
            prompt_version=original_intent.prompt_version,
            target=original_intent.target,
            parameters={"amount": 99999, "currency": "INR"},
            evidence_refs=original_intent.evidence_refs,
            claimed_amount=Money(99999, Currency.INR),
            confidence=original_intent.confidence,
        )

        result = self.engine.execute(authorized_decision, tampered_intent, now=self.now)
        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.error_code, "INTENT_HASH_MISMATCH")
        self.assertIn("hash does not match", result.error_message)
        self.assertEqual(self.mock_client.create_payment_link.call_count, 0)

    def test_duplicate_execution_prevented_by_idempotency_ledger(self) -> None:
        """Idempotency: Re-executing the same authorized intent returns SKIPPED_DUPLICATE without calling Razorpay."""
        self.mock_client.create_payment_link.return_value = {
            "id": "plink_dedup_001",
            "short_url": "https://rzp.io/i/dedup001",
        }

        intent = self._create_intent()
        decision = PolicyDecision(
            decision_id="dec_auth_002",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.ALLOW,
            rationale="Policy allowed.",
            evaluated_at=self.now,
            expires_at=PolicyDecision.default_expiry(self.now, 300),
            rule_set_version="pol-v1",
        )

        # 1. First execution
        r1 = self.engine.execute(decision, intent, now=self.now)
        self.assertEqual(r1.status, ExecutionStatus.SUCCEEDED)
        self.assertEqual(self.mock_client.create_payment_link.call_count, 1)

        # 2. Duplicate execution
        r2 = self.engine.execute(decision, intent, now=self.now)
        self.assertEqual(r2.status, ExecutionStatus.SKIPPED_DUPLICATE)
        self.assertTrue(r2.is_duplicate)
        self.assertIn("Duplicate execution suppressed", r2.message)
        # Crucial: Razorpay was NOT called a second time
        self.assertEqual(self.mock_client.create_payment_link.call_count, 1)


if __name__ == "__main__":
    unittest.main()
