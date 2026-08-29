"""Policy engine configuration and limits.

PROJECT_RULES 1.4, 5.1, 5.2, 5.3 / ARCHITECTURE.md §11.

Defines:
- Bounded operational thresholds for authorization.
- Mode guards and execution kill-switch settings.
- Versioned rule set metadata.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import FrozenSet, Optional, Tuple

from ..domain.enums import Currency, IntentAction
from ..domain.errors import DomainValidationError
from ..domain.money import Money

DEFAULT_POLICY_VERSION = "finpilot-policy-v1"
DEFAULT_DECISION_TTL_SECONDS = 300  # 5 minutes
DEFAULT_CONFIDENCE_FLOOR = Decimal("0.70")
DEFAULT_MAX_ACTION_AMOUNT = Money(50000000, Currency.INR)  # ₹5,00,000 max single action


@dataclass(frozen=True)
class PolicyConfig:
    """Immutable policy configuration controlling authorization rules."""

    execution_enabled: bool = True
    razorpay_mode: str = "test"
    allowed_actions: Tuple[IntentAction, ...] = (
        IntentAction.NO_ACTION,
        IntentAction.NOTIFY_MERCHANT,
        IntentAction.RECOMMEND_ONLY,
        IntentAction.ESCALATE_TO_HUMAN,
        IntentAction.CREATE_PAYMENT_LINK,
    )
    max_amount_per_action: Money = DEFAULT_MAX_ACTION_AMOUNT
    confidence_floor: Decimal = DEFAULT_CONFIDENCE_FLOOR
    decision_ttl_seconds: int = DEFAULT_DECISION_TTL_SECONDS
    policy_version: str = DEFAULT_POLICY_VERSION
    escalation_approver_roles: Tuple[str, ...] = (
        "finance_lead",
        "merchant_operations",
    )

    def __post_init__(self) -> None:
        if not isinstance(self.execution_enabled, bool):
            raise DomainValidationError("execution_enabled must be a bool")
        if not isinstance(self.razorpay_mode, str) or not self.razorpay_mode.strip():
            raise DomainValidationError("razorpay_mode must be a non-empty string")
        if not isinstance(self.max_amount_per_action, Money) or not self.max_amount_per_action.is_positive:
            raise DomainValidationError("max_amount_per_action must be a positive Money instance")
        if not isinstance(self.confidence_floor, Decimal) or not (Decimal(0) <= self.confidence_floor <= Decimal(1)):
            raise DomainValidationError("confidence_floor must be a Decimal in [0, 1]")
        if isinstance(self.decision_ttl_seconds, bool) or not isinstance(self.decision_ttl_seconds, int) or self.decision_ttl_seconds <= 0:
            raise DomainValidationError("decision_ttl_seconds must be a positive integer")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise DomainValidationError("policy_version must be a non-empty string")
        if not isinstance(self.escalation_approver_roles, tuple) or not self.escalation_approver_roles:
            raise DomainValidationError("escalation_approver_roles must be a non-empty tuple of roles")
