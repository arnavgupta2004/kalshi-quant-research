"""Infer which logical relations really hold between Kalshi markets - and how sure we are.

Nothing here *assumes* a relation because titles look alike.  Every relation carries an
``EvidenceLevel`` and the explicit assumptions it rests on:

  PROVEN      follows from the settlement definitions alone (interval containment/disjointness
              on the real line, complementarity of YES/NO)
  LATTICE     proven *given* that the settlement variable is quantised to a grid (e.g. whole
              degrees, cents).  Exhaustiveness of "bucket" events needs this: buckets
              [80,81] and [82,83] leave (81,82) uncovered unless the value is an integer.
  DECLARED    asserted by the exchange (``event.mutually_exclusive``), not independently provable
  EMPIRICAL   supported by settled history of the same series (with a confidence bound)
  UNVERIFIED  no support

How markets are matched - *rules-text templates*.  ``strike_type`` alone is NOT enough: an MLB
spread event holds six ``greater`` markets - "Dodgers win by more than k runs" and "Giants win
by more than k runs" - that share strike types and values yet measure different variables.
Two markets are comparable only if their ``rules_primary`` text is identical once the
comparison clause ("is above 68599.99", "between 80-81°") is masked, they close at the same
instant, and their ``custom_strike`` matches.  The numeric ``Strike`` fields must also agree
with the comparison parsed from the text; a disagreement is reported as a conflict and the
market is excluded.

Completeness.  Exhaustiveness/partition claims require that *every* market of the event is in
hand (``event.market_tickers`` known and fully covered); mutual exclusivity of a subset is fine.
"""

from __future__ import annotations

import itertools
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import IntEnum

from market.contracts import Event, Market, Strike
from market.relationships import (
    Chain,
    Complement,
    Exhaustive,
    MutuallyExclusive,
    Partition,
    Relation,
    RelationKind,
    Union,
)

# ============================================================================= intervals


def dec(x: float | int | str | Decimal) -> Decimal:
    """Exact decimal for a strike.  Floats go through ``repr`` (68799.99, not 68799.98999...)."""
    return Decimal(repr(x)) if isinstance(x, float) else Decimal(x)


@dataclass(frozen=True, slots=True)
class Interval:
    """A subset of the real line; ``None`` bounds are infinite."""

    lo: Decimal | None
    lo_closed: bool
    hi: Decimal | None
    hi_closed: bool

    def __post_init__(self) -> None:  # normalise: an infinite end is never "closed"
        if self.lo is None and self.lo_closed:
            object.__setattr__(self, "lo_closed", False)
        if self.hi is None and self.hi_closed:
            object.__setattr__(self, "hi_closed", False)

    @property
    def is_empty(self) -> bool:
        if self.lo is None or self.hi is None:
            return False
        return self.lo > self.hi or (self.lo == self.hi and not (self.lo_closed and self.hi_closed))

    def subset_of(self, other: Interval) -> bool:
        if self.is_empty:
            return True
        return _lower_within(self, other) and _upper_within(self, other)

    def disjoint(self, other: Interval) -> bool:
        return _ends_before(self, other) or _ends_before(other, self)

    def __str__(self) -> str:
        lo = "-inf" if self.lo is None else f"{self.lo}"
        hi = "+inf" if self.hi is None else f"{self.hi}"
        return f"{'[' if self.lo_closed else '('}{lo}, {hi}{']' if self.hi_closed else ')'}"


def _lower_within(a: Interval, b: Interval) -> bool:
    if b.lo is None:
        return True
    if a.lo is None:
        return False
    return a.lo > b.lo or (a.lo == b.lo and (b.lo_closed or not a.lo_closed))


def _upper_within(a: Interval, b: Interval) -> bool:
    if b.hi is None:
        return True
    if a.hi is None:
        return False
    return a.hi < b.hi or (a.hi == b.hi and (b.hi_closed or not a.hi_closed))


def _ends_before(a: Interval, b: Interval) -> bool:
    """Every point of ``a`` lies strictly below every point of ``b``."""
    if a.hi is None or b.lo is None:
        return False
    return a.hi < b.lo or (a.hi == b.lo and not (a.hi_closed and b.lo_closed))


def strike_interval(strike: Strike) -> Interval | None:
    """The set of settlement values for which the market resolves YES, from the strike fields."""
    t, f, c = strike.strike_type, strike.floor, strike.cap
    if t == "greater" and f is not None:
        return Interval(dec(f), False, None, False)
    if t == "greater_or_equal" and f is not None:
        return Interval(dec(f), True, None, False)
    if t == "less" and c is not None:
        return Interval(None, False, dec(c), False)
    if t == "less_or_equal" and c is not None:
        return Interval(None, False, dec(c), True)
    if t == "between" and f is not None and c is not None:
        return Interval(dec(f), True, dec(c), True)  # inclusive both ends: ASSUMPTION A2 (docs)
    return None


# ----------------------------------------------------------------------------- lattice view
LatticeRange = tuple[int | None, int | None]  # inclusive integer index range; None = unbounded


def lattice_range(iv: Interval, step: Decimal) -> LatticeRange | None:
    """The grid points ``k*step`` inside ``iv`` as an inclusive index range (None if empty)."""
    lo = hi = None
    if iv.lo is not None:
        q = iv.lo / step
        lo = int(q.to_integral_value(ROUND_CEILING if iv.lo_closed else ROUND_FLOOR))
        if not iv.lo_closed:
            lo += 1
    if iv.hi is not None:
        q = iv.hi / step
        hi = int(q.to_integral_value(ROUND_FLOOR if iv.hi_closed else ROUND_CEILING))
        if not iv.hi_closed:
            hi -= 1
    if lo is not None and hi is not None and lo > hi:
        return None
    return lo, hi


def grid_step(values: Iterable[Decimal]) -> Decimal:
    """The coarsest grid that contains every strike: 10^-d for the largest decimal count d.

    Exception (assumption A3): strikes that are all whole or half-integers (x.5) describe an
    *integer-valued* variable - Kalshi uses x.5 thresholds on counts (games, goals, runs) precisely
    so that no outcome can tie the strike - so the grid is 1, not 0.5."""
    vals = list(values)
    d = 0
    for v in vals:
        exp = v.normalize().as_tuple().exponent
        if isinstance(exp, int) and exp < 0:
            d = max(d, -exp)
    if d == 1 and all((v * 2) == (v * 2).to_integral_value() for v in vals):
        return Decimal(1)
    return Decimal(1).scaleb(-d)


def _bounds(iv: Interval) -> list[Decimal]:
    return [b for b in (iv.lo, iv.hi) if b is not None]


# ============================================================================= rules text
_NUM = r"\$?-?\d[\d,]*(?:\.\d+)?(?:[KMB](?![A-Za-z]))?[%°]?"
_NUM_RE = re.compile(r"[$,%°]")
_SUFFIX = {"K": Decimal(10) ** 3, "M": Decimal(10) ** 6, "B": Decimal(10) ** 9}
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("between", re.compile(rf"between\s+({_NUM})\s*(?:-|–|—|and|to)\s*({_NUM})", re.I)),
    ("ge", re.compile(rf"(?:greater|more)\s+than\s+or\s+equal\s+to\s+({_NUM})", re.I)),
    ("le", re.compile(rf"(?:less|fewer)\s+than\s+or\s+equal\s+to\s+({_NUM})", re.I)),
    ("ge", re.compile(rf"at\s+least\s+({_NUM})", re.I)),
    ("le", re.compile(rf"at\s+most\s+({_NUM})", re.I)),
    ("gt", re.compile(rf"(?:strictly\s+)?(?:greater|more)\s+than\s+({_NUM})", re.I)),
    ("lt", re.compile(rf"(?:strictly\s+)?(?:less|fewer)\s+than\s+({_NUM})", re.I)),
    ("gt", re.compile(rf"(?:above|over)\s+({_NUM})", re.I)),
    ("lt", re.compile(rf"below\s+({_NUM})", re.I)),
    ("ge", re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\+(?!\w)")),  # "records 15+ receiving yards"
]
MASK = "«CMP»"


def _to_dec(token: str) -> Decimal:
    """``'10K'`` -> 10000, ``'4.5B'`` -> 4500000000, ``'$1,200'`` -> 1200."""
    t = _NUM_RE.sub("", token)
    mult = _SUFFIX.get(t[-1], None) if t and t[-1] in _SUFFIX else None
    return Decimal(t[:-1] if mult else t) * (mult or 1)


@dataclass(frozen=True, slots=True)
class ParsedRules:
    template: str  # rules text with the comparison clause masked
    comparator: str | None  # gt | ge | lt | le | between | None (no/ambiguous numeric clause)
    interval: Interval | None


def parse_rules(text: str) -> ParsedRules:
    """Mask the numeric comparison clause and recover the interval it describes.

    Returns ``comparator=None`` unless *exactly one* numeric clause is found - ambiguity is never
    resolved by guessing."""
    hits: list[tuple[str, re.Match[str]]] = []
    for kind, pat in _PATTERNS:
        for m in pat.finditer(text):
            if not any(m.start() < h.end() and h.start() < m.end() for _, h in hits):
                hits.append((kind, m))  # earlier (more specific) patterns win overlapping spans
    norm = re.sub(r"\s+", " ", text).strip()
    if len(hits) != 1:
        return ParsedRules(norm, None, None)
    kind, m = hits[0]
    template = re.sub(r"\s+", " ", text[: m.start()] + MASK + text[m.end() :]).strip()
    a = _to_dec(m.group(1))
    iv = {
        "gt": Interval(a, False, None, False),
        "ge": Interval(a, True, None, False),
        "lt": Interval(None, False, a, False),
        "le": Interval(None, False, a, True),
        "between": Interval(a, True, _to_dec(m.group(2)), True) if kind == "between" else None,
    }[kind]
    return ParsedRules(template, kind, iv)


# ============================================================================= evidence & results
class EvidenceLevel(IntEnum):
    UNVERIFIED = 0
    EMPIRICAL = 1
    DECLARED = 2
    LATTICE = 3
    PROVEN = 4


@dataclass(frozen=True, slots=True)
class Assessment:
    relation: Relation
    level: EvidenceLevel
    assumptions: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def usable(self, minimum: EvidenceLevel) -> bool:
        return self.level >= minimum


@dataclass
class Analysis:
    assessments: list[Assessment] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # data contradicts itself: investigate
    excluded: dict[str, str] = field(default_factory=dict)  # ticker -> why it is not numeric-typed

    def of_kind(self, kind) -> list[Assessment]:
        return [a for a in self.assessments if a.relation.kind is kind]

    def usable(self, minimum: EvidenceLevel) -> list[Relation]:
        return [a.relation for a in self.assessments if a.level >= minimum]


A_SAME_VARIABLE = (
    "markets settle on the same variable at the same instant (identical rules template, "
    "close time and custom_strike)"
)
A_INCLUSIVE = "'between a and b' includes both endpoints (assumption A2; corroborated empirically)"
A_LATTICE = (
    "the settlement variable is quantised to a grid of step {step}; "
    "a value inside a gap would resolve no market"
)
A_NORMAL = "every market resolves normally (void/scalar settlement can break the relation)"


@dataclass(frozen=True)
class EmpiricalPolicy:
    """When does settled history count as support?  (Values are policy, tuned nowhere.)"""

    min_events: int = 30
    max_violation_upper: float = 0.10  # one-sided 95% Clopper-Pearson bound


# ============================================================================= numeric markets
@dataclass(frozen=True)
class NumericMarket:
    market: Market
    interval: Interval  # taken from the exchange's strike fields
    family: tuple  # (template, close_time, custom_strike)
    #: True if the rules text describes exactly the same real-line set as the strike; False if they
    #: agree only on the shared grid (e.g. strike [26900, inf) vs text "above 26899.99").
    exact: bool = True
    step: Decimal | None = None  # the grid on which strike and text agree, when not exact


def numeric_view(m: Market) -> tuple[NumericMarket | None, str | None]:
    """(view, reason-if-excluded).  Numeric only if strike fields and rules text agree - exactly on
    the real line, or at least on the grid the two share (then ``exact`` is False and every
    relation built on this market is capped at LATTICE)."""
    strike_iv = strike_interval(m.strike)
    if strike_iv is None:
        return None, f"strike_type={m.strike.strike_type!r} has no numeric interval"
    parsed = parse_rules(m.rules.primary)
    if parsed.interval is None:
        return None, "rules text has no single parseable numeric comparison"
    custom = tuple(sorted((m.strike.custom or {}).items()))
    family = (parsed.template, m.close_time, custom)
    if parsed.interval == strike_iv:
        return NumericMarket(m, strike_iv, family), None
    step = grid_step(_bounds(strike_iv) + _bounds(parsed.interval))
    a, b = lattice_range(strike_iv, step), lattice_range(parsed.interval, step)
    if a is not None and a == b:
        return NumericMarket(m, strike_iv, family, exact=False, step=step), None
    return None, f"strike says {strike_iv} but rules text says {parsed.interval}"


def _narrowest_first(nms: list[NumericMarket], upward: bool) -> list[NumericMarket]:
    """Order nested half-lines by inclusion (each contained in the next)."""

    def key(n: NumericMarket):
        iv = n.interval
        if upward:  # (k, inf): a larger k is narrower; at equal k the open end is narrower
            return (-iv.lo, iv.lo_closed, n.market.ticker)
        return (iv.hi, not iv.hi_closed, n.market.ticker)  # (-inf, k): a smaller k is narrower

    return sorted(nms, key=key)


# ============================================================================= partitions
@dataclass(frozen=True)
class TileResult:
    disjoint: bool
    exhaustive_on_real_line: bool
    exhaustive_on_lattice: bool
    step: Decimal
    gaps: tuple[str, ...]  # human-readable uncovered stretches of the real line


def _by_lower_end(iv: Interval):
    return (0, Decimal(0)) if iv.lo is None else (1, iv.lo)


def analyse_tiling(nms: Sequence[NumericMarket]) -> TileResult:
    ivs = [n.interval for n in nms]
    disjoint = all(a.disjoint(b) for a, b in itertools.combinations(ivs, 2))
    step = grid_step(b for iv in ivs for b in _bounds(iv))
    if not disjoint:
        return TileResult(False, False, False, step, ())
    order = sorted(ivs, key=_by_lower_end)
    gaps: list[str] = []
    real_ok = order[0].lo is None and order[-1].hi is None
    for a, b in itertools.pairwise(order):
        if a.hi is None or b.lo is None:
            real_ok = False
            continue
        if not (a.hi == b.lo and (a.hi_closed or b.lo_closed)):
            real_ok = False
            gaps.append(f"{'(' if a.hi_closed else '['}{a.hi}, {b.lo}{')' if b.lo_closed else ']'}")
    ranges = [lattice_range(iv, step) for iv in order]
    lat_ok = _contiguous_cover(ranges)
    return TileResult(True, real_ok, lat_ok, step, tuple(gaps))


def _contiguous_cover(ranges: Sequence[LatticeRange | None]) -> bool:
    """Do these lattice ranges (sorted by lower end) tile all of Z with no gap and no overlap?"""
    if not ranges or any(r is None for r in ranges):
        return False
    if ranges[0][0] is not None or ranges[-1][1] is not None:
        return False
    return all(
        p[1] is not None and q[0] is not None and q[0] == p[1] + 1
        for p, q in itertools.pairwise(ranges)
    )


def exact_union(
    target: NumericMarket, tiles: Sequence[NumericMarket]
) -> tuple[list[NumericMarket], Decimal] | None:
    """Tiles inside ``target`` whose union is exactly ``target`` on the shared grid, else None."""
    inside = [t for t in tiles if t is not target and t.interval.subset_of(target.interval)]
    if len(inside) < 2:
        return None
    step = grid_step(b for n in (target, *inside) for b in _bounds(n.interval))
    want = lattice_range(target.interval, step)
    have = [(lattice_range(t.interval, step), t) for t in inside]
    if want is None or any(r is None for r, _ in have):
        return None
    have.sort(key=lambda rt: -1 if rt[0][0] is None else rt[0][0])
    ranges = [r for r, _ in have]
    if ranges[0][0] != want[0] or ranges[-1][1] != want[1]:
        return None
    if any(p[1] is None or q[0] is None or q[0] != p[1] + 1 for p, q in itertools.pairwise(ranges)):
        return None
    return [t for _, t in have], step


def _exact_on_real_line(target: Interval, tiles: Sequence[Interval]) -> bool:
    """Is the union of ``tiles`` (sorted, disjoint) *identical* to ``target`` on the real line?

    Requires the outer edges to match ``target`` exactly (including open/closed) and no gap
    between neighbours.  A ladder rung "above 87199.99" is (87199.99, inf) but the buckets tile
    [87200, inf): equal only on the cent grid, so that union is LATTICE, not PROVEN."""
    first, last = tiles[0], tiles[-1]
    if (first.lo, first.lo_closed) != (target.lo, target.lo_closed):
        return False
    if (last.hi, last.hi_closed) != (target.hi, target.hi_closed):
        return False
    return all(a.hi == b.lo and (a.hi_closed or b.lo_closed) for a, b in itertools.pairwise(tiles))


# ============================================================================= event analysis
def _complete(event: Event, markets: Sequence[Market]) -> bool:
    return bool(event.market_tickers) and set(event.market_tickers) == {m.ticker for m in markets}


def analyze_event(
    event: Event,
    markets: Sequence[Market],
    *,
    stats=None,
    policy: EmpiricalPolicy | None = None,
) -> Analysis:
    """All relations among the markets of one event, each with its evidence level."""
    policy = policy or EmpiricalPolicy()
    out = Analysis()
    markets = sorted(markets, key=lambda m: m.ticker)
    complete = _complete(event, markets)
    for m in markets:
        out.assessments.append(
            Assessment(
                Complement(m.ticker),
                EvidenceLevel.PROVEN,
                evidence=("YES + NO pay exactly $1 by construction",),
            )
        )

    fams: dict[tuple, list[NumericMarket]] = defaultdict(list)
    for m in markets:
        nm, why = numeric_view(m)
        if nm is not None:
            fams[nm.family].append(nm)
        elif m.strike.strike_type in (
            "greater",
            "greater_or_equal",
            "less",
            "less_or_equal",
            "between",
        ):
            out.excluded[m.ticker] = why or ""
            if why and "rules text says" in why:
                out.conflicts.append(f"{m.ticker}: {why}")

    for fam in fams.values():
        _numeric_family(fam, out, complete and len(fam) == len(markets))
        if event.mutually_exclusive and len(fam) >= 2 and not analyse_tiling(fam).disjoint:
            a, b = next(
                (a, b)
                for a, b in itertools.combinations(fam, 2)
                if not a.interval.disjoint(b.interval)
            )
            out.conflicts.append(
                f"{event.event_ticker}: flagged mutually exclusive but {a.market.ticker} "
                f"{a.interval} overlaps {b.market.ticker} {b.interval}"
            )

    _flag_relations(event, markets, out, complete, stats, policy)
    return out


def _numeric_family(fam: list[NumericMarket], out: Analysis, whole_event: bool) -> None:
    inexact = [n for n in fam if not n.exact]
    cap = EvidenceLevel.LATTICE if inexact else EvidenceLevel.PROVEN
    grid_note = (
        (f"strike and rules text agree only on the grid of step {inexact[0].step}",)
        if inexact
        else ()
    )
    ups = [n for n in fam if n.interval.hi is None and n.interval.lo is not None]
    downs = [n for n in fam if n.interval.lo is None and n.interval.hi is not None]
    for group, upward in ((ups, True), (downs, False)):
        if len(group) >= 2:
            order = _narrowest_first(group, upward)
            out.assessments.append(
                Assessment(
                    Chain([n.market.ticker for n in order]),
                    cap,
                    assumptions=(A_SAME_VARIABLE, *grid_note),
                    evidence=(
                        "half-lines are nested: "
                        + " ⊆ ".join(str(n.interval) for n in order[:4])
                        + (" ..." if len(order) > 4 else ""),
                    ),
                )
            )
    if len(fam) < 2:
        return
    tiling = analyse_tiling(fam)
    if not tiling.disjoint:
        return
    tickers = [n.market.ticker for n in fam]
    out.assessments.append(
        Assessment(
            MutuallyExclusive(tickers),
            cap,
            assumptions=(A_SAME_VARIABLE, *grid_note),
            evidence=("intervals are pairwise disjoint on the real line",),
        )
    )
    if not whole_event:
        return  # a subset of an event can be exclusive but says nothing about exhaustiveness
    if tiling.exhaustive_on_real_line:
        level, extra = EvidenceLevel.PROVEN, ()
    elif tiling.exhaustive_on_lattice:
        level, extra = EvidenceLevel.LATTICE, (A_LATTICE.format(step=tiling.step), A_INCLUSIVE)
    else:
        return
    out.assessments.append(
        Assessment(
            Partition(tickers),
            min(level, cap),
            assumptions=(A_SAME_VARIABLE, *extra, *grid_note),
            evidence=(
                f"tiles cover (-inf, +inf); real-line gaps: {list(tiling.gaps)[:3] or 'none'}",
            ),
        )
    )


def _flag_relations(event, markets, out: Analysis, complete: bool, stats, policy) -> None:
    """The exchange's ``mutually_exclusive`` flag, cross-checked against proofs and history."""
    if not event.mutually_exclusive or len(markets) < 2:
        return
    tickers = [m.ticker for m in markets]
    proven_me = {
        frozenset(a.relation.markets)
        for a in out.of_kind(RelationKind.MUTUALLY_EXCLUSIVE)
        if a.level is EvidenceLevel.PROVEN
    }
    if frozenset(tickers) in proven_me:  # already proven from strikes: the flag merely agrees
        return
    me_evidence = [f"exchange flag mutually_exclusive=True on {event.event_ticker}"]
    me_level = EvidenceLevel.DECLARED
    if out.conflicts:  # the event's own data contradicts itself: trust none of its declarations
        me_level = EvidenceLevel.UNVERIFIED
        me_evidence.append(
            f"data conflicts in this event ({len(out.conflicts)}): declarations distrusted"
        )
    series = event.series_ticker or event.event_ticker.split("-")[0]
    if stats is not None and (s := stats.get(series)):
        ub = s.multi_yes_upper()
        me_evidence.append(
            f"history: {s.multi_yes}/{s.n_clean} settled events of {series} had >=2 YES "
            f"(95% upper bound {ub:.1%})"
        )
        if s.multi_yes > 0:
            out.conflicts.append(
                f"{event.event_ticker}: flag says ME but {s.multi_yes}/{s.n_clean} settled "
                f"events of {series} had >=2 YES"
            )
            me_level = EvidenceLevel.UNVERIFIED
    out.assessments.append(
        Assessment(
            MutuallyExclusive(tickers),
            me_level,
            assumptions=(A_NORMAL,),
            evidence=tuple(me_evidence),
        )
    )
    if not complete:
        return  # never claim exhaustiveness for an event we do not fully hold
    cover_level, cover_ev = EvidenceLevel.UNVERIFIED, ["no settled history available"]
    if stats is not None and (s := stats.get(series)):
        ub = s.zero_yes_upper()
        cover_ev = [
            f"history: {s.zero_yes}/{s.n_clean} settled events of {series} had no YES "
            f"(95% upper bound {ub:.1%})"
        ]
        if (
            s.n_clean >= policy.min_events
            and ub <= policy.max_violation_upper
            and not out.conflicts
        ):
            cover_level = EvidenceLevel.EMPIRICAL
    out.assessments.append(
        Assessment(
            Exhaustive(tickers), cover_level, assumptions=(A_NORMAL,), evidence=tuple(cover_ev)
        )
    )
    out.assessments.append(
        Assessment(
            Partition(tickers),
            min(me_level, cover_level),
            assumptions=(A_NORMAL,),
            evidence=tuple(me_evidence + cover_ev),
        )
    )


# ============================================================================= cross-event
def find_unions(events: Sequence[tuple[Event, Sequence[Market]]]) -> Analysis:
    """Ladder market == exact union of range buckets, when both quote the same variable.

    Example: 'BTC above 68,699.99 at 4 AM' (one KXBTCD market) equals the union of every KXBTC
    bucket from '68,700 to 68,799.99' upward, including the open-ended top tail."""
    out = Analysis()
    fams: dict[tuple, list[NumericMarket]] = defaultdict(list)
    per_event_groups: dict[tuple, list[list[NumericMarket]]] = defaultdict(list)
    for _, ms in events:
        by_fam: dict[tuple, list[NumericMarket]] = defaultdict(list)
        for m in ms:
            nm, _ = numeric_view(m)
            if nm is not None:
                by_fam[nm.family].append(nm)
                fams[nm.family].append(nm)
        for fam_key, grp in by_fam.items():
            if len(grp) >= 2 and analyse_tiling(grp).disjoint:
                per_event_groups[fam_key].append(grp)
    for fam_key, groups in per_event_groups.items():
        for grp in groups:
            grp_tickers = {n.market.ticker for n in grp}
            for target in fams[fam_key]:
                if target.market.ticker in grp_tickers:
                    continue
                if target.interval.lo is not None and target.interval.hi is not None:
                    continue  # only half-lines: they are the ladder side
                res = exact_union(target, grp)
                if res is None:
                    continue
                tiles, step = res
                level = EvidenceLevel.PROVEN
                extra = (A_SAME_VARIABLE,)
                if not all(n.exact for n in (target, *tiles)) or not _exact_on_real_line(
                    target.interval, [t.interval for t in tiles]
                ):
                    level, extra = (
                        EvidenceLevel.LATTICE,
                        (A_SAME_VARIABLE, A_LATTICE.format(step=step), A_INCLUSIVE),
                    )
                out.assessments.append(
                    Assessment(
                        Union(
                            [t.market.ticker for t in tiles], target.market.ticker, disjoint=True
                        ),
                        level,
                        assumptions=extra,
                        evidence=(
                            f"{target.market.ticker} = {target.interval} is exactly tiled by "
                            f"{len(tiles)} disjoint buckets",
                        ),
                    )
                )
    return out
