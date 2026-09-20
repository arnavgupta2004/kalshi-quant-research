"""Stage 10 evaluation: fit the small models on training data, run the ablation ladder, and compare
every variant with the Stage 9 baseline on the SAME events.

Roles (each database is used for exactly one)
  training     ``books_research_short`` + ``books_research_wide``: the short-horizon models are
               fitted here.  Strategy results on these databases are IN-SAMPLE for the models and
               are labelled so.  (Their Stage 9 baseline results were already public when Stage 10
               was designed, which is why they cannot be the confirmatory set.)
  validation   ``books_shortlived`` + ``books_structural``: never seen by the fit; used to admit or
               reject model components and to choose ``kappa``.
  confirmatory ``books_stage10``: recorded AFTER the design was fixed, opened once.

The primary comparison is PAIRED BY EVENT: for each event, the hold-to-settlement P&L (after fees)
of variant X minus that of the baseline over the same settled markets, with an event-cluster
bootstrap on the mean difference.  A variant that trades more or less than the baseline is thereby
compared on the money it made, not on a rate that depends on how much it traded.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

import numpy as np

from market_making.adaptive import AdaptiveMarketMaker, AdaptiveParams
from market_making.baseline import BaselineParams
from market_making.features import DIRECTION_FEATURES
from market_making.shortterm import Ridge
from research import market_making_analysis as A
from research import signal_study as S
from research.arbitrage_analysis import est
from research.stats import cluster_bootstrap_mean, cluster_bootstrap_ratio

FV_HORIZON_S = 5.0  # the median lifetime of a Stage 9 quote was ~5 s
SCALE_HORIZON_S = 10.0
STALE_FEATURES = ("stale_dev", "stale_out")
ALPHAS = (1.0, 10.0, 100.0, 1000.0, 10_000.0)


@dataclass(frozen=True)
class Models:
    fv_stale: Ridge  # the staleness correction: what a live feed would already show
    fv_full: Ridge  # every direction feature (order-book imbalance, microprice, flow, momentum)
    scale: Ridge  # expected |mid change| over ``SCALE_HORIZON_S`` from realised movement
    sigma_ref: float  # median forecast scale on the training data
    training: tuple[str, ...]


def fit_models(train: S.Samples, names: tuple[str, ...] = ()) -> Models:
    a_stale = S.choose_alpha(train, STALE_FEATURES, FV_HORIZON_S, ALPHAS)
    a_full = S.choose_alpha(train, DIRECTION_FEATURES, FV_HORIZON_S, ALPHAS)
    stale = S.fit_ridge(train, STALE_FEATURES, FV_HORIZON_S, a_stale)
    full = S.fit_ridge(train, DIRECTION_FEATURES, FV_HORIZON_S, a_full)
    scale = S.fit_ridge(train, ("rv_60",), SCALE_HORIZON_S, 1000.0, clip=0.5, target="abs")
    ok = ~np.isnan(train.y[SCALE_HORIZON_S])
    pred = np.maximum(scale.predict(train.x[ok]), 1e-6)
    return Models(stale, full, scale, calibrate_sigma_ref(pred), names)


def calibrate_sigma_ref(pred: np.ndarray, lo: float = 0.2, hi: float = 2.0) -> float:
    """``sigma_ref`` such that the mean size fraction ``clip(sigma_ref / sigma, lo, hi)`` is 1 on
    the training forecasts: the size rule then reallocates size without changing the average."""
    a, b = float(pred.min()) * 0.01, float(pred.max()) * 100
    for _ in range(80):
        mid = math.sqrt(a * b)
        if np.clip(mid / pred, lo, hi).mean() < 1.0:
            a = mid
        else:
            b = mid
    return math.sqrt(a * b)


def variants(m: Models, base: BaselineParams, kappa: float) -> dict[str, AdaptiveParams | None]:
    """The ablation ladder (each rung adds one component) plus leave-one-out from the full model.
    ``None`` = the Stage 9 baseline itself."""
    P = AdaptiveParams
    full = P(
        base=base, fv=m.fv_stale, scale=m.scale, kappa=kappa, size_by_scale=True,
        sigma_ref=m.sigma_ref, ttr_skew=1.0,
    )  # fmt: skip
    return {
        "0 baseline (Stage 9)": None,
        "1 + re-price on prints (no model)": P(base=base),
        "2 + fair value: staleness correction": P(base=base, fv=m.fv_stale),
        "3 + fair value: staleness + order book, flow, momentum": P(base=base, fv=m.fv_full),
        "4 + widening by expected move": P(base=base, fv=m.fv_stale, scale=m.scale, kappa=kappa),
        "5 + size by expected move": replace(
            P(base=base, fv=m.fv_stale, scale=m.scale, kappa=kappa),
            size_by_scale=True,
            sigma_ref=m.sigma_ref,
        ),
        "6 + time-to-resolution skew (= FULL)": full,
        "full - fair value": replace(full, fv=None),
        "full - widening": replace(full, kappa=0.0),
        "full - size": replace(full, size_by_scale=False),
        "full - time skew": replace(full, ttr_skew=0.0),
    }


def run_variant(
    data: A.MMData,
    params: AdaptiveParams | None,
    base: BaselineParams,
    maker: str,
    latency_ms: float,
) -> A.Run:
    """``params=None`` runs the Stage 9 baseline through its own code path."""
    if params is None:
        return A.run_mm(data, A.RunConfig(base, maker, latency_ms))
    from backtest.engine import BacktestConfig, run_backtest
    from backtest.fills import TakerModel

    ns = int(latency_ms * 1_000_000)
    cfg = BacktestConfig(
        initial_cash_micro=10_000 * A.MICRO,
        latency_submit_ns=ns,
        latency_ack_ns=ns,
        latency_cancel_ns=ns,
        taker=TakerModel(max_staleness_ns=30 * A.SEC),
        maker=A.MAKER_MODELS[maker](),
        fees=data.fees,
        equity_sample_ns=60 * A.SEC,
    )
    mm = AdaptiveMarketMaker(data.tickers, params, event_of=data.event_of, worlds=data.worlds)
    res = run_backtest(data.feed, data.infos, mm, cfg)
    return A.Run(data, A.RunConfig(params.base, maker, latency_ms, strategy="adaptive"), res, mm)


# --------------------------------------------------------------------------- paired comparison
def event_pnl(run: A.Run, fills: list[A.FillRow] | None = None) -> dict[str, float]:
    """Hold-to-settlement P&L after fees by event, over SETTLED markets only, in dollars.  Every
    event with a settled market appears (zero if the variant never filled there), so two variants
    are compared over the same set of events."""
    fills = fills if fills is not None else A.fill_table(run)
    d = run.data
    out = {f"{d.name}:{d.event_of[t]}": 0.0 for t in d.settle}
    for f in fills:
        if f.settle_pnl is not None:
            out[f.cluster] = out.get(f.cluster, 0.0) + (f.settle_pnl - f.fee) / A.MICRO
    return out


def paired_difference(
    x: dict[str, float], base: dict[str, float], *, n_boot: int = 1000, seed: int = 0
) -> dict:
    keys = sorted(set(x) | set(base))
    diff = [(k, x.get(k, 0.0) - base.get(k, 0.0)) for k in keys]
    e = cluster_bootstrap_mean(diff, lambda r: r[0], lambda r: r[1], n_boot=n_boot, seed=seed)
    return {
        **est(e),
        "events": len(keys),
        "total_x": sum(x.values()),
        "total_base": sum(base.values()),
    }


def paired_ratio_difference(
    fx: list[A.FillRow],
    fb: list[A.FillRow],
    num,
    den,
    *,
    keys: set[str] | None = None,
    n_boot: int = 1000,
    seed: int = 0,
) -> dict:
    """``sum(num)/sum(den)`` for variant X minus the same for the baseline, resampling EVENTS
    together (a common draw for both, so the difference is paired).  Used for rates per contract:
    a variant that simply trades less is then not credited for it."""
    import random

    per: dict[str, list[float]] = {}
    for tag, fills in ((0, fx), (1, fb)):
        for f in fills:
            n, d = num(f), den(f)
            if n is None:
                continue
            acc = per.setdefault(f.cluster, [0.0, 0.0, 0.0, 0.0])
            acc[2 * tag] += n
            acc[2 * tag + 1] += d
    for k in keys or ():
        per.setdefault(k, [0.0, 0.0, 0.0, 0.0])
    ev = sorted(per)

    def stat(idx) -> float | None:
        s = [0.0] * 4
        for i in idx:
            for j in range(4):
                s[j] += per[ev[i]][j]
        return None if s[1] <= 0 or s[3] <= 0 else s[0] / s[1] - s[2] / s[3]

    point = stat(range(len(ev)))
    if point is None or len(ev) < 2:
        return {"value": point, "lo": None, "hi": None, "n_clusters": len(ev)}
    rng = random.Random(seed)
    draws = sorted(
        v for _ in range(n_boot) if (v := stat([rng.randrange(len(ev)) for _ in ev])) is not None
    )
    return {
        "value": point,
        "lo": draws[int(0.025 * len(draws))],
        "hi": draws[min(len(draws) - 1, int(0.975 * len(draws)))],
        "n_clusters": len(ev),
    }


SETTLED = (
    lambda f: None if f.settle_pnl is None else (f.settle_pnl - f.fee) / A.MICRO * 100.0,  # cents
    lambda f: f.qty / 100.0,
)  # (numerator, denominator): settled P&L in CENTS per contract
MARKOUT = (
    lambda f: (
        None if f.markout30 is None else f.markout30 / 100.0 * f.qty / 100.0
    ),  # cents x contracts
    lambda f: f.qty / 100.0,
)


def rate(fills: list[A.FillRow], nd, *, n_boot: int, seed: int) -> dict:
    rows = [f for f in fills if nd[0](f) is not None]
    return est(
        cluster_bootstrap_ratio(rows, lambda f: f.cluster, nd[0], nd[1], n_boot=n_boot, seed=seed)
    )


def ladder(
    datas: list[A.MMData],
    models: Models,
    base: BaselineParams,
    kappa: float,
    maker: str,
    latency_ms: float,
    *,
    n_boot: int = 1000,
    seed: int = 0,
    only: set[str] | None = None,
) -> dict:
    """Every variant on every database, pooled, each compared with the baseline on the same events.
    Rates are PER CONTRACT, in cents, so a variant that trades less is not flattered."""
    out: dict[str, dict] = {}
    keys: set[str] = set()
    for name, params in variants(models, base, kappa).items():
        if only is not None and name not in only and not name.startswith("0"):
            continue
        runs = [run_variant(d, params, base, maker, latency_ms) for d in datas]
        evals = [A.evaluate(r, n_boot=10) for r in runs]
        fills = [f for e in evals for f in e["_fills"]]
        event = {}
        for r, e in zip(runs, evals, strict=True):
            event.update(event_pnl(r, e["_fills"]))
        keys |= set(event)
        p = A.pooled(evals, n_boot=n_boot, seed=seed)
        row = {
            "net_usd": p["net"],
            "fees_usd": p["fees"],
            "fills": len(fills),
            "contracts": sum(f.qty for f in fills) / 100.0,
            "settled_pnl_cents_per_contract": rate(fills, SETTLED, n_boot=n_boot, seed=seed),
            "markout_30s_cents_per_contract": rate(fills, MARKOUT, n_boot=n_boot, seed=seed),
            "max_drawdown_usd": p["max_drawdown_worst_db"],
            "worst_event_settled_usd": p["worst_event_settled"],
            "mean_abs_inventory": p["mean_abs_inventory_contracts"],
            "killed": p["killed"],
            "settled_pnl_total_usd": sum(event.values()),
        }
        out[name] = {"row": row, "_fills": fills, "_event": event}

    def compare(x: str, y: str) -> dict:
        return {
            "event_pnl_total_usd": paired_difference(
                out[x]["_event"], out[y]["_event"], n_boot=n_boot, seed=seed
            ),
            "settled_cents_per_contract": paired_ratio_difference(
                out[x]["_fills"], out[y]["_fills"], *SETTLED, keys=keys, n_boot=n_boot, seed=seed
            ),
            "markout_cents_per_contract": paired_ratio_difference(
                out[x]["_fills"], out[y]["_fills"], *MARKOUT, keys=keys, n_boot=n_boot, seed=seed
            ),
        }

    first = next(n for n in out if n.startswith("0"))
    for name in out:
        if name != first:
            out[name]["row"]["vs_baseline"] = compare(name, first)
    by_num = {n.split()[0]: n for n in out if n[0].isdigit()}
    pairs = {
        "2 vs 1": ("2", "1"),
        "3 vs 2": ("3", "2"),
        "4 vs 2": ("4", "2"),
        "5 vs 4": ("5", "4"),
        "6 vs 5": ("6", "5"),
    }
    contrasts = {
        label: compare(by_num[x], by_num[y])
        for label, (x, y) in pairs.items()
        if x in by_num and y in by_num
    }
    return {
        "rows": {n: v["row"] for n, v in out.items()},
        "contrasts": contrasts,
        "_fills": {n: v["_fills"] for n, v in out.items()},
    }


def signal_replication(models: Models, s: S.Samples, *, n_boot: int = 1000, seed: int = 0) -> dict:
    """The Experiment F claims, re-scored on data the models never saw (frozen coefficients)."""
    return {
        "fair_value_staleness": S.skill(s, models.fv_stale, n_boot=n_boot, seed=seed),
        "fair_value_all_direction_features": S.skill(s, models.fv_full, n_boot=n_boot, seed=seed),
        "scale_of_next_move": S.abs_skill(s, models.scale, n_boot=n_boot, seed=seed),
        "samples": len(s),
    }
