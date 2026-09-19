"""Resilient, read-only Kalshi WebSocket stream.

``KalshiWebSocket.stream()`` is an async iterator of ``StreamEvent`` values:

  * validated message models (``WsOrderbookDelta``, ``WsTicker``, ...),
  * ``Connected`` / ``Disconnected`` markers,
  * ``Malformed`` for frames that failed validation (raw text preserved for quarantine).

Guarantees
  * **Auto-reconnect** with exponential backoff + jitter; every connection generation gets a
    new ``epoch``.  Subscription ids and sequence numbers are only meaningful *within* an
    epoch, so consumers key their sequence state on ``(epoch, sid)``.
  * **Re-subscription** on every (re)connect - a fresh ``orderbook_snapshot`` follows, which
    is what makes resync-by-reconnect correct.
  * **Heartbeats** via WebSocket protocol ping/pong (the ``websockets`` library answers the
    exchange's pings automatically and closes the socket if our pings go unanswered).  An
    optional ``stale_timeout`` additionally reconnects if *no* frame arrives for that long.
  * Malformed frames never raise and never vanish: they surface as ``Malformed`` events.
  * Auth failure (HTTP 401/403 on the handshake) is fatal, not retried.

The client only ever *receives* market data: the sole frames it sends are ``subscribe``
commands, and there is no order-entry code anywhere in the package.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus

from kalshi_client.config import KalshiConfig
from kalshi_client.exceptions import AuthenticationError, ConfigurationError, MessageValidationError
from kalshi_client.models import WsMessage, parse_ws_message
from market.timeutil import now_ns

log = logging.getLogger(__name__)

# Channels that Kalshi serves.  (Order-entry/fill/position channels are deliberately absent.)
PUBLIC_CHANNELS = frozenset({"ticker", "trade", "market_lifecycle_v2", "orderbook_delta"})


@dataclass(frozen=True, slots=True)
class Connected:
    epoch: int
    url: str
    recv_ts_ns: int


@dataclass(frozen=True, slots=True)
class Disconnected:
    epoch: int
    reason: str
    recv_ts_ns: int


@dataclass(frozen=True, slots=True)
class Malformed:
    epoch: int
    raw: str
    error: str
    recv_ts_ns: int


StreamEvent = WsMessage | Connected | Disconnected | Malformed


class KalshiWebSocket:
    def __init__(
        self,
        config: KalshiConfig | None = None,
        *,
        channels: Sequence[str],
        market_tickers: Sequence[str] | None = None,
        require_auth: bool = True,
        ping_interval: float = 10.0,
        ping_timeout: float = 10.0,
        open_timeout: float = 15.0,
        stale_timeout: float | None = None,
        backoff_base: float = 1.0,
        backoff_max: float = 60.0,
        max_reconnects: int | None = None,
        subscribe_batch: int = 100,
        connect: Callable[..., Awaitable] = ws_connect,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
    ) -> None:
        unknown = set(channels) - PUBLIC_CHANNELS
        if unknown:
            raise ConfigurationError(f"unsupported/non-public channels: {sorted(unknown)}")
        self.config = config or KalshiConfig.from_env()
        self.channels = list(channels)
        self.market_tickers = list(market_tickers) if market_tickers is not None else None
        self._require_auth = require_auth
        self._ping_interval, self._ping_timeout = ping_interval, ping_timeout
        self._open_timeout, self._stale_timeout = open_timeout, stale_timeout
        self._backoff_base, self._backoff_max = backoff_base, backoff_max
        self._max_reconnects = max_reconnects
        self._batch = subscribe_batch
        self._connect, self._sleep, self._rng = connect, sleep, rng

        self._epoch = 0
        self._cmd_id = 0
        self._ws = None
        self._closed = False
        self._resync_reason: str | None = None
        self.stats = {"connects": 0, "frames": 0, "malformed": 0, "resyncs": 0}

    @property
    def epoch(self) -> int:
        return self._epoch

    # ------------------------------------------------------------------ control
    def request_resync(self, reason: str = "consumer requested resync") -> None:
        """Drop the current connection and reconnect, which yields fresh order-book snapshots.

        Call this when the consumer detects a sequence gap or an inconsistent book."""
        self._resync_reason = reason
        self.stats["resyncs"] += 1

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ------------------------------------------------------------------ internals
    def _headers(self) -> dict[str, str]:
        auth = self.config.auth
        if auth is None:
            if self._require_auth:
                raise AuthenticationError(
                    "the Kalshi WebSocket requires API credentials "
                    "(set KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH)"
                )
            return {}
        return auth.headers("GET", self.config.ws_path)

    def _backoff(self, attempt: int) -> float:
        ceiling = min(self._backoff_max, self._backoff_base * 2**attempt)
        return ceiling * (0.5 + 0.5 * self._rng())

    async def _subscribe(self, ws) -> None:
        batches: list[list[str] | None]
        if self.market_tickers is None:
            batches = [None]
        else:
            batches = [
                self.market_tickers[i : i + self._batch]
                for i in range(0, len(self.market_tickers), self._batch)
            ]
        for batch in batches:
            self._cmd_id += 1
            params: dict = {"channels": self.channels}
            if batch is not None:
                params["market_tickers"] = batch
            await ws.send(json.dumps({"id": self._cmd_id, "cmd": "subscribe", "params": params}))

    # ------------------------------------------------------------------ main loop
    async def stream(self) -> AsyncIterator[StreamEvent]:
        attempt = 0
        reconnects = 0
        while not self._closed:
            headers = self._headers()  # fresh signature each attempt
            try:
                ws = await self._connect(
                    self.config.ws_url,
                    additional_headers=headers,
                    ping_interval=self._ping_interval,
                    ping_timeout=self._ping_timeout,
                    open_timeout=self._open_timeout,
                    max_size=2**24,
                )
            except InvalidStatus as exc:
                status = exc.response.status_code
                if status in (401, 403):
                    raise AuthenticationError(
                        f"WebSocket handshake rejected: HTTP {status}"
                    ) from exc
                log.warning("WS handshake failed: HTTP %s", status)
                reason = f"handshake HTTP {status}"
            except (OSError, TimeoutError, InvalidHandshake) as exc:
                log.warning("WS connect failed: %r", exc)
                reason = f"connect failed: {exc!r}"
            else:
                self._epoch += 1
                self._ws = ws
                self.stats["connects"] += 1
                epoch = self._epoch
                self._resync_reason = None
                got_frame = False
                yield Connected(epoch, self.config.ws_url, now_ns())
                reason = "closed"
                try:
                    await self._subscribe(ws)
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), self._stale_timeout)
                        except TimeoutError:
                            reason = f"stale: no frame for {self._stale_timeout}s"
                            break
                        recv_ns = now_ns()
                        self.stats["frames"] += 1
                        got_frame = True
                        try:
                            msg = parse_ws_message(raw, recv_ts_ns=recv_ns, epoch=epoch)
                        except MessageValidationError as exc:
                            self.stats["malformed"] += 1
                            log.error("malformed frame quarantined: %s", exc)
                            yield Malformed(
                                epoch, raw if isinstance(raw, str) else repr(raw), str(exc), recv_ns
                            )
                        else:
                            yield msg
                        if self._closed:
                            reason = "client closed"
                            break
                        if self._resync_reason is not None:
                            reason = f"resync: {self._resync_reason}"
                            break
                except ConnectionClosed as exc:
                    reason = f"closed: code={exc.rcvd.code if exc.rcvd else None}"
                finally:
                    self._ws = None
                    with contextlib.suppress(Exception):
                        await ws.close()
                yield Disconnected(epoch, reason, now_ns())
                if got_frame:
                    attempt = 0

            if self._closed:
                return
            reconnects += 1
            if self._max_reconnects is not None and reconnects > self._max_reconnects:
                return
            await self._sleep(self._backoff(attempt))
            attempt += 1
