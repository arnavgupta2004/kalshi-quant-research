"""Prediction instances: (market, time t) -> what a model may see, and separately what happened.

    Observation   everything knowable at ``t``: the tape and (if recorded) the book up to ``t``, the
    market's static description from the ``MarketInfo`` whitelist, the *scheduled* end. Outcome
    the label and when it became known.  Models never receive it at prediction time.

**Point in time.**  Static fields come only through ``MarketInfo`` (no ``close_time``, ``status``,
``volume``...).  The one place realised times are used is deciding *which instants exist*: an
instance is created only if the market was open at ``t`` (``open_time <= t < close_time``) - a live
system knows what is open now, so conditioning on it is legitimate - and never as a feature.

**Selection, stated.**  The trade-history dataset was chosen on *lifetime* volume (Stage 2), which
is only known after the fact: its markets are ones that went on to trade.  Every result using it
inherits that survivorship and says so.  The recorded book datasets were selected ex ante (top by
volume / scheduled expiry at recording time).

**Sealed holdout.**  Research and evaluation periods are separated at load time, before any feature
is computed (spec s.20: never tune on the final evaluation set):

    trade history     research = markets closing before 2026-09-09, holdout = 2026-09-09 onward
    recorded books    research = ``books_structural`` + ``books_shortlived``,
                      holdout  = ``books_research_wide`` + ``books_research_short``

``include_holdout`` defaults to ``False``; opening it is an explicit act that is recorded in
``ACCESS_LOG``.  Stage 7 never opens it; Stage 8 does, once.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import numpy as np

from backtest.market_info import MarketInfo
from data.storage.duckdb_store import Store
from market.contracts import Market
from market.evidence import StatsBook
from market.order_book import OrderBook
from market.relationships import RelationKind
from market.semantics import EvidenceLevel, analyze_event
from market.timeutil import to_epoch_us
from pricing.microstructure import (
    SEC,
    BookFeatures,
    TradeFeatures,
    TradeTape,
    book_features,
    hours_to_expiry,
    reference_price,
    trade_features,
)

HORIZONS_S = (86_400, 21_600, 7_200, 3_600, 900)  # 24h, 6h, 2h, 1h, 15min before the scheduled end
HISTORY_HOLDOUT_FROM = datetime(2026, 9, 9, tzinfo=UTC)
RESEARCH_BOOK_DBS = ("books_structural", "books_shortlived")
HOLDOUT_BOOK_DBS = ("books_research_wide", "books_research_short")

#: every time the sealed holdout was opened: (what, when).  Reviewed in the write-up.
ACCESS_LOG: list[tuple[str, str]] = []


class SealedHoldoutError(RuntimeError):
    pass


@dataclass(frozen=True)
class Observation:
    ticker: str
    event: str
    series: str
    category: str
    strike_type: str
    n_siblings: int | None  # markets in the event (a public fact once the event is listed)
    t_ns: int
    hours_to_expiry: float | None
    ref: float
    ref_source: str
    trade: TradeFeatures
    book: BookFeatures | None
    #: reference prices of every market in this event's partition at t (mutually exclusive AND
    #: exhaustive, evidence >= DECLARED), and this market's position in it; ``None`` otherwise
    event_refs: tuple[float, ...] | None = None
    event_pos: int | None = None


@dataclass(frozen=True)
class Outcome:
    y: int  # 1 = YES
    settled_ns: int


@dataclass(frozen=True)
class Instance:
    obs: Observation
    out: Outcome
    split: str  # "research" | "holdout"
    dataset: str

    @property
    def group(self) -> str:
        """The unit of independence: siblings of one event share an outcome structure."""
        return self.obs.event


def _ns(dt: datetime) -> int:
    return to_epoch_us(dt) * 1000


def series_of(ticker: str) -> str:
    return ticker.split("-", 1)[0]


def n_bucket(n: int | None) -> str:
    if n is None or n <= 0:
        return "?"
    return "1" if n <= 1 else "2" if n == 2 else "3-5" if n <= 5 else "6-10" if n <= 10 else "11+"


def _binary_label(m: Market) -> int | None:
    """0/1 for a market settled YES or NO; ``None`` for scalar/void/unsettled (no binary label)."""
    v = m.settlement_value
    return None if v is None else 1 if v == 10_000 else 0 if v == 0 else None


def load_tapes(store: Store, tickers: Iterable[str]) -> dict[str, TradeTape]:
    """All prints of the given markets, grouped by ticker (the trade table has millions of rows)."""
    want = set(tickers)
    rows = store.con.execute(
        "SELECT ticker, epoch_ns(created_time) AS ts, yes_price, count, taker_side "
        "FROM trades ORDER BY ticker, created_time"
    ).fetchall()
    by: dict[str, list] = defaultdict(list)
    for tk, ts, px, cnt, side in rows:
        if tk in want:
            by[tk].append((ts, px, cnt, side))
    return {tk: TradeTape.from_rows(v) for tk, v in by.items()}


def event_sizes(db: str) -> dict[str, int]:
    """Markets per event, counted from the stored markets - trustworthy only for a database of
    COMPLETE events (``relations.duckdb``).  (The API's ``market_tickers`` list was empty for every
    event in the history dataset, and counting its markets undercounts: both gave a size of 1 and
    made a base-rate model treat every event as a single-market event.)"""
    with Store(db, read_only=True) as s:
        return {
            r[0]: r[1] for r in s.query("SELECT event_ticker, count(*) FROM markets GROUP BY 1")
        }


def _observation(
    m: Market,
    info: MarketInfo,
    n_sib: int | None,
    t_ns: int,
    tape: TradeTape,
    book: BookFeatures | None,
) -> Observation | None:
    tf = trade_features(tape, t_ns)
    ref, src = reference_price(book, tf)
    if ref is None:
        return None
    end = None if info.scheduled_end is None else _ns(info.scheduled_end)
    return Observation(
        ticker=m.ticker,
        event=m.event_ticker,
        series=series_of(m.ticker),
        category=info.category or "unknown",
        strike_type=info.strike.strike_type or "none",
        n_siblings=n_sib,
        t_ns=t_ns,
        hours_to_expiry=hours_to_expiry(end, t_ns),
        ref=ref,
        ref_source=src,
        trade=tf,
        book=book,
    )


# --------------------------------------------------------------------------- trade-history dataset
def history_instances(
    db: str,
    *,
    horizons_s: Sequence[int] = HORIZONS_S,
    include_holdout: bool = False,
    sizes: dict[str, int] | None = None,
) -> list[Instance]:
    """Instances at fixed times before the *scheduled* end, from the Stage 2 trade-history dataset.

    Only ``ref_source`` in {trade_mid, last_trade} exists here (no recorded books).  ``sizes`` maps
    an event to its true number of markets where that is known (from a complete-event corpus);
    events not in it get ``n_siblings = None`` rather than a wrong count."""
    if include_holdout:
        ACCESS_LOG.append((f"history_instances({db})", datetime.now(UTC).isoformat()))
    out: list[Instance] = []
    with Store(db, read_only=True) as s:
        markets = [
            m
            for m in s.read_markets()
            if _binary_label(m) is not None
            and m.expected_expiration_time
            and m.open_time
            and m.close_time
            and m.settlement_ts
        ]
        keep = []
        for m in markets:
            holdout = m.close_time >= HISTORY_HOLDOUT_FROM
            if holdout and not include_holdout:
                continue  # sealed: not even featurised
            keep.append((m, "holdout" if holdout else "research"))
        tapes = load_tapes(s, (m.ticker for m, _ in keep))
    for m, split in keep:
        tape = tapes.get(m.ticker)
        if tape is None or not len(tape):
            continue
        info = MarketInfo.from_market(m)
        end = _ns(m.expected_expiration_time)
        for h in horizons_s:
            t = end - h * SEC
            if not (_ns(m.open_time) <= t < _ns(m.close_time)):
                continue  # not open then: no live system would have predicted it
            obs = _observation(m, info, (sizes or {}).get(m.event_ticker), t, tape, None)
            if obs is not None:
                out.append(
                    Instance(obs, Outcome(_binary_label(m), _ns(m.settlement_ts)), split, "history")
                )
    return out


# --------------------------------------------------------------------------- recorded-book datasets
def structure_stats(db: str, before_ns: int) -> StatsBook:
    """Per-series outcome statistics (e.g. how often exactly one outcome was YES) from complete
    events that had **fully settled before** ``before_ns``.  Point in time: the evidence that a
    series' outcomes are exhaustive cannot include the events that are later scored with it."""
    book = StatsBook()
    with Store(db, read_only=True) as s:
        for event, markets in s.read_events_with_markets():
            if (
                markets
                and all(m.settlement_value is not None and m.settlement_ts for m in markets)
                and max(_ns(m.settlement_ts) for m in markets) < before_ns
            ):
                book.add_event(event.series_ticker or event.event_ticker.split("-")[0], markets)
    return book


def partition_groups(
    store: Store, *, stats=None, minimum: EvidenceLevel = EvidenceLevel.DECLARED
) -> dict[str, tuple[str, ...]]:
    """event -> tickers of its partition (mutually exclusive AND exhaustive), where the event's
    recorded markets form one at evidence >= ``minimum``.  Uses the Stage 3 semantics, not a
    guess."""
    out: dict[str, tuple[str, ...]] = {}
    for event, markets in store.read_events_with_markets():
        if len(markets) < 2:
            continue
        analysis = analyze_event(event, markets, stats=stats)
        best: tuple[str, ...] = ()
        for a in analysis.assessments:
            if a.relation.kind is RelationKind.PARTITION and a.level >= minimum:
                tk = tuple(a.relation.tickers())
                if len(tk) > len(best):
                    best = tk
        if best:
            out[event.event_ticker] = tuple(sorted(best))
    return out


def _book_at(times: list[int], snaps: list, t: int) -> OrderBook | None:
    i = bisect.bisect_right(times, t) - 1
    if i < 0:
        return None
    b = OrderBook("x")
    ypx, yq, npx, nq = snaps[i]
    b.apply_snapshot(list(zip(ypx, yq, strict=True)), list(zip(npx, nq, strict=True)))
    return b


def book_instances(
    db: str,
    *,
    step_s: int = 900,
    include_holdout: bool = False,
    sizes: dict[str, int] | None = None,
    stats: StatsBook | None = None,
    partition_level: EvidenceLevel = EvidenceLevel.EMPIRICAL,
) -> list[Instance]:
    """Instances every ``step_s`` while a recorded market was open and its book was being polled."""
    label = db.rsplit("/", 1)[-1].removesuffix(".duckdb")
    if label in HOLDOUT_BOOK_DBS:
        if not include_holdout:
            raise SealedHoldoutError(
                f"{label} is the sealed holdout: pass include_holdout=True (recorded in ACCESS_LOG)"
            )
        ACCESS_LOG.append((f"book_instances({db})", datetime.now(UTC).isoformat()))
        split = "holdout"
    elif label in RESEARCH_BOOK_DBS:
        split = "research"
    else:
        raise ValueError(f"{label}: not registered as a research or holdout dataset")
    out: list[Instance] = []
    with Store(db, read_only=True) as s:
        all_markets = s.read_markets()
        markets = [
            m
            for m in all_markets
            if _binary_label(m) is not None
            and m.expected_expiration_time
            and m.open_time
            and m.close_time
            and m.settlement_ts
        ]
        parts = partition_groups(s, stats=stats, minimum=partition_level)
        tapes = load_tapes(s, (m.ticker for m in all_markets))  # siblings need theirs too
        polls = [r[0] for r in s.query("SELECT recv_ts_ns FROM poll_log ORDER BY recv_ts_ns")]
        snap_rows = s.query(
            "SELECT ticker, recv_ts_ns, yes_px, yes_qty, no_px, no_qty FROM book_snapshots "
            "ORDER BY ticker, recv_ts_ns"
        )
    by: dict[str, tuple[list[int], list]] = defaultdict(lambda: ([], []))
    for tk, ts, ypx, yq, npx, nq in snap_rows:
        by[tk][0].append(ts)
        by[tk][1].append((ypx, yq, npx, nq))

    def ref_at(tk: str, t: int, age: float) -> float | None:
        if tk not in by:
            return None
        bk = book_features(_book_at(*by[tk][:1], by[tk][1], t), age)
        tape = tapes.get(tk) or TradeTape.from_rows([])
        return reference_price(bk, trade_features(tape, t))[0]

    for m in markets:
        if m.ticker not in by:
            continue
        times, snaps = by[m.ticker]
        info = MarketInfo.from_market(m)
        tape = tapes.get(m.ticker) or TradeTape.from_rows([])
        group = parts.get(m.event_ticker)
        lo = max(times[0], _ns(m.open_time))
        hi = min(polls[-1], _ns(m.close_time))
        t = lo + 60 * SEC
        while t < hi:
            j = bisect.bisect_right(polls, t) - 1
            if j >= 0:
                age = (t - polls[j]) / SEC
                bk = book_features(_book_at(times, snaps, t), age)
                obs = _observation(m, info, (sizes or {}).get(m.event_ticker), t, tape, bk)
                if obs is not None:
                    if group and m.ticker in group:
                        refs = [ref_at(tk, t, age) for tk in group]
                        if all(r is not None for r in refs):
                            obs = replace(
                                obs, event_refs=tuple(refs), event_pos=group.index(m.ticker)
                            )
                    out.append(
                        Instance(obs, Outcome(_binary_label(m), _ns(m.settlement_ts)), split, label)
                    )
            t += step_s * SEC
    return out


# --------------------------------------------------------------------------- corpus for the base-
# rate model
@dataclass(frozen=True)
class CorpusRow:
    event: str
    series: str
    category: str
    strike_type: str
    n_bucket: str
    y: int
    settled_ns: int


def corpus_rows(db: str, *, min_volume: int | None = 10_000) -> list[CorpusRow]:
    """Settled markets of complete events: the record a base-rate model may learn from.

    ``min_volume`` (centi-contracts, default 100 contracts) applies the *same* lifetime-volume rule
    that selected the history dataset.  Without it the corpus is dominated by far-out-of-the-money
    ladder rungs that resolve NO (30% YES overall) while the markets being predicted are ones that
    went on to trade (~45% YES): a base rate learned on the wrong population is confidently wrong.
    ``None`` keeps every market (reported as a sensitivity)."""
    rows: list[CorpusRow] = []
    sizes = event_sizes(db)
    with Store(db, read_only=True) as s:
        for m in s.read_markets():
            y = _binary_label(m)
            if y is None or not m.settlement_ts:
                continue
            if min_volume is not None and (m.volume or 0) < min_volume:
                continue
            rows.append(
                CorpusRow(
                    m.event_ticker,
                    series_of(m.ticker),
                    m.category or "unknown",
                    m.strike.strike_type or "none",
                    n_bucket(sizes.get(m.event_ticker)),
                    y,
                    _ns(m.settlement_ts),
                )
            )
    rows.sort(key=lambda r: r.settled_ns)
    return rows


def as_arrays(instances: Sequence[Instance]) -> tuple[np.ndarray, np.ndarray]:
    """(reference prices, outcomes) for quick scoring."""
    return (
        np.array([i.obs.ref for i in instances], dtype=float),
        np.array([i.out.y for i in instances], dtype=int),
    )
