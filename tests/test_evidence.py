from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import beta

from data.normalization.normalizer import normalize_market
from kalshi_client.models import ApiMarket, validate_rest
from market.evidence import SeriesStats, StatsBook, clopper_pearson_upper
from tests.fakes import make_market

CLOSE = datetime(2026, 9, 5, tzinfo=UTC)


def event_markets(fx, results):
    return [
        normalize_market(
            validate_rest(ApiMarket, make_market(fx, f"S-E-{i}", close=CLOSE, result=r))
        )
        for i, r in enumerate(results)
    ]


@pytest.mark.parametrize("k,n", [(0, 1), (0, 30), (0, 59), (1, 100), (3, 7), (5, 50), (20, 20)])
def test_clopper_pearson_upper_matches_the_beta_quantile_definition(k, n):
    want = 1.0 if k >= n else beta.ppf(0.95, k + 1, n - k)
    assert clopper_pearson_upper(k, n) == pytest.approx(want, abs=1e-6)


def test_bound_facts():
    assert clopper_pearson_upper(0, 30) == pytest.approx(
        1 - 0.05 ** (1 / 30), abs=1e-6
    )  # ~ rule of three
    assert clopper_pearson_upper(0, 0) == 1.0  # no data supports nothing
    assert (
        clopper_pearson_upper(0, 100) < clopper_pearson_upper(0, 30) < clopper_pearson_upper(1, 30)
    )


@given(st.integers(0, 60), st.integers(1, 200))
def test_bound_is_conservative_and_monotone(k, n):
    k = min(k, n)
    ub = clopper_pearson_upper(k, n)
    assert k / n <= ub <= 1.0
    if k < n:
        assert clopper_pearson_upper(k + 1, n) >= ub


def test_event_classification(fx):
    s = SeriesStats("S")
    assert s.add(event_markets(fx, ["yes", "no", "no"]))  # exactly one
    assert s.add(event_markets(fx, ["yes", "yes", "no"]))  # not exclusive
    assert s.add(event_markets(fx, ["no", "no", "no"]))  # not exhaustive
    assert (s.n_clean, s.one_yes, s.multi_yes, s.zero_yes, s.n_scalar) == (3, 1, 1, 1, 0)


def test_scalar_events_are_resolution_risk_not_clean_outcomes(fx):
    scalar = event_markets(fx, ["yes", "no"])
    from dataclasses import replace

    from market.contracts import SettlementResult

    voided = [replace(m, result=SettlementResult.SCALAR, settlement_value=5000) for m in scalar]
    s = SeriesStats("S")
    assert s.add(voided)
    assert (s.n_events, s.n_scalar, s.n_clean, s.zero_yes) == (
        1,
        1,
        0,
        0,
    )  # not miscounted as "no YES"
    assert s.scalar_rate == 1.0


def test_unsettled_events_are_ignored(fx):
    from dataclasses import replace

    open_ = [replace(m, settlement_value=None) for m in event_markets(fx, ["yes", "no"])]
    s = SeriesStats("S")
    assert not s.add(open_) and s.n_events == 0 and not s.add([])


@given(
    st.lists(
        st.lists(st.sampled_from(["yes", "no"]), min_size=1, max_size=4), min_size=1, max_size=8
    )
)
def test_remove_is_the_exact_inverse_of_add(events):
    import json as _json  # noqa: F401

    from tests.conftest import load_fixture

    fx = load_fixture
    built = [event_markets(fx, r) for r in events]
    full, loo = SeriesStats("S"), SeriesStats("S")
    for b in built:
        full.add(b)
        loo.add(b)
    loo.remove(built[0])
    rest = SeriesStats("S")
    for b in built[1:]:
        rest.add(b)
    assert loo == rest


def test_statsbook_leave_one_out_view_does_not_mutate_the_original(fx):
    book = StatsBook()
    evs = [event_markets(fx, ["yes", "no"]) for _ in range(3)]
    for e in evs:
        book.add_event("S", e)
    view = book.without("S", evs[0])
    assert view.get("S").n_clean == 2 and book.get("S").n_clean == 3
    assert book.without("OTHER", evs[0]).get("S").n_clean == 3
