"""Server startup and dependency wiring for Merchant FinPilot local deployment.

PROJECT_RULES 1.6, 9.11, 10.6-10.9 / ARCHITECTURE.md §1-§17.

Runs pure Python standard library WSGI server without external framework dependencies.
Supports both Offline/Mock mode (default for testing/dev) and Real Gemini Mode (when GEMINI_API_KEY is configured).
"""

import argparse
import json
import os
import sys
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence
from wsgiref.simple_server import WSGIServer, make_server

from .agent.agent import FinancialAgent
from .agent.contracts import LLMMessage
from .agent.provider import GeminiProvider, LLMProvider, MockLLMProvider
from .api.app import FinPilotASGIApp, FinPilotApp, create_app
from .api.router import FinancialIncidentAPI
from .application.orchestrator import FinancialIncidentOrchestrator
from .audit.store import AuditLog
from .db.database import Database
from .detection.detector import Detector
from .domain.enums import IntentAction, TargetEntityType
from .domain.incident import FinancialIncident
from .execution.adapters import ExecutionAdapter, RazorpayExecutionAdapter, SimulatedExecutionAdapter
from .execution.engine import ExecutionEngine
from .execution.store import ExecutionStore
from .investigation.investigator import Investigator
from .policy.engine import PolicyEngine
from .razorpay.service import RazorpayService
from .tools.registry import create_default_registry
from .verification.verifier import FinancialVerifier


def load_env_file(filepath: Optional[str] = None) -> Dict[str, str]:
    """Load key-value pairs from a .env file into os.environ (zero external dependencies)."""
    if filepath is None:
        candidates = [
            os.path.join(os.getcwd(), ".env"),
            os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")),
        ]
        target = None
        for c in candidates:
            if os.path.isfile(c):
                target = c
                break
        if not target:
            return {}
    else:
        target = filepath
        if not os.path.isfile(target):
            return {}

    loaded: Dict[str, str] = {}
    try:
        with open(target, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if (val.startswith('"') and val.endswith('"')) or (
                        val.startswith("'") and val.endswith("'")
                    ):
                        val = val[1:-1]
                    if key not in os.environ:
                        os.environ[key] = val
                    loaded[key] = val
    except Exception:
        pass
    return loaded


def create_default_mock_handler(
    db: Optional[Database] = None,
) -> Callable[[Sequence[LLMMessage], Sequence[Dict[str, Any]]], LLMMessage]:
    """Create a deterministic offline reasoning handler for mock mode."""

    def handler(
        messages: Sequence[LLMMessage], tool_schemas: Sequence[Dict[str, Any]]
    ) -> LLMMessage:
        inc_id: Optional[str] = None
        for m in messages:
            content = m.content or ""
            if m.role == "user" and "Financial Incident '" in content:
                inc_id = content.split("Financial Incident '")[1].split("'")[0].strip()
            elif m.role == "user" and "Incident ID: " in content:
                inc_id = content.split("Incident ID: ")[1].split("\n")[0].strip()

        ev_refs: List[str] = []
        target_id = "test_merchant"
        target_type = TargetEntityType.MERCHANT.value
        action = IntentAction.NOTIFY_MERCHANT.value
        reason = "Automated notification warranted by detected financial degradation over historical baseline."

        if inc_id and db is not None:
            inc = db.get_incident(inc_id)
            if inc is not None:
                target_id = inc.merchant_id or "test_merchant"
                if inc.evidence:
                    ev_refs = [e.evidence_id for e in inc.evidence]

        response_payload = {
            "reasoning": (
                f"Incident '{inc_id or 'unknown'}' demonstrates statistical anomaly against baseline lookback. "
                "Investigation reveals significant concentration of transaction failures."
            ),
            "verified_facts": [
                "Transaction failure rate spiked significantly above baseline expectation."
            ],
            "findings": [
                {
                    "title": "Degradation Concentration",
                    "dimension": "payment_method",
                    "observed_value": "upi",
                    "evidence_ref": ev_refs[0] if ev_refs else None,
                    "summary": "Elevated failure rate detected in primary transaction dimension.",
                }
            ],
            "uncertainty_or_limitations": [
                "Local mock reasoning mode; no external payment rail telemetries queried."
            ],
            "proposed_intent": {
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "reason": reason,
                "evidence_refs": ev_refs,
                "parameters": {"channels": "email,webhook"},
                "confidence": "0.95",
            },
        }

        return LLMMessage(
            role="model",
            content=f"```json\n{json.dumps(response_payload)}\n```",
        )

    return handler


def build_app(
    mode: Optional[str] = None,
    db_path: Optional[str] = None,
    database: Optional[Database] = None,
    api_key: Optional[str] = None,
    custom_agent: Optional[FinancialAgent] = None,
    custom_execution_engine: Optional[ExecutionEngine] = None,
    custom_execution_adapter: Optional[ExecutionAdapter] = None,
    execution_mode: Optional[str] = None,
    audit_log: Optional[AuditLog] = None,
    razorpay_service: Optional[RazorpayService] = None,
    env_file: Optional[str] = None,
) -> FinPilotApp:
    """Construct and wire all application dependencies into a FinPilotApp.

    Args:
        mode: 'mock' for offline testing, 'real' for Google Gemini API. Defaults to env FINPILOT_MODE or 'mock'.
        db_path: SQLite database file path or ':memory:'.
        database: Optional Database instance for dependency injection.
        api_key: Optional Gemini API key (defaults to GEMINI_API_KEY environment variable).
        custom_agent: Optional custom agent instance for test injection.
        custom_execution_engine: Optional custom ExecutionEngine for test injection.
        custom_execution_adapter: Optional custom ExecutionAdapter for test injection.
        execution_mode: 'simulated' or 'razorpay_test'. Defaults to env FINPILOT_EXECUTION_MODE or 'simulated'.
        audit_log: Optional AuditLog instance.
        razorpay_service: Optional RazorpayService instance.
        env_file: Optional path to .env file to load.

    Returns:
        A fully wired FinPilotApp callable.
    """
    load_env_file(env_file)
    db = database or Database(db_path or os.environ.get("FINPILOT_DB_PATH", ":memory:"))
    alog = audit_log or AuditLog()
    detector = Detector()
    investigator = Investigator()
    verifier = FinancialVerifier()
    policy_engine = PolicyEngine()
    exec_store = ExecutionStore()

    env_exec_mode = (os.environ.get("FINPILOT_EXECUTION_MODE") or "simulated").strip().lower()
    eff_exec_mode = execution_mode.strip().lower() if execution_mode else env_exec_mode

    if custom_execution_adapter is not None:
        adapter = custom_execution_adapter
    elif eff_exec_mode == "razorpay_test":
        adapter = RazorpayExecutionAdapter()
    else:
        adapter = SimulatedExecutionAdapter()

    execution_engine = custom_execution_engine or ExecutionEngine(
        adapter=adapter,
        store=exec_store,
        audit_log=alog,
    )

    if api_key is not None:
        effective_api_key = api_key if api_key.strip() else None
    else:
        env_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
        effective_api_key = env_key if env_key else None

    env_mode = (os.environ.get("FINPILOT_MODE") or "").strip().lower()
    if mode is not None:
        configured_mode = mode.strip().lower()
    elif env_mode:
        configured_mode = env_mode
    elif effective_api_key:
        configured_mode = "real"
    else:
        configured_mode = "mock"

    if custom_agent is not None:
        agent = custom_agent
    elif configured_mode == "real":
        gemini_provider = GeminiProvider(api_key=effective_api_key or "")
        registry = create_default_registry()
        bound_tools = registry.bind(db)
        agent = FinancialAgent(
            provider=gemini_provider,
            tools=bound_tools,
            audit_log=alog,
        )
    else:
        # Default Offline/Mock Mode
        mock_provider = MockLLMProvider(handler=create_default_mock_handler(db=db))
        registry = create_default_registry()
        bound_tools = registry.bind(db)
        agent = FinancialAgent(
            provider=mock_provider,
            tools=bound_tools,
            audit_log=alog,
        )

    orchestrator = FinancialIncidentOrchestrator(
        detector=detector,
        investigator=investigator,
        agent=agent,
        verifier=verifier,
        policy_engine=policy_engine,
        execution_engine=execution_engine,
        database=db,
        audit_log=alog,
    )

    rzp_service = razorpay_service or RazorpayService(
        database=db,
        audit_log=alog,
        execution_store=exec_store,
    )

    api = FinancialIncidentAPI(
        orchestrator=orchestrator,
        database=db,
        audit_log=alog,
        razorpay_service=rzp_service,
    )

    return FinPilotApp(api=api)


def build_asgi_app(
    mode: Optional[str] = None,
    db_path: Optional[str] = None,
    database: Optional[Database] = None,
    api_key: Optional[str] = None,
    custom_agent: Optional[FinancialAgent] = None,
    custom_execution_engine: Optional[ExecutionEngine] = None,
    custom_execution_adapter: Optional[ExecutionAdapter] = None,
    execution_mode: Optional[str] = None,
    audit_log: Optional[AuditLog] = None,
    razorpay_service: Optional[RazorpayService] = None,
    env_file: Optional[str] = None,
) -> FinPilotASGIApp:
    """Construct and wire all application dependencies into a dedicated FinPilotASGIApp."""
    wsgi_app = build_app(
        mode=mode,
        db_path=db_path,
        database=database,
        api_key=api_key,
        custom_agent=custom_agent,
        custom_execution_engine=custom_execution_engine,
        custom_execution_adapter=custom_execution_adapter,
        execution_mode=execution_mode,
        audit_log=audit_log,
        razorpay_service=razorpay_service,
        env_file=env_file,
    )
    return FinPilotASGIApp(api=wsgi_app.api)


def __getattr__(name: str) -> Any:
    if name == "app":
        return build_app()
    if name in ("asgi_app", "asgi_application"):
        return build_asgi_app()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def run_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    mode: str = "mock",
    db_path: Optional[str] = None,
) -> WSGIServer:
    """Launch the FinPilot HTTP server locally."""
    app = build_app(mode=mode, db_path=db_path)
    server = make_server(host, port, app)
    print(f"==================================================")
    print(f"Merchant FinPilot API Server")
    print(f"Mode: {mode.upper()}")
    print(f"Serving at: http://{host}:{port}")
    print(f"Health check: http://{host}:{port}/api/v1/health")
    print(f"==================================================")
    return server


def main() -> None:
    """CLI entrypoint for local execution."""
    parser = argparse.ArgumentParser(description="Merchant FinPilot Local API Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default=None,
        help="Operation mode: 'mock' (offline deterministic) or 'real' (Gemini API)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Database path (default: :memory: or FINPILOT_DB_PATH)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="Path to custom .env file (default: searches .env in current and project root)",
    )

    args = parser.parse_args()
    load_env_file(args.env_file)

    mode = args.mode or os.environ.get("FINPILOT_MODE", "mock")
    db_path = args.db or os.environ.get("FINPILOT_DB_PATH", ":memory:")
    server = run_server(host=args.host, port=args.port, mode=mode, db_path=db_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Merchant FinPilot server...")
        server.server_close()


if __name__ == "__main__":
    main()
