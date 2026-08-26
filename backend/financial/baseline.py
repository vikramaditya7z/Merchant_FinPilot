"""Baseline failure rate.

"Is this abnormal?" is only answerable against a baseline, and a badly chosen
baseline is the single largest source of false alarms in this kind of system.
Two guards address that (ARCHITECTURE.md 7.3):

* **Comparable windows.** Comparing 8pm traffic against a flat 24-hour average
  manufactures alarms, because evening failure rates differ structurally from
  4am rates. ``SAME_HOUR_OF_DAY`` compares like with like.
* **Minimum sample.** A baseline drawn from too little traffic is not a
  baseline. Below the threshold the result is ``None`` — no deviation claim may
  be made at all.

Both estimators are deterministic and return a real observed ``Rate`` with a
real numerator and denominator, so a baseline is always auditable.
"""

from typing import Optional, Sequence, Tuple

from ..domain.enums import BaselineMethod, ComparableWindowMode
from ..domain.errors import DomainValidationError
from ..domain.metrics import BaselineFailureRate, Rate, WindowCounts
from ..domain.window import TimeWindow

# Pooled decided transactions required before a baseline is trusted at all.
DEFAULT_MIN_BASELINE_DECIDED = 100

# Per-window decided transactions required for a window to contribute to the
# median estimator. A window with 3 transactions has a meaningless rate, and
# including it would let one quiet hour dominate the median.
DEFAULT_MIN_WINDOW_DECIDED = 20


def select_comparable_windows(
    candidates: Sequence[WindowCounts],
    target: TimeWindow,
    mode: ComparableWindowMode = ComparableWindowMode.ALL,
) -> Tuple[WindowCounts, ...]:
    """Choose which historical windows are fair comparisons for ``target``.

    Always excludes any window overlapping ``target`` — a window must never
    contribute to its own baseline.
    """
    if not isinstance(target, TimeWindow):
        raise DomainValidationError("target must be a TimeWindow")
    if not isinstance(mode, ComparableWindowMode):
        raise DomainValidationError(f"invalid ComparableWindowMode: {mode!r}")

    selected = []
    for candidate in candidates:
        if not isinstance(candidate, WindowCounts):
            raise DomainValidationError("candidates must contain WindowCounts")
        if candidate.window.overlaps(target):
            continue
        if mode is ComparableWindowMode.SAME_HOUR_OF_DAY:
            if candidate.window.start_hour_of_day != target.start_hour_of_day:
                continue
        selected.append(candidate)
    return tuple(selected)


def _pooled(
    windows: Sequence[WindowCounts], min_decided: int
) -> Tuple[Optional[Rate], int, int]:
    """Volume-weighted pooled rate. Returns (rate, windows_used, decided_sample)."""
    failed = 0
    decided = 0
    used = 0
    for window in windows:
        if window.counts.decided == 0:
            continue
        failed += window.counts.failed
        decided += window.counts.decided
        used += 1
    if decided < min_decided:
        return None, used, decided
    return Rate.of(failed, decided), used, decided


def _median_of_windows(
    windows: Sequence[WindowCounts], min_decided: int, min_window_decided: int
) -> Tuple[Optional[Rate], int, int]:
    """Median of per-window rates, robust to a single pathological window.

    The returned baseline is the **lower-median window's own rate**, not an
    interpolated average. That keeps the baseline a genuine observed rate with a
    real numerator and denominator (so it stays auditable and re-derivable), and
    makes the estimator deterministic for an even number of windows.
    """
    eligible = [w for w in windows if w.counts.decided >= min_window_decided]
    decided_sample = sum(w.counts.decided for w in eligible)
    if not eligible or decided_sample < min_decided:
        return None, len(eligible), decided_sample

    # Sort by rate value; tie-break on window start so the result never depends
    # on input ordering.
    ordered = sorted(
        eligible,
        key=lambda w: (w.failure_rate.value, w.window.start),  # failure_rate is not None here
    )
    lower_median_index = (len(ordered) - 1) // 2
    return ordered[lower_median_index].failure_rate, len(eligible), decided_sample


def baseline_failure_rate(
    windows: Sequence[WindowCounts],
    method: BaselineMethod = BaselineMethod.POOLED,
    min_decided: int = DEFAULT_MIN_BASELINE_DECIDED,
    min_window_decided: int = DEFAULT_MIN_WINDOW_DECIDED,
) -> BaselineFailureRate:
    """Estimate the normal failure rate from historical windows.

    Always returns a ``BaselineFailureRate``. When the sample is insufficient the
    contained ``rate`` is ``None`` and ``is_sufficient`` is ``False`` — the
    caller must then make no deviation claim (PROJECT_RULES 1.7).
    """
    if not isinstance(method, BaselineMethod):
        raise DomainValidationError(f"invalid BaselineMethod: {method!r}")
    for name, value in (("min_decided", min_decided), ("min_window_decided", min_window_decided)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DomainValidationError(f"{name} must be a non-negative int")

    windows = tuple(windows)
    for window in windows:
        if not isinstance(window, WindowCounts):
            raise DomainValidationError("windows must contain WindowCounts")

    if method is BaselineMethod.POOLED:
        rate, used, sample = _pooled(windows, min_decided)
    else:
        rate, used, sample = _median_of_windows(windows, min_decided, min_window_decided)

    return BaselineFailureRate(
        method=method,
        rate=rate,
        windows_considered=len(windows),
        windows_used=used,
        decided_sample=sample,
        min_decided_required=min_decided,
    )
