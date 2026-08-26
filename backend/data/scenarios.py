"""Declarative specifications for the eleven Day-2 scenarios.

A scenario is a *description*, not data: traffic mix, base failure rates, and a
list of injected anomalies. ``generator.py`` turns a spec plus a seed into
payments. Keeping the two apart means a scenario can be read and argued about
without reading generator code, and the same spec always produces the same data.

Design notes:

* **Degradations are injected as predicates, not hardcoded rows.** An ``Anomaly``
  names the subset it affects (methods / regions / providers / hours) and the
  elevated failure rate within it. Everything outside stays at its base rate, so
  a dimensional breakdown genuinely localises the problem instead of finding it
  because we planted it in a specific slice.

* **Five of the eleven scenarios have no incident.** ``FALSE_ALARM``,
  ``SMALL_RANDOM_VARIATION``, ``INSUFFICIENT_DATA``, ``NORMAL``, and — in the
  action sense — ``RECOVERY_NOT_ELIGIBLE`` all punish over-eagerness. An agent
  that scores well only on the six real incidents is not usable
  (ARCHITECTURE.md 19.2).

* **Anomalies may be active in the baseline period too.** That single flag is
  what makes ``FALSE_ALARM`` work: a structurally worse evening is not an
  incident, and only a same-hour-of-day baseline sees that
  (``ComparableWindowMode.SAME_HOUR_OF_DAY``).
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Mapping, Tuple

from ..domain.enums import Dimension, FailureCategory, PaymentMethod
from ..domain.errors import DomainValidationError
from .ground_truth import GroundTruth, ScenarioId

# Region and provider codes used across the scenario set. Internal labels only —
# we have not verified that Razorpay exposes either dimension on the payment
# entity (ARCHITECTURE.md 12.1), so these are enrichment values, not API fields.
REGIONS: Tuple[Tuple[str, str], ...] = (
    ("IN-MH", "40"),
    ("IN-KA", "25"),
    ("IN-TN", "20"),
    ("IN-DL", "15"),
)
PROVIDERS: Tuple[Tuple[str, str], ...] = (
    ("acquirer_a", "70"),
    ("acquirer_b", "30"),
)

# Failure codes for ordinary background failures, with relative weights. Format
# mirrors the shape of Razorpay error codes we have seen, but the exact code
# vocabulary is REQUIRES OFFICIAL DOC VERIFICATION (PROJECT_RULES 6.2), so these
# are treated as opaque strings and nothing branches on their value.
BACKGROUND_FAILURES: Tuple[Tuple[str, FailureCategory, str], ...] = (
    ("BAD_REQUEST_ERROR:payment_failed", FailureCategory.CUSTOMER_DROPPED, "35"),
    ("BAD_REQUEST_ERROR:insufficient_funds", FailureCategory.INSUFFICIENT_FUNDS, "25"),
    ("BAD_REQUEST_ERROR:auth_failed", FailureCategory.AUTHENTICATION_FAILED, "20"),
    ("GATEWAY_ERROR:issuer_unavailable", FailureCategory.ISSUER_UNAVAILABLE, "12"),
    ("GATEWAY_ERROR:gateway_timeout", FailureCategory.TIMEOUT, "8"),
)


def _rate(value: str) -> Decimal:
    """Parse a rate from a string. Never from a float (PROJECT_RULES 4.3)."""
    parsed = Decimal(value)
    if not (Decimal(0) <= parsed <= Decimal(1)):
        raise DomainValidationError(f"rate must be in [0, 1], got {value}")
    return parsed


@dataclass(frozen=True)
class MethodProfile:
    """Traffic share and healthy failure rate for one payment method.

    ``traffic_share`` values across a scenario are relative weights, normalised
    by the generator, so a spec can be edited without rebalancing the rest.
    """

    method: PaymentMethod
    traffic_weight: Decimal
    base_failure_rate: Decimal
    mean_ticket_paise: int
    ticket_spread_paise: int

    def __post_init__(self) -> None:
        if not isinstance(self.method, PaymentMethod):
            raise DomainValidationError(f"invalid PaymentMethod: {self.method!r}")
        for name in ("traffic_weight", "base_failure_rate"):
            value = getattr(self, name)
            if isinstance(value, float) or not isinstance(value, Decimal):
                raise DomainValidationError(f"MethodProfile.{name} must be a Decimal")
        if self.traffic_weight <= 0:
            raise DomainValidationError("traffic_weight must be positive")
        if not (Decimal(0) <= self.base_failure_rate <= Decimal(1)):
            raise DomainValidationError("base_failure_rate must be in [0, 1]")
        if self.mean_ticket_paise <= 0:
            raise DomainValidationError("mean_ticket_paise must be positive")
        if not (0 <= self.ticket_spread_paise < self.mean_ticket_paise):
            raise DomainValidationError(
                "ticket_spread_paise must be non-negative and smaller than the mean"
            )


@dataclass(frozen=True)
class Anomaly:
    """An injected degradation, expressed as a predicate over payments.

    A payment is affected when *every* non-empty constraint matches. An empty
    constraint tuple means "any value". Affected payments fail at
    ``failure_rate`` instead of their method's base rate.

    Attributes:
        active_in_baseline: When ``True`` the degradation is present in the
            historical period as well, making it a normal characteristic of this
            merchant's traffic rather than an incident. This is what
            ``FALSE_ALARM`` uses.
    """

    label: str
    failure_rate: Decimal
    failure_code: str
    failure_category: FailureCategory
    methods: Tuple[PaymentMethod, ...] = ()
    regions: Tuple[str, ...] = ()
    providers: Tuple[str, ...] = ()
    hours_utc: Tuple[int, ...] = ()
    active_in_baseline: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise DomainValidationError("Anomaly.label must be non-empty")
        if isinstance(self.failure_rate, float) or not isinstance(self.failure_rate, Decimal):
            raise DomainValidationError("Anomaly.failure_rate must be a Decimal")
        if not (Decimal(0) <= self.failure_rate <= Decimal(1)):
            raise DomainValidationError("Anomaly.failure_rate must be in [0, 1]")
        if not isinstance(self.failure_code, str) or not self.failure_code.strip():
            raise DomainValidationError("Anomaly.failure_code must be non-empty")
        if not isinstance(self.failure_category, FailureCategory):
            raise DomainValidationError("Anomaly.failure_category must be a FailureCategory")
        if not isinstance(self.active_in_baseline, bool):
            raise DomainValidationError("Anomaly.active_in_baseline must be a bool")
        for hour in self.hours_utc:
            if isinstance(hour, bool) or not isinstance(hour, int) or not (0 <= hour <= 23):
                raise DomainValidationError(f"invalid hour_utc: {hour!r}")

    def matches(
        self,
        method: PaymentMethod,
        region: str,
        provider: str,
        hour_utc: int,
        in_baseline: bool,
    ) -> bool:
        """Whether this anomaly applies to a payment with these attributes."""
        if in_baseline and not self.active_in_baseline:
            return False
        if self.methods and method not in self.methods:
            return False
        if self.regions and region not in self.regions:
            return False
        if self.providers and provider not in self.providers:
            return False
        if self.hours_utc and hour_utc not in self.hours_utc:
            return False
        return True


@dataclass(frozen=True)
class ScenarioSpec:
    """Everything needed to generate one scenario deterministically.

    Attributes:
        baseline_days: Days of history generated before the incident window.
            Three days at the default volume gives ~900 decided transactions in
            the same-hour-of-day baseline, well above the engine's minimum, so
            the restraint scenarios fail for statistical reasons rather than for
            want of data. Note this leaves only three candidate windows for
            ``BaselineMethod.MEDIAN_OF_WINDOWS``; pooled is the default for that
            reason.
        incident_hours: Length of the window under investigation. Kept at one
            hour throughout: the engine's same-hour-of-day comparison matches
            hourly buckets, so a multi-hour incident window would be compared
            against buckets of a different duration. Multi-hour windows need
            matched-granularity bucketing, which belongs to the deferred
            detection layer.
        payments_per_hour: Nominal hourly volume. Sized by the *smallest affected
            subset* in the scenario set, not by the total: cards are 25% of
            traffic, so a one-hour window at 150/hour left only ~37 card
            payments, and the standard deviation on the card failure rate was
            ~7.8pp. Whether the scenario reproduced its own label then depended
            on the seed, which makes for a fragile suite and an unreliable demo.
            At 300/hour the affected subset is ~74 transactions and the signal is
            stable. Deliberately low in ``SMALL_RANDOM_VARIATION`` and
            ``INSUFFICIENT_DATA``, where sparse data is the point.
        undecided_share: Fraction of payments left in ``created`` state. Present
            in every scenario so the decided/undecided split is exercised by real
            data rather than only by unit tests (ARCHITECTURE.md 7.2).
        incident_window_start_hour: UTC hour the incident window begins, so
            evening scenarios can be placed deliberately.
    """

    scenario_id: ScenarioId
    description: str
    ground_truth: GroundTruth
    method_profiles: Tuple[MethodProfile, ...]
    baseline_days: int = 3
    incident_hours: int = 1
    payments_per_hour: int = 300
    incident_window_start_hour: int = 12
    undecided_share: Decimal = field(default_factory=lambda: Decimal("0.02"))
    anomalies: Tuple[Anomaly, ...] = ()
    regions: Tuple[Tuple[str, str], ...] = REGIONS
    providers: Tuple[Tuple[str, str], ...] = PROVIDERS

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, ScenarioId):
            raise DomainValidationError(f"invalid ScenarioId: {self.scenario_id!r}")
        if self.ground_truth.scenario_id is not self.scenario_id:
            raise DomainValidationError(
                f"ground truth for {self.ground_truth.scenario_id.value} attached to "
                f"spec {self.scenario_id.value}"
            )
        if not self.method_profiles:
            raise DomainValidationError("a scenario needs at least one MethodProfile")
        seen = set()
        for profile in self.method_profiles:
            if profile.method in seen:
                raise DomainValidationError(f"duplicate method profile: {profile.method}")
            seen.add(profile.method)
        for name in ("baseline_days", "incident_hours", "payments_per_hour"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise DomainValidationError(f"ScenarioSpec.{name} must be a positive int")
        if not (0 <= self.incident_window_start_hour <= 23):
            raise DomainValidationError("incident_window_start_hour must be in [0, 23]")
        if self.incident_hours > 24:
            raise DomainValidationError("incident_hours must not exceed 24")
        if isinstance(self.undecided_share, float) or not isinstance(
            self.undecided_share, Decimal
        ):
            raise DomainValidationError("undecided_share must be a Decimal")
        if not (Decimal(0) <= self.undecided_share < Decimal("0.5")):
            raise DomainValidationError("undecided_share must be in [0, 0.5)")
        if not self.regions or not self.providers:
            raise DomainValidationError("a scenario needs at least one region and provider")

        # An anomaly restricted to hours the incident window never covers would
        # silently produce a healthy incident window, and the scenario would
        # "pass" while testing nothing.
        incident_hours_covered = {
            (self.incident_window_start_hour + offset) % 24
            for offset in range(self.incident_hours)
        }
        for anomaly in self.anomalies:
            if anomaly.hours_utc and not (set(anomaly.hours_utc) & incident_hours_covered):
                raise DomainValidationError(
                    f"{self.scenario_id.value}: anomaly {anomaly.label!r} is restricted to "
                    f"hours {anomaly.hours_utc} which the incident window never covers"
                )

    @property
    def total_hours(self) -> int:
        return self.baseline_days * 24 + self.incident_hours


# --------------------------------------------------------------------------
# Traffic mixes
# --------------------------------------------------------------------------

# A realistic Indian merchant mix: UPI-dominant, cards second. Healthy failure
# rates differ by instrument, which is why a single global threshold is a poor
# detector and per-method breakdown matters.
STANDARD_MIX: Tuple[MethodProfile, ...] = (
    MethodProfile(PaymentMethod.UPI, Decimal("55"), _rate("0.040"), 120_000, 90_000),
    MethodProfile(PaymentMethod.CARD, Decimal("25"), _rate("0.070"), 280_000, 200_000),
    MethodProfile(PaymentMethod.NETBANKING, Decimal("12"), _rate("0.055"), 450_000, 300_000),
    MethodProfile(PaymentMethod.WALLET, Decimal("8"), _rate("0.030"), 65_000, 40_000),
)

# Single-method mix for the low-volume scenarios: with 2 payments an hour, a
# four-way split would leave most slices empty and make the scenario about
# sparsity of slices rather than sparsity of data.
SPARSE_MIX: Tuple[MethodProfile, ...] = (
    MethodProfile(PaymentMethod.UPI, Decimal("100"), _rate("0.050"), 100_000, 60_000),
)


# --------------------------------------------------------------------------
# The eleven scenarios
# --------------------------------------------------------------------------

_NORMAL = ScenarioSpec(
    scenario_id=ScenarioId.NORMAL,
    description="Healthy traffic. Failure rates sit at their per-method baselines.",
    method_profiles=STANDARD_MIX,
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.NORMAL,
        is_incident=False,
        has_sufficient_data=True,
        expected_root_cause="No degradation. Failure rates are at baseline.",
        expected_action_eligible=False,
        notes=(
            "The control case. Any detection that fires here has a false-positive "
            "problem, and any agent that proposes an action here fails on restraint."
        ),
    ),
)

_UPI_FAILURE_SPIKE = ScenarioSpec(
    scenario_id=ScenarioId.UPI_FAILURE_SPIKE,
    description="UPI failure rate jumps from ~4% to ~30% for one hour. Other methods normal.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="upi_degradation",
            failure_rate=_rate("0.30"),
            failure_code="BAD_REQUEST_ERROR:payment_failed",
            failure_category=FailureCategory.ISSUER_UNAVAILABLE,
            methods=(PaymentMethod.UPI,),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.UPI_FAILURE_SPIKE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.PAYMENT_METHOD,
        expected_contributing_values=("upi",),
        expected_root_cause="UPI rail degradation; issuer unavailability on UPI only.",
        expected_action_eligible=True,
        notes=(
            "The canonical incident. UPI carries the majority of traffic, so the "
            "blended failure rate moves too — the agent must still localise to UPI "
            "rather than reporting a general outage."
        ),
    ),
)

_CARD_FAILURE_SPIKE = ScenarioSpec(
    scenario_id=ScenarioId.CARD_FAILURE_SPIKE,
    description="Card failure rate jumps from ~7% to ~35%. Cards are a minority of traffic.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="card_auth_degradation",
            failure_rate=_rate("0.35"),
            failure_code="BAD_REQUEST_ERROR:auth_failed",
            failure_category=FailureCategory.AUTHENTICATION_FAILED,
            methods=(PaymentMethod.CARD,),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.CARD_FAILURE_SPIKE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.PAYMENT_METHOD,
        expected_contributing_values=("card",),
        expected_root_cause="Card authentication failures, likely 3DS/issuer authentication.",
        expected_action_eligible=True,
        notes=(
            "The mirror of the UPI case, and harder: cards are only ~25% of volume, "
            "so the blended rate barely moves. Detection on the blended rate alone "
            "will miss this. Card tickets are larger, so revenue at risk is high "
            "relative to the transaction count — a test that exposure is computed "
            "from amounts and not from counts."
        ),
    ),
)

_EVENING_FAILURE_SPIKE = ScenarioSpec(
    scenario_id=ScenarioId.EVENING_FAILURE_SPIKE,
    description="Genuine degradation confined to 18:00-20:59 UTC, absent from history.",
    method_profiles=STANDARD_MIX,
    incident_window_start_hour=19,
    anomalies=(
        Anomaly(
            label="evening_capacity_degradation",
            failure_rate=_rate("0.24"),
            failure_code="GATEWAY_ERROR:gateway_timeout",
            failure_category=FailureCategory.TIMEOUT,
            hours_utc=(18, 19, 20),
            active_in_baseline=False,
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.EVENING_FAILURE_SPIKE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.HOUR_OF_DAY,
        expected_contributing_values=("19",),
        expected_root_cause="Time-bound degradation during evening peak; gateway timeouts.",
        expected_action_eligible=True,
        requires_same_hour_baseline=True,
        notes=(
            "A real evening incident, and the twin of FALSE_ALARM. Both look "
            "identical against a 24-hour pooled baseline. Only a same-hour-of-day "
            "baseline separates them: here the same hour on previous days was "
            "healthy, there it was not. A system that gets this right and "
            "FALSE_ALARM wrong (or vice versa) is guessing."
        ),
    ),
)

_REGIONAL_FAILURE = ScenarioSpec(
    scenario_id=ScenarioId.REGIONAL_FAILURE,
    description="Failures concentrated in one region (IN-KA) across all methods.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="karnataka_degradation",
            failure_rate=_rate("0.32"),
            failure_code="GATEWAY_ERROR:issuer_unavailable",
            failure_category=FailureCategory.ISSUER_UNAVAILABLE,
            regions=("IN-KA",),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.REGIONAL_FAILURE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.REGION,
        expected_contributing_values=("IN-KA",),
        expected_root_cause="Region-localised failures in IN-KA across all payment methods.",
        expected_action_eligible=True,
        notes=(
            "Region is an ENRICHED dimension — we have not verified that Razorpay "
            "exposes geography on the payment entity (ARCHITECTURE.md 12.1). The "
            "correct behaviour is to report the concentration while stating that it "
            "rests on derived data, not to present it as observed fact."
        ),
    ),
)

_PROVIDER_FAILURE = ScenarioSpec(
    scenario_id=ScenarioId.PROVIDER_FAILURE,
    description="One acquirer/route degrades; the other is healthy.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="acquirer_b_degradation",
            failure_rate=_rate("0.38"),
            failure_code="GATEWAY_ERROR:gateway_error",
            failure_category=FailureCategory.GATEWAY_ERROR,
            providers=("acquirer_b",),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.PROVIDER_FAILURE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.PROVIDER,
        expected_contributing_values=("acquirer_b",),
        expected_root_cause="Single acquirer/route failing; failures span methods and regions.",
        expected_action_eligible=True,
        notes=(
            "The scenario most likely to be mis-attributed: acquirer_b carries a "
            "slice of every method and every region, so a method-only investigation "
            "sees a mild rise everywhere and concludes 'general degradation'. "
            "Provider is ENRICHED and its real source is TBD "
            "(acquirer_data contents vary by method)."
        ),
    ),
)

_MULTIPLE_FAILURES = ScenarioSpec(
    scenario_id=ScenarioId.MULTIPLE_FAILURES,
    description="Two independent degradations at once: UPI method and IN-TN region.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="upi_degradation",
            failure_rate=_rate("0.26"),
            failure_code="BAD_REQUEST_ERROR:payment_failed",
            failure_category=FailureCategory.ISSUER_UNAVAILABLE,
            methods=(PaymentMethod.UPI,),
        ),
        Anomaly(
            label="tamil_nadu_degradation",
            failure_rate=_rate("0.30"),
            failure_code="GATEWAY_ERROR:gateway_error",
            failure_category=FailureCategory.GATEWAY_ERROR,
            regions=("IN-TN",),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.MULTIPLE_FAILURES,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=None,
        expected_contributing_values=("upi", "IN-TN"),
        expected_root_cause=(
            "Two concurrent, independent degradations: UPI rail failures and "
            "region-localised failures in IN-TN."
        ),
        expected_action_eligible=True,
        notes=(
            "expected_primary_dimension is deliberately None. A confident "
            "single-cause diagnosis is WRONG here even though it will look "
            "well-evidenced, because either anomaly alone explains a large share "
            "of failures. This scenario tests whether the agent stops "
            "investigating once it finds its first plausible cause. Note the "
            "anomalies overlap: UPI payments in IN-TN match both, and the "
            "generator resolves that by last-match-wins, so failures are not "
            "cleanly partitionable between the two causes."
        ),
    ),
)

_FALSE_ALARM = ScenarioSpec(
    scenario_id=ScenarioId.FALSE_ALARM,
    description=(
        "Evening failure rates are structurally higher every day, including "
        "throughout the baseline period. Nothing has changed."
    ),
    method_profiles=STANDARD_MIX,
    incident_window_start_hour=19,
    anomalies=(
        Anomaly(
            label="recurring_evening_pattern",
            failure_rate=_rate("0.16"),
            failure_code="BAD_REQUEST_ERROR:payment_failed",
            failure_category=FailureCategory.CUSTOMER_DROPPED,
            hours_utc=(18, 19, 20, 21),
            active_in_baseline=True,
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.FALSE_ALARM,
        is_incident=False,
        has_sufficient_data=True,
        expected_root_cause=(
            "No incident. Elevated evening failure rates are this merchant's normal "
            "diurnal pattern and are present throughout the baseline period."
        ),
        expected_action_eligible=False,
        requires_same_hour_baseline=True,
        notes=(
            "The most important negative case. A 24-hour pooled baseline computes "
            "roughly 6% and sees the evening's ~16% as a large, statistically "
            "significant spike — a confident, well-evidenced, wrong alert. The "
            "same-hour-of-day baseline computes ~16% and correctly sees nothing. "
            "Compare with EVENING_FAILURE_SPIKE, which is byte-for-byte similar in "
            "the incident window and is a genuine incident."
        ),
    ),
)

_SMALL_RANDOM_VARIATION = ScenarioSpec(
    scenario_id=ScenarioId.SMALL_RANDOM_VARIATION,
    description=(
        "Modest volume, no injected degradation. Whatever gap appears between "
        "the window and its baseline is pure sampling noise."
    ),
    method_profiles=SPARSE_MIX,
    baseline_days=5,
    payments_per_hour=45,
    anomalies=(),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.SMALL_RANDOM_VARIATION,
        is_incident=False,
        has_sufficient_data=True,
        expected_root_cause=(
            "No incident. The window and the baseline are drawn from the same "
            "failure rate; the observed difference is sampling variation at this "
            "transaction volume."
        ),
        expected_action_eligible=False,
        notes=(
            "No anomaly is injected at all, so the null hypothesis is literally "
            "true and the label cannot be argued with. A baseline IS computable — "
            "~225 decided transactions in the same-hour history — which is what "
            "separates this from INSUFFICIENT_DATA. But the window holds only ~44 "
            "decided transactions, so one or two extra failures move the rate by "
            "several percentage points and can look like a large relative lift. "
            "The two-proportion test must decline to reject (ADR-006). "
            "Any detector keyed on deviation magnitude, or on a 'failure rate "
            "doubled' rule, fires here and is wrong.\n\n"
            "Design history worth knowing: earlier versions injected a real 2x-3x "
            "lift and labelled it 'not an incident'. That made the label a "
            "judgement about materiality rather than a fact, and at ~28 "
            "transactions a 3x lift genuinely IS significant — the scenario was "
            "mislabelled, not the engine wrong. Removing the anomaly makes the "
            "claim structural.\n\n"
            "This scenario is inherently seed-sensitive: with a true null there is "
            "still a ~5% chance any given seed produces a real type-I error. The "
            "committed DEFAULT_SEED is checked to land non-significant. A test "
            "asserting non-significance here is asserting a property of this "
            "seed's data, not a universal law."
        ),
    ),
)

_INSUFFICIENT_DATA = ScenarioSpec(
    scenario_id=ScenarioId.INSUFFICIENT_DATA,
    description="Barely any traffic. No baseline can be established.",
    method_profiles=SPARSE_MIX,
    baseline_days=1,
    payments_per_hour=2,
    anomalies=(
        Anomaly(
            label="apparent_spike",
            failure_rate=_rate("0.50"),
            failure_code="BAD_REQUEST_ERROR:payment_failed",
            failure_category=FailureCategory.UNKNOWN,
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.INSUFFICIENT_DATA,
        is_incident=False,
        has_sufficient_data=False,
        expected_root_cause=(
            "Undeterminable. Fewer than the minimum decided transactions required "
            "for a baseline."
        ),
        expected_action_eligible=False,
        notes=(
            "The correct output is an explicit 'I cannot tell', not 'all healthy' "
            "and not an incident. The engine returns baseline.rate=None here, and "
            "deviation, significance and revenue_risk are all absent rather than "
            "zero-filled (ADR-004). This scenario exists to prove the difference "
            "between undefined and zero survives the whole pipeline, and to catch "
            "any agent that treats a missing number as a benign one."
        ),
    ),
)

_RECOVERY_NOT_ELIGIBLE = ScenarioSpec(
    scenario_id=ScenarioId.RECOVERY_NOT_ELIGIBLE,
    description="A real, large failure spike — but the failures are risk-blocked payments.",
    method_profiles=STANDARD_MIX,
    anomalies=(
        Anomaly(
            label="risk_engine_blocking",
            failure_rate=_rate("0.34"),
            failure_code="BAD_REQUEST_ERROR:payment_blocked_risk",
            failure_category=FailureCategory.RISK_BLOCKED,
            methods=(PaymentMethod.CARD,),
        ),
    ),
    ground_truth=GroundTruth(
        scenario_id=ScenarioId.RECOVERY_NOT_ELIGIBLE,
        is_incident=True,
        has_sufficient_data=True,
        expected_primary_dimension=Dimension.FAILURE_CATEGORY,
        expected_contributing_values=("risk_blocked",),
        expected_root_cause=(
            "Risk engine is blocking card payments. A genuine change in payment "
            "outcomes, but the failures are intentional refusals."
        ),
        expected_action_eligible=False,
        notes=(
            "Detection is right, diagnosis is right, and action is still wrong. "
            "Every deterministic signal points at a recoverable spike — high "
            "failure rate, large revenue at risk, clean single-dimension "
            "concentration — but re-offering payment on risk-blocked "
            "transactions attempts to complete payments the risk system "
            "deliberately refused. Recovery eligibility is a policy question and "
            "must be decided by the Policy Engine on failure category, never "
            "inferred by the agent from the size of the number "
            "(PROJECT_RULES 5.4). The one scenario where the whole point is that "
            "the correct verdict is BLOCK or ESCALATE on a true positive."
        ),
    ),
)


SCENARIOS: Mapping[ScenarioId, ScenarioSpec] = {
    spec.scenario_id: spec
    for spec in (
        _NORMAL,
        _UPI_FAILURE_SPIKE,
        _CARD_FAILURE_SPIKE,
        _EVENING_FAILURE_SPIKE,
        _REGIONAL_FAILURE,
        _PROVIDER_FAILURE,
        _MULTIPLE_FAILURES,
        _FALSE_ALARM,
        _SMALL_RANDOM_VARIATION,
        _INSUFFICIENT_DATA,
        _RECOVERY_NOT_ELIGIBLE,
    )
}


def get_scenario(scenario_id: ScenarioId) -> ScenarioSpec:
    """Look up a scenario spec, failing loudly on an unknown id."""
    if not isinstance(scenario_id, ScenarioId):
        raise DomainValidationError(f"invalid ScenarioId: {scenario_id!r}")
    spec = SCENARIOS.get(scenario_id)
    if spec is None:
        raise DomainValidationError(f"no spec registered for {scenario_id.value}")
    return spec


def all_scenario_ids() -> Tuple[ScenarioId, ...]:
    """Every scenario id, in the order declared in ``ScenarioId``."""
    return tuple(ScenarioId)


def incident_scenario_ids() -> Tuple[ScenarioId, ...]:
    """Scenarios where a real degradation exists."""
    return tuple(sid for sid in ScenarioId if SCENARIOS[sid].ground_truth.is_incident)


def restraint_scenario_ids() -> Tuple[ScenarioId, ...]:
    """Scenarios where the correct behaviour is to take no corrective action.

    Includes ``RECOVERY_NOT_ELIGIBLE``, which is a real incident with no
    appropriate action — restraint is not the same thing as absence of a problem.
    """
    return tuple(
        sid for sid in ScenarioId if not SCENARIOS[sid].ground_truth.expected_action_eligible
    )


def ground_truth_for(scenario_id: ScenarioId) -> GroundTruth:
    """Evaluation-only label lookup. Never call this from a production path."""
    return get_scenario(scenario_id).ground_truth


def _assert_registry_complete() -> Dict[ScenarioId, ScenarioSpec]:
    """Fail at import if a ``ScenarioId`` has no spec.

    An unregistered scenario would otherwise show up as a silently skipped
    evaluation case rather than an error.
    """
    missing = [sid.value for sid in ScenarioId if sid not in SCENARIOS]
    if missing:
        raise DomainValidationError(f"scenarios missing a spec: {missing}")
    return dict(SCENARIOS)


_assert_registry_complete()
