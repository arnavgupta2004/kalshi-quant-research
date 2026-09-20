"""Binary-contract inventory risk (spec s.13): what a position can lose or make at settlement, and
the limits that keep it bounded.

A market's position is ``yes`` YES contracts and ``no`` NO contracts (the ledger nets pairs, so at
most one is non-zero) bought for a total cost.  At settlement the market pays ``X`` per YES and
``1 - X`` per NO with ``X`` in {0, 1}, so the *whole* future of a position is two numbers::

    pnl if YES wins = yes * $1 - cost          pnl if NO wins = no * $1 - cost

    worst case = min of the two    best case = max of the two
    expected   = p * pnl_if_yes + (1 - p) * pnl_if_no

The worst case is exact and needs no model - it is the number position limits should be written in.
It is NOT the mark-to-market (which moves with the book and says nothing about settlement).

**Event exposure.**  Markets of one event are not independent: in a mutually exclusive, exhaustive
event at most one YES wins, so a long-YES book across all outcomes cannot lose on all of them.
``event_worst_case`` takes the minimum of the portfolio's settlement P&L over the *admissible
worlds* the Stage 3 relations allow; where no relation is known it falls back to the conservative
sum of per-market worst cases.  Portfolio exposure is the sum of event worst cases (events treated
as independent, which only over-states the loss).

**Limits** (``RiskLimits``): per-market net position, per-event and portfolio worst-case loss, a
drawdown limit from the equity peak, a stop-quoting horizon before scheduled expiry, and a **kill-
switch** that, once tripped, cancels every resting order and stops quoting for good.  Inventory skew
lives in the quoting model (``market_making.quoting``), not here: limits are hard constraints, skew
is the soft pressure toward them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from backtest.portfolio import Position
from market.units import PRICE_SCALE

MICRO = 1_000_000


@dataclass(frozen=True)
class Exposure:
    """Settlement outcomes of one market's position, in µ$."""

    net_yes: int  # centi-contracts (long YES > 0, long NO < 0)
    cost: int
    pnl_if_yes: int
    pnl_if_no: int

    @property
    def worst(self) -> int:
        return min(self.pnl_if_yes, self.pnl_if_no)

    @property
    def best(self) -> int:
        return max(self.pnl_if_yes, self.pnl_if_no)

    def expected(self, p: float) -> float:
        return p * self.pnl_if_yes + (1 - p) * self.pnl_if_no


def exposure(pos: Position) -> Exposure:
    cost = pos.yes_cost + pos.no_cost
    return Exposure(pos.net_yes, cost, pos.yes * PRICE_SCALE - cost, pos.no * PRICE_SCALE - cost)


def event_worst_case(
    positions: Mapping[str, Position], worlds: Sequence[Mapping[str, int]] | None = None
) -> int:
    """Worst settlement P&L (µ$, negative = loss) of one event's positions.

    ``worlds`` are the admissible outcomes (ticker -> 1 if YES wins).  Tickers missing from a world
    are treated as unconstrained by it (both outcomes considered).  With no worlds, per-market worst
    cases are summed - always at least as pessimistic."""
    if not positions:
        return 0
    if not worlds:
        return sum(exposure(p).worst for p in positions.values())
    worst = None
    for w in worlds:
        total = 0
        for t, pos in positions.items():
            e = exposure(pos)
            if t in w:
                total += e.pnl_if_yes if w[t] else e.pnl_if_no
            else:
                total += e.worst
        worst = total if worst is None else min(worst, total)
    return worst


def partition_worlds(tickers: Iterable[str]) -> list[dict[str, int]]:
    """Worlds of a mutually exclusive AND exhaustive event: exactly one outcome is YES."""
    ts = list(tickers)
    return [{t: int(t == winner) for t in ts} for winner in ts]


@dataclass(frozen=True)
class RiskLimits:
    max_position_contracts: float = 50.0  # |net YES| per market
    max_event_loss_usd: float = 150.0  # worst-case settlement loss per event
    max_portfolio_loss_usd: float = 1_000.0  # sum of event worst cases
    max_drawdown_usd: float = 300.0  # equity below its running peak: trips the kill-switch
    stop_hours_before_expiry: float = 0.25  # no new quotes this close to the scheduled end
    max_book_age_s: float = 30.0  # a quote needs a book this fresh
    min_price: float = 0.03  # near-certain markets: adverse selection dominates, edge is ticks
    max_price: float = 0.97


@dataclass
class RiskState:
    killed: bool = False
    reason: str = ""
    killed_at_ns: int | None = None
    peak_equity: int | None = None
    events: list[tuple[int, str]] = field(default_factory=list)  # (ts_ns, what) - an audit trail


class RiskMonitor:
    """Tracks worst-case exposure, drawdown and the kill-switch for a set of markets."""

    def __init__(
        self,
        limits: RiskLimits,
        event_of: Mapping[str, str],
        worlds: Mapping[str, Sequence[Mapping[str, int]]] | None = None,
    ) -> None:
        self.limits = limits
        self.event_of = dict(event_of)
        self.worlds = dict(worlds or {})
        self.state = RiskState()
        self._by_event: dict[str, list[str]] = {}
        for t, e in self.event_of.items():
            self._by_event.setdefault(e, []).append(t)

    # ---------------------------------------------------------------- exposure
    def event_loss(self, event: str, positions: Mapping[str, Position]) -> float:
        """Worst-case settlement loss of the event in dollars (>= 0)."""
        held = {
            t: positions[t]
            for t in self._by_event.get(event, ())
            if t in positions and not positions[t].flat
        }
        return max(0.0, -event_worst_case(held, self.worlds.get(event)) / MICRO)

    def portfolio_loss(self, positions: Mapping[str, Position]) -> float:
        events = {
            self.event_of[t] for t, p in positions.items() if not p.flat and t in self.event_of
        }
        return sum(self.event_loss(e, positions) for e in events)

    def headroom(
        self,
        positions: Mapping[str, Position],
        ticker: str,
        side_yes: bool,
        price: float,
        contracts: float,
    ) -> float:
        """Largest ``<= contracts`` that can be bought on this side without breaching a limit,
        judged by the worst case AFTER a hypothetical fill at ``price`` (dollars per contract of
        that side)."""
        lim = self.limits
        pos = positions.get(ticker, Position())
        q = pos.net_yes / 100
        room = (lim.max_position_contracts - q) if side_yes else (lim.max_position_contracts + q)
        n = max(0.0, min(contracts, room))
        event = self.event_of.get(ticker)
        if n <= 0 or event is None:
            return n

        def loss_after(k: float) -> tuple[float, float]:
            p2 = dict(positions)
            hyp = Position(pos.yes, pos.yes_cost, pos.no, pos.no_cost)
            centi, cost = int(round(k * 100)), int(round(price * PRICE_SCALE * k * 100))
            if side_yes:
                hyp.yes += centi
                hyp.yes_cost += cost
            else:
                hyp.no += centi
                hyp.no_cost += cost
            p2[ticker] = hyp
            return self.event_loss(event, p2), self.portfolio_loss(p2)

        for _ in range(12):  # loss is monotone in size: bisect down to a size that fits
            ev, pf = loss_after(n)
            if ev <= lim.max_event_loss_usd and pf <= lim.max_portfolio_loss_usd:
                return n
            n = float(int(n / 2))
            if n < 1:
                return 0.0
        return 0.0

    # ---------------------------------------------------------------- kill-switch
    def check(self, now: int, equity: int, positions: Mapping[str, Position]) -> bool:
        """Update the drawdown and portfolio checks; trip the kill-switch if a hard limit is
        breached. Returns whether trading is still allowed."""
        st = self.state
        if st.killed:
            return False
        st.peak_equity = equity if st.peak_equity is None else max(st.peak_equity, equity)
        drawdown = (st.peak_equity - equity) / MICRO
        if drawdown >= self.limits.max_drawdown_usd:
            self.kill(now, f"drawdown ${drawdown:,.2f} >= ${self.limits.max_drawdown_usd:,.2f}")
        elif self.portfolio_loss(positions) > self.limits.max_portfolio_loss_usd * 1.05:
            self.kill(now, "portfolio worst-case loss exceeded its limit")
        return not st.killed

    def kill(self, now: int, reason: str) -> None:
        if not self.state.killed:
            self.state.killed, self.state.reason, self.state.killed_at_ns = True, reason, now
            self.state.events.append((now, f"KILL: {reason}"))
