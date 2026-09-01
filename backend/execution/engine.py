"""The Deterministic Execution Engine.

PROJECT_RULES 1.4, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7, 7.8 / ARCHITECTURE.md §13.

Guarantees:
- "Execution executes." Sits strictly downstream of PolicyEngine.
- Accepts ONLY an intent authorized by a valid, unexpired PolicyDecision(ALLOW).
- Enforces strict hash matching (decision.intent_hash == intent.content_hash()).
- Enforces deterministic idempotency key derivation and deduplication before outbound calls.
- Enforces fail-closed safety and test-mode simulation tagging.
- Zero reasoning, zero arithmetic, zero policy mutation, zero database tampering.
"""

from datetime import datetime
from typing import Optional

from ..audit.store import AuditLog
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType, ExecutionStatus, IntentAction, PolicyVerdict
from ..domain.execution import build_execution_key
from ..domain.intent import AgentIntent
from ..domain.policy import PolicyDecision
from ..domain.window import require_utc
from .adapters import ExecutionAdapter, SimulatedExecutionAdapter
from .contracts import ExecutionRequest, ExecutionResult
from .store import ExecutionStore


class ExecutionEngine:
    """Deterministic, fail-closed execution engine."""

    def __init__(
        self,
        adapter: Optional[ExecutionAdapter] = None,
        store: Optional[ExecutionStore] = None,
        audit_log: Optional[AuditLog] = None,
        execution_enabled: bool = True,
        razorpay_mode: str = "test",
    ) -> None:
        self._adapter = adapter or SimulatedExecutionAdapter()
        self._store = store or ExecutionStore()
        self._audit_log = audit_log
        self._execution_enabled = execution_enabled
        self._razorpay_mode = razorpay_mode

    @property
    def execution_enabled(self) -> bool:
        return self._execution_enabled

    @property
    def razorpay_mode(self) -> str:
        return self._razorpay_mode

    @property
    def store(self) -> ExecutionStore:
        return self._store

    @property
    def adapter(self) -> ExecutionAdapter:
        return self._adapter

    def execute(
        self,
        decision: PolicyDecision,
        intent: AgentIntent,
        now: Optional[datetime] = None,
    ) -> ExecutionResult:
        """Execute an authorized intent through the configured adapter.

        Args:
            decision: The PolicyDecision authorizing the action (must be ALLOW and unexpired).
            intent: The proposed AgentIntent whose content hash matches the decision.
            now: Current timestamp injection.

        Returns:
            An immutable ExecutionResult detailing the execution outcome.
        """
        when = require_utc(now) if now is not None else datetime.now().astimezone()

        dec_id = getattr(decision, "decision_id", "invalid_decision")
        intent_id = getattr(intent, "intent_id", "invalid_intent")
        action = getattr(intent, "action", IntentAction.NO_ACTION)
        execution_id = f"exec_{short_digest({'decision_id': dec_id, 'intent_id': intent_id, 'when': when.isoformat()})}"

        # 1. Kill Switch Guard
        if not self._execution_enabled:
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=dec_id,
                intent_id=intent_id,
                action=action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_killswitch_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="EXECUTION_DISABLED",
                error_message="Execution kill switch active (execution_enabled=False).",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_BLOCKED,
                actor=AuditActor.EXECUTOR,
                summary=f"Execution blocked by kill switch for intent {intent_id}",
                incident_id=getattr(intent, "incident_id", None),
                subject_id=intent_id,
                when=when,
                payload={"execution_id": execution_id, "reason": result.error_message},
            )
            return result

        # 2. Type & Verdict Validation
        if not isinstance(decision, PolicyDecision):
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=dec_id,
                intent_id=intent_id,
                action=action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_invalid_decision_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="INVALID_DECISION",
                error_message="Execution requires a valid PolicyDecision instance.",
            )

        if not isinstance(intent, AgentIntent):
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=dec_id,
                intent_id=intent_id,
                action=action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_invalid_intent_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="INVALID_INTENT",
                error_message="Execution requires a valid AgentIntent instance.",
            )

        if decision.verdict != PolicyVerdict.ALLOW:
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_verdict_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="POLICY_NOT_ALLOWED",
                error_message=f"Policy verdict '{decision.verdict.value.upper()}' does not authorize execution.",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_BLOCKED,
                actor=AuditActor.EXECUTOR,
                summary=f"Execution blocked by policy verdict {decision.verdict.value} for intent {intent.intent_id}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                when=when,
                payload={"execution_id": execution_id, "verdict": decision.verdict.value},
            )
            return result

        # 3. Decision Freshness (TTL)
        if not decision.is_valid_at(when):
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_expired_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="DECISION_EXPIRED",
                error_message="Policy decision has expired (outside TTL validity window).",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_BLOCKED,
                actor=AuditActor.EXECUTOR,
                summary=f"Execution blocked: expired decision for intent {intent.intent_id}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                when=when,
                payload={"execution_id": execution_id, "expires_at": decision.expires_at.isoformat()},
            )
            return result

        # 4. Intent ID and Hash Integrity Check
        if decision.intent_id != intent.intent_id:
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_id_mismatch_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="INTENT_ID_MISMATCH",
                error_message="Intent ID does not match authorized policy decision intent ID.",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_BLOCKED,
                actor=AuditActor.EXECUTOR,
                summary=f"Execution blocked: ID mismatch for intent {intent.intent_id}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                when=when,
                payload={
                    "execution_id": execution_id,
                    "expected_id": decision.intent_id,
                    "actual_id": intent.intent_id,
                },
            )
            return result

        if not decision.authorizes(intent.content_hash(), when):
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.BLOCKED,
                idempotency_key=f"blocked_hash_mismatch_{execution_id}",
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="INTENT_HASH_MISMATCH",
                error_message="Intent hash does not match authorized policy decision hash.",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_BLOCKED,
                actor=AuditActor.EXECUTOR,
                summary=f"Execution blocked: hash mismatch for intent {intent.intent_id}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                when=when,
                payload={
                    "execution_id": execution_id,
                    "expected_hash": decision.intent_hash,
                    "actual_hash": intent.content_hash(),
                },
            )
            return result

        # 5. Deterministic Idempotency Key Derivation
        target_id = intent.target.entity_id if intent.target is not None else None
        idempotency_key = build_execution_key(
            incident_id=intent.incident_id,
            action=intent.action,
            target=target_id,
            parameters=intent.parameters or {},
        )

        # 6. Duplicate / Idempotency Check
        existing_result = self._store.get(idempotency_key)
        if existing_result is not None:
            dup_result = ExecutionResult(
                execution_id=f"dup_{existing_result.execution_id}",
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.SKIPPED_DUPLICATE,
                idempotency_key=idempotency_key,
                attempted_at=when,
                completed_at=when,
                provider_reference=existing_result.provider_reference,
                response_digest=existing_result.response_digest,
                is_simulation=existing_result.is_simulation,
                message=f"Duplicate execution suppressed; existing execution is {existing_result.execution_id}",
            )
            self._record_audit_event(
                event_type=AuditEventType.EXECUTION_DUPLICATE,
                actor=AuditActor.EXECUTOR,
                summary=f"Duplicate execution suppressed for intent {intent.intent_id}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                when=when,
                payload={"idempotency_key": idempotency_key, "original_execution_id": existing_result.execution_id},
            )
            return dup_result

        # 7. Audit Pre-Execution Attempt
        self._record_audit_event(
            event_type=AuditEventType.ACTION_ATTEMPTED,
            actor=AuditActor.EXECUTOR,
            summary=f"Executor attempting action {intent.action.value} for intent {intent.intent_id}",
            incident_id=intent.incident_id,
            subject_id=intent.intent_id,
            when=when,
            payload={
                "execution_id": execution_id,
                "decision_id": decision.decision_id,
                "action": intent.action.value,
                "idempotency_key": idempotency_key,
                "mode": self._razorpay_mode,
            },
        )

        # 8. Dispatch to Adapter
        request = ExecutionRequest(
            decision=decision,
            intent=intent,
            mode=self._razorpay_mode,
            requested_at=when,
        )

        try:
            result = self._adapter.execute(request=request, idempotency_key=idempotency_key)
        except Exception as exc:
            result = ExecutionResult(
                execution_id=execution_id,
                decision_id=decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=when,
                completed_at=when,
                is_simulation=True,
                error_code="ADAPTER_ERROR",
                error_message=str(exc),
            )

        # 9. Persist Result in Idempotency Store
        self._store.save(result)

        # 10. Audit Post-Execution Result
        event_type = (
            AuditEventType.ACTION_RESULT_RECORDED
            if result.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.SIMULATED)
            else AuditEventType.EXECUTION_FAILED
        )
        self._record_audit_event(
            event_type=event_type,
            actor=AuditActor.EXECUTOR,
            summary=f"Executor completed {intent.action.value} -> {result.status.value.upper()}",
            incident_id=intent.incident_id,
            subject_id=intent.intent_id,
            when=when,
            payload={
                "execution_id": result.execution_id,
                "decision_id": decision.decision_id,
                "action": intent.action.value,
                "status": result.status.value,
                "provider_reference": result.provider_reference,
                "response_digest": result.response_digest,
                "is_simulation": result.is_simulation,
            },
        )

        return result

    def _record_audit_event(
        self,
        event_type: AuditEventType,
        actor: AuditActor,
        summary: str,
        incident_id: Optional[str],
        subject_id: Optional[str],
        when: datetime,
        payload: dict,
    ) -> None:
        if self._audit_log is not None:
            self._audit_log.append(
                actor=actor,
                event_type=event_type,
                summary=summary,
                incident_id=incident_id,
                subject_id=subject_id,
                occurred_at=when,
                payload=payload,
            )
