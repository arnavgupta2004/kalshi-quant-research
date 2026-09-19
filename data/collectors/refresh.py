"""Refresh a recorded-books database: settlements and trades for the markets it already holds.

Book recorders capture markets while they are open.  To *backtest* on them you also need what
happened next: the trade tape (evidence for passive-fill models) and the settlements (to score
positions).  This job re-fetches each market's current state and backfills its trades, reusing the
resumable per-(ticker, partition) trade sync of the history collector - so it can be re-run
repeatedly as more of the recorded markets settle, and only new trades are fetched.

Point-in-time note: ``market_status_log.observed_at`` records when *this job* saw a state, which is
after the fact; backtests take settlement times from the exchange's own ``settlement_ts`` instead.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from data.collectors.config import HistoryConfig, RefreshConfig, UniverseSpec, to_plain
from data.collectors.history import HistoryCollector, trade_partitions
from data.normalization.normalizer import normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.exceptions import (
    APIError,
    AuthenticationError,
    MessageValidationError,
    TransportError,
)
from kalshi_client.rest import KalshiRestClient
from market.timeutil import parse_iso8601, utcnow

log = logging.getLogger(__name__)


@dataclass
class RefreshSummary:
    run_id: str = ""
    markets: int = 0
    settled_now: int = 0
    newly_settled: int = 0
    trades_inserted: int = 0
    failures: list[str] = field(default_factory=list)


class RefreshCollector:
    def __init__(
        self,
        store: Store,
        rest: KalshiRestClient,
        cfg: RefreshConfig,
        *,
        git_commit: str = "unknown",
        now: Callable[[], datetime] = utcnow,
    ):
        self.store, self.rest, self.cfg, self.git_commit = store, rest, cfg, git_commit
        self._now = now
        self.summary = RefreshSummary()
        # Reuse the history collector's resumable trade sync rather than duplicating it.
        self._hc = HistoryCollector(
            store,
            rest,
            HistoryConfig(
                universe=UniverseSpec(status="open"),
                concurrency=cfg.concurrency,
                trade_page_size=cfg.trade_page_size,
                overlap_s=cfg.overlap_s,
            ),
            now=now,
        )

    async def run(self) -> RefreshSummary:
        run_id = self.store.start_run("refresh", to_plain(self.cfg), self.git_commit)
        self.summary.run_id = self._hc.run_id = run_id
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
            self._hc._flush_quarantine()
            self.store.finish_run(run_id, status, note)
        return self.summary

    async def _run(self) -> None:
        s, cfg = self.summary, self.cfg
        before = {
            m.ticker: m.settlement_value is not None
            for m in self.store.read_markets(cfg.tickers or None)
        }
        tickers = sorted(before)
        s.markets = len(tickers)
        cutoff = parse_iso8601((await self.rest.get_historical_cutoff())["trades_created_ts"])
        fresh = []
        for i in range(0, len(tickers), 100):
            async for api in self.rest.iter_markets(
                tickers=tickers[i : i + 100],
                include_multivariate=True,
                on_invalid=lambda item, e: self._hc._quarantine_item("refresh", item, e),
            ):
                try:
                    fresh.append(normalize_market(api))
                except MessageValidationError as exc:
                    self._hc._quarantine_item("normalize_market", api.ticker, exc)
        self.store.upsert_markets(fresh, s.run_id, partition="live")
        s.settled_now = sum(m.settlement_value is not None for m in fresh)
        s.newly_settled = sum(
            1 for m in fresh if m.settlement_value is not None and not before.get(m.ticker)
        )
        log.info(
            "refreshed %d markets (%d settled, %d newly)",
            len(fresh),
            s.settled_now,
            s.newly_settled,
        )

        sem = asyncio.Semaphore(cfg.concurrency)
        now = self._now()

        async def one(m) -> None:
            async with sem:
                try:
                    for part in trade_partitions(m, cutoff, now):
                        await self._hc._sync_trades(m, part)
                except AuthenticationError:
                    raise
                except (APIError, TransportError, MessageValidationError) as exc:
                    s.failures.append(f"trades {m.ticker}: {exc}")

        async with asyncio.TaskGroup() as tg:
            for m in fresh:
                tg.create_task(one(m))
        s.trades_inserted = self._hc.summary.trades_inserted
