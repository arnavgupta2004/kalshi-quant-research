"""Does short-horizon microstructure information predict the next mid move?  (Experiment F, at the
horizon a market maker cares about.)

Stage 7/8 asked whether book features improve the *probability forecast* of the final outcome, and
found they did not.  A market maker is exposed to a different quantity: the mid's move over the next
seconds, while its quotes rest.  This module builds the labelled samples and fits the small linear
model that becomes the adaptive maker's fair-value shift.

    sample   one book snapshot of one market (>= ``min_gap_s`` after that market's previous sample)
    x_t      the causal features of ``market_making.features`` at that instant
    y_t      mid(t + h) - mid(t), dollars, from the recorded book (the last recorded mid at or
             before t + h; a sample is dropped if the market is not observed alive until t + h)

Model: ridge on standardised features, ``mu = beta . x`` with an intercept forced to zero (a fair
value must not have a built-in drift), fitted on the TRAINING databases only.  Skill is the relative
reduction of squared error against the zero forecast "the mid does not move", with event-cluster
bootstrap intervals on data the model did not see.
"""

from __future__ import annotations

import bisect
import math
import random
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from backtest.events import BookConfirm, BookUpdate, MarketClose, Settlement, TradeTick
from backtest.feed import StoreFeed
from market.contracts import Side
from market.timeutil import to_epoch_us
from market_making.features import FEATURES, MarketTracker
from market_making.shortterm import Ridge

SEC = 1_000_000_000
TRADE_DELAY_NS = 250_000_000  # what StoreFeed applies: a print is known this long after it happened


@dataclass
class Samples:
    ticker: list[str]
    event: list[str]
    category: list[str]
    ts: np.ndarray  # int64 ns
    x: np.ndarray  # n x len(FEATURES)
    mid: np.ndarray
    spread: np.ndarray
    ttr_h: np.ndarray  # hours to the scheduled end (nan if unknown)
    y: dict[float, np.ndarray]  # horizon (s) -> mid change; nan where not observable
    dataset: str = ""

    def __len__(self) -> int:
        return len(self.ts)

    def take(self, idx: np.ndarray) -> Samples:
        idx = np.asarray(idx)
        return Samples(
            [self.ticker[i] for i in idx],
            [self.event[i] for i in idx],
            [self.category[i] for i in idx],
            self.ts[idx], self.x[idx], self.mid[idx], self.spread[idx], self.ttr_h[idx],
            {h: v[idx] for h, v in self.y.items()},
            self.dataset,
        )  # fmt: skip

    @staticmethod
    def concat(parts: list[Samples]) -> Samples:
        return Samples(
            sum((p.ticker for p in parts), []),
            [f"{p.dataset}:{e}" for p in parts for e in p.event],
            sum((p.category for p in parts), []),
            np.concatenate([p.ts for p in parts]),
            np.concatenate([p.x for p in parts]),
            np.concatenate([p.mid for p in parts]),
            np.concatenate([p.spread for p in parts]),
            np.concatenate([p.ttr_h for p in parts]),
            {h: np.concatenate([p.y[h] for p in parts]) for h in parts[0].y},
            "+".join(p.dataset for p in parts),
        )


def build_samples(
    feed: StoreFeed,
    name: str,
    horizons: tuple[float, ...] = (5.0, 10.0, 30.0),
    min_gap_s: float = 1.0,
    max_book_age_s: float = 30.0,
    outage_s: float = 30.0,
) -> Samples:
    """Replay the recorded feed once and sample the features.

    RECORDING OUTAGES.  A recorder that stops (a laptop that sleeps) leaves a hole in the books but
    not in the trade tape, which is fetched afterwards.  Sampled at a print inside the hole, the
    features would describe a book that is hours old and the label ``mid(t+h) - mid(t)`` would be
    exactly zero (no book was recorded to move), which teaches every model that nothing predicts
    anything.  So (1) a sample needs a book observed within ``max_book_age_s`` (the strategy would
    not quote otherwise), and (2) a label is dropped when an outage - two consecutive polls more
    than ``outage_s`` apart - overlaps ``[t, t + h]``."""
    infos = feed.infos
    trackers: dict[str, MarketTracker] = defaultdict(MarketTracker)
    last_sample: dict[str, int] = {}
    rows: list[tuple] = []
    mid_t: dict[str, list[int]] = defaultdict(list)
    mid_v: dict[str, list[float]] = defaultdict(list)
    alive_until: dict[str, int] = {}
    end_ts = 0
    observed: list[int] = []  # times at which the recorder demonstrably saw the market
    max_age = int(max_book_age_s * SEC)
    n_confirms = 0
    for ev in feed:
        end_ts = max(end_ts, ev.ts_ns)
        if isinstance(ev, BookConfirm):
            observed.append(ev.ts_ns)
            n_confirms += 1
            for tr in trackers.values():
                tr.on_confirm(ev.ts_ns)
            continue
        if isinstance(ev, (MarketClose, Settlement)):
            alive_until.setdefault(ev.ticker, ev.ts_ns)
            continue
        if isinstance(ev, BookUpdate):
            observed.append(ev.ts_ns)
            tr = trackers[ev.ticker]
            tr.on_book(ev.ts_ns, ev.yes_bids, ev.no_bids)
            snap = tr.snapshot(ev.ts_ns)
            if snap is None:
                continue
            mid_t[ev.ticker].append(ev.ts_ns)
            mid_v[ev.ticker].append(snap.mid)
            if ev.ts_ns - last_sample.get(ev.ticker, -(10**18)) >= min_gap_s * SEC:
                last_sample[ev.ticker] = ev.ts_ns
                rows.append((ev.ticker, ev.ts_ns, snap))
        elif isinstance(ev, TradeTick):
            t = ev.trade
            sign = 1 if t.taker_side is Side.YES else -1 if t.taker_side is Side.NO else 0
            tr = trackers[ev.ticker]
            tr.on_trade(ev.ts_ns, t.yes_price, t.count, sign)
            # a trade is a decision instant too: the strategy re-prices on it, and it is the only
            # place ``stale_dev`` (trades newer than the last book) can be non-zero
            snap = tr.snapshot(ev.ts_ns)
            if (
                snap is not None
                and tr.book_ts is not None
                and ev.ts_ns - tr.book_ts <= max_age
                and ev.ts_ns - last_sample.get(ev.ticker, -(10**18)) >= min_gap_s * SEC
            ):
                last_sample[ev.ticker] = ev.ts_ns
                rows.append((ev.ticker, ev.ts_ns, snap))
    # outages: consecutive observations more than ``outage_s`` apart.  Only meaningful when the feed
    # carries poll confirmations (a feed of book changes alone is legitimately silent for a while).
    outages: list[tuple[int, int]] = []
    if n_confirms:
        observed.sort()
        outages = [
            (a, b) for a, b in zip(observed, observed[1:], strict=False) if b - a > outage_s * SEC
        ]
    out_end = [b for _, b in outages]
    n = len(rows)
    x = np.zeros((n, len(FEATURES)))
    ys = {h: np.full(n, np.nan) for h in horizons}
    mids, spreads, ts_arr, ttr = (
        np.zeros(n),
        np.zeros(n),
        np.zeros(n, dtype=np.int64),
        np.full(n, np.nan),
    )
    tickers, events, cats = [], [], []
    for i, (tk, ts, snap) in enumerate(rows):
        x[i], mids[i], spreads[i], ts_arr[i] = snap.x, snap.mid, snap.spread, ts
        info = infos[tk]
        tickers.append(tk)
        events.append(info.event_ticker)
        cats.append(info.category or "unknown")
        if info.scheduled_end is not None:
            ttr[i] = (to_epoch_us(info.scheduled_end) * 1000 - ts) / (3600 * SEC)
        times, vals = mid_t[tk], mid_v[tk]
        horizon_end = alive_until.get(tk, end_ts)
        for h in horizons:
            t1 = ts + int(h * SEC)
            if t1 > horizon_end or t1 > times[-1] + 40 * SEC:
                continue  # not observed alive until t + h (a heartbeat every 30 s bounds staleness)
            k = bisect.bisect_right(out_end, ts)  # first outage that ends after t
            if k < len(outages) and outages[k][0] < t1:
                continue  # an outage overlaps [t, t + h]: the label would be a frozen book
            j = bisect.bisect_right(times, t1) - 1
            ys[h][i] = vals[j] - snap.mid
    return Samples(tickers, events, cats, ts_arr, x, mids, spreads, ttr, ys, name)


# --------------------------------------------------------------------------- the model
def fit_ridge(
    s: Samples,
    names: tuple[str, ...],
    horizon_s: float,
    alpha: float,
    clip: float = 0.05,
    target: str = "change",
) -> Ridge:
    cols = [FEATURES.index(n) for n in names]
    y = s.y[horizon_s] if target == "change" else np.abs(s.y[horizon_s])
    ok = ~np.isnan(y) & np.isfinite(s.x[:, cols]).all(axis=1)
    X, yy = s.x[ok][:, cols], y[ok]
    center = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-12] = 1.0
    Z = (X - center) / scale
    b = float(yy.mean()) if target == "abs" else 0.0
    w = np.linalg.solve(Z.T @ Z + alpha * np.eye(len(cols)), Z.T @ (yy - b))
    return Ridge(names, tuple(center), tuple(scale), tuple(w), alpha, horizon_s, clip, b, target)


def grouped_folds(groups: list[str], k: int, seed: int = 0) -> list[np.ndarray]:
    """Event-grouped folds: siblings of one event share an outcome and a price path."""
    uniq = sorted(set(groups))
    rng = random.Random(seed)
    rng.shuffle(uniq)
    fold_of = {g: i % k for i, g in enumerate(uniq)}
    f = np.array([fold_of[g] for g in groups])
    return [np.flatnonzero(f == i) for i in range(k)]


def choose_alpha(
    s: Samples,
    names: tuple[str, ...],
    horizon_s: float,
    alphas=(1.0, 10.0, 100.0, 1000.0, 10_000.0),
    k: int = 5,
) -> float:
    """Event-grouped cross-validated squared error; the largest alpha within 1% of the best (prefer
    more shrinkage when it costs nothing)."""
    y = s.y[horizon_s]
    ok = np.flatnonzero(~np.isnan(y))
    sub = s.take(ok)
    folds = grouped_folds(sub.event, k)
    sse = {}
    for a in alphas:
        tot = 0.0
        for held in folds:
            tr = np.setdiff1d(np.arange(len(sub)), held)
            m = fit_ridge(sub.take(tr), names, horizon_s, a)
            p = m.predict(sub.x[held])
            tot += float(((sub.y[horizon_s][held] - p) ** 2).sum())
        sse[a] = tot
    best = min(sse.values())
    return max(a for a in alphas if sse[a] <= best * 1.01)


# --------------------------------------------------------------------------- scoring
def skill(s: Samples, model: Ridge, *, n_boot: int = 1000, seed: int = 0) -> dict:
    """1 - SSE(model) / SSE(zero forecast) on ``s``, with an event-cluster bootstrap, plus the
    correlation and the directional accuracy of the forecast."""
    h = model.horizon_s
    y = s.y[h]
    ok = ~np.isnan(y)
    if ok.sum() < 30:
        return {"n": int(ok.sum())}
    sub = s.take(np.flatnonzero(ok))
    yy = sub.y[h]
    p = model.predict(sub.x)
    e_m, e_0 = (yy - p) ** 2, yy**2
    clusters: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(sub.event):
        clusters[g].append(i)
    keys = list(clusters)
    sm = {g: (e_m[ix].sum(), e_0[ix].sum()) for g, ix in clusters.items()}
    point = 1 - e_m.sum() / e_0.sum()
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        a = b = 0.0
        for _ in keys:
            m0, m1 = sm[keys[rng.randrange(len(keys))]]
            a += m0
            b += m1
        if b > 0:
            draws.append(1 - a / b)
    draws.sort()
    moved = np.abs(yy) > 1e-9
    nz = np.abs(p) > 1e-9
    both = moved & nz
    corr = float(np.corrcoef(p, yy)[0, 1]) if p.std() > 0 and yy.std() > 0 else float("nan")
    return {
        "n": int(len(yy)),
        "events": len(keys),
        "skill_vs_zero": float(point),
        "lo": draws[int(0.025 * len(draws))] if len(draws) > 50 else None,
        "hi": draws[min(len(draws) - 1, int(0.975 * len(draws)))] if len(draws) > 50 else None,
        "corr": corr,
        "direction_accuracy": float((np.sign(p[both]) == np.sign(yy[both])).mean())
        if both.any()
        else None,
        "share_moved": float(moved.mean()),
        "rmse_zero": math.sqrt(float(e_0.mean())),
    }


def cv_predictions(
    s: Samples, names: tuple[str, ...], horizon_s: float, alpha: float, k: int = 5
) -> tuple[Samples, np.ndarray]:
    """Event-grouped cross-fitted predictions: every sample is predicted by a model that never saw
    its event.  Returns the samples with an observable label and the predictions."""
    ok = np.flatnonzero(~np.isnan(s.y[horizon_s]))
    sub = s.take(ok)
    pred = np.zeros(len(sub))
    for held in grouped_folds(sub.event, k):
        tr = np.setdiff1d(np.arange(len(sub)), held)
        m = fit_ridge(sub.take(tr), names, horizon_s, alpha)
        pred[held] = m.predict(sub.x[held])
    return sub, pred


def skill_of(
    sub: Samples, pred: np.ndarray, horizon_s: float, *, n_boot: int = 1000, seed: int = 0
) -> dict:
    """``skill`` for predictions already in hand (e.g. cross-fitted)."""

    class _Fixed:
        pass

    fixed = Ridge(("obi_top",), (0.0,), (1.0,), (1.0,), 0.0, horizon_s, clip=1.0)
    tmp = Samples(
        sub.ticker,
        sub.event,
        sub.category,
        sub.ts,
        np.zeros_like(sub.x),
        sub.mid,
        sub.spread,
        sub.ttr_h,
        sub.y,
        sub.dataset,
    )
    tmp.x[:, 0] = pred  # ``Ridge.predict`` then returns the given predictions unchanged
    return skill(tmp, fixed, n_boot=n_boot, seed=seed)


def abs_skill(s: Samples, model: Ridge, *, n_boot: int = 1000, seed: int = 0) -> dict:
    """Skill of a SCALE model for ``|mid change|``: 1 - SSE(model) / SSE(the training mean level),
    with an event-cluster bootstrap.  (The reference is the constant the model was centred on, so
    positive skill means the features tell the size of the next move apart from its average.)"""
    y = np.abs(s.y[model.horizon_s])
    ok = ~np.isnan(y)
    if ok.sum() < 30:
        return {"n": int(ok.sum())}
    sub = s.take(np.flatnonzero(ok))
    yy = y[ok]
    e_m = (yy - model.predict(sub.x)) ** 2
    e_0 = (yy - model.intercept) ** 2
    clusters: dict[str, list[int]] = defaultdict(list)
    for i, g in enumerate(sub.event):
        clusters[g].append(i)
    keys = list(clusters)
    sm = {g: (e_m[ix].sum(), e_0[ix].sum()) for g, ix in clusters.items()}
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        a = b = 0.0
        for _ in keys:
            m0, m1 = sm[keys[rng.randrange(len(keys))]]
            a += m0
            b += m1
        if b > 0:
            draws.append(1 - a / b)
    draws.sort()
    return {
        "n": int(len(yy)),
        "events": len(keys),
        "skill_vs_mean": float(1 - e_m.sum() / e_0.sum()),
        "lo": draws[int(0.025 * len(draws))] if len(draws) > 50 else None,
        "hi": draws[min(len(draws) - 1, int(0.975 * len(draws)))] if len(draws) > 50 else None,
        "corr": float(np.corrcoef(model.predict(sub.x), yy)[0, 1]),
    }
