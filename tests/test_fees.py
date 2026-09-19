from fractions import Fraction

from hypothesis import given
from hypothesis import strategies as st

from market.fees import FeeBook, FeeSchedule, Rounding, series_of_ticker
from market.units import PRICE_SCALE as S

Q = FeeSchedule()


def test_matches_the_worked_example_in_kalshis_own_fee_documentation():
    """Docs: 'trade fee = ceil_6dp($0.00363825) = $0.003639' (1 contract at $0.055)."""
    assert Q.exact_fee_micro(550, 100) == Fraction(363825, 100)  # 3638.25 µ$
    assert Q.fee_micro(550, 100) == 3639  # $0.003639


def test_known_values_of_the_quadratic_model():
    assert Q.fee_micro(5000, 10_000) == 1_750_000  # 100 contracts at 50c: $1.75, the maximum
    assert Q.fee_micro(5000, 100) == 17_500  # 1 contract at 50c: $0.0175
    assert Q.fee_micro(1000, 100) == 6_300  # 0.07 * 0.10 * 0.90
    assert Q.fee_micro(0, 100) == 0 and Q.fee_micro(S, 100) == 0  # no fee at the extremes


@given(st.integers(1, S - 1), st.integers(1, 10**7))
def test_symmetric_about_fifty_cents_and_maximal_there(p, q):
    assert Q.exact_fee_micro(p, q) == Q.exact_fee_micro(S - p, q)
    assert Q.exact_fee_micro(p, q) <= Q.exact_fee_micro(5000, q)


@given(st.integers(1, S - 1), st.integers(1, 10**6), st.integers(1, 10**6))
def test_rounding_models_are_ordered_and_never_undercharge(p, q1, q2):
    exact = Q.exact_fee_micro(p, q1)
    six = Q.fee_micro(p, q1)
    cent = Q.with_rounding(Rounding.CENT_PER_FILL).fee_micro(p, q1)
    assert exact <= six < exact + 1  # ceil at 6dp: within one µ$
    assert six <= cent < six + 10_000  # whole-cent rounding is the conservative bound
    fills = [(p, q1), (p, q2)]
    per_order = Q.with_rounding(Rounding.CENT_PER_ORDER).order_fee_micro(fills)
    per_fill = Q.with_rounding(Rounding.CENT_PER_FILL).order_fee_micro(fills)
    assert per_order <= per_fill  # one cent-rounding beats two
    assert Q.order_fee_micro(fills) <= per_order


def test_maker_fee_only_on_the_maker_fee_type_and_is_a_quarter_of_taker():
    plain = FeeSchedule("quadratic")
    with_maker = FeeSchedule("quadratic_with_maker_fees")
    assert plain.fee_micro(5000, 10_000, maker=True) == 0
    assert with_maker.fee_micro(5000, 10_000, maker=True) == 437_500  # 0.0175 * 100 * 0.25
    assert with_maker.exact_fee_micro(3000, 500, maker=True) * 4 == with_maker.exact_fee_micro(
        3000, 500
    )


def test_multiplier_scales_the_fee():
    half = FeeSchedule.from_series("quadratic", "0.5")
    assert half.multiplier == Fraction(1, 2)
    assert half.exact_fee_micro(5000, 10_000) * 2 == Q.exact_fee_micro(5000, 10_000)


def test_unknown_fee_metadata_falls_back_and_says_so():
    assert FeeSchedule.from_series("quadratic", "1").known
    assert not FeeSchedule.from_series(None, None).known  # no metadata: assumed standard taker fee
    assert not FeeSchedule.from_series("flat_or_whatever", "1").known
    assert FeeSchedule.from_series(None, None).exact_fee_micro(5000, 100) == Q.exact_fee_micro(
        5000, 100
    )


def test_fee_book_resolves_through_the_series_and_supports_sensitivities():
    book = FeeBook.from_series_meta(
        {"KXBTCD": ("quadratic", "1"), "KXATPMATCH": ("quadratic_with_maker_fees", "1")}
    )
    assert series_of_ticker("KXBTCD-26SEP1904-T68699.99") == "KXBTCD"
    assert (
        book.for_ticker("KXBTCD-26SEP1904-T1").known and not book.for_ticker("KXUNSEEN-1-A").known
    )
    assert book.scaled(Fraction(0)).for_ticker("KXBTCD-X").fee_micro(5000, 10_000) == 0
    assert book.scaled(Fraction(2)).for_ticker("KXBTCD-X").multiplier == 2
    c = book.with_rounding(Rounding.CENT_PER_FILL)
    assert (
        c.for_ticker("KXBTCD-X").rounding is Rounding.CENT_PER_FILL
        and c.default.rounding is Rounding.CENT_PER_FILL
    )
