"""Persistence.  **[Day 3 — not implemented]**

This package contains no implementation. It exists so the contract below is
recorded before any code is written against it (PROJECT_RULES 10.11).

Contract
--------
Stores and retrieves domain objects. Nothing else. Repository functions take and
return ``domain`` contracts; callers never see a row, a cursor or a SQL string.

The database is the internal source of truth for facts we derived, and Razorpay
is authoritative for facts about external financial state (PROJECT_RULES §2).
When the two disagree about a payment's status, Razorpay wins and the
disagreement is itself worth recording.

Obligations
-----------
* **Money stays in integer minor units** in every column. No ``FLOAT``, no
  ``REAL``, no ``DOUBLE`` anywhere near an amount (PROJECT_RULES 4.2). Rates are
  stored as exact decimal strings or as their integer numerator/denominator, not
  as floats.
* **Timestamps are UTC and timezone-aware** on the way in and on the way out,
  matching ``domain.window.require_utc``.
* **Uniqueness constraints are the idempotency mechanism**, not application
  ``SELECT``-then-``INSERT`` checks, which race. Required at minimum on:
  Razorpay webhook event id, ``FinancialIncident.incident_key``, the intent
  content hash, and the executor's ``execution_key`` (ARCHITECTURE.md §15).
* **The execution key row is claimed before the outbound call**, so a crash
  mid-call cannot yield a second attempt.
* **Recorded financial facts are append-only.** A recomputation writes a new row
  carrying its ``COMPUTATION_VERSION``; it does not overwrite the old one
  (PROJECT_RULES 10.7).
* **No business logic and no arithmetic here.** A repository that computes a
  total is a second, untested financial engine.

Connection details, file paths and credentials come from the environment only
(PROJECT_RULES 10.9). Any new variable is added to ``.env.example`` with an
empty value.

Dependencies: may import ``domain``. Must not import ``financial``, ``agent``,
``policy``, ``verification``, ``execution`` or ``api`` (PROJECT_RULES 10.8).
"""
