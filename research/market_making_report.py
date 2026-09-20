"""Render a Stage 9 results JSON as markdown (docs are generated from data, never typed by hand).

python -m research.market_making_report results/stage9/dev/market_making.json > results/stage9/dev/report.md
"""

from __future__ import annotations

import json
import sys


def _e(x: dict | None, d: int = 2) -> str:
    if not x or x.get("value") is None:
        return "n/a"
    lo, hi = x.get("lo"), x.get("hi")
    ci = "" if lo is None or hi is None else f" [{lo:.{d}f}, {hi:.{d}f}]"
    return f"{x['value']:.{d}f}{ci}"


def _n(x, d: int = 2) -> str:
    return "n/a" if x is None else f"{x:.{d}f}"


def _table(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _group_rows(groups: dict) -> list[list[str]]:
    rows = []
    for name, g in groups.items():
        if not g.get("fills"):
            rows.append([name, "0", "", "", "", "", ""])
            continue
        rows.append(
            [
                name,
                str(g["fills"]),
                str(g["events"]),
                _n(g.get("spread_captured_cents_per_contract")),
                _e(g.get("markout_30s_cents")),
                _e(g.get("settled_pnl_per_fill_usd"), 3),
                _n(g.get("settled_pnl_total_usd"), 1),
            ]
        )
    return rows


def render(r: dict) -> str:
    P, ag = r["pooled"], r["pooled"]["all_fills"]
    o = [f"# Stage 9 results — {r['role']}", ""]
    o.append(
        f"Databases: {', '.join(r['databases'])}. Code fingerprint `{r['code_fingerprint']}`, "
        f"commit `{r['git_commit']}`, seed {r['seed']}, {r['n_boot']} bootstrap draws. Central "
        f"assumptions: maker queue model `{r['central']['maker']}`, {r['central']['latency_ms']:g} ms "
        f"latency, γ={r['central']['params']['gamma']}, k={r['central']['params']['k']}, size "
        f"{r['central']['params']['size']} contracts."
    )
    o += ["", "## Headline (baseline, central assumptions)", ""]
    o.append(
        _table(
            [
                "net P&L",
                "gross",
                "fees",
                "spread captured",
                "inventory contribution",
                "fills",
                "contracts",
            ],
            [
                [
                    f"${P['net']:.1f}",
                    f"${P['gross']:.1f}",
                    f"${P['fees']:.1f}",
                    f"${P['spread_captured']:.1f}",
                    f"${P['inventory_contribution']:.1f}",
                    str(ag["fills"]),
                    _n(ag.get("contracts"), 0),
                ]
            ],
        )
    )
    o.append("")
    o.append(
        f"* 30 s markout of fills: **{_e(ag.get('markout_30s_cents'))} ¢** "
        f"(negative = adverse selection; {ag.get('markout_30s_cents', {}).get('n_clusters', 0)} events)"
    )
    o.append(
        f"* Hold-to-settlement P&L per fill (settled markets): **{_e(ag.get('settled_pnl_per_fill_usd'), 3)} $** "
        f"over {ag.get('settled_fills', 0)} fills, total ${_n(ag.get('settled_pnl_total_usd'), 1)}"
    )
    o.append(
        f"* Worst database drawdown ${_n(P['max_drawdown_worst_db'], 1)}; worst settled event "
        f"${_n(P['worst_event_settled'], 1)}; kill-switch tripped per database: {P['killed']}; "
        f"mean |inventory| after a fill {_n(P['mean_abs_inventory_contracts'], 1)} contracts"
    )
    o += ["", "## Per database", ""]
    rows = []
    for name, e in r["per_database"].items():
        k = e["risk"]
        rows.append(
            [
                name,
                f"${e['pnl']['net']:.1f}",
                str(e["fills"]),
                _n(e["execution"]["spread_captured_cents_per_contract"]),
                f"${k['max_drawdown']:.1f}",
                f"${k['worst_event_settled']:.1f}",
                _n(k["sharpe_annualised"], 0),
                str(bool(k["killed"])) + (f" ({k['kill_reason']})" if k["killed"] else ""),
            ]
        )
    o.append(
        _table(
            [
                "database",
                "net",
                "fills",
                "spread ¢/contract",
                "max DD",
                "worst event",
                "Sharpe (annualised, not meaningful)",
                "killed",
            ],
            rows,
        )
    )
    o += ["", "### Execution and risk detail", ""]
    for name, e in r["per_database"].items():
        x, k = e["execution"], e["risk"]
        o.append(
            f"**{name}** — orders {x['orders']}, order fill rate {_n(x['order_fill_rate'], 3)}, "
            f"quantity fill rate {_n(x['quantity_fill_rate'], 3)}, cancellation rate "
            f"{_n(x['cancellation_rate'], 3)}, inventory turnover {_n(x['inventory_turnover'], 1)}×, "
            f"quote lifetime p50/p90 {_n(x['quote_lifetime_s'].get('p50'), 1)}/"
            f"{_n(x['quote_lifetime_s'].get('p90'), 1)} s, markout 5/30/300 s "
            f"{', '.join(_n((v or 0) / 100) for v in x['adverse_selection_markout_ticks'].values())} ¢, "
            f"|inventory| p50/p90/max {_n(k['inventory_abs_contracts'].get('p50'), 1)}/"
            f"{_n(k['inventory_abs_contracts'].get('p90'), 1)}/{_n(k['inventory_abs_contracts'].get('max'), 1)} "
            f"contracts, peak worst-case exposure ${_n(k['portfolio_worst_case_loss_max'], 1)}, "
            f"P&L vol ${_n(k['pnl_volatility_per_minute'], 2)}/min, event concentration of settled P&L "
            f"(effective events) {_n((k['event_concentration_of_settled_pnl'] or {}).get('effective_clusters'), 1)}."
        )
        o.append("")
    head = [
        "group",
        "fills",
        "events",
        "spread ¢/contract",
        "30 s markout ¢",
        "settled P&L / fill $",
        "settled P&L total $",
    ]
    for title, key in (
        ("Category", "category"),
        ("Liquidity (contracts traded in the trailing hour)", "liquidity_trailing_hour_volume"),
        ("Time to resolution", "time_to_resolution"),
        ("Probability range (mid at the fill)", "probability_range"),
    ):
        o += [f"## By {title.lower()}", "", _table(head, _group_rows(r["breakdowns"][key])), ""]
    o += ["## Inventory risk vs time to resolution", ""]
    rows = []
    for t in r["inventory_risk_by_time_to_resolution"]:
        if not t.get("fills"):
            rows.append([t["bucket"], "0", "", "", "", ""])
            continue
        rows.append(
            [
                t["bucket"],
                str(t["fills"]),
                str(t["events"]),
                _n(t["mean_abs_inventory_contracts"], 1),
                _n(t["mean_remaining_std_usd"]),
                _e(t.get("markout_30s_cents")),
            ]
        )
    o.append(
        _table(
            [
                "time to resolution",
                "fills",
                "events",
                "mean \\|inventory\\|",
                "remaining std $ (\\|q\\|√p(1−p))",
                "30 s markout ¢",
            ],
            rows,
        )
    )
    o += ["", "## Ablations (each design element removed in turn)", ""]
    rows = []
    for name, v in r["ablations"].items():
        rows.append(
            [
                name,
                f"${v['net_usd']:.1f}",
                str(v["fills"]),
                _n(v["contracts"], 0),
                _n(v["spread_cents_per_contract"]),
                _e(v["markout_30s_cents"]),
                _e(v["settled_pnl_per_fill_usd"], 3),
                _n(v["mean_abs_inventory"], 1),
                f"${v['max_drawdown_usd']:.0f}",
                f"${v['worst_event_settled_usd']:.1f}",
                str(v["killed"]),
            ]
        )
    o.append(
        _table(
            [
                "variant",
                "net",
                "fills",
                "contracts",
                "spread ¢",
                "30 s markout ¢",
                "settled P&L/fill $",
                "mean \\|inv\\|",
                "max DD",
                "worst event",
                "killed",
            ],
            rows,
        )
    )
    o += ["", "## Fill-model × latency sensitivity", ""]
    rows = [
        [
            f"{v['latency_ms']:g}",
            v["maker"],
            f"${v['net_usd']:.1f}",
            str(v["fills"]),
            _e(v["markout_30s_cents"]),
            _e(v["settled_pnl_per_fill_usd"], 3),
            str(v["killed"]),
        ]
        for v in r["fill_model_latency_sensitivity"]
    ]
    o.append(
        _table(
            [
                "latency ms",
                "maker fill model",
                "net",
                "fills",
                "30 s markout ¢",
                "settled P&L/fill $",
                "killed",
            ],
            rows,
        )
    )
    o += ["", "## Parameter grid (γ × k)", ""]
    rows = [
        [
            f"{v['gamma']:g}",
            f"{v['k']:g}",
            f"${v['net_usd']:.1f}",
            str(v["fills"]),
            _n(v["spread_cents_per_contract"]),
            _e(v["markout_30s_cents"]),
            _e(v["settled_pnl_per_fill_usd"], 3),
            _n(v["mean_abs_inventory"], 1),
        ]
        for v in r["parameter_grid"]
    ]
    o.append(
        _table(
            [
                "γ",
                "k",
                "net",
                "fills",
                "spread ¢",
                "30 s markout ¢",
                "settled P&L/fill $",
                "mean \\|inv\\|",
            ],
            rows,
        )
    )
    ps = r["partition_evidence_sensitivity"]
    o += [
        "",
        "## Partition evidence in the risk limits",
        "",
        f"With `EMPIRICAL`-level partitions instead of `DECLARED`: net ${ps['net_usd']:.1f}, fills {ps['fills']}, "
        f"max drawdown ${ps['max_drawdown_usd']:.1f}, mean |inventory| {_n(ps['mean_abs_inventory'], 1)}.",
    ]
    o += ["", "## Pre-registered hypotheses (docs/market_making.md §8)", ""]
    rows = [[h[0], h[1], h[2], h[3]] for h in r["hypotheses"]]
    o.append(_table(["id", "verdict", "observed", "criterion"], rows))
    return "\n".join(o) + "\n"


if __name__ == "__main__":
    print(render(json.load(open(sys.argv[1]))))
