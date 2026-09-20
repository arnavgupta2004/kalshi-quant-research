"""The paper trader: the Stage 5 engine, run live.

The engine already IS a simulated exchange: it applies feed events in time order, takes orders after
a modelled latency, fills them against the book and the tape with the same queue and taker models a
backtest uses, and keeps the ledger.  Paper trading therefore needs no new exchange, only a feed
that arrives in real time.  ``LiveFeed`` is that feed: a blocking iterator the engine (in its own
thread) pulls from, fed by an asyncio task that pumps a source (WebSocket, REST poll or tape).

Time.  The engine's clock is the events' own timestamps (local receive time).  When nothing has
arrived for ``tick_s`` the feed yields a ``tick``, so scheduled work (an order reaching the
"exchange", a fill notice, a strategy wake-up) happens on time instead of waiting for the next
market event.  A tick carries no market information and never reaches the strategy.

Same outputs as a backtest.  Every event handed to the engine is written to the tape.  At the end
of a session a fresh strategy is run over that tape with the plain engine: the orders, fills and
final equity must be IDENTICAL (``compare``).  The strategy sees only the events, and the events
carry all of time, so nothing else can differ.  A mismatch is a bug and is reported as one.

Safety.  Orders go to ``Backtest.submit``: a method that records an order in a simulated book.
Nothing in this package or in ``kalshi_client`` can send one anywhere else.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from backtest.engine import Backtest, BacktestConfig, BacktestResult
from backtest.events import BookUpdate, TradeTick, Wake
from backtest.market_info import MarketInfo
from backtest.metrics import summarize
from market.timeutil import now_ns
from paper import tape

_END = object()


class LiveFeed:
    """Queue-backed iterator for the engine thread; writes every event it yields to the tape."""

    def __init__(self, *, tick_s: float = 0.1, clock: Callable[[], int] = now_ns, tape_writer=None):
        self._q: queue.Queue = queue.Queue()
        self.tick_s, self._clock, self._tape = tick_s, clock, tape_writer
        self._last_ts = 0
        self.counts: Counter[str] = Counter()

    def push(self, ev: object) -> None:
        self._q.put(ev)

    def close(self) -> None:
        self._q.put(_END)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            ev = self._q.get(timeout=self.tick_s)
        except queue.Empty:
            ev = Wake(self._clock(), tape.TICK_TAG)
        if ev is _END:
            raise StopIteration
        if (
            ev.ts_ns < self._last_ts
        ):  # sources may be a few microseconds out of order: never go back
            ev = dataclasses.replace(ev, ts_ns=self._last_ts)
        self._last_ts = ev.ts_ns
        self.counts[type(ev).__name__] += 1
        if self._tape is not None:
            self._tape.write(ev)
        return ev


class DecisionLog:
    """One JSON object per line: what the strategy did and what happened to it."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("w", encoding="utf-8")
        self._lock = threading.Lock()
        self.count = 0

    def write(self, kind: str, **fields) -> None:
        with self._lock:
            self._f.write(json.dumps({"kind": kind, **fields}, default=str) + "\n")
            self._f.flush()
            self.count += 1

    def close(self) -> None:
        self._f.close()


class _Logged:
    """Wraps a strategy: logs its orders, cancels, the fills that follow, and its reaction time."""

    def __init__(self, inner, log: DecisionLog) -> None:
        self.inner, self.log = inner, log
        self.bt: Backtest | None = None
        self.lag_ms: list[float] = []
        self._nfill = 0
        self._killed = False

    def attach(self, bt: Backtest) -> None:
        self.bt = bt
        ctx = bt.ctx
        place, cancel = ctx.place, ctx.cancel

        def logged_place(order):
            oid = place(order)
            view = ctx.book(order.ticker)
            pos = ctx.position(order.ticker)
            self.log.write(
                "order",
                ts=ctx.now,
                id=oid,
                ticker=order.ticker,
                side=order.side.value,
                price=order.price,
                qty=order.qty,
                tif=order.tif.value,
                tag=order.tag,
                mid=None if view is None else view.mid(),
                net_yes=pos.net_yes,
                equity=ctx.equity(),
            )
            return oid

        def logged_cancel(order_id):
            cancel(order_id)
            self.log.write("cancel", ts=ctx.now, id=order_id)

        ctx.place, ctx.cancel = logged_place, logged_cancel

    def _drain(self) -> None:
        assert self.bt is not None
        for f in self.bt.fill_log[self._nfill :]:
            self.log.write(
                "fill",
                ts=f.exec_ts,
                id=f.order_id,
                ticker=f.ticker,
                side=f.side.value,
                price=f.price,
                qty=f.qty,
                fee=f.fee_micro,
                liquidity=f.liquidity,
            )
        self._nfill = len(self.bt.fill_log)
        risk = getattr(self.inner, "risk", None)
        if risk is not None and risk.state.killed and not self._killed:
            self._killed = True
            self.log.write("risk", ts=self.bt.now, event="kill-switch", reason=risk.state.reason)

    def on_event(self, ctx, ev) -> None:
        if isinstance(ev, BookUpdate | TradeTick):
            self.lag_ms.append((time.time_ns() - ev.ts_ns) / 1e6)
        self.inner.on_event(ctx, ev)
        self._drain()

    def on_end(self, ctx) -> None:
        self._drain()
        if hasattr(self.inner, "on_end"):
            self.inner.on_end(ctx)


# --------------------------------------------------------------------------- parity
def signature(res: BacktestResult) -> dict:
    return {
        "orders": [
            (
                o.submitted_ts,
                o.ticker,
                o.side.value,
                o.price,
                o.qty,
                o.tif.value,
                o.status,
                o.filled,
            )
            for o in res.orders
        ],
        "fills": [
            (
                f.exec_ts,
                f.order_id,
                f.ticker,
                f.side.value,
                f.price,
                f.qty,
                f.fee_micro,
                f.liquidity,
            )
            for f in res.fills
        ],
        "final_equity_micro": res.final_equity_micro,
        "settlements": list(res.settlements),
    }


def replay(
    tape_path, infos: dict[str, MarketInfo], strategy, cfg: BacktestConfig
) -> BacktestResult:
    """The plain backtest engine over a recorded tape."""
    return Backtest(list(tape.read(tape_path)), infos, strategy, cfg).run()


def compare(live: BacktestResult, offline: BacktestResult) -> dict:
    a, b = signature(live), signature(offline)
    diffs = [k for k in a if a[k] != b[k]]
    return {
        "identical": not diffs,
        "differs_in": diffs,
        "orders": len(a["orders"]),
        "fills": len(a["fills"]),
    }


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    ys = sorted(xs)

    def q(p: float) -> float:
        return ys[min(len(ys) - 1, int(p * len(ys)))]

    return {"n": len(ys), "p50": q(0.5), "p90": q(0.9), "p99": q(0.99), "max": ys[-1]}


class PaperTrader:
    def __init__(
        self,
        infos: dict[str, MarketInfo],
        make_strategy: Callable[[], object],
        cfg: BacktestConfig,
        out_dir: str | Path,
        *,
        tick_s: float = 0.1,
    ) -> None:
        self.infos, self.make_strategy, self.cfg = infos, make_strategy, cfg
        self.out = Path(out_dir)
        self.out.mkdir(parents=True, exist_ok=True)
        self.tape = tape.TapeWriter(self.out / "tape.jsonl")
        self.log = DecisionLog(self.out / "decisions.jsonl")
        self.feed = LiveFeed(tick_s=tick_s, tape_writer=self.tape)
        self.strategy = make_strategy()
        self._logged = _Logged(self.strategy, self.log)
        self._result: BacktestResult | None = None
        self._error: BaseException | None = None

    # ------------------------------------------------------------------ the engine thread
    def _engine(self) -> None:
        try:
            bt = Backtest(self.feed, self.infos, self._logged, self.cfg)
            self._logged.attach(bt)
            self._result = bt.run()
        except BaseException as exc:  # surfaced to the caller, never swallowed
            self._error = exc

    # ------------------------------------------------------------------ the session
    async def run(self, source, *, duration_s: float | None = None) -> dict:
        thread = threading.Thread(target=self._engine, name="paper-engine", daemon=True)
        thread.start()
        t0 = time.monotonic()
        n = 0
        it = source.events().__aiter__()
        try:
            while True:
                remaining = None if duration_s is None else duration_s - (time.monotonic() - t0)
                if remaining is not None and remaining <= 0:
                    break
                try:
                    ev = await asyncio.wait_for(it.__anext__(), timeout=remaining)
                except (StopAsyncIteration, TimeoutError):
                    break
                self.feed.push(ev)
                n += 1
                if self._error is not None:
                    break
        finally:
            await source.aclose()
            self.feed.close()
            await asyncio.to_thread(thread.join)
            self.tape.close()
        if self._error is not None:
            self.log.close()
            raise self._error
        assert self._result is not None
        summary = self._summary(self._result, source, time.monotonic() - t0, n)
        self.log.close()
        return summary

    # ------------------------------------------------------------------ the report
    def _summary(self, res: BacktestResult, source, wall_s: float, n_events: int) -> dict:
        offline = replay(self.out / "tape.jsonl", self.infos, self.make_strategy(), self.cfg)
        summ = summarize(res)
        marks = res.portfolio.positions
        held = {
            t: {"yes": p.yes / 100, "no": p.no / 100}
            for t, p in marks.items()
            if not (p.yes == 0 and p.no == 0)
        }
        out = {
            "mode": "PAPER: no order was ever sent to any exchange",
            "wall_seconds": wall_s,
            "events_from_source": n_events,
            "events_to_engine": dict(self.feed.counts),
            "markets": len(self.infos),
            "orders": len(res.orders),
            "fills": len(res.fills),
            "net_pnl_usd": res.pnl_micro / 1e6,
            "fees_usd": res.portfolio.fees / 1e6,
            "positions_held": held,
            "reaction_ms": _stats(self._logged.lag_ms),
            "incidents": [dataclasses.asdict(i) for i in getattr(source, "incidents", [])][:200],
            "n_incidents": len(getattr(source, "incidents", [])),
            "kill_switch": getattr(getattr(self.strategy, "risk", None), "state", None)
            and self.strategy.risk.state.killed,
            "strategy_counters": {
                k: getattr(self.strategy, k)
                for k in ("opportunities_seen", "orders_sent", "decisions")
                if hasattr(self.strategy, k)
            },
            "decisions_logged": self.log.count,
            "metrics": {k: summ[k] for k in ("pnl", "execution", "risk")},
            "parity_with_backtest_of_the_tape": compare(res, offline),
            "files": {
                "tape": str(self.out / "tape.jsonl"),
                "decisions": str(self.out / "decisions.jsonl"),
            },
        }
        (self.out / "summary.json").write_text(json.dumps(out, indent=1, default=str))
        return out
