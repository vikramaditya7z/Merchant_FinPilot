"""Tests for the Agent Tool Surface.

Verifies:
1. get_incident_summary (success, not found, invalid arg).
2. get_failure_breakdown (valid dimensions, invalid dimensions, structured slices).
3. get_time_series (granularity validation, deterministic bucket sums).
4. get_baseline_comparison (overall and slice-level baseline metrics).
5. get_revenue_exposure (integer paise, excess count, recoverability check).
6. check_action_eligibility (deterministic eligibility, risk-blocked constraint, unknown slice rejection).
7. ToolRegistry (schema generation, safe dispatch, error trapping).
8. Security & read-only invariants (no DB mutations, rejection of injection payloads).
"""

import unittest
from datetime import datetime
from decimal import Decimal

from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.enums import Dimension, FailureCategory, PaymentMethod, Severity
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...tools.contracts import ToolErrorCode, ToolResult
from ...tools.incident_tools import (
    check_action_eligibility,
    get_baseline_comparison,
    get_failure_breakdown,
    get_incident_summary,
    get_revenue_exposure,
    get_time_series,
)
from ...tools.registry import ToolRegistry, create_default_registry


class BaseToolTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.registry = create_default_registry()

    def tearDown(self):
        self.db.close()

    def _seed_scenario(self, scenario_id: ScenarioId):
        data = generate_scenario(scenario_id)
        # Save payments to DB
        self.db.save_payments(data.agent_enriched())

        buckets = build_daily_hourly_baseline(
            data.agent_enriched(), data.incident_window, data.spec.baseline_days
        )
        metrics = compute_metrics(
            data.agent_enriched(),
            data.incident_window,
            data.anchor,
            baseline_windows=buckets,
        )

        incident = self.detector.detect(metrics, merchant_id="test_merchant")
        if incident is not None:
            self.db.save_incident(incident)
            report = self.investigator.investigate(
                incident=incident,
                payments=data.incident_enriched(),
                baseline_payments=data.baseline_enriched(),
                now=data.anchor,
            )
            self.db.save_investigation(report)
            return data, incident, report
        return data, None, None


class IncidentSummaryToolTests(BaseToolTestCase):
    def test_get_incident_summary_success(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        res = get_incident_summary(self.db, incident.incident_id)
        self.assertTrue(res.success)
        d = res.data

        self.assertEqual(d["incident_id"], incident.incident_id)
        self.assertEqual(d["merchant_id"], "test_merchant")
        self.assertEqual(d["severity"], "high")
        self.assertEqual(d["traffic"]["failed"], incident.metrics.counts.failed)
        self.assertGreater(d["traffic"]["total_transactions"], 100)
        self.assertEqual(d["revenue_risk"]["currency"], "INR")
        self.assertIsInstance(d["revenue_risk"]["failed_gmv_paise"], int)
        self.assertGreater(d["revenue_risk"]["failed_gmv_paise"], 0)
        self.assertTrue(len(d["attached_evidence_ids"]) > 0)

    def test_get_incident_summary_not_found(self):
        res = get_incident_summary(self.db, "inc_nonexistent_999")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.NOT_FOUND)

    def test_get_incident_summary_invalid_argument(self):
        res = get_incident_summary(self.db, "   ")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.INVALID_ARGUMENT)


class FailureBreakdownToolTests(BaseToolTestCase):
    def test_get_failure_breakdown_payment_method(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = get_failure_breakdown(self.db, incident.incident_id, "payment_method")

        self.assertTrue(res.success)
        d = res.data
        self.assertEqual(d["dimension"], "payment_method")
        self.assertGreater(d["total_slices"], 0)

        # Look for UPI slice
        upi_slice = next((s for s in d["slices"] if s["value"] == "upi"), None)
        self.assertIsNotNone(upi_slice)
        self.assertEqual(upi_slice["failed_count"], 44)
        self.assertGreater(float(upi_slice["share_of_failures"]), 0.8)
        self.assertEqual(upi_slice["evidence_strength"], "strong_evidence")

    def test_get_failure_breakdown_provider_enriched(self):
        data, incident, _ = self._seed_scenario(ScenarioId.PROVIDER_FAILURE)
        res = get_failure_breakdown(self.db, incident.incident_id, "provider")

        self.assertTrue(res.success)
        d = res.data
        acq_b = next((s for s in d["slices"] if s["value"] == "acquirer_b"), None)
        self.assertIsNotNone(acq_b)
        self.assertEqual(acq_b["source_confidence"], "enriched")
        self.assertEqual(acq_b["evidence_strength"], "strong_evidence")

    def test_get_failure_breakdown_invalid_dimension(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = get_failure_breakdown(self.db, incident.incident_id, "invalid_dimension_xyz")
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.INVALID_ARGUMENT)
        self.assertIn("Supported dimensions", res.error_message)


class TimeSeriesToolTests(BaseToolTestCase):
    def test_get_time_series_success(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = get_time_series(self.db, incident.incident_id, granularity_minutes=15)

        self.assertTrue(res.success)
        d = res.data
        self.assertEqual(d["granularity_minutes"], 15)
        # 1-hour window divided into 15m intervals yields 4 buckets
        self.assertEqual(d["bucket_count"], 4)

        total_tx = sum(b["total_transactions"] for b in d["buckets"])
        self.assertEqual(total_tx, incident.metrics.counts.total)

        total_failed_paise = sum(b["failed_gmv_paise"] for b in d["buckets"])
        self.assertEqual(total_failed_paise, incident.metrics.revenue_risk.failed_gmv.minor_units)

    def test_get_time_series_invalid_granularity(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        # Less than 5 min
        res1 = get_time_series(self.db, incident.incident_id, granularity_minutes=1)
        self.assertFalse(res1.success)
        self.assertEqual(res1.error_code, ToolErrorCode.INVALID_ARGUMENT)

        # More than 60 min
        res2 = get_time_series(self.db, incident.incident_id, granularity_minutes=120)
        self.assertFalse(res2.success)
        self.assertEqual(res2.error_code, ToolErrorCode.INVALID_ARGUMENT)


class BaselineComparisonToolTests(BaseToolTestCase):
    def test_get_baseline_comparison_overall(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = get_baseline_comparison(self.db, incident.incident_id)

        self.assertTrue(res.success)
        d = res.data
        self.assertIsNotNone(d["current_failure_rate_percent"])
        self.assertIsNotNone(d["baseline_failure_rate_percent"])
        self.assertIsNotNone(d["deviation_percentage_points"])
        self.assertIsNotNone(d["relative_lift"])
        self.assertIsNotNone(d["significance"])

    def test_get_baseline_comparison_specific_slice(self):
        data, incident, _ = self._seed_scenario(ScenarioId.REGIONAL_FAILURE)
        res = get_baseline_comparison(
            self.db, incident.incident_id, dimension="region", dimension_value="IN-KA"
        )

        self.assertTrue(res.success)
        d = res.data
        self.assertEqual(d["dimension"], "region")
        self.assertEqual(d["value"], "IN-KA")
        self.assertEqual(d["evidence_strength"], "strong_evidence")
        self.assertGreater(float(d["relative_lift"]), 3.0)


class RevenueExposureToolTests(BaseToolTestCase):
    def test_get_revenue_exposure_standard(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = get_revenue_exposure(self.db, incident.incident_id)

        self.assertTrue(res.success)
        d = res.data
        self.assertGreater(d["failed_gmv_paise"], 0)
        self.assertGreater(d["excess_failed_transactions"], 0)
        self.assertGreater(d["revenue_at_risk_paise"], 0)
        self.assertEqual(d["currency"], "INR")
        self.assertTrue(d["is_recoverable"])

    def test_get_revenue_exposure_risk_blocked_non_recoverable(self):
        data, incident, _ = self._seed_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        res = get_revenue_exposure(self.db, incident.incident_id)

        self.assertTrue(res.success)
        d = res.data
        self.assertFalse(d["is_recoverable"])
        self.assertIn("risk-blocked", d["recoverability_notes"])


class ActionEligibilityToolTests(BaseToolTestCase):
    def test_route_update_eligible_on_provider_failure(self):
        data, incident, _ = self._seed_scenario(ScenarioId.PROVIDER_FAILURE)
        res = check_action_eligibility(
            self.db,
            incident.incident_id,
            action_type="ROUTE_UPDATE",
            target_dimension="provider",
            target_value="acquirer_b",
        )

        self.assertTrue(res.success)
        d = res.data
        self.assertTrue(d["eligible"])
        self.assertEqual(d["evidence_strength"], "strong_evidence")
        self.assertGreater(d["estimated_risk_mitigation_paise"], 0)

    def test_route_update_ineligible_on_healthy_provider(self):
        data, incident, _ = self._seed_scenario(ScenarioId.PROVIDER_FAILURE)
        res = check_action_eligibility(
            self.db,
            incident.incident_id,
            action_type="ROUTE_UPDATE",
            target_dimension="provider",
            target_value="acquirer_c",
        )

        self.assertTrue(res.success)
        d = res.data
        self.assertFalse(d["eligible"])
        self.assertIn("does not show concentrated degradation", d["reason"])

    def test_route_update_ineligible_on_risk_blocked_incident(self):
        data, incident, _ = self._seed_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        res = check_action_eligibility(
            self.db,
            incident.incident_id,
            action_type="ROUTE_UPDATE",
            target_dimension="payment_method",
            target_value="card",
        )

        self.assertTrue(res.success)
        d = res.data
        self.assertFalse(d["eligible"])
        self.assertIn("Razorpay risk engine", d["reason"])

    def test_invalid_action_type_returns_error(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = check_action_eligibility(
            self.db, incident.incident_id, action_type="ARBITRARY_TRANSFER_MONEY"
        )
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.INVALID_ARGUMENT)


class ToolRegistryAndSecurityTests(BaseToolTestCase):
    def test_all_expected_tools_registered(self):
        tool_names = self.registry.list_tools()
        expected = [
            "check_action_eligibility",
            "get_baseline_comparison",
            "get_failure_breakdown",
            "get_incident_summary",
            "get_revenue_exposure",
            "get_time_series",
        ]
        self.assertEqual(tool_names, expected)

    def test_schemas_conform_to_function_calling_format(self):
        schemas = self.registry.get_schemas()
        self.assertEqual(len(schemas), 6)
        for s in schemas:
            self.assertIn("name", s)
            self.assertIn("description", s)
            self.assertIn("parameters", s)
            self.assertEqual(s["parameters"]["type"], "object")
            self.assertIn("properties", s["parameters"])
            self.assertIn("required", s["parameters"])

    def test_dispatch_via_registry(self):
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        res = self.registry.execute(
            self.db, "get_incident_summary", {"incident_id": incident.incident_id}
        )
        self.assertTrue(res.success)
        self.assertEqual(res.data["incident_id"], incident.incident_id)

    def test_dispatch_unknown_tool_returns_not_found(self):
        res = self.registry.execute(self.db, "execute_sql_query", {"query": "SELECT * FROM payments"})
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.NOT_FOUND)

    def test_read_only_invariance(self):
        """Calling all tools leaves all database tables 100% unchanged."""
        data, incident, _ = self._seed_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        # Record initial row counts
        initial_payments = len(self.db.list_payments())
        initial_incidents = len(self.db.list_incidents())
        initial_audit = len(self.db.list_audit_events())

        # Execute all 6 tools
        self.registry.execute(self.db, "get_incident_summary", {"incident_id": incident.incident_id})
        self.registry.execute(self.db, "get_failure_breakdown", {"incident_id": incident.incident_id, "dimension": "payment_method"})
        self.registry.execute(self.db, "get_time_series", {"incident_id": incident.incident_id, "granularity_minutes": 15})
        self.registry.execute(self.db, "get_baseline_comparison", {"incident_id": incident.incident_id})
        self.registry.execute(self.db, "get_revenue_exposure", {"incident_id": incident.incident_id})
        self.registry.execute(self.db, "check_action_eligibility", {"incident_id": incident.incident_id, "action_type": "MERCHANT_NOTIFICATION"})

        # Row counts must be identical
        self.assertEqual(len(self.db.list_payments()), initial_payments)
        self.assertEqual(len(self.db.list_incidents()), initial_incidents)
        self.assertEqual(len(self.db.list_audit_events()), initial_audit)

    def test_sql_injection_payload_is_harmless(self):
        """SQL injection payloads in arguments are rejected or safely looked up without executing SQL."""
        malicious_id = "inc_1' OR '1'='1"
        res = self.registry.execute(self.db, "get_incident_summary", {"incident_id": malicious_id})
        self.assertFalse(res.success)
        self.assertEqual(res.error_code, ToolErrorCode.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
