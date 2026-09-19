"""Opportunity episodes: every violated constraint, from first sighting to disappearance.

The Stage 4 replay asked "how many constraint-cycles look violated?".  Research needs the
*lifecycle* (spec s.9): when did a violation appear, how big and how liquid was it, what did fees
and depth leave of it, how long did it live, what ended it, and did it pay off at settlement?
``Scanner`` walks a chronological feed and produces one ``Episode`` per (constraint, uninterrupted
violation).

Design decisions worth knowing

  * **Incremental but exact.**  Only relations touching a book that changed are re-priced, and a
    test proves the open episodes equal a from-scratch ``detect`` over *every* relation after every
    batch of events.  (Stage 4's replay re-priced everything every cycle.)
  * **Simultaneous events are one observation.**  Events with an identical timestamp are applied
    together before anything is evaluated - the Stage 5 lesson (a poll response is one instant;
    seeing its books one at a time manufactured a +$983 phantom arbitrage).
  * **Detection time is when *we* first saw it.**  Nothing earlier is knowable.  A lifetime is
    measured from there and is *interval-censored* (alive at ``last_alive``, dead at
    ``first_dead``): see ``research.stats``.
  * **Poll cycles chunk the universe.**  Within one cycle the books of different chunks arrive up
    to a few seconds apart, so a cross-market violation can be a composite of two moments.  An
    episode is ``persisted`` only if it was still there at the end of a *later* poll cycle (every
    leg re-fetched), which a transient composite cannot survive.  Single-observation episodes are
    reported separately.
  * **Latency probes are as-of, and bracketed.**  For each delay D we evaluate the order that
    would have arrived D after detection against (a) the last book we had observed by then
    ("before": what the Stage 5 engine does; optimistic) and (b) the first book observed after it
    ("after": as if every change happened just before the order arrived; pessimistic).  The truth
    lies between; the width of the bracket *is* the data's inability to resolve latencies below the
    polling interval.
  * **Recording gaps are not silent.**  If no book-side event is seen for ``gap_s`` the recorder
    was blind: open episodes are censored at the last time they were confirmed, and exposure is
    not counted.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

from arbitrage.detector import DetectorParams, detect
from arbitrage.execution import PricedBundle, price_bundle
from arbitrage.opportunity import Classification, Opportunity, RelationSpec
from backtest.events import (
    BookConfirm,
    BookUpdate,
    FeedEvent,
    MarketClose,
    Settlement,
    TradeTick,
)
from backtest.fills import TakerModel
from market.contracts import Side, Trade
from market.fees import FeeBook
from market.order_book import OrderBook
from market.relationships import Constraint, asks_from_books
from market.semantics import EvidenceLevel

SEC = 1_000_000_000
MS = 1_000_000

#: latency grid: the spec's list (1..250 ms) plus the scales the polling data can actually resolve
DEFAULT_DELTAS_NS = tuple(
    int(x * SEC) for x in (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60)
)

STAGES = ("displayed", "liquid", "after_fees", "after_slippage", "executable")


def constraint_id(con: Constraint) -> str:
    """Stable identity of a trade: its legs and bound (identical trades reached through several
    relations are one constraint - the detector's own dedupe key)."""
    legs = sorted(f"{leg.contract}x{leg.qty}" for leg in con.legs)
    return "|".join(legs) + f"|>={con.bound}"


# --------------------------------------------------------------------------- records
@dataclass(frozen=True)
class VariantView:
    """One cost model's verdict on one violated constraint at one instant."""

    cls: str  # Classification value
    net_micro: int | None
    gross_micro: int | None
    slippage_micro: int | None
    fee_micro: int | None
    bundles: int  # profit-maximising size, centi-bundles (0 if untradable)
    breakeven: int
    liquid: bool
    fee_ok: bool
    depth_ok: bool
    limits: tuple[int, ...]  # per leg (constraint order): worst price the order would accept
    priced: PricedBundle | None  # only for tradable classes; used to settle the bundle later

    @property
    def stage(self) -> int:
        """Deepest funnel stage reached: 0 displayed .. 4 executable."""
        if self.cls == Classification.EXECUTABLE.value:
            return 4
        if self.depth_ok:
            return 3
        if self.fee_ok:
            return 2
        return 1 if self.liquid else 0

    @property
    def tradable(self) -> bool:
        return bool(self.limits)


@dataclass(frozen=True)
class Observation:
    ts_ns: int
    top_margin: int  # gross edge per contract-bundle at the best asks, ticks
    top_qty: int  # centi-bundles at those best prices
    capacity: int  # centi-bundles the books could fill at any price
    views: Mapping[str, VariantView]


@dataclass(frozen=True)
class ProbeResult:
    """The order that would have arrived ``delta_ns`` after detection, against one book state."""

    delta_ns: int
    kind: str  # "before" (last observed by then) | "after" (first observed later)
    alive: bool  # constraint still violated at the top of book
    margin: int  # gross edge per bundle at the top of book then (ticks); 0 if gone or closed
    closed: bool  # a leg's market was no longer trading
    since_confirm_ns: int | None  # how stale the book knowledge was at the probe time
    beyond_data: bool  # the probe time is after the last data we have: not observable
    #: variant -> (whole bundle fillable within the limits, min filled fraction across legs, net µ$)
    variants: Mapping[str, tuple[bool, float, int | None]]


@dataclass
class Episode:
    id: int
    key: str
    name: str
    kind: str  # kind of the (strongest-evidence) relation the trade is credited to
    constraint_kind: str  # kind of the constraint itself (an exclusivity leg of a partition...)
    level: str
    event: str
    series: str
    category: str
    tickers: tuple[str, ...]
    legs: tuple[tuple[str, str, int], ...]  # (ticker, side, qty per bundle)
    constraint: Constraint
    first_seen_ns: int
    left_censored: bool  # already there when observation (re)started: true start unknown
    hours_to_expiry: float | None
    first: Observation
    peak: dict[str, VariantView]  # per variant: the view with the highest net seen
    best_stage: dict[str, int]
    last_alive_ns: int
    n_confirms: int = 0  # poll-cycle ends at which it was (re)confirmed, counted from detection
    end_ns: int | None = None  # first time seen gone
    end_reason: str = "open"  # consumed | quote_change | closed | gap | end_of_data
    obs: list[Observation] = field(default_factory=list)
    probes: list[ProbeResult] = field(default_factory=list)
    _last_legs: tuple = ()  # leg asks at the last alive observation (for attribution)

    @property
    def persisted(self) -> bool:
        """Seen at the end of a *later* poll cycle than the detecting one (all legs re-fetched)."""
        return self.n_confirms >= 2

    @property
    def censored(self) -> bool:
        """Lifetime known only from below: it was still alive when we stopped looking."""
        return self.end_reason in ("closed", "gap", "end_of_data")

    @property
    def lo_s(self) -> float:
        return (self.last_alive_ns - self.first_seen_ns) / SEC

    @property
    def hi_s(self) -> float:
        if self.censored or self.end_ns is None:
            return math.inf
        return (self.end_ns - self.first_seen_ns) / SEC

    @property
    def orders(self) -> dict[str, VariantView]:
        return {v: vw for v, vw in self.first.views.items() if vw.tradable}


@dataclass(frozen=True)
class SpecExposure:
    """How long a relation was observable (all books present, none closed, recorder alive)."""

    event: str
    series: str
    category: str
    kind: str
    level: str
    n_constraints: int
    seconds: float
    cycles: int


@dataclass
class ScanResult:
    episodes: list[Episode]
    exposures: list[SpecExposure]
    first_ts_ns: int
    last_ts_ns: int
    cycles: int
    gaps: list[tuple[int, int]]
    batches: int
    recording_s: float = 0.0  # total time observed (summed over datasets when pooled)

    @property
    def span_s(self) -> float:
        return self.recording_s


# --------------------------------------------------------------------------- config
@dataclass(frozen=True)
class ScanConfig:
    variants: Mapping[str, DetectorParams]
    gap_s: float = 30.0
    probe_deltas_ns: tuple[int, ...] = DEFAULT_DELTAS_NS
    trade_memory_ns: int = 10 * 60 * SEC
    min_obs_change_ticks: int = 0  # record an observation only if the top-of-book edge changed


def standard_variants(fees: FeeBook, *, target: int = 10_000) -> dict[str, DetectorParams]:
    """Cost models compared throughout Stage 6.  All price *every* evidence level: the level is an
    attribute of an episode, and analyses filter on it (so one scan serves all of them)."""
    mk = lambda f: DetectorParams(  # noqa: E731
        fees=f, target=target, max_size=target, min_level=EvidenceLevel.UNVERIFIED
    )
    return {
        "baseline": mk(fees),
        "fees_half": mk(fees.scaled(Fraction(1, 2))),
        "fee_free": mk(fees.scaled(Fraction(0))),
    }


# --------------------------------------------------------------------------- scanner
class Scanner:
    def __init__(
        self,
        specs: Sequence[RelationSpec],
        categories: Mapping[str, str],
        scheduled_end: Mapping[str, object],
        config: ScanConfig,
        *,
        observer: Callable[[Scanner, int], None] | None = None,
    ) -> None:
        if not config.variants:
            raise ValueError("at least one cost variant is required")
        self.specs = list(specs)
        self.cfg = config
        self.categories = categories
        self.scheduled_end = scheduled_end
        self.observer = observer
        free = FeeBook().scaled(Fraction(0))
        self.driver = DetectorParams(fees=free, min_level=EvidenceLevel.UNVERIFIED)

        self.spec_tickers: list[tuple[str, ...]] = [tuple(s.relation.tickers()) for s in self.specs]
        self.by_ticker: dict[str, list[int]] = defaultdict(list)
        for i, ts in enumerate(self.spec_tickers):
            for t in ts:
                self.by_ticker[t].append(i)
        self.n_constraints = [
            s.relation.n_constraints()
            if hasattr(s.relation, "n_constraints")
            else len(s.relation.constraints())
            for s in self.specs
        ]

        self.books: dict[str, OrderBook] = {}
        self.closed: set[str] = set()
        self.settled_value: dict[str, int] = {}
        self._trades: dict[str, deque[tuple[int, Trade]]] = defaultdict(deque)

        self.open: dict[str, Episode] = {}
        self.done: list[Episode] = []
        self._next_id = 0
        self._probes: list[tuple[int, int, int, int]] = []  # (probe_ts, seq, episode id, delta)
        self._pending_after: list[tuple[Episode, int]] = []
        self._by_id: dict[int, Episode] = {}
        self._seq = 0

        self._active_since: dict[int, int] = {}
        self._exposure_ns = [0] * len(self.specs)
        self._cycles_active = [0] * len(self.specs)
        self._last_live: int | None = (
            None  # last time any book-side event proved the recorder alive
        )
        self._last_confirm: int | None = None
        self._confirms_since_restart = 0
        self._cycles = 0
        self._gaps: list[tuple[int, int]] = []
        self._batches = 0
        self._first_ts: int | None = None
        self._last_ts = 0
        self.taker = TakerModel()

    # ------------------------------------------------------------------ main loop
    def run(self, feed: Iterable[FeedEvent]) -> ScanResult:
        it = iter(feed)
        nxt = next(it, None)
        while nxt is not None:
            batch = [nxt]
            nxt = next(it, None)
            while nxt is not None and nxt.ts_ns == batch[0].ts_ns:
                batch.append(nxt)
                nxt = next(it, None)
            self._process(batch[0].ts_ns, batch)
        return self._finish()

    def _process(self, ts: int, batch: list[FeedEvent]) -> None:
        self._batches += 1
        if self._first_ts is None:
            self._first_ts = ts
        self._last_ts = ts
        observes_books = any(isinstance(e, (BookUpdate, BookConfirm)) for e in batch)

        self._flush_probes_before(ts)
        if observes_books:
            self._check_gap(ts)

        changed: set[str] = set()
        closed_now: set[str] = set()
        confirm = False
        for e in batch:
            if isinstance(e, BookUpdate):
                bk = OrderBook(e.ticker)
                bk.apply_snapshot(e.yes_bids, e.no_bids)
                self.books[e.ticker] = bk
                changed.add(e.ticker)
            elif isinstance(e, BookConfirm):
                confirm = True
            elif isinstance(e, TradeTick):
                q = self._trades[e.ticker]
                q.append((ts, e.trade))
                while q and q[0][0] < ts - self.cfg.trade_memory_ns:
                    q.popleft()
            elif isinstance(e, MarketClose):
                closed_now.add(e.ticker)
            elif isinstance(e, Settlement):
                closed_now.add(e.ticker)
                self.settled_value[e.ticker] = e.value
        newly_closed = closed_now - self.closed
        self.closed |= closed_now
        if observes_books:
            self._last_live = ts

        touched = changed | newly_closed
        if newly_closed:
            self._close_for_market(ts, newly_closed)
        if touched:
            self._evaluate(ts, changed)
            self._update_activity(ts, touched)
        if confirm:
            self._on_confirm(ts)
        if observes_books:
            self._flush_probes_after(ts)
        if self.observer:
            self.observer(self, ts)

    # ------------------------------------------------------------------ gaps
    def _check_gap(self, ts: int) -> None:
        last = self._last_live
        if last is not None and ts - last > int(self.cfg.gap_s * SEC):
            self._gaps.append((last, ts))
            for ep in list(self.open.values()):
                self._end(ep, ts, "gap", censor=True)
            for i, since in list(self._active_since.items()):
                self._exposure_ns[i] += max(0, last - since)
                self._active_since[i] = ts
            self._confirms_since_restart = 0

    # ------------------------------------------------------------------ evaluation
    def _spec_active(self, i: int) -> bool:
        return all(t in self.books and t not in self.closed for t in self.spec_tickers[i])

    def _update_activity(self, ts: int, touched: set[str]) -> None:
        affected = {i for t in touched for i in self.by_ticker.get(t, ())}
        for i in affected:
            active = self._spec_active(i)
            if active and i not in self._active_since:
                self._active_since[i] = ts
            elif not active and i in self._active_since:
                self._exposure_ns[i] += max(0, ts - self._active_since.pop(i))

    def _evaluate(self, ts: int, changed: set[str]) -> None:
        affected = sorted({i for t in changed for i in self.by_ticker.get(t, ())})
        specs = [self.specs[i] for i in affected if self._spec_active(i)]
        found: dict[str, Opportunity] = {}
        if specs:
            ops, _ = detect(specs, self.books, self.driver, ts_ns=ts)  # specs skip closed tickers
            for op in ops:
                found[constraint_id(op.constraint)] = op
        for key, op in found.items():
            ep = self.open.get(key)
            obs = self._observe(op, ts)
            if ep is None:
                self._open(op, obs, ts)
            else:
                ep.last_alive_ns = ts
                self._record(ep, obs)
        for key, ep in list(self.open.items()):
            if key in found:
                continue
            if any(t in changed for t in ep.tickers):
                self._end(ep, ts, self._why_gone(ep, ts))

    def _observe(self, op: Opportunity, ts: int) -> Observation:
        from arbitrage.execution import max_bundles  # local: keeps the module import light

        views: dict[str, VariantView] = {}
        for name, params in self.cfg.variants.items():
            vops, _ = detect([op.spec], self.books, params, ts_ns=ts)
            match = next(
                (v for v in vops if constraint_id(v.constraint) == constraint_id(op.constraint)),
                None,
            )
            views[name] = _view(match) if match is not None else _EMPTY_VIEW
        return Observation(
            ts, op.top_margin, op.top_qty, max_bundles(op.constraint, self.books), views
        )

    def _open(self, op: Opportunity, obs: Observation, ts: int) -> None:
        con = op.constraint
        tickers = tuple(sorted({leg.contract.ticker for leg in con.legs}))
        ep = Episode(
            id=self._next_id,
            key=constraint_id(con),
            name=con.name,
            kind=op.spec.relation.kind.value,
            constraint_kind=con.kind.value,
            level=op.spec.level.name,
            event=op.spec.event_ticker or "",
            series=op.spec.series or "",
            category=self._category(tickers),
            tickers=tickers,
            legs=tuple((leg.contract.ticker, leg.contract.side.value, leg.qty) for leg in con.legs),
            constraint=con,
            first_seen_ns=ts,
            left_censored=self._confirms_since_restart == 0,
            hours_to_expiry=self._hours_to_expiry(tickers, ts),
            first=obs,
            peak=dict(obs.views),
            best_stage={v: vw.stage for v, vw in obs.views.items()},
            last_alive_ns=ts,
            obs=[obs],
        )
        self._next_id += 1
        ep._last_legs = self._leg_tops(ep)
        self.open[ep.key] = ep
        self._by_id[ep.id] = ep
        for d in self.cfg.probe_deltas_ns:
            self._seq += 1
            heapq.heappush(self._probes, (ts + d, self._seq, ep.id, d))

    def _record(self, ep: Episode, obs: Observation) -> None:
        last = ep.obs[-1]
        if (
            abs(obs.top_margin - last.top_margin) > self.cfg.min_obs_change_ticks
            or obs.top_qty != last.top_qty
            or obs.capacity != last.capacity
        ):
            ep.obs.append(obs)
        for v, vw in obs.views.items():
            ep.best_stage[v] = max(ep.best_stage[v], vw.stage)
            cur = ep.peak[v].net_micro
            if vw.net_micro is not None and (cur is None or vw.net_micro > cur):
                ep.peak[v] = vw
        ep._last_legs = self._leg_tops(ep)

    def _category(self, tickers: Sequence[str]) -> str:
        cats = {self.categories.get(t) or "unknown" for t in tickers}
        return cats.pop() if len(cats) == 1 else "mixed"

    def _hours_to_expiry(self, tickers: Sequence[str], ts: int) -> float | None:
        from market.timeutil import to_epoch_us

        ends = [self.scheduled_end.get(t) for t in tickers]
        if any(e is None for e in ends):
            return None
        return (min(to_epoch_us(e) * 1000 for e in ends) - ts) / (3600 * SEC)

    # ------------------------------------------------------------------ ending an episode
    def _leg_tops(self, ep: Episode) -> tuple:
        out = []
        for ticker, side, _ in ep.legs:
            bk = self.books.get(ticker)
            lv = bk.best_ask(Side(side)) if bk else None
            out.append((ticker, side, lv.price if lv else None, lv.qty if lv else 0))
        return tuple(out)

    def _why_gone(self, ep: Episode, ts: int) -> str:
        """Attribute a disappearance: a qualifying trade in the gap since we last saw it alive means
        the edge was *taken*; otherwise a leg's quote moved or was pulled.  (The trade tape carries
        exchange timestamps, the books our receive time: attribution is only as sharp as the polling
        interval, and says so in the docs.)"""
        for ticker, side, price, _ in ep._last_legs:
            if price is None:
                continue
            for tts, tr in self._trades.get(ticker, ()):
                if not (ep.last_alive_ns < tts <= ts):
                    continue
                if tr.taker_side is not None and tr.taker_side.value == side:
                    px = tr.yes_price if side == "yes" else tr.no_price
                    if px >= price:
                        return "consumed"
        return "quote_change"

    def _end(self, ep: Episode, ts: int, reason: str, *, censor: bool = False) -> None:
        ep.end_reason = reason
        ep.end_ns = None if (censor or reason in ("closed", "gap", "end_of_data")) else ts
        del self.open[ep.key]
        self.done.append(ep)

    def _close_for_market(self, ts: int, tickers: set[str]) -> None:
        for ep in list(self.open.values()):
            if tickers & set(ep.tickers):
                self._end(ep, ts, "closed", censor=True)

    def _on_confirm(self, ts: int) -> None:
        self._last_confirm = ts
        self._cycles += 1
        for i, _ in self._active_since.items():
            self._cycles_active[i] += 1
        for ep in self.open.values():  # includes episodes opened at this very instant
            ep.n_confirms += 1
            ep.last_alive_ns = ts
        self._confirms_since_restart += 1

    # ------------------------------------------------------------------ latency probes
    def _flush_probes_before(self, ts: int) -> None:
        while self._probes and self._probes[0][0] < ts:
            p, _, eid, d = heapq.heappop(self._probes)
            ep = self._by_id[eid]
            ep.probes.append(self._probe(ep, d, "before", p, beyond=False))
            self._pending_after.append((ep, d))

    def _flush_probes_after(self, ts: int) -> None:
        pend, self._pending_after = self._pending_after, []
        for ep, d in pend:
            ep.probes.append(self._probe(ep, d, "after", ep.first_seen_ns + d, beyond=False))

    def _probe(self, ep: Episode, delta: int, kind: str, p_ts: int, *, beyond: bool) -> ProbeResult:
        closed = any(t in self.closed for t in ep.tickers) or any(
            t not in self.books for t in ep.tickers
        )
        chk = None if closed else ep.constraint.check(asks_from_books(self.books))
        alive = chk is not None and chk.margin > 0
        margin = chk.margin if alive else 0
        results: dict[str, tuple[bool, float, int | None]] = {}
        for name, vw in ep.orders.items():
            results[name] = self._try_order(ep, vw, name, self.books, closed)
        since = None if self._last_confirm is None else p_ts - self._last_confirm
        return ProbeResult(delta, kind, alive, margin, closed, since, beyond, results)

    def _try_order(
        self, ep: Episode, vw: VariantView, name: str, books: Mapping[str, OrderBook], closed: bool
    ) -> tuple[bool, float, int | None]:
        if closed:
            return (False, 0.0, None)
        fees = self.cfg.variants[name].fees
        pb = price_bundle(ep.constraint, books, vw.bundles, fees)
        if pb is not None and all(
            leg.fills[-1].price <= lim for leg, lim in zip(pb.legs, vw.limits, strict=True)
        ):
            return (True, 1.0, pb.net_micro)
        fracs = []
        for leg, lim in zip(ep.constraint.legs, vw.limits, strict=True):
            want = leg.qty * vw.bundles
            fills = self.taker.execute(
                books.get(leg.contract.ticker), 0, leg.contract.side, lim, want
            )
            fracs.append(sum(q for _, q in fills) / want)
        return (False, min(fracs), None)

    # ------------------------------------------------------------------ finish
    def _finish(self) -> ScanResult:
        end = self._last_ts
        for ep in list(self.open.values()):
            self._end(ep, end, "end_of_data", censor=True)
        while self._probes:  # probes scheduled beyond the data cannot be observed
            p, _, eid, d = heapq.heappop(self._probes)
            ep = self._by_id[eid]
            ep.probes.append(self._probe(ep, d, "before", p, beyond=True))
        for i, since in self._active_since.items():
            self._exposure_ns[i] += max(0, end - since)
        self._active_since.clear()
        exposures = [
            SpecExposure(
                s.event_ticker or "",
                s.series or "",
                self._category(self.spec_tickers[i]),
                s.relation.kind.value,
                s.level.name,
                self.n_constraints[i],
                self._exposure_ns[i] / SEC,
                self._cycles_active[i],
            )
            for i, s in enumerate(self.specs)
        ]
        eps = sorted(self.done, key=lambda e: (e.first_seen_ns, e.id))
        first = self._first_ts or 0
        return ScanResult(
            eps,
            exposures,
            first,
            end,
            self._cycles,
            self._gaps,
            self._batches,
            (end - first) / SEC,
        )

    # ------------------------------------------------------------------ test hook
    def displayed_now(self) -> set[str]:
        """Keys of the currently open episodes (for the equivalence test against a full re-scan)."""
        return set(self.open)


_EMPTY_VIEW = VariantView(
    "theoretical", None, None, None, None, 0, 0, False, False, False, (), None
)


def _view(op: Opportunity) -> VariantView:
    pb = op.priced
    tradable = op.classification in (Classification.EXECUTABLE, Classification.PARTIAL) and pb
    return VariantView(
        cls=op.classification.value,
        net_micro=op.net_micro,
        gross_micro=op.gross_micro,
        slippage_micro=op.slippage_micro,
        fee_micro=op.fee_micro,
        bundles=pb.bundles if pb else 0,
        breakeven=op.breakeven_bundles,
        liquid=op.liquid,
        fee_ok=op.profitable_after_fees_top,
        depth_ok=op.profitable_after_depth,
        limits=tuple(lg.fills[-1].price for lg in pb.legs) if tradable else (),
        priced=pb if tradable else None,
    )


def merge_results(results: Sequence[tuple[str, ScanResult]]) -> ScanResult:
    """Pool scans of different recordings.  Event names are prefixed with the dataset label so the
    same event recorded twice stays two clusters (two disjoint windows are not one observation)."""
    from dataclasses import replace

    eps: list[Episode] = []
    exposures: list[SpecExposure] = []
    gaps: list[tuple[int, int]] = []
    for label, r in results:
        eps += [replace(e, event=f"{label}:{e.event}") for e in r.episodes]
        exposures += [replace(x, event=f"{label}:{x.event}") for x in r.exposures]
        gaps += r.gaps
    return ScanResult(
        eps,
        exposures,
        min(r.first_ts_ns for _, r in results),
        max(r.last_ts_ns for _, r in results),
        sum(r.cycles for _, r in results),
        gaps,
        sum(r.batches for _, r in results),
        sum(r.recording_s for _, r in results),
    )
