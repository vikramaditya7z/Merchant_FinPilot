"""Incident Trigger model and background worker dispatcher.

PROJECT_RULES 1.4, 1.5, 10.8 / ARCHITECTURE.md §12.

Provides:
- TriggerStatus lifecycle states.
- Typed IncidentTrigger container for durable event job tracking.
- In-process BackgroundJobDispatcher backed by ThreadPoolExecutor.
"""

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, Optional

from ..domain.canonical import short_digest
from ..domain.errors import DomainValidationError
from ..domain.window import require_utc


class TriggerStatus(str, Enum):
    """Lifecycle of an automatically triggered incident job."""

    RECEIVED = "received"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    ESCALATED = "escalated"


@dataclass(frozen=True)
class IncidentTrigger:
    """A durable record representing an automatically triggered incident job."""

    job_id: str
    incident_id: str
    merchant_id: str
    source: str
    event_id: str
    event_type: str
    payment_id: str
    status: TriggerStatus
    created_at: datetime
    updated_at: datetime
    attempt_count: int = 0
    error_message: Optional[str] = None
    completed_at: Optional[datetime] = None
    payload: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise DomainValidationError("job_id must be a non-empty string")
        if not isinstance(self.incident_id, str) or not self.incident_id.strip():
            raise DomainValidationError("incident_id must be a non-empty string")
        if not isinstance(self.merchant_id, str) or not self.merchant_id.strip():
            raise DomainValidationError("merchant_id must be a non-empty string")
        if not isinstance(self.source, str) or not self.source.strip():
            raise DomainValidationError("source must be a non-empty string")
        if not isinstance(self.event_id, str) or not self.event_id.strip():
            raise DomainValidationError("event_id must be a non-empty string")
        if not isinstance(self.event_type, str) or not self.event_type.strip():
            raise DomainValidationError("event_type must be a non-empty string")
        if not isinstance(self.payment_id, str) or not self.payment_id.strip():
            raise DomainValidationError("payment_id must be a non-empty string")
        if not isinstance(self.status, TriggerStatus):
            raise DomainValidationError(f"invalid TriggerStatus: {self.status!r}")

    @classmethod
    def create(
        cls,
        merchant_id: str,
        event_id: str,
        event_type: str,
        payment_id: str,
        source: str = "razorpay_webhook",
        now: Optional[datetime] = None,
        incident_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> "IncidentTrigger":
        """Factory creating a new QUEUED IncidentTrigger."""
        when = require_utc(now) if now is not None else datetime.now().astimezone()
        job_id = f"job_{short_digest({'evt': event_id, 'pay': payment_id, 'm': merchant_id})}"
        inc_id = incident_id or f"inc_{short_digest({'m': merchant_id, 'w': when.strftime('%Y%m%d%H')})}"

        return cls(
            job_id=job_id,
            incident_id=inc_id,
            merchant_id=merchant_id,
            source=source,
            event_id=event_id,
            event_type=event_type,
            payment_id=payment_id,
            status=TriggerStatus.QUEUED,
            created_at=when,
            updated_at=when,
            attempt_count=0,
            payload=payload or {},
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a safe dictionary for database or JSON response."""
        return {
            "job_id": self.job_id,
            "incident_id": self.incident_id,
            "merchant_id": self.merchant_id,
            "source": self.source,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "payment_id": self.payment_id,
            "status": self.status.value,
            "attempt_count": self.attempt_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "payload": self.payload,
        }


class BackgroundJobDispatcher:
    """In-process asynchronous job dispatcher backed by ThreadPoolExecutor.

    Ensures that webhook acknowledgment is fast and does not wait on Gemini or
    expensive financial verification pipelines.
    """

    def __init__(self, max_workers: int = 4) -> None:
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="FinPilotWorker",
        )
        self._is_shutdown = False

    @property
    def is_running(self) -> bool:
        return not self._is_shutdown

    def submit(
        self,
        fn: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Optional[Future]:
        """Submit a task to run asynchronously in the background.

        Returns:
            concurrent.futures.Future if successfully scheduled, or None if dispatcher is shut down.
        """
        if self._is_shutdown:
            return None
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = False) -> None:
        """Gracefully shut down the thread pool."""
        self._is_shutdown = True
        self._executor.shutdown(wait=wait)
