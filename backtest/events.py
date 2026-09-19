"""Events the engine replays, and the rule that orders them.

Every event carries ``ts_ns`` = **the moment the information became available to a live
system**, not when the underlying fact happened.  That distinction is what makes
look-ahead impossible by construction:

  book snapshot   ``recv_ts_ns`` - when *we* received it (REST polls arrive ~0.4 s after the
                  state they describe, and we only ever know it from receipt onward)
  trade           exchange time + ``trade_delay`` (a backfilled trade would have reached a
                  live feed a moment after it printed)
  market close    the actual close time - a live system is told when a market closes, not before
  settlement      settlement time + delay - the outcome is unknowable earlier

Events at the same timestamp are processed in a fixed, documented priority so results are
deterministic and never depend on iteration order:

  SETTLEMENT < CLOSE < BOOK < TRADE < ORDER_ARRIVAL < CANCEL_ARRIVAL < FILL_NOTICE < WAKE

Rationale: state changes that make trading impossible first; then data (an order arriving at
the same instant as a book update sees the update); then our own order traffic; timers last.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from market.contracts import Side, Trade
from market.units import Price, Qty


class Priority(IntEnum):
    SETTLEMENT = 0
    CLOSE = 1
    BOOK = 2
    TRADE = 3
    ORDER_ARRIVAL = 4
    CANCEL_ARRIVAL = 5
    FILL_NOTICE = 6
    WAKE = 7


@dataclass(frozen=True, slots=True)
class BookUpdate:
    """A full-depth snapshot, levels best-first: ``((price_ticks, qty_centi), ...)``."""

    ts_ns: int
    ticker: str
    yes_bids: tuple[tuple[Price, Qty], ...]
    no_bids: tuple[tuple[Price, Qty], ...]
    req_ts_ns: int | None = None
    priority = Priority.BOOK


@dataclass(frozen=True, slots=True)
class BookConfirm:
    """The recorder polled every market at ``ts_ns`` and found each book *unchanged*.

    Recorders store a book only when it changes (plus a heartbeat), so the age of the last stored
    snapshot says nothing about freshness: a book unchanged for ten minutes is still perfectly
    current.  Liveness comes from the poll log - and its *absence* (a 21-minute outage was observed
    in the real recording) is what makes a book genuinely stale.  This event carries no levels."""

    ts_ns: int
    ticker: str = ""  # empty: applies to every market
    priority = Priority.BOOK


@dataclass(frozen=True, slots=True)
class TradeTick:
    ts_ns: int
    ticker: str
    trade: Trade
    priority = Priority.TRADE


@dataclass(frozen=True, slots=True)
class MarketClose:
    """Trading has stopped.  (The *time* of an early close is itself information: it is revealed
    here and never earlier.)"""

    ts_ns: int
    ticker: str
    priority = Priority.CLOSE


@dataclass(frozen=True, slots=True)
class Settlement:
    """The outcome: YES pays ``value`` ticks (0..10,000; fractional for scalar/void markets)."""

    ts_ns: int
    ticker: str
    value: Price
    priority = Priority.SETTLEMENT


@dataclass(frozen=True, slots=True)
class Wake:
    ts_ns: int
    tag: str = ""
    ticker: str = ""
    priority = Priority.WAKE


FeedEvent = BookUpdate | BookConfirm | TradeTick | MarketClose | Settlement


@dataclass(frozen=True, slots=True)
class Fill:
    """A fill notice delivered to the strategy (after acknowledgement latency)."""

    ts_ns: int  # when the *strategy* is told
    exec_ts_ns: int  # when it actually executed at the exchange
    order_id: int
    ticker: str
    side: Side
    price: Price  # of ``side`` (ticks)
    qty: Qty
    fee_micro: int
    liquidity: str  # "taker" | "maker"
    tag: str = ""
    priority = Priority.FILL_NOTICE


@dataclass(frozen=True, slots=True)
class OrderRejected:
    ts_ns: int
    order_id: int
    reason: str
    tag: str = ""
    priority = Priority.FILL_NOTICE
