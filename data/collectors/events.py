"""Collect complete settled events (the event plus every sibling market) for relationship research.

The history dataset samples individual markets, so it cannot say whether an event's outcomes were
mutually exclusive or exhaustive: that needs all siblings.  This job fetches, per series, the most
recent settled events with their nested markets and stores them in the ordinary ``events`` /
``markets`` tables of a *separate* database (so the history dataset's definition and fingerprint
stay untouched).  Re-running is idempotent and skips series that already have enough events.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from data.collectors.config import EventsConfig, to_plain
from data.normalization.normalizer import normalize_event, normalize_market
from data.storage.duckdb_store import Store
from kalshi_client.exceptions import (
    APIError,
    AuthenticationError,
    MessageValidationError,
    TransportError,
)
from kalshi_client.rest import KalshiRestClient
from market.timeutil import now_ns, utcnow

log = logging.getLogger(__name__)


@dataclass
class EventsSummary:
    run_id: str = ""
    series_total: int = 0
    series_skipped: int = 0
    series_fetched: int = 0
    events: int = 0
    markets: int = 0
    quarantined: int = 0
    failures: list[str] = field(default_factory=list)


class EventCollector:
    def __init__(
        self,
        store: Store,
        rest: KalshiRestClient,
        cfg: EventsConfig,
        series: list[str],
        *,
        git_commit: str = "unknown",
    ) -> None:
        self.store, self.rest, self.cfg, self.series = store, rest, cfg, sorted(set(series))
        self.git_commit = git_commit
        self.summary = EventsSummary(series_total=len(self.series))
        self.run_id = ""
        self._qseq = 0

    def _quarantine(self, source: str, item, exc: Exception) -> None:
        self._qseq += 1
        self.summary.quarantined += 1
        self.store.insert_quarantine(
            [
                {
                    "run_id": self.run_id,
                    "recv_ts_ns": now_ns(),
                    "seq_no": self._qseq,
                    "source": source,
                    "error": str(exc)[:2000],
                    "raw": str(item)[:20000],
                }
            ]
        )

    async def run(self) -> EventsSummary:
        self.run_id = self.store.start_run("events", to_plain(self.cfg), self.git_commit)
        self.summary.run_id = self.run_id
        status, note = "ok", None
        try:
            have = dict(
                self.store.query(
                    "SELECT series_ticker, count(*) FROM events "
                    "WHERE series_ticker IS NOT NULL GROUP BY 1"
                )
            )
            todo = [s for s in self.series if have.get(s, 0) < self.cfg.per_series]
            self.summary.series_skipped = len(self.series) - len(todo)
            log.info(
                "events: %d series to fetch (%d already complete)",
                len(todo),
                self.summary.series_skipped,
            )
            sem = asyncio.Semaphore(self.cfg.concurrency)
            done = 0

            async def one(series: str) -> None:
                nonlocal done
                async with sem:
                    try:
                        await self._fetch_series(series)
                    except AuthenticationError:
                        raise
                    except (APIError, TransportError, MessageValidationError) as exc:
                        self.summary.failures.append(f"{series}: {exc}")
                        log.warning("series %s failed (retried next run): %s", series, exc)
                    done += 1
                    if done % 50 == 0 or done == len(todo):
                        log.info(
                            "events: %d/%d series | %d events | %d markets",
                            done,
                            len(todo),
                            self.summary.events,
                            self.summary.markets,
                        )

            async with asyncio.TaskGroup() as tg:
                for s in todo:
                    tg.create_task(one(s))
        except (KeyboardInterrupt, asyncio.CancelledError):
            status = "interrupted"
            raise
        except Exception as exc:
            status, note = "failed", repr(exc)[:500]
            raise
        finally:
            self.store.finish_run(self.run_id, status, note)
        return self.summary

    async def _fetch_series(self, series: str) -> None:
        events, markets = [], []
        async for api in self.rest.iter_events(
            series_ticker=series,
            status="settled",
            with_nested_markets=True,
            page_size=self.cfg.per_series,
            limit=self.cfg.per_series,
            on_invalid=lambda item, e: self._quarantine("event", item.get("event_ticker"), e),
        ):
            try:
                ev = normalize_event(api)
                events.append(ev)
                markets += [normalize_market(m, event=ev) for m in api.markets or []]
            except MessageValidationError as exc:
                self._quarantine("normalize", api.event_ticker, exc)
        now = utcnow()
        self.store.upsert_events(events, self.run_id, now=now)
        self.store.upsert_markets(markets, self.run_id, partition="events_study", now=now)
        self.summary.series_fetched += 1
        self.summary.events += len(events)
        self.summary.markets += len(markets)
