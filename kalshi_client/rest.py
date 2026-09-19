"""Async, read-only Kalshi REST client.

Safety: this client **cannot place orders**.  The only HTTP verb implemented anywhere in
this class is ``GET`` (``_get``), and there are no order/portfolio endpoints.  A test
asserts that no other verb is ever issued.

Reliability
  * client-side token-bucket rate limiting (Kalshi's limit is token-based; 429 carries no
    ``Retry-After``, so we pace ourselves and also back off on 429),
  * bounded retries with exponential backoff + jitter on 429 / 5xx / timeouts / resets,
  * strict response validation - malformed items raise ``MessageValidationError`` unless the
    caller supplies an ``on_invalid`` quarantine callback,
  * cursor-pagination with a loop guard.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from kalshi_client.config import KalshiConfig
from kalshi_client.exceptions import (
    APIError,
    AuthenticationError,
    MessageValidationError,
    NotFoundError,
    RateLimitError,
    ServerError,
    TransportError,
)
from kalshi_client.models import (
    ApiEvent,
    ApiMarket,
    ApiOrderbook,
    ApiSeries,
    ApiTrade,
    validate_rest,
)

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
OnInvalid = Callable[[Any, MessageValidationError], None]

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class RateLimiter:
    """Async token bucket: ``rate`` tokens/second, holding at most ``burst`` tokens."""

    def __init__(
        self,
        rate: float,
        burst: float | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.burst = burst if burst is not None else max(1.0, rate)
        self._clock, self._sleep = clock, sleep
        self._tokens = self.burst
        self._stamp = clock()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: float = 1.0) -> None:
        if cost > self.burst:
            raise ValueError("cost exceeds bucket capacity")
        async with self._lock:
            while True:
                now = self._clock()
                self._tokens = min(self.burst, self._tokens + (now - self._stamp) * self.rate)
                self._stamp = now
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await self._sleep((cost - self._tokens) / self.rate)


class KalshiRestClient:
    def __init__(
        self,
        config: KalshiConfig | None = None,
        *,
        http: httpx.AsyncClient | None = None,
        requests_per_second: float = 10.0,
        max_retries: int = 5,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        timeout: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self.config = config or KalshiConfig.from_env()
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._limiter = RateLimiter(requests_per_second, sleep=sleep)
        self._max_retries = max_retries
        self._backoff_base, self._backoff_max = backoff_base, backoff_max
        self._sleep, self._rng = sleep, rng
        self.stats = {"requests": 0, "retries": 0}

    async def __aenter__(self) -> KalshiRestClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ------------------------------------------------------------------ transport
    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self._backoff_max)
        ceiling = min(self._backoff_max, self._backoff_base * 2**attempt)
        return ceiling * (0.5 + 0.5 * self._rng())  # "equal jitter"

    async def _get(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Any:
        """The single HTTP entry point.  GET only - see module docstring."""
        url = f"{self.config.rest_url}{endpoint}"
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        headers: dict[str, str] = {"Accept": "application/json"}
        last_error: Exception | None = None

        for attempt in range(self._max_retries + 1):
            await self._limiter.acquire()
            if self.config.auth is not None:  # fresh signature per attempt (timestamped)
                headers.update(
                    self.config.auth.headers("GET", f"{self.config.rest_path_prefix}{endpoint}")
                )
            self.stats["requests"] += 1
            retry_after: float | None = None
            try:
                resp = await self._http.get(url, params=clean, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                log.warning("GET %s attempt %d failed: %r", endpoint, attempt + 1, exc)
            else:
                status = resp.status_code
                if status == 200:
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise MessageValidationError(
                            f"{endpoint}: response is not JSON", raw=resp.text[:500]
                        ) from exc
                body = _safe_body(resp)
                if status in (401, 403):
                    raise AuthenticationError(f"HTTP {status} for {endpoint}: {body}")
                if status == 404:
                    raise NotFoundError(status, "not found", url=url, body=body)
                if status not in RETRYABLE_STATUS:
                    raise APIError(status, str(body), url=url, body=body)
                cls = RateLimitError if status == 429 else ServerError
                last_error = cls(status, str(body), url=url, body=body)
                retry_after = _retry_after(resp)
                log.warning("GET %s attempt %d -> HTTP %d", endpoint, attempt + 1, status)

            if attempt == self._max_retries:
                break
            self.stats["retries"] += 1
            await self._sleep(self._backoff(attempt, retry_after))

        if isinstance(last_error, APIError):
            raise last_error
        raise TransportError(f"GET {endpoint} failed after {self._max_retries + 1} attempts") from (
            last_error
        )

    async def _paginate(
        self,
        endpoint: str,
        items_key: str,
        model: type[M],
        params: Mapping[str, Any],
        *,
        limit: int | None,
        on_invalid: OnInvalid | None,
        start_cursor: str | None = None,
        on_page: Callable[[str | None], None] | None = None,
    ) -> AsyncIterator[M]:
        """Cursor-paginate.  ``on_page(next_cursor)`` fires once every item of a page has been
        consumed (``None`` after the last page) - the safe moment to checkpoint, because the
        consumer's state then reflects exactly the pages before ``next_cursor``.

        NOTE (verified live): the exchange silently treats an *invalid* cursor as "start from
        page 1" instead of erroring, so a caller resuming from a saved cursor must verify it."""
        cursor: str | None = start_cursor
        seen_cursors: set[str] = set()
        yielded = 0
        while True:
            data = await self._get(endpoint, {**params, "cursor": cursor})
            items = data.get(items_key)
            if not isinstance(items, list):
                raise MessageValidationError(f"{endpoint}: missing list {items_key!r}", raw=data)
            for item in items:
                try:
                    obj = validate_rest(model, item, what=f"{endpoint} item")
                except MessageValidationError as exc:
                    if on_invalid is None:
                        raise
                    on_invalid(item, exc)
                    continue
                yield obj
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            cursor = data.get("cursor") or None
            if on_page is not None:
                on_page(cursor)
            if not cursor:
                return
            if cursor in seen_cursors:  # defensive: a stuck cursor would loop forever
                raise MessageValidationError(f"{endpoint}: pagination cursor repeated", raw=cursor)
            seen_cursors.add(cursor)

    # ------------------------------------------------------------------ exchange
    async def get_exchange_status(self) -> dict[str, Any]:
        return await self._get("/exchange/status")

    async def get_historical_cutoff(self) -> dict[str, Any]:
        """Timestamps splitting 'live' from 'historical' partitions of markets/trades."""
        return await self._get("/historical/cutoff")

    # ------------------------------------------------------------------ markets
    async def get_market(self, ticker: str) -> ApiMarket:
        data = await self._get(f"/markets/{ticker}")
        return validate_rest(ApiMarket, data.get("market"), what=f"market {ticker}")

    def iter_markets(
        self,
        *,
        status: str | None = None,
        event_ticker: str | None = None,
        series_ticker: str | None = None,
        tickers: Iterable[str] | None = None,
        min_close_ts: int | None = None,
        max_close_ts: int | None = None,
        include_multivariate: bool = False,
        page_size: int = 1000,
        limit: int | None = None,
        historical: bool = False,
        on_invalid: OnInvalid | None = None,
        start_cursor: str | None = None,
        on_page: Callable[[str | None], None] | None = None,
    ) -> AsyncIterator[ApiMarket]:
        """Iterate markets.  Multivariate ("combo") markets are excluded by default because
        they are path-dependent parlays, not the plain binary claims this project studies."""
        params = {
            "status": status,
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "tickers": ",".join(tickers) if tickers is not None else None,
            "min_close_ts": min_close_ts,
            "max_close_ts": max_close_ts,
            "mve_filter": None if include_multivariate else "exclude",
            "limit": page_size,
        }
        endpoint = "/historical/markets" if historical else "/markets"
        return self._paginate(
            endpoint,
            "markets",
            ApiMarket,
            params,
            limit=limit,
            on_invalid=on_invalid,
            start_cursor=start_cursor,
            on_page=on_page,
        )

    async def get_series(self, series_ticker: str) -> ApiSeries:
        data = await self._get(f"/series/{series_ticker}")
        return validate_rest(ApiSeries, data.get("series", data), what=f"series {series_ticker}")

    # ------------------------------------------------------------------ events
    async def get_event(self, event_ticker: str, *, with_nested_markets: bool = True) -> ApiEvent:
        data = await self._get(
            f"/events/{event_ticker}", {"with_nested_markets": str(with_nested_markets).lower()}
        )
        event = dict(data.get("event") or {})
        if with_nested_markets and "markets" not in event and "markets" in data:
            event["markets"] = data["markets"]  # some responses hoist markets beside the event
        return validate_rest(ApiEvent, event, what=f"event {event_ticker}")

    def iter_events(
        self,
        *,
        status: str | None = None,
        series_ticker: str | None = None,
        with_nested_markets: bool = False,
        page_size: int = 200,
        limit: int | None = None,
        on_invalid: OnInvalid | None = None,
    ) -> AsyncIterator[ApiEvent]:
        params = {
            "status": status,
            "series_ticker": series_ticker,
            "with_nested_markets": "true" if with_nested_markets else None,
            "limit": page_size,
        }
        return self._paginate(
            "/events", "events", ApiEvent, params, limit=limit, on_invalid=on_invalid
        )

    # ------------------------------------------------------------------ order books
    async def get_orderbook(self, ticker: str, *, depth: int | None = None) -> ApiOrderbook:
        data = await self._get(f"/markets/{ticker}/orderbook", {"depth": depth})
        return validate_rest(ApiOrderbook, data, what=f"orderbook {ticker}")

    async def get_orderbooks(
        self, tickers: list[str], *, batch_size: int = 50
    ) -> dict[str, ApiOrderbook]:
        """Batch order-book fetch.  Returns ``{ticker: book}``; chunked to bound URL length.

        API QUIRK (verified live): this endpoint wants *repeated* ``tickers=A&tickers=B``
        parameters.  A comma-joined value is silently treated as ONE ticker and answered with
        a 200 and an empty book named "A,B" - so the response is checked against the request.
        (``GET /markets`` by contrast takes a comma-separated ``tickers`` value.)
        """
        out: dict[str, ApiOrderbook] = {}
        for i in range(0, len(tickers), batch_size):
            chunk = tickers[i : i + batch_size]
            data = await self._get("/markets/orderbooks", {"tickers": chunk})
            rows = data.get("orderbooks")
            if not isinstance(rows, list):
                raise MessageValidationError("orderbooks: missing list", raw=data)
            for row in rows:
                book = validate_rest(ApiOrderbook, row, what="orderbook row")
                if book.ticker not in chunk:
                    raise MessageValidationError(
                        f"orderbooks: unexpected ticker {book.ticker!r} (not in request)", raw=row
                    )
                out[book.ticker] = book
        return out

    # ------------------------------------------------------------------ trades
    def iter_trades(
        self,
        *,
        ticker: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
        page_size: int = 1000,
        limit: int | None = None,
        historical: bool = False,
        on_invalid: OnInvalid | None = None,
    ) -> AsyncIterator[ApiTrade]:
        """Iterate public trades (newest first, as served).  ``min_ts``/``max_ts`` are epoch
        seconds.  Trades are keyed by ``trade_id``; callers must dedupe on it."""
        params = {"ticker": ticker, "min_ts": min_ts, "max_ts": max_ts, "limit": page_size}
        endpoint = "/historical/trades" if historical else "/markets/trades"
        return self._paginate(
            endpoint, "trades", ApiTrade, params, limit=limit, on_invalid=on_invalid
        )


def _safe_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return resp.text[:500]


def _retry_after(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    try:
        return float(raw) if raw is not None else None
    except ValueError:
        return None
