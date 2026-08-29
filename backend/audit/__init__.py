"""Append-only audit log.

PROJECT_RULES 10.7, 10.9 / ARCHITECTURE.md §5, §16.

Persists ``domain.audit.AuditEvent`` records and reads them back in sequence.
The audit trail is the accountability authority in the four-authority model:
if it isn't audited, it didn't happen.

Writes only. There is no update and no delete — a correction is a new event
appended after the one it corrects (PROJECT_RULES 10.7).
"""

from .store import AuditLog

__all__ = [
    "AuditLog",
]
