"""Persistence layer for Merchant FinPilot.

PROJECT_RULES 4.2, 10.7, 10.8 / ARCHITECTURE.md §6.

Stores and retrieves domain objects. Nothing else. Repository functions take and
return ``domain`` contracts; callers never see a row, a cursor or a SQL string.

The database is the internal source of truth for facts we derived, and Razorpay
is authoritative for facts about external financial state (PROJECT_RULES §2).

Obligations
-----------
* **Money stays in integer minor units** in every column. No ``FLOAT``, no
  ``REAL``, no ``DOUBLE`` anywhere near an amount (PROJECT_RULES 4.2). Rates are
  stored as exact decimal strings or as their integer numerator/denominator, not
  as floats.
* **Timestamps are UTC and timezone-aware** on the way in and on the way out,
  matching ``domain.window.require_utc``.
* **Uniqueness constraints are the idempotency mechanism**, not application
  ``SELECT``-then-``INSERT`` checks, which race. Required on:
  ``FinancialIncident.incident_key``, audit sequence numbers, etc.
* **Recorded financial facts are append-only.**
* **No business logic and no arithmetic here.**
"""

from .database import Database

__all__ = [
    "Database",
]
