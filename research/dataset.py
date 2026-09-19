"""Loading a recorded books database into everything the arbitrage research needs.

The feed is windowed to the *recording period* (first book snapshot .. last poll).  Without this the
trade tape - which reaches back to the start of each market's life - drags the clock hours or days
before the first book and every exposure and rate would be computed over a span in which nothing was
being observed.  (The first smoke run reported a 12,164-minute span for a 64-minute recording.)

Outcomes are read separately and never enter the feed: they are used only to *score* opportunities
after the fact (did the bundle pay off?), never to detect or size them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arbitrage.detector import build_specs
from arbitrage.opportunity import RelationSpec
from backtest.feed import StoreFeed
from data.storage.duckdb_store import Store
from market.fees import FeeBook
from research.scanner import ScanConfig, Scanner, ScanResult, standard_variants


@dataclass
class Dataset:
    name: str
    db: str
    fingerprint: str
    window_ns: tuple[int, int]
    feed: StoreFeed
    specs: list[RelationSpec]
    categories: dict[str, str]
    scheduled_end: dict[str, object]
    fees: FeeBook
    settled: dict[str, int] = field(default_factory=dict)  # ticker -> YES settlement (ticks)
    n_events: int = 0

    def scan(self, config: ScanConfig | None = None) -> ScanResult:
        cfg = config or ScanConfig(standard_variants(self.fees))
        return Scanner(self.specs, self.categories, self.scheduled_end, cfg).run(self.feed)


def load_dataset(db: str, name: str | None = None) -> Dataset:
    with Store(db, read_only=True) as s:
        lo, hi = s.query("SELECT min(recv_ts_ns), max(recv_ts_ns) FROM book_snapshots")[0]
        poll_hi = s.query("SELECT max(recv_ts_ns) FROM poll_log")[0][0]
        end = max(hi, poll_hi or hi)
        bundles = s.read_events_with_markets()
        fees = FeeBook.from_series_meta(s.read_series_fees())
        feed = StoreFeed(s, start_ns=lo, end_ns=end)
        len(feed)  # materialise the events while the store is open
        specs = build_specs(bundles)
        settled = {
            m.ticker: m.settlement_value for m in feed.markets if m.settlement_value is not None
        }
        fp = s.fingerprint()["fingerprint"]
    return Dataset(
        name=name or db,
        db=db,
        fingerprint=fp,
        window_ns=(lo, end),
        feed=feed,
        specs=specs,
        categories={m.ticker: (m.category or "unknown") for m in feed.markets},
        scheduled_end={t: i.scheduled_end for t, i in feed.infos.items()},
        fees=fees,
        settled=settled,
        n_events=len(bundles),
    )
