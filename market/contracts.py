"""Normalised, exchange-agnostic representation of events, markets and contracts.

Vocabulary (see ``docs/contract_semantics.md``):

* **Event**    - a real-world question grouping one or more markets.
* **Market**   - one binary question ("Will X exceed 24.5?").  Kalshi calls this a market;
                 it is the unit that has an order book, rules and a settlement.
* **Contract** - a claim on one *side* of a market.  ``ContractRef(ticker, YES)`` pays the
                 market's settlement value; ``ContractRef(ticker, NO)`` pays ``$1 - value``.
* **Outcome**  - the settled result (``yes`` / ``no`` / ``scalar``).

Settlement is **not always {0, 1}**: markets can settle ``scalar`` at a fractional value
(observed on live data, e.g. 0.38), and rules can void a market at $0.50.  The payoff
model therefore carries a settlement value in ``[0, 1]`` rather than a boolean.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from market.units import PRICE_SCALE, Price, Qty, complement


class Side(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def opposite(self) -> Side:
        return Side.NO if self is Side.YES else Side.YES


class MarketStatus(StrEnum):
    INITIALIZED = "initialized"
    INACTIVE = "inactive"
    ACTIVE = "active"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    AMENDED = "amended"
    FINALIZED = "finalized"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, value: str | None) -> MarketStatus:
        try:
            return cls((value or "").lower())
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_tradeable(self) -> bool:
        return self is MarketStatus.ACTIVE

    @property
    def is_terminal(self) -> bool:
        """Outcome is final; no further re-resolution expected."""
        return self is MarketStatus.FINALIZED


class SettlementResult(StrEnum):
    YES = "yes"
    NO = "no"
    SCALAR = "scalar"  # settles at a fractional value in (0, 1)
    UNSETTLED = ""

    @classmethod
    def parse(cls, value: str | None) -> SettlementResult:
        v = (value or "").lower()
        try:
            return cls(v)
        except ValueError as exc:
            raise ValueError(f"unrecognised settlement result {value!r}") from exc


@dataclass(frozen=True, slots=True)
class PriceRange:
    """One band of the tick grid: prices in ``[start, end]`` move in increments of ``step``."""

    start: Price
    end: Price
    step: Price


@dataclass(frozen=True, slots=True)
class Strike:
    """Threshold semantics needed later to relate markets logically (Stage 3)."""

    strike_type: str | None = None  # e.g. "greater", "less", "between", "structured"
    floor: float | None = None
    cap: float | None = None
    functional: str | None = None
    custom: dict[str, Any] | None = field(default=None, hash=False)


@dataclass(frozen=True, slots=True)
class Rules:
    primary: str = ""
    secondary: str = ""
    early_close_condition: str | None = None
    can_close_early: bool = False


@dataclass(frozen=True, slots=True)
class SettlementSource:
    name: str
    url: str | None = None


@dataclass(frozen=True, slots=True)
class Event:
    event_ticker: str
    series_ticker: str | None
    title: str
    sub_title: str
    category: str | None
    mutually_exclusive: bool
    settlement_sources: tuple[SettlementSource, ...] = ()
    strike_period: str | None = None
    collateral_return_type: str | None = None
    exchange_index: int | None = None
    market_tickers: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)


@dataclass(frozen=True, slots=True)
class Market:
    """A single binary market (== the pair of YES/NO contracts on one question)."""

    ticker: str
    event_ticker: str
    series_ticker: str | None
    category: str | None
    title: str
    yes_sub_title: str
    no_sub_title: str
    market_type: str
    status: MarketStatus
    strike: Strike
    rules: Rules
    open_time: datetime | None
    close_time: datetime | None
    expected_expiration_time: datetime | None
    expiration_time: datetime | None
    result: SettlementResult
    settlement_ts: datetime | None
    #: YES-contract settlement value in ticks, ``None`` until determined.
    settlement_value: Price | None
    price_level_structure: str | None
    price_ranges: tuple[PriceRange, ...]
    exchange_index: int | None
    is_multivariate: bool
    updated_time: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False, hash=False)

    @property
    def is_settled(self) -> bool:
        return self.settlement_value is not None

    def payoff(self, side: Side) -> Price | None:
        """Terminal value in ticks of one contract on ``side``; ``None`` if unsettled."""
        if self.settlement_value is None:
            return None
        return self.settlement_value if side is Side.YES else complement(self.settlement_value)


@dataclass(frozen=True, slots=True)
class ContractRef:
    """A tradeable claim: one side of one market."""

    ticker: str
    side: Side

    @property
    def complement(self) -> ContractRef:
        return ContractRef(self.ticker, self.side.opposite)

    def __str__(self) -> str:
        return f"{self.ticker}:{self.side.value}"


@dataclass(frozen=True, slots=True)
class Trade:
    trade_id: str
    ticker: str
    yes_price: Price
    no_price: Price
    count: Qty
    #: side of the contract the *taker* acquired
    taker_side: Side | None
    #: "bid" / "ask" - which book side the taker crossed (bid == YES, ask == NO per the docs)
    taker_book_side: str | None
    created_time: datetime
    is_block_trade: bool = False

    def __post_init__(self) -> None:
        if self.yes_price + self.no_price != PRICE_SCALE:
            raise ValueError(
                f"trade {self.trade_id}: yes+no prices {self.yes_price}+{self.no_price} != $1"
            )
