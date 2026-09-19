from datetime import UTC, datetime, timedelta

from backtest.events import BookConfirm, BookUpdate, Settlement, TradeTick
from data.collectors.books import snapshot_row
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket, validate_rest
from market.contracts import Side, Trade
from market.order_book import OrderBook
from market.timeutil import to_epoch_us
from research.dataset import load_dataset
from tests.conftest import load_fixture
from tests.fakes import iso, make_market

BASE = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def ns(dt):
    return to_epoch_us(dt) * 1000


def build_db(path):
    """A market recorded for 20 s, whose trade tape reaches back 10 days and which settled later."""
    raw = make_market(
        load_fixture,
        "T-E-A",
        close=BASE + timedelta(hours=2),
        opened=BASE - timedelta(days=10),
    )
    raw.update(
        expected_expiration_time=iso(BASE + timedelta(hours=2)),
        settlement_ts=iso(BASE + timedelta(hours=3)),
        volume_fp="123456.00",
    )
    s = Store(path)
    m = normalize_market(validate_rest(ApiMarket, raw))
    s.upsert_markets([m], "r", partition="live")
    bk = OrderBook("T-E-A")
    bk.apply_snapshot([(4500, 1000)], [(5000, 500)])
    s.insert_book_snapshots(
        [
            snapshot_row(
                bk,
                recv_ts_ns=ns(BASE) + i * 10_000_000_000,
                req_ts_ns=ns(BASE) + i * 10_000_000_000 - 300_000_000,
                source="rest_poll",
                run_id="r",
            )
            for i in range(3)
        ]
    )
    s.insert_poll_log(
        [
            {
                "run_id": "r",
                "recv_ts_ns": ns(BASE) + i * 10_000_000_000,
                "n_tickers": 1,
                "n_changed": 1 if i == 0 else 0,
                "n_missing": 0,
                "latency_ms": 5.0,
            }
            for i in range(3)
        ]
        + [
            {  # a final poll after the last stored book: the recorder was alive until then
                "run_id": "r",
                "recv_ts_ns": ns(BASE) + 25_000_000_000,
                "n_tickers": 1,
                "n_changed": 0,
                "n_missing": 0,
                "latency_ms": 5.0,
            }
        ]
    )
    trades = [
        Trade("old", "T-E-A", 4500, 5500, 300, Side.NO, "ask", BASE - timedelta(days=9)),
        Trade("in", "T-E-A", 4500, 5500, 300, Side.NO, "ask", BASE + timedelta(seconds=7)),
        Trade("late", "T-E-A", 4500, 5500, 300, Side.NO, "ask", BASE + timedelta(minutes=5)),
    ]
    s.insert_trades(trades, source="rest_live", run_id="r")
    s.close()


def test_the_feed_is_windowed_to_the_recording_period(tmp_path):
    """Without windowing the trade tape (10 days deep) and the later settlement would stretch the
    clock far outside the time books were actually recorded - the first smoke run's 12,164-minute
    'span' for a 64-minute recording."""
    db = str(tmp_path / "b.duckdb")
    build_db(db)
    ds = load_dataset(db, "t")
    lo, hi = ds.window_ns
    assert lo == ns(BASE) and hi == ns(BASE) + 25_000_000_000  # first book .. last poll
    events = list(ds.feed)
    assert all(lo <= e.ts_ns <= hi for e in events)
    assert sum(isinstance(e, BookUpdate) for e in events) == 3
    assert sum(isinstance(e, BookConfirm) for e in events) == 4
    assert [e.trade.trade_id for e in events if isinstance(e, TradeTick)] == ["in"]  # not old/late
    assert not any(isinstance(e, Settlement) for e in events)  # settled after the window


def test_outcomes_are_kept_apart_from_the_feed_for_scoring_only(tmp_path):
    db = str(tmp_path / "b.duckdb")
    build_db(db)
    ds = load_dataset(db, "t")
    assert set(ds.settled) == {"T-E-A"}  # known post hoc, used to score - never fed to the scanner
    assert ds.categories["T-E-A"] and ds.fingerprint
    res = ds.scan()
    assert res.recording_s == 25.0 and res.cycles == 4
