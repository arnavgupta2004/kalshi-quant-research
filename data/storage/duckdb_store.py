"""DuckDB-backed local dataset.

Why DuckDB: columnar analytics over millions of trades/book updates, native Parquet export,
and array columns for book depth.  Caveat: a DuckDB file has a single writer process and
cannot be opened concurrently by a reader while a collector is running - query a copy
(``cp market.duckdb snap.duckdb``) or stop the collector first.

Idempotency contract (what makes resumption safe)
  * every fact table has a natural primary key; inserts are ``ON CONFLICT DO NOTHING`` so
    re-ingesting overlapping data can never duplicate a row;
  * mutable entities (markets, events) are upserted, preserving ``first_seen_at``;
  * ``trade_sync`` checkpoints advance only after a partition was paginated to completion.

Bulk ingestion goes through newline-delimited JSON temp files rather than Python parameters:
binding Python values one by one measured ~2k rows/s here (28 s for 50k rows) versus
~450k rows/s through ``read_json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from data.schemas.ddl import META_DDL, SCHEMA_VERSION, TABLES, VIEWS
from market.contracts import (
    Event,
    Market,
    MarketStatus,
    PriceRange,
    Rules,
    SettlementResult,
    SettlementSource,
    Strike,
    Trade,
)
from market.timeutil import utcnow

TMP_PREFIX = "kalshi_bulk_"
FINGERPRINT_EXCLUDED = frozenset({"collection_runs", "scan_state"})


def _naive_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("refusing to store a naive datetime (which zone?)")
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")


def _json_default(o: Any) -> Any:
    if isinstance(o, datetime):
        return _naive_utc(o)
    if isinstance(o, set | frozenset):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def as_utc(v: datetime | None) -> datetime | None:
    """DuckDB returns naive UTC ``TIMESTAMP`` values; attach the zone explicitly."""
    return None if v is None else v.replace(tzinfo=UTC)


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


class Store:
    def __init__(self, path: str | os.PathLike = ":memory:", *, read_only: bool = False) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.path, read_only=read_only)
        self._tmpdir = None if self.path == ":memory:" else str(Path(self.path).parent)
        self._last_status: dict[tuple[str, str], tuple] | None = None
        if not read_only:
            self._remove_orphan_temp_files()
            self._init_schema()

    def _remove_orphan_temp_files(self) -> None:
        """A hard kill during ingestion strands its NDJSON temp file.  We hold DuckDB's exclusive
        writer lock, so no live ingestion can own a file with our prefix."""
        if self._tmpdir is None:
            return
        for f in Path(self._tmpdir).glob(f"{TMP_PREFIX}*.ndjson"):
            f.unlink(missing_ok=True)

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        self.con.execute(META_DDL)
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] if row else None
        if current is not None and current > SCHEMA_VERSION:
            raise RuntimeError(f"database schema v{current} is newer than code v{SCHEMA_VERSION}")
        # Every schema change so far is additive (new tables, CREATE IF NOT EXISTS), so migrating an
        # older file is just creating what is missing and recording the new version.
        for t in TABLES.values():
            self.con.execute(t.ddl())
        for ddl in VIEWS.values():
            self.con.execute(ddl)
        if current is None or current < SCHEMA_VERSION:
            self.con.execute(
                "INSERT INTO schema_version VALUES (?, ?)",
                [SCHEMA_VERSION, _naive_utc(utcnow())],
            )

    # ------------------------------------------------------------------ bulk ingestion
    def _bulk(
        self,
        table: str,
        rows: Iterable[Mapping[str, Any]],
        *,
        upsert: bool = False,
        preserve: Sequence[str] = (),
    ) -> int:
        """Insert rows; returns the number of rows *newly inserted* (upsert: rows written)."""
        t = TABLES[table]
        keyed: dict[tuple, dict] = {}
        for r in rows:  # in-batch dedupe: first wins (ignore) / last wins (upsert)
            key = tuple(r.get(c) for c in t.pk)
            if upsert or key not in keyed:
                keyed[key] = {c: r.get(c) for c in t.col_names}
        if not keyed:
            return 0
        with tempfile.NamedTemporaryFile(
            "w",
            prefix=TMP_PREFIX,
            suffix=".ndjson",
            delete=False,
            dir=self._tmpdir,
            encoding="utf-8",
        ) as f:
            for r in keyed.values():
                f.write(json.dumps(r, separators=(",", ":"), default=_json_default) + "\n")
            path = f.name
        try:
            cols = ", ".join(t.col_names)
            typemap = ", ".join(f"{_sql_str(c)}: {_sql_str(ty)}" for c, ty in t.columns)
            src = (
                f"SELECT {cols} FROM read_json({_sql_str(path)}, "
                f"format='newline_delimited', columns={{{typemap}}})"
            )
            pk = ", ".join(t.pk)
            if upsert:
                sets = ", ".join(
                    f"{c} = EXCLUDED.{c}"
                    for c in t.col_names
                    if c not in t.pk and c not in preserve
                )
                self.con.execute(
                    f"INSERT INTO {table} ({cols}) {src} ON CONFLICT ({pk}) DO UPDATE SET {sets}"
                )
                return len(keyed)
            inserted = self.con.execute(
                f"INSERT INTO {table} ({cols}) {src} ON CONFLICT DO NOTHING RETURNING {t.pk[0]}"
            ).fetchall()
            return len(inserted)
        finally:
            os.unlink(path)

    # ------------------------------------------------------------------ runs
    def start_run(self, job: str, config: Mapping[str, Any], git_commit: str = "unknown") -> str:
        # Single-writer file: any run still marked 'running' belongs to a dead process.
        self.con.execute("UPDATE collection_runs SET status = 'abandoned' WHERE status = 'running'")
        now = utcnow()
        run_id = f"{job}-{now:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        self._bulk(
            "collection_runs",
            [
                {
                    "run_id": run_id,
                    "job": job,
                    "started_at": now,
                    "status": "running",
                    "git_commit": git_commit,
                    "config": dict(config),
                }
            ],
        )
        return run_id

    def finish_run(self, run_id: str, status: str, note: str | None = None) -> None:
        self.con.execute(
            "UPDATE collection_runs SET status = ?, ended_at = ?, note = ? WHERE run_id = ?",
            [status, _naive_utc(utcnow()), note, run_id],
        )

    # ------------------------------------------------------------------ metadata
    def upsert_events(self, events: Iterable[Event], run_id: str, *, now: datetime | None = None):
        now = now or utcnow()
        rows = [
            {
                "event_ticker": e.event_ticker,
                "series_ticker": e.series_ticker,
                "title": e.title,
                "sub_title": e.sub_title,
                "category": e.category,
                "mutually_exclusive": e.mutually_exclusive,
                "strike_period": e.strike_period,
                "collateral_return_type": e.collateral_return_type,
                "exchange_index": e.exchange_index,
                "market_tickers": list(e.market_tickers),
                "settlement_sources": [
                    {"name": s.name, "url": s.url} for s in e.settlement_sources
                ],
                "raw": e.raw,
                "first_seen_at": now,
                "last_seen_at": now,
                "run_id": run_id,
            }
            for e in events
        ]
        return self._bulk("events", rows, upsert=True, preserve=("first_seen_at",))

    def upsert_markets(
        self,
        markets: Iterable[Market],
        run_id: str,
        *,
        partition: str,
        now: datetime | None = None,
        source: str = "rest",
    ) -> int:
        now = now or utcnow()
        markets = list(markets)
        rows = []
        for m in markets:
            rows.append(
                {
                    "ticker": m.ticker,
                    "event_ticker": m.event_ticker,
                    "title": m.title,
                    "yes_sub_title": m.yes_sub_title,
                    "no_sub_title": m.no_sub_title,
                    "market_type": m.market_type,
                    "status": m.status.value,
                    "result": m.result.value,
                    "settlement_value": m.settlement_value,
                    "strike_type": m.strike.strike_type,
                    "floor_strike": m.strike.floor,
                    "cap_strike": m.strike.cap,
                    "functional_strike": m.strike.functional,
                    "custom_strike": m.strike.custom,
                    "rules_primary": m.rules.primary,
                    "rules_secondary": m.rules.secondary,
                    "early_close_condition": m.rules.early_close_condition,
                    "can_close_early": m.rules.can_close_early,
                    "open_time": m.open_time,
                    "close_time": m.close_time,
                    "expected_expiration_time": m.expected_expiration_time,
                    "expiration_time": m.expiration_time,
                    "settlement_ts": m.settlement_ts,
                    "price_level_structure": m.price_level_structure,
                    "price_ranges": [
                        {"start": r.start, "end": r.end, "step": r.step} for r in m.price_ranges
                    ],
                    "exchange_index": m.exchange_index,
                    "is_multivariate": m.is_multivariate,
                    "volume": m.volume,
                    "open_interest": m.open_interest,
                    "partition": partition,
                    "raw": m.raw,
                    "first_seen_at": now,
                    "last_seen_at": now,
                    "run_id": run_id,
                }
            )
        n = self._bulk("markets", rows, upsert=True, preserve=("first_seen_at",))
        self._log_status(markets, source, run_id, now)
        return n

    def _load_last_status(self) -> dict[tuple[str, str], tuple]:
        if self._last_status is None:
            rows = self.con.execute(
                "SELECT DISTINCT ON (ticker, source) ticker, source, status, result, "
                "settlement_value FROM market_status_log ORDER BY ticker, source, observed_at DESC"
            ).fetchall()
            self._last_status = {(t, s): (st, r, v) for t, s, st, r, v in rows}
        return self._last_status

    def _log_status(self, markets: Sequence[Market], source: str, run_id: str, now: datetime):
        last = self._load_last_status()
        rows, updates = [], {}
        for m in markets:
            state = (m.status.value, m.result.value, m.settlement_value)
            if last.get((m.ticker, source)) != state:
                updates[(m.ticker, source)] = state
                rows.append(
                    {
                        "ticker": m.ticker,
                        "observed_at": now,
                        "source": source,
                        "status": state[0],
                        "result": state[1],
                        "event_type": "",
                        "settlement_value": state[2],
                        "run_id": run_id,
                    }
                )
        self._bulk("market_status_log", rows)
        last.update(updates)

    def log_lifecycle(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("market_status_log", rows)

    def known_event_tickers(self) -> set[str]:
        return {r[0] for r in self.con.execute("SELECT event_ticker FROM events").fetchall()}

    # ------------------------------------------------------------------ trades
    def insert_trades(
        self, trades: Iterable[Trade], *, source: str, run_id: str, now: datetime | None = None
    ) -> int:
        now = now or utcnow()
        rows = [
            {
                "trade_id": t.trade_id,
                "ticker": t.ticker,
                "created_time": t.created_time,
                "yes_price": t.yes_price,
                "no_price": t.no_price,
                "count": t.count,
                "taker_side": t.taker_side.value if t.taker_side else None,
                "taker_book_side": t.taker_book_side,
                "is_block_trade": t.is_block_trade,
                "source": source,
                "ingested_at": now,
                "run_id": run_id,
            }
            for t in trades
        ]
        return self._bulk("trades", rows)

    def get_sync(self, ticker: str, partition: str) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT complete_through, n_trades, completed_at FROM trade_sync "
            "WHERE ticker = ? AND partition = ?",
            [ticker, partition],
        ).fetchone()
        if row is None:
            return None
        return {
            "complete_through": as_utc(row[0]),
            "n_trades": row[1],
            "completed_at": as_utc(row[2]),
        }

    def set_sync(
        self,
        ticker: str,
        partition: str,
        *,
        complete_through: datetime,
        n_trades: int,
        run_id: str,
        now: datetime | None = None,
    ) -> None:
        self._bulk(
            "trade_sync",
            [
                {
                    "ticker": ticker,
                    "partition": partition,
                    "complete_through": complete_through,
                    "n_trades": n_trades,
                    "completed_at": now or utcnow(),
                    "run_id": run_id,
                }
            ],
            upsert=True,
        )

    # ------------------------------------------------------------------ series fee metadata
    def upsert_series(self, series: Iterable[Any], *, now: datetime | None = None) -> int:
        now = now or utcnow()
        rows = [
            {
                "series_ticker": s.ticker,
                "title": s.title,
                "category": s.category,
                "frequency": s.frequency,
                "fee_type": s.fee_type,
                "fee_multiplier": None if s.fee_multiplier is None else str(s.fee_multiplier),
                "raw": s.model_dump(mode="json"),
                "fetched_at": now,
            }
            for s in series
        ]
        return self._bulk("series_meta", rows, upsert=True)

    def read_series_fees(self) -> dict[str, tuple[str | None, str | None]]:
        """``{series: (fee_type, fee_multiplier_as_decimal_string)}``."""
        if "series_meta" not in self.existing_tables():
            return {}
        return {
            r[0]: (r[1], r[2])
            for r in self.query("SELECT series_ticker, fee_type, fee_multiplier FROM series_meta")
        }

    # ------------------------------------------------------------------ scan state
    def get_scan_state(self, scan_key: str, partition: str) -> dict[str, Any] | None:
        row = self.con.execute(
            "SELECT page_cursor, n_scanned, passed, first_ticker, items::VARCHAR, done, updated_at "
            "FROM scan_state WHERE scan_key = ? AND partition = ?",
            [scan_key, partition],
        ).fetchone()
        if row is None:
            return None
        return {
            "cursor": row[0],
            "n_scanned": row[1],
            "passed": row[2],
            "first_ticker": row[3],
            "items": json.loads(row[4]) if row[4] else [],
            "done": bool(row[5]),
            "updated_at": as_utc(row[6]),
        }

    def set_scan_state(
        self,
        scan_key: str,
        partition: str,
        *,
        cursor: str | None,
        n_scanned: int,
        passed: int,
        first_ticker: str | None,
        items: list,
        done: bool,
        run_id: str,
        now: datetime | None = None,
    ) -> None:
        self._bulk(
            "scan_state",
            [
                {
                    "scan_key": scan_key,
                    "partition": partition,
                    "page_cursor": cursor,
                    "n_scanned": n_scanned,
                    "passed": passed,
                    "first_ticker": first_ticker,
                    "items": items,
                    "done": done,
                    "updated_at": now or utcnow(),
                    "run_id": run_id,
                }
            ],
            upsert=True,
        )

    # ------------------------------------------------------------------ order books
    def insert_book_snapshots(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("book_snapshots", rows)

    def insert_book_deltas(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("book_deltas", rows)

    def insert_stream_events(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("stream_events", rows)

    def insert_poll_log(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("poll_log", rows)

    def insert_quarantine(self, rows: Iterable[Mapping[str, Any]]) -> int:
        return self._bulk("quarantine", rows)

    def latest_book_hashes(self) -> dict[str, tuple[str, int]]:
        rows = self.con.execute(
            "SELECT ticker, arg_max(book_hash, recv_ts_ns), max(recv_ts_ns) "
            "FROM book_snapshots GROUP BY ticker"
        ).fetchall()
        return {t: (h, ts) for t, h, ts in rows}

    # ------------------------------------------------------------------ reading domain objects
    _MARKET_COLS = (
        "ticker, event_ticker, title, yes_sub_title, no_sub_title, market_type, status, result, "
        "settlement_value, strike_type, floor_strike, cap_strike, functional_strike, "
        "custom_strike::VARCHAR, rules_primary, rules_secondary, early_close_condition, "
        "can_close_early, open_time, close_time, expected_expiration_time, expiration_time, "
        "settlement_ts, price_level_structure, price_ranges::VARCHAR, exchange_index, "
        "is_multivariate, volume, open_interest"
    )

    def read_events_with_markets(
        self, where: str = "TRUE", params: Sequence[Any] = ()
    ) -> list[tuple[Event, list[Market]]]:
        """Complete events (event + every stored market) selected by a SQL predicate on ``events``.

        ``where`` is trusted internal SQL, e.g. ``"series_ticker = ?"``."""
        ev_rows = self.query(
            "SELECT event_ticker, series_ticker, title, sub_title, category, mutually_exclusive, "
            "strike_period, collateral_return_type, exchange_index, market_tickers, "
            f"settlement_sources::VARCHAR FROM events e WHERE {where} ORDER BY event_ticker",
            params,
        )
        events: dict[str, Event] = {}
        for r in ev_rows:
            events[r[0]] = Event(
                event_ticker=r[0],
                series_ticker=r[1],
                title=r[2] or "",
                sub_title=r[3] or "",
                category=r[4],
                mutually_exclusive=bool(r[5]),
                strike_period=r[6],
                collateral_return_type=r[7],
                exchange_index=r[8],
                market_tickers=tuple(r[9] or ()),
                settlement_sources=tuple(
                    SettlementSource(x["name"], x.get("url")) for x in json.loads(r[10] or "[]")
                ),
            )
        by_event: dict[str, list[Market]] = {t: [] for t in events}
        m_rows = self.query(
            f"SELECT {self._MARKET_COLS} FROM markets WHERE event_ticker IN "
            f"(SELECT event_ticker FROM events e WHERE {where}) ORDER BY ticker",
            params,
        )
        for r in m_rows:
            ev = events[r[1]]
            by_event[r[1]].append(
                Market(
                    ticker=r[0],
                    event_ticker=r[1],
                    series_ticker=ev.series_ticker,
                    category=ev.category,
                    title=r[2] or "",
                    yes_sub_title=r[3] or "",
                    no_sub_title=r[4] or "",
                    market_type=r[5] or "binary",
                    status=MarketStatus.parse(r[6]),
                    result=SettlementResult.parse(r[7]),
                    settlement_value=r[8],
                    strike=Strike(
                        strike_type=r[9],
                        floor=r[10],
                        cap=r[11],
                        functional=r[12],
                        custom=json.loads(r[13]) if r[13] else None,
                    ),
                    rules=Rules(r[14] or "", r[15] or "", r[16], bool(r[17])),
                    open_time=as_utc(r[18]),
                    close_time=as_utc(r[19]),
                    expected_expiration_time=as_utc(r[20]),
                    expiration_time=as_utc(r[21]),
                    settlement_ts=as_utc(r[22]),
                    price_level_structure=r[23],
                    price_ranges=tuple(
                        PriceRange(x["start"], x["end"], x["step"])
                        for x in json.loads(r[24] or "[]")
                    ),
                    exchange_index=r[25],
                    is_multivariate=bool(r[26]),
                    volume=r[27],
                    open_interest=r[28],
                )
            )
        return [(events[t], by_event[t]) for t in events]

    # ------------------------------------------------------------------ inspection
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[tuple]:
        return self.con.execute(sql, list(params)).fetchall()

    def reconcile_volume(self) -> dict[str, Any]:
        """Completeness check for the trade backfill.

        For every *finalized, fully synced* market the exchange reports its lifetime traded
        volume; the stored trade sizes must add up to exactly that.  A shortfall means missing
        trades (dropped page, partition gap), an excess means duplicates or foreign rows.
        Verified on live data: 30/30 markets reconcile to the centi-contract."""
        checked = self.query(
            "SELECT count(*) FROM markets m WHERE m.status = 'finalized' AND m.volume IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM trade_sync s WHERE s.ticker = m.ticker)"
        )[0][0]
        bad = self.query(
            "SELECT m.ticker, m.volume, coalesce(sum(t.count), 0) AS stored "
            "FROM markets m LEFT JOIN trades t ON t.ticker = m.ticker "
            "WHERE m.status = 'finalized' AND m.volume IS NOT NULL "
            "AND EXISTS (SELECT 1 FROM trade_sync s WHERE s.ticker = m.ticker) "
            "GROUP BY m.ticker, m.volume HAVING m.volume != coalesce(sum(t.count), 0) "
            "ORDER BY abs(m.volume - coalesce(sum(t.count), 0)) DESC"
        )
        return {
            "checked": checked,
            "mismatches": [
                {"ticker": t, "exchange_volume": v, "stored_volume": int(st)} for t, v, st in bad
            ],
        }

    def existing_tables(self) -> set[str]:
        return {r[0] for r in self.query("SELECT table_name FROM information_schema.tables")}

    def counts(self) -> dict[str, int]:
        """Row count per table; tables an older-schema file does not have yet report 0 (a read-only
        connection cannot migrate, but reading such a file must still work)."""
        have = self.existing_tables()
        return {
            t: (self.query(f"SELECT count(*) FROM {t}")[0][0] if t in have else 0) for t in TABLES
        }

    def fingerprint(self) -> dict[str, Any]:
        """Compact, deterministic description of the dataset contents.

        Recorded with every experiment so a result can be tied to the exact data it saw.
        Stable across re-ingestion of identical data (depends on content, not run ids)."""
        q = self.query
        # Content only: no schema version (adding a table must not change every old fingerprint),
        # no empty tables, and no process bookkeeping (run log, scan checkpoints).
        info = {
            "counts": {
                k: v for k, v in self.counts().items() if v and k not in FINGERPRINT_EXCLUDED
            },
            "trades_time_range": [
                str(x) for x in q("SELECT min(created_time), max(created_time) FROM trades")[0]
            ],
            "markets_digest": q(
                "SELECT md5(string_agg(ticker || ':' || status || ':' || result, ',' "
                "ORDER BY ticker)) FROM markets"
            )[0][0],
            "trades_digest": q(
                "SELECT md5(string_agg(trade_id, ',' ORDER BY trade_id)) FROM trades"
            )[0][0],
            "books_digest": q(
                "SELECT md5(string_agg(ticker || ':' || recv_ts_ns || ':' || book_hash, ',' "
                "ORDER BY ticker, recv_ts_ns)) FROM book_snapshots"
            )[0][0],
        }
        info["fingerprint"] = hashlib.sha256(json.dumps(info, sort_keys=True).encode()).hexdigest()[
            :16
        ]
        return info
