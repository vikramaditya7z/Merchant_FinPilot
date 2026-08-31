"""API service and routing handlers.

PROJECT_RULES 1.6, 10.6 / ARCHITECTURE.md §1-§17.

Handles request validation, dependency routing to the orchestrator, and response shaping.
Contains ZERO business logic, financial arithmetic, or execution side-effects.
"""

import json
import queue
import threading
from datetime import datetime
from typing import Any, Callable, Dict, Generator, Mapping, Optional, Tuple

from ..application.contracts import PipelineResult
from ..application.orchestrator import FinancialIncidentOrchestrator
from ..audit.store import AuditLog
from ..data import ScenarioId, generate_scenario
from ..data.scenarios import SCENARIOS
from ..db.database import Database
from ..detection.live_evaluator import LiveWindowEvaluator
from ..domain.audit import AuditEvent
from ..domain.enums import ComparableWindowMode
from ..domain.errors import DomainValidationError
from ..domain.window import require_utc
from ..financial.engine import build_daily_hourly_baseline, compute_metrics
from .contracts import (
    EvaluateLiveRequest,
    EvaluateLiveResponse,
    ProcessIncidentRequest,
    ProcessIncidentResponse,
    incident_to_dict,
    metrics_to_dict,
)


def audit_event_to_dict(e: AuditEvent) -> Dict[str, Any]:
    """Serialize an AuditEvent to a safe dictionary."""
    return {
        "event_id": e.event_id,
        "sequence": e.sequence,
        "occurred_at": e.occurred_at.isoformat(),
        "actor": e.actor.value,
        "event_type": e.event_type.value,
        "summary": e.summary,
        "incident_id": e.incident_id,
        "subject_id": e.subject_id,
        "payload": dict(e.payload),
        "payload_digest": e.payload_digest,
    }


class FinancialIncidentAPI:
    """The thin HTTP service layer exposing FinPilot application workflows."""

    def __init__(
        self,
        orchestrator: FinancialIncidentOrchestrator,
        database: Optional[Database] = None,
        audit_log: Optional[AuditLog] = None,
        live_evaluator: Optional[LiveWindowEvaluator] = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._db = database
        self._audit_log = audit_log
        self._live_evaluator = live_evaluator or (
            LiveWindowEvaluator(
                database=self._db,
                detector=self._orchestrator.detector,
                orchestrator=self._orchestrator,
            )
            if self._db is not None
            else None
        )

    @property
    def live_evaluator(self) -> Optional[LiveWindowEvaluator]:
        return self._live_evaluator

    @property
    def orchestrator(self) -> FinancialIncidentOrchestrator:
        return self._orchestrator

    @property
    def database(self) -> Optional[Database]:
        return self._db

    @property
    def audit_log(self) -> Optional[AuditLog]:
        return self._audit_log

    def handle_process_incident(self, data: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Validate request and process an incident through the orchestrator."""
        try:
            merchant_id = data.get("merchant_id")
            if not merchant_id or not isinstance(merchant_id, str) or not merchant_id.strip():
                return 400, {"error": "Invalid request: 'merchant_id' must be a non-empty string."}

            incident_id = data.get("incident_id")
            scenario_id_str = data.get("scenario_id")
            context_notes = data.get("context_notes")

            now_val = None
            if data.get("now"):
                try:
                    now_val = datetime.fromisoformat(data["now"])
                except ValueError:
                    return 400, {"error": "Invalid request: 'now' must be a valid ISO-8601 string."}

            request = ProcessIncidentRequest(
                merchant_id=merchant_id.strip(),
                incident_id=incident_id.strip() if incident_id else None,
                scenario_id=scenario_id_str.strip() if scenario_id_str else None,
                context_notes=context_notes,
                now=now_val,
            )
        except DomainValidationError as exc:
            return 400, {"error": f"Invalid request contract: {str(exc)}"}
        except Exception as exc:
            return 400, {"error": f"Bad request: {str(exc)}"}

        try:
            # 1. Scenario-driven execution (synthetic test data)
            if request.scenario_id:
                try:
                    scen_enum = ScenarioId(request.scenario_id.lower())
                except ValueError:
                    valid = [s.value for s in ScenarioId]
                    return 400, {
                        "error": f"Unknown scenario '{request.scenario_id}'. Valid scenarios: {valid}"
                    }

                scen_data = generate_scenario(scen_enum)
                if self._db is not None:
                    self._db.save_payments(scen_data.agent_enriched())

                buckets = build_daily_hourly_baseline(
                    scen_data.agent_enriched(),
                    scen_data.incident_window,
                    scen_data.spec.baseline_days,
                )
                comparable_mode = (
                    ComparableWindowMode.SAME_HOUR_OF_DAY
                    if scen_data.spec.ground_truth.requires_same_hour_baseline
                    else ComparableWindowMode.ALL
                )
                metrics = compute_metrics(
                    scen_data.agent_enriched(),
                    scen_data.incident_window,
                    scen_data.anchor,
                    baseline_windows=buckets,
                    comparable_mode=comparable_mode,
                )

                pipeline_res = self._orchestrator.process_incident(
                    metrics=metrics,
                    payments=scen_data.incident_enriched(),
                    baseline_payments=scen_data.baseline_enriched(),
                    merchant_id=request.merchant_id,
                    now=request.now or scen_data.anchor,
                )

            # 2. Existing Incident execution
            elif request.incident_id:
                if self._db is None:
                    return 500, {"error": "Database not configured; cannot look up incident by ID."}

                incident = self._db.get_incident(request.incident_id)
                if incident is None:
                    return 404, {"error": f"Incident '{request.incident_id}' not found."}

                payments = self._db.list_payments()
                pipeline_res = self._orchestrator.process_incident(
                    incident=incident,
                    payments=payments,
                    merchant_id=request.merchant_id,
                    now=request.now,
                )

            else:
                return 400, {
                    "error": "Invalid request: must provide either 'scenario_id' or 'incident_id'."
                }

            response = ProcessIncidentResponse.from_pipeline_result(pipeline_res)
            return 200, response.to_dict()

        except Exception as exc:
            return 500, {"error": f"Internal server error while processing incident: {str(exc)}"}

    def handle_process_incident_stream(
        self, data: Mapping[str, Any]
    ) -> Tuple[int, Callable[[], Any]]:
        """Validate request and stream real-time pipeline lifecycle progress events via SSE."""
        try:
            merchant_id = data.get("merchant_id")
            if not merchant_id or not isinstance(merchant_id, str) or not merchant_id.strip():
                return 400, lambda: [f"data: {json.dumps({'error': 'Invalid request: merchant_id must be a non-empty string.'})}\n\n".encode("utf-8")]

            incident_id = data.get("incident_id")
            scenario_id_str = data.get("scenario_id")
            context_notes = data.get("context_notes")

            now_val = None
            if data.get("now"):
                try:
                    now_val = datetime.fromisoformat(data["now"])
                except ValueError:
                    return 400, lambda: [f"data: {json.dumps({'error': 'Invalid request: now must be a valid ISO-8601 string.'})}\n\n".encode("utf-8")]

            request = ProcessIncidentRequest(
                merchant_id=merchant_id.strip(),
                incident_id=incident_id.strip() if incident_id else None,
                scenario_id=scenario_id_str.strip() if scenario_id_str else None,
                context_notes=context_notes,
                now=now_val,
            )
        except DomainValidationError as exc:
            return 400, lambda: [f"data: {json.dumps({'error': f'Invalid request contract: {str(exc)}'})}\n\n".encode("utf-8")]
        except Exception as exc:
            return 400, lambda: [f"data: {json.dumps({'error': f'Bad request: {str(exc)}'})}\n\n".encode("utf-8")]

        def event_stream_generator():
            event_queue: queue.Queue = queue.Queue()
            sentinel = object()

            def progress_callback(event_dict: Dict[str, Any]):
                event_queue.put(("event", event_dict))

            def worker():
                try:
                    # 1. Scenario-driven execution
                    if request.scenario_id:
                        try:
                            scen_enum = ScenarioId(request.scenario_id.lower())
                        except ValueError:
                            valid = [s.value for s in ScenarioId]
                            event_queue.put(("error", {
                                "stage": "pipeline",
                                "status": "failed",
                                "timestamp": datetime.now().astimezone().isoformat(),
                                "details": f"Unknown scenario '{request.scenario_id}'. Valid: {valid}",
                            }))
                            return

                        scen_data = generate_scenario(scen_enum)
                        if self._db is not None:
                            self._db.save_payments(scen_data.agent_enriched())

                        buckets = build_daily_hourly_baseline(
                            scen_data.agent_enriched(),
                            scen_data.incident_window,
                            scen_data.spec.baseline_days,
                        )
                        comparable_mode = (
                            ComparableWindowMode.SAME_HOUR_OF_DAY
                            if scen_data.spec.ground_truth.requires_same_hour_baseline
                            else ComparableWindowMode.ALL
                        )
                        metrics = compute_metrics(
                            scen_data.agent_enriched(),
                            scen_data.incident_window,
                            scen_data.anchor,
                            baseline_windows=buckets,
                            comparable_mode=comparable_mode,
                        )

                        pipeline_res = self._orchestrator.process_incident(
                            metrics=metrics,
                            payments=scen_data.incident_enriched(),
                            baseline_payments=scen_data.baseline_enriched(),
                            merchant_id=request.merchant_id,
                            now=request.now or scen_data.anchor,
                            on_progress=progress_callback,
                        )

                    # 2. Existing Incident execution
                    elif request.incident_id:
                        if self._db is None:
                            event_queue.put(("error", {
                                "stage": "pipeline",
                                "status": "failed",
                                "timestamp": datetime.now().astimezone().isoformat(),
                                "details": "Database not configured; cannot look up incident by ID.",
                            }))
                            return

                        incident = self._db.get_incident(request.incident_id)
                        if incident is None:
                            event_queue.put(("error", {
                                "stage": "pipeline",
                                "status": "failed",
                                "timestamp": datetime.now().astimezone().isoformat(),
                                "details": f"Incident '{request.incident_id}' not found.",
                            }))
                            return

                        payments = self._db.list_payments()
                        pipeline_res = self._orchestrator.process_incident(
                            incident=incident,
                            payments=payments,
                            merchant_id=request.merchant_id,
                            now=request.now,
                            on_progress=progress_callback,
                        )
                    else:
                        event_queue.put(("error", {
                            "stage": "pipeline",
                            "status": "failed",
                            "timestamp": datetime.now().astimezone().isoformat(),
                            "details": "Invalid request: must provide either 'scenario_id' or 'incident_id'.",
                        }))
                        return

                    # Build final authoritative response
                    response = ProcessIncidentResponse.from_pipeline_result(pipeline_res)
                    event_queue.put(("final", {
                        "run_id": pipeline_res.run_id,
                        "stage": "pipeline",
                        "status": pipeline_res.status.value,
                        "timestamp": datetime.now().astimezone().isoformat(),
                        "details": pipeline_res.stop_reason,
                        "payload": response.to_dict(),
                    }))

                except Exception as exc:
                    event_queue.put(("error", {
                        "run_id": "run_error",
                        "stage": "pipeline",
                        "status": "failed",
                        "timestamp": datetime.now().astimezone().isoformat(),
                        "details": f"Internal server error: {str(exc)}",
                    }))
                finally:
                    event_queue.put((sentinel, None))

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()

            try:
                while True:
                    try:
                        kind, event_data = event_queue.get(timeout=1.5)
                    except queue.Empty:
                        # SSE keep-alive heartbeat comment to keep connection alive during Gemini reasoning
                        yield b": keepalive\n\n"
                        continue

                    if kind is sentinel:
                        break
                    data_str = json.dumps(event_data)
                    yield f"data: {data_str}\n\n".encode("utf-8")
            except (GeneratorExit, BrokenPipeError, ConnectionResetError):
                pass

        return 200, event_stream_generator

    def handle_get_incident(self, incident_id: str) -> Tuple[int, Dict[str, Any]]:
        """Fetch an existing incident from the repository."""
        if not incident_id or not incident_id.strip():
            return 400, {"error": "Invalid incident_id."}

        if self._db is None:
            return 500, {"error": "Database not configured."}

        incident = self._db.get_incident(incident_id.strip())
        if incident is None:
            return 404, {"error": f"Incident '{incident_id}' not found."}

        return 200, incident_to_dict(incident)

    def handle_get_audit_trail(self, incident_id: Optional[str] = None) -> Tuple[int, Dict[str, Any]]:
        """Fetch cryptographically verified audit trail records."""
        if self._audit_log is None:
            return 200, {"events": [], "count": 0, "is_valid": True}

        events = self._audit_log.get_events(incident_id=incident_id)
        is_valid, errors = self._audit_log.verify_integrity()

        return 200, {
            "events": [audit_event_to_dict(e) for e in events],
            "count": len(events),
            "is_valid": is_valid,
            "verification_errors": list(errors),
        }

    def handle_health(self) -> Tuple[int, Dict[str, Any]]:
        """Return system health check status."""
        return 200, {
            "status": "healthy",
            "service": "merchant-finpilot-api",
            "version": "1.0.0",
            "execution_mode": "test_simulation",
        }

    def handle_list_scenarios(self) -> Tuple[int, Dict[str, Any]]:
        """Return metadata for all 11 registered synthetic scenarios."""
        scenario_list = []
        for scen_id, spec in SCENARIOS.items():
            scenario_list.append({
                "scenario_id": scen_id.value,
                "name": scen_id.value.replace("_", " ").title(),
                "description": spec.description,
                "is_incident": spec.ground_truth.is_incident,
                "has_sufficient_data": spec.ground_truth.has_sufficient_data,
                "expected_action_eligible": spec.ground_truth.expected_action_eligible,
                "expected_root_cause": spec.ground_truth.expected_root_cause,
            })
        return 200, {
            "scenarios": scenario_list,
            "count": len(scenario_list),
        }

    def handle_evaluate_live(self, data: Mapping[str, Any]) -> Tuple[int, Dict[str, Any]]:
        """Evaluate current window from ingested SQLite payments and open/orchestrate incident if anomalous."""
        if self._db is None or self._live_evaluator is None:
            return 500, {"error": "Database not configured; live evaluation requires persistent storage."}

        try:
            merchant_id = data.get("merchant_id", "merchant_default")
            now_str = data.get("now")
            now_val = datetime.fromisoformat(now_str) if now_str else None
            window_hours = int(data.get("window_hours", 1))
            baseline_days = int(data.get("baseline_days", 7))
            auto_orchestrate = bool(data.get("auto_orchestrate", True))

            req = EvaluateLiveRequest(
                merchant_id=merchant_id,
                now=now_val,
                window_hours=window_hours,
                baseline_days=baseline_days,
                auto_orchestrate=auto_orchestrate,
            )
        except DomainValidationError as exc:
            return 400, {"error": f"Invalid request contract: {str(exc)}"}
        except Exception as exc:
            return 400, {"error": f"Bad request: {str(exc)}"}

        try:
            eval_result = self._live_evaluator.evaluate_window(
                merchant_id=req.merchant_id,
                now=req.now,
                window_hours=req.window_hours,
                baseline_days=req.baseline_days,
                auto_orchestrate=req.auto_orchestrate,
            )

            pipe_dict = None
            if eval_result.pipeline_result is not None:
                pipe_dict = ProcessIncidentResponse.from_pipeline_result(eval_result.pipeline_result).to_dict()

            inc_dict = None
            if eval_result.incident is not None:
                inc_dict = incident_to_dict(eval_result.incident)

            response = EvaluateLiveResponse(
                triggered=eval_result.triggered,
                merchant_id=eval_result.merchant_id,
                evaluated_at=eval_result.evaluated_at.isoformat(),
                current_payment_count=eval_result.current_payment_count,
                baseline_payment_count=eval_result.baseline_payment_count,
                window={
                    "start": eval_result.window.start.isoformat(),
                    "end": eval_result.window.end.isoformat(),
                },
                metrics=metrics_to_dict(eval_result.metrics),
                incident=inc_dict,
                pipeline_result=pipe_dict,
            )

            return 200, response.to_dict()
        except Exception as exc:
            return 500, {"error": f"Internal error during live evaluation: {str(exc)}"}
