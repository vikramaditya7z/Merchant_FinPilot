"""Comprehensive tests for the HTTP API layer.

Verifies:
1. Valid request processing with synthetic scenario.
2. Invalid request validation (missing merchant_id, malformed JSON, invalid scenario).
3. Full successful pipeline reaches SIMULATED execution via API.
4. Normal scenario stops cleanly at DETECTION with 200 OK.
5. Verification failure stops at VERIFICATION.
6. Policy BLOCK (RECOVERY_NOT_ELIGIBLE) stops at POLICY.
7. Policy ESCALATE stops at POLICY.
8. Execution by incident_id (success and 404).
9. GET /api/v1/incidents/{id} returns full incident or 404.
10. GET /api/v1/audit returns audit records with verified integrity.
11. GET /api/v1/health returns 200 healthy.
12. Strict financial invariants: monetary values serialized as integer minor units (paise), zero floats.
13. Security boundaries: zero API keys, auth headers, or raw credentials leaked in payloads.
14. Unknown route returns 404.
"""

import io
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from ...agent.agent import FinancialAgent
from ...agent.contracts import LLMMessage
from ...agent.provider import MockLLMProvider
from ...api.app import FinPilotApp, create_app
from ...api.contracts import ProcessIncidentRequest, ProcessIncidentResponse
from ...api.router import FinancialIncidentAPI
from ...application.orchestrator import FinancialIncidentOrchestrator
from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import (
    Currency,
    ExecutionStatus,
    IntentAction,
    PaymentStatus,
    PolicyVerdict,
    TargetEntityType,
)
from ...execution.adapters import SimulatedExecutionAdapter
from ...execution.engine import ExecutionEngine
from ...execution.store import ExecutionStore
from ...investigation.investigator import Investigator
from ...policy.engine import PolicyEngine
from ...tools.registry import create_default_registry
from ...verification.verifier import FinancialVerifier
from ..helpers import NOW


class APIBaseTestCase(unittest.TestCase):
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
        action: IntentAction = IntentAction.NOTIFY_MERCHANT,
        target_id: Optional[str] = "test_merchant_123",
        target_type: TargetEntityType = TargetEntityType.MERCHANT,
        evidence_ref: Optional[str] = "AUTO",
        confidence: Decimal = Decimal("0.95"),
        include_intent: bool = True,
    ) -> FinancialAgent:
        def handler(messages, schemas):
            inc_id = None
            for m in messages:
                if m.role == "user" and "Financial Incident '" in (m.content or ""):
                    inc_id = m.content.split("Financial Incident '")[1].split("'")[0].strip()
                elif m.role == "user" and "Incident ID: " in (m.content or ""):
                    inc_id = m.content.split("Incident ID: ")[1].split("\n")[0].strip()

            ev_ref = evidence_ref
            if ev_ref == "AUTO":
                if inc_id and self.db:
                    inc = self.db.get_incident(inc_id)
                    if inc and inc.evidence:
                        ev_ref = inc.evidence[0].evidence_id
                    else:
                        ev_ref = "ev_default"
                else:
                    ev_ref = "ev_default"

            tgt_id = target_id
            if tgt_id == "AUTO":
                tgt_id = inc_id or "test_merchant_123"

            proposed_intent_dict = (
                {
                    "action": action.value,
                    "target_type": target_type.value if target_type else None,
                    "target_id": tgt_id,
                    "reason": "Merchant notification warranted by verified degradation over baseline.",
                    "evidence_refs": [ev_ref] if ev_ref else [],
                    "parameters": {"channels": "email,webhook"},
                    "confidence": str(confidence),
                }
                if include_intent
                else None
            )

            response_payload = {
                "reasoning": "The incident exhibits a heavy concentration of payment failures.",
                "verified_facts": [
                    "Payment failures spiked in the incident window."
                ],
                "findings": [
                    {
                        "title": "Payment Degradation",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": ev_ref or "ev_none",
                        "summary": "Payment failure rate spiked significantly above baseline.",
                    }
                ],
                "uncertainty_or_limitations": [
                    "No direct banking rail status API available."
                ],
                "proposed_intent": proposed_intent_dict,
            }

            return LLMMessage(
                role="model",
                content=f"```json\n{json.dumps(response_payload)}\n```",
            )

        provider = MockLLMProvider(handler=handler)
        bound_tools = self.registry.bind(self.db)
        return FinancialAgent(
            provider=provider,
            tools=bound_tools,
            audit_log=self.audit_log,
        )

    def _create_test_api(self, agent: Optional[FinancialAgent] = None) -> FinancialIncidentAPI:
        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=agent or self._create_mock_agent(),
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            database=self.db,
            audit_log=self.audit_log,
        )
        return FinancialIncidentAPI(
            orchestrator=orchestrator,
            database=self.db,
            audit_log=self.audit_log,
        )

    def _call_wsgi(
        self,
        app: FinPilotApp,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query_string: str = "",
    ) -> Tuple[int, Dict[str, Any]]:
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query_string,
            "CONTENT_LENGTH": str(len(body_bytes)),
            "wsgi.input": io.BytesIO(body_bytes),
        }

        response_status = [None]
        response_headers = [None]

        def start_response(status, headers):
            response_status[0] = status
            response_headers[0] = headers

        result_bytes = app(environ, start_response)
        raw_body = b"".join(result_bytes).decode("utf-8")
        status_code = int(response_status[0].split()[0])
        parsed_body = json.loads(raw_body) if raw_body else {}
        return status_code, parsed_body


class APITests(APIBaseTestCase):
    def test_process_incident_valid_scenario_returns_200_and_completed_result(self):
        """POST /api/v1/incidents/process with valid scenario completes successfully."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["final_stage"], "completed")
        self.assertTrue(body["is_completed"])
        self.assertTrue(body["is_simulated"])
        self.assertFalse(body["is_failed"])
        self.assertFalse(body["is_stopped"])
        self.assertIsNotNone(body["incident"])
        self.assertIsNotNone(body["execution_result"])
        self.assertEqual(body["execution_result"]["status"], "simulated")
        self.assertIn("Pipeline COMPLETED", body["summary"])

    def test_process_incident_normal_scenario_stops_at_detection(self):
        """POST /api/v1/incidents/process with NORMAL scenario stops cleanly at DETECTION."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "normal"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "detection")
        self.assertTrue(body["is_stopped"])
        self.assertFalse(body["is_completed"])
        self.assertIsNone(body["incident"])
        self.assertIn("No financial incident detected", body["stop_reason"])

    def test_process_incident_missing_merchant_id_returns_400(self):
        """POST /api/v1/incidents/process without merchant_id returns 400 Bad Request."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"scenario_id": "normal"},
        )

        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("merchant_id", body["error"])

    def test_process_incident_missing_scenario_and_incident_id_returns_400(self):
        """POST /api/v1/incidents/process with neither scenario_id nor incident_id returns 400."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123"},
        )

        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("either 'scenario_id' or 'incident_id'", body["error"])

    def test_process_incident_unknown_scenario_returns_400(self):
        """POST /api/v1/incidents/process with unknown scenario returns 400."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "fake_scenario_xyz"},
        )

        self.assertEqual(status, 400)
        self.assertIn("error", body)
        self.assertIn("Unknown scenario", body["error"])

    def test_process_incident_malformed_json_returns_400(self):
        """Sending malformed non-JSON body returns 400."""
        api = self._create_test_api()
        app = create_app(api=api)

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/v1/incidents/process",
            "CONTENT_LENGTH": "11",
            "wsgi.input": io.BytesIO(b"not a json!"),
        }

        response_status = [None]
        def start_response(status, headers):
            response_status[0] = status

        result_bytes = app(environ, start_response)
        status_code = int(response_status[0].split()[0])
        body = json.loads(b"".join(result_bytes).decode("utf-8"))

        self.assertEqual(status_code, 400)
        self.assertIn("Invalid JSON payload", body["error"])

    def test_process_incident_policy_block_on_recovery_not_eligible(self):
        """In RECOVERY_NOT_ELIGIBLE scenario, API response indicates STOPPED at VERIFICATION (REJECTED)."""
        data = generate_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        risk_payments = [
            p.payment if hasattr(p, "payment") else p
            for p in data.incident_enriched()
            if "risk" in str(getattr(p.payment if hasattr(p, "payment") else p, "error_code", "")).lower()
        ]
        target_payment_id = risk_payments[0].id

        agent = self._create_mock_agent(
            action=IntentAction.CREATE_PAYMENT_LINK,
            target_type=TargetEntityType.PAYMENT,
            target_id=target_payment_id,
        )

        api = self._create_test_api(agent=agent)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "recovery_not_eligible"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "verification")
        self.assertTrue(body["is_stopped"])
        self.assertIsNotNone(body["verification_result"])
        self.assertFalse(body["verification_result"]["is_verified"])
        self.assertIsNone(body["policy_decision"])
        self.assertIsNone(body["execution_result"])

    def test_process_incident_policy_escalate_returns_stopped_policy(self):
        """When policy escalates, API response returns status=stopped and final_stage=policy."""
        agent = self._create_mock_agent(action=IntentAction.ESCALATE_TO_HUMAN)
        api = self._create_test_api(agent=agent)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "policy")
        self.assertEqual(body["policy_decision"]["verdict"], "escalate")

    def test_process_incident_verification_rejection_stops_at_verification(self):
        """Fabricated evidence ID causes API response to stop at VERIFICATION."""
        agent = self._create_mock_agent(evidence_ref="fabricated_ev_999")
        api = self._create_test_api(agent=agent)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "verification")
        self.assertFalse(body["verification_result"]["is_verified"])
        self.assertIsNone(body["policy_decision"])

    def test_get_incident_endpoint_success_and_not_found(self):
        """GET /api/v1/incidents/{id} returns 200 for existing incident and 404 for missing."""
        api = self._create_test_api()
        app = create_app(api=api)

        # 1. Process an incident to seed DB
        _, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )
        incident_id = body["incident"]["incident_id"]

        # 2. Fetch seeded incident
        status, inc_body = self._call_wsgi(app, "GET", f"/api/v1/incidents/{incident_id}")
        self.assertEqual(status, 200)
        self.assertEqual(inc_body["incident_id"], incident_id)
        self.assertEqual(inc_body["merchant_id"], "test_merchant_123")

        # 3. Fetch nonexistent incident
        status_404, err_body = self._call_wsgi(app, "GET", "/api/v1/incidents/inc_nonexistent_999")
        self.assertEqual(status_404, 404)
        self.assertIn("error", err_body)

    def test_get_audit_trail_endpoint(self):
        """GET /api/v1/audit returns events list with verified cryptographic integrity."""
        api = self._create_test_api()
        app = create_app(api=api)

        # Run pipeline
        self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        # Fetch audit trail
        status, audit_body = self._call_wsgi(app, "GET", "/api/v1/audit")
        self.assertEqual(status, 200)
        self.assertTrue(audit_body["is_valid"])
        self.assertTrue(audit_body["count"] >= 1)
        self.assertEqual(len(audit_body["events"]), audit_body["count"])

    def test_health_endpoint(self):
        """GET /api/v1/health returns 200 OK and healthy status."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(app, "GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["service"], "merchant-finpilot-api")

    def test_money_invariant_integer_paise_in_response(self):
        """Monetary values in API response are exact integer minor units (paise) and never float."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        inc = body["incident"]
        metrics = inc["metrics"]

        # Check total failed GMV is integer
        failed_gmv = metrics["revenue_risk"]["failed_gmv_paise"]
        self.assertIsInstance(failed_gmv, int)
        self.assertNotIsInstance(failed_gmv, float)

        # Check revenue at risk is integer
        rev_risk = metrics["revenue_risk"]["revenue_at_risk_paise"]
        self.assertIsInstance(rev_risk, int)
        self.assertNotIsInstance(rev_risk, float)

    def test_security_zero_secrets_or_keys_leaked(self):
        """Responses never leak API keys, authorization headers, or private credentials."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        json_str = json.dumps(body).lower()
        forbidden = ("authorization", "bearer ", "api_key", "secret_key", "password", "token")
        for term in forbidden:
            self.assertNotIn(term, json_str)

    def test_process_incident_agent_stop_without_intent(self):
        """When agent outputs finding without an intent, API reports stopped at AGENT stage."""
        agent = self._create_mock_agent(include_intent=False)
        api = self._create_test_api(agent=agent)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "agent")
        self.assertTrue(body["is_stopped"])
        self.assertIsNone(body["proposed_intent"])
        self.assertIsNone(body["execution_result"])

    def test_process_incident_execution_failure_marks_failed(self):
        """When the execution adapter fails, API reports status=failed and final_stage=execution."""
        class FailingAdapter(SimulatedExecutionAdapter):
            def execute(self, request, idempotency_key):
                raise RuntimeError("Downstream simulation adapter failed.")

        failing_engine = ExecutionEngine(
            adapter=FailingAdapter(),
            store=self.store,
            audit_log=self.audit_log,
        )

        orchestrator = FinancialIncidentOrchestrator(
            detector=self.detector,
            investigator=self.investigator,
            agent=self._create_mock_agent(),
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=failing_engine,
            database=self.db,
            audit_log=self.audit_log,
        )
        api = FinancialIncidentAPI(orchestrator=orchestrator, database=self.db, audit_log=self.audit_log)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["final_stage"], "execution")
        self.assertTrue(body["is_failed"])
        self.assertIsNotNone(body["execution_result"])
        self.assertEqual(body["execution_result"]["status"], "failed")

    def test_unknown_route_returns_404(self):
        """Accessing undefined routes returns 404 Not Found."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(app, "GET", "/api/v1/unknown_route")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_list_scenarios_endpoint(self):
        """GET /api/v1/scenarios returns all 11 registered synthetic scenarios with metadata."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(app, "GET", "/api/v1/scenarios")
        self.assertEqual(status, 200)
        self.assertIn("scenarios", body)
        self.assertEqual(body["count"], 11)
        scen_ids = [s["scenario_id"] for s in body["scenarios"]]
        self.assertIn("upi_failure_spike", scen_ids)
        self.assertIn("normal", scen_ids)
        self.assertIn("recovery_not_eligible", scen_ids)

    def test_options_cors_preflight(self):
        """HTTP OPTIONS preflight requests return 200 OK with standard CORS headers."""
        api = self._create_test_api()
        app = create_app(api=api)

        status, body = self._call_wsgi(app, "OPTIONS", "/api/v1/incidents/process")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ok"))

    def test_false_alarm_does_not_invoke_gemini_and_halts_at_detection(self):
        """TEST A & B: False Alarm halts deterministically at Detection and NEVER calls Gemini."""
        call_count = 0

        def spy_handler(messages, schemas):
            nonlocal call_count
            call_count += 1
            return LLMMessage(role="model", content='{"reasoning": "unexpected call"}')

        provider = MockLLMProvider(handler=spy_handler)
        agent = FinancialAgent(provider=provider, tools=self.registry)
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
        api = FinancialIncidentAPI(orchestrator=orchestrator, database=self.db, audit_log=self.audit_log)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "false_alarm"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(call_count, 0, "Gemini provider MUST NOT be called for False Alarm scenario.")
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "detection")
        self.assertTrue(body["is_stopped"])
        self.assertFalse(body["is_failed"])
        self.assertFalse(body["is_completed"])
        self.assertIsNone(body["agent_response"])
        self.assertIsNone(body["proposed_intent"])
        self.assertIsNone(body["verification_result"])
        self.assertIsNone(body["policy_decision"])
        self.assertIsNone(body["execution_result"])
        self.assertIn("No financial incident detected under baseline metrics", body["stop_reason"])

    def test_genuine_incident_still_invokes_gemini(self):
        """TEST C: Genuine incident (e.g. upi_failure_spike) invokes Gemini."""
        agent = self._create_mock_agent(action=IntentAction.NOTIFY_MERCHANT)
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
        api = FinancialIncidentAPI(orchestrator=orchestrator, database=self.db, audit_log=self.audit_log)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertIsNotNone(body["agent_response"])
        self.assertIsNotNone(body["proposed_intent"])

    def test_state_isolation_previous_gemini_result_cannot_leak(self):
        """TEST D: Running an incident followed by False Alarm guarantees zero state leakage."""
        agent = self._create_mock_agent(action=IntentAction.NOTIFY_MERCHANT)
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
        api = FinancialIncidentAPI(orchestrator=orchestrator, database=self.db, audit_log=self.audit_log)
        app = create_app(api=api)

        # Run 1: True Incident
        status1, body1 = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )
        self.assertEqual(status1, 200)
        self.assertIsNotNone(body1["agent_response"])
        self.assertIsNotNone(body1["proposed_intent"])

        # Run 2: False Alarm
        status2, body2 = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "false_alarm"},
        )
        self.assertEqual(status2, 200)
        self.assertEqual(body2["status"], "stopped")
        self.assertEqual(body2["final_stage"], "detection")
        self.assertIsNone(body2["agent_response"], "No previous agent_response should leak.")
        self.assertIsNone(body2["proposed_intent"], "No previous proposed_intent should leak.")
        self.assertIsNone(body2["execution_result"], "No previous execution_result should leak.")

    def test_gemini_failure_behavior_remains_intact(self):
        """TEST E: When Gemini fails with rate-limit/error on genuine incident, Stage 3 failure is preserved."""
        from ...agent.provider import LLMRateLimitError

        class FailingProvider(MockLLMProvider):
            def generate_turn(self, messages, tool_schemas, temperature=0.0):
                raise LLMRateLimitError("Gemini API rate limit exceeded: Resource has been exhausted (HTTP 429 RESOURCE_EXHAUSTED)")

        agent = FinancialAgent(provider=FailingProvider(), tools=self.registry)
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
        api = FinancialIncidentAPI(orchestrator=orchestrator, database=self.db, audit_log=self.audit_log)
        app = create_app(api=api)

        status, body = self._call_wsgi(
            app,
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_123", "scenario_id": "upi_failure_spike"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["final_stage"], "agent")
        self.assertTrue(body["is_failed"])
        self.assertFalse(body["is_stopped"])
        self.assertIn("RESOURCE_EXHAUSTED", body["stop_reason"])


if __name__ == "__main__":
    unittest.main()
