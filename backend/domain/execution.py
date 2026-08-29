"""Execution result contract.

The executor is deliberately stupid (ARCHITECTURE.md 13). This contract exists
so that even a stupid executor produces a complete, honest record.

The important design point is ``ExecutionStatus.UNKNOWN``. A timeout on a
consequential call is **not** a failure — the action may well have succeeded.
Recording it as failure invites a retry, and a retry is how one payment becomes
two. ``UNKNOWN`` is terminal for the executor and escalates
(PROJECT_RULES 7.7).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .canonical import short_digest
from .enums import ExecutionStatus, IntentAction
from .errors import DomainValidationError
from .window import require_utc


def build_execution_key(
    incident_id: str, action: IntentAction, target: Optional[str], parameters: dict
) -> str:
    """Derive the idempotency key for an action.

    Stable and canonical, so the same authorized action always maps to the same
    key regardless of dict ordering or call site (ARCHITECTURE.md 15,
    PROJECT_RULES 7.3/7.4).

    The key is persisted under a unique constraint *before* the outbound call,
    so a crash mid-call cannot produce a second attempt.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise DomainValidationError("incident_id is required for an execution key")
    if not isinstance(action, IntentAction):
        raise DomainValidationError("action must be an IntentAction")
    return "exec_" + short_digest(
        {
            "incident_id": incident_id,
            "action": action.value,
            "target": target,
            "parameters": parameters,
        },
        length=24,
    )


@dataclass(frozen=True)
class ActionResult:
    """The record of one execution attempt.

    Written on every path — success, clean failure, timeout, exception — because
    an unrecorded attempt on a consequential action is the worst possible state
    (PROJECT_RULES 7.6).

    Attributes:
        execution_key: Idempotency key claimed before the call.
        decision_id: The ALLOW decision that authorized this. Required for any
            attempt: no attempt exists without an authorization.
        provider_reference: The external id Razorpay returned, verbatim. This is
            what post-execution verification reads state back against.
        response_digest: Digest of the raw response, for audit without storing
            a payload that might contain sensitive fields.
    """

    execution_key: str
    intent_id: str
    decision_id: str
    action: IntentAction
    status: ExecutionStatus
    attempted_at: datetime
    completed_at: Optional[datetime] = None
    provider_reference: Optional[str] = None
    response_digest: Optional[str] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("execution_key", "intent_id", "decision_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"ActionResult.{name} must be non-empty")
        if not isinstance(self.action, IntentAction):
            raise DomainValidationError(f"invalid IntentAction: {self.action!r}")
        if not isinstance(self.status, ExecutionStatus):
            raise DomainValidationError(f"invalid ExecutionStatus: {self.status!r}")
        object.__setattr__(
            self, "attempted_at", require_utc(self.attempted_at, "ActionResult.attempted_at")
        )
        if self.completed_at is not None:
            completed = require_utc(self.completed_at, "ActionResult.completed_at")
            if completed < self.attempted_at:
                raise DomainValidationError("completed_at cannot precede attempted_at")
            object.__setattr__(self, "completed_at", completed)

        if self.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.SIMULATED):
            if self.completed_at is None:
                raise DomainValidationError(f"{self.status.value.upper()} requires completed_at")
            if not self.provider_reference:
                # Without an external reference there is nothing to verify the
                # outcome against, so we cannot honestly call it a success.
                raise DomainValidationError(
                    f"{self.status.value.upper()} requires a provider_reference so the outcome can be verified"
                )
        if self.status in (ExecutionStatus.FAILED, ExecutionStatus.BLOCKED) and not (self.error_code or self.error_message):
            raise DomainValidationError(f"{self.status.value.upper()} requires an error_code or error_message")

    @property
    def is_ambiguous(self) -> bool:
        """Whether the real-world effect is unknown. Never retry these."""
        return self.status is ExecutionStatus.UNKNOWN

    @property
    def needs_outcome_verification(self) -> bool:
        """A success claim is not proof; an ambiguous result needs the truth."""
        return self.status in (ExecutionStatus.SUCCEEDED, ExecutionStatus.UNKNOWN)

    def __str__(self) -> str:
        return f"ActionResult({self.action.value}, {self.status.value})"
