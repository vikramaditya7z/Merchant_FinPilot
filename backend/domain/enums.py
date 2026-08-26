"""Closed vocabularies for the Merchant FinPilot domain.

Enums are closed on purpose: an out-of-vocabulary value from an LLM or an
external payload must fail loudly rather than flow through the system as a
string nobody validated.

Provenance is marked on every enum:

* ``RAZORPAY`` — mirrors values documented by Razorpay. Reasonably confident,
  to be re-confirmed against official documentation at integration time
  (ARCHITECTURE.md 12.1).
* ``INTERNAL`` — our own taxonomy. Razorpay does not define these.
"""

from enum import Enum


class Currency(str, Enum):
    """Supported currencies. The MVP is INR-only (ARCHITECTURE.md 4.1)."""

    INR = "INR"

    @property
    def minor_units_per_unit(self) -> int:
        """How many minor units make one major unit (100 paise = 1 rupee)."""
        return 100

    @property
    def symbol(self) -> str:
        return "₹"


class PaymentStatus(str, Enum):
    """Razorpay payment lifecycle status. Provenance: RAZORPAY."""

    CREATED = "created"
    AUTHORIZED = "authorized"
    CAPTURED = "captured"
    REFUNDED = "refunded"
    FAILED = "failed"


class PaymentMethod(str, Enum):
    """Razorpay payment instrument. Provenance: RAZORPAY.

    ``UNKNOWN`` is INTERNAL: it exists so an unrecognised method from a payload
    is representable and countable rather than silently dropped.
    """

    CARD = "card"
    NETBANKING = "netbanking"
    WALLET = "wallet"
    UPI = "upi"
    EMI = "emi"
    UNKNOWN = "unknown"


class OrderStatus(str, Enum):
    """Razorpay order status. Provenance: RAZORPAY."""

    CREATED = "created"
    ATTEMPTED = "attempted"
    PAID = "paid"


class PaymentOutcome(str, Enum):
    """Derived, rate-relevant outcome of a payment. Provenance: INTERNAL.

    The distinction that matters financially (ARCHITECTURE.md 7.2):

    * ``SUCCEEDED`` — reached money-in (authorized / captured / refunded).
      A refund is a separate downstream event; the payment itself succeeded.
    * ``FAILED`` — terminal failure.
    * ``UNDECIDED`` — still in flight (``created``). Excluded from every rate
      denominator, because counting in-flight payments understates failure
      rate and masks real incidents.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNDECIDED = "undecided"


class FailureCategory(str, Enum):
    """Normalised failure taxonomy. Provenance: INTERNAL.

    Razorpay's ``error_code`` / ``error_reason`` strings are stored verbatim on
    ``Payment``. This enum is our own grouping for investigation, applied by the
    enrichment layer. Do not present these as Razorpay values.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    AUTHENTICATION_FAILED = "authentication_failed"
    GATEWAY_ERROR = "gateway_error"
    TIMEOUT = "timeout"
    INVALID_INSTRUMENT = "invalid_instrument"
    CUSTOMER_DROPPED = "customer_dropped"
    ISSUER_UNAVAILABLE = "issuer_unavailable"
    RISK_BLOCKED = "risk_blocked"
    UNKNOWN = "unknown"


class Dimension(str, Enum):
    """Dimensions an investigation may slice along. Provenance: INTERNAL.

    ``REGION`` and ``PROVIDER`` are not available on the Razorpay payment
    entity as far as we have verified; they come from ``PaymentEnrichment``
    (ARCHITECTURE.md 12.2).
    """

    PAYMENT_METHOD = "payment_method"
    FAILURE_CATEGORY = "failure_category"
    FAILURE_CODE = "failure_code"
    REGION = "region"
    PROVIDER = "provider"
    HOUR_OF_DAY = "hour_of_day"


class SourceConfidence(str, Enum):
    """How a fact was obtained. Provenance: INTERNAL.

    Kept on evidence so a reader can tell observed data from inference. Policy
    must not gate solely on an ``ENRICHED`` fact without an explicit note.
    """

    OBSERVED = "observed"
    DERIVED = "derived"
    ENRICHED = "enriched"


class IncidentType(str, Enum):
    """Incident classes. The MVP implements exactly one."""

    PAYMENT_FAILURE_SPIKE = "payment_failure_spike"


class IncidentStatus(str, Enum):
    """Incident lifecycle."""

    DETECTED = "detected"
    INVESTIGATING = "investigating"
    DIAGNOSED = "diagnosed"
    AWAITING_AUTHORIZATION = "awaiting_authorization"
    ACTING = "acting"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    DISMISSED = "dismissed"


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class BaselineMethod(str, Enum):
    """Deterministic baseline estimators (ARCHITECTURE.md 7.3)."""

    POOLED = "pooled"
    MEDIAN_OF_WINDOWS = "median_of_windows"


class ComparableWindowMode(str, Enum):
    """How baseline windows are selected for comparison.

    ``SAME_HOUR_OF_DAY`` exists because comparing peak-hour traffic against a
    flat 24-hour average manufactures false positives.
    """

    ALL = "all"
    SAME_HOUR_OF_DAY = "same_hour_of_day"


class IntentAction(str, Enum):
    """Actions the agent may *propose*. Provenance: INTERNAL.

    Proposing is not executing. Which of these is executable is decided by the
    Policy Engine allowlist, and is constrained by what Razorpay actually
    supports in test mode — an open question (ARCHITECTURE.md 22, Q1/Q2).

    ``CREATE_PAYMENT_LINK`` is listed because a Payment Links API exists, but
    its request/response schema is REQUIRES OFFICIAL DOC VERIFICATION and no
    code may depend on it yet (PROJECT_RULES 6.3).
    """

    NO_ACTION = "no_action"
    NOTIFY_MERCHANT = "notify_merchant"
    RECOMMEND_ONLY = "recommend_only"
    CREATE_PAYMENT_LINK = "create_payment_link"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class TargetEntityType(str, Enum):
    """What an intent points at."""

    PAYMENT = "payment"
    ORDER = "order"
    MERCHANT = "merchant"
    INCIDENT = "incident"


class PolicyVerdict(str, Enum):
    """Authorization outcome. ``ESCALATE`` is a correct answer, not a failure."""

    ALLOW = "allow"
    BLOCK = "block"
    ESCALATE = "escalate"


class ViolationEffect(str, Enum):
    """What a policy violation does to the verdict."""

    BLOCKING = "blocking"
    ESCALATING = "escalating"


class ExecutionStatus(str, Enum):
    """Outcome of an execution attempt.

    ``UNKNOWN`` is load-bearing: a timeout is not a failure, because the action
    may have succeeded. It is never retried automatically
    (PROJECT_RULES 7.7).
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    SKIPPED_DUPLICATE = "skipped_duplicate"
    NOT_ATTEMPTED = "not_attempted"


class VerificationPhase(str, Enum):
    PRE_EXECUTION = "pre_execution"
    POST_EXECUTION = "post_execution"


class VerificationStatus(str, Enum):
    """Result of a deterministic verification pass.

    ``INCONCLUSIVE`` means we could not establish the truth. It is not success.
    """

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class AuditActor(str, Enum):
    SYSTEM = "system"
    AGENT = "agent"
    VERIFIER = "verifier"
    POLICY = "policy"
    EXECUTOR = "executor"
    HUMAN = "human"


class AuditEventType(str, Enum):
    """Auditable events. Extend deliberately; every consequential step needs one."""

    FACT_INGESTED = "fact_ingested"
    METRICS_COMPUTED = "metrics_computed"
    INCIDENT_DETECTED = "incident_detected"
    INCIDENT_DISMISSED = "incident_dismissed"
    TOOL_CALLED = "tool_called"
    AGENT_REASONING_RECORDED = "agent_reasoning_recorded"
    INTENT_PROPOSED = "intent_proposed"
    INTENT_VERIFIED = "intent_verified"
    INTENT_REJECTED = "intent_rejected"
    POLICY_DECIDED = "policy_decided"
    ACTION_ATTEMPTED = "action_attempted"
    ACTION_RESULT_RECORDED = "action_result_recorded"
    OUTCOME_VERIFIED = "outcome_verified"
    ESCALATED = "escalated"
    HUMAN_DECISION_RECORDED = "human_decision_recorded"


SUCCESS_STATUSES = frozenset(
    {PaymentStatus.AUTHORIZED, PaymentStatus.CAPTURED, PaymentStatus.REFUNDED}
)
FAILURE_STATUSES = frozenset({PaymentStatus.FAILED})
UNDECIDED_STATUSES = frozenset({PaymentStatus.CREATED})


def outcome_for_status(status: PaymentStatus) -> PaymentOutcome:
    """Map a Razorpay payment status onto its rate-relevant outcome.

    Single source of truth for this mapping. Changing it changes every rate in
    the system, so it lives in exactly one place (PROJECT_RULES 4.2).
    """
    if status in SUCCESS_STATUSES:
        return PaymentOutcome.SUCCEEDED
    if status in FAILURE_STATUSES:
        return PaymentOutcome.FAILED
    if status in UNDECIDED_STATUSES:
        return PaymentOutcome.UNDECIDED
    # Unreachable while the enum stays closed; explicit so adding a status
    # without updating this mapping fails loudly instead of silently.
    raise AssertionError(f"unmapped payment status: {status!r}")
