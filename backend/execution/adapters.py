"""Execution adapters for outbound dispatch.

PROJECT_RULES 6.4, 7.1, 7.5 / ARCHITECTURE.md §12.3, §13.

Adapters isolate external integration. The default SimulatedExecutionAdapter
operates in deterministic test mode without making real-world financial or API calls.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..domain.canonical import short_digest
from ..domain.enums import ExecutionStatus, IntentAction
from .contracts import ExecutionRequest, ExecutionResult


class ExecutionAdapter(ABC):
    """Abstract base class for execution adapters."""

    @abstractmethod
    def execute(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionResult:
        """Perform execution for the authorized request."""
        raise NotImplementedError


class SimulatedExecutionAdapter(ExecutionAdapter):
    """Deterministic simulation adapter for test mode and local verification."""

    def __init__(self, name: str = "simulated_adapter_v1") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def execute(self, request: ExecutionRequest, idempotency_key: str) -> ExecutionResult:
        now = datetime.now().astimezone()
        intent = request.intent
        execution_id = f"sim_exec_{short_digest({'key': idempotency_key, 'intent_id': intent.intent_id})}"

        if intent.action == IntentAction.NOTIFY_MERCHANT:
            ref = f"sim_notif_{short_digest({'key': idempotency_key, 'action': 'notify'})}"
            msg = f"Simulated merchant notification dispatched for incident {intent.incident_id}."
        elif intent.action == IntentAction.RECOMMEND_ONLY:
            ref = f"sim_rec_{short_digest({'key': idempotency_key, 'action': 'recommend'})}"
            msg = "Simulated advisory recommendation recorded for merchant portal."
        elif intent.action == IntentAction.NO_ACTION:
            ref = f"sim_noact_{short_digest({'key': idempotency_key, 'action': 'no_action'})}"
            msg = "Explicit no-action proposal recorded; no side effects created."
        elif intent.action == IntentAction.CREATE_PAYMENT_LINK:
            target_id = intent.target.entity_id if intent.target else "unknown"
            ref = f"sim_plink_{short_digest({'key': idempotency_key, 'target': target_id})}"
            msg = f"Simulated payment link generated for failed transaction {target_id}."
        elif intent.action == IntentAction.ESCALATE_TO_HUMAN:
            ref = f"sim_esc_{short_digest({'key': idempotency_key, 'action': 'escalate'})}"
            msg = "Simulated operations ticket created for merchant support review."
        else:
            return ExecutionResult(
                execution_id=execution_id,
                decision_id=request.decision.decision_id,
                intent_id=intent.intent_id,
                action=intent.action,
                status=ExecutionStatus.FAILED,
                idempotency_key=idempotency_key,
                attempted_at=now,
                completed_at=now,
                is_simulation=True,
                error_code="UNSUPPORTED_ACTION",
                error_message=f"Action '{intent.action.value}' is not supported by SimulatedExecutionAdapter.",
            )

        resp_digest = short_digest({"ref": ref, "msg": msg, "idempotency_key": idempotency_key})

        return ExecutionResult(
            execution_id=execution_id,
            decision_id=request.decision.decision_id,
            intent_id=intent.intent_id,
            action=intent.action,
            status=ExecutionStatus.SIMULATED,
            idempotency_key=idempotency_key,
            attempted_at=now,
            completed_at=now,
            provider_reference=ref,
            response_digest=resp_digest,
            is_simulation=True,
            message=msg,
        )
