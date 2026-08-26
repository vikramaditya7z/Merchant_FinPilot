"""Deterministic synthetic dataset generator.

Turns a ``ScenarioSpec`` plus an integer seed into payments. Same spec and same
seed always produce byte-identical output, on any machine, in any Python 3.11+
(PROJECT_RULES 9.4) — a test that asserts a revenue figure is worthless if the
data underneath it drifts.

**Determinism rules this module follows:**

* Randomness comes from a local ``random.Random(seed)`` instance, never the
  module-level functions, so nothing else in the process can perturb the stream.
* Only ``Random.random()`` is used. ``shuffle`` / ``sample`` / ``choice`` /
  ``gauss`` are avoided because their internal algorithms are implementation
  details that have changed between CPython versions; ``random()`` is a
  documented Mersenne Twister output and is stable.
* Draws happen in a fixed order per payment, so adding a field later changes the
  stream in one predictable place rather than everywhere.
* No clock reads. The timeline is anchored to an explicit ``anchor`` datetime.

**Ground-truth separation (ADR-005):** the generator returns
``SyntheticPayment`` records that carry labels, and a ``ScenarioDataset`` whose
agent-facing accessors — ``agent_payments()`` and ``agent_enriched()`` — return
plain ``Payment`` / ``EnrichedPayment`` objects with no label field of any kind.
Leakage is structurally impossible, not a matter of remembering to strip.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterable, List, Optional, Sequence, Tuple

from ..domain.enums import (
    Currency,
    FailureCategory,
    PaymentMethod,
    PaymentStatus,
    SourceConfidence,
)
from ..domain.errors import DomainValidationError
from ..domain.money import Money
from ..domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ..domain.window import UTC, TimeWindow, require_utc
from .ground_truth import GroundTruth, ScenarioId
from .scenarios import BACKGROUND_FAILURES, Anomaly, ScenarioSpec, get_scenario

# Default seed, mirrored by FINPILOT_DATASET_SEED in .env.example. A fixed
# default means the committed tests and a fresh checkout see the same data.
DEFAULT_SEED = 20260826

# Fixed timeline anchor: the incident window ends here. Chosen rather than "now"
# so a generated dataset is reproducible tomorrow (PROJECT_RULES 4.1).
DEFAULT_ANCHOR = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)


def _weighted_pick(rng: random.Random, options: Sequence[Tuple[str, Decimal]]) -> str:
    """Pick a value by relative weight using exactly one ``random()`` draw."""
    total = sum((weight for _, weight in options), Decimal(0))
    if total <= 0:
        raise DomainValidationError("weighted_pick requires a positive total weight")
    # Decimal arithmetic on the threshold keeps the comparison exact; the single
    # float from random() is the only float in the pipeline and never touches
    # money.
    threshold = Decimal(str(rng.random())) * total
    cumulative = Decimal(0)
    for value, weight in options:
        cumulative += weight
        if threshold < cumulative:
            return value
    return options[-1][0]


def _uniform_int(rng: random.Random, low: int, high: int) -> int:
    """Uniform integer in ``[low, high]`` from one ``random()`` draw."""
    if high < low:
        raise DomainValidationError(f"empty integer range [{low}, {high}]")
    span = high - low + 1
    return low + min(span - 1, int(rng.random() * span))


@dataclass(frozen=True)
class SyntheticPayment:
    """A generated payment plus its evaluation labels.

    **Never pass this to the agent or into a prompt.** Use ``to_payment()`` or
    ``to_enriched()``, which return the production contracts. The label fields do
    not exist on those types, so a leak requires deliberately reaching into this
    wrapper (PROJECT_RULES 2.7).

    Attributes:
        matched_anomaly: Label of the injected anomaly that governed this
            payment's outcome, or ``None`` if it was ordinary background traffic.
            Lets an evaluation ask "did the agent find the failures we planted"
            rather than only "did it produce the right summary".
        in_baseline_period: Whether this payment predates the incident window.
    """

    payment: Payment
    enrichment: PaymentEnrichment
    scenario_id: ScenarioId
    matched_anomaly: Optional[str]
    in_baseline_period: bool

    def __post_init__(self) -> None:
        if not isinstance(self.payment, Payment):
            raise DomainValidationError("SyntheticPayment.payment must be a Payment")
        if not isinstance(self.enrichment, PaymentEnrichment):
            raise DomainValidationError(
                "SyntheticPayment.enrichment must be a PaymentEnrichment"
            )
        if self.enrichment.payment_id != self.payment.id:
            raise DomainValidationError("enrichment does not belong to this payment")
        if not isinstance(self.scenario_id, ScenarioId):
            raise DomainValidationError(f"invalid ScenarioId: {self.scenario_id!r}")

    def to_payment(self) -> Payment:
        """The observed payment, with no labels attached. Safe for agent input."""
        return self.payment

    def to_enriched(self) -> EnrichedPayment:
        """Payment plus derived dimensions. Safe for agent input."""
        return EnrichedPayment(payment=self.payment, enrichment=self.enrichment)


@dataclass(frozen=True)
class ScenarioDataset:
    """A generated scenario: records, windows, and the labels to score against.

    ``records`` carries labels and is for evaluation code. Agent and engine input
    comes from ``agent_enriched()`` / ``agent_payments()``.
    """

    scenario_id: ScenarioId
    spec: ScenarioSpec
    seed: int
    anchor: datetime
    incident_window: TimeWindow
    baseline_window: TimeWindow
    records: Tuple[SyntheticPayment, ...]

    def __post_init__(self) -> None:
        if self.baseline_window.end != self.incident_window.start:
            raise DomainValidationError(
                "baseline window must end exactly where the incident window begins"
            )
        if self.baseline_window.overlaps(self.incident_window):
            # Belt and braces: an overlap would let incident data into its own
            # baseline and mathematically suppress the deviation it should show.
            raise DomainValidationError("baseline and incident windows must not overlap")

    @property
    def ground_truth(self) -> GroundTruth:
        """Evaluation-only labels (ADR-005)."""
        return self.spec.ground_truth

    def agent_payments(self) -> Tuple[Payment, ...]:
        """Every payment as an unlabelled production contract."""
        return tuple(record.payment for record in self.records)

    def agent_enriched(self) -> Tuple[EnrichedPayment, ...]:
        """Every payment with derived dimensions, unlabelled."""
        return tuple(record.to_enriched() for record in self.records)

    def incident_enriched(self) -> Tuple[EnrichedPayment, ...]:
        """Only the incident window. Filtered on ``created_at``, not on labels."""
        return tuple(
            record.to_enriched()
            for record in self.records
            if self.incident_window.contains(record.payment.created_at)
        )

    def baseline_enriched(self) -> Tuple[EnrichedPayment, ...]:
        """Only the historical period. Filtered on ``created_at``, not on labels."""
        return tuple(
            record.to_enriched()
            for record in self.records
            if self.baseline_window.contains(record.payment.created_at)
        )

    def planted_failure_ids(self) -> Tuple[str, ...]:
        """Ids of incident-window failures caused by an injected anomaly.

        Evaluation only. This is the set an investigation should account for.
        """
        return tuple(
            record.payment.id
            for record in self.records
            if record.matched_anomaly is not None
            and record.payment.is_failure
            and not record.in_baseline_period
        )


def _resolve_anomaly(
    anomalies: Sequence[Anomaly],
    method: PaymentMethod,
    region: str,
    provider: str,
    hour_utc: int,
    in_baseline: bool,
) -> Optional[Anomaly]:
    """The anomaly governing a payment, or ``None``.

    Last match wins when several apply. Overlap is intentional in
    ``MULTIPLE_FAILURES``: a UPI payment in the affected region is subject to both
    degradations, and in reality the two causes are not cleanly separable either.
    """
    winner: Optional[Anomaly] = None
    for anomaly in anomalies:
        if anomaly.matches(method, region, provider, hour_utc, in_baseline):
            winner = anomaly
    return winner


def generate_scenario(
    scenario_id: ScenarioId,
    seed: int = DEFAULT_SEED,
    anchor: datetime = DEFAULT_ANCHOR,
) -> ScenarioDataset:
    """Generate one scenario deterministically.

    Args:
        scenario_id: Which scenario to build.
        seed: Base seed. Mixed with the scenario name so two scenarios generated
            from the same seed do not share an identical random stream (which
            would make their "independent" failures correlated).
        anchor: End of the incident window, as aware UTC.

    Returns:
        A ``ScenarioDataset``. Identical for identical arguments.
    """
    spec = get_scenario(scenario_id)
    anchor = require_utc(anchor, "anchor")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DomainValidationError("seed must be an int")

    # Derive the timeline backwards from the anchor. The incident window is placed
    # so that its start hour matches the spec, which is what makes the evening
    # scenarios land on evening hours rather than wherever the anchor happens to
    # fall.
    incident_end = anchor.replace(minute=0, second=0, microsecond=0)
    incident_start = incident_end - timedelta(hours=spec.incident_hours)
    shift = (incident_start.hour - spec.incident_window_start_hour) % 24
    incident_start -= timedelta(hours=shift)
    incident_end = incident_start + timedelta(hours=spec.incident_hours)

    incident_window = TimeWindow(incident_start, incident_end)
    baseline_window = TimeWindow(
        incident_start - timedelta(days=spec.baseline_days), incident_start
    )

    # Seed mixing: stable across runs, distinct per scenario. Built from the
    # scenario's own characters rather than hash() because hash() of a str is
    # randomised per process by default.
    name_component = sum(
        (index + 1) * ord(char) for index, char in enumerate(scenario_id.value)
    )
    rng = random.Random(seed * 1_000_003 + name_component)

    records: List[SyntheticPayment] = []
    sequence = 0

    method_options: Tuple[Tuple[str, Decimal], ...] = tuple(
        (profile.method.value, profile.traffic_weight) for profile in spec.method_profiles
    )
    profiles_by_method = {profile.method.value: profile for profile in spec.method_profiles}
    region_options = tuple((code, Decimal(weight)) for code, weight in spec.regions)
    provider_options = tuple((code, Decimal(weight)) for code, weight in spec.providers)
    background_options = tuple(
        (code, Decimal(weight)) for code, _, weight in BACKGROUND_FAILURES
    )
    background_categories = {code: category for code, category, _ in BACKGROUND_FAILURES}

    total_hours = spec.total_hours
    timeline_start = baseline_window.start

    for hour_index in range(total_hours):
        hour_start = timeline_start + timedelta(hours=hour_index)
        in_baseline = hour_start < incident_start
        hour_utc = hour_start.hour

        for slot in range(spec.payments_per_hour):
            sequence += 1

            # Draw order is fixed and must not be reordered: doing so changes
            # every generated dataset and silently invalidates committed
            # expected values.
            method_value = _weighted_pick(rng, method_options)
            region = _weighted_pick(rng, region_options)
            provider = _weighted_pick(rng, provider_options)
            profile = profiles_by_method[method_value]
            method = profile.method

            amount_paise = _uniform_int(
                rng,
                profile.mean_ticket_paise - profile.ticket_spread_paise,
                profile.mean_ticket_paise + profile.ticket_spread_paise,
            )

            # Spread payments across the hour so hourly bucketing has something
            # to work with, and so no two payments share a timestamp.
            second_within_hour = _uniform_int(rng, 0, 3599)
            created_at = hour_start + timedelta(seconds=second_within_hour)

            undecided_draw = Decimal(str(rng.random()))
            failure_draw = Decimal(str(rng.random()))

            anomaly = _resolve_anomaly(
                spec.anomalies, method, region, provider, hour_utc, in_baseline
            )
            effective_failure_rate = (
                anomaly.failure_rate if anomaly is not None else profile.base_failure_rate
            )

            payment_id = f"pay_{scenario_id.value}_{sequence:06d}"
            order_id = f"order_{scenario_id.value}_{sequence:06d}"

            if undecided_draw < spec.undecided_share:
                # In flight. Excluded from every rate denominator
                # (ARCHITECTURE.md 7.2), and carries no error fields.
                status = PaymentStatus.CREATED
                error_code: Optional[str] = None
                failure_category: Optional[FailureCategory] = None
            elif failure_draw < effective_failure_rate:
                status = PaymentStatus.FAILED
                if anomaly is not None:
                    error_code = anomaly.failure_code
                    failure_category = anomaly.failure_category
                else:
                    error_code = _weighted_pick(rng, background_options)
                    failure_category = background_categories[error_code]
            else:
                status = PaymentStatus.CAPTURED
                error_code = None
                failure_category = None

            payment = Payment(
                id=payment_id,
                order_id=order_id,
                created_at=created_at,
                amount=Money(amount_paise, Currency.INR),
                status=status,
                method=method,
                error_code=error_code,
                error_description=(
                    f"synthetic failure ({error_code})" if error_code is not None else None
                ),
            )
            enrichment = PaymentEnrichment(
                payment_id=payment_id,
                region=region,
                provider=provider,
                failure_category=failure_category,
                source_confidence=SourceConfidence.ENRICHED,
            )

            records.append(
                SyntheticPayment(
                    payment=payment,
                    enrichment=enrichment,
                    scenario_id=scenario_id,
                    # Only label the anomaly when it actually governed a failure.
                    # An anomaly-eligible payment that succeeded was not affected
                    # in any observable way.
                    matched_anomaly=(
                        anomaly.label
                        if anomaly is not None and status is PaymentStatus.FAILED
                        else None
                    ),
                    in_baseline_period=in_baseline,
                )
            )

    # Sort by event time so downstream code never depends on generation order.
    # The id is the tiebreaker, keeping the sort total and stable.
    records.sort(key=lambda record: (record.payment.created_at, record.payment.id))

    return ScenarioDataset(
        scenario_id=scenario_id,
        spec=spec,
        seed=seed,
        anchor=anchor,
        incident_window=incident_window,
        baseline_window=baseline_window,
        records=tuple(records),
    )


def generate_all(
    seed: int = DEFAULT_SEED,
    anchor: datetime = DEFAULT_ANCHOR,
    scenario_ids: Optional[Iterable[ScenarioId]] = None,
) -> Tuple[ScenarioDataset, ...]:
    """Generate every scenario (or a chosen subset) from one seed."""
    ids = tuple(scenario_ids) if scenario_ids is not None else tuple(ScenarioId)
    return tuple(generate_scenario(sid, seed=seed, anchor=anchor) for sid in ids)
