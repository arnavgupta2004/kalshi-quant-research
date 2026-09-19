import random

import pytest
from hypothesis import given
from hypothesis import strategies as st

from market.contracts import Side
from market.order_book import Level, OrderBook, OrderBookInconsistency
from market.units import PRICE_SCALE


def make():
    b = OrderBook("T")
    b.apply_snapshot([(4500, 300), (4000, 100)], [(5300, 150), (5000, 50)])
    return b


def test_bids_sorted_best_first_regardless_of_input_order():
    b = OrderBook("T")
    b.apply_snapshot([(100, 5), (300, 3), (200, 4)], [])
    assert [lv.price for lv in b.bids(Side.YES)] == [300, 200, 100]


def test_asks_are_derived_from_opposite_bids():
    b = make()
    # NO bid 0.53 x150  ==  YES ask 0.47 x150
    assert b.best_ask(Side.YES) == Level(4700, 150)
    assert b.best_ask(Side.NO) == Level(PRICE_SCALE - 4500, 300)
    assert [lv.price for lv in b.asks(Side.YES)] == [4700, 5000]  # ascending
    assert b.spread() == 4700 - 4500
    assert b.mid() == pytest.approx((0.45 + 0.47) / 2)
    assert not b.is_crossed()


def test_empty_book():
    b = OrderBook("T")
    assert b.best_bid() is None and b.best_ask() is None
    assert b.spread() is None and b.mid() is None and b.is_empty


def test_one_sided_book_has_no_mid_but_has_derived_ask():
    b = OrderBook("T")
    b.apply_snapshot([], [(9000, 10)])
    assert b.best_bid(Side.YES) is None
    assert b.best_ask(Side.YES) == Level(1000, 10)
    assert b.mid() is None


def test_delta_add_reduce_remove():
    b = make()
    b.apply_delta(Side.YES, 4500, 100)
    assert b.best_bid(Side.YES) == Level(4500, 400)
    b.apply_delta(Side.YES, 4500, -400)
    assert b.best_bid(Side.YES) == Level(4000, 100)
    b.apply_delta(Side.NO, 5100, 7)
    assert Level(5100, 7) in b.bids(Side.NO)


def test_delta_below_zero_raises_and_leaves_book_unchanged():
    b = make()
    before = b.as_levels()
    with pytest.raises(OrderBookInconsistency):
        b.apply_delta(Side.YES, 4500, -301)
    with pytest.raises(OrderBookInconsistency):
        b.apply_delta(Side.YES, 1234, -1)  # removing from a level that does not exist
    assert b.as_levels() == before


@pytest.mark.parametrize("price", [0, PRICE_SCALE, -5, PRICE_SCALE + 1])
def test_out_of_range_prices_rejected(price):
    with pytest.raises(OrderBookInconsistency):
        make().apply_delta(Side.YES, price, 1)


def test_snapshot_atomic_on_error():
    b = make()
    before = b.as_levels()
    with pytest.raises(OrderBookInconsistency):
        b.apply_snapshot([(100, 5), (100, 6)], [])  # duplicate level
    assert b.as_levels() == before


def test_snapshot_zero_qty_dropped_negative_rejected():
    b = OrderBook("T")
    b.apply_snapshot([(100, 0), (200, 5)], [])
    assert b.as_levels()["yes"] == [(200, 5)]
    with pytest.raises(OrderBookInconsistency):
        b.apply_snapshot([(100, -1)], [])


def test_crossed_detection():
    b = OrderBook("T")
    b.apply_snapshot([(6000, 1)], [(4000, 1)])  # YES bid 0.60 == YES ask 0.60 -> locked
    assert b.is_crossed()


def test_copy_is_independent():
    b = make()
    c = b.copy()
    c.apply_delta(Side.YES, 4500, 1)
    assert b.best_bid(Side.YES).qty == 300


levels = st.dictionaries(st.integers(1, PRICE_SCALE - 1), st.integers(1, 10**6), max_size=15)


@given(levels, levels)
def test_snapshot_order_independence_and_dual_reading(yes, no):
    items_y, items_n = list(yes.items()), list(no.items())
    a, b = OrderBook("T"), OrderBook("T")
    a.apply_snapshot(items_y, items_n)
    random.Random(1).shuffle(items_y)
    random.Random(2).shuffle(items_n)
    b.apply_snapshot(items_y, items_n)
    assert a.as_levels() == b.as_levels()
    # dual reading: every NO bid p is a YES ask at 1-p with equal size, and vice versa
    assert sorted((lv.price, lv.qty) for lv in a.asks(Side.YES)) == sorted(
        (PRICE_SCALE - p, q) for p, q in no.items()
    )
    assert sorted((lv.price, lv.qty) for lv in a.asks(Side.NO)) == sorted(
        (PRICE_SCALE - p, q) for p, q in yes.items()
    )
    asks = [lv.price for lv in a.asks(Side.YES)]
    assert asks == sorted(asks)


deltas = st.lists(
    st.tuples(st.sampled_from(list(Side)), st.integers(1, 20), st.integers(-50, 50)), max_size=200
)


@given(deltas)
def test_random_deltas_match_reference_model(ops):
    """Applying deltas must equal a plain-dict reference, and never store non-positive qty."""
    book = OrderBook("T")
    ref = {Side.YES: {}, Side.NO: {}}
    for side, price, delta in ops:
        new = ref[side].get(price, 0) + delta
        if new < 0:
            with pytest.raises(OrderBookInconsistency):
                book.apply_delta(side, price, delta)
            continue
        book.apply_delta(side, price, delta)
        if new:
            ref[side][price] = new
        else:
            ref[side].pop(price, None)
    for side in Side:
        assert {lv.price: lv.qty for lv in book.bids(side)} == ref[side]
        assert all(lv.qty > 0 for lv in book.bids(side))
