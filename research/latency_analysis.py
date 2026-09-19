"""Experiment D: how does latency change what is executable?

Two complementary measurements, because polling data cannot resolve everything.

  1. **As-of probes** (``survival_table``, ``execution_table``).  For every opportunity we ask: had
     our order arrived D after we first saw the edge, would it still be there, fill, and pay?
     Answered against two books: the last one observed by then ("before", optimistic: what the
     Stage 5 engine assumes) and the first one observed after it ("after", pessimistic: every
     change landed just before the order).  Each metric is a *bracket* per opportunity and we
     report [min, max].  The bracket is wide for D below the polling interval - that width IS the
     finding: 1-250 ms cannot be resolved from snapshots seconds apart, and pretending otherwise
     would manufacture a precision the data does not have.

  2. **Engine sweep** (``engine_latency_sweep``).  The Stage 4 detector driven through the Stage 5
     engine at each latency, on a dataset that also holds settlements.  It adds what probes cannot:
     *leg risk* (orders for the legs of one bundle can fill unevenly, turning a riskless arbitrage
     into a directional bet) and realised P&L.  It uses the last snapshot at arrival, so it sits
     on the optimistic side of the bracket.

Below the polling interval the engine and the "before" probe are the same optimistic bound; only
for D at or above the interval do the two readings converge on something the data observed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from arbitrage.detector import DetectorParams
from backtest.engine import BacktestConfig, run_backtest
from backtest.fills import QueueModel, TakerModel
from backtest.strategies import ArbitrageTaker
from market.semantics import EvidenceLevel
from research.arbitrage_analysis import (
    MICRO,
    Params,
    boot_mean,
    cluster_of,
    est,
)
from research.scanner import SEC, Episode, ProbeResult
from research.stats import cluster_bootstrap_ratio, exp_survival

#: the spec's grid (1..250 ms) extended to the scales polling data can actually resolve
DELTAS_S = (0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60)


@dataclass(frozen=True)
class Pair:
    ep: Episode
    before: ProbeResult
    after: ProbeResult | None  # absent when the data ends first


def pairs_at(eps: Sequence[Episode], delta_s: float, gap_s: float) -> tuple[list[Pair], int]:
    """(observable probe pairs at this delay, number excluded as unobservable).

    A probe is unobservable if its time lies beyond the data, or in a stretch where the recorder was
    dark (the 'before' book was last confirmed more than ``gap_s`` earlier): there is no book to
    evaluate it against, and inventing one would bias the table."""
    d = int(delta_s * SEC)
    out: list[Pair] = []
    skipped = 0
    for e in eps:
        b = next((p for p in e.probes if p.delta_ns == d and p.kind == "before"), None)
        a = next((p for p in e.probes if p.delta_ns == d and p.kind == "after"), None)
        if (
            b is None or a is None or (b.since_confirm_ns or 0) > gap_s * SEC
        ):  # no 'after' = past the data
            skipped += 1
            continue
        out.append(Pair(e, b, a))
    return out, skipped


def _bracket(pairs: Sequence[Pair], f: Callable[[ProbeResult, Pair], float], p: Params) -> dict:
    """Mean of a per-opportunity value under the optimistic / pessimistic reading of each pair.

    ``lo`` takes the smaller of (before, after) per opportunity, ``hi`` the larger, so the truth -
    one of the two books - lies inside for every single opportunity, not just on average."""
    if not pairs:
        return {"lo": None, "hi": None}
    lo = boot_mean(list(pairs), lambda q: min(f(q.before, q), f(q.after, q)), p, _pcl)
    hi = boot_mean(list(pairs), lambda q: max(f(q.before, q), f(q.after, q)), p, _pcl)
    return {"lo": est(lo), "hi": est(hi)}


def _pcl(q: Pair) -> str:
    return cluster_of(q.ep)


def survival_table(eps: Sequence[Episode], p: Params, gap_s: float = 30.0) -> list[dict]:
    """P(the displayed edge is still there D after we first saw it), as a bracket."""
    rows = []
    for d in DELTAS_S:
        pairs, skipped = pairs_at(eps, d, gap_s)
        differ = sum(1 for q in pairs if q.before.alive != q.after.alive)
        rows.append(
            {
                "delta_s": d,
                "n": len(pairs),
                "unobservable": skipped,
                "alive": _bracket(pairs, lambda pr, q: float(pr.alive), p),
                "unresolved_share": differ / len(pairs) if pairs else None,
                "margin_retained": _margin_retained(pairs, p),
            }
        )
    return rows


def _margin_retained(pairs: Sequence[Pair], p: Params) -> dict:
    """Edge still on the table as a fraction of the edge first seen (0 once it is gone)."""
    if not pairs:
        return {"lo": None, "hi": None}

    def ratio(pick):
        return cluster_bootstrap_ratio(
            list(pairs),
            _pcl,
            lambda q: pick(q.before.margin, q.after.margin),
            lambda q: q.ep.first.top_margin,
            n_boot=p.n_boot,
            alpha=p.alpha,
            seed=p.seed,
        )

    return {"lo": est(ratio(min)), "hi": est(ratio(max))}


def _metrics(variant: str) -> tuple[Callable, Callable, Callable]:
    """Per-probe metrics for one cost variant: whole-bundle fill, min leg fill, net USD received."""

    def full(pr: ProbeResult, q: Pair) -> float:
        return float(pr.variants[variant][0])

    def frac(pr: ProbeResult, q: Pair) -> float:
        return pr.variants[variant][1]

    def net_usd(pr: ProbeResult, q: Pair) -> float:
        _, _, net = pr.variants[variant]  # net is None unless the whole bundle filled
        return (net or 0) / MICRO

    return full, frac, net_usd


def _net_retained(pairs: Sequence[Pair], variant: str, net_usd: Callable, pick, p: Params):
    return cluster_bootstrap_ratio(
        list(pairs),
        _pcl,
        lambda q: pick(net_usd(q.before, q), net_usd(q.after, q)),
        lambda q: q.ep.first.views[variant].net_micro / MICRO,
        n_boot=p.n_boot,
        alpha=p.alpha,
        seed=p.seed,
    )


def execution_table(
    eps: Sequence[Episode], variant: str, p: Params, gap_s: float = 30.0
) -> list[dict]:
    """For opportunities tradable under ``variant`` at first sight: what an order sent then would
    get D later - fill probability, net edge, and how many opportunities are missed."""
    tradable = [e for e in eps if variant in e.orders]
    full, frac, net_usd = _metrics(variant)
    rows = []
    for d in DELTAS_S:
        pairs, skipped = pairs_at(tradable, d, gap_s)
        pairs = [q for q in pairs if variant in q.before.variants and variant in q.after.variants]
        full_b = _bracket(pairs, full, p)
        rows.append(
            {
                "delta_s": d,
                "n": len(pairs),
                "unobservable": skipped,
                "full_fill": full_b,
                "expired_share": {
                    "lo": None if full_b["hi"] is None else _one_minus(full_b["hi"]),
                    "hi": None if full_b["lo"] is None else _one_minus(full_b["lo"]),
                },
                "min_leg_fill_fraction": _bracket(pairs, frac, p),
                "net_usd_per_detected": _bracket(pairs, net_usd, p),
                "net_retained_fraction": (
                    {
                        "lo": est(_net_retained(pairs, variant, net_usd, min, p)),
                        "hi": est(_net_retained(pairs, variant, net_usd, max, p)),
                    }
                    if pairs
                    else None
                ),
                "missed_opportunities": {
                    "lo": sum(1 for q in pairs if not (full(q.before, q) and full(q.after, q))),
                    "hi": sum(1 for q in pairs if not (full(q.before, q) or full(q.after, q))),
                },
            }
        )
    return rows


def latency_funnel(
    eps: Sequence[Episode],
    variant: str,
    p: Params,
    deltas_s: Sequence[float] = (0.1, 1.0, 3.0, 10.0),
    gap_s: float = 30.0,
) -> dict:
    """The spec's ablation with its last stage: displayed -> liquid -> fees -> slippage ->
    executable at first sight -> still fillable whole D later (a bracket, as everywhere here)."""
    at_sight = [e for e in eps if e.first.views[variant].stage >= 4]
    full, _, _ = _metrics(variant)
    rows = []
    for d in deltas_s:
        pairs, skipped = pairs_at(at_sight, d, gap_s)
        pairs = [q for q in pairs if variant in q.before.variants and variant in q.after.variants]
        rows.append(
            {
                "delta_s": d,
                "executable_at_first_sight": len(at_sight),
                "observable": len(pairs),
                "unobservable": skipped,
                "survive_lo": sum(1 for q in pairs if full(q.before, q) and full(q.after, q)),
                "survive_hi": sum(1 for q in pairs if full(q.before, q) or full(q.after, q)),
            }
        )
    return {
        "variant": variant,
        "displayed": len(eps),
        "stages_at_first_sight": [
            sum(1 for e in eps if e.first.views[variant].stage >= k) for k in range(5)
        ],
        "latency": rows,
    }


def _one_minus(e: dict | None) -> dict | None:
    if e is None:
        return None
    return {
        "value": None if e["value"] is None else 1 - e["value"],
        "lo": None if e["hi"] is None else 1 - e["hi"],
        "hi": None if e["lo"] is None else 1 - e["lo"],
        "n_rows": e["n_rows"],
        "n_clusters": e["n_clusters"],
    }


def survival_by_covariate(
    eps: Sequence[Episode],
    delta_s: float,
    bins: Sequence[tuple[str, Callable[[Episode], bool]]],
    p: Params,
    gap_s: float = 30.0,
) -> list[dict]:
    """P(edge survives D) as a function of a covariate (initial edge, liquidity): the spec's
    'as a function of initial edge and liquidity'."""
    pairs, _ = pairs_at(eps, delta_s, gap_s)
    out = []
    for label, pred in bins:
        sub = [q for q in pairs if pred(q.ep)]
        out.append(
            {
                "bin": label,
                "n": len(sub),
                "events": len({cluster_of(q.ep) for q in sub}),
                "alive": _bracket(sub, lambda pr, q: float(pr.alive), p),
            }
        )
    return out


def extrapolated_survival(
    rate_per_s: float | None, deltas: Sequence[float] = DELTAS_S
) -> list[dict]:
    """exp(-rate*D): what a memoryless decay fitted to the observed lifetimes would predict.  Shown
    only next to the bounds it must respect, and labelled as a model."""
    if rate_per_s is None:
        return []
    return [{"delta_s": d, "survival_model": exp_survival(rate_per_s, d)} for d in deltas]


# --------------------------------------------------------------------------- engine sweep
def engine_latency_sweep(
    ds,
    latencies_ms: Sequence[float],
    *,
    min_level: EvidenceLevel = EvidenceLevel.DECLARED,
    fee_scale: float = 1.0,
    stale_s: float = 30.0,
    target: int = 1000,
    max_size: int | None = 10_000,
) -> list[dict]:
    """Stage 4 detector -> Stage 5 engine at each latency: fills, leg risk and realised P&L."""
    from fractions import Fraction

    tickers = [m.ticker for m in ds.feed.markets]
    infos = ds.feed.infos
    fees = ds.fees.scaled(Fraction(fee_scale).limit_denominator(1000))
    out = []
    for ms in latencies_ms:
        ns = int(ms * 1_000_000)
        cfg = BacktestConfig(
            initial_cash_micro=10_000 * MICRO,
            latency_submit_ns=ns,
            latency_ack_ns=ns,
            latency_cancel_ns=ns,
            taker=TakerModel(max_staleness_ns=int(stale_s * SEC)),
            maker=QueueModel.optimistic(),
            fees=fees,
            equity_sample_ns=30 * SEC,
        )
        strat = ArbitrageTaker(
            ds.specs,
            DetectorParams(fees=fees, target=target, max_size=max_size, min_level=min_level),
            max_book_age_ns=int(stale_s * SEC),
        )
        res = run_backtest(ds.feed, {t: infos[t] for t in tickers}, strat, cfg)
        out.append(
            {
                "latency_ms": ms,
                "opportunities": strat.opportunities_seen,
                "orders": len(res.orders),
                **leg_outcomes(res),
                "net_usd": res.pnl_micro / MICRO,
                "fees_usd": res.portfolio.fees / MICRO,
                "settled_markets": len(res.settlements),
            }
        )
    return out


def leg_outcomes(res) -> dict:
    """Group orders into bundles (same submit time and tag = one opportunity), count how many filled
    completely, partially or not at all, and score each *bundle* at settlement.

    Per-bundle P&L is the right measure of leg risk.  (Per-market P&L is not: a hedged bundle
    books a big loss on the market where its NO leg loses and an offsetting gain elsewhere.)  A
    bundle is scored only once every market it traded has settled."""
    groups: dict[tuple[int, str], list] = defaultdict(list)
    for o in res.orders:
        if o.tag.startswith("arb:"):
            groups[(o.submitted_ts, o.tag)].append(o)
    settled = {t: v for _, t, v, _ in getattr(res, "settlements", [])}
    fills_by_order: dict[int, list] = defaultdict(list)
    for f in getattr(res, "fills", []):
        fills_by_order[f.order_id].append(f)
    counts = {"full": 0, "partial": 0, "none": 0}
    pnl: dict[str, list[int]] = {"full": [], "partial": []}
    unsettled = 0
    for orders in groups.values():
        filled = [o.filled >= o.qty for o in orders]
        kind = "full" if all(filled) else "partial" if any(o.filled > 0 for o in orders) else "none"
        counts[kind] += 1
        if kind == "none":
            continue
        fs = [f for o in orders for f in fills_by_order.get(o.order_id, [])]
        if any(f.ticker not in settled for f in fs):
            unsettled += 1
            continue
        total = 0
        for f in fs:
            yes = settled[f.ticker]
            total += f.qty * (yes if f.side.value == "yes" else 10_000 - yes)
            total -= f.price * f.qty + f.fee_micro
        pnl[kind].append(total)
    n = len(groups)
    allp = pnl["full"] + pnl["partial"]
    return {
        "bundles": n,
        "fully_filled": counts["full"],
        "partially_filled": counts["partial"],
        "unfilled": counts["none"],
        "leg_risk_share": counts["partial"] / n if n else None,
        "bundles_scored": len(allp),
        "bundles_unsettled": unsettled,
        "full_bundles_net_usd": sum(pnl["full"]) / MICRO,
        "partial_bundles_net_usd": sum(pnl["partial"]) / MICRO,
        "worst_bundle_usd": min(allp) / MICRO if allp else None,
        "worst_full_bundle_usd": min(pnl["full"]) / MICRO if pnl["full"] else None,
        "worst_partial_bundle_usd": min(pnl["partial"]) / MICRO if pnl["partial"] else None,
    }
