"""The adaptive market maker: the Stage 9 baseline plus four measured adjustments.

Each adjustment is one formula, has its own switch, and is admitted only on evidence from data the
model was not fitted on (``research.signal_study``, ``docs/adaptive_market_maker.md``).

  1. **Fair value** ``f = m + mu``.  ``m`` is the book mid; ``mu`` is a frozen ridge forecast of the
     mid's move over the next ``h_fv`` seconds from causal features (``market_making.features``).
     Quotes are centred on ``f`` instead of ``m``.
  2. **Adverse-selection widening** ``delta += kappa * sigma``.  ``sigma`` is a frozen forecast of
     ``E |mid(t + h) - mid(t)|`` (realised movement clusters: ``rv_60`` predicts it out of sample).
     A resting quote is picked off when the price jumps through it, and the expected size of that
     jump is what a spread must cover; the baseline's ``(1/g) ln(1 + g/k)`` term prices only the
     arrival rate.
  3. **Size** ``n = n0 * clip(sigma_ref / sigma, f_min, f_max)``: a constant dollar risk per quote
     (the loss from a fill scales with ``n * sigma``).  ``sigma_ref`` is calibrated on training data
     so that the AVERAGE size equals ``n0``: the rule moves size from volatile to calm states
     without changing how much is traded overall, so it can be compared with the baseline per
     contract.
  4. **Time to resolution** ``gamma_eff = gamma (1 + a exp(-tau / tau0))``: the closer to
     settlement, the fewer chances to work inventory off passively, so skew harder.

Every switch off gives back the baseline, quote for quote (tested).  The strategy also re-prices on
trade prints, not only on book snapshots: recorded books are ~3 s stale, so a print is the freshest
price information there is, and a live feed would show it in the book at once.  What that buys is
therefore an *emulation of a live feed* and is reported apart from the genuine signals.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from backtest.engine import Context
from backtest.events import BookConfirm, BookUpdate, TradeTick
from market.contracts import Side
from market.timeutil import to_epoch_us
from market_making.baseline import PRICE_SCALE, SEC, BaselineParams, BinaryMarketMaker
from market_making.features import MarketTracker
from market_making.quoting import liquidity_half_spread, quote
from market_making.shortterm import Ridge


@dataclass(frozen=True)
class AdaptiveParams:
    base: BaselineParams = field(default_factory=BaselineParams)
    fv: Ridge | None = None  # signed fair-value shift (dollars); None = quote around the mid
    scale: Ridge | None = None  # expected |mid change| (dollars) for widening and sizing
    kappa: float = 0.0  # half-spread add-on = kappa * scale
    size_by_scale: bool = False
    sigma_ref: float = 0.01  # dollars: the scale at which the full size is quoted
    min_size_frac: float = 0.2
    max_size_frac: float = 2.0
    ttr_skew: float = 0.0  # a in gamma_eff = gamma (1 + a exp(-tau / tau0)); 0 = off
    ttr_tau0_h: float = 1.0
    react_to_trades: bool = True
    trade_react_s: float = 0.5  # re-price on prints at most this often per market


class AdaptiveMarketMaker(BinaryMarketMaker):
    name = "adaptive"

    def __init__(
        self,
        tickers: Sequence[str],
        params: AdaptiveParams,
        *,
        event_of: Mapping[str, str],
        worlds: Mapping[str, Sequence[Mapping[str, int]]] | None = None,
    ) -> None:
        super().__init__(tickers, params.base, event_of=event_of, worlds=worlds)
        self.a = params
        self._trk = {t: MarketTracker() for t in tickers}
        self._last_react: dict[str, int] = {}
        #: (ts, ticker, mu, sigma or None, gamma_eff) each time a target was computed
        self.signals: list[tuple[int, str, float, float | None, float]] = []

    # ------------------------------------------------------------------ engine interface
    def on_event(self, ctx: Context, ev: object) -> None:
        if isinstance(ev, BookUpdate):
            trk = self._trk.get(ev.ticker)
            view = ctx.book(ev.ticker) if trk is not None else None
            if view is not None:
                # read the engine's book, not the event: simultaneous events are applied as a batch
                # before the strategy sees any of them, and the tracker must agree with what the
                # strategy will price off
                trk.on_book(
                    ev.ts_ns,
                    [(lv.price, lv.qty) for lv in view.bids(Side.YES)],
                    [(lv.price, lv.qty) for lv in view.bids(Side.NO)],
                )
        elif isinstance(ev, BookConfirm):
            for trk in self._trk.values():
                trk.on_confirm(ev.ts_ns)
        elif isinstance(ev, TradeTick):
            trk = self._trk.get(ev.ticker)
            if trk is None:
                return
            t = ev.trade
            sign = 1 if t.taker_side is Side.YES else -1 if t.taker_side is Side.NO else 0
            trk.on_trade(ev.ts_ns, t.yes_price, t.count, sign)
            last = self._last_react.get(ev.ticker, -(10**18))
            if self.a.react_to_trades and ctx.now - last >= int(self.a.trade_react_s * SEC):
                self._last_react[ev.ticker] = ctx.now
                self._risk_check(ctx)
                self._manage(ctx, ev.ticker)
            return
        super().on_event(ctx, ev)

    # ------------------------------------------------------------------ quoting
    def _hours_to_end(self, ctx: Context, t: str) -> float | None:
        end = ctx.info(t).scheduled_end
        return None if end is None else (to_epoch_us(end) * 1000 - ctx.now) / (3600 * SEC)

    def _target(self, ctx: Context, t: str) -> dict[Side, tuple[int, int]]:
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
        tau = self._hours_to_end(ctx, t)
        if tau is not None and tau < lim.stop_hours_before_expiry:
            return {}
        snap = self._trk[t].snapshot(ctx.now)
        if snap is None:
            return {}
        mu = self.a.fv.predict_one(snap.x) if self.a.fv is not None else 0.0
        sigma = self.a.scale.predict_one(snap.x) if self.a.scale is not None else None
        fair = snap.mid + mu
        if not lim.min_price <= fair <= lim.max_price:
            return {}
        gamma = self.p.gamma
        if self.a.ttr_skew and tau is not None:
            gamma *= 1.0 + self.a.ttr_skew * math.exp(-max(tau, 0.0) / self.a.ttr_tau0_h)
        half = liquidity_half_spread(gamma, self.p.k)
        if self.a.kappa and sigma is not None:
            half += self.a.kappa * sigma
        q = ctx.position(t).net_yes / 100
        qt = quote(
            q,
            fair,
            gamma=gamma,
            k=self.p.k,
            tick=self.p.tick,
            min_half_spread=max(self.p.min_half_spread, half),
            best_bid=yb.price / PRICE_SCALE,
            best_ask=ya.price / PRICE_SCALE,
        )
        size = self.p.size
        if self.a.size_by_scale and sigma:
            frac = self.a.sigma_ref / sigma
            size = size * min(self.a.max_size_frac, max(self.a.min_size_frac, frac))
        size = max(1.0, math.floor(size))
        self.signals.append((ctx.now, t, mu, sigma, gamma))
        positions = self._positions(ctx, t)
        out: dict[Side, tuple[int, int]] = {}
        if qt.bid is not None:
            px = round(qt.bid * PRICE_SCALE)
            n = self.risk.headroom(positions, t, True, px / PRICE_SCALE, size)
            if n >= 1:
                out[Side.YES] = (px, int(n) * 100)
        if qt.ask is not None:
            no_px = PRICE_SCALE - round(qt.ask * PRICE_SCALE)
            n = self.risk.headroom(positions, t, False, no_px / PRICE_SCALE, size)
            if n >= 1:
                out[Side.NO] = (no_px, int(n) * 100)
        return out


__all__ = ["AdaptiveMarketMaker", "AdaptiveParams"]
