from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from arbitrage.execution import Fill, depth, max_bundles, optimize, price_bundle, walk_asks
from market.contracts import ContractRef, Side
from market.fees import FeeBook, FeeSchedule, Rounding
from market.order_book import OrderBook
from market.relationships import MutuallyExclusive, Partition
from market.units import PRICE_SCALE as S

FEES = FeeBook({}, FeeSchedule())  # standard quadratic taker fee for every series


def book(ticker, yes=(), no=()):
    b = OrderBook(ticker)
    b.apply_snapshot(list(yes), list(no))
    return b


def c(n):  # whole contracts -> centi-contracts
    return n * 100


def test_asks_are_walked_best_first_from_the_opposite_bids():
    b = book(
        "A", no=[(6000, c(3)), (5500, c(2))]
    )  # NO bids 0.60x3, 0.55x2 == YES asks 0.40x3, 0.45x2
    fills, unfilled = walk_asks(b, Side.YES, c(4))
    assert fills == [Fill(4000, c(3)), Fill(4500, c(1))] and unfilled == 0
    fills, unfilled = walk_asks(b, Side.YES, c(9))
    assert sum(f.qty for f in fills) == c(5) and unfilled == c(4)  # book exhausted
    assert depth(b, Side.YES) == c(5) and depth(b, Side.NO) == 0
    assert walk_asks(OrderBook("E"), Side.YES, c(1)) == ([], c(1))


def test_priced_bundle_exact_accounting_on_a_mutex_arbitrage():
    """Two outcomes that cannot both happen, YES bids 0.55 and 0.52 (NO asks 0.45, 0.48)."""
    books = {"A": book("A", yes=[(5500, c(500))]), "B": book("B", yes=[(5200, c(500))])}
    con = MutuallyExclusive(["A", "B"]).constraints()[0]
    pb = price_bundle(con, books, c(100), FEES)  # 100 bundles
    assert (
        pb.payoff_micro == S * c(100) == 10_000 * 10_000
    )  # $100? no: bound 1.00 x 100 bundles = $100
    assert pb.cost_micro == (4500 + 4800) * c(100)  # $93.00
    assert pb.gross_micro == 7_000_000  # $7.00 of edge before costs
    fee_a = 1_732_500  # 0.07 * 100 * 0.45 * 0.55
    fee_b = 1_747_200  # 0.07 * 100 * 0.48 * 0.52
    assert pb.fee_micro == fee_a + fee_b
    assert pb.slippage_micro == 0  # everything filled at the best prices
    assert pb.net_micro == 7_000_000 - fee_a - fee_b == 3_520_300  # $3.52 net


def test_bundle_is_none_when_any_leg_lacks_depth_or_a_book():
    con = MutuallyExclusive(["A", "B"]).constraints()[0]
    thin = {"A": book("A", yes=[(5500, c(10))]), "B": book("B", yes=[(5200, c(500))])}
    assert price_bundle(con, thin, c(11), FEES) is None
    assert price_bundle(con, thin, c(10), FEES) is not None
    assert price_bundle(con, {"A": thin["A"]}, c(1), FEES) is None
    assert max_bundles(con, thin) == c(10) and max_bundles(con, {"A": thin["A"]}) == 0


def test_slippage_and_the_net_identity_when_the_book_must_be_walked():
    # YES bid ladders: A: 0.55 x 10 then 0.50 x 100 ; B: 0.52 x 200. Selling into thicker levels costs more.
    books = {
        "A": book("A", yes=[(5500, c(10)), (5000, c(100))]),
        "B": book("B", yes=[(5200, c(200))]),
    }
    con = MutuallyExclusive(["A", "B"]).constraints()[0]
    pb = price_bundle(con, books, c(50), FEES)
    assert pb.slippage_micro > 0  # 40 of A's contracts are bought at NO ask 0.50, not 0.45
    assert pb.net_micro == pb.gross_micro - pb.slippage_micro - pb.fee_micro  # exact identity
    leg_a = next(leg for leg in pb.legs if leg.contract == ContractRef("A", Side.NO))
    assert leg_a.vwap_ticks == Fraction(4500 * c(10) + 5000 * c(40), c(50))
    assert leg_a.top_price == 4500 and len(leg_a.fills) == 2


def test_optimize_stops_at_the_last_profitable_size():
    # margin per bundle at level 1 = 7c (net of ~3.5c fees), at level 2 = 2c (< fees): stop after level 1
    books = {
        "A": book("A", yes=[(5500, c(30)), (5000, c(500))]),
        "B": book("B", yes=[(5200, c(500))]),
    }
    con = MutuallyExclusive(["A", "B"]).constraints()[0]
    r = optimize(con, books, FEES, lot=100)
    assert r.best.bundles == c(30)
    assert (
        r.breakeven.bundles > r.best.bundles
    )  # total profit stays positive long after marginal contracts lose
    assert r.best.net_micro > 0
    deeper = price_bundle(con, books, c(31), FEES)
    assert deeper.net_micro < r.best.net_micro  # buying one more bundle loses money


def test_optimize_none_when_less_than_a_lot_can_be_filled():
    books = {"A": book("A", yes=[(5500, 50)]), "B": book("B", yes=[(5200, c(9))])}
    assert optimize(MutuallyExclusive(["A", "B"]).constraints()[0], books, FEES) is None


@settings(max_examples=120, deadline=None)
@given(st.data())
def test_optimizer_matches_brute_force_over_every_size(data):
    """The profit is only piecewise-linear and the fee is non-monotone in price, so the size search
    is checked against exhaustively pricing every whole-contract size."""
    n = data.draw(st.integers(2, 3))
    books = {}
    for i in range(n):
        levels = []
        price = data.draw(st.integers(3000, 9000))
        for _ in range(data.draw(st.integers(1, 3))):
            levels.append((price, c(data.draw(st.integers(1, 12)))))
            price = max(100, price - data.draw(st.integers(100, 1500)))
        books[f"M{i}"] = book(f"M{i}", yes=levels)
    con = data.draw(
        st.sampled_from([MutuallyExclusive(list(books)), Partition(list(books))])
    ).constraints()[0]
    rounding = data.draw(st.sampled_from(list(Rounding)))
    fees = FEES.with_rounding(rounding)
    r = optimize(con, books, fees, lot=100)
    deepest = max_bundles(con, books) // 100
    brute = {
        k: p
        for k in range(1, deepest + 1)
        if (p := price_bundle(con, books, c(k), fees)) is not None
    }
    if r is None:
        assert not brute
        return
    best_net = max(p.net_micro for p in brute.values())
    # Guaranteed accuracy of the boundary search.  EXACT_6DP fees are smooth (<= 1 µ$ of rounding noise
    # per fill).  Whole-cent rounding makes the fee a sawtooth whose local optima sit just below each
    # cent step rather than at book-level boundaries, so the optimum can move by up to a cent per fill.
    noise = (10 if rounding is Rounding.EXACT_6DP else 10_000) * len(con.legs)
    assert best_net - r.best.net_micro <= noise, (rounding, best_net, r.best.net_micro)
    clear = [k for k, p in brute.items() if p.net_micro > 5 * noise]  # comfortably profitable sizes
    if clear:
        assert r.breakeven.bundles >= c(max(clear))
