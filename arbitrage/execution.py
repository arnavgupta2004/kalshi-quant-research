"""Turning a violated constraint into an executable trade: walk the book, add fees, find the size.

Everything is integer µ$ (see ``market.fees``), so the accounting identity

    net = payoff - vwap_cost - fees
        = gross_edge_at_top_of_book - slippage - fees

holds *exactly*, and the tests check it.

Execution assumptions (stated, not hidden)
  * **Atomic, simultaneous** execution of every leg against the book as observed: no leg risk, no
    latency.  Both are Stage 6 (latency) - here they would only add noise to what the *static*
    structure can offer.
  * **Taker only**: every leg lifts the best asks; we never rest orders.
  * **Whole contracts** (``lot``): sizes are multiples of ``lot`` centi-contracts.
  * Books are consumed independently per leg (no self-impact between legs on the same book;
    a bundle never trades the same contract twice).
  * Fees per fill; no fee at settlement.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction

from market.contracts import ContractRef, Side
from market.fees import FeeBook
from market.order_book import OrderBook
from market.relationships import Constraint
from market.units import Price, Qty

Books = Mapping[str, OrderBook]


@dataclass(frozen=True, slots=True)
class Fill:
    price: Price
    qty: Qty


def walk_asks(book: OrderBook, side: Side, qty: Qty) -> tuple[list[Fill], Qty]:
    """Buy ``qty`` of ``side`` from the derived asks, best price first.

    Returns ``(fills, unfilled)``; ``unfilled > 0`` means the book was too thin."""
    fills: list[Fill] = []
    remaining = qty
    for level in book.asks(side):
        if remaining <= 0:
            break
        take = min(remaining, level.qty)
        fills.append(Fill(level.price, take))
        remaining -= take
    return fills, max(remaining, 0)


def depth(book: OrderBook, side: Side) -> Qty:
    """Total quantity available to buy on ``side``."""
    return sum(lv.qty for lv in book.asks(side))


@dataclass(frozen=True, slots=True)
class LegExecution:
    contract: ContractRef
    qty: Qty  # centi-contracts bought
    fills: tuple[Fill, ...]
    cost_micro: int
    fee_micro: int
    top_price: Price  # best ask at observation time

    @property
    def vwap_ticks(self) -> Fraction:
        return Fraction(self.cost_micro, self.qty) if self.qty else Fraction(0)

    @property
    def top_cost_micro(self) -> int:
        return self.top_price * self.qty


@dataclass(frozen=True, slots=True)
class PricedBundle:
    """``bundles`` (centi-bundles) of a constraint, priced against the books."""

    constraint: Constraint
    bundles: Qty
    legs: tuple[LegExecution, ...]

    @property
    def payoff_micro(self) -> int:
        """Guaranteed floor: ``bound`` ticks per contract-bundle x centi-bundles = µ$."""
        return self.constraint.bound * self.bundles

    @property
    def cost_micro(self) -> int:
        return sum(leg.cost_micro for leg in self.legs)

    @property
    def fee_micro(self) -> int:
        return sum(leg.fee_micro for leg in self.legs)

    @property
    def top_cost_micro(self) -> int:
        return sum(leg.top_cost_micro for leg in self.legs)

    @property
    def gross_micro(self) -> int:
        """Edge if every contract could be bought at the best displayed price."""
        return self.payoff_micro - self.top_cost_micro

    @property
    def slippage_micro(self) -> int:
        """Extra cost of walking past the best price."""
        return self.cost_micro - self.top_cost_micro

    @property
    def net_micro(self) -> int:
        return self.payoff_micro - self.cost_micro - self.fee_micro


def price_bundle(
    constraint: Constraint, books: Books, bundles: Qty, fees: FeeBook
) -> PricedBundle | None:
    """Price ``bundles`` centi-bundles exactly.  ``None`` if any leg lacks the depth."""
    legs: list[LegExecution] = []
    for leg in constraint.legs:
        book = books.get(leg.contract.ticker)
        if book is None:
            return None
        qty = leg.qty * bundles
        fills, unfilled = walk_asks(book, leg.contract.side, qty)
        if unfilled or not fills:
            return None
        legs.append(
            LegExecution(
                contract=leg.contract,
                qty=qty,
                fills=tuple(fills),
                cost_micro=sum(f.price * f.qty for f in fills),
                fee_micro=fees.for_ticker(leg.contract.ticker).order_fee_micro(
                    [(f.price, f.qty) for f in fills]
                ),
                top_price=fills[0].price,
            )
        )
    return PricedBundle(constraint, bundles, tuple(legs))


def max_bundles(constraint: Constraint, books: Books) -> Qty:
    """Deepest bundle the books can fill at any price (centi-bundles)."""
    out: Qty | None = None
    for leg in constraint.legs:
        book = books.get(leg.contract.ticker)
        if book is None:
            return 0
        n = depth(book, leg.contract.side) // leg.qty
        out = n if out is None else min(out, n)
    return out or 0


@dataclass(frozen=True, slots=True)
class SizeResult:
    best: PricedBundle  # size maximising net profit (ties -> smaller): what a trader would send
    breakeven: PricedBundle  # largest size with *total* net > 0 (marginal contracts lose past best)


def _candidate_sizes(constraint: Constraint, books: Books, lot: Qty, cap: Qty) -> list[Qty]:
    """Sizes worth evaluating: net profit is piecewise linear in size between price-level
    boundaries (up to fee rounding), so its maximum sits at a boundary - and because the quadratic
    fee is *not* monotone in price the profit is not concave, so every boundary is checked rather
    than stopping at the first unprofitable step."""
    bounds: set[Qty] = {lot, cap}
    for leg in constraint.legs:
        cum = 0
        for level in books[leg.contract.ticker].asks(leg.contract.side):
            cum += level.qty
            bounds.add(cum // leg.qty)
    sizes: set[Qty] = set()
    for b in bounds:
        b = min(b, cap)
        down = (b // lot) * lot
        for s in (down, down + lot):
            if lot <= s <= cap:
                sizes.add(s)
    return sorted(sizes)


def optimize(
    constraint: Constraint,
    books: Books,
    fees: FeeBook,
    *,
    lot: Qty = 100,
    cap: Qty | None = None,
) -> SizeResult | None:
    """Find the profit-maximising size and the break-even size beyond which total profit is
    negative.  ``None`` if not even one lot can be filled."""
    deepest = (max_bundles(constraint, books) // lot) * lot
    if cap is not None:
        deepest = min(deepest, (cap // lot) * lot)
    if deepest < lot:
        return None
    priced = [
        p
        for s in _candidate_sizes(constraint, books, lot, deepest)
        if (p := price_bundle(constraint, books, s, fees)) is not None
    ]
    if not priced:
        return None
    best = max(priced, key=lambda p: (p.net_micro, -p.bundles))
    profitable = [p for p in priced if p.net_micro > 0]
    if not profitable:
        return SizeResult(best, best)
    last = max(profitable, key=lambda p: p.bundles)
    later = [p.bundles for p in priced if p.bundles > last.bundles]
    if (
        later
    ):  # total profit falls linearly inside a segment, so the zero crossing is between candidates
        lo, hi = last.bundles, min(later)
        while hi - lo > lot:
            mid = lo + ((hi - lo) // (2 * lot) or 1) * lot
            probe = price_bundle(constraint, books, mid, fees)
            if probe is not None and probe.net_micro > 0:
                lo, last = mid, probe
            else:
                hi = mid
    return SizeResult(best, last)
