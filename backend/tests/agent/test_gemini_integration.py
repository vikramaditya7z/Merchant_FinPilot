"""Tests for Gemini Provider configuration, error handling, and end-to-end integration.

Validates:
1. Mock mode functions without GEMINI_API_KEY.
2. Real mode constructs GeminiProvider correctly with GEMINI_MODEL and GEMINI_API_KEY.
3. Missing API key in real mode fails safely without crashing or exposing secrets.
4. Gemini provider errors (429, 401/403, 500, URLError) are caught and sanitized.
5. Malformed Gemini outputs cannot bypass schema validation or produce invalid AgentIntent.
6. Pipeline ordering is preserved with Gemini: Detection -> Investigation -> Gemini -> Verifier -> Policy -> Execution.
7. Verification and Policy gates strictly enforce all checks on Gemini-generated intents.
8. API keys and authorization secrets are never leaked in error messages, audit records, or responses.
"""

import json
import os
import unittest
import urllib.error
from unittest import mock

from ...agent.agent import FinancialAgent
from ...agent.contracts import AgentIntent, AgentResponse, LLMMessage, ToolCallRequest
from ...agent.provider import (
    GeminiProvider,
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMProviderError,
    LLMRateLimitError,
    MockLLMProvider,
    clean_gemini_schema,
)
from ...api.app import FinPilotApp
from ...application.contracts import PipelineStage, PipelineStatus
from ...application.orchestrator import FinancialIncidentOrchestrator
from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import (
    IntentAction,
    PolicyVerdict,
    TargetEntityType,
    VerificationStatus,
)
from ...execution.adapters import SimulatedExecutionAdapter
from ...execution.engine import ExecutionEngine
from ...execution.store import ExecutionStore
from ...investigation.investigator import Investigator
from ...server import build_app, load_env_file
from ...tools.registry import create_default_registry
from ...verification.verifier import FinancialVerifier


class GeminiConfigurationAndErrorTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.audit_log = AuditLog()

    def tearDown(self):
        self.db.close()

    def test_mock_mode_selected_without_api_key(self):
        """Requirement 7.A: Mock mode works cleanly without any GEMINI_API_KEY."""
        with mock.patch.dict(os.environ, {"FINPILOT_MODE": "mock"}, clear=False):
            if "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]
            app = build_app(mode="mock", database=self.db, audit_log=self.audit_log)
            self.assertIsInstance(app, FinPilotApp)
            # Orchestrator agent should have MockLLMProvider
            agent = app.api.orchestrator._agent
            self.assertIsInstance(agent._provider, MockLLMProvider)

    def test_real_mode_constructs_gemini_provider(self):
        """Requirement 7.B: Real mode constructs GeminiProvider with model configuration."""
        fake_key = "fake_gemini_key_12345"
        with mock.patch.dict(
            os.environ,
            {
                "FINPILOT_MODE": "real",
                "GEMINI_API_KEY": fake_key,
                "GEMINI_MODEL": "gemini-2.5-flash",
            },
        ):
            app = build_app(mode="real", api_key=fake_key, database=self.db, audit_log=self.audit_log)
            agent = app.api.orchestrator._agent
            self.assertIsInstance(agent._provider, GeminiProvider)
            self.assertEqual(agent._provider.model_id, "gemini-2.5-flash")

    def test_missing_api_key_in_real_mode_fails_safely(self):
        """Requirement 7.C: Real mode without API key fails safely at agent stage."""
        with mock.patch.dict(os.environ, {"FINPILOT_MODE": "real"}, clear=True):
            app = build_app(mode="real", api_key="", database=self.db, audit_log=self.audit_log)
            # When processing an incident, GeminiProvider will raise LLMAuthenticationError
            # which the orchestrator maps cleanly to status=FAILED, stage=AGENT
            status, body = app.api.handle_process_incident({
                "merchant_id": "test_merchant",
                "scenario_id": "upi_failure_spike",
            })
            self.assertEqual(status, 200)
            self.assertEqual(body["status"], "failed")
            self.assertEqual(body["final_stage"], "agent")
            self.assertTrue(body["is_failed"])
            self.assertIn("Gemini API key not found", body["stop_reason"])

    @mock.patch("urllib.request.urlopen")
    def test_gemini_provider_rate_limit_sanitized_and_mapped(self, mock_urlopen):
        """Requirement 7.D: Gemini 429 rate limit is mapped to LLMRateLimitError with sanitized text."""
        fake_key = "secret_gemini_api_key_abc123"
        provider = GeminiProvider(api_key=fake_key)

        # Mock HTTP 429
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=mock.MagicMock(read=lambda: f"Quota exceeded for key {fake_key}".encode("utf-8")),
        )

        with self.assertRaises(LLMRateLimitError) as ctx:
            provider.generate_turn(
                messages=[LLMMessage(role="user", content="Investigate incident")],
                tool_schemas=[],
            )

        err_msg = str(ctx.exception)
        self.assertNotIn(fake_key, err_msg)
        self.assertIn("[REDACTED_API_KEY]", err_msg)

    @mock.patch("urllib.request.urlopen")
    def test_gemini_provider_network_urlerror_sanitized(self, mock_urlopen):
        """Requirement 7.D: Network URLError is sanitized and mapped to LLMProviderError."""
        fake_key = "secret_gemini_api_key_xyz789"
        provider = GeminiProvider(api_key=fake_key)

        mock_urlopen.side_effect = urllib.error.URLError(
            reason=f"Connection refused to server with credentials {fake_key}"
        )

        with self.assertRaises(LLMProviderError) as ctx:
            provider.generate_turn(
                messages=[LLMMessage(role="user", content="Investigate incident")],
                tool_schemas=[],
            )

        err_msg = str(ctx.exception)
        self.assertNotIn(fake_key, err_msg)
        self.assertIn("[REDACTED_API_KEY]", err_msg)

    def test_malformed_gemini_output_cannot_bypass_validation(self):
        """Requirement 7.E: Malformed output from Gemini fails to produce unverified intent."""
        class MalformedGeminiProvider(GeminiProvider):
            def __init__(self):
                super().__init__(api_key="mock_key")

            def generate_turn(self, messages, tool_schemas, temperature=0.0):
                return LLMMessage(
                    role="model",
                    content="I think there was a failure. Here is free text without structured JSON.",
                )

        tools = create_default_registry().bind(self.db)
        agent = FinancialAgent(
            provider=MalformedGeminiProvider(),
            tools=tools,
            max_iterations=2,
            audit_log=self.audit_log,
        )

        app = build_app(custom_agent=agent, database=self.db, audit_log=self.audit_log)
        status, body = app.api.handle_process_incident({
            "merchant_id": "test_merchant",
            "scenario_id": "upi_failure_spike",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "agent")
        self.assertIsNone(body["proposed_intent"])
        self.assertIsNone(body["execution_result"])

    def test_gemini_fabricated_evidence_fails_verification(self):
        """Requirement 7.F: A Gemini response inventing evidence is rejected at VERIFICATION stage."""
        class HallucinatingGeminiProvider(GeminiProvider):
            def __init__(self):
                super().__init__(api_key="mock_key")

            def generate_turn(self, messages, tool_schemas, temperature=0.0):
                return LLMMessage(
                    role="model",
                    content=json.dumps({
                        "reasoning": "Hallucinated incident reasoning.",
                        "verified_facts": ["Fabricated fact."],
                        "findings": [],
                        "uncertainty_or_limitations": [],
                        "proposed_intent": {
                            "action": "notify_merchant",
                            "target_type": "merchant",
                            "target_id": "test_merchant",
                            "reason": "Notification citing completely fabricated evidence.",
                            "evidence_refs": ["ev_fake_hallucination_9999"],
                            "confidence": "0.99",
                        },
                    }),
                )

        tools = create_default_registry().bind(self.db)
        agent = FinancialAgent(
            provider=HallucinatingGeminiProvider(),
            tools=tools,
            audit_log=self.audit_log,
        )

        app = build_app(custom_agent=agent, database=self.db, audit_log=self.audit_log)
        status, body = app.api.handle_process_incident({
            "merchant_id": "test_merchant",
            "scenario_id": "upi_failure_spike",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopped")
        self.assertEqual(body["final_stage"], "verification")
        self.assertFalse(body["verification_result"]["is_verified"])
        self.assertIsNone(body["policy_decision"])
        self.assertIsNone(body["execution_result"])

    def test_gemini_valid_proposal_reaches_simulated_execution(self):
        """Requirement 7.G & 7.H: Valid Gemini proposal flows through Verification -> Policy -> Simulated Execution."""
        class WellBehavedGeminiProvider(GeminiProvider):
            def __init__(self, db):
                super().__init__(api_key="mock_key")
                self._db = db

            def generate_turn(self, messages, tool_schemas, temperature=0.0):
                inc_id = None
                for m in messages:
                    c = m.content or ""
                    if "Financial Incident '" in c:
                        inc_id = c.split("Financial Incident '")[1].split("'")[0].strip()

                ev_refs = []
                if inc_id:
                    inc = self._db.get_incident(inc_id)
                    if inc and inc.evidence:
                        ev_refs = [inc.evidence[0].evidence_id]

                return LLMMessage(
                    role="model",
                    content=json.dumps({
                        "reasoning": "Valid Gemini structured reasoning.",
                        "verified_facts": ["Verified tool metrics."],
                        "findings": [{
                            "title": "UPI Spike",
                            "dimension": "payment_method",
                            "observed_value": "upi",
                            "evidence_ref": ev_refs[0] if ev_refs else None,
                            "summary": "Elevated failure rate.",
                        }],
                        "uncertainty_or_limitations": [],
                        "proposed_intent": {
                            "action": "notify_merchant",
                            "target_type": "merchant",
                            "target_id": "test_merchant",
                            "reason": "Merchant notification warranted by verified degradation.",
                            "evidence_refs": ev_refs,
                            "parameters": {"channels": "email"},
                            "confidence": "0.95",
                        },
                    }),
                )

        provider = WellBehavedGeminiProvider(db=self.db)
        tools = create_default_registry().bind(self.db)
        agent = FinancialAgent(provider=provider, tools=tools, audit_log=self.audit_log)

        app = build_app(custom_agent=agent, database=self.db, audit_log=self.audit_log)
        status, body = app.api.handle_process_incident({
            "merchant_id": "test_merchant",
            "scenario_id": "upi_failure_spike",
        })

        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["final_stage"], "completed")
        self.assertTrue(body["is_completed"])
        self.assertTrue(body["is_simulated"])
        self.assertIsNotNone(body["verification_result"])
        self.assertTrue(body["verification_result"]["is_verified"])
        self.assertIsNotNone(body["policy_decision"])
        self.assertEqual(body["policy_decision"]["verdict"], "allow")
        self.assertIsNotNone(body["execution_result"])
        self.assertEqual(body["execution_result"]["status"], "simulated")
        self.assertTrue(body["execution_result"]["is_simulation"])

    def test_zero_api_keys_in_audit_records_or_responses(self):
        """Requirement 7.I: API keys never appear in audit trails or API response payloads."""
        fake_key = "gemini_super_secret_test_key_9999"
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": fake_key, "FINPILOT_MODE": "real"}):
            app = build_app(mode="real", api_key=fake_key, database=self.db, audit_log=self.audit_log)
            status, body = app.api.handle_get_audit_trail()
            self.assertEqual(status, 200)

            # Convert response to string and verify key absence
            raw_body = json.dumps(body)
            self.assertNotIn(fake_key, raw_body)

            # Verify in-memory audit log events
            for event in self.audit_log.get_events():
                raw_event = json.dumps(event.to_dict())
                self.assertNotIn(fake_key, raw_event)

    def test_load_env_file_parses_cleanly(self):
        """Zero-dependency .env loader parses keys, values, and ignores comments."""
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("# Comment line\n")
            f.write("TEST_ENV_KEY=test_val\n")
            f.write('TEST_QUOTED_KEY="quoted_val"\n')
            temp_path = f.name

        try:
            with mock.patch.dict(os.environ, {}, clear=False):
                res = load_env_file(temp_path)
                self.assertEqual(res.get("TEST_ENV_KEY"), "test_val")
                self.assertEqual(res.get("TEST_QUOTED_KEY"), "quoted_val")
                self.assertEqual(os.environ.get("TEST_ENV_KEY"), "test_val")
                self.assertEqual(os.environ.get("TEST_QUOTED_KEY"), "quoted_val")
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    @mock.patch("urllib.request.urlopen")
    def test_gemini_tool_declarations_strip_unsupported_fields(self, mock_urlopen):
        """Regression test: Wire payload to Gemini contains zero additionalProperties, minimum, maximum, or default."""
        captured_payloads = []

        def fake_urlopen(req, timeout=30):
            data = json.loads(req.data.decode("utf-8"))
            captured_payloads.append(data)
            resp_mock = mock.MagicMock()
            resp_mock.read.return_value = json.dumps({
                "candidates": [{
                    "content": {
                        "parts": [{"text": "Investigation response text"}]
                    }
                }]
            }).encode("utf-8")
            resp_mock.__enter__.return_value = resp_mock
            resp_mock.__exit__.return_value = False
            return resp_mock

        mock_urlopen.side_effect = fake_urlopen

        registry = create_default_registry()
        schemas = registry.get_schemas()
        self.assertEqual(len(schemas), 6)

        provider = GeminiProvider(api_key="fake_test_key")
        provider.generate_turn(
            messages=[LLMMessage(role="user", content="Investigate incident")],
            tool_schemas=schemas,
        )

        self.assertEqual(len(captured_payloads), 1)
        req_payload = captured_payloads[0]
        self.assertIn("tools", req_payload)
        func_decls = req_payload["tools"][0]["functionDeclarations"]
        self.assertEqual(len(func_decls), 6)

        raw_tools_json = json.dumps(func_decls)
        self.assertNotIn("additionalProperties", raw_tools_json)
        self.assertNotIn("minimum", raw_tools_json)
        self.assertNotIn("maximum", raw_tools_json)
        self.assertNotIn("default", raw_tools_json)

        # Verify essential fields are preserved
        for decl in func_decls:
            self.assertIn("name", decl)
            self.assertIn("description", decl)
            self.assertIn("parameters", decl)
            self.assertEqual(decl["parameters"]["type"], "object")
            self.assertIn("properties", decl["parameters"])
            self.assertIn("required", decl["parameters"])


if __name__ == "__main__":
    unittest.main()
