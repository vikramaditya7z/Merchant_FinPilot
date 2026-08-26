"""Success and failure rates.

The canonical implementation. Anything that needs a failure rate imports from
here; a second implementation elsewhere is a rule violation even if it agrees
today (PROJECT_RULES 4.2).

Both rates are computed independently from the counts, and their sum is exactly
``1`` at the working precision — an invariant asserted in the test suite rather
than assumed.
"""

from typing import Optional

from ..domain.metrics import Rate, TransactionCounts


def failure_rate(counts: TransactionCounts) -> Optional[Rate]:
    """Failed over decided, or ``None`` when nothing has been decided.

    ``None``, never ``0``: with no terminal outcomes there is no failure rate to
    report, and reporting 0% would read as "healthy" when the truth is "we do
    not know" (ADR-004). This is what makes ``INSUFFICIENT_DATA`` a real outcome.
    """
    return Rate.of(counts.failed, counts.decided)


def success_rate(counts: TransactionCounts) -> Optional[Rate]:
    """Succeeded over decided, or ``None`` when nothing has been decided."""
    return Rate.of(counts.succeeded, counts.decided)
