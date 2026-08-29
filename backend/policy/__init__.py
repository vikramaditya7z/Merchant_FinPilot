"""The Policy Engine — deterministic authorization.

PROJECT_RULES 1.4, 5.1, 5.2, 5.3, 5.5, 5.9 / ARCHITECTURE.md §11.

Contract
--------
Runs after FinancialVerifier on an already-verified intent.
Evaluates deterministic rules and produces an immutable PolicyDecision
in {ALLOW, BLOCK, ESCALATE}.
"""

from .config import PolicyConfig
from .engine import PolicyEngine
from .rules import (
    DEFAULT_RULES,
    RULE_ACTION_ALLOWLIST,
    RULE_AMOUNT_LIMIT,
    RULE_CONFIDENCE_FLOOR,
    RULE_HUMAN_ESCALATION,
    RULE_INSUFFICIENT_EVIDENCE,
    RULE_KILL_SWITCH,
    RULE_MODE_GUARD,
    RULE_RISK_BLOCKED_GUARD,
    RULE_VERIFICATION_REQUIRED,
    RULE_VERIFICATION_STALE,
)

__all__ = [
    "PolicyEngine",
    "PolicyConfig",
    "DEFAULT_RULES",
    "RULE_KILL_SWITCH",
    "RULE_MODE_GUARD",
    "RULE_ACTION_ALLOWLIST",
    "RULE_VERIFICATION_REQUIRED",
    "RULE_RISK_BLOCKED_GUARD",
    "RULE_AMOUNT_LIMIT",
    "RULE_CONFIDENCE_FLOOR",
    "RULE_HUMAN_ESCALATION",
    "RULE_INSUFFICIENT_EVIDENCE",
    "RULE_VERIFICATION_STALE",
]
