"""Stage 9: the baseline binary-contract market maker, evaluated on recorded order books.

    # development: books_shortlived + books_structural (what the Stage 9 hypotheses are formed from)
    python -m scripts.stage9_baseline_mm --role development --out results/stage9/dev

    # confirmatory: books_research_short + books_research_wide, never run with a market maker
    # before; strategy code and hypotheses frozen
    python -m scripts.stage9_baseline_mm --role confirmatory --out results/stage9/confirm \\
        --expect-fingerprint <frozen>

Everything is paper: the strategy places simulated orders against recorded books and trades.

Guards
  * ``--expect-fingerprint`` freezes the strategy, the evaluation code, the engine it runs on and
    this driver: if any of them changed after the hypotheses were written, the run refuses.
  * each role reads only its own databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from data.storage.provenance import git_commit
from market_making.baseline import BaselineParams
from research import market_making_analysis as A
from research.market_making_hypotheses import evaluate as score_hypotheses

DATABASES = {
    "development": ("var/books_shortlived.duckdb", "var/books_structural.duckdb"),
    "confirmatory": ("var/books_research_short.duckdb", "var/books_research_wide.duckdb"),
}
CODE = (
    "market_making/__init__.py",
    "market_making/quoting.py",
    "market_making/risk.py",
    "market_making/baseline.py",
    "research/market_making_analysis.py",
    "research/market_making_hypotheses.py",
    "backtest/engine.py",
    "backtest/fills.py",
    "backtest/portfolio.py",
    "backtest/metrics.py",
    "backtest/feed.py",
    "backtest/strategies.py",
)
CENTRAL_MAKER = "queue(a=1,c=.5)"
CENTRAL_LATENCY_MS = 200.0
SEED = 20260922


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in (*CODE, __file__):
        h.update(Path(f).name.encode())
        h.update(Path(f).read_bytes())
    return h.hexdigest()[:16]


def _clean(x):
    """Drop the per-fill rows (large, and not JSON) before writing."""
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items() if k not in ("_fills",)}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", choices=list(DATABASES), required=True)
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
    base = BaselineParams()
    datas = [A.load_mm_data(db, Path(db).stem) for db in DATABASES[args.role]]
    central = A.RunConfig(base, CENTRAL_MAKER, CENTRAL_LATENCY_MS)

    # 1. the baseline at the central assumptions
    main_run = A.run_pooled(datas, central, n_boot=nb, seed=SEED)
    per_db = {}
    all_fills = []
    for e in main_run["_evals"]:
        fills = e["_fills"]
        all_fills.extend(fills)
    for d, e in zip(datas, main_run["_evals"], strict=True):
        per_db[d.name] = {k: v for k, v in e.items() if k != "_fills"}
    breakdown = A.breakdowns(all_fills, n_boot=nb, seed=SEED)
    ttr_risk = A.inventory_risk_by_ttr(all_fills, n_boot=nb, seed=SEED)

    # 2. ablations: each design element removed in turn
    ablations = {}
    for name, cfg in A.ablation_configs(base, CENTRAL_MAKER, CENTRAL_LATENCY_MS).items():
        ablations[name] = A._row({}, A.run_pooled(datas, cfg, n_boot=nb, seed=SEED))

    # 3. fill-model x latency, 4. parameters, 5. partition evidence in the risk limits
    sens = A.sensitivity(datas, base, n_boot=min(nb, 300))
    grid = A.parameter_grid(datas, base, CENTRAL_MAKER, CENTRAL_LATENCY_MS, n_boot=min(nb, 300))
    emp = [
        A.load_mm_data(db, Path(db).stem, partition_level=A.EvidenceLevel.EMPIRICAL)
        for db in DATABASES[args.role]
    ]
    partition_sens = A._row({"partition_level": "EMPIRICAL"}, A.run_pooled(emp, central, n_boot=nb))

    result = {
        "stage": 9,
        "role": args.role,
        "git_commit": git_commit(),
        "code_fingerprint": fp,
        "seed": SEED,
        "n_boot": nb,
        "databases": list(DATABASES[args.role]),
        "central": {
            "maker": CENTRAL_MAKER,
            "latency_ms": CENTRAL_LATENCY_MS,
            "params": _params(base),
        },
        "pooled": _clean({k: v for k, v in main_run.items() if k != "_evals"}),
        "per_database": _clean(per_db),
        "breakdowns": _clean(breakdown),
        "inventory_risk_by_time_to_resolution": _clean(ttr_risk),
        "ablations": _clean(ablations),
        "fill_model_latency_sensitivity": _clean(sens),
        "parameter_grid": _clean(grid),
        "partition_evidence_sensitivity": _clean(partition_sens),
    }
    result["hypotheses"] = score_hypotheses(result)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "market_making.json").write_text(json.dumps(result, indent=1, default=str))
    print(f"wrote {out / 'market_making.json'}  fingerprint {fp}")


def _params(p: BaselineParams) -> dict:
    return {
        "gamma": p.gamma,
        "k": p.k,
        "size": p.size,
        "tick": p.tick,
        "max_spread": p.max_spread,
        "limits": vars(p.limits) if hasattr(p.limits, "__dict__") else str(p.limits),
    }


if __name__ == "__main__":
    main()
