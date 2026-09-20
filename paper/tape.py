"""The tape: every event the strategy was shown, one JSON object per line.

A live paper-trading session writes its tape as it goes.  Replaying the tape through a fresh copy of
the same strategy and engine must reproduce the session's orders and fills EXACTLY: the strategy
sees nothing but these events, and time in the engine is the events' own timestamps.  That identity
is the sense in which "paper trading produces the same outputs as backtesting", and it is checked
(``paper.trader.compare``) at the end of every session.

    book     full-depth snapshot of one market (levels best-first)
    confirm  the book(s) are known to be current as of ``ts`` (what a poll log gives a backtest)
    trade    a public print
    close    trading stopped in a market
    settle   the market's outcome
    tick     the clock advanced (no market data; lets scheduled orders and fills be processed)
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from backtest.events import BookConfirm, BookUpdate, MarketClose, Settlement, TradeTick, Wake
from market.contracts import Side, Trade

TICK_TAG = "clock"


def dump(ev: object) -> dict:
    if isinstance(ev, BookUpdate):
        return {
            "k": "book",
            "ts": ev.ts_ns,
            "t": ev.ticker,
            "y": [list(x) for x in ev.yes_bids],
            "n": [list(x) for x in ev.no_bids],
        }
    if isinstance(ev, BookConfirm):
        return {"k": "confirm", "ts": ev.ts_ns, "t": ev.ticker}
    if isinstance(ev, TradeTick):
        tr = ev.trade
        return {
            "k": "trade",
            "ts": ev.ts_ns,
            "t": ev.ticker,
            "id": tr.trade_id,
            "yp": tr.yes_price,
            "np": tr.no_price,
            "c": tr.count,
            "side": None if tr.taker_side is None else tr.taker_side.value,
            "bs": tr.taker_book_side,
            "created": tr.created_time.isoformat(),
            "block": tr.is_block_trade,
        }
    if isinstance(ev, MarketClose):
        return {"k": "close", "ts": ev.ts_ns, "t": ev.ticker}
    if isinstance(ev, Settlement):
        return {"k": "settle", "ts": ev.ts_ns, "t": ev.ticker, "v": ev.value}
    if isinstance(ev, Wake) and ev.tag == TICK_TAG:
        return {"k": "tick", "ts": ev.ts_ns}
    raise TypeError(f"cannot write {type(ev).__name__} to a tape")


def load(d: dict):
    k = d["k"]
    if k == "book":
        return BookUpdate(
            d["ts"],
            d["t"],
            tuple((p, q) for p, q in d["y"]),
            tuple((p, q) for p, q in d["n"]),
        )
    if k == "confirm":
        return BookConfirm(d["ts"], d.get("t", ""))
    if k == "trade":
        trade = Trade(
            d["id"],
            d["t"],
            d["yp"],
            d["np"],
            d["c"],
            None if d["side"] is None else Side(d["side"]),
            d["bs"],
            datetime.fromisoformat(d["created"]),
            d.get("block", False),
        )
        return TradeTick(d["ts"], d["t"], trade)
    if k == "close":
        return MarketClose(d["ts"], d["t"])
    if k == "settle":
        return Settlement(d["ts"], d["t"], d["v"])
    if k == "tick":
        return Wake(d["ts"], TICK_TAG)
    raise ValueError(f"unknown tape record kind {k!r}")


class TapeWriter:
    """Append events to ``path``; flushed on every write so a crash loses nothing."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")
        self.count = 0

    def write(self, ev: object) -> None:
        self._f.write(json.dumps(dump(ev), separators=(",", ":")) + "\n")
        self._f.flush()
        self.count += 1

    def close(self) -> None:
        self._f.close()


def read(path: str | Path) -> Iterator[object]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield load(json.loads(line))


def write_all(path: str | Path, events: Iterable[object]) -> int:
    w = TapeWriter(path)
    for e in events:
        w.write(e)
    w.close()
    return w.count
