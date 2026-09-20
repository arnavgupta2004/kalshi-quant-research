"""The final research report is rendered from the results files, not typed.

Every number in ``docs/research_report.md`` is a ``{{placeholder}}`` in
``docs/research_report.tmpl.md`` that this module resolves against the JSON that the stages wrote
(``results/stage*/``).  A number can therefore not drift from the analysis that produced it; when
a result file does not exist yet (the pending confirmatory and out-of-sample runs) the placeholder
renders as ``(pending)`` and the report fills itself in the next time it is rendered.

    python -m scripts.stage12_report            # write docs/research_report.md
    python -m scripts.stage12_report --check    # fail if the committed report is stale

Placeholder forms
    {{n.<name>}}        a named number (``CLAIMS``)
    {{h6.A1}}           the OBSERVED value of a pre-registered hypothesis (stage 6, 8, 9, 10v, 10c)
    {{v6.A1}}           its verdict (consistent / NOT consistent / inconclusive)
    {{score.6}}         "11 of 15" - hypotheses scored consistent, per stage
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PENDING = "(pending)"

FILES = {
    "s3": "results/stage3/relations_summary.json",
    "s4": "results/stage4/replay.json",
    "s5": "results/stage5/backtest_demo.json",
    "s6": "results/stage6/confirm/arbitrage_research.json",
    "s6dev": "results/stage6/dev/arbitrage_research.json",
    "s7": "results/stage7/probability_models.json",
    "s8": "results/stage8/confirm/calibration.json",
    "s9": "results/stage9/confirm/market_making.json",
    "s9dev": "results/stage9/dev/market_making.json",
    "s10": "results/stage10/dev/adaptive.json",
    "s10c": "results/stage10/confirm/adaptive.json",
    "s10tox": "results/stage10/dev/toxicity.json",
    "s11b": "results/stage11/rest_baseline/summary.json",
    "s11a": "results/stage11/rest_adaptive/summary.json",
    "s11r": "results/stage11/rest_arbitrage/summary.json",
    "s12": "results/stage12/final/final_oos.json",
}


def load_results(root: Path = ROOT) -> dict[str, dict | None]:
    out: dict[str, dict | None] = {}
    for k, rel in FILES.items():
        p = root / rel
        out[k] = json.loads(p.read_text()) if p.exists() else None
    return out


# ------------------------------------------------------------------ formatting helpers
def ci(x: dict | None, d: int = 2, scale: float = 1.0) -> str:
    if not x or x.get("value") is None:
        return "n/a"
    lo, hi = x.get("lo"), x.get("hi")
    s = f"{x['value'] * scale:.{d}f}"
    return s if lo is None or hi is None else f"{s} [{lo * scale:.{d}f}, {hi * scale:.{d}f}]"


def num(x: float | None, d: int = 1) -> str:
    return "n/a" if x is None else f"{x:,.{d}f}"


def usd(x: float | None, d: int = 0) -> str:
    if x is None:
        return "n/a"
    return f"-${abs(x):,.{d}f}" if x < 0 else f"${x:,.{d}f}"


def _need(*keys: str):
    """Decorator: the claim renders as (pending) unless every listed result file exists."""

    def deco(fn: Callable) -> Callable:
        def wrapped(R: dict) -> str:
            if any(R.get(k) is None for k in keys):
                return PENDING
            return fn(R)

        wrapped.needs = keys  # type: ignore[attr-defined]
        return wrapped

    return deco


# ------------------------------------------------------------------ hypotheses per stage
def hypotheses(R: dict, stage: str) -> list[tuple[str, str, str, str]]:
    """(id, verdict, observed, criterion) rows for a stage, or [] if its results do not exist."""
    if stage == "6" and R["s6"]:
        from research.hypotheses import evaluate

        return [tuple(r) for r in evaluate(R["s6"])]
    if stage == "8" and R["s8"]:
        rows = R["s8"]["hypotheses_stage7"] + R["s8"]["hypotheses_stage8"]
        return [(r["id"], r["verdict"], r["observed"], r["criterion"]) for r in rows]
    if stage == "9" and R["s9"]:
        return [tuple(r) for r in R["s9"]["hypotheses"]]
    if stage == "10v" and R["s10"]:
        return [tuple(r) for r in R["s10"]["hypotheses_on_validation"]]
    if stage == "10c" and R["s10c"]:
        return [tuple(r) for r in R["s10c"]["hypotheses"]]
    if stage == "12a" and R["s12"]:
        return [tuple(r) for r in R["s12"]["stage6_hypotheses"]]
    if stage == "12m" and R["s12"]:
        return [tuple(r) for r in R["s12"]["market_making_hypotheses"]]
    return []


def _hyp(R: dict, stage: str, hid: str) -> tuple[str, str, str, str] | None:
    return next((r for r in hypotheses(R, stage) if r[0] == hid), None)


# ------------------------------------------------------------------ named numbers
def _levels(R: dict) -> dict[str, tuple[int, int]]:
    out: dict[str, list[int]] = {}
    for row in R["s3"]["relations_by_kind_level"]:
        a = out.setdefault(row["level"], [0, 0])
        a[0] += row["instances"]
        a[1] += row["violated"]
    return {k: (v[0], v[1]) for k, v in out.items()}


def _row(R: dict, key: str, name: str) -> dict:
    return R[key]["blocks"][name]["ladder"]["rows"]


def _pct(x: float, d: int = 0) -> str:
    return f"{100 * x:.{d}f}%"


BASE10 = "0 baseline (Stage 9)"
FULL10 = "6 + time-to-resolution skew (= FULL)"

CLAIMS: dict[str, Callable[[dict], str]] = {}


def claim(name: str, *needs: str):
    def deco(fn: Callable[[dict], str]):
        CLAIMS[name] = _need(*needs)(fn) if needs else fn
        return fn

    return deco


# ---- stage 3
@claim("s3_events", "s3")
def _(R):
    return f"{R['s3']['settled_events']:,}"


@claim("s3_markets", "s3")
def _(R):
    return f"{R['s3']['settled_markets']:,}"


@claim("s3_strong_instances", "s3")
def _(R):
    lv = _levels(R)
    return f"{sum(lv[k][0] for k in ('PROVEN', 'LATTICE', 'DECLARED') if k in lv):,}"


@claim("s3_strong_violated", "s3")
def _(R):
    lv = _levels(R)
    return str(sum(lv[k][1] for k in ("PROVEN", "LATTICE", "DECLARED") if k in lv))


@claim("s3_empirical_rate", "s3")
def _(R):
    n, v = _levels(R)["EMPIRICAL"]
    return f"{100 * v / n:.2f}%"


@claim("s3_unverified_rate", "s3")
def _(R):
    n, v = _levels(R)["UNVERIFIED"]
    return f"{100 * v / n:.1f}%"


# ---- stage 4
@claim("s4_displayed", "s4")
def _(R):
    return str(R["s4"]["variants"]["baseline"]["funnel"]["displayed"])


@claim("s4_executable", "s4")
def _(R):
    return str(R["s4"]["variants"]["baseline"]["funnel"]["executable"])


@claim("s4_fee_free_executable", "s4")
def _(R):
    return str(R["s4"]["variants"]["fee_free"]["funnel"]["executable"])


@claim("s4_minutes", "s4")
def _(R):
    return f"{R['s4']['span_minutes']:.0f}"


# ---- stage 5
@claim("s5_passive_range", "s5")
def _(R):
    lo, hi = R["s5"]["passive_range_usd"]["200"]
    return f"${abs(hi - lo):.0f}"


@claim("s5_passive_best", "s5")
def _(R):
    return usd(R["s5"]["passive_range_usd"]["200"][1], 2)


# ---- stage 6
@claim("s6_minutes", "s6")
def _(R):
    return f"{R['s6']['pooled']['exposure']['recording_minutes']:.0f}"


@claim("s6_events", "s6")
def _(R):
    return str(R["s6"]["pooled"]["exposure"]["events"])


@claim("s6_confirmed", "s6")
def _(R):
    return str(R["s6"]["pooled"]["counts"]["confirmed_at_or_above_min_level"])


@claim("s6_sightings", "s6")
def _(R):
    return str(R["s6"]["pooled"]["counts"]["sightings_at_or_above_min_level"])


@claim("s6_rate", "s6")
def _(R):
    return ci(R["s6"]["pooled"]["A_frequency"]["confirmed"]["per_1000_relation_hours"], 1)


@claim("s6_rate_sightings", "s6")
def _(R):
    return ci(R["s6"]["pooled"]["A_frequency"]["sightings"]["per_1000_relation_hours"], 1)


@claim("s6_dev_rate", "s6dev")
def _(R):
    return ci(R["s6dev"]["pooled"]["A_frequency"]["confirmed"]["per_1000_relation_hours"], 1)


@claim("s6_funnel", "s6")
def _(R):
    st = R["s6"]["pooled"]["B_executability"]["confirmed"]["baseline"]["funnel"]["stages"]
    return " → ".join(f"{s['stage'].replace('_', ' ')} {s['episodes']}" for s in st)


@claim("s6_score", "s6")
def _(R):
    rows = hypotheses(R, "6")
    return f"{sum(r[1] == 'consistent' for r in rows)} of {len(rows)}"


# ---- stage 7 / 8
@claim("s7_market_ll", "s7")
def _(R):
    return f"{R['s7']['history']['scores']['market_reference']['log_loss']['value']:.4f}"


@claim("s7_const_ll", "s7")
def _(R):
    return f"{R['s7']['history']['scores']['constant_base_rate']['log_loss']['value']:.3f}"


@claim("s7_A_ll", "s7")
def _(R):
    return f"{R['s7']['history']['scores']['A_microstructure']['log_loss']['value']:.4f}"


@claim("s8_n", "s8")
def _(R):
    return f"{R['s8']['history']['n']:,}"


@claim("s8_events", "s8")
def _(R):
    return f"{R['s8']['history']['events']:,}"


@claim("s8_slope", "s8")
def _(R):
    return ci(R["s8"]["history"]["summary"]["market_reference"]["slope"], 2)


@claim("s8_citl", "s8")
def _(R):
    return ci(R["s8"]["history"]["summary"]["market_reference"]["calibration_in_the_large"], 3)


@claim("s8_score", "s8")
def _(R):
    rows = hypotheses(R, "8")
    return f"{sum(r[1] == 'consistent' for r in rows)} of {len(rows)}"


# ---- stage 9
@claim("s9_net", "s9")
def _(R):
    return usd(R["s9"]["pooled"]["net"])


@claim("s9_fills", "s9")
def _(R):
    return f"{R['s9']['pooled']['all_fills']['fills']:,}"


@claim("s9_events", "s9")
def _(R):
    return str(R["s9"]["pooled"]["all_fills"]["events"])


@claim("s9_markout", "s9")
def _(R):
    return ci(R["s9"]["pooled"]["all_fills"]["markout_30s_cents"], 2)


@claim("s9_settled_fill", "s9")
def _(R):
    return ci(R["s9"]["pooled"]["all_fills"]["settled_pnl_per_fill_usd"], 3)


@claim("s9_spread", "s9")
def _(R):
    return usd(R["s9"]["pooled"]["spread_captured"])


@claim("s9_inventory", "s9")
def _(R):
    return usd(R["s9"]["pooled"]["inventory_contribution"])


@claim("s9_cells", "s9")
def _(R):
    cells = R["s9"]["fill_model_latency_sensitivity"]
    return f"{sum(c['net_usd'] < 0 for c in cells)} of {len(cells)}"


@claim("s9_net_range", "s9")
def _(R):
    v = [c["net_usd"] for c in R["s9"]["fill_model_latency_sensitivity"]]
    return f"{usd(max(v))} to {usd(min(v))}"


@claim("s9_grid_cells", "s9")
def _(R):
    g = R["s9"]["parameter_grid"]
    good = sum(1 for c in g if (c["settled_pnl_per_fill_usd"] or {}).get("lo", -1) > 0)
    return f"{good} of {len(g)}"


def _abl(R, letter, key):
    return next(v for k, v in R["s9"]["ablations"].items() if k[0] == letter)[key]


@claim("s9_skew_inv", "s9")
def _(R):
    return f"{_abl(R, 'D', 'mean_abs_inventory'):.0f} vs {_abl(R, 'B', 'mean_abs_inventory'):.0f}"


@claim("s9_kill_dd", "s9")
def _(R):
    return f"${_abl(R, 'F', 'max_drawdown_usd'):.0f} vs ${_abl(R, 'E', 'max_drawdown_usd'):.0f}"


@claim("s9_score", "s9")
def _(R):
    rows = hypotheses(R, "9")
    return f"{sum(r[1] == 'consistent' for r in rows)} of {len(rows)}"


# ---- stage 10
@claim("s10_stale_skill", "s10")
def _(R):
    s = R["s10"]["blocks"]["validation"]["signal"]["fair_value_staleness"]
    return f"{100 * s['skill_vs_zero']:.1f}% [{100 * s['lo']:.1f}, {100 * s['hi']:.1f}]"


@claim("s10_scale_skill", "s10")
def _(R):
    s = R["s10"]["blocks"]["validation"]["signal"]["scale_of_next_move"]
    return f"{100 * s['skill_vs_mean']:.1f}% [{100 * s['lo']:.1f}, {100 * s['hi']:.1f}]"


@claim("s10_base_net", "s10")
def _(R):
    return usd(_row(R, "s10", "validation")[BASE10]["net_usd"])


@claim("s10_full_net", "s10")
def _(R):
    return usd(_row(R, "s10", "validation")[FULL10]["net_usd"])


@claim("s10_contract_cut", "s10")
def _(R):
    r = _row(R, "s10", "validation")
    return _pct(1 - r[FULL10]["contracts"] / r[BASE10]["contracts"])


@claim("s10_base_pc", "s10")
def _(R):
    r = _row(R, "s10", "validation")[BASE10]["settled_pnl_cents_per_contract"]["value"]
    return f"{r:.1f}¢"


@claim("s10_full_pc", "s10")
def _(R):
    r = _row(R, "s10", "validation")[FULL10]["settled_pnl_cents_per_contract"]["value"]
    return f"{r:.1f}¢"


@claim("s10_pc_diff", "s10")
def _(R):
    return ci(_row(R, "s10", "validation")[FULL10]["vs_baseline"]["settled_cents_per_contract"], 2)


@claim("s10_score_v", "s10")
def _(R):
    rows = hypotheses(R, "10v")
    return f"{sum(r[1] == 'consistent' for r in rows)} of {len(rows)}"


@claim("s10c_score", "s10c")
def _(R):
    rows = hypotheses(R, "10c")
    c = Counter(
        "consistent"
        if r[1] == "consistent"
        else "inconclusive"
        if r[1].startswith("inc")
        else "not"
        for r in rows
    )
    return (
        f"{c['consistent']} consistent, {c['inconclusive']} inconclusive, "
        f"{c['not']} not consistent of {len(rows)}"
    )


@claim("s10c_base_net", "s10c")
def _(R):
    return usd(_row(R, "s10c", "confirmatory")[BASE10]["net_usd"])


@claim("s10c_full_net", "s10c")
def _(R):
    return usd(_row(R, "s10c", "confirmatory")[FULL10]["net_usd"])


@claim("s10c_pc_diff", "s10c")
def _(R):
    return ci(
        _row(R, "s10c", "confirmatory")[FULL10]["vs_baseline"]["settled_cents_per_contract"], 2
    )


@claim("s9_ttr_std", "s9")
def _(R):
    v = [
        b["mean_remaining_std_usd"]
        for b in R["s9"]["inventory_risk_by_time_to_resolution"]
        if b.get("fills", 0) >= 200
    ]
    return f"${min(v):.2f}-${max(v):.2f}"


@claim("s9_ttr_near_markout", "s9")
def _(R):
    b = R["s9"]["inventory_risk_by_time_to_resolution"][0]
    return f"{ci(b['markout_30s_cents'], 2)}¢ ({b['events']} events)"


@claim("s9_sports_share", "s9")
def _(R):
    cat = R["s9"]["breakdowns"]["category"]
    return f"{cat['Sports']['fills']:,} of {R['s9']['pooled']['all_fills']['fills']:,}"


@claim("s9_sports_markout", "s9")
def _(R):
    return ci(R["s9"]["breakdowns"]["category"]["Sports"]["markout_30s_cents"], 2)


@claim("s10_tox_const", "s10tox")
def _(R):
    return f"{R['s10tox']['markout_intercept_cents']:.2f}"


@claim("s10_tox_slope", "s10tox")
def _(R):
    return f"{R['s10tox']['markout_slope_cents_per_cent_of_sigma']:.2f}"


@claim("s10_tox_calm", "s10tox")
def _(R):
    return f"{R['s10tox']['terciles_of_forecast_scale'][0]['mean_markout_30s_cents']:.2f}"


@claim("s10_tox_volatile", "s10tox")
def _(R):
    return f"{R['s10tox']['terciles_of_forecast_scale'][2]['mean_markout_30s_cents']:.2f}"


def _contrast(R, key, label):
    return ci(
        R[key]["blocks"]["validation"]["ladder"]["contrasts"][label]["settled_cents_per_contract"],
        2,
    )


@claim("s10_c21", "s10")
def _(R):
    return _contrast(R, "s10", "2 vs 1")


@claim("s10_c32", "s10")
def _(R):
    return _contrast(R, "s10", "3 vs 2")


@claim("s10_c54", "s10")
def _(R):
    return _contrast(R, "s10", "5 vs 4")


@claim("s10_c65", "s10")
def _(R):
    return _contrast(R, "s10", "6 vs 5")


@claim("s10_c42", "s10")
def _(R):
    return _contrast(R, "s10", "4 vs 2")


@claim("s10_book_val", "s10")
def _(R):
    a = next(
        x for x in R["s10"]["feature_admission"]["h=5s"] if x["features"].startswith("order book")
    )
    v = a["validation"]
    return f"{100 * v['skill_vs_zero']:.2f}% [{100 * v['lo']:.2f}, {100 * v['hi']:.2f}]"


@claim("s10_full_cells", "s10")
def _(R):
    cells = R["s10"]["blocks"]["validation"]["fill_sensitivity"]
    return f"{sum(c['full_net_usd'] < 0 for c in cells)} of {len(cells)}"


@claim("s10_kappa_pc", "s10")
def _(R):
    blk = R["s10"]["blocks"]["validation"]
    by = {float(k): v for k, v in blk["kappa_sensitivity"].items()}
    by[float(R["s10"]["kappa"])] = blk["ladder"]["rows"]
    out = []
    for k in sorted(by):
        v = next(v for n, v in by[k].items() if n.startswith("6"))
        out.append(f"{v['settled_pnl_cents_per_contract']['value']:.1f}")
    return ", ".join(out)


@claim("s10_tox_table", "s10tox")
def _(R):
    rows = R["s10tox"]["terciles_of_forecast_scale"]
    names = ["calm", "middle", "volatile"]
    return "; ".join(
        f"{n} {r['mean_markout_30s_cents']:.2f}¢" for n, r in zip(names, rows, strict=True)
    )


# ---- stage 11
@claim("s11_parity", "s11b", "s11a", "s11r")
def _(R):
    ok = all(
        R[k]["parity_with_backtest_of_the_tape"]["identical"] for k in ("s11b", "s11a", "s11r")
    )
    return "identical" if ok else "NOT identical"


@claim("s11_orders", "s11b", "s11a")
def _(R):
    return f"{R['s11b']['orders'] + R['s11a']['orders']:,}"


@claim("s11_reaction", "s11b")
def _(R):
    r = R["s11b"]["reaction_ms"]
    return f"{r['p50']:.0f} ms median, {r['p99']:.0f} ms p99"


# ---- stage 12
@claim("s12_score", "s12")
def _(R):
    return R["s12"]["headline"]


CLAIMS["pending_note"] = lambda R: PENDING


# ------------------------------------------------------------------ generated tables
def _md(head: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "|".join("---" for _ in head) + "|"]
    return "\n".join(out + ["| " + " | ".join(r) + " |" for r in rows])


TABLES: dict[str, Callable[[dict], str]] = {}


def table(name: str, *needs: str):
    def deco(fn):
        TABLES[name] = _need(*needs)(fn)
        return fn

    return deco


@table("arb_category", "s6")
def _(R):
    rows = []
    for c in R["s6"]["pooled"]["categories"]:
        cf = c["confirmed"]
        rows.append(
            [
                c["category"],
                f"{c['relation_hours']:.0f}",
                str(c["events_observed"]),
                str(cf["episodes"]),
                ci(cf["per_1000_relation_hours"], 1),
            ]
        )
    return _md(["category", "relation-hours", "events", "confirmed episodes", "per 1,000 rh"], rows)


@table("funnel", "s6")
def _(R):
    st = R["s6"]["pooled"]["B_executability"]["confirmed"]["baseline"]["funnel"]["stages"]
    rows = [
        [s["stage"].replace("_", " "), str(s["episodes"]), ci(s["of_displayed"], 2)] for s in st
    ]
    return _md(["stage (confirmed episodes)", "surviving", "share of displayed"], rows)


@table("mm_category", "s9")
def _(R):
    rows = []
    for name, g in R["s9"]["breakdowns"]["category"].items():
        rows.append(
            [
                name,
                str(g["fills"]),
                str(g["events"]),
                ci(g.get("markout_30s_cents"), 2),
                ci(g.get("settled_pnl_per_fill_usd"), 3)
                if g.get("settled_pnl_per_fill_usd")
                else "n/a",
            ]
        )
    return _md(["category", "fills", "events", "30 s markout (¢)", "settled P&L / fill ($)"], rows)


@table("ablation9", "s9")
def _(R):
    rows = []
    for name, v in R["s9"]["ablations"].items():
        rows.append(
            [
                name,
                usd(v["net_usd"]),
                f"{v['contracts']:,.0f}",
                f"{100 * v['net_usd'] / max(v['contracts'], 1):.1f}¢",
                f"{v['mean_abs_inventory']:.0f}",
                f"${v['max_drawdown_usd']:.0f}",
            ]
        )
    return _md(
        ["variant", "net", "contracts", "net / contract", "mean abs inventory", "max drawdown"],
        rows,
    )


@table("ladder10", "s10")
def _(R):
    rows = []
    for name, v in _row(R, "s10", "validation").items():
        vb = v.get("vs_baseline")
        rows.append(
            [
                name,
                usd(v["net_usd"]),
                f"{v['contracts']:,.0f}",
                ci(v["settled_pnl_cents_per_contract"], 1),
                "-" if vb is None else ci(vb["settled_cents_per_contract"], 2),
            ]
        )
    return _md(
        [
            "variant (validation databases)",
            "net",
            "contracts",
            "settled P&L / contract (¢)",
            "vs baseline (¢)",
        ],
        rows,
    )


@table("ladder10c", "s10c")
def _(R):
    rows = []
    for name, v in _row(R, "s10c", "confirmatory").items():
        vb = v.get("vs_baseline")
        rows.append(
            [
                name,
                usd(v["net_usd"]),
                f"{v['contracts']:,.0f}",
                ci(v["settled_pnl_cents_per_contract"], 1),
                "-" if vb is None else ci(vb["settled_cents_per_contract"], 2),
            ]
        )
    return _md(
        [
            "variant (confirmatory recording)",
            "net",
            "contracts",
            "settled P&L / contract (¢)",
            "vs baseline (¢)",
        ],
        rows,
    )


@table("scoreboard")
def _(R):
    rows = []
    for stage, label in (
        ("6", "Stage 6 arbitrage (fresh recordings)"),
        ("8", "Stages 7-8 calibration (sealed holdout)"),
        ("9", "Stage 9 baseline market maker (fresh recordings)"),
        ("10v", "Stage 10 adaptive maker (validation databases; by construction)"),
        ("10c", "Stage 10 adaptive maker (fresh recording)"),
        ("12a", "Final out-of-sample: arbitrage replication"),
        ("12m", "Final out-of-sample: baseline and adaptive maker"),
    ):
        h = hypotheses(R, stage)
        if not h:
            rows.append([label, PENDING, "", ""])
            continue
        ok = sum(r[1] == "consistent" for r in h)
        inc = sum(r[1].startswith("inconclusive") for r in h)
        bad = [r[0] for r in h if r[1] == "NOT consistent"]
        rows.append([label, f"{ok} of {len(h)}", str(inc), ", ".join(bad) or "none"])
    return _md(["experiment", "consistent", "inconclusive", "NOT consistent"], rows)


# ------------------------------------------------------------------ rendering
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_]+)\.([A-Za-z0-9_]+)\s*\}\}")


def resolve(kind: str, key: str, R: dict) -> str:
    if kind == "n":
        if key not in CLAIMS:
            raise KeyError(f"unknown claim {key!r}")
        return CLAIMS[key](R)
    stage = {"h6": "6", "h8": "8", "h9": "9", "h10v": "10v", "h10c": "10c"}.get(kind)
    if stage is not None:
        h = _hyp(R, stage, key)
        return PENDING if h is None else h[2]
    vstage = {"v6": "6", "v8": "8", "v9": "9", "v10v": "10v", "v10c": "10c"}.get(kind)
    if vstage is not None:
        h = _hyp(R, vstage, key)
        return PENDING if h is None else h[1]
    if kind == "t":
        if key not in TABLES:
            raise KeyError(f"unknown table {key!r}")
        return TABLES[key](R)
    if kind == "score":
        rows = hypotheses(R, key)
        if not rows:
            return PENDING
        ok = sum(r[1] == "consistent" for r in rows)
        inc = sum(r[1].startswith("inconclusive") for r in rows)
        return f"{ok} of {len(rows)}" + (f" ({inc} inconclusive)" if inc else "")
    raise KeyError(f"unknown placeholder kind {kind!r}")


def render(template: str, R: dict) -> str:
    return _PLACEHOLDER.sub(lambda m: resolve(m.group(1), m.group(2), R), template)


def placeholders(template: str) -> list[tuple[str, str]]:
    return _PLACEHOLDER.findall(template)
