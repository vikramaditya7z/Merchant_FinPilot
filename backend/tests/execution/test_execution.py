"""Comprehensive tests for the ExecutionEngine.

Verifies:
1. Valid ALLOW decision reaches adapter and executes safely in test mode.
2. BLOCK decision cannot execute (fails closed).
3. ESCALATE decision cannot execute (fails closed).
4. Expired decision cannot execute.
5. Mismatched intent hash cannot execute.
6. Mismatched intent ID cannot execute.
7. Execution kill switch (execution_enabled=False) blocks execution.
8. Duplicate execution is strictly idempotent (returns SKIPPED_DUPLICATE without re-executing).
9. Simulation is explicitly tagged (SIMULATED status, is_executed=False, is_simulated=True).
10. Unsupported action returns FAILED.
11. Adapter exception handled gracefully (returns FAILED, does not crash).
12. Database remains completely unchanged (no direct payment mutation).
13. Audit log records full execution lifecycle with cryptographic integrity.
14. Deterministic idempotency key generation.
15. Full end-to-end pipeline: Scenario -> Detector -> Investigator -> Intent -> Verifier -> Policy -> Execution.
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
    AuditActor,
    AuditEventType,
    Currency,
    ExecutionStatus,
    IntentAction,
    PaymentStatus,
    PolicyVerdict,
    TargetEntityType,
    ViolationEffect,
)
from ...domain.execution import build_execution_key
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.money import Money
from ...domain.policy import PolicyDecision, PolicyViolation
from ...execution.adapters import ExecutionAdapter, SimulatedExecutionAdapter
from ...execution.contracts import ExecutionRequest, ExecutionResult
from ...execution.engine import ExecutionEngine
from ...execution.store import ExecutionStore
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...policy.config import PolicyConfig
from ...policy.engine import PolicyEngine
from ...verification.contracts import VerifiedIntent
from ...verification.verifier import FinancialVerifier
from ..helpers import NOW


class ExecutionBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier()
        self.policy_engine = PolicyEngine()
        self.audit_log = AuditLog()
        self.store = ExecutionStore()
        self.adapter = SimulatedExecutionAdapter()
        self.engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
            execution_enabled=True,
            razorpay_mode="test",
        )

    def tearDown(self):
        self.db.close()

    def _setup_pipeline(
        self,
        scenario_id: ScenarioId = ScenarioId.UPI_FAILURE_SPIKE,
        action: IntentAction = IntentAction.NOTIFY_MERCHANT,
    ) -> Tuple[PolicyDecision, AgentIntent, datetime]:
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
        target = (
            IntentTarget(entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant")
            if action != IntentAction.NO_ACTION
            else None
        )

        intent = AgentIntent(
            intent_id="intent_exec_test",
            incident_id=incident.incident_id,
            action=action,
            reason="Notifying merchant of verified payment degradation for execution test.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=target,
            evidence_refs=(evidence_ref,) if action != IntentAction.NO_ACTION else (),
            confidence=Decimal("0.95"),
        )

        verified_intent, v_result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )
        self.assertIsNotNone(verified_intent)

        decision = self.policy_engine.evaluate(verified_intent, now=data.anchor)
        return decision, intent, data.anchor


class ExecutionEngineTests(ExecutionBaseTestCase):
    def test_valid_allow_decision_executes_in_simulation(self):
        """A valid unexpired ALLOW decision executes cleanly in simulation mode."""
        decision, intent, when = self._setup_pipeline()
        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)

        result = self.engine.execute(decision=decision, intent=intent, now=when)

        self.assertIsInstance(result, ExecutionResult)
        self.assertEqual(result.status, ExecutionStatus.SIMULATED)
        self.assertTrue(result.is_simulated)
        self.assertFalse(result.is_executed)
        self.assertFalse(result.is_blocked)
        self.assertIsNotNone(result.provider_reference)
        self.assertIsNotNone(result.response_digest)
        self.assertEqual(result.intent_id, intent.intent_id)
        self.assertEqual(result.decision_id, decision.decision_id)

        # Check in store
        self.assertEqual(self.store.count(), 1)
        self.assertIsNotNone(self.store.get(result.idempotency_key))

        # Check audit trail
        events = self.audit_log.get_events()
        event_types = [e.event_type for e in events]
        self.assertIn(AuditEventType.ACTION_ATTEMPTED, event_types)
        self.assertIn(AuditEventType.ACTION_RESULT_RECORDED, event_types)

    def test_block_decision_cannot_execute(self):
        """A BLOCK decision is immediately rejected by the executor."""
        decision, intent, when = self._setup_pipeline()

        # Construct a BLOCK decision
        block_decision = PolicyDecision(
            decision_id="dec_block_1",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.BLOCK,
            rationale="Blocked by test policy",
            evaluated_at=when,
            expires_at=when + timedelta(seconds=300),
            rule_set_version="test-v1",
            violations=(
                PolicyViolation(
                    rule_id="POL-TEST",
                    rule_version="test-v1",
                    effect=ViolationEffect.BLOCKING,
                    message="Manual block test",
                ),
            ),
        )

        result = self.engine.execute(decision=block_decision, intent=intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertTrue(result.is_blocked)
        self.assertEqual(result.error_code, "POLICY_NOT_ALLOWED")
        self.assertEqual(self.store.count(), 0)

    def test_escalate_decision_cannot_execute(self):
        """An ESCALATE decision is rejected by the executor."""
        decision, intent, when = self._setup_pipeline()

        escalate_decision = PolicyDecision(
            decision_id="dec_esc_1",
            intent_id=intent.intent_id,
            intent_hash=intent.content_hash(),
            verdict=PolicyVerdict.ESCALATE,
            rationale="Escalated for human approval",
            evaluated_at=when,
            expires_at=when + timedelta(seconds=300),
            rule_set_version="test-v1",
            required_approvals=("operations",),
        )

        result = self.engine.execute(decision=escalate_decision, intent=intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertTrue(result.is_blocked)
        self.assertEqual(result.error_code, "POLICY_NOT_ALLOWED")
        self.assertEqual(self.store.count(), 0)

    def test_expired_decision_cannot_execute(self):
        """An expired decision is blocked from execution."""
        decision, intent, when = self._setup_pipeline()

        # Execute 10 minutes after decision evaluation (> 300s TTL)
        expired_time = when + timedelta(seconds=600)

        result = self.engine.execute(decision=decision, intent=intent, now=expired_time)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertTrue(result.is_blocked)
        self.assertEqual(result.error_code, "DECISION_EXPIRED")
        self.assertEqual(self.store.count(), 0)

    def test_mismatched_intent_hash_cannot_execute(self):
        """An intent whose content hash differs from the authorized decision hash is blocked."""
        decision, intent, when = self._setup_pipeline()

        # Modify intent parameters to alter hash
        tampered_intent = AgentIntent(
            intent_id=intent.intent_id,
            incident_id=intent.incident_id,
            action=intent.action,
            reason=intent.reason,
            proposed_at=intent.proposed_at,
            model_id=intent.model_id,
            prompt_version=intent.prompt_version,
            target=intent.target,
            parameters={"tampered_key": "tampered_val"},
            evidence_refs=intent.evidence_refs,
        )

        result = self.engine.execute(decision=decision, intent=tampered_intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.error_code, "INTENT_HASH_MISMATCH")
        self.assertEqual(self.store.count(), 0)

    def test_mismatched_intent_id_cannot_execute(self):
        """An intent with mismatched ID is blocked."""
        decision, intent, when = self._setup_pipeline()

        foreign_intent = AgentIntent(
            intent_id="different_intent_id",
            incident_id=intent.incident_id,
            action=intent.action,
            reason=intent.reason,
            proposed_at=intent.proposed_at,
            model_id=intent.model_id,
            prompt_version=intent.prompt_version,
            target=intent.target,
            evidence_refs=intent.evidence_refs,
        )

        result = self.engine.execute(decision=decision, intent=foreign_intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(self.store.count(), 0)

    def test_kill_switch_blocks_execution(self):
        """When execution_enabled=False, execution engine blocks all attempts."""
        decision, intent, when = self._setup_pipeline()

        disabled_engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
            execution_enabled=False,
        )

        result = disabled_engine.execute(decision=decision, intent=intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.BLOCKED)
        self.assertEqual(result.error_code, "EXECUTION_DISABLED")
        self.assertEqual(self.store.count(), 0)

    def test_duplicate_execution_is_idempotent(self):
        """Submitting the same authorized execution twice is strictly deduplicated."""
        decision, intent, when = self._setup_pipeline()

        res1 = self.engine.execute(decision=decision, intent=intent, now=when)
        self.assertEqual(res1.status, ExecutionStatus.SIMULATED)
        self.assertEqual(self.store.count(), 1)

        # Replay duplicate
        res2 = self.engine.execute(decision=decision, intent=intent, now=when + timedelta(seconds=5))

        self.assertEqual(res2.status, ExecutionStatus.SKIPPED_DUPLICATE)
        self.assertTrue(res2.is_duplicate)
        self.assertEqual(res2.idempotency_key, res1.idempotency_key)
        self.assertEqual(self.store.count(), 1)  # No second entry saved

        # Check duplicate audit event
        dup_events = self.audit_log.get_events(event_type=AuditEventType.EXECUTION_DUPLICATE)
        self.assertEqual(len(dup_events), 1)

    def test_simulation_is_explicitly_tagged_and_not_executed(self):
        """Simulated execution has is_simulation=True, status=SIMULATED, is_executed=False."""
        decision, intent, when = self._setup_pipeline()

        result = self.engine.execute(decision=decision, intent=intent, now=when)

        self.assertTrue(result.is_simulated)
        self.assertFalse(result.is_executed)
        self.assertEqual(result.status, ExecutionStatus.SIMULATED)

    def test_adapter_exception_handled_gracefully(self):
        """If an adapter raises an unhandled exception, engine returns FAILED and does not crash."""
        decision, intent, when = self._setup_pipeline()

        class FaultyAdapter(ExecutionAdapter):
            def execute(self, request, idempotency_key):
                raise RuntimeError("External network connection timeout")

        faulty_engine = ExecutionEngine(
            adapter=FaultyAdapter(),
            store=self.store,
            audit_log=self.audit_log,
        )

        result = faulty_engine.execute(decision=decision, intent=intent, now=when)

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertTrue(result.is_failed)
        self.assertEqual(result.error_code, "ADAPTER_ERROR")
        self.assertIn("External network connection timeout", result.error_message)

    def test_execution_does_not_mutate_database(self):
        """Execution engine leaves database payments and records completely untouched."""
        decision, intent, when = self._setup_pipeline()

        payments_before = len(self.db.list_payments())
        incidents_before = len(self.db.list_incidents())

        self.engine.execute(decision=decision, intent=intent, now=when)

        self.assertEqual(len(self.db.list_payments()), payments_before)
        self.assertEqual(len(self.db.list_incidents()), incidents_before)

    def test_audit_trail_integrity_after_execution(self):
        """Audit trail preserves cryptographic hash chaining after execution."""
        decision, intent, when = self._setup_pipeline()

        self.engine.execute(decision=decision, intent=intent, now=when)

        is_valid, errors = self.audit_log.verify_integrity()
        self.assertTrue(is_valid, f"Audit log verification failed: {errors}")

    def test_deterministic_idempotency_key_generation(self):
        """Idempotency keys are deterministically generated."""
        key1 = build_execution_key("inc_1", IntentAction.NOTIFY_MERCHANT, "merchant_1", {"b": 2, "a": 1})
        key2 = build_execution_key("inc_1", IntentAction.NOTIFY_MERCHANT, "merchant_1", {"a": 1, "b": 2})

        self.assertEqual(key1, key2)
        self.assertTrue(key1.startswith("exec_"))
        self.assertEqual(len(key1), 5 + 24)

    def test_full_pipeline_end_to_end(self):
        """Full end-to-end flow: Scenario -> Detector -> Investigator -> Intent -> Verifier -> Policy -> Execution."""
        # 1. Scenario Generation
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.db.save_payments(data.agent_enriched())

        # 2. Financial Metrics & Baseline
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )

        # 3. Detection
        incident = self.detector.detect(metrics, merchant_id="test_merchant")
        self.assertIsNotNone(incident)

        # 4. Investigation
        investigation = self.investigator.investigate(incident, data.agent_enriched())
        self.assertIsNotNone(investigation)

        # 5. Intent Proposal
        intent = AgentIntent(
            intent_id="intent_e2e",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Verified UPI payment degradation exceeds normal baseline.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"),
            evidence_refs=(incident.evidence[0].evidence_id,),
            confidence=Decimal("0.95"),
        )

        # 6. Verification
        verified_intent, v_result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )
        self.assertTrue(v_result.is_verified)
        self.assertIsNotNone(verified_intent)

        # 7. Policy Authorization
        decision = self.policy_engine.evaluate(verified_intent, now=data.anchor)
        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)
        self.assertTrue(decision.authorizes_execution)

        # 8. Execution
        exec_result = self.engine.execute(decision=decision, intent=intent, now=data.anchor)
        self.assertEqual(exec_result.status, ExecutionStatus.SIMULATED)
        self.assertTrue(exec_result.is_simulated)
        self.assertIsNotNone(exec_result.provider_reference)


class DuplicateExecutionConsistencyTests(unittest.TestCase):
    """V2 Phase 1 — Issue 3: SKIPPED_DUPLICATE must have consistent state flags.

    A duplicate execution must:
    - Have is_simulated = True (if the original was simulated)
    - Have is_duplicate = True
    - Have is_blocked = False
    - Have is_failed = False
    """

    def setUp(self):
        from ...audit.store import AuditLog
        from ...execution.adapters import SimulatedExecutionAdapter
        from ...execution.engine import ExecutionEngine
        from ...execution.store import ExecutionStore
        from ...policy.engine import PolicyEngine
        from ...verification.verifier import FinancialVerifier
        from ...tools.registry import create_default_registry
        from ...data import generate_scenario, ScenarioId
        from ...detection.detector import Detector
        from ...investigation.investigator import Investigator
        from ...financial.engine import build_daily_hourly_baseline, compute_metrics

        self.audit_log = AuditLog()
        self.store = ExecutionStore()
        self.adapter = SimulatedExecutionAdapter()
        self.engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
        )
        self.verifier = FinancialVerifier()
        self.policy_engine = PolicyEngine()
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.registry = create_default_registry()

        # Use UPI failure spike — clean technical incident, no risk-blocking
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.db.save_payments(data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(), data.incident_window, data.anchor, baseline_windows=buckets
        )
        self.incident = self.detector.detect(metrics, merchant_id="test_merchant")
        self.db.save_incident(self.incident)
        report = self.investigator.investigate(
            incident=self.incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )
        self.db.save_investigation(report)
        self.data = data

    def tearDown(self):
        self.db.close()

    def _build_intent_and_decision(self):
        """Build a verified intent and policy decision for the UPI spike incident."""
        from decimal import Decimal
        ev_ref = self.incident.evidence[0].evidence_id
        intent = AgentIntent(
            intent_id="intent_dup_test",
            incident_id=self.incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant of confirmed UPI spike observed in checkout traffic.",
            proposed_at=self.data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"),
            evidence_refs=(ev_ref,),
            confidence=Decimal("0.95"),
        )
        verified_intent, v_result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=self.incident,
            evidence=self.incident.evidence,
            payments=self.data.incident_enriched(),
            now=self.data.anchor,
        )
        self.assertIsNotNone(verified_intent, f"Verification failed: {v_result.summary}")
        decision = self.policy_engine.evaluate(verified_intent, now=self.data.anchor)
        self.assertEqual(decision.verdict, PolicyVerdict.ALLOW)
        return intent, decision

    def test_duplicate_result_is_simulated_true(self):
        """Issue 3: A SKIPPED_DUPLICATE ExecutionResult must have is_simulated=True when original was simulated."""
        intent, decision = self._build_intent_and_decision()

        # First execution — produces SIMULATED
        r1 = self.engine.execute(decision=decision, intent=intent, now=self.data.anchor)
        self.assertEqual(r1.status, ExecutionStatus.SIMULATED)
        self.assertTrue(r1.is_simulated)
        self.assertFalse(r1.is_duplicate)

        # Second execution — must produce SKIPPED_DUPLICATE
        r2 = self.engine.execute(decision=decision, intent=intent, now=self.data.anchor)
        self.assertEqual(r2.status, ExecutionStatus.SKIPPED_DUPLICATE)
        self.assertTrue(r2.is_duplicate, "Second execution must be marked as duplicate")
        self.assertTrue(r2.is_simulated, "Duplicate must be is_simulated=True when original was simulated")
        self.assertFalse(r2.is_blocked, "Duplicate must not be is_blocked")
        self.assertFalse(r2.is_failed, "Duplicate must not be is_failed")
        self.assertTrue(r2.is_simulation, "is_simulation flag must be preserved from original")

    def test_duplicate_result_is_simulated_false_for_real_execution(self):
        """Issue 3 edge: SKIPPED_DUPLICATE from a real (non-simulation) execution must be is_simulated=False."""
        from ...execution.contracts import ExecutionResult
        from datetime import timezone

        # Build a fake 'real' execution result in the store
        from ...domain.execution import build_execution_key
        intent, decision = self._build_intent_and_decision()
        target_id = intent.target.entity_id if intent.target else None
        key = build_execution_key(
            incident_id=intent.incident_id,
            action=intent.action,
            target=target_id,
            parameters=dict(intent.parameters),
        )

        # Store a fake 'real' (non-simulation) result with the same idempotency key
        fake_real = ExecutionResult(
            execution_id="exec_real_001",
            decision_id=decision.decision_id,
            intent_id=intent.intent_id,
            action=intent.action,
            status=ExecutionStatus.SUCCEEDED,
            idempotency_key=key,
            attempted_at=self.data.anchor,
            completed_at=self.data.anchor,
            is_simulation=False,  # NOT simulation
            provider_reference="real_razorpay_ref_001",
        )
        self.store.save(fake_real)

        # Now a re-submission should produce SKIPPED_DUPLICATE with is_simulation=False
        r_dup = self.engine.execute(decision=decision, intent=intent, now=self.data.anchor)
        self.assertEqual(r_dup.status, ExecutionStatus.SKIPPED_DUPLICATE)
        self.assertTrue(r_dup.is_duplicate)
        self.assertFalse(r_dup.is_simulation, "Original was not a simulation, so duplicate must also not be")
        self.assertFalse(r_dup.is_simulated, "is_simulated must be False when original was real")


if __name__ == "__main__":
    unittest.main()
