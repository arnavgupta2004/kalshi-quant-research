import math

import pytest

from backtest.engine import TIF, BacktestResult, FillRecord, OrderRecord
from backtest.events import BookUpdate
from backtest.metrics import (
    bootstrap_ci,
    execution_stats,
    markouts,
    max_drawdown,
    mid_series,
    period_returns,
    pnl_breakdown,
    resample_equity,
    sharpe,
    sortino,
    summarize,
)
from backtest.portfolio import Portfolio
from market.contracts import Side

SEC = 1_000_000_000


def result(fills=(), orders=(), equity=((0, 100, 100),), pnl=0, fees=0, realised=0, settlements=()):
    p = Portfolio(100)
    p.fees, p.realised = fees, realised
    return BacktestResult(
        list(orders), list(fills), list(equity), list(settlements), p, 100 + pnl, 100, 0, {}
    )


def test_resample_carries_the_last_observation_forward():
    curve = [(0, 100, 0), (25, 110, 0), (70, 90, 0)]
    assert resample_equity(curve, 10) == [
        (0, 100),
        (10, 100),
        (20, 100),
        (30, 110),
        (40, 110),
        (50, 110),
        (60, 110),
        (70, 90),
    ]
    assert resample_equity([], 10) == []


def test_period_returns_are_fractions_of_starting_capital():
    assert period_returns([(0, 100), (1, 110), (2, 99)], 100) == pytest.approx([0.10, -0.11])


def test_sharpe_and_sortino_on_known_series():
    rets = [0.01, -0.01] * 20  # mean 0, symmetric
    assert sharpe(rets, 252) == pytest.approx(0.0, abs=1e-12)
    up = [0.02, 0.01, 0.03, 0.00] * 10
    mean = sum(up) / len(up)
    sd = math.sqrt(sum((r - mean) ** 2 for r in up) / (len(up) - 1))
    assert sharpe(up, 252) == pytest.approx(mean / sd * math.sqrt(252))
    assert sortino(up, 252) is None  # no downside at all: undefined, not infinite
    mixed = [0.02, -0.01, 0.03, -0.02] * 10
    down = math.sqrt(sum(min(0, r) ** 2 for r in mixed) / len(mixed))
    assert sortino(mixed, 252) == pytest.approx(sum(mixed) / len(mixed) / down * math.sqrt(252))
    assert sharpe([0.1] * 5, 252) is None  # too few observations to annualise honestly
    assert sharpe([0.01] * 40, 252) is None  # zero variance


def test_max_drawdown_finds_peak_to_trough_not_start_to_end():
    dd = max_drawdown([(0, 100), (1, 120), (2, 90), (3, 130), (4, 125)])
    assert dd.max_drawdown_micro == 30 and dd.peak_ts == 1 and dd.trough_ts == 2
    assert dd.max_drawdown_pct_of_peak == pytest.approx(0.25)
    assert max_drawdown([(0, 1), (1, 2), (2, 3)]).max_drawdown_micro == 0


def test_bootstrap_ci_is_reproducible_brackets_the_mean_and_shrinks_with_data():
    data = [
        math.sin(i * 1.7) + 0.5 + (i % 7) / 10 for i in range(200)
    ]  # continuous-ish, not two-valued
    a = bootstrap_ci(data, seed=1)
    assert a == bootstrap_ci(data, seed=1) and a != bootstrap_ci(data, seed=2)
    assert a[0] < sum(data) / len(data) < a[1]
    wide = bootstrap_ci(data[:20], seed=1)
    assert (wide[1] - wide[0]) > (a[1] - a[0])
    blocked = bootstrap_ci(data, block=10, seed=1)
    assert blocked[0] < blocked[1]
    with pytest.raises(ValueError):
        bootstrap_ci([])


def test_pnl_breakdown_reconciles_net_gross_fees_and_settlements():
    res = result(
        pnl=500,
        fees=40,
        realised=530,
        settlements=[(1, "A", 10_000, 300), (2, "B", 0, 230)],
        fills=[
            FillRecord(1, 1, "A", Side.YES, 4000, 100, 15, "taker", ""),
            FillRecord(2, 2, "B", Side.NO, 4000, 300, 25, "maker", ""),
        ],
    )
    b = pnl_breakdown(res)
    assert b["net_micro"] == 500 and b["gross_micro"] == 540 and b["fees_micro"] == 40
    assert b["settled_pnl_by_ticker"] == {"A": 300, "B": 230}
    assert b["fees_by_liquidity"] == {"taker": 15, "maker": 25}
    assert b["volume_centi_by_liquidity"] == {"taker": 100, "maker": 300}
    assert b["return_on_initial_cash"] == 5.0


def order(oid, status, qty=100, filled=0, t=0):
    return OrderRecord(oid, t, t, "A", Side.YES, 4000, qty, TIF.GTC, "", status, filled)


def test_execution_statistics():
    orders = [
        order(1, "filled", 100, 100),
        order(2, "partial", 100, 40),
        order(3, "cancelled"),
        order(4, "rejected"),
        order(5, "expired"),
    ]
    fills = [
        FillRecord(2 * SEC, 1, "A", Side.YES, 4000, 100, 0, "maker", ""),
        FillRecord(5 * SEC, 2, "A", Side.YES, 4000, 40, 0, "taker", ""),
    ]
    s = execution_stats(result(fills=fills, orders=orders))
    assert s["orders"] == 5 and s["order_fill_rate"] == 2 / 5 and s["rejection_rate"] == 1 / 5
    assert s["quantity_fill_rate"] == 140 / 400  # rejected orders excluded from the denominator
    assert s["cancellation_rate"] == 1 / 5 and s["maker_fills"] == 1 and s["taker_fills"] == 1
    assert s["median_seconds_to_first_fill"] == 5.0 and s["turnover_contracts"] == 1.4


def test_markouts_measure_price_moves_after_the_fill_for_both_sides():
    events = [
        BookUpdate(0, "A", ((4400, 10),), ((5400, 10),)),  # mid_yes = (4400 + 4600)/2 = 4500
        BookUpdate(10 * SEC, "A", ((4900, 10),), ((4900, 10),)),  # mid_yes = 5000
        BookUpdate(20 * SEC, "A", ((3900, 10),), ((5900, 10),)),
    ]  # mid_yes = 4000
    mids = mid_series(events)
    fills = [
        FillRecord(SEC, 1, "A", Side.YES, 4400, 100, 0, "maker", ""),  # bought YES at 0.44
        FillRecord(SEC, 2, "A", Side.NO, 5400, 100, 0, "taker", ""),
    ]  # bought NO at 0.54
    m = markouts(result(fills=fills), mids, [10, 18])
    assert m[10]["n"] == 2  # both fills have an observation 10s later: mid_yes = 5000
    assert m[10]["by_liquidity"]["maker"] == 5000 - 4400  # YES bought at 4400, now worth 5000: +600
    assert (
        m[10]["by_liquidity"]["taker"] == (10_000 - 5000) - 5400
    )  # NO bought at 5400, worth 5000: -400
    assert (
        m[18]["by_liquidity"]["maker"] == 5000 - 4400
    )  # fill at 1s + 18s = 19s: latest observation is still the 10s one
    assert (
        markouts(result(fills=fills), mids, [0.5])[0.5]["n"] == 0
    )  # nothing observed after the fill yet


def test_summarize_is_json_ready_and_reports_undefined_ratios_honestly():
    import json

    res = result(equity=[(i * 60 * SEC, 100 + i, 100) for i in range(10)], pnl=9)
    s = summarize(res)
    json.dumps(s)  # serialisable
    assert s["risk"]["periods"] == 9 and s["risk"]["sharpe_annualised"] is None  # < 30 observations
    assert s["risk"]["max_drawdown_micro"] == 0 and s["pnl"]["net_micro"] == 9
