import random

from hypothesis import given, settings
from hypothesis import strategies as st

from backtest.portfolio import Portfolio
from market.contracts import Side
from market.units import PRICE_SCALE as S


def c(n):  # contracts -> centi
    return n * 100


def test_buy_and_settle_long_and_short_exposure():
    p = Portfolio(cash=10**9)
    p.buy("A", Side.YES, 4000, c(100), fee=1000)
    assert p.cash == 10**9 - 4000 * c(100) - 1000 and p.position("A").net_yes == c(100)
    assert p.settle("A", S) == S * c(100) - 4000 * c(100)  # YES wins: +$60
    assert p.realised == 60_000_000 and p.fees == 1000

    q = Portfolio(cash=10**9)
    q.buy("B", Side.NO, 3000, c(100), fee=0)  # long NO == short YES
    assert q.position("B").net_yes == -c(100)
    assert q.settle("B", 0) == S * c(100) - 3000 * c(100)  # YES lost -> NO pays $1


def test_yes_and_no_on_one_market_net_to_a_dollar_and_realise_the_spread():
    p = Portfolio(cash=10**9)
    p.buy("A", Side.YES, 4000, c(10), 0)
    p.buy("A", Side.NO, 5500, c(10), 0)  # 0.40 + 0.55 = 0.95 for a sure $1
    pos = p.position("A")
    assert pos.flat and p.cash == 10**9 - 9500 * c(10) + S * c(10)
    assert p.realised == 500 * c(10)  # 5c per pair, banked at netting, before any settlement


def test_partial_netting_keeps_the_remainder_at_its_own_cost():
    p = Portfolio(cash=10**9)
    p.buy("A", Side.YES, 4000, c(10), 0)
    p.buy("A", Side.NO, 5000, c(4), 0)
    pos = p.position("A")
    assert pos.yes == c(6) and pos.no == 0 and pos.yes_cost == 4000 * c(6)
    assert p.realised == (S - 4000 - 5000) * c(4)


def test_scalar_settlement_pays_fractionally():
    p = Portfolio(cash=10**9)
    p.buy("A", Side.YES, 4000, c(10), 0)
    p.buy("B", Side.NO, 4000, c(10), 0)
    assert p.settle("A", 5000) == (5000 - 4000) * c(10)  # void at $0.50
    assert p.settle("B", 5000) == (S - 5000 - 4000) * c(10)


def test_marks_are_liquidation_values_not_mids():
    p = Portfolio(cash=10**9)
    p.buy("A", Side.YES, 4000, c(10), 0)
    assert p.equity({"A": (3800, None)}) == p.cash + 3800 * c(10)  # sells at the BID, a 2c loss
    assert p.unrealised({"A": (3800, None)}) == -200 * c(10)
    assert p.equity({"A": (None, None)}) == p.cash + 4000 * c(
        10
    )  # no bid: held at cost, no invented profit
    assert p.max_loss() == 4000 * c(10)


def test_cannot_trade_a_settled_market():
    p = Portfolio(cash=1)
    p.settle("A", 0)
    try:
        p.buy("A", Side.YES, 1, 1, 0)
        raise AssertionError("should have refused")
    except ValueError:
        pass


@settings(max_examples=200, deadline=None)
@given(st.data())
def test_conservation_against_an_independent_recomputation(data):
    """After everything settles: cash == initial + sum over fills of (payoff - price) x qty - fees,
    however the fills were netted along the way.  (Netting must preserve value.)"""
    initial = 10**12
    p = Portfolio(initial)
    fills = []
    tickers = ["A", "B", "C"]
    for _ in range(data.draw(st.integers(1, 25))):
        t = data.draw(st.sampled_from(tickers))
        side = data.draw(st.sampled_from(list(Side)))
        price = data.draw(st.integers(1, S - 1))
        qty = data.draw(st.integers(1, 5000))
        fee = data.draw(st.integers(0, 50_000))
        p.buy(t, side, price, qty, fee)
        fills.append((t, side, price, qty, fee))
    values = {
        t: data.draw(st.sampled_from([0, S, 5000, data.draw(st.integers(0, S))])) for t in tickers
    }
    for t in tickers:
        p.settle(t, values[t])
    expected = initial
    for t, side, price, qty, fee in fills:
        pay = values[t] if side is Side.YES else S - values[t]
        expected += (pay - price) * qty - fee
    assert p.cash == expected
    assert all(pos.flat for pos in p.positions.values())
    assert p.realised - p.fees == expected - initial  # realised P&L accounts for every µ$


@settings(max_examples=100, deadline=None)
@given(st.data())
def test_equity_before_settlement_matches_cash_plus_holdings(data):
    p = Portfolio(10**12)
    for _ in range(data.draw(st.integers(1, 15))):
        p.buy(
            data.draw(st.sampled_from("AB")),
            data.draw(st.sampled_from(list(Side))),
            data.draw(st.integers(1, S - 1)),
            data.draw(st.integers(1, 3000)),
            0,
        )
    marks = {t: (data.draw(st.integers(0, S)), data.draw(st.integers(0, S))) for t in "AB"}
    manual = p.cash + sum(
        pos.yes * marks[t][0] + pos.no * marks[t][1] for t, pos in p.positions.items()
    )
    assert p.equity(marks) == manual
    assert not (random.Random(0).random() > 2)  # keep the import honest
