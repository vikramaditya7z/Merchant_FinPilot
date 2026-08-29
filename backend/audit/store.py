"""Append-only audit log store and integrity verifier.

PROJECT_RULES 10.7, 10.9 / ARCHITECTURE.md §16.

The audit log is the accountability authority in the four-authority model:
if it isn't audited, it didn't happen.

Writes only — there is no update and no delete. A correction is a new event
appended after the one it corrects. Every payload is canonicalized and digested
so tampering is detectable.
"""

from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ..domain.audit import AuditEvent
from ..domain.canonical import canonicalize, digest, short_digest
from ..domain.enums import AuditActor, AuditEventType
from ..domain.errors import DomainValidationError
from ..domain.window import require_utc


class AuditLog:
    """An append-only store for immutable ``AuditEvent`` records.

    Thread-safe within a single process. Sequences are strictly monotonic.
    """

    def __init__(self, initial_events: Optional[Sequence[AuditEvent]] = None) -> None:
        self._events: List[AuditEvent] = []
        self._next_sequence: int = 1

        if initial_events:
            for event in initial_events:
                self.append_event(event)

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    @property
    def events(self) -> Tuple[AuditEvent, ...]:
        """All recorded audit events."""
        return tuple(self._events)

    def append(
        self,
        actor: AuditActor,
        event_type: AuditEventType,
        summary: str,
        payload: Optional[Mapping[str, Any]] = None,
        incident_id: Optional[str] = None,
        subject_id: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        event_id: Optional[str] = None,
    ) -> AuditEvent:
        """Create and append an ``AuditEvent`` to the log.

        Args:
            actor: Who performed the action.
            event_type: Category of the action.
            summary: Human-readable explanation.
            payload: Structured canonicalizable details.
            incident_id: Optional incident scope binding.
            subject_id: Optional entity ID targeted.
            occurred_at: Event timestamp (aware UTC, defaults to now).
            event_id: Optional explicit event ID; auto-generated if None.

        Returns:
            The recorded, immutable ``AuditEvent``.
        """
        now = require_utc(occurred_at) if occurred_at is not None else datetime.now().astimezone()
        seq = self._next_sequence
        clean_payload = dict(payload) if payload is not None else {}

        auto_id = event_id or "aud_" + short_digest(
            {
                "sequence": seq,
                "actor": actor.value,
                "type": event_type.value,
                "occurred_at": now.isoformat(),
            }
        )

        event = AuditEvent(
            event_id=auto_id,
            sequence=seq,
            occurred_at=now,
            actor=actor,
            event_type=event_type,
            summary=summary,
            incident_id=incident_id,
            subject_id=subject_id,
            payload=clean_payload,
        )

        self._events.append(event)
        self._next_sequence += 1
        return event

    def append_event(self, event: AuditEvent) -> AuditEvent:
        """Append an already-constructed ``AuditEvent``, enforcing sequence and integrity."""
        if not isinstance(event, AuditEvent):
            raise DomainValidationError("AuditLog.append_event requires an AuditEvent")

        # Check sequence monotonicity
        if self._events:
            last = self._events[-1]
            if event.sequence <= last.sequence:
                raise DomainValidationError(
                    f"AuditEvent sequence #{event.sequence} is not strictly greater than "
                    f"last recorded sequence #{last.sequence}"
                )
            if event.occurred_at < last.occurred_at:
                raise DomainValidationError(
                    f"AuditEvent timestamp {event.occurred_at} precedes last event timestamp {last.occurred_at}"
                )
        else:
            if event.sequence < 1:
                raise DomainValidationError(
                    f"First AuditEvent sequence must be >= 1, got {event.sequence}"
                )

        # Check event_id uniqueness
        if any(e.event_id == event.event_id for e in self._events):
            raise DomainValidationError(
                f"duplicate AuditEvent event_id: {event.event_id}"
            )

        self._events.append(event)
        self._next_sequence = max(self._next_sequence, event.sequence + 1)
        return event

    def get_events(
        self,
        incident_id: Optional[str] = None,
        event_type: Optional[AuditEventType] = None,
        actor: Optional[AuditActor] = None,
    ) -> Tuple[AuditEvent, ...]:
        """Query recorded audit events with optional filters."""
        results = self._events
        if incident_id is not None:
            results = [e for e in results if e.incident_id == incident_id]
        if event_type is not None:
            results = [e for e in results if e.event_type == event_type]
        if actor is not None:
            results = [e for e in results if e.actor == actor]
        return tuple(results)

    def get_event(self, event_id: str) -> Optional[AuditEvent]:
        """Find an event by its ID."""
        for event in self._events:
            if event.event_id == event_id:
                return event
        return None

    def verify_integrity(self) -> Tuple[bool, Tuple[str, ...]]:
        """Verify the cryptographic and structural integrity of the entire log.

        Returns:
            A tuple of (is_valid, error_messages).
        """
        errors = []
        seen_ids = set()

        for idx, event in enumerate(self._events):
            # Check unique ID
            if event.event_id in seen_ids:
                errors.append(f"Duplicate event_id at index {idx}: {event.event_id}")
            seen_ids.add(event.event_id)

            # Check sequence monotonicity
            if idx > 0:
                prev = self._events[idx - 1]
                if event.sequence <= prev.sequence:
                    errors.append(
                        f"Non-monotonic sequence at index {idx}: {event.sequence} <= {prev.sequence}"
                    )

            # Check payload digest matches canonical payload
            computed_digest = digest(canonicalize(dict(event.payload)))
            if event.payload_digest != computed_digest:
                errors.append(
                    f"Digest mismatch on event {event.event_id} (seq {event.sequence}): "
                    f"stored={event.payload_digest}, computed={computed_digest}"
                )

        return len(errors) == 0, tuple(errors)

    def count(self) -> int:
        """Total number of recorded events."""
        return len(self._events)

    def latest(self) -> Optional[AuditEvent]:
        """The most recently appended event, or None if empty."""
        return self._events[-1] if self._events else None

    def clear(self) -> None:
        """Clear all events (used for test isolation)."""
        self._events.clear()
        self._next_sequence = 1
