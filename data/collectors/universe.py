"""Deterministic, outcome-blind, *resumable* market selection.

Selection must be reproducible (same config -> same markets, regardless of API ordering or how
many times the job was resumed) and must not peek at results.  Hence:

  * candidates are ranked by ``blake2b(seed:ticker)`` - a stable pseudo-random key, not API order;
  * ``sample_fraction`` keeps tickers whose key falls under the fraction;
  * ``max_per_series`` keeps the *lowest keys* per series (so one strike ladder with 40 rungs a day
    cannot swamp everything else), and ``max_markets`` does the same globally.

Scanning is the slow part (~70,000 settled markets per day; a 12-day window is ~840,000 markets and
~14 minutes), so it is checkpointed in the ``scan_state`` table:

  * every ``checkpoint_pages`` pages the page cursor and the current winners are persisted;
  * a restart resumes from that cursor.  The exchange silently treats an invalid cursor as
    "start from page 1", so the first resumed page is compared with the recorded first page and,
    if they match, the scan restarts cleanly instead of pretending to have resumed;
  * a *finished* scan is reused for ``reuse_hours``, so a crash later in the job (trade backfill)
    does not cost another full scan.

Known, deliberate bias (documented in ``docs/data_architecture.md``): filtering on lifetime volume
selects markets that attracted trading, and the historical partition can only be scanned by
``created_time``.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from data.collectors.config import UniverseSpec, to_plain
from data.storage.duckdb_store import Store
from kalshi_client.models import ApiMarket
from kalshi_client.rest import KalshiRestClient
from market.timeutil import utcnow
from market.units import QTY_SCALE

log = logging.getLogger(__name__)

PARTITION_MARGIN = timedelta(days=3)  # settle time can trail close time; scan generously
REHYDRATE_CHUNK = 100  # tickers per /markets?tickers= request


@dataclass
class _Progress:
    """Mutable per-partition scan progress (read by the checkpoint callbacks)."""

    n: int = 0
    pages: int = 0
    first: str | None = None


class ScanOrderError(RuntimeError):
    """The historical listing stopped being ordered by created_time desc; early-stop is unsound."""


def series_of(event_ticker: str) -> str:
    return event_ticker.split("-", 1)[0]


def stable_key(seed: int, ticker: str) -> int:
    digest = hashlib.blake2b(f"{seed}:{ticker}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def scan_key(spec: UniverseSpec) -> str:
    """Identity of a scan: any change to the spec means the old progress is not applicable."""
    blob = json.dumps(to_plain(spec), sort_keys=True).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


@dataclass
class ScanStats:
    scanned: dict[str, int] = field(default_factory=dict)
    passed_filters: int = 0
    selected: int = 0
    elapsed_s: float = 0.0
    partitions: list[str] = field(default_factory=list)
    reused: list[str] = field(default_factory=list)  # partitions served from a completed scan
    resumed: list[str] = field(default_factory=list)  # partitions continued from a checkpoint
    restarted: list[str] = field(default_factory=list)  # checkpoint cursor was ignored -> rescanned


class Selector:
    def __init__(self, spec: UniverseSpec) -> None:
        self.spec = spec
        self._cap = spec.max_per_series
        self._sample_limit = int(spec.sample_fraction * 2**64)
        self._heaps: dict[str, list[tuple[int, str]]] = {}  # series -> heap of (-key, ticker)
        self._meta: dict[str, tuple[ApiMarket, str]] = {}  # ticker -> (market, partition)

    def eligible(self, m: ApiMarket) -> bool:
        s = self.spec
        if m.mve_collection_ticker is not None:
            return False
        if s.status == "settled" and m.status != "finalized":
            return False
        if s.close_after and (m.close_time is None or m.close_time < s.close_after):
            return False
        if s.close_before and (m.close_time is None or m.close_time >= s.close_before):
            return False
        if (m.volume_fp or 0) < s.min_volume * QTY_SCALE:
            return False
        series = series_of(m.event_ticker)
        if s.series and series not in s.series:
            return False
        return series not in s.exclude_series

    def _push(self, key: int, m: ApiMarket, partition: str) -> None:
        heap = self._heaps.setdefault(series_of(m.event_ticker), [])
        item = (-key, m.ticker)
        if self._cap is None or len(heap) < self._cap:
            heapq.heappush(heap, item)
            self._meta[m.ticker] = (m, partition)
        elif item > heap[0]:  # smaller key than the current worst (largest key) -> replace it
            _, evicted = heapq.heapreplace(heap, item)
            del self._meta[evicted]
            self._meta[m.ticker] = (m, partition)

    def offer(self, m: ApiMarket, partition: str) -> bool:
        if m.ticker in self._meta or not self.eligible(m):
            return False
        key = stable_key(self.spec.seed, m.ticker)
        if key >= self._sample_limit and self.spec.sample_fraction < 1:
            return False
        self._push(key, m, partition)
        return True

    def result(self) -> list[tuple[ApiMarket, str]]:
        items = sorted(
            ((stable_key(self.spec.seed, t), t) for t in self._meta), key=lambda kt: kt
        )  # ascending key, ties by ticker
        if self.spec.max_markets is not None:
            items = items[: self.spec.max_markets]
        return sorted((self._meta[t] for _, t in items), key=lambda x: x[0].ticker)

    # ---- persistence: only (key, ticker) pairs; the markets themselves are re-fetched by ticker
    def state_items(self, partition: str) -> list[list]:
        return sorted(
            [stable_key(self.spec.seed, t), t] for t, (_, p) in self._meta.items() if p == partition
        )

    def drop_partition(self, partition: str) -> None:
        """Forget everything that came from ``partition`` (used when a resume turns out invalid)."""
        keep = [(m, p) for m, p in self._meta.values() if p != partition]
        self._heaps.clear()
        self._meta.clear()
        for m, p in keep:
            self._push(stable_key(self.spec.seed, m.ticker), m, p)

    def restore(self, markets: list[ApiMarket], partition: str) -> None:
        for m in markets:
            if m.ticker not in self._meta:
                self._push(stable_key(self.spec.seed, m.ticker), m, partition)


def needs_partition(spec: UniverseSpec, partition: str, cutoff: datetime) -> bool:
    """Skip scanning a partition that cannot contain any market in the close-time window."""
    if spec.status != "settled":
        return True
    if partition == "historical":  # settled before the cutoff
        return spec.close_after is None or spec.close_after < cutoff + PARTITION_MARGIN
    return spec.close_before is None or spec.close_before > cutoff - PARTITION_MARGIN


async def _rehydrate(
    rest: KalshiRestClient, tickers: list[str], partition: str, on_invalid: Callable | None
) -> list[ApiMarket]:
    out: list[ApiMarket] = []
    for i in range(0, len(tickers), REHYDRATE_CHUNK):
        chunk = tickers[i : i + REHYDRATE_CHUNK]
        async for m in rest.iter_markets(
            tickers=chunk,
            historical=partition == "historical",
            include_multivariate=True,
            on_invalid=on_invalid,
        ):
            if m.ticker in chunk:  # never trust the API to return only what was asked for
                out.append(m)
    if len(out) < len(tickers):
        log.warning(
            "rehydrate %s: %d of %d markets no longer served", partition, len(out), len(tickers)
        )
    return out


async def scan_universe(
    rest: KalshiRestClient,
    spec: UniverseSpec,
    *,
    cutoff: datetime,
    store: Store | None = None,
    run_id: str = "",
    checkpoint_pages: int = 25,
    reuse_hours: float = 24.0,
    now: Callable[[], datetime] = utcnow,
    on_invalid: Callable | None = None,
    progress: Callable[[str, int], None] | None = None,
) -> tuple[list[tuple[ApiMarket, str]], ScanStats]:
    t0 = time.monotonic()
    sel, stats = Selector(spec), ScanStats()
    key = scan_key(spec)
    passed_by: dict[str, int] = {}

    stats.partitions = [p for p in ("live", "historical") if needs_partition(spec, p, cutoff)]
    if spec.status != "settled":
        stats.partitions = ["live"]

    for part in stats.partitions:
        state = store.get_scan_state(key, part) if store else None
        start_cursor: str | None = None
        pr = _Progress()
        passed_by[part] = 0
        first_ticker: str | None = None

        if state is not None:
            markets = await _rehydrate(rest, [t for _, t in state["items"]], part, on_invalid)
            age_ok = state["updated_at"] is not None and (
                now() - state["updated_at"] <= timedelta(hours=reuse_hours)
            )
            if state["done"] and age_ok:
                sel.restore(markets, part)
                passed_by[part], stats.scanned[part] = state["passed"], state["n_scanned"]
                stats.reused.append(part)
                log.info("scan %s: reusing completed scan (%d selected)", part, len(markets))
                continue
            if not state["done"] and state["cursor"]:
                sel.restore(markets, part)
                start_cursor, first_ticker = state["cursor"], state["first_ticker"]
                pr.n, passed_by[part] = state["n_scanned"], state["passed"]
                log.info("scan %s: resuming from checkpoint after %d markets", part, pr.n)

        pr.first = first_ticker  # first market of page 1, kept across resumes
        awaiting_first_page = start_cursor is not None
        floor = None
        if part == "historical" and spec.close_after:
            floor = spec.close_after - timedelta(days=spec.created_lookback_days)

        def persist(next_cursor, *, done: bool, part=part, pr=pr) -> None:
            if store is not None:
                store.set_scan_state(
                    key,
                    part,
                    cursor=next_cursor,
                    n_scanned=pr.n,
                    passed=passed_by[part],
                    first_ticker=pr.first,
                    items=sel.state_items(part),
                    done=done,
                    run_id=run_id,
                    now=now(),
                )

        def on_page(next_cursor, part=part, pr=pr) -> None:
            pr.pages += 1
            if next_cursor is None:
                persist(None, done=True, part=part, pr=pr)
            elif pr.pages % checkpoint_pages == 0:
                persist(next_cursor, done=False, part=part, pr=pr)

        prev_created, stopped_early = None, False
        async for m in rest.iter_markets(
            status=spec.status if part == "live" else None,
            min_close_ts=int(spec.close_after.timestamp())
            if part == "live" and spec.close_after
            else None,
            max_close_ts=int(spec.close_before.timestamp())
            if part == "live" and spec.close_before
            else None,
            historical=part == "historical",
            on_invalid=on_invalid,
            start_cursor=start_cursor,
            on_page=on_page,
        ):
            if awaiting_first_page:
                awaiting_first_page = False
                if m.ticker == first_ticker:
                    # The exchange ignored our cursor and served page 1 again.  Drop this
                    # partition's partial progress so nothing is double counted; scan from the top.
                    log.warning("scan %s: saved cursor ignored by the exchange; restarting", part)
                    sel.drop_partition(part)
                    pr.n, passed_by[part] = 0, 0
                    stats.restarted.append(part)
                else:
                    stats.resumed.append(part)
            if pr.first is None:
                pr.first = m.ticker
            pr.n += 1
            if part == "historical":
                if m.created_time is not None:
                    if prev_created is not None and m.created_time > prev_created:
                        raise ScanOrderError(
                            f"historical listing not sorted by created_time desc at {m.ticker}: "
                            f"{m.created_time} > {prev_created}"
                        )
                    prev_created = m.created_time
                    if floor is not None and m.created_time < floor:
                        stopped_early = True
                        break
                if pr.n >= spec.historical_max_scan:
                    log.warning(
                        "historical scan hit historical_max_scan=%d; window incomplete", pr.n
                    )
                    stopped_early = True
                    break
            if sel.offer(m, part):
                passed_by[part] += 1
            if progress and pr.n % 5000 == 0:
                progress(part, pr.n)
        stats.scanned[part] = pr.n
        if stopped_early:  # breaking out skips the end-of-stream page callback
            persist(None, done=True, part=part, pr=pr)

    result = sel.result()
    stats.passed_filters, stats.selected = sum(passed_by.values()), len(result)
    stats.elapsed_s = time.monotonic() - t0
    return result, stats
