import numpy as np
import pytest

from market_making.baseline import BaselineParams
from market_making.features import FEATURES
from market_making.shortterm import Ridge
from research import adaptive_analysis as AA
from research import adaptive_hypotheses as H
from research import market_making_analysis as A
from research import signal_study as S


def fill(cluster, qty, pnl_cents, markout=None, ds="d"):
    """A FillRow with a given settled P&L (in cents) and contract count."""
    return A.FillRow(
        dataset=ds, ticker="T", event=cluster, category="Sports", ts=0, side="yes", price=5000,
        qty=int(qty * 100), fee=0, mid_yes=5000.0, edge_micro=0.0, ttr_h=1.0, prob=0.5, volume_1h=0.0,
        inv_after=0, markout30=markout, settle_pnl=int(pnl_cents / 100 * A.MICRO),
    )  # fmt: skip


# ------------------------------------------------------------------ size calibration
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_sigma_ref_makes_the_average_size_fraction_one(seed):
    rng = np.random.default_rng(seed)
    pred = np.exp(rng.normal(-5, 1, 5000))
    ref = AA.calibrate_sigma_ref(pred, 0.2, 2.0)
    assert np.clip(ref / pred, 0.2, 2.0).mean() == pytest.approx(1.0, abs=1e-6)
    assert AA.calibrate_sigma_ref(pred * 2, 0.2, 2.0) == pytest.approx(
        2 * ref, rel=1e-6
    )  # scale-free


# ------------------------------------------------------------------ paired rates per contract
def test_a_variant_that_only_trades_less_is_not_credited_per_contract():
    base = [fill("e1", 10, -30), fill("e2", 20, -60), fill("e3", 10, 5)]
    half = [fill("e1", 5, -15), fill("e2", 10, -30), fill("e3", 5, 2.5)]
    d = AA.paired_ratio_difference(half, base, *AA.SETTLED, n_boot=200)
    assert d["value"] == pytest.approx(0.0, abs=1e-12)  # same cents per contract, half the volume
    assert d["lo"] == pytest.approx(0.0, abs=1e-9) and d["hi"] == pytest.approx(0.0, abs=1e-9)
    # ...while the total P&L per event, which does not normalise, does credit it
    ex = {"d:e1": -15.0, "d:e2": -30.0, "d:e3": 2.5}
    eb = {"d:e1": -30.0, "d:e2": -60.0, "d:e3": 5.0}
    assert AA.paired_difference(ex, eb, n_boot=200)["value"] > 0


def test_paired_ratio_difference_by_hand():
    x = [fill("a", 10, 20), fill("b", 10, -10)]  # +2c/contract on a, -1c on b: pooled +0.5c
    b = [fill("a", 5, 0), fill("b", 15, -30)]  # 0c on a, -2c on b: pooled -1.5c
    d = AA.paired_ratio_difference(x, b, *AA.SETTLED, n_boot=200)
    assert d["value"] == pytest.approx(10 / 20 - (-30) / 20)  # 0.5 - (-1.5) = 2.0
    assert d["n_clusters"] == 2


def test_unsettled_fills_and_markouts_are_excluded_from_the_right_rates():
    f = fill("a", 10, 5)
    unsettled = A.FillRow(**{**f.__dict__, "settle_pnl": None, "markout30": 200.0})
    assert AA.SETTLED[0](unsettled) is None
    assert AA.MARKOUT[0](unsettled) == pytest.approx(2.0 * 10)  # 200 ticks = 2 cents x 10 contracts
    assert AA.MARKOUT[0](f) is None
    r = AA.rate([f, unsettled], AA.SETTLED, n_boot=50, seed=0)
    assert r["value"] == pytest.approx(5 / 10)


def test_event_pnl_lists_every_settled_event_and_only_settled_fills():
    class D:
        name = "d"
        settle = {"T1": 10_000, "T2": 0, "T3": 10_000}
        event_of = {"T1": "e1", "T2": "e2", "T3": "e1"}

    class R:
        data = D()

    fills = [fill("e1", 10, 50), fill("e1", 10, -20), fill("e9", 5, 999)]
    fills[2] = A.FillRow(**{**fills[2].__dict__, "settle_pnl": None})
    out = AA.event_pnl(R(), fills)
    assert out == {"d:e1": pytest.approx(0.30), "d:e2": 0.0}  # e9 has no settled market: not listed


# ------------------------------------------------------------------ the ladder on a toy feed
def toy_models():
    zero = Ridge(("stale_dev",), (0.0,), (0.01,), (0.0,), 1.0, 5.0, clip=0.05)
    scale = Ridge(
        ("rv_60",), (0.0,), (0.01,), (0.0,), 1.0, 10.0, clip=0.5, intercept=0.004, target="abs"
    )
    return AA.Models(zero, zero, scale, 0.004, ("toy",))


def test_the_ladder_names_and_switches():
    v = AA.variants(toy_models(), BaselineParams(), 1.0)
    names = list(v)
    assert names[0].startswith("0") and v[names[0]] is None  # the baseline runs its own code path
    full = v["6 + time-to-resolution skew (= FULL)"]
    assert full.fv is not None and full.scale is not None and full.kappa == 1.0
    assert full.size_by_scale and full.ttr_skew == 1.0
    assert v["full - fair value"].fv is None and v["full - fair value"].kappa == 1.0
    assert v["full - widening"].kappa == 0.0 and v["full - widening"].size_by_scale
    assert not v["full - size"].size_by_scale and v["full - size"].kappa == 1.0
    assert v["full - time skew"].ttr_skew == 0.0 and v["full - time skew"].size_by_scale
    r1 = v["1 + re-price on prints (no model)"]
    assert r1.fv is None and r1.scale is None and r1.kappa == 0 and r1.react_to_trades
    # each rung adds exactly one component to the previous one
    assert v["4 + widening by expected move"].scale is not None
    assert v["4 + widening by expected move"].fv is v["2 + fair value: staleness correction"].fv
    assert not v["4 + widening by expected move"].size_by_scale
    assert (
        v["5 + size by expected move"].size_by_scale
        and v["5 + size by expected move"].ttr_skew == 0
    )


# ------------------------------------------------------------------ signal scoring
def samples_with(x_stale, y, events):
    n = len(y)
    x = np.zeros((n, len(FEATURES)))
    x[:, FEATURES.index("stale_dev")] = x_stale
    return S.Samples(
        ["T"] * n, events, ["Sports"] * n, np.arange(n, dtype=np.int64), x, np.full(n, 0.5),
        np.full(n, 0.02), np.full(n, 1.0), {5.0: np.asarray(y, float)}, "toy",
    )  # fmt: skip


def test_skill_is_positive_for_a_model_that_knows_the_move_and_negative_for_the_wrong_sign():
    rng = np.random.default_rng(0)
    xs = rng.normal(0, 0.02, 600)
    y = xs * 0.5 + rng.normal(0, 0.002, 600)
    ev = [f"e{i % 30}" for i in range(600)]
    s = samples_with(xs, y, ev)
    good = Ridge(("stale_dev",), (0.0,), (0.02,), (0.01,), 0.0, 5.0)  # mu = 0.5 * x
    bad = Ridge(("stale_dev",), (0.0,), (0.02,), (-0.01,), 0.0, 5.0)
    g, b = S.skill(s, good, n_boot=100), S.skill(s, bad, n_boot=100)
    assert g["skill_vs_zero"] > 0.9 and g["lo"] > 0.8
    assert b["skill_vs_zero"] < 0 and b["hi"] < 0
    assert g["events"] == 30 and g["corr"] > 0.97


def test_scale_skill_rewards_a_model_that_tells_calm_from_active():
    rng = np.random.default_rng(1)
    act = rng.uniform(0, 1, 800)
    y = np.where(rng.random(800) < 0.5, 1, -1) * (
        0.002 + 0.02 * act
    )  # the size of the move follows act
    n = len(y)
    x = np.zeros((n, len(FEATURES)))
    x[:, FEATURES.index("rv_60")] = act
    s = S.Samples(["T"] * n, [f"e{i % 40}" for i in range(n)], ["S"] * n, np.arange(n, dtype=np.int64), x,
                  np.full(n, 0.5), np.full(n, 0.02), np.full(n, 1.0), {10.0: y}, "toy")  # fmt: skip
    m = S.fit_ridge(s, ("rv_60",), 10.0, 1.0, clip=0.5, target="abs")
    assert S.abs_skill(s, m, n_boot=100)["skill_vs_mean"] > 0.95
    flat = Ridge(
        ("rv_60",),
        (0.0,),
        (1.0,),
        (0.0,),
        1.0,
        10.0,
        clip=0.5,
        intercept=float(np.abs(y).mean()),
        target="abs",
    )
    assert S.abs_skill(s, flat, n_boot=100)["skill_vs_mean"] == pytest.approx(0.0, abs=1e-9)


# ------------------------------------------------------------------ the hypotheses
def est(v, lo=None, hi=None, n=30):
    return {"value": v, "lo": lo, "hi": hi, "n_clusters": n}


def toy_block(**over):
    row = lambda **k: {  # noqa: E731
        "net_usd": -50.0, "settled_pnl_total_usd": -40.0, "contracts": 100.0, "fills": 50,
        "settled_pnl_cents_per_contract": est(-2.0, -3.0, -1.0), "markout_30s_cents_per_contract": est(-1.5, -2.0, -1.0),
        **k,
    }  # fmt: skip
    cmp_ = lambda v, lo, hi: {"settled_cents_per_contract": est(v, lo, hi)}  # noqa: E731
    full = row(
        contracts=40.0,
        vs_baseline={
            "event_pnl_total_usd": est(1.5, 0.5, 2.5),
            "settled_cents_per_contract": est(-0.3, -1.0, 0.5),
            "markout_cents_per_contract": est(0.1, -0.2, 0.4),
        },
    )
    b = {
        "signal": {
            "fair_value_staleness": {"skill_vs_zero": 0.01, "lo": 0.005, "hi": 0.02, "events": 30},
            "fair_value_all_direction_features": {"skill_vs_zero": -0.01, "lo": -0.02, "hi": 0.0, "events": 30},
            "scale_of_next_move": {"skill_vs_mean": 0.08, "lo": 0.05, "hi": 0.1, "events": 30},
        },
        "ladder": {
            "rows": {H.BASE: row(contracts=100.0), H.FULL: full},
            "contrasts": {
                "2 vs 1": cmp_(0.1, -0.5, 0.7),
                "3 vs 2": cmp_(-0.4, -1.0, 0.2),
                "6 vs 5": cmp_(0.0, -0.4, 0.4),
            },
        },
        "fill_sensitivity": [{"full_net_usd": -1.0}, {"full_net_usd": -5.0}],
    }  # fmt: skip
    b.update(over)
    return b


def test_the_pattern_the_hypotheses_expect_is_scored_consistent():
    v = {h: verdict for h, verdict, *_ in H.evaluate(toy_block())}
    assert set(v) == {"S1", "S2", "S3", "T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8"}
    assert all(x == "consistent" for x in v.values()), v


def test_the_staleness_and_extra_feature_hypotheses_test_what_they_say():
    b = toy_block()
    b["ladder"]["contrasts"]["2 vs 1"] = {"settled_cents_per_contract": est(-0.2, -0.6, 0.2)}
    b["ladder"]["contrasts"]["3 vs 2"] = {"settled_cents_per_contract": est(0.9, 0.3, 1.5)}
    v = {h: verdict for h, verdict, *_ in H.evaluate(b)}
    assert v["T4"] == "NOT consistent"  # the staleness fair value made fills worse
    assert v["T5"] == "NOT consistent"  # extra features gained significantly


def test_a_significant_improvement_or_a_profit_contradicts_them():
    b = toy_block()
    full = b["ladder"]["rows"][H.FULL]
    full["net_usd"] = 30.0
    full["vs_baseline"]["settled_cents_per_contract"] = est(2.0, 1.0, 3.0)
    b["fill_sensitivity"] = [{"full_net_usd": 4.0}]
    v = {h: verdict for h, verdict, *_ in H.evaluate(b)}
    assert v["T1"] == v["T2"] == v["T3"] == v["T8"] == "NOT consistent"


def test_claims_on_too_few_events_are_inconclusive():
    b = toy_block()
    b["signal"]["fair_value_staleness"]["events"] = 8
    b["ladder"]["rows"][H.FULL]["vs_baseline"]["settled_cents_per_contract"]["n_clusters"] = 5
    v = {h: verdict for h, verdict, *_ in H.evaluate(b)}
    assert v["S1"].startswith("inconclusive") and v["T2"].startswith("inconclusive")


def test_event_pnl_nets_fees_out_of_settled_pnl():
    class D:
        name = "d"
        settle = {"T1": 10_000}
        event_of = {"T1": "e1"}

    class R:
        data = D()

    f = fill("e1", 10, 100)  # +$1.00 settled
    f = A.FillRow(**{**f.__dict__, "fee": 250_000})  # a $0.25 fee
    assert AA.event_pnl(R(), [f]) == {"d:e1": pytest.approx(0.75)}


# ------------------------------------------------------------------ labelled samples from a recorded feed
def _toy_feed(close_at=None):
    from datetime import UTC, datetime, timedelta

    from backtest.events import BookUpdate, MarketClose, TradeTick
    from backtest.feed import ListFeed
    from backtest.market_info import MarketInfo
    from market.contracts import Rules, Side, Strike, Trade

    T0 = 1_800_000_000 * S.SEC

    def book(sec, mid_bid):
        return BookUpdate(
            T0 + int(sec * S.SEC), "A", ((mid_bid, 10_000),), ((10_000 - mid_bid - 200, 10_000),)
        )  # spread 2c: mid = bid + 1c

    end = datetime.fromtimestamp(T0 / S.SEC, UTC) + timedelta(hours=2)
    info = MarketInfo("A", "E1", "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())
    tr = Trade("t1", "A", 5000, 5000, 1000, Side.YES, "bid", datetime.now(UTC))
    events = [book(0, 4900), book(10, 5100), book(20, 5400), TradeTick(T0 + 5 * S.SEC, "A", tr)]
    if close_at is not None:
        events.append(MarketClose(T0 + int(close_at * S.SEC), "A"))
    feed = ListFeed(events)
    feed.infos = {"A": info}
    return feed


def test_labels_are_the_mid_change_h_seconds_later_and_include_the_update_at_exactly_t_plus_h():
    s = S.build_samples(_toy_feed(), "toy", horizons=(5.0, 10.0), min_gap_s=1.0)
    by_t = {int((t - s.ts[0]) / S.SEC): i for i, t in enumerate(s.ts)}
    i0 = by_t[0]
    assert s.mid[i0] == pytest.approx(0.50)
    assert s.y[5.0][i0] == pytest.approx(0.0)  # nothing recorded between 0 and 5 s: the mid held
    assert s.y[10.0][i0] == pytest.approx(0.52 - 0.50)  # the update AT t + 10 s counts
    i10 = by_t[10]
    assert s.y[10.0][i10] == pytest.approx(0.55 - 0.52)  # a rise is a positive change
    assert s.ttr_h[i0] == pytest.approx(2.0)


def test_a_label_is_dropped_when_the_market_is_not_observed_alive_until_t_plus_h():
    s = S.build_samples(_toy_feed(close_at=8), "toy", horizons=(5.0, 10.0), min_gap_s=1.0)
    i0 = 0
    assert not np.isnan(s.y[5.0][i0])  # alive at 5 s
    assert np.isnan(s.y[10.0][i0])  # closed at 8 s: no defensible label at 10 s


def test_features_are_sampled_at_trades_as_well_as_books_and_respect_the_gap():
    s = S.build_samples(_toy_feed(), "toy", horizons=(5.0,), min_gap_s=1.0)
    assert len(s) == 4  # three book updates and the print at 5 s
    s2 = S.build_samples(_toy_feed(), "toy", horizons=(5.0,), min_gap_s=8.0)
    assert len(s2) == 3  # samples closer than 8 s to the previous one are skipped


# ------------------------------------------------------------------ recording outages
def _outage_feed():
    """A recorder that polls every 10 s, then goes dark from 20 s to 200 s while the tape keeps going."""
    from datetime import UTC, datetime, timedelta

    from backtest.events import BookConfirm, BookUpdate, TradeTick
    from backtest.feed import ListFeed
    from backtest.market_info import MarketInfo
    from market.contracts import Rules, Side, Strike, Trade

    T0 = 1_800_000_000 * S.SEC

    def at(sec):
        return T0 + int(sec * S.SEC)

    def book(sec, bid):
        return BookUpdate(at(sec), "A", ((bid, 10_000),), ((10_000 - bid - 200, 10_000),))

    def trade(sec, tid):
        tr = Trade(tid, "A", 5000, 5000, 1000, Side.YES, "bid", datetime.now(UTC))
        return TradeTick(at(sec), "A", tr)

    end = datetime.fromtimestamp(T0 / S.SEC, UTC) + timedelta(hours=2)
    info = MarketInfo("A", "E1", "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())
    events = [
        book(0, 4900),
        BookConfirm(at(0)),
        BookConfirm(at(10)),
        trade(15, "before"),  # a print 5 s after the last poll: a fine sample
        BookConfirm(at(20)),
        trade(100, "inside-outage"),  # the recorder was dark: no book for 80 s
        trade(150, "inside-outage-2"),
        BookConfirm(at(200)),
        book(200, 5400),  # the mid jumped while nobody was looking
        trade(205, "after"),
        BookConfirm(at(210)),
    ]
    feed = ListFeed(events)
    feed.infos = {"A": info}
    return feed


def test_prints_inside_a_recording_outage_are_not_sampled():
    s = S.build_samples(_outage_feed(), "toy", horizons=(5.0,), min_gap_s=1.0)
    secs = sorted(round((t - s.ts.min()) / S.SEC) for t in s.ts)
    assert 100 not in secs and 150 not in secs  # the book was 80-130 s old: no defensible sample
    assert 15 in secs and 205 in secs and 0 in secs and 200 in secs


def test_a_label_that_crosses_an_outage_is_dropped_rather_than_recorded_as_no_move():
    s = S.build_samples(_outage_feed(), "toy", horizons=(5.0, 30.0), min_gap_s=1.0)
    t0 = s.ts.min()
    by = {round((t - t0) / S.SEC): i for i, t in enumerate(s.ts)}
    assert not np.isnan(s.y[5.0][by[0]])  # 0 -> 5 s: fully observed
    assert not np.isnan(s.y[5.0][by[15]])  # 15 -> 20 s: ends exactly as the outage begins
    assert np.isnan(s.y[30.0][by[15]])  # 15 -> 45 s runs into the outage
    assert not np.isnan(s.y[5.0][by[200]])  # 200 -> 205 s: after it


def test_a_feed_without_poll_confirmations_has_no_outages():
    s = S.build_samples(_toy_feed(), "toy", horizons=(5.0,), min_gap_s=1.0)
    assert len(s) == 4 and not np.isnan(
        s.y[5.0][0]
    )  # unchanged behaviour for a feed of book changes
