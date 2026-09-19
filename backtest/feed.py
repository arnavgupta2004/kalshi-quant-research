"""Chronological event feeds.

``ListFeed`` replays given events (tests).  ``StoreFeed`` builds the feed from the local database
only - a backtest never touches the network - and is where each source's *information time* is
decided (see ``events.py``).

Survivorship / selection note.  A feed replays exactly the markets it is given.  Whether that set is
point-in-time depends on how it was chosen: the book recordings select markets by 24h volume or
scheduled expiry *at recording time* (ex-ante, fine); the Stage 2 history dataset filters on
**lifetime** volume, which is only known after the fact - backtests on it study markets that
attracted trading and must say so (``universe_point_in_time`` in the result metadata).
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime

from backtest.events import (
    BookConfirm,
    BookUpdate,
    FeedEvent,
    MarketClose,
    Settlement,
    TradeTick,
)
from backtest.market_info import MarketInfo
from data.storage.duckdb_store import Store, as_utc
from market.contracts import Market, Side, Trade
from market.timeutil import to_epoch_us

SECOND = 1_000_000_000


def _key(e: FeedEvent) -> tuple[int, int]:
    return (e.ts_ns, int(e.priority))


class ListFeed:
    """Replays a fixed list of events, sorted by (time, priority) with a stable tie-break."""

    def __init__(self, events: Iterable[FeedEvent]) -> None:
        self.events = sorted(events, key=_key)

    def __iter__(self) -> Iterator[FeedEvent]:
        return iter(self.events)

    def truncated(self, until_ns: int) -> ListFeed:
        """Only what a live system would have seen by ``until_ns`` (for causality tests)."""
        return ListFeed(e for e in self.events if e.ts_ns <= until_ns)


@dataclass(frozen=True)
class FeedTiming:
    """Delay between a fact and the moment a live system could know it."""

    trade_delay_ns: int = 250_000_000  # backfilled trade -> would have reached a live feed
    settlement_delay_ns: int = 1 * SECOND


def _ns(dt: datetime) -> int:
    return to_epoch_us(dt) * 1000


class StoreFeed:
    """A merged, time-ordered feed of books, trades, closes and settlements from a ``Store``."""

    def __init__(
        self,
        store: Store,
        tickers: Sequence[str] | None = None,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
        timing: FeedTiming = FeedTiming(),  # noqa: B008 - frozen dataclass, safe as a default
        confirm_with_polls: bool = True,
    ) -> None:
        self.store = store
        self.timing = timing
        self.start_ns, self.end_ns = start_ns, end_ns
        self.confirm_with_polls = confirm_with_polls
        self.markets: list[Market] = store.read_markets(tickers)
        self.tickers = [m.ticker for m in self.markets]
        self.infos = {m.ticker: MarketInfo.from_market(m) for m in self.markets}
        self._events: list[FeedEvent] | None = None

    # ------------------------------------------------------------------ building
    def _in_window(self, ts: int) -> bool:
        return (self.start_ns is None or ts >= self.start_ns) and (
            self.end_ns is None or ts <= self.end_ns
        )

    def _build(self) -> list[FeedEvent]:
        s, t = self.store, self.tickers
        if not t:
            return []
        events: list[FeedEvent] = []
        book_rows = s.query(
            "SELECT ticker, recv_ts_ns, req_ts_ns, yes_px, yes_qty, no_px, no_qty "
            "FROM book_snapshots "
            "WHERE ticker IN (SELECT unnest(?)) ORDER BY recv_ts_ns",
            [t],
        )
        for tk, recv, req, ypx, yq, npx, nq in book_rows:
            if self._in_window(recv):
                events.append(
                    BookUpdate(
                        recv,
                        tk,
                        tuple(zip(ypx, yq, strict=True)),
                        tuple(zip(npx, nq, strict=True)),
                        req,
                    )
                )
        trade_rows = s.query(
            "SELECT trade_id, ticker, created_time, yes_price, no_price, count, taker_side, "
            "taker_book_side "
            "FROM trades WHERE ticker IN (SELECT unnest(?))",
            [t],
        )
        for tid, tk, created, yp, np_, cnt, side, bside in trade_rows:
            ts = _ns(as_utc(created)) + self.timing.trade_delay_ns
            if self._in_window(ts):
                trade = Trade(
                    tid, tk, yp, np_, cnt, Side(side) if side else None, bside, as_utc(created)
                )
                events.append(TradeTick(ts, tk, trade))
        if self.confirm_with_polls:  # liveness: every poll cycle confirms all unchanged books
            for (recv,) in s.query("SELECT recv_ts_ns FROM poll_log ORDER BY recv_ts_ns"):
                if self._in_window(recv):
                    events.append(BookConfirm(recv))
        floor = self.start_ns
        for m in self.markets:
            # A close/settlement that happened before the window is *already known* when it starts.
            if m.settlement_value is not None and m.settlement_ts is not None:
                ts = _ns(m.settlement_ts) + self.timing.settlement_delay_ns
                if self.end_ns is None or ts <= self.end_ns:
                    events.append(Settlement(max(ts, floor or ts), m.ticker, m.settlement_value))
            if m.close_time is not None and m.status.value in (
                "closed",
                "determined",
                "finalized",
                "disputed",
                "amended",
            ):
                ts = _ns(m.close_time)
                if self.end_ns is None or ts <= self.end_ns:
                    events.append(MarketClose(max(ts, floor or ts), m.ticker))
        events.sort(key=_key)
        return events

    def __iter__(self) -> Iterator[FeedEvent]:
        if self._events is None:
            self._events = self._build()
        return iter(self._events)

    def __len__(self) -> int:
        if self._events is None:
            self._events = self._build()
        return len(self._events)

    def merged(self, *others: Iterable[FeedEvent]) -> Iterator[FeedEvent]:
        return heapq.merge(iter(self), *others, key=_key)
