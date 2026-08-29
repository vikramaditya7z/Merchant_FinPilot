"""Deterministic evaluation of financial metrics against detection criteria.

PROJECT_RULES 4.9 / ARCHITECTURE.md §8 (ADR-006).

This module contains the pure evaluation logic that inspects a
``FinancialMetrics`` object and determines whether it meets all criteria
to trigger an incident.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Optional, Tuple

from ..domain.enums import Severity
from ..domain.errors import DomainValidationError
from ..domain.metrics import FinancialMetrics
from .config import DetectionConfig


class DetectionReason(str, Enum):
    """Specific reasons an evaluation triggered or failed to trigger."""

    TRIGGERED = "triggered"
    INSUFFICIENT_DECIDED_COUNT = "insufficient_decided_count"
    NO_BASELINE = "no_baseline"
    INSUFFICIENT_BASELINE = "insufficient_baseline"
    NO_DEVIATION = "no_deviation"
    NOT_WORSE_THAN_BASELINE = "not_worse_than_baseline"
    INSUFFICIENT_ABSOLUTE_DEVIATION = "insufficient_absolute_deviation"
    INSUFFICIENT_RELATIVE_LIFT = "insufficient_relative_lift"
    INSUFFICIENT_EXCESS_FAILURES = "insufficient_excess_failures"
    NO_SIGNIFICANCE = "no_significance"
    INVALID_NORMAL_APPROXIMATION = "invalid_normal_approximation"
    INSIGNIFICANT_P_VALUE = "insignificant_p_value"
    INSIGNIFICANT_Z_SCORE = "insignificant_z_score"


@dataclass(frozen=True)
class DetectionEvaluation:
    """The result of evaluating a ``FinancialMetrics`` against a ``DetectionConfig``.

    Attributes:
        triggered: Whether all detection criteria are satisfied.
        reasons: Tuple of reasons explaining the verdict.
        severity: Computed severity if triggered, else None.
        config: The configuration evaluated against.
        metrics: The evaluated metrics.
    """

    triggered: bool
    reasons: Tuple[DetectionReason, ...]
    severity: Optional[Severity]
    config: DetectionConfig
    metrics: FinancialMetrics

    def __post_init__(self) -> None:
        if not isinstance(self.triggered, bool):
            raise DomainValidationError("DetectionEvaluation.triggered must be a bool")
        if not isinstance(self.reasons, tuple):
            raise DomainValidationError("DetectionEvaluation.reasons must be a tuple")
        for r in self.reasons:
            if not isinstance(r, DetectionReason):
                raise DomainValidationError(f"invalid DetectionReason: {r!r}")
        if self.severity is not None and not isinstance(self.severity, Severity):
            raise DomainValidationError(f"invalid Severity: {self.severity!r}")
        if not isinstance(self.config, DetectionConfig):
            raise DomainValidationError("DetectionEvaluation.config must be a DetectionConfig")
        if not isinstance(self.metrics, FinancialMetrics):
            raise DomainValidationError("DetectionEvaluation.metrics must be FinancialMetrics")
        if self.triggered and self.severity is None:
            raise DomainValidationError("triggered evaluation must carry a Severity")
        if not self.triggered and self.severity is not None:
            raise DomainValidationError("non-triggered evaluation must not carry a Severity")


def determine_severity(
    metrics: FinancialMetrics, config: DetectionConfig
) -> Severity:
    """Deterministically classify incident severity from metric magnitudes.

    Classification order: CRITICAL -> HIGH -> MEDIUM -> LOW.
    """
    if not isinstance(metrics, FinancialMetrics):
        raise DomainValidationError("determine_severity requires FinancialMetrics")
    if not isinstance(config, DetectionConfig):
        raise DomainValidationError("determine_severity requires DetectionConfig")

    if metrics.deviation is None:
        return Severity.LOW

    dev_pp = metrics.deviation.absolute_percentage_points
    lift = metrics.deviation.relative_lift

    # Critical check
    if dev_pp >= config.critical_deviation_pp:
        return Severity.CRITICAL
    if lift is not None and lift >= config.critical_relative_lift:
        return Severity.CRITICAL

    # High check
    if dev_pp >= config.high_deviation_pp:
        return Severity.HIGH
    if lift is not None and lift >= config.high_relative_lift:
        return Severity.HIGH

    # Medium check
    if dev_pp >= config.medium_deviation_pp:
        return Severity.MEDIUM
    if lift is not None and lift >= config.medium_relative_lift:
        return Severity.MEDIUM

    return Severity.LOW


def evaluate_metrics(
    metrics: FinancialMetrics, config: Optional[DetectionConfig] = None
) -> DetectionEvaluation:
    """Evaluate financial metrics against detection criteria.

    Pure, deterministic evaluation. Does not mutate inputs, does not read the
    clock.
    """
    if not isinstance(metrics, FinancialMetrics):
        raise DomainValidationError("evaluate_metrics requires FinancialMetrics")
    cfg = config if config is not None else DetectionConfig()
    if not isinstance(cfg, DetectionConfig):
        raise DomainValidationError("config must be a DetectionConfig instance")

    reasons = []

    # 1. Minimum decided sample check in the evaluated window
    if metrics.counts.decided < cfg.min_decided_count:
        reasons.append(DetectionReason.INSUFFICIENT_DECIDED_COUNT)

    # 2. Baseline presence and sufficiency
    if metrics.baseline is None:
        reasons.append(DetectionReason.NO_BASELINE)
    elif not metrics.baseline.is_sufficient:
        reasons.append(DetectionReason.INSUFFICIENT_BASELINE)

    # 3. Deviation presence and direction
    if metrics.deviation is None:
        reasons.append(DetectionReason.NO_DEVIATION)
    else:
        if not metrics.deviation.is_worse_than_baseline:
            reasons.append(DetectionReason.NOT_WORSE_THAN_BASELINE)
        elif metrics.deviation.absolute_percentage_points < cfg.min_absolute_deviation_pp:
            reasons.append(DetectionReason.INSUFFICIENT_ABSOLUTE_DEVIATION)

        if cfg.min_relative_lift is not None:
            if (
                metrics.deviation.relative_lift is None
                or metrics.deviation.relative_lift < cfg.min_relative_lift
            ):
                reasons.append(DetectionReason.INSUFFICIENT_RELATIVE_LIFT)

    # 4. Excess failed transactions
    if cfg.min_excess_failed_transactions > 0:
        if (
            metrics.revenue_risk is None
            or metrics.revenue_risk.excess_failed_transactions
            < cfg.min_excess_failed_transactions
        ):
            reasons.append(DetectionReason.INSUFFICIENT_EXCESS_FAILURES)

    # 5. Statistical significance gating
    if metrics.significance is None:
        reasons.append(DetectionReason.NO_SIGNIFICANCE)
    else:
        # Crucial check: A low p-value alone is inadmissible when the normal
        # approximation is invalid (ADR-006, PROJECT_RULES 4.9).
        if (
            cfg.require_normal_approximation_valid
            and not metrics.significance.normal_approximation_valid
        ):
            reasons.append(DetectionReason.INVALID_NORMAL_APPROXIMATION)

        if cfg.max_p_value is not None and metrics.significance.p_value > cfg.max_p_value:
            reasons.append(DetectionReason.INSIGNIFICANT_P_VALUE)

        if cfg.min_z_score is not None and metrics.significance.z_score < cfg.min_z_score:
            reasons.append(DetectionReason.INSIGNIFICANT_Z_SCORE)

    if not reasons:
        severity = determine_severity(metrics, cfg)
        return DetectionEvaluation(
            triggered=True,
            reasons=(DetectionReason.TRIGGERED,),
            severity=severity,
            config=cfg,
            metrics=metrics,
        )
    else:
        return DetectionEvaluation(
            triggered=False,
            reasons=tuple(reasons),
            severity=None,
            config=cfg,
            metrics=metrics,
        )
