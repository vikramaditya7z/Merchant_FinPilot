"""The Financial Incident Orchestrator.

PROJECT_RULES 1.4, 1.5 / ARCHITECTURE.md §1-§15.

Coordinates the sequential lifecycle of an incident:
Detection -> Investigation -> Agent -> Verifier -> Policy -> Execution.

Guarantees:
- Pure coordinator: contains zero duplicate business logic, calculations, or policies.
- Fail-closed: security boundary failures immediately halt downstream processing.
- Dependency injection: all engines and components are injected via constructor.
"""

from datetime import datetime
from typing import Any, Callable, Dict, Optional, Sequence

from ..agent.agent import FinancialAgent
from ..agent.contracts import AgentResponse
from ..audit.store import AuditLog
from ..db.database import Database
from ..detection.detector import Detector
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType, IntentAction, PolicyVerdict
from ..domain.incident import FinancialIncident
from ..domain.intent import AgentIntent
from ..domain.metrics import FinancialMetrics
from ..domain.payment import Payment
from ..domain.window import require_utc
from ..execution.engine import ExecutionEngine
from ..investigation.investigator import Investigator
from ..policy.engine import PolicyEngine
from ..verification.verifier import FinancialVerifier
from .contracts import PipelineResult, PipelineStage, PipelineStatus


class FinancialIncidentOrchestrator:
    """Coordinates the deterministic and reasoning components of FinPilot."""

    def __init__(
        self,
        detector: Optional[Detector] = None,
        investigator: Optional[Investigator] = None,
        agent: Optional[FinancialAgent] = None,
        verifier: Optional[FinancialVerifier] = None,
        policy_engine: Optional[PolicyEngine] = None,
        execution_engine: Optional[ExecutionEngine] = None,
        database: Optional[Database] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self._detector = detector or Detector()
        self._investigator = investigator or Investigator()
        self._agent = agent
        self._verifier = verifier or FinancialVerifier(audit_log=audit_log)
        self._policy_engine = policy_engine or PolicyEngine(audit_log=audit_log)
        self._execution_engine = execution_engine or ExecutionEngine(audit_log=audit_log)
        self._database = database
        self._audit_log = audit_log

    @property
    def detector(self) -> Detector:
        return self._detector

    @property
    def investigator(self) -> Investigator:
        return self._investigator

    @property
    def agent(self) -> Optional[FinancialAgent]:
        return self._agent

    @property
    def verifier(self) -> FinancialVerifier:
        return self._verifier

    @property
    def policy_engine(self) -> PolicyEngine:
        return self._policy_engine

    @property
    def execution_engine(self) -> ExecutionEngine:
        return self._execution_engine

    def process_incident(
        self,
        incident: Optional[FinancialIncident] = None,
        metrics: Optional[FinancialMetrics] = None,
        payments: Optional[Sequence[Payment]] = None,
        baseline_payments: Optional[Sequence[Payment]] = None,
        merchant_id: Optional[str] = None,
        now: Optional[datetime] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> PipelineResult:
        """Process an incident through the complete FinPilot pipeline.

        Args:
            incident: An existing FinancialIncident, or None if detection should run.
            metrics: FinancialMetrics to detect against if incident is None.
            payments: Enriched or standard payments for investigation and verification.
            baseline_payments: Historical baseline payments for multi-dimensional comparison.
            merchant_id: Merchant identifier for detection/reporting.
            now: Current timestamp injection.
            on_progress: Optional real-time progress callback for live stage events.

        Returns:
            An immutable PipelineResult detailing the stage reached and outcome.
        """
        started_at = require_utc(now) if now is not None else datetime.now().astimezone()
        m_id = merchant_id or (incident.merchant_id if incident else "unknown_merchant")
        run_id = f"run_{short_digest({'merchant': m_id, 'when': started_at.isoformat()})}"

        def emit_event(
            stage: str,
            status: str,
            details: Optional[str] = None,
            payload: Optional[Dict[str, Any]] = None,
        ) -> None:
            if on_progress is not None:
                try:
                    on_progress({
                        "run_id": run_id,
                        "stage": stage,
                        "status": status,
                        "timestamp": datetime.now().astimezone().isoformat(),
                        "details": details,
                        "payload": payload,
                    })
                except Exception:
                    pass

        self._record_audit(
            event_type=AuditEventType.PIPELINE_STARTED,
            summary=f"Pipeline started for merchant {m_id}",
            incident_id=incident.incident_id if incident else None,
            when=started_at,
            payload={"run_id": run_id, "merchant_id": m_id},
        )

        # -------------------------------------------------------------
        # Stage 1: Detection
        # -------------------------------------------------------------
        emit_event("detection", "running")
        resolved_incident = incident
        if resolved_incident is None:
            if metrics is not None:
                resolved_incident = self._detector.detect(metrics, merchant_id=m_id)

            if resolved_incident is None:
                completed_at = datetime.now().astimezone() if now is None else started_at
                self._record_audit(
                    event_type=AuditEventType.PIPELINE_STOPPED,
                    summary=f"Pipeline stopped at DETECTION for merchant {m_id}: no incident",
                    incident_id=None,
                    when=completed_at,
                    payload={"run_id": run_id, "stage": PipelineStage.DETECTION.value},
                )
                emit_event(
                    "detection",
                    "stopped",
                    details="No financial incident detected under baseline metrics.",
                )
                return PipelineResult(
                    run_id=run_id,
                    merchant_id=m_id,
                    status=PipelineStatus.STOPPED,
                    final_stage=PipelineStage.DETECTION,
                    started_at=started_at,
                    completed_at=completed_at,
                    stop_reason="No financial incident detected under baseline metrics.",
                )

        emit_event(
            "detection",
            "completed",
            details=f"Incident {resolved_incident.incident_id} detected ({resolved_incident.severity.value})",
        )

        if self._database is not None:
            self._database.save_incident(resolved_incident)

        # -------------------------------------------------------------
        # Stage 2: Investigation
        # -------------------------------------------------------------
        emit_event("investigation", "running")
        investigation_report = None
        if payments is not None:
            investigation_report = self._investigator.investigate(
                incident=resolved_incident,
                payments=payments,
                baseline_payments=baseline_payments,
                now=started_at,
            )
            if self._database is not None and investigation_report is not None:
                self._database.save_investigation(investigation_report)
        emit_event("investigation", "completed", details="Dimensional breakdown and baseline comparison completed.")

        # -------------------------------------------------------------
        # Stage 3: Agent Reasoning & Proposal
        # -------------------------------------------------------------
        emit_event("agent", "running")
        if self._agent is None:
            completed_at = datetime.now().astimezone() if now is None else started_at
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary="Pipeline stopped at AGENT: no agent configured",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.AGENT.value},
            )
            emit_event("agent", "stopped", details="No FinancialAgent configured for reasoning stage.")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.AGENT,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                stop_reason="No FinancialAgent configured for reasoning stage.",
            )

        try:
            agent_response: AgentResponse = self._agent.investigate_and_propose(
                incident_id=resolved_incident.incident_id,
                db=self._database,
                now=started_at,
            )
        except Exception as exc:
            completed_at = datetime.now().astimezone() if now is None else started_at
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary=f"Pipeline failed at AGENT: {str(exc)}",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.AGENT.value, "error": str(exc)},
            )
            emit_event("agent", "failed", details=f"Agent execution encountered an unhandled exception: {str(exc)}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.FAILED,
                final_stage=PipelineStage.AGENT,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                stop_reason=f"Agent execution encountered an unhandled exception: {str(exc)}",
            )

        proposed_intent = agent_response.proposed_intent
        if proposed_intent is None:
            completed_at = datetime.now().astimezone() if now is None else started_at
            finding_summary = (
                agent_response.findings[0].summary
                if agent_response.findings
                else agent_response.reasoning
            )
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary="Pipeline stopped at AGENT: finding generated without actionable intent",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.AGENT.value},
            )
            emit_event("agent", "stopped", details=f"Agent produced diagnostic findings without a proposed action intent: {finding_summary}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.AGENT,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                stop_reason=f"Agent produced diagnostic findings without a proposed action intent: {finding_summary}",
            )

        emit_event("agent", "completed", details=f"Proposed action: {proposed_intent.action.value}")

        # -------------------------------------------------------------
        # Stage 4: Financial Verification
        # -------------------------------------------------------------
        emit_event("verification", "running")
        evidence_pool_list = list(resolved_incident.evidence or ())
        if investigation_report and investigation_report.evidence:
            evidence_pool_list.extend(investigation_report.evidence)

        # If proposed intent has missing or empty evidence_refs, bind to real incident evidence
        if (
            proposed_intent is not None
            and proposed_intent.action != IntentAction.NO_ACTION
            and not proposed_intent.evidence_refs
            and evidence_pool_list
        ):
            real_refs = tuple(e.evidence_id for e in evidence_pool_list)
            proposed_intent = AgentIntent(
                intent_id=proposed_intent.intent_id,
                incident_id=proposed_intent.incident_id,
                action=proposed_intent.action,
                reason=proposed_intent.reason,
                proposed_at=proposed_intent.proposed_at,
                model_id=proposed_intent.model_id,
                prompt_version=proposed_intent.prompt_version,
                target=proposed_intent.target,
                parameters=proposed_intent.parameters,
                evidence_refs=real_refs,
                claimed_amount=proposed_intent.claimed_amount,
                confidence=proposed_intent.confidence,
            )

        verified_intent, v_result = self._verifier.verify_and_wrap(
            intent=proposed_intent,
            incident=resolved_incident,
            evidence=tuple(evidence_pool_list),
            payments=payments,
            db=self._database,
            now=started_at,
        )

        if not v_result.is_verified or verified_intent is None:
            completed_at = datetime.now().astimezone() if now is None else started_at
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary=f"Pipeline stopped at VERIFICATION: {v_result.status.value}",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={
                    "run_id": run_id,
                    "stage": PipelineStage.VERIFICATION.value,
                    "status": v_result.status.value,
                },
            )
            emit_event("verification", "blocked", details=f"Financial verification {v_result.status.value.upper()}: {v_result.summary}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.VERIFICATION,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                stop_reason=f"Financial verification {v_result.status.value.upper()}: {v_result.summary}",
            )

        emit_event("verification", "completed", details=f"Verification PASSED ({len(v_result.checks)}/{len(v_result.checks)} checks passed)")

        # -------------------------------------------------------------
        # Stage 5: Policy Authorization
        # -------------------------------------------------------------
        emit_event("policy", "running")
        policy_decision = self._policy_engine.evaluate(verified_intent=verified_intent, now=started_at)

        if policy_decision.verdict == PolicyVerdict.BLOCK:
            completed_at = datetime.now().astimezone() if now is None else started_at
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary="Pipeline stopped at POLICY: verdict BLOCKED",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.POLICY.value, "verdict": "block"},
            )
            emit_event("policy", "blocked", details=f"Policy BLOCKED action: {policy_decision.rationale}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.POLICY,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                verified_intent=verified_intent,
                policy_decision=policy_decision,
                stop_reason=f"Policy BLOCKED action: {policy_decision.rationale}",
            )

        if policy_decision.verdict == PolicyVerdict.ESCALATE:
            completed_at = datetime.now().astimezone() if now is None else started_at
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary="Pipeline stopped at POLICY: verdict ESCALATED",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.POLICY.value, "verdict": "escalate"},
            )
            emit_event("policy", "blocked", details=f"Policy ESCALATED action for human approval: {policy_decision.rationale}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.POLICY,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                verified_intent=verified_intent,
                policy_decision=policy_decision,
                stop_reason=f"Policy ESCALATED action for human approval: {policy_decision.rationale}",
            )

        if policy_decision.verdict != PolicyVerdict.ALLOW:
            completed_at = datetime.now().astimezone() if now is None else started_at
            emit_event("policy", "blocked", details=f"Policy verdict '{policy_decision.verdict.value}' is not ALLOW.")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.STOPPED,
                final_stage=PipelineStage.POLICY,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                verified_intent=verified_intent,
                policy_decision=policy_decision,
                stop_reason=f"Policy verdict '{policy_decision.verdict.value}' is not ALLOW.",
            )

        emit_event("policy", "completed", details="Policy ALLOWED action.")

        # -------------------------------------------------------------
        # Stage 6: Execution
        # -------------------------------------------------------------
        emit_event("execution", "running")
        exec_result = self._execution_engine.execute(
            decision=policy_decision, intent=proposed_intent, now=started_at
        )

        completed_at = datetime.now().astimezone() if now is None else started_at

        if exec_result.is_failed:
            self._record_audit(
                event_type=AuditEventType.PIPELINE_STOPPED,
                summary=f"Pipeline FAILED at EXECUTION: {exec_result.error_message}",
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={"run_id": run_id, "stage": PipelineStage.EXECUTION.value, "error": exec_result.error_message},
            )
            emit_event("execution", "failed", details=f"Execution failed: {exec_result.error_message or exec_result.error_code}")
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.FAILED,
                final_stage=PipelineStage.EXECUTION,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                verified_intent=verified_intent,
                policy_decision=policy_decision,
                execution_result=exec_result,
                stop_reason=f"Execution failed: {exec_result.error_message or exec_result.error_code}",
            )

        emit_event("execution", "completed", details=f"Execution status: {exec_result.status.value}")

        # -------------------------------------------------------------
        # Stage 7: Pipeline Completed
        # -------------------------------------------------------------

        # Determine the appropriate stop_reason and audit summary based on execution outcome.
        # A SKIPPED_DUPLICATE is a legitimate, deliberate idempotent outcome — the action
        # was already executed and should not run again. Pipeline is COMPLETED (correct),
        # but the stop_reason signals that this was idempotent, not a fresh execution.
        if exec_result.is_duplicate:
            duplicate_stop_reason = (
                f"Action '{exec_result.action.value}' was already executed "
                f"(idempotency key matched existing execution). "
                f"{exec_result.message or 'Duplicate suppressed.'}"
            )
            self._record_audit(
                event_type=AuditEventType.PIPELINE_COMPLETED,
                summary=(
                    f"Pipeline COMPLETED (IDEMPOTENT) for merchant {m_id}: "
                    f"duplicate execution suppressed for action {exec_result.action.value}"
                ),
                incident_id=resolved_incident.incident_id,
                when=completed_at,
                payload={
                    "run_id": run_id,
                    "merchant_id": m_id,
                    "action": exec_result.action.value,
                    "execution_status": exec_result.status.value,
                    "idempotent": True,
                },
            )
            return PipelineResult(
                run_id=run_id,
                merchant_id=m_id,
                status=PipelineStatus.COMPLETED,
                final_stage=PipelineStage.COMPLETED,
                started_at=started_at,
                completed_at=completed_at,
                incident=resolved_incident,
                investigation_report=investigation_report,
                agent_response=agent_response,
                proposed_intent=proposed_intent,
                verification_result=v_result,
                verified_intent=verified_intent,
                policy_decision=policy_decision,
                execution_result=exec_result,
                stop_reason=duplicate_stop_reason,
            )

        self._record_audit(
            event_type=AuditEventType.PIPELINE_COMPLETED,
            summary=f"Pipeline COMPLETED for merchant {m_id} -> {exec_result.status.value.upper()}",
            incident_id=resolved_incident.incident_id,
            when=completed_at,
            payload={
                "run_id": run_id,
                "merchant_id": m_id,
                "action": exec_result.action.value,
                "execution_status": exec_result.status.value,
            },
        )

        return PipelineResult(
            run_id=run_id,
            merchant_id=m_id,
            status=PipelineStatus.COMPLETED,
            final_stage=PipelineStage.COMPLETED,
            started_at=started_at,
            completed_at=completed_at,
            incident=resolved_incident,
            investigation_report=investigation_report,
            agent_response=agent_response,
            proposed_intent=proposed_intent,
            verification_result=v_result,
            verified_intent=verified_intent,
            policy_decision=policy_decision,
            execution_result=exec_result,
            stop_reason=None,
        )

    def _record_audit(
        self,
        event_type: AuditEventType,
        summary: str,
        incident_id: Optional[str],
        when: datetime,
        payload: dict,
    ) -> None:
        if self._audit_log is not None:
            self._audit_log.append(
                actor=AuditActor.SYSTEM,
                event_type=event_type,
                summary=summary,
                incident_id=incident_id,
                occurred_at=when,
                payload=payload,
            )
