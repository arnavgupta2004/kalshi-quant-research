"""Stage 12: the final out-of-sample evaluation, run ONCE on a recording nothing was designed on.

    python -m scripts.stage12_final_oos --db var/books_stage12_us.duckdb --out results/stage12/final \\
        --expect-fingerprint <frozen>

It changes nothing that was frozen.  On the fresh database it runs, exactly as pre-registered:

  1. the Stage 6 arbitrage analysis (Experiments A-D) and scores the same 15 hypotheses;
  2. the Stage 9/10 market-making ladder (baseline, and the adaptive maker with the coefficients fitted
     on the TRAINING databases only) and scores the Stage 10 hypotheses S1-S3, T1-T8;

and writes ``final_oos.json`` with a one-line headline that the research report picks up.

The freeze covers the Stage 6 analysis files as they were when that stage was committed (an explicit
list: the Stage 6 driver hashes whole packages, which changes whenever a new file appears), the Stage 10
code, and this driver.  ``--expect-fingerprint`` makes the run refuse if any of it changed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from data.storage.provenance import git_commit
from research.adaptive_analysis import fit_models
from research.hypotheses import evaluate as score6
from research.signal_study import Samples
from scripts import stage10_adaptive_mm as S10

STAGE6_FILES = tuple(
    f"{pkg}/{name}.py"
    for pkg, names in {
        "research": "__init__ arbitrage_analysis dataset hypotheses latency_analysis scanner stats",
        "arbitrage": "__init__ complementary detector execution exhaustive logical opportunity",
        "backtest": "__init__ engine events feed fills market_info metrics portfolio strategies",
        "market": "__init__ contracts evidence fees order_book relationships semantics timeutil units",
    }.items()
    for name in names.split()
) + ("scripts/stage6_arbitrage_research.py",)  # the 32 modules Stage 6 hashed (minus report.py)
SEED = 20260924


def code_fingerprint() -> str:
    h = hashlib.sha256()
    for f in (*STAGE6_FILES, *S10.CODE, __file__):
        p = Path(f)
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def run_stage6(db: str, out: Path) -> dict:
    """The frozen Stage 6 analysis, in its own process, as it would be run on any recording."""
    subprocess.run(
        [
            sys.executable, "-B", "-m", "scripts.stage6_arbitrage_research", "--role", "confirmatory",
            "--db", db, "--out", str(out), "--engine-sweep",
        ],
        check=True,
    )  # fmt: skip
    return json.loads((out / "arbitrage_research.json").read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="the fresh recording (completed with `refresh`)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-boot", type=int, default=1000)
    ap.add_argument("--expect-fingerprint")
    a = ap.parse_args()
    fp = code_fingerprint()
    if a.expect_fingerprint and a.expect_fingerprint != fp:
        raise SystemExit(f"code fingerprint {fp} != frozen {a.expect_fingerprint}")
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    arb = run_stage6(a.db, out / "stage6")
    rows6 = score6(arb)
    ok6 = sum(r[1] == "consistent" for r in rows6)

    train = Samples.concat([S10._samples(db) for db in S10.TRAIN])
    models = fit_models(train, tuple(Path(d).stem for d in S10.TRAIN))
    block = S10.block((a.db,), models, a.n_boot, sensitivity=True, kappa_grid=True)
    rows10 = S10.score(block)
    ok10 = sum(r[1] == "consistent" for r in rows10)
    inc10 = sum(r[1].startswith("inconclusive") for r in rows10)
    result = {
        "stage": 12,
        "git_commit": git_commit(),
        "code_fingerprint": fp,
        "seed": SEED,
        "database": a.db,
        "headline": (
            f"arbitrage replication {ok6} of {len(rows6)} hypotheses consistent; "
            f"adaptive maker {ok10} of {len(rows10)} consistent"
            + (f" ({inc10} inconclusive)" if inc10 else "")
        ),
        "stage6_hypotheses": [list(r) for r in rows6],
        "stage6_counts": arb["pooled"]["counts"],
        "market_making_hypotheses": [list(r) for r in rows10],
        "market_making": S10._clean(block),
        "models": S10._model_json(models),
    }
    (out / "final_oos.json").write_text(json.dumps(S10._clean(result), indent=1, default=str))
    print(result["headline"], f"fingerprint {fp}")


if __name__ == "__main__":
    main()
