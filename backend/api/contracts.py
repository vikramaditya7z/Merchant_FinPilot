"""Typed request and response contracts for the HTTP API layer.

PROJECT_RULES 1.6, 2.1-2.7, 4.2, 10.6-10.9 / ARCHITECTURE.md §1-§17.

Guarantees:
- Strict typed request validation before reaching application services.
- Financial integrity: all monetary fields are serialized as integer minor units (paise).
- Decimal rates are serialized as exact strings.
- Undefined rates/fields serialize as null (ADR-004), never coerced to 0.
- Zero secrets or internal credentials leaked in responses.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..application.contracts import PipelineResult, PipelineStage, PipelineStatus
from ..db.serde import metrics_to_json
from ..domain.enums import Currency, IntentAction, PolicyVerdict, VerificationStatus
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialIncident
from ..domain.metrics import FinancialMetrics
from ..domain.window import require_utc


def metrics_to_dict(metrics: FinancialMetrics) -> Dict[str, Any]:
    """Serialize FinancialMetrics to a dictionary."""
    return json.loads(metrics_to_json(metrics))


def incident_to_dict(incident: FinancialIncident) -> Dict[str, Any]:
    """Serialize a FinancialIncident to a dictionary."""
    return {
        "incident_id": incident.incident_id,
        "incident_key": incident.incident_key,
        "merchant_id": incident.merchant_id,
        "incident_type": incident.incident_type.value,
        "status": incident.status.value,
        "severity": incident.severity.value,
        "detected_at": incident.detected_at.isoformat(),
        "window": {
            "start": incident.window.start.isoformat(),
            "end": incident.window.end.isoformat(),
        },
        "primary_dimension": (
            incident.primary_dimension.value if incident.primary_dimension else None
        ),
        "primary_dimension_value": incident.primary_dimension_value,
        "metrics": metrics_to_dict(incident.metrics) if incident.metrics else None,
        "evidence": [
            {
                "evidence_id": ev.evidence_id,
                "summary": ev.summary,
                "computed_at": ev.computed_at.isoformat(),
                "source_confidence": ev.source_confidence.value,
                "dimension": ev.dimension.value if ev.dimension else None,
            }
            for ev in incident.evidence
        ],
    }


@dataclass(frozen=True)
class ProcessIncidentRequest:
    """Strongly typed incoming request to process an incident."""

    merchant_id: str
    incident_id: Optional[str] = None
    scenario_id: Optional[str] = None
    context_notes: Optional[str] = None
    now: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.merchant_id, str) or not self.merchant_id.strip():
            raise DomainValidationError("ProcessIncidentRequest.merchant_id must be a non-empty string")
        if self.incident_id is not None and (not isinstance(self.incident_id, str) or not self.incident_id.strip()):
            raise DomainValidationError("ProcessIncidentRequest.incident_id must be a non-empty string if provided")
        if self.scenario_id is not None and (not isinstance(self.scenario_id, str) or not self.scenario_id.strip()):
            raise DomainValidationError("ProcessIncidentRequest.scenario_id must be a non-empty string if provided")
        if self.now is not None:
            object.__setattr__(self, "now", require_utc(self.now, "ProcessIncidentRequest.now"))


@dataclass(frozen=True)
class ProcessIncidentResponse:
    """Strongly typed, serialization-safe pipeline outcome response."""

    run_id: str
    merchant_id: str
    status: str
    final_stage: str
    started_at: str
    completed_at: str
    is_completed: bool
    is_simulated: bool
    is_stopped: bool
    is_failed: bool
    summary: str
    stop_reason: Optional[str] = None
    incident: Optional[Dict[str, Any]] = None
    investigation_report: Optional[Dict[str, Any]] = None
    agent_response: Optional[Dict[str, Any]] = None
    proposed_intent: Optional[Dict[str, Any]] = None
    verification_result: Optional[Dict[str, Any]] = None
    policy_decision: Optional[Dict[str, Any]] = None
    execution_result: Optional[Dict[str, Any]] = None

    @classmethod
    def from_pipeline_result(cls, res: PipelineResult) -> "ProcessIncidentResponse":
        """Convert an internal domain PipelineResult into a safe API response."""
        inc_dict = incident_to_dict(res.incident) if res.incident else None

        # Format investigation report safely
        inv_dict = None
        if res.investigation_report:
            inv = res.investigation_report
            inv_dict = {
                "incident_id": inv.incident_id,
                "window": {
                    "start": inv.window.start.isoformat(),
                    "end": inv.window.end.isoformat(),
                },
                "investigated_at": inv.investigated_at.isoformat(),
                "has_sufficient_evidence": inv.has_sufficient_evidence,
                "has_multiple_concentrations": inv.has_multiple_concentrations,
                "summary": inv.summary,
                "primary_findings_count": len(inv.primary_findings),
                "secondary_findings_count": len(inv.secondary_findings),
            }

        # Format agent response safely
        agent_dict = None
        if res.agent_response:
            ar = res.agent_response
            agent_dict = {
                "incident_id": ar.incident_id,
                "reasoning": ar.reasoning,
                "verified_facts": list(ar.verified_facts),
                "findings": [
                    {
                        "title": f.title,
                        "dimension": f.dimension,
                        "observed_value": f.observed_value,
                        "evidence_ref": f.evidence_ref,
                        "summary": f.summary,
                    }
                    for f in ar.findings
                ],
                "uncertainty_or_limitations": list(ar.uncertainty_or_limitations),
                "model_id": ar.model_id,
                "prompt_version": ar.prompt_version,
                "iterations_count": ar.iterations_count,
            }

        # Format proposed intent safely
        intent_dict = None
        if res.proposed_intent:
            pi = res.proposed_intent
            intent_dict = {
                "intent_id": pi.intent_id,
                "incident_id": pi.incident_id,
                "action": pi.action.value,
                "reason": pi.reason,
                "target": (
                    {
                        "entity_type": pi.target.entity_type.value,
                        "entity_id": pi.target.entity_id,
                    }
                    if pi.target
                    else None
                ),
                "evidence_refs": list(pi.evidence_refs),
                "claimed_amount": (
                    {
                        "amount_paise": pi.claimed_amount.minor_units,
                        "currency": pi.claimed_amount.currency.value,
                    }
                    if pi.claimed_amount
                    else None
                ),
                "confidence": str(pi.confidence) if pi.confidence is not None else None,
                "parameters": dict(pi.parameters) if pi.parameters else {},
                "content_hash": pi.content_hash(),
            }

        # Format verification result safely
        verif_dict = None
        if res.verification_result:
            vr = res.verification_result
            verif_dict = {
                "phase": vr.phase.value,
                "status": vr.status.value,
                "is_verified": vr.is_verified,
                "is_rejected": vr.status == VerificationStatus.REJECTED,
                "is_inconclusive": vr.status == VerificationStatus.INCONCLUSIVE,
                "summary": vr.summary,
                "verified_at": vr.verified_at.isoformat(),
                "checks_count": len(vr.checks),
                "checks": [
                    {
                        "check_id": c.check_id,
                        "name": c.name,
                        "passed": c.passed,
                        "expected": c.expected,
                        "observed": c.observed,
                        "detail": c.detail,
                    }
                    for c in vr.checks
                ],
            }

        # Format policy decision safely
        policy_dict = None
        if res.policy_decision:
            pd = res.policy_decision
            policy_dict = {
                "decision_id": pd.decision_id,
                "intent_id": pd.intent_id,
                "intent_hash": pd.intent_hash,
                "verdict": pd.verdict.value,
                "authorizes_execution": pd.authorizes_execution,
                "rationale": pd.rationale,
                "evaluated_at": pd.evaluated_at.isoformat(),
                "expires_at": pd.expires_at.isoformat(),
                "rule_set_version": pd.rule_set_version,
                "violations": [
                    {
                        "rule_id": v.rule_id,
                        "rule_version": v.rule_version,
                        "effect": v.effect.value,
                        "message": v.message,
                        "detail": v.detail,
                    }
                    for v in pd.violations
                ],
                "required_approvals": list(pd.required_approvals),
            }

        # Format execution result safely
        exec_dict = None
        if res.execution_result:
            er = res.execution_result
            exec_dict = {
                "execution_id": er.execution_id,
                "decision_id": er.decision_id,
                "intent_id": er.intent_id,
                "action": er.action.value,
                "status": er.status.value,
                "idempotency_key": er.idempotency_key,
                "attempted_at": er.attempted_at.isoformat(),
                "completed_at": er.completed_at.isoformat() if er.completed_at else None,
                "provider_reference": er.provider_reference,
                "response_digest": er.response_digest,
                "is_simulation": er.is_simulation,
                "is_executed": er.is_executed,
                "message": er.message,
                "error_code": er.error_code,
                "error_message": er.error_message,
            }

        return cls(
            run_id=res.run_id,
            merchant_id=res.merchant_id,
            status=res.status.value,
            final_stage=res.final_stage.value,
            started_at=res.started_at.isoformat(),
            completed_at=res.completed_at.isoformat(),
            is_completed=res.is_completed,
            is_simulated=res.is_simulated,
            is_stopped=res.is_stopped,
            is_failed=res.is_failed,
            summary=res.summary,
            stop_reason=res.stop_reason,
            incident=inc_dict,
            investigation_report=inv_dict,
            agent_response=agent_dict,
            proposed_intent=intent_dict,
            verification_result=verif_dict,
            policy_decision=policy_dict,
            execution_result=exec_dict,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize cleanly to JSON-compatible dictionary."""
        return {
            "run_id": self.run_id,
            "merchant_id": self.merchant_id,
            "status": self.status,
            "final_stage": self.final_stage,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "is_completed": self.is_completed,
            "is_simulated": self.is_simulated,
            "is_stopped": self.is_stopped,
            "is_failed": self.is_failed,
            "summary": self.summary,
            "stop_reason": self.stop_reason,
            "incident": self.incident,
            "investigation_report": self.investigation_report,
            "agent_response": self.agent_response,
            "proposed_intent": self.proposed_intent,
            "verification_result": self.verification_result,
            "policy_decision": self.policy_decision,
            "execution_result": self.execution_result,
        }


@dataclass(frozen=True)
class EvaluateLiveRequest:
    """Request contract to evaluate currently ingested database transactions."""

    merchant_id: str = "merchant_default"
    now: Optional[datetime] = None
    window_hours: int = 1
    baseline_days: int = 7
    auto_orchestrate: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.merchant_id, str) or not self.merchant_id.strip():
            raise DomainValidationError("EvaluateLiveRequest.merchant_id must be a non-empty string")
        if isinstance(self.window_hours, bool) or not isinstance(self.window_hours, int) or self.window_hours < 1:
            raise DomainValidationError("EvaluateLiveRequest.window_hours must be a positive int >= 1")
        if isinstance(self.baseline_days, bool) or not isinstance(self.baseline_days, int) or self.baseline_days < 1:
            raise DomainValidationError("EvaluateLiveRequest.baseline_days must be a positive int >= 1")
        if self.now is not None:
            object.__setattr__(self, "now", require_utc(self.now, "EvaluateLiveRequest.now"))


@dataclass(frozen=True)
class EvaluateLiveResponse:
    """Response contract for live window evaluation."""

    triggered: bool
    merchant_id: str
    evaluated_at: str
    current_payment_count: int
    baseline_payment_count: int
    window: Dict[str, str]
    metrics: Dict[str, Any]
    incident: Optional[Dict[str, Any]] = None
    pipeline_result: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "triggered": self.triggered,
            "merchant_id": self.merchant_id,
            "evaluated_at": self.evaluated_at,
            "current_payment_count": self.current_payment_count,
            "baseline_payment_count": self.baseline_payment_count,
            "window": self.window,
            "metrics": self.metrics,
            "incident": self.incident,
            "pipeline_result": self.pipeline_result,
        }
