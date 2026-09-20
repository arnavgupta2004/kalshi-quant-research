"""Stage 10: the adaptive market maker, its signal study and its ablation ladder.  Paper only.

    # development: fit on the training databases, judge on the validation databases
    python -m scripts.stage10_adaptive_mm --role development --out results/stage10/dev

    # confirmatory: the fresh recording, opened once, models and code frozen
    python -m scripts.stage10_adaptive_mm --role confirmatory --out results/stage10/confirm \\
        --expect-fingerprint <frozen>

Roles (each database is used for exactly one)
  training      books_research_short, books_research_wide   the short-horizon models are FITTED here
  validation    books_shortlived, books_structural          never seen by the fit; components are
                                                            admitted or rejected on it
  confirmatory  books_stage10                               recorded after the design was fixed

Guards: ``--expect-fingerprint`` freezes the adaptive strategy, the Stage 9 code it extends, the
engine, the analysis and this driver.  Each role reads only its own databases: the development
role never opens the confirmatory one, and the confirmatory role fits nothing on it (the models
are fitted on the training databases in both roles, and are deterministic).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np

from backtest.feed import StoreFeed
from data.storage.duckdb_store import Store
from data.storage.provenance import git_commit
from market_making.baseline import BaselineParams
from market_making.features import DIRECTION_FEATURES, FEATURES
from research import adaptive_analysis as AA
from research import market_making_analysis as A
from research import signal_study as S
from research.adaptive_hypotheses import evaluate as score

TRAIN = ("var/books_research_short.duckdb", "var/books_research_wide.duckdb")
VALID = ("var/books_shortlived.duckdb", "var/books_structural.duckdb")
CONFIRM = ("var/books_stage10.duckdb",)
CODE = (
    "market_making/__init__.py",
    "market_making/quoting.py",
    "market_making/risk.py",
    "market_making/baseline.py",
    "market_making/features.py",
    "market_making/shortterm.py",
    "market_making/adaptive.py",
    "research/market_making_analysis.py",
    "research/signal_study.py",
    "research/adaptive_analysis.py",
    "research/adaptive_hypotheses.py",
    "backtest/engine.py",
    "backtest/fills.py",
    "backtest/portfolio.py",
    "backtest/metrics.py",
    "backtest/feed.py",
)
MAKER, LATENCY_MS = "queue(a=1,c=.5)", 200.0
KAPPA = 1.0  # set from the theory of the widening term (one expected move), not tuned
SEED = 20260923
SENSITIVITY_KAPPAS = (0.5, 2.0, 4.0)


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in (*CODE, __file__):
        h.update(Path(f).name.encode())
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def _samples(db: str) -> S.Samples:
    with Store(db, read_only=True) as s:
        lo = s.query("SELECT min(recv_ts_ns) FROM book_snapshots")[0][0]
        hi = max(
            s.query("SELECT max(recv_ts_ns) FROM book_snapshots")[0][0],
            s.query("SELECT max(recv_ts_ns) FROM poll_log")[0][0] or 0,
        )
        feed = StoreFeed(s, start_ns=lo, end_ns=hi)
        len(feed)
    return S.build_samples(feed, Path(db).stem)


def _clean(x):
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if not k.startswith("_")}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, (np.floating, np.integer)):
        return x.item()
    return x


def _model_json(m: AA.Models) -> dict:
    def r(x):
        return {
            "features": list(x.names),
            "weights": [float(v) for v in x.w],
            "center": list(x.center),
            "scale": list(x.scale),
            "intercept": x.intercept,
            "alpha": x.alpha,
            "horizon_s": x.horizon_s,
            "clip": x.clip,
        }

    return {
        "fair_value_staleness": r(m.fv_stale),
        "fair_value_all_direction": r(m.fv_full),
        "scale": r(m.scale),
        "sigma_ref": m.sigma_ref,
        "training": list(m.training),
    }


def feature_admission(train: S.Samples, valid: S.Samples, h: float) -> list[dict]:
    """The evidence behind admitting a feature group: cross-fitted skill inside the training data and
    skill of a model fitted on ALL of it, on the validation databases."""
    sets = {
        "staleness (stale_dev, stale_out)": ("stale_dev", "stale_out"),
        "order book (obi_top, obi_depth, micro_dev)": ("obi_top", "obi_depth", "micro_dev"),
        "order flow (flow_60)": ("flow_60",),
        "momentum (ret_60)": ("ret_60",),
        "all direction features": DIRECTION_FEATURES,
    }
    out = []
    for name, feats in sets.items():
        a = S.choose_alpha(train, feats, h, AA.ALPHAS)
        sub, pred = S.cv_predictions(train, feats, h, a)
        cv = S.skill_of(sub, pred, h, n_boot=300, seed=SEED)
        va = S.skill(valid, S.fit_ridge(train, feats, h, a), n_boot=300, seed=SEED)
        out.append({"features": name, "alpha": a, "training_cv": cv, "validation": va})
    return out


def block(
    dbs: tuple[str, ...], models: AA.Models, nb: int, *, sensitivity: bool, kappa_grid: bool
) -> dict:
    base = BaselineParams()
    datas = [A.load_mm_data(db, Path(db).stem) for db in dbs]
    samples = S.Samples.concat([_samples(db) for db in dbs])
    lad = AA.ladder(datas, models, base, KAPPA, MAKER, LATENCY_MS, n_boot=nb, seed=SEED)
    fills = lad.pop("_fills")
    full_name = next(n for n in lad["rows"] if "FULL" in n)
    out: dict = {
        "databases": list(dbs),
        "signal": AA.signal_replication(models, samples, n_boot=nb, seed=SEED),
        "ladder": lad,
        "breakdown_full": A.breakdowns(fills[full_name], n_boot=nb, seed=SEED),
        "breakdown_baseline": A.breakdowns(
            fills[next(n for n in lad["rows"] if n[0] == "0")], n_boot=nb, seed=SEED
        ),
        "inventory_risk_by_ttr_full": A.inventory_risk_by_ttr(
            fills[full_name], n_boot=nb, seed=SEED
        ),
    }
    if kappa_grid:
        out["kappa_sensitivity"] = {}
        for k in SENSITIVITY_KAPPAS:
            keep = {n for n in lad["rows"] if n[0] in "0456"}
            sub = AA.ladder(
                datas, models, base, k, MAKER, LATENCY_MS, n_boot=min(nb, 300), seed=SEED, only=keep
            )
            sub.pop("_fills")
            out["kappa_sensitivity"][str(k)] = sub["rows"]
    out["fill_sensitivity"] = []
    if sensitivity:
        full_params = AA.variants(models, base, KAPPA)[full_name]
        for lat in (200.0, 1000.0):
            for m in A.MAKER_MODELS:
                nb_, nf_, fb_, ff_ = 0.0, 0.0, 0, 0
                for d in datas:
                    eb = AA.run_variant(d, None, base, m, lat)
                    ef = AA.run_variant(d, full_params, base, m, lat)
                    nb_ += eb.res.pnl_micro / A.MICRO
                    nf_ += ef.res.pnl_micro / A.MICRO
                    fb_ += len(eb.res.fills)
                    ff_ += len(ef.res.fills)
                out["fill_sensitivity"].append(
                    {
                        "latency_ms": lat,
                        "maker": m,
                        "baseline_net_usd": nb_,
                        "full_net_usd": nf_,
                        "baseline_fills": fb_,
                        "full_fills": ff_,
                    }
                )
    else:
        out["fill_sensitivity"] = [
            {
                "latency_ms": LATENCY_MS,
                "maker": MAKER,
                "full_net_usd": lad["rows"][full_name]["net_usd"],
            }
        ]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=["development", "confirmatory"], required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--expect-fingerprint")
    args = ap.parse_args()
    fp = code_fingerprint()
    if args.expect_fingerprint and args.expect_fingerprint != fp:
        raise SystemExit(
            f"code fingerprint {fp} != frozen {args.expect_fingerprint}: "
            "the analysis changed after it was pre-specified"
        )
    nb = args.n_boot
    train = S.Samples.concat([_samples(db) for db in TRAIN])
    models = AA.fit_models(train, tuple(Path(d).stem for d in TRAIN))
    result: dict = {
        "stage": 10,
        "role": args.role,
        "git_commit": git_commit(),
        "code_fingerprint": fp,
        "seed": SEED,
        "n_boot": nb,
        "kappa": KAPPA,
        "central": {"maker": MAKER, "latency_ms": LATENCY_MS},
        "models": _model_json(models),
        "features": list(FEATURES),
        "blocks": {},
    }
    if args.role == "development":
        valid = S.Samples.concat([_samples(db) for db in VALID])
        result["feature_admission"] = {
            f"h={h:g}s": feature_admission(train, valid, h) for h in (AA.FV_HORIZON_S,)
        }
        result["blocks"]["validation"] = block(VALID, models, nb, sensitivity=True, kappa_grid=True)
        result["blocks"]["training_in_sample"] = block(
            TRAIN, models, nb, sensitivity=False, kappa_grid=False
        )
        result["hypotheses_on_validation"] = score(result["blocks"]["validation"])
    else:
        result["blocks"]["confirmatory"] = block(
            CONFIRM, models, nb, sensitivity=True, kappa_grid=True
        )
        result["hypotheses"] = score(result["blocks"]["confirmatory"])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "adaptive.json").write_text(json.dumps(_clean(result), indent=1, default=str))
    print(f"wrote {out / 'adaptive.json'}  fingerprint {fp}")


if __name__ == "__main__":
    main()
