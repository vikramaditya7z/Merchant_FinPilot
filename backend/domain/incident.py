"""Incident and evidence contracts.

An incident is opened by deterministic detection, never by the agent
(PROJECT_RULES 3.11). Evidence accumulates as the agent investigates, and every
piece is a *typed* financial result computed by ``backend/financial/`` — not a
free-form blob the agent wrote (PROJECT_RULES 3.5).
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

from .canonical import short_digest
from .enums import (
    Dimension,
    IncidentStatus,
    IncidentType,
    Severity,
    SourceConfidence,
)
from .errors import DomainValidationError
from .metrics import DimensionBreakdown, FinancialMetrics
from .window import TimeWindow, require_utc

MIN_SUMMARY_LENGTH = 10


@dataclass(frozen=True)
class FinancialEvidence:
    """One verified finding produced during investigation.

    Holds typed results, not a dict the agent authored. ``summary`` is prose for
    the agent and the merchant to read; the numbers next to it are the
    deterministic values the summary must be consistent with, and the Financial
    Verifier can re-derive them.
    """

    evidence_id: str
    incident_id: str
    summary: str
    window: TimeWindow
    computed_at: datetime
    source_confidence: SourceConfidence = SourceConfidence.OBSERVED
    metrics: Optional[FinancialMetrics] = None
    breakdown: Optional[DimensionBreakdown] = None
    dimension: Optional[Dimension] = None

    def __post_init__(self) -> None:
        for name in ("evidence_id", "incident_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"FinancialEvidence.{name} must be non-empty")
        if not isinstance(self.summary, str) or len(self.summary.strip()) < MIN_SUMMARY_LENGTH:
            raise DomainValidationError(
                f"FinancialEvidence.summary must be at least {MIN_SUMMARY_LENGTH} characters"
            )
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("FinancialEvidence.window must be a TimeWindow")
        object.__setattr__(
            self, "computed_at", require_utc(self.computed_at, "FinancialEvidence.computed_at")
        )
        if not isinstance(self.source_confidence, SourceConfidence):
            raise DomainValidationError("invalid source_confidence")
        if self.metrics is not None and not isinstance(self.metrics, FinancialMetrics):
            raise DomainValidationError("FinancialEvidence.metrics must be FinancialMetrics")
        if self.breakdown is not None and not isinstance(self.breakdown, DimensionBreakdown):
            raise DomainValidationError("FinancialEvidence.breakdown must be DimensionBreakdown")
        if self.metrics is None and self.breakdown is None:
            # Prose with no deterministic result behind it is not evidence.
            raise DomainValidationError(
                "FinancialEvidence must carry metrics or a breakdown; "
                "a narrative alone is not evidence"
            )
        if self.breakdown is not None:
            if self.dimension is None:
                object.__setattr__(self, "dimension", self.breakdown.dimension)
            elif self.dimension is not self.breakdown.dimension:
                raise DomainValidationError("dimension does not match breakdown dimension")

    def is_fresh_at(self, now: datetime, max_age_seconds: int) -> bool:
        """Whether this evidence is recent enough to act on.

        Acting on facts that have since changed is a real failure mode, so
        staleness is checked explicitly rather than assumed away
        (ARCHITECTURE.md 10).
        """
        if isinstance(max_age_seconds, bool) or not isinstance(max_age_seconds, int):
            raise DomainValidationError("max_age_seconds must be an int")
        if max_age_seconds <= 0:
            raise DomainValidationError("max_age_seconds must be positive")
        age = (require_utc(now) - self.computed_at).total_seconds()
        return 0 <= age <= max_age_seconds


@dataclass(frozen=True)
class FinancialIncident:
    """A detected financial degradation under investigation.

    ``incident_key`` is the idempotency key: a stable identity derived from
    what the incident *is*, so repeated detection over the same window
    recognises the same incident instead of opening a new one every poll
    (ARCHITECTURE.md 15).
    """

    incident_id: str
    merchant_id: str
    incident_type: IncidentType
    status: IncidentStatus
    severity: Severity
    detected_at: datetime
    window: TimeWindow
    metrics: FinancialMetrics
    evidence: Tuple[FinancialEvidence, ...] = ()
    primary_dimension: Optional[Dimension] = None
    primary_dimension_value: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("incident_id", "merchant_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise DomainValidationError(f"FinancialIncident.{name} must be non-empty")
        if not isinstance(self.incident_type, IncidentType):
            raise DomainValidationError(f"invalid IncidentType: {self.incident_type!r}")
        if not isinstance(self.status, IncidentStatus):
            raise DomainValidationError(f"invalid IncidentStatus: {self.status!r}")
        if not isinstance(self.severity, Severity):
            raise DomainValidationError(f"invalid Severity: {self.severity!r}")
        object.__setattr__(
            self, "detected_at", require_utc(self.detected_at, "FinancialIncident.detected_at")
        )
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("FinancialIncident.window must be a TimeWindow")
        if not isinstance(self.metrics, FinancialMetrics):
            raise DomainValidationError("FinancialIncident.metrics must be FinancialMetrics")
        if self.metrics.window != self.window:
            raise DomainValidationError("incident metrics window must match incident window")
        if not isinstance(self.evidence, tuple):
            raise DomainValidationError("FinancialIncident.evidence must be a tuple")
        seen = set()
        for item in self.evidence:
            if not isinstance(item, FinancialEvidence):
                raise DomainValidationError("evidence must contain FinancialEvidence")
            if item.incident_id != self.incident_id:
                raise DomainValidationError(
                    f"evidence {item.evidence_id} belongs to another incident"
                )
            if item.evidence_id in seen:
                raise DomainValidationError(f"duplicate evidence_id: {item.evidence_id}")
            seen.add(item.evidence_id)
        if self.primary_dimension is not None and not isinstance(
            self.primary_dimension, Dimension
        ):
            raise DomainValidationError("invalid primary_dimension")
        if self.primary_dimension_value is not None and self.primary_dimension is None:
            raise DomainValidationError(
                "primary_dimension_value requires primary_dimension"
            )

    @property
    def incident_key(self) -> str:
        """Stable identity for deduplicating detection of the same degradation."""
        return "inc_" + short_digest(
            {
                "merchant_id": self.merchant_id,
                "incident_type": self.incident_type.value,
                "window": self.window.label(),
                "dimension": self.primary_dimension.value if self.primary_dimension else None,
                "dimension_value": self.primary_dimension_value,
            }
        )

    @property
    def evidence_ids(self) -> Tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)

    def find_evidence(self, evidence_id: str) -> Optional[FinancialEvidence]:
        """Resolve an evidence reference, or ``None`` if the agent invented it."""
        for item in self.evidence:
            if item.evidence_id == evidence_id:
                return item
        return None

    def with_evidence(self, item: FinancialEvidence) -> "FinancialIncident":
        """Return a new incident with one more piece of evidence.

        Immutable append: a recorded financial fact is never edited in place
        (PROJECT_RULES 10.7).
        """
        if not isinstance(item, FinancialEvidence):
            raise DomainValidationError("with_evidence() requires FinancialEvidence")
        return FinancialIncident(
            incident_id=self.incident_id,
            merchant_id=self.merchant_id,
            incident_type=self.incident_type,
            status=self.status,
            severity=self.severity,
            detected_at=self.detected_at,
            window=self.window,
            metrics=self.metrics,
            evidence=self.evidence + (item,),
            primary_dimension=self.primary_dimension,
            primary_dimension_value=self.primary_dimension_value,
        )
