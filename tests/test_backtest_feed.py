from datetime import UTC, datetime, timedelta

from backtest.events import BookUpdate, MarketClose, Priority, Settlement, TradeTick
from backtest.feed import FeedTiming, ListFeed, StoreFeed
from backtest.market_info import MarketInfo
from data.collectors.books import snapshot_row
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket, validate_rest
from market.contracts import Side, Trade
from market.order_book import OrderBook
from market.timeutil import to_epoch_us
from tests.fakes import iso, make_market

BASE = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)


def ns(dt):
    return to_epoch_us(dt) * 1000


def build_store(close_early=True):
    """A settled market: scheduled to end at 14:00 but it actually closed at 12:30 (the leak)."""
    raw = make_market(
        None if False else __import__("tests.conftest", fromlist=["load_fixture"]).load_fixture,
        "T-E-A",
        close=BASE + timedelta(minutes=30),
        opened=BASE - timedelta(hours=2),
    )
    raw.update(
        expected_expiration_time=iso(BASE + timedelta(hours=2)),
        settlement_ts=iso(BASE + timedelta(minutes=35)),
        volume_fp="123456.00",
    )
    s = Store()
    m = normalize_market(validate_rest(ApiMarket, raw))
    s.upsert_markets([m], "r", partition="live")
    bk = OrderBook("T-E-A")
    bk.apply_snapshot([(4500, 1000)], [(5000, 500)])
    rows = [
        snapshot_row(
            bk,
            recv_ts_ns=ns(BASE) + i * 5_000_000_000,
            req_ts_ns=ns(BASE) + i * 5_000_000_000 - 300_000_000,
            source="rest_poll",
            run_id="r",
        )
        for i in range(3)
    ]
    s.insert_book_snapshots(rows)
    trades = [
        Trade(f"t{i}", "T-E-A", 4500, 5500, 300, Side.NO, "ask", BASE + timedelta(seconds=7 * i))
        for i in range(3)
    ]
    s.insert_trades(trades, source="rest_live", run_id="r")
    return s, m


def test_store_feed_orders_events_and_applies_information_delays():
    s, m = build_store()
    feed = StoreFeed(
        s,
        ["T-E-A"],
        timing=FeedTiming(trade_delay_ns=250_000_000, settlement_delay_ns=2_000_000_000),
    )
    events = list(feed)
    keys = [(e.ts_ns, int(e.priority)) for e in events]
    assert keys == sorted(keys)
    kinds = [type(e).__name__ for e in events]
    assert kinds.count("BookUpdate") == 3 and kinds.count("TradeTick") == 3
    assert kinds.count("MarketClose") == 1 and kinds.count("Settlement") == 1
    first_trade = next(e for e in events if isinstance(e, TradeTick))
    assert (
        first_trade.ts_ns == ns(BASE) + 250_000_000
    )  # printed at 12:00:00, KNOWN a quarter second later
    settle = next(e for e in events if isinstance(e, Settlement))
    assert (
        settle.ts_ns == ns(BASE + timedelta(minutes=35)) + 2_000_000_000
        and settle.value == m.settlement_value
    )
    close = next(e for e in events if isinstance(e, MarketClose))
    assert close.ts_ns == ns(BASE + timedelta(minutes=30))  # revealed at the moment it happened
    assert events.index(close) < events.index(settle)
    assert events[0].priority == Priority.BOOK and len(feed) == len(events)


def test_no_event_before_its_time_reveals_the_outcome_or_the_realised_close():
    s, m = build_store()
    events = list(StoreFeed(s, ["T-E-A"]))
    close_ts = ns(BASE + timedelta(minutes=30))
    for e in events:
        if e.ts_ns < close_ts:
            assert isinstance(
                e, (BookUpdate, TradeTick)
            )  # only market data before the close is announced
    info = StoreFeed(s, ["T-E-A"]).infos["T-E-A"]
    assert isinstance(info, MarketInfo)
    assert info.scheduled_end == BASE + timedelta(
        hours=2
    )  # the schedule, NOT the 12:30 realised close
    assert (
        not hasattr(info, "close_time")
        and not hasattr(info, "volume")
        and not hasattr(info, "result")
    )


def test_window_clamps_pre_window_facts_to_the_start_and_drops_later_ones():
    s, _ = build_store()
    late_start = ns(
        BASE + timedelta(hours=1)
    )  # after the close and the settlement have already happened
    events = list(StoreFeed(s, ["T-E-A"], start_ns=late_start))
    assert [type(e).__name__ for e in events] == [
        "Settlement",
        "MarketClose",
    ]  # known at window start
    assert all(e.ts_ns == late_start for e in events)  # clamped: the strategy learns it on day one
    early = list(StoreFeed(s, ["T-E-A"], end_ns=ns(BASE) + 10_000_000_000))
    assert not any(isinstance(e, (MarketClose, Settlement)) for e in early)
    assert all(e.ts_ns <= ns(BASE) + 10_000_000_000 for e in early)


def test_unknown_tickers_and_markets_without_data_are_handled():
    s, _ = build_store()
    assert list(StoreFeed(s, ["NOPE"])) == [] and StoreFeed(s, ["NOPE"]).infos == {}
    assert len(StoreFeed(s, None).markets) == 1


def test_read_markets_roundtrips_the_domain_object():
    s, m = build_store()
    [got] = s.read_markets(["T-E-A"])
    for f in (
        "ticker",
        "event_ticker",
        "status",
        "result",
        "settlement_value",
        "strike",
        "rules",
        "open_time",
        "close_time",
        "expected_expiration_time",
        "settlement_ts",
        "volume",
    ):
        assert getattr(got, f) == getattr(m, f), f
    assert s.read_markets(["nope"]) == [] and len(s.read_markets()) == 1


def test_list_feed_truncation_keeps_only_what_had_arrived_by_then():
    e = [BookUpdate(t, "A", (), ()) for t in (5, 1, 9, 3)]
    feed = ListFeed(e)
    assert [x.ts_ns for x in feed] == [1, 3, 5, 9]
    assert [x.ts_ns for x in feed.truncated(5)] == [1, 3, 5]


def test_poll_log_becomes_book_confirmations_within_the_window():
    from backtest.events import BookConfirm

    s, _ = build_store()
    polls = [ns(BASE) + i * 5_000_000_000 + 400_000_000 for i in range(4)]
    s.insert_poll_log(
        [
            {
                "run_id": "r",
                "recv_ts_ns": t,
                "n_tickers": 1,
                "n_changed": 0,
                "n_missing": 0,
                "latency_ms": 1.0,
            }
            for t in polls
        ]
    )
    confirms = [e for e in StoreFeed(s, ["T-E-A"]) if isinstance(e, BookConfirm)]
    assert [e.ts_ns for e in confirms] == polls and all(e.ticker == "" for e in confirms)
    windowed = [
        e
        for e in StoreFeed(s, ["T-E-A"], start_ns=polls[1], end_ns=polls[2])
        if isinstance(e, BookConfirm)
    ]
    assert [e.ts_ns for e in windowed] == polls[1:3]
    assert not [
        e for e in StoreFeed(s, ["T-E-A"], confirm_with_polls=False) if isinstance(e, BookConfirm)
    ]
