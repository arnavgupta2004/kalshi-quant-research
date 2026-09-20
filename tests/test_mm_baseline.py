from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backtest.engine import BacktestConfig, run_backtest
from backtest.events import BookConfirm, BookUpdate, TradeTick
from backtest.feed import ListFeed
from backtest.fills import QueueModel
from backtest.market_info import MarketInfo
from market.contracts import Rules, Side, Strike, Trade
from market.fees import FeeBook, FeeSchedule
from market.timeutil import to_epoch_us
from market_making.baseline import BaselineParams, BinaryMarketMaker
from market_making.risk import RiskLimits

SEC = 1_000_000_000
T0 = 1_800_000_000 * SEC
FEES = FeeBook({}, FeeSchedule(known=False))


def c(n):
    return n * 100


def info(t, event="E1", end_in_h=10.0):
    end = datetime.fromtimestamp(T0 / SEC, UTC) + timedelta(hours=end_in_h)
    return MarketInfo(t, event, "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())


def bu(sec, t, yes=(), no=()):
    return BookUpdate(T0 + int(sec * SEC), t, tuple(yes), tuple(no))


def taker_no(sec, t, yes_price, contracts=20):
    tr = Trade(
        f"t{sec}", t, yes_price, 10_000 - yes_price, c(contracts), Side.NO, "bid", datetime.now(UTC)
    )
    return TradeTick(T0 + int(sec * SEC), t, tr)


BOOK = dict(yes=[(4500, c(500))], no=[(4500, c(500))])  # bid 45c / ask 55c: mid 50c, spread 10c


def run(events, params=None, tickers=("A",), infos=None, latency_ms=0, maker=None, event_of=None):
    params = params or BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0)
    mm = BinaryMarketMaker(list(tickers), params, event_of=event_of or {t: "E1" for t in tickers})
    ns = latency_ms * 1_000_000
    cfg = BacktestConfig(
        initial_cash_micro=10**11, latency_submit_ns=ns, latency_ack_ns=ns, latency_cancel_ns=ns,
        fees=FEES, maker=maker or QueueModel.optimistic(), equity_sample_ns=10**12,
    )  # fmt: skip
    res = run_backtest(ListFeed(events), infos or {t: info(t) for t in tickers}, mm, cfg)
    return res, mm


def places(mm, side=None):
    return [q for q in mm.quote_log if q.action == "place" and (side is None or q.side == side)]


# ------------------------------------------------------------------ quoting around the mid
def test_it_quotes_both_sides_of_the_mid_with_post_only_orders():
    res, mm = run([bu(0, "A", **BOOK), BookConfirm(T0 + 1 * SEC)])
    yes_q, no_q = places(mm, "yes")[0], places(mm, "no")[0]
    # mid 0.50, gamma .05, k 50: bid ~ 0.47 (rounded down), ask ~ 0.53 -> a NO bid at 1 - 0.53
    assert yes_q.price == 4700 and no_q.price == 4700 and yes_q.qty == no_q.qty == c(10)
    assert all(o.tif.value == "post_only" and o.status == "resting" for o in res.orders)
    assert not any(o.status == "rejected" for o in res.orders)  # never crosses the market


def test_the_quote_moves_with_the_market():
    res, mm = run([bu(0, "A", **BOOK), bu(5, "A", yes=[(5500, c(500))], no=[(3500, c(500))])])
    later = [q for q in places(mm, "yes") if q.ts > T0 + 4 * SEC][0]
    assert later.price > 4700  # mid moved from 50c to 60c: the bid followed it (rounded to 57c)
    assert any(q.action == "cancel" for q in mm.quote_log)


# ------------------------------------------------------------------ inventory skew and limits
def fill_then_book(extra=()):
    """Bid at 47c is hit by a NO taker printing at 46c -> we are long 20 YES."""
    return [bu(0, "A", **BOOK), taker_no(2, "A", 4600, 20), bu(10, "A", **BOOK), *extra]


def test_a_fill_shifts_both_quotes_against_the_new_inventory():
    res, mm = run(fill_then_book())
    assert res.portfolio.position("A").net_yes > 0  # we bought YES
    first = {q.side: q.price for q in places(mm)[:2]}
    after = {
        q.side: q.price for q in places(mm) if q.ts > T0 + 2 * SEC - 1
    }  # requoted right after the fill
    after["yes"] = [q for q in places(mm, "yes") if q.ts >= T0 + 2 * SEC][0].price
    after["no"] = [q for q in places(mm, "no") if q.ts >= T0 + 2 * SEC][0].price
    assert after["yes"] < first["yes"]  # pay less for more YES...
    assert (
        after["no"] > first["no"]
    )  # ...and offer YES cheaper: the NO bid (the ask) is HIGHER in NO terms


def test_the_position_limit_stops_that_side_but_not_the_other():
    p = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0, size=10.0,
                       limits=RiskLimits(max_position_contracts=10.0))  # fmt: skip
    res, mm = run(fill_then_book(), p)
    assert res.portfolio.position("A").net_yes == c(20) or res.portfolio.position("A").net_yes >= c(
        10
    )
    late = [q for q in places(mm) if q.ts >= T0 + 2 * SEC]  # everything placed after the fill
    assert not [q for q in late if q.side == "yes"]  # at the limit: no more bids
    assert [q for q in late if q.side == "no"]  # but it will still sell (a NO bid) to get flat


@pytest.mark.parametrize(
    "book,note",
    [
        (dict(yes=[(4500, c(500))], no=[]), "one-sided book"),
        (dict(yes=[(400, c(500))], no=[(5000, c(500))]), "spread wider than max_spread"),
        (dict(yes=[(200, c(500))], no=[(9700, c(500))]), "price near 0: below min_price"),
        (dict(yes=[(9700, c(500))], no=[(100, c(500))]), "price near 1: above max_price"),
    ],
)
def test_it_does_not_quote_where_the_mid_is_not_trustworthy(book, note):
    res, mm = run([bu(0, "A", **book)])
    assert not places(mm), note


def test_it_stops_quoting_a_stale_book():
    res, mm = run(
        [bu(0, "A", **BOOK), BookConfirm(T0 + 31 * SEC), bu(31.5, "A", yes=[(4500, c(500))], no=[])]
    )
    # a fresh two-sided book at t=0, then nothing for 31 s and a one-sided update: no new quotes after it
    assert not [q for q in places(mm) if q.ts > T0 + 31 * SEC]


def test_it_stops_before_the_scheduled_end():
    res, mm = run(
        [bu(0, "A", **BOOK)], infos={"A": info("A", end_in_h=0.1)}
    )  # 6 min to go < 15 min cutoff
    assert not places(mm)
    res, mm = run([bu(0, "A", **BOOK)], infos={"A": info("A", end_in_h=2.0)})
    assert places(mm)


def test_it_does_not_quote_a_market_it_was_not_given():
    res, mm = run([bu(0, "B", **BOOK)], tickers=("A",), infos={"A": info("A"), "B": info("B")})
    assert not places(mm)


# ------------------------------------------------------------------ the kill-switch
def crash_feed():
    return [
        bu(0, "A", **BOOK),
        taker_no(2, "A", 4600, 20),  # long ~20 YES at 47c
        bu(4, "A", yes=[(500, c(500))], no=[(9000, c(500))]),  # market collapses to 5c / 10c
        bu(20, "A", yes=[(500, c(500))], no=[(9000, c(500))]),
        bu(40, "A", yes=[(600, c(500))], no=[(9000, c(500))]),
    ]


def test_a_drawdown_trips_the_kill_switch_cancels_everything_and_stays_off():
    p = BaselineParams(
        min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0, limits=RiskLimits(max_drawdown_usd=5.0)
    )
    res, mm = run(crash_feed(), p)
    assert mm.risk.state.killed and "drawdown" in mm.risk.state.reason
    t_kill = mm.risk.state.killed_at_ns
    assert not [q for q in places(mm) if q.ts > t_kill]  # no new quotes once killed
    assert not [o for o in res.orders if o.status == "resting"]  # every resting order was cancelled


def test_without_a_drawdown_the_same_crash_does_not_kill():
    p = BaselineParams(
        min_requote_s=0.0,
        busy_s=0.05,
        risk_check_s=0.0,
        limits=RiskLimits(max_drawdown_usd=10_000.0),
    )
    res, mm = run(crash_feed(), p)
    assert not mm.risk.state.killed


# ------------------------------------------------------------------ latency: no stacked duplicates
@settings(max_examples=60, deadline=None)
@given(st.data())
def test_at_most_one_live_quote_per_side_even_with_latency(data):
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(3, 30))):
        t += data.draw(st.sampled_from([0.0, 0.2, 1.0, 3.0]))
        y = data.draw(st.integers(30, 60)) * 100
        n = data.draw(st.integers(30, 60)) * 100
        if y + n <= 9800:
            events.append(bu(t, "A", yes=[(y, c(50))], no=[(n, c(50))]))
        if data.draw(st.booleans()):
            events.append(taker_no(t, "A", data.draw(st.integers(30, 60)) * 100, 5))
    seen = []
    p = BaselineParams(min_requote_s=0.0, busy_s=1.0, risk_check_s=0.0)
    mm = BinaryMarketMaker(["A"], p, event_of={"A": "E1"})
    real = mm.on_event

    def watching(ctx, ev):
        real(ctx, ev)
        live = [
            o for o in ctx.open_orders("A") if o.tag == "mm" and o.order_id not in mm._cancelling
        ]
        seen.append(max([sum(1 for o in live if o.side == s) for s in (Side.YES, Side.NO)] or [0]))

    mm.on_event = watching
    cfg = BacktestConfig(initial_cash_micro=10**11, latency_submit_ns=200_000_000, latency_ack_ns=200_000_000,
                         latency_cancel_ns=200_000_000, fees=FEES, maker=QueueModel.optimistic(), equity_sample_ns=10**12)  # fmt: skip
    run_backtest(ListFeed(events), {"A": info("A")}, mm, cfg)
    assert max(seen or [0]) <= 1


# ------------------------------------------------------------------ causality
def order_sig(res, upto):
    return [
        (o.submitted_ts, o.ticker, o.side.value, o.price, o.qty)
        for o in res.orders
        if o.submitted_ts <= upto
    ]


@settings(max_examples=60, deadline=None)
@given(st.data())
def test_decisions_up_to_t_depend_only_on_events_up_to_t(data):
    """The look-ahead theorem for this strategy: truncating the feed at T changes nothing before T."""
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(4, 30))):
        t += data.draw(st.sampled_from([0.0, 0.5, 2.0, 5.0]))
        y, n = data.draw(st.integers(30, 60)) * 100, data.draw(st.integers(30, 60)) * 100
        if y + n <= 9800:
            events.append(bu(t, "A", yes=[(y, c(50))], no=[(n, c(50))]))
        if data.draw(st.booleans()):
            events.append(taker_no(t, "A", data.draw(st.integers(30, 60)) * 100, 5))
    if not events:
        return
    cut = T0 + int(data.draw(st.floats(0, t + 1)) * SEC)
    full, _ = run(events, latency_ms=100)
    part, _ = run([e for e in events if e.ts_ns <= cut], latency_ms=100)
    assert order_sig(full, cut) == order_sig(part, cut)
    assert [(f.exec_ts, f.price, f.qty) for f in full.fills if f.exec_ts <= cut] == [
        (f.exec_ts, f.price, f.qty) for f in part.fills if f.exec_ts <= cut
    ]


def test_conservation_and_settlement_pnl_matches_the_fills():
    """Held to settlement, P&L is exactly sum(payoff - price) over fills (pairs net to $1 either way)."""
    from backtest.events import Settlement

    ev = fill_then_book([Settlement(T0 + 60 * SEC, "A", 10_000)])  # YES wins
    res, _ = run(ev)
    expected = sum(f.qty * ((10_000 if f.side.value == "yes" else 0) - f.price) for f in res.fills)
    assert res.pnl_micro == expected - res.portfolio.fees
    assert to_epoch_us(datetime.fromtimestamp(T0 / SEC, UTC)) > 0


# ------------------------------------------------------------------ gaps found by mutation testing
def test_the_kill_switch_pulls_quotes_in_markets_that_are_not_moving():
    """B never receives another event after it is quoted: only an explicit 'cancel everything' on the
    kill can remove its orders (managing A alone would leave them resting)."""
    feed = [bu(0, "B", **BOOK), *crash_feed()]
    p = BaselineParams(
        min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0, limits=RiskLimits(max_drawdown_usd=5.0)
    )
    res, mm = run(
        feed, p, tickers=("A", "B"), event_of={"A": "E1", "B": "E2"},
        infos={"A": info("A", "E1"), "B": info("B", "E2")},
    )  # fmt: skip
    assert mm.risk.state.killed
    b_cancels = [q for q in mm.quote_log if q.ticker == "B" and q.action == "cancel"]
    assert {q.side for q in b_cancels} == {"yes", "no"}
    assert all(q.reason == "kill-switch" for q in b_cancels)
    assert not [o for o in res.orders if o.ticker == "B" and o.status == "resting"]


def test_the_event_loss_limit_counts_positions_held_in_sibling_markets():
    """A1's fill uses most of the event's loss budget, so A2's next quote must be cut to what is left."""
    p = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0, size=10.0,
                       limits=RiskLimits(max_event_loss_usd=6.0))  # fmt: skip
    feed = [
        bu(0, "A1", **BOOK),
        bu(0, "A2", **BOOK),
        taker_no(2, "A1", 4600, 20),  # we buy 10 YES on A1 at 47c: worst case -$4.70
        bu(10, "A2", **BOOK),
    ]
    res, mm = run(
        feed, p, tickers=("A1", "A2"), event_of={"A1": "EA", "A2": "EA"},
        infos={"A1": info("A1", "EA"), "A2": info("A2", "EA")},
    )  # fmt: skip
    assert res.portfolio.position("A1").net_yes == c(10)
    late = [q for q in places(mm, "yes") if q.ticker == "A2" and q.ts >= T0 + 10 * SEC]
    assert late and late[-1].qty <= c(2)  # $6 - $4.70 leaves room for 2 contracts at 47c
    first = [q for q in places(mm, "yes") if q.ticker == "A2"][0]
    assert first.qty == c(10)  # before the fill there was room for the full size


def test_a_mostly_filled_quote_is_replaced_at_full_size_even_if_its_price_is_unchanged():
    p = BaselineParams(
        min_requote_s=0.0,
        busy_s=0.05,
        risk_check_s=0.0,
        size=10.0,
        gamma=1e-6,
        min_half_spread=0.03,
    )  # fmt: skip  no skew: the price does not move
    feed = [
        bu(0, "A", **BOOK),
        taker_no(
            2, "A", 4600, 6
        ),  # hits 6 of our 10 -> a 4-contract residual (< half of the target)
        bu(10, "A", **BOOK),
    ]
    res, mm = run(feed, p)
    assert res.portfolio.position("A").net_yes == c(6)
    yes_places = places(mm, "yes")
    assert yes_places[0].qty == c(10)
    replaced = [
        q for q in mm.quote_log if q.action == "cancel" and q.side == "yes" and q.qty == c(4)
    ]
    assert replaced, "the 4-contract residual should have been cancelled"
    assert [q for q in yes_places if q.ts > replaced[0].ts]  # and a fresh quote placed after it


def test_after_cancelling_a_quote_it_waits_before_acting_on_that_side_again():
    p = BaselineParams(min_requote_s=0.0, busy_s=1.0, risk_check_s=0.0)
    moved = dict(yes=[(5500, c(500))], no=[(3500, c(500))])
    feed = [bu(0, "A", **BOOK), bu(5, "A", **moved), bu(5.2, "A", **moved), bu(8, "A", **moved)]
    res, mm = run(feed, p, latency_ms=0)
    cancel_t = [q.ts for q in mm.quote_log if q.action == "cancel" and q.side == "yes"][0]
    replacement = [q.ts for q in places(mm, "yes") if q.ts > cancel_t][0]
    assert replacement - cancel_t >= 1 * SEC  # the busy window, not the 0.2 s until the next event
