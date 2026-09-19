"""Logical relationships between markets, compiled to executable no-arbitrage constraints.

The order of reasoning is deliberate - **mathematics first, prices second**:

1. A *relation* states which combinations of outcomes are possible ("A implies B",
   "at most one of these happens", ...).  A **world** assigns each market YES (1) or NO (0);
   the relation defines its set of admissible worlds.
2. Every relation is *compiled* into constraints of one form only::

       buying ``qty_i`` of each leg ``i`` pays at least ``bound`` in every admissible world.

   Such a constraint is a guaranteed floor on a **buy-only portfolio**.  Buy-only matches the
   exchange: there is no naked shorting, and "short YES" *is* "buy NO" - so the legs are
   ``ContractRef``s (market, side) and never need a negative quantity.
3. Only then are prices consulted.  If the portfolio costs less than its guaranteed floor,

       margin = bound - cost > 0

   is a riskless profit per bundle (before fees, depth and latency - Stage 4).

Correctness of the compilation is *tested against the definitions*, not assumed:
every admissible world satisfies every constraint (soundness); every inadmissible world violates
at least one (the constraints characterise the relation); each bound is attained (tightness); and
an LP oracle confirms that "no constraint violated" is exactly "some admissible probability
distribution fits inside the quoted bid/ask" (no arbitrage is missed).

Semantic assumptions (stated once, apply to every relation here)
  * **Normal resolution.**  Constraints hold when every market settles according to its rules.
    Cancellation / void markets can settle at fractional values (observed live: ``scalar``
    results; a table-tennis rule voids at $0.50) and can break a relation - e.g. three mutually
    exclusive outcomes all voided at $0.50 sum to 1.5.  This is *resolution risk*, not modelled
    here; ``docs/relationships.md`` quantifies it empirically.
  * **Same information time.**  Two markets are only related if they settle on the same
    variable at the same instant; ``market.semantics`` verifies that from rules text.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from market.contracts import ContractRef, Side
from market.order_book import Level, OrderBook
from market.units import PRICE_SCALE, Price, Qty

World = Mapping[str, int]  # ticker -> 1 (YES) / 0 (NO)


class RelationKind(StrEnum):
    COMPLEMENT = "complement"
    MUTUALLY_EXCLUSIVE = "mutually_exclusive"
    EXHAUSTIVE = "exhaustive"
    PARTITION = "partition"
    IMPLIES = "implies"
    CHAIN = "chain"
    UNION = "union"


# ----------------------------------------------------------------------------- constraints
@dataclass(frozen=True, slots=True)
class Leg:
    contract: ContractRef
    qty: int = 1  # whole contracts per bundle

    def __post_init__(self) -> None:
        if self.qty < 1:
            raise ValueError("legs are buy-only: qty must be >= 1")


AskFn = Callable[[ContractRef], Level | None]  # best ask (ticks, centi-contracts) for a contract


@dataclass(frozen=True, slots=True)
class Constraint:
    """One *bundle* (``qty`` of each leg) pays >= ``bound`` ticks in every admissible world."""

    name: str
    kind: RelationKind
    legs: tuple[Leg, ...]
    bound: Price

    def payoff(self, settlement: Mapping[str, Price]) -> int:
        """Bundle payoff in ticks given each market's YES settlement value (0..PRICE_SCALE)."""
        total = 0
        for leg in self.legs:
            yes = settlement[leg.contract.ticker]
            total += leg.qty * (yes if leg.contract.side is Side.YES else PRICE_SCALE - yes)
        return total

    def world_payoff(self, world: World) -> int:
        return self.payoff({t: v * PRICE_SCALE for t, v in world.items()})

    def check(self, ask: AskFn) -> ConstraintCheck | None:
        """Price the bundle at the best asks.  ``None`` if any leg has no offer to buy."""
        prices: list[Level] = []
        for leg in self.legs:
            lv = ask(leg.contract)
            if lv is None:
                return None
            prices.append(lv)
        cost = sum(leg.qty * lv.price for leg, lv in zip(self.legs, prices, strict=True))
        top = min(lv.qty // leg.qty for leg, lv in zip(self.legs, prices, strict=True))
        return ConstraintCheck(self, cost, self.bound - cost, top)


@dataclass(frozen=True, slots=True)
class ConstraintCheck:
    """A constraint priced at the top of book.  ``margin > 0`` means the constraint is violated.

    ``top_qty`` is the number of bundles (in centi-contracts) available *at the best prices only*;
    it is an upper bound on executable size, not an estimate of it (depth is Stage 4)."""

    constraint: Constraint
    cost: Price
    margin: Price
    top_qty: Qty

    @property
    def violated(self) -> bool:
        return self.margin > 0


def asks_from_books(books: Mapping[str, OrderBook]) -> AskFn:
    def ask(c: ContractRef) -> Level | None:
        book = books.get(c.ticker)
        return None if book is None else book.best_ask(c.side)

    return ask


# ----------------------------------------------------------------------------- relations
class Relation(Protocol):
    kind: RelationKind

    def tickers(self) -> tuple[str, ...]: ...
    def constraints(self) -> tuple[Constraint, ...]: ...
    def admissible(self, world: World) -> bool: ...
    def statement(self) -> str: ...


def _yes(t: str, q: int = 1) -> Leg:
    return Leg(ContractRef(t, Side.YES), q)


def _no(t: str, q: int = 1) -> Leg:
    return Leg(ContractRef(t, Side.NO), q)


def _distinct(tickers: Sequence[str], minimum: int, what: str) -> tuple[str, ...]:
    ts = tuple(tickers)
    if len(set(ts)) != len(ts):
        raise ValueError(f"{what}: duplicate markets")
    if len(ts) < minimum:
        raise ValueError(f"{what}: needs at least {minimum} markets")
    return ts


@dataclass(frozen=True)
class Complement:
    """YES and NO of one market pay exactly $1 in total.

    On Kalshi this can never be violated by a *valid* book: asks are derived from the opposite
    bids (``ask_YES = 1 - bid_NO``), so ``ask_YES + ask_NO = 2 - bid_YES - bid_NO >= 1`` unless the
    book is crossed.  It is kept as the invariant that detects corrupted / crossed books."""

    ticker: str
    kind = RelationKind.COMPLEMENT

    def tickers(self) -> tuple[str, ...]:
        return (self.ticker,)

    def constraints(self) -> tuple[Constraint, ...]:
        return (
            Constraint(
                f"complement({self.ticker})",
                self.kind,
                (_yes(self.ticker), _no(self.ticker)),
                PRICE_SCALE,
            ),
        )

    def admissible(self, world: World) -> bool:
        return True

    def statement(self) -> str:
        return f"X_YES + X_NO = 1 for {self.ticker}"


@dataclass(frozen=True)
class MutuallyExclusive:
    """At most one of the markets resolves YES:  sum X_i <= 1."""

    markets: tuple[str, ...]
    kind = RelationKind.MUTUALLY_EXCLUSIVE

    def __init__(self, markets: Sequence[str]) -> None:
        object.__setattr__(self, "markets", _distinct(markets, 2, "MutuallyExclusive"))

    def tickers(self) -> tuple[str, ...]:
        return self.markets

    def constraints(self) -> tuple[Constraint, ...]:
        n = len(self.markets)
        # sum X <= 1  <=>  sum (1 - X) >= n - 1  <=>  buying every NO pays >= (n-1)
        return (
            Constraint(
                f"mutex({n})", self.kind, tuple(_no(t) for t in self.markets), (n - 1) * PRICE_SCALE
            ),
        )

    def admissible(self, world: World) -> bool:
        return sum(world[t] for t in self.markets) <= 1

    def statement(self) -> str:
        return "sum X_i <= 1 over " + ", ".join(self.markets)


@dataclass(frozen=True)
class Exhaustive:
    """At least one of the markets resolves YES:  sum X_i >= 1."""

    markets: tuple[str, ...]
    kind = RelationKind.EXHAUSTIVE

    def __init__(self, markets: Sequence[str]) -> None:
        object.__setattr__(self, "markets", _distinct(markets, 2, "Exhaustive"))

    def tickers(self) -> tuple[str, ...]:
        return self.markets

    def constraints(self) -> tuple[Constraint, ...]:
        return (
            Constraint(
                f"exhaustive({len(self.markets)})",
                self.kind,
                tuple(_yes(t) for t in self.markets),
                PRICE_SCALE,
            ),
        )

    def admissible(self, world: World) -> bool:
        return sum(world[t] for t in self.markets) >= 1

    def statement(self) -> str:
        return "sum X_i >= 1 over " + ", ".join(self.markets)


@dataclass(frozen=True)
class Partition:
    """Exactly one resolves YES: mutually exclusive AND exhaustive.  sum X_i = 1."""

    markets: tuple[str, ...]
    kind = RelationKind.PARTITION

    def __init__(self, markets: Sequence[str]) -> None:
        object.__setattr__(self, "markets", _distinct(markets, 2, "Partition"))

    def tickers(self) -> tuple[str, ...]:
        return self.markets

    def constraints(self) -> tuple[Constraint, ...]:
        mutex, cover = MutuallyExclusive(self.markets), Exhaustive(self.markets)
        return mutex.constraints() + cover.constraints()

    def admissible(self, world: World) -> bool:
        return sum(world[t] for t in self.markets) == 1

    def statement(self) -> str:
        return "sum X_i = 1 over " + ", ".join(self.markets)


@dataclass(frozen=True)
class Implies:
    """``antecedent`` YES forces ``consequent`` YES (A is a subset of B):  X_A <= X_B."""

    antecedent: str
    consequent: str
    kind = RelationKind.IMPLIES

    def __post_init__(self) -> None:
        if self.antecedent == self.consequent:
            raise ValueError("Implies: a market cannot imply itself")

    def tickers(self) -> tuple[str, ...]:
        return (self.antecedent, self.consequent)

    def constraints(self) -> tuple[Constraint, ...]:
        # X_A <= X_B  <=>  X_B + (1 - X_A) >= 1
        return (
            Constraint(
                f"{self.antecedent}=>{self.consequent}",
                self.kind,
                (_yes(self.consequent), _no(self.antecedent)),
                PRICE_SCALE,
            ),
        )

    def admissible(self, world: World) -> bool:
        return not (world[self.antecedent] == 1 and world[self.consequent] == 0)

    def statement(self) -> str:
        return f"X[{self.antecedent}] <= X[{self.consequent}]"


@dataclass(frozen=True)
class Chain:
    """Nested events ``m_0 ⊆ m_1 ⊆ ... ⊆ m_k`` (narrowest first): each implies all later ones.

    All O(k^2) pairs are emitted, not just neighbours: with bid/ask spreads, "adjacent pairs
    consistent" does *not* imply "non-adjacent pairs consistent" (bid_0 <= ask_1 and bid_1 <= ask_2
    still allows bid_0 > ask_2).  The LP oracle test catches an adjacent-only implementation."""

    markets: tuple[str, ...]
    kind = RelationKind.CHAIN

    def __init__(self, markets: Sequence[str]) -> None:
        object.__setattr__(self, "markets", _distinct(markets, 2, "Chain"))

    def tickers(self) -> tuple[str, ...]:
        return self.markets

    def constraints(self) -> tuple[Constraint, ...]:
        return tuple(
            c
            for a, b in itertools.combinations(self.markets, 2)
            for c in Implies(a, b).constraints()
        )

    def admissible(self, world: World) -> bool:
        vals = [world[t] for t in self.markets]
        return all(vals[i] <= vals[i + 1] for i in range(len(vals) - 1))

    def statement(self) -> str:
        return " <= ".join(f"X[{t}]" for t in self.markets)


@dataclass(frozen=True)
class Union:
    """``whole`` is YES exactly when some ``part`` is YES.  With ``disjoint`` the parts are also
    mutually exclusive, giving the additive identity ``X_whole = sum X_part``."""

    parts: tuple[str, ...]
    whole: str
    disjoint: bool = True
    kind = RelationKind.UNION

    def __init__(self, parts: Sequence[str], whole: str, disjoint: bool = True) -> None:
        ps = _distinct(parts, 2, "Union")
        if whole in ps:
            raise ValueError("Union: the whole cannot be one of its parts")
        object.__setattr__(self, "parts", ps)
        object.__setattr__(self, "whole", whole)
        object.__setattr__(self, "disjoint", disjoint)

    def tickers(self) -> tuple[str, ...]:
        return (*self.parts, self.whole)

    def constraints(self) -> tuple[Constraint, ...]:
        n = len(self.parts)
        cover = Constraint(  # X_W <= sum X_p   <=>   (1 - X_W) + sum X_p >= 1
            f"union-cover({self.whole})",
            self.kind,
            (_no(self.whole), *(_yes(p) for p in self.parts)),
            PRICE_SCALE,
        )
        if not self.disjoint:
            return (cover, *(c for p in self.parts for c in Implies(p, self.whole).constraints()))
        additive = Constraint(  # X_W >= sum X_p   <=>   X_W + sum (1 - X_p) >= n
            f"union-sum({self.whole})",
            self.kind,
            (_yes(self.whole), *(_no(p) for p in self.parts)),
            n * PRICE_SCALE,
        )
        return (cover, additive, *MutuallyExclusive(self.parts).constraints())

    def admissible(self, world: World) -> bool:
        anyp = any(world[p] for p in self.parts)
        if world[self.whole] != int(anyp):
            return False
        return not self.disjoint or sum(world[p] for p in self.parts) <= 1

    def statement(self) -> str:
        op = "+" if self.disjoint else "∨"
        return f"X[{self.whole}] = " + f" {op} ".join(f"X[{p}]" for p in self.parts)


# ----------------------------------------------------------------------------- utilities
def all_worlds(tickers: Sequence[str]) -> Iterator[dict[str, int]]:
    for bits in itertools.product((0, 1), repeat=len(tickers)):
        yield dict(zip(tickers, bits, strict=True))


def admissible_worlds(relation: Relation) -> list[dict[str, int]]:
    return [w for w in all_worlds(relation.tickers()) if relation.admissible(w)]


def scan(relations: Iterable[Relation], ask: AskFn) -> list[ConstraintCheck]:
    """Price every constraint of every relation; returns checks sorted by margin, best first.

    Constraints with a leg that has nothing to buy are skipped (they cannot be traded)."""
    checks = [c for r in relations for con in r.constraints() if (c := con.check(ask)) is not None]
    return sorted(checks, key=lambda c: -c.margin)
