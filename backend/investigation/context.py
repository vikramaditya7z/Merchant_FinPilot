"""Incident Context Assembler for live payment failures.

PROJECT_RULES 1.4, 3.5, 4.1, 4.2 / ARCHITECTURE.md §8, §12.

Assembles observed payment facts, historical context, and baseline measurements
into a validated FinancialIncident with attached FinancialEvidence.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..db.database import Database
from ..domain.canonical import short_digest
from ..domain.enums import (
    IncidentStatus,
    IncidentType,
    PaymentMethod,
    PaymentStatus,
    Severity,
    SourceConfidence,
)
from ..domain.incident import FinancialEvidence, FinancialIncident
from ..domain.metrics import FinancialMetrics
from ..domain.payment import EnrichedPayment, Payment
from ..domain.window import UTC, TimeWindow, require_utc
from ..financial.engine import build_hourly_baseline, compute_metrics
from .classifier import ScenarioClassification, ScenarioClassifier


@dataclass(frozen=True)
class FailureIncidentContext:
    """Complete assembled context for an investigated payment failure."""

    incident: Optional[FinancialIncident]
    triggering_payment: Payment
    classification: ScenarioClassification
    recent_payments: Tuple[EnrichedPayment, ...]
    baseline_payments: Tuple[EnrichedPayment, ...]
    metrics: FinancialMetrics


class ContextAssembler:
    """Assembles rich incident context from persisted database facts."""

    def __init__(
        self,
        database: Database,
        classifier: Optional[ScenarioClassifier] = None,
    ) -> None:
        self._db = database
        self._classifier = classifier or ScenarioClassifier()

    def assemble(
        self,
        payment: Payment,
        merchant_id: str = "merchant_default",
        now: Optional[datetime] = None,
        lookback_hours: int = 1,
    ) -> FailureIncidentContext:
        """Assemble incident context for a failed payment.

        Args:
            payment: The failed Payment entity that triggered the event.
            merchant_id: Identifier of the merchant.
            now: Injected current timestamp (aware UTC).
            lookback_hours: Window duration for recent transaction context.

        Returns:
            FailureIncidentContext with FinancialIncident, evidence, and classification.
        """
        when = require_utc(now) if now is not None else payment.created_at
        if now is None and self._db is not None:
            latest_db_ts = self._db.get_latest_payment_timestamp()
            if latest_db_ts is not None and latest_db_ts > when:
                when = latest_db_ts

        # 1. Define active window (including payment timestamp)
        start_time = when - timedelta(hours=lookback_hours)
        end_time = when + timedelta(seconds=1)
        current_window = TimeWindow(start_time, end_time)

        # 2. Define historical baseline window (past 7 days preceding active window)
        baseline_start = start_time - timedelta(days=7)
        baseline_window = TimeWindow(baseline_start, start_time)

        # 3. Retrieve historical payments from DB
        recent_raw = list(self._db.list_payments_in_window(current_window))
        baseline_raw = list(self._db.list_payments_in_window(baseline_window))

        # Ensure the triggering payment is in the active list
        if not any(p.payment.id == payment.id for p in recent_raw):
            enr_p = self._db.get_enriched_payment(payment.id)
            if enr_p is not None:
                recent_raw.append(enr_p)
            else:
                recent_raw.append(EnrichedPayment(payment=payment))

        recent_payments = tuple(recent_raw)
        baseline_payments = tuple(baseline_raw)

        # 4. Compute deterministic financial metrics for the window
        baseline_buckets = (
            build_hourly_baseline(
                baseline_payments,
                current_window,
                lookback_windows=7 * 24,
            )
            if baseline_payments
            else None
        )
        metrics = compute_metrics(
            items=recent_payments,
            window=current_window,
            now=when,
            baseline_windows=baseline_buckets,
        )

        # 5. Classify scenario
        enr = self._db.get_enriched_payment(payment.id)
        classification = self._classifier.classify(
            payment=payment,
            enrichment=enr.enrichment if enr else None,
            recent_payments=[p.payment for p in recent_payments],
            metrics=metrics,
        )

        # 6. Construct FinancialIncident and attached Evidence only if is_incident is True
        incident = None
        if classification.is_incident:
            inc_id = f"inc_live_{short_digest({'pay': payment.id, 'm': merchant_id, 'when': when.isoformat()})}"
            inc_key = f"key_{short_digest({'incident_id': inc_id, 'rule': classification.scenario_id.value})}"

            # Severity determination
            if payment.amount.minor_units >= 100_000 or classification.scenario_id.value in ("upi_failure_spike", "card_failure_spike"):
                severity = Severity.HIGH
            else:
                severity = Severity.MEDIUM

            # Primary evidence referencing observed payment failure
            ev_summary = (
                f"Payment {payment.id} ({payment.method.value}) failed with code "
                f"'{payment.error_code or 'UNKNOWN'}': {payment.error_description or 'No description'}. "
                f"Scenario classified as {classification.scenario_id.value}: {classification.rationale}"
            )
            if len(ev_summary) < 10:
                ev_summary = ev_summary + " (verified incident context)"

            primary_evidence = FinancialEvidence(
                evidence_id=f"ev_trig_{short_digest({'inc': inc_id, 'pay': payment.id})}",
                incident_id=inc_id,
                summary=ev_summary,
                window=current_window,
                computed_at=when,
                source_confidence=SourceConfidence.OBSERVED,
                metrics=metrics,
                dimension=classification.primary_dimension,
            )

            incident = FinancialIncident(
                incident_id=inc_id,
                merchant_id=merchant_id,
                incident_type=IncidentType.PAYMENT_FAILURE_SPIKE,
                status=IncidentStatus.DETECTED,
                severity=severity,
                detected_at=when,
                window=current_window,
                evidence=(primary_evidence,),
                primary_dimension=classification.primary_dimension,
                primary_dimension_value=classification.contributing_values[0] if classification.contributing_values else None,
                metrics=metrics,
            )

            # Persist incident to DB
            self._db.save_incident(incident)

        return FailureIncidentContext(
            incident=incident,
            triggering_payment=payment,
            classification=classification,
            recent_payments=recent_payments,
            baseline_payments=baseline_payments,
            metrics=metrics,
        )
