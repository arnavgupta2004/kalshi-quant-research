"""Where events come from.  Every source is an async iterator of the engine's feed events, so the
trader neither knows nor cares whether it is running on the WebSocket, on polled REST data, or on a
recorded tape:

  ``WsSource``        the Kalshi WebSocket (needs an API key; read-only, public channels only).
  ``RestPollSource``  public REST polling, no credentials: books every ``interval_s`` (like the
                      recorder) and public trades.  A live source that works today; its books are
                      as stale as a backtest's, so it is the like-for-like live analogue.
  ``TapeSource``      a recorded tape, for replay.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable

from backtest.events import BookConfirm, BookUpdate, TradeTick
from data.normalization.book_stream import SeenSet
from data.normalization.normalizer import normalize_trade, orderbook_from_rest
from kalshi_client.exceptions import MessageValidationError
from kalshi_client.rest import KalshiRestClient
from kalshi_client.websocket import KalshiWebSocket
from market.contracts import Side
from market.order_book import OrderBookInconsistency
from market.timeutil import now_ns
from paper import tape
from paper.adapter import Incident, WsAdapter, levels

log = logging.getLogger(__name__)


class WsSource:
    def __init__(self, ws: KalshiWebSocket, adapter: WsAdapter, *, tick_s: float = 0.25) -> None:
        self.ws, self.adapter, self.tick_s = ws, adapter, tick_s
        self._pending: asyncio.Future | None = None

    @property
    def incidents(self) -> list[Incident]:
        return self.adapter.incidents

    async def events(self) -> AsyncIterator[object]:
        stream = self.ws.stream().__aiter__()
        while True:
            if self._pending is None:
                self._pending = asyncio.ensure_future(stream.__anext__())
            done, _ = await asyncio.wait({self._pending}, timeout=self.tick_s)
            if done:
                try:
                    ev = self._pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    self._pending = None
                feed, resync = self.adapter.on_stream_event(ev)
                if resync:
                    self.ws.request_resync("book sequence problem")
                for e in feed:
                    yield e
            for e in self.adapter.tick(now_ns()):
                yield e

    async def aclose(self) -> None:
        if self._pending is not None:
            self._pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pending
        await self.ws.close()


class RestPollSource:
    def __init__(
        self,
        rest: KalshiRestClient,
        tickers: list[str],
        *,
        interval_s: float = 3.0,
        batch_size: int = 100,
        trade_pages: int = 3,
        clock: Callable[[], int] = now_ns,
        sleep: Callable[[float], object] = asyncio.sleep,
    ) -> None:
        self.rest, self.tickers = rest, list(tickers)
        self._known = set(self.tickers)
        self.interval_s, self.batch_size, self.trade_pages = interval_s, batch_size, trade_pages
        self._clock, self._sleep = clock, sleep
        self.incidents: list[Incident] = []
        self.cycles = 0
        self._last: dict[str, tuple] = {}
        self._seen = SeenSet()
        self._min_ts = int(clock() / 1e9) - 5

    async def _poll_books(self) -> list:
        out: list = []
        chunks = [
            self.tickers[i : i + self.batch_size]
            for i in range(0, len(self.tickers), self.batch_size)
        ]
        for chunk in chunks:
            apis = await self.rest.get_orderbooks(chunk, batch_size=len(chunk))
            recv = self._clock()
            for t in chunk:
                if t not in apis:
                    continue
                try:
                    book = orderbook_from_rest(t, apis[t])
                except OrderBookInconsistency as exc:
                    self.incidents.append(Incident(recv, "book_inconsistent", f"{t}: {exc}"))
                    continue
                key = (levels(book, Side.YES), levels(book, Side.NO))
                if self._last.get(t) != key:
                    self._last[t] = key
                    out.append(BookUpdate(recv, t, key[0], key[1]))
        return out

    async def _poll_trades(self) -> list:
        rows = []
        n = 0
        async for api in self.rest.iter_trades(min_ts=self._min_ts, page_size=500):
            n += 1
            if api.ticker in self._known and self._seen.add(api.trade_id):
                rows.append(api)
            if n >= self.trade_pages * 500:
                break
        out = []
        for api in reversed(rows):  # served newest first: the strategy must see them in order
            try:
                trade = normalize_trade(api)
            except MessageValidationError as exc:
                self.incidents.append(Incident(self._clock(), "trade_rejected", str(exc)[:200]))
                continue
            out.append(TradeTick(self._clock(), api.ticker, trade))
        self._min_ts = int(self._clock() / 1e9) - 3
        return out

    async def events(self) -> AsyncIterator[object]:
        while True:
            try:
                books = await self._poll_books()
                trades = await self._poll_trades()
            except Exception as exc:  # a failed cycle is an incident, not a crash: no confirmation
                self.incidents.append(Incident(self._clock(), "poll_failed", repr(exc)[:200]))
                log.warning("poll failed: %r", exc)
                await self._sleep(self.interval_s)
                continue
            for e in books:
                yield e
            for e in trades:
                yield e
            yield BookConfirm(self._clock())  # every market's book is current as of this poll
            self.cycles += 1
            await self._sleep(self.interval_s)

    async def aclose(self) -> None:
        return None


class TapeSource:
    """Replay a recorded tape (no pacing: as fast as the consumer takes it)."""

    def __init__(self, path) -> None:
        self.path = path
        self.incidents: list[Incident] = []

    async def events(self) -> AsyncIterator[object]:
        for ev in tape.read(self.path):
            yield ev

    async def aclose(self) -> None:
        return None
