"""Deterministic multi-dimensional failure investigation analyzer.

PROJECT_RULES 3.5 / ARCHITECTURE.md §8.

Slices an incident's payment population across all supported dimensions,
measures concentrations and deviations against baselines where available,
and produces structured findings with explicit evidence strengths.
"""

from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ..domain.canonical import short_digest
from ..domain.enums import Dimension, SourceConfidence
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialEvidence
from ..domain.metrics import (
    DimensionBreakdown,
    Rate,
    SignificanceResult,
    TransactionCounts,
)
from ..domain.window import TimeWindow, require_utc
from ..financial.breakdown import (
    ENRICHED_DIMENSIONS,
    FAILURE_ONLY_DIMENSIONS,
    _key_for,
    breakdown_by,
    share_of_failures,
)
from ..financial.deviation import compute_deviation
from ..financial.population import PaymentLike, as_payment, in_window
from ..financial.rates import failure_rate
from ..financial.significance import two_proportion_significance
from .enums import EvidenceStrength
from .findings import DimensionalFinding, InvestigationReport

INVESTIGATED_DIMENSIONS: Tuple[Dimension, ...] = (
    Dimension.PAYMENT_METHOD,
    Dimension.REGION,
    Dimension.PROVIDER,
    Dimension.FAILURE_CODE,
    Dimension.FAILURE_CATEGORY,
    Dimension.HOUR_OF_DAY,
)


def analyze_incident(
    incident_id: str,
    window: TimeWindow,
    current_payments: Sequence[PaymentLike],
    baseline_payments: Optional[Sequence[PaymentLike]] = None,
    investigated_at: Optional[datetime] = None,
    same_hour_baseline: bool = False,
) -> InvestigationReport:
    """Perform deterministic dimensional investigation over an incident window.

    Args:
        incident_id: Incident being investigated.
        window: The time window under investigation.
        current_payments: Payments occurring in the incident window.
        baseline_payments: Historical payments preceding the incident window.
        investigated_at: Investigation timestamp (aware UTC).
        same_hour_baseline: If True, filters historical baseline to matching hour of day.

    Returns:
        An ``InvestigationReport`` with structured evidence and candidate contributors.
    """
    if not isinstance(incident_id, str) or not incident_id.strip():
        raise DomainValidationError("incident_id must be a non-empty string")
    if not isinstance(window, TimeWindow):
        raise DomainValidationError("window must be a TimeWindow")

    when = require_utc(investigated_at) if investigated_at is not None else datetime.now().astimezone()

    window_items = tuple(in_window(current_payments, window))
    raw_history = tuple(baseline_payments) if baseline_payments is not None else ()

    if same_hour_baseline and raw_history:
        history_items = tuple(
            p for p in raw_history if as_payment(p).created_at.hour == window.start.hour
        )
    else:
        history_items = raw_history

    total_failures = sum(1 for p in window_items if as_payment(p).is_failure)
    total_decided = sum(1 for p in window_items if as_payment(p).is_decided)

    # Check sufficient data
    if total_decided < 10 or (not history_items and total_decided < 30):
        breakdowns = {
            dim: breakdown_by(window_items, dim, window)
            for dim in (Dimension.PAYMENT_METHOD, Dimension.FAILURE_CODE)
        }
        return InvestigationReport(
            incident_id=incident_id,
            window=window,
            investigated_at=when,
            has_sufficient_evidence=False,
            primary_findings=(),
            secondary_findings=(),
            breakdowns=breakdowns,
            evidence=(),
            summary=(
                f"Insufficient data in window ({total_decided} decided transactions, "
                f"{total_failures} failures). Baselines cannot be reliably established."
            ),
            has_multiple_concentrations=False,
        )

    all_breakdowns: Dict[Dimension, DimensionBreakdown] = {}
    primary_findings: List[DimensionalFinding] = []
    secondary_findings: List[DimensionalFinding] = []
    evidence_list: List[FinancialEvidence] = []

    # Map baseline slices and history metrics if history is provided (single O(N) pass)
    baseline_slice_counts: Dict[Tuple[Dimension, str], Rate] = {}
    hist_failed = 0
    hist_decided = 0

    if history_items:
        pop_dims = tuple(dim for dim in INVESTIGATED_DIMENSIONS if dim not in FAILURE_ONLY_DIMENSIONS)
        # Accumulator: (dim, val) -> [succeeded, failed, undecided]
        slice_accum: Dict[Tuple[Dimension, str], List[int]] = {}

        for item in history_items:
            p = as_payment(item)
            if p.is_failure:
                hist_failed += 1
            if p.is_decided:
                hist_decided += 1

            for dim in pop_dims:
                val = _key_for(item, dim)
                if val is not None:
                    k = (dim, val)
                    if k not in slice_accum:
                        slice_accum[k] = [0, 0, 0]
                    if p.is_success:
                        slice_accum[k][0] += 1
                    elif p.is_failure:
                        slice_accum[k][1] += 1
                    else:
                        slice_accum[k][2] += 1

        for (dim, val), (succ, fail, und) in slice_accum.items():
            cnt = TransactionCounts(succeeded=succ, failed=fail, undecided=und)
            if cnt.decided >= 10:
                r = failure_rate(cnt)
                if r is not None:
                    baseline_slice_counts[(dim, val)] = r

    # Determine if the overall window has an elevated failure count/rate above baseline
    overall_failure_rate = (
        Decimal(total_failures) / Decimal(total_decided) if total_decided > 0 else Decimal(0)
    )
    overall_is_elevated = False
    if history_items:
        if hist_decided > 0:
            base_overall = Decimal(hist_failed) / Decimal(hist_decided)
            overall_dev_pp = (overall_failure_rate - base_overall) * Decimal(100)
            overall_lift = (
                overall_failure_rate / base_overall if base_overall > 0 else Decimal(1)
            )
            overall_is_elevated = (
                overall_dev_pp >= Decimal("3.0") and overall_lift >= Decimal("1.3")
            )
    else:
        overall_is_elevated = (
            overall_failure_rate >= Decimal("0.10") and total_failures >= 15
        )

    for dim in INVESTIGATED_DIMENSIONS:
        breakdown = breakdown_by(window_items, dim, window)
        all_breakdowns[dim] = breakdown

        ev_id = "ev_" + short_digest(
            {
                "incident_id": incident_id,
                "dimension": dim.value,
                "window": window.label(),
            }
        )
        conf = (
            SourceConfidence.ENRICHED
            if dim in ENRICHED_DIMENSIONS
            else SourceConfidence.OBSERVED
        )

        dim_summary = (
            f"Breakdown along {dim.value} across {breakdown.total_counts.total} transactions "
            f"({breakdown.total_counts.failed} failures)."
        )

        evidence_list.append(
            FinancialEvidence(
                evidence_id=ev_id,
                incident_id=incident_id,
                summary=dim_summary,
                window=window,
                computed_at=when,
                source_confidence=conf,
                breakdown=breakdown,
                dimension=dim,
            )
        )

        # Skip hour_of_day as a causal concentration if window is a single hour
        is_single_hour_window = (window.duration_seconds <= 3600)
        if dim is Dimension.HOUR_OF_DAY and is_single_hour_window:
            continue

        for s in breakdown.slices:
            if s.counts.failed == 0 and s.counts.decided > 0:
                continue

            share = share_of_failures(breakdown, s.value) or Decimal(0)
            curr_rate = failure_rate(s.counts)
            base_rate = baseline_slice_counts.get((dim, s.value))

            dev_pp = None
            lift = None
            sig: Optional[SignificanceResult] = None

            if curr_rate is not None and base_rate is not None:
                dev = compute_deviation(curr_rate, base_rate)
                dev_pp = dev.absolute_percentage_points
                lift = dev.relative_lift
                sig = two_proportion_significance(curr_rate, base_rate)

            strength = EvidenceStrength.OBSERVED_FACT

            if dim in FAILURE_ONLY_DIMENSIONS:
                # Failure codes/categories are only candidate causes if there is a true failure spike
                if overall_is_elevated and total_failures >= 15 and share >= Decimal("0.40"):
                    strength = EvidenceStrength.STRONG_EVIDENCE
                elif overall_is_elevated and total_failures >= 8 and share >= Decimal("0.20"):
                    strength = EvidenceStrength.POSSIBLE_CONTRIBUTOR
            else:
                # Population dimensions (method, region, provider)
                if s.counts.decided < 5:
                    strength = EvidenceStrength.INSUFFICIENT_EVIDENCE
                elif (
                    dev_pp is not None
                    and lift is not None
                    and dev_pp >= Decimal("5.0")
                    and lift >= Decimal("1.8")
                    and s.counts.failed >= 5
                    and (share >= Decimal("0.20") or s.counts.failed >= 10)
                ):
                    strength = EvidenceStrength.STRONG_EVIDENCE
                elif (
                    dev_pp is not None
                    and dev_pp >= Decimal("2.0")
                    and lift is not None
                    and lift >= Decimal("1.3")
                    and s.counts.failed >= 3
                ):
                    strength = EvidenceStrength.POSSIBLE_CONTRIBUTOR
                elif not history_items and share >= Decimal("0.50") and s.counts.failed >= 10:
                    strength = EvidenceStrength.POSSIBLE_CONTRIBUTOR

            finding_summary = (
                f"{dim.value.replace('_', ' ').capitalize()} '{s.value}' accounts for "
                f"{s.counts.failed}/{total_failures} failures ({(share * 100):.1f}% share, "
                f"failed GMV: {s.failed_gmv})."
            )
            if curr_rate is not None:
                finding_summary += f" Observed failure rate: {curr_rate.as_percent()}%."
            if dev_pp is not None and lift is not None:
                finding_summary += (
                    f" Baseline: {base_rate.as_percent()}%, deviation: +{dev_pp}pp, lift: {lift}x."
                )

            finding = DimensionalFinding(
                dimension=dim,
                value=s.value,
                strength=strength,
                counts=s.counts,
                failed_gmv=s.failed_gmv,
                source_confidence=s.source_confidence,
                summary=finding_summary,
                failure_rate=curr_rate,
                baseline_failure_rate=base_rate,
                deviation_pp=dev_pp,
                relative_lift=lift,
                share_of_failures=share,
                significance=sig,
            )

            if strength == EvidenceStrength.STRONG_EVIDENCE:
                primary_findings.append(finding)
            elif strength == EvidenceStrength.POSSIBLE_CONTRIBUTOR:
                secondary_findings.append(finding)

    primary_findings.sort(
        key=lambda f: (
            -(f.share_of_failures or Decimal(0)),
            -(f.deviation_pp or Decimal(0)),
            f.value,
        )
    )
    secondary_findings.sort(
        key=lambda f: (
            -(f.share_of_failures or Decimal(0)),
            -(f.deviation_pp or Decimal(0)),
            f.value,
        )
    )

    strong_dims = {f.dimension for f in primary_findings if f.dimension not in FAILURE_ONLY_DIMENSIONS}
    has_multiple = len(strong_dims) > 1

    if not primary_findings and not secondary_findings:
        summary = (
            f"Investigation across {len(INVESTIGATED_DIMENSIONS)} dimensions observed "
            f"{total_failures} failures across {total_decided} decided transactions. "
            f"No concentrated degradation detected above baseline thresholds."
        )
    elif has_multiple:
        dim_descs = [
            f"{f.dimension.value} '{f.value}' ({(f.share_of_failures or Decimal(0)) * 100:.1f}% failures)"
            for f in primary_findings if f.dimension not in FAILURE_ONLY_DIMENSIONS
        ]
        summary = (
            f"Multiple independent concentrations identified: {', '.join(dim_descs)}. "
            f"Investigation indicates co-occurring degradations rather than a single isolated cause."
        )
    else:
        top = primary_findings[0] if primary_findings else secondary_findings[0]
        summary = (
            f"Failures are heavily concentrated in {top.dimension.value} '{top.value}' "
            f"({(top.share_of_failures or Decimal(0)) * 100:.1f}% of total failures, "
            f"failed GMV: {top.failed_gmv})."
        )

    return InvestigationReport(
        incident_id=incident_id,
        window=window,
        investigated_at=when,
        has_sufficient_evidence=True,
        primary_findings=tuple(primary_findings),
        secondary_findings=tuple(secondary_findings),
        breakdowns=all_breakdowns,
        evidence=tuple(evidence_list),
        summary=summary,
        has_multiple_concentrations=has_multiple,
    )
