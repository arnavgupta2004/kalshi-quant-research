"""Stage 5 on real data: audit the engine, then sweep fill-model assumptions.

    python -m scripts.stage5_backtest_demo --db var/books_shortlived.duckdb

Dataset: books + trades + settlements for the SAME markets (hourly/short-horizon sports markets
recorded by the REST poller, then completed by ``data.collectors.run refresh``).

  1. AUDIT   the engine's look-ahead guarantees, checked on the real feed:
             - truncation: cut the feed at random times; every decision <= the cut must be unchanged
             - hidden-field invariance: MarketInfo carries no close_time / volume / result
             - data quality: recording gaps, how stale a book can get, settlement coverage
  2. SWEEP   a naive passive quoter under five maker-fill models x two latencies.  Queue position is
             NOT in historical data, so a passive strategy's P&L is a RANGE; where it straddles zero
             the edge is not established.  (A test instrument - no profit claim.)
  3. TAKER   Stage 4's arbitrage detector through the engine, with and without fees.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import fields
from fractions import Fraction
from pathlib import Path

from arbitrage.detector import DetectorParams, build_specs
from backtest.engine import BacktestConfig, run_backtest
from backtest.feed import ListFeed, StoreFeed
from backtest.fills import QueueModel, TakerModel
from backtest.market_info import FORBIDDEN_FIELDS, MarketInfo
from backtest.metrics import markouts, mid_series, summarize
from backtest.strategies import ArbitrageTaker, PassiveQuoter
from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from market.fees import FeeBook
from market.semantics import EvidenceLevel
from market.timeutil import from_epoch_ns

SEC = 1_000_000_000
MODELS = [
    ("pessimistic", QueueModel.pessimistic()),
    ("queue(a=1,c=0)", QueueModel.queue(1.0, 0.0)),
    ("queue(a=1,c=.5)", QueueModel.queue(1.0, 0.5)),
    ("queue(a=.5,c=.5)", QueueModel.queue(0.5, 0.5)),
    ("optimistic", QueueModel.optimistic()),
]


def universe(store: Store, min_snapshots: int, min_trades: int) -> list[str]:
    lo, hi = store.query("SELECT min(recv_ts_ns), max(recv_ts_ns) FROM book_snapshots")[0]
    t_lo, t_hi = (from_epoch_ns(x).strftime("%Y-%m-%d %H:%M:%S") for x in (lo, hi))
    rows = store.query(
        "WITH w AS (SELECT ticker, count(*) n FROM book_snapshots GROUP BY 1), "
        f"tr AS (SELECT ticker, count(*) k FROM trades WHERE created_time BETWEEN TIMESTAMP '{t_lo}' "
        f"AND TIMESTAMP '{t_hi}' GROUP BY 1) "
        "SELECT m.ticker FROM markets m JOIN w USING (ticker) JOIN tr USING (ticker) "
        f"WHERE m.settlement_value IS NOT NULL AND m.settlement_ts >= TIMESTAMP '{t_lo}' "
        f"AND w.n >= {min_snapshots} AND tr.k >= {min_trades} ORDER BY m.ticker"
    )
    return [r[0] for r in rows]


def cfg(fees, maker, latency_ms=200, stale_s=30.0, extra=None) -> BacktestConfig:
    ns = latency_ms * 1_000_000
    return BacktestConfig(
        initial_cash_micro=10_000 * 1_000_000,
        latency_submit_ns=ns,
        latency_ack_ns=ns,
        latency_cancel_ns=ns,
        taker=TakerModel(max_staleness_ns=int(stale_s * SEC)),
        maker=maker,
        fees=fees,
        equity_sample_ns=30 * SEC,
        metadata=extra or {},
    )


def order_sig(res, upto):
    return [
        (o.submitted_ts, o.ticker, o.side.value, o.price, o.qty, o.tif.value)
        for o in res.orders
        if o.submitted_ts <= upto
    ]


def audit(feed: StoreFeed, fees, n_cuts: int, seed: int, book_window: tuple[int, int]) -> dict:
    events = list(feed)
    tickers = feed.tickers
    infos = feed.infos
    rng = random.Random(seed)
    t0, t1 = book_window  # cut inside the window where the strategy actually acts
    quoter = lambda: PassiveQuoter(tickers, qty=1000)  # noqa: E731
    base = cfg(fees, QueueModel.queue(1.0, 0.5))
    full = run_backtest(ListFeed(events), infos, quoter(), base)
    out = {
        "cuts": [],
        "passed": True,
        "full_orders": len(full.orders),
        "full_fills": len(full.fills),
    }
    for _ in range(n_cuts):
        cut = rng.randrange(t0 + (t1 - t0) // 10, t1)
        part = run_backtest(ListFeed(events).truncated(cut), infos, quoter(), base)
        same_orders = order_sig(full, cut) == order_sig(part, cut)
        same_fills = [
            (f.exec_ts, f.ticker, f.price, f.qty) for f in full.fills if f.exec_ts <= cut
        ] == [(f.exec_ts, f.ticker, f.price, f.qty) for f in part.fills if f.exec_ts <= cut]
        n_dec = len(order_sig(full, cut))
        out["cuts"].append(
            {
                "at": from_epoch_ns(cut).strftime("%H:%M:%S"),
                "orders_before_cut": n_dec,
                "fills_before_cut": sum(f.exec_ts <= cut for f in full.fills),
                "identical": same_orders and same_fills,
                "vacuous": n_dec == 0,
            }
        )
        out["passed"] &= (
            same_orders and same_fills and n_dec > 0
        )  # a check that exercised nothing is no pass
    names = {f.name for f in fields(MarketInfo)}
    out["market_info_fields"] = sorted(names)
    out["forbidden_fields_present"] = sorted(names & FORBIDDEN_FIELDS)
    out["passed"] &= not out["forbidden_fields_present"]
    return out


def data_quality(store: Store, feed: StoreFeed) -> dict:
    ts = [r[0] for r in store.query("SELECT recv_ts_ns FROM poll_log ORDER BY recv_ts_ns")]
    gaps = [(b - a) / SEC for a, b in zip(ts, ts[1:], strict=False)]
    return {
        "poll_cycles": len(ts),
        "median_gap_s": sorted(gaps)[len(gaps) // 2],
        "max_gap_s": max(gaps),
        "gaps_over_30s": sum(g > 30 for g in gaps),
        "seconds_in_gaps_over_30s": round(sum(g for g in gaps if g > 30)),
        "universe_markets": len(feed.tickers),
        "events_replayed": len(feed),
        "settled_in_universe": sum(m.settlement_value is not None for m in feed.markets),
    }


def summarise_run(res, mids=None) -> dict:
    s = summarize(res, grid_step_ns=60 * SEC)
    row = {
        "net_usd": res.pnl_micro / 1e6,
        "fees_usd": res.portfolio.fees / 1e6,
        "orders": s["execution"]["orders"],
        "fills": len(res.fills),
        "maker_fills": s["execution"]["maker_fills"],
        "taker_fills": s["execution"]["taker_fills"],
        "contracts_traded": s["execution"]["turnover_contracts"],
        "order_fill_rate": s["execution"]["order_fill_rate"],
        "max_drawdown_usd": s["risk"]["max_drawdown_micro"] / 1e6,
        "worst_settlement_usd": None
        if s["risk"]["worst_settlement_micro"] is None
        else s["risk"]["worst_settlement_micro"] / 1e6,
        "settled_markets": len(res.settlements),
    }
    if mids is not None and res.fills:
        m = markouts(res, mids, [30, 300])
        row["markout_ticks"] = {
            str(h): (None if v["mean_ticks"] is None else round(v["mean_ticks"], 1))
            for h, v in m.items()
        }
    return row


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--db", default="var/books_shortlived.duckdb")
    ap.add_argument("--out", default="results/stage5")
    ap.add_argument("--min-snapshots", type=int, default=200)
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--cuts", type=int, default=4)
    ap.add_argument("--seed", type=int, default=20260919)
    a = ap.parse_args()

    with Store(a.db, read_only=True) as store:
        tickers = universe(store, a.min_snapshots, a.min_trades)
        fees = FeeBook.from_series_meta(store.read_series_fees())
        lo, hi = store.query("SELECT min(recv_ts_ns), max(recv_ts_ns) FROM book_snapshots")[0]
        feed = StoreFeed(
            store, tickers, start_ns=lo
        )  # the strategy's day starts with the first book
        events = list(feed)
        mids = mid_series(events)
        bundles = [
            b for b in store.read_events_with_markets() if set(b[0].market_tickers) & set(tickers)
        ]
        print(
            f"universe: {len(tickers)} settled markets with >= {a.min_snapshots} snapshots and >= {a.min_trades} in-window trades"
        )
        report: dict = {
            "git_commit": git_commit(),
            "db": a.db,
            "fingerprint": store.fingerprint()["fingerprint"],
            "universe": {
                "tickers": len(tickers),
                "selection": "point-in-time: scheduled expiry <= 3h at recording start; "
                "then filtered on realised book/trade coverage (post-hoc, disclosed)",
            },
        }
        report["data_quality"] = data_quality(store, feed)
        print("\n== 1. AUDIT ==")
        print("data quality:", json.dumps(report["data_quality"]))
        report["audit"] = audit(feed, fees, a.cuts, a.seed, (lo, hi))
        for c in report["audit"]["cuts"]:
            verdict = (
                "VACUOUS (no decisions)"
                if c["vacuous"]
                else ("IDENTICAL" if c["identical"] else "DIFFERENT (LEAK!)")
            )
            print(
                f"  truncate at {c['at']}: {c['orders_before_cut']:>5} orders / {c['fills_before_cut']:>4} fills before the cut -> {verdict}"
            )
        print("  MarketInfo fields:", ", ".join(report["audit"]["market_info_fields"]))
        print(
            "  forbidden fields exposed:",
            report["audit"]["forbidden_fields_present"] or "none",
            "->",
            "PASS" if report["audit"]["passed"] else "FAIL",
        )

        print(
            "\n== 2. PASSIVE QUOTER: maker-fill sensitivity (a test instrument, not a strategy) =="
        )
        print(
            f"{'model':<18}{'latency':>8}{'net $':>10}{'fills':>7}{'contracts':>10}{'markout30s':>11}{'markout300s':>12}"
        )
        report["passive_sweep"] = []
        for lat in (200, 1000):
            for name, model in MODELS:
                res = run_backtest(
                    ListFeed(events),
                    feed.infos,
                    PassiveQuoter(tickers, qty=1000),
                    cfg(fees, model, latency_ms=lat),
                )
                row = {"model": name, "latency_ms": lat, **summarise_run(res, mids)}
                report["passive_sweep"].append(row)
                mo = row.get("markout_ticks", {})
                print(
                    f"{name:<18}{lat:>6}ms{row['net_usd']:>10.2f}{row['fills']:>7}{row['contracts_traded']:>10.0f}"
                    f"{str(mo.get('30')):>11}{str(mo.get('300')):>12}"
                )
        by_lat = {
            lat: [r["net_usd"] for r in report["passive_sweep"] if r["latency_ms"] == lat]
            for lat in (200, 1000)
        }
        report["passive_range_usd"] = {str(lat): [min(v), max(v)] for lat, v in by_lat.items()}
        print(
            "net P&L range across models:",
            {k: [round(x, 2) for x in v] for k, v in report["passive_range_usd"].items()},
        )

        print("\n== 3. ARBITRAGE TAKER through the engine (Stage 4 detector; 200 ms latency) ==")
        specs = build_specs(bundles)
        print(f"relations: {len(specs)} from {len(bundles)} events")
        report["arbitrage"] = []
        for label, fb, lvl in (
            ("baseline fees", fees, EvidenceLevel.DECLARED),
            ("fee-free (engine exercise)", fees.scaled(Fraction(0)), EvidenceLevel.DECLARED),
            ("fee-free, any evidence", fees.scaled(Fraction(0)), EvidenceLevel.UNVERIFIED),
        ):
            strat = ArbitrageTaker(
                specs, DetectorParams(fees=fb, target=1000, min_level=lvl), max_book_age_ns=20 * SEC
            )
            res = run_backtest(
                ListFeed(events), feed.infos, strat, cfg(fb, QueueModel.queue(), latency_ms=200)
            )
            row = {
                "variant": label,
                "opportunities_seen": strat.opportunities_seen,
                "orders": strat.orders_sent,
                **summarise_run(res),
            }
            report["arbitrage"].append(row)
            print(
                f"  {label:<28} opportunities={strat.opportunities_seen:>4} orders={strat.orders_sent:>4} fills={row['fills']:>4} net=${row['net_usd']:.2f}"
            )

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "backtest_demo.json").write_text(json.dumps(report, indent=2, default=str))
    print(f"\nwrote {out / 'backtest_demo.json'}")


if __name__ == "__main__":
    main()
