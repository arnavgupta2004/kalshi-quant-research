"""Stage 3 real-data study: do the inferred relations survive contact with settled outcomes?

    python -m scripts.stage3_relationship_study study --db var/relations.duckdb
    python -m scripts.stage3_relationship_study scan  --events-db var/relations.duckdb --books-db var/books_stage3.duckdb

``study``  replays every relation the semantics module infers against the *realised settlement
           values* of thousands of real settled events.  A PROVEN relation that a real settlement
           violates would mean a semantic bug or a resolution-risk mechanism we do not understand;
           DECLARED / EMPIRICAL relations get their violation rates measured.  Empirical evidence
           is leave-one-out: an event is never judged by its own outcome.
``scan``   prices the constraints of currently-open events against recorded order books
           (top of book, fee-free, non-atomic REST snapshots - a first look, NOT an arbitrage claim).
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from market.contracts import SettlementResult
from market.evidence import StatsBook
from market.order_book import OrderBook
from market.relationships import RelationKind, asks_from_books, scan
from market.semantics import Analysis, EvidenceLevel, analyze_event, find_unions


def settled_only(bundles):
    return [(e, ms) for e, ms in bundles if ms and all(m.settlement_value is not None for m in ms)]


def violated(relation, settlement: dict[str, int]) -> bool:
    """Does the realised settlement break any constraint of the relation? (works for fractional values)"""
    return any(c.payoff(settlement) < c.bound for c in relation.constraints())


def study(db: str, out: Path, sample_conflicts: int = 12) -> dict:
    with Store(db, read_only=True) as store:
        bundles = settled_only(store.read_events_with_markets())
    stats = StatsBook.from_events(
        (e.series_ticker or e.event_ticker.split("-")[0], ms) for e, ms in bundles
    )

    rows: list[dict] = []
    conflicts: list[str] = []
    settlement_of: dict[str, int] = {}
    for _, ms in bundles:
        settlement_of |= {m.ticker: m.settlement_value for m in ms}
    coverage: dict[str, list[bool]] = defaultdict(list)
    examples: dict[tuple, list[str]] = defaultdict(list)

    def record(ev, a, extra=None):
        ts = a.relation.tickers()
        sett = {t: settlement_of[t] for t in ts}
        has_scalar = any(0 < sett[t] < 10_000 for t in ts)
        v = violated(a.relation, sett)
        key = (a.relation.kind.value, a.level.name)
        row = {
            "event": ev,
            "series": ev.split("-")[0],
            "kind": key[0],
            "level": key[1],
            "n_markets": len(ts),
            "scalar_involved": has_scalar,
            "violated": v,
        }
        rows.append(row | (extra or {}))
        if v and len(examples[(*key, has_scalar)]) < 4:
            examples[(*key, has_scalar)].append(f"{ev}: {a.relation.statement()[:110]}")

    for ev, ms in bundles:
        series = ev.series_ticker or ev.event_ticker.split("-")[0]
        analysis: Analysis = analyze_event(ev, ms, stats=stats.without(series, ms))
        conflicts += analysis.conflicts
        strong = False
        for a in analysis.assessments:
            if a.relation.kind is RelationKind.COMPLEMENT:
                continue
            record(ev.event_ticker, a, {"category": ev.category or "?"})
            strong |= a.level >= EvidenceLevel.LATTICE
        coverage[ev.category or "?"].append(strong)

    union_analysis = find_unions(bundles)
    for a in union_analysis.assessments:
        record("cross-event", a, {"category": "Crypto"})

    def table(pred=lambda r: True):
        c = Counter((r["kind"], r["level"], r["scalar_involved"]) for r in rows if pred(r))
        v = Counter(
            (r["kind"], r["level"], r["scalar_involved"]) for r in rows if pred(r) and r["violated"]
        )
        return [
            {
                "kind": k,
                "level": lv,
                "scalar_involved": sc,
                "instances": n,
                "violated": v[(k, lv, sc)],
            }
            for (k, lv, sc), n in sorted(c.items())
        ]

    flagged = [(e, ms) for e, ms in bundles if e.mutually_exclusive]

    def yes_count(ms):
        return sum(m.result is SettlementResult.YES for m in ms)

    flag_audit = {
        "flagged_events": len(flagged),
        "with_scalar": sum(
            any(m.result is SettlementResult.SCALAR for m in ms) for _, ms in flagged
        ),
        "multi_yes": sum(
            yes_count(ms) >= 2
            for _, ms in flagged
            if all(m.result is not SettlementResult.SCALAR for m in ms)
        ),
        "zero_yes": sum(
            yes_count(ms) == 0
            for _, ms in flagged
            if all(m.result is not SettlementResult.SCALAR for m in ms)
        ),
        "exactly_one": sum(
            yes_count(ms) == 1
            for _, ms in flagged
            if all(m.result is not SettlementResult.SCALAR for m in ms)
        ),
    }
    series_rows = [
        {
            "series": s.series,
            "n_events": s.n_events,
            "n_clean": s.n_clean,
            "zero_yes": s.zero_yes,
            "one_yes": s.one_yes,
            "multi_yes": s.multi_yes,
            "n_scalar": s.n_scalar,
            "multi_yes_upper95": round(s.multi_yes_upper(), 4),
            "zero_yes_upper95": round(s.zero_yes_upper(), 4),
        }
        for s in stats
        if s.n_events >= 5
    ]
    summary = {
        "git_commit": git_commit(),
        "source_db": db,
        "dataset_fingerprint": None,
        "settled_events": len(bundles),
        "settled_markets": sum(len(ms) for _, ms in bundles),
        "relations_by_kind_level": table(),
        "flag_audit": flag_audit,
        "structure_coverage_by_category": {
            c: {"events": len(v), "with_relation_ge_LATTICE": sum(v)}
            for c, v in sorted(coverage.items())
        },
        "conflicts": {"count": len(conflicts), "examples": conflicts[:sample_conflicts]},
        "violation_examples": {"|".join(map(str, k)): v for k, v in examples.items()},
    }
    with Store(db, read_only=True) as store:
        summary["dataset_fingerprint"] = store.fingerprint()["fingerprint"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "relations_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    for name, data in (("relation_instances", rows), ("series_outcome_stats", series_rows)):
        if data:
            with (out / f"{name}.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(data[0]))
                w.writeheader()
                w.writerows(data)
    return summary


def print_study(s: dict) -> None:
    print(
        f"settled events: {s['settled_events']:,} | markets: {s['settled_markets']:,} | fingerprint {s['dataset_fingerprint']}"
    )
    print(
        "\nrelation instances by kind x evidence level  (violated = realised settlement broke a constraint)"
    )
    print(f"  {'kind':<20}{'level':<11}{'scalar?':<9}{'instances':>10}{'violated':>10}{'rate':>9}")
    for r in s["relations_by_kind_level"]:
        rate = f"{r['violated'] / r['instances']:.2%}" if r["instances"] else "-"
        print(
            f"  {r['kind']:<20}{r['level']:<11}{str(r['scalar_involved']):<9}{r['instances']:>10,}{r['violated']:>10,}{rate:>9}"
        )
    print(
        "\nexchange 'mutually_exclusive' flag audit (events without scalar settlement):",
        s["flag_audit"],
    )
    print(f"\nconflicts (data contradicts itself): {s['conflicts']['count']}")
    for c in s["conflicts"]["examples"][:6]:
        print("  ", c[:200])
    if s["violation_examples"]:
        print("\nviolation examples:")
        for k, v in s["violation_examples"].items():
            print(" ", k, *v[:2], sep="\n     ")


def scan_books(events_db: str | None, books_db: str, min_level: str, out: Path) -> dict:
    level = EvidenceLevel[min_level]
    with Store(books_db, read_only=True) as b:
        bundles = b.read_events_with_markets()
        snaps = b.query(
            "SELECT ticker, recv_ts_ns, yes_px, yes_qty, no_px, no_qty FROM book_snapshots ORDER BY recv_ts_ns"
        )
        cycles = [r[0] for r in b.query("SELECT recv_ts_ns FROM poll_log ORDER BY recv_ts_ns")]
    stats = None
    if events_db:
        with Store(events_db, read_only=True) as r:
            stats = StatsBook.from_events(
                (e.series_ticker or e.event_ticker.split("-")[0], ms)
                for e, ms in settled_only(r.read_events_with_markets())
            )
    relations, described = [], []
    for ev, ms in bundles:
        a = analyze_event(ev, ms, stats=stats)
        for x in a.assessments:
            if x.relation.kind is not RelationKind.COMPLEMENT and x.level >= level:
                relations.append(x.relation)
                described.append(
                    (
                        ev.event_ticker,
                        x.relation.kind.value,
                        x.level.name,
                        len(x.relation.tickers()),
                    )
                )
    by_ticker: dict[str, tuple[list[int], list[tuple]]] = defaultdict(lambda: ([], []))
    for t, ts, ypx, yq, npx, nq in snaps:
        by_ticker[t][0].append(ts)
        by_ticker[t][1].append((list(zip(ypx, yq, strict=True)), list(zip(npx, nq, strict=True))))
    cycle_stats, best_rows = [], []
    for t_cycle in cycles:
        books = {}
        for t, (times, states) in by_ticker.items():
            i = bisect.bisect_right(times, t_cycle) - 1
            if i >= 0:
                bk = OrderBook(t)
                bk.apply_snapshot(*states[i])
                books[t] = bk
        checks = scan(relations, asks_from_books(books))
        cycle_stats.append(
            (len(checks), sum(c.violated for c in checks), checks[0].margin if checks else None)
        )
        best_rows += [c for c in checks[:3] if c.violated]
    n_cycles = len(cycle_stats)
    summary = {
        "books_db": books_db,
        "min_level": min_level,
        "cycles": n_cycles,
        "relations": len(relations),
        "relation_kinds": dict(Counter(k for _, k, _, _ in described)),
        "events_with_relations": len({e for e, *_ in described}),
        "constraints_priced_per_cycle_median": sorted(c[0] for c in cycle_stats)[n_cycles // 2]
        if cycle_stats
        else 0,
        "cycles_with_any_violation": sum(1 for c in cycle_stats if c[1] > 0),
        "max_margin_ticks_overall": max(
            (c[2] for c in cycle_stats if c[2] is not None), default=None
        ),
        "median_best_margin_ticks": sorted(c[2] for c in cycle_stats if c[2] is not None)[
            len([c for c in cycle_stats if c[2] is not None]) // 2
        ]
        if cycle_stats
        else None,
        "violations_seen": [
            {
                "constraint": c.constraint.name,
                "kind": c.constraint.kind.value,
                "margin_ticks": c.margin,
                "top_qty_centi": c.top_qty,
            }
            for c in sorted(best_rows, key=lambda c: -c.margin)[:8]
        ],
    }
    out.mkdir(parents=True, exist_ok=True)
    (out / "book_scan.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("study")
    s.add_argument("--db", default="var/relations.duckdb")
    s.add_argument("--out", default="results/stage3")
    c = sub.add_parser("scan")
    c.add_argument("--events-db", default="var/relations.duckdb")
    c.add_argument("--books-db", required=True)
    c.add_argument("--min-level", default="DECLARED", choices=[lv.name for lv in EvidenceLevel])
    c.add_argument("--out", default="results/stage3")
    a = ap.parse_args()
    if a.cmd == "study":
        print_study(study(a.db, Path(a.out)))
    else:
        print(json.dumps(scan_books(a.events_db, a.books_db, a.min_level, Path(a.out)), indent=2))


if __name__ == "__main__":
    main()
