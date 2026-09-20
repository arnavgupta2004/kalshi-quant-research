"""Stage 8: calibration of the market price and of the Stage 7 models.

    # development: research data only (what the Stage 8 hypotheses are formed from)
    python -m scripts.stage8_calibration --role development --out results/stage8/dev

    # confirmatory: opens the sealed holdout ONCE; models are fitted on research only
    python -m scripts.stage8_calibration --role confirmatory --out results/stage8/confirm \\
        --expect-fingerprint <frozen>

Guards
  * The Stage 7 files must still hash to the fingerprint frozen with hypotheses P1-P7
    (``STAGE7_FROZEN``): if the models changed after those hypotheses were written, the run refuses.
  * ``--expect-fingerprint`` freezes THIS stage's code the same way (as in Stage 6).
  * The holdout is opened only in the confirmatory role, and every opening is recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from pricing import dataset as D
from pricing import evaluation as E
from pricing import probability_models as M
from pricing.plots import reliability_svg
from pricing.scoring import logit
from research import calibration_analysis as C
from research.arbitrage_analysis import est

# Stage 7 as frozen with hypotheses P1-P7 (docs/probability_models.md s.7)
STAGE7_FILES = (
    "pricing/__init__.py",
    "pricing/dataset.py",
    "pricing/evaluation.py",
    "pricing/microstructure.py",
    "pricing/probability_models.py",
    "pricing/report.py",
    "pricing/scoring.py",
    "scripts/stage7_probability_models.py",
)
STAGE7_FROZEN = "f47b450da7120eaa"
STAGE8_PACKAGES = ("pricing/calibration.py", "pricing/plots.py", "research/calibration_analysis.py")

HISTORY_DB = "var/kalshi.duckdb"
CORPUS_DB = "var/relations.duckdb"
RESEARCH_BOOKS = ("var/books_shortlived.duckdb", "var/books_structural.duckdb")
HOLDOUT_BOOKS = ("var/books_research_short.duckdb", "var/books_research_wide.duckdb")
SEED = 20260921
LAMBDA_A = 1e6  # the penalty chosen in every Stage 7 outer fold
KAPPA_B = 30.0
LAMBDA_GRID = (1.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 10_000.0, 1e6)


def _hash(files) -> str:
    h = hashlib.sha256()
    for f in files:
        f = Path(f)
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def stage7_fingerprint() -> str:
    return _hash(STAGE7_FILES)


def code_fingerprint() -> str:
    """This stage's code: the calibration modules, this driver, and the Stage 7 files it builds on."""
    return _hash([*STAGE8_PACKAGES, *STAGE7_FILES, __file__])


# --------------------------------------------------------------------------- data
def volumes(db: str) -> dict[str, float]:
    with Store(db, read_only=True) as s:
        return {
            r[0]: float(r[1])
            for r in s.query("SELECT ticker, volume FROM markets WHERE volume IS NOT NULL")
        }


def book_data(dbs, *, holdout: bool) -> list:
    out = []
    for db in dbs:
        with Store(db, read_only=True) as s:
            start = s.query("SELECT min(recv_ts_ns) FROM book_snapshots")[0][0]
        stats = D.structure_stats(CORPUS_DB, start)
        out += D.book_instances(db, stats=stats, include_holdout=holdout)
    return out


def load(role: str) -> dict:
    sizes = D.event_sizes(CORPUS_DB)
    research_h = D.history_instances(HISTORY_DB, sizes=sizes)
    research_b = book_data(RESEARCH_BOOKS, holdout=False)
    out = {
        "research_history": research_h,
        "research_books": research_b,
        "volume": {
            **volumes(HISTORY_DB),
            **{k: v for db in RESEARCH_BOOKS for k, v in volumes(db).items()},
        },
    }
    if role == "confirmatory":
        both = D.history_instances(HISTORY_DB, sizes=sizes, include_holdout=True)
        out["target_history"] = [i for i in both if i.split == "holdout"]
        out["target_books"] = book_data(HOLDOUT_BOOKS, holdout=True)
        out["volume"].update({k: v for db in HOLDOUT_BOOKS for k, v in volumes(db).items()})
    else:
        out["target_history"], out["target_books"] = research_h, research_b
    return out


# --------------------------------------------------------------------------- models fitted on research only
def fit_models(research_h, research_b) -> dict:
    corpus = D.corpus_rows(CORPUS_DB)
    b = M.HistoricalFrequency(corpus, kappa=KAPPA_B)
    y = np.array([i.out.y for i in research_h], float)
    pb = np.array(M.predict_all(b, research_h))
    a_platt, b_platt = E.fit_platt(np.array([logit(p) for p in pb]), y)
    z_m = np.array([logit(i.obs.ref) for i in research_h])
    z_bp = a_platt + b_platt * np.array([logit(p) for p in pb])  # logit of the recalibrated B
    blend_a, blend_g = E.fit_blend(z_m, z_bp, y)
    model_a = M.MicrostructureLogit(lam=LAMBDA_A, groups=("trade", "time")).fit(research_h)
    lam_book = E.select_penalty(
        lambda lam: M.MicrostructureLogit(lam=lam, groups=("trade", "time", "book")),
        research_b,
        LAMBDA_GRID,
        3,
        SEED,
    )
    model_a_book = M.MicrostructureLogit(lam=lam_book, groups=("trade", "time", "book")).fit(
        research_b
    )
    return {
        "A": model_a,
        "B": b,
        "platt_B": (a_platt, b_platt),
        "blend": (blend_a, blend_g),
        "A_book": model_a_book,
        "params": {
            "A_lambda": LAMBDA_A,
            "B_kappa": KAPPA_B,
            "B_platt_a_b": [a_platt, b_platt],
            "blend_a_g": [blend_a, blend_g],
            "A_book_lambda": lam_book,
            "research_base_rate": float(y.mean()),
        },
    }


def forecasts(models, instances) -> dict[str, list[float]]:
    from pricing.scoring import clip, expit

    a_p, b_p = models["platt_B"]
    ba, bg = models["blend"]
    pb = M.predict_all(models["B"], instances)
    pb_recal = [clip(float(expit(a_p + b_p * logit(p)))) for p in pb]
    ref = M.predict_all(M.ReferencePrice(), instances)
    blend = [
        clip(float(expit(logit(r) + ba + bg * (logit(q) - logit(r)))))
        for r, q in zip(ref, pb_recal, strict=True)
    ]
    return {
        "market_reference": ref,
        "last_trade": M.predict_all(M.LastTrade(), instances),
        "A_microstructure": M.predict_all(models["A"], instances),
        "B_recalibrated": pb_recal,
        "blend_market_and_B": blend,
    }


# --------------------------------------------------------------------------- analyses
def calibration_block(instances, models, volume, research_h, n_boot) -> dict:
    fc = forecasts(models, instances)
    frames = {k: C.Frame.build(k, instances, v) for k, v in fc.items()}
    out: dict = {"n": len(instances), "events": len({i.group for i in instances})}
    out["summary"] = {k: C.summarize(f, n_boot=n_boot, seed=SEED) for k, f in frames.items()}
    ref = frames["market_reference"]
    tab = C.bins_table(ref, n_boot=n_boot // 2, seed=SEED)
    out["reliability_market"] = tab
    dims = C.dimensions(research_h, volume)
    out["subgroups_market"] = {
        name: C.subgroup_table(
            ref, groups, n_boot=n_boot // 2, seed=SEED, slope=(name != "probability_range")
        )
        for name, groups in dims.items()
    }
    out["_fc"] = fc
    return out


def hypotheses_p1_p7(models, fc_target, instances, recal, books_block, research_h) -> list[dict]:
    """The Stage 7 pre-specified hypotheses, evaluated on the target period (docs/probability_models.md s.7)."""
    y = [i.out.y for i in instances]
    const = [models["params"]["research_base_rate"]] * len(instances)
    ref = fc_target["market_reference"]
    pd = lambda a, b: E.paired_difference(instances, a, b, "log", n_boot=1000, seed=SEED)  # noqa: E731
    rows = []

    def add(hid, ok, observed, criterion):
        rows.append(
            {
                "id": hid,
                "verdict": "consistent" if ok else "NOT consistent",
                "observed": observed,
                "criterion": criterion,
            }
        )

    d1 = pd(ref, const)
    add(
        "P1",
        d1.value < -0.2,
        f"market minus constant = {d1.value:.3f}",
        "paired log loss difference < -0.2",
    )
    d2 = pd(fc_target["A_microstructure"], ref)
    add("P2", abs(d2.value) <= 0.005, f"A minus market = {d2.value:+.4f}", "within +-0.005")
    d3 = pd(fc_target["B_recalibrated"], const)
    add(
        "P3",
        d3.value > -0.02,
        f"recalibrated B minus constant = {d3.value:+.4f}",
        "difference > -0.02",
    )
    z_m = np.array([logit(p) for p in ref])
    z_b = np.array([logit(p) for p in fc_target["B_recalibrated"]])
    _, g = E.fit_blend(z_m, z_b, np.array(y, float))
    add("P4", g <= 0.1, f"blend weight on B fitted on this period = {g:+.3f}", "weight <= 0.1")
    bm = books_block.get("book_mid_subset")
    add("P5", bm is not None and bm["microprice_minus_mid"]["value"] >= -0.005,
        "n/a" if bm is None else f"microprice minus mid = {bm['microprice_minus_mid']['value']:+.4f} ({bm['instances']} instances)",
        "difference >= -0.005")  # fmt: skip
    cp = books_block.get("C_partition")
    add("P6", cp is not None and abs(cp["normalised_minus_market"]["value"]) <= 0.01,
        "n/a" if cp is None else f"C minus market = {cp['normalised_minus_market']['value']:+.4f} ({cp['events']} events)",
        "within +-0.01")  # fmt: skip
    slope = fc_target["_slope"]
    gain = -recal["platt"]["log"]["value"]
    add("P7", 0.9 <= slope <= 1.3 and gain < 0.005, f"slope {slope:.2f}; Platt recalibration gain {gain:+.4f}",
        "slope in [0.9, 1.3] and recalibration gain < 0.005")  # fmt: skip
    return rows


def hypotheses_stage8(block: dict, fc: dict, instances, recal: dict, books: dict) -> list[dict]:
    """Calibration hypotheses C1-C10, pre-specified from the development period (docs/calibration.md s.6).
    Criteria are transcribed from that table, not chosen after the holdout was opened."""
    rows: list[dict] = []

    def add(hid, ok, observed, criterion):
        v = "not testable" if ok is None else ("consistent" if ok else "NOT consistent")
        rows.append({"id": hid, "verdict": v, "observed": observed, "criterion": criterion})

    S = block["summary"]
    m = S["market_reference"]
    citl = m["calibration_in_the_large"]
    add("C1", citl["lo"] is not None and citl["lo"] <= 0 <= citl["hi"] and abs(citl["value"]) <= 0.03,
        f"YES minus price = {citl['value']:+.3f} [{citl['lo']:+.3f}, {citl['hi']:+.3f}]",
        "interval contains 0 and |gap| <= 0.03")  # fmt: skip
    mu = m["murphy"]
    add("C2", mu["reliability"] / mu["resolution"] < 0.05,
        f"reliability / resolution = {mu['reliability'] / mu['resolution']:.4f}", "< 0.05")  # fmt: skip
    big = [b for b in block["reliability_market"] if b["n"] >= 100]
    worst = max(big, key=lambda b: abs(b["freq_yes"]["value"] - b["mean_forecast"]))
    gap = worst["freq_yes"]["value"] - worst["mean_forecast"]
    add("C3", abs(gap) <= 0.10, f"largest decile gap {gap:+.3f} (bin {worst['bin']}, n={worst['n']})",
        "|gap| <= 0.10 in every decile with n >= 100")  # fmt: skip
    p = np.array(fc["market_reference"])
    y = np.array([i.out.y for i in instances], float)
    low = p < 0.20
    g_low = float(y[low].mean() - p[low].mean()) if low.any() else float("nan")
    add("C4", g_low < 0, f"YES minus price for prices < 20% = {g_low:+.4f} (n={int(low.sum())})",
        "negative (longshot bias: cheap contracts pay off less than their price)")  # fmt: skip
    ttr = {r["group"]: r for r in block["subgroups_market"]["time_to_resolution"]}
    near = ttr.get("<0.5h", {}).get("slope")
    add("C5", None if not near else 0.8 <= near["value"] <= 1.4,
        "n/a" if not near else f"slope in the last half hour = {near['value']:.2f}", "slope in [0.8, 1.4]")  # fmt: skip
    gain = -recal["isotonic"]["log"]["value"]
    add(
        "C6", gain < 0.005, f"isotonic recalibration gain in log loss = {gain:+.4f}", "gain < 0.005"
    )
    a_s, bl_s = S["A_microstructure"]["slope"], S["blend_market_and_B"]["slope"]
    add("C7", None if a_s["lo"] is None or bl_s["lo"] is None else (a_s["lo"] <= 1 <= a_s["hi"] and bl_s["lo"] <= 1 <= bl_s["hi"]),
        f"A slope {a_s['value']:.2f} [{a_s['lo']}, {a_s['hi']}]; blend slope {bl_s['value']:.2f} [{bl_s['lo']}, {bl_s['hi']}]",
        "both slope intervals contain 1")  # fmt: skip
    bm = books.get("book_calibration_by_source", {}).get("book_mid")
    ab = books.get("paired_vs_market", {}).get("A_trade_time_book")
    add("C8", None if bm is None or ab is None else (abs(bm["calibration_in_the_large"]["value"]) <= 0.05 and abs(ab["value"]) <= 0.01),
        "n/a" if bm is None or ab is None else f"book-mid gap {bm['calibration_in_the_large']['value']:+.3f}; A_book minus market {ab['value']:+.4f}",
        "|book-mid gap| <= 0.05 and |A_book - market| <= 0.01")  # fmt: skip
    sig = tested = 0
    for rs in block["subgroups_market"].values():
        for r in rs:
            g = r.get("calibration_in_the_large")
            if g and g["lo"] is not None:
                tested += 1
                sig += g["hi"] < 0 or g["lo"] > 0
    add("C9", sig <= 3, f"{sig} of {tested} subgroup gaps have an interval excluding 0",
        "<= 3 (about 1 expected by chance at 5%; exploratory, not corrected)")  # fmt: skip
    size = {r["group"]: r for r in block["subgroups_market"]["market_size_lifetime_volume"]}
    low_size = next((r for k, r in size.items() if k.startswith("lifetime: low")), None)
    g = (
        None
        if not low_size or "calibration_in_the_large" not in low_size
        else low_size["calibration_in_the_large"]["value"]
    )
    add("C10", None if g is None else g < 0, "n/a" if g is None else f"lowest lifetime-volume tercile: YES minus price = {g:+.3f}",
        "negative (replication of the development-period sign)")  # fmt: skip
    return rows


def books_block(models, instances, n_boot) -> dict:
    out: dict = {"instances": len(instances), "events": len({i.group for i in instances})}
    if not instances:
        return out
    ref = M.predict_all(M.ReferencePrice(), instances)
    out["reference_source"] = {
        s: sum(1 for i in instances if i.obs.ref_source == s)
        for s in ("book_mid", "trade_mid", "last_trade")
    }
    fc = {
        "market_reference": ref,
        "microprice": M.predict_all(M.Microprice(), instances),
        "A_trade_time_book": M.predict_all(models["A_book"], instances),
    }
    frames = {k: C.Frame.build(k, instances, v) for k, v in fc.items()}
    out["summary"] = {k: C.summarize(f, n_boot=n_boot, seed=SEED) for k, f in frames.items()}
    out["paired_vs_market"] = {
        k: est(E.paired_difference(instances, v, ref, "log", n_boot=n_boot, seed=SEED))
        for k, v in fc.items()
        if k != "market_reference"
    }
    with_book = [i for i in instances if i.obs.book is not None]
    if with_book:
        d = E.paired_difference(
            with_book,
            M.predict_all(M.Microprice(), with_book),
            [i.obs.book.mid for i in with_book],
            "log",
            n_boot=n_boot,
            seed=SEED,
        )
        out["book_mid_subset"] = {
            "instances": len(with_book),
            "events": len({i.group for i in with_book}),
            "microprice_minus_mid": est(d),
        }
    part = [i for i in instances if i.obs.event_refs is not None]
    if part:
        r = M.predict_all(M.ReferencePrice(), part)
        c = M.predict_all(M.PartitionNormalizer(), part)
        out["C_partition"] = {
            "instances": len(part),
            "events": len({i.group for i in part}),
            "mean_sum_of_reference_prices": float(np.mean([sum(i.obs.event_refs) for i in part])),
            "normalised_minus_market": est(
                E.paired_difference(part, c, r, "log", n_boot=n_boot, seed=SEED)
            ),
        }
    out["book_calibration_by_source"] = {}
    for src in ("book_mid", "trade_mid", "last_trade"):
        sub = [i for i in instances if i.obs.ref_source == src]
        if len(sub) >= 30:
            out["book_calibration_by_source"][src] = C.summarize(
                C.Frame.build(src, sub, [i.obs.ref for i in sub]), n_boot=n_boot // 2, seed=SEED
            )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["development", "confirmatory"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--expect-fingerprint")
    args = ap.parse_args()
    if stage7_fingerprint() != STAGE7_FROZEN:
        raise SystemExit(
            f"Stage 7 files changed after hypotheses P1-P7 were frozen: {stage7_fingerprint()} != {STAGE7_FROZEN}"
        )
    fp = code_fingerprint()
    if args.expect_fingerprint and args.expect_fingerprint != fp:
        raise SystemExit(
            f"code fingerprint {fp} != frozen {args.expect_fingerprint}: the analysis changed after it was pre-specified"
        )

    data = load(args.role)
    models = fit_models(data["research_history"], data["research_books"])
    target_h, target_b = data["target_history"], data["target_books"]
    block = calibration_block(
        target_h, models, data["volume"], data["research_history"], args.n_boot
    )
    fc = block.pop("_fc")
    ref = C.Frame.build("market_reference", target_h, fc["market_reference"])
    fc["_slope"] = block["summary"]["market_reference"]["slope"]["value"]

    # recalibration: development = event-grouped cross-fitting inside research; confirmatory = fit on research, apply to holdout
    research_ref = C.Frame.build(
        "research_market",
        data["research_history"],
        M.predict_all(M.ReferencePrice(), data["research_history"]),
    )
    if args.role == "confirmatory":
        recal = C.recalibration_test(research_ref, ref, n_boot=args.n_boot, seed=SEED)
    else:
        recal = C.recalibration_cv(ref, n_boot=args.n_boot, seed=SEED)
    books = books_block(models, target_b, args.n_boot)
    result = {
        "stage": 8,
        "role": args.role,
        "git_commit": git_commit(),
        "code_fingerprint": fp,
        "stage7_fingerprint": stage7_fingerprint(),
        "seed": SEED,
        "n_boot": args.n_boot,
        "frozen_model_parameters": models["params"],
        "history": block,
        "recalibration": recal,
        "books": books,
        "hypotheses_stage7": hypotheses_p1_p7(
            models, fc, target_h, recal, books, data["research_history"]
        ),
        "hypotheses_stage8": hypotheses_stage8(block, fc, target_h, recal, books),
        "holdout_access_log": list(D.ACCESS_LOG),
    }
    if args.role == "development" and D.ACCESS_LOG:
        raise SystemExit(f"the holdout was opened during development: {D.ACCESS_LOG}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "calibration.json").write_text(json.dumps(result, indent=1, default=str))
    svg = reliability_svg(
        {"market price": C.points(block["reliability_market"])},
        "Reliability: market reference price",
        f"{args.role}: {block['n']:,} instances, {block['events']:,} events (95% event-cluster intervals)",
    )
    (out / "reliability_market.svg").write_text(svg)
    print(f"wrote {out / 'calibration.json'}; holdout opened {len(D.ACCESS_LOG)} time(s)")


if __name__ == "__main__":
    main()
