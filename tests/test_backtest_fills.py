from datetime import UTC, datetime

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arbitrage.execution import walk_asks
from backtest.fills import (
    QueueModel,
    RestingOrder,
    TakerModel,
    apply_taker_fills_to_book,
    hits_side,
    level_size,
    trade_price,
)
from market.contracts import Side, Trade
from market.order_book import OrderBook

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def c(n):
    return n * 100


def book(yes=(), no=()):
    b = OrderBook("T")
    b.apply_snapshot(list(yes), list(no))
    return b


def print_(yes_price, count, taker):
    return Trade(
        f"t{yes_price}{count}{taker}",
        "T",
        yes_price,
        10_000 - yes_price,
        count,
        Side(taker),
        None,
        T0,
    )


def bid_order(price=4500, qty=1000, side=Side.YES):
    return RestingOrder(1, "T", side, price, qty, qty, 0)


# ---------------------------------------------------------------- taker
@settings(max_examples=150, deadline=None)
@given(st.data())
def test_default_taker_reproduces_stage_4_book_walking(data):
    """The backtest's taker fill and the arbitrage engine's walk_asks must agree exactly."""
    levels = [
        (data.draw(st.integers(100, 9800)), c(data.draw(st.integers(1, 20))))
        for _ in range(data.draw(st.integers(1, 4)))
    ]
    levels = list({p: q for p, q in levels}.items())
    b = book(no=levels)  # YES asks are derived from NO bids
    qty = c(data.draw(st.integers(1, 60)))
    limit = data.draw(st.integers(100, 9900))
    got = TakerModel().execute(b, 0, Side.YES, limit, qty)
    expected, _ = walk_asks(b, Side.YES, qty)
    expected = [(f.price, f.qty) for f in expected if f.price <= limit]
    # walk_asks has no limit; truncate at the first level beyond it
    trimmed = []
    for f in walk_asks(b, Side.YES, qty)[0]:
        if f.price > limit:
            break
        trimmed.append((f.price, f.qty))
    assert got == trimmed


def test_taker_limit_staleness_slippage_and_depth_haircuts():
    b = book(no=[(6000, c(10)), (5500, c(10))])  # YES asks 0.40 x10, 0.45 x10
    assert TakerModel().execute(b, 0, Side.YES, 4200, c(15)) == [
        (4000, c(10))
    ]  # 0.45 is beyond the limit
    assert TakerModel().execute(b, 0, Side.YES, 5000, c(15)) == [(4000, c(10)), (4500, c(5))]
    assert (
        TakerModel(max_staleness_ns=1_000).execute(b, 5_000, Side.YES, 5000, c(1)) == []
    )  # book too old
    assert TakerModel(max_staleness_ns=10_000).execute(b, 5_000, Side.YES, 5000, c(1)) == [
        (4000, c(1))
    ]
    assert TakerModel().execute(None, None, Side.YES, 5000, c(1)) == []
    slipped = TakerModel(extra_slippage_ticks=100).execute(b, 0, Side.YES, 4100, c(5))
    assert slipped == [(4100, c(5))]  # 0.40 + 1c slippage, exactly at the limit
    assert (
        TakerModel(extra_slippage_ticks=200).execute(b, 0, Side.YES, 4100, c(5)) == []
    )  # would exceed it
    assert TakerModel(depth_fraction=0.5).execute(b, 0, Side.YES, 5000, c(20)) == [
        (4000, c(5)),
        (4500, c(5)),
    ]


def test_own_taker_fills_consume_the_book_until_the_next_snapshot():
    b = book(no=[(6000, c(10))])
    fills = TakerModel().execute(b, 0, Side.YES, 5000, c(6))
    apply_taker_fills_to_book(b, Side.YES, fills)
    assert TakerModel().execute(b, 0, Side.YES, 5000, c(20)) == [(4000, c(4))]  # only 4 left


# ---------------------------------------------------------------- trade -> order matching
def test_which_prints_hit_which_side():
    yes_hit = print_(4500, c(1), "no")  # a taker bought NO at 0.55: hit a YES bid at 0.45
    no_hit = print_(4500, c(1), "yes")  # a taker bought YES at 0.45: hit a NO bid at 0.55
    assert hits_side(Side.YES, yes_hit) and not hits_side(Side.NO, yes_hit)
    assert hits_side(Side.NO, no_hit) and not hits_side(Side.YES, no_hit)
    assert trade_price(Side.YES, yes_hit) == 4500 and trade_price(Side.NO, yes_hit) == 5500


# ---------------------------------------------------------------- maker: queue semantics
def test_queue_position_delays_the_fill_until_the_orders_ahead_are_consumed():
    b = book(yes=[(4500, c(50))])  # 50 contracts already bid at 0.45
    m, o = QueueModel.queue(alpha=1.0, cancel_share=0.0), bid_order(4500, c(10))
    m.on_place(o, b)
    assert o.ahead == c(50)
    assert m.on_trade(o, print_(4500, c(30), "no")) == 0 and o.ahead == c(
        20
    )  # eats the queue ahead
    assert (
        m.on_trade(o, print_(4500, c(25), "no")) == c(5) and o.ahead == 0
    )  # 20 ahead, then 5 for us
    o.qty -= c(5)
    assert m.on_trade(o, print_(4500, c(100), "no")) == c(5)  # capped by what we still want


def test_prints_through_our_price_fill_us_completely_in_every_model():
    b = book(yes=[(4500, c(500))])
    for model in (QueueModel.optimistic(), QueueModel.pessimistic(), QueueModel.queue(1.0, 0.0)):
        o = bid_order(4500, c(10))
        model.on_place(o, b)
        assert model.on_trade(o, print_(4400, 1, "no")) == c(10), model.name  # traded BELOW us
        assert (
            model.on_trade(bid_order(4500, c(10)), print_(4600, c(99), "no")) == 0
        )  # never reached us


def test_pessimistic_ignores_prints_at_our_price_optimistic_takes_them_first():
    b = book(yes=[(4500, c(50))])
    pess, opt = bid_order(), bid_order()
    QueueModel.pessimistic().on_place(pess, b)
    QueueModel.optimistic().on_place(opt, b)
    tick = print_(4500, c(4), "no")
    assert QueueModel.pessimistic().on_trade(pess, tick) == 0
    assert QueueModel.optimistic().on_trade(opt, tick) == c(4)


def test_improving_the_best_bid_puts_us_at_the_front():
    b = book(yes=[(4500, c(500))])
    o = bid_order(4600, c(10))  # one tick better than the best bid
    QueueModel.queue(1.0, 0.0).on_place(o, b)
    assert o.ahead == 0
    assert QueueModel.queue(1.0, 0.0).on_trade(o, print_(4600, c(3), "no")) == c(3)


def test_inferred_cancellations_advance_the_queue_by_the_configured_share():
    m = QueueModel.queue(alpha=1.0, cancel_share=0.5)
    o = bid_order(4500, c(10))
    m.on_place(o, book(yes=[(4500, c(100))]))
    # next snapshot: level shrank 100 -> 40 with NO prints at all => 60 cancelled; half are ahead of us
    assert m.on_book(o, book(yes=[(4500, c(40))])) == 0
    assert o.ahead == c(70) - c(70) + min(
        c(100) - c(30), c(40)
    )  # 100 - 0.5*60 = 70, capped by the 40 now displayed
    assert o.ahead == c(40)
    # trades explain a decrease: no cancellations inferred
    m2, o2 = QueueModel.queue(1.0, 1.0), bid_order()
    m2.on_place(o2, book(yes=[(4500, c(100))]))
    m2.on_trade(o2, print_(4500, c(60), "no"))
    m2.on_book(o2, book(yes=[(4500, c(40))]))
    assert o2.ahead == c(40)  # 100 - 60 (traded); the level shows 40: fully explained


def test_a_snapshot_showing_the_market_crossing_our_bid_fills_us():
    m, o = QueueModel.queue(1.0, 0.0), bid_order(4500, c(10))
    m.on_place(o, book(yes=[(4500, c(20))], no=[(4000, c(5))]))  # YES ask = 0.60
    assert m.on_book(o, book(yes=[(4400, c(5))], no=[(5600, c(5))])) == c(
        10
    )  # YES ask now 0.44 <= our 0.45
    assert (
        m.on_book(bid_order(4500), book(yes=[(4500, c(5))], no=[(5400, c(5))])) == 0
    )  # ask 0.46: not crossed


def test_no_side_orders_mirror_yes_side_orders():
    b = book(no=[(5500, c(50))])
    o = bid_order(5500, c(10), side=Side.NO)
    QueueModel.queue(1.0, 0.0).on_place(o, b)
    assert o.ahead == c(50)
    assert QueueModel.queue(1.0, 0.0).on_trade(o, print_(4500, c(60), "yes")) == c(
        10
    )  # taker bought YES at 0.45 = NO 0.55
    assert (
        level_size(b, Side.NO, 5500) == c(50)
        and level_size(b, Side.NO, 5400) == 0
        and level_size(None, Side.YES, 1) == 0
    )


# ---------------------------------------------------------------- the ordering guarantee
MODELS = [
    ("pessimistic", QueueModel.pessimistic()),
    ("queue(1,0)", QueueModel.queue(1.0, 0.0)),
    ("queue(1,.5)", QueueModel.queue(1.0, 0.5)),
    ("queue(.5,.5)", QueueModel.queue(0.5, 0.5)),
    ("queue(.5,1)", QueueModel.queue(0.5, 1.0)),
    ("optimistic", QueueModel.optimistic()),
]


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_cumulative_fills_are_ordered_pessimistic_to_optimistic_at_every_instant(data):
    """The sensitivity range is meaningful only if the models are genuinely nested."""
    start_size = c(data.draw(st.integers(0, 100)))
    qty = c(data.draw(st.integers(1, 30)))
    tape = []
    for _ in range(data.draw(st.integers(1, 25))):
        if data.draw(st.booleans()):
            tape.append(
                ("trade", data.draw(st.integers(4300, 4700)), c(data.draw(st.integers(1, 40))))
            )
        else:
            tape.append(
                ("book", c(data.draw(st.integers(0, 120))), data.draw(st.integers(4400, 5000)))
            )
    states = {}
    for name, model in MODELS:
        o = bid_order(4500, qty)
        model.on_place(o, book(yes=[(4500, start_size)] if start_size else [], no=[(4000, c(5))]))
        states[name] = (model, o, [])
    for kind, a, b_ in tape:
        for model, o, cum in states.values():
            if kind == "trade":
                filled = model.on_trade(o, print_(a, b_, "no"))
            else:
                # a snapshot with `a` contracts at our price; the opposite (YES ask) at price `b_`
                filled = model.on_book(
                    o, book(yes=[(4500, a)] if a else [], no=[(10_000 - b_, c(5))])
                )
            filled = min(filled, o.qty)
            o.qty -= filled
            cum.append((cum[-1] if cum else 0) + filled)
    names = [n for n, _ in MODELS]
    for i in range(len(tape)):
        series = {n: states[n][2][i] for n in names}
        assert (
            series["pessimistic"]
            <= series["queue(1,0)"]
            <= series["queue(1,.5)"]
            <= series["optimistic"]
        ), (tape[: i + 1], series)
        assert series["queue(1,0)"] <= series["queue(.5,.5)"] <= series["optimistic"], series
        assert series["queue(.5,.5)"] <= series["queue(.5,1)"] <= series["optimistic"], series


def test_model_names_and_constructors():
    assert QueueModel.optimistic().alpha == 0 and QueueModel.pessimistic().through_only
    assert QueueModel.queue(0.3, 0.7).name == "queue(a=0.3,c=0.7)"
    with pytest.raises(TypeError):
        RestingOrder()  # a resting order always has an id, market, side, price and size


def test_pessimistic_differs_from_the_queue_model_when_nobody_is_ahead():
    """With an empty level the queue model fills on the first at-price print; pessimistic still waits
    for a print THROUGH us (a previous test hid this: 50 contracts ahead absorbed the print either way)."""
    empty = book(no=[(4000, c(5))])
    pess, queue, opt = bid_order(), bid_order(), bid_order()
    QueueModel.pessimistic().on_place(pess, empty)
    QueueModel.queue(1.0, 0.0).on_place(queue, empty)
    QueueModel.optimistic().on_place(opt, empty)
    tick = print_(4500, c(4), "no")  # exactly at our price
    assert QueueModel.pessimistic().on_trade(pess, tick) == 0
    assert QueueModel.queue(1.0, 0.0).on_trade(queue, tick) == c(4)
    assert QueueModel.optimistic().on_trade(opt, tick) == c(4)


def test_cancellations_ahead_of_a_mid_queue_order_move_it_forward_by_the_stated_share():
    """alpha=1 puts us LAST, so every displayed contract stays ahead and cancellations cannot help
    (cap at displayed size).  Mid-queue (alpha=.5) they do: 50 ahead, 30 unexplained cancels, half ahead."""
    m, o = QueueModel.queue(alpha=0.5, cancel_share=0.5), bid_order(4500, c(10))
    m.on_place(o, book(yes=[(4500, c(100))]))
    assert o.ahead == c(50)
    m.on_book(o, book(yes=[(4500, c(70))]))  # 100 -> 70 with no prints: 30 cancelled
    assert o.ahead == c(50) - c(15)  # half of the 30 were ahead of us
    m0, o0 = QueueModel.queue(alpha=0.5, cancel_share=0.0), bid_order()
    m0.on_place(o0, book(yes=[(4500, c(100))]))
    m0.on_book(o0, book(yes=[(4500, c(70))]))
    assert o0.ahead == c(50)  # share 0: cancellations do not help us


def test_the_ordering_property_is_strict_somewhere():
    """The nesting pess <= queue <= optimistic must not degenerate to all-equal."""
    empty = book(no=[(4000, c(5))])
    totals = {}
    for name, model in MODELS:
        o = bid_order(4500, c(10))
        model.on_place(
            o, empty if name != "queue(1,.5)" else book(yes=[(4500, c(6))], no=[(4000, c(5))])
        )
        totals[name] = model.on_trade(o, print_(4500, c(4), "no"))
    assert (
        totals["pessimistic"] == 0 and totals["optimistic"] == c(4) and totals["queue(1,.5)"] == 0
    )
    assert totals["pessimistic"] < totals["optimistic"]
