"""Deterministic incident investigator facade with audit integration.

PROJECT_RULES 3.5, 10.7 / ARCHITECTURE.md §8.

Coordinates evidence gathering across dimensions, attaches verified evidence,
and records auditable investigation events.
"""

from datetime import datetime
from typing import Optional, Sequence

from ..audit.store import AuditLog
from ..domain.enums import AuditActor, AuditEventType
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialIncident
from ..domain.window import require_utc
from ..financial.population import PaymentLike
from .analyzer import analyze_incident
from .findings import InvestigationReport


class Investigator:
    """Deterministic failure investigator."""

    def investigate(
        self,
        incident: FinancialIncident,
        payments: Sequence[PaymentLike],
        baseline_payments: Optional[Sequence[PaymentLike]] = None,
        now: Optional[datetime] = None,
        audit_log: Optional[AuditLog] = None,
        same_hour_baseline: bool = False,
    ) -> InvestigationReport:
        """Investigate an opened incident across dimensions.

        Args:
            incident: The FinancialIncident being investigated.
            payments: Payments occurring in the incident window (or superset).
            baseline_payments: Historical payments preceding the incident window.
            now: Current timestamp injection (aware UTC).
            audit_log: Optional audit log to record lifecycle events into.
            same_hour_baseline: If True, compares slices against matching hour of day.

        Returns:
            An ``InvestigationReport`` with structured evidence and candidate contributors.
        """
        if not isinstance(incident, FinancialIncident):
            raise DomainValidationError("Investigator requires a FinancialIncident")
        if not isinstance(payments, Sequence):
            raise DomainValidationError("payments must be a Sequence of PaymentLike")

        when = require_utc(now) if now is not None else incident.detected_at

        # Record investigation started in audit log if provided
        if audit_log is not None:
            audit_log.append(
                actor=AuditActor.SYSTEM,
                event_type=AuditEventType.INVESTIGATION_STARTED,
                summary=f"Started deterministic investigation for incident {incident.incident_id}",
                incident_id=incident.incident_id,
                occurred_at=when,
                payload={
                    "incident_id": incident.incident_id,
                    "window": incident.window.label(),
                    "severity": incident.severity.value,
                },
            )

        report = analyze_incident(
            incident_id=incident.incident_id,
            window=incident.window,
            current_payments=payments,
            baseline_payments=baseline_payments,
            investigated_at=when,
            same_hour_baseline=same_hour_baseline,
        )

        # Record investigation completed in audit log if provided
        if audit_log is not None:
            audit_log.append(
                actor=AuditActor.SYSTEM,
                event_type=AuditEventType.INVESTIGATION_COMPLETED,
                summary=(
                    f"Completed investigation for incident {incident.incident_id}: "
                    f"{len(report.primary_findings)} primary finding(s), "
                    f"{len(report.secondary_findings)} secondary finding(s)"
                ),
                incident_id=incident.incident_id,
                occurred_at=when,
                payload={
                    "incident_id": incident.incident_id,
                    "primary_findings_count": len(report.primary_findings),
                    "secondary_findings_count": len(report.secondary_findings),
                    "has_multiple_concentrations": report.has_multiple_concentrations,
                    "has_sufficient_evidence": report.has_sufficient_evidence,
                },
            )

        return report


def investigate_incident(
    incident: FinancialIncident,
    payments: Sequence[PaymentLike],
    baseline_payments: Optional[Sequence[PaymentLike]] = None,
    now: Optional[datetime] = None,
    audit_log: Optional[AuditLog] = None,
    same_hour_baseline: bool = False,
) -> InvestigationReport:
    """Pure functional helper to investigate an incident."""
    investigator = Investigator()
    return investigator.investigate(
        incident=incident,
        payments=payments,
        baseline_payments=baseline_payments,
        now=now,
        audit_log=audit_log,
        same_hour_baseline=same_hour_baseline,
    )
