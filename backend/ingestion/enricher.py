"""Payment enricher.

PROJECT_RULES 2.6, 2.7 / ARCHITECTURE.md §12.

Populates derived dimensions (region, provider, segment, failure_category) joined
to a Payment as PaymentEnrichment. Maintains the strict structural boundary
between observed facts (SourceConfidence.OBSERVED) and derived enrichment
(SourceConfidence.ENRICHED).
"""

from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from ..domain.enums import FailureCategory, PaymentStatus, SourceConfidence
from ..domain.errors import DomainValidationError
from ..domain.payment import EnrichedPayment, Payment, PaymentEnrichment

FAILURE_KEYWORD_TAXONOMY = (
    (("insufficient_funds", "low_balance", "balance_insufficient", "no_money"), FailureCategory.INSUFFICIENT_FUNDS),
    (("auth_failed", "authentication_failed", "authentication", "otp", "mpin", "pin_invalid", "2fa"), FailureCategory.AUTHENTICATION_FAILED),
    (("gateway_timeout", "timed_out", "timeout", "latency", "bank_timeout"), FailureCategory.TIMEOUT),
    (("issuer_unavailable", "bank_down", "bank_error", "node_offline", "cbs_down"), FailureCategory.ISSUER_UNAVAILABLE),
    (("gateway_error", "gateway_unavailable", "downstream_failure", "internal_server_error"), FailureCategory.GATEWAY_ERROR),
    (("invalid_instrument", "card_expired", "invalid_vpa", "invalid_card", "expired_card", "invalid_account"), FailureCategory.INVALID_INSTRUMENT),
    (("risk_blocked", "fraud", "velocity_exceeded", "blacklist", "suspicious"), FailureCategory.RISK_BLOCKED),
    (("customer_dropped", "cancelled", "user_cancelled", "user_abort", "dropped"), FailureCategory.CUSTOMER_DROPPED),
)


class PaymentEnricher:
    """Derives and attaches dimensions to a canonical Payment."""

    def enrich(
        self,
        payment: Payment,
        raw_payload: Optional[Mapping[str, Any]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> EnrichedPayment:
        """Enrich a Payment with derived dimensions.

        Args:
            payment: The validated domain Payment instance.
            raw_payload: Optional original raw payload containing vendor-specific tags.
            metadata: Optional external context dictionary (e.g. merchant or geo tags).

        Returns:
            An ``EnrichedPayment`` containing the original payment and its enrichment.
        """
        if not isinstance(payment, Payment):
            raise DomainValidationError(
                f"payment must be a Payment instance, got {type(payment).__name__}"
            )

        payload = raw_payload or {}
        meta = metadata or {}

        # 1. Region
        region = self._extract_region(payload, meta)

        # 2. Provider
        provider = self._extract_provider(payload, meta)

        # 3. Segment
        segment = self._extract_segment(payload, meta)

        # 4. Failure Category
        failure_category = self._classify_failure_category(payment, payload, meta)

        enrichment = PaymentEnrichment(
            payment_id=payment.id,
            region=region,
            provider=provider,
            segment=segment,
            failure_category=failure_category,
            source_confidence=SourceConfidence.ENRICHED,
        )

        return EnrichedPayment(payment=payment, enrichment=enrichment)

    def enrich_many(
        self,
        payments: Sequence[Payment],
        raw_payloads: Optional[Sequence[Mapping[str, Any]]] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> Tuple[EnrichedPayment, ...]:
        """Enrich a sequence of Payment instances."""
        enriched = []
        payload_list = list(raw_payloads) if raw_payloads is not None else []
        for idx, p in enumerate(payments):
            raw_p = payload_list[idx] if idx < len(payload_list) else None
            enriched.append(self.enrich(p, raw_payload=raw_p, metadata=metadata))
        return tuple(enriched)

    # -------------------------------------------------------------------------
    # Internal Dimension Extractors
    # -------------------------------------------------------------------------

    def _extract_region(
        self, payload: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> Optional[str]:
        raw = (
            payload.get("region")
            or payload.get("geography")
            or payload.get("geo")
            or payload.get("state")
            or payload.get("location")
            or metadata.get("region")
            or metadata.get("geography")
        )
        if raw is not None and isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def _extract_provider(
        self, payload: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> Optional[str]:
        acquirer_bank = None
        acquirer_data = payload.get("acquirer_data")
        if isinstance(acquirer_data, Mapping):
            acquirer_bank = (
                acquirer_data.get("bank")
                or acquirer_data.get("provider")
                or acquirer_data.get("bank_name")
            )

        raw = (
            payload.get("provider")
            or acquirer_bank
            or payload.get("bank")
            or payload.get("acquirer")
            or payload.get("gateway")
            or payload.get("network")
            or metadata.get("provider")
            or metadata.get("bank")
        )
        if raw is not None and isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def _extract_segment(
        self, payload: Mapping[str, Any], metadata: Mapping[str, Any]
    ) -> Optional[str]:
        raw = (
            payload.get("segment")
            or payload.get("tier")
            or payload.get("customer_tier")
            or payload.get("merchant_segment")
            or metadata.get("segment")
            or metadata.get("tier")
        )
        if raw is not None and isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def _classify_failure_category(
        self,
        payment: Payment,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> Optional[FailureCategory]:
        if payment.status is not PaymentStatus.FAILED:
            return None

        # 1. Explicit in payload or metadata
        explicit = (
            payload.get("failure_category")
            or metadata.get("failure_category")
        )
        if explicit is not None:
            if isinstance(explicit, FailureCategory):
                return explicit
            if isinstance(explicit, str) and explicit.strip():
                try:
                    return FailureCategory(explicit.strip().lower())
                except ValueError:
                    pass

        # 2. Derive from error fields using taxonomy search
        error_signals = " ".join(
            filter(
                None,
                (
                    payment.error_code,
                    payment.error_reason,
                    payment.error_description,
                    payment.error_step,
                    payment.error_source,
                ),
            )
        ).lower()

        if error_signals:
            for keywords, category in FAILURE_KEYWORD_TAXONOMY:
                if any(kw in error_signals for kw in keywords):
                    return category

        return FailureCategory.UNKNOWN
