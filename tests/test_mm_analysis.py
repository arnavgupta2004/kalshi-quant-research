"""The market-making evaluation: every number is checked against a hand calculation on a scenario
small enough to do by hand."""

from datetime import UTC, datetime, timedelta

import pytest

from backtest.events import BookConfirm, BookUpdate, Settlement, TradeTick
from backtest.feed import ListFeed
from backtest.market_info import MarketInfo
from backtest.metrics import mid_series
from market.contracts import Rules, Side, Strike, Trade
from market_making.baseline import BaselineParams
from market_making.quoting import quote
from market_making.risk import MICRO, RiskLimits
from research import market_making_analysis as A
from research import market_making_hypotheses as H

SEC = 1_000_000_000
T0 = 1_800_000_000 * SEC


def c(n):
    return n * 100


def info(t, event="E1", end_in_h=10.0):
    end = datetime.fromtimestamp(T0 / SEC, UTC) + timedelta(hours=end_in_h)
    return MarketInfo(t, event, "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())


def bu(sec, t, yes, no):
    return BookUpdate(T0 + int(sec * SEC), t, tuple(yes), tuple(no))


def taker(sec, t, yes_price, side, contracts=20):
    tr = Trade(
        f"t{sec}", t, yes_price, 10_000 - yes_price, c(contracts), side, "bid", datetime.now(UTC)
    )
    return TradeTick(T0 + int(sec * SEC), t, tr)


class FakeFeed(ListFeed):
    """A ListFeed that also carries what StoreFeed carries: markets and infos."""

    def __init__(self, events, infos):
        super().__init__(events)
        self.infos = infos
        self.markets = [type("M", (), {"ticker": t}) for t in infos]


BOOK = dict(yes=[(4500, c(500))], no=[(4500, c(500))])  # 45c bid / 55c ask: mid 50c
PARAMS = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0)


def data_for(events, settle=None, tickers=("A",)):
    feed = FakeFeed(events, {t: info(t) for t in tickers})
    return A.MMData(
        name="test",
        feed=feed,
        fees=A.FeeBook(),
        event_of={t: "E1" for t in tickers},
        category_of={t: "Sports" for t in tickers},
        worlds={},
        mids=mid_series(feed),
        tapes={},
        settle=settle or {},
    )


def run(events, settle=None, params=PARAMS, maker="optimistic", latency_ms=0.0):
    d = data_for(events, settle)
    return A.run_mm(d, A.RunConfig(params, maker, latency_ms))


def scenario(settle_value=10_000):
    """Bid hit at 47c by a NO taker (we buy 20 YES); mid stays 50c; then the market settles."""
    return [
        bu(0, "A", **BOOK),
        taker(2, "A", 4600, Side.NO, 20),
        bu(10, "A", **BOOK),
        bu(40, "A", **BOOK),
        BookConfirm(T0 + 50 * SEC),
        Settlement(T0 + 60 * SEC, "A", settle_value),
    ]


SKEWED = dict(yes=[(5500, c(500))], no=[(3500, c(500))])  # 55c bid / 65c ask: mid 60c, not 50c


def both_sides():
    """A 60c mid; a NO taker hits our YES bid, later a YES taker lifts our YES offer (we buy NO)."""
    return [
        bu(0, "A", **SKEWED),
        taker(2, "A", 5600, Side.NO, 20),
        bu(10, "A", **SKEWED),
        taker(12, "A", 6400, Side.YES, 20),
        bu(20, "A", **SKEWED),
        bu(60, "A", **SKEWED),
        BookConfirm(T0 + 70 * SEC),
    ]


# ------------------------------------------------------------------ the fill table
def test_fill_rows_carry_the_right_prices_edge_and_markout():
    r = run(scenario(), settle={"A": 10_000})
    rows = A.fill_table(r)
    yes_fills = [f for f in rows if f.side == "yes"]
    assert yes_fills, "the bid should have been hit"
    f = yes_fills[0]
    assert f.price == 4700 and f.mid_yes == 5000
    assert f.edge_micro == (5000 - 4700) * f.qty  # bought 3c below the mid: earned 3c per contract
    assert f.markout30 == 5000 - 4700  # the mid stayed at 50c, so the fill kept its 3c
    assert f.settle_pnl == f.qty * (10_000 - 4700)  # held to a YES settlement
    assert f.cluster == "test:E1"


def test_a_no_fill_is_scored_from_the_no_side():
    r = run(both_sides(), settle={"A": 0})
    rows = A.fill_table(r)
    no_fills = [f for f in rows if f.side == "no"]
    yes_fills = [f for f in rows if f.side == "yes"]
    assert no_fills and yes_fills
    for f in no_fills:  # a NO bought at q with the YES mid at 60c: worth 40c
        assert f.mid_yes == 6000
        assert f.edge_micro == ((10_000 - 6000) - f.price) * f.qty
        assert f.markout30 == (10_000 - 6000) - f.price
        assert f.settle_pnl == f.qty * (10_000 - f.price)  # NO pays $1 when the market settles NO
    for f in yes_fills:
        assert f.edge_micro == (6000 - f.price) * f.qty and f.markout30 == 6000 - f.price
        assert f.settle_pnl == f.qty * (0 - f.price)  # a YES that settled NO is worthless


@pytest.mark.parametrize("value", [10_000, 0])
def test_settled_pnl_per_fill_sums_to_the_engines_realised_pnl(value):
    """The identity the breakdowns rely on: over the fills of settled markets, sum(payoff - price) x qty
    IS the settled P&L before fees, so any grouping of fills attributes P&L exactly."""
    r = run(scenario(value), settle={"A": value})
    rows = A.fill_table(r)
    assert rows and all(f.settle_pnl is not None for f in rows)
    assert sum(f.settle_pnl for f in rows) == sum(s[3] for s in r.res.settlements)


def test_unsettled_markets_have_no_settled_pnl():
    r = run(scenario(), settle={})
    assert all(f.settle_pnl is None for f in A.fill_table(r))


def test_inventory_after_each_fill_is_signed_and_cumulative():
    rows = A.fill_table(run(both_sides(), settle={"A": 10_000}))
    assert {f.side for f in rows} == {
        "yes",
        "no",
    }  # long YES, then long NO: the sign must flip back
    running = 0
    for f in rows:
        running += f.qty if f.side == "yes" else -f.qty
        assert f.inv_after == running
    assert any(f.inv_after < rows[0].inv_after for f in rows[1:])


# ------------------------------------------------------------------ evaluate
def test_gross_pnl_splits_exactly_into_spread_and_inventory():
    r = run(scenario(), settle={"A": 10_000})
    e = A.evaluate(r, n_boot=10)
    p = e["pnl"]
    assert p["gross"] == pytest.approx(p["spread_captured"] + p["inventory_contribution"])
    assert p["net"] == pytest.approx(p["gross"] - p["fees"])
    assert p["net"] == pytest.approx(p["realised"] + p["mark_to_market"] - p["fees"])
    assert e["fills"] == len(e["_fills"]) > 0
    # bought YES 3c under the mid and it settled at $1: the spread part is small and the rest is inventory
    assert p["inventory_contribution"] > p["spread_captured"] > 0


def test_a_run_with_no_fills_reports_none_not_zero_rates():
    r = run([bu(0, "A", **BOOK), BookConfirm(T0 + 5 * SEC)])
    e = A.evaluate(r, n_boot=10)
    assert e["fills"] == 0 and e["pnl"]["net"] == 0
    assert e["execution"]["spread_captured_cents_per_contract"] is None
    assert e["execution"]["inventory_turnover"] is None


def test_inventory_turnover_is_volume_over_average_gross_inventory():
    assert A._turnover(100.0, [(0, 0.0, 10.0, 1), (1, 0.0, 30.0, 1)]) == pytest.approx(5.0)
    assert A._turnover(100.0, [(0, 0.0, 0.0, 0)]) is None  # never held anything
    assert A._turnover(100.0, []) is None  # no log (the naive strategy keeps none)


def test_quote_lifetimes_are_from_placement_to_fill_or_cancel():
    r = run(scenario(), settle={"A": 10_000})
    e = A.evaluate(r, n_boot=10)
    lt = e["execution"]["quote_lifetime_s"]
    assert lt["n"] > 0 and 0 < lt["p50"] <= lt["max"] < 60


def test_a_quote_ends_at_the_earlier_of_its_fill_and_its_cancel_taking_effect():
    class Stub:
        pass

    mm, res = Stub(), Stub()
    mm.placed_at = {1: 0, 2: 0, 3: 0}
    mm.cancel_requested_at = {1: 5 * SEC, 2: 100 * SEC}  # order 3 is never cancelled
    res.fills = [
        type("F", (), {"order_id": 2, "exec_ts": 7 * SEC})(),  # filled before its cancel landed
        type("F", (), {"order_id": 3, "exec_ts": 9 * SEC})(),
    ]
    run_ = A.Run(None, A.RunConfig(latency_ms=1000.0), res, mm)
    assert sorted(A._quote_lifetimes(run_)) == [6.0, 7.0, 9.0]  # 5+1 s cancel, fill at 7, fill at 9


# ------------------------------------------------------------------ breakdowns
def test_breakdown_groups_partition_the_fills_and_the_pnl():
    r = run(scenario(), settle={"A": 10_000})
    fills = A.fill_table(r)
    b = A.breakdowns(fills, n_boot=20)
    for key, groups in b.items():
        assert sum(g.get("fills", 0) for g in groups.values()) == len(fills), key
        total = sum(g.get("settled_pnl_total_usd", 0.0) for g in groups.values())
        assert total == pytest.approx(sum(f.settle_pnl - f.fee for f in fills) / MICRO), key


# ------------------------------------------------------------------ ablations
def test_ablation_variants_differ_only_in_the_element_removed():
    base = BaselineParams()
    cfgs = A.ablation_configs(base, "optimistic", 200.0)
    by = {k[0]: v for k, v in cfgs.items()}
    assert by["A"].strategy == "naive_join"
    assert by["F"].params == base
    assert by["E"].params.limits.max_drawdown_usd == A.UNLIMITED
    assert by["E"].params.limits.max_position_contracts == base.limits.max_position_contracts
    assert by["D"].params.limits.max_position_contracts == A.UNLIMITED
    assert by["D"].params.limits.max_drawdown_usd == A.UNLIMITED
    # B and C: no skew, same width, limits off / on
    assert by["B"].params.gamma == by["C"].params.gamma < 1e-3
    assert by["C"].params.limits == base.limits and by["B"].params.limits != base.limits


def test_the_no_skew_variant_quotes_the_same_width_at_fair_odds_but_does_not_move_with_inventory():
    base = BaselineParams()
    flat = A.ablation_configs(base, "optimistic", 200.0)["C fixed width, no skew, limits + kill"]
    kw = dict(tick=0.001)  # fine tick so rounding does not hide the comparison
    a = quote(0, 0.5, gamma=base.gamma, k=base.k, **kw)
    b = quote(
        0,
        0.5,
        gamma=flat.params.gamma,
        k=flat.params.k,
        min_half_spread=flat.params.min_half_spread,
        **kw,
    )
    assert (b.ask - b.bid) == pytest.approx(a.ask - a.bid, abs=2e-3)  # same width at p = 1/2
    long = quote(
        30,
        0.5,
        gamma=flat.params.gamma,
        k=flat.params.k,
        min_half_spread=flat.params.min_half_spread,
        **kw,
    )
    assert long.bid == pytest.approx(b.bid, abs=2e-3)  # no inventory skew
    skewed = quote(30, 0.5, gamma=base.gamma, k=base.k, **kw)
    assert skewed.bid < a.bid - 0.02  # the baseline does skew


def test_limits_off_keeps_the_data_quality_gates():
    lim = A.limits_off(RiskLimits())
    assert lim.max_book_age_s == RiskLimits().max_book_age_s
    assert lim.min_price == RiskLimits().min_price
    assert lim.stop_hours_before_expiry == RiskLimits().stop_hours_before_expiry
    assert (
        A.limits_off(RiskLimits(), kill_switch=True).max_drawdown_usd
        == RiskLimits().max_drawdown_usd
    )


# ------------------------------------------------------------------ the strategy choices run end to end
@pytest.mark.parametrize("strategy", ["baseline", "naive_join"])
def test_both_strategies_run_and_are_deterministic(strategy):
    cfg = A.RunConfig(PARAMS, "optimistic", 0.0, strategy=strategy)
    d = data_for(scenario(), {"A": 10_000})
    a, b = A.run_mm(d, cfg).res, A.run_mm(d, cfg).res
    assert [(f.exec_ts, f.side, f.price, f.qty) for f in a.fills] == [
        (f.exec_ts, f.side, f.price, f.qty) for f in b.fills
    ]
    assert a.final_equity_micro == b.final_equity_micro


def test_fill_model_bounds_order_the_fills():
    """Optimistic can only fill at least as much as pessimistic on the same feed."""
    d = data_for(scenario(), {"A": 10_000})
    n = {
        m: len(A.run_mm(d, A.RunConfig(PARAMS, m, 0.0)).res.fills)
        for m in ("pessimistic", "optimistic")
    }
    assert n["optimistic"] >= n["pessimistic"]


# ------------------------------------------------------------------ pooling and the inventory-risk table
def test_pooled_adds_pnl_and_takes_the_worst_drawdown():
    r = run(scenario(), settle={"A": 10_000})
    e = A.evaluate(r, n_boot=10)
    p = A.pooled([e, e], n_boot=20)
    assert p["net"] == pytest.approx(2 * e["pnl"]["net"])
    assert p["max_drawdown_worst_db"] == e["risk"]["max_drawdown"]
    assert p["all_fills"]["fills"] == 2 * e["fills"]
    assert p["killed"] == [False, False]
    worse = {**e, "risk": {**e["risk"], "max_drawdown": 99.0, "worst_event_settled": -50.0}}
    q = A.pooled([e, worse], n_boot=20)  # two accounts: the pooled risk is the WORST of them
    assert q["max_drawdown_worst_db"] == 99.0 and q["worst_event_settled"] == -50.0


def test_inventory_risk_by_ttr_uses_the_bounded_martingale_variance():
    rows = A.fill_table(run(both_sides(), settle={"A": 10_000}))
    t = A.inventory_risk_by_ttr(rows, n_boot=20)
    filled = [x for x in t if x["fills"]]
    assert sum(x["fills"] for x in filled) == len([f for f in rows if f.ttr_h is not None])
    assert len(filled) == 1  # every fill here is ~10 h from the scheduled end: one bucket
    x = filled[0]
    sd60 = (0.6 * 0.4) ** 0.5  # the mid is 60c: sqrt(p (1 - p)), not p and not .5
    want = [abs(f.inv_after) / 100 * sd60 for f in rows if f.ttr_h is not None]
    assert x["mean_remaining_std_usd"] == pytest.approx(sum(want) / len(want))
    assert x["bucket"] == "4-12h"


# ------------------------------------------------------------------ the pre-registered hypotheses
def est(v, lo=None, hi=None, n=30):
    return {"value": v, "lo": lo, "hi": hi, "n_rows": 100, "n_clusters": n}


def results(**over):
    row = lambda **k: {  # noqa: E731
        "net_usd": -10.0, "fills": 100, "max_drawdown_usd": 20.0, "worst_event_settled_usd": -5.0,
        "mean_abs_inventory": 5.0, "settled_pnl_per_fill_usd": est(-0.1, -0.2, -0.05), **k,
    }  # fmt: skip
    r = {
        "pooled": {"all_fills": {"markout_30s_cents": est(-2.0, -3.0, -1.0), "settled_pnl_per_fill_usd": est(-0.3, -0.4, -0.2)}},
        "fill_model_latency_sensitivity": [
            {"latency_ms": 200.0, "maker": "pessimistic", "fills": 10, "net_usd": -1.0},
            {"latency_ms": 200.0, "maker": "optimistic", "fills": 15, "net_usd": -2.0},
        ],
        "ablations": {
            "B": row(mean_abs_inventory=45.0, worst_event_settled_usd=-40.0, net_usd=-90.0),
            "D": row(net_usd=-440.0),
            "E": row(max_drawdown_usd=440.0),
            "F": row(max_drawdown_usd=335.0),
        },
        "parameter_grid": [{"gamma": 0.05, "k": 50.0, "settled_pnl_per_fill_usd": est(-0.3, -0.4, -0.2)}],
        "inventory_risk_by_time_to_resolution": [
            {"bucket": "0.5-1.5h", "fills": 10, "markout_30s_cents": est(-3.0)},
            {"bucket": "1.5-4h", "fills": 10, "markout_30s_cents": est(-1.0)},
        ],
    }  # fmt: skip
    r.update(over)
    return r


def verdicts(r):
    return {hid: v for hid, v, *_ in H.evaluate(r)}


def test_hypotheses_hold_on_the_development_pattern():
    v = verdicts(results())
    assert set(v) == {"M1", "M2", "M3a", "M3b", "M4", "M5", "M6", "M7"}
    assert all(x == "consistent" for x in v.values()), v


def test_a_significantly_profitable_result_contradicts_the_hypotheses():
    r = results()
    r["pooled"]["all_fills"]["markout_30s_cents"] = est(0.5, 0.1, 0.9)
    r["pooled"]["all_fills"]["settled_pnl_per_fill_usd"] = est(0.2, 0.1, 0.3)
    r["parameter_grid"][0]["settled_pnl_per_fill_usd"] = est(0.2, 0.1, 0.3)
    v = verdicts(r)
    assert v["M1"] == v["M2"] == v["M5"] == "NOT consistent"


def test_too_few_independent_events_is_inconclusive_not_consistent():
    r = results()
    r["pooled"]["all_fills"]["markout_30s_cents"] = est(-2.0, -3.0, -1.0, n=5)
    assert verdicts(r)["M1"].startswith("inconclusive")


def test_one_positive_sensitivity_cell_breaks_m2():
    r = results()
    r["fill_model_latency_sensitivity"][1]["net_usd"] = 3.0
    assert verdicts(r)["M2"] == "NOT consistent"
