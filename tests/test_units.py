import pytest
from hypothesis import given
from hypothesis import strategies as st

from market.units import (
    PRICE_SCALE,
    InvalidUnitError,
    complement,
    format_price,
    format_qty,
    parse_price,
    parse_qty,
)


def test_parse_price_exact():
    assert parse_price("0.0300") == 300
    assert parse_price("1") == PRICE_SCALE
    assert parse_price("0") == 0
    assert parse_price("0.5") == 5000


@pytest.mark.parametrize(
    "bad", ["0.03001", "1.0001", "-0.01", "abc", "NaN", "Infinity", "", 0.03, 3, None]
)
def test_parse_price_rejects(bad):
    with pytest.raises(InvalidUnitError):
        parse_price(bad)


def test_parse_qty():
    assert parse_qty("75.00") == 7500
    assert parse_qty("9.5") == 950
    assert parse_qty("-54.00", allow_negative=True) == -5400
    with pytest.raises(InvalidUnitError):
        parse_qty("-54.00")
    with pytest.raises(InvalidUnitError):
        parse_qty("0.001")  # sub-1/100 contract: would be silently rounded -> reject


@given(st.integers(0, PRICE_SCALE))
def test_price_roundtrip(p):
    assert parse_price(format_price(p)) == p


@given(st.integers(-(10**9), 10**9))
def test_qty_roundtrip(q):
    assert parse_qty(format_qty(q), allow_negative=True) == q


@given(st.integers(0, PRICE_SCALE))
def test_complement_involution(p):
    assert complement(complement(p)) == p
    assert p + complement(p) == PRICE_SCALE
