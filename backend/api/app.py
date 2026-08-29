"""WSGI Application interface for the FinPilot HTTP API.

PROJECT_RULES 1.6, 9.11, 10.6, 10.8 / ARCHITECTURE.md §1-§17.

Runs with Python standard library alone without requiring external web frameworks.
"""

import json
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from ..application.orchestrator import FinancialIncidentOrchestrator
from ..audit.store import AuditLog
from ..db.database import Database
from .router import FinancialIncidentAPI


class FinPilotApp:
    """WSGI Application callable exposing the FinPilot HTTP surface."""

    def __init__(self, api: FinancialIncidentAPI) -> None:
        self._api = api

    @property
    def api(self) -> FinancialIncidentAPI:
        return self._api

    def __call__(self, environ: Dict[str, Any], start_response: Callable) -> List[bytes]:
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
                ("X-Accel-Buffering", "no"),
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
                ("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept, Origin, X-Requested-With"),
            ]
            status_text = "200 OK" if status_code == 200 else f"{status_code} Bad Request"
            start_response(status_text, headers)
            return stream_factory()

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
            ("Access-Control-Allow-Headers", "Content-Type, Authorization, Accept, Origin, X-Requested-With"),
        ]
        start_response(status_text, headers)
        return [data]


def create_app(
    api: Optional[FinancialIncidentAPI] = None,
    orchestrator: Optional[FinancialIncidentOrchestrator] = None,
    database: Optional[Database] = None,
    audit_log: Optional[AuditLog] = None,
) -> FinPilotApp:
    """Factory creating a standard WSGI-compliant FinPilot HTTP application."""
    if api is not None:
        return FinPilotApp(api=api)

    orch = orchestrator or FinancialIncidentOrchestrator(
        database=database, audit_log=audit_log
    )
    api_instance = FinancialIncidentAPI(
        orchestrator=orch, database=database, audit_log=audit_log
    )
    return FinPilotApp(api=api_instance)
