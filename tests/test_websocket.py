"""WebSocket client against an in-process mock exchange (no network, no credentials)."""

import asyncio
import json
from http import HTTPStatus

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.asyncio.server import serve

from kalshi_client.auth import KalshiAuth
from kalshi_client.config import KalshiConfig
from kalshi_client.exceptions import AuthenticationError, ConfigurationError
from kalshi_client.models import WsOrderbookDelta, WsOrderbookSnapshot, WsTicker
from kalshi_client.websocket import Connected, Disconnected, KalshiWebSocket, Malformed


def frame(obj):
    return json.dumps(obj)


SNAP = frame(
    {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": 1,
        "msg": {"market_ticker": "T", "yes_dollars_fp": [["0.4000", "1.00"]], "no_dollars_fp": []},
    }
)
DELTA = frame(
    {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": 2,
        "msg": {"market_ticker": "T", "price_dollars": "0.4000", "delta_fp": "1.00", "side": "yes"},
    }
)
TICK = frame(
    {"type": "ticker", "sid": 2, "msg": {"market_ticker": "T", "yes_bid_dollars": "0.4000"}}
)


class MockExchange:
    """Each connection consumes one script: a list of frames to send, then close (or hold open)."""

    def __init__(self, scripts, *, reject_status=None, hold_open=False):
        self.scripts = list(scripts)
        self.reject_status = reject_status
        self.hold_open = hold_open
        self.handshakes: list[dict] = []
        self.commands: list[dict] = []
        self.connections = 0

    async def _process_request(self, connection, request):
        self.handshakes.append(dict(request.headers))
        if self.reject_status:
            return connection.respond(self.reject_status, "denied\n")

    async def _handler(self, ws):
        self.connections += 1
        script = self.scripts.pop(0) if self.scripts else []
        self.commands.append(json.loads(await ws.recv()))
        for item in script:
            await ws.send(item)
        if self.hold_open:
            await ws.wait_closed()
        await ws.close()

    def serve(self):
        return serve(self._handler, "127.0.0.1", 0, process_request=self._process_request)


async def no_sleep(_):
    return None


def make_client(server, **kw):
    port = next(iter(server.sockets)).getsockname()[1]
    cfg = KalshiConfig(
        rest_url="http://x",
        ws_url=f"ws://127.0.0.1:{port}/trade-api/ws/v2",
        auth=kw.pop("auth", None),
    )
    return KalshiWebSocket(cfg, sleep=no_sleep, require_auth=cfg.auth is not None, **kw)


async def collect(ws, n=None, timeout=10):
    out = []

    async def run():
        async for ev in ws.stream():
            out.append(ev)
            if n is not None and len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_streams_typed_messages_and_subscribes():
    ex = MockExchange([[SNAP, DELTA, TICK]])
    async with ex.serve() as server:
        ws = make_client(
            server, channels=["orderbook_delta", "ticker"], market_tickers=["T"], max_reconnects=0
        )
        events = await collect(ws)
    kinds = [type(e) for e in events]
    assert kinds == [Connected, WsOrderbookSnapshot, WsOrderbookDelta, WsTicker, Disconnected]
    assert ex.commands[0]["cmd"] == "subscribe"
    assert ex.commands[0]["params"] == {
        "channels": ["orderbook_delta", "ticker"],
        "market_tickers": ["T"],
    }
    assert all(e.epoch == 1 for e in events[1:4]) and events[1].recv_ts_ns > 0


async def test_subscription_batching():
    ex = MockExchange([[]])
    ex.scripts = [[]]
    async with ex.serve() as server:
        ws = make_client(
            server,
            channels=["ticker"],
            market_tickers=[f"M{i}" for i in range(5)],
            subscribe_batch=2,
            max_reconnects=0,
        )
        # the mock reads only the first command; batching is asserted on the wire below
        sent = []

        class Spy:
            async def send(self, s):
                sent.append(json.loads(s))

        await ws._subscribe(Spy())
    assert [c["params"]["market_tickers"] for c in sent] == [["M0", "M1"], ["M2", "M3"], ["M4"]]
    assert [c["id"] for c in sent] == [1, 2, 3]


async def test_reconnects_resubscribes_and_bumps_epoch():
    ex = MockExchange([[SNAP], [SNAP, DELTA]])
    async with ex.serve() as server:
        ws = make_client(
            server, channels=["orderbook_delta"], market_tickers=["T"], max_reconnects=1
        )
        events = await collect(ws)
    assert ex.connections == 2 and len(ex.commands) == 2  # subscribed again after reconnect
    conn = [e for e in events if isinstance(e, Connected)]
    assert [c.epoch for c in conn] == [1, 2]
    assert [e.epoch for e in events if isinstance(e, WsOrderbookSnapshot)] == [1, 2]
    assert sum(isinstance(e, Disconnected) for e in events) == 2


async def test_malformed_frames_are_surfaced_not_dropped_and_do_not_kill_stream():
    bad_json, bad_units = (
        "{{{",
        frame(
            {
                "type": "orderbook_delta",
                "sid": 1,
                "seq": 2,
                "msg": {
                    "market_ticker": "T",
                    "price_dollars": "0.123456",
                    "delta_fp": "1.00",
                    "side": "yes",
                },
            }
        ),
    )
    ex = MockExchange([[SNAP, bad_json, bad_units, DELTA]])
    async with ex.serve() as server:
        ws = make_client(server, channels=["orderbook_delta"], max_reconnects=0)
        events = await collect(ws)
    malformed = [e for e in events if isinstance(e, Malformed)]
    assert [m.raw for m in malformed] == [bad_json, bad_units]
    assert any(isinstance(e, WsOrderbookDelta) for e in events)  # good frame after the bad ones
    assert ws.stats["malformed"] == 2


async def test_request_resync_forces_reconnect():
    ex = MockExchange([[SNAP, DELTA], [SNAP]], hold_open=True)
    async with ex.serve() as server:
        ws = make_client(server, channels=["orderbook_delta"], max_reconnects=1)
        seen = []
        async for ev in ws.stream():
            seen.append(ev)
            if isinstance(ev, WsOrderbookDelta):
                ws.request_resync("test gap")
            if isinstance(ev, WsOrderbookSnapshot) and ev.epoch == 2:
                await ws.close()
    assert ex.connections == 2
    disc = [e for e in seen if isinstance(e, Disconnected)]
    assert "resync: test gap" in disc[0].reason


async def test_auth_headers_sent_on_handshake():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    ex = MockExchange([[]])
    async with ex.serve() as server:
        ws = make_client(
            server, auth=KalshiAuth.from_pem("kid", pem), channels=["ticker"], max_reconnects=0
        )
        await collect(ws)
    h = {k.lower(): v for k, v in ex.handshakes[0].items()}
    assert h["kalshi-access-key"] == "kid"
    assert h["kalshi-access-signature"] and h["kalshi-access-timestamp"].isdigit()


async def test_handshake_401_is_fatal_not_retried():
    ex = MockExchange([], reject_status=HTTPStatus.UNAUTHORIZED)
    async with ex.serve() as server:
        ws = make_client(server, channels=["ticker"], max_reconnects=5)
        with pytest.raises(AuthenticationError):
            await collect(ws)
    assert len(ex.handshakes) == 1


async def test_connect_failure_backs_off_and_retries():
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    cfg = KalshiConfig(rest_url="http://x", ws_url="ws://127.0.0.1:1/trade-api/ws/v2")
    ws = KalshiWebSocket(
        cfg,
        channels=["ticker"],
        require_auth=False,
        sleep=sleep,
        rng=lambda: 1.0,
        backoff_base=1.0,
        max_reconnects=3,
        open_timeout=1,
    )
    assert await collect(ws) == []  # never connects; bounded by max_reconnects
    assert sleeps == [1.0, 2.0, 4.0]


async def test_missing_credentials_fail_before_any_network_io():
    ws = KalshiWebSocket(KalshiConfig(auth=None), channels=["orderbook_delta"])
    with pytest.raises(AuthenticationError, match="credentials"):
        await collect(ws)


def test_order_entry_channels_are_not_subscribable():
    for ch in ("fill", "market_positions", "orders", "communications"):
        with pytest.raises(ConfigurationError):
            KalshiWebSocket(KalshiConfig(), channels=[ch])
