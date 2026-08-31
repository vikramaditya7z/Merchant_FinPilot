"""Unit and integration tests for Live Window Evaluator and Live Incident Detection."""

import io
import json
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from typing import List, Optional

from backend.agent.agent import FinancialAgent
from backend.agent.provider import LLMMessage, MockLLMProvider
from backend.api.app import FinPilotApp
from backend.api.router import FinancialIncidentAPI
from backend.application.contracts import PipelineStage, PipelineStatus
from backend.application.orchestrator import FinancialIncidentOrchestrator
from backend.audit.store import AuditLog
from backend.db.database import Database
from backend.detection.detector import Detector
from backend.detection.live_evaluator import LiveEvaluationResult, LiveWindowEvaluator
from backend.domain.enums import Currency, FailureCategory, IntentAction, PaymentMethod, PaymentStatus, SourceConfidence, TargetEntityType
from backend.domain.money import Money
from backend.domain.payment import EnrichedPayment, Payment, PaymentEnrichment
from backend.domain.window import UTC, TimeWindow
from backend.execution.adapters import SimulatedExecutionAdapter
from backend.execution.engine import ExecutionEngine
from backend.policy.engine import PolicyEngine
from backend.tools.registry import create_default_registry
from backend.verification.verifier import FinancialVerifier


def _create_sample_payment(
    p_id: str,
    dt: datetime,
    status: PaymentStatus = PaymentStatus.CAPTURED,
    method: PaymentMethod = PaymentMethod.UPI,
    amount_paise: int = 50000,
    error_code: Optional[str] = None,
    failure_category: Optional[FailureCategory] = None,
) -> EnrichedPayment:
    p = Payment(
        id=p_id,
        created_at=dt,
        amount=Money(amount_paise, Currency.INR),
        status=status,
        method=method,
        error_code=error_code if status == PaymentStatus.FAILED else None,
        error_description=error_code if status == PaymentStatus.FAILED else None,
        error_source="gateway" if status == PaymentStatus.FAILED else None,
        error_step="authorization" if status == PaymentStatus.FAILED else None,
        error_reason=error_code if status == PaymentStatus.FAILED else None,
    )
    enr = PaymentEnrichment(
        payment_id=p_id,
        region="South",
        provider="HDFC",
        segment="retail",
        failure_category=failure_category if status == PaymentStatus.FAILED else None,
        source_confidence=SourceConfidence.ENRICHED,
    )
    return EnrichedPayment(payment=p, enrichment=enr)


def _create_mock_agent(db: Database, audit_log: AuditLog) -> FinancialAgent:
    def handler(messages, tool_schemas):
        incidents = db.list_incidents()
        if incidents:
            latest_inc = incidents[-1]
            m_id = latest_inc.merchant_id
            ev_ids = [ev.evidence_id for ev in latest_inc.evidence] if latest_inc.evidence else ["ev_auto"]
        else:
            m_id = "merchant_default"
            ev_ids = ["ev_auto"]

        payload = {
            "reasoning": "Observed significant UPI failure concentration above historical baseline.",
            "verified_facts": ["UPI failure rate spiked above 25%."],
            "findings": [
                {
                    "title": "UPI Rail Degradation",
                    "dimension": "payment_method",
                    "observed_value": "upi",
                    "evidence_ref": ev_ids[0],
                    "summary": "Elevated UPI failure rate detected.",
                }
            ],
            "uncertainty_or_limitations": [],
            "proposed_intent": {
                "action": IntentAction.NOTIFY_MERCHANT.value,
                "target_type": TargetEntityType.MERCHANT.value,
                "target_id": m_id,
                "reason": "Notify merchant of severe UPI failure degradation.",
                "evidence_refs": ev_ids,
                "parameters": {"channels": "email,webhook"},
                "confidence": "0.95",
            },
        }
        return LLMMessage(
            role="model",
            content=f"```json\n{json.dumps(payload)}\n```",
        )

    mock_provider = MockLLMProvider(handler=handler)
    registry = create_default_registry()
    bound_tools = registry.bind(db)
    return FinancialAgent(provider=mock_provider, tools=bound_tools, audit_log=audit_log)


class TestLiveWindowEvaluator(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.verifier = FinancialVerifier(audit_log=self.audit_log)
        self.policy_engine = PolicyEngine(audit_log=self.audit_log)
        self.execution_adapter = SimulatedExecutionAdapter()
        self.execution_engine = ExecutionEngine(adapter=self.execution_adapter, audit_log=self.audit_log)
        self.detector = Detector()
        self.agent = _create_mock_agent(self.db, self.audit_log)

        self.orchestrator = FinancialIncidentOrchestrator(
            agent=self.agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            detector=self.detector,
            database=self.db,
            audit_log=self.audit_log,
        )

        self.evaluator = LiveWindowEvaluator(
            database=self.db,
            detector=self.detector,
            orchestrator=self.orchestrator,
        )

        # Baseline timestamp: 2026-08-26 13:00:00 UTC (Hour aligned)
        self.anchor = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.db.close()

    def _seed_historical_baseline(
        self, days: int = 7, tx_per_hour: int = 10, failure_rate: float = 0.05, before_hours: int = 1
    ) -> None:
        """Seed historical database payments for preceding days."""
        payments: List[EnrichedPayment] = []
        start_time = self.anchor - timedelta(days=days) - timedelta(hours=before_hours)
        current_time = start_time

        idx = 0
        while current_time < self.anchor - timedelta(hours=before_hours):
            for i in range(tx_per_hour):
                idx += 1
                is_fail = (i / tx_per_hour) < failure_rate
                status = PaymentStatus.FAILED if is_fail else PaymentStatus.CAPTURED
                payments.append(
                    _create_sample_payment(
                        p_id=f"hist_{idx}",
                        dt=current_time + timedelta(minutes=i * (60 // tx_per_hour)),
                        status=status,
                        method=PaymentMethod.UPI,
                        amount_paise=50000,
                        error_code="GATEWAY_ERROR:timeout" if is_fail else None,
                        failure_category=FailureCategory.TIMEOUT if is_fail else None,
                    )
                )
            current_time += timedelta(hours=1)

        self.db.save_payments(payments)

    def test_database_window_helpers(self) -> None:
        p1 = _create_sample_payment("db_1", datetime(2026, 8, 26, 10, 0, 0, tzinfo=UTC))
        p2 = _create_sample_payment("db_2", datetime(2026, 8, 26, 11, 0, 0, tzinfo=UTC))
        p3 = _create_sample_payment("db_3", datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC))
        self.db.save_payments([p1, p2, p3])

        latest = self.db.get_latest_payment_timestamp()
        self.assertEqual(latest, datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC))

        win = TimeWindow(datetime(2026, 8, 26, 10, 30, 0, tzinfo=UTC), datetime(2026, 8, 26, 12, 30, 0, tzinfo=UTC))
        in_win = self.db.list_payments_in_window(win)
        self.assertEqual([p.payment.id for p in in_win], ["db_2", "db_3"])

    def test_healthy_data_no_incident(self) -> None:
        # 1. Seed 7 days of normal baseline (5% failure rate)
        self._seed_historical_baseline(days=7, tx_per_hour=20, failure_rate=0.05)

        # 2. Seed current 1-hour window with normal traffic (5% failure rate)
        curr_window_start = self.anchor - timedelta(hours=1)
        curr_payments = []
        for i in range(100):
            is_fail = (i < 5)  # 5% failures
            status = PaymentStatus.FAILED if is_fail else PaymentStatus.CAPTURED
            curr_payments.append(
                _create_sample_payment(
                    p_id=f"curr_{i}",
                    dt=curr_window_start + timedelta(seconds=i * 30),
                    status=status,
                    method=PaymentMethod.UPI,
                )
            )
        self.db.save_payments(curr_payments)

        # 3. Evaluate live window
        result = self.evaluator.evaluate_window(
            merchant_id="merchant_test_healthy",
            now=self.anchor,
            window_hours=1,
            baseline_days=7,
        )

        self.assertFalse(result.triggered)
        self.assertIsNone(result.incident)
        self.assertIsNone(result.pipeline_result)
        self.assertEqual(result.current_payment_count, 100)
        self.assertGreater(result.baseline_payment_count, 1000)

    def test_failure_spike_incident_detected_and_orchestrated(self) -> None:
        # 1. Seed 7 days of normal baseline (4% failure rate)
        self._seed_historical_baseline(days=7, tx_per_hour=20, failure_rate=0.04)

        # 2. Seed current 1-hour window with severe spike (30% failure rate)
        curr_window_start = self.anchor - timedelta(hours=1)
        curr_payments = []
        for i in range(100):
            is_fail = (i < 30)  # 30% failures
            status = PaymentStatus.FAILED if is_fail else PaymentStatus.CAPTURED
            curr_payments.append(
                _create_sample_payment(
                    p_id=f"curr_spike_{i}",
                    dt=curr_window_start + timedelta(seconds=i * 30),
                    status=status,
                    method=PaymentMethod.UPI,
                    error_code="GATEWAY_ERROR:gateway_timeout" if is_fail else None,
                    failure_category=FailureCategory.TIMEOUT if is_fail else None,
                )
            )
        self.db.save_payments(curr_payments)

        # 3. Evaluate live window
        result = self.evaluator.evaluate_window(
            merchant_id="merchant_test_spike",
            now=self.anchor,
            window_hours=1,
            baseline_days=7,
            auto_orchestrate=True,
        )

        self.assertTrue(result.triggered)
        self.assertIsNotNone(result.incident)
        self.assertEqual(result.incident.merchant_id, "merchant_test_spike")
        self.assertIsNotNone(result.pipeline_result)
        self.assertTrue(result.pipeline_result.is_completed)
        self.assertEqual(result.pipeline_result.final_stage, PipelineStage.COMPLETED)

    def test_insufficient_data_no_incident(self) -> None:
        # 1. No baseline seeded, only 5 payments in current window
        curr_window_start = self.anchor - timedelta(hours=1)
        curr_payments = [
            _create_sample_payment(f"thin_{i}", curr_window_start + timedelta(minutes=i * 10))
            for i in range(5)
        ]
        self.db.save_payments(curr_payments)

        # 2. Evaluate live window
        result = self.evaluator.evaluate_window(
            merchant_id="merchant_test_thin",
            now=self.anchor,
            window_hours=1,
            baseline_days=7,
        )

        self.assertFalse(result.triggered)
        self.assertIsNone(result.incident)
        self.assertEqual(result.current_payment_count, 5)

    def test_baseline_and_current_window_configurations(self) -> None:
        # Test 14 days lookback vs 7 days lookback with 2-hour current window
        self._seed_historical_baseline(days=14, tx_per_hour=10, failure_rate=0.04, before_hours=2)
        curr_window_start = self.anchor - timedelta(hours=2)
        curr_payments = [
            _create_sample_payment(f"cfg_{i}", curr_window_start + timedelta(minutes=i))
            for i in range(120)
        ]
        self.db.save_payments(curr_payments)

        # Evaluate with 2 hour window and 14 days baseline
        res = self.evaluator.evaluate_window(
            merchant_id="merchant_cfg",
            now=self.anchor,
            window_hours=2,
            baseline_days=14,
            auto_orchestrate=False,
        )
        self.assertEqual(res.current_payment_count, 120)
        self.assertEqual(res.window.duration_seconds, 7200)
        self.assertGreater(res.baseline_payment_count, 2000)

    def test_deterministic_repeated_evaluation(self) -> None:
        # Seed baseline & spike
        self._seed_historical_baseline(days=7, tx_per_hour=20, failure_rate=0.04)
        curr_window_start = self.anchor - timedelta(hours=1)
        curr_payments = [
            _create_sample_payment(
                f"det_{i}",
                curr_window_start + timedelta(seconds=i * 30),
                status=PaymentStatus.FAILED if i < 25 else PaymentStatus.CAPTURED,
                error_code="GATEWAY_ERROR:timeout" if i < 25 else None,
                failure_category=FailureCategory.TIMEOUT if i < 25 else None,
            )
            for i in range(100)
        ]
        self.db.save_payments(curr_payments)

        # First evaluation
        res1 = self.evaluator.evaluate_window(
            merchant_id="merchant_det",
            now=self.anchor,
            auto_orchestrate=False,
        )

        # Second evaluation (repeated with same anchor timestamp)
        res2 = self.evaluator.evaluate_window(
            merchant_id="merchant_det",
            now=self.anchor,
            auto_orchestrate=False,
        )

        self.assertEqual(res1.triggered, res2.triggered)
        self.assertEqual(res1.metrics.failure_rate, res2.metrics.failure_rate)
        self.assertEqual(res1.metrics.deviation.absolute_percentage_points, res2.metrics.deviation.absolute_percentage_points)
        self.assertEqual(res1.incident.incident_key, res2.incident.incident_key)


class TestLiveEvaluationHTTPAPI(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.audit_log = AuditLog()
        self.verifier = FinancialVerifier(audit_log=self.audit_log)
        self.policy_engine = PolicyEngine(audit_log=self.audit_log)
        self.execution_adapter = SimulatedExecutionAdapter()
        self.execution_engine = ExecutionEngine(adapter=self.execution_adapter, audit_log=self.audit_log)
        self.detector = Detector()
        self.agent = _create_mock_agent(self.db, self.audit_log)

        self.orchestrator = FinancialIncidentOrchestrator(
            agent=self.agent,
            verifier=self.verifier,
            policy_engine=self.policy_engine,
            execution_engine=self.execution_engine,
            detector=self.detector,
            database=self.db,
            audit_log=self.audit_log,
        )

        self.api = FinancialIncidentAPI(
            orchestrator=self.orchestrator,
            database=self.db,
            audit_log=self.audit_log,
        )

        self.app = FinPilotApp(self.api)
        self.anchor = datetime(2026, 8, 26, 13, 0, 0, tzinfo=UTC)

    def tearDown(self) -> None:
        self.db.close()

    def test_http_endpoint_healthy_evaluation(self) -> None:
        # Seed 10 payments
        curr_window_start = self.anchor - timedelta(hours=1)
        curr_payments = [
            _create_sample_payment(f"http_p_{i}", curr_window_start + timedelta(minutes=i * 5))
            for i in range(10)
        ]
        self.db.save_payments(curr_payments)

        status_code, body = self.api.handle_evaluate_live({
            "merchant_id": "merchant_api_test",
            "now": self.anchor.isoformat(),
            "window_hours": 1,
            "baseline_days": 7,
            "auto_orchestrate": True,
        })

        self.assertEqual(status_code, 200)
        self.assertIn("triggered", body)
        self.assertFalse(body["triggered"])
        self.assertEqual(body["merchant_id"], "merchant_api_test")
        self.assertIn("metrics", body)
        self.assertIsNone(body["incident"])

    def test_wsgi_app_post_evaluate_live(self) -> None:
        # Test calling WSGI app directly
        body_json = json.dumps({
            "merchant_id": "merchant_wsgi_test",
            "now": self.anchor.isoformat(),
            "window_hours": 1,
            "baseline_days": 7,
        }).encode("utf-8")

        response_status = None
        response_headers = None

        def start_response(status, headers):
            nonlocal response_status, response_headers
            response_status = status
            response_headers = headers

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/v1/incidents/evaluate-live",
            "CONTENT_LENGTH": str(len(body_json)),
            "wsgi.input": io.BytesIO(body_json),
        }

        resp_body_bytes = b"".join(self.app(environ, start_response))
        self.assertEqual(response_status, "200 OK")
        data = json.loads(resp_body_bytes.decode("utf-8"))
        self.assertIn("triggered", data)
        self.assertEqual(data["merchant_id"], "merchant_wsgi_test")

    def test_http_endpoint_invalid_payload_handling(self) -> None:
        # Invalid merchant_id
        code, body = self.api.handle_evaluate_live({"merchant_id": "   "})
        self.assertEqual(code, 400)
        self.assertIn("error", body)

        # Invalid window_hours
        code, body = self.api.handle_evaluate_live({"merchant_id": "valid", "window_hours": -5})
        self.assertEqual(code, 400)


if __name__ == "__main__":
    unittest.main()
