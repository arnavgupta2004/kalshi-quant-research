"""Experiments A-C and the category study: frequency, executability, lifetime and edge decay.

Every function takes a ``ScanResult`` (episodes + exposure) and returns plain dicts (JSON-ready),
with intervals from a cluster bootstrap over *events*.  Two populations are reported side by side:

  ``sightings``   every episode: a violated constraint seen in the book we had at that instant.
  ``confirmed``   episodes still there at the end of a *later* poll cycle (``Episode.persisted``).

Why both.  Books of one poll cycle arrive in chunks a fraction of a second apart, so in a fast
market a cross-market "violation" can be two moments stitched together: it appears when the first
chunk lands and vanishes when the second does.  On the first development dataset 83 of 86 episodes
were exactly that (each lived ~0.1 s, the gap between two chunks; only 2 persisted).  Such a
sighting may be a real stale-quote race or a snapshot artefact; polling data cannot say which.
``confirmed`` episodes cannot be composites, so they are the evidence that a *standing* violation
existed; ``sightings`` is the upper bound on anything a faster feed might have raced.  The truth
about "how often is there an opportunity" lies between the two, and the gap is itself a result.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from market.semantics import EvidenceLevel
from research.scanner import STAGES, Episode, ScanResult
from research.stats import (
    Estimate,
    Lifetime,
    cluster_bootstrap,
    cluster_bootstrap_mean,
    cluster_bootstrap_ratio,
    concentration,
    exp_interval_mle,
    kaplan_meier,
    km_at,
    km_quantile,
    spearman,
    survival_bounds,
    wilson_ci,
)

SEC = 1_000_000_000
TICKS_PER_CENT = 100
MICRO = 1_000_000
LIFETIME_GRID_S = (0.0, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60, 120, 300)


@dataclass(frozen=True)
class Params:
    n_boot: int = 2000
    seed: int = 20260919
    min_level: str = "DECLARED"  # weakest evidence level counted as "a real relation"
    alpha: float = 0.05
    low_n_clusters: int = 5  # fewer independent events than this: flag the row, do not trust it

    @property
    def level(self) -> EvidenceLevel:
        return EvidenceLevel[self.min_level]


def est(e: Estimate, scale: float = 1.0) -> dict:
    f = lambda x: None if x is None else x * scale  # noqa: E731
    return {
        "value": f(e.value),
        "lo": f(e.lo),
        "hi": f(e.hi),
        "n_rows": e.n_rows,
        "n_clusters": e.n_clusters,
    }


def cluster_of(e: Episode) -> str:
    return e.event or e.tickers[0]


def select(res: ScanResult, p: Params, *, confirmed: bool) -> list[Episode]:
    return [
        e
        for e in res.episodes
        if EvidenceLevel[e.level] >= p.level and (e.persisted or not confirmed)
    ]


def boot(rows, stat, p: Params, cl=cluster_of) -> Estimate:
    return cluster_bootstrap(rows, cl, stat, n_boot=p.n_boot, alpha=p.alpha, seed=p.seed)


def boot_mean(rows, value: Callable, p: Params, cl=cluster_of) -> Estimate:
    """Cluster bootstrap of a mean - the common case, on the O(clusters) fast path."""
    return cluster_bootstrap_mean(rows, cl, value, n_boot=p.n_boot, alpha=p.alpha, seed=p.seed)


def quantile(xs: Sequence[float], q: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    k = (len(ys) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return ys[lo] + (ys[hi] - ys[lo]) * (k - lo)


# --------------------------------------------------------------------------- exposure
def exposure_summary(res: ScanResult, p: Params) -> dict:
    ex = [x for x in res.exposures if EvidenceLevel[x.level] >= p.level]
    by_kind: dict[str, float] = defaultdict(float)
    by_cat: dict[str, float] = defaultdict(float)
    for x in ex:
        by_kind[x.kind] += x.seconds / 3600
        by_cat[x.category] += x.seconds / 3600
    return {
        "recording_minutes": res.span_s / 60,
        "poll_cycles": res.cycles,
        "recording_gaps": [(b - a) / SEC for a, b in res.gaps],
        "relations": len(ex),
        "relations_observed": sum(1 for x in ex if x.seconds > 0),
        "events": len({x.event for x in ex}),
        "relation_hours": sum(x.seconds for x in ex) / 3600,
        "relation_hours_by_kind": dict(by_kind),
        "relation_hours_by_category": dict(by_cat),
    }


# --------------------------------------------------------------------------- Experiment A
def frequency(res: ScanResult, p: Params, *, confirmed: bool, category: str | None = None) -> dict:
    """Episodes started per 1,000 relation-hours, and the share of relation-cycles in violation.

    Every event with any observable relation is a row - events that never showed a violation
    count in the denominator (dropping them would inflate the rate).  Left-censored episodes
    (already present when observation began) are excluded from the numerator: their start is not
    in the window."""
    eps = [
        e
        for e in select(res, p, confirmed=confirmed)
        if not e.left_censored and (category is None or e.category == category)
    ]
    hours: dict[str, float] = defaultdict(float)
    cycles: dict[str, int] = defaultdict(int)
    for x in res.exposures:
        if EvidenceLevel[x.level] < p.level or (category and x.category != category):
            continue
        hours[x.event] += x.seconds / 3600
        cycles[x.event] += x.cycles
    n_eps: Counter = Counter(cluster_of(e) for e in eps)
    alive_cycles: Counter = Counter()
    for e in eps:
        alive_cycles[cluster_of(e)] += e.n_confirms
    rows = [(ev, n_eps.get(ev, 0), hours[ev], alive_cycles.get(ev, 0), cycles[ev]) for ev in hours]
    rows += [(ev, n, 0.0, alive_cycles[ev], 0) for ev, n in n_eps.items() if ev not in hours]
    rate = cluster_bootstrap_ratio(
        rows,
        lambda r: r[0],
        lambda r: r[1],
        lambda r: r[2],
        n_boot=p.n_boot,
        alpha=p.alpha,
        seed=p.seed,
    )
    share = cluster_bootstrap_ratio(
        rows,
        lambda r: r[0],
        lambda r: r[3],
        lambda r: r[4],
        n_boot=p.n_boot,
        alpha=p.alpha,
        seed=p.seed,
    )
    conc = concentration(dict(n_eps))
    total_hours = sum(hours.values())
    return {
        "episodes": len(eps),
        "events_with_episodes": len(n_eps),
        "events_observed": len(hours),
        "per_1000_relation_hours": est(rate, 1000),
        "violated_cycle_share": est(share),
        "concentration": conc,
        "zero_bounds": zero_bounds(len(eps), len(hours), total_hours, p.alpha),
        "low_n": rate.n_clusters < p.low_n_clusters
        or conc["effective_clusters"] in (None,)
        or (conc["effective_clusters"] or 0) < 2,
    }


def zero_bounds(n_episodes: int, n_events: int, hours: float, alpha: float) -> dict | None:
    """What 'we saw none' rules out.  A bootstrap over rows that are all zero returns [0, 0], which
    is false precision; the honest statement is an upper bound.  Two, because they answer different
    questions: (1) per event - if a share q of events ever showed one, P(none in n events) =
    (1-q)^n, so q <= 1 - alpha^(1/n); (2) per relation-hour, assuming independent hours (a Poisson
    'rule of three': rate <= -ln(alpha)/hours), which clustering makes optimistic."""
    if n_episodes or n_events <= 0 or hours <= 0:
        return None
    return {
        "events_share_upper": 1 - alpha ** (1 / n_events),
        "per_1000_relation_hours_upper_naive": -math.log(alpha) / hours * 1000,
    }


def frequency_by_kind(res: ScanResult, p: Params, *, confirmed: bool) -> dict:
    out = {}
    for kind in sorted({e.kind for e in res.episodes}):
        eps = [e for e in select(res, p, confirmed=confirmed) if e.kind == kind]
        hours = sum(
            x.seconds for x in res.exposures if x.kind == kind and EvidenceLevel[x.level] >= p.level
        )
        out[kind] = {
            "episodes": len(eps),
            "events": len({cluster_of(e) for e in eps}),
            "relation_hours": hours / 3600,
        }
    return out


# --------------------------------------------------------------------------- Experiment B
def funnel(eps: Sequence[Episode], variant: str, p: Params) -> dict:
    """Cumulative survival through displayed -> liquid -> fees -> depth -> executable."""
    n = len(eps)
    reached = [sum(1 for e in eps if e.best_stage[variant] >= k) for k in range(5)]
    rows = []
    for k in range(5):
        cum = boot_mean(list(eps), lambda e, k=k: float(e.best_stage[variant] >= k), p)
        cond_n = reached[k - 1] if k else n
        wl, wh = wilson_ci(reached[k], cond_n) if cond_n else (0.0, 1.0)
        rows.append(
            {
                "stage": STAGES[k],
                "episodes": reached[k],
                "of_displayed": est(cum) if n else None,
                "given_previous": {
                    "value": reached[k] / cond_n if cond_n else None,
                    "wilson_lo": wl,
                    "wilson_hi": wh,
                    "n": cond_n,
                },
            }
        )
    return {"variant": variant, "displayed": n, "stages": rows}


def net_edge_distribution(eps: Sequence[Episode], variant: str, p: Params) -> dict:
    """Gross and net edge at the profit-maximising size, for episodes tradable under ``variant``."""
    tradable = [e for e in eps if variant in e.orders]
    gross_c = [e.first.top_margin / TICKS_PER_CENT for e in eps]
    net = [e.peak[variant].net_micro / MICRO for e in tradable]
    size = [e.peak[variant].bundles / 100 for e in tradable]
    cap = [e.first.capacity / 100 for e in eps]
    top = [e.first.top_qty / 100 for e in eps]

    def q(xs):
        return {
            "p10": quantile(xs, 0.1),
            "p50": quantile(xs, 0.5),
            "p90": quantile(xs, 0.9),
            "max": max(xs) if xs else None,
        }

    med_ci = (
        est(
            boot(
                list(tradable),
                lambda rs: quantile([r.peak[variant].net_micro / MICRO for r in rs], 0.5),
                p,
            )
        )
        if tradable
        else None
    )
    return {
        "variant": variant,
        "n_displayed": len(eps),
        "n_tradable": len(tradable),
        "gross_edge_cents_per_contract": q(gross_c),
        "liquidity_at_best_contracts": q(top),
        "book_capacity_contracts": q(cap),
        "net_profit_usd_at_best_size": q(net),
        "net_profit_median_ci": med_ci,
        "best_size_contracts": q(size),
        "total_net_usd": sum(net) if net else 0.0,
        "positive_net": sum(1 for x in net if x > 0),
    }


def conditional_rate(
    eps: Sequence[Episode],
    bins: Sequence[tuple[str, Callable[[Episode], bool]]],
    outcome: Callable[[Episode], bool],
    p: Params,
) -> list[dict]:
    """P(outcome | bin) with a Wilson interval (treating episodes as independent - optimistic) and a
    cluster-bootstrap interval (respecting events).  The wider one is the honest one."""
    out = []
    for label, pred in bins:
        sub = [e for e in eps if pred(e)]
        k = sum(1 for e in sub if outcome(e))
        wl, wh = wilson_ci(k, len(sub)) if sub else (0.0, 1.0)
        cb = boot_mean(sub, lambda e: float(outcome(e)), p) if sub else None
        out.append(
            {
                "bin": label,
                "n": len(sub),
                "successes": k,
                "rate": k / len(sub) if sub else None,
                "wilson": [wl, wh],
                "cluster": None if cb is None else est(cb),
                "n_events": len({cluster_of(e) for e in sub}),
            }
        )
    return out


def edge_bins() -> list[tuple[str, Callable[[Episode], bool]]]:
    return [
        ("<=1c", lambda e: e.first.top_margin <= 100),
        ("1-2c", lambda e: 100 < e.first.top_margin <= 200),
        (">2c", lambda e: e.first.top_margin > 200),
    ]


def liquidity_bins() -> list[tuple[str, Callable[[Episode], bool]]]:
    return [
        ("<10 contracts", lambda e: e.first.top_qty < 1000),
        ("10-100", lambda e: 1000 <= e.first.top_qty < 10_000),
        (">=100", lambda e: e.first.top_qty >= 10_000),
    ]


def expiry_bins() -> list[tuple[str, Callable[[Episode], bool]]]:
    h = lambda e: e.hours_to_expiry  # noqa: E731
    return [
        ("<1h", lambda e: h(e) is not None and h(e) < 1),
        ("1-3h", lambda e: h(e) is not None and 1 <= h(e) < 3),
        ("3-24h", lambda e: h(e) is not None and 3 <= h(e) < 24),
        (">=24h", lambda e: h(e) is not None and h(e) >= 24),
        ("unknown", lambda e: h(e) is None),
    ]


def executability_by_covariate(eps: Sequence[Episode], variant: str, p: Params) -> dict:
    """P(executable | detected) against edge, liquidity, category, time to expiry and book depth."""
    ok = lambda e: e.best_stage[variant] >= 4  # noqa: E731
    cats = sorted({e.category for e in eps})
    deep = quantile([e.first.capacity for e in eps], 0.5) if eps else None
    return {
        "variant": variant,
        "overall": conditional_rate(eps, [("all", lambda e: True)], ok, p)[0] if eps else None,
        "edge": conditional_rate(eps, edge_bins(), ok, p),
        "liquidity": conditional_rate(eps, liquidity_bins(), ok, p),
        "category": conditional_rate(
            eps, [(c, lambda e, c=c: e.category == c) for c in cats], ok, p
        ),
        "time_to_expiry": conditional_rate(eps, expiry_bins(), ok, p),
        "book_depth": conditional_rate(
            eps,
            [
                ("below median", lambda e: deep is not None and e.first.capacity < deep),
                ("at/above median", lambda e: deep is not None and e.first.capacity >= deep),
            ],
            ok,
            p,
        ),
        "median_depth_contracts": None if deep is None else deep / 100,
    }


def realised_check(eps: Sequence[Episode], settled: dict[str, int], variant: str) -> dict:
    """Did the bundle bought at detection actually pay at least what the constraint promised?

    For a *proven* relation this cannot fail (Stage 4 tests it in every admissible world).  For a
    declared or unverified one it can - which makes settlement the empirical test of the relation
    itself.  A shortfall means the assumed structure was wrong (e.g. an outcome nobody listed)."""
    out: dict[str, dict] = {}
    for level in ("PROVEN", "LATTICE", "DECLARED", "EMPIRICAL", "UNVERIFIED"):
        rows = []
        for e in eps:
            vw = e.first.views.get(variant)
            if e.level != level or vw is None or vw.priced is None:
                continue
            if any(t not in settled for t in e.tickers):
                continue
            pnl = _payoff(vw.priced, settled) - vw.priced.cost_micro - vw.priced.fee_micro
            rows.append((e, pnl, vw.net_micro))
        if not rows:
            continue
        short = [r for r in rows if r[1] < r[2]]
        out[level] = {
            "settled_tradable": len(rows),
            "realised_below_promised": len(short),
            "share_below": len(short) / len(rows),
            "wilson": list(wilson_ci(len(short), len(rows))),
            "realised_usd": sum(r[1] for r in rows) / MICRO,
            "promised_usd": sum(r[2] for r in rows) / MICRO,
            "worst_usd": min(r[1] for r in rows) / MICRO,
            "events": len({cluster_of(r[0]) for r in rows}),
        }
    return out


def _payoff(pb, settled: dict[str, int]) -> int:
    total = 0
    for leg in pb.legs:
        yes = settled[leg.contract.ticker]
        total += leg.qty * (yes if leg.contract.side.value == "yes" else 10_000 - yes)
    return total


# --------------------------------------------------------------------------- Experiment C
def lifetimes(eps: Sequence[Episode]) -> list[Lifetime]:
    return [Lifetime(e.lo_s, e.hi_s, cluster_of(e)) for e in eps]


def lifetime_analysis(eps: Sequence[Episode], p: Params) -> dict:
    """Interval-censored lifetime since first sighting: assumption-free bounds on S(t),
    Kaplan-Meier on both readings of each interval, and a labelled exponential extrapolation."""
    if not eps:
        return {"n": 0}
    lives = lifetimes(eps)
    grid = []
    for t in LIFETIME_GRID_S:
        lo_c = boot_mean(list(eps), lambda e, t=t: float(e.lo_s > t), p)
        up_c = boot_mean(list(eps), lambda e, t=t: float(e.hi_s > t), p)
        lo, up = survival_bounds(lives, t)
        grid.append({"t_s": t, "lower": est(lo_c), "upper": est(up_c), "point": [lo, up]})
    # KM: "died as early as possible" (a lower curve) and "as late as possible" (an upper curve)
    km_lo = kaplan_meier([lf.lo for lf in lives], [not lf.censored for lf in lives])
    km_hi = kaplan_meier(
        [lf.lo if lf.censored else lf.hi for lf in lives], [not lf.censored for lf in lives]
    )
    lam = exp_interval_mle(lives)
    lam_ci = boot(list(eps), lambda rs: exp_interval_mle(lifetimes(rs)), p) if lam else None
    return {
        "n": len(eps),
        "censored": sum(lf.censored for lf in lives),
        "survival_grid": grid,
        "km_median_s": {"pessimistic": km_quantile(km_lo), "optimistic": km_quantile(km_hi)},
        "km_at": {
            str(t): {"pessimistic": km_at(km_lo, t), "optimistic": km_at(km_hi, t)}
            for t in (1, 3, 10, 30, 60)
        },
        "exponential": None
        if lam is None
        else {
            "rate_per_s": lam,
            "mean_life_s": 1 / lam,
            "rate_ci": None if lam_ci is None else est(lam_ci),
            "note": "model-based extrapolation; not identified below the polling interval",
        },
    }


def end_causes(eps: Sequence[Episode], p: Params) -> dict:
    """What ended the episodes that ended (not censored): the edge was taken vs. the quote moved."""
    ended = [e for e in eps if not e.censored]
    cnt = Counter(e.end_reason for e in ended)
    k = cnt.get("consumed", 0)
    wl, wh = wilson_ci(k, len(ended)) if ended else (0.0, 1.0)
    cb = boot_mean(ended, lambda e: float(e.end_reason == "consumed"), p) if ended else None
    return {
        "ended": len(ended),
        "censored": len(eps) - len(ended),
        "counts": dict(cnt),
        "censored_reasons": dict(Counter(e.end_reason for e in eps if e.censored)),
        "consumed_share": {
            "value": k / len(ended) if ended else None,
            "wilson": [wl, wh],
            "cluster": None if cb is None else est(cb),
        },
        "note": "'consumed' = a trade lifted a leg's ask in the polling gap; attribution is "
        "only as sharp as the polling interval",
    }


def sighting_lifetime_diagnostic(eps: Sequence[Episode]) -> dict:
    """The chunk-skew fingerprint: how many sightings lived less than a poll cycle, and where their
    upper lifetime bound concentrates (a spike at the inter-chunk delay = snapshot artefacts)."""
    short = [e for e in eps if not e.persisted and not e.censored]
    his = [e.hi_s for e in short]
    return {
        "sightings": len(eps),
        "not_persisted": len([e for e in eps if not e.persisted]),
        "persisted": len([e for e in eps if e.persisted]),
        "persisted_share": len([e for e in eps if e.persisted]) / len(eps) if eps else None,
        "short_upper_bound_s": {
            "n": len(his),
            "p25": quantile(his, 0.25),
            "p50": quantile(his, 0.5),
            "p75": quantile(his, 0.75),
            "share_under_1s": sum(1 for h in his if h < 1.0) / len(his) if his else None,
        },
    }


def edge_lifetime_association(eps: Sequence[Episode], p: Params) -> dict:
    """Do larger edges die faster (or slower)?  Rank correlation of initial edge / liquidity
    with the lifetime lower bound, with a cluster bootstrap interval."""

    def rho(f):
        def stat(rs):
            return spearman([f(e) for e in rs], [e.lo_s for e in rs])

        return est(boot(list(eps), stat, p)) if len(eps) >= 5 else None

    return {
        "n": len(eps),
        "edge_vs_lifetime": rho(lambda e: e.first.top_margin),
        "liquidity_vs_lifetime": rho(lambda e: e.first.top_qty),
        "depth_vs_lifetime": rho(lambda e: e.first.capacity),
    }


# --------------------------------------------------------------------------- categories
def category_table(
    res: ScanResult, p: Params, variant_pairs: Iterable[str] = ("fee_free", "baseline")
) -> list[dict]:
    cats = sorted({x.category for x in res.exposures} | {e.category for e in res.episodes})
    rows = []
    for c in cats:
        sight = [e for e in select(res, p, confirmed=False) if e.category == c]
        conf = [e for e in sight if e.persisted]
        f_all = frequency(res, p, confirmed=False, category=c)
        f_conf = frequency(res, p, confirmed=True, category=c)
        row = {
            "category": c,
            "relation_hours": sum(
                x.seconds
                for x in res.exposures
                if x.category == c and EvidenceLevel[x.level] >= p.level
            )
            / 3600,
            "events_observed": f_all["events_observed"],
            "sightings": f_all,
            "confirmed": f_conf,
            "confirmed_lifetime_median_s": (
                lifetime_analysis(conf, p)["km_median_s"] if conf else None
            ),
        }
        for v in variant_pairs:
            row[f"executable_{v}"] = {
                "sightings": sum(1 for e in sight if e.best_stage[v] >= 4),
                "confirmed": sum(1 for e in conf if e.best_stage[v] >= 4),
            }
        row["low_n"] = f_all["events_observed"] < p.low_n_clusters
        rows.append(row)
    return rows
