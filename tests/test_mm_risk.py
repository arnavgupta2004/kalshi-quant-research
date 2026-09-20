import itertools

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backtest.portfolio import Position
from market_making.risk import (
    MICRO,
    RiskLimits,
    RiskMonitor,
    event_worst_case,
    exposure,
    partition_worlds,
)

S = 10_000  # PRICE_SCALE


def yes(contracts, price):  # a long-YES position bought at `price` dollars
    return Position(yes=int(contracts * 100), yes_cost=int(contracts * 100 * price * S))


def no(contracts, price):
    return Position(no=int(contracts * 100), no_cost=int(contracts * 100 * price * S))


# ------------------------------------------------------------------ one market
def test_long_yes_settlement_outcomes():
    e = exposure(yes(10, 0.45))  # paid $4.50 for 10 YES
    assert e.pnl_if_yes == 5.5 * MICRO and e.pnl_if_no == -4.5 * MICRO
    assert e.worst == -4.5 * MICRO and e.best == 5.5 * MICRO  # the worst case is the WHOLE cost
    assert e.expected(0.45) == pytest.approx(0.45 * 5.5e6 - 0.55 * 4.5e6)
    assert e.expected(0.45) == pytest.approx(0.0, abs=1)  # a fair price has zero expected P&L


def test_long_no_is_the_mirror_image():
    a, b = exposure(yes(10, 0.3)), exposure(no(10, 0.3))  # same price, opposite outcome
    assert a.pnl_if_yes == b.pnl_if_no and a.pnl_if_no == b.pnl_if_yes
    assert b.net_yes == -1000


def test_a_flat_position_has_no_exposure():
    e = exposure(Position())
    assert e.worst == e.best == 0 and e.expected(0.3) == 0


@settings(max_examples=100, deadline=None)
@given(st.integers(1, 200), st.floats(0.01, 0.99), st.floats(0.01, 0.99))
def test_worst_case_is_never_above_the_expected_value_or_below_the_cost(n, price, p):
    e = exposure(yes(n, price))
    assert e.worst <= e.expected(p) <= e.best
    assert e.worst >= -e.cost - 1  # can never lose more than was paid


# ------------------------------------------------------------------ one event, several markets
def brute_force(positions, worlds):
    worst = None
    for w in worlds:
        total = sum(
            (exposure(p).pnl_if_yes if w[t] else exposure(p).pnl_if_no)
            for t, p in positions.items()
        )
        worst = total if worst is None else min(worst, total)
    return worst


def test_exclusive_outcomes_cannot_all_lose_at_once():
    """Long YES on every outcome of a 3-way event at 30c: cost $9 for a $10 payout in EVERY admissible world."""
    pos = {t: yes(10, 0.30) for t in "ABC"}
    assert event_worst_case(pos, partition_worlds("ABC")) == pytest.approx(
        1 * MICRO
    )  # riskless: +$1
    assert (
        event_worst_case(pos) == -9 * MICRO
    )  # treated as independent: all three can lose their cost
    assert event_worst_case(pos, partition_worlds("ABC")) > event_worst_case(pos)


def test_a_concentrated_bet_is_not_helped_by_the_structure():
    pos = {"A": yes(10, 0.5)}  # a bet on one outcome loses everything whichever world you use
    assert event_worst_case(pos, partition_worlds("ABC")) == event_worst_case(pos) == -5 * MICRO


@settings(max_examples=150, deadline=None)
@given(
    st.lists(
        st.tuples(st.integers(1, 5), st.booleans(), st.floats(0.05, 0.95)), min_size=1, max_size=4
    ),
    st.data(),
)
def test_event_worst_case_matches_brute_force_and_never_exceeds_the_independent_sum(legs, data):
    tickers = [f"T{i}" for i in range(len(legs))]
    pos = {
        t: (yes(n, px) if is_yes else no(n, px))
        for t, (n, is_yes, px) in zip(tickers, legs, strict=True)
    }
    worlds = (
        partition_worlds(tickers)
        if data.draw(st.booleans())
        else [
            dict(zip(tickers, w, strict=True))
            for w in itertools.product((0, 1), repeat=len(tickers))
        ]
    )
    got = event_worst_case(pos, worlds)
    assert got == brute_force(pos, worlds)
    independent = sum(exposure(p).worst for p in pos.values())
    assert got >= independent  # restricting the worlds can only make the worst case milder


# ------------------------------------------------------------------ limits
def monitor(**kw):
    lim = RiskLimits(
        **{
            "max_position_contracts": 50,
            "max_event_loss_usd": 100,
            "max_portfolio_loss_usd": 300,
            **kw,
        }
    )
    return RiskMonitor(
        lim,
        {"A1": "EA", "A2": "EA", "B1": "EB", "C1": "EC"},
        {"EA": partition_worlds(["A1", "A2"])},
    )


def test_headroom_respects_the_position_limit_on_each_side():
    m = monitor()
    assert m.headroom({}, "C1", True, 0.10, 500) == 50  # cheap: only the 50-contract cap binds
    long40 = {"C1": yes(40, 0.10)}
    assert m.headroom(long40, "C1", True, 0.10, 500) == 10  # 50 - 40 more YES
    assert m.headroom(long40, "C1", False, 0.10, 500) == 90  # 50 + 40: selling is room, not risk


def test_headroom_shrinks_to_keep_the_worst_case_loss_inside_the_event_limit():
    m = monitor(max_event_loss_usd=10.0)
    n = m.headroom({}, "B1", True, 0.50, 500)
    assert 0 < n <= 20 and n * 0.50 <= 10.0  # buying n YES at 50c can lose n x $0.50
    assert m.headroom({"B1": yes(20, 0.5)}, "B1", True, 0.50, 500) == 0  # already at the limit


def test_hedged_positions_free_up_event_headroom():
    """Long YES on A1 uses the event's loss budget; the offsetting long YES on A2 (exclusive with it) costs
    little extra worst-case loss, so it is allowed where an unrelated market would be refused."""
    m = monitor(max_event_loss_usd=10.0)
    pos = {"A1": yes(20, 0.5)}  # can lose $10 if A1 loses
    assert m.headroom(pos, "B1", True, 0.5, 500) > 0  # a different event has its own budget
    assert m.headroom(pos, "A1", True, 0.5, 500) == 0
    assert m.headroom(pos, "A2", True, 0.5, 500) > 0  # exclusive with A1: cannot both lose the cost


def test_portfolio_limit_sums_event_losses():
    m = monitor(max_event_loss_usd=1000, max_portfolio_loss_usd=12.0)
    pos = {"B1": yes(20, 0.5), "C1": yes(1, 0.5)}  # $10 + $0.50 at risk
    assert m.portfolio_loss(pos) == pytest.approx(10.5)
    assert (
        m.headroom(pos, "B1", True, 0.5, 500) <= 3
    )  # only ~$1.50 of budget is left in the portfolio


# ------------------------------------------------------------------ drawdown and the kill-switch
def test_the_kill_switch_trips_on_drawdown_once_and_stays_tripped():
    m = monitor(max_drawdown_usd=50.0)
    assert m.check(1, 10_000 * MICRO, {}) and m.check(2, 10_040 * MICRO, {})  # up: peak 10,040
    assert m.check(3, 10_000 * MICRO, {})  # 40 down: fine
    assert not m.check(4, 9_985 * MICRO, {})  # 55 below the peak: killed
    assert m.state.killed and m.state.killed_at_ns == 4 and "drawdown" in m.state.reason
    assert not m.check(5, 20_000 * MICRO, {})  # no coming back: a killed strategy stays killed
    assert len(m.state.events) == 1  # and the audit trail records it once


def test_the_kill_switch_trips_when_worst_case_exposure_blows_through_the_hard_limit():
    m = monitor(max_portfolio_loss_usd=20.0, max_event_loss_usd=1000.0)
    assert m.check(1, 10_000 * MICRO, {"B1": yes(30, 0.5)})  # $15 of $20: fine
    assert not m.check(
        2, 10_000 * MICRO, {"B1": yes(50, 0.5), "C1": yes(10, 0.5)}
    )  # $30 > 1.05 x $20
    assert "worst-case" in m.state.reason


def test_a_manual_kill_is_recorded():
    m = monitor()
    m.kill(7, "operator")
    assert m.state.killed and m.state.events == [(7, "KILL: operator")]


def test_markets_the_worlds_do_not_cover_keep_their_own_worst_case():
    """C is not in the partition {A, B}: whichever world holds, C can still lose its whole cost."""
    pos = {"A": yes(10, 0.5), "C": yes(10, 0.5)}
    assert (
        event_worst_case(pos, partition_worlds(["A", "B"])) == -10 * MICRO
    )  # A loses $5, C loses $5
