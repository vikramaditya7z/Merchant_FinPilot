"""WSGI and ASGI Application interfaces for the FinPilot HTTP API.

PROJECT_RULES 1.6, 9.11, 10.6, 10.8 / ARCHITECTURE.md §1-§17.

Runs with Python standard library alone without requiring external web frameworks.
Exposes both:
1. FinPilotApp (WSGI) for WSGI servers (gunicorn sync/gthread, wsgiref).
2. FinPilotASGIApp (ASGI 3.0) for ASGI servers (uvicorn, gunicorn UvicornWorker).
"""

import asyncio
import json
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs

from ..application.orchestrator import FinancialIncidentOrchestrator
from ..audit.store import AuditLog
from ..db.database import Database
from .router import FinancialIncidentAPI


class FinPilotApp:
    """Standard WSGI Application callable exposing the FinPilot HTTP surface."""

    def __init__(self, api: FinancialIncidentAPI) -> None:
        self._api = api

    @property
    def api(self) -> FinancialIncidentAPI:
        return self._api

    def __call__(self, environ: Dict[str, Any], start_response: Callable) -> List[bytes]:
        """Synchronous PEP 3333 WSGI entrypoint."""
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "/")

        # 0. CORS OPTIONS Preflight
        if method == "OPTIONS":
            return self._send_json(start_response, 200, {"ok": True})

        # 1. Health Check
        if method == "GET" and path in ("/api/v1/health", "/health", "/"):
            status_code, body = self._api.handle_health()
            return self._send_json(start_response, status_code, body)

        # 2. List Supported Scenarios
        if method == "GET" and path in ("/api/v1/scenarios", "/scenarios"):
            status_code, body = self._api.handle_list_scenarios()
            return self._send_json(start_response, status_code, body)

        # 3. Process Incident (Standard HTTP POST)
        if method == "POST" and path == "/api/v1/incidents/process":
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
            except (ValueError, TypeError):
                content_length = 0

            if content_length > 0:
                body_bytes = environ["wsgi.input"].read(content_length)
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                except Exception as exc:
                    return self._send_json(
                        start_response, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                    )
            else:
                payload = {}

            status_code, body = self._api.handle_process_incident(payload)
            return self._send_json(start_response, status_code, body)

        # 3b. Stream Process Incident (Server-Sent Events)
        if path in ("/api/v1/incidents/stream", "/api/v1/incidents/process-stream"):
            if method == "OPTIONS":
                return self._send_json(start_response, 200, {"ok": True})

            if method == "POST":
                try:
                    content_length = int(environ.get("CONTENT_LENGTH", 0))
                except (ValueError, TypeError):
                    content_length = 0

                if content_length > 0:
                    body_bytes = environ["wsgi.input"].read(content_length)
                    try:
                        payload = json.loads(body_bytes.decode("utf-8"))
                    except Exception as exc:
                        return self._send_json(
                            start_response, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                        )
                else:
                    payload = {}
            elif method == "GET":
                query_string = environ.get("QUERY_STRING", "")
                params = parse_qs(query_string)
                payload = {k: v[0] for k, v in params.items() if v}
            else:
                return self._send_json(start_response, 405, {"error": "Method Not Allowed"})

            status_code, stream_factory = self._api.handle_process_incident_stream(payload)
            headers = [
                ("Content-Type", "text/event-stream; charset=utf-8"),
                ("Cache-Control", "no-cache"),
                ("Connection", "keep-alive"),
                ("X-Accel-Buffering", "no"),
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept, Origin, X-Requested-With, X-Razorpay-Signature"),
            ]
            status_text = "200 OK" if status_code == 200 else f"{status_code} Bad Request"
            start_response(status_text, headers)
            return stream_factory()

        # 3c. Evaluate Live Window (Non-Scenario / Database-Driven)
        if method == "POST" and path in ("/api/v1/incidents/evaluate-live", "/api/v1/incidents/live"):
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
            except (ValueError, TypeError):
                content_length = 0

            if content_length > 0:
                body_bytes = environ["wsgi.input"].read(content_length)
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                except Exception as exc:
                    return self._send_json(
                        start_response, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                    )
            else:
                payload = {}

            status_code, body = self._api.handle_evaluate_live(payload)
            return self._send_json(start_response, status_code, body)

        # 3d. Razorpay Webhook Ingestion
        if method == "POST" and path in ("/api/v1/webhooks/razorpay", "/webhooks/razorpay"):
            try:
                content_length = int(environ.get("CONTENT_LENGTH", 0))
            except (ValueError, TypeError):
                content_length = 0

            body_bytes = environ["wsgi.input"].read(content_length) if content_length > 0 else b""
            signature = environ.get("HTTP_X_RAZORPAY_SIGNATURE")
            query_string = environ.get("QUERY_STRING", "")
            params = parse_qs(query_string)
            merchant_id = params.get("merchant_id", [None])[0]

            status_code, body = self._api.handle_razorpay_webhook(
                raw_body=body_bytes,
                signature=signature,
                merchant_id=merchant_id,
            )
            return self._send_json(start_response, status_code, body)

        # 4. Get Incident by ID
        if method == "GET" and path.startswith("/api/v1/incidents/"):
            incident_id = path.split("/api/v1/incidents/", 1)[1].strip()
            if not incident_id:
                return self._send_json(start_response, 400, {"error": "Missing incident_id"})
            status_code, body = self._api.handle_get_incident(incident_id)
            return self._send_json(start_response, status_code, body)

        # 5. Get Audit Trail
        if method == "GET" and path == "/api/v1/audit":
            query_string = environ.get("QUERY_STRING", "")
            params = parse_qs(query_string)
            incident_id = params.get("incident_id", [None])[0]
            status_code, body = self._api.handle_get_audit_trail(incident_id=incident_id)
            return self._send_json(start_response, status_code, body)

        # 404 Not Found fallback
        return self._send_json(
            start_response,
            404,
            {"error": f"Route '{method} {path}' not found on FinPilot API."},
        )

    def _send_json(
        self, start_response: Callable, status_code: int, body: Dict[str, Any]
    ) -> List[bytes]:
        status_text = {
            200: "200 OK",
            400: "400 Bad Request",
            404: "404 Not Found",
            405: "405 Method Not Allowed",
            500: "500 Internal Server Error",
        }.get(status_code, f"{status_code} Status")

        data = json.dumps(body, indent=2).encode("utf-8")
        headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(data))),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept, Origin, X-Requested-With, X-Razorpay-Signature"),
        ]
        start_response(status_text, headers)
        return [data]


class FinPilotASGIApp:
    """Dedicated ASGI 3.0 Application wrapper exposing the FinPilot HTTP & SSE surface."""

    def __init__(self, api: FinancialIncidentAPI) -> None:
        self._api = api

    @property
    def api(self) -> FinancialIncidentAPI:
        return self._api

    async def __call__(
        self, scope: Dict[str, Any], receive: Callable, send: Callable
    ) -> None:
        """Asynchronous ASGI 3.0 entrypoint for Uvicorn / async workers."""
        if scope["type"] == "lifespan":
            while True:
                message = await receive()
                if message["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif message["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    break
            return

        if scope["type"] != "http":
            return

        method = scope.get("method", "GET").upper()
        path = scope.get("path", "/")

        # 0. CORS OPTIONS Preflight
        if method == "OPTIONS":
            await self._send_json(send, 200, {"ok": True})
            return

        # 1. Health Check
        if method == "GET" and path in ("/api/v1/health", "/health", "/"):
            status_code, body = self._api.handle_health()
            await self._send_json(send, status_code, body)
            return

        # 2. List Supported Scenarios
        if method == "GET" and path in ("/api/v1/scenarios", "/scenarios"):
            status_code, body = self._api.handle_list_scenarios()
            await self._send_json(send, status_code, body)
            return

        # 3. Process Incident (Standard HTTP POST)
        if method == "POST" and path == "/api/v1/incidents/process":
            body_bytes = await self._read_body(receive)
            if body_bytes:
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                except Exception as exc:
                    await self._send_json(
                        send, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                    )
                    return
            else:
                payload = {}

            status_code, body = await asyncio.to_thread(
                self._api.handle_process_incident, payload
            )
            await self._send_json(send, status_code, body)
            return

        # 3b. Stream Process Incident (Server-Sent Events)
        if path in ("/api/v1/incidents/stream", "/api/v1/incidents/process-stream"):
            if method == "POST":
                body_bytes = await self._read_body(receive)
                if body_bytes:
                    try:
                        payload = json.loads(body_bytes.decode("utf-8"))
                    except Exception as exc:
                        await self._send_json(
                            send, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                        )
                        return
                else:
                    payload = {}
            elif method == "GET":
                query_string = scope.get("query_string", b"").decode("utf-8")
                params = parse_qs(query_string)
                payload = {k: v[0] for k, v in params.items() if v}
            else:
                await self._send_json(send, 405, {"error": "Method Not Allowed"})
                return

            status_code, stream_factory = self._api.handle_process_incident_stream(payload)
            await send({
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"text/event-stream; charset=utf-8"),
                    (b"cache-control", b"no-cache"),
                    (b"connection", b"keep-alive"),
                    (b"x-accel-buffering", b"no"),
                    (b"access-control-allow-origin", b"*"),
                    (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                    (b"access-control-allow-headers", b"Content-Type, Authorization, Accept, Origin, X-Requested-With, X-Razorpay-Signature"),
                ],
            })

            generator = stream_factory()
            try:
                while True:
                    chunk = await asyncio.to_thread(self._next_chunk, generator)
                    if chunk is None:
                        break
                    await send({
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": True,
                    })
            except Exception:
                pass
            finally:
                await send({
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": False,
                })
            return

        # 3c. Evaluate Live Window (Non-Scenario / Database-Driven)
        if method == "POST" and path in ("/api/v1/incidents/evaluate-live", "/api/v1/incidents/live"):
            body_bytes = await self._read_body(receive)
            if body_bytes:
                try:
                    payload = json.loads(body_bytes.decode("utf-8"))
                except Exception as exc:
                    await self._send_json(
                        send, 400, {"error": f"Invalid JSON payload: {str(exc)}"}
                    )
                    return
            else:
                payload = {}

            status_code, body = await asyncio.to_thread(
                self._api.handle_evaluate_live, payload
            )
            await self._send_json(send, status_code, body)
            return

        # 3d. Razorpay Webhook Ingestion
        if method == "POST" and path in ("/api/v1/webhooks/razorpay", "/webhooks/razorpay"):
            body_bytes = await self._read_body(receive)
            signature = None
            for h_key, h_val in scope.get("headers", []):
                if h_key.lower() == b"x-razorpay-signature":
                    signature = h_val.decode("utf-8", errors="ignore")
                    break

            query_string = scope.get("query_string", b"").decode("utf-8")
            params = parse_qs(query_string)
            merchant_id = params.get("merchant_id", [None])[0]

            status_code, body = await asyncio.to_thread(
                self._api.handle_razorpay_webhook,
                raw_body=body_bytes,
                signature=signature,
                merchant_id=merchant_id,
            )
            await self._send_json(send, status_code, body)
            return

        # 4. Get Incident by ID
        if method == "GET" and path.startswith("/api/v1/incidents/"):
            incident_id = path.split("/api/v1/incidents/", 1)[1].strip()
            if not incident_id:
                await self._send_json(send, 400, {"error": "Missing incident_id"})
                return
            status_code, body = self._api.handle_get_incident(incident_id)
            await self._send_json(send, status_code, body)
            return

        # 5. Get Audit Trail
        if method == "GET" and path == "/api/v1/audit":
            query_string = scope.get("query_string", b"").decode("utf-8")
            params = parse_qs(query_string)
            incident_id = params.get("incident_id", [None])[0]
            status_code, body = self._api.handle_get_audit_trail(incident_id=incident_id)
            await self._send_json(send, status_code, body)
            return

        # 404 Fallback
        await self._send_json(
            send, 404, {"error": f"Route '{method} {path}' not found on FinPilot API."}
        )

    @staticmethod
    def _next_chunk(gen: Any) -> Optional[bytes]:
        try:
            return next(gen)
        except (StopIteration, GeneratorExit):
            return None

    @staticmethod
    async def _read_body(receive: Callable) -> bytes:
        body = b""
        more_body = True
        while more_body:
            message = await receive()
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
        return body

    @staticmethod
    async def _send_json(
        send: Callable, status_code: int, body: Dict[str, Any]
    ) -> None:
        data = json.dumps(body, indent=2).encode("utf-8")
        headers = [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"content-length", str(len(data)).encode("ascii")),
            (b"access-control-allow-origin", b"*"),
            (b"access-control-allow-methods", b"GET, POST, PUT, DELETE, OPTIONS"),
            (b"access-control-allow-headers", b"Content-Type, Authorization, Accept, Origin, X-Requested-With, X-Razorpay-Signature"),
        ]
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": headers,
        })
        await send({
            "type": "http.response.body",
            "body": data,
            "more_body": False,
        })


def create_app(
    api: Optional[FinancialIncidentAPI] = None,
    orchestrator: Optional[FinancialIncidentOrchestrator] = None,
    database: Optional[Database] = None,
    audit_log: Optional[AuditLog] = None,
    agent: Optional[Any] = None,
) -> FinPilotApp:
    """Factory creating a standard WSGI PEP 3333 FinPilot application."""
    if api is not None:
        return FinPilotApp(api=api)

    if orchestrator is not None:
        api_instance = FinancialIncidentAPI(
            orchestrator=orchestrator, database=database, audit_log=audit_log
        )
        return FinPilotApp(api=api_instance)

    # When called as a top-level WSGI factory without an explicit orchestrator,
    # construct the complete application stack with FinancialAgent and GeminiProvider wired.
    from ..server import build_app

    return build_app(
        database=database,
        audit_log=audit_log,
        custom_agent=agent,
    )


def create_asgi_app(
    api: Optional[FinancialIncidentAPI] = None,
    orchestrator: Optional[FinancialIncidentOrchestrator] = None,
    database: Optional[Database] = None,
    audit_log: Optional[AuditLog] = None,
    agent: Optional[Any] = None,
) -> FinPilotASGIApp:
    """Factory creating a dedicated ASGI 3.0 FinPilot application for Uvicorn."""
    if api is not None:
        return FinPilotASGIApp(api=api)

    if orchestrator is not None:
        api_instance = FinancialIncidentAPI(
            orchestrator=orchestrator, database=database, audit_log=audit_log
        )
        return FinPilotASGIApp(api=api_instance)

    from ..server import build_asgi_app

    return build_asgi_app(
        database=database,
        audit_log=audit_log,
        custom_agent=agent,
    )


def __getattr__(name: str) -> Any:
    if name == "app":
        return create_app()
    if name in ("asgi_app", "asgi_application"):
        return create_asgi_app()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
