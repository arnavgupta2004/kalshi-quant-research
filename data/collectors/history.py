"""Resumable historical collector: markets -> events -> trades, into the local ``Store``.

Resumption model
  * The universe scan is deterministic (see ``universe.py``), so re-running selects the same
    markets.
  * Metadata is upserted; facts (trades) are ``ON CONFLICT DO NOTHING`` keyed on the exchange's
    ``trade_id`` - re-ingesting can never duplicate.
  * Per (ticker, partition) a checkpoint in ``trade_sync`` is written only *after* that
    partition was paginated to the end.  A crash mid-market therefore just repeats that market
    (already-written trades are skipped by the primary key).  Finished settled markets are
    skipped without any API call.

Partitions: the exchange splits data at ``GET /historical/cutoff``.  ``/historical/*`` holds
what predates it and ``/markets*`` what follows, so a market's trades may live in either or
(if it straddles the cutoff) both.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from data.collectors.config import HistoryConfig, to_plain
from data.collectors.universe import ScanStats, scan_universe
from data.normalization.normalizer import normalize_event, normalize_market, normalize_trade
from data.storage.duckdb_store import Store
from kalshi_client.exceptions import (
    APIError,
    AuthenticationError,
    MessageValidationError,
    NotFoundError,
    TransportError,
)
from kalshi_client.rest import KalshiRestClient
from market.contracts import Event, Market
from market.timeutil import now_ns, parse_iso8601, utcnow

log = logging.getLogger(__name__)

FLUSH_TRADES = 5000
SYNC_MARGIN = timedelta(seconds=60)  # checkpoint = fetch start - margin (late-arriving trades)


@dataclass
class HistorySummary:
    run_id: str = ""
    scan: ScanStats = field(default_factory=ScanStats)
    markets_upserted: int = 0
    events_fetched: int = 0
    events_missing: int = 0
    trades_inserted: int = 0
    partitions_synced: int = 0
    partitions_skipped: int = 0
    failures: list[str] = field(default_factory=list)
    quarantined: int = 0


def trade_partitions(m: Market, cutoff: datetime, now: datetime) -> list[str]:
    """Which exchange partitions can hold this market's trades."""
    ends = [t for t in (m.close_time, m.settlement_ts) if t is not None]
    end = max(ends) if ends else now
    parts = []
    if m.open_time is None or m.open_time < cutoff:
        parts.append("historical")
    if end >= cutoff:
        parts.append("live")
    return parts or ["live"]


def already_final(sync: dict | None, m: Market) -> bool:
    """A finished sync of a settled market cannot change - skip without touching the API."""
    if sync is None or not m.status.is_terminal or m.settlement_ts is None:
        return False
    return sync["completed_at"] is not None and sync["completed_at"] > m.settlement_ts


class HistoryCollector:
    def __init__(
        self,
        store: Store,
        rest: KalshiRestClient,
        cfg: HistoryConfig,
        *,
        git_commit: str = "unknown",
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.store, self.rest, self.cfg = store, rest, cfg
        self.git_commit, self._now = git_commit, now
        self.run_id = ""
        self.cutoff_note = ""
        self.summary = HistorySummary()
        self._quarantine: list[dict] = []
        self._qseq = 0

    # ------------------------------------------------------------------ quarantine
    def _quarantine_item(self, source: str, item, exc: Exception) -> None:
        self._qseq += 1
        self.summary.quarantined += 1
        self._quarantine.append(
            {
                "run_id": self.run_id,
                "recv_ts_ns": now_ns(),
                "seq_no": self._qseq,
                "source": source,
                "error": str(exc)[:2000],
                "raw": str(item)[:20000],
            }
        )

    def _flush_quarantine(self) -> None:
        if self._quarantine:
            self.store.insert_quarantine(self._quarantine)
            self._quarantine = []

    # ------------------------------------------------------------------ main
    async def run(self) -> HistorySummary:
        self.run_id = self.store.start_run("history", to_plain(self.cfg), self.git_commit)
        self.summary.run_id = self.run_id
        status, note = "ok", None
        try:
            await self._run()
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = "interrupted"
            raise
        except Exception as exc:
            status, note = "failed", repr(exc)[:500]
            raise
        finally:
            self._flush_quarantine()
            parts = [self.cutoff_note]
            if self.summary.failures:
                parts.append(f"{len(self.summary.failures)} per-market failures")
            if note:
                parts.append(note)
            note = "; ".join(p for p in parts if p) or None
            self.store.finish_run(self.run_id, status, note)
        return self.summary

    async def _run(self) -> None:
        s, cfg = self.summary, self.cfg
        cutoffs = await self.rest.get_historical_cutoff()
        cutoff = parse_iso8601(cutoffs["trades_created_ts"])
        market_cutoff = parse_iso8601(cutoffs["market_settled_ts"])
        # the exchange's live/historical boundary moves forward over time, so record what we saw
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        self.cutoff_note = (
            f"historical cutoff: trades={cutoff:{fmt}}, markets={market_cutoff:{fmt}}"
        )
        log.info(self.cutoff_note)

        selected, s.scan = await scan_universe(
            self.rest,
            cfg.universe,
            cutoff=market_cutoff,
            store=self.store,
            run_id=self.run_id,
            checkpoint_pages=cfg.checkpoint_pages,
            reuse_hours=cfg.scan_reuse_hours,
            now=self._now,
            on_invalid=lambda item, e: self._quarantine_item("scan", item.get("ticker"), e),
            progress=lambda part, n: log.info("scan %s: %d markets", part, n),
        )
        log.info(
            "universe: scanned=%s passed=%d selected=%d in %.0fs",
            s.scan.scanned,
            s.scan.passed_filters,
            s.scan.selected,
            s.scan.elapsed_s,
        )

        markets: list[tuple[Market, str]] = []
        for api, partition in selected:
            try:
                markets.append((normalize_market(api), partition))
            except MessageValidationError as exc:
                self._quarantine_item("normalize_market", api.ticker, exc)
        for partition in ("historical", "live"):
            batch = [m for m, p in markets if p == partition]
            for i in range(0, len(batch), 1000):
                s.markets_upserted += self.store.upsert_markets(
                    batch[i : i + 1000], self.run_id, partition=partition, now=self._now()
                )

        if cfg.fetch_events:
            await self._fetch_events({m.event_ticker for m, _ in markets})

        await self._backfill_trades([m for m, _ in markets], cutoff)
        log.info("done: %s", s)

    # ------------------------------------------------------------------ events
    async def _fetch_events(self, tickers: set[str]) -> None:
        todo = sorted(tickers - self.store.known_event_tickers())
        log.info("events: %d to fetch (%d already stored)", len(todo), len(tickers) - len(todo))
        sem = asyncio.Semaphore(self.cfg.concurrency)

        async def one(t: str) -> Event | None:
            async with sem:
                try:
                    return normalize_event(await self.rest.get_event(t, with_nested_markets=False))
                except NotFoundError:
                    self.summary.events_missing += 1
                    return Event(t, None, "", "", None, False, raw={"missing": True})
                except AuthenticationError:
                    raise
                except (APIError, TransportError, MessageValidationError) as exc:
                    self.summary.failures.append(f"event {t}: {exc}")
                    return None

        for i in range(0, len(todo), 200):
            async with asyncio.TaskGroup() as tg:  # a fatal error cancels the siblings
                tasks = [tg.create_task(one(t)) for t in todo[i : i + 200]]
            events = [e for e in (t.result() for t in tasks) if e]
            self.store.upsert_events(events, self.run_id, now=self._now())
            self.summary.events_fetched += len(events)

    # ------------------------------------------------------------------ trades
    async def _backfill_trades(self, markets: list[Market], cutoff: datetime) -> None:
        sem = asyncio.Semaphore(self.cfg.concurrency)
        total, done, t0 = len(markets), 0, time.monotonic()

        async def one(m: Market) -> None:
            nonlocal done
            async with sem:
                try:
                    for part in trade_partitions(m, cutoff, self._now()):
                        await self._sync_trades(m, part)
                except AuthenticationError:
                    raise
                except (APIError, TransportError, MessageValidationError) as exc:
                    self.summary.failures.append(f"trades {m.ticker}: {exc}")
                    log.warning("trades %s failed (will retry next run): %s", m.ticker, exc)
                done += 1
                if done % 100 == 0 or done == total:
                    log.info(
                        "trades: %d/%d markets | +%d rows | %d req | %.0fs",
                        done,
                        total,
                        self.summary.trades_inserted,
                        self.rest.stats["requests"],
                        time.monotonic() - t0,
                    )

        async with asyncio.TaskGroup() as tg:
            for m in markets:
                tg.create_task(one(m))

    async def _sync_trades(self, m: Market, partition: str) -> None:
        s = self.summary
        sync = self.store.get_sync(m.ticker, partition)
        if partition == "historical" and sync is not None or already_final(sync, m):
            s.partitions_skipped += 1  # historical data is immutable once fully fetched
            return
        started = self._now()
        min_ts = None
        if sync is not None:
            min_ts = int(
                (sync["complete_through"] - timedelta(seconds=self.cfg.overlap_s)).timestamp()
            )
        buf, fetched, inserted = [], 0, 0
        async for api_trade in self.rest.iter_trades(
            ticker=m.ticker,
            min_ts=min_ts,
            historical=(partition == "historical"),
            page_size=self.cfg.trade_page_size,
            on_invalid=lambda item, e: self._quarantine_item("trade_page", item, e),
        ):
            try:
                buf.append(normalize_trade(api_trade))
            except MessageValidationError as exc:
                self._quarantine_item("normalize_trade", api_trade.trade_id, exc)
                continue
            fetched += 1
            if len(buf) >= FLUSH_TRADES:
                inserted += self.store.insert_trades(
                    buf, source=f"rest_{partition}", run_id=self.run_id, now=self._now()
                )
                buf = []
        inserted += self.store.insert_trades(
            buf, source=f"rest_{partition}", run_id=self.run_id, now=self._now()
        )
        # only now - after pagination reached the end - may the checkpoint advance
        self.store.set_sync(
            m.ticker,
            partition,
            complete_through=started - SYNC_MARGIN,
            n_trades=(sync["n_trades"] if sync else 0) + inserted,
            run_id=self.run_id,
            now=self._now(),
        )
        s.trades_inserted += inserted
        s.partitions_synced += 1
        self._flush_quarantine()
