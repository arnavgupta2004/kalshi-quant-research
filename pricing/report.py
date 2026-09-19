"""Render ``results/stage7/probability_models.json`` as Markdown (numbers in docs come from here).

python -m pricing.report results/stage7/probability_models.json > results/stage7/report.md
"""

from __future__ import annotations

import json
import sys

from research.report import fe, table


def _ll(x: dict, d: int = 4) -> str:
    return fe(x, 1, d)


def data_table(name: str, d: dict) -> str:
    return table(
        ["dataset", "instances", "markets", "events", "YES rate", "entropy floor (log loss)", "reference source", "categories"],
        [[name, d["instances"], d["markets"], d["events"], f"{d['yes_rate']:.2f}",
          f"{d['entropy_floor_log_loss']:.3f}", d["reference_source"], d["category"]]],
    )  # fmt: skip


def scores(H: dict) -> str:
    rows = []
    for k, v in H["scores"].items():
        diff = H["paired_vs_market"].get(k)
        rows.append(
            [
                k,
                _ll(v["log_loss"]),
                _ll(v["brier"]),
                "-" if diff is None else _ll(diff["log_loss"]),
                "-" if diff is None else _ll(diff["brier"]),
            ]
        )
    return table(
        ["forecast", "log loss", "Brier", "log loss minus market", "Brier minus market"], rows
    )


def model_b(H: dict) -> str:
    rows = []
    for k, v in H["model_b_corpus"].items():
        rows.append(
            [
                k,
                f"{v['corpus_markets']:,}",
                f"{v['corpus_yes_rate']:.2f}",
                v["kappa"],
                f"{v['mean_prediction_on_research']:.2f}",
                f"{v['research_yes_rate']:.2f}",
                _ll(v["log_loss"], 3),
            ]
        )
    return table(
        [
            "training corpus",
            "markets",
            "corpus YES rate",
            "kappa",
            "mean prediction on research",
            "research YES rate",
            "log loss on research",
        ],
        rows,
    )


def by_table(d: dict) -> str:
    if not d:
        return "n/a"
    names = [
        k
        for k in next(iter(d.values()))
        if k not in ("n", "events") and not k.endswith("_minus_market")
    ]
    rows = []
    for k, v in d.items():
        rows.append(
            [k, v["n"], v["events"]]
            + [_ll(v[n], 3) for n in names]
            + [_ll(v[f"{n}_minus_market"], 4) for n in names if n != "market_reference"]
        )
    head = (
        ["group", "n", "events"]
        + names
        + [f"{n} - market" for n in names if n != "market_reference"]
    )
    return table(head, rows)


def signal(H: dict) -> str:
    out = []
    for feat, rows in H["signal_check"].items():
        out.append(
            f"**{feat}** (mean of outcome minus market price; 0 = the price already says it all)\n"
        )
        out.append(
            table(
                ["bin", "n", "events", "mean residual"],
                [[r["bin"], r["n"], r["events"], fe(r["mean_residual"], 1, 3)] for r in rows],
            )
        )
        out.append("")
    return "\n".join(out)


def coefficients(H: dict) -> str:
    c = H["model_a"]["coefficients_standardised_fit_on_all_research"]
    return table(["term", "coefficient (per std. dev.)"], [[k, f"{v:+.4f}"] for k, v in c.items()])


def calibration_preview(H: dict) -> str:
    c = H["market_calibration_preview"]
    rows = [
        [d["bin"], d["n"], f"{d['mean_price']:.3f}", f"{d['freq_yes']:.3f}"] for d in c["deciles"]
    ]
    slopes = ", ".join(f"{b:.3f}" for _, b in c["platt_a_b_per_fold"])
    return (
        table(["price bin", "n", "mean price", "frequency YES"], rows)
        + f"\n\nRecalibration slope on logit(price), per fold: {slopes}. "
        + f"Recalibrated minus market log loss (out of fold): {_ll(c['recalibrated_minus_market']['log_loss'])}."
    )


def books(B: dict) -> str:
    if not B.get("pooled") or not B["pooled"].get("instances"):
        return "no recorded-book instances"
    out = [data_table("recorded books (research)", B["pooled"]), ""]
    rows = []
    for k, v in B["scores"].items():
        diff = B["paired_vs_market"].get(k)
        rows.append([k, _ll(v["log_loss"]), "-" if diff is None else _ll(diff["log_loss"])])
    out.append(table(["forecast", "log loss", "minus market"], rows))
    bm = B.get("book_mid_subset")
    if bm and bm.get("microprice_minus_mid_log_loss"):
        out.append(
            f"\nWhere a two-sided book existed ({bm['instances']} instances, {bm['events']} events): microprice minus mid = {_ll(bm['microprice_minus_mid_log_loss'])}."
        )
    c = B.get("C_partition")
    if c:
        out.append(
            f"\n**Model C** (partition renormalisation): {c['instances']} instances in {c['events']} events; the market's reference prices sum to "
            f"{c['mean_sum_of_reference_prices']:.3f} on average (range {c['range_of_sum'][0]:.2f}-{c['range_of_sum'][1]:.2f}); "
            f"log loss market {_ll(c['market_log_loss'])} vs normalised {_ll(c['normalised_log_loss'])}; paired difference {_ll(c['paired_normalised_minus_market']['log_loss'])}."
        )
    return "\n".join(out)


SECTIONS = {
    "data": lambda r: data_table("history (research)", r["history"]["data"]),
    "model_b": lambda r: model_b(r["history"]),
    "scores": lambda r: scores(r["history"]),
    "horizon": lambda r: by_table(r["history"]["by_horizon"]),
    "source": lambda r: by_table(r["history"]["by_reference_source"]),
    "signal": lambda r: signal(r["history"]),
    "coefficients": lambda r: coefficients(r["history"]),
    "calibration_preview": lambda r: calibration_preview(r["history"]),
    "books": lambda r: books(r["books"]),
}


def render(r: dict, names: list[str] | None = None) -> str:
    names = names or list(SECTIONS)
    return (
        "\n\n".join(
            f"### {n}\n\n{SECTIONS[n](r)}" if names is None else SECTIONS[n](r) for n in names
        )
        + "\n"
    )


if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    only = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[2] == "--only" else None
    print(render(data, only))
