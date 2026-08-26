"""Deviation from baseline.

Two measures, because neither alone is meaningful:

* **Absolute (percentage points)** — the size of the move. Answers "how much
  worse?"
* **Relative (lift ratio)** — the proportion of the move. Answers "how many
  times worse?"

A 0.2pp rise on a 0.1pp baseline is a 3x lift and probably noise. A 5pp rise on
a 2pp baseline is a smaller lift and a genuine incident. Reporting only one of
the two produces confident nonsense in one direction or the other.

Both are ``Decimal``. Neither decides anything: thresholds live in
``detection/`` (ADR-006).
"""

from decimal import Decimal, ROUND_HALF_UP, localcontext
from typing import Optional

from ..domain.errors import DomainValidationError
from ..domain.metrics import Deviation, Rate, RATE_PRECISION

# Output precision for deviation figures. Enough to distinguish any real move,
# few enough digits that stored values stay readable and stable.
DEVIATION_DP = 6

_QUANTUM = Decimal(1).scaleb(-DEVIATION_DP)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_UP)


def absolute_deviation_pp(current: Rate, baseline: Rate) -> Decimal:
    """Signed difference in percentage points. Positive means worse than baseline."""
    if not isinstance(current, Rate) or not isinstance(baseline, Rate):
        raise DomainValidationError("absolute_deviation_pp() requires two Rate values")
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        return _quantize((current.value - baseline.value) * 100)


def relative_lift(current: Rate, baseline: Rate) -> Optional[Decimal]:
    """Ratio of current to baseline, or ``None`` when the baseline is zero.

    A zero baseline makes the ratio undefined, not infinite. Returning ``None``
    forces the caller to say "the baseline was zero" instead of rendering an
    invented multiplier (ADR-004).
    """
    if not isinstance(current, Rate) or not isinstance(baseline, Rate):
        raise DomainValidationError("relative_lift() requires two Rate values")
    if baseline.numerator == 0:
        return None
    with localcontext() as ctx:
        ctx.prec = RATE_PRECISION
        return _quantize(current.value / baseline.value)


def compute_deviation(current: Rate, baseline: Rate) -> Deviation:
    """Full deviation measurement of ``current`` against ``baseline``."""
    return Deviation(
        current=current,
        baseline=baseline,
        absolute_percentage_points=absolute_deviation_pp(current, baseline),
        relative_lift=relative_lift(current, baseline),
    )
