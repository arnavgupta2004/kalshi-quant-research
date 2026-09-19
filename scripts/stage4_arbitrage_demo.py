"""Stage 4: from an observed book to a net edge - a worked example and a replay on real books.

    python -m scripts.stage4_arbitrage_demo worked
    python -m scripts.stage4_arbitrage_demo replay --books-db var/books_structural.duckdb \
        --events-db var/relations.duckdb

``worked``  hand-built books on a REAL event's structure (NYC temperature buckets: a proven partition),
            showing every step: constraint -> displayed margin -> book walk -> fees -> size -> net.
``replay``  replays recorded books cycle by cycle through the detector under several cost models
            (baseline fees / fee-free / cent-rounded fees / fees x2) and evidence thresholds, and reports
            the funnel: displayed -> liquid -> after fees -> after slippage -> executable.
            Books come from REST snapshots: top-of-book fee-inclusive, but NOT latency-adjusted and not
            atomic across markets (see docs/arbitrage.md) - a static-structure measurement, not a P&L.
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from arbitrage.detector import DetectorParams, build_specs, detect
from arbitrage.execution import price_bundle
from arbitrage.opportunity import Funnel, RelationSpec
from data.normalization.fixtures import load_event_fixture as load
from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from market.contracts import Side
from market.evidence import StatsBook
from market.fees import FeeBook, FeeSchedule, Rounding
from market.order_book import OrderBook
from market.relationships import Partition, admissible_worlds
from market.semantics import EvidenceLevel
from market.timeutil import from_epoch_ns
from scripts.stage3_relationship_study import settled_only


def dollars(micro: int | None) -> str:
    return "-" if micro is None else f"${micro / 1e6:,.4f}"


def worked() -> None:
    event, markets = load("nyc_high")
    order = sorted(
        markets, key=lambda m: (m.strike.floor if m.strike.floor is not None else -1e9, m.ticker)
    )
    tickers = [m.ticker for m in order]
    print(f"EVENT {event.event_ticker}: {event.title}\n")
    print("1) THE CONSTRAINT - proven from the rules, not assumed")
    rel = Partition(tickers)
    print(
        f"   {len(tickers)} temperature buckets partition the outcomes (evidence: LATTICE - whole degrees):"
    )
    print(
        "   exactly one bucket resolves YES  =>  buying one YES in every bucket pays exactly $1.00\n"
    )
    asks = [1000, 2000, 2500, 2000, 1200, 700]  # YES asks (ticks): sum 0.94 < 1
    depth = [500, 800, 300, 120, 400, 90]  # contracts on offer at that ask
    books = {}
    for t, a, d in zip(tickers, asks, depth, strict=True):
        b = OrderBook(t)
        b.apply_snapshot(
            [(max(1, a - 500), 100 * 50)], [(10_000 - a, 100 * d), (10_000 - a - 100, 100 * 1000)]
        )
        books[t] = b
    print("2) THE OBSERVED BOOKS (YES asks are derived: ask = 1 - NO bid)")
    for m in order:
        lv = books[m.ticker].asks(Side.YES)
        print(
            f"   {m.yes_sub_title:<14} best ask {lv[0].price / 1e4:.4f} x {lv[0].qty / 100:>5.0f}   next {lv[1].price / 1e4:.4f} x {lv[1].qty / 100:>5.0f}"
        )
    total = sum(books[t].best_ask(Side.YES).price for t in tickers)
    print(
        f"   sum of best YES asks = {total / 1e4:.4f}  ->  DISPLAYED margin {(10_000 - total) / 1e4:.4f} per $1 bundle\n"
    )

    fees = FeeBook({}, FeeSchedule(known=False))
    con = next(c for c in rel.constraints() if c.name.startswith("exhaustive"))
    print("3) WALK THE BOOK, ADD FEES, FIND THE SIZE   (bundle = one YES in each of the 6 buckets)")
    print(
        f"   {'contracts':>9} {'payoff':>10} {'vwap cost':>10} {'slippage':>9} {'fees':>9} {'NET':>10} {'net/contract':>13}"
    )
    best = None
    for n in (1, 10, 50, 90, 91, 100, 120, 200, 300, 400):
        pb = price_bundle(con, books, n * 100, fees)
        if pb is None:
            print(f"   {n:>9}   (a leg runs out of depth)")
            continue
        print(
            f"   {n:>9} {dollars(pb.payoff_micro):>10} {dollars(pb.cost_micro):>10} {dollars(pb.slippage_micro):>9} "
            f"{dollars(pb.fee_micro):>9} {dollars(pb.net_micro):>10} {pb.net_micro / 1e6 / n:>12.4f}"
        )
        best = pb if best is None or pb.net_micro > best.net_micro else best
    spec = RelationSpec(rel, EvidenceLevel.LATTICE, event.event_ticker, "KXHIGHNY")
    ops, funnel = detect([spec], books, DetectorParams(fees=fees, target=10_000), ts_ns=0)
    op = ops[0]
    print(
        f"\n4) VERDICT: {op.classification.value.upper()}  |  recommended size {op.priced.bundles / 100:.0f} contracts"
    )
    print(
        f"   gross {dollars(op.gross_micro)} - slippage {dollars(op.slippage_micro)} - fees {dollars(op.fee_micro)} = NET {dollars(op.net_micro)}"
    )
    print(
        f"   ROI {op.roi:.2%} on {dollars(op.priced.cost_micro + op.priced.fee_micro)} committed (fee schedule assumed: standard taker)"
    )
    print("   funnel:", ", ".join(f"{k}={v}" for k, v in funnel.rows()))
    print("\n5) THE GUARANTEE, checked in every admissible world (exactly one bucket wins):")
    worlds = admissible_worlds(rel)
    pnl = sorted(op.settle_micro(w) for w in worlds)
    print(
        f"   realised P&L across {len(worlds)} worlds: min {dollars(pnl[0])}  max {dollars(pnl[-1])}   (computed net {dollars(op.net_micro)})"
    )
    print(
        "   -> identical in every world: the trade is riskless *given the relation holds* and *atomic execution*."
    )


def _hist(margins: list[int]) -> dict[str, int]:
    """Displayed-margin sizes: how big are the violations before any cost?  (1 tick = $0.0001)"""
    edges = [(0, 100, "<=1c"), (100, 200, "1-2c"), (200, 500, "2-5c"), (500, 10**9, ">5c")]
    return {label: sum(1 for m in margins if lo < m <= hi) for lo, hi, label in edges}


def load_books(db: str):
    with Store(db, read_only=True) as s:
        bundles = s.read_events_with_markets()
        snaps = s.query(
            "SELECT ticker, recv_ts_ns, yes_px, yes_qty, no_px, no_qty FROM book_snapshots ORDER BY recv_ts_ns"
        )
        cycles = [r[0] for r in s.query("SELECT recv_ts_ns FROM poll_log ORDER BY recv_ts_ns")]
        fees = s.read_series_fees()
    by_ticker: dict[str, tuple[list[int], list[tuple]]] = defaultdict(lambda: ([], []))
    for t, ts, ypx, yq, npx, nq in snaps:
        by_ticker[t][0].append(ts)
        by_ticker[t][1].append((list(zip(ypx, yq, strict=True)), list(zip(npx, nq, strict=True))))
    return bundles, by_ticker, cycles, fees


def replay(books_db: str, events_db: str | None, out: Path, target: int, min_level: str) -> dict:
    bundles, by_ticker, cycles, meta = load_books(books_db)
    stats = None
    if events_db:
        with Store(events_db, read_only=True) as r:
            stats = StatsBook.from_events(
                (e.series_ticker or e.event_ticker.split("-")[0], ms)
                for e, ms in settled_only(r.read_events_with_markets())
            )
    specs = build_specs(bundles, stats=stats)
    base_fees = FeeBook.from_series_meta(meta)
    variants = {
        "baseline": DetectorParams(
            fees=base_fees, target=target, min_level=EvidenceLevel[min_level]
        ),
        "fee_free": DetectorParams(
            fees=base_fees.scaled(Fraction(0)), target=target, min_level=EvidenceLevel[min_level]
        ),
        "cent_rounding": DetectorParams(
            fees=base_fees.with_rounding(Rounding.CENT_PER_FILL),
            target=target,
            min_level=EvidenceLevel[min_level],
        ),
        "fees_x2": DetectorParams(
            fees=base_fees.scaled(Fraction(2)), target=target, min_level=EvidenceLevel[min_level]
        ),
        "any_evidence": DetectorParams(
            fees=base_fees, target=target, min_level=EvidenceLevel.UNVERIFIED
        ),
    }
    funnels = {k: Funnel() for k in variants}
    best_ops: dict[str, list] = {k: [] for k in variants}
    episodes: dict[str, dict] = {
        k: {} for k in variants
    }  # key -> [start_ts, last_ts, n_cycles, peak_net]
    finished: dict[str, list] = {k: [] for k in variants}
    idx = {t: -1 for t in by_ticker}
    cache: dict[str, OrderBook] = {}
    for t_cycle in cycles:
        for t, (times, states) in by_ticker.items():
            i = bisect.bisect_right(times, t_cycle) - 1
            if i != idx[t]:
                idx[t] = i
                if i >= 0:
                    bk = OrderBook(t)
                    bk.apply_snapshot(*states[i])
                    cache[t] = bk
        for name, params in variants.items():
            ops, f = detect(specs, cache, params, ts_ns=t_cycle)
            funnels[name].add(f)
            seen = set()
            for op in ops:
                key = f"{op.spec.event_ticker}|{op.constraint.name}|" + ",".join(
                    sorted(str(leg.contract) for leg in op.constraint.legs)
                )
                seen.add(key)
                ep = episodes[name].setdefault(
                    key,
                    {
                        "start": t_cycle,
                        "cycles": 0,
                        "peak_net": None,
                        "class": op.classification.value,
                        "kind": op.spec.relation.kind.value,
                    },
                )
                ep["cycles"] += 1
                ep["last"] = t_cycle
                if op.net_micro is not None and (
                    ep["peak_net"] is None or op.net_micro > ep["peak_net"]
                ):
                    ep["peak_net"] = op.net_micro
                    ep["class"] = op.classification.value
                best_ops[name].append(op)
            for key in [k for k in episodes[name] if k not in seen]:
                finished[name].append(episodes[name].pop(key))
    for name in variants:
        finished[name] += list(episodes[name].values())
    span_s = (cycles[-1] - cycles[0]) / 1e9 if len(cycles) > 1 else 0
    summary = {
        "git_commit": git_commit(),
        "books_db": books_db,
        "cycles": len(cycles),
        "span_minutes": round(span_s / 60, 1),
        "tickers": len(by_ticker),
        "events": len(bundles),
        "relations": len(specs),
        "fee_metadata_series": len(meta),
        "target_contracts": target // 100,
        "variants": {},
    }
    for name, f in funnels.items():
        eps = finished[name]
        top = sorted(
            best_ops[name], key=lambda o: -(o.net_micro if o.net_micro is not None else -(10**18))
        )[:5]
        summary["variants"][name] = {
            "funnel": dict(f.rows()),
            "pruned_by_prefilter": f.pruned,
            "by_class": f.by_class,
            "fee_assumed_displayed": f.fee_assumed,
            "best_margin_ticks_by_kind": f.best_margin,
            "displayed_by_event": dict(
                Counter(o.spec.event_ticker for o in best_ops[name]).most_common(8)
            ),
            "top_margin_ticks_histogram": _hist([o.top_margin for o in best_ops[name]]),
            "episodes": len(eps),
            "episode_median_cycles": sorted(e["cycles"] for e in eps)[len(eps) // 2]
            if eps
            else None,
            "episode_classes": {
                c: sum(1 for e in eps if e["class"] == c) for c in {e["class"] for e in eps}
            },
            "top": [
                {
                    "event": o.spec.event_ticker,
                    "relation": o.spec.relation.kind.value,
                    "level": o.spec.level.name,
                    "class": o.classification.value,
                    "top_margin_ticks": o.top_margin,
                    "top_qty_contracts": o.top_qty / 100,
                    "size_contracts": None if o.priced is None else o.priced.bundles / 100,
                    "gross": None if o.gross_micro is None else o.gross_micro / 1e6,
                    "slippage": None if o.slippage_micro is None else o.slippage_micro / 1e6,
                    "fees": None if o.fee_micro is None else o.fee_micro / 1e6,
                    "net": None if o.net_micro is None else o.net_micro / 1e6,
                    "roi": o.roi,
                    "annualized_roi": o.annualized_roi,
                    "fees_known": o.fees_known,
                    "at": from_epoch_ns(o.ts_ns).isoformat(),
                }
                for o in top
            ],
        }
    out.mkdir(parents=True, exist_ok=True)
    (out / "replay.json").write_text(json.dumps(summary, indent=2, default=str))
    return summary


def print_replay(s: dict) -> None:
    print(
        f"replayed {s['cycles']} cycles over {s['span_minutes']} min | {s['tickers']} tickers | {s['events']} events | "
        f"{s['relations']} relations | fee schedules for {s['fee_metadata_series']} series | target {s['target_contracts']} contracts\n"
    )
    names = list(s["variants"])
    print(f"{'stage':<16}" + "".join(f"{n:>15}" for n in names))
    for stage in Funnel.STAGES:
        print(f"{stage:<16}" + "".join(f"{s['variants'][n]['funnel'][stage]:>15,}" for n in names))
    print(f"{'episodes':<16}" + "".join(f"{s['variants'][n]['episodes']:>15,}" for n in names))
    print(
        "\nclosest approach to a violation (max margin in ticks of $1 = 1e-4; negative = no violation), baseline fees:"
    )
    for k, m in sorted(s["variants"]["baseline"]["best_margin_ticks_by_kind"].items()):
        print(f"   {k:<20}{m:>8}")
    b = s["variants"]["baseline"]
    print(
        "\nclasses (baseline):",
        b["by_class"],
        "| displayed margins:",
        b["top_margin_ticks_histogram"],
    )
    print("displayed violations by event:", b["displayed_by_event"])
    for n in ("baseline", "fee_free", "any_evidence"):
        for t in s["variants"][n]["top"][:3]:
            print(f"  [{n}] {t}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worked")
    r = sub.add_parser("replay")
    r.add_argument("--books-db", required=True)
    r.add_argument("--events-db", default="var/relations.duckdb")
    r.add_argument("--out", default="results/stage4")
    r.add_argument("--target", type=int, default=10_000, help="target size in centi-contracts")
    r.add_argument("--min-level", default="DECLARED", choices=[lv.name for lv in EvidenceLevel])
    a = ap.parse_args()
    if a.cmd == "worked":
        worked()
    else:
        print_replay(replay(a.books_db, a.events_db, Path(a.out), a.target, a.min_level))


if __name__ == "__main__":
    main()
