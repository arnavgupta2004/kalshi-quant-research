"""Complementary-outcome arbitrage.

Same-market YES/NO is *structurally impossible* on a valid Kalshi book: asks are derived from
the opposite bids (``ask_YES = 1 - bid_NO``), so ``ask_YES + ask_NO = 2 - bid_YES - bid_NO >= 1``
unless the book is crossed.  ``crossed_books`` is therefore not an opportunity scanner but an
**integrity invariant**: a hit means corrupted data (or a matching-engine transient), never a trade.

The exploitable "complementary" case is *cross-market*: two outcomes that jointly exhaust an
event (a two-outcome ``Partition``), e.g. "A wins" and "B wins".  Those come from
``two_outcome_specs``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from arbitrage.opportunity import RelationSpec
from market.contracts import Event, Market
from market.fees import series_of_ticker
from market.order_book import OrderBook
from market.relationships import Complement, RelationKind
from market.semantics import Analysis, EvidenceLevel


def crossed_books(books: Mapping[str, OrderBook]) -> list[str]:
    """Tickers whose book is crossed/locked (best YES bid >= best YES ask): an integrity alarm."""
    return sorted(t for t, b in books.items() if b.is_crossed())


def complement_specs(event: Event, markets: Iterable[Market]) -> list[RelationSpec]:
    return [
        RelationSpec(
            Complement(m.ticker),
            EvidenceLevel.PROVEN,
            event.event_ticker,
            series_of_ticker(m.ticker),
            m.close_time,
            ("YES + NO pay exactly $1; only a crossed book can violate this",),
        )
        for m in markets
    ]


def two_outcome_specs(
    event: Event, analysis: Analysis, markets: Iterable[Market]
) -> list[RelationSpec]:
    """Partitions of exactly two markets ("A wins" / "B wins")."""
    closes = {m.ticker: m.close_time for m in markets}
    out = []
    for a in analysis.assessments:
        r = a.relation
        if r.kind is RelationKind.PARTITION and len(r.tickers()) == 2:
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
