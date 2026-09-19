from datetime import UTC, datetime
from fractions import Fraction

import pytest

from arbitrage.detector import DetectorParams, detect
from arbitrage.opportunity import Classification, RelationSpec
from backtest.engine import BacktestConfig, run_backtest
from backtest.events import BookUpdate, Settlement, TradeTick
from backtest.feed import ListFeed
from backtest.fills import QueueModel
from backtest.market_info import MarketInfo
from backtest.strategies import ArbitrageTaker, PassiveQuoter
from market.contracts import Rules, Side, Strike, Trade
from market.fees import FeeBook, FeeSchedule
from market.order_book import OrderBook
from market.relationships import MutuallyExclusive, admissible_worlds
from market.semantics import EvidenceLevel
from market.units import PRICE_SCALE as S

MS = 1_000_000
T0 = 1_800_000_000_000_000_000
FEES = FeeBook({}, FeeSchedule(known=False))  # standard taker fee, flagged as assumed


def c(n):
    return n * 100


def info(t):
    return MarketInfo(t, "E", "KX", "C", "t", "y", "n", Strike(), Rules(), None, None, ())


def bu(ts_ms, t, yes=(), no=()):
    return BookUpdate(T0 + ts_ms * MS, t, tuple(yes), tuple(no))


def cfg(latency_ms=0, fees=FEES, maker=None, cash=10**11):
    return BacktestConfig(
        initial_cash_micro=cash,
        latency_submit_ns=latency_ms * MS,
        latency_ack_ns=latency_ms * MS,
        latency_cancel_ns=latency_ms * MS,
        fees=fees,
        maker=maker or QueueModel.optimistic(),
        equity_sample_ns=10**12,
    )


REL = MutuallyExclusive(["A", "B"])
SPEC = RelationSpec(REL, EvidenceLevel.PROVEN, "EV", "KX")
PARAMS = DetectorParams(fees=FEES, target=3000)
DEEP = [
    bu(0, "A", yes=[(5500, c(1000))]),
    bu(0, "B", yes=[(5200, c(1000))]),
]  # YES bids 0.55 + 0.52


def world_events(winner):
    return [
        Settlement(T0 + 10_000 * MS, "A", S if winner == "A" else 0),
        Settlement(T0 + 10_000 * MS, "B", S if winner == "B" else 0),
    ]


# ------------------------------------------------------------------ Stage 4  <->  Stage 5
@pytest.mark.parametrize("winner", ["A", "B"])
def test_backtest_pnl_equals_the_detectors_predicted_net_edge_exactly(winner):
    """Zero latency, full fills, standard fees: what Stage 4 predicted is what Stage 5 realises."""
    books = {"A": OrderBook("A"), "B": OrderBook("B")}
    books["A"].apply_snapshot([(5500, c(1000))], [])
    books["B"].apply_snapshot([(5200, c(1000))], [])
    [op], _ = detect([SPEC], books, PARAMS, ts_ns=0)
    assert op.classification is Classification.EXECUTABLE

    strat = ArbitrageTaker([SPEC], PARAMS)
    res = run_backtest(
        ListFeed(DEEP + world_events(winner)), {"A": info("A"), "B": info("B")}, strat, cfg()
    )
    assert strat.opportunities_seen == 1 and len(res.fills) == 2
    assert res.portfolio.fees == op.fee_micro  # same fees, fill by fill
    assert sum(f.qty for f in res.fills) == sum(leg.qty for leg in op.priced.legs)
    assert res.pnl_micro == op.net_micro  # to the µ$
    assert res.orders[0].status == "filled" and res.orders[1].status == "filled"


def test_realised_pnl_is_never_below_the_predicted_edge_in_any_possible_world():
    books = {"A": OrderBook("A"), "B": OrderBook("B")}
    books["A"].apply_snapshot([(5500, c(1000))], [])
    books["B"].apply_snapshot([(5200, c(1000))], [])
    [op], _ = detect([SPEC], books, PARAMS, ts_ns=0)
    pnls = []
    for w in admissible_worlds(REL):  # none / A / B
        evs = [Settlement(T0 + 10_000 * MS, t, S * w[t]) for t in ("A", "B")]
        res = run_backtest(
            ListFeed(DEEP + evs),
            {"A": info("A"), "B": info("B")},
            ArbitrageTaker([SPEC], PARAMS),
            cfg(),
        )
        pnls.append(res.pnl_micro)
        assert res.pnl_micro >= op.net_micro
        assert res.pnl_micro == op.settle_micro(
            w
        )  # the detector's own settlement model agrees with the engine
    assert (
        min(pnls) == op.net_micro and max(pnls) > op.net_micro
    )  # tight in the worst world, better when nobody wins


def test_fee_free_variant_earns_exactly_the_gross_edge():
    free = DetectorParams(fees=FEES.scaled(Fraction(0)), target=3000)
    res = run_backtest(
        ListFeed(DEEP + world_events("A")),
        {"A": info("A"), "B": info("B")},
        ArbitrageTaker([SPEC], free),
        cfg(fees=FEES.scaled(Fraction(0))),
    )
    assert res.portfolio.fees == 0 and res.pnl_micro == 700 * c(1000)  # 7c x 1000 contracts = $70


def test_a_coherent_market_makes_the_strategy_do_nothing():
    coherent = [
        bu(0, "A", yes=[(5000, c(1000))]),
        bu(0, "B", yes=[(4500, c(1000))]),
    ]  # bids sum 0.95
    strat = ArbitrageTaker([SPEC], PARAMS)
    res = run_backtest(
        ListFeed(coherent + world_events("A")), {"A": info("A"), "B": info("B")}, strat, cfg()
    )
    assert not res.orders and res.pnl_micro == 0 and strat.opportunities_seen == 0


# ------------------------------------------------------------------ leg risk (what Stage 4 assumes away)
def test_latency_turns_a_riskless_arbitrage_into_leg_risk():
    """The detector saw both legs; by the time the orders arrive B's book has moved, so only leg A
    fills and the 'arbitrage' is an unhedged directional bet."""
    stale = DEEP + [
        bu(50, "B", yes=[(4000, c(1000))])
    ]  # B's YES bid falls to 0.40 => its NO ask rises to 0.60
    fills_by_world = {}
    for winner in ("A", "B"):
        res = run_backtest(
            ListFeed(stale + world_events(winner)),
            {"A": info("A"), "B": info("B")},
            ArbitrageTaker([SPEC], PARAMS),
            cfg(latency_ms=100),
        )
        assert [f.ticker for f in res.fills] == ["A"] and res.fills[
            0
        ].side is Side.NO  # B's IOC found nothing at its limit
        assert {o.ticker: o.status for o in res.orders} == {"A": "filled", "B": "expired"}
        fills_by_world[winner] = res.pnl_micro
    qty = res.fills[0].qty
    fee = res.fills[0].fee_micro
    assert fills_by_world["A"] == -4500 * qty - fee  # A won: the NO we hold expires worthless
    assert fills_by_world["B"] == (S - 4500) * qty - fee  # B won: the NO on A pays $1
    assert fills_by_world["A"] < 0 < fills_by_world["B"]  # direction, not arbitrage


def test_fills_are_reported_to_the_strategy_after_the_ack_latency():
    strat = ArbitrageTaker([SPEC], PARAMS)
    run_backtest(
        ListFeed(DEEP + world_events("A")),
        {"A": info("A"), "B": info("B")},
        strat,
        cfg(latency_ms=100),
    )
    assert strat.fill_events and all(f.ts_ns - f.exec_ts_ns == 100 * MS for f in strat.fill_events)


# ------------------------------------------------------------------ passive quoting across the fill models
def tick(ts_ms, yes_price, count, taker="no", t="T", tid=None):
    tr = Trade(
        tid or f"{ts_ms}",
        t,
        yes_price,
        S - yes_price,
        count,
        Side(taker),
        None,
        datetime(2026, 9, 1, tzinfo=UTC),
    )
    return TradeTick(T0 + ts_ms * MS, t, tr)


def quoter_run(model, prints, winner_yes=True):
    feed = (
        [bu(0, "T", yes=[(4000, c(50))], no=[(5500, c(50))])]
        + prints
        + [Settlement(T0 + 100_000 * MS, "T", S if winner_yes else 0)]
    )
    strat = PassiveQuoter(["T"], qty=c(10))
    res = run_backtest(
        ListFeed(feed), {"T": info("T")}, strat, cfg(fees=FEES.scaled(Fraction(0)), maker=model)
    )
    return res, strat


def test_passive_pnl_is_a_range_across_the_fill_models_and_the_ordering_holds():
    """50 contracts queue at 0.40; 70 contracts trade AT 0.40 (never through it)."""
    prints = [tick(1_000, 4000, c(30)), tick(2_000, 4000, c(40))]
    results = {
        name: quoter_run(m, prints)[0]
        for name, m in [
            ("pessimistic", QueueModel.pessimistic()),
            ("queue", QueueModel.queue(1.0, 0.0)),
            ("optimistic", QueueModel.optimistic()),
        ]
    }
    filled = {k: sum(f.qty for f in r.fills if f.side is Side.YES) for k, r in results.items()}
    assert filled == {
        "pessimistic": 0,
        "queue": c(10),
        "optimistic": c(10),
    }  # 30 eaten ahead, then 40: 20 ahead + 20 ours
    pnl = {k: r.pnl_micro for k, r in results.items()}
    assert pnl["pessimistic"] < pnl["queue"]  # the queue model gets the fill; YES wins: +$0.60 x 10
    assert pnl["queue"] == (S - 4000) * c(10) == pnl["optimistic"]
    # the honest reading: this strategy's edge is only established if it survives the pessimistic model
    assert pnl["pessimistic"] == 0


def test_a_print_through_our_price_fills_every_model_identically():
    res = {
        k: quoter_run(m, [tick(1_000, 3900, c(1))])[0].fills
        for k, m in [
            ("p", QueueModel.pessimistic()),
            ("q", QueueModel.queue(1.0, 0.0)),
            ("o", QueueModel.optimistic()),
        ]
    }
    assert all(sum(f.qty for f in fills if f.side is Side.YES) == c(10) for fills in res.values())


def test_quoter_stays_passive_and_requotes_when_the_book_moves():
    feed = [
        bu(0, "T", yes=[(4000, c(50))], no=[(5500, c(50))]),
        bu(1_000, "T", yes=[(4100, c(50))], no=[(5500, c(50))]),
    ]
    res, strat = quoter_run(QueueModel.optimistic(), [])
    res2 = run_backtest(ListFeed(feed), {"T": info("T")}, PassiveQuoter(["T"], qty=c(10)), cfg())
    yes_orders = [o for o in res2.orders if o.side is Side.YES]
    assert [o.price for o in yes_orders] == [4000, 4100]  # followed the best bid up
    assert yes_orders[0].status == "cancelled"
    assert all(o.tif.value == "post_only" for o in res2.orders)
