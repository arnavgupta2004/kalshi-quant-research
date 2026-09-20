"""WebSocket frames -> the engine's feed events.

``WsAdapter`` is a pure state machine (no I/O, no clock of its own), so it is tested by feeding it
scripted frames.  It reuses the Stage 2 ``BookStreamProcessor``, which enforces per-subscription
sequence numbers and FAILS CLOSED: a gap, a duplicate storm or an inconsistent book poisons the
affected markets, ``book()`` returns ``None`` for them, and a resync (reconnect, hence a fresh
snapshot) is requested.

What the strategy is shown, and why
  * ``BookUpdate``   after every applied snapshot or delta: the full book of that market.
  * ``TradeTick``    every public print, stamped with the LOCAL receive time (information time).
  * ``BookConfirm``  every ``confirm_s`` for each market whose book is trustworthy, while the
                     connection is healthy.  A backtest gets this from the poll log; here it tells
                     a quiet market "your book is still current".  When the connection drops, a
                     frame is missing or a book is poisoned, the confirmations STOP, the book ages,
                     and the strategy's own age limit takes its quotes off the market.
  * ``MarketClose`` / ``Settlement``  from the market-lifecycle channel.

ASSUMPTIONS that cannot be verified without credentials (the WebSocket needs an API key): the
lifecycle ``event_type`` strings ("closed") and that ``settlement_value`` arrives as a dollar
string.  Anything unrecognised is counted and ignored, never guessed at.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from backtest.events import BookConfirm, BookUpdate, MarketClose, Settlement, TradeTick
from data.normalization.book_stream import BookStreamProcessor
from data.normalization.normalizer import normalize_ws_trade
from kalshi_client.exceptions import MessageValidationError
from kalshi_client.models import (
    WsError,
    WsMarketLifecycle,
    WsOrderbookDelta,
    WsOrderbookSnapshot,
    WsTrade,
)
from kalshi_client.websocket import Connected, Disconnected, Malformed
from market.contracts import Side
from market.units import parse_price

SEC = 1_000_000_000


@dataclass(frozen=True)
class Incident:
    ts_ns: int
    kind: str
    detail: str = ""


def levels(book, side: Side) -> tuple[tuple[int, int], ...]:
    return tuple((lv.price, lv.qty) for lv in book.bids(side))


class WsAdapter:
    def __init__(self, tickers, *, confirm_s: float = 1.0, stale_s: float = 15.0) -> None:
        self.universe = set(tickers)
        self.proc = BookStreamProcessor()
        self.confirm_s, self.stale_s = confirm_s, stale_s
        self.connected = False
        self.last_frame_ns = 0
        self._last_confirm_ns = 0
        self.incidents: list[Incident] = []
        self.counters: Counter[str] = Counter()

    # ------------------------------------------------------------------ health
    def healthy(self, now_ns: int) -> bool:
        """Connected and heard from within ``stale_s``."""
        return self.connected and (now_ns - self.last_frame_ns) <= self.stale_s * SEC

    # ------------------------------------------------------------------ frames
    def on_stream_event(self, ev) -> tuple[list, bool]:
        """Convert one stream event.  Returns ``(feed_events, needs_resync)``."""
        out: list = []
        if isinstance(ev, Connected):
            self.proc.on_connected(ev.epoch)
            self.connected = True
            self.last_frame_ns = ev.recv_ts_ns
            self.incidents.append(Incident(ev.recv_ts_ns, "connected", ev.url))
            return out, False
        if isinstance(ev, Disconnected):
            self.connected = False
            self.incidents.append(Incident(ev.recv_ts_ns, "disconnected", ev.reason))
            return out, False
        if isinstance(ev, Malformed):
            self.counters["malformed"] += 1
            self.incidents.append(Incident(ev.recv_ts_ns, "malformed", ev.error[:200]))
            return out, False
        ts = getattr(ev, "recv_ts_ns", 0)
        self.last_frame_ns = max(self.last_frame_ns, ts)
        if isinstance(ev, WsOrderbookSnapshot | WsOrderbookDelta):
            res = self.proc.process(ev)
            self.counters[f"book_{res.status.value}"] += 1
            if res.applied and ev.market_ticker in self.universe:
                book = self.proc.book(ev.market_ticker)
                if book is not None:
                    out.append(
                        BookUpdate(
                            ts, ev.market_ticker, levels(book, Side.YES), levels(book, Side.NO)
                        )
                    )
            elif not res.applied:
                self.incidents.append(Incident(ts, res.status.value, res.detail[:200]))
        elif isinstance(ev, WsTrade):
            if ev.market_ticker in self.universe:
                try:
                    trade = normalize_ws_trade(ev)
                except MessageValidationError as exc:
                    self.counters["trade_rejected"] += 1
                    self.incidents.append(Incident(ts, "trade_rejected", str(exc)[:200]))
                else:
                    out.append(TradeTick(ts, ev.market_ticker, trade))
                    self.counters["trades"] += 1
        elif isinstance(ev, WsMarketLifecycle):
            out += self._lifecycle(ev, ts)
        elif isinstance(ev, WsError):
            self.incidents.append(Incident(ts, "error", f"{ev.code}: {ev.msg}"))
        need = self.proc.needs_resync
        if need:
            self.proc.acknowledge_resync()
            self.incidents.append(Incident(ts, "resync_requested"))
        return out, need

    def _lifecycle(self, ev: WsMarketLifecycle, ts: int) -> list:
        if ev.market_ticker not in self.universe:
            return []
        if ev.settlement_value:
            try:
                return [Settlement(ts, ev.market_ticker, parse_price(ev.settlement_value))]
            except ValueError:
                self.counters["lifecycle_unparsed"] += 1
                return []
        if (ev.event_type or "").lower() == "closed":
            return [MarketClose(ts, ev.market_ticker)]
        self.counters["lifecycle_ignored"] += 1
        return []

    # ------------------------------------------------------------------ liveness
    def tick(self, now_ns: int) -> list:
        """Confirmations due at ``now_ns`` (none unless healthy), one per trustworthy market."""
        if not self.healthy(now_ns) or now_ns - self._last_confirm_ns < self.confirm_s * SEC:
            return []
        self._last_confirm_ns = now_ns
        return [BookConfirm(now_ns, t) for t in sorted(self.universe) if self.proc.book(t)]
