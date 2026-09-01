"""Razorpay Service Coordinator.

PROJECT_RULES 10.7, 10.8, 10.9 / ARCHITECTURE.md §12.

Coordinates Razorpay API polling, webhook verification, normalization,
enrichment, database persistence, and audit logging into FinPilot.
"""

from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..audit.store import AuditLog
from ..db.database import Database
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType
from ..domain.payment import EnrichedPayment, Payment
from ..execution.store import ExecutionStore
from ..ingestion.enricher import PaymentEnricher
from ..ingestion.service import IngestionItemResult, IngestionResult, IngestionService
from .client import RazorpayClient
from .config import RazorpayConfig
from .reconciler import RazorpayReconciler, ReconciliationReport, ReconciliationStatus
from .webhook import RazorpayWebhookHandler, WebhookProcessingResult


class RazorpayService:
    """Service facade for all Razorpay inbound and outbound integrations."""

    def __init__(
        self,
        config: Optional[RazorpayConfig] = None,
        client: Optional[RazorpayClient] = None,
        webhook_handler: Optional[RazorpayWebhookHandler] = None,
        ingestion_service: Optional[IngestionService] = None,
        execution_store: Optional[ExecutionStore] = None,
        reconciler: Optional[RazorpayReconciler] = None,
        database: Optional[Database] = None,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        self._config = config or RazorpayConfig.from_env()
        self._client = client or RazorpayClient(config=self._config)
        self._webhook_handler = webhook_handler or RazorpayWebhookHandler(config=self._config)
        self._ingestion = ingestion_service or IngestionService(
            database=database,
            audit_log=audit_log,
        )
        self._db = database or (self._ingestion.database if self._ingestion else None)
        self._audit_log = audit_log or (self._ingestion.audit_log if self._ingestion else None)
        self._execution_store = execution_store
        self._reconciler = reconciler or (
            RazorpayReconciler(store=self._execution_store, audit_log=self._audit_log)
            if (self._execution_store is not None or self._audit_log is not None)
            else None
        )

    @property
    def reconciler(self) -> Optional[RazorpayReconciler]:
        return self._reconciler

    @property
    def execution_store(self) -> Optional[ExecutionStore]:
        return self._execution_store

    @property
    def config(self) -> RazorpayConfig:
        return self._config

    @property
    def client(self) -> RazorpayClient:
        return self._client

    @property
    def webhook_handler(self) -> RazorpayWebhookHandler:
        return self._webhook_handler

    @property
    def ingestion_service(self) -> IngestionService:
        return self._ingestion

    def handle_webhook(
        self,
        raw_body: bytes,
        signature: Optional[str],
        merchant_id: Optional[str] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        """Process an inbound webhook, verify HMAC-SHA256 signature, enrich, persist, and audit.

        Returns:
            Tuple of (HTTP status code, JSON response dictionary).
        """
        result: WebhookProcessingResult = self._webhook_handler.process_webhook(
            raw_body=raw_body,
            signature=signature,
        )

        if not result.success:
            return result.status_code, {
                "status": "error",
                "message": result.message,
                "event_id": result.event_id,
            }

        if result.is_duplicate:
            return 200, {
                "status": "duplicate_skipped",
                "message": result.message,
                "event_id": result.event_id,
            }

        # If webhook contained a valid normalized payment, enrich & persist it
        enriched_payment: Optional[EnrichedPayment] = None
        if result.normalized_payment is not None:
            metadata: Dict[str, Any] = {
                "provider": "razorpay",
                "route": "razorpay_direct",
            }
            if merchant_id:
                metadata["merchant_id"] = merchant_id
            elif result.raw_payload:
                # Check for account_id or merchant metadata in payload
                acc = result.raw_payload.get("account_id")
                if acc:
                    metadata["merchant_id"] = str(acc)

            enriched_payment = self._ingestion.enricher.enrich(
                payment=result.normalized_payment,
                metadata=metadata,
            )

            # Persist to database
            if self._db is not None:
                self._db.save_payments([enriched_payment])

            p = enriched_payment.payment
            # Record audit event
            if self._audit_log is not None:
                self._audit_log.append(
                    actor=AuditActor.SYSTEM,
                    event_type=AuditEventType.FACT_INGESTED,
                    summary=f"Ingested Razorpay webhook payment {p.id} ({p.status.value})",
                    incident_id=None,
                    subject_id=p.id,
                    payload={
                        "payment_id": p.id,
                        "event_id": result.event_id,
                        "event_type": result.event_type,
                        "amount_paise": p.amount.minor_units,
                        "status": p.status.value,
                        "method": p.method.value,
                    },
                )

        # Outbound Execution Reconciliation
        reconciliation_report: Optional[ReconciliationReport] = None
        if self._reconciler is not None and result.raw_payload is not None and result.event_id and result.event_type:
            reconciliation_report = self._reconciler.reconcile_event(
                raw_payload=result.raw_payload,
                event_id=result.event_id,
                event_type=result.event_type,
                normalized_payment=result.normalized_payment,
            )

        resp_dict: Dict[str, Any] = {
            "status": "processed",
            "event_id": result.event_id,
            "event_type": result.event_type,
            "payment_id": enriched_payment.payment.id if enriched_payment else None,
            "message": result.message,
        }
        if reconciliation_report is not None:
            resp_dict["reconciliation"] = {
                "status": reconciliation_report.status.value,
                "execution_id": reconciliation_report.execution_id,
                "provider_reference": reconciliation_report.provider_reference,
                "reconciled_status": (
                    reconciliation_report.reconciled_execution_status.value
                    if reconciliation_report.reconciled_execution_status
                    else None
                ),
                "mismatch_reason": reconciliation_report.mismatch_reason,
            }

        return 200, resp_dict

    def sync_recent_payments(
        self,
        merchant_id: str = "merchant_default",
        count: int = 100,
        from_timestamp: Optional[int] = None,
        to_timestamp: Optional[int] = None,
    ) -> IngestionResult:
        """Fetch historical/recent payments from Razorpay REST API and ingest them."""
        response = self._client.fetch_payments(
            from_timestamp=from_timestamp,
            to_timestamp=to_timestamp,
            count=count,
        )

        items = response.get("items", [])
        raw_payloads: List[Dict[str, Any]] = []
        for it in items:
            if isinstance(it, dict):
                raw_payloads.append(it)

        metadata = {
            "merchant_id": merchant_id,
            "provider": "razorpay",
            "route": "razorpay_api_sync",
        }

        return self._ingestion.ingest_batch(
            payloads=raw_payloads,
            metadata=metadata,
        )
