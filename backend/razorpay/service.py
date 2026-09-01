"""Razorpay Service Coordinator.

PROJECT_RULES 10.7, 10.8, 10.9 / ARCHITECTURE.md §12.

Coordinates Razorpay API polling, webhook verification, normalization,
enrichment, database persistence, and audit logging into FinPilot.
"""

from datetime import datetime
import json
import threading
from typing import Any, Dict, List, Mapping, Optional, Tuple

from ..application.contracts import PipelineResult, PipelineStatus
from ..application.trigger import BackgroundJobDispatcher, IncidentTrigger, TriggerStatus
from ..audit.store import AuditLog
from ..db.database import Database
from ..domain.canonical import short_digest
from ..domain.enums import AuditActor, AuditEventType, PolicyVerdict
from ..domain.payment import EnrichedPayment, Payment
from ..execution.store import ExecutionStore
from ..ingestion.enricher import PaymentEnricher
from ..ingestion.service import IngestionItemResult, IngestionResult, IngestionService
from ..investigation.context import ContextAssembler
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
        orchestrator: Optional[Any] = None,
        dispatcher: Optional[BackgroundJobDispatcher] = None,
        context_assembler: Optional[ContextAssembler] = None,
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
        self._orchestrator = orchestrator
        self._dispatcher = dispatcher
        self._context_assembler = context_assembler or (
            ContextAssembler(database=self._db) if self._db is not None else None
        )
        self._merchant_locks: Dict[str, threading.Lock] = {}
        self._locks_mutex = threading.Lock()

    def _get_merchant_lock(self, merchant_id: str) -> threading.Lock:
        with self._locks_mutex:
            if merchant_id not in self._merchant_locks:
                self._merchant_locks[merchant_id] = threading.Lock()
            return self._merchant_locks[merchant_id]

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

    @property
    def orchestrator(self) -> Optional[Any]:
        return self._orchestrator

    @property
    def dispatcher(self) -> Optional[BackgroundJobDispatcher]:
        return self._dispatcher

    @property
    def context_assembler(self) -> Optional[ContextAssembler]:
        return self._context_assembler

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
            existing_trigger = (
                self._db.get_trigger_by_event(result.event_id)
                if (self._db is not None and result.event_id)
                else None
            )
            dup_resp: Dict[str, Any] = {
                "status": "duplicate_skipped",
                "message": result.message,
                "event_id": result.event_id,
            }
            if existing_trigger is not None:
                dup_resp["job_id"] = existing_trigger["job_id"]
                dup_resp["incident_id"] = existing_trigger["incident_id"]
                dup_resp["job_status"] = existing_trigger["status"]
            return 200, dup_resp

        # If webhook contained a valid normalized payment, enrich & persist it
        enriched_payment: Optional[EnrichedPayment] = None
        effective_merchant = merchant_id or "merchant_default"
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
                    effective_merchant = str(acc)

            enriched_payment = self._ingestion.enricher.enrich(
                payment=result.normalized_payment,
                metadata=metadata,
            )

            # Persist to database
            if self._db is not None:
                self._db.save_payments([enriched_payment], merchant_id=effective_merchant)

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

        # -------------------------------------------------------------
        # Automated Incident Trigger for Payment Failures
        # -------------------------------------------------------------
        trigger: Optional[IncidentTrigger] = None
        if enriched_payment is not None:
            p = enriched_payment.payment
            if p.is_failure or result.event_type == "payment.failed":
                event_identifier = result.event_id or f"evt_syn_{p.id}"

                # Check deduplication in database
                existing_trigger = (
                    self._db.get_trigger_by_event(event_identifier)
                    if self._db is not None
                    else None
                )

                if existing_trigger is not None:
                    trigger = IncidentTrigger(
                        job_id=existing_trigger["job_id"],
                        incident_id=existing_trigger["incident_id"],
                        merchant_id=existing_trigger["merchant_id"],
                        source=existing_trigger["source"],
                        event_id=existing_trigger["event_id"],
                        event_type=existing_trigger["event_type"],
                        payment_id=existing_trigger["payment_id"],
                        status=TriggerStatus(existing_trigger["status"]),
                        created_at=datetime.fromisoformat(existing_trigger["created_at"]),
                        updated_at=datetime.fromisoformat(existing_trigger["updated_at"]),
                        attempt_count=existing_trigger["attempt_count"],
                        error_message=existing_trigger.get("error_message"),
                    )
                else:
                    trigger = IncidentTrigger.create(
                        merchant_id=effective_merchant,
                        event_id=event_identifier,
                        event_type=result.event_type or "payment.failed",
                        payment_id=p.id,
                        source="razorpay_webhook",
                        payload={
                            "amount_paise": p.amount.minor_units,
                            "currency": p.amount.currency.value,
                            "method": p.method.value,
                            "error_code": p.error_code,
                            "error_description": p.error_description,
                        },
                    )
                    if self._db is not None:
                        self._db.save_trigger(trigger.to_dict())

                    if self._audit_log is not None:
                        self._audit_log.append(
                            actor=AuditActor.SYSTEM,
                            event_type=AuditEventType.INCIDENT_DETECTED,
                            summary=f"Incident {trigger.incident_id} queued for failed payment {p.id}",
                            incident_id=trigger.incident_id,
                            subject_id=p.id,
                            payload={
                                "job_id": trigger.job_id,
                                "payment_id": p.id,
                                "event_id": trigger.event_id,
                            },
                        )

                    # Asynchronously dispatch pipeline
                    if (
                        self._dispatcher is not None
                        and self._orchestrator is not None
                        and self._context_assembler is not None
                    ):
                        self._dispatcher.submit(
                            self._process_incident_job,
                            trigger=trigger,
                            payment=p,
                            merchant_id=effective_merchant,
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
        if trigger is not None:
            resp_dict["job_id"] = trigger.job_id
            resp_dict["incident_id"] = trigger.incident_id
            resp_dict["job_status"] = trigger.status.value

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

    def _process_incident_job(
        self,
        trigger: IncidentTrigger,
        payment: Payment,
        merchant_id: str,
    ) -> None:
        """Asynchronously process an incident job through context assembly and orchestration."""
        if self._db is None or self._orchestrator is None or self._context_assembler is None:
            return

        with self._get_merchant_lock(merchant_id):
            try:
                # 1. Transition to PROCESSING
                self._db.update_trigger_status(
                    job_id=trigger.job_id,
                    status=TriggerStatus.PROCESSING.value,
                )

                # 2. Assemble context & classify scenario
                ctx = self._context_assembler.assemble(
                    payment=payment,
                    merchant_id=merchant_id,
                )

                canonical_inc_id = ctx.incident.incident_id if ctx.incident else trigger.incident_id

                # 3. Process through orchestrator pipeline
                result: PipelineResult = self._orchestrator.process_incident(
                    incident=ctx.incident,
                    metrics=ctx.metrics,
                    payments=ctx.recent_payments,
                    baseline_payments=ctx.baseline_payments,
                    merchant_id=merchant_id,
                )

                # 4. Serialize outcome and update status
                completed_iso = datetime.now().astimezone().isoformat()
                from ..api.contracts import ProcessIncidentResponse
                pipe_dict = ProcessIncidentResponse.from_pipeline_result(result).to_dict()
                pipe_dict["scenario_classification"] = {
                    "scenario_id": ctx.classification.scenario_id.value,
                    "confidence": ctx.classification.confidence,
                    "rationale": ctx.classification.rationale,
                    "is_incident": ctx.classification.is_incident,
                    "is_action_eligible": ctx.classification.is_action_eligible,
                }
                pipe_json = json.dumps(pipe_dict)

                if result.status == PipelineStatus.COMPLETED:
                    self._db.update_trigger_status(
                        job_id=trigger.job_id,
                        status=TriggerStatus.COMPLETED.value,
                        completed_at=completed_iso,
                        payload_json=pipe_json,
                        incident_id=canonical_inc_id,
                    )
                elif result.status == PipelineStatus.STOPPED:
                    is_escalated = (
                        result.policy_decision is not None
                        and result.policy_decision.verdict == PolicyVerdict.ESCALATE
                    )
                    final_status = (
                        TriggerStatus.ESCALATED.value
                        if is_escalated
                        else TriggerStatus.COMPLETED.value
                    )
                    self._db.update_trigger_status(
                        job_id=trigger.job_id,
                        status=final_status,
                        completed_at=completed_iso,
                        payload_json=pipe_json,
                        incident_id=canonical_inc_id,
                    )
                else:
                    self._db.update_trigger_status(
                        job_id=trigger.job_id,
                        status=TriggerStatus.FAILED.value,
                        error_message=result.stop_reason or "Pipeline stopped",
                        completed_at=completed_iso,
                        payload_json=pipe_json,
                        incident_id=canonical_inc_id,
                    )

            except Exception as exc:
                if self._db is not None:
                    self._db.update_trigger_status(
                        job_id=trigger.job_id,
                        status=TriggerStatus.FAILED.value,
                        error_message=str(exc),
                        completed_at=datetime.now().astimezone().isoformat(),
                    )
                if self._audit_log is not None:
                    self._audit_log.append(
                        actor=AuditActor.SYSTEM,
                        event_type=AuditEventType.PIPELINE_STOPPED,
                        summary=f"Incident job {trigger.job_id} failed: {str(exc)}",
                        incident_id=trigger.incident_id,
                        subject_id=trigger.payment_id,
                        payload={"job_id": trigger.job_id, "error": str(exc)},
                    )

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
