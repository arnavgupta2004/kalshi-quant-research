"""An in-memory fake of the parts of the Kalshi REST API the collectors use.

It reproduces the quirks found on the live exchange, because those are what the collectors
must survive:
  * data is split at a cutoff: ``/historical/*`` holds trades/markets before it, ``/markets*``
    after it;
  * ``/historical/markets`` **ignores** close-time filters and is ordered by ``created_time``
    descending; live ``/markets`` honours ``min_close_ts``/``max_close_ts``;
  * trades are served newest-first with opaque cursors, ``min_ts`` in epoch seconds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from kalshi_client.config import KalshiConfig
from kalshi_client.rest import KalshiRestClient

CUTOFF = datetime(2026, 7, 20, tzinfo=UTC)
BASE = "https://fake.test/trade-api/v2"


def iso(d: datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def make_market(
    fx,
    ticker,
    *,
    event=None,
    close,
    created=None,
    opened=None,
    volume=500.0,
    result="yes",
    status="finalized",
):
    m = dict(fx("market_settled_yes.json"))
    for k in ("mve_collection_ticker",):
        m.pop(k, None)
    settled = close + timedelta(minutes=5)
    m.update(
        ticker=ticker,
        event_ticker=event or ticker.rsplit("-", 1)[0],
        status=status,
        result=result,
        close_time=iso(close),
        created_time=iso(created or close - timedelta(days=2)),
        open_time=iso(opened or close - timedelta(days=1)),
        settlement_ts=iso(settled),
        expiration_time=iso(close),
        latest_expiration_time=iso(close),
        expected_expiration_time=iso(close),
        volume_fp=f"{volume:.2f}",
        settlement_value_dollars="1.0000" if result == "yes" else "0.0000",
    )
    if status != "finalized":
        m.update(result="", settlement_value_dollars="", settlement_ts="")
    return m


def make_trade(ticker, i, when):
    return {
        "trade_id": f"{ticker}#{i}",
        "ticker": ticker,
        "created_time": iso(when),
        "yes_price_dollars": "0.4000",
        "no_price_dollars": "0.6000",
        "count_fp": f"{10 + i}.00",
        "taker_side": "yes",
        "taker_outcome_side": "yes",
        "taker_book_side": "bid",
        "is_block_trade": False,
    }


class SimulatedCrash(RuntimeError):
    """A bug/kill in the middle of a run (not an API error the collector is meant to absorb)."""


class FakeExchange:
    def __init__(self, fx, live=(), hist=(), trades=None, events=None, page=3):
        self.fx = fx
        self.live, self.hist = list(live), list(hist)
        self.trades = trades or {}
        self.events = events if events is not None else {}
        self.page = page
        self.requests: list[httpx.Request] = []
        self.crash_after: int | None = None  # raise SimulatedCrash after N requests
        self.fail_tickers: set[str] = set()  # trades endpoint -> HTTP 500 for these
        self.bad_trade_ids: set[str] = set()  # served with an off-grid price

    # ---- helpers
    def paginate(self, items, request, key):
        limit = int(request.url.params.get("limit", self.page))
        limit = min(limit, self.page)
        try:
            start = int(request.url.params.get("cursor") or 0)
        except ValueError:
            start = 0  # like the real API: an invalid cursor silently means "page 1"
        chunk = items[start : start + limit]
        nxt = start + limit
        body = {key: chunk, "cursor": str(nxt) if nxt < len(items) else ""}
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.crash_after is not None and len(self.requests) > self.crash_after:
            raise SimulatedCrash("simulated crash")
        path, q = request.url.path.removeprefix("/trade-api/v2"), request.url.params
        if path == "/historical/cutoff":
            return httpx.Response(
                200, json={"trades_created_ts": iso(CUTOFF), "market_settled_ts": iso(CUTOFF)}
            )
        if path in ("/markets", "/historical/markets") and q.get("tickers"):
            want = set(q["tickers"].split(","))
            pool = self.live if path == "/markets" else self.hist
            return httpx.Response(
                200, json={"markets": [m for m in pool if m["ticker"] in want], "cursor": ""}
            )
        if path == "/markets":
            want = {"open": "active", "settled": "finalized"}.get(q.get("status"), q.get("status"))
            items = [m for m in self.live if not want or m["status"] == want]  # API: open -> active
            if q.get("min_close_ts"):
                items = [
                    m
                    for m in items
                    if m["close_time"] >= iso(datetime.fromtimestamp(int(q["min_close_ts"]), UTC))
                ]
            if q.get("max_close_ts"):
                items = [
                    m
                    for m in items
                    if m["close_time"] <= iso(datetime.fromtimestamp(int(q["max_close_ts"]), UTC))
                ]
            return self.paginate(
                sorted(items, key=lambda m: m["close_time"], reverse=True), request, "markets"
            )
        if path == "/historical/markets":  # ignores close filters, like the real endpoint
            return self.paginate(
                sorted(self.hist, key=lambda m: m["created_time"], reverse=True), request, "markets"
            )
        if path in ("/markets/trades", "/historical/trades"):
            t = q.get("ticker")
            if t in self.fail_tickers:
                return httpx.Response(500, json={"error": "boom"})
            hist = path.startswith("/historical")
            rows = [x for x in self.trades.get(t, []) if (x["created_time"] < iso(CUTOFF)) == hist]
            if q.get("min_ts"):
                floor = iso(datetime.fromtimestamp(int(q["min_ts"]), UTC))
                rows = [x for x in rows if x["created_time"] >= floor]
            rows = sorted(rows, key=lambda x: x["created_time"], reverse=True)
            rows = [
                dict(x, yes_price_dollars="0.40009") if x["trade_id"] in self.bad_trade_ids else x
                for x in rows
            ]
            return self.paginate(rows, request, "trades")
        if path.startswith("/events/"):
            t = path.rsplit("/", 1)[1]
            if t not in self.events:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json={"event": self.events[t]})
        return httpx.Response(404, json={"error": f"unhandled {path}"})

    def rest(self, **kw) -> KalshiRestClient:
        async def nosleep(_):
            return None

        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return KalshiRestClient(
            KalshiConfig(rest_url=BASE),
            http=http,
            sleep=nosleep,
            requests_per_second=1e6,
            max_retries=1,
            **kw,
        )

    def count(self, prefix: str) -> int:
        return sum(
            1 for r in self.requests if r.url.path.removeprefix("/trade-api/v2").startswith(prefix)
        )


def event_payload(fx, event_ticker, category="Sports", mutually_exclusive=False):
    e = dict(fx("event.json"))
    e.pop("markets", None)
    e.update(
        event_ticker=event_ticker,
        category=category,
        mutually_exclusive=mutually_exclusive,
        series_ticker=event_ticker.split("-")[0],
    )
    return e
