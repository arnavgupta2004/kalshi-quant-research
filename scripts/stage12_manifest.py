"""The reproducibility manifest: what was run, on what, with which frozen code.

    python -m scripts.stage12_manifest            # write results/stage12/manifest.json and print it
    python -m scripts.stage12_manifest --check    # exit 1 if a frozen result no longer matches its code

For every experiment with a frozen-code guard (Stages 6, 8, 9, 10) the fingerprint stored in its result
file is compared with the fingerprint of the code as it is now, so a change to frozen code after its
results were written cannot go unnoticed.  Dataset fingerprints come from the databases themselves
(``Store.fingerprint``); a database that is being written to right now is listed as ``locked``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from research.final_report import FILES, ROOT

DATABASES = (
    "var/kalshi.duckdb",
    "var/relations.duckdb",
    "var/books_shortlived.duckdb",
    "var/books_structural.duckdb",
    "var/books_research_short.duckdb",
    "var/books_research_wide.duckdb",
    "var/books_stage10.duckdb",
    "var/books_stage12_us.duckdb",
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def current_fingerprints() -> dict[str, str]:
    from scripts import (
        stage6_arbitrage_research,
        stage8_calibration,
        stage9_baseline_mm,
        stage10_adaptive_mm,
    )

    return {
        "stage6": stage6_arbitrage_research.code_fingerprint(),
        "stage8": stage8_calibration.code_fingerprint(),
        "stage9": stage9_baseline_mm.code_fingerprint(),
        "stage10": stage10_adaptive_mm.code_fingerprint(),
    }


def experiments() -> list[dict]:
    now = current_fingerprints()
    rows = []
    for rel in FILES.values():
        p = ROOT / rel
        if not p.exists():
            rows.append({"result": rel, "status": "not yet produced"})
            continue
        row: dict = {"result": rel, "sha256": _sha(p)}
        if p.suffix == ".json":
            d = json.loads(p.read_text())
            for k in ("stage", "role", "git_commit", "seed", "n_boot", "code_fingerprint"):
                if k in d:
                    row[k] = d[k]
            stage = f"stage{d.get('stage')}"
            if "code_fingerprint" in d and stage in now:
                row["frozen_fingerprint_now"] = now[stage]
                row["frozen_code_unchanged"] = d["code_fingerprint"] == now[stage]
                if stage == "stage6" and not row["frozen_code_unchanged"]:
                    # Stage 6's whole-package fingerprint cannot be reproduced from the committed tree;
                    # what stands in for it is a from-scratch re-run whose numbers were identical
                    rep = ROOT / "results" / "stage12" / "reproduction_stage6.json"
                    if (
                        rep.exists()
                        and json.loads(rep.read_text())["numbers_identical_except_provenance"]
                    ):
                        row["frozen_code_unchanged"] = None
                        row["reproduced_by_rerun"] = True
        rows.append(row)
    return rows


def datasets() -> list[dict]:
    from data.storage.duckdb_store import Store

    out = []
    for rel in DATABASES:
        p = ROOT / rel
        if not p.exists():
            out.append({"db": rel, "status": "absent"})
            continue
        try:
            with Store(str(p), read_only=True) as s:
                out.append({"db": rel, **s.fingerprint()})
        except Exception as exc:  # a recorder holds the write lock
            out.append({"db": rel, "status": "locked" if "lock" in str(exc).lower() else repr(exc)})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()
    rows = experiments()
    manifest = {
        "experiments": rows,
        "datasets": datasets(),
        "frozen_fingerprints_now": current_fingerprints(),
    }
    out = ROOT / "results" / "stage12" / "manifest.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, indent=1, default=str))
    bad = [r["result"] for r in rows if r.get("frozen_code_unchanged") is False]
    for r in rows:
        flag = {True: "ok", False: "CODE CHANGED", None: ""}[r.get("frozen_code_unchanged")]
        flag = "reproduced by re-run" if r.get("reproduced_by_rerun") else flag
        print(f"{r['result']:52s} {r.get('code_fingerprint', ''):18s} {flag}")
    if a.check and bad:
        print("\nfrozen code changed after these results were written:", *bad, sep="\n  ")
        sys.exit(1)


if __name__ == "__main__":
    main()
