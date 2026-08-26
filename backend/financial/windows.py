"""Time bucketing.

Windows are half-open and tile exactly, so a payment lands in precisely one
bucket. Off-by-one bucketing quietly double-counts transactions at boundaries
and corrupts every rate downstream, so the tiling is asserted rather than
assumed.
"""

from datetime import timedelta
from typing import Iterable, Tuple

from ..domain.errors import DomainValidationError
from ..domain.metrics import WindowCounts
from ..domain.window import TimeWindow
from .counts import count_transactions
from .population import PaymentLike, in_window


def split_into_buckets(window: TimeWindow, bucket_seconds: int) -> Tuple[TimeWindow, ...]:
    """Tile ``window`` into consecutive buckets of ``bucket_seconds``.

    The window length must be an exact multiple of the bucket length. A partial
    trailing bucket would carry less traffic than the others and silently skew
    any per-bucket baseline, so it is rejected instead of truncated.
    """
    if isinstance(bucket_seconds, bool) or not isinstance(bucket_seconds, int):
        raise DomainValidationError("bucket_seconds must be an int")
    if bucket_seconds <= 0:
        raise DomainValidationError("bucket_seconds must be positive")
    total = window.duration_seconds
    if total % bucket_seconds != 0:
        raise DomainValidationError(
            f"window of {total}s does not divide evenly into {bucket_seconds}s buckets"
        )
    step = timedelta(seconds=bucket_seconds)
    buckets = []
    cursor = window.start
    while cursor < window.end:
        buckets.append(TimeWindow(cursor, cursor + step))
        cursor += step
    return tuple(buckets)


def hourly_buckets(window: TimeWindow) -> Tuple[TimeWindow, ...]:
    """Tile a window into one-hour buckets."""
    return split_into_buckets(window, 3600)


def preceding_windows(window: TimeWindow, count: int) -> Tuple[TimeWindow, ...]:
    """The ``count`` equal-length windows immediately before ``window``.

    Ordered oldest first. Used to build a baseline period that, by construction,
    excludes the incident window from its own baseline
    (ARCHITECTURE.md 7.3).
    """
    if isinstance(count, bool) or not isinstance(count, int):
        raise DomainValidationError("count must be an int")
    if count < 1:
        raise DomainValidationError("count must be at least 1")
    return tuple(window.preceding(offset) for offset in range(count, 0, -1))


def bucket_counts(
    items: Iterable[PaymentLike], windows: Iterable[TimeWindow]
) -> Tuple[WindowCounts, ...]:
    """Count transactions in each window.

    Materialises the population once so every bucket sees the same data.
    Windows need not be contiguous — comparable-window selection produces
    deliberately non-contiguous sets.
    """
    population = tuple(items)
    return tuple(
        WindowCounts(window=window, counts=count_transactions(in_window(population, window)))
        for window in windows
    )
