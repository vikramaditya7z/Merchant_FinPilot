"""Policy decision contracts.

A ``PolicyDecision`` is the *only* thing that authorizes execution
(PROJECT_RULES 1.4). Its invariants are enforced here rather than trusted to the
engine, so a decision object that says ALLOW while carrying a blocking violation
cannot exist at all.

A decision is also not a bearer token valid forever: it has an explicit expiry,
and the executor re-checks it (ARCHITECTURE.md 13).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple

from .enums import PolicyVerdict, ViolationEffect
from .errors import DomainValidationError
from .window import require_utc

# A decision authorizes action for a short window only. State can change
# underneath us; a stale authorization is not an authorization.
DEFAULT_DECISION_TTL_SECONDS = 300


@dataclass(frozen=True)
class PolicyViolation:
    """One rule that was not satisfied.

    Attributes:
        rule_id: Stable identifier of the rule.
        rule_version: Recorded so a past decision stays explicable after the
            rules change (PROJECT_RULES 5.9).
        effect: Whether this violation blocks outright or forces escalation.
        message: Human-readable explanation, for the audit trail and the UI.
        detail: Optional machine-readable specifics, as text.
    """

    rule_id: str
    rule_version: str
    effect: ViolationEffect
    message: str
    detail: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("rule_id", "rule_version", "message"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"PolicyViolation.{name} must be non-empty")
        if not isinstance(self.effect, ViolationEffect):
            raise DomainValidationError(f"invalid ViolationEffect: {self.effect!r}")

    @property
    def is_blocking(self) -> bool:
        return self.effect is ViolationEffect.BLOCKING

    def __str__(self) -> str:
        return f"[{self.rule_id}] {self.message}"


@dataclass(frozen=True)
class PolicyDecision:
    """The authorization outcome for one verified intent.

    Attributes:
        intent_hash: Content hash of the intent this decision covers. The
            executor compares it, so a decision cannot be replayed against a
            different proposal.
        verdict: ALLOW / BLOCK / ESCALATE.
        violations: Every unsatisfied rule, not just the first
            (PROJECT_RULES 5.5).
        required_approvals: Approver roles needed when escalating.
        rule_set_version: Version of the whole policy set that produced this.
        expires_at: After this moment the decision authorizes nothing.
    """

    decision_id: str
    intent_id: str
    intent_hash: str
    verdict: PolicyVerdict
    rationale: str
    evaluated_at: datetime
    expires_at: datetime
    rule_set_version: str
    violations: Tuple[PolicyViolation, ...] = ()
    required_approvals: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("decision_id", "intent_id", "intent_hash", "rationale", "rule_set_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"PolicyDecision.{name} must be non-empty")
        if not isinstance(self.verdict, PolicyVerdict):
            raise DomainValidationError(f"invalid PolicyVerdict: {self.verdict!r}")
        object.__setattr__(
            self, "evaluated_at", require_utc(self.evaluated_at, "PolicyDecision.evaluated_at")
        )
        object.__setattr__(
            self, "expires_at", require_utc(self.expires_at, "PolicyDecision.expires_at")
        )
        if self.expires_at <= self.evaluated_at:
            raise DomainValidationError("PolicyDecision.expires_at must be after evaluated_at")

        if not isinstance(self.violations, tuple):
            raise DomainValidationError("PolicyDecision.violations must be a tuple")
        for violation in self.violations:
            if not isinstance(violation, PolicyViolation):
                raise DomainValidationError("violations must contain PolicyViolation")

        if not isinstance(self.required_approvals, tuple):
            raise DomainValidationError("required_approvals must be a tuple")

        # The core safety invariant: ALLOW cannot coexist with a blocking
        # violation. Enforced on the contract so no engine bug can produce a
        # self-contradictory authorization.
        if self.verdict is PolicyVerdict.ALLOW and self.blocking_violations:
            raise DomainValidationError(
                "PolicyDecision cannot be ALLOW while carrying blocking violations: "
                f"{[v.rule_id for v in self.blocking_violations]}"
            )
        if self.verdict is PolicyVerdict.BLOCK and not self.violations:
            # A BLOCK with no stated reason is unauditable.
            raise DomainValidationError("BLOCK must state at least one violation")
        if self.verdict is PolicyVerdict.ESCALATE and not self.required_approvals:
            raise DomainValidationError("ESCALATE must name at least one required approval")

    @property
    def blocking_violations(self) -> Tuple[PolicyViolation, ...]:
        return tuple(v for v in self.violations if v.is_blocking)

    @property
    def authorizes_execution(self) -> bool:
        """Only an explicit ALLOW authorizes anything. There is no default-allow."""
        return self.verdict is PolicyVerdict.ALLOW

    def is_valid_at(self, now: datetime) -> bool:
        """Whether the decision is still within its validity window."""
        now = require_utc(now)
        return self.evaluated_at <= now < self.expires_at

    def authorizes(self, intent_hash: str, now: datetime) -> bool:
        """Whether this decision authorizes exactly this intent, right now.

        Three conditions, all required: an ALLOW verdict, an unexpired decision,
        and a matching intent hash. The executor calls this and nothing else.
        """
        return (
            self.authorizes_execution
            and self.is_valid_at(now)
            and intent_hash == self.intent_hash
        )

    @staticmethod
    def default_expiry(evaluated_at: datetime, ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS) -> datetime:
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise DomainValidationError("ttl_seconds must be a positive int")
        return require_utc(evaluated_at) + timedelta(seconds=ttl_seconds)

    def __str__(self) -> str:
        return f"PolicyDecision({self.verdict.value}, {len(self.violations)} violations)"
