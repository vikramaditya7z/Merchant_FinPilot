"""Live window evaluator and automated incident detection coordinator.

PROJECT_RULES 1.4, 1.5, 3.11, 4.1 / ARCHITECTURE.md §8, §12.

Separation of Concerns:
- Database: repository for persisted transactions and historical records.
- LiveWindowEvaluator: slices windows, retrieves history, delegates arithmetic to the financial engine.
- Financial Engine: deterministic mathematical measurement (compute_metrics, build_daily_hourly_baseline).
- Detector: evaluates 3-sigma anomaly gates against thresholds.
- Orchestrator: coordinates 6-stage investigation & verification when an incident is opened.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable, Dict, Mapping, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from ..application.contracts import PipelineResult
    from ..application.orchestrator import FinancialIncidentOrchestrator

from ..db.database import Database
from ..domain.enums import ComparableWindowMode
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialIncident
from ..domain.metrics import FinancialMetrics
from ..domain.payment import EnrichedPayment
from ..domain.window import UTC, TimeWindow, require_utc
from ..financial.engine import (
    build_daily_hourly_baseline,
    build_hourly_baseline,
    compute_metrics,
)
from .detector import Detector


@dataclass(frozen=True)
class LiveEvaluationResult:
    """Outcome of a live window measurement and detection evaluation pass."""

    triggered: bool
    merchant_id: str
    window: TimeWindow
    metrics: FinancialMetrics
    evaluated_at: datetime
    current_payment_count: int
    baseline_payment_count: int
    incident: Optional[FinancialIncident] = None
    pipeline_result: Optional[PipelineResult] = None

    @property
    def has_incident(self) -> bool:
        return self.incident is not None

    @property
    def is_pipeline_completed(self) -> bool:
        return self.pipeline_result is not None and self.pipeline_result.is_completed


class LiveWindowEvaluator:
    """Evaluates current transaction windows against historical baselines in the database."""

    def __init__(
        self,
        database: Database,
        detector: Optional[Detector] = None,
        orchestrator: Optional[FinancialIncidentOrchestrator] = None,
    ) -> None:
        if not isinstance(database, Database):
            raise DomainValidationError("LiveWindowEvaluator requires a Database instance")
        self._db = database
        self._detector = detector or Detector()
        self._orchestrator = orchestrator

    @property
    def database(self) -> Database:
        return self._db

    @property
    def detector(self) -> Detector:
        return self._detector

    @property
    def orchestrator(self) -> Optional[FinancialIncidentOrchestrator]:
        return self._orchestrator

    def evaluate_window(
        self,
        merchant_id: str = "merchant_default",
        now: Optional[datetime] = None,
        window_hours: int = 1,
        baseline_days: int = 7,
        comparable_mode: ComparableWindowMode = ComparableWindowMode.ALL,
        auto_orchestrate: bool = True,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> LiveEvaluationResult:
        """Measure the current window from database payments and evaluate for incidents.

        Args:
            merchant_id: Merchant identifier scope.
            now: Injected evaluation anchor timestamp (aware UTC). If None, resolves
                to the latest payment created_at timestamp in the database or clock.
            window_hours: Duration of current evaluation window in hours (default 1).
            baseline_days: Lookback span for historical comparison in days (default 7).
            comparable_mode: ALL or SAME_HOUR_OF_DAY baseline matching.
            auto_orchestrate: If True and an anomaly triggers, automatically dispatches
                the incident to the orchestrator.
            on_progress: Optional streaming progress callback.

        Returns:
            A ``LiveEvaluationResult`` with computed metrics, detection status, and
            optional pipeline execution outcome.
        """
        if not isinstance(merchant_id, str) or not merchant_id.strip():
            raise DomainValidationError("merchant_id must be a non-empty string")
        if isinstance(window_hours, bool) or not isinstance(window_hours, int) or window_hours < 1:
            raise DomainValidationError("window_hours must be a positive int >= 1")
        if isinstance(baseline_days, bool) or not isinstance(baseline_days, int) or baseline_days < 1:
            raise DomainValidationError("baseline_days must be a positive int >= 1")

        # 1. Resolve evaluation anchor time
        if now is not None:
            anchor = require_utc(now, "now")
        else:
            latest_ts = self._db.get_latest_payment_timestamp()
            anchor = latest_ts if latest_ts is not None else datetime.now().astimezone(UTC)

        # 2. Slice evaluation windows
        current_window = TimeWindow(anchor - timedelta(hours=window_hours), anchor)
        baseline_start = current_window.start - timedelta(days=baseline_days)
        baseline_window = TimeWindow(baseline_start, current_window.start)

        # 3. Retrieve payment records from database
        current_payments = self._db.list_payments_in_window(current_window)
        baseline_payments = self._db.list_payments_in_window(baseline_window)

        # 4. Build historical baseline buckets
        if (current_window.start.minute, current_window.start.second, current_window.start.microsecond) == (0, 0, 0):
            baseline_buckets = build_daily_hourly_baseline(
                baseline_payments, current_window, baseline_days
            )
        else:
            baseline_buckets = build_hourly_baseline(
                baseline_payments, current_window, lookback_windows=baseline_days * 24
            )

        # 5. Compute deterministic financial metrics
        metrics = compute_metrics(
            items=current_payments,
            window=current_window,
            now=anchor,
            baseline_windows=baseline_buckets,
            comparable_mode=comparable_mode,
        )

        # 6. Evaluate detection criteria
        incident = self._detector.detect(
            metrics=metrics,
            merchant_id=merchant_id,
            detected_at=anchor,
        )

        # 7. Coordinate downstream orchestration if triggered
        pipeline_res = None
        if incident is not None:
            if auto_orchestrate and self._orchestrator is not None:
                pipeline_res = self._orchestrator.process_incident(
                    incident=incident,
                    metrics=metrics,
                    payments=current_payments,
                    baseline_payments=baseline_payments,
                    merchant_id=merchant_id,
                    now=anchor,
                    on_progress=on_progress,
                )

            return LiveEvaluationResult(
                triggered=True,
                merchant_id=merchant_id,
                window=current_window,
                metrics=metrics,
                evaluated_at=anchor,
                current_payment_count=len(current_payments),
                baseline_payment_count=len(baseline_payments),
                incident=incident,
                pipeline_result=pipeline_res,
            )

        # 8. Normal healthy baseline / Insufficient data path
        return LiveEvaluationResult(
            triggered=False,
            merchant_id=merchant_id,
            window=current_window,
            metrics=metrics,
            evaluated_at=anchor,
            current_payment_count=len(current_payments),
            baseline_payment_count=len(baseline_payments),
            incident=None,
            pipeline_result=None,
        )
