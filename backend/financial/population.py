"""Population selection — deciding which payments a calculation is about.

Kept in one module because "which transactions counted?" is the question behind
every disagreement about a financial number. Selection is explicit, named, and
reusable rather than re-expressed as an inline comprehension at each call site.

All functions are pure: no clock, no I/O, no randomness (PROJECT_RULES 4.1).
"""

from typing import Iterable, Tuple, Union

from ..domain.errors import DomainValidationError
from ..domain.payment import EnrichedPayment, Payment
from ..domain.window import TimeWindow

PaymentLike = Union[Payment, EnrichedPayment]


def as_payment(item: PaymentLike) -> Payment:
    """Extract the observed ``Payment`` from a payment or an enriched payment.

    Lets every calculation accept either shape without duplicating logic, while
    keeping observed fact (``Payment``) distinct from inference
    (``PaymentEnrichment``) — see PROJECT_RULES 2.6.
    """
    if isinstance(item, Payment):
        return item
    if isinstance(item, EnrichedPayment):
        return item.payment
    raise DomainValidationError(
        f"expected Payment or EnrichedPayment, got {type(item).__name__}"
    )


def normalize(items: Iterable[PaymentLike]) -> Tuple[Payment, ...]:
    """Materialise an iterable of payment-likes into observed payments.

    Materialising matters: a generator consumed twice silently yields different
    populations to two calculations that are supposed to agree.
    """
    return tuple(as_payment(item) for item in items)


def in_window(items: Iterable[PaymentLike], window: TimeWindow) -> Tuple[PaymentLike, ...]:
    """Filter to payments whose event time falls inside the half-open window.

    Uses ``created_at`` (event time from the source of truth), never ingestion
    time (PROJECT_RULES 2.8).
    """
    if not isinstance(window, TimeWindow):
        raise DomainValidationError("in_window() requires a TimeWindow")
    return tuple(item for item in items if window.contains(as_payment(item).created_at))


def failures(items: Iterable[PaymentLike]) -> Tuple[PaymentLike, ...]:
    """Filter to terminally failed payments."""
    return tuple(item for item in items if as_payment(item).is_failure)


def successes(items: Iterable[PaymentLike]) -> Tuple[PaymentLike, ...]:
    """Filter to payments that reached money-in."""
    return tuple(item for item in items if as_payment(item).is_success)


def decided(items: Iterable[PaymentLike]) -> Tuple[PaymentLike, ...]:
    """Filter to payments that reached a terminal outcome.

    This is the rate denominator population. In-flight payments are excluded —
    see ARCHITECTURE.md 7.2 for why including them masks real incidents.
    """
    return tuple(item for item in items if as_payment(item).is_decided)


def assert_single_currency(items: Iterable[PaymentLike]) -> None:
    """Raise if the population mixes currencies.

    Summing across currencies produces a meaningless number, so we refuse
    rather than produce one (PROJECT_RULES 4.6).
    """
    seen = {as_payment(item).currency for item in items}
    if len(seen) > 1:
        raise DomainValidationError(
            f"population mixes currencies: {sorted(c.value for c in seen)}"
        )
