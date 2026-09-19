"""Out-of-sample evaluation that respects how the data are structured.

  * **Folds are grouped by event.**  Sibling markets of one event share an outcome structure
    (exactly one winner among mutually exclusive outcomes); putting one in train and another in test
    would leak the answer.  All instances of an event are in one fold.
  * **Hyper-parameters are chosen inside the training fold** (nested CV): the penalty is picked by
    an inner cross-validation on the outer-train instances, never on the ones being scored.
  * **Uncertainty resamples events**, not instances (``research.stats``): instances of one market at
    five horizons are the same outcome five times.
  * **Comparisons are paired**: the difference in per-instance loss between two models, so the
    hard-to-predict instances cancel.

Nothing here touches the sealed holdout; it only sees the instances it is given.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable, Sequence

import numpy as np

from pricing.dataset import Instance
from pricing.probability_models import ProbabilityModel, predict_all
from pricing.scoring import brier, clip, expit, log_loss, logit, per_row_brier, per_row_log_loss
from research.stats import Estimate, cluster_bootstrap_mean

ModelFactory = Callable[[float], ProbabilityModel]


def grouped_folds(instances: Sequence[Instance], k: int, seed: int = 0) -> list[list[int]]:
    """``k`` lists of instance indices; every event is wholly inside one list."""
    groups: dict[str, list[int]] = defaultdict(list)
    for i, inst in enumerate(instances):
        groups[inst.group].append(i)
    keys = sorted(groups)
    random.Random(seed).shuffle(keys)
    folds: list[list[int]] = [[] for _ in range(k)]
    sizes = [0] * k
    for key in keys:  # greedy: put each event in the currently smallest fold
        j = sizes.index(min(sizes))
        folds[j] += groups[key]
        sizes[j] += len(groups[key])
    return folds


def cross_val_predict(
    factory: Callable[[], ProbabilityModel],
    instances: Sequence[Instance],
    k: int = 5,
    seed: int = 0,
) -> list[float]:
    """Out-of-fold predictions: each instance is predicted by a model that never saw its event."""
    preds = [0.0] * len(instances)
    folds = grouped_folds(instances, k, seed)
    for held in folds:
        held_set = set(held)
        train = [instances[i] for i in range(len(instances)) if i not in held_set]
        model = factory().fit(train)
        for i in held:
            preds[i] = model.predict(instances[i].obs)
    return preds


def select_penalty(
    factory: ModelFactory, train: Sequence[Instance], grid: Sequence[float], k: int, seed: int
) -> float:
    """The penalty with the best inner-CV log loss on ``train``."""
    y = [i.out.y for i in train]
    best, best_ll = grid[0], float("inf")
    for lam in grid:
        ll = log_loss(cross_val_predict(lambda lam=lam: factory(lam), train, k, seed), y)
        if ll < best_ll:
            best, best_ll = lam, ll
    return best


def nested_cv_predict(
    factory: ModelFactory,
    instances: Sequence[Instance],
    grid: Sequence[float],
    *,
    k_outer: int = 5,
    k_inner: int = 3,
    seed: int = 0,
) -> tuple[list[float], list[float]]:
    """(out-of-fold predictions, the penalty chosen in each outer fold)."""
    preds = [0.0] * len(instances)
    chosen: list[float] = []
    for held in grouped_folds(instances, k_outer, seed):
        held_set = set(held)
        train = [instances[i] for i in range(len(instances)) if i not in held_set]
        lam = select_penalty(factory, train, grid, k_inner, seed + 1)
        chosen.append(lam)
        model = factory(lam).fit(train)
        for i in held:
            preds[i] = model.predict(instances[i].obs)
    return preds, chosen


# --------------------------------------------------------------------------- scoring with
# uncertainty
def mean_loss(
    instances: Sequence[Instance], preds: Sequence[float], kind: str = "log", *, n_boot=2000, seed=0
) -> Estimate:
    y = [i.out.y for i in instances]
    rows = per_row_log_loss(preds, y) if kind == "log" else per_row_brier(preds, y)
    data = list(zip(instances, rows, strict=True))
    return cluster_bootstrap_mean(
        data, lambda r: r[0].group, lambda r: r[1], n_boot=n_boot, seed=seed
    )


def paired_difference(
    instances: Sequence[Instance],
    preds_a: Sequence[float],
    preds_b: Sequence[float],
    kind: str = "log",
    *,
    n_boot: int = 2000,
    seed: int = 0,
) -> Estimate:
    """mean(loss_a - loss_b) with an event-cluster interval.  Negative: A is better."""
    y = [i.out.y for i in instances]
    f = per_row_log_loss if kind == "log" else per_row_brier
    diffs = [a - b for a, b in zip(f(preds_a, y), f(preds_b, y), strict=True)]
    data = list(zip(instances, diffs, strict=True))
    return cluster_bootstrap_mean(
        data, lambda r: r[0].group, lambda r: r[1], n_boot=n_boot, seed=seed
    )


def score_table(
    instances: Sequence[Instance],
    preds: dict[str, Sequence[float]],
    *,
    n_boot: int = 2000,
    seed: int = 0,
) -> dict[str, dict]:
    y = [i.out.y for i in instances]
    out = {}
    for name, p in preds.items():
        out[name] = {
            "log_loss": mean_loss(instances, p, "log", n_boot=n_boot, seed=seed),
            "brier": mean_loss(instances, p, "brier", n_boot=n_boot, seed=seed),
            "log_loss_point": log_loss(p, y),
            "brier_point": brier(p, y),
        }
    return out


# --------------------------------------------------------------------------- does B add to the
# market?
def fit_blend(
    z_market: np.ndarray, z_other: np.ndarray, y: np.ndarray, lam: float = 1.0, iters: int = 50
) -> tuple[float, float]:
    """logit p = z_market + a + g * (z_other - z_market), ridge on g: how much weight does an
    independent estimate deserve *given the market price*?  g = 0 -> ignore it; g = 1 -> replace the
    market with it.  Returns (a, g)."""
    d = z_other - z_market
    a = g = 0.0
    for _ in range(iters):
        eta = z_market + a + g * d
        p = 1 / (1 + np.exp(-eta))
        w = np.clip(p * (1 - p), 1e-9, None)
        grad = np.array([np.sum(p - y), np.sum((p - y) * d) + lam * g])
        H = np.array([[np.sum(w), np.sum(w * d)], [np.sum(w * d), np.sum(w * d * d) + lam]])
        step = np.linalg.solve(H, grad)
        a, g = a - step[0], g - step[1]
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(a), float(g)


def blend_cv_predict(
    instances: Sequence[Instance],
    p_other: Sequence[float],
    *,
    k: int = 5,
    seed: int = 0,
    lam: float = 1.0,
) -> tuple[list[float], list[float]]:
    """Out-of-fold predictions of the market/other blend and the fitted weights ``g`` per fold."""
    z_m = np.array([logit(i.obs.ref) for i in instances])
    z_o = np.array([logit(p) for p in p_other])
    y = np.array([i.out.y for i in instances], float)
    preds = [0.0] * len(instances)
    weights = []
    for held in grouped_folds(instances, k, seed):
        hs = set(held)
        tr = np.array([i for i in range(len(instances)) if i not in hs])
        a, g = fit_blend(z_m[tr], z_o[tr], y[tr], lam)
        weights.append(g)
        for i in held:
            preds[i] = clip(float(expit(z_m[i] + a + g * (z_o[i] - z_m[i]))))
    return preds, weights


def predictions(
    models: dict[str, ProbabilityModel], instances: Sequence[Instance]
) -> dict[str, list[float]]:
    return {n: predict_all(m, instances) for n, m in models.items()}


# ------------------------------------------------------------- recalibrating a price-free model
def fit_platt(
    z: np.ndarray, y: np.ndarray, lam: float = 1e-3, iters: int = 50
) -> tuple[float, float]:
    """logit p~ = a + b * z : the two-parameter recalibration of a forecast with logit ``z``.

    Used to re-centre a *price-free* model (Model B) on the population it is scored on; parameters
    are fitted on research data only (spec s.20: parameter selection is a research-period task)."""
    a, b = 0.0, 1.0
    for _ in range(iters):
        eta = a + b * z
        p = 1 / (1 + np.exp(-eta))
        w = np.clip(p * (1 - p), 1e-9, None)
        g = np.array([np.sum(p - y), np.sum((p - y) * z) + lam * (b - 1)])
        H = np.array([[np.sum(w), np.sum(w * z)], [np.sum(w * z), np.sum(w * z * z) + lam]])
        step = np.linalg.solve(H, g)
        a, b = a - step[0], b - step[1]
        if np.max(np.abs(step)) < 1e-10:
            break
    return float(a), float(b)


def platt_cv_predict(
    instances: Sequence[Instance], p: Sequence[float], *, k: int = 5, seed: int = 0
) -> tuple[list[float], list[tuple[float, float]]]:
    """Out-of-fold recalibrated forecasts (event-grouped) and the (a, b) fitted in each fold."""
    z = np.array([logit(q) for q in p])
    y = np.array([i.out.y for i in instances], float)
    out = [0.0] * len(instances)
    params = []
    for held in grouped_folds(instances, k, seed):
        hs = set(held)
        tr = np.array([i for i in range(len(instances)) if i not in hs])
        a, b = fit_platt(z[tr], y[tr])
        params.append((a, b))
        for i in held:
            out[i] = clip(float(expit(a + b * z[i])))
    return out, params
