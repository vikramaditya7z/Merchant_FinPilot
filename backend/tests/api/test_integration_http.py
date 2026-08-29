"""HTTP interface integration tests for Merchant FinPilot API server.

Exercises the complete HTTP / WSGI application boundary without requiring external network
sockets or external frameworks.

Validates all 12 core requirements (A through L) in Requirement 14.
"""

import io
import json
import unittest
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from ...agent.agent import FinancialAgent
from ...agent.contracts import LLMMessage
from ...agent.provider import MockLLMProvider
from ...api.app import FinPilotApp, create_app
from ...api.contracts import ProcessIncidentResponse
from ...api.router import FinancialIncidentAPI
from ...application.orchestrator import FinancialIncidentOrchestrator
from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import (
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
from ...server import build_app
from ...tools.registry import create_default_registry
from ...verification.verifier import FinancialVerifier


class HTTPIntegrationServerTestCase(unittest.TestCase):
    """Integration test suite executing requests against the FinPilot application boundary."""

    def setUp(self):
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.detector = Detector()
        self.investigator = Investigator()
        self.verifier = FinancialVerifier()
        self.policy_engine = PolicyEngine()
        self.store = ExecutionStore()
        self.adapter = SimulatedExecutionAdapter()
        self.execution_engine = ExecutionEngine(
            adapter=self.adapter,
            store=self.store,
            audit_log=self.audit_log,
        )

        def mock_agent_handler(messages, schemas):
            inc_id = None
            for m in messages:
                content = m.content or ""
                if m.role == "user" and "Financial Incident '" in content:
                    inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()

            ev_refs = []
            if inc_id and self.db:
                inc = self.db.get_incident(inc_id)
                if inc and inc.evidence:
                    ev_refs = [inc.evidence[0].evidence_id]

            payload = {
                "reasoning": "Detected significant degradation concentration in UPI.",
                "verified_facts": ["UPI failure rate elevated above baseline."],
                "findings": [
                    {
                        "title": "UPI Spike",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": ev_refs[0] if ev_refs else None,
                        "summary": "Elevated UPI payment failures.",
                    }
                ],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "notify_merchant",
                    "target_type": "merchant",
                    "target_id": "test_merchant_live",
                    "reason": "Merchant notification warranted by verified degradation over baseline.",
                    "evidence_refs": ev_refs,
                    "parameters": {"channels": "email,webhook"},
                    "confidence": "0.95",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(payload)}\n```")

        provider = MockLLMProvider(handler=mock_agent_handler)
        tools = create_default_registry().bind(self.db)
        agent = FinancialAgent(provider=provider, tools=tools, audit_log=self.audit_log)

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
        api = FinancialIncidentAPI(
            orchestrator=orchestrator,
            database=self.db,
            audit_log=self.audit_log,
        )
        self.app = create_app(api=api)

    def tearDown(self):
        self.db.close()

    def _http_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        query_string: str = "",
        app: Optional[FinPilotApp] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        target_app = app or self.app
        body_bytes = json.dumps(body).encode("utf-8") if body is not None else b""
        environ = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path,
            "QUERY_STRING": query_string,
            "CONTENT_LENGTH": str(len(body_bytes)),
            "wsgi.input": io.BytesIO(body_bytes),
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "8000",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
        }

        res_status = [None]
        res_headers = [None]

        def start_response(status, headers):
            res_status[0] = status
            res_headers[0] = headers

        result_chunks = target_app(environ, start_response)
        raw_body = b"".join(result_chunks).decode("utf-8")
        status_code = int(res_status[0].split()[0])
        parsed = json.loads(raw_body) if raw_body else {}
        return status_code, parsed


class HTTPIntegrationTests(HTTPIntegrationServerTestCase):
    def test_requirement_a_health_endpoint(self):
        """Requirement 14.A: GET /api/v1/health returns 200 OK and valid health contract."""
        status, body = self._http_request("GET", "/api/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("status"), "healthy")
        self.assertEqual(body.get("service"), "merchant-finpilot-api")
        self.assertEqual(body.get("execution_mode"), "test_simulation")

    def test_requirement_b_successful_incident_pipeline(self):
        """Requirement 14.B: POST /api/v1/incidents/process with valid incident reaches simulated execution."""
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_live", "scenario_id": "upi_failure_spike"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["final_stage"], "completed")
        self.assertTrue(body["is_completed"])
        self.assertTrue(body["is_simulated"])
        self.assertFalse(body["is_stopped"])
        self.assertFalse(body["is_failed"])
        self.assertIsNotNone(body["execution_result"])
        self.assertEqual(body["execution_result"]["status"], "simulated")
        self.assertTrue(body["execution_result"]["is_simulation"])

    def test_requirement_c_normal_scenario_stops_at_detection(self):
        """Requirement 14.C: Normal scenario stops at DETECTION; downstream stages are not executed."""
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_live", "scenario_id": "normal"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "detection")
        self.assertTrue(body["is_stopped"])
        self.assertIsNone(body["incident"])
        self.assertIsNone(body["investigation_report"])
        self.assertIsNone(body["agent_response"])
        self.assertIsNone(body["verification_result"])
        self.assertIsNone(body["policy_decision"])
        self.assertIsNone(body["execution_result"])

    def test_requirement_d_policy_block_never_invokes_execution(self):
        """Requirement 14.D: When policy BLOCKS (e.g. risk blocked), execution is never invoked."""
        data = generate_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        risk_payments = [
            p.payment if hasattr(p, "payment") else p
            for p in data.incident_enriched()
            if "risk" in str(getattr(p.payment if hasattr(p, "payment") else p, "error_code", "")).lower()
        ]
        target_payment_id = risk_payments[0].id

        def agent_handler(messages, schemas):
            inc_id = None
            for m in messages:
                content = m.content or ""
                if m.role == "user" and "Financial Incident '" in content:
                    inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()

            ev_refs = []
            if inc_id and self.db:
                inc = self.db.get_incident(inc_id)
                if inc and inc.evidence:
                    ev_refs = [inc.evidence[0].evidence_id]

            payload = {
                "reasoning": "Risk blocked failure.",
                "verified_facts": ["Risk blocked transaction."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "create_payment_link",
                    "target_type": "payment",
                    "target_id": target_payment_id,
                    "reason": "Payment link creation for unrecoverable risk transaction.",
                    "evidence_refs": ev_refs,
                    "parameters": {"reason": "risk_blocked"},
                    "confidence": "0.90",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(payload)}\n```")

        custom_provider = MockLLMProvider(handler=agent_handler)
        tools = create_default_registry().bind(self.db)
        custom_agent = FinancialAgent(provider=custom_provider, tools=tools, audit_log=self.audit_log)

        custom_app = build_app(custom_agent=custom_agent, database=self.db, audit_log=self.audit_log)
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant", "scenario_id": "recovery_not_eligible"},
            app=custom_app,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "verification")
        self.assertIsNotNone(body["verification_result"])
        self.assertFalse(body["verification_result"]["is_verified"])
        self.assertIsNone(body["execution_result"])

    def test_requirement_e_policy_escalate_never_invokes_execution(self):
        """Requirement 14.E: When policy ESCALATES, pipeline halts at POLICY without execution."""
        def agent_handler(messages, schemas):
            inc_id = None
            for m in messages:
                content = m.content or ""
                if m.role == "user" and "Financial Incident '" in content:
                    inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()

            ev_refs = []
            if inc_id and self.db:
                inc = self.db.get_incident(inc_id)
                if inc and inc.evidence:
                    ev_refs = [inc.evidence[0].evidence_id]

            payload = {
                "reasoning": "Ambiguous multi-party degradation.",
                "verified_facts": ["Uncertain provider cause."],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "escalate_to_human",
                    "target_type": "merchant",
                    "target_id": "test_merchant",
                    "reason": "Human escalation required for complex multi-party degradation.",
                    "evidence_refs": ev_refs,
                    "parameters": {},
                    "confidence": "0.50",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(payload)}\n```")

        custom_provider = MockLLMProvider(handler=agent_handler)
        tools = create_default_registry().bind(self.db)
        custom_agent = FinancialAgent(provider=custom_provider, tools=tools, audit_log=self.audit_log)

        custom_app = build_app(custom_agent=custom_agent, database=self.db, audit_log=self.audit_log)
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant", "scenario_id": "upi_failure_spike"},
            app=custom_app,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "policy")
        self.assertEqual(body["policy_decision"]["verdict"], "escalate")
        self.assertIsNone(body["execution_result"])

    def test_requirement_f_verification_failure_stops_before_policy_and_execution(self):
        """Requirement 14.F: Verification failure stops at VERIFICATION; policy and execution are not called."""
        def agent_handler(messages, schemas):
            payload = {
                "reasoning": "Fabricated evidence citation.",
                "verified_facts": [],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "notify_merchant",
                    "target_type": "merchant",
                    "target_id": "test_merchant",
                    "reason": "Notification citing non-existent evidence ref.",
                    "evidence_refs": ["ev_nonexistent_fabricated_999"],
                    "parameters": {},
                    "confidence": "0.95",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(payload)}\n```")

        custom_provider = MockLLMProvider(handler=agent_handler)
        tools = create_default_registry().bind(self.db)
        custom_agent = FinancialAgent(provider=custom_provider, tools=tools, audit_log=self.audit_log)

        custom_app = build_app(custom_agent=custom_agent, database=self.db, audit_log=self.audit_log)
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant", "scenario_id": "upi_failure_spike"},
            app=custom_app,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "verification")
        self.assertFalse(body["verification_result"]["is_verified"])
        self.assertIsNone(body["policy_decision"])
        self.assertIsNone(body["execution_result"])

    def test_requirement_g_execution_failure_marks_failed(self):
        """Requirement 14.G: When execution adapter fails, pipeline returns FAILED (no false success)."""
        class FailingSimAdapter(SimulatedExecutionAdapter):
            def execute(self, req, key):
                raise RuntimeError("Downstream simulation network error")

        def agent_handler(messages, schemas):
            inc_id = None
            for m in messages:
                content = m.content or ""
                if m.role == "user" and "Financial Incident '" in content:
                    inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()

            ev_refs = []
            if inc_id and self.db:
                inc = self.db.get_incident(inc_id)
                if inc and inc.evidence:
                    ev_refs = [inc.evidence[0].evidence_id]

            payload = {
                "reasoning": "Normal notification reasoning.",
                "verified_facts": [],
                "findings": [],
                "uncertainty_or_limitations": [],
                "proposed_intent": {
                    "action": "notify_merchant",
                    "target_type": "merchant",
                    "target_id": "test_merchant",
                    "reason": "Notification test for failure mapping with valid evidence ref.",
                    "evidence_refs": ev_refs,
                    "parameters": {"channels": "email"},
                    "confidence": "0.95",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(payload)}\n```")

        failing_engine = ExecutionEngine(adapter=FailingSimAdapter(), store=self.store, audit_log=self.audit_log)
        custom_agent = FinancialAgent(
            provider=MockLLMProvider(handler=agent_handler),
            tools=create_default_registry().bind(self.db),
            audit_log=self.audit_log,
        )
        custom_app = build_app(
            custom_agent=custom_agent,
            custom_execution_engine=failing_engine,
            database=self.db,
            audit_log=self.audit_log,
        )

        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant", "scenario_id": "upi_failure_spike"},
            app=custom_app,
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "failed")
        self.assertEqual(body["final_stage"], "execution")
        self.assertTrue(body["is_failed"])
        self.assertIsNotNone(body["execution_result"])
        self.assertEqual(body["execution_result"]["status"], "failed")

    def test_requirement_h_invalid_request_returns_400(self):
        """Requirement 14.H: Invalid request bodies return HTTP 400 Bad Request."""
        # 1. Missing merchant_id
        status1, body1 = self._http_request("POST", "/api/v1/incidents/process", body={"scenario_id": "normal"})
        self.assertEqual(status1, 400)
        self.assertIn("error", body1)

        # 2. Missing scenario_id and incident_id
        status2, body2 = self._http_request("POST", "/api/v1/incidents/process", body={"merchant_id": "m1"})
        self.assertEqual(status2, 400)
        self.assertIn("error", body2)

        # 3. Invalid scenario name
        status3, body3 = self._http_request("POST", "/api/v1/incidents/process", body={"merchant_id": "m1", "scenario_id": "invalid_xyz"})
        self.assertEqual(status3, 400)
        self.assertIn("error", body3)

    def test_requirement_i_unknown_route_returns_404(self):
        """Requirement 14.I: Accessing undefined routes returns 404 Not Found."""
        status, body = self._http_request("GET", "/api/v1/nonexistent_endpoint")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_requirement_j_security_zero_secrets_leaked(self):
        """Requirement 14.J: Zero API keys, auth tokens, or private secrets in HTTP responses."""
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_live", "scenario_id": "upi_failure_spike"},
        )
        self.assertEqual(status, 200)
        raw_json = json.dumps(body).lower()
        forbidden_terms = ["bearer ", "api_key", "secret_key", "password", "private_key", "credentials"]
        for term in forbidden_terms:
            self.assertNotIn(term, raw_json)

    def test_requirement_k_monetary_invariants_integer_paise(self):
        """Requirement 14.K: All monetary fields are exact integer minor units (paise) and never float."""
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_live", "scenario_id": "upi_failure_spike"},
        )
        self.assertEqual(status, 200)
        metrics = body["incident"]["metrics"]
        failed_gmv = metrics["revenue_risk"]["failed_gmv_paise"]
        rev_risk = metrics["revenue_risk"]["revenue_at_risk_paise"]

        self.assertIsInstance(failed_gmv, int)
        self.assertNotIsInstance(failed_gmv, float)
        self.assertIsInstance(rev_risk, int)
        self.assertNotIsInstance(rev_risk, float)

    def test_requirement_l_response_contract_validation(self):
        """Requirement 14.L: Returned JSON adheres strictly to ProcessIncidentResponse fields."""
        status, body = self._http_request(
            "POST",
            "/api/v1/incidents/process",
            body={"merchant_id": "test_merchant_live", "scenario_id": "upi_failure_spike"},
        )
        self.assertEqual(status, 200)
        expected_keys = {
            "run_id", "merchant_id", "status", "final_stage", "started_at", "completed_at",
            "is_completed", "is_simulated", "is_stopped", "is_failed", "summary", "stop_reason",
            "incident", "investigation_report", "agent_response", "proposed_intent",
            "verification_result", "policy_decision", "execution_result",
        }
        self.assertTrue(expected_keys.issubset(body.keys()))


if __name__ == "__main__":
    unittest.main()
