"""Collector command line.

    python -m data.collectors.run history --config configs/history.yaml
    python -m data.collectors.run books   --config configs/books.yaml [--duration 600]
    python -m data.collectors.run events  --config configs/events.yaml   # complete settled events
    python -m data.collectors.run refresh --config configs/refresh.yaml  # settlements + trades for recorded books
    python -m data.collectors.run status  [--db var/kalshi.duckdb]
    python -m data.collectors.run verify  [--db var/kalshi.duckdb]   # trade completeness check
    python -m data.collectors.run export  --db var/kalshi.duckdb --out var/export

Every job is safe to interrupt (Ctrl-C) and re-run: see ``history.py`` for the resume model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from data.collectors.books import BookPoller, record_ws, select_book_tickers
from data.collectors.config import (
    BooksConfig,
    ConfigError,
    EventsConfig,
    HistoryConfig,
    RefreshConfig,
    load_config,
)
from data.collectors.events import EventCollector
from data.collectors.history import HistoryCollector
from data.collectors.refresh import RefreshCollector
from data.schemas.ddl import TABLES
from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from kalshi_client.config import KalshiConfig
from kalshi_client.rest import KalshiRestClient
from kalshi_client.websocket import KalshiWebSocket

log = logging.getLogger("collector")


async def cmd_history(cfg: HistoryConfig) -> int:
    with Store(cfg.db_path) as store:
        async with KalshiRestClient(
            KalshiConfig.from_env(),
            requests_per_second=cfg.requests_per_second,
            max_retries=cfg.max_retries,
            backoff_max=cfg.backoff_max_s,
        ) as rest:
            summary = await HistoryCollector(store, rest, cfg, git_commit=git_commit()).run()
        print(json.dumps(store.fingerprint(), indent=2, default=str))
    print(
        f"\nrun {summary.run_id}: {summary.trades_inserted} new trades, "
        f"{summary.partitions_synced} synced / {summary.partitions_skipped} skipped partitions, "
        f"{len(summary.failures)} failures, {summary.quarantined} quarantined"
    )
    for f in summary.failures[:10]:
        print("  FAILED:", f)
    return 1 if summary.failures else 0


async def cmd_books(cfg: BooksConfig) -> int:
    kcfg = KalshiConfig.from_env()
    with Store(cfg.db_path) as store:
        async with KalshiRestClient(kcfg, requests_per_second=cfg.requests_per_second) as rest:
            tickers = cfg.tickers or await select_book_tickers(rest, cfg.selection)
            if not tickers:
                print("no tickers selected", file=sys.stderr)
                return 2
            print(f"recording {len(tickers)} markets, mode={cfg.mode}")
            if cfg.mode == "ws":
                ws = KalshiWebSocket(
                    kcfg,
                    channels=["orderbook_delta", "trade", "market_lifecycle_v2"],
                    market_tickers=tickers,
                )
                print(json.dumps(await record_ws(store, ws, cfg, git_commit=git_commit())))
            else:
                poller = BookPoller(store, rest, cfg, tickers, git_commit=git_commit())
                s = await poller.run()
                print(s)
    return 0


async def cmd_events(cfg: EventsConfig) -> int:
    series = list(cfg.series)
    if not series and cfg.series_from_db:
        with Store(cfg.series_from_db, read_only=True) as src:
            series = [
                r[0]
                for r in src.query(
                    "SELECT DISTINCT series_ticker FROM events WHERE series_ticker IS NOT NULL"
                )
            ]
    if not series:
        print("no series given (set `series` or `series_from_db`)", file=sys.stderr)
        return 2
    with Store(cfg.db_path) as store:
        async with KalshiRestClient(
            KalshiConfig.from_env(),
            requests_per_second=cfg.requests_per_second,
            max_retries=cfg.max_retries,
            backoff_max=cfg.backoff_max_s,
        ) as rest:
            s = await EventCollector(store, rest, cfg, series, git_commit=git_commit()).run()
    print(
        f"run {s.run_id}: {s.events} events / {s.markets} markets from {s.series_fetched} series "
        f"({s.series_skipped} already complete), {len(s.failures)} failures, {s.quarantined} quarantined"
    )
    return 1 if s.failures else 0


async def cmd_refresh(cfg: RefreshConfig) -> int:
    with Store(cfg.db_path) as store:
        async with KalshiRestClient(
            KalshiConfig.from_env(),
            requests_per_second=cfg.requests_per_second,
            max_retries=cfg.max_retries,
            backoff_max=cfg.backoff_max_s,
        ) as rest:
            s = await RefreshCollector(store, rest, cfg, git_commit=git_commit()).run()
    print(
        f"run {s.run_id}: {s.markets} markets refreshed, {s.settled_now} settled "
        f"({s.newly_settled} newly), {s.trades_inserted} new trades, {len(s.failures)} failures"
    )
    return 1 if s.failures else 0


def cmd_status(db: str) -> int:
    try:
        store = Store(db, read_only=True)
    except Exception as exc:  # a running collector holds DuckDB's single-writer lock
        print(
            f"cannot open {db}: {exc}\n(if a collector is running, copy the file or stop it first)"
        )
        return 2
    with store:
        q = store.query
        print(f"== {db} ==")
        for name, n in store.counts().items():
            print(f"  {name:<18}{n:>12,}")
        print(
            "\n-- markets by result:",
            dict(q("SELECT result, count(*) FROM markets GROUP BY 1 ORDER BY 2 DESC")),
        )
        print(
            "-- markets by category:",
            q(
                "SELECT coalesce(category,'?'), count(*) FROM markets_enriched GROUP BY 1 ORDER BY 2 DESC LIMIT 8"
            ),
        )
        lo, hi = q("SELECT min(close_time), max(close_time) FROM markets")[0]
        print(f"-- close_time range: {lo} .. {hi}")
        print("-- recent runs:")
        for r in q(
            "SELECT run_id, status, started_at, ended_at FROM collection_runs ORDER BY started_at DESC LIMIT 5"
        ):
            print("   ", r)
        print("-- fingerprint:", store.fingerprint()["fingerprint"])
    return 0


def cmd_verify(db: str) -> int:
    try:
        store = Store(db, read_only=True)
    except Exception as exc:
        print(f"cannot open {db}: {exc}")
        return 2
    with store:
        r = store.reconcile_volume()
    print(
        f"volume reconciliation: {r['checked']} finalized+synced markets checked, "
        f"{len(r['mismatches'])} mismatches"
    )
    for m in r["mismatches"][:20]:
        print(
            f"  {m['ticker']}: exchange={m['exchange_volume'] / 100:.2f} stored={m['stored_volume'] / 100:.2f}"
        )
    return 1 if r["mismatches"] else 0


def cmd_export(db: str, out: str, fmt: str) -> int:
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with Store(db, read_only=True) as store:
        for name in TABLES:
            path = out_dir / f"{name}.{fmt}"
            opts = "(FORMAT PARQUET)" if fmt == "parquet" else "(FORMAT CSV, HEADER)"
            store.con.execute(f"COPY (SELECT * FROM {name}) TO '{path}' {opts}")
            print("wrote", path)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("history", "books", "events", "refresh"):
        p = sub.add_parser(name)
        p.add_argument("--config", required=True)
        p.add_argument("--db", help="override db_path from the config")
        if name == "books":
            p.add_argument("--duration", type=float, help="override duration_s")
    sub.add_parser("status").add_argument("--db", default="var/kalshi.duckdb")
    sub.add_parser("verify").add_argument("--db", default="var/kalshi.duckdb")
    e = sub.add_parser("export")
    e.add_argument("--db", default="var/kalshi.duckdb")
    e.add_argument("--out", default="var/export")
    e.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    if args.cmd == "status":
        return cmd_status(args.db)
    if args.cmd == "verify":
        return cmd_verify(args.db)
    if args.cmd == "export":
        return cmd_export(args.db, args.out, args.format)
    try:
        cfg = load_config(args.config)
    except (ConfigError, OSError) as exc:
        sys.exit(f"config error: {exc}")
    if args.db:
        cfg.db_path = args.db
    try:
        if args.cmd == "history":
            if not isinstance(cfg, HistoryConfig):
                sys.exit(f"{args.config} is not a history config")
            return asyncio.run(cmd_history(cfg))
        if args.cmd == "refresh":
            if not isinstance(cfg, RefreshConfig):
                sys.exit(f"{args.config} is not a refresh config")
            return asyncio.run(cmd_refresh(cfg))
        if args.cmd == "events":
            if not isinstance(cfg, EventsConfig):
                sys.exit(f"{args.config} is not an events config")
            return asyncio.run(cmd_events(cfg))
        if not isinstance(cfg, BooksConfig):
            sys.exit(f"{args.config} is not a books config")
        if args.duration is not None:
            cfg.duration_s = args.duration
        return asyncio.run(cmd_books(cfg))
    except KeyboardInterrupt:
        print(
            "\ninterrupted - progress is saved; re-run the same command to resume", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())
