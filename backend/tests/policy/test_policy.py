"""Comprehensive tests for the PolicyEngine.

Verifies:
1. Valid verified intent within operational parameters -> ALLOW.
2. Kill switch active (FINPILOT_EXECUTION_ENABLED=false) -> BLOCK.
3. Non-test Razorpay mode (RAZORPAY_MODE='live') -> BLOCK.
4. Action outside allowlist -> BLOCK.
5. Risk-blocked incident -> BLOCK.
6. Amount exceeding per-action limit -> ESCALATE.
7. Low agent confidence -> ESCALATE.
8. Explicit ESCALATE_TO_HUMAN -> ESCALATE with approver roles.
9. Stale verified intent (age > TTL) -> BLOCK.
10. Decision TTL expiry and authorization checking.
11. Database immutability and zero execution.
12. Deterministic repeatability.
13. Audit log recording and cryptographic integrity.
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.canonical import digest, short_digest
from ...domain.enums import (
    AuditEventType,
    Currency,
    Dimension,
    IncidentStatus,
    IntentAction,
    PaymentStatus,
    PolicyVerdict,
    SourceConfidence,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
    ViolationEffect,
)
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.money import Money
from ...domain.policy import PolicyDecision, PolicyViolation
from ...domain.verification import VerificationCheck, VerificationResult
from ...domain.window import TimeWindow
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...policy.config import PolicyConfig
from ...policy.engine import PolicyEngine
from ...policy.rules import (
    RULE_ACTION_ALLOWLIST,
    RULE_AMOUNT_LIMIT,
    RULE_CONFIDENCE_FLOOR,
    RULE_HUMAN_ESCALATION,
    RULE_KILL_SWITCH,
    RULE_MODE_GUARD,
    RULE_RISK_BLOCKED_GUARD,
    RULE_VERIFICATION_STALE,
)
from ...verification.contracts import VerifiedIntent
from ...verification.verifier import FinancialVerifier
from ..helpers import NOW


class PolicyBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier()
        self.audit_log = AuditLog()
        self.engine = PolicyEngine(audit_log=self.audit_log)

    def tearDown(self):
        self.db.close()

    def _setup_verified_intent(
        self,
        scenario_id: ScenarioId,
        action: IntentAction = IntentAction.NOTIFY_MERCHANT,
        claimed_amount: Money = None,
        confidence: Decimal = Decimal("0.95"),
    ) -> Tuple[VerifiedIntent, datetime]:
        data = generate_scenario(scenario_id)
        self.db.save_payments(data.agent_enriched())

        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )

        incident = self.detector.detect(metrics, merchant_id="test_merchant")
        self.assertIsNotNone(incident)
        self.db.save_incident(incident)

        evidence_ref = (
            incident.evidence[0].evidence_id if incident.evidence else "ev_1"
        )
        if action == IntentAction.CREATE_PAYMENT_LINK:
            failed_p = next(
                p for p in data.incident_enriched()
                if p.payment.status == PaymentStatus.FAILED
            )
            target = IntentTarget(
                entity_type=TargetEntityType.PAYMENT,
                entity_id=failed_p.payment.id,
            )
        elif action != IntentAction.NO_ACTION:
            target = IntentTarget(
                entity_type=TargetEntityType.MERCHANT,
                entity_id="test_merchant",
            )
        else:
            target = None

        params = {"failure_category": "risk_blocked"} if scenario_id == ScenarioId.RECOVERY_NOT_ELIGIBLE else {}

        intent = AgentIntent(
            intent_id="intent_policy_test",
            incident_id=incident.incident_id,
            action=action,
            reason="Notifying merchant of verified payment degradation for policy evaluation.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=target,
            parameters=params,
            evidence_refs=(evidence_ref,) if action != IntentAction.NO_ACTION else (),
            claimed_amount=claimed_amount,
            confidence=confidence,
        )

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )
        self.assertIsNotNone(verified_intent)
        return verified_intent, data.anchor


class PolicyEngineTests(PolicyBaseTestCase):
    def test_valid_verified_intent_allow(self):
        """A valid verified intent within operational thresholds produces PolicyVerdict.ALLOW."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        decision = self.engine.evaluate(verified_intent, now=when)

        self.assertIsInstance(decision, PolicyDecision)
        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)
        self.assertTrue(decision.authorizes_execution)
        self.assertEqual(len(decision.violations), 0)
        self.assertEqual(decision.intent_id, verified_intent.intent_id)
        self.assertEqual(decision.intent_hash, verified_intent.content_hash)
        self.assertTrue(decision.is_valid_at(when))

        # Check audit event
        events = self.audit_log.get_events(event_type=AuditEventType.POLICY_DECIDED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].payload["verdict"], "allow")

    def test_kill_switch_blocks_consequential_action(self):
        """Execution kill switch (FINPILOT_EXECUTION_ENABLED=false) blocks all consequential actions."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        config = PolicyConfig(execution_enabled=False)
        engine = PolicyEngine(config=config)

        decision = engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertFalse(decision.authorizes_execution)
        self.assertEqual(len(decision.blocking_violations), 1)
        self.assertEqual(decision.blocking_violations[0].rule_id, RULE_KILL_SWITCH)

    def test_non_test_mode_blocks_action(self):
        """Non-test Razorpay mode (e.g. 'live') blocks action execution in current deployment."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        config = PolicyConfig(razorpay_mode="live")
        engine = PolicyEngine(config=config)

        decision = engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertEqual(decision.blocking_violations[0].rule_id, RULE_MODE_GUARD)

    def test_action_outside_allowlist_blocks(self):
        """Action not in configured allowlist produces PolicyVerdict.BLOCK."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        # Config only allows NO_ACTION
        config = PolicyConfig(allowed_actions=(IntentAction.NO_ACTION,))
        engine = PolicyEngine(config=config)

        decision = engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertEqual(decision.blocking_violations[0].rule_id, RULE_ACTION_ALLOWLIST)

    def test_high_exposure_amount_escalates(self):
        """Verified exposure exceeding per-action limit produces PolicyVerdict.ESCALATE."""
        verified_intent, when = self._setup_verified_intent(
            ScenarioId.UPI_FAILURE_SPIKE,
            claimed_amount=Money(50000, Currency.INR),
        )

        # Set action threshold (₹100) lower than claimed ₹500
        config = PolicyConfig(max_amount_per_action=Money(10000, Currency.INR))
        engine = PolicyEngine(config=config)

        decision = engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.ESCALATE)
        self.assertFalse(decision.authorizes_execution)
        self.assertGreater(len(decision.violations), 0)
        self.assertEqual(decision.violations[0].rule_id, RULE_AMOUNT_LIMIT)
        self.assertEqual(decision.required_approvals, config.escalation_approver_roles)

    def test_low_confidence_escalates(self):
        """Agent confidence below confidence_floor produces PolicyVerdict.ESCALATE."""
        verified_intent, when = self._setup_verified_intent(
            ScenarioId.UPI_FAILURE_SPIKE,
            confidence=Decimal("0.50"),  # below 0.70 default floor
        )

        decision = self.engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.ESCALATE)
        self.assertEqual(decision.violations[0].rule_id, RULE_CONFIDENCE_FLOOR)
        self.assertIn("finance_lead", decision.required_approvals)

    def test_explicit_human_escalation_request(self):
        """Agent proposing ESCALATE_TO_HUMAN produces PolicyVerdict.ESCALATE."""
        verified_intent, when = self._setup_verified_intent(
            ScenarioId.MULTIPLE_FAILURES,
            action=IntentAction.ESCALATE_TO_HUMAN,
        )

        decision = self.engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.ESCALATE)
        self.assertTrue(any(v.rule_id == RULE_HUMAN_ESCALATION for v in decision.violations))
        self.assertGreater(len(decision.required_approvals), 0)

    def test_stale_verified_intent_blocks(self):
        """VerifiedIntent older than decision_ttl_seconds is blocked as stale."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        stale_evaluation_time = when + timedelta(seconds=600)  # 10 minutes later (> 300s TTL)

        decision = self.engine.evaluate(verified_intent, now=stale_evaluation_time)

        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertEqual(decision.blocking_violations[0].rule_id, RULE_VERIFICATION_STALE)

    def test_decision_authorization_and_expiry_check(self):
        """PolicyDecision correctly authorizes execution within TTL and rejects after expiry."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        decision = self.engine.evaluate(verified_intent, now=when)
        self.assertTrue(decision.authorizes(verified_intent.content_hash, when))
        self.assertTrue(decision.authorizes(verified_intent.content_hash, when + timedelta(seconds=100)))

        # After expiry (5 minutes later)
        expired_time = when + timedelta(seconds=301)
        self.assertFalse(decision.authorizes(verified_intent.content_hash, expired_time))

        # Different hash
        self.assertFalse(decision.authorizes("different_hash", when))

    def test_policy_evaluation_is_pure_and_does_not_mutate_db(self):
        """Policy evaluation does not modify database state."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        initial_payments_count = len(self.db.list_payments())
        initial_incidents_count = len(self.db.list_incidents())

        self.engine.evaluate(verified_intent, now=when)

        self.assertEqual(len(self.db.list_payments()), initial_payments_count)
        self.assertEqual(len(self.db.list_incidents()), initial_incidents_count)

    def test_deterministic_repeatability(self):
        """Repeated policy evaluation on identical inputs produces identical results."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        dec1 = self.engine.evaluate(verified_intent, now=when)
        dec2 = self.engine.evaluate(verified_intent, now=when)

        self.assertEqual(dec1.verdict, dec2.verdict)
        self.assertEqual(dec1.decision_id, dec2.decision_id)
        self.assertEqual(dec1.intent_hash, dec2.intent_hash)
        self.assertEqual(len(dec1.violations), len(dec2.violations))

    def test_audit_trail_integrity_after_policy_decision(self):
        """Audit log verification succeeds after policy evaluation."""
        verified_intent, when = self._setup_verified_intent(ScenarioId.UPI_FAILURE_SPIKE)

        self.engine.evaluate(verified_intent, now=when)

        is_valid, errors = self.audit_log.verify_integrity()
        self.assertTrue(is_valid, f"Audit log verification failed: {errors}")

    def test_scenario_recovery_not_eligible_policy_blocks_retry(self):
        """RECOVERY_NOT_ELIGIBLE: Policy blocks payment link creation for risk-blocked transactions."""
        verified_intent, when = self._setup_verified_intent(
            ScenarioId.RECOVERY_NOT_ELIGIBLE,
            action=IntentAction.CREATE_PAYMENT_LINK,
        )

        decision = self.engine.evaluate(verified_intent, now=when)

        self.assertEqual(decision.verdict, PolicyVerdict.BLOCK)
        self.assertTrue(
            any(v.rule_id == RULE_RISK_BLOCKED_GUARD for v in decision.violations)
        )


if __name__ == "__main__":
    unittest.main()
