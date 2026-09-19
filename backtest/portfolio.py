"""Exchange-faithful portfolio accounting, in exact integer µ$.

Orders are always *buys* of a side (selling YES is buying NO), so inventory is two counters per
market, ``yes`` and ``no`` (centi-contracts), each with a cost.  Kalshi nets a YES and a NO on
the same market into $1 of cash, and so does this ledger: holding both is never stored.

    buy YES @p / buy NO @q      cash -= price x qty (+ fee)
    net pair (YES + NO)         cash += $1 x pairs;    realised = $1 - cost_yes - cost_no  per pair
    settle at value X           YES pays X, NO pays $1 - X;  realised = payoff - cost

All arithmetic is integer µ$ (price ticks x centi-contracts), so the conservation identity

    equity = initial_cash + sum(realised) + sum(unrealised) - fees

holds *exactly* and is property-tested against an independent recomputation.

Marks are **liquidation values** (what the position would fetch right now): a YES position is
marked at the best YES bid, a NO position at the best NO bid; with no bid, at cost.  Marking at a
mid would book profit that could not actually be realised.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.contracts import Side
from market.units import PRICE_SCALE, Price, Qty


@dataclass(slots=True)
class Position:
    yes: Qty = 0
    yes_cost: int = 0  # µ$ paid for the YES inventory
    no: Qty = 0
    no_cost: int = 0

    @property
    def net_yes(self) -> Qty:
        """Signed exposure to YES: long YES positive, long NO negative."""
        return self.yes - self.no

    @property
    def flat(self) -> bool:
        return self.yes == 0 and self.no == 0


@dataclass
class Portfolio:
    cash: int  # µ$
    positions: dict[str, Position] = field(default_factory=dict)
    realised: int = 0
    fees: int = 0
    settled: dict[str, Price] = field(default_factory=dict)

    def position(self, ticker: str) -> Position:
        return self.positions.setdefault(ticker, Position())

    # ------------------------------------------------------------------ trading
    def buy(self, ticker: str, side: Side, price: Price, qty: Qty, fee: int) -> None:
        if ticker in self.settled:
            raise ValueError(f"{ticker} already settled")
        pos = self.position(ticker)
        cost = price * qty
        self.cash -= cost + fee
        self.fees += fee
        if side is Side.YES:
            pos.yes += qty
            pos.yes_cost += cost
        else:
            pos.no += qty
            pos.no_cost += cost
        self._net(pos)

    def _net(self, pos: Position) -> None:
        pairs = min(pos.yes, pos.no)
        if not pairs:
            return
        cost_yes = pos.yes_cost * pairs // pos.yes if pairs < pos.yes else pos.yes_cost
        cost_no = pos.no_cost * pairs // pos.no if pairs < pos.no else pos.no_cost
        self.cash += PRICE_SCALE * pairs
        self.realised += PRICE_SCALE * pairs - cost_yes - cost_no
        pos.yes -= pairs
        pos.no -= pairs
        pos.yes_cost -= cost_yes
        pos.no_cost -= cost_no

    def settle(self, ticker: str, value: Price) -> int:
        """Pay out the position at YES-value ``value`` (ticks); returns the realised P&L."""
        pos = self.positions.get(ticker)
        self.settled[ticker] = value
        if pos is None or pos.flat:
            return 0
        payoff = value * pos.yes + (PRICE_SCALE - value) * pos.no
        pnl = payoff - pos.yes_cost - pos.no_cost
        self.cash += payoff
        self.realised += pnl
        pos.yes = pos.no = pos.yes_cost = pos.no_cost = 0
        return pnl

    # ------------------------------------------------------------------ valuation
    def unrealised(self, marks: dict[str, tuple[Price | None, Price | None]]) -> int:
        """``marks[ticker] = (yes_bid, no_bid)`` liquidation prices; None -> value at cost."""
        total = 0
        for t, pos in self.positions.items():
            if pos.flat:
                continue
            yb, nb = marks.get(t, (None, None))
            yes_val = pos.yes_cost if yb is None else yb * pos.yes
            no_val = pos.no_cost if nb is None else nb * pos.no
            total += (yes_val - pos.yes_cost) + (no_val - pos.no_cost)
        return total

    def equity(self, marks: dict[str, tuple[Price | None, Price | None]]) -> int:
        holdings = 0
        for t, pos in self.positions.items():
            if pos.flat:
                continue
            yb, nb = marks.get(t, (None, None))
            holdings += pos.yes_cost if yb is None else yb * pos.yes
            holdings += pos.no_cost if nb is None else nb * pos.no
        return self.cash + holdings

    def max_loss(self) -> int:
        """Worst-case settlement loss of the open inventory: everything held pays zero."""
        return sum(p.yes_cost + p.no_cost for p in self.positions.values())
