"""Paper trading: the adapter, the tape, the live feed, the trader (with parity against the plain
backtest engine), the sources, and the promise that nothing here can place an order."""

import asyncio
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backtest.engine import BacktestConfig
from backtest.events import BookConfirm, BookUpdate, MarketClose, Settlement, TradeTick, Wake
from backtest.fills import QueueModel
from backtest.market_info import MarketInfo
from kalshi_client.models import ApiOrderbook, ApiTrade, parse_ws_message, validate_rest
from kalshi_client.websocket import Connected, Disconnected, KalshiWebSocket, Malformed
from market.contracts import Rules, Side, Strike, Trade
from market.fees import FeeBook, FeeSchedule
from market.timeutil import now_ns
from market_making.baseline import BaselineParams, BinaryMarketMaker
from paper import tape
from paper.adapter import WsAdapter
from paper.sources import RestPollSource, WsSource
from paper.strategies import adaptive_params
from paper.trader import LiveFeed, PaperTrader, compare, replay
from tests.test_websocket import MockExchange, make_client

SEC = 1_000_000_000
ROOT = Path(__file__).resolve().parents[1]
YES = (("0.4500", "500.00"),)
NO = (("0.4500", "500.00"),)


def frame(kind, seq, msg, sid=1, **env):
    return json.dumps({"type": kind, "sid": sid, "seq": seq, **env, "msg": msg})


def snap(seq, ticker="A", yes=YES, no=NO, sid=1, ts=None):
    return frame(
        "orderbook_snapshot", seq,
        {"market_ticker": ticker, "yes_dollars_fp": [list(x) for x in yes], "no_dollars_fp": [list(x) for x in no]},
        sid,
    )  # fmt: skip


def delta(seq, price="0.4500", d="-100.00", side="yes", ticker="A", sid=1):
    return frame(
        "orderbook_delta", seq,
        {"market_ticker": ticker, "price_dollars": price, "delta_fp": d, "side": side}, sid,
    )  # fmt: skip


def trade_frame(tid="t1", ticker="A", yes="0.4600", no="0.5400", count="20.00", side="no", sid=2):
    msg = {
        "trade_id": tid,
        "market_ticker": ticker,
        "yes_price_dollars": yes,
        "no_price_dollars": no,
        "count_fp": count,
        "taker_side": side,
        "ts_ms": int(time.time() * 1000),
    }
    return json.dumps({"type": "trade", "sid": sid, "msg": msg})


def lifecycle(ticker="A", **kw):
    return json.dumps(
        {"type": "market_lifecycle_v2", "sid": 3, "msg": {"market_ticker": ticker, **kw}}
    )


def parsed(raw, ts, epoch=1):
    return parse_ws_message(raw, recv_ts_ns=ts, epoch=epoch)


def info(t="A", event="E1", end_in_h=10.0):
    end = datetime.now(UTC) + timedelta(hours=end_in_h)
    return MarketInfo(t, event, "KX", "Sports", "t", "y", "n", Strike(), Rules(), None, end, ())


def cfg(latency_ms=50):
    ns = latency_ms * 1_000_000
    return BacktestConfig(
        initial_cash_micro=10**11, latency_submit_ns=ns, latency_ack_ns=ns, latency_cancel_ns=ns,
        fees=FeeBook({}, FeeSchedule(known=False)), maker=QueueModel.optimistic(), equity_sample_ns=10**12,
    )  # fmt: skip


FAST = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0)


def factory():
    return BinaryMarketMaker(["A"], FAST, event_of={"A": "E1"})


# ------------------------------------------------------------------ the tape
def test_every_event_type_survives_a_round_trip(tmp_path):
    tr = Trade("t1", "A", 4600, 5400, 2000, Side.NO, "ask", datetime(2026, 9, 20, tzinfo=UTC), True)
    events = [
        BookUpdate(5, "A", ((4500, 100), (4400, 50)), ((4500, 100),)),
        BookUpdate(6, "A", (), ()),
        BookConfirm(7, "A"),
        BookConfirm(8),
        TradeTick(9, "A", tr),
        TradeTick(
            10,
            "A",
            Trade("t2", "A", 5000, 5000, 100, None, None, datetime(2026, 9, 20, tzinfo=UTC)),
        ),
        MarketClose(11, "A"),
        Settlement(12, "A", 10_000),
        Wake(13, tape.TICK_TAG),
    ]
    p = tmp_path / "t.jsonl"
    assert tape.write_all(p, events) == len(events)
    assert list(tape.read(p)) == events


def test_a_wake_that_is_not_a_clock_tick_is_not_a_tape_record():
    with pytest.raises(TypeError):
        tape.dump(Wake(1, "mm", "A"))


# ------------------------------------------------------------------ the live feed
def test_the_live_feed_ticks_when_idle_never_goes_backwards_and_ends_on_close(tmp_path):
    w = tape.TapeWriter(tmp_path / "f.jsonl")
    feed = LiveFeed(tick_s=0.02, tape_writer=w)
    feed.push(BookUpdate(10**18, "A", ((4500, 100),), ((4500, 100),)))
    feed.push(BookConfirm(10**18 - 5))  # out of order by 5 ns: clamped, not passed back
    a, b = next(feed), next(feed)
    assert a.ts_ns == 10**18 and b.ts_ns == 10**18
    t = next(feed)  # nothing queued: a tick
    assert isinstance(t, Wake) and t.tag == tape.TICK_TAG and t.ts_ns >= 10**18
    feed.close()
    with pytest.raises(StopIteration):
        next(feed)
    w.close()
    assert [type(e) for e in tape.read(tmp_path / "f.jsonl")] == [BookUpdate, BookConfirm, Wake]


# ------------------------------------------------------------------ the adapter
def test_snapshots_and_deltas_become_full_depth_book_updates():
    ad = WsAdapter(["A"])
    ad.on_stream_event(Connected(1, "ws://x", 1))
    out, resync = ad.on_stream_event(
        parsed(snap(1, yes=(("0.4500", "5.00"), ("0.4400", "2.00"))), 10)
    )
    assert not resync and out == [BookUpdate(10, "A", ((4500, 500), (4400, 200)), ((4500, 50000),))]
    out, _ = ad.on_stream_event(parsed(delta(2, "0.4500", "-3.00"), 11))
    assert out == [BookUpdate(11, "A", ((4500, 200), (4400, 200)), ((4500, 50000),))]
    out, _ = ad.on_stream_event(parsed(delta(3, "0.4600", "1.00"), 12))  # a new best bid
    assert out[0].yes_bids[0] == (4600, 100)


def test_trades_and_lifecycle_are_translated_and_other_markets_ignored():
    ad = WsAdapter(["A"])
    ad.on_stream_event(Connected(1, "x", 1))
    out, _ = ad.on_stream_event(parsed(trade_frame(), 20))
    assert len(out) == 1 and isinstance(out[0], TradeTick) and out[0].ts_ns == 20
    assert out[0].trade.taker_side is Side.NO and out[0].trade.count == 2000
    assert ad.on_stream_event(parsed(trade_frame("t9", ticker="OTHER"), 21))[0] == []
    assert ad.on_stream_event(parsed(lifecycle(event_type="closed"), 22))[0] == [
        MarketClose(22, "A")
    ]
    assert ad.on_stream_event(parsed(lifecycle(settlement_value="1.0000", result="yes"), 23))[
        0
    ] == [Settlement(23, "A", 10_000)]
    assert ad.on_stream_event(parsed(lifecycle(event_type="whatever"), 24))[0] == []
    assert ad.counters["lifecycle_ignored"] == 1


def test_books_of_markets_outside_the_universe_are_not_passed_on():
    ad = WsAdapter(["A"])
    ad.on_stream_event(Connected(1, "x", 1))
    out, _ = ad.on_stream_event(parsed(snap(1, ticker="OTHER"), 10))
    assert out == []  # tracked internally (sequence numbers), but the strategy never sees it
    out, _ = ad.on_stream_event(parsed(snap(2, ticker="A"), 11))
    assert [e.ticker for e in out] == ["A"]


def test_a_malformed_frame_is_an_incident_and_produces_nothing():
    ad = WsAdapter(["A"])
    out, _ = ad.on_stream_event(Malformed(1, "garbage", "bad json", 5))
    assert out == [] and ad.counters["malformed"] == 1 and ad.incidents[-1].kind == "malformed"


def test_confirmations_flow_only_while_healthy_and_for_trustworthy_books():
    ad = WsAdapter(["A", "B"], confirm_s=1.0, stale_s=15.0)
    assert ad.tick(10 * SEC) == []  # never connected
    ad.on_stream_event(Connected(1, "x", 10 * SEC))
    ad.on_stream_event(parsed(snap(1, "A"), 10 * SEC))  # A has a book, B has none
    got = ad.tick(11 * SEC)
    assert got == [BookConfirm(11 * SEC, "A")]  # B is unknown: no confirmation for it
    assert ad.tick(11 * SEC + SEC // 2) == []  # not due yet
    assert ad.tick(12 * SEC + 1)[0].ts_ns == 12 * SEC + 1
    assert ad.tick(30 * SEC) == []  # 20 s of silence > stale_s: unhealthy
    ad.on_stream_event(parsed(delta(2), 31 * SEC))  # a frame arrives: healthy again
    assert ad.tick(32 * SEC) != []
    ad.on_stream_event(Disconnected(1, "closed", 33 * SEC))
    assert ad.tick(34 * SEC) == []  # disconnected: the books age and the strategy pulls its quotes


def test_a_sequence_gap_poisons_the_book_requests_a_resync_and_stops_its_confirmations():
    ad = WsAdapter(["A"])
    ad.on_stream_event(Connected(1, "x", SEC))
    ad.on_stream_event(parsed(snap(1), SEC))
    assert ad.tick(2 * SEC) != []
    out, resync = ad.on_stream_event(parsed(delta(5), 3 * SEC))  # seq 2..4 lost
    assert out == [] and resync
    assert ad.tick(4 * SEC) == []  # untrustworthy: not confirmed, so it will age out of use
    assert {i.kind for i in ad.incidents} >= {"gap", "resync_requested"}
    ad.on_stream_event(Connected(2, "x", 5 * SEC))  # the reconnect
    out, _ = ad.on_stream_event(parsed(snap(1, sid=1), 5 * SEC, epoch=2))
    assert out and ad.tick(6 * SEC) != []  # a fresh snapshot restores it


# ------------------------------------------------------------------ the trader, live, with parity
class Scripted:
    """A source that emits ``make(ts)`` events after real delays, timestamped when produced."""

    def __init__(self, steps):
        self.steps, self.incidents = steps, []

    async def events(self):
        for delay, make in self.steps:
            await asyncio.sleep(delay)
            yield make(now_ns())

    async def aclose(self):
        return None


def book(ts):
    return BookUpdate(ts, "A", ((4500, 50000),), ((4500, 50000),))


def hit_bid(ts):
    tr = Trade("t1", "A", 4600, 5400, 2000, Side.NO, "ask", datetime.now(UTC))
    return TradeTick(ts, "A", tr)


async def test_a_live_session_places_orders_logs_them_and_matches_the_backtest_of_its_tape(
    tmp_path,
):
    steps = [
        (0.0, book),
        (0.4, hit_bid),
        (0.3, book),
        (0.3, lambda ts: BookConfirm(ts)),
        (0.3, book),
    ]
    trader = PaperTrader({"A": info()}, factory, cfg(), tmp_path, tick_s=0.05)
    s = await trader.run(Scripted(steps), duration_s=10)
    assert s["mode"].startswith("PAPER") and s["orders"] >= 2 and s["fills"] >= 1
    assert s["parity_with_backtest_of_the_tape"]["identical"], s["parity_with_backtest_of_the_tape"]
    kinds = [
        json.loads(line)["kind"] for line in (tmp_path / "decisions.jsonl").read_text().splitlines()
    ]
    assert kinds.count("order") == s["orders"]
    assert kinds.count("fill") == s["fills"] >= 1  # each fill logged exactly once
    lines = (tmp_path / "tape.jsonl").read_text().splitlines()
    assert sum(1 for x in lines if json.loads(x)["k"] == "tick") >= 3  # 0.3-0.4 s gaps, 50 ms ticks
    assert len(lines) == sum(s["events_to_engine"].values())
    assert (
        s["reaction_ms"]["n"] > 0 and s["reaction_ms"]["p99"] < 500
    )  # the strategy reacts promptly
    assert (tmp_path / "summary.json").exists()


async def test_the_session_replays_identically_with_a_fresh_strategy(tmp_path):
    steps = [(0.0, book), (0.3, hit_bid), (0.3, book)]
    trader = PaperTrader({"A": info()}, factory, cfg(), tmp_path, tick_s=0.05)
    await trader.run(Scripted(steps), duration_s=10)
    a = replay(tmp_path / "tape.jsonl", {"A": info()}, factory(), cfg())
    b = replay(tmp_path / "tape.jsonl", {"A": info()}, factory(), cfg())
    assert compare(a, b)["identical"]  # deterministic, so parity is meaningful


async def test_the_parity_check_detects_a_strategy_that_behaves_differently(tmp_path):
    steps = [(0.0, book), (0.3, hit_bid), (0.3, book)]
    trader = PaperTrader({"A": info()}, factory, cfg(), tmp_path, tick_s=0.05)
    await trader.run(Scripted(steps), duration_s=10)
    wider = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0, size=3.0)
    other = replay(
        tmp_path / "tape.jsonl",
        {"A": info()},
        BinaryMarketMaker(["A"], wider, event_of={"A": "E1"}),
        cfg(),
    )
    same = replay(tmp_path / "tape.jsonl", {"A": info()}, factory(), cfg())
    assert compare(same, same)["identical"]
    d = compare(same, other)
    assert not d["identical"] and "orders" in d["differs_in"]


@pytest.mark.parametrize(
    "field", ["price", "qty", "fee_micro", "liquidity", "ticker", "side", "exec_ts"]
)
async def test_the_parity_check_looks_at_every_field_of_every_fill(tmp_path, field):
    import dataclasses

    trader = PaperTrader({"A": info()}, factory, cfg(), tmp_path, tick_s=0.05)
    await trader.run(Scripted([(0.0, book), (0.3, hit_bid), (0.3, book)]), duration_s=10)
    res = replay(tmp_path / "tape.jsonl", {"A": info()}, factory(), cfg())
    assert res.fills
    change = {
        "price": lambda f: f.price + 1, "qty": lambda f: f.qty + 1, "fee_micro": lambda f: f.fee_micro + 1,
        "liquidity": lambda f: "taker", "ticker": lambda f: "Z", "exec_ts": lambda f: f.exec_ts + 1,
        "side": lambda f: Side.NO if f.side is Side.YES else Side.YES,
    }[field]  # fmt: skip
    bent = dataclasses.replace(
        res, fills=[dataclasses.replace(f, **{field: change(f)}) for f in res.fills]
    )
    assert compare(res, res)["identical"]
    assert compare(res, bent)["differs_in"] == ["fills"]


async def test_an_error_in_the_strategy_is_raised_not_swallowed(tmp_path):
    class Boom:
        def on_event(self, ctx, ev):
            if isinstance(ev, BookUpdate):
                raise RuntimeError("boom")

    trader = PaperTrader({"A": info()}, Boom, cfg(), tmp_path, tick_s=0.05)
    with pytest.raises(RuntimeError, match="boom"):
        await trader.run(Scripted([(0.0, book), (0.2, book)]), duration_s=5)


async def test_the_kill_switch_fires_live_and_is_logged(tmp_path):
    lim = BaselineParams(min_requote_s=0.0, busy_s=0.05, risk_check_s=0.0)
    from dataclasses import replace

    from market_making.risk import RiskLimits

    p = replace(lim, limits=RiskLimits(max_drawdown_usd=1.0))
    steps = [
        (0.0, book),
        (0.3, hit_bid),  # we are long ~20 YES
        (
            0.3,
            lambda ts: BookUpdate(ts, "A", ((500, 50000),), ((9000, 50000),)),
        ),  # the market collapses
        (0.3, lambda ts: BookUpdate(ts, "A", ((500, 50000),), ((9000, 50000),))),
    ]
    trader = PaperTrader(
        {"A": info()},
        lambda: BinaryMarketMaker(["A"], p, event_of={"A": "E1"}),
        cfg(),
        tmp_path,
        tick_s=0.05,
    )
    s = await trader.run(Scripted(steps), duration_s=10)
    assert s["kill_switch"] is True
    rows = [json.loads(x) for x in (tmp_path / "decisions.jsonl").read_text().splitlines()]
    assert any(r["kind"] == "risk" and r["event"] == "kill-switch" for r in rows)
    assert s["parity_with_backtest_of_the_tape"]["identical"]


# ------------------------------------------------------------------ the real WebSocket client, end to end
async def test_the_websocket_path_end_to_end_with_a_gap_and_a_reconnect(tmp_path):
    good = [snap(1), trade_frame("t1")]
    bad = [snap(1), delta(9)]  # sequence gap on the first connection: must resync (reconnect)
    ex = MockExchange([bad, good], hold_open=True)  # only a resync can make the client reconnect
    async with ex.serve() as server:
        ws = make_client(
            server, channels=["orderbook_delta", "trade"], market_tickers=["A"], max_reconnects=1,
            stale_timeout=30.0,
        )  # fmt: skip
        adapter = WsAdapter(["A"])
        trader = PaperTrader({"A": info()}, factory, cfg(), tmp_path, tick_s=0.05)
        s = await trader.run(WsSource(ws, adapter, tick_s=0.05), duration_s=3)
    kinds = [i.kind for i in adapter.incidents]
    assert kinds.count("connected") == 2 and "gap" in kinds and "resync_requested" in kinds
    assert ex.connections == 2
    rows = [json.loads(x) for x in (tmp_path / "tape.jsonl").read_text().splitlines()]
    assert [r["k"] for r in rows if r["k"] in ("book", "trade")] == ["book", "book", "trade"]
    assert sum(r["k"] == "trade" for r in rows) == 1  # only the second connection's trade
    assert s["parity_with_backtest_of_the_tape"]["identical"]
    assert s["n_incidents"] >= 4


# ------------------------------------------------------------------ the REST poll source
class FakeRest:
    def __init__(self, books, trades):
        self.books, self.trades = list(books), list(trades)
        self.calls = 0

    async def get_orderbooks(self, tickers, *, batch_size=None):
        self.calls += 1
        b = self.books[min(self.calls - 1, len(self.books) - 1)]
        return {t: validate_rest(ApiOrderbook, b) for t in tickers}

    async def iter_trades(self, *, min_ts=None, page_size=500, **_):
        batch = self.trades.pop(0) if self.trades else []
        for t in batch:
            yield validate_rest(ApiTrade, t)


def api_book(yes="0.4500", no="0.4500"):
    return {"orderbook_fp": {"yes_dollars": [[yes, "100.00"]], "no_dollars": [[no, "100.00"]]}}


def api_trade(tid, ticker="A", yes="0.4600"):
    return {
        "trade_id": tid, "ticker": ticker, "created_time": "2026-09-20T10:00:00Z",
        "yes_price_dollars": yes, "no_price_dollars": f"{1 - float(yes):.4f}", "count_fp": "5.00",
        "taker_side": "no",
    }  # fmt: skip


async def test_rest_polling_emits_changed_books_ordered_deduped_trades_and_a_confirmation_per_cycle():
    rest = FakeRest(
        books=[api_book(), api_book(), api_book("0.4600")],
        trades=[
            [api_trade("t2"), api_trade("t1")],
            [api_trade("t2"), api_trade("t3"), api_trade("x", "OTHER")],
            [],
        ],
    )

    async def no_sleep(_):
        return None

    src = RestPollSource(rest, ["A"], interval_s=0, sleep=no_sleep)
    out = []
    async for ev in src.events():
        out.append(ev)
        if sum(isinstance(e, BookConfirm) for e in out) == 3:
            break
    kinds = [type(e).__name__ for e in out]
    assert kinds.count("BookUpdate") == 2  # cycle 1 (new) and cycle 3 (changed); cycle 2 unchanged
    assert kinds.count("BookConfirm") == 3
    ids = [e.trade.trade_id for e in out if isinstance(e, TradeTick)]
    assert ids == ["t1", "t2", "t3"]  # oldest first, duplicates and other markets dropped
    assert [e for e in out if isinstance(e, BookUpdate)][1].yes_bids[0][0] == 4600


# ------------------------------------------------------------------ the frozen Stage 10 models
def test_the_adaptive_strategy_uses_the_frozen_stage_10_coefficients():
    m = json.loads((ROOT / "results/stage10/dev/adaptive.json").read_text())["models"]
    p = adaptive_params(BaselineParams())
    assert (
        p.fv.names == ("stale_dev", "stale_out")
        and list(p.fv.w) == m["fair_value_staleness"]["weights"]
    )
    assert p.scale.target == "abs" and p.scale.intercept == m["scale"]["intercept"]
    assert (
        p.sigma_ref == m["sigma_ref"] and p.kappa == 1.0 and p.size_by_scale and p.ttr_skew == 1.0
    )


# ------------------------------------------------------------------ nothing here can place an order
def test_no_order_entry_exists_in_the_client_or_the_paper_package():
    bad = re.compile(
        r"portfolio/orders|(?:client|http|httpx|session)\.(?:post|put|delete|patch)\(|"
        r"\.request\(\s*[\"'](?:POST|PUT|DELETE|PATCH)",
        re.I,
    )
    hits = []
    for pkg in ("kalshi_client", "paper"):
        for f in (ROOT / pkg).glob("*.py"):
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if bad.search(line) and not line.lstrip().startswith("#"):
                    hits.append(f"{f.name}:{i}: {line.strip()}")
    assert not hits, hits


def test_the_websocket_client_still_refuses_non_public_channels():
    from kalshi_client.config import KalshiConfig
    from kalshi_client.exceptions import ConfigurationError

    with pytest.raises(ConfigurationError):
        KalshiWebSocket(KalshiConfig(), channels=["orders"], require_auth=False)


# ------------------------------------------------------------------ choosing the universe
async def test_the_universe_selection_ranks_events_by_volume_keeps_siblings_and_never_splits_an_event():
    from types import SimpleNamespace as NS

    from data.collectors.config import BookSelection
    from paper.universe import select_markets

    soon = datetime.now(UTC) + timedelta(hours=1)
    later = datetime.now(UTC) + timedelta(hours=30)

    def m(t, e, vol, due=soon):
        return NS(
            ticker=t,
            event_ticker=e,
            volume_24h_fp=vol,
            expected_expiration_time=due,
            close_time=due,
        )

    markets = [
        m("BIG-1", "KXBIG-E", 500), m("BIG-2", "KXBIG-E", 0), m("BIG-3", "KXBIG-E", 0),
        m("MID-1", "KXMID-E", 300), m("MID-2", "KXMID-E", 10),
        m("FAR-1", "KXFAR-E", 900, later),  # outside the horizon
        m("DEAD-1", "KXDEAD-E", 0),  # no volume
    ]  # fmt: skip

    class Rest:
        async def iter_markets(self, status=None):
            for x in markets:
                yield x

    got = await select_markets(
        Rest(), BookSelection(top_events=5, max_tickers=100, max_hours_to_expiry=3)
    )
    assert [x.ticker for x in got] == ["BIG-1", "BIG-2", "BIG-3", "MID-1", "MID-2"]  # siblings kept
    got = await select_markets(
        Rest(), BookSelection(top_events=5, max_tickers=2, max_hours_to_expiry=3)
    )
    assert [x.ticker for x in got] == [
        "MID-1",
        "MID-2",
    ]  # BIG does not fit whole: skipped, not truncated
    got = await select_markets(
        Rest(), BookSelection(top_events=1, max_tickers=100, max_hours_to_expiry=3)
    )
    assert {x.event_ticker for x in got} == {"KXBIG-E"}
