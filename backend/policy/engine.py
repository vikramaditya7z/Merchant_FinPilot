"""The Deterministic Policy Engine.

PROJECT_RULES 1.4, 5.1, 5.2, 5.3, 5.5, 5.9 / ARCHITECTURE.md §11.

Core Guarantees:
- "Policy decides." Sits strictly between verification and execution.
- Evaluates versioned, deterministic policy rules against a VerifiedIntent.
- Collects all violations without early exit.
- Pure function of (VerifiedIntent, injected_time) -> PolicyDecision.
- Zero client references, zero API calls, zero state mutation.
"""

from datetime import datetime
from typing import Callable, List, Optional, Sequence, Tuple

from ..audit.store import AuditLog
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType, IntentAction, PolicyVerdict
from ..domain.errors import DomainValidationError
from ..domain.policy import PolicyDecision, PolicyViolation
from ..domain.window import require_utc
from ..verification.contracts import VerifiedIntent
from .config import PolicyConfig
from .rules import DEFAULT_RULES


class PolicyEngine:
    """Deterministic policy authorization engine."""

    def __init__(
        self,
        config: Optional[PolicyConfig] = None,
        rules: Optional[
            Sequence[Callable[[VerifiedIntent, PolicyConfig, datetime], Optional[PolicyViolation]]]
        ] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self._config = config or PolicyConfig()
        self._rules = tuple(rules) if rules is not None else DEFAULT_RULES
        self._audit_log = audit_log

    @property
    def config(self) -> PolicyConfig:
        return self._config

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    def evaluate(
        self,
        verified_intent: VerifiedIntent,
        now: Optional[datetime] = None,
    ) -> PolicyDecision:
        """Evaluate deterministic policy rules on a VerifiedIntent.

        Args:
            verified_intent: An intent that has passed FinancialVerifier.
            now: Current timestamp injection (aware UTC).

        Returns:
            An immutable PolicyDecision authorizing, blocking, or escalating the action.
        """
        if not isinstance(verified_intent, VerifiedIntent):
            raise DomainValidationError("PolicyEngine requires a VerifiedIntent instance")

        when = require_utc(now) if now is not None else datetime.now().astimezone()

        violations: List[PolicyViolation] = []
        for rule in self._rules:
            v = rule(verified_intent, self._config, when)
            if v is not None:
                violations.append(v)

        blocking_violations = [v for v in violations if v.is_blocking]
        escalating_violations = [v for v in violations if not v.is_blocking]

        # Determine Verdict
        if blocking_violations:
            verdict = PolicyVerdict.BLOCK
            required_approvals: Tuple[str, ...] = ()
            rationale = (
                f"Policy BLOCKED with {len(blocking_violations)} blocking violation(s): "
                f"{[v.rule_id for v in blocking_violations]}"
            )
        elif escalating_violations or verified_intent.intent.action == IntentAction.ESCALATE_TO_HUMAN:
            verdict = PolicyVerdict.ESCALATE
            required_approvals = self._config.escalation_approver_roles
            rationale = (
                f"Policy ESCALATED for human review ({len(escalating_violations)} escalation triggers): "
                f"{[v.rule_id for v in escalating_violations] or ['HUMAN_ESCALATION_REQUESTED']}"
            )
        else:
            verdict = PolicyVerdict.ALLOW
            required_approvals = ()
            rationale = (
                f"Policy ALLOWED action '{verified_intent.intent.action.value}' "
                f"on target '{verified_intent.intent.target}'."
            )

        expires_at = PolicyDecision.default_expiry(when, self._config.decision_ttl_seconds)
        decision_id = f"dec_{short_digest({'intent_hash': verified_intent.content_hash, 'verdict': verdict.value, 'when': when.isoformat()})}"

        decision = PolicyDecision(
            decision_id=decision_id,
            intent_id=verified_intent.intent_id,
            intent_hash=verified_intent.content_hash,
            verdict=verdict,
            rationale=rationale,
            evaluated_at=when,
            expires_at=expires_at,
            rule_set_version=self._config.policy_version,
            violations=tuple(violations),
            required_approvals=required_approvals,
        )

        # Audit Event Recording
        if self._audit_log is not None:
            self._audit_log.append(
                actor=AuditActor.POLICY,
                event_type=AuditEventType.POLICY_DECIDED,
                summary=f"Policy decided {verdict.value.upper()} for intent {verified_intent.intent_id}",
                incident_id=verified_intent.incident_id,
                subject_id=verified_intent.intent_id,
                occurred_at=when,
                payload={
                    "decision_id": decision_id,
                    "intent_id": verified_intent.intent_id,
                    "intent_hash": verified_intent.content_hash,
                    "verdict": verdict.value,
                    "violations": [v.rule_id for v in violations],
                    "required_approvals": list(required_approvals),
                    "expires_at": expires_at.isoformat(),
                },
            )

        return decision
