from dataclasses import fields
from datetime import UTC, datetime
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backtest.engine import TIF, Backtest, BacktestConfig, Order, run_backtest
from backtest.events import (
    BookUpdate,
    MarketClose,
    Priority,
    Settlement,
    TradeTick,
    Wake,
)
from backtest.feed import ListFeed
from backtest.fills import QueueModel, TakerModel
from backtest.market_info import FORBIDDEN_FIELDS, MarketInfo
from market.contracts import (
    Market,
    MarketStatus,
    PriceRange,
    Rules,
    SettlementResult,
    Side,
    Strike,
    Trade,
)
from market.fees import FeeBook, FeeSchedule
from market.units import PRICE_SCALE as S

MS = 1_000_000
T0 = 1_800_000_000_000_000_000
NOFEE = FeeBook({}, FeeSchedule(known=False)).scaled(Fraction(0))


def c(n):
    return n * 100


def info(t="T"):
    return MarketInfo(t, "E", "KX", "Cat", "title", "yes", "no", Strike(), Rules(), None, None, ())


def book(ts_ms, yes=(), no=(), t="T"):
    return BookUpdate(T0 + ts_ms * MS, t, tuple(yes), tuple(no))


def tick(ts_ms, yes_price, count, taker="no", t="T", tid=None):
    tr = Trade(
        tid or f"{t}{ts_ms}{yes_price}{taker}",
        t,
        yes_price,
        S - yes_price,
        count,
        Side(taker),
        None,
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    return TradeTick(T0 + ts_ms * MS, t, tr)


class Script:
    """A strategy driven by a dict {feed_event_index_or_type: action}; records everything it sees."""

    def __init__(self, on_book=None, on_other=None):
        self.seen = []
        self.on_book, self.on_other = on_book, on_other
        self.ctx = None

    def on_event(self, ctx, ev):
        self.ctx = ctx
        self.seen.append(
            (ctx.now, type(ev).__name__, ctx.outcome("T") if "T" in ctx.tickers else None)
        )
        if isinstance(ev, BookUpdate) and self.on_book:
            self.on_book(ctx, ev)
        elif self.on_other:
            self.on_other(ctx, ev)


def cfg(**kw):
    base = dict(
        latency_submit_ns=100 * MS,
        latency_ack_ns=100 * MS,
        latency_cancel_ns=100 * MS,
        fees=NOFEE,
        maker=QueueModel.optimistic(),
        equity_sample_ns=10**12,
    )
    base.update(kw)
    return BacktestConfig(**base)


def run(events, strat, **kw):
    return run_backtest(ListFeed(events), {"T": info()}, strat, cfg(**kw))


ASK40 = dict(no=[(6000, c(100))])  # YES ask 0.40 x 100
ASK45 = dict(no=[(5500, c(100))])  # YES ask 0.45 x 100


# ------------------------------------------------------------------ latency and lifecycle
def test_orders_fill_against_the_book_as_it_is_at_arrival_not_at_decision():
    def act(ctx, ev):
        if ctx.now == T0 and not ctx.open_orders():
            ctx.place(Order("T", Side.YES, 4500, c(10)))  # decided when the ask was 0.40

    s = Script(on_book=act)
    res = run(
        [book(0, **ASK40), book(50, **ASK45)], s
    )  # ask moves to 0.45 before arrival (t=100ms)
    [f] = res.fills
    assert (
        f.price == 4500 and f.qty == c(10) and f.exec_ts == T0 + 100 * MS and f.liquidity == "taker"
    )
    assert res.orders[0].status == "filled" and res.orders[0].arrival_ts == T0 + 100 * MS
    notices = [(ts, k) for ts, k, _ in s.seen if k == "Fill"]
    assert notices == [(T0 + 200 * MS, "Fill")]  # the strategy hears one ack-latency later


def test_stale_books_are_refused_when_a_staleness_limit_is_set():
    def act(ctx, ev):
        ctx.place(Order("T", Side.YES, 5000, c(1)))

    res = run([book(0, **ASK40)], Script(on_book=act), taker=TakerModel(max_staleness_ns=50 * MS))
    assert (
        not res.fills
        and res.orders[0].status == "expired"
        and "no liquidity" in res.orders[0].reason
    )


def test_insufficient_buying_power_is_rejected_and_reported():
    def act(ctx, ev):
        ctx.place(Order("T", Side.YES, 4500, c(100)))

    s = Script(on_book=act)
    res = run([book(0, **ASK40)], s, initial_cash_micro=1_000_000)  # $1 cannot buy $45
    assert (
        res.orders[0].status == "rejected" and res.orders[0].reason == "insufficient buying power"
    )
    assert any(k == "OrderRejected" for _, k, _ in s.seen)


def test_resting_orders_reserve_cash_and_release_it_on_fill():
    def act(ctx, ev):
        if not ctx.open_orders() and ctx.now == T0:
            ctx.place(Order("T", Side.YES, 3000, c(10), TIF.GTC))

    s = Script(on_book=act)
    res = run(
        [
            book(0, **ASK40),
            tick(500, 2900, c(3)),
        ],
        s,
    )  # through print: fills the whole bid
    [f] = res.fills
    assert (
        f.liquidity == "maker"
        and f.price == 3000
        and f.qty == c(10)
        and res.orders[0].status == "filled"
    )
    assert s.ctx.available_cash == s.ctx.cash  # reservation released after the fill


def test_reserved_cash_blocks_a_second_order_that_would_overdraw():
    def act(ctx, ev):
        if ctx.now == T0:
            ctx.place(Order("T", Side.YES, 3000, c(10), TIF.GTC))  # reserves $3.00
            ctx.place(Order("T", Side.YES, 3000, c(10), TIF.GTC))

    res = run(
        [book(0, **ASK40)], Script(on_book=act), initial_cash_micro=4_000_000
    )  # $4: only one fits
    assert [o.status for o in res.orders] == ["resting", "rejected"]


def test_post_only_that_would_cross_is_rejected_and_ioc_without_liquidity_expires():
    def act(ctx, ev):
        ctx.place(Order("T", Side.YES, 5000, c(1), TIF.POST_ONLY))  # ask is 0.40: would cross
        ctx.place(Order("T", Side.YES, 3000, c(1), TIF.IOC))  # bid below the ask: nothing to take

    res = run([book(0, **ASK40)], Script(on_book=act))
    assert [o.status for o in res.orders] == ["rejected", "expired"] and not res.fills


def test_gtc_remainder_rests_after_a_partial_taker_fill_then_fills_from_the_tape():
    def act(ctx, ev):
        if ctx.now == T0:
            ctx.place(Order("T", Side.YES, 4000, c(150), TIF.GTC))  # only 100 offered at 0.40

    res = run([book(0, no=[(6000, c(100))]), tick(600, 3900, c(80))], Script(on_book=act))
    assert [(f.liquidity, f.qty) for f in res.fills] == [("taker", c(100)), ("maker", c(50))]
    assert res.orders[0].status == "filled" and res.orders[0].filled == c(150)


def test_cancel_takes_effect_after_its_latency_so_a_fill_can_beat_it():
    def act(ctx, ev):
        if isinstance(ev, BookUpdate) and ctx.now == T0:
            ctx.place(Order("T", Side.YES, 3000, c(10), TIF.GTC))
            ctx.wake_at(ctx.now + 1000 * MS, "cancel")  # the strategy schedules its own timer
        elif isinstance(ev, Wake):
            for o in ctx.open_orders():
                ctx.cancel(o.order_id)  # decided at t=1000ms, takes effect at 1100ms

    # a through-print at 1050ms lands BEFORE the cancel reaches the exchange -> the order fills
    res = run([book(0, **ASK40), tick(1050, 2900, c(3))], Script(on_book=act, on_other=act))
    assert res.orders[0].status == "filled" and len(res.fills) == 1
    # a print at 1150ms arrives after the cancel -> no fill
    res2 = run([book(0, **ASK40), tick(1150, 2900, c(3))], Script(on_book=act, on_other=act))
    assert res2.orders[0].status == "cancelled" and not res2.fills


def test_close_cancels_resting_orders_and_later_orders_are_rejected():
    def act(ctx, ev):
        if isinstance(ev, BookUpdate) and ctx.now == T0:
            ctx.place(Order("T", Side.YES, 3000, c(10), TIF.GTC))
            ctx.wake_at(ctx.now + 900 * MS, "late")
        elif isinstance(ev, Wake):
            ctx.place(Order("T", Side.YES, 3000, c(1), TIF.GTC))  # after the 500ms close

    res = run(
        [book(0, **ASK40), MarketClose(T0 + 500 * MS, "T")], Script(on_book=act, on_other=act)
    )
    assert [(o.status, o.reason) for o in res.orders] == [
        ("cancelled", "market closed"),
        ("rejected", "market closed"),
    ]


# ------------------------------------------------------------------ settlement and leakage
def test_the_outcome_is_invisible_until_the_settlement_event_and_pays_out_exactly():
    def act(ctx, ev):
        if ctx.now == T0:
            ctx.place(Order("T", Side.YES, 4000, c(100)))

    s = Script(on_book=act)
    res = run([book(0, **ASK40), Settlement(T0 + 5_000 * MS, "T", S)], s)
    assert all(out is None for ts, k, out in s.seen if ts < T0 + 5_000 * MS)  # never visible early
    assert [out for ts, k, out in s.seen if k == "Settlement"] == [S]
    assert res.pnl_micro == S * c(100) - 4000 * c(100)  # $60 on 100 contracts, no fees
    assert res.settlements == [(T0 + 5_000 * MS, "T", S, 60_000_000)]
    assert res.final_equity_micro == res.initial_cash_micro + 60_000_000


def test_settlement_beats_close_beats_book_beats_trade_beats_orders_at_the_same_instant():
    order = []

    def rec(ctx, ev):
        order.append(type(ev).__name__)

    same = [tick(0, 4000, 1), book(0, **ASK40), MarketClose(T0, "T"), Settlement(T0, "T", 0)]
    run(same, Script(on_book=rec, on_other=rec))
    assert order == ["Settlement", "MarketClose", "BookUpdate", "TradeTick"]
    assert [
        int(p)
        for p in (
            Priority.SETTLEMENT,
            Priority.CLOSE,
            Priority.BOOK,
            Priority.TRADE,
            Priority.ORDER_ARRIVAL,
            Priority.CANCEL_ARRIVAL,
            Priority.FILL_NOTICE,
            Priority.WAKE,
        )
    ] == list(range(8))


def test_a_feed_that_goes_backwards_is_an_error_not_silently_reordered():
    bad = iter([book(10, **ASK40), book(5, **ASK40)])
    with pytest.raises(ValueError, match="backwards"):
        Backtest(bad, {"T": info()}, Script(), cfg()).run()


def test_market_info_is_a_whitelist_and_hides_every_field_that_leaks_the_future():
    names = {f.name for f in fields(MarketInfo)}
    assert not names & FORBIDDEN_FIELDS
    assert names == {
        "ticker",
        "event_ticker",
        "series_ticker",
        "category",
        "title",
        "yes_sub_title",
        "no_sub_title",
        "strike",
        "rules",
        "open_time",
        "scheduled_end",
        "price_ranges",
    }


def market(**over):
    d = dict(
        ticker="T",
        event_ticker="E",
        series_ticker="KX",
        category="C",
        title="t",
        yes_sub_title="y",
        no_sub_title="n",
        market_type="binary",
        status=MarketStatus.ACTIVE,
        strike=Strike(),
        rules=Rules(),
        open_time=datetime(2026, 9, 1, tzinfo=UTC),
        close_time=None,
        expected_expiration_time=datetime(2026, 9, 2, tzinfo=UTC),
        expiration_time=None,
        result=SettlementResult.UNSETTLED,
        settlement_ts=None,
        settlement_value=None,
        price_level_structure=None,
        price_ranges=(PriceRange(0, S, 100),),
        exchange_index=None,
        is_multivariate=False,
    )
    d.update(over)
    return Market(**d)


def test_hidden_fields_cannot_influence_anything_a_strategy_sees():
    """Two markets that differ ONLY in post-hoc fields yield identical MarketInfo, so a strategy
    cannot behave differently - this is the guard against the real close_time/volume leaks."""
    early = market(
        close_time=datetime(2026, 9, 1, 15, tzinfo=UTC),
        volume=999_999,
        open_interest=5,
        result=SettlementResult.YES,
        settlement_value=S,
        status=MarketStatus.FINALIZED,
        settlement_ts=datetime(2026, 9, 1, 15, 5, tzinfo=UTC),
        expiration_time=datetime(2026, 10, 1, tzinfo=UTC),
    )
    late = market(
        close_time=datetime(2026, 9, 2, tzinfo=UTC),
        volume=1,
        open_interest=0,
        result=SettlementResult.NO,
        settlement_value=0,
        status=MarketStatus.CLOSED,
    )
    assert MarketInfo.from_market(early) == MarketInfo.from_market(late)
    assert MarketInfo.from_market(early).scheduled_end == datetime(2026, 9, 2, tzinfo=UTC)


# ------------------------------------------------------------------ determinism and causality
class Reactive:
    """A deterministic strategy whose actions depend on everything visible: books, prints, cash,
    position, outcomes, time.  Used to test that decisions depend only on the past."""

    def on_event(self, ctx, ev):
        h = 0
        for t in ctx.tickers:
            b = ctx.book(t)
            bb, ba = (b.best_bid(Side.YES), b.best_ask(Side.YES)) if b else (None, None)
            lt = ctx.last_trade(t)
            h += (
                (bb.price if bb else 7) * 3
                + (ba.price if ba else 11) * 5
                + (lt.yes_price if lt else 13)
            )
            h += ctx.position(t).net_yes + (ctx.outcome(t) or 0) // 100
        h = (h + ctx.now // MS + ctx.available_cash // 10_000) % 1009
        t = ctx.tickers[h % len(ctx.tickers)]
        if ctx.is_open(t) and ctx.book(t):
            ba = ctx.book(t).best_ask(Side.YES)
            if h % 7 == 0 and ba:
                ctx.place(Order(t, Side.YES, ba.price, c(1 + h % 5), TIF.IOC, f"h{h}"))
            elif h % 7 == 1:
                ctx.place(Order(t, Side.NO, 1000 + (h * 13) % 8000, c(1 + h % 4), TIF.GTC, f"g{h}"))
            elif h % 7 == 2:
                for o in ctx.open_orders(t)[:1]:
                    ctx.cancel(o.order_id)
            elif h % 7 == 3:
                ctx.wake_at(ctx.now + (1 + h % 9) * 100 * MS, "w", t)


def random_feed(draw, tickers=("A", "B", "C")):
    ev, ts = [], 0
    for _ in range(draw(st.integers(20, 80))):
        ts += draw(st.integers(0, 400))
        t = draw(st.sampled_from(tickers))
        kind = draw(st.integers(0, 9))
        if kind <= 4:
            ev.append(
                book(
                    ts,
                    yes=[(draw(st.integers(500, 4500)), c(draw(st.integers(1, 40))))],
                    no=[(draw(st.integers(500, 4500)), c(draw(st.integers(1, 40))))],
                    t=t,
                )
            )
        elif kind <= 8:
            ev.append(
                tick(
                    ts,
                    draw(st.integers(400, 9000)),
                    c(draw(st.integers(1, 20))),
                    draw(st.sampled_from(["yes", "no"])),
                    t=t,
                    tid=f"id{len(ev)}",
                )
            )
        elif kind == 9 and draw(st.booleans()):
            ev.append(Settlement(T0 + ts * MS, t, draw(st.sampled_from([0, S, 5000]))))
    return ev


def order_signature(res, upto_ns):
    return [
        (o.submitted_ts, o.ticker, o.side.value, o.price, o.qty, o.tif.value, o.tag)
        for o in res.orders
        if o.submitted_ts <= upto_ns
    ]


@settings(max_examples=120, deadline=None)
@given(st.data())
def test_decisions_up_to_time_T_depend_only_on_events_up_to_T(data):
    """The look-ahead theorem: truncate the feed after T and every decision at or before T is
    unchanged.  If a strategy (or the engine) leaked anything from the future, these would differ."""
    events = random_feed(data.draw)
    infos = {t: info(t) for t in ("A", "B", "C")}
    feed = ListFeed(events)
    cut = T0 + data.draw(st.integers(0, max(1, (feed.events[-1].ts_ns - T0) // MS))) * MS
    config = cfg(
        fees=FeeBook({}, FeeSchedule(known=False)),
        maker=data.draw(
            st.sampled_from(
                [QueueModel.optimistic(), QueueModel.pessimistic(), QueueModel.queue(1.0, 0.5)]
            )
        ),
    )
    full = run_backtest(feed, infos, Reactive(), config)
    part = run_backtest(feed.truncated(cut), infos, Reactive(), config)
    assert order_signature(full, cut) == order_signature(part, cut)
    # fills that happened by T are also identical (state, not just decisions, has no future in it)
    assert [(f.exec_ts, f.ticker, f.price, f.qty) for f in full.fills if f.exec_ts <= cut] == [
        (f.exec_ts, f.ticker, f.price, f.qty) for f in part.fills if f.exec_ts <= cut
    ]


def test_the_replay_is_deterministic():
    events = [
        book(0, yes=[(4000, c(10))], no=[(5500, c(30))]),
        tick(300, 3900, c(5)),
        book(600, **ASK45),
        tick(900, 4400, c(9), "yes"),
    ]
    outs = []
    for _ in range(3):
        res = run(events, Reactive(), maker=QueueModel.queue(1.0, 0.5))
        outs.append(
            (
                res.pnl_micro,
                [(f.exec_ts, f.price, f.qty, f.liquidity) for f in res.fills],
                [(o.order_id, o.status) for o in res.orders],
                res.equity,
            )
        )
    assert outs[0] == outs[1] == outs[2]


def test_equity_curve_is_marked_to_liquidation_value_and_ends_at_final_equity():
    def act(ctx, ev):
        if ctx.now == T0:
            ctx.place(Order("T", Side.YES, 4000, c(10)))

    res = run(
        [
            book(0, yes=[(3800, c(50))], no=[(6000, c(100))]),
            book(2_000, yes=[(3500, c(50))], no=[(6000, c(100))]),
        ],
        Script(on_book=act),
        equity_sample_ns=MS,
    )
    assert res.equity[-1][1] == res.final_equity_micro
    # bought 10 @0.40; the best YES bid is 0.35 at the end -> liquidation value 0.35 x 10
    assert res.final_equity_micro == res.initial_cash_micro - 4000 * c(10) + 3500 * c(10)
    times = [e[0] for e in res.equity]
    assert times == sorted(times)


def test_strategy_errors_propagate_instead_of_being_swallowed():
    def boom(ctx, ev):
        raise RuntimeError("bug")

    with pytest.raises(RuntimeError, match="bug"):
        run([book(0, **ASK40)], Script(on_book=boom))


def test_scheduling_into_the_past_is_refused():
    def act(ctx, ev):
        ctx.wake_at(ctx.now - 1)

    with pytest.raises(ValueError, match="past"):
        run([book(0, **ASK40)], Script(on_book=act))


def test_unknown_markets_are_an_error_but_events_for_unlisted_markets_are_ignored():
    def act(ctx, ev):
        ctx.place(Order("NOPE", Side.YES, 4000, 1))

    with pytest.raises(KeyError):
        run([book(0, **ASK40)], Script(on_book=act))
    s = Script()
    run([book(0, **ASK40), book(1, yes=[(1000, 1)], t="OTHER")], s)
    assert len(s.seen) == 1  # the OTHER market's snapshot never reached the strategy


def test_the_causality_check_has_teeth_it_flags_a_strategy_that_peeks_at_the_future():
    """Meta-test: a strategy whose behaviour depends on how much data exists AFTER the cut must be
    caught by the same comparison that passes for honest strategies."""
    events = [book(i * 100, yes=[(4000, c(10))], no=[(5500, c(10))]) for i in range(30)]
    feed = ListFeed(events)
    cut = T0 + 1_000 * MS

    class Leaky:
        def __init__(self, total_events):
            self.total = total_events  # knowledge of the future: how long the feed will run

        def on_event(self, ctx, ev):
            if (
                ctx.now == T0 and self.total > 20
            ):  # behaviour depends on data that has not arrived yet
                ctx.place(Order("T", Side.YES, 4500, c(1), TIF.GTC, "leak"))

    full = run_backtest(feed, {"T": info()}, Leaky(len(feed.events)), cfg())
    part_feed = feed.truncated(cut)
    part = run_backtest(part_feed, {"T": info()}, Leaky(len(part_feed.events)), cfg())
    assert order_signature(full, cut) != order_signature(part, cut)  # the leak is visible

    class Honest:
        def on_event(self, ctx, ev):
            if ctx.now == T0:
                ctx.place(Order("T", Side.YES, 4500, c(1), TIF.GTC, "ok"))

    a = run_backtest(feed, {"T": info()}, Honest(), cfg())
    b = run_backtest(part_feed, {"T": info()}, Honest(), cfg())
    assert order_signature(a, cut) == order_signature(b, cut)


def test_our_own_taker_fills_consume_liquidity_until_the_next_snapshot():
    """Two 10-contract IOC buys against a book offering only 10: the second must find nothing.
    (Without self-impact a strategy could 'buy' the same contracts twice within one snapshot.)"""

    def act(ctx, ev):
        if isinstance(ev, BookUpdate):
            ctx.place(Order("T", Side.YES, 5000, c(10), TIF.IOC, f"o@{ev.ts_ns}"))
            if ctx.now == T0:
                ctx.place(Order("T", Side.YES, 5000, c(10), TIF.IOC, "second"))

    feed = [
        book(0, no=[(6000, c(10))]),
        book(1_000, no=[(6000, c(10))]),
    ]  # a fresh snapshot refills the level
    res = run(feed, Script(on_book=act))
    by_tag = {o.tag: (o.status, o.filled) for o in res.orders}
    assert by_tag[f"o@{T0}"] == ("filled", c(10))
    assert by_tag["second"] == ("expired", 0)  # the first order already took the only 10 contracts
    assert by_tag[f"o@{T0 + 1_000 * MS}"] == (
        "filled",
        c(10),
    )  # the next snapshot is fresh liquidity
    assert sum(f.qty for f in res.fills) == c(20)


def test_a_poll_confirmation_refreshes_book_age_without_changing_the_book():
    """Books are stored only on change, so freshness must come from the poll log: an unchanged book
    confirmed 1s ago is fresh; the same book after a long polling gap is stale and refused."""
    from backtest.events import BookConfirm

    ages = []

    def act(ctx, ev):
        if isinstance(ev, (BookUpdate, BookConfirm)):
            v = ctx.book("T")
            if v is not None:
                ages.append(
                    (
                        (ctx.now - T0) // (1000 * MS),
                        v.age_ns // (1000 * MS),
                        v.best_ask(Side.YES).price,
                    )
                )

    s = Script(on_book=act, on_other=act)
    feed = [book(0, **ASK40), BookConfirm(T0 + 30_000 * MS), BookConfirm(T0 + 60_000 * MS)]
    run(feed, s)
    assert ages == [
        (0, 0, 4000),
        (30, 0, 4000),
        (60, 0, 4000),
    ]  # age 0 at each confirmation, levels unchanged

    # a 20-minute outage: no confirmations, then an order -> the book is stale and the taker refuses it
    def act2(ctx, ev):
        if isinstance(ev, Wake):
            ctx.place(Order("T", Side.YES, 5000, c(1)))

    def start(ctx, ev):
        if isinstance(ev, BookUpdate):
            ctx.wake_at(ctx.now + 1_200_000 * MS, "after-outage")

    res = run(
        [book(0, **ASK40), BookConfirm(T0 + 30_000 * MS)],
        Script(on_book=start, on_other=act2),
        taker=TakerModel(max_staleness_ns=60_000 * MS),
    )
    assert res.orders[0].status == "expired" and not res.fills  # 1200s since the last confirmation


def test_events_at_one_instant_are_applied_together_before_the_strategy_sees_any():
    """Real bug found on live data: after a goal, three markets repriced in ONE poll response (same
    timestamp).  Applying them one at a time showed the strategy an impossible mixture of new and old
    books.  A simultaneous batch must be visible only in its final, consistent state."""
    seen = []

    class Watcher:
        def on_event(self, ctx, ev):
            if isinstance(ev, BookUpdate):
                seen.append(
                    (
                        ev.ticker,
                        ctx.book("A").best_bid(Side.YES).price,
                        ctx.book("B").best_bid(Side.YES).price,
                    )
                )

    old = [book(0, yes=[(2000, c(10))], t="A"), book(0, yes=[(7000, c(10))], t="B")]  # 0.20 / 0.70
    new = [
        book(1_000, yes=[(7000, c(10))], t="A"),
        book(1_000, yes=[(2000, c(10))], t="B"),
    ]  # goal: 0.70 / 0.20
    run_backtest(ListFeed(old + new), {"A": info("A"), "B": info("B")}, Watcher(), cfg())
    after_goal = [(a, b) for t, a, b in seen[2:]]
    assert after_goal == [
        (7000, 2000),
        (7000, 2000),
    ]  # never the mixture (7000, 7000) or (2000, 2000)


def test_a_repricing_in_one_response_creates_no_phantom_arbitrage_but_separate_instants_do_show_the_intermediate():
    from arbitrage.detector import DetectorParams
    from arbitrage.opportunity import RelationSpec
    from backtest.strategies import ArbitrageTaker
    from market.relationships import MutuallyExclusive
    from market.semantics import EvidenceLevel

    spec = RelationSpec(MutuallyExclusive(["A", "B"]), EvidenceLevel.PROVEN, "EV", "KX")
    params = DetectorParams(fees=NOFEE, target=100)
    infos = {"A": info("A"), "B": info("B")}
    old = [
        book(0, yes=[(2000, c(500))], t="A"),
        book(0, yes=[(7000, c(500))], t="B"),
    ]  # bids sum 0.90
    same_instant = [
        book(1_000, yes=[(7000, c(500))], t="A"),
        book(1_000, yes=[(2000, c(500))], t="B"),
    ]
    strat = ArbitrageTaker([spec], params)
    res = run_backtest(ListFeed(old + same_instant), infos, strat, cfg())
    assert (
        not res.orders and strat.opportunities_seen == 0
    )  # one consistent update: nothing to trade

    # if the SAME two updates genuinely arrive at different instants, the intermediate state is real
    # (A repriced first): the market really did briefly show 0.70 + 0.70 and a strategy may act on it.
    staggered = [book(1_000, yes=[(7000, c(500))], t="A"), book(1_001, yes=[(2000, c(500))], t="B")]
    strat2 = ArbitrageTaker([spec], params)
    res2 = run_backtest(ListFeed(old + staggered), infos, strat2, cfg())
    assert strat2.opportunities_seen == 1 and res2.orders
