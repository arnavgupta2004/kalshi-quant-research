"""Stage 7: fair-probability models, developed on the RESEARCH period only.

    python -m scripts.stage7_probability_models --out results/stage7

Three approaches (``pricing/probability_models.py``):

    A  microstructure   logit p = logit(market reference) + correction from tape / book / time
    B  historical rates hierarchical empirical-Bayes base rate; never sees a price
    C  partition        renormalise mutually exclusive AND exhaustive outcomes to sum to 1

What this stage answers: are they *usable* (point-in-time, bounded, no leakage) and *is there any sign*
they carry information the market price lacks?  It deliberately does NOT do the calibration study
(reliability diagrams, calibration by category / liquidity / time to resolution): that is Stage 8, on
the sealed holdout, which this script never opens (asserted at the end: ``ACCESS_LOG`` is empty).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

import numpy as np

from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from pricing import dataset as D
from pricing import evaluation as E
from pricing import probability_models as M
from pricing.scoring import base_rate_log_loss
from research.arbitrage_analysis import est
from research.stats import cluster_bootstrap_mean

HISTORY_DB = "var/kalshi.duckdb"
CORPUS_DB = "var/relations.duckdb"
BOOK_DBS = ("var/books_shortlived.duckdb", "var/books_structural.duckdb")
LAMBDA_GRID = (1.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 10_000.0, 1e6)
SEED = 20260920


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in sorted(Path("pricing").glob("*.py")) + [Path(__file__)]:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def describe(instances) -> dict:
    return {
        "instances": len(instances),
        "markets": len({i.obs.ticker for i in instances}),
        "events": len({i.group for i in instances}),
        "yes_rate": float(np.mean([i.out.y for i in instances])),
        "entropy_floor_log_loss": base_rate_log_loss([i.out.y for i in instances]),
        "reference_source": dict(Counter(i.obs.ref_source for i in instances)),
        "category": dict(Counter(i.obs.category for i in instances).most_common(8)),
        "hours_to_expiry": {
            str(h): sum(
                1
                for i in instances
                if i.obs.hours_to_expiry is not None and round(i.obs.hours_to_expiry * 4) / 4 == h
            )
            for h in (24.0, 6.0, 2.0, 1.0, 0.25)
        },
    }


def loss_row(instances, preds, n_boot, seed) -> dict:
    return {
        "log_loss": est(E.mean_loss(instances, preds, "log", n_boot=n_boot, seed=seed)),
        "brier": est(E.mean_loss(instances, preds, "brier", n_boot=n_boot, seed=seed)),
    }


def diff_row(instances, a, b, n_boot, seed) -> dict:
    return {
        "log_loss": est(E.paired_difference(instances, a, b, "log", n_boot=n_boot, seed=seed)),
        "brier": est(E.paired_difference(instances, a, b, "brier", n_boot=n_boot, seed=seed)),
    }


def by(instances, preds: dict, key, keys, n_boot, seed) -> dict:
    out = {}
    for k in keys:
        idx = [j for j, i in enumerate(instances) if key(i) == k]
        if len(idx) < 30:
            continue
        sub = [instances[j] for j in idx]
        ref = [preds["market_reference"][j] for j in idx]
        out[str(k)] = {
            "n": len(sub),
            "events": len({i.group for i in sub}),
            **{
                name: loss_row(sub, [p[j] for j in idx], n_boot, seed)["log_loss"]
                for name, p in preds.items()
            },
            **{
                f"{name}_minus_market": est(
                    E.paired_difference(
                        sub, [p[j] for j in idx], ref, "log", n_boot=n_boot, seed=seed
                    )
                )
                for name, p in preds.items()
                if name != "market_reference"
            },
        }
    return out


def signal_check(instances, feature, edges, n_boot, seed) -> list[dict]:
    """Mean residual (outcome - market price) by bins of one feature.  If a feature carried information
    the price lacks, the residual would move with it; a flat residual is the null."""
    rows = []
    for lo, hi in zip(edges, edges[1:], strict=False):
        sub = [i for i in instances if (v := feature(i)) is not None and lo <= v < hi]
        if len(sub) < 30:
            continue
        r = cluster_bootstrap_mean(
            [(i, i.out.y - i.obs.ref) for i in sub],
            lambda x: x[0].group,
            lambda x: x[1],
            n_boot=n_boot,
            seed=seed,
        )
        rows.append(
            {
                "bin": f"[{lo:g}, {hi:g})",
                "n": len(sub),
                "events": len({i.group for i in sub}),
                "mean_residual": est(r),
            }
        )
    return rows


def history_study(n_boot: int) -> dict:
    sizes = D.event_sizes(CORPUS_DB)
    H = D.history_instances(HISTORY_DB, sizes=sizes)
    y = [i.out.y for i in H]
    out: dict = {"data": describe(H)}

    # ---- Model B: corpus, shrinkage, population shift
    b_rows = {}
    for label, mv in (("all_markets", None), ("volume_matched", 10_000)):
        corpus = D.corpus_rows(CORPUS_DB, min_volume=mv)
        kap = M.prequential_kappa(corpus)
        kappa = min(kap, key=kap.get)
        p = M.predict_all(M.HistoricalFrequency(corpus, kappa=kappa), H)
        b_rows[label] = {
            "corpus_markets": len(corpus),
            "corpus_yes_rate": float(np.mean([r.y for r in corpus])),
            "prequential_log_loss_by_kappa": kap,
            "kappa": kappa,
            "mean_prediction_on_research": float(np.mean(p)),
            "research_yes_rate": float(np.mean(y)),
            "log_loss": loss_row(H, p, n_boot, SEED)["log_loss"],
        }
    out["model_b_corpus"] = b_rows
    corpus = D.corpus_rows(CORPUS_DB)
    kappa = b_rows["volume_matched"]["kappa"]

    # ---- predictions
    ref = M.predict_all(M.ReferencePrice(), H)
    p_b = M.predict_all(M.HistoricalFrequency(corpus, kappa=kappa), H)
    p_b_recal, platt = E.platt_cv_predict(H, p_b, seed=SEED)
    p_a, chosen = E.nested_cv_predict(
        lambda lam: M.MicrostructureLogit(lam=lam, groups=("trade", "time")),
        H,
        LAMBDA_GRID,
        seed=SEED,
    )
    blend_b, g = E.blend_cv_predict(H, p_b_recal, seed=SEED)
    preds = {
        "constant_base_rate": [float(np.mean(y))] * len(H),
        "market_reference": ref,
        "last_trade": M.predict_all(M.LastTrade(), H),
        "B_historical_frequency": p_b,
        "B_recalibrated": p_b_recal,
        "A_microstructure": p_a,
        "blend_market_and_B": blend_b,
    }
    out["scores"] = {k: loss_row(H, v, n_boot, SEED) for k, v in preds.items()}
    out["scores"]["constant_base_rate"]["note"] = "in-sample frequency: an optimistic floor"
    out["paired_vs_market"] = {
        k: diff_row(H, v, ref, n_boot, SEED) for k, v in preds.items() if k != "market_reference"
    }
    out["model_a"] = {
        "penalty_grid": list(LAMBDA_GRID),
        "penalty_chosen_per_outer_fold": chosen,
        "platt_a_b_per_fold_for_B": platt,
        "blend_weight_on_B_per_fold": g,
    }
    full = M.MicrostructureLogit(lam=float(np.median(chosen)), groups=("trade", "time")).fit(H)
    out["model_a"]["coefficients_standardised_fit_on_all_research"] = full.coefficients()
    out["by_horizon"] = by(
        H, {k: preds[k] for k in ("market_reference", "A_microstructure", "B_recalibrated")},
        lambda i: round(i.obs.hours_to_expiry * 4) / 4 if i.obs.hours_to_expiry is not None else None,
        (24.0, 6.0, 2.0, 1.0, 0.25), n_boot, SEED,
    )  # fmt: skip
    out["by_reference_source"] = by(
        H, {k: preds[k] for k in ("market_reference", "A_microstructure", "B_recalibrated")},
        lambda i: i.obs.ref_source, ("last_trade", "trade_mid"), n_boot, SEED,
    )  # fmt: skip
    ref_recal, ref_platt = E.platt_cv_predict(H, ref, seed=SEED)
    p_arr, y_arr = np.array(ref), np.array(y)
    out["market_calibration_preview"] = {
        "note": "research period only; the calibration study proper is Stage 8 on the sealed holdout",
        "platt_a_b_per_fold": ref_platt,
        "recalibrated_minus_market": diff_row(H, ref_recal, ref, n_boot, SEED),
        "deciles": [
            {
                "bin": f"[{lo / 10:.1f}, {(lo + 1) / 10:.1f})",
                "n": int(m.sum()),
                "mean_price": float(p_arr[m].mean()),
                "freq_yes": float(y_arr[m].mean()),
            }
            for lo in range(10)
            if (m := (p_arr >= lo / 10) & (p_arr < (lo + 1) / 10 + (1e-9 if lo == 9 else 0))).sum()
            >= 20
        ],
    }
    out["signal_check"] = {
        "flow_1h": signal_check(
            H, lambda i: i.obs.trade.flow[3600], (-1.01, -0.5, -0.01, 0.01, 0.5, 1.01), n_boot, SEED
        ),
        "ret_1h": signal_check(
            H,
            lambda i: i.obs.trade.ret[3600],
            (-1.0, -0.05, -0.001, 0.001, 0.05, 1.0),
            n_boot,
            SEED,
        ),
    }
    return out


def book_study(n_boot: int) -> dict:
    out: dict = {"datasets": {}}
    instances = []
    for db in BOOK_DBS:
        with Store(db, read_only=True) as s:
            start = s.query("SELECT min(recv_ts_ns) FROM book_snapshots")[0][0]
        stats = D.structure_stats(CORPUS_DB, start)
        got = D.book_instances(db, stats=stats)
        out["datasets"][Path(db).stem] = describe(got) if got else {"instances": 0}
        instances += got
    out["pooled"] = describe(instances)
    if not instances:
        return out
    ref = M.predict_all(M.ReferencePrice(), instances)
    p_micro = M.predict_all(M.Microprice(), instances)
    p_a, chosen = E.nested_cv_predict(
        lambda lam: M.MicrostructureLogit(lam=lam, groups=("trade", "time", "book")),
        instances, LAMBDA_GRID, seed=SEED,
    )  # fmt: skip
    preds = {"market_reference": ref, "microprice": p_micro, "A_trade_time_book": p_a}
    out["scores"] = {k: loss_row(instances, v, n_boot, SEED) for k, v in preds.items()}
    out["paired_vs_market"] = {
        k: diff_row(instances, v, ref, n_boot, SEED)
        for k, v in preds.items()
        if k != "market_reference"
    }
    out["penalty_chosen_per_outer_fold"] = chosen
    with_book = [i for i in instances if i.obs.book is not None]
    out["book_mid_subset"] = {
        "instances": len(with_book),
        "events": len({i.group for i in with_book}),
        "microprice_minus_mid_log_loss": est(
            E.paired_difference(
                with_book, M.predict_all(M.Microprice(), with_book), [i.obs.book.mid for i in with_book], "log",
                n_boot=n_boot, seed=SEED,
            )
        ) if with_book else None,
    }  # fmt: skip
    part = [i for i in instances if i.obs.event_refs is not None]
    if part:
        c = M.predict_all(M.PartitionNormalizer(), part)
        r = M.predict_all(M.ReferencePrice(), part)
        out["C_partition"] = {
            "instances": len(part),
            "events": len({i.group for i in part}),
            "mean_sum_of_reference_prices": float(np.mean([sum(i.obs.event_refs) for i in part])),
            "range_of_sum": [
                float(min(sum(i.obs.event_refs) for i in part)),
                float(max(sum(i.obs.event_refs) for i in part)),
            ],
            "market_log_loss": loss_row(part, r, n_boot, SEED)["log_loss"],
            "normalised_log_loss": loss_row(part, c, n_boot, SEED)["log_loss"],
            "paired_normalised_minus_market": diff_row(part, c, r, n_boot, SEED),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-boot", type=int, default=2000)
    args = ap.parse_args()
    result = {
        "stage": 7,
        "git_commit": git_commit(),
        "code_fingerprint": code_fingerprint(),
        "seed": SEED,
        "n_boot": args.n_boot,
        "split": {
            "history_research": f"markets closing before {D.HISTORY_HOLDOUT_FROM:%Y-%m-%d}",
            "book_research": list(D.RESEARCH_BOOK_DBS),
            "sealed_holdout": {
                "history": f"closing on/after {D.HISTORY_HOLDOUT_FROM:%Y-%m-%d}",
                "books": list(D.HOLDOUT_BOOK_DBS),
            },
        },
        "history": history_study(args.n_boot),
        "books": book_study(args.n_boot),
    }
    result["holdout_access_log"] = list(D.ACCESS_LOG)
    if D.ACCESS_LOG:
        raise SystemExit(f"the sealed holdout was opened during Stage 7: {D.ACCESS_LOG}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "probability_models.json").write_text(json.dumps(result, indent=1, default=str))
    print(f"wrote {out / 'probability_models.json'}  (holdout untouched)")


if __name__ == "__main__":
    main()
