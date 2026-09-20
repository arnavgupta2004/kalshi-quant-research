"""Robustness of the Stage 8 confirmatory result to a nondeterminism in the trade tape.

Trades that share a timestamp (one taker order sweeping several price levels) come back from the database
in arbitrary order, so "the last trade" (and every feature built from it) can differ between runs of the
same code on the same data.  ``pricing/dataset.py`` is frozen with the Stage 7 hypotheses, so the defect is
not patched there; instead this script re-runs the whole confirmatory analysis under several *fixed*
orderings of tied prints and reports whether any headline number or verdict moves.

The re-runs test the implementation, not new hypotheses: they use the same frozen code and the same
research-fitted models, and each opening of the holdout is recorded in its own ``ACCESS_LOG``.

    python -m scripts.stage8_robustness --out results/stage8/robustness
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from pathlib import Path

from pricing import dataset as D
from pricing.microstructure import TradeTape
from scripts import stage8_calibration as S8

ORDERINGS = ("price_asc", "price_desc", "trade_id", "shuffle_1", "shuffle_2")


def ordered_loader(kind: str):
    def load_tapes(store, tickers):
        want = set(tickers)
        rows = store.con.execute(
            "SELECT ticker, epoch_ns(created_time), yes_price, count, taker_side, trade_id "
            "FROM trades ORDER BY ticker, created_time"
        ).fetchall()
        by: dict[str, list] = defaultdict(list)
        for tk, ts, px, cnt, side, tid in rows:
            if tk in want:
                by[tk].append((ts, px, cnt, side, tid))
        rng = random.Random(int(kind.split("_")[1])) if kind.startswith("shuffle") else None
        out = {}
        for tk, v in by.items():
            if kind == "price_asc":
                v.sort(key=lambda r: (r[0], r[1]))
            elif kind == "price_desc":
                v.sort(key=lambda r: (r[0], -r[1]))
            elif kind == "trade_id":
                v.sort(key=lambda r: (r[0], r[4]))
            else:
                rng.shuffle(v)
                v.sort(key=lambda r: r[0])  # stable: ties keep the shuffled order
            out[tk] = TradeTape.from_rows([r[:4] for r in v])
        return out

    return load_tapes


def headline(r: dict) -> dict:
    m = r["history"]["summary"]["market_reference"]
    return {
        "market_log_loss": m["log_loss"],
        "citl": m["calibration_in_the_large"]["value"],
        "slope": m["slope"]["value"],
        "slope_ci": [m["slope"]["lo"], m["slope"]["hi"]],
        "blend_slope_ci": [
            r["history"]["summary"]["blend_market_and_B"]["slope"]["lo"],
            r["history"]["summary"]["blend_market_and_B"]["slope"]["hi"],
        ],
        "verdicts": {
            x["id"]: x["verdict"] for x in r["hypotheses_stage7"] + r["hypotheses_stage8"]
        },
        "observed": {
            x["id"]: x["observed"] for x in r["hypotheses_stage7"] + r["hypotheses_stage8"]
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-boot", type=int, default=2000)
    ap.add_argument("--expect-fingerprint", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    real = D.load_tapes
    results = {}
    for kind in ORDERINGS:
        D.load_tapes = ordered_loader(kind)
        sys.argv = [
            "s8",
            "--role", "confirmatory",
            "--out", str(out / kind),
            "--n-boot", str(args.n_boot),
            "--expect-fingerprint", args.expect_fingerprint,
        ]  # fmt: skip
        try:
            S8.main()
        finally:
            D.load_tapes = real
        results[kind] = headline(json.load(open(out / kind / "calibration.json")))
    (out / "robustness.json").write_text(json.dumps(results, indent=1))
    ids = list(next(iter(results.values()))["verdicts"])
    flips = [i for i in ids if len({r["verdicts"][i] for r in results.values()}) > 1]
    print(f"verdicts that differ across orderings: {flips or 'none'}")


if __name__ == "__main__":
    main()
