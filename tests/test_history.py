from datetime import UTC, datetime, timedelta

import pytest

from data.collectors.config import HistoryConfig, UniverseSpec
from data.collectors.history import HistoryCollector, already_final, trade_partitions
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket, validate_rest
from tests.fakes import CUTOFF, FakeExchange, event_payload, make_market, make_trade

D = lambda m, d, h=0: datetime(2026, m, d, h, tzinfo=UTC)  # noqa: E731
LIVE_SPEC = UniverseSpec(close_after=D(9, 1), close_before=D(9, 10))
CFG = HistoryConfig(universe=LIVE_SPEC, concurrency=3, requests_per_second=1e6)


def scenario(fx, n_trades=7):
    """3 eligible live markets (2 events) + 2 that must be filtered out."""
    live = [
        make_market(fx, "SERA-E1-A", close=D(9, 5)),
        make_market(fx, "SERA-E1-B", close=D(9, 6)),
        make_market(fx, "SERB-E1-A", close=D(9, 7), result="no"),
        make_market(fx, "SERC-E1-A", close=D(9, 5), volume=5),  # too thin
        make_market(fx, "SERD-E1-A", close=D(9, 5), status="active"),  # not settled
    ]
    trades = {
        m["ticker"]: [
            make_trade(m["ticker"], i, D(8, 30) + timedelta(hours=i)) for i in range(n_trades)
        ]
        for m in live
    }
    events = {t: event_payload(fx, t) for t in ("SERA-E1", "SERB-E1", "SERC-E1", "SERD-E1")}
    return FakeExchange(fx, live=live, trades=trades, events=events)


async def collect(ex, store, cfg=CFG, **kw):
    async with ex.rest() as rest:
        return await HistoryCollector(store, rest, cfg, **kw).run()


async def test_full_run_populates_every_table(fx):
    ex, store = scenario(fx), Store()
    s = await collect(ex, store)
    assert s.scan.selected == 3 and s.markets_upserted == 3 and not s.failures
    assert store.query("SELECT ticker FROM markets ORDER BY ticker") == [
        ("SERA-E1-A",),
        ("SERA-E1-B",),
        ("SERB-E1-A",),
    ]
    assert store.query("SELECT count(*), count(DISTINCT trade_id) FROM trades") == [(21, 21)]
    assert store.query("SELECT count(*) FROM events") == [(2,)]  # only events of selected markets
    assert store.query("SELECT count(*) FROM trade_sync") == [(3,)]
    assert store.query("SELECT ticker, result FROM markets ORDER BY ticker")[2] == (
        "SERB-E1-A",
        "no",
    )
    assert store.query("SELECT status FROM collection_runs") == [("ok",)]
    assert ex.count("/historical/trades") == 0  # window entirely after the cutoff
    assert store.query("SELECT DISTINCT source FROM trades") == [("rest_live",)]


async def test_rerun_is_idempotent_and_makes_no_trade_requests(fx):
    ex, store = scenario(fx), Store()
    await collect(ex, store)
    fp1, trade_reqs = store.fingerprint(), ex.count("/markets/trades")
    s2 = await collect(ex, store)
    assert store.fingerprint()["fingerprint"] == fp1["fingerprint"]
    assert s2.trades_inserted == 0 and s2.partitions_skipped == 3 and s2.partitions_synced == 0
    assert ex.count("/markets/trades") == trade_reqs  # finished settled markets are not re-fetched
    assert store.query("SELECT count(*) FROM collection_runs") == [(2,)]
    assert store.query("SELECT count(*) FROM market_status_log") == [
        (3,)
    ]  # no spurious state changes


async def test_crash_mid_run_then_resume_equals_a_clean_run(fx, monkeypatch):
    import data.collectors.history as history

    monkeypatch.setattr(history, "FLUSH_TRADES", 2)  # write partial pages so a crash leaves them
    cfg = HistoryConfig(universe=LIVE_SPEC, concurrency=1, requests_per_second=1e6)
    clean = Store()
    await collect(scenario(fx), clean, cfg)

    ex, store = scenario(fx), Store()
    ex.crash_after = 9  # A completes; B is killed after its first trade page was flushed
    with pytest.raises(ExceptionGroup):
        await collect(ex, store, cfg)
    assert store.get_sync("SERA-E1-A", "live") is not None
    partial_b = store.query("SELECT count(*) FROM trades WHERE ticker = 'SERA-E1-B'")[0][0]
    assert 0 < partial_b < 7  # rows on disk ...
    assert store.get_sync("SERA-E1-B", "live") is None  # ... but the checkpoint never advanced
    assert store.query("SELECT status FROM collection_runs") == [("failed",)]

    ex.crash_after = None
    s2 = await collect(ex, store, cfg)  # resume: A skipped, B refetched over its partial rows
    assert s2.partitions_skipped == 1 and s2.partitions_synced == 2
    assert store.query("SELECT count(*), count(DISTINCT trade_id) FROM trades") == [(21, 21)]
    assert store.query("SELECT count(*) FROM trade_sync") == [(3,)]
    assert store.fingerprint()["fingerprint"] == clean.fingerprint()["fingerprint"]


async def test_hard_kill_leaves_running_row_that_next_run_marks_abandoned(fx):
    ex, store = scenario(fx), Store()
    store.start_run("history", {}, "x")  # a process that died without finishing
    await collect(ex, store)
    assert sorted(r[0] for r in store.query("SELECT status FROM collection_runs")) == [
        "abandoned",
        "ok",
    ]


async def test_one_failing_market_does_not_stop_the_rest_and_is_retried_next_run(fx):
    ex, store = scenario(fx), Store()
    ex.fail_tickers = {"SERB-E1-A"}
    s = await collect(ex, store)
    assert len(s.failures) == 1 and "SERB-E1-A" in s.failures[0]
    assert store.query("SELECT count(*) FROM trades") == [(14,)]
    assert store.get_sync("SERB-E1-A", "live") is None  # checkpoint NOT advanced for the failure
    ex.fail_tickers = set()
    s2 = await collect(ex, store)
    assert s2.trades_inserted == 7 and s2.partitions_skipped == 2 and not s2.failures
    assert store.query("SELECT count(*) FROM trades") == [(21,)]


async def test_missing_event_gets_a_stub_and_is_not_refetched(fx):
    ex, store = scenario(fx), Store()
    del ex.events["SERB-E1"]
    s = await collect(ex, store)
    assert s.events_missing == 1
    assert store.query(
        "SELECT raw->>'missing', category FROM events WHERE event_ticker = 'SERB-E1'"
    ) == [("true", None)]
    before = ex.count("/events/")
    await collect(ex, store)
    assert ex.count("/events/") == before


async def test_malformed_trades_are_quarantined_not_stored_and_not_fatal(fx):
    ex, store = scenario(fx), Store()
    ex.bad_trade_ids = {"SERA-E1-A#2", "SERB-E1-A#4"}
    s = await collect(ex, store)
    assert s.quarantined == 2
    assert store.query("SELECT count(*) FROM trades") == [(19,)]
    q = store.query("SELECT source, raw FROM quarantine")
    assert len(q) == 2 and all("0.40009" in r[1] for r in q)


async def test_trades_in_both_partitions_for_a_market_straddling_the_cutoff(fx):
    m = make_market(fx, "STR-E1-A", close=D(7, 21), created=D(7, 15), opened=D(7, 18))
    before = [make_trade("STR-E1-A", i, D(7, 19, i)) for i in range(4)]
    after = [make_trade("STR-E1-A", 10 + i, D(7, 20, 1 + i)) for i in range(4)]
    ex = FakeExchange(
        fx,
        live=[m],
        hist=[m],
        trades={"STR-E1-A": before + after},
        events={"STR-E1": event_payload(fx, "STR-E1")},
    )
    cfg = HistoryConfig(
        universe=UniverseSpec(close_after=D(7, 1), close_before=D(8, 1)), concurrency=2
    )
    await collect(ex, Store(), cfg)  # smoke; below uses a store we can inspect
    store = Store()
    await collect(ex, store, cfg)
    assert store.query("SELECT source, count(*) FROM trades GROUP BY source ORDER BY source") == [
        ("rest_historical", 4),
        ("rest_live", 4),
    ]
    assert store.query("SELECT partition FROM markets") == [
        ("live",)
    ]  # listed in both -> counted once
    assert {r[0] for r in store.query("SELECT partition FROM trade_sync")} == {"historical", "live"}


def test_trade_partition_rule(fx):
    mk = lambda **kw: normalize_market(validate_rest(ApiMarket, make_market(fx, "X-E-A", **kw)))  # noqa: E731
    now = D(9, 19)
    assert trade_partitions(mk(close=D(7, 1), opened=D(6, 25)), CUTOFF, now) == ["historical"]
    assert trade_partitions(mk(close=D(9, 1), opened=D(8, 25)), CUTOFF, now) == ["live"]
    assert trade_partitions(mk(close=D(9, 1), opened=D(6, 25)), CUTOFF, now) == [
        "historical",
        "live",
    ]


async def test_late_trades_after_checkpoint_are_fetched_without_duplicating(fx):
    m = make_market(fx, "OPEN-E-A", close=D(9, 30), status="active")
    ex = FakeExchange(
        fx,
        live=[m],
        trades={"OPEN-E-A": [make_trade("OPEN-E-A", i, D(9, 10, i)) for i in range(5)]},
    )
    store = Store()
    market = normalize_market(validate_rest(ApiMarket, m))
    clock = [D(9, 12)]
    async with ex.rest() as rest:
        c = HistoryCollector(
            store, rest, HistoryConfig(universe=LIVE_SPEC, overlap_s=120), now=lambda: clock[0]
        )
        c.run_id = store.start_run("history", {})
        await c._sync_trades(market, "live")
        assert store.query("SELECT count(*) FROM trades") == [(5,)]
        ex.trades["OPEN-E-A"] += [make_trade("OPEN-E-A", i, D(9, 13, i)) for i in range(5, 8)]
        clock[0] = D(9, 14)
        ex.requests.clear()
        await c._sync_trades(market, "live")  # market not final -> must re-sync from the checkpoint
    assert store.query("SELECT count(*), count(DISTINCT trade_id) FROM trades") == [(8, 8)]
    floor = int((D(9, 12) - timedelta(seconds=60) - timedelta(seconds=120)).timestamp())
    assert (
        int(ex.requests[0].url.params["min_ts"]) == floor
    )  # resumed from checkpoint - margin - overlap
    assert store.get_sync("OPEN-E-A", "live")["n_trades"] == 8


def test_already_final_requires_sync_completed_after_settlement(fx):
    m = normalize_market(validate_rest(ApiMarket, make_market(fx, "X-E-A", close=D(9, 5))))
    assert m.status.is_terminal and m.settlement_ts is not None
    assert not already_final(None, m)
    assert not already_final(
        {"completed_at": m.settlement_ts - timedelta(minutes=1)}, m
    )  # synced pre-settlement
    assert already_final({"completed_at": m.settlement_ts + timedelta(minutes=1)}, m)
