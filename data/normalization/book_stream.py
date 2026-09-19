"""Turn a stream of order-book messages into trustworthy local ``OrderBook`` state.

The exchange sends one ``orderbook_snapshot`` per market and then ``orderbook_delta`` frames.
Each frame carries ``(sid, seq)``.  Per the docs, ``seq`` increases by one for every frame on
a subscription (``sid``), so this processor enforces, per ``(epoch, sid)``:

  * ``seq == last + 1``  -> apply
  * ``seq == last``      -> ``DUPLICATE``     (dropped, counted)
  * ``seq <  last``      -> ``OUT_OF_ORDER``  (dropped, counted)
  * ``seq >  last + 1``  -> ``GAP``           (a message was lost: every book on that ``sid`` is
                            untrustworthy until re-snapshotted, so it is *poisoned* and
                            ``needs_resync`` is raised)

Design choice - *fail closed*: ``book(ticker)`` returns ``None`` (never a stale or guessed
book) whenever the book is unknown, poisoned, or flagged inconsistent.  A dataset built from
this class can therefore not silently contain a corrupted book; the worst case is a hole
that is explicitly reported.

ASSUMPTION (unverified without credentials at time of writing): ``seq`` is contiguous per
``sid`` across all markets of that subscription, as the docs state.  If the live exchange
disagrees, this shows up immediately as a stream of ``GAP`` results and is a one-line fix.
"""

from __future__ import annotations

import logging
from collections import Counter, OrderedDict
from dataclasses import dataclass
from enum import StrEnum

from kalshi_client.models import WsMessage, WsOrderbookDelta, WsOrderbookSnapshot
from market.order_book import OrderBook, OrderBookInconsistency

log = logging.getLogger(__name__)


class ApplyStatus(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    OUT_OF_ORDER = "out_of_order"
    GAP = "gap"
    NO_SNAPSHOT = "no_snapshot"
    INCONSISTENT = "inconsistent"
    STALE_STREAM = "stale_stream"
    NOT_A_BOOK_MESSAGE = "not_a_book_message"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    status: ApplyStatus
    ticker: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.status is ApplyStatus.APPLIED


class BookStreamProcessor:
    def __init__(self) -> None:
        self._books: dict[str, OrderBook] = {}
        self._ticker_stream: dict[str, tuple[int, int]] = {}
        self._last_seq: dict[tuple[int, int], int] = {}
        self._poisoned: set[tuple[int, int]] = set()
        self._stale_tickers: set[str] = set()
        self._needs_resync = False
        self.counters: Counter[str] = Counter()

    # ------------------------------------------------------------------ lifecycle
    def on_connected(self, epoch: int) -> None:
        """A new connection generation: all previous book state is void until re-snapshotted."""
        self._books.clear()
        self._ticker_stream.clear()
        self._last_seq = {k: v for k, v in self._last_seq.items() if k[0] == epoch}
        self._poisoned = {k for k in self._poisoned if k[0] == epoch}
        self._stale_tickers.clear()
        self._needs_resync = False
        self.counters["reconnects"] += 1

    @property
    def needs_resync(self) -> bool:
        return self._needs_resync

    def acknowledge_resync(self) -> None:
        self._needs_resync = False

    # ------------------------------------------------------------------ reading
    def book(self, ticker: str) -> OrderBook | None:
        """The current book, or ``None`` if it is not currently trustworthy."""
        book = self._books.get(ticker)
        if book is None or ticker in self._stale_tickers:
            return None
        if self._ticker_stream.get(ticker) in self._poisoned:
            return None
        return book

    @property
    def tickers(self) -> list[str]:
        return sorted(self._books)

    # ------------------------------------------------------------------ processing
    def _sequence(self, msg: WsOrderbookSnapshot | WsOrderbookDelta) -> ProcessResult | None:
        """Validate ``seq``.  Returns a rejecting result, or ``None`` if the frame may proceed."""
        if msg.sid is None or msg.seq is None:
            return self._reject(
                ApplyStatus.INCONSISTENT, msg.market_ticker, "missing sid/seq", resync=True
            )
        key = (msg.epoch, msg.sid)
        if key in self._poisoned:
            return self._reject(
                ApplyStatus.STALE_STREAM, msg.market_ticker, f"sid {msg.sid} poisoned"
            )
        last = self._last_seq.get(key)
        if last is not None:
            if msg.seq == last:
                return self._reject(ApplyStatus.DUPLICATE, msg.market_ticker, f"seq {msg.seq}")
            if msg.seq < last:
                return self._reject(
                    ApplyStatus.OUT_OF_ORDER, msg.market_ticker, f"seq {msg.seq} < {last}"
                )
            if msg.seq != last + 1:
                self._poisoned.add(key)
                return self._reject(
                    ApplyStatus.GAP,
                    msg.market_ticker,
                    f"sid {msg.sid}: expected {last + 1}, got {msg.seq}",
                    resync=True,
                )
        self._last_seq[key] = msg.seq
        self._ticker_stream[msg.market_ticker] = key
        return None

    def _reject(
        self, status: ApplyStatus, ticker: str | None, detail: str, *, resync: bool = False
    ) -> ProcessResult:
        self.counters[status.value] += 1
        if resync:
            self._needs_resync = True
            log.warning("book stream %s [%s]: %s - resync required", status.value, ticker, detail)
        return ProcessResult(status, ticker, detail)

    def process(self, msg: WsMessage) -> ProcessResult:
        if isinstance(msg, WsOrderbookSnapshot):
            return self._on_snapshot(msg)
        if isinstance(msg, WsOrderbookDelta):
            return self._on_delta(msg)
        return ProcessResult(ApplyStatus.NOT_A_BOOK_MESSAGE)

    def _on_snapshot(self, msg: WsOrderbookSnapshot) -> ProcessResult:
        if (rejected := self._sequence(msg)) is not None:
            return rejected
        book = self._books.get(msg.market_ticker) or OrderBook(msg.market_ticker)
        try:
            book.apply_snapshot(msg.yes_dollars_fp, msg.no_dollars_fp)
        except OrderBookInconsistency as exc:
            self._stale_tickers.add(msg.market_ticker)
            return self._reject(ApplyStatus.INCONSISTENT, msg.market_ticker, str(exc), resync=True)
        book.last_seq, book.last_ts = msg.seq, msg.exchange_ts
        self._books[msg.market_ticker] = book
        self._stale_tickers.discard(msg.market_ticker)  # a snapshot repairs the book
        self.counters["snapshots"] += 1
        return ProcessResult(ApplyStatus.APPLIED, msg.market_ticker)

    def _on_delta(self, msg: WsOrderbookDelta) -> ProcessResult:
        if (rejected := self._sequence(msg)) is not None:
            return rejected
        book = self._books.get(msg.market_ticker)
        if book is None or msg.market_ticker in self._stale_tickers:
            return self._reject(
                ApplyStatus.NO_SNAPSHOT, msg.market_ticker, "delta before snapshot", resync=True
            )
        try:
            book.apply_delta(msg.side, msg.price_dollars, msg.delta_fp)
        except OrderBookInconsistency as exc:
            self._stale_tickers.add(msg.market_ticker)
            return self._reject(ApplyStatus.INCONSISTENT, msg.market_ticker, str(exc), resync=True)
        book.last_seq, book.last_ts = msg.seq, msg.exchange_ts
        if book.is_crossed():
            # counted, not fatal: whether transient crossing can occur mid-batch is unverified
            self.counters["crossed_observed"] += 1
        self.counters["deltas"] += 1
        return ProcessResult(ApplyStatus.APPLIED, msg.market_ticker)


class SeenSet:
    """Bounded LRU set for duplicate suppression (e.g. of ``trade_id``s)."""

    def __init__(self, maxlen: int = 100_000) -> None:
        self._maxlen = maxlen
        self._items: OrderedDict[str, None] = OrderedDict()

    def add(self, key: str) -> bool:
        """Record ``key``; returns ``True`` if it was new, ``False`` if already seen."""
        if key in self._items:
            self._items.move_to_end(key)
            return False
        self._items[key] = None
        if len(self._items) > self._maxlen:
            self._items.popitem(last=False)
        return True

    def __len__(self) -> int:
        return len(self._items)
