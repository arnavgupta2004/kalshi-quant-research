"""Mutually-exclusive / exhaustive outcome arbitrage within an event (spec s.6).

For outcomes A_1..A_n of one event:

    mutually exclusive  sum X_i <= 1   violated when the YES **bids** sum to more than $1
                        (buy every NO: pays >= n-1, costs sum(1 - bid_i))
    exhaustive          sum X_i >= 1   violated when the YES **asks** sum to less than $1
                        (buy every YES: pays >= 1)
    partition           both

Which of these holds is *not* assumed: ``market.semantics`` supplies the relations with their
evidence levels (proven from strikes, exchange-declared, or empirical), and only relations at or
above the detector's ``min_level`` are priced.  Two-outcome partitions belong to
``complementary.two_outcome_specs``.
"""

from __future__ import annotations

from collections.abc import Iterable

from arbitrage.opportunity import RelationSpec
from market.contracts import Event, Market
from market.fees import series_of_ticker
from market.relationships import RelationKind
from market.semantics import Analysis

KINDS = (RelationKind.MUTUALLY_EXCLUSIVE, RelationKind.EXHAUSTIVE, RelationKind.PARTITION)


def exhaustive_specs(
    event: Event, analysis: Analysis, markets: Iterable[Market]
) -> list[RelationSpec]:
    closes = {m.ticker: m.close_time for m in markets}
    out = []
    for a in analysis.assessments:
        r = a.relation
        if r.kind not in KINDS:
            continue
        if r.kind is RelationKind.PARTITION and len(r.tickers()) == 2:
            continue  # handled as a complementary two-outcome event
        out.append(
            RelationSpec(
                r,
                a.level,
                event.event_ticker,
                series_of_ticker(r.tickers()[0]),
                max((closes[t] for t in r.tickers() if closes.get(t)), default=None),
                a.evidence,
                a.assumptions,
            )
        )
    return out
