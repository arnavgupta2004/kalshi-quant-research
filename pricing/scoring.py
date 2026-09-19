"""Proper scoring rules and the probability-bounds convention every model obeys.

A probability forecast p for a binary outcome y is judged by a *proper* scoring rule: one whose
expected value is optimised only by reporting the true probability.  Two are used throughout:

    Brier     (p - y)^2                    bounded in [0, 1]; forgiving of confident misses
    log loss  -(y ln p + (1-y) ln(1-p))    unbounded: a confident miss is punished without limit

**Bounds.**  Kalshi prices live on the open interval (0, 1) in whole ticks (1e-4 $), and a model
that outputs exactly 0 or 1 has infinite log loss the moment it is wrong.  Every model here
therefore returns probabilities clipped to ``[EPS, 1 - EPS]`` with ``EPS`` = one tick; ``clip`` is
the single place that convention lives, and the tests assert it holds for every model on every
input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

EPS = 1e-4  # one price tick: the closest to 0 or 1 a market itself can quote


def clip(p: float, eps: float = EPS) -> float:
    """Force a probability into [eps, 1 - eps]; NaN is an error, not a silent 0.5."""
    if p != p:
        raise ValueError("probability is NaN")
    return min(1.0 - eps, max(eps, p))


def _check(p: Sequence[float], y: Sequence[int]) -> None:
    if len(p) != len(y):
        raise ValueError(f"{len(p)} probabilities but {len(y)} outcomes")
    if any(v not in (0, 1) for v in y):
        raise ValueError("outcomes must be 0 or 1 (scalar/void markets have no binary label)")
    if any(not 0.0 <= q <= 1.0 for q in p):
        raise ValueError("probabilities must lie in [0, 1]")


def brier(p: Sequence[float], y: Sequence[int]) -> float:
    _check(p, y)
    if not p:
        raise ValueError("no forecasts")
    return sum((q - o) ** 2 for q, o in zip(p, y, strict=True)) / len(p)


def log_loss(p: Sequence[float], y: Sequence[int], eps: float = EPS) -> float:
    """Mean negative log likelihood; inputs are clipped to [eps, 1-eps] so a model that reports
    exactly 0 or 1 gets a large finite penalty instead of infinity (the clipping is reported, never
    hidden)."""
    _check(p, y)
    if not p:
        raise ValueError("no forecasts")
    total = 0.0
    for q, o in zip(p, y, strict=True):
        q = clip(q, eps)
        total -= math.log(q) if o else math.log(1.0 - q)
    return total / len(p)


def per_row_log_loss(p: Sequence[float], y: Sequence[int], eps: float = EPS) -> list[float]:
    _check(p, y)
    return [
        -(math.log(clip(q, eps)) if o else math.log(1.0 - clip(q, eps)))
        for q, o in zip(p, y, strict=True)
    ]


def per_row_brier(p: Sequence[float], y: Sequence[int]) -> list[float]:
    _check(p, y)
    return [(q - o) ** 2 for q, o in zip(p, y, strict=True)]


def base_rate_log_loss(y: Sequence[int]) -> float:
    """Log loss of always forecasting the sample's own frequency: the entropy, the floor for a model
    that knows nothing about any individual market (but was told the base rate)."""
    if not y:
        raise ValueError("no outcomes")
    q = sum(y) / len(y)
    if q in (0.0, 1.0):
        return 0.0
    return -(q * math.log(q) + (1 - q) * math.log(1 - q))


def logit(p: float) -> float:
    p = clip(p)
    return math.log(p / (1.0 - p))


def expit(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)
