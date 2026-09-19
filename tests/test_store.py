import time
from datetime import UTC, datetime, timedelta

import pytest

from data.normalization.normalizer import normalize_event, normalize_market
from data.storage.duckdb_store import Store, as_utc
from kalshi_client.models import ApiEvent, ApiMarket, validate_rest
from market.contracts import Side, Trade

T0 = datetime(2026, 9, 1, 12, 0, 0, 123456, tzinfo=UTC)


def mk(fx, name="market_active.json", **over):
    raw = dict(fx(name), **over)
    return normalize_market(validate_rest(ApiMarket, raw))


def trade(i, ticker="T", t=T0):
    return Trade(f"id{i}", ticker, 3000, 7000, 150 + i, Side.YES, "bid", t + timedelta(seconds=i))


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


def test_schema_is_idempotent_and_versioned(tmp_path):
    p = tmp_path / "d.duckdb"
    with Store(p) as s:
        s.insert_trades([trade(1)], source="rest_live", run_id="r")
    with Store(p) as s:  # reopen: no re-creation errors, data intact
        assert s.counts()["trades"] == 1
        assert s.query("SELECT version FROM schema_version") == [(1,)]


def test_trade_insert_dedupes_on_trade_id_and_reports_new_rows(store):
    assert store.insert_trades([trade(1), trade(2), trade(1)], source="a", run_id="r") == 2
    # overlapping re-ingest from a *different source* must not duplicate
    assert store.insert_trades([trade(2), trade(3)], source="ws", run_id="r2") == 1
    assert store.query("SELECT count(*), count(DISTINCT trade_id) FROM trades") == [(3, 3)]
    # first writer wins: provenance of the original row is preserved
    assert store.query("SELECT source FROM trades WHERE trade_id = 'id2'") == [("a",)]


def test_timestamps_roundtrip_as_utc_microseconds(store):
    store.insert_trades([trade(0)], source="s", run_id="r")
    got = as_utc(store.query("SELECT created_time FROM trades")[0][0])
    assert got == T0 and got.tzinfo is UTC


def test_naive_datetimes_are_refused(store):
    with pytest.raises(ValueError, match="naive"):
        store.insert_trades([trade(0, t=datetime(2026, 1, 1))], source="s", run_id="r")


def test_market_upsert_preserves_first_seen_and_logs_only_state_changes(store, fx):
    t1, t2, t3 = T0, T0 + timedelta(hours=1), T0 + timedelta(hours=2)
    active = mk(fx)
    store.upsert_markets([active], "r1", partition="live", now=t1)
    store.upsert_markets([active], "r2", partition="live", now=t2)  # unchanged state
    assert store.query("SELECT count(*) FROM market_status_log") == [(1,)]
    row = store.query("SELECT first_seen_at, last_seen_at, run_id, volume FROM markets")[0]
    assert as_utc(row[0]) == t1 and as_utc(row[1]) == t2 and row[2] == "r2"
    assert row[3] == active.volume
    # a status/result change is appended, with the LOCAL observation time
    changed = mk(fx, status="closed")
    store.upsert_markets([changed], "r3", partition="live", now=t3)
    log = store.query("SELECT status, observed_at FROM market_status_log ORDER BY observed_at")
    assert [r[0] for r in log] == ["active", "closed"] and as_utc(log[1][1]) == t3
    assert store.query("SELECT status FROM markets") == [("closed",)]


def test_status_log_survives_reopen_without_duplicating(tmp_path, fx):
    p = tmp_path / "d.duckdb"
    m = mk(fx)
    with Store(p) as s:
        s.upsert_markets([m], "r1", partition="live", now=T0)
    with Store(p) as s:
        s.upsert_markets([m], "r2", partition="live", now=T0 + timedelta(hours=1))
        assert s.query("SELECT count(*) FROM market_status_log") == [(1,)]


def test_json_and_list_columns_roundtrip(store, fx):
    ev = normalize_event(validate_rest(ApiEvent, fx("event.json")))
    store.upsert_events([ev], "r", now=T0)
    m = mk(fx)
    store.upsert_markets([m], "r", partition="historical", now=T0)
    tickers, srcs = store.query("SELECT market_tickers, settlement_sources FROM events")[0]
    assert tickers == list(ev.market_tickers) and "name" in srcs
    rng = store.query("SELECT price_ranges->>'$[0].step' FROM markets")[0][0]
    assert int(rng) == m.price_ranges[0].step
    assert store.query("SELECT partition, settlement_value FROM markets") == [("historical", None)]


def test_scalar_settlement_persists_as_fraction(store, fx):
    store.upsert_markets([mk(fx, "market_settled_scalar.json")], "r", partition="live", now=T0)
    res, val = store.query("SELECT result, settlement_value FROM markets")[0]
    assert res == "scalar" and 0 < val < 10_000


def test_view_joins_event_category(store, fx):
    ev = normalize_event(validate_rest(ApiEvent, fx("event.json")))
    m = mk(fx, event_ticker=ev.event_ticker)
    store.upsert_events([ev], "r", now=T0)
    store.upsert_markets([m], "r", partition="live", now=T0)
    assert store.query("SELECT category FROM markets_enriched") == [(ev.category,)]


def test_book_snapshot_arrays_and_latest_hash(store):
    row = dict(
        ticker="T",
        recv_ts_ns=5,
        source="rest_poll",
        req_ts_ns=4,
        yes_px=[4500, 4000],
        yes_qty=[300, 100],
        no_px=[5300],
        no_qty=[150],
        book_hash="h1",
        run_id="r",
    )
    assert store.insert_book_snapshots([row, dict(row, recv_ts_ns=9, book_hash="h2")]) == 2
    assert store.insert_book_snapshots([row]) == 0  # PK dedupe
    assert store.latest_book_hashes() == {"T": ("h2", 9)}
    assert store.query("SELECT yes_px, no_qty FROM book_snapshots WHERE recv_ts_ns = 5") == [
        ([4500, 4000], [150])
    ]


def test_run_lifecycle_abandons_stale_runs(store):
    a = store.start_run("history", {"x": 1}, "abc")
    b = store.start_run("history", {"x": 2}, "abc")  # 'a' never finished -> its process died
    store.finish_run(b, "ok")
    assert dict(store.query("SELECT run_id, status FROM collection_runs")) == {
        a: "abandoned",
        b: "ok",
    }


def test_sync_checkpoint_upsert(store):
    assert store.get_sync("T", "live") is None
    store.set_sync("T", "live", complete_through=T0, n_trades=5, run_id="r")
    store.set_sync("T", "live", complete_through=T0 + timedelta(days=1), n_trades=9, run_id="r2")
    got = store.get_sync("T", "live")
    assert got["complete_through"] == T0 + timedelta(days=1) and got["n_trades"] == 9


def test_bulk_ingest_is_fast_not_the_2k_rows_per_second_path(store):
    trades = [trade(i, ticker=f"M{i % 50}") for i in range(60_000)]
    t0 = time.perf_counter()
    assert store.insert_trades(trades, source="s", run_id="r") == 60_000
    assert time.perf_counter() - t0 < 10  # the executemany path needs ~30 s for this


def test_fingerprint_depends_on_content_not_ingest_order_or_run(fx):
    a, b = Store(":memory:"), Store(":memory:")
    ts = [trade(i) for i in range(5)]
    a.insert_trades(ts, source="x", run_id="run-a", now=T0)
    b.insert_trades(ts[::-1], source="y", run_id="run-b", now=T0 + timedelta(days=3))
    assert a.fingerprint()["fingerprint"] == b.fingerprint()["fingerprint"]
    b.insert_trades([trade(99)], source="y", run_id="run-b")
    assert a.fingerprint()["fingerprint"] != b.fingerprint()["fingerprint"]


def test_volume_reconciliation_flags_missing_and_extra_trades(store, fx):
    m = mk(fx, "market_settled_yes.json", ticker="V-E-A", volume_fp="10.00")  # 1000 centi
    store.upsert_markets([m], "r", partition="live", now=T0)
    assert store.reconcile_volume() == {"checked": 0, "mismatches": []}  # not synced -> not judged
    store.set_sync("V-E-A", "live", complete_through=T0, n_trades=0, run_id="r")
    ts = [
        Trade(f"v{i}", "V-E-A", 5000, 5000, 400, Side.YES, "bid", T0 + timedelta(seconds=i))
        for i in range(3)
    ]
    store.insert_trades(
        ts[:2], source="s", run_id="r"
    )  # 800 stored vs 1000 reported: a page went missing
    r = store.reconcile_volume()
    assert r["checked"] == 1 and r["mismatches"] == [
        {"ticker": "V-E-A", "exchange_volume": 1000, "stored_volume": 800}
    ]
    store.insert_trades(ts[2:], source="s", run_id="r")  # 1200 > 1000: excess
    assert store.reconcile_volume()["mismatches"][0]["stored_volume"] == 1200
    store.con.execute("DELETE FROM trades WHERE trade_id = 'v2'")
    store.insert_trades(
        [Trade("v9", "V-E-A", 5000, 5000, 200, Side.NO, "ask", T0)], source="s", run_id="r"
    )
    assert store.reconcile_volume() == {"checked": 1, "mismatches": []}  # 400+400+200 == 1000


def test_orphaned_bulk_temp_files_from_a_hard_kill_are_removed_but_foreign_files_are_not(tmp_path):
    (tmp_path / "kalshi_bulk_abc.ndjson").write_text("{}")  # stranded by a kill -9 mid-ingest
    (tmp_path / "someones_notes.ndjson").write_text("{}")
    with Store(tmp_path / "d.duckdb") as s:
        s.insert_trades([trade(1)], source="s", run_id="r")
        assert not list(
            tmp_path.glob("kalshi_bulk_*")
        )  # own orphan gone, and none leaked by ingest
    assert (tmp_path / "someones_notes.ndjson").exists()
