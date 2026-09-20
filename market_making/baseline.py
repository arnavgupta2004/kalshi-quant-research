"""The baseline binary market maker: bounded-support Avellaneda-Stoikov with hard inventory limits.

    fair value   the book mid (Stage 8: the market price is well calibrated; no model beat it)
    quote centre the CARA reservation prices of the Bernoulli claim (``quoting.py``): bounded by
                 construction, skewed against inventory, risk term = the claim's remaining variance
    quote width  the reservation spread plus the AS liquidity term (1/g) ln(1 + g/k)
    quote size   ``size`` contracts, cut so that the worst SETTLEMENT loss after a fill stays inside
                 the per-market, per-event and portfolio limits (``risk.py``)
    stops        no quotes when the book is stale or one-sided, the price is near 0 or 1, or the
                 scheduled end is close; a drawdown or exposure breach trips the kill-switch

It quotes both sides with post-only orders.  Kalshi has bids only, so the "ask" on YES at ``a`` is a
bid on NO at ``1 - a``; ``ctx.position`` nets the two into one signed inventory ``q``.

This strategy is deliberately *non-adaptive*: it does not read order-book imbalance, order flow,
recent fills or an adverse-selection estimate, and its volatility enters only through the Bernoulli
variance ``p (1-p)``.  Those are the components Stage 10 adds and ablates against this baseline.

Cancel/replace under latency.  An order that has been submitted is not visible as resting until it
reaches the exchange, and a cancel takes effect only after its own latency, so naive requoting can
stack duplicates.  The strategy therefore acts on a (market, side) at most once per ``busy_s`` and
re-checks after a wake-up.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from backtest.engine import TIF, Context, Order
from backtest.events import BookConfirm, BookUpdate, Fill, MarketClose, Settlement, Wake
from backtest.portfolio import Position
from market.contracts import Side
from market_making.quoting import PRICE_SCALE, quote
from market_making.risk import MICRO, RiskLimits, RiskMonitor

SEC = 1_000_000_000
TAG = "mm"


@dataclass(frozen=True)
class BaselineParams:
    gamma: float = 0.05  # CARA risk aversion per contract
    k: float = 50.0  # fill-intensity decay per dollar of distance (delta ~ 1/k when gamma is small)
    size: float = 10.0  # contracts quoted per side
    tick: float = 0.01
    min_half_spread: float = 0.0  # dollars: an extra floor under the AS liquidity term
    max_spread: float = 0.15  # do not quote books wider than this (the mid is not trustworthy)
    requote_ticks: int = 1  # replace a quote only if its target moved this many ticks
    min_requote_s: float = 2.0
    busy_s: float = 1.0
    risk_check_s: float = 1.0
    limits: RiskLimits = field(default_factory=RiskLimits)


@dataclass
class QuoteEvent:
    ts: int
    ticker: str
    side: str  # "yes" (bid) | "no" (the ask, as a NO bid)
    action: str  # "place" | "cancel"
    price: int
    qty: int
    reason: str = ""
    order_id: int | None = None


class BinaryMarketMaker:
    name = "baseline"

    def __init__(
        self,
        tickers: Sequence[str],
        params: BaselineParams = BaselineParams(),  # noqa: B008
        *,
        event_of: Mapping[str, str],
        worlds: Mapping[str, Sequence[Mapping[str, int]]] | None = None,
        fair: Callable[[Context, str, object], float | None] | None = None,
    ) -> None:
        self.universe = set(tickers)
        self.p = params
        self.risk = RiskMonitor(params.limits, event_of, worlds)
        self._fair = fair
        self._held: set[str] = set()
        self._busy: dict[tuple[str, Side], int] = {}
        self._cancelling: set[int] = set()
        self._last_place: dict[tuple[str, Side], int] = {}
        self._next_risk = 0
        self.quote_log: list[QuoteEvent] = []
        # : (ts_ns, portfolio worst-case loss $, gross inventory contracts, markets held) at each
        # risk check
        self.exposure_log: list[tuple[int, float, float, int]] = []
        self.fills: list[Fill] = []
        self.placed_at: dict[int, int] = {}
        self.cancel_requested_at: dict[int, int] = {}
        self.decisions = 0

    # ------------------------------------------------------------------ engine interface
    def on_event(self, ctx: Context, ev: object) -> None:
        if isinstance(ev, Fill):
            self.fills.append(ev)
            self._held.add(ev.ticker)
            todo = {ev.ticker}
        elif isinstance(ev, BookUpdate):
            todo = {ev.ticker} if ev.ticker in self.universe else set()
        elif isinstance(ev, Wake) and ev.tag == TAG:
            todo = {ev.ticker} if ev.ticker else set()
        elif isinstance(ev, (BookConfirm, MarketClose, Settlement)):
            return
        else:
            return
        if not todo:
            return
        self._risk_check(ctx)
        for t in sorted(todo):
            self._manage(ctx, t)

    # ------------------------------------------------------------------ risk
    def _positions(self, ctx: Context, extra: str | None = None) -> dict[str, Position]:
        ts = set(self._held)
        if extra:
            ts.add(extra)
        return {t: ctx.position(t) for t in ts}

    def _risk_check(self, ctx: Context) -> None:
        if ctx.now < self._next_risk:
            return
        self._next_risk = ctx.now + int(self.p.risk_check_s * SEC)
        positions = self._positions(ctx)
        held = [p for p in positions.values() if not p.flat]
        self.exposure_log.append(
            (
                ctx.now,
                self.risk.portfolio_loss(positions),
                sum(abs(p.net_yes) for p in held) / 100,
                len(held),
            )
        )
        if not self.risk.check(ctx.now, ctx.equity(), positions):
            self._cancel_everything(ctx, "kill-switch")

    def _cancel_everything(self, ctx: Context, why: str) -> None:
        for o in ctx.open_orders():
            if o.tag == TAG:
                self._cancel(ctx, o, why)

    # ------------------------------------------------------------------ quoting
    def _fair_value(self, ctx: Context, ticker: str, view) -> float | None:
        if self._fair is not None:
            return self._fair(ctx, ticker, view)
        yb, ya = view.best_bid(Side.YES), view.best_ask(Side.YES)
        if yb is None or ya is None:
            return None
        return (yb.price + ya.price) / 2 / PRICE_SCALE

    def _target(self, ctx: Context, t: str) -> dict[Side, tuple[int, int]]:
        """Desired resting orders as {side: (price_ticks, qty_centi)}; empty = do not quote."""
        lim = self.p.limits
        if self.risk.state.killed or not ctx.is_open(t):
            return {}
        view = ctx.book(t)
        if view is None or view.age_ns > lim.max_book_age_s * SEC:
            return {}
        yb, ya = view.best_bid(Side.YES), view.best_ask(Side.YES)
        if yb is None or ya is None:
            return {}
        spread = (ya.price - yb.price) / PRICE_SCALE
        if spread > self.p.max_spread or spread <= 0:
            return {}
        end = ctx.info(t).scheduled_end
        if end is not None:
            from market.timeutil import to_epoch_us

            if (to_epoch_us(end) * 1000 - ctx.now) / (3600 * SEC) < lim.stop_hours_before_expiry:
                return {}
        fair = self._fair_value(ctx, t, view)
        if fair is None or not lim.min_price <= fair <= lim.max_price:
            return {}
        q = ctx.position(t).net_yes / 100
        qt = quote(
            q,
            fair,
            gamma=self.p.gamma,
            k=self.p.k,
            tick=self.p.tick,
            min_half_spread=self.p.min_half_spread,
            best_bid=yb.price / PRICE_SCALE,
            best_ask=ya.price / PRICE_SCALE,
        )
        positions = self._positions(ctx, t)
        out: dict[Side, tuple[int, int]] = {}
        if qt.bid is not None:
            px = round(qt.bid * PRICE_SCALE)
            n = self.risk.headroom(positions, t, True, px / PRICE_SCALE, self.p.size)
            if n >= 1:
                out[Side.YES] = (px, int(n) * 100)
        if qt.ask is not None:
            no_px = PRICE_SCALE - round(qt.ask * PRICE_SCALE)
            n = self.risk.headroom(positions, t, False, no_px / PRICE_SCALE, self.p.size)
            if n >= 1:
                out[Side.NO] = (no_px, int(n) * 100)
        return out

    def _manage(self, ctx: Context, t: str) -> None:
        self.decisions += 1
        target = self._target(ctx, t)
        live = {o.side: o for o in ctx.open_orders(t) if o.tag == TAG}
        step = round(self.p.requote_ticks * self.p.tick * PRICE_SCALE)
        retry = False
        for side in (Side.YES, Side.NO):
            key = (t, side)
            if self._busy.get(key, 0) > ctx.now:
                retry = True
                continue
            want, cur = target.get(side), live.get(side)
            if cur is not None and cur.order_id in self._cancelling:
                retry = True
                continue
            if cur is None:
                if want is not None and ctx.now - self._last_place.get(key, -(10**18)) >= int(
                    self.p.min_requote_s * SEC
                ):
                    self._place(ctx, t, side, *want)
                elif want is not None:
                    retry = True
                continue
            if want is None:
                self._cancel(ctx, cur, "no target")
                retry = True
            elif abs(want[0] - cur.price) >= step or want[1] < cur.qty or cur.qty < 0.5 * want[1]:
                self._cancel(ctx, cur, "requote")
                retry = True
        if retry:
            ctx.wake_at(ctx.now + int((self.p.busy_s + 0.05) * SEC), TAG, t)

    def _place(self, ctx: Context, t: str, side: Side, price: int, qty: int) -> None:
        oid = ctx.place(Order(t, side, price, qty, TIF.POST_ONLY, TAG))
        self.placed_at[oid] = ctx.now
        self._last_place[(t, side)] = ctx.now
        self._busy[(t, side)] = ctx.now + int(self.p.busy_s * SEC)
        self.quote_log.append(QuoteEvent(ctx.now, t, side.value, "place", price, qty, "", oid))

    def _cancel(self, ctx: Context, o, reason: str) -> None:
        if o.order_id in self._cancelling:
            return
        ctx.cancel(o.order_id)
        self._cancelling.add(o.order_id)
        self.cancel_requested_at[o.order_id] = ctx.now
        self._busy[(o.ticker, o.side)] = ctx.now + int(self.p.busy_s * SEC)
        self.quote_log.append(
            QuoteEvent(
                ctx.now, o.ticker, o.side.value, "cancel", o.price, o.qty, reason, o.order_id
            )
        )


__all__ = ["BaselineParams", "BinaryMarketMaker", "QuoteEvent", "MICRO"]
