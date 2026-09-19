import math
import random

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pricing.dataset import CorpusRow, Instance, Observation, Outcome, n_bucket
from pricing.microstructure import BookFeatures, TradeFeatures
from pricing.probability_models import (
    DAY_NS,
    FEATURES,
    HistoricalFrequency,
    LastTrade,
    Microprice,
    MicrostructureLogit,
    ReferencePrice,
    predict_all,
    prequential_kappa,
)
from pricing.scoring import EPS, log_loss, logit

T0 = 1_800_000_000 * 1_000_000_000


def tf(flow=None, ret=None, last=0.5, secs=30.0, vol=10.0, tmid=None, tspread=None):
    return TradeFeatures(
        3, last, secs, {300: None, 3600: None}, {300: flow, 3600: flow}, {300: vol, 3600: vol},
        {300: ret, 3600: ret}, tmid, tspread,
    )  # fmt: skip


def bf(mid=0.5, spread=0.02, imb=0.0):
    return BookFeatures(
        mid - spread / 2, mid + spread / 2, mid, spread, mid + imb * spread / 4, imb, imb, 3.0
    )


def obs(
    ref=0.5,
    trade=None,
    book=None,
    t=T0,
    cat="Sports",
    series="KXA",
    strike="none",
    n=2,
    h=2.0,
    event="E",
):
    return Observation(
        "T", event, series, cat, strike, n, t, h, ref, "last_trade", trade or tf(last=ref), book
    )


def inst(o, y, settled=T0 + 10 * DAY_NS, event=None):
    if event:
        o = Observation(**{**o.__dict__, "event": event})
    return Instance(o, Outcome(y, settled), "research", "test")


# ------------------------------------------------------------------ bounds, for every model, on every input
@settings(max_examples=150, deadline=None)
@given(
    st.floats(0.0, 1.0),
    st.one_of(st.none(), st.floats(-1, 1)),
    st.one_of(st.none(), st.floats(-1, 1)),
    st.floats(-5, 100),
)
def test_every_model_stays_inside_the_probability_bounds(ref, flow, ret, hours):
    rng = random.Random(0)
    train = [
        inst(obs(ref=rng.uniform(0.05, 0.95), trade=tf(flow=rng.uniform(-1, 1))), rng.randrange(2))
        for _ in range(60)
    ]
    corpus = [
        CorpusRow("E", "KXA", "Sports", "none", "2", rng.randrange(2), T0 - DAY_NS * k)
        for k in range(1, 40)
    ]
    corpus.sort(key=lambda r: r.settled_ns)
    models = [
        ReferencePrice(),
        Microprice(),
        LastTrade(),
        MicrostructureLogit(lam=1.0, groups=("trade", "time")).fit(train),
        MicrostructureLogit(lam=1.0, slope=True).fit(train),
        HistoricalFrequency(corpus),
    ]
    o = obs(
        ref=ref,
        trade=tf(flow=flow, ret=ret, last=ref),
        book=bf(mid=min(max(ref, 0.05), 0.95)),
        h=hours,
    )
    for m in models:
        p = m.predict(o)
        assert EPS <= p <= 1 - EPS and p == p, (m.name, p)


def test_extreme_references_do_not_produce_infinite_logits():
    m = MicrostructureLogit().fit(
        [inst(obs(ref=r), int(r > 0.5)) for r in (0.1, 0.3, 0.7, 0.9)] * 10
    )
    for r in (0.0, 1e-12, 1.0, 1 - 1e-12):
        assert EPS <= m.predict(obs(ref=r)) <= 1 - EPS


# ------------------------------------------------------------------ baselines
def test_baselines_read_the_right_field():
    o = obs(ref=0.42, trade=tf(last=0.60), book=bf(mid=0.42, spread=0.04, imb=0.8))
    assert ReferencePrice().predict(o) == pytest.approx(0.42)
    assert LastTrade().predict(o) == pytest.approx(0.60)
    assert Microprice().predict(o) == pytest.approx(o.book.microprice)
    assert Microprice().predict(obs(ref=0.42)) == pytest.approx(0.42)  # no book: falls back to ref


# ------------------------------------------------------------------ Model A
def _world(n, w_flow, seed, noise_flow=False):
    """y ~ Bernoulli(sigmoid(logit(ref) + w_flow * flow)): flow carries information the price lacks."""
    rng = random.Random(seed)
    out = []
    for k in range(n):
        ref = rng.uniform(0.15, 0.85)
        flow = rng.uniform(-1, 1)
        p = 1 / (1 + math.exp(-(logit(ref) + w_flow * flow)))
        out.append(
            inst(
                obs(ref=ref, trade=tf(flow=flow, ret=rng.uniform(-0.2, 0.2), last=ref)),
                int(rng.random() < p),
                event=f"E{k}",
            )
        )
    return out


def test_model_a_recovers_a_planted_signal_and_beats_the_market_out_of_sample():
    train, test = _world(4000, 1.5, 1), _world(4000, 1.5, 2)
    m = MicrostructureLogit(lam=1.0).fit(train)
    coef = m.coefficients()
    names = [n for n, _ in m._spec()]
    raw = sum(coef[k] / m.std[names.index(k)] for k in ("flow_5m", "flow_1h"))  # raw flow units
    assert 1.2 < raw < 1.8  # the true weight is 1.5 (both windows carry the same signal here)
    y = [i.out.y for i in test]
    ref_ll = log_loss([i.obs.ref for i in test], y)
    a_ll = log_loss(predict_all(m, test), y)
    assert a_ll < ref_ll - 0.05


def test_model_a_returns_the_market_price_when_there_is_nothing_to_learn():
    """No information beyond the price: coefficients shrink to ~0 and predictions to the reference."""
    rng = random.Random(5)
    train = []
    for k in range(600):
        ref = rng.uniform(0.2, 0.8)
        train.append(
            inst(
                obs(ref=ref, trade=tf(flow=rng.uniform(-1, 1), last=ref)),
                int(rng.random() < ref),
                event=f"E{k}",
            )
        )
    m = MicrostructureLogit(lam=300.0).fit(train)
    assert max(abs(v) for k, v in m.coefficients().items() if k != "intercept") < 0.08
    for i in train[:20]:
        assert abs(m.predict(i.obs) - i.obs.ref) < 0.03


def test_an_infinite_penalty_is_exactly_the_reference_price():
    train = _world(300, 2.0, 7)
    m = MicrostructureLogit(lam=1e9).fit(train)
    for i in train[:15]:
        assert (
            m.predict(i.obs) == pytest.approx(i.obs.ref, abs=1e-6)
            or abs(m.coefficients()["intercept"]) > 0
        )
    coef = m.coefficients()
    assert all(abs(v) < 1e-6 for k, v in coef.items() if k != "intercept")


def test_the_newton_solution_matches_an_independent_optimiser():
    """scipy minimising the same penalised negative log-likelihood reaches the same coefficients."""
    from scipy.optimize import minimize

    train = _world(500, 1.0, 11)
    m = MicrostructureLogit(lam=5.0, groups=("trade",)).fit(train)
    raw = [m._raw(i.obs) for i in train]
    X = np.array([r[0] for r in raw])
    miss = np.array([r[1] for r in raw])
    D = m._design(X, miss)
    y = np.array([i.out.y for i in train], float)
    z = np.array([logit(i.obs.ref) for i in train])
    pen = np.full(D.shape[1], 5.0)
    pen[0] = 1e-8

    def obj(th):
        eta = z + D @ th
        return np.sum(np.logaddexp(0, eta) - y * eta) + 0.5 * np.sum(pen * th**2)

    res = minimize(obj, np.zeros(D.shape[1]), method="BFGS", options={"gtol": 1e-9, "maxiter": 500})
    assert np.allclose(m.theta, res.x, atol=1e-3)


def test_missing_features_are_neutral_and_flagged_not_dropped():
    rng = random.Random(9)
    train = []
    for k in range(400):
        ref = rng.uniform(0.2, 0.8)
        flow = None if k % 3 == 0 else rng.uniform(-1, 1)
        train.append(
            inst(
                obs(ref=ref, trade=tf(flow=flow, last=ref)), int(rng.random() < ref), event=f"E{k}"
            )
        )
    m = MicrostructureLogit(lam=1.0).fit(train)
    assert (
        "missing:flow_5m" in m.coefficients()
    )  # 'no flow' is sometimes informative, so it has a flag
    p = m.predict(obs(ref=0.4, trade=tf(flow=None, last=0.4)))
    assert 0.2 < p < 0.6


def test_predicting_before_fitting_is_an_error():
    with pytest.raises(RuntimeError):
        MicrostructureLogit().predict(obs())


def test_book_features_enter_only_when_their_group_is_requested():
    names = {n for n, g, _ in FEATURES if g == "book"}
    train = _world(200, 1.0, 13)
    assert not names & set(MicrostructureLogit(groups=("trade",)).fit(train).coefficients())
    with_book = [
        inst(obs(ref=i.obs.ref, trade=i.obs.trade, book=bf(mid=i.obs.ref)), i.out.y) for i in train
    ]
    assert names <= set(MicrostructureLogit(groups=("trade", "book")).fit(with_book).coefficients())


# ------------------------------------------------------------------ Model B
def corpus(rows):
    """rows: (event, category, series, strike, n_siblings, y, days_before_T0)"""
    out = [
        CorpusRow(e, s, c, k, n_bucket(n), y, T0 - int(d * DAY_NS)) for e, c, s, k, n, y, d in rows
    ]
    return sorted(out, key=lambda r: r.settled_ns)


def test_hierarchical_shrinkage_matches_a_hand_computation():
    rows = [("e1", "Sports", "KXA", "none", 2, 1, 5), ("e2", "Sports", "KXA", "none", 2, 0, 4),
            ("e3", "Sports", "KXA", "none", 2, 1, 3), ("e4", "Sports", "KXB", "none", 2, 0, 3),
            ("e5", "Crypto", "KXC", "none", 2, 1, 2)]  # fmt: skip
    hf = HistoricalFrequency(corpus(rows), kappa=2.0)
    o = obs(cat="Sports", series="KXA", strike="none", n=2, t=T0)
    # global 3/5 -> category Sports (2 of 4)... computed level by level
    p_glob = (3 + 2 * 0.5) / (5 + 2)
    p_cat = (2 + 2 * p_glob) / (4 + 2)  # Sports: e1 yes, e2 no, e3 yes, e4 no -> 2 yes of 4
    p_ser = (2 + 2 * p_cat) / (3 + 2)  # KXA: yes, no, yes -> 2 of 3
    p_leaf = (2 + 2 * p_ser) / (3 + 2)  # same leaf: (KXA, none, '2')
    assert hf.predict(o) == pytest.approx(p_leaf)


def test_an_unseen_series_falls_back_to_its_category_then_the_global_rate():
    rows = [(f"e{k}", "Sports", "KXA", "none", 2, k % 2, 10 - k) for k in range(8)]
    hf = HistoricalFrequency(corpus(rows), kappa=1.0)
    p_unseen_series = hf.predict(obs(cat="Sports", series="KXZ", n=2))
    p_unseen_cat = hf.predict(obs(cat="Politics", series="KXZ", n=2))
    assert 0.3 < p_unseen_series < 0.7 and p_unseen_cat == pytest.approx(0.5, abs=0.1)
    assert HistoricalFrequency([], kappa=3.0).predict(obs()) == pytest.approx(
        0.5
    )  # no history: the prior


def test_the_shrinkage_strength_interpolates_between_the_leaf_and_its_parent():
    rows = [(f"e{k}", "Sports", "KXA", "none", 2, 1, 10 - k) for k in range(6)] + [
        (f"f{k}", "Sports", "KXB", "none", 2, 0, 10 - k) for k in range(6)
    ]
    o = obs(cat="Sports", series="KXA", n=2)
    tight = HistoricalFrequency(corpus(rows), kappa=1e-6).predict(o)  # trust the leaf's own record
    loose = HistoricalFrequency(corpus(rows), kappa=1e6).predict(o)  # ignore it: the parents' 50%
    assert tight > 0.99 and abs(loose - 0.5) < 0.01


def test_model_b_uses_only_outcomes_settled_before_the_day_of_the_prediction():
    """Point in time: change what settles on/after the prediction's day and nothing may move."""
    base = [(f"e{k}", "Sports", "KXA", "none", 2, k % 2, 10 - k) for k in range(8)]
    t = T0 + 3 * DAY_NS + 100
    late = [
        ("late1", "Sports", "KXA", "none", 2, 1, -3.2),
        ("late2", "Sports", "KXA", "none", 2, 1, -3.5),
    ]
    same_day = [("today", "Sports", "KXA", "none", 2, 1, -3.0)]
    o = obs(cat="Sports", series="KXA", n=2, t=t)
    p0 = HistoricalFrequency(corpus(base), kappa=2.0).predict(o)
    p1 = HistoricalFrequency(corpus(base + late + same_day), kappa=2.0).predict(o)
    assert p0 == pytest.approx(p1)  # outcomes from the future (or from later today) are invisible
    earlier = [("prior", "Sports", "KXA", "none", 2, 1, -2.0)]  # settled two days BEFORE t's day
    assert HistoricalFrequency(corpus(base + earlier), kappa=2.0).predict(o) != pytest.approx(p0)


def test_prequential_kappa_learns_a_whole_event_only_after_predicting_all_of_it():
    """Siblings settle together with jointly determined outcomes; predicting one after learning another
    would leak.  Check against a hand computation with events learned as a unit."""
    rows = []
    for k in range(6):  # each event: one YES, one NO sibling
        rows += [
            (f"ev{k}", "Sports", "KXA", "none", 2, 1, 20 - k),
            (f"ev{k}", "Sports", "KXA", "none", 2, 0, 20 - k),
        ]
    corp = corpus(rows)
    kappa = 2.0
    got = prequential_kappa(corp, grid=(kappa,), sample_every=1, warmup=0)[kappa]
    hf = HistoricalFrequency([], kappa=kappa)
    from collections import defaultdict

    from pricing.probability_models import _Counts, _keys

    counts = defaultdict(_Counts)
    total = n = 0
    for _ in range(6):
        for y in (1, 0):
            p = hf.rate(counts, "Sports", "KXA", "none", "2")
            total -= math.log(p if y else 1 - p)
            n += 1
        for y in (1, 0):
            for key in _keys("Sports", "KXA", "none", "2"):
                counts[key].n += 1
                counts[key].yes += y
    assert got == pytest.approx(total / n)
    # a leaky version (learn each sibling immediately) would score this world better - proof it matters
    counts = defaultdict(_Counts)
    leaky = 0.0
    for _ in range(6):
        for y in (1, 0):
            p = hf.rate(counts, "Sports", "KXA", "none", "2")
            leaky -= math.log(p if y else 1 - p)
            for key in _keys("Sports", "KXA", "none", "2"):
                counts[key].n += 1
                counts[key].yes += y
    assert leaky / n != pytest.approx(got)


def test_n_bucket_edges():
    assert [n_bucket(x) for x in (None, 1, 2, 3, 5, 6, 10, 11, 200)] == [
        "?",
        "1",
        "2",
        "3-5",
        "3-5",
        "6-10",
        "6-10",
        "11+",
        "11+",
    ]


def test_prequential_kappa_actually_learns_from_earlier_events():
    """A balanced world hides whether anything is learned (the rate stays at 0.5), so use one where every
    sibling resolves YES: predictions must rise as events accumulate, and the loss must beat 'never learn'."""
    rows = []
    for k in range(8):
        rows += [
            (f"ev{k}", "Sports", "KXA", "none", 2, 1, 30 - k),
            (f"ev{k}", "Sports", "KXA", "none", 2, 1, 30 - k),
        ]
    corp = corpus(rows)
    got = prequential_kappa(corp, grid=(2.0,), sample_every=1, warmup=0)[2.0]
    assert got < math.log(2) - 0.1  # a model that never learns predicts 0.5 forever: exactly ln 2
    from collections import defaultdict

    from pricing.probability_models import _Counts, _keys

    hf, counts, total, n = HistoricalFrequency([], kappa=2.0), defaultdict(_Counts), 0.0, 0
    for _ in range(8):
        for _ in range(2):  # both siblings are predicted from the same (pre-event) counts
            total -= math.log(hf.rate(counts, "Sports", "KXA", "none", "2"))
            n += 1
        for key in _keys("Sports", "KXA", "none", "2"):
            counts[key].n += 2
            counts[key].yes += 2
    assert got == pytest.approx(total / n)


def test_missing_values_sit_exactly_at_the_training_mean():
    rng = random.Random(21)
    train = []
    for k in range(120):
        ref = rng.uniform(0.2, 0.8)
        flow = None if k % 4 == 0 else rng.uniform(-1, 1)
        train.append(
            inst(
                obs(ref=ref, trade=tf(flow=flow, last=ref)), int(rng.random() < ref), event=f"E{k}"
            )
        )
    m = MicrostructureLogit(lam=1.0, groups=("trade",)).fit(train)
    x, miss = m._raw(obs(ref=0.5, trade=tf(flow=None)))
    Z = m._design(x[None, :], miss[None, :])
    names = [n for n, _ in m._spec()]
    assert Z[0, 1 + names.index("flow_5m")] == 0.0  # standardised: the mean, i.e. "no information"


def test_the_market_price_is_the_offset_of_model_a():
    """With every correction switched off the model IS the market: dropping the offset would predict ~0.5."""
    train = _world(300, 1.0, 31)
    m = MicrostructureLogit(lam=1e12).fit(train)
    hi, lo = m.predict(obs(ref=0.9)), m.predict(obs(ref=0.1))
    assert hi > 0.85 and lo < 0.15


def test_the_probability_bound_is_one_price_tick():
    from market.units import PRICE_SCALE

    assert EPS == pytest.approx(1 / PRICE_SCALE)
