import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from research.stats import (
    Lifetime,
    _z,
    cluster_bootstrap,
    cluster_bootstrap_diff,
    cluster_bootstrap_ratio,
    concentration,
    exp_interval_mle,
    exp_survival,
    kaplan_meier,
    km_at,
    km_quantile,
    survival_bounds,
    wilson_ci,
)


# ------------------------------------------------------------------ proportions
def test_normal_quantiles_match_reference_values():
    assert _z(0.05) == pytest.approx(1.959964, abs=1e-5)
    assert _z(0.01) == pytest.approx(2.575829, abs=1e-5)
    assert _z(0.10) == pytest.approx(1.644854, abs=1e-5)


def test_wilson_reference_values_and_boundaries():
    lo, hi = wilson_ci(5, 10)
    assert (lo, hi) == pytest.approx((0.2366, 0.7634), abs=1e-3)
    lo, hi = wilson_ci(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)  # nonzero upper bound at k=0
    lo, hi = wilson_ci(10, 10)
    assert hi == 1.0 and lo == pytest.approx(0.7225, abs=1e-3)
    assert wilson_ci(0, 0) == (0.0, 1.0)  # no data: total ignorance, not a crash
    with pytest.raises(ValueError):
        wilson_ci(11, 10)


@pytest.mark.parametrize("p", [0.05, 0.3, 0.5])
def test_wilson_coverage_is_near_nominal(p):
    rng = random.Random(7)
    n, hits = 40, 0
    trials = 1500
    for _ in range(trials):
        k = sum(rng.random() < p for _ in range(n))
        lo, hi = wilson_ci(k, n)
        hits += lo <= p <= hi
    assert 0.93 <= hits / trials <= 0.985


# ------------------------------------------------------------------ clustered bootstrap
def _mean(rs):
    return sum(rs) / len(rs) if rs else None


def test_cluster_bootstrap_is_wider_than_a_row_bootstrap_when_rows_are_duplicated():
    # 10 clusters, every row inside a cluster identical -> effective n is 10 not 500.
    rng = random.Random(1)
    cluster_vals = [rng.gauss(0, 1) for _ in range(10)]
    rows = [(c, cluster_vals[c]) for c in range(10) for _ in range(50)]
    clustered = cluster_bootstrap(
        rows, lambda r: r[0], lambda rs: _mean([r[1] for r in rs]), seed=3
    )
    naive = cluster_bootstrap(
        [(i, r[1]) for i, r in enumerate(rows)],  # every row its own cluster
        lambda r: r[0],
        lambda rs: _mean([r[1] for r in rs]),
        seed=3,
    )
    assert clustered.n_clusters == 10 and naive.n_clusters == 500
    assert (clustered.hi - clustered.lo) > 3 * (naive.hi - naive.lo)


def test_a_single_cluster_gives_no_interval_rather_than_a_fake_one():
    est = cluster_bootstrap(
        [("a", 1.0), ("a", 2.0)], lambda r: r[0], lambda rs: _mean([r[1] for r in rs])
    )
    assert est.value == 1.5 and est.lo is None and est.hi is None and est.n_clusters == 1


def test_cluster_bootstrap_coverage_with_cluster_effects():
    # true mean 0.3; between-cluster sd 1, within sd 1; 30 clusters x 5 rows
    trials, hits = 250, 0
    for s in range(trials):
        rng = random.Random(1000 + s)
        rows = []
        for c in range(30):
            eff = rng.gauss(0.3, 1.0)
            rows += [(c, eff + rng.gauss(0, 1)) for _ in range(5)]
        est = cluster_bootstrap(
            rows, lambda r: r[0], lambda rs: _mean([r[1] for r in rs]), n_boot=300, seed=s
        )
        hits += est.lo <= 0.3 <= est.hi
    assert 0.88 <= hits / trials <= 0.99


def test_ratio_estimator_is_a_ratio_of_sums_not_a_mean_of_ratios():
    rows = [("a", 1, 10), ("a", 1, 10), ("b", 9, 10)]  # 11 / 30
    est = cluster_bootstrap_ratio(rows, lambda r: r[0], lambda r: r[1], lambda r: r[2], n_boot=50)
    assert est.value == pytest.approx(11 / 30)


def test_ratio_with_an_empty_denominator_resample_is_withheld_not_biased():
    # cluster "z" carries all exposure; resamples without it have den=0 -> those draws are dropped,
    # and with too many dropped the interval is withheld
    rows = [("a", 0, 0), ("b", 0, 0), ("c", 0, 0), ("d", 0, 0), ("z", 5, 100)]
    est = cluster_bootstrap_ratio(
        rows, lambda r: r[0], lambda r: r[1], lambda r: r[2], n_boot=400, seed=1
    )
    assert est.value == pytest.approx(0.05)
    assert est.lo is None or est.lo >= 0  # never a nonsense value


def test_diff_bootstrap_detects_a_real_difference_and_not_a_null_one():
    rng = random.Random(5)

    def make(mu):
        return [(c, mu + rng.gauss(0, 0.2)) for c in range(25) for _ in range(3)]

    a, b, c = make(1.0), make(0.0), make(0.0)
    f = lambda rs: _mean([r[1] for r in rs])  # noqa: E731
    real = cluster_bootstrap_diff(a, b, lambda r: r[0], f, seed=1)
    null = cluster_bootstrap_diff(b, c, lambda r: r[0], f, seed=1)
    assert real.lo > 0.5
    assert null.lo < 0 < null.hi


def test_concentration_of_one_dominant_cluster():
    c = concentration({"KXTRUMPAPPROVE": 325, "b": 1, "c": 1})
    assert c["top1_share"] == pytest.approx(325 / 327)
    assert 1.0 < c["effective_clusters"] < 1.02  # ~1 independent event, not 327 observations
    assert concentration({})["effective_clusters"] is None
    even = concentration({i: 10 for i in range(8)})
    assert even["effective_clusters"] == pytest.approx(8)


# ------------------------------------------------------------------ survival
def test_kaplan_meier_textbook_example():
    curve = kaplan_meier([1, 2, 3, 4, 5], [True, False, True, False, True])
    assert curve[0] == (1, pytest.approx(0.8))
    assert curve[1] == (3, pytest.approx(0.8 * (1 - 1 / 3)))
    assert curve[2] == (5, pytest.approx(0.0))
    assert km_at(curve, 0.5) == 1.0 and km_at(curve, 3.5) == pytest.approx(0.8 * 2 / 3)
    assert km_quantile(curve, 0.5) == 5  # S(3)=0.53 is still above one half


def test_kaplan_meier_counts_a_death_at_the_same_time_as_a_censoring_as_at_risk():
    # standard convention: the censored subject at t=2 is still at risk when the death at t=2 happens
    curve = kaplan_meier([2, 2, 5], [True, False, True])
    assert curve[0] == (2, pytest.approx(1 - 1 / 3))


def test_kaplan_meier_median_is_none_when_censoring_prevents_it():
    curve = kaplan_meier([1, 10, 10, 10], [True, False, False, False])
    assert km_quantile(curve, 0.5) is None


def test_kaplan_meier_length_mismatch_is_an_error():
    with pytest.raises(ValueError):
        kaplan_meier([1, 2], [True])


def test_lifetime_rejects_impossible_bounds():
    with pytest.raises(ValueError):
        Lifetime(3.0, 2.0)
    with pytest.raises(ValueError):
        Lifetime(-1.0, 2.0)
    assert Lifetime(1.0, math.inf).censored


@settings(max_examples=200, deadline=None)
@given(
    st.lists(
        st.tuples(st.floats(0, 100), st.one_of(st.floats(0, 100), st.just(math.inf))),
        min_size=1,
        max_size=30,
    ),
    st.floats(0, 120),
    st.floats(0, 120),
)
def test_survival_bounds_are_ordered_and_monotone(pairs, t1, t2):
    lives = [Lifetime(min(a, b), max(a, b)) for a, b in pairs]
    lo1, up1 = survival_bounds(lives, min(t1, t2))
    lo2, up2 = survival_bounds(lives, max(t1, t2))
    assert 0 <= lo1 <= up1 <= 1 and 0 <= lo2 <= up2 <= 1
    assert lo2 <= lo1 and up2 <= up1  # survival never increases with t


def test_survival_bounds_contain_the_truth_in_simulation():
    """Exponential lifetimes observed on a 3-second cadence: the interval-censored bounds must
    bracket the true S(t) at every t (up to sampling noise), and be WIDE below the cadence."""
    rng = random.Random(11)
    lam, cadence, n = 0.1, 3.0, 4000
    lives = []
    for _ in range(n):
        first_seen_offset = rng.uniform(0, cadence)  # start is uniform inside a poll interval
        life = rng.expovariate(lam)  # true time from first sighting until it dies
        # observed on a fixed grid from the sighting: alive at k*cadence while k*cadence < life
        k_alive = math.floor(life / cadence)
        lives.append(Lifetime(k_alive * cadence, (k_alive + 1) * cadence))
        del first_seen_offset
    for t in [0.5, 1, 2.9, 3, 6, 9, 15, 30]:
        lo, up = survival_bounds(lives, t)
        truth = exp_survival(lam, t)
        assert lo - 0.03 <= truth <= up + 0.03, (t, lo, truth, up)
    lo, up = survival_bounds(lives, 1.0)  # a latency below the cadence is NOT identified
    assert up - lo > 0.2


def test_exp_mle_matches_closed_form_when_intervals_are_tiny():
    ts = [1.0, 2.5, 4.0, 0.7, 3.3]
    lives = [Lifetime(t, t + 1e-7) for t in ts]
    assert exp_interval_mle(lives) == pytest.approx(len(ts) / sum(ts), rel=1e-4)


def test_exp_mle_recovers_the_rate_from_interval_censored_data():
    rng = random.Random(3)
    lam, cadence = 0.15, 3.0
    lives = []
    for _ in range(3000):
        life = rng.expovariate(lam)
        k = math.floor(life / cadence)
        lives.append(Lifetime(k * cadence, (k + 1) * cadence))
    assert exp_interval_mle(lives) == pytest.approx(lam, rel=0.06)


def test_exp_mle_handles_right_censoring_and_refuses_degenerate_cases():
    rng = random.Random(4)
    lam, horizon = 0.05, 20.0
    lives = []
    for _ in range(3000):
        life = rng.expovariate(lam)
        lives.append(Lifetime(horizon, math.inf) if life > horizon else Lifetime(life, life + 1e-7))
    assert exp_interval_mle(lives) == pytest.approx(lam, rel=0.08)
    assert exp_interval_mle([Lifetime(5.0, math.inf)] * 4) is None  # nothing ever died: no rate
    assert exp_interval_mle([]) is None
    # every lifetime inside [0, 1): the likelihood grows without bound -> no finite MLE, not a made-up one
    assert exp_interval_mle([Lifetime(0.0, 1.0)] * 5) is None


# ------------------------------------------------------------------ association
def test_spearman_known_values_and_ties():
    from research.stats import spearman

    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4, 5], [2, 1, 4, 3, 5]) == pytest.approx(0.8)
    # heavy ties (edges are integer ticks): average ranks, still well defined
    assert spearman([1, 1, 1, 2, 2, 2], [1, 1, 2, 2, 3, 3]) == pytest.approx(
        12 / math.sqrt(13.5 * 16)
    )  # by hand: 0.8165
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # a constant variable has no correlation
    assert spearman([1, 2], [1, 2]) is None  # too few points to say anything
    with pytest.raises(ValueError):
        spearman([1, 2, 3], [1, 2])


@settings(max_examples=100, deadline=None)
@given(st.lists(st.integers(-1000, 1000), min_size=5, max_size=30, unique=True))
def test_spearman_is_invariant_to_monotone_transforms(xs):
    from research.stats import spearman

    ys = [x * x * x + x for x in xs]  # strictly increasing
    assert spearman(xs, ys) == pytest.approx(1.0)
    assert spearman(xs, [math.exp(x / 50) for x in xs]) == pytest.approx(1.0)


def test_the_fast_ratio_and_mean_bootstraps_match_the_general_one_exactly():
    """Same seed, same clusters drawn in the same order: identical intervals, faster."""
    from research.stats import cluster_bootstrap_mean

    rng = random.Random(9)
    rows = [(f"c{rng.randrange(12)}", rng.random(), rng.random() + 0.5) for _ in range(300)]
    kw = {"n_boot": 400, "seed": 5}
    slow = cluster_bootstrap(
        rows, lambda r: r[0], lambda rs: sum(r[1] for r in rs) / sum(r[2] for r in rs), **kw
    )
    fast = cluster_bootstrap_ratio(rows, lambda r: r[0], lambda r: r[1], lambda r: r[2], **kw)
    assert (fast.value, fast.lo, fast.hi) == pytest.approx((slow.value, slow.lo, slow.hi))
    slow_m = cluster_bootstrap(rows, lambda r: r[0], lambda rs: _mean([r[1] for r in rs]), **kw)
    fast_m = cluster_bootstrap_mean(rows, lambda r: r[0], lambda r: r[1], **kw)
    assert (fast_m.value, fast_m.lo, fast_m.hi) == pytest.approx(
        (slow_m.value, slow_m.lo, slow_m.hi)
    )
    assert cluster_bootstrap_mean([], lambda r: r[0], lambda r: r[1]).value is None
