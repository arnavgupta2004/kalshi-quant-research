"""Order-book collection - the only source of book history, because Kalshi serves none.

(``GET /historical/markets/{t}/orderbook`` 404s and candlesticks carry only top-of-book OHLC.)

Two recorders, one schema:

``BookPoller`` (REST, needs no credentials)
    Polls the batch order-book endpoint every ``interval_s`` and stores a full-depth
    keyframe whenever a book *changed* (or after ``heartbeat_s``).  Each row carries the
    request and receive timestamps because a REST snapshot is not instantaneous: the true
    state lies somewhere in ``[req_ts_ns, recv_ts_ns]`` and books polled in different chunks
    are not mutually atomic.  ``poll_log`` proves the poller was alive when a book did not change.

``WsBookRecorder`` (WebSocket, needs credentials)
    Stores snapshots and deltas *exactly as received* (with epoch/sid/seq) plus connection
    incidents, so a replay can re-run ``BookStreamProcessor`` and discard invalid segments
    instead of trusting the recorder's live judgement.  Written against the documented
    message schemas and tested with synthetic events; not yet exercised on the live stream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from data.collectors.config import BooksConfig, BookSelection, to_plain
from data.collectors.universe import series_of
from data.normalization.book_stream import ApplyStatus, BookStreamProcessor
from data.normalization.normalizer import (
    normalize_event,
    normalize_market,
    normalize_ws_trade,
    orderbook_from_rest,
)
from data.storage.duckdb_store import Store
from kalshi_client.exceptions import (
    APIError,
    AuthenticationError,
    MessageValidationError,
    TransportError,
)
from kalshi_client.models import (
    ApiOrderbook,
    WsError,
    WsMarketLifecycle,
    WsOrderbookDelta,
    WsOrderbookSnapshot,
    WsTrade,
)
from kalshi_client.rest import KalshiRestClient
from kalshi_client.websocket import Connected, Disconnected, KalshiWebSocket, Malformed
from market.contracts import Side
from market.order_book import OrderBook, OrderBookInconsistency
from market.timeutil import from_epoch_ns, now_ns, utcnow
from market.units import parse_price

log = logging.getLogger(__name__)


def book_hash(yes: list[tuple[int, int]], no: list[tuple[int, int]]) -> str:
    canon = f"Y{sorted(yes)}N{sorted(no)}".encode()
    return hashlib.blake2b(canon, digest_size=8).hexdigest()


def snapshot_row(
    book: OrderBook,
    *,
    recv_ts_ns: int,
    source: str,
    run_id: str,
    req_ts_ns: int | None = None,
    epoch: int | None = None,
    sid: int | None = None,
    seq: int | None = None,
    exchange_ts: datetime | None = None,
) -> dict:
    yes = [(lv.price, lv.qty) for lv in book.bids(Side.YES)]  # best first
    no = [(lv.price, lv.qty) for lv in book.bids(Side.NO)]
    return {
        "ticker": book.ticker,
        "recv_ts_ns": recv_ts_ns,
        "source": source,
        "req_ts_ns": req_ts_ns,
        "epoch": epoch,
        "sid": sid,
        "seq": seq,
        "exchange_ts": exchange_ts,
        "yes_px": [p for p, _ in yes],
        "yes_qty": [q for _, q in yes],
        "no_px": [p for p, _ in no],
        "no_qty": [q for _, q in no],
        "book_hash": book_hash(yes, no),
        "run_id": run_id,
    }


# ---------------------------------------------------------------------------- selection
async def select_book_tickers(rest: KalshiRestClient, sel: BookSelection) -> list[str]:
    """Most active open events (by summed 24h volume), with all their sibling markets."""
    by_event: dict[str, list] = defaultdict(list)
    async for m in rest.iter_markets(status="open"):
        if series_of(m.event_ticker) in sel.exclude_series:
            continue
        if sel.include_series and series_of(m.event_ticker) not in sel.include_series:
            continue
        if sel.max_hours_to_expiry is not None:
            due = m.expected_expiration_time or m.close_time
            if due is None or (due - utcnow()).total_seconds() > sel.max_hours_to_expiry * 3600:
                continue
        by_event[m.event_ticker].append(m)
    ranked = sorted(
        (
            (sum(m.volume_24h_fp or 0 for m in ms), et)
            for et, ms in by_event.items()
            if len(ms) >= sel.min_event_markets
        ),
        key=lambda x: (-x[0], x[1]),
    )
    tickers: list[str] = []
    for n, (vol, et) in enumerate(ranked):
        if n >= sel.top_events or vol <= 0:
            break
        ms = by_event[et] if sel.include_siblings else [m for m in by_event[et] if m.volume_24h_fp]
        if len(tickers) + len(ms) > sel.max_tickers:
            continue  # never truncate an event mid-way: a partial event is useless for arbitrage
        tickers += sorted(m.ticker for m in ms)
    return tickers


# ---------------------------------------------------------------------------- REST poller
@dataclass
class PollSummary:
    run_id: str = ""
    cycles: int = 0
    snapshots_stored: int = 0
    unchanged: int = 0
    missing: int = 0
    invalid: int = 0
    failures: int = 0
    tickers: int = 0


class BookPoller:
    def __init__(
        self,
        store: Store,
        rest: KalshiRestClient,
        cfg: BooksConfig,
        tickers: list[str],
        *,
        git_commit: str = "unknown",
        clock_ns: Callable[[], int] = now_ns,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.store, self.rest, self.cfg, self.tickers = store, rest, cfg, sorted(set(tickers))
        self.git_commit = git_commit
        self._clock_ns, self._mono, self._sleep = clock_ns, monotonic, sleep
        self.run_id = ""
        self.summary = PollSummary(tickers=len(self.tickers))
        # resume: a restart must not re-store books identical to the last stored ones
        self._last: dict[str, tuple[str, int]] = store.latest_book_hashes()
        self._qseq = 0

    async def run(self) -> PollSummary:
        self.run_id = self.store.start_run(
            "books", to_plain(self.cfg) | {"n_tickers": len(self.tickers)}, self.git_commit
        )
        self.summary.run_id = self.run_id
        status, note = "ok", None
        try:
            await self._loop()
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = "interrupted"
            raise
        except Exception as exc:
            status, note = "failed", repr(exc)[:500]
            raise
        finally:
            self.store.finish_run(self.run_id, status, note)
        return self.summary

    async def _loop(self) -> None:
        cfg = self.cfg
        start = self._mono()
        await self.refresh_metadata()
        next_meta = start + cfg.metadata_refresh_s
        next_poll, consecutive = start, 0
        while cfg.duration_s is None or self._mono() - start < cfg.duration_s:
            try:
                await self.poll_once()
                consecutive = 0
            except (APIError, TransportError, MessageValidationError) as exc:
                consecutive += 1
                self.summary.failures += 1
                log.warning("poll cycle failed (%d in a row): %s", consecutive, exc)
                if consecutive >= cfg.max_consecutive_failures:
                    raise
            if self._mono() >= next_meta:
                try:
                    await self.refresh_metadata()
                except (APIError, TransportError, MessageValidationError) as exc:
                    log.warning("metadata refresh failed: %s", exc)
                next_meta = self._mono() + cfg.metadata_refresh_s
            next_poll += cfg.interval_s
            delay = next_poll - self._mono()
            if delay < 0:  # fell behind: skip missed cycles instead of bursting to catch up
                next_poll = self._mono()
                delay = 0
            await self._sleep(delay)

    async def refresh_metadata(self) -> None:
        """Upsert market metadata (status changes are logged) and any unseen events."""
        markets = []
        for i in range(0, len(self.tickers), 100):
            async for api in self.rest.iter_markets(
                tickers=self.tickers[i : i + 100],
                include_multivariate=True,
                on_invalid=lambda item, e: self._quarantine("market", item, e),
            ):
                try:
                    markets.append(normalize_market(api))
                except MessageValidationError as exc:
                    self._quarantine("normalize_market", api.ticker, exc)
        self.store.upsert_markets(markets, self.run_id, partition="live")
        known = self.store.known_event_tickers()
        stored = set(self.store.read_series_fees())
        for series in sorted({m.event_ticker.split("-", 1)[0] for m in markets} - stored):
            try:  # fees are per series: without them net edge cannot be computed
                self.store.upsert_series([await self.rest.get_series(series)])
            except (APIError, MessageValidationError) as exc:
                log.warning("series %s: %s", series, exc)
        for et in sorted({m.event_ticker for m in markets} - known):
            try:
                self.store.upsert_events(
                    [normalize_event(await self.rest.get_event(et, with_nested_markets=True))],
                    self.run_id,
                )
            except (APIError, MessageValidationError) as exc:
                log.warning("event %s: %s", et, exc)

    def _quarantine(self, source: str, item, exc: Exception) -> None:
        self._qseq += 1
        self.summary.invalid += 1
        self.store.insert_quarantine(
            [
                {
                    "run_id": self.run_id,
                    "recv_ts_ns": self._clock_ns(),
                    "seq_no": self._qseq,
                    "source": source,
                    "error": str(exc)[:2000],
                    "raw": str(item)[:20000],
                }
            ]
        )

    async def poll_once(self) -> None:
        cfg, s = self.cfg, self.summary
        rows: list[dict] = []
        n_changed = n_missing = 0
        t0 = self._mono()
        last_recv = 0

        async def fetch(chunk: list[str]):
            req_ns = self._clock_ns()
            apis: dict[str, ApiOrderbook] = await self.rest.get_orderbooks(
                chunk, batch_size=len(chunk)
            )
            return chunk, req_ns, self._clock_ns(), apis

        # Chunks are fetched CONCURRENTLY (the client's rate limiter still paces them): sequential
        # chunks would skew the timestamps of markets in one event by seconds, and cross-market
        # relations are only meaningful if their books are observed close together in time.
        chunks = [
            self.tickers[i : i + cfg.batch_size]
            for i in range(0, len(self.tickers), cfg.batch_size)
        ]
        try:
            async with asyncio.TaskGroup() as tg:
                tasks = [tg.create_task(fetch(c)) for c in chunks]
        except ExceptionGroup as group:
            # TaskGroup wraps errors; the run loop's handlers expect the original exception types
            raise group.exceptions[0] from None
        for task in tasks:
            chunk, req_ns, recv_ns, apis = task.result()
            last_recv = max(last_recv, recv_ns)
            for t in chunk:
                if t not in apis:
                    n_missing += 1
                    continue
                try:
                    book = orderbook_from_rest(t, apis[t])
                except OrderBookInconsistency as exc:
                    self._quarantine("rest_book", t, exc)
                    continue
                row = snapshot_row(
                    book,
                    recv_ts_ns=recv_ns,
                    req_ts_ns=req_ns,
                    source="rest_poll",
                    run_id=self.run_id,
                )
                prev = self._last.get(t)
                heartbeat_due = prev is None or (recv_ns - prev[1]) / 1e9 >= cfg.heartbeat_s
                if prev is None or prev[0] != row["book_hash"] or heartbeat_due:
                    rows.append(row)
                    self._last[t] = (row["book_hash"], recv_ns)
                    n_changed += prev is None or prev[0] != row["book_hash"]
                else:
                    s.unchanged += 1
        s.snapshots_stored += self.store.insert_book_snapshots(rows)
        s.missing += n_missing
        s.cycles += 1
        self.store.insert_poll_log(
            [
                {
                    "run_id": self.run_id,
                    "recv_ts_ns": last_recv or self._clock_ns(),
                    "n_tickers": len(self.tickers),
                    "n_changed": n_changed,
                    "n_missing": n_missing,
                    "latency_ms": (self._mono() - t0) * 1000,
                }
            ]
        )


# ---------------------------------------------------------------------------- WebSocket recorder
class WsBookRecorder:
    """Persist a WebSocket stream faithfully.  Feed it every ``StreamEvent``."""

    def __init__(self, store: Store, run_id: str, *, flush_rows: int = 500) -> None:
        self.store, self.run_id, self.flush_rows = store, run_id, flush_rows
        self.proc = BookStreamProcessor()
        self._snap: list[dict] = []
        self._delta: list[dict] = []
        self._events: list[dict] = []
        self._trades: list = []
        self._quar: list[dict] = []
        self._status: list[dict] = []
        self._qseq = 0
        self.counters: dict[str, int] = defaultdict(int)

    def _incident(self, recv_ts_ns: int, epoch: int, kind: str, detail: str = "") -> None:
        self._events.append(
            {
                "run_id": self.run_id,
                "recv_ts_ns": recv_ts_ns,
                "epoch": epoch,
                "kind": kind,
                "detail": detail,
            }
        )

    def handle(self, ev) -> bool:
        """Record one stream event.  Returns True if the consumer should request a resync."""
        if isinstance(ev, Connected):
            self.proc.on_connected(ev.epoch)
            self._incident(ev.recv_ts_ns, ev.epoch, "connected", ev.url)
        elif isinstance(ev, Disconnected):
            self._incident(ev.recv_ts_ns, ev.epoch, "disconnected", ev.reason)
        elif isinstance(ev, Malformed):
            self._qseq += 1
            self.counters["malformed"] += 1
            self._quar.append(
                {
                    "run_id": self.run_id,
                    "recv_ts_ns": ev.recv_ts_ns,
                    "seq_no": self._qseq,
                    "source": "ws",
                    "error": ev.error[:2000],
                    "raw": ev.raw[:20000],
                }
            )
        elif isinstance(ev, WsOrderbookSnapshot | WsOrderbookDelta):
            self._record_book(ev)
        elif isinstance(ev, WsTrade):
            try:
                self._trades.append(normalize_ws_trade(ev))
            except MessageValidationError as exc:
                self._qseq += 1
                self._quar.append(
                    {
                        "run_id": self.run_id,
                        "recv_ts_ns": ev.recv_ts_ns,
                        "seq_no": self._qseq,
                        "source": "ws_trade",
                        "error": str(exc)[:2000],
                        "raw": ev.raw[:20000],
                    }
                )
        elif isinstance(ev, WsMarketLifecycle):
            sv = None
            try:
                sv = parse_price(ev.settlement_value) if ev.settlement_value else None
            except ValueError:
                pass
            self._status.append(
                {
                    "ticker": ev.market_ticker,
                    "observed_at": from_epoch_ns(ev.recv_ts_ns),
                    "source": "ws_lifecycle",
                    "status": "",
                    "result": ev.result or "",
                    "event_type": ev.event_type or "",
                    "settlement_value": sv,
                    "run_id": self.run_id,
                }
            )
        elif isinstance(ev, WsError):
            self._incident(ev.recv_ts_ns, ev.epoch, "error", f"{ev.code}: {ev.msg}")
        # ticker / control frames are intentionally not stored: top-of-book is derivable from books
        if (
            sum(
                map(
                    len,
                    (self._snap, self._delta, self._trades, self._events, self._quar, self._status),
                )
            )
            >= self.flush_rows
        ):
            self.flush()
        need = self.proc.needs_resync
        if need:
            self.proc.acknowledge_resync()
        return need

    def _record_book(self, ev: WsOrderbookSnapshot | WsOrderbookDelta) -> None:
        res = self.proc.process(ev)
        self.counters[res.status.value] += 1
        if res.status not in (ApplyStatus.APPLIED, ApplyStatus.DUPLICATE):
            self._incident(ev.recv_ts_ns, ev.epoch, res.status.value, res.detail)
        if ev.sid is None or ev.seq is None:
            return  # cannot key it; it is already an incident, and replay cannot use it
        if isinstance(ev, WsOrderbookSnapshot):
            book = OrderBook(ev.market_ticker)
            try:
                book.apply_snapshot(ev.yes_dollars_fp, ev.no_dollars_fp)
            except OrderBookInconsistency:
                return
            self._snap.append(
                snapshot_row(
                    book,
                    recv_ts_ns=ev.recv_ts_ns,
                    source="ws",
                    run_id=self.run_id,
                    epoch=ev.epoch,
                    sid=ev.sid,
                    seq=ev.seq,
                    exchange_ts=ev.exchange_ts,
                )
            )
        else:
            self._delta.append(
                {
                    "run_id": self.run_id,
                    "epoch": ev.epoch,
                    "sid": ev.sid,
                    "seq": ev.seq,
                    "ticker": ev.market_ticker,
                    "recv_ts_ns": ev.recv_ts_ns,
                    "exchange_ts": ev.exchange_ts,
                    "side": ev.side.value,
                    "price": ev.price_dollars,
                    "delta": ev.delta_fp,
                }
            )

    def flush(self) -> None:
        self.store.insert_book_snapshots(self._snap)
        self.store.insert_book_deltas(self._delta)
        self.store.insert_stream_events(self._events)
        self.store.insert_quarantine(self._quar)
        self.store.log_lifecycle(self._status)
        if self._trades:
            self.store.insert_trades(self._trades, source="ws", run_id=self.run_id)
        self._snap, self._delta, self._events, self._quar, self._status, self._trades = (
            [],
            [],
            [],
            [],
            [],
            [],
        )


async def record_ws(
    store: Store, ws: KalshiWebSocket, cfg: BooksConfig, *, git_commit: str = "unknown"
) -> dict:
    run_id = store.start_run("books_ws", to_plain(cfg), git_commit)
    rec = WsBookRecorder(store, run_id)
    deadline = None if cfg.duration_s is None else time.monotonic() + cfg.duration_s
    status = "ok"
    try:
        async for ev in ws.stream():
            if rec.handle(ev):
                ws.request_resync("book stream requires resync")
            if deadline is not None and time.monotonic() >= deadline:
                await ws.close()
                break
    except AuthenticationError:
        status = "failed"
        raise
    except (KeyboardInterrupt, asyncio.CancelledError):
        status = "interrupted"
        raise
    finally:
        rec.flush()
        store.finish_run(run_id, status)
    return dict(rec.counters)


__all__ = [
    "BookPoller",
    "WsBookRecorder",
    "record_ws",
    "select_book_tickers",
    "snapshot_row",
    "book_hash",
]
