"""Quoting mathematics for a bounded (0/1) claim: what Avellaneda-Stoikov becomes when the asset is
binary.

WHY AVELLANEDA-STOIKOV CANNOT BE USED AS DERIVED
------------------------------------------------
Avellaneda & Stoikov (2008) price a dealer in a stock whose mid follows ``dS = sigma dW``, with
exponential (CARA) utility, a fixed horizon ``T`` and arrival intensity ``lambda(delta) = A
exp(-k delta)``.  The result is a *reservation price* and a *spread*::

    r = s - q gamma sigma^2 (T - t)
    spread = gamma sigma^2 (T - t) + (2/gamma) ln(1 + gamma/k)

Every ingredient assumes a continuous, unbounded asset and fails for a binary claim:

  1. **Unbounded price.**  ``s`` is arithmetic Brownian, so quotes can leave [0, 1] and go
     negative; a Kalshi price lives strictly inside (0, $1) and orders outside it are invalid.
  2. **Constant volatility.**  A probability is a *bounded martingale*: its variance must vanish as
     it nears 0 or 1 (locally ``sigma sqrt(p (1-p))``).  A constant ``sigma`` is wrong precisely
     where risk is smallest and quotes are most constrained.
  3. **Deterministic risk clock.**  The inventory-risk term ``sigma^2 (T - t)`` shrinks with
     calendar time.  For a martingale that resolves to X in {0, 1} the *total remaining variance*
     is exactly ``E[(X - p_t)^2 | F_t] = p_t (1 - p_t)`` whatever the time left: it changes through
     **information arriving** (``p_t`` moving toward an edge), not through the clock.  Time to
     settlement still matters - for how long inventory stays exposed, and because information (and
     adverse selection) arrives faster near resolution - but it enters through the state, not
     through a ``(T - t)`` factor.
  4. **Terminal condition.**  AS liquidates at the market price at ``T``.  A binary position
     *settles* at 0 or 1: a jump, not a price the dealer can trade out of; worst-case loss is the
     whole cost.
  5. **Symmetry of the two sides.**  Kalshi has bids only: an "ask" on YES is a bid on NO.  The
     right model is invariant under swapping YES/NO (``p -> 1-p``, ``q -> -q``); AS has no such
     structure.

THE BOUNDED FORMULATION USED HERE
---------------------------------
Hold ``q`` YES contracts (``q < 0`` = short YES = long NO) to settlement, belief ``p = P(X = 1)``,
CARA utility ``U(w) = -exp(-gamma w)``.  Terminal wealth is ``q X``, so the exact certainty
equivalent is::

    V(q) = -(1/gamma) ln E[exp(-gamma q X)] = -(1/gamma) ln(1 - p + p exp(-gamma q))

The dealer's **reservation prices** are marginal certainty equivalents::

    bid*(q) = V(q + 1) - V(q)      (the most he will pay for one more YES)
    ask*(q) = V(q) - V(q - 1)      (the least he will accept to sell one YES) = bid*(q - 1)

Properties (each asserted in the tests):
  * ``0 < bid* < ask* < 1`` for every ``q``: bounded by construction, no clipping needed;
  * decreasing in ``q``: the more he owns, the less he will pay (inventory skew);
  * YES/NO duality: ``ask*(q; p) = 1 - bid*(-q; 1 - p)``;
  * ``gamma -> 0``: both -> ``p`` (risk neutral);
  * small ``gamma``: ``bid*(q) ~ p - gamma p (1-p) (q + 1/2)`` - exactly the AS form with
    ``sigma^2 (T-t)`` replaced by the binary claim's remaining variance ``p (1-p)``;
  * the structural spread ``ask* - bid*`` ~ ``gamma p (1-p)``: widest at ``p = 1/2``, vanishing at
    the edges.

The AS **liquidity term** survives unchanged because it comes from the fill-intensity model, not
from the price process: with ``lambda(delta) = A exp(-k delta)`` the optimal extra distance per
side is ``delta = (1/gamma) ln(1 + gamma/k)`` (``-> 1/k`` as ``gamma -> 0``).  The quotes are::

    bid = bid*(q) - delta        ask = ask*(q) + delta

then rounded to the tick *away from the reservation price* (bid down, ask up) and clipped so they
never cross the market or leave (0, 1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

PRICE_SCALE = 10_000
_GAMMA_FLOOR = 1e-9


def certainty_equivalent(q: float, p: float, gamma: float) -> float:
    """CE (dollars) of holding ``q`` YES contracts to settlement: belief ``p``, CARA ``gamma``."""
    if not 0.0 < p < 1.0:
        raise ValueError(f"p must lie strictly inside (0, 1), got {p}")
    if gamma < _GAMMA_FLOOR:
        return q * p
    # ln(1 - p + p e^{-gamma q}) computed stably as logaddexp(ln(1-p), ln p - gamma q)
    return -(1.0 / gamma) * _logaddexp(math.log1p(-p), math.log(p) - gamma * q)


def _logaddexp(a: float, b: float) -> float:
    m = max(a, b)
    return m + math.log(math.exp(a - m) + math.exp(b - m))


def _unit(x: float) -> float:
    """Clamp float dust: the marginal certainty equivalent of a [0, 1] payoff lies in [0, 1]
    mathematically (asserted unclamped in the tests); at extreme inventory rounding can overshoot
    it by ~1e-15."""
    return min(1.0, max(0.0, x))


def reservation_bid(q: float, p: float, gamma: float) -> float:
    """Most the dealer will pay for one more YES contract when already holding ``q``."""
    return _unit(certainty_equivalent(q + 1, p, gamma) - certainty_equivalent(q, p, gamma))


def reservation_ask(q: float, p: float, gamma: float) -> float:
    """Least the dealer will accept to sell one YES contract when holding ``q`` (= bid at q - 1)."""
    return _unit(certainty_equivalent(q, p, gamma) - certainty_equivalent(q - 1, p, gamma))


def liquidity_half_spread(gamma: float, k: float) -> float:
    """AS extra distance per side (dollars) for fill intensity ``A exp(-k delta)``, k per dollar."""
    if k <= 0:
        raise ValueError("k must be positive")
    if gamma < _GAMMA_FLOOR:
        return 1.0 / k
    return math.log1p(gamma / k) / gamma


@dataclass(frozen=True)
class Quotes:
    bid: float | None  # YES bid price in dollars (None = do not quote)
    ask: float | None  # YES ask price in dollars
    bid_raw: float
    ask_raw: float
    reservation_bid: float
    reservation_ask: float


def raw_quotes(
    q: float, p: float, gamma: float, k: float, min_half_spread: float = 0.0
) -> tuple[float, float]:
    """Unrounded, unclipped quotes around the bounded reservation prices."""
    delta = max(liquidity_half_spread(gamma, k), min_half_spread)
    return reservation_bid(q, p, gamma) - delta, reservation_ask(q, p, gamma) + delta


def quote(
    q: float,
    p: float,
    *,
    gamma: float,
    k: float,
    tick: float = 0.01,
    min_half_spread: float = 0.0,
    best_bid: float | None = None,
    best_ask: float | None = None,
) -> Quotes:
    """Tick-rounded quotes that never cross the visible market and always lie in ``[tick, 1 -
    tick]``.

    Rounding is conservative (bid down, ask up), so rounding never gives away edge.  If the market
    is known, a bid is capped one tick below the best ask and an ask one tick above the best bid (a
    post-only order that would cross is rejected by the exchange).  A side that cannot be quoted
    returns ``None``."""
    bid_raw, ask_raw = raw_quotes(q, p, gamma, k, min_half_spread)
    rb, ra = reservation_bid(q, p, gamma), reservation_ask(q, p, gamma)
    eps = 1e-9
    bid = math.floor(bid_raw / tick + eps) * tick
    ask = math.ceil(ask_raw / tick - eps) * tick
    if best_ask is not None:
        bid = min(bid, best_ask - tick)
    if best_bid is not None:
        ask = max(ask, best_bid + tick)
    lo, hi = tick, 1.0 - tick
    bid_out = bid if lo - eps <= bid <= hi + eps else None
    ask_out = ask if lo - eps <= ask <= hi + eps else None
    if bid_out is not None and ask_out is not None and bid_out >= ask_out - eps:
        bid_out = ask_out = None  # nothing sensible to quote between
    return Quotes(bid_out, ask_out, bid_raw, ask_raw, rb, ra)
