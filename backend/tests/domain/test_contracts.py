"""Tests for the contracts that carry authority: payment, intent, policy,
verification, execution, incident, audit, and canonical serialization.

These types are where the architecture's safety claims are actually cashed. A
``PolicyDecision`` that can be ALLOW while carrying a blocking violation, an
``AgentIntent`` that can cite evidence that does not exist, an ``ActionResult``
that can claim SUCCEEDED with nothing to verify against — each would make a
documented guarantee false. So most of what follows asserts that the illegal
object *cannot be constructed*.
"""

import unittest
from datetime import datetime, timedelta
from decimal import Decimal

from ...domain.audit import AuditEvent
from ...domain.canonical import (
    assert_no_secrets,
    canonical_json,
    canonicalize,
    digest,
    short_digest,
)
from ...domain.enums import (
    AuditActor,
    AuditEventType,
    Currency,
    Dimension,
    ExecutionStatus,
    FailureCategory,
    IncidentStatus,
    IncidentType,
    IntentAction,
    PaymentMethod,
    PaymentOutcome,
    PaymentStatus,
    PolicyVerdict,
    Severity,
    SourceConfidence,
    TargetEntityType,
    VerificationPhase,
    VerificationStatus,
    ViolationEffect,
)
from ...domain.errors import (
    DomainValidationError,
    MoneyPrecisionError,
    NonCanonicalValueError,
    SecretLeakError,
)
from ...domain.execution import ActionResult, build_execution_key
from ...domain.incident import FinancialEvidence, FinancialIncident
from ...domain.intent import AgentIntent, IntentTarget
from ...domain.metrics import FinancialMetrics, Rate, TransactionCounts
from ...domain.money import Money
from ...domain.payment import (
    EnrichedPayment,
    Order,
    Payment,
    PaymentEnrichment,
)
from ...domain.policy import PolicyDecision, PolicyViolation
from ...domain.verification import VerificationCheck, VerificationResult
from ...domain.window import UTC, TimeWindow
from ..helpers import HOUR, NOW, T0, enriched, payment

REASON = "Failure rate on UPI rose well above baseline across the last hour."


class PaymentTests(unittest.TestCase):
    def test_zero_amount_is_not_a_payment(self):
        with self.assertRaises(DomainValidationError):
            payment(amount_paise=0)

    def test_negative_amount_rejected(self):
        with self.assertRaises(DomainValidationError):
            payment(amount_paise=-100)

    def test_naive_created_at_rejected(self):
        with self.assertRaises(DomainValidationError):
            Payment(
                id="pay_1",
                created_at=datetime(2026, 8, 20, 10, 0, 0),
                amount=Money(100),
                status=PaymentStatus.CAPTURED,
                method=PaymentMethod.UPI,
            )

    def test_blank_id_rejected(self):
        with self.assertRaises(DomainValidationError):
            payment(id="   ")

    def test_identifier_is_stored_verbatim(self):
        # No stripping, no case folding: the id must match the source of truth
        # exactly or reconciliation silently fails (PROJECT_RULES 2.3).
        raw = "pay_MixedCase123"
        self.assertEqual(payment(id=raw).id, raw)

    def test_error_details_on_a_successful_payment_rejected(self):
        # A captured payment carrying an error code means the upstream mapping is
        # wrong; better to fail here than skew a failure breakdown later.
        with self.assertRaises(DomainValidationError):
            Payment(
                id="pay_1",
                created_at=T0,
                amount=Money(100),
                status=PaymentStatus.CAPTURED,
                method=PaymentMethod.UPI,
                error_code="GATEWAY_ERROR",
            )

    def test_outcome_mapping(self):
        self.assertIs(
            payment(status=PaymentStatus.CAPTURED).outcome, PaymentOutcome.SUCCEEDED
        )
        self.assertIs(
            payment(status=PaymentStatus.AUTHORIZED).outcome, PaymentOutcome.SUCCEEDED
        )
        self.assertIs(
            payment(status=PaymentStatus.REFUNDED).outcome, PaymentOutcome.SUCCEEDED
        )
        self.assertIs(payment(status=PaymentStatus.FAILED).outcome, PaymentOutcome.FAILED)
        self.assertIs(
            payment(status=PaymentStatus.CREATED).outcome, PaymentOutcome.UNDECIDED
        )

    def test_refunded_counts_as_succeeded_for_rate_purposes(self):
        # A refund is a business event, not an authorization failure. Counting it
        # as a failure would make refunds look like an outage.
        refunded = payment(status=PaymentStatus.REFUNDED)
        self.assertTrue(refunded.is_success)
        self.assertFalse(refunded.is_failure)
        self.assertTrue(refunded.is_decided)

    def test_created_is_undecided_and_out_of_the_denominator(self):
        in_flight = payment(status=PaymentStatus.CREATED)
        self.assertFalse(in_flight.is_decided)
        self.assertFalse(in_flight.is_failure)
        self.assertFalse(in_flight.is_success)

    def test_from_unix_matches_razorpay_shape(self):
        built = Payment.from_unix(
            id="pay_2",
            created_at_unix=1_756_000_000,
            amount_minor_units=25_000,
            status=PaymentStatus.FAILED,
            method=PaymentMethod.CARD,
            error_code="BAD_REQUEST_ERROR",
        )
        self.assertEqual(built.created_at.tzinfo, UTC)
        self.assertEqual(built.amount, Money(25_000, Currency.INR))

    def test_payment_carries_no_ground_truth_fields(self):
        """ADR-005 enforced structurally, not by convention.

        If a label field ever appears on ``Payment``, evaluation ground truth can
        reach an agent prompt by accident. The check is on the contract itself so
        the mistake is impossible rather than merely discouraged.
        """
        fields = set(Payment.__dataclass_fields__)
        for forbidden in (
            "scenario_id",
            "ground_truth",
            "is_incident",
            "matched_anomaly",
            "label",
            "expected_root_cause",
        ):
            self.assertNotIn(forbidden, fields)


class EnrichmentTests(unittest.TestCase):
    def test_enrichment_cannot_claim_to_be_observed(self):
        # Enrichment is inference. Labelling it OBSERVED would launder a guess
        # into a fact (PROJECT_RULES 2.6).
        with self.assertRaises(DomainValidationError):
            PaymentEnrichment(
                payment_id="pay_1", source_confidence=SourceConfidence.OBSERVED
            )

    def test_enrichment_must_match_its_payment(self):
        with self.assertRaises(DomainValidationError):
            EnrichedPayment(
                payment=payment(id="pay_1"),
                enrichment=PaymentEnrichment(payment_id="pay_OTHER"),
            )

    def test_unenriched_payment_has_none_dimensions(self):
        bare = EnrichedPayment(payment=payment())
        self.assertIsNone(bare.region)
        self.assertIsNone(bare.provider)
        self.assertIsNone(bare.failure_category)

    def test_enriched_accessors(self):
        item = enriched(
            payment(status=PaymentStatus.FAILED, error_code="GATEWAY_ERROR"),
            region="IN-KA",
            provider="acquirer_b",
            failure_category=FailureCategory.ISSUER_UNAVAILABLE,
        )
        self.assertEqual(item.region, "IN-KA")
        self.assertEqual(item.provider, "acquirer_b")
        self.assertIs(item.failure_category, FailureCategory.ISSUER_UNAVAILABLE)


class OrderTests(unittest.TestCase):
    def test_zero_amount_order_rejected(self):
        with self.assertRaises(DomainValidationError):
            Order(id="order_1", created_at=T0, amount=Money(0), status="paid")  # type: ignore[arg-type]

    def test_is_paid(self):
        from ...domain.enums import OrderStatus

        self.assertTrue(
            Order(
                id="order_1", created_at=T0, amount=Money(100), status=OrderStatus.PAID
            ).is_paid
        )


class CanonicalTests(unittest.TestCase):
    def test_key_order_does_not_change_the_digest(self):
        self.assertEqual(
            digest({"a": 1, "b": 2}),
            digest({"b": 2, "a": 1}),
        )

    def test_floats_cannot_be_canonicalized(self):
        with self.assertRaises(NonCanonicalValueError):
            canonical_json({"amount": 100.5})

    def test_nested_float_is_caught(self):
        # The obvious case is a top-level float; the dangerous one is buried.
        with self.assertRaises(NonCanonicalValueError):
            canonical_json({"outer": {"inner": [1, 2, 3.0]}})

    def test_money_round_trips_losslessly(self):
        self.assertEqual(
            canonicalize(Money(12_345)),
            {"__type__": "money", "minor_units": 12_345, "currency": "INR"},
        )

    def test_decimal_trailing_zeros_hash_identically(self):
        # 0.50 and 0.5 are the same rate. If they hashed differently, an
        # idempotency key would depend on how a number was typed.
        self.assertEqual(digest(Decimal("0.50")), digest(Decimal("0.5")))

    def test_decimal_integer_does_not_become_exponent_notation(self):
        # normalize() turns 100 into 1E+2; the encoder must undo that or the
        # digest changes with the input's formatting.
        self.assertEqual(canonicalize(Decimal("100"))["value"], "100")
        self.assertEqual(digest(Decimal("100")), digest(Decimal("100.00")))

    def test_non_finite_decimal_rejected(self):
        with self.assertRaises(NonCanonicalValueError):
            canonical_json(Decimal("NaN"))

    def test_naive_datetime_rejected(self):
        with self.assertRaises(NonCanonicalValueError):
            canonical_json(datetime(2026, 8, 20, 10, 0, 0))

    def test_set_ordering_does_not_affect_the_digest(self):
        self.assertEqual(digest({"x", "y", "z"}), digest({"z", "y", "x"}))

    def test_non_string_mapping_key_rejected(self):
        with self.assertRaises(NonCanonicalValueError):
            canonical_json({1: "one"})

    def test_unknown_type_rejected_rather_than_stringified(self):
        class Opaque:
            pass

        with self.assertRaises(NonCanonicalValueError):
            canonical_json(Opaque())

    def test_bool_is_preserved_as_bool(self):
        # Not coerced to 1/0, which would make True and 1 hash alike.
        self.assertIs(canonicalize(True), True)
        self.assertNotEqual(digest(True), digest(1))

    def test_short_digest_is_a_prefix_and_has_a_floor(self):
        value = {"k": "v"}
        self.assertTrue(digest(value).startswith(short_digest(value, 16)))
        with self.assertRaises(NonCanonicalValueError):
            short_digest(value, 4)

    def test_secret_screening_catches_credential_shaped_keys(self):
        for key in ("api_key", "KeySecret", "x-authorization", "webhook_signature"):
            with self.subTest(key=key):
                with self.assertRaises(SecretLeakError):
                    assert_no_secrets({key: "whatever"})

    def test_secret_screening_permits_ordinary_keys(self):
        assert_no_secrets({"payment_id": "pay_1", "amount_paise": 100})


class AgentIntentTests(unittest.TestCase):
    """The agent's authority ends at constructing one of these."""

    def _intent(self, **overrides) -> AgentIntent:
        kwargs = dict(
            intent_id="int_1",
            incident_id="inc_1",
            action=IntentAction.NOTIFY_MERCHANT,
            reason=REASON,
            proposed_at=NOW,
            model_id="claude-test",
            prompt_version="v1",
            target=IntentTarget(TargetEntityType.MERCHANT, "acct_1"),
            evidence_refs=("ev_1",),
        )
        kwargs.update(overrides)
        return AgentIntent(**kwargs)

    def test_intent_has_no_execution_capability(self):
        """The structural claim behind ARCHITECTURE.md 5.1.

        An intent is a proposal. If it grew an ``execute``/``apply``/``send``
        method, or held a client or credential, the agent could act directly and
        the entire verification chain would be optional.
        """
        intent = self._intent()
        for forbidden in ("execute", "apply", "run", "send", "commit", "perform"):
            self.assertFalse(
                hasattr(intent, forbidden),
                f"AgentIntent must not expose {forbidden}()",
            )
        for forbidden in ("client", "api", "credentials", "session", "executor"):
            self.assertNotIn(forbidden, AgentIntent.__dataclass_fields__)

    def test_a_short_reason_is_not_a_justification(self):
        with self.assertRaises(DomainValidationError):
            self._intent(reason="because")

    def test_consequential_action_requires_evidence(self):
        with self.assertRaises(DomainValidationError):
            self._intent(evidence_refs=())

    def test_duplicate_evidence_refs_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._intent(evidence_refs=("ev_1", "ev_1"))

    def test_no_action_needs_no_evidence_and_no_target(self):
        # Proposing to do nothing requires no justification and points at
        # nothing. It is also the only action with that exemption.
        idle = self._intent(action=IntentAction.NO_ACTION, target=None, evidence_refs=())
        self.assertFalse(idle.is_consequential)

    def test_no_action_must_not_carry_a_target(self):
        with self.assertRaises(DomainValidationError):
            self._intent(action=IntentAction.NO_ACTION, evidence_refs=())

    def test_non_targetless_action_requires_a_target(self):
        with self.assertRaises(DomainValidationError):
            self._intent(action=IntentAction.CREATE_PAYMENT_LINK, target=None)

    def test_recommend_only_is_not_consequential(self):
        self.assertFalse(
            self._intent(action=IntentAction.RECOMMEND_ONLY).is_consequential
        )

    def test_payment_link_is_consequential(self):
        self.assertTrue(
            self._intent(
                action=IntentAction.CREATE_PAYMENT_LINK,
                target=IntentTarget(TargetEntityType.PAYMENT, "pay_1"),
            ).is_consequential
        )

    def test_float_parameter_rejected(self):
        # The parameter dict is the one place an LLM-authored number could reach
        # an outbound payload. Floats do not get in (PROJECT_RULES 1.6).
        with self.assertRaises(MoneyPrecisionError):
            self._intent(parameters={"amount": 100.5})

    def test_parameter_names_must_be_snake_case(self):
        for bad in ("Amount", "amount-paise", "amount paise", ""):
            with self.subTest(name=bad):
                with self.assertRaises(DomainValidationError):
                    self._intent(parameters={bad: 1})

    def test_permitted_parameter_types(self):
        intent = self._intent(
            parameters={
                "note": "text",
                "count": 3,
                "urgent": True,
                "amount": Money(50_000),
            }
        )
        self.assertEqual(intent.parameters["amount"], Money(50_000))

    def test_unsupported_parameter_type_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._intent(parameters={"window": HOUR})

    def test_parameters_are_immutable(self):
        intent = self._intent(parameters={"note": "text"})
        with self.assertRaises(TypeError):
            intent.parameters["note"] = "changed"  # type: ignore[index]

    def test_float_confidence_rejected(self):
        with self.assertRaises(MoneyPrecisionError):
            self._intent(confidence=0.9)

    def test_confidence_out_of_range_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._intent(confidence=Decimal("1.5"))

    def test_claimed_amount_must_be_positive(self):
        with self.assertRaises(DomainValidationError):
            self._intent(claimed_amount=Money(0))

    def test_content_hash_ignores_wording_timing_and_confidence(self):
        """Two proposals that differ only cosmetically are the same proposal.

        This is what stops an agent from re-executing the same action by
        rephrasing its justification.
        """
        first = self._intent()
        second = self._intent(
            intent_id="int_2",
            reason=REASON + " Restating the same finding in different words.",
            proposed_at=NOW + timedelta(minutes=5),
            confidence=Decimal("0.4"),
        )
        self.assertEqual(first.content_hash(), second.content_hash())

    def test_content_hash_changes_with_the_claimed_amount(self):
        self.assertNotEqual(
            self._intent(claimed_amount=Money(10_000)).content_hash(),
            self._intent(claimed_amount=Money(10_001)).content_hash(),
        )

    def test_content_hash_changes_with_the_target(self):
        self.assertNotEqual(
            self._intent().content_hash(),
            self._intent(
                target=IntentTarget(TargetEntityType.MERCHANT, "acct_2")
            ).content_hash(),
        )

    def test_content_hash_is_stable_across_parameter_insertion_order(self):
        forward = self._intent(parameters={"a": 1, "b": 2})
        backward = self._intent(parameters={"b": 2, "a": 1})
        self.assertEqual(forward.content_hash(), backward.content_hash())

    def test_empty_target_id_rejected(self):
        with self.assertRaises(DomainValidationError):
            IntentTarget(TargetEntityType.PAYMENT, "  ")


class PolicyDecisionTests(unittest.TestCase):
    """The only object that authorizes execution."""

    def _violation(self, effect=ViolationEffect.BLOCKING) -> PolicyViolation:
        return PolicyViolation(
            rule_id="R-001",
            rule_version="v1",
            effect=effect,
            message="amount exceeds the per-action limit",
        )

    def _decision(self, **overrides) -> PolicyDecision:
        kwargs = dict(
            decision_id="dec_1",
            intent_id="int_1",
            intent_hash="a" * 64,
            verdict=PolicyVerdict.ALLOW,
            rationale="all checks satisfied",
            evaluated_at=NOW,
            expires_at=PolicyDecision.default_expiry(NOW),
            rule_set_version="rules-v1",
        )
        kwargs.update(overrides)
        return PolicyDecision(**kwargs)

    def test_allow_cannot_coexist_with_a_blocking_violation(self):
        # The core safety invariant, enforced on the contract so no engine bug
        # can emit a self-contradictory authorization.
        with self.assertRaises(DomainValidationError):
            self._decision(violations=(self._violation(),))

    def test_allow_may_carry_a_non_blocking_note(self):
        allowed = self._decision(
            violations=(self._violation(ViolationEffect.ESCALATING),),
            verdict=PolicyVerdict.ALLOW,
        )
        self.assertTrue(allowed.authorizes_execution)

    def test_block_must_state_a_reason(self):
        with self.assertRaises(DomainValidationError):
            self._decision(verdict=PolicyVerdict.BLOCK, violations=())

    def test_escalate_must_name_an_approver(self):
        with self.assertRaises(DomainValidationError):
            self._decision(
                verdict=PolicyVerdict.ESCALATE,
                violations=(self._violation(ViolationEffect.ESCALATING),),
                required_approvals=(),
            )

    def test_only_allow_authorizes(self):
        for verdict, extra in (
            (PolicyVerdict.BLOCK, {"violations": (self._violation(),)}),
            (
                PolicyVerdict.ESCALATE,
                {
                    "violations": (self._violation(ViolationEffect.ESCALATING),),
                    "required_approvals": ("finance_lead",),
                },
            ),
        ):
            with self.subTest(verdict=verdict):
                self.assertFalse(self._decision(verdict=verdict, **extra).authorizes_execution)

    def test_expiry_must_be_after_evaluation(self):
        with self.assertRaises(DomainValidationError):
            self._decision(expires_at=NOW)

    def test_a_stale_decision_authorizes_nothing(self):
        decision = self._decision()
        later = NOW + timedelta(seconds=301)
        self.assertTrue(decision.authorizes("a" * 64, NOW))
        self.assertFalse(decision.authorizes("a" * 64, later))

    def test_a_decision_cannot_be_replayed_against_another_intent(self):
        decision = self._decision()
        self.assertFalse(decision.authorizes("b" * 64, NOW))

    def test_authorizes_requires_all_three_conditions(self):
        blocked = self._decision(
            verdict=PolicyVerdict.BLOCK, violations=(self._violation(),)
        )
        self.assertFalse(blocked.authorizes("a" * 64, NOW))

    def test_validity_window_is_half_open(self):
        decision = self._decision(expires_at=NOW + timedelta(seconds=300))
        self.assertTrue(decision.is_valid_at(NOW))
        self.assertFalse(decision.is_valid_at(NOW + timedelta(seconds=300)))

    def test_default_expiry_rejects_a_non_positive_ttl(self):
        with self.assertRaises(DomainValidationError):
            PolicyDecision.default_expiry(NOW, 0)
        with self.assertRaises(DomainValidationError):
            PolicyDecision.default_expiry(NOW, True)  # type: ignore[arg-type]

    def test_blocking_violations_are_separable_from_escalating_ones(self):
        decision = self._decision(
            verdict=PolicyVerdict.BLOCK,
            violations=(
                self._violation(),
                self._violation(ViolationEffect.ESCALATING),
            ),
        )
        self.assertEqual(len(decision.violations), 2)
        self.assertEqual(len(decision.blocking_violations), 1)


class VerificationTests(unittest.TestCase):
    def _check(self, passed) -> VerificationCheck:
        return VerificationCheck(
            check_id="chk_1",
            name="amount matches source record",
            passed=passed,
            expected="₹500.00",
            observed="₹500.00",
        )

    def _result(self, status, checks) -> VerificationResult:
        return VerificationResult(
            verification_id="ver_1",
            phase=VerificationPhase.PRE_EXECUTION,
            subject_id="int_1",
            status=status,
            verified_at=NOW,
            checks=checks,
        )

    def test_inconclusive_is_not_rounded_up_to_verified(self):
        # "We could not tell" must never render as "it worked"
        # (PROJECT_RULES 8.5).
        with self.assertRaises(DomainValidationError):
            self._result(VerificationStatus.VERIFIED, (self._check(None),))

    def test_a_failed_check_blocks_verified(self):
        with self.assertRaises(DomainValidationError):
            self._result(VerificationStatus.VERIFIED, (self._check(False),))

    def test_verified_requires_at_least_one_check(self):
        # An empty check list is not a clean bill of health.
        with self.assertRaises(DomainValidationError):
            self._result(VerificationStatus.VERIFIED, ())

    def test_verified_with_all_checks_passing(self):
        result = self._result(VerificationStatus.VERIFIED, (self._check(True),))
        self.assertTrue(result.is_verified)
        self.assertEqual(result.failed_checks, ())

    def test_mismatch_requires_a_failed_check(self):
        with self.assertRaises(DomainValidationError):
            self._result(VerificationStatus.MISMATCH, (self._check(True),))

    def test_rejected_requires_a_failed_check(self):
        with self.assertRaises(DomainValidationError):
            self._result(VerificationStatus.REJECTED, (self._check(None),))

    def test_inconclusive_status_needs_no_failed_check(self):
        result = self._result(VerificationStatus.INCONCLUSIVE, (self._check(None),))
        self.assertFalse(result.is_verified)
        self.assertEqual(len(result.inconclusive_checks), 1)

    def test_duplicate_check_ids_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._result(
                VerificationStatus.INCONCLUSIVE, (self._check(None), self._check(None))
            )

    def test_passing_checks_are_retained_not_discarded(self):
        # A reviewer needs to see what was examined, not only what broke
        # (PROJECT_RULES 8.7).
        mixed = VerificationResult(
            verification_id="ver_2",
            phase=VerificationPhase.POST_EXECUTION,
            subject_id="exec_1",
            status=VerificationStatus.MISMATCH,
            verified_at=NOW,
            checks=(
                VerificationCheck("chk_1", "state reachable", True, "200", "200"),
                VerificationCheck("chk_2", "amount matches", False, "500", "450"),
            ),
        )
        self.assertEqual(len(mixed.checks), 2)
        self.assertEqual(len(mixed.failed_checks), 1)

    def test_non_bool_passed_rejected(self):
        with self.assertRaises(DomainValidationError):
            VerificationCheck("chk_1", "n", "yes", "a", "b")  # type: ignore[arg-type]


class ExecutionTests(unittest.TestCase):
    def _result(self, **overrides) -> ActionResult:
        kwargs = dict(
            execution_key="exec_abc",
            intent_id="int_1",
            decision_id="dec_1",
            action=IntentAction.NOTIFY_MERCHANT,
            status=ExecutionStatus.SUCCEEDED,
            attempted_at=NOW,
            completed_at=NOW + timedelta(seconds=1),
            provider_reference="rzp_ref_1",
        )
        kwargs.update(overrides)
        return ActionResult(**kwargs)

    def test_success_requires_something_to_verify_against(self):
        # Without an external reference there is nothing to read state back
        # from, so the claim cannot be checked and is not accepted.
        with self.assertRaises(DomainValidationError):
            self._result(provider_reference=None)

    def test_success_requires_a_completion_time(self):
        with self.assertRaises(DomainValidationError):
            self._result(completed_at=None)

    def test_failure_requires_an_error(self):
        with self.assertRaises(DomainValidationError):
            self._result(
                status=ExecutionStatus.FAILED,
                provider_reference=None,
                error_code=None,
                error_message=None,
            )

    def test_completion_cannot_precede_the_attempt(self):
        with self.assertRaises(DomainValidationError):
            self._result(completed_at=NOW - timedelta(seconds=1))

    def test_unknown_is_ambiguous_and_needs_verification(self):
        # A timeout is not a failure. Recording it as one invites a retry, and a
        # retry is how one payment becomes two.
        unknown = self._result(
            status=ExecutionStatus.UNKNOWN, provider_reference=None, completed_at=None
        )
        self.assertTrue(unknown.is_ambiguous)
        self.assertTrue(unknown.needs_outcome_verification)

    def test_success_also_needs_outcome_verification(self):
        self.assertTrue(self._result().needs_outcome_verification)

    def test_clean_failure_needs_no_outcome_verification(self):
        failed = self._result(
            status=ExecutionStatus.FAILED,
            provider_reference=None,
            completed_at=None,
            error_code="RATE_LIMITED",
        )
        self.assertFalse(failed.needs_outcome_verification)
        self.assertFalse(failed.is_ambiguous)

    def test_every_attempt_names_its_authorization(self):
        # No attempt exists without an ALLOW decision behind it.
        with self.assertRaises(DomainValidationError):
            self._result(decision_id="")

    def test_execution_key_is_stable_across_parameter_ordering(self):
        first = build_execution_key(
            "inc_1", IntentAction.CREATE_PAYMENT_LINK, "pay_1", {"a": 1, "b": 2}
        )
        second = build_execution_key(
            "inc_1", IntentAction.CREATE_PAYMENT_LINK, "pay_1", {"b": 2, "a": 1}
        )
        self.assertEqual(first, second)

    def test_execution_key_separates_different_actions(self):
        self.assertNotEqual(
            build_execution_key("inc_1", IntentAction.NOTIFY_MERCHANT, "m_1", {}),
            build_execution_key("inc_1", IntentAction.CREATE_PAYMENT_LINK, "m_1", {}),
        )

    def test_execution_key_separates_different_incidents(self):
        # Same action on the same target in a *different* incident is a
        # different action, and must not be deduplicated away.
        self.assertNotEqual(
            build_execution_key("inc_1", IntentAction.NOTIFY_MERCHANT, "m_1", {}),
            build_execution_key("inc_2", IntentAction.NOTIFY_MERCHANT, "m_1", {}),
        )

    def test_execution_key_requires_an_incident(self):
        with self.assertRaises(DomainValidationError):
            build_execution_key("", IntentAction.NOTIFY_MERCHANT, "m_1", {})


class IncidentTests(unittest.TestCase):
    def _metrics(self, window=HOUR) -> FinancialMetrics:
        return FinancialMetrics(
            window=window,
            counts=TransactionCounts(90, 10),
            failure_rate=Rate(10, 100),
            success_rate=Rate(90, 100),
            baseline=None,
            deviation=None,
            significance=None,
            revenue_risk=None,
            computed_at=NOW,
            computation_version="test-1",
        )

    def _evidence(self, evidence_id="ev_1", incident_id="inc_1") -> FinancialEvidence:
        return FinancialEvidence(
            evidence_id=evidence_id,
            incident_id=incident_id,
            summary="UPI failure rate is materially above baseline.",
            window=HOUR,
            computed_at=NOW,
            metrics=self._metrics(),
        )

    def _incident(self, **overrides) -> FinancialIncident:
        kwargs = dict(
            incident_id="inc_1",
            merchant_id="acct_1",
            incident_type=IncidentType.PAYMENT_FAILURE_SPIKE,
            status=IncidentStatus.DETECTED,
            severity=Severity.HIGH,
            detected_at=NOW,
            window=HOUR,
            metrics=self._metrics(),
        )
        kwargs.update(overrides)
        return FinancialIncident(**kwargs)

    def test_a_narrative_alone_is_not_evidence(self):
        # Prose with no deterministic result behind it cannot be cited.
        with self.assertRaises(DomainValidationError):
            FinancialEvidence(
                evidence_id="ev_1",
                incident_id="inc_1",
                summary="I think UPI looks bad right now.",
                window=HOUR,
                computed_at=NOW,
            )

    def test_evidence_from_another_incident_is_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._incident(evidence=(self._evidence(incident_id="inc_OTHER"),))

    def test_duplicate_evidence_ids_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._incident(evidence=(self._evidence(), self._evidence()))

    def test_metrics_window_must_match_the_incident_window(self):
        other = TimeWindow(T0 + timedelta(hours=5), T0 + timedelta(hours=6))
        with self.assertRaises(DomainValidationError):
            self._incident(window=other)

    def test_unresolvable_evidence_reference_returns_none(self):
        # This is how a fabricated citation gets caught rather than tidied up.
        incident = self._incident(evidence=(self._evidence(),))
        self.assertIsNotNone(incident.find_evidence("ev_1"))
        self.assertIsNone(incident.find_evidence("ev_invented"))

    def test_with_evidence_appends_immutably(self):
        original = self._incident()
        updated = original.with_evidence(self._evidence())
        self.assertEqual(original.evidence, ())
        self.assertEqual(updated.evidence_ids, ("ev_1",))

    def test_incident_key_is_stable_for_the_same_degradation(self):
        # Repeated detection over the same window must recognise the same
        # incident rather than open a new one every poll.
        self.assertEqual(self._incident().incident_key, self._incident().incident_key)

    def test_incident_key_separates_dimensions(self):
        upi = self._incident(
            primary_dimension=Dimension.PAYMENT_METHOD, primary_dimension_value="upi"
        )
        card = self._incident(
            primary_dimension=Dimension.PAYMENT_METHOD, primary_dimension_value="card"
        )
        self.assertNotEqual(upi.incident_key, card.incident_key)

    def test_incident_key_separates_windows(self):
        later = TimeWindow(T0 + timedelta(hours=1), T0 + timedelta(hours=2))
        self.assertNotEqual(
            self._incident().incident_key,
            self._incident(window=later, metrics=self._metrics(later)).incident_key,
        )

    def test_dimension_value_without_a_dimension_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._incident(primary_dimension_value="upi")

    def test_evidence_freshness_is_checked_not_assumed(self):
        evidence = self._evidence()
        self.assertTrue(evidence.is_fresh_at(NOW + timedelta(seconds=30), 60))
        self.assertFalse(evidence.is_fresh_at(NOW + timedelta(seconds=120), 60))
        # Evidence computed in the future is not "fresh", it is wrong.
        self.assertFalse(evidence.is_fresh_at(NOW - timedelta(seconds=30), 60))

    def test_freshness_window_must_be_a_positive_int(self):
        with self.assertRaises(DomainValidationError):
            self._evidence().is_fresh_at(NOW, 0)
        with self.assertRaises(DomainValidationError):
            self._evidence().is_fresh_at(NOW, True)  # type: ignore[arg-type]


class AuditEventTests(unittest.TestCase):
    def _event(self, **overrides) -> AuditEvent:
        kwargs = dict(
            event_id="aud_1",
            sequence=1,
            occurred_at=NOW,
            actor=AuditActor.POLICY,
            event_type=AuditEventType.POLICY_DECIDED,
            summary="intent allowed",
            payload={"verdict": "allow", "amount": Money(50_000)},
        )
        kwargs.update(overrides)
        return AuditEvent(**kwargs)

    def test_digest_is_computed_not_accepted(self):
        event = self._event()
        self.assertEqual(len(event.payload_digest), 64)
        self.assertEqual(event.payload_digest, digest(canonicalize(dict(event.payload))))

    def test_a_supplied_digest_that_disagrees_is_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._event(payload_digest="0" * 64)

    def test_credential_shaped_payload_key_rejected(self):
        with self.assertRaises(SecretLeakError):
            self._event(payload={"key_secret": "rzp_test_xxx"})

    def test_float_payload_rejected(self):
        with self.assertRaises(NonCanonicalValueError):
            self._event(payload={"rate": 0.15})

    def test_payload_is_immutable(self):
        event = self._event()
        with self.assertRaises(TypeError):
            event.payload["verdict"] = "block"  # type: ignore[index]

    def test_event_cannot_be_mutated(self):
        event = self._event()
        with self.assertRaises(Exception):
            event.summary = "rewritten"  # type: ignore[misc]

    def test_sequence_must_be_a_non_negative_int(self):
        with self.assertRaises(DomainValidationError):
            self._event(sequence=-1)
        with self.assertRaises(DomainValidationError):
            self._event(sequence=True)

    def test_naive_timestamp_rejected(self):
        with self.assertRaises(DomainValidationError):
            self._event(occurred_at=datetime(2026, 8, 20, 10, 0, 0))


if __name__ == "__main__":
    unittest.main()
