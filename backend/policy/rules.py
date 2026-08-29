"""Pure deterministic policy rule functions.

PROJECT_RULES 1.4, 5.1, 5.2, 5.3, 5.5 / ARCHITECTURE.md §11.

Invariants:
- Every rule is an independent pure function: (VerifiedIntent, PolicyConfig, datetime) -> Optional[PolicyViolation].
- Rules never modify state, perform I/O, or make external API calls.
- Rule violations are collected comprehensively (no premature short-circuiting).
"""

from datetime import datetime
from decimal import Decimal
from typing import Callable, List, Optional, Sequence, Tuple

from ..domain.enums import Dimension, IntentAction, ViolationEffect
from ..domain.policy import PolicyViolation
from ..verification.contracts import VerifiedIntent
from .config import PolicyConfig

# Stable Rule Identifiers
RULE_KILL_SWITCH = "POL-001"
RULE_MODE_GUARD = "POL-002"
RULE_ACTION_ALLOWLIST = "POL-003"
RULE_VERIFICATION_REQUIRED = "POL-004"
RULE_RISK_BLOCKED_GUARD = "POL-005"
RULE_AMOUNT_LIMIT = "POL-006"
RULE_CONFIDENCE_FLOOR = "POL-007"
RULE_HUMAN_ESCALATION = "POL-008"
RULE_INSUFFICIENT_EVIDENCE = "POL-009"
RULE_VERIFICATION_STALE = "POL-010"


def check_kill_switch(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Block all consequential actions if the global kill switch is active."""
    if not config.execution_enabled and verified_intent.intent.is_consequential:
        return PolicyViolation(
            rule_id=RULE_KILL_SWITCH,
            rule_version=config.policy_version,
            effect=ViolationEffect.BLOCKING,
            message="Execution kill switch active (FINPILOT_EXECUTION_ENABLED=false); action blocked.",
        )
    return None


def check_mode_guard(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Block consequential actions if not in test mode (MVP safety constraint)."""
    if config.razorpay_mode.lower() != "test" and verified_intent.intent.is_consequential:
        return PolicyViolation(
            rule_id=RULE_MODE_GUARD,
            rule_version=config.policy_version,
            effect=ViolationEffect.BLOCKING,
            message=f"Non-test Razorpay mode '{config.razorpay_mode}' blocked in current deployment.",
            detail="MVP permits execution only when RAZORPAY_MODE == 'test'",
        )
    return None


def check_action_allowlist(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Block actions not in the explicitly permitted allowlist."""
    if verified_intent.intent.action not in config.allowed_actions:
        return PolicyViolation(
            rule_id=RULE_ACTION_ALLOWLIST,
            rule_version=config.policy_version,
            effect=ViolationEffect.BLOCKING,
            message=f"Action '{verified_intent.intent.action.value}' is not in the policy allowlist.",
            detail=f"Allowed actions: {[a.value for a in config.allowed_actions]}",
        )
    return None


def check_verification_required(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Ensure that the intent has successfully cleared deterministic verification."""
    if not verified_intent.verification_result.is_verified:
        return PolicyViolation(
            rule_id=RULE_VERIFICATION_REQUIRED,
            rule_version=config.policy_version,
            effect=ViolationEffect.BLOCKING,
            message="Intent has not passed deterministic pre-execution verification.",
            detail=f"Verification status: {verified_intent.verification_result.status.value}",
        )
    return None


def check_risk_blocked_guard(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Block automated remediation or retry for risk-blocked (fraud) incidents."""
    intent = verified_intent.intent
    if intent.action == IntentAction.CREATE_PAYMENT_LINK:
        # Check checks detail in verification result or parameters
        checks_text = " ".join(c.detail or "" for c in verified_intent.verification_result.checks)
        if "risk" in checks_text.lower() or "risk" in str(intent.parameters).lower():
            return PolicyViolation(
                rule_id=RULE_RISK_BLOCKED_GUARD,
                rule_version=config.policy_version,
                effect=ViolationEffect.BLOCKING,
                message="Action blocked: transactions under risk_blocked rules cannot be bypassed or automated.",
                detail="Risk and compliance blocks require manual compliance clearance.",
            )
    return None


def check_amount_limit(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Escalate if action amount exceeds per-action threshold."""
    intent = verified_intent.intent
    amount_to_check = None
    if intent.claimed_amount is not None:
        amount_to_check = intent.claimed_amount
    elif intent.action == IntentAction.CREATE_PAYMENT_LINK:
        amount_to_check = verified_intent.verified_failed_gmv

    if amount_to_check is not None and amount_to_check > config.max_amount_per_action:
        return PolicyViolation(
            rule_id=RULE_AMOUNT_LIMIT,
            rule_version=config.policy_version,
            effect=ViolationEffect.ESCALATING,
            message=(
                f"Action amount {amount_to_check} exceeds single action limit {config.max_amount_per_action}; "
                "human approval required."
            ),
            detail=f"Amount: {amount_to_check}, Cap: {config.max_amount_per_action}",
        )
    return None


def check_confidence_floor(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Escalate to human if agent reported confidence is below the minimum threshold."""
    intent = verified_intent.intent
    if intent.confidence is not None and intent.is_consequential:
        if intent.confidence < config.confidence_floor:
            return PolicyViolation(
                rule_id=RULE_CONFIDENCE_FLOOR,
                rule_version=config.policy_version,
                effect=ViolationEffect.ESCALATING,
                message=(
                    f"Agent confidence ({intent.confidence}) is below the required floor ({config.confidence_floor}); "
                    "human review required."
                ),
                detail=f"Confidence: {intent.confidence} < {config.confidence_floor}",
            )
    return None


def check_human_escalation_request(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Route explicit ESCALATE_TO_HUMAN proposals to human approval workflow."""
    if verified_intent.intent.action == IntentAction.ESCALATE_TO_HUMAN:
        return PolicyViolation(
            rule_id=RULE_HUMAN_ESCALATION,
            rule_version=config.policy_version,
            effect=ViolationEffect.ESCALATING,
            message="Agent proposed explicit escalation to human operations team.",
            detail="Human review workflow triggered by agent intent.",
        )
    return None


def check_insufficient_evidence(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Escalate if consequential action lacks sufficient evidence citations."""
    intent = verified_intent.intent
    if intent.is_consequential and not intent.evidence_refs:
        return PolicyViolation(
            rule_id=RULE_INSUFFICIENT_EVIDENCE,
            rule_version=config.policy_version,
            effect=ViolationEffect.ESCALATING,
            message="Consequential action has no supporting evidence references; escalating.",
        )
    return None


def check_verification_freshness(
    verified_intent: VerifiedIntent, config: PolicyConfig, now: datetime
) -> Optional[PolicyViolation]:
    """Block action if the verified intent is stale relative to decision time."""
    age_seconds = (now - verified_intent.verified_at).total_seconds()
    if age_seconds > config.decision_ttl_seconds:
        return PolicyViolation(
            rule_id=RULE_VERIFICATION_STALE,
            rule_version=config.policy_version,
            effect=ViolationEffect.BLOCKING,
            message=f"VerifiedIntent is stale ({int(age_seconds)}s > {config.decision_ttl_seconds}s TTL).",
            detail="Intent must be re-verified before policy evaluation.",
        )
    return None


DEFAULT_RULES: Tuple[Callable[[VerifiedIntent, PolicyConfig, datetime], Optional[PolicyViolation]], ...] = (
    check_kill_switch,
    check_mode_guard,
    check_action_allowlist,
    check_verification_required,
    check_risk_blocked_guard,
    check_amount_limit,
    check_confidence_floor,
    check_human_escalation_request,
    check_insufficient_evidence,
    check_verification_freshness,
)
