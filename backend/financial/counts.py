"""Transaction counting.

The foundation every rate rests on. The only interesting decision here is which
bucket a payment falls into, and that mapping lives in
``domain.enums.outcome_for_status`` so it is defined exactly once
(PROJECT_RULES 4.2).
"""

from typing import Iterable

from ..domain.enums import PaymentOutcome
from ..domain.metrics import TransactionCounts
from .population import PaymentLike, as_payment


def count_transactions(items: Iterable[PaymentLike]) -> TransactionCounts:
    """Count payments by rate-relevant outcome.

    Returns all-zero counts for an empty population. That is a genuine zero
    (nothing happened), which is different from an undefined *rate* over an
    empty population — see PROJECT_RULES 1.7.
    """
    succeeded = 0
    failed = 0
    undecided = 0
    for item in items:
        outcome = as_payment(item).outcome
        if outcome is PaymentOutcome.SUCCEEDED:
            succeeded += 1
        elif outcome is PaymentOutcome.FAILED:
            failed += 1
        else:
            undecided += 1
    return TransactionCounts(succeeded=succeeded, failed=failed, undecided=undecided)
