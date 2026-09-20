"""A frozen linear model of a short-horizon target, small enough to audit by eye.

``mu = intercept + ((x - center) / scale) . w``: features standardised on the TRAINING data, then
a ridge-shrunk linear combination, clipped to a sane range.  Fitting lives in
``research.signal_study``; this class is only the fitted object the strategy carries, so it needs
nothing but arithmetic.

    target "change"   the forecast of mid(t + h) - mid(t) in dollars; no intercept (a fair value
                      must not carry a built-in drift), clipped to +-``clip``
    target "abs"      the forecast of |mid(t + h) - mid(t)|: a scale (expected size of the move),
                      with an intercept, clipped to [0, ``clip``]
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from market_making.features import FEATURES


@dataclass(frozen=True)
class Ridge:
    """``mu = ((x - center) / scale) . w``, no intercept.  Features are standardised on the training
    data; the fit minimises squared error plus ``alpha * |w|^2``."""

    names: tuple[str, ...]
    center: tuple[float, ...]
    scale: tuple[float, ...]
    w: tuple[float, ...]
    alpha: float
    horizon_s: float
    clip: float = 0.05  # dollars: |mu| never exceeds this (a shift is a nudge, not a re-pricing)
    intercept: float = 0.0  # zero for a signed fair-value shift; the mean level for a scale model
    target: str = "change"  # "change": y = mid(t+h) - mid(t);  "abs": y = |mid(t+h) - mid(t)|

    def predict(self, x: np.ndarray, names: tuple[str, ...] = FEATURES) -> np.ndarray:
        cols = [names.index(n) for n in self.names]
        z = (np.atleast_2d(x)[:, cols] - np.array(self.center)) / np.array(self.scale)
        out = self.intercept + z @ np.array(self.w)
        return (
            np.clip(out, 0.0, self.clip)
            if self.target == "abs"
            else np.clip(out, -self.clip, self.clip)
        )

    def predict_one(self, x: tuple[float, ...]) -> float:
        z = self.intercept
        for i, n in enumerate(self.names):
            z += (x[FEATURES.index(n)] - self.center[i]) / self.scale[i] * self.w[i]
        return (
            max(0.0, min(self.clip, z))
            if self.target == "abs"
            else max(-self.clip, min(self.clip, z))
        )
