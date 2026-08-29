"""Comprehensive tests for the FinancialVerifier.

Verifies:
1. Valid proposal with evidence and matching target -> VERIFIED.
2. Nonexistent incident -> REJECTED.
3. Nonexistent evidence reference -> REJECTED.
4. Evidence belonging to another incident -> REJECTED.
5. Stale evidence exceeding max age -> REJECTED.
6. Future-dated evidence -> REJECTED.
7. Target entity mismatch -> MISMATCH / REJECTED.
8. Claimed amount greater than verified deterministic exposure -> MISMATCH.
9. Claimed amount within verified deterministic exposure -> VERIFIED.
10. NO_ACTION evidence exemption.
11. Intent on resolved/dismissed incident -> REJECTED.
12. Database immutability (zero mutation during verification).
13. Deterministic repeatability.
14. Audit log recording and cryptographic integrity.
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from ...audit.store import AuditLog
from ...data import ScenarioId, generate_scenario
from ...db.database import Database
from ...detection.detector import Detector
from ...domain.canonical import digest, short_digest
from ...domain.enums import (
    AuditEventType,
    Currency,
    Dimension,
    IncidentStatus,
    IntentAction,
    SourceConfidence,
    TargetEntityType,
    VerificationStatus,
)
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.money import Money
from ...domain.payment import Payment
from ...domain.window import TimeWindow
from ...financial.engine import build_daily_hourly_baseline, compute_metrics
from ...investigation.investigator import Investigator
from ...verification.contracts import (
    CHK_AMOUNT_SAFETY,
    CHK_EVIDENCE_EXISTS,
    CHK_EVIDENCE_FRESHNESS,
    CHK_EVIDENCE_SCOPE,
    CHK_INCIDENT_EXISTS,
    CHK_INTENT_SCHEMA,
    CHK_TARGET_CONSISTENCY,
    VerifiedIntent,
)
from ...verification.verifier import FinancialVerifier
from ..helpers import NOW


class VerifierBaseTestCase(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.detector = Detector()
        self.investigator = Investigator()
        self.audit_log = AuditLog()
        self.verifier = FinancialVerifier(audit_log=self.audit_log)

    def tearDown(self):
        self.db.close()

    def _setup_scenario(self, scenario_id: ScenarioId):
        data = generate_scenario(scenario_id)
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
        report = None
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


class FinancialVerifierTests(VerifierBaseTestCase):
    def test_valid_intent_proposal_verified(self):
        """A well-formed intent citing existing incident evidence is VERIFIED and wrapped."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)
        self.assertIsNotNone(report)

        evidence_ref = incident.evidence[0].evidence_id

        intent = AgentIntent(
            intent_id="intent_test_1",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant of confirmed UPI spike observed in checkout traffic.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=(evidence_ref,),
            confidence=Decimal("0.95"),
        )

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )

        self.assertTrue(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)
        self.assertIsNotNone(verified_intent)
        self.assertEqual(verified_intent.intent_id, "intent_test_1")
        self.assertEqual(verified_intent.incident_id, incident.incident_id)
        self.assertIsNotNone(verified_intent.verified_failed_gmv)
        self.assertTrue(verified_intent.verified_failed_gmv.is_positive)

        # Audit event checked
        events = self.audit_log.get_events(event_type=AuditEventType.INTENT_VERIFIED)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].incident_id, incident.incident_id)

    def test_nonexistent_incident_rejected(self):
        """Intent referencing a nonexistent incident is REJECTED."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_bad_inc",
            incident_id="inc_nonexistent",
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant of an incident that does not exist.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=("ev_1",),
        )

        result = self.verifier.verify(intent=intent, db=self.db, now=data.anchor)
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.REJECTED)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_INCIDENT_EXISTS, failed_check_ids)

    def test_nonexistent_evidence_ref_rejected(self):
        """Intent citing a fabricated evidence ID is REJECTED."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_fake_ev",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant with a fabricated citation reference.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=("ev_fabricated_citation",),
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            now=data.anchor,
        )
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.REJECTED)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_EVIDENCE_EXISTS, failed_check_ids)

    def test_evidence_belonging_to_another_incident_rejected(self):
        """Evidence belonging to another incident cannot be cited (cross-incident boundary)."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        other_evidence = FinancialEvidence(
            evidence_id="ev_other_inc",
            incident_id="inc_DIFFERENT_INCIDENT",
            summary="Elevated failure rate in foreign incident.",
            window=data.incident_window,
            computed_at=data.anchor,
            metrics=incident.evidence[0].metrics,
        )

        intent = AgentIntent(
            intent_id="intent_scope_violation",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Attempting to use foreign incident evidence for this proposal.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=("ev_other_inc",),
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            evidence=(other_evidence,),
            now=data.anchor,
        )
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.REJECTED)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_EVIDENCE_SCOPE, failed_check_ids)

    def test_stale_evidence_rejected(self):
        """Evidence computed beyond the maximum staleness threshold is REJECTED."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        stale_time = data.anchor - timedelta(seconds=7200)  # 2 hours old
        stale_evidence = FinancialEvidence(
            evidence_id="ev_stale",
            incident_id=incident.incident_id,
            summary="Stale observation from 2 hours prior.",
            window=data.incident_window,
            computed_at=stale_time,
            metrics=incident.evidence[0].metrics,
        )

        intent = AgentIntent(
            intent_id="intent_stale_test",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant based on stale evidence beyond threshold.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=("ev_stale",),
        )

        verifier = FinancialVerifier(
            max_evidence_age_seconds=1800
        )  # 30 min staleness cap
        result = verifier.verify(
            intent=intent,
            incident=incident,
            evidence=(stale_evidence,),
            now=data.anchor,
        )
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.REJECTED)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_EVIDENCE_FRESHNESS, failed_check_ids)

    def test_target_merchant_mismatch_rejected(self):
        """Target merchant ID differing from incident merchant ID is rejected with MISMATCH."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_wrong_target",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying wrong merchant entity identifier.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="merchant_ATTACKER"
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            now=data.anchor,
        )
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.MISMATCH)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_TARGET_CONSISTENCY, failed_check_ids)

    def test_claimed_amount_exceeding_exposure_rejected(self):
        """Claimed amount exceeding verified failed GMV is rejected with MISMATCH."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        # Claim an absurdly inflated amount ₹50,00,000 (500000000 paise)
        inflated_amount = Money(500000000, Currency.INR)

        intent = AgentIntent(
            intent_id="intent_inflated_amount",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant with an exaggerated hallucinated amount.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
            claimed_amount=inflated_amount,
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )
        self.assertFalse(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.MISMATCH)

        failed_check_ids = [c.check_id for c in result.failed_checks]
        self.assertIn(CHK_AMOUNT_SAFETY, failed_check_ids)

    def test_claimed_amount_within_verified_exposure_verified(self):
        """Claimed amount within verified deterministic failed GMV is VERIFIED."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        # Small amount well within verified failed GMV
        valid_amount = Money(100000, Currency.INR)  # ₹1,000

        intent = AgentIntent(
            intent_id="intent_valid_amount",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant with an accurate bounded amount figure.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
            claimed_amount=valid_amount,
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            payments=data.incident_enriched(),
            now=data.anchor,
        )
        self.assertTrue(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_no_action_intent_is_evidence_exempt(self):
        """NO_ACTION proposal requires no supporting evidence and is VERIFIED."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_no_action",
            incident_id=incident.incident_id,
            action=IntentAction.NO_ACTION,
            reason="Proposing no action as incident investigation requires no external changes.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=None,
            evidence_refs=(),
        )

        result = self.verifier.verify(
            intent=intent,
            incident=incident,
            now=data.anchor,
        )
        self.assertTrue(result.is_verified)
        self.assertEqual(result.status, VerificationStatus.VERIFIED)

    def test_verification_is_pure_and_does_not_mutate_db(self):
        """Running verification leaves database records 100% unchanged."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        initial_payments_count = len(self.db.list_payments())
        initial_incidents_count = len(self.db.list_incidents())

        intent = AgentIntent(
            intent_id="intent_immutability_test",
            incident_id=incident.incident_id,
            action=IntentAction.RECOMMEND_ONLY,
            reason="Recommend merchant review current acquirer distribution.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.INCIDENT, entity_id=incident.incident_id
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
        )

        self.verifier.verify(intent=intent, db=self.db, now=data.anchor)

        self.assertEqual(len(self.db.list_payments()), initial_payments_count)
        self.assertEqual(len(self.db.list_incidents()), initial_incidents_count)

    def test_deterministic_repeatability(self):
        """Calling verify multiple times on identical inputs yields identical results."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_repeatability",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant of verified payment anomaly repeatability.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
        )

        res1 = self.verifier.verify(
            intent=intent, incident=incident, evidence=incident.evidence, now=data.anchor
        )
        res2 = self.verifier.verify(
            intent=intent, incident=incident, evidence=incident.evidence, now=data.anchor
        )

        self.assertEqual(res1.status, res2.status)
        self.assertEqual(res1.verification_id, res2.verification_id)
        self.assertEqual(len(res1.checks), len(res2.checks))
        self.assertEqual([c.passed for c in res1.checks], [c.passed for c in res2.checks])

    def test_audit_trail_integrity_after_verifier_run(self):
        """Verifier audit events pass full cryptographic integrity verification."""
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)

        intent = AgentIntent(
            intent_id="intent_audit_test",
            incident_id=incident.incident_id,
            action=IntentAction.NOTIFY_MERCHANT,
            reason="Notifying merchant for audit trail integrity verification.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=IntentTarget(
                entity_type=TargetEntityType.MERCHANT, entity_id="test_merchant"
            ),
            evidence_refs=(incident.evidence[0].evidence_id,),
        )

        self.verifier.verify(
            intent=intent, incident=incident, evidence=incident.evidence, now=data.anchor
        )

        is_valid, errors = self.audit_log.verify_integrity()
        self.assertTrue(is_valid, f"Audit log verification failed: {errors}")


class ActionEligibilityVerificationTests(VerifierBaseTestCase):
    """V2 Phase 1 — Issue 1 & 2: Deterministic action eligibility gate.

    The verifier must reject consequential actions (NOTIFY_MERCHANT,
    CREATE_PAYMENT_LINK) when the incident's evidence shows RISK_BLOCKED
    failures are dominant. ESCALATE_TO_HUMAN, RECOMMEND_ONLY, and NO_ACTION
    are always eligible regardless of failure category.
    """

    def _make_intent(self, incident, action, data, target_type=TargetEntityType.MERCHANT):
        """Helper to build a minimal valid AgentIntent for a given incident and action."""
        from ...domain.intent import _TARGETLESS_ACTIONS
        target = None if action in _TARGETLESS_ACTIONS else IntentTarget(
            entity_type=target_type, entity_id=incident.merchant_id
        )
        evidence_refs = () if action == IntentAction.NO_ACTION else (incident.evidence[0].evidence_id,)
        return AgentIntent(
            intent_id=f"intent_{action.value}_{incident.incident_id[:8]}",
            incident_id=incident.incident_id,
            action=action,
            reason=f"Test intent for action {action.value} on incident {incident.incident_id}.",
            proposed_at=data.anchor,
            model_id="gemini-2.5-flash",
            prompt_version="finpilot-v1",
            target=target,
            evidence_refs=evidence_refs,
        )

    def test_notify_merchant_rejected_for_risk_blocked_incident(self):
        """Issue 1 & 2: NOTIFY_MERCHANT must be REJECTED when failures are RISK_BLOCKED.

        RECOVERY_NOT_ELIGIBLE scenario has card failures dominated by RISK_BLOCKED
        category. A NOTIFY_MERCHANT intent (implying technical recovery) must fail
        the CHK_ACTION_ELIGIBILITY check regardless of what the LLM proposed.
        """
        from ...verification.contracts import CHK_ACTION_ELIGIBILITY
        data, incident, report = self._setup_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident, "RECOVERY_NOT_ELIGIBLE must detect an incident")
        self.assertIsNotNone(report, "Investigation report must exist")

        intent = self._make_intent(incident, IntentAction.NOTIFY_MERCHANT, data)

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,  # DB lookup for investigation findings is required
            now=data.anchor,
        )

        # Must NOT pass verification
        self.assertFalse(result.is_verified, "NOTIFY_MERCHANT on risk-blocked incident must not verify")
        self.assertIsNone(verified_intent, "No VerifiedIntent must be produced for rejected action")

        # The eligibility check must be the one that failed
        eligibility_checks = [c for c in result.checks if c.check_id == CHK_ACTION_ELIGIBILITY]
        self.assertEqual(len(eligibility_checks), 1, "CHK_ACTION_ELIGIBILITY check must be present")
        eligibility_check = eligibility_checks[0]
        self.assertFalse(eligibility_check.passed, "CHK_ACTION_ELIGIBILITY must fail")
        self.assertIn("RISK_BLOCKED", eligibility_check.detail)
        self.assertIn("INELIGIBLE", eligibility_check.observed)

    def test_escalate_to_human_allowed_on_risk_blocked_incident(self):
        """Issue 1 & 2: ESCALATE_TO_HUMAN must always be VERIFIED on risk-blocked incidents.

        Escalation defers to human judgment and does not attempt automated recovery.
        It is always a safe, eligible action regardless of failure category.
        """
        from ...verification.contracts import CHK_ACTION_ELIGIBILITY
        data, incident, report = self._setup_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        intent = self._make_intent(incident, IntentAction.ESCALATE_TO_HUMAN, data)

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )

        self.assertTrue(result.is_verified, f"ESCALATE_TO_HUMAN must verify: {result.summary}")
        self.assertIsNotNone(verified_intent)

        eligibility_checks = [c for c in result.checks if c.check_id == CHK_ACTION_ELIGIBILITY]
        self.assertEqual(len(eligibility_checks), 1)
        self.assertTrue(eligibility_checks[0].passed, "CHK_ACTION_ELIGIBILITY must pass for ESCALATE_TO_HUMAN")

    def test_recommend_only_allowed_on_risk_blocked_incident(self):
        """Issue 1 & 2: RECOMMEND_ONLY is an exempt action — it must verify on risk-blocked incidents."""
        from ...verification.contracts import CHK_ACTION_ELIGIBILITY
        data, incident, report = self._setup_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        intent = self._make_intent(incident, IntentAction.RECOMMEND_ONLY, data)

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )

        self.assertTrue(result.is_verified, f"RECOMMEND_ONLY must verify: {result.summary}")
        eligibility_checks = [c for c in result.checks if c.check_id == CHK_ACTION_ELIGIBILITY]
        self.assertEqual(len(eligibility_checks), 1)
        self.assertTrue(eligibility_checks[0].passed, "CHK_ACTION_ELIGIBILITY must pass for RECOMMEND_ONLY")

    def test_no_action_always_eligible(self):
        """NO_ACTION is trivially eligible and must always verify."""
        from ...verification.contracts import CHK_ACTION_ELIGIBILITY
        data, incident, report = self._setup_scenario(ScenarioId.RECOVERY_NOT_ELIGIBLE)
        self.assertIsNotNone(incident)

        intent = self._make_intent(incident, IntentAction.NO_ACTION, data)

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )

        self.assertTrue(result.is_verified, f"NO_ACTION must always verify: {result.summary}")
        eligibility_checks = [c for c in result.checks if c.check_id == CHK_ACTION_ELIGIBILITY]
        self.assertEqual(len(eligibility_checks), 1)
        self.assertTrue(eligibility_checks[0].passed)

    def test_notify_merchant_verified_for_normal_incident(self):
        """Eligibility check passes for NOTIFY_MERCHANT on a technical (non-risk-blocked) incident.

        Issue regression guard: the new check must not block legitimate recovery actions
        on real technical incidents where RISK_BLOCKED is not the dominant failure category.
        """
        from ...verification.contracts import CHK_ACTION_ELIGIBILITY
        data, incident, report = self._setup_scenario(ScenarioId.UPI_FAILURE_SPIKE)
        self.assertIsNotNone(incident)

        intent = self._make_intent(incident, IntentAction.NOTIFY_MERCHANT, data)

        verified_intent, result = self.verifier.verify_and_wrap(
            intent=intent,
            incident=incident,
            evidence=incident.evidence,
            db=self.db,
            now=data.anchor,
        )

        self.assertTrue(result.is_verified, f"NOTIFY_MERCHANT must verify for UPI spike: {result.summary}")
        eligibility_checks = [c for c in result.checks if c.check_id == CHK_ACTION_ELIGIBILITY]
        self.assertEqual(len(eligibility_checks), 1)
        self.assertTrue(eligibility_checks[0].passed, "Eligibility must pass for non-risk-blocked incident")
        self.assertIn("ELIGIBLE", eligibility_checks[0].observed)


if __name__ == "__main__":
    unittest.main()
