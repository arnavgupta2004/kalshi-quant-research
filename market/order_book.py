"""Local limit-order-book for a binary market.

Kalshi publishes **bids only**, on both sides.  Because a YES contract and a NO contract
on the same market jointly pay exactly $1, resting liquidity has a dual reading:

    a NO  bid at price q  ==  a YES ask at price 1 - q     (same size)
    a YES bid at price p  ==  a NO  ask at price 1 - p     (same size)

so both asks are *derived*.  The book is stored as two ``{price_ticks: qty_centi}`` maps
and never trusts input ordering.  Mutations validate themselves and raise
``OrderBookInconsistency`` rather than clamping, so a lost delta is loud, not silent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from market.contracts import Side
from market.units import PRICE_SCALE, Price, Qty, complement


class OrderBookError(Exception):
    pass


class OrderBookInconsistency(OrderBookError):
    """The book was asked to do something no consistent exchange state could produce."""


@dataclass(frozen=True, slots=True)
class Level:
    price: Price
    qty: Qty


class OrderBook:
    __slots__ = ("_bids", "last_seq", "last_ts", "ticker")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self._bids: dict[Side, dict[Price, Qty]] = {Side.YES: {}, Side.NO: {}}
        self.last_seq: int | None = None
        self.last_ts: datetime | None = None

    # ------------------------------------------------------------------ mutation
    def apply_snapshot(
        self,
        yes: Iterable[tuple[Price, Qty]],
        no: Iterable[tuple[Price, Qty]],
    ) -> None:
        """Replace the whole book.  Atomic: on invalid input the old state is kept."""
        new = {Side.YES: self._validated(yes, Side.YES), Side.NO: self._validated(no, Side.NO)}
        self._bids = new

    def apply_delta(self, side: Side, price: Price, delta: Qty) -> None:
        """Add ``delta`` (may be negative) to the resting quantity at ``price``."""
        self._check_price(price, side)
        book = self._bids[side]
        new_qty = book.get(price, 0) + delta
        if new_qty < 0:
            raise OrderBookInconsistency(
                f"{self.ticker} {side.value}@{price}: delta {delta} drives qty "
                f"{book.get(price, 0)} below zero"
            )
        if new_qty == 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    def clear(self) -> None:
        self._bids = {Side.YES: {}, Side.NO: {}}
        self.last_seq = None
        self.last_ts = None

    @staticmethod
    def _check_price(price: Price, side: Side) -> None:
        if not 0 < price < PRICE_SCALE:
            raise OrderBookInconsistency(f"{side.value} bid price {price} outside (0, $1)")

    def _validated(self, levels: Iterable[tuple[Price, Qty]], side: Side) -> dict[Price, Qty]:
        out: dict[Price, Qty] = {}
        for price, qty in levels:
            self._check_price(price, side)
            if qty < 0:
                raise OrderBookInconsistency(f"{side.value}@{price}: negative quantity {qty}")
            if price in out:
                raise OrderBookInconsistency(f"{side.value}@{price}: duplicate level in snapshot")
            if qty:
                out[price] = qty
        return out

    # ------------------------------------------------------------------ reading
    def bids(self, side: Side) -> list[Level]:
        """Resting bids on ``side``, best (highest) first."""
        return [Level(p, q) for p, q in sorted(self._bids[side].items(), reverse=True)]

    def asks(self, side: Side) -> list[Level]:
        """Asks for ``side`` contracts, best (lowest) first - derived from the opposite bids."""
        opposite = self._bids[side.opposite]
        return [Level(complement(p), q) for p, q in sorted(opposite.items(), reverse=True)]

    def best_bid(self, side: Side = Side.YES) -> Level | None:
        book = self._bids[side]
        if not book:
            return None
        p = max(book)
        return Level(p, book[p])

    def best_ask(self, side: Side = Side.YES) -> Level | None:
        opp = self.best_bid(side.opposite)
        return None if opp is None else Level(complement(opp.price), opp.qty)

    def depth(self, side: Side) -> Qty:
        """Total resting bid quantity on ``side``."""
        return sum(self._bids[side].values())

    @property
    def is_empty(self) -> bool:
        return not self._bids[Side.YES] and not self._bids[Side.NO]

    def spread(self) -> Price | None:
        """YES bid-ask spread in ticks (== ``1 - best_yes_bid - best_no_bid``)."""
        bid, ask = self.best_bid(Side.YES), self.best_ask(Side.YES)
        return None if bid is None or ask is None else ask.price - bid.price

    def mid(self) -> float | None:
        """YES midpoint in dollars.  Analytics only: a mid is *not* a probability (see docs)."""
        bid, ask = self.best_bid(Side.YES), self.best_ask(Side.YES)
        if bid is None or ask is None:
            return None
        return (bid.price + ask.price) / (2 * PRICE_SCALE)

    def is_crossed(self) -> bool:
        """True if best YES bid >= best YES ask (locked or crossed) - impossible in a live book."""
        bid, ask = self.best_bid(Side.YES), self.best_ask(Side.YES)
        return bid is not None and ask is not None and bid.price >= ask.price

    # ------------------------------------------------------------------ utilities
    def copy(self) -> OrderBook:
        other = OrderBook(self.ticker)
        other._bids = {s: dict(b) for s, b in self._bids.items()}
        other.last_seq = self.last_seq
        other.last_ts = self.last_ts
        return other

    def as_levels(self) -> dict[str, list[tuple[Price, Qty]]]:
        """Canonical, ordering-independent form (for tests, hashing, persistence)."""
        return {s.value: [(lv.price, lv.qty) for lv in self.bids(s)] for s in Side}

    def __repr__(self) -> str:
        yb, ya = self.best_bid(Side.YES), self.best_ask(Side.YES)
        return f"OrderBook({self.ticker}, yes_bid={yb}, yes_ask={ya})"
