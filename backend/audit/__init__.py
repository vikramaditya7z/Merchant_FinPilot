"""Append-only audit log.  **[Day 3 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
Persists ``domain.audit.AuditEvent`` records and reads them back in sequence.
The audit trail is the accountability authority in the four-authority model: if
it isn't audited, it didn't happen (ARCHITECTURE.md §5, §16).

Writes only. There is no update and no delete — a correction is a new event
appended after the one it corrects (PROJECT_RULES 10.7).

What must be recorded
---------------------
Every fact ingested, every metric computed *with its inputs*, every tool call
and result, the agent's full reasoning trace with model and prompt version, the
intent, the verification result with every individual check, the policy decision
with every violation and rule version, the execution attempt and its raw
response, the outcome verification, and every escalation and human action
(ARCHITECTURE.md §16).

Both inputs and outputs of each deterministic computation are stored, so any
number in the system can be recomputed independently later. That is what makes
``COMPUTATION_VERSION`` in ``financial.engine`` meaningful.

Obligations
-----------
* **Never store a secret.** Keys, tokens and webhook signatures are redacted at
  write time, not at read time. A redaction that only happens on the read path
  has already leaked (PROJECT_RULES 10.9).
* **Monotonic sequence.** Ordering is a property of the record, not of the
  filesystem or of insertion timing.
* **Digest every payload** using ``domain.canonical`` so tampering is
  detectable and so the same payload hashes identically across runs. An unstable
  digest is not a digest.
* **Replayable.** A reviewer must be able to reconstruct exactly why a decision
  was made — including a decision that turned out to be wrong.

Dependencies: may import ``domain`` (and ``db`` for persistence). Must not
import ``agent``, ``policy``, ``verification`` or ``execution``: the audit log
records their behaviour and must not participate in it (PROJECT_RULES 10.8).
"""
