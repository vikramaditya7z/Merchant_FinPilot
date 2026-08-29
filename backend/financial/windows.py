"""Time bucketing.

Windows are half-open and tile exactly, so a payment lands in precisely one
bucket. Off-by-one bucketing quietly double-counts transactions at boundaries
and corrupts every rate downstream, so the tiling is asserted rather than
assumed.
"""

from datetime import timedelta
from typing import Iterable, List, Tuple

from ..domain.errors import DomainValidationError
from ..domain.metrics import WindowCounts
from ..domain.window import TimeWindow
from .counts import count_transactions
from .population import PaymentLike, as_payment, in_window


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

    Optimized: When windows form a contiguous uniform grid (e.g. 720 hourly
    buckets), bucketing executes in a single O(N) pass using timestamp index
    arithmetic rather than O(W * N) full scans.
    """
    win_tuple = tuple(windows)
    if not win_tuple:
        return ()

    population = tuple(items)
    if not population:
        return tuple(
            WindowCounts(window=w, counts=count_transactions(()))
            for w in win_tuple
        )

    # Check if windows form a contiguous uniform grid
    first_w = win_tuple[0]
    step_sec = first_w.duration_seconds
    is_uniform_grid = (
        step_sec > 0
        and len(win_tuple) > 1
        and all(w.duration_seconds == step_sec for w in win_tuple)
        and all(win_tuple[i].end == win_tuple[i + 1].start for i in range(len(win_tuple) - 1))
    )

    if is_uniform_grid:
        num_windows = len(win_tuple)
        grid_start_dt = win_tuple[0].start
        grid_end_dt = win_tuple[-1].end
        grid_start_ts = grid_start_dt.timestamp()

        # Group payments into bucket lists
        bucket_items: List[List[PaymentLike]] = [[] for _ in range(num_windows)]
        for item in population:
            p = as_payment(item)
            if grid_start_dt <= p.created_at < grid_end_dt:
                idx = int((p.created_at.timestamp() - grid_start_ts) // step_sec)
                if 0 <= idx < num_windows:
                    bucket_items[idx].append(item)

        return tuple(
            WindowCounts(window=win_tuple[i], counts=count_transactions(bucket_items[i]))
            for i in range(num_windows)
        )

    return tuple(
        WindowCounts(window=window, counts=count_transactions(in_window(population, window)))
        for window in win_tuple
    )
