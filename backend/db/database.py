"""SQLite database repository for Merchant FinPilot.

PROJECT_RULES 4.2, 10.7, 10.8 / ARCHITECTURE.md §6.

Provides typed persistence for domain contracts:
- Payments and EnrichedPayments
- FinancialIncidents and FinancialEvidence
- InvestigationReports
- AuditEvents

Invariants:
- All monetary values stored as INTEGER minor units (paise).
- Timestamps preserved as ISO-8601 UTC.
- Enums converted to/from strings cleanly.
- Re-hydrated records are validated through their domain constructors.
"""

import json
import sqlite3
from datetime import datetime
from typing import Any, List, Optional, Sequence, Tuple

from ..domain.audit import AuditEvent
from ..domain.enums import (
    AuditActor,
    AuditEventType,
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
from ..domain.money import Money
from ..domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from ..domain.window import TimeWindow, require_utc
from ..financial.population import PaymentLike, as_payment
from ..investigation.findings import InvestigationReport
from .schema import SCHEMA_DDL
from .serde import (
    dict_to_evidence,
    evidence_to_dict,
    json_to_metrics,
    json_to_report,
    metrics_to_json,
    report_to_json,
)


class Database:
    """Lightweight, typed SQLite repository for FinPilot domain entities."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(
            db_path,
            check_same_thread=False,
            isolation_level=None,  # Autocommit mode; explicit BEGIN/COMMIT used when needed
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.executescript(SCHEMA_DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # Payments repository
    # -----------------------------------------------------------------------

    def save_payment(
        self,
        item: PaymentLike,
        enrichment: Optional[PaymentEnrichment] = None,
    ) -> None:
        """Save a single payment and its optional enrichment."""
        payment = as_payment(item)
        enr = (
            item.enrichment
            if isinstance(item, EnrichedPayment)
            else enrichment
        )

        query = """
        INSERT OR REPLACE INTO payments (
            id, order_id, amount_paise, currency, status, method,
            created_at, error_code, error_description, error_source,
            error_step, error_reason,
            region, provider, failure_category, source_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            payment.id,
            payment.order_id,
            payment.amount.minor_units,
            payment.amount.currency.value,
            payment.status.value,
            payment.method.value,
            payment.created_at.isoformat(),
            payment.error_code,
            payment.error_description,
            payment.error_source,
            payment.error_step,
            payment.error_reason,
            enr.region if enr else None,
            enr.provider if enr else None,
            enr.failure_category.value if (enr and enr.failure_category) else None,
            enr.source_confidence.value if enr else None,
        )

        with self._conn:
            self._conn.execute(query, params)

    def save_payments(self, items: Sequence[PaymentLike]) -> None:
        """Batch save multiple payments."""
        if not items:
            return
        query = """
        INSERT OR REPLACE INTO payments (
            id, order_id, amount_paise, currency, status, method,
            created_at, error_code, error_description, error_source,
            error_step, error_reason,
            region, provider, failure_category, source_confidence
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        param_list = []
        for item in items:
            payment = as_payment(item)
            enr = (
                item.enrichment
                if isinstance(item, EnrichedPayment)
                else None
            )
            param_list.append(
                (
                    payment.id,
                    payment.order_id,
                    payment.amount.minor_units,
                    payment.amount.currency.value,
                    payment.status.value,
                    payment.method.value,
                    payment.created_at.isoformat(),
                    payment.error_code,
                    payment.error_description,
                    payment.error_source,
                    payment.error_step,
                    payment.error_reason,
                    enr.region if enr else None,
                    enr.provider if enr else None,
                    enr.failure_category.value if (enr and enr.failure_category) else None,
                    enr.source_confidence.value if enr else None,
                )
            )

        with self._conn:
            self._conn.executemany(query, param_list)

    def _row_to_enriched_payment(self, row: sqlite3.Row) -> EnrichedPayment:
        curr = Currency(row["currency"])
        p = Payment(
            id=row["id"],
            order_id=row["order_id"],
            amount=Money(row["amount_paise"], curr),
            status=PaymentStatus(row["status"]),
            method=PaymentMethod(row["method"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            error_code=row["error_code"],
            error_description=row["error_description"],
            error_source=row["error_source"],
            error_step=row["error_step"],
            error_reason=row["error_reason"],
        )

        enr = PaymentEnrichment(
            payment_id=p.id,
            region=row["region"],
            provider=row["provider"],
            failure_category=FailureCategory(row["failure_category"]) if row["failure_category"] else None,
            source_confidence=SourceConfidence.ENRICHED,
        )

        return EnrichedPayment(payment=p, enrichment=enr)

    def get_payment(self, payment_id: str) -> Optional[Payment]:
        """Retrieve a Payment by ID."""
        row = self._conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_enriched_payment(row).payment

    def get_enriched_payment(self, payment_id: str) -> Optional[EnrichedPayment]:
        """Retrieve an EnrichedPayment by ID."""
        row = self._conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_enriched_payment(row)

    def list_payments(
        self,
        window: Optional[TimeWindow] = None,
        status: Optional[PaymentStatus] = None,
        method: Optional[PaymentMethod] = None,
        order_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> Tuple[EnrichedPayment, ...]:
        """List payments matching optional criteria."""
        clauses = []
        params: List[Any] = []

        if window is not None:
            clauses.append("created_at >= ? AND created_at < ?")
            params.extend([window.start.isoformat(), window.end.isoformat()])
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if method is not None:
            clauses.append("method = ?")
            params.append(method.value)
        if order_id is not None:
            clauses.append("order_id = ?")
            params.append(order_id)

        sql = "SELECT * FROM payments"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC, id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_to_enriched_payment(r) for r in rows)

    # -----------------------------------------------------------------------
    # Incidents & Evidence repository
    # -----------------------------------------------------------------------

    def save_incident(self, incident: FinancialIncident) -> None:
        """Save a FinancialIncident and all attached FinancialEvidence."""
        if not isinstance(incident, FinancialIncident):
            raise DomainValidationError("save_incident requires FinancialIncident")

        inc_query = """
        INSERT OR REPLACE INTO incidents (
            incident_id, incident_key, merchant_id, incident_type, status,
            severity, detected_at, window_start, window_end,
            primary_dimension, primary_dimension_value, metrics_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        inc_params = (
            incident.incident_id,
            incident.incident_key,
            incident.merchant_id,
            incident.incident_type.value,
            incident.status.value,
            incident.severity.value,
            incident.detected_at.isoformat(),
            incident.window.start.isoformat(),
            incident.window.end.isoformat(),
            incident.primary_dimension.value if incident.primary_dimension else None,
            incident.primary_dimension_value,
            metrics_to_json(incident.metrics),
        )

        with self._conn:
            self._conn.execute(inc_query, inc_params)
            for ev in incident.evidence:
                self.save_evidence(ev)

    def save_evidence(self, evidence: FinancialEvidence) -> None:
        """Save an individual piece of FinancialEvidence."""
        if not isinstance(evidence, FinancialEvidence):
            raise DomainValidationError("save_evidence requires FinancialEvidence")

        ev_dict = evidence_to_dict(evidence)
        query = """
        INSERT OR REPLACE INTO evidence (
            evidence_id, incident_id, summary, window_start, window_end,
            computed_at, source_confidence, dimension, metrics_json, breakdown_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            ev_dict["evidence_id"],
            ev_dict["incident_id"],
            ev_dict["summary"],
            ev_dict["window_start"],
            ev_dict["window_end"],
            ev_dict["computed_at"],
            ev_dict["source_confidence"],
            ev_dict["dimension"],
            ev_dict["metrics_json"],
            ev_dict["breakdown_json"],
        )

        with self._conn:
            self._conn.execute(query, params)

    def get_evidence(self, evidence_id: str) -> Optional[FinancialEvidence]:
        """Retrieve a single FinancialEvidence record by ID."""
        row = self._conn.execute(
            "SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            return None
        return dict_to_evidence(dict(row))

    def list_evidence(self, incident_id: str) -> Tuple[FinancialEvidence, ...]:
        """List all evidence attached to an incident."""
        rows = self._conn.execute(
            "SELECT * FROM evidence WHERE incident_id = ? ORDER BY computed_at ASC, evidence_id ASC",
            (incident_id,),
        ).fetchall()
        return tuple(dict_to_evidence(dict(r)) for r in rows)

    def get_incident(self, incident_id: str) -> Optional[FinancialIncident]:
        """Retrieve a FinancialIncident by ID."""
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_incident(row)

    def get_incident_by_key(self, incident_key: str) -> Optional[FinancialIncident]:
        """Retrieve a FinancialIncident by its deduplication idempotency key."""
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE incident_key = ?", (incident_key,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_incident(row)

    def list_incidents(
        self,
        merchant_id: Optional[str] = None,
        status: Optional[IncidentStatus] = None,
        limit: Optional[int] = None,
    ) -> Tuple[FinancialIncident, ...]:
        """List incidents with optional filters."""
        clauses = []
        params: List[Any] = []

        if merchant_id is not None:
            clauses.append("merchant_id = ?")
            params.append(merchant_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)

        sql = "SELECT * FROM incidents"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY detected_at DESC, incident_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_to_incident(r) for r in rows)

    def _row_to_incident(self, row: sqlite3.Row) -> FinancialIncident:
        w_start = datetime.fromisoformat(row["window_start"])
        w_end = datetime.fromisoformat(row["window_end"])
        window = TimeWindow(w_start, w_end)
        metrics = json_to_metrics(row["metrics_json"])
        evidence = self.list_evidence(row["incident_id"])

        return FinancialIncident(
            incident_id=row["incident_id"],
            merchant_id=row["merchant_id"],
            incident_type=IncidentType(row["incident_type"]),
            status=IncidentStatus(row["status"]),
            severity=Severity(row["severity"]),
            detected_at=datetime.fromisoformat(row["detected_at"]),
            window=window,
            metrics=metrics,
            evidence=evidence,
            primary_dimension=Dimension(row["primary_dimension"]) if row["primary_dimension"] else None,
            primary_dimension_value=row["primary_dimension_value"],
        )

    # -----------------------------------------------------------------------
    # Investigations repository
    # -----------------------------------------------------------------------

    def save_investigation(self, report: InvestigationReport) -> None:
        """Save an InvestigationReport."""
        if not isinstance(report, InvestigationReport):
            raise DomainValidationError("save_investigation requires InvestigationReport")

        query = """
        INSERT OR REPLACE INTO investigations (
            incident_id, window_start, window_end, investigated_at,
            has_sufficient_evidence, has_multiple_concentrations,
            summary, report_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            report.incident_id,
            report.window.start.isoformat(),
            report.window.end.isoformat(),
            report.investigated_at.isoformat(),
            1 if report.has_sufficient_evidence else 0,
            1 if report.has_multiple_concentrations else 0,
            report.summary,
            report_to_json(report),
        )

        with self._conn:
            self._conn.execute(query, params)

    def get_investigation(self, incident_id: str) -> Optional[InvestigationReport]:
        """Retrieve an InvestigationReport by incident ID."""
        row = self._conn.execute(
            "SELECT report_json FROM investigations WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        return json_to_report(row["report_json"])

    # -----------------------------------------------------------------------
    # Audit Events repository
    # -----------------------------------------------------------------------

    def save_audit_event(self, event: AuditEvent) -> None:
        """Save an AuditEvent to the persistent log."""
        if not isinstance(event, AuditEvent):
            raise DomainValidationError("save_audit_event requires AuditEvent")

        query = """
        INSERT INTO audit_events (
            event_id, sequence, occurred_at, actor, event_type,
            summary, incident_id, subject_id, payload_json, payload_digest
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params = (
            event.event_id,
            event.sequence,
            event.occurred_at.isoformat(),
            event.actor.value,
            event.event_type.value,
            event.summary,
            event.incident_id,
            event.subject_id,
            json.dumps(dict(event.payload), sort_keys=True),
            event.payload_digest,
        )

        with self._conn:
            self._conn.execute(query, params)

    def get_audit_event(self, event_id: str) -> Optional[AuditEvent]:
        """Retrieve a single AuditEvent by ID."""
        row = self._conn.execute(
            "SELECT * FROM audit_events WHERE event_id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_audit_event(row)

    def list_audit_events(
        self,
        incident_id: Optional[str] = None,
        actor: Optional[AuditActor] = None,
        event_type: Optional[AuditEventType] = None,
        limit: Optional[int] = None,
    ) -> Tuple[AuditEvent, ...]:
        """List audit events in strictly monotonic sequence order."""
        clauses = []
        params: List[Any] = []

        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        if actor is not None:
            clauses.append("actor = ?")
            params.append(actor.value)
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type.value)

        sql = "SELECT * FROM audit_events"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY sequence ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return tuple(self._row_to_audit_event(r) for r in rows)

    def get_max_audit_sequence(self) -> int:
        """Get the highest recorded audit sequence number, or 0 if empty."""
        row = self._conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS max_seq FROM audit_events"
        ).fetchone()
        return int(row["max_seq"]) if row else 0

    def _row_to_audit_event(self, row: sqlite3.Row) -> AuditEvent:
        return AuditEvent(
            event_id=row["event_id"],
            sequence=row["sequence"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            actor=AuditActor(row["actor"]),
            event_type=AuditEventType(row["event_type"]),
            summary=row["summary"],
            incident_id=row["incident_id"],
            subject_id=row["subject_id"],
            payload=json.loads(row["payload_json"]),
            payload_digest=row["payload_digest"],
        )
