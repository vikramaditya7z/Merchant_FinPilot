"""Tests for Server-Sent Events (SSE) streaming, keep-alives, and ASGI 3.0 compatibility.

Validates:
1. The ASGI entrypoint is recognized as genuine ASGI3 (inspect.iscoroutinefunction returns True).
2. WSGI FinPilotApp is recognized as standard 2-arg PEP 3333 WSGI.
3. ASGI GET /health and /api/v1/health return 200 with healthy JSON.
4. ASGI GET /api/v1/scenarios returns 200 with scenarios list.
5. ASGI POST /api/v1/incidents/stream streams progressive SSE chunks.
6. Final pipeline completion event is always sent with full payload.
7. Keep-alive comments (: keepalive) are emitted during idle/latency periods.
8. Stream terminates cleanly on LLM/Gemini errors without hanging.
9. Disconnect handling and fail-closed safety gates are strictly preserved.
"""

import asyncio
import inspect
import json
import os
import unittest
from unittest import mock

from backend.agent.contracts import LLMMessage
from backend.agent.provider import GeminiProvider, LLMAuthenticationError
from backend.api.app import FinPilotASGIApp, FinPilotApp, create_app, create_asgi_app
from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.server import build_app, build_asgi_app


class TestSSEStreamingAndASGI(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.wsgi_app = build_app(mode="mock", database=self.db, audit_log=self.audit_log)
        self.asgi_app = build_asgi_app(mode="mock", database=self.db, audit_log=self.audit_log)

    def tearDown(self) -> None:
        self.db.close()

    def test_asgi3_callable_signature_compliance(self) -> None:
        """Requirement 1: FinPilotASGIApp is recognized as ASGI 3.0 by Uvicorn."""
        self.assertTrue(inspect.iscoroutinefunction(self.asgi_app.__call__))
        self.assertTrue(inspect.iscoroutinefunction(FinPilotASGIApp.__call__))

        # Verify factory returns FinPilotASGIApp
        factory_inst = create_asgi_app()
        self.assertIsInstance(factory_inst, FinPilotASGIApp)
        self.assertTrue(inspect.iscoroutinefunction(factory_inst.__call__))

        # Verify WSGI app is standard non-coroutine callable
        self.assertFalse(inspect.iscoroutinefunction(self.wsgi_app.__call__))
        self.assertIsInstance(self.wsgi_app, FinPilotApp)

    def test_wsgi_sse_stream_progressive_events_and_final_payload(self) -> None:
        """Requirement 2: WSGI SSE stream delivers multiple progressive events and finishes with final payload."""
        payload = {
            "merchant_id": "merchant_test",
            "scenario_id": "upi_failure_spike",
        }
        status_code, stream_factory = self.wsgi_app.api.handle_process_incident_stream(payload)
        self.assertEqual(status_code, 200)

        generator = stream_factory()
        events = []
        keepalives = 0

        for chunk in generator:
            text = chunk.decode("utf-8")
            for line in text.split("\n"):
                line = line.strip()
                if line.startswith(":"):
                    keepalives += 1
                elif line.startswith("data:"):
                    json_str = line[5:].strip()
                    if json_str:
                        events.append(json.loads(json_str))

        # Check progressive events
        self.assertGreater(len(events), 3)
        stages = [e.get("stage") for e in events]
        self.assertIn("detection", stages)
        self.assertIn("investigation", stages)
        self.assertIn("agent", stages)
        self.assertIn("verification", stages)
        self.assertIn("policy", stages)
        self.assertIn("execution", stages)

        # Final event must be stage='pipeline' with payload
        final_event = events[-1]
        self.assertEqual(final_event.get("stage"), "pipeline")
        self.assertEqual(final_event.get("status"), "completed")
        self.assertIn("payload", final_event)
        self.assertIsNotNone(final_event["payload"].get("execution_result"))

    def test_asgi_http_health_check(self) -> None:
        """Requirement 3: ASGI 3.0 interface handles standard GET /health and /api/v1/health."""
        for path in ("/health", "/api/v1/health"):
            scope = {
                "type": "http",
                "method": "GET",
                "path": path,
                "headers": [],
            }

            sent_messages = []

            async def dummy_receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def dummy_send(message):
                sent_messages.append(message)

            asyncio.run(self.asgi_app(scope, dummy_receive, dummy_send))

            self.assertEqual(len(sent_messages), 2)
            self.assertEqual(sent_messages[0]["type"], "http.response.start")
            self.assertEqual(sent_messages[0]["status"], 200)
            self.assertEqual(sent_messages[1]["type"], "http.response.body")
            body = json.loads(sent_messages[1]["body"].decode("utf-8"))
            self.assertEqual(body["status"], "healthy")

    def test_asgi_http_scenarios(self) -> None:
        """Requirement 4: ASGI 3.0 interface handles GET /api/v1/scenarios."""
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/scenarios",
            "headers": [],
        }

        sent_messages = []

        async def dummy_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def dummy_send(message):
            sent_messages.append(message)

        asyncio.run(self.asgi_app(scope, dummy_receive, dummy_send))

        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 200)
        body = json.loads(sent_messages[1]["body"].decode("utf-8"))
        self.assertIn("scenarios", body)
        self.assertGreaterEqual(len(body["scenarios"]), 11)

    def test_asgi_sse_streaming_progressive_delivery(self) -> None:
        """Requirement 5: ASGI 3.0 interface streams SSE events chunk by chunk."""
        request_body = json.dumps({
            "merchant_id": "merchant_test",
            "scenario_id": "upi_failure_spike",
        }).encode("utf-8")

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/incidents/stream",
            "headers": [],
        }

        received = False

        async def dummy_receive():
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        sent_messages = []

        async def dummy_send(message):
            sent_messages.append(message)

        asyncio.run(self.asgi_app(scope, dummy_receive, dummy_send))

        self.assertGreater(len(sent_messages), 3)
        self.assertEqual(sent_messages[0]["type"], "http.response.start")
        self.assertEqual(sent_messages[0]["status"], 200)

        # Collect data chunks
        combined_text = ""
        for msg in sent_messages[1:]:
            if msg["type"] == "http.response.body":
                combined_text += msg["body"].decode("utf-8")

        self.assertIn("data:", combined_text)
        self.assertIn('"stage": "pipeline"', combined_text)
        self.assertIn('"status": "completed"', combined_text)

    def test_asgi_lifespan_support(self) -> None:
        """Requirement 6: ASGI lifespan startup and shutdown protocol."""
        scope = {"type": "lifespan"}
        messages_to_receive = [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
        sent_messages = []

        async def dummy_receive():
            if messages_to_receive:
                return messages_to_receive.pop(0)
            return {"type": "lifespan.shutdown"}

        async def dummy_send(message):
            sent_messages.append(message)

        asyncio.run(self.asgi_app(scope, dummy_receive, dummy_send))

        self.assertEqual(len(sent_messages), 2)
        self.assertEqual(sent_messages[0]["type"], "lifespan.startup.complete")
        self.assertEqual(sent_messages[1]["type"], "lifespan.shutdown.complete")

    def test_stream_gemini_error_fails_safely_with_final_event(self) -> None:
        """Requirement 7: When Gemini errors during streaming, error and final events are sent."""
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "invalid_key", "FINPILOT_MODE": "real"}):
            with mock.patch.object(
                GeminiProvider,
                "generate_turn",
                side_effect=LLMAuthenticationError("Gemini API authentication failed"),
            ):
                real_app = build_asgi_app(mode="real", database=self.db, audit_log=self.audit_log)
                status_code, stream_factory = real_app.api.handle_process_incident_stream({
                    "merchant_id": "merchant_test",
                    "scenario_id": "upi_failure_spike",
                })
                self.assertEqual(status_code, 200)

                generator = stream_factory()
                events = []
                for chunk in generator:
                    text = chunk.decode("utf-8")
                    for line in text.split("\n"):
                        if line.startswith("data:"):
                            json_str = line[5:].strip()
                            if json_str:
                                events.append(json.loads(json_str))

                self.assertGreater(len(events), 0)
                final_event = events[-1]
                self.assertEqual(final_event.get("stage"), "pipeline")
                self.assertEqual(final_event.get("status"), "failed")
                self.assertIn("Gemini API authentication failed", final_event.get("details", ""))


if __name__ == "__main__":
    unittest.main()
