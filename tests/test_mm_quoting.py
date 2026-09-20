import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from market_making.quoting import (
    certainty_equivalent,
    liquidity_half_spread,
    quote,
    raw_quotes,
    reservation_ask,
    reservation_bid,
)

P = st.floats(0.02, 0.98)
G = st.floats(0.005, 2.0)
Q = st.floats(-60, 60)


# ------------------------------------------------------------------ the certainty equivalent
def test_risk_neutral_limit_and_no_position():
    assert certainty_equivalent(7, 0.3, 0.0) == pytest.approx(2.1)  # gamma -> 0: expected value
    assert certainty_equivalent(0, 0.3, 0.5) == pytest.approx(
        0.0
    )  # holding nothing is worth nothing
    with pytest.raises(ValueError):
        certainty_equivalent(1, 0.0, 0.1)  # p must be strictly inside (0, 1)
    assert math.isfinite(
        certainty_equivalent(500, 0.999, 3.0)
    )  # numerically stable at large |gamma q|


@settings(max_examples=200, deadline=None)
@given(Q, P, G)
def test_a_risk_averse_dealer_values_a_gamble_below_its_expectation(q, p, g):
    """Jensen: CE(q) <= E[q X] = q p, for long and short positions alike."""
    assert certainty_equivalent(q, p, g) <= q * p + 1e-9


@settings(max_examples=200, deadline=None)
@given(Q, P, G)
def test_certainty_equivalent_is_concave_in_inventory(q, p, g):
    ce = lambda x: certainty_equivalent(x, p, g)  # noqa: E731
    assert ce(q + 1) - ce(q) <= ce(q) - ce(q - 1) + 1e-9  # marginal value falls as inventory grows


# ------------------------------------------------------------------ reservation prices: the bounded-support properties
@settings(max_examples=300, deadline=None)
@given(Q, P, G)
def test_reservation_prices_are_always_valid_prices_and_ordered(q, p, g):
    """The point of the formulation: no clipping is ever needed, unlike a continuous-asset AS."""
    rb, ra = reservation_bid(q, p, g), reservation_ask(q, p, g)
    assert 0.0 <= rb <= 1.0 and 0.0 <= ra <= 1.0
    # ...and the UNCLAMPED marginal value is inside [0, 1] up to float dust: the clamp changes nothing real
    raw = certainty_equivalent(q + 1, p, g) - certainty_equivalent(q, p, g)
    assert -1e-9 <= raw <= 1 + 1e-9
    assert rb <= ra + 1e-12  # buying is never valued above selling


@settings(max_examples=200, deadline=None)
@given(Q, P, G)
def test_inventory_skew_the_more_you_own_the_less_you_will_pay(q, p, g):
    assert reservation_bid(q + 1, p, g) <= reservation_bid(q, p, g) + 1e-12
    assert reservation_ask(q + 1, p, g) <= reservation_ask(q, p, g) + 1e-12
    assert reservation_ask(q, p, g) == pytest.approx(reservation_bid(q - 1, p, g))


@settings(max_examples=200, deadline=None)
@given(Q, P, G)
def test_yes_no_duality(q, p, g):
    """Swapping the two outcomes (p -> 1-p, q -> -q) maps the ask to the bid: the model has no favourite side."""
    assert reservation_ask(q, p, g) == pytest.approx(1 - reservation_bid(-q, 1 - p, g), abs=1e-9)


@settings(max_examples=100, deadline=None)
@given(Q, st.floats(0.02, 0.9), st.floats(0.005, 0.08), G)
def test_reservation_price_rises_with_the_probability(q, p, dp, g):
    assert reservation_bid(q, p + dp, g) >= reservation_bid(q, p, g) - 1e-12


def test_small_gamma_matches_the_avellaneda_stoikov_form_with_the_binary_variance():
    """bid*(q) ~ p - gamma p (1-p) (q + 1/2): AS's  s - q gamma sigma^2 (T-t)  with sigma^2 (T-t) -> p (1-p)."""
    for p in (0.2, 0.5, 0.8):
        for q in (-5, 0, 5):
            g = 0.002
            approx = p - g * p * (1 - p) * (q + 0.5)
            assert reservation_bid(q, p, g) == pytest.approx(
                approx, abs=5 * g * g * (abs(q) + 1) ** 2
            )


def test_the_structural_spread_is_widest_at_one_half_and_vanishes_at_the_edges():
    """Bounded payoff: a claim near 0 or 1 carries almost no settlement risk, so it needs almost no risk premium."""
    width = lambda p: reservation_ask(0, p, 0.3) - reservation_bid(0, p, 0.3)  # noqa: E731
    assert width(0.5) > width(0.2) > width(0.05) > 0
    assert width(0.5) == pytest.approx(0.3 * 0.25, rel=0.15)  # ~ gamma p (1-p)
    assert width(0.5) == pytest.approx(width(0.5))  # symmetric around 1/2 below
    assert width(0.3) == pytest.approx(width(0.7), rel=1e-9)


# ------------------------------------------------------------------ the liquidity term
def test_liquidity_half_spread():
    assert liquidity_half_spread(0.0, 50) == pytest.approx(0.02)  # 1/k
    assert liquidity_half_spread(1e-6, 50) == pytest.approx(0.02, rel=1e-4)
    assert liquidity_half_spread(0.5, 50) == pytest.approx(math.log(1 + 0.5 / 50) / 0.5)
    assert liquidity_half_spread(0.5, 10) > liquidity_half_spread(0.5, 50)  # thinner market: wider
    with pytest.raises(ValueError):
        liquidity_half_spread(0.1, 0.0)


@given(G, st.floats(1, 200))
def test_the_liquidity_term_is_below_one_over_k(g, k):
    assert liquidity_half_spread(g, k) <= 1 / k + 1e-12  # ln(1+x) <= x


# ------------------------------------------------------------------ the final, rounded quotes
@settings(max_examples=300, deadline=None)
@given(Q, P, G, st.floats(5, 200), st.floats(0, 0.03))
def test_quotes_never_give_away_edge_and_never_cross_the_market(q, p, g, k, floor):
    """Bid rounded DOWN and ask UP (so rounding costs nothing), bid below the best ask, ask above the best
    bid, both inside [tick, 1 - tick]."""
    bb, ba = max(0.01, p - 0.02), min(0.99, p + 0.02)
    qt = quote(q, p, gamma=g, k=k, tick=0.01, min_half_spread=floor, best_bid=bb, best_ask=ba)
    raw_b, raw_a = raw_quotes(q, p, g, k, floor)
    if qt.bid is not None:
        assert (
            qt.bid <= raw_b + 1e-9
            and qt.bid <= ba - 0.01 + 1e-9
            and 0.01 - 1e-9 <= qt.bid <= 0.99 + 1e-9
        )
    if qt.ask is not None:
        assert (
            qt.ask >= raw_a - 1e-9
            and qt.ask >= bb + 0.01 - 1e-9
            and 0.01 - 1e-9 <= qt.ask <= 0.99 + 1e-9
        )
    if qt.bid is not None and qt.ask is not None:
        assert qt.bid < qt.ask


def test_quotes_are_mirror_images_under_swapping_yes_and_no():
    a = quote(3, 0.4, gamma=0.1, k=30, best_bid=0.39, best_ask=0.41)
    b = quote(-3, 0.6, gamma=0.1, k=30, best_bid=0.59, best_ask=0.61)
    assert a.bid == pytest.approx(1 - b.ask, abs=1e-9) and a.ask == pytest.approx(
        1 - b.bid, abs=1e-9
    )


def test_inventory_moves_both_quotes_down_and_a_full_book_stops_bidding():
    flat = quote(0, 0.5, gamma=0.05, k=50, best_bid=0.49, best_ask=0.51)
    long = quote(20, 0.5, gamma=0.05, k=50, best_bid=0.49, best_ask=0.51)
    assert long.bid < flat.bid and long.ask < flat.ask  # willing to sell cheaper, buy only cheaper
    huge = quote(80, 0.5, gamma=0.5, k=50, best_bid=0.49, best_ask=0.51)
    assert huge.bid is None  # the reservation price is below one tick: stop buying


def test_a_quote_that_would_have_to_leave_the_open_interval_is_withheld():
    assert quote(0, 0.99, gamma=0.05, k=50).ask is None  # 1.01 is not a price
    assert quote(0, 0.01, gamma=0.05, k=50).bid is None
