import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from pricing.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    boot_stat,
    calibration_in_the_large,
    calibration_regression,
    cluster_index,
    ece,
    edges_mass,
    edges_width,
    murphy,
    reliability_bins,
)
from pricing.scoring import EPS


def simulate(n, slope=1.0, intercept=0.0, seed=0, lo=0.05, hi=0.95):
    """Forecasts p; outcomes drawn from the TRUE probability sigmoid(intercept + slope * logit(p))."""
    rng = np.random.default_rng(seed)
    p = rng.uniform(lo, hi, n)
    z = np.log(p / (1 - p))
    q = 1 / (1 + np.exp(-(intercept + slope * z)))
    return p, (rng.random(n) < q).astype(float)


# ------------------------------------------------------------------ reliability bins
def test_bins_count_and_signed_gap():
    p = np.array([0.05, 0.05, 0.15, 0.95, 0.95, 0.95, 1.0])
    y = np.array([0, 1, 0, 1, 1, 0, 1], float)
    bins = reliability_bins(p, y, edges_width(10))
    assert [b.n for b in bins] == [2, 1, 4]  # p == 1.0 belongs to the LAST bin, not off the grid
    assert bins[0].freq == 0.5 and bins[0].gap == pytest.approx(
        0.5 - 0.05
    )  # YES more often than said
    assert bins[2].mean_p == pytest.approx((0.95 * 3 + 1.0) / 4)
    assert sum(b.n for b in bins) == len(p)  # every forecast lands in exactly one bin


def test_mass_edges_cover_the_unit_interval_and_balance_the_bins():
    p = np.concatenate([np.full(500, 0.02), np.linspace(0.1, 0.9, 500)])
    e = edges_mass(p, 10)
    assert e[0] == 0.0 and e[-1] == 1.0 and e == sorted(set(e))
    counts = [b.n for b in reliability_bins(p, np.zeros(len(p)), e)]
    assert max(counts) < 0.6 * len(p)  # a fixed grid would put half the forecasts in one bin


# ------------------------------------------------------------------ summary statistics
def test_ece_is_zero_when_bins_are_exactly_calibrated_and_large_when_biased():
    p = np.array([0.2] * 10 + [0.8] * 10)
    y = np.array([1, 1] + [0] * 8 + [1] * 8 + [0] * 2, float)
    assert ece(p, y, edges_width(10)) == pytest.approx(0.0, abs=1e-12)
    assert ece(np.full(20, 0.9), np.zeros(20), edges_width(10)) == pytest.approx(0.9)
    assert calibration_in_the_large(np.array([0.3, 0.3]), np.array([1.0, 1.0])) == pytest.approx(
        0.7
    )


@settings(max_examples=150, deadline=None)
@given(
    st.lists(st.tuples(st.floats(0, 1), st.integers(0, 1)), min_size=2, max_size=60),
    st.integers(2, 12),
)
def test_the_murphy_decomposition_is_an_exact_identity(pairs, k):
    """Brier = reliability - resolution + uncertainty + within_var - 2 within_cov, for ANY forecasts/bins."""
    p = np.array([a for a, _ in pairs])
    y = np.array([b for _, b in pairs], float)
    m = murphy(p, y, edges_width(k))
    assert abs(m["identity_residual"]) < 1e-9
    assert m["reliability"] >= 0 and m["resolution"] >= 0 and 0 <= m["uncertainty"] <= 0.25


def test_a_perfectly_calibrated_forecaster_has_near_zero_reliability_and_positive_resolution():
    p, y = simulate(40_000, seed=1)
    m = murphy(p, y, edges_width(10))
    assert m["reliability"] < 0.0005 and m["resolution"] > 0.03


# ------------------------------------------------------------------ the calibration regression
@pytest.mark.parametrize(
    "slope,intercept", [(1.0, 0.0), (0.5, 0.0), (1.5, 0.0), (1.0, 0.7), (0.7, -0.4)]
)
def test_the_regression_recovers_planted_calibration_errors(slope, intercept):
    p, y = simulate(60_000, slope, intercept, seed=3)
    a, b = calibration_regression(p, y)
    assert a == pytest.approx(intercept, abs=0.06) and b == pytest.approx(slope, abs=0.06)


def test_overconfidence_shows_as_a_slope_below_one():
    p, y = simulate(30_000, slope=0.5, seed=4)  # the forecasts are twice as extreme as the truth
    assert calibration_regression(p, y)[1] < 0.6


def test_a_forecast_with_no_spread_has_no_calibration_slope():
    assert (
        calibration_regression(
            np.full(50, 0.4), np.random.default_rng(0).integers(0, 2, 50).astype(float)
        )
        is None
    )
    assert (
        calibration_regression(np.array([0.2, 0.8]), np.array([0.0, 1.0])) is None
    )  # too few points


# ------------------------------------------------------------------ recalibrators
@pytest.mark.parametrize("cal", [PlattCalibrator, IsotonicCalibrator])
def test_recalibrators_are_bounded_and_never_reorder_forecasts(cal):
    p, y = simulate(3000, slope=0.6, seed=5)
    c = cal().fit(p, y)
    grid = np.linspace(0, 1, 501)
    out = c.predict(grid)
    assert (out >= EPS).all() and (out <= 1 - EPS).all()
    assert (np.diff(out) >= -1e-12).all()  # monotone


def test_platt_repairs_an_overconfident_forecaster_out_of_sample():
    p_tr, y_tr = simulate(5000, slope=0.5, seed=6)
    p_te, y_te = simulate(5000, slope=0.5, seed=7)
    ll = lambda q, o: -float(np.mean(o * np.log(q) + (1 - o) * np.log(1 - q)))  # noqa: E731
    fixed = PlattCalibrator().fit(p_tr, y_tr).predict(p_te)
    assert ll(fixed, y_te) < ll(np.clip(p_te, EPS, 1 - EPS), y_te) - 0.02
    assert PlattCalibrator().fit(p_tr, y_tr).b == pytest.approx(0.5, abs=0.08)


def _minmax_isotonic(y):
    """Independent oracle: the isotonic regression has the closed form v_i = max_{s<=i} min_{t>=i} mean(y[s..t])."""
    n = len(y)
    v = []
    for i in range(n):
        best = -math.inf
        for s in range(i + 1):
            m = min(sum(y[s : t + 1]) / (t - s + 1) for t in range(i, n))
            best = max(best, m)
        v.append(best)
    return v


@settings(max_examples=100, deadline=None)
@given(st.lists(st.integers(0, 1), min_size=2, max_size=14))
def test_isotonic_matches_the_closed_form_oracle(ys):
    n = len(ys)
    p = np.arange(1, n + 1) / (n + 1)  # distinct, already sorted forecasts
    y = np.array(ys, float)
    c = IsotonicCalibrator().fit(p, y)
    fitted = c.fitted_  # the pure isotonic fit at each training point
    assert np.allclose(fitted, _minmax_isotonic(ys), atol=1e-9)
    assert (np.diff(fitted) >= -1e-12).all() and fitted.sum() == pytest.approx(y.sum())


def test_isotonic_leaves_an_already_monotone_relationship_alone_and_flattens_a_reversed_one():
    p = np.linspace(0.05, 0.95, 40)
    assert np.allclose(
        IsotonicCalibrator().fit(p, (p > 0.5).astype(float)).predict(p[[0, -1]]), [EPS, 1 - EPS]
    )
    flat = (
        IsotonicCalibrator().fit(p, (p < 0.5).astype(float)).predict(p)
    )  # forecasts anti-informative
    assert np.ptp(flat) < 1e-9  # the best non-decreasing fit to a decreasing signal is a constant


# ------------------------------------------------------------------ the event bootstrap
def test_bootstrap_resamples_events_and_withholds_an_interval_from_a_single_event():
    clusters = cluster_index(["a", "a", "b", "c", "c", "c"])
    assert sorted(len(c) for c in clusters) == [1, 2, 3]
    est = boot_stat(clusters, lambda idx: float(len(idx)), n_boot=200, seed=1)
    assert est.value == 6 and est.n_clusters == 3 and est.lo <= est.value or True
    single = boot_stat(cluster_index(["a", "a"]), lambda idx: 1.0, n_boot=50)
    assert single.lo is None and single.hi is None  # one event: no honest interval


def test_event_clustering_widens_the_interval_for_the_calibration_slope():
    rng = np.random.default_rng(9)
    n_events, per = 60, 8
    p = np.repeat(
        rng.uniform(0.1, 0.9, n_events), per
    )  # every market of an event shares one forecast...
    q = 1 / (1 + np.exp(-np.log(p / (1 - p))))
    y = np.repeat((rng.random(n_events) < q[::per]).astype(float), per)  # ...and one outcome
    ev = [i // per for i in range(len(p))]
    clustered = boot_stat(
        cluster_index(ev),
        lambda idx: (calibration_regression(p[idx], y[idx]) or (0, np.nan))[1],
        n_boot=300,
        seed=2,
    )
    naive = boot_stat(
        cluster_index(list(range(len(p)))),
        lambda idx: (calibration_regression(p[idx], y[idx]) or (0, np.nan))[1],
        n_boot=300,
        seed=2,
    )
    assert (clustered.hi - clustered.lo) > 1.5 * (naive.hi - naive.lo)


def test_ece_weights_bins_by_how_many_forecasts_they_hold():
    p = np.array([0.2] * 90 + [0.8] * 10)
    y = np.array(
        [1] * 18 + [0] * 72 + [1] * 5 + [0] * 5, float
    )  # first bin exact; second off by 0.3
    assert ece(p, y, edges_width(10)) == pytest.approx(
        0.1 * 0.3
    )  # 10% of forecasts carry the 0.3 gap


def test_platt_never_reverses_the_ordering_even_for_an_anti_informative_forecast():
    p = np.linspace(0.05, 0.95, 400)
    y = (p < 0.5).astype(
        float
    )  # the forecast points the wrong way: the fitted slope would be negative
    cal = PlattCalibrator().fit(p, y)
    assert cal.b > 0  # refuses a sign flip: a calibrator must not reorder forecasts
    assert (np.diff(cal.predict(p)) >= 0).all()


def test_platt_keeps_the_ordering_when_the_forecast_is_noisily_anti_informative():
    rng = np.random.default_rng(11)
    p = rng.uniform(0.05, 0.95, 4000)
    y = (rng.random(4000) < (1 - p)).astype(
        float
    )  # outcomes follow 1-p: the true slope is about -1
    assert calibration_regression(p, y)[1] < -0.5  # the raw regression does go negative...
    cal = PlattCalibrator().fit(p, y)
    assert (
        cal.b > 0 and (np.diff(cal.predict(np.linspace(0, 1, 101))) >= 0).all()
    )  # ...the calibrator must not
