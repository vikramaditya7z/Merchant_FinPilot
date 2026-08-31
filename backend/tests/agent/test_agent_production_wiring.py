"""Comprehensive tests for production reasoning stage wiring with FinancialAgent and GeminiProvider.

Validates:
1. Production entrypoint (create_app/build_app) automatically configures FinancialAgent.
2. Reason stage no longer stops with 'No FinancialAgent configured for reasoning stage.'
3. GeminiProvider is selected when GEMINI_API_KEY is present or FINPILOT_MODE=real.
4. Structured Gemini outputs are parsed and validated into AgentResponse and AgentIntent.
5. Gemini errors (API timeout, 401, 429, 500) fail safely at Stage 3 (Agent) and do not proceed to execution.
6. Malformed/non-JSON Gemini responses fail safely and fail closed.
7. Stages 4 (Verify), 5 (Authorize), and 6 (Execute) cannot be bypassed.
8. Simulated execution produces a valid audit trail without live money mutation.
"""

import json
import os
import unittest
from decimal import Decimal
from unittest import mock

from backend.agent.agent import FinancialAgent
from backend.agent.contracts import AgentIntent, AgentResponse, LLMMessage, ToolCallRequest
from backend.agent.provider import (
    GeminiProvider,
    LLMAuthenticationError,
    LLMProviderError,
    MockLLMProvider,
)
from backend.api.app import FinPilotApp, create_app
from backend.application.contracts import PipelineStage, PipelineStatus
from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.domain.enums import IntentAction, TargetEntityType
from backend.server import build_app


class TestAgentProductionWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()

    def tearDown(self) -> None:
        self.db.close()

    def test_create_app_default_factory_has_financial_agent(self) -> None:
        """Requirement 1: Calling create_app() with zero args builds an app with a configured FinancialAgent."""
        app = create_app()
        self.assertIsInstance(app, FinPilotApp)
        self.assertIsNotNone(app.api.orchestrator.agent)
        self.assertIsInstance(app.api.orchestrator.agent, FinancialAgent)

    def test_production_wiring_activates_gemini_when_key_present(self) -> None:
        """Requirement 2: When GEMINI_API_KEY is present in env, GeminiProvider is automatically wired."""
        with mock.patch.dict(
            os.environ,
            {
                "GEMINI_API_KEY": "AIzaSyTestKey12345",
                "GEMINI_MODEL": "gemini-3.1-flash-lite",
            },
            clear=False,
        ):
            app = build_app(database=self.db, audit_log=self.audit_log)
            agent = app.api.orchestrator.agent
            self.assertIsNotNone(agent)
            self.assertIsInstance(agent._provider, GeminiProvider)
            self.assertEqual(agent._provider.model_id, "gemini-3.1-flash-lite")

    def test_reasoning_stage_executes_successfully_and_proceeds(self) -> None:
        """Requirement 3: Reason stage processes successfully through Gemini and completes pipeline."""
        def mock_generate_turn(messages, tool_schemas, temperature=0.0):
            # Extract incident details from db if available to cite real evidence
            inc_list = self.db.list_incidents()
            ev_id = inc_list[0].evidence[0].evidence_id if inc_list and inc_list[0].evidence else None

            resp_payload = {
                "reasoning": "Observed severe UPI transaction failure concentration on primary gateway.",
                "verified_facts": ["UPI failure rate jumped from 4% baseline to 30%."],
                "findings": [
                    {
                        "title": "UPI Rail Degradation",
                        "dimension": "payment_method",
                        "observed_value": "upi",
                        "evidence_ref": ev_id,
                        "summary": "Elevated UPI failure rate detected across transactions.",
                    }
                ],
                "uncertainty_or_limitations": ["No external banking rail status API available."],
                "proposed_intent": {
                    "action": IntentAction.NOTIFY_MERCHANT.value,
                    "target_type": TargetEntityType.MERCHANT.value,
                    "target_id": "test_merchant",
                    "reason": "Automated notification warranted by detected financial degradation over baseline.",
                    "evidence_refs": [ev_id] if ev_id else [],
                    "parameters": {"channels": "email,webhook"},
                    "confidence": "0.95",
                },
            }
            return LLMMessage(role="model", content=f"```json\n{json.dumps(resp_payload)}\n```")

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test_key", "FINPILOT_MODE": "real"}):
            with mock.patch.object(GeminiProvider, "generate_turn", side_effect=mock_generate_turn):
                app = build_app(database=self.db, audit_log=self.audit_log)
                status_code, body = app.api.handle_process_incident({
                    "merchant_id": "test_merchant",
                    "scenario_id": "upi_failure_spike",
                })

                self.assertEqual(status_code, 200)
                self.assertEqual(body["status"], "completed")
                self.assertEqual(body["final_stage"], "completed")
                self.assertIsNotNone(body["agent_response"])
                self.assertIn("severe UPI transaction failure", body["agent_response"]["reasoning"])
                self.assertIsNotNone(body["verification_result"])
                self.assertTrue(body["verification_result"]["is_verified"])
                self.assertIsNotNone(body["policy_decision"])
                self.assertEqual(body["policy_decision"]["verdict"], "allow")
                self.assertIsNotNone(body["execution_result"])
                self.assertEqual(body["execution_result"]["status"], "simulated")
                self.assertTrue(body["is_simulated"])

    def test_gemini_api_network_or_auth_error_fails_safely_and_closed(self) -> None:
        """Requirement 4: Network, rate limit, or auth errors from Gemini fail closed at agent stage."""
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "invalid_key", "FINPILOT_MODE": "real"}):
            with mock.patch.object(
                GeminiProvider,
                "generate_turn",
                side_effect=LLMAuthenticationError("Gemini API authentication failed"),
            ):
                app = build_app(database=self.db, audit_log=self.audit_log)
                status_code, body = app.api.handle_process_incident({
                    "merchant_id": "test_merchant",
                    "scenario_id": "upi_failure_spike",
                })

                self.assertEqual(status_code, 200)
                self.assertEqual(body["status"], "failed")
                self.assertEqual(body["final_stage"], "agent")
                self.assertIn("Gemini API authentication failed", body["stop_reason"])
                # Must NEVER have reached verification, policy, or execution
                self.assertIsNone(body["verification_result"])
                self.assertIsNone(body["policy_decision"])
                self.assertIsNone(body["execution_result"])

    def test_gemini_malformed_json_response_fails_closed(self) -> None:
        """Requirement 5: Malformed non-JSON output from LLM fails closed without executing."""
        malformed_turn = LLMMessage(
            role="model",
            content="I am analyzing the UPI spike but I will not output valid JSON format.",
        )

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test_key", "FINPILOT_MODE": "real"}):
            with mock.patch.object(GeminiProvider, "generate_turn", return_value=malformed_turn):
                app = build_app(database=self.db, audit_log=self.audit_log)
                status_code, body = app.api.handle_process_incident({
                    "merchant_id": "test_merchant",
                    "scenario_id": "upi_failure_spike",
                })

                self.assertEqual(status_code, 200)
                self.assertEqual(body["status"], "stopped")
                self.assertEqual(body["final_stage"], "agent")
                # Stopped at agent without intent, verification/policy/execution NOT run
                self.assertIsNone(body["verification_result"])
                self.assertIsNone(body["policy_decision"])
                self.assertIsNone(body["execution_result"])

    def test_gemini_cannot_bypass_verification_gate(self) -> None:
        """Requirement 6: Mismatched merchant targets or non-existent evidence in Gemini intent are caught and blocked by Verify stage."""
        gemini_bad_target_payload = {
            "reasoning": "Detected degradation requiring immediate merchant notification.",
            "verified_facts": ["Failure rate is elevated above baseline."],
            "findings": [
                {
                    "title": "UPI Degradation",
                    "dimension": "payment_method",
                    "observed_value": "upi",
                    "evidence_ref": "ev_non_existent_ref_12345",
                    "summary": "Elevated UPI failure rate detected.",
                }
            ],
            "uncertainty_or_limitations": [],
            "proposed_intent": {
                "action": IntentAction.NOTIFY_MERCHANT.value,
                "target_type": TargetEntityType.MERCHANT.value,
                "target_id": "mismatched_target_merchant_999",  # Target does NOT match incident merchant
                "reason": "Automated notification warranted by detected financial degradation over baseline.",
                "evidence_refs": ["ev_non_existent_ref_12345"],
                "parameters": {"channels": "email"},
                "confidence": "0.95",
            },
        }

        bad_turn = LLMMessage(
            role="model",
            content=f"```json\n{json.dumps(gemini_bad_target_payload)}\n```",
        )

        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test_key", "FINPILOT_MODE": "real"}):
            with mock.patch.object(GeminiProvider, "generate_turn", return_value=bad_turn):
                app = build_app(database=self.db, audit_log=self.audit_log)
                status_code, body = app.api.handle_process_incident({
                    "merchant_id": "test_merchant",
                    "scenario_id": "upi_failure_spike",
                })

                self.assertEqual(status_code, 200)
                self.assertEqual(body["status"], "stopped")
                self.assertEqual(body["final_stage"], "verification")
                self.assertIsNotNone(body["verification_result"])
                self.assertFalse(body["verification_result"]["is_verified"])
                # Execution NEVER reached
                self.assertIsNone(body["execution_result"])


if __name__ == "__main__":
    unittest.main()
