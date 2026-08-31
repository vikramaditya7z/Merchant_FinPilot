"""Ingestion service coordinator.

PROJECT_RULES 2.3, 2.8, 10.7 / ARCHITECTURE.md §12.

Provides the authoritative entrypoint for accepting, normalizing, enriching,
persisting, and auditing external payment streams.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Tuple

from ..audit.store import AuditLog
from ..db.database import Database
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType
from ..domain.errors import DomainValidationError
from ..domain.payment import EnrichedPayment, Payment
from ..domain.window import require_utc
from .enricher import PaymentEnricher
from .normalizer import PaymentNormalizer


@dataclass(frozen=True)
class IngestionItemResult:
    """Outcome of processing a single payment item within an ingestion batch."""

    payment_id: Optional[str]
    success: bool
    error: Optional[str] = None
    enriched_payment: Optional[EnrichedPayment] = None


@dataclass(frozen=True)
class IngestionResult:
    """Summary of a batch ingestion operation."""

    total_received: int
    ingested: int
    failed: int
    items: Tuple[IngestionItemResult, ...]
    batch_id: str
    occurred_at: datetime

    @property
    def is_all_successful(self) -> bool:
        return self.failed == 0 and self.ingested == self.total_received

    @property
    def successful_payments(self) -> Tuple[EnrichedPayment, ...]:
        return tuple(
            item.enriched_payment
            for item in self.items
            if item.success and item.enriched_payment is not None
        )


class IngestionService:
    """Coordinates validation, normalization, enrichment, persistence, and audit logging."""

    def __init__(
        self,
        database: Optional[Database] = None,
        audit_log: Optional[AuditLog] = None,
        normalizer: Optional[PaymentNormalizer] = None,
        enricher: Optional[PaymentEnricher] = None,
    ) -> None:
        self._db = database
        self._audit_log = audit_log
        self._normalizer = normalizer or PaymentNormalizer()
        self._enricher = enricher or PaymentEnricher()

    @property
    def database(self) -> Optional[Database]:
        return self._db

    @property
    def audit_log(self) -> Optional[AuditLog]:
        return self._audit_log

    @property
    def normalizer(self) -> PaymentNormalizer:
        return self._normalizer

    @property
    def enricher(self) -> PaymentEnricher:
        return self._enricher

    def ingest_payment(
        self,
        payload: Mapping[str, Any],
        metadata: Optional[Mapping[str, Any]] = None,
        record_audit: bool = True,
        now: Optional[datetime] = None,
    ) -> EnrichedPayment:
        """Ingest, normalize, enrich, and persist a single payment payload.

        Args:
            payload: Raw payment dictionary.
            metadata: Optional external metadata (merchant scope, geo tags, etc.).
            record_audit: Whether to record a FACT_INGESTED event in the audit log.
            now: Optional timestamp injection.

        Returns:
            The normalized and enriched ``EnrichedPayment``.

        Raises:
            DomainValidationError: If normalization or enrichment fails.
        """
        occurred_at = require_utc(now) if now is not None else datetime.now().astimezone()

        payment: Payment = self._normalizer.normalize(payload)
        enriched: EnrichedPayment = self._enricher.enrich(
            payment, raw_payload=payload, metadata=metadata
        )

        if self._db is not None:
            self._db.save_payment(enriched)

        if record_audit and self._audit_log is not None:
            self._audit_log.append(
                actor=AuditActor.SYSTEM,
                event_type=AuditEventType.FACT_INGESTED,
                summary=f"Ingested payment {payment.id} ({payment.amount.as_rupees()} INR, {payment.status.value})",
                occurred_at=occurred_at,
                subject_id=payment.id,
                payload={
                    "payment_id": payment.id,
                    "amount_paise": payment.amount.minor_units,
                    "currency": payment.currency.value,
                    "status": payment.status.value,
                    "method": payment.method.value,
                    "region": enriched.region,
                    "provider": enriched.provider,
                },
            )

        return enriched

    def ingest_batch(
        self,
        payloads: Sequence[Mapping[str, Any]],
        metadata: Optional[Mapping[str, Any]] = None,
        record_audit: bool = True,
        batch_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> IngestionResult:
        """Ingest, normalize, enrich, and persist a batch of payment payloads.

        Processes each item independently: invalid payloads are captured as failed
        items without aborting valid sibling items.

        Args:
            payloads: Sequence of raw payment dictionaries.
            metadata: Optional shared metadata.
            record_audit: Whether to record a summary FACT_INGESTED audit event.
            batch_id: Optional explicit batch identifier; auto-generated if None.
            now: Optional timestamp injection.

        Returns:
            An ``IngestionResult`` detailing successes and failures.
        """
        occurred_at = require_utc(now) if now is not None else datetime.now().astimezone()

        b_id = batch_id or f"batch_{short_digest({count: len(payloads), time: occurred_at.isoformat()})}"

        results: List[IngestionItemResult] = []
        valid_enriched: List[EnrichedPayment] = []
        total_amount_paise = 0

        for payload in payloads:
            p_id = None
            try:
                if isinstance(payload, Mapping):
                    p_id = payload.get("id") or payload.get("payment_id") or payload.get("transaction_id")

                payment = self._normalizer.normalize(payload)
                enriched = self._enricher.enrich(
                    payment, raw_payload=payload, metadata=metadata
                )

                valid_enriched.append(enriched)
                total_amount_paise += payment.amount.minor_units

                results.append(
                    IngestionItemResult(
                        payment_id=payment.id,
                        success=True,
                        error=None,
                        enriched_payment=enriched,
                    )
                )
            except Exception as exc:
                results.append(
                    IngestionItemResult(
                        payment_id=str(p_id) if p_id else None,
                        success=False,
                        error=str(exc),
                        enriched_payment=None,
                    )
                )

        # Batch persist valid records into database
        if self._db is not None and valid_enriched:
            self._db.save_payments(valid_enriched)

        # Audit logging
        if record_audit and self._audit_log is not None and valid_enriched:
            self._audit_log.append(
                actor=AuditActor.SYSTEM,
                event_type=AuditEventType.FACT_INGESTED,
                summary=f"Ingested batch {b_id}: {len(valid_enriched)} succeeded, {len(payloads) - len(valid_enriched)} failed",
                occurred_at=occurred_at,
                subject_id=b_id,
                payload={
                    "batch_id": b_id,
                    "total_received": len(payloads),
                    "ingested_count": len(valid_enriched),
                    "failed_count": len(payloads) - len(valid_enriched),
                    "total_amount_paise": total_amount_paise,
                },
            )

        return IngestionResult(
            total_received=len(payloads),
            ingested=len(valid_enriched),
            failed=len(payloads) - len(valid_enriched),
            items=tuple(results),
            batch_id=b_id,
            occurred_at=occurred_at,
        )
