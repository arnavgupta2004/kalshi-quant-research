"""Reference strategies: the smallest ones that exercise each side of the engine.

They are *test instruments*, not trading recommendations:

  ``ArbitrageTaker``  Stage 4's detector run live against the engine's books; sends IOC orders for
                      every leg.  With zero latency and full fills its realised P&L equals the
                      detector's predicted net edge *exactly* (an integration test between the two
                      stages); with latency it measures the leg risk the detector assumes away.
  ``PassiveQuoter``   posts a two-sided passive quote and refreshes it - the simplest strategy whose
                      P&L depends on the maker fill model (the sensitivity-range demo).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from arbitrage.detector import DetectorParams, detect
from arbitrage.opportunity import Classification, RelationSpec
from backtest.engine import TIF, Context, Order
from backtest.events import BookUpdate, Fill
from market.contracts import Side


@dataclass
class ArbitrageTaker:
    specs: list[RelationSpec]
    params: DetectorParams
    min_net_micro: int = 1
    max_book_age_ns: int | None = None
    cooldown_ns: int = 0
    size_cap: int | None = None  # centi-bundles; None = whatever the detector recommends

    def __post_init__(self) -> None:
        self._by_ticker: dict[str, list[RelationSpec]] = defaultdict(list)
        for s in self.specs:
            for t in s.relation.tickers():
                self._by_ticker[t].append(s)
        self._last_fire: dict[int, int] = {}
        self.opportunities_seen = 0
        self.orders_sent = 0
        self.fill_events: list[Fill] = []

    def on_event(self, ctx: Context, ev: object) -> None:
        if isinstance(ev, Fill):
            self.fill_events.append(ev)
            return
        if not isinstance(ev, BookUpdate):
            return
        for spec in self._by_ticker.get(ev.ticker, []):
            books = {}
            for t in spec.relation.tickers():
                view = ctx.book(t)
                if view is None or not ctx.is_open(t):
                    break
                if self.max_book_age_ns is not None and view.age_ns > self.max_book_age_ns:
                    break
                books[t] = view.copy()
            else:
                self._try(ctx, spec, books)

    def _try(self, ctx: Context, spec: RelationSpec, books) -> None:
        key = id(spec)
        last = self._last_fire.get(key)
        if last == ctx.now:
            return  # one reaction per instant: a simultaneous batch is ONE piece of news
        if last is not None and ctx.now - last < self.cooldown_ns:
            return
        ops, _ = detect([spec], books, self.params, ts_ns=ctx.now)
        for op in ops:
            if op.classification not in (Classification.EXECUTABLE, Classification.PARTIAL):
                continue
            if op.net_micro is None or op.net_micro < self.min_net_micro:
                continue
            self.opportunities_seen += 1
            self._last_fire[key] = ctx.now
            bundles = (
                op.priced.bundles
                if self.size_cap is None
                else min(op.priced.bundles, self.size_cap)
            )
            for leg in op.constraint.legs:
                limit = (
                    op.priced.legs[[lg.contract for lg in op.priced.legs].index(leg.contract)]
                    .fills[-1]
                    .price
                )
                ctx.place(
                    Order(
                        leg.contract.ticker,
                        leg.contract.side,
                        limit,
                        leg.qty * bundles,
                        TIF.IOC,
                        f"arb:{op.constraint.name}",
                    )
                )
                self.orders_sent += 1
            return  # one opportunity per relation per event


@dataclass
class PassiveQuoter:
    """Bid both sides at the current best bid (join the queue); requote when it moves."""

    tickers: list[str]
    qty: int = 1000  # centi-contracts
    improve_ticks: int = 0
    max_inventory: int = 5000

    def __post_init__(self) -> None:
        self._quoted: dict[tuple[str, Side], tuple[int, int]] = {}  # -> (order_id, price)

    def on_event(self, ctx: Context, ev: object) -> None:
        if (
            not isinstance(ev, BookUpdate)
            or ev.ticker not in self.tickers
            or not ctx.is_open(ev.ticker)
        ):
            return
        view = ctx.book(ev.ticker)
        pos = ctx.position(ev.ticker)
        for side in (Side.YES, Side.NO):
            bid = view.best_bid(side)
            key = (ev.ticker, side)
            inventory_side = pos.yes if side is Side.YES else pos.no
            if bid is None or inventory_side >= self.max_inventory:
                continue
            price = bid.price + self.improve_ticks
            ask = view.best_ask(side)
            if ask is not None and price >= ask.price:
                price = ask.price - 100  # never cross: stay passive
            if price <= 0:
                continue
            prev = self._quoted.get(key)
            if (
                prev
                and prev[1] == price
                and any(o.order_id == prev[0] for o in ctx.open_orders(ev.ticker))
            ):
                continue
            if prev:
                ctx.cancel(prev[0])
            oid = ctx.place(Order(ev.ticker, side, price, self.qty, TIF.POST_ONLY, "quote"))
            self._quoted[key] = (oid, price)
