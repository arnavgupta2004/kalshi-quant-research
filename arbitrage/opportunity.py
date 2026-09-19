"""The record of one detected pricing violation, and how far it survives.

Classification (spec s.8) - assigned by how the opportunity fares against reality:

    THEORETICAL     displayed prices violate a proven constraint, but there is not even the
                    minimum tradable size at those prices
    UNPROFITABLE    tradable, but fees and slippage consume the whole edge at every size
    PARTIAL         net-profitable, but the profit-maximising size is below the target size
    EXECUTABLE      the profit-maximising size (where marginal contracts stop paying) hits target
    EXPIRED         edge gone before the order could reach the exchange   (latency: Stage 6)

``Funnel`` counts how many constraints survive each filtering stage - the spec's central
ablation: displayed -> liquidity -> fees -> slippage -> executable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from arbitrage.execution import PricedBundle
from market.contracts import Side
from market.relationships import Constraint, Relation, World
from market.semantics import EvidenceLevel
from market.units import PRICE_SCALE


class Classification(StrEnum):
    THEORETICAL = "theoretical"
    UNPROFITABLE = "unprofitable_after_costs"
    PARTIAL = "partially_executable"
    EXECUTABLE = "executable"
    EXPIRED = "expired_before_execution"


@dataclass(frozen=True)
class RelationSpec:
    """A relation that is a candidate for arbitrage, with the provenance of the claim."""

    relation: Relation
    level: EvidenceLevel
    event_ticker: str
    series: str
    close_time: datetime | None = None
    evidence: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class Opportunity:
    ts_ns: int
    spec: RelationSpec
    constraint: Constraint
    classification: Classification
    top_margin: int  # ticks per contract-bundle at the best displayed asks
    top_qty: int  # centi-bundles available at those best prices only
    liquid: bool
    profitable_after_fees_top: bool  # net > 0 at minimum size, best prices, fees included
    profitable_after_depth: bool  # net > 0 at the profit-maximising size (slippage included)
    priced: PricedBundle | None  # the recommended bundle (or minimum-lot context if unprofitable)
    breakeven_bundles: (
        int  # largest size with total net > 0; the profit-maximising size is `priced`
    )
    target_bundles: int
    fees_known: bool
    days_to_close: float | None = None

    # ---- the accounting decomposition:  net = gross - slippage - fees   (exact, in µ$)
    @property
    def gross_micro(self) -> int | None:
        return None if self.priced is None else self.priced.gross_micro

    @property
    def slippage_micro(self) -> int | None:
        return None if self.priced is None else self.priced.slippage_micro

    @property
    def fee_micro(self) -> int | None:
        return None if self.priced is None else self.priced.fee_micro

    @property
    def net_micro(self) -> int | None:
        return None if self.priced is None else self.priced.net_micro

    @property
    def roi(self) -> float | None:
        """Net profit per dollar committed (locked until settlement)."""
        if self.priced is None or not self.priced.cost_micro:
            return None
        return self.priced.net_micro / (self.priced.cost_micro + self.priced.fee_micro)

    @property
    def annualized_roi(self) -> float | None:
        r = self.roi
        if r is None or not self.days_to_close or self.days_to_close <= 0:
            return None
        return r * 365.0 / self.days_to_close

    def settle_micro(self, world: World) -> int | None:
        """Realised P&L (µ$) if the markets settle as ``world`` (ticker -> 1/0), fees included.

        The guarantee (tested): this is >= ``net_micro`` in every admissible world."""
        if self.priced is None:
            return None
        payoff = 0
        for leg in self.priced.legs:
            yes = world[leg.contract.ticker]
            wins = yes if leg.contract.side is Side.YES else 1 - yes
            payoff += wins * PRICE_SCALE * leg.qty
        return payoff - self.priced.cost_micro - self.priced.fee_micro


@dataclass
class Funnel:
    """How many constraints survive each filter (spec s.21).  Cumulative: every stage is a subset
    of the previous one."""

    constraints: int = 0  # distinct constraints examined
    duplicates: int = 0  # identical trades reached through another relation (counted once)
    unpriceable: int = 0  # a leg had nothing to buy
    pruned: int = (
        0  # constraints proven non-violated by the lossless ladder pre-filter (not priced)
    )
    priced: int = 0
    displayed: int = 0  # top-of-book prices violate the constraint
    liquid: int = 0  # ... and at least the minimum size is available at those prices
    after_fees: int = 0  # ... and still profitable after fees (best prices, minimum size)
    after_slippage: int = 0  # ... and after walking the book to the profit-maximising size
    executable: int = 0  # ... and the profit-maximising size reaches the target size
    fee_assumed: int = 0  # of the displayed ones: fee metadata missing, standard fee assumed
    by_class: dict[str, int] = field(default_factory=dict)
    #: largest margin per relation kind among *priced* constraints (negative: not yet violated)
    best_margin: dict[str, int] = field(default_factory=dict)

    STAGES = ("priced", "displayed", "liquid", "after_fees", "after_slippage", "executable")

    def add(self, other: Funnel) -> None:
        for f in (
            "constraints",
            "unpriceable",
            "priced",
            "displayed",
            "liquid",
            "after_fees",
            "after_slippage",
            "executable",
            "fee_assumed",
        ):
            setattr(self, f, getattr(self, f) + getattr(other, f))
        for k, v in other.by_class.items():
            self.by_class[k] = self.by_class.get(k, 0) + v
        for k, m in other.best_margin.items():
            self.best_margin[k] = max(m, self.best_margin.get(k, m))

    def rows(self) -> list[tuple[str, int]]:
        return [(s, getattr(self, s)) for s in self.STAGES]
