"""Time windows.

Every financial figure in this system is scoped to a window, so the window is a
first-class contract rather than a pair of loose datetimes.

Conventions, fixed once here:

* All datetimes are timezone-aware and UTC. Naive datetimes are rejected.
* Windows are half-open: ``[start, end)``. Half-open intervals tile without
  double-counting a transaction that lands exactly on a boundary — which, with
  hourly buckets and second-resolution timestamps, happens constantly.
* Time is always injected, never read from the clock inside a calculation
  (PROJECT_RULES 4.1).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .errors import DomainValidationError

UTC = timezone.utc


def require_utc(value: datetime, field: str = "timestamp") -> datetime:
    """Validate that ``value`` is timezone-aware, and normalise it to UTC."""
    if not isinstance(value, datetime):
        raise DomainValidationError(f"{field} must be a datetime, got {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(
            f"{field} must be timezone-aware; naive datetimes are ambiguous"
        )
    return value.astimezone(UTC)


def from_unix_seconds(seconds: int) -> datetime:
    """Convert a Razorpay ``created_at`` unix timestamp to an aware UTC datetime."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise DomainValidationError(f"unix timestamp must be an int, got {seconds!r}")
    return datetime.fromtimestamp(seconds, tz=UTC)


def to_unix_seconds(value: datetime) -> int:
    """Convert an aware datetime to whole unix seconds."""
    return int(require_utc(value).timestamp())


@dataclass(frozen=True)
class TimeWindow:
    """A half-open UTC interval ``[start, end)``."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        start = require_utc(self.start, "TimeWindow.start")
        end = require_utc(self.end, "TimeWindow.end")
        if end <= start:
            raise DomainValidationError(
                f"TimeWindow.end must be after start (got {start.isoformat()} .. {end.isoformat()})"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @classmethod
    def of_hours(cls, start: datetime, hours: int) -> "TimeWindow":
        if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
            raise DomainValidationError("hours must be a positive int")
        start = require_utc(start)
        return cls(start, start + timedelta(hours=hours))

    @classmethod
    def ending_at(cls, end: datetime, hours: int) -> "TimeWindow":
        if isinstance(hours, bool) or not isinstance(hours, int) or hours <= 0:
            raise DomainValidationError("hours must be a positive int")
        end = require_utc(end)
        return cls(end - timedelta(hours=hours), end)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def duration_seconds(self) -> int:
        return int(self.duration.total_seconds())

    @property
    def start_hour_of_day(self) -> int:
        """UTC hour the window opens in. Used for same-hour baseline matching."""
        return self.start.hour

    def contains(self, moment: datetime) -> bool:
        """Half-open membership: ``start <= moment < end``."""
        moment = require_utc(moment)
        return self.start <= moment < self.end

    def overlaps(self, other: "TimeWindow") -> bool:
        return self.start < other.end and other.start < self.end

    def shifted_by(self, delta: timedelta) -> "TimeWindow":
        return TimeWindow(self.start + delta, self.end + delta)

    def preceding(self, count: int = 1) -> "TimeWindow":
        """The window of equal length ending where this one starts.

        ``count=1`` is immediately before; ``count=2`` is the one before that.
        """
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise DomainValidationError("count must be a positive int")
        return self.shifted_by(-self.duration * count)

    def label(self) -> str:
        return f"{self.start.isoformat()}/{self.end.isoformat()}"

    def __str__(self) -> str:
        return self.label()
