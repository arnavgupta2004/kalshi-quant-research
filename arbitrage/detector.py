"""Constraint violation -> classified, sized, fee-adjusted opportunity.

    observed books
        |  Constraint.check                    top-of-book margin           ("displayed")
        |  top quantity >= minimum size        liquidity
        |  exact fees at best prices           fees
        |  walk the book, optimise the size    slippage / depth
        |  size >= target                      executable
        v
    Opportunity (with the full gross -> slippage -> fees -> net decomposition)

The detector only prices constraints of relations it is *given*; which relations are trustworthy is
decided upstream (``market.semantics`` evidence levels) and enforced here by ``min_level``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from arbitrage.execution import Books, optimize, price_bundle
from arbitrage.opportunity import Classification, Funnel, Opportunity, RelationSpec
from market.fees import FeeBook
from market.relationships import Constraint, ConstraintCheck, asks_from_books
from market.semantics import EvidenceLevel
from market.timeutil import from_epoch_ns


@dataclass(frozen=True)
class DetectorParams:
    fees: FeeBook = field(default_factory=FeeBook)
    lot: int = 100  # trade in whole contracts (centi-contracts)
    min_qty: int = 100  # smallest size worth trading: 1 contract
    target: int = 10_000  # size at which an opportunity counts as fully executable: 100 contracts
    #: never size above this many centi-contracts (None: as deep as the books allow).  A cap at or
    #: above ``target`` leaves every classification unchanged and only bounds the reported size/P&L.
    max_size: int | None = None
    min_level: EvidenceLevel = EvidenceLevel.DECLARED


def detect(
    specs: Iterable[RelationSpec], books: Books, params: DetectorParams, *, ts_ns: int = 0
) -> tuple[list[Opportunity], Funnel]:
    ask = asks_from_books(books)
    funnel = Funnel()
    found: list[Opportunity] = []
    now = from_epoch_ns(ts_ns) if ts_ns else None
    seen: set[tuple] = set()
    # Strongest evidence first: the SAME trade appears inside several relations (the exclusivity
    # constraint sits in the declared relation, the proven one and the partition).  It is one
    # opportunity, credited to the best-supported claim, never counted two or three times.
    for spec in sorted(specs, key=lambda sp: -sp.level):
        if spec.level < params.min_level:
            continue
        rel = spec.relation
        if hasattr(rel, "candidate_constraints"):  # lossless pre-filter (nested ladders)
            candidates = rel.candidate_constraints(ask)
            funnel.pruned += rel.n_constraints() - len(candidates)
        else:
            candidates = rel.constraints()
        for con in candidates:
            key = (tuple(sorted((str(leg.contract), leg.qty) for leg in con.legs)), con.bound)
            if key in seen:
                funnel.duplicates += 1
                continue
            seen.add(key)
            funnel.constraints += 1
            chk = con.check(ask)
            if chk is None:
                funnel.unpriceable += 1
                continue
            funnel.priced += 1
            kind = con.kind.value
            funnel.best_margin[kind] = max(chk.margin, funnel.best_margin.get(kind, chk.margin))
            if chk.margin <= 0:
                continue
            funnel.displayed += 1
            op = _evaluate(spec, con, chk, books, params, funnel, ts_ns, now)
            funnel.by_class[op.classification.value] = (
                funnel.by_class.get(op.classification.value, 0) + 1
            )
            found.append(op)
    return found, funnel


def _evaluate(
    spec: RelationSpec,
    con: Constraint,
    chk: ConstraintCheck,
    books: Books,
    params: DetectorParams,
    funnel: Funnel,
    ts_ns: int,
    now: datetime | None,
) -> Opportunity:
    """Take one displayed violation through liquidity -> fees -> slippage -> size."""
    fees_known = all(params.fees.for_ticker(leg.contract.ticker).known for leg in con.legs)
    funnel.fee_assumed += not fees_known
    days = None
    if now is not None and spec.close_time is not None:
        days = (spec.close_time - now).total_seconds() / 86_400

    def build(cls, priced, liquid, fee_ok, depth_ok, breakeven) -> Opportunity:
        return Opportunity(
            ts_ns=ts_ns,
            spec=spec,
            constraint=con,
            classification=cls,
            top_margin=chk.margin,
            top_qty=chk.top_qty,
            liquid=liquid,
            profitable_after_fees_top=fee_ok,
            profitable_after_depth=depth_ok,
            priced=priced,
            breakeven_bundles=breakeven,
            target_bundles=params.target,
            fees_known=fees_known,
            days_to_close=days,
        )

    if chk.top_qty < params.min_qty:  # nothing meaningful to trade at the displayed price
        return build(Classification.THEORETICAL, None, False, False, False, 0)
    funnel.liquid += 1
    top = price_bundle(con, books, params.min_qty, params.fees)  # all fills at the best prices
    fee_ok = top is not None and top.net_micro > 0
    funnel.after_fees += fee_ok
    sized = optimize(con, books, params.fees, lot=params.lot, cap=params.max_size)
    if sized is None or sized.best.net_micro <= 0:
        return build(Classification.UNPROFITABLE, top, True, fee_ok, False, 0)
    funnel.after_slippage += 1
    biggest = sized.breakeven.bundles
    full = sized.best.bundles >= params.target  # the size a trader would actually send
    funnel.executable += full
    cls = Classification.EXECUTABLE if full else Classification.PARTIAL
    return build(cls, sized.best, True, fee_ok, True, biggest)


def merge_funnels(funnels: Sequence[Funnel]) -> Funnel:
    total = Funnel()
    for f in funnels:
        total.add(f)
    return total


def build_specs(bundles, *, stats=None, include_unions: bool = True) -> list[RelationSpec]:
    """Every candidate relation for a set of events: ``bundles`` = [(Event, [Market])]."""
    from arbitrage.complementary import complement_specs, two_outcome_specs
    from arbitrage.exhaustive import exhaustive_specs
    from arbitrage.logical import logical_specs
    from market.semantics import analyze_event, find_unions

    bundles = list(bundles)
    specs: list[RelationSpec] = []
    for event, markets in bundles:
        analysis = analyze_event(event, markets, stats=stats)
        specs += complement_specs(event, markets)
        specs += two_outcome_specs(event, analysis, markets)
        specs += exhaustive_specs(event, analysis, markets)
        specs += logical_specs(event, analysis, markets)
    if include_unions:
        all_markets = [m for _, ms in bundles for m in ms]
        specs += logical_specs(None, find_unions(bundles), all_markets)
    return specs
