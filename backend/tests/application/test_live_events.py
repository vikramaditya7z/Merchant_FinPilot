"""Unit tests for the real-time stage progress event lifecycle and SSE streaming.

Tests:
A. Successful incident: Emits full sequence of stage events (detection, investigation, agent, verification, policy, execution).
B. False alarm: Emits detection running -> stopped, and ZERO downstream events.
C. Recovery not eligible: Emits verification running -> blocked, and ZERO policy/execution events.
D. Agent failure: Emits agent running -> failed, and ZERO downstream events.
E. Stale run protection: Confirms run_id is attached to every event and isolates separate runs.
F. API streaming handler: Verifies handle_process_incident_stream yields valid SSE data chunks.
"""

import json
import unittest
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ...agent.agent import FinancialAgent
from ...agent.contracts import AgentResponse, LLMMessage
from ...agent.provider import MockLLMProvider
from ...api.router import FinancialIncidentAPI
from ...application.contracts import PipelineStage, PipelineStatus, StageProgressEvent, StageProgressStatus
from ...application.orchestrator import FinancialIncidentOrchestrator
from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import ComparableWindowMode, Currency, IntentAction, PolicyVerdict, TargetEntityType
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.money import Money
from ...execution.adapters import SimulatedExecutionAdapter
from ...execution.engine import ExecutionEngine
from ...execution.store import ExecutionStore
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...policy.engine import PolicyEngine
from ...server import create_default_mock_handler
from ...tools.registry import create_default_registry
from ...verification.verifier import FinancialVerifier


def create_test_agent(db: Database, audit_log: Optional[AuditLog] = None) -> FinancialAgent:
    handler = create_default_mock_handler(db)
    provider = MockLLMProvider(handler=handler)
    registry = create_default_registry()
    bound_tools = registry.bind(db)
    return FinancialAgent(provider=provider, tools=bound_tools, audit_log=audit_log)


class LiveStageEventsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database()
        self.audit_log = AuditLog()
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier(audit_log=self.audit_log)
        self.policy_engine = PolicyEngine(audit_log=self.audit_log)
        self.exec_store = ExecutionStore()
        self.adapter = SimulatedExecutionAdapter()
        self.execution_engine = ExecutionEngine(
            store=self.exec_store,
            adapter=self.adapter,
            audit_log=self.audit_log,
        )
        self.agent = create_test_agent(self.db)
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

    def test_successful_incident_event_sequence(self) -> None:
        """A. Successful incident emits running and completed for all 6 stages."""
        scen_data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.db.save_payments(scen_data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(), scen_data.incident_window, scen_data.spec.baseline_days
        )
        comparable_mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if scen_data.spec.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=comparable_mode,
        )

        events: List[Dict[str, Any]] = []
        res = self.orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_test_01",
            now=scen_data.anchor,
            on_progress=events.append,
        )

        self.assertEqual(res.status, PipelineStatus.COMPLETED)

        stages_emitted = [(e["stage"], e["status"]) for e in events]
        expected_stages = [
            ("detection", "running"),
            ("detection", "completed"),
            ("investigation", "running"),
            ("investigation", "completed"),
            ("agent", "running"),
            ("agent", "completed"),
            ("verification", "running"),
            ("verification", "completed"),
            ("policy", "running"),
            ("policy", "completed"),
            ("execution", "running"),
            ("execution", "completed"),
        ]
        self.assertEqual(stages_emitted, expected_stages)

        # Confirm all events share the identical valid run_id
        for e in events:
            self.assertEqual(e["run_id"], res.run_id)
            self.assertIn("timestamp", e)

    def test_false_alarm_event_sequence(self) -> None:
        """B. False alarm stops at Detection and emits zero downstream events."""
        scen_data = generate_scenario(ScenarioId.FALSE_ALARM)
        self.db.save_payments(scen_data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(), scen_data.incident_window, scen_data.spec.baseline_days
        )
        comparable_mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if scen_data.spec.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=comparable_mode,
        )

        events: List[Dict[str, Any]] = []
        res = self.orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_test_01",
            now=scen_data.anchor,
            on_progress=events.append,
        )

        self.assertEqual(res.status, PipelineStatus.STOPPED)
        self.assertEqual(res.final_stage, PipelineStage.DETECTION)

        stages_emitted = [(e["stage"], e["status"]) for e in events]
        self.assertEqual(
            stages_emitted,
            [
                ("detection", "running"),
                ("detection", "stopped"),
            ],
        )

        # Zero downstream events
        for e in events:
            self.assertNotIn(e["stage"], ["investigation", "agent", "verification", "policy", "execution"])

    def test_recovery_not_eligible_event_sequence(self) -> None:
        """C. Recovery not eligible stops at Verification and emits zero policy/execution events."""
        scen_data = generate_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.db.save_payments(scen_data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(), scen_data.incident_window, scen_data.spec.baseline_days
        )
        comparable_mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if scen_data.spec.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=comparable_mode,
        )

        events: List[Dict[str, Any]] = []
        res = self.orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_test_01",
            now=scen_data.anchor,
            on_progress=events.append,
        )

        self.assertEqual(res.status, PipelineStatus.STOPPED)
        self.assertEqual(res.final_stage, PipelineStage.VERIFICATION)

        stages_emitted = [(e["stage"], e["status"]) for e in events]
        self.assertEqual(
            stages_emitted,
            [
                ("detection", "running"),
                ("detection", "completed"),
                ("investigation", "running"),
                ("investigation", "completed"),
                ("agent", "running"),
                ("agent", "completed"),
                ("verification", "running"),
                ("verification", "blocked"),
            ],
        )

        # Zero policy or execution events
        for e in events:
            self.assertNotIn(e["stage"], ["policy", "execution"])

    def test_agent_failure_event_sequence(self) -> None:
        """D. Agent failure emits agent failed and stops immediately."""
        def failing_handler(messages, tool_schemas):
            raise RuntimeError("Gemini Quota Exceeded (RESOURCE_EXHAUSTED)")

        failing_agent = FinancialAgent(
            provider=MockLLMProvider(handler=failing_handler),
            tools=create_default_registry(),
        )
        orch = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=failing_agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        scen_data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.db.save_payments(scen_data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(), scen_data.incident_window, scen_data.spec.baseline_days
        )
        comparable_mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if scen_data.spec.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=comparable_mode,
        )

        events: List[Dict[str, Any]] = []
        res = orch.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_test_01",
            now=scen_data.anchor,
            on_progress=events.append,
        )

        self.assertEqual(res.status, PipelineStatus.FAILED)
        self.assertEqual(res.final_stage, PipelineStage.AGENT)

        stages_emitted = [(e["stage"], e["status"]) for e in events]
        self.assertEqual(
            stages_emitted,
            [
                ("detection", "running"),
                ("detection", "completed"),
                ("investigation", "running"),
                ("investigation", "completed"),
                ("agent", "running"),
                ("agent", "failed"),
            ],
        )

        # Zero verification/policy/execution events
        for e in events:
            self.assertNotIn(e["stage"], ["verification", "policy", "execution"])

    def test_stale_run_protection_isolation(self) -> None:
        """E. Multiple consecutive runs emit unique run IDs to isolate stale callbacks."""
        scen_data = generate_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.db.save_payments(scen_data.agent_enriched())
        buckets = build_daily_hourly_baseline(
            scen_data.agent_enriched(), scen_data.incident_window, scen_data.spec.baseline_days
        )
        comparable_mode = (
            ComparableWindowMode.SAME_HOUR_OF_DAY
            if scen_data.spec.ground_truth.requires_same_hour_baseline
            else ComparableWindowMode.ALL
        )
        metrics = compute_metrics(
            scen_data.agent_enriched(),
            scen_data.incident_window,
            scen_data.anchor,
            baseline_windows=buckets,
            comparable_mode=comparable_mode,
        )

        events_a: List[Dict[str, Any]] = []
        res_a = self.orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_run_A",
            now=scen_data.anchor,
            on_progress=events_a.append,
        )

        events_b: List[Dict[str, Any]] = []
        res_b = self.orchestrator.process_incident(
            metrics=metrics,
            payments=scen_data.incident_enriched(),
            baseline_payments=scen_data.baseline_enriched(),
            merchant_id="merchant_run_B",
            now=scen_data.anchor,
            on_progress=events_b.append,
        )

        self.assertNotEqual(res_a.run_id, res_b.run_id)
        self.assertTrue(all(e["run_id"] == res_a.run_id for e in events_a))
        self.assertTrue(all(e["run_id"] == res_b.run_id for e in events_b))

    def test_api_handle_process_incident_stream(self) -> None:
        """F. API router handle_process_incident_stream yields valid SSE byte chunks."""
        api = FinancialIncidentAPI(
            orchestrator=self.orchestrator,
            database=self.db,
            audit_log=self.audit_log,
        )

        status_code, stream_factory = api.handle_process_incident_stream({
            "merchant_id": "merchant_test_01",
            "scenario_id": "false_alarm",
        })

        self.assertEqual(status_code, 200)
        chunks = list(stream_factory())
        self.assertGreater(len(chunks), 0)

        events = []
        for chunk in chunks:
            text = chunk.decode("utf-8").strip()
            if text.startswith("data:"):
                ev_data = json.loads(text[5:].strip())
                events.append(ev_data)

        self.assertEqual(events[0]["stage"], "detection")
        self.assertEqual(events[0]["status"], "running")
        self.assertEqual(events[1]["stage"], "detection")
        self.assertEqual(events[1]["status"], "stopped")
        self.assertEqual(events[-1]["stage"], "pipeline")
        self.assertIn("payload", events[-1])


if __name__ == "__main__":
    unittest.main()
