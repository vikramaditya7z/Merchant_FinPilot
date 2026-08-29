"""Application orchestration contracts and pipeline models.

PROJECT_RULES 1.4 / ARCHITECTURE.md §1-§15.

Defines:
- PipelineStage lifecycle stages.
- PipelineStatus terminal states.
- PipelineResult immutable comprehensive workflow outcome.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional

from ..agent.contracts import AgentResponse
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialIncident
from ..domain.intent import AgentIntent
from ..domain.policy import PolicyDecision
from ..domain.verification import VerificationResult
from ..domain.window import require_utc
from ..execution.contracts import ExecutionResult
from ..investigation import InvestigationReport
from ..verification.contracts import VerifiedIntent


class PipelineStage(str, Enum):
    """The explicit stages of the incident processing pipeline."""

    DETECTION = "detection"
    INVESTIGATION = "investigation"
    AGENT = "agent"
    VERIFICATION = "verification"
    POLICY = "policy"
    EXECUTION = "execution"
    COMPLETED = "completed"


class PipelineStatus(str, Enum):
    """Overall outcome of the pipeline execution."""

    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"


class StageProgressStatus(str, Enum):
    """Execution status of an individual pipeline stage in real time."""

    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass(frozen=True)
class StageProgressEvent:
    """A real-time lifecycle event emitted during live pipeline execution."""

    run_id: str
    stage: str
    status: StageProgressStatus
    timestamp: datetime
    details: Optional[str] = None
    payload: Optional[dict] = None

    def to_dict(self) -> dict:
        d = {
            "run_id": self.run_id,
            "stage": self.stage if isinstance(self.stage, str) else self.stage.value,
            "status": self.status if isinstance(self.status, str) else self.status.value,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.details is not None:
            d["details"] = self.details
        if self.payload is not None:
            d["payload"] = self.payload
        return d


@dataclass(frozen=True)
class PipelineResult:
    """The complete, immutable record of an incident processing run."""

    run_id: str
    merchant_id: str
    status: PipelineStatus
    final_stage: PipelineStage
    started_at: datetime
    completed_at: datetime
    stop_reason: Optional[str] = None
    incident: Optional[FinancialIncident] = None
    investigation_report: Optional[InvestigationReport] = None
    agent_response: Optional[AgentResponse] = None
    proposed_intent: Optional[AgentIntent] = None
    verification_result: Optional[VerificationResult] = None
    verified_intent: Optional[VerifiedIntent] = None
    policy_decision: Optional[PolicyDecision] = None
    execution_result: Optional[ExecutionResult] = None

    def __post_init__(self) -> None:
        for name in ("run_id", "merchant_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"PipelineResult.{name} must be a non-empty string")
        if not isinstance(self.status, PipelineStatus):
            raise DomainValidationError(f"invalid PipelineStatus: {self.status!r}")
        if not isinstance(self.final_stage, PipelineStage):
            raise DomainValidationError(f"invalid PipelineStage: {self.final_stage!r}")
        object.__setattr__(
            self, "started_at", require_utc(self.started_at, "PipelineResult.started_at")
        )
        object.__setattr__(
            self, "completed_at", require_utc(self.completed_at, "PipelineResult.completed_at")
        )
        if self.completed_at < self.started_at:
            raise DomainValidationError("completed_at cannot precede started_at")

    @property
    def is_completed(self) -> bool:
        return self.status == PipelineStatus.COMPLETED

    @property
    def is_stopped(self) -> bool:
        return self.status == PipelineStatus.STOPPED

    @property
    def is_failed(self) -> bool:
        return self.status == PipelineStatus.FAILED

    @property
    def reached_execution(self) -> bool:
        return self.execution_result is not None

    @property
    def is_simulated(self) -> bool:
        """True when the pipeline's execution outcome was simulated or idempotently duplicate-simulated."""
        return self.execution_result is not None and self.execution_result.is_simulated

    @property
    def summary(self) -> str:
        if self.is_completed:
            action_name = self.execution_result.action.value if self.execution_result else "N/A"
            exec_status = self.execution_result.status.value.upper() if self.execution_result else "N/A"
            if self.execution_result is not None and self.execution_result.is_duplicate:
                return (
                    f"Pipeline COMPLETED (IDEMPOTENT): Action '{action_name}' was already executed "
                    f"({exec_status}). Duplicate suppressed; original execution preserved."
                )
            return f"Pipeline COMPLETED: Action '{action_name}' executed ({exec_status})."
        elif self.is_stopped:
            return f"Pipeline STOPPED at stage '{self.final_stage.value}': {self.stop_reason or 'No reason provided'}."
        else:
            return f"Pipeline FAILED at stage '{self.final_stage.value}': {self.stop_reason or 'Unknown error'}."
