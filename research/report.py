"""Render a Stage 6 results JSON as Markdown tables.

Numbers in the write-up are pasted from this output, never retyped.

    python -m research.report results/stage6/dev/arbitrage_research.json > results/stage6/dev/report.md
"""

from __future__ import annotations

import json
import sys

VARIANTS = ("baseline", "fees_half", "fee_free")
VARIANT_NOTE = {
    "baseline": "standard taker fees",
    "fees_half": "half fees (hypothetical fee tier)",
    "fee_free": "no fees (counterfactual, isolates the market structure)",
}


def fe(x: dict | None, scale: float = 1.0, d: int = 2) -> str:
    """'value [lo, hi]' from an estimate dict; 'n/a' when there is nothing to say."""
    if not x or x.get("value") is None:
        return "n/a"
    f = lambda v: "?" if v is None else f"{v * scale:.{d}f}"  # noqa: E731
    if x.get("lo") is None or x.get("hi") is None:
        return f"{f(x['value'])} [no interval]"
    return f"{f(x['value'])} [{f(x['lo'])}, {f(x['hi'])}]"


def br(b: dict | None, scale: float = 1.0, d: int = 2) -> str:
    """A [lo, hi] bracket of two estimates (optimistic/pessimistic reading), value only."""
    if not b or b.get("lo") is None or b.get("hi") is None:
        return "n/a"
    lo, hi = b["lo"]["value"], b["hi"]["value"]
    if lo is None or hi is None:
        return "n/a"
    lo, hi = sorted((lo, hi))
    return f"{lo * scale:.{d}f} to {hi * scale:.{d}f}"


def _s(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def provenance(r: dict) -> str:
    rows = [
        [
            d["label"],
            f"{d['recording_minutes']:.1f}",
            d["markets"],
            d["relations"],
            d["settled_markets"],
            d["episodes"],
            d["fingerprint"],
        ]
        for d in r["datasets"]
    ]
    head = (
        f"role **{r['role']}** - git `{r['git_commit'][:10]}` - code `{r['code_fingerprint']}` - "
        f"seed {r['params']['seed']} - {r['params']['n_boot']} bootstrap draws - "
        f"evidence >= {r['params']['min_level']}\n\n"
    )
    return head + table(
        [
            "dataset",
            "span (min, incl. gaps)",
            "markets",
            "relations",
            "settled",
            "episodes (all levels)",
            "fingerprint",
        ],
        rows,
    )


def section_a(P: dict) -> str:
    e = P["exposure"]
    out = [
        f"Observed {e['recording_minutes'] - sum(e['recording_gaps']) / 60:.0f} minutes of recording "
        f"(span {e['recording_minutes']:.0f} minus {sum(e['recording_gaps']) / 60:.0f} in gaps), "
        f"{e['poll_cycles']:,} poll cycles, "
        f"{e['events']} events, {e['relations_observed']} relations = "
        f"**{e['relation_hours']:.0f} relation-hours** (evidence >= the stated level). "
        f"Recording gaps > 30 s: {len(e['recording_gaps'])}.\n"
    ]
    rows = []
    for pop in ("sightings", "confirmed"):
        f = P["A_frequency"][pop]
        c = f["concentration"]
        z = f.get("zero_bounds")
        rows.append(
            [
                pop,
                f["episodes"],
                f"{f['events_with_episodes']} / {f['events_observed']}",
                fe(f["per_1000_relation_hours"], 1, 1),
                fe(f["violated_cycle_share"], 100, 2) + " %",
                "n/a" if c["top1_share"] is None else f"{c['top1_share']:.0%}",
                "n/a" if c["effective_clusters"] is None else f"{c['effective_clusters']:.1f}",
                "-" if not z else f"<= {z['events_share_upper']:.1%} of events",
            ]
        )
    out.append(
        table(
            [
                "population",
                "episodes",
                "events with / observed",
                "episodes per 1,000 relation-hours",
                "share of relation-cycles violated",
                "top event's share",
                "effective independent events",
                "if none: 95% upper bound",
            ],
            rows,
        )
    )
    d = P["diagnostic"]
    s = d["short_upper_bound_s"]
    out.append(
        f"\nChunk-skew fingerprint: {d['persisted']} of {d['sightings']} sightings persisted to a later "
        f"poll cycle ({(d['persisted_share'] or 0):.0%}); the {s['n']} that did not lived at most "
        f"p50 = {s['p50']:.2f} s, p75 = {s['p75']:.2f} s ({(s['share_under_1s'] or 0):.0%} under one second)."
        if s["n"]
        else "\nEvery sighting persisted to a later poll cycle."
    )
    bk = P["A_frequency"]["by_kind_confirmed"]
    out.append(
        "\nConfirmed episodes by relation kind: "
        + ", ".join(f"{k}: {v['episodes']} ({v['events']} events)" for k, v in bk.items())
    )
    return "\n".join(out)


def section_b(P: dict) -> str:
    out = []
    for pop in ("sightings", "confirmed"):
        rows = []
        for v in VARIANTS:
            fu = P["B_executability"][pop][v]["funnel"]
            rows.append(
                [v]
                + [f"{s['episodes']}" for s in fu["stages"]]
                + [
                    "n/a"
                    if not fu["displayed"]
                    else f"{fu['stages'][4]['episodes'] / fu['displayed']:.0%}"
                ]
            )
        out.append(
            f"**{pop}** ({P['B_executability'][pop]['baseline']['funnel']['displayed']} displayed)\n"
        )
        out.append(
            table(
                [
                    "cost model",
                    "displayed",
                    "liquid",
                    "after fees",
                    "after slippage",
                    "executable (100 contracts)",
                    "executable share",
                ],
                rows,
            )
        )
        out.append("")
    rows = []
    for pop in ("sightings", "confirmed"):
        for v in ("baseline", "fee_free"):
            ed = P["B_executability"][pop][v]["edge"]
            g, n = ed["gross_edge_cents_per_contract"], ed["net_profit_usd_at_best_size"]
            rows.append(
                [
                    pop,
                    v,
                    ed["n_displayed"],
                    "n/a"
                    if g["p50"] is None
                    else f"{g['p10']:.1f} / {g['p50']:.1f} / {g['p90']:.1f}",
                    "n/a"
                    if ed["liquidity_at_best_contracts"]["p50"] is None
                    else f"{ed['liquidity_at_best_contracts']['p50']:.0f} / {ed['book_capacity_contracts']['p50']:.0f}",
                    ed["n_tradable"],
                    "n/a"
                    if n["p50"] is None
                    else f"{n['p10']:.2f} / {n['p50']:.2f} / {n['p90']:.2f}",
                    f"{ed['total_net_usd']:.2f}",
                ]
            )
    out.append("Edge and liquidity (p10 / p50 / p90):\n")
    out.append(
        table(
            [
                "population",
                "cost model",
                "n",
                "gross edge (cents per contract)",
                "median liquidity at best / capacity (contracts)",
                "tradable",
                "net profit per opportunity ($, at <=100 contracts)",
                "total net $",
            ],
            rows,
        )
    )
    return "\n".join(out)


def section_b_cov(P: dict, pop: str = "sightings", v: str = "fee_free") -> str:
    cov = P["B_executability"][pop][v]["by_covariate"]
    rows = []
    for name in ("edge", "liquidity", "time_to_expiry", "category", "book_depth"):
        for b in cov[name]:
            if not b["n"]:
                continue
            rows.append(
                [
                    name,
                    b["bin"],
                    b["n"],
                    b["n_events"],
                    f"{b['rate']:.0%}",
                    f"{b['wilson'][0]:.0%}-{b['wilson'][1]:.0%}",
                    "n/a"
                    if not b["cluster"] or b["cluster"]["lo"] is None
                    else f"{b['cluster']['lo']:.0%}-{b['cluster']['hi']:.0%}",
                ]
            )
    return table(
        [
            "covariate",
            "bin",
            "episodes",
            "events",
            "P(executable)",
            "Wilson 95%",
            "event-cluster bootstrap 95%",
        ],
        rows,
    )


def section_realised(r: dict) -> str:
    rows = []
    for label, per in r["realised_vs_promised"].items():
        for v in ("baseline", "fee_free"):
            for level, x in per.get(v, {}).items():
                rows.append(
                    [
                        label,
                        v,
                        level,
                        x["settled_tradable"],
                        x["events"],
                        x["realised_below_promised"],
                        f"{x['share_below']:.0%} ({x['wilson'][0]:.0%}-{x['wilson'][1]:.0%})",
                        f"{x['realised_usd']:.2f}",
                        f"{x['promised_usd']:.2f}",
                        f"{x['worst_usd']:.2f}",
                    ]
                )
    if not rows:
        return "No tradable opportunities on markets that had settled."
    return table(
        [
            "dataset",
            "cost model",
            "evidence",
            "settled & tradable",
            "events",
            "realised < promised",
            "share (Wilson)",
            "realised $",
            "promised $",
            "worst $",
        ],
        rows,
    )


def section_c(P: dict) -> str:
    out = []
    for pop in ("sightings", "confirmed"):
        L = P["C_lifetime"][pop]
        lt = L["lifetime"]
        if not lt.get("n"):
            out.append(f"**{pop}**: none.\n")
            continue
        rows = []
        for g in lt["survival_grid"]:
            if g["t_s"] in (0.1, 0.5, 1, 3, 5, 10, 30, 60):
                rows.append([g["t_s"], fe(g["lower"], 1, 2), fe(g["upper"], 1, 2)])
        out.append(
            f"**{pop}** (n = {lt['n']}, censored {lt['censored']}); Kaplan-Meier median lifetime "
            f"{_s(lt['km_median_s']['pessimistic'])} to {_s(lt['km_median_s']['optimistic'])} s "
            f"(pessimistic to optimistic reading of each interval)\n"
        )
        out.append(table(["t (s)", "P(life > t): lower bound", "upper bound"], rows))
        ec = L["end_causes"]
        cs = ec["consumed_share"]
        out.append(
            f"\nEnded {ec['ended']} (censored {ec['censored']}: {ec['censored_reasons']}); causes {ec['counts']}; "
            f"consumed by a trade: {fe(cs.get('cluster'), 100, 0) if cs.get('cluster') else 'n/a'} % "
            f"(Wilson {cs['wilson'][0]:.0%}-{cs['wilson'][1]:.0%})."
        )
        a = L["edge_association"]
        out.append(
            "Spearman vs lifetime lower bound - edge: "
            f"{fe(a.get('edge_vs_lifetime'))}; liquidity: {fe(a.get('liquidity_vs_lifetime'))}; "
            f"depth: {fe(a.get('depth_vs_lifetime'))}.\n"
        )
        if lt.get("exponential"):
            x = lt["exponential"]
            out.append(
                f"Exponential fit (model, not identified below the polling interval): mean life "
                f"{x['mean_life_s']:.1f} s, rate {fe(x['rate_ci'], 1, 4)} per s.\n"
            )
    return "\n".join(out)


def section_d(P: dict) -> str:
    out = []
    for pop in ("sightings", "confirmed"):
        D = P["D_latency"][pop]
        rows = []
        for r in D["survival"]:
            rows.append(
                [
                    r["delta_s"],
                    r["n"],
                    br(r["alive"], 100, 0) + " %",
                    "n/a" if r["unresolved_share"] is None else f"{r['unresolved_share']:.0%}",
                    br(r["margin_retained"], 100, 0) + " %",
                ]
            )
        out.append(
            f"**{pop}: does the displayed edge survive D?** (bracket: optimistic/pessimistic reading)\n"
        )
        out.append(
            table(
                [
                    "D (s)",
                    "n",
                    "edge still displayed",
                    "unresolved by the data",
                    "edge size retained",
                ],
                rows,
            )
        )
        for v in ("fee_free", "baseline"):
            ex = D["execution"][v]
            if not ex[0]["n"]:
                continue
            rows = []
            for r in ex:
                if not r["n"]:
                    continue
                rows.append(
                    [
                        r["delta_s"],
                        r["n"],
                        br(r["full_fill"], 100, 0) + " %",
                        br(r["net_usd_per_detected"], 1, 2),
                        br(r["net_retained_fraction"], 100, 0) + " %"
                        if r["net_retained_fraction"]
                        else "n/a",
                        "-".join(str(v) for v in sorted(r["missed_opportunities"].values())),
                    ]
                )
            out.append(f"\n{pop}, tradable under **{v}** ({VARIANT_NOTE[v]}):\n")
            out.append(
                table(
                    [
                        "D (s)",
                        "n",
                        "fills whole",
                        "net $ per opportunity",
                        "net edge retained",
                        "missed",
                    ],
                    rows,
                )
            )
        lf = D["latency_funnel"]
        rows = []
        for v in VARIANTS:
            f = lf[v]
            for r in f["latency"]:
                rows.append(
                    [
                        v,
                        r["delta_s"],
                        r["executable_at_first_sight"],
                        r["observable"],
                        f"{r['survive_lo']}-{r['survive_hi']}",
                    ]
                )
        out.append(
            f"\n{pop}: the funnel's last stage (executable at first sight -> still fillable whole D later):\n"
        )
        out.append(
            table(
                [
                    "cost model",
                    "D (s)",
                    "executable at first sight",
                    "observable",
                    "still fillable (range)",
                ],
                rows,
            )
        )
        out.append("")
    return "\n".join(out)


def section_engine(r: dict) -> str:
    out = []
    for label, per in r.get("engine_sweep", {}).items():
        for name, rows in per.items():
            out.append(f"**{label} - {name}**\n")
            out.append(
                table(
                    [
                        "latency (ms)",
                        "opportunities",
                        "bundles",
                        "fully filled",
                        "partial (leg risk)",
                        "unfilled",
                        "net $ (whole run)",
                        "scored bundles",
                        "full bundles $",
                        "partial bundles $",
                        "worst bundle $",
                    ],
                    [
                        [
                            x["latency_ms"],
                            x["opportunities"],
                            x["bundles"],
                            x["fully_filled"],
                            x["partially_filled"],
                            x["unfilled"],
                            f"{x['net_usd']:.2f}",
                            x["bundles_scored"],
                            f"{x['full_bundles_net_usd']:.2f}",
                            f"{x['partial_bundles_net_usd']:.2f}",
                            "n/a"
                            if x["worst_bundle_usd"] is None
                            else f"{x['worst_bundle_usd']:.2f}",
                        ]
                        for x in rows
                    ],
                )
            )
            out.append("")
    return "\n".join(out) or "not run"


def section_categories(P: dict) -> str:
    rows = []
    for c in P["categories"]:
        s, k = c["sightings"], c["confirmed"]
        zb = s.get("zero_bounds")
        rows.append(
            [
                c["category"],
                f"{c['relation_hours']:.0f}",
                c["events_observed"],
                s["episodes"],
                fe(s["per_1000_relation_hours"], 1, 1)
                if s["episodes"]
                else (
                    f"0 (<= {zb['per_1000_relation_hours_upper_naive']:.1f} naive)" if zb else "0"
                ),
                k["episodes"],
                fe(k["per_1000_relation_hours"], 1, 1)
                if k["episodes"]
                else (
                    f"0 (<= {(k.get('zero_bounds') or {}).get('per_1000_relation_hours_upper_naive', 0):.1f} naive)"
                ),
                c["executable_fee_free"]["confirmed"],
                c["executable_baseline"]["confirmed"],
                "yes" if c["low_n"] else "",
            ]
        )
    return table(
        [
            "category",
            "relation-hours",
            "events",
            "sightings",
            "sightings per 1,000 rh",
            "confirmed",
            "confirmed per 1,000 rh",
            "executable (no fees)",
            "executable (fees)",
            "< 5 events",
        ],
        rows,
    )


def section_sensitivity(P: dict) -> str:
    rows = [
        [
            x["min_level"],
            x["sightings"],
            fe(x["rate_sightings"], 1, 1),
            x["confirmed"],
            fe(x["rate_confirmed"], 1, 1),
            f"{x['executable_baseline'][0]} / {x['executable_baseline'][1]}",
            f"{x['executable_fee_free'][0]} / {x['executable_fee_free'][1]}",
        ]
        for x in P["evidence_sensitivity"]
    ]
    return table(
        [
            "relations trusted at >=",
            "sightings",
            "per 1,000 rh",
            "confirmed",
            "per 1,000 rh",
            "executable with fees (sightings / confirmed)",
            "executable no fees (sightings / confirmed)",
        ],
        rows,
    )


def section_d_short(P: dict) -> str:
    """Survival of the displayed edge and the funnel's latency stage (the long per-cost-model
    execution tables stay in the full report)."""
    out = []
    for pop in ("sightings", "confirmed"):
        D = P["D_latency"][pop]
        rows = [
            [
                r["delta_s"],
                r["n"],
                br(r["alive"], 100, 0) + " %",
                "n/a" if r["unresolved_share"] is None else f"{r['unresolved_share']:.0%}",
                br(r["margin_retained"], 100, 0) + " %",
            ]
            for r in D["survival"]
            if r["delta_s"] in (0.001, 0.01, 0.05, 0.25, 1, 3, 10, 30, 60)
        ]
        out.append(
            f"**{pop}: does the displayed edge survive D?** (optimistic to pessimistic reading)\n"
        )
        out.append(
            table(
                [
                    "D (s)",
                    "n",
                    "edge still displayed",
                    "unresolved by the data",
                    "edge size retained",
                ],
                rows,
            )
        )
        rows = []
        for v in VARIANTS:
            for r in D["latency_funnel"][v]["latency"]:
                rows.append(
                    [
                        v,
                        r["delta_s"],
                        r["executable_at_first_sight"],
                        r["observable"],
                        f"{r['survive_lo']}-{r['survive_hi']}",
                    ]
                )
        out.append(f"\n{pop}: executable at first sight -> still fillable whole D later:\n")
        out.append(
            table(
                [
                    "cost model",
                    "D (s)",
                    "executable at first sight",
                    "observable",
                    "still fillable (range)",
                ],
                rows,
            )
        )
        out.append("")
    return "\n".join(out)


SECTIONS = {
    "provenance": lambda r: provenance(r),
    "a": lambda r: section_a(r["pooled"]),
    "b": lambda r: section_b(r["pooled"]),
    "b_cov": lambda r: section_b_cov(r["pooled"], "sightings", "fee_free"),
    "realised": lambda r: section_realised(r),
    "c": lambda r: section_c(r["pooled"]),
    "d": lambda r: section_d(r["pooled"]),
    "d_short": lambda r: section_d_short(r["pooled"]),
    "engine": lambda r: section_engine(r),
    "categories": lambda r: section_categories(r["pooled"]),
    "sensitivity": lambda r: section_sensitivity(r["pooled"]),
}


def render_only(r: dict, names: list[str]) -> str:
    return "\n\n".join(SECTIONS[n](r) for n in names) + "\n"


def render(r: dict) -> str:
    P = r["pooled"]
    parts = [
        "# Stage 6 results",
        provenance(r),
        "## Counts and exposure (Experiment A)",
        section_a(P),
        "## Executability (Experiment B)",
        section_b(P),
        "\nP(executable | detected) by covariate - sightings, no fees (the option with the most events):\n",
        section_b_cov(P, "sightings", "fee_free"),
        "\nBundle payoff vs. promise, on markets that had settled by the time of analysis:\n",
        section_realised(r),
        "## Lifetime and edge decay (Experiment C)",
        section_c(P),
        "## Latency (Experiment D)",
        section_d(P),
        "### Engine sweep (Stage 5 engine, leg risk and realised P&L)",
        section_engine(r),
        "## Categories",
        section_categories(P),
        "## Sensitivity to the evidence threshold",
        section_sensitivity(P),
    ]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    args = sys.argv[1:]
    data = json.load(open(args[0]))
    if len(args) > 2 and args[1] == "--only":
        print(render_only(data, args[2].split(",")))
    else:
        print(render(data))
