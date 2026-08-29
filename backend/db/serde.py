"""Serialization and deserialization between domain models and persistence formats.

PROJECT_RULES 1.6, 4.2 / ARCHITECTURE.md §6.

Guarantees:
- Integer minor units (paise) are preserved without floating-point conversion.
- Decimals are converted to/from exact strings.
- Timestamps are converted to/from ISO-8601 aware UTC strings.
- Enums are converted to/from their string values.
- All deserialized objects are instantiated through their domain constructors
  and validated automatically.
"""

import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

from ..domain.audit import AuditEvent
from ..domain.canonical import canonical_json
from ..domain.enums import (
    AuditActor,
    AuditEventType,
    BaselineMethod,
    Currency,
    Dimension,
    FailureCategory,
    IncidentStatus,
    IncidentType,
    PaymentMethod,
    PaymentStatus,
    Severity,
    SourceConfidence,
)
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialEvidence, FinancialIncident
from ..domain.metrics import (
    BaselineFailureRate,
    Deviation,
    DimensionBreakdown,
    DimensionSlice,
    FinancialMetrics,
    Rate,
    RevenueRisk,
    SignificanceResult,
    TransactionCounts,
)
from ..domain.money import Money
from ..domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ..domain.window import TimeWindow, require_utc
from ..investigation.enums import EvidenceStrength
from ..investigation.findings import DimensionalFinding, InvestigationReport


# ---------------------------------------------------------------------------
# Rate, Counts & Metrics serialization
# ---------------------------------------------------------------------------

def rate_to_dict(rate: Optional[Rate]) -> Optional[Dict[str, Any]]:
    if rate is None:
        return None
    return {"numerator": rate.numerator, "denominator": rate.denominator}


def dict_to_rate(data: Optional[Mapping[str, Any]]) -> Optional[Rate]:
    if data is None:
        return None
    return Rate(numerator=data["numerator"], denominator=data["denominator"])


def counts_to_dict(counts: TransactionCounts) -> Dict[str, Any]:
    return {
        "succeeded": counts.succeeded,
        "failed": counts.failed,
        "undecided": counts.undecided,
    }


def dict_to_counts(data: Mapping[str, Any]) -> TransactionCounts:
    return TransactionCounts(
        succeeded=data["succeeded"],
        failed=data["failed"],
        undecided=data.get("undecided", 0),
    )


def baseline_to_dict(base: Optional[BaselineFailureRate]) -> Optional[Dict[str, Any]]:
    if base is None:
        return None
    return {
        "method": base.method.value,
        "rate": rate_to_dict(base.rate),
        "windows_considered": base.windows_considered,
        "windows_used": base.windows_used,
        "decided_sample": base.decided_sample,
        "min_decided_required": base.min_decided_required,
    }


def dict_to_baseline(data: Optional[Mapping[str, Any]]) -> Optional[BaselineFailureRate]:
    if data is None:
        return None
    return BaselineFailureRate(
        method=BaselineMethod(data["method"]),
        rate=dict_to_rate(data.get("rate")),
        windows_considered=data["windows_considered"],
        windows_used=data["windows_used"],
        decided_sample=data["decided_sample"],
        min_decided_required=data.get("min_decided_required", 100),
    )


def deviation_to_dict(dev: Optional[Deviation]) -> Optional[Dict[str, Any]]:
    if dev is None:
        return None
    return {
        "current": rate_to_dict(dev.current),
        "baseline": rate_to_dict(dev.baseline),
        "absolute_percentage_points": str(dev.absolute_percentage_points),
        "relative_lift": str(dev.relative_lift) if dev.relative_lift is not None else None,
    }


def dict_to_deviation(data: Optional[Mapping[str, Any]]) -> Optional[Deviation]:
    if data is None:
        return None
    return Deviation(
        current=dict_to_rate(data["current"]),
        baseline=dict_to_rate(data["baseline"]),
        absolute_percentage_points=Decimal(data["absolute_percentage_points"]),
        relative_lift=Decimal(data["relative_lift"]) if data.get("relative_lift") is not None else None,
    )


def significance_to_dict(sig: Optional[SignificanceResult]) -> Optional[Dict[str, Any]]:
    if sig is None:
        return None
    return {
        "z_score": sig.z_score,
        "p_value": sig.p_value,
        "current_decided": sig.current_decided,
        "baseline_decided": sig.baseline_decided,
        "min_expected_count": sig.min_expected_count,
    }


def dict_to_significance(data: Optional[Mapping[str, Any]]) -> Optional[SignificanceResult]:
    if data is None:
        return None
    return SignificanceResult(
        z_score=data["z_score"],
        p_value=data["p_value"],
        current_decided=data["current_decided"],
        baseline_decided=data["baseline_decided"],
        min_expected_count=data["min_expected_count"],
    )


def risk_to_dict(risk: Optional[RevenueRisk]) -> Optional[Dict[str, Any]]:
    if risk is None:
        return None
    return {
        "failed_gmv_paise": risk.failed_gmv.minor_units,
        "excess_failed_transactions": risk.excess_failed_transactions,
        "mean_failed_ticket_paise": risk.mean_failed_ticket.minor_units if risk.mean_failed_ticket else None,
        "revenue_at_risk_paise": risk.revenue_at_risk.minor_units,
        "currency": risk.failed_gmv.currency.value,
    }


def dict_to_risk(data: Optional[Mapping[str, Any]]) -> Optional[RevenueRisk]:
    if data is None:
        return None
    curr = Currency(data.get("currency", Currency.INR.value))
    return RevenueRisk(
        failed_gmv=Money(data["failed_gmv_paise"], curr),
        excess_failed_transactions=data["excess_failed_transactions"],
        mean_failed_ticket=Money(data["mean_failed_ticket_paise"], curr) if data.get("mean_failed_ticket_paise") is not None else None,
        revenue_at_risk=Money(data["revenue_at_risk_paise"], curr),
    )


def metrics_to_json(metrics: FinancialMetrics) -> str:
    payload = {
        "window": {
            "start": metrics.window.start.isoformat(),
            "end": metrics.window.end.isoformat(),
        },
        "counts": counts_to_dict(metrics.counts),
        "failure_rate": rate_to_dict(metrics.failure_rate),
        "success_rate": rate_to_dict(metrics.success_rate),
        "baseline": baseline_to_dict(metrics.baseline),
        "deviation": deviation_to_dict(metrics.deviation),
        "significance": significance_to_dict(metrics.significance),
        "revenue_risk": risk_to_dict(metrics.revenue_risk),
        "computed_at": metrics.computed_at.isoformat(),
        "computation_version": metrics.computation_version,
    }
    return json.dumps(payload, sort_keys=True)


def json_to_metrics(raw: str) -> FinancialMetrics:
    data = json.loads(raw)
    w_start = datetime.fromisoformat(data["window"]["start"])
    w_end = datetime.fromisoformat(data["window"]["end"])
    window = TimeWindow(w_start, w_end)

    return FinancialMetrics(
        window=window,
        counts=dict_to_counts(data["counts"]),
        failure_rate=dict_to_rate(data.get("failure_rate")),
        success_rate=dict_to_rate(data.get("success_rate")),
        baseline=dict_to_baseline(data.get("baseline")),
        deviation=dict_to_deviation(data.get("deviation")),
        significance=dict_to_significance(data.get("significance")),
        revenue_risk=dict_to_risk(data.get("revenue_risk")),
        computed_at=datetime.fromisoformat(data["computed_at"]),
        computation_version=data.get("computation_version", "financial-engine-1"),
    )


# ---------------------------------------------------------------------------
# Dimension Breakdown serialization
# ---------------------------------------------------------------------------

def breakdown_to_dict(breakdown: DimensionBreakdown) -> Dict[str, Any]:
    return {
        "dimension": breakdown.dimension.value,
        "window": {
            "start": breakdown.window.start.isoformat(),
            "end": breakdown.window.end.isoformat(),
        },
        "total_counts": counts_to_dict(breakdown.total_counts),
        "slices": [
            {
                "dimension": s.dimension.value,
                "value": s.value,
                "counts": counts_to_dict(s.counts),
                "failed_gmv_paise": s.failed_gmv.minor_units,
                "source_confidence": s.source_confidence.value,
            }
            for s in breakdown.slices
        ],
    }


def dict_to_breakdown(data: Mapping[str, Any]) -> DimensionBreakdown:
    dim = Dimension(data["dimension"])
    w_start = datetime.fromisoformat(data["window"]["start"])
    w_end = datetime.fromisoformat(data["window"]["end"])
    window = TimeWindow(w_start, w_end)

    slices = [
        DimensionSlice(
            dimension=dim,
            value=s["value"],
            counts=dict_to_counts(s["counts"]),
            failed_gmv=Money(s["failed_gmv_paise"], Currency.INR),
            source_confidence=SourceConfidence(s["source_confidence"]),
        )
        for s in data["slices"]
    ]

    return DimensionBreakdown(
        dimension=dim,
        window=window,
        slices=tuple(slices),
        total_counts=dict_to_counts(data["total_counts"]),
    )


# ---------------------------------------------------------------------------
# Financial Evidence serialization
# ---------------------------------------------------------------------------

def evidence_to_dict(ev: FinancialEvidence) -> Dict[str, Any]:
    return {
        "evidence_id": ev.evidence_id,
        "incident_id": ev.incident_id,
        "summary": ev.summary,
        "window_start": ev.window.start.isoformat(),
        "window_end": ev.window.end.isoformat(),
        "computed_at": ev.computed_at.isoformat(),
        "source_confidence": ev.source_confidence.value,
        "dimension": ev.dimension.value if ev.dimension else None,
        "metrics_json": metrics_to_json(ev.metrics) if ev.metrics else None,
        "breakdown_json": json.dumps(breakdown_to_dict(ev.breakdown), sort_keys=True) if ev.breakdown else None,
    }


def dict_to_evidence(data: Mapping[str, Any]) -> FinancialEvidence:
    w_start = datetime.fromisoformat(data["window_start"])
    w_end = datetime.fromisoformat(data["window_end"])
    window = TimeWindow(w_start, w_end)

    metrics = json_to_metrics(data["metrics_json"]) if data.get("metrics_json") else None
    breakdown = dict_to_breakdown(json.loads(data["breakdown_json"])) if data.get("breakdown_json") else None

    return FinancialEvidence(
        evidence_id=data["evidence_id"],
        incident_id=data["incident_id"],
        summary=data["summary"],
        window=window,
        computed_at=datetime.fromisoformat(data["computed_at"]),
        source_confidence=SourceConfidence(data["source_confidence"]),
        metrics=metrics,
        breakdown=breakdown,
        dimension=Dimension(data["dimension"]) if data.get("dimension") else None,
    )


# ---------------------------------------------------------------------------
# Investigation Report serialization
# ---------------------------------------------------------------------------

def finding_to_dict(finding: DimensionalFinding) -> Dict[str, Any]:
    return {
        "dimension": finding.dimension.value,
        "value": finding.value,
        "strength": finding.strength.value,
        "counts": counts_to_dict(finding.counts),
        "failed_gmv_paise": finding.failed_gmv.minor_units,
        "source_confidence": finding.source_confidence.value,
        "summary": finding.summary,
        "failure_rate": rate_to_dict(finding.failure_rate),
        "baseline_failure_rate": rate_to_dict(finding.baseline_failure_rate),
        "deviation_pp": str(finding.deviation_pp) if finding.deviation_pp is not None else None,
        "relative_lift": str(finding.relative_lift) if finding.relative_lift is not None else None,
        "share_of_failures": str(finding.share_of_failures) if finding.share_of_failures is not None else None,
        "significance": significance_to_dict(finding.significance),
    }


def dict_to_finding(data: Mapping[str, Any]) -> DimensionalFinding:
    return DimensionalFinding(
        dimension=Dimension(data["dimension"]),
        value=data["value"],
        strength=EvidenceStrength(data["strength"]),
        counts=dict_to_counts(data["counts"]),
        failed_gmv=Money(data["failed_gmv_paise"], Currency.INR),
        source_confidence=SourceConfidence(data["source_confidence"]),
        summary=data["summary"],
        failure_rate=dict_to_rate(data.get("failure_rate")),
        baseline_failure_rate=dict_to_rate(data.get("baseline_failure_rate")),
        deviation_pp=Decimal(data["deviation_pp"]) if data.get("deviation_pp") is not None else None,
        relative_lift=Decimal(data["relative_lift"]) if data.get("relative_lift") is not None else None,
        share_of_failures=Decimal(data["share_of_failures"]) if data.get("share_of_failures") is not None else None,
        significance=dict_to_significance(data.get("significance")),
    )


def report_to_json(report: InvestigationReport) -> str:
    payload = {
        "incident_id": report.incident_id,
        "window": {
            "start": report.window.start.isoformat(),
            "end": report.window.end.isoformat(),
        },
        "investigated_at": report.investigated_at.isoformat(),
        "has_sufficient_evidence": report.has_sufficient_evidence,
        "has_multiple_concentrations": report.has_multiple_concentrations,
        "summary": report.summary,
        "primary_findings": [finding_to_dict(f) for f in report.primary_findings],
        "secondary_findings": [finding_to_dict(f) for f in report.secondary_findings],
        "breakdowns": {
            dim.value: breakdown_to_dict(bd) for dim, bd in report.breakdowns.items()
        },
        "evidence": [evidence_to_dict(ev) for ev in report.evidence],
    }
    return json.dumps(payload, sort_keys=True)


def json_to_report(raw: str) -> InvestigationReport:
    data = json.loads(raw)
    w_start = datetime.fromisoformat(data["window"]["start"])
    w_end = datetime.fromisoformat(data["window"]["end"])
    window = TimeWindow(w_start, w_end)

    breakdowns = {
        Dimension(dim_val): dict_to_breakdown(bd_data)
        for dim_val, bd_data in data["breakdowns"].items()
    }
    primary_findings = tuple(dict_to_finding(f) for f in data["primary_findings"])
    secondary_findings = tuple(dict_to_finding(f) for f in data["secondary_findings"])
    evidence = tuple(dict_to_evidence(ev) for ev in data["evidence"])

    return InvestigationReport(
        incident_id=data["incident_id"],
        window=window,
        investigated_at=datetime.fromisoformat(data["investigated_at"]),
        has_sufficient_evidence=bool(data["has_sufficient_evidence"]),
        primary_findings=primary_findings,
        secondary_findings=secondary_findings,
        breakdowns=breakdowns,
        evidence=evidence,
        summary=data["summary"],
        has_multiple_concentrations=bool(data.get("has_multiple_concentrations", False)),
    )
