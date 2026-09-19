import json
from datetime import UTC, datetime

import pytest

from data.collectors.books import (
    BookPoller,
    WsBookRecorder,
    book_hash,
    select_book_tickers,
)
from data.collectors.config import (
    BooksConfig,
    BookSelection,
    ConfigError,
    HistoryConfig,
    load_config,
)
from data.storage.duckdb_store import Store
from kalshi_client.exceptions import MessageValidationError
from kalshi_client.models import parse_ws_message
from kalshi_client.websocket import Connected, Disconnected, Malformed
from tests.fakes import FakeExchange, event_payload, make_market

D = lambda m, d, h=0: datetime(2026, m, d, h, tzinfo=UTC)  # noqa: E731


class BookExchange(FakeExchange):
    """FakeExchange + an order-book endpoint whose books tests can mutate between polls."""

    def __init__(self, fx, live):
        super().__init__(fx, live=live, events={"EVT-1": event_payload(fx, "EVT-1")})
        self.books = {
            m["ticker"]: {
                "yes": [["0.4000", "10.00"], ["0.3000", "5.00"]],
                "no": [["0.5000", "7.00"]],
            }
            for m in live
        }

    def handler(self, request):
        import httpx

        path = request.url.path.removeprefix("/trade-api/v2")
        if path == "/markets/orderbooks":
            self.requests.append(request)
            want = request.url.params.get_list("tickers")
            rows = [
                {
                    "ticker": t,
                    "orderbook_fp": {"yes_dollars": b["yes"][::-1], "no_dollars": b["no"]},
                }
                for t in want
                if (b := self.books.get(t))
            ]  # worst->best, like the real API
            return httpx.Response(200, json={"orderbooks": rows})
        if path == "/markets" and request.url.params.get("tickers"):
            self.requests.append(request)
            want = set(request.url.params["tickers"].split(","))
            return httpx.Response(
                200, json={"markets": [m for m in self.live if m["ticker"] in want], "cursor": ""}
            )
        return super().handler(request)


def poller(ex, store, tickers, **cfg):
    clock = {"ns": 1_000_000_000_000}
    rest = ex.rest()

    async def sleep(_):
        clock["ns"] += int(cfg.get("interval_s", 5) * 1e9)

    def now():
        clock["ns"] += 1_000_000  # 1 ms passes per call
        return clock["ns"]

    bc = BooksConfig(**{"heartbeat_s": 60, "duration_s": 12, **cfg})
    p = BookPoller(
        store, rest, bc, tickers, clock_ns=now, monotonic=lambda: clock["ns"] / 1e9, sleep=sleep
    )
    return p, rest


@pytest.fixture
def ex(fx):
    live = [
        make_market(fx, f"EVT-1-{s}", event="EVT-1", close=D(9, 30), status="active") for s in "AB"
    ]
    return BookExchange(fx, live)


async def test_poller_stores_on_change_and_heartbeat_only(ex):
    store = Store()
    p, _ = poller(ex, store, ["EVT-1-A", "EVT-1-B"])
    async with ex.rest() as rest:  # drive cycles manually for exact control
        p.rest = rest
        p.run_id = store.start_run("books", {})
        await p.poll_once()  # first observation: both stored
        assert store.query("SELECT count(*) FROM book_snapshots") == [(2,)]
        await p.poll_once()  # unchanged: nothing stored
        assert (
            store.query("SELECT count(*) FROM book_snapshots") == [(2,)]
            and p.summary.unchanged == 2
        )
        ex.books["EVT-1-A"]["yes"][0] = ["0.4100", "10.00"]  # only A changes
        await p.poll_once()
        assert store.query("SELECT ticker, count(*) FROM book_snapshots GROUP BY 1 ORDER BY 1") == [
            ("EVT-1-A", 2),
            ("EVT-1-B", 1),
        ]
        assert store.query("SELECT count(*) FROM poll_log") == [
            (3,)
        ]  # alive-proof for unchanged cycles
        p._last = {
            t: (h, 0) for t, (h, _) in p._last.items()
        }  # pretend the last store was long ago
        await p.poll_once()  # heartbeat: unchanged books re-stored
        assert store.query("SELECT count(*) FROM book_snapshots") == [(5,)]


async def test_book_rows_are_best_first_with_request_and_receive_times(ex):
    store = Store()
    p, _ = poller(ex, store, ["EVT-1-A"])
    async with ex.rest() as rest:
        p.rest, p.run_id = rest, store.start_run("books", {})
        await p.poll_once()
    yes_px, yes_qty, no_px, req, recv, src = store.query(
        "SELECT yes_px, yes_qty, no_px, req_ts_ns, recv_ts_ns, source FROM book_snapshots"
    )[0]
    assert (
        yes_px == [4000, 3000] and yes_qty == [1000, 500] and no_px == [5000]
    )  # sorted despite worst->best input
    assert src == "rest_poll" and req < recv


async def test_resume_does_not_restore_unchanged_books(ex):
    store = Store()
    p1, _ = poller(ex, store, ["EVT-1-A"])
    async with ex.rest() as rest:
        p1.rest, p1.run_id = rest, store.start_run("books", {})
        await p1.poll_once()
    p2, _ = poller(ex, store, ["EVT-1-A"])  # a new process, same db
    assert p2._last["EVT-1-A"][0] == store.latest_book_hashes()["EVT-1-A"][0]
    async with ex.rest() as rest:
        p2.rest, p2.run_id = rest, store.start_run("books", {})
        await p2.poll_once()
    assert store.query("SELECT count(*) FROM book_snapshots") == [(1,)]


async def test_missing_and_invalid_books_are_reported_not_stored(ex):
    store = Store()
    del ex.books["EVT-1-B"]
    ex.books["EVT-1-A"]["no"] = [["0.5000", "-7.00"]]  # impossible negative size
    p, _ = poller(ex, store, ["EVT-1-A", "EVT-1-B"])
    async with ex.rest() as rest:
        p.rest, p.run_id = rest, store.start_run("books", {})
        with pytest.raises(MessageValidationError):  # negative qty fails wire validation
            await p.poll_once()
    ex.books["EVT-1-A"]["no"] = [["0.5000", "7.00"]]
    async with ex.rest() as rest:
        p.rest = rest
        await p.poll_once()
    assert p.summary.missing == 1 and store.query("SELECT ticker FROM book_snapshots") == [
        ("EVT-1-A",)
    ]
    assert store.query("SELECT n_missing FROM poll_log") == [(1,)]


async def test_full_poller_run_records_provenance_and_metadata(ex):
    store = Store()
    p, rest = poller(ex, store, ["EVT-1-A", "EVT-1-B"], interval_s=5, duration_s=12)
    async with rest:
        p.rest = rest
        s = await p.run()
    assert s.cycles >= 3 and s.snapshots_stored == 2 and s.unchanged >= 4
    assert store.query("SELECT count(*) FROM markets") == [(2,)] and store.query(
        "SELECT count(*) FROM events"
    ) == [(1,)]
    assert store.query("SELECT status, job FROM collection_runs") == [("ok", "books")]


async def test_select_book_tickers_keeps_whole_events_and_respects_caps(fx):
    live = []
    for ev, n, vol in [("BIG", 4, 9), ("MID", 3, 5), ("SOLO", 1, 50), ("ZERO", 3, 0)]:
        for i in range(n):
            m = make_market(fx, f"{ev}-{i}", event=ev, close=D(9, 30), status="active")
            m["volume_24h_fp"] = f"{vol}.00"
            live.append(m)
    ex = FakeExchange(fx, live=live)
    async with ex.rest() as rest:
        got = await select_book_tickers(
            rest, BookSelection(top_events=5, max_tickers=100, min_event_markets=2)
        )
        assert got == [f"BIG-{i}" for i in range(4)] + [
            f"MID-{i}" for i in range(3)
        ]  # SOLO<2 mkts, ZERO no volume
        capped = await select_book_tickers(
            rest, BookSelection(top_events=5, max_tickers=5, min_event_markets=2)
        )
    assert capped == [
        f"BIG-{i}" for i in range(4)
    ]  # MID would overflow -> dropped whole, never truncated


def test_book_hash_ignores_level_order():
    assert book_hash([(1, 2), (3, 4)], [(5, 6)]) == book_hash([(3, 4), (1, 2)], [(5, 6)])
    assert book_hash([(1, 2)], []) != book_hash([], [(1, 2)])


# ---------------------------------------------------------------- WebSocket recorder
def frame(kind, seq, epoch=1, sid=1, **msg):
    return parse_ws_message(
        json.dumps({"type": kind, "sid": sid, "seq": seq, "msg": msg}),
        recv_ts_ns=1000 + seq,
        epoch=epoch,
    )


def SNAP(seq, **kw):  # noqa: N802
    return frame(
        "orderbook_snapshot",
        seq,
        market_ticker="T",
        yes_dollars_fp=[["0.4000", "10.00"]],
        no_dollars_fp=[["0.5000", "5.00"]],
        **kw,
    )


def DELTA(seq, d="1.00", **kw):  # noqa: N802
    return frame(
        "orderbook_delta",
        seq,
        market_ticker="T",
        price_dollars="0.4000",
        delta_fp=d,
        side="yes",
        ts_ms=1669149841000,
        **kw,
    )


def test_ws_recorder_stores_raw_events_and_replays_to_identical_book():
    store = Store()
    rec = WsBookRecorder(store, "run1")
    rec.handle(Connected(1, "wss://x", 1))
    for ev in (
        SNAP(1),
        DELTA(2, "5.00"),
        DELTA(3, "-3.00"),
        DELTA(3, "-3.00"),
    ):  # last is a duplicate
        assert rec.handle(ev) is False
    rec.flush()
    assert store.query("SELECT count(*) FROM book_deltas") == [(2,)]  # duplicate collapsed by PK
    assert store.query("SELECT epoch, sid, seq, yes_px, yes_qty FROM book_snapshots") == [
        (1, 1, 1, [4000], [1000])
    ]
    assert store.query("SELECT kind FROM stream_events") == [("connected",)]
    # replaying the stored facts through the same validator reproduces the live book
    from data.normalization.book_stream import BookStreamProcessor

    live = rec.proc.book("T").as_levels()
    replay = BookStreamProcessor()
    snap = store.query(
        "SELECT epoch, sid, seq, yes_px, yes_qty, no_px, no_qty FROM book_snapshots"
    )[0]
    replay.process(SNAP(snap[2]))
    for _epoch, _sid, seq, _side, _price, delta in store.query(
        "SELECT epoch, sid, seq, side, price, delta FROM book_deltas ORDER BY seq"
    ):
        replay.process(DELTA(seq, f"{delta / 100:.2f}"))
    expected = {"yes": [(4000, 1000 + 500 - 300)], "no": [(5000, 500)]}  # 10.00 + 5.00 - 3.00
    assert live == expected and replay.book("T").as_levels() == expected


def test_ws_recorder_flags_gap_requests_resync_and_records_incident():
    store = Store()
    rec = WsBookRecorder(store, "run1")
    rec.handle(Connected(1, "wss://x", 1))
    rec.handle(SNAP(1))
    assert rec.handle(DELTA(5)) is True  # seq 2..4 lost
    assert rec.proc.book("T") is None  # fails closed
    rec.handle(Disconnected(1, "resync", 2))
    rec.flush()
    kinds = [r[0] for r in store.query("SELECT kind FROM stream_events ORDER BY recv_ts_ns, kind")]
    assert "gap" in kinds and "disconnected" in kinds
    assert store.query("SELECT count(*) FROM book_deltas") == [
        (1,)
    ]  # raw event kept for forensic replay


def test_ws_recorder_quarantines_malformed_and_records_trades_and_lifecycle():
    store = Store()
    rec = WsBookRecorder(store, "run1")
    rec.handle(Malformed(1, "{{{", "invalid JSON", 7))
    rec.handle(
        parse_ws_message(
            json.dumps(
                {
                    "type": "trade",
                    "sid": 2,
                    "seq": 1,
                    "msg": {
                        "trade_id": "tw1",
                        "market_ticker": "T",
                        "yes_price_dollars": "0.3600",
                        "no_price_dollars": "0.6400",
                        "count_fp": "136.00",
                        "taker_side": "no",
                        "taker_book_side": "ask",
                        "ts_ms": 1669149841000,
                    },
                }
            )
        )
    )
    rec.handle(
        parse_ws_message(
            json.dumps(
                {
                    "type": "market_lifecycle_v2",
                    "sid": 3,
                    "seq": 1,
                    "msg": {
                        "market_ticker": "T",
                        "event_type": "settled",
                        "result": "yes",
                        "settlement_value": "1.0000",
                    },
                }
            ),
            recv_ts_ns=5_000_000_000,
        )
    )
    rec.flush()
    assert store.query("SELECT source, raw FROM quarantine") == [("ws", "{{{")]
    assert store.query("SELECT trade_id, source, taker_side FROM trades") == [("tw1", "ws", "no")]
    assert store.query(
        "SELECT event_type, result, settlement_value, source FROM market_status_log"
    ) == [("settled", "yes", 10000, "ws_lifecycle")]


# ---------------------------------------------------------------- config
def test_config_loading_and_strictness(tmp_path):
    p = tmp_path / "h.yaml"
    p.write_text(
        "job: history\nuniverse:\n  close_after: 2026-08-01\n  close_before: 2026-08-10\n  seed: 7\n"
    )
    cfg = load_config(p)
    assert (
        isinstance(cfg, HistoryConfig)
        and cfg.universe.close_after == D(8, 1)
        and cfg.universe.seed == 7
    )
    p.write_text(
        "job: history\nuniverse:\n  close_after: 2026-08-01\n  close_before: 2026-08-10\n  sample_fractoin: 0.5\n"
    )
    with pytest.raises(ConfigError, match="unknown keys"):
        load_config(p)
    p.write_text("job: books\nmode: carrier-pigeon\n")
    with pytest.raises(ConfigError, match="mode"):
        load_config(p)
    p.write_text("nothing: here\n")
    with pytest.raises(ConfigError, match="job"):
        load_config(p)


def test_shipped_configs_parse():
    from pathlib import Path

    for name in ("history.yaml", "books.yaml"):
        assert load_config(Path(__file__).parents[1] / "configs" / name)


async def test_a_failing_chunk_surfaces_as_the_original_error_type_not_an_exception_group(ex):
    """The run loop retries APIError/TransportError; a wrapped ExceptionGroup would kill the recorder."""
    import httpx

    from kalshi_client.exceptions import ServerError

    store = Store()
    p, _ = poller(
        ex, store, ["EVT-1-A", "EVT-1-B"], batch_size=1
    )  # two chunks -> concurrent fetches
    orig = ex.handler

    def flaky(request):
        if request.url.path.endswith(
            "/markets/orderbooks"
        ) and "EVT-1-B" in request.url.params.get_list("tickers"):
            return httpx.Response(500, json={"error": "boom"})
        return orig(request)

    ex.handler = flaky
    async with ex.rest() as rest:
        p.rest, p.run_id = rest, store.start_run("books", {})
        with pytest.raises(ServerError):
            await p.poll_once()
