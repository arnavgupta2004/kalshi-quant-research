"""Stage 1 demo: live Kalshi data -> validated, normalised local state.

    python -m scripts.stage1_demo --markets 5 --seconds 30

* With API credentials (KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH) it streams the
  ``orderbook_delta`` / ``ticker`` / ``trade`` WebSocket channels and maintains local books
  through the sequence-checking ``BookStreamProcessor``.
* Without credentials the WebSocket is unavailable (the exchange returns 401), so it falls
  back to polling the public REST order-book / trades endpoints.  The mode is printed.

Read-only: nothing here can place an order.  Raw + normalised events are appended to a
JSONL file under ``var/`` (gitignored); durable storage arrives in Stage 2.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

from data.normalization.book_stream import ApplyStatus, BookStreamProcessor, SeenSet
from data.normalization.normalizer import (
    normalize_event,
    normalize_market,
    normalize_trade,
    normalize_ws_trade,
    orderbook_from_rest,
)
from kalshi_client.config import KalshiConfig
from kalshi_client.models import ApiMarket, WsOrderbookDelta, WsOrderbookSnapshot, WsTicker, WsTrade
from kalshi_client.rest import KalshiRestClient
from kalshi_client.websocket import Connected, Disconnected, KalshiWebSocket, Malformed
from market.contracts import Side
from market.order_book import OrderBook
from market.timeutil import now_ns
from market.units import format_price, format_qty

log = logging.getLogger("stage1")


def book_line(ticker: str, book: OrderBook | None) -> str:
    if book is None:
        return f"  {ticker:<48} <no trustworthy book>"
    yb, ya = book.best_bid(Side.YES), book.best_ask(Side.YES)

    def fmt(lv) -> str:
        return (
            "      -      " if lv is None else f"{format_price(lv.price)} x {format_qty(lv.qty):>9}"
        )

    spread = "-" if book.spread() is None else format_price(book.spread())
    return (
        f"  {ticker:<48} YES bid {fmt(yb)} | ask {fmt(ya)} | spread {spread}"
        f" | depth {format_qty(book.depth(Side.YES))}/{format_qty(book.depth(Side.NO))}"
    )


async def pick_markets(rest: KalshiRestClient, n: int) -> list[ApiMarket]:
    """Most active open, non-combo markets that currently have a two-sided quote."""
    pool = [
        m
        async for m in rest.iter_markets(status="open", limit=4000)
        if (m.yes_bid_size_fp or 0) > 0 and (m.yes_ask_size_fp or 0) > 0
    ]
    pool.sort(key=lambda m: (m.volume_24h_fp or 0, m.open_interest_fp or 0), reverse=True)
    return pool[:n]


async def describe(rest: KalshiRestClient, markets: list[ApiMarket]) -> None:
    print("\n== Normalised contract metadata ==")
    events: dict[str, object] = {}
    for m in markets:
        if m.event_ticker not in events:
            events[m.event_ticker] = normalize_event(
                await rest.get_event(m.event_ticker, with_nested_markets=False)
            )
        ev = events[m.event_ticker]
        mk = normalize_market(m, event=ev)
        print(
            f"  {mk.ticker}\n    event: {ev.title!r} [{ev.category}]"
            f" mutually_exclusive={ev.mutually_exclusive}"
            f"\n    market: {mk.title!r} | strike={mk.strike.strike_type}/{mk.strike.floor}"
            f" | closes {mk.close_time:%Y-%m-%d %H:%MZ} | tick={mk.price_level_structure}"
            f"\n    rule: {mk.rules.primary[:110]!r}"
        )


async def run_rest_polling(rest, tickers, seconds, interval, sink) -> dict:
    print(f"\n== REST polling mode ({interval:.0f}s interval): no WebSocket credentials ==")
    seen, last = SeenSet(), {}
    counters = {"polls": 0, "book_changes": 0, "new_trades": 0}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        apis = await rest.get_orderbooks(tickers)
        counters["polls"] += 1
        print(f"\n[{time.strftime('%H:%M:%S')}] poll {counters['polls']}")
        for t in tickers:
            book = orderbook_from_rest(t, apis[t]) if t in apis else None
            print(book_line(t, book))
            if book is not None and (state := book.as_levels()) != last.get(t):
                if t in last:
                    counters["book_changes"] += 1
                last[t] = state
                sink({"kind": "book", "ticker": t, "recv_ts_ns": now_ns(), "levels": state})
            async for tr in rest.iter_trades(ticker=t, limit=20):
                if seen.add(tr.trade_id):
                    counters["new_trades"] += 1
                    sink(
                        {
                            "kind": "trade",
                            "recv_ts_ns": now_ns(),
                            "trade": asdict(normalize_trade(tr)),
                        }
                    )
        await asyncio.sleep(max(0.0, interval - (time.monotonic() - t0)))
    return counters


async def run_websocket(cfg, tickers, seconds, sink) -> dict:
    print("\n== WebSocket mode: orderbook_delta + ticker + trade ==")
    ws = KalshiWebSocket(
        cfg, channels=["orderbook_delta", "ticker", "trade"], market_tickers=tickers
    )
    proc, seen = BookStreamProcessor(), SeenSet()
    counters = {"frames": 0, "trades": 0, "tickers": 0, "malformed": 0, "reconnects": 0}
    deadline, next_print = time.monotonic() + seconds, 0.0
    async for ev in ws.stream():
        if isinstance(ev, Connected):
            proc.on_connected(ev.epoch)
            counters["reconnects"] += ev.epoch > 1
            print(f"connected (epoch {ev.epoch})")
        elif isinstance(ev, Disconnected):
            print(f"disconnected: {ev.reason}")
        elif isinstance(ev, Malformed):
            counters["malformed"] += 1
            sink({"kind": "malformed", "epoch": ev.epoch, "error": ev.error, "raw": ev.raw})
        else:
            counters["frames"] += 1
            sink({"kind": ev.type, "epoch": ev.epoch, "recv_ts_ns": ev.recv_ts_ns, "raw": ev.raw})
            if isinstance(ev, WsOrderbookSnapshot | WsOrderbookDelta):
                res = proc.process(ev)
                if (
                    res.status is not ApplyStatus.APPLIED
                    and res.status is not ApplyStatus.DUPLICATE
                ):
                    print(f"  ! {res.status.value}: {res.detail}")
                if proc.needs_resync:
                    ws.request_resync(res.detail)
                    proc.acknowledge_resync()
            elif isinstance(ev, WsTrade) and seen.add(ev.trade_id):
                counters["trades"] += 1
                normalize_ws_trade(ev)
            elif isinstance(ev, WsTicker):
                counters["tickers"] += 1
        if time.monotonic() >= next_print:
            next_print = time.monotonic() + 2
            print(f"\n[{time.strftime('%H:%M:%S')}] {dict(proc.counters)}")
            for t in tickers:
                print(book_line(t, proc.book(t)))
        if time.monotonic() >= deadline:
            await ws.close()
            break
    return counters | {"book": dict(proc.counters)}


async def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--markets", type=int, default=5)
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--poll-interval", type=float, default=3.0)
    ap.add_argument("--out", type=Path, default=Path("var/stage1_live.jsonl"))
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    cfg = KalshiConfig.from_env()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    n_lines = 0
    with args.out.open("a") as f:

        def sink(obj: dict) -> None:
            nonlocal n_lines
            f.write(json.dumps(obj, default=str) + "\n")
            n_lines += 1

        async with KalshiRestClient(cfg) as rest:
            status = await rest.get_exchange_status()
            print(
                f"exchange_active={status['exchange_active']}"
                f" trading_active={status['trading_active']}"
            )
            markets = await pick_markets(rest, args.markets)
            tickers = [m.ticker for m in markets]
            await describe(rest, markets)
            if cfg.auth is not None:
                counters = await run_websocket(cfg, tickers, args.seconds, sink)
            else:
                counters = await run_rest_polling(
                    rest, tickers, args.seconds, args.poll_interval, sink
                )
            print(f"\nREST requests: {rest.stats}")
    print(f"summary: {counters}\nwrote {n_lines} records to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
