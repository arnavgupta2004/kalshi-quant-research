"""Exploratory diagnostic (not part of the frozen analysis): is the loss on a fill larger when the
forecast size of the next move is larger?

The adverse-selection widening ``delta += kappa * sigma`` assumes it is.  Run the baseline with the
scale model attached but ``kappa = 0`` (so it changes nothing), tag each fill with the forecast scale
at the last quote decision before it, and bucket the fills.  Development databases only.

    python -m scripts.stage10_toxicity_diagnostic --out results/stage10/dev/toxicity.json
"""

from __future__ import annotations

import argparse
import bisect
import json
from collections import defaultdict
from pathlib import Path

import numpy as np

from market_making.adaptive import AdaptiveParams
from market_making.baseline import BaselineParams
from research import adaptive_analysis as AA
from research import market_making_analysis as A
from research import signal_study as S
from scripts.stage10_adaptive_mm import LATENCY_MS, MAKER, TRAIN, VALID, _samples

DBS = (VALID[0], *TRAIN)  # books_structural has no trade tape: it adds no fills


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    train = S.Samples.concat([_samples(db) for db in TRAIN])
    models = AA.fit_models(train, tuple(Path(d).stem for d in TRAIN))
    base = BaselineParams()
    params = AdaptiveParams(base=base, scale=models.scale, kappa=0.0)
    rows = []
    for db in DBS:
        d = A.load_mm_data(db, Path(db).stem)
        r = AA.run_variant(d, params, base, MAKER, LATENCY_MS)
        sig = defaultdict(lambda: ([], []))
        for ts, t, _mu, s, _g in r.mm.signals:
            sig[t][0].append(ts)
            sig[t][1].append(s)
        for f in A.fill_table(r):
            times, vals = sig.get(f.ticker, ([], []))
            i = bisect.bisect_right(times, f.ts) - 1
            if i < 0 or f.markout30 is None:
                continue
            settled = (
                None
                if f.settle_pnl is None
                else (f.settle_pnl - f.fee) / A.MICRO * 100 / (f.qty / 100)
            )
            rows.append((Path(db).stem, vals[i] * 100, f.markout30 / 100, f.qty / 100, settled))
    sigma = np.array([r[1] for r in rows])
    mo = np.array([r[2] for r in rows])
    qty = np.array([r[3] for r in rows])
    edges = np.quantile(sigma, [0, 1 / 3, 2 / 3, 1.0])
    terciles = []
    for i in range(3):
        m = (sigma >= edges[i]) & (sigma <= edges[i + 1])
        settled = [r[4] for r, k in zip(rows, m, strict=True) if k and r[4] is not None]
        terciles.append(
            {
                "sigma_hat_cents": [float(edges[i]), float(edges[i + 1])],
                "fills": int(m.sum()),
                "mean_markout_30s_cents": float(mo[m].mean()),
                "contract_weighted_markout_30s_cents": float(np.average(mo[m], weights=qty[m])),
                "mean_settled_pnl_cents_per_contract": float(np.mean(settled)) if settled else None,
                "settled_fills": len(settled),
            }
        )
    slope = np.polyfit(sigma, mo, 1)
    out = {
        "databases": list(DBS),
        "fills": len(rows),
        "terciles_of_forecast_scale": terciles,
        "markout_slope_cents_per_cent_of_sigma": float(slope[0]),
        "markout_intercept_cents": float(slope[1]),
        "corr_sigma_abs_markout": float(np.corrcoef(sigma, np.abs(mo))[0, 1]),
        "note": "fills are not independent (clustered by event); no intervals are claimed",
    }
    p = Path(args.out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
