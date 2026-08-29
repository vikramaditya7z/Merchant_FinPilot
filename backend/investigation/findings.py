"""Structured findings and report contracts for deterministic investigation.

All findings are traceable to deterministic calculations from ``backend.financial``.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Mapping, Optional, Tuple

from ..domain.enums import Dimension, SourceConfidence
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialEvidence
from ..domain.metrics import (
    DimensionBreakdown,
    Rate,
    SignificanceResult,
    TransactionCounts,
)
from ..domain.money import Money
from ..domain.window import TimeWindow, require_utc
from .enums import EvidenceStrength


@dataclass(frozen=True)
class DimensionalFinding:
    """A verified finding for a single slice of a dimension.

    Attributes:
        dimension: The investigated dimension (e.g. PAYMENT_METHOD, REGION).
        value: The slice value (e.g. 'upi', 'IN-KA').
        strength: The evaluated empirical strength of association.
        counts: Transaction counts in this slice.
        failed_gmv: Monetary exposure of failures in this slice.
        source_confidence: Whether this dimension is OBSERVED or ENRICHED.
        summary: Factual, traceable description of the slice's metrics.
        failure_rate: Observed failure rate of this slice (if population dimension).
        baseline_failure_rate: Historical failure rate of this slice (if available).
        deviation_pp: Absolute percentage-point change vs baseline.
        relative_lift: Relative lift ratio vs baseline.
        share_of_failures: Fraction of all incident-window failures in this slice.
        significance: Two-proportion test result vs baseline (if computable).
    """

    dimension: Dimension
    value: str
    strength: EvidenceStrength
    counts: TransactionCounts
    failed_gmv: Money
    source_confidence: SourceConfidence
    summary: str
    failure_rate: Optional[Rate] = None
    baseline_failure_rate: Optional[Rate] = None
    deviation_pp: Optional[Decimal] = None
    relative_lift: Optional[Decimal] = None
    share_of_failures: Optional[Decimal] = None
    significance: Optional[SignificanceResult] = None

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, Dimension):
            raise DomainValidationError(f"invalid Dimension: {self.dimension!r}")
        if not isinstance(self.value, str) or not self.value.strip():
            raise DomainValidationError("DimensionalFinding.value must be non-empty")
        if not isinstance(self.strength, EvidenceStrength):
            raise DomainValidationError(f"invalid EvidenceStrength: {self.strength!r}")
        if not isinstance(self.counts, TransactionCounts):
            raise DomainValidationError("DimensionalFinding.counts must be TransactionCounts")
        if not isinstance(self.failed_gmv, Money):
            raise DomainValidationError("DimensionalFinding.failed_gmv must be Money")
        if not isinstance(self.source_confidence, SourceConfidence):
            raise DomainValidationError(
                f"invalid SourceConfidence: {self.source_confidence!r}"
            )
        if not isinstance(self.summary, str) or len(self.summary.strip()) < 10:
            raise DomainValidationError(
                "DimensionalFinding.summary must be at least 10 characters"
            )


@dataclass(frozen=True)
class InvestigationReport:
    """The complete verified result of an incident investigation.

    Attributes:
        incident_id: ID of the incident investigated.
        window: The time window evaluated.
        investigated_at: Timestamp of the investigation (aware UTC).
        has_sufficient_evidence: True if data permitted meaningful analysis.
        primary_findings: Strongly supported candidate concentrations.
        secondary_findings: Possible contributing factors / moderate findings.
        breakdowns: Mapping from Dimension to full DimensionBreakdown.
        evidence: Tuple of verified FinancialEvidence objects.
        summary: High-level evidence-backed summary for merchant/agent.
        has_multiple_concentrations: True if multiple independent dimensions show
            strong evidence (as in MULTIPLE_FAILURES).
    """

    incident_id: str
    window: TimeWindow
    investigated_at: datetime
    has_sufficient_evidence: bool
    primary_findings: Tuple[DimensionalFinding, ...]
    secondary_findings: Tuple[DimensionalFinding, ...]
    breakdowns: Mapping[Dimension, DimensionBreakdown]
    evidence: Tuple[FinancialEvidence, ...]
    summary: str
    has_multiple_concentrations: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, str) or not self.incident_id.strip():
            raise DomainValidationError("InvestigationReport.incident_id must be non-empty")
        if not isinstance(self.window, TimeWindow):
            raise DomainValidationError("InvestigationReport.window must be a TimeWindow")
        object.__setattr__(
            self,
            "investigated_at",
            require_utc(self.investigated_at, "InvestigationReport.investigated_at"),
        )
        if not isinstance(self.has_sufficient_evidence, bool):
            raise DomainValidationError("has_sufficient_evidence must be a bool")
        if not isinstance(self.primary_findings, tuple):
            raise DomainValidationError("primary_findings must be a tuple")
        if not isinstance(self.secondary_findings, tuple):
            raise DomainValidationError("secondary_findings must be a tuple")
        if not isinstance(self.breakdowns, dict) and not isinstance(
            self.breakdowns, Mapping
        ):
            raise DomainValidationError("breakdowns must be a mapping")
        if not isinstance(self.evidence, tuple):
            raise DomainValidationError("evidence must be a tuple")
        if not isinstance(self.summary, str) or len(self.summary.strip()) < 10:
            raise DomainValidationError(
                "InvestigationReport.summary must be at least 10 characters"
            )
        if not isinstance(self.has_multiple_concentrations, bool):
            raise DomainValidationError("has_multiple_concentrations must be a bool")
