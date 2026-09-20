"""Choose the markets to paper-trade and load what the strategies need to know about them.

Public REST only (no credentials).  Selection is the recorder's own rule (``select_book_tickers``:
the most active open events by 24 h volume, with all their siblings); metadata comes from the event
endpoint, so each market carries its category, scheduled end and settlement rules, and the fee
schedule from the series endpoint.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from backtest.market_info import MarketInfo
from data.collectors.config import BookSelection
from data.collectors.universe import series_of
from data.normalization.normalizer import normalize_event, normalize_market
from kalshi_client.rest import KalshiRestClient
from market.contracts import Event, Market
from market.fees import FeeBook
from market.timeutil import utcnow


@dataclass
class Universe:
    tickers: list[str]
    infos: dict[str, MarketInfo]
    markets: dict[str, Market]
    events: dict[str, Event]
    event_of: dict[str, str]
    fees: FeeBook = field(default_factory=FeeBook)

    def bundles(self) -> list[tuple[Event, list[Market]]]:
        """``[(Event, [Market])]`` as ``arbitrage.detector.build_specs`` expects."""
        by: dict[str, list[Market]] = {}
        for t in self.tickers:
            m = self.markets[t]
            by.setdefault(m.event_ticker, []).append(m)
        return [(self.events[e], ms) for e, ms in by.items() if e in self.events]


async def select_markets(rest: KalshiRestClient, sel: BookSelection) -> list:
    """The recorder's selection rule (most active open events by 24 h volume, with all their
    siblings), keeping the market objects so the universe needs ONE pass over the open markets."""
    by_event: dict[str, list] = defaultdict(list)
    async for m in rest.iter_markets(status="open"):
        if series_of(m.event_ticker) in sel.exclude_series:
            continue
        if sel.include_series and series_of(m.event_ticker) not in sel.include_series:
            continue
        if sel.max_hours_to_expiry is not None:
            due = m.expected_expiration_time or m.close_time
            if due is None or (due - utcnow()).total_seconds() > sel.max_hours_to_expiry * 3600:
                continue
        by_event[m.event_ticker].append(m)
    ranked = sorted(
        (
            (sum(m.volume_24h_fp or 0 for m in ms), et)
            for et, ms in by_event.items()
            if len(ms) >= sel.min_event_markets
        ),
        key=lambda x: (-x[0], x[1]),
    )
    chosen: list = []
    for n, (vol, et) in enumerate(ranked):
        if n >= sel.top_events or vol <= 0:
            break
        ms = by_event[et] if sel.include_siblings else [m for m in by_event[et] if m.volume_24h_fp]
        if len(chosen) + len(ms) > sel.max_tickers:
            continue  # never truncate an event mid-way
        chosen += sorted(ms, key=lambda m: m.ticker)
    return chosen


async def load_universe(rest: KalshiRestClient, sel: BookSelection) -> Universe:
    chosen = await select_markets(rest, sel)
    tickers = [m.ticker for m in chosen]
    wanted = set(tickers)
    event_tickers = {m.event_ticker for m in chosen}
    events: dict[str, Event] = {}
    markets: dict[str, Market] = {}
    for et in sorted(event_tickers):
        api = await rest.get_event(et, with_nested_markets=True)
        ev = normalize_event(api)
        events[et] = ev
        for am in api.markets:
            if am.ticker in wanted:
                markets[am.ticker] = normalize_market(am, event=ev)
    tickers = [t for t in tickers if t in markets]
    meta: dict[str, tuple[str | None, str | None]] = {}
    for s in sorted({e.series_ticker for e in events.values() if e.series_ticker}):
        api_s = await rest.get_series(s)
        meta[s] = (
            api_s.fee_type,
            None if api_s.fee_multiplier is None else str(api_s.fee_multiplier),
        )
    return Universe(
        tickers=tickers,
        infos={t: MarketInfo.from_market(markets[t]) for t in tickers},
        markets={t: markets[t] for t in tickers},
        events=events,
        event_of={t: markets[t].event_ticker for t in tickers},
        fees=FeeBook.from_series_meta(meta),
    )
