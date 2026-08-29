"""Typed contracts and containers for the Financial Verifier.

PROJECT_RULES 1.3, 1.4, 8.5, 8.6, 8.7 / ARCHITECTURE.md §10.

Defines:
- VerifiedIntent: The verified container passed to the Policy Engine.
- Check ID constants for auditable, deterministic verification checks.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from ..domain.enums import VerificationPhase, VerificationStatus
from ..domain.errors import DomainValidationError
from ..domain.intent import AgentIntent
from ..domain.money import Money
from ..domain.verification import VerificationCheck, VerificationResult
from ..domain.window import require_utc

# Check Name Constants
CHK_INTENT_SCHEMA = "chk_intent_schema"
CHK_INCIDENT_EXISTS = "chk_incident_exists"
CHK_INCIDENT_ACTIVE = "chk_incident_active"
CHK_ACTION_SUPPORTED = "chk_action_supported"
CHK_EVIDENCE_EXISTS = "chk_evidence_exists"
CHK_EVIDENCE_SCOPE = "chk_evidence_scope"
CHK_EVIDENCE_FRESHNESS = "chk_evidence_freshness"
CHK_EVIDENCE_INTEGRITY = "chk_evidence_integrity"
CHK_ACTION_ELIGIBILITY = "chk_action_eligibility"
CHK_TARGET_CONSISTENCY = "chk_target_consistency"
CHK_AMOUNT_SAFETY = "chk_amount_safety"
CHK_ACTION_PRECONDITIONS = "chk_action_preconditions"


@dataclass(frozen=True)
class VerifiedIntent:
    """An AgentIntent that has successfully passed deterministic verification.

    This container is the sole input accepted by the Policy Engine (ARCHITECTURE.md §10, §11).
    It guarantees:
    1. The intent passed all deterministic checks with VerificationStatus.VERIFIED.
    2. Any monetary amounts have been re-derived from deterministic source records.
    3. The intent cannot be executed without subsequent Policy authorization.
    """

    intent: AgentIntent
    verification_result: VerificationResult
    verified_failed_gmv: Optional[Money] = None
    verified_revenue_at_risk: Optional[Money] = None
    verified_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not isinstance(self.intent, AgentIntent):
            raise DomainValidationError("VerifiedIntent.intent must be an AgentIntent")
        if not isinstance(self.verification_result, VerificationResult):
            raise DomainValidationError(
                "VerifiedIntent.verification_result must be a VerificationResult"
            )
        if not self.verification_result.is_verified:
            raise DomainValidationError(
                f"Cannot create VerifiedIntent with non-verified status: {self.verification_result.status.value}"
            )
        if self.verification_result.subject_id != self.intent.intent_id:
            raise DomainValidationError(
                f"VerificationResult subject_id '{self.verification_result.subject_id}' "
                f"does not match intent_id '{self.intent.intent_id}'"
            )

        if self.verified_failed_gmv is not None and not isinstance(self.verified_failed_gmv, Money):
            raise DomainValidationError("verified_failed_gmv must be a Money instance")
        if self.verified_revenue_at_risk is not None and not isinstance(
            self.verified_revenue_at_risk, Money
        ):
            raise DomainValidationError("verified_revenue_at_risk must be a Money instance")

        when = self.verified_at or self.verification_result.verified_at
        object.__setattr__(self, "verified_at", require_utc(when, "VerifiedIntent.verified_at"))

    @property
    def intent_id(self) -> str:
        return self.intent.intent_id

    @property
    def incident_id(self) -> str:
        return self.intent.incident_id

    @property
    def content_hash(self) -> str:
        return self.intent.content_hash()
