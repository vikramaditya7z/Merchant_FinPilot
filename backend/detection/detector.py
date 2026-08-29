"""Deterministic incident detector.

PROJECT_RULES 3.11 / ARCHITECTURE.md §8 (ADR-006).

Turns a ``FinancialMetrics`` into an opened ``FinancialIncident`` when detection
criteria are satisfied.
"""

from datetime import datetime
from typing import Optional, Sequence

from ..domain.canonical import short_digest
from ..domain.enums import (
    BaselineMethod,
    ComparableWindowMode,
    IncidentStatus,
    IncidentType,
    SourceConfidence,
)
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialEvidence, FinancialIncident
from ..domain.metrics import FinancialMetrics, WindowCounts
from ..domain.window import TimeWindow, require_utc
from ..financial.engine import compute_metrics
from ..financial.population import PaymentLike
from .config import DetectionConfig
from .evaluator import DetectionEvaluation, evaluate_metrics


class Detector:
    """Deterministic detector for financial failure-rate degradations.

    Attributes:
        config: Configurable thresholds governing incident opening.
    """

    def __init__(self, config: Optional[DetectionConfig] = None) -> None:
        self.config = config if config is not None else DetectionConfig()
        if not isinstance(self.config, DetectionConfig):
            raise DomainValidationError("Detector config must be a DetectionConfig")

    def evaluate(self, metrics: FinancialMetrics) -> DetectionEvaluation:
        """Evaluate metrics against the configured thresholds."""
        return evaluate_metrics(metrics, self.config)

    def detect(
        self,
        metrics: FinancialMetrics,
        merchant_id: str = "merchant_default",
        incident_id: Optional[str] = None,
        detected_at: Optional[datetime] = None,
    ) -> Optional[FinancialIncident]:
        """Evaluate metrics and open a ``FinancialIncident`` if triggered.

        Args:
            metrics: The deterministic financial measurement of the window.
            merchant_id: Merchant identifier for scope binding.
            incident_id: Optional explicit incident id; auto-generated if None.
            detected_at: Optional detection timestamp as aware UTC; defaults to
                ``metrics.computed_at``.

        Returns:
            A ``FinancialIncident`` with attached evidence if criteria are met;
            otherwise ``None``.
        """
        if not isinstance(metrics, FinancialMetrics):
            raise DomainValidationError("detect requires FinancialMetrics")
        if not isinstance(merchant_id, str) or not merchant_id.strip():
            raise DomainValidationError("merchant_id must be a non-empty string")

        evaluation = self.evaluate(metrics)
        if not evaluation.triggered:
            return None

        when = (
            require_utc(detected_at, "detected_at")
            if detected_at is not None
            else metrics.computed_at
        )

        inc_id = incident_id or "inc_" + short_digest(
            {
                "merchant_id": merchant_id,
                "window": metrics.window.label(),
                "type": IncidentType.PAYMENT_FAILURE_SPIKE.value,
                "version": self.config.rule_version,
            }
        )

        ev_id = "ev_" + short_digest(
            {
                "incident_id": inc_id,
                "window": metrics.window.label(),
                "role": "initial_trigger",
            }
        )

        # Construct verified initial evidence from the metrics
        summary = (
            f"Failure rate of {metrics.failure_rate.as_percent()}% exceeds baseline "
            f"of {metrics.baseline.rate.as_percent()}% by "
            f"{metrics.deviation.absolute_percentage_points}pp "
            f"({metrics.deviation.relative_lift}x lift, "
            f"z={metrics.significance.z_score:.2f})."
        )

        evidence = FinancialEvidence(
            evidence_id=ev_id,
            incident_id=inc_id,
            summary=summary,
            window=metrics.window,
            computed_at=metrics.computed_at,
            source_confidence=SourceConfidence.OBSERVED,
            metrics=metrics,
        )

        return FinancialIncident(
            incident_id=inc_id,
            merchant_id=merchant_id,
            incident_type=IncidentType.PAYMENT_FAILURE_SPIKE,
            status=IncidentStatus.DETECTED,
            severity=evaluation.severity,
            detected_at=when,
            window=metrics.window,
            metrics=metrics,
            evidence=(evidence,),
            primary_dimension=None,
            primary_dimension_value=None,
        )

    def detect_from_payments(
        self,
        payments: Sequence[PaymentLike],
        window: TimeWindow,
        now: datetime,
        baseline_windows: Sequence[WindowCounts],
        merchant_id: str = "merchant_default",
        comparable_mode: Optional[ComparableWindowMode] = None,
        baseline_method: Optional[BaselineMethod] = None,
        incident_id: Optional[str] = None,
    ) -> Optional[FinancialIncident]:
        """Compute metrics and evaluate detection from raw payments and history.

        Args:
            payments: Payments to evaluate (filtered to window internally).
            window: Time window under evaluation.
            now: Current time injection (aware UTC).
            baseline_windows: Historical window counts for baseline comparison.
            merchant_id: Merchant identifier.
            comparable_mode: ALL or SAME_HOUR_OF_DAY window matching.
            baseline_method: POOLED or MEDIAN_OF_WINDOWS.
            incident_id: Optional explicit incident id.

        Returns:
            A ``FinancialIncident`` if triggered; otherwise ``None``.
        """
        metrics = compute_metrics(
            items=payments,
            window=window,
            now=now,
            baseline_windows=baseline_windows,
            comparable_mode=comparable_mode or ComparableWindowMode.ALL,
            baseline_method=baseline_method or BaselineMethod.POOLED,
        )
        return self.detect(
            metrics=metrics,
            merchant_id=merchant_id,
            incident_id=incident_id,
            detected_at=now,
        )


def detect_incident(
    metrics: FinancialMetrics,
    merchant_id: str = "merchant_default",
    config: Optional[DetectionConfig] = None,
    incident_id: Optional[str] = None,
    detected_at: Optional[datetime] = None,
) -> Optional[FinancialIncident]:
    """Pure functional interface for incident detection from metrics."""
    detector = Detector(config=config)
    return detector.detect(
        metrics=metrics,
        merchant_id=merchant_id,
        incident_id=incident_id,
        detected_at=detected_at,
    )


def detect_from_payments(
    payments: Sequence[PaymentLike],
    window: TimeWindow,
    now: datetime,
    baseline_windows: Sequence[WindowCounts],
    merchant_id: str = "merchant_default",
    config: Optional[DetectionConfig] = None,
    comparable_mode: Optional[ComparableWindowMode] = None,
    baseline_method: Optional[BaselineMethod] = None,
    incident_id: Optional[str] = None,
) -> Optional[FinancialIncident]:
    """Pure functional interface for incident detection from raw payments."""
    detector = Detector(config=config)
    return detector.detect_from_payments(
        payments=payments,
        window=window,
        now=now,
        baseline_windows=baseline_windows,
        merchant_id=merchant_id,
        comparable_mode=comparable_mode,
        baseline_method=baseline_method,
        incident_id=incident_id,
    )
