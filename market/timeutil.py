"""Timestamp normalisation.

Everything internal is a timezone-aware UTC ``datetime``.  Naive timestamps are
rejected: silently assuming a zone is exactly the kind of error that corrupts a
time-ordered dataset.  Exchange time (``ts_ms`` / ISO strings) and local receive
time (``time.time_ns()``) are kept as *separate* fields throughout the system.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


def now_ns() -> int:
    """Local wall-clock receive time in integer nanoseconds since the epoch."""
    return time.time_ns()


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 string that carries an explicit UTC offset (``Z`` or ``+hh:mm``)."""
    if not isinstance(value, str):
        raise ValueError(f"expected ISO-8601 string, got {type(value).__name__}: {value!r}")
    text = value.strip()
    # Python's parser accepts at most 6 fractional digits; the exchange may send more.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        for i, ch in enumerate(tail):
            if not ch.isdigit():
                rest = tail[i:]
                break
            digits += ch
        else:
            rest = ""
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp {value!r} has no UTC offset; refusing to guess a zone")
    return dt.astimezone(UTC)


def from_epoch_seconds(value: int | float) -> datetime:
    return datetime.fromtimestamp(value, UTC)


def from_epoch_ms(value: int) -> datetime:
    # integer arithmetic avoids float rounding on millisecond stamps
    return datetime.fromtimestamp(value // 1000, UTC).replace(microsecond=(value % 1000) * 1000)


def from_epoch_ns(value: int) -> datetime:
    return datetime.fromtimestamp(value // 1_000_000_000, UTC).replace(
        microsecond=(value % 1_000_000_000) // 1000
    )


def to_epoch_us(dt: datetime) -> int:
    """Microseconds since the epoch (the resolution of DuckDB/SQLite-friendly timestamps)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime")
    delta = dt.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds
