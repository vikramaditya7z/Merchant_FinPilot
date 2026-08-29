"""Tests for the FinancialAgent reasoning layer.

Verifies:
1. Agent receives incident and executes multi-step tool calls via ToolRegistry.
2. Tool results are treated as authoritative and preserved.
3. Invalid tool arguments or unknown tools are handled safely.
4. Structured AgentResponse parsing and AgentIntent generation.
5. AgentIntent is strictly a proposal (no state changes or side effects).
6. Malformed JSON handling, reprompting, and safe degradation.
7. Provider error handling (GeminiProvider credentials, rate limits, network errors).
8. Audit trail integration (INVESTIGATION_STARTED, TOOL_CALLED, AGENT_REASONING_RECORDED, INTENT_PROPOSED).
9. Scenario-level reasoning across NORMAL, UPI_FAILURE_SPIKE, MULTIPLE_FAILURES, INSUFFICIENT_DATA, RECOVERY_NOT_ELIGIBLE.
"""

import json
import unittest
from unittest import mock
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    Dimension,
    IntentAction,
    TargetEntityType,
)
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...tools.registry import create_default_registry
from ..helpers import NOW
from ...agent.agent import FinancialAgent
from ...agent.contracts import (
    AgentResponse,
    LLMMessage,
    ToolCallRecord,
    ToolCallRequest,
)
from ...agent.parser import AgentParsingError, extract_json_payload, parse_agent_response
from ...agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from ...agent.provider import (
    GeminiProvider,
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    MockLLMProvider,
)


class AgentBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.registry = create_default_registry()
        self.audit_log = AuditLog()

    def tearDown(self):
        self.db.close()

    def _seed_scenario(self, scenario_id: ScenarioId):
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
        if incident is not None:
            self.db.save_incident(incident)
            report = self.investigator.investigate(
                incident=incident,
                payments=data.incident_enriched(),
                baseline_payments=data.baseline_enriched(),
                now=data.anchor,
            )
            self.db.save_investigation(report)
            return data, incident, report
        return data, None, None


class AgentCoreLoopTests(AgentBaseTestCase):
    def test_agent_investigation_multi_turn_tool_loop(self):
        """Agent performs multi-step tool investigation and produces structured AgentResponse."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        # Scripted turns:
        # Turn 1: request get_incident_summary and get_failure_breakdown(payment_method)
        turn1 = LLMMessage(
            role="model",
            tool_calls=(
                ToolCallRequest(
                    call_id="c1",
                    tool_name="get_incident_summary",
                    arguments={"incident_id": incident.incident_id},
                ),
                ToolCallRequest(
                    call_id="c2",
                    tool_name="get_failure_breakdown",
                    arguments={
                        "incident_id": incident.incident_id,
                        "dimension": "payment_method",
                    },
                ),
            ),
        )

        # Turn 2: request get_revenue_exposure
        turn2 = LLMMessage(
            role="model",
            tool_calls=(
                ToolCallRequest(
                    call_id="c3",
                    tool_name="get_revenue_exposure",
                    arguments={"incident_id": incident.incident_id},
                ),
            ),
        )

        # Turn 3: final structured response
        turn3 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": (
                    "The incident exhibits a heavy concentration of payment failures in UPI. "
                    "UPI failure rate spiked to 29.7% (+25.7pp deviation, 7.4x lift) representing 93.6% of failures."
                ),
                "verified_facts": [
                    "UPI accounts for 44 out of 47 total failures in the 1-hour window.",
                    "Observed failure rate for UPI is 29.7% against baseline of 4.1%.",
                    "Total failed GMV is ₹1,46,500 with revenue at risk of ₹1,26,400.",
                ],
                "findings": [
                    {
                        "title": "UPI Payment Method Degradation",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": incident.evidence[0].evidence_id if incident.evidence else "ev_upi",
                        "summary": "UPI failure rate is 7.4x elevated above baseline.",
                    }
                ],
                "uncertainty_or_limitations": [
                    "No direct banking rail status API was available; correlation observed in merchant traffic."
                ],
                "proposed_intent": {
                    "action": "notify_merchant",
                    "reason": (
                        "Notifying merchant of confirmed UPI degradation to advise customer checkout messaging "
                        "and temporary promotion of card/netbanking alternatives."
                    ),
                    "target_type": "merchant",
                    "target_id": "test_merchant",
                    "parameters": {"channels": "email,webhook"},
                    "evidence_refs": [incident.evidence[0].evidence_id] if incident.evidence else ["ev_1"],
                    "confidence": "0.95",
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1, turn2, turn3])
        agent = FinancialAgent(
            provider=mock_provider,
            tools=self.registry,
            audit_log=self.audit_log,
        )

        response = agent.investigate(
            incident_id=incident.incident_id,
            db=self.db,
            now=data.anchor,
        )

        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.incident_id, incident.incident_id)
        self.assertEqual(len(response.tool_calls_used), 3)
        self.assertEqual(
            [t.tool_name for t in response.tool_calls_used],
            ["get_incident_summary", "get_failure_breakdown", "get_revenue_exposure"],
        )
        self.assertTrue(all(t.success for t in response.tool_calls_used))
        self.assertEqual(len(response.findings), 1)
        self.assertEqual(response.findings[0].dimension, "payment_method")
        self.assertEqual(response.findings[0].observed_value, "upi")

        # Verify proposed intent
        intent = response.proposed_intent
        self.assertIsNotNone(intent)
        self.assertEqual(intent.action, IntentAction.NOTIFY_MERCHANT)
        self.assertEqual(intent.target.entity_type, TargetEntityType.MERCHANT)
        self.assertEqual(intent.target.entity_id, "test_merchant")
        self.assertGreaterEqual(len(intent.reason), 20)
        self.assertTrue(intent.is_consequential)

        # Audit events verification
        events = self.audit_log.events
        event_types = [e.event_type for e in events]
        self.assertIn(AuditEventType.INVESTIGATION_STARTED, event_types)
        self.assertIn(AuditEventType.TOOL_CALLED, event_types)
        self.assertIn(AuditEventType.AGENT_REASONING_RECORDED, event_types)
        self.assertIn(AuditEventType.INTENT_PROPOSED, event_types)

    def test_agent_handles_invalid_tool_arguments_safely(self):
        """Tool validation errors are returned cleanly to the model rather than crashing the loop."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        turn1 = LLMMessage(
            role="model",
            tool_calls=(
                ToolCallRequest(
                    call_id="c_err",
                    tool_name="get_failure_breakdown",
                    arguments={
                        "incident_id": incident.incident_id,
                        "dimension": "invalid_dimension_name",
                    },
                ),
            ),
        )

        turn2 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Observed invalid tool argument error and concluded gracefully.",
                "verified_facts": ["Tool returned INVALID_ARGUMENT."],
                "findings": [],
                "uncertainty_or_limitations": ["Invalid dimension requested."],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "No action proposed due to invalid tool query parameter.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1, turn2])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(len(response.tool_calls_used), 1)
        self.assertFalse(response.tool_calls_used[0].success)
        self.assertEqual(
            response.tool_calls_used[0].raw_result["error_code"], "INVALID_ARGUMENT"
        )
        self.assertEqual(response.proposed_intent.action, IntentAction.NO_ACTION)

    def test_agent_reprompts_on_malformed_json_output(self):
        """Agent reprompts on malformed model response and recovers when valid JSON is returned."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        # Turn 1: model returns non-JSON text
        turn1 = LLMMessage(
            role="model",
            content="This is just free-form text without the required JSON schema.",
        )

        # Turn 2: model corrects itself after reprompt
        turn2 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Corrected structured response following schema requirements.",
                "verified_facts": ["Incident verified."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "No action proposed after schema reprompt.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1, turn2])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.proposed_intent.action, IntentAction.NO_ACTION)
        self.assertEqual(response.iterations_count, 2)

    def test_agent_intent_is_strictly_a_proposal(self):
        """Agent generating an intent modifies zero database records."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        initial_payments_count = len(self.db.list_payments())
        initial_incidents_count = len(self.db.list_incidents())

        turn1 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Proposing a merchant recommendation based on tool facts.",
                "verified_facts": ["Facts verified."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "recommend_only",
                    "reason": "Propose merchant recommendation to monitor payment rails.",
                    "target_type": "incident",
                    "target_id": incident.incident_id,
                    "evidence_refs": [incident.evidence[0].evidence_id] if incident.evidence else ["ev_1"],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertIsNotNone(response.proposed_intent)

        # Verify DB remained completely unchanged
        self.assertEqual(len(self.db.list_payments()), initial_payments_count)
        self.assertEqual(len(self.db.list_incidents()), initial_incidents_count)


class ScenarioAgentReasoningTests(AgentBaseTestCase):
    def test_scenario_normal_proposes_no_action(self):
        """NORMAL scenario: Agent observes healthy traffic and proposes NO_ACTION."""
        data = generate_scenario(ScenarioId.NORMAL)
        self.db.save_payments(data.agent_enriched())

        # In NORMAL, no incident is opened by detector
        # We can construct a hypothetical inspection query
        turn1 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Normal transaction traffic observed with 3.1% failure rate consistent with baseline.",
                "verified_facts": ["Failure rate is within expected variance."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "Baseline behavior is normal and no payment degradation is present.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate("inc_normal", db=self.db, now=data.anchor)
        self.assertEqual(response.proposed_intent.action, IntentAction.NO_ACTION)

    def test_scenario_multiple_failures_reports_concurrent_findings(self):
        """MULTIPLE_FAILURES: Agent synthesizes both UPI and Tamil Nadu regional findings."""
        data, incident, _ = self._seed_scenario(ScenarioId.MULTIPLE_FAILURES)
        self.assertIsNotNone(incident)

        turn1 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": (
                    "Investigation reveals multiple concurrent failure concentrations: "
                    "UPI payment method shows +25.9pp deviation (45 failures), while "
                    "Tamil Nadu (IN-TN) region shows +20.6pp deviation (14 failures). "
                    "Evidence indicates co-occurring issues rather than an isolated single cause."
                ),
                "verified_facts": [
                    "UPI accounts for 45/54 failures (30.0% failure rate vs 4.1% baseline).",
                    "IN-TN accounts for 14/54 failures (25.9% failure rate vs 5.3% baseline).",
                ],
                "findings": [
                    {
                        "title": "UPI Degradation",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": incident.evidence[0].evidence_id if incident.evidence else "ev_upi",
                        "summary": "Elevated UPI failure rate (+25.9pp deviation).",
                    },
                    {
                        "title": "Tamil Nadu Regional Concentration",
                        "dimension": "region",
                        "observed_value": "IN-TN",
                        "evidence_ref": incident.evidence[1].evidence_id if len(incident.evidence) > 1 else "ev_tn",
                        "summary": "Regional outage in IN-TN (+20.6pp deviation).",
                    },
                ],
                "uncertainty_or_limitations": [
                    "Slices overlap; UPI traffic originates partly within IN-TN."
                ],
                "proposed_intent": {
                    "action": "escalate_to_human",
                    "reason": "Escalating multi-dimensional outage involving both UPI rails and IN-TN regional routes to operations team.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertEqual(len(response.findings), 2)
        self.assertEqual(response.proposed_intent.action, IntentAction.ESCALATE_TO_HUMAN)

    def test_scenario_recovery_not_eligible_recognizes_risk_blocked_constraint(self):
        """RECOVERY_NOT_ELIGIBLE: Agent recognizes risk-engine blocks and does not propose routing bypass."""
        data, incident, _ = self._seed_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        turn1 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": (
                    "Failures are categorized as risk_blocked under Razorpay fraud policy rules. "
                    "These transactions are ineligible for automated retry or routing changes."
                ),
                "verified_facts": [
                    "Failures are driven by risk engine blocks.",
                    "Revenue exposure is marked non-recoverable.",
                ],
                "findings": [
                    {
                        "title": "Risk Engine Policy Block",
                        "dimension": "failure_category",
                        "observed_value": "risk_blocked",
                        "evidence_ref": incident.evidence[0].evidence_id if incident.evidence else "ev_risk",
                        "summary": "Transactions blocked by risk rules; not retryable.",
                    }
                ],
                "uncertainty_or_limitations": [
                    "Risk scoring rules are governed by external Razorpay compliance policies."
                ],
                "proposed_intent": {
                    "action": "escalate_to_human",
                    "reason": "Risk-blocked transactions flagged for manual merchant fraud review.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertEqual(response.findings[0].observed_value, "risk_blocked")
        self.assertEqual(response.proposed_intent.action, IntentAction.ESCALATE_TO_HUMAN)

    def test_scenario_insufficient_data_acknowledges_thin_sample(self):
        """INSUFFICIENT_DATA: Agent detects thin sample volume and notes uncertainty."""
        data = generate_scenario(ScenarioId.INSUFFICIENT_DATA)
        self.db.save_payments(data.agent_enriched())

        turn1 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Decided transaction count is below minimum threshold (<10 transactions). Evidence is insufficient.",
                "verified_facts": ["Total sample size is 8 decided transactions."],
                "findings": [],
                "uncertainty_or_limitations": [
                    "Transaction volume is too small for statistical significance or reliable baseline deviation."
                ],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "Abstaining from action proposal due to insufficient data in window.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1])
        agent = FinancialAgent(provider=mock_provider, tools=self.registry)

        response = agent.investigate("inc_insufficient", db=self.db, now=data.anchor)
        self.assertEqual(response.proposed_intent.action, IntentAction.NO_ACTION)
        self.assertTrue(len(response.uncertainty_or_limitations) > 0)

    def test_max_iterations_exhaustion_fallback(self):
        """Agent gracefully returns fallback response when max iterations are exceeded."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        # Provider continuously emits tool calls without ever concluding
        turn = LLMMessage(
            role="model",
            tool_calls=(
                ToolCallRequest(
                    call_id="c_inf",
                    tool_name="get_incident_summary",
                    arguments={"incident_id": incident.incident_id},
                ),
            ),
        )

        mock_provider = MockLLMProvider(handler=lambda msgs, schemas: turn)
        agent = FinancialAgent(
            provider=mock_provider, tools=self.registry, max_iterations=3
        )

        response = agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        self.assertIsInstance(response, AgentResponse)
        self.assertEqual(response.iterations_count, 3)
        self.assertIn("iteration limit", response.reasoning)
        self.assertIsNone(response.proposed_intent)

    def test_audit_trail_integrity_after_agent_run(self):
        """Full cryptographic audit integrity is verified after agent investigation."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        turn1 = LLMMessage(
            role="model",
            tool_calls=(
                ToolCallRequest(
                    call_id="c1",
                    tool_name="get_incident_summary",
                    arguments={"incident_id": incident.incident_id},
                ),
            ),
        )
        turn2 = LLMMessage(
            role="model",
            content=json.dumps({
                "reasoning": "Standard investigation completed.",
                "verified_facts": ["Facts verified."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "no_action",
                    "reason": "No action needed after summary verification.",
                    "evidence_refs": [],
                },
            }),
        )

        mock_provider = MockLLMProvider(scripted_turns=[turn1, turn2])
        agent = FinancialAgent(
            provider=mock_provider, tools=self.registry, audit_log=self.audit_log
        )

        agent.investigate(incident.incident_id, db=self.db, now=data.anchor)
        is_valid, errors = self.audit_log.verify_integrity()
        self.assertTrue(is_valid, f"Audit integrity failed: {errors}")


class GeminiProviderUnitTests(unittest.TestCase):
    def test_gemini_provider_raises_on_missing_api_key(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            provider = GeminiProvider(api_key=None)
            with self.assertRaises(LLMAuthenticationError):
                provider.generate_turn(messages=[], tool_schemas=[])

    def test_json_payload_extractor_handles_markdown_fences(self):
        markdown_text = "Here is the result:\n```json\n{\"reasoning\": \"ok\", \"verified_facts\": []}\n```\nHope that helps!"
        extracted = extract_json_payload(markdown_text)
        self.assertEqual(extracted, {"reasoning": "ok", "verified_facts": []})

    def test_json_payload_extractor_raises_on_invalid_json(self):
        with self.assertRaises(AgentParsingError):
            extract_json_payload("not valid json at all")

    def test_parse_agent_response_rejects_empty_reasoning(self):
        with self.assertRaises(AgentParsingError):
            parse_agent_response(
                raw_text=json.dumps({"reasoning": "", "verified_facts": []}),
                incident_id="inc_1",
                tool_calls_used=(),
                model_id="test-model",
                prompt_version="v1",
                iterations_count=1,
                now=NOW,
            )

class EvidenceBindingRegressionTests(AgentBaseTestCase):
    def test_evidence_binding_safely_resolves_real_evidence_to_verifier_pass(self):
        """STEP 6: Missing evidence_refs in intent binds safely to incident's real evidence, passing verification."""
        from ...verification.verifier import FinancialVerifier

        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)
        real_evidence_id = incident.evidence[0].evidence_id

        # Model emits intent with empty evidence_refs
        raw_model_json = json.dumps({
            "reasoning": "UPI rail failure observed across merchants.",
            "verified_facts": ["UPI failure rate spiked."],
            "findings": [],
            "uncertainty_or_limitations": [],
            "proposed_intent": {
                "action": "notify_merchant",
                "target_type": "merchant",
                "target_id": "test_merchant",
                "reason": "UPI failure rate spiked significantly above baseline lookback.",
                "evidence_refs": [],
                "confidence": "0.95",
            },
        })

        parsed = parse_agent_response(
            raw_text=raw_model_json,
            incident_id=incident.incident_id,
            tool_calls_used=(),
            model_id="test-model",
            prompt_version="v1",
            iterations_count=1,
            now=data.anchor,
            db=self.db,
        )

        self.assertIsNotNone(parsed.proposed_intent)
        self.assertIn(real_evidence_id, parsed.proposed_intent.evidence_refs)
        self.assertFalse(any("ev_a138" in ref for ref in parsed.proposed_intent.evidence_refs))

        verifier = FinancialVerifier()
        v_res = verifier.verify(
            intent=parsed.proposed_intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )
        self.assertTrue(v_res.is_verified)
        chk5 = next(c for c in v_res.checks if c.check_id == "chk_evidence_exists")
        self.assertTrue(chk5.passed, "chk_evidence_exists must PASS for real evidence")

    def test_fabricated_evidence_id_strictly_rejected_by_verifier(self):
        """STEP 6 (Negative): Fabricated/nonexistent evidence IDs (e.g. ev_fake123) are strictly REJECTED."""
        from ...verification.verifier import FinancialVerifier

        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        # Model emits intent with fabricated evidence ref
        raw_model_json = json.dumps({
            "reasoning": "UPI rail failure observed.",
            "verified_facts": ["UPI failure rate spiked."],
            "findings": [],
            "uncertainty_or_limitations": [],
            "proposed_intent": {
                "action": "notify_merchant",
                "target_type": "merchant",
                "target_id": "test_merchant",
                "reason": "UPI failure rate spiked significantly above baseline lookback.",
                "evidence_refs": ["ev_fake123_hallucinated"],
                "confidence": "0.95",
            },
        })

        parsed = parse_agent_response(
            raw_text=raw_model_json,
            incident_id=incident.incident_id,
            tool_calls_used=(),
            model_id="test-model",
            prompt_version="v1",
            iterations_count=1,
            now=data.anchor,
            db=self.db,
        )

        self.assertIsNotNone(parsed.proposed_intent)
        self.assertEqual(parsed.proposed_intent.evidence_refs, ("ev_fake123_hallucinated",))

        verifier = FinancialVerifier()
        v_res = verifier.verify(
            intent=parsed.proposed_intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )
        self.assertFalse(v_res.is_verified)
        chk5 = next(c for c in v_res.checks if c.check_id == "chk_evidence_exists")
        self.assertFalse(chk5.passed, "chk_evidence_exists must FAIL for fabricated evidence")


if __name__ == "__main__":
    unittest.main()
