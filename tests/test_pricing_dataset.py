import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from backtest.market_info import FORBIDDEN_FIELDS
from data.collectors.books import snapshot_row
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket, validate_rest
from market.contracts import Side, Trade
from market.order_book import OrderBook
from market.timeutil import to_epoch_us
from pricing import dataset as D
from pricing.probability_models import PartitionNormalizer
from tests.conftest import load_fixture
from tests.fakes import iso, make_market

SEC = 1_000_000_000


def ns(dt):
    return to_epoch_us(dt) * 1000


def add_market(
    store, ticker, event, close, *, result="yes", volume=500.0, hours_open=24, end_offset_min=60
):
    raw = make_market(
        load_fixture,
        ticker,
        event=event,
        close=close,
        opened=close - timedelta(hours=hours_open),
        result=result,
        volume=volume,
    )
    raw["expected_expiration_time"] = iso(close + timedelta(minutes=end_offset_min))
    m = normalize_market(validate_rest(ApiMarket, raw))
    store.upsert_markets([m], "r", partition="live")
    return m


def add_trades(store, ticker, times, price=(4500, 5500), side=Side.NO):
    trades = [
        Trade(f"{ticker}#{i}", ticker, price[0], price[1], 300, side, "ask", t)
        for i, t in enumerate(times)
    ]
    store.insert_trades(trades, source="rest_live", run_id="r")


BASE = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def history_db(path):
    s = Store(path)
    research = add_market(s, "KXA-E1-A", "KXA-E1", BASE, result="yes")  # closes Sep 5: research
    holdout = add_market(
        s, "KXB-E2-A", "KXB-E2", datetime(2026, 9, 10, 12, tzinfo=UTC), result="no"
    )
    scalar = add_market(s, "KXC-E3-A", "KXC-E3", BASE, result="yes")
    for m in (research, holdout, scalar):
        add_trades(s, m.ticker, [m.close_time - timedelta(hours=8, minutes=i) for i in range(5)])
    s.con.execute(
        "UPDATE markets SET settlement_value = 5000 WHERE ticker = 'KXC-E3-A'"
    )  # fractional
    s.close()


# ------------------------------------------------------------------ what a model may see
def test_an_observation_carries_no_field_that_reveals_the_future():
    fields = {f.name for f in dataclasses.fields(D.Observation)}
    assert not fields & FORBIDDEN_FIELDS  # close_time, status, result, volume, settlement_*...
    assert "y" not in fields and "settled_ns" not in fields  # the label lives elsewhere
    assert {f.name for f in dataclasses.fields(D.Outcome)} == {"y", "settled_ns"}
    assert {f.name for f in dataclasses.fields(D.Instance)} == {"obs", "out", "split", "dataset"}


# ------------------------------------------------------------------ the sealed holdout, history
def test_the_history_holdout_is_not_even_featurised_unless_explicitly_opened(tmp_path, monkeypatch):
    db = str(tmp_path / "h.duckdb")
    history_db(db)
    asked = []
    real = D.load_tapes

    def spy(store, tickers):
        tickers = list(tickers)
        asked.extend(tickers)
        return real(store, tickers)

    monkeypatch.setattr(D, "load_tapes", spy)
    D.ACCESS_LOG.clear()
    got = D.history_instances(db)
    assert {i.obs.ticker for i in got} == {
        "KXA-E1-A"
    }  # holdout market absent, scalar market unlabelled
    assert "KXB-E2-A" not in asked  # sealed: its trades were never even loaded
    assert D.ACCESS_LOG == [] and all(i.split == "research" for i in got)
    opened = D.history_instances(db, include_holdout=True)
    assert {i.obs.ticker for i in opened} == {"KXA-E1-A", "KXB-E2-A"}
    assert {i.split for i in opened} == {"research", "holdout"}
    assert len(D.ACCESS_LOG) == 1  # opening it is recorded
    D.ACCESS_LOG.clear()


def test_scalar_and_unsettled_markets_have_no_binary_label(tmp_path):
    db = str(tmp_path / "h.duckdb")
    history_db(db)
    got = D.history_instances(db, include_holdout=True)
    assert "KXC-E3-A" not in {i.obs.ticker for i in got}
    D.ACCESS_LOG.clear()


def test_instances_exist_only_while_the_market_was_open_and_have_trades_to_reference(tmp_path):
    db = str(tmp_path / "h.duckdb")
    history_db(db)
    got = D.history_instances(db, horizons_s=(7200, 3600, 900, 86_400 * 3))
    hours = sorted(round(i.obs.hours_to_expiry, 2) for i in got)
    # scheduled end = close + 60 min.  h=2h -> t = close-1h (open, has trades); h=1h -> t = close (closed:
    # no instance); h=15min -> after close (no instance); h=3d -> before the market opened (none)
    assert hours == [2.0]
    i = got[0]
    assert i.out.y == 1 and i.obs.n_siblings is None
    assert (
        i.obs.ref_source == "last_trade"
    )  # every print was a NO taker: no ask-side print, no implied mid
    assert i.obs.trade.n_seen == 5


def test_only_prints_visible_at_t_reach_the_features(tmp_path):
    db = str(tmp_path / "h.duckdb")
    s = Store(db)
    m = add_market(s, "KXA-E1-A", "KXA-E1", BASE)
    t_pred = ns(m.expected_expiration_time) - 7200 * SEC
    early = [m.close_time - timedelta(hours=8)]
    late = [
        datetime.fromtimestamp(t_pred / SEC, UTC) + timedelta(seconds=5)
    ]  # after the prediction time
    add_trades(s, m.ticker, early + late)
    s.close()
    (i,) = D.history_instances(db, horizons_s=(7200,))
    assert i.obs.trade.n_seen == 1  # the later print exists in the database but is invisible at t


# ------------------------------------------------------------------ event sizes and corpus
def test_event_sizes_count_stored_markets_and_unknown_is_not_one():
    assert D.n_bucket(0) == "?" and D.n_bucket(None) == "?" and D.n_bucket(1) == "1"


def test_event_sizes_and_the_volume_matched_corpus(tmp_path):
    db = str(tmp_path / "c.duckdb")
    s = Store(db)
    add_market(s, "KXA-E1-A", "KXA-E1", BASE, volume=5000.0, result="yes")
    add_market(s, "KXA-E1-B", "KXA-E1", BASE, volume=5.0, result="no")  # thin: below 100 contracts
    add_market(s, "KXB-E2-A", "KXB-E2", BASE, volume=5000.0, result="no")
    s.close()
    assert D.event_sizes(db) == {"KXA-E1": 2, "KXB-E2": 1}
    everything = D.corpus_rows(db, min_volume=None)
    matched = D.corpus_rows(db)  # default: the history dataset's own selection rule
    assert len(everything) == 3 and len(matched) == 2
    assert {r.series for r in matched} == {"KXA", "KXB"} and all(
        r.n_bucket in ("1", "2") for r in everything
    )
    assert [r.settled_ns for r in everything] == sorted(r.settled_ns for r in everything)


# ------------------------------------------------------------------ recorded books and their seal
def book_db(path, *, minutes=40, step_s=60):
    s = Store(path)
    m = add_market(s, "KXA-E1-A", "KXA-E1", BASE + timedelta(hours=1), result="yes")
    t0 = ns(BASE)
    rows, polls = [], []
    for k in range(minutes * 60 // step_s):
        t = t0 + k * step_s * SEC
        bk = OrderBook(m.ticker)
        bk.apply_snapshot([(4000 + 10 * (k % 5), 1000)], [(5000, 1000)])
        rows.append(
            snapshot_row(
                bk, recv_ts_ns=t, req_ts_ns=t - 300_000_000, source="rest_poll", run_id="r"
            )
        )
        polls.append(
            {
                "run_id": "r",
                "recv_ts_ns": t,
                "n_tickers": 1,
                "n_changed": 1,
                "n_missing": 0,
                "latency_ms": 5.0,
            }
        )
    s.insert_book_snapshots(rows)
    s.insert_poll_log(polls)
    add_trades(s, m.ticker, [BASE - timedelta(hours=2)])
    s.close()
    return t0


def test_recorded_book_instances_use_the_book_as_of_t_and_the_registry_seals_the_holdout(tmp_path):
    D.ACCESS_LOG.clear()
    db = tmp_path / "books_shortlived.duckdb"
    t0 = book_db(str(db))
    got = D.book_instances(str(db), step_s=900)
    assert [round((i.obs.t_ns - t0) / SEC) for i in got] == [
        60,
        960,
        1860,
    ]  # 15-minute grid from t0+60s
    assert all(i.split == "research" and i.obs.ref_source == "book_mid" for i in got)
    # the book at t is the last snapshot at or before t: k = t/60 -> bid 40c + 10*(k mod 5) ticks, ask 50c
    for i in got:
        k = round((i.obs.t_ns - t0) / SEC) // 60
        assert i.obs.book.bid == pytest.approx((4000 + 10 * (k % 5)) / 10_000)
        assert i.obs.book.ask == pytest.approx(0.50)
    unknown = tmp_path / "some_other.duckdb"
    unknown.write_bytes(db.read_bytes())
    with pytest.raises(ValueError):
        D.book_instances(str(unknown))  # not registered as research or holdout: refuse to guess
    sealed = tmp_path / "books_research_short.duckdb"
    sealed.write_bytes(db.read_bytes())
    with pytest.raises(D.SealedHoldoutError):
        D.book_instances(str(sealed))
    assert D.ACCESS_LOG == []
    opened = D.book_instances(str(sealed), include_holdout=True)
    assert {i.split for i in opened} == {"holdout"} and len(D.ACCESS_LOG) == 1
    D.ACCESS_LOG.clear()


def test_the_book_at_t_ignores_snapshots_recorded_after_t(tmp_path):
    D.ACCESS_LOG.clear()
    db = tmp_path / "books_shortlived.duckdb"
    t0 = book_db(str(db))
    before = D.book_instances(str(db), step_s=900)
    s = Store(str(db))  # rewrite every snapshot AFTER the first instance's time
    s.con.execute(
        f"UPDATE book_snapshots SET yes_px = [1000], yes_qty = [1] WHERE recv_ts_ns > {t0 + 60 * SEC}"
    )
    s.close()
    after = D.book_instances(str(db), step_s=900)
    assert (
        before[0].obs.book == after[0].obs.book
    )  # the first instance (t0+60s) cannot see the change


# ------------------------------------------------------------------ Model C
def obs_with(refs, pos, ref=None):
    o = dataclasses.replace(
        _bare_obs(refs[pos] if ref is None else ref),
        event_refs=tuple(refs) if refs else None,
        event_pos=pos,
    )
    return o


def _bare_obs(ref):
    from pricing.microstructure import TradeFeatures

    tf = TradeFeatures(0, None, None, {}, {}, {}, {}, None, None)
    return D.Observation("T", "E", "S", "c", "k", 2, 0, 1.0, ref, "book_mid", tf, None)


def test_partition_normaliser_projects_prices_onto_the_sum_to_one_constraint():
    m = PartitionNormalizer()
    refs = (0.60, 0.50)  # sums to 1.10: a 10-point overround
    assert m.predict(obs_with(refs, 0)) == pytest.approx(0.60 / 1.10)
    assert sum(m.predict(obs_with(refs, k)) for k in range(2)) == pytest.approx(1.0)
    three = (0.5, 0.3, 0.3)
    assert sum(m.predict(obs_with(three, k)) for k in range(3)) == pytest.approx(1.0)
    assert m.predict(obs_with(None, None, ref=0.42)) == pytest.approx(
        0.42
    )  # no partition: the reference
    assert not m.applies(obs_with(None, None, ref=0.42)) and m.applies(obs_with(refs, 0))
    already_coherent = (0.4, 0.6)
    assert m.predict(obs_with(already_coherent, 0)) == pytest.approx(0.4)  # nothing to fix


def test_structure_statistics_exclude_events_that_settle_after_the_cutoff(tmp_path):
    from data.normalization.normalizer import normalize_event
    from kalshi_client.models import ApiEvent
    from tests.fakes import event_payload

    db = str(tmp_path / "c.duckdb")
    s = Store(db)
    for ev in ("KXA-E1", "KXA-E2"):
        s.upsert_events(
            [normalize_event(validate_rest(ApiEvent, event_payload(load_fixture, ev)))], "r"
        )
    early = add_market(s, "KXA-E1-A", "KXA-E1", BASE)
    add_market(s, "KXA-E1-B", "KXA-E1", BASE, result="no")
    add_market(s, "KXA-E2-A", "KXA-E2", BASE + timedelta(days=3))
    add_market(s, "KXA-E2-B", "KXA-E2", BASE + timedelta(days=3), result="no")
    s.close()
    cutoff = (
        ns(early.close_time) + 86_400 * SEC
    )  # a day after the first event settled, before the second
    (before,) = list(D.structure_stats(db, cutoff))
    (everything,) = list(D.structure_stats(db, ns(BASE) + 30 * 86_400 * SEC))
    assert before.series == "KXA" and before.n_events == 1  # only the event already settled
    assert everything.n_events == 2  # with a later cutoff, both
    assert (
        D.structure_stats(db, ns(BASE)) is not None
        and len(list(D.structure_stats(db, ns(BASE)))) == 0
    )
