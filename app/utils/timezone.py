"""IST-aware time helpers.

All daily caps and quiet-hour windows are evaluated in user-facing local
time (Asia/Kolkata), not UTC. These are pure functions — no I/O, no globals
— so they are directly unit-testable.
"""
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Current time in IST."""
    return datetime.now(IST)


def ist_today_start_utc() -> datetime:
    """UTC-naive datetime corresponding to 00:00 IST today.

    Used for daily-cap counting so the cap resets at midnight IST rather
    than at 05:30 IST (which is what a UTC midnight boundary would give).

    Returns:
        Naive datetime in UTC, comparable against stored `created_at` values.
    """
    midnight_ist = now_ist().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(timezone.utc).replace(tzinfo=None)


def is_within_window(hour: int, start: int, end: int) -> bool:
    """Whether `hour` falls inside the [start, end) window, wrapping midnight.

    Args:
        hour: Hour of day, 0-23.
        start: Window start hour, inclusive.
        end: Window end hour, exclusive.

    Returns:
        True if the hour is inside the window.

    Examples:
        >>> is_within_window(23, 22, 7)   # 22:00-07:00 wraps midnight
        True
        >>> is_within_window(12, 22, 7)
        False
    """
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end
