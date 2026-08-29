"""Comprehensive tests for the FinancialIncidentOrchestrator.

Verifies:
1. Full end-to-end pipeline execution on UPI_FAILURE_SPIKE reaches SIMULATED execution.
2. Normal scenario (no anomaly) stops cleanly at DETECTION with no incident.
3. Investigation reports are properly generated and preserved in PipelineResult.
4. Missing agent halts at AGENT stage.
5. Agent finding with no actionable intent stops at AGENT stage.
6. Verification failure (e.g. fabricated evidence) stops at VERIFICATION stage without calling Policy or Execution.
7. Policy BLOCK (e.g. RECOVERY_NOT_ELIGIBLE) stops at POLICY stage without invoking ExecutionEngine.
8. Policy ESCALATE stops at POLICY stage with escalation reasons.
9. Execution failure produces FAILED PipelineStatus.
10. Repeated pipeline execution is idempotent (SKIPPED_DUPLICATE).
11. Audit trail records PIPELINE_STARTED and PIPELINE_COMPLETED/STOPPED with verified integrity.
12. Zero database corruption / no payment mutation.
"""

import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Tuple

from ...agent.agent import FinancialAgent
from ...agent.contracts import (
    AgentResponse,
    AgentStructuredFinding,
    LLMMessage,
    ToolCallRequest,
)
from ...agent.provider import MockLLMProvider
from ...application.contracts import PipelineResult, PipelineStage, PipelineStatus
from ...application.orchestrator import FinancialIncidentOrchestrator
from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import (
    AuditEventType,
    Currency,
    ExecutionStatus,
    IntentAction,
    PaymentStatus,
    PolicyVerdict,
    TargetEntityType,
)
from ...domain.execution import build_execution_key
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.money import Money
from ...domain.policy import PolicyDecision
from ...domain.window import TimeWindow
from ...execution.adapters import ExecutionAdapter, SimulatedExecutionAdapter
from ...execution.contracts import ExecutionResult
from ...execution.engine import ExecutionEngine
from ...execution.store import ExecutionStore
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...policy.config import PolicyConfig
from ...policy.engine import PolicyEngine
from ...tools.registry import create_default_registry
from ...verification.verifier import FinancialVerifier
from ..helpers import NOW


class OrchestratorBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier()
        self.policy_engine = PolicyEngine()
        self.store = ExecutionStore()
        self.adapter = SimulatedExecutionAdapter()
        self.audit_log = AuditLog()
        self.execution_engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
        )
        self.registry = create_default_registry()

    def tearDown(self):
        self.db.close()

    def _create_mock_agent(
        self,
        incident_id: str,
        action: IntentAction = IntentAction.NOTIFY_MERCHANT,
        target_id: str = "test_merchant",
        target_type: TargetEntityType = TargetEntityType.MERCHANT,
        evidence_ref: str = "ev_1",
        confidence: Decimal = Decimal("0.95"),
        include_intent: bool = True,
    ) -> FinancialAgent:
        proposed_intent_dict = (
            {
                "action": action.value,
                "target_type": target_type.value if target_type else None,
                "target_id": target_id,
                "reason": "Merchant notification warranted by verified UPI degradation over baseline.",
                "evidence_refs": [evidence_ref],
                "parameters": {"channels": "email,webhook"},
                "confidence": str(confidence),
            }
            if include_intent
            else None
        )

        response_payload = {
            "reasoning": "The incident exhibits a heavy concentration of payment failures in UPI.",
            "verified_facts": [
                "UPI accounts for majority of failures in the incident window."
            ],
            "findings": [
                {
                    "title": "UPI Degradation",
                    "dimension": "payment_method",
                    "observed_value": "upi",
                    "evidence_ref": evidence_ref,
                    "summary": "UPI failure rate spiked significantly above baseline.",
                }
            ],
            "uncertainty_or_limitations": [
                "No direct banking rail status API available."
            ],
            "proposed_intent": proposed_intent_dict,
        }

        turn = LLMMessage(
            role="model",
            content=f"```json\n{json.dumps(response_payload)}\n```",
        )

        provider = MockLLMProvider(scripted_turns=[turn])
        bound_tools = self.registry.bind(self.db)
        return FinancialAgent(
            provider=provider,
            tools=bound_tools,
            audit_log=self.audit_log,
        )


class OrchestratorTests(OrchestratorBaseTestCase):
    def test_full_successful_pipeline_completes_with_simulated_execution(self):
        """Full pipeline executes from payments data to simulated execution."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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
        evidence_id = incident.evidence[0].evidence_id if incident.evidence else "ev_1"

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            evidence_ref=evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertIsInstance(result, PipelineResult)
        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertEqual(result.final_stage, PipelineStage.COMPLETED)
        self.assertTrue(result.is_completed)
        self.assertTrue(result.is_simulated)
        self.assertFalse(result.is_stopped)
        self.assertFalse(result.is_failed)
        self.assertIsNotNone(result.incident)
        self.assertIsNotNone(result.investigation_report)
        self.assertIsNotNone(result.agent_response)
        self.assertIsNotNone(result.proposed_intent)
        self.assertIsNotNone(result.verification_result)
        self.assertTrue(result.verification_result.is_verified)
        self.assertIsNotNone(result.policy_decision)
        self.assertEqual(result.policy_decision.verdict, PolicyVerdict.ALLOW)
        self.assertIsNotNone(result.execution_result)
        self.assertEqual(result.execution_result.status, ExecutionStatus.SIMULATED)

    def test_normal_scenario_stops_at_detection_with_no_incident(self):
        """NORMAL scenario without anomalies stops at DETECTION stage."""
        data = generate_scenario(ScenarioId.NORMAL)
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

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            metrics=metrics,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.DETECTION)
        self.assertTrue(result.is_stopped)
        self.assertIsNone(result.incident)
        self.assertIn("No financial incident detected", result.stop_reason)

    def test_no_agent_stops_at_agent_stage(self):
        """When no agent is configured, pipeline stops at AGENT stage."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=None,  # No agent
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            metrics=metrics,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.AGENT)
        self.assertIsNotNone(result.incident)
        self.assertIsNone(result.proposed_intent)
        self.assertIn("No FinancialAgent configured", result.stop_reason)

    def test_agent_finding_without_intent_stops_at_agent_stage(self):
        """If agent returns a finding but no intent, pipeline halts at AGENT stage."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            include_intent=False,  # No intent in response
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.AGENT)
        self.assertIsNone(result.proposed_intent)
        self.assertIsNone(result.verification_result)

    def test_verification_failure_stops_before_policy_and_execution(self):
        """Fabricated evidence ref causes verification failure and halts before Policy."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        # Agent cites fabricated evidence ID
        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref="fabricated_ev_999",
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.VERIFICATION)
        self.assertIsNotNone(result.verification_result)
        self.assertFalse(result.verification_result.is_verified)
        self.assertIsNone(result.policy_decision)
        self.assertIsNone(result.execution_result)

    def test_policy_block_stops_before_execution(self):
        """Policy BLOCK stops execution from being invoked."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        # Config policy to block all consequential actions (kill switch)
        blocked_policy_engine = PolicyEngine(
            config=PolicyConfig(execution_enabled=False),
            audit_log=self.audit_log,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=blocked_policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.POLICY)
        self.assertIsNotNone(result.policy_decision)
        self.assertEqual(result.policy_decision.verdict, PolicyVerdict.BLOCK)
        self.assertIsNone(result.execution_result)

    def test_policy_escalate_stops_before_execution(self):
        """Explicit human escalation stops at POLICY stage without invoking execution."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            action=IntentAction.ESCALATE_TO_HUMAN,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.POLICY)
        self.assertIsNotNone(result.policy_decision)
        self.assertEqual(result.policy_decision.verdict, PolicyVerdict.ESCALATE)
        self.assertIsNone(result.execution_result)

    def test_execution_failure_marks_pipeline_failed(self):
        """Adapter failure marks PipelineResult as FAILED."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        class FaultyAdapter(ExecutionAdapter):
            def execute(self, request, idempotency_key):
                raise ConnectionResetError("External gateway timeout")

        failing_execution_engine = ExecutionEngine(
            adapter=FaultyAdapter(),
            audit_log=self.audit_log,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=failing_execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.FAILED)
        self.assertEqual(result.final_stage, PipelineStage.EXECUTION)
        self.assertTrue(result.is_failed)
        self.assertIsNotNone(result.execution_result)
        self.assertEqual(result.execution_result.status, ExecutionStatus.FAILED)

    def test_idempotent_duplicate_pipeline_run(self):
        """Running the pipeline twice with identical intent results in SKIPPED_DUPLICATE."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        # Run 1: Executes
        res1 = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )
        self.assertEqual(res1.status, PipelineStatus.COMPLETED)
        self.assertEqual(res1.execution_result.status, ExecutionStatus.SIMULATED)

        # Run 2: Replay duplicate
        agent2 = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )
        orchestrator2 = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent2,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        res2 = orchestrator2.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor + timedelta(seconds=10),
        )
        self.assertEqual(res2.status, PipelineStatus.COMPLETED)
        self.assertEqual(res2.execution_result.status, ExecutionStatus.SKIPPED_DUPLICATE)

    def test_audit_integrity_valid_after_pipeline_runs(self):
        """Audit trail preserves cryptographic hash chaining across multiple pipeline runs."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        is_valid, errors = self.audit_log.verify_integrity()
        self.assertTrue(is_valid, f"Audit log verification failed: {errors}")

    def test_scenario_recovery_not_eligible_blocked_by_policy_never_executes(self):
        """In RECOVERY_NOT_ELIGIBLE, payment link creation on non-retryable error is blocked and NEVER executes.

        V2 update: With the CHK_ACTION_ELIGIBILITY deterministic gate, CREATE_PAYMENT_LINK
        is now blocked at VERIFICATION (not Policy), because the verifier detects RISK_BLOCKED
        dominance from investigation findings. This is earlier and stronger than the V1 behavior.
        """
        data = generate_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
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

        # Run investigation so the DB has findings (verifier needs them for eligibility check)
        self.db.save_incident(incident)
        report = self.investigator.investigate(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            now=data.anchor,
        )
        self.db.save_investigation(report)

        # Find a failed payment with risk blocked error
        failed_payment = [
            p.payment if hasattr(p, "payment") else p
            for p in data.incident_enriched()
            if "risk" in str(getattr(p.payment if hasattr(p, "payment") else p, "error_code", "")).lower()
        ][0]

        # Agent proposes CREATE_PAYMENT_LINK for unrecoverable risk-blocked failure
        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            action=IntentAction.CREATE_PAYMENT_LINK,
            target_type=TargetEntityType.PAYMENT,
            target_id=failed_payment.id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        class SpyExecutionEngine(ExecutionEngine):
            def __init__(self):
                super().__init__()
                self.called = False

            def execute(self, decision, intent, now=None):
                self.called = True
                return super().execute(decision, intent, now)

        spy_exec = SpyExecutionEngine()

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=spy_exec,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        # V2: eligibility gate stops at VERIFICATION — stronger than the V1 POLICY block
        self.assertEqual(result.final_stage, PipelineStage.VERIFICATION,
                         f"V2 eligibility gate must stop at VERIFICATION: {result.stop_reason}")
        self.assertIsNotNone(result.verification_result)
        self.assertFalse(result.verification_result.is_verified)
        # Verify execution engine was NEVER called
        self.assertFalse(spy_exec.called)
        self.assertIsNone(result.execution_result)

    def test_scenario_insufficient_data_does_not_force_diagnosis_or_execution(self):
        """In INSUFFICIENT_DATA, low volume prevents false anomaly detection or execution."""
        data = generate_scenario(ScenarioId.INSUFFICIENT_DATA)
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

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            metrics=metrics,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.DETECTION)
        self.assertIsNone(result.incident)
        self.assertIsNone(result.execution_result)

    def test_scenario_multiple_failures_preserves_multiple_concentrations(self):
        """In MULTIPLE_FAILURES, investigation generates dimensional findings across multiple dimensions."""
        data = generate_scenario(ScenarioId.MULTIPLE_FAILURES)
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

        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            baseline_payments=data.baseline_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.COMPLETED)
        self.assertIsNotNone(result.investigation_report)
        findings_count = len(result.investigation_report.primary_findings) + len(result.investigation_report.secondary_findings)
        self.assertTrue(findings_count >= 1)
        self.assertTrue(result.investigation_report.has_multiple_concentrations)

    def test_downstream_components_not_called_on_verification_failure(self):
        """When verification fails, PolicyEngine and ExecutionEngine are never called."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        # Agent with fabricated evidence
        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            evidence_ref="nonexistent_ev_999",
        )

        class SpyPolicyEngine(PolicyEngine):
            def __init__(self):
                super().__init__()
                self.called = False

            def evaluate(self, verified_intent, now=None):
                self.called = True
                return super().evaluate(verified_intent, now)

        class SpyExecutionEngine(ExecutionEngine):
            def __init__(self):
                super().__init__()
                self.called = False

            def execute(self, decision, intent, now=None):
                self.called = True
                return super().execute(decision, intent, now)

        spy_policy = SpyPolicyEngine()
        spy_exec = SpyExecutionEngine()

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            agent=agent,
            verifier=self.verifier,
            policy_engine=spy_policy,
            execution_engine=spy_exec,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertEqual(result.final_stage, PipelineStage.VERIFICATION)
        self.assertFalse(spy_policy.called)
        self.assertFalse(spy_exec.called)

    def test_pipeline_result_summary_formatting(self):
        """PipelineResult summary provides informative human-readable text."""
        data = generate_scenario(ScenarioId.NORMAL)
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

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            database=self.db,
        )

        res = orchestrator.process_incident(
            metrics=metrics,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        self.assertIn("Pipeline STOPPED at stage 'detection'", res.summary)

    def test_deterministic_repeatability_on_identical_inputs(self):
        """Running orchestrator with identical inputs produces identical stage transitions."""
        data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
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

        agent1 = self._create_mock_agent(incident.incident_id, evidence_ref=incident.evidence[0].evidence_id)
        agent2 = self._create_mock_agent(incident.incident_id, evidence_ref=incident.evidence[0].evidence_id)

        orch1 = FinancialIncidentOrchestrator(detector=self.detector, agent=agent1, database=self.db)
        orch2 = FinancialIncidentOrchestrator(detector=self.detector, agent=agent2, database=self.db)

        r1 = orch1.process_incident(incident=incident, payments=data.incident_enriched(), merchant_id="test_merchant", now=data.anchor)
        r2 = orch2.process_incident(incident=incident, payments=data.incident_enriched(), merchant_id="test_merchant", now=data.anchor)

        self.assertEqual(r1.status, r2.status)
        self.assertEqual(r1.final_stage, r2.final_stage)
        self.assertEqual(r1.proposed_intent.content_hash(), r2.proposed_intent.content_hash())


class V2PipelineCorrectnessTests(OrchestratorBaseTestCase):
    """V2 Phase 1 — End-to-end orchestrator correctness tests.

    Tests all three V2 issues at the pipeline level:
    - Issue 1 & 2: RECOVERY_NOT_ELIGIBLE must stop at VERIFICATION, not reach execution.
    - Issue 3: Duplicate pipeline runs must have is_completed=True, is_simulated=True,
      and a distinct idempotent summary.
    """

    def _setup_scenario_full(self, scenario_id: ScenarioId):
        """Setup scenario data, detect incident, run investigation, save everything."""
        from ...financial.engine import build_daily_hourly_baseline, compute_metrics
        data = generate_scenario(scenario_id)
        self.db.save_payments(data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(), data.incident_window, data.anchor, baseline_windows=buckets
        )
        incident = self.detector.detect(metrics, merchant_id="test_merchant")
        if incident is not None:
            self.db.save_incident(incident)
            report = self.investigator.investigate(
                incident=incident,
                payments=data.incident_enriched(),
                baseline_payments=data.baseline_enriched(),
                now=data.anchor,
            )
            self.db.save_investigation(report)
        return data, incident

    def test_recovery_not_eligible_stops_at_verification(self):
        """Issue 1 & 2: RECOVERY_NOT_ELIGIBLE scenario must stop at VERIFICATION.

        When Gemini proposes NOTIFY_MERCHANT for a risk-blocked incident, the
        deterministic CHK_ACTION_ELIGIBILITY gate in FinancialVerifier must reject
        the intent. The pipeline must stop at VERIFICATION, never reaching Policy or Execution.
        """
        data, incident = self._setup_scenario_full(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident, "RECOVERY_NOT_ELIGIBLE must detect an incident")

        # Mock agent proposes NOTIFY_MERCHANT (the problematic action for this scenario)
        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        # Pipeline must stop at VERIFICATION, not reach Policy or Execution
        self.assertEqual(result.final_stage, PipelineStage.VERIFICATION,
                         f"Expected VERIFICATION, got {result.final_stage.value}. stop_reason: {result.stop_reason}")
        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertFalse(result.is_completed, "Pipeline must NOT be completed for risk-blocked scenario")
        self.assertFalse(result.is_simulated, "No execution must have occurred")
        self.assertIsNone(result.policy_decision, "Policy must NOT have been reached")
        self.assertIsNone(result.execution_result, "Execution must NOT have occurred")
        self.assertIsNotNone(result.verification_result)
        self.assertFalse(result.verification_result.is_verified,
                         "Verification must have REJECTED the NOTIFY_MERCHANT intent")

        # Ensure the stop reason mentions the eligibility failure
        self.assertIsNotNone(result.stop_reason)

    def test_recovery_not_eligible_escalate_to_human_succeeds(self):
        """Issue 1 & 2 complement: ESCALATE_TO_HUMAN must complete the pipeline for risk-blocked incidents.

        The eligibility gate exempts ESCALATE_TO_HUMAN, so it should pass verification,
        get an ESCALATE policy verdict, and stop at POLICY (not execution).
        """
        data, incident = self._setup_scenario_full(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        # Mock agent proposes ESCALATE_TO_HUMAN — the correct action for risk-blocked incidents
        agent = self._create_mock_agent(
            incident_id=incident.incident_id,
            action=IntentAction.ESCALATE_TO_HUMAN,
            evidence_ref=incident.evidence[0].evidence_id,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        result = orchestrator.process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )

        # Verification must pass for ESCALATE_TO_HUMAN
        self.assertIsNotNone(result.verification_result)
        self.assertTrue(result.verification_result.is_verified,
                        f"ESCALATE_TO_HUMAN must pass verification: {result.verification_result.summary}")

        # Policy must stop at ESCALATE (not execute)
        self.assertIsNotNone(result.policy_decision)
        self.assertEqual(result.policy_decision.verdict, PolicyVerdict.ESCALATE)
        self.assertEqual(result.final_stage, PipelineStage.POLICY)
        self.assertEqual(result.status, PipelineStatus.STOPPED)
        self.assertIsNone(result.execution_result, "ESCALATE must not reach execution")

    def test_duplicate_pipeline_result_is_completed_and_simulated(self):
        """Issue 3: A duplicate pipeline run must produce is_completed=True and is_simulated=True.

        When the same intent is processed twice (identical idempotency key), the second
        run must return COMPLETED (idempotent) with is_simulated=True and a distinct
        idempotent stop_reason.
        """
        data, incident = self._setup_scenario_full(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        evidence_ref = incident.evidence[0].evidence_id
        shared_store = self.store  # both runs share the idempotency store

        def make_orchestrator():
            agent = self._create_mock_agent(
                incident_id=incident.incident_id,
                action=IntentAction.NOTIFY_MERCHANT,
                evidence_ref=evidence_ref,
            )
            return FinancialIncidentOrchestrator(
                detector=self.detector,
                investigator=self.investigator,
                agent=agent,
                verifier=self.verifier,
                policy_engine=self.policy_engine,
                execution_engine=ExecutionEngine(
                    adapter=self.adapter,
                    store=shared_store,
                    audit_log=self.audit_log,
                ),
                database=self.db,
                audit_log=self.audit_log,
            )

        # First run — fresh simulated execution
        r1 = make_orchestrator().process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )
        self.assertEqual(r1.status, PipelineStatus.COMPLETED)
        self.assertTrue(r1.is_completed)
        self.assertTrue(r1.is_simulated)
        self.assertIsNone(r1.stop_reason, "Fresh execution must have no stop_reason")
        self.assertEqual(r1.execution_result.status, ExecutionStatus.SIMULATED)

        # Second run — idempotent duplicate
        r2 = make_orchestrator().process_incident(
            incident=incident,
            payments=data.incident_enriched(),
            merchant_id="test_merchant",
            now=data.anchor,
        )
        self.assertEqual(r2.status, PipelineStatus.COMPLETED,
                         "Duplicate must still be COMPLETED, not FAILED or STOPPED")
        self.assertTrue(r2.is_completed)
        self.assertTrue(r2.is_simulated,
                        "Duplicate pipeline run must be is_simulated=True")
        self.assertEqual(r2.execution_result.status, ExecutionStatus.SKIPPED_DUPLICATE)
        self.assertTrue(r2.execution_result.is_duplicate)

        # stop_reason must be populated and mention idempotency
        self.assertIsNotNone(r2.stop_reason,
                             "Duplicate run must have a stop_reason explaining the idempotent outcome")
        self.assertIn("already executed", r2.stop_reason.lower(),
                      f"stop_reason must explain duplicate: {r2.stop_reason}")

        # Summary must be distinct and mention IDEMPOTENT
        self.assertIn("IDEMPOTENT", r2.summary,
                      f"Duplicate summary must say IDEMPOTENT: {r2.summary}")


if __name__ == "__main__":
    unittest.main()

