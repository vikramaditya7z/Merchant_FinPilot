"""Revenue exposure — the money at risk.

The numbers a merchant actually cares about, and therefore the numbers the LLM is
most tempted to invent. Every one of them is computed here, from source records,
and re-derived by the Financial Verifier before any action
(PROJECT_RULES 1.2/1.3).

Three quantities, deliberately not conflated (ARCHITECTURE.md 7.5):

* ``failed_gmv`` — observed. What failed, in total.
* ``excess_failed_transactions`` / ``revenue_at_risk`` — derived. What failed
  *beyond normal*, and what that is worth.
* ``RecoverableRevenue`` — an **estimate**, constructible only with an explicit
  assumption.

Conflating the first two overstates the loss enormously: most of ``failed_gmv``
is the failure volume a healthy business always has.
"""

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Iterable, Optional

from ..domain.errors import DomainValidationError
from ..domain.metrics import (
    RATE_PRECISION,
    Rate,
    RecoverableRevenue,
    RecoveryAssumption,
    RevenueRisk,
    TransactionCounts,
)
from ..domain.money import Money, sum_money
from ..domain.enums import Currency
from .population import PaymentLike, as_payment, assert_single_currency, failures


def failed_gmv(items: Iterable[PaymentLike], currency: Currency = Currency.INR) -> Money:
    """Total amount of failed payments. An observed fact, exact."""
    population = tuple(items)
    assert_single_currency(population)
    return sum_money((as_payment(item).amount for item in failures(population)), currency)


def mean_failed_ticket(
    items: Iterable[PaymentLike], currency: Currency = Currency.INR
) -> Optional[Money]:
    """Average value of a failed payment, or ``None`` if nothing failed.

    ``None`` rather than zero: with no failures there is no average ticket, and a
    zero would silently zero out any figure derived from it (ADR-004).
    """
    population = tuple(items)
    failed = failures(population)
    if not failed:
        return None
    total = sum_money((as_payment(item).amount for item in failed), currency)
    return total.divide_by_int(len(failed))


def expected_failures(counts: TransactionCounts, baseline: Rate) -> int:
    """How many failures the baseline predicts for this population size.

    Rounded once, half-up, to a whole transaction.
    """
    if not isinstance(counts, TransactionCounts):
        raise DomainValidationError("expected_failures() requires TransactionCounts")
    if not isinstance(baseline, Rate):
        raise DomainValidationError("expected_failures() requires a Rate baseline")
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        predicted = baseline.value * Decimal(counts.decided)
    return int(predicted.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def excess_failed_transactions(counts: TransactionCounts, baseline: Rate) -> int:
    """Failures above what the baseline predicts, clamped at zero.

    Clamping matters: when the current rate is *better* than baseline the excess
    is zero, not negative. A negative excess would flow into a negative
    ``revenue_at_risk``, which is meaningless — the business did not earn extra
    revenue because fewer payments failed than usual (PROJECT_RULES 1.8).
    """
    excess = counts.failed - expected_failures(counts, baseline)
    return max(0, excess)


def revenue_at_risk(
    items: Iterable[PaymentLike],
    counts: TransactionCounts,
    baseline: Rate,
    currency: Currency = Currency.INR,
) -> Money:
    """Value of the excess failures — the money this degradation is costing.

    Conceptually ``excess_failures x mean_failed_ticket``. Implemented as
    ``failed_gmv x (excess / failed)``, which is the same quantity with a
    single rounding step at the end rather than one rounding inside the mean and
    another on the product (PROJECT_RULES 4.5).
    """
    population = tuple(items)
    excess = excess_failed_transactions(counts, baseline)
    if excess == 0:
        return Money.zero(currency)

    failed_count = counts.failed
    if failed_count == 0:
        # Unreachable: excess > 0 implies failed > 0. Explicit so a future
        # change to the clamp cannot produce a divide-by-zero.
        raise DomainValidationError("excess failures without any failed transactions")

    total_failed_value = failed_gmv(population, currency)
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        share = Decimal(excess) / Decimal(failed_count)
    return total_failed_value.multiply_by_ratio(share)


def recoverable_revenue(
    revenue_at_risk_amount: Money, assumption: RecoveryAssumption
) -> RecoverableRevenue:
    """Apply an explicit recovery assumption to produce an **estimate**.

    There is no default assumption and no way to call this without one
    (ADR-007). Recovery rate is not observable from payment data, and a defaulted
    assumption would silently become a fact in the merchant's dashboard.
    """
    if not isinstance(revenue_at_risk_amount, Money):
        raise DomainValidationError("recoverable_revenue() requires a Money amount")
    if not isinstance(assumption, RecoveryAssumption):
        raise DomainValidationError(
            "recoverable_revenue() requires an explicit RecoveryAssumption"
        )
    return RecoverableRevenue(
        amount=revenue_at_risk_amount.multiply_by_ratio(assumption.rate),
        assumption=assumption,
    )


def compute_revenue_risk(
    items: Iterable[PaymentLike],
    counts: TransactionCounts,
    baseline: Optional[Rate],
    currency: Currency = Currency.INR,
    assumption: Optional[RecoveryAssumption] = None,
) -> Optional[RevenueRisk]:
    """Assemble the full exposure picture for a window.

    Returns ``None`` when there is no usable baseline: without one, "excess"
    failures are undefined, and quoting a loss figure would be a guess dressed as
    a fact (PROJECT_RULES 1.10).
    """
    if baseline is None:
        return None
    population = tuple(items)
    total_failed_value = failed_gmv(population, currency)
    excess = excess_failed_transactions(counts, baseline)
    at_risk = revenue_at_risk(population, counts, baseline, currency)
    mean_ticket = mean_failed_ticket(population, currency) or Money.zero(currency)

    return RevenueRisk(
        failed_gmv=total_failed_value,
        excess_failed_transactions=excess,
        mean_failed_ticket=mean_ticket,
        revenue_at_risk=at_risk,
        recoverable=(
            recoverable_revenue(at_risk, assumption) if assumption is not None else None
        ),
    )
