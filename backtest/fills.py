"""Fill models: how much of an order does the recorded market let us have, and at what price?

TAKER (marketable) orders are simple in principle - walk the derived asks - but two things are not
knowable from snapshots and are therefore *parameters*, not facts:

  * staleness   the book we walk is the last snapshot; REST polls are seconds apart and the true
                book at the order's arrival may have moved.  ``max_staleness_ns`` rejects orders
                that would trade against a book older than that; ``extra_slippage_ticks`` and
                ``depth_fraction`` haircut what the stale book claims.
  * self-impact our fills consume liquidity until the next snapshot (the engine applies them).

MAKER (resting) orders are fundamentally underdetermined: **historical data contains no queue
position.**  Whether a resting bid at 45c filled depends on how many contracts were ahead
of it, which we cannot observe.  So there are three model families, ordered by how
generous they are:

  pessimistic     fills only when the market trades *through* our price (so we must have been
                  exhausted, by price-time priority) or a snapshot shows the market crossing us.
  queue(a, c)     we join the queue with ``a`` x (displayed size at our price) contracts ahead
                  (a=1 back of the queue, a=0 front).  Prints at our price consume the queue before
                  us; inferred cancellations (a snapshot showing less size than trades explain) move
                  us forward by the share ``c``.  Through-prints and crossings fill us fully.
  optimistic      queue(a=0): first in line at our price.

By construction, cumulative fills satisfy

    pessimistic <= queue(a=1, c=0) <= queue(a, c) <= optimistic     at every instant,

which is property-tested.  A backtest result for a passive strategy is only meaningful as a *range*
across these models; where the range straddles zero, the strategy's edge is not established.

Which trades hit which resting order (price-time priority):
    resting YES bid @p  is hit by taker_side == NO  prints with yes_price <= p
    resting NO  bid @q  is hit by taker_side == YES prints with no_price  <= q
A print at exactly our price consumes the queue; a print strictly worse than ours means every bid at
our level (ours included) was exhausted first.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from market.contracts import Side, Trade
from market.order_book import OrderBook
from market.units import Price, Qty, complement


@dataclass(slots=True)
class RestingOrder:
    order_id: int
    ticker: str
    side: Side  # we are BUYING this side
    price: Price  # limit price of ``side``
    qty: Qty  # remaining
    original_qty: Qty
    placed_ts: int
    tag: str = ""
    # queue state (used by QueueModel)
    ahead: Qty = 0
    last_level_size: Qty = 0
    traded_at_level_since: Qty = 0
    filled: Qty = field(default=0)


def trade_price(side: Side, t: Trade) -> Price:
    """The trade's price expressed on ``side``'s scale."""
    return t.yes_price if side is Side.YES else t.no_price


def hits_side(side: Side, t: Trade) -> bool:
    """Did this print consume liquidity resting as a bid on ``side``?"""
    return t.taker_side is not None and t.taker_side is side.opposite


def level_size(book: OrderBook | None, side: Side, price: Price) -> Qty:
    """Displayed size of bids on ``side`` at exactly ``price`` (others’ orders only)."""
    if book is None:
        return 0
    for lv in book.bids(side):
        if lv.price == price:
            return lv.qty
        if lv.price < price:
            break
    return 0


# --------------------------------------------------------------------------- taker
@dataclass(frozen=True)
class TakerModel:
    max_staleness_ns: int | None = None  # reject if the book is older than this at arrival
    extra_slippage_ticks: int = 0  # each fill priced this much worse (capped by the limit)
    depth_fraction: float = 1.0  # only this fraction of displayed depth is assumed takeable

    def execute(
        self, book: OrderBook | None, book_age_ns: int | None, side: Side, limit: Price, qty: Qty
    ) -> list[tuple[Price, Qty]]:
        """Fills ``[(price, qty), ...]`` for a marketable buy of ``side`` limited at ``limit``."""
        if book is None or book_age_ns is None:
            return []
        if self.max_staleness_ns is not None and book_age_ns > self.max_staleness_ns:
            return []
        fills: list[tuple[Price, Qty]] = []
        remaining = qty
        for lv in book.asks(side):
            if remaining <= 0:
                break
            px = lv.price + self.extra_slippage_ticks
            if lv.price > limit or px > limit:
                break
            take = min(remaining, int(lv.qty * self.depth_fraction))
            if take <= 0:
                continue
            fills.append((px, take))
            remaining -= take
        return fills


# --------------------------------------------------------------------------- maker
@dataclass(frozen=True)
class QueueModel:
    """Resting-order fills.  ``alpha``: share of the displayed level ahead of us on arrival;
    ``cancel_share``: share of *unexplained* size decreases attributed to orders ahead of us;
    ``through_only``: ignore at-price prints entirely (the pessimistic model)."""

    alpha: float = 1.0
    cancel_share: float = 0.0
    through_only: bool = False
    name: str = ""

    @classmethod
    def optimistic(cls) -> QueueModel:
        return cls(alpha=0.0, cancel_share=0.0, name="optimistic")

    @classmethod
    def pessimistic(cls) -> QueueModel:
        return cls(alpha=1.0, cancel_share=0.0, through_only=True, name="pessimistic")

    @classmethod
    def queue(cls, alpha: float = 1.0, cancel_share: float = 0.5) -> QueueModel:
        return cls(
            alpha=alpha, cancel_share=cancel_share, name=f"queue(a={alpha},c={cancel_share})"
        )

    # -- lifecycle
    def on_place(self, o: RestingOrder, book: OrderBook | None) -> None:
        size = level_size(book, o.side, o.price)
        o.last_level_size = size
        o.ahead = int(self.alpha * size)
        o.traded_at_level_since = 0

    def on_trade(self, o: RestingOrder, t: Trade) -> Qty:
        """Contracts filled by this print (0 if none)."""
        if not hits_side(o.side, t) or o.qty <= 0:
            return 0
        px = trade_price(o.side, t)
        if px > o.price:
            return 0  # traded at a price better for the seller than our bid: never reached us
        if px < o.price:  # through us: our level (and everything ahead) was exhausted first
            return o.qty
        if self.through_only:
            return 0
        o.traded_at_level_since += t.count
        consumed = min(o.ahead, t.count)
        o.ahead -= consumed
        return min(o.qty, t.count - consumed)

    def on_book(self, o: RestingOrder, book: OrderBook) -> Qty:
        """Update queue position from a new snapshot; returns fills if the market crossed us."""
        ask = book.best_ask(o.side)
        if ask is not None and ask.price <= o.price:
            return o.qty  # the market is offering at or below our bid: we would have been hit
        size = level_size(book, o.side, o.price)
        expected = max(0, o.last_level_size - o.traded_at_level_since)
        if size < expected:
            o.ahead -= int(self.cancel_share * (expected - size))  # unexplained -> cancellations
        o.ahead = max(0, min(o.ahead, size))
        o.last_level_size, o.traded_at_level_since = size, 0
        return 0


def apply_taker_fills_to_book(book: OrderBook, side: Side, fills: list[tuple[Price, Qty]]) -> None:
    """Remove liquidity we consumed so a second order in the same snapshot cannot re-use it."""
    opposite = side.opposite
    for price, qty in fills:
        try:
            book.apply_delta(opposite, complement(price), -qty)
        except Exception:  # noqa: BLE001 - haircut slippage may shift the price off the level; best effort
            continue
