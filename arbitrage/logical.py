"""Logical / conditional arbitrage (spec s.7).

Covers implications, nested threshold ladders, and ladder rung = sum of range buckets.

    A implies B     X_A <= X_B        buy YES_B + NO_A  (pays >= $1)
    threshold chain each rung implies every looser rung (all pairs, see ``relationships.Chain``)
    union           X_W = sum X_p     a KXBTCD "above 68,699.99" rung vs the sum of KXBTC buckets

The claim that two threshold markets are nested is *proven* only if they settle on the same variable
at the same instant - established from their rules text in ``market.semantics``, never from titles.
"""

from __future__ import annotations

from collections.abc import Iterable

from arbitrage.opportunity import RelationSpec
from market.contracts import Event, Market
from market.fees import series_of_ticker
from market.relationships import RelationKind
from market.semantics import Analysis

KINDS = (RelationKind.IMPLIES, RelationKind.CHAIN, RelationKind.UNION)


def logical_specs(
    event: Event | None, analysis: Analysis, markets: Iterable[Market]
) -> list[RelationSpec]:
    closes = {m.ticker: m.close_time for m in markets}
    ev = event.event_ticker if event else "cross-event"
    out = []
    for a in analysis.assessments:
        r = a.relation
        if r.kind not in KINDS:
            continue
        out.append(
            RelationSpec(
                r,
                a.level,
                ev,
                series_of_ticker(r.tickers()[0]),
                max((closes[t] for t in r.tickers() if closes.get(t)), default=None),
                a.evidence,
                a.assumptions,
            )
        )
    return out
