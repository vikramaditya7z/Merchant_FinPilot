"""Statistical significance of a rate difference.

A two-proportion z-test. This is the module that tells the difference between a
real incident and 3 failures out of 12 — but only if the caller reads the whole
result. On its own, the p-value does **not** protect against thin data: 3 of 12
against a 5% baseline scores p=0.002. The arithmetic is right; the normal
approximation is not applicable at that sample size, because only 0.63 failures
were expected. ``SignificanceResult.min_expected_count`` is what makes that
visible, and it is why the result carries it.

**This is a measurement, not a detector** (ADR-006). It reports how unlikely the
observed difference is if nothing actually changed. It does not decide whether an
incident exists — that judgement, and its thresholds, live in ``detection/``
where they are configurable and auditable.

``float`` is used here, and only here. A z-score is a test statistic, never money
and never a rate; this is the one exception in PROJECT_RULES 1.6.
"""

import math
from typing import Optional

from ..domain.errors import DomainValidationError
from ..domain.metrics import Rate, SignificanceResult


def _standard_normal_cdf(x: float) -> float:
    """P(Z <= x) for a standard normal, via the error function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def two_proportion_significance(
    current: Rate, baseline: Rate
) -> Optional[SignificanceResult]:
    """Test whether two observed rates differ by more than sampling noise.

    Uses the pooled-proportion form of the two-proportion z-test::

        p_pool = (x1 + x2) / (n1 + n2)
        se     = sqrt(p_pool * (1 - p_pool) * (1/n1 + 1/n2))
        z      = (p1 - p2) / se

    Returns ``None`` when the test is undefined — specifically when the pooled
    standard error is zero, which happens if every observation in both samples
    fell the same way (all succeeded, or all failed). There is no distribution to
    test against there, and returning a z of 0 or infinity would both be lies.

    The p-value is two-sided: we care about an unusual move in either direction,
    and a one-sided test would understate the chance of a spurious result.

    The result also carries ``min_expected_count``, the smallest of the four
    expected cell counts under the pooled proportion. The normal approximation
    this test relies on degrades on thin data — 3 failures out of 12 against a 5%
    baseline yields p=0.002, which is arithmetically right and statistically
    unsupportable, because only 0.63 failures were expected. The p-value alone
    cannot express that; ``min_expected_count`` can, and the caller must read it.
    """
    if not isinstance(current, Rate) or not isinstance(baseline, Rate):
        raise DomainValidationError("two_proportion_significance() requires two Rate values")

    x1, n1 = current.numerator, current.denominator
    x2, n2 = baseline.numerator, baseline.denominator

    pooled = (x1 + x2) / (n1 + n2)
    variance = pooled * (1.0 - pooled) * ((1.0 / n1) + (1.0 / n2))
    if variance <= 0.0:
        return None

    standard_error = math.sqrt(variance)
    p1 = x1 / n1
    p2 = x2 / n2
    z_score = (p1 - p2) / standard_error
    p_value = 2.0 * (1.0 - _standard_normal_cdf(abs(z_score)))

    # Guard the floating-point tail: erf saturates for large |z| and can yield a
    # p-value a hair outside [0, 1], which the contract rejects.
    p_value = min(1.0, max(0.0, p_value))

    # The four expected cell counts of the implied 2x2 table under the null.
    # The smallest one governs whether the normal approximation holds at all.
    min_expected_count = min(
        pooled * n1, (1.0 - pooled) * n1, pooled * n2, (1.0 - pooled) * n2
    )

    return SignificanceResult(
        z_score=z_score,
        p_value=p_value,
        current_decided=n1,
        baseline_decided=n2,
        min_expected_count=min_expected_count,
    )
