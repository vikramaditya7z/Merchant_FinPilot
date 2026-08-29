"""Tests for the append-only audit log store.

Tests:
1. Append-only behavior and monotonic sequence numbering.
2. Secret screening enforcement (PROJECT_RULES 6.10, 10.9).
3. Canonical payload digestion and tampering verification.
4. Filtering and event retrieval.
5. Invariant enforcement (unique IDs, chronological sequence).
"""

import unittest
from datetime import datetime, timedelta

from ...audit.store import AuditLog
from ...domain.audit import AuditEvent
from ...domain.enums import AuditActor, AuditEventType
from ...domain.errors import DomainValidationError, NonCanonicalValueError, SecretLeakError
from ...domain.money import Money
from ...domain.window import UTC
from ..helpers import NOW


class AuditLogTests(unittest.TestCase):
    def setUp(self):
        self.log = AuditLog()

    def test_append_creates_monotonic_sequence(self):
        e1 = self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="Incident detected on UPI",
            incident_id="inc_1",
            occurred_at=NOW,
        )
        e2 = self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Investigation started",
            incident_id="inc_1",
            occurred_at=NOW + timedelta(seconds=1),
        )
        self.assertEqual(e1.sequence, 1)
        self.assertEqual(e2.sequence, 2)
        self.assertEqual(self.log.count(), 2)

    def test_secret_screening_rejects_credential_keys(self):
        with self.assertRaises(SecretLeakError):
            self.log.append(
                actor=AuditActor.AGENT,
                event_type=AuditEventType.TOOL_CALLED,
                summary="Calling tool with secret",
                payload={"api_key": "rzp_live_secret123"},
                occurred_at=NOW,
            )

    def test_float_in_payload_is_rejected(self):
        with self.assertRaises(NonCanonicalValueError):
            self.log.append(
                actor=AuditActor.SYSTEM,
                event_type=AuditEventType.METRICS_COMPUTED,
                summary="Computed failure rate",
                payload={"rate": 0.15},
                occurred_at=NOW,
            )

    def test_money_and_ints_in_payload_are_canonicalized(self):
        event = self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.METRICS_COMPUTED,
            summary="Computed exposure",
            payload={"failed_amount": Money(50000), "count": 10},
            occurred_at=NOW,
        )
        self.assertTrue(len(event.payload_digest) == 64)
        is_valid, errors = self.log.verify_integrity()
        self.assertTrue(is_valid)
        self.assertEqual(errors, ())

    def test_get_events_filters_by_incident_id(self):
        self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="Inc 1",
            incident_id="inc_1",
            occurred_at=NOW,
        )
        self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="Inc 2",
            incident_id="inc_2",
            occurred_at=NOW + timedelta(seconds=1),
        )
        inc1_events = self.log.get_events(incident_id="inc_1")
        self.assertEqual(len(inc1_events), 1)
        self.assertEqual(inc1_events[0].incident_id, "inc_1")

    def test_get_events_filters_by_event_type_and_actor(self):
        self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="System detect",
            occurred_at=NOW,
        )
        self.log.append(
            actor=AuditActor.AGENT,
            event_type=AuditEventType.AGENT_REASONING_RECORDED,
            summary="Agent reasoning",
            occurred_at=NOW + timedelta(seconds=1),
        )
        agent_events = self.log.get_events(actor=AuditActor.AGENT)
        self.assertEqual(len(agent_events), 1)
        self.assertEqual(agent_events[0].actor, AuditActor.AGENT)

    def test_append_event_enforces_monotonic_sequence(self):
        e1 = AuditEvent(
            event_id="aud_1",
            sequence=1,
            occurred_at=NOW,
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="First event",
        )
        e2_invalid = AuditEvent(
            event_id="aud_2",
            sequence=1,  # Duplicate/non-monotonic sequence
            occurred_at=NOW + timedelta(seconds=1),
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Invalid sequence event",
        )
        self.log.append_event(e1)
        with self.assertRaises(DomainValidationError):
            self.log.append_event(e2_invalid)

    def test_append_event_enforces_chronological_order(self):
        e1 = AuditEvent(
            event_id="aud_1",
            sequence=1,
            occurred_at=NOW,
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="First event",
        )
        e2_past = AuditEvent(
            event_id="aud_2",
            sequence=2,
            occurred_at=NOW - timedelta(seconds=10),  # Timestamp in the past
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Past event",
        )
        self.log.append_event(e1)
        with self.assertRaises(DomainValidationError):
            self.log.append_event(e2_past)

    def test_duplicate_event_id_rejected(self):
        e1 = AuditEvent(
            event_id="aud_same",
            sequence=1,
            occurred_at=NOW,
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="First event",
        )
        e2 = AuditEvent(
            event_id="aud_same",
            sequence=2,
            occurred_at=NOW + timedelta(seconds=1),
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Duplicate ID event",
        )
        self.log.append_event(e1)
        with self.assertRaises(DomainValidationError):
            self.log.append_event(e2)

    def test_verify_integrity_passes_on_valid_log(self):
        self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INCIDENT_DETECTED,
            summary="Detected",
            occurred_at=NOW,
        )
        self.log.append(
            actor=AuditActor.SYSTEM,
            event_type=AuditEventType.INVESTIGATION_STARTED,
            summary="Investigating",
            occurred_at=NOW + timedelta(seconds=1),
        )
        valid, errors = self.log.verify_integrity()
        self.assertTrue(valid)
        self.assertEqual(len(errors), 0)


if __name__ == "__main__":
    unittest.main()
