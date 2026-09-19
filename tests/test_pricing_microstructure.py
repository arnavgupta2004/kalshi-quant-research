import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from market.order_book import OrderBook
from pricing.microstructure import (
    DEFAULT_DELAY_NS,
    SEC,
    TradeTape,
    book_features,
    hours_to_expiry,
    reference_price,
    trade_features,
)

T0 = 1_800_000_000 * SEC


def tape(rows):
    """rows: (seconds_from_T0, yes_price_ticks, contracts, 'yes'|'no'|None)"""
    return TradeTape.from_rows([(T0 + int(s * SEC), p, c * 100, side) for s, p, c, side in rows])


def book(yes=(), no=()):
    b = OrderBook("X")
    b.apply_snapshot(yes, no)
    return b


# ------------------------------------------------------------------ the tape and information time
def test_a_print_is_visible_only_after_its_delay():
    tp = tape([(10.0, 5000, 5, "yes")])
    assert tp.upto(T0 + 10 * SEC) == 0  # exchange time == now: not yet delivered
    assert tp.upto(T0 + 10 * SEC + DEFAULT_DELAY_NS - 1) == 0
    assert tp.upto(T0 + 10 * SEC + DEFAULT_DELAY_NS) == 1  # exactly at delivery: visible


def test_an_unsorted_tape_is_rejected_and_from_rows_sorts():
    with pytest.raises(ValueError):
        TradeTape(
            np.array([2, 1]), np.array([1, 1]), np.array([1, 1]), np.array([0, 0], dtype=np.int8)
        )
    tp = tape([(5, 5000, 1, "yes"), (1, 4000, 1, "no")])
    assert list(tp.yes_price) == [4000, 5000]


def test_no_trades_means_no_features_not_zero_features():
    f = trade_features(tape([]), T0 + 100 * SEC)
    assert f.n_seen == 0 and f.last_price is None and f.tmid is None
    assert f.vwap[300] is None and f.flow[300] is None and f.volume[300] == 0.0


# ------------------------------------------------------------------ what the features mean
def test_vwap_flow_and_returns_over_windows():
    tp = tape(
        [
            (0.0, 4000, 10, "no"),  # 1h ago-ish
            (3300.0, 5000, 10, "yes"),
            (3500.0, 6000, 30, "yes"),
        ]
    )
    t = T0 + 3600 * SEC
    f = trade_features(tp, t)
    assert f.n_seen == 3 and f.last_price == pytest.approx(0.6)
    # 5-minute window (now-300s..): prints at 3300 and 3500 only (3300 > 3600-0.25-300)
    assert f.volume[300] == pytest.approx(40.0)
    assert f.vwap[300] == pytest.approx((5000 * 10 + 6000 * 30) / 40 / 10_000)
    assert f.flow[300] == pytest.approx(1.0)  # all buying YES
    # 1-hour window includes the first print (a seller of YES): flow = (40 - 10) / 50
    assert f.flow[3600] == pytest.approx(30 / 50)
    assert f.ret[300] == pytest.approx(0.6 - 0.4)  # price as of t-300s was the 4000 print
    assert f.secs_since_last == pytest.approx(3600 - 0.25 - 3500)


def test_taker_sides_read_the_ask_and_the_bid():
    """taker YES lifts the ask, taker NO hits the bid: the two together imply a mid and a spread."""
    tp = tape([(1.0, 5300, 4, "yes"), (2.0, 5000, 4, "no")])
    f = trade_features(tp, T0 + 10 * SEC)
    assert f.tmid == pytest.approx(0.515) and f.tspread == pytest.approx(0.03)
    only_ask = trade_features(tape([(1.0, 5300, 4, "yes")]), T0 + 10 * SEC)
    assert only_ask.tmid is None  # one side alone implies no mid


def test_a_stale_side_of_the_tape_does_not_imply_a_mid():
    tp = tape([(1.0, 5300, 4, "yes"), (2000.0, 5000, 4, "no")])
    assert trade_features(tp, T0 + 2001 * SEC).tmid is None  # the ask print is > 10 minutes old


def test_prints_without_a_taker_side_carry_volume_but_no_flow():
    tp = tape([(1.0, 5000, 10, None)])
    f = trade_features(tp, T0 + 10 * SEC)
    assert f.volume[300] == pytest.approx(10.0) and f.flow[300] is None


# ------------------------------------------------------------------ causality
@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(
            st.floats(0, 5000),
            st.integers(100, 9900),
            st.integers(1, 50),
            st.sampled_from(["yes", "no", None]),
        ),
        min_size=0,
        max_size=25,
    ),
    st.floats(0, 6000),
)
def test_features_at_t_depend_only_on_prints_visible_at_t(rows, t_s):
    """The prefix theorem: features from the full tape equal features from the tape truncated at t
    (plus the delivery delay).  A leak would show up as any difference."""
    full = tape(rows)
    t = T0 + int(t_s * SEC)
    keep = [r for r in rows if T0 + int(r[0] * SEC) <= t - DEFAULT_DELAY_NS]
    cut = tape(keep)
    a, b = trade_features(full, t), trade_features(cut, t)
    assert a == b


def test_a_print_after_t_changes_nothing():
    base = [(1.0, 5000, 3, "yes")]
    t = T0 + 50 * SEC
    assert trade_features(tape(base), t) == trade_features(tape([*base, (60.0, 9000, 99, "no")]), t)


# ------------------------------------------------------------------ the book
def test_book_features_microprice_and_imbalance():
    # YES bid 45c x 30, NO bid 50c x 10  ->  YES ask 50c x 10
    f = book_features(book(yes=[(4500, 3000)], no=[(5000, 1000)]))
    assert (f.bid, f.ask, f.mid, f.spread) == pytest.approx((0.45, 0.50, 0.475, 0.05))
    assert f.imb_top == pytest.approx((30 - 10) / 40)  # buyers dominate: positive
    # microprice leans toward the side with LESS size: thin ask -> price is pulled toward the ask
    assert f.microprice == pytest.approx((0.45 * 10 + 0.50 * 30) / 40)
    assert f.mid < f.microprice < f.ask
    assert f.log_depth == pytest.approx(math.log1p(40))


def test_a_one_sided_or_stale_book_gives_no_book_features():
    assert book_features(book(yes=[(4500, 1000)])) is None  # no NO bids -> no YES ask
    assert book_features(book(no=[(5000, 1000)])) is None
    assert book_features(None) is None
    assert book_features(book(yes=[(4500, 1000)], no=[(5000, 1000)]), age_s=31.0) is None
    assert book_features(book(yes=[(4500, 1000)], no=[(5000, 1000)]), age_s=29.0) is not None


@settings(max_examples=100, deadline=None)
@given(
    st.integers(1, 98),
    st.integers(1, 98),
    st.integers(1, 5000),
    st.integers(1, 5000),
)
def test_book_features_stay_inside_their_natural_bounds(yb, nb, qy, qn):
    if yb + nb > 99:  # keep the book uncrossed
        nb = 99 - yb
        if nb < 1:
            return
    f = book_features(book(yes=[(yb * 100, qy)], no=[(nb * 100, qn)]))
    assert 0 < f.bid < f.ask < 1 and f.bid <= f.mid <= f.ask and f.bid <= f.microprice <= f.ask
    assert -1 <= f.imb_top <= 1 and -1 <= f.imb_depth <= 1 and f.spread > 0


# ------------------------------------------------------------------ reference price
def test_reference_price_prefers_the_book_then_the_tape_mid_then_the_last_trade():
    bf = book_features(book(yes=[(4500, 1000)], no=[(5000, 1000)]))
    tf = trade_features(tape([(1.0, 5300, 4, "yes"), (2.0, 5000, 4, "no")]), T0 + 10 * SEC)
    assert reference_price(bf, tf) == (pytest.approx(0.475), "book_mid")
    assert reference_price(None, tf) == (pytest.approx(0.515), "trade_mid")
    last_only = trade_features(tape([(1.0, 7000, 4, "yes")]), T0 + 10 * SEC)
    assert reference_price(None, last_only) == (pytest.approx(0.70), "last_trade")
    assert reference_price(None, trade_features(tape([]), T0)) == (None, "none")


def test_hours_to_expiry_uses_the_scheduled_end_only():
    assert hours_to_expiry(T0 + 7200 * SEC, T0) == pytest.approx(2.0)
    assert hours_to_expiry(T0 - 1800 * SEC, T0) == pytest.approx(-0.5)  # a game running long
    assert hours_to_expiry(None, T0) is None
