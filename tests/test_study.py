"""The study/scan reporting tools: they must *detect* violations, not just run."""

import json
from dataclasses import replace
from pathlib import Path

from data.collectors.books import snapshot_row
from data.storage.duckdb_store import Store
from market.contracts import SettlementResult
from market.order_book import OrderBook
from scripts.stage3_relationship_study import scan_books, study
from tests.test_semantics import load

NAMES = (
    "nyc_high",
    "btc_range",
    "btc_ladder",
    "epl_3way",
    "atp_match",
    "mlb_spread",
    "tennis_total_games",
)


def build_db(path: Path, names=NAMES, corrupt=None):
    with Store(path) as s:
        for n in names:
            ev, ms = load(n)
            if corrupt and n == corrupt[0]:
                ms = corrupt[1](ms)
            s.upsert_events([ev], "r")
            s.upsert_markets(ms, "r", partition="events_study")
    return path


def test_study_finds_no_violations_in_real_settled_events_and_reports_structure(tmp_path):
    db = build_db(tmp_path / "rel.duckdb")
    s = study(str(db), tmp_path / "out")
    assert s["settled_events"] == 7 and s["conflicts"]["count"] == 0
    by = {
        (r["kind"], r["level"]): r for r in s["relations_by_kind_level"] if not r["scalar_involved"]
    }
    assert (
        by[("partition", "LATTICE")]["instances"] == 2
        and by[("partition", "LATTICE")]["violated"] == 0
    )
    assert by[("union", "LATTICE")]["instances"] > 100 and by[("union", "LATTICE")]["violated"] == 0
    assert by[("chain", "PROVEN")]["violated"] == 0
    assert all(
        r["violated"] == 0
        for r in s["relations_by_kind_level"]
        if r["level"] in ("PROVEN", "LATTICE")
    )
    assert (tmp_path / "out" / "relations_summary.json").exists()
    assert json.loads((tmp_path / "out" / "relations_summary.json").read_text())[
        "dataset_fingerprint"
    ]


def test_study_detects_a_settlement_that_breaks_a_proven_relation(tmp_path):
    def two_yes(
        ms,
    ):  # make TWO disjoint temperature buckets resolve YES - impossible if buckets are disjoint
        yes = [replace(m, result=SettlementResult.YES, settlement_value=10000) for m in ms[:2]]
        return yes + list(ms[2:])

    db = build_db(tmp_path / "rel.duckdb", corrupt=("nyc_high", two_yes))
    s = study(str(db), tmp_path / "out")
    bad = [
        r
        for r in s["relations_by_kind_level"]
        if r["violated"] and r["level"] in ("PROVEN", "LATTICE")
    ]
    assert {r["kind"] for r in bad} >= {"mutually_exclusive", "partition"}
    assert any("KXHIGHNY" in ex for v in s["violation_examples"].values() for ex in v)


def test_study_separates_scalar_settlements_as_resolution_risk(tmp_path):
    def void_pair(ms):  # a cancelled 3-way event where every outcome voids at $0.50
        return [replace(m, result=SettlementResult.SCALAR, settlement_value=5000) for m in ms]

    db = build_db(tmp_path / "rel.duckdb", names=("epl_3way",), corrupt=("epl_3way", void_pair))
    s = study(str(db), tmp_path / "out")
    row = next(r for r in s["relations_by_kind_level"] if r["kind"] == "mutually_exclusive")
    # three voids at 0.5 sum to 1.5 > 1: the declared exclusivity is broken by resolution, and is reported as such
    assert row["scalar_involved"] is True and row["violated"] == 1
    assert s["flag_audit"]["with_scalar"] == 1


def test_scan_forward_fills_books_and_flags_only_the_incoherent_interval(tmp_path):
    ev, ms = load("atp_match")  # two-outcome match, exchange-declared mutually exclusive
    a, b = sorted(m.ticker for m in ms)
    db = tmp_path / "books.duckdb"

    def book(t, yes_bid_ticks, no_bid_ticks):
        bk = OrderBook(t)
        bk.apply_snapshot([(yes_bid_ticks, 1000)], [(no_bid_ticks, 1000)])
        return bk

    with Store(db) as s:
        s.upsert_events([ev], "r")
        s.upsert_markets(ms, "r", partition="live")
        t0 = 1_000_000_000_000
        rows = []
        for t, ts, yb, nb in [
            (a, t0, 5000, 4500),
            (b, t0, 4500, 5000),  # bids sum 0.95 <= 1: coherent
            (a, t0 + 20, 6500, 3000),  # A's YES bid jumps: 0.65 + 0.45 = 1.10 > 1: violation
            (a, t0 + 40, 5000, 4500),  # ... and is repaired
        ]:
            rows.append(
                snapshot_row(book(t, yb, nb), recv_ts_ns=ts, source="rest_poll", run_id="r")
            )
        s.insert_book_snapshots(rows)
        s.insert_poll_log(
            [
                {
                    "run_id": "r",
                    "recv_ts_ns": t0 + dt,
                    "n_tickers": 2,
                    "n_changed": 0,
                    "n_missing": 0,
                    "latency_ms": 1.0,
                }
                for dt in (10, 25, 30, 45)
            ]
        )
    out = scan_books(None, str(db), "DECLARED", tmp_path / "out")
    assert out["relations"] == 1 and out["cycles"] == 4
    # cycles at +25 and +30 see A's jumped book forward-filled; +10 and +45 are coherent
    assert out["cycles_with_any_violation"] == 2
    [seen] = [v for v in out["violations_seen"][:1]]
    assert seen["kind"] == "mutually_exclusive" and seen["margin_ticks"] == 6500 + 4500 - 10000
