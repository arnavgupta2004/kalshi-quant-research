"""Score the pre-specified hypotheses (docs/arbitrage_research.md s.2) against a results JSON.

The criteria are transcribed from the pre-registration, not chosen after seeing results; each row
prints the observed value next to the criterion so a reader can check the verdict.

    python -m research.hypotheses results/stage6/confirm/arbitrage_research.json
"""

from __future__ import annotations

import json
import sys


def _v(x):
    return None if not x else x.get("value")


def evaluate(r: dict) -> list[tuple[str, str, str, str]]:
    """(id, verdict, observed, criterion)."""
    P = r["pooled"]
    out: list[tuple[str, str, str, str]] = []

    def add(hid, ok, observed, criterion):
        out.append((hid, "consistent" if ok else "NOT consistent", observed, criterion))

    conf = P["A_frequency"]["confirmed"]["per_1000_relation_hours"]
    add(
        "A1",
        conf["lo"] is not None and conf["lo"] > 0 and conf["value"] < 200,
        f"{conf['value']:.1f} [{conf['lo']:.1f}, {conf['hi']:.1f}]",
        "lower bound > 0 and rate < 200 per 1,000 rh",
    )
    d = P["diagnostic"]
    add(
        "A2",
        d["persisted_share"] < 0.5,
        f"{d['persisted_share']:.0%} persisted",
        "persisted share of sightings < 50%",
    )
    s = d["short_upper_bound_s"]
    u = s["share_under_1s"]
    add(
        "A3",
        u is not None and u > 0.5,
        "n/a" if u is None else f"{u:.0%} of {s['n']} under 1 s",
        "> 50% of non-persisted sightings have upper bound < 1 s",
    )
    bad = [
        f"{c['category']} ({c['confirmed']['episodes']} confirmed, {c['events_observed']} events)"
        for c in P["categories"]
        if c["category"] != "Sports"
        and c["events_observed"] >= 5
        and c["confirmed"]["episodes"] > 0
    ]
    add(
        "A4",
        not bad,
        "counter-examples: " + (", ".join(bad) or "none"),
        "no non-sports category with >= 5 events has a confirmed episode",
    )
    sens = {x["min_level"]: x for x in P["evidence_sensitivity"]}
    n_conf = sens["DECLARED"]["confirmed"]
    share = sens["PROVEN"]["confirmed"] / n_conf if n_conf else 0
    add(
        "A6",
        share < 0.25,
        f"{sens['PROVEN']['confirmed']} of {n_conf} confirmed are PROVEN",
        "PROVEN share of confirmed < 25%",
    )
    fb = P["B_executability"]["confirmed"]
    base = fb["baseline"]["funnel"]
    free = fb["fee_free"]["funnel"]
    bs = base["stages"][4]["episodes"] / base["displayed"] if base["displayed"] else 0
    fs = free["stages"][4]["episodes"] / free["displayed"] if free["displayed"] else 0
    add(
        "B1",
        bs < 0.05,
        f"{base['stages'][4]['episodes']} / {base['displayed']} = {bs:.1%}",
        "confirmed baseline executable share < 5%",
    )
    add(
        "B2",
        fs > 2 * bs and fs > 0,
        f"fee-free {fs:.1%} vs baseline {bs:.1%}",
        "fee-free executable share > 2x baseline",
    )
    tot = short = 0
    proven_short = 0
    for per in r["realised_vs_promised"].values():
        for lvl, x in per.get("fee_free", {}).items():
            tot += x["settled_tradable"]
            short += x["realised_below_promised"]
            if lvl == "PROVEN":
                proven_short += x["realised_below_promised"]
    add(
        "B3",
        proven_short == 0 and (tot == 0 or short / tot < 0.10),
        f"{short} shortfalls in {tot} settled tradable episodes ({proven_short} PROVEN)",
        "0 PROVEN shortfalls; overall shortfall share < 10%",
    )
    lt = P["C_lifetime"]["confirmed"]["lifetime"]
    lo, hi = lt["km_median_s"]["pessimistic"], lt["km_median_s"]["optimistic"]
    add(
        "C1",
        lo is not None and hi is not None and 3 <= lo and hi <= 30,
        f"{lo:.1f} to {hi:.1f} s",
        "both readings of the median inside 3-30 s",
    )
    cs = P["C_lifetime"]["confirmed"]["end_causes"]["consumed_share"]["value"]
    add(
        "C2",
        cs is not None and cs > 0.5,
        "n/a" if cs is None else f"{cs:.0%} consumed",
        "consumed share of ended confirmed episodes > 50%",
    )
    rho = _v(P["C_lifetime"]["sightings"]["edge_association"]["liquidity_vs_lifetime"])
    add("C3", rho is not None and rho < 0, f"rho = {rho:.2f}", "Spearman(liquidity, lifetime) < 0")
    rows = {x["delta_s"]: x for x in P["D_latency"]["sightings"]["survival"]}

    def width(dl):
        a = rows[dl]["alive"]
        return (
            None
            if not a["lo"] or a["lo"]["value"] is None
            else abs(a["hi"]["value"] - a["lo"]["value"])
        )

    w1, w2 = width(0.001), width(1)
    add(
        "D1",
        w1 is not None and w2 is not None and w1 >= 0.10 and w2 <= 0.05,
        f"width {(w1 or 0) * 100:.0f} pp at 1 ms, {(w2 or 0) * 100:.0f} pp at 1 s",
        "width >= 10 pp at 1 ms and <= 5 pp at 1 s",
    )
    a3 = rows[3]["alive"]
    lo3, hi3 = sorted((a3["lo"]["value"], a3["hi"]["value"]))
    add(
        "D2",
        0.25 <= lo3 <= 0.60 or 0.25 <= hi3 <= 0.60,
        f"{lo3:.0%} to {hi3:.0%} alive at 3 s",
        "either bound in 25-60% (lenient, as pre-specified)",
    )
    part = {250: [0, 0], 3000: [0, 0]}
    for per in r.get("engine_sweep", {}).values():
        for row in per["declared_fee_free"]:
            if row["latency_ms"] in part:
                part[row["latency_ms"]][0] += row["partially_filled"]
                part[row["latency_ms"]][1] += row["bundles"]
    sh = {k: (v[0] / v[1] if v[1] else None) for k, v in part.items()}
    add(
        "D3",
        all(v is not None for v in sh.values()) and sh[250] < 0.05 and sh[3000] > 0.20,
        ", ".join(f"{k} ms: {v[0]}/{v[1]}" for k, v in part.items()),
        "partial share < 5% at 250 ms and > 20% at 3 s (DECLARED, fee-free)",
    )
    worst = 0.0
    lat: dict[int, float] = {}
    for per in r.get("engine_sweep", {}).values():
        for row in per["declared_baseline_fees"]:
            lat[row["latency_ms"]] = lat.get(row["latency_ms"], 0.0) + row["full_bundles_net_usd"]
    worst = max(lat.values()) if lat else 0.0
    add(
        "D4",
        worst < 5.0,
        f"max over latencies of scored full-bundle P&L = ${worst:.2f}",
        "scored full-bundle P&L with standard fees < $5 at every latency",
    )
    return out


def render(r: dict) -> str:
    rows = evaluate(r)
    n_ok = sum(1 for x in rows if x[1] == "consistent")
    lines = [
        f"{n_ok} of {len(rows)} scored hypotheses consistent.\n",
        "| # | verdict | observed | criterion |",
        "|---|---|---|---|",
    ]
    lines += [f"| {h} | {v} | {o} | {c} |" for h, v, o, c in rows]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    print(render(json.load(open(sys.argv[1]))))
