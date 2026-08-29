"""Financial Verifier and post-action outcome verification.

PROJECT_RULES 1.3, 1.4, 8.5, 8.6, 8.7 / ARCHITECTURE.md §10.

Contract
--------
Deterministic pre-execution verification of AgentIntent proposals against source records.
The Verifier rejects without repairing.
"""

from .contracts import (
    CHK_ACTION_PRECONDITIONS,
    CHK_ACTION_SUPPORTED,
    CHK_AMOUNT_SAFETY,
    CHK_EVIDENCE_EXISTS,
    CHK_EVIDENCE_FRESHNESS,
    CHK_EVIDENCE_INTEGRITY,
    CHK_EVIDENCE_SCOPE,
    CHK_INCIDENT_ACTIVE,
    CHK_INCIDENT_EXISTS,
    CHK_INTENT_SCHEMA,
    CHK_TARGET_CONSISTENCY,
    VerifiedIntent,
)
from .verifier import FinancialVerifier

__all__ = [
    "FinancialVerifier",
    "VerifiedIntent",
    "CHK_INTENT_SCHEMA",
    "CHK_INCIDENT_EXISTS",
    "CHK_INCIDENT_ACTIVE",
    "CHK_ACTION_SUPPORTED",
    "CHK_EVIDENCE_EXISTS",
    "CHK_EVIDENCE_SCOPE",
    "CHK_EVIDENCE_FRESHNESS",
    "CHK_EVIDENCE_INTEGRITY",
    "CHK_TARGET_CONSISTENCY",
    "CHK_AMOUNT_SAFETY",
    "CHK_ACTION_PRECONDITIONS",
]
