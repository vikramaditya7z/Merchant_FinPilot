"""AgentIntent — what the agent *wants* to do.

The most important contract in the system.

An intent is a **proposal**, not an action. This class holds no API client, no
credential, and no reference to the executor, and it has no method that causes
anything to happen. The agent's authority ends here; the Financial Verifier and
the Policy Engine decide what becomes of it (ARCHITECTURE.md 5.1, 8.1).

Design notes:

* ``claimed_amount`` is what the agent *says*. It is never used for execution.
  The Financial Verifier re-derives the true value from source records and
  compares; a mismatch is a BLOCK, not a correction (PROJECT_RULES 1.3).
* ``evidence_refs`` must resolve. An uncited claim is rejected rather than
  tidied up (PROJECT_RULES 3.8).
* Parameter values are restricted to an enumerated set of exact types. Floats
  are rejected outright, so no lossy value can enter an action payload.
* ``content_hash`` is canonical and stable, and is the basis of intent-level
  idempotency (ARCHITECTURE.md 15).
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Optional, Tuple, Union

from .canonical import digest
from .enums import IntentAction, TargetEntityType
from .errors import DomainValidationError, MoneyPrecisionError
from .money import Money
from .window import require_utc

# The exact types an action parameter may hold. Deliberately narrow: anything
# that cannot be canonically serialized cannot be idempotently executed.
ParameterValue = Union[str, int, bool, Money]

# Actions that operate on nothing in particular and so need no target.
_TARGETLESS_ACTIONS = frozenset({IntentAction.NO_ACTION, IntentAction.ESCALATE_TO_HUMAN})

# Actions that require no supporting evidence. Only the null action qualifies:
# proposing to do nothing needs no justification, everything else does.
_EVIDENCE_EXEMPT_ACTIONS = frozenset({IntentAction.NO_ACTION})

MIN_REASON_LENGTH = 20
MAX_REASON_LENGTH = 4000
MAX_PARAMETERS = 16


@dataclass(frozen=True)
class IntentTarget:
    """The entity an intent points at."""

    entity_type: TargetEntityType
    entity_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.entity_type, TargetEntityType):
            raise DomainValidationError(f"invalid entity_type: {self.entity_type!r}")
        if not isinstance(self.entity_id, str) or not self.entity_id.strip():
            raise DomainValidationError("IntentTarget.entity_id must be a non-empty string")

    def __str__(self) -> str:
        return f"{self.entity_type.value}:{self.entity_id}"


def _validate_parameters(parameters: Mapping[str, ParameterValue]) -> Mapping[str, ParameterValue]:
    if not isinstance(parameters, Mapping):
        raise DomainValidationError("AgentIntent.parameters must be a mapping")
    if len(parameters) > MAX_PARAMETERS:
        raise DomainValidationError(
            f"too many parameters ({len(parameters)} > {MAX_PARAMETERS})"
        )
    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise DomainValidationError(f"parameter name must be a non-empty string: {key!r}")
        if not key.replace("_", "").isalnum() or key != key.lower():
            raise DomainValidationError(
                f"parameter name must be lower snake_case: {key!r}"
            )
        if isinstance(value, bool):
            continue  # bool is checked before int on purpose
        if isinstance(value, float):
            raise MoneyPrecisionError(
                f"parameter {key!r} is a float; use int minor units or a Money value"
            )
        if isinstance(value, (str, int, Money)):
            continue
        raise DomainValidationError(
            f"parameter {key!r} has unsupported type {type(value).__name__}; "
            "allowed: str, int, bool, Money"
        )
    return MappingProxyType(dict(parameters))


@dataclass(frozen=True)
class AgentIntent:
    """A structured proposal produced by the reasoning layer.

    Attributes:
        intent_id: Our identifier for this proposal.
        incident_id: The incident this proposal belongs to. Policy checks that
            the target stays inside this incident's scope, so an intent cannot
            be used to reach unrelated data.
        action: What the agent proposes. Being on this enum does **not** make an
            action executable — the Policy Engine holds the allowlist.
        target: What it applies to. Required for all but targetless actions.
        parameters: Exact-typed action parameters.
        reason: The agent's justification, in prose. Long enough to be a real
            explanation.
        evidence_refs: Ids of ``FinancialEvidence`` supporting the proposal.
        claimed_amount: The agent's stated amount, if any. **Untrusted.**
        confidence: The agent's self-reported confidence in ``[0, 1]``.
            Self-reported, therefore only ever used to *reduce* authority — a
            low value can force ESCALATE, a high value can never grant ALLOW.
        proposed_at: When the proposal was made.
        model_id, prompt_version: Recorded so a past decision stays explicable.
    """

    intent_id: str
    incident_id: str
    action: IntentAction
    reason: str
    proposed_at: datetime
    model_id: str
    prompt_version: str
    target: Optional[IntentTarget] = None
    parameters: Mapping[str, ParameterValue] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = ()
    claimed_amount: Optional[Money] = None
    confidence: Optional[Decimal] = None

    def __post_init__(self) -> None:
        for name in ("intent_id", "incident_id", "model_id", "prompt_version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"AgentIntent.{name} must be a non-empty string")

        if not isinstance(self.action, IntentAction):
            raise DomainValidationError(f"invalid IntentAction: {self.action!r}")

        if not isinstance(self.reason, str) or len(self.reason.strip()) < MIN_REASON_LENGTH:
            raise DomainValidationError(
                f"AgentIntent.reason must be at least {MIN_REASON_LENGTH} characters "
                "of actual justification"
            )
        if len(self.reason) > MAX_REASON_LENGTH:
            raise DomainValidationError("AgentIntent.reason is too long")

        object.__setattr__(
            self, "proposed_at", require_utc(self.proposed_at, "AgentIntent.proposed_at")
        )

        if self.target is not None and not isinstance(self.target, IntentTarget):
            raise DomainValidationError("AgentIntent.target must be an IntentTarget")
        if self.action not in _TARGETLESS_ACTIONS and self.target is None:
            raise DomainValidationError(
                f"action {self.action.value} requires a target"
            )
        if self.action is IntentAction.NO_ACTION and self.target is not None:
            raise DomainValidationError("NO_ACTION must not carry a target")

        object.__setattr__(self, "parameters", _validate_parameters(self.parameters))

        if not isinstance(self.evidence_refs, tuple):
            raise DomainValidationError("AgentIntent.evidence_refs must be a tuple")
        for ref in self.evidence_refs:
            if not isinstance(ref, str) or not ref.strip():
                raise DomainValidationError("evidence_refs entries must be non-empty strings")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise DomainValidationError("evidence_refs contains duplicates")
        if self.action not in _EVIDENCE_EXEMPT_ACTIONS and not self.evidence_refs:
            raise DomainValidationError(
                f"action {self.action.value} requires at least one evidence reference"
            )

        if self.claimed_amount is not None:
            if not isinstance(self.claimed_amount, Money):
                raise DomainValidationError("claimed_amount must be a Money instance")
            if not self.claimed_amount.is_positive:
                raise DomainValidationError("claimed_amount must be positive when present")

        if self.confidence is not None:
            confidence = self.confidence
            if isinstance(confidence, bool) or isinstance(confidence, float):
                raise MoneyPrecisionError("confidence must be a Decimal, not a float")
            if isinstance(confidence, int):
                confidence = Decimal(confidence)
            if not isinstance(confidence, Decimal) or not confidence.is_finite():
                raise DomainValidationError("confidence must be a finite Decimal")
            if not (Decimal(0) <= confidence <= Decimal(1)):
                raise DomainValidationError(
                    f"confidence must be in [0, 1], got {confidence}"
                )
            object.__setattr__(self, "confidence", confidence)

    @property
    def is_consequential(self) -> bool:
        """Whether this proposal, if executed, would change external state.

        ``NO_ACTION`` and ``RECOMMEND_ONLY`` change nothing outside our own
        records; everything else must clear verification and policy.
        """
        return self.action not in (IntentAction.NO_ACTION, IntentAction.RECOMMEND_ONLY)

    def canonical_form(self) -> dict:
        """The semantic content of the proposal, for hashing.

        Excludes ``intent_id``, ``proposed_at``, ``reason`` and ``confidence``:
        two identical proposals differing only in wording, timing or the agent's
        mood are the *same* proposal, and must not execute twice.
        """
        return {
            "incident_id": self.incident_id,
            "action": self.action.value,
            "target": str(self.target) if self.target else None,
            "parameters": dict(self.parameters),
            "claimed_amount": self.claimed_amount,
        }

    def content_hash(self) -> str:
        """Stable digest of the canonical form. Basis of intent idempotency."""
        return digest(self.canonical_form())

    def __str__(self) -> str:
        target = f" -> {self.target}" if self.target else ""
        return f"AgentIntent({self.action.value}{target})"
