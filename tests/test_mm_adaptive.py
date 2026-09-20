import math
from datetime import UTC, datetime, timedelta

import numpy as np
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
from market_making.adaptive import AdaptiveMarketMaker, AdaptiveParams
from market_making.baseline import BaselineParams, BinaryMarketMaker
from market_making.features import FEATURES, V0, MarketTracker
from market_making.shortterm import Ridge

SEC = 1_000_000_000
T0 = 1_800_000_000 * SEC
FEES = FeeBook({}, FeeSchedule(known=False))


def c(n):
    return n * 100


def ts(sec):
    return T0 + int(sec * SEC)


def info(t, event="E1", end_in_h=10.0):
    end = datetime.fromtimestamp(T0 / SEC, UTC) + timedelta(hours=end_in_h)
    return MarketInfo(t, event, "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())


def bu(sec, t, yes, no):
    return BookUpdate(ts(sec), t, tuple(yes), tuple(no))


def trade(sec, t, yes_price, side, contracts=20):
    tr = Trade(
        f"t{sec}-{yes_price}", t, yes_price, 10_000 - yes_price, c(contracts), side, "bid",
        datetime.now(UTC),
    )  # fmt: skip
    return TradeTick(ts(sec), t, tr)


BOOK = dict(yes=[(4500, c(500))], no=[(4500, c(500))])  # 45c / 55c, mid 50c
FAST = dict(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0)
IDX = {n: i for i, n in enumerate(FEATURES)}


# ------------------------------------------------------------------ features, by hand
def tracker_with(book_yes, book_no, at=0.0):
    tr = MarketTracker()
    tr.on_book(ts(at), book_yes, book_no)
    return tr


def test_book_features_are_the_queue_imbalance_and_the_microprice_offset():
    tr = tracker_with([(4500, c(300)), (4400, c(100))], [(4500, c(100)), (4300, c(200))])
    x = tr.snapshot(ts(0)).x
    assert x[IDX["obi_top"]] == pytest.approx((300 - 100) / 400)  # 3x more resting on the bid
    assert x[IDX["obi_depth"]] == pytest.approx((400 - 300) / 700)
    assert x[IDX["micro_dev"]] == pytest.approx(0.10 * 0.5 / 2)  # spread 10c x obi / 2 = +2.5c
    assert tracker_with([(4500, c(100))], [(4500, c(100))]).snapshot(ts(0)).x[IDX["obi_top"]] == 0


def test_flow_is_signed_taker_volume_shrunk_by_v0():
    tr = tracker_with(**{"book_yes": BOOK["yes"], "book_no": BOOK["no"]})
    tr.on_trade(ts(1), 4600, c(30), +1)
    tr.on_trade(ts(2), 4600, c(10), -1)
    x = tr.snapshot(ts(3)).x
    assert x[IDX["flow_60"]] == pytest.approx((30 - 10) / (40 + V0))
    tr.on_trade(ts(3), 4600, c(5), 0)  # an unknown-side print adds volume but no direction
    assert tr.snapshot(ts(4)).x[IDX["flow_60"]] == pytest.approx(20 / (45 + V0))
    assert tr.snapshot(ts(100)).x[IDX["flow_60"]] == 0.0  # everything is older than 60 s


def test_stale_dev_is_the_vwap_of_prints_newer_than_the_book_and_stale_out_is_its_part_beyond_the_spread():
    tr = tracker_with(BOOK["yes"], BOOK["no"], at=0)  # bid 45 / ask 55, mid 50, spread 10c
    tr.on_trade(ts(-1), 3000, c(10), -1)  # older than the book: already in it
    assert tr.snapshot(ts(1)).x[IDX["stale_dev"]] == 0.0
    tr.on_trade(ts(1), 4700, c(10), +1)  # inside the spread: bounce, not a repricing
    x = tr.snapshot(ts(1.5)).x
    assert x[IDX["stale_dev"]] == pytest.approx(-0.03) and x[IDX["stale_out"]] == 0.0
    tr.on_trade(ts(2), 6500, c(10), +1)  # far above the stale ask
    x = tr.snapshot(ts(2.5)).x
    assert x[IDX["stale_dev"]] == pytest.approx((4700 + 6500) / 2 / 10_000 - 0.5)  # +0.06
    assert x[IDX["stale_out"]] == pytest.approx(0.06 - 0.05)
    tr.on_trade(ts(3), 2000, c(20), -1)  # a print below the stale bid: a large negative deviation
    x = tr.snapshot(ts(3.5)).x
    assert x[IDX["stale_dev"]] < 0 and x[IDX["stale_out"]] < 0  # the sign carries through


def test_a_book_update_or_a_confirmation_makes_earlier_prints_no_longer_stale():
    tr = tracker_with(BOOK["yes"], BOOK["no"], at=0)
    tr.on_trade(ts(1), 4700, c(10), +1)
    assert tr.snapshot(ts(2)).x[IDX["stale_dev"]] != 0
    tr.on_confirm(ts(2))  # a poll found the book unchanged: the print is reflected in it
    assert tr.snapshot(ts(2.5)).x[IDX["stale_dev"]] == 0.0
    tr.on_trade(ts(3), 4700, c(10), +1)
    assert tr.snapshot(ts(3.5)).x[IDX["stale_dev"]] != 0
    tr.on_book(ts(4), BOOK["yes"], BOOK["no"])
    assert tr.snapshot(ts(4)).x[IDX["stale_dev"]] == 0.0


def test_a_print_stops_counting_as_newer_than_the_book_after_ten_seconds():
    tr = tracker_with(BOOK["yes"], BOOK["no"], at=0)
    tr.on_trade(ts(1), 4700, c(10), +1)
    assert tr.snapshot(ts(5)).x[IDX["stale_dev"]] == pytest.approx(-0.03)
    assert tr.snapshot(ts(12)).x[IDX["stale_dev"]] == 0.0  # 11 s old: the book has had its chance


def test_realised_movement_and_return_come_from_recorded_mids():
    tr = MarketTracker()
    for sec, bid in [
        (0, 4000),
        (20, 4200),
        (40, 4100),
        (70, 4400),
    ]:  # 2c spread: mids .41 .43 .42 .45
        tr.on_book(ts(sec), [(bid, c(100))], [(10_000 - bid - 200, c(100))])
    x = tr.snapshot(ts(70)).x
    # changes that arrived in the last 60 s (after t = 10): .41->.43, .43->.42, .42->.45
    assert x[IDX["rv_60"]] == pytest.approx(0.02 + 0.01 + 0.03)
    assert x[IDX["ret_60"]] == pytest.approx(0.45 - 0.41)  # the last snapshot at or before t = 10
    assert x[IDX["lv_10"]] == 0.0
    tr.on_trade(ts(65), 4300, c(100), 1)
    assert tr.snapshot(ts(70)).x[IDX["lv_10"]] == pytest.approx(math.log1p(100.0))  # 100 contracts
    assert tr.snapshot(ts(70)).x[IDX["lv_60"]] == pytest.approx(math.log1p(100.0))  # 100 contracts


def test_features_are_none_without_a_two_sided_uncrossed_book():
    assert tracker_with([(4500, c(10))], []).snapshot(ts(0)) is None
    assert MarketTracker().snapshot(ts(0)) is None
    assert tracker_with([(6000, c(10))], [(6000, c(10))]).snapshot(ts(0)) is None  # bid 60 > ask 40


@settings(max_examples=80, deadline=None)
@given(st.data())
def test_features_at_t_depend_only_on_events_up_to_t(data):
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(2, 25))):
        t += data.draw(st.sampled_from([0.0, 0.3, 2.0, 15.0, 70.0]))
        if data.draw(st.booleans()):
            y = data.draw(st.integers(30, 60)) * 100
            n = data.draw(st.integers(30, 60)) * 100
            events.append(
                (
                    "book",
                    t,
                    [(y, c(data.draw(st.integers(1, 90))))],
                    [(n, c(data.draw(st.integers(1, 90))))],
                )
            )
        else:
            events.append(
                (
                    "trade",
                    t,
                    data.draw(st.integers(20, 80)) * 100,
                    data.draw(st.sampled_from([-1, 0, 1])),
                )
            )
    cut = data.draw(st.floats(0, t + 1))

    def replay(evs):
        tr = MarketTracker()
        for e in evs:
            if e[0] == "book":
                tr.on_book(ts(e[1]), e[2], e[3])
            else:
                tr.on_trade(ts(e[1]), e[2], c(5), e[3])
        return tr.snapshot(ts(cut))

    full = replay([e for e in events if e[1] <= cut])
    later = replay([e for e in events if e[1] <= cut] + [("trade", cut + 1, 5000, 1)])
    # feeding an event AFTER the cut must not change what the cut sees (features are read at `cut`)
    assert (full is None and later is None) or full.x == later.x


# ------------------------------------------------------------------ the fitted-model object
def test_ridge_predict_matches_predict_one_and_clips():
    r = Ridge(("stale_dev", "flow_60"), (0.0, 0.0), (0.02, 0.5), (0.01, -0.02), 1.0, 5.0, clip=0.03)
    x = np.zeros((3, len(FEATURES)))
    x[:, IDX["stale_dev"]] = [0.0, 0.01, 0.5]
    x[:, IDX["flow_60"]] = [0.0, 0.25, -0.5]
    got = r.predict(x)
    assert got[0] == 0.0 and got[1] == pytest.approx(0.5 * 0.01 - 0.5 * 0.02)  # z = (.5, .5)
    assert got[2] == 0.03  # (25 x .01 + 1 x .02) clipped to the limit
    for i in range(3):
        assert r.predict_one(tuple(x[i])) == pytest.approx(got[i])


def test_a_scale_model_is_non_negative_and_has_a_level():
    r = Ridge(
        ("rv_60",), (0.0,), (0.01,), (-5.0,), 1.0, 10.0, clip=0.5, intercept=0.004, target="abs"
    )
    x = [0.0] * len(FEATURES)
    assert r.predict_one(tuple(x)) == pytest.approx(0.004)
    x[IDX["rv_60"]] = 0.1  # z = 10 -> 0.004 - 50 < 0: floored at zero, never negative
    assert r.predict_one(tuple(x)) == 0.0


# ------------------------------------------------------------------ the strategy
def run(
    events,
    params,
    tickers=("A",),
    infos=None,
    latency_ms=0,
    event_of=None,
    adaptive=True,
    maker=None,
):
    eo = event_of or {t: "E1" for t in tickers}
    if adaptive:
        mm = AdaptiveMarketMaker(list(tickers), params, event_of=eo)
    else:
        mm = BinaryMarketMaker(list(tickers), params, event_of=eo)
    ns = latency_ms * 1_000_000
    cfg = BacktestConfig(
        initial_cash_micro=10**11, latency_submit_ns=ns, latency_ack_ns=ns, latency_cancel_ns=ns,
        fees=FEES, maker=maker or QueueModel.optimistic(), equity_sample_ns=10**12,
    )  # fmt: skip
    res = run_backtest(ListFeed(events), infos or {t: info(t) for t in tickers}, mm, cfg)
    return res, mm


def places(mm, side=None):
    return [q for q in mm.quote_log if q.action == "place" and (side is None or q.side == side)]


def adaptive(**kw):
    react = kw.pop("react_to_trades", False)
    return AdaptiveParams(base=BaselineParams(**FAST), react_to_trades=react, **kw)


def scenario():
    return [
        bu(0, "A", **BOOK),
        trade(2, "A", 4600, Side.NO, 20),
        bu(10, "A", **BOOK),
        bu(15, "A", yes=[(5500, c(500))], no=[(3500, c(500))]),
        trade(16, "A", 5400, Side.NO, 15),
        bu(30, "A", yes=[(5200, c(500))], no=[(4300, c(500))]),
        BookConfirm(ts(40)),
    ]


def sig(res):
    return [(o.submitted_ts, o.side.value, o.price, o.qty) for o in res.orders], [
        (f.exec_ts, f.side.value, f.price, f.qty) for f in res.fills
    ]


def test_with_every_adjustment_off_it_is_the_baseline_quote_for_quote():
    a, _ = run(scenario(), adaptive())
    b, _ = run(scenario(), BaselineParams(**FAST), adaptive=False)
    assert sig(a) == sig(b) and a.final_equity_micro == b.final_equity_micro
    assert sig(a)[1], "the scenario must contain fills, or this proves nothing"


@settings(max_examples=40, deadline=None)
@given(st.data())
def test_with_every_adjustment_off_it_matches_the_baseline_on_random_feeds(data):
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(3, 25))):
        t += data.draw(st.sampled_from([0.0, 0.5, 2.0, 5.0]))
        y, n = data.draw(st.integers(30, 60)) * 100, data.draw(st.integers(30, 60)) * 100
        if y + n <= 9800:
            events.append(bu(t, "A", yes=[(y, c(50))], no=[(n, c(50))]))
        if data.draw(st.booleans()):
            events.append(trade(t, "A", data.draw(st.integers(30, 60)) * 100, Side.NO, 5))
    a, _ = run(events, adaptive(), latency_ms=100)
    b, _ = run(events, BaselineParams(**FAST), adaptive=False, latency_ms=100)
    assert sig(a) == sig(b)


def const_scale(sigma):
    """A scale model that always returns ``sigma``."""
    return Ridge(
        ("rv_60",), (0.0,), (1.0,), (0.0,), 1.0, 10.0, clip=0.5, intercept=sigma, target="abs"
    )


def first_quotes(res_mm):
    yes = places(res_mm, "yes")[0].price
    no = places(res_mm, "no")[0].price
    return yes, 10_000 - no  # the YES bid and the YES ask


def test_widening_moves_both_quotes_out_by_kappa_times_sigma():
    feed = [bu(0, "A", **BOOK)]
    _, base = run(feed, adaptive())
    _, wide = run(feed, adaptive(scale=const_scale(0.02), kappa=1.0))
    (b0, a0), (b1, a1) = first_quotes(base), first_quotes(wide)
    assert b0 - b1 == 200 and a1 - a0 == 200  # 2c further out on each side (both tick-aligned)
    _, none = run(feed, adaptive(scale=const_scale(0.02), kappa=0.0))
    assert first_quotes(none) == (b0, a0)  # kappa = 0: the scale model is inert


def test_size_follows_the_expected_move_between_a_floor_and_a_cap():
    feed = [bu(0, "A", **BOOK)]
    _, m = run(feed, adaptive(scale=const_scale(0.02), size_by_scale=True, sigma_ref=0.01))
    assert places(m, "yes")[0].qty == c(5)  # 10 x .01/.02
    _, m = run(feed, adaptive(scale=const_scale(0.5), size_by_scale=True, sigma_ref=0.01))
    assert places(m, "yes")[0].qty == c(2)  # floored at 20% of 10
    _, m = run(feed, adaptive(scale=const_scale(0.001), size_by_scale=True, sigma_ref=0.01))
    assert places(m, "yes")[0].qty == c(20)  # calm: up to the cap, twice the configured size
    _, m = run(
        feed,
        adaptive(scale=const_scale(0.001), size_by_scale=True, sigma_ref=0.01, max_size_frac=1.0),
    )
    assert places(m, "yes")[0].qty == c(10)  # with the cap at 1: never above the configured size


def stale_shift(w):
    """mu = w x stale_dev (one standardised unit = 1c)."""
    return Ridge(("stale_dev",), (0.0,), (0.01,), (w * 0.01,), 1.0, 5.0, clip=0.05)


def test_a_print_above_the_stale_book_lifts_both_quotes_and_a_print_below_lowers_them():
    up = [bu(0, "A", **BOOK), trade(1, "A", 5300, Side.YES, 5)]  # 3c above the stale mid
    down = [bu(0, "A", **BOOK), trade(1, "A", 4700, Side.NO, 5)]
    kw = dict(fv=stale_shift(1.0), react_to_trades=True)
    _, base = run(up, adaptive())
    _, mu_up = run(up, adaptive(**kw))
    _, mu_dn = run(down, adaptive(**kw))
    b0, a0 = first_quotes(base)
    assert places(mu_up, "yes")[-1].price > b0  # the bid followed the print up
    ask = 10_000 - places(mu_up, "no")[-1].price
    assert ask > a0  # the YES ask (a NO bid at 1 - a) rose too
    assert places(mu_dn, "yes")[-1].price < b0


def test_without_reacting_to_trades_a_print_does_not_move_the_quote_until_the_next_book():
    feed = [bu(0, "A", **BOOK), trade(1, "A", 5200, Side.YES, 5), bu(20, "A", **BOOK)]
    _, m = run(feed, adaptive(fv=stale_shift(1.0), react_to_trades=False))
    assert all(q.ts < ts(1) or q.ts >= ts(20) for q in places(m))  # nothing placed in between


def test_the_fair_value_shift_respects_the_price_band():
    high = dict(yes=[(9400, c(50))], no=[(400, c(50))])  # 94c / 96c: mid 95c, inside the band
    feed = [
        bu(0, "A", **high),
        trade(1, "A", 9900, Side.YES, 5),
    ]  # a print far above: fair -> ~1.00
    _, m = run(feed, adaptive(fv=stale_shift(5.0), react_to_trades=True))
    assert places(m) and all(
        q.ts < ts(1) for q in places(m)
    )  # quoted before the print, never after
    assert any(q.action == "cancel" and q.ts >= ts(1) for q in m.quote_log)  # and pulled on it


def test_time_to_resolution_skews_harder_near_the_end_only_when_switched_on():
    events = [
        bu(0, "A", **BOOK),
        trade(2, "A", 4600, Side.NO, 20),  # we buy YES: long
        bu(10, "A", **BOOK),
    ]
    near = {"A": info("A", end_in_h=0.6)}  # 36 min left, inside the 15 min stop
    _, off = run(events, adaptive(), infos=near)
    _, on = run(events, adaptive(ttr_skew=2.0, ttr_tau0_h=1.0), infos=near)
    off_bid = [q for q in places(off, "yes") if q.ts >= ts(2)][0].price
    on_bid = [q for q in places(on, "yes") if q.ts >= ts(2)][0].price
    assert on_bid < off_bid  # long inventory + more risk aversion -> pay less for more
    far = {"A": info("A", end_in_h=50.0)}
    off_f, _ = run(events, adaptive(), infos=far)
    on_f, _ = run(events, adaptive(ttr_skew=2.0), infos=far)
    assert sig(off_f) == sig(on_f)  # 50 h out: exp(-50) ~ 0, the same quotes


@settings(max_examples=40, deadline=None)
@given(st.data())
def test_at_most_one_live_quote_per_side_even_when_it_reprices_on_prints(data):
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(3, 30))):
        t += data.draw(st.sampled_from([0.0, 0.2, 1.0, 3.0]))
        y, n = data.draw(st.integers(30, 60)) * 100, data.draw(st.integers(30, 60)) * 100
        if y + n <= 9800:
            events.append(bu(t, "A", yes=[(y, c(50))], no=[(n, c(50))]))
        if data.draw(st.booleans()):
            events.append(trade(t, "A", data.draw(st.integers(30, 60)) * 100, Side.NO, 5))
    params = AdaptiveParams(base=BaselineParams(**{**FAST, "busy_s": 1.0}), fv=stale_shift(1.0),
                            scale=const_scale(0.01), kappa=1.0, size_by_scale=True, react_to_trades=True,
                            trade_react_s=0.0)  # fmt: skip
    mm = AdaptiveMarketMaker(["A"], params, event_of={"A": "E1"})
    seen, real = [], mm.on_event

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


@settings(max_examples=40, deadline=None)
@given(st.data())
def test_decisions_up_to_t_depend_only_on_events_up_to_t(data):
    events, t = [], 0.0
    for _ in range(data.draw(st.integers(4, 30))):
        t += data.draw(st.sampled_from([0.0, 0.5, 2.0, 5.0]))
        y, n = data.draw(st.integers(30, 60)) * 100, data.draw(st.integers(30, 60)) * 100
        if y + n <= 9800:
            events.append(bu(t, "A", yes=[(y, c(50))], no=[(n, c(50))]))
        if data.draw(st.booleans()):
            events.append(trade(t, "A", data.draw(st.integers(30, 60)) * 100, Side.NO, 5))
    if not events:
        return
    cut = ts(data.draw(st.floats(0, t + 1)))
    p = adaptive(fv=stale_shift(1.0), scale=const_scale(0.01), kappa=1.0, size_by_scale=True,
                 react_to_trades=True)  # fmt: skip
    full, _ = run(events, p, latency_ms=100)
    part, _ = run([e for e in events if e.ts_ns <= cut], p, latency_ms=100)

    def keep(res):
        return [
            (o.submitted_ts, o.side.value, o.price, o.qty)
            for o in res.orders
            if o.submitted_ts <= cut
        ]

    assert keep(full) == keep(part)


# ------------------------------------------------------------------ gaps found by mutation testing
def test_a_print_far_from_the_mid_is_not_read_as_a_price_signal():
    tr = tracker_with(BOOK["yes"], BOOK["no"], at=0)
    tr.on_trade(ts(1), 9000, c(10), +1)  # 40c above the mid: a fat finger or a block, not a price
    x = tr.snapshot(ts(2)).x
    assert x[IDX["stale_dev"]] == 0.0 and x[IDX["stale_out"]] == 0.0


def test_trade_intensity_windows_are_ten_and_sixty_seconds():
    tr = tracker_with(BOOK["yes"], BOOK["no"], at=0)
    tr.on_trade(ts(30), 4700, c(100), +1)
    x = tr.snapshot(ts(45)).x  # the print is 15 s old
    assert x[IDX["lv_10"]] == 0.0
    assert x[IDX["lv_60"]] == pytest.approx(math.log1p(100.0))


def test_depth_imbalance_uses_exactly_three_levels_a_side():
    yes = [(4500, c(100)), (4400, c(100)), (4300, c(100)), (4200, c(900))]  # a big 4th level
    no = [(4500, c(100)), (4400, c(100)), (4300, c(100)), (4200, c(100))]
    x = tracker_with(yes, no).snapshot(ts(0)).x
    assert x[IDX["obi_depth"]] == 0.0  # 300 vs 300: the fourth level is ignored


def test_widening_is_proportional_to_kappa():
    feed = [bu(0, "A", **BOOK)]
    _, base = run(feed, adaptive())
    _, k1 = run(feed, adaptive(scale=const_scale(0.01), kappa=1.0))
    _, k3 = run(feed, adaptive(scale=const_scale(0.01), kappa=3.0))
    (b0, a0), (b1, a1), (b3, a3) = first_quotes(base), first_quotes(k1), first_quotes(k3)
    assert (b0 - b1, a1 - a0) == (100, 100)  # 1c
    assert (b0 - b3, a3 - a0) == (300, 300)  # 3c: kappa scales it, sigma alone does not


def test_a_poll_that_finds_the_book_unchanged_clears_the_staleness_correction():
    model = dict(fv=stale_shift(1.0), react_to_trades=True)
    up = trade(1, "A", 5200, Side.YES, 5)  # 2c above the mid, inside our ask: no fill
    at_mid = trade(3, "A", 5000, Side.YES, 1)
    feed_confirm = [bu(0, "A", **BOOK), up, BookConfirm(ts(2)), at_mid]  # the poll saw no change
    feed_plain = [bu(0, "A", **BOOK), up, at_mid]
    _, base = run(feed_confirm, adaptive())
    res_c, m_c = run(feed_confirm, adaptive(**model))
    _, m_p = run(feed_plain, adaptive(**model))
    b0, _ = first_quotes(base)
    assert not res_c.fills
    assert places(m_p, "yes")[-1].price > b0  # without the poll, the 52c print still drags it up
    assert places(m_c, "yes")[-1].price == b0  # the polled book already contained the print


def test_a_return_is_not_measured_against_a_mid_from_before_an_outage():
    tr = MarketTracker()
    tr.on_book(ts(0), [(4000, c(100))], [(5800, c(100))])  # mid .41
    tr.on_book(ts(3600), [(5000, c(100))], [(4800, c(100))])  # an hour later: mid .51
    assert tr.snapshot(ts(3600)).x[IDX["ret_60"]] == 0.0  # the .41 is not "the mid 60 s ago"
    tr.on_book(ts(3640), [(5100, c(100))], [(4700, c(100))])
    tr.on_book(ts(3700), [(5300, c(100))], [(4500, c(100))])  # mid .54; 60 s earlier: .52 at t=3640
    assert tr.snapshot(ts(3700)).x[IDX["ret_60"]] == pytest.approx(0.54 - 0.52)
