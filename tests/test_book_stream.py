import json

from hypothesis import given
from hypothesis import strategies as st

from data.normalization.book_stream import ApplyStatus as S
from data.normalization.book_stream import BookStreamProcessor, SeenSet
from kalshi_client.models import parse_ws_message
from market.contracts import Side
from market.order_book import OrderBook


def snap(seq, ticker="T", yes=(("0.4000", "10.00"),), no=(("0.5000", "5.00"),), sid=1, epoch=1):
    return parse_ws_message(
        json.dumps(
            {
                "type": "orderbook_snapshot",
                "sid": sid,
                "seq": seq,
                "msg": {
                    "market_ticker": ticker,
                    "yes_dollars_fp": list(yes),
                    "no_dollars_fp": list(no),
                },
            }
        ),
        epoch=epoch,
    )


def delta(seq, price="0.4000", d="1.00", side="yes", ticker="T", sid=1, epoch=1):
    return parse_ws_message(
        json.dumps(
            {
                "type": "orderbook_delta",
                "sid": sid,
                "seq": seq,
                "msg": {
                    "market_ticker": ticker,
                    "price_dollars": price,
                    "delta_fp": d,
                    "side": side,
                    "ts_ms": 1669149841000,
                },
            }
        ),
        epoch=epoch,
    )


def test_snapshot_then_deltas_build_book():
    p = BookStreamProcessor()
    assert p.process(snap(2)).applied
    assert p.process(delta(3, d="5.00")).applied
    assert p.process(delta(4, price="0.4000", d="-15.00")).applied
    book = p.book("T")
    assert book.best_bid(Side.YES) is None and book.best_bid(Side.NO).qty == 500
    assert book.last_seq == 4 and book.last_ts.year == 2022


def test_first_seq_need_not_be_one():
    p = BookStreamProcessor()
    assert p.process(snap(57)).applied and p.process(delta(58)).applied


def test_duplicate_is_dropped_and_not_double_applied():
    p = BookStreamProcessor()
    p.process(snap(1))
    assert p.process(delta(2, d="5.00")).applied
    r = p.process(delta(2, d="5.00"))
    assert r.status is S.DUPLICATE
    assert p.book("T").best_bid(Side.YES).qty == 1500  # applied exactly once
    assert not p.needs_resync  # duplicates are benign


def test_out_of_order_is_dropped():
    p = BookStreamProcessor()
    p.process(snap(1))
    p.process(delta(2))
    p.process(delta(3))
    assert p.process(delta(2)).status is S.OUT_OF_ORDER
    assert p.book("T") is not None and not p.needs_resync


def test_gap_poisons_stream_and_fails_closed():
    p = BookStreamProcessor()
    p.process(snap(1, ticker="A"))
    p.process(snap(2, ticker="B"))
    r = p.process(delta(5, ticker="A"))  # seq 3,4 lost
    assert r.status is S.GAP and p.needs_resync
    # neither market is trustworthy: we cannot know which one the lost frames touched
    assert p.book("A") is None and p.book("B") is None
    assert p.process(delta(6, ticker="B")).status is S.STALE_STREAM  # nothing applied after a gap
    assert p.counters["gap"] == 1


def test_reconnect_epoch_clears_state_and_recovers():
    p = BookStreamProcessor()
    p.process(snap(1))
    p.process(delta(3))  # gap
    assert p.book("T") is None and p.needs_resync
    p.on_connected(2)
    assert p.book("T") is None and not p.needs_resync  # nothing trusted until re-snapshotted
    assert p.process(snap(1, epoch=2)).applied and p.book("T") is not None
    # the same (sid, seq) values in a new epoch are not duplicates of the old epoch
    assert p.process(delta(2, epoch=2)).applied


def test_delta_before_snapshot_requests_resync():
    p = BookStreamProcessor()
    r = p.process(delta(1))
    assert r.status is S.NO_SNAPSHOT and p.needs_resync and p.book("T") is None


def test_inconsistent_delta_marks_book_stale_until_snapshot_repairs():
    p = BookStreamProcessor()
    p.process(snap(1))
    r = p.process(delta(2, d="-999.00"))  # removes more than exists
    assert r.status is S.INCONSISTENT and p.needs_resync and p.book("T") is None
    assert p.process(snap(3)).applied and p.book("T") is not None  # fresh snapshot repairs it


def test_missing_seq_is_rejected_not_assumed_ok():
    p = BookStreamProcessor()
    raw = json.dumps(
        {
            "type": "orderbook_snapshot",
            "sid": 1,
            "msg": {"market_ticker": "T", "yes_dollars_fp": [], "no_dollars_fp": []},
        }
    )
    assert p.process(parse_ws_message(raw)).status is S.INCONSISTENT


def test_multiple_sids_tracked_independently():
    p = BookStreamProcessor()
    p.process(snap(1, ticker="A", sid=1))
    p.process(snap(1, ticker="B", sid=2))
    p.process(delta(2, ticker="A", sid=1))
    assert p.process(delta(2, ticker="B", sid=2)).applied


def test_non_book_messages_pass_through():
    p = BookStreamProcessor()
    t = parse_ws_message(json.dumps({"type": "ticker", "sid": 3, "msg": {"market_ticker": "T"}}))
    assert p.process(t).status is S.NOT_A_BOOK_MESSAGE


@given(
    st.lists(
        st.tuples(st.sampled_from(["yes", "no"]), st.integers(1, 5), st.integers(1, 9)),
        min_size=1,
        max_size=60,
    )
)
def test_processor_equals_direct_application(ops):
    """Streaming through the processor == applying the same deltas straight to an OrderBook."""
    p, ref = BookStreamProcessor(), OrderBook("T")
    p.process(snap(1, yes=(), no=()))
    for i, (side, cents, qty) in enumerate(ops, start=2):
        p.process(delta(i, price=f"0.{cents:02d}00", d=f"{qty}.00", side=side))
        ref.apply_delta(Side(side), cents * 100, qty * 100)
    assert p.book("T").as_levels() == ref.as_levels()


def test_seen_set_dedupes_and_bounds_memory():
    s = SeenSet(maxlen=3)
    assert s.add("a") and not s.add("a")
    for k in "bcd":
        s.add(k)
    assert len(s) == 3 and s.add("a")  # 'a' was evicted (LRU), so it counts as new again
