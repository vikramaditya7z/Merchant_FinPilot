"""Audit trail contract.

Append-only, immutable, complete. If it isn't audited, it didn't happen
(ARCHITECTURE.md 16).

An ``AuditEvent`` is frozen and has no update path. A correction is a new event,
never an edit — an audit trail you can rewrite is not an audit trail.
"""

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional

from .canonical import assert_no_secrets, canonicalize, digest
from .enums import AuditActor, AuditEventType
from .errors import DomainValidationError
from .window import require_utc


@dataclass(frozen=True)
class AuditEvent:
    """One immutable record of something that happened.

    Attributes:
        sequence: Monotonic per-incident ordering. Wall-clock timestamps can
            collide or arrive out of order; a sequence cannot.
        actor: Who acted — SYSTEM, AGENT, VERIFIER, POLICY, EXECUTOR or HUMAN.
            Attribution is the point of the trail.
        payload: Canonicalizable detail. Screened for anything that looks like a
            credential at write time (PROJECT_RULES 6.10).
        payload_digest: Digest of the canonical payload, computed here rather
            than supplied, so it always matches what was stored.
    """

    event_id: str
    sequence: int
    occurred_at: datetime
    actor: AuditActor
    event_type: AuditEventType
    summary: str
    incident_id: Optional[str] = None
    subject_id: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    payload_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise DomainValidationError("AuditEvent.event_id must be non-empty")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise DomainValidationError("AuditEvent.sequence must be an int")
        if self.sequence < 0:
            raise DomainValidationError("AuditEvent.sequence must be non-negative")
        object.__setattr__(
            self, "occurred_at", require_utc(self.occurred_at, "AuditEvent.occurred_at")
        )
        if not isinstance(self.actor, AuditActor):
            raise DomainValidationError(f"invalid AuditActor: {self.actor!r}")
        if not isinstance(self.event_type, AuditEventType):
            raise DomainValidationError(f"invalid AuditEventType: {self.event_type!r}")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise DomainValidationError("AuditEvent.summary must be non-empty")
        if not isinstance(self.payload, Mapping):
            raise DomainValidationError("AuditEvent.payload must be a mapping")

        # Raises SecretLeakError if a key looks like a credential, and
        # NonCanonicalValueError if the payload cannot be represented exactly
        # (a float, for instance). Both are better found here than in a log.
        assert_no_secrets(self.payload)
        canonical_payload = canonicalize(dict(self.payload))

        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        computed = digest(canonical_payload)
        if self.payload_digest and self.payload_digest != computed:
            raise DomainValidationError(
                "supplied payload_digest does not match the payload"
            )
        object.__setattr__(self, "payload_digest", computed)

    def __str__(self) -> str:
        return f"#{self.sequence} {self.actor.value}/{self.event_type.value}: {self.summary}"
