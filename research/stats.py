"""Statistics for the arbitrage research: uncertainty that respects how the data were generated.

Two facts shape every choice here.

  1. **Observations are clustered.**  325 of the 327 violations in the Stage 4 replay came from ONE
     event; the episodes of one event are the same market state seen through several constraints and
     are not independent.  A naive bootstrap over episodes would report a tight interval around a
     sample of effective size ~1.  So every interval here resamples *clusters* (events), never rows.
  2. **Lifetimes are interval-censored.**  Books arrive every few seconds, so an opportunity seen
     alive at ``a`` and dead at ``b`` lived somewhere in [a, b).  A survival curve is therefore
     a *pair of bounds*, not a single line, and the gap between them is the data's resolving
     power.

Nothing here needs scipy; numpy is used only where a vector form is much clearer.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass

INF = math.inf


# --------------------------------------------------------------------------- proportions
def wilson_ci(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (valid at k=0 and k=n, unlike Wald)."""
    if n <= 0:
        return (0.0, 1.0)
    if not 0 <= k <= n:
        raise ValueError(f"k={k} outside [0, {n}]")
    z = _z(alpha)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def _z(alpha: float) -> float:
    """Two-sided standard-normal quantile (Acklam's rational approximation, |error| < 1.2e-9)."""
    p = 1 - alpha / 2
    a = [
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    ]
    b = [
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    ]
    c = [
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    ]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00, 3.754408661907416e00]
    lo = 0.02425
    if p < lo:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p <= 1 - lo:
        q = p - 0.5
        r = q * q
        return (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
        )
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
    )


# --------------------------------------------------------------------------- clustered bootstrap
@dataclass(frozen=True)
class Estimate:
    """A point estimate with a percentile interval; ``n_clusters`` is the effective sample size."""

    value: float | None
    lo: float | None
    hi: float | None
    n_rows: int
    n_clusters: int

    def fmt(self, digits: int = 3, scale: float = 1.0) -> str:
        if self.value is None:
            return "n/a"
        f = f"{{:.{digits}f}}"
        s = f.format(self.value * scale)
        if self.lo is None or self.hi is None:
            return s
        return f"{s} [{f.format(self.lo * scale)}, {f.format(self.hi * scale)}]"


def cluster_bootstrap(
    rows: Sequence,
    cluster_of: Callable[[object], Hashable],
    stat: Callable[[Sequence], float | None],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Estimate:
    """Percentile bootstrap that resamples whole clusters.

    ``stat`` receives the concatenated rows of the drawn clusters.  A statistic that returns
    ``None`` on a resample (e.g. a ratio with an empty denominator) is skipped; if too many
    are skipped the interval is withheld rather than reported on a biased subset.
    """
    groups: dict[Hashable, list] = {}
    for r in rows:
        groups.setdefault(cluster_of(r), []).append(r)
    keys = list(groups)
    point = stat(list(rows)) if rows else None
    if len(keys) < 2 or point is None:
        return Estimate(point, None, None, len(rows), len(keys))
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        sample: list = []
        for k in (keys[rng.randrange(len(keys))] for _ in keys):
            sample.extend(groups[k])
        v = stat(sample)
        if v is not None and not (isinstance(v, float) and math.isnan(v)):
            draws.append(v)
    if len(draws) < 0.9 * n_boot:
        return Estimate(point, None, None, len(rows), len(keys))
    draws.sort()
    lo = draws[int(alpha / 2 * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))]
    return Estimate(point, lo, hi, len(rows), len(keys))


def cluster_bootstrap_ratio(
    rows: Sequence,
    cluster_of: Callable[[object], Hashable],
    num: Callable[[object], float],
    den: Callable[[object], float],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Estimate:
    """Ratio estimator sum(num)/sum(den) with a cluster bootstrap (rates: episodes per hour...).

    Works from per-cluster sums, so a draw costs O(clusters), not O(rows).  It consumes the random
    generator exactly as ``cluster_bootstrap`` does: identical intervals for one seed."""
    sums: dict[Hashable, list[float]] = {}
    for r in rows:
        acc = sums.setdefault(cluster_of(r), [0.0, 0.0])
        acc[0] += num(r)
        acc[1] += den(r)
    keys = list(sums)
    tn = sum(v[0] for v in sums.values())
    td = sum(v[1] for v in sums.values())
    point = tn / td if rows and td > 0 else None
    if len(keys) < 2 or point is None:
        return Estimate(point, None, None, len(rows), len(keys))
    rng = random.Random(seed)
    draws: list[float] = []
    for _ in range(n_boot):
        n_sum = d_sum = 0.0
        for k in (keys[rng.randrange(len(keys))] for _ in keys):
            n_sum += sums[k][0]
            d_sum += sums[k][1]
        if d_sum > 0:
            draws.append(n_sum / d_sum)
    if len(draws) < 0.9 * n_boot:
        return Estimate(point, None, None, len(rows), len(keys))
    draws.sort()
    lo = draws[int(alpha / 2 * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))]
    return Estimate(point, lo, hi, len(rows), len(keys))


def cluster_bootstrap_mean(
    rows: Sequence,
    cluster_of: Callable[[object], Hashable],
    value: Callable[[object], float],
    **kw,
) -> Estimate:
    """Mean of ``value`` over rows, resampling clusters (a ratio with denominator = row count)."""
    return cluster_bootstrap_ratio(rows, cluster_of, value, lambda r: 1.0, **kw)


def cluster_bootstrap_diff(
    a: Sequence,
    b: Sequence,
    cluster_of: Callable[[object], Hashable],
    stat: Callable[[Sequence], float | None],
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 0,
) -> Estimate:
    """Difference stat(a) - stat(b) with the two groups resampled independently by cluster."""
    ga: dict[Hashable, list] = {}
    gb: dict[Hashable, list] = {}
    for r in a:
        ga.setdefault(cluster_of(r), []).append(r)
    for r in b:
        gb.setdefault(cluster_of(r), []).append(r)
    ka, kb = list(ga), list(gb)
    pa, pb = (stat(list(a)) if a else None), (stat(list(b)) if b else None)
    if pa is None or pb is None:
        return Estimate(None, None, None, len(a) + len(b), len(ka) + len(kb))
    point = pa - pb
    if len(ka) < 2 or len(kb) < 2:
        return Estimate(point, None, None, len(a) + len(b), len(ka) + len(kb))
    rng = random.Random(seed)
    draws = []
    for _ in range(n_boot):
        sa: list = []
        for k in (ka[rng.randrange(len(ka))] for _ in ka):
            sa.extend(ga[k])
        sb: list = []
        for k in (kb[rng.randrange(len(kb))] for _ in kb):
            sb.extend(gb[k])
        va, vb = stat(sa), stat(sb)
        if va is not None and vb is not None:
            draws.append(va - vb)
    if len(draws) < 0.9 * n_boot:
        return Estimate(point, None, None, len(a) + len(b), len(ka) + len(kb))
    draws.sort()
    return Estimate(
        point,
        draws[int(alpha / 2 * len(draws))],
        draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))],
        len(a) + len(b),
        len(ka) + len(kb),
    )


def concentration(counts: Mapping[Hashable, int]) -> dict:
    """How unevenly a total is spread across clusters (top-1 share, HHI, effective #clusters).

    ``effective_clusters = 1 / HHI`` is the number of equally-sized clusters that would give the
    same concentration: 327 violations of which 325 sit in one event is ~1.01, not 327 and not 2."""
    total = sum(counts.values())
    if total <= 0:
        return {
            "total": 0,
            "clusters": 0,
            "top1_share": None,
            "hhi": None,
            "effective_clusters": None,
        }
    shares = sorted((v / total for v in counts.values() if v > 0), reverse=True)
    hhi = sum(s * s for s in shares)
    return {
        "total": total,
        "clusters": len(shares),
        "top1_share": shares[0],
        "hhi": hhi,
        "effective_clusters": 1 / hhi,
    }


# ------------------------------------------------------------------- survival (interval-censored)
@dataclass(frozen=True)
class Lifetime:
    """One opportunity's lifetime since we first saw it, known only to lie in [lo, hi).

    ``lo``: seen alive that long (last confirmed alive minus first seen).
    ``hi``: seen dead by then; ``inf`` when it was still alive when observation stopped
    (right-censored: the data ended, a market closed on it, or the recorder went dark)."""

    lo: float
    hi: float
    cluster: Hashable = None

    def __post_init__(self) -> None:
        if self.lo < 0 or self.hi < self.lo:
            raise ValueError(f"impossible lifetime bounds [{self.lo}, {self.hi})")

    @property
    def censored(self) -> bool:
        return math.isinf(self.hi)


def survival_bounds(lives: Sequence[Lifetime], t: float) -> tuple[float, float]:
    """(lower, upper) bound on P(lifetime > t): lower counts only what was *seen* alive past t,
    upper counts everything not *seen dead* by t.  lower <= truth <= upper, whatever happened
    between
    observations (this is the identification region; no distributional assumption)."""
    if not lives:
        return (0.0, 1.0)
    n = len(lives)
    lower = sum(1 for lf in lives if lf.lo > t) / n
    upper = sum(1 for lf in lives if lf.hi > t) / n
    return (lower, upper)


def kaplan_meier(durations: Sequence[float], observed: Sequence[bool]) -> list[tuple[float, float]]:
    """Product-limit estimate S(t) as ``[(t, S(t)), ...]`` at each event time (right-censoring).

    Ties: events at a time are processed before censorings at the same time (standard)."""
    if len(durations) != len(observed):
        raise ValueError("durations and observed must have the same length")
    data = sorted(zip(durations, observed, strict=True), key=lambda x: (x[0], not x[1]))
    n_at_risk = len(data)
    s = 1.0
    out: list[tuple[float, float]] = []
    i = 0
    while i < len(data):
        t = data[i][0]
        deaths = 0
        j = i
        while j < len(data) and data[j][0] == t:
            deaths += data[j][1]
            j += 1
        if deaths:
            s *= 1 - deaths / n_at_risk
            out.append((t, s))
        n_at_risk -= j - i
        i = j
    return out


def km_at(curve: Sequence[tuple[float, float]], t: float) -> float:
    """S(t) from a product-limit curve (right-continuous step, S=1 before the first event)."""
    s = 1.0
    for ti, si in curve:
        if ti <= t:
            s = si
        else:
            break
    return s


def km_quantile(curve: Sequence[tuple[float, float]], q: float = 0.5) -> float | None:
    """Smallest t with S(t) <= 1-q, or ``None`` if the curve never gets there (censoring)."""
    for t, s in curve:
        if s <= 1 - q:
            return t
    return None


def exp_interval_mle(lives: Sequence[Lifetime], *, tol: float = 1e-10) -> float | None:
    """Exponential hazard rate (1/s) maximising the interval-censored likelihood.

        L(lambda) = prod_i [exp(-lambda*lo_i) - exp(-lambda*hi_i)]      (hi=inf: exp(-lambda*lo_i))

    ILLUSTRATIVE ONLY: an exponential lifetime is an assumption the data cannot check below the
    polling interval.  It is used to *extrapolate* to sub-cadence latencies and is always reported
    next to the assumption-free bounds.  Returns ``None`` with no interior maximum
    (everything right-censored: hazard ~ 0; or everything dead at lo=0: hazard unbounded)."""
    if not lives:
        return None
    if all(lf.censored for lf in lives):
        return None

    def loglik(lam: float) -> float:
        total = 0.0
        for lf in lives:
            a = math.exp(-lam * lf.lo)
            b = 0.0 if lf.censored else math.exp(-lam * lf.hi)
            total += math.log(max(a - b, 1e-300))
        return total

    lo_l, hi_l = math.log(1e-9), math.log(1e3)  # rates from 1e-9/s to 1e3/s
    phi = (math.sqrt(5) - 1) / 2
    a, b = lo_l, hi_l
    c, d = b - phi * (b - a), a + phi * (b - a)
    fc, fd = loglik(math.exp(c)), loglik(math.exp(d))
    while b - a > tol:
        if fc > fd:
            b, d, fd = d, c, fc
            c = b - phi * (b - a)
            fc = loglik(math.exp(c))
        else:
            a, c, fc = c, d, fd
            d = a + phi * (b - a)
            fd = loglik(math.exp(d))
    lam = math.exp((a + b) / 2)
    if lam <= 1.0001e-9 or lam >= 0.9999e3:  # ran into the search boundary: no interior optimum
        return None
    return lam


def exp_survival(lam: float, t: float) -> float:
    return math.exp(-lam * t)


# --------------------------------------------------------------------------- association
def _ranks(xs: Sequence[float]) -> list[float]:
    """Average ranks (ties share the mean of the ranks they span)."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    """Rank correlation; ``None`` if either variable is constant (correlation undefined)."""
    if len(xs) != len(ys):
        raise ValueError("length mismatch")
    if len(xs) < 3:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    sxx = sum((a - mx) ** 2 for a in rx)
    syy = sum((b - my) ** 2 for b in ry)
    if sxx == 0 or syy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True)) / math.sqrt(sxx * syy)
