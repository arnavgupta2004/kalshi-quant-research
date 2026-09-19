"""The replay tool: forward-fills recorded books, applies fee metadata, tracks episodes."""

from data.collectors.books import snapshot_row
from data.normalization.fixtures import load_event_fixture
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiSeries
from market.order_book import OrderBook
from scripts.stage4_arbitrage_demo import replay


def make_db(path, violate_between=(1, 3)):
    ev, ms = load_event_fixture("atp_match")  # two players, exchange-declared mutually exclusive
    a, b = sorted(m.ticker for m in ms)
    t0 = 1_800_000_000_000_000_000
    step = 5_000_000_000

    def bk(t, yes, no):
        o = OrderBook(t)
        o.apply_snapshot([(yes, 100 * 500)], [(no, 100 * 500)])
        return o

    with Store(path) as s:
        s.upsert_events([ev], "r")
        s.upsert_markets(ms, "r", partition="live")
        s.upsert_series(
            [
                ApiSeries.model_validate(
                    {
                        "ticker": "KXATPMATCH",
                        "fee_type": "quadratic_with_maker_fees",
                        "fee_multiplier": 1,
                    }
                )
            ]
        )
        rows = [
            snapshot_row(bk(a, 4500, 4500), recv_ts_ns=t0, source="rest_poll", run_id="r"),
            snapshot_row(bk(b, 4500, 4500), recv_ts_ns=t0, source="rest_poll", run_id="r"),
        ]
        lo, hi = violate_between  # A's YES bid jumps to 0.62 (bids sum 1.07) for cycles lo..hi-1
        rows.append(
            snapshot_row(
                bk(a, 6200, 3000), recv_ts_ns=t0 + lo * step + 1, source="rest_poll", run_id="r"
            )
        )
        rows.append(
            snapshot_row(
                bk(a, 4500, 4500), recv_ts_ns=t0 + hi * step + 1, source="rest_poll", run_id="r"
            )
        )
        s.insert_book_snapshots(rows)
        s.insert_poll_log(
            [
                {
                    "run_id": "r",
                    "recv_ts_ns": t0 + i * step + 10,
                    "n_tickers": 2,
                    "n_changed": 0,
                    "n_missing": 0,
                    "latency_ms": 1.0,
                }
                for i in range(5)
            ]
        )
    return a, b


def test_replay_finds_one_episode_and_costs_change_the_funnel(tmp_path):
    make_db(tmp_path / "books.duckdb")
    s = replay(
        str(tmp_path / "books.duckdb"), None, tmp_path / "out", target=3000, min_level="DECLARED"
    )
    base, free = s["variants"]["baseline"], s["variants"]["fee_free"]
    assert s["cycles"] == 5 and s["fee_metadata_series"] == 1
    assert base["funnel"]["displayed"] == 2 and base["episodes"] == 1  # cycles 1 and 2 are violated
    assert base["episode_median_cycles"] == 2
    assert base["fee_assumed_displayed"] == 0  # KXATPMATCH fee metadata was recorded and used
    # 0.62 + 0.45 bids -> 7c of margin; fees are ~3.5c, so it survives and is executable at 30 contracts
    assert base["funnel"]["executable"] == 2 and base["by_class"] == {"executable": 2}
    top = base["top"][0]
    assert top["class"] == "executable" and top["gross"] > top["net"] > 0
    assert (
        abs(top["gross"] - top["slippage"] - top["fees"] - top["net"]) < 1e-9
    )  # identity survives serialisation
    assert free["top"][0]["net"] > base["top"][0]["net"]  # fee-free is strictly better
    assert s["variants"]["fees_x2"]["top"][0]["net"] < base["top"][0]["net"]
    assert (tmp_path / "out" / "replay.json").exists()


def test_replay_forward_fills_and_sees_nothing_when_books_stay_coherent(tmp_path):
    make_db(
        tmp_path / "books.duckdb", violate_between=(9, 10)
    )  # violation would start after the last cycle
    s = replay(
        str(tmp_path / "books.duckdb"), None, tmp_path / "out", target=3000, min_level="DECLARED"
    )
    assert (
        s["variants"]["baseline"]["funnel"]["displayed"] == 0
        and s["variants"]["baseline"]["episodes"] == 0
    )
    assert (
        s["variants"]["baseline"]["best_margin_ticks_by_kind"]["mutually_exclusive"] < 0
    )  # closest approach reported
