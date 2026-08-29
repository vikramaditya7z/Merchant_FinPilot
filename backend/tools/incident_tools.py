"""Read-only deterministic tools for LLM Agent consumption.

PROJECT_RULES 1.6, 4.2, 10.8 / ARCHITECTURE.md §9.

Security & Integrity Guarantees:
- Every tool is strictly READ-ONLY.
- Arguments are explicitly validated before execution.
- Calculations call the deterministic financial/investigation engines, never re-implemented.
- All monetary amounts are integers in minor units (paise).
- No arbitrary SQL, no arbitrary Python, no raw DB internals exposed.
"""

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..db.database import Database
from ..domain.enums import Dimension, FailureCategory, IncidentStatus
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialIncident
from ..domain.window import TimeWindow, require_utc
from ..financial.counts import count_transactions
from ..financial.population import as_payment
from ..financial.rates import failure_rate
from ..investigation.enums import EvidenceStrength
from ..investigation.findings import InvestigationReport
from .contracts import SliceSummary, TimeBucketSummary, ToolErrorCode, ToolResult

VALID_ACTION_TYPES = frozenset(
    {"ROUTE_UPDATE", "CIRCUIT_BREAKER", "RETRY_ROUTING", "MERCHANT_NOTIFICATION"}
)


def get_incident_summary(db: Database, incident_id: str) -> ToolResult:
    """Retrieve structured information and core metrics for a FinancialIncident.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.

    Returns:
        A ToolResult containing structured incident facts and metrics.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    metrics = incident.metrics
    dev = metrics.deviation
    risk = metrics.revenue_risk
    base = metrics.baseline

    data = {
        "incident_id": incident.incident_id,
        "incident_key": incident.incident_key,
        "merchant_id": incident.merchant_id,
        "incident_type": incident.incident_type.value,
        "status": incident.status.value,
        "severity": incident.severity.value,
        "detected_at": incident.detected_at.isoformat(),
        "window": {
            "start": incident.window.start.isoformat(),
            "end": incident.window.end.isoformat(),
            "duration_seconds": incident.window.duration_seconds,
        },
        "traffic": {
            "total_transactions": metrics.counts.total,
            "succeeded": metrics.counts.succeeded,
            "failed": metrics.counts.failed,
            "undecided": metrics.counts.undecided,
        },
        "failure_rate": {
            "percent": metrics.failure_rate.as_percent() if metrics.failure_rate else None,
            "numerator": metrics.failure_rate.numerator if metrics.failure_rate else 0,
            "denominator": metrics.failure_rate.denominator if metrics.failure_rate else 0,
        },
        "baseline": {
            "method": base.method.value if base else None,
            "failure_rate_percent": base.rate.as_percent() if (base and base.rate) else None,
            "decided_sample": base.decided_sample if base else 0,
        },
        "deviation": {
            "absolute_percentage_points": str(dev.absolute_percentage_points) if dev else None,
            "relative_lift": str(dev.relative_lift) if (dev and dev.relative_lift is not None) else None,
        },
        "revenue_risk": {
            "failed_gmv_paise": risk.failed_gmv.minor_units if risk else 0,
            "excess_failed_transactions": risk.excess_failed_transactions if risk else 0,
            "mean_failed_ticket_paise": (
                risk.mean_failed_ticket.minor_units
                if (risk and risk.mean_failed_ticket)
                else None
            ),
            "revenue_at_risk_paise": risk.revenue_at_risk.minor_units if risk else 0,
            "currency": risk.failed_gmv.currency.value if risk else "INR",
        },
        "primary_dimension": incident.primary_dimension.value if incident.primary_dimension else None,
        "primary_dimension_value": incident.primary_dimension_value,
        "evidence_id": incident.evidence[0].evidence_id if incident.evidence else None,
        "attached_evidence_ids": [ev.evidence_id for ev in incident.evidence],
    }

    return ToolResult.ok(data)


def get_failure_breakdown(
    db: Database, incident_id: str, dimension: str
) -> ToolResult:
    """Retrieve deterministic failure breakdown along a specific dimension.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.
        dimension: One of the supported dimension strings.

    Returns:
        A ToolResult containing structured slices ordered by failure count.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )
    if not isinstance(dimension, str) or not dimension.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "dimension must be a non-empty string"
        )

    try:
        dim_enum = Dimension(dimension.strip().lower())
    except ValueError:
        valid_dims = ", ".join(d.value for d in Dimension)
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT,
            f"Unsupported dimension '{dimension}'. Supported dimensions: {valid_dims}",
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    investigation = db.get_investigation(incident_id.strip())

    slices_data: List[Dict[str, Any]] = []

    if investigation is not None and dim_enum in investigation.breakdowns:
        bd = investigation.breakdowns[dim_enum]
        findings_map = {
            f.value: f
            for f in (investigation.primary_findings + investigation.secondary_findings)
            if f.dimension == dim_enum
        }

        total_failures = bd.total_counts.failed

        for s in bd.slices:
            finding = findings_map.get(s.value)
            rate = failure_rate(s.counts)
            share = (
                Decimal(s.counts.failed) / Decimal(total_failures)
                if total_failures > 0
                else Decimal(0)
            )

            slices_data.append(
                {
                    "value": s.value,
                    "total_count": s.counts.total,
                    "succeeded_count": s.counts.succeeded,
                    "failed_count": s.counts.failed,
                    "failed_gmv_paise": s.failed_gmv.minor_units,
                    "source_confidence": s.source_confidence.value,
                    "share_of_failures": str(share),
                    "failure_rate_percent": rate.as_percent() if rate else None,
                    "baseline_rate_percent": (
                        finding.baseline_failure_rate.as_percent()
                        if (finding and finding.baseline_failure_rate)
                        else None
                    ),
                    "deviation_percentage_points": (
                        str(finding.deviation_pp)
                        if (finding and finding.deviation_pp is not None)
                        else None
                    ),
                    "relative_lift": (
                        str(finding.relative_lift)
                        if (finding and finding.relative_lift is not None)
                        else None
                    ),
                    "evidence_strength": (
                        finding.strength.value
                        if finding
                        else EvidenceStrength.OBSERVED_FACT.value
                    ),
                }
            )
    else:
        # If no stored investigation report, query evidence attached to incident
        matching_ev = [ev for ev in incident.evidence if ev.dimension == dim_enum and ev.breakdown is not None]
        if matching_ev:
            bd = matching_ev[0].breakdown
            total_failures = bd.total_counts.failed
            for s in bd.slices:
                rate = failure_rate(s.counts)
                share = (
                    Decimal(s.counts.failed) / Decimal(total_failures)
                    if total_failures > 0
                    else Decimal(0)
                )
                slices_data.append(
                    {
                        "value": s.value,
                        "total_count": s.counts.total,
                        "succeeded_count": s.counts.succeeded,
                        "failed_count": s.counts.failed,
                        "failed_gmv_paise": s.failed_gmv.minor_units,
                        "source_confidence": s.source_confidence.value,
                        "share_of_failures": str(share),
                        "failure_rate_percent": rate.as_percent() if rate else None,
                        "baseline_rate_percent": None,
                        "deviation_percentage_points": None,
                        "relative_lift": None,
                        "evidence_strength": EvidenceStrength.OBSERVED_FACT.value,
                    }
                )
        else:
            return ToolResult.error(
                ToolErrorCode.UNAVAILABLE,
                f"No breakdown data available for dimension '{dimension}' in incident '{incident_id}'",
            )

    dim_evidence_id: Optional[str] = None
    if investigation is not None and investigation.evidence:
        for ev in investigation.evidence:
            if ev.dimension == dim_enum:
                dim_evidence_id = ev.evidence_id
                break
    if dim_evidence_id is None and incident.evidence:
        dim_evidence_id = incident.evidence[0].evidence_id

    return ToolResult.ok(
        {
            "incident_id": incident.incident_id,
            "dimension": dim_enum.value,
            "evidence_id": dim_evidence_id,
            "total_slices": len(slices_data),
            "slices": slices_data,
        }
    )


def get_time_series(
    db: Database, incident_id: str, granularity_minutes: int = 15
) -> ToolResult:
    """Retrieve time-series bucket metrics across the incident window.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.
        granularity_minutes: Bucket duration in minutes (between 5 and 60).

    Returns:
        A ToolResult containing ordered time bucket summaries.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )
    if not isinstance(granularity_minutes, int) or not (5 <= granularity_minutes <= 60):
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT,
            "granularity_minutes must be an integer between 5 and 60",
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    payments = db.list_payments(window=incident.window)

    # Divide window into time buckets
    buckets: List[Dict[str, Any]] = []
    bucket_step = timedelta(minutes=granularity_minutes)
    curr_start = incident.window.start

    while curr_start < incident.window.end:
        curr_end = min(curr_start + bucket_step, incident.window.end)
        b_window = TimeWindow(curr_start, curr_end)

        b_items = [p for p in payments if b_window.contains(p.payment.created_at)]
        counts = count_transactions(b_items)
        rate = failure_rate(counts)

        failed_gmv_paise = sum(
            p.payment.amount.minor_units
            for p in b_items
            if p.payment.is_failure
        )

        buckets.append(
            {
                "start": curr_start.isoformat(),
                "end": curr_end.isoformat(),
                "duration_minutes": int((curr_end - curr_start).total_seconds() // 60),
                "total_transactions": counts.total,
                "succeeded": counts.succeeded,
                "failed": counts.failed,
                "undecided": counts.undecided,
                "failure_rate_percent": rate.as_percent() if rate else None,
                "failed_gmv_paise": failed_gmv_paise,
            }
        )
        curr_start = curr_end

    return ToolResult.ok(
        {
            "incident_id": incident.incident_id,
            "window": {
                "start": incident.window.start.isoformat(),
                "end": incident.window.end.isoformat(),
            },
            "granularity_minutes": granularity_minutes,
            "bucket_count": len(buckets),
            "buckets": buckets,
        }
    )


def get_baseline_comparison(
    db: Database,
    incident_id: str,
    dimension: Optional[str] = None,
    dimension_value: Optional[str] = None,
) -> ToolResult:
    """Retrieve deterministic baseline comparison for the incident or a slice.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.
        dimension: Optional dimension name to inspect a specific slice.
        dimension_value: Optional slice value to inspect.

    Returns:
        A ToolResult with baseline rates, deviations, and statistical significance.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    # Slice-level comparison if dimension is specified
    if dimension is not None:
        try:
            dim_enum = Dimension(dimension.strip().lower())
        except ValueError:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENT, f"Unsupported dimension '{dimension}'"
            )

        if dimension_value is None or not dimension_value.strip():
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENT,
                "dimension_value must be provided when dimension is specified",
            )

        val = dimension_value.strip()
        investigation = db.get_investigation(incident_id.strip())
        if investigation is None:
            return ToolResult.error(
                ToolErrorCode.UNAVAILABLE,
                f"No investigation baseline data available for incident '{incident_id}'",
            )

        all_findings = investigation.primary_findings + investigation.secondary_findings
        matching = [f for f in all_findings if f.dimension == dim_enum and f.value == val]

        if not matching:
            # Check if it exists in the breakdown as an observed slice
            bd = investigation.breakdowns.get(dim_enum)
            matching_slice = next((s for s in bd.slices if s.value == val), None) if bd else None
            if matching_slice is None:
                return ToolResult.error(
                    ToolErrorCode.NOT_FOUND,
                    f"Slice '{val}' not found in dimension '{dimension}'",
                )
            r = failure_rate(matching_slice.counts)
            return ToolResult.ok(
                {
                    "incident_id": incident.incident_id,
                    "dimension": dim_enum.value,
                    "value": val,
                    "current_failure_rate_percent": r.as_percent() if r else None,
                    "baseline_failure_rate_percent": None,
                    "deviation_percentage_points": None,
                    "relative_lift": None,
                    "evidence_strength": EvidenceStrength.OBSERVED_FACT.value,
                    "note": "Slice observed within normal background variation; no baseline deviation flagged.",
                }
            )

        f = matching[0]
        sig = f.significance
        return ToolResult.ok(
            {
                "incident_id": incident.incident_id,
                "dimension": dim_enum.value,
                "value": val,
                "evidence_id": incident.evidence[0].evidence_id if incident.evidence else None,
                "current_failure_rate_percent": f.failure_rate.as_percent() if f.failure_rate else None,
                "baseline_failure_rate_percent": f.baseline_failure_rate.as_percent() if f.baseline_failure_rate else None,
                "deviation_percentage_points": str(f.deviation_pp) if f.deviation_pp is not None else None,
                "relative_lift": str(f.relative_lift) if f.relative_lift is not None else None,
                "share_of_failures": str(f.share_of_failures) if f.share_of_failures is not None else None,
                "evidence_strength": f.strength.value,
                "significance": {
                    "z_score": sig.z_score if sig else None,
                    "p_value": sig.p_value if sig else None,
                    "normal_approximation_valid": sig.normal_approximation_valid if sig else None,
                } if sig else None,
            }
        )

    # Overall incident baseline comparison
    metrics = incident.metrics
    base = metrics.baseline
    dev = metrics.deviation
    sig = metrics.significance

    return ToolResult.ok(
        {
            "incident_id": incident.incident_id,
            "evidence_id": incident.evidence[0].evidence_id if incident.evidence else None,
            "window": {
                "start": incident.window.start.isoformat(),
                "end": incident.window.end.isoformat(),
            },
            "baseline_method": base.method.value if base else None,
            "baseline_failure_rate_percent": base.rate.as_percent() if (base and base.rate) else None,
            "baseline_decided_sample": base.decided_sample if base else 0,
            "current_failure_rate_percent": metrics.failure_rate.as_percent() if metrics.failure_rate else None,
            "current_decided_sample": metrics.counts.decided,
            "deviation_percentage_points": str(dev.absolute_percentage_points) if dev else None,
            "relative_lift": str(dev.relative_lift) if (dev and dev.relative_lift is not None) else None,
            "significance": {
                "z_score": sig.z_score if sig else None,
                "p_value": sig.p_value if sig else None,
                "normal_approximation_valid": sig.normal_approximation_valid if sig else None,
                "min_expected_count": sig.min_expected_count if sig else None,
            } if sig else None,
        }
    )


def get_revenue_exposure(db: Database, incident_id: str) -> ToolResult:
    """Retrieve deterministic financial revenue exposure for an incident.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.

    Returns:
        A ToolResult containing exact failed GMV and revenue-at-risk amounts.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    risk = incident.metrics.revenue_risk
    if risk is None:
        return ToolResult.error(
            ToolErrorCode.UNAVAILABLE,
            f"Revenue risk metrics not computed for incident '{incident_id}'",
        )

    # Check if investigation report indicates recoverable failure categories
    investigation = db.get_investigation(incident_id.strip())
    is_recoverable = True
    recoverability_notes = "Failures stem from technical/provider/network errors and may be recoverable."

    if investigation is not None:
        # If the primary cause is risk engine block, it is NOT recoverable
        for f in investigation.primary_findings:
            if f.dimension is Dimension.FAILURE_CATEGORY and f.value == FailureCategory.RISK_BLOCKED.value:
                is_recoverable = False
                recoverability_notes = "Failures are flagged as risk-blocked/fraud by risk engine; non-recoverable."
                break

    return ToolResult.ok(
        {
            "incident_id": incident.incident_id,
            "evidence_id": incident.evidence[0].evidence_id if incident.evidence else None,
            "failed_gmv_paise": risk.failed_gmv.minor_units,
            "currency": risk.failed_gmv.currency.value,
            "excess_failed_transactions": risk.excess_failed_transactions,
            "mean_failed_ticket_paise": (
                risk.mean_failed_ticket.minor_units
                if risk.mean_failed_ticket
                else None
            ),
            "revenue_at_risk_paise": risk.revenue_at_risk.minor_units,
            "is_recoverable": is_recoverable,
            "recoverability_notes": recoverability_notes,
        }
    )


def check_action_eligibility(
    db: Database,
    incident_id: str,
    action_type: str,
    target_dimension: Optional[str] = None,
    target_value: Optional[str] = None,
) -> ToolResult:
    """Deterministically check whether a hypothetical action is eligible for consideration.

    Note: This is a read-only pre-flight check. It does NOT authorize or execute any action.

    Args:
        db: The persistent Database repository.
        incident_id: The unique incident identifier.
        action_type: One of ROUTE_UPDATE, CIRCUIT_BREAKER, RETRY_ROUTING, MERCHANT_NOTIFICATION.
        target_dimension: Optional target dimension (e.g. 'provider', 'payment_method').
        target_value: Optional target value (e.g. 'acquirer_b', 'upi').

    Returns:
        A ToolResult indicating eligibility, constraints, and deterministic rationale.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "incident_id must be a non-empty string"
        )
    if not isinstance(action_type, str) or not action_type.strip():
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT, "action_type must be a non-empty string"
        )

    norm_action = action_type.strip().upper()
    if norm_action not in VALID_ACTION_TYPES:
        valid_actions = ", ".join(sorted(VALID_ACTION_TYPES))
        return ToolResult.error(
            ToolErrorCode.INVALID_ARGUMENT,
            f"Unsupported action_type '{action_type}'. Supported actions: {valid_actions}",
        )

    incident = db.get_incident(incident_id.strip())
    if incident is None:
        return ToolResult.error(
            ToolErrorCode.NOT_FOUND, f"Incident '{incident_id}' not found"
        )

    # Check incident status
    if incident.status in (IncidentStatus.RESOLVED, IncidentStatus.DISMISSED):
        return ToolResult.ok(
            {
                "incident_id": incident.incident_id,
                "action_type": norm_action,
                "eligible": False,
                "reason": f"Incident is currently {incident.status.value}; no remediation actions are eligible.",
                "constraints": ["Incident must be active (DETECTED, INVESTIGATING, CONFIRMED)."],
            }
        )

    investigation = db.get_investigation(incident_id.strip())
    if investigation is None or not investigation.has_sufficient_evidence:
        return ToolResult.ok(
            {
                "incident_id": incident.incident_id,
                "action_type": norm_action,
                "eligible": False,
                "reason": "Insufficient statistical evidence established to justify remediation.",
                "constraints": ["Must have sufficient sample volume and verified evidence."],
            }
        )

    # Check if incident is driven by risk blocks (fraud policy)
    is_risk_blocked = any(
        f.dimension is Dimension.FAILURE_CATEGORY and f.value == FailureCategory.RISK_BLOCKED.value
        for f in investigation.primary_findings
    )

    if is_risk_blocked and norm_action in ("ROUTE_UPDATE", "RETRY_ROUTING"):
        return ToolResult.ok(
            {
                "incident_id": incident.incident_id,
                "action_type": norm_action,
                "eligible": False,
                "reason": (
                    "Ineligible: Failures are blocked by Razorpay risk engine / fraud rules. "
                    "Routing modifications cannot bypass compliance/risk security controls."
                ),
                "constraints": ["Risk-blocked transactions cannot be rerouted."],
            }
        )

    # Dimensional verification for routing/circuit breaker
    target_dim_enum: Optional[Dimension] = None
    if target_dimension is not None and target_dimension.strip():
        try:
            target_dim_enum = Dimension(target_dimension.strip().lower())
        except ValueError:
            return ToolResult.error(
                ToolErrorCode.INVALID_ARGUMENT,
                f"Unsupported target_dimension '{target_dimension}'",
            )

    if norm_action in ("ROUTE_UPDATE", "CIRCUIT_BREAKER"):
        if target_dim_enum is None or target_value is None or not target_value.strip():
            return ToolResult.ok(
                {
                    "incident_id": incident.incident_id,
                    "action_type": norm_action,
                    "eligible": False,
                    "reason": f"{norm_action} requires both target_dimension and target_value.",
                    "constraints": ["target_dimension and target_value must be specified."],
                }
            )

        # Look up target slice in investigation primary findings
        norm_val = target_value.strip()
        matching_strong = [
            f for f in investigation.primary_findings
            if f.dimension == target_dim_enum and f.value == norm_val
        ]
        matching_secondary = [
            f for f in investigation.secondary_findings
            if f.dimension == target_dim_enum and f.value == norm_val
        ]

        if not matching_strong and not matching_secondary:
            return ToolResult.ok(
                {
                    "incident_id": incident.incident_id,
                    "action_type": norm_action,
                    "target_dimension": target_dim_enum.value,
                    "target_value": norm_val,
                    "eligible": False,
                    "reason": (
                        f"Target slice '{target_dim_enum.value}:{norm_val}' does not show "
                        f"concentrated degradation in the investigation findings."
                    ),
                    "evidence_strength": EvidenceStrength.OBSERVED_FACT.value,
                    "constraints": ["Action target must correspond to an identified anomalous slice."],
                }
            )

        finding = matching_strong[0] if matching_strong else matching_secondary[0]
        failed_gmv = finding.failed_gmv.minor_units

        return ToolResult.ok(
            {
                "incident_id": incident.incident_id,
                "action_type": norm_action,
                "target_dimension": target_dim_enum.value,
                "target_value": norm_val,
                "eligible": True,
                "evidence_strength": finding.strength.value,
                "estimated_risk_mitigation_paise": failed_gmv,
                "reason": (
                    f"Eligible for consideration: {target_dim_enum.value} '{norm_val}' is a verified "
                    f"concentration ({finding.counts.failed} failures, failed GMV: {failed_gmv} paise)."
                ),
                "constraints": [
                    "Requires Policy Engine verification prior to execution.",
                    "Test mode execution guard enforced.",
                ],
            }
        )

    # Default eligibility for general actions (e.g. MERCHANT_NOTIFICATION, RETRY_ROUTING)
    risk_paise = incident.metrics.revenue_risk.revenue_at_risk.minor_units if incident.metrics.revenue_risk else 0
    return ToolResult.ok(
        {
            "incident_id": incident.incident_id,
            "action_type": norm_action,
            "eligible": True,
            "evidence_strength": EvidenceStrength.STRONG_EVIDENCE.value,
            "estimated_risk_mitigation_paise": risk_paise,
            "reason": f"Eligible for consideration based on active {incident.severity.value} incident.",
            "constraints": [
                "Requires Policy Engine verification prior to execution.",
            ],
        }
    )
