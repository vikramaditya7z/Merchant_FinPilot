"""Deterministic Financial Verifier.

PROJECT_RULES 1.3, 1.4, 8.5, 8.6, 8.7 / ARCHITECTURE.md §10.

Core Responsibilities:
- Independent re-derivation of agent proposals against deterministic source facts.
- Strict rejection on missing/tampered/stale evidence, scope widening, target mismatch, or amount inflation.
- Produces immutable VerificationResult and VerifiedIntent containers.
- Zero capability to execute actions, modify payments, or mutate financial state.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..audit.store import AuditLog
from ..domain.canonical import digest, short_digest
from ..domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    Dimension,
    FailureCategory,
    IncidentStatus,
    IntentAction,
    PaymentOutcome,
    PaymentStatus,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
)
from ..domain.errors import DomainValidationError
from ..domain.incident import FinancialEvidence, FinancialIncident
from ..domain.intent import AgentIntent, IntentTarget
from ..domain.money import Money
from ..domain.payment import Payment
from ..domain.verification import VerificationCheck, VerificationResult
from ..domain.window import require_utc
from .contracts import (
    CHK_ACTION_ELIGIBILITY,
    CHK_ACTION_PRECONDITIONS,
    CHK_ACTION_SUPPORTED,
    CHK_AMOUNT_SAFETY,
    CHK_EVIDENCE_EXISTS,
    CHK_EVIDENCE_FRESHNESS,
    CHK_EVIDENCE_INTEGRITY,
    CHK_EVIDENCE_SCOPE,
    CHK_INCIDENT_ACTIVE,
    CHK_INCIDENT_EXISTS,
    CHK_INTENT_SCHEMA,
    CHK_TARGET_CONSISTENCY,
    VerifiedIntent,
)


class FinancialVerifier:
    """Deterministic verifier for AgentIntent proposals."""

    DEFAULT_MAX_EVIDENCE_AGE_SECONDS = 86400  # 24 hours

    def __init__(
        self,
        max_evidence_age_seconds: int = DEFAULT_MAX_EVIDENCE_AGE_SECONDS,
        audit_log: Optional[AuditLog] = None,
    ) -> None:
        if max_evidence_age_seconds <= 0:
            raise DomainValidationError("max_evidence_age_seconds must be positive")
        self._max_evidence_age_seconds = max_evidence_age_seconds
        self._audit_log = audit_log

    def verify(
        self,
        intent: AgentIntent,
        incident: Optional[FinancialIncident] = None,
        evidence: Optional[Sequence[FinancialEvidence]] = None,
        payments: Optional[Sequence[Payment]] = None,
        db: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> VerificationResult:
        """Deterministically verify an AgentIntent proposal against source records.

        Args:
            intent: The proposed AgentIntent to verify.
            incident: The FinancialIncident context (optional if db provided).
            evidence: Explicit list of FinancialEvidence items (optional if db provided).
            payments: Source payment records for independent re-derivation (optional).
            db: Database repository for record resolution.
            now: Current timestamp injection (aware UTC).

        Returns:
            A strongly typed VerificationResult with every evaluated check recorded.
        """
        if not isinstance(intent, AgentIntent):
            raise DomainValidationError("FinancialVerifier requires an AgentIntent instance")

        when = require_utc(now) if now is not None else datetime.now().astimezone()
        checks: List[VerificationCheck] = []

        # 1. CHECK_INTENT_SCHEMA: Schema & Content Hash Integrity
        schema_passed = True
        schema_detail = "Intent schema and content hash valid."
        try:
            expected_hash = digest(intent.canonical_form())
            if intent.content_hash() != expected_hash:
                schema_passed = False
                schema_detail = f"Content hash mismatch: {intent.content_hash()} != {expected_hash}"
            elif len(intent.reason.strip()) < 20:
                schema_passed = False
                schema_detail = "Reason length too short (< 20 characters)"
        except Exception as e:
            schema_passed = False
            schema_detail = f"Schema validation error: {e}"

        checks.append(
            VerificationCheck(
                check_id=CHK_INTENT_SCHEMA,
                name="Intent Schema & Content Hash",
                passed=schema_passed,
                expected="Valid AgentIntent with verified canonical content hash",
                observed=intent.content_hash() if schema_passed else "INVALID",
                detail=schema_detail,
            )
        )

        # Resolve Incident
        resolved_incident = incident
        if resolved_incident is None and db is not None:
            resolved_incident = db.get_incident(intent.incident_id)

        # 2. CHECK_INCIDENT_EXISTS: Referenced Incident Exists
        inc_exists_passed = (
            resolved_incident is not None
            and resolved_incident.incident_id == intent.incident_id
        )
        checks.append(
            VerificationCheck(
                check_id=CHK_INCIDENT_EXISTS,
                name="Referenced Incident Exists",
                passed=inc_exists_passed,
                expected=f"Incident '{intent.incident_id}' exists in store",
                observed=resolved_incident.incident_id if inc_exists_passed else "NOT_FOUND",
                detail=(
                    f"Resolved incident {resolved_incident.incident_id}"
                    if inc_exists_passed
                    else f"Incident '{intent.incident_id}' not found"
                ),
            )
        )

        # 3. CHECK_INCIDENT_ACTIVE: Incident Status is Active
        if resolved_incident is not None:
            is_active = resolved_incident.status not in (
                IncidentStatus.RESOLVED,
                IncidentStatus.DISMISSED,
            )
            checks.append(
                VerificationCheck(
                    check_id=CHK_INCIDENT_ACTIVE,
                    name="Incident Status Active",
                    passed=is_active,
                    expected="Incident status is active (not RESOLVED or DISMISSED)",
                    observed=resolved_incident.status.value,
                    detail=f"Current lifecycle status: {resolved_incident.status.value}",
                )
            )

        # 4. CHECK_ACTION_SUPPORTED: Action is Supported
        supported_actions = frozenset(
            {
                IntentAction.NO_ACTION,
                IntentAction.NOTIFY_MERCHANT,
                IntentAction.RECOMMEND_ONLY,
                IntentAction.CREATE_PAYMENT_LINK,
                IntentAction.ESCALATE_TO_HUMAN,
            }
        )
        action_supported = intent.action in supported_actions
        checks.append(
            VerificationCheck(
                check_id=CHK_ACTION_SUPPORTED,
                name="Action Supported",
                passed=action_supported,
                expected=f"Action in {[a.value for a in supported_actions]}",
                observed=intent.action.value,
                detail=f"Action {intent.action.value} is supported",
            )
        )

        # Resolve Evidence
        evidence_pool: Dict[str, FinancialEvidence] = {}
        if evidence:
            for ev in evidence:
                evidence_pool[ev.evidence_id] = ev
        if resolved_incident and resolved_incident.evidence:
            for ev in resolved_incident.evidence:
                evidence_pool[ev.evidence_id] = ev
        if db is not None:
            inv = db.get_investigation(intent.incident_id)
            if inv and inv.evidence:
                for ev in inv.evidence:
                    evidence_pool[ev.evidence_id] = ev

        # 5. CHECK_EVIDENCE_EXISTS: Cited Evidence Exists
        if intent.action is IntentAction.NO_ACTION:
            checks.append(
                VerificationCheck(
                    check_id=CHK_EVIDENCE_EXISTS,
                    name="Evidence Resolution",
                    passed=True,
                    expected="NO_ACTION is evidence-exempt",
                    observed="EXEMPT",
                    detail="NO_ACTION requires no supporting evidence",
                )
            )
        else:
            missing_refs = [
                ref for ref in intent.evidence_refs if ref not in evidence_pool
            ]
            ev_exists = len(missing_refs) == 0 and len(intent.evidence_refs) > 0
            checks.append(
                VerificationCheck(
                    check_id=CHK_EVIDENCE_EXISTS,
                    name="Evidence Resolution",
                    passed=ev_exists,
                    expected=f"All {len(intent.evidence_refs)} cited evidence refs exist",
                    observed=(
                        f"Resolved {len(intent.evidence_refs)} refs"
                        if ev_exists
                        else f"Missing: {missing_refs}"
                    ),
                    detail=(
                        "All evidence IDs resolved successfully"
                        if ev_exists
                        else f"Failed to resolve evidence IDs: {missing_refs}"
                    ),
                )
            )

        # 6. CHECK_EVIDENCE_SCOPE: Evidence Scope Matches Incident Scope
        scoped_evidence: List[FinancialEvidence] = [
            evidence_pool[ref]
            for ref in intent.evidence_refs
            if ref in evidence_pool
        ]
        out_of_scope_refs = [
            ev.evidence_id
            for ev in scoped_evidence
            if ev.incident_id != intent.incident_id
        ]
        scope_passed = len(out_of_scope_refs) == 0
        checks.append(
            VerificationCheck(
                check_id=CHK_EVIDENCE_SCOPE,
                name="Evidence Scope Boundary",
                passed=scope_passed,
                expected=f"All evidence belongs to incident '{intent.incident_id}'",
                observed=(
                    "ALL_SCOPED"
                    if scope_passed
                    else f"Cross-incident refs: {out_of_scope_refs}"
                ),
                detail=(
                    "Evidence scope verified within incident boundary"
                    if scope_passed
                    else f"Evidence IDs {out_of_scope_refs} belong to another incident"
                ),
            )
        )

        # 7. CHECK_EVIDENCE_FRESHNESS: Evidence Freshness
        stale_refs = []
        for ev in scoped_evidence:
            if not ev.is_fresh_at(when, self._max_evidence_age_seconds):
                stale_refs.append(ev.evidence_id)

        freshness_passed = len(stale_refs) == 0
        checks.append(
            VerificationCheck(
                check_id=CHK_EVIDENCE_FRESHNESS,
                name="Evidence Freshness",
                passed=freshness_passed,
                expected=f"Evidence computed within last {self._max_evidence_age_seconds}s and not in future",
                observed="FRESH" if freshness_passed else f"Stale/Future: {stale_refs}",
                detail=(
                    "All cited evidence is fresh"
                    if freshness_passed
                    else f"Stale or future-dated evidence: {stale_refs}"
                ),
            )
        )

        # 8. CHECK_EVIDENCE_INTEGRITY: Evidence Carries Valid Deterministic Results
        integrity_passed = True
        integrity_detail = "Evidence data structures valid."
        for ev in scoped_evidence:
            if ev.metrics is None and ev.breakdown is None:
                integrity_passed = False
                integrity_detail = f"Evidence {ev.evidence_id} carries no deterministic metrics or breakdown"
                break

        checks.append(
            VerificationCheck(
                check_id=CHK_EVIDENCE_INTEGRITY,
                name="Evidence Deterministic Integrity",
                passed=integrity_passed,
                expected="Evidence contains deterministic FinancialMetrics or DimensionBreakdown",
                observed="VALID" if integrity_passed else "INVALID",
                detail=integrity_detail,
            )
        )

        # 9. CHK_ACTION_ELIGIBILITY: Action Compatible With Incident Failure Category
        # Deterministically re-derives whether the incident permits the proposed action.
        # Risk-blocked failures are intentional refusals by the risk engine — re-offering
        # or routing payment on these is a compliance violation, not a recovery action.
        # This gate is independent of what the LLM proposed; it checks the evidence directly.
        eligibility_passed = True
        eligibility_detail = "Action is compatible with incident failure profile."

        # Only enforce for consequential actions that imply recovery/re-offer semantics.
        # ESCALATE_TO_HUMAN and RECOMMEND_ONLY are always eligible — they do not attempt
        # to route or re-offer payments; they defer to humans or just inform.
        # NO_ACTION is trivially eligible.
        _ELIGIBILITY_EXEMPT = frozenset({
            IntentAction.NO_ACTION,
            IntentAction.ESCALATE_TO_HUMAN,
            IntentAction.RECOMMEND_ONLY,
        })

        if intent.action not in _ELIGIBILITY_EXEMPT:
            # Walk evidence breakdowns for the incident to find dominant failure category.
            # We inspect the incident's own evidence (already scope-checked above) plus
            # any additional evidence passed into the verifier.
            _all_evidence = list(scoped_evidence)

            is_risk_blocked_dominant = False
            for ev in _all_evidence:
                if ev.breakdown is not None and ev.breakdown.dimension == Dimension.FAILURE_CATEGORY:
                    total_failed = ev.breakdown.total_counts.failed
                    if total_failed > 0:
                        for sl in ev.breakdown.slices:
                            if sl.value == FailureCategory.RISK_BLOCKED.value:
                                share = sl.counts.failed / total_failed
                                # Risk-blocked is dominant if it accounts for ≥50% of failures.
                                if share >= 0.5:
                                    is_risk_blocked_dominant = True
                                    break
                if is_risk_blocked_dominant:
                    break

            # Also check the incident's attached evidence directly (covers cases where
            # only the incident is provided, not the separate evidence list).
            if not is_risk_blocked_dominant and resolved_incident is not None:
                for ev in resolved_incident.evidence:
                    if ev.breakdown is not None and ev.breakdown.dimension == Dimension.FAILURE_CATEGORY:
                        total_failed = ev.breakdown.total_counts.failed
                        if total_failed > 0:
                            for sl in ev.breakdown.slices:
                                if sl.value == FailureCategory.RISK_BLOCKED.value:
                                    share = sl.counts.failed / total_failed
                                    if share >= 0.5:
                                        is_risk_blocked_dominant = True
                                        break
                    if is_risk_blocked_dominant:
                        break

            # Primary path: consult the investigation report stored in the DB.
            # The orchestrator saves the investigation before calling the verifier, so
            # this lookup is always fresh. Investigation primary findings carry verified
            # failure-category attribution computed by the Investigator — the most
            # authoritative source of failure cause available to the verifier.
            if not is_risk_blocked_dominant and db is not None and resolved_incident is not None:
                investigation = db.get_investigation(resolved_incident.incident_id)
                if investigation is not None:
                    for finding in investigation.primary_findings:
                        if (finding.dimension is Dimension.FAILURE_CATEGORY
                                and finding.value == FailureCategory.RISK_BLOCKED.value):
                            is_risk_blocked_dominant = True
                            break

            if is_risk_blocked_dominant:
                eligibility_passed = False
                eligibility_detail = (
                    f"Action '{intent.action.value}' is ineligible: incident failures are dominated "
                    "by RISK_BLOCKED category (≥50% of failures). Risk engine refusals are intentional "
                    "compliance decisions and cannot be bypassed by automated recovery actions. "
                    "Only ESCALATE_TO_HUMAN or RECOMMEND_ONLY are permitted for risk-blocked incidents."
                )

        checks.append(
            VerificationCheck(
                check_id=CHK_ACTION_ELIGIBILITY,
                name="Action Eligibility For Incident Profile",
                passed=eligibility_passed,
                expected="Action must be compatible with the incident's verified failure category",
                observed="ELIGIBLE" if eligibility_passed else "INELIGIBLE",
                detail=eligibility_detail,
            )
        )

        # 10. CHECK_TARGET_CONSISTENCY: Target Entity Matches Incident/Evidence Scope

        target_passed = True
        target_detail = "Target entity matches scope."
        if intent.target is not None:
            if intent.target.entity_type == TargetEntityType.INCIDENT:
                if intent.target.entity_id != intent.incident_id:
                    target_passed = False
                    target_detail = (
                        f"Target incident '{intent.target.entity_id}' does not match "
                        f"intent incident '{intent.incident_id}'"
                    )
            elif intent.target.entity_type == TargetEntityType.MERCHANT:
                if resolved_incident and resolved_incident.merchant_id:
                    if intent.target.entity_id != resolved_incident.merchant_id:
                        target_passed = False
                        target_detail = (
                            f"Target merchant '{intent.target.entity_id}' does not match "
                            f"incident merchant '{resolved_incident.merchant_id}'"
                        )
            elif intent.target.entity_type == TargetEntityType.PAYMENT:
                if payments:
                    target_payment = next(
                        (
                            p.payment if hasattr(p, "payment") else p
                            for p in payments
                            if (p.payment.id if hasattr(p, "payment") else p.id)
                            == intent.target.entity_id
                        ),
                        None,
                    )
                    if target_payment is None:
                        target_passed = False
                        target_detail = (
                            f"Target payment '{intent.target.entity_id}' not found in incident payments"
                        )
                    else:
                        err_code = getattr(target_payment, "error_code", "")
                        target_detail = f"Target payment '{intent.target.entity_id}' verified in incident payments (error: {err_code})"

        checks.append(
            VerificationCheck(
                check_id=CHK_TARGET_CONSISTENCY,
                name="Target Entity Consistency",
                passed=target_passed,
                expected="Target entity matches incident and evidence scope",
                observed=str(intent.target) if target_passed else "MISMATCH",
                detail=target_detail,
            )
        )

        # Re-derive Deterministic Financial Exposure
        rederived_failed_gmv: Optional[Money] = None
        rederived_revenue_at_risk: Optional[Money] = None

        if payments:
            failed_payments = [
                p for p in payments
                if (p.payment.status if hasattr(p, "payment") else p.status) == PaymentStatus.FAILED
            ]
            rederived_failed_gmv = (
                Money(
                    sum(
                        (p.payment.amount.minor_units if hasattr(p, "payment") else p.amount.minor_units)
                        for p in failed_payments
                    ),
                    Currency.INR,
                )
                if failed_payments
                else Money.zero(Currency.INR)
            )

        if rederived_failed_gmv is None:
            # Fall back to evidence metrics
            for ev in scoped_evidence:
                if ev.metrics is not None and ev.metrics.revenue_risk is not None:
                    rederived_failed_gmv = ev.metrics.revenue_risk.failed_gmv
                    rederived_revenue_at_risk = ev.metrics.revenue_risk.revenue_at_risk
                    break

        # 10. CHECK_AMOUNT_SAFETY: Claimed Amount Within Verified Exposure
        amount_passed: Optional[bool] = True
        amount_detail = "No untrusted amount claimed or amount within verified bounds."
        if intent.claimed_amount is not None:
            if not intent.claimed_amount.is_positive:
                amount_passed = False
                amount_detail = "Claimed amount must be positive"
            elif rederived_failed_gmv is not None:
                if intent.claimed_amount > rederived_failed_gmv:
                    amount_passed = False
                    amount_detail = (
                        f"Claimed amount {intent.claimed_amount} exceeds verified failed GMV {rederived_failed_gmv}"
                    )
                else:
                    amount_detail = (
                        f"Claimed amount {intent.claimed_amount} within verified failed GMV {rederived_failed_gmv}"
                    )
            else:
                amount_passed = None
                amount_detail = "Could not re-derive verified exposure to validate claimed amount"

        checks.append(
            VerificationCheck(
                check_id=CHK_AMOUNT_SAFETY,
                name="Monetary Exposure Safety",
                passed=amount_passed,
                expected=(
                    f"Claimed amount <= verified failed GMV ({rederived_failed_gmv})"
                    if rederived_failed_gmv
                    else "Verified deterministic exposure available"
                ),
                observed=str(intent.claimed_amount) if intent.claimed_amount else "NONE_CLAIMED",
                detail=amount_detail,
            )
        )

        # 11. CHK_ACTION_PRECONDITIONS: Specific Action Safety Preconditions
        precond_passed = True
        precond_detail = "Action preconditions satisfied."
        if intent.action == IntentAction.CREATE_PAYMENT_LINK:
            if intent.target is None or intent.target.entity_type != TargetEntityType.PAYMENT:
                precond_passed = False
                precond_detail = "CREATE_PAYMENT_LINK requires a payment target"
            elif payments:
                target_p = next(
                    (
                        p
                        for p in payments
                        if (p.payment.id if hasattr(p, "payment") else p.id)
                        == intent.target.entity_id
                    ),
                    None,
                )
                target_status = (
                    (target_p.payment.status if hasattr(target_p, "payment") else target_p.status)
                    if target_p
                    else None
                )
                if target_p is None or target_status != PaymentStatus.FAILED:
                    precond_passed = False
                    precond_detail = "CREATE_PAYMENT_LINK target payment is not failed"

        checks.append(
            VerificationCheck(
                check_id=CHK_ACTION_PRECONDITIONS,
                name="Action Preconditions",
                passed=precond_passed,
                expected="Action-specific safety preconditions satisfied",
                observed="SATISFIED" if precond_passed else "VIOLATED",
                detail=precond_detail,
            )
        )

        # Aggregate Verdict
        status = VerificationStatus.VERIFIED
        failed_checks = [c for c in checks if c.passed is False]
        inconclusive_checks = [c for c in checks if c.passed is None]

        if failed_checks:
            # If failure is amount mismatch or target mismatch, assign MISMATCH or REJECTED
            mismatch_check_ids = {CHK_AMOUNT_SAFETY, CHK_TARGET_CONSISTENCY}
            if any(c.check_id in mismatch_check_ids for c in failed_checks):
                status = VerificationStatus.MISMATCH
            else:
                status = VerificationStatus.REJECTED
        elif inconclusive_checks:
            status = VerificationStatus.INCONCLUSIVE

        verification_id = (
            f"ver_{short_digest({'intent_id': intent.intent_id, 'status': status.value, 'when': when.isoformat()})}"
        )

        summary = (
            f"Pre-execution verification {status.value.upper()} with "
            f"{len(checks) - len(failed_checks) - len(inconclusive_checks)} passed, "
            f"{len(failed_checks)} failed, {len(inconclusive_checks)} inconclusive."
        )

        result = VerificationResult(
            verification_id=verification_id,
            phase=VerificationPhase.PRE_EXECUTION,
            subject_id=intent.intent_id,
            status=status,
            verified_at=when,
            checks=tuple(checks),
            summary=summary,
        )

        # Audit Event Recording
        if self._audit_log is not None:
            event_type = (
                AuditEventType.INTENT_VERIFIED
                if result.is_verified
                else AuditEventType.INTENT_REJECTED
            )
            self._audit_log.append(
                actor=AuditActor.VERIFIER,
                event_type=event_type,
                summary=f"Verifier evaluated intent {intent.intent_id} -> {status.value.upper()}",
                incident_id=intent.incident_id,
                subject_id=intent.intent_id,
                occurred_at=when,
                payload={
                    "verification_id": verification_id,
                    "intent_id": intent.intent_id,
                    "status": status.value,
                    "checks_count": len(checks),
                    "failed_checks": [c.check_id for c in failed_checks],
                    "intent_content_hash": intent.content_hash(),
                },
            )

        return result

    def verify_and_wrap(
        self,
        intent: AgentIntent,
        incident: Optional[FinancialIncident] = None,
        evidence: Optional[Sequence[FinancialEvidence]] = None,
        payments: Optional[Sequence[Payment]] = None,
        db: Optional[Any] = None,
        now: Optional[datetime] = None,
    ) -> Tuple[Optional[VerifiedIntent], VerificationResult]:
        """Verify the intent and wrap it in a VerifiedIntent if verification succeeds."""
        result = self.verify(
            intent=intent,
            incident=incident,
            evidence=evidence,
            payments=payments,
            db=db,
            now=now,
        )
        if result.is_verified:
            # Re-derive exposure for the wrapped intent
            failed_gmv = None
            revenue_at_risk = None
            if payments:
                failed_payments = [
                    p
                    for p in payments
                    if (p.payment.status if hasattr(p, "payment") else p.status) == PaymentStatus.FAILED
                ]
                failed_gmv = (
                    Money(
                        sum(
                            (p.payment.amount.minor_units if hasattr(p, "payment") else p.amount.minor_units)
                            for p in failed_payments
                        ),
                        Currency.INR,
                    )
                    if failed_payments
                    else Money.zero(Currency.INR)
                )
            elif evidence:
                for ev in evidence:
                    if ev.metrics and ev.metrics.revenue_risk:
                        failed_gmv = ev.metrics.revenue_risk.failed_gmv
                        revenue_at_risk = ev.metrics.revenue_risk.revenue_at_risk
                        break

            verified_intent = VerifiedIntent(
                intent=intent,
                verification_result=result,
                verified_failed_gmv=failed_gmv,
                verified_revenue_at_risk=revenue_at_risk,
                verified_at=result.verified_at,
            )
            return verified_intent, result

        return None, result
