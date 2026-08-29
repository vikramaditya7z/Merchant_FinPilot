"""Configurable thresholds for deterministic incident detection.

PROJECT_RULES 4.9 / ARCHITECTURE.md §8 (ADR-006).

Thresholds are configuration, versioned and recorded with any detection outcome,
never constants buried in arithmetic.

Floating-point numbers are strictly forbidden for financial metrics (percentage
points, counts, lift ratios). Float is permitted solely for statistical test
quantities (p-value, z-score) per PROJECT_RULES 1.6.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ..domain.enums import Severity
from ..domain.errors import DomainValidationError, MoneyPrecisionError

DEFAULT_RULE_VERSION = "detection-v1"


def _require_decimal(value: Decimal, field_name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MoneyPrecisionError(f"{field_name} must be a Decimal, not a float")
    if isinstance(value, int):
        return Decimal(value)
    if not isinstance(value, Decimal):
        raise DomainValidationError(
            f"{field_name} must be a Decimal, got {type(value).__name__}"
        )
    if not value.is_finite():
        raise DomainValidationError(f"{field_name} must be finite, got {value}")
    return value


def _require_count(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{field_name} must be an int, got {value!r}")
    if value < 0:
        raise DomainValidationError(f"{field_name} must be non-negative, got {value}")
    return value


@dataclass(frozen=True)
class DetectionConfig:
    """Configurable criteria for opening a FinancialIncident.

    Attributes:
        min_absolute_deviation_pp: Minimum absolute failure-rate increase in
            percentage points (e.g. Decimal("3.0") means +3.0pp over baseline).
        min_relative_lift: Minimum relative failure-rate lift ratio (e.g.
            Decimal("1.5") means 1.5x baseline failure rate).
        min_decided_count: Minimum decided transactions in the evaluated window
            to permit detection.
        min_excess_failed_transactions: Minimum number of excess failed
            transactions above baseline expectation.
        max_p_value: Maximum two-proportion p-value for significance gating.
        min_z_score: Minimum two-proportion z-score for significance gating.
        require_normal_approximation_valid: If True, detection is gated on
            ``significance.normal_approximation_valid``. A low p-value on thin
            data with an invalid normal approximation is rejected as evidence.
        critical_deviation_pp: Absolute deviation threshold for CRITICAL severity.
        critical_relative_lift: Relative lift threshold for CRITICAL severity.
        high_deviation_pp: Absolute deviation threshold for HIGH severity.
        high_relative_lift: Relative lift threshold for HIGH severity.
        medium_deviation_pp: Absolute deviation threshold for MEDIUM severity.
        medium_relative_lift: Relative lift threshold for MEDIUM severity.
        rule_version: Version identifier for this threshold specification.
    """

    min_absolute_deviation_pp: Decimal = Decimal("3.0")
    min_relative_lift: Optional[Decimal] = Decimal("1.5")
    min_decided_count: int = 30
    min_excess_failed_transactions: int = 1
    max_p_value: Optional[float] = 0.01
    min_z_score: Optional[float] = 2.5
    require_normal_approximation_valid: bool = True

    # Severity classification thresholds
    critical_deviation_pp: Decimal = Decimal("15.0")
    critical_relative_lift: Decimal = Decimal("4.0")
    high_deviation_pp: Decimal = Decimal("8.0")
    high_relative_lift: Decimal = Decimal("2.5")
    medium_deviation_pp: Decimal = Decimal("4.0")
    medium_relative_lift: Decimal = Decimal("1.8")

    rule_version: str = DEFAULT_RULE_VERSION

    def __post_init__(self) -> None:
        # Validate primary detection thresholds
        _require_decimal(self.min_absolute_deviation_pp, "min_absolute_deviation_pp")
        if self.min_absolute_deviation_pp <= Decimal(0):
            raise DomainValidationError("min_absolute_deviation_pp must be positive")

        if self.min_relative_lift is not None:
            _require_decimal(self.min_relative_lift, "min_relative_lift")
            if self.min_relative_lift <= Decimal(1):
                raise DomainValidationError("min_relative_lift must be > 1.0")

        _require_count(self.min_decided_count, "min_decided_count")
        if self.min_decided_count == 0:
            raise DomainValidationError("min_decided_count must be positive")

        _require_count(
            self.min_excess_failed_transactions, "min_excess_failed_transactions"
        )

        if not isinstance(self.require_normal_approximation_valid, bool):
            raise DomainValidationError("require_normal_approximation_valid must be a bool")

        if self.max_p_value is not None:
            if not isinstance(self.max_p_value, float):
                raise DomainValidationError("max_p_value must be a float")
            if not (0.0 < self.max_p_value <= 1.0):
                raise DomainValidationError("max_p_value must be in (0.0, 1.0]")

        if self.min_z_score is not None:
            if not isinstance(self.min_z_score, float):
                raise DomainValidationError("min_z_score must be a float")
            if self.min_z_score <= 0.0:
                raise DomainValidationError("min_z_score must be positive")

        # Validate severity thresholds
        for name in (
            "critical_deviation_pp",
            "critical_relative_lift",
            "high_deviation_pp",
            "high_relative_lift",
            "medium_deviation_pp",
            "medium_relative_lift",
        ):
            _require_decimal(getattr(self, name), name)

        if not (self.critical_deviation_pp >= self.high_deviation_pp >= self.medium_deviation_pp):
            raise DomainValidationError(
                "Severity deviation thresholds must satisfy critical >= high >= medium"
            )

        if not (
            self.critical_relative_lift
            >= self.high_relative_lift
            >= self.medium_relative_lift
        ):
            raise DomainValidationError(
                "Severity lift thresholds must satisfy critical >= high >= medium"
            )

        if not isinstance(self.rule_version, str) or not self.rule_version.strip():
            raise DomainValidationError("rule_version must be a non-empty string")
