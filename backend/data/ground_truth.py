"""Ground-truth labels for the synthetic scenario set.

**Evaluation only.** Nothing in this module may reach a production code path or an
LLM prompt (PROJECT_RULES 2.7).

The separation is structural, not conventional (ADR-005): the production contract
``domain.Payment`` has no label fields at all, so there is no field for a label to
leak through. Labels live here and on ``SyntheticPayment``, and the dataset's
agent-facing accessors return plain ``Payment`` objects.

These labels are what let us measure the agent honestly — including whether it has
the restraint to do nothing when nothing is wrong.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from ..domain.enums import Dimension
from ..domain.errors import DomainValidationError


class ScenarioId(str, Enum):
    """The Day-2 scenario set.

    Weighted towards restraint, because an agent that acts on everything is worse
    than no agent. Three counts, and they are deliberately not the same number:

    * **Seven are real degradations** (``is_incident``).
    * **Six warrant a corrective action** (``expected_action_eligible``).
    * **Five have no degradation at all.**

    The gap between the first two is ``RECOVERY_NOT_ELIGIBLE``: a genuine incident
    where acting is still wrong. Detection and authorisation are different
    questions, and collapsing them is the mistake this set is built to catch.
    """

    NORMAL = "normal"
    UPI_FAILURE_SPIKE = "upi_failure_spike"
    CARD_FAILURE_SPIKE = "card_failure_spike"
    EVENING_FAILURE_SPIKE = "evening_failure_spike"
    REGIONAL_FAILURE = "regional_failure"
    PROVIDER_FAILURE = "provider_failure"
    MULTIPLE_FAILURES = "multiple_failures"
    FALSE_ALARM = "false_alarm"
    SMALL_RANDOM_VARIATION = "small_random_variation"
    INSUFFICIENT_DATA = "insufficient_data"
    RECOVERY_NOT_ELIGIBLE = "recovery_not_eligible"


@dataclass(frozen=True)
class GroundTruth:
    """What the agent *should* conclude about a scenario.

    Attributes:
        is_incident: Whether a real degradation exists. ``False`` for the
            restraint scenarios.
        has_sufficient_data: Whether there is enough data to make any claim. When
            ``False``, the correct answer is "I cannot tell" — not "all healthy".
        expected_primary_dimension: Where the problem concentrates. ``None`` when
            no single dimension explains it (as in ``MULTIPLE_FAILURES``), in
            which case a confident single-cause answer is *wrong*.
        expected_contributing_values: Dimension values that genuinely contribute.
        expected_root_cause: Short description of the true cause.
        expected_action_eligible: Whether a corrective action is appropriate. A
            real incident can still be action-ineligible — see
            ``RECOVERY_NOT_ELIGIBLE``.
        requires_same_hour_baseline: Whether a naive 24-hour pooled baseline
            reaches the wrong conclusion here. Marks the scenarios that exist to
            catch baseline-selection bugs.
        notes: Why this scenario exists and what it is testing.
    """

    scenario_id: ScenarioId
    is_incident: bool
    has_sufficient_data: bool
    expected_root_cause: str
    expected_action_eligible: bool
    notes: str
    expected_primary_dimension: Optional[Dimension] = None
    expected_contributing_values: Tuple[str, ...] = ()
    requires_same_hour_baseline: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, ScenarioId):
            raise DomainValidationError(f"invalid ScenarioId: {self.scenario_id!r}")
        for name in ("is_incident", "has_sufficient_data", "expected_action_eligible",
                     "requires_same_hour_baseline"):
            if not isinstance(getattr(self, name), bool):
                raise DomainValidationError(f"GroundTruth.{name} must be a bool")
        for name in ("expected_root_cause", "notes"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"GroundTruth.{name} must be non-empty")
        if self.expected_primary_dimension is not None and not isinstance(
            self.expected_primary_dimension, Dimension
        ):
            raise DomainValidationError("invalid expected_primary_dimension")
        if not isinstance(self.expected_contributing_values, tuple):
            raise DomainValidationError("expected_contributing_values must be a tuple")

        # Consistency guards, so a mislabelled scenario fails at import rather
        # than quietly skewing an evaluation score.
        if self.expected_action_eligible and not self.is_incident:
            raise DomainValidationError(
                f"{self.scenario_id.value}: an action cannot be eligible when there is no incident"
            )
        if self.is_incident and not self.has_sufficient_data:
            raise DomainValidationError(
                f"{self.scenario_id.value}: cannot assert an incident without sufficient data"
            )
