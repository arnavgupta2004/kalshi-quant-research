import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from arbitrage.complementary import complement_specs, crossed_books, two_outcome_specs
from arbitrage.detector import DetectorParams, build_specs, detect
from arbitrage.opportunity import Classification, RelationSpec
from market.fees import FeeBook, FeeSchedule, Rounding
from market.order_book import OrderBook
from market.relationships import (
    Chain,
    Complement,
    Exhaustive,
    Implies,
    MutuallyExclusive,
    Partition,
    Union,
    admissible_worlds,
)
from market.semantics import EvidenceLevel
from market.units import PRICE_SCALE as S
from tests.test_semantics import load

FEES = FeeBook({}, FeeSchedule(known=False))  # no series metadata: standard fee assumed
PARAMS = DetectorParams(fees=FEES, lot=100, min_qty=100, target=3000)  # target: 30 contracts


def c(n):
    return n * 100


def book(ticker, yes=(), no=()):
    b = OrderBook(ticker)
    b.apply_snapshot(list(yes), list(no))
    return b


def spec(relation, level=EvidenceLevel.PROVEN, series="KX"):
    return RelationSpec(relation, level, "EV", series)


def run(relation, books, params=PARAMS, level=EvidenceLevel.PROVEN):
    ops, funnel = detect([spec(relation, level)], books, params, ts_ns=0)
    return ops, funnel


DEEP_ME = {
    "A": book("A", yes=[(5500, c(1000))]),
    "B": book("B", yes=[(5200, c(1000))]),
}  # bids sum 1.07


def test_a_deep_exclusivity_violation_is_executable_with_an_exact_decomposition():
    [op], f = run(MutuallyExclusive(["A", "B"]), DEEP_ME)
    assert op.classification is Classification.EXECUTABLE
    assert op.top_margin == 700 and op.priced.bundles == c(
        1000
    )  # the whole 1000-contract depth pays
    assert op.gross_micro == 700 * c(1000)  # $70 edge at the displayed prices
    assert op.slippage_micro == 0 and op.fee_micro > 0
    assert op.net_micro == op.gross_micro - op.slippage_micro - op.fee_micro
    assert op.net_micro == 35_203_000  # 1000 x (7c - 1.7325c - 1.7472c) = $35.203 (exact)
    assert (
        0 < op.roi < 0.06 and op.fees_known is False
    )  # no series metadata -> standard fee assumed
    assert f.rows() == [
        ("priced", 1),
        ("displayed", 1),
        ("liquid", 1),
        ("after_fees", 1),
        ("after_slippage", 1),
        ("executable", 1),
    ]


def test_coherent_prices_produce_nothing():
    books = {
        "A": book("A", yes=[(5000, c(100))]),
        "B": book("B", yes=[(4500, c(100))]),
    }  # bids sum 0.95
    ops, f = run(MutuallyExclusive(["A", "B"]), books)
    assert ops == [] and f.priced == 1 and f.displayed == 0


def test_theoretical_when_the_displayed_price_has_no_real_size():
    books = {
        "A": book("A", yes=[(5500, 40)]),
        "B": book("B", yes=[(5200, c(1000))]),
    }  # 0.4 contracts on A
    [op], f = run(MutuallyExclusive(["A", "B"]), books)
    assert op.classification is Classification.THEORETICAL and op.priced is None and not op.liquid
    assert f.displayed == 1 and f.liquid == 0


def test_unprofitable_when_fees_eat_a_thin_edge():
    # bids 0.51 + 0.50: 1c of gross edge per bundle vs ~3.5c of fees
    books = {"A": book("A", yes=[(5100, c(1000))]), "B": book("B", yes=[(5000, c(1000))])}
    [op], f = run(MutuallyExclusive(["A", "B"]), books)
    assert op.classification is Classification.UNPROFITABLE and op.top_margin == 100
    assert op.net_micro < 0 and not op.profitable_after_fees_top
    assert (f.displayed, f.liquid, f.after_fees, f.after_slippage, f.executable) == (1, 1, 0, 0, 0)
    # the SAME books are profitable if trading were free: the fee is what kills it
    free = DetectorParams(fees=FEES.scaled(Fraction(0)), target=3000)
    [op0], _ = run(MutuallyExclusive(["A", "B"]), books, free)
    assert op0.classification is Classification.EXECUTABLE and op0.net_micro == op0.gross_micro


def test_partial_when_only_a_sliver_of_depth_is_profitable():
    books = {
        "A": book("A", yes=[(5500, c(10)), (5000, c(1000))]),
        "B": book("B", yes=[(5200, c(1000))]),
    }
    [op], _ = run(MutuallyExclusive(["A", "B"]), books)
    assert op.classification is Classification.PARTIAL
    assert op.priced.bundles == c(10) < op.target_bundles
    assert (
        op.breakeven_bundles > op.priced.bundles
    )  # buying more still nets positive in total, but marginally loses


def test_slippage_is_reported_when_the_best_size_walks_the_book():
    books = {
        "A": book("A", yes=[(5500, c(40)), (5400, c(1000))]),
        "B": book("B", yes=[(5200, c(1000))]),
    }
    [op], _ = run(MutuallyExclusive(["A", "B"]), books)
    assert op.classification is Classification.EXECUTABLE and op.priced.bundles > c(40)
    assert (
        op.slippage_micro > 0 and op.net_micro == op.gross_micro - op.slippage_micro - op.fee_micro
    )


def test_exhaustive_violation_buys_every_yes():
    # YES asks 0.40 + 0.45 = 0.85 < 1: buy both, $1 for sure
    books = {"A": book("A", no=[(6000, c(500))]), "B": book("B", no=[(5500, c(500))])}
    [op], _ = run(Exhaustive(["A", "B"]), books)
    assert op.classification is Classification.EXECUTABLE and op.top_margin == 1500
    assert {leg.contract.side.value for leg in op.priced.legs} == {"yes"}


def test_implication_arbitrage_buys_the_wider_event_and_the_no_of_the_narrower():
    books = {
        "narrow": book("narrow", yes=[(6200, c(500))]),
        "wide": book("wide", no=[(4200, c(500))]),
    }
    # wide YES ask = 0.58, narrow YES bid = 0.62 -> narrower priced ABOVE the wider event
    [op], _ = run(Implies("narrow", "wide"), books)
    assert op.top_margin == 400 and {
        (leg.contract.ticker, leg.contract.side.value) for leg in op.priced.legs
    } == {("wide", "yes"), ("narrow", "no")}


def test_evidence_level_gate_and_missing_books():
    ops, f = run(MutuallyExclusive(["A", "B"]), DEEP_ME, level=EvidenceLevel.UNVERIFIED)
    assert ops == [] and f.constraints == 0  # below min_level: never even priced
    ops, f = run(MutuallyExclusive(["A", "B"]), {"A": DEEP_ME["A"]})
    assert ops == [] and f.unpriceable == 1 and f.priced == 0


def test_known_fee_metadata_is_used_and_flagged():
    known = FeeBook(
        {"KX": FeeSchedule("quadratic", Fraction(1), known=True)}, FeeSchedule(known=False)
    )
    [op], f = run(
        MutuallyExclusive(["A", "B"]),
        {t: DEEP_ME[t] for t in "AB"},
        DetectorParams(fees=known, target=3000),
    )
    assert (
        op.fees_known is False
    )  # tickers 'A'/'B' have series 'A'/'B', not 'KX' -> default (assumed) applies
    books = {
        "KX-A": book("KX-A", yes=[(5500, c(1000))]),
        "KX-B": book("KX-B", yes=[(5200, c(1000))]),
    }
    [op], f = run(
        MutuallyExclusive(["KX-A", "KX-B"]), books, DetectorParams(fees=known, target=3000)
    )
    assert op.fees_known is True and f.fee_assumed == 0


def test_fee_rounding_and_multiplier_sensitivity_move_net_edge_the_right_way():
    def net(fees):
        [op], _ = run(
            MutuallyExclusive(["A", "B"]), DEEP_ME, DetectorParams(fees=fees, target=3000)
        )
        return op.net_micro

    exact = net(FEES)
    assert (
        net(FEES.with_rounding(Rounding.CENT_PER_FILL)) <= exact
    )  # conservative rounding never helps
    assert net(FEES.scaled(Fraction(2))) < exact < net(FEES.scaled(Fraction(0)))


def test_roi_and_time_locked_up():
    from datetime import UTC, datetime

    from market.timeutil import to_epoch_us

    now = datetime(2026, 9, 19, tzinfo=UTC)
    sp = RelationSpec(
        MutuallyExclusive(["A", "B"]),
        EvidenceLevel.PROVEN,
        "EV",
        "KX",
        close_time=datetime(2026, 9, 29, tzinfo=UTC),
    )
    [op], _ = detect([sp], DEEP_ME, PARAMS, ts_ns=to_epoch_us(now) * 1000)
    assert op.days_to_close == pytest.approx(10.0)
    assert op.annualized_roi == pytest.approx(op.roi * 36.5)  # capital locked 10 days


# ---------------------------------------------------------------------------- the guarantee
def world_pnl_never_below_net(op):
    """In EVERY admissible world the realised P&L must be >= the computed net; and equal in one."""
    worlds = admissible_worlds(op.spec.relation)
    pnl = [op.settle_micro(w) for w in worlds]
    assert all(p >= op.net_micro for p in pnl), (
        op.spec.relation.statement(),
        min(pnl),
        op.net_micro,
    )
    return min(pnl) == op.net_micro


@pytest.mark.parametrize(
    "rel,books",
    [
        (MutuallyExclusive(["A", "B"]), DEEP_ME),
        (Partition(["A", "B"]), DEEP_ME),
        (
            Exhaustive(["A", "B"]),
            {"A": book("A", no=[(6000, c(500))]), "B": book("B", no=[(5500, c(500))])},
        ),
        (
            Implies("narrow", "wide"),
            {
                "narrow": book("narrow", yes=[(6200, c(500))]),
                "wide": book("wide", no=[(4200, c(500))]),
            },
        ),
    ],
)
def test_realised_pnl_is_at_least_the_net_edge_in_every_possible_world(rel, books):
    ops, _ = run(rel, books)
    assert ops and all(op.classification is Classification.EXECUTABLE for op in ops)
    assert any(world_pnl_never_below_net(op) for op in ops)  # tight in at least one world


@settings(max_examples=150, deadline=None)
@given(st.data())
def test_guarantee_holds_for_random_books_and_every_relation(data):
    n = data.draw(st.integers(2, 4))
    tickers = [f"KX-M{i}" for i in range(n)]
    books = {}
    for t in tickers:
        yes = [(data.draw(st.integers(200, 9000)), c(data.draw(st.integers(1, 30))))]
        no = [(data.draw(st.integers(200, 9800 - yes[0][0])), c(data.draw(st.integers(1, 30))))]
        books[t] = book(t, yes=yes, no=no)
    rel = data.draw(
        st.sampled_from(
            [
                MutuallyExclusive(tickers),
                Exhaustive(tickers),
                Partition(tickers),
                Chain(tickers),
                Union(tickers[:-1], tickers[-1]) if n >= 3 else Chain(tickers),
            ]
        )
    )
    fees = FeeBook({}, FeeSchedule(rounding=data.draw(st.sampled_from(list(Rounding)))))
    ops, f = detect(
        [spec(rel, series="KX")], books, DetectorParams(fees=fees, target=c(20)), ts_ns=0
    )
    for op in ops:
        if op.priced is None:
            continue
        pnl = [op.settle_micro(w) for w in admissible_worlds(rel)]
        assert min(pnl) >= op.net_micro
        assert (
            op.net_micro == op.priced.gross_micro - op.priced.slippage_micro - op.priced.fee_micro
        )
    assert f.displayed >= f.liquid >= f.after_slippage >= f.executable >= 0
    assert f.liquid >= f.after_fees and sum(f.by_class.values()) == f.displayed


@settings(max_examples=120, deadline=None)
@given(st.data(), st.lists(st.integers(0, 9), min_size=4, max_size=4), st.integers(0, 400))
def test_books_priced_from_a_coherent_model_never_yield_an_opportunity(data, weights, spread):
    """No-false-positive property: if a probability model generated the prices, there is nothing to find."""
    rel = data.draw(
        st.sampled_from(
            [
                MutuallyExclusive(["M0", "M1", "M2"]),
                Exhaustive(["M0", "M1", "M2"]),
                Partition(["M0", "M1", "M2"]),
                Chain(["M0", "M1", "M2"]),
            ]
        )
    )
    worlds = admissible_worlds(rel)
    w = [weights[i % 4] for i in range(len(worlds))]
    if not sum(w):
        w[0] = 1
    books = {}
    for t in rel.tickers():
        p = sum(wt * wd[t] for wt, wd in zip(w, worlds, strict=True)) / sum(
            w
        )  # model probability of YES
        yes_bid = max(1, math.floor(p * S) - spread)
        no_bid = max(1, math.floor((1 - p) * S) - spread)
        if yes_bid + no_bid >= S:
            continue
        books[t] = book(t, yes=[(min(yes_bid, S - 1), c(50))], no=[(min(no_bid, S - 1), c(50))])
    if len(books) < len(rel.tickers()):
        return
    ops, f = detect([spec(rel)], books, DetectorParams(fees=FEES.scaled(Fraction(0))), ts_ns=0)
    assert ops == [], (rel.statement(), [(t, b.best_bid(), b.best_ask()) for t, b in books.items()])


# ---------------------------------------------------------------------------- integrity + builders
def test_crossed_books_are_flagged_as_data_errors_not_opportunities():
    ok = book("A", yes=[(5000, c(1))], no=[(4000, c(1))])
    bad = book(
        "B", yes=[(6500, c(1))], no=[(4000, c(1))]
    )  # 0.65 + 0.40 > 1: cannot exist on a real book
    assert crossed_books({"A": ok, "B": bad}) == ["B"]
    ev, ms = load("epl_3way")
    [spec_b] = [s for s in complement_specs(ev, ms) if s.relation.tickers() == (ms[0].ticker,)]
    assert spec_b.level is EvidenceLevel.PROVEN


def test_same_market_arbitrage_needs_a_crossed_book():
    """On any valid (uncrossed) book the complement margin is <= 0: buying YES and NO can never lock a profit."""
    for yes_bid, no_bid in [(1, 1), (5000, 4999), (9998, 1), (3000, 6999)]:
        b = book("A", yes=[(yes_bid, c(10))], no=[(no_bid, c(10))])
        ops, f = run(Complement("A"), {"A": b})
        assert ops == [] and f.priced == 1


def test_build_specs_partitions_relations_without_double_counting():
    bundles = [load(n) for n in ("nyc_high", "atp_match", "btc_ladder", "btc_range", "mlb_spread")]
    specs = build_specs(bundles)
    kinds = {}
    for s in specs:
        kinds.setdefault(s.relation.kind.value, 0)
        kinds[s.relation.kind.value] += 1
    assert kinds["complement"] == sum(len(ms) for _, ms in bundles)
    two = [
        s for s in specs if s.relation.kind.value == "partition" and len(s.relation.tickers()) == 2
    ]
    assert len(two) == 1 and two[0].event_ticker.startswith("KXATPMATCH")  # the tennis match, once
    assert (
        kinds["union"] > 100 and kinds["chain"] == 3
    )  # the BTC ladder + one per team in the MLB spread event
    ev, ms = load("atp_match")
    from market.semantics import analyze_event

    assert len(two_outcome_specs(ev, analyze_event(ev, ms), ms)) == 1


def test_the_same_trade_reached_through_several_relations_is_one_opportunity():
    """MutuallyExclusive, Partition (which contains it) and a weaker declared copy all imply the SAME
    'buy every NO' bundle.  It must be counted once and credited to the strongest evidence."""
    specs = [
        RelationSpec(MutuallyExclusive(["A", "B"]), EvidenceLevel.DECLARED, "EV", "KX"),
        RelationSpec(Partition(["A", "B"]), EvidenceLevel.LATTICE, "EV", "KX"),
        RelationSpec(MutuallyExclusive(["A", "B"]), EvidenceLevel.PROVEN, "EV", "KX"),
    ]
    ops, f = detect(specs, DEEP_ME, PARAMS, ts_ns=0)
    assert len(ops) == 1 and ops[0].spec.level is EvidenceLevel.PROVEN  # strongest claim wins
    assert f.displayed == 1 and f.duplicates >= 2
    # the partition's other half (exhaustive) is a different trade: evaluated, but unpriceable here
    assert (
        f.constraints == 2 and f.priced == 1 and f.unpriceable == 1
    )  # no YES offers in these books
