"""The replay loop.

Causality is enforced structurally, not by convention:

  * the engine is the only owner of state and advances it one event at a time in
    (time, priority, sequence) order; ``ctx.now`` is that event's time;
  * a strategy sees the world *only* through ``Context``, which exposes state as of ``now`` (books
    as last received, trades as last received, ``MarketInfo`` - never the realised close, volume or
    result until a ``Settlement`` event has been processed);
  * orders take effect ``latency_submit`` after the decision, are filled against the book as it is
    *at arrival*, and the strategy hears about fills ``latency_ack`` later - so leg risk and stale
    decisions are simulated, not assumed away;
  * cash is reserved for resting buy orders, so no order can create leverage the account lacks.

Determinism: no randomness; identical inputs give identical outputs (tested).
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from backtest.events import (
    BookConfirm,
    BookUpdate,
    FeedEvent,
    Fill,
    MarketClose,
    OrderRejected,
    Priority,
    Settlement,
    TradeTick,
    Wake,
)
from backtest.fills import (
    QueueModel,
    RestingOrder,
    TakerModel,
    apply_taker_fills_to_book,
    trade_price,
)
from backtest.market_info import MarketInfo
from backtest.portfolio import Portfolio, Position
from market.contracts import Side, Trade
from market.fees import FeeBook
from market.order_book import Level, OrderBook
from market.units import PRICE_SCALE, Price, Qty

SECOND = 1_000_000_000


class TIF(StrEnum):
    IOC = "ioc"  # immediate-or-cancel: take what is there, drop the rest
    GTC = "gtc"  # good-till-cancelled: take what is there, rest the remainder
    POST_ONLY = "post_only"  # rest only; rejected if it would cross


@dataclass(frozen=True, slots=True)
class Order:
    """A request to BUY ``qty`` (centi-contracts) of ``side`` at no more than ``price`` (ticks)."""

    ticker: str
    side: Side
    price: Price
    qty: Qty
    tif: TIF = TIF.IOC
    tag: str = ""


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_cash_micro: int = 10_000 * 1_000_000  # $10,000
    latency_submit_ns: int = 100_000_000  # decision -> exchange
    latency_ack_ns: int = 100_000_000  # exchange -> strategy (fill notices)
    latency_cancel_ns: int = 100_000_000
    taker: TakerModel = field(default_factory=TakerModel)
    maker: QueueModel = field(default_factory=QueueModel.queue)
    fees: FeeBook = field(default_factory=FeeBook)
    equity_sample_ns: int = 10 * SECOND
    trade_history: int = 64  # recent prints kept per market for strategies
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class OrderRecord:
    order_id: int
    submitted_ts: int
    arrival_ts: int
    ticker: str
    side: Side
    price: Price
    qty: Qty
    tif: TIF
    tag: str
    status: str = "submitted"  # rejected | filled | partial | resting | cancelled | expired
    filled: Qty = 0
    reason: str = ""


@dataclass(frozen=True, slots=True)
class FillRecord:
    exec_ts: int
    order_id: int
    ticker: str
    side: Side
    price: Price
    qty: Qty
    fee_micro: int
    liquidity: str
    tag: str


@dataclass
class BacktestResult:
    orders: list[OrderRecord]
    fills: list[FillRecord]
    equity: list[tuple[int, int, int]]  # (ts_ns, equity µ$, cash µ$)
    settlements: list[tuple[int, str, int, int]]  # (ts_ns, ticker, value, realised µ$)
    portfolio: Portfolio
    final_equity_micro: int
    initial_cash_micro: int
    events_processed: int
    metadata: dict[str, Any]

    @property
    def pnl_micro(self) -> int:
        return self.final_equity_micro - self.initial_cash_micro


class BookView:
    """Read-only view of a market's last received book (the engine's state cannot be mutated)."""

    __slots__ = ("_b", "ts_ns", "age_ns")

    def __init__(self, book: OrderBook, ts_ns: int, now: int) -> None:
        self._b, self.ts_ns, self.age_ns = book, ts_ns, now - ts_ns

    def bids(self, side: Side = Side.YES) -> list[Level]:
        return self._b.bids(side)

    def asks(self, side: Side = Side.YES) -> list[Level]:
        return self._b.asks(side)

    def best_bid(self, side: Side = Side.YES) -> Level | None:
        return self._b.best_bid(side)

    def best_ask(self, side: Side = Side.YES) -> Level | None:
        return self._b.best_ask(side)

    def mid(self) -> float | None:
        return self._b.mid()

    def spread(self) -> Price | None:
        return self._b.spread()

    def copy(self) -> OrderBook:
        return self._b.copy()


class Strategy(Protocol):
    def on_event(self, ctx: Context, event: object) -> None: ...


@dataclass(slots=True)
class MarketState:
    book: OrderBook | None = None
    book_ts: int | None = None
    last_trade: Trade | None = None
    trades: deque = field(default_factory=lambda: deque(maxlen=64))
    closed: bool = False
    outcome: Price | None = None  # revealed only when the Settlement event is processed


class Context:
    """Everything a strategy can see and do.  State is as of ``now``; nothing else is reachable."""

    def __init__(self, engine: Backtest) -> None:
        self._e = engine

    @property
    def now(self) -> int:
        return self._e.now

    def info(self, ticker: str) -> MarketInfo:
        return self._e.infos[ticker]

    @property
    def tickers(self) -> list[str]:
        return list(self._e.infos)

    def book(self, ticker: str) -> BookView | None:
        st = self._e.state.get(ticker)
        return (
            None if st is None or st.book is None else BookView(st.book, st.book_ts or 0, self.now)
        )

    def last_trade(self, ticker: str) -> Trade | None:
        st = self._e.state.get(ticker)
        return None if st is None else st.last_trade

    def recent_trades(self, ticker: str, n: int = 20) -> list[Trade]:
        st = self._e.state.get(ticker)
        return [] if st is None else list(st.trades)[-n:]

    def is_open(self, ticker: str) -> bool:
        st = self._e.state.get(ticker)
        return st is not None and not st.closed and st.outcome is None

    def outcome(self, ticker: str) -> Price | None:
        """The settlement value - ``None`` until the Settlement event has been delivered."""
        st = self._e.state.get(ticker)
        return None if st is None else st.outcome

    def position(self, ticker: str) -> Position:
        p = self._e.portfolio.position(ticker)
        return Position(p.yes, p.yes_cost, p.no, p.no_cost)

    @property
    def cash(self) -> int:
        return self._e.portfolio.cash

    @property
    def available_cash(self) -> int:
        return self._e.portfolio.cash - sum(self._e.reserved.values())

    def equity(self) -> int:
        return self._e.equity()

    def open_orders(self, ticker: str | None = None) -> list[RestingOrder]:
        return [o for t, os in self._e.resting.items() if ticker in (None, t) for o in os]

    def place(self, order: Order) -> int:
        return self._e.submit(order)

    def cancel(self, order_id: int) -> None:
        self._e.request_cancel(order_id)

    def wake_at(self, ts_ns: int, tag: str = "", ticker: str = "") -> None:
        self._e.schedule(ts_ns, Priority.WAKE, Wake(ts_ns, tag, ticker))


class Backtest:
    def __init__(
        self,
        feed: Iterable[FeedEvent],
        infos: Mapping[str, MarketInfo],
        strategy: Strategy,
        config: BacktestConfig = BacktestConfig(),  # noqa: B008 - frozen, safe default
    ) -> None:
        self.feed, self.infos, self.strategy, self.cfg = feed, dict(infos), strategy, config
        self.now = 0
        self._seq = 0
        self._heap: list[tuple[int, int, int, object]] = []
        self.state: dict[str, MarketState] = {
            t: MarketState(trades=deque(maxlen=config.trade_history)) for t in self.infos
        }
        self.portfolio = Portfolio(config.initial_cash_micro)
        self.resting: dict[str, list[RestingOrder]] = {}
        self.reserved: dict[int, int] = {}
        self.records: dict[int, OrderRecord] = {}
        self.fill_log: list[FillRecord] = []
        self.equity_curve: list[tuple[int, int, int]] = []
        self.settlements: list[tuple[int, str, int, int]] = []
        self._next_order_id = 1
        self._next_sample = 0
        self._n_events = 0
        self.ctx = Context(self)

    # ------------------------------------------------------------------ scheduling
    def schedule(self, ts_ns: int, priority: Priority, payload: object) -> None:
        if ts_ns < self.now:
            raise ValueError(f"cannot schedule in the past: {ts_ns} < now {self.now}")
        self._seq += 1
        heapq.heappush(self._heap, (ts_ns, int(priority), self._seq, payload))

    def submit(self, order: Order) -> int:
        if order.ticker not in self.state:
            raise KeyError(f"unknown market {order.ticker}")
        oid = self._next_order_id
        self._next_order_id += 1
        arrival = self.now + self.cfg.latency_submit_ns
        self.records[oid] = OrderRecord(
            oid,
            self.now,
            arrival,
            order.ticker,
            order.side,
            order.price,
            order.qty,
            order.tif,
            order.tag,
        )
        self.schedule(arrival, Priority.ORDER_ARRIVAL, ("order", oid, order))
        return oid

    def request_cancel(self, order_id: int) -> None:
        self.schedule(
            self.now + self.cfg.latency_cancel_ns, Priority.CANCEL_ARRIVAL, ("cancel", order_id)
        )

    # ------------------------------------------------------------------ marks
    def marks(self) -> dict[str, tuple[Price | None, Price | None]]:
        out = {}
        for t, st in self.state.items():
            if st.book is None:
                continue
            yb, nb = st.book.best_bid(Side.YES), st.book.best_bid(Side.NO)
            out[t] = (None if yb is None else yb.price, None if nb is None else nb.price)
        return out

    def equity(self) -> int:
        return self.portfolio.equity(self.marks())

    def _sample_equity(self, force: bool = False) -> None:
        if force or self.now >= self._next_sample:
            self.equity_curve.append((self.now, self.equity(), self.portfolio.cash))
            self._next_sample = self.now + self.cfg.equity_sample_ns

    # ------------------------------------------------------------------ main loop
    def run(self) -> BacktestResult:
        feed_iter = iter(self.feed)
        nxt = next(feed_iter, None)
        started = False
        if hasattr(self.strategy, "on_start"):
            self.strategy.on_start(self.ctx)
        while nxt is not None or self._heap:
            take_feed = nxt is not None and (
                not self._heap
                or (nxt.ts_ns, int(nxt.priority)) <= (self._heap[0][0], self._heap[0][1])
            )
            if take_feed:
                ev = nxt
                nxt = next(feed_iter, None)
                if ev.ts_ns < self.now:
                    raise ValueError(
                        f"feed went backwards: {ev.ts_ns} < {self.now} ({type(ev).__name__})"
                    )
                self.now = ev.ts_ns
                if not started:
                    self._next_sample, started = self.now, True
                # Everything stamped with the same instant is SIMULTANEOUS (e.g. the books of one
                # poll response).  Apply all of it before the strategy sees any, or it would observe
                # impossible half-updated cross-sections - a real cause of phantom arbitrage.
                batch = [ev]
                while nxt is not None and nxt.ts_ns == ev.ts_ns:
                    batch.append(nxt)
                    nxt = next(feed_iter, None)
                for e in batch:
                    self._apply_feed(e)
                for e in batch:
                    self._notify_feed(e)
                self._n_events += len(batch) - 1
            else:
                ts, _, _, payload = heapq.heappop(self._heap)
                self.now = ts
                self._dispatch_scheduled(payload)
            self._n_events += 1
            self._sample_equity()
        # anything still resting at the end is simply left unfilled
        self._sample_equity(force=True)
        if hasattr(self.strategy, "on_end"):
            self.strategy.on_end(self.ctx)
        for rec in self.records.values():
            if rec.status in ("submitted", "resting") and rec.filled == 0:
                rec.status = "resting" if rec.status == "resting" else "expired"
        return BacktestResult(
            orders=list(self.records.values()),
            fills=self.fill_log,
            equity=self.equity_curve,
            settlements=self.settlements,
            portfolio=self.portfolio,
            final_equity_micro=self.equity(),
            initial_cash_micro=self.cfg.initial_cash_micro,
            events_processed=self._n_events,
            metadata=dict(self.cfg.metadata),
        )

    # ------------------------------------------------------------------ feed events
    def _apply_feed(self, ev: FeedEvent) -> None:
        """Update engine state (books, fills, settlements) for one feed event - no strategy call."""
        if isinstance(ev, BookConfirm):
            # a poll found the book unchanged: it is current as of now (levels untouched)
            for t, stt in self.state.items():
                if stt.book is not None and ev.ticker in ("", t):
                    stt.book_ts = ev.ts_ns
            return
        st = self.state.get(ev.ticker)
        if st is None:
            return  # a market the strategy universe does not include
        if isinstance(ev, BookUpdate):
            book = OrderBook(ev.ticker)
            book.apply_snapshot(ev.yes_bids, ev.no_bids)
            st.book, st.book_ts = book, ev.ts_ns
            for o in list(self.resting.get(ev.ticker, [])):
                filled = self.cfg.maker.on_book(o, book)
                if filled:
                    self._maker_fill(o, filled)
        elif isinstance(ev, TradeTick):
            st.last_trade = ev.trade
            st.trades.append(ev.trade)
            self._match_resting(ev.ticker, ev.trade)
        elif isinstance(ev, MarketClose):
            st.closed = True
            for o in list(self.resting.get(ev.ticker, [])):
                self._drop_resting(o, "cancelled", "market closed")
        elif isinstance(ev, Settlement):
            st.closed, st.outcome = True, ev.value
            for o in list(self.resting.get(ev.ticker, [])):
                self._drop_resting(o, "cancelled", "market settled")
            pnl = self.portfolio.settle(ev.ticker, ev.value)
            self.settlements.append((ev.ts_ns, ev.ticker, ev.value, pnl))
            self._sample_equity(force=True)

    def _notify_feed(self, ev: FeedEvent) -> None:
        if isinstance(ev, BookConfirm) or ev.ticker in self.state:
            self.strategy.on_event(self.ctx, ev)

    def _match_resting(self, ticker: str, trade: Trade) -> None:
        budget: dict[tuple[Side, Price], int] = {}
        for o in list(self.resting.get(ticker, [])):
            px = trade_price(o.side, trade)
            if px == o.price:  # at-level prints are shared by our orders in priority order
                key = (o.side, px)
                budget.setdefault(key, trade.count)
                ahead0 = o.ahead
                filled = self.cfg.maker.on_trade(o, replace(trade, count=budget[key]))
                budget[key] = max(0, budget[key] - filled - (ahead0 - o.ahead))
            else:
                filled = self.cfg.maker.on_trade(o, trade)
            if filled:
                self._maker_fill(o, filled)

    # ------------------------------------------------------------------ scheduled events
    def _dispatch_scheduled(self, payload: object) -> None:
        if isinstance(payload, tuple) and payload[0] == "order":
            self._on_order_arrival(payload[1], payload[2])
        elif isinstance(payload, tuple) and payload[0] == "cancel":
            for lst in self.resting.values():
                for o in lst:
                    if o.order_id == payload[1]:
                        self._drop_resting(o, "cancelled", "cancelled by strategy")
                        return
        elif isinstance(payload, (Fill, OrderRejected, Wake)):
            self.strategy.on_event(self.ctx, payload)
        else:  # pragma: no cover - defensive
            raise TypeError(f"unknown scheduled payload {payload!r}")

    def _reject(self, oid: int, reason: str) -> None:
        rec = self.records[oid]
        rec.status, rec.reason = "rejected", reason
        self.schedule(
            self.now + self.cfg.latency_ack_ns,
            Priority.FILL_NOTICE,
            OrderRejected(self.now + self.cfg.latency_ack_ns, oid, reason, rec.tag),
        )

    def _on_order_arrival(self, oid: int, order: Order) -> None:
        rec = self.records[oid]
        st = self.state[order.ticker]
        if st.closed or st.outcome is not None:
            return self._reject(oid, "market closed")
        if order.qty <= 0 or not 0 < order.price < PRICE_SCALE:
            return self._reject(oid, "invalid price or quantity")
        need = order.price * order.qty
        if self.portfolio.cash - sum(self.reserved.values()) < need:
            return self._reject(oid, "insufficient buying power")
        book = st.book
        remaining = order.qty
        marketable = (
            book is not None
            and (ask := book.best_ask(order.side)) is not None
            and ask.price <= order.price
        )
        if marketable:
            if order.tif is TIF.POST_ONLY:
                return self._reject(oid, "post-only order would cross")
            age = None if st.book_ts is None else self.now - st.book_ts
            fills = self.cfg.taker.execute(book, age, order.side, order.price, order.qty)
            fee_sched = self.cfg.fees.for_ticker(order.ticker)
            if fills:
                fee_total = fee_sched.order_fee_micro(fills)
                self._record_fills(oid, order, fills, "taker", fee_total, fee_sched)
                apply_taker_fills_to_book(book, order.side, fills)
                remaining -= sum(q for _, q in fills)
        if remaining > 0:
            if order.tif is TIF.GTC or order.tif is TIF.POST_ONLY:
                o = RestingOrder(
                    oid,
                    order.ticker,
                    order.side,
                    order.price,
                    remaining,
                    order.qty,
                    self.now,
                    order.tag,
                )
                o.filled = order.qty - remaining
                self.cfg.maker.on_place(o, book)
                self.resting.setdefault(order.ticker, []).append(o)
                self.reserved[oid] = order.price * remaining
                rec.status = "resting" if rec.filled == 0 else "partial"
            else:
                rec.status = "partial" if rec.filled else "expired"
                rec.reason = (
                    "no liquidity at limit" if not rec.filled else "ioc remainder cancelled"
                )
        else:
            rec.status = "filled"

    # ------------------------------------------------------------------ fills
    def _record_fills(self, oid, order, fills, liquidity, fee_total, fee_sched) -> None:
        total_q = sum(q for _, q in fills)
        rec = self.records[oid]
        fee_left = fee_total
        for i, (price, qty) in enumerate(fills):
            fee = fee_left if i == len(fills) - 1 else fee_total * qty // total_q
            fee_left -= fee
            self.portfolio.buy(order.ticker, order.side, price, qty, fee)
            fr = FillRecord(
                self.now, oid, order.ticker, order.side, price, qty, fee, liquidity, order.tag
            )
            self.fill_log.append(fr)
            rec.filled += qty
            self.schedule(
                self.now + self.cfg.latency_ack_ns,
                Priority.FILL_NOTICE,
                Fill(
                    self.now + self.cfg.latency_ack_ns,
                    self.now,
                    oid,
                    order.ticker,
                    order.side,
                    price,
                    qty,
                    fee,
                    liquidity,
                    order.tag,
                ),
            )
        self._sample_equity(force=True)

    def _maker_fill(self, o: RestingOrder, qty: Qty) -> None:
        qty = min(qty, o.qty)
        if qty <= 0:
            return
        fee_sched = self.cfg.fees.for_ticker(o.ticker)
        fee = fee_sched.order_fee_micro([(o.price, qty)], maker=True)
        self.portfolio.buy(o.ticker, o.side, o.price, qty, fee)
        rec = self.records[o.order_id]
        rec.filled += qty
        self.fill_log.append(
            FillRecord(self.now, o.order_id, o.ticker, o.side, o.price, qty, fee, "maker", o.tag)
        )
        self.schedule(
            self.now + self.cfg.latency_ack_ns,
            Priority.FILL_NOTICE,
            Fill(
                self.now + self.cfg.latency_ack_ns,
                self.now,
                o.order_id,
                o.ticker,
                o.side,
                o.price,
                qty,
                fee,
                "maker",
                o.tag,
            ),
        )
        o.qty -= qty
        o.filled += qty
        self.reserved[o.order_id] = o.price * o.qty
        rec.status = "filled" if o.qty == 0 else "partial"
        if o.qty == 0:
            self._remove_resting(o)
        self._sample_equity(force=True)

    def _remove_resting(self, o: RestingOrder) -> None:
        self.resting[o.ticker] = [
            x for x in self.resting.get(o.ticker, []) if x.order_id != o.order_id
        ]
        self.reserved.pop(o.order_id, None)

    def _drop_resting(self, o: RestingOrder, status: str, reason: str) -> None:
        rec = self.records[o.order_id]
        rec.status = "partial" if rec.filled and status == "cancelled" else status
        rec.reason = reason
        self._remove_resting(o)


def run_backtest(
    feed: Iterable[FeedEvent],
    infos: Mapping[str, MarketInfo],
    strategy: Strategy,
    config: BacktestConfig = BacktestConfig(),  # noqa: B008
) -> BacktestResult:
    return Backtest(feed, infos, strategy, config).run()


__all__ = [
    "Backtest",
    "BacktestConfig",
    "BacktestResult",
    "Context",
    "Order",
    "TIF",
    "Strategy",
    "run_backtest",
    "BookView",
    "OrderRecord",
    "FillRecord",
    "Callable",
]
