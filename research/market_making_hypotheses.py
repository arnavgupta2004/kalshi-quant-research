"""Score the pre-specified Stage 9 hypotheses (docs/market_making.md s.8) against a results JSON.

The criteria are transcribed from the pre-registration, written after the DEVELOPMENT run and
before the confirmatory data were ever run with a market maker.  Each row prints the observed value
next to the criterion so a reader can check the verdict.

    python -m research.market_making_hypotheses results/stage9/confirm/market_making.json

Sufficiency rule: a claim resting on fewer than ``MIN_CLUSTERS`` independent events is reported as
"inconclusive", never as consistent or not.
"""

from __future__ import annotations

import json
import sys

MIN_CLUSTERS = 20


def _fmt(e: dict | None, d: int = 2) -> str:
    if not e or e.get("value") is None:
        return "n/a"
    lo, hi = e.get("lo"), e.get("hi")
    ci = "" if lo is None or hi is None else f" [{lo:.{d}f}, {hi:.{d}f}]"
    return f"{e['value']:.{d}f}{ci} ({e['n_clusters']} events)"


def _by_letter(ablations: dict) -> dict[str, dict]:
    return {k[0]: v for k, v in ablations.items()}


def evaluate(r: dict) -> list[tuple[str, str, str, str]]:
    """(id, verdict, observed, criterion)."""
    out: list[tuple[str, str, str, str]] = []
    g = r["pooled"]["all_fills"]

    def add(hid, ok, observed, criterion, *, clusters: int | None = None):
        if clusters is not None and clusters < MIN_CLUSTERS:
            verdict = f"inconclusive (< {MIN_CLUSTERS} events)"
        else:
            verdict = "consistent" if ok else "NOT consistent"
        out.append((hid, verdict, observed, criterion))

    # M1  adverse selection
    mo = g.get("markout_30s_cents") or {}
    add(
        "M1",
        mo.get("hi") is not None and mo["hi"] < 0,
        _fmt(mo),
        "30 s markout of the baseline's fills (cents, bought side): 95% interval entirely below 0",
        clusters=mo.get("n_clusters", 0),
    )
    # M2  no edge, whatever the fill model
    sp = g.get("settled_pnl_per_fill_usd") or {}
    cells = r["fill_model_latency_sensitivity"]
    neg = sum(1 for c in cells if c["net_usd"] < 0)
    add(
        "M2",
        sp.get("value") is not None and sp["value"] < 0 and neg == len(cells),
        f"settled P&L/fill {_fmt(sp, 3)}; net < 0 in {neg}/{len(cells)} fill-model x latency cells",
        "point estimate of hold-to-settlement P&L per fill < 0 AND net P&L < 0 in every cell",
        clusters=sp.get("n_clusters", 0),
    )
    # M3  skew reduces inventory risk; it does not buy profit (M3b: observed in development)
    a = _by_letter(r["ablations"])
    b, d = a["B"], a["D"]
    add(
        "M3a",
        d["mean_abs_inventory"] < b["mean_abs_inventory"]
        and d["worst_event_settled_usd"] >= b["worst_event_settled_usd"],
        f"|inventory| {d['mean_abs_inventory']:.1f} vs {b['mean_abs_inventory']:.1f} contracts; "
        f"worst event {d['worst_event_settled_usd']:.1f} vs {b['worst_event_settled_usd']:.1f} USD",
        "skew (D) has lower mean |inventory| AND a no-worse worst-event loss than no skew (B)",
    )
    add(
        "M3b",
        d["net_usd"] <= b["net_usd"],
        f"net {d['net_usd']:.1f} (skew) vs {b['net_usd']:.1f} (no skew) USD",
        "skew does not raise net P&L: net(D) <= net(B)  [from development data, not derived]",
    )
    # M4  the kill-switch caps the drawdown
    e, f = a["E"], a["F"]
    add(
        "M4",
        f["max_drawdown_usd"] <= e["max_drawdown_usd"] + 1e-9,
        f"max drawdown {f['max_drawdown_usd']:.1f} (kill) vs {e['max_drawdown_usd']:.1f} (none)",
        "max drawdown with the kill-switch <= without it",
    )
    # M5  width is not the fix
    grid = r["parameter_grid"]
    lows = [(c["gamma"], c["k"], (c.get("settled_pnl_per_fill_usd") or {}).get("lo")) for c in grid]
    winners = [x for x in lows if x[2] is not None and x[2] > 0]
    add(
        "M5",
        not winners,
        f"{len(winners)}/{len(grid)} grid cells have settled P&L/fill with lower bound > 0",
        "no (gamma, k) cell has a settled P&L per fill whose 95% interval lies above 0",
    )
    # M6  adverse selection worse near resolution (weak: few near-settlement events)
    ttr = {t["bucket"]: t for t in r["inventory_risk_by_time_to_resolution"]}

    def wmean(names):
        rows = [ttr[n] for n in names if n in ttr and ttr[n].get("markout_30s_cents")]
        n = sum(x["fills"] for x in rows)
        return (
            None if not n else sum(x["fills"] * x["markout_30s_cents"]["value"] for x in rows) / n
        )

    near, far = wmean(["<0.5h", "0.5-1.5h"]), wmean(["1.5-4h", "4-12h", ">=12h"])
    add(
        "M6",
        near is not None and far is not None and near < far,
        "n/a" if near is None or far is None else f"{near:.2f}c (< 1.5 h) vs {far:.2f}c (>= 1.5 h)",
        "fill-weighted 30 s markout is more negative within 1.5 h of resolution than beyond (point "
        "comparison)",
    )
    # M7  the fill-model bounds behave as bounds
    ok7 = True
    seen = []
    for lat in sorted({c["latency_ms"] for c in cells}):
        row = {c["maker"]: c["fills"] for c in cells if c["latency_ms"] == lat}
        ok7 &= row["optimistic"] >= row["pessimistic"]
        seen.append(f"{lat:g} ms: {row['pessimistic']} <= {row['optimistic']}")
    add(
        "M7",
        ok7,
        "; ".join(seen),
        "at each latency, fills under the optimistic queue model >= under the pessimistic one",
    )
    return out


if __name__ == "__main__":
    res = json.load(open(sys.argv[1]))
    for hid, verdict, observed, crit in evaluate(res):
        print(f"{hid:4s} {verdict:34s} {observed}\n     criterion: {crit}")
