"""Calibration analysis: is the market price (and each model) a calibrated probability?

Built on ``pricing.calibration``.  Everything is reported with event-cluster bootstrap intervals,
and every subgroup table shows how many independent *events* stand behind a row, because a gap
measured on eight events is not a finding.

Dimensions (spec s.11): market category, probability range, time to resolution, liquidity, size.

  time to resolution  hours from the prediction to the SCHEDULED end (public at the time).
  liquidity           contracts traded in the trailing hour (point in time: known when predicting).
  market size         LIFETIME volume of the market - known only after the fact, so it is a
                      *stratifier for the analysis*, never a feature; it inherits the survivorship
                      of the history dataset (every market here went on to trade).  Flagged.

Tercile edges for liquidity and size are fixed on the research period and applied unchanged to any
other period, so a group means the same thing in both.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from pricing.calibration import (
    IsotonicCalibrator,
    PlattCalibrator,
    boot_multi,
    calibration_in_the_large,
    calibration_regression,
    cluster_index,
    ece,
    edges_width,
    murphy,
)
from pricing.dataset import Instance
from pricing.plots import Point
from pricing.scoring import EPS
from research.arbitrage_analysis import est
from research.stats import cluster_bootstrap_mean

NAN = float("nan")
PRICE_RANGES = [
    ("<5%", 0.0, 0.05),
    ("5-20%", 0.05, 0.20),
    ("20-50%", 0.20, 0.50),
    ("50-80%", 0.50, 0.80),
    ("80-95%", 0.80, 0.95),
    (">=95%", 0.95, 1.0001),
]
TTR_BUCKETS = [
    ("<0.5h", -1e9, 0.5),
    ("0.5-1.5h", 0.5, 1.5),
    ("1.5-4h", 1.5, 4.0),
    ("4-12h", 4.0, 12.0),
    (">=12h", 12.0, 1e9),
]


@dataclass(frozen=True)
class Frame:
    """Forecasts, outcomes and the instances behind them."""

    name: str
    p: np.ndarray
    y: np.ndarray
    instances: tuple[Instance, ...]

    @classmethod
    def build(cls, name: str, instances: Sequence[Instance], preds: Sequence[float]) -> Frame:
        return cls(
            name,
            np.clip(np.asarray(preds, float), EPS, 1 - EPS),
            np.array([i.out.y for i in instances], float),
            tuple(instances),
        )

    def __len__(self) -> int:
        return len(self.y)

    @property
    def groups(self) -> list[str]:
        return [i.group for i in self.instances]

    def clusters(self) -> list[np.ndarray]:
        return cluster_index(self.groups)

    def where(self, mask: np.ndarray) -> Frame:
        idx = np.flatnonzero(mask)
        return Frame(self.name, self.p[idx], self.y[idx], tuple(self.instances[j] for j in idx))


def _reg(p: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    r = calibration_regression(p, y)
    return (NAN, NAN) if r is None else r


# -------------------------------------------------------------------- one forecast, all summaries
def summarize(fr: Frame, *, n_boot: int = 2000, seed: int = 0, bins: int = 10) -> dict:
    """Calibration summary of one set of forecasts, with event-cluster intervals."""
    edges = edges_width(bins)
    p, y = fr.p, fr.y

    def stat(idx: np.ndarray) -> tuple[float, float, float, float]:
        a, b = _reg(p[idx], y[idx])
        return (calibration_in_the_large(p[idx], y[idx]), a, b, ece(p[idx], y[idx], edges))

    citl, a, b, e = boot_multi(fr.clusters(), stat, n_boot=n_boot, seed=seed)
    m = murphy(p, y, edges)
    return {
        "n": len(fr),
        "events": len(set(fr.groups)),
        "base_rate": float(y.mean()),
        "mean_forecast": float(p.mean()),
        "calibration_in_the_large": est(citl),
        "intercept": est(a),
        "slope": est(b),
        "ece": est(e),
        "brier": m["brier"],
        "log_loss": float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))),
        "murphy": m,
    }


def bins_table(
    fr: Frame, *, edges: Sequence[float] | None = None, n_boot: int = 1000, seed: int = 0
) -> list[dict]:
    """Reliability diagram data: per bin, the frequency of YES with an event-cluster interval."""
    edges = list(edges or edges_width(10))
    out = []
    groups = fr.groups
    for j, (lo, hi) in enumerate(zip(edges, edges[1:], strict=False)):
        m = (fr.p >= lo) & ((fr.p <= hi) if j == len(edges) - 2 else (fr.p < hi))
        if not m.any():
            continue
        idx = np.flatnonzero(m)
        rows = [(groups[i], fr.y[i], fr.p[i]) for i in idx]
        f = cluster_bootstrap_mean(rows, lambda r: r[0], lambda r: r[1], n_boot=n_boot, seed=seed)
        out.append(
            {
                "bin": f"[{lo:.2f}, {hi:.2f})",
                "n": int(m.sum()),
                "events": len({r[0] for r in rows}),
                "mean_forecast": float(fr.p[m].mean()),
                "freq_yes": est(f),
            }
        )
    return out


def points(table: list[dict], min_n: int = 20) -> list[Point]:
    return [
        Point(
            r["mean_forecast"],
            r["freq_yes"]["value"],
            r["freq_yes"]["lo"],
            r["freq_yes"]["hi"],
            r["n"],
        )
        for r in table
        if r["n"] >= min_n
    ]


# --------------------------------------------------------------------------- subgroup calibration
def subgroup_row(
    fr: Frame,
    mask: np.ndarray,
    label: str,
    *,
    n_boot: int,
    seed: int,
    min_events_slope: int = 40,
    slope: bool = True,
) -> dict:
    sub = fr.where(mask)
    events = len(set(sub.groups))
    row = {"group": label, "n": len(sub), "events": events}
    if len(sub) < 10 or events < 2:
        return {**row, "note": "too few events for an interval"}
    p, y = sub.p, sub.y
    with_slope = slope and events >= min_events_slope

    def stat(idx: np.ndarray) -> tuple[float, ...]:
        citl = calibration_in_the_large(p[idx], y[idx])
        if not with_slope:
            return (citl,)
        return (citl, _reg(p[idx], y[idx])[1])

    res = boot_multi(sub.clusters(), stat, n_boot=n_boot, seed=seed)
    row.update(
        {
            "mean_forecast": float(p.mean()),
            "freq_yes": float(y.mean()),
            "calibration_in_the_large": est(res[0]),
            "slope": est(res[1]) if with_slope else None,
        }
    )
    return row


def subgroup_table(
    fr: Frame,
    groups: Sequence[tuple[str, Callable[[Instance], bool]]],
    *,
    n_boot: int = 1000,
    seed: int = 0,
    slope: bool = True,
) -> list[dict]:
    """``slope=False`` for groups defined by the forecast itself (probability ranges): a regression
    on a narrow span of the forecast is not identified, and only the gap is meaningful."""
    return [
        subgroup_row(
            fr, np.array([f(i) for i in fr.instances]), label, n_boot=n_boot, seed=seed, slope=slope
        )
        for label, f in groups
    ]


def _tercile_groups(
    values: Sequence[float], value_of: Callable[[Instance], float | None], label: str
) -> list[tuple[str, Callable[[Instance], bool]]]:
    """Low / mid / high groups with edges fixed at research quantiles.

    Where many values are zero (no trades in the hour) that mass is its own group and the positive
    values are split at their median, so no group is empty or duplicated."""
    v = np.array([x for x in values if x is not None], float)
    e1, e2 = (float(x) for x in np.quantile(v, [1 / 3, 2 / 3]))

    def grp(lo: float, hi: float) -> Callable[[Instance], bool]:
        return lambda i: (x := value_of(i)) is not None and lo < x <= hi

    if e1 <= 0:
        med = float(np.median(v[v > 0]))
        return [
            (f"{label}: none", grp(-math.inf, 0.0)),
            (f"{label}: low (0, {med:g}]", grp(0.0, med)),
            (f"{label}: high (> {med:g})", grp(med, math.inf)),
        ]
    return [
        (f"{label}: low (<= {e1:g})", grp(-math.inf, e1)),
        (f"{label}: mid ({e1:g}, {e2:g}]", grp(e1, e2)),
        (f"{label}: high (> {e2:g})", grp(e2, math.inf)),
    ]


def dimensions(
    research: Sequence[Instance], volume_by_ticker: dict[str, float]
) -> dict[str, list[tuple[str, Callable[[Instance], bool]]]]:
    """The spec's five dimensions, with liquidity/size edges fixed on the research period."""
    cats = {}
    for i in research:
        cats[i.obs.category] = cats.get(i.obs.category, 0) + 1
    top = [c for c, n in sorted(cats.items(), key=lambda kv: -kv[1]) if n >= 100][:6]
    liq = lambda i: i.obs.trade.volume[3600]  # noqa: E731  contracts in the trailing hour
    size = lambda i: volume_by_ticker.get(i.obs.ticker)  # noqa: E731  lifetime, post hoc
    return {
        "category": [(c, lambda i, c=c: i.obs.category == c) for c in top]
        + [("other", lambda i: i.obs.category not in top)],
        "probability_range": [
            (name, lambda i, lo=lo, hi=hi: lo <= i.obs.ref < hi) for name, lo, hi in PRICE_RANGES
        ],
        "time_to_resolution": [
            (
                name,
                lambda i, lo=lo, hi=hi: (
                    i.obs.hours_to_expiry is not None and lo <= i.obs.hours_to_expiry < hi
                ),
            )
            for name, lo, hi in TTR_BUCKETS
        ],
        "liquidity_trailing_hour_volume": _tercile_groups(
            [liq(i) for i in research], liq, "volume"
        ),
        "market_size_lifetime_volume": _tercile_groups(
            [size(i) / 100 for i in research if size(i) is not None],
            lambda i: None if size(i) is None else size(i) / 100,
            "lifetime",
        ),
    }


# -------------------------------------------------------------------- recalibration, out of sample
def recalibration_test(fit: Frame, target: Frame, *, n_boot: int = 1000, seed: int = 0) -> dict:
    """Fit Platt and isotonic recalibrators on ``fit`` (research) and apply them UNCHANGED to
    ``target``: does correcting the forecast improve it on data the correction has never seen?"""
    from pricing import evaluation as E  # paired loss differences with event clustering

    out: dict = {}
    for name, cal in (("platt", PlattCalibrator()), ("isotonic", IsotonicCalibrator())):
        cal.fit(fit.p, fit.y)
        q = cal.predict(target.p)
        row: dict = {}
        for kind in ("log", "brier"):
            row[kind] = est(
                E.paired_difference(
                    target.instances, list(q), list(target.p), kind, n_boot=n_boot, seed=seed
                )
            )
        if isinstance(cal, PlattCalibrator):
            row["fitted_intercept"], row["fitted_slope"] = cal.a, cal.b
        out[name] = row
    return out


def recalibration_cv(fr: Frame, *, k: int = 5, n_boot: int = 1000, seed: int = 0) -> dict:
    """The development-period version: event-grouped cross-fitting inside one period (each instance
    is recalibrated by a calibrator that never saw its event), then the same paired comparison."""
    from pricing import evaluation as E

    out: dict = {}
    for name, make in (("platt", PlattCalibrator), ("isotonic", IsotonicCalibrator)):
        q = np.zeros(len(fr))
        fitted = []
        for held in E.grouped_folds(fr.instances, k, seed):
            hs = set(held)
            tr = np.array([i for i in range(len(fr)) if i not in hs])
            cal = make().fit(fr.p[tr], fr.y[tr])
            q[held] = cal.predict(fr.p[held])
            if isinstance(cal, PlattCalibrator):
                fitted.append((cal.a, cal.b))
        row: dict = {}
        for kind in ("log", "brier"):
            row[kind] = est(
                E.paired_difference(
                    fr.instances, list(q), list(fr.p), kind, n_boot=n_boot, seed=seed
                )
            )
        if fitted:
            row["fitted_a_b_per_fold"] = fitted
        out[name] = row
    return out
