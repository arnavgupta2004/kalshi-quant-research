"""Stage 6: arbitrage research (Experiments A-D and the category study).

    # development: the two datasets the analysis code was written against
    python -m scripts.stage6_arbitrage_research --role development \\
        --db var/books_structural.duckdb --db var/books_shortlived.duckdb --out results/stage6/dev

    # confirmatory: fresh recordings, analysed once with the code frozen (see docs/arbitrage_research.md)
    python -m scripts.stage6_arbitrage_research --role confirmatory \\
        --db var/books_research_wide.duckdb --db var/books_research_short.duckdb \\
        --out results/stage6/confirm

Every result carries its provenance (git commit, code fingerprint, dataset fingerprints, seed, window)
and its role, so a number can always be traced to the data it came from and whether that data had
already been looked at while developing the method.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from data.storage.provenance import git_commit
from market.semantics import EvidenceLevel
from research import arbitrage_analysis as A
from research import latency_analysis as L
from research.dataset import load_dataset
from research.scanner import merge_results

VARIANTS = ("baseline", "fees_half", "fee_free")
ENGINE_LATENCIES_MS = (0, 5, 25, 100, 250, 1000, 3000, 10_000)


FROZEN_PACKAGES = ("research", "arbitrage", "backtest", "market")
NOT_ANALYSIS = {"report.py"}  # presentation only: changing how a table is printed changes no number


def code_fingerprint() -> str:
    """Hash of every module a number depends on (analysis, detector, engine, market model) and of
    this driver.  The confirmatory run must present the fingerprint that was frozen beforehand."""
    h = hashlib.sha256()
    files = [
        f
        for pkg in FROZEN_PACKAGES
        for f in sorted(Path(pkg).glob("*.py"))
        if f.name not in NOT_ANALYSIS
    ]
    for f in [*files, Path(__file__)]:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()[:16]


def analyse(res, p: A.Params, label: str) -> dict:
    out: dict = {"label": label, "exposure": A.exposure_summary(res, p)}
    sight = A.select(res, p, confirmed=False)
    conf = A.select(res, p, confirmed=True)
    out["counts"] = {
        "sightings_all_levels": len(res.episodes),
        "sightings_at_or_above_min_level": len(sight),
        "confirmed_at_or_above_min_level": len(conf),
    }
    out["diagnostic"] = A.sighting_lifetime_diagnostic(sight)
    out["A_frequency"] = {
        "sightings": A.frequency(res, p, confirmed=False),
        "confirmed": A.frequency(res, p, confirmed=True),
        "by_kind_sightings": A.frequency_by_kind(res, p, confirmed=False),
        "by_kind_confirmed": A.frequency_by_kind(res, p, confirmed=True),
    }
    out["B_executability"] = {
        pop: {
            v: {
                "funnel": A.funnel(eps, v, p),
                "edge": A.net_edge_distribution(eps, v, p),
                "by_covariate": A.executability_by_covariate(eps, v, p),
            }
            for v in VARIANTS
        }
        for pop, eps in (("sightings", sight), ("confirmed", conf))
    }
    out["C_lifetime"] = {
        pop: {
            "lifetime": A.lifetime_analysis(eps, p),
            "end_causes": A.end_causes(eps, p),
            "edge_association": A.edge_lifetime_association(eps, p),
        }
        for pop, eps in (("sightings", sight), ("confirmed", conf))
    }
    out["D_latency"] = {}
    for pop, eps in (("sightings", sight), ("confirmed", conf)):
        lam = A.lifetime_analysis(eps, p).get("exponential")
        out["D_latency"][pop] = {
            "survival": L.survival_table(eps, p),
            "latency_funnel": {v: L.latency_funnel(eps, v, p) for v in VARIANTS},
            "execution": {v: L.execution_table(eps, v, p) for v in VARIANTS},
            "survival_at_3s_by_edge": L.survival_by_covariate(eps, 3, A.edge_bins(), p),
            "survival_at_3s_by_liquidity": L.survival_by_covariate(eps, 3, A.liquidity_bins(), p),
            "exponential_model": L.extrapolated_survival(lam["rate_per_s"] if lam else None),
        }
    out["categories"] = A.category_table(res, p)
    out["evidence_sensitivity"] = evidence_sensitivity(res, p)
    return out


def evidence_sensitivity(res, p: A.Params) -> list[dict]:
    """The headline counts again at each evidence threshold: how much rests on trusting weaker claims."""
    rows = []
    for level in ("PROVEN", "DECLARED", "UNVERIFIED"):
        q = A.Params(n_boot=p.n_boot, seed=p.seed, min_level=level, alpha=p.alpha)
        sight, conf = A.select(res, q, confirmed=False), A.select(res, q, confirmed=True)
        rows.append(
            {
                "min_level": level,
                "sightings": len(sight),
                "confirmed": len(conf),
                "rate_sightings": A.frequency(res, q, confirmed=False)["per_1000_relation_hours"],
                "rate_confirmed": A.frequency(res, q, confirmed=True)["per_1000_relation_hours"],
                "executable_baseline": [
                    sum(1 for e in sight if e.best_stage["baseline"] >= 4),
                    sum(1 for e in conf if e.best_stage["baseline"] >= 4),
                ],
                "executable_fee_free": [
                    sum(1 for e in sight if e.best_stage["fee_free"] >= 4),
                    sum(1 for e in conf if e.best_stage["fee_free"] >= 4),
                ],
            }
        )
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--role", choices=["development", "confirmatory"], required=True)
    ap.add_argument("--min-level", default="DECLARED")
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument(
        "--expect-fingerprint", help="refuse to run if the code has changed since it was frozen"
    )
    ap.add_argument(
        "--engine-sweep", action="store_true", help="run the Stage 5 engine latency sweep"
    )
    args = ap.parse_args()

    fp = code_fingerprint()
    if args.expect_fingerprint and args.expect_fingerprint != fp:
        raise SystemExit(
            f"code fingerprint {fp} != frozen {args.expect_fingerprint}: the analysis changed after it "
            "was pre-specified.  Re-freeze and re-run the development analysis, or revert."
        )
    p = A.Params(n_boot=args.n_boot, min_level=args.min_level)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    scans, datasets, meta = [], [], []
    for db in args.db:
        ds = load_dataset(db)
        res = ds.scan()
        label = Path(db).stem
        scans.append((label, res))
        datasets.append((label, ds))
        meta.append(
            {
                "db": db,
                "label": label,
                "fingerprint": ds.fingerprint,
                "window_ns": list(ds.window_ns),
                "recording_minutes": res.span_s / 60,
                "relations": len(ds.specs),
                "markets": len(ds.categories),
                "settled_markets": len(ds.settled),
                "episodes": len(res.episodes),
            }
        )
        print(f"{label}: {res.span_s / 60:.1f} min, {len(res.episodes)} episodes")

    pooled = merge_results(scans)
    result = {
        "stage": 6,
        "role": args.role,
        "git_commit": git_commit(),
        "code_fingerprint": fp,
        "params": {
            "n_boot": p.n_boot,
            "seed": p.seed,
            "min_level": p.min_level,
            "alpha": p.alpha,
            "variants": list(VARIANTS),
        },
        "datasets": meta,
        "pooled": analyse(pooled, p, "pooled"),
        "per_dataset": {label: analyse(r, p, label) for label, r in scans},
    }
    if args.engine_sweep:
        result["engine_sweep"] = {}
        for label, ds in datasets:
            if len(ds.settled) < 20:
                continue
            result["engine_sweep"][label] = {
                "declared_baseline_fees": L.engine_latency_sweep(
                    ds, ENGINE_LATENCIES_MS, min_level=EvidenceLevel.DECLARED
                ),
                "declared_fee_free": L.engine_latency_sweep(
                    ds, ENGINE_LATENCIES_MS, min_level=EvidenceLevel.DECLARED, fee_scale=0
                ),
                "any_evidence_fee_free": L.engine_latency_sweep(
                    ds, ENGINE_LATENCIES_MS, min_level=EvidenceLevel.UNVERIFIED, fee_scale=0
                ),
            }
    # realised-vs-promised needs outcomes, which live in the datasets, not in the scan
    result["realised_vs_promised"] = {}
    for label, res in scans:
        ds = dict(datasets)[label]
        result["realised_vs_promised"][label] = {
            v: A.realised_check(res.episodes, ds.settled, v) for v in VARIANTS
        }
    (out_dir / "arbitrage_research.json").write_text(json.dumps(result, indent=1, default=str))
    print(f"wrote {out_dir / 'arbitrage_research.json'}")


if __name__ == "__main__":
    main()
