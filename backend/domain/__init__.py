"""Domain contracts for Merchant FinPilot.

Typed, immutable, self-validating, and dependency-free (ADR-001). Contracts
validate themselves in ``__post_init__``, so an invalid financial fact cannot
exist in memory — invalid input fails at construction rather than three layers
downstream.

This package imports **nothing** internal beyond itself and **nothing**
third-party. See PROJECT_RULES 4.8 and 10.8.
"""

from .audit import AuditEvent
from .canonical import canonical_json, digest, short_digest
from .enums import (
    AuditActor,
    AuditEventType,
    BaselineMethod,
    ComparableWindowMode,
    Currency,
    Dimension,
    ExecutionStatus,
    FailureCategory,
    IncidentStatus,
    IncidentType,
    IntentAction,
    OrderStatus,
    PaymentMethod,
    PaymentOutcome,
    PaymentStatus,
    PolicyVerdict,
    Severity,
    SourceConfidence,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
    ViolationEffect,
    outcome_for_status,
)
from .errors import (
    CurrencyMismatchError,
    DomainValidationError,
    FinPilotError,
    InsufficientDataError,
    MoneyPrecisionError,
    NonCanonicalValueError,
    SecretLeakError,
)
from .execution import ActionResult, build_execution_key
from .incident import FinancialEvidence, FinancialIncident
from .intent import AgentIntent, IntentTarget
from .metrics import (
    BaselineFailureRate,
    Deviation,
    DimensionBreakdown,
    DimensionSlice,
    FinancialMetrics,
    Rate,
    RecoverableRevenue,
    RecoveryAssumption,
    RevenueRisk,
    SignificanceResult,
    TransactionCounts,
    WindowCounts,
)
from .money import Money, sum_money
from .payment import EnrichedPayment, Order, Payment, PaymentEnrichment
from .policy import PolicyDecision, PolicyViolation
from .verification import VerificationCheck, VerificationResult
from .window import TimeWindow, from_unix_seconds, require_utc, to_unix_seconds

__all__ = [
    # money & time
    "Money",
    "sum_money",
    "TimeWindow",
    "require_utc",
    "from_unix_seconds",
    "to_unix_seconds",
    # canonical
    "canonical_json",
    "digest",
    "short_digest",
    # enums
    "AuditActor",
    "AuditEventType",
    "BaselineMethod",
    "ComparableWindowMode",
    "Currency",
    "Dimension",
    "ExecutionStatus",
    "FailureCategory",
    "IncidentStatus",
    "IncidentType",
    "IntentAction",
    "OrderStatus",
    "PaymentMethod",
    "PaymentOutcome",
    "PaymentStatus",
    "PolicyVerdict",
    "Severity",
    "SourceConfidence",
    "TargetEntityType",
    "VerificationPhase",
    "VerificationStatus",
    "ViolationEffect",
    "outcome_for_status",
    # entities
    "Payment",
    "PaymentEnrichment",
    "EnrichedPayment",
    "Order",
    # metrics
    "Rate",
    "TransactionCounts",
    "WindowCounts",
    "BaselineFailureRate",
    "Deviation",
    "SignificanceResult",
    "DimensionSlice",
    "DimensionBreakdown",
    "RecoveryAssumption",
    "RecoverableRevenue",
    "RevenueRisk",
    "FinancialMetrics",
    # incident & decision path
    "FinancialEvidence",
    "FinancialIncident",
    "AgentIntent",
    "IntentTarget",
    "PolicyDecision",
    "PolicyViolation",
    "ActionResult",
    "build_execution_key",
    "VerificationCheck",
    "VerificationResult",
    "AuditEvent",
    # errors
    "FinPilotError",
    "DomainValidationError",
    "CurrencyMismatchError",
    "MoneyPrecisionError",
    "InsufficientDataError",
    "NonCanonicalValueError",
    "SecretLeakError",
]
