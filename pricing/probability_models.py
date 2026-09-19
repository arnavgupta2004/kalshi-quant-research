"""Fair-probability models: P(X = 1 | information at t).

Every model maps an ``Observation`` (never an outcome) to a probability in ``[EPS, 1 - EPS]``.

    Baselines            the market's own numbers: reference price, microprice, last trade. The bar
    any model must clear - and what "the market says" means. MicrostructureLogit  MODEL A.  Starts
    from the market's reference price and learns a *correction* from what the tape and the book
    show: signed flow, short-term moves, spread, imbalance, microprice, staleness, time to expiry.
    Ridge-shrunk toward the market, so with no signal it returns the market price unchanged.
    HistoricalFrequency  MODEL B.  Never sees a price.  A hierarchical empirical-Bayes base rate:
    how often markets of this kind (category -> series -> series x strike type x event size)
    resolved YES *before* t.  Independent of the market's view: its disagreement with the market is
    information, its agreement is corroboration.

Why A is an offset model.  ``logit p = logit(p_ref) + a + w.x`` rather than a free logistic
regression on the features: the market price is by far the best single predictor, and relearning it
from noisy features wastes data.  The question A answers is precisely "does anything the market
*shows* but does not *say* add information?"  Whether the reference price itself is well calibrated
(a slope on ``logit p_ref``) is Stage 8's question; ``slope=True`` exposes it here only as an
option.

Why B is honest about being weak.  A base rate cannot know the score of a game.  For balanced
two-outcome markets it is close to 0.5.  Its value is as an independent floor, a prior, and a sanity
check on where the market is doing all the work.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from pricing.dataset import CorpusRow, Instance, Observation, n_bucket
from pricing.scoring import EPS, clip, expit, logit

DAY_NS = 86_400 * 1_000_000_000


class ProbabilityModel(Protocol):
    name: str

    def fit(self, train: Sequence[Instance]) -> ProbabilityModel: ...

    def predict(self, obs: Observation) -> float: ...


def predict_all(model: ProbabilityModel, instances: Sequence[Instance]) -> list[float]:
    return [model.predict(i.obs) for i in instances]


# --------------------------------------------------------------------------- baselines
@dataclass
class ReferencePrice:
    """The market's own point estimate (book mid / trade mid / last trade)."""

    name: str = "market_reference"

    def fit(self, train):  # noqa: ANN001
        return self

    def predict(self, obs: Observation) -> float:
        return clip(obs.ref)


@dataclass
class Microprice:
    """Size-weighted mid; falls back to the reference price when there is no book."""

    name: str = "microprice"

    def fit(self, train):  # noqa: ANN001
        return self

    def predict(self, obs: Observation) -> float:
        return clip(obs.book.microprice if obs.book is not None else obs.ref)


@dataclass
class LastTrade:
    name: str = "last_trade"

    def fit(self, train):  # noqa: ANN001
        return self

    def predict(self, obs: Observation) -> float:
        p = obs.trade.last_price
        return clip(obs.ref if p is None else p)


# --------------------------------------------------------------------------- Model A
def _val(x: float | None, default: float = 0.0) -> float:
    return default if x is None else x


#: name, group, extractor(obs) -> float | None  (None = not computable at t)
FEATURES: list[tuple[str, str, Callable[[Observation], float | None]]] = [
    ("flow_5m", "trade", lambda o: o.trade.flow[300]),
    ("flow_1h", "trade", lambda o: o.trade.flow[3600]),
    ("ret_5m", "trade", lambda o: o.trade.ret[300]),
    ("ret_1h", "trade", lambda o: o.trade.ret[3600]),
    ("log_volume_1h", "trade", lambda o: math.log1p(o.trade.volume[3600])),
    (
        "log_secs_since_trade",
        "trade",
        lambda o: (
            None
            if o.trade.secs_since_last is None
            else math.log1p(min(max(o.trade.secs_since_last, 0.0), 86_400.0))
        ),
    ),
    (
        "last_minus_ref",
        "trade",
        lambda o: None if o.trade.last_price is None else o.trade.last_price - o.ref,
    ),
    ("trade_spread", "trade", lambda o: o.trade.tspread),
    ("spread", "book", lambda o: None if o.book is None else o.book.spread),
    ("imbalance_top", "book", lambda o: None if o.book is None else o.book.imb_top),
    ("imbalance_depth", "book", lambda o: None if o.book is None else o.book.imb_depth),
    (
        "micro_minus_mid",
        "book",
        lambda o: None if o.book is None else o.book.microprice - o.book.mid,
    ),
    ("log_depth", "book", lambda o: None if o.book is None else o.book.log_depth),
    (
        "hours_to_expiry",
        "time",
        lambda o: None if o.hours_to_expiry is None else min(max(o.hours_to_expiry, -6.0), 72.0),
    ),
]


@dataclass
class MicrostructureLogit:
    """MODEL A: ``logit p = logit(p_ref) + a + w . x``; ridge on ``w`` (and the slope if on)."""

    lam: float = 20.0
    slope: bool = False
    groups: tuple[str, ...] = ("trade", "time")
    name: str = "microstructure_logit"
    max_iter: int = 60
    tol: float = 1e-9
    # fitted state
    cols: list[str] = field(default_factory=list)
    mean: np.ndarray | None = None
    std: np.ndarray | None = None
    theta: np.ndarray | None = None
    n_train: int = 0
    _indicator_idx: list[int] = field(default_factory=list, repr=False)

    def _spec(self) -> list[tuple[str, Callable[[Observation], float | None]]]:
        return [(n, f) for n, g, f in FEATURES if g in self.groups]

    def _raw(self, obs: Observation) -> tuple[np.ndarray, np.ndarray]:
        spec = self._spec()
        vals = np.array([_val(f(obs), np.nan) for _, f in spec], dtype=float)
        return vals, np.isnan(vals)

    def _design(self, X: np.ndarray, miss: np.ndarray) -> np.ndarray:
        Z = (X - self.mean) / self.std
        Z = np.where(miss, 0.0, Z)  # a missing value sits at the training mean: it says nothing
        ind = miss[:, self._indicator_idx].astype(float)  # informative-missingness flags
        return np.hstack([np.ones((len(X), 1)), Z, ind])

    def fit(self, train: Sequence[Instance]) -> MicrostructureLogit:
        rows = [self._raw(i.obs) for i in train]
        X = np.array([r[0] for r in rows])
        miss = np.array([r[1] for r in rows])
        y = np.array([i.out.y for i in train], dtype=float)
        z_ref = np.array([logit(i.obs.ref) for i in train])
        names = [n for n, _ in self._spec()]
        seen = (~miss).sum(axis=0)
        filled = np.where(miss, 0.0, X)
        self.mean = filled.sum(axis=0) / np.maximum(seen, 1)
        var = (np.where(miss, 0.0, (X - self.mean) ** 2)).sum(axis=0) / np.maximum(seen, 1)
        self.std = np.where(var > 1e-24, np.sqrt(var), 1.0)
        # a flag for a feature that is sometimes missing and sometimes not (constant flags carry
        # nothing)
        self._indicator_idx = [j for j in range(len(names)) if 0 < miss[:, j].mean() < 1]
        D = self._design(X, miss)
        cols = ["intercept", *names, *[f"missing:{names[j]}" for j in self._indicator_idx]]
        if self.slope:
            D = np.hstack([D, z_ref[:, None]])
            cols.append("logit_ref_slope-1")
        self.cols = cols
        pen = np.full(D.shape[1], self.lam)
        pen[0] = 1e-8  # the intercept is not shrunk
        theta = np.zeros(D.shape[1])
        for _ in range(self.max_iter):
            eta = z_ref + D @ theta
            p = 1.0 / (1.0 + np.exp(-eta))
            g = D.T @ (p - y) + pen * theta
            w = np.clip(p * (1 - p), 1e-9, None)
            H = (D * w[:, None]).T @ D + np.diag(pen)
            step = np.linalg.solve(H, g)
            theta -= step
            if np.max(np.abs(step)) < self.tol:
                break
        self.theta = theta
        self.n_train = len(train)
        return self

    def predict(self, obs: Observation) -> float:
        if self.theta is None:
            raise RuntimeError("model not fitted")
        x, miss = self._raw(obs)
        D = self._design(x[None, :], miss[None, :])
        z_ref = logit(obs.ref)
        if self.slope:
            D = np.hstack([D, [[z_ref]]])
        return clip(expit(z_ref + float(D[0] @ self.theta)))

    def coefficients(self) -> dict[str, float]:
        if self.theta is None:
            raise RuntimeError("model not fitted")
        return dict(zip(self.cols, (float(v) for v in self.theta), strict=True))


# --------------------------------------------------------------------------- Model C
@dataclass
class PartitionNormalizer:
    """MODEL C: for outcomes that are mutually exclusive AND exhaustive, exactly one is YES, so the
    true probabilities sum to 1.  Market prices need not (a spread, stale prints and ``vig`` push
    the sum away from 1); dividing each by the sum projects them back onto the constraint.  Uses
    only the Stage 3 relation and the siblings' prices at t.  Where no partition is known it returns
    the reference."""

    name: str = "partition_normalized"

    def fit(self, train):  # noqa: ANN001
        return self

    def applies(self, obs: Observation) -> bool:
        return obs.event_refs is not None and obs.event_pos is not None

    def predict(self, obs: Observation) -> float:
        if not self.applies(obs):
            return clip(obs.ref)
        total = sum(obs.event_refs)
        return clip(obs.event_refs[obs.event_pos] / total) if total > 0 else clip(obs.ref)


# --------------------------------------------------------------------------- Model B
class _Counts:
    __slots__ = ("n", "yes")

    def __init__(self) -> None:
        self.n = 0
        self.yes = 0


def _keys(category: str, series: str, strike_type: str, nb: str) -> tuple[tuple, ...]:
    """The hierarchy, coarse to fine.  Each level shrinks toward the one above."""
    return (
        (),
        (category,),
        (category, series),
        (category, series, strike_type, nb),
    )


@dataclass
class HistoricalFrequency:
    """MODEL B: hierarchical Beta-Binomial base rate learned only from markets settled before t.

    p_level = (yes + kappa * p_parent) / (n + kappa), from the global rate down to (category,
    series, strike type, event size).  ``kappa`` is the strength of the pull toward the parent
    (pseudo-counts); it is chosen prequentially on the corpus itself (``prequential_kappa``), never
    on the instances being scored.  The model is re-fitted at each UTC midnight so a prediction uses
    only outcomes settled before its own day."""

    corpus: Sequence[CorpusRow]
    kappa: float = 10.0
    name: str = "historical_frequency"
    prior: float = 0.5  # used before any history exists
    _cache: dict[int, dict[tuple, _Counts]] = field(default_factory=dict, repr=False)

    def fit(self, train):  # noqa: ANN001  (independent of the training instances by design)
        return self

    def _counts_asof(self, as_of_ns: int) -> dict[tuple, _Counts]:
        day = as_of_ns // DAY_NS
        if day not in self._cache:
            c: dict[tuple, _Counts] = defaultdict(_Counts)
            for r in self.corpus:
                if r.settled_ns >= day * DAY_NS:
                    break  # the corpus is time-ordered
                for k in _keys(r.category, r.series, r.strike_type, r.n_bucket):
                    c[k].n += 1
                    c[k].yes += r.y
            self._cache[day] = c
        return self._cache[day]

    def rate(
        self, counts: dict[tuple, _Counts], category: str, series: str, strike: str, nb: str
    ) -> float:
        p = self.prior
        for k in _keys(category, series, strike, nb):
            c = counts.get(k)
            if c is not None:
                p = (c.yes + self.kappa * p) / (c.n + self.kappa)
        return p

    def predict(self, obs: Observation) -> float:
        counts = self._counts_asof(obs.t_ns)
        return clip(
            self.rate(counts, obs.category, obs.series, obs.strike_type, n_bucket(obs.n_siblings))
        )


def prequential_kappa(
    corpus: Sequence[CorpusRow],
    grid: Sequence[float] = (1.0, 3.0, 10.0, 30.0, 100.0),
    sample_every: int = 7,
    warmup: int = 1000,
) -> dict[float, float]:
    """Mean log loss of predicting each corpus market from strictly earlier *events*, per ``kappa``.

    Prequential (predict, then learn) on the corpus alone, so the shrinkage strength is chosen on
    data that has nothing to do with the instances a model is later scored on.  Learning happens one
    whole event at a time: siblings of an event settle together and their outcomes are jointly
    determined, so predicting one after learning another would leak the answer."""
    by_event: dict[str, list[CorpusRow]] = defaultdict(list)
    for r in corpus:
        by_event[r.event].append(r)
    events = sorted(by_event.values(), key=lambda rs: min(r.settled_ns for r in rs))
    out = {}
    for kappa in grid:
        counts: dict[tuple, _Counts] = defaultdict(_Counts)
        model = HistoricalFrequency(corpus=[], kappa=kappa)
        total, n, seen = 0.0, 0, 0
        for rs in events:
            for r in rs:
                if seen >= warmup and n_tick(seen, sample_every):
                    p = clip(model.rate(counts, r.category, r.series, r.strike_type, r.n_bucket))
                    total -= math.log(p if r.y else 1 - p)
                    n += 1
                seen += 1
            for r in rs:  # learn only after the whole event has been predicted
                for k in _keys(r.category, r.series, r.strike_type, r.n_bucket):
                    counts[k].n += 1
                    counts[k].yes += r.y
        out[kappa] = total / n if n else float("nan")
    return out


def n_tick(i: int, every: int) -> bool:
    return i % every == 0


ALL_BOUNDS = (EPS, 1.0 - EPS)  # the interval every model's output lies in (asserted in the tests)
