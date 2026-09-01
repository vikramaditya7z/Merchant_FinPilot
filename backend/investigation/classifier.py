"""Runtime Scenario Classifier for Merchant FinPilot.

PROJECT_RULES 1.4, 3.5, 4.1, 4.2 / ARCHITECTURE.md §8, §12.

Connects the 11 canonical Day-2 scenario definitions (ScenarioId) to runtime
evaluation. Slices observed payment attributes, window failure rates, and
error distributions to deterministically classify which scenario best explains
the observed financial event without inventing data or hallucinating facts.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional, Sequence, Tuple

from ..data.ground_truth import ScenarioId
from ..domain.enums import Dimension, FailureCategory, PaymentMethod, PaymentStatus
from ..domain.metrics import FinancialMetrics
from ..domain.payment import EnrichedPayment, Payment


@dataclass(frozen=True)
class ScenarioClassification:
    """Deterministic runtime classification of a payment failure incident."""

    scenario_id: ScenarioId
    confidence: float
    rationale: str
    is_incident: bool
    is_action_eligible: bool
    primary_dimension: Optional[Dimension] = None
    contributing_values: Tuple[str, ...] = ()


class ScenarioClassifier:
    """Evaluates observed payment facts against the 11 canonical scenarios."""

    def classify(
        self,
        payment: Payment,
        enrichment: Optional[Any] = None,
        recent_payments: Optional[Sequence[Payment]] = None,
        metrics: Optional[FinancialMetrics] = None,
    ) -> ScenarioClassification:
        """Classify which of the 11 scenarios matches the failure context.

        Args:
            payment: The triggering payment entity.
            enrichment: Optional PaymentEnrichment with derived dimensions.
            recent_payments: Recent transactions in the active evaluation window.
            metrics: Optional pre-computed FinancialMetrics.

        Returns:
            A deterministic ScenarioClassification grounded in observed facts.
        """
        # 1. Normal Check
        if payment.status != PaymentStatus.FAILED:
            return ScenarioClassification(
                scenario_id=ScenarioId.NORMAL,
                confidence=0.99,
                rationale="Payment is not in failed status; baseline normal traffic.",
                is_incident=False,
                is_action_eligible=False,
                primary_dimension=None,
                contributing_values=(),
            )

        err_code = (payment.error_code or "").upper()
        err_desc = (payment.error_description or "").lower()
        err_source = (payment.error_source or "").lower()
        method = payment.method
        fc = enrichment.failure_category if enrichment and hasattr(enrichment, "failure_category") else None

        # 2. Risk / Fraud Blocked (Scenario 11: RECOVERY_NOT_ELIGIBLE)
        if (
            fc == FailureCategory.RISK_BLOCKED
            or err_source == "risk"
            or "risk" in err_code.lower()
            or "fraud" in err_desc
            or "blacklisted" in err_desc
            or "suspicious" in err_desc
        ):
            return ScenarioClassification(
                scenario_id=ScenarioId.RECOVERY_NOT_ELIGIBLE,
                confidence=0.95,
                rationale="Payment blocked by risk/fraud protection rules. Automated recovery is intentionally forbidden.",
                is_incident=True,
                is_action_eligible=False,
                primary_dimension=Dimension.FAILURE_CATEGORY,
                contributing_values=("risk_blocked",),
            )

        # 3. Traffic Volume & Baseline Sufficiency Check
        recent_count = len(recent_payments) if recent_payments is not None else 1
        failed_payments = [p for p in recent_payments if p.is_failure] if recent_payments else [payment]
        failed_count = len(failed_payments)

        has_baseline = (
            metrics is not None
            and metrics.baseline is not None
            and metrics.baseline.is_sufficient
        )

        # Cold-Start / Low-Volume Guard:
        # A single isolated failure without an established baseline or meaningful failure cluster
        # cannot statistically be identified as a "spike".
        if (
            (metrics is not None and (metrics.baseline is None or not metrics.baseline.is_sufficient) and failed_count < 3)
            or (not has_baseline and recent_count < 5 and failed_count < 3)
        ):
            return ScenarioClassification(
                scenario_id=ScenarioId.INSUFFICIENT_DATA,
                confidence=0.90,
                rationale=(
                    f"Insufficient transaction volume (recent decided={recent_count}, failures={failed_count}) "
                    "or missing historical baseline to establish failure rate degradation."
                ),
                is_incident=False,
                is_action_eligible=False,
                primary_dimension=None,
                contributing_values=(),
            )

        # 4. Multi-Method Degradation (Scenario 7: MULTIPLE_FAILURES)
        if recent_payments and len(recent_payments) >= 5:
            upi_fails = [p for p in recent_payments if p.method == PaymentMethod.UPI and p.is_failure]
            card_fails = [p for p in recent_payments if p.method == PaymentMethod.CARD and p.is_failure]

            if len(upi_fails) >= 2 and len(card_fails) >= 2:
                return ScenarioClassification(
                    scenario_id=ScenarioId.MULTIPLE_FAILURES,
                    confidence=0.90,
                    rationale="Multiple distinct payment methods (UPI and Cards) experiencing concurrent failures.",
                    is_incident=True,
                    is_action_eligible=True,
                    primary_dimension=None,
                    contributing_values=("upi", "card"),
                )

        # 5. Low Volume Sampling / Non-Significant Variation (Scenario 9: SMALL_RANDOM_VARIATION)
        if (
            has_baseline
            and metrics is not None
            and metrics.significance is not None
            and metrics.significance.z_score < 2.0
            and (
                metrics.deviation is None
                or not metrics.deviation.is_worse_than_baseline
                or metrics.deviation.absolute_percentage_points < Decimal("5.0")
            )
        ):
            return ScenarioClassification(
                scenario_id=ScenarioId.SMALL_RANDOM_VARIATION,
                confidence=0.85,
                rationale="Observed failure variation is within normal statistical sampling bounds.",
                is_incident=False,
                is_action_eligible=False,
                primary_dimension=None,
                contributing_values=(),
            )

        # 6. Evening Gateway Timeouts (Scenario 4: EVENING_FAILURE_SPIKE)
        hour_utc = payment.created_at.hour
        if (
            18 <= hour_utc <= 21
            and (fc == FailureCategory.TIMEOUT or "timeout" in err_desc or "gateway" in err_code.lower())
            and (failed_count >= 2 or has_baseline)
        ):
            return ScenarioClassification(
                scenario_id=ScenarioId.EVENING_FAILURE_SPIKE,
                confidence=0.88,
                rationale="Gateway timeout degradation during peak evening volume hours.",
                is_incident=True,
                is_action_eligible=True,
                primary_dimension=Dimension.HOUR_OF_DAY,
                contributing_values=(str(hour_utc),),
            )

        # 7. UPI Rail Degradation (Scenario 2: UPI_FAILURE_SPIKE)
        if method == PaymentMethod.UPI:
            upi_fails_count = len([p for p in recent_payments if p.method == PaymentMethod.UPI and p.is_failure]) if recent_payments else 1
            is_upi_error = (
                fc == FailureCategory.ISSUER_UNAVAILABLE
                or "upi" in err_code.lower()
                or "vpa" in err_desc
                or "issuer_unavailable" in err_desc
                or "mpin" in err_desc
                or "bank" in err_source
            )
            if upi_fails_count >= 2 or (has_baseline and is_upi_error) or (metrics is not None and metrics.counts.decided >= 5 and is_upi_error):
                return ScenarioClassification(
                    scenario_id=ScenarioId.UPI_FAILURE_SPIKE,
                    confidence=0.92,
                    rationale="UPI rail or issuing bank degradation detected with elevated failure concentration.",
                    is_incident=True,
                    is_action_eligible=True,
                    primary_dimension=Dimension.PAYMENT_METHOD,
                    contributing_values=("upi",),
                )

        # 8. Card Authentication Degradation (Scenario 3: CARD_FAILURE_SPIKE)
        if method == PaymentMethod.CARD:
            card_fails_count = len([p for p in recent_payments if p.method == PaymentMethod.CARD and p.is_failure]) if recent_payments else 1
            is_card_error = (
                fc == FailureCategory.AUTHENTICATION_FAILED
                or "auth" in err_code.lower()
                or "3ds" in err_desc
                or "otp" in err_desc
                or "declined" in err_desc
            )
            if card_fails_count >= 2 or (has_baseline and is_card_error) or (metrics is not None and metrics.counts.decided >= 5 and is_card_error):
                return ScenarioClassification(
                    scenario_id=ScenarioId.CARD_FAILURE_SPIKE,
                    confidence=0.91,
                    rationale="Card payment authentication or 3DS verification failure concentration detected.",
                    is_incident=True,
                    is_action_eligible=True,
                    primary_dimension=Dimension.PAYMENT_METHOD,
                    contributing_values=("card",),
                )

        # 9. Fallback Classification
        if not has_baseline or recent_count < 5:
            return ScenarioClassification(
                scenario_id=ScenarioId.INSUFFICIENT_DATA,
                confidence=0.80,
                rationale=f"Single isolated failure ({method.value}: {err_code or 'UNKNOWN'}) without sufficient volume or baseline.",
                is_incident=False,
                is_action_eligible=False,
                primary_dimension=None,
                contributing_values=(),
            )

        return ScenarioClassification(
            scenario_id=ScenarioId.SMALL_RANDOM_VARIATION,
            confidence=0.75,
            rationale=f"Transaction failed ({method.value}: {err_code or 'UNKNOWN'}) within normal sampling variance.",
            is_incident=False,
            is_action_eligible=False,
            primary_dimension=None,
            contributing_values=(),
        )
