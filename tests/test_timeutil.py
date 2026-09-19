from datetime import UTC, datetime, timedelta, timezone

import pytest
from hypothesis import given
from hypothesis import strategies as st

from market.timeutil import from_epoch_ms, from_epoch_ns, parse_iso8601, to_epoch_us


def test_parse_z_suffix_microseconds():
    dt = parse_iso8601("2026-09-19T06:31:35.366305Z")
    assert dt == datetime(2026, 9, 19, 6, 31, 35, 366305, tzinfo=UTC)


def test_parse_truncates_nanoseconds():
    assert parse_iso8601("2026-09-19T06:31:35.366305999Z").microsecond == 366305


def test_parse_offset_converted_to_utc():
    dt = parse_iso8601("2026-09-19T12:01:35+05:30")
    assert dt.utcoffset() == timedelta(0) and dt.hour == 6


def test_naive_rejected():
    with pytest.raises(ValueError):
        parse_iso8601("2026-09-19T06:31:35")


def test_epoch_ms_exact():
    dt = from_epoch_ms(1669149841123)
    assert dt == datetime(2022, 11, 22, 20, 44, 1, 123000, tzinfo=UTC)


@given(st.integers(0, 4_000_000_000_000))
def test_ms_roundtrip(ms):
    assert to_epoch_us(from_epoch_ms(ms)) == ms * 1000


def test_ns():
    assert from_epoch_ns(1_669_149_841_123_456_789).microsecond == 123456


def test_to_epoch_us_tz_aware_only():
    with pytest.raises(ValueError):
        to_epoch_us(datetime(2026, 1, 1))
    est = timezone(timedelta(hours=-5))
    assert to_epoch_us(datetime(2026, 1, 1, tzinfo=est)) == to_epoch_us(
        datetime(2026, 1, 1, 5, tzinfo=UTC)
    )
