"""Render ``results/stage8/*/calibration.json`` as Markdown (numbers in docs come from here).

python -m research.calibration_report results/stage8/dev/calibration.json [--only a,b]
"""

from __future__ import annotations

import json
import sys

from research.report import fe, table


def _e(x: dict | None, d: int = 3, scale: float = 1.0) -> str:
    return fe(x, scale, d)


def summary(r: dict) -> str:
    rows = []
    for name, s in r["history"]["summary"].items():
        m = s["murphy"]
        rows.append([name, s["n"], s["events"], f"{s['base_rate']:.3f}", f"{s['mean_forecast']:.3f}",
                     _e(s["calibration_in_the_large"]), _e(s["intercept"]), _e(s["slope"]), _e(s["ece"]),
                     f"{s['brier']:.4f}", f"{s['log_loss']:.4f}",
                     f"{m['reliability']:.4f}", f"{m['resolution']:.4f}", f"{m['uncertainty']:.4f}"])  # fmt: skip
    return table(["forecast", "n", "events", "YES rate", "mean forecast", "calibration in the large (YES - forecast)",
                  "intercept", "slope", "ECE", "Brier", "log loss", "reliability", "resolution", "uncertainty"], rows)  # fmt: skip


def reliability(r: dict) -> str:
    rows = [[b["bin"], b["n"], b["events"], f"{b['mean_forecast']:.3f}", _e(b["freq_yes"]),
             f"{b['freq_yes']['value'] - b['mean_forecast']:+.3f}"] for b in r["history"]["reliability_market"]]  # fmt: skip
    return table(
        ["forecast bin", "n", "events", "mean forecast", "frequency YES", "gap (freq - forecast)"],
        rows,
    )


def subgroups(r: dict) -> str:
    out = []
    for dim, rows in r["history"]["subgroups_market"].items():
        body = []
        for x in rows:
            if "note" in x:
                body.append([x["group"], x["n"], x["events"], "-", "-", "-", x["note"]])
                continue
            body.append([x["group"], x["n"], x["events"], f"{x['mean_forecast']:.3f}", f"{x['freq_yes']:.3f}",
                         _e(x["calibration_in_the_large"]), _e(x["slope"]) if x["slope"] else "(< 40 events)"])  # fmt: skip
        out.append(f"**{dim.replace('_', ' ')}**\n")
        out.append(
            table(
                ["group", "n", "events", "mean price", "freq YES", "gap (YES - price)", "slope"],
                body,
            )
        )
        out.append("")
    return "\n".join(out)


def recalibration(r: dict) -> str:
    rows = []
    for name, x in r["recalibration"].items():
        rows.append([name, _e(x["log"], 4), _e(x["brier"], 4), ", ".join(f"({a:+.2f}, {b:.2f})" for a, b in x["fitted_a_b_per_fold"]) if "fitted_a_b_per_fold" in x else (f"a={x['fitted_intercept']:+.3f}, b={x['fitted_slope']:.3f}" if "fitted_slope" in x else "")])  # fmt: skip
    return table(
        ["recalibrator", "log loss minus raw price", "Brier minus raw price", "fitted (a, b)"], rows
    )


def books(r: dict) -> str:
    b = r["books"]
    if not b.get("instances"):
        return "no recorded-book instances"
    out = [
        f"{b['instances']} instances in {b['events']} events; reference source {b['reference_source']}\n"
    ]
    rows = []
    for k, s in b["summary"].items():
        diff = b["paired_vs_market"].get(k)
        rows.append(
            [
                k,
                f"{s['log_loss']:.4f}",
                "-" if diff is None else _e(diff, 4),
                _e(s["slope"]),
                _e(s["calibration_in_the_large"]),
            ]
        )
    out.append(
        table(["forecast", "log loss", "minus market", "slope", "calibration in the large"], rows)
    )
    if b.get("book_mid_subset"):
        m = b["book_mid_subset"]
        out.append(
            f"\nWhere a two-sided book existed ({m['instances']} instances, {m['events']} events): microprice minus mid = {_e(m['microprice_minus_mid'], 4)}."
        )
    if b.get("C_partition"):
        c = b["C_partition"]
        out.append(
            f"\nPartition renormalisation: {c['instances']} instances, {c['events']} events, prices sum to {c['mean_sum_of_reference_prices']:.3f} on average; normalised minus market = {_e(c['normalised_minus_market'], 4)}."
        )
    if b.get("book_calibration_by_source"):
        rows = [
            [
                k,
                s["n"],
                s["events"],
                f"{s['mean_forecast']:.3f}",
                f"{s['base_rate']:.3f}",
                _e(s["slope"]),
                _e(s["calibration_in_the_large"]),
            ]
            for k, s in b["book_calibration_by_source"].items()
        ]
        out.append("\nCalibration of the reference price by where it came from:\n")
        out.append(
            table(
                ["source", "n", "events", "mean price", "YES rate", "slope", "gap (YES - price)"],
                rows,
            )
        )
    return "\n".join(out)


def _hyp(h: list[dict]) -> str:
    n = sum(1 for x in h if x["verdict"] == "consistent")
    rows = [[x["id"], x["verdict"], x["observed"], x["criterion"]] for x in h]
    return f"{n} of {len(h)} consistent.\n\n" + table(
        ["#", "verdict", "observed", "criterion"], rows
    )


def hypotheses(r: dict) -> str:
    return (
        "**Stage 7 hypotheses P1-P7**\n\n"
        + _hyp(r["hypotheses_stage7"])
        + "\n\n**Stage 8 hypotheses C1-C10**\n\n"
        + _hyp(r["hypotheses_stage8"])
    )


SECTIONS = {
    "summary": summary,
    "reliability": reliability,
    "subgroups": subgroups,
    "recalibration": recalibration,
    "books": books,
    "hypotheses": hypotheses,
}


def render(r: dict, names=None) -> str:
    return "\n\n".join(SECTIONS[n](r) for n in (names or SECTIONS)) + "\n"


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    only = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[2] == "--only" else None
    print(render(data, only))
