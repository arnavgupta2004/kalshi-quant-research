import base64

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_client.auth import KalshiAuth
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
from kalshi_client.rest import KalshiRestClient, RateLimiter

CFG = KalshiConfig(rest_url="https://example.test/trade-api/v2")


class Harness:
    """Scripted httpx transport that records every request and every backoff sleep."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []
        self.sleeps: list[float] = []

    def handler(self, request):
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r

    async def sleep(self, s):
        self.sleeps.append(s)

    def client(self, cfg=CFG, **kw):
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return KalshiRestClient(
            cfg, http=http, sleep=self.sleep, rng=lambda: 1.0, requests_per_second=1e6, **kw
        )


def js(data, status=200):
    return httpx.Response(status, json=data)


async def test_get_success_drops_none_params():
    h = Harness([js({"exchange_active": True})])
    async with h.client() as c:
        assert (await c.get_exchange_status())["exchange_active"] is True
    assert h.requests[0].url.path == "/trade-api/v2/exchange/status"


async def test_retries_429_then_succeeds_with_exponential_backoff():
    h = Harness([js({"error": "too many requests"}, 429)] * 3 + [js({"ok": 1})])
    async with h.client(backoff_base=0.5) as c:
        assert await c._get("/x") == {"ok": 1}
        assert c.stats["retries"] == 3
    assert h.sleeps == [0.5, 1.0, 2.0]  # rng=1.0 -> full ceiling: base * 2**attempt


async def test_gives_up_after_max_retries_with_typed_error():
    h = Harness([js({"e": 1}, 503)])
    async with h.client(max_retries=2) as c:
        with pytest.raises(ServerError):
            await c._get("/x")
    assert len(h.requests) == 3
    h = Harness([js({"e": 1}, 429)])
    async with h.client(max_retries=1) as c:
        with pytest.raises(RateLimitError):
            await c._get("/x")


async def test_transport_errors_retried_then_wrapped():
    h = Harness([httpx.ConnectError("boom"), js({"ok": 1})])
    async with h.client() as c:
        assert await c._get("/x") == {"ok": 1}
    h = Harness([httpx.ReadTimeout("slow")])
    async with h.client(max_retries=2) as c:
        with pytest.raises(TransportError):
            await c._get("/x")
    assert len(h.requests) == 3


async def test_non_retryable_statuses_fail_fast():
    for status, exc in [(404, NotFoundError), (400, APIError), (401, AuthenticationError)]:
        h = Harness([js({"error": "nope"}, status)])
        async with h.client() as c:
            with pytest.raises(exc):
                await c._get("/x")
        assert len(h.requests) == 1 and h.sleeps == []


async def test_retry_after_header_honoured_when_present():
    h = Harness([httpx.Response(429, headers={"Retry-After": "7"}, json={}), js({"ok": 1})])
    async with h.client() as c:
        await c._get("/x")
    assert h.sleeps == [7.0]


async def test_non_json_200_is_a_validation_error_not_a_retry():
    h = Harness([httpx.Response(200, text="<html>maintenance</html>")])
    async with h.client() as c:
        with pytest.raises(MessageValidationError):
            await c._get("/x")
    assert len(h.requests) == 1


async def test_read_only_only_get_verbs_are_ever_issued(fx):
    h = Harness(
        [
            js({"exchange_active": True}),
            js({"markets": [fx("market_active.json")], "cursor": ""}),
            js(fx("orderbook.json")),
        ]
    )
    async with h.client() as c:
        await c.get_exchange_status()
        [m async for m in c.iter_markets(limit=1)]
        await c.get_orderbook("T")
    assert {r.method for r in h.requests} == {"GET"}
    for name in ("post", "put", "delete", "patch", "create_order", "place_order", "cancel_order"):
        assert not hasattr(c, name)


async def test_pagination_follows_cursor_and_respects_limit(fx):
    m = fx("market_active.json")
    pages = [
        js({"markets": [dict(m, ticker=f"A{i}") for i in range(3)], "cursor": "c1"}),
        js({"markets": [dict(m, ticker=f"B{i}") for i in range(3)], "cursor": "c2"}),
        js({"markets": [dict(m, ticker="C0")], "cursor": ""}),
    ]
    h = Harness(pages)
    async with h.client() as c:
        got = [x.ticker async for x in c.iter_markets()]
    assert got == ["A0", "A1", "A2", "B0", "B1", "B2", "C0"]
    assert [r.url.params.get("cursor") for r in h.requests] == [None, "c1", "c2"]

    h = Harness([js({"markets": [dict(m, ticker=f"A{i}") for i in range(3)], "cursor": "c1"})])
    async with h.client() as c:
        assert len([x async for x in c.iter_markets(limit=2)]) == 2
    assert len(h.requests) == 1  # stopped without fetching the next page


async def test_repeated_cursor_is_detected():
    h = Harness([js({"trades": [], "cursor": "same"})])
    async with h.client() as c:
        with pytest.raises(MessageValidationError, match="cursor repeated"):
            [t async for t in c.iter_trades()]


async def test_invalid_item_raises_unless_quarantine_callback_given(fx):
    good, bad = fx("market_active.json"), dict(fx("market_active.json"), yes_bid_dollars="0.123456")
    h = Harness([js({"markets": [good, bad, good], "cursor": ""})])
    async with h.client() as c:
        with pytest.raises(MessageValidationError):
            [m async for m in c.iter_markets()]
    h = Harness([js({"markets": [good, bad, good], "cursor": ""})])
    quarantined = []
    async with h.client() as c:
        got = [m async for m in c.iter_markets(on_invalid=lambda item, e: quarantined.append(item))]
    assert len(got) == 2 and quarantined == [bad]


async def test_multivariate_excluded_by_default(fx):
    h = Harness([js({"markets": [], "cursor": ""})])
    async with h.client() as c:
        [m async for m in c.iter_markets(status="open")]
        [m async for m in c.iter_markets(include_multivariate=True)]
    assert h.requests[0].url.params["mve_filter"] == "exclude"
    assert h.requests[0].url.params["status"] == "open"
    assert "mve_filter" not in h.requests[1].url.params


async def test_historical_flag_switches_endpoint():
    h = Harness([js({"trades": [], "cursor": ""})])
    async with h.client() as c:
        [t async for t in c.iter_trades(historical=True, ticker="T", min_ts=5)]
    r = h.requests[0]
    assert r.url.path.endswith("/historical/trades") and r.url.params["ticker"] == "T"
    assert r.url.params["min_ts"] == "5"


async def test_batch_orderbooks_chunks_and_keys_by_ticker(fx):
    ob = fx("orderbook.json")
    h = Harness(
        [
            js({"orderbooks": [dict(ob, ticker="A"), dict(ob, ticker="B")]}),
            js({"orderbooks": [dict(ob, ticker="C")]}),
        ]
    )
    async with h.client() as c:
        books = await c.get_orderbooks(["A", "B", "C"], batch_size=2)
    assert sorted(books) == ["A", "B", "C"]
    # repeated params, NOT comma-joined (a comma-joined value is silently read as one ticker)
    assert h.requests[0].url.params.get_list("tickers") == ["A", "B"]
    assert h.requests[1].url.params.get_list("tickers") == ["C"]


async def test_batch_orderbooks_rejects_unexpected_ticker(fx):
    """Regression: the exchange answers a comma-joined ticker list with 200 + one empty book
    named 'A,B'.  That must be an error, never a silently-empty book."""
    bogus = {
        "orderbooks": [{"ticker": "A,B", "orderbook_fp": {"yes_dollars": [], "no_dollars": []}}]
    }
    h = Harness([js(bogus)])
    async with h.client() as c:
        with pytest.raises(MessageValidationError, match="unexpected ticker"):
            await c.get_orderbooks(["A", "B"])


async def test_markets_by_ticker_uses_comma_form():
    h = Harness([js({"markets": [], "cursor": ""})])
    async with h.client() as c:
        [m async for m in c.iter_markets(tickers=["A", "B"])]
    assert h.requests[0].url.params["tickers"] == "A,B"


async def test_event_nested_markets_hoisted(fx):
    ev = fx("event.json")
    markets = ev.pop("markets")
    h = Harness([js({"event": ev, "markets": markets})])
    async with h.client() as c:
        e = await c.get_event(ev["event_ticker"])
    assert e.markets and e.markets[0].ticker == markets[0]["ticker"]


async def test_signed_requests_verify_and_sign_path_without_query():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    cfg = KalshiConfig(rest_url=CFG.rest_url, auth=KalshiAuth.from_pem("kid", pem))
    h = Harness([js({"markets": [], "cursor": ""})])
    async with h.client(cfg) as c:
        [m async for m in c.iter_markets(status="open")]
    r = h.requests[0]
    ts = r.headers["KALSHI-ACCESS-TIMESTAMP"]
    key.public_key().verify(
        base64.b64decode(r.headers["KALSHI-ACCESS-SIGNATURE"]),
        f"{ts}GET/trade-api/v2/markets".encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    assert r.headers["KALSHI-ACCESS-KEY"] == "kid"


async def test_public_requests_carry_no_auth_headers():
    h = Harness([js({})])
    async with h.client() as c:
        await c._get("/x")
    assert "KALSHI-ACCESS-KEY" not in h.requests[0].headers


async def test_rate_limiter_paces_after_burst():
    now = [0.0]
    slept = []

    async def sleep(s):
        slept.append(s)
        now[0] += s

    rl = RateLimiter(rate=10, burst=2, clock=lambda: now[0], sleep=sleep)
    for _ in range(2):
        await rl.acquire()  # burst is free
    assert slept == []
    await rl.acquire()
    await rl.acquire()
    assert slept == pytest.approx([0.1, 0.1])  # then one token per 1/rate seconds
    with pytest.raises(ValueError):
        await rl.acquire(cost=3)
