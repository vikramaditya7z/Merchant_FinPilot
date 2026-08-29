"""Execution contracts and data models.

PROJECT_RULES 1.4, 7.1-7.8 / ARCHITECTURE.md §13.

Defines:
- Strongly typed execution request and result structures.
- Idempotency key mapping and outcome representations.
- ExecutionStatus wrappers and ActionResult bridge.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ..domain.canonical import short_digest
from ..domain.enums import ExecutionStatus, IntentAction, PolicyVerdict
from ..domain.errors import DomainValidationError
from ..domain.execution import ActionResult, build_execution_key
from ..domain.intent import AgentIntent
from ..domain.policy import PolicyDecision
from ..domain.window import require_utc


@dataclass(frozen=True)
class ExecutionRequest:
    """A strongly typed request to execute an authorized action."""

    decision: PolicyDecision
    intent: AgentIntent
    mode: str = "test"
    requested_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.decision, PolicyDecision):
            raise DomainValidationError("ExecutionRequest.decision must be a PolicyDecision")
        if not isinstance(self.intent, AgentIntent):
            raise DomainValidationError("ExecutionRequest.intent must be an AgentIntent")
        if not isinstance(self.mode, str) or not self.mode.strip():
            raise DomainValidationError("ExecutionRequest.mode must be a non-empty string")
        when = (
            require_utc(self.requested_at, "ExecutionRequest.requested_at")
            if self.requested_at is not None
            else datetime.now().astimezone()
        )
        object.__setattr__(self, "requested_at", when)


@dataclass(frozen=True)
class ExecutionResult:
    """The immutable outcome of an execution attempt."""

    execution_id: str
    decision_id: str
    intent_id: str
    action: IntentAction
    status: ExecutionStatus
    idempotency_key: str
    attempted_at: datetime
    completed_at: Optional[datetime] = None
    provider_reference: Optional[str] = None
    response_digest: Optional[str] = None
    is_simulation: bool = True
    message: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("execution_id", "decision_id", "intent_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"ExecutionResult.{name} must be non-empty string")
        if not isinstance(self.action, IntentAction):
            raise DomainValidationError(f"ExecutionResult.action invalid: {self.action!r}")
        if not isinstance(self.status, ExecutionStatus):
            raise DomainValidationError(f"ExecutionResult.status invalid: {self.status!r}")
        object.__setattr__(
            self, "attempted_at", require_utc(self.attempted_at, "ExecutionResult.attempted_at")
        )
        if self.completed_at is not None:
            completed = require_utc(self.completed_at, "ExecutionResult.completed_at")
            if completed < self.attempted_at:
                raise DomainValidationError("completed_at cannot precede attempted_at")
            object.__setattr__(self, "completed_at", completed)

    @property
    def is_executed(self) -> bool:
        """True ONLY when actual real-world execution succeeded (never for simulation)."""
        return self.status == ExecutionStatus.SUCCEEDED and not self.is_simulation

    @property
    def is_simulated(self) -> bool:
        """True when action was executed under test/simulation mode.

        SKIPPED_DUPLICATE is considered simulated when the original execution
        was a simulation — the outcome is identical and no real-world call was
        re-issued.
        """
        if self.status == ExecutionStatus.SIMULATED:
            return True
        if self.status == ExecutionStatus.SUCCEEDED and self.is_simulation:
            return True
        if self.status == ExecutionStatus.SKIPPED_DUPLICATE and self.is_simulation:
            return True
        return False

    @property
    def is_blocked(self) -> bool:
        return self.status == ExecutionStatus.BLOCKED

    @property
    def is_failed(self) -> bool:
        return self.status in (ExecutionStatus.FAILED, ExecutionStatus.UNKNOWN)

    @property
    def is_duplicate(self) -> bool:
        return self.status == ExecutionStatus.SKIPPED_DUPLICATE

    def to_action_result(self) -> ActionResult:
        """Convert this result to the core domain ActionResult."""
        status_for_domain = self.status
        if status_for_domain == ExecutionStatus.SIMULATED:
            status_for_domain = ExecutionStatus.SIMULATED
        return ActionResult(
            execution_key=self.idempotency_key,
            intent_id=self.intent_id,
            decision_id=self.decision_id,
            action=self.action,
            status=status_for_domain,
            attempted_at=self.attempted_at,
            completed_at=self.completed_at or self.attempted_at,
            provider_reference=self.provider_reference or f"ref_{self.execution_id}",
            response_digest=self.response_digest,
            error_code=self.error_code or ("BLOCKED" if self.is_blocked else None),
            error_message=self.error_message or self.message,
        )
