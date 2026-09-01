"""End-to-end integration tests for Razorpay Webhook -> Ingestion -> Detection -> 6-Stage Pipeline.

PROJECT_RULES 1.4, 1.6, 10.6-10.9 / ARCHITECTURE.md §1-§17.
"""

import asyncio
import hashlib
import hmac
import io
import json
import unittest
from datetime import datetime, timedelta, timezone

from backend.api.app import FinPilotASGIApp, FinPilotApp, create_app, create_asgi_app
from backend.api.router import FinancialIncidentAPI
from backend.application.contracts import PipelineStage, PipelineStatus
from backend.application.orchestrator import FinancialIncidentOrchestrator
from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.detection.detector import Detector
from backend.detection.live_evaluator import LiveWindowEvaluator
from backend.domain.enums import (
    Currency,
    ExecutionStatus,
    IntentAction,
    PaymentMethod,
    PaymentStatus,
    PolicyVerdict,
    VerificationStatus,
)
from backend.domain.window import UTC
from backend.execution.adapters import SimulatedExecutionAdapter
from backend.execution.engine import ExecutionEngine
from backend.execution.store import ExecutionStore
from backend.investigation.investigator import Investigator
from backend.policy.engine import PolicyEngine
from backend.razorpay.config import RazorpayConfig
from backend.razorpay.service import RazorpayService
from backend.razorpay.webhook import RazorpayWebhookHandler
from backend.server import build_app, build_asgi_app
from backend.verification.verifier import FinancialVerifier


class TestRazorpayPipelineIntegration(unittest.TestCase):
    """End-to-end integration test from Razorpay HTTP Webhook to full 6-stage pipeline."""

    def setUp(self) -> None:
        self.secret = "whsec_integration_test_secret_123"
        self.config = RazorpayConfig(
            key_id="rzp_test_key",
            key_secret="rzp_test_secret",
            webhook_secret=self.secret,
        )
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.razorpay_service = RazorpayService(
            config=self.config,
            database=self.db,
            audit_log=self.audit_log,
        )
        self.app = build_app(
            database=self.db,
            audit_log=self.audit_log,
            razorpay_service=self.razorpay_service,
            mode="mock",
        )
        self.asgi_app = build_asgi_app(
            database=self.db,
            audit_log=self.audit_log,
            razorpay_service=self.razorpay_service,
            mode="mock",
        )

    def _sign(self, body_bytes: bytes) -> str:
        return hmac.new(
            self.secret.encode("utf-8"),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()

    def test_wsgi_webhook_endpoint_success(self) -> None:
        """Test POST /api/v1/webhooks/razorpay through WSGI."""
        payload = {
            "entity": "event",
            "event_id": "evt_wsgi_001",
            "event": "payment.captured",
            "created_at": 1700000000,
            "payload": {
                "payment": {
                    "entity": {
                        "id": "pay_wsgi_101",
                        "amount": 50000,
                        "currency": "INR",
                        "status": "captured",
                        "method": "upi",
                        "created_at": 1700000000,
                    }
                }
            }
        }
        raw_body = json.dumps(payload).encode("utf-8")
        sig = self._sign(raw_body)

        status_captured = []
        headers_captured = []

        def start_response(status, headers):
            status_captured.append(status)
            headers_captured.append(headers)

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/v1/webhooks/razorpay",
            "CONTENT_LENGTH": str(len(raw_body)),
            "HTTP_X_RAZORPAY_SIGNATURE": sig,
            "wsgi.input": io.BytesIO(raw_body),
        }

        response_body = self.app(environ, start_response)
        self.assertEqual(status_captured[0], "200 OK")
        res_json = json.loads(response_body[0].decode("utf-8"))
        self.assertEqual(res_json["status"], "processed")
        self.assertEqual(res_json["payment_id"], "pay_wsgi_101")

        # Verify persisted in database
        saved_payments = self.db.list_payments()
        self.assertEqual(len(saved_payments), 1)
        self.assertEqual(saved_payments[0].payment.id, "pay_wsgi_101")
        self.assertEqual(saved_payments[0].payment.status, PaymentStatus.CAPTURED)

    def test_asgi_webhook_endpoint_invalid_signature(self) -> None:
        """Test POST /api/v1/webhooks/razorpay with forged signature over ASGI."""
        payload = {"event": "payment.captured"}
        raw_body = json.dumps(payload).encode("utf-8")

        messages = [
            {"type": "http.request", "body": raw_body, "more_body": False}
        ]
        sent_messages = []

        async def receive():
            return messages.pop(0)

        async def send(msg):
            sent_messages.append(msg)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/webhooks/razorpay",
            "headers": [(b"x-razorpay-signature", b"bad_sig_hex")],
        }

        asyncio.run(self.asgi_app(scope, receive, send))

        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 401)
        body = json.loads(sent_messages[1]["body"].decode("utf-8"))
        self.assertEqual(body["status"], "error")
        self.assertIn("signature verification failed", body["message"])

    def test_razorpay_telemetry_detection_and_full_pipeline(self) -> None:
        """Ingest baseline and an anomalous failure spike via Razorpay webhooks, and run the pipeline."""
        anchor = datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC)

        # 1. Ingest 7 days of normal baseline payments via Razorpay webhook (30 tx/day at 3% failure)
        for day in range(1, 8):
            for i in range(30):
                t = int((anchor - timedelta(days=day, minutes=2 + i * 1.8)).timestamp())
                is_fail = i == 0
                payload = {
                    "event_id": f"evt_base_{day}_{i}",
                    "event": "payment.failed" if is_fail else "payment.captured",
                    "payload": {
                        "payment": {
                            "entity": {
                                "id": f"pay_base_{day}_{i}",
                                "amount": 100000,
                                "currency": "INR",
                                "status": "failed" if is_fail else "captured",
                                "method": "upi",
                                "created_at": t,
                            }
                        }
                    }
                }
                raw = json.dumps(payload).encode("utf-8")
                sig = self._sign(raw)
                code, resp = self.razorpay_service.handle_webhook(raw, sig, merchant_id="merchant_rzp_01")
                self.assertEqual(code, 200)

        # 2. Ingest failure spike in the active window (50 transactions, 30 failures = 60% failure rate)
        for i in range(50):
            t = int((anchor - timedelta(minutes=1 + i * 1.1)).timestamp())
            is_fail = i < 30
            payload = {
                "event_id": f"evt_spike_{i}",
                "event": "payment.failed" if is_fail else "payment.captured",
                "payload": {
                    "payment": {
                        "entity": {
                            "id": f"pay_spike_{i}",
                            "amount": 250000,
                            "currency": "INR",
                            "status": "failed" if is_fail else "captured",
                            "method": "upi",
                            "created_at": t,
                            "error_code": "BAD_REQUEST_ERROR" if is_fail else None,
                            "error_description": "UPI PSP Timeout" if is_fail else None,
                            "error_source": "psp" if is_fail else None,
                        }
                    }
                }
            }
            raw = json.dumps(payload).encode("utf-8")
            sig = self._sign(raw)
            code, resp = self.razorpay_service.handle_webhook(raw, sig, merchant_id="merchant_rzp_01")
            self.assertEqual(code, 200)

        # 3. Evaluate live window directly on ingested Razorpay payments
        live_evaluator = LiveWindowEvaluator(
            database=self.db,
            detector=Detector(),
            orchestrator=self.app.api.orchestrator,
        )

        eval_res = live_evaluator.evaluate_window(
            merchant_id="merchant_rzp_01",
            now=anchor,
            window_hours=1,
            baseline_days=7,
            auto_orchestrate=True,
        )

        # Verify Detection Stage triggered
        self.assertTrue(eval_res.triggered)
        self.assertIsNotNone(eval_res.incident)
        self.assertIsNotNone(eval_res.pipeline_result)

        pipe_res = eval_res.pipeline_result
        self.assertEqual(pipe_res.status, PipelineStatus.COMPLETED)
        self.assertEqual(pipe_res.final_stage, PipelineStage.COMPLETED)

        # Verify Stage 4: Verification Result has 12 checks passed
        self.assertIsNotNone(pipe_res.verification_result)
        self.assertTrue(pipe_res.verification_result.is_verified)
        self.assertEqual(len(pipe_res.verification_result.checks), 12)
        self.assertTrue(all(c.passed for c in pipe_res.verification_result.checks))

        # Verify Stage 5: Policy Decision ALLOWED
        self.assertIsNotNone(pipe_res.policy_decision)
        self.assertEqual(pipe_res.policy_decision.verdict, PolicyVerdict.ALLOW)

        # Verify Stage 6: Execution is SIMULATED and FAIL-CLOSED (No live money mutation)
        self.assertIsNotNone(pipe_res.execution_result)
        self.assertEqual(pipe_res.execution_result.status, ExecutionStatus.SIMULATED)
        self.assertTrue(pipe_res.execution_result.is_simulated)
        self.assertIn("Simulated", pipe_res.execution_result.message)

        # Verify Audit Log recorded all stages
        events = self.audit_log.get_events()
        self.assertGreater(len(events), 5)
        is_valid, errs = self.audit_log.verify_integrity()
        self.assertTrue(is_valid)
        self.assertEqual(len(errs), 0)


if __name__ == "__main__":
    unittest.main()
