"""Probability calibration: do events happen about as often as the forecasts say?

A forecast is *calibrated* if, among all instances where it said ``p``, the event occurred with
frequency ``p``.  Calibration is separate from skill: a forecaster who always says the base rate is
perfectly calibrated and useless; one who is right often but overconfident is skilful and
miscalibrated.  This module measures the first and reports the second alongside it.

What is measured (all on ``(p, y)`` with an event id per instance):

  reliability bins      frequency of YES against mean forecast in each probability bin calibration
  regression  logit(P(y=1)) = a + b * logit(p).  b = 1, a = 0 is perfect calibration; b < 1 means
  forecasts are too extreme (overconfident), b > 1 too timid; a is a shift. calibration in the large
  mean(y) - mean(p): is the forecaster biased on average? ECE                   expected calibration
  error: bin-weighted mean |frequency - mean forecast|. Murphy decomposition  Brier = reliability -
  resolution + uncertainty (+ two within-bin terms): how much of the score is miscalibration, how
  much is skill, how much is the inherent unpredictability of the base rate.

Uncertainty is an **event-cluster bootstrap** (instances of one market, and siblings of one event,
share an outcome): every interval here resamples whole events.

ECE is biased upward in small samples (noise looks like miscalibration), so it is never reported
without an interval and a hint of its noise floor; the regression slope and intercept are the
primary summaries.

Recalibrators (``PlattCalibrator``, ``IsotonicCalibrator``) map a forecast to a corrected one.  Both
are monotone (they never reorder forecasts), bounded to ``[EPS, 1 - EPS]``, and are fitted on the
research period only when applied to a holdout.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass

import numpy as np

from pricing.scoring import EPS
from research.stats import Estimate

Z_LO, Z_HI = np.log(EPS / (1 - EPS)), np.log((1 - EPS) / EPS)


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def _expit(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


# --------------------------------------------------------------------------- event-cluster
# bootstrap
def cluster_index(groups: Sequence[Hashable]) -> list[np.ndarray]:
    """Row indices of each event, so a resample can be built by concatenating index arrays."""
    by: dict[Hashable, list[int]] = defaultdict(list)
    for i, g in enumerate(groups):
        by[g].append(i)
    return [np.array(v) for v in by.values()]


def boot_stat(
    clusters: list[np.ndarray],
    stat: Callable[[np.ndarray], float | None],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Estimate:
    """Percentile bootstrap of ``stat(row_indices)`` resampling whole events."""
    n_rows = sum(len(c) for c in clusters)
    point = stat(np.concatenate(clusters)) if clusters else None
    if len(clusters) < 2 or point is None:
        return Estimate(point, None, None, n_rows, len(clusters))
    rng = random.Random(seed)
    draws = []
    k = len(clusters)
    for _ in range(n_boot):
        idx = np.concatenate([clusters[rng.randrange(k)] for _ in range(k)])
        v = stat(idx)
        if v is not None and np.isfinite(v):
            draws.append(v)
    if len(draws) < 0.9 * n_boot:
        return Estimate(point, None, None, n_rows, len(clusters))
    draws.sort()
    lo = draws[int(alpha / 2 * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))]
    return Estimate(point, lo, hi, n_rows, len(clusters))


def boot_multi(
    clusters: list[np.ndarray],
    stat: Callable[[np.ndarray], Sequence[float]],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> list[Estimate]:
    """One event-cluster bootstrap for several statistics at once (they share every resample, so
    their intervals are mutually consistent and the cost is one refit per draw, not one per
    statistic)."""
    n_rows = sum(len(c) for c in clusters)
    point = list(stat(np.concatenate(clusters))) if clusters else []
    if not point:
        return []
    if len(clusters) < 2:
        return [
            Estimate(v if np.isfinite(v) else None, None, None, n_rows, len(clusters))
            for v in point
        ]
    rng = random.Random(seed)
    k = len(clusters)
    draws: list[list[float]] = [[] for _ in point]
    for _ in range(n_boot):
        idx = np.concatenate([clusters[rng.randrange(k)] for _ in range(k)])
        for j, v in enumerate(stat(idx)):
            if np.isfinite(v):
                draws[j].append(float(v))
    out = []
    for v0, d in zip(point, draws, strict=True):
        v0 = float(v0) if np.isfinite(v0) else None
        if v0 is None or len(d) < 0.9 * n_boot:
            out.append(Estimate(v0, None, None, n_rows, k))
            continue
        d.sort()
        out.append(
            Estimate(
                v0,
                d[int(alpha / 2 * len(d))],
                d[min(len(d) - 1, int((1 - alpha / 2) * len(d)))],
                n_rows,
                k,
            )
        )
    return out


# --------------------------------------------------------------------------- reliability bins
@dataclass(frozen=True)
class Bin:
    lo: float
    hi: float
    n: int
    mean_p: float
    freq: float

    @property
    def gap(self) -> float:
        """frequency - forecast: positive = the event happened MORE often than forecast."""
        return self.freq - self.mean_p


def edges_width(k: int = 10) -> list[float]:
    return [i / k for i in range(k + 1)]


def edges_mass(p: np.ndarray, k: int = 10) -> list[float]:
    """Quantile edges: each bin holds about the same number of forecasts (a fixed-width grid is
    empty where prices cluster at the extremes)."""
    e = np.quantile(p, np.linspace(0, 1, k + 1))
    e[0], e[-1] = 0.0, 1.0
    return sorted(set(float(x) for x in e))


def reliability_bins(p: np.ndarray, y: np.ndarray, edges: Sequence[float]) -> list[Bin]:
    out = []
    for j, (lo, hi) in enumerate(zip(edges, edges[1:], strict=False)):
        last = j == len(edges) - 2
        m = (p >= lo) & ((p <= hi) if last else (p < hi))
        if m.any():
            out.append(Bin(lo, hi, int(m.sum()), float(p[m].mean()), float(y[m].mean())))
    return out


# --------------------------------------------------------------------------- summary statistics
def calibration_in_the_large(p: np.ndarray, y: np.ndarray) -> float:
    return float(y.mean() - p.mean())


def ece(p: np.ndarray, y: np.ndarray, edges: Sequence[float]) -> float:
    bins = reliability_bins(p, y, edges)
    n = sum(b.n for b in bins)
    return sum(b.n / n * abs(b.gap) for b in bins) if n else float("nan")


def calibration_regression(
    p: np.ndarray, y: np.ndarray, ridge: float = 1e-8, iters: int = 60
) -> tuple[float, float] | None:
    """(intercept a, slope b) of ``logit P(y=1) = a + b logit(p)`` by maximum likelihood.  ``None``
    when the forecasts have no spread (a constant forecast has no slope) or the fit degenerates."""
    z = _logit(p)
    if len(z) < 5 or float(z.std()) < 1e-6:
        return None
    a, b = 0.0, 1.0
    for _ in range(iters):
        eta = a + b * z
        q = _expit(eta)
        w = np.clip(q * (1 - q), 1e-9, None)
        g = np.array([np.sum(q - y), np.sum((q - y) * z)])
        H = np.array(
            [[np.sum(w) + ridge, np.sum(w * z)], [np.sum(w * z), np.sum(w * z * z) + ridge]]
        )
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return None
        a, b = a - step[0], b - step[1]
        if np.max(np.abs(step)) < 1e-10:
            break
    return (float(a), float(b)) if abs(a) < 20 and abs(b) < 20 else None


def murphy(p: np.ndarray, y: np.ndarray, edges: Sequence[float]) -> dict[str, float]:
    """Murphy decomposition of the Brier score over probability bins (an exact identity):

        Brier = reliability - resolution + uncertainty + within_var - 2 * within_cov

      reliability   sum_k w_k (mean_p_k - freq_k)^2    miscalibration (lower is better)
      resolution    sum_k w_k (freq_k - base)^2        how far the bins separate from the base rate
      uncertainty   base (1 - base)                    inherent unpredictability of the outcome
      within_var    sum_k w_k Var(p | bin k)           forecast spread hidden inside a bin
      within_cov    sum_k w_k Cov(p, y | bin k)        skill hidden inside a bin

    The last two vanish when the bins are fine enough that p is constant within each; with coarse
    bins they are the (usually small) price of binning.  ``identity_residual`` should be ~1e-16.
    """
    n = len(p)
    base = float(y.mean())
    rel = res = wvar = wcov = 0.0
    for j, (lo, hi) in enumerate(zip(edges, edges[1:], strict=False)):
        m = (p >= lo) & ((p <= hi) if j == len(edges) - 2 else (p < hi))
        if not m.any():
            continue
        w = m.sum() / n
        pm, ym = float(p[m].mean()), float(y[m].mean())
        rel += w * (pm - ym) ** 2
        res += w * (ym - base) ** 2
        wvar += w * float(p[m].var())
        wcov += w * float(np.mean((p[m] - pm) * (y[m] - ym)))
    unc = base * (1 - base)
    brier = float(np.mean((p - y) ** 2))
    return {
        "brier": brier,
        "reliability": rel,
        "resolution": res,
        "uncertainty": unc,
        "within_bin_variance": wvar,
        "within_bin_covariance": wcov,
        "identity_residual": brier - (rel - res + unc + wvar - 2 * wcov),
    }


# --------------------------------------------------------------------------- recalibrators
class PlattCalibrator:
    """logit p~ = a + b logit(p): two parameters, always monotone (b > 0)."""

    def __init__(self, ridge: float = 1e-6) -> None:
        self.ridge = ridge
        self.a = 0.0
        self.b = 1.0

    def fit(self, p: np.ndarray, y: np.ndarray) -> PlattCalibrator:
        r = calibration_regression(p, y, ridge=self.ridge)
        if r is not None and r[1] > 0:
            self.a, self.b = r
        return self

    def predict(self, p: np.ndarray) -> np.ndarray:
        return np.clip(_expit(self.a + self.b * _logit(np.asarray(p, float))), EPS, 1 - EPS)


class IsotonicCalibrator:
    """Non-parametric monotone recalibration by pool-adjacent-violators.

    Fits the best non-decreasing step function of the forecast.  Flexible (it can fix any monotone
    distortion) and therefore prone to overfit with few events; predictions interpolate linearly
    between block centres and are clipped to ``[EPS, 1 - EPS]``."""

    def __init__(self) -> None:
        self.x: np.ndarray = np.array([0.0, 1.0])
        self.v: np.ndarray = np.array([0.5, 0.5])
        self.fitted_: np.ndarray = np.array([])

    def fit(self, p: np.ndarray, y: np.ndarray) -> IsotonicCalibrator:
        order = np.argsort(p, kind="stable")
        ps, ys = np.asarray(p, float)[order], np.asarray(y, float)[order]
        blocks: list[list[float]] = []  # [sum_y, weight, sum_p]
        for pi, yi in zip(ps, ys, strict=True):
            blocks.append([yi, 1.0, pi])
            while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
                b = blocks.pop()
                blocks[-1][0] += b[0]
                blocks[-1][1] += b[1]
                blocks[-1][2] += b[2]
        self.x = np.array([b[2] / b[1] for b in blocks])
        self.v = np.array([b[0] / b[1] for b in blocks])
        # the pure isotonic fit at each training point (its block's mean), in the caller's order
        sizes = [int(b[1]) for b in blocks]
        step = np.repeat(self.v, sizes)
        self.fitted_ = np.empty(len(ps))
        self.fitted_[order] = step
        return self

    def predict(self, p: np.ndarray) -> np.ndarray:
        return np.clip(np.interp(np.asarray(p, float), self.x, self.v), EPS, 1 - EPS)
