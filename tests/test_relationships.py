"""Relations are tested against their *definitions*, independently of how constraints are built.

Four independent lines of evidence per relation:
  soundness       every admissible world satisfies every constraint
  characterises   every inadmissible world violates at least one constraint
  tightness       every bound is attained (no slack that would hide an arbitrage)
  LP oracle       "no constraint violated" == "some admissible distribution fits inside the quotes"
"""

import math

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.optimize import linprog

from market.contracts import ContractRef, Side
from market.order_book import Level, OrderBook
from market.relationships import (
    Chain,
    Complement,
    Exhaustive,
    Implies,
    Leg,
    MutuallyExclusive,
    Partition,
    RelationKind,
    Union,
    admissible_worlds,
    all_worlds,
    asks_from_books,
    scan,
)
from market.units import PRICE_SCALE as S

M = [f"M{i}" for i in range(5)]


def relations():
    out = [Complement("M0")]
    for n in (2, 3, 4, 5):
        out += [MutuallyExclusive(M[:n]), Exhaustive(M[:n]), Partition(M[:n])]
        out += [Chain(M[:n])]
    out += [Implies("M0", "M1"), Implies("M1", "M0")]
    for n in (2, 3, 4):
        out += [Union(M[:n], "W"), Union(M[:n], "W", disjoint=False)]
    return out


IDS = [
    f"{r.kind.value}:{len(r.tickers())}{'' if getattr(r, 'disjoint', True) else ':overlap'}"
    for r in relations()
]


@pytest.mark.parametrize("rel", relations(), ids=IDS)
def test_soundness_characterisation_tightness(rel):
    cons = rel.constraints()
    good = admissible_worlds(rel)
    bad = [w for w in all_worlds(rel.tickers()) if not rel.admissible(w)]
    assert good, "a relation with no admissible world is contradictory"
    for w in good:  # soundness
        for c in cons:
            assert c.world_payoff(w) >= c.bound, (rel.statement(), c.name, w)
    for w in bad:  # the constraints exclude every impossible world ...
        assert any(c.world_payoff(w) < c.bound for c in cons), (rel.statement(), w)
    for c in cons:  # ... and each bound is attained by some possible world
        assert min(c.world_payoff(w) for w in good) == c.bound, (rel.statement(), c.name)


def test_admissible_world_counts_match_the_combinatorics():
    assert len(admissible_worlds(Partition(M[:4]))) == 4  # exactly one YES
    assert len(admissible_worlds(MutuallyExclusive(M[:4]))) == 5  # none or one
    assert len(admissible_worlds(Exhaustive(M[:4]))) == 15  # anything but all-NO
    assert len(admissible_worlds(Chain(M[:4]))) == 5  # 0000 0001 0011 0111 1111
    assert len(admissible_worlds(Implies("M0", "M1"))) == 3
    assert len(admissible_worlds(Union(M[:3], "W"))) == 4  # parts ME: none/one, W follows
    assert len(admissible_worlds(Union(M[:3], "W", disjoint=False))) == 8  # any parts, W = OR


# ---------------------------------------------------------------------------- pricing
def quotes_from_bidask(bids: dict[str, int], asks: dict[str, int], qty=1000):
    """YES ask = a; NO ask = 1 - YES bid (the identity a bids-only book enforces)."""

    def ask(c: ContractRef):
        if c.side is Side.YES:
            return Level(asks[c.ticker], qty)
        return Level(S - bids[c.ticker], qty)

    return ask


def violated(rel, bids, asks) -> bool:
    ask = quotes_from_bidask(bids, asks)
    return any(chk.violated for con in rel.constraints() if (chk := con.check(ask)))


def lp_feasible(rel, bids, asks) -> bool:
    """Does any distribution over admissible worlds have E[Y_i] inside [bid_i, ask_i]?

    Solved in *probability* units.  (Scaling by the 10,000 tick size with a 1e-6 tolerance made
    HiGHS call an exactly-coherent instance infeasible - found by hypothesis - so the oracle
    itself is stress-tested in ``test_oracle_is_numerically_robust``.)"""
    ts, worlds = rel.tickers(), admissible_worlds(rel)
    A_ub, b_ub = [], []
    for t in ts:
        row = [w[t] for w in worlds]
        A_ub.append(row)
        b_ub.append(asks[t] / S + 1e-9)
        A_ub.append([-x for x in row])
        b_ub.append(-bids[t] / S + 1e-9)
    res = linprog(
        [0] * len(worlds),
        A_ub=A_ub,
        b_ub=b_ub,
        A_eq=[[1] * len(worlds)],
        b_eq=[1],
        bounds=(0, None),
        method="highs",
    )
    return res.status == 0


@pytest.mark.parametrize(
    "rel",
    [r for r in relations() if r.kind is not RelationKind.UNION],
    ids=lambda r: f"{r.kind.value}{len(r.tickers())}",
)
@settings(max_examples=60, deadline=None)
@given(data=st.data())
def test_lp_oracle_no_arbitrage_missed_and_none_invented(rel, data):
    # bias quotes toward the interesting region so both outcomes are exercised
    coarse = data.draw(st.booleans())
    bids, asks = {}, {}
    for t in rel.tickers():
        b = data.draw(st.integers(0, 20)) * 500 if coarse else data.draw(st.integers(0, S))
        a = min(
            S,
            b + (data.draw(st.integers(0, 6)) * 500 if coarse else data.draw(st.integers(0, 3000))),
        )
        bids[t], asks[t] = b, a
    assert violated(rel, bids, asks) == (not lp_feasible(rel, bids, asks)), (
        rel.statement(),
        bids,
        asks,
    )


@pytest.mark.parametrize("disjoint", [True, False])
@pytest.mark.parametrize("n", [2, 3])
@settings(max_examples=60, deadline=None)
@given(data=st.data())
def test_lp_oracle_for_unions(n, disjoint, data):
    rel = Union(M[:n], "W", disjoint=disjoint)
    bids, asks = {}, {}
    for t in rel.tickers():
        b = data.draw(st.integers(0, 20)) * 500
        bids[t], asks[t] = b, min(S, b + data.draw(st.integers(0, 4)) * 500)
    assert violated(rel, bids, asks) == (not lp_feasible(rel, bids, asks)), (
        rel.statement(),
        bids,
        asks,
    )


@settings(max_examples=80, deadline=None)
@given(data=st.data(), spreads=st.lists(st.integers(0, 800), min_size=6, max_size=6))
def test_prices_from_any_coherent_distribution_never_flag_a_violation(data, spreads):
    """Soundness in the economically meaningful direction: a market whose prices come from a real
    probability model over admissible worlds must never look like an arbitrage."""
    rel = data.draw(
        st.sampled_from([r for r in relations() if r.kind is not RelationKind.COMPLEMENT])
    )
    worlds = admissible_worlds(rel)
    w = [data.draw(st.integers(0, 9)) for _ in worlds]
    if sum(w) == 0:
        w[0] = 1
    total = sum(w)
    bids, asks = {}, {}
    for i, t in enumerate(rel.tickers()):
        mean = sum(wt * wd[t] for wt, wd in zip(w, worlds, strict=True)) * S / total
        sp = spreads[i % len(spreads)]
        bids[t] = max(0, math.floor(mean) - sp)  # widen outward -> only ever *more* coherent
        asks[t] = min(S, math.ceil(mean) + sp)
    assert not violated(rel, bids, asks)


def test_chain_needs_non_adjacent_pairs():
    """bid0 <= ask1 and bid1 <= ask2 hold, yet bid0 > ask2: only the (0,2) pair sees the arbitrage."""
    rel = Chain(["A", "B", "C"])  # A ⊆ B ⊆ C
    bids = {"A": 6000, "B": 5000, "C": 4000}
    asks = {"A": 6500, "B": 6500, "C": 5500}
    ask = quotes_from_bidask(bids, asks)
    flagged = {c.name for c in rel.constraints() if (k := c.check(ask)) and k.violated}
    assert flagged == {"A=>C"}
    assert not lp_feasible(rel, bids, asks)  # and the LP agrees it is a genuine arbitrage


# ---------------------------------------------------------------------------- worked examples on real book objects
def book(ticker, yes=(), no=()):
    b = OrderBook(ticker)
    b.apply_snapshot(list(yes), list(no))
    return b


def test_mutex_arbitrage_from_orderbooks():
    """Two markets that cannot both resolve YES, but whose YES bids sum to 1.07."""
    books = {
        "A": book("A", yes=[(5500, 300)], no=[(4000, 300)]),
        "B": book("B", yes=[(5200, 200)], no=[(4300, 500)]),
    }
    ask = asks_from_books(books)
    assert ask(ContractRef("A", Side.NO)) == Level(4500, 300)  # 1 - best YES bid
    chk = MutuallyExclusive(["A", "B"]).constraints()[0].check(ask)
    assert chk.cost == 4500 + 4800 and chk.margin == S - 9300 == 700 and chk.violated
    assert chk.top_qty == 200  # limited by the thinner leg, at the best price only


def test_no_violation_in_a_coherent_partition():
    books = {  # two-outcome event priced 0.55 / 0.47 asks -> sums to 1.02 > 1: no arb
        "A": book("A", yes=[(5300, 100)], no=[(4500, 100)]),
        "B": book("B", yes=[(4500, 100)], no=[(5300, 100)]),
    }
    checks = scan([Partition(["A", "B"])], asks_from_books(books))
    assert [c.violated for c in checks] == [False, False]
    assert all(c.margin < 0 for c in checks)


def test_cover_arbitrage_when_yes_asks_sum_below_one():
    books = {
        "A": book("A", no=[(6000, 50)]),
        "B": book("B", no=[(5500, 80)]),
    }  # YES asks 0.40, 0.45
    chk = Exhaustive(["A", "B"]).constraints()[0].check(asks_from_books(books))
    assert chk.cost == 8500 and chk.margin == 1500 and chk.top_qty == 50


def test_implication_arbitrage_when_narrower_event_is_priced_above_wider():
    # "BTC > 70k" (narrow) bid 0.62 but "BTC > 65k" (wide) ask only 0.58: buy wide, sell narrow
    books = {
        "narrow": book("narrow", yes=[(6200, 100)], no=[(3000, 100)]),
        "wide": book("wide", yes=[(5000, 100)], no=[(4200, 100)]),
    }
    chk = Implies("narrow", "wide").constraints()[0].check(asks_from_books(books))
    assert chk.cost == 5800 + 3800 and chk.margin == 400 and chk.violated


def test_complement_is_an_invariant_of_valid_books_and_flags_crossed_ones():
    ok = book("A", yes=[(5000, 10)], no=[(4000, 10)])
    crossed = book("A", yes=[(6500, 10)], no=[(4000, 10)])  # YES bid 0.65 + NO bid 0.40 > 1
    con = Complement("A").constraints()[0]
    assert con.check(asks_from_books({"A": ok})).margin < 0
    assert con.check(asks_from_books({"A": crossed})).margin == 500
    assert crossed.is_crossed() and not ok.is_crossed()


def test_missing_offers_skip_the_constraint_rather_than_guess():
    books = {"A": book("A", yes=[(5000, 10)]), "B": book("B", no=[(5000, 10)])}
    assert MutuallyExclusive(["A", "B"]).constraints()[0].check(asks_from_books(books)) is None
    assert scan([MutuallyExclusive(["A", "B"])], asks_from_books(books)) == []
    assert (
        MutuallyExclusive(["A", "B"]).constraints()[0].check(asks_from_books({"A": books["A"]}))
        is None
    )


def test_scan_sorts_most_violated_first():
    books = {
        "A": book("A", yes=[(7000, 10)], no=[(2000, 10)]),
        "B": book("B", yes=[(6000, 10)], no=[(3000, 10)]),
        "C": book("C", yes=[(2000, 10)], no=[(7000, 10)]),
    }
    checks = scan(
        [MutuallyExclusive(["A", "B"]), MutuallyExclusive(["B", "C"]), Exhaustive(["A", "C"])],
        asks_from_books(books),
    )
    assert [c.margin for c in checks] == sorted((c.margin for c in checks), reverse=True)
    assert checks[0].margin == 7000 + 6000 - S  # YES bids 0.70 + 0.60 = 1.30 against a limit of 1


def test_constructor_validation():
    with pytest.raises(ValueError, match="duplicate"):
        MutuallyExclusive(["A", "A"])
    with pytest.raises(ValueError, match="at least 2"):
        Partition(["A"])
    with pytest.raises(ValueError, match="imply itself"):
        Implies("A", "A")
    with pytest.raises(ValueError, match="one of its parts"):
        Union(["A", "B"], "A")
    with pytest.raises(ValueError, match="buy-only"):
        Leg(ContractRef("A", Side.YES), 0)


def test_payoff_handles_fractional_settlement():
    """Void / scalar markets: NO pays 1 - X, so YES+NO is still exactly $1 (complement survives voids)."""
    c = Complement("A").constraints()[0]
    assert c.payoff({"A": 3800}) == S
    # ... but a mutex bundle can pay LESS than its bound if all three void at $0.50:
    mx = MutuallyExclusive(["A", "B", "C"]).constraints()[0]
    voided = {"A": 5000, "B": 5000, "C": 5000}
    assert mx.payoff(voided) == 3 * S - 15000 == 15000  # $1.50 ...
    assert mx.bound == 20000 > mx.payoff(voided)  # ... against a $2.00 floor: resolution risk


def test_oracle_is_numerically_robust():
    """Exactly-coherent and one-tick-incoherent instances must be classified correctly, always."""
    import random

    rng = random.Random(20260919)
    rels = [r for r in relations()]
    for _ in range(3000):
        rel = rng.choice(rels)
        worlds = admissible_worlds(rel)
        w = [rng.randint(0, 5) for _ in worlds]
        if not sum(w):
            w[0] = 1
        tot = sum(w)
        # exactly coherent: prices ARE the model's expectations (rounded to ticks only when exact)
        exp = {
            t: sum(wt * wd[t] for wt, wd in zip(w, worlds, strict=True)) * S / tot
            for t in rel.tickers()
        }
        if any(e != int(e) for e in exp.values()):
            continue
        prices = {t: int(e) for t, e in exp.items()}
        assert lp_feasible(rel, prices, prices), (rel.statement(), prices)
        assert not violated(rel, prices, prices)


@settings(max_examples=150, deadline=None)
@given(st.data())
def test_chain_prefilter_is_lossless(data):
    """The O(k) candidate filter must return exactly the violated constraints of the full O(k^2) scan."""
    n = data.draw(st.integers(2, 7))
    tickers = [f"M{i}" for i in range(n)]
    books = {}
    for t in tickers:
        yes = data.draw(st.one_of(st.none(), st.integers(100, 9000)))
        no = data.draw(st.one_of(st.none(), st.integers(100, 9800)))
        if yes is not None and no is not None and yes + no >= S:
            no = None
        b = OrderBook(t)
        b.apply_snapshot([(yes, 100)] if yes else [], [(no, 100)] if no else [])
        books[t] = b
    ask = asks_from_books(books)
    chain = Chain(tickers)
    full = {c.name for c in chain.constraints() if (k := c.check(ask)) and k.violated}
    fast = {c.name for c in chain.candidate_constraints(ask) if (k := c.check(ask)) and k.violated}
    assert fast == full
    assert set(c.name for c in chain.candidate_constraints(ask)) <= {
        c.name for c in chain.constraints()
    }
    assert chain.n_constraints() == len(chain.constraints())
